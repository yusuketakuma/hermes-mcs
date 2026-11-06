"""Verify summaries and repair evidence before guarded stage persistence."""
from __future__ import annotations
import mcs_util
import semantic_audit
import semantic_render
import semantic_transport
import json
import hashlib
import sqlite3
from mcs_util import strict_json
from inference_version import (CURRENT_VERSION, VERSION_KEY, is_current)
import semantic_jev as jev
import semantic_runtime as runtime
from semantic_audit import audit_code
from semantic_llm import summarize
from semantic_policy import (KIND_ASSESS, KIND_AUDIT,
                             KIND_CANDIDATE, KIND_FACTS, KIND_FACTS_V2,
                             KIND_REPAIR,
                             KIND_SHADOW_V2, KIND_SUMMARY,
                             SCHEMA_VERSION)
from semantic_store import current_artifact


def _shadow_v2_generation() -> str:
    from semantic_extraction import v2_generation
    generation = v2_generation()
    contract = semantic_transport.inference_contract()
    return generation if contract is None else generation + '|producer:' + mcs_util.payload_hash(contract)


def _shadow_v2_tried(ledger, mid: int, fp: str, policy: str) -> bool:
    """This source + policy already ran the current v2 extraction in shadow
    and it stayed incomplete — a receipt, never a completed document."""
    row = current_artifact(ledger.db, KIND_SHADOW_V2, mid, fp, policy)
    return (row is not None and row["content"].get("status") == "incomplete"
            and row["meta"].get("generation") == _shadow_v2_generation())


def _jev_fail_flag(error) -> str | None:
    """Map a Jev failure onto this pass's accounting flags —
    'resource' waits (never counted against the input), 'retryable'
    retried, 'hard' stops the generation.  None when no error."""
    if error is None:
        return None
    return {"resource": "resource",
            "retry": "retryable"}.get(jev.failure_class(error), "hard")


def _job_diagnostics() -> dict:
    """Allocate code-only diagnostics owned by one served job, including QC."""
    return {"code": None, "summary_validation_error": None,
            "loops_rejected": None, "not_sent": None}


def _why(diagnostics: dict, code: str) -> bool:
    """Record ``code`` unless an earlier cause already did; returns True
    so a flag assignment can carry its reason."""
    if diagnostics["code"] is None:
        diagnostics["code"] = code
    return True


def _summary_provenance(producer, fp, policy, revision, summary, jev="unknown"):
    proof = {"version": 1, "stage": "summary", "providers": producer["providers"],
            "complete": producer["complete"], "result_hash": mcs_util.payload_hash(summary),
            "source_fingerprint": fp, "policy_fingerprint": policy,
            "target_revision": revision, "jev": jev}
    if 'repair_evidence' in producer:
        proof['repair_evidence'] = producer['repair_evidence']
        if 'repair_render_phase' in producer:
            proof['repair_render_phase'] = producer['repair_render_phase']
    return proof


