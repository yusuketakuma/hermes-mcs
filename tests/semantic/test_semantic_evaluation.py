"""Offline WP08 evaluation contracts; every fixture is synthetic."""
import copy
import json
import pathlib

import pytest

import semantic_evaluation as evaluation


MANIFEST = {
    "version": "manifest-v1",
    "bundle_version": "bundle-v1",
    "candidate_version": "candidate-v1",
    "label_version": "label-v1",
}
CRITERIA = {
    "version": "criteria-v1",
    "required_metrics": list(evaluation.METRICS),
    "minimums": {
        "important_fact_recall": 1.0,
        "final_recall": 1.0,
        "medication_recall": 1.0,
        "negation_recall": 1.0,
        "time_recall": 1.0,
        "speaker_relation_recall": 1.0,
        "loop_conformity": 1.0,
        "loop_precision": 1.0,
        "mandatory_fact_recall": 1.0,
        "rendered_fact_recall": 1.0,
        "delivered_fact_recall": 1.0,
    },
    "maximums": {
        "critical_overclaim": 0.0,
        "loop_false_resolution": 0.0,
        "loop_unresolved_miss_rate": 0.0,
        "defer_rate": 0.0,
        "silent_drop": 0.0,
    },
    "required_splits": ["test"],
    "min_human_labels": 1,
}


def _record(source="synthetic"):
    return {
        "case_id": "case-1",
        "split": "test",
        "account_id": "account-1",
        "project_id": "project-1",
        "thread_id": "thread-1",
        "bundle": {
            "version": "bundle-v1",
            "messages": [{"message_id": "m0"}, {"message_id": "m1"}],
            "attachments": [{
                "attachment_id": "att-1", "message_id": "m1",
                "path": "fixtures/attachment-1.bin",
                "context_before": ["m0"],
            }],
            "body_original": "ORIGINAL-SECRET-MUST-NOT-LEAK",
        },
        "candidate": {
            "version": "candidate-v1",
            "facts": [
                {"fact_id": "f1", "important": True,
                 "medication": "drug-a", "negation": "affirmed",
                 "time": "tomorrow", "speaker_relation": "doctor",
                 "evidence_ids": ["ev-1"]},
                {"fact_id": "f2", "important": True,
                 "medication": "drug-b", "negation": "negated",
                 "time": "today", "speaker_relation": "nurse",
                 "evidence_ids": ["ev-2"]},
            ],
            "rendered_fact_ids": ["f1", "f2"],
            "delivered_fact_ids": ["f1", "f2"],
            "relations": [{"left_fact_id": "f1", "right_fact_id": "f2",
                           "type": "COMPLEMENTS"}],
            "unresolved": [],
            "claims": [
                {"claim_id": "c1", "critical": False,
                 "fact_refs": ["f1"], "attachment_refs": ["att-1"]},
                {"claim_id": "c2", "critical": False,
                 "fact_refs": ["f2"], "attachment_refs": []},
            ],
            "loops": [{"loop_id": "loop-1", "resolved": False}],
            "status": "complete",
            "latency_ms": 100,
            "usage": {"requests": 2, "input_tokens": 10,
                       "output_tokens": 5, "total_tokens": 15},
        },
        "label": {
            "version": "label-v1",
            "source": source,
            **({"receipt": {"receipt_id": "rcpt-1",
                            "labelled_at": "2026-09-21",
                            "reviewer": "reviewer-1"}}
               if source == "human" else {}),
            "facts": [
                {"fact_id": "f1", "important": True, "mandatory": True,
                 "medication": "drug-a", "negation": "affirmed",
                 "time": "tomorrow", "speaker_relation": "doctor"},
                {"fact_id": "f2", "important": True,
                 "medication": "drug-b", "negation": "negated",
                 "time": "today", "speaker_relation": "nurse"},
            ],
            "relations": [{"left_fact_id": "f1", "right_fact_id": "f2",
                           "type": "COMPLEMENTS"}],
            "claims": [
                {"claim_id": "c1", "critical": True, "supported": True,
                 "final": True, "covered_gold_fact_ids": ["f1"]},
                {"claim_id": "c2", "critical": False, "supported": True,
                 "final": True, "covered_gold_fact_ids": ["f2"]},
            ],
            "loops": [{"loop_id": "loop-1", "resolved": False}],
        },
    }


