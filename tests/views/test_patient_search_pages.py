"""Patient search uses bounded originals and private source references (fully synthetic)."""
import json

import pytest

import attachment_ocr
import notify_views
import patient_search
from notify_testkit import _click, _delivered_card, _extract, _msg, _patient, led as led  # noqa: F401
from test_ocr_original_sources import seeded


def test_all_terms_are_honoured_without_llm_results(led):
    _patient(led)
    _msg(led, 101, body='one two three four five six')
    _msg(led, 102, body='one two three four five other')
    got = patient_search.search(led.db, 1, 'one two three four five six')
    assert [r['message_id'] for r in got['items']] == [101]
    assert got['range_complete'] and not got['next_cursor']
    assert not led.db.execute("SELECT 1 FROM artifacts WHERE kind LIKE 'cloud_%'").fetchone()


def test_keyset_continuation_does_not_count_every_match_or_repeat_results(led):
    _patient(led)
    for mid in range(100, 121):
        _msg(led, mid, body=f'synthetic term {mid}')
    before = led.db.total_changes
    ids, cursor = [], None
    while True:
        got = patient_search.search(led.db, 1, 'term', cursor=cursor)
        ids.extend(item['message_id'] for item in got['items'])
        assert got['scanned'] <= patient_search.SCAN_LIMIT
        cursor = got['next_cursor']
        if cursor is None:
            break
    assert ids == list(range(120, 99, -1))
    assert led.db.total_changes == before


def test_empty_bounded_range_is_resumable_not_a_zero_match_claim(led, monkeypatch):
    _patient(led)
    _msg(led, 100, body='target in old thread')
    for mid in range(101, 105):
        _msg(led, mid, body='different')
    monkeypatch.setattr(patient_search, 'SCAN_LIMIT', 2)
    got = notify_views.patient_search_view(led.db, 1, 'target')
    assert got['items'] == [] and got['next_cursor'] and not got['range_complete']
    assert 'この範囲' in got['empty'] and any('まだ調べていない' in n for n in got['notes'])
    while got['next_cursor']:
        got = notify_views.patient_search_view(led.db, 1, 'target', cursor=got['next_cursor'])
    assert len(got['items']) == 1


def test_cursor_is_bound_to_patient_query_and_filters(led):
    _patient(led)
    for mid in range(100, 107):
        _msg(led, mid, body='synthetic term')
    cursor = patient_search.search(led.db, 1, 'term')['next_cursor']
    for pid, query in ((1, 'synthetic'), (2, 'term'), (1, 'term 資料:投稿')):
        with pytest.raises(ValueError, match='search_cursor_invalid'):
            patient_search.search(led.db, pid, query, cursor=cursor)


def test_single_source_budget_advances_over_a_completed_message_and_null_dates(led, monkeypatch):
    _patient(led)
    _msg(led, 101, body='target')
    _msg(led, 102, body='other')
    _msg(led, 103, body='other')
    led.db.execute('UPDATE messages SET posted_at_ts=NULL WHERE message_id=101')
    monkeypatch.setattr(patient_search, 'SCAN_LIMIT', 1)
    cursor, ids, cursors = None, [], []
    for _ in range(4):
        got = patient_search.search(led.db, 1, 'target', cursor=cursor)
        ids.extend(row['message_id'] for row in got['items'])
        cursor = got['next_cursor']
        if cursor is None:
            break
        assert cursor not in cursors
        cursors.append(cursor)
    assert ids == [101] and cursor is None


def test_original_is_rechecked_for_edit_delete_and_scope_revocation(led):
    _patient(led)
    _msg(led, 101, body='本人に発熱はありません。家族は発熱。')
    ref = patient_search.search(led.db, 1, '発熱')['items'][0]['source_ref']
    assert patient_search.read_source(led.db, 1, ref)['body_text'].startswith('本人に発熱はありません。')
    led.db.execute("UPDATE messages SET body_text='訂正された合成本文' WHERE message_id=101")
    with pytest.raises(ValueError, match='search_source_changed'):
        patient_search.read_source(led.db, 1, ref)
    led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=101")
    assert patient_search.search(led.db, 1, '発熱')['items'] == []
    led.db.execute("UPDATE patients SET fetch_state='forbidden' WHERE project_id=1")
    with pytest.raises(ValueError, match='patient_scope_unavailable'):
        patient_search.read_source(led.db, 1, ref)


