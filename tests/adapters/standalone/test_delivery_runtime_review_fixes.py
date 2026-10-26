"""Synthetic regressions for the v1.1 delivery/runtime review fixes (F1, F3)."""
import asyncio
from types import SimpleNamespace

import pytest

from adapters.common import commands, paths, registry
from mcs_standalone import restart_credential


def test_admitted_work_is_tracked_but_never_refused_by_a_closed_gate():
    gate = commands.NativeAdmission()
    gate.close()
    seen = asyncio.run(commands.run_native(lambda: gate.snapshot()['running'],
                                           admission=gate, admitted=True))
    assert seen == 1 and gate.snapshot() == {'open': False, 'queued': 0, 'running': 0}
    with pytest.raises(commands.NativeAdmissionClosed):
        asyncio.run(commands.run_native(lambda: pytest.fail('new work'), admission=gate))


def test_slack_consumed_followup_still_registers_tokens_and_answers(tmp_path, monkeypatch):
    from adapters.slack.actions import Actions
    reg = registry.Registry(str(tmp_path))
    gate = commands.NativeAdmission()
    settings = {'application_id': 'app', 'channel_id': 'channel', 'team_id': 'team',
                'profile': 'synthetic', 'allowed_user_ids': {'operator'},
                'allowed_chat_ids': {'channel'}, 'project_ids': {1}, 'route_epoch': 1}
    origin = {**{k: settings[k] for k in ('application_id', 'channel_id', 'team_id', 'profile')},
              'transport': 'slack'}
    cid = 'a' * 32
    reg.put_followup(cid, {'kind': 'view', 'token': 'b' * 32, 'origin': origin,
                           'user': 'operator', 'actor': 'slack:team:operator'})
    token = 'c' * 32
    result = {'outcome': 'applied', 'action': 'body', 'title': '合成', 'body': '合成本文',
              'projects': [1], 'token_ctx': {token: {'action': 'summary', 'synthetic': True}}}

    def read_then_close(*_args):
        gate.close()  # a stop/config change lands between the read and the answer
        return result
    monkeypatch.setattr(paths, 'read_result', read_then_close)
    said = []

    async def say(channel, user, text, **kwargs):
        said.append(text)
        return True
    action = Actions(None, settings, {'cmd_results': str(tmp_path)}, reg, None,
                     lambda *a, **k: None, native_admission=gate)
    action._active = True
    monkeypatch.setattr(action, '_pinned', lambda *a: {'action': 'body', 'message_id': '1.1'})
    monkeypatch.setattr(action, '_say', say)
    asyncio.run(action.sweep_followups())
    assert reg.followup(cid) is None
    assert reg.token(token)['synthetic'] is True
    assert said and '合成本文' in said[-1]


def test_discord_answer_tokens_land_while_gate_closes(tmp_path):
    from adapters.discord.actions import Actions
    action = object.__new__(Actions)
    action._reg = registry.Registry(str(tmp_path))
    gate = action._native_admission = commands.NativeAdmission()
    gate.close()
    token = 'd' * 32
    asyncio.run(action._register_tokens({'token_ctx': {token: {'synthetic': True}}}))
    assert action._reg.token(token)['synthetic'] is True


@pytest.mark.parametrize('reaper', ['echild', 'unexpected_pid'])
def test_lost_reap_child_stays_busy_until_exit_is_observed(monkeypatch, reaper):
    carrier = object.__new__(restart_credential.CredentialCarrier)
    carrier._spawning = carrier._request = carrier._response = None
    carrier._child = SimpleNamespace(pid=2 ** 22, returncode=None)
    carrier._lost_reap = carrier._failed = False
    assert carrier.busy

    def unknown_child(*_args):
        if reaper == 'echild':
            raise ChildProcessError
        return carrier._child.pid + 1, 0

    monkeypatch.setattr(restart_credential.os, 'waitpid', unknown_child)
    carrier._poll()
    assert carrier._lost_reap and carrier._failed and carrier._child.returncode is None
    assert carrier.busy
    carrier._child.returncode = 0
    assert not carrier.busy
