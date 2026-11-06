"""Fence extraction source versions and execution limits against on-disk changes."""
import hashlib
import os
from pathlib import Path
import re
import time
import semantic_quantities
import semantic_transport
import semantic_loops
import semantic_assessment
import patient_context_plan
import patient_context_projection
import semantic_jev
import semantic_runtime
import semantic_usage_receipts
import semantic_projection
import semantic_audit
import semantic_candidate
import semantic_publication
import semantic_conversion
import semantic_store
import semantic_notice
import semantic_render
import semantic_send_gate
import writer_lock
import inference_version as inference
import llm_availability  # noqa: E402
import self_identity  # noqa: E402
import safe_io  # noqa: E402
import bounded_http
import cloud_client
import anthropic_client
import openai_client
import cloud_policy
import cloud_usage_accounting
import cloud_history_scope
import cloud_backlog
import cloud_inference
import llm_retry
import mcs_config
import clinical_values
import clinical_urgency
import clinical_scope
import clinical_events
import extract_schema
import extract_validation
import extract_quality
import clinical_chunking
import extraction_refresh
import extract
import intervention_schedule
import local_llm
import patient_context
import patient_context_refresh
import initial_import_state
import history_walk
import mcs_queries
import inference_dependency_loss
import patient_context_artifacts
import mcs_util
import semantic_assembly
import semantic_facts
import extraction_context
import patient_priority
import patient_context_scan
import patient_context_schedule
import patient_readiness_witness
import extraction_medications
import drug_map
import drug_mentions
from mcs_queries import (current_v4_id)
from mcs_util import HOME

from extract_llm_prompts import (
    MAX_TOKENS,
)

DB = os.path.join(HOME, "data", "ledger.db")


KIND = "extract_llm"


ENDPOINT = local_llm.ENDPOINT


MODEL = local_llm.MODEL


_ENDPOINT_PIN, _MODEL_PIN = ENDPOINT, MODEL


TIMEOUT = 300


EXTRACT_VERSION = inference.LLM_EXTRACT_VERSION


PATIENT_CONTEXT_VERSION = 4


_LOADED_SOURCE_PATHS = dict((("extract_llm", str(Path(__file__).with_name("extract_llm.py"))),
                       ("cloud_client", cloud_client.__file__),
                       ("anthropic_client", anthropic_client.__file__),
                       ("openai_client", openai_client.__file__),
                       ("cloud_policy", cloud_policy.__file__),
                       ("cloud_usage_accounting", cloud_usage_accounting.__file__),
                       ("cloud_history_scope", cloud_history_scope.__file__),
                       ("cloud_backlog", cloud_backlog.__file__),
                       ("cloud_inference", cloud_inference.__file__),
                       ("llm_retry", llm_retry.__file__),
                       ("mcs_config", mcs_config.__file__),
                       ("mcs_queries", mcs_queries.__file__),
                       ("mcs_util", mcs_util.__file__),
                       ("bounded_http", bounded_http.__file__),
                       ("writer_lock", writer_lock.__file__),
                       ("inference_version", inference.__file__),
                       ("inference_dependency_loss", inference_dependency_loss.__file__),
                       ("patient_context_artifacts", patient_context_artifacts.__file__),
                       ("semantic_store", semantic_store.__file__),
                       ("semantic_notice", semantic_notice.__file__),
                       ("semantic_render", semantic_render.__file__),
                       ("semantic_send_gate", semantic_send_gate.__file__),
                       ("semantic_v4", semantic_publication.__file__),
                       ("semantic_v4_cohort", semantic_conversion.__file__),
                       ("semantic_audit", semantic_audit.__file__),
                       ("semantic_candidate", semantic_candidate.__file__),
                       ("semantic_projection", semantic_projection.__file__),
                       ("semantic_runtime", semantic_runtime.__file__),
                       ("semantic_usage_receipts", semantic_usage_receipts.__file__),
                       ("semantic_jev", semantic_jev.__file__),
                       ("patient_context_plan", patient_context_plan.__file__),
                       ("patient_context_state", patient_context_projection.__file__),
                       ("patient_context_refresh", patient_context_refresh.__file__),
                       ("initial_import_state", initial_import_state.__file__),
                       ("history_walk", history_walk.__file__),
                       ("semantic_transport", semantic_transport.__file__),
                       ("semantic_loops", semantic_loops.__file__),
                       ("semantic_assessment", semantic_assessment.__file__),
                       ("llm_availability", llm_availability.__file__),
                       ("self_identity", self_identity.__file__),
                       ("safe_io", safe_io.__file__),
                       ("extract", extract.__file__),
                       ("intervention_schedule", intervention_schedule.__file__),
                       ("clinical_values", clinical_values.__file__),
                       ("clinical_urgency", clinical_urgency.__file__),
                       ("clinical_scope", clinical_scope.__file__),
                       ("clinical_events", clinical_events.__file__),
                       ("clinical_chunking", clinical_chunking.__file__),
                       ("semantic_quantities", semantic_quantities.__file__),
                       ("extraction_refresh", extraction_refresh.__file__),
                       ("patient_context", patient_context.__file__),
                       ("semantic_assembly", semantic_assembly.__file__),
                       ("semantic_facts", semantic_facts.__file__),
                       ("patient_state_context", extraction_context.__file__),
                       ("local_llm", local_llm.__file__),
                       ("extract_schema", extract_schema.__file__),
                       ("extract_validation", extract_validation.__file__),
                       ("extract_quality", extract_quality.__file__)))


