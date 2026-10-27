"""Synthetic finite-chunk patient-summary checks; no external inference."""
import time

import pytest

import semantic_audit as audit
import semantic_jev as jev
import semantic_runtime as runtime


BODY = '本人の体温は36.4℃でした。ただし前回の数値であり、現在の値は未確認です。'


def sample():
    source = {'project_id': 1, 'message_id': 1, 'source': 'chat', 'body_state': 'full',
              'content_hash': 'c' * 64, 'body_text': BODY, 'posted_at': '2026-10-01T10:00:00+09:00',
              'sender_name': 'synthetic sender'}
    facts = []
    for fid, evidence in [('a' * 64, '本人の体温は36.4℃でした。'),
                          ('b' * 64, '現在の値は未確認です。')]:
        start = BODY.index(evidence)
        facts.append({'id': fid, 'source': 'chat', 'source_key': 1, 'project_id': 1, 'message_id': 1,
            'content_hash': source['content_hash'], 'text': evidence, 'evidence': evidence,
            'evidence_span': [start, start + len(evidence)], 'quote': evidence,
            'quote_span': [start, start + len(evidence)], 'category': 'observations',
            'reported_subject': 'patient', 'temporality': 'reported', 'temporal': {},
            'details': [], 'view_bucket': 'unknown', 'relations': []})
    summary = [{'text': facts[0]['quote'], 'fact_ids': [facts[0]['id']]}]
    return summary, facts, [source]


def answer(choice, confidence=0.95):
    return {'choice': choice, 'confidence': confidence}


class FakeClient:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def evaluate(self, state, questions, deadline):
        self.calls.append((state, questions, deadline))
        value = next(self.outputs)
        if isinstance(value, Exception):
            raise value
        return {'answers': value}


def test_one_request_maps_selected_and_omitted_facts_to_the_full_source():
    summary, facts, sources = sample()
    client = FakeClient([{'q0': answer('supports'), 'q1': answer('important_missing')}])
    result = audit.audit_patient_summary_chunk(client, summary, facts, sources, time.monotonic() + 10, 0.8, on_valid_answers=lambda _: None)
    assert result['evaluated'] is True
    assert result['answers'] == {facts[0]['id']: answer('supports'), facts[1]['id']: answer('important_missing')}
    assert result['findings'] == [{'code': 'patient_summary_important_missing', 'fact_id': facts[1]['id']}]
    assert len(client.calls) == 1
    state, questions, _ = client.calls[0]
    assert len(state['context']) == 1 and state['context'][0]['text'] == BODY
    assert state['context'][0]['posted_at'] == sources[0]['posted_at']
    assert state['context'][0]['sender'] == sources[0]['sender_name']
    assert [item['source_id'] for item in state['target']['facts']] == ['s0', 's0']
    assert list(questions) == ['q0', 'q1']
    assert all('only context source s0' in q['instructions'] for q in questions.values())
    assert BODY not in repr(result)


def test_different_sources_are_not_interchangeable_evidence():
    summary, facts, sources = sample()
    other_body = '家族は服薬を確認しました。'
    other = {**sources[0], 'message_id': 2, 'body_text': other_body, 'content_hash': 'd' * 64}
    other_fact = {**facts[1], 'id': 'e' * 64, 'message_id': 2, 'source_key': 2,
                  'content_hash': other['content_hash'], 'text': other_body, 'evidence': other_body,
                  'evidence_span': [0, len(other_body)], 'quote': other_body, 'quote_span': [0, len(other_body)],
                  'reported_subject': 'family'}
    client = FakeClient([{'q0': answer('supports'), 'q1': answer('omission_ok')}])
    result = audit.audit_patient_summary_chunk(client, summary, [facts[0], other_fact], [sources[0], other],
                                               time.monotonic() + 10, 0.8, on_valid_answers=lambda _: None)
    assert result['evaluated'] and result['findings'] == [] and len(client.calls) == 1
    state, questions, _ = client.calls[0]
    assert [entry['text'] for entry in state['context']] == [BODY, other_body]
    assert [entry['source_id'] for entry in state['target']['facts']] == ['s0', 's1']
    assert 'only context source s0' in questions['q0']['instructions']
    assert 'only context source s1' in questions['q1']['instructions']


@pytest.mark.parametrize('problem', ['foreign', 'key', 'hash', 'evidence', 'quote', 'source_missing',
                                      'summary_changed', 'duplicate', 'bad_unicode', 'empty'])
