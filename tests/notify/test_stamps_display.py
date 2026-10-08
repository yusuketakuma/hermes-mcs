"""自分の投稿への反応・自分宛メンション・しおり等の表示（読取り専用・完全合成）。"""
import json
import asyncio
import sys
from types import SimpleNamespace

import pytest

import ledger
import mcs_signals
import mcs_view
import notify_digest
import notify_render
import notify_views
from message_metadata import (
    actor_line, flag_lines, get_message_metadata, mentions_self, stamp_line)
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

    def own():
        return stamp_line(get_message_metadata(led.db, 100) | {"own_post": True})
    assert own() == "スタンプ 未取得"
    _meta(led, 100, reactions=[_r("viewed", 3, True), _r("accepted", 1), _r("good", 0)])
    assert own().startswith("スタンプ 👀2 🙆1（自分 👀） · 観測 ")
    assert "👍" not in own()
    _meta(led, 100, reactions=[_r("viewed", 1, True)])
    assert own().startswith("スタンプ 他者なし（自分 👀）")
    _meta(led, 100, reactions=[])
    assert own().startswith("スタンプ なし · 観測 ")


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
    # own post counts others only (1), the other's post counts both (2)
    assert "スタンプ 👀3 · 自分 2投稿" in footer
    body = notify_render._card_body_text(
        led.db, _card(led), {"shown": "[100, 101]"}, max_chars=None)[1]
    own, other = body.split("\n\n")
    assert "スタンプ 👀1 · 自分 👀 · " in own and "スタンプ 👀2 · 自分 👀 · " in other


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
    assert _section(_digest(led, CFG), "反応がない自分の投稿") is None
    sec = _section(_digest(led), "反応がない自分の投稿")
    assert "反応なし 2投稿" in sec
    assert "・project 1 2日経過・観測" in sec and "・project 1 7日経過" in sec
    for mid in (101, 102, 104, 105, 106, 107, 108, 109):
        assert f"message {mid} " not in sec
    assert "スタンプ未取得 1投稿・取得不正 1" in sec
    assert "合成同名" not in sec and "本文" not in sec


def test_unreacted_section_without_self_id_and_scope(led):
    _patient(led)
    _patient(led, 2)
    _post(led, 100, SELF, ts=NOW - 100)
    _meta(led, 100, reactions=[])
    assert "本人の送信者IDが不明" in _section(_digest(led), "反応がない自分の投稿")
    _self_known(led)
    _post(led, 200, SELF, ts=NOW - 100, pid=2)
    _meta(led, 200, reactions=[])
    sec = _section(_digest(led, allowed=[2]), "反応がない自分の投稿")
    assert "・project 2 " in sec and "・project 1 " not in sec
    flt = notify_digest.parse_scope("project:1")
    sec = _section(_digest(led, flt=flt), "反応がない自分の投稿")
    assert "・project 1 " in sec and "・project 2 " not in sec


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
    sec = _section(_digest(led), "自分宛で応答なし")
    # 110, 130 and 180 only — rows name the room, never internal ids
    assert sec.count("・project 1 自分宛・") == 3 and "message " not in sec
    assert "自分の返信観測なし・本人スタンプ観測なし" in sec
    assert "自分宛・1時間経過・自分の返信観測なし・本人スタンプ未取得" in sec
    assert "施設宛: 1投稿" in sec
    assert "宛先不明: 1投稿" in sec
    assert "記録が見つからない≠対応がなかった" not in sec
    assert "合成同名" not in sec and "本文" not in sec
    # 同じ表示は publish 設定に依らない（capture の値を読むだけ）
    assert _section(_digest(led, CFG), "自分宛で応答なし") == sec


def test_addressed_section_without_self_id_lists_no_mentions(led):
    _patient(led)
    _post(led, 110, OTHER, ts=NOW - 5000)
    _meta(led, 110, mentions=[{"type": "user", "id": SELF}], reactions=[])
    _signal_row(led, "pru:1:110", stype="pharmacist_request_unanswered", mids=[110])
    cfg = {**PUB, "signals": {"notify": True}}
    sec = _section(_digest(led, cfg), "自分宛で応答なし")
    assert "本人の送信者IDが不明のためメンションは判定していません" in sec
    assert "本人宛メンション" not in sec
    assert "薬剤師宛の依頼に応答なし・本人ID不明" in sec
    assert _section(_digest(led, cfg, allowed=[2]), "自分宛で応答なし") is None


