"""Visible stamp controls and legacy compact clicks keep token and scope checks."""

import asyncio
import math
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from adapters.slack.actions import parse_action_origin
from adapters.slack.cards import render
from adapters.slack import cards, delivery
from slack_actions_testkit import TOKEN, ack, click, command, fixture
from slack_card_testkit import _spec
from slack_testkit import FakeClient


def _stamps_spec():
    spec = _spec()
    spec["parts"]["action_rows"][0].append({
        "id": "stamp", "ui": "button", "label": "MCSスタンプ", "token": "d" * 32})
    spec["parts"]["action_rows"].append([
        {"id": "stamp_press", "ui": "button", "label": label,
         "token": f"{i:032x}", "style": "primary" if i == 2 else "secondary"}
        for i, label in enumerate(("👀見ました", "🙆承知 ✓", "🙏感謝", "👍いいね", "✅完了"), 1)])
    return spec


def _stamp_control(blocks):
    return next(b for b in blocks if b.get("block_id", "").startswith("mcs:stamps:"))


def test_stamp_controls_expand_one_menu_with_six_distinct_names():
    blocks = render(_stamps_spec())[1]
    control = _stamp_control(blocks)
    menu = control["elements"][0]
    assert control["type"] == "actions" and "text" not in control
    assert menu["type"] == "static_select"
    assert menu["placeholder"]["text"] == "スタンプを押す…"
    assert [o["text"]["text"] for o in menu["options"]] == [
        "🙆承知 ✓（押印済み）", "👀見ました", "🙆承知 ✓（取消）", "🙏感謝", "👍いいね", "✅完了",
        "🗂 別の投稿"]
    assert all(o["text"]["type"] == "plain_text" for o in menu["options"])
    # the stamp already pressed (also from MCS itself) shows as the menu's selection
    assert menu["initial_option"] == menu["options"][0]
    assert menu["initial_option"]["value"] == cards.OWN_STAMP_VALUE
    assert not any(b["type"] == "context_actions" for b in blocks)
    assert not any(e["type"] == "button" for b in blocks if b["type"] == "actions"
                   for e in b["elements"])


def test_stamp_menu_without_an_own_stamp_has_no_selection():
    spec = _stamps_spec()
    for b in spec["parts"]["action_rows"][-1]:
        b["style"], b["label"] = "secondary", b["label"].replace(" ✓", "")
    menu = _stamp_control(render(spec)[1])["elements"][0]
    assert "initial_option" not in menu and len(menu["options"]) == 6


def test_selecting_the_display_only_own_stamp_option_sends_nothing():
    body, _ = click()
    assert _origin(body, {"type": "static_select", "action_id": "mcs:stamps-menu",
                          "selected_option": {"value": cards.OWN_STAMP_VALUE}}) is None


def _pair_action(token=TOKEN, *, second=False):
    pair = ("c" * 32, token) if second else (token, "c" * 32)
    return {"type": "feedback_buttons", "action_id": "mcs:stamp-pair:" + ":".join(pair),
            "value": token}


def _origin(body, action):
    return parse_action_origin(body, action, team_id="T_SYNTHETIC",
                               application_id="A_SYNTHETIC", channel_id="C_SYNTHETIC",
                               profile="cco", allowed_user_ids={"U_OPERATOR"})


def test_compact_controls_preserve_all_tokens_and_observed_own_state():
    spec = _stamps_spec()
    _, blocks = render(spec)
    ordinary, = [b for b in blocks if b["type"] == "actions"
                  and b["elements"][0]["action_id"] == cards.MENU_ACTION]
    select, = ordinary["elements"]
    assert [o["value"] for o in select["options"]] == ["b" * 32, "c" * 32]
    control = _stamp_control(blocks)
    menu = control["elements"][0]
    assert [o["value"] for o in menu["options"]] == [
        cards.OWN_STAMP_VALUE, *(f"{i:032x}" for i in range(1, 6)), "d" * 32]
    body, _ = click()
    for option in menu["options"][1:]:
        origin = _origin(body, {**menu, "selected_option": option})
        assert origin and origin["token"] == option["value"]
    assert blocks.index(control) < blocks.index(ordinary)
    spec["render_rev"] += 1
    assert control["block_id"] != _stamp_control(render(spec)[1])["block_id"]


