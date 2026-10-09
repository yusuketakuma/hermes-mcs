"""Canonical projection + consumer shadowing tests (T12).

``project_v2_doc_legacy`` renders an audited v2 doc into the legacy
extract_llm content shape — verified facts only, honestly lossy.
``current_fact_pred`` makes a hash-current canonical_projection row
shadow the message's extract_llm row for every read-side consumer.
"""
import json
import pytest

import semantic_projection as projection
import mcs_requests as requests
from mcs_queries import current_fact_pred
from semantic_testkit import (EV, _drained_old_version, _kind_rows, _seeded,
                              v2_doc, v2_fact)


def _fact(fid, kind="medication_event", verified=True, **kw):
    base = {"kind": kind, "quantity": "unknown", "evidence_ids": ["ev_1"],
            "validation_status": "verified" if verified else "unverified"}
    if kind == "medication_event":
        base["action"] = "continue"
    return v2_fact(fid, **{**base, **kw})


def _doc(facts):
    return v2_doc(facts, [EV])


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
    if kind in ("canonical_projection", "semantic_facts_v4"):
        meta = {"projection_version": projection.PROJECTION_VERSION, **meta}
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
            # Plant old damage for reader-defense assertions, not a valid write.
            db.db.execute("DROP TRIGGER g1_artifacts_msg_upd")
            assert db.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='g1_artifacts_msg_ins'").fetchone()
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


def _med_bucket(med):
    from rollup import _med_states
    state = {}
    _med_states({"posted_at": "2026-09-01T00:00:00+09:00"}, {},
                {"meds": [dict(med, name="アムロジピン")]}, state)
    return state["アムロジピン"][0]


def _disputed_doc(rel_type):
    doc = _doc([_fact("f1"), _fact("f2", statement="アムロジピン中止",
                                   action="stop")])
    doc["relations"] = [{"left_fact_id": "f1", "right_fact_id": "f2",
                         "type": rel_type}]
    return doc


@pytest.mark.parametrize("doc", [
    # adherence reports are observations, not current prescriptions
    _doc([_fact("f1", kind="adherence_administration",
                statement="アムロジピン服用済み")]),
    # cancelled workflow is past whatever the action says
    _doc([_fact("f1", action="continue", workflow_status="cancelled")]),
    _doc([_fact("f1", action="cancelled", workflow_status="done")]),
    # no stated / only held or considered action is not confirmed
    _doc([_fact("f1", action="unknown")]),
    _doc([_fact("f1", action="hold")]),
    _doc([_fact("f1", action="consider")]),
    _doc([{k: v for k, v in _fact("f1").items() if k != "action"}]),
    # open conflicts are retained, never asserted
    _disputed_doc("CONTRADICTION"),
    _disputed_doc("UNRESOLVED"),
    # a fact whose evidence id binds no stored quote
    _doc([_fact("f1", evidence_ids=["ev_missing"])]),
], ids=["adherence", "cancelled_workflow", "cancelled_done",
        "action_unknown", "action_hold", "action_consider",
        "action_absent", "contradiction", "unresolved", "no_quote"])
def test_projection_never_asserts_unconfirmed_current_medication(doc):
    from mcs_queries import med_is_patient_current
    out = projection.project_v2_doc_legacy(doc)
    for med in out.get("meds", []):
        assert not med_is_patient_current(med), med
        assert _med_bucket(med) != "current", med
    # the fact itself is never lost — it stays a canonical fact
    assert {f["fact_id"] for f in doc["facts"]} \
        == {f["fact_id"] for f in out["canonical_facts"]}


def test_projection_adherence_and_cancel_semantics():
    out = projection.project_v2_doc_legacy(_doc([
        _fact("adh", kind="adherence_administration")]))
    assert "meds" not in out
    assert out["canonical_facts"][0]["kind"] == "adherence_administration"
    assert projection.project_v2_facts(_doc([
        _fact("adh", kind="adherence_administration")]))[0]["kind"] \
        == "observation"
    # operator precedence: a stop that is only planned is not past
    planned_stop = projection.project_v2_doc_legacy(_doc([
        _fact("f1", action="stop", workflow_status="planned")]))
    assert planned_stop["meds"][0]["status"] == "planned"


