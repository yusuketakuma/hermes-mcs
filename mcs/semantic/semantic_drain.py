"""Semantic drain engine: the durable-job worker (spec §14, §18).

Claims jobs, runs staged extraction/summary/audit, wires the Jev budget
and circuit, records plans, and emits notices. Transport, source bundles
and audit decisions are consumed from their defining modules. This worker
owns claims, guarded stage commits and job outcomes."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401  registers every subdir as import root
import semantic_facts
import writer_lock
import llm_availability  # noqa: E402
import mcs_util
import semantic_audit
import semantic_render
import semantic_extraction
import semantic_store
import semantic_transport
import patient_priority

from functools import wraps, partial
import json
import copy
import hashlib
from contextlib import contextmanager, ExitStack, nullcontext
import math
import heapq
import sqlite3
import threading
import time

from mcs_util import load_config, loads_dict, strict_json
from inference_version import (CURRENT_VERSION, VERSION_KEY, is_current,
                               current_meta_pred, read_version, LEGACY_VERSION)
import semantic_jev as jev
import semantic_runtime as runtime
import semantic_publication as v4
import semantic_conversion as cohort
from semantic_audit import audit_code, audit_status_for
from semantic_llm import summarize
from semantic_loops import update_loops
from semantic_policy import (BACKLOG_BUDGET_RESERVE, JOB_KIND, KIND_ASSESS, KIND_AUDIT,
                             KIND_BUNDLE, KIND_CANDIDATE, KIND_COVERAGE,
                             KIND_DRAIN_RUN, KIND_FACT_AUDIT, KIND_FACT_PROJ,
                             KIND_FACT_REPAIR, KIND_FACTS, KIND_FACTS_V2,
                             KIND_LOOP, KIND_LOOP_EVENT,
                             KIND_PLAN, KIND_POLICY, KIND_REPAIR,
                             KIND_SHADOW_V2, KIND_SUMMARY,
                             KIND_SUMMARY_FORMAT_REPAIR, KIND_USAGE,
                             QC_JOB_KIND, SCHEMA_VERSION, SCHED_KIND,
                             policy_fingerprint, runtime_config)
import semantic_notice
from semantic_store import current_artifact, jev_state, jev_usage_today

import semantic_qc

KIND_LOOP_COMPLETION = 'semantic_loop_completion'


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


def _empty_repair_candidate(db, pid, mid, fp, policy, revision, mode, facts):
    """Recover only the latest source-bound, reserved empty repair candidate."""
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
            return None, candidate_bound
        marker_content, marker_meta = strict_json(marker['content']), strict_json(marker['meta'])
        if not isinstance(marker_content, dict) or not isinstance(marker_meta, dict):
            return None, True
        if not isinstance(marker_meta.get('fingerprint'), str):
            return None, True
        if marker_meta.get('fingerprint') != fp or marker_meta.get('empty_repair') is not True:
            return None, candidate_bound
        if not is_current(marker_meta) or marker_meta.get('invalidated') or marker_meta.get('stale'):
            return None, True
        if row is None:
            return None, True
        if not isinstance(content, dict) or not isinstance(meta, dict):
            return None, True
        proof = meta.get('provider_provenance')
        producer = _summary_producer({'content': content, 'meta': meta}, fp, policy, revision)
        if (not is_current(meta) or meta.get('invalidated') or meta.get('stale')
                or meta.get('schema') != SCHEMA_VERSION or meta.get('stage') != 'empty_repair'
                or meta.get('fingerprint') != fp or meta.get('policy_fingerprint') != policy
                or meta.get('publication_mode') != mode or meta.get('target_revision') != revision
                or type(meta.get('repair_artifact_id')) is not int or meta['repair_artifact_id'] != marker['artifact_id']
                or marker_content != {'target_message_id': mid}
                or marker_meta.get('policy_fingerprint') != policy
                or type(marker_meta.get('repair_count')) is not int or marker_meta['repair_count'] != 1
                or type(content.get('target_message_id')) is not int or content['target_message_id'] != mid
                or not isinstance(content.get('claims'), list) or not content['claims']
                or meta.get('claim_input_binding') != _claim_input_binding(pid, mid, fp, policy, revision, content, facts)
                or meta.get('prior') != _empty_repair_prior(db, pid, mid)
                or proof != _summary_provenance(producer, fp, policy, revision, content)
                or meta.get('audit_state') != 'ready' or meta.get('partial_answers') != {}):
            return None, True
        return {'content': content, 'meta': meta, 'producer': producer}, True
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return None, True


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


def _fact_stage(ledger, scfg, member, pid, mid, fp, policy,
                jev_client, llm_fn, deadline, *, shadow_stats: dict | None = None,
                jev_skip: str | None = None, activate_skip=None, skip_state=None):
    """Fact stage for one bundle member.

    fact_source selects the canonical path: "shadow" still feeds
    consumers from the legacy extractor while writing the v2 document
    for comparison; "canonical" makes the v2 document the fact source —
    extract -> bidirectional audit -> one bounded repair -> legacy
    projection — and refuses to proceed while it stays incomplete; it
    never silently falls back to legacy.

    Returns {"outcome": None, "facts": [...], "v2_doc": doc|None} on
    success.  outcome "incomplete"|"retryable"|"hard_fail" asks the
    caller to break the member loop with the matching failure flag set.
    """
    # facts: reuse a stored set for this fingerprint, else extract.
    fact_source = scfg.get("fact_source", "legacy")
    v2_doc = None
    if fact_source != "legacy":
        early, v2_doc, doc_build = _fact_v2_document(
            ledger, member, pid, mid, fp, policy, fact_source,
            jev_client, llm_fn, deadline, shadow_stats=shadow_stats)
        if early is not None:
            return early
    if fact_source == "canonical":
        early, v2_doc, doc_hash = _fact_canonical_audit(
            ledger, scfg, member, pid, mid, fp, policy, fact_source,
            v2_doc, doc_build, jev_client, llm_fn, deadline, jev_skip=jev_skip,
            activate_skip=activate_skip, skip_state=skip_state)
        if early is not None:
            return early
        from semantic_projection import project_v2_facts
        facts = project_v2_facts(v2_doc)
        if jev_skip is None and not (skip_state and skip_state.get("reason")):
            v4.publish_fact_projection(ledger, member, pid, mid, fp, policy,
                                       v2_doc, doc_hash)
    else:
        early, facts = _fact_legacy(ledger, member, pid, mid, fp, policy,
                                    llm_fn, deadline)
        if early is not None:
            return early
    return {"outcome": None, "facts": facts, "v2_doc": v2_doc}


def _fact_v2_document(ledger, member, pid, mid, fp, policy, fact_source,
                      jev_client, llm_fn, deadline, *, shadow_stats: dict | None = None):
    """Reuse or extract the v2 fact document for a non-legacy fact source.

    Returns (early, v2_doc, doc_build): early is the _fact_stage result to
    return as-is, else None with the document (None when not stored) and
    the hint-merge build it carries."""
    from semantic_extraction import extract_facts_v2
    import semantic_projection  # noqa: F401  loaded before any stage write
    v2_doc = None
    prev_v2 = current_artifact(ledger.db, KIND_FACTS_V2, mid, fp)
    prev_v2, spent, coverage_retry = semantic_facts.cached_facts_plan(
        prev_v2, member, mid, fp, fact_source)
    execution_contract = semantic_transport.inference_contract()
    if prev_v2 is not None and not coverage_retry and execution_contract is not None:
        if prev_v2['meta'].get('repaired'):
            previous = v4._repaired_projection_producer(
                ledger.db, pid, mid, fp, policy, member, prev_v2['content'], prev_v2['meta'])
            if (previous is None or any(item.get('contract') != execution_contract for item in previous['base'])):
                # Historical publication stays intact; do not revive a spent repair.
                return {"outcome": "incomplete", "v2_doc": None}, None, None
        else:
            from semantic_extraction import rebuild_facts_v2_cached
            producer, contracts = {}, []
            rebuilt, reason = rebuild_facts_v2_cached(member, ledger.db, fp, pid, prev_v2['content'],
                prev_v2['meta'], producer_out=producer, contracts_out=contracts)
            if (reason is not None or rebuilt is None or not producer.get('complete')
                    or mcs_util.payload_hash(rebuilt) != mcs_util.payload_hash(prev_v2['content'])):
                return {"outcome": "incomplete", "v2_doc": None}, None, None
            if any(item.get('contract') != execution_contract for item in contracts):
                prev_v2 = None
    if fact_source == "canonical":
        # S0: deterministic prep (extract_v1 hints + atom/chunk
        # boundaries inside extract_facts_v2) — no model call
        v4.record_stage(ledger, pid, mid, fp, policy,
                        "s0_prep", "done")
    # the hint-merge build of the document this run continues from
    doc_build = None
    if prev_v2 is not None and not coverage_retry:
        v2_doc = prev_v2["content"]
        doc_build = prev_v2["meta"].get("build")
        if fact_source == "canonical":
            v4.record_stage(ledger, pid, mid, fp, policy,
                            "s1_extract", "reused")
    elif fact_source == "shadow" and _shadow_v2_tried(
            ledger, mid, fp, policy):
        # this source + policy already ran its shadow comparison and it
        # stayed incomplete (receipt below) — a retry does not re-run it
        pass
    else:
        if time.monotonic() > deadline - 5:
            return {"outcome": "incomplete", "v2_doc": None}, None, None
        if fact_source == "canonical":
            v4.record_stage(ledger, pid, mid, fp, policy,
                            "s1_extract", "start")
        started = time.monotonic()
        shadow = shadow_stats if fact_source == "shadow" else None
        complete = False
        try:
            v2_result = extract_facts_v2(
                llm_fn, member, deadline - 5, ledger=ledger,
                source_fingerprint=fp, project_id=pid,
                jev_client=jev_client, retry_coverage=coverage_retry or spent)
            complete = v2_result["extraction_complete"]
        finally:
            if shadow is not None:      # a raised call ended incomplete too
                shadow["calls"] += 1
                shadow["incomplete"] += 0 if complete else 1
                shadow["s"] += time.monotonic() - started
        if not v2_result["extraction_complete"]:
            if fact_source == "canonical":
                return {"outcome": "retryable"
                        if v2_result["failure_reason"] == "model"
                        else "incomplete", "v2_doc": None}, None, None
            v2_doc = None      # shadow: incomplete doc not stored
            if v2_result["failure_reason"] != "deadline":
                # Without a durable mark every retry re-extracted each
                # finished target until a multi-target job could no
                # longer reach the next one (2026-10-10). The receipt
                # says "incomplete", never complete; a time-out instead
                # resumes from the persisted chunks next pass.
                ledger.artifact_add(
                    KIND_SHADOW_V2,
                    json.dumps({"status": "incomplete",
                                "failure_reason":
                                    v2_result["failure_reason"],
                                # codes/counts only: which "model" stop
                                # it was (LLM failure code, or facts
                                # dropped by the chunk's validation)
                                "llm_failure": semantic_transport.last_llm_failure(),
                                "dropped_in_failed_chunk":
                                    v2_result.get("dropped_in_failed_chunk"),
                                "dropped_reasons":
                                    v2_result.get("dropped_reasons"),
                                "null_contexts_omitted":
                                    v2_result.get("null_contexts_omitted"),
                                "chunks_completed":
                                    v2_result["chunks_completed"],
                                "chunks_total":
                                    v2_result["chunks_total"]}),
                    project_id=pid, message_id=mid,
                    model=semantic_transport.llm_model(),
                    meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp,
                          "policy_fingerprint": policy,
                          "generation": _shadow_v2_generation()})
        else:
            v2_doc = v2_result["doc"]
            from semantic_extraction import FACTS_V2_BUILD
            doc_build = FACTS_V2_BUILD
            meta = {VERSION_KEY: CURRENT_VERSION, "fingerprint": fp,
                    "policy_fingerprint": policy,
                    "schema": SCHEMA_VERSION,
                    "fact_source": fact_source,
                    "coverage_status":
                        v2_doc["coverage"]["status"],
                    "facts": len(v2_doc["facts"]),
                    "open_obligations": len(
                        v2_doc["coverage"]
                        ["open_obligation_ids"]),
                    "build": FACTS_V2_BUILD,
                    # newly normalised this run (reused chunks excluded)
                    "null_contexts_omitted":
                        v2_result.get("null_contexts_omitted", 0)}
            producer = v2_result.get("producer_evidence", {})
            meta["provider_provenance"] = {
                "version": 1, "stage": "facts", "providers": producer.get("providers", []),
                "complete": producer.get("complete") is True,
                "result_hash": mcs_util.payload_hash(v2_doc), "source_fingerprint": fp,
                "policy_fingerprint": policy, "target_revision": member["revision"]}
            if coverage_retry or spent:
                # the bounded retry already ran — a still-incomplete
                # doc is flagged for human review and never
                # re-extracted again (C03)
                meta["coverage_retry"] = True
                if v2_doc["coverage"]["status"] != "complete":
                    meta["needs_review"] = True
            if fact_source == "canonical":
                v4.record_stage(ledger, pid, mid, fp, policy,
                                "s1_extract", "done",
                                facts=len(v2_doc["facts"]))
            ledger.artifact_add(
                KIND_FACTS_V2,
                json.dumps(v2_doc, ensure_ascii=False,
                           allow_nan=False),
                project_id=pid, message_id=mid,
                model=semantic_transport.llm_model(),
                meta={**meta, "inference_requests": semantic_transport.inference_requests()})
    if fact_source == "canonical" and v2_doc is not None \
            and v2_doc["coverage"]["status"] != "complete":
        if coverage_retry or spent \
                or (prev_v2 and prev_v2["meta"].get("coverage_retry")):
            return _park_needs_review(
                ledger, pid, mid, fp, policy, v2_doc), None, None
        return {"outcome": "incomplete", "v2_doc": v2_doc}, None, None
    return None, v2_doc, doc_build


def _fact_canonical_audit(ledger, scfg, member, pid, mid, fp, policy,
                          fact_source, v2_doc, doc_build, jev_client,
                          llm_fn, deadline, *, jev_skip=None, activate_skip=None, skip_state=None):
    """POST_JEV bidirectional fact audit with its one bounded repair.

    Returns (early, v2_doc, doc_hash): early is the _fact_stage result to
    return as-is, else None with the audited document and its hash."""
    fail_kind = "incomplete"
    # POST_JEV: bidirectional audit must pass before the
    # canonical fact set feeds any consumer.  The audit artifact
    # is keyed by doc_hash, so a repaired document is always
    # re-audited; the repair receipt bounds dispatch to one
    # attempt per generation.  Findings hold the generation —
    # never promoted or silently dropped.
    audited = False
    for _attempt in range(2):
        doc_hash = semantic_facts.doc_hash(v2_doc)
        prev_fa = current_artifact(ledger.db, KIND_FACT_AUDIT, mid, fp, policy)
        # C04: only a COMPLETED evaluation is a reusable audit — a
        # stored evaluated:false row (mid-audit outage) must be
        # re-run, otherwise a transient failure pins the generation
        # on a failed verdict forever and manual retry cannot clear it
        fact_audit = semantic_audit.cached_fact_audit(prev_fa, v2_doc, doc_hash)
        if fact_audit is None:
            if time.monotonic() > deadline - 5:
                break
            from semantic_audit import audit_facts_v2
            jev_outcome = "unknown"
            if jev_skip is not None:
                if prev_fa is None:
                    jev_outcome = "skipped"
                fact_audit = semantic_audit.optional_fact_audit(
                    v2_doc, member, fp, jev_skip, prev_fa["content"] if prev_fa else None,
                    match_threshold=scfg["match_threshold"])
            else:
                before_requests = getattr(jev_client, "requests_made", None)
                fact_audit = audit_facts_v2(
                    jev_client, v2_doc, member["body_original"],
                    deadline - 5,
                    match_threshold=scfg["match_threshold"],
                    per_request=scfg.get("max_questions_per_request", 12))
                after_requests = getattr(jev_client, "requests_made", None)
                sent = (type(before_requests) is int and type(after_requests) is int
                        and after_requests > before_requests)
                if sent and fact_audit.get("evaluated") is True:
                    jev_outcome = "done"
                elif sent and fact_audit.get("fact_verdicts"):
                    jev_outcome = "partial"
                reason = semantic_audit.jev_skip_reason(getattr(jev_client, "last_error", None))
                if not fact_audit["evaluated"] and reason is not None and activate_skip is not None:
                    if not sent and prev_fa is None:
                        jev_outcome = "skipped"
                    activate_skip(reason)
                    jev_skip = reason
                    if skip_state is not None:
                        skip_state["reason"] = reason
                    fact_audit = semantic_audit.optional_fact_audit(
                        v2_doc, member, fp, reason, fact_audit, match_threshold=scfg["match_threshold"])
            stored = {"status": fact_audit["status"],
                      "evaluated": fact_audit["evaluated"],
                      "findings": fact_audit["findings"],
                      "fact_verdicts": fact_audit["fact_verdicts"]}
            if fact_audit["status"] == "SKIPPED":
                stored.update(jev_skip_reason=jev_skip, code_checks="passed",
                              document_hash=mcs_util.payload_hash(v2_doc))
            # an unevaluated re-run identical to the stored row (a
            # durable resource wait) adds no information — record
            # an outcome once, not once per drain (FIX-SD1)
            if not (prev_fa is not None
                    and prev_fa["meta"].get("doc_hash") == doc_hash
                    and prev_fa["content"] == stored):
                ledger.artifact_add(
                    KIND_FACT_AUDIT,
                    json.dumps(stored, ensure_ascii=False,
                               allow_nan=False),
                    project_id=pid, message_id=mid,
                    model=jev.JEV_MODEL,
                    meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp,
                          "policy_fingerprint": policy,
                          "schema": SCHEMA_VERSION,
                          "doc_hash": doc_hash,
                          "audit_status": fact_audit["status"],
                          "jev_provenance": {"version": 1, "stage": "fact_audit", "outcome": jev_outcome,
                                             "result_hash": mcs_util.payload_hash(stored), "doc_hash": doc_hash,
                                             "document_hash": mcs_util.payload_hash(v2_doc)}})
        v4.record_stage(
            ledger, pid, mid, fp, policy,
            "s4_reaudit" if _attempt else "s2_fact_audit",
            fact_audit["status"] if fact_audit["evaluated"] or fact_audit["status"] == "SKIPPED"
            else "unevaluated",
            doc_hash=doc_hash)
        if jev_skip is not None and fact_audit["status"] == "SKIPPED":
            audited = True
            break
        if jev_skip is not None and fact_audit["status"] == "NEEDS_REVIEW" and not fact_audit["evaluated"]:
            return _park_needs_review(ledger, pid, mid, fp, policy, v2_doc, fact_audit["findings"]), None, None
        disposition = semantic_audit.canonical_audit_disposition(fact_audit, v2_doc)
        if disposition == "wait":
            error = getattr(jev_client, "last_error", None)
            if error is not None \
                    and jev.failure_class(error) == "failed":
                fail_kind = "hard_fail"
            elif error is None \
                    or jev.failure_class(error) != "resource":
                fail_kind = "retryable"
            break
        if disposition in ("pass", "coverage_incomplete"):
            if disposition == "coverage_incomplete":
                # defense in depth: never mint PASS over open
                # obligations, whatever the audit said
                return _park_needs_review(
                    ledger, pid, mid, fp, policy, v2_doc), None, None
            audited = True
            break
        # NEEDS_REVIEW: one targeted repair dispatch per
        # generation (receipt-persisted), then re-audit.
        rejected, coverage_missing, repair_candidate = semantic_audit.fact_repair_plan(
            fact_audit["findings"], _attempt)
        if repair_candidate \
                and current_artifact(ledger.db, KIND_FACT_REPAIR,
                             mid, fp) is None:
            if time.monotonic() > deadline - 5:
                break      # repair still possible next pass
            # S3 (T18): reserve the single repair dispatch BEFORE
            # the model call — a crash leaves the 'started'
            # receipt, which permanently consumes this generation's
            # one dispatch (no second repair run)
            original_row = ledger.db.execute(
                "SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? "
                "AND message_id=? ORDER BY artifact_id DESC LIMIT 1", (KIND_FACTS_V2, pid, mid)).fetchone()
            audit_row = ledger.db.execute(
                "SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? "
                "AND message_id=? ORDER BY artifact_id DESC LIMIT 1", (KIND_FACT_AUDIT, pid, mid)).fetchone()
            started = ledger.artifact_add(
                KIND_FACT_REPAIR,
                json.dumps({"status": "started",
                            "rejected": rejected},
                           ensure_ascii=False),
                project_id=pid, message_id=mid,
                model=semantic_transport.llm_model(),
                meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp,
                      "policy_fingerprint": policy,
                      "schema": SCHEMA_VERSION,
                      "doc_hash": doc_hash})
            v4.record_stage(ledger, pid, mid, fp, policy,
                            "s3_repair", "reserved",
                            doc_hash=doc_hash)
            from semantic_extraction import repair_facts_v2
            sent, answers = [], []

            def repair_llm(prompt):
                previous_requests = set(semantic_transport.inference_requests())
                try:
                    value = llm_fn(prompt)
                except runtime.RuntimeGuardError as e:
                    if runtime.llm_request_was_dispatched(e):
                        sent.append(True)
                    raise
                except Exception:
                    sent.append(True)
                    raise
                sent.append(True)
                receipt = semantic_transport.inference_receipt()
                contract = semantic_transport.inference_contract()
                if (isinstance(value, str) and receipt is not None and contract is not None
                        and receipt.get('reservation_id') not in previous_requests):
                    import bounded_http
                    try:
                        if len(value.encode('utf-8')) <= bounded_http.MAX_RESPONSE_BYTES:
                            answers.append({'text': value, 'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                                            'reservation_id': receipt.get('reservation_id'), 'contract': contract,
                                            'unit_key': receipt.get('unit_key'), 'owner': receipt.get('owner')})
                    except UnicodeError:
                        pass
                return value
            try:
                with semantic_transport.quality_repair():
                    repair = repair_facts_v2(
                        repair_llm, member, v2_doc, rejected, deadline - 5,
                        **({"coverage_missing": True} if coverage_missing else {}))
            except runtime.RuntimeGuardError:
                if not sent:
                    # stopped before any dispatch — the one repair
                    # was never spent, so release its reservation
                    with ledger.db:
                        ledger.db.execute(
                            "DELETE FROM artifacts WHERE artifact_id=?",
                            (started,))
                raise
            v4.record_stage(ledger, pid, mid, fp, policy,
                            "s3_repair", "completed",
                            repaired=repair["repaired"])
            evidence = None
            if repair['repaired'] and len(answers) == len(sent) and answers and original_row and audit_row:
                from inference_dependency_loss import source_binding
                try:
                    original_meta = strict_json(original_row['meta'])
                    audit_meta = strict_json(audit_row['meta'])
                    if (strict_json(original_row['content']) == v2_doc
                            and original_meta.get('fingerprint') == fp
                            and original_meta.get('policy_fingerprint') == policy
                            and audit_meta.get('doc_hash') == doc_hash
                            and audit_meta.get('fingerprint') == fp
                            and audit_meta.get('policy_fingerprint') == policy):
                        evidence = {'version': 1, 'original_artifact_id': original_row['artifact_id'],
                                    'audit_artifact_id': audit_row['artifact_id'], 'started_artifact_id': started,
                                    'source_binding': source_binding(ledger.db, pid, mid),
                                    'target_revision': member['revision'], 'original_doc_hash': doc_hash,
                                    'repaired_doc_hash': semantic_facts.doc_hash(repair['doc']),
                                    'coverage_missing': coverage_missing, 'answers': answers}
                except (TypeError, ValueError, KeyError, RecursionError):
                    pass
            completed_id = ledger.artifact_add(
                KIND_FACT_REPAIR,
                json.dumps({"rejected": rejected,
                            "repaired": repair["repaired"],
                            "repaired_fact_ids":
                                repair["repaired_fact_ids"],
                            "owner_chunk_ids":
                                repair["owner_chunk_ids"],
                            **({'__mcs_internal_proof_v1': evidence} if evidence is not None else {}),
                            # pair scan cut by the deadline: the
                            # repaired doc was not kept and the
                            # spent repair parks for review below
                            **({"relation_pairs_skipped":
                                repair["relation_pairs_skipped"]}
                               if repair.get("relation_pairs_skipped")
                               else {})},
                           ensure_ascii=False, allow_nan=False),
                project_id=pid, message_id=mid,
                model=semantic_transport.llm_model(),
                meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp,
                      "policy_fingerprint": policy,
                      "schema": SCHEMA_VERSION,
                      "doc_hash": doc_hash,
                      "repaired": repair["repaired"]})
            if repair["repaired"]:
                v2_doc = repair["doc"]
                ledger.artifact_add(
                    KIND_FACTS_V2,
                    json.dumps(v2_doc, ensure_ascii=False,
                               allow_nan=False),
                    project_id=pid, message_id=mid,
                    model=semantic_transport.llm_model(),
                    meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp,
                          "policy_fingerprint": policy,
                          "schema": SCHEMA_VERSION,
                          "fact_source": fact_source,
                          "repaired": True,
                          **({'repair_artifact_id': completed_id} if evidence is not None else {}),
                          "coverage_status":
                              v2_doc["coverage"]["status"],
                          "facts": len(v2_doc["facts"]),
                          **({"build": doc_build} if doc_build else {})})
                # the repair drops every rejected fact and reopens
                # the obligations they covered — a repaired doc with
                # open obligations must never reach PASS (rollout
                # "Nothing degrades to PASS"); the one repair is
                # spent, so the generation parks for review
                if v2_doc["coverage"]["status"] != "complete":
                    return _park_needs_review(
                        ledger, pid, mid, fp, policy, v2_doc), None, None
                continue
        # an evaluated NEEDS_REVIEW with no repair left (no per-fact
        # finding to repair, repair already spent, or still failing
        # after it) is a clinical verdict, not a technical wait —
        # park it for review instead of deferring forever
        return _park_needs_review(
            ledger, pid, mid, fp, policy, v2_doc,
            fact_audit["findings"]), None, None
    if not audited:
        return {"outcome": fail_kind, "v2_doc": v2_doc}, None, None
    return None, v2_doc, doc_hash




def _fact_legacy(ledger, member, pid, mid, fp, policy, llm_fn, deadline):
    """Reuse or extract the legacy fact set.

    Returns (early, facts): early is the _fact_stage result to return
    as-is, else None with the facts."""
    prev_f = current_artifact(ledger.db, KIND_FACTS, mid, fp)
    if prev_f is not None:
        facts = prev_f["content"].get("facts", [])
    else:
        if time.monotonic() > deadline - 5:
            return {"outcome": "incomplete", "v2_doc": None}, None
        facts, f_complete, f_dropped, f_reason = semantic_extraction.extract_facts(
            llm_fn, member, deadline - 5, return_reason=True,
            ledger=ledger, source_fingerprint=fp, project_id=pid)
        if not f_complete:
            return {"outcome": "retryable" if f_reason == "model"
                    else "incomplete", "v2_doc": None}, None
        ledger.artifact_add(
            KIND_FACTS,
            json.dumps({"facts": facts,
                        "evidence": {f["_evidence"]["evidence_id"]:
                                     f["_evidence"] for f in facts
                                     if f.get("_evidence")}},
                       ensure_ascii=False),
            project_id=pid, message_id=mid, model=semantic_transport.llm_model(),
            meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
                  "chunks_total": len(mcs_util.text_chunks(
                      member["body_original"])),
                  "dropped_by_cap": f_dropped})
    return None, facts




def _prefiltered(ledger, mid) -> bool:
    """The message's current extraction is the local no-signal prefilter marker."""
    from mcs_queries import current_extract_pred, json_or_null
    return ledger.db.execute(
        "SELECT 1 FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
        f"WHERE a.kind='extract_llm' AND a.message_id=? {current_extract_pred()} "
        f"AND json_extract({json_or_null('a.meta')},'$.prefilter')='no_signal' LIMIT 1",
        (mid,)).fetchone() is not None


def _partial_assessment(answers, scfg):
    """Validate and reduce previously answered proposition chunks to codes."""
    if (not isinstance(answers, dict)
            or set(answers) - set(jev.active_propositions(scfg))):
        raise ValueError("semantic_partial_assessment_invalid")
    out = {}
    for name, answer in answers.items():
        value = answer.get("noul") if isinstance(answer, dict) else None
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("semantic_partial_assessment_invalid")
        out[name] = {"noul": value, "verdict": jev.verdict_for(
            value, scfg["match_threshold"], scfg["nomatch_threshold"])}
    return out



