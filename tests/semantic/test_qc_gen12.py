"""extract_qc for generation 12: allergy claims are audited and the urgency question is independent (synthetic)."""
import json
import time

import pytest

import processing_route
import semantic_drain
import semantic_qc
from extract_testkit import _hash, _ledger, _message, _v2_artifact
from semantic_testkit import _FakeJev

_ALLERGIES = [{"text": "合成薬Aで発疹", "evidence": "ご本人は合成薬Aで発疹が出たことがあります。"},
              {"text": "合成薬Bで蕁麻疹", "evidence": "ご本人は合成薬Bで蕁麻疹が出ました。"}]


def _cfg():
    return {"semantic": {"mode": "shadow", "summary_mode": "shadow", "loop_mode": "off",
                         "threshold_mode": "calibrated", "calibration_version": "synthetic-test-v1",
                         "daily_request_budget": 50, "project_ids": None, "extract_qc": "annotate"}}


def _recent():
    return time.strftime("%Y-%m-%dT%H:%M:%S+09:00", time.localtime(time.time() - 86400))


def test_every_allergy_claim_gets_a_support_question():
    questions, layout, context = semantic_qc._qc_questions({"allergies": _ALLERGIES, "urgency": "routine"})
    allergy = [(qid, index) for qid, section, index in layout if section == "allergies"]
    assert [index for _, index in allergy] == [0, 1]
    for qid, index in allergy:
        assert questions[qid]["type"] == "noul"
        assert json.loads(context[qid]) == _ALLERGIES[index]
    assert len(set(questions)) == len(layout)


def test_allergy_claims_share_the_bounded_round_robin():
    ex = {"vitals": {"hr": 90, "sbp": 120}, "meds": [{"name": f"薬{i}"} for i in range(10)],
          "symptoms": [{"text": f"症状{i}"} for i in range(10)], "labs": [{"name": "採血"}],
          "events": ["visit"], "allergies": [{"text": f"合成薬{i}"} for i in range(10)]}
    _, layout, _ = semantic_qc._qc_questions(ex)
    sections = [section for _, section, _ in layout if section != "urgency"]
    assert len(sections) == semantic_qc.QC_MAX_ITEMS
    assert sections[:6] == ["vitals", "meds", "symptoms", "labs", "events", "allergies"]
    assert sections.count("allergies") == 4


@pytest.mark.parametrize("value", ["high", "routine", "unclear"])
def test_urgency_question_does_not_reveal_the_extracted_class(value):
    questions, layout, _ = semantic_qc._qc_questions({"urgency": value})
    assert ("urg", "urgency", -1) in layout
    reference = semantic_qc._qc_questions({"urgency": "high"})[0]["urg"]
    assert questions["urg"] == reference
    assert "抽出側" not in questions["urg"]["instructions"]
    assert value not in questions["urg"]["instructions"]


def test_qc_result_compares_and_covers_allergies_for_the_processing_route(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(posted_at=_recent())])
        content = {"meds": [{"name": "プレドニン", "action": "stop", "subject": "patient"}],
                   "allergies": _ALLERGIES, "urgency": "routine"}
        source = _v2_artifact(db, 1, _hash(db), content=content)
        out = semantic_drain.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 60,
                                     jev_client=_FakeJev(noul=0.9, choice="high"))
        assert out["done"] == 1
        row = db.db.execute("SELECT content,meta FROM artifacts WHERE kind='extract_qc' AND message_id=1").fetchone()
        qc = json.loads(row["content"])
        # the answer is still compared with the extracted class
        assert qc["urgency"] == {"extracted": "routine", "jev": "high", "confidence": 0.9}
        checked = [item for item in qc["items"] if item["section"] == "allergies"]
        assert [item["index"] for item in checked] == [0, 1]
        assert qc["coverage"]["by_field"]["allergies"] == {"checked": 2, "total": 2, "unchecked": 0}
        _, layout, _ = semantic_qc._qc_questions(content)
        assert qc["coverage"]["checked"] == len(layout) == 4
        meta = json.loads(row["meta"])
        assert processing_route._extract_jev_state(
            db.db, 1, 1, source, _hash(db), meta["extract_version"], content) == "done"
    finally:
        db.close()


@pytest.mark.parametrize("allergies, capped", [(6, False), (7, True)])
def test_allergies_count_toward_the_capped_coverage(tmp_path, allergies, capped):
    """10 medications plus 6 allergies fill the 16 slots exactly; one more allergy caps the round."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(posted_at=_recent())])
        content = {"meds": [{"name": f"合成薬M{i}", "action": "start", "subject": "patient"} for i in range(10)],
                   "allergies": [{"text": f"合成薬A{i}で発疹", "evidence": f"合成薬A{i}で発疹"}
                                 for i in range(allergies)]}
        source = _v2_artifact(db, 1, _hash(db), content=content)
        out = semantic_drain.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 60,
                                     jev_client=_FakeJev(noul=0.9))
        assert out["done"] == 1
        row = db.db.execute("SELECT content,meta FROM artifacts WHERE kind='extract_qc' AND message_id=1").fetchone()
        coverage = json.loads(row["content"])["coverage"]
        assert coverage["capped"] is capped
        assert coverage["by_field"]["allergies"]["total"] == allergies
        assert coverage["checked"] == semantic_qc.QC_MAX_ITEMS
        assert coverage["unchecked"] == (1 if capped else 0)
        assert processing_route._extract_jev_state(
            db.db, 1, 1, source, _hash(db), json.loads(row["meta"])["extract_version"], content) == "done"
    finally:
        db.close()
