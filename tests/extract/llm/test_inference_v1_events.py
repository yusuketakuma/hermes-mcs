"""Source-first synthetic event axes, compatibility projection and chunk binding."""
import copy
import json

import pytest

import clinical_events
import extract_schema
import extract_validation as validation


def _record(body, kind='admission', **changes):
    return {'kind': kind, 'subject': 'patient', 'status': 'current',
            'negated': False, 'evidence': body, **changes}


def _validate(body, *, events=None, records=None):
    doc = {}
    if events is not None:
        doc['events'] = events
    if records is not None:
        doc['event_records'] = records
    result = validation.validate(doc, body)
    assert result is not None
    return result


@pytest.mark.parametrize('body,subject,status,negated,legacy', [
    ('本人は現在、入院しました。', 'patient', 'current', False, ['admission']),
    ('母は昨日、入院しました。', 'family', 'past', False, []),
    ('他者は現在、入院しました。', 'other', 'current', False, []),
    ('本人は過去に入院しました。', 'patient', 'past', False, []),
    ('本人は明日入院予定です。', 'patient', 'planned', False, []),
    ('本人が入院した場合は連絡してください。', 'patient', 'conditional', False, []),
    ('本人は入院はありません。', 'patient', 'current', True, []),
    ('本人は入院の予定はありません。', 'patient', 'planned', True, []),
    ('本人は入院ではありません。', 'patient', 'current', True, []),
])
def test_legacy_enum_is_bound_to_source_person_time_and_polarity(body, subject, status, negated, legacy):
    output = _validate(body, events=['admission'])
    assert output['events'] == legacy
    record, = output['event_records']
    assert (record['subject'], record['status'], record['negated']) == (subject, status, negated)
    assert record['evidence'] in body
    assert body[record['span']['start']:record['span']['end']] == record['evidence']
    assert not record.get('unverified')


@pytest.mark.parametrize('kind,body', [
    ('admission', '本人は救急搬送されました。'),
    ('transfer', '本人は別室へ移動しました。'),
    ('eol', '本人は合成薬のオピオイドを使用しています。'),
    ('eol', '本人は緩和ケアを受けました。'),
])
def test_broad_cue_is_not_evidence_of_a_different_transition(kind, body):
    output = _validate(body, events=[kind])
    assert output['events'] == []
    assert output['event_records']
    assert all(record['unverified'] for record in output['event_records'])


def test_correct_paraphrase_candidate_survives_without_becoming_a_verified_admission():
    body = '本人は病棟で療養を始めました。'
    output = _validate(body, records=[_record(body)])
    record, = output['event_records']
    assert output['events'] == []
    assert record['kind'] == 'admission' and record['evidence'] == body
    assert record['unverified'] is True
    assert validation.validate(copy.deepcopy(output), body) == output


def test_wrong_axes_cannot_become_current_again_on_revalidation():
    body = '母は昨日、入院はありません。'
    output = _validate(body, events=['admission'], records=[_record(body)])
    record, = output['event_records']
    assert output['events'] == []
    assert (record['subject'], record['status'], record['negated']) == ('family', 'past', True)
    assert record['unverified'] is True
    assert validation.validate(copy.deepcopy(output), body) == output


def test_kind_without_source_quote_is_a_visible_unverified_candidate():
    body = '本人の現在の状態について連絡しました。'
    output = _validate(body, records=[_record('参考contextにしかない入院しました。')])
    record, = output['event_records']
    assert output['events'] == [] and record['unverified'] is True
    assert record['span'] is None and record['evidence'] == ''
    assert validation.validate(copy.deepcopy(output), body) == output


def test_unknown_subject_and_uncertain_polarity_are_not_patient_occurrence():
    for body in ('入院しました。', '本人の入院は未確認です。', '本人は入院しましたか？',
                 '本人は入院したとは書かれていません。'):
        output = _validate(body, events=['admission'])
        assert output['events'] == []
        record, = output['event_records']
        assert record['unverified'] is True
        assert record['subject'] == 'unknown' or record['negated'] is None


def test_same_kind_different_source_subject_time_and_negation_survive_chunk_merge():
    bodies = ['本人は現在、入院しました。', '母は昨日、入院はありません。', '本人は明日入院予定です。']
    documents = [_validate(body, events=['admission']) for body in bodies]
    merged = validation.merge([*documents, documents[0]])
    assert merged['events'] == ['admission']
    assert len(merged['event_records']) == 3
    assert {(r['subject'], r['status'], r['negated']) for r in merged['event_records']} == {
        ('patient', 'current', False), ('family', 'past', True), ('patient', 'planned', False)}
    assert json.loads(json.dumps(merged, ensure_ascii=False)) == merged


