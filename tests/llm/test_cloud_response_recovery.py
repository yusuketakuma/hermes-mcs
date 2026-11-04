"""Paid answers survive restart; exact receipts never authorize another HTTP attempt."""
from contextlib import closing, nullcontext
import json
import time
from types import SimpleNamespace

import pytest

import anthropic_client
import cloud_backlog
import cloud_client
import cloud_inference
import cloud_policy
from ledger import Ledger
from mcs_util import payload_hash
from semantic_testkit import _message, _patient


@pytest.fixture
def paid(tmp_path, monkeypatch):
    path = str(tmp_path / 'synthetic.db')
    with closing(Ledger(path)) as ledger:
        _patient(ledger)
        ledger.save_messages([_message()])
    cfg = {'cloud_llm': {'mode': 'on', 'provider': 'anthropic', 'project_ids': [1]}}
    calls, locks, state = [], [], {'current': True, 'generation': 1, 'crash': False}
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(cloud_policy, 'selected_key', lambda *_args: 'synthetic-key')
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    monkeypatch.setattr(cloud_inference.writer_lock, 'updater_state', lambda: (False, state['generation']))
    monkeypatch.setattr(cloud_backlog, 'owner_alive', lambda: True)
    monkeypatch.setattr(cloud_backlog, 'request_slot', lambda *_args: locks.append(True) or nullcontext(True))
    original = anthropic_client.AnthropicClient

    def post(body, _timeout):
        calls.append(body)
        if state.get('reject_first') and len(calls) == 1:
            return 429, {'Retry-After': '0'}, b'{}'
        return 200, {}, json.dumps({'model': original.MODEL, 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': '{"synthetic":"answer"}'}],
            'usage': {'input_tokens': 10, 'output_tokens': 2}}).encode()

    monkeypatch.setattr(anthropic_client, 'AnthropicClient',
                        lambda key, **kwargs: original(key, post_fn=post, **kwargs))
    record = cloud_policy.record_result

    class Restart(BaseException):
        pass

    def store(*args, **kwargs):
        result = record(*args, **kwargs)
        if state['crash']:
            raise Restart()
        return result

    monkeypatch.setattr(cloud_policy, 'record_result', store)

    def unit(ledger, *, binding='original-input', owner='first', current=None, hook=None):
        return cloud_inference.CloudInferenceUnit(ledger, cfg, route='reextract', project_id=1,
            message_id=1, unit_key='synthetic-unit', deadline=time.monotonic() + 120,
            current=current or (lambda: state['current']), dispatch=lambda fn, **_kw: fn(),
            owner={'lease': owner}, input_binding=payload_hash(binding), on_reserved_tx=hook)

    return path, cfg, calls, locks, state, unit, Restart


