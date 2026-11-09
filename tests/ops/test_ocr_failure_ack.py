"""Operational acknowledgement never recovers or alters synthetic OCR evidence."""
import copy
import json
import os
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

import attachment_ocr as ocr
import attachment_ocr_ack as ack
import health_report
import health_watch
import ocr_failure_ack as operator
from common_testkit import make_ledger
from extract_testkit import _message
from test_attachment_ocr_health import CFG, _health, _status


@pytest.fixture
def case(tmp_path):
    root = tmp_path / 'synthetic-install'
    root.mkdir()
    ledger = make_ledger(root, data_dir=True)
    ledger.ensure_patient(1)
    ledger.save_messages([_message(1, body='完全合成の添付を確認しました。')])
    with ledger.db:
        ledger.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1 WHERE project_id=1")
        ledger.db.execute("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                          "VALUES(1,1,'downloaded',?,32,1)", ('a' * 64,))
    doc = {'schema': ocr.SCHEMA, 'attachment_id': 1, 'input_sha256': 'a' * 64, 'input_bytes': 32,
           'engine': 'yomitoku', 'engine_version': None, 'runtime_contract': ocr.RUNTIME_CONTRACT,
           'state': 'failed', 'pages': [], 'text_hash': ocr._hash('[]'), 'attempts': 3,
           'next_try': 2000, 'reason': 'output_schema_table_overlap'}
    artifact = ledger.artifact_add(ocr.KIND, json.dumps(doc), project_id=1, message_id=1,
                                   meta={'attachment_id': 1})
    yield SimpleNamespace(root=root, data=root / 'data', ledger=ledger, db=ledger.db,
                          artifact=artifact, doc=doc)
    ledger.close()


def plan(case):
    case.db.execute('BEGIN')
    try:
        return ack.build_plan(case.db, [case.artifact])
    finally:
        case.db.rollback()


def request(case):
    return {'command_id': str(uuid.uuid4()), 'actor': 'synthetic-operator',
            'reason': '合成の運用通知を受付。本文は未回収のまま。',
            'human_confirmed': True, 'plan': plan(case)}


def observed(case):
    case.db.execute('BEGIN')
    try:
        return ack.failure_counts(case.db)
    finally:
        case.db.rollback()


def originals(case):
    return [tuple(row) for row in case.db.execute('SELECT * FROM artifacts WHERE kind=?', (ocr.KIND,))]


def test_ack_keeps_exact_failure_coverage_and_future_retry_evidence(case):
    before = originals(case)
    counts = observed(case)
    assert counts['pending_failed'] == counts['exhausted_failed'] == 1
    assert counts['acknowledged_exhausted'] == 0
    receipt = operator.apply(case.root, request(case))
    assert receipt['outcome'] == 'applied' and receipt['receipt_state'] == 'saved'
    assert receipt['ocr_outcome'] == 'failed' and receipt['source_coverage'] == 'unrecovered'
    assert originals(case) == before
    assert ocr.current(case.db, 1, project_id=1) is None
    assert ocr.view(case.db, 1)['state'] == 'failed'
    counts = observed(case)
    assert counts['pending_failed'] == counts['exhausted_failed'] == counts['acknowledged_exhausted'] == 1
    assert counts['unacknowledged_pending_failed'] == 0
    assert counts['failure_reason_counts'] == {'output_schema_table_overlap': 1}
    assert (case.data / receipt['backup']).is_file()


def test_replay_never_reacknowledges_new_failure_or_creates_backup(case):
    req = request(case)
    first = operator.apply(case.root, req)
    second = operator.apply(case.root, copy.deepcopy(req))
    assert second['replayed'] and second['acknowledgement_ids'] == first['acknowledgement_ids']
    assert len(list(case.data.glob('ocr_alert_acknowledgements/*/before-*.db'))) == 1
    with pytest.raises(ValueError, match='command_id_conflict'):
        operator.apply(case.root, {**req, 'reason': '別の合成理由'})
    case.ledger.artifact_add(ocr.KIND, json.dumps(case.doc), project_id=1, message_id=1,
                            meta={'attachment_id': 1})
    assert operator.apply(case.root, req)['replayed']
    assert observed(case)['acknowledged_exhausted'] == 0


@pytest.mark.parametrize('field,value', [('attempts', 2), ('attempts', 4), ('attempts', True),
                                        ('reason', 'timeout'), ('state', 'unavailable'),
                                        ('runtime_contract', 'synthetic-old')])
def test_only_current_exhausted_overlap_is_eligible(case, field, value):
    changed = {**case.doc, field: value}
    with case.db:
        case.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                        (json.dumps(changed), case.artifact))
    with pytest.raises(ValueError, match='ocr_ack_target_invalid'):
        plan(case)


