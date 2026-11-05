"""Synthetic OCR detail sources preserve clinical scope and never borrow posting dates."""
from copy import deepcopy
import hashlib
import json

import pytest

import attachment_ocr
import attachment_extraction
import observation_series
import original_source_keys
import patient_context
import patient_context_view
from test_ocr_original_sources import seeded


QUOTE = '本人は2026-10-01の空腹時の採血 Cr 1.0 mg/dL。'


def source(quote=QUOTE, *, artifact=1, attachment=1):
    return {'project_id': 1, 'message_id': 0, 'source': 'attachment_ocr',
        'source_key': f'attachment_ocr:{artifact}:1', 'source_artifact_id': artifact,
        'attachment_id': attachment, 'page': 1, 'parent_message_id': 10,
        'extraction_artifact_id': 20 + artifact, 'ready': True,
        'body_text': quote, 'body_state': 'full', 'content_hash': hashlib.sha256(quote.encode()).hexdigest(),
        '_source_binding': 'b' * 64, 'result_hash': 'c' * 64,
        'posted_at': None, 'posted_at_ts': None, 'sender_id': None, 'sender_name': None}


def observation(src):
    quote = src['body_text']
    fields = [('observation_name', 'Cr'), ('value', '1.0'), ('unit', 'mg/dL'),
              ('condition', '空腹'), ('measured_on', '2026-10-01')]
    return {**{key: value for key, value in src.items() if key not in ('body_text', '_source_binding', 'document')},
        'source_binding': src['_source_binding'], 'category': 'observations', 'subject': 'patient',
        'quote': quote, 'text': quote, 'evidence': quote, 'evidence_span': [0, len(quote)],
        'details': [{'key': key, 'value': value, 'evidence': quote} for key, value in fields]}


def series(items, originals):
    return observation_series.observation_series({'project_id': 1, 'items': items}, source_bodies=originals)


def test_ocr_measurement_uses_only_explicit_day_with_null_post_time():
    src = source()
    item = observation(src)
    result = series([item], {(src['source_key'], src['content_hash']): QUOTE})
    assert len(result['series']) == 1 and not result['unconfirmed']
    record = result['series'][0]['records'][0]
    assert record['event_date_text'] == '2026-10-01' and record['posted_at_ts'] is None
    assert record['items'][0]['source_key'] == src['source_key']
    assert record['items'][0]['parent_message_id'] == 10
    assert result['order_basis'] == 'event_day_within_series'


@pytest.mark.parametrize('mutation', ['missing_date', 'missing_unit', 'family', 'negated', 'wrong_value',
                                      'wrong_binding', 'borrowed_post_time', 'unbound_original', 'other_page'])
def test_ocr_unsafe_or_unbound_measurement_stays_unknown(mutation):
    src = source()
    item = observation(src)
    originals = {(src['source_key'], src['content_hash']): QUOTE}
    if mutation.startswith('missing_'):
        field = 'measured_on' if mutation == 'missing_date' else 'unit'
        item['details'] = [detail for detail in item['details'] if detail['key'] != field]
    elif mutation == 'family':
        item['reported_subject'] = 'family'
    elif mutation == 'negated':
        item['negated'] = True
    elif mutation == 'wrong_value':
        next(detail for detail in item['details'] if detail['key'] == 'value')['value'] = '9.0'
    elif mutation == 'wrong_binding':
        item['source_binding'] = None
    elif mutation == 'borrowed_post_time':
        item['posted_at_ts'] = 1_800_000_000
    elif mutation == 'unbound_original':
        originals.clear()
    else:
        item['source_key'] = 'attachment_ocr:1:2'
    result = series([item], originals)
    assert not result['series'] and result['unconfirmed']
    assert result['unconfirmed'][0]['relation'] == 'unknown'
    assert '1.0' not in str(result['unconfirmed'])


def test_identical_ocr_pages_are_distinct_originals_and_have_distinct_references():
    first, second = source(), source(artifact=2, attachment=2)
    items = [observation(first), observation(second)]
    originals = {(src['source_key'], src['content_hash']): QUOTE for src in (first, second)}
    confirmed = series(items, originals)
    assert len(confirmed['series'][0]['records']) == 2
    assert {record['items'][0]['source_key'] for record in confirmed['series'][0]['records']} == {
        first['source_key'], second['source_key']}
    for item in items:
        item['details'] = [detail for detail in item['details'] if detail['key'] != 'measured_on']
    result = series(items, {(src['source_key'], src['content_hash']): QUOTE for src in (first, second)})
    keys = {row['references'][0]['source_key'] for row in result['unconfirmed']}
    assert keys == {first['source_key'], second['source_key']}
    assert all(row['references'][0]['message_id'] == 0 for row in result['unconfirmed'])


def test_ocr_view_rechecks_current_original_even_with_stale_ready_result(tmp_path, monkeypatch):
    led, key = seeded(tmp_path)
    try:
        artifact = original_source_keys.parse(key)[1]
        doc = json.loads(led.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (artifact,)).fetchone()[0])
        doc['pages'][0].update(text=QUOTE, text_hash=attachment_ocr._hash(QUOTE))
        doc['text_hash'] = attachment_ocr._hash(json.dumps(doc['pages'], ensure_ascii=False, sort_keys=True))
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), artifact))
        raw = original_source_keys.source(led.db, 1, key)
        ready = {**raw, 'extraction_artifact_id': 100, 'ready': True, 'document': {}}
        result = {'artifact_id': 100, 'ready': True, 'document': {}, 'result_hash': 'c' * 64,
                  'source_binding': raw['_source_binding'], 'stage': 'local'}
        # Deliberately retain stale ready data: the view must recheck the actual original.
        monkeypatch.setattr(attachment_extraction, 'current', lambda *a, **kw: result)
        ev = {'project_id': 1, 'items': [observation(ready)]}
        result = patient_context_view.observations(led.db, ev, cfg={'attachment_ocr': {'enabled': True, 'executable': '/synthetic/ocr'}})
        assert len(result['series']) == 1
        assert result['series'][0]['records'][0]['items'][0]['extraction_artifact_id'] == 100
        led.db.execute("UPDATE attachments SET state='withdrawn' WHERE attachment_id=1")
        invalid = patient_context_view.observations(led.db, ev, cfg={'attachment_ocr': {'enabled': True, 'executable': '/synthetic/ocr'}})
        assert not invalid['series'] and invalid['source_checks']['rejected'] == 1
        assert not invalid['unconfirmed']
    finally:
        led.close()



