"""Whole-source context QA pages keep all synthetic facts and durable answer semantics."""
from copy import deepcopy
import time

import pytest

import semantic_audit as audit
import semantic_jev as jev
from mcs_util import payload_hash
from test_patient_summary_audit import FakeClient, answer, sample


SOURCE_INPUT_KEY = 'd' * 64
ASSESSMENT = {'code': 'e' * 64, 'policy': 'f' * 64}


def plan(source, facts, *, source_input_key=SOURCE_INPUT_KEY, assessment=None):
    return audit.patient_source_quality_pages(source, facts, source_input_key=source_input_key,
        assessment=ASSESSMENT if assessment is None else assessment)


def pages():
    _, facts, sources = sample()
    return list(plan(sources[0], facts))


def evaluate(client, page, **kwargs):
    return audit.audit_patient_quality_page(client, page, time.monotonic() + 10, 0.8,
        **({'on_attempt_result': lambda *_: None} if not kwargs else kwargs))


def test_all_257_facts_and_the_full_source_remain_in_the_coverage_denominator():
    _, original, sources = sample()
    source = sources[0]
    source['body_text'] += '末尾の完全合成記録。' * 3000
    facts = [{**original[0], 'id': payload_hash(['synthetic_fact', index])} for index in range(257)]
    planned = list(plan(source, facts))
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
    [page] = list(plan(sources[0], []))
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
    planned = list(plan(sources[0], facts))
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
    first = list(plan(sources[0], facts))[-1]['fact_ids']
    for fact in facts:
        fact.update(found_at=987, found_night_id='other_day', discovery='backfill_found')
    assert list(plan(sources[0], list(reversed(facts))))[-1]['fact_ids'] == first
    facts[0]['temporal'] = {'status': 'past'}
    assert list(plan(sources[0], facts))[-1]['fact_ids'] != first


@pytest.mark.parametrize('change', ['post', 'hash', 'posted_at', 'body', 'karte_occurrence'])
def test_coverage_logical_id_binds_the_actual_source_occurrence(change):
    _, facts, sources = sample()
    source = sources[0]
    if change == 'karte_occurrence':
        source.update(source='karte_summary', message_id=0, source_key='karte:10')
        for fact in facts:
            fact.update(source='karte_summary', message_id=0, source_key='karte:10')
    first = list(plan(source, facts))[-1]['fact_ids']
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
    assert list(plan(source, facts))[-1]['fact_ids'] != first


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
        next(plan(source, facts))


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


def test_support_question_identity_survives_an_unrelated_fact_and_changed_wire_alias():
    _, facts, sources = sample()
    source = sources[0]
    original = list(plan(source, [facts[1]]))
    enlarged = list(plan(source, facts))
    fid = facts[1]['id']
    assert original[0]['state']['target']['facts'][0]['id'] == 'q0'
    assert enlarged[0]['state']['target']['facts'][1]['id'] == 'q1'
    assert original[0]['questions']['q0'] == enlarged[0]['questions']['q1']
    assert original[0]['question_keys'][fid] == enlarged[0]['question_keys'][fid]
    assert original[-1]['fact_ids'] != enlarged[-1]['fact_ids']
    assert set(original[-1]['question_keys'].values()) != set(enlarged[-1]['question_keys'].values())


def test_support_question_identity_survives_page_sharding_order_and_bookkeeping():
    _, facts, sources = sample()
    source = sources[0]
    fact = {**facts[0], 'id': 'f' * 64}
    original = list(plan(source, [fact]))[0]['question_keys'][fact['id']]
    others = [{**facts[0], 'id': payload_hash(['synthetic_other', index])} for index in range(24)]
    fact.update(found_at=123, found_night_id='another_day', discovery='backfill_found')
    source.update(extraction_sig='new_generated_output', canonical_context=True)
    new_pages = list(plan(source, [fact, *reversed(others)]))
    [changed] = [page['question_keys'][fact['id']] for page in new_pages[:-1] if fact['id'] in page['question_keys']]
    assert changed == original


