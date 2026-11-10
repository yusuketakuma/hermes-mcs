"""Policy exclusion retains failures, requires acquisition proof, and never fabricates OCR text."""
import copy
import json

import pytest

import attachment_ocr as ocr
import attachment_ocr_ack as ack
import attachment_report_reader
import health_report
import health_watch
import mcs_queries
import ocr_document_reader
from test_ocr_failure_ack import case as case, originals, request
from test_attachment_ocr_health import CFG, _status, _health
import ocr_failure_ack as operator


def configured():
    cfg = copy.deepcopy(CFG)
    cfg['attachment_ocr']['reject_uncertain'] = True
    return cfg


def bind(case, aid=1, *, reason='output_schema_table_overlap', attempts=3):
    if aid != 1:
        with case.db:
            case.db.execute("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                            "VALUES(?,1,'downloaded',?,32,1)", (aid, 'a' * 64))
    source = ocr._source(case.db, aid)
    doc = {**case.doc, 'attachment_id': aid, 'reason': reason, 'attempts': attempts,
           'input_occurrence': ocr._source_occurrence(case.db, source)}
    meta = {'attachment_id': aid, 'input_occurrence': doc['input_occurrence']}
    if aid == 1:
        with case.db:
            case.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                            (json.dumps(doc), json.dumps(meta), case.artifact))
        return case.artifact
    return case.ledger.artifact_add(ocr.KIND, json.dumps(doc), project_id=1, message_id=1, meta=meta)


def observed(case, cfg):
    case.db.execute('BEGIN')
    try:
        return ack.failure_counts(case.db, cfg=cfg)
    finally:
        case.db.rollback()


def test_explicit_policy_is_readonly_reversible_and_keeps_original_references(case):
    bind(case)
    cfg = configured()
    before, changes = originals(case), case.db.total_changes
    case.db.execute('PRAGMA query_only=ON')
    assert ocr.view(case.db, 1)['state'] == 'failed'
    excluded = ocr.view(case.db, 1, project_id=1, cfg=cfg)
    assert excluded['state'] == 'rejected_by_policy' and excluded['page_count'] == 0
    assert excluded['artifact_id'] == case.artifact and excluded['attachment_id'] == 1
    assert excluded['original_project_url'].endswith('/1')
    assert excluded['clinical_review'] == 'not_proven' and excluded['source_coverage'] == 'unrecovered'
    assert ocr.current(case.db, 1) is None
    core = mcs_queries.attachment_ocr_coverage(case.db, 1, cfg)
    assert core['complete'] and core['excluded'] == core['policy_rejected'] == 1
    docs = ocr_document_reader.read(case.db, 1, cfg=cfg)
    assert docs['coverage']['complete'] and docs['coverage']['policy_rejected'] == 1
    assert docs['documents'] == [] and docs['excluded_documents'] == [excluded]
    cfg['attachment_ocr']['reject_uncertain'] = False
    assert ocr.view(case.db, 1, cfg=cfg)['state'] == 'failed'
    off = mcs_queries.attachment_ocr_coverage(case.db, 1, cfg)
    assert not off['complete'] and off['pending'] == 1 and off['signature'] != core['signature']
    assert originals(case) == before and case.db.total_changes == changes


@pytest.mark.parametrize('field,value', [('attempts', 2), ('attempts', 4), ('attempts', True),
    ('attempts', 3.0), ('reason', 'timeout'), ('reason', 'engine_load_failed'),
    ('reason', 'synthetic_unknown'), ('state', 'unavailable'), ('input_occurrence', None),
    ('input_occurrence', 'b' * 64)])
def test_unproven_or_nonstructural_failure_is_never_excluded(case, field, value):
    bind(case)
    doc = json.loads(case.db.execute('SELECT content FROM artifacts WHERE artifact_id=?',
                                  (case.artifact,)).fetchone()[0])
    doc[field] = value
    with case.db:
        case.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), case.artifact))
    assert ocr.view(case.db, 1, cfg=configured())['state'] != 'rejected_by_policy'


@pytest.mark.parametrize('fault', ['legacy', 'new_acquisition', 'bad_meta', 'corrupt_latest'])
def test_old_or_damaged_failure_cannot_be_rebound_to_current_source(case, fault):
    if fault != 'legacy':
        bind(case)
    with case.db:
        if fault == 'new_acquisition':
            case.db.execute('UPDATE attachments SET downloaded_at=2 WHERE attachment_id=1')
        elif fault == 'bad_meta':
            case.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.input_occurrence',?) WHERE artifact_id=?",
                            ('b' * 64, case.artifact))
        elif fault == 'corrupt_latest':
            case.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", ('{bad', case.artifact))
    assert ocr.view(case.db, 1, cfg=configured())['state'] != 'rejected_by_policy'
    assert observed(case, configured())['queue_status'] == 'partial'


def test_ack_and_policy_intersection_does_not_hide_another_timeout(case, monkeypatch):
    bind(case)
    cfg = configured()
    monkeypatch.setattr(ocr, 'load_config', lambda: cfg)
    assert operator.apply(case.root, request(case))['outcome'] == 'applied'
    bind(case, 2, reason='timeout')
    counts = observed(case, cfg)
    assert counts['pending_failed'] == 2 and counts['acknowledged_exhausted'] == 1
    assert counts['policy_rejected_exhausted'] == 1 and counts['policy_rejected_unacknowledged'] == 0
    assert counts['unacknowledged_pending_failed'] == 1
    _status(case.data, **counts)
    assert health_report.attachment_ocr_health(str(case.data), cfg, 1000)['reason'] == 'attachment_ocr_processing_failed'


