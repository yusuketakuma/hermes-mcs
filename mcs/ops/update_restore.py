"""Restore prior source and data evidence without weakening rollback fences."""
from __future__ import annotations
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
from contextlib import suppress
import service_management  # noqa: E402
import mcs_util  # noqa: E402
import update_journal as journal_store  # noqa: E402
import restore_evidence  # noqa: E402
import ledger  # noqa: E402
import notify_spool  # noqa: E402
import notify_reconcile  # noqa: E402
import notify_flush  # noqa: E402,F401 - restore text quarantine
import notify_digest  # noqa: E402,F401 - restored digest card reissue
import notify_urgent  # noqa: E402,F401 - restored alert settlement
import alert_view  # noqa: E402,F401 - restored alert card reissue
import extraction_progress  # noqa: E402,F401 - restored card presentation
import present_discord  # noqa: E402,F401 - restored card presentation
import present_slack  # noqa: E402,F401 - restored card presentation
import present_lineworks  # noqa: E402,F401 - restored card presentation
import local_llm  # noqa: E402,F401 - postcheck model/admission diagnostics
import llm_admission  # noqa: E402,F401 - postcheck admission diagnostics
import mcs_restore  # noqa: E402,F401 - existing backup-adoption marker gate
import cloud_inference  # noqa: E402,F401 - rollback cloud-contract validation
from mcs_util import file_sha256 as _file_sha256  # noqa: E402
from safe_io import fsync_dir, fdopen_owned  # noqa: E402

from update_execution import (
    _record_attempt,
)
from update_lifecycle_context import (
    DATA,
    LEDGER,
    RESTORE_REPORT_PATH,
    RestoreConsentPending,
    STATE_PATH,
    UpdateError,
    _CARD_LAYOUT_RE,
    _STAGING_KEYS,
    _STAGING_NAME,
    _record_executed,
    _report,
    _service_paths,
)
from update_preflight import (
    _fresh_rollback_state,
    _provider_rollback_preflight,
    _standalone_target_supported,
)
from update_repository import (
    _git,
    _git_out,
    _head_sha,
    _local_changes_outside,
    _tree_clean,
)
from update_runtime_migration import (
    _restore_runtime,
    _services_reconcile,
)

def _consent_hold_record(e, backup_path) -> dict:
    """The restore_consent hold record — the SAME report_id binds every
    later ops.restore_approve receipt, so all three hold sites build it
    identically."""
    return {"report_id": e.report["report_id"],
            "backup_path": backup_path,
            "backup_sha256": e.report["backup_sha256"],
            "backup_schema": e.report["backup_schema"],
            "intervening_messages": e.report["intervening_messages"],
            "external_effect_rows": e.report["external_effect_rows"],
            "at": time.time()}


def _consent_hold(state, e, backup_path) -> str:
    """Persist a consent hold (record + state save + operator-facing
    pending report). Returns the binding report_id."""
    rid = e.report["report_id"]
    state["restore_consent"] = _consent_hold_record(e, backup_path)
    journal_store.save_state(STATE_PATH, state)
    _report("restore_consent_pending",
            rid + " — restore_report.json を確認し "
            "ops.restore_approve で承認。" + restore_evidence.RESTORE_LOSS_NOTICE)
    return rid


def _rollback_applying(entry, prev_sha, command_id) -> dict:
    """The rollback-shaped 'applying' journal record (mirrored by
    mcs_recover's rollback_shaped check): target is entry's prev_sha."""
    return {
        "tag": "rollback:" + (entry.get("tag") or "?"),
        "sha": entry["prev_sha"],
        "prev_sha": prev_sha,
        "rollback": True,
        "plugin_changed": entry.get("plugin_changed"),
        "schema_bump": entry.get("schema_bump"),
        "backup_path": entry.get("backup_path"),
        "manifest_snapshot": entry.get("manifest_snapshot"),
        **({"runtime_migration": entry["runtime_migration"]}
           if entry.get("runtime_migration") else {}),
        **({"target_kind": "release"} if entry.get('target_kind') == 'release' else {}),
        **({"release_ref": entry["release_ref"]} if "release_ref" in entry else {}),
        "command_id": command_id,
        "at": time.time()}


