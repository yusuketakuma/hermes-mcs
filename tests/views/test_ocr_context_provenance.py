"""Synthetic OCR evidence retains distinct original pages through private display."""
import json

import pytest

from adapters.common.text import list_messages
from mcs_contract import project_url
import mcs_queries
import notify_views
import original_source_keys
import patient_context_plan
import patient_context_refresh
import patient_context_view
from test_ocr_original_sources import seeded


CFG = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/ocr'}}


@pytest.fixture
def stored(tmp_path, monkeypatch):
    led, first = seeded(tmp_path)
    raw = json.loads(led.db.execute('SELECT content FROM artifacts WHERE artifact_id=?',
                    (original_source_keys.parse(first)[1],)).fetchone()[0])
    led.db.execute("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                   "VALUES(2,10,'downloaded',?,32,1)", ('a' * 64,))
    raw['attachment_id'] = 2
    second_id = led.artifact_add('attachment_ocr_v1', json.dumps(raw), project_id=1, message_id=10,
                                 meta={'attachment_id': 2})
    led.db.commit()
    keys = [first, f'attachment_ocr:{second_id}:1']
    facts = []
    for key in keys:
        source = original_source_keys.source(led.db, 1, key)
        quote = source['body_text']
        facts += patient_context_plan.source_facts(source,
            [{'category': 'medication_management', 'text': quote, 'evidence': quote,
              'subject': 'patient', 'details': []}], night_id='synthetic', discovery='scan', found_at=1)
    monkeypatch.setattr(patient_context_refresh, 'context_view_read', lambda *a, **kw: ({
        'facts': facts, 'summary': [{'text': '合成の文書要約', 'fact_ids': [fact['id']]} for fact in facts],
        'coverage': {}, 'as_of': 2}, {}))
    yield led, keys, facts
    led.close()


def test_same_text_in_distinct_ocr_originals_stays_distinct(stored):
    led, keys, facts = stored
    ev = patient_context_view.evidence(led.db, 1, cfg=CFG)
    rows = [item for item in ev['items'] if item['source'] == 'attachment_ocr']
    assert len(rows) == 2 and {item['source_key'] for item in rows} == set(keys)
    assert {item['attachment_id'] for item in rows} == {1, 2}
    assert all(item['message_id'] == 0 and item['parent_message_id'] == 10 and item['page'] == 1 for item in rows)
    assert len({patient_context_view._assertion_key(item) for item in rows}) == 2
    assert {item['quote'] for item in rows} == {facts[0]['quote']}
    assert all(item['posted_at_ts'] is item['sender'] is None for item in rows)


@pytest.mark.parametrize('change', ["UPDATE attachments SET state='withdrawn'",
                                   "UPDATE attachments SET sha256='changed'",
                                   "UPDATE messages SET body_state='deleted' WHERE message_id=10"])
def test_invalidated_original_is_not_presented_as_current(stored, change):
    led, _, _ = stored
    led.db.execute(change)
    ev = patient_context_view.evidence(led.db, 1, cfg=CFG)
    assert not any(item['source'] == 'attachment_ocr' for item in ev['items'])
    assert not ev['summary'] and not ev['nightly_facts']


def test_private_rows_link_to_existing_room_and_name_parent_attachment_page(stored):
    led, keys, _ = stored
    view = notify_views.patient_context_list(led.db, 1, 'timeline', cfg=CFG, since=0, now=2)
    rows = [item for item in view['items'] if item.get('source_key') in keys]
    assert len(rows) == 2 and all(item['url'] == project_url(1) for item in rows)
    assert all('親投稿 #10' in item['source'] and '1ページ' in item['source'] for item in rows)
    assert all('#0' not in item['source'] and '文書日時未確認' in item['source'] for item in rows)
    assert {item['attachment_id'] for item in rows} == {1, 2}
    hidden = list_messages({'outcome': 'applied', 'action': 'list', 'list': view}, lambda pid: False, False)
    assert not any('合成薬A' in text for text in hidden)


def test_explicit_ocr_off_excludes_cached_unprocessed_sources(stored):
    led, keys, _ = stored
    off = {'attachment_ocr': {'enabled': False}}
    assert list(mcs_queries.attachment_ocr_sources(led.db, 1, cfg=off)) == []
    sources = list(mcs_queries.patient_context_sources(led.db, 1, cfg=off))
    assert all(source['source'] != 'attachment_ocr' for source in sources)
    assert mcs_queries.attachment_ocr_coverage(led.db, 1, off)['complete']
    assert all(original_source_keys.source(led.db, 1, key) is not None for key in keys)
    ev = patient_context_view.evidence(led.db, 1, cfg=off)
    assert not any(item['source'] == 'attachment_ocr' for item in ev['items'])