def test_identical_quote_requires_an_exact_occurrence_and_keeps_both_records():
    quote = '退院しました。'
    body = '本人は現在、' + quote + '母は昨日、' + quote
    positions = [body.index(quote), body.rindex(quote)]
    items = [_record(quote, kind='discharge', subject=subject, status=status,
                     span={'start': start, 'end': start + len(quote)})
             for start, subject, status in [(positions[0], 'patient', 'current'),
                                            (positions[1], 'family', 'past')]]
    output = _validate(body, records=items)
    assert output['events'] == ['discharge']
    assert [r['span']['start'] for r in output['event_records']] == positions
    assert [r['subject'] for r in output['event_records']] == ['patient', 'family']
    assert not any(r.get('unverified') for r in output['event_records'])
    ambiguous = _validate(body, records=[_record(quote, kind='discharge')])
    assert ambiguous['events'] == []
    assert ambiguous['event_records'][0]['unverified'] is True


def test_piece_offset_uses_full_body_owner_and_does_not_borrow_a_different_occurrence():
    piece = '退院しました。'
    body = '本人は現在、' + piece + '母は昨日、' + piece
    doc = {'events': ['discharge']}
    offset = body.rindex(piece)
    result = validation.validate_piece(doc, piece, body, source_offset=offset)
    assert result is not None and result['events'] == []
    record, = result['event_records']
    assert record['subject'] == 'family' and record['status'] == 'past'
    assert record['span'] == {'start': offset, 'end': offset + len(piece)}
    assert not record.get('unverified')
    ambiguous = validation.validate_piece(doc, piece, body)
    assert ambiguous['events'] == []
    assert ambiguous['event_records'][0]['unverified'] is True


def test_invalid_piece_offset_never_silently_promotes_a_candidate():
    piece = '本人は現在、退院しました。'
    body = '合成前置き。' + piece
    result = validation.validate_piece({'events': ['discharge']}, piece, body, source_offset=0)
    assert result['events'] == []
    assert result['event_records'][0]['unverified'] is True


@pytest.mark.parametrize('body,subject,status,negated', [
    ('次の記載は誤りです。本人は退院しました。', 'patient', 'unknown', None),
    ('次の記載は事実ではありません。本人は退院しました。', 'patient', 'unknown', None),
    ('次の記載は正しくありません。本人は退院しました。', 'patient', 'unknown', None),
    ('本人は退院しました。その記載を訂正します。', 'patient', 'unknown', None),
    ('本人は退院しました。その件は違います。', 'patient', 'unknown', None),
    ('本人は退院しました。その件はなしで。', 'patient', 'unknown', None),
    ('本人は退院しました。先ほどの件は誤りでした。', 'patient', 'unknown', None),
    ('本人は退院しましたが、その記載は誤りです。', 'patient', 'unknown', None),
    ('家族の話です。本人は退院しました。', 'unknown', 'current', False),
    ('家族の情報です。本人は退院しました。', 'unknown', 'current', False),
])
def test_adjacent_correction_or_ambiguous_referent_is_not_confirmed(body, subject, status, negated):
    quote = '本人は退院しました'
    start = body.index(quote)
    proposed = _record(quote, kind='discharge', span={'start': start, 'end': start + len(quote)})
    result = _validate(body, events=['discharge'], records=[proposed])
    record, = result['event_records']
    assert result['events'] == []
    assert (record['subject'], record['status'], record['negated']) == (subject, status, negated)
    assert record['unverified'] is True and record['evidence'] == quote
    assert body[record['span']['start']:record['span']['end']] == quote
    assert validation.validate(copy.deepcopy(result), body) == result


@pytest.mark.parametrize('body', [
    '家族から連絡がありました。本人は退院しました。',
    '母は入院しました。本人は退院しました。',
    '本人は退院しました。薬剤の記載を訂正します。',
    '本人は薬を服用しています。その記載は誤りです。本人は退院しました。',
    '次の記載は誤りではありません。本人は退院しました。',
    '本人は退院しました。その記載の訂正は不要です。',
    '本人は退院しました。その記載を訂正しません。',
    '本人は退院しました。その件は違いません。',
    '本人は退院しました。その記載は間違っていません。',
])
def test_unrelated_sentences_and_family_report_do_not_hide_patient_event(body):
    result = _validate(body, events=['discharge'])
    assert result['events'] == ['discharge']
    record, = result['event_records']
    assert record['subject'] == 'patient' and record['status'] == 'current'
    assert record['negated'] is False and not record.get('unverified')


