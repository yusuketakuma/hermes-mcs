#!/usr/bin/env python3
"""MCS LLM extraction — the extract_llm layer via local llama.cpp (Qwen3.5-9B).

Adds what rule extraction (extract_v1) cannot: dose-less medication names,
negated symptoms ("食欲低下なし"), request targets by profession, and a
one-line summary. Output goes to artifacts kind='extract_llm' alongside
extract_v1 (provenance kept — rules and model never overwrite each other).

Local endpoint: http://127.0.0.1:8080/v1/chat/completions (OpenAI-compatible,
enable_thinking=false for clean JSON). Eligible units can use the configured
cloud route; scope, keys and budgets gate each HTTP attempt.

Incremental: run_pending() extracts only messages lacking a current
extract_llm artifact (content_hash tracked in meta). run_check calls it
with a time budget each tick so the backlog drains gradually. A row whose
current artifact Jev QC flagged re-enters pending once for a feedback
re-extract (meta.qc_fix settles the flag); vitals are anchored to the
nearest measurement label in the body at validation time.

Naming: comments here call this engine "the v3 pass" (the generation
it was built as; see the clinical-plan/v3 prompt key). It lives in
extract/llm/ as the current generation (docs/specs/semantic-evaluation.md),
EXTRACT_VERSION is an independent schema counter. semantic_publication owns
publication across extraction generations.
"""
import argparse as argparse
import contextlib as contextlib
import copy as copy
from datetime import datetime as datetime
import hashlib as hashlib
import json as json
import math as math
import os as os
from pathlib import Path as Path
import queue as queue
import re as re
import sqlite3 as sqlite3
import subprocess as subprocess
import sys as sys
import functools as functools
import threading as threading
import time as time
from types import SimpleNamespace as SimpleNamespace

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every module dir (any depth) as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import _mcs_path as _mcs_path
import semantic_quantities as semantic_quantities
import semantic_transport as semantic_transport
import semantic_loops as semantic_loops
import semantic_assessment as semantic_assessment
import patient_context_plan as patient_context_plan
import patient_context_projection as patient_context_projection
import semantic_jev as semantic_jev
import semantic_runtime as semantic_runtime
import semantic_usage_receipts as semantic_usage_receipts
import semantic_projection as semantic_projection
import semantic_audit as semantic_audit
import semantic_candidate as semantic_candidate
import semantic_publication as semantic_publication
import semantic_conversion as semantic_conversion
import semantic_store as semantic_store
import semantic_notice as semantic_notice
import semantic_render as semantic_render
import semantic_send_gate as semantic_send_gate
import semantic_policy as semantic_policy
import writer_lock as writer_lock
import inference_version as inference  # noqa: F401 - retained extraction facade alias
import llm_availability as llm_availability
import self_identity as self_identity
import safe_io as safe_io
import bounded_http as bounded_http
import cloud_client as cloud_client
import anthropic_client as anthropic_client
import openai_client as openai_client
import cloud_policy as cloud_policy
import cloud_usage_accounting as cloud_usage_accounting
import cloud_history_scope as cloud_history_scope
import cloud_backlog as cloud_backlog
import cloud_inference as cloud_inference
import llm_retry as llm_retry
import mcs_config as mcs_config
import clinical_values as clinical_values
import clinical_urgency as clinical_urgency
import clinical_scope as clinical_scope
import clinical_events as clinical_events
import extract_schema as extract_schema
import extract_validation as extract_validation
import extract_quality as extract_quality
from extraction_provenance import provider_evidence as _provider_evidence  # noqa: F401 - retained extraction facade alias
from extraction_provenance import provider_strict_dict as _provider_strict_dict  # noqa: F401 - retained extraction facade alias
from extraction_provenance import provider_result_hash as _provider_result_hash  # noqa: F401 - retained extraction facade alias
from extraction_provenance import internal_proof_hash as _internal_proof_hash  # noqa: F401 - retained extraction facade alias
from extraction_provenance import evidence_matches_route as _evidence_matches_route  # noqa: F401 - retained extraction facade alias
from extraction_provenance import prior_provider_evidence as _prior_provider_evidence  # noqa: F401 - retained extraction facade alias
from extraction_provenance import EvidenceLeaf as _EvidenceLeaf  # noqa: F401 - retained extraction facade alias
from extraction_provenance import EvidenceSplit as _EvidenceSplit  # noqa: F401 - retained extraction facade alias
from extraction_provenance import leaf_evidence as _leaf_evidence  # noqa: F401 - retained extraction facade alias
import clinical_chunking as clinical_chunking
import extraction_refresh as extraction_refresh
import extract as extract
import intervention_schedule as intervention_schedule
from clinical_chunking import PLAN_VERSION as PLAN_VERSION
from clinical_chunking import plan_chunks as plan_chunks
import local_llm as local_llm
import patient_context as patient_context
import patient_context_refresh as patient_context_refresh
import initial_import_state as initial_import_state
import history_walk as history_walk
import mcs_queries as mcs_queries
import inference_dependency_loss as inference_dependency_loss
import patient_context_artifacts as patient_context_artifacts
import mcs_util as mcs_util
import semantic_assembly as semantic_assembly
import semantic_facts as semantic_facts
import extraction_context as extraction_context
import patient_priority as patient_priority
import patient_context_scan as patient_context_scan
import patient_context_schedule as patient_context_schedule
import patient_readiness_witness as patient_readiness_witness
import extraction_medications as extraction_medications
import drug_map as drug_map
import drug_mentions as drug_mentions
from ledger import Ledger as Ledger
from ledger import LedgerReader as LedgerReader
from mcs_queries import ELIGIBLE_MESSAGE as ELIGIBLE_MESSAGE
from mcs_queries import EXTRACT_FEEDBACK_KIND as EXTRACT_FEEDBACK_KIND
from mcs_queries import V4_PROJECTION_KIND as V4_PROJECTION_KIND
from mcs_queries import current_extract_pred as current_extract_pred
from mcs_queries import current_qc_pred as current_qc_pred
from mcs_queries import current_v4_id as current_v4_id
from mcs_queries import json_or_null as json_or_null
from mcs_queries import qc_source_id as qc_source_id
from mcs_util import CONF_PATH as CONF_PATH
from mcs_util import HOME as HOME
from mcs_util import json_object as json_object
from mcs_util import load_config as load_config
from mcs_util import loads_dict as loads_dict
from mcs_util import strict_json as strict_json
from writer_lock import acquire_run_lock as acquire_run_lock
from llm_availability import circuit_failure as circuit_failure
from llm_availability import circuit_open_s as circuit_open_s
from llm_availability import circuit_streak as circuit_streak
from llm_availability import circuit_success as circuit_success
from llm_availability import disk_floor_mb as disk_floor_mb
from llm_availability import disk_free_mb as disk_free_mb

