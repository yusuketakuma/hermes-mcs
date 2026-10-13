"""Ledger upkeep for long-running installs: FTS trigger scope, artifact
kind/time index, WAL size limit, change-gated snapshot publish, backup
deadline, log discovery, age-based retention, daily maintenance and
bounded worker reaping. Temp ledgers and synthetic rows only."""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, suppress

import pytest

import bounded_http
import ledger
import maintenance
from extract_testkit import _ledger, _message

DAY = 86400


# ---------- M4: FTS update trigger fires on indexed columns only ----------

def _fts_rows(db, mid):
    return db.db.execute(
        "SELECT body_text FROM messages_fts WHERE rowid=?", (mid,)).fetchall()


def test_fts_trigger_ignores_bookkeeping_updates(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message(1, body="alpha")])
    with db.db:   # drop the index row: only a real indexed change restores it
        db.db.execute("DELETE FROM messages_fts WHERE rowid=1")
    with db.db:
        db.db.execute("UPDATE messages SET updated_seen=updated_seen+1, "
                      "reply_count=3 WHERE message_id=1")
    assert _fts_rows(db, 1) == []
    with db.db:
        db.db.execute("UPDATE messages SET body_text='beta' WHERE message_id=1")
    assert [r[0] for r in _fts_rows(db, 1)] == ["beta"]
    with db.db:
        db.db.execute("UPDATE messages SET sender_name='other' WHERE message_id=1")
    assert db.db.execute("SELECT sender_name FROM messages_fts WHERE rowid=1"
                         ).fetchone()[0] == "other"
    db.close()


def test_old_unconditional_trigger_is_replaced_on_open(tmp_path):
    path = tmp_path / "ledger.db"
    ledger.Ledger(str(path)).close()
    with closing(sqlite3.connect(path)) as raw, raw:
        raw.execute("DROP TRIGGER messages_au")
        raw.execute("""CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
            UPDATE messages_fts SET body_text=new.body_text,
              sender_name=new.sender_name WHERE rowid=old.message_id; END""")
    with closing(ledger.Ledger(str(path))) as db:
        sql = db.db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                            "AND name='messages_au'").fetchone()[0]
        assert ledger._FTS_AU_MARK in sql and "WHEN" in sql
        # still exactly one trigger of that name, and the version is unchanged
        assert db.db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION


def test_artifact_kind_time_index_and_wal_limit(tmp_path):
    db = _ledger(tmp_path)
    plan = " ".join(r[3] for r in db.db.execute(
        "EXPLAIN QUERY PLAN SELECT meta FROM artifacts "
        "WHERE kind='semantic_usage' AND created_at>=?", (0,)))
    assert "idx_artifacts_kind_created" in plan
    assert db.db.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                         "AND name='idx_messages_posted_ts'").fetchone()
    assert db.db.execute("PRAGMA journal_size_limit").fetchone()[0] \
        == ledger.JOURNAL_SIZE_LIMIT
    db.close()


def test_relation_recount_is_skipped_while_fresh(tmp_path):
    path = tmp_path / "ledger.db"
    with closing(ledger.Ledger(str(path))) as db, db.db:
        for suffix in ("ins", "upd"):
            db.db.execute(f"DROP TRIGGER g1_attachments_msg_{suffix}")
        db.db.execute("INSERT INTO attachments(message_id) VALUES(999)")
    ledger.Ledger(str(path)).close()    # audit: shadow, marker recorded
    with closing(ledger.Ledger(str(path))) as db:
        statements = []
        db.db.set_trace_callback(statements.append)
        assert db._relation_guards_current() is True
        assert not any("COUNT(*) FROM attachments" in s for s in statements)



def test_relation_marker_moves_on_a_backfilled_lower_id_parent(tmp_path):
    with closing(ledger.Ledger(str(tmp_path / "ledger.db"))) as db, db.db:
        db.db.execute("INSERT INTO messages(message_id) VALUES(500)")
        before = db._relation_marker()
        # an older parent backfilled below MAX(message_id) can resolve an orphan
        db.db.execute("INSERT INTO messages(message_id) VALUES(100)")
        assert db._relation_marker() != before

# ---------- H4: snapshot only when changed / stale ----------

@pytest.fixture
def snapdir(tmp_path, monkeypatch):
    out = tmp_path / "snapshots"
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(out))
    return out


