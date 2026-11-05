"""Retained raw originals require an exact occurrence audit after temporary withdrawal."""
import inference_dependency_loss as loss
import original_source_keys
from test_ocr_original_sources import seeded


def test_withdraw_return_requires_a_new_audit_of_the_same_raw_occurrence(tmp_path):
    ledger, key = seeded(tmp_path)
    try:
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            prior = loss.capture_tx(ledger.db, 1, 0, source_key=key)
            ledger.db.execute("UPDATE attachments SET state='withdrawn' WHERE attachment_id=1")
            loss.record_tx(ledger.db, prior)
        assert loss.read(ledger.db, 1)['coverage']['terminal'] > 0
        with ledger.db:
            ledger.db.execute("UPDATE attachments SET state='downloaded',downloaded_at=2 WHERE attachment_id=1")
        proof = loss.read(ledger.db, 1)
        assert not proof['complete'] and proof['coverage']['repairable'] == 1
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            # The audit writer attests this exact retained occurrence; it does not restore old approval.
            audited = {**prior, 'gaps': [], 'observed': prior['gaps'], 'audited': True}
            loss.record_tx(ledger.db, audited, audited_source=True)
        assert loss.read(ledger.db, 1)['complete']
    finally:
        ledger.close()


def test_other_occurrence_audit_cannot_resolve_an_old_binding_gap(tmp_path):
    ledger, key = seeded(tmp_path)
    try:
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            prior = loss.capture_tx(ledger.db, 1, 0, source_key=key)
            old = {**prior['gaps'][0], 'binding': 'f' * 64}
            loss.record_tx(ledger.db, {**prior, 'gaps': [old]})
            loss.record_tx(ledger.db, {**prior, 'gaps': [], 'observed': prior['gaps'], 'audited': True}, audited_source=True)
        assert original_source_keys.source(ledger.db, 1, key)
        proof = loss.read(ledger.db, 1)
        assert not proof['complete']
        assert proof['coverage']['terminal'] == 1 and proof['coverage']['repairable'] == 0
    finally:
        ledger.close()