def _summary_producer(artifact, fp, policy, revision, *, db=None, bundle=None):
    unknown = {"providers": [], "complete": False}
    if not artifact:
        return unknown
    proof = artifact["meta"].get("provider_provenance")
    if (not isinstance(proof, dict) or type(proof.get("version")) is not int or proof["version"] != 1
            or proof.get("stage") != "summary" or proof.get("source_fingerprint") != fp
            or proof.get("policy_fingerprint") != policy or proof.get("target_revision") != revision
            or proof.get("result_hash") != mcs_util.payload_hash(artifact["content"])
            or type(proof.get("complete")) is not bool or not isinstance(proof.get("providers"), list)
            or any(not isinstance(p, str) or p not in ("local", "anthropic", "openai")
                   for p in proof["providers"])
            or len(set(proof["providers"])) != len(proof["providers"])
            or (proof["complete"] and not proof["providers"])):
        return unknown
    producer = {"providers": proof["providers"], "complete": proof["complete"]}
    if 'repair_evidence' in proof:
        reference = proof['repair_evidence']
        if (not isinstance(reference, dict) or set(reference) != {'artifact_id', 'hash'}
                or type(reference.get('artifact_id')) is not int or db is None or bundle is None):
            return unknown
        try:
            row = db.execute('SELECT content,meta,project_id,message_id FROM artifacts WHERE artifact_id=? AND kind=?',
                             (reference['artifact_id'], KIND_REPAIR)).fetchone()
            content, meta = (strict_json(row['content']), strict_json(row['meta'])) if row else (None, None)
            evidence = content.get('__mcs_internal_proof_v1') if isinstance(content, dict) else None
            phase = proof.get('repair_render_phase')
            if phase not in (None, 'pending', 'rendered'):
                return unknown
            if phase is not None and isinstance(evidence, dict) and evidence.get('fact_kind') != KIND_FACTS_V2:
                return unknown
            if phase is None and isinstance(evidence, dict) and evidence.get('fact_kind') == KIND_FACTS_V2:
                if artifact['content'].get('audit_status') == 'PENDING':
                    if not _summary_repair_pending(db, bundle, artifact['content'], evidence,
                                                   fp, policy, revision, completed_id=reference['artifact_id']):
                        return unknown
                    phase = 'pending'
                else:
                    phase = 'rendered'
            if (not isinstance(evidence, dict) or not isinstance(evidence.get('answer'), dict)
                    or not isinstance(evidence['answer'].get('contract'), dict)
                    or row['project_id'] != evidence.get('project_id') or row['message_id'] != evidence.get('message_id')
                    or not isinstance(meta, dict) or not is_current(meta) or meta.get('invalidated')
                    or meta.get('fingerprint') != fp or meta.get('policy_fingerprint') != policy
                    or mcs_util.payload_hash(evidence) != reference['hash']
                    or proof['complete'] is not True
                    or proof['providers'] != [evidence['answer']['contract'].get('backend')]
                    or not _summary_repair_evidence(db, bundle, artifact['content'], evidence, fp, policy, revision,
                                                    completed_id=reference['artifact_id'], render_phase=phase)):
                return unknown
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            return unknown
        producer['repair_evidence'] = reference
        if proof.get('repair_render_phase') is not None:
            producer['repair_render_phase'] = proof['repair_render_phase']
    return producer


def _summarize_observed(*args, **kwargs):
    with semantic_transport.trace_inference() as observed:
        value = summarize(*args, **kwargs)
    result = value[0] if isinstance(value, tuple) else value
    producer = {"providers": [observed[-1]] if observed and isinstance(result, dict)
                and not result.get("_input_oversize") else [],
                "complete": bool(observed and isinstance(result, dict) and not result.get("_input_oversize"))}
    return value, producer


def _claim_input_binding(pid, mid, fp, policy, revision, summary, facts):
    """Bind the exact claims and evidence supplied to the claim audit."""
    return mcs_util.payload_hash({"project_id": pid, "message_id": mid, "fingerprint": fp,
                                  "policy": policy, "revision": revision,
                                  "claims": summary.get("claims"), "facts": facts})