def test_self_mentioned_and_evidence_flags(led, tmp_path):
    def self_mentioned(db, mid):
        return mentions_self(get_message_metadata(db, mid), mcs_signals.self_sender_id(db))

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
    cov = _section(_digest(led), "取得状況")
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
        sec = _section(text, "反応がない自分の投稿")
        assert "反応なし 40投稿" in sec and "スタンプ未取得 1投稿" in sec
        assert "・…他" in sec
        assert "取得状況" in text
    full = _section(notify_render.parts_text(parts), "反応がない自分の投稿")
    assert full.count("0時間経過・観測") == notify_digest.MAX_LIST
    assert "・…他30件" in full


def test_patient_summary_request_reply_states(led):
    _patient(led)
    _msg(led, 101)
    reqs = [{"kind": "pharmacist", "ctx": "残薬確認", "at": "2026-09-30T09:00",
             "mid": 100, "reply_state": "done", "reply_conflict": True},
            {"kind": "doctor", "ctx": "処方変更", "at": "2026-09-29T09:00", "mid": 101}]
    reqs += [{"ctx": f"依頼{i}", "at": "2026-09-01T09:00", "mid": 200 + i} for i in range(5)]
    led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,"
                   "created_at) VALUES('patient_rollup',1,NULL,?,'t','{}',?)",
                   (json.dumps({"recent_requests": reqs}), NOW))
    led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,"
                   "created_at) VALUES('loop_candidate',1,101,?,'t','{}',?)",
                   (json.dumps({"origin": {"revision": f"{101:064x}"}}), NOW))
    _, text = notify_views.patient_summary_text(led.db, 1)
    sec = text.split("■ 依頼候補の返信状況（記録上）", 1)[1].split("\n■ ", 1)[0]
    assert "・09-30 残薬確認 — 返信: 完了（完了後に取消の記録あり）" in sec
    assert "・09-29 処方変更 — 返信: 記録なし・Loop候補（semantic shadow）あり" in sec
    assert sec.count("\n・") == 5
    assert "返信記録が見つからないことは対応がなかったことを意味しません" not in sec


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


def test_every_post_reads_summary_then_stamps_then_body(led, monkeypatch):
    """Owner order (2026-10-03) for every message: header, 📋 summary,
    MCS stamps, then the body as posted to MCS."""
    _patient(led)
    _self_known(led)
    _post(led, 100, OTHER, ts=NOW - 100)
    _post(led, 101, SELF, ts=NOW - 50, parent=100)
    _meta(led, 100, reactions=[_r("accepted", 2, True), _r("good", 1)])
    monkeypatch.setattr(notify_render, "_structured_block", lambda db, mid, **_kw: {
        "type": "text", "text": f"📋 要約\n・要約{mid}"})
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101]}))
    body = notify_render._card_body_text(
        led.db, _card(led), {"shown": "[100, 101]"}, max_chars=None)[1]
    for post, mid, stamps in zip(body.split("\n\n"), (100, 101),
                                 ("スタンプ 🙆2 👍1 · 自分 🙆 · ",
                                  "スタンプ 未取得")):
        lines = post.split("\n")
        assert lines[1:3] == ["📋 要約", f"・要約{mid}"]
        assert lines[3].startswith(stamps)        # one merged stamp line
        assert lines[4:6] == [notify_render.SECTION_RULE, "📄 本文"]
        assert post.count(notify_render.SECTION_RULE) == 1
        assert lines[6] == led.db.execute(
            "SELECT body_text FROM messages WHERE message_id=?", (mid,)).fetchone()[0]


def test_long_post_keeps_summary_and_stamps_with_its_body():
    """Regression: a long body started its own chunk, leaving the
    header / summary / stamp lines as a post of their own."""
    import notify_cards
    head = "10-03 08:00 職員\n📋 要約\n・要約\nスタンプ 👀9 🙏1 · 観測 10-03 08:05\n" + notify_render.SECTION_RULE + "\n📄 本文\n"
    for body in ("本" * 1852, "本" * 3000, ("行\n" * 1200)):
        chunks = notify_cards._split_body_chunks(head + body)
        assert "".join(chunks) == head + body
        assert all(len(c) <= notify_cards.THREAD_PART_LIMIT for c in chunks)
        assert chunks[0].startswith(head) and len(chunks[0]) > len(head)