def test_projection_confirmed_medication_stays_current():
    from mcs_queries import med_is_patient_current
    med = projection.project_v2_doc_legacy(_doc([_fact("f1")]))["meds"][0]
    assert med_is_patient_current(med) and _med_bucket(med) == "current"


def test_projection_disputed_or_unbound_care_event_is_not_a_transition():
    doc = _doc([_fact("f1", kind="care_event", statement="本日退院済み"),
                _fact("f2", kind="care_event", statement="本日退院済み")])
    doc["relations"] = [{"left_fact_id": "f1", "right_fact_id": "f2",
                         "type": "CONTRADICTION"}]
    assert "events" not in projection.project_v2_doc_legacy(doc)
    doc = _doc([_fact("f1", kind="care_event", statement="本日退院済み",
                      evidence_ids=[])])
    assert "events" not in projection.project_v2_doc_legacy(doc)


@pytest.mark.parametrize("kw,unverified", [
    ({}, False),
    ({"polarity": "negated"}, True),
    ({"polarity": "uncertain"}, True),
    ({"epistemic": "speculated"}, True),
    ({"evidence_ids": []}, True),
])
def test_projection_request_keeps_full_statement(kw, unverified):
    statement = "来週の往診までに残薬を数えて薬局へ報告してください"
    out = projection.project_v2_doc_legacy(_doc([
        _fact("r1", kind="request_pending", statement=statement,
              workflow_status="pending", **kw)]))
    request = out["requests"][0]
    assert request["action"] == statement      # never cut at 15 chars
    assert request["unverified"] is unverified


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
    from semantic_testkit import _cfg
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


def test_old_projection_version_rows_are_superseded_on_rerun(tmp_path):
    """A projection row minted by an older PROJECTION_VERSION for the
    same fingerprint/doc_hash is stale semantics: reprocessing writes a
    fresh row (the newest wins for readers) instead of reusing it, and
    a same-version rerun still reuses."""
    import time
    import semantic
    import semantic_drain as drain
    import semantic_v4 as v4
    from mcs_queries import current_projection_id, current_v4_id
    from semantic_policy import policy_fingerprint, semantic_config
    from semantic_testkit import _canonical_cfg, _llm_v2, _seeded_two
    from semantic_testkit import _PassJev

    def rows(kind):
        return db.db.execute(
            "SELECT artifact_id,meta FROM artifacts WHERE kind=? "
            "AND message_id=1 ORDER BY artifact_id", (kind,)).fetchall()

    def current(sub):
        return db.db.execute(
            f"SELECT {sub} FROM messages m WHERE m.message_id=1"
        ).fetchone()[0]

    db = _seeded_two(tmp_path)
    try:
        semantic.run_due(db, _canonical_cfg(), {"errors": []},
                         time.monotonic() + 300, jev_client=_PassJev(),
                         llm_fn=_llm_v2)
        for kind in ("canonical_projection", v4.KIND_V4):
            assert [json.loads(r["meta"])["projection_version"]
                    for r in rows(kind)] == [projection.PROJECTION_VERSION]
            # simulate a row minted before the projection changed
            db.db.execute(
                "UPDATE artifacts SET meta=json_set(meta,"
                "'$.projection_version',?) WHERE kind=? AND message_id=1",
                (projection.PROJECTION_VERSION - 1, kind))
        db.db.commit()
        old_proj = rows("canonical_projection")[0]["artifact_id"]
        old_v4 = rows(v4.KIND_V4)[0]["artifact_id"]
        assert current(current_projection_id()) is None
        assert current(current_projection_id(require_version=False)) == old_proj
        scfg = semantic_config(_canonical_cfg())[0]
        bundle = semantic.thread_bundle(db, 1, 1)
        member = next(m for m in bundle["members"] if m["message_id"] == 1)
        fp, policy = bundle["source_fingerprint"], policy_fingerprint(scfg)

        def rerun():
            stage = drain._fact_stage(db, scfg, member, 1, 1, fp, policy,
                                      _PassJev(), _llm_v2,
                                      time.monotonic() + 300)
            assert stage["outcome"] is None
            with db.db:
                return v4.publish(db, 1, 1, fp, policy, member,
                                  stage["v2_doc"])

        new_v4 = rerun()
        proj_rows = rows("canonical_projection")
        assert len(proj_rows) == 2
        assert json.loads(proj_rows[-1]["meta"])["projection_version"] \
            == projection.PROJECTION_VERSION
        assert json.loads(proj_rows[-1]["meta"])["doc_hash"] \
            == json.loads(proj_rows[0]["meta"])["doc_hash"]
        assert current(current_projection_id()) == proj_rows[-1]["artifact_id"]
        assert new_v4 != old_v4 and current(current_v4_id()) == new_v4
        assert json.loads(rows(v4.KIND_V4)[-1]["meta"])[
            "projection_version"] == projection.PROJECTION_VERSION
        # same version + same document -> reuse, no row churn
        assert rerun() == new_v4
        assert len(rows("canonical_projection")) == 2
        assert len(rows(v4.KIND_V4)) == 2
    finally:
        db.close()


