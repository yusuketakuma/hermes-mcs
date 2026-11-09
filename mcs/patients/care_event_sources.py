"""Read bounded, source-bound care events from one caller-owned patient snapshot."""
import hashlib
import sqlite3
import care_event_timeline
import extract_llm
import inference_dependency_loss
import initial_import_state
from mcs_queries import FACT_KINDS_SQL, current_fact_pred
from mcs_util import html_to_text, loads_dict, payload_hash, valid_hash

_BODY_LIMIT = 20000
_HTML_LIMIT = 80000
_ARTIFACT_LIMIT = 1000000
_META_LIMIT = 1000000
_PER_SOURCE_LIMIT = 256
_MAX_MESSAGES = 200
_MAX_EVENTS = 400
_MAX_PREFLIGHT_ROWS = 5000
_MAX_PREFLIGHT_BYTES = 32 * 1024 * 1024


def _unsafe_fields(columns):
    """Check byte lengths and embedded NUL before JSON or Python decoding."""
    # CASE is deliberately sequential: never cast an oversize field just to
    # find NUL; octet_length does not stop at an embedded zero byte.
    return " OR ".join(f"CASE WHEN octet_length({field})>{limit} THEN 1 "
                       f"WHEN instr(CAST({field} AS BLOB),x'00')>0 THEN 1 ELSE 0 END"
                       for field, limit in columns)


def _resource_preflight(db, project_id, *, patient_only=False):
    """Conservatively bound the patient data the existing progress reader may touch."""
    scopes = (
        ("patients", "project_id=?", (("initial_import", _META_LIMIT),
          ("fetch_state", _HTML_LIMIT), ("fetch_reason", _HTML_LIMIT))),
        ("messages", "project_id=?", (("body_text", _BODY_LIMIT), ("body_html", _HTML_LIMIT),
          ("sender_name", _HTML_LIMIT), ("sender_type", _HTML_LIMIT), ("profession", _HTML_LIMIT),
          ("organization", _HTML_LIMIT), ("posted_at", _HTML_LIMIT), ("content_hash", _HTML_LIMIT))),
        ("fetch_jobs", "project_id=?", (("payload", _META_LIMIT),)),
        ("message_revisions", "message_id IN (SELECT message_id FROM messages WHERE project_id=?)",
         (("content_hash", _HTML_LIMIT),)),
        ("artifacts", "(project_id=? OR message_id IN (SELECT message_id FROM messages WHERE project_id=?))",
         (("meta", _META_LIMIT), ("content", _ARTIFACT_LIMIT), ("model", _HTML_LIMIT))),
    )
    for table, scope, columns in scopes:
        if (table == "patients") != patient_only:
            continue
        params = (project_id, project_id) if table == "artifacts" else (project_id,)
        count = db.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM {table} WHERE {scope} LIMIT ?)",
                           (*params, _MAX_PREFLIGHT_ROWS + 1)).fetchone()[0]
        if count > _MAX_PREFLIGHT_ROWS:
            return "row_limit"
        size_expr = " + ".join(f"coalesce(octet_length({field}),0)" for field, _ in columns)
        total = db.execute(f"SELECT coalesce(sum({size_expr}),0) FROM {table} WHERE {scope}", params).fetchone()[0]
        if total > _MAX_PREFLIGHT_BYTES:
            return "byte_limit"
        if db.execute(f"SELECT 1 FROM {table} WHERE {scope} AND ({_unsafe_fields(columns)}) LIMIT 1",
                      params).fetchone():
            return "oversize_or_nul"
    return "bounded"


_CANONICAL_KINDS = ("canonical_projection", "semantic_facts_v4")


