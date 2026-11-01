"""Synthetic launchd workers capture the update hold at import time."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import mcs_update
from ops_testkit import _load


@pytest.fixture(params=['updater', 'watchdog'])
def runtime(request, tmp_path, monkeypatch):
    tool = mcs_update if request.param == 'updater' else _load()
    data = tmp_path / 'data'
    data.mkdir()
    (tmp_path / 'config.json').write_text(json.dumps({'runtime_mode': 'hermes'}))
    marker = data / 'marker'
    monkeypatch.setattr(tool, 'DATA', str(data))
    monkeypatch.setattr(tool, 'MARKER_PATH', str(marker))
    monkeypatch.setattr(tool, 'AGENTS_DIR', str(tmp_path))
    labels = ('ai.mcs.first', 'ai.mcs.second')
    if request.param == 'updater':
        monkeypatch.setattr(tool.service_contract, 'RESIDENT_LABELS', labels)
        monkeypatch.setattr(tool.service_contract, 'LEGACY_LABELS', ())
        monkeypatch.setattr(tool.service_contract, 'WATCHER_LABELS', ())
        restart = tool.restart_agents
    else:
        monkeypatch.setattr(tool, 'RESIDENT_LABELS', labels)
        monkeypatch.setattr(tool, '_check_recovery_root', lambda: None)
        monkeypatch.setattr(tool, '_runtime_mode', lambda: 'hermes')
        restart = tool._restart_drainers
    running = set(labels)
    calls = []
    fail_stop = set()
    def launch(argv, **kwargs):
        verb, target = argv[1], argv[-1]
        label = Path(target).stem if verb == 'bootstrap' else target.rsplit('/', 1)[-1]
        calls.append((verb, label))
        if verb == 'bootout' and label not in fail_stop:
            running.discard(label)
        elif verb in ('bootstrap', 'kickstart'):
            assert not marker.exists(), 'worker imported with update hold'
            running.add(label)
        rc = 0 if label in running else 113
        out = '\tpid = 4242\n' if label in running else ''
        return subprocess.CompletedProcess(argv, rc if verb == 'print' else 0, out, '')
    for label in labels:
        (tmp_path / (label + '.plist')).write_text('synthetic')
    monkeypatch.setattr(tool, '_run' if request.param == 'updater' else '_launchctl',
                        launch if request.param == 'updater' else lambda args, **kw: launch(['launchctl', *args], **kw))
    monkeypatch.setattr(tool, '_bootstrap_agent', lambda label, plist: launch(['launchctl', 'bootstrap', plist]).returncode == 0)
    clock = iter(i / 10 for i in range(10000))
    monkeypatch.setattr(tool, 'time', SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None))
    return tool, restart, marker, labels, running, calls, fail_stop


@pytest.mark.parametrize('bounce', [True, False])
def test_update_hold_is_removed_after_all_stops_before_any_import(runtime, bounce):
    tool, restart, marker, labels, running, calls, _ = runtime
    marker.write_text('update')
    assert restart(bounce=bounce) == []
    assert not marker.exists() and running == set(labels)
    first_start = next(i for i, (verb, _) in enumerate(calls) if verb == 'bootstrap')
    assert {label for verb, label in calls[:first_start] if verb == 'bootout'} == set(labels)


def test_healthy_ensure_without_marker_never_bounces(runtime):
    _, restart, _, _, _, calls, _ = runtime
    assert restart(bounce=False) == []
    assert all(verb == 'print' for verb, _ in calls)


def test_unverified_stop_keeps_hold_and_never_launches(runtime):
    _, restart, marker, labels, _, calls, fail_stop = runtime
    marker.write_text('update')
    fail_stop.add(labels[0])
    assert restart()
    assert marker.exists()
    assert not any(verb in ('bootstrap', 'kickstart') for verb, _ in calls)


def test_failed_hold_removal_never_launches(runtime, monkeypatch):
    tool, restart, marker, _, _, calls, _ = runtime
    marker.write_text('update')
    monkeypatch.setattr(tool, '_remove_marker', lambda: None)
    assert restart() == ['update_marker_unverifiable']
    assert marker.exists()
    assert not any(verb in ('bootstrap', 'kickstart') for verb, _ in calls)