def test_thread_post_names_who_pressed(led, monkeypatch):
    """#22-D2 (2026-10-03): the thread post names the people behind each
    stamp, right under the stamp line; the card face stays one line."""
    _patient(led)
    _self_known(led)
    _post(led, 100, SELF, ts=NOW - 100)
    _meta(led, 100, reactions=[_r("viewed", 2), _r("accepted", 1, True)])
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    body = lambda: notify_render._card_body_text(  # noqa: E731
        led.db, _card(led), {"shown": "[100]"}, max_chars=None)[1]
    def stamp():
        return next(ln for ln in body().split("\n") if ln.startswith("スタンプ "))
    # never walked yet: counts only, one line
    assert stamp() == "スタンプ 👀2 · 自分 🙆 · 09-21 23:12時点"
    led.save_reaction_actors(100, [
        {"actor_id": OTHER, "reaction_type": "viewed", "name": "合成 一郎", "profession": "医師"},
        {"actor_id": 9, "reaction_type": "viewed", "name": "合成 花子"},
        {"actor_id": SELF, "reaction_type": "accepted", "name": "合成 自分"}], True, now=NOW - 50)
    monkeypatch.setattr(ledger.time, "time", lambda: NOW + 86400)
    assert stamp() == ("スタンプ 👀2 合成 一郎・合成 花子 · 自分 🙆 · 押した人 古い情報"
                       " · 09-21 23:12時点")       # pinned clock: 24h past
    monkeypatch.setattr(ledger.time, "time", lambda: NOW)
    assert stamp() == "スタンプ 👀2 合成 一郎・合成 花子 · 自分 🙆 · 09-21 23:12時点"
    footer = "\n".join(x["text"] for x in notify_render._card_content(led.db, _card(led))["footer"])
    assert "合成 一郎" not in footer


@pytest.mark.parametrize(("summary", "expected"), [
    (None, "押した人: 未取得"),
    ({"complete_at": None, "state": "not_fetched"}, "押した人: 未取得"),
    ({"complete_at": None, "state": "failed"}, "押した人: 未取得（取得失敗）"),
    ({"complete_at": NOW - 10, "state": "complete", "actors": []},
     "押した人: なし · 観測 09-21 23:13"),
    ({"complete_at": NOW - 10, "state": "stale", "actors": []},
     "押した人（古い情報）: なし · 観測 09-21 23:13"),
    ({"complete_at": NOW - 10, "state": "failed", "actors": []},
     "押した人（再取得失敗）: なし · 観測 09-21 23:13"),
])
def test_stamp_actor_fetch_states_are_explicit(summary, expected):
    assert actor_line(summary) == expected


def test_stamp_actor_names_self_unknown_and_kinds_are_distinct():
    actors = [
        {"reaction_type": "accepted", "name": "合成 自分", "profession": "医師", "self": True},
        {"reaction_type": "viewed", "name": "合成 花子", "profession": "看護師", "self": False},
        {"reaction_type": "good", "name": None, "profession": None, "self": False},
        {"reaction_type": "future", "name": "合成 将来", "profession": None, "self": False},
        {"reaction_type": "viewed", "name": "合成 自分", "profession": "医師", "self": True},
    ]
    assert actor_line({"state": "complete", "complete_at": NOW - 10, "actors": actors}) == (
        "押した人: 👀 合成 花子（看護師）・合成 自分（医師）（自分） / "
        "🙆 合成 自分（医師）（自分） / 👍 氏名不明 / ❔ 合成 将来 · 観測 09-21 23:13")


