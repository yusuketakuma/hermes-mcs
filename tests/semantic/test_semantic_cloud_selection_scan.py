"""Synthetic cloud selection scan bounds, new arrivals and progress expiry contracts."""
import time
from types import SimpleNamespace
import pytest
import cloud_inference
import semantic_drain
import semantic_due_selection
from semantic_testkit import _message
from semantic_cloud_testkit import cloud as cloud, snapshot as snapshot
from test_cloud_response_recovery import legacy_cloud_routes


def _seed(led, mids):
    for mid in mids:
        led.save_messages([_message(mid, body="完全合成の過去記録。")])
        led.semantic_seed(1, [mid], {'source': 'history_import'})


def _job_id(led, mid):
    return led.db.execute("SELECT job_id FROM fetch_jobs WHERE kind='semantic' AND message_id=?",
                          (mid,)).fetchone()[0]


def _clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(semantic_due_selection, 'time', semantic_drain.time)
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)
    monkeypatch.setattr(semantic_due_selection, '_cloud_selection_progress', None)
    return clock


def _select(led, deadline, max_jobs=1):
    with legacy_cloud_routes():
        return semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, max_jobs,
                                                deadline=deadline)[0]


def test_cloud_selection_scan_skips_finished_and_other_kind_rows(cloud, monkeypatch):
    led, _, job, calls = cloud
    _seed(led, [2, 3])
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET state='done' WHERE job_id=?", (job['job_id'],))
    snapshot(led)
    monkeypatch.setattr(semantic_due_selection, '_CLOUD_SELECTION_PAGE', 1)
    clock = _clock(monkeypatch)
    led.db.set_trace_callback(lambda sql: clock.__setitem__(0, 1.0)
        if sql.startswith('SELECT j.job_id,j.kind') else None)
    try:
        with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='deadline'):
            _select(led, 1.0)
    finally:
        led.db.set_trace_callback(None)
    # The single adopted page holds the first pending semantic job, never the done one.
    ranked = [rank[-1] for rank in semantic_due_selection._cloud_selection_progress['rows']]
    assert ranked == [_job_id(led, 2)]
    assert not calls


def test_cloud_selection_retained_progress_scans_new_arrivals(cloud, monkeypatch):
    led, _, job, calls = cloud
    # The history scope already covers message 2; only its job arrives later,
    # so the retained progress keeps the same binding.
    _seed(led, [2])
    snapshot(led)
    with led.db:
        led.db.execute("DELETE FROM fetch_jobs WHERE job_id=?", (_job_id(led, 2),))
    clock = _clock(monkeypatch)
    first = _select(led, 10.0)
    assert [row['job_id'] for row in first] == [job['job_id']]
    binding = semantic_due_selection._cloud_selection_progress['binding']
    led.semantic_seed(1, [2], {'source': 'history_import'})
    assert _job_id(led, 2) > job['job_id']
    later = _select(led, 10.0, max_jobs=2)
    assert _job_id(led, 2) in [row['job_id'] for row in later]
    assert semantic_due_selection._cloud_selection_progress['binding'] == binding
    assert clock[0] == 0.0 and not calls


@pytest.mark.parametrize('finished', ['done', 'failed'])
def test_cloud_selection_progress_expires_to_pick_requeued_rows(cloud, monkeypatch, finished):
    led, _, job, calls = cloud
    _seed(led, [2])
    requeued = _job_id(led, 2)
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET state=? WHERE job_id=?", (finished, requeued))
    snapshot(led)
    clock = _clock(monkeypatch)
    assert [row['job_id'] for row in _select(led, 10.0)] == [job['job_id']]
    assert semantic_due_selection._cloud_selection_progress is not None
    # ON CONFLICT re-queue (done) or automatic retry (failed) below the cursor.
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET state='pending' WHERE job_id=?", (requeued,))
    assert requeued not in [row['job_id'] for row in _select(led, 10.0, max_jobs=2)]
    clock[0] = semantic_due_selection._CLOUD_SELECTION_TTL_S
    assert requeued in [row['job_id'] for row in _select(led, clock[0] + 10.0, max_jobs=2)]
    assert not calls


def test_cloud_selection_long_full_scan_does_not_expire(cloud, monkeypatch):
    led, _, job, calls = cloud
    _seed(led, [2, 3])
    snapshot(led)
    monkeypatch.setattr(semantic_due_selection, '_CLOUD_SELECTION_PAGE', 1)
    clock = _clock(monkeypatch)
    # Every page takes a whole TTL and each tick's deadline stops after one page.
    led.db.set_trace_callback(lambda sql: clock.__setitem__(0, clock[0] + semantic_due_selection._CLOUD_SELECTION_TTL_S)
        if sql.startswith('SELECT j.job_id,j.kind') else None)
    try:
        progress, ticks = None, 0
        while True:
            ticks += 1
            assert ticks < 10
            try:
                selected = _select(led, clock[0] + 1.0)
                break
            except semantic_drain._CloudSelectionIncomplete:
                current = semantic_due_selection._cloud_selection_progress
                assert progress is None or current is progress
                progress = current
    finally:
        led.db.set_trace_callback(None)
    assert ticks > 2
    assert {row['job_id'] for row in selected} == {job['job_id'], _job_id(led, 2), _job_id(led, 3)}
    assert not calls
