"""Compact Slack controls keep the existing token, scope and stamp paths."""

import asyncio
import math
from pathlib import Path
import re

import pytest

from adapters.slack.actions import parse_action_origin
from adapters.slack.cards import render
from slack_actions_testkit import TOKEN, ack, click, command, fixture
from slack_card_testkit import _spec


def _stamps_spec():
    spec = _spec()
    spec["parts"]["action_rows"][0].append({
        "id": "stamp", "ui": "button", "label": "MCSスタンプ", "token": "d" * 32})
    spec["parts"]["action_rows"].append([
        {"id": "stamp_press", "ui": "button", "label": label,
         "token": f"{i:032x}", "style": "primary" if i == 2 else "secondary"}
        for i, label in enumerate(("👀見ました", "🙆承知 ✓", "🙏感謝", "👍いいね", "✅完了"), 1)])
    return spec


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
    menu, = [b for b in blocks if b["type"] == "actions"]
    select, = menu["elements"]
    assert select["type"] == "static_select"
    assert [o["value"] for o in select["options"]] == ["b" * 32, "c" * 32]
    compact, = [b for b in blocks if b["type"] == "context_actions"]
    assert len(compact["elements"]) == 3
    buttons = [e[key] for e in compact["elements"]
               for key in ("positive_button", "negative_button")]
    assert [b["text"]["text"] for b in buttons] == ["👀", "🙆 ✓", "🙏", "👍", "✅", "別の投稿"]
    assert [b["value"] for b in buttons] == [f"{i:032x}" for i in range(1, 6)] + ["d" * 32]
    assert all(b["text"]["type"] == "plain_text" and 0 < len(b["accessibility_label"]) <= 75
               for b in buttons)
    assert "取消" in buttons[1]["accessibility_label"]
    assert "見ました" in buttons[0]["accessibility_label"]
    body, _ = click()
    for pair in compact["elements"]:
        assert pair["type"] == "feedback_buttons" and len(pair["action_id"]) <= 255
        for key in ("positive_button", "negative_button"):
            origin = _origin(body, {"type": pair["type"], "action_id": pair["action_id"],
                                    "value": pair[key]["value"]})
            assert origin and origin["token"] == pair[key]["value"]
    spec["render_rev"] += 1
    newer, = [b for b in render(spec)[1] if b["type"] == "context_actions"]
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


@pytest.mark.parametrize("second", [False, True])
def test_compact_stamp_click_queues_existing_command_with_click_time(tmp_path, second):
    async def run():
        actions, app, _, dirs = fixture(tmp_path, kind="stamp_press")
        body, _ = click()
        action = _pair_action(second=second)
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
