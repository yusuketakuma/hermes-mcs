"""The SQL delivery-key match keeps the old per-row Python semantics."""
import pytest

from ledger import Ledger
import semantic_render as render


@pytest.fixture
def ledger(tmp_path):
    result = Ledger(str(tmp_path / "synthetic.db"))
    yield result
    result.close()


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
    assert not render._outbox_has_delivery(ledger, key)


@pytest.mark.parametrize("state", ["pending", "failed", "accepted",
                                   "suppressed"])
def test_every_counted_state_matches(ledger, state):
    _raw(ledger, "{broken", state)
    _raw(ledger, '{"delivery_key": "SYNTH-k", "root_id": 1}', state)
    assert render._outbox_has_delivery(ledger, "SYNTH-k")
    assert not render._outbox_has_delivery(ledger, "SYNTH-other")