def _summary_repair_pending(db, bundle, summary, evidence, fp, policy, revision, *, completed_id):
    """Accept an unrendered result only when its interrupted audit is retained."""
    try:
        pid, mid = evidence['project_id'], evidence['message_id']
        if (type(completed_id) is not int or completed_id <= 0
                or summary.get('audit_status') != 'PENDING' or bundle.get('source_fingerprint') != fp
                or next(m for m in bundle['members'] if m['message_id'] == mid)['revision'] != revision):
            return False
        prior = db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? '
                           'AND message_id=? ORDER BY artifact_id DESC LIMIT 1', (KIND_SUMMARY, pid, mid)).fetchone()
        audit = db.execute('SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? '
                           'AND message_id=? ORDER BY artifact_id DESC LIMIT 1', (KIND_AUDIT, pid, mid)).fetchone()
        if prior is None or audit is None:
            return False
        stored, smeta = strict_json(prior['content']), strict_json(prior['meta'])
        result, ameta = strict_json(audit['content']), strict_json(audit['meta'])
        fact_kind = evidence.get('fact_kind')
        if fact_kind not in (KIND_FACTS, KIND_FACTS_V2):
            return False
        fact_row = db.execute('SELECT content,meta FROM artifacts WHERE artifact_id=? AND kind=? '
                              'AND project_id=? AND message_id=?',
                              (evidence.get('facts_id'), fact_kind, pid, mid)).fetchone()
        if fact_row is None:
            return False
        fact_doc, fact_meta = strict_json(fact_row['content']), strict_json(fact_row['meta'])
        if (not isinstance(fact_doc, dict) or not isinstance(fact_meta, dict) or not is_current(fact_meta)
                or fact_meta.get('invalidated') or fact_meta.get('stale')
                or fact_meta.get('fingerprint') != fp or fact_meta.get('policy_fingerprint') != policy):
            return False
        if fact_kind == KIND_FACTS_V2:
            import semantic_projection
            facts = semantic_projection.project_v2_facts(fact_doc)
        else:
            facts = fact_doc.get('facts')
        if not isinstance(facts, list):
            return False
        findings = result.get('findings') if isinstance(result, dict) else None
        return (stored == summary and isinstance(smeta, dict) and isinstance(ameta, dict)
                and is_current(smeta) and is_current(ameta)
                and not smeta.get('invalidated') and not ameta.get('invalidated')
                and not smeta.get('stale') and not ameta.get('stale')
                and smeta.get('fingerprint') == ameta.get('fingerprint') == fp
                and smeta.get('policy_fingerprint') == ameta.get('policy_fingerprint') == policy
                and smeta.get('target_revision') == ameta.get('target_revision') == revision
                and isinstance(smeta.get('provider_provenance'), dict)
                and smeta['provider_provenance'].get('repair_evidence') == {
                    'artifact_id': completed_id, 'hash': mcs_util.payload_hash(evidence)}
                and smeta['provider_provenance'].get('repair_render_phase') in (None, 'pending')
                and smeta.get('publication_mode') == ameta.get('publication_mode')
                and smeta.get('audit_status') == ameta.get('audit_status') == 'PENDING'
                and result.get('status') == 'PENDING' and result.get('target_message_id') == mid
                and type(ameta.get('repair_count')) is int and ameta['repair_count'] == 1
                and type(ameta.get('summary_artifact_id')) is int
                and ameta['summary_artifact_id'] == prior['artifact_id']
                and ameta.get('summary_hash') == mcs_util.payload_hash(stored)
                and ameta.get('audit_content_hash') == mcs_util.payload_hash(result)
                and ameta.get('claim_input_binding') == _claim_input_binding(pid, mid, fp, policy, revision, stored, facts)
                and isinstance(findings, list)
                and any(isinstance(item, dict) and item.get('code') == 'support_unevaluated' for item in findings))
    except (StopIteration, KeyError, TypeError, ValueError, AttributeError, RecursionError,
            OverflowError, sqlite3.Error):
        return False


