"""Patient context quality claims, original answers and publication receipts."""
import json
import math
import os
import patient_context_artifacts as context_artifacts
from mcs_util import loads_dict, strict_json, valid_hash
from inference_version import VERSION_KEY, current_meta_pred, read_version
import patient_context_plan as context_plan
import patient_context_refresh_state


def _quality_scope_allowed(db, pid, policy, sources=None):
    """Keep validated policy scope and canonical provenance separate from notification modes."""
    from mcs_queries import paused
    if paused(db, pid) or policy["project_ids"] is not None and pid not in policy["project_ids"]:
        return False
    if sources is None:
        return True
    canonical = policy["fact_source"] == "canonical"
    return all(bool(source.get("canonical_context")) == canonical
               for source in sources if context_plan.source_usable(source) and source["source"] == "chat")


def _has_prior_quality_failure(db, pid, *, purpose=None):
    kinds = ((patient_context_refresh_state.QUALITY_FAIL, patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_FAIL, {'purpose': 'source'})) if purpose is None
             else (patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_FAIL, {'purpose': purpose}),))
    return db.execute('SELECT 1 FROM artifacts WHERE project_id=? AND kind IN ('
                      + ','.join('?' for _ in kinds) + ') LIMIT 1', (pid, *kinds)).fetchone() is not None


def _profiled_summary_repair_exists(db, pid, input_key):
    for (raw,) in db.execute("SELECT meta FROM artifacts WHERE kind='cloud_usage_reserve' AND project_id=?", (pid,)):
        try:
            meta = strict_json(raw)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return True
        if not isinstance(meta, dict):
            return True
        owner = meta.get('owner')
        if (meta.get('route') == 'patient_context' and meta.get('unit_key') == 'summary_quality_repair'
                and isinstance(owner, dict) and owner.get('source_sig') == input_key
                and meta.get('profile') is not None):
            return True
    return False


def _quality_claim(ledger, pid, input_key, deadline, current):
    """Claim one patient's quality work; inputs are checked inside the writer transaction."""
    if ledger.db.in_transaction:
        raise RuntimeError("patient_summary_transaction_active")
    if not current():
        raise ValueError("patient_summary_quality_source_changed")
    fd = patient_context_refresh_state.acquire_run_lock()
    if fd is None:
        return None
    try:
        if patient_context_refresh_state.time.monotonic() >= deadline:
            return None
        ledger.db.execute("BEGIN IMMEDIATE")
        try:
            if not current():
                raise ValueError("patient_summary_quality_source_changed")
            now = patient_context_refresh_state.time.time()
            token = os.urandom(16).hex()
            lease = now + max(1.0, deadline - patient_context_refresh_state.time.monotonic()) + patient_context_refresh_state.CALL_SECONDS + patient_context_refresh_state.COMMIT_MARGIN
            payload = json.dumps({"token": token, "input_key": input_key}, separators=(",", ":"))
            row = ledger.db.execute("""
                INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,payload,state,next_try,created_at,updated_at)
                VALUES(?,?,0,NULL,?,'pending',?,?,?)
                ON CONFLICT(kind,project_id,message_id) DO UPDATE SET
                  payload=excluded.payload,state='pending',next_try=excluded.next_try,updated_at=excluded.updated_at
                WHERE fetch_jobs.next_try<=?
                """, (patient_context_refresh_state.QUALITY_CLAIM, pid, payload, lease, now, now, now))
            if row.rowcount == 1 and not current(1):
                raise ValueError("patient_summary_quality_source_changed")
            ledger.db.commit()
            return (token, lease) if row.rowcount == 1 else None
        except BaseException:
            ledger.db.rollback()
            raise
    finally:
        os.close(fd)


def _quality_owns(db, pid, input_key, claim):
    token, lease = claim
    row = db.execute("SELECT payload FROM fetch_jobs WHERE kind=? AND project_id=? "
                     "AND message_id=0 AND next_try=? AND next_try>?",
                     (patient_context_refresh_state.QUALITY_CLAIM, pid, lease, patient_context_refresh_state.time.time())).fetchone()
    return bool(row and loads_dict(row[0]) == {"token": token, "input_key": input_key})


