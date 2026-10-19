"""A body edit never re-binds metadata observed on the earlier body; synthetic only."""
import pytest

from ledger import Ledger, publish_snapshot
from mcs_payload import normalize_message as _norm_message
from mcs_signals import record_station_staff
from mcs_view import View
from response_observation_view import get_response_observation_list

NOW = 1_800_000_000
SELF = 11
MENTION = [{"type": "user", "user": {"id": SELF}}]


@pytest.fixture
def store(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr("ledger.time.time", lambda: clock[0])
    led = Ledger(str(tmp_path / "synthetic.db"))
    led.ensure_patient(1)
    with led.db:
        record_station_staff(led.db, [{"staff_id": SELF, "is_self": True}])
    yield led, clock, tmp_path
    led.close()


def _capture(store, at, body, **fields):
    led, clock, tmp_path = store
    clock[0] = at
    m = _norm_message({"id": 1, "comment": body, "user": {"id": 22},
                       "created_at": "2027-01-15T17:00:00+00:00", **fields}, 1)
    led.save_messages([m], project_id=1, notify=False)
    with led.db:
        led.db.execute("UPDATE patients SET fetch_state='complete'")
    clock[0] = at + 1
    path = publish_snapshot(str(tmp_path / "synthetic.db"), str(tmp_path / str(at)))
    view = View(path)
    try:
        result = get_response_observation_list(view.db, enabled=True, max_age_s=300)
        return {**result["counts"], "reasons": result["unknown_reasons"]}
    finally:
        view.close()


def test_edit_without_mentions_stays_unknown_until_they_are_recaptured(store):
    counts = _capture(store, NOW, "synthetic original", mentions=MENTION, reactions=[])
    assert counts["self_target"] == 1
    for at in (NOW + 10, NOW + 20):
        counts = _capture(store, at, "synthetic edited", reactions=[])
        assert counts["self_target"] == 0 and counts["unknown_target"] == 1
    counts = _capture(store, NOW + 30, "synthetic edited", mentions=MENTION, reactions=[])
    assert counts["self_target"] == 1 and counts["unknown_target"] == 0


def test_edit_capture_without_any_metadata_does_not_keep_old_mentions(store):
    assert _capture(store, NOW, "synthetic original", mentions=MENTION,
                    reactions=[])["self_target"] == 1
    counts = _capture(store, NOW + 10, "synthetic edited")
    assert counts["self_target"] == 0 and counts["unknown_target"] == 1
    counts = _capture(store, NOW + 20, "synthetic edited", reactions=[])
    assert counts["self_target"] == 0 and counts["unknown_target"] == 1


def test_unedited_partial_capture_keeps_the_observed_mentions(store):
    assert _capture(store, NOW, "synthetic body", mentions=MENTION,
                    reactions=[])["self_target"] == 1
    assert _capture(store, NOW + 10, "synthetic body", reactions=[])["self_target"] == 1


@pytest.mark.parametrize("recaptured,expected", [
    (MENTION, "self_target"),
    ([{"type": "user", "user": {"id": 99}}], "other_target"),
])
def test_edit_without_metadata_is_unknown_until_mentions_are_recaptured(
        store, recaptured, expected):
    assert _capture(store, NOW, "synthetic original", mentions=MENTION,
                    reactions=[])["self_target"] == 1
    # the discarded pre-edit mention is unobserved, not an unbound value
    for at, fields in ((NOW + 10, {}), (NOW + 20, {"reactions": []})):
        counts = _capture(store, at, "synthetic edited", **fields)
        assert counts["self_target"] == 0 and counts["unknown_target"] == 1
        assert counts["reasons"] == {"mentions_unknown": 1}
    counts = _capture(store, NOW + 30, "synthetic edited", mentions=recaptured,
                      reactions=[])
    assert counts[expected] == 1 and counts["unknown_target"] == 0
