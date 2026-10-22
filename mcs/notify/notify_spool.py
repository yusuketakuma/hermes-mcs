"""Publish committed notification specs and restore gates to the worker filesystem.

The caller owns borrowed SQLite lifetime and commits new renders before
publish_specs. Each publication witness commits separately. Recovery repairs
already committed stored bytes in its existing transaction; it never sends or
consumes worker claims. Scope/config APIs accept plain values, never providers.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import suppress

import mcs_runtime
from mcs_requests import canonical
from safe_io import atomic_write
import notify_scope

RESTORE_MARKER = "restore_pending.json"


RESTORE_RECEIPT = "restore_reconcile.json"


def data_root(ledger) -> str:
    """The ledger's own data dir (…/.mcs/data) — derived, never assumed."""
    main = getattr(ledger, "db", ledger).execute(
        "PRAGMA database_list").fetchone()["file"]
    return os.path.dirname(os.path.abspath(main))


def notify_dirs(root: str) -> dict:
    return {name: os.path.join(root, name) for name in
            ("discord_render", "discord_state", "slack_render", "slack_state",
             "lineworks_render", "lineworks_state", "flags",
             "cmd_int", "cmd_results")}


def ensure_dirs(root: str) -> None:
    for path in notify_dirs(root).values():
        os.makedirs(path, mode=0o700, exist_ok=True)


def restore_pending(root: str) -> dict | None:
    """A restore marker blocks every send grant until reconcile runs.
    A corrupt or unreadable marker still blocks — fail closed."""
    path = os.path.join(root, RESTORE_MARKER)
    backup_restore = any(os.path.lexists(os.path.join(root, name)) for name in (
        "restore.json", "backup_restore_approval.json",
        "backup_restore_resume.json", "backup_restore_blocked.json"))
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except FileNotFoundError:
        return {"unreadable": True} if backup_restore or os.path.lexists(path) else None
    except (OSError, ValueError, RecursionError):
        return {"unreadable": True}
    if (backup_restore or isinstance(data, dict)
            and data.get("by") == "mcs_backup_restore") and (
            not isinstance(data, dict)
            or data.get("by") != "mcs_backup_restore"
            or data.get("phase") != "awaiting_consent"):
        return {"unreadable": True}
    if isinstance(data, dict) and (
            data.get("phase") in ("restored", "awaiting_consent")
            or ("phase" not in data and "restored_at" in data)):
        return data
    return {"unreadable": True}


def mark_restored(root: str, backup_path=None, by="manual",
                  now=None, phase="restored", report_id=None) -> str:
    """Durable 'a restore happened' witness — written by every restore
    path so the runner can hold sends until reconcile completes.
    phase='awaiting_consent' holds sends while a schema-bump rollback
    waits on its per-restore human approval; nothing was swapped yet,
    so restored_at stays unset and reconcile must NOT clear it."""
    marker = {"v": 1, "phase": phase, "backup_path": backup_path,
              "by": by, "at": now if now is not None else time.time()}
    if phase == "restored":
        marker["restored_at"] = marker["at"]
    if report_id is not None:
        marker["report_id"] = report_id
    return publish_file(root, RESTORE_MARKER, canonical(marker))


def restore_awaiting_consent(root: str) -> dict | None:
    """Hold writers for pending consent or an unreadable restore phase."""
    marker = restore_pending(root)
    if isinstance(marker, dict) and marker.get("by") == "mcs_backup_restore":
        from mcs_restore import writers_resumed
        if writers_resumed(root):
            # Adoption does not authorize delivery or reconciliation. Keep the
            # awaiting_consent marker intact for both of those independent gates.
            return None
    if isinstance(marker, dict) \
            and (marker.get("phase") == "awaiting_consent"
                 or marker.get("unreadable")):
        return marker
    return None


def clear_restore_pending(root: str) -> None:
    marker = restore_pending(root)
    if ((marker or {}).get("by") == "mcs_backup_restore"
            or os.path.lexists(os.path.join(root, "restore.json"))):
        raise ValueError("backup_restore_delivery_remains_held")
    with suppress(FileNotFoundError):
        os.unlink(os.path.join(root, RESTORE_MARKER))


def publish_file(directory: str, name: str, raw: bytes) -> str:
    """mkstemp -> fsync -> os.replace -> dir fsync. Never a partial file."""
    path = os.path.join(directory, name)
    atomic_write(path, lambda stream: stream.write(raw), binary=True,
                 tmp_prefix=".pub-", tmp_suffix="", makedirs=False,
                 require_dir=False)
    return path