@pytest.mark.parametrize('mutation', ['ready', 'pending', 'wrong_binding', 'disabled', 'invalid_config',
                                      'foreign_patient', 'changed_hash', 'other_page'])
def test_ocr_view_reads_selected_source_only(tmp_path, monkeypatch, mutation):
    led, key = seeded(tmp_path)
    try:
        artifact = original_source_keys.parse(key)[1]
        doc = json.loads(led.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (artifact,)).fetchone()[0])
        doc['pages'][0].update(text=QUOTE, text_hash=attachment_ocr._hash(QUOTE))
        doc['text_hash'] = attachment_ocr._hash(json.dumps(doc['pages'], ensure_ascii=False, sort_keys=True))
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), artifact))
        raw = original_source_keys.source(led.db, 1, key)
        ready = {**raw, 'extraction_artifact_id': 100, 'ready': True}
        result = {'artifact_id': 100, 'ready': mutation != 'pending', 'document': {},
                  'result_hash': 'c' * 64, 'source_binding': raw['_source_binding'], 'stage': 'local'}
        if mutation == 'wrong_binding':
            result['source_binding'] = 'f' * 64
        selected = observation(ready)
        if mutation == 'changed_hash':
            selected['content_hash'] = 'd' * 64
        elif mutation == 'other_page':
            selected['source_key'] = f'attachment_ocr:{artifact}:2'
            selected['page'] = 2
        cfg = {'attachment_ocr': {'enabled': mutation != 'disabled', 'executable': '/synthetic/ocr'}}
        if mutation == 'invalid_config':
            cfg['attachment_ocr']['executable'] = 'relative'
        calls, originals = [], []
        original = original_source_keys.source

        def read_original(db, pid, source_key):
            originals.append(source_key)
            return original(db, pid, source_key)

        def read_result(db, pid, source_key, **kw):
            calls.append(source_key)
            return result

        monkeypatch.setattr(original_source_keys, 'source', read_original)
        monkeypatch.setattr(attachment_extraction, 'current', read_result)
        monkeypatch.setattr(attachment_extraction, 'iter_sources',
                            lambda *a, **kw: pytest.fail('unbounded source iteration'))
        items = [selected] + [observation(source(artifact=10000 + n, attachment=10000 + n))
                              for n in range(4999)]
        ev = {'project_id': 2 if mutation == 'foreign_patient' else 1, 'items': items}
        view = patient_context_view.observations(led.db, ev, source_limit=1, cfg=cfg)
        assert set(calls + originals) <= {selected['source_key']}
        assert len(calls) <= 1 and len(originals) <= 2
        assert view['source_checks']['eligible'] == 5000 and view['source_checks']['selected'] == 1
        assert view['source_checks']['limited'] is True
        assert bool(view['series']) is (mutation == 'ready')
        assert view['source_checks']['verified'] == (1 if mutation == 'ready' else 0)
    finally:
        led.close()


def pharmacy_document(quote):
    return {'patient_context': [{'category': 'medication_management', 'text': quote, 'evidence': quote,
        'subject': 'patient', 'details': [{'key': 'drug_name', 'value': '合成薬A', 'evidence': quote},
                                       {'key': 'residual_quantity', 'value': '3錠', 'evidence': quote}]}]}


def test_pharmacy_ocr_reports_keep_exact_source_and_separate_identical_documents():
    quote = '本人の合成薬Aは残薬3錠。'
    one, two = source(quote), source(quote, artifact=2, attachment=2)
    document = pharmacy_document(quote)
    before = deepcopy(document)
    a, = patient_context.pharmacy_state_items(document, one)
    b, = patient_context.pharmacy_state_items(document, two)
    assert a['source_key'] != b['source_key'] and a['fingerprint'] != b['fingerprint']
    assert a['message_id'] == 0 and a['parent_message_id'] == 10 and a['page'] == 1
    assert a['posted_at'] is a['posted_at_ts'] is None
    assert a['extraction_artifact_id'] == one['extraction_artifact_id']
    assert a['parent']['evidence'] == quote and document == before


@pytest.mark.parametrize('mutation', ['pending', 'wrong_page', 'bad_body_hash', 'post_date', 'nonliteral_value'])
def test_pharmacy_ocr_reports_fail_closed(mutation):
    quote = '本人の合成薬Aは残薬3錠。'
    src, document = source(quote), pharmacy_document(quote)
    if mutation == 'pending':
        src['ready'] = False
    elif mutation == 'wrong_page':
        src['page'] = 2
    elif mutation == 'bad_body_hash':
        src['content_hash'] = 'a' * 64
    elif mutation == 'post_date':
        src['posted_at_ts'] = 1_800_000_000
    else:
        document['patient_context'][0]['details'][-1]['value'] = '8錠'
    assert patient_context.pharmacy_state_items(document, src) == []
