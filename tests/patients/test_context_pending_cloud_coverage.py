"""Synthetic pending observations distinguish readable local scans from cloud work."""
from contextlib import contextmanager
import copy

import cloud_policy
import extract_llm
import patient_context_refresh as refresh
import patient_context_refresh_readiness as readiness
import patient_context_refresh_sources as sources
import patient_context_refresh_state as state
from test_patient_context_refresh import CFG, runtime, seed, step

__all__ = ['runtime']


def test_local_completed_scan_is_pending_cloud_work_but_stays_readable(runtime, monkeypatch):
    led, _, calls = runtime
    seed(led)
    for _ in range(10):
        if step(runtime)['status'] == 'complete':
            break
    else:
        raise AssertionError('synthetic local context did not complete')
    assert calls
    before = len(calls), led.db.total_changes, led.db.in_transaction
    cfg = {**CFG, 'cloud_llm': {'mode': 'on', 'provider': 'anthropic',
                              'project_ids': [1], 'routes': ['patient_context']}}
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *args: 'synthetic-key')
    import semantic_jev
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *args: None)
    assert readiness.context_view_input(led.db, 1, cfg=cfg)
    estimate = readiness.pending_estimate(led.db, 1, cfg)
    assert estimate['targets'] == estimate['scan_sources'] == 1
    assert estimate['scan_chunks_estimate'] >= 1 and estimate['input_chars'] >= 1
    assert readiness.status(led.db, cfg)['history_pending'] == 1
    # The same saved local work is still complete when cloud scanning is disabled.
    local_cfg = {**CFG, 'cloud_llm': {'mode': 'off', 'provider': 'anthropic'}}
    assert not any(readiness.pending_estimate(led.db, 1, local_cfg).values())
    local = readiness.status(led.db, local_cfg)
    assert local['history_pending'] == 0 and local['complete'] == 1
    assert (len(calls), led.db.total_changes, led.db.in_transaction) == before


def test_phase_scope_filter_never_promotes_assumed_key_to_available(runtime, monkeypatch):
    led, _, _ = runtime
    cfg = {**CFG, 'cloud_llm': {'mode': 'on', 'provider': 'anthropic', 'project_ids': [2, 3]}}
    checked = []
    def mode(_ledger, actual, route, pid):
        checked.append((actual['cloud_llm']['mode'], route, pid))
        return 'off'
    monkeypatch.setattr(extract_llm, '_cloud_mode', mode)
    observe = readiness._cloud_scan_for(led, cfg)
    assert not observe(1)  # Out-of-scope first must not populate the availability cache.
    assert checked == []
    assert not observe(2) and not observe(3) and not observe(2)
    assert checked == [('on', 'patient_context', 2)]
    # A later phase resolves again, including a changed cfg and availability.
    changed = copy.deepcopy(cfg)
    changed['cloud_llm']['mode'] = 'off'
    assert not readiness._cloud_scan_for(led, changed)(2)
    assert len(checked) == 1
    monkeypatch.setattr(extract_llm, '_cloud_mode', lambda *args: checked.append('new') or 'on')
    assert readiness._cloud_scan_for(led, cfg)(2)
    assert checked[-1] == 'new'


def test_patient_selection_resolves_once_after_out_of_scope_patient(runtime, monkeypatch):
    led, clock, _ = runtime
    cfg = {**CFG, 'cloud_llm': {'mode': 'on', 'provider': 'anthropic', 'project_ids': [2]}}
    checked, observed = [], []
    monkeypatch.setattr(extract_llm, '_cloud_mode', lambda *args: checked.append(args[-1]) or 'on')
    plans = iter([1, 2])
    monkeypatch.setattr(refresh.context_schedule, 'plan', lambda *args: {'project_id': next(plans), 'next': None})
    monkeypatch.setattr(refresh.context_schedule, 'advance_turn', lambda *args: None)
    monkeypatch.setattr(refresh, '_advance_patient_scan', lambda ledger, expected, *args: expected)
    monkeypatch.setattr(refresh, '_scan_covered', lambda *args: False)
    monkeypatch.setattr(state, '_out_of_scope', lambda snapshot: False)
    monkeypatch.setattr(state, '_published_version', lambda *args: 0)
    def pending(db, pid, cfg, *, cloud_scan, **kwargs):
        observed.append((pid, cloud_scan))
        return {'dependencies': {'complete': True}, 'unfinished': False}, {}, {'B': cloud_scan}
    monkeypatch.setattr(readiness, '_pending', pending)
    monkeypatch.setattr(readiness, '_observe_readiness', lambda *args, **kwargs: (True, None, None))
    _, selected, *_ = refresh._select_patient(led, cfg, clock.wall, clock.mono + 60,
                                             lambda: True, None)
    assert selected == 2 and observed == [(1, False), (2, True)] and checked == [2]


def test_status_reuses_availability_only_for_its_own_phase(runtime, monkeypatch):
    led, _, _ = runtime
    for pid in (1, 2, 3):
        seed(led, mid=pid, pid=pid, proof=False)
    cfg = {**CFG, 'cloud_llm': {'mode': 'on', 'provider': 'anthropic', 'project_ids': [2, 3]}}
    checked, observed = [], []
    monkeypatch.setattr(extract_llm, '_cloud_mode', lambda *args: checked.append(args[-1]) or 'on')
    monkeypatch.setattr(state, '_out_of_scope', lambda snapshot: False)
    @contextmanager
    def owner(*args):
        yield lambda *args: None
    monkeypatch.setattr(sources, '_scan_read_owner', owner)
    def pending(db, pid, cfg, *, cloud_scan, **kwargs):
        observed.append((pid, cloud_scan))
        return {'source_failures': 0}, {}, {'A': False, 'B': cloud_scan, 'C': False}
    monkeypatch.setattr(readiness, '_pending', pending)
    monkeypatch.setattr(readiness, '_inference_coverage', lambda *args, **kwargs: {'complete': False})
    monkeypatch.setattr(readiness, 'lane_metrics', lambda db: {'lanes': {}})
    assert readiness.status(led.db, cfg)['history_pending'] == 2
    assert observed == [(1, False), (2, True), (3, True)] and checked == [2]
    changed = copy.deepcopy(cfg)
    changed['cloud_llm']['mode'] = 'off'
    assert readiness.status(led.db, changed)['history_pending'] == 0
    assert checked == [2]


def test_pinned_estimate_never_resolves_availability(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    contract = state._contract(CFG, 1)
    monkeypatch.setattr(extract_llm, '_cloud_mode', lambda *args: (_ for _ in ()).throw(
        AssertionError('pinned estimate must not resolve credentials')))
    result = readiness.pending_estimate(led.db, 1, CFG, contract=contract)
    assert result['scan_sources'] == 1
