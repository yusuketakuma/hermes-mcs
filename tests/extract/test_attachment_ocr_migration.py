"""Legacy table text is re-read from retained originals without erasing paid evidence."""
import json

import pytest

import attachment_inference
import attachment_ocr as ocr
import attachment_paid_results as journal
from test_attachment_ocr import setup as setup
from test_attachment_inference import page_store as page_store, run
from test_attachment_inference_recovery import _lose_accounting_lock
from test_cloud_after_local import local_then_cloud  # noqa: F401
from test_anthropic_runtime import routed  # noqa: F401

LEGACY = 'yomitoku-0.16.0-cpu-lite-text-first/v1'


def test_table_span_has_a_memory_bound_before_expansion(monkeypatch):
    monkeypatch.setattr(ocr, 'MAX_TABLE_ENTRIES', 2)
    with pytest.raises(ValueError, match='output_schema'):
        ocr._document_text({'paragraphs': [], 'tables': [{'cells': [
            {'row': 0, 'col': 0, 'row_span': 3, 'contents': ''}]}]})


def _legacy(db):
    with db:
        db.execute("UPDATE artifacts SET content=json_set(content,'$.runtime_contract',?) WHERE kind=?",
                   (LEGACY, ocr.KIND))


def test_legacy_table_source_is_reprocessed_without_overwriting_history(setup):
    ledger, cfg, _, calls, _ = setup
    assert ocr.run_pending(ledger, cfg)['complete'] == 1
    _legacy(ledger.db)
    before = tuple(ledger.db.execute('SELECT * FROM artifacts WHERE kind=?', (ocr.KIND,)).fetchone())
    assert ocr.current(ledger.db, 1) is None
    assert ocr.run_pending(ledger, cfg)['complete'] == 1
    assert len(calls) == 2
    rows = ledger.db.execute('SELECT * FROM artifacts WHERE kind=? ORDER BY artifact_id', (ocr.KIND,)).fetchall()
    assert len(rows) == 2 and tuple(rows[0]) == before
    assert ocr.current(ledger.db, 1)['artifact_id'] == rows[-1]['artifact_id']


def test_pruned_legacy_source_stays_unavailable_without_reprocessing(setup):
    ledger, cfg, _, calls, path = setup
    assert ocr.run_pending(ledger, cfg)['complete'] == 1
    _legacy(ledger.db)
    path.unlink()
    with ledger.db:
        ledger.db.execute("UPDATE attachments SET state='pruned'")
    assert ocr.current(ledger.db, 1) is None
    assert ocr.view(ledger.db, 1)['state'] == 'unavailable'
    assert ocr.run_pending(ledger, cfg)['complete'] == 0
    assert len(calls) == 1


def test_legacy_source_paid_journal_settles_without_adoption_or_resend(page_store, monkeypatch):
    ledger, cfg, key, _, sends, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    unavailable = _lose_accounting_lock(ledger, sends, monkeypatch)
    assert run(ledger, cfg, 'cloud')['deferred'] == 1
    assert len(sends) == 1 and len(journal.pending(ocr._data_root(ledger))) == 1
    _legacy(ledger.db)
    unavailable[0] = False
    assert run(ledger, cfg, 'cloud')['done'] == 0
    assert len(sends) == len(ledger.artifacts('cloud_usage_result')) == 1
    disposition, = ledger.artifacts('cloud_usage_disposition')
    assert json.loads(disposition['meta'])['disposition'] == 'discarded_scope'
    assert not journal.pending(ocr._data_root(ledger))
    assert attachment_inference.current(ledger.db, 1, key, cfg=cfg) is None