def _current_ids(db, mid):
    from mcs_queries import current_projection_id, current_v4_id
    return db.db.execute(
        f"SELECT {current_projection_id()}, {current_v4_id()} "
        "FROM messages m WHERE m.message_id=?", (mid,)).fetchone()


def test_reproject_supersedes_old_version_rows_bounded_and_idempotent(
        tmp_path):
    import semantic_v4 as v4
    from semantic_policy import KIND_FACTS_V2, semantic_config
    from semantic_testkit import _canonical_cfg
    db = _drained_old_version(tmp_path)
    try:
        scfg = semantic_config(_canonical_cfg())[0]
        old = {r["artifact_id"]: r for kind in
               ("canonical_projection", v4.KIND_V4)
               for r in _kind_rows(db, kind)}
        # the bound is respected: one row per call
        assert v4.reproject_stale(db, scfg, limit=1)["reprojected"] == 1
        out = v4.reproject_stale(db, scfg)
        assert out == {"reprojected": 3, "skipped": 0, "skip_reasons": {}}
        for mid in (1, 2):
            doc = json.loads(_kind_rows(db, KIND_FACTS_V2, mid)[-1]
                             ["content"])
            expected = projection.project_v2_doc_legacy(doc)
            assert expected != {"meds": [{"name": "OLD"}]}
            for kind, cur in zip(("canonical_projection", v4.KIND_V4),
                                 _current_ids(db, mid)):
                rows = _kind_rows(db, kind, mid)
                assert len(rows) == 2 and cur == rows[-1]["artifact_id"]
                new_meta = json.loads(rows[-1]["meta"])
                prev = old[rows[0]["artifact_id"]]
                assert json.loads(rows[-1]["content"]) == expected
                assert rows[-1]["model"] == prev["model"]
                assert new_meta.pop("projection_version") \
                    == projection.PROJECTION_VERSION
                assert new_meta.pop("reprojected_from") \
                    == rows[0]["artifact_id"]
                # binding meta is carried verbatim
                assert new_meta == json.loads(prev["meta"])
            # every reader now sees the new semantics (v4 outranks)
            read = db.db.execute(
                "SELECT a.kind,a.content FROM artifacts a JOIN messages m "
                "ON m.message_id=a.message_id WHERE m.message_id=? AND "
                "a.kind IN ('extract_llm','canonical_projection',"
                "'semantic_facts_v4')" + current_fact_pred(),
                (mid,)).fetchall()
            assert [(r["kind"], json.loads(r["content"])) for r in read] \
                == [(v4.KIND_V4, expected)]
        # the patient's rollup is rebuilt from the new rows
        assert not db.artifacts("patient_rollup", project_id=1)
        # second pass is a no-op: no rows written
        total = db.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        assert v4.reproject_stale(db, scfg)["reprojected"] == 0
        assert db.db.execute(
            "SELECT COUNT(*) FROM artifacts").fetchone()[0] == total
    finally:
        db.close()


