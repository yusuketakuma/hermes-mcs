"""🔎 search and shared notification wording state what was searched and what is unknown (synthetic data only)."""
import json

import pytest

import attachment_ocr
import notify_actions
import notify_render
import notify_views
from mcs_contract import project_url
from mcs_queries import karte_summary_bound
from message_metadata import FLAG_LABELS, flag_lines
from notify_testkit import _msg, _patient, led as led  # noqa: F401


def _search(led, query="本文"):
    return notify_views.patient_search_view(led.db, 1, query)


def test_head_line_links_only_a_patient_room(led):
    _patient(led)
    _msg(led, 101)
    assert project_url(1) in _search(led)["head"][0]
    assert project_url(1) not in notify_views.patient_search_view(led.db, 1, "本文", room_link=False)["head"][0]
    led.db.execute("UPDATE patients SET project_type='group',group_id=9,group_name='合成グループ' WHERE project_id=1")
    assert "MCS:" not in _search(led)["head"][0] and project_url(1) not in str(_search(led))


def test_completed_search_without_hit_names_the_searched_range(led):
    _patient(led)
    _msg(led, 101)
    empty = _search(led, "合成の存在しない語")["empty"]
    assert empty == "調べた範囲の取得済み原資料には、一致する記載は見つかりませんでした。"
    assert "ありません" not in empty


@pytest.mark.parametrize("posted_at,label", [
    (None, "日時未取得"), ("", "日時未取得"), ("壊れ", "日時不明"), ("2026-09-24", "09-24 時刻不明"),
    ("2026-09-24T08:01", "09-24 08:01")])
def test_post_time_words_an_unknown_time(posted_at, label):
    assert notify_render.posted_time_label(posted_at) == label


def test_search_row_without_a_date_has_no_placeholder(led):
    _patient(led)
    _msg(led, 101)
    led.db.execute("UPDATE messages SET posted_at=NULL WHERE message_id=101")
    (item,) = _search(led)["items"]
    assert "?" not in item["text"] and "日時未取得" in item["text"]


@pytest.mark.parametrize("archived,reason", [(1, None), (0, "forbidden")])
def test_unsearchable_room_says_why_in_the_search_action(led, archived, reason):
    _patient(led, archived=archived)
    led.db.execute("UPDATE patients SET fetch_reason=? WHERE project_id=1", (reason,))
    _msg(led, 101)
    card = {"project_id": 1}
    answer = notify_actions._patient_search(led.db, {}, card, {"query": "本文"}, {}, {}, 0)
    assert answer["title"] == "原資料を検索していません"
    assert answer["body"] == notify_views.SEARCH_SCOPE_UNAVAILABLE
    opened = notify_actions._patient_search(led.db, {}, card, {}, {"query": "本文", "source_ref": {
        "key": 101, "binding": "a" * 64}}, {}, 0)
    assert opened["body"] == notify_views.SOURCE_SCOPE_UNAVAILABLE
    bad = notify_actions._patient_search(led.db, {}, card, {"query": "資料:投稿"}, {}, {}, 0)
    assert bad["title"] == "検索条件を確認してください"


def test_every_ocr_view_state_has_its_own_wording():
    assert set(notify_render.OCR_STATE_LABELS) == attachment_ocr.VIEW_STATES
    assert len(set(notify_render.OCR_STATE_LABELS.values())) == len(notify_render.OCR_STATE_LABELS)
    assert notify_render._ocr_label({"state": "synthetic_new_state"}) == "状態不明"


@pytest.mark.parametrize("meta,karte_id,bound", [
    ({"karte_id": 7}, 7, True), ({"karte_id": 7}, 8, False), ({"karte_id": 7}, None, False),
    ({}, None, False), ({"karte_id": True}, 1, False), (None, 7, False)])
def test_one_rule_binds_a_karte_summary(meta, karte_id, bound):
    assert karte_summary_bound(meta, karte_id) is bound


def test_summary_card_and_read_model_agree_on_an_unlinked_karte(led):
    import read_model
    _patient(led)
    led.artifact_add("karte_summary", json.dumps({"comment": "合成の連携メモ", "empty": False}),
                     project_id=1, meta={"karte_id": 7, "fetched_at": 1})
    # stored but not this patient's: the same "unknown" reading, never 未取得
    status = notify_views.karte_summary_status(led.db, 1)
    assert status["state"] == read_model._project_metadata(led.db, "detail", 1, None)["records"][0]["state"] == "unknown"
    assert status["line"] == "連携サマリー（MCS）: 未確認" and status["comment"] is None
    assert "■ 連携サマリー（MCS）: 未確認" in notify_views.patient_summary_text(led.db, 1)[1]
    led.db.execute("UPDATE patients SET karte_id=7 WHERE project_id=1")
    assert notify_views.karte_summary_status(led.db, 1)["state"] == "present"
    assert read_model._project_metadata(led.db, "detail", 1, None)["records"][0]["state"] == "reported"


def test_flag_lines_use_the_workspace_words(led):
    observed = {"mentions": [], "mentions_status": "observed", "is_pinned": True, "pin_status": "observed",
                "is_bookmarked": None, "bookmark_status": "not_fetched"}
    lines = flag_lines(led.db, observed, self_id=5)
    assert lines == ["@自分宛て: なし", "ピン留め: あり", "ブックマーク: 未取得"]
    assert [line.split(":")[0] for line in lines] == [label for _, label in FLAG_LABELS]
    failed = dict(observed, pin_status="failed", is_pinned=None, bookmark_status="invalid")
    assert flag_lines(led.db, failed, self_id=None) == [
        "@自分宛て: 自分のアカウントを特定できません", "ピン留め: 取得失敗", "ブックマーク: 読み取れません"]
    assert not any("未読" in line or "取得不正" in line for line in lines)


def test_search_returns_each_ranges_unsearched_counts(led, monkeypatch):
    import patient_search
    _patient(led)
    _msg(led, 101)
    original = patient_search.search

    def counted(*args, **kwargs):
        return {**original(*args, **kwargs), "pending": 2, "unavailable": 3, "unclassified": 1}
    monkeypatch.setattr(patient_search, "search", counted)
    assert _search(led)["skipped"] == {"ocr_pending": 2, "body_unavailable": 3, "unclassified": 1}


def test_search_counts_unsearched_sources_from_the_stored_posts(led):
    _patient(led)
    _msg(led, 101)
    _msg(led, 102)
    _msg(led, 103)
    led.db.execute("UPDATE messages SET body_state='snippet',body_text=NULL WHERE message_id IN (102,103)")
    led.db.commit()
    result = _search(led)
    assert result["skipped"] == {"ocr_pending": 0, "body_unavailable": 2, "unclassified": 0}
    assert any("本文未取得・確認不能の原資料が2件" in note for note in result["notes"])


def test_unreadable_settings_leave_the_ocr_side_unknown(led):
    _patient(led)
    _msg(led, 101)
    notes = notify_views.patient_search_view(led.db, 1, "本文", settings_unreadable=True)["notes"]
    assert notify_views.OCR_SEARCH_SETTINGS_UNREADABLE in notes
    assert not any("設定で無効" in note for note in notes)
