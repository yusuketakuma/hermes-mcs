"""未発行のSlack・Discord・LINE WORKS通知と未送信が確定した集計通知を、履歴を残して取り消す。"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: F401,E402

import initial_import_state

from mcs_util import valid_text  # noqa: E402
from mcs_requests import canonical, payload_hash, valid_uuid  # noqa: E402
from writer_lock import acquire_run_lock
from safe_io import atomic_write, fsync_dir  # noqa: E402  # noqa: E402
from notify_cards import mark_snapshot_dirty  # noqa: E402
from card_worker_status import REASONS, TRANSPORTS, VERSION  # noqa: E402
from notify_spool import notify_dirs  # noqa: E402
from safe_io import read_regular_nofollow  # noqa: E402


def _target(db, event_id, requested_event_ids):
    row = db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (event_id,)).fetchone()
    if row is None or row["route"] != "interactive":
        raise ValueError("unsupported_event")
    progress = json.loads(row["progress"] or "{}")
    if not isinstance(progress, dict) or progress.keys() - {"wait", "hold_reason"}:
        raise ValueError("text_delivery_progress_present")
    coverage = [dict(r) for r in db.execute(
        "SELECT * FROM notification_intent_cards WHERE event_id=? ORDER BY card_id", (event_id,))]
    renders = [dict(r) for r in db.execute(
        "SELECT * FROM notification_renders WHERE intent_event_id=? OR delivery_id IN "
        "(SELECT delivery_id FROM notification_intent_cards WHERE event_id=?) "
        "ORDER BY delivery_id", (event_id, event_id))]
    attempts = [dict(r) for r in db.execute(
        "SELECT a.* FROM notification_delivery_attempts a JOIN notification_renders r "
        "USING(delivery_id) WHERE r.intent_event_id=? ORDER BY attempt_id", (event_id,))]
    unsealed_history = None
    if row["kind"] == "patient_history" and not coverage:
        unsealed_history = _unsealed_history(db, row, renders, attempts, progress)
    elif row["kind"] in ("new_messages", "patient_history"):
        if (row["state"] != "pending" or row["next_try"] is None or renders
                or len(coverage) != 1 or coverage[0]["state"] != "pending"
                or coverage[0]["delivery_id"] is not None
                or coverage[0]["required_render_rev"] != 0 or "hold_reason" in progress):
            raise ValueError("notification_already_issued")
        card = db.execute("SELECT transport,message_id,desired_render_rev FROM notification_cards WHERE card_id=?",
                          (coverage[0]["card_id"],)).fetchone()
        if row["kind"] == "patient_history" and (card is None or card["message_id"] is not None or card["desired_render_rev"] != 0):
            raise ValueError("notification_already_issued")
        if row["kind"] == "patient_history":
            siblings = {r[0] for r in db.execute(
                "SELECT event_id FROM notification_intent_cards WHERE card_id=? "
                "AND state='pending' LIMIT 21", (coverage[0]["card_id"],))}
            if not siblings <= requested_event_ids:
                raise ValueError("history_card_has_other_pending_intents")
        if card is None or card[0] not in TRANSPORTS:
            raise ValueError("unsupported_transport")
    elif row["kind"] == "daily_digest":
        if (row["state"] != "failed" or row["next_try"] is not None or coverage
                or progress.get("hold_reason") != "resend_exhausted" or not renders
                or len({r["transport"] for r in renders}) != 1
                or any(r["card_id"] is not None or r["op"] != "notice"
                       or r["transport"] not in TRANSPORTS or r["state"] != "not_sent" for r in renders)
                or any(a["state"] != "not_sent" for a in attempts)
                or {r["delivery_id"] for r in renders} != {a["delivery_id"] for a in attempts}):
            raise ValueError("notice_delivery_not_proven_unsent")
    else:
        raise ValueError("unsupported_event")
    # Only hashes leave the DB: payloads and destination identifiers remain private.
    state = {"outbox": dict(row), "coverage": coverage, "renders": renders, "attempts": attempts}
    target = {"event_id": event_id, "kind": row["kind"],
              "state_sha256": hashlib.sha256(canonical(state)).hexdigest(),
              "notice_specs": [r["delivery_id"] for r in renders]}
    if unsealed_history is not None:
        target["unsealed_history"] = unsealed_history
    return target


def _unsealed_history(db, row, renders, attempts, progress):
    if (row["state"] != "pending" or row["next_try"] is None or row["attempts"] != 0
            or renders or attempts or "hold_reason" in progress):
        raise ValueError("notification_already_issued")
    payload = json.loads(row["payload"])
    state = initial_import_state.load(db, row["project_id"])
    if (state is None or not isinstance(payload, dict) or payload.get("source") != "initial_history"
            or payload.get("cohort_id") != state["cohort_id"]
            or state.get("notification_cancelled")):
        raise ValueError("initial_history_source_changed")
    siblings = db.execute("SELECT * FROM notify_outbox WHERE kind='patient_history' AND project_id=? "
                          "AND json_valid(payload) AND json_extract(payload,'$.source')='initial_history' "
                          "AND json_extract(payload,'$.cohort_id')=? ORDER BY event_id LIMIT 21",
                          (row["project_id"], state["cohort_id"])).fetchall()
    if len(siblings) > 20:
        raise ValueError("history_cancel_scope_too_large")
    for sibling in siblings:
        eid = sibling["event_id"]
        raw = json.loads(sibling["progress"] or "{}")
        if (sibling["route"] != "interactive" or sibling["state"] != "pending"
                or sibling["next_try"] is None or sibling["attempts"] != 0
                or not isinstance(raw, dict) or raw.keys() - {"wait"}
                or db.execute("SELECT 1 FROM notification_intent_batches WHERE event_id=?", (eid,)).fetchone()
                or db.execute("SELECT 1 FROM notification_intent_cards WHERE event_id=?", (eid,)).fetchone()
                or db.execute("SELECT 1 FROM notification_renders WHERE intent_event_id=?", (eid,)).fetchone()):
            raise ValueError("notification_already_issued")
    if db.execute("SELECT 1 FROM notification_cards WHERE kind='patient_history' AND project_id=?",
                  (row["project_id"],)).fetchone():
        raise ValueError("notification_already_issued")
    return {"project_id": row["project_id"], "cohort_id": state["cohort_id"],
            "event_ids": [r["event_id"] for r in siblings],
            "state_sha256": payload_hash({"cohort": state, "events": [dict(r) for r in siblings]})}


def _cancel_history_tx(db, event_id):
    row = db.execute("SELECT project_id,payload FROM notify_outbox WHERE event_id=? AND kind='patient_history'", (event_id,)).fetchone()
    if row is None:
        return
    payload = json.loads(row["payload"])
    if isinstance(payload, dict) and payload.get("source") == "initial_history":
        initial_import_state.cancel_notifications_tx(db, row["project_id"], payload.get("cohort_id"))


def build_plan(db, event_ids):
    """Return a read-only plan for an explicit, bounded set of eligible events."""
    if (not event_ids or len(event_ids) > 20 or len(set(event_ids)) != len(event_ids)
            or any(type(i) is not int or i <= 0 for i in event_ids)):
        raise ValueError("bad_event_ids")
    requested_event_ids = frozenset(event_ids)
    targets = [_target(db, i, requested_event_ids) for i in sorted(event_ids)]
    expanded = set(event_ids).union(*(e.get("unsealed_history", {}).get("event_ids", []) for e in targets))
    if len(expanded) > 20:
        raise ValueError("history_cancel_scope_too_large")
    return {"version": 1, "events": targets}


def _transports(db, event_ids):
    """Transports an event's card or notices go through; valid after cancellation too."""
    found = set()
    for event_id in event_ids:
        found.update(r[0] for r in db.execute(
            "SELECT c.transport FROM notification_intent_cards i JOIN notification_cards c "
            "USING(card_id) WHERE i.event_id=? UNION SELECT transport FROM notification_renders "
            "WHERE intent_event_id=?", (event_id, event_id)))
    return found


