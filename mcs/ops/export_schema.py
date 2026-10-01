"""Project and validate aggregate export records with a shared field allowlist."""
from __future__ import annotations

import math
import re


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _integer(value):
    return type(value) is int and value >= 0


def _token(value):
    return isinstance(value, str) and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is not None


def _date(value):
    return isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None


def _enum(*values):
    return lambda value: isinstance(value, str) and value in values


def _counts(*names):
    return dict.fromkeys(names, _integer)


def _items(row):
    return {"total": _integer, "returned": _integer,
            "truncated": lambda v: type(v) is bool, "items": [row]}


def _boolean(value):
    return type(value) is bool


def _windows(row):
    return {f"last_{days}d": _items(row) for days in (7, 14, 30)}


_BOOL = _boolean
_STATE = _enum("current", "stale", "pending", "unknown")
_KINDS = ("extract_v1", "extract_llm", "canonical_projection", "semantic_facts_v4")
_ATT_STATES = ("pending", "downloaded", "failed", "pruned", "withdrawn", "unknown")
_BODY_STATES = ("full", "snippet", "unknown", "deleted", "null")
_SCOPE = {"since": finite_number, "until": finite_number,
          "as_of": finite_number, "project_id": _integer}
_RATIO = {"numerator": _integer, "denominator": _integer,
          "unit": _enum("messages", "posts"), "value": finite_number,
          "reason": _enum("denominator_zero")}
_ACTIVITY = {**_counts("project_id", "posts", "active_days", "senders",
                       "professions", "orgs")}
_STAT_COMMON = {"status": _enum("ok", "partial", "unavailable", "unsupported"),
                "definition_version": _date, "scope": _SCOPE}
# Raw labels, dates copied from source text, notes and explanatory prose
# remain on the local human surface. Machine records retain numeric facts,
# typed identifiers and validated states; omitted content is declared.
_STATS = {
    "data_quality": {
        "stages": dict.fromkeys(("fetched", "parsed_current_revision",
                                 "stat_ready_timestamped"), _RATIO),
        "body_states": _counts(*_BODY_STATES),
        **_counts("stale_parsed", "extract_meta_unparseable", "extract_prefiltered")},
    "patient_activity": {"windows": _windows(_ACTIVITY)},
    "med_change_burden": {
        "windows": _windows(_counts("project_id", "change_mentions")),
        "busiest_days": _items({**_counts("project_id", "changes"),
                                 "day": lambda v: v == "unknown" or _date(v)})},
    "open_loop_aging": {
        "formal_open_requests": _items({**_counts("request_id", "project_id"),
            "status": _enum("open", "in_progress"),
            "due_unparseable": _BOOL, "days_since_due": finite_number}),
        "age_buckets": _counts("not_yet_due", "0-7d", "8-30d", "31-90d",
                               "over_90d", "no_due"),
        "text_candidates": {"status": _enum("unavailable")}},
    "med_change_followup": {
        "change_mentions_7d_plus": _RATIO,
        "no_followup_record": _items(_counts("project_id", "message_id"))},
    "rx_expiry": {
        "expiring_periods": _items({**_counts("project_id", "message_id"),
                                    "days_left": finite_number}),
        **_counts("rooms_with_expiring", "horizon_days")},
    "transition_reconciliation": {
        "cooccurrences": _items({**_counts("project_id", "discharge_message_id"),
                                  "med_change_message_ids": [_integer]})},
    "meds": {"action_totals": _counts("start", "stop", "change", "increase",
                                       "decrease", "none", "other"),
             "distinct_names": _integer},
    "med_mentions": {"rooms_with_mentions": _integer,
                     "by_room": _items(_counts("project_id", "distinct_med_names"))},
    "adherence_events": {},
    "overview": _counts("rooms_total", "rooms_with_posts_in_scope", "posts_in_scope",
                         "distinct_senders_in_scope", "distinct_organizations_in_scope"),
}
_BASE = {"type": _token, "contract": _enum("mcs-read-model/1"),
         "snapshot_generation_id": _token, "content_omitted": _BOOL}