def test_rules_and_full_body_chunk_revalidation_hold_correction():
    import extract

    body = '次の記載は誤りです。本人は退院しました。'
    piece = '本人は退院しました。'
    rules = extract.extract_message(body, '2026-10-03T09:00:00+09:00')
    assert 'discharge' not in rules.get('events', [])
    assert any(record['kind'] == 'discharge' and record['unverified']
               for record in rules['event_records'])
    result = validation.validate_piece({'events': ['discharge']}, piece, body,
                                       source_offset=body.index(piece))
    assert result['events'] == []
    record, = result['event_records']
    assert record['unverified'] is True
    assert body[record['span']['start']:record['span']['end']] == record['evidence']


def test_same_sentence_explicit_contradiction_is_not_an_affirmed_event():
    body = '本人は2026年9月1日に退院しましたが、実際には違います。'
    result = _validate(body, records=[_record(body, kind='discharge')])
    assert result['events'] == []
    record, = result['event_records']
    assert record['unverified'] is True and record['status'] == 'unknown'
    assert record['negated'] is None and record['evidence'] == body


def test_empty_records_are_a_valid_empty_result_and_schema_preserves_old_enum_shape():
    output = _validate('合成の定期連絡です。', events=[], records=[])
    assert output['events'] == [] and output['event_records'] == []
    props = extract_schema.SCHEMA['schema']['properties']
    assert props['events']['items']['type'] == 'string'
    assert set(props['events']['items']['enum']) == set(clinical_events.EVENT_KINDS)
    assert set(props['event_records']['items']['properties']['kind']['enum']) == set(clinical_events.EVENT_KINDS)


def test_malformed_record_does_not_hide_valid_sibling():
    body = '本人は現在、退院しました。'
    output = _validate(body, records=[{'kind': 'unknown-enum'}, _record(body, kind='discharge')])
    assert output['events'] == ['discharge']
    assert len(output['event_records']) == 1
    assert output['_items_dropped'] == 1


@pytest.mark.parametrize('subject,workflow,polarity,quote,valid_time,wanted', [
    ('patient:1', 'done', 'affirmed', '本人は本日退院しました。', 'current', 'current'),
    ('role:family', 'done', 'affirmed', '母は昨日退院しました。', 'past', 'past'),
    ('patient:1', 'planned', 'affirmed', '本人は明日退院予定です。', 'planned', 'planned'),
    ('patient:1', 'reported', 'negated', '本人の退院はありません。', 'unknown', 'unknown'),
    ('unknown', 'reported', 'unknown', '退院については未確認です。', 'unknown', 'unknown'),
])
def test_canonical_events_keep_original_axes_and_coordinates(subject, workflow, polarity, quote, valid_time, wanted):
    from semantic_projection import project_v2_doc_legacy
    from semantic_testkit import v2_doc, v2_fact
    evidence = {'evidence_id': 'event-evidence', 'message_id': 'm1', 'revision': 'r1',
                'start': 100, 'end': 100 + len(quote), 'quote': quote, 'atom_id': 'a1'}
    fact = v2_fact('event-fact', kind='care_event', subject=subject, workflow_status=workflow,
                   statement=quote, polarity=polarity, valid_time=valid_time, evidence_ids=['event-evidence'])
    doc = v2_doc([fact], [evidence])
    before = copy.deepcopy(doc)
    output = project_v2_doc_legacy(doc)
    record, = output['event_records']
    assert record['status'] == wanted
    assert record['span'] == {'start': 100, 'end': 100 + len(quote)}
    assert record['evidence'] == quote
    assert output.get('events', []) == (['discharge'] if wanted == 'current' else [])
    assert output['canonical_facts'][0]['fact_id'] == 'event-fact'
    assert output['canonical_facts'][0]['valid_time'] == valid_time
    assert doc == before


def test_canonical_event_without_coordinates_does_not_invent_a_source_span():
    from semantic_projection import project_v2_doc_legacy
    from semantic_testkit import v2_doc, v2_fact
    fact = v2_fact('event', kind='care_event', statement='本人は本日退院しました。',
                   workflow_status='done', valid_time='current', evidence_ids=['missing'])
    output = project_v2_doc_legacy(v2_doc([fact]))
    record, = output['event_records']
    assert record['span'] is None and record['unverified'] is True
    assert output.get('events', []) == []
    assert output['canonical_facts'][0]['fact_id'] == 'event'


