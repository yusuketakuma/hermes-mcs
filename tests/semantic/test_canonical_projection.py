"""Canonical projection + consumer shadowing tests (T12).

``project_v2_doc_legacy`` renders an audited v2 doc into the legacy
extract_llm content shape — verified facts only, honestly lossy.
``current_fact_pred`` makes a hash-current canonical_projection row
shadow the message's extract_llm row for every read-side consumer.
"""
import json
import pytest

import semantic_projection as projection
import semantic_facts as sf
import mcs_requests as requests
from mcs_queries import current_fact_pred
from test_mcs_semantic import _seeded


def _fact(fid, kind="medication_event", verified=True, **kw):
    fact = {"fact_id": fid, "kind": kind, "subject": "patient:1",
            "actor": "sender:s1", "statement": "アムロジピン継続",
            "polarity": "affirmed", "epistemic": "asserted",
            "workflow_status": "performed", "event_time": "unknown",
            "valid_time": "unknown", "quantity": "unknown",
            "evidence_ids": ["ev_1"], "obligation_ids": [],
            "importance": "T1", "provenance": "local_llm",
            "validation_status": "verified" if verified else "unverified"}
    if kind == "medication_event":
        fact["action"] = "continue"
    fact.update(kw)
    return fact


EV = {"evidence_id": "ev_1", "message_id": "m1", "revision": "r1",
      "start": 0, "end": 9, "quote": "アムロジピン", "atom_id": "a1"}


def _doc(facts):
    return {"version": sf.CONTRACT_VERSION,
            "source": {"message_id": "m1", "revision": "r1",
                       "content_hash": "h", "body_codepoints": 20,
                       "content_quality": "full",
                       "attachments_complete": True,
                       "source_fingerprint": "sf_x"},
            "atoms": [], "chunks": [], "obligations": [],
            "evidence": [EV], "facts": list(facts), "relations": [],
            "coverage": {"category_counts": {},
                         "open_obligation_ids": [], "limitations": [],
                         "status": "complete"}}


def test_projection_maps_verified_medication_fact():
    doc = _doc([_fact("f1", action="stop",
                      workflow_status="performed", quantity="5mg",
                      statement="アムロジピン5mg中止")])
    out = projection.project_v2_doc_legacy(doc)
    assert len(out["meds"]) == 1
    med = out["meds"][0]
    assert med["action"] == "stop" and med["status"] == "past"
    assert med["subject"] == "patient" and not med["negated"]
    assert med["dose"] == "5mg"
    assert med["evidence"] == "アムロジピン"


def test_projection_skips_unverified_and_unmappable():
    doc = _doc([_fact("f1", verified=False),
                _fact("f2", kind="preference",
                      statement="訪問は午前希望"),
                _fact("f3", kind="symptom_state",
                      statement="疼痛あり",
                      workflow_status="reported")])
    out = projection.project_v2_doc_legacy(doc)
    assert "meds" not in out            # unverified fact never projects
    assert "requests" not in out        # preference has no legacy slot
    assert out["symptoms"][0]["status"] == "new"


def test_projection_subject_and_events():
    doc = _doc([_fact("f1", subject="role:family"),
                _fact("f2", kind="care_event",
                      statement="本日退院済み",
                      workflow_status="done")])
    out = projection.project_v2_doc_legacy(doc)
    assert out["meds"][0]["subject"] == "family"
    assert out["events"] == ["discharge"]


def _artifact(db, kind, mid, content, meta):
    project_id = db.execute(
        "SELECT project_id FROM messages WHERE message_id=?", (mid,)
    ).fetchone()[0]
    return db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
        "VALUES(?,?,?,?,?)",
        (kind, project_id, mid, json.dumps(content), json.dumps(meta))).lastrowid


