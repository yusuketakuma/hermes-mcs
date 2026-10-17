"""Bind private ledger observations to the exact locally generated documents."""
from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing

from mcs_util import payload_hash
from semantic_evaluation import EvaluationError


def policy_identity(cfg):
    from semantic_policy import semantic_config, policy_fingerprint
    from semantic_candidate import candidate_fingerprint
    policy, _ = semantic_config({})
    policy.update(cfg.get('semantic') or {})
    policy['candidate_fingerprint'] = candidate_fingerprint(cfg)
    return policy_fingerprint(policy)


def source_bundle(record, cfg):
    """Validate the exported runtime bundle against every input message."""
    from local_llm import resolve
    from semantic_store import bundle_fingerprint
    obs = record.get('sealed_observations')
    if obs is None:
        return None
    fields = {'schema_version', 'bundle_sha256', 'candidate_fingerprint',
              'policy_fingerprint', 'generation_sha256', 'bundle', 'artifacts',
              'notify_outbox', 'requests', 'finals', 'loop_ids'}
    if not isinstance(obs, dict) or set(obs) != fields or obs['schema_version'] != 1:
        raise EvaluationError('observation_schema_invalid')
    from semantic_candidate import candidate_fingerprint
    if obs['candidate_fingerprint'] != candidate_fingerprint(cfg):
        raise EvaluationError('observation_candidate_mismatch')
    if obs['policy_fingerprint'] != policy_identity(cfg):
        raise EvaluationError('observation_policy_mismatch')
    if obs['bundle_sha256'] != payload_hash(record['bundle']):
        raise EvaluationError('observation_source_mismatch')
    bundle = obs['bundle']
    if not isinstance(bundle, dict) or bundle.get('project_id') != record['project_id']:
        raise EvaluationError('observation_source_mismatch')
    members = bundle.get('members')
    messages = record['bundle']['messages']
    if not isinstance(members, list) or len(members) != len(messages):
        raise EvaluationError('observation_source_mismatch')
    ids = set()
    for member, message in zip(members, messages, strict=True):
        if (not isinstance(member, dict) or type(member.get('message_id')) is not int
                or member['message_id'] in ids
                or member.get('project_id') != record['project_id']
                or member['message_id'] != message['message_id']
                or member.get('body_original') != message['body']
                or member.get('revision') != message.get('revision', 'r1')
                or member.get('posted_at', '') != message.get('posted_at', '')
                or member.get('body_state') != 'full'):
            raise EvaluationError('observation_source_mismatch')
        ids.add(member['message_id'])
    if bundle.get('source_fingerprint') != bundle_fingerprint(members, local_model=resolve(cfg)[1]):
        raise EvaluationError('observation_source_fingerprint_mismatch')
    return bundle


def _rows(db, table, rows, columns, json_fields=()):
    if not isinstance(rows, list):
        raise EvaluationError('observation_rows_invalid')
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(columns):
            raise EvaluationError('observation_row_schema_invalid')
        values = []
        for key in columns:
            value = row[key]
            if key in json_fields:
                if not isinstance(value, dict):
                    raise EvaluationError('observation_json_object_required')
                value = json.dumps(value, allow_nan=False)
            values.append(value)
        db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)


