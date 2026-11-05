"""Synthetic cloud selection progress priority contracts with isolated shared fixtures."""
import extract_llm_carry
import extract_llm_context
import extract_llm_engine
import extract_llm_errors
import extract_llm_prepare
import extract_llm_provider
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
import json
import sqlite3
import time
from types import SimpleNamespace
import pytest
import cloud_policy
import extract_llm
import cloud_inference
from semantic_testkit import _message, _patient
from test_anthropic_runtime import _assert_legacy_retained as _assert_legacy_retained, _current_artifacts as _current_artifacts, old as old, routed as routed
from cloud_selection_progress_testkit import _claim_count as _claim_count, _cloud_pass as _cloud_pass, _complete_cloud_tx as _complete_cloud_tx, _select as _select, blocked_prefix as blocked_prefix, cloud_candidates as cloud_candidates


@pytest.mark.parametrize('oldest_first', [False, True])
def test_cloud_priority_walk_matches_fixed_snapshot_order(cloud_candidates, oldest_first):
    led, cfg, *_ = cloud_candidates
    cfg['cloud_llm']['project_ids'] = [1, 2, 3]
    for pid in (2, 3):
        _patient(led, pid)
        for mid in (100 + pid * 10, 101 + pid * 10):
            led.save_messages([_message(mid, pid=pid, body='完全合成の過去記録。')])
    with led.db:
        led.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        led.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        led.db.execute('UPDATE messages SET posted_at_ts=NULL WHERE message_id IN (1,120,130,131)')
        led.db.execute('UPDATE messages SET posted_at_ts=17 WHERE message_id IN (2,3,121)')
        led.db.execute('UPDATE patients SET is_archived=1 WHERE project_id=3')
        cloud_policy.capture_history_tx(led.db, 'synthetic-order', time.time())
    args = (led, 100, None, None, oldest_first)
    kwargs = {'cloud_only': True, 'prioritize_patients': True}
    expected = extract_llm._select_pending(*args, **kwargs)[0]
    actual = extract_llm._select_pending(*args, **kwargs, deadline=time.monotonic() + 120)[0]
    assert [r['message_id'] for r in actual] == [r['message_id'] for r in expected]
    assert len(actual) == 68


def test_cloud_priority_retains_patient_order_without_retaining_all_message_metadata(cloud_candidates):
    led, *_ = cloud_candidates
    for mid in range(65, 513):
        old(led, mid)
    with led.db:
        led.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        led.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        cloud_policy.capture_history_tx(led.db, 'synthetic-bounded-order', time.time())
    rows, _ = extract_llm._select_pending(led, 4, None, None, False, cloud_only=True,
        prioritize_patients=True, deadline=time.monotonic() + 120)
    assert len(rows) == len({r['message_id'] for r in rows}) == 4
    retained_bytes = len(json.dumps(led._cloud_priority_progress).encode())
    assert retained_bytes < 2048, retained_bytes


def test_cloud_priority_walk_resumes_across_deadlines_and_unrelated_writes(cloud_candidates, monkeypatch):
    led, _, *_ = cloud_candidates
    expected = [row['message_id'] for row in _select(led, 'extract')]
    clock = [time.monotonic()]
    real_connect = sqlite3.connect
    scans = []

    def connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        def trace(sql):
            if sql.startswith('SELECT message_id,project_id,posted_at_ts FROM messages'):
                scans.append(sql)
                clock[0] += 0.4
        db.set_trace_callback(trace)
        return db

    monkeypatch.setattr(extract_llm.sqlite3, 'connect', connect)
    monkeypatch.setattr(extract_llm, '_CLOUD_PRIORITY_PAGE', 8)
    monkeypatch.setattr(extract_llm_selection, '_CLOUD_PRIORITY_PAGE', extract_llm._CLOUD_PRIORITY_PAGE)
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(extract_llm_carry, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_context, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_engine, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_errors, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_prepare, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_provider, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_resident, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_runner, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_selection, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_source_contract, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_store, 'time', extract_llm.time)
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    positions, observed = [], []
    for _ in range(200):
        try:
            actual = _select(led, 'extract', deadline=clock[0] + 1)
        except extract_llm._CloudSelectionIncomplete as error:
            assert str(error) == 'deadline'
            positions.append(led._cloud_priority_progress['after'])
            led.artifact_add('synthetic_unrelated_observation', '{}')
            clock[0] += 1
        else:
            with led.db:
                led.db.execute('BEGIN IMMEDIATE')
                for row in actual:
                    observed.append(row['message_id'])
                    _complete_cloud_tx(led, row)
            if observed == expected:
                break
            clock[0] += 1
    else:
        pytest.fail('bounded walk did not reach its finite highwater')
    assert positions == sorted(set(positions)) and positions[-1] >= 56
    assert len(scans) > 2 and observed == expected and len(observed) == len(set(observed))


