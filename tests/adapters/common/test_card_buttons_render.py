"""Card button surfaces in both transports: link buttons, silent member
mentions, the spec keys an outdated worker must refuse. Synthetic only."""
from __future__ import annotations

import copy
import hashlib
import sys

import pytest

from adapters.common import envelopes, spec as spec_mod, text
from adapters.discord import cards as discord_cards
from adapters.lineworks import cards as lineworks_cards
from adapters.slack import cards as slack_cards, presentation as slack_presentation
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


@pytest.mark.parametrize("rows", ["missing", None])
def test_drug_view_navigation_without_action_rows_is_a_validation_error(rows):
    """A manifest-less spec flagging drug_view_navigation with no action
    rows is rejected with ValueError — the only exception _scan_specs
    skips — instead of KeyError/TypeError aborting the worker tick."""
    spec = _spec()
    parts = spec["parts"]
    del parts["manifest"]
    if rows == "missing":
        del parts["action_rows"]
    else:
        parts["action_rows"] = rows
    parts["drug_view_navigation"] = True
    with pytest.raises(ValueError, match="bad_drug_view_navigation"):
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
    assert "-# 📝 一件目 — 担当 山田\n-# 📝 他1件 & ＜b>" in face
    # the link button closes the primary row
    link = inner[-1].children[-1]
    assert (link.style, link.url, link.custom_id) == (
        5, LINK["url"], None)
    none = discord_cards.no_pings()
    assert not (none.everyone or none.users or none.roles
                or none.replied_user)


def test_slack_footer_never_carries_mention_syntax():
    """Slack has no allowed_mentions — a re-posted card with <@U…>
    would ping. Mentions become display names or a neutral label and
    the footer is plain_text."""
    _, blocks = slack_cards.render(_spec(slack=True))
    context = [b["elements"][0] for b in blocks if b["type"] == "context"]
    assert context[0] == {"type": "plain_text",
                          "text": "✅ 確認: <@1001>・メンバー"}
    assert context[1] == {"type": "plain_text",
                          "text": "📝 一件目 — 担当 山田\n📝 他1件 & <b>"}
    _, blocks = slack_cards.render(_spec(slack=True), {"U0AB12CD": "佐藤"})
    ctx = next(b for b in blocks if b["type"] == "context")["elements"][0]
    assert ctx["text"] == "✅ 確認: <@1001>・佐藤"
    assert all("<@U" not in b["elements"][0]["text"]
               for b in blocks if b["type"] == "context")
    # the MCS link is a text link (no full-width button on mobile)
    links = [b["elements"][0] for b in blocks if b["type"] == "context"
             and b["elements"][0]["type"] == "mrkdwn"]
    assert links == [{"type": "mrkdwn",
                      "text": f"<{LINK['url']}|🔗 MCSで開く>"}]


def test_slack_footer_stays_plain_without_mentions():
    spec = _spec(footer=("<!channel> 取り込み",), slack=True)
    spec["parts"].pop("mentions")
    spec["parts"]["manifest"][0]["sha256"] = hashlib.sha256(
        envelopes.canonical({k: spec["parts"][k] for k in (
            "containers", "footer", "action_rows")})).hexdigest()
    _, blocks = slack_cards.render(spec)
    ctx = next(b for b in blocks if b["type"] == "context")["elements"][0]
    assert ctx == {"type": "plain_text", "text": "<!channel> 取り込み"}


def test_slack_mention_ids_reads_footer_only():
    assert slack_presentation.mention_ids(_spec(slack=True)) == {"U0AB12CD"}


