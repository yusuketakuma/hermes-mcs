"""A taken confirm frees its confirm and followup only when the admission refused
before any publish; once the publish may have started nothing is freed or resent
(synthetic registry and offload stubs, no SDK)."""
import asyncio
from types import SimpleNamespace

import pytest

from adapters.common import commands
from adapters.discord.actions import Actions as DiscordActions
from adapters.slack.actions import Actions as SlackActions

PAYLOAD = {"command_id": "cmd-synthetic", "kind": "human"}


class _Reg:
    def __init__(self):
        self.calls = []

    def end_confirm(self, confirm_id):
        self.calls.append(("end_confirm", confirm_id))

    def drop_followup(self, command_id):
        self.calls.append(("drop_followup", command_id))
        return True


@pytest.fixture
def published(monkeypatch):
    sent = []
    monkeypatch.setattr(commands.envelopes, "publish_command",
                        lambda cmd_dir, payload: sent.append((cmd_dir, payload)) or "env")
    return sent


def _run(offload, reg):
    return asyncio.run(commands.publish_taken_confirm(reg, offload, "/synthetic/cmd_int",
                                                      "c-1", PAYLOAD))


def test_refusal_before_any_publish_frees_the_confirm_and_followup(published):
    reg = _Reg()

    async def refused(handler):
        raise commands.NativeAdmissionClosed()
    with pytest.raises(commands.NativeAdmissionClosed):
        _run(refused, reg)
    assert reg.calls == [("end_confirm", "c-1"), ("drop_followup", "cmd-synthetic")]
    assert published == []


@pytest.mark.parametrize("failure", [commands.NativeAdmissionClosed(), OSError("synthetic"),
                                     asyncio.CancelledError()])
def test_a_publish_that_may_have_started_frees_nothing(published, failure):
    reg = _Reg()

    async def started_then_fails(handler):
        handler()
        raise failure
    with pytest.raises(type(failure)):
        _run(started_then_fails, reg)
    assert reg.calls == []
    assert published == [("/synthetic/cmd_int", PAYLOAD)]                 # exactly one attempt


def test_a_successful_publish_keeps_the_confirm(published):
    reg = _Reg()

    async def offload(handler):
        return handler()
    assert _run(offload, reg) is None
    assert reg.calls == [] and published == [("/synthetic/cmd_int", PAYLOAD)]


@pytest.mark.parametrize("actions", [SlackActions, DiscordActions])
def test_both_transports_delegate_to_the_shared_boundary(published, actions):
    reg = _Reg()

    async def refused(handler):
        raise commands.NativeAdmissionClosed()
    host = SimpleNamespace(_reg=reg, _offload=refused, _dirs={"cmd_int": "/synthetic/cmd_int"})
    with pytest.raises(commands.NativeAdmissionClosed):
        asyncio.run(actions._publish_taken_confirm(host, "c-2", PAYLOAD))
    assert reg.calls == [("end_confirm", "c-2"), ("drop_followup", "cmd-synthetic")]
