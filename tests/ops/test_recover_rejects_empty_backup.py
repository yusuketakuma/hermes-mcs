"""A zero-byte (truncated) rollback backup must fail closed in
the watchdog exactly like mcs_update (backup_invalid), never become a consent
prompt for replacing the live ledger with an empty database. Synthetic only."""
import sqlite3

from ops_testkit import _load


def _mk_db(path, version):
    con = sqlite3.connect(str(path))
    con.execute(f"PRAGMA user_version={version}")
    con.execute("CREATE TABLE messages(message_id INTEGER PRIMARY KEY, posted_at_ts REAL)")
    con.execute("INSERT INTO messages VALUES(1, 1.0)")
    con.commit()
    con.close()


def test_zero_byte_backup_is_unreadable_not_consent(tmp_path, monkeypatch):
    rec = _load()
    data = tmp_path / "data"
    data.mkdir()
    live, back = data / "ledger.db", data / "preupdate-empty.db"
    _mk_db(live, 8)
    back.write_bytes(b"")                       # truncated backup
    for name, value in (("DATA", data), ("LEDGER", live),
                        ("STATE_PATH", data / "update_state.json")):
        monkeypatch.setattr(rec.mcs_recovery_core, name, str(value))
    before = live.read_bytes()
    err = rec.mcs_recovery_database._restore_db(str(back))
    assert live.read_bytes() == before
    # mcs_update._restore_db raises backup_invalid for the same file
    # (ledger.valid_mcs_db rejects size 0); the watchdog must not ask a
    # human to approve restoring an empty schema-0 database instead.
    assert err is not None and err.startswith("backup_unreadable"), err


def test_zero_byte_backup_never_replaces_live_ledger(tmp_path, monkeypatch):
    import json
    import time
    rec = _load()
    data = tmp_path / "data"
    data.mkdir()
    live, back = data / "ledger.db", data / "preupdate-empty.db"
    _mk_db(live, 8)
    back.write_bytes(b"")
    for name, value in (("DATA", data), ("LEDGER", live),
                        ("STATE_PATH", data / "update_state.json")):
        monkeypatch.setattr(rec.mcs_recovery_core, name, str(value))
    state = rec.mcs_recovery_core._load_state()
    state["applying"] = {"tag": "rollback:v9.9.9", "sha": "b" * 40,
                         "prev_sha": "a" * 40, "rollback": True,
                         "backup_path": str(back), "at": time.time()}
    rec.mcs_recovery_core._save_state(state)
    report = rec.mcs_recovery_database._loss_report(str(back))
    con = sqlite3.connect(str(live))
    con.execute("CREATE TABLE command_receipts(command_id TEXT PRIMARY KEY,"
                " payload_hash TEXT, project_id INTEGER, request_id INTEGER,"
                " outcome TEXT, receipt_json TEXT, processed_at REAL)")
    con.execute("INSERT INTO command_receipts VALUES(?,?,NULL,NULL,'applied',?,?)",
                ("cid-synthetic", "h" * 64, json.dumps({
                    "cmd": "ops.restore_approve", "scheduled": True,
                    "command_id": "cid-synthetic", "report_id": report["report_id"],
                    "backup_sha256": report["backup_sha256"],
                    "backup_schema": report["backup_schema"]}), time.time()))
    con.commit()
    con.close()
    rec.mcs_recovery_database._restore_db(str(back))
    # an invalid (empty) backup must never become the live ledger
    assert live.stat().st_size > 0
