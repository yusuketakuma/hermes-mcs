"""Semantic policy + config layer (spec §22.1): mode names, artifact
kind constants, schema/policy versions, config validation, and the
policy fingerprint. Fixed Jev registry defaults and conditional candidate
approval checks share this policy boundary with runtime and offline callers."""
from __future__ import annotations

from mcs_util import payload_hash, until_epoch
import provider_settings
import semantic_jev as jev

SCHEMA_VERSION = "2026-09-20"
POLICY_VERSION = "2026-10-16.1"
# Background work leaves this share of the daily Jev budget for new arrivals.
BACKLOG_BUDGET_RESERVE = 0.10
JOB_KIND = "semantic"
QC_JOB_KIND = "extract_qc"
SCHED_KIND = "semantic_sched"
QC_ARTIFACT = "extract_qc"

KIND_BUNDLE = "semantic_bundle"
KIND_ASSESS = "semantic_assess"
KIND_FACTS = "semantic_facts"
KIND_FACTS_V2 = "semantic_facts_v2"
KIND_FACT_AUDIT = "semantic_facts_audit"
KIND_FACT_REPAIR = "semantic_facts_repair"
KIND_FACT_PROJ = "canonical_projection"
KIND_MANIFEST = "semantic_source_manifest"
KIND_SUMMARY = "semantic_summary"
KIND_AUDIT = "semantic_audit"
KIND_LOOP = "loop_candidate"
KIND_LOOP_EVENT = "loop_event"
KIND_PLAN = "notify_plan"
KIND_USAGE = "semantic_usage"
KIND_COVERAGE = "semantic_coverage"
KIND_CANDIDATE = "semantic_candidate"
KIND_REPAIR = "semantic_repair"
KIND_SUMMARY_FORMAT_REPAIR = "semantic_summary_format_repair"
KIND_POLICY = "semantic_policy"
KIND_DRAIN_RUN = "semantic_drain_run"
# Receipt of a shadow v2 comparison that ran for a source + policy and
# stayed incomplete (the incomplete document itself is never stored).
KIND_SHADOW_V2 = "semantic_shadow_v2"

# Interpretation results a semantic LLM writes. Their row ``model`` is the
# configured local model, so a row not proven cloud-produced is current only
# for that model (extraction_provenance.local_producer_current); a cloud
# result keeps its identity when only the local model changes. Chunk kinds
# are gated by the chunk cache, the others by semantic_store.current_artifact.
LOCAL_MODEL_GATED_KINDS = frozenset({
    KIND_FACTS, KIND_FACTS_V2, KIND_FACT_PROJ, "semantic_facts_v4", KIND_SUMMARY, KIND_CANDIDATE,
    "semantic_extraction_chunk", "semantic_extraction_chunk_v2"})
# Jev results, reservation receipts (a spent repair is never refunded),
# completion receipts and bookkeeping: not decided by the local model.
LOCAL_MODEL_UNGATED_KINDS = frozenset({
    KIND_BUNDLE, KIND_ASSESS, KIND_FACT_AUDIT, KIND_FACT_REPAIR, KIND_MANIFEST, KIND_AUDIT,
    KIND_LOOP, KIND_LOOP_EVENT, KIND_PLAN, KIND_USAGE, KIND_COVERAGE, KIND_REPAIR,
    KIND_SUMMARY_FORMAT_REPAIR, KIND_POLICY, KIND_DRAIN_RUN, KIND_SHADOW_V2,
    "semantic_loop_completion", "loop_relation_rejected", "semantic_facts_v4_skipped", "v4_stage",
    "v4_diagnostic", "v4_retire_cohort", "v4_retire_item"})

MODES = ("off", "shadow", "assist", "enforce")
# Which extraction path produces the fact set consumed downstream.
# "legacy" keeps the v1 extractor authoritative; "shadow" runs the
# canonical semantic-facts/v2 document alongside for comparison;
# "canonical" makes the v2 document the source of truth and projects it
# back to the legacy consumer shape.  canonical is a production gate:
# it requires a pinned human-labelled evaluation evidence token.
FACT_SOURCES = ("legacy", "shadow", "canonical")


