"""Collection stage contracts for unread acknowledgement, backfill, downloads and inference."""
import extract_llm_provider
import extract_llm_resident
import extract_llm_runner
import collection_stages
import semantic_drain
import health_report
import mcs_contract
import json
import time
from contextlib import closing
from types import SimpleNamespace
import pytest
import extract_llm
import job_ops
import ledger
import mcs_adapter
import mcs_download
import run_check
from ingest_testkit import _ledger, _message, _unread_patient, _att
from run_check_stage_testkit import _capped_run, _certified_capped_db, _pending_attachments, _pinned_history_adapter, _sem_args, _sem_setup, _stub_llm_lane
from run_check_stage_testkit import _no_disk_guard as _no_disk_guard
from run_check_stage_testkit import _point_run_check_at as _point_run_check_at
from run_check_stage_testkit import _MemoAdapter as _MemoAdapter
from run_check_stage_testkit import _karte_db as _karte_db
from run_check_stage_testkit import _run_stage as _run_stage


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
    monkeypatch.setattr(collection_stages, 'ATTACH_DIR', str(tmp_path))
    collection_stages.stage_attachments(adapter, db, {"errors": []},
                                time.monotonic() + 60)
    assert len(set(seen)) == 2


def test_backfill_does_not_advance_past_missing_reply():
    parent = _message(mid=10)
    parent.reply_count = 1
    covered = []
    reserved = []

    class Adapter:
        def fetch_history(self, *args, **kwargs):
            return mcs_contract.MessageBatch([parent], pages=1, reached=True)

        def fetch_thread(self, *args):
            return []
        def fetch_thread_window(self, pid, parent_id, start_page=1):
            return mcs_contract.MessageBatch(self.fetch_thread(pid, parent_id), pages=1, reached=True)

    class Store:
        # no prior history_head row (stall probe reads fetch_jobs)
        db = SimpleNamespace(
            execute=lambda *a: SimpleNamespace(fetchone=lambda: None))

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
    collection_stages.stage_backfill(Adapter(), Store(), result,
                             time.monotonic() + 60, 1)

    assert covered == []
    assert reserved == [
        ("thread", 1, {"page": 1}),
        ("history_head", 1,
         {"since": 0, "page": 1, "pages": collection_stages.BACKFILL_MAX_PAGES,
          "trickle": False}),
    ]


