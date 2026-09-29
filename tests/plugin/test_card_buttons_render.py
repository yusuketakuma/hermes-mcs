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


def test_slack_mention_ids_reads_footer_only():
    assert slack_cards.mention_ids(_spec(slack=True)) == {"U0AB12CD"}


# ---------- shared modal definitions ------------------------------------

def test_task_fields_with_and_without_roster():
    bare = text.modal_fields("request", None, "佐藤")
    assert [f["id"] for f in bare] == ["task", "assignee", "due_date",
                                       "reason"]
    assert bare[-1]["required"] is False and bare[-1]["default"] == ""
    assert bare[1]["default"] == "佐藤" and bare[0]["default"] == ""
    roster = text.modal_fields(
        "request", {"hint": "残薬確認", "staff": ["佐藤（みどり薬局）",
                                               "x" * 76]}, "佐藤")
    # Discord modals hold at most 5 components
    assert [f["id"] for f in roster] == ["task", "assignee_pick",
                                        "assignee", "due_date", "reason"]
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
    ({"task": "t", "reason": " 家族から依頼 "},
     {"title": "t", "reason": "家族から依頼"}),
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


# ---------- 🚫 reason codes / 🔎 / list views ------------------------------

def test_dismiss_reasons_match_the_runner_vocabulary():
    import mcs_operations
    assert tuple(v for v, _ in text.DISMISS_REASONS) \
        == mcs_operations.DISMISS_REASON_CODES
    fields = text.modal_fields("dismiss")
    assert [f["id"] for f in fields] == ["reason_code", "note"]
    assert fields[0]["required"] and fields[0]["options"]


@pytest.mark.parametrize("fields,expect", [
    ({"reason_code": "duplicate", "note": " 同じ件 "}, ("同じ件", "duplicate")),
    ({"reason_code": "false_positive", "note": ""}, ("誤検知", "false_positive")),
    ({"reason_code": "", "note": "x"}, "却下理由を選択してください。"),
    ({"reason_code": "nope"}, "却下理由を選択してください。"),
    # a modal opened before the upgrade: free text only, no code
    ({"reason": "旧理由"}, ("旧理由", None)),
    ({"reason": ""}, "理由の入力が必要です。"),
])
def test_dismiss_attrs(fields, expect):
    assert text.dismiss_attrs(fields) == expect


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


def test_search_query_normalizes():
    assert text.search_query({"query": "  発熱　 咳 "}) == "発熱 咳"
    assert text.search_query({"query": "  "}) is None
    assert [f["id"] for f in text.modal_fields("search")] == ["query"]


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
