"""Synthetic OCR originals retain identity without becoming chat messages."""
import json

import pytest

import attachment_ocr
import inference_dependency_loss as loss
import original_source_keys as originals
import patient_context_artifacts
from mcs_util import payload_hash
from semantic_testkit import _ledger, _message, _patient


def seeded(tmp_path):
    led = _ledger(tmp_path)
    _patient(led)
    led.save_messages([_message(10, body='合成チャット本文')], project_id=1)
    led.db.execute("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                   "VALUES(1,10,'downloaded',?,32,1)", ('a' * 64,))
    pages = [{'page': 1, 'engine': 'pdf_text', 'text': '本人は2026年10月1日、合成薬Aを中止しました。'}]
    for page in pages:
        page['text_hash'] = attachment_ocr._hash(page['text'])
    doc = {'schema': attachment_ocr.SCHEMA, 'runtime_contract': attachment_ocr.RUNTIME_CONTRACT,
           'engine': 'yomitoku', 'engine_version': attachment_ocr.ENGINE_VERSION,
           'state': 'complete', 'attachment_id': 1, 'input_sha256': 'a' * 64,
           'input_bytes': 32, 'pages': pages,
           'text_hash': attachment_ocr._hash(json.dumps(pages, ensure_ascii=False, sort_keys=True))}
    artifact = led.artifact_add(attachment_ocr.KIND, json.dumps(doc), project_id=1,
                                message_id=10, meta={'attachment_id': 1})
    led.db.commit()
    return led, f'attachment_ocr:{artifact}:1'


@pytest.mark.parametrize('key', ['karte:1', 'attachment_ocr:1:1', f'attachment_ocr:{2**63-1}:30'])
def test_original_key_roundtrip(key):
    assert originals.valid(key)
    assert originals.parse(key)[1] > 0
    assert patient_context_artifacts._key(1, key) == key


@pytest.mark.parametrize('key', [None, 1, True, 'karte:0', 'karte:1:1', 'attachment_ocr:1',
    'attachment_ocr:1:0', 'attachment_ocr:1:31', 'attachment_ocr:01:1', f'karte:{2**63}',
    'attachment_ocr:1:1:1', 'attachment_ocr:1:1\n'])
def test_original_key_fails_closed(key):
    assert not originals.valid(key)
    with pytest.raises(ValueError):
        originals.parse(key)


def test_ocr_original_exact_binding_and_null_attribution(tmp_path):
    led, key = seeded(tmp_path)
    try:
        source = originals.source(led.db, 1, key)
        assert source['message_id'] == 0 and source['parent_message_id'] == 10
        assert source['posted_at'] is source['posted_at_ts'] is source['sender_id'] is source['sender_name'] is None
        assert originals.source(led.db, 2, key) is None
        before = originals.binding(led.db, 1, key)
        led.db.execute("UPDATE attachments SET state='pruned' WHERE attachment_id=1")
        assert originals.binding(led.db, 1, key) == before
        assert originals.source(led.db, 1, key)['original_available'] is False
        assert before == loss.source_binding(led.db, 1, 0, source_key=key)
        led.db.execute('UPDATE attachments SET downloaded_at=2 WHERE attachment_id=1')
        assert originals.binding(led.db, 1, key) == before
        led.db.execute("UPDATE messages SET content_hash=? WHERE message_id=10", ('b' * 64,))
        assert originals.binding(led.db, 1, key) == before
        led.db.execute("UPDATE attachments SET state='withdrawn' WHERE attachment_id=1")
        assert originals.source(led.db, 1, key) is None
    finally:
        led.close()


def test_karte_existing_binding_formula_is_preserved(tmp_path):
    led = _ledger(tmp_path)
    try:
        content = {'comment': '合成の原本', 'updated_at': '2026-10-01T10:00:00+09:00'}
        artifact = led.artifact_add('karte_summary', json.dumps(content), project_id=1, meta={'karte_id': 7})
        key = f'karte:{artifact}'
        assert originals.binding(led.db, 1, key) == payload_hash(['karte-source/v1', 1, artifact, payload_hash(content), 7])
    finally:
        led.close()


def test_ocr_loss_capture_before_withdrawal_and_strict_revision(tmp_path):
    led, key = seeded(tmp_path)
    try:
        led.db.execute('BEGIN IMMEDIATE')
        snapshot = loss.capture_tx(led.db, 1, 0, source_key=key)
        assert snapshot['source_key'] == key and snapshot['gaps']
        led.db.execute("UPDATE attachments SET state='withdrawn' WHERE attachment_id=1")
        loss.record_tx(led.db, snapshot)
        proof = loss.read(led.db, 1)
        assert not proof['complete']
        assert 'invalid_dependency_proof' not in proof['coverage']['reasons']
        origin = {**snapshot['gaps'][0], 'revision': originals.parse(key)[1] + 1}
        assert not loss._valid_loss({'origin': origin, 'gap': True})
    finally:
        led.close()


def test_current_ocr_dependency_gap_can_be_resolved_only_by_source_audit(tmp_path):
    led, key = seeded(tmp_path)
    try:
        led.db.execute('BEGIN IMMEDIATE')
        snapshot = loss.capture_tx(led.db, 1, 0, source_key=key)
        loss.record_tx(led.db, snapshot)
        pending = loss.read(led.db, 1)
        assert not pending['complete']
        assert pending['coverage']['reasons'] == {'retained_attachment_requires_audit': 1}
        verified = {**snapshot, 'records': [], 'gaps': [], 'observed': [snapshot['gaps'][0]], 'audited': True}
        loss.record_tx(led.db, verified, audited_source=True)
        assert loss.read(led.db, 1)['complete']
    finally:
        led.close()


@pytest.mark.parametrize('state', ['downloaded', 'pruned'])
def test_model_revision_change_invalidates_old_original_without_erasing_it(tmp_path, monkeypatch, state):
    led, key = seeded(tmp_path)
    try:
        led.db.execute("UPDATE attachments SET state=? WHERE attachment_id=1", (state,))
        assert originals.source(led.db, 1, key) is not None
        before = led.db.execute("SELECT content FROM artifacts WHERE kind=?", (attachment_ocr.KIND,)).fetchone()[0]
        monkeypatch.setattr(attachment_ocr, 'RUNTIME_CONTRACT', attachment_ocr.RUNTIME_CONTRACT + '-new-revision')
        assert originals.source(led.db, 1, key) is None
        assert attachment_ocr.current(led.db, 1) is None
        assert led.db.execute("SELECT content FROM artifacts WHERE kind=?", (attachment_ocr.KIND,)).fetchone()[0] == before
        assert led.db.execute("SELECT state FROM attachments WHERE attachment_id=1").fetchone()[0] == state
    finally:
        led.close()