def _ask(unit, tokens=100):
    return unit('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=tokens)


@pytest.mark.parametrize('observation', ['unknown', 'changed'])
def test_post_response_observation_distinguishes_unknown_from_changed_scope(paid, observation):
    path, _cfg, calls, _locks, _state, unit, _restart = paid
    def current():
        if calls:
            if observation == 'unknown':
                raise cloud_inference.CloudObservationIncomplete('cloud_current_unverified')
            return False
        return True
    with closing(Ledger(path)) as ledger:
        provider = unit(ledger, current=current)
        error = (cloud_inference.CloudObservationIncomplete if observation == 'unknown'
                 else cloud_inference.CloudFallback)
        with pytest.raises(error):
            _ask(provider)
        assert len(calls) == 1
        result = ledger.artifacts('cloud_usage_result')[0]
        metadata = json.loads(result['meta'])
        assert metadata['http_class'] == 'http_200'
        assert json.loads(result['content'])['text'] == '{"synthetic":"answer"}'
        assert bool(metadata.get('disposition')) is (observation == 'changed')
        assert provider.discarded is (observation == 'changed')
        before = cloud_policy.usage_report(ledger)
    with closing(Ledger(path)) as ledger:
        if observation == 'unknown':
            answer = _ask(unit(ledger, owner='verified-restart'))
            assert answer['reused_response'] is True
            assert cloud_policy.usage_report(ledger) == before
        else:
            with pytest.raises(cloud_client.CloudNotSent):
                _ask(unit(ledger, owner='verified-restart'))
        assert len(calls) == 1


@pytest.mark.parametrize('waiting', ['deadline', 'owner', 'budget', 'read_error'])
def test_temporary_send_gate_keeps_previously_paid_answer(paid, monkeypatch, waiting):
    path, cfg, calls, _locks, _state, unit, _restart = paid
    with closing(Ledger(path)) as ledger:
        provider = unit(ledger)
        original = _ask(provider)
        before = [tuple(row) for row in ledger.db.execute(
            "SELECT * FROM artifacts WHERE kind LIKE 'cloud_usage_%' ORDER BY artifact_id")]
        with monkeypatch.context() as patch:
            if waiting == 'deadline':
                later = time.monotonic() + 1000
                patch.setattr(cloud_inference, 'time', SimpleNamespace(monotonic=lambda: later))
            elif waiting == 'owner':
                patch.setattr(cloud_backlog, 'owner_alive', lambda: False)
            elif waiting == 'budget':
                patch.setitem(cfg['cloud_llm'], 'daily_cost_limit_usd', 0)
            else:
                def unreadable(*args, **kwargs):
                    raise ValueError('synthetic accounting read failure')
                patch.setattr(cloud_policy, 'block_reason', unreadable)
            with pytest.raises(cloud_inference.CloudObservationIncomplete):
                provider.guard()
            provider.commit_guard()  # A send denial does not revoke adoption authority.
            assert not provider.discarded and provider.final_disposition is None
        recovered = _ask(unit(ledger, owner='verified-restart'))
        assert recovered.pop('reused_response') is True and recovered == original
        assert len(calls) == 1
        assert [tuple(row) for row in ledger.db.execute(
            "SELECT * FROM artifacts WHERE kind LIKE 'cloud_usage_%' ORDER BY artifact_id")] == before


def test_restart_reuses_atomic_paid_answer_without_capacity_reservation_or_new_bill(paid):
    path, cfg, calls, locks, state, unit, restart = paid
    state['crash'] = True
    with closing(Ledger(path)) as ledger:
        with pytest.raises(restart):
            _ask(unit(ledger))
        assert len(ledger.artifacts('cloud_usage_reserve')) == len(ledger.artifacts('cloud_usage_result')) == 1
        result = ledger.artifacts('cloud_usage_result')[0]
        assert json.loads(result['content'])['text'] == '{"synthetic":"answer"}'
        assert json.loads(result['content'])['text'] not in result['meta']
        before = cloud_policy.usage_report(ledger)
    state['crash'] = False
    cfg['cloud_llm']['daily_cost_limit_usd'] = 0
    with closing(Ledger(path)) as ledger:
        provider = unit(ledger, owner='second', hook=lambda *_a: pytest.fail('a spent slot cannot reserve again'))
        answer = _ask(provider)
        assert answer['text'] == '{"synthetic":"answer"}'
        assert answer['reused_response'] is True
        assert provider.completed_reservations[0]['owner'] == {'lease': 'first'}
        assert cloud_policy.usage_report(ledger) == before
        assert len(calls) == len(locks) == 1


def test_recovery_keeps_a_known_rejected_attempt_and_reuses_the_later_paid_answer(paid):
    path, _cfg, calls, locks, state, unit, _restart = paid
    state['reject_first'] = True
    with closing(Ledger(path)) as ledger:
        answer = _ask(unit(ledger))
        assert len(calls) == 2 and len(ledger.artifacts('cloud_usage_reserve')) == 2
    with closing(Ledger(path)) as ledger:
        recovered = _ask(unit(ledger, owner='restart'))
        assert recovered.pop('reused_response') is True
        assert recovered == answer
        assert len(calls) == 2 and len(locks) == 1


@pytest.mark.parametrize('damage', ['answer', 'duplicate', 'unknown', 'missing_answer',
                                   'missing_receipt', 'missing_reservation', 'discarded', 'legacy', 'input', 'tokens'])
def test_invalid_or_different_paid_evidence_never_replays_or_resends(paid, damage):
    path, _cfg, calls, _locks, _state, unit, _restart = paid
    with closing(Ledger(path)) as ledger:
        _ask(unit(ledger))
        with ledger.db:
            if damage in ('answer', 'missing_answer'):
                content = '{}' if damage == 'missing_answer' else '{"text":"tampered","finish_reason":"stop"}'
                ledger.db.execute("UPDATE artifacts SET content=? WHERE kind='cloud_usage_result'", (content,))
            elif damage == 'duplicate':
                row = ledger.artifacts('cloud_usage_result')[0]
                ledger.artifact_add_tx('cloud_usage_result', row['content'], project_id=1, message_id=1,
                                       model=row['model'], meta=json.loads(row['meta']))
            elif damage == 'unknown':
                ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.http_class','transport',"
                                  "'$.error_kind','transport','$.usage',NULL) WHERE kind='cloud_usage_result'")
            elif damage == 'missing_receipt':
                ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_result'")
            elif damage == 'missing_reservation':
                ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_reserve'")
            elif damage == 'discarded':
                reservation = json.loads(ledger.artifacts('cloud_usage_reserve')[0]['meta'])
                ledger.db.execute('BEGIN')
                cloud_policy.record_disposition_tx(ledger, reservation, 'discarded_scope')
            elif damage == 'legacy':
                ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.input_binding'),content='{}' "
                                  "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')")
    with closing(Ledger(path)) as ledger:
        with pytest.raises(cloud_client.CloudNotSent):
            _ask(unit(ledger, binding='changed' if damage == 'input' else 'original-input'),
                 tokens=101 if damage == 'tokens' else 100)
        assert len(calls) == 1
        assert len(ledger.artifacts('cloud_usage_reserve')) == (0 if damage == 'missing_reservation' else 1)


