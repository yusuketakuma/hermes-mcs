"""Whole-source context QA pages keep all synthetic facts and durable answer semantics."""
from copy import deepcopy
import time

import pytest

import semantic_audit as audit
import semantic_jev as jev
from mcs_util import payload_hash
from test_patient_summary_audit import FakeClient, answer, sample


def pages():
    _, facts, sources = sample()
    return list(audit.patient_source_quality_pages(sources[0], facts))


def evaluate(client, page, **kwargs):
    return audit.audit_patient_quality_page(client, page, time.monotonic() + 10, 0.8,
        **({'on_attempt_result': lambda *_: None} if not kwargs else kwargs))


def test_all_257_facts_and_the_full_source_remain_in_the_coverage_denominator():
    _, original, sources = sample()
    source = sources[0]
    source['body_text'] += '末尾の完全合成記録。' * 3000
    facts = [{**original[0], 'id': payload_hash(['synthetic_fact', index])} for index in range(257)]
    planned = list(audit.patient_source_quality_pages(source, facts))
    support, coverage = planned[:-1], planned[-1]
    assert len(support) == 22 and sum(len(page['fact_ids']) for page in support) == 257
    assert {fid for page in support for fid in page['fact_ids']} == {fact['id'] for fact in facts}
    assert all(1 <= len(page['questions']) <= 12 for page in planned)
    assert all(page['state']['context'][0]['text'] == source['body_text'] for page in planned)
    assert len(coverage['state']['target']['candidate_facts']) == 257
    assert {f['id'] for f in coverage['state']['target']['candidate_facts']} == {f['id'] for f in facts}
    assert coverage['passing_choices'] == dict.fromkeys(coverage['fact_ids'], 'complete')
    assert all(page['passing_choices'] == dict.fromkeys(page['fact_ids'], 'supports') for page in support)
    for page in planned:
        assert list(page['questions']) == [f'q{i}' for i in range(len(page['fact_ids']))]
        assert [target['fact']['id'] for target in page['state']['target']['facts']] == page['fact_ids']


@pytest.mark.parametrize('choice,findings', [('complete', []), ('missing', ['patient_source_missing']),
                                           ('ambiguous', ['patient_source_ambiguous'])])
def test_zero_facts_still_require_a_whole_source_coverage_answer(choice, findings):
    _, _, sources = sample()
    [page] = list(audit.patient_source_quality_pages(sources[0], []))
    assert page['state']['target']['candidate_facts'] == []
    assert list(page['questions']) == ['q0'] and len(page['fact_ids']) == 1
    client = FakeClient([{'q0': answer(choice)}])
    result = evaluate(client, page)
    assert result['evaluated'] and [item['code'] for item in result['findings']] == findings
    assert len(client.calls) == 1


def test_support_and_coverage_preserve_subject_negation_unknown_time_and_all_attributes():
    _, facts, sources = sample()
    facts[0].update(subject='unspecified', reported_subject='family', polarity='negated',
        temporal={'subject': 'family', 'status': 'historical', 'event_time': 'unknown'},
        assertion={'text': facts[0]['text'], 'negated': True},
        details=[{'key': 'synthetic_detail', 'value': 'unknown'}],
        relations=[{'kind': 'contradicts', 'target_fact_id': facts[1]['id']}],
        found_at=123, found_night_id='synthetic_day', discovery='new_report')
    planned = list(audit.patient_source_quality_pages(sources[0], facts))
    actual = planned[0]['state']['target']['facts'][0]['fact']
    expected = {key: value for key, value in facts[0].items()
                if key not in ('found_at', 'found_night_id', 'discovery')}
    assert actual == planned[-1]['state']['target']['candidate_facts'][0] == expected
    assert actual['quote_span'] == facts[0]['quote_span'] and actual['evidence_span'] == facts[0]['evidence_span']
    assert actual['temporal']['event_time'] == 'unknown'
    assert 'not canonical clinical-fact coverage' in planned[-1]['questions']['q0']['instructions']
    # The caller's nested data cannot change an already-planned interpretation.
    facts[0]['temporal']['status'] = 'current'
    assert actual['temporal']['status'] == 'historical'


