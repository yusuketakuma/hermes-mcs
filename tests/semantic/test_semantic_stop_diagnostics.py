"""Synthetic first-failed-gate diagnostics preserve recovery and fact-stage holds."""
import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

import cloud_backlog
import semantic_drain as drain
from inference_version import CURRENT_VERSION, VERSION_KEY


@pytest.fixture
def empty_candidate(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE artifacts (artifact_id INTEGER PRIMARY KEY,kind,project_id,message_id,content,meta)')
    content = {'target_message_id': 1, 'claims': [{'synthetic': True}]}
    marker = {VERSION_KEY: CURRENT_VERSION, 'fingerprint': 'fp', 'empty_repair': True,
              'policy_fingerprint': 'policy', 'repair_count': 1}
    meta = {VERSION_KEY: CURRENT_VERSION, 'schema': drain.SCHEMA_VERSION, 'stage': 'empty_repair',
            'fingerprint': 'fp', 'policy_fingerprint': 'policy', 'publication_mode': 'assist',
            'target_revision': 'revision', 'repair_artifact_id': 1, 'claim_input_binding': 'binding',
            'prior': [], 'provider_provenance': {'synthetic': True}, 'audit_state': 'ready',
            'partial_answers': {}}
    calls = []
    monkeypatch.setattr(drain, '_summary_producer', lambda *_: calls.append('producer') or {'synthetic': True})
    monkeypatch.setattr(drain, '_claim_input_binding', lambda *_: calls.append('binding') or 'binding')
    monkeypatch.setattr(drain, '_empty_repair_prior', lambda *_: calls.append('prior') or [])
    monkeypatch.setattr(drain, '_summary_provenance', lambda *_: calls.append('proof') or {'synthetic': True})

    def seed(candidate_content=content, candidate_meta=meta, marker_content=None, marker_meta=marker):
        db.execute('DELETE FROM artifacts')
        if marker_meta is not None:
            db.execute('INSERT INTO artifacts VALUES (1,?,1,1,?,?)',
                       (drain.KIND_REPAIR, json.dumps(marker_content or {'target_message_id': 1}),
                        json.dumps(marker_meta)))
        if candidate_meta is not None:
            db.execute('INSERT INTO artifacts VALUES (2,?,1,1,?,?)',
                       (drain.KIND_CANDIDATE, json.dumps(candidate_content), json.dumps(candidate_meta)))
        db.commit()
        calls.clear()

    yield db, content, meta, marker, calls, seed
    db.close()


@pytest.mark.parametrize('where,key,value,reason,calls', [
    ('marker', 'fingerprint', 9, 'marker_invalid', []),
    ('marker', 'fingerprint', 'other', 'marker_binding', []),
    ('marker', 'invalidated', True, 'marker_currentness', []),
    ('candidate', 'stale', True, 'candidate_currentness', ['producer']),
    ('candidate', 'target_revision', 'other', 'candidate_binding', ['producer']),
    ('candidate', 'repair_artifact_id', True, 'marker_link', ['producer']),
    ('marker', 'repair_count', 2, 'marker_link', ['producer']),
    ('content', 'claims', [], 'claims_invalid', ['producer']),
    ('candidate', 'claim_input_binding', 'other', 'claim_binding', ['producer', 'binding']),
    ('candidate', 'prior', ['synthetic-private-detail'], 'prior_unverified', ['producer', 'binding', 'prior']),
    ('candidate', 'provider_provenance', {'synthetic-private-detail': True}, 'provenance_unverified',
     ['producer', 'binding', 'prior', 'proof']),
    ('candidate', 'audit_state', 'waiting', 'audit_not_ready', ['producer', 'binding', 'prior', 'proof']),
    ('candidate', 'partial_answers', {'synthetic-private-detail': True}, 'audit_not_ready',
     ['producer', 'binding', 'prior', 'proof']),
])
def test_empty_repair_first_failed_gate_is_read_only(empty_candidate, where, key, value, reason, calls):
    db, content, meta, marker, observed, seed = empty_candidate
    content, meta, marker = copy.deepcopy(content), copy.deepcopy(meta), copy.deepcopy(marker)
    {'content': content, 'candidate': meta, 'marker': marker}[where][key] = value
    seed(content, meta, marker_meta=marker)
    before = db.total_changes
    statements = []
    db.set_trace_callback(statements.append)
    diag = {}
    assert drain._empty_repair_candidate(db, 1, 1, 'fp', 'policy', 'revision', 'assist', [],
                                         reason_out=diag) == (None, True)
    assert diag == {'code': 'empty_repair_' + reason}
    assert diag['code'] in cloud_backlog._SEMANTIC_STOP_REASONS
    assert observed == calls
    assert db.total_changes == before
    assert len(statements) == 2 and all(s.startswith('SELECT ') for s in statements)
    assert 'synthetic-private-detail' not in json.dumps(diag)


@pytest.mark.parametrize('case,reason,reserved', [
    ('no_marker', 'marker_missing', True),
    ('no_rows', 'marker_missing', False),
    ('no_candidate', 'candidate_missing', True),
    ('bad_content', 'candidate_invalid', True),
    ('bad_marker', 'marker_invalid', True),
])
def test_empty_missing_records_preserve_reservation(empty_candidate, case, reason, reserved):
    db, content, meta, marker, _, seed = empty_candidate
    seed(None if case == 'bad_content' else content,
         None if case in ('no_rows', 'no_candidate') else meta,
         marker_meta=None if case in ('no_rows', 'no_marker') else ([] if case == 'bad_marker' else marker))
    diag = {}
    assert drain._empty_repair_candidate(db, 1, 1, 'fp', 'policy', 'revision', 'assist', [],
                                         reason_out=diag) == (None, reserved)
    assert diag == {'code': 'empty_repair_' + reason}


def test_empty_recovery_preserves_legacy_tuple_and_does_not_report_failure(empty_candidate):
    db, content, meta, _, calls, seed = empty_candidate
    seed()
    before = db.total_changes
    diag = {}
    recovered, reserved = drain._empty_repair_candidate(
        db, 1, 1, 'fp', 'policy', 'revision', 'assist', [], reason_out=diag)
    assert reserved is True and recovered['content'] == content and recovered['meta'] == meta
    assert diag == {} and db.total_changes == before
    assert calls == ['producer', 'binding', 'prior', 'proof']
    assert drain._empty_repair_candidate(db, 1, 1, 'fp', 'policy', 'revision', 'assist', []) == (recovered, True)


def test_empty_validation_exception_hides_error_text():
    class Unavailable:
        def execute(self, *_):
            raise sqlite3.OperationalError('synthetic-private-detail')
    diag = {}
    assert drain._empty_repair_candidate(Unavailable(), 1, 1, 'fp', 'policy', 'rev', 'assist', [],
                                         reason_out=diag) == (None, True)
    assert diag == {'code': 'empty_repair_validation_unavailable'}


@pytest.mark.parametrize('proof,reason', [
    (None, 'fact_repaired_producer_unverified'),
    ({'base': [{'contract': 'previous'}]}, 'fact_repaired_contract_changed'),
])
def test_fact_repaired_cache_hold_does_not_dispatch(monkeypatch, proof, reason):
    previous = {'content': {'synthetic': True}, 'meta': {'repaired': True}}
    monkeypatch.setattr(drain, 'current_artifact', lambda *_: previous)
    monkeypatch.setattr(drain.semantic_facts, 'cached_facts_plan', lambda *_: (previous, True, False))
    monkeypatch.setattr(drain.semantic_transport, 'inference_contract', lambda: 'current')
    monkeypatch.setattr(drain.v4, '_repaired_projection_producer', lambda *_: proof)
    def forbidden(*_, **__):
        pytest.fail('a held repaired cache must not dispatch or write')
    early, found, build = drain._fact_v2_document(
        SimpleNamespace(db=object(), artifact_add=forbidden), {}, 1, 1, 'fp', 'policy',
        'canonical', forbidden, forbidden, 100)
    assert early == {'outcome': 'incomplete', 'v2_doc': None, 'diagnostic_reason': reason}
    assert found is build is None
    assert drain._fact_stop_reason(early) == reason


@pytest.mark.parametrize('failure,outcome,reason', [
    ('deadline', 'incomplete', 'fact_extract_deadline'),
    ('model', 'retryable', 'fact_extract_model'),
    ('synthetic-private-detail', 'incomplete', 'fact_extract_incomplete'),
])
def test_fact_extraction_failure_keeps_outcome_and_one_attempt(monkeypatch, failure, outcome, reason):
    monkeypatch.setattr(drain, 'current_artifact', lambda *_: None)
    monkeypatch.setattr(drain.time, 'monotonic', lambda: 1)
    monkeypatch.setattr(drain.semantic_facts, 'cached_facts_plan', lambda *_: (None, False, False))
    monkeypatch.setattr(drain.semantic_transport, 'inference_contract', lambda: None)
    records, attempts = [], []
    monkeypatch.setattr(drain.v4, 'record_stage', lambda *args: records.append(args[-2:]))
    def extract(*args, **kwargs):
        attempts.append(True)
        return {'extraction_complete': False, 'failure_reason': failure}
    monkeypatch.setattr(drain.semantic_extraction, 'extract_facts_v2', extract)
    early, found, build = drain._fact_v2_document(
        SimpleNamespace(db=object()), {}, 1, 1, 'fp', 'policy', 'canonical', None, None, 100)
    assert early == {'outcome': outcome, 'v2_doc': None, 'diagnostic_reason': reason}
    assert found is build is None
    assert attempts == [True] and records == [('s0_prep', 'done'), ('s1_extract', 'start')]


@pytest.mark.parametrize('untrusted', ['synthetic-private-detail', None, [], {'private': True}])
def test_fact_reason_whitelist_rejects_arbitrary_details(untrusted):
    assert drain._fact_stop_reason({'outcome': 'incomplete', 'diagnostic_reason': untrusted}) == 'fact_stage_incomplete'


@pytest.mark.parametrize('failure', ['private-exception-text', None, [], {'private': True}])
def test_empty_reason_whitelist_precedes_job_diagnostic(failure):
    diagnostics = drain._job_diagnostics()
    drain._why(diagnostics, drain._fixed_stop_reason(failure, 'empty_repair_unverified'))
    assert diagnostics['code'] == 'empty_repair_unverified'


@pytest.mark.parametrize('reason,rebuilt,complete', [
    ('synthetic-private-detail', {'synthetic': True}, True),
    (None, None, True),
    (None, {'synthetic': True}, False),
    (None, {'synthetic': False}, True),
])
def test_fact_cached_rebuild_gate_retains_hold_without_dispatch(monkeypatch, reason, rebuilt, complete):
    previous = {'content': {'synthetic': True}, 'meta': {}}
    monkeypatch.setattr(drain, 'current_artifact', lambda *_: previous)
    monkeypatch.setattr(drain.semantic_facts, 'cached_facts_plan', lambda *_: (previous, False, False))
    monkeypatch.setattr(drain.semantic_transport, 'inference_contract', lambda: 'current')
    rebuilds = []
    def rebuild(*args, producer_out, contracts_out):
        rebuilds.append(True)
        producer_out['complete'] = complete
        return rebuilt, reason
    monkeypatch.setattr(drain.semantic_extraction, 'rebuild_facts_v2_cached', rebuild)
    early, found, build = drain._fact_v2_document(
        SimpleNamespace(db=object()), {}, 1, 1, 'fp', 'policy', 'canonical', None, None, 100)
    assert early == {'outcome': 'incomplete', 'v2_doc': None,
                     'diagnostic_reason': 'fact_cached_rebuild_unverified'}
    assert found is build is None and rebuilds == [True]


@pytest.mark.parametrize('available,expected_outcome,expected_reason', [
    (False, 'incomplete', 'fact_audit_deadline'),
    (True, 'retryable', 'fact_audit_wait'),
])
def test_fact_audit_wait_preserves_outcome_and_no_additional_requests(
        monkeypatch, available, expected_outcome, expected_reason):
    doc = {'synthetic': True}
    audit = {'status': 'PENDING', 'evaluated': False, 'findings': []}
    monkeypatch.setattr(drain.semantic_facts, 'doc_hash', lambda *_: 'synthetic-hash')
    monkeypatch.setattr(drain, 'current_artifact', lambda *_: None)
    monkeypatch.setattr(drain.semantic_audit, 'cached_fact_audit', lambda *_: audit if available else None)
    monkeypatch.setattr(drain.semantic_audit, 'canonical_audit_disposition', lambda *_: 'wait')
    monkeypatch.setattr(drain.time, 'monotonic', lambda: 100)
    records = []
    monkeypatch.setattr(drain.v4, 'record_stage', lambda *args, **kwargs: records.append(args[-2:]))
    early, found, build = drain._fact_canonical_audit(
        SimpleNamespace(db=object()), {}, {}, 1, 1, 'fp', 'policy', 'canonical', doc, None,
        SimpleNamespace(last_error=None), None, 100)
    assert early == {'outcome': expected_outcome, 'v2_doc': doc, 'diagnostic_reason': expected_reason}
    assert found is build is None
    assert records == ([('s2_fact_audit', 'unevaluated')] if available else [])