def test_exact_fixture_scores_and_synthetic_labels_do_not_pass_g6():
    record = _record()
    report = evaluation.evaluate_records([record], MANIFEST, CRITERIA)
    assert report["metrics"]["important_fact_recall"]["denominator"] == 2
    assert report["metrics"]["final_recall"]["correct"] == 2
    assert report["metrics"]["critical_overclaim"]["errors"] == 0
    assert report["metrics"]["loop_false_resolution"]["errors"] == 0
    assert report["latency"]["p50_ms"] == 100.0
    assert report["usage"]["input_tokens"]["total"] == 10
    assert not report["gate"]["pass"]
    assert not report["gate"]["g6_eligible"]
    assert "human_labels_insufficient" in report["gate"]["reasons"]
    assert "ORIGINAL-SECRET" not in json.dumps(report, ensure_ascii=False)


def test_human_label_schema_can_pass_explicit_criteria():
    report = evaluation.evaluate_records([_record("human")], MANIFEST, CRITERIA)
    assert report["gate"] == {"pass": True, "reasons": [],
                              "g6_eligible": True}


def test_cross_split_identity_is_rejected():
    second = copy.deepcopy(_record())
    second["case_id"] = "case-2"
    second["split"] = "dev"
    with pytest.raises(evaluation.EvaluationError, match="account_id_cross_split"):
        evaluation.evaluate_records([_record(), second], MANIFEST, CRITERIA)


def test_zero_denominators_cannot_pass_gate():
    record = _record("human")
    record["label"]["facts"] = []
    record["label"]["claims"] = []
    record["label"]["loops"] = []
    record["label"]["relations"] = []
    record["candidate"]["facts"] = []
    record["candidate"]["claims"] = []
    record["candidate"]["loops"] = []
    record["candidate"]["relations"] = []
    record["candidate"]["rendered_fact_ids"] = []
    record["candidate"]["unresolved"] = []
    report = evaluation.evaluate_records([record], MANIFEST, CRITERIA)
    assert not report["gate"]["pass"]
    assert "denominator_zero:important_fact_recall" in report["gate"]["reasons"]
    assert "denominator_zero:loop_conformity" in report["gate"]["reasons"]


def test_critical_overclaim_is_always_a_gate_failure():
    record = _record("human")
    record["candidate"]["claims"][0]["critical"] = False
    record["label"]["claims"][0]["supported"] = False
    record["label"]["claims"][0]["covered_gold_fact_ids"] = []
    report = evaluation.evaluate_records([record], MANIFEST, CRITERIA)
    assert report["metrics"]["critical_overclaim"]["errors"] == 1
    assert not report["gate"]["pass"]
    assert "critical_overclaim" in report["gate"]["reasons"]


def test_swapped_drug_with_valid_fact_ref_uses_human_claim_label():
    record = _record("human")
    record["candidate"]["facts"][0]["medication"] = "wrong-drug"
    record["candidate"]["claims"][0]["critical"] = False
    record["candidate"]["final_fact_ids"] = ["f1", "f2"]
    record["label"]["claims"][0]["supported"] = False
    record["label"]["claims"][0]["covered_gold_fact_ids"] = []
    report = evaluation.evaluate_records([record], MANIFEST, CRITERIA)
    assert report["metrics"]["final_recall"]["correct"] == 1
    assert report["metrics"]["critical_overclaim"]["errors"] == 1
    assert not report["gate"]["pass"]


