"""The bounded single-batch path must not hold the whole-run lock."""
import sys
from types import SimpleNamespace

import pytest

import extract_llm_resident


def test_bounded_batch_runs_per_write_without_run_lock(monkeypatch, capsys):
    captured = {}

    monkeypatch.setattr(
        extract_llm_resident, 'acquire_run_lock',
        lambda: pytest.fail('whole-run lock taken for a bounded batch'))
    monkeypatch.setattr(
        extract_llm_resident, '_open_current_ledger',
        lambda *a, **k: SimpleNamespace(close=lambda: None))

    def fake_run_pending(led, *args, **kwargs):
        captured.update(kwargs)
        return {"done": 0, "left": 0, "failed": 0}

    monkeypatch.setattr(extract_llm_resident, 'run_pending', fake_run_pending)
    monkeypatch.setattr(extract_llm_resident, 'legacy_admissions', lambda *a: set())
    monkeypatch.setattr(
        extract_llm_resident.extract_validation,
        'vital_threshold_policy', lambda cfg: None)
    monkeypatch.setattr(extract_llm_resident, 'load_config', lambda *a: {})
    monkeypatch.setattr(sys, 'argv', ['extract_llm.py', '--limit', '5'])

    assert extract_llm_resident._main() == 0
    assert captured['per_write_lock'] is True
    assert captured['write_wait_s'] == extract_llm_resident._RESIDENT_WRITE_WAIT_S
