"""Semantic policy + config layer (spec §22.1): mode names, artifact
kind constants, schema/policy versions, config validation, and the
policy fingerprint. Leaf module — every other semantic_* module may
import it; it imports none of them."""
from __future__ import annotations

import math

from mcs_requests import payload_hash
import semantic_jev as jev

SCHEMA_VERSION = "2026-09-20"
POLICY_VERSION = "2026-09-21.2"
JOB_KIND = "semantic"

KIND_BUNDLE = "semantic_bundle"
KIND_ASSESS = "semantic_assess"
KIND_FACTS = "semantic_facts"
KIND_SUMMARY = "semantic_summary"
KIND_AUDIT = "semantic_audit"
KIND_LOOP = "loop_candidate"
KIND_LOOP_EVENT = "loop_event"
KIND_PLAN = "notify_plan"
KIND_USAGE = "semantic_usage"

MODES = ("off", "shadow", "assist", "enforce")


def semantic_config(cfg: dict) -> tuple[dict, list]:
    """Validate config.json's "semantic" block. Unknown/malformed values
    fail CLOSED — mode falls back to off and each error is reported, so a
    typo can never widen the rollout stage (spec §22.1, AT-067)."""
    errors = []
    out = {"mode": "off", "summary_mode": "off", "loop_mode": "off",
           "threshold_mode": "shadow_only", "calibration_version": None,
           "model": jev.JEV_MODEL,
           "daily_request_budget": 0,
           "attempt_timeout_seconds": 20.0,
           "job_budget_seconds": 45.0,
           "max_attempts_per_try": 3,
           "max_questions_per_request": 12,
           "match_threshold": jev.MATCH_THRESHOLD,
           "nomatch_threshold": jev.NOMATCH_THRESHOLD,
           "delayed_notice_seconds": 900,
           "project_ids": []}
    block = cfg.get("semantic")
    if block is None:
        return out, errors
    if not isinstance(block, dict):
        return out, ["config: semantic_not_object"]
    if block.keys() - out.keys():
        errors.append("config: semantic_unknown_field")
    mode = block.get("mode", "off")
    if mode not in MODES:
        errors.append("config: semantic_mode_invalid")
        mode = "off"
    out["mode"] = mode
    if mode != "off" and "project_ids" not in block:
        errors.append("config: semantic_project_scope_required")
    for feature in ("summary_mode", "loop_mode"):
        value = block.get(feature, "off")
        if value not in MODES:
            errors.append(f"config: semantic_{feature}_invalid")
        else:
            out[feature] = value
    threshold_mode = block.get("threshold_mode", "shadow_only")
    if threshold_mode not in ("shadow_only", "calibrated"):
        errors.append("config: semantic_threshold_mode_invalid")
    else:
        out["threshold_mode"] = threshold_mode
    calibration = block.get("calibration_version")
    if calibration is not None and (not isinstance(calibration, str)
                                    or not calibration.strip()
                                    or len(calibration) > 128):
        errors.append("config: semantic_calibration_version_invalid")
    else:
        out["calibration_version"] = calibration
    if ("enforce" in (mode, out["summary_mode"], out["loop_mode"])
            and (threshold_mode != "calibrated" or not calibration)):
        errors.append("config: semantic_calibration_required")
    if "model" in block:
        if block["model"] != jev.JEV_MODEL:
            errors.append("config: semantic_model_invalid")
            out["mode"] = "off"
        out["model"] = jev.JEV_MODEL
    for key, lo, hi in (("daily_request_budget", 0, 100000),
                        ("max_questions_per_request", 1, 48),
                        ("max_attempts_per_try", 1, 3),
                        ("delayed_notice_seconds", 60, 86400)):
        if key in block:
            v = block[key]
            if type(v) is not int or not lo <= v <= hi:
                errors.append(f"config: semantic_{key}_invalid")
            else:
                out[key] = v
    for key, lo, hi in (("attempt_timeout_seconds", 1.0, 60.0),
                        ("job_budget_seconds", 5.0, 300.0),
                        ("match_threshold", 0.0, 1.0),
                        ("nomatch_threshold", 0.0, 1.0)):
        if key in block:
            v = block[key]
            if type(v) not in (int, float) or not math.isfinite(v) \
                    or not lo <= v <= hi:
                errors.append(f"config: semantic_{key}_invalid")
            else:
                out[key] = float(v)
    if "project_ids" in block:
        v = block["project_ids"]
        if v is None:
            out["project_ids"] = None          # explicit null = all
        elif not isinstance(v, list) \
                or any(type(p) is not int or p <= 0 for p in v):
            errors.append("config: semantic_project_ids_invalid")
            out["project_ids"] = []
        else:
            out["project_ids"] = sorted(set(v))
    if out["nomatch_threshold"] >= out["match_threshold"]:
        errors.append("config: semantic_threshold_order_invalid")
    if errors:
        out["mode"] = "off"
    return out, errors


def policy_fingerprint(scfg: dict) -> str:
    """Only interpretation settings invalidate cached analysis, not retry budgets."""
    return payload_hash({key: scfg.get(key) for key in (
        "model", "match_threshold", "nomatch_threshold", "calibration_version")})
