"""Layout-3 safety and immutable delivery limits, using only synthetic ledgers."""
import notify_parts
import asyncio
import copy
import json

import pytest

import notify_cards as cards
import notify_scope
import notify_render
from adapters.common import spec as spec_module
from adapters.lineworks.delivery import Sender
from adapters.lineworks import cards as lw_cards
from notify_lineworks_testkit import LINEWORKS, MID, _deliver as deliver_lw
from notify_testkit import CFG, NOW, _card, _dispatch, _intent, _latest_render, _seed_thread, led as led
import clinical_urgency


def test_new_header_is_independent_from_long_original(led):
    _seed_thread(led, mids=(100,))
    led.db.execute("UPDATE messages SET body_text=?", ("合成" * 2300,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    spec = json.loads(_latest_render(led)["spec_json"])
    parts = spec["parts"]
    assert _card(led)["layout"] == 3
    assert parts["manifest"][2]["name"] == "m:100:header#1"
    assert notify_render.SECTION_RULE not in parts["thread_body_parts"][0]
    assert "".join(parts["thread_body_parts"][1:]) == "合成" * 2300
    assert spec_module.validate(spec) is spec


def test_legacy_fingerprint_preserves_exhausted_and_held(led, monkeypatch):
    monkeypatch.setattr(cards, "CARD_LAYOUT", 2)
    _seed_thread(led)
    _dispatch(led, _intent(led))
    old = _latest_render(led)
    led.db.execute("UPDATE notification_renders SET state='held'")
    led.db.execute("UPDATE notification_cards SET delivery_state='update_failed'")
    led.db.commit()
    before = dict(_card(led))
    monkeypatch.setattr(cards, "CARD_LAYOUT", 3)
    assert cards.sweep(led, CFG, now=NOW + 1)["updated"] == 0
    assert _latest_render(led)["delivery_id"] == old["delivery_id"]
    assert _latest_render(led)["state"] == "held"
    assert _card(led)["source_generation"] == before["source_generation"]
    assert _card(led)["presentation_generation"] == before["presentation_generation"]


def test_lw_reuses_card_and_tokens_on_stamp_only_update(led):
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), LINEWORKS)
    first = _latest_render(led)
    original = deliver_lw(led, first)
    expiry = [tuple(r) for r in led.db.execute("SELECT token,expires_at FROM notification_action_tokens")]
    with led.db:
        led.db.execute("UPDATE message_metadata SET last_error='synthetic_failure' WHERE message_id=100")
        specs = []
        cards.issue_render(led.db, 1, LINEWORKS, NOW + 1, specs, force=True)
    latest = specs[0]
    assert latest["parts"]["manifest"][0]["prior_remote_id"] == MID
    assert latest["parts"]["action_rows"] == original["parts"]["action_rows"]
    assert lw_cards.validate(latest) is latest
    for token, until in expiry:
        assert led.db.execute("SELECT expires_at FROM notification_action_tokens WHERE token=?", (token,)).fetchone()[0] == until
    sender = Sender(None, latest["delivery"], "synthetic")
    sender.route_current = lambda spec: True
    sender.send = lambda *args: (_ for _ in ()).throw(AssertionError("card resent"))
    assert asyncio.run(sender.perform(latest)) == {"result": "delivered", "message_id": MID}
    invalid = copy.deepcopy(latest)
    invalid["parts"]["manifest"][0]["prior_remote_id"] = "lw:" + "d" * 32
    import pytest
    with pytest.raises(ValueError, match="prior_remote"):
        lw_cards.validate(invalid)


def test_lw_first_summary_wait_expires(led):
    _seed_thread(led, mids=(100,))
    led.db.execute("UPDATE messages SET first_seen=?", (NOW,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), LINEWORKS)
    assert _latest_render(led) is None
    cards.sweep(led, LINEWORKS, now=NOW + cards.LINEWORKS_SUMMARY_WAIT_S)
    assert _latest_render(led) is not None


