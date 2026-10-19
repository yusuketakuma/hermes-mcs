"""Synthetic regressions: a recovered stage replay records each walk entry once."""
import collection_stages
import health_report
import mcs_contract
import time

import mcs_adapter
from ingest_testkit import _iso, _ledger, _message, _msg_at, _unread_patient


def test_backfill_replay_records_each_coverage_gap_once(tmp_path):
    db = _ledger(tmp_path)
    for pid, mid in ((1, 100), (2, 200)):
        db.ensure_patient(pid)
        db.save_messages([_message(mid=mid, project_id=pid)])
        db.set_history_floor(pid, 0)
        db.set_coverage(pid, db.high_watermark(pid))
    items = [{"id": mid, "comment": "synthetic post",
              "created_at": "2026-09-19T01:00:00+09:00"}
             for mid in range(1, 33)]
    expired = []

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, **kwargs):
            start = (params["page"] - 1) * params["per_page"]
            return {"messages": items[start:start + params["per_page"]],
                    "paginate": {"has_next": start + params["per_page"] < len(items)}}

        def fetch_history(self, pid, since, **kw):
            if pid == 2:
                if not expired:
                    expired.append(1)
                    raise mcs_contract.SessionExpired(status=403)
                return mcs_contract.MessageBatch([], pages=1, reached=True)
            return super().fetch_history(pid, since, **kw)

        def auto_login(self, **kw):
            return "ok"

    adapter = Adapter()
    result = {"errors": [], "backfilled": 0, "incomplete": [], "run_id": 1}
    collection_stages.with_relogin(adapter, db, result, "backfill",
                            collection_stages.stage_backfill, adapter, db, result,
                            time.monotonic() + 300, 1)
    assert expired == [1]
    assert [g["pid"] for g in result["coverage_gaps"]] == [1]
    assert health_report.collection(result)["coverage_lagging"]["count"] == 1
    db.close()


def test_self_probe_replay_lists_each_fetched_patient_once(tmp_path):
    db = _ledger(tmp_path)
    for pid in (90, 91):
        db.upsert_patient_info(_unread_patient(pid))
        db.save_messages([_msg_at(pid * 10, pid, _iso(2), unread=False)])
    expired = []

    class Adapter:
        def fetch_latest(self, pid):
            if pid == 91 and not expired:
                expired.append(1)
                raise mcs_contract.SessionExpired(status=403)
            return {"message_id": pid * 10 + 5, "is_self_only": False}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            # page-limited walk that has not reached the probed id yet
            return mcs_contract.MessageBatch([], pages=1, reached=False)

        def auto_login(self, **kw):
            return "ok"

    adapter = Adapter()
    result = {"errors": [], "new_messages": 0, "run_id": 1}
    collection_stages.with_relogin(adapter, db, result, "self_probe",
                            collection_stages.stage_self_probe, adapter, db, result,
                            time.monotonic() + 300, 1)
    assert expired == [1]
    assert result["self_probe_fetched"] == [90, 91]
    db.close()


def test_unread_replay_keeps_records_of_patients_acked_before_expiry(tmp_path):
    """--mark-read: a patient acknowledged by the aborted attempt is no
    longer unread on replay, so its post_ack_gap and message count stay."""
    from types import SimpleNamespace
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(1))
    db.save_messages([_message(mid=5, unread=True,
                               posted_at="2026-09-01T00:00:00+09:00")],
                     project_id=1)
    db.set_history_floor(1, 0)
    db.set_coverage(1, db.high_watermark(1))
    marked, expired = set(), []
    latest = iter([5, 8])         # gate sees 5 stored; 8 lands before re-probe

    def fetch(pid, ts):
        if pid == 1:              # capped: plain-list acknowledgement
            return mcs_contract.MessageBatch(messages=[], error=mcs_contract.MCSError(
                "http_error", "GET", status=400))
        return mcs_contract.MessageBatch(
            messages=[_message(mid=pid * 100, project_id=pid, unread=True)],
            reached=True)

    def replies(m):
        if m.project_id == 2 and not expired:
            expired.append(1)
            raise mcs_contract.SessionExpired(status=403)
        return mcs_contract.ReplyBatch([], [])
    adapter = SimpleNamespace(
        auto_login=lambda **kw: "ok",
        list_unread=lambda: mcs_contract.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(p) for p in (3, 1, 2)
                                     if p not in marked]),
        fetch_unread_messages=fetch, fetch_unread_replies=replies,
        oldest_unread_id=lambda pid: 5,
        # strict-acked patients re-probe their stored newest id (no gap)
        fetch_latest=lambda pid: {
            "message_id": next(latest) if pid == 1 else pid * 100,
            "is_self_only": False},
        mark_patient_read=lambda pid, *a, **k: marked.add(pid))
    result = {"errors": [], "incomplete": [], "messages": 0,
              "new_messages": 0, "marked_read": [], "run_id": 7}
    collection_stages.with_relogin(
        adapter, db, result, "unread", collection_stages.stage_unread, adapter, db,
        result, time.monotonic() + 30,
        db.begin_run(None), mark_read=True)
    assert expired == [1]
    assert result["errors"] == ["mark 1: post_ack_gap"]
    assert result["marked_read"] == [3, 1, 2]
    assert result["messages"] == result["new_messages"] == 2
    assert "_unread_counted" not in result
    db.close()


