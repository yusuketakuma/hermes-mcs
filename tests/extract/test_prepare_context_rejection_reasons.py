"""Synthetic preparation diagnostics remain fixed while source holds stay unchanged."""
import extract_llm_prepare
import extract_llm_runner
import json

import pytest

import cloud_backlog
import extract_llm as worker
from test_anthropic_runtime import old, routed as routed


CASES = [
    ('load', 'pending_context_unverifiable', 'prepare_context_load_unverifiable'),
    ('load', 'pending_context_reference_changed', 'prepare_context_load_reference_changed'),
    ('medication', 'medication_reference_input_invalid', 'prepare_context_medication_input_invalid'),
    ('medication', 'medication_reference_snapshot_invalid', 'prepare_context_medication_snapshot_invalid'),
    ('prepare', 'context_transaction_owned', 'prepare_context_prepare_transaction_owned'),
    ('prepare', 'invalid_context_options', 'prepare_context_prepare_options_invalid'),
    ('prepare', 'context_budget_exceeded', 'prepare_context_prepare_budget_exceeded'),
    ('prepare', 'context_medication_hints_invalid', 'prepare_context_prepare_medication_invalid'),
    ('prepare', 'context_source_changed', 'prepare_context_prepare_source_changed'),
    ('prepare', 'context_snapshot_invalid', 'prepare_context_prepare_snapshot_invalid'),
]


def reject_stage(monkeypatch, stage, error, *, only_mid=None):
    pending = worker._pending_context_frame
    build = worker.extraction_medications.build
    prepare = worker.extraction_context.prepare
    current, reached = [None], []

    def fail_if_selected(at):
        if at == stage and (only_mid is None or current[0] == only_mid):
            reached.append(current[0])
            raise error

    def load(db, row, **kwargs):
        current[0] = row['message_id']
        fail_if_selected('load')
        return pending(db, row, **kwargs)

    def medication(*args, **kwargs):
        fail_if_selected('medication')
        return build(*args, **kwargs)

    def frame(*args, **kwargs):
        fail_if_selected('prepare')
        return prepare(*args, **kwargs)

    monkeypatch.setattr(worker, '_pending_context_frame', load)
    monkeypatch.setattr(extract_llm_prepare, '_pending_context_frame', worker._pending_context_frame)
    monkeypatch.setattr(worker.extraction_medications, 'build', medication)
    monkeypatch.setattr(worker.extraction_context, 'prepare', frame)
    return reached


@pytest.mark.parametrize('stage,code,expected', CASES)
def test_prepare_records_one_fixed_reason_without_claiming_or_changing_originals(routed, monkeypatch, capsys,
                                                                                stage, code, expected):
    ledger, _cfg, calls, _reserves, _results = routed
    old(ledger, pid=2)
    rows, _fresh = worker._select_pending(ledger, 1, None, None, False)
    assert len(rows) == 1 and rows[0]['message_id'] == 1
    before = [tuple(saved) for saved in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    reached = reject_stage(monkeypatch, stage, ValueError(code))
    reasons, refused = {}, []
    jobs, skipped, blocked = worker._prepare_jobs(ledger, rows, ('', 'fictional-model'), False,
        load_checkpoints=False, blocked_reasons=reasons, blocked_rows=refused)
    assert reached == [1]
    assert jobs == [] and skipped == 0 and blocked == 1
    assert reasons == {expected: 1} and [reason for _row, reason in refused] == [expected]
    assert expected in cloud_backlog._DEFER_REASONS
    assert expected in cloud_backlog._PREPARE_CONTEXT_REJECTION_CODES
    assert [tuple(saved) for saved in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert not ledger.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim'").fetchone()
    assert not calls and capsys.readouterr() == ('', '')


class Unrenderable:
    def __str__(self):
        raise AssertionError('diagnostics must not render arbitrary error arguments')

    __repr__ = __str__


@pytest.mark.parametrize('stage,error', [
    pytest.param('load', ValueError('synthetic-private-detail /synthetic/path'), id='arbitrary-string'),
    pytest.param('load', ValueError('pending_context_unverifiable', 'synthetic-private-detail'), id='multiple-arguments'),
    pytest.param('load', ValueError({'synthetic-private-detail': 1}), id='dictionary-argument'),
    pytest.param('load', ValueError(Unrenderable()), id='unrenderable-argument'),
    pytest.param('load', ValueError(), id='missing-argument'),
    pytest.param('load', ValueError('context_snapshot_invalid'), id='known-code-wrong-stage'),
    pytest.param('unknown-stage', ValueError('pending_context_unverifiable'), id='unknown-stage'),
])
def test_unknown_or_misplaced_value_error_is_only_fixed_other(stage, error, capsys):
    reason = worker._prepare_context_rejection(stage, error)
    assert reason == 'prepare_context_other'
    assert reason in cloud_backlog._DEFER_REASONS
    assert reason in cloud_backlog._PREPARE_CONTEXT_REJECTION_CODES
    assert 'synthetic-private-detail' not in json.dumps({'reason': reason})
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('stage,code,expected', [CASES[0], CASES[2], CASES[-1],
    ('load', 'synthetic-private-detail /synthetic/path', 'prepare_context_other')])
def test_new_fixed_reasons_keep_finite_hold_and_advance_to_healthy_local_source(routed, monkeypatch, capsys,
                                                                                            stage, code, expected):
    ledger, _cfg, calls, _reserves, _results = routed
    old(ledger, mid=1, pid=2)
    old(ledger, mid=2, pid=2)
    before = [tuple(saved) for saved in ledger.db.execute(
        'SELECT * FROM artifacts WHERE message_id=2 ORDER BY artifact_id')]
    reached = reject_stage(monkeypatch, stage, ValueError(code), only_mid=2)
    monkeypatch.setattr(worker, 'llm_extract', lambda *args, **kwargs: {})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    result = worker.run_pending(ledger, limit=1, budget_s=180)
    assert reached == [2]
    assert result['selected'] == 2 and result['done'] == 1 and result['failed'] == 0
    assert result['context_blocked'] == 1 and result['defer_reasons'] == {expected: 1}
    assert [tuple(saved) for saved in ledger.db.execute(
        'SELECT * FROM artifacts WHERE message_id=2 ORDER BY artifact_id')] == before
    claim = ledger.db.execute("SELECT next_try FROM fetch_jobs WHERE kind='extract_claim' AND message_id=2").fetchone()
    assert claim is not None and worker.time.time() < claim[0] <= worker.time.time() + worker._PREPARE_HOLD_S
    assert not ledger.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim' AND message_id=1").fetchone()
    assert not calls
    captured = capsys.readouterr()
    assert 'synthetic-private-detail' not in captured.out + captured.err + json.dumps(result)