def test_invalid_or_foreign_input_does_not_reach_jev(problem):
    summary, facts, sources = sample()
    if problem == 'foreign':
        facts[0]['project_id'] = 2
    elif problem == 'key':
        facts[0]['source_key'] = 2
    elif problem == 'hash':
        facts[0]['content_hash'] = 'd' * 64
    elif problem == 'evidence':
        facts[0]['evidence'] = '異なる合成記録'
    elif problem == 'quote':
        facts[0]['quote_span'] = [0, len(BODY)]
    elif problem == 'source_missing':
        sources.clear()
    elif problem == 'summary_changed':
        summary[0]['text'] = '別の合成要約'
    elif problem == 'duplicate':
        facts.append(facts[0])
    elif problem == 'bad_unicode':
        facts[0]['temporal'] = {'raw': '\ud800'}
    else:
        facts.clear()
    client = FakeClient([])
    result = audit.audit_patient_summary_chunk(client, summary, facts, sources, time.monotonic() + 10, 0.8, on_valid_answers=lambda _: None)
    assert not result['evaluated'] and result['answers'] == {}
    assert result['findings'] == [{'code': 'patient_summary_chunk_invalid'}]
    assert not client.calls


def test_long_source_is_never_silently_cut():
    summary, facts, sources = sample()
    sources[0]['body_text'] += '後続の合成記録。' * 3000
    client = FakeClient([{'q0': answer('supports')}])
    result = audit.audit_patient_summary_chunk(client, summary, facts[:1], sources, time.monotonic() + 10, 0.8, on_valid_answers=lambda _: None)
    assert result['evaluated'] and client.calls[0][0]['context'][0]['text'] == sources[0]['body_text']


@pytest.mark.parametrize('key', ['karte:01', 'karte:１', 'karte:0', 'karte:9223372036854775808'])
def test_noncanonical_karte_key_is_rejected_without_send(key):
    summary, facts, sources = sample()
    sources[0].update(source='karte_summary', message_id=0, source_key=key)
    facts[0].update(source='karte_summary', message_id=0, source_key=key)
    client = FakeClient([])
    result = audit.audit_patient_summary_chunk(client, summary, facts[:1], sources, time.monotonic() + 10, 0.8, on_valid_answers=lambda _: None)
    assert not result['evaluated'] and not client.calls


def test_partial_valid_answer_is_preserved_if_repair_transport_fails():
    summary, facts, sources = sample()
    defect = jev.JevError('protocol_error', 'q1:type_invalid')
    defect.answers, defect.failed = {'q0': answer('supports')}, ['q1']
    client = FakeClient([defect, jev.JevError('transport')])
    result = audit.audit_patient_summary_chunk(client, summary, facts, sources, time.monotonic() + 10, 0.8, on_valid_answers=lambda _: None)
    assert not result['evaluated']
    assert result['answers'] == {facts[0]['id']: answer('supports')}
    assert result['findings'][-1]['code'] == 'patient_summary_audit_unevaluated'
    assert list(client.calls[0][1]) == ['q0', 'q1'] and list(client.calls[1][1]) == ['q1']


def test_only_the_malformed_question_gets_the_existing_single_retry():
    summary, facts, sources = sample()
    defect = jev.JevError('protocol_error', 'q1:type_invalid')
    defect.answers, defect.failed = {'q0': answer('supports')}, ['q1']
    client = FakeClient([defect, {'q1': answer('omission_ok')}])
    result = audit.audit_patient_summary_chunk(client, summary, facts, sources, time.monotonic() + 10, 0.8, on_valid_answers=lambda _: None)
    assert result['evaluated'] and result['findings'] == []
    assert result['answers'] == {facts[0]['id']: answer('supports'), facts[1]['id']: answer('omission_ok')}
    assert len(client.calls) == 2 and list(client.calls[1][1]) == ['q1']


def test_guard_error_propagates_and_never_becomes_a_quality_verdict():
    summary, facts, sources = sample()
    client = FakeClient([runtime.RuntimeOff('synthetic')])
    with pytest.raises(runtime.RuntimeOff):
        audit.audit_patient_summary_chunk(client, summary, facts[:1], sources, time.monotonic() + 10, 0.8, on_valid_answers=lambda _: None)
    assert len(client.calls) == 1