@pytest.mark.parametrize("older_projection", [False, True])
@pytest.mark.parametrize("fault", [
    "invalid_json", "array", "null", "payload_error", "meta_error",
    "invalid_meta", "nonobject_meta", "invalidated", "invalidated_number",
    "invalidated_string", "old_hash", "wrong_project",
])
def test_projection_readers_agree_after_newer_unusable_row(
        tmp_path, older_projection, fault):
    """A failed projection cannot hide either usable canonical facts or legacy fallback."""
    db = _seeded(tmp_path)
    try:
        msg = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        meta = {"hash": msg["content_hash"]}
        content = {"requests": [{"action": "synthetic request", "to": None}]}
        selected = _artifact(db.db, "extract_llm", 1, content, meta)
        if older_projection:
            selected = _artifact(db.db, "canonical_projection", 1, content, meta)
        broken_content = {"array": [], "null": None,
                          "payload_error": {"_error": True}}.get(fault, content)
        broken_meta = {**meta, **{
            "meta_error": {"error": True}, "invalidated": {"invalidated": True},
            "invalidated_number": {"invalidated": 2},
            "invalidated_string": {"invalidated": "yes"},
            "old_hash": {"hash": "stale"},
        }.get(fault, {})}
        broken = _artifact(db.db, "canonical_projection", 1, broken_content, broken_meta)
        if fault == "invalid_json":
            db.db.execute("UPDATE artifacts SET content='{' WHERE artifact_id=?", (broken,))
        elif fault == "invalid_meta":
            db.db.execute("UPDATE artifacts SET meta='{' WHERE artifact_id=?", (broken,))
        elif fault == "nonobject_meta":
            db.db.execute("UPDATE artifacts SET meta='[]' WHERE artifact_id=?", (broken,))
        elif fault == "wrong_project":
            db.ensure_patient(2)
            db.db.execute("UPDATE artifacts SET project_id=2 WHERE artifact_id=?", (broken,))
        rows = db.db.execute(
            "SELECT a.artifact_id FROM artifacts a JOIN messages m "
            "ON m.message_id=a.message_id WHERE m.message_id=1 "
            "AND a.kind IN ('extract_llm','canonical_projection') "
            + current_fact_pred()).fetchall()
        suggestions = [item for item in requests.candidates(db.db, msg)
                       if item["extraction_kind"] != "extract_v1"]
        assert [row[0] for row in rows] == [selected]
        assert [item["artifact_id"] for item in suggestions] == [selected]
    finally:
        db.close()


@pytest.mark.parametrize("empty_content", [{}, {"requests": []}])
def test_projection_readers_keep_newest_empty_projection(tmp_path, empty_content):
    """An intentionally empty projection still replaces older requests and legacy facts."""
    db = _seeded(tmp_path)
    try:
        msg = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        meta = {"hash": msg["content_hash"]}
        content = {"requests": [{"action": "synthetic old request", "to": None}]}
        _artifact(db.db, "extract_llm", 1, content, meta)
        _artifact(db.db, "canonical_projection", 1, content, meta)
        selected = _artifact(db.db, "canonical_projection", 1, empty_content, meta)
        rows = db.db.execute(
            "SELECT a.artifact_id FROM artifacts a JOIN messages m "
            "ON m.message_id=a.message_id WHERE m.message_id=1 "
            "AND a.kind IN ('extract_llm','canonical_projection') "
            + current_fact_pred()).fetchall()
        assert [row[0] for row in rows] == [selected]
        assert [item for item in requests.candidates(db.db, msg)
                if item["extraction_kind"] != "extract_v1"] == []
    finally:
        db.close()


def test_current_fact_pred_prefers_canonical_projection(tmp_path):
    db = _seeded(tmp_path)
    try:
        hash_row = db.db.execute(
            "SELECT content_hash FROM messages WHERE message_id=1"
        ).fetchone()
        content_hash = hash_row["content_hash"]
        _artifact(db.db, "extract_llm", 1,
                  {"meds": [{"name": "旧薬", "action": "start"}]},
                  {"hash": content_hash})
        db.db.commit()

        sql = ("SELECT a.kind, a.content FROM artifacts a"
               " JOIN messages m ON m.message_id=a.message_id"
               " WHERE a.kind IN ('extract_llm','canonical_projection')"
               f" {current_fact_pred()} AND a.message_id=1")
        rows = db.db.execute(sql).fetchall()
        assert [r["kind"] for r in rows] == ["extract_llm"]

        # A hash-current canonical projection shadows the legacy row.
        _artifact(db.db, "canonical_projection", 1,
                  {"meds": [{"name": "新薬", "action": "stop"}]},
                  {"hash": content_hash})
        # A STALE canonical projection (old hash) must not shadow.
        _artifact(db.db, "canonical_projection", 2,
                  {"meds": [{"name": "古い", "action": "stop"}]},
                  {"hash": "outdated"})
        _artifact(db.db, "extract_llm", 2,
                  {"meds": [{"name": "旧2", "action": "start"}]},
                  {"hash": db.db.execute(
                      "SELECT content_hash FROM messages"
                      " WHERE message_id=2").fetchone()["content_hash"]})
        db.db.commit()
        rows = {r["message_id"]: r["kind"] for r in db.db.execute(
            sql.replace("a.message_id=1", "a.message_id IN (1,2)")
            .replace("a.kind, a.content",
                     "a.kind, a.content, a.message_id")).fetchall()}
        assert rows[1] == "canonical_projection"
        assert rows[2] == "extract_llm"
    finally:
        db.close()


