"""Card button surfaces in both transports: link buttons, silent member
mentions, the spec keys an outdated worker must refuse. Synthetic only."""
from __future__ import annotations

import copy
import hashlib
import sys

import pytest

from hermes_plugin.mcs_delivery import envelopes, spec as spec_mod, text
from hermes_plugin.mcs_discord import cards as discord_cards
from hermes_plugin.mcs_slack import cards as slack_cards
from discord_testkit import _fake_discord

LINK = {"id": "link", "ui": "link", "label": "🔗 MCSで開く",
        "url": "https://www.medical-care.net/projects/medical/1"}


def _spec(footer=("✅ 確認: <@1001>・<@U0AB12CD>",
                  "📝 一件目 — 担当 山田\n📝 他1件 & <b>"),
          slack=False):
    parts = {"containers": [{"type": "heading", "text": "💬 患者A"}],
             "footer": [{"type": "text", "text": t} for t in footer],
             "action_rows": [[{"id": "ack", "ui": "button",
                               "style": "success", "label": "✅ 確認済み",
                               "token": "a" * 32}], [dict(LINK)]],
             "context": {"project_id": 1}, "mentions": "silent"}
    parts["manifest"] = [{"part_id": "card", "kind": "card", "index": 0,
                          "sha256": hashlib.sha256(envelopes.canonical({
                              "containers": parts["containers"],
                              "footer": parts["footer"],
                              "action_rows": parts["action_rows"]}))
                          .hexdigest()}]
    delivery = {"application_id": "1", "channel_id": "42",
                "route_epoch": 1, "correlation": "c" * 32,
                "intent_event_ids": [1]}
    if slack:
        delivery.update(transport="slack", team_id="T1", profile="p")
    else:
        delivery["guild_id"] = "7"
    return {"schema": "mcs-card-render/v2" if slack
            else "mcs-card-render/v1",
            "delivery_id": "00000000-0000-4000-8000-000000000001",
            "card_key": "v1|thread|1|100", "kind": "thread", "op": "create",
            "render_rev": 1, "source_generation": 1,
            "presentation_generation": 1, "ui_revision": 1,
            "delivery": delivery, "parts": parts}


def test_validator_accepts_link_and_silent_mentions():
    spec = _spec()
    assert spec_mod.validate(spec) is spec
    # link buttons carry no token — nothing enters the token registry
    assert list(spec_mod.token_map(spec)) == ["a" * 32]


@pytest.mark.parametrize("mutate,error", [
    (lambda s: s["parts"].update(mentions="loud"), "bad_mentions"),
    (lambda s: s["parts"]["action_rows"][1][0].update(
        url="http://example.invalid"), "bad_button_url"),
    (lambda s: s["parts"]["action_rows"][1][0].pop("url"),
     "bad_button_url"),
])
def test_validator_rejects_bad_link_or_mentions(mutate, error):
    spec = _spec()
    mutate(spec)
    with pytest.raises(ValueError, match=error):
        spec_mod.validate(spec)


def test_outdated_worker_key_set_refuses_new_specs(monkeypatch):
    """A worker that predates these keys rejects the spec (held) rather
    than sending live mentions or a broken link button."""
    old = spec_mod.PARTS_KEYS - {"mentions"}
    monkeypatch.setattr(spec_mod, "PARTS_KEYS", old)
    with pytest.raises(ValueError, match="unsupported_parts_key"):
        spec_mod.validate(_spec())


