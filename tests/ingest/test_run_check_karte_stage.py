"""Patient memo-summary refresh cadence, prioritisation and expiry contracts."""
import collection_stages
import mcs_contract
import time
import pytest
import job_ops
import ledger
from ingest_testkit import _unread_patient, _msg_at, _iso
from run_check_stage_testkit import _MemoAdapter, _karte_db, _run_stage
from run_check_stage_testkit import _no_disk_guard as _no_disk_guard


def test_karte_summary_tick_fetches_only_projects_with_new_messages(tmp_path):
    db = _karte_db(tmp_path, 5)
    db.upsert_patient_info(_unread_patient(9))                   # no karte_id
    adapter = _MemoAdapter(empty={30})
    result = _run_stage(adapter, db, targets=[3, 1, 9, 3])

    assert adapter.calls == [30, 10]                             # dedup, newest first, no id -> no GET
    assert result["karte_summary"] == {
        "fetched": 2, "stored": 2, "empty": 1, "skipped": 0, "deferred": 0,
        "errors": []}
    assert db.karte_summary_current(3)["empty"] is True
    assert db.karte_summary_current(1)["comment"] == "合成 10"
    assert db.karte_summary_current(2) is None                   # untouched
    assert result["errors"] == []
    assert db.karte_summary_due() == []                          # cleared by store
    assert _run_stage(adapter, db)["karte_summary"]["fetched"] == 0
    db.close()


def test_karte_summary_deep_run_fills_ten_oldest_missing(tmp_path):
    db = _karte_db(tmp_path, 14)
    db.karte_summary_store(1, 10, None)                          # already known
    adapter = _MemoAdapter()
    _run_stage(adapter, db)                                      # tick: nothing
    assert adapter.calls == []
    result = _run_stage(adapter, db, jobs_only=True)
    assert adapter.calls == [pid * 10 for pid in range(2, 12)]   # 10 oldest
    assert result["karte_summary"]["fetched"] == 10
    assert db.karte_summary_missing(20) == [12, 13, 14]
    db.close()


@pytest.mark.parametrize("artifact", [False, True])
def test_karte_summary_cap_twelve_and_carry_over(tmp_path, artifact):
    # artifact=False is the rollout case: none of the 13 projects has
    # ever been fetched, and the 13th must still be fetched on the NEXT
    # normal tick, not on a later deep-run fill slot
    db = _karte_db(tmp_path, 13, artifact=artifact)
    adapter = _MemoAdapter()
    result = _run_stage(adapter, db, targets=range(1, 14))

    assert len(adapter.calls) == collection_stages.KARTE_SUMMARY_TICK_CAP == 12
    assert adapter.calls[0] == 130                               # newest first
    assert result["karte_summary"]["deferred"] == 1
    assert result["karte_summary"]["fetched"] == 12
    assert db.karte_summary_due() == [1]                         # durable carry-over
    # next tick: only the deferred oldest is fetched, nothing refreshed
    # this tick is fetched again
    result = _run_stage(adapter, db)
    assert adapter.calls[12:] == [10]
    assert result["karte_summary"]["fetched"] == 1
    assert db.karte_summary_due() == []
    assert _run_stage(adapter, db)["karte_summary"]["fetched"] == 0
    db.close()


def test_karte_summary_deferral_rewrites_only_rows_not_already_due(tmp_path, monkeypatch):
    # past the cap a flagged project stays due=1 untouched, while a deep-run
    # fill target (missing/stale, never flagged) is flagged for the next tick
    db = _karte_db(tmp_path, 20)
    for pid in range(8, 21):                                     # 13 flagged: one over the cap
        db.karte_summary_mark_due(pid)
    due = db.karte_summary_due()
    fill = list(dict.fromkeys(db.karte_summary_missing(collection_stages.KARTE_SUMMARY_FILL)))
    rewrites = []
    real = db.karte_summary_mark_due
    monkeypatch.setattr(db, "karte_summary_mark_due",
                        lambda pid, due=True: rewrites.append(pid) or real(pid, due))
    result = _run_stage(_MemoAdapter(), db, jobs_only=True)
    expected = [pid for pid in fill if pid not in due]
    assert result["karte_summary"]["fetched"] == collection_stages.KARTE_SUMMARY_TICK_CAP
    assert result["karte_summary"]["deferred"] == 1 + len(expected)
    assert expected and rewrites == expected                     # never the already-due 8
    assert set(db.karte_summary_due()) == {8, *expected}
    db.close()


@pytest.mark.parametrize("left", [-1, collection_stages.KARTE_SUMMARY_MARGIN_S / 2])
def test_karte_summary_deadline_margin_stops_the_loop(tmp_path, left):
    # the stage keeps KARTE_SUMMARY_MARGIN_S of the run for the tail
    # (semantic lane, finish) like its sibling stages, so a tick that
    # is already inside the margin does not spend it on GETs
    db = _karte_db(tmp_path, 3)
    adapter = _MemoAdapter()
    result = _run_stage(adapter, db, targets=[1, 2, 3],
                        deadline=time.monotonic() + left)
    assert adapter.calls == []
    assert result["karte_summary"]["deferred"] == 3
    assert db.karte_summary_due() == [3, 2, 1]                   # retried next run
    db.close()


