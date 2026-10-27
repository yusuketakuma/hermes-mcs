"""Terse source-only schedule mentions never become attributed appointments."""
import hashlib

import pytest

import intervention_schedule as schedule
from test_intervention_schedule import POSTED, put_source
from test_intervention_schedule import source_db as source_db


@pytest.mark.parametrize(('body', 'written', 'action'), [
    ('明日に介護ベッドを設置します', '明日', '介護ベッドを設置'),
    ('11/2に訪問調査です', '11/2', '訪問調査'),
    ('来週金曜日に担当者会議', '来週金曜日', '担当者会議'),
    ('来週金曜に担当者会議。', '来週金曜', '担当者会議'),
    ('11/2に訪問診療する予定です。', '11/2', '訪問診療'),
    ('11月2日に服薬指導を予定しています。', '11月2日', '服薬指導'),
])
@pytest.mark.parametrize('posted', [None, POSTED, POSTED + 86400 * 30])
def test_written_mention_keeps_unresolved_date_and_unknown_owner(source_db, body, written, action, posted):
    put_source(source_db, 1, 1, body)
    source_db.execute('UPDATE messages SET posted_at_ts=? WHERE message_id=1', (posted,))
    source_db.commit()
    result = schedule.read_source_schedule(source_db, 1)
    record, = result['records']
    assert record['state'] == 'schedule_mention_unconfirmed'
    assert record['date_text'] == written and record['event_day'] is None
    assert record['role'] is None and record['role_text'] is None
    assert record['action'] == action and record['quote'] == body
    assert record['span'] == [0, len(body)] and record['assertion_only'] is True
    assert record['content_hash'] == hashlib.sha256(body.encode()).hexdigest()
    assert result['coverage']['unknown'] == 1


@pytest.mark.parametrize('body', [
    '明日に介護ベッドを設置しません。', '明日に介護ベッド設置は中止しました。',
    '明日に介護ベッド設置ですか？', '条件が整えば明日に介護ベッド設置。',
    '家族は明日に介護ベッド設置。', '別の患者は11/2訪問調査。',
    '来週金曜に担当者会議かもしれません。', '2/30訪問調査',
    '「明日に介護ベッド設置」と伝聞しました。',
    '前の記録は誤り。来週金曜に担当者会議。',
    '明日に介護ベッド設置。予定は取り消しました。',
    '共有します。11/2に訪問調査です。これは訂正です。',
    '家族の話です。明日に介護ベッドを設置します。共有します。',
])
def test_unresolved_or_misleading_context_is_not_exposed_as_mention(source_db, body):
    put_source(source_db, 1, 1, body)
    records = schedule.read_source_schedule(source_db, 1)['records']
    assert all(record['state'] == 'unknown' and record['quote'] is None for record in records)


def test_mentions_stay_patient_scoped_revalidate_and_do_not_skip_on_resume(source_db):
    put_source(source_db, 1, 1, '明日に介護ベッド設置')
    put_source(source_db, 2, 1, '11/2訪問調査')
    put_source(source_db, 3, 2, '来週金曜に担当者会議')
    source_db.execute('BEGIN')
    first = schedule.read_source_schedule_page(source_db, 1, max_records=1)
    second = schedule.read_source_schedule_page(source_db, 1, cursor=first['next_cursor'], max_records=1)
    assert [first['records'][0]['date_text'], second['records'][0]['date_text']] == ['11/2', '明日']
    assert all(record['project_id'] == 1 for page in (first, second) for record in page['records'])
    source_db.rollback()
    put_source(source_db, 1, 1, '介護ベッド設置は取り消しました。')
    assert all(record['quote'] != '明日に介護ベッド設置'
               for record in schedule.read_source_schedule(source_db, 1)['records'])


def test_compact_mention_keeps_only_bound_span_not_raw_text():
    body = '明日に介護ベッド設置'
    result = schedule.compact_source_record(1, 1, hashlib.sha256(body.encode()).hexdigest(), body, POSTED)
    assert result['coverage']['unknown'] == 1
    assert result['records'] == [{'span': [0, len(body)], 'role': None, 'action': '介護ベッド設置',
                                  'event_day': None, 'state': 'schedule_mention_unconfirmed'}]
    assert 'quote' not in str(result) and 'date_text' not in str(result)


