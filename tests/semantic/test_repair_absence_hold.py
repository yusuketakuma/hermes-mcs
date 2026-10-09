"""A repaired canonical document stored before repairs stopped turning an
adjudicated absence into coverage is held once that upgrade is proven by
the generation's repair receipt and its stored pre-repair document. A plain
repaired document, or one without a sound proof, is left as it is. No model,
Jev, re-queue or new repair. Synthetic ledger only."""
import json

import pytest

import request_loops
import semantic
import semantic_extraction as extraction
import semantic_facts as sf
import semantic_loops
import semantic_send_gate
import semantic_v4 as v4
from semantic_testkit import _cfg, _ledger, _message, _patient

NO = {c: "none" for c in sf.MANDATORY_CATEGORIES}
BODY = "アムロジピン5mgを継続。頭痛あり。"
MED = {"statement": "アムロジピン5mg継続", "kind": "medication_event", "action": "continue",
       "subject_role": "patient", "polarity": "affirmed", "workflow_status": "performed",
       "evidence_quote": "アムロジピン5mgを継続"}
SYMPTOM = {"statement": "頭痛あり", "kind": "symptom_state", "subject_role": "patient",
           "polarity": "affirmed", "workflow_status": "reported", "evidence_quote": "頭痛あり"}
SCHEDULE = {"fact_id": "fact_synthetic", "kind": "explicit_request", "statement": "受診予約",
            "polarity": "affirmed", "evidence_refs": [], "_evidence": None,
            "validation_status": "verified"}


def _model(facts, presence=None):
    return lambda prompt: json.dumps(
        {"facts": facts, "category_presence": dict(NO, **(presence or {}))},
        ensure_ascii=False)


def _config():
    return _cfg("enforce", fact_source="canonical", fact_source_gate="g6-v1:test")


def _scfg():
    return semantic.semantic_config(_config())[0]


class World:
    def __init__(self, tmp_path, upgraded=True, conflicting_base=False):
        self.db = db = _ledger(tmp_path)
        patient = _patient(db)
        patient.messages = [_message(1, body=BODY)]
        patient.messages[0].replies = [_message(2, parent=1, body="承知しました。")]
        db.save_patient(patient)
        self.policy = semantic.policy_fingerprint(_scfg())
        db.artifact_add("semantic_policy", self.policy)
        bundle = semantic.thread_bundle(db, 1, 1)
        self.fp = bundle["source_fingerprint"]
        member = next(m for m in bundle["members"] if m["message_id"] == 1)
        base = extraction.extract_facts_v2(_model([MED], {"medication": "one"}), member,
                                           source_fingerprint=self.fp, project_id=1)["doc"]
        med = next(f["fact_id"] for f in base["facts"] if f["kind"] == "medication_event")
        repaired = extraction.repair_facts_v2(
            _model([MED, SYMPTOM] if upgraded else [MED]), member, base, {med: "synthetic"})["doc"]
        if upgraded:
            # stored by the pre-fix rule: the absence became coverage
            for ob in repaired["obligations"]:
                if ob["status"] == "ambiguous" and ob["fact_ids"]:
                    ob["status"] = "covered"
            repaired["coverage"] = dict(repaired["coverage"], status="complete",
                                        open_obligation_ids=[])
        sf.validate_facts_doc(repaired)
        self.base_hash, self.repaired_hash = v4._doc_hash(base), v4._doc_hash(repaired)
        meta = {"fingerprint": self.fp, "policy_fingerprint": self.policy}
        self.base_id = self._add("semantic_facts_v2", base, meta)
        if conflicting_base:
            # same facts and evidence (same doc_hash), different statuses
            other = json.loads(json.dumps(base))
            for ob in other["obligations"]:
                if ob["status"] == "explicit_no_fact":
                    ob["status"] = "ambiguous"
            self._add("semantic_facts_v2", other, meta)
        self._add("semantic_facts_repair", {"status": "started"}, dict(meta, doc_hash=self.base_hash))
        self._add("semantic_facts_repair", {"repaired": True, "repaired_fact_ids": []},
                  dict(meta, doc_hash=self.base_hash, repaired=True))
        self.repaired_id = self._add("semantic_facts_v2", repaired,
                                     dict(meta, repaired=True, coverage_status="complete"))
        content_hash = db.db.execute("SELECT content_hash FROM messages "
                                     "WHERE message_id=1").fetchone()[0]
        pub = dict(meta, hash=content_hash, doc_hash=self.repaired_hash, schema=2,
                   extract_version=4, engine_version=4, fact_source="canonical",
                   projection_version=v4_projection_version())
        for kind in (v4.KIND_V4, "canonical_projection"):
            self._add(kind, {"meds": [{"name": "アムロジピン"}]}, pub)
        self._add("semantic_summary", {"claims": []},
                  dict(meta, audit_status="PASS", publication_mode="enforce",
                       target_revision=content_hash))
        start = BODY.index("頭痛あり")
        evidence = {"evidence_id": "ev_synthetic", "message_id": 1,
                    "revision_id": member["revision"], "start_codepoint": start,
                    "end_codepoint": start + 4, "quote": "頭痛あり"}
        loop_fact = dict(SCHEDULE, evidence_refs=["ev_synthetic"], _evidence=evidence)
        semantic_loops.update_loops(db, 1, bundle, {1: [loop_fact]}, None, _scfg(), 1e18)
        # bystanders: the same message's other generation and the other message
        self._add(v4.KIND_V4, {}, {"fingerprint": "f" * 64, "doc_hash": "e" * 64})
        self._add(v4.KIND_V4, {}, dict(meta, doc_hash="d" * 64), mid=2)

    def _add(self, kind, content, meta, mid=1):
        self.db.artifact_add(kind, json.dumps(content, ensure_ascii=False), project_id=1,
                             message_id=mid, meta=meta)
        return self.db.db.execute("SELECT MAX(artifact_id) FROM artifacts").fetchone()[0]

    def meta(self, artifact_id):
        return json.loads(self.db.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?",
                                             (artifact_id,)).fetchone()[0])

    def rows(self, kind, mid=1):
        return [(r[0], json.loads(r[1])) for r in self.db.db.execute(
            "SELECT artifact_id,meta FROM artifacts WHERE kind=? AND message_id=? "
            "ORDER BY artifact_id", (kind, mid))]

    def protected(self):
        return self.db.db.execute(
            "SELECT artifact_id,content,meta FROM artifacts WHERE kind IN "
            "('semantic_facts_repair') OR artifact_id=? "
            "OR json_extract(meta,'$.fingerprint')=? OR message_id=2 "
            "ORDER BY artifact_id", (self.base_id, "f" * 64)).fetchall(), \
            self.db.db.execute("SELECT message_id,body_text,content_hash FROM messages").fetchall(), \
            self.db.db.execute("SELECT count(*) FROM requests").fetchone()[0], \
            self.db.db.execute("SELECT content FROM artifacts WHERE artifact_id=?",
                               (self.repaired_id,)).fetchone()[0]


