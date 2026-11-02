"""Recheck semantic send eligibility and current audited summaries at each delivery boundary."""
import semantic_policy
import semantic_store
import semantic_transport
import json
import sqlite3
from mcs_util import payload_hash, positive, strict_json, valid_hash





class DeferredSend(Exception):
    """Not deliverable under CURRENT policy (e.g. semantic mode left
    enforce) — the committed intent stays pending for a later flush;
    it is not destroyed (INV-23) and never sent past its gate."""


class StaleSend(Exception):
    """The source generation this intent was built for has moved on —
    suppress terminally instead of publishing stale results (§18.4)."""


class FreezeSend(Exception):
    """A semantic intent changed after a chunk was accepted — preserve its
    receipt/progress and quarantine the remaining chunks."""


def _stale(reason: str, in_progress: bool):
    if in_progress:
        raise FreezeSend(reason)
    raise StaleSend(reason)



def intake_root_held(db, project_id, root):
    """Hold semantic presentation if the root's intake cannot be verified."""
    try:
        import semantic_intake_hold
        semantic_intake_hold.validate_indexes(db)
        return semantic_intake_hold.has_held_root(db, project_id, root)
    except sqlite3.Error:
        return True


def _latest_receipt(db, kind, project_id, message_id):
    try:
        row = db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? "
                         "AND message_id=? ORDER BY artifact_id DESC LIMIT 1",
                         (kind, project_id, message_id)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        content, meta = strict_json(row["content"]), strict_json(row["meta"])
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return None
    if not isinstance(content, dict) or not isinstance(meta, dict):
        return None
    return {"artifact_id": row["artifact_id"], "content": content, "meta": meta}


def _receipt_not_invalidated(meta):
    return all(value is None or value is False or type(value) is int and value == 0
               for value in (meta.get("invalidated"), meta.get("stale")))


def _notice_receipt_current(db, project_id, message_id, payload):
    from inference_version import is_current
    receipt_id = payload.get("notice_receipt_id")
    if type(receipt_id) is not int or receipt_id <= 0:
        return False
    try:
        row = db.execute("SELECT kind,project_id,message_id,content,meta FROM artifacts WHERE artifact_id=?",
                         (receipt_id,)).fetchone()
        if (row is None or row["kind"] != "semantic_notice_receipt"
                or row["project_id"] != project_id or row["message_id"] != message_id):
            return False
        content, meta = strict_json(row["content"]), strict_json(row["meta"])
        if (not isinstance(content, dict) or not isinstance(meta, dict)
                or not is_current(meta) or not _receipt_not_invalidated(meta)
                or meta.get("fingerprint") != payload.get("fingerprint")
                or meta.get("policy_fingerprint") != payload.get("policy_fingerprint")):
            return False
        integers = ("root_id", "target_message_id", "src_event_id", "summary_artifact_id")
        strings = ("fingerprint", "policy_fingerprint", "summary_hash", "notice_text_hash", "delivery_key")
        if set(content) != set(integers + strings):
            return False
        if any(type(content.get(key)) is not int or content[key] <= 0
               or type(payload.get(key)) is not int or payload[key] != content[key] for key in integers):
            return False
        if any(not valid_hash(content.get(key)) or content[key] != payload.get(key) for key in strings):
            return False
        return content["delivery_key"] == payload_hash({
            "kind": "semantic_notice", "root": content["root_id"], "fp": content["fingerprint"],
            "mid": content["target_message_id"], "policy": content["policy_fingerprint"]})
    except (sqlite3.Error, ValueError, TypeError, UnicodeError, KeyError, RecursionError, OverflowError):
        return False


