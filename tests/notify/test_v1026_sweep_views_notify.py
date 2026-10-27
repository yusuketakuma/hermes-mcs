"""v1.0.26 sweep: notify views / digest wording regressions (all synthetic)."""
import re
from datetime import datetime

import notify_digest
import notify_views
import patient_context_view as pcv
from mcs_contract import project_url
from notify_testkit import NOW as CARD_NOW, _delivered_card, led, pinned_clock  # noqa: F401
from notify_views import CONTEXT_PAGE_SIZE, patient_context_list
from patient_context import extract_context
from mcs_queries import JST
from semantic_testkit import _message
from test_patient_context_v3_views import DAY, T0, ctx  # noqa: F401 - shared synthetic fixture
from test_read_model import _artifact, _hash_of

__all__ = ["ctx", "led", "pinned_clock"]
NOW = T0 + 30 * DAY


def _view(ctx, section, **kw):
    return patient_context_list(ctx.db.db, 1, section, cfg={}, since=T0, now=NOW, **kw)


def test_unacked_head_ignores_archived_rooms_whose_stamps_are_never_fetched(led):
    _delivered_card(led)
    led.db.execute("UPDATE patients SET is_archived=1")
    led.db.commit()
    view = notify_views.unacked_view(led.db, "discord", now=CARD_NOW)
    assert view["items"] and "MCSで本人反応: 未取得あり" not in view["head"]


def test_meds_head_does_not_say_unavailable_while_items_show_candidates(led, monkeypatch):
    import structured_view
    led.db.execute("INSERT INTO patients(project_id,patient_name) VALUES(1,'合成')")
    led.db.execute("INSERT INTO messages(message_id,project_id,posted_at,profession,body_text,content_hash,"
                   "body_state) VALUES(100,1,'2026-10-01T09:00:00+09:00','薬剤師','本文','h','full')")
    ref = {"status": "ambiguous", "cands": [{}, {}], "dict_id": "fictional", "dict_sha256": "0" * 64}
    monkeypatch.setattr(structured_view, "medication_entries", lambda db, mid: ([("合成薬", ref)], []))
    view = notify_views.meds_view(led.db, 1, 100)
    assert "辞書の候補はすべて未確認です" in view["head"]
    assert not set(notify_views._DICTIONARY_STATE.values()) & set(view["head"])


def test_context_title_names_the_project_when_the_patient_has_no_name(ctx):
    ctx.db.db.execute("UPDATE patients SET patient_name=NULL WHERE project_id=1")
    assert _view(ctx, "timeline")["title"].startswith("project 1 — ")


def test_scope_accepts_full_width_colon_digits_and_comma():
    flt = notify_digest.parse_scope("施設：さくら 日数:３ 患者：1，2")
    assert isinstance(flt, dict)
    assert flt["stations"] == ["さくら"] and flt["days"] == 3 and flt["projects"] == [1, 2]
    assert notify_digest.parse_scope("施設:Ａ：Ｂ")["stations"] == ["Ａ：Ｂ"]   # the name stays as typed


def test_request_rows_use_the_short_linked_date(ctx, monkeypatch):
    monkeypatch.setattr(pcv, "replies", lambda db, pid: {"items": [
        {"message_id": 9, "at": "2026-10-01T09:00:00+09:00", "text": "合成依頼", "sender": "看護師 合成",
         "reply": "返答が見つからない"}]})
    row = _view(ctx, "replies")["items"][0]
    assert row["source"].startswith("10/01 ") and "T09" not in row["source"]
    assert row["url"] == project_url(1)


def test_previsit_marks_corrected_reports_and_does_not_repeat_the_heading(ctx, monkeypatch):
    item = {"message_id": 2, "quote": "残薬: 5日分", "posted_at_ts": T0 + 2 * DAY}
    monkeypatch.setattr(pcv, "previsit", lambda db, ev, now: {
        "recent": {"edited": [item], "deleted": []}, "written_checks": [],
        "unanswered": [{"message_id": 7, "text": "合成依頼", "reply": "返答が見つからない", "posted_at_ts": T0}],
        "interventions": {"project_id":1,"records":[],"coverage":{"state":"unknown"}}})
    rows = _view(ctx, "previsit")["items"]
    assert [r["group"] for r in rows] == ["訂正された報告", "返答が見つからない記録"]
    assert "返答が見つからない" not in rows[1]["text"]


def test_discrepancy_heading_names_the_attribute_and_drug(ctx):
    groups = {r["group"] for r in _view(ctx, "discrepancies")["items"]}
    assert any(re.fullmatch(r".+ — 残薬（架空薬）", g) for g in groups), groups


def test_background_keeps_an_informative_sub_heading(ctx):
    quotes = [q for r in _view(ctx, "background")["items"] for q in r["quotes"]]
    assert any(q.startswith("残薬") for q in quotes), quotes


def test_transition_windows_name_their_anchor_date(ctx):
    # A nominal discharge report is retained, but cannot establish execution.
    reported = _view(ctx, "transition_course")
    assert {r["group"] for r in reported["items"]} == {"移行に関する記載"}
    body = "経過: 本人は本日退院しました。"
    ctx.db.save_messages([_message(6, body=body)], project_id=1)
    anchor_at = T0 + 6 * DAY
    ctx.db.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=6", (anchor_at,))
    _artifact(ctx.db, "extract_v1", 6, {"patient_context": extract_context(body)},
              {"hash": _hash_of(ctx.db, 6)})
    view = _view(ctx, "transition_course")
    groups = [r["group"] for r in view["items"]]
    assert any(re.fullmatch(r"\d\d/\d\dの前後14日の記録", g) for g in groups), groups
    day = datetime.fromtimestamp(anchor_at, JST).strftime("%m/%d")
    assert f"{day}の前後14日の記録" in groups, groups
    assert "本人の移行実施の記録" in groups
    assert any(h.startswith(f"{day}の前後14日で記載なし: ") for h in view["head"])
    assert not any(h.startswith("この範囲で記載なし") for h in view["head"])


def test_changes_head_uses_the_short_date_with_time(ctx):
    head = _view(ctx, "changes")["head"]
    assert any(re.fullmatch(r"前回の表示依頼: \d\d/\d\d \d\d:\d\d", h) for h in head), head


def test_paged_views_give_the_range_and_mark_a_continued_group(ctx):
    extra = {mid: f"病名: 架空病{mid}" for mid in range(20, 32)}
    ctx.db.save_messages([_message(mid, body=body) for mid, body in extra.items()], project_id=1)
    for mid in extra:
        body = ctx.db.db.execute("SELECT body_text FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
        _artifact(ctx.db, "extract_v1", mid, {"patient_context": extract_context(body)}, {"hash": _hash_of(ctx.db, mid)})
    first, second = _view(ctx, "background"), _view(ctx, "background", page=1)
    assert any(re.fullmatch(r"全\d+件中 1〜8件目", h) for h in first["head"])
    assert any(re.fullmatch(rf"全\d+件中 {CONTEXT_PAGE_SIZE + 1}〜\d+件目", h) for h in second["head"])
    if first["items"][-1]["group"] == second["items"][0]["group"].removesuffix("（続き）"):
        assert second["items"][0]["group"].endswith("（続き）")
    assert not any(r["group"].endswith("（続き）") for r in first["items"])
