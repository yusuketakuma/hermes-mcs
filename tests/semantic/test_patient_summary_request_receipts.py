"""Synthetic request bindings protect unknown Jev results across later attempts."""

import json
import time

import pytest

import inference_version
import maintenance
import semantic_jev
import semantic_runtime as runtime
from semantic_testkit import _ledger


IDENTITY = {"input_key": "1" * 64, "candidate_key": "2" * 64,
            "failure_key": "3" * 64, "audit_key": "4" * 64}
FACT0, FACT1 = "a" * 64, "b" * 64
ANSWER = {"choice": "not_supported", "confidence": 0.9}


def body(qids=("q0", "q1")):
    return {"model": "synthetic", "state": {"target": {"facts": [
        {"id": "q0", "fact": {"id": FACT0}}, {"id": "q1", "fact": {"id": FACT1}}]}},
        "questions": {qid: {} for qid in qids}}


def reserve(db, pid=1, identity=IDENTITY, qids=("q0", "q1")):
    fn = runtime.patient_summary_usage_reserver(db, project_id=pid, guard=lambda _stage: None,
                                                daily_request_budget=100, identity=identity)
    return fn(body(qids), 10)


def save_answer(db, fid=FACT0, value=ANSWER, identity=IDENTITY, pid=1):
    return db.artifact_add_tx("patient_context_summary_quality_v1", json.dumps(value), project_id=pid,
        meta={"inference_engine_version": inference_version.CURRENT_VERSION,
              "input_key": identity["input_key"], "audit_key": identity["audit_key"], "fact_id": fid})


def test_bound_unknown_fences_overlap_not_other_patient_or_generation(tmp_path):
    db = _ledger(tmp_path)
    try:
        first = reserve(db)
        row = db.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (first.artifact_id,)).fetchone()
        assert json.loads(row[0])["quality_request"]["questions"] == {"q0": FACT0, "q1": FACT1}
        assert runtime.patient_summary_request_problem(db.db, 1, IDENTITY, [FACT0]) == "patient_summary_request_unknown"
        with pytest.raises(semantic_jev.JevError):
            reserve(db, qids=("q0",))
        assert runtime.patient_summary_request_problem(db.db, 2, IDENTITY, [FACT0]) is None
        different = {**IDENTITY, "failure_key": "5" * 64}
        assert runtime.patient_summary_request_problem(db.db, 1, different, [FACT0]) is None
        reordered = {**IDENTITY, "audit_key": "6" * 64}
        assert runtime.patient_summary_request_problem(db.db, 1, reordered, [FACT0]) == "patient_summary_request_unknown"
    finally:
        db.close()


def test_resolution_requires_actual_answer_and_exact_question_coverage(tmp_path):
    db = _ledger(tmp_path)
    try:
        reservation = reserve(db)
        db.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError):
            runtime.patient_summary_resolve_request_tx(db, reservation, IDENTITY, {FACT0: ANSWER}, {FACT1}, project_id=1)
        save_answer(db)
        with pytest.raises(ValueError):
            runtime.patient_summary_resolve_request_tx(db, reservation, IDENTITY, {FACT0: ANSWER}, set(), project_id=1)
        with pytest.raises(ValueError):
            runtime.patient_summary_resolve_request_tx(db, reservation, IDENTITY, {FACT0: ANSWER}, {FACT1}, project_id=2)
        assert runtime.patient_summary_resolve_request_tx(db, reservation, IDENTITY, {FACT0: ANSWER}, {FACT1}, project_id=1) == 1
        assert runtime.patient_summary_resolve_request_tx(db, reservation, IDENTITY, {FACT0: ANSWER}, {FACT1}, project_id=1) == 0
        db.db.commit()
        assert runtime.patient_summary_request_problem(db.db, 1, IDENTITY, [FACT0, FACT1]) is None
        db.db.execute("DELETE FROM artifacts WHERE kind='patient_context_summary_quality_v1'")
        db.db.commit()
        assert runtime.patient_summary_request_problem(db.db, 1, IDENTITY, [FACT0]) == "patient_summary_request_unknown"
    finally:
        db.close()


def test_rollback_and_unbound_or_corrupt_metadata_remain_unknown(tmp_path):
    db = _ledger(tmp_path)
    try:
        reservation = reserve(db)
        db.db.execute("BEGIN IMMEDIATE")
        save_answer(db)
        runtime.patient_summary_resolve_request_tx(db, reservation, IDENTITY, {FACT0: ANSWER}, {FACT1}, project_id=1)
        db.db.rollback()
        assert runtime.patient_summary_request_problem(db.db, 1, IDENTITY, [FACT0]) == "patient_summary_request_unknown"
        db.artifact_add('semantic_usage', '{}', project_id=2,
                        meta={'operation': 'patient_summary_quality', 'jev_requests': 1})
        assert runtime.patient_summary_request_problem(db.db, 2, IDENTITY, [FACT0]) == "patient_summary_request_unknown"
        db.db.execute("INSERT INTO artifacts(kind,project_id,content,meta,created_at) "
                      "VALUES('semantic_usage',3,'{}',?,0)",
                      ('{"operation":"other","operation":"patient_summary_quality"}',))
        db.db.commit()
        assert runtime.patient_summary_request_problem(db.db, 3, IDENTITY, [FACT0]) == "patient_summary_request_unknown"
    finally:
        db.close()


