#!/usr/bin/env python3
"""Approved reference set statistics workflow (ops tooling).

A human reviews a captured stats report, approves it through the
existing `control refstat_approve --confirm-human` command queue, and
later runs `verify` to recompute the same stats on a snapshot and diff
them against the approved reference — a regression gate for stat code
changes and a drift report for data changes.

  mcs_refstats.py capture --name nightly --preset operational
      [--stat X --since ... --until ... --project N]
      [--snapshot PATH] [--data-dir DIR]
  mcs_refstats.py verify  --name nightly [--snapshot PATH] [--data-dir DIR]
  mcs_refstats.py list    [--data-dir DIR]
  mcs_refstats.py pending --name nightly   # print hash for the approve command

Files live under <data-dir>/refstats/{pending,approved}/<name>.json —
they contain aggregate statistics only (never message bodies), but are
still local-only data and stay out of the repo via the existing data/
ignore. Approval is recorded as a `refstat_approval_v1` artifact whose
file_hash pins the exact approved bytes; verify reports a file/artifact
mismatch as tampered rather than silently accepting it.

--data-dir MUST be the live ledger's directory (the approve op derives
the same location from `PRAGMA database_list`); pointing it elsewhere
makes pending captures unreachable by the approver.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

from mcs_util import atomic_write, file_sha256

APPROVAL_KIND = "refstat_approval_v1"
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

# volatile envelope keys live at the RESULT level (snapshot,
# snapshot_age_s, warnings, query, captured_at) — they are siblings of
# "stats", never inside it, so verify diffs the stats subtree as-is;
# stripping deeper would silently hide a stat that emitted such a key


def refstats_dir(data_dir) -> str:
    return os.path.join(str(data_dir), "refstats")


def _name_ok(name: str) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.fullmatch(name))


def _ref_path(data_dir, name: str, stage: str) -> str:
    return os.path.join(refstats_dir(data_dir), stage, name + ".json")


def _diff(want, got, path: str = "") -> list:
    """Recursive structural diff; returns ['path: want -> got', ...]."""
    out = []
    if type(want) is not type(got) and not (
            isinstance(want, int | float) and isinstance(got, int | float)
            and not isinstance(want, bool) and not isinstance(got, bool)):
        return [f"{path or '<root>'}: type {type(want).__name__}"
                f" -> {type(got).__name__}"]
    if isinstance(want, dict):
        for k in sorted(set(want) | set(got)):
            if k not in want:
                out.append(f"{path}.{k}: <missing> -> {got[k]!r}")
            elif k not in got:
                out.append(f"{path}.{k}: {want[k]!r} -> <missing>")
            else:
                out.extend(_diff(want[k], got[k], f"{path}.{k}"))
    elif isinstance(want, list):
        if len(want) != len(got):
            out.append(f"{path}: len {len(want)} -> {len(got)}")
        for i, (w, g) in enumerate(zip(want, got, strict=False)):
            out.extend(_diff(w, g, f"{path}[{i}]"))
    elif want != got:
        # Larger integers can lose their difference when coerced to float.
        if isinstance(want, int | float) and isinstance(got, int | float) \
                and not isinstance(want, bool) and not isinstance(got, bool) \
                and abs(want) < 2**53 and abs(got) < 2**53 \
                and abs(want - got) < 1e-9:
            return out
        out.append(f"{path}: {want!r} -> {got!r}")
    return out


def _load_view(snapshot):
    import mcs_view
    return mcs_view.View(snapshot)


def _receipt_applied(db, approval: dict, artifact_id: int) -> bool:
    """Bind an approval artifact and its exact file bytes to the committed receipt."""
    command_id = approval.get("command_id")
    if not isinstance(command_id, str):
        return False
    from mcs_requests import parse_command, payload_hash, validate
    try:
        row = db.execute(
            "SELECT receipt_json,payload_hash,project_id FROM command_receipts "
            "WHERE command_id=? AND outcome='applied'",
            (command_id,)).fetchone()
    except Exception:
        return False
    if row is None:
        return False
    try:
        receipt = parse_command(row["receipt_json"])
        command = {"version": 1, "cmd": "ops.refstat_approve", "command_id": command_id,
                   "actor": approval.get("actor"), "human_confirmed": True,
                   "project_id": row["project_id"], "name": approval.get("name"),
                   "file_hash": approval.get("file_hash"), "reason": approval.get("reason")}
        return (isinstance(receipt, dict) and validate(command) is None
                and type(receipt.get("project_id")) is int
                and receipt["project_id"] == row["project_id"]
                and receipt.get("payload_hash") == row["payload_hash"] == payload_hash(command)
                and receipt.get("outcome") == "applied" and receipt.get("error") is None
                and receipt.get("command_id") == command_id
                and type(receipt.get("refstat_artifact_id")) is int
                and receipt["refstat_artifact_id"] == artifact_id
                and receipt.get("actor") == approval.get("actor")
                and receipt.get("name") == approval.get("name")
                and receipt.get("file_hash") == approval.get("file_hash"))
    except (ValueError, TypeError, AttributeError, RecursionError):
        return False


def _approvals_in_db(db, name: str):
    """All approval artifacts for `name`, newest first, each verified
    against its command receipt."""
    out = []
    for r in db.execute(
            "SELECT artifact_id,content FROM artifacts WHERE kind=? "
            "ORDER BY artifact_id DESC", (APPROVAL_KIND,)):
        try:
            c = json.loads(r["content"] or "{}")
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(c, dict) and c.get("name") == name \
                and _receipt_applied(db, c, r["artifact_id"]):
            out.append(c)
    return out


def cmd_capture(args) -> int:
    if not _name_ok(args.name):
        print(json.dumps({"ok": False, "error": "bad_name"}))
        return 1
    if args.list:
        # a registry listing pins nothing — a reference set must carry
        # computed stats or verify would pass vacuously
        print(json.dumps({"ok": False, "error": "list_not_capturable"}))
        return 1
    view = _load_view(args.snapshot)
    try:
        # pin the resolved as_of BEFORE computing: unpinned it means
        # "this snapshot's generated_at", which silently re-bases every
        # windowed stat on each republish. generated_at is a float but
        # verify replays the ISO pin via _parse_when (whole seconds), so
        # compute at int(generated_at) too or lower window bounds differ
        if getattr(args, "as_of", None) is None:
            from datetime import datetime
            import mcs_stats
            gen = view.meta.get("generated_at")
            if gen is not None:
                try:
                    args.as_of = datetime.fromtimestamp(
                        int(gen), mcs_stats.JST).isoformat()
                except (ValueError, OverflowError, OSError):
                    raise ValueError("refstat_time_unrepresentable") from None
        result = view.stats(vars(args))
    finally:
        view.close()
    ref = {"schema": "refstat_v1", "name": args.name,
           "captured_at": int(time.time()), **result}
    pending = _ref_path(args.data_dir, args.name, "pending")
    atomic_write(pending, lambda f: json.dump(
        ref, f, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=1),
        tmp_prefix=".capture-")
    print(json.dumps({"ok": True, "name": args.name,
                      "pending": pending,
                      "file_hash": file_sha256(pending),
                      "snapshot": result.get("snapshot")},
                     ensure_ascii=False))
    return 0


def _fold_as_of(stats_obj):
    """scope.as_of is an echo of the (pinned) query value; normalize to
    int so a float captured default and its ISO-string replay compare
    equal."""
    for st in (stats_obj or {}).values():
        scope = st.get("scope") if isinstance(st, dict) else None
        if isinstance(scope, dict) \
                and isinstance(scope.get("as_of"), int | float) \
                and not isinstance(scope["as_of"], bool):
            if isinstance(scope["as_of"], float) and not math.isfinite(scope["as_of"]):
                raise ValueError("refstat_corrupt")
            scope["as_of"] = int(scope["as_of"])


def cmd_pending(args) -> int:
    """Print the approve-command envelope fields for a pending capture."""
    if not _name_ok(args.name):
        print(json.dumps({"ok": False, "error": "bad_name"}))
        return 1
    path = _ref_path(args.data_dir, args.name, "pending")
    if not os.path.isfile(path):
        print(json.dumps({"ok": False, "error": "refstat_not_pending"}))
        return 1
    print(json.dumps({"ok": True, "name": args.name,
                      "file_hash": file_sha256(path)}, ensure_ascii=False))
    return 0


def cmd_verify(args) -> int:
    if not _name_ok(args.name):
        print(json.dumps({"ok": False, "error": "bad_name"}))
        return 1
    path = _ref_path(args.data_dir, args.name, "approved")
    if not os.path.isfile(path):
        print(json.dumps({"ok": False, "error": "refstat_not_approved"}))
        return 1
    try:
        raw = Path(path).read_bytes()
    except OSError:
        print(json.dumps({"ok": False, "error": "refstat_corrupt"}))
        return 1
    file_hash = hashlib.sha256(raw).hexdigest()
    try:
        ref = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"ok": False, "error": "refstat_corrupt"}))
        return 1
    # structural gate — an approved file must be a refstat_v1 dict for
    # this name carrying dict-typed query/stats/snapshot
    if not isinstance(ref, dict) \
            or ref.get("schema") != "refstat_v1" \
            or ref.get("name") != args.name \
            or not isinstance(ref.get("stats"), dict) \
            or not isinstance(ref.get("query"), dict) \
            or not isinstance(ref.get("snapshot") or {}, dict):
        print(json.dumps({"ok": False, "error": "refstat_corrupt"}))
        return 1
    view = _load_view(args.snapshot)
    try:
        approvals = _approvals_in_db(view.db, args.name)
        result = view.stats(ref.get("query") or {})
    finally:
        view.close()
    approval = next((a for a in approvals
                     if a.get("file_hash") == file_hash), None)
    ref_stats, got_stats = ref["stats"], result.get("stats") or {}
    _fold_as_of(ref_stats)
    _fold_as_of(got_stats)
    diffs = _diff(ref_stats, got_stats)
    ref_snapshot = ref.get("snapshot") or {}
    same_snapshot = (ref_snapshot.get("generation_id")
                     == (result.get("snapshot") or {})
                     .get("generation_id"))
    # statuses: unverified (no approval artifact for these exact bytes),
    # superseded (a NEWER approval exists for a different hash — the
    # file was rolled back or replaced), match, drift, regression
    if approval is None:
        status = "unverified"
    elif approvals[0].get("file_hash") != file_hash:
        status = "superseded"
    elif not diffs:
        status = "match"
    else:
        status = "drift" if not same_snapshot else "regression"
    report = {"ok": True, "name": args.name, "status": status,
              "file_hash": file_hash,
              "approved": approval is not None,
              "approval": ({k: approval.get(k) for k in
                            ("actor", "reason", "approved_at")}
                           if approval else None),
              "same_snapshot": same_snapshot,
              "reference_snapshot": ref_snapshot,
              "current_snapshot": result.get("snapshot"),
              "diffs": diffs}
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if status == "match" else 2


def cmd_list(args) -> int:
    out = {"pending": [], "approved": []}
    for stage in ("pending", "approved"):
        d = os.path.join(refstats_dir(args.data_dir), stage)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                p = os.path.join(d, f)
                if f.endswith(".json") and _name_ok(f[:-5]) \
                        and os.path.isfile(p) and not os.path.islink(p):
                    try:
                        digest = file_sha256(p)
                    except OSError:
                        continue
                    out[stage].append({"name": f[:-5], "path": p,
                                       "file_hash": digest})
    print(json.dumps(out, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--name", required=True)
    group = c.add_mutually_exclusive_group(required=True)
    group.add_argument("--stat")
    group.add_argument("--preset")
    group.add_argument("--list", action="store_true")
    c.add_argument("--since")
    c.add_argument("--until")
    c.add_argument("--as-of", dest="as_of")
    c.add_argument("--project", type=int)
    c.add_argument("--limit", type=int, default=20)
    c.set_defaults(fn=cmd_capture)
    v = sub.add_parser("verify")
    v.add_argument("--name", required=True)
    v.set_defaults(fn=cmd_verify)
    p = sub.add_parser("pending")
    p.add_argument("--name", required=True)
    p.set_defaults(fn=cmd_pending)
    ls = sub.add_parser("list")
    ls.set_defaults(fn=cmd_list)
    for s in (c, v):
        s.add_argument("--snapshot",
                       default=os.path.expanduser(
                           "~/.mcs/data/snapshots/ledger-snapshot.db"))
    for s in (c, v, p, ls):
        s.add_argument("--data-dir",
                       default=os.path.expanduser("~/.mcs/data"))
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, OSError, sqlite3.Error, RecursionError,
            TypeError, AttributeError) as e:
        code = str(e) if type(e) is ValueError and \
            str(e).replace("_", "").isalnum() else type(e).__name__
        print(json.dumps({"ok": False, "error": code}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
