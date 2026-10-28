"""Synthetic request bindings protect unknown Jev results across later attempts."""

import json
import time

import pytest

import inference_version
import maintenance
import semantic_audit
import semantic_jev
import semantic_runtime as runtime
from mcs_util import payload_hash
from semantic_testkit import _ledger
from test_patient_summary_audit import sample


IDENTITY = {"input_key": "1" * 64, "candidate_key": "2" * 64,
            "failure_key": "3" * 64, "audit_key": "4" * 64}
FACT0, FACT1 = "a" * 64, "b" * 64
ANSWER = {"choice": "not_supported", "confidence": 0.9}


def body(qids=("q0", "q1")):
    return {"model": "synthetic", "state": {"target": {"facts": [
        {"id": "q0", "fact": {"id": FACT0}}, {"id": "q1", "fact": {"id": FACT1}}]}},
        "questions": {qid: {} for qid in qids}}


def reserve(db, pid=1, identity=IDENTITY, qids=("q0", "q1"), *, purpose="summary"):
    fn = runtime.patient_summary_usage_reserver(db, project_id=pid, guard=lambda _stage: None,
                                                daily_request_budget=100, identity=identity, purpose=purpose)
    return fn(body(qids), 10)


def save_answer(db, fid=FACT0, value=ANSWER, identity=IDENTITY, pid=1, *, purpose="summary"):
    kind = {"summary": "patient_context_summary_quality_v1", "source": "patient_context_source_quality_v1"}[purpose]
    return db.artifact_add_tx(kind, json.dumps(value), project_id=pid,
        meta={"inference_engine_version": inference_version.CURRENT_VERSION,
              "input_key": identity["input_key"], "audit_key": identity["audit_key"], "fact_id": fid})


def source_case(identity=IDENTITY, *, extra=False, source_input_key="7" * 64, assessment=None, coverage=False):
    _, facts, sources = sample()
    if extra:
        facts.append({**facts[0], "id": "c" * 64})
    assessment = assessment or {"code": "8" * 64, "policy": "9" * 64}
    pages = list(semantic_audit.patient_source_quality_pages(sources[0], facts,
        source_input_key=source_input_key, assessment=assessment))
    page = pages[-1] if coverage else pages[0]
    keys = {**identity, "source_input_key": source_input_key, "assessment": assessment,
            "question_keys": {fid: key for item in pages for fid, key in item["question_keys"].items()},
            "passing_choices": {fid: value for item in pages for fid, value in item["passing_choices"].items()}}
    wire = {"model": semantic_jev.JEV_MODEL, "state": page["state"], "questions": page["questions"]}
    return keys, wire, page["fact_ids"]


def reserve_source(db, identity, wire, qids=None, *, pid=1):
    wire = {**wire, "questions": {qid: value for qid, value in wire["questions"].items()
                                if qids is None or qid in qids}}
    return runtime.patient_summary_usage_reserver(db, project_id=pid, guard=lambda _stage: None,
        daily_request_budget=100, identity=identity, purpose="source")(wire, 10)


def save_source_answer(db, ticket, identity, fid=FACT0, value=ANSWER, *, pid=1, **change):
    binding = runtime.patient_source_answer_binding(db.db, pid, identity, ticket, [fid])[fid]
    return db.artifact_add_tx("patient_context_source_quality_v1", json.dumps(value), project_id=pid,
        meta={"inference_engine_version": inference_version.CURRENT_VERSION,
              "input_key": identity["input_key"], "audit_key": identity["audit_key"], "fact_id": fid,
              **binding, **change})


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