def review_held(block, key_present, transfer_status) -> bool:
    """True when Jev would run (an explicit mode other than off, or an omitted
    mode with a present key) but the facility review does not cover Jev.
    Without a review record every service is allowed, so nothing is held."""
    import external_transfer
    if isinstance(block, dict) and "mode" in block:
        wanted = block["mode"] in MODES and block["mode"] != "off"
    else:
        wanted = key_present is True and (block is None or isinstance(block, dict))
    return wanted and not external_transfer.service_allowed(transfer_status(), "jev")


def semantic_config(cfg: dict, *, key_present=False, root=None,
                    transfer_status=None) -> tuple[dict, list]:
    """Validate config.json's "semantic" block. Unknown/malformed values
    fail CLOSED — mode falls back to off and each error is reported, so a
    typo can never widen the rollout stage (spec §22.1, AT-067).

    While a facility review record exists, Jev runs only when the review
    covers it, whether the mode is omitted or explicit; every caller shares
    that gate. An explicit mode stays as configured and only Jev is
    withheld: ``jev_held`` is True, every Jev client honours it and jobs run
    with the skip reason ``semantic_jev.REVIEW_HELD`` (local processing,
    the cloud backlog route and enforce publication behave as with any other
    unavailable Jev). An omitted mode exists only because of the Jev key, so
    a hold leaves it off. ``transfer_status()`` defaults to the record under
    ``root``."""
    errors = []
    out = {"mode": "off", "summary_mode": "off", "loop_mode": "off",
           "extract_qc": "off",
           "threshold_mode": "shadow_only", "calibration_version": None,
           "fact_source": "legacy", "fact_source_gate": None,
           "fact_source_evidence": None, "fact_source_approval": None,
           "model": jev.JEV_MODEL,
           "daily_request_budget": jev.DAILY_REQUEST_LIMIT,
           "attempt_timeout_seconds": 20.0,
           "job_budget_seconds": 45.0,
           "max_attempts_per_try": 3,
           "max_questions_per_request": 12,
           "match_threshold": jev.MATCH_THRESHOLD,
           "nomatch_threshold": jev.NOMATCH_THRESHOLD,
           "delayed_notice_seconds": 900,
           "project_ids": [], "until": None,
           "extra_propositions": []}
    block = cfg.get("semantic", {})
    if not isinstance(block, dict):
        out["daily_request_budget"] = 0
        out["jev_held"] = False
        return out, ["config: semantic_not_object"]
    if "mode" in block:
        out["daily_request_budget"] = 0
    if block.keys() - out.keys():
        errors.append("config: semantic_unknown_field")
    if transfer_status is None:
        import external_transfer

        def transfer_status():
            return external_transfer.status(root)
    held = review_held(block, key_present, transfer_status)
    if held and "mode" not in block:
        key_present = False
    mode = block.get("mode", "assist" if key_present is True else "off")
    if mode not in MODES:
        errors.append("config: semantic_mode_invalid")
        mode = "off"
    out["mode"] = mode
    if key_present is True and "mode" not in block and "project_ids" not in block:
        out["project_ids"] = None
    if "mode" in block and mode != "off" and "project_ids" not in block:
        errors.append("config: semantic_project_scope_required")
    for feature in ("summary_mode", "loop_mode"):
        value = block.get(feature, "off")
        if value not in MODES:
            errors.append(f"config: semantic_{feature}_invalid")
        else:
            out[feature] = value
    qc = block.get("extract_qc", "off")
    if qc not in ("off", "annotate"):
        errors.append("config: semantic_extract_qc_invalid")
    else:
        out["extract_qc"] = qc
    fact_source = block.get("fact_source", "legacy")
    gate = block.get("fact_source_gate")
    if gate is not None and (not isinstance(gate, str)
                             or not gate.strip() or len(gate) > 194):
        errors.append("config: semantic_fact_source_gate_invalid")
        gate = None
    out["fact_source_gate"] = gate
    if fact_source not in FACT_SOURCES:
        errors.append("config: semantic_fact_source_invalid")
        fact_source = "legacy"
    elif fact_source == "canonical" and not gate:
        # Production promotion is evidence-gated: the activation command
        # pins a verified human-labelled evaluation report here.
        errors.append("config: semantic_fact_source_gate_required")
    out["fact_source"] = fact_source
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
        if not provider_settings.jev_model_pin_ok(block["model"], jev.JEV_MODEL):
            errors.append("config: semantic_model_invalid")
            out["mode"] = "off"
        out["model"] = jev.resolve_model(block["model"]) or jev.JEV_MODEL
    for key, lo, hi in (("daily_request_budget", 0, jev.DAILY_REQUEST_BUDGET_MAX),
                        ("max_questions_per_request", 1, 48),
                        ("max_attempts_per_try", 1, 3),
                        ("delayed_notice_seconds", 60, 86400)):
        if key in block:
            v = block[key]
            if type(v) is not int or not lo <= v <= hi:
                errors.append(f"config: semantic_{key}_invalid")
            else:
                out[key] = min(v, jev.DAILY_REQUEST_LIMIT) if key == "daily_request_budget" else v
    for key, lo, hi in (("attempt_timeout_seconds", 1.0, 60.0),
                        # 1260 s holds a full backlog commit reserve
                        # (semantic_drain); the pass (+30 s) stays well
                        # inside llm_admission.LEASE_S (1800 s)
                        ("job_budget_seconds", 5.0, 1260.0),
                        ("match_threshold", 0.0, 1.0),
                        ("nomatch_threshold", 0.0, 1.0)):
        if key in block:
            v = block[key]
            if type(v) not in (int, float) or not lo <= v <= hi:
                errors.append(f"config: semantic_{key}_invalid")
            else:
                out[key] = float(v)
    if "project_ids" in block:
        from mcs_config import project_scope
        try:
            out["project_ids"] = project_scope(block["project_ids"])
        except ValueError:
            errors.append("config: semantic_project_ids_invalid")
            out["project_ids"] = []
    extra = block.get("extra_propositions", [])
    if (not isinstance(extra, list)
            or any(type(pid) is not str or pid not in jev.OPTIONAL_PROPOSITIONS
                   for pid in extra)
            or len(extra) != len(set(extra))):
        errors.append("config: semantic_extra_propositions_invalid")
    else:
        out["extra_propositions"] = sorted(extra)
    if out["nomatch_threshold"] >= out["match_threshold"]:
        errors.append("config: semantic_threshold_order_invalid")
    if block.get("until") is not None:
        # Enforced per Jev reservation (semantic_runtime), never by switching modes off:
        # intake, existing notices and in-flight results keep their normal handling.
        try:
            until_epoch(block["until"])
            out["until"] = block["until"]
        except (ValueError, OverflowError):
            errors.append("config: semantic_until_invalid")
    if fact_source == "canonical":
        from semantic_candidate import promotion_problem, token, approval_matches
        evidence = block.get("fact_source_evidence")
        out["fact_source_evidence"] = evidence
        out["fact_source_approval"] = block.get("fact_source_approval")
        try:
            problem = promotion_problem(cfg, evidence)
            if (problem or gate != token(cfg, evidence)
                    or not approval_matches(cfg, evidence, out["fact_source_approval"])):
                errors.append("config: semantic_fact_source_evidence_mismatch")
        except (ValueError, TypeError, KeyError, OSError, AttributeError):
            errors.append("config: semantic_fact_source_evidence_invalid")
    if not errors and fact_source != 'legacy':
        from semantic_candidate import candidate_fingerprint
        out['candidate_fingerprint'] = candidate_fingerprint(cfg)
    out["jev_held"] = held   # set after the unknown-field check: never a config key
    if errors:
        out["fact_source"] = "legacy"
        out["mode"] = "off"
    return out, errors


