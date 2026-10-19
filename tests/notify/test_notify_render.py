"""Signal-card evidence changes must invalidate previously rendered actions."""

import notify_scope
import json

import pytest

import notify_cards
import notify_action_context
import notify_actions
import notify_flush
import notify_render
from notify_testkit import (
    CFG, NOW, ORIGIN, _begin, _card, _dispatch, _extract, _intent,
    _latest_render, _msg, _notif, _patient, _receipt, _seed_thread,
    _signal_row, _token_for, led,
)
from notify_testkit import delivered_source

__all__ = ["led"]  # shared isolated-ledger fixture


@pytest.mark.parametrize("transport", ["discord", "slack", "lineworks"])
@pytest.mark.parametrize("corruption", ["json", "state", "type", "evidence"])
def test_latest_invalid_signal_never_revives_old_display_or_actions(
        led, transport, corruption):
    import copy
    import mcs_signals

    cfg = copy.deepcopy(CFG)
    cfg["notify"]["interactive"] = transport
    scope = dict(cfg["notify"]["discord"])
    if transport != "discord":
        scope.pop("guild_id")
        scope["team_id"] = "synthetic-team"
    cfg["notify"][transport] = scope
    _patient(led)
    _msg(led, 100)
    key = "synthetic-invalid-latest"
    _signal_row(led, key, mids=[100])
    event = _intent(led, "signal", payload={"signal_keys": [key], "project_id": 1})
    _dispatch(led, event, cfg)
    card = _card(led)
    old_fp = notify_render.source_fingerprint(led.db, card)
    _signal_row(led, key, state="resolved", mids=[100])
    latest = led.db.execute(
        "SELECT artifact_id,content FROM artifacts WHERE kind='signal_v1' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    content = json.loads(latest["content"])
    content["note"] = "INVALID-LATEST-CANARY"
    if corruption == "json":
        raw = "{invalid synthetic json"
    else:
        content[corruption] = {"state": "invalid", "type": None,
                               "evidence": []}[corruption]
        raw = json.dumps(content)
    with led.db:
        led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                       (raw, latest["artifact_id"]))
    assert mcs_signals._latest_signal_states(led.db)[key] is None
    assert notify_scope.latest_signals(led.db, [key], 1) == {}
    assert notify_render.source_fingerprint(led.db, card) != old_fp
    model = notify_render.card_content(led.db, card)
    assert model["shown"] == []
    assert "note " + key not in notify_render.display_text(model)
    assert "INVALID-LATEST-CANARY" not in notify_render.display_text(model)
    assert notify_action_context.render_context(led.db, card) == {}
    assert "note " + key not in notify_render.card_body_text(
        led.db, card, {"shown": json.dumps([key])})[1]


def test_signal_evidence_cannot_read_another_patient(led):
    _patient(led, 1)
    _patient(led, 2, name="合成患者B")
    _msg(led, 200, pid=2, body="SYNTHETIC-OTHER-PATIENT")
    sig = {"project_id": 1, "type": "med_followup", "note": "合成候補",
           "evidence": {"message_ids": [200]}}
    face = notify_render._signal_compact(led.db, 1, [sig])
    body = notify_render._signal_body(led.db, sig)
    text = notify_flush._signal_text(
        led, {"text": "候補\n場所", "project_id": 1}, sig)
    assert "SYNTHETIC-OTHER-PATIENT" not in json.dumps(face) + body + text


def test_thread_manifest_cannot_read_another_patient(led):
    _patient(led, 1)
    _patient(led, 2, name="合成患者B")
    _msg(led, 100, body="OWN-PATIENT")
    _msg(led, 200, pid=2, parent=100, body="SYNTHETIC-OTHER-PATIENT")
    card = {"kind": "thread", "root_message_id": 100, "project_id": 1}
    _, body = notify_render.card_body_text(led.db, card, {"shown": "[100,200]"})
    assert "OWN-PATIENT" in body and "SYNTHETIC-OTHER-PATIENT" not in body
    before = notify_render.source_fingerprint(led.db, card)
    led.db.execute("UPDATE messages SET content_hash='foreign-edit' WHERE message_id=200")
    assert notify_render.source_fingerprint(led.db, card) == before


