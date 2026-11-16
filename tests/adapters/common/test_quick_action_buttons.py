"""確認する・担当する as card buttons on every transport, the layout-6
request-first summary order and the state accent. Synthetic only."""
from __future__ import annotations

import copy
import sys

import pytest

from adapters.common import card_controls, spec as spec_mod
from adapters.discord import cards as discord_cards
from adapters.lineworks import cards as lineworks_cards
from adapters.slack import cards as slack_cards
from discord_testkit import _fake_discord

LINK = {"id": "link", "ui": "link", "label": "MCSで開く",
        "url": "https://www.medical-care.net/projects/medical/1"}
ACTIONS = ("ack", "assign", "request", "body", "summary")


def _spec(transport="discord", accent=None):
    rows = [[{"id": a, "ui": "button", "style": "secondary", "label": f"L{a}",
              "token": f"{n:032x}"} for n, a in enumerate(ACTIONS, 1)], [dict(LINK)]]
    parts = {"containers": [{"type": "heading", "text": "✅ 合成 花子"}],
             "footer": [], "action_rows": rows, "context": {"project_id": 1}}
    delivery = {"application_id": "1", "channel_id": "42", "route_epoch": 1,
                "correlation": "c" * 32, "intent_event_ids": [1]}
    schema = {"discord": "mcs-card-render/v1", "slack": "mcs-card-render/v2",
              "lineworks": "mcs-card-render/v3"}[transport]
    if transport == "discord":
        delivery["guild_id"] = "7"
    else:
        delivery.update(transport=transport, team_id="T1", profile="p")
    if accent:
        parts[transport] = {"accent": accent}
    return {"schema": schema, "delivery_id": "00000000-0000-4000-8000-000000000001",
            "card_key": "v1|thread|1|100", "kind": "thread", "op": "create",
            "render_rev": 1, "source_generation": 1, "presentation_generation": 1,
            "ui_revision": 1, "delivery": delivery, "parts": parts}


def _token_ids(spec):
    return {b["token"]: b["id"] for row in spec["parts"]["action_rows"]
            for b in row if b.get("token")}


def test_controls_split_quick_actions_out_of_the_menu():
    controls = card_controls.plan_controls(_spec()["parts"])
    assert [b["id"] for b in controls.quick] == ["ack", "assign"]
    assert [b["id"] for b in controls.menu] == ["request", "body", "summary"]
    # every operation still has exactly one place
    assert {b["id"] for b in controls.quick + controls.menu} == set(ACTIONS)


def test_slack_draws_quick_buttons_before_the_menu():
    spec = _spec("slack")
    ids = _token_ids(spec)
    _, blocks = slack_cards.render(spec)
    (actions,) = [b for b in blocks if b["type"] == "actions"]
    *buttons, menu = actions["elements"]
    assert [(b["type"], ids[b["value"]], b["action_id"]) for b in buttons] == [
        ("button", "ack", "mcs:a:" + f"{1:032x}"), ("button", "assign", "mcs:a:" + f"{2:032x}")]
    assert [ids[o["value"]] for o in menu["options"]] == ["request", "body", "summary"]


def test_discord_draws_quick_buttons_with_the_link_row(monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    spec = _spec()
    ids = _token_ids(spec)
    inner = discord_cards.build_view(spec).items[0].children
    menu_row, button_row = inner[-2].children, inner[-1].children
    assert [ids[o.value] for o in menu_row[0].options] == ["request", "body", "summary"]
    assert [ids.get((b.custom_id or "").removeprefix("mcs:a:"), b.url) for b in button_row] == [
        "ack", "assign", LINK["url"]]


def test_discord_quick_buttons_return_to_the_menu_when_the_row_is_full(monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    spec = _spec()
    spec["parts"]["action_rows"].append([dict(LINK, label=f"MCSで開く{n}") for n in range(3)])
    buttons, menu = discord_cards._card_buttons(spec)
    assert all(b.url for b in buttons)
    assert [_token_ids(spec)[o.value] for o in menu][:2] == ["ack", "assign"]


def test_lineworks_card_buttons_lead_with_quick_actions():
    spec = _spec("lineworks")
    spec["parts"]["action_rows"].append(
        [{"id": "more", "ui": "button", "label": "その他の操作", "token": "f" * 32}])
    assert [b["label"] for b in lineworks_cards.action_buttons(spec)] == [
        "Lack", "Lassign", "操作を選ぶ…", "MCSで開く"]
    assert [b["id"] for b in lineworks_cards.secondary(spec)] == ["request", "body", "summary"]


@pytest.mark.parametrize("accent,colour", [
    ("urgent", 0xED4245), ("assigned", 0xFEE75C), ("acked", 0x80848E), (None, 0x5865F2)])
def test_discord_accent_follows_the_card_state(accent, colour):
    spec = _spec(accent=accent)
    spec_mod.validate(spec)
    assert discord_cards._accent(spec) == colour


def test_unknown_accent_is_rejected():
    spec = copy.deepcopy(_spec(accent="done"))
    with pytest.raises(ValueError, match="bad_accent"):
        spec_mod.validate(spec)


def test_request_first_order_pins_confirmed_requests_only(monkeypatch):
    import structured_view
    monkeypatch.setattr(structured_view, "_head_ranked", lambda *a, **k: [
        (0, "🚨 緊急度: 高"), (1, "合成の概要"), (1, "要点: 合成の要点"), (7, "区分: 依頼")])
    monkeypatch.setattr(structured_view.mcs_queries, "current_message_extract",
                        lambda db, kind, mid: {"next_planned": "合成日"})
    monkeypatch.setattr(structured_view.mcs_queries, "current_message_facts",
                        lambda db, mid: {})
    monkeypatch.setattr(structured_view.clinical_urgency, "message_urgency_details",
                        lambda *a, **k: {"source": None, "reasons": [], "held": False, "kind": None})
    monkeypatch.setattr(structured_view, "vital_line", lambda *a, **k: None)
    monkeypatch.setattr(structured_view, "_symptom_line", lambda *a, **k: None)
    monkeypatch.setattr(structured_view, "_canonical_finding_lines", lambda *a, **k: [])
    monkeypatch.setattr(structured_view, "_med_lines", lambda *a, **k: ["薬剤: 合成薬 5mg[開始]"])
    monkeypatch.setattr(structured_view, "_request_lines", lambda *a, **k: [
        "依頼: 医師へ合成確認", "依頼候補（未確認）: 合成の候補"])
    old = structured_view.structured_lines(None, 1, plain=True)
    new = structured_view.structured_lines(None, 1, plain=True, request_first=True)
    assert old == ["🚨 緊急度: 高", "合成の概要", "要点: 合成の要点", "依頼: 医師へ合成確認",
                   "依頼候補（未確認）: 合成の候補", "薬剤: 合成薬 5mg[開始]", "次回予定: 合成日",
                   "区分: 依頼"]
    assert new == ["🚨 緊急度: 高", "📌 依頼: 医師へ合成確認", "薬剤: 合成薬 5mg[開始]",
                   "合成の概要", "要点: 合成の要点", "依頼候補（未確認）: 合成の候補",
                   "次回予定: 合成日", "区分: 依頼"]
