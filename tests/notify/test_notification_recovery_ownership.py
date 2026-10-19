"""Synthetic recovery boundaries: borrowed settlement and durable human receipts."""
from copy import deepcopy
import json

import pytest

import notify_spool
import notify_recovery
import notify_scope
import notify_transport
from notify_testkit import (CFG, NOW, SCOPE, _begin, _dispatch, _intent,
                            _latest_render, _receipt, _seed_thread, _uuid,
                            led as led)


def _stored_state(db):
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
            for table in ("notification_delivery_attempts", "notification_cards",
                          "notification_renders", "notification_render_parts",
                          "notification_intent_cards", "notify_outbox")}


@pytest.mark.parametrize("result", ["delivered", "not_sent", "unknown"])
def test_attempt_settlement_borrows_transaction_and_preserves_inputs(led, result):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = dict(_latest_render(led))
    grant = _begin(led, render)
    assert grant["granted"]
    attempt = dict(led.db.execute(
        "SELECT * FROM notification_delivery_attempts WHERE attempt_id=?",
        (grant["attempt_id"],)).fetchone())
    original_render, original_attempt = deepcopy(render), deepcopy(attempt)
    before = _stored_state(led.db)
    led.db.execute("BEGIN IMMEDIATE")
    try:
        led.db.execute("SAVEPOINT borrowed_attempt")
        assert notify_transport.settle_attempt_tx(
            led.db, attempt, render, result, NOW + 1,
            message_id="synthetic-post" if result == "delivered" else None,
            error_code=None if result == "delivered" else "synthetic-result",
        ) == {"settled": result}
        assert led.db.in_transaction
        assert led.db.execute(
            "SELECT state FROM notification_delivery_attempts WHERE attempt_id=?",
            (attempt["attempt_id"],)).fetchone()[0] == result
        assert render == original_render and attempt == original_attempt
        led.db.execute("ROLLBACK TO borrowed_attempt")
        assert _stored_state(led.db) == before
        led.db.execute("RELEASE borrowed_attempt")
    finally:
        led.db.rollback()
    assert notify_scope.render_row(led.db, render["delivery_id"]) is not None
    assert notify_scope.render_row(led.db, _uuid(999)) is None


def test_operator_recovery_publishes_only_after_its_receipt_commit(led, monkeypatch):
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    render = _latest_render(led)
    assert _begin(led, render)["granted"]
    assert _receipt(led, render, f"{1:016x}", result="unknown", message_id=None)["applied"]
    req = {"version": 1, "cmd": "ops.card_resolve", "command_id": _uuid(987),
           "actor": "synthetic-operator", "human_confirmed": True,
           "reason": "合成の配送確認", "delivery_id": render["delivery_id"],
           "attempt_id": f"{1:016x}", "result": "mark_not_sent", **SCOPE,
           "evidence": {"method": "synthetic_lookup", "ref": "synthetic-proof",
                        "worker_stopped": True, "proof": "remote_absent"}}
    publish = notify_spool.publish_specs
    observed = []

    def after_commit(db, dirs, specs, now):
        assert not db.in_transaction
        receipt = json.loads(db.execute(
            "SELECT receipt_json FROM command_receipts WHERE command_id=?",
            (req["command_id"],)).fetchone()[0])
        assert receipt["outcome"] == "applied"
        assert receipt["attempt_state"] == "not_sent"
        assert specs
        observed.append(receipt)
        return publish(db, dirs, specs, now)

    monkeypatch.setattr(notify_spool, "publish_specs", after_commit)
    receipt = notify_recovery.apply_card_resolve(led, req, CFG, NOW + 1)
    assert observed == [receipt]
    assert notify_recovery.apply_card_resolve(led, req, CFG, NOW + 2) == receipt
    assert observed == [receipt]