def test_stamp_actor_names_have_one_line_and_a_finite_budget():
    actors = [{"reaction_type": "viewed", "name": "合成\r\n\x00\u202e<@UFAKE> @everyone" + "名" * 1000,
               "profession": "職\n" * 1000, "self": False} for _ in range(15)]
    actors += [{"reaction_type": "accepted", "name": "合成別名", "profession": None, "self": False}]
    text = actor_line({"state": "complete", "complete_at": NOW - 10, "actors": actors})
    assert "\n" not in text and "\r" not in text and "\x00" not in text and "\u202e" not in text
    assert "<" not in text and ">" not in text and "@" not in text
    assert len(text) < 1000
    assert text.count("合成") == 12 and "他3名" in text and "🙆 1名" in text
    assert text.count("…") == 24


@pytest.mark.parametrize("platform", ["slack", "discord", "lineworks"])
def test_stamp_actor_names_reach_each_platform_without_pings(led, monkeypatch, platform):
    from adapters.discord import cards as discord_cards, delivery as discord_delivery
    from adapters.lineworks import cards as lineworks_cards, delivery as lineworks_delivery
    from adapters.slack import cards as slack_cards, delivery as slack_delivery
    from discord_delivery_testkit import FakeHTTPClient
    from discord_testkit import _fake_discord
    from notify_testkit import _latest_render
    from slack_testkit import SLACK
    from test_notify_lineworks import LINEWORKS

    monkeypatch.setattr(ledger.time, "time", lambda: NOW)
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    _patient(led)
    _self_known(led)
    _post(led, 100, SELF, ts=NOW - 100)
    _post(led, 101, OTHER, ts=NOW - 50, parent=100)
    for mid in (100, 101):
        _meta(led, mid, reactions=[_r("viewed", 2, True)])
        led.save_reaction_actors(mid, [
            {"actor_id": SELF, "reaction_type": "viewed", "name": f"合成本人{mid}"},
            {"actor_id": OTHER, "reaction_type": "viewed", "name": f"合成他者{mid}<@UFAKE>"},
        ], True, now=NOW - 10)
    cfg = {"slack": SLACK, "discord": CFG, "lineworks": LINEWORKS}[platform]
    assert _dispatch(led, _intent(led), cfg)["dispatched"]
    spec = json.loads(_latest_render(led)["spec_json"])
    footer = "\n".join(item["text"] for item in spec["parts"]["footer"] if item["type"] == "text")
    assert "スタンプ 👀3 · 自分 2投稿" in footer and "MCS 👀" not in footer
    assert "合成本人" not in footer and "合成他者" not in footer
    if platform == "slack":
        blocks = slack_cards.render(spec)[1]
        assert any("スタンプ 👀3" in element.get("text", "") for block in blocks
                   for element in block.get("elements", []))
        stamp_context = [element for block in blocks if block["type"] == "context"
                         for element in block["elements"] if "スタンプ" in element["text"]]
        assert len(stamp_context) == 1 and stamp_context[0]["type"] == "plain_text"
    elif platform == "discord":
        view = discord_cards.build_view(spec)
        assert any("-# スタンプ 👀3" in getattr(child, "content", "")
                   for container in view.items for child in container.children)
    else:
        shown = lineworks_cards.render(spec)
        visible = (shown["contents"]["body"]["contents"][0]["text"]
                   if shown["type"] == "flex" else shown.get("contentText", shown.get("text", "")))
        assert "スタンプ 👀3" in visible

    calls = []

    async def send(text=None, **kwargs):
        calls.append((text, kwargs))
        return SimpleNamespace(id=123)

    async def slack_send(**kwargs):
        calls.append((kwargs["text"], kwargs))
        return {"ok": True, "ts": "1790000000.000001", "channel": kwargs["channel"]}

    if platform == "slack":
        worker = object.__new__(slack_delivery.DeliveryWorker)
        worker._settings = {"channel_id": "C_SYNTHETIC"}
        worker._sender = SimpleNamespace(single_attempt=lambda: SimpleNamespace(chat_postMessage=slack_send))
        context = {"thread_id": "1790000000.000100"}
    elif platform == "discord":
        worker = object.__new__(discord_delivery.DeliveryWorker)
        worker._bot = SimpleNamespace(http=FakeHTTPClient())
        context = {"thread": SimpleNamespace(send=send)}
    else:
        worker = object.__new__(lineworks_delivery.DeliveryWorker)
        worker._sender = SimpleNamespace(route_current=lambda _spec: True,
                                         send=lambda content: calls.append((content["text"], {})))
        context = {"card_message_id": "lw:" + "c" * 32}
    posts = {}
    for part in spec["parts"]["manifest"]:
        if part["kind"] == "body_part":
            result = asyncio.run(worker._perform_part({"spec": spec}, part, context))
            assert result["result"] == "delivered"
            if part["name"].startswith("m:"):
                mid = int(part["name"].split("#", 1)[0][2:])
                posts.setdefault(mid, []).append(calls[-1][0])
    assert set(posts) == {100, 101}
    for mid in (100, 101):
        post = "".join(posts[mid])
        assert "<@" not in post and "@everyone" not in post
        assert post.count(notify_render.SECTION_RULE) == 1
        front, body = post.split(notify_render.SECTION_RULE, 1)
        assert ("📋 要約" in front) is (platform == "lineworks")
        if platform != "lineworks":
            assert "解析更新中" not in post
        assert body.startswith("\n📄 本文\n")
        if platform == "lineworks":
            # LINE WORKS cannot edit a post: no stamp line at all, so a
            # stamp change never re-posts the body
            assert "スタンプ" not in post and "合成他者" not in post
            continue
        assert f"合成本人{201 - mid}" not in post
        if mid == 100:
            # one's own post counts and names others only
            assert "スタンプ 👀1 合成他者100＜＠UFAKE＞ · 自分 👀 · " in post
        else:
            assert "スタンプ 👀2 合成本人101・合成他者101＜＠UFAKE＞ · 自分 👀 · " in post
    for _, kwargs in calls:
        if platform == "slack":
            assert kwargs["link_names"] is False and kwargs["unfurl_links"] is False
        elif platform == "discord":
            mentions = kwargs["allowed_mentions"]
            assert not any((mentions.everyone, mentions.users, mentions.roles, mentions.replied_user))