def _summary_repair_evidence(db, bundle, summary, evidence, fp, policy, revision, *,
                             completed_id=None, render_phase=None):
    """Replay a QC summary repair against its retained inputs and settled answer."""
    import bounded_http
    import cloud_inference
    import semantic_llm
    import semantic_projection
    from inference_dependency_loss import source_binding

    try:
        if (not isinstance(evidence, dict) or type(evidence.get('version')) is not int
                or evidence['version'] != 1 or not isinstance(bundle, dict)
                or bundle.get('source_fingerprint') != fp):
            return False
        pid, mid = evidence['project_id'], evidence['message_id']
        if type(pid) is not int or type(mid) is not int or pid <= 0 or mid <= 0:
            return False
        member = next(m for m in bundle['members'] if m['message_id'] == mid)
        if member['revision'] != revision or summary.get('target_message_id') != mid:
            return False

        def row(key, kind):
            artifact_id = evidence[key]
            if type(artifact_id) is not int or artifact_id <= 0:
                raise ValueError('summary_repair_reference_invalid')
            result = db.execute('SELECT content,meta FROM artifacts WHERE artifact_id=? AND kind=? '
                                'AND project_id=? AND message_id=?', (artifact_id, kind, pid, mid)).fetchone()
            if result is None:
                raise ValueError('summary_repair_reference_missing')
            content, meta = strict_json(result['content']), strict_json(result['meta'])
            if (not isinstance(content, dict) or not isinstance(meta, dict) or not is_current(meta)
                    or meta.get('invalidated') or meta.get('stale') or meta.get('fingerprint') != fp
                    or meta.get('policy_fingerprint') != policy):
                raise ValueError('summary_repair_reference_stale')
            return content, meta

        kind = evidence['fact_kind']
        if kind not in (KIND_FACTS, KIND_FACTS_V2):
            return False
        fact_content, _ = row('facts_id', kind)
        facts = (semantic_projection.project_v2_facts(fact_content) if kind == KIND_FACTS_V2
                 else fact_content.get('facts'))
        assessment, assessment_meta = row('assessment_id', KIND_ASSESS)
        original, original_meta = row('original_summary_id', KIND_SUMMARY)
        audit, audit_meta = row('audit_id', KIND_AUDIT)
        marker, marker_meta = row('repair_id', KIND_REPAIR)
        if (not isinstance(facts, list) or assessment_meta.get('fact_id') is not None
                or assessment.get('target_message_id') != mid or not isinstance(assessment.get('verdicts'), dict)
                or original.get('target_message_id') != mid or original_meta.get('target_revision') != revision
                or audit.get('target_message_id') != mid or audit.get('status') != 'PENDING'
                or audit_meta.get('summary_artifact_id') != evidence['original_summary_id']
                or audit_meta.get('summary_hash') != mcs_util.payload_hash(original)
                or audit_meta.get('audit_content_hash') != mcs_util.payload_hash(audit)
                or audit_meta.get('claim_input_binding') != _claim_input_binding(pid, mid, fp, policy, revision, original, facts)
                or audit_meta.get('repair_count') != 1 or marker != {'target_message_id': mid}
                or marker_meta.get('empty_repair') is True or marker_meta.get('repair_count') != 1
                or not (evidence['facts_id'] < evidence['repair_id'] and evidence['assessment_id'] < evidence['repair_id']
                        and evidence['original_summary_id'] < evidence['audit_id'] < evidence['repair_id'])):
            return False
        findings = audit.get('findings')
        if (not isinstance(findings, list) or not findings
                or semantic_audit.audit_status_for(findings, [], True, False) != 'REPAIR_REQUIRED'):
            return False
        checked = audit_code(bundle, facts, original)
        raw_audit = audit_meta.get('claim_audit')
        raw_audit = raw_audit if isinstance(raw_audit, dict) else {}
        verified_finding = any(
            finding in checked or (
                finding.get('code') == 'claim_not_supported'
                and isinstance(raw_audit.get(finding.get('claim')), dict)
                and raw_audit[finding['claim']].get('choice') == 'not_supported'
                and raw_audit[finding['claim']].get('confidence') == finding.get('confidence')
                and semantic_audit._probability(finding.get('confidence'))
                and any(claim.get('claim_id') == finding['claim'] for claim in original.get('claims', [])
                        if isinstance(claim, dict))) for finding in findings if isinstance(finding, dict))
        if not verified_finding:
            return False
        feedback = [f.get('code', '') + ' ' + f.get('statement', f.get('claim', '')) for f in findings]
        answer = evidence['answer']
        if (not isinstance(answer, dict) or set(answer) != {'text', 'prompt_sha256', 'reservation_id', 'contract', 'unit_key', 'owner'}
                or not isinstance(answer['text'], str) or len(answer['text'].encode('utf-8')) > bounded_http.MAX_RESPONSE_BYTES
                or not isinstance(answer['reservation_id'], str) or not isinstance(answer['owner'], dict)):
            return False
        reservations = db.execute("SELECT artifact_id FROM artifacts WHERE kind='cloud_usage_reserve' "
            "AND project_id=? AND message_id=? AND CASE WHEN json_valid(meta) "
            "THEN json_extract(meta,'$.reservation_id')=? ELSE 0 END LIMIT 2", (pid, mid, answer['reservation_id'])).fetchall()
        if len(reservations) != 1 or reservations[0]['artifact_id'] <= evidence['repair_id']:
            return False
        if completed_id is not None and (type(completed_id) is not int
                                         or reservations[0]['artifact_id'] >= completed_id):
            return False
        owner = answer['owner']
        root = db.execute('SELECT COALESCE(parent_id,message_id) FROM messages WHERE project_id=? '
                          'AND message_id=?', (pid, mid)).fetchone()
        if root is None:
            return False
        members = db.execute('SELECT message_id FROM messages WHERE project_id=? AND (message_id=? OR parent_id=?) '
                             'ORDER BY message_id', (pid, root[0], root[0])).fetchall()
        occurrences = [(r[0], source_binding(db, pid, r[0])) for r in members]
        if (not occurrences or any(binding is None for _, binding in occurrences)
                or owner.get('semantic_target_id') != mid or owner.get('semantic_thread_root') != root[0]
                or owner.get('semantic_phase') != 'summary'
                or owner.get('semantic_source') != mcs_util.payload_hash(occurrences)
                or not isinstance(owner.get('semantic_generation'), str) or not owner['semantic_generation']
                or not isinstance(answer['unit_key'], str) or not answer['unit_key'].startswith('semantic:')):
            return False
        called = []

        def reply(prompt):
            if called or answer['prompt_sha256'] != hashlib.sha256(prompt.encode('utf-8')).hexdigest():
                raise runtime.RuntimeStale('summary_repair_prompt_invalid')
            called.append(True)
            try:
                cloud_inference.verify_cloud_repair_evidence(
                    db, reservation_artifact_id=reservations[0]['artifact_id'], reservation_id=answer['reservation_id'],
                    route='semantic_backlog', project_id=pid, message_id=mid, unit_key=answer['unit_key'],
                    owner=owner, prompt=prompt, answer_text=answer['text'], contract=answer['contract'])
            except ValueError:
                raise runtime.RuntimeStale('summary_repair_receipt_invalid') from None
            return answer['text']

        replay = semantic_llm.summarize(reply, bundle, mid, facts, assessment['verdicts'], feedback=feedback)
        if not called or not isinstance(replay, dict):
            return False
        if kind == KIND_FACTS_V2:
            if render_phase == 'pending':
                if not _summary_repair_pending(db, bundle, summary, evidence, fp, policy, revision,
                                               completed_id=completed_id):
                    return False
            elif render_phase != 'pre_render':
                if render_phase not in (None, 'rendered'):
                    return False
                status = summary.get('audit_status')
                if status not in ('PASS', 'PENDING', 'NEEDS_REVIEW', 'SKIPPED'):
                    return False
                if semantic_render.apply_mandatory_render(replay, [], status, fact_content) != status:
                    return False
        return ({k: v for k, v in replay.items() if k != 'audit_status'}
                == {k: v for k, v in summary.items() if k != 'audit_status' and not k.startswith('_')})
    except (StopIteration, KeyError, TypeError, ValueError, AttributeError, RecursionError,
            OverflowError, UnicodeError, sqlite3.Error, runtime.RuntimeGuardError):
        return False


