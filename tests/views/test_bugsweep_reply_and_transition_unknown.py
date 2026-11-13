"""An unevaluated reply or an unanalysed post is unknown, never a missing reply or record (synthetic only)."""
import mcs_contract

import notify_views
import patient_context_view as pcv
from patient_context import extract_context
from semantic_testkit import _message
from test_patient_context_v3_views import DAY, T0, _requests, ctx  # noqa: F401 - shared synthetic fixture
from test_read_model import _artifact, _hash_of
from test_shortcut_debt_request_replies import seed_loop_replies

__all__ = ["ctx"]

UNKNOWN = "返答の確認ができない（評価未取得）"


def _thread_reply(ctx, mid, parent, extraction):
    ctx.db.save_messages([mcs_contract.Message(
        message_id=mid, project_id=1, parent_id=parent, sender_id=2, sender_name="返信者", sender_type="user",
        profession="", organization="", posted_at="2026-09-19T00:00:00+09:00", body_html="<p>架空の返信</p>",
        body_state="full", is_unread=False, reply_count=0)], project_id=1)
    ctx.db.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?", (T0 + parent * DAY + 3600, mid))
    _artifact(ctx.db, "extract_llm", mid, extraction, {"hash": _hash_of(ctx.db, mid)})


def _assert_unknown_not_unanswered(ctx):
    item = next(r for r in pcv.replies(ctx.db.db, 1)["items"] if r["message_id"] == 3)
    assert item["reply_state"] is None and item["reply"] == UNKNOWN
    ev = pcv.evidence(ctx.db.db, 1)
    for view in (pcv.previsit(ctx.db.db, ev, T0 + 7 * DAY), pcv.handoff(ctx.db.db, ev)):
        assert 3 not in [r["message_id"] for r in view["unanswered"]]
        assert [r["message_id"] for r in view["reply_unknown"]] == [3]


def test_reply_without_relation_evaluation_is_not_unanswered(ctx):
    _requests(ctx, [(3, "架空依頼")])
    _thread_reply(ctx, 30, 3, {"reply": {}})   # the loop exists, the reply was never evaluated
    seed_loop_replies(ctx.db)
    _assert_unknown_not_unanswered(ctx)


def test_reply_to_a_request_without_loop_candidate_is_not_unanswered(ctx):
    _requests(ctx, [(3, "架空依頼")])
    _thread_reply(ctx, 30, 3, {"reply": {"kind": "done"}})   # no loop candidate was built
    _assert_unknown_not_unanswered(ctx)


def test_request_without_any_thread_reply_stays_unanswered(ctx):
    _requests(ctx, [(3, "架空依頼")])
    seed_loop_replies(ctx.db)
    note = pcv.previsit(ctx.db.db, pcv.evidence(ctx.db.db, 1), T0 + 7 * DAY)
    assert [r["message_id"] for r in note["unanswered"]] == [3] and note["reply_unknown"] == []


def _transition_ev(unanalysed):
    item = {"category": "course", "text": "本人は本日退院しました。", "quote": "本人は本日退院しました。",
            "subject": "patient", "details": [], "message_id": 6, "posted_at_ts": T0 + 6 * DAY, "source": "chat"}
    return {"items": [item], "unanalysed_posts": unanalysed}


def test_pending_post_in_window_leaves_absence_unconfirmed():
    group = pcv.transition_course(_transition_ev([T0 + 7 * DAY]))["items"][0]
    assert group["anchor_kind"] == "patient_current"
    assert group["no_record"] == [] and group["no_record_unverified"] is True


def test_pending_post_outside_window_keeps_the_observed_absence():
    group = pcv.transition_course(_transition_ev([T0 + 30 * DAY]))["items"][0]
    assert group["no_record"] == ["adl", "care_services", "followup", "medication_management"]
    assert group["no_record_unverified"] is False


def test_pending_extraction_reaches_the_rendered_heading(ctx):
    ctx.db.db.execute("DELETE FROM artifacts WHERE message_id=5")   # message 5 is now unanalysed
    body = "経過: 本人は本日退院しました。"
    ctx.db.save_messages([_message(7, body=body)], project_id=1)
    ctx.db.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=7", (T0 + 6 * DAY,))
    _artifact(ctx.db, "extract_v1", 7, {"patient_context": extract_context(body)}, {"hash": _hash_of(ctx.db, 7)})
    view = notify_views.patient_context_list(ctx.db.db, 1, "transition_course", cfg={}, since=T0, now=T0 + 30 * DAY)
    assert not any("の前後14日で記載なし: " in h for h in view["head"])
    assert any(h.endswith("の前後14日: 未解析の投稿があるため記載の有無は未確認") for h in view["head"])
