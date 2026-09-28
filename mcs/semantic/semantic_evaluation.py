#!/usr/bin/env python3
"""Offline evaluation for fixed semantic bundles and reviewed labels.

The evaluator consumes JSONL records and never opens bundle attachment paths,
calls a model, or emits source text.  A record is the unit of split isolation:
``case_id``, ``account_id``, ``project_id`` and ``thread_id`` must not cross
dev, calibration, and test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path, PureWindowsPath

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

from semantic_facts import RELATION_TYPES
from mcs_util import atomic_write


SCHEMA_VERSION = "semantic-evaluation/v2"
SPLITS = ("dev", "calibration", "test")
METRICS = (
    "important_fact_recall", "final_recall", "critical_overclaim",
    "medication_recall", "negation_recall", "time_recall",
    "speaker_relation_recall", "loop_conformity",
    "loop_false_resolution", "loop_precision", "loop_unresolved_miss_rate", "defer_rate",
    "mandatory_fact_recall", "rendered_fact_recall",
    "delivered_fact_recall",
    "relation_accuracy", "evidence_closure", "silent_drop",
)
ATTRIBUTE_FIELDS = ("medication", "negation", "time", "speaker_relation")
_MISSING = object()


class EvaluationError(ValueError):
    """Input or acceptance criteria are not safe to evaluate."""


def _id(value, field: str) -> str:
    if value is None:
        raise EvaluationError(f"{field}_missing")
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise EvaluationError(f"{field}_invalid")
    value = str(value).strip()
    if not value:
        raise EvaluationError(f"{field}_missing")
    return value


def _dict(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise EvaluationError(f"{field}_object_required")
    return value


def _list(value, field: str) -> list:
    if not isinstance(value, list):
        raise EvaluationError(f"{field}_list_required")
    return value


def _finite_number(value, field: str, integer: bool = False) -> float | int:
    if type(value) not in (int, float) or (integer and type(value) is not int):
        raise EvaluationError(f"{field}_invalid")
    try:
        number = float(value)
    except OverflowError:
        raise EvaluationError(f"{field}_invalid") from None
    if not math.isfinite(number) or value < 0:
        raise EvaluationError(f"{field}_invalid")
    return value if integer else number


def wilson_interval(successes: int, denominator: int,
                    z: float = 1.96) -> dict[str, float | None]:
    """Return a bounded 95% Wilson interval without a statistics package."""
    if denominator <= 0:
        return {"low": None, "high": None}
    p = successes / denominator
    z2 = z * z
    center = (p + z2 / (2 * denominator)) / (1 + z2 / denominator)
    half = z / (1 + z2 / denominator) * math.sqrt(
        p * (1 - p) / denominator + z2 / (4 * denominator ** 2))
    return {"low": max(0.0, center - half),
            "high": min(1.0, center + half)}


def _success_metric(correct: int, denominator: int) -> dict:
    errors = max(0, denominator - correct)
    return {
        "correct": correct,
        "errors": errors,
        "denominator": denominator,
        "rate": correct / denominator if denominator else None,
        "wilson95": wilson_interval(correct, denominator),
    }


def _error_metric(errors: int, denominator: int) -> dict:
    errors = max(0, errors)
    correct = max(0, denominator - errors)
    return {
        "correct": correct,
        "errors": errors,
        "denominator": denominator,
        "rate": errors / denominator if denominator else None,
        "wilson95": wilson_interval(errors, denominator),
    }


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    for value in values:
        _finite_number(value, "percentile_value")
    values = sorted(values)
    pos = (len(values) - 1) * p
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return values[low]
    return values[low] + (values[high] - values[low]) * (pos - low)


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


def _item_id(item: dict, field: str) -> str:
    return _id(item.get(field), field)


def _items(value, field: str) -> list[dict]:
    rows = _list(value, field)
    if any(not isinstance(row, dict) for row in rows):
        raise EvaluationError(f"{field}_item_object_required")
    return rows


def _validate_relative_path(value, field: str) -> str:
    path = _id(value, field)
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
    manifest = _dict(manifest, "manifest")
    for field in ("version", "bundle_version", "candidate_version"):
        _id(manifest.get(field), f"manifest_{field}")
    if "label_version" in manifest:
        _id(manifest["label_version"], "manifest_label_version")
    if "splits" in manifest:
        splits = _list(manifest["splits"], "manifest_splits")
        if any(split not in SPLITS for split in splits):
            raise EvaluationError("manifest_splits_invalid")
    return manifest


def validate_criteria(criteria: dict) -> dict:
    criteria = _dict(criteria, "criteria")
    _id(criteria.get("version"), "criteria_version")
    required = criteria.get("required_metrics", list(METRICS))
    required = _list(required, "criteria_required_metrics")
    if not required or any(not isinstance(x, str) for x in required):
        raise EvaluationError("criteria_required_metrics_invalid")
    if any(name not in METRICS for name in required):
        raise EvaluationError("criteria_required_metrics_invalid")
    minimums = _dict(criteria.get("minimums") or {}, "criteria_minimums")
    maximums = _dict(criteria.get("maximums") or {}, "criteria_maximums")
    if any(name not in METRICS for name in minimums) \
            or any(name not in METRICS for name in maximums):
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
    return {
        "version": _id(criteria["version"], "criteria_version"),
        "required_metrics": required,
        "minimums": {k: float(v) for k, v in minimums.items()},
        "maximums": {k: float(v) for k, v in maximums.items()},
        "required_splits": splits,
        "min_human_labels": human_min,
    }


def _record_identity(record: dict, field: str) -> str:
    return _id(record.get(field), field)


def _validate_bundle(bundle: dict, record: dict, manifest: dict) -> set:
    _dict(bundle, "bundle")
    if _id(bundle.get("version"), "bundle_version") != manifest["bundle_version"]:
        raise EvaluationError("bundle_version_mismatch")
    for field in ("account_id", "project_id", "thread_id"):
        if field in bundle and _id(bundle[field], field) != \
                _record_identity(record, field):
            raise EvaluationError(f"bundle_{field}_mismatch")
    messages = bundle.get("messages", [])
    if not isinstance(messages, list):
        raise EvaluationError("bundle_messages_list_required")
    if any(not isinstance(message, dict) for message in messages):
        raise EvaluationError("bundle_message_object_required")
    message_ids = [_id(m.get("message_id"), "message_id")
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
        mid = _id(attachment.get("message_id"), "attachment_message_id")
        if message_set and mid not in message_set:
            raise EvaluationError("attachment_message_missing")
        before = attachment.get("context_before", [])
        if not isinstance(before, list):
            raise EvaluationError("attachment_context_before_list_required")
        before_ids = [_id(value, "attachment_context_message_id")
                      for value in before]
        if (message_set and any(value not in message_set for value in before_ids)):
            raise EvaluationError("attachment_context_message_missing")
        if (mid in message_order
                and any(message_order[value] >= message_order[mid]
                        for value in before_ids)):
            raise EvaluationError("attachment_context_not_before")
    return attachment_ids


def _validate_candidate(candidate: dict, manifest: dict, attachment_ids: set) -> None:
    _dict(candidate, "candidate")
    if _id(candidate.get("version"), "candidate_version") != \
            manifest["candidate_version"]:
        raise EvaluationError("candidate_version_mismatch")
    for field in ("facts", "claims", "loops"):
        if field in candidate:
            _items(candidate[field], f"candidate_{field}")
    usage = candidate.get("usage")
    if usage is not None:
        usage = _dict(usage, "candidate_usage")
        for key in ("requests", "input_tokens",
                    "output_tokens", "total_tokens"):
            if key in usage:
                _finite_number(usage[key], f"candidate_usage_{key}", integer=True)
    latency = candidate.get("latency_ms")
    if latency is not None:
        _finite_number(latency, "candidate_latency_ms")
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
            _id(ref, "candidate_evidence_id")
        if "fact_refs" in row and not isinstance(row["fact_refs"], list):
            raise EvaluationError("candidate_fact_refs_list_required")
        refs = row.get("attachment_refs", [])
        if not isinstance(refs, list):
            raise EvaluationError("candidate_attachment_refs_list_required")
        for ref in row.get("fact_refs", []):
            _id(ref, "fact_ref")
        if any(_id(ref, "attachment_ref") not in attachment_ids
               for ref in refs):
            raise EvaluationError("candidate_attachment_ref_unknown")
    for row in _items(candidate.get("loops", []), "candidate_loops"):
        _item_id(row, "loop_id")
        if type(row.get("resolved")) is not bool:
            raise EvaluationError("candidate_loop_resolved_required")
    rendered = candidate.get("rendered_fact_ids", [])
    if not isinstance(rendered, list):
        raise EvaluationError("candidate_rendered_fact_ids_list_required")
    if any(not isinstance(ref, str) or not ref.strip() for ref in rendered):
        raise EvaluationError("candidate_rendered_fact_id_invalid")
    if len(rendered) != len(set(rendered)):
        raise EvaluationError("candidate_rendered_fact_id_duplicate")
    # verified -> rendered -> delivered chain (T6): the delivered id
    # set follows the rendered contract — same shape, same dedupe, and
    # an absent list scores as nothing delivered rather than skipped.
    delivered = candidate.get("delivered_fact_ids", [])
    if not isinstance(delivered, list):
        raise EvaluationError("candidate_delivered_fact_ids_list_required")
    if any(not isinstance(ref, str) or not ref.strip()
           for ref in delivered):
        raise EvaluationError("candidate_delivered_fact_id_invalid")
    if len(delivered) != len(set(delivered)):
        raise EvaluationError("candidate_delivered_fact_id_duplicate")
    for row in _items(candidate.get("relations", []), "candidate_relations"):
        _id(row.get("left_fact_id"), "candidate_relation_left")
        _id(row.get("right_fact_id"), "candidate_relation_right")
        if row.get("left_fact_id") == row.get("right_fact_id"):
            raise EvaluationError("candidate_relation_self_loop")
        if row.get("type") not in RELATION_TYPES:
            raise EvaluationError("candidate_relation_type_invalid")
    for row in _items(candidate.get("unresolved", []),
                      "candidate_unresolved"):
        _id(row.get("fact_ref"), "candidate_unresolved_fact_ref")


def _validate_label(label, manifest: dict, candidate: dict) -> str:
    if label is None:
        return "missing"
    label = _dict(label, "label")
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
        left = _id(row.get("left_fact_id"), "label_relation_left")
        right = _id(row.get("right_fact_id"), "label_relation_right")
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
        covered_ids = [_id(value, "covered_gold_fact_id") for value in covered]
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
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise EvaluationError(f"record_{index}_object_required")
        case_id = _id(record.get("case_id"), "case_id")
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
        bundle = _dict(record.get("bundle"), "bundle")
        attachment_ids = _validate_bundle(bundle, record, manifest)
        candidate = _dict(record.get("candidate"), "candidate")
        _validate_candidate(candidate, manifest, attachment_ids)
        source = _validate_label(record.get("label"), manifest, candidate)
        provenance[source] += 1
        normalized.append({**record, "case_id": case_id, "split": split,
                           "_label_source": source})
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
    rendered_ids = set(candidate.get("rendered_fact_ids") or [])
    mandatory_rendered = mandatory & rendered_ids
    delivered_ids = set(candidate.get("delivered_fact_ids") or [])
    mandatory_delivered = mandatory & delivered_ids
    unresolved_refs = {row.get("fact_ref")
                       for row in _items(candidate.get("unresolved", []),
                                         "candidate_unresolved")}
    silently_dropped = mandatory - predicted.keys() - unresolved_refs

    gold_relations = _items(label.get("relations", []), "label_relations")
    pred_relations = _items(candidate.get("relations", []),
                            "candidate_relations")
    pred_rel_triples = {(row.get("left_fact_id"),
                         row.get("right_fact_id"), row.get("type"))
                        for row in pred_relations}
    relation_hits = sum(
        (row.get("left_fact_id"), row.get("right_fact_id"),
         row.get("type")) in pred_rel_triples for row in gold_relations)
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
    for record in records:
        values = _case_counts(record)
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
        "important_fact_recall": _success_metric(*counts["important"]),
        "final_recall": _success_metric(*counts["final"]),
        "critical_overclaim": _error_metric(*counts["critical"]),
        "loop_conformity": _success_metric(*counts["loops"]),
        "loop_precision": _success_metric(*counts["loop_precision"]),
        "loop_unresolved_miss_rate": _error_metric(*counts["unresolved_missed"]),
        "loop_false_resolution": _error_metric(*counts["false_resolution"]),
        "defer_rate": _error_metric(*counts["deferred"]),
        "mandatory_fact_recall": _success_metric(*counts["mandatory"]),
        "rendered_fact_recall": _success_metric(*counts["rendered"]),
        "delivered_fact_recall": _success_metric(*counts["delivered"]),
        "relation_accuracy": _success_metric(*counts["relations"]),
        "evidence_closure": _success_metric(*counts["evidenced"]),
        "silent_drop": _error_metric(*counts["silent_drop"]),
    }
    for name, values in attrs.items():
        metrics[f"{name}_recall"] = _success_metric(*values)
    latency = {
        "denominator": len(latencies),
        "missing": len(records) - len(latencies),
        "p50_ms": _percentile(latencies, 0.50),
        "p95_ms": _percentile(latencies, 0.95),
    }
    usage_report = {"denominator": usage_cases,
                    "missing": len(records) - usage_cases}
    for key, values in usage.items():
        total = _finite_number(sum(values), "usage_total_" + key, integer=True)
        usage_report[key] = {
            "denominator": len(values),
            "total": total,
            "mean": total / len(values) if values else None,
            "p50": _percentile([float(v) for v in values], 0.50),
            "p95": _percentile([float(v) for v in values], 0.95),
        }
    return {"cases": len(records), "metrics": metrics,
            "latency": latency, "usage": usage_report}


def _gate(report: dict, criteria: dict, provenance: dict) -> dict:
    reasons = []
    if min(provenance["human"], report["splits"]["test"]["cases"]) < criteria["min_human_labels"]:
        reasons.append("human_labels_insufficient")
    if provenance["human"] != sum(provenance.values()):
        reasons.append("human_labels_required")
    required_splits = set(criteria["required_splits"])
    if any(report["splits"].get(split, {}).get("cases", 0) == 0
           for split in required_splits):
        reasons.append("required_split_missing")
    heldout = report["splits"]["test"]
    metrics = heldout["metrics"]
    reasons.extend(f"denominator_zero:{name}" for name in METRICS
                   if metrics[name]["denominator"] == 0)
    if heldout["latency"]["denominator"] == 0:
        reasons.append("denominator_zero:latency")
    if heldout["usage"]["denominator"] == 0:
        reasons.append("denominator_zero:usage")
    if heldout["latency"]["missing"]:
        reasons.append("telemetry_missing:latency")
    reasons.extend(f"telemetry_missing:{field}"
                   for field in ("requests", "input_tokens", "output_tokens")
                   if heldout["usage"][field]["denominator"]
                   < heldout["cases"])
    for name in criteria["required_metrics"]:
        metric = metrics.get(name)
        if metric is None:
            reasons.append(f"metric_missing:{name}")
            continue
        if metric["denominator"] == 0:
            continue
        minimum = criteria["minimums"].get(name)
        if minimum is not None and metric["rate"] < minimum:
            reasons.append(f"below_threshold:{name}")
        maximum = criteria["maximums"].get(name)
        if maximum is not None and metric["rate"] > maximum:
            reasons.append(f"above_threshold:{name}")
        if name == "critical_overclaim" and metric["errors"] > 0:
            reasons.append("critical_overclaim")
    if metrics.get("critical_overclaim", {}).get("errors", 0) > 0 \
            and "critical_overclaim" not in reasons:
        reasons.append("critical_overclaim")
    return {"pass": not reasons, "reasons": reasons,
            "g6_eligible": not reasons and provenance["human"]
            == sum(provenance.values())}


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
    report = {
        "schema_version": SCHEMA_VERSION,
        "manifest": manifest_report,
        "criteria_version": criteria["version"],
        "criteria": criteria,
        "criteria_sha256": hashlib.sha256(json.dumps(
            criteria, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
        "label_provenance": checked["provenance"],
        "records": len(records),
        "splits": {split: _scope_report(rows)
                   for split, rows in scopes.items()},
    }
    report["metrics"] = overall["metrics"]
    report["latency"] = overall["latency"]
    report["usage"] = overall["usage"]
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
            "observed_total": _finite_number(
                sum(g[key] for g in groups), "token_total_" + key, integer=True),
            "complete_denominator": len(complete),
            "complete_p50": _percentile([g[key] for g in complete], .50),
            "complete_p95": _percentile([g[key] for g in complete], .95),
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
        row = _dict(row, "run_record")
        account = _id(row.get("account_id"), "run_account_id")
        run = _dict(row.get("result"), "run_result")
        identity = (account, _id(run.get("run_id"), "run_id"))
        if identity in seen:
            raise EvaluationError("duplicate_run")
        seen.add(identity)
        elapsed = run.get("elapsed_s")
        if elapsed is None:
            missing["run_elapsed_s"] += 1
        else:
            values["run_elapsed_s"].append(_finite_number(elapsed, "run_elapsed_s"))
        semantic = run.get("semantic")
        if semantic is None or _dict(semantic, "run_semantic").get("mode") == "off":
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
                values[metric].append(_finite_number(value, metric))
        if "job_metrics" not in semantic:
            missing["run_jev_requests"] += 1
            continue
        patients = {}
        usage_by_patient = {}
        jobs = set()
        for job in _list(semantic.get("job_metrics", []), "job_metrics"):
            job = _dict(job, "job_metric")
            job_id = _id(job.get("job_id"), "job_id")
            if job_id in jobs:
                raise EvaluationError("duplicate_run_job")
            jobs.add(job_id)
            patient = _id(job.get("project_id"), "job_project_id")
            totals = patients.setdefault(patient, [0.0, 0])
            totals[0] += _finite_number(job.get("elapsed_s"), "job_elapsed_s")
            requests = _finite_number(job.get("jev_requests"), "job_requests", integer=True)
            totals[1] += requests
            usage = usage_by_patient.setdefault(patient, dict.fromkeys(
                ("input_tokens", "output_tokens", "reported_requests", "unreported_requests", "missing_jobs"), 0))
            raw = job.get("usage")
            if raw is None:
                usage["missing_jobs"] += 1
                usage["unreported_requests"] += requests
            else:
                raw = _dict(raw, "job_usage")
                checked = {key: _finite_number(raw.get(key), "job_usage_" + key, integer=True)
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
                        "total": _finite_number(total, key, integer=type(total) is int)
                        if observations else None,
                        "p50": _percentile(observations, .50),
                        "p95": _percentile(observations, .95)}
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


if __name__ == "__main__":
    raise SystemExit(main())


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
        messages = case.get("messages") or [
            {"message_id": cid, "revision": "r1",
             "body": case.get("body", "")}]
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
                   *, deadline_s: float = 300,
                   chunk_size: int = 3000) -> dict:
    """Per-case shadow run of the canonical pipeline.

    ``cases`` entries use the bench corpus shape (``messages`` or a
    single ``body``).  Returns a stage-indexed report; ``complete``
    counts cases whose audit reached PASS (after at most one repair)."""
    import time
    import semantic_audit
    import semantic_extraction
    import semantic_relations
    import semantic_render

    deadline = time.monotonic() + deadline_s
    per_case = []
    complete = 0
    for case in cases:
        cid = str(case.get("id", "case"))
        messages = case.get("messages") or [
            {"message_id": cid, "revision": "r1",
             "body": case.get("body", "")}]
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
                    active_facts, doc["facts"])
                doc["relations"] = rels["relations"]
            except Exception as error:
                stages["relations"] = f"error:{type(error).__name__}"
            audit = semantic_audit.audit_facts_v2(
                jev_client, doc, member["body_original"], deadline)
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
                            doc["relations"] = semantic_relations.reconcile_facts(
                                active_facts, doc["facts"])["relations"]
                        except Exception as error:
                            stages["relations"] = f"error:{type(error).__name__}"
                        audit = semantic_audit.audit_facts_v2(
                            jev_client, doc, member["body_original"],
                            deadline)
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
                         "passed": passed, "stages": stages})
    return {"schema_version": "shadow-e2e-v1",
            "cases": len(per_case), "complete": complete,
            "per_case": per_case}
