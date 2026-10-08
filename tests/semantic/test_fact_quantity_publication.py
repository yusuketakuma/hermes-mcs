"""Published medication quantities must trace to the fact's own evidence."""
import json
import time

import pytest

import semantic
import semantic_audit as audit
import semantic_drain as drain
import semantic_v4 as v4
import structured_view
import semantic_projection as projection
from mcs_queries import FACT_KINDS_SQL, current_fact_pred
from semantic_testkit import (_PassJev, _canonical_cfg, _llm_v2,
                              _seeded_two, v2_doc, v2_fact)


def _doc(quote, quantity, *, statement=None, kind="medication_event"):
    ev = {"evidence_id": "ev_1", "message_id": "m1", "revision": "r1",
          "start": 0, "end": len(quote), "quote": quote, "atom_id": "atom_1"}
    return v2_doc([v2_fact("fact_a", kind=kind, action="continue",
                          statement=statement or quote, quantity=quantity,
                          evidence_ids=["ev_1"])], [ev])


@pytest.mark.parametrize("quote,quantity,statement,accepted", [
    ("アムロジピン5mgを継続", "5mg", None, True),
    ("アムロジピン５．０ｍｇを継続", "5 mg", None, True),
    ("アムロジピン.5mgを継続", "0.50mg", None, True),
    ("アムロジピン300mgを1日3回", "300mg×3回", None, True),
    ("アムロジピン5mgを継続", "50mg", None, False),
    ("アムロジピン500mgを継続", "0mg", None, False),
    ("アムロジピン5mgを継続", "5g", None, False),
    ("アムロジピン300mgを継続", "0.3g", None, False),
    ("アムロジピン5mgを継続", "50", None, False),
    ("アムロジピン5mgを継続", True, None, False),
    ("アムロジピン5mgを継続", {"value": 5}, None, False),
    ("HR50、アムロジピン5mgを継続", "50mg", None, False),
    ("アムロジピン300mg/日", "300mg", None, False),
    ("アムロジピン5mg、別薬剤50mg", "50mg", "アムロジピン5mgを継続", False),
    ("アムロジピン5mgを1日3回", "5mg", "アムロジピン5mgを1日2回", False),
    ("アムロジピン5mgは使用していない", "5mg", None, True),
    ("以前はアムロジピン5mg、現在50mg", "5mg", None, False),
    ("照射20Gy", "20g", None, False),
    ("アムロジピン300mg/5mL", "300mg", None, False),
])
def test_model_support_cannot_override_quantity_guard(quote, quantity, statement, accepted):
    doc = _doc(quote, quantity, statement=statement)
    if "使用していない" in quote:
        doc["facts"][0]["polarity"] = "negated"
    result = audit.audit_facts_v2(_PassJev(), doc, quote, time.monotonic() + 60)
    assert (result["status"] == "PASS") is accepted
    assert result["evaluated"]
    if not accepted:
        assert any(finding["code"].startswith("fact_quantity_") for finding in result["findings"])


def test_unknown_hint_and_nonmedication_quantity_keep_the_existing_contract():
    for doc in (_doc("アムロジピン", "unknown", statement="アムロジピン5mg"),
                _doc("収縮期血圧120", "120mmHg", kind="vital_lab")):
        assert audit._published_quantity_findings(doc) == []
    assert "quantity:5mg" in audit._fact_audit_target(_doc("5mg", "5mg")["facts"][0])


def test_other_fact_evidence_cannot_rescue_wrong_quantity():
    doc = _doc("アムロジピン5mgを継続", "50mg")
    doc["evidence"].append({**doc["evidence"][0], "evidence_id": "ev_other",
                            "quote": "別薬剤50mg"})
    doc["facts"].append(v2_fact("fact_other", statement="別薬剤50mg", quantity="unknown",
                               evidence_ids=["ev_other"]))
    findings = audit._published_quantity_findings(doc)
    assert findings and all(finding["fact"] == "fact_a" for finding in findings)
    doc["facts"][0]["evidence_ids"].append("ev_other")
    assert {item["code"] for item in audit._published_quantity_findings(doc)} == {
        "fact_quantity_relation_unverified"}