_LOADED_SOURCE_PATHS.update({"extraction_medications": extraction_medications.__file__,
                             "drug_mentions": drug_mentions.__file__, "drug_map": drug_map.__file__,
                             "patient_priority": patient_priority.__file__,
                             "patient_context_scan": patient_context_scan.__file__,
                             "patient_context_schedule": patient_context_schedule.__file__,
                             "patient_readiness_witness": patient_readiness_witness.__file__})


_LOADED_SOURCE_PATHS.update({
    'attachment_cloud_identity': str(Path(__file__).resolve().parents[1] / 'attachment_cloud_identity.py'),
    'patient_read_scope': str(Path(__file__).resolve().parents[2] / 'core' / 'patient_read_scope.py'),
    'attachment_feedback': str(Path(__file__).resolve().parents[1] / 'attachment_feedback.py'),
    'attachment_observation_seeds': str(Path(__file__).resolve().parents[1] / 'clinical' / 'attachment_observation_seeds.py'),
    'measurement_sources': str(Path(__file__).resolve().parents[2] / 'patients' / 'measurement_sources.py'),
    'measurement_timeline': str(Path(__file__).resolve().parents[2] / 'patients' / 'measurement_timeline.py'),
    'ocr_model_contract': str(Path(__file__).resolve().parents[1] / 'ocr_model_contract.py'),
    'attachment_ocr': str(Path(__file__).resolve().parents[1] / 'attachment_ocr.py'),
    'attachment_inference': str(Path(__file__).resolve().parents[1] / 'attachment_inference.py'),
    'attachment_extraction': str(Path(__file__).resolve().parents[1] / 'attachment_extraction.py'),
    'attachment_inference_checkpoints': str(Path(__file__).resolve().parents[1] / 'attachment_inference_checkpoints.py'),
    'attachment_inference_contract': str(Path(__file__).resolve().parents[1] / 'attachment_inference_contract.py'),
    'extraction_provenance': str(Path(__file__).with_name('extraction_provenance.py')),
    'attachment_refactor_witness': str(Path(__file__).resolve().parents[3] / 'resources' / 'inference-contracts'
                                       / 'attachment-refactor-20261105.json'),
    'attachment_paid_results': str(Path(__file__).resolve().parents[1] / 'attachment_paid_results.py'),
    'original_source_keys': str(Path(__file__).resolve().parents[2] / 'core' / 'original_source_keys.py')})


