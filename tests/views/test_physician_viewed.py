"""Care-team physicians x complete 見ました reactor set, counts only (synthetic)."""
import pytest

from ledger import Ledger
from project_metadata_view import physician_viewed_status
from test_project_metadata_view import capture


def _member(mid, profession="医師"):
    return {"id": mid, "type": "medical", "is_director": False, "is_self": False,
            "specialist_categories": [{"name": profession}], "station": None,
            "last_name": "NAME_CANARY", "first_name": "FIRST_CANARY"}


@pytest.fixture
def store(tmp_path):
    db = Ledger(str(tmp_path / "synthetic.db"))
    db.ensure_patient(1)
    yield db
    db.close()


def _status(store, now=150):
    return physician_viewed_status(store.db, 1, 900, now=now)


def test_counts_unobserved_physicians_without_names(store):
    capture(store, rows=[_member(5), _member(6), _member(7), _member(8, "看護師")], now=100)
    store.save_reaction_actors(900, [
        {"actor_id": 5, "reaction_type": "viewed", "profession": "医師"},
        {"actor_id": 6, "reaction_type": "accepted", "profession": "医師"},
        {"actor_id": 8, "reaction_type": "viewed", "profession": "看護師"},
        # Same ID but a different recorded profession is not counted as observed.
        {"actor_id": 7, "reaction_type": "viewed", "profession": "看護師"}], True, now=120)
    out = _status(store)
    assert out == {"state": "known", "reason": None, "physicians": 3,
                   "viewed_observed": 1, "not_observed": 2}
    assert "CANARY" not in repr(out)


def test_unknown_without_current_roster_or_complete_reactors(store):
    assert _status(store)["reason"] == "care_team_not_current"
    capture(store, rows=[_member(5)], now=100)
    assert _status(store)["reason"] == "reactors_not_current"
    store.save_reaction_actors(900, [], False, error="http_error", now=120)
    assert _status(store)["reason"] == "reactors_not_current"
    store.save_reaction_actors(900, [], True, now=130)
    assert _status(store)["state"] == "known"
    # An aged roster is history, never current evidence.
    assert _status(store, now=100 + 86401)["reason"] == "care_team_not_current"


def test_empty_roster_is_known_zero_not_unknown(store):
    capture(store, rows=[], now=100)
    store.save_reaction_actors(900, [], True, now=120)
    assert _status(store) == {"state": "known", "reason": None, "physicians": 0,
                              "viewed_observed": 0, "not_observed": 0}


def test_member_with_unknown_profession_keeps_the_count_unknown(store):
    member = _member(9)
    member["specialist_categories"] = None
    capture(store, rows=[_member(5), member], now=100)
    store.save_reaction_actors(900, [], True, now=120)
    assert _status(store)["reason"] == "care_team_profession_unknown"
    assert _status(store)["state"] == "unknown"
