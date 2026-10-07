"""Source-only density plans retain atom ownership, scoped evidence and honest progress."""
import json
from pathlib import Path

import pytest

import clinical_chunking
import semantic
import semantic_extraction as extraction
import semantic_facts as sf
import semantic_llm
from semantic_testkit import _PassJev, _canonical_cfg, _drain, _seeded_two
from test_semantic_facts_v2 import FP, NO_FACTS, _FakeLedger, _member


def _case():
    path = Path(__file__).resolve().parents[2] / "evaluation/semantic_completeness_cases.json"
    value = json.loads(path.read_text())
    cases = value if isinstance(value, list) else value["cases"]
    return next(case for case in cases if case["id"] == "dense-multi-drug-16plus")


def _core(prompt):
    return prompt.split("<<<\n", 1)[1].split("\n>>>", 1)[0]


def test_dense_229_character_case_is_planned_before_inference_with_all_obligations(monkeypatch):
    case = _case()
    body, expected = case["messages"][0]["body"], case["expect"]["facts"]
    manifest = extraction.build_manifest(body, FP)
    pieces = [chunk["text"] for chunk in manifest["chunks"]]
    assert len(body) == 229 and len(pieces) > 1
    assert pieces == clinical_chunking.plan_chunks(body)
    assert "".join(pieces) == body
    for fact in expected:
        assert sum(fact["evidence_quote"] in piece for piece in pieces) == 1
    calls = []

    def llm(prompt):
        core = _core(prompt)
        calls.append(core)
        facts = [{"statement": fact["statement"], "kind": fact["kind"],
                  "subject_role": "patient", "polarity": fact["polarity"], "epistemic": "reported",
                  "workflow_status": fact["workflow_status"], "event_time": fact["event_time"],
                  "evidence_quote": fact["evidence_quote"],
                  **({"action": "stop"} if fact["kind"] == "medication_event" else {})}
                 for fact in expected if fact["evidence_quote"] in core]
        presence = dict(NO_FACTS)
        for fact in facts:
            category = sf.KIND_CATEGORY[fact["kind"]]
            presence[category] = "multiple" if presence[category] == "one" else "one"
        return json.dumps({"facts": facts, "category_presence": presence}, ensure_ascii=False)

    result = extraction.extract_facts_v2(llm, _member(body), source_fingerprint=FP)
    assert calls == pieces and result["extraction_complete"]
    assert result["complete"] and not result["coverage"]["open_obligation_ids"]
    with monkeypatch.context() as old_rules:
        old_rules.setattr(extraction, "_symptom_names", lambda _source, names: list(names))
        split_before_fix = extraction.extract_facts_v2(llm, _member(body), source_fingerprint=FP)
        assert not split_before_fix["complete"]
        old_rules.setattr(clinical_chunking, "plan_chunks", lambda source, size: [source])
        whole_before_fix = extraction.extract_facts_v2(llm, _member(body), source_fingerprint=FP)
        assert whole_before_fix["complete"]
    sf.validate_facts_doc(result["doc"])
    model_statements = {fact["statement"] for fact in result["doc"]["facts"]
                        if fact["provenance"] == "local_llm"}
    assert {fact["statement"] for fact in expected if fact["mandatory"]} <= model_statements
    owned = [ref for chunk in manifest["chunks"] for ref in chunk["core_atom_ids"]]
    assert len(owned) == len(set(owned)) == len(manifest["atoms"])


