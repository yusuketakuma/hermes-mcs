"""Post-owned reply-card counts, tasks and feedback use only synthetic ledgers."""
import json

import pytest

import notify_action_tokens
import notify_actions
import notify_cards
import notify_render
from mcs_operations import _apply_extract_feedback_tx
from notify_testkit import (
    CFG, NOW, ORIGIN, _add_request, _dispatch, _intent, _llm_extract,
    _msg, _patient, _seed_thread, led, pinned_clock,
)

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")
REPLY = {**CFG, "notify": {**CFG["notify"], "reply_cards": True}}


def _post_cards(led):
    _seed_thread(led, mids=(100, 101, 102))
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101, 102]}), REPLY)
    return {notify_render.own_post(row): dict(row) for row in led.db.execute(
        "SELECT * FROM notification_cards ORDER BY card_id")}


def _text(led, card, **kwargs):
    return notify_render.display_text(notify_render.card_content(
        led.db, card, cfg=REPLY, **kwargs))


def _task_token(led, card, action="tasks", params=None, request_rev=None):
    token = notify_action_tokens.mint(
        led.db, card["card_id"], action, params,
        {"source_gen": card["source_generation"], "request_rev": request_rev}, NOW)
    led.db.commit()
    return led.db.execute("SELECT * FROM notification_action_tokens WHERE token=?",
                          (token,)).fetchone()


def _apply(led, token):
    return notify_actions.apply_notification(led, {
        "version": 1, "op": "notification", "command_id": f"{token}:{'ab' * 8}",
        "actor": "discord:1001", "token": token, "origin": ORIGIN,
    }, REPLY, now=NOW + 1)


@pytest.mark.parametrize("reported,missing", [(2, None), (4, 2), (1, None)])
def test_root_fetch_gap_counts_saved_replies_outside_its_face(led, reported, missing):
    cards = _post_cards(led)
    led.db.execute("UPDATE messages SET reply_count=? WHERE message_id=100", (reported,))
    # A different patient's reused parent reference must not hide a real gap.
    _patient(led, 2, name="合成患者B")
    _msg(led, 200, pid=2, parent=100)
    text = _text(led, cards[100])
    assert "↳ 返信 2件を保存済み" in text
    assert "はスレッド内" not in text
    if missing is None:
        assert "返信未取得" not in text
    else:
        assert f"返信未取得 {missing}件" in text
    assert notify_render.card_content(led.db, cards[100], cfg=REPLY)["shown"] == [100]


def test_saved_count_never_claims_waiting_cards_have_been_delivered(led):
    cards = _post_cards(led)
    assert all(cards[mid]["message_id"] is None for mid in (101, 102))
    assert "↳ 返信 2件を保存済み" in _text(led, cards[100])
    led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=102")
    led.db.execute("UPDATE messages SET reply_count=2 WHERE message_id=100")
    # A known deletion is not an uncollected reply; the live saved count omits it.
    text = _text(led, cards[100], waiting=frozenset({101}))
    assert "返信未取得" not in text
    assert "↳ 返信 1件を保存済み" in text


def test_reply_face_does_not_repeat_the_root_fetch_gap(led):
    cards = _post_cards(led)
    led.db.execute("UPDATE messages SET reply_count=5 WHERE message_id=100")
    led.db.execute("UPDATE messages SET reply_count=8 WHERE message_id=101")
    assert "返信未取得 3件" in _text(led, cards[100])
    for mid in (101, 102):
        text = _text(led, cards[mid])
        assert "返信未取得" not in text and "を保存済み" not in text


@pytest.mark.parametrize("layout", [1, 2, 3])
def test_legacy_cards_keep_their_collecting_fetch_gap(led, layout):
    root = {**_post_cards(led)[100], "layout": layout}
    led.db.execute("UPDATE messages SET reply_count=3 WHERE message_id=100")
    text = _text(led, root, waiting=frozenset({102}))
    assert "返信未取得 1件" in text
    assert "を保存済み" not in text
    assert notify_render.card_content(led.db, root, cfg=REPLY)["shown"] == [100, 101, 102]