def test_ocr_search_does_not_require_local_inference_but_rejects_replaced_document(tmp_path):
    ledger, key = seeded(tmp_path)
    cfg = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/yomitoku'}}
    try:
        got = patient_search.search(ledger.db, 1, '中止 資料:添付', cfg=cfg)
        assert len(got['items']) == 1 and got['items'][0]['source_ref']['key'] == key
        ref = got['items'][0]['source_ref']
        assert '本人' in patient_search.read_source(ledger.db, 1, ref, cfg=cfg)['body_text']
        assert patient_search.search(ledger.db, 1, '中止', cfg={})['items'] == []
        ledger.db.execute("UPDATE attachments SET state='pruned' WHERE attachment_id=1")
        view = notify_views.patient_search_view(ledger.db, 1, '中止 資料:添付', cfg=cfg)
        assert '原本ファイルはローカル未保存' in view['items'][0]['text']
        assert '添付元' in view['items'][0]['text']
        ledger.db.execute("UPDATE attachments SET sha256=? WHERE attachment_id=1", ('b' * 64,))
        with pytest.raises(ValueError, match='search_source_changed'):
            patient_search.read_source(ledger.db, 1, ref, cfg=cfg)
        assert patient_search.search(ledger.db, 1, '中止', cfg=cfg)['items'] == []
    finally:
        ledger.close()


def test_ocr_page_cursor_keeps_remaining_pages(tmp_path):
    ledger, key = seeded(tmp_path)
    cfg = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/yomitoku'}}
    try:
        aid = int(key.split(':')[1])
        doc = json.loads(ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()[0])
        doc['pages'] = [{'page': n, 'text': f'合成の記録 {n}', 'engine': 'pdf_text',
                         'text_hash': attachment_ocr._hash(f'合成の記録 {n}')} for n in range(1, 8)]
        doc['text_hash'] = attachment_ocr._hash(json.dumps(doc['pages'], ensure_ascii=False, sort_keys=True))
        ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), aid))
        one = patient_search.search(ledger.db, 1, '記録 資料:添付', cfg=cfg)
        two = patient_search.search(ledger.db, 1, '記録 資料:添付', cfg=cfg, cursor=one['next_cursor'])
        assert [row['page'] for row in one['items'] + two['items']] == list(range(1, 8))
        assert two['next_cursor'] is None
    finally:
        ledger.close()


@pytest.mark.parametrize('available', [False, True])
def test_attachment_cursor_keeps_lookahead_after_skipping_the_completed_attachment(tmp_path, monkeypatch,
                                                                                  available):
    ledger, key = seeded(tmp_path)
    cfg = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/yomitoku'}}
    try:
        artifact_id = int(key.split(':')[1])
        doc = json.loads(ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?',
                                          (artifact_id,)).fetchone()[0])
        expected_keys = [key]
        for aid in (2, 3):
            ledger.db.execute("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes) "
                              "VALUES(?,10,'downloaded',?,32)", (aid, 'a' * 64))
            artifact_id = ledger.artifact_add(attachment_ocr.KIND, json.dumps({**doc, 'attachment_id': aid}),
                project_id=1, message_id=10, meta={'attachment_id': aid})
            expected_keys.append(f'attachment_ocr:{artifact_id}:1')
        if not available:
            ledger.db.execute('UPDATE attachments SET sha256=?', ('b' * 64,))
        monkeypatch.setattr(patient_search, 'SCAN_LIMIT', 1)
        pages, cursor = [], None
        for index in range(3):
            page = patient_search.search(ledger.db, 1, '中止 資料:添付', cfg=cfg, cursor=cursor)
            assert page['scanned'] == 1 and page['pending'] == (0 if available else 1)
            cursor = page['next_cursor']
            assert (cursor is not None) == (index < 2)
            pages.append(page)
        keys = [item['source_ref']['key'] for page in pages for item in page['items']]
        assert keys == (expected_keys if available else [])
        assert pages[0]['next_cursor'] != pages[1]['next_cursor']
    finally:
        ledger.close()


def test_category_uses_current_source_bound_facts_and_date_filter(led):
    _patient(led)
    from datetime import datetime
    from mcs_queries import JST
    stamp = int(datetime(2026, 11, 7, 12, tzinfo=JST).timestamp())
    _msg(led, 101, body='服薬管理: 本人がカレンダーで管理。', ts=stamp)
    _extract(led, 101, {'patient_context': [{'category': 'medication_management',
              'text': '本人がカレンダーで管理。', 'evidence': '本人がカレンダーで管理。',
              'subject': 'patient', 'details': []}]})
    query = 'カレンダー 分類:服薬管理 開始:2026-11-07 終了:2026-11-07 資料:投稿'
    assert len(patient_search.search(led.db, 1, query)['items']) == 1
    assert patient_search.search(led.db, 1, query.replace('開始:2026-11-07', '開始:2026-11-08')
                                  .replace('終了:2026-11-07', '終了:2026-11-08'))['items'] == []
    led.db.execute("UPDATE messages SET content_hash=? WHERE message_id=101", ('f' * 64,))
    assert patient_search.search(led.db, 1, query)['items'] == []


