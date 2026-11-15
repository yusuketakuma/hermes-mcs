"""Synthetic independent invariants for summary-side context selection."""
import copy
import pytest
import patient_context_selection as selection


def fact(identity, category, text, stamp):
    return {"id": identity, "category": category, "text": text,
            "source": "chat", "project_id": 1, "message_id": 1,
            "posted_at": None, "posted_at_ts": stamp, "subject": "unspecified"}


def test_category_fairness_and_summary_source_identity(monkeypatch):
    values = [fact("a", "living", "家族は過去の居住を希望していません。", 3),
              fact("b", "living", "本人の転居予定は未確認。", 2),
              fact("c", "allergies", "アレルギーは未確認。", 1)]
    original = copy.deepcopy(values)
    monkeypatch.setattr(selection, "MAX_FACTS", 2)
    selected, total = selection.select_facts({f["id"]: f for f in values})
    assert [f["id"] for f in selected] == ["c", "a"]
    assert total == 3
    reports = selection.summary_reports(values + [values[0]])
    assert [r["id"] for r in reports] == ["a", "b", "c"]
    assert selection.summary_selection({"fact_ids": ["b", "a"]}, reports) == [
        {"text": "本人の転居予定は未確認。", "fact_ids": ["b"]},
        {"text": "家族は過去の居住を希望していません。", "fact_ids": ["a"]}]
    assert values == original


@pytest.mark.parametrize("answer", [{"fact_ids": []}, {"fact_ids": ["a", "a"]},
    {"fact_ids": ["unknown"]}, {"fact_ids": [1]}, {"fact_ids": ["a"], "text": "推論"}])
def test_summary_rejects_unbound_or_generated_content(answer):
    reports = selection.summary_reports([fact("a", "living", "原文は未確認。", 0)])
    with pytest.raises(ValueError, match="patient_context_unsupported_summary"):
        selection.summary_selection(answer, reports)