def test_notify_pending_attachments_jump_download_queue(tmp_path,
                                                        monkeypatch):
    """Attachments referenced by an unsent notify event must be
    downloaded before older backlog — otherwise flush() posts the event
    without files and an accepted event never re-sends them."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(1))
    db.save_messages([_message(mid) for mid in [*range(100, 110), 999]])
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
    monkeypatch.setattr(collection_stages, 'ATTACH_DIR', str(tmp_path))
    collection_stages.stage_attachments(adapter, db, {"errors": []},
                                time.monotonic() + 60)
    assert seen[0] == "u_new"
    db.close()


@pytest.mark.parametrize("stop", ["before_commit", "after_commit"])
def test_unread_commit_boundary_preserves_work_before_ack(tmp_path, monkeypatch, stop):
    """AT001/002: interrupted persistence can replay without orphaning work."""
    path = str(tmp_path / "boundary.db")
    db = ledger.Ledger(path)
    marks = []
    adapter = SimpleNamespace(
        list_unread=lambda: mcs_contract.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1)]),
        fetch_unread_messages=lambda *_: mcs_contract.MessageBatch(
            messages=[_message(unread=True)], reached=True),
        fetch_unread_replies=lambda *_: mcs_contract.ReplyBatch([], []),
        fetch_latest=lambda pid: {"message_id": 1, "is_self_only": False},
        mark_patient_read=lambda *a, **k: marks.append(a),
    )

    def run():
        result = {"errors": [], "incomplete": [], "messages": 0,
                  "new_messages": 0, "marked_read": []}
        collection_stages.stage_unread(
            adapter, db, result,
            time.monotonic() + 30, db.begin_run(None), semantic=True, mark_read=True)

    with monkeypatch.context() as patch:
        if stop == "before_commit":
            def fail_seed(*args, **kwargs):
                raise OSError("synthetic persistence interruption")
            patch.setattr(db, "semantic_seed_tx", fail_seed)
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
    for table, where in (("messages", ""), ("notify_outbox", " WHERE kind='new_messages'"),
                         ("notify_outbox", " WHERE kind='patient_history'")):
        assert db.db.execute(f"SELECT count(*) FROM {table}{where}").fetchone()[0] == expected
    assert db.db.execute(
        "SELECT count(*) FROM fetch_jobs WHERE kind='semantic'"
    ).fetchone()[0] == expected
    assert db.db.execute("SELECT count(*) FROM read_marks").fetchone()[0] == 0

    run()
    assert marks == [(1, 123)]
    for table, where in (("messages", ""), ("notify_outbox", " WHERE kind='new_messages'")):
        assert db.db.execute(f"SELECT count(*) FROM {table}{where}").fetchone()[0] == 1
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
            return mcs_contract.UnreadSnapshot(123, [_unread_patient(1)])

        def fetch_unread_messages(self, *args):
            return mcs_contract.MessageBatch([parent], reached=True)

        def fetch_thread(self, *args):
            return parent.replies

        def _request(self, *args, **kwargs):
            calls.append(args)
            if failure == "lost_ack":
                raise mcs_contract.MCSError("network_error", retryable=True)
            return 200, b'{}', {}

    result = {"errors": [], "incomplete": [], "messages": 0,
              "new_messages": 0, "marked_read": []}
    collection_stages.stage_unread(
        Adapter(), db, result,
        time.monotonic() + 30, db.begin_run(None), mark_read=True)
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


@pytest.mark.parametrize("error", [mcs_contract.SessionExpired(status=401),
                                   mcs_contract.MCSError("timeout", retryable=True)])
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
        collection_stages.stage_backfill(Adapter(), db, result,
                                 time.monotonic() + 60, db.begin_run(None),
                                 semantic=True)
    if isinstance(error, mcs_contract.SessionExpired):
        with pytest.raises(mcs_contract.SessionExpired):
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
    collection_stages.stage_backfill(Adapter(), db, result, time.monotonic() + 60,
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


def test_err_str_includes_structured_detail():
    e = mcs_contract.SessionExpired("auto_login=keychain_locked")
    assert mcs_contract.describe_error(e) == \
        "session_expired: auto_login=keychain_locked"
    e2 = mcs_contract.MCSError("http_error", "GET /x", status=500)
    assert mcs_contract.describe_error(e2) == "http_error(status=500): GET /x"
    assert mcs_contract.describe_error(ValueError("v")) == "ValueError"


@pytest.mark.parametrize("state", ["snippet", "unknown", "full", "deleted"])
def test_parent_body_completeness_controls_mark_read(tmp_path, state):
    """F05: an unreceived or truncated parent must not be ACKed — the patient
    stays incomplete until a later fetch upgrades it."""
    import mcs_payload

    payload = {"id": 10, "is_unread": True}
    if state == "snippet":
        payload["comment_snippet"] = "synthetic partial body"
    elif state == "full":
        payload.update(comment="", files=[{"name": "synthetic.pdf",
                       "url": "https://www.medical-care.net/synthetic-file"}])
    elif state == "deleted":
        payload["delete_user"] = {"id": 1}
    message = mcs_payload.normalize_message(payload, 1)
    assert message.body_state == state
    db = _ledger(tmp_path)
    marks = []
    adapter = SimpleNamespace(
        list_unread=lambda: mcs_contract.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1)]),
        fetch_unread_messages=lambda *_: mcs_contract.MessageBatch(
            messages=[message],
            reached=True),
        fetch_unread_replies=lambda *_: mcs_contract.ReplyBatch([], []),
        fetch_latest=lambda *_: {"message_id": 10},
        mark_patient_read=lambda *a, **k: marks.append(a),
    )
    result = {"errors": [], "incomplete": [], "messages": 0,
              "new_messages": 0, "marked_read": []}
    collection_stages.stage_unread(
        adapter, db, result,
        time.monotonic() + 30, db.begin_run(None), mark_read=True)

    incomplete = state in ("snippet", "unknown")
    assert len(marks) == (not incomplete)
    assert result["marked_read"] == ([] if incomplete else [1])
    assert result["incomplete"] == ([1] if incomplete else [])
    if incomplete:
        assert "parent_body_incomplete" in result["errors"][0]
    else:
        assert result["errors"] == []
    assert db.db.execute("SELECT fetch_state FROM patients WHERE project_id=1").fetchone()[0] == (
        "incomplete" if incomplete else "complete")
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
    monkeypatch.setattr(extract_llm_resident, '_BATCH_K', extract_llm._BATCH_K)
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: {"summary": "s"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    result = {"errors": [], "realtime_ids": [1]}
    run_check.stage_derive(db, result, time.monotonic() + 150)
    assert result["extracted"] == 1
    assert result["extract_llm"]["done"] == 1
    assert db.artifacts("extract_v1", message_id=1)
    assert db.artifacts("extract_llm", message_id=1)
    db.close()


@pytest.mark.parametrize("semantic,expected", [
    ({"mode": "off", "fact_source": "canonical",
      "fact_source_gate": "g6-v1:abc123"}, set()),
    ({"mode": "off"}, {1}),
])
def test_stage_derive_admission_by_fact_source(tmp_path, monkeypatch,
                                               semantic, expected):
    """canonical mode admits nothing new (fail-closed empty set);
    otherwise admission is unrestricted (None)."""
    db = _ledger(tmp_path)
    seen = {}

    def run_pending(ledger, **kw):
        seen["admitted"] = kw["admitted_ids"]
        assert kw["allow_cloud"] is False
        return {"done": 0, "failed": 0, "left": 0, "pids": []}
    monkeypatch.setattr(extract_llm, "run_pending", run_pending)
    monkeypatch.setattr(extract_llm_resident, 'run_pending', extract_llm.run_pending)
    result = {"errors": [], "realtime_ids": [1]}
    run_check.stage_derive(db, result, time.monotonic() + 150,
                           cfg={"semantic": semantic})
    assert "admitted" in seen and seen["admitted"] == expected
    db.close()


def test_stage_derive_reports_gc_error_without_losing_extraction_result(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    monkeypatch.setattr(extract_llm, "pinned_slot_busy", lambda deadline: False)
    monkeypatch.setattr(extract_llm, "run_pending", lambda ledger, **kw: {
        "done": 1, "failed": 0, "left": 0, "pids": [],
        "gc_error": "extract_stale_gc_failed"})
    monkeypatch.setattr(extract_llm_resident, 'run_pending', extract_llm.run_pending)
    result = {"errors": [], "realtime_ids": [1]}
    run_check.stage_derive(db, result, time.monotonic() + 150, cfg={})
    assert result["extract_llm"]["done"] == 1
    assert "extract_llm: extract_stale_gc_failed" in result["errors"]
    db.close()


@pytest.mark.parametrize(("cap", "limit"), [(90, 1), (240, 3)])
def test_stage_derive_limits_rows_to_budget(tmp_path, monkeypatch, cap, limit):
    db = _ledger(tmp_path)
    calls = _stub_llm_lane(monkeypatch)
    run_check.stage_derive(db, {"errors": [], "realtime_ids": [1]}, time.monotonic() + 600, {},
                           llm_budget_cap=cap)
    assert calls[0]["limit"] == limit and calls[0]["budget_s"] == cap
    assert calls[0]["oldest_first"] is False
    db.close()


def test_stage_derive_skips_llm_lane_when_pinned_slot_busy(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    calls = _stub_llm_lane(monkeypatch, busy=True)
    result = {"errors": [], "realtime_ids": [1]}
    run_check.stage_derive(db, result, time.monotonic() + 600, {})
    assert calls == []
    assert result["extract_llm"] == {"done": 0, "failed": 0, "left": -1,
                                     "pids": [], "skipped_busy": True}
    db.close()


def test_stage_derive_carries_recent_notified_arrivals(tmp_path, monkeypatch):
    """A notified arrival a busy or one-call tick left behind stays on
    the realtime lane; stale and never-notified rows go to the backlog."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=m) for m in (1, 2, 3)])
    now = time.time()
    db.db.execute("UPDATE messages SET notified_at=? WHERE message_id=1",
                  (now - 60,))
    db.db.execute("UPDATE messages SET notified_at=? WHERE message_id=2",
                  (now - run_check.REALTIME_CARRY_S - 60,))
    db.db.commit()
    calls = _stub_llm_lane(monkeypatch)
    run_check.stage_derive(db, {"errors": []}, time.monotonic() + 600, {})
    assert calls[0]["admitted_ids"] == {1}
    db.close()