def test_coverage_logical_id_ignores_only_discovery_bookkeeping_and_input_order():
    _, facts, sources = sample()
    first = list(audit.patient_source_quality_pages(sources[0], facts))[-1]['fact_ids']
    for fact in facts:
        fact.update(found_at=987, found_night_id='other_day', discovery='backfill_found')
    assert list(audit.patient_source_quality_pages(sources[0], list(reversed(facts))))[-1]['fact_ids'] == first
    facts[0]['temporal'] = {'status': 'past'}
    assert list(audit.patient_source_quality_pages(sources[0], facts))[-1]['fact_ids'] != first


@pytest.mark.parametrize('change', ['post', 'hash', 'posted_at', 'body', 'karte_occurrence'])
def test_coverage_logical_id_binds_the_actual_source_occurrence(change):
    _, facts, sources = sample()
    source = sources[0]
    if change == 'karte_occurrence':
        source.update(source='karte_summary', message_id=0, source_key='karte:10')
        for fact in facts:
            fact.update(source='karte_summary', message_id=0, source_key='karte:10')
    first = list(audit.patient_source_quality_pages(source, facts))[-1]['fact_ids']
    if change == 'post':
        source['message_id'] = 2
        for fact in facts:
            fact.update(message_id=2, source_key=2)
    elif change == 'hash':
        source['content_hash'] = 'd' * 64
        for fact in facts:
            fact['content_hash'] = source['content_hash']
    elif change == 'posted_at':
        source['posted_at'] = '2026-10-02T10:00:00+09:00'
    elif change == 'body':
        source['body_text'] += '後続の合成記録。'
    else:
        source['source_key'] = 'karte:11'
        for fact in facts:
            fact['source_key'] = source['source_key']
    assert list(audit.patient_source_quality_pages(source, facts))[-1]['fact_ids'] != first


@pytest.mark.parametrize('problem', ['patient', 'message', 'source', 'hash', 'evidence', 'quote', 'duplicate',
                                     'source_type', 'source_key', 'body_state', 'bad_unicode', 'last_fact'])
def test_every_fact_is_validated_before_any_page_is_yielded(problem):
    _, facts, sources = sample()
    source = sources[0]
    if problem == 'patient':
        facts[0]['project_id'] = 2
    elif problem == 'message':
        facts[0]['message_id'] = 2
    elif problem == 'source':
        facts[0]['source_key'] = 2
    elif problem == 'hash':
        facts[0]['content_hash'] = 'd' * 64
    elif problem == 'evidence':
        facts[0]['evidence'] = '異なる完全合成記録'
    elif problem == 'quote':
        facts[0]['quote_span'] = [0, len(source['body_text'])]
    elif problem == 'duplicate':
        facts.append(facts[0])
    elif problem == 'source_type':
        source['project_id'] = True
    elif problem == 'source_key':
        source.update(source='karte_summary', message_id=0, source_key='karte:01')
    elif problem == 'body_state':
        source['body_state'] = 'partial'
    elif problem == 'bad_unicode':
        facts[-1]['temporal'] = {'status': '\ud800'}
    else:
        facts = [{**facts[0], 'id': payload_hash(['synthetic_fact', index])} for index in range(13)]
        facts[-1]['project_id'] = 2
    with pytest.raises(ValueError):
        next(audit.patient_source_quality_pages(source, facts))


def test_a_normal_negative_answer_is_reused_without_another_request():
    page = pages()[0]
    prior = {fid: answer('not_supported') for fid in page['fact_ids']}
    client = FakeClient([])
    result = evaluate(client, page, prior_answers=prior,
                      on_attempt_result=lambda *_: pytest.fail('no request or result is new'))
    assert result['evaluated'] and result['answers'] == prior
    assert {item['code'] for item in result['findings']} == {'patient_source_not_supported'}
    assert not client.calls