def _stamp_settle(led, render, result="delivered"):
    import notify_cards
    import notify_cmds
    from adapters.common import envelopes

    spec = json.loads(render["spec_json"])
    claim = {"spec": spec, "payload_hash": render["payload_hash"],
             "attempt_id": f"{spec['render_rev']:016x}", "worker_id": "b" * 16}

    def apply(req):
        assert notify_cmds.validate_int(req) is None
        return notify_cmds.dispatch(led, req, CFG, notify_cards.data_root(led), now=NOW)

    assert apply(envelopes.transport_begin(claim))["granted"]
    assert apply(envelopes.transport_receipt(
        claim, result, message_id="m-1" if result == "delivered" else None,
        error_code="synthetic_unknown" if result == "unknown" else None))["applied"]
    if result == "delivered":
        for part in spec["parts"]["manifest"][1:]:
            assert apply(envelopes.part_receipt(
                claim, part, "delivered", remote_id="synthetic-" + part["part_id"]))["applied"]


@pytest.mark.parametrize("state", ["queued", "delivered", "unknown"])
def test_stamp_actor_late_names_and_fetch_state_refresh_existing_parts(led, monkeypatch, state):
    import notify_cards
    from notify_testkit import _latest_render

    monkeypatch.setattr(ledger.time, "time", lambda: NOW)
    _patient(led)
    _self_known(led)
    _post(led, 100, SELF, ts=NOW - 100)
    _meta(led, 100, reactions=[_r("viewed", 2)])
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    first = _latest_render(led)
    original_card = _card(led)
    if state != "queued":
        _stamp_settle(led, first, state)
    led.save_reaction_actors(100, [{"actor_id": OTHER, "reaction_type": "viewed", "name": "合成後着"}],
                             True, now=NOW - 10)
    specs = []
    with led.db:
        issued = notify_cards._issue_render(led.db, 1, CFG, NOW, specs)
    if state == "unknown":
        assert issued is None and specs == []
        assert _latest_render(led)["delivery_id"] == first["delivery_id"]
        return
    assert issued is not None
    fresh = _latest_render(led)
    assert fresh["render_rev"] == first["render_rev"] + 1
    assert _card(led)["source_generation"] == original_card["source_generation"]
    assert "合成後着" in "".join(specs[0]["parts"]["thread_body_parts"])
    if state == "delivered":
        assert next(p for p in specs[0]["parts"]["manifest"] if p["kind"] == "body_part")["prior_remote_id"]
    # The next failed fetch preserves the complete names and still owes a visible state update.
    led.save_reaction_actors(100, [], False, error="synthetic_failure", now=NOW)
    specs = []
    with led.db:
        assert notify_cards._issue_render(led.db, 1, CFG, NOW, specs) is not None
    body = "".join(specs[0]["parts"]["thread_body_parts"])
    assert "スタンプ 👀2 合成後着 · 押した人 取得失敗 · " in body