def test_task_list_and_footer_have_the_same_post_owner(led):
    cards = _post_cards(led)
    ids = {mid: _add_request(led, title=f"合成タスク{mid}", src_mid=mid)
           for mid in cards}
    done = _add_request(led, title="合成の完了済み", src_mid=101, status="done")
    _add_request(led, title="合成の取消済み", src_mid=101, status="cancelled")
    for mid, card in cards.items():
        items, contexts = notify_actions._thread_tasks(
            led.db, card, _task_token(led, card), NOW)
        assert {item["request_id"] for item in items} == (
            {ids[mid], done} if mid == 101 else {ids[mid]})
        assert [row["request_id"] for row in notify_render.open_tasks(led.db, card)] == [ids[mid]]
        assert all(ctx["card_key"] == card["card_key"] for ctx in contexts.values())


@pytest.mark.parametrize("layout", [1, 2, 3])
def test_legacy_task_lists_still_include_root_and_reply_tasks(led, layout):
    root = {**_post_cards(led)[100], "layout": layout}
    ids = {_add_request(led, src_mid=mid) for mid in (100, 101, 102)}
    items, _ = notify_actions._thread_tasks(led.db, root, _task_token(led, root), NOW)
    assert {item["request_id"] for item in items} == ids


def test_old_cross_post_transition_token_cannot_change_a_sibling_task(led):
    cards = _post_cards(led)
    rid = _add_request(led, src_mid=102)
    # Simulate a token issued by the old unscoped reply-card list.
    tok = _task_token(led, cards[101], "task_status",
                      {"request_id": rid, "status": "done", "ephemeral": True}, 1)
    result = _apply(led, tok["token"])
    assert result["error"] == "request_not_found"
    assert led.db.execute("SELECT status FROM requests WHERE request_id=?", (rid,)).fetchone()[0] == "open"


@pytest.mark.parametrize("root_layout", [3, 4])
def test_task_transition_refreshes_only_cards_displaying_its_source(led, monkeypatch, root_layout):
    cards = _post_cards(led)
    led.db.execute("UPDATE notification_cards SET layout=? WHERE card_id=?",
                   (root_layout, cards[100]["card_id"]))
    rid = _add_request(led, src_mid=101)
    items, _ = notify_actions._thread_tasks(
        led.db, cards[101], _task_token(led, cards[101]), NOW)
    token = items[0]["transitions"]["done"]["token"]
    led.db.commit()
    refreshed = []

    def issue(db, card_id, cfg, now, specs):
        assert db is led.db and db.in_transaction
        refreshed.append(card_id)
        return f"synthetic-render-{card_id}"

    monkeypatch.setattr(notify_cards, "issue_render", issue)
    result = _apply(led, token)
    assert result["outcome"] == "applied" and result["status"] == "done"
    assert result["delivery_id"] == f"synthetic-render-{cards[101]['card_id']}"
    expected = {cards[101]["card_id"]}
    if root_layout == 3:
        expected.add(cards[100]["card_id"])
    assert set(refreshed) == expected
    # The shared connection remains usable and the owned transaction committed.
    assert not led.db.in_transaction
    assert led.db.execute("SELECT status FROM requests WHERE request_id=?", (rid,)).fetchone()[0] == "done"


def test_report_badge_stays_on_its_post_and_clears_on_reextraction(led):
    cards = _post_cards(led)
    artifact = _llm_extract(led, 101, {"summary": "合成の要約", "requests": []})
    error, _ = _apply_extract_feedback_tx(led.db, {
        "project_id": 1, "message_id": 101, "artifact_id": artifact,
        "field": "summary", "reason": "合成の報告", "actor": "discord:1001",
        "command_id": "11111111-2222-4333-8444-555555555555",
    }, NOW)
    assert error is None
    for mid, card in cards.items():
        assert notify_render.feedback_pending(led.db, card) is (mid == 101)
        footer = json.dumps(notify_render.card_content(led.db, card, cfg=REPLY)["footer"],
                            ensure_ascii=False)
        assert ("誤り報告あり" in footer) is (mid == 101)
    for layout in (1, 2, 3):
        assert notify_render.feedback_pending(led.db, {**cards[100], "layout": layout})
    # Match the extractor's replacement of the old message artifact.
    led.db.execute("DELETE FROM artifacts WHERE artifact_id=?", (artifact,))
    _llm_extract(led, 101, {"summary": "合成の再要約", "requests": []})
    assert not any(notify_render.feedback_pending(led.db, card) for card in cards.values())