def test_snapshot_publishes_only_on_change_dirty_or_age(tmp_path, snapdir):
    db = _ledger(tmp_path)
    path = str(tmp_path / "ledger.db")
    db.save_messages([_message(1)])
    assert maintenance.refresh_snapshot(path) == "published"
    first = (snapdir / "ledger-snapshot.db").stat().st_ino
    assert maintenance.refresh_snapshot(path) == "unchanged"
    # tick bookkeeping alone is no content change
    db.finish_run(db.begin_run(None), "ok")
    assert maintenance.refresh_snapshot(path) == "unchanged"
    assert (snapdir / "ledger-snapshot.db").stat().st_ino == first
    db.save_messages([_message(2)])
    assert maintenance.refresh_snapshot(path) == "published"
    assert maintenance.refresh_snapshot(path, force=True) == "published"
    later = time.time() + maintenance.SNAPSHOT_MAX_AGE_S + 1
    assert maintenance.refresh_snapshot(path, now=later) == "published"
    (snapdir / "ledger-snapshot.db").unlink()
    assert maintenance.refresh_snapshot(path, now=later) == "published"
    db.close()


def test_snapshot_failed_publish_is_reported(tmp_path, snapdir, monkeypatch):
    _ledger(tmp_path).close()
    monkeypatch.setattr(maintenance, "publish_snapshot", lambda *_a, **_k: False)
    assert maintenance.refresh_snapshot(str(tmp_path / "ledger.db")) is None


def test_snapshot_stops_at_the_callers_hard_deadline(tmp_path, snapdir, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(1)])
    db.close()
    path = str(tmp_path / "ledger.db")
    assert maintenance.refresh_snapshot(path, deadline=time.monotonic() - 1) is None
    seen = {}

    def verify(_tmp, *, deadline):
        seen["deadline"] = deadline
        return False
    monkeypatch.setattr(ledger, "_valid_snapshot_candidate", verify)
    hard = time.monotonic() + 5
    assert maintenance.refresh_snapshot(path, deadline=hard) is None
    assert seen["deadline"] == hard          # never past the watchdog
    assert not (snapdir / "ledger-snapshot.db").exists()


def test_snapshot_copy_completes_under_concurrent_writes(tmp_path, monkeypatch):
    path = str(tmp_path / "busy.db")
    with closing(sqlite3.connect(path)) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t(x BLOB)")
        with con:   # several backup steps (4096 pages each)
            con.executemany("INSERT INTO t VALUES(randomblob(4000))",
                            [()] * 6000)
        con.execute("CREATE TABLE snapshot_meta(singleton INTEGER PRIMARY "
                    "KEY, generation_id TEXT, generated_at REAL)")
    writer = sqlite3.connect(path, isolation_level=None)
    real = ledger.monotonic

    def clock():   # another connection commits between every backup step
        writer.execute("INSERT INTO t VALUES(1)")
        return real()
    monkeypatch.setattr(ledger, "monotonic", clock)
    monkeypatch.setattr(ledger, "_valid_snapshot_candidate",
                        lambda _p, *, deadline: True)
    try:
        out = ledger.publish_snapshot(path, str(tmp_path / "snap"),
                                      deadline=real() + 10)
    finally:
        writer.close()
    assert out is not None
    with closing(sqlite3.connect(out)) as snap:
        assert snap.execute("SELECT COUNT(*) FROM t").fetchone()[0] >= 6000


def test_snapshot_verify_budget_grows_with_size(tmp_path, monkeypatch):
    small = tmp_path / "small.db"
    small.write_bytes(b"x")
    assert ledger.snapshot_verify_timeout_s(str(small)) == pytest.approx(
        ledger.SNAPSHOT_VERIFY_TIMEOUT_S, abs=0.01)
    monkeypatch.setattr(ledger.os.path, "getsize", lambda _p: 4 * 1024 ** 3)
    big = ledger.snapshot_verify_timeout_s(str(small))
    assert ledger.SNAPSHOT_VERIFY_TIMEOUT_S < big <= (
        ledger.SNAPSHOT_VERIFY_TIMEOUT_S + ledger.SNAPSHOT_VERIFY_EXTRA_MAX_S)


# ---------- M7: backup respects the hard deadline ----------