def _verified_prefilter(ledger, pid, mid):
    import extract_llm
    import patient_context_refresh
    from mcs_queries import current_extract_pred
    try:
        source = ledger.db.execute('SELECT * FROM messages WHERE project_id=? AND message_id=?',
                                   (pid, mid)).fetchone()
        raw = ledger.db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind='extract_llm' AND message_id=? "
                                "AND (project_id IS NULL OR project_id=?) ORDER BY artifact_id DESC LIMIT 1",
                                (mid, pid)).fetchone()
        meta = strict_json(raw['meta']) if raw is not None else None
        content = strict_json(raw['content']) if raw is not None else None
        current = (ledger.db.execute(
            "SELECT 1 FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
            f"WHERE a.artifact_id=? AND m.project_id=? {current_extract_pred()}",
            (raw['artifact_id'], pid)).fetchone() if raw is not None else None)
        return bool(source is not None and isinstance(meta, dict) and isinstance(content, dict)
                    and current is not None and is_current(meta)
                    and not meta.get('invalidated') and meta.get('prefilter') == 'no_signal'
                    and meta.get('hash') == source['content_hash']
                    and patient_context_refresh._saved_cloud_contract(meta) is None
                    and extract_llm._low_signal(source['body_text'], extract_llm._rule_hints(dict(source))))
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return False


def _valid_primary_assessment(content, meta, model, mid, fp, policy, scfg, *, prefiltered=False):
    try:
        if (not isinstance(content, dict) or not isinstance(meta, dict) or not is_current(meta)
                or meta.get('invalidated') or meta.get('stale') or meta.get('fact_id') is not None
                or meta.get('fingerprint') != fp or meta.get('policy_fingerprint') != policy
                or meta.get('schema') != SCHEMA_VERSION or model != jev.JEV_MODEL
                or type(content.get('target_message_id')) is not int or content['target_message_id'] != mid):
            return False
        verdicts = content.get('verdicts')
        if _partial_assessment(verdicts, scfg) != verdicts:
            return False
        status = meta.get('technical_status')
        if status != 'complete':
            return (status in ('retry_wait', 'error', 'pending', 'skipped')
                    and (not verdicts or (meta.get('registry') == jev.REGISTRY_VERSION
                                          and meta.get('model') == jev.JEV_MODEL)))
        if meta.get('registry') != jev.REGISTRY_VERSION or meta.get('model') != jev.JEV_MODEL:
            return False
        return (set(verdicts) == set(jev.active_propositions(scfg))
                or (verdicts == {} and prefiltered and meta.get('skipped') == 'prefilter_no_signal'
                    and type(meta.get('jev_requests')) is int and meta['jev_requests'] == 0))
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return False



def _primary_assessment_history(db, pid, mid, fp, policy):
    assessment, previous = None, []
    for stored in db.execute('SELECT content,meta,model FROM artifacts WHERE kind=? AND project_id=? '
                             'AND message_id=? ORDER BY artifact_id DESC', (KIND_ASSESS, pid, mid)):
        content, meta = strict_json(stored['content']), strict_json(stored['meta'])
        if not isinstance(content, dict) or not isinstance(meta, dict):
            raise ValueError('semantic_assessment_history_invalid')
        if meta.get('fact_id') is not None:
            if not _valid_detail_assessment(content, meta, mid):
                raise ValueError('semantic_assessment_detail_invalid')
            continue
        if (type(meta.get('fingerprint')) is not str or not meta['fingerprint']
                or type(meta.get('policy_fingerprint')) is not str or not meta['policy_fingerprint']):
            raise ValueError('semantic_assessment_binding_invalid')
        if meta.get('fingerprint') != fp or meta.get('policy_fingerprint') != policy:
            continue
        if assessment is None:
            assessment = {'content': content, 'meta': meta, 'model': stored['model']}
        else:
            previous.append((content, meta, stored['model']))
    return assessment, previous


def _valid_primary_history(content, meta, previous, mid, fp, policy, scfg):
    verdicts = content.get('verdicts')
    for old, old_meta, old_model in previous:
        old_status = old_meta.get('technical_status')
        if old_status not in ('complete', 'retry_wait', 'error', 'pending', 'skipped'):
            return False
        if meta.get('technical_status') != 'skipped' and old_status == 'complete':
            continue
        old_verdicts = old.get('verdicts')
        if (not is_current(old_meta) or old_meta.get('invalidated') or old_meta.get('stale')
                or old_meta.get('fingerprint') != fp or old_meta.get('policy_fingerprint') != policy
                or old_meta.get('schema') != SCHEMA_VERSION or old_model != jev.JEV_MODEL
                or (old_verdicts and (old_meta.get('registry') != jev.REGISTRY_VERSION
                                      or old_meta.get('model') != jev.JEV_MODEL))
                or type(old.get('target_message_id')) is not int or old['target_message_id'] != mid
                or _partial_assessment(old_verdicts, scfg) != old_verdicts
                or any(verdicts.get(key) != value for key, value in old_verdicts.items())):
            return False
    return True


def proposition_quality_report(ledger, project_id: int, scfg: dict) -> dict:
    """Per-proposition noul/verdict distribution over current assessment rows.

    For threshold/registry tuning, each message's newest row contributes
    if it is COMPLETE and matches the ACTIVE proposition set; valid
    no-signal rows count as prefiltered."""
    active = jev.active_propositions(scfg)
    latest, seen, superseded, prefiltered_count = {}, set(), 0, 0
    policy = policy_fingerprint(scfg)
    for stored in ledger.db.execute(
            'SELECT message_id,content,meta,model FROM artifacts WHERE kind=? AND project_id=? '
            'ORDER BY artifact_id DESC', (KIND_ASSESS, project_id)):
        try:
            content, meta = strict_json(stored['content']), strict_json(stored['meta'])
        except (ValueError, TypeError, RecursionError, OverflowError):
            content, meta = None, None
        if isinstance(meta, dict) and meta.get('fact_id') is not None:
            continue
        mid = stored['message_id']
        if type(mid) is not int:
            continue
        if mid in seen:
            continue
        seen.add(mid)
        if not isinstance(content, dict) or not isinstance(meta, dict):
            superseded += 1
            continue
        verdicts = content.get('verdicts')
        row = ledger.db.execute('SELECT parent_id FROM messages WHERE project_id=? AND message_id=?',
                                (project_id, mid)).fetchone()
        try:
            bundle = (semantic_store.thread_bundle(ledger.db, project_id, row['parent_id'] or mid, [mid])
                      if row is not None else None)
            fp = bundle['source_fingerprint'] if bundle is not None else None
            prefiltered = (meta.get('skipped') == 'prefilter_no_signal'
                           and _verified_prefilter(ledger, project_id, mid))
            assessment, previous = (_primary_assessment_history(ledger.db, project_id, mid, fp, policy)
                                    if fp is not None else (None, []))
            valid = (assessment is not None and meta.get('technical_status') == 'complete'
                     and assessment == {'content': content, 'meta': meta, 'model': stored['model']}
                     and _valid_primary_assessment(content, meta, stored['model'], mid, fp, policy, scfg,
                                                   prefiltered=prefiltered)
                     and _valid_primary_history(content, meta, previous, mid, fp, policy, scfg))
        except (ValueError, TypeError, KeyError, sqlite3.Error, RecursionError, OverflowError):
            valid = False
        if not valid:
            superseded += 1
            continue
        if verdicts == {} and prefiltered:
            prefiltered_count += 1
            continue
        latest[mid] = verdicts
    props = {}
    for pid, prop in active.items():
        buckets, nouls = {"MATCH": 0, "NO_MATCH": 0, "UNDETERMINED": 0}, []
        invalid = 0
        for verdicts in latest.values():
            entry = verdicts.get(pid)
            verdict = entry.get('verdict') if isinstance(entry, dict) else None
            noul = entry.get('noul') if isinstance(entry, dict) else None
            if verdict not in buckets:
                invalid += 1
                continue
            buckets[verdict] += 1
            if type(noul) in (int, float) and math.isfinite(noul):
                nouls.append(float(noul))
        props[pid] = {"label_ja": prop.get("label_ja", pid),
                      "verdicts": {**buckets, "invalid": invalid},
                      "noul_mean": sum(nouls) / len(nouls) if nouls else None,
                      "noul_min": min(nouls) if nouls else None,
                      "noul_max": max(nouls) if nouls else None}
    return {"project_id": project_id, "messages": len(latest),
            "prefiltered": prefiltered_count, "superseded": superseded, "propositions": props}


def _assess_target(ledger, scfg, bundle, jev_client, pid, mid, fp, policy,
                   prev, deadline, *, diagnostics: dict):
    """Primary Jev proposition assessment of one target, durably recorded.

    Returns (verdicts, flag): verdicts is None when no answer arrived (the
    wait/error row is recorded once per status change); flag is the
    _jev_fail_flag of a raised JevError, else None."""
    flag = None
    state = jev_state(bundle, mid)
    meta_base = {VERSION_KEY: CURRENT_VERSION, "fingerprint": fp, "policy_fingerprint": policy, "model": jev.JEV_MODEL,
                 "registry": jev.REGISTRY_VERSION,
                 "schema": SCHEMA_VERSION}
    answers = None
    try:
        old = prev["content"].get("verdicts", {}) if prev else {}
        if _partial_assessment(old, scfg) != old:
            raise ValueError("semantic_partial_assessment_invalid")
    except (ValueError, TypeError, KeyError):
        _why(diagnostics, "assess_partial_invalid")
        return None, "hard"
    partial = dict(old)
    req0 = jev_client.requests_made if jev_client else 0
    if jev_client is not None and state is not None and _verified_prefilter(ledger, pid, mid):
        if prev and prev["content"].get("verdicts") != {}:
            _why(diagnostics, "assess_partial_invalid")
            return None, "hard"
        # the local prefilter already settled this body as carrying no clinical
        # signal (a lone greeting): asking twelve clinical propositions about it
        # spends a request for a known answer
        ledger.artifact_add(
            KIND_ASSESS,
            json.dumps({"target_message_id": mid, "verdicts": {}, "detail": {}}, ensure_ascii=False),
            project_id=pid, message_id=mid, model=jev.JEV_MODEL,
            meta={**meta_base, "technical_status": "complete", "jev_requests": 0,
                  "skipped": "prefilter_no_signal"})
        return {}, flag
    if jev_client is not None and state is not None:
        qs = {k: jev.noul_question(p["instructions"], p["true"],
                                   p["false"])
              for k, p in jev.active_propositions(scfg).items() if k not in old}
        if not qs:
            _why(diagnostics, "assess_partial_invalid")
            return None, "hard"
        try:
            out = jev.evaluate_chunked(
                jev_client, state, qs, deadline,
                scfg["max_questions_per_request"])
            answers = out["answers"]
        except jev.JevError as e:
            meta_base["technical_status"] = (
                "retry_wait" if e.retryable else "error")
            meta_base["error_kind"] = e.kind
            flag = _jev_fail_flag(e)
            try:
                if hasattr(e, "partial_answers"):
                    new = _partial_assessment(e.partial_answers, scfg)
                    if set(new) - set(qs) or set(new) & set(old):
                        raise ValueError("semantic_partial_assessment_conflict")
                    partial = {**old, **new}
                    e.partial_verdicts = partial
            except (ValueError, TypeError):
                e.partial_invalid = True
                _why(diagnostics, "assess_partial_invalid")
                return None, "hard"
            _why(diagnostics, f"assess_jev_{flag}")
    meta_base["jev_requests"] = (
        jev_client.requests_made - req0) if jev_client else 0
    if answers is None:
        meta_base.setdefault("technical_status", "pending")
        meta_base.setdefault("error_kind", "jev_unavailable")
        # an identical wait/error record adds no information —
        # only a status CHANGE earns a new row, so a durable
        # resource wait (Jev down, no key, budget hold) does not
        # append one artifact per drain forever
        if prev is None or (
                prev["meta"].get("technical_status"),
                prev["meta"].get("error_kind"), prev["content"].get("verdicts")) != (
                meta_base["technical_status"],
                meta_base.get("error_kind"), partial):
            ledger.artifact_add(
                KIND_ASSESS,
                json.dumps({"target_message_id": mid,
                            "verdicts": partial},
                           ensure_ascii=False),
                project_id=pid, message_id=mid,
                model=jev.JEV_MODEL, meta=meta_base)
        return None, flag
    target_verdicts = {k: {"noul": a["noul"],
                           "verdict": jev.verdict_for(
                               a["noul"], scfg["match_threshold"],
                               scfg["nomatch_threshold"])}
                       for k, a in answers.items()}
    try:
        if (set(target_verdicts) != set(qs) or set(target_verdicts) & set(old)
                or _partial_assessment(target_verdicts, scfg) != target_verdicts):
            raise ValueError("semantic_partial_assessment_conflict")
        target_verdicts = {**old, **target_verdicts}
        if set(target_verdicts) != set(jev.active_propositions(scfg)):
            raise ValueError("semantic_partial_assessment_incomplete")
    except (ValueError, TypeError):
        _why(diagnostics, "assess_partial_invalid")
        return None, "hard"
    ledger.artifact_add(
        KIND_ASSESS,
        # "detail" stays in the stored shape (medication detail
        # lives in semantic_assessment rows)
        json.dumps({"target_message_id": mid,
                    "verdicts": target_verdicts,
                    "detail": {}}, ensure_ascii=False),
        project_id=pid, message_id=mid, model=jev.JEV_MODEL,
        meta={**meta_base, "technical_status": "complete"})
    return target_verdicts, flag


def _obtain_summary(ledger, scfg, bundle, llm_fn, guard, pid, mid, fp, policy,
                    facts, verdicts, existing, deadline, *, diagnostics: dict):
    """Stored, candidate, or freshly generated summary for one target, with
    the one reserved format repair.

    Returns (summary, summary_reason, summary_error, sent_error, producer): sent_error
    is the LLMNotSent/RuntimeBudget stop that left no summary, else None;
    any other runtime guard error propagates."""
    sent_error = None
    summary_reason = None
    summary_error = {}
    revision = next(m["revision"] for m in bundle["members"] if m["message_id"] == mid)
    producer = {"providers": [], "complete": False}
    if existing is not None:
        summary = existing["content"]
        producer = _summary_producer(existing, fp, policy, revision, db=ledger.db, bundle=bundle)
        if existing["meta"].get("input_oversize"):
            summary["_input_oversize"] = True
    else:
        candidate = current_artifact(ledger.db, KIND_CANDIDATE, mid, fp, policy)
        if candidate and candidate["meta"].get("publication_mode") == scfg["summary_mode"]:
            summary = candidate["content"]
            producer = _summary_producer(candidate, fp, policy, revision, db=ledger.db, bundle=bundle)
        else:
            format_repair_id = None
            try:
                (summary, summary_reason), producer = _summarize_observed(
                    llm_fn, bundle, mid, facts, verdicts.get(mid, {}),
                    deadline=deadline, return_reason=True,
                    error_out=summary_error)
                if summary is None and summary_reason == "model" \
                        and summary_error.get("code") \
                        and semantic_transport.last_llm_failure() != "length_stop" \
                        and current_artifact(ledger.db, KIND_SUMMARY_FORMAT_REPAIR,
                                     mid, fp, policy) is None:
                    if diagnostics.get("summary_validation_error") is None:
                        diagnostics["summary_validation_error"] = summary_error["code"]
                    guard("summary_format_repair_reservation")
                    format_repair_id = ledger.artifact_add(
                        KIND_SUMMARY_FORMAT_REPAIR,
                        json.dumps({"target_message_id": mid}),
                        project_id=pid, message_id=mid,
                        model=semantic_transport.llm_model(),
                        meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy,
                              "repair_count": 1})
                    (summary, summary_reason), producer = _summarize_observed(
                        llm_fn, bundle, mid, facts, verdicts.get(mid, {}),
                        format_repair=True,
                        deadline=deadline, return_reason=True,
                        error_out=summary_error)
            except runtime.RuntimeGuardError as e:
                if format_repair_id is not None and not runtime.llm_request_was_dispatched(e):
                    with ledger.db:
                        ledger.db.execute(
                            "DELETE FROM artifacts WHERE artifact_id=?",
                            (format_repair_id,))
                if not isinstance(e, (runtime.LLMNotSent, runtime.RuntimeBudget)):
                    raise
                # the local model never got (or ran out of budget
                # for) this target's request: stop here like any
                # incomplete pass, so targets already audited are
                # still committed below; re-raised for _process_job
                sent_error = e
                summary, summary_reason = None, "not_sent"
    return summary, summary_reason, summary_error, sent_error, producer


def _promote_loops(ledger, scfg, pl, pid, bundle, facts_by_target,
                   jev_client, deadline, *, diagnostics: dict):
    """Loop candidates/relation events for the live generation.

    Returns (loops_done, loops_short, resource, retryable, hard): the last
    three are failure flags the caller ORs into its own."""
    loops_short = False
    resource = retryable = hard = False
    rejections = []
    scope = pl.get("retry_command_id")
    try:
        _, loops_done = update_loops(
            ledger, pid, bundle, facts_by_target, jev_client,
            scfg, deadline,
            retry_scope=scope if isinstance(scope, str) else "",
            rejections=rejections)
    except runtime.RuntimeBudgetShort:
        # the Jev cutoff stopped the relation pass: the results below
        # still commit and the job resumes the remaining pairs next
        # tick (last_error is not this stop's cause — no flag)
        loops_done = False
        loops_short = True
    else:
        flag = _jev_fail_flag(getattr(jev_client, "last_error", None)) \
            if not loops_done and jev_client is not None else None
        if rejections:
            if diagnostics.get("loops_rejected") is None:
                diagnostics["loops_rejected"] = rejections[0]["code"]
            if flag is None:
                # Only cached rejections, or a later pair's success
                # reset last_error: the known rejection is the cause —
                # a bounded retry, never a free defer that would loop
                # forever. A later hard/resource failure keeps its own
                # handling below.
                _why(diagnostics, "loops_jev_rejected")
                retryable = True
        if not loops_done and jev_client is not None:
            _why(diagnostics, f"loops_jev_{flag}")
            resource |= flag == "resource"
            retryable |= flag == "retryable"
            hard |= flag == "hard"
    return loops_done, loops_short, resource, retryable, hard