def test_stamp_actor_late_name_updates_an_existing_post_outside_the_card_page(led, monkeypatch):
    import notify_cards
    from notify_testkit import _latest_render

    monkeypatch.setattr(ledger.time, "time", lambda: NOW)
    _patient(led)
    mids = list(range(100, 110))
    for mid in mids:
        _post(led, mid, OTHER, ts=NOW - 200 + mid, parent=100 if mid != 100 else None)
        _meta(led, mid, reactions=[_r("viewed")])
    assert _dispatch(led, _intent(led, payload={"message_ids": mids}))["dispatched"]
    first = _latest_render(led)
    initial = json.loads(first["spec_json"])
    assert 100 not in json.loads(led.db.execute(
        "SELECT shown FROM notification_view_manifests WHERE manifest_id=?", (first["manifest_id"],)
    ).fetchone()[0])
    _stamp_settle(led, first)
    led.save_reaction_actors(100, [{"actor_id": OTHER, "reaction_type": "viewed", "name": "合成前ページ"}],
                             True, now=NOW - 10)
    fresh = []
    with led.db:
        assert notify_cards._issue_render(led.db, 1, CFG, NOW, fresh) is not None
    assert "合成前ページ" in "".join(fresh[0]["parts"]["thread_body_parts"])
    old_body = [p for p in initial["parts"]["manifest"] if p["kind"] == "body_part"]
    new_body = [p for p in fresh[0]["parts"]["manifest"] if p["kind"] == "body_part"]
    assert {p["name"] for p in new_body} == {p["name"] for p in old_body}
    assert all(p["prior_remote_id"] == "synthetic-" + p["part_id"] for p in new_body)
    assert fresh[0]["source_generation"] == initial["source_generation"]


def test_stamp_actor_partial_walk_is_unknown_and_preserves_the_last_complete_set(led, monkeypatch):
    monkeypatch.setattr(ledger.time, "time", lambda: NOW)
    _patient(led)
    _post(led, 100, OTHER, ts=NOW - 100)
    rows = [{"actor_id": OTHER, "reaction_type": "viewed", "name": "合成部分氏名"}]
    led.save_reaction_actors(100, rows, False, error="incomplete", now=NOW - 10)
    assert actor_line(ledger.reaction_actor_summary(led.db, 100)) == "押した人: 未取得（取得失敗）"
    rows[0]["name"] = "合成完全氏名"
    led.save_reaction_actors(100, rows, True, now=NOW - 10)
    rows[0]["name"] = "合成失敗氏名"
    led.save_reaction_actors(100, rows, False, error="incomplete", now=NOW)
    assert actor_line(ledger.reaction_actor_summary(led.db, 100)) == (
        "押した人（再取得失敗）: 👀 合成完全氏名 · 観測 09-21 23:13")


def test_stamp_actor_refresh_keeps_human_confirmation_and_source_identity(led, monkeypatch):
    import notify_cards
    from adapters.common import envelopes
    from notify_testkit import _latest_render, _token_for

    monkeypatch.setattr(ledger.time, "time", lambda: NOW)
    _patient(led)
    _post(led, 100, OTHER, ts=NOW - 100)
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    first = _latest_render(led)
    _stamp_settle(led, first)
    spec = json.loads(first["spec_json"])
    origin = {k: spec["delivery"][k] for k in ("profile", "application_id", "channel_id", "guild_id")}
    origin["message_id"] = "m-1"
    req = envelopes.notification(_token_for(spec, "ack"), "discord:1001", origin)
    assert notify_cards.apply_notification(led, req, CFG, now=NOW)["outcome"] == "applied"
    _stamp_settle(led, _latest_render(led))
    before = _card(led)
    led.save_reaction_actors(100, [{"actor_id": OTHER, "reaction_type": "viewed", "name": "合成後着"}],
                             True, now=NOW - 10)
    specs = []
    with led.db:
        assert notify_cards._issue_render(led.db, 1, CFG, NOW, specs) is not None
    after = _card(led)
    assert (after["source_generation"], after["source_fp"]) == (before["source_generation"], before["source_fp"])
    assert after["presentation_generation"] == before["presentation_generation"] + 1
    assert "✅ 確認: <@1001>" in "\n".join(item.get("text", "") for item in specs[0]["parts"]["footer"])
    assert led.db.execute("SELECT count(*) FROM notification_acknowledgements WHERE withdrawn_at IS NULL").fetchone()[0] == 1