def test_karte_summary_fresh_project_beats_persistently_failing_backlog(tmp_path):
    # 12 older due projects whose karte GET 404s (non-retryable) must not
    # push a project that just stored chat past the per-tick cap
    db = _karte_db(tmp_path, 13)
    adapter = _MemoAdapter(gone={pid * 10 for pid in range(1, 13)})
    result = _run_stage(adapter, db, targets=range(1, 14))
    assert adapter.calls[0] == 130                               # fresh wins the cap
    assert result["karte_summary"]["fetched"] == 1
    assert result["karte_summary"]["deferred"] == 1              # 12 dead + fresh > cap
    assert len(result["karte_summary"]["errors"]) == 11
    # a 4xx is not carried over (the flag re-arms on the next new
    # message); only the never-reached oldest one still waits
    assert db.karte_summary_due() == [1]
    # the failed ones sit out the backoff even when re-flagged, so the
    # next ticks spend no GET on them
    result = _run_stage(adapter, db, targets=range(1, 14))
    assert adapter.calls[12:] == [130, 10]
    result = _run_stage(adapter, db, targets=range(1, 14))
    assert adapter.calls[14:] == [130]
    assert db.karte_summary_current(13)["comment"] == "合成 130"
    db.close()


def test_karte_summary_deep_fill_skips_archived_projects(tmp_path):
    db = _karte_db(tmp_path, 3)
    db.upsert_patient_info(_unread_patient(2), is_archived=True)
    db.db.execute("UPDATE patients SET last_seen=project_id")
    db.db.commit()
    adapter = _MemoAdapter()
    _run_stage(adapter, db, jobs_only=True)
    assert adapter.calls == [10, 30]
    assert db.karte_summary_missing(10) == []
    db.close()


def test_karte_summary_project_error_is_recorded_not_partial(tmp_path):
    db = _karte_db(tmp_path, 3)
    adapter = _MemoAdapter(fail={20})
    result = _run_stage(adapter, db, targets=[1, 2, 3])

    assert adapter.calls == [30, 20, 10]                         # loop continues
    assert result["karte_summary"]["errors"] == [{"project": 2, "kind": "http_error"}]
    assert result["karte_summary"]["fetched"] == 2
    assert result["errors"] == [] and result["incomplete"] == []
    assert adapter.marked == []
    assert db.karte_summary_current(2) is None
    # a retryable (5xx) failure keeps the flag, but the project sits
    # out the backoff before it is retried
    assert db.db.execute("SELECT karte_summary_due FROM patients "
                         "WHERE project_id=2").fetchone()[0] == 1
    assert db.karte_summary_due() == []
    db.db.execute("UPDATE patients SET karte_summary_failed_at=? WHERE project_id=2",
                  (time.time() - ledger.KARTE_SUMMARY_BACKOFF_S - 1,))
    db.db.commit()
    assert db.karte_summary_due() == [2]
    adapter.fail.clear()
    assert _run_stage(adapter, db)["karte_summary"]["fetched"] == 1
    assert adapter.calls[3:] == [20] and db.karte_summary_due() == []
    assert db.db.execute("SELECT karte_summary_failed_at FROM patients "
                         "WHERE project_id=2").fetchone()[0] is None
    db.close()


def test_karte_summary_session_expired_propagates(tmp_path):
    db = _karte_db(tmp_path, 1)

    class Adapter(_MemoAdapter):
        def fetch_memo_summary(self, karte_id):
            raise mcs_contract.SessionExpired("gone")

        def _probe_session_ok(self):
            return False

    with pytest.raises(mcs_contract.SessionExpired):
        _run_stage(Adapter(), db, targets=[1])
    assert db.karte_summary_due() == [1]                         # still due after relogin
    db.close()


def test_karte_summary_expiry_on_one_karte_with_live_session_is_a_karte_failure(tmp_path):
    db = _karte_db(tmp_path, 3)

    class Adapter(_MemoAdapter):
        def fetch_memo_summary(self, karte_id):
            if karte_id == 20:
                self.calls.append(karte_id)
                raise mcs_contract.SessionExpired("403")
            return super().fetch_memo_summary(karte_id)

        def _probe_session_ok(self):
            return True

    adapter = Adapter()
    result = _run_stage(adapter, db, targets=[1, 2, 3])
    assert adapter.calls == [30, 20, 10]                         # loop continues
    assert result["karte_summary"]["errors"] == [
        {"project": 2, "kind": "session_expired"}]
    assert result["karte_summary"]["fetched"] == 2
    assert db.karte_summary_due() == []                          # non-retryable: cleared
    # the failed karte is not re-picked by the deep-run fill either
    assert db.karte_summary_missing(10) == []
    db.close()


def test_new_message_paths_mark_karte_summary_due(tmp_path):
    db = _karte_db(tmp_path, 2)
    db.upsert_patient_info(_unread_patient(9))                   # karte_id unknown
    db.karte_summary_mark_due(9)
    assert db.karte_summary_due() == []                          # waits for karte_id
    # reply job path: a stored reply flags its project
    db.save_messages([_msg_at(10, 1, _iso(2))])
    db.job_add("reply", 1, 11, parent_id=10)

    class ReplyAdapter:
        def fetch_thread(self, pid, parent_id):
            return [_msg_at(11, pid, _iso(1))]
        def fetch_thread_window(self, pid, parent_id, start_page=1):
            return mcs_contract.MessageBatch(self.fetch_thread(pid, parent_id), pages=1, reached=True)

    result = {"errors": [], "incomplete": []}
    job_ops.run_reply_jobs(ReplyAdapter(), db, result, time.monotonic() + 300)
    assert db.karte_summary_due() == [1]
    # self probe path: a stored own post flags its project
    db.save_messages([_msg_at(100, 2, _iso(1), unread=False)])
    own = _msg_at(101, 2, _iso(0), unread=False)

    class Adapter:
        def fetch_latest(self, pid):
            return {"message_id": 101 if pid == 2 else None,
                    "is_self_only": True}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_contract.MessageBatch([own], pages=1, reached=True)

    result = {"errors": [], "new_messages": 0}
    collection_stages.stage_self_probe(Adapter(), db, result,
                               time.monotonic() + 300, run_id=1)
    assert sorted(db.karte_summary_due()) == [1, 2]
    db.close()
