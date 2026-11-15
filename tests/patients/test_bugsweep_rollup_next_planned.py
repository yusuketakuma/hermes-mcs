"""A rollup's next visit is neither a past date nor a plan a later post cancelled (synthetic only)."""
from datetime import datetime

import pytest

import rollup
from mcs_queries import JST
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of


def _post(store, mid, day, body, planned=None):
    store.save_messages([_message(mid, body=body)], project_id=1)
    store.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?",
                     (datetime.fromisoformat(day).replace(hour=9, tzinfo=JST).timestamp(), mid))
    _artifact(store, "extract_v1", mid, {"next_planned": planned} if planned else {},
              {"hash": _hash_of(store, mid)})
    store.db.commit()


@pytest.fixture
def store(tmp_path):
    db = _db(tmp_path, ())
    yield db
    db.close()


def test_past_planned_visit_is_not_the_next_visit(store):
    _post(store, 1, "2020-09-01", "次回訪問は9/15です", "2020-09-15")
    assert "next_planned" not in rollup.build_rollup(store.db, 1)


def test_later_cancellation_clears_the_planned_visit(store):
    _post(store, 1, "2099-09-01", "次回訪問は9/15です", "2099-09-15")
    assert rollup.build_rollup(store.db, 1)["next_planned"] == "2099-09-15"
    _post(store, 2, "2099-09-14", "9/15の訪問はキャンセルになりました")
    assert "next_planned" not in rollup.build_rollup(store.db, 1)


def test_newer_plan_after_a_cancellation_is_kept(store):
    _post(store, 1, "2099-09-01", "次回訪問は9/15です", "2099-09-15")
    _post(store, 2, "2099-09-14", "9/15の訪問は中止です。次回は9/22です", "2099-09-22")
    assert rollup.build_rollup(store.db, 1)["next_planned"] == "2099-09-22"