def _process_job_inner(ledger, scfg, job, jev_client, llm_fn, deadline,
                       cfg_path: str | None = None,
                       config_generation: str | None = None,
                       reserve_fn=None, lane: str | None = None, *,
                       jev_transport=None, jev_timing=None,
                       diagnostics: dict, shadow_stats: dict | None = None,
                       jev_skip: str | None = None) -> str:
    """One semantic job -> durable artifacts + job state transition.
    Returns 'done'|'deferred'|'deferred_short'|'retry'|'failed'|'stale'."""
    pid, root = job["project_id"], job["message_id"]
    if jev_skip is not None:
        diagnostics["jev_skip_reason"] = jev_skip
    token = runtime.JobToken.from_row(job)
    deadline = runtime.job_deadline(scfg, deadline)
    # Stages and their result guards keep the job deadline. In the backlog
    # lane every unlocked transport (local model and Jev) must end a commit
    # reserve earlier, so its result can wait out a tick's hold on the run
    # lock and still be written in time.
    transport_deadline = deadline - (_backlog_commit_reserve(scfg)
                                     if lane == "backlog" else 0.0)
    pl = loads_dict(job["payload"])
    if pl is None or not is_current(pl):
        return "failed"
    targets = pl.get("targets", [root])
    if targets is None:
        return "failed"
    try:
        bundle = semantic_store.thread_bundle(ledger.db, pid, root, targets)
    except ValueError:
        return "failed"
    if bundle is None:
        # source vanished — nothing to preserve for it; mark done so
        # the row does not re-run as a no-op on every drain
        return "done" if runtime.transition(ledger, token, "done") \
            else "stale"
    fp = bundle["source_fingerprint"]
    policy = policy_fingerprint(scfg)

    def current_source_fp():
        fresh = semantic_store.thread_bundle(ledger.db, pid, root, targets or [root])
        return fresh["source_fingerprint"] if fresh else None

    def guard(stage):
        try:
            if jev_skip is not None:
                # Even a skipped call must not bypass a malformed/expired
                # authorization window or an unreadable usage ledger.
                try:
                    runtime._jev_window_check(config_path=cfg_path)
                    used = jev_usage_today(ledger)
                    cap = scfg["daily_request_budget"]
                    if lane == "backlog":
                        cap -= math.ceil(cap * BACKLOG_BUDGET_RESERVE)
                    if ((jev_skip in ("daily_budget", "backlog_reserved") and used < cap)
                            or (jev_skip == "disabled" and scfg["daily_request_budget"] > 0)
                            or (jev_skip == "not_configured" and jev.api_key())
                            or (jev_skip == "circuit_open" and (not runtime.circuit_open(ledger)
                                or runtime._circuit_state(ledger)["failure_class"]
                                   not in runtime._CIRCUIT_FAILURE_CLASSES))):
                        raise ValueError("semantic_skip_reason_stale")
                except (jev.JevError, ValueError, sqlite3.Error):
                    raise runtime.RuntimeOff(stage + ':jev_unverified') from None
            runtime.guard(
                ledger, token, deadline=deadline,
                expected_config_generation=config_generation,
                expected_execution_generation=runtime.execution_generation(scfg),
                expected_mode=scfg["mode"], cfg_path=cfg_path,
                load_cfg=load_config, parse_cfg=runtime_config,
                source_fingerprint=fp, current_source=current_source_fp,
                stage=stage)
        except runtime.RuntimeStale as error:
            # Keep the stale marker bound to the worker's original
            # fingerprint.  A fresh bundle must never be used to label an
            # old worker's result.
            if error.stage.endswith(":source"):
                ledger.artifact_add(
                    KIND_AUDIT,
                    json.dumps({"status": "STALE", "findings": [],
                                "target_message_id": root},
                               ensure_ascii=False),
                    project_id=pid, message_id=root, model=jev.JEV_MODEL,
                    meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
                          "audit_status": "STALE",
                          "technical_status": "stale"})
            raise

    guard("admission")
    if cohort.hold_unbounded_job(ledger, job):
        return "failed"

    # The adapters below make every actual Jev/local-model boundary pass
    # through the same identity check.  A real Jev client additionally gets
    # a hook before each internal retry attempt and a durable reservation
    # immediately before the POST.
    jev_client = runtime.bind_jev(
        jev_client, guard, reserve_fn,
        cutoff=transport_deadline if lane == "backlog" else None,
        transport=jev_transport, timing=jev_timing, config_path=cfg_path)
    llm_fn = runtime.guarded_llm(
        # a backlog worker owns its slot: one long-output call may use the
        # transport deadline instead of burning 600 s per attempt (2026-10-10)
        llm_fn, guard, transport_deadline,
        timeout_cap=None if lane == "backlog" else semantic_transport.LLM_TIMEOUT,
        # the shared follow-up reserve or two thirds of the per-job budget,
        # whichever is smaller — a call that cannot fit defers the pass
        # instead of overrunning it
        call_reserve=min(runtime.LLM_CALL_RESERVE_S,
                         float(scfg["job_budget_seconds"]) * 2 / 3))
    # provenance on the recorded bundle (spec §12.1): which stored
    # origin event / capture path this evaluation descends from
    origin = pl.get("origin") if isinstance(pl.get("origin"), dict) \
        else {}
    bundle["origin_event_id"] = origin.get("event_id")
    bundle["capture_origin"] = origin.get("source")
    if current_artifact(ledger.db, KIND_BUNDLE, root, fp, policy) is None:
        ledger.artifact_add(
            KIND_BUNDLE, json.dumps(bundle, ensure_ascii=False, separators=(',', ':')),
            project_id=pid, message_id=root, model=jev.JEV_MODEL,
            meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION})
    all_targets = [t for t in (targets or [root])
                   if any(m["message_id"] == t
                          and m['body_state'] == 'full' and m['body_original']
                          for m in bundle["members"])]
    members = {m["message_id"]: m for m in bundle["members"]}
    fact_source = scfg.get("fact_source", "legacy")
    facts_by_target: dict[int, list] = {}
    v2_docs_by_target: dict[int, dict] = {}
    coverage_by_target: dict[int, list] = {}
    detail_findings: dict[int, list] = {}
    verdicts: dict[int, dict] = {}
    incomplete = False
    hard_fail = False   # non-retryable Jev error — bound the retries
    not_sent = None     # a summary/repair could not dispatch (see below)
    retryable_failure = False
    resource_wait = False

    def record_skipped_assessment(mid, previous, reason, partial=None):
        partial = partial or {}
        guard("assessment_skip")
        if previous is None or (previous["meta"].get("technical_status") != "skipped"
                                or previous["meta"].get("jev_skip_reason") != reason
                                or previous["content"].get("verdicts") != partial):
            ledger.artifact_add(
                KIND_ASSESS, json.dumps({"target_message_id": mid, "verdicts": partial}, ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={VERSION_KEY: CURRENT_VERSION, "fingerprint": fp, "policy_fingerprint": policy,
                      "schema": SCHEMA_VERSION, "model": jev.JEV_MODEL, "registry": jev.REGISTRY_VERSION,
                      "technical_status": "skipped", "jev_skip_reason": reason})

    def activate_skip(reason):
        nonlocal jev_skip, jev_client
        jev_skip = reason
        jev_client = None
        diagnostics["jev_skip_reason"] = reason
        guard("jev_skip")

    for mid in all_targets:
        semantic_transport.set_inference_target(mid)
        member = members[mid]
        # restart-safe: a complete assessment for THIS fingerprint is
        # reused; retry_wait/error/pending ones are re-attempted
        try:
            prev, previous = _primary_assessment_history(ledger.db, pid, mid, fp, policy)
            valid = (prev is None or (_valid_primary_assessment(
                prev["content"], prev["meta"], prev["model"], mid, fp, policy, scfg,
                prefiltered=(prev["meta"].get("skipped") == "prefilter_no_signal"
                             and _verified_prefilter(ledger, pid, mid)))
                and _valid_primary_history(prev["content"], prev["meta"], previous, mid, fp, policy, scfg)))
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            valid = False
        if not valid:
            _why(diagnostics, "assess_scope_invalid")
            hard_fail = incomplete = True
            break
        if prev and prev["meta"].get("technical_status") == "complete":
            verdicts[mid] = prev["content"].get("verdicts", {})
        elif jev_skip is not None:
            partial = prev["content"].get("verdicts", {}) if prev else {}
            try:
                if _partial_assessment(partial, scfg) != partial:
                    raise ValueError("semantic_partial_assessment_invalid")
            except (ValueError, TypeError):
                hard_fail = incomplete = True
                break
            record_skipped_assessment(mid, prev, jev_skip, partial)
            verdicts[mid] = partial
        else:
            if time.monotonic() > deadline - 5:
                incomplete = True
                break
            target_verdicts, flag = _assess_target(
                ledger, scfg, bundle, jev_client, pid, mid, fp, policy,
                prev, deadline, diagnostics=diagnostics)
            if target_verdicts is None:
                error = getattr(jev_client, "last_error", None)
                reason = semantic_audit.jev_skip_reason(error)
                if not hasattr(error, "partial_verdicts") or getattr(error, "partial_invalid", False):
                    reason = None
                if reason is None:
                    resource_wait |= flag == "resource"
                    retryable_failure |= flag == "retryable"
                    hard_fail |= flag == "hard"
                    incomplete = True
                    continue
                jev_skip = reason
                diagnostics["jev_skip_reason"] = reason
                partial = error.partial_verdicts
                jev_client = None
                record_skipped_assessment(mid, prev, jev_skip, partial)
                verdicts[mid] = partial
            else:
                verdicts[mid] = target_verdicts
        if scfg["summary_mode"] == "off" and scfg["loop_mode"] == "off":
            continue
        skip_state = {"reason": jev_skip}
        stage = _fact_stage(ledger, scfg, member, pid, mid, fp, policy,
                            jev_client, llm_fn, deadline, shadow_stats=shadow_stats, jev_skip=jev_skip,
                            activate_skip=activate_skip, skip_state=skip_state)
        if stage["outcome"] is not None:
            if fact_source == "canonical":
                # T18: a fact-stage failure still leaves a durable
                # diagnostic receipt — never an invisible stop
                doc = stage["v2_doc"]
                v4.diagnostic(
                    ledger, pid, mid, fp, policy,
                    "NEEDS_REVIEW" if stage["outcome"] == "hard_fail"
                    else "PENDING",
                    [{"code": f"fact_stage_{stage['outcome']}"}]
                    + list(stage.get("findings") or []),
                    doc_hash=(semantic_facts.doc_hash(doc) if doc else None))
            _why(diagnostics, f"fact_stage_{stage['outcome']}")
            if stage["outcome"] == "retryable":
                retryable_failure = True
            elif stage["outcome"] == "hard_fail":
                hard_fail = True
            incomplete = True
            break
        facts = stage["facts"]
        if stage["v2_doc"] is not None:
            v2_docs_by_target[mid] = stage["v2_doc"]
        facts_by_target[mid] = facts
        from semantic_assessment import evaluate_medication_events
        event_details = evaluate_medication_events(
            ledger, bundle, mid, facts, jev_client, scfg, deadline - 5,
            skip_unavailable=jev_skip is not None)
        if not event_details["complete"] and event_details["failure_reason"] == "technical" and jev_skip is None:
            reason = semantic_audit.jev_skip_reason(getattr(jev_client, "last_error", None))
            if reason is not None:
                jev_skip = reason
                diagnostics["jev_skip_reason"] = reason
                jev_client = None
                guard("medication_detail_skip")
                event_details = evaluate_medication_events(
                    ledger, bundle, mid, facts, None, scfg, deadline - 5, skip_unavailable=True)
        guard("medication_detail_result")
        detail_findings[mid] = [finding for finding in event_details["findings"]
                                if finding.get("code") != "medication_detail_unevaluated"]
        if (not event_details["complete"] and event_details["failure_reason"] != "invalid"
                and not (jev_skip is not None and event_details["failure_reason"] == "skipped")):
            incomplete = _why(
                diagnostics, f"medication_detail_{event_details['failure_reason']}")
            error = getattr(jev_client, "last_error", None)
            if error is not None and jev.failure_class(error) == "failed":
                hard_fail = True
            elif event_details["failure_reason"] != "deadline" and (
                    error is None or jev.failure_class(error) != "resource"):
                retryable_failure = True
            break
        if scfg["loop_mode"] != "off":
            guard("loop_candidates")
            update_loops(ledger, pid, bundle, {mid: facts}, None, scfg, deadline)
        if scfg["summary_mode"] != "off":
            previous = current_artifact(ledger.db, KIND_COVERAGE, mid, fp, policy)
            coverage = previous["content"] if previous else None
            if not coverage or not coverage.get("evaluated"):
                from semantic_audit import evaluate_source_fact_coverage
                if jev_skip is not None:
                    coverage = {"status": "SKIPPED", "evaluated": False, "choice": None,
                                "confidence": None, "failure_reason": jev_skip,
                                "findings": [{"code": "source_fact_coverage_unevaluated"}]}
                else:
                    coverage = evaluate_source_fact_coverage(
                        jev_client, member["body_original"], facts, deadline,
                        target_id=f"m{mid}", match_threshold=scfg["match_threshold"])
                    reason = semantic_audit.jev_skip_reason(getattr(jev_client, "last_error", None))
                    if not coverage["evaluated"] and reason is not None:
                        jev_skip = reason
                        diagnostics["jev_skip_reason"] = reason
                        jev_client = None
                        guard("coverage_skip")
                        coverage = {"status": "SKIPPED", "evaluated": False, "choice": None,
                                    "confidence": None, "failure_reason": reason,
                                    "findings": [{"code": "source_fact_coverage_unevaluated"}]}
                guard("coverage_result")
                # an identical unevaluated re-run is a durable wait, not new
                # information — record the outcome once (FIX-SD1)
                if previous is None or previous["content"] != coverage:
                    ledger.artifact_add(
                        KIND_COVERAGE, json.dumps(coverage, ensure_ascii=False),
                        project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                        meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy,
                              "technical_status": "complete" if coverage["evaluated"] else "pending"})
            coverage_by_target[mid] = ([] if coverage.get("status") == "SKIPPED"
                                       else coverage["findings"])
            if not coverage["evaluated"] and coverage.get("status") != "SKIPPED":
                incomplete = _why(diagnostics, f"coverage_{coverage.get('failure_reason')}")
                error = getattr(jev_client, "last_error", None)
                if error is not None and jev.failure_class(error) == "failed":
                    hard_fail = True
                elif (error is not None and jev.failure_class(error) == "retry"
                      or coverage.get("failure_reason") in ("model", "retry")):
                    retryable_failure = True
                break
    if incomplete:
        if hard_fail:
            return "failed"
        if retryable_failure:
            # deterministic failure (e.g. protocol_error, oversized
            # payload) — deferring forever would burn a Jev call every
            # tick on an input that can never pass; bounded retry ends
            # 'failed' where status_report can surface it
            return "retry"
        return "deferred"

    # summary + audit. A TERMINAL audit (PASS/NEEDS_REVIEW) for THIS
    # fingerprint is reused — its notification intent was already
    # committed atomically below. A PENDING one is NOT final: the
    # stored summary is re-audited so a mid-audit outage can never
    # wedge the thread on a stale PENDING marker (AT-058).
    results = {}
    held_empty_targets = set()
    for mid, facts in facts_by_target.items():
        semantic_transport.set_inference_target(mid, "summary")
        if scfg["summary_mode"] == "off":
            continue
        existing = current_artifact(ledger.db, KIND_SUMMARY, mid, fp, policy)
        prev_audit = current_artifact(ledger.db, KIND_AUDIT, mid, fp, policy)
        if existing and existing["meta"].get("publication_mode") != scfg["summary_mode"]:
            existing = None
            prev_audit = None
        prev_status = (prev_audit["meta"].get("audit_status")
                       if prev_audit else None)
        if existing is not None and prev_status == "PASS":
            import semantic_send_gate
            if not semantic_send_gate.passed_summary_current(
                    ledger.db, pid, mid, fp, policy, members[mid]["revision"],
                    scfg["summary_mode"], bundle=bundle, fact_source=fact_source):
                _why(diagnostics, "prior_pass_unverified")
                incomplete = resource_wait = True
                continue
        if existing is not None and prev_status in ("PASS",
                                                    "NEEDS_REVIEW"):
            results[mid] = {"summary": existing["content"],
                            "status": prev_status,
                            # stored findings keep v4.diagnostic's
                            # (status, doc_hash, findings) dedup intact
                            "findings": list(prev_audit["content"].get(
                                "findings") or []),
                            "repaired": False, "fresh": False}
            continue
        if time.monotonic() > deadline - 5:
            incomplete = True
            break
        recovered, empty_reserved = _empty_repair_candidate(
            ledger.db, pid, mid, fp, policy, members[mid]['revision'], scfg['summary_mode'], facts)
        if empty_reserved and recovered is None:
            _why(diagnostics, 'empty_repair_unverified')
            held_empty_targets.add(mid)
            incomplete = resource_wait = True
            continue
        if recovered is not None:
            summary, producer = recovered['content'], recovered['producer']
            summary_reason, summary_error, sent_error = None, {}, None
        else:
            summary, summary_reason, summary_error, sent_error, producer = _obtain_summary(
                ledger, scfg, bundle, llm_fn, guard, pid, mid, fp, policy,
                facts, verdicts, existing, deadline, diagnostics=diagnostics)
        if sent_error is not None:
            not_sent = sent_error
        if summary is None:
            _why(diagnostics, f"summary_{summary_reason}")
            if diagnostics.get("summary_validation_error") is None:
                diagnostics["summary_validation_error"] = summary_error.get("code")
            # same dedup as the assess wait-state above: a persistent
            # local-LLM outage defers without stacking identical
            # PENDING rows on every drain
            if summary_reason == "model":
                retryable_failure = True
            prev_a = current_artifact(ledger.db, KIND_AUDIT, mid, fp, policy)
            if not (prev_a
                    and prev_a["meta"].get("technical_status")
                    == "pending"
                    and prev_a["content"].get("status") == "PENDING"):
                ledger.artifact_add(
                    KIND_AUDIT,
                    json.dumps({"status": "PENDING",
                                "findings":
                                    [{"code": "summary_unavailable"}],
                                "target_message_id": mid},
                               ensure_ascii=False),
                    project_id=pid, message_id=mid, model=semantic_transport.llm_model(),
                    meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
                          "technical_status": "pending"})
            incomplete = True
            if not_sent is not None:
                break         # the model is held — later targets too
            continue
        if fact_source == "canonical":
            v4.record_stage(ledger, pid, mid, fp, policy, "s6_summary",
                            "reused" if existing is not None
                            else "done")
        # Preserve the pre-audit output for the fixed-bundle comparison (§24.1).
        # It is never eligible for publication or automatic adoption.
        if existing is None and current_artifact(ledger.db, KIND_CANDIDATE, mid, fp, policy) is None:
            guard("candidate_snapshot")
            ledger.artifact_add(
                KIND_CANDIDATE, json.dumps(summary, ensure_ascii=False),
                project_id=pid, message_id=mid, model=semantic_transport.llm_model(),
                meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy,
                      "schema": SCHEMA_VERSION, "stage": "pre_audit",
                      "publication_mode": scfg["summary_mode"],
                      "target_revision": members[mid]["revision"],
                      "provider_provenance": _summary_provenance(producer, fp, policy,
                                                                  members[mid]["revision"], summary)})
        # Reserve before dispatch: a crash cannot refund the repair (INV-10).
        repaired = bool(current_artifact(ledger.db, KIND_REPAIR, mid, fp)
                        or (prev_audit
                            and prev_audit["meta"].get("repair_count", 0) >= 1))
        summary["_facts"] = facts   # evidence context for the Jev audit
        audit_client = jev_client
        req0 = audit_client.requests_made if audit_client else 0
        findings = []
        status = "PENDING"
        claim_input_binding = None
        jev_outcome = "unknown"
        empty_unverified = False
        empty_repair = recovered is not None
        empty_prior = recovered['meta']['prior'] if recovered else None
        empty_repair_id = recovered['meta']['repair_artifact_id'] if recovered else None
        for _attempt in range(2):      # initial + at most one repair
            if summary.get("_input_oversize") is True:
                findings = audit_code(bundle, facts, summary)
                status = "NEEDS_REVIEW"
                break
            if summary.get("claims") == []:
                _why(diagnostics, "audit_empty_unverified")
                if repaired or summary.get("_input_oversize"):
                    held_empty_targets.add(mid)
                    empty_unverified = incomplete = resource_wait = True
                    break
                # A malformed or otherwise unbound prior audit is unknown;
                # do not let a fresh empty candidate replace that evidence.
                prior_row = ledger.db.execute(
                    "SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND message_id=? "
                    "ORDER BY artifact_id DESC LIMIT 1", (KIND_AUDIT, pid, mid)).fetchone()
                if prior_row is not None:
                    try:
                        prior_bound = (prev_audit is not None
                                       and strict_json(prior_row['content']) == prev_audit['content']
                                       and strict_json(prior_row['meta']) == prev_audit['meta'])
                    except (ValueError, TypeError, RecursionError):
                        prior_bound = False
                    if not prior_bound:
                        held_empty_targets.add(mid)
                        empty_unverified = incomplete = resource_wait = True
                        break
                empty_repair = True
                empty_prior = _empty_repair_prior(ledger.db, pid, mid)
                code_f = [{"code": "summary_empty_unverified"}]
                jev_f = []
                findings = code_f
                status = "REPAIR_REQUIRED"
                claim_input_binding = _claim_input_binding(
                    pid, mid, fp, policy, members[mid]["revision"], summary, facts)
            else:
                if not isinstance(summary.get("claims"), list):
                    _why(diagnostics, "audit_claims_invalid")
                    if empty_repair:
                        held_empty_targets.add(mid)
                        empty_unverified = incomplete = resource_wait = True
                    else:
                        hard_fail = incomplete = True
                    break
                code_f = (audit_code(bundle, facts, summary) + coverage_by_target.get(mid, [])
                          + detail_findings.get(mid, []))
                claim_input_binding = _claim_input_binding(pid, mid, fp, policy, members[mid]["revision"], summary, facts)
                before_audit = getattr(jev_client, "requests_made", None)
                if empty_repair and jev_client is not None:
                    _store_empty_repair(ledger, pid, mid, fp, policy, members[mid]['revision'],
                        scfg['summary_mode'], facts, summary, producer, empty_repair_id, empty_prior,
                        guard, audit_state='inflight')
                try:
                    jev_f, evaluated = semantic_audit.audit_claims(
                        jev_client, bundle, summary, deadline,
                        scfg["match_threshold"], scfg["max_questions_per_request"])
                except (runtime.LLMNotSent, runtime.RuntimeBudget) as error:
                    # Jev cutoff/budget: earlier targets commit below. A repair
                    # already applied this pass is kept as a PENDING result, so
                    # the next pass re-audits the repaired text (INV-10: the
                    # one repair is spent, never refunded).
                    not_sent = error
                    incomplete = True
                    if empty_repair and jev_client is not None:
                        partial = summary.get('_claim_audit') or {}
                        _store_empty_repair(ledger, pid, mid, fp, policy, members[mid]['revision'],
                            scfg['summary_mode'], facts, summary, producer, empty_repair_id, empty_prior,
                            guard, partial_answers=partial, audit_state='held' if partial else 'ready')
                    if _attempt and not empty_repair:
                        summary["audit_status"] = "PENDING"
                        if fact_source == 'canonical' and producer.get('repair_evidence'):
                            producer = {**producer, 'repair_render_phase': 'pending'}
                        results[mid] = {
                            "summary": summary, "status": "PENDING",
                            "findings": code_f + [{"code": "support_unevaluated"}],
                            "repaired": True, "fresh": True,
                            "producer": producer, "jev_outcome": "unknown",
                            "claim_input_binding": claim_input_binding,
                            "jev_requests": (jev_client.requests_made - req0)
                            if jev_client else 0}
                    break
                if empty_repair and jev_client is not None:
                    partial = summary.get('_claim_audit') or {} if not evaluated else {}
                    if not evaluated:
                        adverse = [item for item in jev_f if not isinstance(item, dict)
                                   or item.get('code') != 'support_unevaluated']
                        if adverse:
                            partial = {'answers': partial, 'findings': adverse}
                    _store_empty_repair(ledger, pid, mid, fp, policy, members[mid]['revision'],
                        scfg['summary_mode'], facts, summary, producer, empty_repair_id, empty_prior,
                        guard, partial_answers=partial, audit_state='held' if partial else 'ready')
                if not evaluated and jev_skip is None:
                    skip = semantic_audit.jev_skip_reason(getattr(jev_client, "last_error", None))
                    if skip is not None:
                        jev_skip = skip
                        diagnostics["jev_skip_reason"] = skip
                        jev_client = None
                        guard("claim_audit_skip")
                after_audit = getattr(audit_client, "requests_made", None)
                sent = (type(before_audit) is int and type(after_audit) is int
                        and after_audit > before_audit)
                observed = summary.get("_claim_audit", {})
                claim_ids = [claim.get("claim_id") for claim in summary["claims"] if isinstance(claim, dict)]
                valid_ids = (len(claim_ids) == len(summary["claims"])
                             and all(isinstance(claim_id, str) for claim_id in claim_ids))
                if (sent and evaluated and valid_ids and claim_ids and isinstance(observed, dict)
                        and set(observed) == set(claim_ids)):
                    jev_outcome = "done"
                elif sent and isinstance(observed, dict) and observed:
                    jev_outcome = "partial"
                elif jev_skip is not None and prev_audit is None and not sent and not observed:
                    jev_outcome = "skipped"
                else:
                    jev_outcome = "unknown"
                if not evaluated and jev_skip is None:
                    reason = summary.pop("_claim_audit_reason", None)
                    if reason in ("claims_empty", "claims_invalid", "client_unavailable", "options_invalid", "response_invalid"):
                        _why(diagnostics, f"audit_{reason}")
                        # A saved empty/invalid claim list is reused unchanged on each retry.
                        hard_fail |= reason in ("claims_empty", "claims_invalid")
                        resource_wait |= reason == "client_unavailable"
                        retryable_failure |= reason == "response_invalid"
                    else:
                        flag = _jev_fail_flag(getattr(jev_client, "last_error", None))
                        _why(diagnostics, f"audit_jev_{flag}" if flag else "audit_unevaluated")
                        resource_wait |= flag == "resource"
                        retryable_failure |= flag == "retryable"
                        hard_fail |= flag == "hard"
                prior_blocked = False
                if not evaluated and jev_skip is not None:
                    known, unbound = _prior_pending_findings(ledger.db, pid, mid, fp, policy,
                                                             members[mid]["revision"], scfg["summary_mode"], repaired, prev_audit,
                                                             summary, claim_input_binding)
                    prior_blocked = bool(known) or unbound or _attempt > 0
                    if known:
                        jev_f.extend(f for f in known if f not in jev_f)
                    if unbound or _attempt > 0:
                        jev_f.append({"code": "prior_audit_unverified"})
                    if any(f.get("code") != "support_unevaluated" for f in jev_f):
                        prior_blocked = True
                findings = code_f + jev_f
                status = (semantic_audit.optional_audit_status(code_f, jev_f, jev_skip, repaired)
                          if not evaluated and jev_skip is not None
                          else audit_status_for(code_f, jev_f, evaluated, repaired))
                if prior_blocked:
                    # A skipped re-audit cannot clear an earlier negative result
                    # or certify a repair that has not itself been re-evaluated.
                    status = "PENDING"
                    resource_wait = True
            if status == "REPAIR_REQUIRED" and not repaired:
                guard("repair_reservation")
                # Preserve the rejected original and its exact audit before
                # dispatch: a crash or empty repair must not erase a finding.
                repair_pending = {"summary": {**summary, "audit_status": "PENDING"},
                                  "status": "PENDING", "findings": findings, "repaired": True,
                                  "producer": producer, "jev_outcome": jev_outcome,
                                  "claim_input_binding": claim_input_binding,
                                  "jev_requests": (audit_client.requests_made - req0) if audit_client else 0}
                with ledger.db:
                    if not ledger.db.in_transaction:
                        ledger.db.execute('BEGIN IMMEDIATE')
                    guard("repair_reservation_tx")
                    if not empty_repair:
                        _write_result(ledger, pid, mid, repair_pending, fp, members,
                                      "PENDING", policy, scfg["summary_mode"])
                    repair_id = ledger.artifact_add_tx(
                        KIND_REPAIR, json.dumps({"target_message_id": mid}),
                        project_id=pid, message_id=mid, model=semantic_transport.llm_model(),
                        meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy, "repair_count": 1,
                              **({'empty_repair': True} if empty_repair else {})})
                repaired = True
                if empty_repair:
                    empty_repair_id = repair_id
                repair_answers = []

                def summary_repair_llm(prompt):
                    previous_requests = set(semantic_transport.inference_requests())
                    value = llm_fn(prompt)
                    receipt = semantic_transport.inference_receipt()
                    contract = semantic_transport.inference_contract()
                    if (not empty_repair and isinstance(value, str) and receipt is not None and contract is not None
                            and receipt.get('reservation_id') not in previous_requests):
                        import bounded_http
                        try:
                            if len(value.encode('utf-8')) <= bounded_http.MAX_RESPONSE_BYTES:
                                repair_answers.append({'text': value,
                                    'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                                    'reservation_id': receipt.get('reservation_id'), 'contract': contract,
                                    'unit_key': receipt.get('unit_key'), 'owner': receipt.get('owner')})
                        except UnicodeError:
                            pass
                    return value

                try:
                    with (semantic_transport.quality_repair() if not empty_repair else nullcontext()):
                        summary2, repair_producer = _summarize_observed(
                            summary_repair_llm, bundle, mid, facts, verdicts.get(mid, {}),
                            feedback=[f.get("code", "") + " " +
                                      f.get("statement", f.get("claim", ""))
                                      for f in code_f + jev_f])
                except runtime.RuntimeGuardError as error:
                    if not runtime.llm_request_was_dispatched(error):
                        # A proven pre-dispatch stop did not spend the one
                        # repair. A crash or post-dispatch stop still does.
                        with ledger.db:
                            ledger.db.execute(
                                "DELETE FROM artifacts WHERE artifact_id=?",
                                (repair_id,))
                            if not empty_repair:
                                repair_pending["repaired"] = False
                                _write_result(ledger, pid, mid, repair_pending, fp, members,
                                              "PENDING", policy, scfg["summary_mode"])
                    if isinstance(error, (runtime.LLMNotSent,
                                          runtime.RuntimeBudget)):
                        not_sent = error
                        incomplete = True
                        break
                    raise
                if summary2 is not None and summary2.get("_input_oversize") is not True:
                    if not empty_repair and repair_producer.get('providers') in (['openai'], ['anthropic']):
                        fact_kind = KIND_FACTS_V2 if fact_source == 'canonical' else KIND_FACTS
                        refs = {}
                        for key, kind in (('facts_id', fact_kind), ('assessment_id', KIND_ASSESS),
                                          ('original_summary_id', KIND_SUMMARY), ('audit_id', KIND_AUDIT)):
                            row = ledger.db.execute("SELECT artifact_id FROM artifacts WHERE kind=? AND project_id=? "
                                "AND message_id=? AND artifact_id<? AND CASE WHEN json_valid(meta) THEN "
                                "json_extract(meta,'$.fingerprint')=? AND json_extract(meta,'$.policy_fingerprint')=? "
                                "AND (?!=? OR json_extract(meta,'$.fact_id') IS NULL) ELSE 0 END "
                                "ORDER BY artifact_id DESC LIMIT 1",
                                (kind, pid, mid, repair_id, fp, policy, kind, KIND_ASSESS)).fetchone()
                            if row is not None:
                                refs[key] = row['artifact_id']
                        if len(repair_answers) == 1 and len(refs) == 4:
                            repair_evidence = {'version': 1, 'project_id': pid, 'message_id': mid,
                                'fact_kind': fact_kind, **refs, 'repair_id': repair_id, 'answer': repair_answers[0]}
                            if _summary_repair_evidence(ledger.db, bundle, summary2, repair_evidence,
                                                        fp, policy, members[mid]['revision'], render_phase='pre_render'):
                                guard('summary_repair_evidence')
                                proof_id = ledger.artifact_add(
                                    KIND_REPAIR, json.dumps({'target_message_id': mid,
                                        '__mcs_internal_proof_v1': repair_evidence}, ensure_ascii=False),
                                    project_id=pid, message_id=mid, model=semantic_transport.llm_model(),
                                    meta={VERSION_KEY: CURRENT_VERSION, 'fingerprint': fp,
                                          'policy_fingerprint': policy, 'repair_count': 1})
                                repair_producer['repair_evidence'] = {
                                    'artifact_id': proof_id, 'hash': mcs_util.payload_hash(repair_evidence)}
                            else:
                                repair_producer = {'providers': [], 'complete': False}
                        else:
                            repair_producer = {'providers': [], 'complete': False}
                    summary = summary2
                    producer = repair_producer
                    if empty_repair and isinstance(summary.get('claims'), list) and summary['claims']:
                        _store_empty_repair(ledger, pid, mid, fp, policy, members[mid]['revision'],
                            scfg['summary_mode'], facts, summary, producer, repair_id, empty_prior, guard)
                    summary["_facts"] = facts
                    continue
                if empty_repair:
                    held_empty_targets.add(mid)
                    empty_unverified = incomplete = resource_wait = True
                    break
                status = "NEEDS_REVIEW"
                findings.append({"code": "repair_unavailable"})
            break
        if not_sent is not None:
            break
        if empty_repair and status == "PENDING":
            held_empty_targets.add(mid)
            empty_unverified = incomplete = resource_wait = True
        if empty_unverified:
            continue
        v2_doc = v2_docs_by_target.get(mid)
        if fact_source == "canonical" and v2_doc is not None:
            # T11: the adjudicated contract drives a mandatory render
            # layer — verified facts stay visible and non-terminal
            # obligations are disclosed as limitations even if the
            # model summary dropped them. Per-target doc: the LAST
            # member's doc must never leak into another target's
            # summary (C02).
            status = semantic_render.apply_mandatory_render(summary, findings, status,
                                             v2_doc)
            if producer.get('repair_evidence'):
                producer = {**producer, 'repair_render_phase': 'rendered'}
        summary["audit_status"] = status
        if fact_source == "canonical":
            v4.record_stage(ledger, pid, mid, fp, policy,
                            "s7_summary_audit", status)
        results[mid] = {"summary": summary, "status": status,
                        "findings": findings, "repaired": repaired,
                        "fresh": True,
                        "producer": producer, "jev_outcome": jev_outcome,
                        "claim_input_binding": claim_input_binding,
                        "jev_skip_reason": jev_skip if status == "SKIPPED" else None,
                        "jev_requests": (audit_client.requests_made - req0)
                        if audit_client else 0}
    if incomplete:
        if results:
            try:
                guard("partial_promote")
            except runtime.RuntimeStale:
                # generation moved — budget/off re-raise so _process_job
                # maps them like every other boundary (bounded retry /
                # pause), never as a silent uncounted stale
                return "stale"
            # commit each completed target's outcome NOW — dropping it
            # would re-spend Jev calls on an identical input next run
            # AND silently reset the one-shot repair budget, whose
            # counter lives on these artifacts (INV-10, §18.2)
            with ledger.db:
                if not ledger.db.in_transaction:
                    ledger.db.execute('BEGIN IMMEDIATE')
                guard('partial_publish_tx')
                only_empty_held = (bool(held_empty_targets)
                                   and set(results) | held_empty_targets == set(facts_by_target)
                                   and all(r["status"] in ("PASS", "SKIPPED") for r in results.values())
                                   and not hard_fail and not retryable_failure and not_sent is None)
                for mid, r in results.items():
                    if r["fresh"]:
                        _write_result(ledger, pid, mid, r, fp, members,
                                      r["status"], policy, scfg["summary_mode"])
                    v4.publish_target_tx(ledger, pid, mid, fp, policy, members,
                                v2_docs_by_target, r["status"],
                                r.get("findings") or [],
                                fact_source)
                    reusable = (not r["fresh"] and r["status"] == "PASS"
                                and _reused_pass_current(ledger.db, pid, mid, fp, policy,
                                                         members[mid]["revision"], scfg["summary_mode"],
                                                         r, facts_by_target[mid]))
                    if (only_empty_held and fact_source != "canonical" and (r["fresh"] or reusable)):
                        import semantic_intake_hold
                        semantic_notice.record_notice_plan_tx(
                            ledger, pid, root, mid, fp, policy, r["summary"], r["status"],
                            members[mid], all_targets, mode=scfg["mode"],
                            summary_mode=scfg["summary_mode"],
                            notification_free=(pl.get("notification_free") or not
                                semantic_intake_hold.notification_allowed(ledger, pl, pid, mid)),
                            origin=pl.get("origin"), content_quality=bundle["content_quality"])
        if hard_fail:
            return "failed"
        if retryable_failure:
            return "retry"
        if not_sent is not None:
            raise not_sent    # _process_job maps it (Jev-spend backoff)
        return "deferred"

    # generation guard: the bundle, config, and complete job identity must
    # still be current before promotion.  The same guard also runs before
    # every Jev/local-model call through the wrappers above.
    stale = False
    try:
        guard("promote")
    except runtime.RuntimeStale:
        # only a generation change labels the results STALE; an
        # exhausted budget or a pause/OFF propagates to _process_job
        # (retry/deferred/stale) and writes nothing after the boundary
        stale = True
    pending = not stale and any(r["status"] == "PENDING"
                                for r in results.values())
    loops_done = True
    loops_short = False
    if not stale and scfg["loop_mode"] != "off" and jev_skip is not None:
        guard("loop_skip")
        prior = current_artifact(ledger.db, "semantic_loop_skipped", root, fp, policy)
        if prior is None or prior["content"].get("reason") != jev_skip:
            ledger.artifact_add(
                "semantic_loop_skipped", json.dumps({"status": "SKIPPED", "reason": jev_skip}),
                project_id=pid, message_id=root,
                meta={VERSION_KEY: CURRENT_VERSION, "fingerprint": fp, "policy_fingerprint": policy,
                      "schema": SCHEMA_VERSION})
    if not stale and scfg["loop_mode"] != "off" and jev_skip is None:
        # loop candidates/relation events for the LIVE generation —
        # kept OUT of the commit tx because matching makes Jev calls
        # (no network inside a DB transaction, §6.2); replay-safe via
        # candidate_fp and pair dedup.
        loops_done, loops_short, resource, retryable, hard = _promote_loops(
            ledger, scfg, pl, pid, bundle, facts_by_target, jev_client,
            deadline, diagnostics=diagnostics)
        resource_wait |= resource
        retryable_failure |= retryable
        hard_fail |= hard
    # notification eligibility is derived from the stored origin event,
    # not from this job's seed provenance (INV-20, §20.3) — evaluated
    # per target below: a covering new_messages intent must exist for
    # the message a notice reports on.
    with ledger.db:
        if not ledger.db.in_transaction:
            ledger.db.execute('BEGIN IMMEDIATE')
        if not stale:
            guard('publish_tx')
        for mid, r in results.items():
            final_status = "STALE" if stale else r["status"]
            if r["fresh"]:
                _write_result(ledger, pid, mid, r, fp, members,
                              final_status, policy, scfg["summary_mode"])
            # T18 S8: v4 publication/diagnostic commits in the SAME
            # transaction as the status receipt — a crash cannot leave
            # a PASS result without its read-model row, nor a non-PASS
            # without its diagnostic. STALE is a status too: the
            # generation receipt persists even when the source moved
            v4.publish_target_tx(ledger, pid, mid, fp, policy, members,
                        v2_docs_by_target, final_status,
                        r.get("findings") or [], fact_source)
            if stale:
                continue
            import semantic_intake_hold
            semantic_notice.record_notice_plan_tx(
                ledger, pid, root, mid, fp, policy, r["summary"], final_status,
                members[mid], all_targets, mode=scfg["mode"],
                summary_mode=scfg["summary_mode"],
                notification_free=(pl.get("notification_free") or not
                    semantic_intake_hold.notification_allowed(ledger, pl, pid, mid)),
                origin=pl.get("origin"), content_quality=bundle["content_quality"])
        if not stale and not pending and loops_done:
            if (scfg['summary_mode'] == 'off' and scfg['loop_mode'] != 'off'
                    and not hard_fail and not retryable_failure and not resource_wait
                    and isinstance(targets, list) and targets and len(targets) == len(set(targets))
                    and set(targets) == set(facts_by_target) and not any(detail_findings.values())
                    and (jev_skip is not None or jev_client is not None)):
                try:
                    manifest = _loop_completion_manifest(ledger.db, pid, bundle, targets, scfg,
                        facts_by_target=facts_by_target, skipped=jev_skip is not None, deadline=deadline)
                except sqlite3.Error as error:
                    raise runtime.RuntimeBudgetShort('loop_completion_read') from error
                except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
                    manifest = None
                skip_ref = (_loop_skip_ref(ledger.db, pid, root, fp, policy, jev_skip)
                            if jev_skip is not None else None)
                if jev_skip is not None and skip_ref is None:
                    manifest = None
                guard('loop_completion')
                if manifest is not None:
                    status = 'SKIPPED' if jev_skip is not None else 'DONE'
                    content = {'status': status, 'target_ids': sorted(targets),
                               'loop_mode': scfg['loop_mode'], 'semantic_mode': scfg['mode'], 'manifest': manifest,
                               'skip_reason': jev_skip if status == 'SKIPPED' else None, 'skip_ref': skip_ref}
                    for mid in targets:
                        ledger.artifact_add_tx(KIND_LOOP_COMPLETION, json.dumps(content, ensure_ascii=False),
                            project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                            meta={VERSION_KEY: CURRENT_VERSION, 'fingerprint': fp,
                                  'policy_fingerprint': policy, 'schema': SCHEMA_VERSION,
                                  'target_revision': members[mid]['revision'],
                                  'execution_generation': runtime.execution_generation(scfg),
                                  'loop_status': status, 'skip_reason': content['skip_reason'],
                                  'content_hash': mcs_util.payload_hash(content)})
            if not runtime.transition_tx(ledger, token, "done"):
                raise runtime.RuntimeStale("promote")
    if stale:
        return "stale"
    if hard_fail:
        return "failed"
    if loops_short:
        # same mapping as a cutoff raised anywhere else in the pass
        _why(diagnostics, "budget_short")
        return "deferred_short"
    if pending:
        # evaluation incomplete (e.g. Jev outage mid-audit) — the job
        # must not sit 'done' on an unfinished audit; consume a bounded
        # retry attempt so a persistent outage eventually fails rather
        # than busy-loops (AT-058)
        _why(diagnostics, "audit_pending")
        return "deferred" if resource_wait and not retryable_failure \
            else "retry"
    if not loops_done:
        # relation pass truncated by the pair budget/deadline — the
        # results committed above stand; the job re-runs to evaluate
        # the remaining (candidate, target) pairs (§17.2 持ち越し)
        _why(diagnostics, "loops_incomplete")
        return "retry" if retryable_failure else "deferred"
    return "done"


