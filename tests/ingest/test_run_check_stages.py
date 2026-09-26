"""run_check stage contracts — stage_unread / stage_backfill /
stage_attachments / stage_derive / stage_self_probe, tick E2E, run
lock, health, session-expired alerting."""

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

import extract_llm
import job_ops
import ledger
import mcs_adapter
import notify_flush
import run_check
from ingest_testkit import _ledger, _message, _unread_patient, _att, _msg_at, _iso


def test_attachment_destination_uses_ledger_identity(tmp_path, monkeypatch):
    seen = []
    adapter = SimpleNamespace(
        download=lambda url, dest: seen.append(dest) or {"bytes": 1, "sha256": "x"}
    )
    db = SimpleNamespace(
        attachments_due=lambda limit, priority_mids=None: [
            {"attachment_id": 1, "file_id": "same", "url": "u1"},
            {"attachment_id": 2, "file_id": "same", "url": "u2"},
        ],
        attachment_saved=lambda *a, **kw: None,
        attachment_failed=lambda *a, **kw: None,
        pending_notify_message_ids=lambda: [],
    )
    monkeypatch.setattr(run_check, "ATTACH_DIR", str(tmp_path))
    run_check.stage_attachments(adapter, db, {"errors": []},
                                time.monotonic() + 60)
    assert len(set(seen)) == 2


def test_backfill_does_not_advance_past_missing_reply():
    parent = _message(mid=10)
    parent.reply_count = 1
    covered = []
    reserved = []

    class Adapter:
        def fetch_history(self, *args, **kwargs):
            return mcs_adapter.MessageBatch([parent], pages=1, reached=True)

        def fetch_thread(self, *args):
            return []

    class Store:
        def frontier_patients(self): return [{"project_id": 1}]
        def high_watermark(self, pid): return 100
        def coverage_ts(self, pid): return 0
        def coverage_lag(self, pid): return 100
        def save_messages(self, *args, **kwargs): return []
        def pending_reply_jobs(self, pid): return 0
        def set_coverage(self, pid, ts): covered.append((pid, ts))
        def job_add(self, kind, pid, **kwargs):
            reserved.append((kind, pid, kwargs["payload"]))

    result = {"errors": [], "backfilled": 0}
    run_check.stage_backfill(Adapter(), Store(), result,
                             time.monotonic() + 60, 1)

    assert covered == []
    assert reserved == [
        ("thread", 1, {"page": 1}),
        ("history_head", 1,
         {"since": 0, "page": 1, "pages": run_check.BACKFILL_MAX_PAGES,
          "trickle": False}),
    ]


def test_tick_real_storage_snapshot_and_replay(tmp_path, monkeypatch, capsys):
    import maintenance

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if path == "/projects":
                return {"projects": [{"id": 1, "is_unread": True,
                                      "karte": {}}],
                        "paginate": {"has_next": False, "timestamp": 123}}
            assert path == "/projects/1/messages"
            assert params["keep_read_status"] == 1
            return {"messages": [{"id": 1, "comment": "synthetic body",
                                  "created_at": "2026-09-19T00:00:00+09:00"}],
                    "paginate": {"has_next": False}}

        def _request(self, *args, **kwargs):
            pytest.fail("external request forbidden")

    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.json"
    config.write_text('{"deep_history":false}', encoding="utf-8")
    for name, value in {
        "HOME": tmp_path, "DB": data / "ledger.db",
        "ATTACH_DIR": data / "attachments", "LOCKFILE": data / "run.lock",
        "CONF_PATH": config, "CACHE": tmp_path / "absent-token.json",
    }.items():
        monkeypatch.setattr(run_check, name, str(value))
    for name, value in {
        "BACKUP_DIR": data / "backups", "SNAPSHOT_DIR": data / "snapshots",
        "LOGFILE": data / "run.log",
    }.items():
        monkeypatch.setattr(maintenance, name, str(value))
    drain = job_ops.drain_commands
    monkeypatch.setattr(job_ops, "drain_commands", lambda db, result:
                        drain(db, result, str(data / "cmd")))
    monkeypatch.setattr(run_check, "MCSAdapter", Adapter)
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: None)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: False)
    monkeypatch.setattr(notify_flush, "flush", lambda *a, **k:
                        pytest.fail("notification forbidden"))
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify", "--no-backfill"])

    assert run_check.main() == 0
    import mcs_requests
    import uuid

    source = ledger.LedgerReader(str(data / "snapshots" / "ledger-snapshot.db"))
    source_hash = source.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    source.close()
    (data / "cmd").mkdir()
    mcs_requests.enqueue({
        "version": 1, "cmd": "request.create", "command_id": str(uuid.uuid4()),
        "actor": "synthetic reviewer", "human_confirmed": True, "project_id": 1,
        "source_message_id": 1, "source_hash": source_hash,
        "title": "synthetic task",
    }, data / "cmd")
    assert run_check.main() == 0
    capsys.readouterr()
    db = ledger.LedgerReader(str(data / "snapshots" / "ledger-snapshot.db"))
    assert db.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM notify_outbox").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM read_marks").fetchone()[0] == 0
    assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM command_receipts WHERE outcome='applied'").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0] == 0
    assert db.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    db.close()