@pytest.mark.parametrize(("cfg", "admitted"), [
    ({}, {1}),
    (None, {1}),                                   # direct caller: defaults
    ({"semantic": "typo"}, set()),                  # semantic_config error
    ({"semantic": {"fact_sourse": "canonical"}}, set()),
    # gate missing → error → legacy fallback used to open v3 unrestricted
    ({"semantic": {"fact_source": "canonical"}}, set()),
])
def test_stage_derive_legacy_admission_fails_closed(tmp_path, monkeypatch,
                                                     cfg, admitted):
    db = _ledger(tmp_path)
    calls = _stub_llm_lane(monkeypatch)
    run_check.stage_derive(db, {"errors": [], "realtime_ids": [1]}, time.monotonic() + 600, cfg)
    assert calls[0]["admitted_ids"] == admitted
    db.close()


@pytest.mark.parametrize(("state", "probe", "expect"), [
    ({}, {0: True, 1: False}, True),
    ({}, {0: False, 1: True}, False),
    ({}, None, False),                               # probe failed
    ({"_SLOT_OVERRIDE": 0}, {0: True, 1: True}, True),
])
def test_pinned_slot_busy(monkeypatch, state, probe, expect):
    for k, v in state.items():
        monkeypatch.setattr(extract_llm, k, v)
    monkeypatch.delenv("MCS_LLM_ADMISSION", raising=False)
    monkeypatch.setattr(extract_llm.local_llm, "request_slot", lambda: 0)
    monkeypatch.setattr(extract_llm, "_slots_busy", lambda deadline: probe)
    monkeypatch.setattr(extract_llm_provider, '_slots_busy', extract_llm._slots_busy)
    assert extract_llm.pinned_slot_busy() is expect


