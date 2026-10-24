"""View evidence: missing messages fail alike on every route, and the per-view
self-identity reuse matches the per-call reads (synthetic snapshots only)."""
import dataclasses

import pytest

from ledger import Ledger, publish_snapshot
from mcs_view import View
from message_metadata import flag_lines, is_self_sender
from message_observations import get_message_metadata
from self_identity import is_own_station_message, record_self_profile, record_station_staff
from views_testkit import _message


@pytest.fixture
def store(tmp_path):
    ledger = Ledger(str(tmp_path / "synthetic.db"))
    ledger.ensure_patient(1)
    ledger.save_messages([dataclasses.replace(_message(1), sender_id=22),
                          dataclasses.replace(_message(2), sender_id=33)], project_id=1)
    yield ledger
    ledger.close()


def _view(store, tmp_path):
    source = store.db.execute("PRAGMA database_list").fetchone()["file"]
    return View(publish_snapshot(source, str(tmp_path / "snapshots")))


@pytest.mark.parametrize("kind", ["evidence", "thread", "attachments", "semantic", "comparison"])
def test_every_message_route_reports_a_missing_message_alike(store, tmp_path, kind):
    view = _view(store, tmp_path)
    try:
        with pytest.raises(ValueError, match="^message_not_found$"):
            view.read(kind, project=1, message_id=999)
        with pytest.raises(ValueError, match="^bad_message_id$"):
            view.read(kind, project=1, message_id=0)
    finally:
        view.close()


@pytest.mark.parametrize("identity", ["roster", "profile_only", "ambiguous"])
def test_reused_self_identity_matches_the_per_call_reads(store, tmp_path, identity):
    with store.db:
        if identity == "roster":
            record_station_staff(store.db, [{"staff_id": 22, "is_self": True}, {"staff_id": 44}])
        elif identity == "profile_only":
            record_self_profile(store.db, {"sender_id": 22, "name": "synthetic"})
        else:
            record_station_staff(store.db, [{"staff_id": 22, "is_self": True},
                                            {"staff_id": 33, "is_self": True}])
    view = _view(store, tmp_path)
    try:
        at = view.meta["generated_at"]
        for mid, sender in ((1, 22), (2, 33)):
            row = view.read("evidence", project=1, message_id=mid)["message"]
            assert row["is_self_sender"] == is_self_sender(view.db, sender)
            assert row["is_own_station_sender"] == is_own_station_message(view.db, sender)
            expected = get_message_metadata(view.db, mid, as_of=at)
            assert row["message_metadata"] == expected
            assert row["metadata_flags"] == flag_lines(view.db, expected)
        assert view._identity()[0] == (22 if identity != "ambiguous" else None)
    finally:
        view.close()