def off_cause(cfg: dict, *, key_present, root=None, transfer_status=None) -> str | None:
    """Why semantic_config resolves semantic to off ("invalid", "explicit", "review_held" or "no_key"), or None while on."""
    scfg, errors = semantic_config(cfg, key_present=key_present, root=root, transfer_status=transfer_status)
    if scfg["mode"] != "off":
        return None
    block = cfg.get("semantic")
    if errors:
        return "invalid"
    if isinstance(block, dict) and "mode" in block:
        return "explicit"
    return "review_held" if scfg["jev_held"] else "no_key"


def runtime_key_present(cfg, root) -> bool:
    """Whether this MCS dotenv's Jev key can switch an omitted mode on."""
    block = cfg.get("semantic")
    explicit = isinstance(block, dict) and "mode" in block
    return not explicit and bool(jev.api_key(root))


def jev_available(scfg: dict, key_present) -> bool:
    """Whether Jev may be asked now: semantic on, a key present, no review hold and a budget."""
    return (bool(key_present) and scfg["mode"] != "off" and not scfg["jev_held"]
            and scfg["daily_request_budget"] > 0)


def _review_not_required():
    return {"state": "not_required", "auto_enable": True}


def jev_scope_policy(cfg: dict) -> tuple[dict, list]:
    """Resolve the policy Jev would use with a key present, so scope decisions never depend on the key."""
    # Records made with or without the key, and their later credential-free
    # verification, then reach the same patient scope.
    return semantic_config(cfg, key_present=True, transfer_status=_review_not_required)


