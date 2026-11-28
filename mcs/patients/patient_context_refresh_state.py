"""Patient context contracts, artifact identity and shared execution dependencies."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import time as time
from error_codes import KNOWN_ERROR_CODES
from writer_lock import acquire_run_lock as acquire_run_lock
import local_llm
import cloud_inference
import inference_dependency_loss as dependency_loss
import patient_context_artifacts as context_artifacts
from clinical_chunking import PLAN_VERSION
from mcs_queries import JST
from mcs_util import loads_dict, valid_hash
from inference_version import CURRENT_VERSION, VERSION_KEY, current_meta_pred
import patient_context_plan as context_plan
import patient_context_scan as context_scan
import patient_context_selection as context_selection
import mcs_queries as context_queries
import patient_context_projection as context_state
import patient_context_quality_identity as quality_identity



KIND = "patient_context_v1"


LEGACY_KIND = "patient_context_nightly_v1"


SCAN = "patient_context_scan_v1"


CHUNK = "patient_context_scan_chunk_v1"


PROGRESS = "patient_context_progress_v1"


QUALITY = "patient_context_summary_quality_v1"


QUALITY_FAIL = "patient_context_summary_quality_fail_v1"


QUALITY_PASS = "patient_context_summary_quality_pass_v1"


QUALITY_REPAIR = "patient_context_summary_quality_repair_v1"


QUALITY_REPAIR_CANDIDATE = "patient_context_summary_quality_repair_candidate_v1"


QUALITY_RETRY = "patient_context_summary_quality_retry_v1"


QUALITY_CLAIM = "patient_context_summary_quality_claim"


QUALITY_SKIP = "patient_context_summary_quality_skipped_v1"


SOURCE_REPAIR = "patient_context_source_quality_repair_v1"


SOURCE_REPAIR_CANDIDATE = "patient_context_source_quality_repair_candidate_v1"


_INTERNAL_PROOF = "__mcs_internal_proof_v1"


CONTRACT = "patient-context-nightly/1"


SUMMARY_CONTRACT = "patient-context-summary/1"


CALL_SECONDS = 300


def _code(*modules):
    return hashlib.sha256(b"".join(Path(module.__file__).read_bytes() for module in modules)).hexdigest()


# Scan contract: the code that plans chunks, asks for and quotes source facts.
# Facts store no read-time interpretation (temporal is computed on reading).
_SCAN_CODE = _code(context_plan, context_scan)


# Summary contract: fact selection, read-time temporal projection and
# dependency loss. Lane rotation and the bookmark planner only schedule work.
_SUMMARY_CODE = _code(context_selection, context_state, dependency_loss)


COMMIT_MARGIN = cloud_inference.COMMIT_MARGIN


_quality_kind = context_artifacts.quality_kind


def _quality_expected(identity, fid, selected):
    return quality_identity.patient_quality_expected(identity, fid, selected)


class Deferred(Exception):
    """Admission or remaining time is unavailable; no attempt is charged to the unit."""


# The gate at which a quality step (or the save-time quality check) ended without
# passing. Observation only: a recorded code never decides an outcome.
QUALITY_DETAIL_CODES = frozenset({
    # live refusals of the step's current() check
    "deadline", "admission", "writer_changed", "jev_key_changed", "cloud_key_changed",
    "semantic_config_invalid", "semantic_off", "jev_withheld", "jev_scope_excluded",
    "skip_condition_changed", "skip_condition_unread", "patient_paused", "cloud_stopped",
    "assessment_changed", "config_changed", "claim_lost",
    # changed inputs
    "summary_changed", "identity_changed", "chunks_changed", "scope_excluded",
    # the read fence
    "own_write_mismatch", "version_unwitnessed", "witness_mismatch", "replay_commit",
    # steps that end without raising
    "origin_unverified", "jev_unavailable", "jev_window_closed", "jev_usage_unread", "jev_circuit_unclassified",
    "claim_unavailable", "failure_recorded", "jev_client_hooks", "request_problem", "retry_wait",
    "jev_not_evaluated", "prior_failure_blocks_skip", "skip_unrecorded", "candidate_failed",
    # repairs
    "cloud_off", "repair_profile", "repair_budget", "repair_deadline", "repair_prompt_capacity",
    "repair_dispatch", "repair_evidence_held", "repair_slot_unknown", "repair_deferred",
    # the save-time check
    "save_proof_unread", "save_proof_changed", "save_policy_changed", "save_receipt_invalid",
    "save_repair_unverified",
    # Deferred codes a quality step raises (ValueError codes are in KNOWN_ERROR_CODES)
    "patient_summary_quality_lock", "patient_summary_quality_budget_changed",
    "patient_summary_quality_source_changed", "patient_context_source_repair_changed",
    "patient_context_source_repair_lock", "patient_context_capacity_or_deadline",
    "patient_context_admission_deferred", "patient_context_turn_busy",
    "patient_context_endpoint_unsent", "patient_context_endpoint_unavailable",
})


_QUALITY_DETAIL_TYPE = re.compile(r"[A-Z][A-Za-z0-9]{0,69}(?:Error|Exception|Stale|Fallback)")


_quality_detail = ContextVar("patient_context_quality_detail", default=None)


@contextmanager
def capture_quality_detail():
    """Collect the first gate code that the quality checks in this scope record."""
    detail = {"code": None}
    token = _quality_detail.set(detail)
    try:
        yield detail
    finally:
        _quality_detail.reset(token)


def record_quality_detail(code):
    """Note the gate that stops a quality check; the first code in a scope stands.

    Nothing reads the note back to decide an outcome, and outside a capture it is dropped."""
    detail = _quality_detail.get()
    if detail is not None and detail["code"] is None:
        detail["code"] = code


def bounded_quality_detail(value):
    """A fixed gate code, a registered error code or an exception type name; else ``unclassified``."""
    if type(value) is str and (value in QUALITY_DETAIL_CODES or value in KNOWN_ERROR_CODES
                               or _QUALITY_DETAIL_TYPE.fullmatch(value)):
        return value
    return "unclassified"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _credential_tag(jev_key, cloud_key):
    """Fingerprint the Jev and cloud keys so a key change invalidates work without keeping the keys."""
    return _digest([str(key) if key else None for key in (jev_key, cloud_key)])


def _digest_iter(items):
    """_digest(list(items)) without holding the items: same bytes, one item at a time."""
    h = hashlib.sha256(b"[")
    for n, item in enumerate(items):
        h.update(((b"," if n else b"") + json.dumps(item, sort_keys=True, ensure_ascii=False,
                                                    separators=(",", ":")).encode()))
    h.update(b"]")
    return h.hexdigest()


_facts_sig = context_artifacts.facts_signature


def options(cfg):
    """Enabled by default; explicit opt-out and malformed settings stay closed."""
    ext = cfg.get("extract", {}) if isinstance(cfg, dict) else None
    opt = ext.get("patient_context", ext.get("patient_context_nightly", {})) if isinstance(ext, dict) else None
    if (not isinstance(opt, dict) or set(opt) - {"enabled", "publication"}
            or any(type(v) is not bool for v in opt.values())):
        return {"enabled": False, "publication": False}
    return {k: opt.get(k, True) for k in ("enabled", "publication")}


def workday_window(now):
    """Return the current JST calendar day's id and next midnight."""
    stamp = datetime.fromtimestamp(now, JST)
    start = stamp.date()
    end = datetime.combine(start + timedelta(days=1), datetime.min.time(), JST)
    return start.isoformat(), end.timestamp()