def test_partial_normal_failure_persists_and_only_the_invalid_question_is_retried():
    page = pages()[0]
    first = jev.JevError('protocol_error', 'q1:type_invalid')
    first.answers, first.failed = {'q0': answer('contradicts')}, ['q1']
    client = FakeClient([first, {'q1': answer('supports')}])
    saved = []
    result = evaluate(client, page, on_attempt_result=lambda values, invalid: saved.append((values, invalid)))
    left, right = page['fact_ids']
    assert saved == [({left: answer('contradicts')}, {right}), ({right: answer('supports')}, set())]
    assert result['evaluated'] and result['answers'][left] == answer('contradicts')
    assert result['findings'] == [{'code': 'patient_source_contradicts', 'fact_id': left}]
    assert len(client.calls) == 2 and list(client.calls[1][1]) == ['q1']


@pytest.mark.parametrize('failure', ['false', 'exception', 'jev_exception'])
def test_source_callback_failure_stops_before_a_second_http(failure):
    page = pages()[0]
    defect = jev.JevError('protocol_error', 'q1:type_invalid')
    defect.answers, defect.failed = {'q0': answer('not_supported')}, ['q1']
    client = FakeClient([defect, {'q1': answer('supports')}])
    def refuse(*_):
        if failure == 'false':
            return False
        if failure == 'jev_exception':
            raise jev.JevError('transport')
        raise RuntimeError('synthetic persistence failure')
    with pytest.raises(jev.JevError if failure == 'jev_exception' else RuntimeError):
        evaluate(client, page, on_attempt_result=refuse)
    assert len(client.calls) == 1


def test_second_malformed_result_is_recorded_without_a_third_attempt():
    page = pages()[0]
    first = jev.JevError('protocol_error', 'q0:type_invalid')
    second = jev.JevError('protocol_error', 'q1:type_invalid')
    first.answers, first.failed = {}, ['q0', 'q1']
    second.answers, second.failed = {'q0': answer('not_supported')}, ['q1']
    client = FakeClient([first, second])
    saved = []
    result = evaluate(client, page, on_attempt_result=lambda values, invalid: saved.append((values, invalid)))
    left, right = page['fact_ids']
    assert saved == [({}, {left, right}), ({left: answer('not_supported')}, {right})]
    assert not result['evaluated'] and result['answers'] == {left: answer('not_supported')}
    assert result['findings'][-1] == {'code': 'patient_source_audit_unevaluated'}
    assert len(client.calls) == 2


def test_transport_unknown_produces_no_answers_and_no_fabricated_success():
    client = FakeClient([jev.JevError('transport', 'synthetic private detail')])
    saved = []
    result = evaluate(client, pages()[0], on_attempt_result=lambda *values: saved.append(values))
    assert not result['evaluated'] and result['answers'] == {} and not saved
    assert result['findings'] == [{'code': 'patient_source_audit_unevaluated'}]
    assert 'synthetic private detail' not in repr(result) and len(client.calls) == 1


@pytest.mark.parametrize('problem', ['foreign_prior', 'wrong_target', 'wrong_passing', 'no_owner'])
def test_invalid_page_or_receipt_never_reaches_the_client(problem):
    page = deepcopy(pages()[0])
    client = FakeClient([])
    kwargs = {'on_attempt_result': lambda *_: None}
    if problem == 'foreign_prior':
        kwargs['prior_answers'] = {'d' * 64: answer('supports')}
    elif problem == 'wrong_target':
        page['state']['target']['facts'][0]['fact']['id'] = 'd' * 64
    elif problem == 'wrong_passing':
        page['passing_choices'][page['fact_ids'][0]] = 'contradicts'
    else:
        kwargs = {'on_attempt_result': None}
    result = evaluate(client, page, **kwargs)
    assert not result['evaluated'] and not client.calls
    assert result['findings'] == [{'code': 'patient_source_page_invalid'}]


def test_shared_executor_keeps_summary_attempt_callbacks_and_codes():
    summary, facts, sources = sample()
    client = FakeClient([{'q0': answer('supports'), 'q1': answer('important_missing')}])
    saved = []
    result = audit.audit_patient_summary_chunk(client, summary, facts, sources, time.monotonic() + 10, 0.8,
        on_attempt_result=lambda values, invalid: saved.append((values, invalid)))
    assert saved == [({facts[0]['id']: answer('supports'), facts[1]['id']: answer('important_missing')}, set())]
    assert result['evaluated'] and result['findings'] == [
        {'code': 'patient_summary_important_missing', 'fact_id': facts[1]['id']}]