def test_pinned_slot_busy_never_peeks_under_admission(monkeypatch):
    monkeypatch.setenv("MCS_LLM_ADMISSION", "1")
    monkeypatch.setattr(extract_llm, "_slots_busy",
                        lambda deadline: pytest.fail("T20: no /slots peek"))
    monkeypatch.setattr(extract_llm_provider, '_slots_busy', extract_llm._slots_busy)
    assert extract_llm.pinned_slot_busy() is False


def test_backfill_tail_survives_a_completed_deep_import(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=100)])
    db.set_history_floor(1, 0)
    db.set_coverage(1, db.high_watermark(1))
    result = {"errors": [], "backfilled": 0}
    adapter = _pinned_history_adapter()
    collection_stages.stage_backfill(adapter, db, result, time.monotonic() + 300, 1)
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
    result = {"errors": [], "backfilled": 0}
    collection_stages.stage_backfill(_pinned_history_adapter(created_at="2026-09-19T00:00:00+09:00"), db, result, time.monotonic() + 300, 1)
    assert result["errors"] == []
    assert not result.get("coverage_gaps")
    assert db.job_pending("history_head", 1)
    assert db.db.execute("SELECT 1 FROM messages WHERE message_id=30").fetchone()
    db.close()


@pytest.mark.parametrize(("status", "expected"), [(404, "failed"), (429, "pending")])
def test_download_failure_status_reaches_retry_policy(tmp_path, monkeypatch, status, expected):
    import urllib.error
    import mcs_worker
    db = _ledger(tmp_path)
    message = _message()
    message.attachments = [_att("file")]
    db.save_messages([message])
    def fail(req, timeout):
        raise urllib.error.HTTPError(req.full_url, status, "synthetic", {}, None)

    monkeypatch.setattr(mcs_download, "no_proxy_opener",
                        lambda *handlers: SimpleNamespace(open=fail))
    adapter = mcs_adapter.MCSAdapter(worker=lambda payload, timeout, deadline:
        mcs_worker._execute(dict(payload, timeout=timeout)))
    adapter._token = "synthetic"
    monkeypatch.setattr(collection_stages, 'ATTACH_DIR', str(tmp_path))
    collection_stages.stage_attachments(adapter, db, {"errors": []}, time.monotonic() + 100)
    row = db.db.execute("SELECT state,error FROM attachments").fetchone()
    assert tuple(row) == (expected, f"http_{status}")
    assert not (tmp_path / "1.part").exists()
    adapter.set_deadline(time.monotonic() - 1)
    with pytest.raises(mcs_contract.MCSError) as exc:
        adapter.download("https://www.medical-care.net/f", str(tmp_path / "later"))
    assert exc.value.kind == "deadline_exceeded"
    db.close()


def test_attachments_defer_under_disk_pressure(tmp_path, monkeypatch):
    db = _pending_attachments(tmp_path)
    monkeypatch.setenv("MCS_DISK_GUARD_MB", "512")
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 1024.0)
    monkeypatch.setattr(collection_stages, 'ATTACH_DIR', str(tmp_path))
    adapter = SimpleNamespace(download=lambda *a: pytest.fail("downloaded"))
    result = {"errors": []}
    collection_stages.stage_attachments(adapter, db, result, time.monotonic() + 100)
    assert result == {"errors": [], "attachments_deferred": 2}
    rows = db.db.execute("SELECT state,attempts FROM attachments").fetchall()
    assert [tuple(r) for r in rows] == [("pending", 0), ("pending", 0)]
    db.close()


def test_enospc_download_keeps_attachment_pending(tmp_path, monkeypatch):
    import errno
    import mcs_worker
    db = _pending_attachments(tmp_path)

    class Full:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n): raise OSError(errno.ENOSPC, "synthetic")

    monkeypatch.setattr(mcs_download, "no_proxy_opener",
                        lambda *handlers: SimpleNamespace(
                            open=lambda req, timeout: Full()))
    adapter = mcs_adapter.MCSAdapter(worker=lambda payload, timeout, deadline:
        mcs_worker._execute(dict(payload, timeout=timeout)))
    adapter._token = "synthetic"
    monkeypatch.setattr(collection_stages, 'ATTACH_DIR', str(tmp_path))
    result = {"errors": []}
    collection_stages.stage_attachments(adapter, db, result, time.monotonic() + 100)
    assert result == {"errors": [], "attachments_deferred": 2}
    rows = db.db.execute("SELECT state,attempts FROM attachments").fetchall()
    assert [tuple(r) for r in rows] == [("pending", 0), ("pending", 0)]
    db.close()