def _quality_integrity(db, pid):
    """Reject malformed quality metadata instead of treating spent work as absent."""
    kinds = {patient_context_refresh_state.QUALITY: ("input_key", "audit_key", "fact_id"),
             patient_context_refresh_state.QUALITY_FAIL: ("input_key", "candidate_key", "failure_key", "audit_key"),
             patient_context_refresh_state.QUALITY_PASS: ("input_key", "candidate_key", "audit_key"),
             patient_context_refresh_state.QUALITY_SKIP: ("input_key", "candidate_key", "audit_key"),
             patient_context_refresh_state.QUALITY_RETRY: ("input_key", "audit_key", "page_key"),
             patient_context_refresh_state.QUALITY_REPAIR: ("input_key", "candidate_key"),
             patient_context_refresh_state.QUALITY_REPAIR_CANDIDATE: ("input_key", "candidate_key", "audit_key", "original_failure_key"),
             patient_context_refresh_state.SOURCE_REPAIR: ("repair_input_key", "source_sig", "source_binding", "original_audit_key", "original_failure_key",
                             "original_chunks_sig", "protected_sig", "original_receipts_sig"),
             patient_context_refresh_state.SOURCE_REPAIR_CANDIDATE: ("repair_input_key", "source_sig", "source_binding", "original_audit_key", "original_failure_key",
                                       "original_chunks_sig", "protected_sig", "original_receipts_sig", "unit_key", "request_fp")}
    kinds.update({patient_context_refresh_state._quality_kind(kind, {"purpose": "source"}): kinds[kind]
                  for kind in (patient_context_refresh_state.QUALITY, patient_context_refresh_state.QUALITY_FAIL, patient_context_refresh_state.QUALITY_PASS, patient_context_refresh_state.QUALITY_RETRY, patient_context_refresh_state.QUALITY_SKIP)})
    for row in db.execute("SELECT kind,CASE WHEN typeof(meta)='text' AND length(meta)<=32768 "
                          "THEN meta END FROM artifacts WHERE project_id=? AND kind IN ("
                          + ",".join("?" for _ in kinds) + ")",
                          (pid, *kinds)):
        try:
            meta = (strict_json(row[1]) if row[0] in (patient_context_refresh_state.QUALITY_SKIP,
                    patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_SKIP, {"purpose": "source"})) else loads_dict(row[1]))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError("patient_summary_quality_metadata_invalid") from None
        if (not isinstance(meta, dict) or VERSION_KEY not in meta or read_version(meta) is None
                or any(not valid_hash(meta.get(key)) for key in kinds[row[0]])):
            raise ValueError("patient_summary_quality_metadata_invalid")


def _quality_release(ledger, pid, input_key, claim):
    if ledger.db.in_transaction:
        return
    fd = patient_context_refresh_state.acquire_run_lock()
    if fd is None:
        return
    try:
        ledger.db.execute("BEGIN IMMEDIATE")
        try:
            if _quality_owns(ledger.db, pid, input_key, claim):
                ledger.db.execute("DELETE FROM fetch_jobs WHERE kind=? AND project_id=? AND message_id=0",
                                  (patient_context_refresh_state.QUALITY_CLAIM, pid))
            ledger.db.commit()
        except BaseException:
            ledger.db.rollback()
            raise
    finally:
        os.close(fd)


def _quality_answers(db, pid, audit_key, fact_ids, *, purpose="summary", identity=None):
    if purpose == "source":
        import semantic_runtime
        if identity is None or identity.get("audit_key") != audit_key:
            raise ValueError("patient_source_quality_identity_invalid")
        return semantic_runtime.patient_source_question_evidence(db, pid, identity, fact_ids)["answers"]
    return _stored_quality_answers(db, pid, audit_key, fact_ids, purpose=purpose)


