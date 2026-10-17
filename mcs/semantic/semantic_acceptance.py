"""Shared metrics-only acceptance checks used by runtime promotion and offline scoring."""
from __future__ import annotations

METRICS = (
    "important_fact_recall", "final_recall", "critical_overclaim",
    "medication_recall", "negation_recall", "time_recall",
    "speaker_relation_recall", "loop_conformity",
    "loop_false_resolution", "loop_precision", "loop_unresolved_miss_rate", "defer_rate",
    "mandatory_fact_recall", "rendered_fact_recall",
    "delivered_fact_recall",
    "relation_accuracy", "evidence_closure", "silent_drop",
)
ADDITIONAL_METRICS = ("urgency_recall", "ai_priority_accuracy", "patient_state_ab_noninferiority")


def evaluate_gate(report: dict, criteria: dict, provenance: dict) -> dict:
    reasons = []
    if criteria.get('sealed_holdout') and report.get('data_origin', {}).get('real_authorized', 0) < criteria['min_human_labels']:
        reasons.append('real_authorized_labels_insufficient')
    if criteria.get('sealed_holdout') and not report.get('evidence', {}).get('sealed'):
        reasons.append('sealed_holdout_required')
    if min(provenance["human"], report["splits"]["test"]["cases"]) < criteria["min_human_labels"]:
        reasons.append("human_labels_insufficient")
    if provenance["human"] != sum(provenance.values()):
        reasons.append("human_labels_required")
    required_splits = set(criteria["required_splits"])
    if any(report["splits"].get(split, {}).get("cases", 0) == 0
           for split in required_splits):
        reasons.append("required_split_missing")
    heldout = report["splits"]["test"]
    if heldout["fact_lifecycle"]["complete"] != heldout["cases"]:
        reasons.append("fact_lifecycle_incomplete")
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
            reason = f"denominator_zero:{name}"
            if reason not in reasons:
                reasons.append(reason)
            continue
        if metric['denominator'] < criteria.get('denominator_minimums', {}).get(name, 1):
            reasons.append(f'denominator_insufficient:{name}')
        low = metric['wilson95']['low'] if name in criteria.get('wilson_metrics', []) else metric['rate']
        high = metric['wilson95']['high'] if name in criteria.get('wilson_metrics', []) else metric['rate']
        minimum = criteria["minimums"].get(name)
        if minimum is not None and low < minimum:
            reasons.append(f"below_threshold:{name}")
        maximum = criteria["maximums"].get(name)
        if maximum is not None and high > maximum:
            reasons.append(f"above_threshold:{name}")
        if name == "critical_overclaim" and metric["errors"] > 0:
            reasons.append("critical_overclaim")
    if metrics.get("critical_overclaim", {}).get("errors", 0) > 0 \
            and "critical_overclaim" not in reasons:
        reasons.append("critical_overclaim")
    return {"pass": not reasons, "reasons": reasons,
            "g6_eligible": not reasons and provenance["human"]
            == sum(provenance.values()) and report.get('data_origin', {}).get('real_authorized', 0) >= criteria['min_human_labels']}

