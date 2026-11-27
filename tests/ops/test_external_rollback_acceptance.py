"""Isolated regression probes for the November 27 rollback review."""
import hashlib
import os
import sqlite3
from pathlib import Path

import pytest
import update_execution
from mcs_update_testkit import updater as updater
from ops_testkit import _git, patch_lifecycle_lookup
from test_update_postcheck_ledger import _quiet_postcheck
from test_update_recovery_stability import _synthetic_flagless_rollback


def test_foreign_messages_is_untouched(tmp_path, monkeypatch):
    _quiet_postcheck(monkeypatch)
    db = tmp_path / "ledger.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, text TEXT)")
        con.execute("INSERT INTO messages VALUES(1, 'synthetic')")
    before = hashlib.sha256(db.read_bytes()).digest()
    monkeypatch.setattr(update_execution, "LEDGER", str(db))
    errors = update_execution._postcheck({}, "synthetic-sha")
    with sqlite3.connect(db) as con:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    assert errors and tables == ["messages"] and hashlib.sha256(db.read_bytes()).digest() == before, (errors, tables)


@pytest.mark.parametrize("engine", ["updater", "watchdog"])
@pytest.mark.parametrize("phase", ["forward_reverted", "forward_pre_merge", "rollback_not_switched", "rollback_busy", "rollback_missing_attempts"])
def test_rollback_render_before_restart(updater, tmp_path, monkeypatch, engine, phase):
    tool, recover, repo, db, _backup, old, _restarts = _synthetic_flagless_rollback(updater, tmp_path, monkeypatch, engine)
    state = updater.journal_store.load_state(updater.STATE_PATH)
    new = state["applying"]["prev_sha"]
    entry = state["applying"]
    entry.pop("backup_path")
    entry["schema_bump"] = False
    if phase in ("forward_reverted", "forward_pre_merge"):
        entry.update(sha=new, prev_sha=old, tag="v1.1.0")
        entry.pop("rollback")
        state["stages"] = [{"stage": s, "at": 1} for s in ("quiesce", "merge", "post_merge")]
        if phase == "forward_pre_merge":
            state["stages"] = [{"stage": "quiesce", "at": 1}]
    elif phase == "rollback_not_switched":
        _git(repo, "reset", "-q", "--hard", new)
    updater.journal_store.save_state(updater.STATE_PATH, state)
    path = Path(db).resolve()
    assert path.is_relative_to(tmp_path.resolve())

    with sqlite3.connect(path) as con:
        con.executescript("""
        DROP TABLE IF EXISTS notification_delivery_attempts;
        DROP TABLE IF EXISTS notification_intent_cards;
        DROP TABLE IF EXISTS notification_renders;
        DROP TABLE IF EXISTS notify_outbox;
        CREATE TABLE notify_outbox(event_id INTEGER PRIMARY KEY,state TEXT,next_try REAL,updated_at REAL);
        CREATE TABLE notification_renders(delivery_id TEXT PRIMARY KEY,card_id INTEGER,op TEXT,
            intent_event_id INTEGER,state TEXT,updated_at REAL);
        CREATE TABLE notification_delivery_attempts(attempt_id TEXT PRIMARY KEY,delivery_id TEXT,state TEXT);
        CREATE TABLE notification_intent_cards(event_id INTEGER,card_id INTEGER,state TEXT,delivery_id TEXT,
            required_render_rev INTEGER);
        INSERT INTO notify_outbox VALUES(1,'pending',900,0),(2,'pending',900,0);
        INSERT INTO notification_renders VALUES('fresh',NULL,'notice',1,'queued',0),('attempted',NULL,'notice',2,'queued',0);
        INSERT INTO notification_delivery_attempts VALUES('once','attempted','unknown');
        """)
    connect = sqlite3.connect
    def states():
        with connect(path) as con:
            return dict(con.execute("SELECT delivery_id,state FROM notification_renders"))
    def fast_connect(database, *args, **kwargs):
        if not args and (os.fspath(database) == str(path) or os.fspath(database).split("?",1)[0] == path.as_uri()):
            kwargs["timeout"] = 0.02
        return connect(database,*args,**kwargs)
    monkeypatch.setattr(sqlite3,"connect",fast_connect)
    starts = []
    def restart(*args, **kwargs):
        starts.append(states())
        return []
    if engine == "updater":
        patch_lifecycle_lookup(monkeypatch, tool, "restart_agents", restart)
        patch_lifecycle_lookup(monkeypatch, tool, "restart_services", lambda *a, **kw: None)
        patch_lifecycle_lookup(monkeypatch, tool, "_restore_runtime", lambda entry: None)
    else:
        patch_lifecycle_lookup(monkeypatch, tool, "_runtime_mode", lambda: "hermes")
        patch_lifecycle_lookup(monkeypatch, tool, "_reconcile_membership", lambda snapshot: [])
        patch_lifecycle_lookup(monkeypatch, tool, "_restart_drainers", restart)
        patch_lifecycle_lookup(monkeypatch, tool, "_restart_services", lambda *a, **kw: 0)

    queued = {"fresh": "queued", "attempted": "queued"}
    cancelled = {"fresh": "cancelled", "attempted": "queued"}
    if phase == "rollback_missing_attempts":
        with connect(path) as con:
            con.execute("DROP TABLE notification_delivery_attempts")
        assert recover() != 0
        assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]
        assert Path(updater.MARKER_PATH).is_file()
        assert not starts and states() == queued
        return
    if phase == "rollback_busy":
        locker = connect(path)
        try:
            locker.execute("BEGIN IMMEDIATE")
            rc = recover()
            live = updater.journal_store.load_state(updater.STATE_PATH)
            assert rc != 0 and live["applying"] is not None and not starts and states() == queued, (rc, starts, states(), live["applying"])
        finally:
            locker.close()
    rc = recover()
    finished = updater.journal_store.load_state(updater.STATE_PATH)
    expected = queued if phase in ("rollback_not_switched", "forward_pre_merge") else cancelled
    assert rc == 0 and finished["applying"] is None and starts and all(s == expected for s in starts) and states() == expected, (rc, starts, states(), finished["applying"])