def test_low_confidence_is_a_finding_not_a_pass():
    summary, facts, sources = sample()
    client = FakeClient([{'q0': answer('supports', 0.2)}])
    result = audit.audit_patient_summary_chunk(client, summary, facts[:1], sources, time.monotonic() + 10, 0.8, on_valid_answers=lambda _: None)
    assert result['evaluated'] is True
    assert result['findings'] == [{'code': 'patient_summary_low_confidence', 'fact_id': facts[0]['id']}]


def test_expired_deadline_never_reaches_jev():
    summary, facts, sources = sample()
    client = FakeClient([])
    result = audit.audit_patient_summary_chunk(client, summary, facts, sources, time.monotonic() - 1, 0.8, on_valid_answers=lambda _: None)
    assert not result['evaluated'] and not client.calls
    assert result['findings'] == [{'code': 'patient_summary_audit_deadline'}]
    assert result['failure_class'] == 'resource'


@pytest.mark.parametrize('kind,retryable,expected', [
    ('budget_exceeded', False, 'resource'),
    ('no_api_key', False, 'resource'),
    ('payment_required', False, 'resource'),
    ('timeout', True, 'retry'),
    ('transport', True, 'retry'),
    ('unauthorized', False, 'failed'),
])
def test_incomplete_audit_retains_answers_and_safe_scheduling_class(kind, retryable, expected):
    summary, facts, sources = sample()
    partial = jev.JevError('protocol_error', 'q1:type_invalid')
    partial.answers, partial.failed = {'q0': answer('not_supported')}, ['q1']
    client = FakeClient([partial, jev.JevError(kind, 'synthetic-private-detail', retryable=retryable)])
    saved = {}
    result = audit.audit_patient_summary_chunk(client, summary, facts, sources,
        time.monotonic() + 10, 0.8, on_valid_answers=lambda values: saved.update(values))
    assert result['evaluated'] is False and result['failure_class'] == expected
    assert result['answers'] == saved == {facts[0]['id']: answer('not_supported')}
    assert {'code': 'patient_summary_not_supported', 'fact_id': facts[0]['id']} in result['findings']
    assert len(client.calls) == 2 and 'synthetic-private-detail' not in repr(result)


@pytest.mark.parametrize('failure', ['false', 'exception', 'jev_exception'])
def test_partial_valid_failure_must_persist_before_second_http(failure):
    summary, facts, sources = sample()
    defect = jev.JevError('protocol_error', 'q1:type_invalid')
    defect.answers, defect.failed = {'q0': answer('contradicts')}, ['q1']
    client = FakeClient([defect, {'q1': answer('omission_ok')}])
    saved = []
    def refuse(answers):
        saved.append(answers)
        if failure == 'false':
            return False
        if failure == 'jev_exception':
            raise jev.JevError('transport')
        raise RuntimeError('synthetic persistence failure')
    exception = jev.JevError if failure == 'jev_exception' else RuntimeError
    with pytest.raises(exception):
        audit.audit_patient_summary_chunk(client, summary, facts, sources, time.monotonic() + 10, 0.8,
                                          on_valid_answers=refuse)
    assert saved == [{facts[0]['id']: answer('contradicts')}]
    assert len(client.calls) == 1


def test_saved_failure_survives_second_timeout_and_is_not_asked_on_resume():
    summary, facts, sources = sample()
    defect = jev.JevError('protocol_error', 'q1:type_invalid')
    defect.answers, defect.failed = {'q0': answer('not_supported')}, ['q1']
    saved = {}
    first = FakeClient([defect, jev.JevError('timeout')])
    out = audit.audit_patient_summary_chunk(first, summary, facts, sources, time.monotonic() + 10, 0.8,
                                           on_valid_answers=lambda values: saved.update(values))
    assert not out['evaluated'] and saved == {facts[0]['id']: answer('not_supported')}
    assert out['answers'] == saved
    later = FakeClient([{'q1': answer('omission_ok')}])
    resumed = audit.audit_patient_summary_chunk(later, summary, facts, sources, time.monotonic() + 10, 0.8,
              prior_answers=saved, on_valid_answers=lambda values: saved.update(values))
    assert resumed['evaluated'] and resumed['answers'][facts[0]['id']]['choice'] == 'not_supported'
    assert resumed['findings'] == [{'code': 'patient_summary_not_supported', 'fact_id': facts[0]['id']}]
    assert list(later.calls[0][1]) == ['q1']
    assert later.calls[0][0] == first.calls[0][0]  # same complete chunk state; only pending questions differ


