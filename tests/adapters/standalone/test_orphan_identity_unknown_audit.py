"""Unverified prior children fence replacements until identity or exit is known."""
import json
from types import SimpleNamespace

import pytest

from mcs_standalone import runtime, service
from test_stab_runtime_supervision import _complete_identity


@pytest.fixture
def host(tmp_path, monkeypatch):
    (tmp_path / 'data').mkdir()
    monkeypatch.setattr(runtime, 'log', lambda *args, **kwargs: None)
    starts = []
    monkeypatch.setattr(runtime.subprocess, 'Popen',
                        lambda argv, **kwargs: starts.append(argv) or
                        SimpleNamespace(pid=9001, poll=lambda: None))
    instance = runtime.Runtime(tmp_path, {'runtime_mode': 'standalone'})
    monkeypatch.setattr(instance, '_start_identity', lambda: None)
    status = tmp_path / 'data' / service.STATUS_FILE
    status.write_text(json.dumps({'pid': 3999, 'children': {'extract-0': {'pid': 4001}}}))
    status.chmod(0o600)
    instance.inflight['extract-0'] = 60
    return instance, starts


def test_unknown_initial_orphan_blocks_start_and_preserves_inflight(host, monkeypatch):
    instance, starts = host
    monkeypatch.setattr(runtime.os, 'kill', lambda *args: None)
    instance.adopt_orphans(100)
    _complete_identity(instance, monkeypatch, "extract-0", None, 100, runtime.signal.SIGTERM)
    instance.recover_inflight()
    instance.start('extract-0', 'background', 100)
    assert instance.children == {} and starts == []
    assert instance.orphan_candidates == {'extract-0': 4001}
    assert instance.inflight == {'extract-0': 60}
    instance.restarting = True
    monkeypatch.setattr(instance, '_updating', lambda now: False)
    monkeypatch.setattr(instance, '_publish_status', lambda *args: None)
    assert instance.tick(101, draining=True) is False
    assert instance.orphan_candidates and instance.inflight


@pytest.mark.parametrize('identity', ['same', 'different', 'exited'])
def test_pending_identity_eventually_adopts_or_allows_replacement(host, monkeypatch, identity):
    instance, starts = host
    signals = []

    def kill(pid, sig):
        signals.append((pid, sig))
        if identity == 'exited':
            assert sig == 0
            raise ProcessLookupError

    monkeypatch.setattr(runtime.os, 'kill', kill)
    instance.adopt_orphans(100)
    assert signals == []
    _complete_identity(instance, monkeypatch, 'extract-0',
                       'exited' if identity == 'exited' else identity == 'same', 101,
                       runtime.signal.SIGTERM if identity == 'same' else None)
    instance.recover_inflight()
    assert instance.orphan_candidates == {}
    instance.start('extract-0', 'background', 101)
    if identity == 'same':
        assert starts == []
        assert instance.children['extract-0']['adopted'] is True
        assert instance.inflight == {'extract-0': 60}
        assert signals == [(4001, runtime.signal.SIGTERM)]
    else:
        assert len(starts) == 1
        assert instance.inflight == {}
        assert signals == ([(4001, 0)] if identity == 'exited' else [])


def test_unknown_orphan_survives_status_and_another_host_restart(host, monkeypatch):
    instance, starts = host
    instance.adopt_orphans(100)
    instance._save_fired()
    instance._publish_status(101, False)
    status = instance.data / service.STATUS_FILE
    saved = json.loads(status.read_text())
    assert saved['children']['extract-0'] == {
        'pid': 4001, 'kind': 'background', 'state': 'orphan_identity_unknown'}
    saved['pid'] = 3999  # The first host exited; the second has a different identity.
    status.write_text(json.dumps(saved))
    second = runtime.Runtime(instance.root, instance.cfg)
    second.adopt_orphans(102)
    second.recover_inflight()
    second.start('extract-0', 'background', 102)
    assert second.orphan_candidates == {'extract-0': 4001}
    assert second.inflight == {'extract-0': 60}
    assert starts == []


@pytest.mark.parametrize('identity', ['different', 'exited'])
def test_unknown_calendar_orphan_preserves_due_slot_until_one_catchup(host, monkeypatch, identity):
    instance, starts = host
    job = 'mcs_check'
    assert job in instance.argv
    instance.orphan_candidates[job] = 4002
    instance.fired[job] = 60
    instance.inflight.clear()
    instance.last_minute = 3
    instance.due = {job: 120}
    instance._schedule(180)
    assert starts == []
    assert instance.fired[job] == 60 and instance.inflight == {}
    assert instance.skipped[job] == 120

    def gone(pid, sig):
        assert (pid, sig) == (4002, 0)
        raise ProcessLookupError

    monkeypatch.setattr(runtime.os, 'kill', gone)
    _complete_identity(instance, monkeypatch, job, 'exited' if identity == 'exited' else False, 181)
    instance.recover_inflight()
    instance._schedule(181)
    assert len(starts) == 1
    assert instance.children[job]['kind'] == 'core'
    assert instance.fired[job] == 120 and instance.inflight[job] == 60
    instance._schedule(182)
    assert len(starts) == 1