def test_gate_uses_held_out_test_metrics():
    records = []
    for index in range(9):
        record = _record("human")
        record["case_id"] = f"dev-{index}"
        record["split"] = "dev"
        record["account_id"] = f"account-dev-{index}"
        record["project_id"] = f"project-dev-{index}"
        record["thread_id"] = f"thread-dev-{index}"
        records.append(record)
    heldout = _record("human")
    heldout["label"]["claims"][0]["supported"] = False
    heldout["label"]["claims"][0]["covered_gold_fact_ids"] = []
    records.append(heldout)
    criteria = copy.deepcopy(CRITERIA)
    criteria["minimums"]["final_recall"] = 0.9
    report = evaluation.evaluate_records(records, MANIFEST, criteria)
    assert report["metrics"]["final_recall"]["rate"] > 0.9
    assert report["splits"]["test"]["metrics"]["final_recall"]["rate"] == 0.5
    assert "below_threshold:final_recall" in report["gate"]["reasons"]


def test_attachment_paths_are_relative_and_never_opened():
    record = _record()
    record["bundle"]["attachments"][0]["path"] = "../outside.bin"
    with pytest.raises(evaluation.EvaluationError, match="attachment_path"):
        evaluation.evaluate_records([record], MANIFEST, CRITERIA)


def test_attachment_context_must_precede_its_message():
    record = _record()
    record["bundle"]["attachments"][0]["context_before"] = ["m1"]
    with pytest.raises(evaluation.EvaluationError,
                       match="attachment_context_not_before"):
        evaluation.evaluate_records([record], MANIFEST, CRITERIA)


def test_cli_writes_only_aggregate_report(tmp_path):
    cases = tmp_path / "cases.jsonl"
    manifest = tmp_path / "manifest.json"
    criteria = tmp_path / "criteria.json"
    output = tmp_path / "report.json"
    cases.write_text(json.dumps(_record(), ensure_ascii=False) + "\n",
                     encoding="utf-8")
    manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
    criteria.write_text(json.dumps(CRITERIA), encoding="utf-8")
    assert evaluation.main(["--input", str(cases), "--manifest", str(manifest),
                            "--criteria", str(criteria), "--output", str(output)]) == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["schema_version"] == evaluation.SCHEMA_VERSION
    assert "ORIGINAL-SECRET" not in output.read_text(encoding="utf-8")


def test_extra_loop_candidate_cannot_disappear_from_quality_gate():
    record = _record("human")
    record["candidate"]["loops"].append({"loop_id": "invented", "resolved": True})
    report = evaluation.evaluate_records([record], MANIFEST, CRITERIA)
    metric = report["metrics"]["loop_conformity"]
    assert metric["correct"] == 1
    assert metric["denominator"] == 2
    assert not report["gate"]["pass"]


@pytest.mark.parametrize("prediction,precision,missed", [
    ([{"loop_id": "loop-1", "resolved": False},
      {"loop_id": "extra", "resolved": False}], 0.5, 0),
    ([], None, 1),
    ([{"loop_id": "loop-1", "resolved": True}], 0.0, 1),
])
def test_loop_precision_and_unresolved_misses_are_distinct(prediction, precision, missed):
    record = _record("human")
    record["candidate"]["loops"] = prediction
    report = evaluation.evaluate_records([record], MANIFEST, CRITERIA)
    assert report["metrics"]["loop_precision"]["rate"] == precision
    metric = report["metrics"]["loop_unresolved_miss_rate"]
    assert metric["denominator"] == 1
    assert metric["errors"] == missed
    assert not report["gate"]["pass"]


@pytest.mark.parametrize("missing", ["latency_ms", "usage", "requests"])
def test_missing_case_telemetry_cannot_be_hidden_by_other_cases(missing):
    first = _record("human")
    second = copy.deepcopy(first)
    second["case_id"] = "case-2"
    if missing == "requests":
        second["candidate"]["usage"].pop("requests")
    else:
        second["candidate"].pop(missing)
    report = evaluation.evaluate_records([first, second], MANIFEST, CRITERIA)
    assert not report["gate"]["pass"]
    assert any(reason.startswith("telemetry_missing:")
               for reason in report["gate"]["reasons"])


