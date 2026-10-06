"""Card layout zones (2026-10-05): heading identity, context/change lines,
per-post 要約 states, state footer, primary buttons, thread posts, and the
LINE WORKS planning rules. Synthetic ledger only."""
import json

import notify_cards
import notify_render
from notify_testkit import (
    NOW, _add_request, _card, _deliver, _dispatch, _intent,
    _latest_render, _msg, _patient, _seed_thread, _signal_row, _spec,
    led as led,
)
from test_notify_lineworks import LINEWORKS, _deliver as _lw_deliver, _render as _lw_render


def _texts(spec):
    return [c.get("text", "") for c in spec["parts"]["containers"]]


def test_heading_names_patient_station_and_same_name_suffix(led):
    _seed_thread(led)
    led.db.execute("UPDATE patients SET station_name='あおぞら' WHERE project_id=1")
    _patient(led, 2345, name="患者A")              # a same-name room
    _dispatch(led, _intent(led))
    head = _texts(_spec(led))[0]
    assert head == "💬 患者A（あおぞら） #1 · 起点 09-24"


def test_context_line_counts_posts_missing_replies_files_and_mention(led):
    _seed_thread(led)
    led.db.execute("UPDATE messages SET reply_count=3 WHERE message_id=100")
    led.db.execute("INSERT INTO attachments(message_id,name,state) "
                   "VALUES(101,'a.jpg','downloaded')")
    led.db.commit()
    _dispatch(led, _intent(led))
    texts = _texts(_spec(led))
    assert texts[1] == "2投稿 · 返信未取得 2件 · 📎 1"
    # each post is its own zone: rule before it, its 要約 state after it
    rules = [c for c in _spec(led)["parts"]["containers"] if c.get("rule")]
    assert len(rules) == 2 and texts.count("📋 要約 処理待ち") == 2


def test_new_replies_since_the_card_was_posted(led):
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    _deliver(led)
    created = _card(led)["created_at"]
    _msg(led, 101, parent=100)
    led.db.execute("UPDATE messages SET first_seen=? WHERE message_id=101",
                   (created + 5,))
    led.db.commit()
    _dispatch(led, _intent(led, payload={"message_ids": [101]}))
    assert "🆕 返信+1" in _texts(_spec(led))


def test_failed_extraction_is_named_on_the_post(led):
    _seed_thread(led, mids=(100,))
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,"
        "created_at) VALUES('extract_llm',1,100,'{}','m',?,?)",
        (json.dumps({"error": 1, "attempts": 5, "hash": f"{100:064x}"}), NOW))
    led.db.commit()
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    assert "📋 要約 作成失敗" in _texts(_spec(led))


def test_footer_is_one_state_item_and_revoked_card_has_no_buttons(led):
    _seed_thread(led)
    _add_request(led, title="期限切れ", due="2020-01-01")
    led.db.execute("UPDATE patients SET fetch_state='incomplete'")
    led.db.commit()
    _dispatch(led, _intent(led))
    footer = [f for f in _spec(led)["parts"]["footer"] if f["type"] == "text"]
    assert len(footer) == 1
    assert footer[0]["text"].splitlines()[0].startswith("📝 タスク 1件（期限切れ 1） · ")
    assert "⚠ 履歴取得未完了" in footer[0]["text"]
    card = _card(led) | {"delivery_state": "revoked"}
    content = notify_render._card_content(led.db, card)
    assert "⛔ 取り下げ済み" in content["footer"][0]["text"]
    assert notify_cards._action_rows(led.db, card, content, NOW) == []


def test_signal_face_labels_type_and_state_in_japanese(led):
    _patient(led)
    _msg(led, 100)
    _signal_row(led, "s1", stype="request_overdue", state="resolved", mids=[100])
    _signal_row(led, "s2", stype="discharge_notice", mids=[100])
    _dispatch(led, _intent(led, "signal", payload={
        "signal_keys": ["s1", "s2"], "project_id": 1}))
    texts = _texts(_spec(led))
    assert texts[0] == "💬 アラート ［要確認］"           # discharge is immediate
    assert "・【期限超過】note s1（解消）" in texts[-1]
    assert "・【退院連絡】note s2（未確認）" in texts[-1]
    assert "resolved" not in "".join(texts)


