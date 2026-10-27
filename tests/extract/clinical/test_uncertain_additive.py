"""Synthetic checks that uncertain additive phrases cannot assert clinical state."""
from contextlib import closing
import json

import pytest

import clinical_scope
import extract_llm
import extract_validation
import inference_version
import rollup
from extract_testkit import _hash, _ledger, _message


def _medication(body, *, action="stop", status="current"):
    return {"name": "合成薬A", "dose": None, "action": action, "status": status,
            "subject": "patient", "negated": False, "evidence": body}


def _symptom(body, *, negated=False):
    return {"text": "発熱", "status": "ongoing", "subject": "patient",
            "negated": negated, "evidence": body}


@pytest.mark.parametrize("suffix", ["中止もしたいです", "中止もしなくてはならない", "中止もしましたか"])
def test_uncertain_additive_medication_cannot_confirm_stop(suffix):
    body = f"本人は合成薬Aを{suffix}。"
    start = body.index("合成薬A")
    assert clinical_scope.source_axes(body, start, start + len("合成薬A")) == ("patient", "unknown")
    [item] = extract_validation.validate({"meds": [_medication(body)]}, body)["meds"]
    assert item["evidence"] == body and item["name"] == "合成薬A"
    assert item.get("status") != "current" and item.get("unverified") is True


def test_uncertain_additive_symptom_is_not_a_confirmed_current_finding():
    body = "本人は発熱もしましたか。"
    start = body.index("発熱")
    assert clinical_scope.source_axes(body, start, start + len("発熱")) == ("patient", "unknown")
    [item] = extract_validation.validate({"symptoms": [_symptom(body)]}, body)["symptoms"]
    assert item["evidence"] == body and item["text"] == "発熱"
    assert item.get("status") != "ongoing" and item.get("unverified") is True


@pytest.mark.parametrize("body,negated", [
    ("本人は発熱もあります。", False),
    ("本人は発熱もありません。", True),
])
def test_explicit_additive_symptom_and_denial_remain_verified(body, negated):
    start = body.index("発熱")
    assert clinical_scope.source_axes(body, start, start + len("発熱")) == ("patient", "current")
    [item] = extract_validation.validate({"symptoms": [_symptom(body, negated=negated)]}, body)["symptoms"]
    assert item["evidence"] == body and item["negated"] is negated
    assert item["status"] == "ongoing" and not item.get("unverified")


def test_a_real_hypothetical_is_not_a_confirmed_current_symptom():
    body = "もし本人に発熱があれば連絡します。"
    start = body.index("発熱")
    assert clinical_scope.source_axes(body, start, start + len("発熱")) == ("patient", "conditional")
    [item] = extract_validation.validate({"symptoms": [_symptom(body)]}, body)["symptoms"]
    assert item["evidence"] == body and item.get("unverified") is True
    assert item.get("status") != "ongoing"


def test_uncertain_stop_does_not_create_a_confirmed_stop_in_the_rollup(tmp_path):
    with closing(_ledger(tmp_path)) as ledger:
        known = "本人は現在合成薬Aを服用中です。"
        uncertain = "本人は合成薬Aを中止もしたいです。"
        for mid, body, action, day in ((1, known, "none", "2026-10-01"),
                                       (2, uncertain, "stop", "2026-10-02")):
            ledger.save_messages([_message(mid=mid, body=body, posted_at=day + "T09:00:00+09:00")])
            document = extract_validation.validate({"meds": [_medication(body, action=action)]}, body)
            ledger.artifact_add("extract_llm", json.dumps(document, ensure_ascii=False), project_id=1,
                message_id=mid, meta={"hash": _hash(ledger, mid),
                    "extract_version": extract_llm.EXTRACT_VERSION,
                    inference_version.VERSION_KEY: inference_version.CURRENT_VERSION})
        view = rollup.build_rollup(ledger.db, 1)
        [later] = [fact for fact in view["clinical_state"]["facts"] if fact["message_id"] == 2]
        assert later["temporal"]["unknown"] is True
        assert later["attributes"]["unverified"] is True
        assert later["id"] not in view["clinical_state"]["groups"]["current"]
        assert ledger.db.execute("SELECT body_text FROM messages WHERE message_id=2").fetchone()[0] == uncertain