def v4_projection_version():
    from semantic_projection import PROJECTION_VERSION
    return PROJECTION_VERSION


def _current_ids(db):
    from mcs_queries import current_projection_id, current_v4_id
    return tuple(db.db.execute(
        f"SELECT {current_projection_id('m')},{current_v4_id('m')} "
        "FROM messages m WHERE message_id=1").fetchone())


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.db.close()


def test_proven_upgrade_holds_the_generation_once(world):
    db = world.db
    assert None not in _current_ids(db)
    loop_id = world.rows("loop_candidate")[0][0]
    assert request_loops.current_candidate(db.db, 1, loop_id, 1)["artifact_id"] == loop_id
    before = world.protected()
    jobs = db.db.execute("SELECT count(*) FROM fetch_jobs").fetchone()[0]
    out = v4.reproject_stale(db, _scfg())
    assert out["skip_reasons"]["repair_absence:held"] == 1
    meta = world.meta(world.repaired_id)
    assert meta["repair_checked"] == "held"
    assert meta["hint_rebuild"] == "skipped:repair_absence"
    assert _current_ids(db) == (None, None)
    for kind in (v4.KIND_V4, "canonical_projection", "semantic_summary", "loop_candidate"):
        assert all(m.get("invalidated") is True for _, m in world.rows(kind)
                   if m.get("fingerprint") == world.fp), kind
    bundle = semantic.thread_bundle(db, 1, 1)
    assert semantic_send_gate._summary_current(db, 1, 1, bundle, world.policy) is None
    with pytest.raises(ValueError, match="loop_candidate_stale"):
        request_loops.current_candidate(db.db, 1, loop_id, 1)
    assert world.protected() == before          # receipts, base doc, bystanders, body
    assert db.db.execute("SELECT count(*) FROM fetch_jobs").fetchone()[0] == jobs
    rows = db.db.execute("SELECT artifact_id,meta FROM artifacts ORDER BY artifact_id").fetchall()
    assert "repair_absence:held" not in v4.reproject_stale(db, _scfg()).get("skip_reasons", {})
    assert db.db.execute("SELECT artifact_id,meta FROM artifacts "
                         "ORDER BY artifact_id").fetchall() == rows


def test_manual_retry_never_reuses_the_held_document_or_repairs_again(world):
    from semantic_testkit import _PassJev
    db = world.db
    v4.reproject_stale(db, _scfg())
    receipts = db.db.execute("SELECT count(*) FROM artifacts "
                             "WHERE kind='semantic_facts_repair'").fetchone()[0]
    extractions = []

    def llm(prompt):
        if "要約器" in prompt:
            return json.dumps({"claims": [], "limitations": []})
        extractions.append(1)
        return _model([MED, SYMPTOM], {"medication": "one",
                                       "symptom_state": "one"})(prompt)
    db.semantic_seed(1, [1], {"source": "manual_retry", "notification_free": True})
    semantic.run_due(db, _config(), {"errors": []}, 1e18, jev_client=_PassJev(), llm_fn=llm)
    assert extractions                                    # re-extracted, not reused
    newest = world.rows("semantic_facts_v2")[-1]
    assert newest[0] != world.repaired_id
    assert db.db.execute("SELECT count(*) FROM artifacts "
                         "WHERE kind='semantic_facts_repair'").fetchone()[0] == receipts