def _stored_quality_answers(db, pid, audit_key, fact_ids, *, purpose="summary"):
    """Read at most one immutable judgement for each requested fact."""
    if len(fact_ids) > 12 or len(set(fact_ids)) != len(fact_ids):
        raise ValueError("patient_summary_quality_answers_invalid")
    answers = {}
    for fact_id in fact_ids:
        rows = db.execute("SELECT content FROM artifacts WHERE kind=? AND project_id=? AND "
                          + current_meta_pred("meta", target=patient_context_refresh_state.CURRENT_VERSION)
                          + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.audit_key') END=? "
                          "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.fact_id') END=?",
                          (patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY, {"purpose": purpose}), pid, audit_key, fact_id)).fetchall()
        if len(rows) > 1:
            raise ValueError("patient_summary_quality_answers_ambiguous")
        if rows:
            answer = loads_dict(rows[0][0])
            if (not isinstance(answer, dict) or set(answer) != {"choice", "confidence"}
                    or answer.get("choice") not in ({"supports", "contradicts", "not_supported", "ambiguous"}
                          | ({"complete", "missing"} if purpose == "source" else {"omission_ok", "important_missing"}))
                    or type(answer.get("confidence")) not in (int, float)
                    or not math.isfinite(answer["confidence"]) or not 0 <= answer["confidence"] <= 1):
                raise ValueError("patient_summary_quality_answers_invalid")
            answers[fact_id] = answer
    return answers


_quality_failed = context_artifacts.quality_failed


def _quality_skip_evidence(db, pid, identity, selected, threshold):
    """Reject unresolved or adverse existing answers before accepting a skip."""
    import semantic_runtime
    purpose = identity.get("purpose", "summary")
    if (_has_prior_quality_failure(db, pid, purpose='source' if purpose == 'source' else None)
            or _quality_failed(db, pid, identity)
            or semantic_runtime.patient_summary_request_problem(
                db, pid, identity, identity["fact_ids"], purpose=purpose)):
        return False
    for offset in range(0, len(identity["fact_ids"]), 12):
        ids = identity["fact_ids"][offset:offset + 12]
        answers = _quality_answers(db, pid, identity["audit_key"], ids, purpose=purpose, identity=identity)
        if any(answer["confidence"] < threshold
               or answer["choice"] != patient_context_refresh_state._quality_expected(identity, fid, selected)
               for fid, answer in answers.items()):
            return False
    return True


def _quality_skip_receipt(db, pid, identity, selected, threshold):
    """Read an identity-bound skip without turning it into an audit PASS."""
    rows = db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND "
                      + current_meta_pred("meta", target=patient_context_refresh_state.CURRENT_VERSION)
                      + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.audit_key') END=?",
                      (patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_SKIP, identity), pid, identity["audit_key"])).fetchall()
    if len(rows) != 1 or not _quality_skip_evidence(db, pid, identity, selected, threshold):
        return None
    try:
        content, meta = strict_json(rows[0][0]), strict_json(rows[0][1])
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
    reasons = {"not_configured", "disabled", "daily_budget", "backlog_reserved", "service_unavailable", "circuit_open"}
    if (not isinstance(content, dict) or set(content) != {"status", "reason", "fact_count", "purpose"}
            or content.get("status") != "SKIPPED" or content.get("reason") not in reasons
            or type(content.get("fact_count")) is not int or content["fact_count"] != len(identity["fact_ids"])
            or content.get("purpose") != identity.get("purpose", "summary")
            or not isinstance(meta, dict) or any(meta.get(key) != identity[key]
                for key in ("input_key", "candidate_key", "audit_key"))):
        return None
    return content


def _quality_mark_skipped(ledger, pid, identity, claim, selected, current, threshold, reason):
    """Persist a bounded skip only while the source, scope and owner remain current."""
    if not current():
        raise ValueError("patient_summary_quality_source_changed")
    version = ledger.db.execute("PRAGMA data_version").fetchone()[0]
    changes = ledger.db.total_changes
    ledger.db.execute("BEGIN")
    try:
        if not _quality_skip_evidence(ledger.db, pid, identity, selected, threshold):
            raise ValueError("patient_summary_quality_unresolved_request")
        prior = _quality_skip_receipt(ledger.db, pid, identity, selected, threshold)
    finally:
        ledger.db.rollback()
    if ledger.db.execute("PRAGMA data_version").fetchone()[0] != version or ledger.db.total_changes != changes:
        raise ValueError("patient_summary_quality_source_changed")
    fd = patient_context_refresh_state.acquire_run_lock()
    if fd is None:
        raise patient_context_refresh_state.Deferred("patient_summary_quality_lock")
    try:
        ledger.db.execute("BEGIN IMMEDIATE")
        try:
            if (not _quality_owns(ledger.db, pid, identity["input_key"], claim) or not current()
                    or ledger.db.execute("PRAGMA data_version").fetchone()[0] != version
                    or ledger.db.total_changes != changes):
                raise ValueError("patient_summary_quality_source_changed")
            writes = 0
            if prior is None:
                # A malformed or duplicate existing receipt is never replaced.
                exists = ledger.db.execute("SELECT 1 FROM artifacts WHERE kind=? AND project_id=? AND "
                    + current_meta_pred("meta", target=patient_context_refresh_state.CURRENT_VERSION)
                    + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.audit_key') END=? LIMIT 1",
                    (patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_SKIP, identity), pid, identity["audit_key"])).fetchone()
                if exists:
                    raise ValueError("patient_summary_quality_skip_invalid")
                ledger.artifact_add_tx(patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_SKIP, identity), json.dumps({
                    "status": "SKIPPED", "reason": reason, "fact_count": len(identity["fact_ids"]),
                    "purpose": identity.get("purpose", "summary")}, separators=(",", ":")),
                    project_id=pid, meta={VERSION_KEY: patient_context_refresh_state.CURRENT_VERSION, **{key: identity[key] for key in
                        ("input_key", "candidate_key", "audit_key")}})
                writes = 1
            if not _quality_skip_receipt(ledger.db, pid, identity, selected, threshold) or not current(writes):
                raise ValueError("patient_summary_quality_source_changed")
            ledger.db.commit()
        except BaseException:
            ledger.db.rollback()
            raise
    finally:
        os.close(fd)