@pytest.mark.parametrize("kind,secondary", [
    ("thread", ["body", "tasks", "summary", "search", "report", "meds", "drugsearch", "prev", "next"]),
    ("signal", ["body", "tasks", "summary", "search", "report", "dismiss", "meds", "drugsearch", "prev", "next"]),
    ("digest", ["body", "prev", "next", "digest", "mytasks", "unacked"]),
])
def test_secondary_action_sets_reach_all_three_transport_menus(monkeypatch, kind, secondary):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    spec = _spec(footer=())
    spec["kind"] = kind
    spec["parts"].pop("manifest")
    primary = ["ack", "assign"] + ([] if kind == "digest" else ["request"])
    buttons = [{"id": action, "ui": "button", "style": "secondary",
                "label": action, "token": f"{index:032x}"}
               for index, action in enumerate(primary + secondary, 1)]
    if kind != "digest":
        buttons.append(dict(LINK))
    spec["parts"]["action_rows"] = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    tokens = {b["token"]: b["id"] for b in buttons if b.get("token")}

    slack_spec = copy.deepcopy(spec)
    slack_spec["schema"] = "mcs-card-render/v2"
    slack_spec["delivery"].pop("guild_id")
    slack_spec["delivery"].update(transport="slack", team_id="T1", profile="p")
    _, blocks = slack_cards.render(slack_spec)
    menu = next(element for block in blocks if block["type"] == "actions"
                for element in block["elements"] if element["type"] == "static_select")
    # every transport: 確認・担当・タスク作成 lead the one 操作 menu, as on Slack
    assert [tokens[option["value"]] for option in menu["options"]] == primary + secondary
    assert any(block["type"] == "context" and block["elements"][0]["type"] == "mrkdwn"
               for block in blocks) == (kind != "digest")

    _, options = discord_cards._button_items(spec)
    assert [tokens[option.value] for option in options] == primary + secondary
    spec["parts"]["thread_drug_actions"] = True
    _, options = discord_cards._button_items(spec)
    assert [tokens[option.value] for option in options] == primary + [
        action for action in secondary if action not in {"meds", "drugsearch"}]
    thread = discord_cards.thread_drug_view(spec)
    assert ([] if thread is None else [tokens[b.custom_id.removeprefix("mcs:a:")]
                                      for b in thread.items]) == [
        action for action in secondary if action in {"meds", "drugsearch"}]

    spec["parts"]["action_rows"].append([
        {"id": "more", "ui": "button", "label": "その他の操作", "token": "f" * 32}])
    menu = lineworks_cards.secondary(spec)
    assert [button["id"] for button in menu] == primary + secondary
    assert [button["label"] for button in lineworks_cards.action_buttons(spec)] == (
        ["操作を選ぶ…"] + ([] if kind == "digest" else [LINK["label"]]))


def test_dismiss_preview_names_the_code():
    ctx = {"signals": {"k": {"project_id": 1, "artifact_id": 2}}}
    env = envelopes.signal_dismiss("discord:1", ctx, "k", "誤検知",
                                   "false_positive")
    assert env["reason_code"] == "false_positive"
    assert "区分: 誤検知" in text.preview_text("dismiss", env, True)
    old = envelopes.signal_dismiss("discord:1", ctx, "k", "理由")
    assert "reason_code" not in old
    assert "区分" not in text.preview_text("dismiss", old, False)


def test_input_folds_into_command_id_and_validates():
    import notify_cmds
    origin = {"application_id": "1", "channel_id": "42", "message_id": "9"}
    plain = envelopes.notification("a" * 32, "discord:1", origin)
    q1 = envelopes.notification("a" * 32, "discord:1", origin,
                                {"query": "発熱"})
    q2 = envelopes.notification("a" * 32, "discord:1", origin,
                                {"query": "咳"})
    assert "input" not in plain
    assert len({plain["command_id"], q1["command_id"], q2["command_id"]}) == 3
    for env in (plain, q1, q2):
        assert notify_cmds.validate_int(env) is None