def test_neutral_surrounding_sentences_keep_exact_source_span(source_db):
    body = '共有します。明日に介護ベッドを設置します。ありがとうございます。'
    quote = '明日に介護ベッドを設置します。'
    put_source(source_db, 1, 1, body)
    record, = schedule.read_source_schedule(source_db, 1)['records']
    assert record['state'] == 'schedule_mention_unconfirmed'
    assert record['quote'] == quote and record['date_text'] == '明日'
    assert record['span'] == [body.index(quote), body.index(quote) + len(quote)]
    assert record['role'] is None and record['event_day'] is None


def test_three_mentions_in_one_post_keep_each_span_and_page_position(source_db):
    quotes = ['明日に介護ベッドを設置します。', '11/2に訪問調査です。', '来週金曜日に担当者会議。']
    body = ''.join(quotes)
    put_source(source_db, 1, 1, body)
    rows = schedule.read_source_schedule(source_db, 1)['records']
    assert [row['quote'] for row in rows] == quotes
    assert [row['date_text'] for row in rows] == ['明日', '11/2', '来週金曜日']
    assert all(row['state'] == 'schedule_mention_unconfirmed' and row['role'] is None
               and row['event_day'] is None for row in rows)
    assert all(body[slice(*row['span'])] == row['quote'] for row in rows)
    source_db.execute('BEGIN')
    cursor = None
    pages = []
    for _ in range(3):
        page = schedule.read_source_schedule_page(source_db, 1, cursor=cursor, max_records=1)
        pages.extend(page['records'])
        cursor = page['next_cursor']
    assert [row['quote'] for row in pages] == quotes and cursor is None
    source_db.rollback()


@pytest.mark.parametrize('other', [
    '11/2に訪問調査は中止しました。', '予定を取り消します。', '前の記載は訂正します。',
    '状況が変われば実施します。', '家族の話です。', '2/30に訪問調査です。',
])
def test_mixed_ambiguous_context_never_promotes_other_mentions(source_db, other):
    body = '明日に介護ベッドを設置します。' + other + '来週金曜日に担当者会議。'
    put_source(source_db, 1, 1, body)
    rows = schedule.read_source_schedule(source_db, 1)['records']
    assert len(rows) >= 2
    assert all(row['state'] == 'unknown' and row['quote'] is None for row in rows)


def test_undated_cues_do_not_use_the_candidate_budget():
    body = '明日に介護ベッドを設置します。' + '訪問調査票を送付。' * 201
    result = schedule.compact_source_record(1, 1, hashlib.sha256(body.encode()).hexdigest(), body, POSTED)
    assert result['coverage']['candidates'] == 1
    assert result['coverage']['unknown'] == 1 and not result['coverage']['records_truncated']
    assert len(result['records']) == 1 and result['records'][0]['state'] == 'unknown'


@pytest.mark.parametrize('noise', ['訪問調査票を送付。', '11/2に訪問看護師から連絡。',
                                   '11/2の資料を確認しました。', '11/2に訪問調査票を送付。',
                                   '11/2に確認した資料を送付。'])
def test_unrelated_dated_sentences_do_not_consume_record_capacity(noise):
    body = '明日に介護ベッドを設置します。' + noise * 201
    result = schedule.compact_source_record(1, 1, hashlib.sha256(body.encode()).hexdigest(), body, POSTED)
    assert result['coverage']['candidates'] == 1
    assert result['coverage']['unknown'] == 1 and not result['coverage']['records_truncated']
    assert len(result['records']) == 1


@pytest.mark.parametrize('noise', ['11/2に訪問看護師から連絡。', '11/2に訪問調査票を送付。',
                                   '11/2に確認した資料を送付。'])