def test_semantic_run_leaves_tail_reserve(tmp_path, monkeypatch):
    seen = {}
    _sem_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(semantic_drain, 'run_due', lambda ledger, cfg, result, deadline, **kw: (
        seen.__setitem__("deadline", deadline) or {"done": 1, "deferred": 0, "failed": 0}))
    db = _ledger(tmp_path)
    result = {"errors": []}
    deadline = time.monotonic() + 400
    run_check._run_semantic(db, _sem_args(), {}, result, deadline, True)
    # The shared size-based calculation must preserve the 60s minimum.
    assert seen["deadline"] <= deadline - 60
    assert seen["deadline"] == run_check._snapshot_work_deadline(db, deadline)
    db.close()


def test_snapshot_work_deadline_delegates_to_shared_helper(tmp_path):
    import snapshots
    db = _ledger(tmp_path)
    deadline = time.monotonic() + 400
    source_db = db.db.execute("PRAGMA database_list").fetchone()[2]
    assert run_check._snapshot_work_deadline(db, deadline) == (
        snapshots.snapshot_work_deadline(deadline, source_db))
    db.close()


def test_semantic_run_defers_when_size_reserve_exceeds_window(tmp_path, monkeypatch):
    """Large source DBs reserve more than the fixed floor: once the shared
    work deadline leaves under SEMANTIC_MIN_WORK_S the drain must not start,
    even though the raw run deadline still looks roomy."""
    import snapshots
    _sem_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(snapshots, "snapshot_verify_timeout_s", lambda path: 180.0)
    monkeypatch.setattr(semantic_drain, 'run_due',
                        lambda *a, **k: pytest.fail("must not dispatch"))
    db = _ledger(tmp_path)
    result = {"errors": []}
    # raw window 250s > old fixed 60s floor, but work window is 250-210=40s
    run_check._run_semantic(db, _sem_args(), {}, result,
                            time.monotonic() + 250, True)
    assert result["semantic"]["skipped"] == "no_budget"
    db.close()


def test_semantic_run_stops_dispatch_when_probe_reaches_work_deadline(tmp_path, monkeypatch):
    """A slot probe that consumes the work window must not start run_due."""
    clock = [1000.0]
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
    _sem_setup(monkeypatch, tmp_path)

    def busy(deadline):
        clock[0] = deadline
        return False
    monkeypatch.setattr(extract_llm, "pinned_slot_busy", busy)
    monkeypatch.setattr(semantic_drain, 'run_due',
                        lambda *a, **k: pytest.fail("must not dispatch"))
    db = _ledger(tmp_path)
    result = {"errors": []}
    run_check._run_semantic(db, _sem_args(), {}, result, clock[0] + 400, True)
    assert result["semantic"]["skipped"] == "no_budget"
    db.close()


def test_semantic_run_reports_error_when_source_db_unreadable(tmp_path, monkeypatch):
    """A failed source-DB probe skips the drain with an explicit error and
    never falls back to the full run deadline."""
    import sqlite3
    _sem_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(semantic_drain, 'run_due',
                        lambda *a, **k: pytest.fail("must not dispatch"))
    db = _ledger(tmp_path)

    class _FailingDB:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("synthetic")

        def close(self):
            pass
    monkeypatch.setattr(db, "db", _FailingDB())
    result = {"errors": []}
    run_check._run_semantic(db, _sem_args(), {}, result,
                            time.monotonic() + 400, True)
    assert result["errors"] == ["semantic: OperationalError"]
    assert result["semantic"]["skipped"] == "work_deadline_unavailable"
    db.close()