def test_discord_view_renders_link_and_every_footer_line(monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    view = discord_cards.build_view(_spec())
    inner = view.items[0].children
    face = "\n".join(c.content for c in inner if hasattr(c, "content"))
    assert "-# ✅ 確認: <@1001>・<@U0AB12CD>" in face
    assert "-# 📝 一件目 — 担当 山田\n-# 📝 他1件 & <b>" in face
    link = inner[-1].children[0]
    assert (link.style, link.url, link.custom_id) == (
        5, LINK["url"], None)
    none = discord_cards.no_pings()
    assert not (none.everyone or none.users or none.roles
                or none.replied_user)


def test_slack_footer_names_members_and_escapes_the_rest():
    _, blocks = slack_cards.render(_spec(slack=True))
    context = [b["elements"][0] for b in blocks if b["type"] == "context"]
    # only a Slack-shaped user mention stays live; anything else is
    # escaped to literal text
    assert context[0] == {"type": "mrkdwn",
                          "text": "✅ 確認: &lt;@1001&gt;・<@U0AB12CD>"}
    assert context[1]["text"] == "📝 一件目 — 担当 山田\n📝 他1件 &amp; &lt;b&gt;"
    actions = [b for b in blocks if b["type"] == "actions"]
    link = actions[1]["elements"][0]
    assert link == {"type": "button", "action_id": slack_cards.LINK_ACTION,
                    "text": {"type": "plain_text", "text": "🔗 MCSで開く"},
                    "url": LINK["url"]}


def test_slack_footer_stays_plain_without_mentions():
    spec = _spec(footer=("<!channel> 取り込み",), slack=True)
    spec["parts"].pop("mentions")
    spec["parts"]["manifest"][0]["sha256"] = hashlib.sha256(
        envelopes.canonical({k: spec["parts"][k] for k in (
            "containers", "footer", "action_rows")})).hexdigest()
    _, blocks = slack_cards.render(spec)
    ctx = next(b for b in blocks if b["type"] == "context")["elements"][0]
    assert ctx == {"type": "plain_text", "text": "<!channel> 取り込み"}


def test_slack_mrkdwn_never_lets_text_form_a_broadcast():
    assert slack_cards._mrkdwn("<!channel> <@U1ABC> <@here>") \
        == "&lt;!channel&gt; <@U1ABC> &lt;@here&gt;"


# ---------- shared modal definitions ------------------------------------

def test_task_fields_with_and_without_roster():
    bare = text.modal_fields("request", None, "佐藤")
    assert [f["id"] for f in bare] == ["task", "assignee", "due_date"]
    assert bare[1]["default"] == "佐藤" and bare[0]["default"] == ""
    roster = text.modal_fields(
        "request", {"hint": "残薬確認", "staff": ["佐藤（みどり薬局）",
                                               "x" * 76]}, "佐藤")
    assert [f["id"] for f in roster] == ["task", "assignee_pick",
                                        "assignee", "due_date"]
    assert roster[0]["default"] == "残薬確認"
    # an over-long name can never become an option (Slack 75 chars)
    assert roster[1]["options"] == [("佐藤（みどり薬局）", "佐藤（みどり薬局）")]
    assert roster[1]["default"] == "佐藤（みどり薬局）"
    assert text.modal_fields("defer") == []


@pytest.mark.parametrize("fields,expect", [
    ({"task": " 残薬確認 ", "assignee_pick": "佐藤（みどり薬局）"},
     {"title": "残薬確認", "assignee": "佐藤（みどり薬局）",
      "reason": text.TASK_REASON}),
    ({"task": "t", "assignee": "手入力", "assignee_pick": "佐藤"},
     {"title": "t", "assignee": "手入力", "reason": text.TASK_REASON}),
    ({"task": "t", "due_date": "2026-10-01"},
     {"title": "t", "due_date": "2026-10-01", "reason": text.TASK_REASON}),
    # an old 依頼 form submitted across a restart still validates
    ({"title": "旧件名", "reason": "旧理由"},
     {"title": "旧件名", "reason": "旧理由"}),
    ({"task": ""}, "タスク内容の入力が必要です。"),
    ({"task": "t", "due_date": "2026-02-30"},
     "期限は YYYY-MM-DD 形式で入力してください。"),
    ({"task": "t", "assignee": "a" * 121},
     "担当者は120文字以内で入力してください。"),
])
def test_task_attrs(fields, expect):
    assert text.task_attrs(copy.deepcopy(fields)) == expect


def test_feedback_attrs():
    assert text.feedback_attrs({"field": "meds", "note": " 量 "}) \
        == ("meds", "量")
    assert text.feedback_attrs({"field": "vitals"}) \
        == ("vitals", "抽出の誤り報告（バイタル）")
    assert text.feedback_attrs({"field": "x"}) \
        == "誤っている箇所を選択してください。"
