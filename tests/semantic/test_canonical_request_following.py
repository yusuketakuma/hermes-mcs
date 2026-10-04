"""Real local request-following pipeline; all sources and databases are fictional."""
import copy
import hashlib
import json

import pytest

import semantic
import semantic_extraction as extraction
import semantic_facts as facts
import semantic_loops as loops
import semantic_projection as projection
import semantic_v4 as v4
from semantic_testkit import _cfg, _ledger, _message, _patient

TEXT = "架空担当Aから薬剤師へ、必要な場合に架空資料Xを明日までに確認してください。"
DETAILS = {"request_to": "薬剤師", "request_from": "架空担当A",
           "due_text": "明日まで", "condition": "必要な場合", "request_kind": "request"}


@pytest.fixture
def runtime(tmp_path):
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1, body=TEXT)], project_id=1)
    bundle = semantic.thread_bundle(db, 1, 1)
    member = bundle["members"][0]
    manifest = extraction.build_manifest(TEXT, bundle["source_fingerprint"], 3000)
    ev = {"evidence_id": facts.evidence_id(1, member["revision"], 0, len(TEXT), TEXT),
          "message_id": "1", "revision": member["revision"], "start": 0,
          "end": len(TEXT), "quote": TEXT, "atom_id": manifest["atoms"][0]["atom_id"]}
    fact = {"fact_id": facts.fact_id("1", 1, member["revision"], "request_pending",
                                   "unknown", "架空資料Xを確認", [ev["evidence_id"]]),
            "kind": "request_pending", "statement": "架空資料Xを確認",
            "subject": "unknown", "polarity": "affirmed", "epistemic": "asserted",
            "workflow_status": "pending", "event_time": "unknown", "valid_time": "unknown",
            "evidence_ids": [ev["evidence_id"]], "validation_status": "verified"}
    doc = {"version": facts.CONTRACT_VERSION,
           "source": {"message_id": "1", "revision": member["revision"],
                      "content_hash": hashlib.sha256(TEXT.encode()).hexdigest(),
                      "body_codepoints": len(TEXT), "content_quality": "full",
                      "attachments_complete": True,
                      "source_fingerprint": bundle["source_fingerprint"]},
           "atoms": manifest["atoms"], "chunks": manifest["chunks"], "obligations": [],
           "evidence": [ev], "facts": [fact], "relations": [],
           "coverage": {"category_counts": {"request_pending": 1}, "status": "complete"}}
    yield db, bundle, member, manifest, doc
    db.close()


def _with_details(doc, **over):
    doc = copy.deepcopy(doc)
    doc["facts"][0].update(DETAILS, **over)
    return doc


def test_old_document_shape_ids_hashes_and_released_generations_are_unchanged(runtime):
    _, _, _, _, doc = runtime
    old = facts.validate_facts_doc(doc)
    assert not set(DETAILS) & old["facts"][0].keys()
    assert facts.validate_facts_doc(old) == old
    enriched = facts.validate_facts_doc(_with_details(doc))
    assert enriched["facts"][0]["fact_id"] == old["facts"][0]["fact_id"]
    assert v4._doc_hash(enriched) != v4._doc_hash(old)
    assert projection.project_v2_doc_legacy(old) == projection.project_v2_doc_legacy(enriched)
    assert projection.project_v2_facts(old) == projection.project_v2_facts(enriched)
    assert extraction.SCHEMA_VERSION_V2 == "semantic-extraction/v2"
    assert projection.PROJECTION_VERSION == 2
    assert v4.ENGINE_VERSION == 4


def test_five_fields_survive_validation_and_explicit_runtime_projection(runtime):
    _, _, _, _, doc = runtime
    enriched = facts.validate_facts_doc(_with_details(doc))
    assert {key: enriched["facts"][0][key] for key in DETAILS} == DETAILS
    result = projection.project_v2_doc_legacy(enriched, request_details=True)["requests"][0]
    assert result == {"to": "薬剤師", "from": "架空担当A", "action": "架空資料Xを確認",
                      "due": None, "due_text": "明日まで", "condition": "必要な場合",
                      "kind": "request", "unverified": True}
    projected = projection.project_v2_facts(enriched, request_details=True)[0]
    assert projected["assignee_text"] == "薬剤師" and projected["time_text"] == "明日まで"
    assert projected["request_details"] == DETAILS
    assert "reply" not in projection.project_v2_doc_legacy(enriched, request_details=True)


@pytest.mark.parametrize("over", [
    {"request_to": "根拠にない宛先"}, {"request_from": "根拠にない依頼者"},
    {"due_text": "来月まで"}, {"condition": "根拠にない条件"},
    {"request_kind": "done"}, {"request_from": None}, {"condition": False},
])
def test_ungrounded_or_malformed_detail_degrades_only_that_field(runtime, over):
    _, _, _, _, doc = runtime
    clean = facts.validate_facts_doc(_with_details(doc, **over))
    fact = clean["facts"][0]
    assert fact["fact_id"] == doc["facts"][0]["fact_id"]
    assert fact["validation_status"] == "verified"
    assert fact["statement"] == doc["facts"][0]["statement"]
    for key in over:
        assert fact[key] == "unknown"
    result = projection.project_v2_doc_legacy(clean, request_details=True)["requests"][0]
    assert result["unverified"] is True
    if "due_text" in over:
        assert result["due"] is None and result["due_text"] is None