NOT_SENT_BACKOFF_S = 3600
# LLMNotSent reasons that observe the endpoint itself down (refused
# connection / 503 while the model loads) — never an admission verdict
# or the long-output budget gate, which say nothing about its health.
_LLM_DOWN_REASONS = frozenset({"llm_unreachable", "llm_unavailable"})


def _process_job(ledger, scfg, job, jev_client, llm_fn, deadline,
                 cfg_path: str | None = None,
                 config_generation: str | None = None,
                 reserve_fn=None, lane: str | None = None, *,
                       jev_transport=None, jev_timing=None, cloud_transport=None, llm_timing=None,
                       cloud_only=False,
                 diagnostics: dict, shadow_stats: dict | None = None,
                 jev_skip: str | None = None) -> str:
    """Preserve stale workers while bounding timeouts after dispatched work."""
    requests_before = jev_client.requests_made if jev_client is not None else 0
    llm_started = False
    local_llm_fn = llm_fn

    @wraps(llm_fn)
    def tracked_llm(*args, **kwargs):
        nonlocal llm_started
        llm_started = True
        return llm_fn(*args, **kwargs)

    def jev_sent():
        return (jev_client is not None
                and jev_client.requests_made > requests_before)

    try:
        with ExitStack() as stack:
            stack.enter_context(semantic_transport.inference_targets(runtime.execution_generation(scfg)))
            if cloud_transport is not None and lane == "backlog":
                token = runtime.JobToken.from_row(job)
                payload = runtime.parse_payload(job)
                targets = payload.get("targets", [job["message_id"]])

                def source():
                    bundle = semantic_store.thread_bundle(
                        ledger.db, job["project_id"], job["message_id"], targets)
                    return bundle["source_fingerprint"] if bundle else None

                fingerprint = source()
                def fallback_current():
                    # Never join a local result to a previously persisted cloud
                    # prefix, or to a chunk whose producer cannot be proven.
                    import semantic_extraction
                    rows = ledger.db.execute("SELECT * FROM artifacts WHERE project_id=? AND kind IN (?,?) "
                        "AND (NOT json_valid(meta) OR json_extract(meta,'$.source_fingerprint')=?)",
                        (job['project_id'], semantic_extraction.KIND_CHUNK,
                         semantic_extraction.KIND_CHUNK_V2, fingerprint)).fetchall()
                    return all(semantic_extraction._cached_provider(
                        ledger.db, row, semantic_transport.llm_model()) == 'local' for row in rows)

                def current():
                    try:
                        runtime.guard(ledger, token, deadline=deadline,
                            expected_config_generation=config_generation,
                            expected_execution_generation=runtime.execution_generation(scfg),
                            expected_mode=scfg["mode"], cfg_path=cfg_path,
                            load_cfg=load_config, parse_cfg=runtime_config,
                            source_fingerprint=fingerprint, current_source=source, stage="cloud")
                    except runtime.RuntimeGuardError:
                        return False
                    return True

                cloud = stack.enter_context(semantic_transport.backlog_inference(
                    ledger, load_config() if cfg_path is None else load_config(cfg_path),
                    job, runtime.job_deadline(scfg, deadline), current, cloud_transport,
                    local_fallback=local_llm_fn if not cloud_only else None,
                    fallback_current=fallback_current if not cloud_only else None))
                if cloud_only and cloud is None:
                    raise runtime.LLMNotSent("cloud_scope_or_budget")
                if cloud is not None:
                    def cloud_call(prompt, timeout=None):
                        started = time.perf_counter()
                        before = llm_timing["s"] if llm_timing is not None else 0.0
                        try:
                            return cloud(prompt, timeout=timeout)
                        finally:
                            if llm_timing is not None:
                                elapsed = time.perf_counter() - started
                                # A fallback can already have charged this same accumulator.
                                llm_timing["s"] += max(0.0, elapsed - (llm_timing["s"] - before))
                    llm_fn = cloud_call
            status = _process_job_inner(
                ledger, scfg, job, jev_client, tracked_llm, deadline,
                cfg_path=cfg_path, config_generation=config_generation,
                reserve_fn=reserve_fn, lane=lane, diagnostics=diagnostics, shadow_stats=shadow_stats,
                jev_transport=jev_transport, jev_timing=jev_timing, jev_skip=jev_skip)
        from extraction_refresh import refresh_extraction_cards
        if semantic_store.refresh_cards_allowed(ledger.db, job['project_id'], job['message_id']):
            refresh_extraction_cards(ledger, job["project_id"], job["message_id"])
        return status
    except runtime.RuntimeOff as error:
        _why(diagnostics, 'permission_refused' if error.stage.endswith(':permission') else 'off_or_scope')
        return "stale"
    except runtime.RuntimeStale as error:
        _why(diagnostics, 'source_changed' if error.stage.endswith(':source') else 'generation_changed')
        return "stale"
    except runtime.LLMNotSent as e:
        diagnostics["not_sent"] = getattr(e, "stage", None)
        if diagnostics["not_sent"] == "llm_turn_busy":
            # the other lane held the GPU turn: the endpoint is healthy
            # and the next pass normally gets it, so no hourly backoff
            return "deferred"
        # admission hold / refused connection: the LLM request never
        # left, so no attempt is consumed — but Jev calls this pass
        # already made (e.g. the per-chunk preflight) are real spend:
        # back off hourly so a held LLM cannot re-spend the Jev budget
        # every minute
        return "deferred_backoff" if jev_sent() else "deferred"
    except runtime.RuntimeBudgetShort:
        # earlier calls of this pass completed and persisted; the next
        # one could not start in the remaining budget — resume next
        # tick from the durable stages without charging an attempt.
        # Distinct from "deferred" so run_due stops the pass: the
        # remaining budget cannot fit another long generation either
        _why(diagnostics, "budget_short")
        return "deferred_short"
    except runtime.RuntimeBudget as e:
        _why(diagnostics, f"budget_{e.stage}")
        return "retry" if jev_sent() or llm_started else "deferred"




def _jev_error_brief(error) -> dict | None:
    """Kind/class of a JevError for run-record diagnostics (no bodies)."""
    if error is None:
        return None
    return {"kind": str(getattr(error, "kind", "") or "")[:40],
            # JevError.detail is a code (never a response body) — the
            # protocol_error variant is only diagnosable from it
            "detail": str(getattr(error, "detail", "") or "")[:60],
            "status": getattr(error, "status", 0) or 0,
            "class": jev.failure_class(error)}


# guard stages raised before a local-model request leaves (runtime)

# Artifact kinds a semantic pass persists as durable stage results.
# Scoped (kind + project) so a concurrent drainer's extract_llm rows or
# another project's work never read as this job's progress (2026-09-30:
# a job looped 19 passes 'progressing' on foreign writes).
_STAGE_KINDS = (KIND_FACT_PROJ, v4.KIND_V4_STAGE, KIND_LOOP,
                KIND_LOOP_EVENT, KIND_PLAN)


def _last_stage_artifact(ledger, project_id) -> int:
    """Highest id of a semantic stage artifact for this project (Jev
    usage receipts and drain bookkeeping excluded) — a pass that raised
    it made durable progress."""
    ph = ",".join("?" * len(_STAGE_KINDS))
    return ledger.db.execute(
        "SELECT COALESCE(MAX(artifact_id),0) FROM artifacts "
        # GLOB, not LIKE: a case-insensitive LIKE cannot seek
        # idx_artifacts_lookup, and its OR branch forced a full
        # artifacts scan for every job, twice per pass
        "WHERE project_id=? AND (kind GLOB 'semantic_*' "
        f"OR kind IN ({ph})) AND kind NOT IN (?,?,?)",
        (project_id, *_STAGE_KINDS, KIND_USAGE, KIND_DRAIN_RUN,
         SCHED_KIND)).fetchone()[0]


def _timed_llm(fn):
    """Wrap an injected llm_fn so wall time accumulates without
    changing its signature contract — ``timeout`` is forwarded only
    when the inner callable accepts it (``llm_call`` inspects the
    signature the same way)."""
    acc = {"s": 0.0}
    if runtime.accepts_timeout(fn):
        def timed(prompt, timeout=None):
            started = time.perf_counter()
            try:
                return fn(prompt, timeout=timeout)
            finally:
                acc["s"] += time.perf_counter() - started
    else:
        def timed(prompt):
            started = time.perf_counter()
            try:
                return fn(prompt)
            finally:
                acc["s"] += time.perf_counter() - started
    return timed, acc


def _sched_state(ledger) -> dict:
    """Persisted fair-schedule accounting — one fetch_jobs row
    (kind='semantic_sched', project_id/message_id 0, state='done' so no
    drain ever claims it) keeps the cohort share and progress counters
    restart-safe instead of living in process memory (T14)."""
    row = ledger.db.execute(
        "SELECT job_id FROM fetch_jobs WHERE kind=? "
        "AND project_id=0 AND message_id=0", (SCHED_KIND,)).fetchone()
    return (loads_dict(_bounded_job_payload(ledger.db, row['job_id'],
                                            _REPROCESS_JOB_MAX_BYTES)) or {}) if row else {}


def _sched_write(ledger, state: dict, *, commit=True) -> None:
    now = time.time()
    payload = json.dumps(state, sort_keys=True)
    if len(payload.encode('utf-8')) > _REPROCESS_JOB_MAX_BYTES:
        raise ValueError('semantic_scheduler_invalid')
    ledger.db.execute("""
      INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,
        payload,state,next_try,created_at,updated_at)
      VALUES(?,0,0,NULL,?,'done',0,?,?)
      ON CONFLICT(kind,project_id,message_id) DO UPDATE SET
        payload=excluded.payload,updated_at=excluded.updated_at
    """, (SCHED_KIND, payload, now, now))
    if commit:
        ledger.db.commit()


@contextmanager
def _sched_update(ledger):
    """Serialize a short scheduler read/modify/write against concurrent owners."""
    if ledger.db.in_transaction:
        raise ValueError('semantic_scheduler_transaction_active')
    ledger.db.execute('BEGIN IMMEDIATE')
    with ledger.db:
        state, _ = _reservation_sched(ledger)
        yield state
        _sched_write(ledger, state, commit=False)


_REPROCESS_JOB_MAX_BYTES = 1024 * 1024


def _reprocess_job_payload(ledger, row, project_id, root):
    """Read a bounded, supported job without changing uncertain evidence."""
    raw = row['payload']
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > _REPROCESS_JOB_MAX_BYTES:
        return None
    try:
        value = strict_json(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or read_version(value) not in (CURRENT_VERSION, LEGACY_VERSION):
        return None
    stack = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > 128 or (type(node) is int and not -(2**63) <= node < 2**63):
            return None
        if isinstance(node, dict):
            stack.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)
    if 'targets' not in value:
        return value  # Older jobs use their root as the default target.
    targets = value['targets']
    if (not isinstance(targets, list) or not targets
            or any(type(mid) is not int or not 0 < mid < 2**63 for mid in targets)):
        return None
    if ledger.db.execute(
            'SELECT 1 FROM json_each(?) target WHERE NOT EXISTS '
            '(SELECT 1 FROM messages WHERE project_id=? AND message_id=target.value '
            'AND (message_id=? OR parent_id=?)) LIMIT 1',
            (json.dumps(targets), project_id, root, root)).fetchone():
        return None
    return value


def _valid_detail_assessment(content, meta, mid):
    import semantic_assessment
    return (meta.get('schema') == semantic_assessment.ASSESSMENT_SCHEMA_VERSION
            and content.get('schema') == semantic_assessment.ASSESSMENT_SCHEMA_VERSION
            and meta.get('technical_status') == 'complete'
            and isinstance(meta.get('fact_id'), str) and isinstance(meta.get('dimension'), str)
            and content.get('fact_id') == meta['fact_id'] and content.get('dimension') == meta['dimension']
            and type(content.get('target_message_id')) is int and content['target_message_id'] == mid
            and isinstance(content.get('choice'), str)
            and semantic_assessment._finite_confidence(content.get('confidence')) is not None)


def _loop_skip_ref(db, pid, root, fp, policy, reason):
    """Identify the newest valid loop-skip record for this generation."""
    row = db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? '
                     'AND message_id=? ORDER BY artifact_id DESC LIMIT 1',
                     ('semantic_loop_skipped', pid, root)).fetchone()
    if row is None:
        return None
    try:
        content, meta = strict_json(row['content']), strict_json(row['meta'])
        if (not isinstance(content, dict) or not isinstance(meta, dict) or not is_current(meta)
                or meta.get('invalidated') or meta.get('stale') or meta.get('schema') != SCHEMA_VERSION
                or meta.get('fingerprint') != fp or meta.get('policy_fingerprint') != policy
                or content != {'status': 'SKIPPED', 'reason': reason}):
            return None
        return [row['artifact_id'], mcs_util.payload_hash([content, meta])]
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return None


def _loop_completion_manifest(db, pid, bundle, targets, scfg, *, facts_by_target=None, skipped=False,
                              deadline=None):
    """Bind actual fact and loop artifacts for a completed target set."""
    import semantic_loops
    import semantic_projection

    def check_deadline():
        if deadline is not None and time.monotonic() >= deadline:
            raise runtime.RuntimeBudgetShort('loop_completion')

    check_deadline()
    fp, policy = bundle['source_fingerprint'], policy_fingerprint(scfg)
    members = {member['message_id']: member for member in bundle['members']}
    if (not isinstance(targets, list) or not targets or len(targets) != len(set(targets))
            or any(type(mid) is not int or mid not in members for mid in targets)):
        return None
    refs, candidates, events, rejections = [], {}, {}, []
    fact_kind = KIND_FACTS_V2 if scfg.get('fact_source') == 'canonical' else KIND_FACTS
    for mid in targets:
        check_deadline()
        found = None
        for row in db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? '
                              'AND message_id=? ORDER BY artifact_id DESC', (fact_kind, pid, mid)):
            check_deadline()
            meta = strict_json(row['meta'])
            if not isinstance(meta, dict):
                return None
            if meta.get('fingerprint') != fp:
                continue
            content = strict_json(row['content'])
            if (not isinstance(content, dict) or not is_current(meta) or meta.get('invalidated')
                    or meta.get('stale') or meta.get('schema') != SCHEMA_VERSION):
                return None
            facts = (semantic_projection.project_v2_facts(content) if fact_kind == KIND_FACTS_V2
                     else content.get('facts'))
            if (not isinstance(facts, list) or facts_by_target is not None and facts != facts_by_target.get(mid)):
                return None
            found = row
            refs.append([fact_kind, mid, row['artifact_id'], mcs_util.payload_hash([content, meta])])
            break
        if found is None:
            return None
        if fact_kind == KIND_FACTS_V2:
            audit = db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? '
                               'AND message_id=? ORDER BY artifact_id DESC LIMIT 1',
                               (KIND_FACT_AUDIT, pid, mid)).fetchone()
            if audit is None:
                return None
            audit_content, audit_meta = strict_json(audit['content']), strict_json(audit['meta'])
            if (not isinstance(audit_content, dict) or not isinstance(audit_meta, dict)
                    or not is_current(audit_meta) or audit_meta.get('invalidated') or audit_meta.get('stale')
                    or audit_meta.get('fingerprint') != fp or audit_meta.get('policy_fingerprint') != policy
                    or audit_content.get('status') not in ('PASS', 'SKIPPED')):
                return None
            refs.append([KIND_FACT_AUDIT, mid, audit['artifact_id'],
                         mcs_util.payload_hash([audit_content, audit_meta])])
        assessments = 0
        for row in db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? '
                              'AND message_id=? ORDER BY artifact_id', (KIND_ASSESS, pid, mid)):
            check_deadline()
            meta = strict_json(row['meta'])
            if not isinstance(meta, dict):
                return None
            if meta.get('fingerprint') != fp or meta.get('policy_fingerprint') != policy:
                continue
            content = strict_json(row['content'])
            if (not isinstance(content, dict)
                    or (not is_current(meta) if meta.get('fact_id') is None
                        else not _valid_detail_assessment(content, meta, mid))):
                return None
            refs.append([KIND_ASSESS, mid, row['artifact_id'], mcs_util.payload_hash([content, meta])])
            if meta.get('fact_id') is None:
                assessments += 1
        if not assessments:
            return None
    def loop_rows():
        for kind in (KIND_LOOP, KIND_LOOP_EVENT, semantic_loops.KIND_LOOP_REJECTED):
            for mid in sorted(members):
                check_deadline()
                yield from db.execute('SELECT artifact_id,kind,message_id,content,meta FROM artifacts '
                                      'WHERE kind=? AND project_id=? AND message_id=? ORDER BY artifact_id',
                                      (kind, pid, mid))
    for row in loop_rows():
        check_deadline()
        meta = strict_json(row['meta'])
        if not isinstance(meta, dict):
            return None
        if meta.get('fingerprint') != fp or meta.get('policy_fingerprint') != policy:
            continue
        content = strict_json(row['content'])
        if not isinstance(content, dict) or not is_current(meta):
            return None
        refs.append([row['kind'], row['message_id'], row['artifact_id'], mcs_util.payload_hash([content, meta])])
        if meta.get('invalidated') or meta.get('stale'):
            continue
        if row['kind'] == KIND_LOOP:
            source = members.get(row['message_id'])
            origin = content.get('origin')
            if (not semantic_loops._valid_candidate(content, meta) or not isinstance(origin, dict)
                    or source is None or origin.get('message_id') != row['message_id']
                    or origin.get('revision') != source['revision'] or not origin.get('evidence')
                    or content.get('root_id') != bundle['root_id']
                    or content.get('project_id') != pid or content.get('account_scope') != bundle['account_scope']
                    or meta.get('registry') != jev.REGISTRY_VERSION):
                return None
            candidates[row['artifact_id']] = (row['message_id'], content)
        elif row['kind'] == KIND_LOOP_EVENT:
            key = (content.get('loop_artifact_id'), content.get('trigger_message_id'))
            if (type(key[0]) is not int or type(key[1]) is not int or key in events
                    or content.get('trigger_message_id') != row['message_id']
                    or meta.get('registry') != jev.REGISTRY_VERSION
                    or content.get('relation') not in jev.LOOP_RELATION_OPTIONS
                    or content.get('reported_relation') not in jev.LOOP_RELATION_OPTIONS
                    or type(content.get('confidence')) not in (int, float)
                    or not math.isfinite(content['confidence']) or not 0 <= content['confidence'] <= 1):
                return None
            events[key] = content
        else:
            rejections.append((content, meta))
    for content, meta in rejections:
        check_deadline()
        scope = meta.get('retry_scope')
        if not isinstance(scope, str):
            return None
        rejected = semantic_loops.rejected_relation_pair(content, meta, policy, scope)
        if rejected is None:
            return None
        (aid, mid, revision), _ = rejected
        if (aid in candidates and mid in targets and revision == members[mid]['revision']
                and (skipped or (aid, mid) not in events)):
            return None
    if not skipped:
        for aid, (origin_id, candidate) in candidates.items():
            for mid in targets:
                check_deadline()
                if mid == origin_id:
                    continue
                event = events.get((aid, mid))
                if (event is None or event.get('root_id') != bundle['root_id']
                        or event.get('origin_revision') != members[origin_id]['revision']
                        or event.get('trigger_revision') != members[mid]['revision']
                        or event.get('candidate_state') != candidate.get('state')):
                    return None
    check_deadline()
    digest = mcs_util.payload_hash({'targets': sorted(targets), 'artifacts': refs})
    check_deadline()
    return digest


def _classification_only_state(ledger, row, scfg, cfg, bundle, fp, policy):
    """Validate current raw and primary assessment evidence for classification only."""
    import extract_llm
    import patient_context_refresh
    import semantic_intake_hold

    db, pid, mid = ledger.db, row['project_id'], row['message_id']
    root = row['parent_id'] or mid
    try:
        if semantic_intake_hold.has_unstored(db, pid) or semantic_intake_hold.has_held_root(db, pid, root):
            return 'held'
        job = db.execute("SELECT state,substr(payload,1,?) AS payload FROM fetch_jobs WHERE kind=? "
                         "AND project_id=? AND message_id=?",
                         (_REPROCESS_JOB_MAX_BYTES + 1, JOB_KIND, pid, root)).fetchone()
        if job is not None:
            if _reprocess_job_payload(ledger, job, pid, root) is None:
                return 'held'
            if job['state'] == 'failed':
                return 'failed'
            if job['state'] != 'done':
                return 'pending'
        source = db.execute('SELECT * FROM messages WHERE project_id=? AND message_id=?', (pid, mid)).fetchone()
        if source is None:
            return 'held'
        if (scfg['extract_qc'] == 'annotate'
                and not patient_context_refresh._extraction_qc_ready(db, pid, scfg, message_id=mid)):
            return 'held'
        complete, selected_meta = patient_context_refresh._raw_extraction_current(db, source, cfg)
        latest = db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND message_id=? "
                            "AND (project_id IS NULL OR project_id=?) ORDER BY artifact_id DESC LIMIT 1",
                            (extract_llm.KIND, mid, pid)).fetchone()
        if not complete or latest is None:
            return 'held'
        raw, raw_meta = strict_json(latest['content']), strict_json(latest['meta'])
        if (not isinstance(raw, dict) or not isinstance(raw_meta, dict) or raw_meta != selected_meta
                or not is_current(raw_meta) or raw_meta.get('invalidated')):
            return 'held'
        # A newer adverse or unparseable audit never becomes a classification success.
        for kind in (KIND_AUDIT, KIND_FACT_AUDIT):
            audit = db.execute('SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND message_id=? '
                               'ORDER BY artifact_id DESC LIMIT 1', (kind, pid, mid)).fetchone()
            if audit is None:
                continue
            content, meta = strict_json(audit['content']), strict_json(audit['meta'])
            if (not isinstance(content, dict) or not isinstance(meta, dict) or not is_current(meta)
                    or not isinstance(meta.get('fingerprint'), str)
                    or not isinstance(meta.get('policy_fingerprint'), str)):
                return 'held'
            if meta.get('fingerprint') == fp and meta.get('policy_fingerprint') == policy:
                status, findings = content.get('status'), content.get('findings', [])
                if (meta.get('invalidated') or status not in ('PASS', 'SKIPPED')
                        or not isinstance(findings, list) or any(not isinstance(item, dict)
                            or not isinstance(item.get('code'), str) for item in findings)):
                    return 'held'
                if kind == KIND_FACT_AUDIT:
                    facts = current_artifact(db, KIND_FACTS_V2, mid, fp)
                    if facts is None or meta.get('doc_hash') != semantic_facts.doc_hash(facts['content']):
                        return 'held'
                    if status == 'PASS':
                        if findings or content.get('evaluated') is not True or v4.reproject_doc(db, mid, meta)[0] is None:
                            return 'held'
                    else:
                        member = next((item for item in bundle['members'] if item['message_id'] == mid), None)
                        reason = content.get('jev_skip_reason')
                        if (content.get('evaluated') is not False or content.get('code_checks') != 'passed'
                                or content.get('document_hash') != mcs_util.payload_hash(facts['content'])
                                or semantic_audit.optional_fact_audit(facts['content'], member, fp, reason, content,
                                    match_threshold=scfg['match_threshold'])['status'] != 'SKIPPED'):
                            return 'held'
                else:
                    import semantic_send_gate
                    member = next((item for item in bundle['members'] if item['message_id'] == mid), None)
                    mode = meta.get('publication_mode')
                    if (member is None or mode not in ('shadow', 'assist', 'enforce')
                            or semantic_send_gate._paired_summary(db, pid, mid, fp, policy,
                                member['revision'], mode, status) is None):
                        return 'held'
                if kind == KIND_AUDIT and status == 'PASS':
                    if (any(item['code'] in ('fact_contradicts', 'fact_not_supported', 'fact_ambiguous',
                                              'fact_low_confidence') for item in findings)
                            or semantic_audit.audit_status_for(findings, findings, True, True) != 'PASS'):
                        return 'held'
                elif kind == KIND_AUDIT and (meta.get('jev_evaluated') is not False or meta.get('code_checks') != 'passed'
                      or meta.get('jev_skip_reason') not in ('not_configured', 'disabled', 'daily_budget',
                          'backlog_reserved', 'service_unavailable', 'circuit_open')
                      or any(item['code'] != 'support_unevaluated' for item in findings)):
                    return 'held'
        assessment, previous = _primary_assessment_history(db, pid, mid, fp, policy)
        if assessment is None:
            return 'held'
        content, meta, model = assessment['content'], assessment['meta'], assessment['model']
        if (not _valid_primary_assessment(content, meta, model, mid, fp, policy, scfg,
                                          prefiltered=_verified_prefilter(ledger, pid, mid))
                or not _valid_primary_history(content, meta, previous, mid, fp, policy, scfg)):
            return 'held'
        if meta.get('technical_status') == 'complete':
            if meta.get('registry') != jev.REGISTRY_VERSION or meta.get('model') != jev.JEV_MODEL:
                return 'held'
            return 'current'
        if (meta.get('technical_status') != 'skipped' or meta.get('jev_skip_reason') not in
                ('not_configured', 'disabled', 'daily_budget', 'backlog_reserved', 'service_unavailable', 'circuit_open')):
            return 'held'
        return 'current_skipped'
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return 'held'


