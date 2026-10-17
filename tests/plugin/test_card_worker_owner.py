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
def test_discord_bound_owner_and_native_bot(monkeypatch, owner, user, application, starts, reason):
    events, started = [], []
    ctx = NS(get_config=lambda k, d=None: d)
    adapter = NS() if owner is None else NS(_owner_profile=owner)
    monkeypatch.setattr(cw, '_interactive_settings', lambda *_: {
        'data_root': '/synthetic', 'profile': 'owner', 'application_id': '101'})
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
