"""自分の投稿への反応・自分宛メンション・しおり等の表示（読取り専用・完全合成）。"""
import json

import ledger
import mcs_signals
import mcs_view
import notify_digest
import notify_render
import notify_views
from message_metadata import (
    flag_lines, get_message_metadata, own_post_reaction_text, self_mentioned)
from notify_testkit import CFG, NOW, _card, _dispatch, _intent, _msg, _patient, _signal_row, led

__all__ = ["led"]

DAY = 86400
SELF, OTHER = 7, 8
PUB = {**CFG, "metadata_refresh_publish": True}


def _meta(led, mid, *, at=NOW - 60, error=None, **fields):
    content = {k: {"value": v, "observed_at": at} for k, v in fields.items()}
    led.db.execute("INSERT OR REPLACE INTO message_metadata "
                   "(message_id,source,content,checked_at,last_error) VALUES(?,?,?,?,?)",
                   (mid, "capture", json.dumps(content), at, error))
    led.db.commit()


def _r(kind="viewed", count=1, mine=False):
    return {"type": kind, "count": count, "self_reacted": mine}


def _self_known(led):
    mcs_signals.record_station_staff(led.db, [{"staff_id": SELF, "is_self": True},
                                            {"staff_id": OTHER, "is_self": False}])


def _post(led, mid, sender, *, ts, pid=1, parent=None, name="合成同名"):
    _msg(led, mid, pid=pid, parent=parent, ts=int(ts), sender=name)
    led.db.execute("UPDATE messages SET sender_id=? WHERE message_id=?", (sender, mid))
    led.db.commit()


def _digest(led, cfg=PUB, **kw):
    return notify_render.parts_text(
        notify_digest.build(led.db, cfg, NOW - DAY, NOW, **kw))


def _section(text, head):
    return text.split(f"■ {head}", 1)[1].split("\n■", 1)[0] if f"■ {head}" in text else None


def test_own_post_text_counts_others_and_keeps_unfetched_distinct(led):
    _patient(led)
    _msg(led, 100)
    meta = get_message_metadata(led.db, 100)
    assert own_post_reaction_text(meta) == "MCS: スタンプ未取得"
    _meta(led, 100, reactions=[_r("viewed", 3, True), _r("accepted", 1), _r("good", 0)])
    text = own_post_reaction_text(get_message_metadata(led.db, 100))
    assert text.startswith("MCS: 自分の投稿への反応 見ました2/承知1（観測 ")
    assert "いいね" not in text
    _meta(led, 100, reactions=[_r("viewed", 1, True)])
    assert "他者0件" in own_post_reaction_text(get_message_metadata(led.db, 100))
    _meta(led, 100, reactions=[])
    assert "他者0件" in own_post_reaction_text(get_message_metadata(led.db, 100))


def test_card_footer_uses_sender_id_not_same_name(led):
    _patient(led)
    _self_known(led)
    _post(led, 100, SELF, ts=NOW - 100)
    _post(led, 101, OTHER, ts=NOW - 50, parent=100)
    for mid in (100, 101):
        _meta(led, mid, reactions=[_r("viewed", 2, True)])
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101]}))
    footer = "\n".join(i["text"] for i in
                       notify_render._card_content(led.db, _card(led))["footer"])
    assert "#100 MCS: 自分の投稿への反応 見ました1" in footer
    assert "#101 MCS: 本人 見ました" in footer


def test_unreacted_own_posts_section_only_when_published(led):
    _patient(led)
    _patient(led, 2, archived=1)
    _self_known(led)
    _post(led, 100, SELF, ts=NOW - 2 * DAY)            # self reaction only -> 0
    _post(led, 101, SELF, ts=NOW - 3600)               # others reacted
    _post(led, 102, SELF, ts=NOW - 7 * DAY - 1)        # outside 7 days
    _post(led, 103, SELF, ts=NOW - 7 * DAY)            # boundary: inside
    _post(led, 104, SELF, ts=NOW - 100)                # unfetched
    _post(led, 105, SELF, ts=NOW - 100)                # deleted
    _post(led, 106, SELF, ts=NOW - 100, pid=2)         # archived
    _post(led, 107, OTHER, ts=NOW - 100)               # not mine
    _post(led, 108, SELF, ts=NOW - 90, parent=107)     # reply, not root
    _post(led, 109, SELF, ts=NOW - 80)                 # invalid
    _meta(led, 100, reactions=[_r("viewed", 1, True)])
    _meta(led, 101, reactions=[_r("accepted", 2, True)])
    for mid in (102, 103, 105, 106, 107, 108):
        _meta(led, mid, reactions=[])
    _meta(led, 109, reactions=["broken"])
    led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=105")
    assert _section(_digest(led, CFG), "反応が観測されていない自分の投稿") is None
    sec = _section(_digest(led), "反応が観測されていない自分の投稿")
    assert "他者反応0件 2投稿" in sec
    assert "message 100 2日経過・観測" in sec and "message 103 7日経過" in sec
    for mid in (101, 102, 104, 105, 106, 107, 108, 109):
        assert f"message {mid} " not in sec
    assert "スタンプ未取得 1投稿・取得不正 1（0件に含めません）" in sec
    assert "合成同名" not in sec and "本文" not in sec