def test_late_extract_landing_bumps_thread_source_fp(led):
    """extract_llm lands behind the card (drain queue); the source fp
    must carry fact-readiness or a card rendered early would keep its
    v1-only 構造化 block forever. A stale-hash extraction does not
    count as ready — latest_fact_artifact would reject it too."""
    _seed_thread(led)
    card = {"kind": "thread", "root_message_id": 100, "project_id": 1}
    before = notify_render.source_fingerprint(led.db, card)
    _extract(led, 100, {"v": 1, "symptoms": ["疼痛"]},
             kind="extract_llm", stale=True)
    assert notify_render.source_fingerprint(led.db, card) == before
    _extract(led, 100, {"v": 1, "symptoms": ["疼痛"]}, kind="extract_llm")
    assert notify_render.source_fingerprint(led.db, card) != before


def test_dispatch_does_not_follow_foreign_parent_or_event_member(led):
    _patient(led, 1)
    _patient(led, 2, name="合成患者B")
    _msg(led, 200, pid=2, body="OTHER")
    _msg(led, 100, pid=1, parent=200, body="OWN")
    _dispatch(led, _intent(led, payload={"message_ids": [100, 200]}))
    card = _card(led)
    assert card["project_id"] == 1 and card["root_message_id"] == 100
    assert led.db.execute("SELECT COUNT(*) FROM notification_cards").fetchone()[0] == 1
    content = notify_render.card_content(led.db, card)
    assert content["shown"] == [100]


def test_signal_card_rejects_foreign_artifact_and_context(led):
    _patient(led, 1)
    _patient(led, 2, name="合成患者B")
    _msg(led, 200, pid=2, body="OTHER")
    _signal_row(led, "foreign-signal", pid=2, mids=[200])
    _dispatch(led, _intent(led, "signal", payload={
        "signal_keys": ["foreign-signal"], "project_id": 1}))
    card = _card(led)
    assert notify_render.card_content(led.db, card)["shown"] == []
    assert notify_action_context.render_context(led.db, card) == {}


def test_long_thread_title_keeps_spec_deliverable(led):
    from adapters.common import spec as spec_mod
    _patient(led, 1, name="合成患者名" * 40)
    _msg(led, 100)
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    spec = json.loads(_latest_render(led)["spec_json"])
    assert spec_mod.validate(spec) is spec


@pytest.mark.parametrize("raw", ["[]", '"string"', "1", "[" * 1200],
                         ids=["array", "string", "number", "recursive"])
def test_invalid_card_state_falls_back_safely(raw):
    assert notify_render.anchor_keys({"kind": "signal", "anchor_key": raw}) == []
    assert notify_render._page(raw, 3, default=2) == 2


def test_card_list_omits_original_body_but_body_view_keeps_it(led):
    _seed_thread(led)
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101]}))
    card = _card(led)
    old_source = notify_render.card_content(led.db, card)["source_fp"]
    body = "完全合成の本文。" * 10000 + "SYNTHETIC-END"
    with led.db:
        led.db.execute("UPDATE messages SET body_text=?,content_hash=? WHERE message_id=100",
                       (body, "a" * 64))
    changes = led.db.total_changes
    statements = []
    led.db.set_trace_callback(statements.append)
    try:
        face = notify_render.card_content(led.db, card)
    finally:
        led.db.set_trace_callback(None)
    assert "body_text" not in statements[0].partition("FROM")[0], \
        "card list must not retain original bodies"
    assert face["shown"] == [100, 101]
    assert "2投稿" in notify_render.display_text(face)
    assert face["source_fp"] != old_source
    assert "SYNTHETIC-END" not in face["preview_text"]
    _, full_body = notify_render.card_body_text(
        led.db, card, {"shown": "[100]"}, max_chars=None,
        stamps=False, include_summary=False)
    assert full_body.endswith(body)
    assert led.db.total_changes == changes


@pytest.mark.parametrize(
    ("message_ids", "expected"),
    [([100, 101], "後の投稿"), ([100, "invalid"], "退院時の投稿")],
)
def test_signal_face_compact_body_selects_evidence(
        led, message_ids, expected):
    _patient(led)
    _msg(led, 100, body="最初の投稿")
    _msg(led, 101, body="後の投稿")
    _msg(led, 102, body="退院時の投稿")
    sig = {"project_id": 1, "type": "med_followup", "note": "合成候補",
           "evidence": {
        "message_ids": message_ids, "discharge_message_id": 102,
        "message_id": 100}}

    face = notify_render._signal_compact(led.db, 1, [sig])
    body = notify_render._signal_body(led.db, sig)

    assert expected in body
    assert all(other not in body for other in
               ("最初の投稿", "後の投稿", "退院時の投稿") if other != expected)
    face_text = json.dumps(face, ensure_ascii=False)
    assert "合成候補" in face_text
    assert all(t not in face_text for t in
               ("最初の投稿", "後の投稿", "退院時の投稿"))