def _loop_only_state(ledger, row, scfg, cfg, bundle, fp, policy, deadline=None):
    """Require a current classification and a bound completed loop pass."""
    base = _classification_only_state(ledger, row, scfg, cfg, bundle, fp, policy)
    if base not in ('current', 'current_skipped'):
        return base
    db, pid, mid = ledger.db, row['project_id'], row['message_id']
    try:
        receipt = db.execute('SELECT content,meta,model FROM artifacts WHERE kind=? AND project_id=? '
                             'AND message_id=? ORDER BY artifact_id DESC LIMIT 1',
                             (KIND_LOOP_COMPLETION, pid, mid)).fetchone()
        if receipt is None:
            return 'held'
        content, meta = strict_json(receipt['content']), strict_json(receipt['meta'])
        if not isinstance(content, dict) or not isinstance(meta, dict):
            return 'held'
        targets, status = content.get('target_ids'), content.get('status')
        member = next((item for item in bundle['members'] if item['message_id'] == mid), None)
        if (not isinstance(targets, list) or not targets or any(type(item) is not int for item in targets)
                or targets != sorted(set(targets)) or mid not in targets or member is None
                or set(content) != {'status', 'target_ids', 'loop_mode', 'semantic_mode',
                                    'manifest', 'skip_reason', 'skip_ref'}
                or status not in ('DONE', 'SKIPPED') or content['loop_mode'] != scfg['loop_mode']
                or content['semantic_mode'] != scfg['mode']
                or not is_current(meta) or meta.get('invalidated') or meta.get('stale')
                or meta.get('schema') != SCHEMA_VERSION or meta.get('fingerprint') != fp
                or meta.get('policy_fingerprint') != policy or meta.get('target_revision') != member['revision']
                or meta.get('execution_generation') != runtime.execution_generation(scfg)
                or meta.get('loop_status') != status or meta.get('skip_reason') != content.get('skip_reason')
                or meta.get('content_hash') != mcs_util.payload_hash(content) or receipt['model'] != jev.JEV_MODEL):
            return 'held'
        if status == 'SKIPPED':
            reason = content.get('skip_reason')
            if (reason not in ('not_configured', 'disabled', 'daily_budget', 'backlog_reserved',
                               'service_unavailable', 'circuit_open')
                    or content.get('skip_ref') is None
                    or content['skip_ref'] != _loop_skip_ref(db, pid, bundle['root_id'], fp, policy, reason)):
                return 'held'
        elif content.get('skip_reason') is not None or content.get('skip_ref') is not None:
            return 'held'
        current = semantic_store.thread_bundle(db, pid, bundle['root_id'], targets)
        if current is None or current['source_fingerprint'] != fp:
            return 'held'
        states = []
        for target in targets:
            if deadline is not None and time.monotonic() >= deadline:
                return 'held'
            source = db.execute('SELECT m.*,p.project_id AS patient_id,p.fetch_reason FROM messages m '
                                'JOIN patients p ON p.project_id=m.project_id '
                                'WHERE m.project_id=? AND m.message_id=?', (pid, target)).fetchone()
            if source is None:
                return 'held'
            state = _classification_only_state(ledger, source, scfg, cfg, current, fp, policy)
            if state not in ('current', 'current_skipped'):
                return 'held'
            states.append(state)
        if status == 'DONE' and 'current_skipped' in states:
            return 'held'
        manifest = _loop_completion_manifest(db, pid, current, targets, scfg,
                                             skipped=status == 'SKIPPED', deadline=deadline)
        if manifest is None or manifest != content.get('manifest'):
            return 'held'
        return 'current_skipped' if status == 'SKIPPED' or 'current_skipped' in states else 'current'
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError,
            runtime.RuntimeBudgetShort):
        return 'held'


def _reprocess_target_state(ledger, row, scfg, *, cfg=None, deadline=None):
    """Observe one stored source without mistaking job completion for coverage."""
    from mcs_queries import current_v4_id, paused
    pid, mid = row['project_id'], row['message_id']
    if row['fetch_reason'] in ('forbidden', 'permission_denied', 'access_revoked'):
        return 'permission'
    if pid is None or row['patient_id'] is None:
        return 'scope'
    if scfg['project_ids'] is not None and pid not in scfg['project_ids']:
        return 'scope'
    if paused(ledger.db, pid):
        return 'paused'
    if row['body_state'] == 'deleted':
        return 'deleted'
    if row['body_state'] == 'full' and row['body_text'] == '':
        return 'empty_body'
    if row['body_state'] not in (None, 'full') or not row['body_text']:
        return 'body_unavailable'
    root = row['parent_id'] or mid
    bundle = semantic_store.thread_bundle(ledger.db, pid, root, [mid])
    if bundle is None:
        return 'held'
    fp, policy = bundle['source_fingerprint'], policy_fingerprint(scfg)
    if scfg['summary_mode'] == 'off' and scfg['loop_mode'] == 'off':
        return _classification_only_state(ledger, row, scfg, cfg if cfg is not None else load_config(), bundle, fp, policy)
    if scfg['summary_mode'] == 'off':
        return _loop_only_state(ledger, row, scfg, cfg if cfg is not None else load_config(),
                                bundle, fp, policy, deadline=deadline)
    audit = current_artifact(ledger.db, KIND_AUDIT, mid, fp, policy)
    summary = current_artifact(ledger.db, KIND_SUMMARY, mid, fp, policy)
    if audit and summary and audit['content'].get('status') == 'SKIPPED':
        import semantic_send_gate
        member = next((item for item in bundle['members'] if item['message_id'] == mid), None)
        if (member is not None and semantic_send_gate.verified_skipped_summary_current(
                ledger.db, pid, mid, fp, policy, member['revision'], scfg['summary_mode'],
                bundle=bundle, fact_source=scfg.get('fact_source'))):
            if scfg.get('fact_source') != 'canonical':
                if current_artifact(ledger.db, KIND_FACTS, mid, fp) is not None:
                    return 'current_skipped'
            elif v4.current_skipped(ledger.db, pid, mid, fp, policy, member):
                return 'current_skipped'
        return 'held'
    if audit and summary and audit['content'].get('status') == 'PASS':
        import semantic_send_gate
        member = next((item for item in bundle['members'] if item['message_id'] == mid), None)
        if (member is None or not semantic_send_gate.passed_summary_current(
                ledger.db, pid, mid, fp, policy, member['revision'], scfg['summary_mode'],
                bundle=bundle, fact_source=scfg.get('fact_source'))):
            return 'held'
        if scfg.get('fact_source') != 'canonical':
            if current_artifact(ledger.db, KIND_FACTS, mid, fp) is not None:
                return 'current'
        vid = ledger.db.execute(f'SELECT {current_v4_id()} FROM messages m '
                                'WHERE m.message_id=?', (mid,)).fetchone()[0]
        published = ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?',
                                       (vid,)).fetchone() if vid is not None else None
        meta = loads_dict(published[0]) if published is not None else {}
        if (meta and meta.get('fingerprint') == fp
                and meta.get('policy_fingerprint') == policy):
            doc, _ = v4.reproject_doc(ledger.db, mid, meta)
            if doc is not None:
                return 'current'
    job = ledger.db.execute("SELECT state,substr(payload,1,?) AS payload FROM fetch_jobs WHERE kind=? "
                            'AND project_id=? AND message_id=?',
                            (_REPROCESS_JOB_MAX_BYTES + 1, JOB_KIND, pid, root)).fetchone()
    if job is not None and is_current(loads_dict(job['payload'])):
        if job['state'] == 'failed':
            return 'failed'
        if audit and audit['content'].get('status') == 'NEEDS_REVIEW':
            return 'held'
        if job['state'] == 'done':
            return 'repair_required'
        return 'pending'
    return 'pending'


def _reprocess_patient_state(ledger, row, cfg, scfg, witness=None):
    """Inspect patient proofs without replacing a rollup whose freshness is unproved."""
    import patient_context_refresh
    from mcs_queries import paused
    pid = row['project_id']
    if (scfg['project_ids'] is not None and pid not in scfg['project_ids']
            or row['fetch_reason'] in ('forbidden', 'permission_denied', 'access_revoked')):
        return 'excluded'
    import semantic_intake_hold
    if semantic_intake_hold.has_unstored(ledger.db, pid):
        return 'held'
    if paused(ledger.db, pid):
        return 'paused'
    proof = patient_context_refresh.inference_coverage(ledger.db, pid, cfg)
    if not is_current(proof):
        return 'pending'
    if not proof['complete']:
        dependency = proof.get('dependencies', {}).get('coverage', {})
        if dependency.get('terminal', 0):
            return 'terminal_gap'
        if dependency.get('repairable', 0):
            return 'repair_pending'
        counts = proof['sources']
        if counts['failed']:
            return 'failed'
        if counts['held'] or proof['status'] == 'off':
            return 'held'
        if counts['incomplete'] or proof['status'] in ('unacquired', 'dependency_gap'):
            return 'incomplete'
        return 'pending'
    import rollup
    proof = rollup.completion_coverage(ledger.db, pid, cfg)
    if not is_current(proof) or proof.get('current') is not True:
        return 'rollup_unverified'
    if witness is not None:
        witness.update(proof)
    return 'current'


_REPROCESS_PROOF_CONTRACT = 1


def _reprocess_proof_generation(ledger, cfg, scfg):
    """Bind completion to interpretation and scope, never credentials or spending caps."""
    import patient_context_refresh
    import rollup
    import cloud_inference
    cloud = cfg.get('cloud_llm', {})
    cloud = cloud if isinstance(cloud, dict) else {'mode': 'invalid'}
    contract_cfg = dict(cfg)
    contract_cloud = {key: value for key, value in cloud.items()
                      if key not in runtime.CLOUD_OPERATING_LIMITS}
    if contract_cloud:
        contract_cfg['cloud_llm'] = contract_cloud
    else:
        contract_cfg.pop('cloud_llm', None)
    return mcs_util.payload_hash({
        'contract': _REPROCESS_PROOF_CONTRACT, 'target_classification': 'empty-body-v1',
        'execution': runtime.execution_generation(scfg),
        'semantic': {key: scfg.get(key) for key in ('mode', 'summary_mode', 'loop_mode',
                     'extract_qc', 'project_ids', 'candidate_fingerprint')},
        'context': patient_context_refresh._contract(cfg),
        'context_options': patient_context_refresh.options(cfg),
        'cloud': {key: cloud.get(key) for key in ('mode', 'routes', 'project_ids', 'exclude_project_ids', 'model')},
        'cloud_contract': cloud_inference.cloud_provider_contract(contract_cfg),
        'cloud_repair_contract': cloud_inference.cloud_provider_contract(
            contract_cfg, route='semantic_backlog', purpose='quality_repair'),
        'rollup': rollup.config_generation(cfg),
        'dictionary': rollup.generation_signature(ledger.db),
        'references': tuple(ledger.db.execute('SELECT COUNT(*),MAX(artifact_id) FROM artifacts WHERE kind=?',
                                             (rollup.REF_KIND,)).fetchone()),
        'patients': tuple(ledger.db.execute('SELECT COUNT(*),MAX(project_id) FROM patients').fetchone())})


def _reprocess_proof_time_valid(progress, now):
    started, until = progress.get('proof_started_at'), progress.get('proof_valid_until')
    checked = progress.get('proof_checked_at', started)
    return (type(started) in (int, float) and math.isfinite(started) and 0 <= started <= now
            and type(checked) in (int, float) and math.isfinite(checked) and started <= checked <= now
            and (until is None or type(until) in (int, float) and math.isfinite(until) and now < until < 1e12))


def _reprocess_bookmark_sealed(ledger, old_progress, epochs, version, changes, cfg, scfg, generation):
    """Recheck a cached observation under the same writer fence without rewriting it."""
    import semantic_intake_hold
    ledger.db.execute('BEGIN IMMEDIATE')
    with ledger.db:
        current, _ = _reservation_sched(ledger)
        return (current.get('inference_reprocess') == old_progress
                and all(current.get(field, 0) == epochs[field]
                        for field in ('source_epoch', 'analysis_epoch', 'job_state_epoch'))
                and semantic_intake_hold.hold_epoch(ledger.db) == epochs['intake_hold_epoch']
                and semantic_intake_hold.unstored_epoch(ledger.db) == epochs['intake_unstored_epoch']
                and ledger.db.execute('PRAGMA data_version').fetchone()[0] == version
                and ledger.db.total_changes == changes
                and _reprocess_proof_generation(ledger, cfg, scfg) == generation
                and _reprocess_proof_time_valid(old_progress, time.time()))


def reprocess_step(ledger, cfg, *, limit=32, deadline=None):
    """Advance a finite keyset pass; input races never erase its accepted cursor."""
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError('inference_reprocess_limit')
    if ledger.db.in_transaction:
        raise ValueError('inference_reprocess_transaction_active')
    cfg = copy.deepcopy(cfg)
    scfg, errors = runtime_config(cfg)
    if errors or scfg['mode'] == 'off':
        return {'status': 'off', 'complete': False}
    generation = runtime.execution_generation(scfg)
    scope_generation = mcs_util.payload_hash(scfg['project_ids'])
    proof_generation = _reprocess_proof_generation(ledger, cfg, scfg)
    now = time.time()
    version = ledger.db.execute('PRAGMA data_version').fetchone()[0]
    changes = ledger.db.total_changes
    original = _sched_state(ledger)
    old_progress = original.get('inference_reprocess')
    fields = ('source_epoch', 'analysis_epoch', 'job_state_epoch')
    epochs = {field: original.get(field, 0) for field in fields}
    import semantic_intake_hold
    try:
        epochs['intake_hold_epoch'] = semantic_intake_hold.hold_epoch(ledger.db)
        epochs['intake_unstored_epoch'] = semantic_intake_hold.unstored_epoch(ledger.db)
    except sqlite3.Error:
        return {'status': 'intake_hold_unverified', 'complete': False}
    if any(original.get(field + '_invalid') or type(value) is not int
           or not 0 <= value < 2**63 for field, value in epochs.items()):
        return {'status': 'invalid_source_epoch', 'complete': False}
    progress = old_progress
    live_upper = ledger.db.execute('SELECT COALESCE(MAX(message_id),0) FROM messages').fetchone()[0]
    if (not isinstance(progress, dict) or not is_current(progress)
            or progress.get('execution_generation') != generation
            or progress.get('scope_generation') != scope_generation):
        progress = {VERSION_KEY: CURRENT_VERSION, 'execution_generation': generation,
                    'scope_generation': scope_generation, **epochs,
                    'upper': live_upper,
                    'cursor': 0, 'seed_cursor': 0, 'phase': 'seed', 'counts': {}}
    counters = {'current', 'current_skipped', 'pending', 'repair_required', 'held', 'failed', 'permission',
                'scope', 'paused', 'deleted', 'body_unavailable', 'empty_body'}
    if (progress.get('phase') not in ('seed', 'verify', 'verify_patients', 'waiting', 'complete')
            or any(type(progress.get(key)) is not int or not 0 <= progress[key] < 2**63
                   for key in ('cursor', 'upper'))
            or progress['cursor'] > progress['upper']
            or not isinstance(progress.get('counts'), dict)
            or any(key not in counters or type(value) is not int or not 0 <= value < 2**63
                   for key, value in progress['counts'].items())):
        return {'status': 'invalid_progress', 'complete': False}
    progress = {**progress, 'counts': dict(progress['counts'])}
    progress.setdefault('seed_cursor', progress['cursor'] if progress['phase'] == 'seed' else progress['upper'])
    if type(progress['seed_cursor']) is not int or not 0 <= progress['seed_cursor'] < 2**63:
        return {'status': 'invalid_progress', 'complete': False}
    changed = any(progress.get(field, 0) != value for field, value in epochs.items())
    proof_changed = progress.get('proof_generation') != proof_generation
    proof_expired = not _reprocess_proof_time_valid(progress, now)
    progress.update(future_upper=live_upper, tail_pending=live_upper > progress['seed_cursor'])
    progress.update(epochs)
    if changed:
        progress.update(dirty=True, complete=False)
    # Finished/waiting rounds are bookmarks, not an instruction to seed the
    # corpus again. Only a real source, adopted proof or job-state change wakes
    # a new bounded round. Operational settings grant no fresh attempt budget.
    if progress['phase'] in ('waiting', 'complete'):
        patient_counts = progress.get('patient_counts')
        proof_contract = progress.get('proof_contract')
        if (not isinstance(patient_counts, dict) or any(
                not isinstance(key, str) or type(value) is not int or not 0 <= value < 2**63
                for key, value in patient_counts.items())
                or proof_contract is not None and (type(proof_contract) is not int
                    or proof_contract != _REPROCESS_PROOF_CONTRACT)):
            return {'status': 'invalid_progress', 'complete': False}
        if not changed and not progress['tail_pending'] and not proof_changed and not proof_expired \
                and progress.get('proof_contract') == _REPROCESS_PROOF_CONTRACT \
                and ((progress['phase'] == 'waiting' and progress.get('complete') is False
                      and progress.get('proof_status') in ('incomplete', 'rollup_freshness_unverified'))
                     or (progress['phase'] == 'complete' and progress.get('complete') is True
                         and progress.get('proof_status') == 'current' and patient_counts.get('current', 0) > 0
                         and progress.get('proof_valid_until') is not None)):
            if not _reprocess_bookmark_sealed(ledger, old_progress, epochs, version, changes,
                                             cfg, scfg, proof_generation):
                return {'status': 'source_changed', 'complete': False, 'processed': 0}
            return {'status': progress['phase'], 'processed': 0, **progress}
        if not changed and not progress['tail_pending'] and progress.get('proof_contract') != _REPROCESS_PROOF_CONTRACT:
            counts = progress.get('patient_counts')
            if counts is not None and (not isinstance(counts, dict) or any(
                    not isinstance(key, str) or type(value) is not int or not 0 <= value < 2**63
                    for key, value in counts.items())):
                return {'status': 'invalid_progress', 'complete': False}
            if isinstance(counts, dict) and counts.get('current', 0):
                current, unverified = counts['current'], counts.get('rollup_unverified', 0)
                if current + unverified >= 2**63:
                    return {'status': 'invalid_progress', 'complete': False}
                counts = dict(counts)
                counts['rollup_unverified'] = current + unverified
                counts.pop('current')
                progress['patient_counts'] = counts
            reasons = progress.get('waiting_reasons')
            if reasons is not None and not isinstance(reasons, dict):
                return {'status': 'invalid_progress', 'complete': False}
            if isinstance(counts, dict) and counts.get('rollup_unverified', 0):
                reasons = dict(reasons or {})
                patients = reasons.get('patients')
                if patients is not None and (not isinstance(patients, dict) or any(
                        not isinstance(key, str) or type(value) is not int or not 0 <= value < 2**63
                        for key, value in patients.items())):
                    return {'status': 'invalid_progress', 'complete': False}
                patients = dict(patients or {})
                patients['rollup_unverified'] = counts['rollup_unverified']
                reasons['patients'] = patients
                progress['waiting_reasons'] = reasons
            stages = progress.get('remaining_stages')
            stages = list(stages) if isinstance(stages, list) else []
            if 'rollup_freshness_witness' not in stages:
                stages.append('rollup_freshness_witness')
            progress.update(phase='waiting', complete=False,
                            proof_status='rollup_freshness_unverified',
                            proof_reason='患者集約の現行性を確認できません',
                            remaining_stages=stages)
            # Keep old observations as unverified; the next call evaluates
            # this same corpus without resetting a job's attempt generation.
            progress['proof_contract'] = _REPROCESS_PROOF_CONTRACT
            progress['proof_generation'] = None
            if progress != old_progress:
                with _sched_update(ledger) as sched:
                    if (sched.get('inference_reprocess') != old_progress
                            or any(sched.get(field, 0) != epochs[field] for field in fields)
                            or semantic_intake_hold.hold_epoch(ledger.db) != epochs['intake_hold_epoch']
                            or semantic_intake_hold.unstored_epoch(ledger.db) != epochs['intake_unstored_epoch']
                            or ledger.db.execute('PRAGMA data_version').fetchone()[0] != version
                            or ledger.db.total_changes != changes):
                        return {'status': 'source_changed', 'complete': False, 'processed': 0}
                    sched['inference_reprocess'] = progress
            return {'status': progress['phase'], 'processed': 0, **progress}
        upper = ledger.db.execute('SELECT COALESCE(MAX(message_id),0) FROM messages').fetchone()[0]
        tail = upper > progress['seed_cursor']
        progress.update(phase='seed' if tail else 'verify',
                        cursor=progress['seed_cursor'] if tail else 0, upper=upper,
                        counts={}, dirty=False, verify_epochs=dict(epochs), complete=False,
                        proof_generation=proof_generation)
    if progress['phase'] in ('verify', 'verify_patients') and progress.get('proof_generation') != proof_generation:
        # Finish the bounded round, but never accept mixed-generation proofs.
        progress.update(dirty=True, complete=False)
    if progress['phase'] == 'verify_patients' and proof_expired:
        # Configuration/time changes invalidate accepted patient proofs, not
        # source enumeration or same-input retry rights.
        progress.update(patient_cursor=0, patient_counts={},
                        proof_contract=_REPROCESS_PROOF_CONTRACT, proof_started_at=now,
                        proof_checked_at=now, proof_valid_until=None, complete=False)
    if progress['phase'] == 'verify_patients':
        states = {'current', 'pending', 'held', 'paused', 'failed', 'incomplete', 'excluded',
                  'terminal_gap', 'repair_pending', 'rollup_unverified'}
        if (any(type(progress.get(key)) is not int or progress[key] < 0
                for key in ('patient_cursor', 'patient_upper'))
                or not isinstance(progress.get('patient_counts'), dict)
                or any(key not in states or type(value) is not int or value < 0
                       for key, value in progress['patient_counts'].items())):
            return {'status': 'invalid_progress', 'complete': False}
        rows = ledger.db.execute('SELECT project_id,fetch_reason FROM patients '
                                 'WHERE project_id>? AND project_id<=? ORDER BY project_id LIMIT ?',
                                 (progress['patient_cursor'], progress['patient_upper'], limit)).fetchall()
        counts = dict(progress['patient_counts'])
        processed = 0
        for row in rows:
            if deadline is not None and time.monotonic() >= deadline:
                break
            witness = {}
            state = _reprocess_patient_state(ledger, row, cfg, scfg, witness)
            if state == 'current':
                until = witness.get('valid_until')
                checked = witness.get('checked_at')
                if (type(until) not in (int, float) or not math.isfinite(until) or not now < until < 1e12
                        or type(checked) not in (int, float) or not math.isfinite(checked) or not now <= checked < until):
                    state = 'rollup_unverified'
                else:
                    prior = progress.get('proof_valid_until')
                    progress['proof_valid_until'] = until if prior is None else min(prior, until)
                    progress['proof_checked_at'] = max(progress.get('proof_checked_at', now), checked)
            counts[state] = counts.get(state, 0) + 1
            progress['patient_cursor'] = row['project_id']
            processed += 1
        with _sched_update(ledger) as sched:
            # Patient proofs are read-only. No local or external writer may
            # change their inputs before this batch's cursor is adopted.
            if (sched.get('inference_reprocess') != old_progress
                    or any(sched.get(field, 0) != epochs[field] for field in fields)
                    or ledger.db.execute('PRAGMA data_version').fetchone()[0] != version
                    or ledger.db.total_changes != changes
                    or _reprocess_proof_generation(ledger, cfg, scfg) != proof_generation):
                return {'status': 'source_changed', 'complete': False, 'processed': 0}
            actual = {field: sched.get(field, 0) for field in fields}
            actual['intake_hold_epoch'] = semantic_intake_hold.hold_epoch(ledger.db)
            actual['intake_unstored_epoch'] = semantic_intake_hold.unstored_epoch(ledger.db)
            if any(sched.get(field + '_invalid') or type(value) is not int or not 0 <= value < 2**63
                   for field, value in actual.items()):
                return {'status': 'invalid_source_epoch', 'complete': False, 'processed': 0}
            progress['dirty'] = progress.get('dirty', False) or actual != epochs
            progress.update(actual)
            progress['patient_counts'] = counts
            if processed == len(rows) and (len(rows) < limit or progress['patient_cursor'] >= progress['patient_upper']):
                messages = progress.get('coverage', {}).get('counts', {})
                blocked_messages = {key: value for key, value in messages.items()
                    if value and key in ('pending', 'repair_required', 'held', 'failed', 'paused',
                                         'body_unavailable', 'empty_body')}
                blocked_patients = {key: value for key, value in counts.items() if value and key not in ('current', 'excluded')}
                upper = ledger.db.execute('SELECT COALESCE(MAX(message_id),0) FROM messages').fetchone()[0]
                tail = upper > progress['seed_cursor']
                progress.update(future_upper=upper, tail_pending=tail)
                dirty = progress.get('dirty', False) or progress.get('verify_epochs', actual) != actual
                proof_time_valid = _reprocess_proof_time_valid(progress, time.time())
                progress['complete'] = bool((counts.get('current', 0) + counts.get('current_skipped', 0))
                    and not blocked_messages and not blocked_patients
                    and not tail and not dirty and not actual['intake_unstored_epoch'] and proof_time_valid
                    and progress.get('proof_valid_until') is not None
                    and progress.get('proof_generation') == proof_generation)
                progress['waiting_reasons'] = {'messages': blocked_messages, 'patients': blocked_patients}
                if actual['intake_unstored_epoch']:
                    progress['waiting_reasons']['intake_unstored'] = 'present'
                progress['remaining_stages'] = (['source_or_patient_proof']
                    if blocked_messages or blocked_patients or tail or dirty else [])
                unverified = bool(counts.get('rollup_unverified') or not counts or not proof_time_valid)
                progress['proof_status'] = ('current' if progress['complete'] else
                    'rollup_freshness_unverified' if unverified else 'incomplete')
                if unverified:
                    progress['proof_reason'] = '患者集約の現行性を確認できません'
                    progress['remaining_stages'].append('rollup_freshness_witness')
                else:
                    progress.pop('proof_reason', None)
                progress['verified_at'] = time.time()
                if tail or dirty:
                    progress.update(phase='seed' if tail else 'verify',
                        cursor=progress['seed_cursor'] if tail else 0, upper=upper,
                        counts={}, dirty=False, verify_epochs=dict(actual),
                        proof_generation=proof_generation)
                else:
                    progress['phase'] = 'complete' if progress['complete'] else 'waiting'
            sched['inference_reprocess'] = progress
        return {'status': progress['phase'], 'processed': processed, **progress}
    rows = ledger.db.execute(
        'SELECT m.message_id,m.project_id,m.parent_id,m.body_state,m.body_text,'
        'p.project_id AS patient_id,p.fetch_reason FROM messages m '
        'LEFT JOIN patients p ON p.project_id=m.project_id '
        'WHERE m.message_id>? AND m.message_id<=? ORDER BY m.message_id LIMIT ?',
        (progress['cursor'], progress['upper'], limit)).fetchall()
    planned, sources = [], {}
    for row in rows:
        if deadline is not None and time.monotonic() >= deadline:
            break
        extra = {'deadline': deadline} if scfg['summary_mode'] == 'off' and scfg['loop_mode'] != 'off' else {}
        state = _reprocess_target_state(ledger, row, scfg, cfg=cfg, **extra)
        replay = None
        if state in ('current', 'current_skipped', 'pending', 'repair_required', 'held', 'failed'):
            root = row['parent_id'] or row['message_id']
            key = (row['project_id'], root)
            old = ledger.db.execute('SELECT substr(payload,1,?) AS payload,state FROM fetch_jobs WHERE kind=? '
                                    'AND project_id=? AND message_id=?',
                                    (_REPROCESS_JOB_MAX_BYTES + 1, JOB_KIND, *key)).fetchone()
            pl = _reprocess_job_payload(ledger, old, *key) if old is not None else {}
            import semantic_intake_hold
            semantic_intake_hold.validate_indexes(ledger.db)
            if semantic_intake_hold.has_held_root(ledger.db, *key):
                planned.append((row, 'held', None))
                continue
            if old is not None and pl is None:
                planned.append((row, 'held', None))
                continue
            if state in ('current', 'current_skipped'):
                planned.append((row, state, None))
                continue
            if key not in sources:
                sources[key] = ledger._semantic_source_generation(*key)
            if (not is_current(pl)
                    or pl.get('execution_contract') == 1 and pl.get('execution_generation') != generation
                    or pl.get('source_generation') != sources[key]
                    or not isinstance(pl.get('targets'), list) or row['message_id'] not in pl['targets']):
                arrival = (semantic_notice.origin_event_id(ledger.db, row['project_id'], [row['message_id']])
                           if old is not None and old['state'] == 'pending' and pl.get('eligible')
                           and not pl.get('notification_free') else None)
                replay = (sources[key], arrival)
        planned.append((row, state, replay))
    with _sched_update(ledger) as sched:
        if (sched.get('inference_reprocess') != old_progress
                or any(sched.get(field, 0) != epochs[field] for field in fields)
                or semantic_intake_hold.hold_epoch(ledger.db) != epochs['intake_hold_epoch']
                or semantic_intake_hold.unstored_epoch(ledger.db) != epochs['intake_unstored_epoch']
                or ledger.db.execute('PRAGMA data_version').fetchone()[0] != version
                or ledger.db.total_changes != changes):
            return {'status': 'source_changed', 'complete': False, 'processed': 0}
        for row, state, replay in planned:
            if replay is not None:
                ledger.semantic_seed_tx(row['project_id'], [row['message_id']],
                    {'source': 'inference_reprocess', 'notification_free': True,
                     'inference_generation': generation, 'arrival_event_id': replay[1]},
                    _source_generation=replay[0])
            progress['counts'][state] = progress['counts'].get(state, 0) + 1
            progress['cursor'] = row['message_id']
            if progress['phase'] == 'seed':
                progress['seed_cursor'] = row['message_id']
        if len(planned) == len(rows) and (len(rows) < limit or progress['cursor'] >= progress['upper']):
            if progress['phase'] == 'seed':
                progress['seed_cursor'] = max(progress['seed_cursor'], progress['upper'])
                progress.update(phase='verify', cursor=0, counts={}, dirty=False, verify_epochs=dict(epochs),
                                proof_generation=proof_generation)
            else:
                progress['coverage'] = {'counts': progress['counts'], 'observed_at': time.time(),
                                       'upper': progress['upper'], **epochs}
                progress.update(phase='verify_patients', counts={}, patient_cursor=0, patient_counts={},
                    proof_contract=_REPROCESS_PROOF_CONTRACT,
                    proof_started_at=time.time(), proof_checked_at=time.time(), proof_valid_until=None,
                    patient_upper=ledger.db.execute('SELECT COALESCE(MAX(project_id),0) FROM patients').fetchone()[0])
        progress['complete'] = False
        progress['tail_pending'] = progress['future_upper'] > progress['seed_cursor']
        progress['remaining_stages'] = ['patient_context', 'rollup', 'karte']
        sched['inference_reprocess'] = progress
    return {'status': progress['phase'], 'processed': len(planned), **progress}