_LOADED_SOURCE_PATHS.update({
    "extract_llm_admission": str(Path(__file__).with_name("extract_llm_admission.py")),
    "extract_llm_call_context": str(Path(__file__).with_name("extract_llm_call_context.py")),
    "extract_llm_carry": str(Path(__file__).with_name("extract_llm_carry.py")),
    "extract_llm_context": str(Path(__file__).with_name("extract_llm_context.py")),
    "extract_llm_engine": str(Path(__file__).with_name("extract_llm_engine.py")),
    "extract_llm_errors": str(Path(__file__).with_name("extract_llm_errors.py")),
    "extract_llm_prepare": str(Path(__file__).with_name("extract_llm_prepare.py")),
    "extract_llm_progress": str(Path(__file__).with_name("extract_llm_progress.py")),
    "extract_llm_prompts": str(Path(__file__).with_name("extract_llm_prompts.py")),
    "extract_llm_proofs": str(Path(__file__).with_name("extract_llm_proofs.py")),
    "extract_llm_provider": str(Path(__file__).with_name("extract_llm_provider.py")),
    "extract_llm_qc": str(Path(__file__).with_name("extract_llm_qc.py")),
    "extract_llm_resident": str(Path(__file__).with_name("extract_llm_resident.py")),
    "extract_llm_runner": str(Path(__file__).with_name("extract_llm_runner.py")),
    "extract_llm_selection": str(Path(__file__).with_name("extract_llm_selection.py")),
    "extract_llm_source_contract": str(Path(__file__).with_name("extract_llm_source_contract.py")),
    "extract_llm_store": str(Path(__file__).with_name("extract_llm_store.py")),
})

_LOADED_SOURCE_PATHS.update({
    'semantic_summary_stages': str(Path(__file__).resolve().parents[2] / 'semantic' / 'semantic_summary_stages.py'),
    'semantic_fact_stages': str(Path(__file__).resolve().parents[2] / 'semantic' / 'semantic_fact_stages.py'),
    'semantic_job_processor': str(Path(__file__).resolve().parents[2] / 'semantic' / 'semantic_job_processor.py'),
    'semantic_reprocess': str(Path(__file__).resolve().parents[2] / 'semantic' / 'semantic_reprocess.py'),
    'semantic_scheduler_state': str(Path(__file__).resolve().parents[2] / 'semantic' / 'semantic_scheduler_state.py'),
    'semantic_due_selection': str(Path(__file__).resolve().parents[2] / 'semantic' / 'semantic_due_selection.py'),
    'semantic_drain_accounting': str(Path(__file__).resolve().parents[2] / 'semantic' / 'semantic_drain_accounting.py'),
    'patient_context_refresh_state': str(Path(__file__).resolve().parents[2] / 'patients' / 'patient_context_refresh_state.py'),
    'patient_context_quality_receipts': str(Path(__file__).resolve().parents[2] / 'patients' / 'patient_context_quality_receipts.py'),
    'patient_context_refresh_sources': str(Path(__file__).resolve().parents[2] / 'patients' / 'patient_context_refresh_sources.py'),
    'patient_context_refresh_readiness': str(Path(__file__).resolve().parents[2] / 'patients' / 'patient_context_refresh_readiness.py'),
    'patient_context_quality_controller': str(Path(__file__).resolve().parents[2] / 'patients' / 'patient_context_quality_controller.py'),
    'rollup_builder': str(Path(__file__).resolve().parents[2] / 'patients' / 'rollup_builder.py'),
})


_LOADED_SOURCE_DIGESTS = {
    name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
    for name, path in _LOADED_SOURCE_PATHS.items()}


_WRITER_STATE = writer_lock.updater_state()


_INFERENCE_VERSION = inference.CURRENT_VERSION


_INFERENCE_SOURCE_STAT = os.stat(inference.__file__).st_mtime_ns


def _writer_current(*, full: bool = False) -> bool:
    """Fence update boundaries; lockless bookkeeping checks only the version source."""
    try:
        current = (not _WRITER_STATE[0]
            and not writer_lock.writer_blocked()
            and os.stat(inference.__file__).st_mtime_ns == _INFERENCE_SOURCE_STAT
            and inference.CURRENT_VERSION == _INFERENCE_VERSION)
    except OSError:
        return False
    return current and (not full or writer_lock.updater_state() == _WRITER_STATE)


def _unsealed_source_pred(r) -> str:
    """A sealed (v4-adopted) source still admits a required contract-bound
    cloud write; every other caller keeps the sealed-source fence."""
    return ("1=1" if isinstance(r, dict) and r.get("_required_cloud_contract")
            else f"{current_v4_id()} IS NULL")


def _source_writable(db, r) -> bool:
    if isinstance(r, dict) and r.get('_source_binding') is not None:
        from inference_dependency_loss import source_binding
        if source_binding(db, r['project_id'], r['message_id']) != r['_source_binding']:
            return False
    return _writer_current() and db.execute(
        "SELECT 1 FROM messages m WHERE m.message_id=? AND m.project_id=? "
        "AND m.content_hash=? AND (m.body_state IS NULL OR m.body_state='full') "
        "AND length(m.body_text)>0 " + f"AND {_unsealed_source_pred(r)}",
        (r["message_id"], r["project_id"], r["content_hash"])).fetchone() is not None


