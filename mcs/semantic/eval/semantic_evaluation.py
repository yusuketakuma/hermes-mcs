#!/usr/bin/env python3
"""Offline evaluation for fixed semantic bundles and reviewed labels.

The scoring CLI consumes supplied JSONL records without opening bundle
attachment paths, calling a model, or emitting source text. Explicit injected
pipeline drivers separately exercise synthetic extraction and audit callbacks.
A record is the unit of split isolation:
``case_id``, ``account_id``, ``project_id`` and ``thread_id`` must not cross
dev, calibration, and test.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path, PureWindowsPath

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every subdir holding .py files (any depth) as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import _mcs_path  # noqa: F401

from semantic_acceptance import METRICS, ADDITIONAL_METRICS, evaluate_gate as _gate
from semantic_facts import RELATION_TYPES
from safe_io import atomic_write
from mcs_util import payload_hash
import stats_math


SCHEMA_VERSION = "semantic-evaluation/v3"
SPLITS = ("dev", "calibration", "test")
LIFECYCLE_STAGES = ("verified", "rendered", "delivered")
ATTRIBUTE_FIELDS = ("medication", "negation", "time", "speaker_relation")
_MISSING = object()


class EvaluationError(ValueError):
    """Input or acceptance criteria are not safe to evaluate."""


def evaluation_id(value, field: str) -> str:
    if value is None:
        raise EvaluationError(f"{field}_missing")
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise EvaluationError(f"{field}_invalid")
    value = str(value).strip()
    if not value:
        raise EvaluationError(f"{field}_missing")
    return value


def evaluation_object(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise EvaluationError(f"{field}_object_required")
    return value


def _list(value, field: str) -> list:
    if not isinstance(value, list):
        raise EvaluationError(f"{field}_list_required")
    return value


def nonnegative_number(value, field: str, integer: bool = False) -> float | int:
    if type(value) not in (int, float) or (integer and type(value) is not int):
        raise EvaluationError(f"{field}_invalid")
    try:
        number = float(value)
    except OverflowError:
        raise EvaluationError(f"{field}_invalid") from None
    if not math.isfinite(number) or value < 0:
        raise EvaluationError(f"{field}_invalid")
    return value if integer else number


def success_metric(correct: int, denominator: int) -> dict:
    errors = max(0, denominator - correct)
    return {
        "correct": correct,
        "errors": errors,
        "denominator": denominator,
        "rate": correct / denominator if denominator else None,
        "wilson95": stats_math.wilson_interval(correct, denominator),
    }


def _error_metric(errors: int, denominator: int) -> dict:
    errors = max(0, errors)
    correct = max(0, denominator - errors)
    return {
        "correct": correct,
        "errors": errors,
        "denominator": denominator,
        "rate": errors / denominator if denominator else None,
        "wilson95": stats_math.wilson_interval(errors, denominator),
    }


def validated_percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    for value in values:
        nonnegative_number(value, "percentile_value")
    return stats_math.percentile(values, p)


def _eval_member(case: dict, msg: dict, cid: str) -> dict:
    """Synthetic ledger-member input for an eval message — one shape
    shared by every benchmark harness."""
    return {"project_id": case.get("project_id", "eval"),
            "message_id": msg.get("message_id", cid),
            "revision": str(msg.get("revision", "r1")),
            "body_original": msg.get("body", ""),
            "body_state": "full",
            "posted_at": msg.get("posted_at", ""),
            "sender": {"id": "eval", "type": "staff", "profession": ""}}


def _case_messages(case: dict, cid: str) -> list:
    """A case's messages; a single-body case becomes one message."""
    return case.get("messages") or [
        {"message_id": cid, "revision": "r1", "body": case.get("body", "")}]


def _item_id(item: dict, field: str) -> str:
    return evaluation_id(item.get(field), field)


def _items(value, field: str) -> list[dict]:
    rows = _list(value, field)
    if any(not isinstance(row, dict) for row in rows):
        raise EvaluationError(f"{field}_item_object_required")
    return rows


def _validate_relative_path(value, field: str) -> str:
    path = evaluation_id(value, field)
    if ("\x00" in path or path.startswith("/") or path.startswith("\\")
            or path.startswith("~") or PureWindowsPath(path).is_absolute()
            or PureWindowsPath(path).drive):
        raise EvaluationError(f"{field}_must_be_relative")
    parts = Path(path).parts + PureWindowsPath(path).parts
    if ".." in parts:
        raise EvaluationError(f"{field}_must_be_relative")
    return path


def validate_manifest(manifest: dict) -> dict:
    """Validate the fixed version manifest without echoing arbitrary fields."""
    manifest = evaluation_object(manifest, "manifest")
    for field in ("version", "bundle_version", "candidate_version"):
        evaluation_id(manifest.get(field), f"manifest_{field}")
    if manifest.get('data_origin', 'synthetic') not in ('synthetic', 'real_authorized'):
        raise EvaluationError('manifest_data_origin_invalid')
    if manifest.get('source') == 'fully_synthetic' and manifest.get('data_origin', 'synthetic') != 'synthetic':
        raise EvaluationError('manifest_data_origin_mismatch')
    if "label_version" in manifest:
        evaluation_id(manifest["label_version"], "manifest_label_version")
    if "splits" in manifest:
        splits = _list(manifest["splits"], "manifest_splits")
        if any(split not in SPLITS for split in splits):
            raise EvaluationError("manifest_splits_invalid")
    return manifest