def test_semantic_run_skips_when_slot_never_frees(tmp_path, monkeypatch):
    """A busy realtime slot skips analysis without pausing background."""
    clock = [1000.0]
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
    _sem_setup(monkeypatch, tmp_path, busy=True)
    monkeypatch.setattr(run_check.time, "sleep",
                        lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(semantic_drain, 'run_due',
                        lambda *a, **k: pytest.fail("busy slot must not run"))
    db = _ledger(tmp_path)
    result = {"errors": []}
    run_check._run_semantic(db, _sem_args(), {}, result, clock[0] + 400, True)
    assert result["semantic"]["skipped"] == "slot_busy"
    assert result["semantic"]["mode"] == "off" and result["semantic"]["elapsed_s"] == 0.0
    db.close()


def test_semantic_run_off_keeps_overall_ok(tmp_path, monkeypatch):
    _sem_setup(monkeypatch, tmp_path)
    db = _ledger(tmp_path)
    result = {"errors": []}
    run_check._run_semantic(db, _sem_args(), {}, result, time.monotonic() + 480, False)
    assert health_report.build_health(
        db.db, result, "ok",
        cfg={"attachment_ocr": {"enabled": False}})["overall"] == "ok"
    db.close()


@pytest.mark.parametrize("outcome", ["done", "exception"])
def test_semantic_run_records_outcome_or_error(tmp_path, monkeypatch, outcome):
    """A crashing semantic lane surfaces through errors, not a state flag."""
    clock = [1000.0]
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
    _sem_setup(monkeypatch, tmp_path)

    def run_due(ledger, cfg, result, deadline, **kw):
        clock[0] = deadline
        if outcome == "exception":
            raise RuntimeError("boom")
        return {"done": 1, "deferred": 0, "failed": 0}
    monkeypatch.setattr(semantic_drain, 'run_due', run_due)
    db = _ledger(tmp_path)
    result = {"errors": []}
    run_check._run_semantic(db, _sem_args(), {}, result, clock[0] + 480, True)
    if outcome == "done":
        assert result["semantic"]["done"] == 1
    else:
        assert result["errors"] == ["semantic: RuntimeError"]
    db.close()


def test_unread_capped_patient_waits_for_history_then_acks(tmp_path):
    db = _ledger(tmp_path)
    marks = []
    oldest = {"id": 5}
    adapter = SimpleNamespace(
        list_unread=lambda: mcs_contract.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1)]),
        fetch_unread_messages=lambda *_: mcs_contract.MessageBatch(
            messages=[_message(mid=90, unread=True)], reached=True, capped=True),
        fetch_unread_replies=lambda *_: mcs_contract.ReplyBatch([], []),
        oldest_unread_id=lambda pid: oldest["id"],
        fetch_latest=lambda pid: {"message_id": 90, "is_self_only": False},
        mark_patient_read=lambda *a, **k: marks.append(a),
    )
    result = _capped_run(db, adapter)
    row = db.db.execute(
        "SELECT fetch_state,fetch_reason FROM patients WHERE project_id=1").fetchone()
    assert (row["fetch_state"], row["fetch_reason"]) == ("incomplete", "unread_capped")
    assert result["incomplete"] == [1] and marks == []
    assert db.history_job(1) is not None            # walk seeded once
    _capped_run(db, adapter)
    assert db.db.execute(
        "SELECT COUNT(*) FROM fetch_jobs WHERE kind='history' AND project_id=1"
    ).fetchone()[0] == 1

    # the history walk stores the oldest unread and certifies the floor
    db.save_messages([_message(mid=5, unread=True,
                               posted_at="2026-09-01T00:00:00+09:00")],
                     project_id=1)
    db.set_history_floor(1, 0)                        # -1: whole timeline
    db.set_coverage(1, db.high_watermark(1))          # the since=0 walk certifies it
    result = _capped_run(db, adapter)
    row = db.db.execute(
        "SELECT fetch_state FROM patients WHERE project_id=1").fetchone()
    assert row["fetch_state"] == "complete" and marks == [(1, 123)]
    assert result["incomplete"] == []

    # server says nothing is unread while the walk still shows the cap:
    # contradictory — stay incomplete rather than acknowledge
    oldest["id"] = None
    marks.clear()
    result = _capped_run(db, adapter)
    assert marks == [] and result["incomplete"] == [1]
    db.close()