def test_backup_copy_stops_at_the_deadline(tmp_path, monkeypatch):
    """An underestimated copy is abandoned at the deadline mid-copy — it
    never runs on to verify past the watchdog — and leaves no backup."""
    db = _ledger(tmp_path)
    db.save_messages([_message(i, body="x" * 2000) for i in range(1, 200)])
    db.close()
    backups = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(maintenance, "BACKUP_STEP_PAGES", 1)
    monkeypatch.setattr(maintenance, "backup_time_needed_s", lambda _p: 0)
    clock = iter([0.0])   # the pre-copy budget check sees time left
    monkeypatch.setattr(maintenance, "monotonic", lambda: next(clock, 100.0))
    verified, valid = [], maintenance._valid
    monkeypatch.setattr(maintenance, "_valid",
                        lambda p, d: verified.append(p) or valid(p, None))
    with pytest.raises(maintenance.MaintenanceError,
                       match="backup_deadline_exceeded"):
        maintenance.daily_backup(str(tmp_path / "ledger.db"), deadline=50.0)
    assert not any(p.endswith(".db.tmp") for p in verified)
    assert not list(backups.glob("ledger-*"))


def test_broken_backup_check_leaves_time_to_replace_it(tmp_path, monkeypatch):
    """Checking an unmarked same-day backup is bounded so the replacement
    still fits; a broken file is then replaced, not kept forever."""
    _ledger(tmp_path).close()
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(maintenance, "backup_time_needed_s", lambda _p: 10.0)
    dest = backups / f"ledger-{time.strftime('%Y%m%d')}.db"
    dest.write_bytes(b"not a database")
    checked, valid = {}, maintenance._valid

    def record(path, deadline):
        checked.setdefault(path, deadline)
        return valid(path, deadline)
    monkeypatch.setattr(maintenance, "_valid", record)
    deadline = time.monotonic() + 600
    assert maintenance.daily_backup(str(tmp_path / "ledger.db"),
                                    deadline=deadline) is None
    assert checked[str(dest)] == pytest.approx(
        deadline - 10.0 - maintenance.BACKUP_CHECK_SLACK_S)
    assert maintenance.valid_mcs_db(str(dest))


def test_overrunning_check_still_leaves_time_to_replace(tmp_path, monkeypatch):
    """A same-day check that runs a little past its cut-off (the progress
    handler fires between VM steps) still leaves the replacement budget."""
    _ledger(tmp_path).close()
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(maintenance, "backup_time_needed_s", lambda _p: 10.0)
    dest = backups / f"ledger-{time.strftime('%Y%m%d')}.db"
    dest.write_bytes(b"not a database")
    clock = [0.0]
    monkeypatch.setattr(maintenance, "monotonic", lambda: clock[0])
    valid = maintenance._valid

    def check(path, deadline):
        if path == str(dest):
            clock[0] = deadline + 1.0     # overran its cut-off by 1s
            return False
        return valid(path, None)
    monkeypatch.setattr(maintenance, "_valid", check)
    assert maintenance.daily_backup(str(tmp_path / "ledger.db"),
                                    deadline=100.0) is None
    assert maintenance.valid_mcs_db(str(dest))


def test_backup_skips_when_time_left_cannot_cover_the_ledger(tmp_path, monkeypatch):
    _ledger(tmp_path).close()
    backups = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    path = str(tmp_path / "ledger.db")
    assert maintenance.daily_backup(path, deadline=time.monotonic() + 1) \
        == "skipped_deadline"
    assert not list(backups.glob("ledger-*.db"))
    assert maintenance.daily_backup(path, deadline=time.monotonic() + 600) is None
    assert len(list(backups.glob("ledger-*.db"))) == 1