@pytest.mark.parametrize("engine", ["updater", "watchdog"])
@pytest.mark.parametrize("shape", [1, 2, 3, 4, 6])
def test_recovery_preserves_historical_render_contract(tmp_path, monkeypatch, engine, shape):
    import update_restore
    from ops_testkit import _load

    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "schema_upgrade"
    ledger = tmp_path / "historical.db"
    if engine == "updater":
        module = update_restore
        owner = module
        error = module.UpdateError
    else:
        module = _load()
        owner = module.mcs_recovery_core
        error = ValueError
    monkeypatch.setattr(owner, "LEDGER", str(ledger))
    with sqlite3.connect(ledger) as con:
        con.executescript((fixtures / f"shape-{shape}.sql").read_text())
        if shape in (1, 6):
            assert module._cancel_unsent_notices() == 0
            return
        con.executescript((fixtures / "notification-seed.sql").read_text())
        con.execute("UPDATE notification_renders SET state='queued'")
    # No intent_event_id exists in schema 7. An uncertain attempt still protects its render.
    assert module._cancel_unsent_notices() == 0
    with sqlite3.connect(ledger) as con:
        con.execute("DELETE FROM notification_delivery_attempts")
        con.execute("INSERT INTO notification_renders(delivery_id,op,render_rev,route_epoch,"
                    "payload_hash,correlation,created_at,updated_at) VALUES('unbound','notice',"
                    "2,1,'hash','unbound',120,130)")
    with pytest.raises(error, match="notice_cancel_failed"):
        module._cancel_unsent_notices()
    with sqlite3.connect(ledger) as con:
        assert con.execute("SELECT count(*) FROM notification_renders WHERE state='queued'").fetchone() == (2,)
        con.execute("DELETE FROM notification_renders WHERE delivery_id='unbound'")
    assert module._cancel_unsent_notices() == 1
    with sqlite3.connect(ledger) as con:
        assert con.execute("SELECT state FROM notification_renders").fetchone() == ("cancelled",)
        assert con.execute("SELECT delivery_id,required_render_rev FROM notification_intent_cards").fetchone() == (None, 0)