from extract_llm_source_contract import (
    DB as DB,
    KIND as KIND,
    ENDPOINT as ENDPOINT,
    MODEL as MODEL,
    _ENDPOINT_PIN as _ENDPOINT_PIN,
    _MODEL_PIN as _MODEL_PIN,
    TIMEOUT as TIMEOUT,
    EXTRACT_VERSION as EXTRACT_VERSION,
    PATIENT_CONTEXT_VERSION as PATIENT_CONTEXT_VERSION,
    _LOADED_SOURCE_PATHS as _LOADED_SOURCE_PATHS,
    _LOADED_SOURCE_DIGESTS as _LOADED_SOURCE_DIGESTS,
    _WRITER_STATE as _WRITER_STATE,
    _INFERENCE_VERSION as _INFERENCE_VERSION,
    _INFERENCE_SOURCE_STAT as _INFERENCE_SOURCE_STAT,
    _writer_current as _writer_current,
    _unsealed_source_pred as _unsealed_source_pred,
    _source_writable as _source_writable,
    _CHUNK_SIZE as _CHUNK_SIZE,
    _PROGRESS_MAX_NODES as _PROGRESS_MAX_NODES,
    _INTERNAL_REPAIR_PROOF as _INTERNAL_REPAIR_PROOF,
    _INTERNAL_LEAF_CLAIMS as _INTERNAL_LEAF_CLAIMS,
    _QUALITY_TERMINAL as _QUALITY_TERMINAL,
    _DEFERRED as _DEFERRED,
    REVIVE_MAX as REVIVE_MAX,
    REVIVE_PER_INPUT as REVIVE_PER_INPUT,
    REVIVE_COOLDOWN_S as REVIVE_COOLDOWN_S,
    UPGRADE_KIND as UPGRADE_KIND,
    _PREPARE_HOLD_S as _PREPARE_HOLD_S,
    _EXTRACT_LEASE_S as _EXTRACT_LEASE_S,
    _CARRY_MARGIN_S as _CARRY_MARGIN_S,
    _RESIDENT_WRITE_WAIT_S as _RESIDENT_WRITE_WAIT_S,
    _BATCH_K as _BATCH_K,
    _BATCH_MAX_TOKENS as _BATCH_MAX_TOKENS,
    _MIN_CALL_S as _MIN_CALL_S,
    _MIN_REPAIR_S as _MIN_REPAIR_S,
    _BATCH_PER_ITEM_S as _BATCH_PER_ITEM_S,
    _STALE_GC_S as _STALE_GC_S,
    _owns_current_source as _owns_current_source,
    _rule_hints as _rule_hints,
    _GREETING_RE as _GREETING_RE,
    _PREFILTER_CONTENT as _PREFILTER_CONTENT,
    _STALE_GC_KIND as _STALE_GC_KIND,
    _STALE_GC_PAGE as _STALE_GC_PAGE,
    _STALE_GC_BUDGET_S as _STALE_GC_BUDGET_S,
    _STALE_GC_CURSOR_RESERVE_S as _STALE_GC_CURSOR_RESERVE_S,
    _STALE_GC_CLAIM_PAGE as _STALE_GC_CLAIM_PAGE,
    _CLOUD_PRIORITY_PAGE as _CLOUD_PRIORITY_PAGE,
    SEMANTIC_FIRST_S as SEMANTIC_FIRST_S,
    _SEMANTIC_RECENT_TURNS as _SEMANTIC_RECENT_TURNS,
    _ARRIVAL_CARRY_S as _ARRIVAL_CARRY_S,
    _RESIDENT_STARTUP_WAIT_S as _RESIDENT_STARTUP_WAIT_S,
    _RESIDENT_STARTUP_POLL_S as _RESIDENT_STARTUP_POLL_S,
)
from extract_llm_prompts import (
    MAX_TOKENS as MAX_TOKENS,
    _PROMPT_SPEC as _PROMPT_SPEC,
    _PROMPT_EXAMPLES as _PROMPT_EXAMPLES,
    _PROMPT_HEAD as _PROMPT_HEAD,
    _CTX_HEAD as _CTX_HEAD,
    _CTX_TAIL as _CTX_TAIL,
    _KARTE_HEAD as _KARTE_HEAD,
    _KARTE_TAIL as _KARTE_TAIL,
    _KARTE_MAX as _KARTE_MAX,
    _HINT_HEAD as _HINT_HEAD,
    _HINT_TAIL as _HINT_TAIL,
    _FEEDBACK_HEAD as _FEEDBACK_HEAD,
    _TARGET_HEAD as _TARGET_HEAD,
    _target_head as _target_head,
    _PROMPT_TAIL as _PROMPT_TAIL,
    _BATCH_FMT as _BATCH_FMT,
    _BATCH_HEAD as _BATCH_HEAD,
    _BATCH_TARGET as _BATCH_TARGET,
    _BATCH_HINT as _BATCH_HINT,
    _batch_prompt as _batch_prompt,
    _REPAIR_HEAD as _REPAIR_HEAD,
    _REPAIR_MID as _REPAIR_MID,
    _REPAIR_CLOSE as _REPAIR_CLOSE,
    _repair_prompt as _repair_prompt,
    _piece_reference as _piece_reference,
    _hint_coverage as _hint_coverage,
    _response_has_required_fields as _response_has_required_fields,
    _extract_prompt_context as _extract_prompt_context,
    _CTX_ITEM_MAX as _CTX_ITEM_MAX,
    _CTX_TOTAL_MAX as _CTX_TOTAL_MAX,
    _sanitize_ctx as _sanitize_ctx,
    _HINT_MAX as _HINT_MAX,
    _hint_block as _hint_block,
)
from extract_llm_provider import (
    _FMT_MODE as _FMT_MODE,
    _FMT_TS as _FMT_TS,
    _PROBE_RETRY_S as _PROBE_RETRY_S,
    _NEXT_FMT as _NEXT_FMT,
    resolved_model as resolved_model,
    _resolved_llm as _resolved_llm,
    _probe_format as _probe_format,
    _leaf_record as _leaf_record,
    pin_target as pin_target,
    _note_list as _note_list,
    _integrity_note as _integrity_note,
    _integrity_summary as _integrity_summary,
    _cloud_origin_for_checkpoint as _cloud_origin_for_checkpoint,
    _cloud_mode as _cloud_mode,
    _comparison_fact as _comparison_fact,
    _comparison_extract as _comparison_extract,
    _cloud_compare as _cloud_compare,
    _cloud_compare_tx as _cloud_compare_tx,
    _cloud_reextract as _cloud_reextract,
    _llm_call as _llm_call,
    _slots_busy as _slots_busy,
    pinned_slot_busy as pinned_slot_busy,
    _actual_model as _actual_model,
)
from extract_llm_call_context import (
    _opener_request as _opener_request,
    _CALL_NOTES as _CALL_NOTES,
    _SLOT_OVERRIDE as _SLOT_OVERRIDE,
    _choose_slot as _choose_slot,
)
from extract_llm_engine import (
    llm_extract as llm_extract,
)
from extract_llm_context import (
    thread_context_lines as thread_context_lines,
    _karte_block as _karte_block,
    _thread_context as _thread_context,
    _chunk_context as _chunk_context,
    _frame_meta as _frame_meta,
    _frame_refs as _frame_refs,
    _frame_references_current as _frame_references_current,
    _frame_medications as _frame_medications,
    _medication_reference_current as _medication_reference_current,
    _extraction_dispatch_current as _extraction_dispatch_current,
    _context_contract as _context_contract,
    _PRIOR_CONTEXT_SOURCE as _PRIOR_CONTEXT_SOURCE,
    _PREPARE_CONTEXT_SOURCE as _PREPARE_CONTEXT_SOURCE,
    _context_contracts as _context_contracts,
    _pending_context_frame as _pending_context_frame,
)
from extract_llm_errors import (
    _llm_up as _llm_up,
    _fail_tx as _fail_tx,
    revive_failed as revive_failed,
    _fail as _fail,
    _error_auto_retry as _error_auto_retry,
    _error_attempts as _error_attempts,
    _clear_error_tx as _clear_error_tx,
    _write_lock as _write_lock,
    _claim as _claim,
    _release as _release,
    _renew as _renew,
)
from extract_llm_qc import (
    _AUTO_RETRY_SQL as _AUTO_RETRY_SQL,
    _current as _current,
    _upgrade_contract as _upgrade_contract,
    _prior_extract as _prior_extract,
    _prior_identity as _prior_identity,
    _hold_sql as _hold_sql,
    _verify_created_artifact_tx as _verify_created_artifact_tx,
    _commit_rejected as _commit_rejected,
    _rerender_cards as _rerender_cards,
    _thin_fact_count as _thin_fact_count,
    _register_thin_fact_count as _register_thin_fact_count,
    _thin_pending_sql as _thin_pending_sql,
    _thin_retry_content as _thin_retry_content,
    _human_flagged_sql as _human_flagged_sql,
    _has_kind as _has_kind,
    pending_pred as pending_pred,
    _QC_ACTIONABLE_SQL as _QC_ACTIONABLE_SQL,
    _qc_flagged_sql as _qc_flagged_sql,
    _qc_feedback as _qc_feedback,
    _qc_notes as _qc_notes,
    _compact_qc_feedback as _compact_qc_feedback,
    _snapshot_repair_qc as _snapshot_repair_qc,
    _repair_qc_snapshot_feedback as _repair_qc_snapshot_feedback,
    _ERROR_ROWS_SQL as _ERROR_ROWS_SQL,
    _retry_budget_sql as _retry_budget_sql,
)
from extract_llm_progress import (
    cloud_completion as cloud_completion,
    _progress_steps as _progress_steps,
    _progress_probe as _progress_probe,
    _finish_progress as _finish_progress,
    progress_for_message as progress_for_message,
    _batch_progress_probes as _batch_progress_probes,
    _progress_interrupted as _progress_interrupted,
    _progress_page as _progress_page,
    _copy_progress_value as _copy_progress_value,
    progress_for_messages as progress_for_messages,
)
from extract_llm_store import (
    _record_hold_tx as _record_hold_tx,
    _replace_current as _replace_current,
    _replace_current_tx as _replace_current_tx,
    _commit_extraction_atomic as _commit_extraction_atomic,
    _persist_chunks as _persist_chunks,
)
from extract_llm_selection import (
    _next_retry as _next_retry,
    _IDLE_FLOOR_S as _IDLE_FLOOR_S,
    _next_due_in as _next_due_in,
    patient_extraction_complete as patient_extraction_complete,
    _stale_gc_at as _stale_gc_at,
    _batch_need_s as _batch_need_s,
    _LEFT_ESTIMATE as _LEFT_ESTIMATE,
    _LEFT_ESTIMATE_S as _LEFT_ESTIMATE_S,
    _left_estimate_key as _left_estimate_key,
    _FRESH_SQL as _FRESH_SQL,
    _rehold_sql as _rehold_sql,
    upgrade_status as upgrade_status,
    _selection_sql as _selection_sql,
    _validate_run_args as _validate_run_args,
    _StaleGCDeadline as _StaleGCDeadline,
    _gc_stale_artifacts as _gc_stale_artifacts,
    _FOCUS as _FOCUS,
    _FOCUS_PATIENTS as _FOCUS_PATIENTS,
    _focus_patients as _focus_patients,
    patient_progress_counts as patient_progress_counts,
    _CloudSelectionIncomplete as _CloudSelectionIncomplete,
    _cloud_priority_identity as _cloud_priority_identity,
    _cloud_priority_discard as _cloud_priority_discard,
    _select_pending as _select_pending,
    _select_pending_impl as _select_pending_impl,
)
from extract_llm_proofs import (
    _saved_chunk_size as _saved_chunk_size,
    _chunk_piece as _chunk_piece,
    _chunk_span as _chunk_span,
    _checkpoint_quality_origin as _checkpoint_quality_origin,
    _checkpoint_origin_ref as _checkpoint_origin_ref,
    _replay_repair_leaf as _replay_repair_leaf,
    _saved_chunk_state as _saved_chunk_state,
    _checkpoint_write as _checkpoint_write,
    _checkpoint_identity as _checkpoint_identity,
    _capture_checkpoint_state as _capture_checkpoint_state,
    _checkpoint_keys as _checkpoint_keys,
    _checkpoint_matches as _checkpoint_matches,
    _checkpoint_state_matches as _checkpoint_state_matches,
    _EXTRACT_PROOF_FIELDS as _EXTRACT_PROOF_FIELDS,
    _extract_inline_proof as _extract_inline_proof,
    _quality_record_consistent as _quality_record_consistent,
    _verify_completeness_claim as _verify_completeness_claim,
    _verify_inline_extract_proof as _verify_inline_extract_proof,
    _delete_checkpoint_keys_tx as _delete_checkpoint_keys_tx,
    _CheckpointChanged as _CheckpointChanged,
    _verify_unique_llm_current as _verify_unique_llm_current,
    _verify_atomic_witness as _verify_atomic_witness,
)
from extract_llm_prepare import (
    _prefilter_enabled as _prefilter_enabled,
    _low_signal as _low_signal,
    _mark_prefiltered as _mark_prefiltered,
    _v1_write_transaction as _v1_write_transaction,
    _ensure_v1 as _ensure_v1,
    _PREPARE_CONTEXT_REASONS as _PREPARE_CONTEXT_REASONS,
    _prepare_context_rejection as _prepare_context_rejection,
    _prepare_jobs as _prepare_jobs,
    _plan_units as _plan_units,
    _backlog_report as _backlog_report,
)
from extract_llm_runner import (
    _run_batch_call as _run_batch_call,
    run_pending as run_pending,
)
from extract_llm_admission import (
    _COUNT_KEYS as _COUNT_KEYS,
    _llm_call_stats as _llm_call_stats,
    legacy_admissions as legacy_admissions,
    cloud_backlog_admissions as cloud_backlog_admissions,
    _admission_closed_last as _admission_closed_last,
    _admission_closed as _admission_closed,
)
from extract_llm_resident import (
    _SEMANTIC_WORK as _SEMANTIC_WORK,
    _recent_arrivals as _recent_arrivals,
    _background_semantic as _background_semantic,
    _context_memory_available as _context_memory_available,
    _CLOUD_OPERATING_LIMITS as _CLOUD_OPERATING_LIMITS,
    _same_but_operating_limits as _same_but_operating_limits,
    _background_context_refresh as _background_context_refresh,
    main as main,
    _RESIDENT_FENCE_LOST as _RESIDENT_FENCE_LOST,
    _RESIDENT_SOURCE_ROOT as _RESIDENT_SOURCE_ROOT,
    _resident_source_state as _resident_source_state,
    _resident_loaded_sources_current as _resident_loaded_sources_current,
    _open_current_ledger as _open_current_ledger,
    _open_resident_ledger as _open_resident_ledger,
    _main as _main,
)
from extract_llm_carry import (
    _settle_carry as _settle_carry,
)

if __name__ == "__main__":
    sys.exit(main())