def _hold_adapter_locks(data, locks):
    """The LINE WORKS adapter holds its send scope lock for its whole life and writes no
    worker_status; holding every one until apply returns also keeps it from starting."""
    state = Path(notify_dirs(str(data))["lineworks_state"])
    if state.is_symlink():
        raise ValueError("unsafe_data_path")
    held = sorted(state.glob("send-*.lock"))
    if not held:
        raise ValueError("worker_stop_unverified")
    for path in held:
        if path.is_symlink():
            raise ValueError("unsafe_data_path")
        fd = acquire_run_lock(str(path))
        if fd is None:
            raise ValueError("worker_not_stopped")
        locks.append(fd)


def _workers_stopped(data, transports, locks=None):
    if not transports or not transports <= set(TRANSPORTS):
        raise ValueError("worker_stop_unverified")
    for transport in sorted(transports):
        if transport == "lineworks":
            if locks is not None:  # a recheck runs while the first call's locks are held
                _hold_adapter_locks(data, locks)
            continue
        _witnesses_stopped(data, transport)


def _witnesses_stopped(data, transport):
    witnesses = list((data / "worker_status").glob(f"{transport}-*.json"))
    if not witnesses:
        raise ValueError("worker_stop_unverified")
    for path in witnesses:
        value = json.loads(read_regular_nofollow(str(path), 2048, ValueError))
        pid = value.get("pid") if isinstance(value, dict) else None
        if (type(pid) is not int or pid <= 0 or value.get("version") != VERSION
                or value.get("reason") not in REASONS):
            raise ValueError("worker_stop_unverified")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        raise ValueError("worker_not_stopped")


