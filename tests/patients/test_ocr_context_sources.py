"""OCR pages enter patient context only as ready, independently grounded originals."""
from types import SimpleNamespace
import sys

import pytest

import mcs_queries
import original_source_keys
import patient_context_plan as plan
import patient_context_refresh as refresh
import semantic_audit
from mcs_util import payload_hash
from test_ocr_original_sources import seeded


@pytest.fixture
def source_store(tmp_path, monkeypatch):
    led, key = seeded(tmp_path)
    result = {'artifact_id': 100, 'result_hash': payload_hash(['local']), 'ready': False,
              'document': {'patient_context': []}}
    monkeypatch.setitem(sys.modules, 'attachment_inference', SimpleNamespace(current=lambda *a, **kw: dict(result)))
    yield led, key, result
    led.close()


def test_current_sources_keep_ocr_separate_and_pending_visible(source_store):
    led, key, result = source_store
    sources = list(mcs_queries.patient_context_sources(led.db, 1))
    chat, page = next(s for s in sources if s['source'] == 'chat'), next(s for s in sources if s['source'] == 'attachment_ocr')
    assert chat['body_text'] == '合成チャット本文'
    assert page['source_key'] == key and page['message_id'] == 0
    assert not plan.source_usable(page) and not refresh._sources_intact([page])
    before = plan.source_signature(page)
    result.update(ready=True, result_hash=payload_hash(['cloud']))
    ready = next(mcs_queries.patient_context_sources(led.db, 1, mid=key))
    assert plan.source_usable(ready) and refresh._sources_intact([ready])
    assert plan.source_signature(ready) != before
    assert ready['posted_at_ts'] is ready['sender_name'] is None
    assert list(mcs_queries.patient_context_sources(led.db, 2, mid=key)) == []


def test_ocr_paid_binding_preserves_retention_and_rejects_changed_content(source_store):
    led, key, result = source_store
    result['ready'] = True
    sources = list(mcs_queries.patient_context_sources(led.db, 1, mid=key))
    snapshot = {'sources': sources, 'source_sig': payload_hash(['sources']), 'contract': 'synthetic'}
    before = refresh._cloud_input_binding(led.db, 1, snapshot)
    led.db.execute('UPDATE attachments SET downloaded_at=2 WHERE attachment_id=1')
    assert refresh._cloud_input_binding(led.db, 1, snapshot) == before
    led.db.execute("UPDATE attachments SET sha256=? WHERE attachment_id=1", ('b' * 64,))
    with pytest.raises(ValueError, match='nightly_cloud_source_changed'):
        refresh._cloud_input_binding(led.db, 1, snapshot)


def test_ocr_quality_sources_and_facts_require_exact_original_quotes(source_store):
    led, key, result = source_store
    result['ready'] = True
    source = next(mcs_queries.patient_context_sources(led.db, 1, mid=key))
    body = source['body_text']
    items = [{'category': 'medication_management', 'text': body, 'evidence': body, 'subject': 'patient', 'details': []}]
    facts = plan.source_facts(source, items, night_id='synthetic', discovery='scan', found_at=1)
    assert facts and plan.validated_source_facts(source, facts) == facts
    index = semantic_audit._patient_quality_sources([source])
    assert semantic_audit._patient_quality_facts(facts, index)
    with pytest.raises(ValueError, match='patient_quality_fact_invalid'):
        semantic_audit._patient_quality_facts([{**facts[0], 'evidence': '別の合成原文'}], index)
    assert original_source_keys.source(led.db, 1, key)['body_text'] == body


OCR_CFG = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/ocr'}, 'semantic': {'mode': 'off'}}


def test_attachment_coverage_distinguishes_pending_ready_and_opt_out(source_store):
    led, key, result = source_store
    pending = mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)
    assert not pending['complete'] and pending['clinical_pending'] == 1
    assert pending['ocr_complete'] == 1 and pending['pending'] == 0
    result['ready'] = True
    ready = mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)
    assert ready['complete'] and ready['clinical_pending'] == 0
    assert ready['signature'] != pending['signature']
    disabled = {'attachment_ocr': {'enabled': False}}
    assert mcs_queries.attachment_ocr_coverage(led.db, 1, disabled)['complete']
    assert not any(s['source'] == 'attachment_ocr' for s in mcs_queries.patient_context_sources(led.db, 1, cfg=disabled))
    led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (original_source_keys.parse(key)[1],))
    unknown = mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)
    assert not unknown['complete'] and unknown['pending'] == 1


