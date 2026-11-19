"""Resident pass records and the bounded local lookahead behind a save wait."""
import extract_llm_admission
import extract_llm_carry
import extract_llm_context
import extract_llm_engine
import extract_llm_errors
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
import json
import os

import cloud_inference
import extract_llm as worker


class _StubLedger:
    def close(self):
        pass


class _Pool:
    def __init__(self, root, *, context_only):
        assert context_only is True

    def sync(self, *args, **kwargs):
        pass

    def close(self):
        pass


def _resident_env(monkeypatch, tmp_path, ledger, argv, *, stub_fence=True):
    """Patch the resident loop's collaborators; returns the fake monotonic clock."""
    clock = [0.0]
    for module in (worker, extract_llm_engine, extract_llm_prepare, extract_llm_provider,
                   extract_llm_resident, extract_llm_selection, extract_llm_source_contract,
                   extract_llm_store):
        monkeypatch.setattr(module, 'HOME', str(tmp_path))
    monkeypatch.setattr(worker, '_open_current_ledger', lambda **_: ledger)
    monkeypatch.setattr(extract_llm_resident, '_open_current_ledger', worker._open_current_ledger)
    if stub_fence:
        monkeypatch.setattr(worker, '_writer_current', lambda **kwargs: True)
        for module in (extract_llm_carry, extract_llm_errors, extract_llm_resident, extract_llm_runner,
                       extract_llm_selection, extract_llm_source_contract):
            monkeypatch.setattr(module, '_writer_current', worker._writer_current)
        monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(worker, 'load_config', lambda: {})
    for module in (extract_llm_context, extract_llm_prepare, extract_llm_provider, extract_llm_qc,
                   extract_llm_resident, extract_llm_runner, extract_llm_selection, extract_llm_store):
        monkeypatch.setattr(module, 'load_config', worker.load_config)
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings',
                        lambda cfg: {'mode': 'off', 'max_concurrency': 1})
    monkeypatch.setattr(worker.cloud_backlog, 'CloudBacklogPool', _Pool)
    monkeypatch.setattr(worker, 'legacy_admissions', lambda *args: None)
    monkeypatch.setattr(extract_llm_admission, 'legacy_admissions', worker.legacy_admissions)
    monkeypatch.setattr(extract_llm_resident, 'legacy_admissions', worker.legacy_admissions)
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(worker.sys, 'argv', ['extract_llm', '--all', *argv])
    return clock


def _records(out):
    return [json.loads(line) for line in out.splitlines() if line.startswith('{')]


def test_pass_records_carry_lane_pid_elapsed_and_turn(tmp_path, monkeypatch, capsys):
    clock = _resident_env(monkeypatch, tmp_path, _StubLedger(),
                          ['--lane', '2', '--stop-after', '10'])

    def run(led, **kwargs):
        clock[0] += 4
        return {'done': 1, 'failed': 0, 'left': 1, 'selected': 1, 'carry': []}
    monkeypatch.setattr(worker, 'run_pending', run)
    monkeypatch.setattr(extract_llm_resident, 'run_pending', worker.run_pending)
    assert worker.main() == 0
    passes = [row for row in _records(capsys.readouterr().out) if 'turn' in row]
    assert len(passes) == 2
    assert all(row['lane'] == 2 and row['pid'] == os.getpid() and row['turn'] == 'normal'
               and row['elapsed_s'] == 4.0 for row in passes)