def _degraded_receipt_current(db, project_id, payload):
    from inference_version import is_current
    receipt_id, root = payload.get("degraded_receipt_id"), payload.get("root_id")
    if not positive(receipt_id) or not positive(root):
        return False
    try:
        row = db.execute("SELECT kind,project_id,message_id,content,meta FROM artifacts WHERE artifact_id=?",
                         (receipt_id,)).fetchone()
        if (row is None or row["kind"] != "semantic_degraded_notice_receipt"
                or row["project_id"] != project_id or row["message_id"] != root):
            return False
        content, meta = strict_json(row["content"]), strict_json(row["meta"])
        keys = ("delivery_key", "root_id", "src_event_id", "fingerprint", "policy_fingerprint",
                "target_message_ids", "notice_text_hash", "policy_version")
        if (not isinstance(content, dict) or set(content) != set(keys) or not isinstance(meta, dict)
                or not is_current(meta) or not _receipt_not_invalidated(meta)
                or payload.get("degraded") is not True
                or any(content[k] != payload.get(k) for k in keys)
                or meta.get("fingerprint") != content["fingerprint"]
                or meta.get("policy_fingerprint") != content["policy_fingerprint"]):
            return False
        targets = content["target_message_ids"]
        if (not positive(content["root_id"]) or not positive(content["src_event_id"])
                or not positive(payload.get("src_event_id"))
                or not isinstance(targets, list) or not targets
                or any(not positive(mid) for mid in targets)
                or not isinstance(payload.get("target_message_ids"), list)
                or any(type(mid) is not int for mid in payload["target_message_ids"])
                or len(set(targets)) != len(targets)
                or any(not valid_hash(content[k]) for k in
                       ("fingerprint", "policy_fingerprint", "notice_text_hash", "delivery_key"))
                or not isinstance(content["policy_version"], str)
                or not isinstance(payload.get("text"), str) or not payload["text"]):
            return False
        return (content["notice_text_hash"] == payload_hash(payload["text"])
                and content["delivery_key"] == payload_hash({"kind": "semantic_notice",
                    "root": root, "fp": content["fingerprint"], "degraded": 1}))
    except (sqlite3.Error, ValueError, TypeError, UnicodeError, KeyError, RecursionError, OverflowError):
        return False


def _paired_summary(db, project_id, message_id, fingerprint, policy, revision, mode, status):
    from inference_version import is_current
    summary = _latest_receipt(db, semantic_policy.KIND_SUMMARY, project_id, message_id)
    audit = _latest_receipt(db, semantic_policy.KIND_AUDIT, project_id, message_id)
    if summary is None or audit is None:
        return None
    for receipt in (summary, audit):
        meta = receipt["meta"]
        if (not is_current(meta) or not _receipt_not_invalidated(meta)
                or meta.get("schema") != semantic_policy.SCHEMA_VERSION
                or meta.get("fingerprint") != fingerprint or meta.get("policy_fingerprint") != policy
                or meta.get("target_revision") != revision or meta.get("publication_mode") != mode
                or meta.get("audit_status") != status):
            return None
    content, verdict, meta = summary["content"], audit["content"], audit["meta"]
    findings = verdict.get("findings")
    if (content.get("audit_status") != status or verdict.get("status") != status
            or type(verdict.get("target_message_id")) is not int or verdict["target_message_id"] != message_id
            or not isinstance(content.get("claims"), list) or not content["claims"]
            or not isinstance(findings, list)
            or any(not isinstance(item, dict) or not isinstance(item.get("code"), str) for item in findings)
            or type(meta.get("summary_artifact_id")) is not int
            or meta["summary_artifact_id"] != summary["artifact_id"]
            or meta.get("summary_hash") != payload_hash(content)
            or meta.get("audit_content_hash") != payload_hash(verdict)
            or not valid_hash(meta.get("claim_input_binding"))):
        return None
    blocked = {"input_oversize", "input_incomplete", "support_unevaluated", "evidence_missing",
               "evidence_revision_mismatch", "evidence_span_mismatch", "source_fact_coverage_missing",
               "source_fact_coverage_ambiguous", "source_fact_coverage_low_confidence", "fact_dropped",
               "claim_not_supported", "quantity_untraced", "claim_without_evidence", "claim_contradicts",
               "claim_ambiguous", "claim_low_confidence"}
    if status == "PASS" and any(item["code"] in blocked or item["code"].startswith(
            ("claim_quantity_", "medication_detail_")) for item in findings):
        return None
    return summary, audit


def _resolve_fact_source(db, project_id, message_id, fingerprint, policy, fact_source):
    from inference_version import is_current
    if fact_source is None:
        # Canonical fact audits carry the interpretation policy; shadow fact
        # documents alone do not make a legacy summary canonical.
        row = db.execute("SELECT meta FROM artifacts WHERE kind=? AND project_id=? AND message_id=? "
                         "ORDER BY artifact_id DESC LIMIT 1",
                         (semantic_policy.KIND_FACT_AUDIT, project_id, message_id)).fetchone()
        if row is not None:
            try:
                meta = strict_json(row["meta"])
            except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
                return None
            if not isinstance(meta, dict):
                return None
            if meta.get("fingerprint") == fingerprint and meta.get("policy_fingerprint") == policy:
                if not is_current(meta) or not _receipt_not_invalidated(meta):
                    return None
                fact_source = "canonical"
        if fact_source is None:
            fact_source = "legacy"
    if fact_source not in ("legacy", "shadow", "canonical"):
        return None
    return fact_source


