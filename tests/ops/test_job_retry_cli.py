"""Synthetic diagnosis-bound retries use existing receipts and safety checks."""
import json
import uuid

import pytest

import cloud_cli
import job_cli
import job_diagnostics
import mcs_cli
import mcs_requests
import mcs_util
from inference_version import CURRENT_VERSION, VERSION_KEY
from ledger import Ledger
from mcs_util import payload_hash


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path
    (root / 'data').mkdir()
    ledger = Ledger(str(root / 'data/ledger.db'))
    ledger.ensure_patient(1)
    ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,body_state) VALUES(42,1,'synthetic-private','full')")
    payload = {VERSION_KEY: CURRENT_VERSION, 'generation': 'g1', 'targets': [42]}
    job = ledger.job_add('semantic', 1, 42, payload=payload)
    ledger.db.execute("UPDATE fetch_jobs SET state='failed',attempts=6 WHERE job_id=?", (job,))
    ledger.db.commit()
    cfg = {'semantic': {'mode': 'assist', 'project_ids': [1]}}
    monkeypatch.setattr(mcs_util, 'HOME', str(root))
    monkeypatch.setattr(mcs_util, 'load_config', lambda: cfg)
    yield ledger, job, payload, cfg
    ledger.close()


def request(world):
    ledger, job, payload, cfg = world
    ledger.db.execute('BEGIN')
    report = job_diagnostics.diagnose_job(ledger.db, cfg, job)
    ledger.db.rollback()
    return {'version': 1, 'cmd': 'ops.retry', 'command_id': str(uuid.uuid4()),
            'actor': 'synthetic-operator', 'human_confirmed': True, 'project_id': 1,
            'job_id': job, 'expected_payload_hash': payload_hash(payload),
            'diagnosis_hash': report['diagnosis_hash'], 'remediation': report['remediation_code'],
            'additional_attempts': 1, 'reason': 'synthetic investigation and corrective action'}


def test_retry_unknown_reason_after_human_investigation_preserves_history_and_receipt(world):
    ledger, job, payload, cfg = world
    req = request(world)
    first = mcs_requests.apply_command(ledger, req, cfg=cfg)
    assert first['outcome'] == 'applied'
    assert mcs_requests.apply_command(ledger, req, cfg=cfg) == first
    row = ledger.db.execute('SELECT state,attempts,payload FROM fetch_jobs WHERE job_id=?', (job,)).fetchone()
    assert row['state'] == 'pending' and row['attempts'] == 6
    actual = json.loads(row['payload'])
    assert actual['generation'] == payload['generation'] and actual['manual_attempt_limit'] == 7


@pytest.mark.parametrize('change,error', [('source', 'diagnosis_changed'), ('scope', 'diagnosis_changed'),
                                          ('remediation', 'remediation_mismatch')])
def test_retry_rechecks_diagnosis_in_receipt_transaction(world, change, error):
    ledger, job, payload, cfg = world
    req = request(world)
    if change == 'source':
        ledger.db.execute("UPDATE messages SET body_text='synthetic-changed' WHERE message_id=42")
        ledger.db.commit()
    elif change == 'scope':
        cfg['semantic']['project_ids'] = [2]
    else:
        req['remediation'] = 'budget'
    assert mcs_requests.apply_command(ledger, req, cfg=cfg)['error'] == error
    assert ledger.db.execute('SELECT state FROM fetch_jobs WHERE job_id=?', (job,)).fetchone()[0] == 'failed'


def test_retry_validation_requires_human_reason_and_paired_fields(world):
    req = request(world)
    for changes in ({'human_confirmed': False}, {'reason': ''}, {'diagnosis_hash': 'bad'}, {'remediation': 'arbitrary'}):
        assert mcs_requests.validate({**req, **changes}) is not None


def test_cli_unconfirmed_retry_never_opens_reader(monkeypatch, capsys):
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(cloud_cli, '_reader', lambda: pytest.fail('unconfirmed read'))
    assert job_cli.main(['retry', '--job-id', '1', '--diagnosis-hash', '0'*64,
                         '--expected-payload-hash', '0'*64, '--remediation', 'investigate']) == 1
    assert '--confirm-human' in capsys.readouterr().out


def test_cli_failed_list_is_content_free_and_public_route_works(world, monkeypatch, capsys):
    monkeypatch.setenv('MCS_LIFECYCLE_PINNED', '1')
    assert mcs_cli.main(['jobs', 'failed']) == 0
    text = capsys.readouterr().out
    assert 'synthetic-private' not in text and 'project_id' not in text
    report = json.loads(text)
    assert report['jobs'][0]['reason_code'] == 'unknown'


def test_cli_retry_uses_writer_lock_and_identical_command_is_replayable(world, monkeypatch, capsys):
    import writer_lock
    ledger, job, payload, cfg = world
    req = request(world)
    calls = []
    real = writer_lock.acquire_run_lock
    monkeypatch.setattr(writer_lock, 'acquire_run_lock', lambda path: calls.append(path) or real(path))
    args = ['retry', '--job-id', str(job), '--diagnosis-hash', req['diagnosis_hash'],
            '--expected-payload-hash', req['expected_payload_hash'], '--remediation', req['remediation'],
            '--additional-attempts', '1', '--confirm-human', '--reason', req['reason'], '--command-id', req['command_id']]
    assert job_cli.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert job_cli.main(args) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert len(calls) == 2


@pytest.mark.parametrize('block', ['off', 'legacy'])
def test_matching_diagnosis_cannot_override_blockers(world, block):
    ledger, job, payload, cfg = world
    if block == 'off':
        cfg['semantic']['mode'] = 'off'
    else:
        from inference_version import LEGACY_VERSION
        payload[VERSION_KEY] = LEGACY_VERSION
        ledger.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?', (json.dumps(payload), job))
        ledger.db.commit()
    req = request(world)
    receipt = mcs_requests.apply_command(ledger, req, cfg=cfg)
    assert receipt['error'] == 'retry_blocked'
    assert ledger.db.execute('SELECT state FROM fetch_jobs WHERE job_id=?', (job,)).fetchone()[0] == 'failed'


def test_retry_below_limit_does_not_extend_attempts(world):
    ledger, job, payload, cfg = world
    ledger.db.execute('UPDATE fetch_jobs SET attempts=2 WHERE job_id=?', (job,))
    ledger.db.commit()
    req = request(world)
    req.pop('additional_attempts')
    assert mcs_requests.apply_command(ledger, req, cfg=cfg)['outcome'] == 'applied'
    row = ledger.db.execute('SELECT attempts,payload FROM fetch_jobs WHERE job_id=?', (job,)).fetchone()
    assert row[0] == 2 and 'manual_attempt_limit' not in json.loads(row[1])