def publish_flags(cfg: dict, root: str) -> bool:
    """flags/notify.json — the plugin's DB-free view of the effective
    interactive/kill-switch state. Published only when it changes."""
    n = notify_scope.notify_cfg(cfg)
    flags = {
        "interactive": notify_scope.interactive_enabled(cfg),
        "kill_switch": not notify_scope.interactive_enabled(cfg),
        "transport": notify_scope.active_transport(cfg),
        "route_epoch": notify_scope.route_epoch(cfg),
        "card_thread": n.get("card_thread") is True,
        "restore_pending": restore_pending(root) is not None,
        # the Hermes plugin stands down when MCS runs its own connector
        "runtime_mode": mcs_runtime.mode(cfg),
        "at": time.time(),
    }
    raw = canonical(flags)
    path = os.path.join(notify_dirs(root)["flags"], "notify.json")
    try:
        with open(path, "rb") as f:
            old = f.read()
    except OSError:
        old = None
    if old is not None:
        try:
            prior = json.loads(old)
            if isinstance(prior, dict):
                prior.pop("at", None)
                cur = dict(flags)
                cur.pop("at", None)
                if canonical(prior) == canonical(cur):
                    return False
        except (ValueError, RecursionError):
            pass
    publish_file(os.path.dirname(path), "notify.json", raw)
    return True


def _cleanup_budget_held(db, delivery_id):
    return db.execute("SELECT 1 FROM notification_render_parts WHERE delivery_id=? AND state='held' "
                      "AND error_code IN ('thread_cleanup_budget_exceeded','part_budget_exceeded',"
                      "'blank_part_unrepresentable') "
                      "LIMIT 1", (delivery_id,)).fetchone() is not None


def publish_specs(db, dirs, specs, now) -> list:
    """Atomic file publication AFTER the render commit. A crash between
    leaves spec_published=0; recovery republishes identical bytes."""
    published = []
    for spec in specs:
        if _cleanup_budget_held(db, spec["delivery_id"]):
            continue
        transport = spec["delivery"].get("transport", "discord")
        path = publish_file(dirs[transport + "_render"],
                            spec["delivery_id"] + ".json",
                            canonical(spec))
        # per spec: a later publish error must not leave this UPDATE in
        # an open implicit transaction (callers' next BEGIN would fail)
        with db:
            db.execute(
                "UPDATE notification_renders SET spec_published=1,"
                "first_published_at=COALESCE(first_published_at,?),"
                "updated_at=? WHERE delivery_id=?",
                (now, now, spec["delivery_id"]))
        published.append(path)
    return published


def recover(ledger, cfg, result) -> dict:
    """Startup/periodic recovery for the file-published plane: republish
    queued/held renders and delivered plans with pending parts whose
    spec file is missing, repair the flags file, and surface stale
    claims for the health layer. Never consumes
    .claimed work — a claim is not proof the worker is dead."""
    db = getattr(ledger, "db", ledger)
    root = data_root(ledger)
    dirs = notify_dirs(root)
    ensure_dirs(root)
    now = time.time()
    fixed = {"republished": 0, "missing_claimed": 0}
    # Read and publish outside any write transaction (stat + fsync per
    # render must not hold the ledger lock), then one short transaction
    # that updates only rows still in the state that was read.
    marks = []
    for r in db.execute(
            "SELECT delivery_id,spec_json,transport,state FROM notification_renders r "
            "WHERE state IN ('queued','held') OR (state='delivered' "
            "AND EXISTS (SELECT 1 FROM notification_render_parts p "
            "WHERE p.delivery_id=r.delivery_id AND p.state='pending'))").fetchall():
        if _cleanup_budget_held(db, r["delivery_id"]):
            continue
        path = os.path.join(dirs[r["transport"] + "_render"],
                            r["delivery_id"] + ".json")
        if os.path.isfile(path):
            marks.append((r, False))
        elif r["spec_json"]:
            publish_file(dirs[r["transport"] + "_render"],
                         r["delivery_id"] + ".json",
                         r["spec_json"].encode("utf-8"))
            marks.append((r, True))
    with db:
        db.execute("BEGIN IMMEDIATE")
        for r, republished in marks:
            if republished:
                n = db.execute(
                    "UPDATE notification_renders SET spec_published=1,"
                    "first_published_at=COALESCE(first_published_at,?),"
                    "updated_at=? WHERE delivery_id=? AND state=?",
                    (now, now, r["delivery_id"], r["state"])).rowcount
                fixed["republished"] += n
            else:
                db.execute(
                    "UPDATE notification_renders SET spec_published=1 "
                    "WHERE delivery_id=? AND state=?",
                    (r["delivery_id"], r["state"]))
    retired = notify_scope.retired_transports(cfg)
    stale = {"live": 0, "retired": 0}
    for t in notify_scope.SUPPORTED_TRANSPORTS:
        try:
            names = [os.path.join(dirs[t + "_render"], n)
                     for n in os.listdir(dirs[t + "_render"])
                     if n.endswith(".json.claimed")]
        except OSError:
            continue
        for n in names:
            with suppress(OSError):
                if now - os.stat(n).st_mtime > 60:
                    stale["retired" if t in retired else "live"] += 1
    # a claim left on a transport switched away from is disclosed,
    # never live delivery trouble (the claim itself is kept)
    fixed["missing_claimed"] = stale["live"]
    fixed["retired_claimed"] = stale["retired"]
    if stale["live"]:
        result.setdefault("errors", []).append(
            f"claimed_specs_stale:{stale['live']}")
    publish_flags(cfg, root)
    return fixed
