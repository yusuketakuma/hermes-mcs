"""Synthetic source-bound checks for non-medication context projections."""
import patient_context_plan as context_plan

from datetime import datetime

import pytest

from patient_context import extract_context
import patient_context_refresh as refresh
import patient_context_projection as state


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
    return context_plan.source_facts(row, extract_context(row["body_text"]), night_id=1, discovery="new_report", found_at=refresh.time.time())


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


@pytest.mark.parametrize("body", [
    "生活環境: 本人は本日、医師に生活状況を報告しました。",
    "生活環境: 看護師の介助で本人は本日生活しています。",
    "生活環境: 本人は家族の協力で本日生活しています。",
])
def test_patient_with_support_or_report_recipient_remains_patient(body):
    fact = category_fact(source(1, body), "living")
    assert fact["temporal"]["subject"] == "patient"
    assert bucket([fact], fact) == "current"


@pytest.mark.parametrize("body", [
    "生活環境: 本人は本日、医師に家族の生活状況を報告しました。",
    "生活環境: 本人は家族の協力で生活していますが、家族は本日独居しています。",
])
def test_other_persons_state_is_not_made_patient_state_by_support_or_reporting(body):
    fact = category_fact(source(1, body), "living")
    assert bucket([fact], fact) != "current"


def test_family_as_recipient_does_not_prove_who_has_allergy():
    body = "アレルギー: 家族に本人の卵アレルギーについて本日報告しました。"
    fact = category_fact(source(1, body), "allergies")
    assert fact["temporal"]["subject"] != "family"
    assert bucket([fact], fact) != "current"
    assert fact["evidence"] == body


@pytest.mark.parametrize("recipient", ["家族", "医師", "看護師"])
def test_report_recipient_does_not_imply_whose_allergy_was_reported(recipient):
    ambiguous = f"アレルギー: 本人は{recipient}に卵アレルギーがあると本日報告しました。"
    explicit = f"アレルギー: 本人は{recipient}に本人の卵アレルギーがあると本日報告しました。"
    unclear = category_fact(source(1, ambiguous), "allergies")
    patient = category_fact(source(2, explicit), "allergies")
    assert bucket([unclear], unclear) != "current"
    assert patient["temporal"]["subject"] == "patient"
    assert bucket([patient], patient) == "current"


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


def test_distinct_source_bound_allergies_do_not_hide_each_other():
    egg = category_fact(source(1, "アレルギー: 本人に卵アレルギーが本日あります。"), "allergies")
    wheat = category_fact(source(2, "アレルギー: 本人に小麦アレルギーが本日あります。"), "allergies")
    projection = state.project([egg, wheat])
    assert {egg["id"], wheat["id"]} <= set(projection["groups"]["current"])


def test_unknown_other_allergy_does_not_hide_a_bound_current_allergy():
    egg = category_fact(source(1, "アレルギー: 本人に卵アレルギーが本日あります。"), "allergies")
    wheat = category_fact(source(2, "アレルギー: 本人に小麦アレルギーがあるか本日不明です。"), "allergies")
    projection = state.project([egg, wheat])
    assert egg["id"] in projection["groups"]["current"]
    assert wheat["id"] in projection["groups"]["unknown"]


def test_same_allergy_with_unrelated_reports_is_not_resolved_by_posting_order():
    first = category_fact(source(1, "アレルギー: 本人に卵アレルギーが本日あります。", day=1), "allergies")
    second = category_fact(source(2, "アレルギー: 本人に卵アレルギーは本日ありません。", day=2), "allergies")
    projection = state.project([first, second])
    assert {first["id"], second["id"]} <= set(projection["groups"]["unknown"])


