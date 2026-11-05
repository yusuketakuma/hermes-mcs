"""PCA and referral views borrow the same synthetic source-ready OCR snapshot."""
import copy
import json

import pytest

import attachment_extraction
import attachment_ocr
import original_source_keys
import ocr_document_reader
import pump_document
import pump_reader
import referral_reader
from test_referral_reader import CFG, referral as _referral_fixture

referral = _referral_fixture


def _pump_snapshot(referral):
    ledger, key = referral
    artifact_id = original_source_keys.parse(key)[1]
    value = json.loads(ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?',
                                        (artifact_id,)).fetchone()[0])
    text = 'PCAポンプ\n測定日:2026年11月5日 残量:10mL\n'
    value['pages'][0].update(text=text, text_hash=attachment_ocr._hash(text))
    value['text_hash'] = attachment_ocr._hash(json.dumps(value['pages'], ensure_ascii=False, sort_keys=True))
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(value), artifact_id))
    return ocr_document_reader.read(ledger.db, 1, cfg=CFG)


def test_shared_snapshot_is_read_only_and_projections_do_not_mutate(referral):
    ledger, _ = referral
    ledger.db.execute('PRAGMA query_only=ON')
    changes = ledger.db.total_changes
    snapshot = ocr_document_reader.read(ledger.db, 1, cfg=CFG)
    before = copy.deepcopy(snapshot)
    assert referral_reader.project(snapshot)['coverage']['documents'] == 1
    assert pump_reader.project(snapshot)['events'] == []
    assert snapshot == before and ledger.db.total_changes == changes


def test_pump_candidates_keep_complete_source_binding_and_cannot_be_measurements(referral):
    snapshot = _pump_snapshot(referral)
    row, = pump_reader.project(snapshot)['events']
    assert row['source_key'].startswith('attachment_ocr:') and row['page'] == 1
    assert row['source_artifact_id'] == snapshot['documents'][0]['document']['artifact_id']
    assert row['extraction_artifact_id'] == 999 and row['source_binding']
    assert row['parent_message_id'] == 10 and row['attachment_id'] == 1
    assert row['subject'] == 'unspecified' and row['unverified'] is True
    assert row['posted_at_ts'] is None and row['time']['value'] == '2026-11-05'


@pytest.mark.parametrize('fault', ['pending', 'binding'])
def test_common_reader_never_exposes_unready_or_mismatched_pages(referral, monkeypatch, fault):
    ledger, _ = referral
    current = attachment_extraction.current

    def changed(*args, **kwargs):
        row = current(*args, **kwargs)
        return {**row, **({'ready': False} if fault == 'pending' else {'source_binding': '0' * 64})}

    monkeypatch.setattr(attachment_extraction, 'current', changed)
    snapshot = ocr_document_reader.read(ledger.db, 1, cfg=CFG)
    assert not snapshot['documents'][0]['ready']
    assert not pump_reader.project(snapshot)['events']
    assert not snapshot['coverage']['complete']


def test_pump_projection_rejects_tampered_quote(referral, monkeypatch):
    snapshot = _pump_snapshot(referral)
    real = pump_document.project

    def changed(pages):
        result = real(pages)
        result['events'][0]['evidence_span'] = [0, 1]
        return result

    monkeypatch.setattr(pump_document, 'project', changed)
    result = pump_reader.project(snapshot)
    assert result['events'] == [] and result['coverage']['pending'] == 1
