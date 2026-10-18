"""Full patient request projections retain old rows while saved rollups stay bounded."""
from types import SimpleNamespace

import pytest

import rollup
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of
from test_patient_context_v3_views import _reply, T0, DAY


@pytest.mark.parametrize("old_reply", [None, "done"])
def test_explicit_full_request_read_keeps_old_request_and_its_reply(tmp_path, old_reply):
    store = _db(tmp_path, ())
    try:
        # The newest 15 requests were answered. The first request must still be
        # present, and its reply must be checked even outside the ordinary cap.
        for mid in range(1, 17):
            store.save_messages([_message(mid, body=f"架空依頼{mid}の確認をお願いします。")], project_id=1)
            store.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?", (T0 + mid * DAY, mid))
            _artifact(store, "extract_llm", mid,
                      {"requests": [{"to": "pharmacy", "action": f"架空依頼{mid}"}]},
                      {"hash": _hash_of(store, mid)})
            if mid > 1 or old_reply:
                _reply(SimpleNamespace(db=store), mid + 100, mid, old_reply if mid == 1 else "done")
        before = store.db.total_changes
        ordinary = rollup.build_rollup(store, 1)["recent_requests"]
        full = rollup.build_rollup(store, 1, request_limit=None)["recent_requests"]
        assert [r["mid"] for r in ordinary] == list(range(16, 1, -1))
        assert [r["mid"] for r in full] == list(range(16, 0, -1))
        assert all(r["reply_state"] == "done" for r in full[:-1])
        assert full[-1].get("reply_state") == old_reply
        assert full[:-1] == ordinary
        assert store.db.total_changes == before
    finally:
        store.close()
