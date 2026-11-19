"""The 🧾 summary reads only the patient's current karte, and a category search counts
text matches it could not classify instead of calling them non-matches. Synthetic ledger."""
import notify_views
from notify_testkit import _msg, _patient, led

__all__ = ["led"]

SUMMARY = {"comment": "既往歴：合成の旧カルテ記載。", "updated_at": "2026-09-30T10:00:00+09:00",
           "user": {"profession": "看護師", "name": "SYNTH"}, "is_editable": True}


def test_a_summary_of_the_previous_karte_is_not_shown_for_the_relinked_patient(led):
    _patient(led, 1)
    led.db.execute("UPDATE patients SET karte_id=100 WHERE project_id=1")
    led.karte_summary_store(1, 100, SUMMARY)
    body = notify_views.patient_summary_text(led.db, 1)[1]
    assert "合成の旧カルテ記載" in body  # control: the same karte is shown
    led.db.execute("UPDATE patients SET karte_id=200 WHERE project_id=1")
    body = notify_views.patient_summary_text(led.db, 1)[1]
    assert "合成の旧カルテ記載" not in body
    assert "連携サマリー（MCS）: 未取得" in body


def test_a_category_search_counts_unanalysed_text_matches(led):
    _patient(led, 1)
    _msg(led, 101, body="残薬が合成の量あります")
    view = notify_views.patient_search_view(led.db, 1, "残薬 分類:服薬管理")
    assert view["items"] == []
    assert "解析未完了のため分類を判定できない原資料が1件あります。" in view["notes"]
    assert view["empty"] == "解析が完了した原資料には、この分類で一致するものはありません。"


def test_a_search_without_category_needs_no_classification(led):
    _patient(led, 1)
    _msg(led, 101, body="残薬が合成の量あります")
    view = notify_views.patient_search_view(led.db, 1, "残薬")
    assert len(view["items"]) == 1
    assert not any("分類を判定できない" in note for note in view["notes"])


def test_an_unscanned_range_keeps_its_scope_wording_with_unclassified_matches(led, monkeypatch):
    _patient(led, 1)
    _msg(led, 101, body="残薬が合成の量あります")
    import patient_search
    real = patient_search.search

    def limited(*args, **kwargs):
        result = real(*args, **kwargs)
        return {**result, "next_cursor": result.get("next_cursor") or "synthetic-cursor",
                "range_complete": False}

    monkeypatch.setattr(patient_search, "search", limited)
    view = notify_views.patient_search_view(led.db, 1, "残薬 分類:服薬管理")
    assert view["empty"] == "この範囲の解析が完了した原資料には、この分類で一致するものはありません。"
