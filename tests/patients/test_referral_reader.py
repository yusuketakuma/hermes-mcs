"""Referral chronology checks original identity and clinical readiness on temporary ledgers."""
import json

import pytest

import attachment_extraction
import attachment_ocr
import original_source_keys
import ocr_document_reader
import referral_document
import referral_reader
from test_ocr_original_sources import seeded

CFG = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/yomitoku'}}
BODY = ('診療情報提供書\n文書作成日: 2026年11月5日\n生年月日: 昭和25年1月1日\n'
        '傷病名: 合成疾患\n治療経過: 2025年3月2日 入院していない。\n'
        '2026年10月 合成薬の開始予定。\n備考: 時期不明の家族の記載。\n')


@pytest.fixture
def referral(tmp_path, monkeypatch):
    ledger, key = seeded(tmp_path)
    aid = original_source_keys.parse(key)[1]
    value = json.loads(ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()[0])
    value['pages'][0].update(text=BODY, text_hash=attachment_ocr._hash(BODY))
    value['text_hash'] = attachment_ocr._hash(json.dumps(value['pages'], ensure_ascii=False, sort_keys=True))
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(value), aid))

    def current(db, pid, source_key, **kwargs):
        raw = original_source_keys.source(db, pid, source_key)
        return ({'ready': True, 'source_binding': raw['_source_binding'], 'artifact_id': 999,
                 'document': {}, 'stage': 'local', 'result_hash': 'c' * 64} if raw else None)

    monkeypatch.setattr(attachment_extraction, 'current', current)
    yield ledger, key
    ledger.close()


def test_referral_read_keeps_dates_provenance_and_literal_denial_without_writes(referral):
    ledger, key = referral
    changes = ledger.db.total_changes
    ledger.db.execute('PRAGMA query_only=ON')
    result = referral_reader.read(ledger.db, 1, cfg=CFG)
    assert result['coverage']['complete'] and result['coverage']['documents'] == 1
    assert any('入院していない' in row['text'] for row in result['events'])
    assert any('開始予定' in row['text'] for row in result['events'])
    assert all(row['source_key'] == key and row['posted_at_ts'] is None for row in result['events'])
    assert all(row['parent_message_id'] == 10 and row['attachment_id'] == 1 and row['page'] == 1
               for row in result['events'])
    assert not any('生年月日' in row['text'] or '文書作成日' in row['text'] for row in result['events'])
    assert ledger.db.total_changes == changes


@pytest.mark.parametrize('fault', ['pending', 'binding', 'withdrawn', 'foreign', 'off', 'permission'])
def test_referral_never_adopts_unready_or_unavailable_sources(referral, monkeypatch, fault):
    ledger, _ = referral
    cfg, pid = CFG, 1
    if fault in ('pending', 'binding'):
        real = attachment_extraction.current

        def changed(*args, **kwargs):
            row = real(*args, **kwargs)
            return {**row, **({'ready': False} if fault == 'pending' else {'source_binding': '0' * 64})}
        monkeypatch.setattr(attachment_extraction, 'current', changed)
    elif fault == 'withdrawn':
        with ledger.db:
            ledger.db.execute("UPDATE attachments SET state='withdrawn' WHERE attachment_id=1")
    elif fault == 'foreign':
        ledger.ensure_patient(2)
        pid = 2
    elif fault == 'off':
        cfg = {'attachment_ocr': {'enabled': False}}
    else:
        with ledger.db:
            ledger.db.execute("UPDATE patients SET fetch_reason='access_revoked' WHERE project_id=1")
    result = referral_reader.read(ledger.db, pid, cfg=cfg)
    assert result['documents'] == result['events'] == []
    if fault in ('pending', 'binding'):
        assert result['coverage']['pending'] == 1 and not result['coverage']['complete']


@pytest.mark.parametrize('bound', ['bytes', 'time'])
def test_referral_resource_limit_is_not_reported_as_complete(referral, monkeypatch, bound):
    ledger, _ = referral
    monkeypatch.setattr(ocr_document_reader, 'MAX_SOURCE_BYTES' if bound == 'bytes' else 'READ_SECONDS', 0)
    result = referral_reader.read(ledger.db, 1, cfg=CFG)
    assert not result['events'] and result['coverage']['limited'] and not result['coverage']['complete']


def test_referral_rejects_unbound_projection(referral, monkeypatch):
    ledger, _ = referral
    real = referral_document.project

    def tampered(pages):
        value = real(pages)
        value['events'][0]['evidence_span'] = [0, 1]
        return value
    monkeypatch.setattr(referral_document, 'project', tampered)
    result = referral_reader.read(ledger.db, 1, cfg=CFG)
    assert not result['events'] and result['coverage']['pending'] == 1
