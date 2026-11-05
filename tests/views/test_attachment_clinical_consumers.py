"""Clinical readers consume actual ready OCR artifacts without treating them as chat posts."""
import json

import attachment_extraction
import attachment_ocr
import mcs_util
import notify_views
import original_source_keys
import structured_view
from test_anthropic_runtime import routed
from test_attachment_inference import page_store, run
from test_cloud_after_local import local_then_cloud
from test_ocr_observations_pharmacy import pharmacy_document

__all__ = ['routed', 'page_store', 'local_then_cloud']


def _page(ledger, key, text):
    artifact = original_source_keys.parse(key)[1]
    document = json.loads(ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (artifact,)).fetchone()[0])
    document['pages'][0].update(text=text, text_hash=attachment_ocr._hash(text))
    document['text_hash'] = attachment_ocr._hash(json.dumps(document['pages'], ensure_ascii=False, sort_keys=True))
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(document), artifact))


def test_actual_ready_reader_medication_button_and_document_source(page_store, monkeypatch):
    ledger, cfg, key, _local_calls, _cloud_calls, answers = page_store
    quote = '本人はキラナを使用しています。'
    _page(ledger, key, quote)
    answers['local'] = {'symptoms': [], 'meds': [{'name': 'キラナ', 'action': 'none',
        'status': 'current', 'subject': 'patient', 'negated': False, 'evidence': quote}]}
    monkeypatch.setattr(mcs_util, 'load_config', lambda: cfg)
    assert list(attachment_extraction.iter_sources(ledger.db, 1, cfg=cfg)) == []
    pending = notify_views.meds_view(ledger.db, 1, 1, cfg=cfg)
    assert not pending['items'] and '解析待ち' in pending['empty']
    assert run(ledger, cfg, 'local')['done'] == 1
    source, = attachment_extraction.iter_sources(ledger.db, 1, cfg=cfg)
    assert source['source_key'] == key and source['message_id'] == 0
    assert source['posted_at_ts'] is None and source['extraction_artifact_id'] > source['artifact_id']
    assert list(structured_view.medication_post_ids(ledger.db, [1])) == [1]
    view = notify_views.meds_view(ledger.db, 1, 1, cfg=cfg)
    assert len(view['items']) == 1 and 'キラナ' in view['items'][0]['text']
    assert '添付#1・1ページ' in view['items'][0]['text']
    assert list(attachment_extraction.iter_sources(ledger.db, 2, cfg=cfg)) == []
    assert list(attachment_extraction.iter_sources(ledger.db, 1, mid=2, cfg=cfg)) == []
    cfg['attachment_ocr']['enabled'] = False
    assert list(attachment_extraction.iter_sources(ledger.db, 1, cfg=cfg)) == []
    assert not notify_views.meds_view(ledger.db, 1, 1, cfg=cfg)['items']


def test_actual_ready_pharmacy_reports_recheck_original_and_do_not_need_cached_rollup(page_store):
    ledger, cfg, key, _local_calls, _cloud_calls, answers = page_store
    quote = '本人の合成薬Aは残薬3錠。'
    _page(ledger, key, quote)
    answers['local'] = {'symptoms': [], **pharmacy_document(quote)}
    assert run(ledger, cfg, 'local')['done'] == 1
    lines = notify_views._pharmacy_state_lines(ledger.db, 1, {}, cfg=cfg)
    text = '\n'.join(lines)
    assert quote in text and '文書日時未確認' in text
    assert '親投稿#1・添付#1・1ページ' in text
    assert '投稿#0' not in text
    with ledger.db:
        ledger.db.execute("UPDATE attachments SET state='withdrawn' WHERE attachment_id=1")
    assert notify_views._pharmacy_state_lines(ledger.db, 1, {}, cfg=cfg) == []