def test_omitted_metric_list_uses_all_supported_metrics():
    criteria = copy.deepcopy(CRITERIA)
    criteria.pop("required_metrics")
    normalized = evaluation.validate_criteria(criteria)
    assert normalized["required_metrics"] == list(evaluation.METRICS)
    assert evaluation.evaluate_records([_record("human")], MANIFEST, criteria)["gate"]["pass"]


def test_runtime_aggregation_preserves_run_account_and_patient_boundaries():
    def run(account, rid, wall, jobs):
        return {"account_id": account, "result": {
            "run_id": rid, "elapsed_s": wall, "semantic": {
                "mode": "shadow", "elapsed_s": 20,
                "oldest_pending_job_age_s": 8, "job_metrics": [
                    {"job_id": i, "project_id": pid, "elapsed_s": seconds,
                     "jev_requests": requests}
                    for i, (pid, seconds, requests) in enumerate(jobs)]}}}
    rows = [run("a", 1, 100, [(1, 2, 1), (1, 4, 2), (2, 10, 4)]),
            run("b", 1, 200, [(1, 12, 5)]),
            {"account_id": "a", "result": {"run_id": 2}}]
    metrics = evaluation.evaluate_runs(rows)["metrics"]
    assert metrics["patient_semantic_work_s"]["denominator"] == 3
    assert metrics["patient_semantic_work_s"]["p50"] == 10
    assert metrics["patient_semantic_work_s"]["p95"] == pytest.approx(11.8)
    assert metrics["patient_jev_requests"]["total"] == 12
    assert metrics["run_elapsed_s"]["p50"] == 150
    assert metrics["run_elapsed_s"]["missing"] == 1
    with pytest.raises(evaluation.EvaluationError, match="duplicate_run"):
        evaluation.evaluate_runs(rows + [rows[0]])
    rows[0]["result"]["semantic"]["job_metrics"][0]["elapsed_s"] = float("nan")
    with pytest.raises(evaluation.EvaluationError, match="job_elapsed_s_invalid"):
        evaluation.evaluate_runs(rows)


def test_runtime_token_totals_expose_partial_usage_without_zero_imputation():
    jobs = [
        {"job_id": 1, "project_id": 1, "elapsed_s": 1, "jev_requests": 2,
         "usage": {"input_tokens": 10, "output_tokens": 2,
                   "reported_requests": 2, "unreported_requests": 0}},
        {"job_id": 2, "project_id": 2, "elapsed_s": 1, "jev_requests": 2,
         "usage": {"input_tokens": 7, "output_tokens": 3,
                   "reported_requests": 1, "unreported_requests": 1}},
        {"job_id": 3, "project_id": 3, "elapsed_s": 1, "jev_requests": 1}]
    rows = [{"account_id": "a", "result": {"run_id": 1, "elapsed_s": 4,
             "semantic": {"mode": "shadow", "elapsed_s": 3, "job_metrics": jobs}}}]
    usage = evaluation.evaluate_runs(rows)["jev_usage"]
    assert usage["patient"]["complete_observations"] == 1
    assert usage["patient"]["tokens"]["input_tokens"]["complete_p50"] == 10
    assert usage["run"]["tokens"]["input_tokens"]["observed_total"] == 17
    assert usage["run"]["tokens"]["input_tokens"]["complete_p50"] is None
    assert usage["run"]["unreported_requests"] == 2
    assert usage["run"]["missing_jobs"] == 1
    jobs[0]["usage"]["reported_requests"] = 3
    with pytest.raises(evaluation.EvaluationError, match="job_usage_request_mismatch"):
        evaluation.evaluate_runs(rows)


