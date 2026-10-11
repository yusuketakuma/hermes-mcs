"""Synthetic regressions: an undated reply keeps its parent in a since-bounded history walk."""
from datetime import datetime

import mcs_adapter

OLD = "2026-09-01T09:00:00+09:00"
SINCE = int(datetime.fromisoformat("2026-09-10T09:00:00+09:00").timestamp())


def _adapter(reply_created_at):
    parent = {"id": 700001, "comment": "synthetic parent", "created_at": OLD,
              "user": {"id": 700101, "name": "synthetic author"},
              "thread_messages": [{"id": 700002, "comment": "synthetic reply",
                                   "created_at": reply_created_at,
                                   "user": {"id": 700102, "name": "synthetic author"}}]}

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, **kwargs):
            return {"messages": [parent], "paginate": {"has_next": False}}
    return Adapter()


def test_undated_reply_keeps_parent_instead_of_failing_the_walk():
    batch = _adapter(None).fetch_history(700, SINCE)
    assert batch.error is None and batch.reached
    assert [m.message_id for m in batch.messages] == [700001]


def test_malformed_reply_date_still_fails_closed():
    batch = _adapter("not-a-date").fetch_history(700, SINCE)
    assert isinstance(batch.error, mcs_adapter.SchemaError)
    assert batch.messages == []


def test_old_dated_reply_still_skips_the_old_parent():
    batch = _adapter(OLD).fetch_history(700, SINCE)
    assert batch.error is None and batch.messages == []
