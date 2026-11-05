"""A single OCR source lookup never walks unrelated attachments."""
import pytest

import attachment_extraction
import attachment_ocr
import mcs_queries
from test_ocr_original_sources import seeded


@pytest.mark.parametrize('hydrate', [True, False])
def test_external_source_lookup_checks_only_the_requested_attachment(tmp_path, monkeypatch, hydrate):
    ledger, key = seeded(tmp_path)
    try:
        with ledger.db:
            ledger.db.executemany(
                "INSERT INTO attachments(attachment_id,message_id,state,name) VALUES(?,10,'downloaded','synthetic.pdf')",
                [(number,) for number in range(100, 200)])
        calls = []
        current = attachment_ocr.current

        def observed(db, attachment_id, **kw):
            calls.append(attachment_id)
            return current(db, attachment_id, **kw)

        monkeypatch.setattr(attachment_ocr, 'current', observed)
        monkeypatch.setattr(attachment_extraction, 'current', lambda *a, **kw: None)
        ledger.db.execute('PRAGMA query_only=ON')
        before = ledger.db.total_changes
        [source] = mcs_queries.attachment_ocr_sources(ledger.db, 1, mid=key, hydrate=hydrate)
        assert source['source_key'] == key
        assert calls == [1]
        assert ('body_text' in source) is hydrate
        assert ledger.db.total_changes == before
        assert list(mcs_queries.attachment_ocr_sources(ledger.db, 2, mid=key)) == []
    finally:
        ledger.close()


@pytest.mark.parametrize('key', ['karte:1', 'attachment_ocr:0:1', 'attachment_ocr:1:31', 'invalid'])
def test_other_or_invalid_external_keys_never_scan_ocr(tmp_path, monkeypatch, key):
    ledger, _ = seeded(tmp_path)
    try:
        def forbidden(*args, **kwargs):
            pytest.fail('unrelated source triggered OCR scan')
        monkeypatch.setattr(attachment_ocr, 'current', forbidden)
        assert list(mcs_queries.attachment_ocr_sources(ledger.db, 1, mid=key)) == []
    finally:
        ledger.close()