def test_notify_pending_attachments_jump_download_queue(tmp_path,
                                                        monkeypatch):
    """Attachments referenced by an unsent notify event must be
    downloaded before older backlog — otherwise flush() posts the event
    without files and an accepted event never re-sends them."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(1))
    # deep backlog of older pending attachments
    for i in range(10):
        db.db.execute(
            "INSERT INTO attachments(message_id,file_id,name,url,"
            "created_at) VALUES(?,?,?,?,?)",
            (100 + i, f"old{i}", "old.bin", "u_old", time.time()))
    # the newly-notified message's attachment sits at the back (FIFO)
    db.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,url,"
        "created_at) VALUES(?,?,?,?,?)",
        (999, "new", "new.bin", "u_new", time.time()))
    db.db.commit()
    db.outbox_add("new_messages", 1, {"message_ids": [999]})

    # priority ordering: the notify-referenced attachment comes first
    due = db.attachments_due(
        limit=30, priority_mids=db.pending_notify_message_ids())
    assert due[0]["message_id"] == 999
    assert [r["message_id"] for r in db.attachments_due(limit=30)][0] == 100

    # accepted events no longer claim priority — queue falls back to FIFO
    db.outbox_mark(db.db.execute(
        "SELECT event_id FROM notify_outbox").fetchone()[0], "accepted")
    assert db.pending_notify_message_ids() == []
    assert db.attachments_due(limit=30)[0]["message_id"] == 100

    # stage-level: re-queue the event — its attachment is downloaded
    # inside the same tick, before flush()
    db.db.execute("UPDATE notify_outbox SET state='pending'")
    db.db.commit()
    seen = []
    adapter = SimpleNamespace(
        download=lambda url, dest: seen.append(url)
        or {"bytes": 1, "sha256": "x"})
    monkeypatch.setattr(run_check, "ATTACH_DIR", str(tmp_path))
    run_check.stage_attachments(adapter, db, {"errors": []},
                                time.monotonic() + 60)
    assert seen[0] == "u_new"
    db.close()


def test_run_lock_excludes_second_writer(tmp_path):
    """FIX-R00-01: one flock per run — a second writer must be refused,
    and closing the fd must release it for the next holder."""
    import mcs_util
    lock = tmp_path / "run.lock"
    fd = mcs_util.acquire_run_lock(str(lock))
    assert fd is not None
    second = mcs_util.acquire_run_lock(str(lock))
    assert second is None
    os.close(fd)
    third = mcs_util.acquire_run_lock(str(lock))
    assert third is not None
    os.close(third)


def test_cli_writer_holds_run_lock(tmp_path, monkeypatch):
    """A manual write CLI must refuse to run while a tick holds the lock —
    the shared queue is drained by exactly one writer at a time."""
    import mcs_util
    lock = tmp_path / "run.lock"
    held = mcs_util.acquire_run_lock(str(lock))
    assert held is not None
    monkeypatch.setattr(mcs_util, "RUN_LOCK", str(lock))
    monkeypatch.setattr(extract_llm, "DB", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(sys, "argv", ["extract_llm", "--limit", "1"])
    assert extract_llm.main() == 3
    os.close(held)
    # free lock -> the CLI proceeds against the throwaway DB
    assert extract_llm.main() == 0


@pytest.mark.parametrize("stop", ["before_commit", "after_commit"])
def test_unread_commit_boundary_preserves_work_before_ack(tmp_path, monkeypatch, stop):
    """AT001/002: interrupted persistence can replay without orphaning work."""
    path = str(tmp_path / "boundary.db")
    db = ledger.Ledger(path)
    marks = []
    adapter = SimpleNamespace(
        list_unread=lambda: mcs_adapter.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1)]),
        fetch_unread_messages=lambda *_: mcs_adapter.MessageBatch(
            messages=[_message(unread=True)], reached=True),
        fetch_unread_replies=lambda *_: mcs_adapter.ReplyBatch([], []),
        mark_patient_read=lambda *args: marks.append(args),
    )

    def run():
        result = {"errors": [], "incomplete": [], "messages": 0,
                  "new_messages": 0, "marked_read": []}
        run_check.stage_unread(
            adapter, db, SimpleNamespace(mark_read=True), result,
            time.monotonic() + 30, db.begin_run(None), semantic=True)

    with monkeypatch.context() as patch:
        if stop == "before_commit":
            def fail_seed(*args, **kwargs):
                raise OSError("synthetic persistence interruption")
            patch.setattr(db, "_semantic_seed_tx", fail_seed)
            run()
        else:
            save = db.save_patient
            def stop_after_save(*args, **kwargs):
                save(*args, **kwargs)
                raise KeyboardInterrupt("synthetic stop after commit")
            patch.setattr(db, "save_patient", stop_after_save)
            with pytest.raises(KeyboardInterrupt):
                run()
    assert marks == []
    db.close()
    db = ledger.Ledger(path)
    expected = int(stop == "after_commit")
    for table in ("messages", "notify_outbox"):
        assert db.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == expected
    assert db.db.execute(
        "SELECT count(*) FROM fetch_jobs WHERE kind='semantic'"
    ).fetchone()[0] == expected
    assert db.db.execute("SELECT count(*) FROM read_marks").fetchone()[0] == 0

    run()
    assert marks == [(1, 123)]
    for table in ("messages", "notify_outbox"):
        assert db.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    assert db.db.execute(
        "SELECT count(*) FROM fetch_jobs WHERE kind='semantic'"
    ).fetchone()[0] == 1
    assert db.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    db.close()


@pytest.mark.parametrize("failure", ["snippet", "empty_ack", "lost_ack"])
def test_incomplete_fetch_or_ack_never_becomes_confirmed(tmp_path, failure):
    """AT003/008: uncertainty and hydration work survive a DB reopen."""
    path = str(tmp_path / "uncertain.db")
    db = ledger.Ledger(path)
    parent = _message(mid=10, unread=True)
    if failure == "snippet":
        parent.replies = [_message(mid=20, state="snippet", parent_id=10,
                                   unread=True)]
    calls = []

    class Adapter(mcs_adapter.MCSAdapter):
        def list_unread(self):
            return mcs_adapter.UnreadSnapshot(123, [_unread_patient(1)])

        def fetch_unread_messages(self, *args):
            return mcs_adapter.MessageBatch([parent], reached=True)

        def fetch_thread(self, *args):
            return parent.replies

        def _request(self, *args, **kwargs):
            calls.append(args)
            if failure == "lost_ack":
                raise mcs_adapter.MCSError("network_error", retryable=True)
            return 200, b'{}', {}

    result = {"errors": [], "incomplete": [], "messages": 0,
              "new_messages": 0, "marked_read": []}
    run_check.stage_unread(
        Adapter(), db, SimpleNamespace(mark_read=True), result,
        time.monotonic() + 30, db.begin_run(None))
    assert result["marked_read"] == []
    db.close()
    db = ledger.Ledger(path)
    assert not db.was_marked(1, 123)
    if failure == "snippet":
        assert calls == []
        assert db.job_pending("reply", 1, 20)["parent_id"] == 10
        assert db.db.execute(
            "SELECT body_state FROM messages WHERE message_id=20"
        ).fetchone()[0] == "snippet"
        assert db.db.execute(
            "SELECT fetch_state FROM patients WHERE project_id=1"
        ).fetchone()[0] == "incomplete"
        assert db.db.execute("SELECT count(*) FROM read_marks").fetchone()[0] == 0
    else:
        # the mark is two GETs now (list read = the mark, detail = confirm);
        # empty_ack returns {} for both -> unknown, lost_ack raises on the
        # first -> same unknown outcome
        assert 1 <= len(calls) <= 2
        assert db.db.execute(
            "SELECT status FROM read_marks WHERE project_id=1 AND snapshot_ts=123"
        ).fetchone()[0] == "unknown"
    db.close()


@pytest.mark.parametrize("error", [mcs_adapter.SessionExpired(status=401),
                                   mcs_adapter.MCSError("timeout", retryable=True)])
def test_backfill_page_failure_keeps_saved_page_without_coverage(tmp_path, error):
    """AT005: real pagination and storage preserve partial work on 401/timeout."""
    path = str(tmp_path / "partial-history.db")
    db = ledger.Ledger(path)
    patient = _unread_patient(1)
    patient.messages = [_message(mid=1)]
    db.save_patient(patient)
    db.set_coverage(1, 100)
    pages = []

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            pages.append(params["page"])
            if params["page"] == 2:
                raise error
            return {"messages": [{"id": 2, "comment": "synthetic page one",
                                  "created_at": "2099-01-01T00:00:00+00:00"}],
                    "paginate": {"has_next": True}}

    result = {"errors": [], "backfilled": 0}
    def run():
        run_check.stage_backfill(Adapter(), db, result,
                                 time.monotonic() + 60, db.begin_run(None),
                                 semantic=True)
    if isinstance(error, mcs_adapter.SessionExpired):
        with pytest.raises(mcs_adapter.SessionExpired):
            run()
    else:
        run()
    assert pages == [1, 2]
    assert result["backfilled"] == 1
    assert any(error.kind in item for item in result["errors"])
    db.close()
    db = ledger.Ledger(path)
    assert db.coverage_ts(1) == 100
    assert db.db.execute(
        "SELECT body_text FROM messages WHERE message_id=2"
    ).fetchone()[0] == "synthetic page one"
    assert db.job_pending("semantic", 1, 2) is not None
    db.close()


@pytest.mark.parametrize("complete", [True, False])
def test_backfill_recovers_read_reply_on_old_parent_without_hiding_gaps(tmp_path, complete):
    """AT006: parent age and unread status cannot discard a recent reply."""
    db = _ledger(tmp_path)
    patient = _unread_patient(1)
    patient.messages = [_message(mid=1)]
    db.save_patient(patient)
    watermark = db.high_watermark(1)
    coverage = watermark - 100
    db.set_coverage(1, coverage)

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            assert params["keep_read_status"] == 1
            reply = {"id": 11, "created_at": "2099-01-01T00:00:00+00:00",
                     "is_unread": False,
                     "comment" if complete else "comment_snippet": "new reply"}
            return {"messages": [{"id": 10, "comment": "old parent",
                                  "created_at": "2000-01-01T00:00:00+00:00",
                                  "count": {"thread_messages": 1},
                                  "thread_messages": [reply]}],
                    "paginate": {"has_next": False}}

        def fetch_thread(self, *args):
            return []

    result = {"errors": [], "backfilled": 0}
    run_check.stage_backfill(Adapter(), db, result, time.monotonic() + 60,
                             db.begin_run(None), semantic=True)
    row = db.db.execute(
        "SELECT parent_id,body_text,body_state,is_unread FROM messages WHERE message_id=11"
    ).fetchone()
    assert tuple(row) == (10, "new reply", "full" if complete else "snippet", 0)
    assert db.coverage_ts(1) == (watermark if complete else coverage)
    assert (db.job_pending("reply", 1, 11) is None) == complete
    assert db.job_pending("semantic", 1, 10) is not None
    assert db.db.execute("SELECT count(*) FROM notify_outbox").fetchone()[0] == 0
    db.close()


def test_session_expired_alert_throttled(tmp_path):
    """A dead session alerts once per hour, not once per tick — the run
    row and health file still record every expiry."""
    db = _ledger(tmp_path)
    assert run_check._alert_session_expired(db, 1, "d") is True
    assert run_check._alert_session_expired(db, 2, "d") is False
    db.db.execute(
        "UPDATE notify_outbox SET created_at=?"
        " WHERE kind='session_expired'",
        (time.time() - run_check.SESSION_ALERT_MIN_INTERVAL_S - 1,))
    db.db.commit()
    assert run_check._alert_session_expired(db, 3, "d") is True
    n = db.db.execute(
        "SELECT COUNT(*) c FROM notify_outbox"
        " WHERE kind='session_expired'").fetchone()["c"]
    assert n == 2
    db.close()


def test_err_str_includes_structured_detail():
    e = mcs_adapter.SessionExpired("auto_login=keychain_locked")
    assert run_check._err_str(e) == \
        "session_expired: auto_login=keychain_locked"
    e2 = mcs_adapter.MCSError("http_error", "GET /x", status=500)
    assert run_check._err_str(e2) == "http_error(status=500): GET /x"
    assert run_check._err_str(ValueError("v")) == "ValueError"


def test_snippet_parent_blocks_mark_read(tmp_path):
    """F05: a truncated parent body must not be ACKed — the patient
    stays incomplete until a later fetch upgrades it."""
    db = _ledger(tmp_path)
    marks = []
    adapter = SimpleNamespace(
        list_unread=lambda: mcs_adapter.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1)]),
        fetch_unread_messages=lambda *_: mcs_adapter.MessageBatch(
            messages=[_message(mid=10, unread=True, state="snippet")],
            reached=True),
        fetch_unread_replies=lambda *_: mcs_adapter.ReplyBatch([], []),
        mark_patient_read=lambda *a: marks.append(a),
    )
    result = {"errors": [], "incomplete": [], "messages": 0,
              "new_messages": 0, "marked_read": []}
    run_check.stage_unread(
        adapter, db, SimpleNamespace(mark_read=True), result,
        time.monotonic() + 30, db.begin_run(None))

    assert marks == []
    assert result["marked_read"] == []
    assert result["incomplete"] == [1]
    assert "parent_body_incomplete" in result["errors"][0]
    db.close()


def test_stage_derive_two_lanes(tmp_path, monkeypatch):
    """Speed lane + quality lane in one stage: the instant rule pass
    mints extract_v1 immediately (real-time analysis must not wait on
    the LLM queue) and the bounded v3 pass covers the same message."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1)])
    # batch off keeps this test on the llm_extract seam — the batch
    # path would reach the real server through its worker subprocess
    monkeypatch.setattr(extract_llm, "_BATCH_K", 0)
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: {"summary": "s"})
    result = {"errors": []}
    run_check.stage_derive(db, result, time.monotonic() + 60)
    assert result["extracted"] == 1
    assert result["extract_llm"]["done"] == 1
    assert db.artifacts("extract_v1", message_id=1)
    assert db.artifacts("extract_llm", message_id=1)
    db.close()


