"""Reachable recovery failures stay refused behind fixed whitelist reasons."""
from contextlib import closing
import json
import time

import pytest

import cloud_client
import cloud_inference
import cloud_policy
from ledger import Ledger
from mcs_util import payload_hash
from test_cloud_response_recovery import legacy_cloud_routes, paid as paid


def _ask(unit):
    return unit('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)


def _history(ledger):
    return [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]


def _recover(ledger):
    reservation = json.loads(ledger.artifacts('cloud_usage_reserve')[0]['meta'])
    return cloud_policy.recover_response(ledger, route=reservation['route'], project_id=1,
        message_id=1, unit_key=reservation['unit_key'], input_binding=reservation['input_binding'],
        request_fp=reservation['request_fp'])


def _events(capsys):
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    for record in records:
        record.pop("pid")
        record.pop("ts")
    return records


def _damage(ledger, damage):
    with ledger.db:
        if damage == 'missing':
            ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_result'")
        elif damage == 'duplicate':
            row = ledger.artifacts('cloud_usage_result')[0]
            ledger.artifact_add_tx('cloud_usage_result', row['content'], project_id=1,
                                   message_id=1, model=row['model'], meta=json.loads(row['meta']))
        elif damage in ('discarded', 'missing_discarded'):
            ledger.db.execute('BEGIN')
            if damage == 'missing_discarded':
                ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_result'")
            reservation = json.loads(ledger.artifacts('cloud_usage_reserve')[0]['meta'])
            cloud_policy.record_disposition_tx(ledger, reservation, 'discarded_scope')
        elif damage == 'unsettled':
            ledger.db.execute("UPDATE artifacts SET "
                              "meta=json_set(meta,'$.http_class','http_500','$.error_kind','transport') "
                              "WHERE kind='cloud_usage_result'")
        elif damage == 'binding_mismatch':
            ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.request_fp') "
                              "WHERE kind='cloud_usage_result'")
        elif damage == 'answerless_body':
            ledger.db.execute("UPDATE artifacts SET content='{}' "
                              "WHERE kind='cloud_usage_result'")
        elif damage == 'hash_mismatch':
            ledger.db.execute("UPDATE artifacts SET "
                              "content='{\"text\":\"tampered\",\"finish_reason\":\"stop\"}' "
                              "WHERE kind='cloud_usage_result'")


@pytest.mark.parametrize('damage,reason', [
    ('missing', 'cloud_result_missing'),
    ('missing_discarded', 'cloud_result_discarded'),
    ('duplicate', 'cloud_receipt_invalid'),
    ('discarded', 'cloud_result_discarded'),
    ('unsettled', 'cloud_result_unsettled'),
    ('binding_mismatch', 'cloud_receipt_invalid'),
    ('answerless_body', 'cloud_answer_unresolved'),
    ('hash_mismatch', 'cloud_answer_invalid')])
def test_reachable_failure_refuses_with_a_fixed_reason(paid, capsys, damage, reason):
    path, _cfg, calls, _locks, _state, unit, _restart = paid
    with closing(Ledger(path)) as ledger:
        with legacy_cloud_routes():
            _ask(unit(ledger))
        _damage(ledger, damage)
        before = _history(ledger)
        with pytest.raises(ValueError, match=f'^{reason}$'):
            _recover(ledger)
        with pytest.raises(cloud_client.CloudNotSent):
            _ask(unit(ledger, owner='restart'))
        assert len(calls) == 1 and _history(ledger) == before
    assert _events(capsys) == [{"event": "cloud_history_read_error", "phase": "recover_response",
                              "error_category": "value_error", "error_reason": reason}]


def test_saved_answer_recovers_without_new_send(paid):
    path, _cfg, calls, locks, _state, unit, _restart = paid
    with closing(Ledger(path)) as ledger:
        with legacy_cloud_routes():
            original = _ask(unit(ledger))
        before = _history(ledger)
        recovered = _ask(unit(ledger, owner='restart'))
        assert recovered.pop('reused_response') is True and recovered == original
        assert len(calls) == len(locks) == 1 and _history(ledger) == before


def test_unbound_reservation_writes_accounting_only_result(paid):
    path, cfg, _calls, _locks, _state, unit, _restart = paid
    with closing(Ledger(path)) as ledger:
        provider = cloud_inference.CloudInferenceUnit(ledger, cfg, route='reextract', project_id=1,
            message_id=1, unit_key='synthetic-unbound', deadline=time.monotonic() + 60,
            current=lambda: True, dispatch=lambda fn, **kw: fn(),
            owner={'lease': 'first'}, input_binding=None)
        with legacy_cloud_routes():
            _ask(provider)
        result = ledger.artifacts('cloud_usage_result')[0]
        metadata = json.loads(result['meta'])
        assert result['content'] == '{}'
        assert isinstance(metadata['answer_text_sha256_v1'], str)
        assert metadata['answer_finish_reason_v1'] == 'stop'
        assert 'input_binding' not in json.loads(ledger.artifacts('cloud_usage_reserve')[0]['meta'])


def test_unrelated_answerless_row_does_not_block_a_saved_answer(paid):
    path, _cfg, calls, _locks, _state, unit, _restart = paid
    with closing(Ledger(path)) as ledger:
        with legacy_cloud_routes():
            original = _ask(unit(ledger))
        with ledger.db:
            meta = json.loads(ledger.artifacts('cloud_usage_reserve')[0]['meta'])
            meta.update(unit_key='other-unit', request_fp=payload_hash('other-request'),
                        reservation_id='f' * 32)
            ledger.artifact_add_tx('cloud_usage_reserve', None, project_id=1, message_id=1,
                                   model=meta['model'], meta=meta)
            result = json.loads(ledger.artifacts('cloud_usage_result')[0]['meta'])
            result.update(reservation_id='f' * 32, request_fp=payload_hash('other-request'))
            ledger.artifact_add_tx('cloud_usage_result', '{}', project_id=1, message_id=1,
                                   model=result['model'], meta=result)
        recovered = _ask(unit(ledger, owner='restart'))
        assert recovered.pop('reused_response') is True and recovered == original
        assert len(calls) == 1
