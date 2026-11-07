"""Patient context contracts, artifact identity and shared execution dependencies."""
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import time as time
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
import mcs_queries as context_queries
import patient_context_projection as context_state



KIND = "patient_context_nightly_v1"


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


CALL_SECONDS = 300


_FAILURE_CODE = re.compile(r"nightly_[a-z_]{1,60}")


_CHECKS = hashlib.sha256(Path(context_plan.__file__).read_bytes()).hexdigest()


_STATE_CODE = hashlib.sha256(Path(context_state.__file__).read_bytes()).hexdigest()


_DEPENDENCY_CODE = hashlib.sha256(Path(dependency_loss.__file__).read_bytes()).hexdigest()


COMMIT_MARGIN = cloud_inference.COMMIT_MARGIN


_quality_kind = context_artifacts.quality_kind


def _quality_expected(identity, fid, selected):
    if identity.get("purpose", "summary") == "source":
        return identity["passing_choices"][fid]
    return "supports" if fid in selected else "omission_ok"


class Deferred(Exception):
    """Admission or remaining time is unavailable; no attempt is charged to the unit."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


_facts_sig = context_artifacts.facts_signature


def options(cfg):
    """Enabled by default; explicit opt-out and malformed settings stay closed."""
    ext = cfg.get("extract", {}) if isinstance(cfg, dict) else None
    opt = ext.get("patient_context_nightly", {}) if isinstance(ext, dict) else None
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
    row = db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? "
                     "AND message_id IS ? AND " + current_meta_pred("meta", target=CURRENT_VERSION)
                     + " ORDER BY artifact_id DESC LIMIT 1",
                     (kind, pid, mid)).fetchone()
    return (loads_dict(row[0]) or {}, loads_dict(row[1]) or {}) if row else ({}, {})


def _published_version(db, pid):
    """Newest context/progress row of this patient: the selection-time
    inputs are stale once it moves."""
    return db.execute("SELECT MAX(artifact_id) FROM artifacts WHERE project_id=? "
                      "AND kind IN (?,?) AND " + current_meta_pred("meta", target=CURRENT_VERSION),
                      (pid, KIND, PROGRESS)).fetchone()[0]


def _artifact_meta(kind, meta, mid):
    meta = {**meta, VERSION_KEY: CURRENT_VERSION}
    if kind in (SCAN, CHUNK):
        meta.setdefault("source_key", mid if mid is not None else 0)
    if kind == SCAN:
        meta['backend_slot'] = _digest([meta.get('contract'), meta.get('provider'),
                                        meta.get('source_binding')])
    return meta


def _replace(ledger, kind, pid, content, meta, mid=None, *, write_count=None):
    # Linked summaries have no messages row; their checkpoint is patient-scoped.
    meta = _artifact_meta(kind, meta, mid)
    mid = None if kind in (SCAN, CHUNK) and mid == 0 else mid
    # Keep previous inference generations and distinct original karte records.
    deleted = ledger.db.execute("DELETE FROM artifacts WHERE kind=? AND project_id=? AND message_id IS ? AND "
                      + current_meta_pred("meta", target=CURRENT_VERSION)
                      + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.source_key') END IS ?"
                      + (" AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.backend_slot') END=?" if kind == SCAN else "")
                      + (" AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.chunk_key') END=?" if kind == CHUNK else ""),
                      (kind, pid, mid, meta.get("source_key"), *([meta['backend_slot']] if kind == SCAN else []),
                       *([meta["chunk_key"]] if kind == CHUNK else [])))
    artifact_id = ledger.artifact_add_tx(kind, json.dumps(content, ensure_ascii=False),
                                         project_id=pid, message_id=mid, model=meta.get("model", ""), meta=meta)
    if write_count is not None:
        if deleted.rowcount < 0:
            raise ValueError("nightly_source_adoption_changed")
        write_count[0] += deleted.rowcount + 1
    return artifact_id


def _save_source_chunk_tx(ledger, pid, mid, source_key, chunk_span, facts, meta):
    """Replace one source unit and its dependency record within the borrowed writer transaction."""
    if not ledger.db.in_transaction:
        raise RuntimeError("patient_context_source_transaction_required")
    unit_key = _digest([meta["source_sig"], chunk_span, meta["contract"], meta.get("provider"), meta["source_occurrence"]])
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
        raise ValueError("nightly_source_adoption_epoch")
    expected = db.execute("SELECT json_set(?, '$.analysis_epoch', ?)", (row[1], value + 1)).fetchone()[0]
    return row[0], expected


def _contract(cfg, pid=None):
    import extract_llm
    import cloud_inference
    import cloud_policy
    import extract
    contract = {"version": CONTRACT, VERSION_KEY: CURRENT_VERSION, "chunks": PLAN_VERSION,
            "facts": 2, "context_checks": _CHECKS, "state_code": _STATE_CODE, "dependency_code": _DEPENDENCY_CODE,
            "rule": extract.RULE_VERSION, "llm": extract_llm.EXTRACT_VERSION,
            "context": extract_llm.PATIENT_CONTEXT_VERSION,
            "model": local_llm.resolve(cfg)[1]}
    if pid is not None:
        settings = cloud_inference.resolve_cloud_settings(cfg)
        if (settings["mode"] == "on"
                and cloud_policy.eligible(settings, "patient_context", pid)):
            contract["provider"] = cloud_inference.cloud_provider_contract(cfg, route="patient_context", purpose="patient_context")
    return contract


def _sources_intact(sources):
    return all(context_plan.source_usable(source) or source.get("body_state") == "deleted"
               or source.get("body_state") in (None, "full") and source.get("body_length") == 0
               for source in sources)


def _cloud_input_binding(db, pid, snapshot):
    """Bind paid-response adoption to original occurrences as well as the input contract."""
    pairs = [(pid, source['message_id']) for source in snapshot['sources'] if source['source'] == 'chat']
    bindings = []
    for offset in range(0, len(pairs), 64):
        bindings.extend(dependency_loss.source_bindings(db, pairs[offset:offset + 64]))
    for source in snapshot['sources']:
        if isinstance(context_plan.source_key(source), str):
            bindings.append(dependency_loss.source_binding(db, pid, 0, source_key=context_plan.source_key(source)))
    if any(not valid_hash(binding) for binding in bindings):
        raise ValueError('nightly_cloud_source_changed')
    return _digest([snapshot['source_sig'], snapshot['contract'], bindings,
                    [(context_plan.source_key(s), s.get('first_seen')) for s in snapshot['sources']]])


_route_contract = context_artifacts.route_contract


def _quality_code():
    """Read code identity without probing provider credentials."""
    import extract_llm
    import semantic_candidate
    return _digest([extract_llm._LOADED_SOURCE_DIGESTS, semantic_candidate.loaded_source_contract()])


def _quality_assessment(scfg):
    """Bind interpretation code and policy without spending or publication toggles."""
    import semantic_policy
    return {"code": _quality_code(), "policy": semantic_policy.policy_fingerprint(scfg)}


def _source_quality_hold_key(db, pid, cfg, source, chunks_sig):
    binding = dependency_loss.source_binding(db, pid, source["message_id"],
        **({"source_key": context_plan.source_key(source)} if isinstance(context_plan.source_key(source), str) else {}))
    scope = context_queries.patient_context_scope(db, pid)
    return _digest([cfg, _quality_code(), scope, binding, source.get("first_seen"),
                    context_plan.source_signature(source), chunks_sig])


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
    rows = [tuple(r) for r in db.execute(
        "SELECT message_id,content,meta FROM artifacts WHERE kind IN (?,?) AND project_id=? AND "
        + current_meta_pred("meta", target=CURRENT_VERSION) + " ORDER BY artifact_id",
        (SCAN, CHUNK, pid))]
    if source_keys is not None:
        selected = set(source_keys)
        rows = [row for row in rows if loads_dict(row[2]) is None
                or (loads_dict(row[2]) or {}).get('source_key', row[0]) in selected]
    keys = {(loads_dict(content) or {}).get("accepted_candidate_ref", {}).get("repair_input_key")
            for mid, content, meta in rows if isinstance((loads_dict(content) or {}).get("accepted_candidate_ref"), dict)
            and valid_hash((loads_dict(content) or {})["accepted_candidate_ref"].get("repair_input_key"))}
    for key in sorted(keys):
        rows.extend(tuple(row) for row in db.execute("SELECT message_id,content,meta FROM artifacts WHERE kind=? "
            "AND project_id=? AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.repair_input_key') END=? "
            "ORDER BY artifact_id", (SOURCE_REPAIR_CANDIDATE, pid, key)))
    return _digest(rows)


def _out_of_scope(snapshot) -> bool:
    """No readable scope, or MCS explicitly refused this patient: not context-refresh work.
    A fetch failure of another kind stays in scope (not counted as nothing to do)."""
    return not snapshot["scope"] or snapshot["scope"].get("fetch_reason") in (
        "forbidden", "permission_denied", "access_revoked")