def test_backfill_tail_survives_a_completed_deep_import(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=100)])
    db.set_history_floor(1, 0)
    db.set_coverage(1, db.high_watermark(1))
    items = [{"id": mid, "comment": "synthetic post",
              "created_at": "2026-09-19T01:00:00+09:00"}
             for mid in range(1, 33)]

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, **kwargs):
            start = (params["page"] - 1) * params["per_page"]
            return {"messages": items[start:start + params["per_page"]],
                    "paginate": {"has_next": start + params["per_page"] < len(items)}}

    result = {"errors": [], "backfilled": 0}
    adapter = Adapter()
    run_check.stage_backfill(adapter, db, result, time.monotonic() + 300, 1)
    assert db.job_pending("history_head", 1)
    assert result["coverage_gaps"]
    assert db.db.execute("SELECT 1 FROM messages WHERE message_id=32").fetchone() is None
    job_ops.run_history_jobs(adapter, db, result, time.monotonic() + 300)
    assert db.db.execute("SELECT 1 FROM messages WHERE message_id=32").fetchone()
    assert db.job_state("history_head", 1) == "done"
    assert db.history_floor(1) == -1
    db.close()


def test_backfill_deferred_certification_is_not_an_error(tmp_path):
    """cov==wm with >BACKFILL_MAX_PAGES of history: the bounded scan can
    never reach the natural end, so certification defers to history_head
    — with lag=0 nothing is suspected missing, hence no error/gap."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=100)])
    db.set_history_floor(1, 0)
    db.set_coverage(1, db.high_watermark(1))
    items = [{"id": mid, "comment": "synthetic post",
              "created_at": "2026-09-19T00:00:00+09:00"}
             for mid in range(1, 33)]

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, **kwargs):
            start = (params["page"] - 1) * params["per_page"]
            return {"messages": items[start:start + params["per_page"]],
                    "paginate": {"has_next": start + params["per_page"] < len(items)}}

    result = {"errors": [], "backfilled": 0}
    run_check.stage_backfill(Adapter(), db, result, time.monotonic() + 300, 1)
    assert result["errors"] == []
    assert not result.get("coverage_gaps")
    assert db.job_pending("history_head", 1)
    assert db.db.execute("SELECT 1 FROM messages WHERE message_id=30").fetchone()
    db.close()


@pytest.mark.parametrize("status,expected", [(404, "failed"), (429, "pending")])
def test_download_failure_status_reaches_retry_policy(tmp_path, monkeypatch, status, expected):
    import urllib.error
    import mcs_worker
    db = _ledger(tmp_path)
    message = _message()
    message.attachments = [_att("file")]
    db.save_messages([message])
    def fail(req, timeout):
        raise urllib.error.HTTPError(req.full_url, status, "synthetic", {}, None)

    monkeypatch.setattr(mcs_adapter, "no_proxy_opener",
                        lambda *handlers: SimpleNamespace(open=fail))
    adapter = mcs_adapter.MCSAdapter(worker=lambda payload, timeout, deadline:
        mcs_worker._execute(dict(payload, timeout=timeout)))
    adapter._token = "synthetic"
    monkeypatch.setattr(run_check, "ATTACH_DIR", str(tmp_path))
    run_check.stage_attachments(adapter, db, {"errors": []}, time.monotonic() + 100)
    row = db.db.execute("SELECT state,error FROM attachments").fetchone()
    assert tuple(row) == (expected, f"http_{status}")
    assert not (tmp_path / "1.part").exists()
    adapter.set_deadline(time.monotonic() - 1)
    with pytest.raises(mcs_adapter.MCSError) as exc:
        adapter.download("https://www.medical-care.net/f", str(tmp_path / "later"))
    assert exc.value.kind == "deadline_exceeded"
    db.close()


def test_health_counts_held_sends_and_age_from_creation(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    db.db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES('extract_llm',1,'{}','{')")
    db.db.commit()
    db.job_add("extract_qc", 1, 1)
    eid = db.outbox_add("new_messages", 1, {"message_ids": [1]})
    db.outbox_hold(eid)
    other = db.outbox_add("new_messages", 1, {"message_ids": [2]})
    now = time.time()
    db.db.execute("UPDATE notify_outbox SET created_at=?,next_try=? WHERE event_id=?",
                  (now - 1000, now + 500, other))
    db.db.execute("UPDATE fetch_jobs SET created_at=?,next_try=? WHERE kind='extract_qc'",
                  (now - 1000, now + 500))
    db.db.commit()
    health = run_check._health(db, {"notify": {}, "errors": [], "coverage_gaps": [{}]}, "partial")
    assert health["overall"] == "degraded" and health["collection"] == "incomplete"
    assert health["notify"]["held"] == 1
    assert health["notify"]["oldest_age_s"] >= 1000
    assert health["extract_qc_jobs"]["pending"] == 1
    assert health["extract_qc_jobs"]["oldest_age_s"] >= 1000
    coverage = health["extract_v2_coverage"]
    assert coverage["eligible"] == 1 and coverage["current"] == 0
    assert coverage["poison_gated"] == 1 and coverage["ratio"] == 0
    assert coverage["extract_version"] == extract_llm.EXTRACT_VERSION
    db.close()


def test_health_write_binds_run_and_survives_failure(tmp_path, monkeypatch):
    """A health.json write failure must not roll back committed ingest
    work nor mark the stored run failed — it raises a separate
    machine-readable 'health_write_failed' incident instead (GAP-1)."""
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    result = {"errors": [], "notify": {}}
    monkeypatch.setattr(run_check, "HEALTH_FILE",
                        str(tmp_path / "health.json"))

    # happy path first — fresh present evidence bound to its source run
    run_check._write_health(db, result, "ok", run_id=7)
    h = json.loads((tmp_path / "health.json").read_text())
    assert h["overall"] == "ok" and h["run_id"] == 7 and h["at"]

    # now break the atomic publish: stored messages stay, run row stays
    # 'ok', and exactly one incident lands in the durable outbox
    def boom(path, text):
        raise OSError("synthetic full disk")
    monkeypatch.setattr(run_check.maintenance, "atomic_publish_text", boom)
    run_id = db.begin_run(None, "tick")
    db.finish_run(run_id, "ok", "")
    run_check._write_health(db, result, "ok", run_id=run_id)
    assert any(e.startswith("health_write_failed")
               for e in result["errors"])
    kinds = [r[0] for r in db.db.execute(
        "SELECT kind FROM notify_outbox WHERE kind='health_write_failed'")]
    assert kinds == ["health_write_failed"]
    assert db.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert db.db.execute("SELECT status FROM runs WHERE run_id=?",
                         (run_id,)).fetchone()[0] == "ok"
    db.close()


# ---------- self-post / latest probe ----------


def test_self_probe_fetches_and_notifies_own_post(tmp_path):
    """A positive probe triggers a bounded history fetch and the newly
    stored OWN post (is_unread=0 by definition) still lands in a notify
    intent — notify_all_new widens eligibility past the unread flag."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(90))
    db.save_messages([_msg_at(100, 90, _iso(1), unread=False)])
    wm = db.high_watermark(90)
    own = _msg_at(101, 90, _iso(0), unread=False)

    class Adapter:
        def __init__(self):
            self.history_calls = 0

        def fetch_latest(self, pid):
            assert pid == 90
            return {"message_id": 101, "is_self_only": True}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            assert since == wm
            self.history_calls += 1
            return mcs_adapter.MessageBatch([own], pages=1, reached=True)

    adapter = Adapter()
    result = {"errors": [], "new_messages": 0}
    run_check.stage_self_probe(adapter, db, result,
                               time.monotonic() + 300, run_id=1)

    assert adapter.history_calls == 1
    assert result["self_probe"] == 1
    assert result["self_probe_fetched"] == [90]
    assert result["new_messages"] == 1
    row = db.db.execute(
        "SELECT kind,payload FROM notify_outbox").fetchone()
    assert row["kind"] == "new_messages"
    payload = json.loads(row["payload"])
    assert payload["source"] == "self"
    assert payload["message_ids"] == [101]
    db.close()