def _hold_rollback_for_consent(state, e, tag, command_id,
                               reason) -> int:
    """Rollback reached the DB replace and is held on consent: keep
    drainers stopped, both markers up, and a rollback-shaped 'applying'
    record — the tree is already reset to prev_sha, so the next recover
    pass must see head == target and reach _restore_db again (an
    apply-shaped record would classify as head == prev and drop the
    consent hold without ever restoring)."""
    rid = e.report["report_id"]
    entry = state["applying"]
    if not entry.get("rollback"):
        state["applying"] = _rollback_applying(
            entry, entry.get("sha"), command_id or entry.get("command_id"))
    reason += " (rollback held: consent pending " + rid[:16] + "…)"
    if command_id:
        _record_executed(state, command_id, "failed", reason)
    _record_attempt(state, tag, "failed", reason)
    _consent_hold(state, e, state["applying"].get("backup_path"))
    return 2


def _rollback_backup_error(entry: dict) -> str | None:
    """A schema-bump rollback restores its preupdate backup.
    Maintenance retains actionable references, but a missing or invalid
    copy must still block rollback. rollback() checks it before journaling or resetting:
    old code must never be left running against the newer schema. (The
    apply bail path keeps reset-then-escalate: its backup is fresh and
    its journal must stay classifiable as a pending restore.)"""
    if not entry.get("schema_bump"):
        return None
    path = entry.get("backup_path")
    if not isinstance(path, str) or not path.strip():
        return "backup_invalid: missing or invalid backup path"
    if ledger.valid_mcs_db(path):
        return None
    return "backup_invalid: " + path


def _target_card_layout(ref: str) -> int:
    """The card layout the code at ref renders; 1 before the constant existed."""
    path = "mcs/notify/notify_cards.py"
    r = _git(["show", f"{ref}:{path}"])
    if r.returncode != 0:
        # Only a successfully read tree can establish that this predates cards.
        # A listed but unreadable blob (or an unreadable tree) is not legacy.
        if not _git_out(["ls-tree", "-z", ref, "--", path]):
            return 1
        raise UpdateError("target_card_layout_unreadable")
    m = _CARD_LAYOUT_RE.search(r.stdout)
    return int(m.group(1)) if m else 1


def _rollback_layout_error(entry: dict) -> str | None:
    """Refuse a rollback whose target code cannot read the stored card layout.

    Old code has no guard for cards of a newer layout: it reads them as
    drifted and re-renders/re-sends every one. Refuse before any side
    effect; the way back is the human-consent restore, not old code on a
    newer ledger. Checks the DB that will be live after the rollback and
    passes only on a readable MAX(layout) or a provably absent cards table —
    a DB that cannot be read is unverified, never safe."""
    path = (entry["backup_path"] if entry.get("schema_bump")
            and entry.get("backup_path") else LEDGER)
    if not os.path.isfile(path):
        return None
    try:
        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                              uri=True)
        try:
            columns = {row[1] for row in con.execute(
                "PRAGMA table_info(notification_cards)")}
            if "layout" not in columns:
                # no cards table, or a schema older than the layout column:
                # every card is layout 1 and nothing can be misread
                return None
            # Revoked/deleted cards remain sweep inputs and may owe a remote
            # revoke or a new render. Their stored layout is still a rollback
            # contract; delivery state alone cannot prove they are disposable.
            stored = con.execute(
                "SELECT COALESCE(MAX(layout),1) FROM notification_cards"
            ).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as e:
        if path != LEDGER:
            # an unreadable backup already fails closed downstream:
            # rollback() rejects it in _rollback_backup_error and the bail
            # path's _restore_db escalates with writers stopped. Keep the
            # backup validation in those paths; recovery preserves an
            # incomplete restore until the bound target is restored.
            return None
        return f"rollback_layout_unverified: {e}"
    try:
        target = _target_card_layout(entry["prev_sha"])
    except UpdateError as e:
        return f"rollback_layout_unverified: {e}"
    if stored <= target:
        return None
    return (f"rollback_blocked_layout_floor: ledger layout {stored} > "
            f"target layout {target} ({entry['prev_sha'][:12]})")


