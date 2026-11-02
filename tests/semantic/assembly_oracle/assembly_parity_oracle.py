"""Compare fixed synthetic meaning after independent assertions, never bless a new owner."""
import llm_availability
import copy
import hashlib
import inspect
from functools import wraps
import json
from pathlib import Path
import time

import extract_llm
import semantic_extraction as extraction
import semantic_facts
import semantic_relations
import semantic_publication
import test_extract_upgrade_preservation as upgrade_cases
import test_hint_rebuild_pass as hint_cases
import test_semantic_facts_v2 as fact_cases


BASE_HEAD = "4b0787b42b949c78e8156c6ea56caa1fa0bad839"
ORIGINAL_EXTRACTION_SHA256 = "d78039da8acb828738123c1f87f4712a257292a7d4c486cd22db27785ae99eee"
CORPUS_VERSION = "assembly-meaning-v1"
WALL = 1792368000.0
MONOTONIC = 100.0
# These are pre-existing test bodies, including their independent assertions.
SCENARIOS = (
    "empty_source", "subject_patient_family_unknown", "durable_prefix",
    "evidence_cache", "stored_preflight", "old_rule_same_vs_held",
    "missing_preflight", "cache_miss_before_preflight",
    "manifest_change_before_cache_miss", "held_prior", "unfinished_relations",
)


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _input(real, args, kwargs):
    bound = inspect.signature(real).bind_partial(*args, **kwargs).arguments
    selected = {key: bound[key] for key in (
        "member", "source_fingerprint", "project_id", "chunk_size", "deadline",
        "retry_coverage", "request_following", "coverage_missing", "doc",
        "rejected", "stored_doc") if key in bound}
    if "stored_meta" in bound:
        # This is the only metadata input consumed by cached assembly.
        selected["stored_meta"] = {"coverage_retry": bool(bound["stored_meta"].get("coverage_retry"))}
    return _json_copy(selected)


