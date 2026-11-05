"""Synthetic source mutations retain the old OCR dependency proof in the same transaction."""
import json

import pytest

import attachment_ocr as ocr
import inference_dependency_loss as loss
import original_source_keys as originals
from semantic_testkit import _message
from test_ocr_original_sources import seeded


def _assert_lost(led, key, before):
    assert originals.binding(led.db, 1, key) != before
    proof = loss.read(led.db, 1)
    assert not proof['complete']
    stored = [json.loads(row[0]) for row in led.db.execute(
        'SELECT content FROM artifacts WHERE kind=?', (loss.KIND,))]
    assert key in {item['origin']['source_key'] for item in stored}
    assert proof['coverage']['reasons']['historical_original_body_unavailable'] >= 1
    assert 'invalid_dependency_proof' not in proof['coverage']['reasons']


@pytest.mark.parametrize('mutation', ['delete', 'withdraw', 'download', 'failure'])
def test_ledger_mutation_captures_ocr_before_old_binding_disappears(tmp_path, mutation):
    led, key = seeded(tmp_path)
    try:
        before = originals.binding(led.db, 1, key)
        if mutation == 'download':
            led.attachment_saved(1, '/synthetic/unused', 33, 'b' * 64)
        elif mutation == 'failure':
            led.attachment_failed(1, 'http_404')
        else:
            message = _message(10, body='変更した合成本文' if mutation == 'edit' else '合成チャット本文',
                               parent=11 if mutation == 'reparent' else None)
            if mutation == 'delete':
                message.body_state = 'deleted'
            if mutation == 'withdraw':
                message.files_present = True
            led.save_messages([message], project_id=1)
        _assert_lost(led, key, before)
    finally:
        led.close()


@pytest.mark.parametrize('mutation', ['unchanged', 'prune', 'edit', 'reparent', 'same_download'])
def test_same_raw_original_does_not_lose_ocr(tmp_path, mutation):
    led, key = seeded(tmp_path)
    try:
        before = originals.binding(led.db, 1, key)
        if mutation == 'same_download':
            led.attachment_saved(1, '/synthetic/unused', 32, 'a' * 64)
        else:
            led.save_messages([_message(10, body='変更した合成本文' if mutation == 'edit' else '合成チャット本文',
                                       parent=11 if mutation == 'reparent' else None)], project_id=1)
            if mutation == 'prune':
                with led.db:
                    led.db.execute("UPDATE attachments SET state='pruned' WHERE attachment_id=1")
        assert originals.binding(led.db, 1, key) == before
        assert not led.db.execute("SELECT 1 FROM artifacts WHERE kind=? AND "
            "json_extract(content,'$.origin.source_key')=?", (loss.KIND, key)).fetchone()
    finally:
        led.close()


def test_ocr_replacement_keeps_old_dependency_and_rolls_back_atomically(tmp_path, monkeypatch):
    led, key = seeded(tmp_path)
    try:
        before = originals.binding(led.db, 1, key)
        previous = ocr.current(led.db, 1)
        replacement = {k: v for k, v in previous.items() if k not in ('artifact_id', 'project_id', 'message_id')}
        replacement.update(state='failed', pages=[])
        # Existing valid completions are normally not due; exercise the replacement boundary itself.
        monkeypatch.setattr(ocr, '_due', lambda *args: True)
        monkeypatch.setattr(ocr.writer_lock, 'updater_state', lambda: (False, 1))
        source = ocr._source(led.db, 1)
        with monkeypatch.context() as patch:
            def fail_record(*args):
                raise RuntimeError('synthetic record failure')
            patch.setattr(ocr, 'record_dependencies_tx', fail_record)
            with pytest.raises(RuntimeError, match='synthetic record failure'):
                ocr._settle(led, source, replacement, tmp_path, (False, 1))
        assert originals.binding(led.db, 1, key) == before
        assert ocr._settle(led, source, replacement, tmp_path, (False, 1))
        _assert_lost(led, key, before)
        assert json.loads(led.db.execute('SELECT content FROM artifacts WHERE kind=? ORDER BY artifact_id DESC',
                                       (ocr.KIND,)).fetchone()[0])['state'] == 'failed'
    finally:
        led.close()