def _empty_repair_prior(db, pid, mid):
    """Identify the latest summary and audit without replacing either."""
    refs = []
    for kind in (KIND_SUMMARY, KIND_AUDIT):
        row = db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? '
                         'AND project_id=? AND message_id=? ORDER BY artifact_id DESC LIMIT 1',
                         (kind, pid, mid)).fetchone()
        if row is None:
            refs.append(None)
            continue
        content, meta = strict_json(row['content']), strict_json(row['meta'])
        if not isinstance(content, dict) or not isinstance(meta, dict):
            raise ValueError('empty_repair_prior_invalid')
        refs.append({'id': row['artifact_id'], 'hash': mcs_util.payload_hash([content, meta])})
    return refs


def _empty_repair_candidate(db, pid, mid, fp, policy, revision, mode, facts, *, reason_out=None):
    """Recover only the latest source-bound, reserved empty repair candidate."""
    def held(reason, reserved=True):
        if reason_out is not None:
            reason_out['code'] = reason
        return None, reserved

    try:
        row = db.execute('SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? '
                         'AND message_id=? ORDER BY artifact_id DESC LIMIT 1',
                         (KIND_CANDIDATE, pid, mid)).fetchone()
        content, meta = (strict_json(row['content']), strict_json(row['meta'])) if row else (None, None)
        candidate_bound = (isinstance(meta, dict) and meta.get('stage') == 'empty_repair'
                           and meta.get('fingerprint') == fp)
        marker = db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? '
                            'AND message_id=? ORDER BY artifact_id DESC LIMIT 1',
                            (KIND_REPAIR, pid, mid)).fetchone()
        if marker is None:
            return held('empty_repair_marker_missing', candidate_bound)
        marker_content, marker_meta = strict_json(marker['content']), strict_json(marker['meta'])
        if not isinstance(marker_content, dict) or not isinstance(marker_meta, dict):
            return held('empty_repair_marker_invalid')
        if not isinstance(marker_meta.get('fingerprint'), str):
            return held('empty_repair_marker_invalid')
        if marker_meta.get('fingerprint') != fp or marker_meta.get('empty_repair') is not True:
            return held('empty_repair_marker_binding', candidate_bound)
        if not is_current(marker_meta) or marker_meta.get('invalidated') or marker_meta.get('stale'):
            return held('empty_repair_marker_currentness')
        if row is None:
            return held('empty_repair_candidate_missing')
        if not isinstance(content, dict) or not isinstance(meta, dict):
            return held('empty_repair_candidate_invalid')
        proof = meta.get('provider_provenance')
        producer = _summary_producer({'content': content, 'meta': meta}, fp, policy, revision)
        # Keep the original predicate order and short-circuiting; these labels
        # only describe the first failed gate, never weaken a recovery gate.
        if not is_current(meta) or meta.get('invalidated') or meta.get('stale'):
            return held('empty_repair_candidate_currentness')
        if (meta.get('schema') != SCHEMA_VERSION or meta.get('stage') != 'empty_repair'
                or meta.get('fingerprint') != fp or meta.get('policy_fingerprint') != policy
                or meta.get('publication_mode') != mode or meta.get('target_revision') != revision):
            return held('empty_repair_candidate_binding')
        if (type(meta.get('repair_artifact_id')) is not int or meta['repair_artifact_id'] != marker['artifact_id']
                or marker_content != {'target_message_id': mid}
                or marker_meta.get('policy_fingerprint') != policy
                or type(marker_meta.get('repair_count')) is not int or marker_meta['repair_count'] != 1):
            return held('empty_repair_marker_link')
        if (type(content.get('target_message_id')) is not int or content['target_message_id'] != mid
                or not isinstance(content.get('claims'), list) or not content['claims']):
            return held('empty_repair_claims_invalid')
        if meta.get('claim_input_binding') != _claim_input_binding(pid, mid, fp, policy, revision, content, facts):
            return held('empty_repair_claim_binding')
        if meta.get('prior') != _empty_repair_prior(db, pid, mid):
            return held('empty_repair_prior_unverified')
        if proof != _summary_provenance(producer, fp, policy, revision, content):
            return held('empty_repair_provenance_unverified')
        if meta.get('audit_state') != 'ready' or meta.get('partial_answers') != {}:
            return held('empty_repair_audit_not_ready')
        return {'content': content, 'meta': meta, 'producer': producer}, True
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return held('empty_repair_validation_unavailable')


