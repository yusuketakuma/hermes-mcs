"""Daily source-summary refresh uses only synthetic ledgers and GET stubs."""
import pytest

import ledger
import run_check
from test_run_check_stages import _karte_db, _MemoAdapter, _run_stage


@pytest.fixture
def world(tmp_path, monkeypatch):
    clock = [1790000000.0]
    monkeypatch.setattr(ledger.time, "time", lambda: clock[0])
    db = _karte_db(tmp_path, 16, artifact=True)
    yield db, clock
    db.close()


def test_deep_refreshes_at_daily_boundary_without_new_chat(world):
    db, clock = world
    adapter = _MemoAdapter()
    clock[0] += ledger.KARTE_SUMMARY_REFRESH_S - 1
    assert _run_stage(adapter, db, jobs_only=True)["karte_summary"]["fetched"] == 0
    clock[0] += 1
    assert _run_stage(adapter, db)["karte_summary"]["fetched"] == 0
    first = _run_stage(adapter, db, jobs_only=True)["karte_summary"]
    assert first["fetched"] == 10 and adapter.calls == list(range(10, 101, 10))
    assert _run_stage(adapter, db, jobs_only=True)["karte_summary"]["fetched"] == 6
    assert adapter.calls == list(range(10, 161, 10))
    assert _run_stage(adapter, db, jobs_only=True)["karte_summary"]["fetched"] == 0
    assert all(db.karte_summary_current(pid)["fetched_at"] == clock[0]
               for pid in range(1, 17))
    assert adapter.marked == []


def test_deep_prioritizes_missing_within_ten_background_targets(world):
    db, clock = world
    with db.db:
        db.db.execute("DELETE FROM artifacts WHERE kind='karte_summary' AND project_id IN (15,16)")
    clock[0] += ledger.KARTE_SUMMARY_REFRESH_S
    adapter = _MemoAdapter()
    result = _run_stage(adapter, db, jobs_only=True)["karte_summary"]
    assert result["fetched"] == 10
    assert adapter.calls == [150, 160, *range(10, 81, 10)]


def test_due_fresh_chats_keep_priority_and_total_cap(world):
    db, clock = world
    clock[0] += ledger.KARTE_SUMMARY_REFRESH_S
    adapter = _MemoAdapter()
    result = _run_stage(adapter, db, targets=[14, 15, 16], jobs_only=True)["karte_summary"]
    assert result["fetched"] == run_check.KARTE_SUMMARY_TICK_CAP == 12
    assert adapter.calls[:3] == [160, 150, 140]
    assert result["deferred"] == 1 and db.karte_summary_due() == [10]


def test_archived_and_unknown_karte_never_refresh_even_when_flagged(world):
    db, clock = world
    with db.db:
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
        db.db.execute("UPDATE patients SET karte_id=NULL WHERE project_id=2")
    db.karte_summary_mark_due(1)
    db.karte_summary_mark_due(2)
    clock[0] += ledger.KARTE_SUMMARY_REFRESH_S
    assert db.karte_summary_stale(20) == list(range(3, 17))
    assert db.karte_summary_due() == []
    adapter = _MemoAdapter()
    _run_stage(adapter, db, jobs_only=True)
    assert 10 not in adapter.calls and 20 not in adapter.calls


@pytest.mark.parametrize("permanent", [False, True])
def test_failed_refresh_keeps_success_and_backoff_before_retry(world, permanent):
    db, clock = world
    payload = {"comment": "合成の最後の成功記録", "updated_at": "",
               "is_editable": True, "user": {}}
    db.karte_summary_store(1, 10, payload)
    with db.db:
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id>1")
    clock[0] += ledger.KARTE_SUMMARY_REFRESH_S
    before = db.karte_summary_current(1)
    adapter = _MemoAdapter(**({"gone": {10}} if permanent else {"fail": {10}}))
    result = _run_stage(adapter, db, jobs_only=True)["karte_summary"]
    assert result["errors"] == [{"project": 1, "kind": "http_error"}]
    assert result["fetched"] == 0 and db.karte_summary_current(1) == before
    assert db.karte_summary_stale(10) == []
    clock[0] += ledger.KARTE_SUMMARY_BACKOFF_S + 1
    assert db.karte_summary_stale(10) == [1]
    fixed = _MemoAdapter()
    assert _run_stage(fixed, db, jobs_only=True)["karte_summary"]["fetched"] == 1
    assert db.karte_summary_current(1)["comment"] == "合成 10"


def test_unchanged_empty_refresh_advances_fetched_at_without_new_artifact(world):
    db, clock = world
    count = len(db.artifacts("karte_summary", project_id=1))
    clock[0] += ledger.KARTE_SUMMARY_REFRESH_S
    adapter = _MemoAdapter(empty={10})
    _run_stage(adapter, db, jobs_only=True)
    assert len(db.artifacts("karte_summary", project_id=1)) == count
    assert db.karte_summary_current(1)["empty"] is True
    assert db.karte_summary_current(1)["fetched_at"] == clock[0]


@pytest.mark.parametrize("meta", ["[", "{}", '{"fetched_at":"unknown"}',
                                  '{"fetched_at":true}', '{"fetched_at":1790100000}'])
def test_unknown_or_future_fetch_time_is_refreshed_without_query_failure(world, meta):
    db, _ = world
    with db.db:
        db.db.execute("UPDATE artifacts SET meta=? WHERE kind='karte_summary' AND project_id=1",
                      (meta,))
    assert db.karte_summary_stale(10) == [1]
    adapter = _MemoAdapter()
    assert _run_stage(adapter, db, jobs_only=True)["karte_summary"]["fetched"] == 1


def test_refresh_margin_defers_with_existing_durable_due_flag(world):
    db, clock = world
    clock[0] += ledger.KARTE_SUMMARY_REFRESH_S
    adapter = _MemoAdapter()
    result = _run_stage(adapter, db, jobs_only=True,
                        deadline=run_check.time.monotonic() + 1)["karte_summary"]
    assert result["fetched"] == 0 and result["deferred"] == 10
    assert adapter.calls == []
    assert sorted(db.karte_summary_due()) == list(range(1, 11))
