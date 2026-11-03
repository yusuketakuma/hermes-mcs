"""Synthetic medication amounts, people and plans retain their source meaning."""
import pytest

import clinical_values
import extract_quality
import extract_validation


def medication(body, **changes):
    return {"name": "架空薬A", "dose": None, "action": "none", "status": "current",
            "subject": "patient", "negated": False, "evidence": body, **changes}


@pytest.mark.parametrize("separator", ["。", "、", ","])
@pytest.mark.parametrize("denial", ["服用量は記載されていません", "用量は未記載です", "1回量は不明です"])
def test_product_strength_is_not_an_unrecorded_dose(separator, denial):
    body = f"本人の架空薬A5mgは継続です{separator}{denial}。"
    drops = {}
    result = extract_validation.validate({"meds": [medication(body, dose="5mg")]}, body, drops=drops)
    item = result["meds"][0]
    assert item["dose"] is None and item["unverified"] is True
    assert any("dose" in reason and "規格" in reason for reason in drops["reasons"])
    assert extract_quality.repair_issues(drops, result)


@pytest.mark.parametrize("tail", ["服用量は不明ではありません", "服用量は記載されていないわけではありません",
                                  "別の薬の服用量は不明です"])
def test_unknown_dose_guard_does_not_cross_a_different_claim(tail):
    body = f"本人は架空薬Aを5mg服用しています。{tail}。"
    result = extract_validation.validate({"meds": [medication(body, dose="5mg")]}, body)
    assert result["meds"][0]["dose"] == "5mg"


@pytest.mark.parametrize("prefix", ["1回", "一回"])
def test_complete_explicit_dose_and_frequency_survive_clause_separation(prefix):
    body = f"本人は架空薬Aを{prefix}2錠、1日2回服用しています。"
    result = extract_validation.validate({"meds": [medication(body, dose="2錠", freq="1日2回")]}, body)
    item = result["meds"][0]
    assert item["dose"] == "2錠" and item["freq"] == "1日2回"
    assert not item.get("unverified")


def test_missing_explicit_frequency_is_repairable_without_inventing_it():
    body = "本人は架空薬Aを1回2錠、1日2回服用しています。"
    drops = {}
    result = extract_validation.validate({"meds": [medication(body, dose="2錠")]}, body, drops=drops)
    assert result["meds"][0].get("freq") is None
    assert result["meds"][0]["unverified"] is True
    assert any(".freq" in issue for issue in extract_quality.repair_issues(drops, result))


def test_missing_family_identity_is_requested_not_filled_with_patient():
    body = "家族自身は架空薬Aを服用しています。本人は服用していません。"
    item = medication(body)
    del item["subject"]
    drops = {}
    result = extract_validation.validate({"meds": [item]}, body, drops=drops)
    assert result["meds"][0].get("subject") is None
    assert result["meds"][0]["unverified"] is True
    assert any(".subject" in issue for issue in extract_quality.repair_issues(drops, result))


def test_correct_family_identity_does_not_become_patient_current_medication():
    body = "家族自身は架空薬Aを服用しています。本人は服用していません。"
    result = extract_validation.validate({"meds": [medication(body, subject="family")]}, body)
    item = result["meds"][0]
    assert item["subject"] == "family"
    assert clinical_values.patient_item_scope(item, body, surface=item["name"]) == "family"


@pytest.mark.parametrize("subject", ["patient", "family"])
def test_planned_dose_change_remains_planned(subject):
    actor = "本人" if subject == "patient" else "家族"
    body = f"{actor}の架空薬Aは来週から減量する予定です。"
    result = extract_validation.validate({"meds": [medication(body, subject=subject,
                                              action="decrease", status="planned")]}, body)
    assert result["meds"][0]["status"] == "planned"


def test_explicitly_unknown_dose_has_no_invented_amount():
    body = "本人の架空薬A5mgは継続です。服用量は記載されていません。"
    result = extract_validation.validate({"meds": [medication(body)]}, body)
    assert result["meds"][0]["dose"] is None


@pytest.mark.parametrize("other", ["別の薬", "架空薬B", "別架空薬A"])
def test_other_drug_unknown_amount_does_not_erase_this_drug_dose(other):
    body = f"本人は架空薬Aを5mg服用していますが{other}の服用量は不明です。"
    result = extract_validation.validate({"meds": [medication(body, dose="5mg")]}, body)
    assert result["meds"][0]["dose"] == "5mg"


def test_own_drug_explicit_unknown_amount_is_not_a_strength_dose():
    body = "本人の架空薬A5mgの服用量は不明です。"
    result = extract_validation.validate({"meds": [medication(body, dose="5mg")]}, body)
    assert result["meds"][0]["dose"] is None


@pytest.mark.parametrize("actor,expected", [("本人自身", "patient"), ("家族自身", "family"),
                                             ("家族本人自身", "family"), ("他者自身", "other")])
def test_explicit_self_suffix_does_not_switch_the_drug_owner(actor, expected):
    body = f"{actor}は架空薬Aを服用しています。"
    result = extract_validation.validate({"meds": [medication(body, subject=expected)]}, body)
    assert result["meds"][0]["subject"] == expected
