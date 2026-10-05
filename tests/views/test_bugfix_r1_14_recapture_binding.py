"""Unchanged metadata re-captured through save_messages stays bound to the body."""
import pytest

from mcs_adapter import _norm_message
from test_response_observation_view import NOW, SELF, _read, _view, store  # noqa: F401


@pytest.mark.parametrize("captures", [1, 2])
def test_recaptured_unchanged_mentions_stay_self_target(store, tmp_path, captures):  # noqa: F811
    ledger, clock = store
    for i in range(captures):
        clock[0] = NOW - 100 + 60 * i
        m = _norm_message({"id": 1, "comment": "SYNTHETIC-BODY", "user": {"id": 22},
                           "mentions": [{"type": "user", "user": {"id": SELF}}],
                           "reactions": []}, 1)
        ledger.save_messages([m], project_id=1, notify=False)
    with ledger.db:
        ledger.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=1",
                          (NOW - 100,))
    view = _view(store, tmp_path)
    try:
        result = _read(view)
        assert result["counts"]["self_target"] == 1, result
        assert result["counts"]["unknown_target"] == 0
    finally:
        view.close()