def test_validated_backup_without_marker_rearms_the_fast_path(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    path = str(tmp_path / "ledger.db")
    _ledger(tmp_path).close()
    assert maintenance.daily_backup(path) is None
    (made,) = backups.glob("ledger-*.db")
    marker = made.with_name(made.name + ".ok")
    marker.unlink()                 # a same-day backup from before the marker
    checks = []
    real = maintenance.valid_mcs_db
    monkeypatch.setattr(maintenance, "valid_mcs_db",
                        lambda *a, **k: checks.append(1) or real(*a, **k))
    assert maintenance.daily_backup(path) is None
    assert marker.exists() and len(checks) == 1
    assert maintenance.daily_backup(path) is None
    assert len(checks) == 1         # the next tick skips the full check

# ---------- M1: child and LINE WORKS logs rotate ----------

def test_rotate_log_discovers_child_logs_and_skips_symlinks(tmp_path, monkeypatch):
    home = tmp_path / "mcs"
    data = home / "data"
    (data / "lineworks_state").mkdir(parents=True)
    monkeypatch.setattr(maintenance, "HOME", str(home))
    monkeypatch.setattr(maintenance, "LOGFILE", str(data / "run.log"))
    monkeypatch.setattr(maintenance, "LOG_MAX", 3)
    child = data / "extract-0.log"
    service = data / "lineworks_state" / "service.stderr.log"
    outside = tmp_path / "outside.log"
    for f in (child, service, outside):
        f.write_text("synthetic log line\n")
    (data / "linked.log").symlink_to(outside)
    maintenance.rotate_log()
    assert child.read_text() == "" and (data / "extract-0.log.1").exists()
    assert service.read_text() == ""
    assert outside.read_text() == "synthetic log line\n"   # never followed
    assert not (data / "linked.log.1").exists()


# ---------- retention ----------

def _seed_history(db, now):
    old, new = now - 100 * DAY, now - DAY
    rows = [(old, old, "ok"), (old, old, "ok"), (old, old, "failed"),
            (new, new, "ok")]
    for started, finished, status in rows:
        db.db.execute("INSERT INTO runs(started_at,finished_at,status) VALUES(?,?,?)",
                      (started, finished, status))
    db.db.execute("INSERT INTO read_marks(project_id,snapshot_ts,marked_at,status) "
                  "VALUES(1,1,?,'confirmed'),(1,2,?,'confirmed')", (old, new))
    for kind, state, at in (("session_expired", "accepted", old),
                            ("run_failed", "accepted", old),
                            ("run_failed", "pending", old),
                            ("session_expired", "accepted", new),
                            ("new_messages", "accepted", old),
                            ("semantic_notice", "accepted", old)):
        db.db.execute("INSERT INTO notify_outbox(kind,payload,state,created_at,"
                      "updated_at) VALUES(?,'{}',?,?,?)", (kind, state, at, at))
    for kind, age in (("semantic_drain_run", 40 * DAY), ("semantic_drain_run", DAY),
                      ("semantic_usage", 8 * DAY), ("semantic_usage", 60),
                      ("semantic_bundle", 400 * DAY)):
        db.db.execute("INSERT INTO artifacts(kind,content,created_at) "
                      "VALUES(?,'{}',?)", (kind, now - age))
    db.db.commit()


def test_prune_history_keeps_references_and_audit_rows(tmp_path):
    db = _ledger(tmp_path)
    now = time.time()
    db.save_messages([_message(1, body="a")])
    db.save_messages([_message(1, body="edited")])   # message_revisions rows
    _seed_history(db, now)
    referenced = db.db.execute(
        "SELECT event_id FROM notify_outbox WHERE kind='run_failed' "
        "AND state='accepted'").fetchone()[0]
    db.db.execute("INSERT INTO notification_task_reminders(request_id,stage,"
                  "due_date,event_id,created_at) VALUES(1,'due','d',?,0)",
                  (referenced,))
    db.db.commit()
    revisions = db.db.execute("SELECT COUNT(*) FROM message_revisions").fetchone()[0]
    with closing(sqlite3.connect(tmp_path / "ledger.db")) as con:
        out = maintenance.prune_history(con, now)
    assert out == {"runs": 2, "read_marks": 1, "alerts": 1,
                   "drain_runs": 1, "usage_receipts": 1}
    assert sorted(r[0] for r in db.db.execute("SELECT status FROM runs")) \
        == ["failed", "ok"]           # newest per status survives
    left = {(r[0], r[1]) for r in db.db.execute(
        "SELECT kind,state FROM notify_outbox")}
    assert ("run_failed", "accepted") in left      # still referenced
    assert ("run_failed", "pending") in left       # unfinished
    assert ("new_messages", "accepted") in left    # dedupe evidence
    assert ("semantic_notice", "accepted") in left
    assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='semantic_bundle'"
                         ).fetchone()[0] == 1
    assert db.db.execute("SELECT COUNT(*) FROM message_revisions"
                         ).fetchone()[0] == revisions > 0
    db.close()


def test_held_sends_pinning_old_attachments_are_counted(tmp_path):
    db = _ledger(tmp_path)
    now = time.time()
    db.db.execute("INSERT INTO messages(message_id,project_id) VALUES(7,1)")
    db.db.execute("INSERT INTO attachments(message_id,file_id,local_path,bytes,state,"
                  "downloaded_at) VALUES(7,'f','/x',10,'downloaded',?)", (now - 60 * DAY,))
    db.db.execute("INSERT INTO notify_outbox(kind,payload,state,next_try,created_at) "
                  "VALUES('new_messages',?,'failed',NULL,?)",
                  (json.dumps({"message_ids": [7]}), now - 40 * DAY))
    db.db.commit()
    assert maintenance.held_pinned_attachments(db.db, now) == {"count": 1, "bytes": 10}
    assert maintenance.held_pinned_attachments(db.db, now - 20 * DAY)["count"] == 0
    db.close()