def _quality_failure_allows_remainder(db, pid, identity):
    rows = db.execute("SELECT meta FROM artifacts WHERE kind=? AND project_id=? AND "
                      + current_meta_pred("meta", target=patient_context_refresh_state.CURRENT_VERSION)
                      + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.failure_key') END=?",
                      (patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_FAIL, identity), pid, identity["failure_key"])).fetchall()
    if not rows:
        return True
    if len(rows) != 1:
        return False
    meta = loads_dict(rows[0][0]) or {}
    return meta.get("audit_key") == identity["audit_key"]


def _quality_store_answers(ledger, pid, identity, claim, answers, current, threshold, selected_ids, *,
                           reservation=None, invalid_ids=None):
    """Persist a bounded callback before any next Jev request; refuse stale owners."""
    if ledger.db.in_transaction:
        raise RuntimeError("patient_summary_transaction_active")
    if (type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 1
            or not isinstance(answers, dict) or len(answers) > 12
            or any(key not in identity["fact_ids"] for key in answers)):
        raise ValueError("patient_summary_quality_answers_invalid")
    if invalid_ids is not None and (reservation is None or not isinstance(invalid_ids, set)
            or not invalid_ids <= set(identity["fact_ids"]) or invalid_ids & set(answers)):
        raise ValueError("patient_summary_quality_answers_invalid")
    if not current():
        raise ValueError("patient_summary_quality_source_changed")
    fd = patient_context_refresh_state.acquire_run_lock()
    if fd is None:
        raise patient_context_refresh_state.Deferred("patient_summary_quality_lock")
    try:
        ledger.db.execute("BEGIN IMMEDIATE")
        try:
            if not _quality_owns(ledger.db, pid, identity["input_key"], claim) or not current():
                raise ValueError("patient_summary_quality_source_changed")
            purpose = identity.get("purpose", "summary")
            reused = purpose == "source" and reservation is None
            old = (_quality_answers(ledger.db, pid, identity["audit_key"], list(answers),
                                    purpose=purpose, identity=identity) if reused else
                   _stored_quality_answers(ledger.db, pid, identity["audit_key"], list(answers), purpose=purpose))
            if reused and old != answers:
                raise ValueError("patient_source_quality_receipt_missing")
            bindings = {}
            if purpose == "source" and reservation is not None:
                import semantic_runtime
                bindings = semantic_runtime.patient_source_answer_binding(
                    ledger.db, pid, identity, reservation, list(answers))
            writes = 0
            for fact_id, answer in answers.items():
                if fact_id in old:
                    if old[fact_id] != answer:
                        raise ValueError("patient_summary_quality_answers_conflict")
                    continue
                ledger.artifact_add_tx(patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY, identity), json.dumps(answer, separators=(",", ":")), project_id=pid,
                    meta={VERSION_KEY: patient_context_refresh_state.CURRENT_VERSION, "input_key": identity["input_key"],
                          "audit_key": identity["audit_key"], "fact_id": fact_id,
                          **bindings.get(fact_id, {})})
                writes += 1
            if not reused and _stored_quality_answers(ledger.db, pid, identity["audit_key"],
                                                      list(answers), purpose=purpose) != answers:
                raise ValueError("patient_summary_quality_answers_unverified")
            if (any(answer["choice"] != patient_context_refresh_state._quality_expected(identity, fid, selected_ids)
                    or answer["confidence"] < threshold for fid, answer in answers.items())
                    and not _quality_failed(ledger.db, pid, identity)):
                ledger.artifact_add_tx(patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_FAIL, identity), "{}", project_id=pid,
                    meta={VERSION_KEY: patient_context_refresh_state.CURRENT_VERSION, "input_key": identity["input_key"],
                          "candidate_key": identity["candidate_key"], "failure_key": identity["failure_key"],
                          "audit_key": identity["audit_key"]})
                writes += 1
                if not _quality_failed(ledger.db, pid, identity):
                    raise ValueError("patient_summary_quality_failure_unverified")
                # Negative evidence also invalidates an earlier accepted skip.
                epoch = ledger.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='semantic_sched' "
                                          "AND project_id=0 AND message_id=0").fetchone()
                ledger.bump_inference_analysis_epoch_tx()
                writes += int(epoch is not None)
            if reservation is not None:
                import semantic_runtime
                writes += semantic_runtime.patient_summary_resolve_request_tx(
                    ledger, reservation, identity, answers, invalid_ids, project_id=pid, purpose=purpose)
            if purpose == "source" and _quality_answers(ledger.db, pid, identity["audit_key"],
                    list(answers), purpose=purpose, identity=identity) != answers:
                raise ValueError("patient_source_quality_receipt_missing")
            if not _quality_owns(ledger.db, pid, identity["input_key"], claim) or not current(writes):
                raise ValueError("patient_summary_quality_source_changed")
            ledger.db.commit()
            return True
        except BaseException:
            ledger.db.rollback()
            raise
    finally:
        os.close(fd)