_REALTIME_ORDER = ("CASE WHEN next_try<=updated_at THEN updated_at "
                   "ELSE created_at END DESC, job_id DESC")
_REALTIME_KEY = "CASE WHEN next_try<=updated_at THEN updated_at ELSE created_at END"
# Free long-output defers (the call needs LONG_MIN_CALL_S and the pass
# had less) are tracked per job generation in the sched row: such a job
# leaves the realtime lane, and after this many free defers of the same
# generation the next one charges a normal attempt so a budget that can
# never fit the call ends in a visible failure instead of looping (M6).
LONG_FREE_DEFER_CAP = 24
_LONG_DEFERS_MAX = 500
_LONG_DEFERS_SCAN_BYTES = 8 * 1024 * 1024


def _bounded_job_payload(db, job_id, limit):
    if (getattr(db, 'text_factory', str) is not str
            or not callable(getattr(db, 'blobopen', None))
            or db.execute('PRAGMA encoding').fetchone()[0].upper() != 'UTF-8'):
        return None
    row = db.execute('SELECT _rowid_,typeof(payload) FROM fetch_jobs WHERE job_id=?',
                     (job_id,)).fetchone()
    if row is None or row[0] != job_id or row[1] != 'text':
        return None
    with db.blobopen('fetch_jobs', 'payload', job_id, readonly=True) as blob:
        size = len(blob)
        if size > limit:
            return None
        raw = blob.read(size)
        if len(raw) != size:
            return None
    try:
        return raw.decode('utf-8')
    except UnicodeError:
        return None


def _long_defers(ledger) -> dict:
    """{str(job_id): {"g": generation, "n": free_defers}} — valid entries."""
    raw = _sched_state(ledger).get("long_defers")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items()
            if isinstance(k, str) and k.isdigit() and isinstance(v, dict)
            and isinstance(v.get("g"), str) and type(v.get("n")) is int}


def _known_long_defer_generation(raw):
    if not isinstance(raw, str) or len(raw) > _REPROCESS_JOB_MAX_BYTES:
        return None
    try:
        if len(raw.encode('utf-8')) > _REPROCESS_JOB_MAX_BYTES:
            return None
        payload = strict_json(raw)
        if not isinstance(payload, dict) or read_version(payload) not in (CURRENT_VERSION, LEGACY_VERSION):
            return None
        nodes = [(payload, 0)]
        while nodes:
            node, depth = nodes.pop()
            if depth > 128 or (type(node) is int and not -(2**63) <= node < 2**63):
                return None
            if isinstance(node, dict):
                nodes.extend((value, depth + 1) for value in node.values())
            elif isinstance(node, list):
                nodes.extend((value, depth + 1) for value in node)
        if 'targets' in payload and (not isinstance(payload['targets'], list)
                or not payload['targets']
                or any(type(mid) is not int or not 0 < mid < 2**63 for mid in payload['targets'])):
            return None
        generation = payload.get('generation')
        if generation is not None and not isinstance(generation, str):
            return None
        return runtime.payload_generation(payload)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return None


def _reservation_sched(ledger):
    row = ledger.db.execute(
        "SELECT job_id FROM fetch_jobs WHERE kind=? AND project_id=0 AND message_id=0",
        (SCHED_KIND,)).fetchone()
    if row is None:
        return {}, {}
    raw = _bounded_job_payload(ledger.db, row['job_id'], _REPROCESS_JOB_MAX_BYTES)
    if raw is None:
        raise ValueError('semantic_scheduler_invalid')
    try:
        if len(raw.encode('utf-8')) > _REPROCESS_JOB_MAX_BYTES:
            raise ValueError('semantic_scheduler_invalid')
        sched = strict_json(raw)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('semantic_scheduler_invalid') from None
    if not isinstance(sched, dict):
        raise ValueError('semantic_scheduler_invalid')
    entries = sched.get('long_defers', {})
    if not isinstance(entries, dict) or any(
            not isinstance(key, str) or not key.isascii() or not key.isdigit()
            or len(key) > 19 or str(int(key)) != key or int(key) >= 2**63
            or not isinstance(value, dict) or set(value) != {'g', 'n'}
            or not isinstance(value['g'], str) or not value['g']
            or type(value['n']) is not int or not 0 <= value['n'] < 2**63 - 1
            for key, value in entries.items()):
        raise ValueError('semantic_scheduler_invalid')
    cursors = sched.get('payload_hold_cursors', {})
    if (not isinstance(cursors, dict) or any(
            key not in ('combined', 'realtime', 'backlog')
            or not _held_cursor_valid(value, None if key == 'combined' else key)
            for key, value in cursors.items())):
        raise ValueError('semantic_scheduler_invalid')
    return sched, dict(entries)


def _reserve_long_defer(ledger, token):
    if ledger.db.in_transaction:
        raise ValueError('semantic_scheduler_transaction_active')
    ledger.db.execute('BEGIN IMMEDIATE')
    with ledger.db:
        sched, entries = _reservation_sched(ledger)
        import semantic_intake_hold
        try:
            semantic_intake_hold.validate_indexes(ledger.db)
            held = semantic_intake_hold.has_held_root(ledger.db, token.project_id, token.message_id)
        except sqlite3.Error:
            raise ValueError('semantic_intake_hold_unverified') from None
        if held:
            raise ValueError('semantic_intake_held')
        row = ledger.db.execute(
            'SELECT state,attempts FROM fetch_jobs '
            'WHERE job_id=? AND kind=? AND project_id=? AND message_id=?',
            (token.job_id, token.kind, token.project_id, token.message_id)).fetchone()
        raw = _bounded_job_payload(ledger.db, token.job_id, _REPROCESS_JOB_MAX_BYTES) if row else None
        if row is None or row['state'] != token.state or int(row['attempts'] or 0) != token.attempts:
            return None
        generation = _known_long_defer_generation(raw)
        if generation is None:
            raise ValueError('semantic_job_payload_unreadable')
        if raw != token.payload_raw or generation != token.generation:
            return None
        key = str(token.job_id)
        prior = entries.get(key)
        if prior is None and len(entries) >= _LONG_DEFERS_MAX:
            raise ValueError('semantic_long_defers_capacity')
        if prior and prior['g'] != token.generation:
            if _reprocess_job_payload(ledger, {'payload': raw}, token.project_id, token.message_id) is None:
                raise ValueError('semantic_long_defer_generation_unknown')
            prior = None
        count = prior['n'] if prior else 0
        if count >= 2**63 - 2:
            raise ValueError('semantic_scheduler_invalid')
        reserved = {'g': token.generation, 'n': count + 1}
        entries[key] = reserved
        sched['long_defers'] = entries
        _sched_write(ledger, sched, commit=False)
        return prior, reserved


def _refund_long_defer(ledger, token, reservation):
    if ledger.db.in_transaction:
        raise ValueError('semantic_scheduler_transaction_active')
    ledger.db.execute('BEGIN IMMEDIATE')
    with ledger.db:
        sched, entries = _reservation_sched(ledger)
        import semantic_intake_hold
        try:
            semantic_intake_hold.validate_indexes(ledger.db)
            held = semantic_intake_hold.has_held_root(ledger.db, token.project_id, token.message_id)
        except sqlite3.Error:
            raise ValueError('semantic_intake_hold_unverified') from None
        if held:
            return
        key = str(token.job_id)
        row = ledger.db.execute(
            'SELECT job_id FROM fetch_jobs WHERE job_id=? AND kind=? AND project_id=? AND message_id=?',
            (token.job_id, token.kind, token.project_id, token.message_id)).fetchone()
        raw = _bounded_job_payload(ledger.db, token.job_id, _REPROCESS_JOB_MAX_BYTES) if row else None
        if (row is None or raw != token.payload_raw
                or _known_long_defer_generation(raw) != token.generation
                or entries.get(key) != reservation[1]):
            return
        if reservation[0] is None:
            entries.pop(key)
        else:
            entries[key] = reservation[0]
        if entries:
            sched['long_defers'] = entries
        else:
            sched.pop('long_defers', None)
        _sched_write(ledger, sched, commit=False)


def _merge_long_defers(ledger, sched: dict, seen: dict) -> None:
    """Fold this pass's long defers into the persisted map and prune
    entries whose job left 'pending' or moved to a new generation."""
    previous = _long_defers(ledger)
    current = dict(previous)
    current.update(seen)
    if not current:
        sched.pop("long_defers", None)
        return
    alive = {}
    budget = _LONG_DEFERS_SCAN_BYTES
    cursor = sched.get('long_defers_scan_cursor', 0)
    if type(cursor) is not int or not 0 <= cursor < 2**63:
        cursor = 0
    last_scanned = None
    for row in ledger.db.execute(
            "SELECT job_id,state,project_id,message_id FROM fetch_jobs WHERE kind=? "
            "AND job_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY CASE WHEN job_id>? THEN 0 ELSE 1 END,job_id",
            (JOB_KIND, json.dumps(sorted(int(k) for k in current)), cursor)):
        key = str(row["job_id"])
        generation = None
        if budget > 0:
            last_scanned = row['job_id']
            limit = min(budget, _REPROCESS_JOB_MAX_BYTES)
            raw = _bounded_job_payload(ledger.db, row['job_id'], limit)
            if isinstance(raw, str):
                try:
                    size = len(raw.encode('utf-8'))
                except UnicodeError:
                    size = limit + 1
                if size <= limit:
                    budget -= size
                    generation = _known_long_defer_generation(raw)
                    if (generation is not None
                            and (row['state'] != 'pending' or current[key]['g'] != generation)
                            and _reprocess_job_payload(ledger, {'payload': raw},
                                                       row['project_id'], row['message_id']) is None):
                        generation = None
                else:
                    budget -= limit
            else:
                budget -= limit
        if generation is None:
            alive[key] = previous.get(key, current[key])
        elif row['state'] == 'pending' and current[key]["g"] == generation:
            alive[key] = current[key]
    if last_scanned is not None:
        sched['long_defers_scan_cursor'] = last_scanned
    if len(alive) > _LONG_DEFERS_MAX:
        raise ValueError('semantic_long_defers_capacity')
    if alive:
        sched["long_defers"] = alive
    else:
        sched.pop("long_defers", None)


_ARCHIVED_JOB = ("EXISTS (SELECT 1 FROM patients p "
                 "WHERE p.project_id=fetch_jobs.project_id AND p.is_archived)")
# an arrival-seeded job: a real notification event spawned it
_ELIGIBLE_SQL = "json_valid(payload) AND COALESCE(json_extract(payload,'$.eligible'),0)=1"
# Malformed rows have no usable version/arrival metadata. Select these only
# for the bounded raw-CAS quarantine before any admission or model call.
_MALFORMED_JOB_SQL = (
    "(CASE WHEN kind='semantic' AND typeof(payload)='text' "
    "AND length(CAST(payload AS BLOB))<=1048576 THEN CASE "
    "WHEN NOT json_valid(payload) THEN 1 WHEN json_type(payload)!='object' THEN 1 "
    "WHEN json_type(payload,'$.inference_engine_version') IS NULL AND ("
    "(json_type(payload,'$.targets') IS NOT NULL AND (json_type(payload,'$.targets')!='array' "
    "OR json_array_length(payload,'$.targets')=0)) OR "
    "(json_type(payload,'$.generation') IS NOT NULL AND json_type(payload,'$.generation')!='text')) "
    "THEN 1 ELSE 0 END ELSE 0 END)")


def _held_cursor_valid(value, lane):
    if (not isinstance(value, dict) or set(value) != {'job_id', 'rank'}
            or type(value['job_id']) is not int or not 0 < value['job_id'] < 2**63):
        return False
    rank = value['rank']
    return rank is None or (lane == 'realtime' and type(rank) in (int, float)
                            and abs(rank) < 2**63 and math.isfinite(rank))


def _held_cursor_for(job, lane):
    rank = None
    if lane == 'realtime':
        next_try, updated, created = job['next_try'], job['updated_at'], job['created_at']
        values = (next_try, updated, created)
        if any(value is not None and (type(value) not in (int, float)
                or abs(value) >= 2**63 or not math.isfinite(value)) for value in values):
            return None
        rank = updated if next_try is not None and updated is not None and next_try <= updated else created
    cursor = {'job_id': job['job_id'], 'rank': rank}
    return cursor if _held_cursor_valid(cursor, lane) else None


def _class_scope(archived: bool, excluded, *, lane=None, held_after=None) -> tuple[str, str]:
    """(patient-class + excluded-job SQL predicate, its json_each parameter)."""
    scope = (("" if archived else "NOT ") + _ARCHIVED_JOB
             + ' AND (' + current_meta_pred('fetch_jobs.payload')
             + ' OR (' + current_meta_pred('fetch_jobs.payload', target=LEGACY_VERSION)
             + " AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.eligible')=1 END)"
             + ' OR ' + _MALFORMED_JOB_SQL + ')'
             + " AND job_id NOT IN (SELECT value FROM json_each(?))")
    if _held_cursor_valid(held_after, lane):
        job_id = held_after['job_id']
        if lane == 'realtime':
            rank = held_after['rank']
            predicate = (f"({_REALTIME_KEY} IS NULL AND job_id<{job_id})" if rank is None else
                         f"({_REALTIME_KEY} IS NULL OR {_REALTIME_KEY}<{rank!r} OR "
                         f"({_REALTIME_KEY}={rank!r} AND job_id<{job_id}))")
        else:
            predicate = f'job_id>{job_id}'
        scope += f" AND (kind!='{JOB_KIND}' OR {predicate})"
    return scope, json.dumps(sorted(excluded))


def _adopt_pending_arrival(ledger, job):
    """Upgrade a selected legacy arrival from its actual unconsumed intent, without a corpus scan."""
    payload = runtime.parse_payload(job)
    if is_current(payload):
        return job
    if (read_version(payload) != LEGACY_VERSION or job['state'] != 'pending'
            or not payload.get('eligible') or payload.get('notification_free')):
        return None
    targets = payload.get('targets', [job['message_id']])
    if not isinstance(targets, list) or not targets or any(type(mid) is not int for mid in targets):
        return None
    version = ledger.db.execute('PRAGMA data_version').fetchone()[0]
    source = ledger._semantic_source_generation(job['project_id'], job['message_id'])
    origin = semantic_notice.origin_event_id(ledger.db, job['project_id'], targets)
    if ledger.db.in_transaction:
        raise ValueError('semantic_arrival_adoption_transaction_active')
    ledger.db.execute('BEGIN IMMEDIATE')
    with ledger.db:
        current = ledger.db.execute('SELECT * FROM fetch_jobs WHERE job_id=?', (job['job_id'],)).fetchone()
        import semantic_intake_hold
        try:
            semantic_intake_hold.validate_indexes(ledger.db)
            held = semantic_intake_hold.has_held_root(ledger.db, job['project_id'], job['message_id'])
        except sqlite3.Error:
            raise ValueError('semantic_intake_hold_unverified') from None
        if held:
            return None
        if (current is None or current['payload'] != job['payload'] or current['state'] != 'pending'
                or ledger.db.execute('PRAGMA data_version').fetchone()[0] != version):
            return None
        if origin is None:
            ledger.db.execute('UPDATE fetch_jobs SET next_try=? WHERE job_id=?',
                              (time.time() + 300, job['job_id']))
            return None
        ledger.semantic_seed_tx(job['project_id'], targets,
            {'source': 'unread', 'event_id': origin}, _source_generation=source)
    return ledger.db.execute('SELECT * FROM fetch_jobs WHERE job_id=?', (job['job_id'],)).fetchone()