def test_self_probe_skips_when_latest_already_stored(tmp_path):
    """The latest endpoint returns no timestamp — freshness is decided
    by whether the returned id is already in the ledger."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(91))
    db.save_messages([_msg_at(100, 91, _iso(0), unread=False)])

    class Adapter:
        def fetch_latest(self, pid):
            return {"message_id": 100, "is_self_only": False}

        def fetch_history(self, *a, **k):
            raise AssertionError("must not fetch")

    result = {"errors": [], "new_messages": 0}
    run_check.stage_self_probe(Adapter(), db, result,
                               time.monotonic() + 300, run_id=1)
    assert result["self_probe"] == 1
    assert "self_probe_fetched" not in result
    assert db.db.execute("SELECT COUNT(*) c FROM notify_outbox"
                         ).fetchone()["c"] == 0
    db.close()


def test_self_probe_null_message_means_nothing_new(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(92))
    db.save_messages([_msg_at(100, 92, _iso(0), unread=False)])

    class Adapter:
        def fetch_latest(self, pid):
            return {"message_id": None, "is_self_only": False}

        def fetch_history(self, *a, **k):
            raise AssertionError("must not fetch")

    result = {"errors": [], "new_messages": 0}
    run_check.stage_self_probe(Adapter(), db, result,
                               time.monotonic() + 300, run_id=1)
    assert result["self_probe"] == 1
    assert result["errors"] == []
    db.close()


def test_self_probe_never_imported_patient_skipped(tmp_path):
    """No high watermark -> the durable deep-import jobs own the backlog;
    probing them would only re-trigger full history fetches."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(93))

    class Adapter:
        def fetch_latest(self, pid):
            raise AssertionError("must not probe")

    result = {"errors": [], "new_messages": 0}
    run_check.stage_self_probe(Adapter(), db, result,
                               time.monotonic() + 300, run_id=1)
    assert result["self_probe"] == 0
    db.close()


