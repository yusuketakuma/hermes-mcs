"""Versioned thread boundaries preserve synthetic source text and sealed parts."""
import asyncio
import copy
import hashlib
import json

import pytest

import notify_cards
import notify_parts
import notify_render
import present_slack
from adapters.common import spec as spec_module
from adapters.lineworks.cards import validate as validate_lineworks
from adapters.lineworks.delivery import chunk_text
from adapters.slack.cards import validate as validate_slack
from adapters.slack.presentation import thread_part_blocks
from notify_lineworks_testkit import LINEWORKS
from notify_testkit import CFG, NOW, _deliver, _dispatch, _intent, _latest_render, _part_receipt, _seed_thread, led
from slack_testkit import SLACK, _granted_card, _mkworld

__all__ = ["led"]


@pytest.mark.parametrize("cfg", [CFG, SLACK, LINEWORKS], ids=["discord", "slack", "lineworks"])
def test_generated_divider_survives_sealed_parts_and_source_delimiter(led, cfg, monkeypatch):
    if cfg is SLACK:
        monkeypatch.setattr(present_slack, "FACE_ORIGINAL_MAX", 20)
    _seed_thread(led, mids=(100,))
    source = "合成本文" * 400 + "\n" + notify_render.SECTION_RULE + "\n末尾"
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", (source,))
    assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), cfg)["dispatched"]
    spec = json.loads(_latest_render(led)["spec_json"])
    assert spec["parts"]["section_divider_version"] == 1
    validate = validate_slack if cfg is SLACK else validate_lineworks if cfg is LINEWORKS else spec_module.validate
    assert validate(spec) is spec
    entries = [(p, spec["parts"]["thread_body_parts"][int(p["part_id"].split(":")[1]) - 1])
               for p in spec["parts"]["manifest"] if p["kind"] == "body_part"]
    header = [(p, text) for p, text in entries if ":header#" in p["name"]]
    body = [(p, text) for p, text in entries if p["name"].startswith("m:100#")]
    assert header[-1][1].endswith("\n" + notify_render.SECTION_RULE)
    assert "".join(text for _, text in body) == source
    for part, text in entries:
        assert part["bytes"] == len(text.encode())
        assert part["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    if cfg is SLACK:
        assert thread_part_blocks(spec, *header[-1])[-1] == {"type": "divider"}
    elif cfg is LINEWORKS:
        assert chunk_text(spec, header[-1][0]["name"], header[-1][1]).endswith(notify_render.SECTION_RULE)


def test_old_sealed_spec_and_unknown_worker_version(led, monkeypatch):
    _seed_thread(led, mids=(100,))
    real = notify_parts.build_manifest
    def old_builder(db, card, spec, *args, **kwargs):
        spec["parts"].pop("section_divider_version", None)
        return real(db, card, spec, *args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(notify_parts, "build_manifest", old_builder)
        assert _dispatch(led, _intent(led, payload={"message_ids": [100]}))["dispatched"]
    old = json.loads(_latest_render(led)["spec_json"])
    assert "section_divider_version" not in old["parts"]
    assert spec_module.validate(old) is old
    saved = copy.deepcopy(old)
    _deliver(led)
    _part_receipt(led, _latest_render(led), "thread", remote_id="synthetic-thread")
    with led.db:
        specs = []
        notify_cards.issue_render(led.db, 1, CFG, NOW + 1, specs, force=True)
    assert json.loads(led.db.execute("SELECT spec_json FROM notification_renders WHERE delivery_id=?",
                                   (old["delivery_id"],)).fetchone()[0]) == saved
    new = specs[0]
    assert new["parts"]["section_divider_version"] == 1
    assert spec_module.validate(new) is new
    monkeypatch.setattr(spec_module, "PARTS_KEYS", spec_module.PARTS_KEYS - {"section_divider_version"})
    with pytest.raises(ValueError, match="unsupported_parts_key"):
        spec_module.validate(new)
    assert spec_module.validate(old) is old


def test_long_header_keeps_separator_indivisible_and_slack_fallback_text(led, monkeypatch):
    monkeypatch.setattr(present_slack, "FACE_ORIGINAL_MAX", 20)
    _seed_thread(led, mids=(100,))
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", ("合成本文" * 400,))
    header = "↳ " + "x" * 1897
    monkeypatch.setattr(notify_parts, "card_body_text", lambda *args, **kwargs: (
        "合成", header + "\n" + notify_render.SECTION_RULE + "\n原文"))
    assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)["dispatched"]
    spec = json.loads(_latest_render(led)["spec_json"])
    items = [(p, spec["parts"]["thread_body_parts"][int(p["part_id"].split(":")[1]) - 1])
             for p in spec["parts"]["manifest"] if p["kind"] == "body_part"]
    heads = [(p, text) for p, text in items if ":header#" in p["name"]]
    assert "".join(text for _, text in heads) == header + "\n" + notify_render.SECTION_RULE
    assert heads[-1][1] == "\n" + notify_render.SECTION_RULE
    blocks = thread_part_blocks(spec, *heads[-1])
    assert blocks is None  # later header chunks use their sealed plain-text fallback
    assert validate_slack(spec) is spec


def test_old_slack_scope_is_only_used_for_legacy_fingerprint(led):
    _seed_thread(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    row = dict(led.db.execute("SELECT * FROM notification_cards WHERE card_id=1").fetchone())
    row["layout"] = 2
    old = notify_render.card_content(led.db, row, cfg=SLACK, _provider_version=1)
    current = notify_render.card_content(led.db, row, cfg=SLACK)
    assert old["shown"] == [100, 101] and current["shown"] == [100]
    assert old["source_fp"] != current["source_fp"]
    assert notify_render.own_post(row) == 100


def test_old_slack_policy_cannot_suppress_new_generation_by_legacy_fingerprint(led):
    _seed_thread(led, mids=(100,))
    assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)["dispatched"]
    world = _mkworld(led)
    asyncio.run(_granted_card(world.worker, led, world.root))
    with led.db:
        led.db.execute("UPDATE notification_cards SET layout=2 WHERE card_id=1")
        row = dict(led.db.execute("SELECT * FROM notification_cards WHERE card_id=1").fetchone())
        old = notify_render.card_content(led.db, row, cfg=SLACK, _provider_version=1)
        old_fp = notify_cards._presentation_fp(row, old, SLACK)
        render = _latest_render(led)
        old_spec = json.loads(render["spec_json"])
        old_spec["parts"].pop("slack_thread_policy")
        old_spec["parts"]["provider_states_version"] = 1
        led.db.execute("UPDATE notification_cards SET source_fp=?,content_fp=? WHERE card_id=1",
                       (old["source_fp"], old_fp))
        led.db.execute("UPDATE notification_renders SET spec_json=? WHERE delivery_id=?",
                       (json.dumps(old_spec), render["delivery_id"]))
    assert notify_cards.sweep(led, SLACK, now=NOW + 1)["updated"] == 1
    new = json.loads(_latest_render(led)["spec_json"])
    assert new["parts"]["slack_thread_policy"] == 1
    assert new["parts"]["post_source_id"] == 100