def test_human_minimum_cannot_be_filled_by_calibration_cases():
    report = evaluation.evaluate_records([_record("human")], MANIFEST, CRITERIA)
    criteria = evaluation.validate_criteria(dict(CRITERIA, min_human_labels=2))
    gate = evaluation._gate(report, criteria, {"human": 200, "synthetic": 0})
    assert "human_labels_insufficient" in gate["reasons"]


def test_report_binds_actual_criteria_even_if_version_is_reused():
    first = evaluation.evaluate_records([_record("human")], MANIFEST, CRITERIA)
    changed = copy.deepcopy(CRITERIA)
    changed["min_human_labels"] = CRITERIA.get("min_human_labels", 1) + 1
    second = evaluation.evaluate_records([_record("human")], MANIFEST, changed)
    assert first["criteria_version"] == second["criteria_version"]
    assert first["criteria_sha256"] != second["criteria_sha256"]
    assert second["criteria"] == evaluation.validate_criteria(changed)
    assert first["criteria_sha256"] == evaluation.evaluate_records(
        [_record("human")], MANIFEST, dict(reversed(list(CRITERIA.items()))))["criteria_sha256"]


def _facts(prefix, n, mandatory=True, important=True):
    return [{"fact_id": f"{prefix}-{i}", "important": important,
             "mandatory": mandatory, "medication": f"drug-{i}",
             "negation": "affirmed", "time": "today",
             "speaker_relation": "nurse", "evidence_ids": [f"ev-{i}"]}
            for i in range(n)]


def _claim_pair(fid, index=0):
    """One critical+final claim pair (label + candidate) over `fid`."""
    return (
        {"claim_id": f"c-{fid}", "critical": index == 0,
         "supported": True, "final": True,
         "covered_gold_fact_ids": [fid]},
        {"claim_id": f"c-{fid}", "critical": index == 0,
         "fact_refs": [fid], "attachment_refs": []},
    )


def _set_claims(record, fids):
    label_claims, candidate_claims = [], []
    for i, fid in enumerate(fids):
        lc, cc = _claim_pair(fid, i)
        label_claims.append(lc)
        candidate_claims.append(cc)
    record["label"]["claims"] = label_claims
    record["candidate"]["claims"] = candidate_claims


def test_missing_41st_fact_fails_gate():
    """T6 failure axis: a mandatory fact dropped between extraction and
    render must break the gate. The candidate predicts all 41 but lists
    only 40 rendered ids — rendered recall sees the missing one."""
    record = _record("human")
    record["label"]["facts"] = [
        {**f, "mandatory": True} for f in _facts("f", 41)]
    record["candidate"]["facts"] = _facts("f", 41)
    rel = {"left_fact_id": "f-0", "right_fact_id": "f-1",
           "type": "COMPLEMENTS"}
    record["label"]["relations"] = [dict(rel)]
    record["candidate"]["relations"] = [dict(rel)]
    _set_claims(record, [f"f-{i}" for i in range(41)])
    record["candidate"]["rendered_fact_ids"] = [f"f-{i}" for i in range(40)]
    record["candidate"]["delivered_fact_ids"] = [f"f-{i}" for i in range(41)]
    report = evaluation.evaluate_records([record], MANIFEST, CRITERIA)
    rendered = report["metrics"]["rendered_fact_recall"]
    assert rendered["correct"] == 40 and rendered["denominator"] == 41
    assert not report["gate"]["pass"]
    assert "below_threshold:rendered_fact_recall" in report["gate"]["reasons"]