def _latest(db, kind, pid, mid=None):
    mid = None if kind in (SCAN, CHUNK) and mid == 0 else mid
    kinds = (kind, LEGACY_KIND) if kind == KIND else (kind,)
    row = db.execute("SELECT content,meta FROM artifacts WHERE kind IN ("
                     + ",".join("?" for _ in kinds) + ") AND project_id=? "
                     "AND message_id IS ? AND " + current_meta_pred("meta", target=CURRENT_VERSION)
                     + " ORDER BY artifact_id DESC LIMIT 1",
                     (*kinds, pid, mid)).fetchone()
    return (loads_dict(row[0]) or {}, loads_dict(row[1]) or {}) if row else ({}, {})


def _published_version(db, pid):
    """Newest context/progress row of this patient: the selection-time
    inputs are stale once it moves."""
    return db.execute("SELECT MAX(artifact_id) FROM artifacts WHERE project_id=? "
                      "AND kind IN (?,?,?) AND " + current_meta_pred("meta", target=CURRENT_VERSION),
                      (pid, KIND, LEGACY_KIND, PROGRESS)).fetchone()[0]


def _artifact_meta(kind, meta, mid):
    meta = {**meta, VERSION_KEY: CURRENT_VERSION}
    if kind in (SCAN, CHUNK):
        meta.setdefault("source_key", mid if mid is not None else 0)
    if kind == SCAN:
        meta['backend_slot'] = _digest([meta.get('contract'), meta.get('provider'),
                                        meta.get('source_binding'), _local_producer(meta)])
    return meta


