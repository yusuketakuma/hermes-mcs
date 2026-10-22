"""Synthetic worker ownership and native identity boundaries."""
from types import SimpleNamespace as NS

import pytest

from hermes_plugin import card_workers as cw


@pytest.mark.parametrize('adapter,ctx,expected', [
    (NS(_owner_profile='a'), NS(profile_name='b'), ('a', 'adapter')),
    (NS(gateway_runner=NS(_primary_profile_name='a')), NS(profile_name='b'), ('a', 'runner')),
    (NS(), NS(profile_name='a'), ('a', 'ctx')),
    (NS(_owner_profile=None), NS(), ('default', 'adapter')),
    (NS(), NS(), (None, 'none')),
])
def test_owner_source(adapter, ctx, expected):
    assert cw._owner_identity(ctx, adapter) == expected


@pytest.mark.parametrize('owner,user,application,starts,reason', [
    ('owner', 101, 101, True, None),
    ('foreign', 101, 101, False, 'profile_mismatch'),
    ('foreign', 202, 101, False, 'profile_mismatch'),
    ('owner', 202, 101, False, 'application_mismatch'),
    ('owner', 101, 202, False, 'application_mismatch'),
    (None, 101, 101, False, 'owner_unverified'),
])
def test_discord_bound_owner_and_native_bot(tmp_path, monkeypatch, owner, user, application, starts, reason):
    events, started = [], []
    ctx = NS(get_config=lambda k, d=None: d)
    adapter = NS() if owner is None else NS(_owner_profile=owner)
    monkeypatch.setattr(cw, '_interactive_settings', lambda *_: {
        'data_root': str(tmp_path), 'profile': 'owner', 'application_id': '101'})
    from adapters.common import paths
    monkeypatch.setattr(paths, 'read_flags', lambda *_: {
        'interactive': True, 'transport': 'discord', 'route_epoch': 3})
    monkeypatch.setattr(cw, '_standalone_owned', lambda *_: False)
    monkeypatch.setattr(cw, '_event_logger', lambda *_: lambda event, **kw: events.append((event, kw)))
    from adapters.discord import tasks
    class Supervisor:
        def __init__(self, **kw):
            pass
        def start(self):
            started.append(True)
            return True
    monkeypatch.setattr(tasks, 'Supervisor', Supervisor)
    cw.make_discord_factory(ctx)(NS(user=NS(id=user), application_id=application), adapter)
    assert bool(started) is starts
    if reason:
        assert ('worker_disabled', {'reason': reason}) in events
    else:
        assert ('worker_started', {}) in events
        import card_worker_status
        assert card_worker_status.status(tmp_path, 'discord', 'owner') == {
            'state': 'started', 'reason': 'worker_started'}


@pytest.mark.parametrize('transport,override', [
    ('discord', {'interactive': False}),
    ('discord', {'transport': 'slack'}),
    ('discord', {'runtime_mode': 'standalone'}),
    ('discord', {'route_epoch': None}),
    ('discord', {'route_epoch': True}),
    ('slack', {'interactive': False}),
    ('slack', {'transport': 'discord'}),
    ('slack', {'runtime_mode': 'standalone'}),
])
def test_route_flags_rejection_replaces_old_started_witness_without_private_values(
        monkeypatch, tmp_path, caplog, transport, override):
    import json
    import importlib
    import logging
    import card_worker_status
    from adapters.common import paths

    # These values are synthetic sentinels; none belongs in diagnostic output.
    profile = 'synthetic-private-profile'
    settings = {'data_root': str(tmp_path), 'profile': profile, 'application_id': '101'}
    monkeypatch.setattr(cw, '_interactive_settings', lambda *_: dict(settings))
    monkeypatch.setattr(cw, '_slack_adapter_settings', lambda *_: dict(settings))
    monkeypatch.setattr(cw, '_standalone_owned', lambda *_: False)
    flags = {'interactive': True, 'transport': transport, 'route_epoch': 3,
             'synthetic_private_field': 'synthetic-private-config', **override}
    monkeypatch.setattr(paths, 'read_flags', lambda *_: dict(flags))
    def unexpected_supervisor(**_kwargs):
        pytest.fail('route flags must reject before supervisor construction')
    monkeypatch.setattr(importlib.import_module(f'adapters.{transport}.tasks'),
                        'Supervisor', unexpected_supervisor)
    card_worker_status.record(tmp_path, transport, profile, 'worker_started')
    assert card_worker_status.status(tmp_path, transport, profile)['state'] == 'started'
    caplog.set_level(logging.INFO, logger='hermes.plugin')
    factory = getattr(cw, f'make_{transport}_factory')(NS(get_config=lambda k, d=None: d))
    assert factory(NS(user=NS(id=101), application_id=101), NS(_owner_profile=profile)) is None
    assert card_worker_status.status(tmp_path, transport, profile) == {
        'state': 'disabled', 'reason': 'route_flags_unready'}
    events = [json.loads(record.getMessage()) for record in caplog.records]
    assert events == [{'event': 'worker_owner', 'code': 'adapter'},
                      {'event': 'worker_disabled', 'reason': 'route_flags_unready'}]
    (witness,) = (tmp_path / 'worker_status').glob('*.json')
    stored = json.loads(witness.read_text())
    assert set(stored) == {'version', 'reason', 'pid', 'at'}
    rendered = '\n'.join(record.getMessage() for record in caplog.records)
    for private in (profile, str(tmp_path), '101', 'synthetic-private-config'):
        assert private not in rendered
    assert profile not in witness.read_text()