def test_source_and_summary_receipts_are_separate_even_with_identical_identity(tmp_path):
    db = _ledger(tmp_path)
    try:
        source_identity, wire, _ = source_case()
        source = reserve_source(db, source_identity, wire, ("q0",))
        assert runtime.patient_summary_request_problem(db.db, 1, source_identity, [FACT0], purpose="source") == "patient_summary_request_unknown"
        assert runtime.patient_summary_request_problem(db.db, 1, IDENTITY, [FACT0]) is None
        summary = reserve(db, qids=("q0",))
        db.db.execute("BEGIN IMMEDIATE")
        save_answer(db, value=ANSWER)
        with pytest.raises(ValueError):
            runtime.patient_summary_resolve_request_tx(db, source, source_identity, {FACT0: ANSWER}, set(), project_id=1, purpose="source")
        with pytest.raises(ValueError):
            runtime.patient_summary_resolve_request_tx(db, source, IDENTITY, {FACT0: ANSWER}, set(), project_id=1)
        save_source_answer(db, source, source_identity)
        assert runtime.patient_summary_resolve_request_tx(db, source, source_identity, {FACT0: ANSWER}, set(), project_id=1, purpose="source") == 1
        assert runtime.patient_summary_resolve_request_tx(db, summary, IDENTITY, {FACT0: ANSWER}, set(), project_id=1) == 1
        db.db.commit()
        assert runtime.patient_summary_request_problem(db.db, 1, source_identity, [FACT0], purpose="source") is None
        assert runtime.patient_summary_request_problem(db.db, 1, IDENTITY, [FACT0]) is None
        db.db.execute("DELETE FROM artifacts WHERE kind='patient_context_source_quality_v1'")
        db.db.commit()
        assert runtime.patient_summary_request_problem(db.db, 1, source_identity, [FACT0], purpose="source") == "patient_summary_request_unknown"
        assert runtime.patient_summary_request_problem(db.db, 1, IDENTITY, [FACT0]) == "patient_summary_request_unknown"
    finally:
        db.close()


@pytest.mark.parametrize("choice", ["supports", "contradicts", "not_supported", "ambiguous", "complete", "missing"])
def test_source_supported_answers_resolve_but_summary_choices_do_not(tmp_path, choice):
    db = _ledger(tmp_path)
    try:
        source_identity, wire, ids = source_case(coverage=choice in ("complete", "missing"))
        fid = ids[0]
        ticket = reserve_source(db, source_identity, wire, ("q0",))
        value = {"choice": choice, "confidence": 0.9}
        db.db.execute("BEGIN IMMEDIATE")
        save_source_answer(db, ticket, source_identity, fid, value)
        assert runtime.patient_summary_resolve_request_tx(db, ticket, source_identity, {fid: value}, set(), project_id=1, purpose="source") == 1
        db.db.commit()
        assert runtime.patient_summary_request_problem(db.db, 1, source_identity, [fid], purpose="source") is None
    finally:
        db.close()


@pytest.mark.parametrize("purpose,choice", [("source", "omission_ok"), ("source", "important_missing"),
                                           ("summary", "complete"), ("summary", "missing")])
def test_other_purpose_answer_is_rejected(tmp_path, purpose, choice):
    db = _ledger(tmp_path)
    try:
        identity, wire, _ = source_case() if purpose == "source" else (IDENTITY, None, None)
        ticket = reserve_source(db, identity, wire, ("q0",)) if purpose == "source" else reserve(db, qids=("q0",))
        value = {"choice": choice, "confidence": 0.9}
        db.db.execute("BEGIN IMMEDIATE")
        if purpose == "source":
            save_source_answer(db, ticket, identity, value=value)
        else:
            save_answer(db, value=value)
        with pytest.raises(ValueError):
            runtime.patient_summary_resolve_request_tx(db, ticket, identity, {FACT0: value}, set(), project_id=1, purpose=purpose)
        db.db.rollback()
    finally:
        db.close()