_FACT = {"fact_id": _token, "kind": _enum(
             "medication_event", "medication_exposure", "allergy_intolerance",
             "adverse_drug_event", "adherence_administration", "symptom_state",
             "vital_lab", "care_event", "request_pending", "preference", "other_observation"),
         "validation_status": _enum("verified", "unverified", "rejected", "unknown"),
         "workflow_status": _enum("reported", "ordered", "planned", "considering",
                                   "in_progress", "performed", "done", "cancelled",
                                   "on_hold", "pending", "unknown"),
         "evidence_ids": [_token]}
_SCHEMAS = {
    "meta": {"snapshot": {"generation_id": _token, "generated_at": finite_number,
                            "published": _BOOL}},
    "coverage": {"coverage": {
        "collection": _counts("patients", "messages", "deleted", "extraction_eligible"),
        "extraction": {k: _counts("current", "stale", "pending", "unknown") for k in _KINDS},
        "attachments": _counts("total", *_ATT_STATES)}},
    "signal": {"signal_type": _enum(
                   "request_overdue", "request_aging", "med_change_no_followup",
                   "comm_concentration", "rx_period_expiry", "rx_period_lapsed",
                   "transition_reconciliation",
                   "pharmacist_request_unanswered", "rx_request_visibility",
                   "adherence_concern", "discharge_notice", "symptom_after_med_change"),
               "project_id": _integer,
               "detected_at": finite_number,
               "evidence": {**_counts("project_id", "message_id", "request_id",
                                       "discharge_message_id"),
                            **dict.fromkeys(("message_ids", "request_ids",
                                             "med_change_message_ids"), [_integer])}},
    "signals_truncated": {"total": _integer},
    "attachment": {**_counts("attachment_id", "message_id", "bytes"),
                   "sha256": _token, "state": _enum(*_ATT_STATES)},
    "message": {**_counts("project_id", "message_id", "parent_id"),
                "posted_at_ts": finite_number, "content_hash": _token,
                "body_state": _enum(*_BODY_STATES), "extraction_eligible": _BOOL,
                "state": _STATE, "extraction": {k: {
                    "state": _STATE, "artifact_id": _integer,
                    "last_error": _BOOL, "engine_version": _integer} for k in _KINDS},
                "facts": [_FACT], "relations": [{"left_fact_id": _token,
                    "right_fact_id": _token, "kind": _enum(
                        "EXACT_DUPLICATE", "EXPLICIT_SUPERSESSION", "TRANSITION",
                        "CONTRADICTION", "COMPLEMENTS", "UNRESOLVED")}]},
}
RECORD_TYPES = (*_SCHEMAS, "stat")


def _select(value, schema):
    if value is None:
        return None  # missing measurements stay unknown, never zero
    if isinstance(schema, dict) and isinstance(value, dict):
        return {key: _select(value[key], child) for key, child in schema.items()
                if key in value}
    if isinstance(schema, list) and isinstance(value, list):
        return [_select(item, schema[0]) for item in value]
    if callable(schema) and schema(value):
        return value
    raise ValueError("aggregate_field_type_invalid")


def project_record(record: dict) -> dict:
    """Select aggregate fields without forwarding arbitrary nested content."""
    if not isinstance(record, dict):
        raise ValueError("record_not_object")
    kind = record.get("type")
    if kind == "stat":
        name = record.get("name")
        if not isinstance(name, str) or name not in _STATS:
            raise ValueError("stat_not_exportable")
        schema = {"preset": _enum("operational", "pharmacy"), "name": _enum(name),
                  "value": {**_STAT_COMMON, **_STATS[name]}}
    elif isinstance(kind, str) and kind in _SCHEMAS:
        schema = _SCHEMAS[kind]
    else:
        raise ValueError("record_type_unknown")
    result = _select(record, {**_BASE, **schema})
    for key in ("type", "contract", "snapshot_generation_id"):
        if result.get(key) is None:
            raise ValueError("record_provenance_missing")
    required = {"message": ("project_id", "message_id"),
                "signal": ("project_id", "signal_type"),
                "attachment": ("attachment_id", "message_id")}.get(kind, ())
    if any(result.get(key) is None for key in required):
        raise ValueError("record_identity_missing")
    if result != record:
        result["content_omitted"] = True
    return result


def validate_record(record: dict) -> None:
    """Refuse records requiring redaction at a delivery boundary."""
    if project_record(record) != record:
        raise ValueError("record_field_not_exportable")