def _store_empty_repair(ledger, pid, mid, fp, policy, revision, mode, facts,
                        summary, producer, repair_id, prior, guard, *, partial_answers=None, audit_state='ready'):
    """Persist a bounded repair result without modifying the previous audit."""
    content = {key: value for key, value in summary.items() if not key.startswith('_')}
    guard('empty_repair_candidate')
    with ledger.db:
        if not ledger.db.in_transaction:
            ledger.db.execute('BEGIN IMMEDIATE')
        guard('empty_repair_candidate_tx')
        if _empty_repair_prior(ledger.db, pid, mid) != prior:
            raise runtime.RuntimeStale('empty_repair_prior')
        ledger.artifact_add_tx(
            KIND_CANDIDATE, json.dumps(content, ensure_ascii=False), project_id=pid, message_id=mid,
            model=semantic_transport.llm_model(),
            meta={VERSION_KEY: CURRENT_VERSION, 'fingerprint': fp, 'policy_fingerprint': policy,
                  'schema': SCHEMA_VERSION, 'stage': 'empty_repair', 'publication_mode': mode,
                  'target_revision': revision, 'repair_artifact_id': repair_id, 'prior': prior,
                  'claim_input_binding': _claim_input_binding(pid, mid, fp, policy, revision, content, facts),
                  'provider_provenance': _summary_provenance(producer, fp, policy, revision, content),
                  'partial_answers': partial_answers or {}, 'audit_state': audit_state})


def _reused_pass_current(db, pid, mid, fp, policy, revision, mode, result, facts):
    """Allow a reused PASS notice only while its latest paired receipts agree."""
    rows = []
    for kind in (KIND_SUMMARY, KIND_AUDIT):
        row = db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? "
                         "AND message_id=? ORDER BY artifact_id DESC LIMIT 1", (kind, pid, mid)).fetchone()
        if row is None:
            return False
        try:
            content, meta = mcs_util.strict_json(row["content"]), mcs_util.strict_json(row["meta"])
            if (not isinstance(content, dict) or not isinstance(meta, dict) or not is_current(meta)
                    or meta.get("invalidated") or meta.get("stale") or meta.get("audit_status") != "PASS"
                    or meta.get("fingerprint") != fp or meta.get("policy_fingerprint") != policy
                    or meta.get("publication_mode") != mode or meta.get("target_revision") != revision):
                return False
            rows.append((row["artifact_id"], content, meta))
        except (ValueError, TypeError, RecursionError):
            return False
    (summary_id, summary, _), (_, audit, ameta) = rows
    return (summary == result["summary"] and summary.get("audit_status") == "PASS"
            and audit.get("target_message_id") == mid and audit.get("status") == "PASS"
            and audit.get("findings") == result["findings"]
            and ameta.get("summary_artifact_id") == summary_id
            and ameta.get("summary_hash") == mcs_util.payload_hash(summary)
            and ameta.get("audit_content_hash") == mcs_util.payload_hash(audit)
            and ameta.get("claim_input_binding") == _claim_input_binding(
                pid, mid, fp, policy, revision, summary, facts))