def test_fixed_synthetic_assembly_meaning(request, tmp_path, monkeypatch, pytestconfig):
    mode = pytestconfig.getoption("--assembly-oracle-mode")
    assert mode in ("seal", "compare"), "Select --assembly-oracle-mode explicitly"
    file_option = pytestconfig.getoption("--assembly-oracle-file")
    assert file_option, "Select --assembly-oracle-file explicitly"
    reference = Path(file_option)
    assert reference.is_absolute(), "Use an explicit Q output path"
    monkeypatch.setattr(time, "time", lambda: WALL)
    monkeypatch.setattr(time, "monotonic", lambda: MONOTONIC)
    monkeypatch.setattr(time, "perf_counter", lambda: MONOTONIC)
    # Sufficient synthetic disk capacity, no actual machine measurement.
    monkeypatch.setattr(llm_availability, 'disk_free_mb', lambda *args: 100_000.0)

    trace, observations, excluded_code_hashes = [], {}, []
    current_case = [None]
    depth = [0]
    for name in ("extract_facts_v2", "repair_facts_v2", "rebuild_facts_v2_cached"):
        real = getattr(extraction, name)

        @wraps(real)
        def capture(*args, _real=real, _name=name, **kwargs):
            outer = depth[0] == 0
            inputs = _input(_real, args, kwargs) if outer else None
            depth[0] += 1
            try:
                result = _real(*args, **kwargs)
            finally:
                depth[0] -= 1
            if outer:
                trace.append({"scenario": current_case[0], "entry": _name,
                              "input": inputs, "output": _json_copy(result)})
            return result
        monkeypatch.setattr(extraction, name, capture)

    # Capture the receipt meaning after its actual transaction. The source-code
    # contract intentionally changes; assert its validity before replacing that
    # one exact value for A/B meaning comparison. No other hash is removed.
    real_hold = extract_llm._record_hold_tx

    @wraps(real_hold)
    def capture_hold(ledger, row, prior, reason, **kwargs):
        result = real_hold(ledger, row, prior, reason, **kwargs)
        receipt_row = ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id DESC LIMIT 1",
            (extract_llm.UPGRADE_KIND,)).fetchone()
        receipt, meta = map(json.loads, receipt_row)
        assert receipt["state"] == "held_prior" and receipt["reason"] == "protected_loss"
        assert receipt["prior_artifact_id"] == prior["artifact_id"]
        assert receipt["lost"] == {"symptoms": 2}
        assert receipt["contract"] == meta["contract"] == extract_llm._upgrade_contract()
        assert receipt["hash"] == row["content_hash"]
        excluded_code_hashes.append({"trace_index": len(trace), "content.contract": receipt["contract"],
                                     "meta.contract": meta["contract"]})
        receipt["contract"] = meta["contract"] = "<intentionally-changing-loaded-code-contract>"
        trace.append({"scenario": current_case[0], "entry": "held_prior_receipt",
                      "output": {"content": receipt, "meta": meta}})
        return result
    monkeypatch.setattr(extract_llm, "_record_hold_tx", capture_hold)

    def run(name, function, *args):
        current_case[0] = name
        function(*args)
        observations[name] = "independent_assertions_passed"

    run("empty_source", fact_cases.test_empty_source_validates_empty_doc)
    run("subject_patient_family_unknown", fact_cases.test_subject_mapping_patient_family_unknown)
    run("durable_prefix", fact_cases.test_durable_prefix_reuse_v2, request)
    run("evidence_cache", fact_cases.test_v2_cache_reuses_evidence_bearing_chunks, request)
    run("stored_preflight", fact_cases.test_preflight_verdicts_cached_with_chunk, request)

    for name in ("old_rule_same_vs_held", "missing_preflight",
                 "cache_miss_before_preflight", "manifest_change_before_cache_miss"):
        current_case[0] = name
        location = tmp_path / name
        location.mkdir()
        # Existing generator fixture and its exact synthetic source/model/Jev.
        # Its old-rule patch is migrated to the actual owner by the C patch.
        with monkeypatch.context() as case_patch:
            fixture = hint_cases.world.__wrapped__(location, case_patch)
            db = next(fixture)
            try:
                if name == "old_rule_same_vs_held":
                    hint_cases.test_old_documents_are_marked_and_only_the_changed_one_is_held(db)
                    # Replay identity: no new artifact/meta change on second pass.
                    hint_cases.test_second_pass_does_nothing(db)
                elif name == "missing_preflight":
                    hint_cases.test_preflight_not_stored_is_never_asked_again(db)
                else:
                    for row in hint_cases._kind_rows(db, "semantic_extraction_chunk_v2", 2):
                        content = json.loads(row["content"])
                        content.pop("jev_preflight")
                        db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                                      (json.dumps(content, ensure_ascii=False), row["artifact_id"]))
                    hint_cases._drop_chunks(db, 2)
                    if name == "manifest_change_before_cache_miss":
                        db.db.execute("UPDATE artifacts SET content=json_set(content,'$.body_codepoints',-1) "
                                      "WHERE kind='semantic_source_manifest' AND message_id=2")
                        db.db.commit()
                    result = semantic_publication.reproject_stale(db, hint_cases._scfg())
                    reason = ("manifest_changed" if name == "manifest_change_before_cache_miss"
                              else "chunk_cache_miss")
                    assert result["skip_reasons"]["hint_rebuild:" + reason] == 1
                    assert all(meta.get("invalidated") is True
                               for meta in hint_cases._meta(db, semantic_publication.KIND_V4, 2))
                    assert hint_cases._job(db)[0] == "pending"
                    observations[name] = result
                if name in ("old_rule_same_vs_held", "missing_preflight"):
                    observations[name] = "existing_independent_assertions_passed"
            finally:
                try:
                    next(fixture)
                except StopIteration:
                    pass

    location = tmp_path / "held_prior"
    location.mkdir()
    run("held_prior", upgrade_cases.test_automatic_replacements_preserve_the_original_artifact_and_stop_repeating,
        location, "upgrade")

    current_case[0] = "unfinished_relations"
    body = "合成観察Aです。合成観察Bです。"
    items = [{"statement": "合成観察" + letter, "kind": "other_observation",
              "subject_role": "family", "polarity": "negated", "workflow_status": "planned",
              "evidence_quote": "合成観察" + letter} for letter in ("A", "B")]
    model = fact_cases._llm(items, {"other_observation": "multiple"})
    real_reconcile = semantic_relations.reconcile_facts

    def expired_scan(active, new, **kwargs):
        # Exercise the actual comparator's deadline boundary after extraction;
        # no fabricated relation set or replacement pairs_skipped result.
        kwargs["deadline"] = MONOTONIC - 1.0
        return real_reconcile(active, new, **kwargs)
    with monkeypatch.context() as expiry:
        expiry.setattr(semantic_relations, "reconcile_facts", expired_scan)
        member = fact_cases._member(body)
        result = extraction.extract_facts_v2(model, member)
        semantic_facts.validate_facts_doc(result["doc"])
        assert result["chunks_completed"] == result["chunks_total"] == 1
        assert len(result["facts"]) == 2 and result["relation_pairs_skipped"] == 1
        assert result["failure_reason"] == "deadline" and not result["extraction_complete"]
        assert result["coverage"]["status"] == "incomplete"
        assert "relations_incomplete" in result["coverage"]["limitations"]
        assert all(f["subject"] == "role:family" and f["polarity"] == "negated"
                   and f["workflow_status"] == "planned" for f in result["facts"])
        old = result["doc"]
        before = copy.deepcopy(old)
        repaired = extraction.repair_facts_v2(model, member, old,
                                             {old["facts"][0]["fact_id"]: "synthetic"})
        assert not repaired["repaired"] and repaired["doc"] is old and old == before
    observations["unfinished_relations"] = "independent_deadline_subject_negation_status_old_doc_assertions_passed"
    assert set(observations) == set(SCENARIOS)
    assert all(any(row["scenario"] == name for row in trace) for name in SCENARIOS)

    payload = {"corpus_version": CORPUS_VERSION, "scenarios": list(SCENARIOS),
               "clock": {"time": WALL, "monotonic": MONOTONIC, "perf_counter": MONOTONIC},
               "trace": trace, "independent_observations": observations}
    inputs = [{k: value for k, value in row.items() if k != "output"} for row in trace]
    artifact = {"metadata": {"original_head": BASE_HEAD, "mode": mode,
                             "test_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                             "semantic_extraction_sha256": hashlib.sha256(Path(extraction.__file__).read_bytes()).hexdigest(),
                             "excluded_code_identity": excluded_code_hashes},
                "inputs_sha256": _digest(inputs), "meaning_sha256": _digest(payload), "payload": payload}
    if mode == "seal":
        assert not hasattr(extraction, "assembly"), "Seal only the unchanged original owner"
        assert artifact["metadata"]["semantic_extraction_sha256"] == ORIGINAL_EXTRACTION_SHA256
        with reference.open("x") as stream:
            json.dump(artifact, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
    else:
        sealed = json.loads(reference.read_text())
        assert sealed["metadata"]["mode"] == "seal"
        assert sealed["metadata"]["original_head"] == BASE_HEAD
        assert sealed["metadata"]["test_source_sha256"] == artifact["metadata"]["test_source_sha256"]
        observed = reference.with_name(reference.stem + ".observed.json")
        with observed.open("x") as stream:
            json.dump(artifact, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
        assert sealed["inputs_sha256"] == artifact["inputs_sha256"]
        assert sealed["payload"] == payload