def test_multiple_own_quotes_with_the_same_amount_stay_unverified():
    doc = _doc("アムロジピン5mgを継続", "5mg")
    doc["evidence"].append({**doc["evidence"][0], "evidence_id": "ev_second"})
    doc["facts"][0]["evidence_ids"].append("ev_second")
    assert audit._published_quantity_findings(doc) == [
        {"code": "fact_quantity_relation_unverified", "fact": "fact_a"}]


def _stage(db, quantity):
    bundle = semantic.thread_bundle(db, 1, 1, [1])
    member = bundle["members"][0]
    scfg = semantic.semantic_config(_canonical_cfg())[0]
    policy = semantic.policy_fingerprint(scfg)

    def model(prompt):
        response = json.loads(_llm_v2(prompt))
        for fact in response.get("facts", []):
            fact.update(epistemic="reported", quantity=quantity)
        return json.dumps(response, ensure_ascii=False)

    stage = drain._fact_stage(db, scfg, member, 1, 1, bundle["source_fingerprint"], policy,
                             _PassJev(), model, time.monotonic() + 480)
    return stage, bundle, member, scfg, policy, model


@pytest.mark.parametrize("quantity,accepted", [("5mg", True), ("50mg", False)])
def test_fact_stage_blocks_wrong_quantity_before_current_projection_and_view(tmp_path, quantity, accepted):
    db = _seeded_two(tmp_path)
    try:
        stage, *_ = _stage(db, quantity)
        assert (stage["outcome"] is None) is accepted
        current = db.db.execute(
            "SELECT a.content FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
            f"WHERE a.kind IN ({FACT_KINDS_SQL}) AND m.message_id=1 {current_fact_pred()}").fetchone()
        assert bool(current) is accepted
        lines = structured_view.structured_lines(db.db, 1, drug_candidates=False)
        assert any("5mg" in line for line in lines) is accepted
        assert not any("50mg" in line for line in lines)
        if not accepted:
            assert stage["outcome"] == "hard_fail"
            assert any(finding["code"].startswith("fact_quantity_")
                       for finding in stage["findings"])
    finally:
        db.close()


def test_cached_pass_cannot_authorize_wrong_quantity_or_reprojection(tmp_path):
    db = _seeded_two(tmp_path)
    try:
        stage, bundle, member, scfg, policy, model = _stage(db, "5mg")
        assert stage["outcome"] is None
        doc = stage["v2_doc"]
        for fact in doc["facts"]:
            if fact["provenance"] == "local_llm":
                fact["quantity"] = "50mg"
        fingerprint = bundle["source_fingerprint"]
        doc_hash = v4._doc_hash(doc)
        db.artifact_add("semantic_facts_v2", json.dumps(doc), project_id=1, message_id=1,
                        meta={"fingerprint": fingerprint, "policy_fingerprint": policy,
                              "coverage_status": "complete"})
        db.artifact_add("semantic_facts_audit", json.dumps({"status": "PASS", "evaluated": True,
                        "findings": [], "fact_verdicts": {}}), project_id=1, message_id=1,
                        meta={"fingerprint": fingerprint, "policy_fingerprint": policy,
                              "doc_hash": doc_hash})
        assert v4.reproject_doc(db, 1, {"fingerprint": fingerprint, "policy_fingerprint": policy,
                                       "doc_hash": doc_hash}) == (None, "quantity_unverified")
        # The synthetic old PASS is present, but cannot mint another projection.
        count = len(db.artifacts("canonical_projection", message_id=1))

        def still_wrong(prompt):
            response = json.loads(model(prompt))
            for fact in response.get("facts", []):
                fact["quantity"] = "50mg"
            return json.dumps(response, ensure_ascii=False)

        result = drain._fact_stage(db, scfg, member, 1, 1, fingerprint, policy,
                                  _PassJev(), still_wrong, time.monotonic() + 480)
        assert result["outcome"] == "hard_fail"
        assert len(db.artifacts("canonical_projection", message_id=1)) == count
        assert not any("50mg" in line for line in structured_view.structured_lines(
            db.db, 1, drug_candidates=False))
    finally:
        db.close()