def test_unreacted_section_without_self_id_and_scope(led):
    _patient(led)
    _patient(led, 2)
    _post(led, 100, SELF, ts=NOW - 100)
    _meta(led, 100, reactions=[])
    assert "本人の送信者IDが不明" in _section(_digest(led), "反応が観測されていない自分の投稿")
    _self_known(led)
    _post(led, 200, SELF, ts=NOW - 100, pid=2)
    _meta(led, 200, reactions=[])
    sec = _section(_digest(led, allowed=[2]), "反応が観測されていない自分の投稿")
    assert "message 200" in sec and "message 100" not in sec
    flt = notify_digest.parse_scope("project:1")
    sec = _section(_digest(led, flt=flt), "反応が観測されていない自分の投稿")
    assert "message 100" in sec and "message 200" not in sec


def test_addressed_to_me_requires_same_thread_reply_or_self_stamp(led):
    _patient(led)
    _self_known(led)
    me = [{"type": "user", "id": SELF}]
    _post(led, 100, OTHER, ts=NOW - 5000)          # answered by my later reply
    _post(led, 101, SELF, ts=NOW - 4000, parent=100)
    _post(led, 110, OTHER, ts=NOW - 5000)          # my reply only in another thread
    _post(led, 120, SELF, ts=NOW - 4000)
    _post(led, 130, OTHER, ts=NOW - 5000)          # others' stamp only
    _post(led, 140, OTHER, ts=NOW - 5000)          # my stamp
    _post(led, 150, OTHER, ts=NOW - 5000)          # mention to someone else
    _post(led, 160, OTHER, ts=NOW - 5000)          # station mention
    _post(led, 170, OTHER, ts=NOW - 5000)          # invalid mentions
    _post(led, 180, OTHER, ts=NOW - 5000)          # my reply BEFORE the mention
    _post(led, 181, SELF, ts=NOW - 6000, parent=180)
    _meta(led, 100, mentions=me, reactions=[])
    _meta(led, 110, mentions=me, reactions=[])
    _meta(led, 130, mentions=me, reactions=[_r("viewed", 3)])
    _meta(led, 140, mentions=me, reactions=[_r("accepted", 1, True)])
    _meta(led, 150, mentions=[{"type": "user", "id": OTHER}], reactions=[])
    _meta(led, 160, mentions=[{"type": "station", "id": 5}], reactions=[])
    _meta(led, 170, mentions=[{"type": "user", "id": "7"}], reactions=[])
    _meta(led, 180, mentions=me)
    sec = _section(_digest(led), "自分宛で応答未観測")
    assert "message 110 " in sec and "message 130 " in sec and "message 180 " in sec
    for mid in (100, 140, 150, 160, 170):
        assert f"message {mid} " not in sec
    assert "自分の返信観測なし・本人スタンプ観測なし" in sec
    assert "message 180 本人宛メンション・1時間経過・自分の返信観測なし・本人スタンプ未取得" in sec
    assert "施設宛（自局判定なし）: 1投稿" in sec
    assert "メンション不明（未取得・取得不正）" in sec
    assert "記録が見つからない≠対応がなかった" in sec
    assert "合成同名" not in sec and "本文" not in sec
    # 同じ表示は publish 設定に依らない（capture の値を読むだけ）
    assert _section(_digest(led, CFG), "自分宛で応答未観測") == sec


def test_addressed_section_without_self_id_lists_no_mentions(led):
    _patient(led)
    _post(led, 110, OTHER, ts=NOW - 5000)
    _meta(led, 110, mentions=[{"type": "user", "id": SELF}], reactions=[])
    _signal_row(led, "pru:1:110", stype="pharmacist_request_unanswered", mids=[110])
    cfg = {**PUB, "signals": {"notify": True}}
    sec = _section(_digest(led, cfg), "自分宛で応答未観測")
    assert "本人の送信者IDが不明のためメンションは判定していません" in sec
    assert "本人宛メンション" not in sec
    assert "薬剤師宛依頼の応答未確認（シグナル）・本人ID不明" in sec
    assert _section(_digest(led, cfg, allowed=[2]), "自分宛で応答未観測") is None


