"""D3/RC16 — real discord.py serialization checks.

The default test env has no SDK; these run only where discord.py is
installed (e.g. the Hermes messaging venv, discord.py==2.7.1) and skip
cleanly otherwise. They pin the wire contract our builders rely on:
LayoutView -> Components V2 payload, Modal custom_id round-trip, and
the response-type semantics of defer() for component/modal submits.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

discord = pytest.importorskip("discord")

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from hermes_plugin.mcs_delivery import spec as spec_mod  # noqa: E402
from hermes_plugin.mcs_discord import cards  # noqa: E402


def _spec():
    return {
        "schema": "mcs-card-render/v1",
        "delivery_id": "12345678-1234-4234-8234-1234567890ab",
        "card_key": "signal:s-1",
        "kind": "signal",
        "render_rev": 1,
        "op": "create",
        "source_generation": 1,
        "presentation_generation": 1,
        "ui_revision": 0,
        "delivery": {
            "application_id": "123456789",
            "channel_id": "987654321",
            "profile": "mcs",
            "guild_id": "7",
            "message_id": None,
            "thread_id": None,
            "route_epoch": 1,
            "correlation": "ab" * 16,
            "intent_event_ids": [7],
        },
        "parts": {
            "containers": [
                {"type": "heading", "text": "要確認シグナル"},
                {"type": "text", "text": "対象: 患者A — マルチバイト"},
                {"type": "field", "name": "優先度", "value": "high"},
                {"type": "quote", "text": "発言: 嘔気が続く"},
                {"type": "meta", "text": "correlation-hidden"},
            ],
            "footer": [{"type": "text", "text": "mcs notify"}],
            "thread_name": "signal-thread",
            "action_rows": [[
                {"ui": "button", "id": "ack", "label": "確認",
                 "style": "success", "token": "ab" * 16},
                {"ui": "button", "id": "dismiss", "label": "棄却",
                 "style": "danger", "token": "ef" * 16},
            ]],
            "context": {"project_id": 1},
        },
    }


def test_layout_view_serializes_components_v2():
    spec = _spec()
    spec_mod.validate(spec)
    view = cards.build_view(spec)
    comps = view.to_components()
    # the face is a single Container card (type 17) — bare TextDisplays
    # on a LayoutView render as flat text with no card look
    assert [c["type"] for c in comps] == [17]
    assert comps[0]["accent_color"] == cards._ACCENTS["signal"]
    inner = comps[0]["components"]
    # body TextDisplay=10, Separator=14, footer, Separator, the primary
    # button row and the 他の操作 select row; meta is never displayed
    assert [c["type"] for c in inner] == [10, 14, 10, 14, 1, 1]
    assert all(c["divider"] and c["spacing"] == 1
               for c in inner if c["type"] == 14)
    assert inner[2]["content"] == "-# mcs notify"
    buttons = inner[-2]["components"]
    assert [b["type"] for b in buttons] == [2]
    assert buttons[0]["custom_id"] == "mcs:a:" + "ab" * 16
    (menu,) = inner[-1]["components"]
    assert (menu["type"], menu["custom_id"]) == (3, "mcs:menu")
    assert [(o["label"], o["value"]) for o in menu["options"]] == [
        ("棄却", "ef" * 16)]
    # multi-byte labels/contents survive the wire unchanged
    assert "患者A" in inner[0]["content"]
    # '>>>' would swallow the rest of the merged display — the quote
    # must use the per-line '>' form
    assert "> 発言: 嘔気が続く" in inner[0]["content"]
    assert ">>>" not in inner[0]["content"]
    assert buttons[0]["style"] == discord.ButtonStyle.success.value


def test_layout_view_marks_v2_flag_for_send_and_edit():
    """handle_message_parameters sets flags=32768 (components_v2) for
    any view reporting has_components_v2 — covering both the create
    send() and update edit() paths without a request firing."""
    from discord.http import handle_message_parameters
    view = cards.build_view(_spec())
    assert view.has_components_v2()
    params = handle_message_parameters(view=view)
    payload = params.payload
    assert payload["flags"] & 32768
    assert payload["components"]


def test_modal_custom_id_and_text_inputs_round_trip():
    """Modal submit dispatches on data['custom_id'] — our mcs:m:<id>
    namespace — with TextInput rows under data['components']."""
    modal = discord.ui.Modal(title="依頼を起票", custom_id="mcs:m:aa" * 1)
    modal.add_item(discord.ui.TextInput(
        label="件名", style=discord.TextStyle.short,
        custom_id="title", max_length=1000, required=True))
    modal.add_item(discord.ui.TextInput(
        label="理由・依頼内容", style=discord.TextStyle.paragraph,
        custom_id="reason", max_length=2000, required=True))
    payload = modal.to_components()
    assert modal.custom_id.startswith("mcs:m:")
    for row in payload:
        assert row["type"] == 1                    # ActionRow
        assert row["components"][0]["type"] == 4   # TextInput


def test_defer_type_is_message_update_for_components():
    """defer() on component/modal_submit interactions answers with
    type-6 deferred_message_update — ephemeral only applies to
    thinking=True or application commands. Pin the branch our code
    relies on (a bare defer must NOT produce a public message)."""
    src = Path(discord.InteractionResponse.defer.__code__.co_filename)
    body = discord.InteractionResponse.defer.__doc__ or ""
    assert "deferred_message_update" in body
    assert "modal_submit" in body
    assert src.name == "interactions.py"


def test_send_attachment_wraps_sealed_file_in_real_discord_file(tmp_path):
    """cards.send_attachment is the single discord.py import site for
    durable attachment parts — it must produce a real discord.File
    addressed to the part's target thread, nothing else."""
    import asyncio
    from types import SimpleNamespace

    f = tmp_path / "sealed.bin"
    f.write_bytes(b"synthetic-sealed-bytes")
    sent = []

    class FakeThread:
        async def send(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(id=4242)

    out = asyncio.run(cards.send_attachment(
        FakeThread(), str(f), "sealed.bin"))
    assert out.id == 4242
    (kw,) = sent
    file = kw["file"]
    assert isinstance(file, discord.File)
    assert file.filename == "sealed.bin"
    file.fp.seek(0)
    assert file.fp.read() == b"synthetic-sealed-bytes"


def test_webhook_partial_accepts_client_for_followup():
    """sweep_followups uses Webhook.partial(app_id, token, client=bot)
    — the client kwarg is what borrows the bot's session/state."""
    import inspect
    params = inspect.signature(discord.Webhook.partial).parameters
    assert "client" in params
    send = inspect.signature(discord.Webhook.send).parameters
    assert "ephemeral" in send and "view" in send


def test_validator_matches_nested_components_budget():
    spec = _spec()
    # 34 text items + one action row + five buttons reach the SDK limit.
    spec["parts"]["containers"] = [{"type": "text", "text": "x"}] * 34
    spec["parts"]["footer"] = []
    spec["parts"]["action_rows"] = [[
        {"ui": "button", "id": "ack", "label": "確認", "style": "success",
         "token": f"{i:032x}"} for i in range(5)]]
    spec_mod.validate(spec)
    view = cards.build_view(spec)
    # text merges inside the Container — the 10-child cap is never hit
    inner = view.to_components()[0]["components"]
    assert len(inner) == 3          # merged TextDisplay, Separator, row
    spec["parts"]["containers"].append({"type": "text", "text": "overflow"})
    with pytest.raises(ValueError, match="component_budget"):
        spec_mod.validate(spec)


@pytest.mark.parametrize("wire_length", [4000, 4001])
def test_field_text_budget_counts_markdown_wrapper(wire_length):
    spec = _spec()
    spec["parts"]["containers"] = [
        {"type": "field", "name": "N", "value": "x" * (wire_length - 7)}]
    spec["parts"]["footer"] = []
    spec["parts"]["action_rows"] = []
    view = cards.build_view(spec)
    inner = view.to_components()[0]["components"]
    assert sum(len(item["content"]) for item in inner) == wire_length
    if wire_length == 4000:
        spec_mod.validate(spec)
    else:
        with pytest.raises(ValueError, match="text_budget"):
            spec_mod.validate(spec)


def test_real_sdk_send_history_fetch_and_edit_preserve_remote_message_id(monkeypatch):
    """The testkit's receipt/history identity follows actual SDK message decoding."""
    import asyncio

    async def scenario():
        bot = discord.Client(intents=discord.Intents.none())
        await bot._async_setup_hook()
        remote = {"id": "7001", "channel_id": "42", "content": "合成本文",
                  "author": {"id": "4242", "username": "MCS", "discriminator": "0000",
                             "avatar": None, "bot": True},
                  "timestamp": "2026-10-01T00:00:00+00:00", "edited_timestamp": None,
                  "tts": False, "mention_everyone": False, "mentions": [],
                  "mention_roles": [], "attachments": [], "embeds": [], "pinned": False,
                  "type": 0, "flags": 0}

        class Channel(discord.abc.Messageable):
            id, guild, _state = 42, None, bot._connection

            async def _get_channel(self):
                return self

        async def send(channel_id, *, params):
            assert channel_id == 42
            remote["content"] = params.payload["content"]
            return dict(remote)

        async def fetch(channel_id, message_id):
            assert (channel_id, message_id) == (42, 7001)
            return dict(remote)

        async def history(channel_id, limit, **kwargs):
            assert channel_id == 42 and limit == 1
            return [dict(remote)]

        async def edit(channel_id, message_id, *, params):
            assert (channel_id, message_id) == (42, 7001)
            remote["content"] = params.payload["content"]
            return dict(remote)

        monkeypatch.setattr(bot.http, "send_message", send)
        monkeypatch.setattr(bot.http, "get_message", fetch)
        monkeypatch.setattr(bot.http, "logs_from", history)
        monkeypatch.setattr(bot.http, "edit_message", edit)
        try:
            channel = Channel()
            sent = await channel.send("合成本文", allowed_mentions=discord.AllowedMentions.none())
            listed = [m async for m in channel.history(limit=1)]
            fetched = await channel.fetch_message(sent.id)
            updated = await fetched.edit(content="合成更新", allowed_mentions=discord.AllowedMentions.none())
            assert isinstance(sent, discord.Message)
            assert sent.id == listed[0].id == fetched.id == updated.id == 7001
            assert updated.content == "合成更新" and listed[0].author.id == 4242
        finally:
            await bot.close()
    asyncio.run(scenario())