def _quality_pass_content(db, pid, identity, *, threshold=None, descriptor=None):
    if identity.get("purpose", "summary") != "source":
        return {"fact_count": len(identity["fact_ids"]), "zero_questions": not identity["fact_ids"]}
    import semantic_runtime
    db.execute("SAVEPOINT source_pass_read")
    try:
        if descriptor is None:
            descriptor = semantic_runtime.patient_source_pass_descriptor(db, pid, identity["audit_key"])
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("policy"), dict):
            return None
        if threshold is not None and descriptor.get("policy", {}).get("match_threshold") != threshold:
            return None
        return semantic_runtime.patient_source_pass_content(db, pid, identity, descriptor)
    finally:
        db.execute("RELEASE source_pass_read")


def _quality_passed(db, pid, identity, *, expected=None):
    rows = db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND "
                      + current_meta_pred("meta", target=patient_context_refresh_state.CURRENT_VERSION)
                      + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.audit_key') END=?",
                      (patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_PASS, identity), pid, identity["audit_key"])).fetchall()
    if len(rows) != 1:
        return False
    expected = _quality_pass_content(db, pid, identity) if expected is None else expected
    if identity.get("purpose", "summary") == "source":
        try:
            content, meta = strict_json(rows[0][0]), strict_json(rows[0][1])
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            return False
        return (expected is not None and content == expected and isinstance(meta, dict)
                and all(meta.get(key) == identity[key] for key in ("input_key", "candidate_key", "audit_key")))
    return expected is not None and loads_dict(rows[0][0]) == expected


def _quality_pass_present(db, pid, identity):
    return db.execute("SELECT 1 FROM artifacts WHERE kind=? AND project_id=? AND "
                      + current_meta_pred("meta", target=patient_context_refresh_state.CURRENT_VERSION)
                      + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.audit_key') END=? LIMIT 1",
                      (patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_PASS, identity), pid, identity["audit_key"])).fetchone() is not None