def test_source_invalid_attempts_and_unknown_survive_cleanup(tmp_path):
    db = _ledger(tmp_path)
    try:
        source_identity, wire, _ = source_case()
        for _ in range(2):
            ticket = reserve_source(db, source_identity, wire, ("q0",))
            db.db.execute("BEGIN IMMEDIATE")
            assert runtime.patient_summary_resolve_request_tx(db, ticket, source_identity, {}, {FACT0}, project_id=1, purpose="source") == 1
            db.db.commit()
        assert runtime.patient_summary_request_problem(db.db, 1, source_identity, [FACT0], purpose="source") == "patient_summary_invalid_exhausted"
        assert runtime.patient_summary_request_problem(db.db, 1, IDENTITY, [FACT0]) is None
        unknown = reserve_source(db, source_identity, wire, ("q1",))
        db.db.execute("UPDATE artifacts SET created_at=? WHERE kind='semantic_usage'", (time.time() - 9 * 86400,))
        db.db.commit()
        maintenance.prune_history(db.db)
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='semantic_usage'").fetchone()[0] == 3
        assert runtime.patient_summary_request_problem(db.db, 1, source_identity, [FACT1], purpose="source") == "patient_summary_request_unknown"
        db.db.execute("BEGIN IMMEDIATE")
        # An unbound answer is not proof that either spent invalid request received it.
        save_answer(db, value={"choice": "complete", "confidence": 0.9}, purpose="source")
        db.db.commit()
        maintenance.prune_history(db.db)
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE artifact_id=?", (unknown.artifact_id,)).fetchone()[0] == 1
        assert runtime.patient_summary_request_problem(db.db, 1, source_identity, [FACT0], purpose="source") == "patient_summary_request_unknown"
    finally:
        db.close()


@pytest.mark.parametrize("purpose", ["source", "summary"])
def test_corrupt_other_purpose_receipt_is_neither_ignored_nor_pruned(tmp_path, purpose):
    db = _ledger(tmp_path)
    try:
        other = "summary" if purpose == "source" else "source"
        source_identity, wire, _ = source_case()
        ticket = reserve_source(db, source_identity, wire, ("q0",)) if other == "source" else reserve(db, qids=("q0",))
        raw = db.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (ticket.artifact_id,)).fetchone()[0]
        meta = json.loads(raw)
        meta["quality_request"]["questions"] = {"q12": FACT0}
        db.db.execute("UPDATE artifacts SET meta=?,created_at=? WHERE artifact_id=?",
                      (json.dumps(meta), time.time() - 9 * 86400, ticket.artifact_id))
        db.db.commit()
        identity = source_identity if purpose == "source" else IDENTITY
        assert runtime.patient_summary_request_problem(db.db, 1, identity, [FACT0], purpose=purpose) == "patient_summary_request_unknown"
        maintenance.prune_history(db.db)
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE artifact_id=?", (ticket.artifact_id,)).fetchone()[0] == 1
    finally:
        db.close()


def finish_source(db, ticket, identity, values=None, invalid=()):
    values = values or {}
    db.db.execute("BEGIN IMMEDIATE")
    saved = {fid: save_source_answer(db, ticket, identity, fid, value) for fid, value in values.items()}
    runtime.patient_summary_resolve_request_tx(db, ticket, identity, values, set(invalid), project_id=1, purpose="source")
    db.db.commit()
    return saved


def test_source_v2_persists_only_actual_questions_and_the_original_answer_reference(tmp_path):
    db = _ledger(tmp_path)
    try:
        identity, wire, _ = source_case()
        identity["question_keys"].update({payload_hash(["extra", i]): payload_hash(["key", i]) for i in range(1000)})
        identity["passing_choices"].update(dict.fromkeys(identity["question_keys"], "supports"))
        ticket = reserve_source(db, identity, wire, ("q0",))
        row = db.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (ticket.artifact_id,)).fetchone()
        request = json.loads(row[0])["quality_request"]
        assert request == {"v": 2, **IDENTITY, "source_input_key": identity["source_input_key"],
            "assessment_hash": payload_hash(identity["assessment"]),
            "questions": {"q0": {"fact_id": FACT0, "question_key": identity["question_keys"][FACT0]}}}
        assert len(row[0]) < 32768 and "assessment" not in request
        [answer_id] = finish_source(db, ticket, identity, {FACT0: ANSWER}).values()
        evidence = runtime.patient_source_question_evidence(db.db, 1, identity, [FACT0])
        assert evidence["answers"] == {FACT0: ANSWER} and evidence["problems"] == {}
        reference = evidence["references"][FACT0]
        assert reference["usage_artifact_id"] == ticket.artifact_id and reference["answer_artifact_id"] == answer_id
        assert reference["answer_hash"] == payload_hash(ANSWER)
        meta = json.loads(db.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (ticket.artifact_id,)).fetchone()[0])
        assert meta["quality_result"] == {"answers": {FACT0: {"answer_artifact_id": answer_id,
            "answer_hash": payload_hash(ANSWER)}}, "invalid": []}
    finally:
        db.close()