def test_real_normalizer_binds_fields_to_the_owning_fact_quote(runtime):
    _, _, member, manifest, _ = runtime
    spec = extraction._manifest_specs(manifest)[0]
    owners = {atom: chunk["chunk_id"] for chunk in manifest["chunks"]
              for atom in chunk["core_atom_ids"]}
    item = {"kind": "request_pending", "statement": "架空資料Xを確認",
            "evidence_quote": TEXT, "polarity": "affirmed", "epistemic": "asserted",
            "workflow_status": "pending", **DETAILS}
    normalized, dropped = extraction._normalise_facts_v2(
        [item], member, TEXT, semantic, spec=spec, manifest=manifest,
        atom_owner=owners, obligations={})
    assert dropped == 0
    assert {key: normalized[0][key] for key in DETAILS} == DETAILS
    item["evidence_quote"] = "架空資料X"
    normalized, dropped = extraction._normalise_facts_v2(
        [item], member, TEXT, semantic, spec=spec, manifest=manifest,
        atom_owner=owners, obligations={})
    assert dropped == 0 and normalized[0]["validation_status"] == "verified"
    for key in ("request_to", "request_from", "due_text", "condition"):
        assert normalized[0][key] == "unknown"


def test_duplicate_chunk_metadata_is_retained_and_conflicts_stay_unknown(runtime):
    _, _, _, _, doc = runtime
    old = facts.validate_facts_doc(doc)["facts"][0]
    new = facts.validate_facts_doc(_with_details(doc))["facts"][0]
    for ordered in ([old, new], [new, old]):
        merged = extraction._merge_dupe_facts(copy.deepcopy(ordered))
        assert len(merged) == 1 and merged[0]["fact_id"] == old["fact_id"]
        assert {key: merged[0][key] for key in DETAILS} == DETAILS
        assert merged[0]["workflow_status"] == "pending"
    conflicting = copy.deepcopy(new)
    conflicting["request_to"] = "架空担当A"  # also quoted, but attribution conflicts
    for ordered in ([new, conflicting], [conflicting, new]):
        merged = extraction._merge_dupe_facts(copy.deepcopy(ordered))
        assert merged[0]["request_to"] == "unknown"
        assert merged[0]["request_from"] == "架空担当A"
    # A collapsed conflict stays unknown whatever order a third duplicate takes.
    for ordered in ([new, conflicting, new], [conflicting, new, new],
                    [new, new, conflicting]):
        merged = extraction._merge_dupe_facts(copy.deepcopy(ordered))
        assert merged[0]["request_to"] == "unknown"
    blanked = copy.deepcopy(new)
    blanked["request_to"] = "unknown"  # chunk context lacked the recipient
    for ordered in ([new, blanked], [blanked, new]):
        merged = extraction._merge_dupe_facts(copy.deepcopy(ordered))
        assert {key: merged[0][key] for key in DETAILS} == DETAILS


def test_actual_synthetic_extraction_cache_replay_and_durable_staging(runtime):
    db, bundle, member, _, _ = runtime
    calls = []

    def fictional_response(_prompt):
        calls.append("synthetic-only")
        presence = dict.fromkeys(facts.MANDATORY_CATEGORIES, "none")
        presence["request_pending"] = "one"
        return json.dumps({
            "facts": [{"kind": "request_pending", "statement": "架空資料Xを確認",
                       "evidence_quote": TEXT, "polarity": "affirmed",
                       "epistemic": "asserted", "workflow_status": "pending", **DETAILS}],
            "category_presence": presence}, ensure_ascii=False)

    result = extraction.extract_facts_v2(
        fictional_response, member, ledger=db, project_id=1,
        source_fingerprint=bundle["source_fingerprint"])
    assert result["extraction_complete"] is True and len(calls) == 1
    found = next(f for f in result["doc"]["facts"] if f["statement"] == "架空資料Xを確認")
    assert {key: found[key] for key in DETAILS} == DETAILS
    cached = extraction.extract_facts_v2(
        fictional_response, member, ledger=db, project_id=1,
        source_fingerprint=bundle["source_fingerprint"])
    assert len(calls) == 1  # cache consumed, no second inference dispatch
    assert cached["doc"] == result["doc"]
    candidate = v4.stage_request_following(
        db, bundle, cached["doc"], policy="fixture-policy")
    request = next(r for r in candidate["projection"]["requests"]
                   if r["action"] == "架空資料Xを確認")
    assert request["to"] == "薬剤師" and request["from"] == "架空担当A"
    assert request["due_text"] == "明日まで" and request["condition"] == "必要な場合"
    assert request["kind"] == "request" and request["unverified"] is True
    assert candidate["promotion_ready"] is False
    chunk = db.db.execute("SELECT meta FROM artifacts WHERE kind=?",
                          (extraction.KIND_CHUNK_V2,)).fetchone()
    assert json.loads(chunk[0])["schema"] == extraction.SCHEMA_VERSION_V2


