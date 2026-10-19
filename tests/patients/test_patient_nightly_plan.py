"""Synthetic independent invariants for nightly selection and source validation."""
import copy
import pytest
import patient_nightly_plan as plan


def fact(identity, category, text, stamp):
    return {"id": identity, "category": category, "text": text,
            "source": "chat", "project_id": 1, "message_id": 1,
            "posted_at": None, "posted_at_ts": stamp, "subject": "unspecified"}


def test_category_fairness_and_summary_source_identity(monkeypatch):
    values = [fact("a", "living", "家族は過去の居住を希望していません。", 3),
              fact("b", "living", "本人の転居予定は未確認。", 2),
              fact("c", "allergies", "アレルギーは未確認。", 1)]
    original = copy.deepcopy(values)
    monkeypatch.setattr(plan, "MAX_FACTS", 2)
    selected, total = plan.select_facts({f["id"]: f for f in values})
    assert [f["id"] for f in selected] == ["c", "a"]
    assert total == 3
    reports = plan.summary_reports(values + [values[0]])
    assert [r["id"] for r in reports] == ["a", "b", "c"]
    assert plan.summary_selection({"fact_ids": ["b", "a"]}, reports) == [
        {"text": "本人の転居予定は未確認。", "fact_ids": ["b"]},
        {"text": "家族は過去の居住を希望していません。", "fact_ids": ["a"]}]
    assert values == original


@pytest.mark.parametrize("answer", [{"fact_ids": []}, {"fact_ids": ["a", "a"]},
    {"fact_ids": ["unknown"]}, {"fact_ids": [1]}, {"fact_ids": ["a"], "text": "推論"}])
def test_summary_rejects_unbound_or_generated_content(answer):
    reports = plan.summary_reports([fact("a", "living", "原文は未確認。", 0)])
    with pytest.raises(ValueError, match="nightly_unsupported_summary"):
        plan.summary_selection(answer, reports)


def test_scan_cursor_and_complete_quoted_unit_do_not_mutate_inputs():
    item = {"category": "living", "text": "家族は転居を希望していません。",
            "evidence": "家族は転居を希望していません。", "subject": "family"}
    source = {"body_text": item["text"], "items": [item]}
    saved = {"cursor": True, "facts": [{"id": "obsolete"}]}
    original = copy.deepcopy((source, saved))
    chunks, cursor, reusable, items, covered = plan.scan_plan(source, saved)
    assert chunks == [item["text"]] and cursor == 0 and reusable == {}
    assert items == [item] and covered is True
    assert plan.scan_items({"patient_context": [item]}, chunks[0]) == [item]
    with pytest.raises(ValueError, match="nightly_unsupported_items"):
        plan.scan_items({"patient_context": [{**item, "text": "家族は転居を希望しています。"}]}, chunks[0])
    with pytest.raises(ValueError, match="nightly_invalid_items"):
        plan.scan_items({"patient_context": [{**item, "status": "current"}]}, chunks[0])
    assert (source, saved) == original


def test_lane_priority_and_rotation_keep_initial_then_oldest():
    assert plan.candidate_key({"initial": True}, {"last_attempt": 9}, 2, "A") < \
        plan.candidate_key({"initial": False}, {"last_attempt": 0}, 1, "A")
    assert plan.candidate_key({"initial": False}, {"last_attempt": 1}, 2, "B") < \
        plan.candidate_key({"initial": True}, {"last_attempt": 2}, 1, "B")
    lanes = [plan.lane_rotation({"A": 1, "B": 1, "C": 1}, {"cursor": i})[0] for i in range(10)]
    assert lanes == list("AAAAAABBBC")
    assert plan.lane_rotation({"C": 1}, {"cursor": 0}) == ("C", 0, 1)
    assert plan.lane_rotation({}, {"cursor": 9}) == (None, 9, 0)