def test_layout_inventory_is_read_only_and_counts_unknown(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE kind='body_part'")
    before = led.db.total_changes
    rows = notify_scope.layout_migration_counts(led.db)
    assert rows[0]["cards"] == rows[0]["unknown_parts"] == 1
    assert led.db.total_changes == before


@pytest.mark.parametrize("collect", [False, True])
def test_lw_post_budget_preserves_original_and_emits_limit_notice(led, monkeypatch, collect):
    _seed_thread(led, mids=(100,))
    revision = [0]
    import notify_lineworks_testkit as kit
    claim = kit._claim
    import itertools
    attempts = itertools.count(100)
    monkeypatch.setattr(kit, "_claim", lambda render: claim(render, f"{next(attempts):016x}"))
    monkeypatch.setattr(notify_parts, "card_body_text", lambda *a, **k: (
        "合成", f"↳ 合成投稿\n要約 {revision[0]}\n────────────\n合成原文"))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), LINEWORKS)
    first = deliver_lw(led, _latest_render(led))
    count = sum(p["kind"] == "body_part" for p in first["parts"]["manifest"])
    initial = count
    for n in range(1, 8):
        if collect:
            cards.gc(led, LINEWORKS, now=NOW + n)
            assert _latest_render(led)["spec_json"] is None
        revision[0] = n
        with led.db:
            specs = []
            cards.issue_render(led.db, 1, LINEWORKS, NOW + n, specs, force=True)
        spec = specs[0]
        count += sum(p["kind"] == "body_part" and not p.get("prior_remote_id")
                     for p in spec["parts"]["manifest"])
        deliver_lw(led, _latest_render(led))
    assert count == initial + notify_parts.LINEWORKS_POST_UPDATES
    assert "合成原文" in spec["parts"]["thread_body_parts"]
    assert any("更新上限" in text for text in spec["parts"]["thread_body_parts"])


def test_lw_unknown_replacement_does_not_retire_old_buttons():
    from adapters.lineworks.delivery import DeliveryWorker
    from types import SimpleNamespace
    retired = []
    async def perform(spec):
        return {"result": "unknown", "error_code": "synthetic_timeout"}
    worker = object.__new__(DeliveryWorker)
    worker._ours = lambda delivery: True
    worker._sender = SimpleNamespace(route_current=lambda spec: True, perform=perform)
    worker._reg = SimpleNamespace(retire_card_tokens=lambda key: retired.append(key))
    outcome = asyncio.run(worker._perform({"spec": {"op": "update", "card_key": "synthetic", "delivery": {}}}))
    assert outcome["result"] == "unknown"
    assert retired == []


def test_legacy_fingerprints_match_v1022_fixed_vectors(led):
    # Generated by HEAD notify_render.py, loaded independently before the change.
    _seed_thread(led)
    _dispatch(led, _intent(led))
    expected = {
        (1, "slack"): "bafd3bc14dbf6985b30e038beee8c28d88d448749cefd27f41d92d89119ef594",
        (1, "discord"): "bafd3bc14dbf6985b30e038beee8c28d88d448749cefd27f41d92d89119ef594",
        (1, "lineworks"): "442157f8d96e603fa6384e25047b6bf6f551ba71ab2a08bdca7987f35cfe8a99",
        (2, "slack"): "7fbdf61767aad454cc32bd273ec8865e2e6e3266a6e1389a92c180f70dc6cf8e",
        (2, "discord"): "7fbdf61767aad454cc32bd273ec8865e2e6e3266a6e1389a92c180f70dc6cf8e",
        (2, "lineworks"): "0b01389d53ffea83b21cd44373c9e8fab9e97c88c0e656888621046a0b46f513",
    }
    for (layout, transport), fingerprint in expected.items():
        content = notify_render.card_content(led.db, {**_card(led), "layout": layout, "transport": transport}, cfg=CFG)
        assert notify_render.content_fingerprint(content) == fingerprint


def test_lw_original_write_token_cannot_revive_after_source_aba(led):
    from adapters.common import envelopes
    from notify_lineworks_testkit import SCOPE, _apply
    from notify_testkit import _token_for
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), LINEWORKS)
    spec = deliver_lw(led, _latest_render(led))
    old_token = _token_for(spec, "ack")
    initial = _card(led)["source_generation"]
    for index, content_hash in enumerate(("b" * 64, f"{100:064x}"), 1):
        with led.db:
            led.db.execute("UPDATE messages SET content_hash=? WHERE message_id=100", (content_hash,))
            cards.issue_render(led.db, 1, LINEWORKS, NOW + index, [], force=True)
        assert _card(led)["source_generation"] == initial + index
        request = envelopes.notification(old_token, "lineworks:40029600:synthetic-operator", {**SCOPE, "message_id": MID})
        assert _apply(led, request)["error"] == "stale_source"