def test_observed_unsupported_is_excluded_but_failed_is_pending(source_store, monkeypatch):
    import attachment_ocr
    led, _, _ = source_store
    monkeypatch.setattr(attachment_ocr, 'current', lambda *a, **kw: None)
    state = {'state': 'unsupported'}
    monkeypatch.setattr(attachment_ocr, 'view', lambda *a, **kw: state)
    excluded = mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)
    assert excluded['complete'] and excluded['excluded'] == 1 and excluded['eligible'] == 0
    state['state'] = 'failed'
    failed = mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)
    assert not failed['complete'] and failed['pending'] == 1


def test_image_only_post_readiness_releases_after_ocr_inference(source_store, monkeypatch):
    import hashlib
    import time
    led, _, result = source_store
    led.db.execute('UPDATE messages SET body_html=?,body_text=?,content_hash=? WHERE message_id=10',
                   ('<p></p>', '', hashlib.sha256(b'<p></p>').hexdigest()))
    led.db.commit()
    monkeypatch.setattr(refresh.initial_import_state, 'acquisition_status',
                        lambda *a, **kw: {'complete': True, 'signature': payload_hash(['synthetic-acquired'])})
    assert not refresh._inference_ready(led, 1, OCR_CFG, deadline=time.monotonic() + 600)
    result['ready'] = True
    assert refresh._inference_ready(led, 1, OCR_CFG, deadline=time.monotonic() + 600)


def test_verified_empty_page_needs_no_clinical_inference(source_store, monkeypatch):
    import json
    import attachment_ocr
    led, key, _ = source_store
    artifact = original_source_keys.parse(key)[1]
    doc = json.loads(led.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (artifact,)).fetchone()[0])
    doc['state'] = 'empty'
    doc['pages'][0].update(text='', text_hash=attachment_ocr._hash(''))
    doc['text_hash'] = attachment_ocr._hash(json.dumps(doc['pages'], ensure_ascii=False, sort_keys=True))
    led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), artifact))
    monkeypatch.setitem(sys.modules, 'attachment_inference', SimpleNamespace(
        current=lambda *a, **kw: pytest.fail('empty original must not need clinical inference')))
    page = next(mcs_queries.patient_context_sources(led.db, 1, mid=key, cfg=OCR_CFG))
    assert page['empty'] and page['extraction_ready'] and refresh._sources_intact([page])
    assert mcs_queries.attachment_ocr_coverage(led.db, 1, OCR_CFG)['complete']


@pytest.mark.parametrize('subject,quote', [
    ('patient', '本人は2026年10月1日、合成薬Aを中止しました。'),
    ('family', '母は2026年10月1日、合成薬Aを中止しました。'),
    ('patient', '本人は2026年10月1日、合成薬Aを中止していません。'),
])
def test_clinical_medication_seed_preserves_quote_subject_and_negation(subject, quote):
    source = {'project_id': 1, 'message_id': 0, 'source_key': 'attachment_ocr:1:1',
              'body_text': quote, 'body_state': 'full', 'content_hash': payload_hash(quote),
              'posted_at': None, 'posted_at_ts': None, 'source': 'attachment_ocr'}
    doc = {'patient_context': [], 'meds': [{'name': '合成薬A', 'subject': subject,
        'status': 'current', 'negated': 'いません' in quote, 'evidence': quote}]}
    items = mcs_queries.attachment_context_items(doc, source)
    assert len(items) == 1 and items[0]['subject'] == subject
    assert items[0]['evidence'] == items[0]['text'] == quote
    assert items[0]['details'] == [{'key': 'drug_name', 'value': '合成薬A', 'evidence': quote}]
    facts = plan.source_facts(source, items, night_id='synthetic', discovery='seed', found_at=1)
    assert facts and facts[0]['source_key'] == source['source_key']
    assert facts[0]['posted_at_ts'] is facts[0]['sender'] is None
    assert facts[0]['temporal']['event_day'] == '2026-10-01'
    assert facts[0]['temporal']['negated'] is ('いません' in quote)


def test_clinical_seed_does_not_infer_dose_from_strength_or_unbound_values():
    quote = '本人は合成薬A 5mg錠を服用しています。用量は不明です。'
    source = {'project_id': 1, 'message_id': 0, 'body_text': quote, 'body_state': 'full',
              'content_hash': payload_hash(quote), 'posted_at_ts': None}
    doc = {'patient_context': [], 'meds': [{'name': '合成薬A', 'dose': '5mg',
        'subject': 'patient', 'status': 'current', 'negated': False, 'evidence': quote}],
        'labs': [{'name': '合成検査', 'value': 99, 'unit': 'mg/dL'}], 'vitals': {'sbp': 200}}
    assert mcs_queries.attachment_context_items(doc, source) == []
    doc['meds'][0].update(unverified=True)
    assert mcs_queries.attachment_context_items(doc, source) == []