def test_plain_repaired_document_is_left_as_it_is(tmp_path):
    w = World(tmp_path, upgraded=False)
    try:
        out = v4.reproject_stale(w.db, _scfg())
        assert out["skip_reasons"]["repair_absence:clear"] == 1
        assert w.meta(w.repaired_id)["repair_checked"] == "clear"
        assert "hint_rebuild" not in w.meta(w.repaired_id)
        assert None not in _current_ids(w.db)
    finally:
        w.db.close()


@pytest.mark.parametrize("break_proof,why", [
    ("DELETE FROM artifacts WHERE kind='semantic_facts_repair'", "receipt"),
    ("UPDATE artifacts SET meta=json_set(meta,'$.doc_hash','0') "
     "WHERE kind='semantic_facts_repair' AND json_extract(meta,'$.repaired') IS 1", "base_document"),
])
def test_missing_or_broken_proof_is_never_asserted(world, break_proof, why):
    db = world.db
    db.db.execute(break_proof)
    db.db.commit()
    out = v4.reproject_stale(db, _scfg())
    assert out["skip_reasons"][f"repair_absence:unproven:{why}"] == 1
    assert world.meta(world.repaired_id)["repair_checked"] == f"unproven:{why}"
    assert None not in _current_ids(db)


def test_contradicting_pre_repair_documents_are_not_chosen_from(tmp_path):
    w = World(tmp_path, conflicting_base=True)
    try:
        out = v4.reproject_stale(w.db, _scfg())
        assert out["skip_reasons"]["repair_absence:unproven:base_document"] == 1
        assert None not in _current_ids(w.db)
    finally:
        w.db.close()


@pytest.mark.parametrize("fact_source", ["shadow", "legacy"])
def test_non_canonical_fact_source_changes_nothing(world, fact_source):
    db = world.db
    rows = db.db.execute("SELECT artifact_id,meta FROM artifacts ORDER BY artifact_id").fetchall()
    scfg = semantic.semantic_config(_cfg("enforce", fact_source=fact_source))[0]
    v4.reproject_stale(db, scfg)
    assert db.db.execute("SELECT artifact_id,meta FROM artifacts "
                         "ORDER BY artifact_id").fetchall() == rows


def test_the_check_is_bounded(world):
    out = v4.reproject_stale(world.db, _scfg(), limit=0)
    assert "repair_checked" not in world.meta(world.repaired_id)
    assert out.get("skip_reasons", {}) == {}


@pytest.mark.parametrize("target,path,value,why", [
    # the repaired document breaks the canonical contract
    ("repaired", "$.coverage.open_obligation_ids", '["obl_unknown"]', "repaired_document"),
    ("repaired", "$.obligations[0].status", '"bogus"', "repaired_document"),
    # the pre-repair document breaks it (same doc_hash: facts/evidence untouched)
    ("base", "$.obligations[0].status", '"bogus"', "base_document"),
    ("base", "$.coverage.status", '"finished"', "base_document"),
    # either is bound to another message, revision or generation
    ("base", "$.source.source_fingerprint", '"sf_other"', "base_document"),
    ("base", "$.source.revision", '"other-revision"', "base_document"),
    ("repaired", "$.source.message_id", '"2"', "repaired_document"),
    ("repaired", "$.source.source_fingerprint", '"sf_other"', "repaired_document"),
])
def test_an_invalid_or_unbound_document_is_never_proof(world, target, path, value, why):
    db = world.db
    artifact = world.repaired_id if target == "repaired" else world.base_id
    db.db.execute("UPDATE artifacts SET content=json_set(content,?,json(?)) "
                  "WHERE artifact_id=?", (path, value, artifact))
    db.db.commit()
    before = db.db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE "
                           "artifact_id!=? ORDER BY artifact_id", (world.repaired_id,)).fetchall()
    out = v4.reproject_stale(db, _scfg())
    assert out["skip_reasons"][f"repair_absence:unproven:{why}"] == 1
    assert "hint_rebuild" not in world.meta(world.repaired_id)
    assert None not in _current_ids(db)
    assert db.db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE "
                         "artifact_id!=? ORDER BY artifact_id",
                         (world.repaired_id,)).fetchall() == before


def test_an_invalid_candidate_among_same_hash_bases_makes_the_proof_unknown(tmp_path):
    w = World(tmp_path, conflicting_base=True)
    try:
        # make the copy agree with the first base but break its contract
        rows = w.rows("semantic_facts_v2")
        copy_id = rows[1][0]
        base = w.db.db.execute("SELECT content FROM artifacts WHERE artifact_id=?",
                               (w.base_id,)).fetchone()[0]
        w.db.db.execute("UPDATE artifacts SET content=json_set(?,'$.coverage.status','x') "
                        "WHERE artifact_id=?", (base, copy_id))
        w.db.db.commit()
        out = v4.reproject_stale(w.db, _scfg())
        assert out["skip_reasons"]["repair_absence:unproven:base_document"] == 1
        assert None not in _current_ids(w.db)
    finally:
        w.db.close()