@pytest.mark.parametrize('change', ['parent_aba', 'attachment_aba', 'deleted', 'new_failure', 'receipt_missing', 'ack_missing', 'ack_meta_duplicate'])
def test_changed_source_or_broken_receipt_invalidates_acknowledgement(case, change):
    receipt = operator.apply(case.root, request(case))
    if change == 'parent_aba':
        case.ledger.save_messages([_message(1, body='完全合成の別版。')])
        case.ledger.save_messages([_message(1, body='完全合成の添付を確認しました。')])
    elif change == 'attachment_aba':
        with case.db:
            case.db.execute("UPDATE attachments SET sha256=?,downloaded_at=2 WHERE attachment_id=1", ('b' * 64,))
            case.db.execute("UPDATE attachments SET sha256=?,downloaded_at=3 WHERE attachment_id=1", ('a' * 64,))
    elif change == 'deleted':
        case.ledger.save_messages([_message(1, body='', state='deleted')])
    elif change == 'new_failure':
        case.ledger.artifact_add(ocr.KIND, json.dumps(case.doc), project_id=1, message_id=1,
                                meta={'attachment_id': 1})
    else:
        with case.db:
            if change == 'receipt_missing':
                case.db.execute('DELETE FROM command_receipts WHERE command_id=?', (receipt['command_id'],))
            elif change == 'ack_missing':
                case.db.execute('DELETE FROM artifacts WHERE kind=?', (ack.KIND,))
            else:
                case.db.execute('UPDATE artifacts SET meta=? WHERE kind=?',
                    ('{"failure_artifact_id":'+str(case.artifact)+',"failure_artifact_id":'+str(case.artifact)+'}', ack.KIND))
    assert observed(case)['acknowledged_exhausted'] == 0


def test_borrowed_plan_success_and_failure_do_not_end_callers_transaction(case):
    case.db.execute('BEGIN')
    case.db.execute("INSERT INTO artifacts(kind,project_id,content,meta) VALUES('synthetic_outer',1,'{}','{}')")
    before = case.db.total_changes
    ack.build_plan(case.db, [case.artifact])
    with pytest.raises(ValueError):
        ack.build_plan(case.db, [case.artifact, case.artifact])
    assert case.db.in_transaction and case.db.total_changes == before
    case.db.rollback()
    assert case.db.execute("SELECT 1 FROM artifacts WHERE kind='synthetic_outer'").fetchone() is None


def test_transaction_failure_is_atomic_and_keeps_backup(case):
    req = request(case)
    case.db.execute("CREATE TRIGGER reject_ocr_receipt BEFORE INSERT ON command_receipts "
                    "BEGIN SELECT RAISE(ABORT,'synthetic receipt rejection'); END")
    with pytest.raises(sqlite3.Error):
        operator.apply(case.root, req)
    assert not case.ledger.artifacts(ack.KIND)
    assert not case.db.execute('SELECT 1 FROM command_receipts').fetchone()
    assert len(list(case.data.glob('ocr_alert_acknowledgements/*/before-*.db'))) == 1


def test_receipt_copy_failure_reports_applied_and_replays_only_copy(case, monkeypatch):
    req = request(case)
    publish = operator._publish
    monkeypatch.setattr(operator, '_publish', lambda *args: (_ for _ in ()).throw(OSError('synthetic')))
    first = operator.apply(case.root, req)
    assert first['outcome'] == 'applied' and first['receipt_state'] == 'pending'
    assert observed(case)['acknowledged_exhausted'] == 1
    monkeypatch.setattr(operator, '_publish', publish)
    second = operator.apply(case.root, req)
    assert second['replayed'] and second['receipt_state'] == 'saved'
    assert len(case.ledger.artifacts(ack.KIND)) == 1
    assert len(list(case.data.glob('ocr_alert_acknowledgements/*/before-*.db'))) == 1


def test_external_receipt_after_db_rewind_blocks_automatic_reapply(case):
    req = request(case)
    operator.apply(case.root, req)
    with case.db:
        case.db.execute('DELETE FROM command_receipts WHERE command_id=?', (req['command_id'],))
        case.db.execute('DELETE FROM artifacts WHERE kind=?', (ack.KIND,))
    with pytest.raises(ValueError, match='ocr_ack_restore_conflict'):
        operator.apply(case.root, req)
    assert not case.ledger.artifacts(ack.KIND)