def test_reproject_never_resurrects_or_guesses(tmp_path):
    """Invalidated, deleted-body and non-derivable rows are left alone;
    the non-derivable ones are counted and marked so they cannot starve
    the per-tick bound."""
    import semantic_v4 as v4
    from semantic_policy import KIND_FACT_AUDIT, KIND_FACTS_V2, \
        semantic_config
    from semantic_testkit import _canonical_cfg
    db = _drained_old_version(tmp_path)
    try:
        scfg = semantic_config(_canonical_cfg())[0]
        # message 1: rows invalidated (a later source/policy change)
        db.db.execute(
            "UPDATE artifacts SET meta=json_set(meta,'$.invalidated',"
            "json('true')) WHERE message_id=1 AND kind IN (?,?)",
            ("canonical_projection", v4.KIND_V4))
        # message 2: stored audit no longer PASS for the bound document
        audit = _kind_rows(db, KIND_FACT_AUDIT, 2)[-1]
        db.db.execute(
            "UPDATE artifacts SET content=json_set(content,'$.status',"
            "'NEEDS_REVIEW') WHERE artifact_id=?", (audit["artifact_id"],))
        db.db.commit()
        before = db.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        out = v4.reproject_stale(db, scfg)
        assert out == {"reprojected": 0, "skipped": 2,
                       "skip_reasons": {"audit_not_pass": 2}}
        assert db.db.execute(
            "SELECT COUNT(*) FROM artifacts").fetchone()[0] == before
        # marked rows are not re-selected; old rows keep serving
        assert v4.reproject_stale(db, scfg)["skipped"] == 0
        assert all(json.loads(r["content"]) == {"meds": [{"name": "OLD"}]}
                   for kind in ("canonical_projection", v4.KIND_V4)
                   for r in _kind_rows(db, kind))
        # the audited doc itself replaced by another generation's doc
        db.db.execute(
            "UPDATE artifacts SET meta=json_remove(meta,"
            "'$.reproject_skipped') WHERE message_id=2")
        db.db.execute(
            "UPDATE artifacts SET content=json_set(content,'$.status',"
            "'PASS') WHERE artifact_id=?", (audit["artifact_id"],))
        v2 = _kind_rows(db, KIND_FACTS_V2, 2)[-1]
        doc = json.loads(v2["content"])
        doc["facts"] = []
        db.artifact_add(KIND_FACTS_V2, json.dumps(doc), project_id=1,
                        message_id=2, meta=json.loads(v2["meta"]))
        out = v4.reproject_stale(db, scfg)
        assert out["skip_reasons"] == {"doc_unavailable": 2}
        # a deleted body is never re-projected
        db.db.execute(
            "UPDATE artifacts SET meta=json_remove(meta,"
            "'$.reproject_skipped') WHERE message_id=2")
        db.db.execute(
            "DELETE FROM artifacts WHERE artifact_id=(SELECT MAX("
            "artifact_id) FROM artifacts WHERE kind=?)", (KIND_FACTS_V2,))
        db.db.execute(
            "UPDATE messages SET body_state='deleted' WHERE message_id=2")
        db.db.commit()
        assert v4.reproject_stale(db, scfg)["reprojected"] == 0
        # mode off never writes; rows left from a canonical period are
        # still brought forward after a switch back to legacy
        db.db.execute(
            "UPDATE messages SET body_state='full' WHERE message_id=2")
        db.db.commit()
        assert v4.reproject_stale(
            db, dict(scfg, mode="off"))["reprojected"] == 0
        assert v4.reproject_stale(
            db, dict(scfg, fact_source="legacy"))["reprojected"] == 2
        assert v4.reproject_stale(
            db, dict(scfg, fact_source="shadow"))["reprojected"] == 0
    finally:
        db.close()


def test_run_due_reprojects_old_version_rows(tmp_path):
    import time
    import semantic
    import semantic_v4 as v4
    from semantic_testkit import _canonical_cfg

    def no_llm(prompt):
        raise AssertionError("re-projection must not call the model")

    db = _drained_old_version(tmp_path)
    try:
        result = {"errors": []}
        out = semantic.run_due(db, _canonical_cfg(), result,
                               time.monotonic() + 300, llm_fn=no_llm)
        assert result["errors"] == []
        assert out["reproject"]["reprojected"] == 4
        for kind in ("canonical_projection", v4.KIND_V4):
            assert all(json.loads(r["meta"]).get("projection_version")
                       == projection.PROJECTION_VERSION
                       for r in _kind_rows(db, kind)[-2:])
    finally:
        db.close()


