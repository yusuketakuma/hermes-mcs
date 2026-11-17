"""The SQL delivery-key match keeps the old per-row Python semantics."""
import semantic_notice
import pytest

from common_testkit import ledger_fixture


ledger = ledger_fixture(name="ledger")


def _raw(ledger, payload: str, state: str = "pending",
         kind: str = "semantic_notice") -> None:
    eid = ledger.outbox_add(kind, 1, {})
    ledger.db.execute("UPDATE notify_outbox SET payload=?, state=? "
                      "WHERE event_id=?", (payload, state, eid))
    ledger.db.commit()


@pytest.mark.parametrize("payload, state, kind", [
    ('[{"delivery_key": "SYNTH-k"}]', "pending", "semantic_notice"),
    ('{"delivery_key": 5}', "pending", "semantic_notice"),
    ('{"other": "SYNTH-k"}', "pending", "semantic_notice"),
    ("not json SYNTH-k", "pending", "semantic_notice"),
    ('{"delivery_key": "SYNTH-k"}', "in_flight", "semantic_notice"),
    ('{"delivery_key": "SYNTH-k"}', "pending", "new_messages"),
])
def test_rows_that_never_matched_still_do_not_match(ledger, payload, state,
                                                    kind):
    _raw(ledger, payload, state, kind)
    key = "5" if '": 5' in payload else "SYNTH-k"
    assert not semantic_notice.outbox_has_delivery(ledger.db, key)


@pytest.mark.parametrize("state", ["pending", "failed", "accepted",
                                   "suppressed"])
def test_every_counted_state_matches(ledger, state):
    _raw(ledger, "{broken", state)
    _raw(ledger, '{"delivery_key": "SYNTH-k", "root_id": 1}', state)
    assert semantic_notice.outbox_has_delivery(ledger.db, "SYNTH-k")
    assert not semantic_notice.outbox_has_delivery(ledger.db, "SYNTH-other")


def test_suppressed_notice_of_another_summary_does_not_hide_the_rebuilt_one(ledger):
    _raw(ledger, '{"delivery_key": "SYNTH-k", "summary_artifact_id": 7}', "suppressed")
    assert semantic_notice.outbox_has_delivery(ledger.db, "SYNTH-k")
    assert semantic_notice.outbox_has_delivery(ledger.db, "SYNTH-k", 7)
    assert not semantic_notice.outbox_has_delivery(ledger.db, "SYNTH-k", 8)
    _raw(ledger, '{"delivery_key": "SYNTH-k", "summary_artifact_id": 7}', "pending")
    assert semantic_notice.outbox_has_delivery(ledger.db, "SYNTH-k", 8)