def jev_scope_excludes(scope_policy: dict, project_id) -> bool:
    """Whether a policy from jev_scope_policy leaves this patient out of Jev's scope."""
    scope = scope_policy["project_ids"]
    return scope_policy["mode"] != "off" and scope is not None and project_id not in scope


def patient_policy(policy: dict, cfg: dict, project_id) -> dict:
    """The semantic policy one patient runs under: outside Jev's patient scope it is off, as without Jev."""
    if policy["mode"] != "off" and jev_scope_excludes(jev_scope_policy(cfg)[0], project_id):
        return {**policy, "mode": "off"}
    return policy


def runtime_config(cfg: dict, *, root=None) -> tuple[dict, list]:
    """Resolve an omitted Jev mode from this MCS dotenv without widening its scope."""
    return semantic_config(cfg, key_present=runtime_key_present(cfg, root), root=root)


def runtime_review_held(cfg: dict, *, root=None) -> bool:
    """True when this installation's facility review keeps Jev from running."""
    import external_transfer
    return review_held(cfg.get("semantic"), runtime_key_present(cfg, root),
                       lambda: external_transfer.status(root))


def primary_verdicts_consumed(scfg: dict) -> bool:
    """Whether a feature reads semantic job results: only summaries and loops use them.

    Those are also the only stages after the Jev assessment, and the only
    ones that call the local model. Without such a reader intake, explicit
    reruns and reprocessing queue no semantic job, none is run and patient
    context never waits on one; a confirmed intake-hold release still
    replaces its held job, which then waits unrun. Extraction QC keeps its
    own lane."""
    return scfg["mode"] != "off" and (scfg["summary_mode"] != "off" or scfg["loop_mode"] != "off")


def waits_for_jev_quota(scfg: dict, lane, kind: str) -> bool:
    """Whether a Jev refused for quota leaves the job due instead of completing it as skipped.

    A backlog job whose whole result is Jev's — every QC job, and a semantic
    job with no reader for its result — would complete with nothing but a skipped
    assessment, so it waits for the next quota window. Realtime jobs complete."""
    return lane == "backlog" and (kind == QC_JOB_KIND or not primary_verdicts_consumed(scfg))


def policy_fingerprint(scfg: dict) -> str:
    """Only interpretation settings invalidate cached analysis, not retry budgets."""
    policy = {key: scfg.get(key) for key in (
        "model", "match_threshold", "nomatch_threshold", "calibration_version")}
    extra = scfg.get("extra_propositions", [])
    if (not isinstance(extra, list) or any(type(pid) is not str
            or pid not in jev.OPTIONAL_PROPOSITIONS for pid in extra)
            or len(extra) != len(set(extra))):
        raise ValueError("semantic_extra_propositions_invalid")
    if extra:
        policy["extra_propositions"] = {pid: jev.OPTIONAL_PROPOSITIONS[pid] for pid in sorted(extra)}
    # Canonical facts require their own coverage/summary audit and mandatory
    # rendering. Keep historical legacy hashes stable, but never reuse their
    # PASS receipts after changing the fact source.
    fact_source = scfg.get("fact_source", "legacy")
    if fact_source != "legacy":
        policy["fact_source"] = fact_source
        policy["candidate_fingerprint"] = scfg.get("candidate_fingerprint")
    return payload_hash(policy)