@pytest.mark.parametrize("fact_source", ["legacy", "shadow"])
@pytest.mark.parametrize("change", [
    "none", "context", "policy", "unknown", "superseded", "audit", "payload_error",
])
def test_run_due_switch_hides_then_revives_only_current_projections(
        tmp_path, monkeypatch, fact_source, change):
    import semantic
    import semantic_v4 as v4
    from semantic_testkit import _canonical_cfg
    from semantic_testkit import _PassJev

    db = _drained_old_version(tmp_path)
    try:
        def read_current():
            return [tuple(r) for r in db.db.execute(
                "SELECT a.message_id,a.kind,a.artifact_id FROM artifacts a "
                "JOIN messages m ON m.message_id=a.message_id "
                "WHERE a.kind IN ('extract_llm','canonical_projection',"
                "'semantic_facts_v4')" + current_fact_pred()
                + " ORDER BY a.message_id")]

        def no_processing(*args, **kwargs):
            raise AssertionError("configuration switches must not reprocess")

        def tick():
            result = {"errors": []}
            out = semantic.run_due(db, cfg, result, float("inf"),
                                   llm_fn=no_processing, jev_client=_PassJev())
            assert result["errors"] == []
            assert out["done"] == out["failed"] == out["deferred"] == 0
            return out

        cfg = _canonical_cfg()
        cfg["semantic"].update(mode="shadow", fact_source=fact_source)
        kinds = ("canonical_projection", v4.KIND_V4)
        old_ids = [r["artifact_id"] for kind in kinds
                   for r in _kind_rows(db, kind, 1)]
        for mid in (1, 2):
            source_hash = db.db.execute(
                "SELECT content_hash FROM messages WHERE message_id=?",
                (mid,)).fetchone()[0]
            db.artifact_add("extract_llm", "{}", project_id=1, message_id=mid,
                            meta={"hash": source_hash})
        if change == "unknown":
            db.db.execute(
                "UPDATE artifacts SET meta=json_set(meta,'$.invalidated',"
                "json('true')) WHERE message_id=1 AND kind IN (?,?)", kinds)
            db.db.commit()
        elif change == "superseded":
            for kind in kinds:
                row = _kind_rows(db, kind, 1)[-1]
                db.artifact_add(kind, row["content"], project_id=1, message_id=1,
                                meta=json.loads(row["meta"]), model=row["model"])
        monkeypatch.setattr(semantic, "_process_job", no_processing)
        assert tick()["reproject"]["reprojected"] == 0
        assert [(r[0], r[1]) for r in read_current()] == [
            (1, "extract_llm"), (2, "extract_llm")]
        hidden_count = db.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        assert not db.artifacts("patient_rollup", project_id=1)

        # Change the source/policy while canonical readers are disabled.
        if change == "context":
            db.db.execute(
                "UPDATE messages SET body_text='changed synthetic context' "
                "WHERE message_id=2")
            db.db.commit()
        elif change == "policy":
            cfg["semantic"]["match_threshold"] = 0.99
        elif change == "audit":
            db.db.execute(
                "UPDATE artifacts SET content=json_set(content,'$.status',"
                "'NEEDS_REVIEW') WHERE message_id=1 "
                "AND kind='semantic_facts_audit'")
            db.db.commit()
        elif change == "payload_error":
            db.db.execute(
                "UPDATE artifacts SET content=json_set(content,'$._error',"
                "json('true')) WHERE message_id=1 AND kind IN (?,?)", kinds)
            db.db.commit()
        assert tick()["reproject"]["reprojected"] == 0
        assert [(r[0], r[1]) for r in read_current()] == [
            (1, "extract_llm"), (2, "extract_llm")]
        if change != "policy":
            assert db.db.execute(
                "SELECT COUNT(*) FROM artifacts").fetchone()[0] == hidden_count

        cfg["semantic"]["fact_source"] = "canonical"
        out = tick()
        revived_mids = ({1, 2} if change in ("none", "superseded") else
                        set() if change in ("context", "policy") else {2})
        current = read_current()
        assert [(r[0], r[1]) for r in current] == [
            (mid, v4.KIND_V4 if mid in revived_mids else "extract_llm")
            for mid in (1, 2)]
        assert out["reproject"]["reprojected"] == 2 * len(revived_mids)
        for kind in kinds:
            for row in _kind_rows(db, kind):
                meta = json.loads(row["meta"])
                rejected = row["message_id"] not in revived_mids or (
                    change == "superseded" and row["artifact_id"] in old_ids)
                assert bool(meta.get("invalidated")) == rejected
                if not rejected:
                    assert "invalidated_reason" not in meta
                elif change == "unknown":
                    assert "invalidated_reason" not in meta
                else:
                    assert meta["invalidated_reason"] == (
                        "policy" if change == "policy" else "source")
        after = db.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        assert tick()["reproject"]["reprojected"] == 0
        assert read_current() == current
        assert db.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == after
    finally:
        db.close()