@pytest.mark.parametrize("body,signal", [
    ("痛み時のみ使用とのこと。", False), ("疼痛時は架空薬を頓服してください。", False),
    ("発熱がある場合は架空薬を服用。", False), ("膝の痛みが続いています。", True),
    ("痛みが続くため、痛み時のみ使用。", True), ("痛みはない。痛み時のみ使用。", True),
    ("痛みがあるか不明。", True), ("痛み時に架空薬を内服しました。", True),
    ("痛み時のみ使用。発熱があります。", True), ("家族の痛みが続いています。", True),
    ("痛み時のみ使用しないとのこと。", True),
    ("疼痛時間が長く、架空薬を使用。", True), ("痛みときどきあり、架空薬を使用。", True),
])
def test_prn_reference_filter_keeps_reports_mixed_negation_and_uncertain_conditions(body, signal):
    assert ("symptom_state" in extraction._v1_category_signals(body, "")) is signal
    symptoms = [item for item in extraction._v1_hint_items(body, "") if item["kind"] == "symptom_state"]
    assert bool(symptoms) is signal


def test_heading_reference_preserves_scope_but_cannot_supply_evidence():
    body = "家族\n" + "・架空薬A1mgは使用せず中止予定。\n" * 6 + "本人\n・架空薬B2mgを朝に服用。"
    manifest = extraction.build_manifest(body, FP, chunk_size=50)
    specs = extraction._manifest_specs(manifest)
    target = next(spec for spec in specs if spec["dependency_atom_ids"] and "家族" not in spec["text"])
    prompt = extraction._chunk_prompt(semantic_llm._FACT_V2_PROMPT, target, manifest, body, 50)
    assert "家族" in prompt.split("参考文脈", 1)[1]
    assert "家族" not in _core(prompt)
    assert extraction._resolve_evidence("家族", target["text"], target["start"], body, semantic) is None
    assert "使用せず" in _core(prompt) and "中止予定" in _core(prompt)
    assert "patient_context" in semantic_llm._FACT_V2_PROMPT
    assert "任意欄のnull/空配列は省略" in semantic_llm._FACT_V2_PROMPT
    assert "statement/kind/subject_role/polarity" in semantic_llm._FACT_V2_PROMPT


def test_same_line_fragments_keep_mothers_past_scope_and_stop_action_as_references():
    body = "母の過去の処方：" + "、".join(f"架空薬{chr(65 + i)}{10 + i}mg" for i in range(14)) + "を中止。"
    manifest = extraction.build_manifest(body, FP, chunk_size=50)
    assert len(manifest["chunks"]) >= 3
    specs = extraction._manifest_specs(manifest)
    central = next(spec for spec in specs if "母" not in spec["text"] and "中止" not in spec["text"])
    prompt = extraction._chunk_prompt(semantic_llm._FACT_V2_PROMPT, central, manifest, body, 50)
    reference = prompt.split("参考文脈", 1)[1]
    assert "母の過去の処方" in reference and "を中止" in reference
    assert extraction._resolve_evidence("母の過去の処方", central["text"], central["start"], body, semantic) is None
    assert extraction._resolve_evidence("を中止", central["text"], central["start"], body, semantic) is None
    assert [spec["text"] for spec in specs] == clinical_chunking.plan_chunks(body, 50)


def test_current_plan_reuses_completed_prefix_but_plan_version_change_rejects_it(monkeypatch):
    body = "宇宙の架空説明。" * 25
    ledger, calls = _FakeLedger(), []

    def interrupted(prompt):
        calls.append(_core(prompt))
        if len(calls) == 2:
            raise RuntimeError("synthetic interruption")
        return json.dumps({"facts": [], "category_presence": NO_FACTS})

    first = extraction.extract_facts_v2(interrupted, _member(body), ledger=ledger,
                                        source_fingerprint=FP, chunk_size=50)
    assert first["completed_chunks"] == [0] and not first["extraction_complete"]
    resumed = extraction.extract_facts_v2(lambda _: json.dumps({"facts": [], "category_presence": NO_FACTS}),
                                          _member(body), ledger=ledger, source_fingerprint=FP, chunk_size=50)
    assert resumed["complete"] and resumed["reused_chunks"] == [0]
    monkeypatch.setattr(clinical_chunking, "PLAN_VERSION", clinical_chunking.PLAN_VERSION + 1)
    changed = extraction.extract_facts_v2(lambda _: json.dumps({"facts": [], "category_presence": NO_FACTS}),
                                          _member(body), ledger=ledger, source_fingerprint=FP, chunk_size=50)
    assert changed["complete"] and not changed["reused_chunks"]