def test_irrelevant_date_and_role_messages_do_not_prevent_next_page_progress(source_db, noise):
    put_source(source_db, 1, 1, '11/2に訪問診療する予定です。')
    for mid in range(2, 213):
        put_source(source_db, mid, 1, noise)
    source_db.execute('BEGIN')
    first = schedule.read_source_schedule_page(source_db, 1, limit=200, max_records=1)
    assert first['records'] == [] and first['next_cursor'] is not None
    second = schedule.read_source_schedule_page(source_db, 1, cursor=first['next_cursor'],
                                                limit=200, max_records=1)
    record, = second['records']
    assert record['quote'] == '11/2に訪問診療する予定です。'
    assert record['state'] == 'schedule_mention_unconfirmed' and record['event_day'] is None
    source_db.rollback()


@pytest.mark.parametrize('body', ['11/2に訪問診療は中止しました。',
                                   '11/2に訪問調査を取り消しました。',
                                   '11/2に訪問調査を取消しました。',
                                   '11/2に訪問調査を取り消す。',
                                   '11/2に訪問調査しません。',
                                   '11/2に訪問調査する予定を中止しました。'])
def test_adjacent_dated_cancellation_remains_an_unknown_candidate(body):
    result = schedule.compact_source_record(1, 1, hashlib.sha256(body.encode()).hexdigest(), body, POSTED)
    assert result['coverage']['candidates'] == 1 and result['coverage']['unknown'] == 1
    assert result['records'][0]['state'] == 'unknown'


def test_cursor_rejects_same_hash_new_revision_and_revoked_patient(source_db):
    put_source(source_db, 1, 1, '明日に介護ベッド設置。11/2訪問調査。')
    source_db.execute('BEGIN')
    first = schedule.read_source_schedule_page(source_db, 1, max_records=1)
    assert first['next_cursor'] and first['records'][0]['state'] == 'schedule_mention_unconfirmed'
    assert first['records'][0]['date_text'] == '明日' and first['records'][0]['event_day'] is None
    source_db.execute('INSERT INTO message_revisions VALUES(1, 2)')
    with pytest.raises(ValueError, match='intervention_cursor_source_changed'):
        schedule.read_source_schedule_page(source_db, 1, cursor=first['next_cursor'], max_records=1)
    source_db.rollback()
    source_db.execute("UPDATE patients SET fetch_reason='access_revoked' WHERE project_id=1")
    result = schedule.read_source_schedule(source_db, 1)
    assert result['coverage']['state'] == 'scope_unavailable' and not result['records']


def test_many_mentions_scan_context_once_per_call_and_continue_pages(source_db, monkeypatch):
    body = '11/2に訪問調査です。' * 400
    put_source(source_db, 1, 1, body)
    real = schedule._SENTENCE
    calls = []

    class CountedSentences:
        def finditer(self, text):
            calls.append(len(text))
            return real.finditer(text)

    monkeypatch.setattr(schedule, '_SENTENCE', CountedSentences())
    source_db.execute('BEGIN')
    cursor = None
    records = []
    for page_number in range(5):
        page = schedule.read_source_schedule_page(source_db, 1, cursor=cursor, max_records=80)
        records.extend(page['records'])
        cursor = page['next_cursor']
        assert len(calls) == 2 * (page_number + 1)
    assert cursor is None and len(records) == 400
    assert all(row['state'] == 'schedule_mention_unconfirmed' and row['event_day'] is None for row in records)
    assert len({tuple(row['span']) for row in records}) == 400
    source_db.rollback()


def test_partial_quote_cannot_hide_a_sentence_suffix():
    body = '11/2に訪問調査の予定です。'
    quote = '11/2に訪問調査'
    item = {'project_id': 1, 'message_id': 1, 'content_hash': 'a' * 64, 'category': 'followup',
            'subject': 'unspecified', 'source': 'chat', 'body_state': 'full', 'posted_at_ts': POSTED,
            'source_body': body, 'quote': quote, 'evidence_span': [0, len(quote)], 'unverified': True}
    record, = schedule.project(1, [item])['records']
    assert record['state'] == 'unknown' and record['quote'] is None
