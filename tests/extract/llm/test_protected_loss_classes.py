"""Synthetic regression tests for protected-item loss diagnostics."""
import json

import extract_llm
import extract_quality as quality
import inference_dependency_loss
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY


HOLD_BODY = "本人に息切れはありません。家族には軽い発熱があります。昨日は息切れがありました。"
HOLD_PRIOR = {"summary": "合成の報告", "symptoms": [
    {"text": "息切れ", "negated": True, "status": "ongoing", "subject": "patient", "evidence": "本人に息切れはありません。"},
    {"text": "発熱", "negated": False, "status": "ongoing", "subject": "family", "evidence": "家族には軽い発熱があります。"}]}
HOLD_CANDIDATE = {"summary": "合成の候補", "symptoms": [
    {"text": "息切れ", "negated": False, "status": "ongoing", "subject": "patient", "evidence": "昨日は息切れがありました。"}]}


def test_loss_classes_split_absent_conflict_and_unlocated():
    assert quality.protected_loss_classes(HOLD_PRIOR, HOLD_CANDIDATE, HOLD_BODY, pieces=[HOLD_BODY]) == {
        "symptoms": {"conflict": 1, "absent": 1}}
    split = [HOLD_BODY[:22], HOLD_BODY[22:]]
    assert quality.protected_loss_classes(HOLD_PRIOR, HOLD_CANDIDATE, HOLD_BODY, pieces=split) == {
        "symptoms": {"conflict": 1, "unlocated": 1}}
    assert quality.protected_loss_classes(HOLD_PRIOR, HOLD_PRIOR, HOLD_BODY) == {}


def test_hold_receipt_records_loss_classes_and_still_matches_the_hold(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(body=HOLD_BODY)])
        row = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        row["_source_binding"] = inference_dependency_loss.source_binding(db.db, row["project_id"], row["message_id"])
        db.artifact_add("extract_llm", json.dumps(HOLD_PRIOR, ensure_ascii=False), project_id=1, message_id=1,
                        meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(db), "source_binding": row["_source_binding"],
                              "extract_version": extract_llm.EXTRACT_VERSION - 1})
        assert extract_llm._replace_current(db, row, json.dumps(HOLD_CANDIDATE, ensure_ascii=False)) is False
        [artifact] = db.artifacts(extract_llm.UPGRADE_KIND)
        receipt = json.loads(artifact["content"])
        assert receipt["reason"] == "protected_loss" and receipt["lost"] == {"symptoms": 2}
        assert receipt["lost_class"] == {"symptoms": {"conflict": 1, "absent": 1}}
        assert sum(receipt["lost_class"]["symptoms"].values()) == receipt["lost"]["symptoms"]
        # The current-contract hold still matches, so the source is not re-selected.
        assert not db.db.execute("SELECT 1 FROM messages m WHERE " + extract_llm.pending_pred(db.db)).fetchone()
    finally:
        db.close()