def _progress(db, mid=1):
    cfg, errors = semantic.semantic_config(_canonical_cfg())
    assert not errors
    row = dict(db.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())
    return extraction.progress_for_message(db.db, row, model=semantic.llm_model(),
                                           policy_fingerprint=semantic.policy_fingerprint(cfg))


def test_progress_complete_requires_current_audited_published_document(tmp_path):
    db = _seeded_two(tmp_path)
    try:
        assert _progress(db) is None
        _drain(db, jev=_PassJev())
        assert _progress(db) == {"state": "complete", "completed": 1, "total": 1, "backend": "semantic"}
        with db.db:
            db.db.execute("DELETE FROM artifacts WHERE kind IN ('semantic_facts_v4','canonical_projection')")
        assert _progress(db)["state"] == "attention" and _progress(db)["completed"] == 1
        with db.db:
            db.db.execute("UPDATE messages SET content_hash='synthetic-edited' WHERE message_id=1")
        assert _progress(db)["state"] == "attention" and _progress(db)["completed"] == 0
    finally:
        db.close()


@pytest.mark.parametrize("fault", ["invalidated", "broken", "nonpass"])
def test_latest_current_generation_failure_never_revives_older_pass(tmp_path, fault):
    db = _seeded_two(tmp_path)
    try:
        _drain(db, jev=_PassJev())
        assert _progress(db)["state"] == "complete"
        publication = db.db.execute("SELECT * FROM artifacts WHERE kind='semantic_facts_v4' "
                                    "AND message_id=1 ORDER BY artifact_id DESC LIMIT 1").fetchone()
        meta = json.loads(publication["meta"])
        if fault == "invalidated":
            meta["invalidated"] = True
        db.artifact_add("semantic_facts_v4", "[" if fault == "broken" else publication["content"],
                        project_id=1, message_id=1, model=publication["model"], meta=meta)
        if fault == "nonpass":
            audit = db.db.execute("SELECT * FROM artifacts WHERE kind='semantic_facts_audit' "
                                  "AND message_id=1 ORDER BY artifact_id DESC LIMIT 1").fetchone()
            content = json.loads(audit["content"])
            content["status"] = "NEEDS_REVIEW"
            db.artifact_add(audit["kind"], json.dumps(content), project_id=1, message_id=1,
                            model=audit["model"], meta=json.loads(audit["meta"]))
        assert _progress(db)["state"] == "attention"
    finally:
        db.close()


@pytest.mark.parametrize("request_following", [False, True])
def test_progress_counts_current_paid_prefix_without_mixing_staging_generation(tmp_path, monkeypatch, request_following):
    from semantic_testkit import _ledger, _message, _patient

    db = _ledger(tmp_path)
    try:
        _patient(db)
        body = "宇宙の架空説明。" * 400
        db.save_messages([_message(1, body=body)], project_id=1)
        bundle = semantic.thread_bundle(db, 1, 1)
        calls = []

        def interrupted(prompt):
            calls.append(_core(prompt))
            if len(calls) == 2:
                raise RuntimeError("synthetic interruption")
            return json.dumps({"facts": [], "category_presence": NO_FACTS})

        result = extraction.extract_facts_v2(interrupted, bundle["members"][0], ledger=db,
                                            source_fingerprint=bundle["source_fingerprint"],
                                            request_following=request_following)
        assert result["completed_chunks"] == [0] and not result["extraction_complete"]
        progress = _progress(db)
        assert progress["state"] == "processing"
        assert progress["completed"] == (0 if request_following else 1)
        assert progress["total"] == result["chunks_total"]
    finally:
        db.close()