def _rollback_tree(entry: dict, on_hold=None, *, expected_rollback=None) -> None:
    """Restore tracked files to prev_sha — shared by rollback() and the
    post-merge failure path. Caller holds both locks and drainers are
    already quiesced. Do not additionally delete untracked paths just
    because their names occur in the update diff; ownership is unproven."""
    prev = entry["prev_sha"]
    backup_path = entry.get("backup_path")
    if entry.get("schema_bump") and (not isinstance(backup_path, str) or not backup_path.strip()):
        raise UpdateError("backup_invalid: missing or invalid backup path")
    if expected_rollback is not None:
        _fresh_rollback_state(expected_rollback)
    _provider_rollback_preflight(prev)
    if mcs_util.load_config().get("runtime_mode") == "standalone" and not _standalone_target_supported(prev):
        raise UpdateError("standalone_runtime_missing_in_target")
    err = _rollback_layout_error(entry)
    if err:
        raise UpdateError(err)
    foreign = _local_changes_outside(entry.get("sha"), prev)
    if foreign:
        raise UpdateError("local changes outside update: " + ",".join(foreign)[:160])
    # runtime first: once the tree is back on prev, an older updater that
    # knows nothing of runtime_migration may run the next recovery
    _restore_runtime(entry)
    _git_out(["reset", "--hard", prev])
    if _head_sha() != prev or not _tree_clean():
        raise UpdateError("rollback_verify_failed")
    if entry.get("schema_bump") and entry.get("backup_path"):
        expected = expected_rollback
        _restore_db(entry["backup_path"], on_hold=on_hold)
        if expected is not None:
            _fresh_state, current = _fresh_rollback_state(expected)
            if "restore_staging" in current:
                raise UpdateError("rollback_restore_staging_unverified")
    _cancel_unsent_notices()
    problems = service_management.retire_to_snapshot(entry.get('manifest_snapshot') if 'manifest_snapshot' in entry else journal_store.load_state(STATE_PATH).get('manifest_snapshot'), cfg=mcs_util.load_config(), paths=_service_paths())
    _services_reconcile()
    if problems:
        raise UpdateError("membership: " + ",".join(problems))


def _cancel_unsent_notices() -> int:
    """After a tree rollback, a queued never-attempted render still
    carries the newer renderer's spec (e.g. preview_text, post_actions),
    which the restored worker rejects forever while the runner counts it
    as live. Cancel it — the restored dispatch issues a fresh notice
    render for the same event, and the restored sweep a fresh card render
    for the unbound intents or drift, as for a moved route. Best-effort."""
    if not os.path.isfile(LEDGER):
        return 0
    try:
        con = sqlite3.connect(Path(LEDGER).resolve().as_uri() + "?mode=rw",
                              uri=True, timeout=30)
        try:
            with con:
                now = time.time()
                rows = con.execute(
                    "SELECT delivery_id,card_id,intent_event_id "
                    "FROM notification_renders r WHERE state='queued' "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM notification_delivery_attempts a "
                    "WHERE a.delivery_id=r.delivery_id "
                    "AND a.state IN ('granted','unknown'))").fetchall()
                for delivery_id, card_id, event_id in rows:
                    con.execute(
                        "UPDATE notification_renders SET state='cancelled',"
                        "updated_at=? WHERE delivery_id=? AND state='queued'",
                        (now, delivery_id))
                    if card_id is None:
                        con.execute(
                            "UPDATE notify_outbox SET next_try=?,updated_at=? "
                            "WHERE event_id=? AND state='pending'",
                            (now, now, event_id))
                    else:
                        con.execute(
                            "UPDATE notification_intent_cards SET "
                            "delivery_id=NULL,required_render_rev=0 "
                            "WHERE delivery_id=?", (delivery_id,))
            return len(rows)
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def _reconcile_restored() -> None:
    """Post-swap journal-vs-DB reconcile while writers stay quiesced —
    on failure the restore_pending marker stays up (send grants keep
    denying) and the caller escalates for the next pass to retry."""
    # The captured writer belongs to the newer generation. Opening a restored
    # older DB through it would migrate it before the old tree resumes. Keep
    # the restored marker and the existing incomplete-restore journal instead.
    version = restore_evidence.database_version(LEDGER)
    if version != ledger.SCHEMA_VERSION:
        state = journal_store.load_state(STATE_PATH)
        applying = state.get("applying")
        if isinstance(applying, dict):
            applying["restore_incomplete"] = True
            journal_store.save_state(STATE_PATH, state)
        raise UpdateError("restore_reconcile_schema_blocked")
    restored = ledger.Ledger(LEDGER)
    try:
        report = notify_reconcile.reconcile_after_restore(restored, mcs_util.load_config())
        if report.get("journal_incomplete"):
            raise ValueError("restore_journal_incomplete")
    except Exception as e:
        raise UpdateError(
            f"restore_reconcile_failed: {type(e).__name__}") from e
    finally:
        restored.close()