def validate_criteria(criteria: dict) -> dict:
    criteria = evaluation_object(criteria, "criteria")
    evaluation_id(criteria.get("version"), "criteria_version")
    required = criteria.get("required_metrics", list(METRICS))
    required = _list(required, "criteria_required_metrics")
    if not required or any(not isinstance(x, str) for x in required):
        raise EvaluationError("criteria_required_metrics_invalid")
    if any(name not in METRICS + ADDITIONAL_METRICS for name in required):
        raise EvaluationError("criteria_required_metrics_invalid")
    minimums = evaluation_object(criteria.get("minimums") or {}, "criteria_minimums")
    maximums = evaluation_object(criteria.get("maximums") or {}, "criteria_maximums")
    if any(name not in METRICS + ADDITIONAL_METRICS for name in minimums) \
            or any(name not in METRICS + ADDITIONAL_METRICS for name in maximums):
        raise EvaluationError("criteria_metric_invalid")
    for value in (*minimums.values(), *maximums.values()):
        if isinstance(value, bool) or not isinstance(value, int | float) \
                or not 0 <= value <= 1:
            raise EvaluationError("criteria_threshold_invalid")
    splits = criteria.get("required_splits", ["test"])
    splits = _list(splits, "criteria_required_splits")
    if any(split not in SPLITS for split in splits):
        raise EvaluationError("criteria_required_splits_invalid")
    if "test" not in splits:
        raise EvaluationError("criteria_test_split_required")
    human_min = criteria.get("min_human_labels", 1)
    if type(human_min) is not int or human_min < 1:
        raise EvaluationError("criteria_min_human_labels_invalid")
    extras = {}
    for field in ('denominator_minimums', 'strata_minimums'):
        if field in criteria:
            values = evaluation_object(criteria[field], field)
            if any(type(v) is not int or v < 1 for v in values.values()):
                raise EvaluationError(field + '_invalid')
            extras[field] = values
    for field in ('min_patients', 'min_threads'):
        if field in criteria:
            if type(criteria[field]) is not int or criteria[field] < 1:
                raise EvaluationError(field + '_invalid')
            extras[field] = criteria[field]
    if 'sealed_holdout' in criteria:
        if criteria['sealed_holdout'] is not True:
            raise EvaluationError('sealed_holdout_invalid')
        extras['sealed_holdout'] = True
    if 'wilson_metrics' in criteria:
        values = _list(criteria['wilson_metrics'], 'wilson_metrics')
        if any(v not in required for v in values):
            raise EvaluationError('wilson_metrics_invalid')
        extras['wilson_metrics'] = values
    return {
        **extras,
        "version": evaluation_id(criteria["version"], "criteria_version"),
        "required_metrics": required,
        "minimums": {k: float(v) for k, v in minimums.items()},
        "maximums": {k: float(v) for k, v in maximums.items()},
        "required_splits": splits,
        "min_human_labels": human_min,
    }


def _record_identity(record: dict, field: str) -> str:
    return evaluation_id(record.get(field), field)


def _validate_bundle(bundle: dict, record: dict, manifest: dict) -> set:
    evaluation_object(bundle, "bundle")
    if evaluation_id(bundle.get("version"), "bundle_version") != manifest["bundle_version"]:
        raise EvaluationError("bundle_version_mismatch")
    for field in ("account_id", "project_id", "thread_id"):
        if field in bundle and evaluation_id(bundle[field], field) != \
                _record_identity(record, field):
            raise EvaluationError(f"bundle_{field}_mismatch")
    messages = bundle.get("messages", [])
    if not isinstance(messages, list):
        raise EvaluationError("bundle_messages_list_required")
    if any(not isinstance(message, dict) for message in messages):
        raise EvaluationError("bundle_message_object_required")
    message_ids = [evaluation_id(m.get("message_id"), "message_id")
                   for m in messages if isinstance(m, dict)]
    if len(message_ids) != len(set(message_ids)):
        raise EvaluationError("message_id_duplicate")
    message_set = set(message_ids)
    message_order = {message_id: index
                     for index, message_id in enumerate(message_ids)}
    attachments = bundle.get("attachments", [])
    attachments = _items(attachments, "bundle_attachments")
    attachment_ids = set()
    for attachment in attachments:
        aid = _item_id(attachment, "attachment_id")
        if aid in attachment_ids:
            raise EvaluationError("attachment_id_duplicate")
        attachment_ids.add(aid)
        _validate_relative_path(attachment.get("path"), "attachment_path")
        mid = evaluation_id(attachment.get("message_id"), "attachment_message_id")
        if message_set and mid not in message_set:
            raise EvaluationError("attachment_message_missing")
        before = attachment.get("context_before", [])
        if not isinstance(before, list):
            raise EvaluationError("attachment_context_before_list_required")
        before_ids = [evaluation_id(value, "attachment_context_message_id")
                      for value in before]
        if (message_set and any(value not in message_set for value in before_ids)):
            raise EvaluationError("attachment_context_message_missing")
        if (mid in message_order
                and any(message_order[value] >= message_order[mid]
                        for value in before_ids)):
            raise EvaluationError("attachment_context_not_before")
    return attachment_ids


