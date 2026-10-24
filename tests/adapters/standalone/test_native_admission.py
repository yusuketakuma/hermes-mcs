"""Synthetic native admission accounts for real worker and Host cleanup lifetimes."""
import asyncio
import threading

import pytest

from adapters.common import commands
from mcs_standalone.host import Host


def test_closed_gate_refuses_and_only_explicit_owner_reopens():
    gate = commands.NativeAdmission()
    assert gate.close() == {'open': False, 'queued': 0, 'running': 0}
    with pytest.raises(commands.NativeAdmissionClosed, match='^native_admission_closed$'):
        asyncio.run(commands.run_native(lambda: pytest.fail('started'), admission=gate))
    assert gate.open() == {'open': True, 'queued': 0, 'running': 0}


def test_queued_and_canceled_waiter_still_owns_real_work(monkeypatch):
    async def scenario():
        gate = commands.NativeAdmission()
        submitted, release = asyncio.Event(), asyncio.Event()
        original = asyncio.to_thread
        async def delayed(handler, *args, **kwargs):
            submitted.set()
            await release.wait()
            return await original(handler, *args, **kwargs)
        monkeypatch.setattr(asyncio, 'to_thread', delayed)
        called = []
        task = asyncio.create_task(commands.run_native(lambda: called.append(True), admission=gate))
        await submitted.wait()
        assert gate.close() == {'open': False, 'queued': 1, 'running': 0}
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert gate.snapshot()['queued'] == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert called == [True]
        assert gate.snapshot() == {'open': False, 'queued': 0, 'running': 0}
    asyncio.run(scenario())


def test_running_counts_until_actual_finally_after_cancel():
    async def scenario():
        gate = commands.NativeAdmission()
        entered, release = threading.Event(), threading.Event()
        def handler():
            entered.set()
            assert release.wait(5)
        task = asyncio.create_task(commands.run_native(handler, admission=gate))
        try:
            for _ in range(10000):
                if entered.is_set():
                    break
                await asyncio.sleep(0.001)
            assert entered.is_set()
            assert gate.close() == {'open': False, 'queued': 0, 'running': 1}
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert gate.snapshot()['running'] == 1
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gate.snapshot()['running'] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['to_thread', 'create_task', 'submit', 'handler'])
def test_submission_and_handler_failures_release_once(monkeypatch, failure):
    async def scenario():
        gate = commands.NativeAdmission()
        def fail(*args, **kwargs):
            raise OSError('synthetic')
        if failure in ('to_thread', 'create_task'):
            monkeypatch.setattr(asyncio, failure, fail)
        elif failure == 'submit':
            async def submit(*args, **kwargs):
                raise OSError('synthetic')
            monkeypatch.setattr(asyncio, 'to_thread', submit)
        with pytest.raises(OSError):
            await commands.run_native(fail, admission=gate)
        assert gate.snapshot() == {'open': True, 'queued': 0, 'running': 0}
    asyncio.run(scenario())


def test_host_concurrent_close_cancellation_waits_actual_cleanup():
    async def scenario():
        gate = commands.NativeAdmission()
        host = Host({}, native_admission=gate)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def cleanup():
            calls.append(True)
            entered.set()
            await release.wait()
        host.on_unload(cleanup)
        first = asyncio.create_task(host.close())
        await entered.wait()
        second = asyncio.create_task(host.close())
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        assert host.spawn_task(asyncio.sleep(0)) is None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        await host.close()
        assert calls == [True]
        assert gate.snapshot()['open'] is True
        gate.close()
        replacement = Host({}, native_admission=gate)
        await replacement.close()
        assert gate.snapshot()['open'] is False
    asyncio.run(scenario())


@pytest.mark.parametrize('platform', ['slack', 'discord'])
def test_all_action_offloads_borrow_actual_owner(platform):
    if platform == 'slack':
        from adapters.slack.actions import Actions
    else:
        from adapters.discord.actions import Actions
    async def scenario():
        action = object.__new__(Actions)
        gate = action._native_admission = commands.NativeAdmission()
        assert await action._offload(lambda: gate.snapshot()['running']) == 1
        gate.close()
        with pytest.raises(commands.NativeAdmissionClosed):
            await action._offload(lambda: pytest.fail('closed native work'))
    asyncio.run(scenario())


def test_connection_reconnect_does_not_reopen_runtime_admission(monkeypatch):
    from adapters.discord.standalone import _Scope
    async def scenario():
        gate = commands.NativeAdmission()
        scope = _Scope('synthetic', {}, asyncio.Event(), native_admission=gate)
        monkeypatch.setattr(scope, 'current', lambda: True)
        called = []
        async def handler():
            called.append(True)
        assert await scope.invoke(handler)
        gate.close()
        scope.admit_native = False
        scope.admit_native = True  # SDK reconnect may reset only its connection gate.
        assert not await scope.invoke(handler)
        assert called == [True]
        assert not gate.snapshot()['open']
    asyncio.run(scenario())