# ---------- M3: daily upkeep ----------

def test_db_maintenance_runs_daily_and_weekly_once(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(1)])
    _seed_history(db, time.time())
    db.close()
    monkeypatch.setattr(maintenance, "DB_MAINT_STATE", str(tmp_path / "maint.json"))
    path = str(tmp_path / "ledger.db")
    out = maintenance.db_maintenance(path)
    assert out["pruned"]["runs"] == 2 and out["fts"] == "optimized"
    assert out["checkpoint"]["busy"] == 0
    assert maintenance.db_maintenance(path) is None          # done for today
    state = json.loads((tmp_path / "maint.json").read_text())
    assert maintenance.db_maintenance(path, now=state["daily_at"] + DAY + 1) \
        .get("fts") is None                                  # weekly only
    assert oct((tmp_path / "maint.json").stat().st_mode & 0o777) == "0o600"


def test_a_checkpoint_held_off_by_a_reader_is_retried_next_tick(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(1)])
    db.close()
    monkeypatch.setattr(maintenance, "DB_MAINT_STATE", str(tmp_path / "maint.json"))
    path = str(tmp_path / "ledger.db")
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM messages").fetchone()
    writer = sqlite3.connect(path)
    with writer:
        writer.execute("UPDATE messages SET sender_name='synthetic' WHERE message_id=1")
    writer.close()
    out = maintenance.db_maintenance(path)
    assert out["checkpoint"]["busy"] != 0
    assert json.loads((tmp_path / "maint.json").read_text())["checkpoint_pending"] is True
    reader.rollback()
    reader.close()
    out = maintenance.db_maintenance(path)              # same day: checkpoint only
    assert out == {"checkpoint": {"busy": 0, "log": 0, "done": 0}}
    assert "checkpoint_pending" not in json.loads((tmp_path / "maint.json").read_text())
    assert maintenance.db_maintenance(path) is None


def test_a_checkpoint_that_raises_is_still_retried(tmp_path, monkeypatch):
    _ledger(tmp_path).close()
    monkeypatch.setattr(maintenance, "DB_MAINT_STATE", str(tmp_path / "maint.json"))
    real = sqlite3.connect

    class Failing:
        def __init__(self, con):
            self._con = con

        def __getattr__(self, name):
            return getattr(self._con, name)

        def __enter__(self):
            return self._con.__enter__()

        def __exit__(self, *exc):
            return self._con.__exit__(*exc)

        def execute(self, sql, *args):
            if "wal_checkpoint" in sql:
                raise sqlite3.OperationalError("synthetic I/O error")
            return self._con.execute(sql, *args)
    monkeypatch.setattr(maintenance.sqlite3, "connect",
                        lambda *a, **k: Failing(real(*a, **k)))
    path = str(tmp_path / "ledger.db")
    with pytest.raises(sqlite3.OperationalError):
        maintenance.db_maintenance(path)
    state = json.loads((tmp_path / "maint.json").read_text())
    assert state["checkpoint_pending"] is True and "daily_at" in state
    monkeypatch.setattr(maintenance.sqlite3, "connect", real)
    assert maintenance.db_maintenance(path)["checkpoint"]["busy"] == 0


def test_db_maintenance_waits_for_time(tmp_path, monkeypatch):
    _ledger(tmp_path).close()
    monkeypatch.setattr(maintenance, "DB_MAINT_STATE", str(tmp_path / "maint.json"))
    assert maintenance.db_maintenance(
        str(tmp_path / "ledger.db"), deadline=time.monotonic() + 1) \
        == {"skipped": "deadline"}
    assert not (tmp_path / "maint.json").exists()


# ---------- low: a killed worker is reaped within a bound ----------

def test_reap_returns_when_a_descendant_keeps_the_pipe(tmp_path, monkeypatch):
    monkeypatch.setattr(bounded_http, "REAP_TIMEOUT_S", 0.5)
    ready = tmp_path / "descendant-ready"
    descendant = ("from pathlib import Path; import sys, time; "
                  "Path(sys.argv[1]).write_text('ready'); time.sleep(30)")
    code = ("import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {descendant!r}, {str(ready)!r}]); "
            "time.sleep(30)")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        # Prove that the pipe is inherited before exercising the reap bound.
        ready_deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < ready_deadline:
            assert proc.poll() is None, "synthetic parent exited before readiness"
            time.sleep(0.01)
        assert ready.is_file(), "synthetic descendant must have started"
        proc.kill()
        started = time.monotonic()
        bounded_http._reap(proc)
        assert time.monotonic() - started < 5
    finally:
        with suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