def test_policy_quiet_requires_live_settings_and_current_bounded_proof(case, monkeypatch):
    bind(case)
    cfg = configured()
    live = copy.deepcopy(cfg)
    monkeypatch.setattr(ocr, 'load_config', lambda: live)
    counts = observed(case, cfg)
    _status(case.data, **counts)
    result = health_report.attachment_ocr_health(str(case.data), cfg, 1000)
    assert result['reason'] == 'attachment_ocr_policy_excluded' and result['alert_required'] is False
    assert result['source_coverage'] == 'unrecovered' and result['clinical_review'] == 'not_proven'
    live['attachment_ocr']['reject_uncertain'] = False
    stale = health_report.attachment_ocr_health(str(case.data), cfg, 1000)
    assert stale['state'] == 'unknown' and stale.get('alert_required') is not False
    live['attachment_ocr']['reject_uncertain'] = True
    with case.db:
        case.db.execute('UPDATE attachments SET downloaded_at=2 WHERE attachment_id=1')
    assert health_report.attachment_ocr_health(str(case.data), cfg, 1000)['state'] == 'unknown'


def test_rejected_only_report_pages_advance_both_directions_and_pin_policy(case):
    bind(case)
    bind(case, 2)
    cfg = configured()
    first = attachment_report_reader.read_page(case.db, 1, [1], cfg=cfg, limit=1)
    assert first['sources'] == [] and [d['attachment_id'] for d in first['excluded_documents']] == [1]
    assert first['next_cursor'] and first['next_cursor']['policy_binding'] == ocr.rejection_policy(cfg)
    second = attachment_report_reader.read_page(case.db, 1, [1], cfg=cfg, cursor=first['next_cursor'], limit=1)
    assert [d['attachment_id'] for d in second['excluded_documents']] == [2]
    back = attachment_report_reader.read_page(case.db, 1, [1], cfg=cfg, cursor=second['prev_cursor'], limit=1)
    assert [d['attachment_id'] for d in back['excluded_documents']] == [1]
    cfg['attachment_ocr']['reject_uncertain'] = False
    with pytest.raises(ValueError, match='attachment_report_cursor_invalid'):
        attachment_report_reader.read_page(case.db, 1, [1], cfg=cfg, cursor=first['next_cursor'], limit=1)


@pytest.mark.parametrize('value', [None, 0, 1, 'true'])
def test_policy_setting_requires_an_exact_boolean(value):
    cfg = configured()
    cfg['attachment_ocr']['reject_uncertain'] = value
    assert ocr.config_error(cfg) == 'attachment_ocr_reject_uncertain'
    assert ocr.rejection_policy(cfg) is None


def test_broken_ack_receipt_cannot_be_hidden_by_policy(case, monkeypatch):
    bind(case)
    cfg = configured()
    monkeypatch.setattr(ocr, 'load_config', lambda: cfg)
    assert operator.apply(case.root, request(case))['outcome'] == 'applied'
    with case.db:
        case.db.execute('UPDATE artifacts SET content=? WHERE kind=?', ('{bad', ack.KIND))
    counts = observed(case, cfg)
    assert counts['policy_rejected_exhausted'] == 1 and counts['queue_status'] == 'partial'
    _status(case.data, **counts)
    assert health_report.attachment_ocr_health(str(case.data), cfg, 1000).get('alert_required') is not False


def test_partial_cohort_and_new_run_failure_are_never_quieted(case, monkeypatch):
    bind(case)
    cfg = configured()
    monkeypatch.setattr(ocr, 'load_config', lambda: cfg)
    counts = observed(case, cfg)
    _status(case.data, failed=1, **counts)
    assert health_report.attachment_ocr_health(str(case.data), cfg, 1000)['reason'] == 'attachment_ocr_processing_failed'
    with case.db:
        case.db.executemany("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                            "VALUES(?,1,'downloaded',?,32,1)", [(aid, 'a' * 64) for aid in range(2, 258)])
    counts = observed(case, cfg)
    assert counts['queue_status'] == 'partial'
    _status(case.data, **counts)
    assert health_report.attachment_ocr_health(str(case.data), cfg, 1000).get('alert_required') is not False


def test_policy_only_gap_does_not_alert_but_other_failures_still_do(case, monkeypatch):
    bind(case)
    cfg = configured()
    monkeypatch.setattr(ocr, 'load_config', lambda: cfg)
    _status(case.data, **observed(case, cfg))
    _health(case.root)
    report = health_watch.evaluate(str(case.root), 1000, cfg=cfg)
    assert report['state_reasons'] == ['attachment_ocr_policy_excluded']
    assert report['alert'] is False
    _status(case.data, failed=1, **observed(case, cfg))
    report = health_watch.evaluate(str(case.root), 1001, cfg=cfg)
    assert report['attachment_ocr']['reason'] == 'attachment_ocr_processing_failed'
    assert report['alert'] is True