def test_projection_carries_every_verified_fact_category():
    """GAP-2: categories with no legacy slot must not disappear —
    canonical_facts carries fact_id/kind/statement/evidence for every
    verified fact (allergy, adverse, vitals, preference, observation),
    with doc relations and the coverage quality state alongside."""
    doc = _doc([
        _fact("f_med", kind="medication_event", action="continue"),
        _fact("f_alg", kind="allergy_intolerance",
              statement="ペニシリンアレルギー"),
        _fact("f_ade", kind="adverse_drug_event",
              statement="嘔気（薬剤起因疑い）"),
        _fact("f_vit", kind="vital_lab", statement="BP 120/80"),
        _fact("f_pref", kind="preference", statement="午前訪問希望"),
        _fact("f_obs", kind="other_observation", statement="独居"),
    ])
    doc["relations"] = [
        {"left_fact_id": "f_med", "right_fact_id": "f_alg",
         "type": "CAUTION"},
        {"left_fact_id": "f_med", "right_fact_id": "f_ghost",
         "type": "DANGLING"},
    ]
    doc["coverage"] = {"category_counts": {},
                       "open_obligation_ids": ["ob_1"],
                       "limitations": ["家族分は未確認"],
                       "status": "partial"}
    out = projection.project_v2_doc_legacy(doc)
    ids = [f["fact_id"] for f in out["canonical_facts"]]
    assert ids == ["f_med", "f_alg", "f_ade", "f_vit", "f_pref", "f_obs"]
    by_id = {f["fact_id"]: f for f in out["canonical_facts"]}
    assert by_id["f_alg"]["kind"] == "allergy_intolerance"
    assert by_id["f_alg"]["evidence_quote"] == "アムロジピン"
    assert by_id["f_alg"]["evidence_ids"] == ["ev_1"]
    assert by_id["f_vit"]["importance"] == "T1"
    assert by_id["f_obs"]["subject"] == "patient:1"
    # only relations whose endpoints both survived projection
    assert out["canonical_relations"] == [
        {"left_fact_id": "f_med", "right_fact_id": "f_alg",
         "type": "CAUTION"}]
    q = out["canonical_quality"]
    assert q["coverage_status"] == "partial"
    assert q["open_obligation_ids"] == ["ob_1"]
    assert q["limitations"] == ["家族分は未確認"]
    assert q["source_fingerprint"] == "sf_x"


def test_projection_canonical_facts_exclude_unverified():
    """Unverified work never surfaces — not even in canonical_facts."""
    doc = _doc([_fact("f_bad", kind="allergy_intolerance",
                      statement="アレルギー", verified=False)])
    out = projection.project_v2_doc_legacy(doc)
    assert out["canonical_facts"] == []
    assert out["canonical_relations"] == []


def test_projection_normalizes_string_message_ids(tmp_path):
    """C01: v2 JSON carries message ids as strings; the projection must
    emit int ids so audit/bundle lookups (int-keyed) match."""
    ev = {"evidence_id": "ev_1", "message_id": "7", "revision": "r1",
          "start": 0, "end": 9, "quote": "アムロジピン", "atom_id": "a1"}
    doc = _doc([_fact("f1", evidence_ids=["ev_1"])])
    doc["source"]["message_id"] = "7"
    doc["evidence"] = [ev]
    out = projection.project_v2_facts(doc)
    assert out[0]["_evidence"]["message_id"] == 7
    assert isinstance(out[0]["_evidence"]["message_id"], int)


def test_care_event_typed_gates(tmp_path):
    """Only completed, certain patient events enter transition consumers."""
    for kw in ({"polarity": "negated"},
               {"polarity": "uncertain"},
               {"polarity": "unknown"},
               {"epistemic": "speculated"},
               {"epistemic": "unknown"},
               {"subject": "role:family"},
               {"workflow_status": "cancelled"},
               {"workflow_status": "on_hold"},
               {"workflow_status": "planned"},
               {"workflow_status": "considering"},
               {"workflow_status": "unknown"},
               {"subject": "person:uncle"}):
        doc = _doc([_fact("f1", kind="care_event",
                          statement="来週退院予定", **kw)])
        assert "events" not in projection.project_v2_doc_legacy(doc), kw
    for workflow in ("performed", "done"):
        doc = _doc([_fact("f1", kind="care_event",
                          statement="本日退院済み",
                          workflow_status=workflow)])
        assert projection.project_v2_doc_legacy(doc)["events"] == ["discharge"]


