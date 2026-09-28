"""Observed verified -> rendered -> delivered fact sets from a read-only
ledger snapshot (semantic_lifecycle). Every fixture is synthetic: the
stored artifacts and the frozen notice are built with the production
renderer/chunker on a temp ledger."""
import copy
import json
import sqlite3

import pytest

import notify_flush
import semantic_blind as blind
import semantic_evaluation as evaluation
import semantic_lifecycle as lifecycle
import semantic_v4
from semantic_evaluation import EvaluationError
from semantic_render import mandatory_render, render_notice
from test_mcs_semantic import _ledger, _message, _patient
from test_semantic_evaluation import CRITERIA, MANIFEST, _record

FP, POLICY, REV = "fp-synthetic", "policy-synthetic", "rev-synthetic"


def _fact(fid, statement):
    return {"fact_id": fid, "kind": "medication_event",
            "subject": "patient:1", "actor": "sender:s1",
            "statement": statement, "polarity": "affirmed",
            "epistemic": "asserted", "workflow_status": "performed",
            "event_time": "unknown", "valid_time": "unknown",
            "evidence_ids": [], "obligation_ids": [],
            "importance": "T1", "provenance": "local_llm",
            "validation_status": "verified"}


def _doc(facts):
    return {"version": "semantic-facts/v2",
            "source": {"message_id": "m1", "revision": REV,
                       "source_fingerprint": FP},
            "atoms": [], "chunks": [], "evidence": [],
            "facts": facts, "obligations": [], "relations": [],
            "coverage": {"category_counts": {}, "open_obligation_ids": [],
                         "limitations": [], "status": "complete"}}


def _meta(**extra):
    return {"fingerprint": FP, "policy_fingerprint": POLICY, **extra}


def _audit(db, doc, status="PASS"):
    db.artifact_add("semantic_facts_audit", json.dumps(
        {"status": status, "evaluated": True, "findings": [],
         "fact_verdicts": {}}), project_id=1, message_id=1,
        meta=_meta(doc_hash=semantic_v4._doc_hash(doc), audit_status=status))


def _chain(tmp_path, facts, audit="PASS", notice=True):
    """v2 doc + fact audit + summary (stored mandatory pages) + frozen
    semantic_notice for message 1. Returns (ledger, final_id, event_id,
    chunks, summary)."""
    db = _ledger(tmp_path)
    p = _patient(db)
    p.messages = [_message(1)]
    db.save_patient(p)
    doc = _doc(facts)
    db.artifact_add("semantic_facts_v2", json.dumps(doc, ensure_ascii=False),
                    project_id=1, message_id=1, meta=_meta())
    if audit is not None:
        _audit(db, doc, audit)
    rendered = mandatory_render(doc)
    summary = {"claims": [{"section": "status", "claim_kind": "fact",
                           "text": "合成の状態記述"}],
               "limitations": [],
               "mandatory_facts": rendered["facts"],
               "mandatory_fact_ids": rendered["fact_ids"],
               "mandatory_pages": rendered["pages"],
               "mandatory_overview": rendered["overview"]}
    final_id = db.artifact_add(
        "semantic_summary", json.dumps(summary, ensure_ascii=False),
        project_id=1, message_id=1,
        meta=_meta(audit_status="PASS", publication_mode="enforce",
                   target_revision=REV))
    event_id, chunks = None, []
    if notice:
        text = render_notice(db, 1, 1, summary, "PASS", targets=[1],
                             quality="full", focus_mid=1)
        chunks = notify_flush._semantic_chunks(text)
        event_id = db.outbox_add("semantic_notice", 1, {
            "delivery_key": "dk", "root_id": 1, "target_message_id": 1,
            "target_revision": REV, "src_event_id": 1, "text": text,
            "fingerprint": FP, "policy_version": "pv",
            "policy_fingerprint": POLICY})
    return db, final_id, event_id, chunks, summary