def test_negative_source_answer_survives_new_candidate_without_copying_or_resending(tmp_path):
    db = _ledger(tmp_path)
    try:
        identity, wire, _ = source_case()
        ticket = reserve_source(db, identity, wire, ("q0",))
        finish_source(db, ticket, identity, {FACT0: ANSWER})
        before = [tuple(row) for row in db.db.execute("SELECT artifact_id,content,meta FROM artifacts "
            "WHERE kind IN ('semantic_usage','patient_context_source_quality_v1') ORDER BY artifact_id")]
        changed, next_wire, _ = source_case({**IDENTITY, "candidate_key": "5" * 64, "audit_key": "6" * 64}, extra=True)
        assert changed["question_keys"][FACT0] == identity["question_keys"][FACT0]
        proof = runtime.patient_source_question_evidence(db.db, 1, changed, [FACT0, "c" * 64])
        assert proof["answers"] == {FACT0: ANSWER} and not proof["problems"]
        assert proof["references"][FACT0]["usage_artifact_id"] == ticket.artifact_id
        with pytest.raises(semantic_jev.JevError) as caught:
            reserve_source(db, changed, next_wire, ("q0",))
        assert caught.value.detail == "patient_source_question_answered"
        assert before == [tuple(row) for row in db.db.execute("SELECT artifact_id,content,meta FROM artifacts "
            "WHERE kind IN ('semantic_usage','patient_context_source_quality_v1') ORDER BY artifact_id")]
        reserve_source(db, changed, next_wire, ("q2",))
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='semantic_usage'").fetchone()[0] == 2
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='patient_context_source_quality_v1'").fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.parametrize("state", ["unknown", "invalid_exhausted"])
def test_source_unknown_and_two_invalid_answers_follow_the_question_across_audits(tmp_path, state):
    db = _ledger(tmp_path)
    try:
        identity, wire, _ = source_case()
        ticket = reserve_source(db, identity, wire, ("q0",))
        if state == "invalid_exhausted":
            finish_source(db, ticket, identity, invalid=[FACT0])
            ticket = reserve_source(db, identity, wire, ("q0",))
            finish_source(db, ticket, identity, invalid=[FACT0])
        changed, next_wire, _ = source_case({key: payload_hash([key, "new"]) for key in IDENTITY}, extra=True)
        proof = runtime.patient_source_question_evidence(db.db, 1, changed, [FACT0, "c" * 64])
        assert proof["problems"] == {FACT0: state} and not proof["answers"]
        with pytest.raises(semantic_jev.JevError):
            reserve_source(db, changed, next_wire, ("q0",))
        reserve_source(db, changed, next_wire, ("q2",))
    finally:
        db.close()


@pytest.mark.parametrize("change", ["source", "assessment"])
def test_only_changed_source_or_interpretation_can_start_a_new_question(tmp_path, change):
    db = _ledger(tmp_path)
    try:
        identity, wire, _ = source_case()
        reserve_source(db, identity, wire, ("q0",))
        options = {"source_input_key": "e" * 64} if change == "source" else {
            "assessment": {"code": "e" * 64, "policy": "9" * 64}}
        changed, next_wire, _ = source_case(**options)
        assert identity["question_keys"][FACT0] != changed["question_keys"][FACT0]
        assert not runtime.patient_source_question_evidence(db.db, 1, changed, [FACT0])["problems"]
        reserve_source(db, changed, next_wire, ("q0",))
    finally:
        db.close()