def test_projection_preserves_subject_and_uncertainty():
    doc = _doc([_fact("family", kind="symptom_state", subject="role:family"),
                _fact("uncertain", polarity="uncertain"),
                _fact("unknown", kind="symptom_state", epistemic="unknown"),
                _fact("patient", kind="symptom_state")])
    out = projection.project_v2_doc_legacy(doc)
    assert out["symptoms"][0]["subject"] == "family"
    assert out["symptoms"][1]["unverified"] is True
    assert out["meds"][0]["unverified"] is True
    assert out["symptoms"][2]["subject"] == "patient"
    assert not out["symptoms"][2].get("unverified")


def test_current_fact_pred_resolves_one_generation(tmp_path):
    """C06: among hash-current projections only THE newest row is
    current — a newer (even empty) projection supersedes older
    generations of the same body."""
    db = _seeded(tmp_path)
    try:
        content_hash = db.db.execute(
            "SELECT content_hash FROM messages WHERE message_id=1"
        ).fetchone()["content_hash"]
        _artifact(db.db, "extract_llm", 1,
                  {"meds": [{"name": "旧薬", "action": "start"}]},
                  {"hash": content_hash})
        # older generation: non-empty projection
        _artifact(db.db, "canonical_projection", 1,
                  {"meds": [{"name": "世代1", "action": "stop"}]},
                  {"hash": content_hash,
                   "policy_fingerprint": "gen1"})
        # newer generation of the same body: empty projection
        _artifact(db.db, "canonical_projection", 1,
                  {"meds": []},
                  {"hash": content_hash,
                   "policy_fingerprint": "gen2"})
        db.db.commit()
        sql = ("SELECT a.artifact_id, a.kind, a.content FROM artifacts a"
               " JOIN messages m ON m.message_id=a.message_id"
               " WHERE a.kind IN ('extract_llm','canonical_projection')"
               f" {current_fact_pred()} AND a.message_id=1")
        rows = db.db.execute(sql).fetchall()
        assert len(rows) == 1
        assert rows[0]["kind"] == "canonical_projection"
        assert json.loads(rows[0]["content"])["meds"] == []
        # the newest (empty) projection still shadows extract_llm
        assert not any(r["kind"] == "extract_llm" for r in rows)
    finally:
        db.close()


@pytest.mark.parametrize("change", ["context", "policy", "off", "legacy", "prompt"])
def test_projection_expiry_releases_legacy_and_invalidates_rollup(tmp_path, monkeypatch, change):
    import semantic
    import semantic_llm
    from semantic_store import invalidate_projections
    from test_mcs_semantic import _cfg
    db = _seeded(tmp_path)
    try:
        scfg, _ = semantic.semantic_config(_cfg(
            "shadow", fact_source="canonical", fact_source_gate="synthetic"))
        bundle = semantic.thread_bundle(db, 1, 1)
        source_hash = db.db.execute(
            "SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
        db.artifact_add("extract_llm", "{}", project_id=1, message_id=1,
                        meta={"hash": source_hash})
        projection_id = db.artifact_add(
            "canonical_projection", "{}", project_id=1, message_id=1,
            meta={"hash": source_hash, "fingerprint": bundle["source_fingerprint"],
                  "policy_fingerprint": semantic.policy_fingerprint(scfg)})
        db.artifact_add("patient_rollup", "{}", project_id=1)
        assert invalidate_projections(db, scfg) == 0
        if change == "context":
            db.db.execute("UPDATE messages SET body_text='別の合成文脈' WHERE message_id=2")
            db.db.commit()
        elif change == "policy":
            scfg["match_threshold"] = 0.99
        elif change == "off":
            scfg["mode"] = "off"
        elif change == "legacy":
            scfg["fact_source"] = "legacy"
        else:
            monkeypatch.setattr(semantic_llm, "_FACT_V2_PROMPT",
                                semantic_llm._FACT_V2_PROMPT + "\nnew contract")
        assert invalidate_projections(db, scfg) == 1
        meta = json.loads(db.db.execute(
            "SELECT meta FROM artifacts WHERE artifact_id=?", (projection_id,)).fetchone()[0])
        assert meta["invalidated"] is True and meta["hash"] == source_hash
        assert not db.artifacts("patient_rollup", project_id=1)
        kinds = [r[0] for r in db.db.execute(
            "SELECT a.kind FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
            "WHERE a.kind IN ('extract_llm','canonical_projection') AND m.message_id=1 "
            + current_fact_pred())]
        assert kinds == ["extract_llm"]
    finally:
        db.close()