def _local_producer(meta):
    """The local model of a locally produced row; a cloud row never depends on it."""
    return None if meta.get("provider") else (meta.get("provenance") or {}).get("model")


def _unit_key(source_sig, chunk_span, contract, provider, local_model, occurrence):
    """One source unit per input, scan contract and producing backend.

    `provider` is the route's provider (a local fallback keeps its cloud route),
    so a fallback unit never collides with a plain local unit of the same model."""
    return _digest([source_sig, chunk_span, contract, provider, local_model, occurrence])


def _stored_unit_key(meta, chunk_span):
    """The unit key of a stored source unit, from its own backend identity."""
    return _unit_key(meta["source_sig"], chunk_span, meta["contract"],
                     meta.get("provider") or meta.get("route_provider"),
                     _local_producer(meta), meta["source_occurrence"])


def _replace(ledger, kind, pid, content, meta, mid=None, *, write_count=None):
    # Linked summaries have no messages row; their checkpoint is patient-scoped.
    meta = _artifact_meta(kind, meta, mid)
    mid = None if kind in (SCAN, CHUNK) and mid == 0 else mid
    kinds = (kind, LEGACY_KIND) if kind == KIND else (kind,)
    # Keep previous inference generations and distinct original karte records.
    deleted = ledger.db.execute("DELETE FROM artifacts WHERE kind IN ("
                      + ",".join("?" for _ in kinds) + ") AND project_id=? AND message_id IS ? AND "
                      + current_meta_pred("meta", target=CURRENT_VERSION)
                      + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.source_key') END IS ?"
                      + (" AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.backend_slot') END=?" if kind == SCAN else "")
                      + (" AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.chunk_key') END=?" if kind == CHUNK else ""),
                      (*kinds, pid, mid, meta.get("source_key"), *([meta['backend_slot']] if kind == SCAN else []),
                       *([meta["chunk_key"]] if kind == CHUNK else [])))
    artifact_id = ledger.artifact_add_tx(kind, json.dumps(content, ensure_ascii=False),
                                         project_id=pid, message_id=mid, model=meta.get("model", ""), meta=meta)
    if write_count is not None:
        if deleted.rowcount < 0:
            raise ValueError("patient_context_source_adoption_changed")
        write_count[0] += deleted.rowcount + 1
    return artifact_id