def test_self_mentioned_and_evidence_flags(led, tmp_path):
    _patient(led)
    _post(led, 100, OTHER, ts=NOW - 100)
    assert self_mentioned(led.db, 100) is None
    _meta(led, 100, mentions=[{"type": "user", "id": SELF}], is_bookmarked=True)
    assert self_mentioned(led.db, 100) is None          # self ID unknown
    assert flag_lines(led.db, get_message_metadata(led.db, 100)) == [
        "メンション: 本人判定不可（本人ID不明）", "しおり: あり", "ピン留め: 未取得"]
    _self_known(led)
    assert self_mentioned(led.db, 100) is True
    _meta(led, 100, mentions=[{"type": "user", "id": OTHER}], is_pinned=False,
          error="is_bookmarked_invalid")
    assert self_mentioned(led.db, 100) is False
    snap = ledger.publish_snapshot(str(tmp_path / "data" / "ledger.db"),
                                   str(tmp_path / "snap"))
    view = mcs_view.View(snap)
    try:
        message = view.read("evidence", project=1, message_id=100)["message"]
        assert message["metadata_flags"] == [
            "メンション: 本人宛なし", "しおり: 取得不正", "ピン留め: なし"]
    finally:
        view.close()


def test_coverage_counts_stamp_gaps_in_window(led):
    _patient(led)
    for mid in (100, 101, 102):
        _msg(led, mid, ts=int(NOW - 100))
        led.db.execute("UPDATE messages SET first_seen=? WHERE message_id=?",
                       (NOW - 100, mid))
    _meta(led, 101, reactions=[])
    _meta(led, 102, reactions=["broken"])
    cov = _section(_digest(led), "取得状況（記録ベース）")
    assert "・スタンプ未取得 1投稿・取得不正 1" in cov


def test_fit_parts_folds_list_rows_not_disclosures(led):
    _patient(led, name="合" * 60)
    _self_known(led)
    for i in range(40):
        _post(led, 1000 + i, SELF, ts=NOW - 1000 + i)
        _meta(led, 1000 + i, reactions=[])
    _post(led, 2000, SELF, ts=NOW - 10)
    parts = notify_digest.build(led.db, PUB, NOW - DAY, NOW, names=True)
    folded = notify_render.fit_parts(parts, 600)
    for dialect in ("plain", "slack", "discord"):
        text = notify_render.parts_text(folded, dialect)
        sec = _section(text, "反応が観測されていない自分の投稿")
        assert "他者反応0件 40投稿" in sec and "スタンプ未取得 1投稿" in sec
        assert "・…他" in sec
        assert "取得状況（記録ベース）" in text
    full = _section(notify_render.parts_text(parts), "反応が観測されていない自分の投稿")
    assert full.count("\n・project") == notify_digest.MAX_LIST
    assert "・…他30件" in full


def test_patient_summary_request_reply_states(led):
    _patient(led)
    reqs = [{"kind": "pharmacist", "ctx": "残薬確認", "at": "2026-09-30T09:00",
             "mid": 100, "reply_state": "done", "reply_conflict": True},
            {"kind": "doctor", "ctx": "処方変更", "at": "2026-09-29T09:00", "mid": 101}]
    reqs += [{"ctx": f"依頼{i}", "at": "2026-09-01T09:00", "mid": 200 + i} for i in range(5)]
    led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,"
                   "created_at) VALUES('patient_rollup',1,NULL,?,'t','{}',?)",
                   (json.dumps({"recent_requests": reqs}), NOW))
    led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,"
                   "created_at) VALUES('loop_candidate',1,101,'{}','t','{}',?)", (NOW,))
    _, text = notify_views.patient_summary_text(led.db, 1)
    sec = text.split("■ 依頼候補の返信状況（記録上）", 1)[1]
    assert "・09-30 残薬確認 — 返信: 完了（完了後に取消の記録あり）" in sec
    assert "・09-29 処方変更 — 返信: 記録なし・Loop候補（semantic shadow）あり" in sec
    assert sec.count("\n・") == 5
    assert "返信記録が見つからないことは対応がなかったことを意味しません" in sec


def test_evidence_shows_actor_counts_for_own_posts_only(led, tmp_path):
    _patient(led)
    _self_known(led)
    _post(led, 100, SELF, ts=NOW - 600)
    _post(led, 101, OTHER, ts=NOW - 500)
    led.save_reaction_actors(100, [
        {"actor_id": OTHER, "reaction_type": "viewed", "profession": "医師"},
        {"actor_id": SELF, "reaction_type": "accepted", "profession": None}],
        True, now=NOW - 60)
    snap = ledger.publish_snapshot(str(tmp_path / "data" / "ledger.db"),
                                   str(tmp_path / "snap"))
    view = mcs_view.View(snap)
    try:
        own = view.read("evidence", project=1, message_id=100)["message"]
        other = view.read("evidence", project=1, message_id=101)["message"]
    finally:
        view.close()
    actors = own["reaction_actors"]
    assert actors["counts"] == {"医師": {"viewed": 1}, "": {"accepted": 1}}
    assert actors["self_included"] is True
    assert "actor_id" not in json.dumps(actors) and other["reaction_actors"] is None