def _prior_pending_findings(db, pid, mid, fp, policy, revision, mode, repaired, previous, summary, binding):
    """Keep known negative findings only when the prior pair is bound to this audit input."""
    if previous is None:
        rows = db.execute("SELECT meta FROM artifacts WHERE kind=? AND project_id=? AND message_id=? "
                          "ORDER BY artifact_id DESC LIMIT 1025", (KIND_AUDIT, pid, mid))
        for index, row in enumerate(rows):
            if index == 1024:
                return [], True
            try:
                meta = mcs_util.strict_json(row["meta"])
                if (not isinstance(meta, dict) or not isinstance(meta.get("fingerprint"), str)
                        or not isinstance(meta.get("policy_fingerprint"), str)):
                    return [], True
                if meta["fingerprint"] == fp and meta["policy_fingerprint"] == policy:
                    return [], True
            except (ValueError, TypeError, RecursionError):
                return [], True
        return [], False
    if previous["meta"].get("audit_status") != "PENDING":
        return [], False
    findings = previous["content"].get("findings")
    if not isinstance(findings, list) or any(not isinstance(f, dict) or not isinstance(f.get("code"), str)
                                              for f in findings):
        return [], True
    known = [f for f in findings if f["code"] != "support_unevaluated"]
    audit = db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? "
                       "AND message_id=? ORDER BY artifact_id DESC LIMIT 1", (KIND_AUDIT, pid, mid)).fetchone()
    prior_sum = db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? "
                           "AND message_id=? ORDER BY artifact_id DESC LIMIT 1", (KIND_SUMMARY, pid, mid)).fetchone()
    try:
        ameta = mcs_util.strict_json(audit["meta"])
        content = mcs_util.strict_json(audit["content"])
        smeta = mcs_util.strict_json(prior_sum["meta"])
        stored = mcs_util.strict_json(prior_sum["content"])
        bound = (is_current(ameta) and is_current(smeta)
                 and ameta == previous["meta"] and content == previous["content"]
                 and ameta.get("audit_content_hash") == mcs_util.payload_hash(content)
                 and ameta.get("summary_artifact_id") == prior_sum["artifact_id"]
                 and ameta.get("summary_hash") == mcs_util.payload_hash(stored)
                 and ameta.get("claim_input_binding") == binding
                 and smeta.get("fingerprint") == ameta.get("fingerprint")
                 and smeta.get("policy_fingerprint") == ameta.get("policy_fingerprint")
                 and smeta.get("target_revision") == ameta.get("target_revision")
                 and all(summary.get(k) == stored.get(k) for k in ("claims",)))
        # Old PENDING pairs predate the hash/ID receipt. Permit only the
        # strictly identifiable no-answer case, never an old repair or any
        # partial finding/verdict whose source cannot be proven.
        legacy_empty = (not known and bool(findings) and not repaired and is_current(ameta) and is_current(smeta)
                        and not any(key in ameta for key in ("summary_artifact_id", "claim_input_binding", "audit_content_hash"))
                        and ameta == previous["meta"] and content == previous["content"]
                        and content.get("status") == "PENDING" and content.get("target_message_id") == mid
                        and ameta.get("audit_status") == smeta.get("audit_status") == "PENDING"
                        and ameta.get("fingerprint") == smeta.get("fingerprint") == fp
                        and ameta.get("policy_fingerprint") == smeta.get("policy_fingerprint") == policy
                        and ameta.get("publication_mode") == smeta.get("publication_mode") == mode
                        and smeta.get("target_revision") == revision
                        and type(ameta.get("repair_count")) is int and ameta["repair_count"] == 0
                        and ameta.get("claim_audit") == {}
                        and not any(meta.get(key) for meta in (ameta, smeta) for key in ("invalidated", "stale"))
                        and summary.get("claims") == stored.get("claims"))
        bound = bound or legacy_empty
    except (TypeError, KeyError, ValueError, AttributeError, RecursionError):
        bound = False
    return (known, False) if bound else ([], True)