@pytest.mark.parametrize('quote,workflow,action,event_time,expected', [
    ('本人は現在アムロジピンを継続しています。', 'reported', 'continue', 'unknown', 'current'),
    ('本人は以前アムロジピンを開始しましたが現在も継続しています。', 'reported', 'start', '2020-01-01', 'current'),
    ('本人は以前アムロジピンを開始しました。', 'reported', 'start', '2020-01-01', 'past'),
    ('アムロジピンという薬剤名の記載です。', 'reported', 'continue', '2020-01-01', None),
    ('本人は明日アムロジピンを中止予定です。', 'planned', 'stop', 'unknown', 'planned'),
])
def test_medication_currentness_requires_source_use_not_reported_workflow(quote, workflow, action, event_time, expected):
    from semantic_projection import project_v2_doc_legacy
    from semantic_testkit import v2_doc, v2_fact
    from mcs_queries import med_is_patient_current
    evidence = {'evidence_id': 'med-evidence', 'message_id': 'm1', 'revision': 'r1',
                'start': 0, 'end': len(quote), 'quote': quote, 'atom_id': 'a1'}
    fact = v2_fact('med', statement='アムロジピン継続', workflow_status=workflow,
                   action=action, event_time=event_time, evidence_ids=['med-evidence'])
    output = project_v2_doc_legacy(v2_doc([fact], [evidence]))
    med, = output['meds']
    assert med['status'] == expected
    if expected is None:
        assert med['unverified'] is True and not med_is_patient_current(med)
    assert output['canonical_facts'][0]['event_time'] == event_time


@pytest.mark.parametrize('kind,quote,valid_time', [
    ('care_event', '本人は本日退院しました。', 'past'),
    ('medication_event', '本人は現在アムロジピンを継続しています。', 'past'),
    ('medication_event', '本人は以前アムロジピンを服用していました。', 'current'),
])
def test_quote_period_conflicting_with_valid_time_remains_unverified(kind, quote, valid_time):
    from semantic_projection import project_v2_doc_legacy
    from semantic_testkit import v2_doc, v2_fact
    from mcs_queries import med_is_patient_current
    evidence = {'evidence_id': 'time-evidence', 'message_id': 'm1', 'revision': 'r1',
                'start': 0, 'end': len(quote), 'quote': quote, 'atom_id': 'a1'}
    fact = v2_fact('conflict', kind=kind, statement=quote, workflow_status='done',
                   action='continue', valid_time=valid_time, evidence_ids=['time-evidence'])
    output = project_v2_doc_legacy(v2_doc([fact], [evidence]))
    if kind == 'care_event':
        assert output['event_records'][0]['unverified'] is True
        assert output.get('events', []) == []
    else:
        med, = output['meds']
        assert med['status'] is None and med['unverified'] is True
        assert not med_is_patient_current(med)
    assert output['canonical_facts'][0]['valid_time'] == valid_time


@pytest.mark.parametrize('body,confirmed', [
    ('本日ご本人がお亡くなりになりました。', True),
    ('本人は亡くなりました。', True),
    ('本人は亡くなった。', True),
    ('母がお亡くなりになりました。', False),
    ('本人は昨日、お亡くなりになりました。', False),
    ('本人がお亡くなりになる可能性があります。', False),
    ('本人がお亡くなりになったら連絡してください。', False),
    ('本人がお亡くなりになっていません。', False),
    ('本人がお亡くなりになりました？', False),
    ('本日、本人が亡くなったという報告はありません。', False),
    ('本日ご本人がお亡くなりになったとの記録はありません。', False),
    ('本日、本人が亡くなった、という報告はありません。', False),
])
def test_direct_eol_wording_keeps_person_time_and_uncertainty(body, confirmed):
    result = _validate(body, events=['eol'])
    assert result['events'] == (['eol'] if confirmed else [])
    assert result['event_records']
    for record in result['event_records']:
        assert body[record['span']['start']:record['span']['end']] == record['evidence']


@pytest.mark.parametrize('mark', ['?', '？'])
def test_a_question_mark_cannot_turn_an_event_into_confirmed_occurrence(mark):
    body = '本人は入院しました' + mark
    result = _validate(body, events=['admission'])
    assert result['events'] == []
    assert result['event_records'][0]['negated'] is None
    assert result['event_records'][0]['unverified']


@pytest.mark.parametrize('comma', ['', '、', ','])
def test_absence_of_an_admission_report_is_not_an_affirmed_or_denied_admission(comma):
    body = '本人は入院した' + comma + 'という報告はありません。'
    result = _validate(body, events=['admission'])
    assert result['events'] == []
    assert result['event_records'][0]['negated'] is None
    assert result['event_records'][0]['unverified']
