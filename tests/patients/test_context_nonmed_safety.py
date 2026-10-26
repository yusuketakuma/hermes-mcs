"""Synthetic source-bound checks for non-medication context projections."""

from datetime import datetime

import pytest

from patient_context import extract_context
import patient_context_refresh as refresh
import patient_context_state as state


def source(mid, body, *, day=1, content_hash=None):
    return {
        "project_id": 1,
        "message_id": mid,
        "content_hash": content_hash or f"synthetic-{mid}",
        "source": "chat",
        "body_state": "full",
        "body_text": body,
        "posted_at_ts": datetime.fromisoformat(f"2026-10-{day:02d}T10:00:00+09:00").timestamp(),
    }


def facts_for(row):
    return refresh._facts(row, extract_context(row["body_text"]), night_id=1, discovery="new_report")


def category_fact(row, category):
    matches = [fact for fact in facts_for(row) if fact["category"] == category]
    assert len(matches) == 1
    return matches[0]


def bucket(facts, fact):
    projection = state.project(facts)
    return next(name for name, ids in projection["groups"].items() if fact["id"] in ids)


@pytest.mark.parametrize("particle", ["は", "に"])
def test_family_allergy_does_not_become_patient_current(particle):
    body = f"アレルギー: 家族{particle}卵アレルギーが本日あります。"
    fact = category_fact(source(1, body), "allergies")
    assert fact["evidence"] == body
    assert fact["temporal"]["subject"] != "patient"
    assert bucket([fact], fact) != "current"


def test_explicit_patient_allergy_retains_person_and_negative_statement():
    body = "アレルギー: 本人に卵アレルギーは本日ありません。"
    fact = category_fact(source(1, body), "allergies")
    assert fact["evidence"] == body
    assert fact["temporal"]["subject"] == "patient"
    assert fact["temporal"].get("symptom_state") is None
    assert state._entity(fact) is None


def test_single_explicit_patient_current_report_remains_visible():
    body = "生活環境: 本人は本日独居しています。"
    fact = category_fact(source(1, body), "living")
    assert fact["temporal"]["subject"] == "patient"
    assert bucket([fact], fact) == "current"
    assert fact["evidence"] == body


def test_family_as_recipient_does_not_prove_who_has_allergy():
    body = "アレルギー: 家族に本人の卵アレルギーについて本日報告しました。"
    fact = category_fact(source(1, body), "allergies")
    assert fact["temporal"]["subject"] != "family"
    assert bucket([fact], fact) != "current"
    assert fact["evidence"] == body


def test_unknown_mention_category_is_rejected():
    with pytest.raises(ValueError, match="medications or symptoms"):
        state.clinical_state_fact(source(1, "合成"), {"text": "合成"}, "allergies")


def test_missing_allergy_record_is_not_absence_of_allergy():
    body = "アレルギー: 本人に卵アレルギーの記録は本日ありません。"
    fact = category_fact(source(1, body), "allergies")
    assert fact["evidence"] == body
    assert fact["temporal"].get("negated") is not True
    assert fact["temporal"].get("symptom_state") is None
    assert bucket([fact], fact) != "current"


def test_nonmedication_plan_is_not_a_current_living_state():
    body = "生活環境: 本人は本日、来週から家族と同居する予定です。"
    fact = category_fact(source(1, body), "living")
    assert fact["evidence"] == body
    assert bucket([fact], fact) != "current"


def test_two_living_reports_do_not_establish_current_state_without_relation():
    earlier = category_fact(source(1, "生活環境: 本人は本日独居しています。", day=1), "living")
    later = category_fact(source(2, "生活環境: 本人は本日家族と同居しています。", day=2), "living")
    projection = state.project([earlier, later])
    assert not ({earlier["id"], later["id"]} & set(projection["groups"]["current"]))
    assert earlier["evidence"].endswith("独居しています。")
    assert later["evidence"].endswith("家族と同居しています。")


def test_deleted_or_revised_source_cannot_validate_old_context_fact():
    row = source(1, "生活環境: 本人は本日独居しています。")
    fact = category_fact(row, "living")
    assert refresh._validated_facts(row, [fact])
    assert refresh._validated_facts({**row, "body_state": "deleted"}, [fact]) == []
    revised = {**row, "content_hash": "synthetic-revised", "body_text": "生活環境: 本人は同居しています。"}
    assert refresh._validated_facts(revised, [fact]) == []


def test_explicit_nonmedication_correction_preserves_both_sources():
    old_body = "生活環境: 本人は本日独居しています。"
    correction_body = f"生活環境: 投稿#1の「{old_body}」を訂正しました。"
    old = category_fact(source(1, old_body, day=1), "living")
    correction = category_fact(source(2, correction_body, day=2), "living")
    projection = state.project([old, correction])
    assert old["evidence"] == old_body and correction["evidence"] == correction_body
    assert old["id"] not in projection["groups"]["current"]
    assert correction["id"] not in projection["groups"]["current"]
    assert any(rel["type"] == "corrects" and rel["target_fact_id"] == old["id"]
               for rel in projection["relations"])
