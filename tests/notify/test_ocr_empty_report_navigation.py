"""Ready OCR originals remain reportable when clinical extraction finds no medications."""
import copy
import json

import pytest

import attachment_extraction
import attachment_feedback
import attachment_ocr
import attachment_report_reader
from adapters.common import envelopes
import notify_cards
import notify_transport
import notify_views
import original_source_keys
from notify_testkit import CFG, NOW, _click, _delivered_card, _spec, led, pinned_clock

__all__ = ['led', 'pinned_clock']


def _seed(ledger, monkeypatch, count=6, source_mid=100):
    if source_mid != 100:
        monkeypatch.setitem(CFG['notify'], 'reply_cards', True)
    spec = _delivered_card(ledger)
    card_id = (ledger.db.execute("SELECT card_id FROM notification_cards WHERE "
        "json_extract(anchor_key,'$.source')=?", (source_mid,)).fetchone()[0]
        if source_mid != 100 else 1)
    cfg = copy.deepcopy(CFG)
    cfg['attachment_ocr'] = {'enabled': True, 'executable': '/synthetic/yomitoku'}
    monkeypatch.setitem(CFG, 'attachment_ocr', cfg['attachment_ocr'])
    if source_mid != 100:
        assert notify_transport.apply_thread_receipt(ledger, {
            'delivery_id': spec['delivery_id'], 'message_id': 'm-9',
            'thread_id': 'synthetic-thread'}, cfg, now=NOW)['applied']
    pages = [{'page': page, 'engine': 'pdf_text', 'text': f'本人は合成薬{page}を服用しています。'}
             for page in range(1, count + 1)]
    for page in pages:
        page['text_hash'] = attachment_ocr._hash(page['text'])
    with ledger.db:
        ledger.db.execute("UPDATE messages SET body_text='',body_html='' WHERE message_id=?", (source_mid,))
        ledger.db.execute("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                          "VALUES(7,?,'downloaded',?,32,1)", (source_mid, 'a' * 64))
    raw = {'schema': attachment_ocr.SCHEMA, 'runtime_contract': attachment_ocr.RUNTIME_CONTRACT,
           'engine': 'yomitoku', 'engine_version': attachment_ocr.ENGINE_VERSION, 'state': 'complete',
           'attachment_id': 7, 'input_sha256': 'a' * 64, 'input_bytes': 32, 'pages': pages,
           'text_hash': attachment_ocr._hash(json.dumps(pages, ensure_ascii=False, sort_keys=True))}
    aid = ledger.artifact_add(attachment_ocr.KIND, json.dumps(raw), project_id=1,
                              message_id=source_mid, meta={'attachment_id': 7})

    def current(db, pid, key, **kwargs):
        source = original_source_keys.source(db, pid, key)
        if source is None:
            return None
        return {'ready': True, 'source_binding': source['_source_binding'], 'artifact_id': 999,
                'document': {'meds': []}, 'stage': 'local', 'result_hash': 'c' * 64, 'require_cloud': False}

    monkeypatch.setattr(attachment_extraction, 'current', current)
    monkeypatch.setattr('mcs_util.load_config', lambda: cfg)
    # Rebind the live card to the newly retained OCR original before opening its body.
    with ledger.db:
        specs = []
        notify_cards.issue_render(ledger.db, card_id, cfg, NOW + 1, specs, force=True)
    spec = _spec(ledger, card_id)
    return spec, cfg, aid


def _open_pages(ledger, spec):
    body = _click(ledger, spec, 'body', now=NOW + 2)
    button, = body['navigation']
    assert button['id'] == 'body' and '添付の解析ページ' in button['label']
    return _click(ledger, spec, 'body', token=button['token'], now=NOW + 3), button


def test_image_only_zero_medication_result_can_report_each_ready_page(led, monkeypatch):
    spec, cfg, aid = _seed(led, monkeypatch)
    opened, _ = _open_pages(led, spec)
    assert len(opened['list']['items']) == 5 and opened['list']['has_next']
    assert '合成薬1' in opened['list']['items'][0]['text']
    reports = [b for b in opened['navigation'] if b['id'] == 'report']
    assert len(reports) == 5
    token = reports[0]['token']
    report = _click(led, spec, 'report', token=token, now=NOW + 4)
    assert report['modal'] and report['outcome'] == 'applied'
    context = opened['token_ctx'][token]['context']
    req = envelopes.human_payload('report', 'discord:1001', context, report['params'],
                                 {'field': 'meds', 'note': '合成薬の全件見落し'})
    assert req['source_key'] == f'attachment_ocr:{aid}:1'
    assert req['artifact_id'] == 999 and req['message_id'] == 100
    assert attachment_feedback.target(led.db, req, cfg=cfg) is not None
    next_page = next(b for b in opened['navigation'] if b['label'] == '次の5ページ')
    second = _click(led, spec, 'body', token=next_page['token'], now=NOW + 5)
    assert len(second['list']['items']) == 1 and not second['list']['has_next']
    last = next(b for b in second['navigation'] if b['id'] == 'report')
    assert second['token_ctx'][last['token']]['context']['extract_ref']['source_key'] == f'attachment_ocr:{aid}:6'


def test_reply_image_only_zero_extraction_keeps_its_own_parent_pin(led, monkeypatch):
    spec, _, aid = _seed(led, monkeypatch, count=1, source_mid=101)
    opened, _ = _open_pages(led, spec)
    report = next(button for button in opened['navigation'] if button['id'] == 'report')
    ref = opened['token_ctx'][report['token']]['context']['extract_ref']
    assert ref == {'message_id': 101, 'artifact_id': 999, 'source_key': f'attachment_ocr:{aid}:1'}
    result = _click(led, spec, 'report', token=report['token'], now=NOW + 4)
    assert result['outcome'] == 'applied', result.get('error')


def test_private_ocr_page_list_rechecks_actor_manifest_and_source(led, monkeypatch):
    spec, _, _ = _seed(led, monkeypatch, count=1)
    opened, button = _open_pages(led, spec)
    denied = _click(led, spec, 'body', token=button['token'], actor='discord:2002', now=NOW + 4)
    assert denied['error'] == 'actor_mismatch'
    report = next(b for b in opened['navigation'] if b['id'] == 'report')
    with led.db:
        led.db.execute("UPDATE attachments SET state='withdrawn' WHERE attachment_id=7")
    assert _click(led, spec, 'report', token=report['token'], now=NOW + 5)['outcome'] == 'rejected'
    with led.db:
        led.db.execute('UPDATE notification_view_manifests SET invalidated=1')
    assert _click(led, spec, 'body', token=button['token'], now=NOW + 6)['error'] == 'manifest_invalid'


def test_unready_pages_never_get_report_controls(led, monkeypatch):
    spec, _, _ = _seed(led, monkeypatch, count=1)
    monkeypatch.setattr(attachment_extraction, 'current', lambda *args, **kwargs: None)
    opened, _ = _open_pages(led, spec)
    assert not opened['list']['items']
    assert all(button['id'] != 'report' for button in opened['navigation'])


def test_empty_scan_windows_offer_next_controls_until_a_ready_page_is_reachable(led, monkeypatch):
    spec, _, aid = _seed(led, monkeypatch, count=5)
    monkeypatch.setattr(attachment_report_reader, 'SCAN_LIMIT', 2)
    monkeypatch.setattr(attachment_report_reader, 'READ_SECONDS', 1000)
    current = attachment_extraction.current

    def pending(db, pid, key, **kwargs):
        return None if int(key.rsplit(':', 1)[1]) < 5 else current(db, pid, key, **kwargs)

    monkeypatch.setattr(attachment_extraction, 'current', pending)
    opened, _ = _open_pages(led, spec)
    assert not opened['list']['items'] and any('点検未完了' in note for note in opened['list']['notes'])
    for n in range(2):
        next_page = next(button for button in opened['navigation'] if button['label'] == '次の5ページ')
        opened = _click(led, spec, 'body', token=next_page['token'], now=NOW + 5 + n)
    report, = [button for button in opened['navigation'] if button['id'] == 'report']
    assert opened['token_ctx'][report['token']]['context']['extract_ref']['source_key'] == f'attachment_ocr:{aid}:5'


@pytest.mark.parametrize('field,value', [('is_archived', 1), ('fetch_state', 'forbidden'),
                                        ('fetch_reason', 'access_revoked')])
def test_unavailable_patient_never_exposes_page_text_or_report_ref(led, monkeypatch, field, value):
    spec, _, _ = _seed(led, monkeypatch, count=1)
    with led.db:
        led.db.execute(f'UPDATE patients SET {field}=? WHERE project_id=1', (value,))
    opened, _ = _open_pages(led, spec)
    assert not opened['list']['items']
    assert all(button['id'] != 'report' for button in opened['navigation'])


@pytest.mark.parametrize('mutation', ['foreign', 'deleted', 'withdrawn'])
def test_stale_parent_or_attachment_never_exposes_ready_page(led, monkeypatch, mutation):
    spec, _, _ = _seed(led, monkeypatch, count=1)
    _, button = _open_pages(led, spec)
    with led.db:
        if mutation == 'foreign':
            led.db.execute('UPDATE messages SET project_id=2 WHERE message_id=100')
        elif mutation == 'deleted':
            led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=100")
        else:
            led.db.execute("UPDATE attachments SET state='withdrawn' WHERE attachment_id=7")
    result = _click(led, spec, 'body', token=button['token'], now=NOW + 5)
    assert result['outcome'] == 'rejected' and 'list' not in result


def test_page_view_only_reads_its_manifest_posts(led, monkeypatch):
    spec, cfg, _ = _seed(led, monkeypatch, count=1)
    with led.db:
        led.db.execute('UPDATE notification_cards SET layout=4 WHERE card_id=1')
    calls = []
    real = notify_views.attachment_report_view

    def view(db, pid, mids, **kwargs):
        calls.append((pid, mids))
        return real(db, pid, mids, **kwargs)

    monkeypatch.setattr(notify_views, 'attachment_report_view', view)
    _open_pages(led, spec)
    assert calls == [(1, [100])]
    # The parent card's frozen manifest does not grant its reply or another patient's post.
    assert 101 not in calls[0][1]
    assert cfg['attachment_ocr']['enabled']