def _quality_mark_pass(ledger, pid, identity, claim, selected_ids, current, threshold, *, descriptor=None):
    import semantic_runtime
    if ledger.db.in_transaction:
        raise RuntimeError("patient_summary_transaction_active")
    if not current():
        raise ValueError("patient_summary_quality_source_changed")
    # Validate the entire receipt set in a read snapshot, never under the
    # writer lock. The caller's current() fence is checked again in the
    # following writer transaction, including any intervening own writes.
    version = ledger.db.execute("PRAGMA data_version").fetchone()[0]
    changes = ledger.db.total_changes
    ledger.db.execute("BEGIN")
    try:
        purpose = identity.get("purpose", "summary")
        if semantic_runtime.patient_summary_request_problem(ledger.db, pid, identity, identity["fact_ids"], purpose=purpose):
            raise ValueError("patient_summary_quality_unresolved_request")
        for start in range(0, len(identity["fact_ids"]), 12):
            ids = identity["fact_ids"][start:start + 12]
            answers = _quality_answers(ledger.db, pid, identity["audit_key"], ids, purpose=purpose, identity=identity)
            if set(answers) != set(ids) or any(
                    answers[fid]["confidence"] < threshold
                    or answers[fid]["choice"] != patient_context_refresh_state._quality_expected(identity, fid, selected_ids)
                    for fid in ids):
                raise ValueError("patient_summary_quality_incomplete")
        pass_content = _quality_pass_content(ledger.db, pid, identity, threshold=threshold, descriptor=descriptor)
        if pass_content is None:
            raise ValueError("patient_source_quality_receipt_missing")
    finally:
        ledger.db.rollback()
    if ledger.db.execute("PRAGMA data_version").fetchone()[0] != version or ledger.db.total_changes != changes:
        raise ValueError("patient_summary_quality_source_changed")
    fd = patient_context_refresh_state.acquire_run_lock()
    if fd is None:
        raise patient_context_refresh_state.Deferred("patient_summary_quality_lock")
    try:
        ledger.db.execute("BEGIN IMMEDIATE")
        try:
            if (not _quality_owns(ledger.db, pid, identity["input_key"], claim) or not current()
                    or _quality_failed(ledger.db, pid, identity)
                    or ledger.db.execute("PRAGMA data_version").fetchone()[0] != version
                    or ledger.db.total_changes != changes):
                raise ValueError("patient_summary_quality_source_changed")
            writes = 0
            if not _quality_passed(ledger.db, pid, identity, expected=pass_content):
                prior = (ledger.db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? AND "
                    + current_meta_pred("meta", target=patient_context_refresh_state.CURRENT_VERSION)
                    + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.audit_key') END=? LIMIT 2",
                    (patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_PASS, identity), pid, identity["audit_key"])).fetchall()
                    if purpose == "source" else [])
                if prior:
                    # Enrich one legacy PASS only after revalidating every original receipt.
                    old_content = strict_json(prior[0][1])
                    old_meta = strict_json(prior[0][2])
                    if (len(prior) != 1 or old_content != {k: v for k, v in pass_content.items() if k != "descriptor"}
                            or not isinstance(old_meta, dict) or any(old_meta.get(k) != identity[k]
                                for k in ("input_key", "candidate_key", "audit_key"))
                            or ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=? AND kind=? "
                                "AND project_id=? AND content=? AND meta=?",
                                (json.dumps(pass_content, separators=(",", ":")), prior[0][0],
                                 patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_PASS, identity), pid, prior[0][1], prior[0][2])).rowcount != 1):
                        raise ValueError("patient_source_quality_receipt_missing")
                else:
                    ledger.artifact_add_tx(patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_PASS, identity),
                        json.dumps(pass_content, separators=(",", ":")),
                        project_id=pid, meta={VERSION_KEY: patient_context_refresh_state.CURRENT_VERSION, **{key: identity[key] for key in
                            ("input_key", "candidate_key", "audit_key")}})
                writes = 1
            if not _quality_passed(ledger.db, pid, identity, expected=pass_content) or not current(writes):
                raise ValueError("patient_summary_quality_source_changed")
            ledger.db.commit()
            return True
        except BaseException:
            ledger.db.rollback()
            raise
    finally:
        os.close(fd)


def _quality_repair_spent(db, pid, input_key):
    return db.execute("SELECT 1 FROM artifacts WHERE kind=? AND project_id=? AND "
                      + current_meta_pred("meta", target=patient_context_refresh_state.CURRENT_VERSION)
                      + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.input_key') END=? LIMIT 1",
                      (patient_context_refresh_state.QUALITY_REPAIR, pid, input_key)).fetchone() is not None