def _write_result(ledger, pid: int, mid: int, r: dict, fp: str,
                  members: dict, final_status: str, policy: str, publication_mode: str) -> None:
    """Summary + audit artifact pair for one evaluated target — the
    durable record of this generation's outcome. The repair_count meta
    is the INV-10 budget: it survives restarts AND mid-job deferrals
    because it lives on this artifact, not in process memory."""
    summary = {k: v for k, v in r["summary"].items()
               if not k.startswith("_")}
    provenance = _summary_provenance(r.get("producer", {"providers": [], "complete": False}),
                                     fp, policy, members[mid]["revision"], summary,
                                     r.get("jev_outcome", "unknown"))
    skip_meta = {}
    if final_status == "SKIPPED":
        reason = r.get("jev_skip_reason")
        if reason not in {"not_configured", "daily_budget", "backlog_reserved", "disabled",
                          "service_unavailable", "circuit_open"}:
            raise ValueError("semantic_skip_reason_invalid")
        skip_meta = {"jev_skip_reason": reason, "jev_evaluated": False,
                     "code_checks": "passed", "summary_hash": mcs_util.payload_hash(summary)}
    # A deferred/pending job re-derives the same outcome on every pass.
    # Without this guard, unreachable Jev can produce byte-identical
    # summary+audit pairs repeatedly. The durable store records an outcome
    # once, not once per attempt (FIX-SD1).
    prev_sum = current_artifact(ledger.db, KIND_SUMMARY, mid, fp, policy)
    prev_aud = current_artifact(ledger.db, KIND_AUDIT, mid, fp, policy)
    if (prev_sum is not None and prev_aud is not None
            and prev_sum["content"] == summary
            and prev_sum["meta"].get("audit_status") == final_status
            and prev_sum["meta"].get("provider_provenance") == provenance
            and all(prev_sum["meta"].get(key) == value for key, value in skip_meta.items())
            and prev_sum["meta"].get("publication_mode")
            == publication_mode
            and prev_aud["content"].get("status") == final_status
            and prev_aud["meta"].get("repair_count") == (1 if r["repaired"] else 0)
            and prev_aud["meta"].get("claim_input_binding") == r.get("claim_input_binding")
            and all(prev_aud["meta"].get(key) == value for key, value in skip_meta.items())
            and prev_aud["content"].get("findings") == r["findings"]):
        return
    summary_id = ledger.artifact_add_tx(
        KIND_SUMMARY,
        json.dumps(summary, ensure_ascii=False),
        project_id=pid, message_id=mid, model=semantic_transport.llm_model(),
        meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
              "audit_status": final_status, "publication_mode": publication_mode,
              "stale": final_status == "STALE",
              "target_revision": members[mid]["revision"],
              "provider_provenance": provenance,
              # the stored content drops _-keys — the oversize marker
              # must survive storage so a later re-audit of the stub
              # keeps its blocking finding
              "input_oversize": bool(r["summary"].get("_input_oversize")), **skip_meta})
    audit_content = {"status": final_status, "findings": r["findings"], "target_message_id": mid}
    ledger.artifact_add_tx(
        KIND_AUDIT,
        json.dumps(audit_content, ensure_ascii=False),
        project_id=pid, message_id=mid, model=jev.JEV_MODEL,
        meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
              "audit_status": final_status, "publication_mode": publication_mode,
              "repair_count": 1 if r["repaired"] else 0,
              "summary_artifact_id": summary_id, "summary_hash": mcs_util.payload_hash(summary),
              "audit_content_hash": mcs_util.payload_hash(audit_content),
              "claim_input_binding": r.get("claim_input_binding"),
              "target_revision": members[mid]["revision"],
              "jev_requests": r.get("jev_requests", 0),
              # raw Jev claim verdicts — the calibration corpus for
              # match_threshold tuning (summary content strips _-keys,
              # so this private channel is the only place they persist)
              "claim_audit": r["summary"].get("_claim_audit") or {}, **skip_meta})


def _park_needs_review(ledger, pid, mid, fp, policy, v2_doc,
                       findings: list | None = None) -> dict:
    """Terminal NEEDS_REVIEW for a canonical generation: a durable audit
    receipt carrying the (code-only) findings, then ``hard_fail`` so the
    job stops for human review instead of deferring or publishing.
    Findings default to incomplete canonical coverage."""
    if findings is None:
        findings = [{"code": "canonical_coverage_incomplete"}]
    ledger.artifact_add(
        KIND_AUDIT,
        json.dumps({"status": "NEEDS_REVIEW", "findings": findings,
                    "target_message_id": mid}, ensure_ascii=False),
        project_id=pid, message_id=mid,
        meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy,
              "audit_status": "NEEDS_REVIEW",
              "technical_status": "needs_review"})
    return {"outcome": "hard_fail", "v2_doc": v2_doc,
            "findings": findings}