@pytest.mark.parametrize('marker', ['update_in_progress.marker', 'restore_pending.json'])
def test_lifecycle_markers_reject_acknowledgement(case, marker):
    req = request(case)
    (case.data / marker).write_text('{}')
    with pytest.raises(ValueError, match='lifecycle_operation_pending'):
        operator.apply(case.root, req)
    assert not case.ledger.artifacts(ack.KIND)


def test_waiting_writer_lock_is_not_bypassed(case):
    req = request(case)
    fd = operator.acquire_run_lock(str(case.data / 'run.lock'))
    assert fd is not None
    try:
        with pytest.raises(ValueError, match='operation_lock_held'):
            operator.apply(case.root, req)
    finally:
        os.close(fd)


def test_live_health_keeps_gap_failed_but_only_repeated_ocr_alert_is_held(case):
    operator.apply(case.root, request(case))
    _status(case.data, **observed(case))
    report = health_report.attachment_ocr_health(str(case.data), CFG, 1000)
    assert report['state'] == 'failed' and report['alert_required'] is False
    assert report['source_coverage'] == 'unrecovered' and report['pending_failed'] == 1
    _health(case.root, overall='degraded', reasons=['attachment_ocr_processing_failed'])
    report = health_watch.evaluate(str(case.root), 1000, cfg=CFG)
    assert report['status'] == 'degraded' and not report['alert']
    assert report['state_reasons'] == ['attachment_ocr_acknowledged_gap']
    _health(case.root, overall='degraded', reasons=['attachment_ocr_processing_failed', 'notification_held'])
    report = health_watch.evaluate(str(case.root), 1001, cfg=CFG)
    assert report['alert'] and 'notification_held' in report['state_reasons']


@pytest.mark.parametrize('updates', [{'failed': 1}, {'reason': 'runtime_unavailable'},
                                    {'queue_status': 'partial'}, {'deferred': 1},
                                    {'acknowledged_exhausted': True}, {'unacknowledged_pending_failed': 1}])
def test_acked_gap_cannot_hide_new_errors_or_unverified_observations(case, updates):
    operator.apply(case.root, request(case))
    _status(case.data, **{**observed(case), **updates})
    report = health_report.attachment_ocr_health(str(case.data), CFG, 1000)
    assert report.get('alert_required') is not False
    assert report['state'] not in ('ok', 'disabled')


def test_live_source_change_invalidates_even_fresh_ack_status(case):
    operator.apply(case.root, request(case))
    _status(case.data, **observed(case))
    case.ledger.save_messages([_message(1, body='完全合成の改訂本文。')])
    report = health_report.attachment_ocr_health(str(case.data), CFG, 1000)
    assert report['state'] == 'unknown' and report['reason'] == 'attachment_ocr_ack_unverified'
    assert report.get('alert_required') is not False