def test_deleted_signal_evidence_differs_between_card_and_body(led):
    _patient(led)
    _msg(led, 100, body="消された本文")
    led.db.execute(
        "UPDATE messages SET body_state='deleted' WHERE message_id=100")
    sig = {"project_id": 1, "type": "med_followup", "note": "合成候補",
           "evidence": {"message_id": 100}}

    face = notify_render._signal_compact(led.db, 1, [sig])
    body = notify_render._signal_body(led.db, sig)

    assert "消された本文" not in json.dumps(face, ensure_ascii=False)
    assert "（削除された投稿）" in body and "消された本文" not in body


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_signal_evidence_change_invalidates_old_action(led, change):
    _patient(led)
    _msg(led, 100)
    _signal_row(led, "synthetic-signal", mids=[100])
    _dispatch(led, _intent(led, "signal", payload={
        "signal_keys": ["synthetic-signal"], "project_id": 1,
        "type": "med_followup"}))
    delivered_source(led, card_id=2, mids=(100,))
    notify_cards.sweep(led, CFG, now=NOW)
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    attempt = _begin(led, render)
    _receipt(led, render, attempt["attempt_id"], message_id="mid-1")
    if change == "edit":
        led.db.execute("UPDATE messages SET content_hash='edited',body_text='更新済み' WHERE message_id=100")
    else:
        led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=100")
    led.db.commit()
    # The signal artifact has not been re-evaluated yet; its evidence
    # nevertheless changed and must invalidate the button immediately.
    request = _notif(_token_for(spec, "assign"))
    request["origin"] = ORIGIN
    result = notify_actions.apply_notification(led, request, CFG, now=NOW + 1)
    assert result["outcome"] == "rejected" and result["error"] == "stale_source"


# fit_parts の折りたたみ件数表示と、body_state NULL 投稿の反応表示（完全合成）。
NOTE = "※ 記録が見つからない≠対応がなかった。"


def test_fit_parts_merges_marker_before_trailing_note():
    rows = [f"・合成行{i:02d} " + "x" * 10 for i in range(10)]
    parts = {"containers": [{"type": "text", "fold": True,
                             "text": "\n".join(rows + ["・…他5件", NOTE])}]}
    lines = notify_render.fit_parts(parts, 150)["containers"][0]["text"].split("\n")
    marks = [ln for ln in lines if notify_render._FOLD_RE.match(ln)]
    kept = [ln for ln in lines if ln.startswith("・合成行")]
    assert len(marks) == 1
    assert marks[0] == f"・…他{5 + 10 - len(kept)}件"
    assert lines[-1] == NOTE
    assert lines.index(marks[0]) == len(kept)


def test_card_reactions_keeps_posts_with_null_body_state(led):
    db = led.db
    db.execute("INSERT INTO messages(message_id,project_id,parent_id,posted_at_ts,body_state) "
               "VALUES(1,'P',NULL,1,NULL)")
    db.execute("INSERT INTO messages(message_id,project_id,parent_id,posted_at_ts,body_state) "
               "VALUES(2,'P',1,2,'full')")
    db.execute("INSERT INTO messages(message_id,project_id,parent_id,posted_at_ts,body_state) "
               "VALUES(3,'P',1,3,'deleted')")
    card = {"kind": "thread", "project_id": "P", "root_message_id": 1}
    assert [m for m, _ in notify_render.card_reactions(db, card)] == [1, 2]


# A reply with NULL posted_at_ts must not displace the thread root.
def test_null_ts_reply_does_not_become_thread_root(led):
    _patient(led)
    _msg(led, 100)
    _msg(led, 101, parent=100)
    led.db.execute("UPDATE messages SET reply_count=5 WHERE message_id=100")
    led.db.execute("UPDATE messages SET posted_at_ts=NULL, posted_at='bad' "
                   "WHERE message_id=101")
    led.db.commit()
    _dispatch(led, _intent(led))
    content = notify_render.card_content(led.db, _card(led))
    assert content["shown"][0] == 100
    text = notify_render.display_text(content)
    assert "09-24〜" in text
    assert "返信未取得 4件" in text
