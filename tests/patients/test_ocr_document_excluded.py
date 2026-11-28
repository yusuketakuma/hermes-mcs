"""OCR page readers share the core excluded/unknown distinction without changing rows."""
import pytest

import attachment_ocr
import mcs_queries
import ocr_document_reader
from test_ocr_original_sources import seeded

CFG = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/yomitoku'}}


@pytest.mark.parametrize('state', ['unsupported', 'failed', 'unprocessed', 'unavailable', 'deferred'])
def test_shared_reader_excludes_confirmed_unavailable_attachments(tmp_path, monkeypatch, state):
    ledger, _ = seeded(tmp_path)
    try:
        monkeypatch.setattr(attachment_ocr, 'current', lambda *args, **kwargs: None)
        monkeypatch.setattr(attachment_ocr, 'view', lambda *args, **kwargs: {'state': state})
        before = ledger.db.total_changes
        ledger.db.execute('PRAGMA query_only=ON')
        reader = ocr_document_reader.read(ledger.db, 1, cfg=CFG)['coverage']
        core = mcs_queries.attachment_ocr_coverage(ledger.db, 1, CFG)
        excluded = state in attachment_ocr.EXCLUDED_STATES
        assert reader['excluded'] == core['excluded'] == int(excluded)
        assert reader['pending'] == core['pending'] == int(not excluded)
        assert reader['unavailable'] == core.get('unavailable', 0) == int(state == 'unavailable')
        assert reader['complete'] == core['complete'] == excluded
        assert not reader['limited'] and ledger.db.total_changes == before
    finally:
        ledger.close()