@pytest.mark.parametrize('change', ['scope', 'config', 'db'])
def test_cloud_priority_scope_change_invalidates_the_saved_walk(cloud_candidates, monkeypatch, change):
    led, cfg, *_ = cloud_candidates
    clock = [time.monotonic()]
    real_connect = sqlite3.connect
    def connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        def trace(sql):
            if sql.startswith('SELECT message_id,project_id,posted_at_ts FROM messages'):
                clock[0] += 0.4
        db.set_trace_callback(trace)
        return db
    monkeypatch.setattr(extract_llm.sqlite3, 'connect', connect)
    monkeypatch.setattr(extract_llm, '_CLOUD_PRIORITY_PAGE', 8)
    monkeypatch.setattr(extract_llm_selection, '_CLOUD_PRIORITY_PAGE', extract_llm._CLOUD_PRIORITY_PAGE)
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(extract_llm_carry, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_context, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_engine, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_errors, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_prepare, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_provider, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_resident, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_runner, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_selection, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_source_contract, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_store, 'time', extract_llm.time)
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    inode = [1]
    if change == 'db':
        monkeypatch.setattr(extract_llm, 'os', SimpleNamespace(stat=lambda _: SimpleNamespace(st_dev=1, st_ino=inode[0])))
        monkeypatch.setattr(extract_llm_errors, 'os', extract_llm.os)
        monkeypatch.setattr(extract_llm_prepare, 'os', extract_llm.os)
        monkeypatch.setattr(extract_llm_resident, 'os', extract_llm.os)
        monkeypatch.setattr(extract_llm_runner, 'os', extract_llm.os)
        monkeypatch.setattr(extract_llm_selection, 'os', extract_llm.os)
        monkeypatch.setattr(extract_llm_source_contract, 'os', extract_llm.os)
        monkeypatch.setattr(extract_llm_store, 'os', extract_llm.os)
    with pytest.raises(extract_llm._CloudSelectionIncomplete, match='deadline'):
        _select(led, 'extract', deadline=clock[0] + 1)
    previous = led._cloud_priority_progress
    if change == 'scope':
        led.artifact_add('cloud_history_scope', '{"version":1,"sources":{}}')
    elif change == 'config':
        cfg['cloud_llm']['exclude_project_ids'] = [1]
    else:
        inode[0] = 2
    clock[0] += 1
    try:
        rows = _select(led, 'extract', deadline=clock[0] + 120)
    except extract_llm._CloudSelectionIncomplete:
        rows = []
    assert len(rows) == (64 if change == 'db' else 0)
    assert led._cloud_priority_progress is not previous


def test_cloud_priority_retained_candidate_is_revalidated_after_deadline(cloud_candidates, monkeypatch):
    led, _, *_ = cloud_candidates
    clock = [time.monotonic()]
    ready = cloud_policy.history_target_ready
    calls = []
    def expires_after_second(db, pid, mid):
        result = ready(db, pid, mid)
        calls.append(mid)
        if len(calls) == 2:
            clock[0] += 10
        return result
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(extract_llm_carry, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_context, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_engine, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_errors, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_prepare, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_provider, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_resident, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_runner, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_selection, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_source_contract, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_store, 'time', extract_llm.time)
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    monkeypatch.setattr(cloud_policy, 'history_target_ready', expires_after_second)
    with pytest.raises(extract_llm._CloudSelectionIncomplete, match='deadline'):
        _select(led, 'extract', deadline=clock[0] + 5)
    first = led._cloud_priority_progress['ready'][0][0]
    with led.db:
        led.db.execute('UPDATE messages SET body_state=? WHERE message_id=?', ('deleted', first))
    rows = _select(led, 'extract', deadline=clock[0] + 120)
    assert first not in [row['message_id'] for row in rows]