@pytest.mark.parametrize('transport', ['slack', 'discord'])
def test_failed_supervisor_does_not_record_started(monkeypatch, tmp_path, transport):
    import importlib
    import card_worker_status
    events = []
    monkeypatch.setattr(cw, '_interactive_settings', lambda *_: {
        'data_root': str(tmp_path), 'profile': 'owner', 'application_id': '101'})
    monkeypatch.setattr(cw, '_slack_adapter_settings', lambda *_: {
        'data_root': str(tmp_path), 'profile': 'owner', 'application_id': '101'})
    from adapters.common import paths
    monkeypatch.setattr(paths, 'read_flags', lambda *_: {
        'interactive': True, 'transport': transport, 'route_epoch': 1})
    monkeypatch.setattr(cw, '_standalone_owned', lambda *_: False)
    monkeypatch.setattr(cw, '_event_logger', lambda *_: lambda event, **kw: events.append(event))
    tasks = importlib.import_module(f'adapters.{transport}.tasks')
    class Supervisor:
        def __init__(self, **kw):
            pass
        def start(self):
            return False
    monkeypatch.setattr(tasks, 'Supervisor', Supervisor)
    factory = getattr(cw, f'make_{transport}_factory')(NS(get_config=lambda k, d=None: d))
    assert factory(NS(user=NS(id=101), application_id=101), NS(_owner_profile='owner')) is None
    assert 'worker_started' not in events
    assert card_worker_status.status(tmp_path, transport, 'owner') == {
        'state': 'disabled', 'reason': 'worker_start_failed'}


@pytest.mark.parametrize('transport', ['discord', 'slack'])
@pytest.mark.parametrize('failure_at', ['bootstrap', 'status_import'])
@pytest.mark.parametrize('startup_error', [False, True])
def test_diagnostic_import_failure_preserves_started_supervisor(
        tmp_path, monkeypatch, transport, failure_at, startup_error):
    import builtins
    import importlib
    from adapters.common import mcs_api, paths
    settings = {'data_root': str(tmp_path), 'profile': 'owner', 'application_id': '101'}
    monkeypatch.setattr(cw, '_interactive_settings', lambda *_: dict(settings))
    monkeypatch.setattr(cw, '_slack_adapter_settings', lambda *_: dict(settings))
    monkeypatch.setattr(cw, '_standalone_owned', lambda *_: False)
    monkeypatch.setattr(paths, 'read_flags', lambda *_: {
        'interactive': True, 'transport': transport, 'route_epoch': 1})
    events, instances = [], []
    monkeypatch.setattr(cw, '_event_logger', lambda *_: lambda event, **kw: events.append(event))
    tasks = importlib.import_module(f'adapters.{transport}.tasks')
    class Supervisor:
        def __init__(self, **kw):
            self.started = False
            instances.append(self)
        def start(self):
            if startup_error:
                raise ImportError('actual-startup-failed')
            self.started = True
            return True
    monkeypatch.setattr(tasks, 'Supervisor', Supervisor)
    if failure_at == 'bootstrap':
        def unavailable():
            raise ImportError('diagnostic-bootstrap-unavailable')
        monkeypatch.setattr(mcs_api, 'bootstrap', unavailable)
    else:
        original_import = builtins.__import__
        def limited_import(name, *args, **kwargs):
            if name == 'card_worker_status':
                raise ImportError('diagnostic-status-unavailable')
            return original_import(name, *args, **kwargs)
        monkeypatch.setattr(builtins, '__import__', limited_import)
    factory = getattr(cw, f'make_{transport}_factory')(NS(get_config=lambda k, d=None: d))
    native, adapter = NS(user=NS(id=101), application_id=101), NS(_owner_profile='owner')
    if startup_error:
        with pytest.raises(ImportError, match='actual-startup-failed'):
            factory(native, adapter)
        assert not instances[0].started
        assert 'worker_started' not in events
        assert 'worker_status_failed' not in events
    else:
        result = factory(native, adapter)
        assert result is instances[0] and result.started
        assert 'worker_started' in events
        assert 'worker_status_failed' in events
