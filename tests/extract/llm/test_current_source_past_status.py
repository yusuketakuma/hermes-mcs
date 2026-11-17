"""完全合成の原文で、明示的な現在に反する過去の状態を検証済みにしないことを確かめる（#154）。"""
import pytest
import extract_validation
import structured_view


def _symptom(body, status, *, subject="patient", negated=False):
    doc = {"symptoms": [{"text": "発熱", "status": status, "subject": subject,
                         "negated": negated, "evidence": body}]}
    return extract_validation.validate(doc, body)


def test_explicit_current_source_rejects_past_symptom_status():
    body = "本人は現在、発熱あり。"
    item = _symptom(body, "past")["symptoms"][0]
    assert "status" not in item and item["unverified"] is True
    assert (item["subject"], item["negated"], item["evidence"]) == ("patient", False, body)


def test_explicit_current_source_keeps_ongoing_symptom_shown():
    body = "本人は現在、発熱あり。"
    clean = _symptom(body, "ongoing")
    assert clean["symptoms"][0]["status"] == "ongoing"
    assert "unverified" not in clean["symptoms"][0]
    line = structured_view._symptom_line(clean, {"symptoms": ["発熱"]}, context=(body, None, "patient"))
    assert line == "症状: 発熱"


@pytest.mark.parametrize("body", ["本人は昨日、発熱あり。", "本人は昨日発熱あり、現在は解熱。"])
def test_genuine_past_symptom_stays_past_and_not_current(body):
    clean = _symptom(body, "past")
    assert clean["symptoms"][0]["status"] == "past"
    assert "unverified" not in clean["symptoms"][0]
    assert structured_view._symptom_line(clean, {"symptoms": ["発熱"]}, context=(body, None, "patient")) is None


@pytest.mark.parametrize("body,negated", [
    ("発熱は現在消失しています。", False), ("発熱は今は治まっています。", False), ("本人は現在、発熱なし。", True)])
def test_current_resolved_or_negated_symptom_keeps_past(body, negated):
    item = _symptom(body, "past", negated=negated)["symptoms"][0]
    assert item["status"] == "past" and "unverified" not in item
    assert item["negated"] is negated


@pytest.mark.parametrize("body", ["本人は現在、ロキソニンを中止しています。", "本日よりロキソニン中止。"])
def test_current_medication_stop_keeps_past(body):
    doc = {"meds": [{"name": "ロキソニン", "status": "past", "subject": "patient",
                     "negated": False, "evidence": body}]}
    med = extract_validation.validate(doc, body)["meds"][0]
    assert med["status"] == "past" and "unverified" not in med


def test_other_person_current_source_keeps_subject():
    body = "家族は現在、発熱あり。"
    item = _symptom(body, "past", subject="family")["symptoms"][0]
    assert item["subject"] == "family" and "status" not in item and item["unverified"] is True


def test_conditional_source_keeps_existing_uncertainty():
    item = _symptom("明日、発熱があれば連絡。", "past")["symptoms"][0]
    assert "status" not in item and item["unverified"] is True