def _staging_record(state):
    """(state, applying, record) of a usable journal — OSError otherwise."""
    if state.get("_corrupt"):
        raise OSError("restore_staging_journal_corrupt")
    applying = state.get("applying")
    if not isinstance(applying, dict):
        raise OSError("restore_staging_unjournaled")
    record = applying.get("restore_staging")
    if record is not None and not (
            isinstance(record, dict) and set(record) == {"name", *_STAGING_KEYS}
            and isinstance(record["name"], str) and _STAGING_NAME.fullmatch(record["name"])
            and all(type(record[key]) is int for key in _STAGING_KEYS)):
        raise OSError("restore_staging_record_invalid")
    return applying, record


def _staging_matches(dfd, record) -> bool | None:
    """None when the recorded name is gone; whether it is provably ours."""
    try:
        st = os.stat(record["name"], dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (stat.S_ISREG(st.st_mode) and st.st_nlink == 1
            and st.st_uid == os.getuid() == record["uid"]
            and (st.st_dev, st.st_ino) == (record["dev"], record["ino"]))


def _settle_staging(directory) -> None:
    """Under both locks, before a new copy: remove the recorded copy of an
    interrupted restore, or keep everything and refuse when it cannot be
    proven to be that copy."""
    state = journal_store.load_state(STATE_PATH)
    applying, record = _staging_record(state)
    if record is None:
        return
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        matches = _staging_matches(dfd, record)
        if matches is False:
            raise OSError("restore_staging_identity_changed")
        if matches:
            os.unlink(record["name"], dir_fd=dfd)
            os.fsync(dfd)
    finally:
        os.close(dfd)
    applying.pop("restore_staging")
    journal_store.save_state(STATE_PATH, state)


def _release_staging(directory, record, journaled=True) -> None:
    """After this attempt: remove our copy if it is still exactly ours and
    drop its record; a mismatch keeps both for a human."""
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        matches = _staging_matches(dfd, record)
        if matches is False:
            return
        if matches:
            os.unlink(record["name"], dir_fd=dfd)
            os.fsync(dfd)
    finally:
        os.close(dfd)
    if not journaled:
        return
    state = journal_store.load_state(STATE_PATH)
    applying, current = _staging_record(state)
    if current == record:
        applying.pop("restore_staging")
        journal_store.save_state(STATE_PATH, state)


def _replace_database(backup_path, expected_sha, before_replace):
    """Stage and verify the backup before checkpointing and replacing the live DB.
    The staging copy is journaled (applying.restore_staging) before any byte
    of the backup is copied; there is no unjournaled fallback."""
    directory = os.path.dirname(LEDGER)
    _settle_staging(directory)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".restore.", suffix=".db")
    try:
        created = os.fstat(fd)
    except BaseException:
        with suppress(OSError):
            os.close(fd)
        # Without the inode witness this path cannot be safely removed.
        raise
    own = {"name": os.path.basename(temporary), "dev": created.st_dev,
           "ino": created.st_ino, "uid": created.st_uid}
    journaled = False
    try:
        with fdopen_owned(fd, "wb") as dst:
            state = journal_store.load_state(STATE_PATH)
            applying, _ = _staging_record(state)
            applying["restore_staging"] = own
            journal_store.save_state(STATE_PATH, state)
            journaled = True
            with open(backup_path, "rb") as src:
                shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if _file_sha256(temporary) != expected_sha:
            raise OSError("backup_changed_during_restore")
        candidate = sqlite3.connect(
            Path(temporary).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            if candidate.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise OSError("backup_integrity_failed")
        finally:
            candidate.close()
        # Preserve committed WAL contents even if the subsequent replace fails.
        # SQLite owns journal removal; never unlink a live WAL by hand.
        live = sqlite3.connect(Path(LEDGER).resolve().as_uri() + "?mode=rw",
                               uri=True, timeout=5)
        try:
            if live.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                raise OSError("live_checkpoint_busy")
            if live.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise OSError("live_journal_busy")
        finally:
            live.close()
        # = deployment/recovery/mcs_recover._replace_database: same lexists
        # sidecar rule, kept as an independent copy (recovery must run
        # without the repo's modules)
        if any(os.path.lexists(LEDGER + suffix)
               for suffix in ("-wal", "-shm", "-journal")):
            raise OSError("live_sidecars_remaining")
        before_replace()
        os.replace(temporary, LEDGER)
        fsync_dir(directory, require_dir=False)
    except sqlite3.Error as exc:
        raise OSError("restore_database_unverifiable") from exc
    finally:
        with suppress(OSError):          # never mask the restore's own failure
            _release_staging(directory, own, journaled)


def _restore_db(backup_path: str, on_hold=None) -> None:
    """Verified restore — validate the backup before touching the live
    DB. Skips when the live schema already matches the backup (a failed
    apply may never have migrated). Stages the private copy, verifies its
    approved hash, then checkpoints the live DB before replacement so a
    failed copy cannot erase committed WAL data. Callers quiesce every writer.
    A real replace requires a per-restore human consent receipt bound
    to the exact backup bytes/schema/loss report (R2 — an earlier
    update or rollback approval does not substitute): missing consent
    raises RestoreConsentPending and callers must HOLD, not unwind.
    Loses records written between apply and restore; Discord posts
    already sent cannot be retracted (design R2)."""
    marker = notify_spool.restore_pending(DATA)
    if marker and marker.get("unreadable"):
        raise UpdateError("restore_marker_unreadable")
    if not ledger.valid_mcs_db(backup_path):
        raise UpdateError("backup_invalid: " + backup_path)
    live_ver = restore_evidence.database_version(LEDGER)
    if live_ver is None:
        raise UpdateError("live_db_unreadable")
    back_ver = restore_evidence.database_version(backup_path)
    if live_ver == back_ver:
        # A remaining restore marker still requires reconciliation. A prior
        # swap may already have restored the backup version; version equality
        # alone must not finish a schema-blocked or interrupted reconcile.
        if marker is not None:
            if notify_spool.restore_awaiting_consent(DATA) is not None:
                try:
                    notify_spool.mark_restored(DATA, backup_path=backup_path,
                                               by="mcs_update")
                except OSError as e:
                    raise UpdateError(
                        f"restore_marker_failed: {e}") from e
            _reconcile_restored()
        return
    # Sender hold BEFORE measuring the loss — a grant slipping between
    # the count and the swap would lose its record silently.
    try:
        notify_spool.mark_restored(DATA, backup_path=backup_path,
                                   by="mcs_update",
                                   phase="awaiting_consent")
    except OSError as e:
        raise UpdateError(f"restore_marker_failed: {e}") from e
    try:
        report = restore_evidence.create_loss_report(
            live_path=LEDGER, backup_path=backup_path, report_path=RESTORE_REPORT_PATH)
    except restore_evidence.RestoreEvidenceError as exc:
        raise UpdateError(str(exc)) from exc
    try:
        # re-pin the hold with the report identity so operators and the
        # watchdog can correlate marker <-> restore_report.json
        notify_spool.mark_restored(DATA, backup_path=backup_path,
                                   by="mcs_update",
                                   phase="awaiting_consent",
                                   report_id=report["report_id"])
    except OSError as e:
        raise UpdateError(f"restore_marker_failed: {e}") from e
    if on_hold is not None:
        on_hold(report)  # Persist the freeze before even an approved swap can fail.
    if restore_evidence.find_restore_consent(report, live_path=LEDGER) is None:
        raise RestoreConsentPending(report)
    # Mark BEFORE the file swap: a crash after the replace but before
    # the marker write must not leave senders running against a rewound
    # DB. A stale marker is harmless — the next tick's reconcile finds
    # nothing to hold and clears it.
    try:
        _replace_database(backup_path, report["backup_sha256"],
                          lambda: notify_spool.mark_restored(
                              DATA, backup_path=backup_path, by="mcs_update",
                              report_id=report["report_id"]))
    except OSError as e:
        # A failed swap after the restored marker was written must still
        # hold writers on the live newer schema. Callers honor this marker.
        try:
            notify_spool.mark_restored(
                DATA, backup_path=backup_path, by="mcs_update",
                phase="awaiting_consent", report_id=report["report_id"])
        except OSError as marker_error:
            _consent_hold(journal_store.load_state(STATE_PATH), RestoreConsentPending(report), backup_path)
            raise UpdateError(f"restore_marker_failed: {marker_error}") from e
        raise UpdateError(f"restore_failed: {e}") from e
    if not ledger.valid_mcs_db(LEDGER):
        raise UpdateError("restore_verify_failed")
    # The DB just rewound — records of sends between the backup and now
    # are gone while the remote messages/journal survive. Reconcile
    # journal vs restored DB here, while drainers are still quiesced;
    # if it fails the restore_pending marker stays up (send grants keep
    # denying) and the escalate path reports it for the next tick to
    # retry.
    _reconcile_restored()