def _canonical_events(db, message, projection_row, cfg):
    """Project audited canonical care events only under the current source and policy.

    Returns the care_event_timeline.project result, or None when any link of
    the evidence chain fails. Read-only: no model dispatch, writes or
    transaction ownership (rebuild_facts_v2_cached contract).
    """
    import inference_version
    import semantic_extraction
    import semantic_facts
    import semantic_policy
    import semantic_projection
    import semantic_publication
    import semantic_store
    import semantic_transport

    if not isinstance(cfg, dict) or not isinstance(cfg.get("semantic"), dict) \
            or "mode" not in cfg["semantic"]:
        return None
    policy, errors = semantic_policy.runtime_config(cfg)
    if errors or policy.get("mode") == "off" or policy.get("fact_source") != "canonical":
        return None
    meta = loads_dict(projection_row["meta"]) or {}
    mid, pid = message["message_id"], message["project_id"]
    binding = inference_dependency_loss.source_binding(db, pid, mid)
    if not binding or meta.get("source_binding") != binding:
        return None
    bundle = semantic_store.thread_bundle(db, pid, message.get("parent_id") or mid,
                                          local_model=semantic_transport.llm_model(cfg))
    if (bundle is None or bundle.get("source_fingerprint") != meta.get("fingerprint")
            or semantic_policy.policy_fingerprint(policy) != meta.get("policy_fingerprint")):
        return None
    member = next((item for item in bundle["members"] if item.get("message_id") == mid), None)
    raw, error = semantic_publication.reproject_doc(db, mid, meta)
    if member is None or error or raw is None:
        return None
    try:
        clean = semantic_facts.validate_facts_doc(raw)
    except (semantic_facts.ContractError, KeyError, TypeError, ValueError):
        return None
    body = message["body_text"]
    source = clean["source"]
    if (member.get("body_state") != "full" or member.get("body_original") != body
            or member.get("revision") != message.get("content_hash")
            or source.get("message_id") != str(mid) or source.get("revision") != member.get("revision")
            or source.get("content_hash") != hashlib.sha256(body.encode("utf-8")).hexdigest()
            or source.get("body_codepoints") != len(body) or source.get("content_quality") != "full"
            or source.get("source_fingerprint") != bundle["source_fingerprint"]
            or loads_dict(projection_row["content"]) != semantic_projection.project_v2_doc_legacy(raw)):
        return None
    for evidence in clean["evidence"]:
        start, end = evidence["start"], evidence["end"]
        if not 0 <= start < end <= len(body) or body[start:end] != evidence["quote"]:
            return None
    # Reproduce the current_artifact selection; the newest matching row is authoritative.
    canonical_id = None
    canonical_meta = None
    for candidate in db.execute("SELECT artifact_id,content,meta,project_id FROM artifacts WHERE kind=? "
                                "AND message_id=? ORDER BY artifact_id DESC", ("semantic_facts_v2", mid)):
        candidate_meta = loads_dict(candidate["meta"]) or {}
        if (not inference_version.is_current(candidate_meta) or candidate_meta.get("invalidated")
                or candidate_meta.get("fingerprint") != meta.get("fingerprint")):
            continue
        if candidate["project_id"] != pid or loads_dict(candidate["content"]) != raw:
            return None
        canonical_id, canonical_meta = candidate["artifact_id"], candidate_meta
        break
    if canonical_id is None or canonical_meta is None:
        return None
    # The stored document must re-assemble identically from cached chunks with
    # verified producers; a mismatch means the audit PASS does not cover it.
    rebuilt, rebuild_error = semantic_extraction.rebuild_facts_v2_cached(
        member, db, meta.get("fingerprint"), pid, raw, canonical_meta)
    if rebuild_error is not None or rebuilt is None \
            or payload_hash(rebuilt) != payload_hash(raw):
        return None
    return care_event_timeline.project(pid, message, projection_row["artifact_id"],
                                       loads_dict(projection_row["content"]))