def test_cloud_priority_interrupted_metadata_query_retries_same_page(cloud_candidates, monkeypatch):
    led, _, *_ = cloud_candidates
    clock = [time.monotonic()]
    real_connect, interrupted = sqlite3.connect, []
    def connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        def trace(sql):
            if not interrupted and sql.startswith('SELECT message_id,project_id,posted_at_ts FROM messages'):
                interrupted.append(True)
                clock[0] += 20
        db.set_trace_callback(trace)
        return db
    monkeypatch.setattr(extract_llm.sqlite3, 'connect', connect)
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(extract_llm_carry, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_context, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_engine, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_errors, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_prepare, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_provider, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_resident, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_runner, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_selection, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_source_contract, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_store, 'time', extract_llm.time)
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    with pytest.raises(extract_llm._CloudSelectionIncomplete, match='deadline'):
        _select(led, 'extract', deadline=clock[0] + 10)
    assert interrupted and led._cloud_priority_progress['after'] is None
    rows = _select(led, 'extract', deadline=clock[0] + 120)
    assert len(rows) == 64 and len({r['message_id'] for r in rows}) == 64


def test_cloud_priority_wrap_discovers_new_rows_after_fixed_highwater(cloud_candidates):
    led, _, *_ = cloud_candidates
    first = _select(led, 'extract', deadline=time.monotonic() + 120)
    assert len(first) == 64
    old(led, 65)
    with led.db:
        led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=65', (time.time() - 1200,))
        led.db.execute('UPDATE message_revisions SET observed_at=? WHERE message_id=65', (time.time() - 1200,))
        for row in first:
            _complete_cloud_tx(led, row)
    assert _select(led, 'extract', deadline=time.monotonic() + 120) == []
    after_wrap = _select(led, 'extract', deadline=time.monotonic() + 120)
    assert all(row['message_id'] != 65 for row in after_wrap)  # not in the old receipt
    with led.db:
        led.db.execute('BEGIN IMMEDIATE')
        cloud_policy.capture_history_tx(led.db, 'synthetic-new-scope', time.time())
    # A new receipt invalidates the old hint, so the new occurrence is checked afresh.
    rows = _select(led, 'extract', deadline=time.monotonic() + 120)
    assert any(row['message_id'] == 65 for row in rows)


def test_cloud_priority_consumes_all_selected_in_fixed_order_over_single_unit_passes(routed):
    led, _, calls, reservations, results = routed
    for mid in range(1, 6):
        old(led, mid)
    with led.db:
        led.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        led.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        cloud_policy.capture_history_tx(led.db, 'synthetic-sequential', time.time())
    expected = [row['message_id'] for row in _select(led, 'extract')]
    for _ in expected:
        assert _cloud_pass(led)['done'] == 1
    actual = [r['identity']['message_id'] for r in reservations]
    assert actual == expected and len(actual) == len(set(actual)) == 5
    assert len(calls) == len(results) == 5 and _claim_count(led) == 0


def test_cloud_priority_short_budget_keeps_selected_candidates_for_next_pass(cloud_candidates):
    led, _, calls, reservations, results = cloud_candidates
    expected = [row['message_id'] for row in _select(led, 'extract')]
    report = extract_llm.run_pending(led, limit=1, budget_s=50, background=True,
        cloud_only=True, prioritize_patients=True)
    assert report['done'] == 0 and calls == reservations == results == []
    assert _claim_count(led) == 0
    assert _cloud_pass(led)['done'] == 1
    assert reservations[0]['identity']['message_id'] == expected[0]