def test_observed_stamps_and_send_menu_share_one_area_after_the_originals():
    spec = _stamps_spec()
    spec["parts"]["containers"] += [
        {"type": "text", "rule": True, "text": "10-01 09:00 合成の根元投稿"},
        {"type": "text", "text": "合成要点1"},
        {"type": "text", "rule": True, "text": "10-01 09:10 合成の返信"},
        {"type": "text", "text": "合成要点2"}]
    spec["parts"]["slack"] = {"face_posts": [
        {"at": 3, "original": "合成本文1", "stamps": ["スタンプ 👀2 合成甲・合成乙 · 観測 10:00"]},
        {"at": 5, "original": "合成本文2", "stamps": ["スタンプ 🙆1 · 自分 🙆 · 再取得失敗"]}]}
    blocks = render(spec)[1]
    fold_end = max(i for i, b in enumerate(blocks) if b["type"] == "container")
    stamp_at = [i for i, b in enumerate(blocks) if b["type"] == "context"
                and "スタンプ" in b["elements"][0]["text"]]
    control_at = blocks.index(_stamp_control(blocks))
    assert len(stamp_at) == 2 and fold_end < min(stamp_at) < max(stamp_at) < control_at
    assert "合成の根元投稿" in blocks[stamp_at[0]]["elements"][0]["text"]
    assert "合成の返信" in blocks[stamp_at[1]]["elements"][0]["text"]
    assert "再取得失敗" in blocks[stamp_at[1]]["elements"][0]["text"]
    assert blocks[control_at + 1]["type"] == "divider"



@pytest.mark.parametrize("without", ["form", "one_stamp"])
def test_incomplete_older_specs_keep_every_action(without):
    spec = _stamps_spec()
    if without == "form":
        spec["parts"]["action_rows"][0].pop()
    else:
        spec["parts"]["action_rows"][1].pop()
    _, blocks = render(spec)
    assert not any(b["type"] == "context_actions" for b in blocks)
    controls = [e for b in blocks for e in (b.get("elements", []) if b["type"] == "actions" else [b["accessory"]] if "accessory" in b else [])]
    actual = [o["value"] for e in controls if e["type"] == "static_select" for o in e["options"]
              if o["value"] != cards.OWN_STAMP_VALUE]
    actual += [e["value"] for e in controls if e["type"] == "button"]
    expected = [e["token"] for row in spec["parts"]["action_rows"] for e in row]
    assert sorted(actual) == sorted(expected)


@pytest.mark.parametrize("bad", [
    {"value": "d" * 32}, {"value": None}, {"value": {}},
    {"action_id": "mcs:stamp-pair:" + TOKEN + ":" + TOKEN},
    {"action_id": "mcs:stamp-pair:" + TOKEN + ":bad"},
    {"action_id": "mcs:a:" + TOKEN}, {"action_id": None},
    {"type": "button"},
])
def test_compact_pair_rejects_forged_values_and_routes(bad):
    body, _ = click()
    assert _origin(body, {**_pair_action(), **bad}) is None


@pytest.mark.parametrize("field,value", [
    ("team", {"id": "T_OTHER"}), ("api_app_id", "A_OTHER"),
    ("channel", {"id": "C_OTHER"}), ("user", {"id": "U_OTHER"}),
    ("message", {"ts": "not-a-message"}),
])
def test_compact_click_keeps_native_origin_checks(field, value):
    body, _ = click()
    assert _origin({**body, field: value}, _pair_action()) is None


@pytest.mark.parametrize("wire", ["legacy_positive", "legacy_negative", "button", "menu"])
def test_stamp_click_queues_existing_command_with_click_time(tmp_path, wire):
    async def run():
        actions, app, _, dirs = fixture(tmp_path, kind="stamp_press")
        body, _ = click()
        action = ({"type": "button", "action_id": "mcs:a:" + TOKEN, "value": TOKEN}
                  if wire == "button" else {"type": "static_select", "action_id": "mcs:stamps-menu",
                                         "selected_option": {"value": TOKEN}} if wire == "menu"
                  else _pair_action(second=wire == "legacy_negative"))
        handler, = [fn for pattern, fn in app.actions.items()
                    if re.fullmatch(pattern, action["action_id"])]
        await handler(ack, body, action)
        env = command(dirs)
        assert env["token"] == TOKEN and env["actor"] == "slack:T_SYNTHETIC:U_OPERATOR"
        assert env["origin"]["message_id"] == body["message"]["ts"]
        assert math.isfinite(env["clicked_at"]) and env["clicked_at"] > 0
        assert not app.client.views
        assert actions._active
    asyncio.run(run())


@pytest.mark.parametrize("denial", ["wrong_post", "wrong_patient", "wrong_kind", "unknown_token"])
@pytest.mark.parametrize("wire", ["pair", "menu"])
def test_compact_click_cannot_bypass_the_pinned_registry(tmp_path, denial, wire):
    async def run():
        actions, _, _, dirs = fixture(
            tmp_path, kind="ack" if denial == "wrong_kind" else "stamp_press",
            project_ids={999} if denial == "wrong_patient" else {123})
        body, _ = click(ts="1790000000.000002" if denial == "wrong_post" else "1790000000.000001")
        token = "d" * 32 if denial == "unknown_token" else TOKEN
        action = (_pair_action(token) if wire == "pair" else
                  {"type": "static_select", "action_id": "mcs:stamps-menu", "selected_option": {"value": token}})
        await actions._action(ack, body, action)
        assert not list(Path(dirs["cmd_int"]).glob("*.json"))
    asyncio.run(run())


def _slack_error(status=200, error="invalid_blocks"):
    exc = RuntimeError("synthetic Slack rejection")
    exc.response = SimpleNamespace(status_code=status, data={"ok": False, "error": error})
    return exc