def _other_summary_repair_spent(db, pid, identity):
    for (raw,) in db.execute("SELECT meta FROM artifacts WHERE kind=? AND project_id=?", (patient_context_refresh_state.QUALITY_REPAIR, pid)):
        try:
            meta = strict_json(raw)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return True
        if not isinstance(meta, dict) or not valid_hash(meta.get('input_key')):
            return True
        if meta['input_key'] == identity['input_key']:
            continue
        if (not valid_hash(meta.get('repair_scope')) or not valid_hash(identity.get('repair_scope'))
                or meta['repair_scope'] == identity['repair_scope']):
            return True
    return False


def _quality_retry(db, pid, audit_key, page_key, *, purpose="summary"):
    row = db.execute("SELECT content FROM artifacts WHERE kind=? AND project_id=? AND "
                     + current_meta_pred("meta", target=patient_context_refresh_state.CURRENT_VERSION)
                     + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.audit_key') END=? "
                     "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.page_key') END=? "
                     "ORDER BY artifact_id DESC LIMIT 1", (patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_RETRY, {"purpose": purpose}), pid, audit_key, page_key)).fetchone()
    if row is None:
        return {"attempts": 0, "next_try": 0, "held": False}
    value = loads_dict(row[0])
    if (not isinstance(value, dict) or set(value) != {"attempts", "next_try", "held"}
            or type(value["attempts"]) is not int or not 0 <= value["attempts"] <= 6
            or type(value["next_try"]) not in (int, float) or not math.isfinite(value["next_try"])
            or type(value["held"]) is not bool):
        raise ValueError("patient_summary_quality_retry_invalid")
    return value


def _quality_record_failure(ledger, pid, identity, claim, page_key, failure_class, current):
    if ledger.db.in_transaction:
        raise RuntimeError("patient_summary_transaction_active")
    if failure_class not in ("retry", "failed") or not current():
        raise ValueError("patient_summary_quality_source_changed")
    fd = patient_context_refresh_state.acquire_run_lock()
    if fd is None:
        raise patient_context_refresh_state.Deferred("patient_summary_quality_lock")
    try:
        ledger.db.execute("BEGIN IMMEDIATE")
        try:
            if not _quality_owns(ledger.db, pid, identity["input_key"], claim) or not current():
                raise ValueError("patient_summary_quality_source_changed")
            purpose = identity.get("purpose", "summary")
            prior = _quality_retry(ledger.db, pid, identity["audit_key"], page_key, purpose=purpose)
            attempts = min(6, prior["attempts"] + 1)
            value = {"attempts": attempts, "next_try": patient_context_refresh_state.time.time() + min(3600, 300 * attempts),
                     "held": failure_class == "failed" or attempts >= 6}
            ledger.artifact_add_tx(patient_context_refresh_state._quality_kind(patient_context_refresh_state.QUALITY_RETRY, identity), json.dumps(value, separators=(",", ":")), project_id=pid,
                meta={VERSION_KEY: patient_context_refresh_state.CURRENT_VERSION, "input_key": identity["input_key"],
                      "audit_key": identity["audit_key"], "page_key": page_key})
            if _quality_retry(ledger.db, pid, identity["audit_key"], page_key, purpose=purpose) != value or not current(1):
                raise ValueError("patient_summary_quality_source_changed")
            ledger.db.commit()
        except BaseException:
            ledger.db.rollback()
            raise
    finally:
        os.close(fd)


def _quality_reserve_repair_tx(ledger, pid, identity, claim, current, receipt_fence,
                               reservation, artifact_id, owner):
    """Consume the repair in its monetary reservation's existing transaction."""
    if not ledger.db.in_transaction:
        raise RuntimeError("patient_summary_transaction_active")
    if (not current(1) or not _quality_owns(ledger.db, pid, identity["input_key"], claim)
            or reservation.get("route") != "patient_context" or reservation.get("project_id") != pid
            or reservation.get("message_id") is not None
            or reservation.get("unit_key") != "summary_quality_repair"
            or reservation.get("owner") != owner):
        raise ValueError("patient_summary_quality_source_changed")
    if (ledger.db.execute("PRAGMA data_version").fetchone()[0] != receipt_fence[0]
            or ledger.db.total_changes != receipt_fence[1] + 1
            or _quality_repair_spent(ledger.db, pid, identity["input_key"])
            or _other_summary_repair_spent(ledger.db, pid, identity)):
        raise ValueError("patient_summary_quality_source_changed")
    ledger.artifact_add_tx(patient_context_refresh_state.QUALITY_REPAIR, "{}", project_id=pid,
        meta={VERSION_KEY: patient_context_refresh_state.CURRENT_VERSION, "input_key": identity["input_key"],
              "candidate_key": identity["candidate_key"],
              "reservation_artifact_id": artifact_id, "reservation_id": reservation["reservation_id"],
              "request_fp": reservation["request_fp"],
              **({'repair_scope': identity['repair_scope']} if valid_hash(identity.get('repair_scope')) else {})})
    if not _quality_repair_spent(ledger.db, pid, identity["input_key"]) or not current(2):
        raise ValueError("patient_summary_quality_source_changed")
    return 1


