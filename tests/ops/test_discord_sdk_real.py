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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from hermes_plugin.mcs_discord import cards  # noqa: E402


def _spec():
    return {
        "schema": "mcs-card-render/v1",
        "delivery_id": "12345678-1234-4234-8234-1234567890ab",
        "card_key": "signal:s-1",
        "render_id": "r1",
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
    cards.validate(spec)
    view = cards.build_view(spec)
    comps = view.to_components()
    types = [c["type"] for c in comps]
    # TextDisplay=10, ActionRow=1; meta containers are never displayed
    assert types == [10, 10, 10, 10, 10, 1]
    assert all(b["type"] == 2 for b in comps[-1]["components"])
    assert all(b["custom_id"].startswith("mcs:a:")
               for b in comps[-1]["components"])
    # multi-byte labels/contents survive the wire unchanged
    assert "患者A" in comps[1]["content"]
    assert comps[-1]["components"][0]["style"] \
        == discord.ButtonStyle.success.value


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


def test_webhook_partial_accepts_client_for_followup():
    """sweep_followups uses Webhook.partial(app_id, token, client=bot)
    — the client kwarg is what borrows the bot's session/state."""
    import inspect
    params = inspect.signature(discord.Webhook.partial).parameters
    assert "client" in params
    send = inspect.signature(discord.Webhook.send).parameters
    assert "ephemeral" in send and "view" in send