@pytest.mark.parametrize("change", ["answer", "usage", "source_input_key", "question_key", "reservation_artifact_id",
                                     "reservation_id", "qid", "duplicate"])
def test_original_source_evidence_cannot_be_deleted_changed_or_copied(tmp_path, change):
    db = _ledger(tmp_path)
    try:
        identity, wire, _ = source_case()
        ticket = reserve_source(db, identity, wire, ("q0",))
        answer_id = finish_source(db, ticket, identity, {FACT0: ANSWER})[FACT0]
        if change in ("answer", "usage"):
            db.db.execute("DELETE FROM artifacts WHERE artifact_id=?", (answer_id if change == "answer" else ticket.artifact_id,))
        else:
            raw = db.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (answer_id,)).fetchone()[0]
            meta = json.loads(raw)
            if change == "duplicate":
                db.artifact_add_tx('patient_context_source_quality_v1', json.dumps(ANSWER), project_id=1, meta=meta)
            else:
                meta[change] = (ticket.artifact_id + 1 if change == "reservation_artifact_id" else
                                "0" * 32 if change == "reservation_id" else "q1" if change == "qid" else "e" * 64)
                db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), answer_id))
        db.db.commit()
        assert runtime.patient_source_question_evidence(db.db, 1, identity, [FACT0])["problems"] == {FACT0: "unknown"}
        with pytest.raises(semantic_jev.JevError):
            reserve_source(db, identity, wire, ("q0",))
    finally:
        db.close()


def test_source_v1_without_question_meaning_holds_source_but_not_summary(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.artifact_add('semantic_usage', '{}', project_id=1, meta={"jev_requests": 1, "reserved": True,
            "operation": "patient_source_quality", "request_fp": payload_hash(body()), "timeout_seconds": 10,
            "reservation_id": "f" * 32, "quality_request": runtime._quality_request(IDENTITY, body())})
        identity, wire, _ = source_case()
        assert runtime.patient_source_question_evidence(db.db, 1, identity, [FACT0])["problems"] == {FACT0: "unknown"}
        assert runtime.patient_summary_request_problem(db.db, 1, IDENTITY, [FACT0]) is None
        with pytest.raises(semantic_jev.JevError):
            reserve_source(db, identity, wire, ("q0",))
        reserve(db, qids=("q0",))
    finally:
        db.close()


@pytest.mark.parametrize("change", ["question", "fact", "source", "model", "identity"])
def test_source_reservation_recomputes_actual_body_question_meaning(tmp_path, change):
    from copy import deepcopy
    db = _ledger(tmp_path)
    try:
        identity, original, _ = source_case()
        wire = deepcopy(original)
        if change == "question":
            wire["questions"]["q0"]["instructions"] += " Different synthetic rule."
        elif change == "fact":
            wire["state"]["target"]["facts"][0]["fact"]["temporality"] = "planned"
        elif change == "source":
            wire["state"]["context"][0]["text"] += "追加。"
        elif change == "model":
            wire["model"] = "different-synthetic-model"
        else:
            identity["question_keys"][FACT0] = "e" * 64
        with pytest.raises(ValueError):
            reserve_source(db, identity, wire, ("q0",))
        assert not db.artifacts('semantic_usage')
    finally:
        db.close()


def test_new_answer_binding_cannot_borrow_an_old_reservation_or_a_sealed_result(tmp_path):
    db = _ledger(tmp_path)
    try:
        identity, wire, _ = source_case()
        ticket = reserve_source(db, identity, wire, ("q0",))
        with pytest.raises(RuntimeError):
            runtime.patient_source_answer_binding(db.db, 1, identity, ticket, [FACT0])
        db.db.execute("BEGIN IMMEDIATE")
        for pid, changed, ids in ((2, identity, [FACT0]), (1, {**identity, "audit_key": "e" * 64}, [FACT0]),
                                  (1, identity, [FACT1])):
            with pytest.raises(ValueError):
                runtime.patient_source_answer_binding(db.db, pid, changed, ticket, ids)
        binding = runtime.patient_source_answer_binding(db.db, 1, identity, ticket, [FACT0])[FACT0]
        assert binding["reservation_artifact_id"] == ticket.artifact_id and binding["qid"] == "q0"
        db.db.rollback()
        finish_source(db, ticket, identity, {FACT0: ANSWER})
        db.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError):
            runtime.patient_source_answer_binding(db.db, 1, identity, ticket, [FACT0])
        db.db.rollback()
    finally:
        db.close()