def _binding_facts(db, project_id, message_id, fingerprint, policy, member, fact_source):
    from inference_version import is_current
    fact_source = _resolve_fact_source(db, project_id, message_id, fingerprint, policy, fact_source)
    if fact_source is None:
        return None
    kind = semantic_policy.KIND_FACTS_V2 if fact_source == "canonical" else semantic_policy.KIND_FACTS
    receipt = _latest_receipt(db, kind, project_id, message_id)
    if receipt is None:
        return None
    meta, content = receipt["meta"], receipt["content"]
    if (not is_current(meta) or not _receipt_not_invalidated(meta)
            or meta.get("fingerprint") != fingerprint
            or meta.get("schema") != semantic_policy.SCHEMA_VERSION):
        return None
    if fact_source != "canonical":
        facts = content.get("facts")
        return facts if isinstance(facts, list) else None
    import semantic_facts
    import semantic_projection
    cached, _, _ = semantic_facts.cached_facts_plan(receipt, member, message_id, fingerprint, "canonical")
    return semantic_projection.project_v2_facts(cached["content"]) if cached is not None else None


def _canonical_chain_current(db, project_id, message_id, fingerprint, policy, member, status):
    from inference_version import is_current
    import semantic_audit
    import semantic_facts
    import semantic_projection
    import semantic_publication
    from inference_dependency_loss import source_binding

    facts = _latest_receipt(db, semantic_policy.KIND_FACTS_V2, project_id, message_id)
    audit = _latest_receipt(db, semantic_policy.KIND_FACT_AUDIT, project_id, message_id)
    projection = _latest_receipt(db, semantic_publication.KIND_V4_SKIPPED if status == "SKIPPED"
                                 else semantic_publication.KIND_V4, project_id, message_id)
    if not all((facts, audit, projection)):
        return False
    for item in (facts, audit, projection):
        meta = item["meta"]
        if (not is_current(meta) or not _receipt_not_invalidated(meta)
                or meta.get("fingerprint") != fingerprint):
            return False
    if audit["meta"].get("policy_fingerprint") != policy or projection["meta"].get("policy_fingerprint") != policy:
        return False
    cached, _, _ = semantic_facts.cached_facts_plan(facts, member, message_id, fingerprint, "canonical")
    if cached is None:
        return False
    doc = cached["content"]
    if status == "SKIPPED":
        return semantic_publication.current_skipped(db, project_id, message_id, fingerprint, policy, member) is not None
    if status != "PASS":
        return False
    doc_hash = semantic_facts.doc_hash(doc)
    meta = projection["meta"]
    binding = source_binding(db, project_id, message_id)
    return (audit["meta"].get("schema") == semantic_policy.SCHEMA_VERSION
            and audit["meta"].get("audit_status") == "PASS"
            and semantic_audit.fact_audit_verdict(audit, doc_hash) == "PASS"
            and audit["content"].get("findings") == []
            and doc.get("coverage", {}).get("status") == "complete"
            and not semantic_audit.published_quantity_findings(doc)
            and meta.get("schema") == 2 and type(meta.get("engine_version")) is int
            and meta["engine_version"] == semantic_publication.ENGINE_VERSION
            and type(meta.get("projection_version")) is int
            and meta["projection_version"] == semantic_projection.PROJECTION_VERSION
            and meta.get("hash") == member.get("revision") and meta.get("doc_hash") == doc_hash
            and isinstance(binding, str) and bool(binding) and meta.get("source_binding") == binding
            and projection["content"] == semantic_projection.project_v2_doc_legacy(doc))


def _claim_binding_current(db, project_id, message_id, fingerprint, policy, revision,
                           summary, audit, member, fact_source):
    try:
        facts = _binding_facts(db, project_id, message_id, fingerprint, policy, member, fact_source)
        if facts is None:
            return False
        binding = payload_hash({"project_id": project_id, "message_id": message_id,
                                "fingerprint": fingerprint, "policy": policy, "revision": revision,
                                "claims": summary["content"].get("claims"), "facts": facts})
        return binding == audit["meta"].get("claim_input_binding")
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return False


