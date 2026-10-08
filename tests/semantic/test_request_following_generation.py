"""Fictional callback, real validation/cache/staging; no model or API acceptance."""
import json

import pytest

import semantic
import semantic_extraction as extraction
import semantic_facts as facts
import semantic_llm as prompts
import semantic_v4 as v4
from semantic_testkit import _ledger, _message, _patient

TEXT = "架空担当Aから薬剤師へ、必要な場合に架空資料Xを明日までに確認してください。"
DETAILS = {"request_to": "薬剤師", "request_from": "架空担当A",
           "due_text": "明日まで", "condition": "必要な場合", "request_kind": "request"}


def reply(text=TEXT, details=None):
    presence = dict.fromkeys(facts.MANDATORY_CATEGORIES, "none")
    presence["request_pending"] = "one"
    return json.dumps({"facts": [{
        "kind": "request_pending", "statement": text, "evidence_quote": text,
        "polarity": "affirmed", "epistemic": "asserted", "workflow_status": "pending",
        **(details or {})}], "category_presence": presence}, ensure_ascii=False)


@pytest.fixture
def source(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1, body=TEXT)], project_id=1)
    monkeypatch.setattr(semantic, "llm_model", lambda: "fictional-model")
    bundle = semantic.thread_bundle(db, 1, 1)
    yield db, bundle, bundle["members"][0]
    db.close()


def extract(source, callback, **options):
    db, bundle, member = source
    return extraction.extract_facts_v2(
        callback, member, ledger=db, project_id=1,
        source_fingerprint=bundle["source_fingerprint"], **options)


def test_default_and_explicit_false_share_the_current_plan_generation(source):
    # Given / When
    implicit = extract(source, lambda _: reply())
    explicit = extract(source, lambda _: pytest.fail("default cache missed"), request_following=False)
    # Then: this is a machine cache identity, not a prose assertion.
    assert extraction._chunk_generation("fictional-model", prompts._FACT_V2_PROMPT) == \
        "fictional-model|b7c2f15eb3182ceb|plan:1|size:3000"
    db, _, _ = source
    meta = json.loads(db.artifacts(extraction.KIND_CHUNK_V2)[0]["meta"])
    assert meta["generation"] == "fictional-model|b7c2f15eb3182ceb|plan:1|size:3000"
    assert implicit["doc"] == explicit["doc"] and explicit["reused_chunks"] == [0]
    assert "generation" not in implicit and "generation" not in explicit
    assert extraction.SCHEMA_VERSION_V2 == "semantic-extraction/v2"
    assert facts.CONTRACT_VERSION == "semantic-facts/v2"


@pytest.mark.parametrize("first", [False, True])
def test_default_and_candidate_cache_do_not_cross_generations(source, first):
    # Given
    calls = []
    outputs = {}
    for mode in (first, not first):
        def callback(_prompt):
            calls.append(mode)
            return reply(details=DETAILS if mode else None)
        outputs[mode] = extract(source, callback, request_following=mode)
    # When
    reused = {mode: extract(source, lambda _: pytest.fail("own cache not reused"),
                            request_following=mode) for mode in (False, True)}
    # Then
    assert calls == [first, not first]
    assert all(result["reused_chunks"] == [0] for result in reused.values())
    assert outputs[False]["doc"] == reused[False]["doc"]
    assert outputs[True]["doc"] == reused[True]["doc"]
    found = next(f for f in reused[True]["doc"]["facts"] if f["statement"] == TEXT)
    assert {key: found[key] for key in DETAILS} == DETAILS
    assert not any(set(DETAILS) & f.keys() for f in reused[False]["doc"]["facts"])
    db, _, _ = source
    rows = db.artifacts(extraction.KIND_CHUNK_V2)
    generations = {json.loads(row["meta"])["generation"] for row in rows}
    assert len(generations) == 2
    assert outputs[True]["generation"] in generations
    assert {json.loads(row["meta"])["schema"] for row in rows} == {extraction.SCHEMA_VERSION_V2}