def test_staging_is_callable_durable_idempotent_and_not_a_promotion(runtime):
    db, bundle, _, _, doc = runtime
    candidate = v4.stage_request_following(db, bundle, _with_details(doc), policy="fixture-policy")
    assert candidate["projection"]["requests"][0]["to"] == "薬剤師"
    assert candidate["source"]["revision"] == bundle["members"][0]["revision"]
    assert candidate["source_doc_hash"] == v4._doc_hash(_with_details(doc))
    assert candidate["doc_hash"] == v4._doc_hash(facts.validate_facts_doc(_with_details(doc)))
    assert candidate["promotion_ready"] is False
    assert "human_200_g6" in candidate["required_promotion_gates"]
    assert "calibration" in candidate["required_promotion_gates"]
    again = v4.stage_request_following(db, bundle, _with_details(doc), policy="fixture-policy")
    assert again == candidate
    rows = db.db.execute("SELECT content,meta FROM artifacts WHERE kind=?", (v4.KIND_V4_STAGE,)).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["content"])["candidate"] == candidate
    meta = json.loads(rows[0]["meta"])
    assert meta["fingerprint"] == bundle["source_fingerprint"]
    assert meta["policy_fingerprint"] == "fixture-policy"
    assert meta["engine_version"] == 4
    assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind IN "
                         "('loop_candidate','canonical_projection','semantic_facts_v4')").fetchone()[0] == 0


@pytest.mark.parametrize("damage", [
    "revision", "quote", "source_hash", "quality", "id_alias", "empty_policy",
])
def test_staging_refuses_forged_lineage_or_quote_before_any_write(runtime, damage):
    db, bundle, _, _, doc = runtime
    doc = _with_details(doc)
    policy = "fixture-policy"
    if damage == "revision":
        doc["source"]["revision"] = "another-revision"
        doc["evidence"][0]["revision"] = "another-revision"
    elif damage == "quote":
        doc["evidence"][0]["quote"] = TEXT.replace("架空", "偽造", 1)
    elif damage == "source_hash":
        doc["source"]["content_hash"] = "not-the-source"
    elif damage == "quality":
        doc["source"]["content_quality"] = "partial"
    elif damage == "id_alias":
        doc["source"]["message_id"] = "01"
        doc["evidence"][0]["message_id"] = "01"
    else:
        policy = ""
    with pytest.raises(facts.ContractError, match="request_following_"):
        v4.stage_request_following(db, bundle, doc, policy=policy)
    assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?",
                         (v4.KIND_V4_STAGE,)).fetchone()[0] == 0


def test_staging_holds_old_thread_generation_after_edit_or_reply(runtime):
    db, bundle, _, _, doc = runtime
    db.save_messages([_message(2, parent=1, body="承知しました。まだ実施していません。")],
                     project_id=1)
    with pytest.raises(facts.ContractError, match="request_following_source_stale"):
        v4.stage_request_following(db, bundle, _with_details(doc), policy="fixture-policy")
    assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?",
                         (v4.KIND_V4_STAGE,)).fetchone()[0] == 0


def test_default_loop_inputs_do_not_change_stored_identity_or_merge_reply(runtime):
    db, bundle, _, _, doc = runtime
    scfg, errors = semantic.semantic_config(_cfg("shadow"))
    assert not errors
    old = facts.validate_facts_doc(doc)
    new = facts.validate_facts_doc(_with_details(doc))
    assert loops.update_loops(db, 1, bundle, {1: projection.project_v2_facts(old)},
                              None, scfg, None) == (1, True)
    assert loops.update_loops(db, 1, bundle, {1: projection.project_v2_facts(new)},
                              None, scfg, None) == (0, True)
    row = db.db.execute("SELECT content FROM artifacts WHERE kind='loop_candidate'").fetchone()
    original = json.loads(row[0])
    v4.stage_request_following(db, bundle, new, policy=semantic.policy_fingerprint(scfg))
    assert json.loads(db.db.execute(
        "SELECT content FROM artifacts WHERE kind='loop_candidate'").fetchone()[0]) == original
    assert original["state"] == "PROPOSED" and original["assignee_text"] is None
    assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='loop_candidate'").fetchone()[0] == 1


@pytest.mark.parametrize("workflow", ["reported", "planned", "in_progress", "done", "cancelled"])
def test_workflow_or_request_classification_does_not_synthesize_reply_or_close_loop(
        runtime, workflow):
    db, bundle, _, _, doc = runtime
    doc = _with_details(doc)
    doc["facts"][0]["workflow_status"] = workflow
    doc["facts"][0]["request_kind"] = "question"
    candidate = v4.stage_request_following(db, bundle, doc, policy="fixture-policy")
    assert candidate["projection"]["requests"][0]["kind"] == "question"
    assert candidate["projection"]["requests"][0]["unverified"] is True
    assert "reply" not in candidate["projection"]
    assert candidate["promotion_ready"] is False
    assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='loop_event'").fetchone()[0] == 0
