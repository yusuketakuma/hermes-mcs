"""Synthetic cloud selection progress contracts with isolated shared fixtures."""
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
from types import SimpleNamespace
import pytest
import cloud_policy
import extract_llm
import cloud_inference
from test_anthropic_runtime import _assert_legacy_retained as _assert_legacy_retained, _current_artifacts as _current_artifacts, old as old, routed as routed
from cloud_selection_progress_testkit import _claim_count as _claim_count, _make_paid_carry as _make_paid_carry, blocked_prefix as blocked_prefix, cloud_candidates as cloud_candidates
from test_cloud_response_recovery import legacy_cloud_routes as legacy_cloud_routes


@pytest.mark.parametrize("raw", [
    '{"version":true,"sources":{"1:1":"' + "a" * 64 + '"}}',
    '{"version":1,"version":1,"sources":{"1:1":"' + "a" * 64 + '"}}',
    '{"version":1,"sources":{"1:1":"' + "a" * 64 + '","1:1":"' + "b" * 64 + '"}}',
])
def test_ambiguous_history_receipt_never_authorizes_source(raw):
    assert cloud_policy._history_sources(raw) == {}


@pytest.mark.parametrize('wait_s', [0, 0.04, 0.5])
def test_deadline_lock_wait_is_bounded_with_real_contended_lock(tmp_path, monkeypatch, wait_s):
    import os
    import writer_lock
    path = str(tmp_path / 'synthetic-run.lock')
    owner = writer_lock.acquire_run_lock(path)
    assert owner is not None
    clock, sleeps, attempts = [100.0], [], []
    def acquire():
        attempts.append(clock[0])
        return writer_lock.acquire_run_lock(path)
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(extract_llm, 'acquire_run_lock', acquire)
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', extract_llm.acquire_run_lock)
    monkeypatch.setattr(extract_llm_resident, 'acquire_run_lock', extract_llm.acquire_run_lock)
    monkeypatch.setattr(extract_llm, '_writer_current', lambda **kw: True)
    monkeypatch.setattr(extract_llm_carry, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_errors, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_resident, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_runner, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_selection, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_source_contract, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
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
    try:
        with extract_llm._write_lock(True, wait_s=wait_s, deadline=100.1) as held:
            assert not held
        assert attempts and clock[0] <= 100.1
        assert sum(sleeps) == pytest.approx(min(wait_s, 0.1))
        sleeps.clear()
        with extract_llm._write_lock(True, wait_s=wait_s) as held:
            assert not held
        assert sleeps == [1]  # unchanged historical polling, even for zero and fractions
    finally:
        os.close(owner)