def test_cli_plan_and_apply_use_fixed_nonclinical_summary(case, capsys):
    assert operator.main(['--root', str(case.root), 'plan', '--artifact-id', str(case.artifact)]) == 0
    value = json.loads(capsys.readouterr().out)
    path = case.data / 'synthetic-plan.json'
    path.write_text(json.dumps(value))
    assert operator.main(['--root', str(case.root), 'apply', '--plan', str(path),
                         '--command-id', str(uuid.uuid4()), '--actor', 'synthetic-operator',
                         '--reason', '合成の運用警報を受付', '--confirm-human']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['outcome'] == 'applied' and result['source_coverage'] == 'unrecovered'
    assert 'reason' not in result and 'actor' not in result and 'backup' not in result


def test_partial_cohort_and_expired_read_budget_never_prove_complete_ack(case):
    operator.apply(case.root, request(case))
    with case.db:
        case.db.executemany("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                            "VALUES(?,1,'downloaded',?,32,1)",
                            [(value, 'a' * 64) for value in range(2, 258)])
    counts = observed(case)
    assert counts['queue_status'] == 'partial' and counts['pending_failed'] == 1
    _status(case.data, **counts)
    assert health_report.attachment_ocr_health(str(case.data), CFG, 1000).get('alert_required') is not False
    case.db.execute('BEGIN')
    try:
        limited = ack.failure_counts(case.db, deadline=0)
        assert limited['queue_status'] == 'partial' and limited['acknowledged_exhausted'] == 0
    finally:
        case.db.rollback()


def test_ack_never_changes_overlap_rejection(case):
    operator.apply(case.root, request(case))
    document = {'paragraphs': [], 'tables': [{'cells': [
        {'row': 1, 'col': 1, 'contents': '合成A'},
        {'row': 1, 'col': 1, 'contents': '合成B'}]}]}
    with pytest.raises(ValueError, match='output_schema_table_overlap'):
        ocr._document_text(document)
    assert observed(case)['acknowledged_exhausted'] == 1


@pytest.mark.parametrize('observe_ack', [False, True])
def test_new_failure_realerts_even_inside_old_episode_or_missed_ack(case, observe_ack):
    _health(case.root, overall='degraded', reasons=['attachment_ocr_processing_failed'])
    _status(case.data, **observed(case))
    first = health_watch.evaluate(str(case.root), 1000, cfg=CFG)
    assert first['alert']
    operator.apply(case.root, request(case))
    _status(case.data, at=1001, **observed(case))
    if observe_ack:
        middle = health_watch.evaluate(str(case.root), 1001, cfg=CFG)
        assert middle['status'] == 'degraded' and not middle['alert']
    case.ledger.artifact_add(ocr.KIND, json.dumps(case.doc), project_id=1, message_id=1,
                            meta={'attachment_id': 1})
    _status(case.data, at=1002, **observed(case))
    last = health_watch.evaluate(str(case.root), 1002, cfg=CFG)
    assert last['alert'] and last['attachment_ocr']['unacknowledged_pending_failed'] == 1


def test_reacknowledge_current_proof_keeps_valid_old_ack_history(case):
    first = operator.apply(case.root, request(case))
    case.ledger.save_messages([_message(1, body='完全合成の別版。')])
    case.ledger.save_messages([_message(1, body='完全合成の添付を確認しました。')])
    assert observed(case)['acknowledged_exhausted'] == 0
    req = request(case)
    second = operator.apply(case.root, req)
    assert second['acknowledgement_ids'] != first['acknowledgement_ids']
    assert observed(case)['acknowledged_exhausted'] == 1
    assert len(case.ledger.artifacts(ack.KIND)) == 2
    assert operator.apply(case.root, req)['replayed']
    assert len(case.ledger.artifacts(ack.KIND)) == 2


def test_real_database_replace_invalidates_old_plan_and_allows_new_proof(case):
    req = request(case)
    operator.apply(case.root, req)
    path = case.root / 'replacement.db'
    with sqlite3.connect(path) as replacement:
        case.db.backup(replacement)
    case.ledger.close()
    os.replace(path, case.data / 'ledger.db')
    with sqlite3.connect(case.data / 'ledger.db') as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        assert not ack.current_acknowledged(db, case.artifact)
        new_plan = ack.build_plan(db, [case.artifact])
        db.rollback()
        with pytest.raises(ValueError, match='stale_plan'):
            operator.apply(case.root, {**req, 'command_id': str(uuid.uuid4())})
        fresh = {**req, 'command_id': str(uuid.uuid4()), 'plan': new_plan}
        operator.apply(case.root, fresh)
        db.execute('BEGIN')
        assert ack.current_acknowledged(db, case.artifact)
        db.rollback()


@pytest.mark.parametrize('field,value', [('human_confirmed', False), ('human_confirmed', None),
    ('actor', None), ('reason', '別の合成理由'), ('ocr_outcome', 'SYNTHETIC_PRIVATE_PATH'),
    ('source_coverage', 'SYNTHETIC_PRIVATE_PATH'), ('targets', 1), ('targets', None),
    ('acknowledgement_ids', [True])])
def test_corrupt_receipt_never_suppresses_or_echoes_private_values(case, capsys, field, value):
    req = request(case)
    operator.apply(case.root, req)
    row = case.db.execute('SELECT receipt_json FROM command_receipts WHERE command_id=?',
                          (req['command_id'],)).fetchone()
    receipt = json.loads(row[0])
    receipt[field] = value
    with case.db:
        case.db.execute('UPDATE command_receipts SET receipt_json=? WHERE command_id=?',
                        (json.dumps(receipt), req['command_id']))
    assert observed(case)['acknowledged_exhausted'] == 0
    plan_file = case.data / 'replay-plan.json'
    plan_file.write_text(json.dumps(req['plan']))
    rc = operator.main(['--root', str(case.root), 'apply', '--plan', str(plan_file),
        '--command-id', req['command_id'], '--actor', req['actor'], '--reason', req['reason'], '--confirm-human'])
    text = capsys.readouterr().out
    assert rc == 2 and json.loads(text)['error'] == 'ocr_ack_receipt_unverified'
    assert 'SYNTHETIC_PRIVATE_PATH' not in text


@pytest.mark.parametrize('kind', ['ack', 'receipt'])
def test_oversized_evidence_is_not_parsed_as_current_ack(case, kind):
    receipt = operator.apply(case.root, request(case))
    with case.db:
        if kind == 'ack':
            raw = case.db.execute('SELECT content FROM artifacts WHERE artifact_id=?',
                                  (receipt['acknowledgement_ids'][0],)).fetchone()[0]
            case.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                            (raw + ' ' * 65536, receipt['acknowledgement_ids'][0]))
        else:
            raw = case.db.execute('SELECT receipt_json FROM command_receipts WHERE command_id=?',
                                  (receipt['command_id'],)).fetchone()[0]
            case.db.execute('UPDATE command_receipts SET receipt_json=? WHERE command_id=?',
                            (raw + ' ' * 65536, receipt['command_id']))
    assert observed(case)['acknowledged_exhausted'] == 0


