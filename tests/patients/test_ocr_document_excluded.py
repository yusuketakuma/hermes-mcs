"""OCR page readers share the core excluded/unknown distinction without changing rows."""
import pytest

import attachment_ocr
import mcs_queries
import ocr_document_reader
from test_ocr_original_sources import seeded

CFG = {'attachment_ocr': {'enabled': True, 'executable': '/synthetic/yomitoku'}}


@pytest.mark.parametrize('state', ['unsupported', 'failed', 'unprocessed', 'unavailable', 'deferred'])
def test_shared_reader_only_excludes_explicitly_unsupported_attachments(tmp_path, monkeypatch, state):
    ledger, _ = seeded(tmp_path)
    try:
        monkeypatch.setattr(attachment_ocr, 'current', lambda *args, **kwargs: None)
        monkeypatch.setattr(attachment_ocr, 'view', lambda *args, **kwargs: {'state': state})
        before = ledger.db.total_changes
        ledger.db.execute('PRAGMA query_only=ON')
        reader = ocr_document_reader.read(ledger.db, 1, cfg=CFG)['coverage']
        core = mcs_queries.attachment_ocr_coverage(ledger.db, 1, CFG)
        assert reader['excluded'] == core['excluded'] == int(state == 'unsupported')
        assert reader['pending'] == core['pending'] == int(state != 'unsupported')
        assert reader['complete'] == core['complete'] == (state == 'unsupported')
        assert not reader['limited'] and ledger.db.total_changes == before
    finally:
        ledger.close()