_CHUNK_SIZE = 3000


_PROGRESS_MAX_NODES = 16384


_INTERNAL_REPAIR_PROOF = "__mcs_internal_proof_v1"


_INTERNAL_LEAF_CLAIMS = "__mcs_leaf_claims_v1"


_QUALITY_TERMINAL = ("hint_gaps_resolved", "unresolved", "dispatch_unknown", "skipped_budget")


_DEFERRED = object()


REVIVE_MAX = 30


REVIVE_PER_INPUT = 3


REVIVE_COOLDOWN_S = 6 * 3600


UPGRADE_KIND = "extract_upgrade_v1"


_PREPARE_HOLD_S = 60  # a refused context waits without consuming an inference attempt


_EXTRACT_LEASE_S = 900   # crash → the claim self-expires; a stolen


_CARRY_MARGIN_S = 120    # a carried result is dropped this close to its


_RESIDENT_WRITE_WAIT_S = 3   # residents carry commits instead of waiting out the tick


_BATCH_K = 0                     # --batch defaults off; rejected batches retry as singles.


_BATCH_MAX_TOKENS = MAX_TOKENS * 3   # K outputs share one envelope;


_MIN_CALL_S = 75.0      # one full extraction call (eval + decode)


_MIN_REPAIR_S = 75.0    # repair re-ask — same shape as a single call


_BATCH_PER_ITEM_S = 55.0  # decode share per item inside a batch call


_STALE_GC_S = 300.0


def _owns_current_source(ledger, r, lease: float) -> bool:
    if r.get("_cloud_background"):
        if (not cloud_policy.history_target_ready(ledger.db, r["project_id"], r["message_id"])
                or not r.get('_cloud_history_snapshot')
                or cloud_policy.history_thread_snapshot(ledger.db, r["project_id"], r["message_id"])
                    != r['_cloud_history_snapshot']):
            return False
    token = r.get("_claim_token") if isinstance(r, dict) else None
    return ledger.db.execute(
        "SELECT 1 FROM messages m JOIN fetch_jobs j "
        "ON j.message_id=m.message_id AND j.project_id=m.project_id "
        "WHERE m.message_id=? AND m.content_hash=? "
        "AND (m.body_state IS NULL OR m.body_state='full') "
        "AND j.kind='extract_claim' AND j.next_try=? AND j.next_try>? "
        "AND CASE WHEN json_valid(j.payload) THEN json_extract(j.payload,'$.owner_token') END IS ?",
        (r["message_id"], r["content_hash"], lease, time.time(), token)).fetchone() is not None


def _rule_hints(r):
    """v1/v2 deterministic parse of this exact body — the v3 pass folds
    the rule stage's work into itself: the same dict rides the LLM
    prompt as candidates AND backs the extract_v1 artifact write, so
    every covered message gets both analyses in one pass. Returns None
    when the parser cannot produce a dict (pathological input) — the
    LLM lane must never die on the hint path."""
    try:
        import extract
        d = extract.extract_message(r["body_text"], r["posted_at"] or "")
        return d if isinstance(d, dict) else None
    except Exception:
        return None


_GREETING_RE = re.compile(r"[\s、。，,.！？!?]*(?:おはようございます|こんにちは|こんばんは)[\s、。，,.！？!?]*")


_PREFILTER_CONTENT = ('{"meds":[],"symptoms":[],"events":[],'
                      '"requests":[],"vitals":{},'
                      '"points":[],"urgency":"routine"}')


_STALE_GC_KIND = 'extract_stale_gc'


_STALE_GC_PAGE = 64


_STALE_GC_BUDGET_S = 0.25


_STALE_GC_CURSOR_RESERVE_S = 0.05


_STALE_GC_CLAIM_PAGE = 64


_CLOUD_PRIORITY_PAGE = 128


SEMANTIC_FIRST_S = 1800.0


_SEMANTIC_RECENT_TURNS = 3


_ARRIVAL_CARRY_S = 86400      # = run_check.REALTIME_CARRY_S (pinned by a test)


_RESIDENT_STARTUP_WAIT_S = 600.0


_RESIDENT_STARTUP_POLL_S = 0.5