def _new_summary_repair_response(db, pid, identity, provider, prompt, response, summary):
    import cloud_inference
    if (len(response) != 1 or response[0][0] != prompt or len(provider.completed_reservations) != 1):
        raise ValueError("patient_summary_repair_evidence_invalid")
    reservation = provider.completed_reservations[0]
    rid = reservation["reservation_id"]
    rows = db.execute("SELECT artifact_id FROM artifacts WHERE kind='cloud_usage_reserve' AND project_id=? "
                      "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.reservation_id') END=? LIMIT 2",
                      (pid, rid)).fetchall()
    if len(rows) != 1:
        raise ValueError("patient_summary_repair_evidence_invalid")
    owner = reservation["owner"]
    if owner.get("source_sig") != identity["input_key"] or not isinstance(owner.get("night_id"), str):
        raise ValueError("patient_summary_repair_evidence_invalid")
    proof = {"version": 1, "answer_text": response[0][1], "reservation_artifact_id": rows[0][0],
             "reservation_id": rid, "contract": provider.contract, "night_id": owner["night_id"],
             "original_audit_key": identity["audit_key"],
             "original_ids": [row["fact_ids"][0] for row in summary]}
    evidence = cloud_inference.verify_cloud_repair_evidence(
        db, reservation_artifact_id=proof["reservation_artifact_id"], reservation_id=rid,
        route="patient_context", project_id=pid, message_id=None, unit_key="summary_quality_repair",
        owner=owner, prompt=prompt, answer_text=proof["answer_text"], contract=proof["contract"])
    proof["receipt_sig"] = evidence["witness"]
    return proof


_repair_response = context_artifacts.repair_response


def _summary_repair_response(db, pid, original, repaired, content, meta, prompt):
    import cloud_inference
    proof = _repair_response(content)
    if (not isinstance(proof, dict) or set(proof) != {"version", "answer_text", "reservation_artifact_id",
            "reservation_id", "contract", "night_id", "original_audit_key", "original_ids", "receipt_sig"}
            or type(proof["version"]) is not int or proof["version"] != 1
            or not isinstance(proof["night_id"], str) or not valid_hash(proof["original_audit_key"])
            or proof["original_audit_key"] != original["audit_key"]
            or not isinstance(proof["original_ids"], list) or not valid_hash(proof["receipt_sig"])
            or meta.get("input_key") != original["input_key"]
            or meta.get("original_failure_key") != original["failure_key"]
            or meta.get("candidate_key") != repaired["candidate_key"] or meta.get("audit_key") != repaired["audit_key"]):
        raise ValueError("patient_summary_repair_evidence_invalid")
    try:
        answer = strict_json(proof["answer_text"])
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("patient_summary_repair_evidence_invalid") from None
    if answer != {"fact_ids": content.get("fact_ids")}:
        raise ValueError("patient_summary_repair_evidence_invalid")
    owner = {"night_id": proof["night_id"], "source_sig": original["input_key"]}
    evidence = cloud_inference.verify_cloud_repair_evidence(
        db, reservation_artifact_id=proof["reservation_artifact_id"], reservation_id=proof["reservation_id"],
        route="patient_context", project_id=pid, message_id=None, unit_key="summary_quality_repair",
        owner=owner, prompt=prompt, answer_text=proof["answer_text"], contract=proof["contract"])
    if evidence["witness"] != proof["receipt_sig"]:
        raise ValueError("patient_summary_repair_evidence_invalid")
    return proof