def test_unread_cap_stuck_after_full_walk_is_reported_not_rewalked(tmp_path):
    """A completed since=0 walk (floor -1) that still cannot clear the
    cap must not be revived every tick — it is surfaced instead."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(1))
    db.save_messages([_message(mid=5, unread=True,
                               posted_at="2026-09-01T00:00:00+09:00")],
                     project_id=1)
    db.set_history_floor(1, 0)
    job = db.job_add("history", 1, payload={"since": 0, "page": 1, "pages": 10})
    db.job_done(job)
    marks = []
    adapter = SimpleNamespace(      # coverage never certified -> not cleared
        list_unread=lambda: mcs_contract.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1)]),
        fetch_unread_messages=lambda *_: mcs_contract.MessageBatch(
            messages=[_message(mid=90, unread=True)], reached=True, capped=True),
        fetch_unread_replies=lambda *_: mcs_contract.ReplyBatch([], []),
        oldest_unread_id=lambda pid: 5,
        mark_patient_read=lambda *a, **k: marks.append(a),
    )
    for _ in range(2):
        result = _capped_run(db, adapter)
        assert "project 1: unread_cap_stuck" in result["errors"]
        assert result["incomplete"] == [1] and marks == []
        assert db.job_state("history", 1) == "done"
    row = db.db.execute(
        "SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()
    assert row["fetch_reason"] == "unread_capped"
    db.close()


@pytest.mark.parametrize(("floor", "reply_pending", "stored", "covered", "cleared"), [
    (-1, False, "full", True, True),
    (0, False, "full", True, False),         # never floored
    (1_700_000_000, False, "full", True, True),   # floor at/below the oldest unread
    (1_800_000_000, False, "full", True, False),  # floor still above it
    (-1, True, "full", True, False),         # replies still pending
    (-1, False, None, True, False),          # oldest unread not stored
    (-1, False, "deleted", True, True),      # server-deleted anchor is terminal
    (-1, False, "snippet", True, False),     # truncated anchor is not
    (-1, False, "full", False, False),       # a newer row sits past verified coverage
])
def test_unread_cap_cleared_rule(tmp_path, floor, reply_pending, stored,
                                 covered, cleared):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(1))     # floor lives on the patient row
    if stored:
        db.save_messages([_message(mid=5, state=stored,
                                   posted_at="2026-09-01T00:00:00+09:00")],
                         project_id=1)   # ~1.788e9
    db.set_coverage(1, db.high_watermark(1))
    if not covered:
        db.save_messages([_message(mid=9, posted_at="2026-09-02T00:00:00+09:00")],
                         project_id=1)
    if floor:
        db.set_history_floor(1, floor if floor > 0 else 0)
    if reply_pending:
        db.job_add("reply", 1, 6, parent_id=5)
    assert db.unread_cap_cleared(1, 5) is cleared
    db.close()


def test_unread_route_error_acks_via_fallback_only_after_certification(tmp_path):
    """F-5: the unread route itself fails past the screen cap. The
    patient stays incomplete (walk seeded) until the history walk has
    certified down to the oldest unread; then it is acknowledged through
    the plain list read, never through the failing unread-filtered one.
    Nothing in that branch fetches the TOP of the range either: a post
    newer than the walk's last page stays unacknowledged until it is
    stored, since the plain read would clear it unfetched (and the walk
    would then import it read, never notified)."""
    db = _ledger(tmp_path)
    marks = []
    latest = {"id": 5}
    adapter = SimpleNamespace(
        list_unread=lambda: mcs_contract.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1)]),
        fetch_unread_messages=lambda *_: mcs_contract.MessageBatch(
            messages=[], error=mcs_contract.MCSError("http_error", "GET", status=400)),
        fetch_unread_replies=lambda *_: mcs_contract.ReplyBatch([], []),
        oldest_unread_id=lambda pid: 5,
        fetch_latest=lambda pid: {"message_id": latest["id"],
                                  "is_self_only": False},
        mark_patient_read=lambda *a, **k: marks.append((a, k)),
    )
    result = _capped_run(db, adapter)
    row = db.db.execute(
        "SELECT fetch_state,fetch_reason FROM patients WHERE project_id=1").fetchone()
    assert (row["fetch_state"], row["fetch_reason"]) == ("incomplete", "unread_capped")
    assert marks == [] and db.history_job(1) is not None and result["incomplete"] == [1]

    db.save_messages([_message(mid=5, unread=True,
                               posted_at="2026-09-01T00:00:00+09:00")], project_id=1)
    db.set_history_floor(1, 0)
    db.set_coverage(1, db.high_watermark(1))
    # the bottom is certified but a newer post (id 7) is not stored yet
    latest["id"] = 7
    result = _capped_run(db, adapter)
    assert marks == [] and result["incomplete"] == [1]
    row = db.db.execute(
        "SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()
    assert row["fetch_reason"] == "unread_capped"
    db.save_messages([_message(mid=7, unread=True,
                               posted_at="2026-09-02T00:00:00+09:00")], project_id=1)
    # stored by the unread path, not yet certified by a walk: still waits
    result = _capped_run(db, adapter)
    assert marks == [] and result["incomplete"] == [1]
    db.set_coverage(1, db.high_watermark(1))              # backfill certifies it
    result = _capped_run(db, adapter)
    assert marks == [((1, 123), {"fallback_plain": True})]
    assert result["marked_read"] == [1] and result["incomplete"] == []
    assert db.job_pending("history_head", 1) is None      # post-ack probe: no gap
    db.close()


@pytest.mark.parametrize("probe", ["newer_post", "probe_fails"])
def test_fallback_ack_seeds_head_walk_on_post_ack_gap(tmp_path, probe):
    """A post landing between the gate and the plain read would be
    cleared unfetched: the post-ack probe seeds a bounded head walk and
    records post_ack_gap. An unanswerable probe counts as a gap."""
    db = _certified_capped_db(tmp_path)
    seen = iter([5, 8])            # gate sees 5 stored; 8 lands before the re-probe

    def latest(pid):
        mid = next(seen)
        if mid == 8 and probe == "probe_fails":
            raise mcs_contract.MCSError("http_error", status=502)
        return {"message_id": mid, "is_self_only": False}
    marks = []
    adapter = SimpleNamespace(
        list_unread=lambda: mcs_contract.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1)]),
        fetch_unread_messages=lambda *_: mcs_contract.MessageBatch(
            messages=[], error=mcs_contract.MCSError("http_error", "GET", status=400)),
        fetch_unread_replies=lambda *_: mcs_contract.ReplyBatch([], []),
        oldest_unread_id=lambda pid: 5,
        fetch_latest=latest,
        mark_patient_read=lambda *a, **k: marks.append(a),
    )
    result = _capped_run(db, adapter)
    assert marks == [(1, 123)] and result["marked_read"] == [1]
    assert "mark 1: post_ack_gap" in result["errors"]
    job = db.job_pending("history_head", 1)
    assert json.loads(job["payload"]) == {
        "since": db.coverage_ts(1) - collection_stages.BACKFILL_OVERLAP_S, "page": 1,
        "pages": collection_stages.BACKFILL_MAX_PAGES, "trickle": False,
        "notify": True}
    db.close()


def test_unread_route_5xx_keeps_http_error_without_fallback(tmp_path):
    """Only a 4xx is the screen-cap rejection; a 5xx is an ordinary
    incomplete fetch — no cap gate, no full walk, no acknowledgement."""
    db = _certified_capped_db(tmp_path)
    marks = []

    def forbidden(*_):
        raise AssertionError("cap gate must not run for a 5xx")
    adapter = SimpleNamespace(
        list_unread=lambda: mcs_contract.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1)]),
        fetch_unread_messages=lambda *_: mcs_contract.MessageBatch(
            messages=[], error=mcs_contract.MCSError("http_error", "GET", status=503)),
        fetch_unread_replies=lambda *_: mcs_contract.ReplyBatch([], []),
        oldest_unread_id=forbidden, fetch_latest=forbidden,
        mark_patient_read=lambda *a, **k: marks.append(a),
    )
    result = _capped_run(db, adapter)
    row = db.db.execute(
        "SELECT fetch_state,fetch_reason FROM patients WHERE project_id=1").fetchone()
    assert (row["fetch_state"], row["fetch_reason"]) == ("incomplete", "http_error")
    assert marks == [] and result["incomplete"] == [1]
    assert db.history_job(1) is None
    db.close()


def test_semantic_run_lock_lost_is_raised_to_end_the_tick(tmp_path, monkeypatch):
    from writer_lock import RunLockLost
    _sem_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(semantic_drain, 'run_due', lambda *a, **k: {
        "done": 0, "deferred": 0, "failed": 0, "run_lock_lost": "timeout"})
    db = _ledger(tmp_path)
    with pytest.raises(RunLockLost) as e:
        run_check._run_semantic(db, _sem_args(), {}, {"errors": []},
                                time.monotonic() + 400, True)
    assert e.value.held is False
    db.close()


@pytest.mark.parametrize("state", ["unknown", "full", "deleted"])
def test_capped_unread_requires_every_parent_body_in_the_unread_time_range(tmp_path, state):
    with closing(_ledger(tmp_path)) as db:
        db.upsert_patient_info(_unread_patient(1))
        oldest = _message(mid=5, unread=True, posted_at="2026-09-01T00:00:00+09:00")
        recent = _message(mid=7, unread=True, state=state,
                          posted_at="2026-09-02T00:00:00+09:00")
        # Exercise the real history vehicle: unknown roots currently permit floor/coverage.
        db.job_add("history", 1, payload={"since": 0, "page": 1, "pages": 1})
        history = SimpleNamespace(fetch_history=lambda *_a, **_k:
                                  mcs_contract.MessageBatch([oldest, recent], pages=1, reached=True))
        job_ops.run_history_jobs(history, db, {"errors": []}, time.monotonic() + 300)
        assert db.history_floor(1) == -1 and db.coverage_ts(1) == db.high_watermark(1)
        # An old unknown post precedes the oldest unread; it must not hold today's ACK forever.
        db.save_messages([_message(mid=3, unread=False, state="unknown",
                                   posted_at="2026-08-01T00:00:00+09:00")], project_id=1)
        marks = []
        adapter = SimpleNamespace(
            list_unread=lambda: mcs_contract.UnreadSnapshot(123, [_unread_patient(1)]),
            fetch_unread_messages=lambda *_a: mcs_contract.MessageBatch(
                [], error=mcs_contract.MCSError("http_error", status=400)),
            fetch_unread_replies=lambda *_a: mcs_contract.ReplyBatch([], []),
            oldest_unread_id=lambda _pid: 5,
            fetch_latest=lambda _pid: {"message_id": 7, "is_self_only": False},
            mark_patient_read=lambda *a, **k: marks.append((a, k)))
        result = _capped_run(db, adapter)
        if state == "unknown":
            assert marks == [] and result["incomplete"] == [1]
            # A later body upgrade, unlike an old out-of-range unknown, releases the ACK.
            db.save_messages([_message(mid=7, unread=True,
                                       posted_at="2026-09-02T00:00:00+09:00")], project_id=1)
            result = _capped_run(db, adapter)
        assert marks == [((1, 123), {"fallback_plain": True})]
        assert result["marked_read"] == [1] and result["incomplete"] == []