def _withdraw(data, receipt):
    archive = data / "notification_cancellations" / receipt["command_id"]
    dirs = notify_dirs(str(data))
    spools = [Path(dirs[t + "_render"]) for t in TRANSPORTS]
    if archive.parent.is_symlink() or archive.is_symlink() or any(s.is_symlink() for s in spools):
        raise ValueError("unsafe_spec_path")
    archive.mkdir(mode=0o700, parents=True, exist_ok=True)
    for delivery in receipt["notice_specs"]:
        if not valid_uuid(delivery):
            raise ValueError("invalid_delivery_id")
        # A delivery id is a UUID issued to one transport, so it lives in one spool only.
        for spool in spools:
            source, target = spool / (delivery + ".json"), archive / (delivery + ".json")
            if source.is_symlink() or target.is_symlink():
                raise ValueError("unsafe_spec_path")
            if source.exists():
                if target.exists():
                    raise ValueError("spec_archive_conflict")
                source.rename(target)
                fsync_dir(str(source.parent))
                fsync_dir(str(archive))
    atomic_write(str(archive / "receipt.json"),
                 lambda f: json.dump(receipt, f, ensure_ascii=False, indent=2), mode=0o600)


def apply(root, request):
    """Cancel eligible intents with approval, locks, backup and an idempotent receipt."""
    if (not isinstance(request, dict) or request.get("human_confirmed") is not True
            or not valid_uuid(request.get("command_id"))
            or not valid_text(request.get("actor"), 120) or not valid_text(request.get("reason"), 2000)):
        raise ValueError("human_confirmation_reason_and_identity_required")
    if request.keys() != {"command_id", "actor", "reason", "human_confirmed", "plan"}:
        raise ValueError("unknown_field")
    data = Path(root) / "data"
    archive = data / "notification_cancellations" / request["command_id"]
    if (data.is_symlink() or (data / "ledger.db").is_symlink()
            or archive.parent.is_symlink() or archive.is_symlink()):
        raise ValueError("unsafe_data_path")
    locks = []
    try:
        for name in ("update.lock", "run.lock"):
            fd = acquire_run_lock(str(data / name))
            if fd is None:
                raise ValueError("operation_lock_held")
            locks.append(fd)
        for marker in ("update_in_progress.marker", "restore_pending.json"):
            if os.path.lexists(data / marker):
                raise ValueError("lifecycle_operation_pending")
        with closing(sqlite3.connect((data / "ledger.db").resolve().as_uri() + "?mode=rw",
                                     uri=True, timeout=1)) as db, db:
            db.row_factory = sqlite3.Row
            digest = payload_hash(request)
            prior = db.execute("SELECT payload_hash,receipt_json FROM command_receipts "
                               "WHERE command_id=?", (request["command_id"],)).fetchone()
            if prior:
                if prior["payload_hash"] != digest:
                    raise ValueError("command_id_conflict")
                receipt = json.loads(prior["receipt_json"])
                transports = _transports(db, receipt["event_ids"])
                if transports:
                    _workers_stopped(data, transports, locks)
            else:
                plan = request["plan"]
                try:
                    if not isinstance(plan, dict) or plan.keys() != {"version", "events"}:
                        raise ValueError("bad_plan")
                    ids = [e["event_id"] for e in plan["events"]]
                except (KeyError, TypeError):
                    raise ValueError("bad_plan") from None
                if build_plan(db, ids) != plan:
                    raise ValueError("stale_plan")
                transports = _transports(db, ids)
                # An unsealed cohort has no destination, spec or HTTP-capable worker.
                # The run lock excludes every writer that could seal it meanwhile.
                if transports:
                    _workers_stopped(data, transports, locks)
                if db.execute("SELECT 1 FROM notification_restore_holds "
                              "WHERE released_at IS NULL LIMIT 1").fetchone():
                    raise ValueError("restore_delivery_held")
                archive.mkdir(mode=0o700, parents=True, exist_ok=True)
                # A pre-commit failure leaves its evidence intact; a retry
                # gets a fresh backup without overwriting that earlier copy.
                fd, backup_name = tempfile.mkstemp(prefix="before-", suffix=".db", dir=archive)
                os.close(fd)
                backup = Path(backup_name)
                with closing(sqlite3.connect(str(backup))) as dest:
                    db.backup(dest)
                    if dest.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("backup_verification_failed")
                db.execute("BEGIN IMMEDIATE")
                if build_plan(db, ids) != plan:
                    raise ValueError("stale_plan")
                if transports:
                    _workers_stopped(data, transports)
                cancel_ids = sorted(set(ids).union(*(e.get("unsealed_history", {}).get("event_ids", []) for e in plan["events"])))
                now = time.time()
                for event_id in cancel_ids:
                    _cancel_history_tx(db, event_id)
                    db.execute("UPDATE notification_intent_cards SET state='suppressed' "
                               "WHERE event_id=? AND state='pending'", (event_id,))
                    db.execute("UPDATE notify_outbox SET state='suppressed',next_try=NULL,updated_at=? "
                               "WHERE event_id=?", (now, event_id))
                    db.execute("UPDATE notification_cards SET delivery_state='revoked',revoked_at=?,updated_at=? "
                               "WHERE kind='patient_history' AND message_id IS NULL AND desired_render_rev=0 "
                               "AND card_id IN (SELECT card_id FROM notification_intent_cards WHERE event_id=?) "
                               "AND NOT EXISTS(SELECT 1 FROM notification_intent_cards i WHERE i.card_id=notification_cards.card_id "
                               "AND i.state='pending')", (now, now, event_id))
                receipt = {"kind": "ops.notification_cancel_unissued", "outcome": "applied",
                           "command_id": request["command_id"], "actor": request["actor"],
                           "reason": request["reason"], "processed_at": now,
                           "event_ids": cancel_ids, "backup": str(backup), "plan": plan,
                           "notice_specs": [s for e in plan["events"] for s in e["notice_specs"]],
                           "remote_operations": 0}
                db.execute("INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
                           (request["command_id"], digest, None, None, "applied",
                            canonical(receipt).decode(), now))
                mark_snapshot_dirty(db)
                db.commit()
            # Failed cleanup is retried with the same command_id; a committed
            # cancellation never turns back into a retryable notification.
            _withdraw(data, receipt)
            return receipt
    finally:
        for fd in reversed(locks):
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="導入先のMCSフォルダ")
    sub = parser.add_subparsers(dest="action", required=True)
    plan = sub.add_parser("plan", help="対象と状態の照合計画を表示（変更なし）")
    plan.add_argument("--event-id", type=int, action="append", required=True)
    cancel = sub.add_parser("apply", help="計画に一致する通知を送信せず取消")
    cancel.add_argument("--plan", type=Path, required=True)
    cancel.add_argument("--command-id", required=True)
    cancel.add_argument("--actor", required=True)
    cancel.add_argument("--reason", required=True)
    cancel.add_argument("--confirm-human", action="store_true")
    args = parser.parse_args(argv)
    from mcs_runtime import sqlite_wal_safe
    try:
        if not sqlite_wal_safe(sqlite3.sqlite_version_info):
            raise ValueError("sqlite_runtime_unsafe")
        if args.action == "plan":
            with closing(sqlite3.connect((args.root / "data/ledger.db").resolve().as_uri() + "?mode=ro",
                                         uri=True, timeout=1)) as db:
                db.row_factory = sqlite3.Row
                result = build_plan(db, args.event_id)
        else:
            result = apply(args.root, {"command_id": args.command_id, "actor": args.actor,
                                      "reason": args.reason, "human_confirmed": args.confirm_human,
                                      "plan": json.loads(read_regular_nofollow(
                                          str(args.plan), 65536, ValueError))})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"outcome": "rejected", "error": str(error)
                          if isinstance(error, ValueError) else "operation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