def test_source_request_lookup_decodes_only_relevant_questions_and_handles_scalar_corruption(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        identity, wire, _ = source_case()
        ticket = reserve_source(db, identity, wire, ("q0",))
        raw = db.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (ticket.artifact_id,)).fetchone()[0]
        for i in range(40):
            meta = json.loads(raw)
            meta['reservation_id'] = payload_hash(['reservation', i])[:32]
            meta['quality_request']['source_input_key'] = payload_hash(['source', i])
            meta['quality_request']['questions']['q0']['question_key'] = payload_hash(['question', i])
            db.artifact_add_tx('semantic_usage', '{}', project_id=1, meta=meta)
        db.db.commit()
        parsed = []
        original = runtime._quality_json
        def decode(value):
            parsed.append(value)
            return original(value)
        monkeypatch.setattr(runtime, '_quality_json', decode)
        assert runtime.patient_source_question_evidence(db.db, 1, identity, [FACT0])["problems"] == {FACT0: "unknown"}
        assert len(parsed) == 1  # Unrelated source requests remain SQL-filtered, not Python-decoded.
        meta = json.loads(raw)
        meta['quality_request']['questions']['q0'] = 'not a JSON object'
        db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), ticket.artifact_id))
        db.db.commit()
        assert runtime.patient_source_question_evidence(db.db, 1, identity, [FACT0])["problems"] == {FACT0: "unknown"}
        with pytest.raises(ValueError):
            runtime.patient_source_question_evidence(db.db, 1, identity, [FACT0] * 13)
    finally:
        db.close()


@pytest.mark.parametrize('count', [13, 257])
@pytest.mark.parametrize('state', ['unknown', 'invalid_exhausted'])
def test_source_problem_checks_the_entire_denominator_in_bounded_pages(tmp_path, count, state):
    db = _ledger(tmp_path)
    try:
        _, original, sources = sample()
        facts = [{**original[0], 'id': payload_hash(['denominator', i])} for i in range(count)]
        assessment = {'code': '8' * 64, 'policy': '9' * 64}
        pages = list(semantic_audit.patient_source_quality_pages(sources[0], facts,
            source_input_key='7' * 64, assessment=assessment))
        identity = {**IDENTITY, 'source_input_key': '7' * 64, 'assessment': assessment,
            'question_keys': {fid: key for page in pages for fid, key in page['question_keys'].items()},
            'passing_choices': {fid: value for page in pages for fid, value in page['passing_choices'].items()}}
        fid = max(identity['question_keys'])
        last = next(page for page in pages if fid in page['fact_ids'])
        qids = [qid for qid, target in zip(last['questions'], last['state']['target']['facts'])
                if target['fact']['id'] == fid]
        wire = {'model': semantic_jev.JEV_MODEL, 'state': last['state'], 'questions': last['questions']}
        ticket = reserve_source(db, identity, wire, qids)
        if state == 'invalid_exhausted':
            finish_source(db, ticket, identity, invalid=[fid])
            ticket = reserve_source(db, identity, wire, qids)
            finish_source(db, ticket, identity, invalid=[fid])
        ids = sorted(key for page in pages for key in page['fact_ids'])
        assert len(ids) == count + 1 and ids[-1] == fid
        assert runtime.patient_summary_request_problem(db.db, 1, identity, ids, purpose='source') == (
            'patient_summary_request_unknown' if state == 'unknown' else 'patient_summary_invalid_exhausted')
    finally:
        db.close()