@pytest.mark.parametrize('change', ['source_revision', 'source_time', 'fact_subject', 'fact_polarity',
                                    'fact_time', 'fact_details', 'question', 'options', 'policy', 'model'])
def test_a_real_source_fact_or_exact_question_contract_change_is_a_new_question(change):
    _, facts, sources = sample()
    source = sources[0]
    [support, _] = list(plan(source, facts[:1]))
    fid = facts[0]['id']
    original = support['question_keys'][fid]
    body = {'model': jev.JEV_MODEL, 'state': deepcopy(support['state']), 'questions': deepcopy(support['questions'])}
    assessment = dict(ASSESSMENT)
    source_key = SOURCE_INPUT_KEY
    if change == 'source_revision':
        source_key = '9' * 64  # The raw occurrence/revision is assembled by the patient owner.
    elif change == 'source_time':
        body['state']['context'][0]['posted_at'] = '2026-10-02T10:00:00+09:00'
    elif change == 'fact_subject':
        body['state']['target']['facts'][0]['fact']['reported_subject'] = 'family'
    elif change == 'fact_polarity':
        body['state']['target']['facts'][0]['fact']['polarity'] = 'negated'
    elif change == 'fact_time':
        body['state']['target']['facts'][0]['fact']['temporal'] = {'event_time': 'unknown', 'status': 'past'}
    elif change == 'fact_details':
        body['state']['target']['facts'][0]['fact']['details'] = [{'key': 'synthetic', 'value': 'unknown'}]
    elif change == 'question':
        body['questions']['q0']['instructions'] += ' Preserve the reported interval.'
    elif change == 'options':
        body['questions']['q0']['criteria']['supports'] = 'a changed support meaning'
    elif change == 'policy':
        assessment['policy'] = '8' * 64
    else:
        body['model'] = 'unapproved_synthetic_model'
        with pytest.raises(ValueError, match='patient_source_question_invalid'):
            audit.patient_source_question_keys(body, source_input_key=source_key, assessment=assessment)
        return
    changed = audit.patient_source_question_keys(body, source_input_key=source_key, assessment=assessment)
    assert changed[fid] != original


def test_runtime_question_reconstruction_uses_only_the_actually_requested_alias_subset():
    page = pages()[0]
    fid = page['fact_ids'][1]
    body = {'model': jev.JEV_MODEL, 'state': page['state'], 'questions': {'q1': page['questions']['q1']}}
    assert audit.patient_source_question_keys(body, source_input_key=SOURCE_INPUT_KEY,
        assessment=ASSESSMENT) == {fid: page['question_keys'][fid]}


@pytest.mark.parametrize('change', ['foreign_source', 'foreign_fact', 'source_body', 'fact_span',
                                    'duplicate_fact', 'unknown_alias', 'budget_in_assessment'])
def test_actual_request_question_binding_rejects_malformed_or_foreign_material(change):
    page = deepcopy(pages()[0])
    body = {'model': jev.JEV_MODEL, 'state': page['state'], 'questions': page['questions']}
    assessment = dict(ASSESSMENT)
    if change == 'foreign_source':
        body['state']['context'][0]['project_id'] = 2
    elif change == 'foreign_fact':
        body['state']['target']['facts'][0]['fact']['source_key'] = 2
    elif change == 'source_body':
        body['state']['context'][0]['text'] += ' altered synthetic source'
    elif change == 'fact_span':
        body['state']['target']['facts'][0]['fact']['evidence_span'] = [0, 1]
    elif change == 'duplicate_fact':
        body['state']['target']['facts'][1]['fact'] = body['state']['target']['facts'][0]['fact']
    elif change == 'unknown_alias':
        body['questions']['q12'] = body['questions']['q0']
    else:
        assessment['daily_request_budget'] = 3000
    with pytest.raises(ValueError, match='patient_source_question_invalid'):
        audit.patient_source_question_keys(body, source_input_key=SOURCE_INPUT_KEY, assessment=assessment)