def test_signal_thread_posts_are_keyed_per_signal(led):
    _patient(led)
    _msg(led, 100)
    _signal_row(led, "s1", mids=[100])
    _signal_row(led, "s2", mids=[100])
    _dispatch(led, _intent(led, "signal", payload={
        "signal_keys": ["s1", "s2"], "project_id": 1}))
    names = [p["name"] for p in _spec(led)["parts"]["manifest"]
             if p["kind"] == "body_part"]
    assert len(names) == 2 and all(n.startswith("s:") for n in names)
    assert names[0] != names[1]
    body = "".join(_spec(led)["parts"]["thread_body_parts"])
    assert "/ 状態: 未確認" in body and "↳ 患者A · " in body
    assert "[MCS]" not in body and "project 1" not in body


def test_attachment_parts_carry_a_caption(led):
    _seed_thread(led)
    led.db.execute("INSERT INTO attachments(message_id,name,state,local_path,"
                   "sha256,bytes) VALUES(100,'a.jpg','downloaded','/x/a.jpg',?,1)",
                   ("ab" * 32,))
    led.db.commit()
    _dispatch(led, _intent(led))
    att = next(p for p in _spec(led)["parts"]["manifest"]
               if p["kind"] == "attachment_part")
    assert "患者A:" in att["caption"] and "職員（所属未取得） / 09-24 08:40" in att["caption"]
    assert "要約処理待ち" in att["caption"] and "📎 a.jpg" in att["caption"]


def test_lineworks_card_member_names_repost_marker_and_more(led):
    cfg = json.loads(json.dumps(LINEWORKS))
    cfg["notify"]["lineworks"]["user_names"] = {"u-1": "田中"}
    render = _lw_render(led)
    spec = json.loads(render["spec_json"])
    rows = spec["parts"]["action_rows"]
    assert [b["id"] for b in rows[0]] == ["ack", "assign", "request", "link", "more"]
    assert not any(p["name"].startswith("actions#")
                   for p in spec["parts"]["manifest"] if p["kind"] == "body_part")
    # names: a configured member and an unknown one, never a mention
    card = _card(led)
    footer = [{"type": "text", "text": "✅ 確認: <@u-1>・<@u-2>"}]
    out = notify_render.lineworks_member_names(footer[0]["text"],
                                               cfg["notify"]["lineworks"]["user_names"])
    assert out == "✅ 確認: 田中・メンバー"
    assert notify_render.actor_label("lineworks:40029600:u-1") == "<@u-1>"
    assert card["transport"] == "lineworks"
    marked = notify_cards._mark_reposted(spec["parts"]["containers"])
    assert "🔄 更新版" in [c.get("text") for c in marked]


def _lw_reissue(led, change=False):
    render = _lw_render(led)
    spec = json.loads(render["spec_json"])
    assert not any("スタンプ" in b for b in spec["parts"]["thread_body_parts"])
    _lw_deliver(led, render)
    led.db.execute(
        "UPDATE notification_render_parts SET state='delivered',"
        "remote_id='lw:p-'||part_id WHERE delivery_id=? AND kind='body_part'",
        (render["delivery_id"],))
    if change:
        led.db.execute("UPDATE notification_render_parts SET "
                       "payload_sha256='changed' WHERE kind='body_part'")
    led.db.commit()
    with led.db:
        assert notify_cards._issue_render(led.db, 1, LINEWORKS, NOW + 1, [],
                                          force=True)
    again = json.loads(_latest_render(led)["spec_json"])
    assert again["op"] == "update"
    return [p for p in again["parts"]["manifest"] if p["kind"] == "body_part"]


def test_lineworks_identical_post_is_not_reposted(led):
    posts = _lw_reissue(led)
    assert posts and all(p.get("prior_remote_id") for p in posts)


def test_lineworks_changed_post_is_posted_anew(led):
    posts = _lw_reissue(led, change=True)
    assert posts and not any(p.get("prior_remote_id") for p in posts)


def test_body_chunks_never_end_blank():
    for text in ("a" * 1898 + "\n\n\n", "x" * 1899 + "\n" + " " * 30):
        chunks = notify_cards._split_body_chunks(text)
        assert "".join(chunks) == text
        assert all(c.strip() for c in chunks)
        assert all(len(c) <= notify_cards.THREAD_PART_LIMIT for c in chunks)


def test_lineworks_card_split_breaks_at_a_line():
    text = "\n".join(["行" * 30] * 40)
    head, rest = notify_render.lineworks_card_split(text)
    assert len(head) <= 1000 and head.endswith("\n↓ 続き")
    assert head.removesuffix("\n↓ 続き") + "\n" + rest == text
    assert notify_render.lineworks_card_split("短い") == ("短い", "")