def test_self_probe_missed_post_uses_probe_source(tmp_path):
    """is_self_only=False means others' already-read posts were missed —
    they notify under the 'probe' source, not 'self'."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(94))
    db.save_messages([_msg_at(100, 94, _iso(1), unread=False)])
    missed = _msg_at(101, 94, _iso(0), unread=False)

    class Adapter:
        def fetch_latest(self, pid):
            return {"message_id": 101, "is_self_only": False}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch([missed], pages=1,
                                            reached=True)

    result = {"errors": [], "new_messages": 0}
    run_check.stage_self_probe(Adapter(), db, result,
                               time.monotonic() + 300, run_id=1)
    payload = json.loads(db.db.execute(
        "SELECT payload FROM notify_outbox").fetchone()["payload"])
    assert payload["source"] == "probe"
    assert payload["message_ids"] == [101]
    db.close()


def test_self_probe_unfetchable_id_not_retried(tmp_path):
    """If a completed fetch cannot store the probed id (e.g. it is a
    reply only reachable via its own thread), the probe marker stops the
    same id from re-triggering a fetch on every subsequent tick."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(96))
    db.save_messages([_msg_at(100, 96, _iso(0), unread=False)])

    class Adapter:
        def __init__(self):
            self.history_calls = 0

        def fetch_latest(self, pid):
            return {"message_id": 555, "is_self_only": False}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            self.history_calls += 1
            return mcs_adapter.MessageBatch([], pages=1, reached=True)

    adapter = Adapter()
    for _ in range(2):
        result = {"errors": [], "new_messages": 0}
        run_check.stage_self_probe(adapter, db, result,
                                   time.monotonic() + 300, run_id=1)
        assert result["self_probe"] == 1
    assert adapter.history_calls == 1
    db.close()