def _due_lanes(ledger, kinds: tuple, max_jobs: int,
               archived: bool = False, *, excluded=(), turns=None, held_after=None) -> tuple[list, list, dict]:
    """Two-lane due selection with a persisted fairness share.

    Arrival-seeded jobs (payload.eligible=1 — a real notification event
    spawned them) keep priority, but the backfill cohort always gets a
    guaranteed share of this drain's window, so a continuous arrival
    stream can never starve runnable backlog within this patient class
    (GAP-6). At max_jobs < 4
    a single share slot would otherwise displace the only arrival slot:
    the persisted 'turn' marker alternates cohorts instead of
    permanently starving either.

    Returns (arrival_jobs, backfill_jobs, sched_mutation) — the mutation
    is written by the caller once the batch is consumed."""
    now = time.time()
    scope, excluded_json = _class_scope(archived, excluded, held_after=held_after)
    turn_key = "archived_turn" if archived else "turn"
    kind_ph = ",".join("?" * len(kinds))
    eligible = _ELIGIBLE_SQL
    arrivals_due = ledger.db.execute(f"""
      SELECT COUNT(*) FROM fetch_jobs
      WHERE state='pending' AND next_try <= ? AND kind IN ({kind_ph})
        AND {eligible} AND {scope}
    """, (now, *kinds, excluded_json)).fetchone()[0]
    backlog_due = ledger.db.execute(f"""
      SELECT COUNT(*) FROM fetch_jobs
      WHERE state='pending' AND next_try <= ? AND kind IN ({kind_ph})
        AND NOT ({eligible}) AND {scope}
    """, (now, *kinds, excluded_json)).fetchone()[0]
    share = 0
    turn_next = None
    if backlog_due:
        share = min(backlog_due, max(1, max_jobs // 4))
        if arrivals_due and share >= max_jobs:
            if max_jobs == 1:
                # one-slot drains alternate cohorts via the persisted
                # turn marker — neither starves permanently. The marker
                # records the cohort that won the last contested slot;
                # an absent marker keeps arrival first (its ordinary
                # priority), so backfill wins the SECOND contention.
                turn_next = ("arrival"
                             if (turns or _sched_state(ledger)).get(turn_key)
                             != "arrival" else "backfill")
                share = 1 if turn_next == "backfill" else 0
            else:
                share = max_jobs - 1
        # Unused arrival capacity can serve due backfill within the same
        # max_jobs/deadline/request budget. Preserve contested-slot turns.
        if arrivals_due < max_jobs - share:
            share = min(backlog_due, max_jobs - arrivals_due)
    order = "CASE WHEN kind=? THEN 0 ELSE 1 END, job_id"
    arrivals = ledger.db.execute(f"""
      SELECT * FROM fetch_jobs
      WHERE state='pending' AND next_try <= ? AND kind IN ({kind_ph})
        AND {eligible} AND {scope}
      ORDER BY {order} LIMIT ?
    """, (now, *kinds, excluded_json, JOB_KIND, max_jobs - share)).fetchall() \
        if max_jobs - share > 0 else []
    backfill = ledger.db.execute(f"""
      SELECT * FROM fetch_jobs
      WHERE state='pending' AND next_try <= ? AND kind IN ({kind_ph})
        AND NOT ({eligible}) AND {scope}
      ORDER BY {order} LIMIT ?
    """, (now, *kinds, excluded_json, JOB_KIND, share)).fetchall() if share else []
    mutation = {"turn": turn_next, "turn_key": turn_key}
    contested = arrivals or backfill
    if turn_next and contested:
        mutation["turn_job_id"] = contested[0]["job_id"]
    return list(arrivals), list(backfill), mutation


_BACKLOG_INVALIDATE_S = 300.0
# A backlog worker releases the run lock around every model/Jev call and
# must retake it before persisting the result. The tick (run_check) can
# hold that lock for its whole RUN_DEADLINE_S (480 s) after waiting
# LOCK_WAIT_S (150 s) itself; with the shared 150 s reacquire bound a call
# ending inside that window was abandoned (run_lock_lost), leaving the
# job untouched and exiting the resident (2026-10-09). Outwait a full
# tick instead — never past the pass deadline run_due was given.
RUN_LOCK_REACQUIRE_S = 660.0
# A tick can hold the run lock across its work window. Backlog work
# stops early enough for a result to wait out a tick and still be
# written before the job deadline.
BACKLOG_COMMIT_RESERVE_S = 540.0
# what a resumed pass may spend before its first (long-output) call
_LONG_START_S = 60.0


def _backlog_commit_reserve(scfg: dict) -> float:
    """The commit reserve the job budget affords while a long-output call
    still fits the work window: full from a ~1220 s budget, partial below
    (220 s at 900 s), never squeezing the long call out."""
    budget = float(scfg["job_budget_seconds"])
    return min(BACKLOG_COMMIT_RESERVE_S,
               max(0.0, budget - semantic_transport.LONG_MIN_CALL_S - _LONG_START_S))
# kind this resident backlog worker served last (in-process; a restart
# simply starts with semantic again)
_BACKLOG_TURN = {"kind": None}
_JOB_LOCK_BUCKETS = 1024
_invalidated_at: float | None = None


class _SchedulerUnavailable(Exception):
    pass


@contextmanager
def _job_lock(ledger, job_id):
    """Exclude duplicate inference for a job while ingestion keeps writing."""
    from writer_lock import acquire_run_lock
    db_path = ledger.db.execute("PRAGMA database_list").fetchone()[2]
    # Bucketed so the directory stays bounded (inodes are never unlinked,
    # see below). A bucket collision only makes the other job skip this
    # pass — the same path as a duplicate holder.
    path = os.path.join(os.path.dirname(db_path), "semantic_locks",
                        str(job_id % _JOB_LOCK_BUCKETS) + ".lock")
    fd = acquire_run_lock(path)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)
    # Keep the inode: unlinking a flock file can create two lock owners.


def _select_due(ledger, lane, kinds: tuple, qc_active: bool,
                max_jobs: int, long_defers: dict | None = None, *,
                excluded=(), turns=None, held_after=None, prioritize_patients=False) -> tuple:
    """Select active patients first, retaining each cohort's bounded fairness."""
    select_class = (partial(_select_due_class, prioritize_patients=True)
                    if prioritize_patients else _select_due_class)
    if lane == 'backlog' and not prioritize_patients:
        # Two resident lanes share this durable turn. Realtime arrivals keep
        # their existing active-first policy; an archive backlog cannot starve.
        try:
            with _sched_update(ledger) as state:
                archive_first = bool(state.get('reprocess_archive_turn'))
                state['reprocess_archive_turn'] = not archive_first
        except Exception:
            raise _SchedulerUnavailable from None
        def select(after):
            first = select_class(ledger, lane, kinds, qc_active, max_jobs,
                                      long_defers, archive_first,
                                      excluded=excluded, turns=turns, held_after=after)
            if first[0]:
                return first
            return select_class(ledger, lane, kinds, qc_active,
                                     max_jobs, long_defers, not archive_first,
                                     excluded=excluded, turns=turns, held_after=after)
    else:
        def select(after):
            active = select_class(
                ledger, lane, kinds, qc_active, max_jobs, long_defers, False,
                excluded=excluded, turns=turns, held_after=after)
            remaining = max_jobs - len(active[0])
            if remaining <= 0:
                return active
            archived = select_class(
                ledger, lane, kinds, qc_active, remaining, long_defers, True,
                excluded=excluded, turns=turns, held_after=after)
            due, qc_jobs, arrivals, backfill = [
                a + b for a, b in zip(active[:4], archived[:4])]
            return (due, qc_jobs, arrivals, backfill,
                    active[4] if active[4].get("turn") else archived[4])
    result = select(held_after)
    return select(None) if held_after and not result[0] else result


def _select_due_class(ledger, lane, kinds: tuple, qc_active: bool,
                      max_jobs: int, long_defers: dict | None,
                      archived: bool, *, excluded=(), turns=None, held_after=None,
                      prioritize_patients=False) -> tuple:
    """Due-job selection for one patient class in a drain pass.

    Returns (due, qc_jobs, arrivals, backfill, sched_mut): the ordered
    candidate list, its per-cohort split, and the fairness turn to persist.
    ``long_defers`` ({str(job_id): {"g", "n"}}) keeps jobs known to need
    the long-output budget out of the realtime lane.
    """
    scope, excluded_json = _class_scope(archived, excluded, lane=lane, held_after=held_after)
    if lane is None:
        qc_rows = []
        if qc_active and max_jobs > 1:
            qc_rows = ledger.db.execute(
                "SELECT * FROM fetch_jobs WHERE state='pending' "
                f"AND kind=? AND next_try<=? AND {scope} ORDER BY job_id LIMIT ?",
                (QC_JOB_KIND, time.time(), excluded_json, max_jobs)).fetchall()
        qc_share = min(len(qc_rows), max(1, max_jobs // 4), max_jobs - 1)
        qc_jobs = list(qc_rows[:qc_share])
        arrivals, backfill, sched_mut = _due_lanes(
            ledger, (JOB_KIND,) if qc_jobs else kinds,
            max_jobs - len(qc_jobs), archived, excluded=excluded, turns=turns, held_after=held_after)
        # unused lane capacity flows back to QC — same rule the backfill
        # cohort uses against an idle arrival lane
        spare = max_jobs - len(qc_jobs) - len(arrivals) - len(backfill)
        if spare > 0:
            qc_jobs += list(qc_rows[qc_share:qc_share + spare])
    else:
        # explicit lane: the fairness/QC selection is never computed
        # (it would be discarded), saving its queries per tick
        eligible = _ELIGIBLE_SQL
        # Fresh arrivals use realtime. Background workers may also finish
        # older arrival-seeded jobs; a per-job flock excludes double work.
        if lane == "realtime":
            # Newest SEED first (M6): a seed/re-seed/revival writes
            # next_try=updated_at, while every defer/retry pushes
            # next_try past updated_at — so a job that already took a
            # free defer ranks by its creation time instead of jumping
            # back to the head on each defer. Jobs known to need the
            # long-output budget never fit this lane and are left to
            # the backlog workers.
            # (same input generation only: a re-seed gets a new try)
            rows = ledger.db.execute(
                f"SELECT * FROM fetch_jobs WHERE state='pending' AND next_try<=? "
                f"AND kind=? AND {eligible} AND {scope} "
                "AND NOT EXISTS (SELECT 1 FROM json_each(?) s"
                " WHERE s.key=CAST(fetch_jobs.job_id AS TEXT)"
                " AND json_extract(s.value,'$.g')=CASE WHEN json_valid("
                "fetch_jobs.payload) THEN json_extract(fetch_jobs.payload,"
                "'$.generation') END) "
                f"ORDER BY {_REALTIME_ORDER} LIMIT ?",
                (time.time(), JOB_KIND, excluded_json, json.dumps(long_defers or {}),
                 max_jobs + 8 + len(long_defers or {}))).fetchall()
            # a payload from before the generation field: its generation
            # is the payload digest, which SQL cannot compute
            rows = [r for r in rows
                    if (long_defers or {}).get(str(r["job_id"]), {}).get("g")
                    != runtime.payload_generation(runtime.parse_payload(r))
                    ][:max_jobs + 8]
        else:
            # Re-extraction re-pends QC rows under their old (low) job_id;
            # one job_id order let them win every pass and no semantic job
            # ran for hours (2026-10-10). Each kind keeps oldest-first, and
            # the kind this worker served last goes second.
            # QC re-seeding refreshes next_try: its new generation must
            # queue behind QC that was already waiting, despite an old id.
            priority = patient_priority.activity_order_sql('fetch_jobs.project_id') + ', ' if prioritize_patients else ''
            rows = [row for kind in sorted(
                        kinds, key=lambda k: k == _BACKLOG_TURN["kind"])
                    for row in ledger.db.execute(
                        "SELECT * FROM fetch_jobs WHERE state='pending' "
                        f"AND next_try<=? AND kind=? AND {scope} ORDER BY "
                        + priority + f"{'next_try, job_id' if kind == QC_JOB_KIND else 'job_id'} LIMIT ?",
                        (time.time(), kind, excluded_json, max_jobs + 8)).fetchall()]
        # Include extra candidates so another worker's current job cannot
        # hide the next runnable item. Work remains bounded by max_jobs.
        qc_jobs = [j for j in rows if j['kind'] == QC_JOB_KIND]
        arrivals = [j for j in rows if j['kind'] == JOB_KIND
                    and runtime.parse_payload(j).get('eligible')]
        backfill = [j for j in rows if j['kind'] == JOB_KIND
                    and not runtime.parse_payload(j).get('eligible')]
        sched_mut = {"turn": None}
    due = (list(rows) if lane is not None else
           qc_jobs + list(arrivals) + list(backfill))
    return due, qc_jobs, arrivals, backfill, sched_mut


class _CloudSelectionIncomplete(Exception):
    """A cloud selection could not finish safely within the current pass."""


def _cloud_selection_identity(db, project_id, message_id):
    """Bind the receipt, source occurrences and attachments to one snapshot."""
    import cloud_policy
    anchor = db.execute('SELECT COALESCE(parent_id,message_id) FROM messages '
                        'WHERE project_id=? AND message_id=? '
                        'UNION ALL SELECT NULL UNION ALL SELECT NULL', (project_id, message_id))
    try:
        root = anchor.fetchone()[0]
        if root is None:
            return None
        scope = cloud_policy.history_thread_snapshot(db, project_id, message_id)
        bundle = semantic_store.thread_bundle(db, project_id, root, [message_id])
        if scope is None or bundle is None:
            return None
        return mcs_util.payload_hash({'scope': scope, 'bundle': bundle['source_fingerprint']})
    finally:
        anchor.close()


def _cloud_selection_memory_guard(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        global _cloud_selection_progress
        try:
            return fn(*args, **kwargs)
        except MemoryError as error:
            _cloud_selection_progress = None
            raise _CloudSelectionIncomplete('memory') from error
    return guarded


@_cloud_selection_memory_guard
def _select_cloud_due(ledger, lane, kinds, qc_active, max_jobs, long_defers=None, *,
                      excluded=(), turns=None, held_after=None, deadline=None, isolated=False):
    """Choose active patients and their newest activity before archived cloud work."""
    import cloud_inference
    import cloud_policy
    global _cloud_selection_progress
    if deadline is not None and time.monotonic() >= deadline:
        raise _CloudSelectionIncomplete('deadline')
    if isolated:
        # The outer connection may have a caller-owned cancellation handler.
        # Only the separate, read-only connection owns our deadline handler.
        from pathlib import Path
        from types import SimpleNamespace
        if ledger.db.in_transaction:
            raise _CloudSelectionIncomplete('active_transaction')
        try:
            path = ledger.db.execute('PRAGMA database_list').fetchone()[2]
            if not path:
                raise _CloudSelectionIncomplete('reader_unavailable')
            before = os.stat(path)
            reader = sqlite3.connect(Path(path).absolute().as_uri() + '?mode=ro',
                                     uri=True, timeout=0)
            try:
                after = os.stat(path)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise _CloudSelectionIncomplete('reader_changed')
                reader.row_factory = sqlite3.Row
                if deadline is not None:
                    reader.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                return _select_cloud_due(SimpleNamespace(db=reader), lane, kinds, qc_active,
                    max_jobs, long_defers, excluded=excluded, turns=turns, held_after=held_after,
                    deadline=deadline)
            finally:
                reader.close()
        except sqlite3.OperationalError as error:
            if deadline is not None and time.monotonic() >= deadline:
                raise _CloudSelectionIncomplete('deadline') from error
            raise _CloudSelectionIncomplete('reader_unavailable') from error
        except OSError as error:
            raise _CloudSelectionIncomplete('reader_unavailable') from error
    cfg = load_config()
    settings = cloud_inference.resolve_cloud_settings(cfg)
    if (lane != "backlog" or settings["mode"] != "on" or "semantic_backlog" not in settings["routes"]
            or not cloud_policy.selected_key()):
        _cloud_selection_progress = None
        return [], [], [], [], {"turn": None}
    semantic_settings = runtime_config(cfg)[0]
    semantic_scope = semantic_settings["project_ids"]
    # Caller-owned transactions may include uncommitted rows. Never retain a
    # cursor derived from them beyond this invocation.
    durable = not ledger.db.in_transaction
    identity = None
    if durable:
        path = ledger.db.execute('PRAGMA database_list').fetchone()[2]
        if path:
            try:
                stat = os.stat(path)
                identity = (os.path.realpath(path), stat.st_dev, stat.st_ino)
            except OSError:
                raise _CloudSelectionIncomplete('reader_unavailable')
    scope_row = ledger.db.execute(
        "SELECT artifact_id,content,meta FROM artifacts WHERE kind='cloud_history_scope' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    scope_identity = mcs_util.payload_hash(tuple(scope_row)) if scope_row else None
    binding = (identity, scope_identity, lane, tuple(kinds),
               mcs_util.payload_hash({'cloud': settings, 'semantic': semantic_settings}))
    if not _cloud_selection_lock.acquire(blocking=False):
        raise _CloudSelectionIncomplete('selection_busy')
    try:
        return _cloud_selection_page(ledger.db, kinds, max_jobs, settings, semantic_scope,
                                     excluded, deadline, binding if identity else None)
    finally:
        _cloud_selection_lock.release()


# One non-authoritative traversal per process. Only IDs and ordering metadata
# survive a tick; receipt, source and job data are always read again.
_cloud_selection_lock = threading.Lock()
_cloud_selection_progress = None
_CLOUD_SELECTION_PAGE = 128


def _cloud_selection_page(db, kinds, max_jobs, settings, semantic_scope, excluded, deadline, binding):
    import cloud_policy
    global _cloud_selection_progress

    def check_deadline():
        if deadline is not None and time.monotonic() >= deadline:
            raise _CloudSelectionIncomplete('deadline')

    check_deadline()
    progress = _cloud_selection_progress if binding is not None else None
    if progress is None or progress['binding'] != binding:
        upper = db.execute('SELECT COALESCE(MAX(job_id), 0) FROM fetch_jobs').fetchone()[0]
        progress = {'binding': binding, 'upper': upper, 'cursor': 0,
                    'rows': [], 'pending': [], 'collecting': True,
                    'yield_early': False}
        if binding is not None:
            _cloud_selection_progress = progress
    # PK range scan: the correlated MAX uses idx_messages_project_time.
    # A page is adopted only after its statement has fully completed.
    marks = ','.join('?' for _ in kinds)
    while progress['collecting']:
        check_deadline()
        rows = db.execute(
            'SELECT j.job_id,j.kind,j.project_id,COALESCE(p.is_archived,0), '
            + patient_priority.latest_activity_sql('j.project_id') + ' '
            'FROM fetch_jobs j NOT INDEXED LEFT JOIN patients p ON p.project_id=j.project_id '
            'WHERE j.job_id>? AND j.job_id<=? '
            'ORDER BY j.job_id LIMIT ?',
            (progress['cursor'], progress['upper'], _CLOUD_SELECTION_PAGE)).fetchall()
        if rows:
            for job_id, kind, project_id, archived, latest in rows:
                if kind in kinds:
                    # SQLite orders NULL timestamps last for DESC and NULL
                    # project IDs first for ASC.
                    heapq.heappush(progress['rows'], (
                        *patient_priority.priority_key(project_id, latest, archived=bool(archived)), job_id))
            progress['cursor'] = rows[-1][0]
        if len(rows) < _CLOUD_SELECTION_PAGE:
            progress['collecting'] = False
    check_deadline()
    selected, snapshots = [], {}
    pending = progress['pending']
    pending_pos = 0
    try:
        with cloud_policy.history_selection_check(db) as receipt_check:
            while pending_pos < len(pending) or progress['rows']:
                check_deadline()
                from_pending = pending_pos < len(pending)
                rank = pending[pending_pos] if from_pending else progress['rows'][0]
                job_id = rank[-1]
                if job_id in excluded:
                    if from_pending:
                        pending.pop(pending_pos)
                    else:
                        heapq.heappop(progress['rows'])
                    continue
                row = db.execute(
                    'SELECT j.* FROM fetch_jobs j WHERE j.job_id=? '
                    f"AND j.kind IN ({marks}) AND j.state='pending' AND j.next_try<=? "
                    'AND json_valid(j.payload) AND ' + current_meta_pred('j.payload') + ' '
                    'AND (? IS NULL OR j.project_id IN (SELECT value FROM json_each(?))) '
                    'AND ' + cloud_policy.history_scope_target_sql('j.message_id') + ' '
                    'AND j.project_id NOT IN (SELECT value FROM json_each(?)) '
                    'AND (? IS NULL OR j.project_id IN (SELECT value FROM json_each(?)))',
                    (job_id, *kinds, time.time(),
                     json.dumps(settings['project_ids']) if settings['project_ids'] is not None else None,
                     json.dumps(settings['project_ids']), json.dumps(settings['exclude_project_ids']),
                     json.dumps(semantic_scope) if semantic_scope is not None else None,
                     json.dumps(semantic_scope))).fetchone()
                eligible = False
                if (row is not None and cloud_policy.history_target_ready(db, row['project_id'], row['message_id'])
                        and receipt_check(row['project_id'], row['message_id'])):
                    snapshot = _cloud_selection_identity(db, row['project_id'], row['message_id'])
                    check_deadline()
                    if snapshot is not None:
                        eligible = True
                        snapshots[row['job_id']] = snapshot
                        selected.append(row)
                if from_pending:
                    if eligible:
                        pending_pos += 1
                    else:
                        pending.pop(pending_pos)
                else:
                    heapq.heappop(progress['rows'])
                    if eligible:
                        pending.append(rank)
                        pending_pos += 1
                if progress['yield_early'] and len(selected) >= max(1, max_jobs):
                    break
                if len(selected) >= min(512, max_jobs + max(8, settings['max_concurrency'] * 2)):
                    break
            check_deadline()
    except BaseException:
        # Retain only IDs/ranks, and do not rescan an already rejected suffix.
        # On the next tick revalidate accepted IDs and return promptly once
        # a useful batch is ready, even if the lookahead could not finish.
        if pending:
            progress['yield_early'] = True
        raise
    progress['yield_early'] = False
    if not progress['rows'] and not pending and binding is not None:
        _cloud_selection_progress = None
    semantic = [r for r in selected if r['kind'] == JOB_KIND]
    arrivals = [r for r in semantic if runtime.parse_payload(r).get('eligible')]
    backfill = [r for r in semantic if not runtime.parse_payload(r).get('eligible')]
    return (selected, [r for r in selected if r['kind'] == QC_JOB_KIND], arrivals, backfill,
            {'turn': None, 'cloud_snapshots': snapshots})


def _refill_due(ledger, lane, kinds, qc_active, max_jobs, long_defers,
                first_batch, selected, out, deadline, consumed_turns, held_after=None,
                selector=None):
    """Refill skipped slots without revisiting jobs or scanning an unbounded queue."""
    inspected = set()
    batch = first_batch
    scan_limit = max_jobs * (64 if selector is not None else 16)
    select = selector or _select_due
    while len(inspected) < scan_limit and sum(selected.values()) < max_jobs:
        before = len(inspected)
        due, qc_jobs, arrivals, backfill, mutation = batch
        if not due:
            return
        cohort_of = {row["job_id"]: name for name, rows in (
            ("qc", qc_jobs), ("arrival", arrivals), ("backfill", backfill))
            for row in rows}
        for job in due:
            if (len(inspected) >= scan_limit or sum(selected.values()) >= max_jobs
                    or time.monotonic() >= deadline):
                return
            job_id = job["job_id"]
            if job_id in inspected:
                continue
            inspected.add(job_id)
            cohort_name = cohort_of[job_id]
            out["lanes"][cohort_name] += 1
            yield job, cohort_name, mutation
        if len(inspected) == before or time.monotonic() >= deadline:
            return
        remaining = max_jobs - sum(selected.values())
        if remaining <= 0 or len(inspected) >= scan_limit:
            return
        turns = {**_sched_state(ledger), **consumed_turns}
        try:
            batch = select(ledger, lane, kinds, qc_active, remaining,
                                long_defers, excluded=inspected, turns=turns, held_after=held_after)
        except _CloudSelectionIncomplete as error:
            out['cloud_selection_incomplete'] = True
            out['cloud_selection_reason'] = str(error)
            return
        except _SchedulerUnavailable:
            out['long_defer_reservation_failed'] = True
            return


def _job_metric(job, token, cohort: str, status, job_age: float,
                job_elapsed: float, llm_s: float, jev_s: float, used: int,
                usage: dict, jev_client, *, diagnostics: dict) -> dict:
    """The bounded per-job metrics row of one drain pass."""
    return {
        "job_id": job["job_id"], "project_id": job["project_id"],
        "kind": job["kind"],
        "cohort": "qc" if job["kind"] == QC_JOB_KIND else cohort,
        "generation": token.generation,
        "payload_digest": token.payload_digest, "attempts": token.attempts,
        "status": status if status is not None else "error",
        # phase split — queue wait vs model call vs Jev evaluation
        # vs the residual validate/render phase (T14)
        "queue_wait_s": job_age,
        "llm_s": llm_s,
        "jev_s": jev_s,
        "post_s": max(0.0, job_elapsed - llm_s - jev_s),
        "elapsed_s": job_elapsed, "job_age_s": job_age,
        "jev_requests": used, "usage": usage,
        # last Jev error class/kind of this pass — a job that ends
        # "failed" after a few Jev calls was previously
        # undiagnosable from the run record (2026-09-30)
        "jev_error": _jev_error_brief(
            getattr(jev_client, "last_error", None))
        if status not in (None, "done") else None,
        # last local-model failure code of this pass (never
        # text) — a long call ending "retry" was undiagnosable
        # from the run record (2026-10-09). Module-level and
        # latest-wins: only a pass that actually called the
        # model may report it, or a cached/QC pass would wear
        # the previous job's code.
        "llm_error": semantic_transport.last_llm_failure()
        if status not in (None, "done") and llm_s > 0 else None,
        "retry_reason": diagnostics["code"]
        if status != "done" else None,
        "summary_validation_error": diagnostics["summary_validation_error"],
        "loops_rejected": diagnostics["loops_rejected"],
    }


def _defer_note(out: dict, reason: str) -> None:
    """Count a prepare-phase defer so its reason reaches defer_reasons."""
    out["deferred"] += 1
    counts = out.setdefault("defer_reasons", {})
    counts[reason] = counts.get(reason, 0) + 1


def _apply_job_status(ledger, token, limit: int, status: str,
                      budget_short: bool, out: dict) -> bool:
    """Map one finished job's status onto its queue transition and the
    pass counters; True when the pass must stop (no further call fits)."""
    if status == "done":
        out["done"] += 1
    elif status in ("deferred", "deferred_short"):
        out["deferred"] += 1
        runtime.transition(ledger, token, "defer", retry_in=60)
        if status == "deferred_short":
            # less than one call reserve is left: the next job's
            # first call is exempt from the reserve gate and would
            # be dispatched only to time out at the deadline
            return True
    elif status == "deferred_backoff":
        out["deferred"] += 1
        runtime.transition(ledger, token, "defer",
                           retry_in=NOT_SENT_BACKOFF_S)
    elif status == "retry":
        runtime.transition(ledger, token, "retry", retry_in=300,
                           max_attempts=limit)
        out["deferred"] += 1
        if budget_short:
            return True  # same reason as deferred_short: no call fits
    elif status == "failed":
        runtime.transition(ledger, token, "retry", max_attempts=1)
        out["failed"] += 1
    elif status == "stale":
        # The worker that observed this row no longer owns it.  Do not
        # defer/retry/done the replacement generation by ID alone.
        out["deferred"] += 1
    else:
        out["failed"] += 1
    return False


def _observation(started_at: float, finished_at: float, generation,
                 lane, metrics: list) -> dict:
    """The wall-clock window of one drain pass; ``complete`` only when
    every processed job fits the bounded artifact and the clock ran
    forward across the pass and each completion."""
    stamps = [started_at, *(m["completed_at"] for m in metrics
                            if "completed_at" in m), finished_at]
    complete = (len(metrics) <= 64
                and all(math.isfinite(t) for t in stamps)
                and all(a <= b for a, b in zip(stamps, stamps[1:])))
    return {"version": 1, "started_at": started_at, "finished_at": finished_at,
            "config_generation": generation, "complete": complete, "lane": lane}


def _persist_drain_accounting(ledger, scfg, out: dict, result: dict,
                              selected: dict, qc_active: bool,
                              sched_mut: dict, long_seen: dict | None = None,
                              long_defers: dict | None = None) -> None:
    """Persist the fairness counters and the bounded drain-run metrics
    artifact; either failure is recorded in ``result`` and never breaks
    the drain. This pass's long-output defers (``long_seen``) are folded
    into the persisted map alongside the fairness counters."""
    # persisted fairness accounting — survives restarts, feeds
    # semantic_observe's scheduler section (T14)
    try:
        with _sched_update(ledger) as sched:
            for cohort in ("arrival", "backfill"):
                sched[cohort + "_selected"] = \
                    sched.get(cohort + "_selected", 0) + selected[cohort]
            if qc_active:
                sched["qc_selected"] = \
                    sched.get("qc_selected", 0) + selected["qc"]
            if selected["backfill"]:
                sched["backfill_last_served_at"] = time.time()
            if selected["qc"]:
                sched["qc_last_served_at"] = time.time()
            if sched_mut.get("turn") and sched_mut.get("turn_consumed"):
                sched[sched_mut.get("turn_key", "turn")] = sched_mut["turn"]
            sched.update(sched_mut.get("consumed_turns", {}))
            if sched_mut.get('hold_cursor_changed'):
                cursors = dict(sched.get('payload_hold_cursors', {}))
                key = sched_mut['hold_cursor_lane']
                cursor = sched_mut.get('hold_cursor')
                if cursors.get(key) == sched_mut.get('hold_cursor_expected'):
                    if cursor is None:
                        cursors.pop(key, None)
                    else:
                        cursors[key] = cursor
                    if cursors:
                        sched['payload_hold_cursors'] = cursors
                    else:
                        sched.pop('payload_hold_cursors', None)
            if long_seen or long_defers:
                _merge_long_defers(ledger, sched, long_seen or {})
    except Exception:
        # accounting must never break the drain itself
        result["errors"].append("semantic: sched_persist_failed")
    # Bounded per-run metrics artifact — observe() aggregates the
    # recent ones; a drain that attempted work leaves durable
    # evidence instead of an in-memory-only result (T14).
    try:
        ledger.artifact_add(
            KIND_DRAIN_RUN,
            json.dumps({
                "v": 1, "mode": scfg["mode"],
                "done": out["done"], "deferred": out["deferred"],
                "failed": out["failed"],
                "budget_exhausted": out["budget_exhausted"],
                "left": out["left"], "elapsed_s": out["elapsed_s"],
                "oldest_pending_age_s":
                    out["oldest_pending_job_age_s"],
                "lanes": out["lanes"],
                "job_metrics": out["job_metrics"][:64],
                "observation": out.get("observation")},
                ensure_ascii=False),
            model="semantic_drain")
    except Exception:
        result["errors"].append("semantic: metrics_persist_failed")


def run_due(ledger, cfg: dict, result: dict, deadline: float,
            jev_client=None, llm_fn=None, max_jobs: int = 4,
            cfg_path: str | None = None, run_lock_fd=None,
            lane: str | None = None, cloud_only: bool = False,
            prioritize_patients: bool = False) -> dict:
    """Drain due semantic jobs inside the tick's remaining budget.
    OFF returns immediately — no job creation, no external calls, no
    auto-drain (AT-053). The caller's shared run lock is held by
    run_check; a standalone CLI takes acquire_run_lock itself.
    cfg_path, when given, re-validates config between jobs so an OFF
    flip mid-run takes effect at the next job boundary (AT-060)."""
    started_at = time.perf_counter()
    started_wall = time.time()
    if lane not in (None, "realtime", "backlog"):
        raise ValueError("semantic_lane_invalid")
    if cloud_only and (lane != "backlog" or llm_fn is not None):
        raise ValueError("semantic_cloud_lane_invalid")
    if type(max_jobs) is not int or not 1 <= max_jobs <= 32:
        raise ValueError("semantic_max_jobs_invalid")
    scfg, errors = runtime_config(cfg)
    cfg_generation = runtime.config_generation(cfg)
    observation_config_changed = False
    for e in errors:
        if e not in result["errors"]:
            result["errors"].append(e)
    out = {"mode": scfg["mode"], "done": 0, "deferred": 0,
           "failed": 0, "left": None, "budget_exhausted": False,
           # jobs whose pass persisted at least one new stage artifact
           # (usage receipts excluded) — "no completion" is not "no
           # progress" for the tick's starvation guard (2026-09-30)
           "progressed": 0}
    if scfg['mode'] != 'off':
        try:
            initial_sched, _ = _reservation_sched(ledger)
        except Exception:
            result['errors'].append('semantic: long_defer_reservation_failed')
            out['long_defer_reservation_failed'] = True
            return out
    global _invalidated_at
    # The scan is O(corpus) (thread_bundle per canonical thread) and the
    # backlog resident calls run_due once per job under the run lock, so
    # that lane refreshes at most every _BACKLOG_INVALIDATE_S; the tick
    # (_semantic_enabled + realtime lane) still runs it every pass.
    now_m = time.monotonic()
    if not cloud_only and (lane != "backlog" or _invalidated_at is None \
            or now_m - _invalidated_at >= _BACKLOG_INVALIDATE_S):
        from semantic_publication import invalidate_projections
        invalidate_projections(ledger.db, scfg)
        _invalidated_at = time.monotonic()
    if scfg["mode"] == "off":
        return out
    if not cloud_only:
        try:
            out['inference_reprocess'] = reprocess_step(ledger, cfg, deadline=deadline)
        except Exception as error:
            out['inference_reprocess'] = {'status': 'error', 'complete': False}
            result['errors'].append(f'semantic_reprocess: {type(error).__name__}')
    # Stability guards shared with the extract lane: a nearly-full
    # volume turns per-job artifact writes into an I/O error storm, and
    # an open endpoint breaker means every llm_fn call pays a dead
    # timeout — defer the whole window instead of churning jobs.
    free_mb = llm_availability.disk_free_mb(ledger.db)
    if free_mb is not None \
            and free_mb < llm_availability.disk_floor_mb():
        out["disk_free_mb"] = round(free_mb)
        return out
    # Deterministic maintenance (no model, no Jev — not gated by the
    # endpoint breaker or LLM admission): bring current projection/v4
    # rows minted by an older projection version up to date, a bounded
    # slice per tick, right after this tick's invalidation pass.
    if not cloud_only:
        try:
            out["reproject"] = v4.reproject_stale(ledger, scfg)
        except Exception as e:
            result["errors"].append(f"semantic_reproject: {type(e).__name__}")
    circuit_s = llm_availability.circuit_open_s(ledger.db)
    if circuit_s and not cloud_only:
        out["circuit_open_s"] = round(circuit_s)
        return out
    drain_started = time.perf_counter()
    # maintenance ahead of the job loop, and jobs skipped without a
    # transition — both invisible from the run record until 2026-10-09
    out["preamble_s"] = round(drain_started - started_at, 3)
    out["lock_skipped"] = {}
    out["floor_skipped"] = {}
    out["job_metrics"] = []
    oldest = ledger.db.execute(
        "SELECT MIN(created_at) FROM fetch_jobs "
        "WHERE kind IN (?,?) AND state='pending'",
        (JOB_KIND, QC_JOB_KIND)).fetchone()[0]
    out["oldest_pending_job_age_s"] = (
        max(0.0, time.time() - oldest) if oldest is not None else None)
    out["pending_by_kind"] = {
        r["kind"]: r["n"] for r in ledger.db.execute(
            "SELECT kind, COUNT(*) AS n FROM fetch_jobs "
            "WHERE kind IN (?,?) AND state='pending' GROUP BY kind",
            (JOB_KIND, QC_JOB_KIND)).fetchall()}
    out["done_by_kind"] = {}
    policy = policy_fingerprint(scfg)
    previous_policy = ledger.db.execute(
        "SELECT content FROM artifacts WHERE kind=? "
        "ORDER BY artifact_id DESC LIMIT 1", (KIND_POLICY,)).fetchone()
    if previous_policy is None or previous_policy["content"] != policy:
        ledger.artifact_add(KIND_POLICY, policy)
    cloud_backlog = llm_fn is None and lane == "backlog"
    if llm_fn is None:
        llm_fn = semantic_transport.llm_chat
    if jev_client is None and scfg["daily_request_budget"] > 0:
        jev_key = jev.api_key()
        jev_client = jev.JevClient(
            api_key=jev_key, key_current=lambda: bool(jev_key) and jev.api_key() == jev_key,
            model=scfg["model"],
            attempt_timeout=scfg["attempt_timeout_seconds"],
            job_budget=scfg["job_budget_seconds"],
            max_attempts=scfg["max_attempts_per_try"])
    from writer_lock import unlocked_transport
    original_llm = llm_fn
    # the tick keeps the shared default: it never has a tick to outwait
    reacquire_s = RUN_LOCK_REACQUIRE_S if lane == "backlog" else None
    # Log-only (never in the drain_run artifact, no job identity): seconds
    # in transport vs. waiting to retake the run lock, and the window a
    # lost reacquire had — llm_s includes both (2026-10-10: losses landed
    # inside a tick's hold with ~30 s of the pass left).
    transport = out["transport"] = {
        "llm_transport_s": 0.0, "llm_reacquire_s": 0.0,
        "jev_transport_s": 0.0, "jev_reacquire_s": 0.0}
    # Log-only totals belong to this pass output; only its explicit stage chain may add to them.
    shadow_stats = out["shadow_v2"] = {"calls": 0, "incomplete": 0, "s": 0.0}

    def _unlocked(kind, call, *args, **kwargs):
        started, ended = time.monotonic(), None
        try:
            with unlocked_transport(ledger.db, run_lock_fd, wait_s=reacquire_s,
                                    deadline=deadline):
                try:
                    return call(*args, **kwargs)
                finally:
                    ended = time.monotonic()
        except writer_lock.RunLockLost:
            if ended is not None:
                wait = writer_lock.RUN_LOCK_REACQUIRE_S if reacquire_s is None \
                    else reacquire_s
                out["run_lock_lost_detail"] = {
                    "kind": kind, "transport_s": round(ended - started, 1),
                    "reacquire_s": round(time.monotonic() - ended, 1),
                    "window_s": round(max(0.0, min(wait, deadline - ended)), 1)}
            raise
        finally:
            if ended is not None:
                # raw sums: rounding each call would drop short calls
                transport[f"{kind}_transport_s"] += ended - started
                transport[f"{kind}_reacquire_s"] += time.monotonic() - ended

    # Endpoint health shared with the extract lane's breaker: a refused
    # connection / 503 never consumes an attempt here (LLMNotSent -> free
    # defer, which also refreshes updated_at), so without this feed an
    # outage of any length stayed invisible to the breaker and the stall
    # alert. Counted once per pass; an answered call resets the streak.
    llm_health = {"down": False, "fed": False}
    marks_unsaved0 = semantic_transport.long_mark_failures()
    long_defers = _long_defers(ledger)

    @wraps(original_llm)
    def unlocked_llm(*args, **kwargs):
        try:
            text = _unlocked("llm", original_llm, *args, **kwargs)
        except runtime.LLMNotSent as e:
            if getattr(e, "stage", "") in _LLM_DOWN_REASONS:
                if not llm_health["fed"]:
                    llm_availability.circuit_failure(ledger.db)
                llm_health["down"] = llm_health["fed"] = True
            raise
        if isinstance(text, str):
            llm_availability.circuit_success(ledger.db)
            llm_health["down"] = False
        return text

    llm_fn = unlocked_llm
    def unlocked_jev_request(call, *args, **kwargs):
        return _unlocked("jev", call, *args, **kwargs)

    jev_transport = unlocked_jev_request if run_lock_fd is not None else None
    if not cloud_only and scfg["mode"] != "off" and scfg["extract_qc"] == "annotate":
        try:
            qc_cap = scfg["daily_request_budget"]
            if lane == "backlog":
                qc_cap -= math.ceil(qc_cap * BACKLOG_BUDGET_RESERVE)
            qc_skip_mode = (jev_client is None or
                            not getattr(jev_client, "api_key", True) or
                            runtime.circuit_open(ledger) or jev_usage_today(ledger) >= qc_cap)
            if qc_skip_mode:
                semantic_qc._qc_seed(ledger, time.time(), skip_mode=True)
            else:
                semantic_qc._qc_seed(ledger, time.time())
        except Exception:
            result["errors"].append("semantic: qc_seed_failed")
    qc_active = scfg["extract_qc"] == "annotate"
    kinds = (JOB_KIND, QC_JOB_KIND) if qc_active else (JOB_KIND,)
    # arrival-descended seeds outrank import/replay seeds: a deep
    # backfill must never starve a fresh notification's evaluation
    # (§19.1's existing-work-first ordering applied inside the queue
    # too). 'eligible' is set at seed time and survives payload merges,
    # so a merged arrival+history job keeps its priority.
    # T14: the backfill cohort additionally gets a guaranteed share of
    # every window — a continuous arrival stream cannot starve runnable
    # backlog (persisted accounting row keeps it restart-safe).
    # These shares apply within each patient class; archived patients
    # receive only capacity left by active patients.
    # QC dedicated frame: kind ordering puts semantic rows ahead of QC
    # in both cohorts. Long generations can consume the whole window
    # and starve QC without attempting it. QC gets a bounded share,
    # served FIRST
    # (each pass is one bounded chunked eval), expanding into whatever
    # the semantic lanes leave unused. A 1-job window cannot be split —
    # QC falls back to the persisted cohort alternation inside the
    # non-eligible lane.
    hold_lane = lane or 'combined'
    held_after = initial_sched.get('payload_hold_cursors', {}).get(hold_lane)
    def select_cloud(*args, **kwargs):
        with unlocked_transport(ledger.db, run_lock_fd, wait_s=reacquire_s, deadline=deadline):
            return _select_cloud_due(*args, **kwargs, deadline=deadline,
                                     isolated=run_lock_fd is not None)
    try:
        cursor_arg = {'held_after': held_after} if held_after is not None else {}
        selector = (select_cloud if cloud_only else
                    partial(_select_due, prioritize_patients=True) if prioritize_patients else _select_due)
        first_batch = selector(ledger, lane, kinds, qc_active, max_jobs, long_defers,
                                  **cursor_arg)
    except _CloudSelectionIncomplete as error:
        out['cloud_selection_incomplete'] = True
        out['cloud_selection_reason'] = str(error)
        return out
    except _SchedulerUnavailable:
        result['errors'].append('semantic: long_defer_reservation_failed')
        out['long_defer_reservation_failed'] = True
        return out
    due = first_batch[0]
    consumed_turns = {}
    sched_mut = {"consumed_turns": consumed_turns, 'hold_cursor_expected': held_after}
    saw_held = False

    def hold_job(job, mutation, reason='semantic: job_payload_held'):
        nonlocal saw_held
        saw_held = True
        out['payload_hold_count'] = out.get('payload_hold_count', 0) + 1
        reasons = out.setdefault('hold_reasons', [])
        if reason not in reasons:
            reasons.append(reason)
        if reason not in result['errors']:
            result['errors'].append(reason)
        cursor = _held_cursor_for(job, lane)
        if cursor is not None:
            sched_mut.update(hold_cursor_changed=True, hold_cursor_lane=hold_lane,
                             hold_cursor=cursor)
        if mutation.get('turn') and job['job_id'] == mutation.get('turn_job_id'):
            consumed_turns[mutation.get('turn_key', 'turn')] = mutation['turn']
    out["lanes"] = {"arrival": 0, "backfill": 0}
    if qc_active:
        out["lanes"]["qc"] = 0
    selected = {"qc": 0, "arrival": 0, "backfill": 0}
    for job, cohort_name, mutation in _refill_due(
            ledger, lane, kinds, qc_active, max_jobs, long_defers,
            first_batch, selected, out, deadline, consumed_turns, held_after,
            selector=selector if cloud_only or prioritize_patients else None):
        if llm_health["down"] and job["kind"] == JOB_KIND:
            # the endpoint refused this pass: the remaining model jobs
            # would each only re-defer (and rewrite updated_at); they
            # stay due for the next pass. QC rows need no model.
            continue
        with _job_lock(ledger, job["job_id"]) as held:
            if not held:
                out["lock_skipped"][job["kind"]] = \
                    out["lock_skipped"].get(job["kind"], 0) + 1
                continue
            if cloud_only:
                # Selection ran without the writer lock. Re-read the full job
                # and its live admission before any transition or model call.
                import cloud_policy
                import cloud_inference
                fresh = ledger.db.execute('SELECT * FROM fetch_jobs WHERE job_id=?',
                                          (job['job_id'],)).fetchone()
                live_cfg = load_config(cfg_path) if cfg_path is not None else load_config()
                live_settings = cloud_inference.resolve_cloud_settings(live_cfg)
                live_scfg, live_errors = runtime_config(live_cfg)
                project = job['project_id']
                if (time.monotonic() >= deadline or fresh is None or tuple(fresh) != tuple(job) or live_errors
                        or live_scfg['mode'] == 'off' or live_settings['mode'] != 'on'
                        or 'semantic_backlog' not in live_settings['routes']
                        or not cloud_policy.selected_key()
                        or live_settings['project_ids'] is not None and project not in live_settings['project_ids']
                        or project in live_settings['exclude_project_ids']
                        or live_scfg['project_ids'] is not None and project not in live_scfg['project_ids']
                        or not cloud_policy.history_target_ready(ledger.db, project, job['message_id'])
                        or not mutation.get('cloud_snapshots', {}).get(job['job_id'])
                        or mutation['cloud_snapshots'][job['job_id']] != _cloud_selection_identity(
                            ledger.db, project, job['message_id'])):
                    out['cloud_selection_changed'] = True
                    continue
                job = fresh
            if job['kind'] == JOB_KIND:
                import semantic_intake_hold
                try:
                    semantic_intake_hold.validate_indexes(ledger.db)
                    if (_known_long_defer_generation(job['payload']) is None
                            and semantic_intake_hold.quarantine_invalid_job(ledger, job)):
                        out['failed'] += 1
                        continue
                    intake_held = semantic_intake_hold.has_held_root(
                        ledger.db, job['project_id'], job['message_id'])
                except sqlite3.Error:
                    result['errors'].append('semantic: intake_hold_unverified')
                    out['intake_hold_unverified'] = True
                    break
                if intake_held:
                    hold_job(job, mutation, 'semantic: intake_held')
                    continue
            if job['kind'] == JOB_KIND and _known_long_defer_generation(job['payload']) is None:
                hold_job(job, mutation)
                continue
            if job['kind'] == JOB_KIND and not is_current(runtime.parse_payload(job)):
                try:
                    job = _adopt_pending_arrival(ledger, job)
                except ValueError as error:
                    if str(error) != 'semantic_intake_hold_unverified':
                        raise
                    result['errors'].append('semantic: intake_hold_unverified')
                    out['intake_hold_unverified'] = True
                    break
                if job is None:
                    continue
            token = runtime.JobToken.from_row(job)
            payload = runtime.parse_payload(job)
            limit = runtime.attempt_limit(payload)
            job_skip = None
            if runtime.circuit_open(ledger):
                out["circuit_open"] = True
                circuit = runtime._circuit_state(ledger)
                if circuit["failure_class"] not in runtime._CIRCUIT_FAILURE_CLASSES:
                    break
                job_skip = "circuit_open"
            from mcs_queries import paused
            if paused(ledger.db, job["project_id"]):
                runtime.transition(ledger, token, "defer", retry_in=300)
                _defer_note(out, "paused")
                continue
            if cfg_path is not None:
                current_cfg = load_config(cfg_path)
                scfg, errs = runtime_config(current_cfg)
                errors = errs
                current_generation = runtime.config_generation(current_cfg)
                observation_config_changed |= current_generation != cfg_generation
                cfg_generation = current_generation
                for e in errs:
                    if e not in result["errors"]:
                        result["errors"].append(e)
                if scfg["mode"] == "off":
                    runtime.transition(ledger, token, "defer", retry_in=300)
                    _defer_note(out, "mode_off")
                    out["mode"] = "off"
                    break
            # A semantic job's first call is exempt from the in-call reserve
            # gate. Starting below that reserve can burn a doomed
            # generation and a retry attempt before any useful progress.
            # Only start one when a call can plausibly fit, using the same
            # reserve applied to follow-up calls. Further stages defer
            # via BudgetShort without charging an attempt. Trailing QC
            # rows use their separate start floor.
            floor = deadline - (15.0 if job["kind"] == QC_JOB_KIND else
                                min(runtime.LLM_CALL_RESERVE_S,
                                    float(scfg["job_budget_seconds"]) * 2 / 3))
            if lane == "backlog":
                floor -= _backlog_commit_reserve(scfg)
            if time.monotonic() > floor:
                out["floor_skipped"][job["kind"]] = \
                    out["floor_skipped"].get(job["kind"], 0) + 1
                if job["kind"] == QC_JOB_KIND:
                    _defer_note(out, "deadline")
                    break
                continue
            if scfg["project_ids"] is not None \
                    and job["project_id"] not in scfg["project_ids"]:
                # outside the rollout scope — defer instead of leaving it
                # due-now, so a backlog of out-of-scope rows cannot fill
                # the whole max_jobs window and starve in-scope work. The
                # row stays pending: a later project_ids change resumes it.
                runtime.transition(ledger, token, "defer", retry_in=300)
                _defer_note(out, "out_of_scope")
                continue
            if token.attempts < 0 or token.attempts >= limit:
                # A row can be re-seeded or manually edited while it is waiting
                # in this due-list snapshot.  Close an exhausted row with the
                # complete token CAS before any client, budget, or network path.
                # Keep OFF/paused/out-of-scope rows untouched; they retain the
                # existing queue hold contract until the feature can run again.
                if runtime.transition(ledger, token, "failed"):
                    out["failed"] += 1
                else:
                    _defer_note(out, "commit_rejected")
                continue
            lane_cap = scfg["daily_request_budget"]
            if lane == "backlog":
                lane_cap -= math.ceil(lane_cap * BACKLOG_BUDGET_RESERVE)
            if jev_client is None:
                job_skip = job_skip or "disabled"
            elif not getattr(jev_client, "api_key", True):
                job_skip = job_skip or "not_configured"
            if jev_client is not None and job_skip is None:
                # the daily cap binds at REQUEST granularity, not just job
                # granularity — one job's claim-audit + loop-relation calls
                # must not overshoot it mid-flight (§13.5)
                remaining = lane_cap - jev_usage_today(ledger)
                if remaining <= 0:
                    # Compatibility field: the Jev quota is unavailable;
                    # the semantic job itself may still continue.
                    out["budget_exhausted"] = True
                    job_skip = "backlog_reserved" if lane == "backlog" else "daily_budget"
                else:
                    jev_client.request_cap = (jev_client.requests_made
                                              + remaining)
                if hasattr(jev_client, "attempt_timeout"):
                    jev_client.attempt_timeout = scfg["attempt_timeout_seconds"]
                if hasattr(jev_client, "job_budget"):
                    jev_client.job_budget = scfg["job_budget_seconds"]
            if job_skip is not None:
                try:
                    if errors:
                        raise ValueError("semantic_config_invalid")
                    runtime._jev_window_check(config_path=cfg_path)
                    jev_usage_today(ledger)
                except (jev.JevError, ValueError, sqlite3.Error):
                    if "semantic: jev_skip_unverified" not in result["errors"]:
                        result["errors"].append("semantic: jev_skip_unverified")
                    runtime.transition(ledger, token, "defer", retry_in=300)
                    _defer_note(out, "jev_skip_unverified")
                    continue
            job_jev = None if job_skip is not None else jev_client
            reserve_fn = None
            if (job_jev is not None
                    and getattr(jev_client, "supports_attempt_hooks", False)):
                reserve_fn = runtime.usage_reserver(
                    ledger, token, kind=KIND_USAGE, model=jev.JEV_MODEL,
                    project_id=job["project_id"], message_id=job["message_id"],
                    daily_request_budget=lane_cap, config_path=cfg_path)
            reservation = None
            if job['kind'] == JOB_KIND:
                try:
                    reservation = _reserve_long_defer(ledger, token)
                except Exception as error:
                    if type(error) is ValueError and str(error) == 'semantic_intake_held':
                        hold_job(job, mutation, 'semantic: intake_held')
                        continue
                    if type(error) is ValueError and str(error) == 'semantic_intake_hold_unverified':
                        result['errors'].append('semantic: intake_hold_unverified')
                        out['intake_hold_unverified'] = True
                        break
                    if 'semantic: long_defer_reservation_failed' not in result['errors']:
                        result['errors'].append('semantic: long_defer_reservation_failed')
                    out['long_defer_reservation_failed'] = True
                    if type(error) is ValueError and str(error) in (
                            'semantic_job_payload_unreadable', 'semantic_long_defers_capacity',
                            'semantic_long_defer_generation_unknown'):
                        hold_job(job, mutation, 'semantic: long_defer_capacity'
                                 if str(error) == 'semantic_long_defers_capacity'
                                 else 'semantic: job_payload_held')
                    continue
                if reservation is None:
                    continue
            # only now has the job cleared every gate — the persisted
            # fairness counters count jobs that were actually served, not
            # selections lost to circuit/budget/pause breaks (T14)
            selected[cohort_name] += 1
            if job["job_id"] == mutation.get("turn_job_id"):
                consumed_turns[mutation.get("turn_key", "turn")] = mutation["turn"]
            if lane == "backlog":
                _BACKLOG_TURN["kind"] = job["kind"]
            req0 = jev_client.requests_made if jev_client is not None else 0
            usage_before = dict(getattr(jev_client, "usage_totals", {}))
            job_started = time.perf_counter()
            job_age = max(0.0, time.time() - job["created_at"])
            # T14 phase split: queue wait (job_age_s) | model calls | Jev
            # evaluation | post-processing (validate→facts→render). The
            # wrappers only time calls — all attribution stays honest, and
            # the residual post_s covers the work between them.
            jev_timing = {"s": 0.0}
            timed_llm, llm_acc = _timed_llm(llm_fn)
            art0 = _last_stage_artifact(ledger, job["project_id"])
            diagnostics = _job_diagnostics()
            if job_skip is not None:
                diagnostics["jev_skip_reason"] = job_skip
            try:
                if job["kind"] == QC_JOB_KIND:
                    status = semantic_qc._process_qc_job(
                        ledger, scfg, job, job_jev, deadline,
                        reserve_fn=reserve_fn, cfg_path=cfg_path,
                        config_generation=cfg_generation,
                        jev_transport=jev_transport, jev_timing=jev_timing,
                        skip_reason=job_skip, lane=lane, diagnostics=diagnostics)
                else:
                    status = _process_job(
                        ledger, scfg, job, job_jev, timed_llm, deadline,
                        cfg_path=cfg_path,
                        config_generation=cfg_generation,
                        reserve_fn=reserve_fn, lane=lane, diagnostics=diagnostics, shadow_stats=shadow_stats,
                        jev_skip=job_skip,
                        jev_transport=jev_transport, jev_timing=jev_timing,
                        cloud_transport=(lambda call, *a, **kw: _unlocked("llm", call, *a, **kw))
                        if cloud_backlog else None, llm_timing=llm_acc, cloud_only=cloud_only)
            except writer_lock.RunLockLost as e:
                # An updater won the lock in a transport window (or the
                # bounded reacquire expired). Make no further writes: the job
                # stays pending with its attempts untouched. Its previously
                # committed long-defer reservation remains conservatively charged;
                # the _job_lock flock is released on return.
                # later jobs are not started, and the caller ends the
                # tick/batch on out["run_lock_lost"].
                out["run_lock_lost"] = ("held" if e.held
                                        else "updated" if e.updated
                                        else "timeout")
                result["errors"].append("semantic: run_lock_lost")
                return out
            except Exception as e:
                _why(diagnostics, f"exception_{type(e).__name__}")
                runtime.transition(ledger, token, "retry", retry_in=300,
                                   max_attempts=limit)
                result["errors"].append(
                    f"semantic {job['message_id']}: {type(e).__name__}")
                out["failed"] += 1
                status = None
            job_elapsed = time.perf_counter() - job_started
            skip_observed = diagnostics.get("jev_skip_reason")
            if skip_observed and status == "done":
                counts = out.setdefault("jev_skipped", {})
                counts[skip_observed] = counts.get(skip_observed, 0) + 1
                if skip_observed in ("daily_budget", "backlog_reserved"):
                    out["budget_exhausted"] = True
            job_progressed = _last_stage_artifact(ledger, job["project_id"]) > art0
            if job_progressed:
                out["progressed"] += 1
            budget_short = status == "deferred_short"
            if budget_short and not job_progressed:
                # nothing persisted this pass: a free defer would loop
                # forever (and re-spend Jev every minute) — charge a bounded
                # attempt like any other overrun
                status = "retry"
            if status in ("deferred", "deferred_backoff") \
                    and diagnostics.get("not_sent") == "long_output_needs_budget":
                n = reservation[1]['n'] if reservation is not None else 0
                if n > LONG_FREE_DEFER_CAP:
                    # this generation keeps meeting passes too short for
                    # its long-output call — stop the free loop: charge
                    # the attempt (ends 'failed' at the ceiling, where
                    # revive_failed and the operator can see it)
                    _why(diagnostics, "long_output_defer_capped")
                    out["long_output_capped"] = \
                        out.get("long_output_capped", 0) + 1
                    status = "retry"
            elif reservation is not None and status is not None:
                try:
                    _refund_long_defer(ledger, token, reservation)
                except Exception:
                    result['errors'].append('semantic: long_defer_refund_failed')
            # requests this attempt made — real clients already reserved one
            # usage row per request before POST; the delta row below is only
            # for fake clients without attempt hooks (jev_usage_today)
            used = (jev_client.requests_made - req0) \
                if jev_client is not None else 0
            usage_after = getattr(jev_client, "usage_totals", {})
            usage = {key: usage_after.get(key, 0) - usage_before.get(key, 0)
                     for key in ("input_tokens", "output_tokens", "reported_requests")}
            usage["unreported_requests"] = used - usage["reported_requests"]
            llm_s = llm_acc["s"]
            jev_s = jev_timing["s"]
            metric = _job_metric(
                job, token, cohort_name, status,
                job_age, job_elapsed, llm_s, jev_s, used, usage,
                jev_client, diagnostics=diagnostics)
            if status == "done":
                # every "done" return has already passed the
                # generation-fenced transition
                metric["completed_at"] = time.time()
            out["job_metrics"].append(metric)
            if status == "done":
                out["done_by_kind"][job["kind"]] = \
                    out["done_by_kind"].get(job["kind"], 0) + 1
            if used:
                runtime.record_circuit_result(ledger, getattr(jev_client, "last_error", None))
            # Real Jev calls reserve one usage row before POST.  A crash after
            # that commit therefore still spends the daily cap; adding the old
            # post-job delta would double count it.  Injected fake clients keep
            # the delta row for compatibility with offline tests.
            if used and reserve_fn is None:
                ledger.artifact_add(
                    KIND_USAGE, json.dumps({"job_id": job["job_id"]}),
                    project_id=job["project_id"],
                    message_id=job["message_id"], model=jev.JEV_MODEL,
                    meta={"jev_requests": used})
            if status is None:
                continue
            if status in ("deferred", "deferred_short", "deferred_backoff", "retry", "stale"):
                import cloud_backlog
                reason = diagnostics.get("not_sent") or diagnostics.get("code")
                if reason not in cloud_backlog._DEFER_REASONS:
                    reason = "unclassified"
                counts = out.setdefault("defer_reasons", {})
                counts[reason] = counts.get(reason, 0) + 1
            if _apply_job_status(ledger, token, limit, status,
                                 budget_short, out):
                break
            if getattr(getattr(jev_client, "last_error", None),
                       "kind", "") == "payment_required":
                # the service refuses every call until the account is
                # settled — stop this pass here rather than spend one
                # more 402 per remaining job (2026-10-08)
                result["errors"].append("semantic: jev_payment_required")
                out["jev_blocked"] = "payment_required"
                break
    if held_after and not saw_held and sum(selected.values()):
        sched_mut.update(hold_cursor_changed=True, hold_cursor_lane=hold_lane, hold_cursor=None)
    if llm_health["fed"]:
        out["llm_down"] = True
    if (out.get('long_defer_reservation_failed')
            and 'semantic: long_defer_reservation_failed' not in result['errors']):
        result['errors'].append('semantic: long_defer_reservation_failed')
    unsaved = {cause: n - marks_unsaved0.get(cause, 0)
               for cause, n in semantic_transport.long_mark_failures().items()
               if n > marks_unsaved0.get(cause, 0)}
    if unsaved:
        # long-output marks this pass could not persist (T8): the
        # suppression still holds in-process, but a restart repeats
        # the base-ceiling call — surfaced instead of swallowed
        out["long_mark_unsaved"] = unsaved
    if not cloud_only and scfg["mode"] == "enforce" and scfg["summary_mode"] == "enforce":
        try:
            out["degraded_notices"] = semantic_notice.emit_degraded(ledger, scfg)
        except Exception:
            out["degraded_notices"] = 0
            result["errors"].append("semantic: degraded_scan_failed")
    if not out.get('cloud_selection_incomplete'):
        kind_ph = ",".join("?" * len(kinds))
        out["left"] = ledger.db.execute(
            f"SELECT COUNT(*) FROM fetch_jobs WHERE kind IN ({kind_ph}) "
            "AND state='pending' AND next_try<=?", (*kinds, time.time())).fetchone()[0]
    out["elapsed_s"] = time.perf_counter() - drain_started
    out["observation"] = _observation(
        started_wall, time.time(), cfg_generation, lane, out["job_metrics"])
    if observation_config_changed:
        # One run must not attribute earlier jobs to its final config,
        # even if settings changed and later returned to the initial value.
        out["observation"]["complete"] = False
    if due:
        _persist_drain_accounting(ledger, scfg, out, result, selected,
                                  qc_active, sched_mut, None,
                                  long_defers)
    return out


# Bounded automatic retry of exhausted semantic jobs (owner request
# 2026-09-30). Bounded three ways so a permanently broken input cannot
# burn the Jev budget: at most REVIVE_MAX jobs per pass, each
# job at most REVIVE_PER_INPUT times for the same input
# generation, and only after REVIVE_COOLDOWN_S since it failed.
# Each revival grants exactly one more attempt (manual_attempt_limit =
# attempts + 1); an input change still resets everything as before.
REVIVE_MAX = 20
REVIVE_PER_INPUT = 3
REVIVE_COOLDOWN_S = 6 * 3600


def revive_failed(ledger, now: float | None = None) -> dict:
    """Give exhausted 'failed' semantic jobs one more bounded attempt.

    Oldest failures first. The job keeps its generation and accumulated
    attempts; ``auto_retry`` counts revivals of the current input so the
    per-input cap survives restarts, and ``retry_command_id`` fences a
    worker that captured the pre-revival payload (same mechanism as the
    human retry command)."""
    now = time.time() if now is None else now
    out = {"revived": 0, "skipped_cap": 0}
    # capped jobs are excluded BEFORE the LIMIT — selecting them first
    # let a capped prefix starve every eligible job behind it
    capped = ("(CASE WHEN json_valid(payload) AND "
              "json_type(payload,'$.auto_retry')='integer' "
              "THEN json_extract(payload,'$.auto_retry') ELSE 0 END)")
    where = ("WHERE kind=? AND state='failed' AND updated_at<=? "
             "AND reason_code IS NOT 'semantic_payload_invalid' AND ")
    rows = ledger.db.execute(
        "SELECT job_id,attempts,payload,updated_at FROM fetch_jobs " + where
        + capped + "<? ORDER BY updated_at LIMIT ?",
        (JOB_KIND, now - REVIVE_COOLDOWN_S, REVIVE_PER_INPUT,
         REVIVE_MAX * 4)).fetchall()
    out["skipped_cap"] = ledger.db.execute(
        "SELECT COUNT(*) FROM fetch_jobs " + where + capped + ">=?",
        (JOB_KIND, now - REVIVE_COOLDOWN_S,
         REVIVE_PER_INPUT)).fetchone()[0]
    for row in rows:
        if out["revived"] >= REVIVE_MAX:
            break
        pl = loads_dict(row["payload"] or "{}")
        if pl is None:
            continue
        n = pl.get("auto_retry")
        n = n if type(n) is int and n >= 0 else 0
        if n >= REVIVE_PER_INPUT:
            out["skipped_cap"] += 1
            continue
        attempts = int(row["attempts"] or 0)
        pl["auto_retry"] = n + 1
        pl["manual_attempt_limit"] = max(
            attempts + 1, runtime.attempt_limit(pl))
        pl["retry_command_id"] = f"auto-{int(now)}-{row['job_id']}"
        with ledger.db:
            changed = ledger.db.execute(
                "UPDATE fetch_jobs SET state='pending',payload=?,next_try=?,"
                "updated_at=? WHERE job_id=? AND state='failed' "
                "AND attempts IS ? AND payload IS ? AND updated_at IS ?",
                (json.dumps(pl, ensure_ascii=False, sort_keys=True),
                 now, now, row["job_id"], row["attempts"], row["payload"],
                 row["updated_at"])).rowcount
        out["revived"] += changed
    return out


def main() -> int:
    """Bounded background drain; transport releases the ingestion lock."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--drain", action="store_true",
                    help="loop run_due until the queue is empty or "
                         "--stop-after expires")
    ap.add_argument("--stop-after", type=float, default=3600.0)
    ap.add_argument("--max-jobs", type=int, default=8)
    ap.add_argument("--revive-failed", action="store_true",
                    help="before draining, give exhausted failed jobs one "
                         "more bounded attempt")
    args = ap.parse_args()
    if not args.drain:
        if not args.revive_failed:
            ap.error("--drain or --revive-failed required")
        # Bookkeeping only: revive_failed's per-row conditional UPDATE is
        # a CAS on the selected failed input, so it needs no run lock — taking one
        # made the 6-hourly maintenance report failure whenever a tick or
        # drain held the lock at that moment (2026-10).
        from ledger import Ledger
        from mcs_util import DB
        led = Ledger(DB)
        try:
            print(json.dumps(revive_failed(led)))
        finally:
            led.close()
        return 0
    if not 1 <= args.max_jobs <= 32:
        ap.error("max-jobs must be between 1 and 32")
    if not math.isfinite(args.stop_after) or args.stop_after < 0:
        ap.error("stop-after must be a finite nonnegative number")
    from ledger import Ledger
    from mcs_util import CONF_PATH, DB
    from writer_lock import acquire_run_lock
    led = None
    stop = time.monotonic() + args.stop_after
    totals = {"done": 0, "deferred": 0, "failed": 0, "batches": 0}
    try:
        while time.monotonic() < stop:
            lock_fd = acquire_run_lock()
            if lock_fd is None:
                time.sleep(min(10, max(0.0, stop - time.monotonic())))
                continue
            try:
                if led is None:
                    led = Ledger(DB)
                    if args.revive_failed:
                        totals["revive"] = revive_failed(led)
                cfg = load_config()
                # The batch must fit one full job budget so a canonical
                # generation can complete. A shorter batch can repeatedly
                # abandon useful work and exhaust the retry allowance of
                # revived jobs without letting the generation finish. Still
                # bounded: the tick waits at most job_budget+30 s.
                try:
                    batch_s = max(
                        120.0, float(runtime_config(cfg)[0]
                                     ["job_budget_seconds"]) + 30.0)
                except Exception:
                    batch_s = 480.0
                result = {"errors": []}
                out = run_due(
                    led, cfg, result,
                    deadline=min(stop, time.monotonic() + batch_s),
                    max_jobs=args.max_jobs, cfg_path=CONF_PATH,
                    run_lock_fd=lock_fd, lane="backlog")
            finally:
                os.close(lock_fd)
            totals["batches"] += 1
            for k in ("done", "deferred", "failed"):
                totals[k] += out.get(k) or 0
            totals["left"] = out.get("left")
            # ts/pid: a window cut short (e.g. gateway restart kills
            # the catchup) is judged later from mcs.log
            print(json.dumps({**out, "batches": totals["batches"],
                              "ts": time.time(), "pid": os.getpid()},
                             ensure_ascii=False, default=str),
                  flush=True)
            if out.get("run_lock_lost"):
                # an update/rollback ran under us: this process holds the
                # old code, so it must not take the lock again
                totals["stopped"] = "run_lock_lost"
                break
            if out.get("left") == 0:
                totals["stopped"] = "queue_empty"
                break
            if not out.get("done"):
                # deferred/failed-only batches mean every claimable job
                # is backed off — hot-looping re-claims the same rows.
                time.sleep(min(10, max(0.0, stop - time.monotonic())))
            else:
                time.sleep(min(1, max(0.0, stop - time.monotonic())))
    finally:
        if led is not None:
            led.close()
    print(json.dumps({**totals, "ts": time.time(), "pid": os.getpid()},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