def attach(record, documents, candidate, cfg):
    """Read exact-generation receipts; never infer a missing stage or audit."""
    from semantic_lifecycle import fact_lifecycle, _verified
    from semantic_store import _current
    from semantic_v4 import _doc_hash
    from types import SimpleNamespace
    from request_loops import effective_loop_state
    bundle = source_bundle(record, cfg)
    if bundle is None:
        return candidate, None
    obs = record['sealed_observations']
    docs = [result['doc'] for result in documents]
    if obs['generation_sha256'] != payload_hash(docs):
        raise EvaluationError('observation_generation_mismatch')
    fp, policy = bundle['source_fingerprint'], obs['policy_fingerprint']
    members = bundle['members']
    mids = {m['message_id'] for m in members}
    with closing(sqlite3.connect(':memory:')) as db:
        db.row_factory = sqlite3.Row
        db.executescript('''
          CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY, project_id INTEGER,
            message_id INTEGER, kind TEXT, content TEXT, meta TEXT, created_at INTEGER);
          CREATE TABLE notify_outbox(event_id INTEGER PRIMARY KEY, project_id INTEGER,
            kind TEXT, state TEXT, payload TEXT, progress TEXT);
          CREATE TABLE messages(message_id INTEGER PRIMARY KEY, project_id INTEGER,
            content_hash TEXT, parent_id INTEGER);
          CREATE TABLE requests(project_id INTEGER,request_id TEXT,status TEXT,revision INTEGER,title TEXT);
        ''')
        _rows(db, 'artifacts', obs['artifacts'], ('artifact_id', 'project_id', 'message_id',
              'kind', 'content', 'meta', 'created_at'), ('content', 'meta'))
        _rows(db, 'notify_outbox', obs['notify_outbox'], ('event_id', 'project_id',
              'kind', 'state', 'payload', 'progress'), ('payload', 'progress'))
        _rows(db, 'requests', obs['requests'], ('project_id', 'request_id', 'status', 'revision', 'title'))
        for member in members:
            db.execute('INSERT INTO messages VALUES(?,?,?,?)', (member['message_id'],
                       record['project_id'], member['revision'], member.get('parent_id')))
        allowed = {'semantic_facts_v2', 'semantic_facts_audit', 'semantic_summary',
                   'semantic_audit', 'loop_candidate', 'loop_event',
                   'loop_relation_rejected', 'request_loop_link'}
        for row in obs['requests']:
            if row['project_id'] != record['project_id'] or row['status'] not in ('open', 'in_progress', 'done', 'cancelled'):
                raise EvaluationError('observation_request_invalid')
        for row in obs['artifacts']:
            meta = row['meta']
            binding = row['content'] if row['kind'] == 'request_loop_link' else meta
            source_fp = binding.get('source_fingerprint') if row['kind'] == 'request_loop_link' else binding.get('fingerprint')
            if (row['kind'] not in allowed or row['project_id'] != record['project_id']
                    or row['message_id'] not in mids or meta.get('invalidated')
                    or source_fp != fp
                    or binding.get('policy_fingerprint') != policy):
                raise EvaluationError('observation_artifact_binding_mismatch')
        for row in obs['notify_outbox']:
            payload = row['payload']
            if (row['project_id'] != record['project_id'] or row['kind'] != 'semantic_notice'
                    or payload.get('fingerprint') != fp
                    or payload.get('policy_fingerprint') != policy
                    or payload.get('target_message_id') not in mids):
                raise EvaluationError('observation_delivery_binding_mismatch')
        verified = []
        for member, doc in zip(members, docs, strict=True):
            stored = _current(SimpleNamespace(db=db), 'semantic_facts_v2', member['message_id'], fp, policy)
            if stored is None or _doc_hash(stored['content']) != _doc_hash(doc):
                raise EvaluationError('observation_document_mismatch')
            ids, reason = _verified(db, member['message_id'], fp, policy)
            if ids is None:
                raise EvaluationError('observation_' + reason)
            verified.extend(ids)
        finals = obs['finals']
        if not isinstance(finals, list) or len(finals) != len(members):
            raise EvaluationError('observation_finals_incomplete')
        claims, rendered, delivered, targets = [], [], [], set()
        for final in finals:
            if not isinstance(final, dict) or set(final) != {'final_id', 'audit_id', 'summary_sha256'}:
                raise EvaluationError('observation_final_schema_invalid')
            row = db.execute("SELECT * FROM artifacts WHERE artifact_id=? AND kind='semantic_summary'", (final['final_id'],)).fetchone()
            audit = db.execute("SELECT * FROM artifacts WHERE artifact_id=? AND kind='semantic_audit'", (final['audit_id'],)).fetchone()
            if row is None or audit is None or row['message_id'] != audit['message_id'] or row['message_id'] in targets:
                raise EvaluationError('observation_final_binding_mismatch')
            for selected, kind in ((row, 'semantic_summary'), (audit, 'semantic_audit')):
                newest = db.execute('SELECT MAX(artifact_id) FROM artifacts WHERE kind=? AND message_id=?', (kind, row['message_id'])).fetchone()[0]
                if newest != selected['artifact_id']:
                    raise EvaluationError('observation_final_not_current')
            targets.add(row['message_id'])
            summary, meta = json.loads(row['content']), json.loads(row['meta'])
            audit_doc, audit_meta = json.loads(audit['content']), json.loads(audit['meta'])
            if (payload_hash(summary) != final['summary_sha256']
                    or audit['artifact_id'] != row['artifact_id'] + 1
                    or meta.get('audit_status') != 'PASS'
                    or audit_doc.get('target_message_id') != row['message_id']
                    or audit_doc.get('status') != 'PASS' or audit_meta.get('audit_status') != 'PASS'
                    or meta.get('target_revision') != next(m['revision'] for m in members if m['message_id'] == row['message_id'])):
                raise EvaluationError('observation_summary_audit_mismatch')
            if not isinstance(summary.get('claims'), list):
                raise EvaluationError('observation_claims_missing')
            for claim in summary['claims']:
                claim = copy.deepcopy(claim)
                refs = claim.get('fact_refs', [])
                if any(type(ref) is int for ref in refs):
                    from semantic_projection import project_v2_facts
                    projection = project_v2_facts(docs[next(i for i, m in enumerate(members) if m['message_id'] == row['message_id'])])
                    if not isinstance(projection, list) or any(type(ref) is not int or not 0 <= ref < len(projection) or not projection[ref].get('fact_id') for ref in refs):
                        raise EvaluationError('observation_claim_fact_binding_missing')
                    claim['fact_refs'] = [projection[ref]['fact_id'] for ref in refs]
                if not set(claim.get('fact_refs', [])) <= set(verified):
                    raise EvaluationError('observation_claim_fact_mismatch')
                claims.append(claim)
            lifecycle = fact_lifecycle(db, final['final_id'], cfg=cfg)
            if set(lifecycle['fact_ids']) != {'verified', 'rendered', 'delivered'}:
                raise EvaluationError('observation_lifecycle_incomplete')
            rendered.extend(lifecycle['fact_ids']['rendered'])
            delivered.extend(lifecycle['fact_ids']['delivered'])
        loop_rows = list(db.execute("SELECT * FROM artifacts WHERE kind='loop_candidate' ORDER BY artifact_id"))
        if obs['loop_ids'] != [row['artifact_id'] for row in loop_rows]:
            raise EvaluationError('observation_loop_scope_mismatch')
        loops = []
        for row in loop_rows:
            loop = effective_loop_state(db, row, policy=policy, bundles={bundle['root_id']: bundle})
            if loop is None or not loop.get('current') or loop['relation_evaluation_unavailable']:
                raise EvaluationError('observation_loop_incomplete')
            # A reported completion is advisory, never a human-confirmed closure.
            links = loop['linked_requests']
            resolved = bool(links) and all(link['status'] in ('done', 'cancelled') for link in links)
            loops.append({'loop_id': loop['loop_id'], 'resolved': resolved,
                          'kind': loop.get('kind'), 'description': loop.get('description')})
    from semantic_projection import project_v2_facts
    facts = []
    for doc in docs:
        for fact, projected in zip(doc['facts'], project_v2_facts(doc), strict=True):
            enriched = {**fact, 'negation': fact.get('polarity'),
                        'time': {'event_time': fact.get('event_time'), 'valid_time': fact.get('valid_time')},
                        'speaker_relation': {'subject': fact.get('subject'), 'actor': fact.get('actor')}}
            if fact.get('kind') == 'medication_event':
                enriched['medication'] = projected['drug_ref']
            facts.append(enriched)
    out = {**candidate, 'facts': facts, 'claims': claims, 'loops': loops, 'status': 'complete',
           'relations': [relation for doc in docs for relation in doc.get('relations', [])],
           'verified_fact_ids': sorted(set(verified)), 'rendered_fact_ids': sorted(set(rendered)),
           'delivered_fact_ids': sorted(set(delivered))}
    return out, {'snapshot_sha256': payload_hash(obs), 'generation_sha256': obs['generation_sha256'],
                 'audit': 'imported_pass', 'lifecycle': 'imported_observations'}
