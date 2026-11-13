"""Synthetic: self_probe and backfill visit at most WALK_TICK_CAP patients per
tick, rotate on their walk cursors, and backfill follows the probe's window."""
import time
from types import SimpleNamespace

import pytest

import collection_stages
import mcs_contract
import run_check
from ingest_testkit import _ledger, _message

ROTATION = [[1, 2, 3], [4, 5, 6], [7, 1, 2]]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(collection_stages, "WALK_TICK_CAP", 3)
    store = _ledger(tmp_path)
    for pid in range(1, 8):
        store.ensure_patient(pid)
        store.save_messages([_message(mid=pid * 10, project_id=pid)])
        store.set_coverage(pid, store.high_watermark(pid))
    yield store
    store.close()


def _adapter(visited):
    def latest(pid):
        visited.append(("probe", pid))
        return {"message_id": pid * 10, "is_self_only": False}   # already stored

    def history(pid, since, **kw):
        visited.append(("backfill", pid))
        return mcs_contract.MessageBatch([], pages=1, reached=True)
    return SimpleNamespace(fetch_latest=latest, fetch_history=history)


@pytest.mark.parametrize("stage,kind", [
    (collection_stages.stage_self_probe, "probe"),
    (collection_stages.stage_backfill, "backfill")])
def test_each_tick_visits_the_cap_and_every_patient_within_ceil_n_over_cap(db, stage, kind):
    visited = []
    ticks = []
    for run_id in (1, 2, 3):
        visited.clear()
        result = {"errors": [], "new_messages": 0, "backfilled": 0}
        stage(_adapter(visited), db, result, time.monotonic() + 300, run_id)
        assert result["errors"] == []   # the cap is not a deadline failure
        ticks.append([pid for k, pid in visited if k == kind])
    assert ticks == ROTATION


def test_backfill_walks_the_probe_window_and_keeps_its_own_cursor(db, monkeypatch):
    """Backfill stores own/other-read posts silently, so it must never reach
    a patient before this tick's probe: with self_posts on it walks the
    probe's window instead of its own cursor."""
    visited = []
    monkeypatch.setattr(collection_stages, "stage_unread", lambda *a, **k: None)
    monkeypatch.setattr(collection_stages, "outage_since", lambda: None)
    args = SimpleNamespace(mark_read=False, jobs_only=False, no_backfill=False)
    ticks = []
    for run_id in (1, 2, 3):
        visited.clear()
        result = {"errors": [], "new_messages": 0, "backfilled": 0, "run_id": run_id}
        run_check._stage_fetch(_adapter(visited), db, args, {"self_posts": True}, result,
                               time.monotonic() + 300, run_id, False, 86400)
        ticks.append(visited[:])
    assert ticks == [[("probe", p) for p in t] + [("backfill", p) for p in t] for t in ROTATION]
    assert collection_stages.walk_order(db, "backfill")[0]["project_id"] == 1
