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


def test_stamp_controls_show_six_distinct_visible_illustrations_and_names():
    blocks = render(_stamps_spec())[1]
    buttons = [e for b in blocks if b["type"] == "actions"
               for e in b["elements"] if e["type"] == "button"]
    assert [b["text"]["text"] for b in buttons] == [
        "👀見ました", "🙆承知 ✓", "🙏感謝", "👍いいね", "✅完了", "🗂 別の投稿"]
    assert not any(b["type"] == "context_actions" for b in blocks)


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
    menu, compact = [b for b in blocks if b["type"] == "actions"]
    select, = menu["elements"]
    assert select["type"] == "static_select"
    assert [o["value"] for o in select["options"]] == ["b" * 32, "c" * 32]
    buttons = compact["elements"]
    assert len(buttons) == 6 and all(b["type"] == "button" for b in buttons)
    assert [b["value"] for b in buttons] == [f"{i:032x}" for i in range(1, 6)] + ["d" * 32]
    assert all(b["text"]["type"] == "plain_text" and 0 < len(b["accessibility_label"]) <= 75
               for b in buttons)
    assert "取消" in buttons[1]["accessibility_label"]
    assert buttons[1]["style"] == "primary" and "✓" in buttons[1]["text"]["text"]
    assert "見ました" in buttons[0]["accessibility_label"]
    body, _ = click()
    for button in buttons:
        origin = _origin(body, button)
        assert origin and origin["token"] == button["value"]
    spec["render_rev"] += 1
    newer, = [b for b in render(spec)[1] if b.get("block_id", "").startswith("mcs:stamps:")]
    assert compact["block_id"] != newer["block_id"]


@pytest.mark.parametrize("without", ["form", "one_stamp"])
def test_incomplete_older_specs_keep_every_action(without):
    spec = _stamps_spec()
    if without == "form":
        spec["parts"]["action_rows"][0].pop()
    else:
        spec["parts"]["action_rows"][1].pop()
    _, blocks = render(spec)
    assert not any(b["type"] == "context_actions" for b in blocks)
    controls = [e for b in blocks if b["type"] == "actions" for e in b["elements"]]
    actual = [o["value"] for e in controls if e["type"] == "static_select" for o in e["options"]]
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


@pytest.mark.parametrize("wire", ["legacy_positive", "legacy_negative", "button"])
def test_stamp_click_queues_existing_command_with_click_time(tmp_path, wire):
    async def run():
        actions, app, _, dirs = fixture(tmp_path, kind="stamp_press")
        body, _ = click()
        action = ({"type": "button", "action_id": "mcs:a:" + TOKEN, "value": TOKEN}
                  if wire == "button" else _pair_action(second=wire == "legacy_negative"))
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
def test_compact_click_cannot_bypass_the_pinned_registry(tmp_path, denial):
    async def run():
        actions, _, _, dirs = fixture(
            tmp_path, kind="ack" if denial == "wrong_kind" else "stamp_press",
            project_ids={999} if denial == "wrong_patient" else {123})
        body, _ = click(ts="1790000000.000002" if denial == "wrong_post" else "1790000000.000001")
        await actions._action(ack, body, _pair_action("d" * 32 if denial == "unknown_token" else TOKEN))
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
    controls = [e for b in payload["blocks"] if b["type"] == "actions" for e in b["elements"]]
    menu, = [e for e in controls if e["type"] == "static_select"]
    assert [o["value"] for o in menu["options"]] == ["b" * 32, "c" * 32]
    stamps = [e for e in controls if e["type"] == "button"]
    assert [e["value"] for e in stamps] == [f"{i:032x}" for i in range(1, 6)] + ["d" * 32]
    assert stamps[1]["style"] == "primary"
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