def test_private_search_reads_other_thread_but_not_another_actor(led):
    spec = _delivered_card(led)
    _msg(led, 500, body='別スレッドの合成検索語、本人は否定。', parent=None)
    result = _click(led, spec, 'search', {'query': '合成検索語'})
    token = next(b['token'] for b in result['navigation'] if b['label'] == '1 原資料を開く')
    opened = _click(led, spec, 'search', token=token)
    assert opened['outcome'] == 'applied' and '別スレッド' in opened['body']
    assert 'https://www.medical-care.net/projects/medical/1' in opened['body']
    assert _click(led, spec, 'search', token=token, actor='discord:other')['error'] == 'actor_mismatch'
    assert not led.db.execute("SELECT 1 FROM command_receipts WHERE receipt_json LIKE '%別スレッド%'").fetchone()


def test_private_source_pages_preserve_every_character_and_recheck_source(led):
    from adapters.common import text
    spec = _delivered_card(led)
    body = ('合成原文。本人に発熱はありません。家族は発熱。\n' * 400) + '最終行'
    _msg(led, 500, body=body)
    result = _click(led, spec, 'search', {'query': '合成原文'})
    token = next(b['token'] for b in result['navigation'] if b['label'] == '1 原資料を開く')
    collected, page_tokens = [], []
    while token:
        page_tokens.append(token)
        opened = _click(led, spec, 'search', token=token)
        assert opened['outcome'] == 'applied'
        collected.append(opened['body'].split('\n\nMCS: ')[0])
        assert not any('長文のため' in chunk for chunk, _ in text.view_answer(opened, lambda _: True))
        token = next((b['token'] for b in opened['navigation'] if b['label'] == '次の範囲'), None)
    assert ''.join(collected) == body and len(page_tokens) > 1
    led.db.execute("UPDATE messages SET body_text='合成の訂正後原文' WHERE message_id=500")
    led.db.commit()
    stale = _click(led, spec, 'search', token=page_tokens[-1])
    assert '原資料が更新' in stale['body'] and '最終行' not in stale['body']


def test_cli_search_cursor_uses_the_same_bounded_reader(led, monkeypatch):
    import mcs_util
    import mcs_view
    from types import SimpleNamespace
    _patient(led)
    for mid in range(100, 107):
        _msg(led, mid, body='合成の検索対象')
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    view = SimpleNamespace(db=led.db)
    first = mcs_view.View._patient_context(view, 1, 'search', None, '合成', '')
    second = mcs_view.View._patient_context(view, 1, 'search', None, '合成', '',
                                           json.dumps(first['next_cursor']))
    assert len(first['items']) == 5 and len(second['items']) == 2 and second['next_cursor'] is None
    with pytest.raises(ValueError, match='search_cursor_invalid'):
        mcs_view.View._patient_context(view, 1, 'search', None, '合成', '', 'not-json')


def test_unfetched_original_is_explicitly_excluded_from_zero_match_claim(led):
    _patient(led)
    _msg(led, 101, body='取得待ちの合成本文')
    led.db.execute("UPDATE messages SET body_state='partial' WHERE message_id=101")
    got = notify_views.patient_search_view(led.db, 1, '合成')
    assert not got['items']
    assert any('本文未取得・確認不能の原資料が1件' in note for note in got['notes'])


@pytest.mark.parametrize('params', [
    {'unknown': True}, {'source_page': 1}, {'source_ref': {}, 'cursor': {}},
    {'source_ref': {}, 'source_page': True},
])
def test_private_read_reference_params_do_not_broaden_other_actions(led, params):
    import notify_action_tokens as tokens
    from notify_testkit import NOW
    spec = _delivered_card(led)
    token = tokens.mint(led.db, 1, 'search', params, {}, NOW)
    led.db.commit()
    result = _click(led, spec, 'search', token=token)
    assert result['outcome'] == 'rejected' and result['error'] == 'action_not_applicable'


@pytest.mark.parametrize('query', ['term 開始:bad', 'term 資料:bad', 'term 分類:unknown',
                                   'term 開始:2026-02-02 終了:2026-02-01',
                                   'term 資料:全て 資料:投稿'])
def test_invalid_filters_are_not_silently_ignored(query):
    with pytest.raises(ValueError):
        patient_search.parse_query(query)
