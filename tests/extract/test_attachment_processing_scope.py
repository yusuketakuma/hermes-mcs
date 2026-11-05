"""Unavailable patient scopes never enter OCR, local or cloud processing."""
import hashlib
import json

import pytest

import attachment_ocr
import attachment_inference as worker
import original_source_keys
from anthropic_client import AnthropicClient
from test_attachment_ocr import setup as _raw_setup
from test_attachment_inference import page_store as _page_store, run
from test_cloud_after_local import local_then_cloud  # noqa: F401
from test_anthropic_runtime import routed  # noqa: F401

raw_store = _raw_setup
page_store = _page_store


def _block(db, pid, cause):
    with db:
        if cause == 'missing':
            db.execute('DELETE FROM patients WHERE project_id=?', (pid,))
        elif cause == 'archived':
            db.execute('UPDATE patients SET is_archived=1 WHERE project_id=?', (pid,))
        elif cause == 'forbidden':
            db.execute("UPDATE patients SET fetch_state='forbidden' WHERE project_id=?", (pid,))
        else:
            db.execute("UPDATE patients SET fetch_reason='access_revoked' WHERE project_id=?", (pid,))


@pytest.mark.parametrize('cause', ['archived', 'forbidden', 'revoked', 'missing'])
def test_scope_denial_means_zero_raw_local_and_cloud_calls(raw_store, page_store, cause):
    raw_ledger, raw_cfg, _, raw_calls, _ = raw_store
    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    _block(raw_ledger.db, 20, cause)
    _block(ledger.db, 1, cause)
    changes = (raw_ledger.db.total_changes, ledger.db.total_changes)
    originals = [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    assert attachment_ocr.run_pending(raw_ledger, raw_cfg)['complete'] == 0
    assert run(ledger, cfg, 'local')['done'] == 0
    cfg['cloud_llm']['mode'] = 'on'
    assert run(ledger, cfg, 'cloud')['done'] == 0
    assert raw_calls == local_calls == cloud_calls == []
    assert original_source_keys.source(ledger.db, 1, key) is None
    assert changes == (raw_ledger.db.total_changes, ledger.db.total_changes)
    assert originals == [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    assert not ledger.artifacts('cloud_usage_reserve') and not ledger.artifacts(worker.KIND)


def test_scope_revoked_during_http_keeps_accounting_and_rejects_clinical_adoption(page_store, monkeypatch):
    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    prior = [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts WHERE kind=?', (worker.KIND,))]
    cfg['cloud_llm']['mode'] = 'on'
    request = AnthropicClient.request

    def revoked(self, method, body, timeout, **kwargs):
        response = request(self, method, body, timeout, **kwargs)
        _block(ledger.db, 1, 'revoked')
        return response

    monkeypatch.setattr(AnthropicClient, 'request', revoked)
    result = run(ledger, cfg, 'cloud')
    assert result['done'] == 0 and result['deferred'] == 1
    assert len(cloud_calls) == len(local_calls) == 1
    assert len(ledger.artifacts('cloud_usage_reserve')) == 1
    receipt, = ledger.artifacts('cloud_usage_result')
    assert json.loads(receipt['meta'])['http_class'] == 'http_200'
    assert json.loads(receipt['meta'])['usage']
    assert [json.loads(row['meta'])['disposition'] for row in ledger.artifacts('cloud_usage_disposition')] == ['discarded_scope']
    assert prior == [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts WHERE kind=?', (worker.KIND,))]
    assert worker.current(ledger.db, 1, key, cfg=cfg) is None


@pytest.mark.parametrize('change', ['off', 'executable', 'timeout', 'unreadable'])
def test_runtime_setting_change_never_adopts_new_raw_text_or_rewrites_history(raw_store, monkeypatch, change):
    ledger, cfg, pages, _, path = raw_store
    assert attachment_ocr.run_pending(ledger, cfg)['complete'] == 1
    prior = [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    blob = b'\x89PNG\r\n\x1a\nnew synthetic image'
    path.write_bytes(blob)
    with ledger.db:
        ledger.db.execute('UPDATE attachments SET sha256=?,bytes=? WHERE attachment_id=1',
                          (hashlib.sha256(blob).hexdigest(), len(blob)))

    def runtime(*args):
        if change == 'off':
            cfg['attachment_ocr']['enabled'] = False
        elif change == 'executable':
            cfg['attachment_ocr']['executable'] = '/synthetic/other/yomitoku'
        elif change == 'timeout':
            cfg['attachment_ocr']['timeout_seconds'] = 300
        else:
            def unreadable():
                raise attachment_ocr.ProviderSettingsUnreadable('synthetic settings unavailable')
            monkeypatch.setattr(attachment_ocr, 'load_config', unreadable)
        return pages

    monkeypatch.setattr(attachment_ocr, '_runtime', runtime)
    result = attachment_ocr.run_pending(ledger, cfg)
    assert result['complete'] == 0 and result['deferred'] == 1
    assert prior == [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    assert attachment_ocr.current(ledger.db, 1, project_id=20) is None


def test_stale_or_unverified_runtime_config_never_scans_or_starts_raw_engine(raw_store, monkeypatch):
    ledger, cfg, _, calls, _ = raw_store
    monkeypatch.setattr(attachment_ocr, 'load_config', lambda: {'attachment_ocr': {'enabled': False}})
    changes = ledger.db.total_changes
    result = attachment_ocr.run_pending(ledger, cfg)
    assert result['deferred'] == 1 and calls == [] and ledger.db.total_changes == changes
    assert not (attachment_ocr._data_root(ledger) / 'attachment_ocr_cursor.json').exists()