def test_unpredicted_41st_fact_is_silent_drop():
    """Same class, harder failure: the fact never reached the candidate
    AND was never listed unresolved -> silent_drop, the exact 'cannot
    disappear' property the 40-item cap violated."""
    record = _record("human")
    record["label"]["facts"] = [
        {**f, "mandatory": True} for f in _facts("f", 41)]
    record["candidate"]["facts"] = _facts("f", 40)
    rel = {"left_fact_id": "f-0", "right_fact_id": "f-1",
           "type": "COMPLEMENTS"}
    record["label"]["relations"] = [dict(rel)]
    record["candidate"]["relations"] = [dict(rel)]
    _set_claims(record, [f"f-{i}" for i in range(40)])
    record["candidate"]["rendered_fact_ids"] = [f"f-{i}" for i in range(40)]
    record["candidate"]["delivered_fact_ids"] = [f"f-{i}" for i in range(40)]
    report = evaluation.evaluate_records([record], MANIFEST, CRITERIA)
    drop = report["metrics"]["silent_drop"]
    assert drop["errors"] == 1 and drop["denominator"] == 41
    assert "above_threshold:silent_drop" in report["gate"]["reasons"]


def test_undelivered_mandatory_fact_breaks_delivery_chain():
    """verified->rendered->delivered: a fact rendered but absent from
    delivered_fact_ids fails the chain — no silent success."""
    record = _record("human")
    record["candidate"]["delivered_fact_ids"] = ["f2"]  # f1 undelivered
    criteria = copy.deepcopy(CRITERIA)
    criteria["minimums"]["delivered_fact_recall"] = 1.0
    report = evaluation.evaluate_records([record], MANIFEST, criteria)
    metric = report["metrics"]["delivered_fact_recall"]
    assert metric["correct"] == 0 and metric["denominator"] == 1
    assert not report["gate"]["pass"]
    assert "below_threshold:delivered_fact_recall" \
        in report["gate"]["reasons"]


def test_fake_human_label_without_receipt_rejected():
    """A bare source='human' string cannot mint provenance — human
    labels must bind a durable labelling receipt."""
    record = _record("human")
    del record["label"]["receipt"]
    with pytest.raises(evaluation.EvaluationError,
                       match="label_human_receipt_required"):
        evaluation.evaluate_records([record], MANIFEST, CRITERIA)


@pytest.mark.parametrize("receipt", [
    {"receipt_id": "", "labelled_at": "2026-09-21", "reviewer": "r1"},
    {"receipt_id": "rcpt-1", "labelled_at": None, "reviewer": "r1"},
    {"receipt_id": "rcpt-1", "labelled_at": "2026-09-21"},
    "a receipt id",
])
def test_malformed_human_receipt_rejected(receipt):
    record = _record("human")
    record["label"]["receipt"] = receipt
    with pytest.raises(evaluation.EvaluationError,
                       match="label_human_receipt"):
        evaluation.evaluate_records([record], MANIFEST, CRITERIA)


def test_g6_file_minimum_needs_200_human_provenance_rows():
    """The shipped criteria file means it: fewer than 200 human-labelled
    held-out cases can never pass, however good the metrics look."""
    criteria_file = pathlib.Path(__file__).resolve().parents[2] \
        / "evaluation" / "g6-criteria-v1.json"
    criteria = json.loads(criteria_file.read_text(encoding="utf-8"))
    manifest = dict(MANIFEST)
    records = []
    for i in range(199):
        rec = copy.deepcopy(_record("human"))
        rec["case_id"] = f"case-{i}"
        rec["label"]["receipt"]["receipt_id"] = f"rcpt-{i}"
        records.append(rec)
    report = evaluation.evaluate_records(records, manifest, criteria)
    assert not report["gate"]["pass"]
    assert "human_labels_insufficient" in report["gate"]["reasons"]


@pytest.mark.parametrize("value", [None, "true", 1])
def test_unannotated_importance_cannot_shrink_recall_denominator(value):
    record = _record("human")
    if value is None:
        record["label"]["facts"][0].pop("important")
    else:
        record["label"]["facts"][0]["important"] = value
    with pytest.raises(evaluation.EvaluationError, match="label_fact_importance_required"):
        evaluation.evaluate_records([record], MANIFEST, CRITERIA)