@pytest.mark.parametrize("general", [
    "本人にアレルギーは本日ありません。",
    "本人のアレルギーの有無は本日未確認です。",
    "本人に食物アレルギーは本日ありません。",
    "アレルギーは本日不明です。",
    "本人のアレルギー情報を本日訂正しました。",
    "本人には本日ありません。",
])
@pytest.mark.parametrize("bound_detail", [False, True])
def test_general_allergy_denial_uncertainty_or_correction_blocks_specific_current(general, bound_detail):
    egg = category_fact(source(1, "アレルギー: 本人に卵アレルギーが本日あります。"), "allergies")
    if bound_detail:
        egg["details"] = [{"key": "symptom", "value": "卵", "evidence": egg["evidence"]}]
        assert state._entity(egg) is not None
    broad = category_fact(source(2, "アレルギー: " + general), "allergies")
    projection = state.project([egg, broad])
    assert egg["id"] in projection["groups"]["unknown"]
    assert broad["id"] not in projection["groups"]["current"]


@pytest.mark.parametrize("other", [
    "家族はアレルギーが本日ありません。",
    "家族にアレルギーは本日不明です。",
    "家族には本日ありません。",
])
def test_other_persons_general_allergy_does_not_hide_patient_allergy(other):
    egg = category_fact(source(1, "アレルギー: 本人に卵アレルギーが本日あります。"), "allergies")
    broad = category_fact(source(2, "アレルギー: " + other), "allergies")
    projection = state.project([egg, broad])
    assert egg["id"] in projection["groups"]["current"]
    assert broad["id"] not in projection["groups"]["current"]


@pytest.mark.parametrize("text", [
    "本人のアレルギーの詳細は現時点で確認できていません。",
    "本人の食物アレルギーの詳しい情報は本日不明です。",
    "本人のアレルギーの記載は本日誤記でした。",
])
@pytest.mark.parametrize("bound_detail", [False, True])
def test_unspecified_allergy_uncertainty_blocks_concrete_target(text, bound_detail):
    egg = category_fact(source(1, "アレルギー: 本人に卵アレルギーが本日あります。"), "allergies")
    if bound_detail:
        egg["details"] = [{"key": "symptom", "value": "卵", "evidence": egg["evidence"]}]
    uncertain = category_fact(source(2, "アレルギー: " + text), "allergies")
    assert egg["id"] in state.project([egg, uncertain])["groups"]["unknown"]


@pytest.mark.parametrize("text", [
    "本人にアレルギーが本日あります。",
    "本人にアレルギーが本日ありますが、発疹はありません。",
])
def test_unspecified_affirmative_allergy_does_not_hide_specific_allergy(text):
    egg = category_fact(source(1, "アレルギー: 本人に卵アレルギーが本日あります。"), "allergies")
    general = category_fact(source(2, "アレルギー: " + text), "allergies")
    assert {egg["id"], general["id"]} <= set(state.project([egg, general])["groups"]["current"])


def test_clearly_different_allergy_dates_do_not_interfere():
    egg = category_fact(source(1, "アレルギー: 本人に卵アレルギーが本日あります。", day=2), "allergies")
    general = category_fact(source(2, "アレルギー: 本人にアレルギーは本日ありません。", day=1), "allergies")
    assert egg["id"] in state.project([egg, general])["groups"]["current"]


def test_other_project_or_category_does_not_interfere_with_allergy():
    egg = category_fact(source(1, "アレルギー: 本人に卵アレルギーが本日あります。"), "allergies")
    general = category_fact(source(2, "アレルギー: 本人にアレルギーは本日ありません。"), "allergies")
    general["project_id"] = 2
    living = category_fact(source(3, "生活環境: 本人の生活状況は本日未確認です。"), "living")
    assert egg["id"] in state.project([egg, general, living])["groups"]["current"]


def test_deleted_or_revised_source_cannot_validate_old_context_fact():
    row = source(1, "生活環境: 本人は本日独居しています。")
    fact = category_fact(row, "living")
    assert context_plan.validated_source_facts(row, [fact])
    assert context_plan.validated_source_facts({**row, "body_state": "deleted"}, [fact]) == []
    revised = {**row, "content_hash": "synthetic-revised", "body_text": "生活環境: 本人は同居しています。"}
    assert context_plan.validated_source_facts(revised, [fact]) == []


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
