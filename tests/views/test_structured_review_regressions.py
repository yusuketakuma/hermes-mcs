import clinical_display
import extract_validation
import pytest
import extract
import structured_view


@pytest.mark.parametrize(('body', 'event'), [
    ('昨日亡くなりました。', 'eol'),
    ('今日は内科の外来に行ってきました。', 'exam'),
    ('看護師が家に来てくださいました。', 'visit'),
])
def test_event_synonyms_survive(body, event):
    # Inference v1 keeps the synonym's kind and source span, but a report
    # without an explicit patient subject stays unverified and never current.
    out = extract_validation.validate({'events': [event]}, body)
    assert out['events'] == []
    [record] = out['event_records']
    assert record['kind'] == event and record['unverified'] is True
    assert record['evidence'] == body
    assert body[record['span']['start']:record['span']['end']] == record['evidence']


@pytest.mark.parametrize(('body', 'events', 'subject', 'unverified'), [
    ('本人は本日亡くなりました。', ['eol'], 'patient', False),
    ('母が本日亡くなりました。', [], 'family', False),
    ('本人は本日亡くなっていません。', [], 'patient', True),
    ('本人は本日亡くなりました。上記の件は誤りです。', [], 'patient', True),
])
def test_explicit_patient_event_is_current_only_without_family_negation_or_correction(
        body, events, subject, unverified):
    out = extract_validation.validate({'events': ['eol']}, body)
    assert out['events'] == events
    [record] = out['event_records']
    assert record['kind'] == 'eol' and record['subject'] == subject
    assert bool(record.get('unverified')) is unverified
    assert body[record['span']['start']:record['span']['end']] == record['evidence']


@pytest.mark.parametrize(('body', 'event', 'forbidden'), [
    ('本日訪問しました。入院は不要です。', 'visit', '入院'),
    ('本日退院して自宅に戻りました。', 'discharge', '入院'),
])
def test_rules_cannot_restore_excluded_event(body, event, forbidden):
    llm = extract_validation.validate({'events': [event]}, body)
    rules = extract.extract_message(body, '2026-09-28')
    assert forbidden not in '\n'.join(structured_view._head_lines(llm, rules))


@pytest.mark.parametrize(('body', 'vitals', 'forbidden'), [
    ('本人の脈拍は72です。同居の娘さんの血圧は180/110でした。', {'hr': 72}, '180'),
    ('午前の血圧120/80。現在の収縮期血圧は90、拡張期は測定不可。', {'sbp': 90}, '/80'),
])
def test_readings_never_mix_subject_or_time(body, vitals, forbidden):
    llm = extract_validation.validate({'vitals': vitals}, body)
    rules = extract.extract_message(body, '2026-09-28')
    assert forbidden not in clinical_display.vital_line(llm, rules)


@pytest.mark.parametrize(('vitals', 'expected'), [
    ({'sbp': 120}, 'BP 120/?'),
    ({'dbp': 70}, 'BP ?/70'),
    ({'sbp': 120, 'dbp': 70}, 'BP 120/70'),
])
def test_half_extracted_bp_never_renders_none(vitals, expected):
    line = clinical_display.vital_line({'vitals': vitals}, {})
    assert expected in line and 'None' not in line