@pytest.mark.parametrize("kind", ["canonical_projection", "semantic_facts_v4"])
def test_existing_wrong_publication_is_held_before_and_after_reprojection(tmp_path, kind):
    db = _seeded_two(tmp_path)
    try:
        stage, bundle, member, scfg, policy, _model = _stage(db, "5mg")
        doc = stage["v2_doc"]
        for fact in doc["facts"]:
            if fact["provenance"] == "local_llm":
                fact["quantity"] = "50mg"
        fingerprint, doc_hash = bundle["source_fingerprint"], v4._doc_hash(doc)
        meta = {"fingerprint": fingerprint, "policy_fingerprint": policy,
                "doc_hash": doc_hash, "hash": member["revision"], "projection_version": 3}
        payload = json.dumps(projection.project_v2_doc_legacy(doc))
        db.artifact_add("semantic_facts_v2", json.dumps(doc), project_id=1, message_id=1,
                        meta={"fingerprint": fingerprint, "policy_fingerprint": policy,
                              "coverage_status": "complete"})
        db.artifact_add("semantic_facts_audit", json.dumps({"status": "PASS", "evaluated": True,
                        "findings": [], "fact_verdicts": {}}), project_id=1, message_id=1,
                        meta={"fingerprint": fingerprint, "policy_fingerprint": policy, "doc_hash": doc_hash})
        # Represent the proven old producer's persisted row. No history is deleted.
        with db.db:
            db.db.execute("UPDATE artifacts SET content=?,meta=? WHERE kind='canonical_projection' "
                          "AND message_id=1", (payload, json.dumps(meta)))
        if kind == "semantic_facts_v4":
            db.artifact_add(kind, payload, project_id=1, message_id=1,
                            meta={**meta, "engine_version": 4, "extract_version": 4})
        assert db.db.execute(
            "SELECT 1 FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
            f"WHERE a.kind IN ({FACT_KINDS_SQL}) AND m.message_id=1 {current_fact_pred()}").fetchone() is None
        assert not any("50mg" in line for line in structured_view.structured_lines(db.db, 1, drug_candidates=False))
        before = [(row["artifact_id"], row["content"]) for row in db.artifacts(kind, message_id=1)]
        out = v4.reproject_stale(db, scfg)
        assert out["skipped"] >= 1 and out["skip_reasons"]["quantity_unverified"] >= 1
        assert [(row["artifact_id"], row["content"]) for row in db.artifacts(kind, message_id=1)] == before
        assert not any("50mg" in line for line in structured_view.structured_lines(db.db, 1, drug_candidates=False))
        assert v4.reproject_stale(db, scfg)["skipped"] == 0
        with db.db:
            assert v4.publish(db, 1, 1, fingerprint, policy, member, doc) is None
    finally:
        db.close()


@pytest.mark.parametrize("quantity", ["5mg", "unknown"])
def test_valid_old_publication_regenerates_once_with_the_current_version(tmp_path, quantity):
    db = _seeded_two(tmp_path)
    try:
        stage, bundle, member, scfg, policy, _model = _stage(db, quantity)
        assert stage["outcome"] is None
        with db.db:
            v4.publish(db, 1, 1, bundle["source_fingerprint"], policy, member, stage["v2_doc"])
            db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.projection_version',3) "
                          "WHERE kind IN ('canonical_projection','semantic_facts_v4') AND message_id=1")
        assert not any("5mg" in line for line in structured_view.structured_lines(db.db, 1, drug_candidates=False))
        out = v4.reproject_stale(db, scfg, limit=1)
        assert out["reprojected"] == 1
        assert v4.reproject_stale(db, scfg)["reprojected"] == 1
        for kind in ("canonical_projection", "semantic_facts_v4"):
            rows = db.artifacts(kind, message_id=1)
            assert len(rows) == 2
            assert json.loads(rows[-1]["meta"])["projection_version"] == projection.PROJECTION_VERSION
            assert json.loads(rows[-1]["content"]) == projection.project_v2_doc_legacy(stage["v2_doc"])
        assert v4.reproject_stale(db, scfg)["reprojected"] == 0
        if quantity == "5mg":
            assert any("5mg" in line for line in structured_view.structured_lines(db.db, 1, drug_candidates=False))
    finally:
        db.close()