def test_candidate_prompt_hash_change_reextracts_without_invalidating_default(source, monkeypatch):
    # Given
    extract(source, lambda _: reply())
    candidate = extract(source, lambda _: reply(details=DETAILS), request_following=True)
    monkeypatch.setattr(prompts, "_FACT_V2_REQUEST_FOLLOWING_PROMPT",
                        prompts._FACT_V2_REQUEST_FOLLOWING_PROMPT + "\n")
    calls = []
    # When
    changed = extract(source, lambda _: calls.append(1) or reply(details=DETAILS),
                      request_following=True)
    old = extract(source, lambda _: pytest.fail("candidate invalidated old cache"))
    # Then
    assert calls == [1] and changed["generation"] != candidate["generation"]
    assert changed["reused_chunks"] == [] and old["reused_chunks"] == [0]


@pytest.mark.parametrize("retry_coverage", [False, True])
def test_candidate_prefix_failure_retry_preserves_generation_and_fields(source, retry_coverage):
    # Given
    db, _, _ = source
    second = TEXT.replace("担当A", "担当B").replace("資料X", "資料Y").replace("明日まで", "翌週まで")
    db.save_messages([_message(1, body=TEXT + "\n" + second)], project_id=1)
    bundle = semantic.thread_bundle(db, 1, 1)
    current = (db, bundle, bundle["members"][0])
    calls = []
    def interrupted(_prompt):
        calls.append(1)
        if len(calls) == 2:
            raise ConnectionError("synthetic callback interrupted")
        return reply(details=DETAILS)
    options = {"request_following": True, "chunk_size": len(TEXT),
               "retry_coverage": retry_coverage}
    partial = extract(current, interrupted, **options)
    # When
    resumed = extract(current, lambda _: reply(second, {
        **DETAILS, "request_from": "架空担当B", "due_text": "翌週まで"}), **options)
    # Then
    assert partial["extraction_complete"] is False and partial["completed_chunks"] == [0]
    assert resumed["extraction_complete"] is True and resumed["reused_chunks"] == [0]
    assert partial["generation"] == resumed["generation"]
    first = next(f for f in resumed["doc"]["facts"] if f["statement"] == TEXT)
    assert {key: first[key] for key in DETAILS} == DETAILS
    assert all(json.loads(row["meta"])["generation"] == resumed["generation"]
               for row in db.artifacts(extraction.KIND_CHUNK_V2))


def test_coverage_retry_schema_keeps_candidate_prompt_generation(source):
    # Given
    ordinary = extract(source, lambda _: reply(details=DETAILS), request_following=True)
    calls = []
    # When
    retried = extract(source, lambda _: calls.append(1) or reply(details=DETAILS),
                      request_following=True, retry_coverage=True)
    # Then
    assert calls == [1] and retried["generation"] == ordinary["generation"]
    assert retried["reused_chunks"] == []
    assert extract(source, lambda _: pytest.fail("retry cache lost"),
                   request_following=True, retry_coverage=True)["reused_chunks"] == [0]


def test_candidate_repair_uses_selected_machine_route_and_keeps_generation(source, monkeypatch):
    # Given: temporary machine routing tokens, no production prose pinned.
    monkeypatch.setattr(prompts, "_FACT_V2_PROMPT", "OLD_ROUTE\n<<<\n%s\n>>>\nJSON:")
    monkeypatch.setattr(prompts, "_FACT_V2_REQUEST_FOLLOWING_PROMPT",
                        "CANDIDATE_ROUTE\n<<<\n%s\n>>>\nJSON:")
    db = source[0]
    bundle = semantic.thread_bundle(db, 1, 1)
    source = (db, bundle, bundle["members"][0])
    result = extract(source, lambda _: reply(details=DETAILS), request_following=True)
    doc = result["doc"]
    target = next(f for f in doc["facts"] if f["statement"] == TEXT)
    routes = []
    def callback(prompt):
        routes.append(prompt.splitlines()[0])
        return reply(details=DETAILS)
    # When
    repaired = extraction.repair_facts_v2(
        callback, source[2], doc, {target["fact_id"]: "synthetic rejection"},
        request_following=True)
    candidate = v4.stage_request_following(source[0], source[1], repaired["doc"], policy="fixture")
    old = extraction.repair_facts_v2(
        callback, source[2], doc, {target["fact_id"]: "synthetic rejection"})
    # Then
    assert routes == ["CANDIDATE_ROUTE", "OLD_ROUTE"]
    assert repaired["repaired"] and repaired["generation"] == result["generation"]
    assert "generation" not in old
    request = next(r for r in candidate["projection"]["requests"] if r["action"] == TEXT)
    assert request["to"] == "薬剤師" and request["from"] == "架空担当A"
    assert candidate["promotion_ready"] is False