@pytest.mark.parametrize('first,last', [(1, 0), (0, 1)])
def test_duplicate_status_key_cannot_hide_current_failure(case, first, last):
    operator.apply(case.root, request(case))
    path = _status(case.data, **observed(case))
    raw = path.read_text().replace('"failed": 0', '"failed": '+str(first))
    path.write_text(raw[:-1] + ',"failed":'+str(last)+'}')
    report = health_report.attachment_ocr_health(str(case.data), CFG, 1000)
    assert report['state'] == 'unknown' and report['reason'] == 'attachment_ocr_status_corrupt'
    assert report.get('alert_required') is not False


def test_deadline_crossed_by_last_ack_is_not_a_complete_or_suppressible_observation(case, monkeypatch):
    operator.apply(case.root, request(case))
    _status(case.data, **observed(case))
    clock = SimpleNamespace(now=0)
    fake_time = SimpleNamespace(monotonic=lambda: clock.now)
    original = ack.acknowledged
    def overrun(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.now = kwargs['deadline'] + 1
        return result
    monkeypatch.setattr(ack, 'time', fake_time)
    monkeypatch.setattr(ack, 'acknowledged', overrun)
    case.db.execute('BEGIN')
    counts = ack.failure_counts(case.db, deadline=1)
    case.db.rollback()
    assert counts['queue_status'] == 'partial'
    clock.now = 0
    monkeypatch.setattr(health_report, 'time', fake_time)
    report = health_report.attachment_ocr_health(str(case.data), CFG, 1000)
    assert report['state'] == 'unknown' and report.get('alert_required') is not False


@pytest.mark.parametrize('marker', ['update_in_progress.marker', 'restore_pending.json'])
def test_restore_or_update_fence_blocks_health_suppression(case, marker):
    operator.apply(case.root, request(case))
    _status(case.data, **observed(case))
    (case.data / marker).write_text('{}')
    report = health_report.attachment_ocr_health(str(case.data), CFG, 1000)
    assert report['state'] == 'unknown' and report.get('alert_required') is not False


def test_unknown_degraded_cause_is_not_relabelled_as_only_acknowledged_ocr(case):
    operator.apply(case.root, request(case))
    _status(case.data, **observed(case))
    _health(case.root, overall='degraded', reasons=[])
    report = health_watch.evaluate(str(case.root), 1000, cfg=CFG)
    assert report['status'] == 'degraded' and report['alert']
    assert 'unknown' in report['state_reasons']


def test_relative_root_produces_same_durable_receipt(case, monkeypatch):
    req = request(case)
    before = originals(case)
    monkeypatch.chdir(case.root.parent)
    receipt = operator.apply(case.root.name, req)
    assert receipt['outcome'] == 'applied' and receipt['receipt_state'] == 'saved'
    assert (case.data / receipt['backup']).is_file()
    assert not receipt['backup'].startswith('/') and originals(case) == before


@pytest.mark.parametrize('outcome', ['pending', 'unknown'])
def test_ack_stops_old_unsent_alert_and_preserves_unknown_delivery(case, outcome):
    _health(case.root, overall='degraded', reasons=['attachment_ocr_processing_failed'])
    _status(case.data, **observed(case))
    report = health_watch.evaluate(str(case.root), 1000, cfg=CFG)
    assert report['alert']
    state_file = case.root / health_watch.STATE_REL
    state = json.loads(state_file.read_text())
    state['delivery']['outcome'] = outcome
    state_file.write_text(json.dumps(state))
    operator.apply(case.root, request(case))
    _status(case.data, at=1001, **observed(case))
    report = health_watch.evaluate(str(case.root), 1001, cfg=CFG)
    assert not report['alert'] and report['status'] == 'degraded'
    final = json.loads(state_file.read_text())
    if outcome == 'pending':
        assert final['delivery']['outcome'] == 'superseded'
    else:
        assert final['unknown_deliveries'][-1]['outcome'] == 'unknown'
        assert final.get('delivery', {}).get('outcome') != 'pending'