def _receipt(db, event_id, state, accepted, count=None):
    db.db.execute(
        "UPDATE notify_outbox SET state=?,progress=? WHERE event_id=?",
        (state, json.dumps({"next": accepted,
                            "sent": [str(i) for i in range(1, accepted + 1)],
                            "fingerprint": "synthetic", "sending": None}),
         event_id))
    db.db.commit()


def _read(tmp_path, final_id):
    with sqlite3.connect((tmp_path / "ledger.db").resolve().as_uri()
                         + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        return lifecycle.fact_lifecycle(con, final_id)


def test_full_chain_yields_equal_sets_and_complete_evaluator_lifecycle(
        tmp_path, monkeypatch):
    db, final_id, event_id, chunks, _ = _chain(
        tmp_path, [_fact("f1", "薬剤A継続"), _fact("f2", "薬剤B中止")])
    try:
        _receipt(db, event_id, "accepted", len(chunks))
        before = list(db.db.iterdump())
        record = _record(source="human")
        for stage in evaluation.LIFECYCLE_STAGES:
            del record["candidate"][f"{stage}_fact_ids"]
        record["artifact_ids"] = {"final_id": final_id}
        source = tmp_path / "candidate-records.jsonl"
        source.write_text(json.dumps(record, ensure_ascii=False) + "\n")
        uris = []
        real_connect = sqlite3.connect

        def connect(target, *args, **kwargs):
            uris.append(target)
            return real_connect(target, *args, **kwargs)

        monkeypatch.setattr(lifecycle.sqlite3, "connect", connect)
        assert blind.main(["--input", str(source), "--output-dir",
                           str(tmp_path / "out"), "--lifecycle-snapshot",
                           str(tmp_path / "ledger.db")]) == 0
        assert uris and all(u.endswith("?mode=ro") for u in uris)
        assert list(db.db.iterdump()) == before
        out = json.loads((tmp_path / "out" / "candidate-records.jsonl").read_text())
        cand = out["candidate"]
        assert cand["verified_fact_ids"] == ["f1", "f2"]
        assert cand["rendered_fact_ids"] == ["f1", "f2"]
        assert cand["delivered_fact_ids"] == ["f1", "f2"]
        assert out["fact_lifecycle_observations"] == {
            "verified": "pass_audit", "rendered": "pages_complete",
            "delivered": "notice_receipt"}
        report = evaluation.evaluate_records([out], MANIFEST, CRITERIA)
        assert report["fact_lifecycle"]["complete"] == 1
        assert report["fact_lifecycle"]["missing_observations"] == 0
        assert "fact_lifecycle_incomplete" not in report["gate"]["reasons"]
        # stage lists already present are never overwritten
        with pytest.raises(EvaluationError, match="lifecycle_already_present"):
            lifecycle.attach_lifecycle(str(tmp_path / "ledger.db"), [out])
    finally:
        db.close()


def test_undelivered_chunk_drops_every_fact_it_carries(tmp_path):
    facts = [_fact(f"f{i}", f"合成事実{i}：" + "長い記述" * 12)
             for i in range(1, 41)]
    db, final_id, event_id, chunks, summary = _chain(tmp_path, facts)
    try:
        assert len(chunks) >= 3
        _receipt(db, event_id, "failed", 1)       # only chunk 1 accepted
        got = _read(tmp_path, final_id)["fact_ids"]
        ids = [f["fact_id"] for f in facts]
        assert got["verified"] == ids and got["rendered"] == ids
        delivered = got["delivered"]
        assert delivered and len(delivered) < len(ids)
        first_body = chunks[0]
        lines = dict(zip(summary["mandatory_fact_ids"],
                         summary["mandatory_facts"], strict=True))
        for fid in ids:
            whole = ("・" + lines[fid]) in first_body
            assert (fid in delivered) == whole
        # a line split across the chunk 1/2 boundary counts as undelivered
        split = [fid for fid in ids if fid not in delivered
                 and lines[fid][:lines[fid].index("：") + 1] in first_body]
        assert split
        # once every chunk is accepted the same notice delivers them all
        _receipt(db, event_id, "accepted", len(chunks))
        assert _read(tmp_path, final_id)["fact_ids"]["delivered"] == ids
    finally:
        db.close()


def test_unprovable_delivery_omits_the_stage(tmp_path):
    db, final_id, _, _, _ = _chain(tmp_path, [_fact("f1", "薬剤A継続")],
                                   notice=False)
    try:
        got = _read(tmp_path, final_id)
        assert "delivered" not in got["fact_ids"]
        assert got["observations"]["delivered"] == "notice_missing"
        assert got["fact_ids"]["rendered"] == ["f1"]
    finally:
        db.close()


def test_accepted_state_without_full_receipt_is_unprovable(tmp_path):
    db, final_id, event_id, chunks, _ = _chain(
        tmp_path, [_fact("f1", "薬剤A継続")])
    try:
        _receipt(db, event_id, "accepted", 0)
        got = _read(tmp_path, final_id)
        assert "delivered" not in got["fact_ids"]
        assert got["observations"]["delivered"] == "notice_receipt_unprovable"
        record = _record(source="human")
        del record["candidate"]["delivered_fact_ids"]
        report = evaluation.evaluate_records([record], MANIFEST, CRITERIA)
        assert report["fact_lifecycle"]["missing_observations"] == 1
        assert "fact_lifecycle_incomplete" in report["gate"]["reasons"]
    finally:
        db.close()


@pytest.mark.parametrize("audit", [None, "NEEDS_REVIEW", "later_needs_review",
                                   "stale_doc"])
def test_non_pass_fact_audit_leaves_verified_unobserved(tmp_path, audit):
    facts = [_fact("f1", "薬剤A継続")]
    db, final_id, event_id, chunks, _ = _chain(
        tmp_path, facts,
        audit="PASS" if audit in ("later_needs_review", "stale_doc")
        else audit)
    try:
        if audit == "later_needs_review":
            _audit(db, _doc(facts), "NEEDS_REVIEW")
        if audit == "stale_doc":
            # a newer (e.g. repaired) doc for the generation has no audit
            newer = _doc([_fact("f1", "薬剤A継続"), _fact("f9", "合成")])
            db.artifact_add("semantic_facts_v2", json.dumps(newer),
                            project_id=1, message_id=1, meta=_meta())
        _receipt(db, event_id, "accepted", len(chunks))
        got = _read(tmp_path, final_id)
        assert "verified" not in got["fact_ids"]
        assert got["observations"]["verified"] in (
            "fact_audit_missing", "fact_audit_not_pass")
        # later stages are observed on their own — never imputed
        assert got["fact_ids"]["rendered"] == ["f1"]
        assert got["fact_ids"]["delivered"] == ["f1"]
    finally:
        db.close()


def test_artifact_binding_is_checked(tmp_path):
    db, final_id, _, _, _ = _chain(tmp_path, [_fact("f1", "薬剤A継続")])
    try:
        other = db.artifact_add("semantic_candidate", "{}", project_id=1,
                                message_id=1,
                                meta={"fingerprint": "another"})
        record = copy.deepcopy(_record())
        record["artifact_ids"] = {"final_id": final_id, "candidate_id": other}
        for stage in evaluation.LIFECYCLE_STAGES:
            del record["candidate"][f"{stage}_fact_ids"]
        with pytest.raises(EvaluationError, match="artifact_source_mismatch"):
            lifecycle.attach_lifecycle(str(tmp_path / "ledger.db"), [record])
        record["artifact_ids"] = {"final_id": other}
        with pytest.raises(EvaluationError, match="artifact_missing"):
            lifecycle.attach_lifecycle(str(tmp_path / "ledger.db"), [record])
    finally:
        db.close()