def test_wire_binding_rejects_duplicate_facts_and_unmapped_questions(tmp_path):
    db = _ledger(tmp_path)
    try:
        fn = runtime.patient_summary_usage_reserver(db, project_id=1, guard=lambda _stage: None,
                                                    daily_request_budget=100, identity=IDENTITY)
        invalid = body()
        invalid["state"]["target"]["facts"][1]["fact"]["id"] = FACT0
        with pytest.raises(ValueError):
            fn(invalid, 10)
        with pytest.raises(ValueError):
            fn(body(("q2",)), 10)
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='semantic_usage'").fetchone()[0] == 0
    finally:
        db.close()


def test_duplicate_or_changed_answer_evidence_never_resolves(tmp_path):
    db = _ledger(tmp_path)
    try:
        reservation = reserve(db, qids=("q0",))
        db.db.execute("BEGIN IMMEDIATE")
        save_answer(db)
        with pytest.raises(ValueError):
            runtime.patient_summary_resolve_request_tx(db, reservation, IDENTITY,
                {FACT0: {"choice": "supports", "confidence": 0.9}}, set(), project_id=1)
        save_answer(db)
        with pytest.raises(ValueError):
            runtime.patient_summary_resolve_request_tx(db, reservation, IDENTITY,
                {FACT0: ANSWER}, set(), project_id=1)
        db.db.rollback()
        assert runtime.patient_summary_request_problem(db.db, 1, IDENTITY, [FACT0]) == "patient_summary_request_unknown"
    finally:
        db.close()


def test_bad_result_metadata_cannot_be_hidden_by_another_generation(tmp_path):
    db = _ledger(tmp_path)
    try:
        reservation = reserve(db, qids=("q0",))
        raw = db.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (reservation.artifact_id,)).fetchone()[0]
        meta = json.loads(raw)
        meta["quality_result"] = {"answers": {}, "invalid": [FACT1]}
        db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), reservation.artifact_id))
        db.db.commit()
        different = {**IDENTITY, "failure_key": "5" * 64}
        assert runtime.patient_summary_request_problem(db.db, 1, different, [FACT1]) == "patient_summary_request_unknown"
    finally:
        db.close()


@pytest.mark.parametrize("malformed", [
    '{"input_key":"' + "f" * 64 + '","input_key":"' + "1" * 64 + '"}',
    '{"number":1e999}',
    '{"broken":',
])
def test_ambiguous_answer_metadata_cannot_hide_from_scoped_receipt_validation(tmp_path, malformed):
    db = _ledger(tmp_path)
    try:
        reservation = reserve(db, qids=("q0",))
        db.db.execute("BEGIN IMMEDIATE")
        save_answer(db)
        db.db.execute("INSERT INTO artifacts(kind,project_id,content,meta,created_at) "
                      "VALUES('patient_context_summary_quality_v1',1,'{}',?,0)", (malformed,))
        with pytest.raises(ValueError):
            runtime.patient_summary_resolve_request_tx(db, reservation, IDENTITY,
                {FACT0: ANSWER}, set(), project_id=1)
        with pytest.raises(ValueError):
            runtime.patient_summary_resolve_request_tx(db, reservation, IDENTITY,
                {}, {FACT0}, project_id=1)
        db.db.rollback()
    finally:
        db.close()


def test_invalid_limit_survives_cleanup_and_clears_only_with_answer(tmp_path):
    db = _ledger(tmp_path)
    try:
        for attempt in range(2):
            ticket = reserve(db, qids=("q0",))
            db.db.execute("BEGIN IMMEDIATE")
            assert runtime.patient_summary_resolve_request_tx(
                db, ticket, IDENTITY, {}, {FACT0}, project_id=1) == 1
            db.db.commit()
            assert runtime.patient_summary_request_problem(db.db, 1, IDENTITY, [FACT0]) == (
                None if attempt == 0 else "patient_summary_invalid_exhausted")
        db.db.execute("UPDATE artifacts SET created_at=? WHERE kind='semantic_usage'", (time.time() - 9 * 86400,))
        db.db.commit()
        maintenance.prune_history(db.db)
        assert db.db.execute("SELECT count(*) FROM artifacts WHERE kind='semantic_usage'").fetchone()[0] == 2
        assert runtime.patient_summary_request_problem(db.db, 1, IDENTITY, [FACT0]) == "patient_summary_invalid_exhausted"
        with pytest.raises(semantic_jev.JevError) as error:
            reserve(db, qids=("q0",))
        assert error.value.detail == "patient_summary_invalid_exhausted"
        db.db.execute("BEGIN IMMEDIATE")
        save_answer(db)
        db.db.commit()
        maintenance.prune_history(db.db)
        assert db.db.execute("SELECT count(*) FROM artifacts WHERE kind='semantic_usage'").fetchone()[0] == 0
        assert runtime.patient_summary_request_problem(db.db, 1, IDENTITY, [FACT0]) is None
    finally:
        db.close()


@pytest.mark.parametrize("qid", ["q12", "q000", "q" + "1" * 1000, "q１"])
def test_wire_question_ids_are_limited_to_the_protocol(qid, tmp_path):
    db = _ledger(tmp_path)
    try:
        data = body((qid,))
        data["state"]["target"]["facts"][0]["id"] = qid
        fn = runtime.patient_summary_usage_reserver(db, project_id=1, guard=lambda _stage: None,
                                                    daily_request_budget=100, identity=IDENTITY)
        with pytest.raises(ValueError):
            fn(data, 10)
    finally:
        db.close()