@pytest.mark.parametrize('bad', [{'d' * 64: answer('supports')}, {'a' * 64: answer('unknown')},
                                 {'a' * 64: answer('supports', float('nan'))}])
def test_bad_prior_receipt_is_rejected_before_http(bad):
    summary, facts, sources = sample()
    client = FakeClient([])
    out = audit.audit_patient_summary_chunk(client, summary, facts, sources, time.monotonic() + 10, 0.8,
                                            prior_answers=bad, on_valid_answers=lambda _: None)
    assert not out['evaluated'] and out['findings'] == [{'code': 'patient_summary_chunk_invalid'}]
    assert not client.calls


def test_missing_persistence_owner_never_reaches_jev():
    summary, facts, sources = sample()
    client = FakeClient([])
    out = audit.audit_patient_summary_chunk(client, summary, facts, sources, time.monotonic() + 10, 0.8)
    assert not out['evaluated'] and not client.calls


def test_jev_legacy_call_without_callback_keeps_partial_defect_retry():
    defect = jev.JevError('protocol_error', 'q1:type_invalid')
    defect.answers, defect.failed = {'q0': answer('supports')}, ['q1']
    client = FakeClient([defect, {'q1': answer('omission_ok')}])
    result = jev.evaluate_once_more(client, {}, {'q0': {}, 'q1': {}}, time.monotonic() + 10)
    assert result['answers'] == {'q0': answer('supports'), 'q1': answer('omission_ok')}
    assert len(client.calls) == 2 and list(client.calls[1][1]) == ['q1']


def test_jev_callback_saves_second_partial_response_before_raising():
    first = jev.JevError('protocol_error', 'q1:type_invalid')
    first.answers, first.failed = {'q0': answer('not_supported')}, ['q1', 'q2']
    second = jev.JevError('protocol_error', 'q2:type_invalid')
    second.answers, second.failed = {'q1': answer('important_missing')}, ['q2']
    client = FakeClient([first, second])
    saved = []
    with pytest.raises(jev.JevError):
        jev.evaluate_once_more(client, {}, {'q0': {}, 'q1': {}, 'q2': {}}, time.monotonic() + 10,
                               on_valid_answers=lambda answers: saved.append(answers))
    assert saved == [{'q0': answer('not_supported')}, {'q1': answer('important_missing')}]
    assert len(client.calls) == 2


def test_jev_callback_receives_complete_response_before_return():
    client = FakeClient([{'q0': answer('not_supported')}])
    saved = []
    result = jev.evaluate_once_more(client, {}, {'q0': {}}, time.monotonic() + 10,
                                   on_valid_answers=lambda answers: saved.append(answers))
    assert saved == [result['answers']] == [{'q0': answer('not_supported')}]
    assert len(client.calls) == 1


def test_reusing_all_prior_answers_requires_no_jev_request():
    summary, facts, sources = sample()
    prior = {facts[0]['id']: answer('not_supported'), facts[1]['id']: answer('omission_ok')}
    client = FakeClient([])
    result = audit.audit_patient_summary_chunk(client, summary, facts, sources, time.monotonic() + 10, 0.8,
                                               prior_answers=prior, on_valid_answers=lambda _: pytest.fail('no new answers'))
    assert result['evaluated'] and result['answers'] == prior and not client.calls
    assert result['findings'] == [{'code': 'patient_summary_not_supported', 'fact_id': facts[0]['id']}]


def test_callback_mutation_cannot_turn_a_valid_failure_into_a_pass():
    summary, facts, sources = sample()
    client = FakeClient([{'q0': answer('not_supported'), 'q1': answer('important_missing')}])
    callback_values = []
    def mutate(receipt):
        callback_values.append(tuple(receipt))
        receipt[facts[0]['id']]['choice'] = 'supports'
        del receipt[facts[1]['id']]
        receipt['unexpected'] = answer('omission_ok')
    result = audit.audit_patient_summary_chunk(client, summary, facts, sources, time.monotonic() + 10, 0.8,
                                               on_valid_answers=mutate)
    assert callback_values == [(facts[0]['id'], facts[1]['id'])]
    assert result['evaluated'] is True
    assert result['answers'] == {facts[0]['id']: answer('not_supported'), facts[1]['id']: answer('important_missing')}
    assert result['findings'] == [{'code': 'patient_summary_not_supported', 'fact_id': facts[0]['id']},
                                  {'code': 'patient_summary_important_missing', 'fact_id': facts[1]['id']}]
    assert len(client.calls) == 1