@pytest.mark.parametrize("failure", ["empty", "unknown", "callback"])
def test_candidate_repair_noop_or_failure_keeps_generation(source, failure):
    # Given
    extracted = extract(source, lambda _: reply(details=DETAILS), request_following=True)
    target = next(f for f in extracted["doc"]["facts"] if f["statement"] == TEXT)
    rejected = {} if failure == "empty" else {
        "fact_missing" if failure == "unknown" else target["fact_id"]: "synthetic rejection"}
    def failing(_prompt):
        raise ConnectionError("synthetic repair failure")
    # When
    result = extraction.repair_facts_v2(
        failing, source[2], extracted["doc"], rejected, request_following=True)
    # Then
    assert not result["repaired"] and result["doc"] == extracted["doc"]
    assert result["generation"] == extracted["generation"]


def test_actual_candidate_wrapper_cache_and_pending_staging_have_no_public_side_effects(source):
    # Given
    db, bundle, _ = source
    tables = [r[0] for r in db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name!='artifacts' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    before = {table: [tuple(r) for r in db.db.execute(f'SELECT * FROM "{table}"')] for table in tables}
    calls = []
    # When
    first = v4.extract_request_following(
        lambda _: calls.append(1) or reply(details=DETAILS), db, bundle, 1, policy="fixture")
    again = v4.extract_request_following(
        lambda _: pytest.fail("candidate replay dispatched"), db, bundle, 1, policy="fixture")
    # Then
    assert calls == [1] and again["extraction"]["reused_chunks"] == [0]
    assert first["candidate"] == again["candidate"]
    assert first["candidate"]["promotion_ready"] is False
    assert {"human_200_g6", "calibration"} <= set(first["candidate"]["required_promotion_gates"])
    assert before == {table: [tuple(r) for r in db.db.execute(f'SELECT * FROM "{table}"')] for table in tables}
    stages = v4.stage_ledger(db, 1, bundle["source_fingerprint"])
    assert len(stages) == 2 and {stage["status"] for stage in stages} == {"PENDING"}
    receipt = next(r for r in stages if r["stage"] == "request_following_extraction")
    assert receipt["generation"] == first["extraction"]["generation"]
    assert receipt["doc_hash"] == first["candidate"]["source_doc_hash"]
    assert {r[0] for r in db.db.execute("SELECT DISTINCT kind FROM artifacts")} <= {
        extraction.KIND_MANIFEST, extraction.KIND_CHUNK_V2, v4.KIND_V4_STAGE}


def test_wrapper_failed_extraction_is_pending_without_candidate_then_resumes(source):
    # Given
    db, bundle, _ = source
    incomplete = v4.extract_request_following(
        lambda _: "not-json", db, bundle, 1, policy="fixture")
    # When
    completed = v4.extract_request_following(
        lambda _: reply(details=DETAILS), db, bundle, 1, policy="fixture")
    # Then
    assert incomplete["candidate"] is None and not incomplete["extraction"]["extraction_complete"]
    assert completed["candidate"]["promotion_ready"] is False
    assert incomplete["extraction"]["generation"] == completed["extraction"]["generation"]
    receipts = v4.stage_ledger(db, 1, bundle["source_fingerprint"])
    assert {r["status"] for r in receipts} == {"PENDING"}
    attempts = [r for r in receipts if r["stage"] == "request_following_extraction"]
    assert [r["extraction_complete"] for r in attempts] == [False, True]
    assert {r["generation"] for r in attempts} == {completed["extraction"]["generation"]}


@pytest.mark.parametrize("invalid", ["stale", "message", "policy"])
def test_wrapper_invalid_source_or_policy_never_dispatches(source, invalid):
    # Given
    db, bundle, _ = source
    if invalid == "stale":
        db.save_messages([_message(1, body=TEXT + "架空追記。")], project_id=1)
    # When / Then
    with pytest.raises(facts.ContractError):
        v4.extract_request_following(
            lambda _: pytest.fail("invalid source dispatched"), db, bundle,
            99 if invalid == "message" else 1, policy="" if invalid == "policy" else "fixture")
    assert not db.artifacts(v4.KIND_V4_STAGE)


def test_source_changed_by_callback_cannot_stage_stale_candidate(source):
    # Given
    db, bundle, _ = source
    def changed(_prompt):
        db.save_messages([_message(1, body=TEXT + "架空追記。")], project_id=1)
        return reply(details=DETAILS)
    # When / Then
    with pytest.raises(facts.ContractError):
        v4.extract_request_following(changed, db, bundle, 1, policy="fixture")
    assert not db.artifacts(v4.KIND_V4_STAGE)


@pytest.mark.parametrize("flag", [None, 1, "true"])
def test_candidate_flag_rejects_implicit_truth_before_dispatch(source, flag):
    # Given / When / Then
    with pytest.raises(ValueError):
        extract(source, lambda _: pytest.fail("invalid flag dispatched"), request_following=flag)
    with pytest.raises(ValueError):
        extraction.repair_facts_v2(lambda _: pytest.fail("invalid flag repair"),
                                  source[2], {}, {}, request_following=flag)


def test_shadow_compare_reads_staged_candidates_without_writes(tmp_path, source, capsys):
    # Given: candidates for messages 1-7 with one legacy situation each.
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "canonical_request_eval_shadow",
        Path(__file__).resolve().parents[2] / "evaluation" / "canonical_request_eval.py")
    report_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report_module)
    db, _, member = source
    db.save_messages([_message(i, parent=1, body=TEXT) for i in range(2, 8)], project_id=1)
    bundle = semantic.thread_bundle(db, 1, 1)
    for mid in range(1, 8):
        v4.extract_request_following(lambda _: reply(details=DETAILS), db, bundle, mid,
                                     policy="fixture")

    def legacy(mid, doc):
        db.artifact_add("extract_llm", json.dumps(doc, ensure_ascii=False), project_id=1,
                        message_id=mid, meta={"hash": member["revision"]})

    # 1: paraphrased action paired by evidence, one unrelated row, one malformed row.
    legacy(1, {"requests": [
        {"to": "医師", "action": "短い引用", "evidence": "資料X"},  # too short to pair
        {"to": "薬剤師", "from": "看護師", "action": "資料X確認", "kind": "request",
         "due_text": "明日まで", "evidence": "架空資料Xを明日までに確認してください"},
        {"to": "医師", "action": "別件", "evidence": "架空の別件"}, "bad"]})
    # 2: no extract_llm row (unknown).  3: no requests key (real zero).
    legacy(3, {})
    legacy(4, {"requests": "x"})  # non-list: unknown
    legacy(5, {"requests": [], "_items_dropped": 1})  # partial
    db.save_messages([_message(6, parent=1, body=TEXT + "架空追記。")], project_id=1)  # stale
    db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=7")
    db.db.commit()
    before = db.db.total_changes
    # When
    report = report_module.shadow_compare(db.db)
    # Then
    assert db.db.total_changes == before
    assert report["candidates"] == 7 and report["compared"] == 2
    assert report["legacy_unknown"] == 2 and report["legacy_partial"] == 1
    assert report["stale_or_invalid"] == 2 and report["dropped_legacy_rows"] == 1
    assert (report["matched"], report["unmatched_legacy"], report["unmatched_candidate"]) \
        == (1, 2, 1)
    fields = report["fields"]
    assert fields["to"]["correct"] == 1 and fields["from"]["correct"] == 0
    assert fields["from"]["denominator"] == 1 and fields["kind"]["correct"] == 1
    assert fields["due_text"]["correct"] == 1 and fields["condition"]["denominator"] == 0
    assert report["production_ready"] is False and report["model_calls"] == 0
    # CLI: read-only URI, same report; a missing ledger fails without being created.
    path = tmp_path / "ledger.db"
    capsys.readouterr()
    assert report_module.main(["--shadow-db", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == report
    missing = tmp_path / "missing.db"
    assert report_module.main(["--shadow-db", str(missing)]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "OperationalError"}
    assert not missing.exists()