def test_self_probe_incomplete_walk_retries_latest_id(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(97))
    db.save_messages([_msg_at(100, 97, _iso(0), unread=False)])

    class Adapter:
        def __init__(self):
            self.history_calls = 0

        def fetch_latest(self, pid):
            return {"message_id": 555, "is_self_only": False}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            self.history_calls += 1
            if self.history_calls == 1:
                return mcs_adapter.MessageBatch([], pages=2, reached=False)
            return mcs_adapter.MessageBatch(
                [_msg_at(555, 97, _iso(1), unread=False)],
                pages=1, reached=True)

    adapter = Adapter()
    first = {"errors": [], "new_messages": 0}
    run_check.stage_self_probe(adapter, db, first,
                               time.monotonic() + 300, run_id=1)
    assert db.probe_marker(97) is None
    assert "probe 97: history_incomplete" in first["errors"]

    second = {"errors": [], "new_messages": 0}
    run_check.stage_self_probe(adapter, db, second,
                               time.monotonic() + 300, run_id=2)
    assert adapter.history_calls == 2
    assert db.has_message(555)
    assert second["new_messages"] == 1
    db.close()


def test_self_probe_incomplete_body_does_not_mark_latest(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(98))
    db.save_messages([_msg_at(100, 98, _iso(1), unread=False)])

    class Adapter:
        def __init__(self):
            self.history_calls = 0

        def fetch_latest(self, pid):
            return {"message_id": 555, "is_self_only": False}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            self.history_calls += 1
            if self.history_calls == 1:
                partial = _msg_at(101, 98, _iso(0), unread=False)
                partial.body_state = "snippet"
                return mcs_adapter.MessageBatch(
                    [partial], pages=1, reached=True)
            return mcs_adapter.MessageBatch(
                [_msg_at(555, 98, _iso(0), unread=False)],
                pages=1, reached=True)

    adapter = Adapter()
    first = {"errors": [], "new_messages": 0}
    run_check.stage_self_probe(
        adapter, db, first, time.monotonic() + 300, run_id=1)
    assert db.probe_marker(98) is None
    assert "probe 98: history_incomplete" in first["errors"]

    second = {"errors": [], "new_messages": 0}
    run_check.stage_self_probe(
        adapter, db, second, time.monotonic() + 300, run_id=2)
    assert adapter.history_calls == 2
    assert db.has_message(555)
    db.close()


