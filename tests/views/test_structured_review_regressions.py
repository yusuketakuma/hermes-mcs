import pytest
import extract
import extract_llm
import structured_view


@pytest.mark.parametrize(('body', 'event'), [
    ('昨日亡くなりました。', 'eol'),
    ('今日は内科の外来に行ってきました。', 'exam'),
    ('看護師が家に来てくださいました。', 'visit'),
])
def test_event_synonyms_survive(body, event):
    assert extract_llm._validate({'events': [event]}, body)['events'] == [event]


@pytest.mark.parametrize(('body', 'event', 'forbidden'), [
    ('本日訪問しました。入院は不要です。', 'visit', '入院'),
    ('本日退院して自宅に戻りました。', 'discharge', '入院'),
])
def test_rules_cannot_restore_excluded_event(body, event, forbidden):
    llm = extract_llm._validate({'events': [event]}, body)
    rules = extract.extract_message(body, '2026-09-28')
    assert forbidden not in '\n'.join(structured_view._head_lines(llm, rules))


@pytest.mark.parametrize(('body', 'vitals', 'forbidden'), [
    ('本人の脈拍は72です。同居の娘さんの血圧は180/110でした。', {'hr': 72}, '180'),
    ('午前の血圧120/80。現在の収縮期血圧は90、拡張期は測定不可。', {'sbp': 90}, '/80'),
])
def test_readings_never_mix_subject_or_time(body, vitals, forbidden):
    llm = extract_llm._validate({'vitals': vitals}, body)
    rules = extract.extract_message(body, '2026-09-28')
    assert forbidden not in structured_view._vital_line(llm, rules)