def test_growing_and_shrinking_header_keeps_original_chunks_and_cleans_old_header(led, monkeypatch):
    from notify_testkit import _deliver, _part_receipt
    _seed_thread(led, mids=(100,))
    header = ["要約"]
    monkeypatch.setattr(notify_parts, "card_body_text", lambda *a, **k: (
        "合成", "↳ 合成\n" + header[0] + "\n────────────\n" + "原文" * 2100))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    _deliver(led)
    _part_receipt(led, _latest_render(led), "thread", remote_id="synthetic-thread")
    original = json.loads(_latest_render(led)["spec_json"])["parts"]["manifest"]
    hashes = {p["name"]: p["sha256"] for p in original if p.get("name", "").startswith("m:100#")}
    for value in ("要約" * 1200, "短い要約"):
        header[0] = value
        with led.db:
            specs = []
            cards.issue_render(led.db, 1, CFG, NOW + 1, specs, force=True)
        manifest = specs[0]["parts"]["manifest"]
        for part in manifest:
            if part.get("name") in hashes:
                assert part["sha256"] == hashes[part["name"]]
                assert part["prior_remote_id"]
                assert not part.get("edit_only")
        _deliver(led)
        _part_receipt(led, _latest_render(led), "thread", remote_id="synthetic-thread")
    cleanup = [p for p in manifest if p.get("name", "").startswith("m:100:header#") and p.get("edit_only")]
    assert any(p["name"] == "m:100:header#2" for p in cleanup)


def test_nonlegacy_extraction_attention_is_not_perpetual_pending(led, monkeypatch):
    _seed_thread(led, mids=(100,))
    monkeypatch.setattr(notify_render, "progress_state", lambda *a: {"state": "attention", "completed": 0, "total": 1})
    assert notify_render._summary_block(led.db, 100, cfg=CFG, label=False)["text"] == "要約 要確認"
    led.db.execute("UPDATE messages SET first_seen=?", (NOW,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), LINEWORKS)
    assert _latest_render(led) is not None


def test_lw_new_reply_uses_same_durable_summary_wait(led):
    from notify_testkit import _msg
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), LINEWORKS)
    original = _latest_render(led)
    deliver_lw(led, original)
    _msg(led, 101, parent=100)
    led.db.execute("UPDATE messages SET first_seen=? WHERE message_id=101", (NOW,))
    _dispatch(led, _intent(led, payload={"message_ids": [101]}), LINEWORKS)
    assert _latest_render(led)["delivery_id"] == original["delivery_id"]
    cards.sweep(led, LINEWORKS, now=NOW + cards.LINEWORKS_SUMMARY_WAIT_S)
    assert _latest_render(led)["delivery_id"] != original["delivery_id"]


def test_lineworks_new_source_keeps_a_usable_write_button(led):
    import notify_cards as cards
    from notify_testkit import _token_for
    from notify_lineworks_testkit import LINEWORKS, _deliver, SCOPE as LW_SCOPE, MID, _apply, _claim
    from adapters.common import envelopes
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={'message_ids':[100]}), LINEWORKS)
    initial = _deliver(led, _latest_render(led))
    old = _token_for(initial,'ack')
    with led.db:
        led.db.execute("UPDATE messages SET body_text='changed synthetic body',content_hash=? WHERE message_id=100", ('b'*64,))
        specs=[]
        cards.issue_render(led.db,1,LINEWORKS,NOW+1,specs,force=True)
    assert 'prior_remote_id' not in specs[0]['parts']['manifest'][0]
    claim = _claim(_latest_render(led), attempt="c" * 16)
    result = _apply(led, envelopes.transport_begin(claim))
    assert result["granted"], result
    assert _apply(led, envelopes.transport_receipt(
        claim, "delivered", message_id=MID))["applied"]
    updated = claim["spec"]
    new = _token_for(updated, 'ack')
    assert new != old
    actor = "lineworks:40029600:synthetic-operator"
    origin = {**LW_SCOPE, "message_id": MID}
    assert _apply(led, envelopes.notification(old, actor, origin))["error"] == "stale_source"
    result = _apply(led, envelopes.notification(new, actor, origin))
    assert result["outcome"] == "applied", result