def _validate_candidate(candidate: dict, manifest: dict, attachment_ids: set) -> None:
    evaluation_object(candidate, "candidate")
    if 'ai_priority' in candidate and type(candidate['ai_priority']) is not bool:
        raise EvaluationError('candidate_ai_priority_invalid')
    if 'urgency' in candidate and candidate['urgency'] not in ('high', 'routine', 'unclear'):
        raise EvaluationError('candidate_urgency_invalid')
    if evaluation_id(candidate.get("version"), "candidate_version") != \
            manifest["candidate_version"]:
        raise EvaluationError("candidate_version_mismatch")
    for field in ("facts", "claims", "loops"):
        if field in candidate:
            _items(candidate[field], f"candidate_{field}")
    usage = candidate.get("usage")
    if usage is not None:
        usage = evaluation_object(usage, "candidate_usage")
        for key in ("requests", "input_tokens",
                    "output_tokens", "total_tokens"):
            if key in usage:
                nonnegative_number(usage[key], f"candidate_usage_{key}", integer=True)
    latency = candidate.get("latency_ms")
    if latency is not None:
        nonnegative_number(latency, "candidate_latency_ms")
    status = candidate.get("status")
    if not isinstance(status, str) or not status.strip():
        raise EvaluationError("candidate_status_missing")
    facts = _items(candidate.get("facts", []), "candidate_facts")
    claims = _items(candidate.get("claims", []), "candidate_claims")
    claim_ids = [_item_id(row, "claim_id") for row in claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise EvaluationError("claim_id_duplicate")
    for row in facts + claims:
        for ref in _list(row.get("evidence_ids", []), "candidate_evidence_ids"):
            evaluation_id(ref, "candidate_evidence_id")
        if "fact_refs" in row and not isinstance(row["fact_refs"], list):
            raise EvaluationError("candidate_fact_refs_list_required")
        refs = row.get("attachment_refs", [])
        if not isinstance(refs, list):
            raise EvaluationError("candidate_attachment_refs_list_required")
        for ref in row.get("fact_refs", []):
            evaluation_id(ref, "fact_ref")
        if any(evaluation_id(ref, "attachment_ref") not in attachment_ids
               for ref in refs):
            raise EvaluationError("candidate_attachment_ref_unknown")
    for row in _items(candidate.get("loops", []), "candidate_loops"):
        _item_id(row, "loop_id")
        if type(row.get("resolved")) is not bool:
            raise EvaluationError("candidate_loop_resolved_required")
    # verified -> rendered -> delivered chain (T6): every stage's id
    # set follows one contract — same shape, same dedupe. An absent list
    # is a missing observation, scored as nothing reached that stage
    # rather than skipped.
    for stage in LIFECYCLE_STAGES:
        ids = candidate.get(f"{stage}_fact_ids", [])
        if not isinstance(ids, list):
            raise EvaluationError(f"candidate_{stage}_fact_ids_list_required")
        if any(not isinstance(ref, str) or not ref.strip() for ref in ids):
            raise EvaluationError(f"candidate_{stage}_fact_id_invalid")
        if len(ids) != len(set(ids)):
            raise EvaluationError(f"candidate_{stage}_fact_id_duplicate")
    for row in _items(candidate.get("relations", []), "candidate_relations"):
        left = evaluation_id(row.get("left_fact_id"), "candidate_relation_left")
        right = evaluation_id(row.get("right_fact_id"), "candidate_relation_right")
        if left == right:
            raise EvaluationError("candidate_relation_self_loop")
        if row.get("type") not in RELATION_TYPES:
            raise EvaluationError("candidate_relation_type_invalid")
    for row in _items(candidate.get("unresolved", []),
                      "candidate_unresolved"):
        evaluation_id(row.get("fact_ref"), "candidate_unresolved_fact_ref")


def _validate_label(label, manifest: dict, candidate: dict) -> str:
    if label is None:
        return "missing"
    label = evaluation_object(label, "label")
    if 'ai_priority' in label and type(label['ai_priority']) is not bool:
        raise EvaluationError('label_ai_priority_invalid')
    if 'urgency' in label and label['urgency'] not in ('high', 'routine', 'unclear'):
        raise EvaluationError('label_urgency_invalid')
    source = label.get("source")
    if source not in ("human", "synthetic"):
        raise EvaluationError("label_source_invalid")
    if source == "human":
        # A bare 'human' string cannot mint provenance: the label must
        # bind a durable labelling receipt. The schema checks receipt
        # presence/shape — it cannot prove authenticity; that remains
        # the human review process described in annotation-guide.md.
        receipt = label.get("receipt")
        if not isinstance(receipt, dict):
            raise EvaluationError("label_human_receipt_required")
        for field in ("receipt_id", "labelled_at", "reviewer"):
            if not isinstance(receipt.get(field), str) \
                    or not receipt[field].strip():
                raise EvaluationError(f"label_human_receipt_{field}")
    if "label_version" in manifest and label.get("version") \
            != manifest["label_version"]:
        raise EvaluationError("label_version_mismatch")
    facts = _items(label.get("facts", []), "label_facts")
    if any(type(row.get("important")) is not bool for row in facts):
        raise EvaluationError("label_fact_importance_required")
    if any("mandatory" in row and type(row.get("mandatory")) is not bool
           for row in facts):
        raise EvaluationError("label_fact_mandatory_invalid")
    fact_ids = [_item_id(row, "fact_id") for row in facts]
    if len(fact_ids) != len(set(fact_ids)):
        raise EvaluationError("fact_id_duplicate")
    fact_id_set = set(fact_ids)
    for row in _items(label.get("relations", []), "label_relations"):
        left = evaluation_id(row.get("left_fact_id"), "label_relation_left")
        right = evaluation_id(row.get("right_fact_id"), "label_relation_right")
        if left not in fact_id_set or right not in fact_id_set:
            raise EvaluationError("label_relation_fact_unknown")
        if left == right:
            raise EvaluationError("label_relation_self_loop")
        if row.get("type") not in RELATION_TYPES:
            raise EvaluationError("label_relation_type_invalid")
    claims = _items(label.get("claims", []), "label_claims")
    candidate_claims = _items(candidate.get("claims", []),
                              "candidate_claims")
    candidate_claim_ids = {_item_id(row, "claim_id") for row in candidate_claims}
    label_claim_ids = [_item_id(row, "claim_id") for row in claims]
    if len(label_claim_ids) != len(set(label_claim_ids)):
        raise EvaluationError("label_claim_id_duplicate")
    if set(label_claim_ids) != candidate_claim_ids:
        raise EvaluationError("label_claims_mismatch")
    for row in claims:
        if type(row.get("critical")) is not bool:
            raise EvaluationError("label_claim_critical_required")
        if type(row.get("supported")) is not bool:
            raise EvaluationError("label_claim_supported_required")
        if type(row.get("final")) is not bool:
            raise EvaluationError("label_claim_final_required")
        covered = row.get("covered_gold_fact_ids")
        if not isinstance(covered, list):
            raise EvaluationError("label_claim_covered_facts_list_required")
        covered_ids = [evaluation_id(value, "covered_gold_fact_id") for value in covered]
        if len(covered_ids) != len(set(covered_ids)):
            raise EvaluationError("label_claim_covered_facts_duplicate")
        if any(value not in fact_id_set for value in covered_ids):
            raise EvaluationError("label_claim_covered_fact_unknown")
        if not row["supported"] and covered_ids:
            raise EvaluationError("label_claim_unsupported_coverage")
        if row["supported"] and not covered_ids:
            raise EvaluationError("label_claim_supported_coverage_required")
    for row in _items(label.get("loops", []), "label_loops"):
        _item_id(row, "loop_id")
        if type(row.get("resolved")) is not bool:
            raise EvaluationError("label_loop_resolved_required")
    return source


def validate_records(records: list[dict], manifest: dict) -> dict:
    """Validate records and reject split identity leakage before scoring."""
    seen_cases = set()
    seen = {field: {} for field in ("account_id", "project_id", "thread_id")}
    provenance = {"human": 0, "synthetic": 0, "missing": 0}
    normalized = []
    receipts = set()
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise EvaluationError(f"record_{index}_object_required")
        case_id = evaluation_id(record.get("case_id"), "case_id")
        if case_id in seen_cases:
            raise EvaluationError("case_id_duplicate")
        seen_cases.add(case_id)
        split = record.get("split")
        if split not in SPLITS:
            raise EvaluationError("split_invalid")
        identities = {field: _record_identity(record, field)
                      for field in seen}
        for field, value in identities.items():
            previous = seen[field].get(value)
            if previous is not None and previous != split:
                raise EvaluationError(f"{field}_cross_split")
            seen[field][value] = split
        bundle = evaluation_object(record.get("bundle"), "bundle")
        attachment_ids = _validate_bundle(bundle, record, manifest)
        candidate = evaluation_object(record.get("candidate"), "candidate")
        _validate_candidate(candidate, manifest, attachment_ids)
        source = _validate_label(record.get("label"), manifest, candidate)
        origin = record.get('data_origin', manifest.get('data_origin', 'synthetic'))
        if origin not in ('synthetic', 'real_authorized'):
            raise EvaluationError('data_origin_invalid')
        if manifest.get('data_origin', 'synthetic') != origin:
            raise EvaluationError('data_origin_mismatch')
        if source == 'human':
            receipt_id = record['label']['receipt']['receipt_id']
            if receipt_id in receipts:
                raise EvaluationError('label_receipt_duplicate')
            receipts.add(receipt_id)
        provenance[source] += 1
        normalized.append({**record, "case_id": case_id, "split": split,
                           "_label_source": source, "data_origin": origin})
    return {"records": normalized, "provenance": provenance}


def _fact_map(rows: list[dict]) -> dict[str, dict]:
    out = {}
    for row in rows:
        key = _item_id(row, "fact_id")
        if key in out:
            raise EvaluationError("fact_id_duplicate")
        out[key] = row
    return out


def _loop_map(rows: list[dict]) -> dict[str, dict]:
    out = {}
    for row in rows:
        key = _item_id(row, "loop_id")
        if key in out:
            raise EvaluationError("loop_id_duplicate")
        out[key] = row
    return out


def _normalized(value, field: str):
    if field == "negation":
        if isinstance(value, bool):
            return "negated" if value else "affirmed"
        if isinstance(value, str):
            value = value.strip().casefold()
            if value in {"negated", "negative", "否定", "true", "yes"}:
                return "negated"
            if value in {"affirmed", "positive", "肯定", "false", "no"}:
                return "affirmed"
            return value
    if isinstance(value, str):
        return value.strip().casefold()
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    return value


def _resolved(row: dict) -> bool | None:
    value = row.get("resolved")
    return value if type(value) is bool else None


def _case_counts(record: dict) -> dict:
    label = record.get("label") or {}
    candidate = record["candidate"]
    gold = _fact_map(_items(label.get("facts", []), "label_facts"))
    predicted = _fact_map(_items(candidate.get("facts", []), "candidate_facts"))
    important = {key for key, row in gold.items()
                 if row.get("important") is True}
    found = important & predicted.keys()
    label_claims = _items(label.get("claims", []), "label_claims")
    final = {fact_id for claim in label_claims
             if claim["final"] and claim["supported"]
             for fact_id in claim["covered_gold_fact_ids"]}
    critical_claims = [claim for claim in label_claims
                       if claim["critical"]]
    critical_errors = sum(not claim["supported"] for claim in critical_claims)

    attrs = {name: [0, 0] for name in ATTRIBUTE_FIELDS}
    for key in gold:
        row = gold[key]
        pred = predicted.get(key)
        for name in ATTRIBUTE_FIELDS:
            expected = row.get(name, _MISSING)
            if expected is _MISSING:
                continue
            attrs[name][1] += 1
            if pred is not None and _normalized(pred.get(name, _MISSING), name) \
                    == _normalized(expected, name):
                attrs[name][0] += 1

    gold_loops = _loop_map(_items(label.get("loops", []), "label_loops"))
    pred_loops = _loop_map(_items(candidate.get("loops", []),
                                  "candidate_loops"))
    loop_correct = 0
    false_resolution = 0
    unresolved = 0
    unresolved_missed = 0
    for key, row in gold_loops.items():
        expected = _resolved(row)
        actual = _resolved(pred_loops[key]) if key in pred_loops else None
        if expected is not None:
            if expected is False:
                unresolved += 1
                unresolved_missed += int(actual is not False)
                if actual is True:
                    false_resolution += 1
            if actual is expected:
                loop_correct += 1

    mandatory = {key for key, row in gold.items()
                 if row.get("mandatory") is True}
    mandatory_found = mandatory & predicted.keys()
    # stage ids and refs trimmed like the predicted fact ids (_fact_map)
    stage_ids = {stage: {ref.strip() for ref in
                         candidate.get(f"{stage}_fact_ids") or []}
                 for stage in LIFECYCLE_STAGES}
    rendered_ids = stage_ids["rendered"]
    mandatory_rendered = mandatory & rendered_ids
    delivered_ids = stage_ids["delivered"]
    mandatory_delivered = mandatory & delivered_ids
    unresolved_refs = {evaluation_id(row.get("fact_ref"),
                           "candidate_unresolved_fact_ref")
                       for row in _items(candidate.get("unresolved", []),
                                         "candidate_unresolved")}
    silently_dropped = mandatory - predicted.keys() - unresolved_refs
    # Full lifecycle over EVERY predicted fact, not only mandatory gold
    # ones: a late non-mandatory fact lost between verify, render and
    # delivery is still a completeness failure. An absent stage list is
    # a missing observation and never imputed from another stage.
    observed = all(f"{stage}_fact_ids" in candidate
                   for stage in LIFECYCLE_STAGES)
    lifecycle = {"complete": int(observed), "missing": 0, "extra": 0,
                 "missing_observations": int(not observed)}
    for stage in LIFECYCLE_STAGES:
        ids = stage_ids[stage]
        lost = len(predicted.keys() - ids)
        added = len(ids - predicted.keys())
        lifecycle["missing"] += lost
        lifecycle["extra"] += added
        if lost or added:
            lifecycle["complete"] = 0

    gold_relations = _items(label.get("relations", []), "label_relations")
    pred_relations = _items(candidate.get("relations", []),
                            "candidate_relations")
    def _triple(row):
        return (evaluation_id(row.get("left_fact_id"), "relation_left"),
                evaluation_id(row.get("right_fact_id"), "relation_right"),
                row.get("type"))
    pred_rel_triples = {_triple(row) for row in pred_relations}
    relation_hits = sum(_triple(row) in pred_rel_triples
                        for row in gold_relations)
    evidenced = sum(bool(row.get("evidence_ids"))
                    for row in predicted.values())

    status = str(candidate.get("status", "")) \
        .casefold()
    deferred = status in {"pending", "deferred", "retry_wait", "needs_review"}
    latency = candidate.get("latency_ms")
    usage = candidate.get("usage") or {}
    usage_values = {}
    for key in ("requests", "input_tokens",
                "output_tokens", "total_tokens"):
        if key in usage:
            usage_values[key] = int(usage[key])
    return {
        "important": [len(found), len(important)],
        "final": [len(important & final), len(important)],
        "mandatory": [len(mandatory_found), len(mandatory)],
        "rendered": [len(mandatory_rendered), len(mandatory)],
        "delivered": [len(mandatory_delivered), len(mandatory)],
        "relations": [relation_hits, len(gold_relations)],
        "evidenced": [evidenced, len(predicted)],
        "silent_drop": [len(silently_dropped), len(mandatory)],
        "critical": [critical_errors, len(critical_claims)],
        "attributes": attrs,
        "loops": [loop_correct, len(gold_loops.keys() | pred_loops.keys())],
        "false_resolution": [false_resolution, unresolved],
        "loop_precision": [loop_correct, len(pred_loops)],
        "unresolved_missed": [unresolved_missed, unresolved],
        "deferred": [int(deferred), 1],
        "latency": float(latency) if latency is not None else None,
        "usage": usage_values,
        "lifecycle": lifecycle,
    }


def _scope_report(records: list[dict]) -> dict:
    counts = {
        "important": [0, 0], "final": [0, 0], "critical": [0, 0],
        "loops": [0, 0], "false_resolution": [0, 0], "deferred": [0, 0],
        "loop_precision": [0, 0], "unresolved_missed": [0, 0],
        "mandatory": [0, 0], "rendered": [0, 0], "delivered": [0, 0],
        "relations": [0, 0],
        "evidenced": [0, 0], "silent_drop": [0, 0],
    }
    attrs = {name: [0, 0] for name in ATTRIBUTE_FIELDS}
    latencies = []
    usage = {key: [] for key in
             ("requests", "input_tokens",
              "output_tokens", "total_tokens")}
    usage_cases = 0
    lifecycle = {"complete": 0, "missing": 0, "extra": 0,
                 "missing_observations": 0}
    for record in records:
        values = _case_counts(record)
        for key in lifecycle:
            lifecycle[key] += values["lifecycle"][key]
        for key in counts:
            counts[key][0] += values[key][0]
            counts[key][1] += values[key][1]
        for key in attrs:
            attrs[key][0] += values["attributes"][key][0]
            attrs[key][1] += values["attributes"][key][1]
        if values["latency"] is not None:
            latencies.append(values["latency"])
        if values["usage"]:
            usage_cases += 1
            for key, value in values["usage"].items():
                usage[key].append(value)
    metrics = {
        "important_fact_recall": success_metric(*counts["important"]),
        "final_recall": success_metric(*counts["final"]),
        "critical_overclaim": _error_metric(*counts["critical"]),
        "loop_conformity": success_metric(*counts["loops"]),
        "loop_precision": success_metric(*counts["loop_precision"]),
        "loop_unresolved_miss_rate": _error_metric(*counts["unresolved_missed"]),
        "loop_false_resolution": _error_metric(*counts["false_resolution"]),
        "defer_rate": _error_metric(*counts["deferred"]),
        "mandatory_fact_recall": success_metric(*counts["mandatory"]),
        "rendered_fact_recall": success_metric(*counts["rendered"]),
        "delivered_fact_recall": success_metric(*counts["delivered"]),
        "relation_accuracy": success_metric(*counts["relations"]),
        "evidence_closure": success_metric(*counts["evidenced"]),
        "silent_drop": _error_metric(*counts["silent_drop"]),
    }
    for field, name in (('urgency', 'urgency_recall'), ('ai_priority', 'ai_priority_accuracy')):
        eligible = [r for r in records if field in (r.get('label') or {})]
        if field == 'urgency':
            eligible = [r for r in eligible if r['label'][field] == 'high']
        metrics[name] = success_metric(sum(r['candidate'].get(field) == r['label'][field]
                                             for r in eligible), len(eligible))
    paired = []
    for record in records:
        ab = record.get('patient_state_ab') or {}
        scores = (record.get('label') or {}).get('patient_state_ab') or {}
        arms = ab.get('arms', {})
        if (ab.get('same_case_sha256') == payload_hash(record['bundle'])
                and ab.get('off_fingerprint') != ab.get('on_fingerprint')
                and all(isinstance(arms.get(arm), dict)
                        and scores.get(arm + '_sha256') == payload_hash(arms[arm])
                        and type(scores.get(arm + '_score')) in (int, float)
                        and 0 <= scores[arm + '_score'] <= 1 for arm in ('off', 'on'))):
            paired.append(scores)
    metrics['patient_state_ab_noninferiority'] = success_metric(sum(
        row['on_score'] >= row['off_score'] for row in paired), len(records))
    for name, values in attrs.items():
        metrics[f"{name}_recall"] = success_metric(*values)
    latency = {
        "denominator": len(latencies),
        "missing": len(records) - len(latencies),
        "p50_ms": validated_percentile(latencies, 0.50),
        "p95_ms": validated_percentile(latencies, 0.95),
    }
    usage_report = {"denominator": usage_cases,
                    "missing": len(records) - usage_cases}
    for key, values in usage.items():
        total = nonnegative_number(sum(values), "usage_total_" + key, integer=True)
        usage_report[key] = {
            "denominator": len(values),
            "total": total,
            "mean": total / len(values) if values else None,
            "p50": validated_percentile([float(v) for v in values], 0.50),
            "p95": validated_percentile([float(v) for v in values], 0.95),
        }
    return {"cases": len(records), "metrics": metrics,
            "latency": latency, "usage": usage_report,
            "fact_lifecycle": lifecycle}




def criteria_sha256(criteria: dict) -> str:
    """SHA-256 of the canonical JSON of validated criteria."""
    return payload_hash(criteria)


def evaluate_records(records: list[dict], manifest: dict,
                     criteria: dict) -> dict:
    manifest = validate_manifest(manifest)
    criteria = validate_criteria(criteria)
    checked = validate_records(records, manifest)
    records = checked["records"]
    scopes = {split: [r for r in records if r["split"] == split]
              for split in SPLITS}
    overall = _scope_report(records)
    manifest_report = {key: manifest[key] for key in
                       ("version", "bundle_version", "candidate_version")}
    if "label_version" in manifest:
        manifest_report["label_version"] = manifest["label_version"]
    if criteria.get('sealed_holdout'):
        manifest_report = {key + '_sha256': payload_hash(value) for key, value in manifest_report.items()}
    report = {
        "schema_version": SCHEMA_VERSION,
        "manifest": manifest_report,
        "criteria_version": criteria["version"],
        "criteria": criteria,
        "criteria_sha256": criteria_sha256(criteria),
        "label_provenance": checked["provenance"],
        "records": len(records),
        "splits": {split: _scope_report(rows)
                   for split, rows in scopes.items()},
    }
    report["metrics"] = overall["metrics"]
    report["latency"] = overall["latency"]
    report["usage"] = overall["usage"]
    report["fact_lifecycle"] = overall["fact_lifecycle"]
    report['data_origin'] = {origin: sum(r['split'] == 'test' and r['data_origin'] == origin
                                               and r['_label_source'] == 'human' for r in records)
                             for origin in ('real_authorized', 'synthetic')}
    if criteria.get('sealed_holdout'):
        from semantic_holdout import evidence_summary
        report['evidence'] = evidence_summary(records, manifest, criteria)
    report["gate"] = _gate(report, criteria, checked["provenance"])
    return report


def _token_report(groups: list[dict]) -> dict:
    complete = [g for g in groups if not g["unreported_requests"] and not g["missing_jobs"]]
    return {
        "observations": len(groups), "complete_observations": len(complete),
        "incomplete_observations": len(groups) - len(complete),
        "reported_requests": sum(g["reported_requests"] for g in groups),
        "unreported_requests": sum(g["unreported_requests"] for g in groups),
        "missing_jobs": sum(g["missing_jobs"] for g in groups),
        "tokens": {key: {
            "observed_total": nonnegative_number(
                sum(g[key] for g in groups), "token_total_" + key, integer=True),
            "complete_denominator": len(complete),
            "complete_p50": validated_percentile([g[key] for g in complete], .50),
            "complete_p95": validated_percentile([g[key] for g in complete], .95),
        } for key in ("input_tokens", "output_tokens")},
    }


def evaluate_runs(records: list[dict]) -> dict:
    """Aggregate observed run wall time and serial patient semantic work.

    Account IDs are supplied by the exporter; run IDs are only DB-local.
    Missing observations stay missing, never zero-duration successes.
    """
    values = {key: [] for key in (
        "run_elapsed_s", "semantic_elapsed_s", "patient_semantic_work_s",
        "patient_jev_requests", "run_jev_requests", "oldest_pending_job_age_s")}
    missing = {key: 0 for key in values}
    seen = set()
    patient_usage, run_usage = [], []
    for row in records:
        row = evaluation_object(row, "run_record")
        account = evaluation_id(row.get("account_id"), "run_account_id")
        run = evaluation_object(row.get("result"), "run_result")
        identity = (account, evaluation_id(run.get("run_id"), "run_id"))
        if identity in seen:
            raise EvaluationError("duplicate_run")
        seen.add(identity)
        elapsed = run.get("elapsed_s")
        if elapsed is None:
            missing["run_elapsed_s"] += 1
        else:
            values["run_elapsed_s"].append(nonnegative_number(elapsed, "run_elapsed_s"))
        semantic = run.get("semantic")
        if semantic is None or evaluation_object(semantic, "run_semantic").get("mode") == "off":
            missing["semantic_elapsed_s"] += 1
            missing["run_jev_requests"] += 1
            missing["oldest_pending_job_age_s"] += 1
            continue
        for field, metric in (("elapsed_s", "semantic_elapsed_s"),
                              ("oldest_pending_job_age_s", "oldest_pending_job_age_s")):
            value = semantic.get(field)
            if value is None:
                missing[metric] += 1
            else:
                values[metric].append(nonnegative_number(value, metric))
        if "job_metrics" not in semantic:
            missing["run_jev_requests"] += 1
            continue
        patients = {}
        usage_by_patient = {}
        jobs = set()
        for job in _list(semantic.get("job_metrics", []), "job_metrics"):
            job = evaluation_object(job, "job_metric")
            job_id = evaluation_id(job.get("job_id"), "job_id")
            if job_id in jobs:
                raise EvaluationError("duplicate_run_job")
            jobs.add(job_id)
            patient = evaluation_id(job.get("project_id"), "job_project_id")
            totals = patients.setdefault(patient, [0.0, 0])
            totals[0] += nonnegative_number(job.get("elapsed_s"), "job_elapsed_s")
            requests = nonnegative_number(job.get("jev_requests"), "job_requests", integer=True)
            totals[1] += requests
            usage = usage_by_patient.setdefault(patient, dict.fromkeys(
                ("input_tokens", "output_tokens", "reported_requests", "unreported_requests", "missing_jobs"), 0))
            raw = job.get("usage")
            if raw is None:
                usage["missing_jobs"] += 1
                usage["unreported_requests"] += requests
            else:
                raw = evaluation_object(raw, "job_usage")
                checked = {key: nonnegative_number(raw.get(key), "job_usage_" + key, integer=True)
                           for key in ("input_tokens", "output_tokens", "reported_requests", "unreported_requests")}
                if checked["reported_requests"] + checked["unreported_requests"] != requests:
                    raise EvaluationError("job_usage_request_mismatch")
                if not checked["reported_requests"] and (checked["input_tokens"] or checked["output_tokens"]):
                    raise EvaluationError("job_usage_tokens_without_response")
                for key, value in checked.items():
                    usage[key] += value
        patient_usage.extend(usage_by_patient.values())
        run_usage.append({key: sum(g[key] for g in usage_by_patient.values())
                          for key in ("input_tokens", "output_tokens", "reported_requests", "unreported_requests", "missing_jobs")})
        values["run_jev_requests"].append(sum(v[1] for v in patients.values()))
        for elapsed, requests in patients.values():
            values["patient_semantic_work_s"].append(elapsed)
            values["patient_jev_requests"].append(requests)
    metrics = {}
    for key, observations in values.items():
        total = sum(observations)
        metrics[key] = {"denominator": len(observations),
                        "missing": None if key.startswith("patient_") else missing[key],
                        "total": nonnegative_number(total, key, integer=type(total) is int)
                        if observations else None,
                        "p50": validated_percentile(observations, .50),
                        "p95": validated_percentile(observations, .95)}
    return {"runs": len(records), "metrics": metrics,
            "jev_usage": {"patient": _token_report(patient_usage),
                          "run": _token_report(run_usage),
                          "unobserved_runs": len(records) - len(run_usage)},
            "patient_unit": "account/run/project with started semantic jobs",
            "patient_latency_scope": "serial semantic processing time; excludes queue and collection",
            "missing_patient_observations": "unknown when job_metrics are absent",
            "gate_evidence": False}


def load_jsonl(path: str | Path) -> list[dict]:
    rows = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise EvaluationError(f"jsonl_line_invalid:{line_number}") from error
            rows.append(row)
    return rows


def load_json(path: str | Path) -> dict:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise EvaluationError("json_invalid") from error


def write_report(path: str | Path, report: dict) -> None:
    text = json.dumps(report, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, indent=2) + "\n"
    atomic_write(str(path), lambda handle: handle.write(text), mode=0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="offline semantic evaluation")
    parser.add_argument("--input", required=True, help="case JSONL")
    parser.add_argument("--manifest", required=True, help="fixed version manifest")
    parser.add_argument("--criteria", required=True, help="acceptance criteria")
    parser.add_argument("--output", required=True, help="report JSON path")
    parser.add_argument("--runs", help="optional account-scoped run result JSONL")
    args = parser.parse_args(argv)
    try:
        report = evaluate_records(load_jsonl(args.input),
                                  load_json(args.manifest),
                                  load_json(args.criteria))
        if args.runs:
            report["runtime"] = evaluate_runs(load_jsonl(args.runs))
        write_report(args.output, report)
    except (EvaluationError, OSError, TypeError, ValueError) as error:
        # Validation errors contain only a code/line/field, never input text.
        print(f"semantic evaluation failed: {error}", file=sys.stderr)
        return 2
    return 0


# ---------------------------------------------------------------------
# Jev incremental-value evaluation (T14)
#
# Measures what the Jev verifier adds on top of the deterministic audit
# gates: findings the deterministic pass could never produce (per-fact
# support verdicts, coverage-miss detection).  The report doubles as
# activation evidence — `gate.pass` is true only when every case was
# actually evaluated and produced a contract-valid doc.
# ---------------------------------------------------------------------

# Findings produced only by Jev verdicts — they measure the audit's
# incremental detection.  The coverage codes are all Jev outputs (the
# shared coverage Choice is a Jev call), so ambiguous/low_confidence
# belong here too; only the *_unevaluated/input_invalid transport-level
# codes stay deterministic-side.
JEV_FINDING_CODES = {
    "fact_contradicts", "fact_not_supported", "fact_ambiguous",
    "fact_low_confidence", "source_fact_coverage_missing",
    "source_fact_coverage_ambiguous", "source_fact_coverage_low_confidence",
}


def _expected_matched(doc: dict, expected: list) -> int:
    """Expected facts whose evidence quote is covered by a doc evidence
    record or whose statement matches a doc fact verbatim — the honest
    recall numerator."""
    facts = [f for f in doc.get("facts", []) if isinstance(f, dict)]
    evidence_ids = {eid for fact in facts for eid in fact.get("evidence_ids", [])}
    quotes = {e.get("quote") for e in doc.get("evidence", [])
              if isinstance(e, dict) and e.get("evidence_id") in evidence_ids}
    statements = {f.get("statement") for f in facts}
    matched = 0
    for item in expected:
        quote = item.get("evidence_quote")
        statement = item.get("statement")
        if (isinstance(quote, str) and quote in quotes) or (
                isinstance(statement, str) and statement in statements):
            matched += 1
    return matched


def evaluate_jev_incremental(cases: list, llm_fn, jev_client,
                             *, deadline_s: float = 120) -> dict:
    """Run the canonical extract + audit pipeline over labelled bench
    cases and report Jev's incremental detection share.

    ``cases`` entries: {"id", "body", "expect": {"facts": [...]}}
    (multi-message cases are flattened to per-message runs).  Any case
    whose audit cannot be evaluated marks the report evaluated=False —
    the gate refuses partial evidence.
    """
    import time
    import semantic_audit
    import semantic_extraction

    deadline = time.monotonic() + deadline_s
    per_case = []
    det_total = jev_total = 0
    mandatory_total = mandatory_matched = 0
    evaluated = True
    for case in cases:
        cid = str(case.get("id", "case"))
        expected = [f for f in (case.get("expect", {}) or {})
                    .get("facts", [])
                    if isinstance(f, dict) and f.get("mandatory")]
        messages = _case_messages(case, cid)
        for msg in messages:
            member = _eval_member(case, msg, cid)
            mid = f"{cid}:{member['message_id']}"
            expected_here = [f for f in expected
                             if f.get("message_id")
                             in (None, member["message_id"])]
            mandatory_total += len(expected_here)
            try:
                result = semantic_extraction.extract_facts_v2(
                    llm_fn, member, deadline)
            except Exception as error:
                per_case.append({"id": mid, "error": type(error).__name__})
                evaluated = False
                continue
            doc = result["doc"]
            mandatory_matched += _expected_matched(doc, expected_here)
            audit = semantic_audit.audit_facts_v2(
                jev_client, doc, member["body_original"], deadline)
            if not audit["evaluated"]:
                evaluated = False
            det_codes = [f["code"] for f in audit["findings"]
                         if f.get("code") not in JEV_FINDING_CODES]
            jev_codes = [f["code"] for f in audit["findings"]
                         if f.get("code") in JEV_FINDING_CODES]
            det_total += len(det_codes)
            jev_total += len(jev_codes)
            per_case.append({
                "id": mid, "evaluated": audit["evaluated"],
                "status": audit["status"],
                "facts": len(doc.get("facts", [])),
                "deterministic_findings": len(det_codes),
                "jev_incremental_findings": len(jev_codes)})

    total_findings = det_total + jev_total
    return {
        "schema_version": "jev-value-v1",
        "cases": len(per_case),
        "evaluated": evaluated,
        "extraction": {
            "mandatory_expected": mandatory_total,
            "mandatory_matched": mandatory_matched,
            "mandatory_recall": (mandatory_matched / mandatory_total)
            if mandatory_total else None},
        "audit": {
            "deterministic_findings": det_total,
            "jev_incremental_findings": jev_total,
            "incremental_share": (jev_total / total_findings)
            if total_findings else None},
        "per_case": per_case,
        "gate": {"pass": evaluated and bool(per_case)},
    }


# ---------------------------------------------------------------------
# Synthetic shadow end-to-end driver (T16)
#
# Runs the whole canonical path per case — extraction, relation
# reconciliation, bidirectional audit, one targeted repair, mandatory
# rendering — with the same entry points the drain uses, so a bench
# run exercises real control flow.  Each stage is reported
# independently; unevaluated or failed stages stay visible.
# ---------------------------------------------------------------------


def run_shadow_e2e(cases: list, llm_fn, jev_client,
                   *, source: str, deadline_s: float = 300,
                   chunk_size: int = 3000) -> dict:
    """Per-case shadow run of the canonical pipeline.

    ``cases`` entries use the bench corpus shape (``messages`` or a
    single ``body``).  Returns a stage-indexed report; ``complete``
    counts cases whose audit reached PASS (after at most one repair).
    ``source`` is the caller's explicit provenance of the cases: only
    "synthetic" input reaches the model; anything else is reported as
    not evaluated, never passed."""
    import time
    import semantic_audit
    import semantic_extraction
    import semantic_relations
    import semantic_render

    if source != "synthetic":
        # Unconfirmed provenance never reaches the model or counts as passed.
        return {"schema_version": "shadow-e2e-v1", "source": "unconfirmed",
                "cases": len(cases), "complete": 0,
                "per_case": [{"id": str(case.get("id", "case")) if isinstance(case, dict) else "case",
                              "facts": 0, "passed": False, "reason": "source_unconfirmed",
                              "stages": dict.fromkeys(("extract", "relations", "audit", "repair", "render"),
                                                      "not_evaluated")} for case in cases]}
    deadline = time.monotonic() + deadline_s
    per_case = []
    complete = 0
    for case in cases:
        cid = str(case.get("id", "case"))
        messages = _case_messages(case, cid)
        active_facts = []
        stages = {"extract": "ok", "relations": "ok", "audit": None,
                  "repair": "skipped", "render": "ok"}
        case_doc_facts = 0
        all_audits_passed = True
        all_renders_complete = True
        for msg in messages:
            member = _eval_member(case, msg, cid)
            try:
                result = semantic_extraction.extract_facts_v2(
                    llm_fn, member, deadline, jev_client=jev_client,
                    chunk_size=chunk_size)
            except Exception as error:
                stages["extract"] = f"error:{type(error).__name__}"
                break
            doc = result["doc"]
            if not result["extraction_complete"]:
                stages["extract"] = "incomplete"
            try:
                rels = semantic_relations.reconcile_facts(
                    active_facts, doc["facts"], deadline=deadline)
                doc["relations"] = rels["relations"]
                if rels.get("pairs_skipped"):
                    stages["relations"] = "incomplete"
            except Exception as error:
                stages["relations"] = f"error:{type(error).__name__}"
            audit = _terminal_audit(semantic_audit.audit_facts_v2(
                jev_client, doc, member["body_original"], deadline))
            audit_status = audit["status"]
            if audit["evaluated"] and audit["status"] != "PASS":
                rejected = {f["fact"]: f["code"]
                            for f in audit["findings"] if f.get("fact")}
                if rejected:
                    try:
                        repair = semantic_extraction.repair_facts_v2(
                            llm_fn, member, doc, rejected, deadline,
                            chunk_size=chunk_size)
                    except Exception as error:
                        stages["repair"] = f"error:{type(error).__name__}"
                        repair = {"repaired": False}
                    else:
                        stages["repair"] = ("repaired"
                                            if repair["repaired"]
                                            else "failed")
                    if repair["repaired"]:
                        doc = repair["doc"]
                        try:
                            rels = semantic_relations.reconcile_facts(
                                active_facts, doc["facts"], deadline=deadline)
                            doc["relations"] = rels["relations"]
                            if rels.get("pairs_skipped"):
                                stages["relations"] = "incomplete"
                        except Exception as error:
                            stages["relations"] = f"error:{type(error).__name__}"
                        audit = _terminal_audit(semantic_audit.audit_facts_v2(
                            jev_client, doc, member["body_original"],
                            deadline))
                        audit_status = f"{audit['status']} (post-repair)"
            all_audits_passed &= audit["evaluated"] and audit["status"] == "PASS"
            # Preserve the first failing message's audit in a thread report.
            if stages["audit"] is None or stages["audit"].startswith("PASS"):
                stages["audit"] = audit_status
            active_facts.extend(doc["facts"])
            case_doc_facts += len(doc["facts"])
            mandatory = semantic_render.mandatory_render(doc)
            all_renders_complete &= mandatory["complete"]
            stages["render"] = (f"{len(mandatory['facts'])}facts/"
                                f"{len(mandatory['limitations'])}lims"
                                if all_renders_complete else "incomplete")
        passed = stages["extract"] == "ok" \
            and stages["relations"] == "ok" and all_audits_passed \
            and all_renders_complete \
            and isinstance(stages["audit"], str) \
            and stages["audit"].startswith("PASS")
        if passed:
            complete += 1
        per_case.append({"id": cid, "facts": case_doc_facts,
                         "passed": passed, "stages": stages,
                         "reason": None if passed else _shadow_reason(
                             stages, all_audits_passed, all_renders_complete)})
    return {"schema_version": "shadow-e2e-v1", "source": "synthetic",
            "cases": len(per_case), "complete": complete,
            "per_case": per_case}


_AUDIT_STATUSES = frozenset({"PASS", "NEEDS_REVIEW", "INCOMPLETE"})  # audit_facts_v2 terminals


def _terminal_audit(audit) -> dict:
    """An audit without a known terminal status is reported, never a PASS."""
    if (not isinstance(audit, dict) or not isinstance(audit.get("status"), str)
            or audit["status"] not in _AUDIT_STATUSES
            or type(audit.get("evaluated")) is not bool):
        return {"status": "STATUS_MISSING", "evaluated": False, "findings": []}
    return audit


def _shadow_reason(stages: dict, audits_passed: bool, renders_complete: bool) -> str:
    """The first stage that kept a shadow case from passing, as a code."""
    if stages["extract"] != "ok":
        return "extract_" + stages["extract"].split(":")[0]
    if stages["relations"] != "ok":
        return "relations_" + stages["relations"].split(":")[0]
    if not isinstance(stages["audit"], str) or stages["audit"].startswith("STATUS_MISSING"):
        return "audit_status_missing"
    if not audits_passed or not stages["audit"].startswith("PASS"):
        return "audit_not_pass"
    return "render_incomplete" if not renders_complete else "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