def skipped_summary_current(db, project_id, message_id, fingerprint, policy, revision, mode):
    """Validate a skipped summary against its current, separately stored receipt."""
    try:
        pair = _paired_summary(db, project_id, message_id, fingerprint, policy, revision, mode, "SKIPPED")
    except (sqlite3.Error, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return False
    if pair is None:
        return False
    summary_receipt, audit_receipt = pair
    summary, smeta = summary_receipt["content"], summary_receipt["meta"]
    audit, ameta = audit_receipt["content"], audit_receipt["meta"]
    reasons = {"not_configured", "disabled", "daily_budget", "backlog_reserved", "service_unavailable", "circuit_open"}
    findings = audit.get("findings")
    return (all(meta.get("jev_evaluated") is False and meta.get("code_checks") == "passed"
                for meta in (smeta, ameta))
            and smeta.get("target_revision") == revision and summary.get("audit_status") == "SKIPPED"
            and audit.get("status") == "SKIPPED" and audit.get("target_message_id") == message_id
            and isinstance(findings, list) and bool(findings)
            and all(isinstance(item, dict) and item.get("code") == "support_unevaluated" for item in findings)
            and smeta.get("jev_skip_reason") in reasons
            and smeta.get("jev_skip_reason") == ameta.get("jev_skip_reason")
            and smeta.get("summary_hash") == ameta.get("summary_hash") == payload_hash(summary))


def _verified_pair(db, project_id, message_id, fingerprint, policy, revision, mode, status,
                   bundle, fact_source):
    if not isinstance(bundle, dict) or bundle.get("source_fingerprint") != fingerprint:
        return None
    members = bundle.get("members")
    if not isinstance(members, list):
        return None
    target = next((m for m in members if isinstance(m, dict) and m.get("message_id") == message_id), None)
    if target is None or target.get("revision") != revision:
        return None
    try:
        resolved_source = _resolve_fact_source(db, project_id, message_id, fingerprint, policy, fact_source)
        if resolved_source is None:
            return None
        pair = _paired_summary(db, project_id, message_id, fingerprint, policy, revision, mode, status)
        if (pair is None or status == "SKIPPED" and not skipped_summary_current(
                db, project_id, message_id, fingerprint, policy, revision, mode)
                or not _claim_binding_current(db, project_id, message_id, fingerprint, policy,
                                              revision, *pair, target, resolved_source)
                or resolved_source == "canonical" and not _canonical_chain_current(
                    db, project_id, message_id, fingerprint, policy, target, status)):
            return None
        return pair
    except (sqlite3.Error, ValueError, TypeError, UnicodeError, KeyError, RecursionError, OverflowError):
        return None


def passed_summary_current(db, project_id, message_id, fingerprint, policy, revision, mode, *,
                           bundle=None, fact_source=None):
    if mode not in ("shadow", "assist", "enforce"):
        return False
    if bundle is None:
        try:
            row = db.execute("SELECT COALESCE(parent_id,message_id) FROM messages "
                             "WHERE project_id=? AND message_id=?", (project_id, message_id)).fetchone()
            if row is None:
                return False
            bundle = semantic_store.thread_bundle(db, project_id, row[0], [message_id])
        except (sqlite3.Error, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            return False
    return _verified_pair(db, project_id, message_id, fingerprint, policy, revision, mode, "PASS",
                          bundle, fact_source) is not None


def verified_skipped_summary_current(db, project_id, message_id, fingerprint, policy, revision, mode, *,
                                     bundle=None, fact_source=None):
    if mode not in ("shadow", "assist", "enforce"):
        return False
    if bundle is None:
        try:
            row = db.execute("SELECT COALESCE(parent_id,message_id) FROM messages "
                             "WHERE project_id=? AND message_id=?", (project_id, message_id)).fetchone()
            if row is None:
                return False
            bundle = semantic_store.thread_bundle(db, project_id, row[0], [message_id])
        except (sqlite3.Error, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            return False
    return _verified_pair(db, project_id, message_id, fingerprint, policy, revision, mode, "SKIPPED",
                          bundle, fact_source) is not None


def current_summary(db, project_id: int, message_id: int,
                     bundle, policy: str, *, fact_source=None):
    """The newest semantic_summary for this message when it is publishable
    on the CURRENT generation (PASS, not stale, enforce, same policy,
    whole-thread fingerprint and target revision), else None. The single
    predicate shared by the send gate, render state and summary block.
    Returns ``{artifact_id, content, meta}``. ``bundle`` may be a dict or
    a zero-arg callable resolved only after the cheap row/meta checks pass
    (thread_bundle reads the whole thread); a raising callable -> None."""
    row = _latest_receipt(db, semantic_policy.KIND_SUMMARY, project_id, message_id)
    if row is None:
        return None
    meta = row["meta"]
    from inference_version import is_current
    if (meta.get("audit_status") not in ("PASS", "SKIPPED")
            or not is_current(meta) or not _receipt_not_invalidated(meta)
            or meta.get("publication_mode") != "enforce"
            or meta.get("policy_fingerprint") != policy):
        return None
    if callable(bundle):
        try:
            bundle = bundle()
        except Exception:
            return None
    if not isinstance(bundle, dict):
        return None
    members = bundle.get("members")
    if not isinstance(members, list):
        return None
    target = next((m for m in members
                   if isinstance(m, dict) and m.get("message_id") == message_id), None)
    if (meta.get("fingerprint") != bundle.get("source_fingerprint")
            or target is None or meta.get("target_revision") != target.get("revision")):
        return None
    pair = _verified_pair(db, project_id, message_id, bundle["source_fingerprint"], policy,
                          target["revision"], "enforce", meta["audit_status"], bundle, fact_source)
    if pair is None or pair[0]["artifact_id"] != row["artifact_id"]:
        return None
    return row


def enforce_block_reason(db, scfg: dict, project_id) -> str | None:
    """The reason semantic content must not ride this send, or None.

    Order is mode, project_id type, pause, scope: a malformed id never
    reaches ``paused``. Each caller maps the reason to its own outcome."""
    if scfg["mode"] != "enforce" or scfg["summary_mode"] != "enforce":
        return "semantic_not_enforce"
    if type(project_id) is not int or project_id <= 0:
        return "payload_invalid"
    from mcs_queries import paused
    if paused(db, project_id):
        return "semantic_paused"
    if scfg["project_ids"] is not None and project_id not in scfg["project_ids"]:
        return "project_out_of_scope"
    return None


class _SourceUnverified(Exception):
    """The source cannot be read within the safe admission bounds."""


def _degraded_source_gate(row, in_progress):
    if row["state"] == "accepted":
        _stale("base_delivered", in_progress)
    attempts = row["attempts"]
    if type(attempts) is not int or attempts < 0 or row["state"] not in ("pending", "failed"):
        raise FreezeSend("src_event_unverified") if in_progress else DeferredSend("src_event_unverified")
    try:
        raw = row["progress"]
        progress = {} if raw is None or raw == "" or raw == b"" else strict_json(raw)
        if not isinstance(progress, dict) or "invalid_progress" in progress:
            raise ValueError
        sent, next_chunk = progress.get("sent", []), progress.get("next", 0)
        sending, fingerprint = progress.get("sending"), progress.get("fingerprint")
        if (type(next_chunk) is not int or next_chunk < 0 or not isinstance(sent, list)
                or len(sent) != next_chunk or any(not isinstance(item, str) or not item.isdigit() for item in sent)
                or (fingerprint is not None and not isinstance(fingerprint, str))
                or (sending is not None and type(sending) is not int)):
            raise ValueError
        if sent:
            _stale("base_delivered", in_progress)
        if sending is not None:
            raise ValueError
        # Legacy attempted rows with no complete pre-send journal cannot
        # establish that delivery never began.
        if attempts and (not {"next", "sent", "sending", "fingerprint"}.issubset(progress)
                         or not valid_hash(fingerprint)):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise FreezeSend("src_event_unverified") if in_progress else DeferredSend("src_event_unverified") from None


def _degraded_summaries_ready(db, project_id, bundle, targets, scfg):
    policy = semantic_policy.policy_fingerprint(scfg)
    return bool(targets) and all(current_summary(db, project_id, mid, bundle, policy,
                                                 fact_source=scfg.get("fact_source")) is not None
                                 for mid in targets)


def _source_event(db, project_id: int, event_id: int, *, read_progress=True):
    """Load a bounded origin event only inside the semantic notice's project."""
    import semantic_intake_hold
    try:
        if not semantic_intake_hold._rowid_primary_key(db, 'notify_outbox', 'event_id'):
            raise _SourceUnverified()
        row = db.execute(
            "SELECT state,attempts,typeof(payload) AS payload_type,typeof(progress) AS progress_type, "
            "CASE WHEN typeof(progress) IN ('integer','real') THEN progress END AS progress_scalar "
            "FROM main.notify_outbox WHERE event_id=? AND kind='new_messages' AND project_id=?",
            (event_id, project_id)).fetchone()
        if row is None:
            return None, None
        if row["state"] == "suppressed":
            return {"state": "suppressed"}, None
        def bounded(column, storage, limit):
            if storage not in ('text', 'blob'):
                return None
            with db.blobopen('notify_outbox', column, event_id, readonly=True) as blob:
                if len(blob) > limit:
                    raise _SourceUnverified()
                raw = blob.read()
            return raw.decode(semantic_intake_hold._codec(db)) if storage == 'text' else raw
        raw = bounded('payload', row['payload_type'], 1024 * 1024)
        progress = (bounded('progress', row['progress_type'], 65536)
                    if read_progress and row['progress_type'] in ('text', 'blob')
                    else row['progress_scalar'] if read_progress else None)
        source = strict_json(raw)
        if not isinstance(source, dict):
            raise _SourceUnverified()
        return {'state': row['state'], 'attempts': row['attempts'], 'progress': progress}, source
    except (sqlite3.Error, UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
        raise _SourceUnverified() from None



def marker_source_gate(db, project_id, payload, *, in_progress=False, cfg=None):
    """Recheck the exact arrival and target inside the send-marker transaction."""
    import semantic_intake_hold
    if payload.get('degraded') is True or 'degraded_receipt_id' in payload:
        if not _degraded_receipt_current(db, project_id, payload):
            raise FreezeSend('degraded_unverified') if in_progress else DeferredSend('degraded_unverified')
        if not isinstance(cfg, dict):
            raise FreezeSend('policy_unverified') if in_progress else DeferredSend('policy_unverified')
        scfg, _ = semantic_policy.runtime_config(cfg)
        reason = enforce_block_reason(db, scfg, project_id)
        if reason:
            raise FreezeSend(reason) if in_progress else DeferredSend(reason)
        if (payload['policy_version'] != semantic_policy.POLICY_VERSION
                or payload['policy_fingerprint'] != semantic_policy.policy_fingerprint(scfg)):
            _stale('policy_changed', in_progress)
        try:
            row, source = _source_event(db, project_id, payload['src_event_id'])
        except _SourceUnverified:
            raise FreezeSend('src_event_unverified') if in_progress else DeferredSend('src_event_unverified') from None
        if row is None or row['state'] == 'suppressed':
            _stale('src_event_ineligible', in_progress)
        ids = source.get('message_ids')
        if (not isinstance(ids, list) or any(type(mid) is not int or mid <= 0 for mid in ids)
                or len(set(ids)) != len(ids)):
            raise FreezeSend('src_event_unverified') if in_progress else DeferredSend('src_event_unverified')
        if not set(payload['target_message_ids']).issubset(ids):
            _stale('src_event_ineligible', in_progress)
        _degraded_source_gate(row, in_progress)
        if _has_initial_history(db, project_id, payload['target_message_ids']):
            _stale('initial_history', in_progress)
        bundle = semantic_store.thread_bundle(db, project_id, payload['root_id'])
        if (bundle is None or bundle['source_fingerprint'] != payload['fingerprint']
                or not set(payload['target_message_ids']).issubset(
                    {member['message_id'] for member in bundle['members']})):
            _stale('stale_generation', in_progress)
        if _degraded_summaries_ready(db, project_id, bundle, payload['target_message_ids'], scfg):
            _stale('summary_ready', in_progress)
        return
    src = payload.get('src_event_id')
    if type(src) is not int or src <= 0:
        raise ValueError('payload_invalid')
    row = db.execute(
        "SELECT state,attempts,typeof(progress) AS progress_type FROM main.notify_outbox "
        "WHERE event_id=? AND kind='new_messages' AND project_id=?", (src, project_id)).fetchone()
    if row is None or row['state'] == 'suppressed':
        _stale('src_event_ineligible', in_progress)
    try:
        ids = semantic_intake_hold._arrival_targets(db, project_id, src, strict=True)
    except (sqlite3.Error, ValueError, TypeError):
        raise FreezeSend('src_event_unverified') if in_progress else DeferredSend('src_event_unverified') from None
    degraded = payload.get('degraded', False)
    targets = payload.get('target_message_ids') if degraded else [payload.get('target_message_id')]
    if (not isinstance(targets, list) or not targets
            or any(type(mid) is not int or mid <= 0 for mid in targets)
            or not set(targets).issubset(ids)):
        _stale('src_event_ineligible', in_progress)

def initial_history_ids(db, project_id, ids):
    history = set()
    for offset in range(0, len(ids), 900):
        group = ids[offset:offset + 900]
        history.update(row[0] for row in db.execute(
                "SELECT m.message_id FROM messages m JOIN message_metadata d ON d.message_id=m.message_id "
                "AND d.source='capture' WHERE m.project_id=? AND m.message_id IN ("
                + ','.join('?' * len(group)) + ") AND CASE WHEN json_valid(d.content) "
                "THEN json_extract(d.content,'$._initial_import.role')='history' ELSE 0 END",
                (project_id, *group)))
    return history


def _has_initial_history(db, project_id, ids):
    return bool(initial_history_ids(db, project_id, ids))


def semantic_gate(db, ev, payload: dict, cfg: dict,
                  in_progress: bool = False):
    """Recheck a semantic intent immediately before every external post."""
    if not isinstance(payload, dict):
        raise ValueError("payload_invalid")
    scfg, _ = semantic_policy.runtime_config(cfg)
    project_id = ev["project_id"]
    reason = enforce_block_reason(db, scfg, project_id)
    if reason == "payload_invalid":
        raise ValueError(reason)
    if reason is not None:
        raise FreezeSend(reason) if in_progress else DeferredSend(reason)
    if payload.get("degraded") is True or "degraded_receipt_id" in payload:
        try:
            raw_payload = ev["payload"]
        except (KeyError, IndexError):
            raw_payload = None
        if raw_payload is not None:
            try:
                if not isinstance(strict_json(raw_payload), dict):
                    raise ValueError
            except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
                raise FreezeSend("degraded_unverified") if in_progress else DeferredSend("degraded_unverified") from None
        if not _degraded_receipt_current(db, project_id, payload):
            raise FreezeSend("degraded_unverified") if in_progress else DeferredSend("degraded_unverified")
    policy_version = payload.get("policy_version")
    if not isinstance(policy_version, str) \
            or policy_version != semantic_policy.POLICY_VERSION:
        _stale("policy_changed", in_progress)
    degraded = payload.get("degraded", False)
    if type(degraded) is not bool:
        raise ValueError("payload_invalid")
    policy = payload.get("policy_fingerprint")
    if not isinstance(policy, str) \
            or policy != semantic_policy.policy_fingerprint(scfg):
        _stale("policy_changed", in_progress)
    fp = payload.get("fingerprint")
    root = payload.get("root_id")
    src = payload.get("src_event_id")
    if not isinstance(fp, str) or not fp or type(root) is not int \
            or root <= 0 or type(src) is not int or src <= 0:
        raise ValueError("payload_invalid")
    if intake_root_held(db, project_id, root):
        raise FreezeSend('semantic_intake_held') if in_progress else DeferredSend('semantic_intake_held')
    try:
        source_row, source = _source_event(db, project_id, src, read_progress=degraded)
    except _SourceUnverified:
        raise FreezeSend('src_event_unverified') if in_progress else DeferredSend('src_event_unverified') from None
    if source_row is None or source_row["state"] == "suppressed":
        _stale("src_event_ineligible", in_progress)
    try:
        source_ids = source.get("message_ids")
        if (not isinstance(source_ids, list)
                or any(type(mid) is not int or mid <= 0 for mid in source_ids)
                or len(set(source_ids)) != len(source_ids)):
            raise ValueError
        source_ids = set(source_ids)
    except (AttributeError, ValueError):
        raise FreezeSend('src_event_unverified') if in_progress else DeferredSend('src_event_unverified') from None
    if degraded:
        target_ids = payload.get("target_message_ids")
        if (not isinstance(target_ids, list) or not target_ids
                or any(type(mid) is not int or mid <= 0 for mid in target_ids)
                or len(set(target_ids)) != len(target_ids)
                or not set(target_ids).issubset(source_ids)):
            raise ValueError("payload_invalid")
        if _has_initial_history(db, project_id, target_ids):
            _stale("initial_history", in_progress)
        _degraded_source_gate(source_row, in_progress)
        bundle = semantic_store.thread_bundle(db, project_id, root)
        if bundle is None or bundle["source_fingerprint"] != fp:
            _stale("stale_generation", in_progress)
        member_ids = {m["message_id"] for m in bundle["members"]}
        if not set(target_ids).issubset(member_ids):
            _stale("stale_generation", in_progress)
        if _degraded_summaries_ready(db, project_id, bundle, target_ids, scfg):
            _stale("summary_ready", in_progress)
    else:
        target = payload.get("target_message_id")
        target_revision = payload.get("target_revision")
        if (type(target) is not int or target <= 0
                or not isinstance(target_revision, str) or not target_revision):
            raise ValueError("payload_invalid")
        if target not in source_ids:
            _stale("src_event_ineligible", in_progress)
        if _has_initial_history(db, project_id, [target]):
            _stale("initial_history", in_progress)
        bundle = semantic_store.thread_bundle(db, project_id, root)
        if bundle is None or bundle["source_fingerprint"] != fp:
            _stale("stale_generation", in_progress)
        member = next((m for m in bundle["members"]
                       if m["message_id"] == target), None)
        if member is None or member["revision"] != target_revision:
            _stale("stale_generation", in_progress)
        current = current_summary(db, project_id, target, bundle, policy,
                                  fact_source=scfg.get("fact_source"))
        summary_id, summary_hash = payload.get("summary_artifact_id"), payload.get("summary_hash")
        notice_text, notice_hash = payload.get("text"), payload.get("notice_text_hash")
        try:
            bound = (current is not None and type(summary_id) is int and summary_id > 0
                     and summary_id == current["artifact_id"] and valid_hash(summary_hash)
                     and summary_hash == payload_hash(current["content"])
                     and isinstance(notice_text, str) and valid_hash(notice_hash)
                     and notice_hash == payload_hash(notice_text)
                     and _notice_receipt_current(db, project_id, target, payload))
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            bound = False
        if not bound:
            raise FreezeSend("summary_unverified") if in_progress else DeferredSend("summary_unverified")


def semantic_render_state(db, ev, cfg: dict) -> tuple:
    """Current identities of audited summaries attached to a raw notice."""
    scfg, _ = semantic_policy.runtime_config(cfg)
    project_id = ev["project_id"]
    if enforce_block_reason(db, scfg, project_id) is not None:
        return ()
    try:
        payload = json.loads(ev["payload"])
        ids = payload.get("message_ids") or []
    except Exception:
        return ()
    state = []
    local_model = semantic_transport.llm_model() if isinstance(ids, list) and ids else None
    for mid in ids if isinstance(ids, list) else []:
        if type(mid) is not int:
            continue
        row = db.execute(
            "SELECT project_id,message_id,parent_id,content_hash "
            "FROM messages WHERE project_id=? AND message_id=?",
            (project_id, mid)).fetchone()
        if row is None:
            continue
        root = row["parent_id"] or row["message_id"]
        if intake_root_held(db, project_id, root):
            continue
        cur = current_summary(
            db, project_id, mid,
            lambda r=root, m=mid: semantic_store.thread_bundle(
                db, project_id, r, [m], local_model=local_model),
            semantic_policy.policy_fingerprint(scfg))
        if cur is None:
            continue
        state.append((mid, cur["artifact_id"], cur["meta"]["fingerprint"]))
    return tuple(state)


def semantic_render_gate(db, ev, initial: tuple, cfg: dict,
                         in_progress: bool = False):
    """Freeze/defer a raw notice whose attached summaries left enforce
    or changed since ``initial`` was snapshotted."""
    if not initial:
        return
    scfg, _ = semantic_policy.runtime_config(cfg)
    # the cheap policy check runs before the thread_bundle-heavy snapshot
    reason = enforce_block_reason(db, scfg, ev["project_id"])
    if reason is not None:
        raise FreezeSend(reason) if in_progress else DeferredSend(reason)
    if semantic_render_state(db, ev, cfg) != initial:
        raise FreezeSend("semantic_generation_changed") if in_progress \
            else StaleSend("semantic_generation_changed")








   # never let a summary render break delivery