@pytest.mark.parametrize("mode", ["off", "enforce"])
@pytest.mark.parametrize("fact_source", ["canonical", "legacy", "shadow"])
def test_invalidation_reads_model_config_once_per_active_scan(
        tmp_path, monkeypatch, mode, fact_source):
    import semantic
    from semantic_store import invalidate_projections
    from semantic_testkit import _canonical_cfg

    db = _seeded(tmp_path)
    try:
        scfg = semantic.semantic_config(_canonical_cfg())[0]
        scfg.update(mode=mode, fact_source=fact_source)
        # Two independent threads, with both published row kinds.
        db.db.execute("UPDATE messages SET parent_id=NULL")
        db.db.commit()
        for mid in (1, 2):
            bundle = semantic.thread_bundle(
                db, 1, mid, local_model="synthetic-model")
            source_hash = db.db.execute(
                "SELECT content_hash FROM messages WHERE message_id=?",
                (mid,)).fetchone()[0]
            for kind in ("canonical_projection", "semantic_facts_v4"):
                db.artifact_add(
                    kind, "{}", project_id=1, message_id=mid,
                    meta={"hash": source_hash,
                          "fingerprint": bundle["source_fingerprint"],
                          "policy_fingerprint": semantic.policy_fingerprint(scfg)})
        reads = []

        def model():
            reads.append("read")
            return "synthetic-model"

        monkeypatch.setattr(semantic, "llm_model", model)
        enabled = mode != "off" and fact_source == "canonical"
        assert invalidate_projections(db, scfg) == (0 if enabled else 4)
        assert len(reads) == (1 if enabled else 0)
    finally:
        db.close()


@pytest.mark.parametrize("workflow", ["done", "performed", "reported", "cancelled", "unknown"])
def test_completed_or_unplanned_care_is_an_observation_not_a_schedule(workflow):
    doc = _doc([_fact("f1", kind="care_event", statement="本日退院済み", workflow_status=workflow)])
    assert projection.project_v2_facts(doc)[0]["kind"] == "observation"


@pytest.mark.parametrize("workflow", ["planned", "ordered", "pending", "considering", "on_hold", "in_progress"])
def test_pending_care_preserves_legacy_schedule_kind(workflow):
    doc = _doc([_fact("f1", kind="care_event", statement="訪問の予定", workflow_status=workflow)])
    assert projection.project_v2_facts(doc)[0]["kind"] == "schedule"


@pytest.mark.parametrize("event_time,expected", [
    ("3日前", None), ("3日後まで", None), ("2026-02-30", None),
    ("2026-10-09", "2026-10-09"), ("2026/10/09", "2026-10-09"),
    ("2026年10月9日", "2026-10-09"),
])
def test_projection_absolute_date_slots_do_not_accept_relative_or_invalid_dates(event_time, expected):
    doc = _doc([_fact("f1", kind="request_pending", statement="合成の確認依頼", event_time=event_time)])
    fact = projection.project_v2_facts(doc)[0]
    request = projection.project_v2_doc_legacy(doc)["requests"][0]
    assert fact["occurred_at"] == expected
    assert fact["time_text"] == event_time
    assert request["due"] == expected
    if expected is None:
        assert request["due_text"] == event_time