def test_lw_gc_missing_spec_does_not_reuse_unproven_card_tokens(led):
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), LINEWORKS)
    original = deliver_lw(led, _latest_render(led))
    expiry = [tuple(r) for r in led.db.execute("SELECT token,expires_at FROM notification_action_tokens")]
    cards.gc(led, LINEWORKS, now=NOW + 1)
    assert _latest_render(led)["spec_json"] is None
    with led.db:
        led.db.execute("UPDATE message_metadata SET last_error='synthetic_failure' WHERE message_id=100")
        specs = []
        cards.issue_render(led.db, 1, LINEWORKS, NOW + 2, specs, force=True)
    latest = specs[0]
    assert "prior_remote_id" not in latest["parts"]["manifest"][0]
    assert latest["parts"]["action_rows"] != original["parts"]["action_rows"]
    assert latest["source_generation"] == original["source_generation"]
    for token, until in expiry:
        assert led.db.execute("SELECT expires_at FROM notification_action_tokens WHERE token=?", (token,)).fetchone()[0] == until
    assert lw_cards.validate(latest) is latest


@pytest.mark.parametrize("combined_intent", [False, True])
def test_lw_continuous_replies_keep_individual_deadlines_and_pending_coverage(
        led, monkeypatch, combined_intent):
    import itertools
    import notify_lineworks_testkit as kit
    from notify_testkit import _msg, _attach, _token_for
    attempts = itertools.count(100)
    claim = kit._claim
    monkeypatch.setattr(kit, "_claim", lambda render: claim(render, f"{next(attempts):016x}"))
    _seed_thread(led, mids=(100,))
    led.db.execute("UPDATE messages SET first_seen=? WHERE message_id=100", (NOW,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), LINEWORKS)
    for mid, offset in ((101, 60), (102, 110)):
        _msg(led, mid, parent=100)
        led.db.execute("UPDATE messages SET first_seen=? WHERE message_id=?", (NOW + offset, mid))
        covered = list(range(100, mid + 1)) if combined_intent else [mid]
        _dispatch(led, _intent(led, payload={"message_ids": covered}), LINEWORKS, now=NOW + offset)
        assert _latest_render(led) is None
    attachment_id = _attach(led, 102)
    for eligible, offset in ((100, 120), (101, 180), (102, 230)):
        assert cards.sweep(led, LINEWORKS, now=NOW + offset)["updated"] == 1
        render = _latest_render(led)
        spec = json.loads(render["spec_json"])
        shown = json.loads(led.db.execute("SELECT shown FROM notification_view_manifests WHERE manifest_id=?",
                                        (spec["parts"]["manifest_id"],)).fetchone()[0])
        assert set(shown) == set(range(100, eligible + 1))
        token = led.db.execute("SELECT need_manifest_id FROM notification_action_tokens WHERE token=?",
                               (_token_for(spec, "ack"),)).fetchone()
        assert token["need_manifest_id"] == spec["parts"]["manifest_id"]
        attachments = {p["attachment_id"] for p in spec["parts"]["manifest"]
                       if p["kind"] == "attachment_part"}
        assert attachments == ({attachment_id} if eligible == 102 else set())
        names = {p.get("name") for p in spec["parts"]["manifest"] if p["kind"] == "body_part"}
        assert f"m:{eligible}:header#1" in names
        assert all(f"m:{mid}:header#1" not in names for mid in range(eligible + 1, 103))
        # Source guards still bind the full current thread; ack uses only shown.
        assert _card(led)["source_fp"] == notify_render.source_fingerprint(led.db, _card(led))
        deliver_lw(led, render)
        for intent in led.db.execute("SELECT coverage,state,delivery_id FROM notification_intent_cards"):
            if any(mid > eligible for mid in json.loads(intent["coverage"])):
                assert intent["state"] == "pending" and intent["delivery_id"] is None
        if eligible < 102:
            assert cards.sweep(led, LINEWORKS, now=NOW + offset + 1)["updated"] == 0
    assert {r[0] for r in led.db.execute("SELECT state FROM notification_intent_cards")} == {"delivered"}


def test_lw_wait_does_not_delay_urgent_thread(led, monkeypatch):
    _seed_thread(led, mids=(100, 101))
    led.db.execute("UPDATE messages SET first_seen=?", (NOW,))
    monkeypatch.setattr(clinical_urgency, 'message_urgency', lambda db, mid: "rule")
    _dispatch(led, _intent(led), LINEWORKS)
    spec = json.loads(_latest_render(led)["spec_json"])
    names = {p.get("name") for p in spec["parts"]["manifest"]}
    assert {"m:100:header#1", "m:101:header#1"} <= names