def test_unread_replay_keeps_a_mark_failure_of_a_patient_no_longer_unread(tmp_path):
    """A mark whose response failed after the server applied it: the
    replay no longer lists the patient, so the failure must stay."""
    from types import SimpleNamespace
    db = _ledger(tmp_path)
    read_on_server, expired = set(), []

    def mark(pid, *a, **k):
        read_on_server.add(pid)
        if pid == 1:
            raise mcs_contract.MCSError("network_error", retryable=True)

    def replies(m):
        if m.project_id == 2 and not expired:
            expired.append(1)
            raise mcs_contract.SessionExpired(status=403)
        return mcs_contract.ReplyBatch([], [])
    adapter = SimpleNamespace(
        auto_login=lambda **kw: "ok",
        list_unread=lambda: mcs_contract.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(p) for p in (1, 2)
                                     if p not in read_on_server]),
        fetch_unread_messages=lambda pid, ts: mcs_contract.MessageBatch(
            messages=[_message(mid=pid, project_id=pid, unread=True)],
            reached=True),
        fetch_unread_replies=replies, mark_patient_read=mark,
        fetch_latest=lambda pid: {"message_id": pid, "is_self_only": False})
    result = {"errors": [], "incomplete": [], "messages": 0,
              "new_messages": 0, "marked_read": [], "run_id": 7}
    collection_stages.with_relogin(
        adapter, db, result, "unread", collection_stages.stage_unread, adapter, db,
        result, time.monotonic() + 30,
        db.begin_run(None), mark_read=True)
    assert expired == [1]
    assert result["errors"] == ["mark 1: network_error"]
    assert result["messages"] == 2 and result["marked_read"] == [2]
    db.close()


def test_backfill_replay_keeps_a_stalled_head_walk(tmp_path):
    """The aborted attempt's job_add revives the failed history_head, so
    the replay cannot see the stall again — it must stay recorded."""
    db = _ledger(tmp_path)
    now = int(time.time())
    for pid in (1, 2):
        db.ensure_patient(pid)
        db.save_messages([_msg_at(pid * 100, pid, _iso(0))], project_id=pid)
        db.set_history_floor(pid, 0)
        db.set_coverage(pid, now - 7200)        # lagging behind the head
    db.job_fail(db.job_add("history_head", 1, payload={"since": 0}),
                reason_code="window_stalled")
    expired = []

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            if pid == 2 and not expired:
                expired.append(1)
                raise mcs_contract.SessionExpired(status=403)
            return mcs_contract.MessageBatch([], pages=1, reached=False)

        def auto_login(self, **kw):
            return "ok"

    adapter = Adapter()
    result = {"errors": [], "backfilled": 0, "incomplete": [], "run_id": 1}
    collection_stages.with_relogin(adapter, db, result, "backfill",
                            collection_stages.stage_backfill, adapter, db, result,
                            time.monotonic() + 300, 1)
    assert expired == [1]
    assert result["errors"] == ["backfill 1: coverage_stalled_failed"]
    assert [g["pid"] for g in result["coverage_stalled"]] == [1]
    assert health_report.collection(result)["collection"] != "ok"
    db.close()



def test_nested_unread_recovery_shares_budget_and_keeps_committed_work(tmp_path):
    """List recovery and later patient expiry share a budget and durable saves."""
    import sqlite3
    from contextlib import closing

    db = _ledger(tmp_path)
    run_id = db.begin_run(None)
    calls = {"lists": 0, "reply_expired": False}
    logins, marked = [], []

    class Adapter:
        def list_unread(self):
            calls["lists"] += 1
            if calls["lists"] == 1:
                raise mcs_contract.SessionExpired(status=403)
            return mcs_contract.UnreadSnapshot(
                timestamp=123, patients=[_unread_patient(1), _unread_patient(2)])

        def fetch_unread_messages(self, pid, snapshot_ts):
            assert snapshot_ts == 123
            return mcs_contract.MessageBatch(
                [_message(mid=pid, project_id=pid, unread=True)], reached=True)

        def fetch_unread_replies(self, message):
            if message.project_id == 2 and not calls["reply_expired"]:
                calls["reply_expired"] = True
                raise mcs_contract.SessionExpired(status=403)
            return mcs_contract.ReplyBatch([], [])

        def auto_login(self, **kwargs):
            # A separate connection observes committed work, not this writer's
            # uncommitted rows. The second recovery follows the partial save.
            with closing(sqlite3.connect(tmp_path / "ledger.db")) as observer:
                logins.append((
                    observer.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                    observer.execute("SELECT COUNT(*) FROM notify_outbox"
                                     " WHERE kind='new_messages'").fetchone()[0],
                    observer.execute("SELECT snapshot_ts FROM runs WHERE run_id=?",
                                     (run_id,)).fetchone()[0]))
            return "ok"

        def mark_patient_read(self, pid, snapshot_ts, **kwargs):
            marked.append((pid, snapshot_ts))

        def fetch_latest(self, pid):
            return {"message_id": pid}

    adapter = Adapter()
    result = {"errors": [], "incomplete": [], "messages": 0,
              "new_messages": 0, "marked_read": [], "run_id": run_id}
    try:
        collection_stages.with_relogin(
            adapter, db, result, "unread", collection_stages.stage_unread,
            adapter, db, result, time.monotonic() + 30, run_id, mark_read=True)
        assert calls == {"lists": 3, "reply_expired": True}
        assert logins[0][:2] == (0, 0)
        assert logins[1] == (2, 2, 123)
        assert marked == [(1, 123), (2, 123)]
        assert result["errors"] == result["incomplete"] == []
        assert result["messages"] == result["new_messages"] == 2
        assert [a["state"] for a in result["relogin_attempts"]] == ["ok", "ok"]
        assert "_unread_counted" not in result
    finally:
        db.close()
