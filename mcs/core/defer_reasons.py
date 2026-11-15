"""Fixed defer-reason vocabularies that extraction and semantic passes may report."""

# Fixed defer reasons reported by extract_llm.run_pending and semantic_drain.run_due — a raw
# error class, config value or id is never a valid reason in the pass record.
DEFER_REASONS = {"deadline", "probe_unobserved", "admission", "endpoint_down",
                 "not_sent", "busy", "cloud_fallback", "circuit_open",
                 "prepare_blocked", "currentness_unverified", "cloud_guard",
                 "commit_rejected", "prior_unavailable", "worker_error", "write_unavailable",
                 "update_fence", "lease_renewal_failed", "checkpoint_unverified", "quality_checkpoint_unverified",
                 "repair_unavailable", "unclassified",
                 "cloud_unit_unresolved", "cloud_owner_transaction", "cloud_deadline",
                 "cloud_budget_blocked", "cloud_pricing_unverified",
                 "cloud_not_sent", "llm_turn_busy", "budget_short",
                 "paused", "mode_off", "out_of_scope", "jev_skip_unverified",
                 "prepare_deadline", "prepare_source_changed", "prepare_cloud_disabled",
                 "prepare_profile_unavailable", "prepare_medication_changed",
                 "prepare_context_invalid", "prepare_database_unavailable", "other",
                 "cloud_capacity", "cloud_profile_unavailable", "cloud_source_unverified",
                 "cloud_target_unverified", "audit_pending", "loops_incomplete"}
# Semantic gates emit only these branch labels; no provider text or source id.
SEMANTIC_STOP_REASONS = frozenset({
    "fact_stage_incomplete", "fact_stage_retryable", "fact_stage_hard_fail",
    "fact_repaired_producer_unverified", "fact_repaired_contract_changed",
    "fact_cached_rebuild_unverified",
    "fact_extract_deadline", "fact_extract_model", "fact_extract_incomplete",
    "fact_coverage_incomplete", "fact_audit_incomplete", "fact_audit_deadline",
    "fact_audit_wait", "fact_repair_deadline", "empty_repair_unverified",
    "empty_repair_marker_missing", "empty_repair_marker_invalid",
    "empty_repair_marker_binding", "empty_repair_marker_currentness",
    "empty_repair_candidate_missing", "empty_repair_candidate_invalid",
    "empty_repair_candidate_currentness", "empty_repair_candidate_binding",
    "empty_repair_marker_link", "empty_repair_claims_invalid",
    "empty_repair_claim_binding", "empty_repair_prior_unverified",
    "empty_repair_provenance_unverified", "empty_repair_audit_not_ready",
    "empty_repair_validation_unavailable",
})
DEFER_REASONS.update(SEMANTIC_STOP_REASONS)
PREPARE_CONTEXT_REJECTION_CODES = frozenset({
    "prepare_context_invalid", "prepare_context_other", "prepare_context_load_unverifiable",
    "prepare_context_load_superseded", "prepare_context_load_reference_changed",
    "prepare_context_medication_input_invalid",
    "prepare_context_medication_snapshot_invalid", "prepare_context_prepare_transaction_owned",
    "prepare_context_prepare_options_invalid", "prepare_context_prepare_budget_exceeded",
    "prepare_context_prepare_medication_invalid", "prepare_context_prepare_source_changed",
    "prepare_context_prepare_snapshot_invalid",
})
DEFER_REASONS.update(PREPARE_CONTEXT_REJECTION_CODES)
COMMIT_REJECTION_REASONS = frozenset({
    "transaction", "source_or_owner", "checkpoint", "prior_changed", "quality_proof",
    "completeness", "context", "reference", "medication", "retained_proof", "result_seal",
    "insert_fence", "cloud_guard", "superseded_guard",
})
COMPLETENESS_REJECTION_REASONS = frozenset("completeness_" + reason for reason in (
    "shape", "layout", "pending", "adopted", "terminal", "coverage", "validation"))
COMMIT_REJECTION_REASONS |= COMPLETENESS_REJECTION_REASONS
COMMIT_REJECTION_CODES = frozenset("commit_" + reason for reason in COMMIT_REJECTION_REASONS)
DEFER_REASONS.update(COMMIT_REJECTION_CODES)
# Defer reasons that keep a bounded claim instead of re-heading the queue:
# the unit's evidence cannot be proven this pass — an unverifiable head
# must not starve the tail under a limited claim window.
HOLD_DEFER_REASONS = frozenset(
    {"cloud_unit_unresolved", "cloud_source_unverified", "currentness_unverified",
     "prepare_profile_unavailable"})
