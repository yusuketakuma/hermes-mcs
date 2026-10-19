"""Synthetic borrowed read/transaction boundaries after notification owner migration."""
from copy import deepcopy

import attachment_followups
import notify_scope
from notify_testkit import CFG, NOW, _patient, led as led


def test_notification_health_reads_preserve_borrowed_transaction_and_inputs(led):
    cfg = deepcopy(CFG)
    before = led.db.total_changes
    led.db.execute("BEGIN IMMEDIATE")
    try:
        led.db.execute("SAVEPOINT notification_read_owner")
        health = notify_scope.health_cards(led.db, cfg)
        pending = notify_scope.pending_breakdown(led.db, NOW)
        assert health["attempts_unsettled"] == 0
        assert health["layout_inventory"] == []
        assert pending == {"total": 0, "oldest_age_s": None,
                           "by_reason": {}, "attention": 0}
        assert led.db.total_changes == before and led.db.in_transaction
        assert cfg == CFG
        led.db.execute("ROLLBACK TO notification_read_owner")
        led.db.execute("RELEASE notification_read_owner")
    finally:
        led.db.rollback()
    assert led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 0


def test_attachment_followup_apis_borrow_outer_transaction_and_deduplicate(led):
    with led.db:
        _patient(led)
    led.db.execute("BEGIN IMMEDIATE")
    try:
        led.db.execute("SAVEPOINT attachment_followup_owner")
        assert attachment_followups.enqueue_attachment_followup_tx(
            led.db, 991, 1, 123, NOW) is True
        assert attachment_followups.enqueue_attachment_followup_tx(
            led.db, 991, 1, 123, NOW) is False
        assert attachment_followups.enqueue_ready_attachment_followups_tx(
            led.db, 999, NOW) == 0
        assert attachment_followups.enqueue_downloaded_attachment_followup_tx(
            led.db, 992, 1, 123, NOW) is None
        assert led.db.in_transaction
        assert led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 1
        led.db.execute("ROLLBACK TO attachment_followup_owner")
        assert led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 0
        led.db.execute("RELEASE attachment_followup_owner")
    finally:
        led.db.rollback()
    assert led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 0