def test_tail_command_failure_marks_run_partial(tmp_path, monkeypatch, capsys):
    import maintenance
    import mcs_signals
    import notify_cards
    import notify_cmds

    class Adapter:
        def __init__(self, **kwargs):
            pass

        def set_deadline(self, deadline):
            pass

        def list_unread(self):
            return mcs_adapter.UnreadSnapshot(timestamp=123, patients=[])

        def list_projects(self):
            return []

        def self_profile(self):
            return {}

    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.json"
    config.write_text(
        '{"deep_history":false,"semantic":{"mode":"off"}}',
        encoding="utf-8")
    for name, value in {
        "HOME": tmp_path, "DB": data / "ledger.db",
        "ATTACH_DIR": data / "attachments", "LOCKFILE": data / "run.lock",
        "HEALTH_FILE": data / "health.json", "CONF_PATH": config,
        "MCSAdapter": Adapter, "stage_derive": lambda *a, **k: None,
    }.items():
        monkeypatch.setattr(run_check, name, str(value)
                            if isinstance(value, Path) else value)
    for name in ("drain_commands", "seed_discovery", "run_discovery",
                 "run_reply_jobs", "run_history_jobs", "run_reconcile_jobs",
                 "seed_trickle"):
        monkeypatch.setattr(job_ops, name, lambda *a, **k: None)
    for name in ("ensure_dirs", "recover", "sweep", "publish_flags",
                 "gc", "clear_snapshot_dirty"):
        monkeypatch.setattr(notify_cards, name, lambda *a, **k: None)
    monkeypatch.setattr(mcs_signals, "record_self_profile", lambda *a: False)
    monkeypatch.setattr(maintenance, "daily_backup", lambda *a: None)
    monkeypatch.setattr(maintenance, "rotate_log", lambda *a: None)
    monkeypatch.setattr(maintenance, "prune_attachments", lambda *a: 0)
    monkeypatch.setattr(maintenance, "publish_snapshot", lambda *a: True)
    calls = []

    def fail_tail(*args, **kwargs):
        calls.append(None)
        if len(calls) == 2:
            raise RuntimeError("synthetic tail failure")

    monkeypatch.setattr(notify_cmds, "drain_int_commands", fail_tail)
    monkeypatch.setattr(
        sys, "argv", ["run_check", "--no-notify", "--no-backfill"])

    assert run_check.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["errors"] == ["cmd_int_tail: RuntimeError"]
    db = ledger.Ledger(str(data / "ledger.db"))
    assert db.db.execute(
        "SELECT status FROM runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()[0] == "partial"
    db.close()
    assert json.loads((data / "health.json").read_text())["run_status"] == "partial"


def test_consent_hold_tick_freezes_all_but_approve(tmp_path, monkeypatch,
                                                   capsys):
    """An awaiting_consent restore marker switches the tick to
    consent-only: no adapter, no ingest, no reconcile — only the
    ops.restore_approve drain runs so the approved loss report cannot
    drift (T13)."""
    import notify_cards
    import mcs_requests
    import uuid

    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.json"
    config.write_text('{"deep_history":false}', encoding="utf-8")
    for name, value in {
        "HOME": tmp_path, "DB": data / "ledger.db",
        "ATTACH_DIR": data / "attachments", "LOCKFILE": data / "run.lock",
        "CONF_PATH": config, "CACHE": tmp_path / "absent-token.json",
    }.items():
        monkeypatch.setattr(run_check, name, str(value))
    monkeypatch.setattr(run_check, "MCSAdapter",
                        lambda *a, **k: pytest.fail(
                            "adapter must not be built during the hold"))
    notify_cards.mark_restored(str(data), backup_path=str(data / "b.db"),
                               phase="awaiting_consent")
    cmd = data / "cmd"
    cmd.mkdir()
    drain = job_ops.drain_commands
    monkeypatch.setattr(job_ops, "drain_commands",
                        lambda db, result: drain(db, result, str(cmd)))
    # an unrelated op stays queued — its writes would be wiped by the swap
    mcs_requests.enqueue(
        {"version": 1, "cmd": "ops.pause",
         "command_id": str(uuid.uuid4()), "actor": "t",
         "human_confirmed": True, "project_id": 1,
         "feature": "semantic"}, str(cmd))
    rid = "a" * 64
    mcs_requests.enqueue(
        {"version": 1, "cmd": "ops.restore_approve",
         "command_id": str(uuid.uuid4()), "actor": "t",
         "human_confirmed": True, "project_id": None,
         "report_id": rid, "backup_sha256": "b" * 64,
         "backup_schema": 7, "reason": "synthetic"}, str(cmd))
    import mcs_update
    spawned = []
    monkeypatch.setattr(mcs_update, "spawn_detached",
                        lambda: spawned.append(1))
    # the respawn path recomputes the report — stub the backup read but
    # keep the REAL consent scan against the tick's own ledger
    monkeypatch.setattr(mcs_update, "_restore_loss_report",
                        lambda p: {"report_id": rid,
                                   "backup_sha256": "b" * 64,
                                   "backup_schema": 7})
    monkeypatch.setattr(mcs_update, "LEDGER", str(data / "ledger.db"))
    monkeypatch.setattr(sys, "argv", ["run_check"])
    assert run_check.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["restore_consent_hold"] is True
    assert out["commands"] == 1                # only the consent op ran
    assert spawned                             # updater re-launched
    left = [p.name for p in cmd.glob("*.json")]
    assert len(left) == 1                      # pause stayed queued