def test_patient_input_recovery_binding_detects_aba_even_when_original_text_returns(paid):
    import mcs_queries
    import patient_context_refresh

    path, *_ = paid
    with closing(Ledger(path)) as ledger:
        def binding():
            return patient_context_refresh._cloud_input_binding(ledger.db, 1,
                {'source_sig': 'same-input', 'contract': {'version': 'synthetic'},
                 'sources': list(mcs_queries.patient_context_sources(ledger.db, 1, hydrate=False))})
        original = binding()
        ledger.save_messages([_message(body='合成の変更後本文。')])
        ledger.save_messages([_message()])
        assert binding() != original


@pytest.mark.parametrize('denial', ['owner', 'updater', 'scope'])
def test_recovery_rechecks_current_adoption_authority(paid, denial):
    path, cfg, calls, _locks, state, unit, _restart = paid
    with closing(Ledger(path)) as ledger:
        _ask(unit(ledger))
    with closing(Ledger(path)) as ledger:
        provider = unit(ledger, owner='new')
        if denial == 'owner':
            state['current'] = False
        elif denial == 'updater':
            state['generation'] += 1
        else:
            cfg['cloud_llm']['project_ids'] = []
        with pytest.raises(cloud_inference.CloudFallback):
            _ask(provider)
        assert len(calls) == 1


def test_response_insert_failure_leaves_reservation_unknown_and_never_resends(paid):
    path, _cfg, calls, _locks, _state, unit, _restart = paid
    with closing(Ledger(path)) as ledger:
        ledger.db.execute("CREATE TRIGGER stop_result BEFORE INSERT ON artifacts "
                          "WHEN NEW.kind='cloud_usage_result' BEGIN SELECT RAISE(ABORT,'synthetic disk fault'); END")
        ledger.db.commit()
        with pytest.raises(cloud_inference.CloudFallback):
            _ask(unit(ledger))
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1
        assert not ledger.artifacts('cloud_usage_result')
        ledger.db.execute('DROP TRIGGER stop_result')
        ledger.db.commit()
    with closing(Ledger(path)) as ledger:
        with pytest.raises(cloud_client.CloudNotSent):
            _ask(unit(ledger, owner='new'))
        assert len(calls) == 1