def _retire_cloud_scan_tx(ledger, pid, mid, source_key, source_sig):
    """Take one source input's cloud scan and its units out of every selection.

    Local work replaced their paid requests, so their receipts can never verify again. The
    rows stay as invalidated history, like the scan rows of a source that changed."""
    if not ledger.db.in_transaction:
        raise RuntimeError("patient_context_source_transaction_required")
    ledger.db.execute(
        "UPDATE artifacts SET meta=json_set(meta,'$.invalidated',json('true')) "
        "WHERE kind IN (?,?) AND project_id=? AND message_id IS ? AND "
        + current_meta_pred("meta", target=CURRENT_VERSION)
        + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.source_key') IS ? "
        "AND json_extract(meta,'$.source_sig') IS ? AND json_type(meta,'$.provider')='object' "
        "AND json_extract(meta,'$.invalidated') IS NOT 1 END",
        (SCAN, CHUNK, pid, mid or None, source_key, source_sig))


def _save_source_chunk_tx(ledger, pid, mid, source_key, chunk_span, facts, meta):
    """Replace one source unit and its dependency record within the borrowed writer transaction."""
    if not ledger.db.in_transaction:
        raise RuntimeError("patient_context_source_transaction_required")
    unit_key = _stored_unit_key(meta, chunk_span)
    parts = max(1, math.ceil(len(facts) / context_plan.MAX_SOURCE_FACTS))
    # A unit's complete shard set and cursor advance have one owner TX.
    # An empty successful unit still proves its source interval was visited.
    replaced_ids = [row[0] for row in ledger.db.execute("SELECT artifact_id FROM artifacts WHERE kind=? AND project_id=? AND "
        + current_meta_pred("meta", target=CURRENT_VERSION)
        + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.unit_key') END=?", (CHUNK, pid, unit_key))]
    loss_snapshot = dependency_loss.capture_tx(ledger.db, pid, mid, artifact_ids=replaced_ids,
        **({"source_key": source_key} if isinstance(source_key, str) else {})) if replaced_ids else None
    ledger.db.execute("DELETE FROM artifacts WHERE kind=? AND project_id=? AND "
        + current_meta_pred("meta", target=CURRENT_VERSION)
        + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.unit_key') END=?", (CHUNK, pid, unit_key))
    replacement_ids = []
    for part in range(parts):
        replacement_ids.append(_replace(ledger, CHUNK, pid,
            {"chunk_span": chunk_span, "part": part, "parts": parts,
             "facts": facts[part * context_plan.MAX_SOURCE_FACTS:(part + 1) * context_plan.MAX_SOURCE_FACTS]},
            {**meta, "unit_key": unit_key, "chunk_key": _digest([unit_key, part])}, mid))
    if loss_snapshot is not None:
        dependency_loss.record_tx(ledger.db, loss_snapshot, replacement_ids=replacement_ids)
    return replacement_ids


def _adoption_artifact_matches(db, artifact_id, kind, pid, mid, content, meta):
    meta = _artifact_meta(kind, meta, mid)
    mid = None if kind == SCAN and mid == 0 else mid
    row = db.execute("SELECT kind=?,project_id=?,message_id IS ?,content=?,model=?,meta=? "
        "FROM artifacts WHERE artifact_id=?", (kind, pid, mid, json.dumps(content, ensure_ascii=False),
        meta.get("model", ""), json.dumps(meta, ensure_ascii=False), artifact_id)).fetchone()
    return row is not None and all(value == 1 for value in row)


def _adoption_epoch_expected(db):
    row = db.execute("SELECT job_id,payload FROM fetch_jobs WHERE kind='semantic_sched' "
                     "AND project_id=0 AND message_id=0").fetchone()
    if row is None:
        return None
    state = loads_dict(row[1])
    value = state.get('analysis_epoch', 0) if isinstance(state, dict) else None
    if type(value) is not int or not 0 <= value < 2**63 - 1:
        raise ValueError("patient_context_source_adoption_epoch")
    expected = db.execute("SELECT json_set(?, '$.analysis_epoch', ?)", (row[1], value + 1)).fetchone()[0]
    return row[0], expected


def _contract():
    """Scan contract: code and versions only, never a backend or model name.

    Extraction generations are not part of it: each source signature binds the
    items and summaries the scan reads. ``context`` stays for the item
    derivation code that the signature does not hash."""
    import extract_llm
    return {"version": CONTRACT, VERSION_KEY: CURRENT_VERSION, "chunks": PLAN_VERSION, "facts": 2,
            "scan_code": _SCAN_CODE, "context": extract_llm.PATIENT_CONTEXT_VERSION}


def _summary_contract():
    """Summary contract: selection, read-time projection and dependency loss."""
    return {"version": SUMMARY_CONTRACT, "code": _SUMMARY_CODE}


def _checks():
    """Retry marker: a contract code change reopens exhausted retries."""
    return _digest([_SCAN_CODE, _SUMMARY_CODE])


def _route(cfg, pid=None):
    """Accepted provenance for this patient: the scan contract, the eligible cloud provider and the local model."""
    import cloud_inference
    import cloud_policy
    route = {"contract": _contract(), "provider": None,
             "local_model": local_llm.resolve(cfg)[1], "local_model_pinned": True}
    if pid is not None:
        settings = cloud_inference.resolve_cloud_settings(cfg)
        if (settings["mode"] == "on"
                and cloud_policy.eligible(settings, "patient_context", pid)):
            route["provider"] = cloud_inference.cloud_provider_contract(cfg, route="patient_context", purpose="patient_context")
    return route


def _local_provenance(cfg):
    return {"backend": "local", "model": local_llm.resolve(cfg)[1], "profile": None}


def _cloud_provenance(provider_contract, model, selection):
    return {"backend": provider_contract.get("backend"), "model": model,
            "profile": (selection or {}).get("profile")}


# Backend identity of one artifact, copied as a whole whenever a row is reused.
_BACKEND_KEYS = ("contract", "provider", "route_provider", "local_fallback", "model", "provenance")


def _record_backend(meta, provenance, *, provider=None, route_provider=None):
    """Record which backend produced this artifact; a local result under a cloud route is a fallback."""
    for key in ("provider", "route_provider", "local_fallback"):
        meta.pop(key, None)
    if provider is not None:
        meta["provider"] = provider
    elif route_provider is not None:
        meta.update(route_provider=route_provider, local_fallback=True)
    meta.update(model=provenance["model"], provenance=provenance)


def _sources_intact(sources):
    return all(context_plan.source_usable(source) or source.get("body_state") == "deleted"
               or source.get("body_state") in (None, "full") and source.get("body_length") == 0
               for source in sources)


def _cloud_input_binding(db, pid, snapshot):
    """Bind paid-response adoption to original occurrences as well as the input contract.

    This is the overview's binding: its input is every source of the patient. A scan binds to
    its own source instead (:func:`_cloud_scan_binding`)."""
    pairs = [(pid, source['message_id']) for source in snapshot['sources'] if source['source'] == 'chat']
    bindings = []
    for offset in range(0, len(pairs), 64):
        bindings.extend(dependency_loss.source_bindings(db, pairs[offset:offset + 64]))
    for source in snapshot['sources']:
        if isinstance(context_plan.source_key(source), str):
            bindings.append(dependency_loss.source_binding(db, pid, 0, source_key=context_plan.source_key(source)))
    if any(not valid_hash(binding) for binding in bindings):
        raise ValueError('patient_context_cloud_source_changed')
    # Signed without the summary contract: a summary-only code change keeps this binding.
    return _digest([snapshot['scan_source_sig'], snapshot['contract'], snapshot['route']['provider'], bindings,
                    [(context_plan.source_key(s), s.get('first_seen')) for s in snapshot['sources']]])


def _cloud_scan_binding(source_sig, contract, provider, occurrence):
    """Bind one source's paid scan answers to that source's own input.

    A scan asks the cloud about one source, so the patient's other sources are no part of what
    its answer depends on: the source signature, the scan contract, the provider and the
    original occurrence are. The chunk span needs no place here: every scan request carries its
    own span, and an answer is taken up only for the exact request."""
    return _digest(["scan", source_sig, contract, provider, occurrence])


route_accepts = context_artifacts.route_accepts


def _quality_assessment_code():
    """Read the quality-checking code identity without probing provider credentials."""
    return quality_identity.patient_quality_assessment_code()


def _quality_assessment(scfg):
    """Bind quality-checking code and policy without spending or publication toggles."""
    import semantic_policy
    return {"code": _quality_assessment_code(), "policy": semantic_policy.policy_fingerprint(scfg)}


def _source_quality_hold_key(db, pid, cfg, source, chunks_sig):
    binding = dependency_loss.source_binding(db, pid, source["message_id"],
        **({"source_key": context_plan.source_key(source)} if isinstance(context_plan.source_key(source), str) else {}))
    scope = context_queries.patient_context_scope(db, pid)
    return _digest([cfg, _quality_assessment_code(), scope, binding, source.get("first_seen"),
                    context_plan.source_signature(source), chunks_sig, context_artifacts.SOURCE_REPAIR_RULE])


def _comparison_context(value, sources, *, facts=None, prefix_ids=()):
    """Compare validated source facts, resolving summary selections by their IDs."""
    import extract_llm
    entries = value.get("facts", []) if isinstance(value, dict) else [
        fact for sentence in value for key in sentence.get("fact_ids", [])
        for fact in (facts or []) if fact.get("id") == key]
    by_source = {(source.get("source"), source["project_id"], source["message_id"], source["content_hash"]): source
                 for source in sources}
    result = set()
    for fact in entries:
        if fact.get("id") in prefix_ids:
            continue
        source = by_source.get(tuple(fact.get(key) for key in ("source", "project_id", "message_id", "content_hash")))
        if source is None:
            raise ValueError("cloud_comparison_source_invalid")
        assertion = {key: fact.get(key) for key in ("category", "text", "reported_subject", "subject",
                                                  "temporality", "negated", "status", "details")}
        result.add(extract_llm._comparison_fact(source, "patient_context", assertion, fact.get("evidence"),
                                               evidence_span=fact.get("evidence_span")))
    return result


def _scan_sig(db, pid, *, source_keys=None) -> str:
    """Digest of the patient's per-source scan rows, read at the caller's observation point."""
    selected = None if source_keys is None else set(source_keys)
    keys = set()

    def rows():
        # One decode per row; rows stream into the digest instead of being held (digest input is unchanged).
        for row in db.execute(
                "SELECT message_id,content,meta FROM artifacts WHERE kind IN (?,?) AND project_id=? AND "
                + current_meta_pred("meta", target=CURRENT_VERSION) + " ORDER BY artifact_id", (SCAN, CHUNK, pid)):
            row = tuple(row)
            if selected is not None:
                meta = loads_dict(row[2])
                if meta is not None and meta.get('source_key', row[0]) not in selected:
                    continue
            ref = (loads_dict(row[1]) or {}).get("accepted_candidate_ref")
            if isinstance(ref, dict) and valid_hash(ref.get("repair_input_key")):
                keys.add(ref["repair_input_key"])
            yield row
        for key in sorted(keys):
            for row in db.execute("SELECT message_id,content,meta FROM artifacts WHERE kind=? "
                    "AND project_id=? AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.repair_input_key') END=? "
                    "ORDER BY artifact_id", (SOURCE_REPAIR_CANDIDATE, pid, key)):
                yield tuple(row)
    return _digest_iter(rows())


def _out_of_scope(snapshot) -> bool:
    """No readable scope, or MCS explicitly refused this patient: not context-refresh work.
    A fetch failure of another kind stays in scope (not counted as nothing to do)."""
    return not snapshot["scope"] or snapshot["scope"].get("fetch_reason") in (
        "forbidden", "permission_denied", "access_revoked")