def test_list_messages_filters_scope_and_caps():
    items = [{"project_id": 1 if i % 2 else 2, "group": f"患者{i // 4}",
              "text": f"・item {i}"} for i in range(40)]
    result = {"list": {"title": "🗂 未確認一覧", "head": ["未確認 40件"],
                       "items": items, "more": 5, "empty": "なし",
                       "notes": ["※ 記録された状態です"]}}
    out = "\n".join(text.list_messages(result, lambda pid: pid == 1))
    shown = [ln for ln in out.splitlines() if ln.startswith("・item")]
    assert len(shown) == text.LIST_SHOW
    assert all(int(ln.split()[-1]) % 2 for ln in shown)   # project 1 only
    assert "他10件" in out                  # 20 in scope - 15 + 5 overflow
    assert out.startswith("**🗂 未確認一覧**\n未確認 40件\n■ 患者0")
    assert out.rstrip().endswith("※ 記録された状態です")
    empty = text.list_messages({"list": {**result["list"], "items": [],
                                         "more": 0}}, lambda pid: True)
    assert "なし" in empty[0] and "他" not in empty[0]


def _layout_spec():
    spec = _spec()
    parts = spec["parts"]
    parts["containers"] = [
        {"type": "heading", "text": "💬 合成 花子 様"},
        {"type": "text", "text": "10-01 09:40 合成さん", "rule": True},
        {"type": "text", "text": "**太字** [x](https://e.invalid) # 見出し"},
        {"type": "text", "text": "10-01 10:00 合成さん", "rule": True},
        {"type": "text", "text": "📋 要約\n・合成"}]
    parts["action_rows"] = [[
        {"id": i, "ui": "button", "style": "secondary", "label": f"L{i}",
         "token": f"{n:032x}"}
        for n, i in enumerate(("ack", "assign", "request", "body"))],
        [{"id": "summary", "ui": "button", "style": "secondary",
          "label": "Lsummary", "token": "f" * 32}, dict(LINK)]]
    return spec


def test_discord_view_zones_separators_and_menu(monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    inner = discord_cards.build_view(_layout_spec()).items[0].children
    kinds = [type(c).__name__ for c in inner]
    # heading | message 1 | message 2 | footer | rows
    assert kinds == ["TextDisplay", "Separator", "TextDisplay", "Separator",
                     "TextDisplay", "Separator", "TextDisplay",
                     "Separator", "ActionRow", "ActionRow"]
    assert all(c.visible and c.spacing == 1 for c in inner
               if type(c).__name__ == "Separator")
    assert inner[6].content.startswith("-# ")
    # the Slack order: the 操作を選ぶ… select first, then the MCSで開く link
    menu_row, link_row = inner[-2].children, inner[-1].children
    (menu,) = menu_row
    assert menu.custom_id == "mcs:menu" and menu.placeholder == "操作を選ぶ…"
    assert [(o.label, o.value) for o in menu.options] == [
        ("Lack", f"{0:032x}"), ("Lassign", f"{1:032x}"), ("Lrequest", f"{2:032x}"),
        ("Lbody", f"{3:032x}"), ("Lsummary", "f" * 32)]
    (link,) = link_row
    assert link.url == LINK["url"]


def test_discord_escape_is_literal_and_length_preserving():
    raw = ("# 見出し\n-# 小\n> 引用\n- 項目\n1. 番号\n"
           "**太** _斜_ ~~消~~ ||伏|| `c` [x](https://e.invalid/a_b) "
           "<@1001> <t:1:R>")
    out = discord_cards.escape_md(raw)
    assert len(out) == len(raw)
    assert out.splitlines()[:5] == ["＃ 見出し", "－# 小", "＞ 引用",
                                    "－ 項目", "1． 番号"]
    for ch in "*_~|`[]":
        assert ch not in out.replace("https://e.invalid/a_b", "")
    # URLs and runner mentions stay usable; other tags are inert
    assert "https://e.invalid/a_b" in out and "<@1001>" in out
    assert "＜t:1:R>" in out


def test_discord_card_text_is_escaped_but_prefixes_are_not(monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    inner = discord_cards.build_view(_layout_spec()).items[0].children
    face = "\n".join(c.content for c in inner if hasattr(c, "content"))
    assert face.startswith("## 💬")
    assert "＊＊太字＊＊ ［x］(https://e.invalid) # 見出し" in face
