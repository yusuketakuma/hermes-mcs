"""Stored OCR cloud results keep an input binding that a later history approval cannot change."""
import time


import attachment_cloud_identity
import attachment_extraction
import attachment_inference as worker
import cloud_policy
from test_attachment_inference import page_store, run, routed, local_then_cloud

__all__ = ['page_store', 'routed', 'local_then_cloud']


def test_cloud_input_binding_is_independent_of_approval_generation(page_store):
    ledger, cfg, key, *_ = page_store
    assert run(ledger, cfg)['done'] == 1
    source = worker.original_source_keys.source(ledger.db, 1, key)
    before = attachment_extraction.history_snapshot(ledger.db, source, require_ready=True)
    assert before
    first_binding = attachment_cloud_identity.input_binding(source)
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-new-approved-scope', time.time())
    after = attachment_extraction.history_snapshot(ledger.db, source, require_ready=True)
    assert after and after != before
    assert attachment_cloud_identity.input_binding(source) == first_binding