@pytest.mark.parametrize("platform", ["slack", "discord", "lineworks"])
@pytest.mark.parametrize("ready", [True, False])
def test_legacy_text_notice_has_summary_stamps_then_one_body_section(led, monkeypatch, platform, ready):
    import notify_flush
    _patient(led)
    _self_known(led)
    _post(led, 100, OTHER, ts=NOW - 100)
    _meta(led, 100, reactions=[_r("viewed", 2)])
    with led.db:
        led.db.execute("UPDATE messages SET body_html='<p>完全合成の本文です。</p>' WHERE message_id=100")
    cfg = {**CFG, "notify_target": platform + ":synthetic"}
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    monkeypatch.setattr(notify_flush.structured_view, "structured_lines", lambda db, mid, **_kw: ["合成の要約"] if ready else [])
    event = {"kind": "new_messages", "project_id": 1, "payload": '{"message_ids":[100]}'}
    text, files = notify_flush._format_event(led, event)
    assert text.count(notify_render.SECTION_RULE) == 1 and files == []
    front, body = text.split(notify_render.SECTION_RULE, 1)
    assert "📋 要約" in front
    assert ("合成の要約" if ready else "処理待ち") in front
    assert ("スタンプ 👀2" in front) is (platform != "lineworks")
    assert body == "\n📄 本文\n完全合成の本文です。"


def test_legacy_text_audited_supplement_stays_in_summary_section(led, monkeypatch):
    import notify_flush
    _patient(led)
    _post(led, 100, OTHER, ts=NOW - 100)
    with led.db:
        led.db.execute("UPDATE messages SET body_html='<p>完全合成の本文です。</p>' WHERE message_id=100")
    monkeypatch.setattr(notify_flush, "_config", lambda: CFG)
    monkeypatch.setattr(notify_flush, "_sem_block", lambda *_args: "\n監査済みの合成要約")
    text, _files = notify_flush._format_event(led, {"kind": "new_messages", "project_id": 1, "payload": '{"message_ids":[100]}'})
    front, body = text.split(notify_render.SECTION_RULE, 1)
    assert "監査済みの合成要約" in front and "監査済み" not in body


def test_legacy_text_empty_summary_keeps_current_failure_state(led, monkeypatch):
    import notify_flush
    _patient(led)
    _post(led, 100, OTHER, ts=NOW - 100)
    with led.db:
        led.db.execute("UPDATE messages SET body_html='<p>完全合成の本文です。</p>' WHERE message_id=100")
        content_hash = led.db.execute("SELECT content_hash FROM messages WHERE message_id=100").fetchone()[0]
        led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) VALUES('extract_llm',1,100,'{}',?,?)",
                       (json.dumps({"hash": content_hash, "error": 1, "attempts": 5}), NOW))
    monkeypatch.setattr(notify_flush, "_config", lambda: CFG)
    text, _files = notify_flush._format_event(led, {"kind": "new_messages", "project_id": 1, "payload": '{"message_ids":[100]}'})
    front, body = text.split(notify_render.SECTION_RULE, 1)
    assert "📋 要約 作成失敗" in front and "処理待ち" not in front
    assert body == "\n📄 本文\n完全合成の本文です。"
    with led.db:
        led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=100")
    with pytest.raises(notify_flush._StaleSend, match="messages_unavailable"):
        notify_flush._format_event(led, {"kind": "new_messages", "project_id": 1, "payload": '{"message_ids":[100]}'})