def collect(db, project_id, *, cfg=None, limit=_MAX_MESSAGES, event_limit=_MAX_EVENTS):
    """Return references and coverage without writing or borrowing another snapshot.

    Only a completed, current source-bound local extraction is eligible. Any
    canonical artifact is conservatively held until its independent policy and
    evidence contract can be proved; a legacy extraction never replaces it.
    The returned records contain neither source bodies nor quotes.
    """
    if (type(project_id) is not int or project_id <= 0 or type(limit) is not int
            or not 1 <= limit <= _MAX_MESSAGES or type(event_limit) is not int
            or not 1 <= event_limit <= _MAX_EVENTS):
        raise ValueError("care_event_scope_invalid")
    counts = dict.fromkeys(("examined", "deleted", "incomplete", "invalid_source", "oversize",
                            "empty", "not_extracted", "not_recorded", "unverified_extraction",
                            "canonical_unconfirmed", "invalid_records", "unverified_records",
                            "date_unknown", "events"), 0)
    coverage = {**counts, "state": "snapshot_required", "source_complete": False,
                "fetch_state": "unknown", "history_complete": False, "attachments": None,
                "truncated": False, "event_truncated": False, "limit": limit,
                "event_limit": event_limit, "per_source_limit": _PER_SOURCE_LIMIT,
                "preflight_row_limit": _MAX_PREFLIGHT_ROWS, "preflight_byte_limit": _MAX_PREFLIGHT_BYTES,
                "resource_state": "unknown",
                "relations": "not_assessed"}
    result = {"project_id": project_id, "events": [], "coverage": coverage}
    if not db.in_transaction:
        return result
    required = {
        "patients": {"project_id", "fetch_state", "fetch_reason", "history_floor", "initial_import", "is_archived"},
        "messages": {"project_id", "message_id", "body_state", "body_html", "body_text", "content_hash",
                     "posted_at", "posted_at_ts", "parent_id", "sender_id", "sender_name", "sender_type",
                     "profession", "organization"},
        "artifacts": {"artifact_id", "project_id", "message_id", "kind", "content", "meta", "model"},
        "message_revisions": {"message_id", "seq", "content_hash"},
        "fetch_jobs": {"job_id", "project_id", "kind", "state", "payload"},
        "attachments": {"message_id"},
    }
    if any(not columns <= {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}
           for table, columns in required.items()):
        coverage["state"] = "schema_unknown"
        return result
    try:
        db.execute("SELECT octet_length('x')").fetchone()
    except sqlite3.OperationalError as error:
        if "no such function: octet_length" not in str(error):
            raise
        coverage["state"] = "resource_unconfirmed"
        coverage["resource_state"] = "byte_length_unavailable"
        return result
    coverage["resource_state"] = _resource_preflight(db, project_id, patient_only=True)
    if coverage["resource_state"] != "bounded":
        coverage["state"] = "resource_unconfirmed"
        return result
    patient = db.execute("SELECT fetch_state,fetch_reason,history_floor,is_archived FROM patients WHERE project_id=?",
                         (project_id,)).fetchone()
    if patient is None:
        coverage["state"] = "scope_unknown"
        return result
    fetch, reason, floor, archived = patient
    if archived != 0 or fetch == "forbidden" or reason in ("forbidden", "permission_denied", "access_revoked"):
        coverage["state"] = "scope_unavailable"
        return result
    coverage["resource_state"] = _resource_preflight(db, project_id)
    if coverage["resource_state"] != "bounded":
        coverage["state"] = "resource_unconfirmed"
        return result
    coverage["fetch_state"] = fetch if fetch in ("complete", "incomplete", "pending", "failed") else "unknown"
    try:
        coverage["history_complete"] = initial_import_state.acquisition_status(db, project_id)["complete"]
    except (ValueError, KeyError, TypeError):
        coverage["history_complete"] = False
    coverage["attachments"] = db.execute("SELECT COUNT(*) FROM attachments a JOIN messages m ON m.message_id=a.message_id "
                                         "WHERE m.project_id=?", (project_id,)).fetchone()[0]
    rows = db.execute("SELECT m.message_id,m.body_state,octet_length(m.body_text) AS body_length,"
                      "octet_length(m.body_html) AS html_length "
                      "FROM messages m WHERE m.project_id=? ORDER BY m.message_id DESC LIMIT ?",
                      (project_id, limit + 1)).fetchall()
    coverage["truncated"] = len(rows) > limit
    model = extract_llm.resolved_model(cfg) if isinstance(cfg, dict) else None
    coverage["state"] = "observed"
    for mid, state, body_length, html_length in rows[:limit]:
        coverage["examined"] += 1
        if state == "deleted":
            coverage["deleted"] += 1
            continue
        if state != "full":
            coverage["incomplete"] += 1
            continue
        if ((type(body_length) is int and body_length > _BODY_LIMIT)
                or (type(html_length) is int and html_length > _HTML_LIMIT)):
            coverage["oversize"] += 1
            continue
        source = db.execute("SELECT * FROM messages WHERE project_id=? AND message_id=?", (project_id, mid)).fetchone()
        if source is None:
            coverage["invalid_source"] += 1
            continue
        m = dict(source)
        body, html, source_hash = m["body_text"], m["body_html"], m["content_hash"]
        if (not isinstance(body, str) or not isinstance(html, str) or not valid_hash(source_hash)
                or hashlib.sha256(html.encode()).hexdigest() != source_hash or html_to_text(html) != body):
            coverage["invalid_source"] += 1
            continue
        if not body:
            coverage["empty"] += 1
            continue
        # A canonical row, current or stale, requires the audited evidence
        # chain; a good local extraction never replaces it.
        canonical_present = db.execute(
            "SELECT 1 FROM artifacts WHERE message_id=? AND kind IN "
            "('canonical_projection','semantic_facts_v4') LIMIT 1",
            (m["message_id"],)).fetchone() is not None
        artifact = db.execute(
            "SELECT a.artifact_id,a.kind,a.meta,a.model,octet_length(a.content) AS content_length,"
            "CASE WHEN octet_length(a.content)<=? THEN a.content END AS content FROM artifacts a "
            "JOIN messages m ON m.message_id=a.message_id "
            f"WHERE m.project_id=? AND m.message_id=? AND a.kind IN ({FACT_KINDS_SQL}) "
            + current_fact_pred("a", "m") + " ORDER BY a.artifact_id DESC LIMIT 1",
            (_ARTIFACT_LIMIT, project_id, m["message_id"])).fetchone()
        if artifact is None:
            coverage["canonical_unconfirmed" if canonical_present else "not_extracted"] += 1
            continue
        if canonical_present:
            if artifact["kind"] not in _CANONICAL_KINDS:
                coverage["canonical_unconfirmed"] += 1
                continue
        elif artifact["kind"] != "extract_llm":
            coverage["canonical_unconfirmed"] += 1
            continue
        if type(artifact["content_length"]) is int and artifact["content_length"] > _ARTIFACT_LIMIT:
            coverage["oversize"] += 1
            continue
        if canonical_present:
            projected = _canonical_events(db, m, artifact, cfg)
            if projected is None:
                coverage["canonical_unconfirmed"] += 1
                continue
        else:
            meta, document = loads_dict(artifact["meta"]), loads_dict(artifact["content"])
            binding = inference_dependency_loss.source_binding(db, project_id, m["message_id"])
            if (not isinstance(meta, dict) or not isinstance(document, dict) or not isinstance(model, str)
                    or not model or artifact["model"] != model or meta.get("actual_model") != model
                    or not isinstance(binding, str) or meta.get("source_binding") != binding
                    or extract_llm.progress_for_message(db, m, model=model).get("state") != "complete"):
                coverage["unverified_extraction"] += 1
                continue
            items = document.get("event_records")
            if isinstance(items, list) and len(items) > _PER_SOURCE_LIMIT:
                coverage["oversize"] += 1
                continue
            projected = care_event_timeline.project(project_id, m, artifact["artifact_id"], document)
        pc = projected["coverage"]
        if pc["state"] == "not_recorded":
            coverage["not_recorded"] += 1
            continue
        if pc["state"] not in ("complete", "incomplete"):
            coverage["invalid_records"] += 1
            continue
        coverage["invalid_records"] += pc["invalid"]
        coverage["unverified_records"] += pc["unverified"]
        coverage["date_unknown"] += pc["date_unknown_count"]
        remaining = event_limit - len(result["events"])
        result["events"].extend(projected["events"][:remaining])
        if len(projected["events"]) > remaining:
            coverage["event_truncated"] = True
            break
    coverage["events"] = len(result["events"])
    loss = inference_dependency_loss.read(db, project_id)
    gaps = ("deleted", "incomplete", "invalid_source", "oversize", "not_extracted", "not_recorded",
            "unverified_extraction", "canonical_unconfirmed", "invalid_records", "unverified_records")
    coverage["source_complete"] = bool(coverage["history_complete"] and coverage["attachments"] == 0
        and not coverage["truncated"] and not coverage["event_truncated"] and loss.get("complete") is True
        and all(not coverage[key] for key in gaps))
    return result
