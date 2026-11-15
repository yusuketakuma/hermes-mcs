"""Unavailable patient scopes never enter OCR, local or cloud processing."""
import hashlib

import pytest

import attachment_ocr
from test_attachment_ocr import setup as _raw_setup
from test_attachment_inference import page_store as _page_store
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