def test_lw_expired_post_gets_one_later_summary_update(led, monkeypatch):
    import itertools
    import notify_lineworks_testkit as kit
    from notify_testkit import _extract, _msg
    attempts = itertools.count(100)
    claim = kit._claim
    monkeypatch.setattr(kit, "_claim", lambda render: claim(render, f"{next(attempts):016x}"))
    _seed_thread(led, mids=(100,))
    led.db.execute("UPDATE messages SET first_seen=?", (NOW,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), LINEWORKS)
    assert cards.sweep(led, LINEWORKS, now=NOW + 120)["updated"] == 1
    deliver_lw(led, _latest_render(led))
    _msg(led, 101, parent=100)
    led.db.execute("UPDATE messages SET first_seen=? WHERE message_id=101", (NOW + 120,))
    _dispatch(led, _intent(led, payload={"message_ids": [101]}), LINEWORKS, now=NOW + 120)
    _extract(led, 100, {"summary": "合成の完成した要約"}, kind="extract_llm")
    assert cards.sweep(led, LINEWORKS, now=NOW + 121)["updated"] == 1
    revised = deliver_lw(led, _latest_render(led))
    assert any("合成の完成した要約" in text for text in revised["parts"]["thread_body_parts"])
    original = next(p for p in revised["parts"]["manifest"] if p.get("name") == "m:100#1")
    assert original["prior_remote_id"]
    assert all(p.get("name") != "m:101:header#1" for p in revised["parts"]["manifest"])
    assert cards.sweep(led, LINEWORKS, now=NOW + 122)["updated"] == 0


def test_lw_ready_reply_does_not_miscount_waiting_root_as_missing(led):
    from notify_testkit import _extract
    _seed_thread(led)
    led.db.execute("UPDATE messages SET first_seen=?", (NOW,))
    led.db.execute("UPDATE messages SET reply_count=3 WHERE message_id=100")
    _extract(led, 101, {"summary": "合成の返信要約"}, kind="extract_llm")
    _dispatch(led, _intent(led), LINEWORKS)
    spec = json.loads(_latest_render(led)["spec_json"])
    shown = json.loads(led.db.execute("SELECT shown FROM notification_view_manifests WHERE manifest_id=?",
                                     (spec["parts"]["manifest_id"],)).fetchone()[0])
    assert shown == [101]
    texts = "\n".join(block.get("text", "") for block in spec["parts"]["containers"])
    assert "返信未取得 2件" in texts
    assert all(p.get("name") != "m:100:header#1" for p in spec["parts"]["manifest"])


@pytest.mark.parametrize("coverage", ['{', 'null', '123', '{}', '[{}]', '[true]', '[0]', '["100"]'])
@pytest.mark.parametrize("waiting_reply", [False, True])
def test_invalid_intent_coverage_never_binds_or_blocks_ready_posts(led, coverage, waiting_reply):
    from notify_testkit import _msg
    _seed_thread(led, mids=(100,))
    led.db.execute("UPDATE messages SET first_seen=?", (NOW,))
    event = _intent(led, payload={"message_ids": [100]})
    _dispatch(led, event, LINEWORKS)
    assert _latest_render(led) is None
    if waiting_reply:
        _msg(led, 101, parent=100)
        led.db.execute("UPDATE messages SET first_seen=? WHERE message_id=101", (NOW + 60,))
        _dispatch(led, _intent(led, payload={"message_ids": [101]}), LINEWORKS, now=NOW + 60)
    led.db.execute("UPDATE notification_intent_cards SET coverage=? WHERE event_id=?",
                   (coverage, event["event_id"]))
    led.db.commit()
    assert cards.sweep(led, LINEWORKS, now=NOW + 120)["updated"] == 1
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    assert event["event_id"] not in spec["delivery"]["intent_event_ids"]
    assert "m:100:header#1" in {p.get("name") for p in spec["parts"]["manifest"]}
    deliver_lw(led, render)
    damaged = led.db.execute("SELECT state,delivery_id FROM notification_intent_cards WHERE event_id=?",
                             (event["event_id"],)).fetchone()
    assert damaged["state"] == "pending" and damaged["delivery_id"] is None