def _deliver_card(failures, *, op="create", thread=False, gate=None, spec=None):
    async def run():
        client = FakeClient()
        pending = list(failures)
        method = "chat_update" if op == "update" else "chat_postMessage"
        original = getattr(client, method)

        async def send(**kwargs):
            if pending:
                client.calls.append((op, kwargs))
                raise pending.pop(0)
            return await original(**kwargs)

        setattr(client, method, send)
        adapter = delivery.SlackCardAdapter(SimpleNamespace(client=client),
            team_id="T_SYNTHETIC", application_id="A_SYNTHETIC",
            channel_id="C_SYNTHETIC", profile="cco")
        assert await adapter.bind()
        card = spec if spec is not None else _stamps_spec()
        card["op"] = op
        if op == "update":
            card["delivery"]["message_id"] = "1790000000.000001"
        if thread:
            card["kind"] = "signal"
            card["parts"]["source_thread"] = True
            card["delivery"]["thread_id"] = "1790000000.000002"
        token = delivery.RETRY_ALLOWED.set(gate)
        try:
            outcome = await adapter.perform(card)
        finally:
            delivery.RETRY_ALLOWED.reset(token)
        return outcome, [(kind, kw) for kind, kw in client.calls if kind in {"create", "update"}]
    return asyncio.run(run())


@pytest.mark.parametrize("op", ["create", "update"])
@pytest.mark.parametrize("thread", [False, True])
@pytest.mark.parametrize("rejected", [False, True])
def test_visible_controls_keep_the_target_and_never_fallback(op, thread, rejected, caplog):
    outcome, calls = _deliver_card([_slack_error()] if rejected else [], op=op, thread=thread)
    assert outcome["result"] == ("not_sent" if rejected else "delivered")
    assert len(calls) == 1 and calls[0][0] == op
    payload = calls[0][1]
    assert not any(b["type"] == "context_actions" for b in payload["blocks"])
    ordinary, = [b for b in payload["blocks"] if b["type"] == "actions"
                  and b["elements"][0]["action_id"] == cards.MENU_ACTION]
    menu, = ordinary["elements"]
    assert [o["value"] for o in menu["options"]] == ["b" * 32, "c" * 32]
    stamps = _stamp_control(payload["blocks"])["elements"][0]["options"]
    assert [o["value"] for o in stamps] == [
        cards.OWN_STAMP_VALUE, *(f"{i:032x}" for i in range(1, 6)), "d" * 32]
    assert "取消" in stamps[2]["text"]["text"] and "押印済み" in stamps[0]["text"]["text"]
    assert payload["blocks"] == render(_stamps_spec())[1]
    if op == "update":
        assert payload["ts"] == "1790000000.000001"
    elif thread:
        assert payload["thread_ts"] == "1790000000.000002"
    assert "合成カード" not in caplog.text


@pytest.mark.parametrize("failure,result", [
    (TimeoutError("synthetic"), "unknown"),
    (_slack_error(500), "unknown"), (_slack_error(None), "unknown"),
    (_slack_error(200, "internal_error"), "unknown"),
    (_slack_error(403, "missing_scope"), "not_sent"),
])
def test_uncertain_or_other_failure_never_retries_visible_card(failure, result):
    outcome, calls = _deliver_card([failure])
    assert outcome["result"] == result and len(calls) == 1


def test_rejection_does_not_send_again_when_authority_is_revoked():
    async def denied():
        return False

    outcome, calls = _deliver_card([_slack_error()], gate=denied)
    assert outcome == {"result": "not_sent", "error_code": "invalid_blocks"}
    assert len(calls) == 1


def test_legacy_card_rejection_does_not_retry_or_drop_its_blocks():
    outcome, calls = _deliver_card([_slack_error()], spec=_spec())
    assert outcome == {"result": "not_sent", "error_code": "invalid_blocks"}
    assert len(calls) == 1 and calls[0][1]["blocks"]


def test_visible_controls_that_exceed_block_budget_are_not_sent(monkeypatch):
    spec = _stamps_spec()
    spec["parts"]["action_rows"][0] = [spec["parts"]["action_rows"][0][-1]]
    monkeypatch.setattr(cards, "BLOCK_MAX", len(render(spec)[1]) - 1)
    outcome, calls = _deliver_card([_slack_error()], spec=spec)
    assert outcome == {"result": "not_sent", "error_code": "bad_render"}
    assert calls == []


@pytest.mark.parametrize("op", ["create", "update"])
def test_card_attempts_propagate_cancellation(op):
    with pytest.raises(asyncio.CancelledError):
        _deliver_card([asyncio.CancelledError()], op=op)


@pytest.mark.parametrize("option", [None, {}, {"value": "bad"}, {"value": None}, "bad"])
def test_stamp_menu_rejects_malformed_selection(option):
    body, _ = click()
    assert _origin(body, {"type": "static_select", "action_id": "mcs:stamps-menu",
                          "selected_option": option}) is None