@pytest.mark.parametrize('platform', ['slack', 'discord'])
@pytest.mark.parametrize('boundary', ['before', 'after', 'queued_check'])
def test_durable_publish_then_gate_close_keeps_confirm_and_receipt(tmp_path, monkeypatch, platform, boundary):
    """No retry invitation or second publish after a durable command lost result access."""
    from types import SimpleNamespace
    from adapters.common import envelopes, registry
    from adapters.discord import actions as discord_actions
    from adapters.slack.actions import Actions as SlackActions
    state, inbox, results = (tmp_path / name for name in ('state', 'inbox', 'results'))
    for path in (state, inbox, results):
        path.mkdir()
    reg = registry.Registry(str(state))
    gate = commands.NativeAdmission()
    cid, confirm_id = 'a' * 32, 'b' * 16
    settings = {'application_id': 'app', 'channel_id': 'channel', 'team_id': 'team',
                'profile': 'synthetic', 'allowed_user_ids': {'operator'},
                'allowed_chat_ids': {'channel'}, 'project_ids': {1}, 'route_epoch': 1}
    origin = {**{k: settings[k] for k in ('application_id', 'channel_id', 'team_id', 'profile')},
              'transport': platform}
    actor = 'slack:team:operator' if platform == 'slack' else 'discord:operator'
    payload = {'command_id': cid, 'cmd': 'ops.synthetic', 'project_id': 1}
    reg.put_confirm(confirm_id, {'token': 'c' * 32, 'actor': actor,
                                'origin': origin, 'payload': payload})
    published, messages = [], []
    original = envelopes.publish_command
    def publish(directory, envelope):
        result = original(directory, envelope)
        published.append(envelope.copy())
        gate.close()
        if boundary == "queued_check":
            raise OSError("synthetic after publish")
        return result
    monkeypatch.setattr(envelopes, 'publish_command', publish)
    async def interactive(**kwargs):
        if boundary == "before":
            gate.close()
        return True
    async def response(*args, **kwargs):
        if args:
            messages.append(args[-1])
    async def ack(**kwargs):
        pass
    if platform == 'slack':
        action = SlackActions(None, settings, {}, reg, None, lambda *a, **k: None,
                              native_admission=gate)
        action._active = True
        monkeypatch.setattr(action, '_card', lambda *a: True)
        monkeypatch.setattr(action, '_pinned', lambda *a: True)
        monkeypatch.setattr(action, '_say', response)
        body = {'team': {'id': 'team'}, 'user': {'id': 'operator'},
                'channel': {'id': 'channel'}, 'api_app_id': 'app'}
        async def invoke():
            await action._confirm(ack, body, {'action_id': 'mcs:c:' + confirm_id})
    else:
        action = discord_actions.Actions(bot=None, settings=settings, root=str(tmp_path),
            reg=reg, log=lambda *a, **k: None, native_admission=gate)
        monkeypatch.setattr(action, '_authorized', lambda *a: None)
        monkeypatch.setattr(action, '_ephemeral', response)
        monkeypatch.setattr(action, '_followup', response)
        interaction = SimpleNamespace(data={'custom_id': 'mcs:c:' + confirm_id},
            user=SimpleNamespace(id='operator', roles=[]), application_id='app',
            channel_id='channel', token='synthetic', guild_id=None, channel=None,
            response=SimpleNamespace(defer=ack))
        async def invoke():
            await action.on_interaction(interaction)
    action._dirs = {'cmd_int': str(inbox), 'cmd_results': str(results)}
    monkeypatch.setattr(action, '_interactive', interactive)
    asyncio.run(invoke())
    if boundary == 'before':
        assert not (inbox / (cid + '.json')).exists()
        assert published == []
        assert not reg.confirm(confirm_id).get('consumed')
        assert not reg.confirm(confirm_id).get('in_flight')
        assert reg.followup(cid) is None
        assert reg.take_confirm(confirm_id, False) == 'taken'
        assert messages[-1] == commands.NATIVE_ADMISSION_CLOSED_TEXT
        return
    saved = (inbox / (cid + '.json')).read_bytes()
    assert saved == envelopes.canonical(payload)
    assert len(published) == 1
    assert reg.confirm(confirm_id).get('consumed', False) is (boundary == 'after')
    assert reg.confirm(confirm_id)['in_flight'] is True
    assert reg.followup(cid) is not None
    assert messages[-1] == commands.NATIVE_ADMISSION_CLOSED_TEXT
    assert all('もう一度' not in message and '失敗しました' not in message for message in messages)
    # The durable consumed confirm remains busy; closed-gate followups never release it.
    assert reg.take_confirm(confirm_id, False) == 'busy'
    assert (inbox / (cid + '.json')).read_bytes() == saved
    assert len(published) == 1