@pytest.mark.parametrize('kind', ['semantic_usage', 'patient_context_source_quality_v1'])
def test_oversized_source_receipt_metadata_is_unknown_without_hydrating_it(tmp_path, kind):
    db = _ledger(tmp_path)
    try:
        identity, _, ids = source_case()
        db.db.execute('INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES(?,1,?,?,0)',
                      (kind, '{}', 'x' * (256 * 1024)))
        db.db.commit()
        def bounded_text(raw):
            assert len(raw) <= 32768, 'oversized receipt metadata must remain inside SQLite'
            return raw.decode('utf-8')
        db.db.text_factory = bounded_text
        evidence = runtime.patient_source_question_evidence(db.db, 1, identity, ids)
        assert evidence['problems'] == dict.fromkeys(ids, 'unknown')
        assert not evidence['answers'] and not evidence['references']
    finally:
        db.close()


@pytest.mark.parametrize("purpose", ["summary", "source"])
def test_window_ending_after_the_reservation_seals_it_unsent_and_the_page_can_be_asked_again(tmp_path, purpose):
    conf = tmp_path / "config.json"
    def until(value):
        conf.write_text(json.dumps({"semantic": {"until": value}}))
    until("2999-01-01T00:00:00+00:00")
    db = _ledger(tmp_path)
    try:
        if purpose == "source":
            identity, wire, ids = source_case()
            reserver = runtime.patient_summary_usage_reserver(db, project_id=1, guard=lambda _stage: None,
                daily_request_budget=100, identity=identity, purpose="source")
        else:
            identity, wire, ids = IDENTITY, body(), [FACT0, FACT1]
            reserver = runtime.patient_summary_usage_reserver(db, project_id=1, guard=lambda _stage: None,
                daily_request_budget=100, identity=identity)

        def reserve_then_window_ends(payload, timeout):
            reservation = reserver(payload, timeout)
            until("2000-01-01T00:00:00+00:00")     # the window ended while BEGIN waited
            return reservation

        def seal(unsent, reason):
            db.db.execute("BEGIN IMMEDIATE")
            with db.db:
                runtime.patient_summary_resolve_request_tx(db, unsent, identity, {}, set(), project_id=1,
                                                           purpose=purpose, not_sent=reason)

        class Client:
            supports_attempt_hooks = True
            posted = False

            def evaluate(self, state, questions, deadline, *, before_attempt, after_result, reserve_fn, transport):
                before_attempt()
                reserve_fn(wire, 10)
                before_attempt()                 # the client's last hook before its POST
                self.posted = True

        client = Client()
        bound = runtime.bind_jev(client, lambda _stage: None, reserve_then_window_ends,
                                 config_path=conf, not_sent=seal)
        with pytest.raises(semantic_jev.JevError) as raised:
            bound.evaluate({}, {}, time.time() + 60)
        assert raised.value.detail == "semantic_until_expired" and not client.posted
        (meta,) = [json.loads(row[0]) for row in db.db.execute(
            "SELECT meta FROM artifacts WHERE kind='semantic_usage'")]
        assert meta["quality_result"] == {"answers": {}, "invalid": [], "not_sent": "semantic_until_expired"}
        # Settled, not unknown and not an invalid attempt: once the window reopens it is asked again.
        assert runtime.patient_summary_request_problem(db.db, 1, identity, ids, purpose=purpose) is None
        until("2999-01-01T00:00:00+00:00")
        assert reserver(wire, 10) is not None
    finally:
        db.close()


def test_not_sent_is_only_a_seal_without_answers(tmp_path):
    db = _ledger(tmp_path)
    try:
        reservation = reserve(db)
        db.db.execute("BEGIN IMMEDIATE")
        for answers, invalid, reason in (({FACT0: ANSWER}, set(), "semantic_until_expired"),
                                         ({}, {FACT0}, "semantic_until_expired"), ({}, set(), "http_200")):
            with pytest.raises(ValueError):
                runtime.patient_summary_resolve_request_tx(db, reservation, IDENTITY, answers, invalid,
                                                           project_id=1, not_sent=reason)
        db.db.rollback()
    finally:
        db.close()
