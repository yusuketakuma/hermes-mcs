"""Full patient request projections retain old rows while saved rollups stay bounded."""
from types import SimpleNamespace

import pytest

import patient_context_view as pcv
import rollup
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of
from test_patient_context_v3_views import _reply, T0, DAY


@pytest.mark.parametrize("old_reply", [None, "done"])
@pytest.mark.parametrize("source_kind", ["extract_llm", "extract_v1"])
def test_explicit_full_request_read_keeps_old_request_and_its_reply(tmp_path, old_reply, source_kind):
    store = _db(tmp_path, ())
    try:
        # The newest 15 requests were answered. The first request must still be
        # present, and its reply must be checked even outside the ordinary cap.
        for mid in range(1, 17):
            store.save_messages([_message(mid, body=f"架空依頼{mid}の確認をお願いします。")], project_id=1)
            store.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?", (T0 + mid * DAY, mid))
            request = ({"to": "pharmacy", "action": f"架空依頼{mid}"} if source_kind == "extract_llm"
                       else {"kind": "pharmacy", "ctx": f"架空依頼{mid}"})
            _artifact(store, source_kind, mid, {"requests": [request]},
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
        if source_kind == "extract_llm":
            assert full[:-1] == ordinary
        else:
            # Preserve the saved rollup's historical LLM-only reply scan.
            assert all("reply_state" not in r for r in ordinary)
        # The three patient screens use the full projection, not the ordinary
        # bounded rollup. A reply outside the old cap must also be respected.
        evidence = {"project_id": 1, "items": [], "as_of": None, "coverage": {}}
        unanswered = [] if old_reply else [1]
        assert len(pcv.replies(store.db, 1)["items"]) == 16
        assert [r["message_id"] for r in pcv.previsit(
            store.db, evidence, T0 + 20 * DAY)["unanswered"]] == unanswered
        assert [r["message_id"] for r in pcv.handoff(
            store.db, evidence)["unanswered"]] == unanswered
        assert store.db.total_changes == before
    finally:
        store.close()


def test_reply_index_matches_previous_semantics_for_mixed_threads_and_times():
    import random

    rng = random.Random(1026)
    senders = [None, "a", "b", "c", "d"]
    kinds = tuple(rollup._REPLY_STAGE)
    for _ in range(100):
        # Deliberately unsorted: NULL->0 can follow negative SQL timestamps.
        replies = {root: [(rng.randrange(-3, 8), rng.choice(senders), rng.choice(kinds))
                          for _ in range(40)] for root in range(3)}
        requests = [({}, rng.randrange(4), rng.randrange(-3, 8), rng.choice(senders))
                    for _ in range(30)]
        expected = []
        for _, root, ts, sender in requests:
            row = {}
            seen = [(at, kind) for at, other, kind in replies.get(root, ())
                    if at > ts and sender is not None and other is not None and other != sender]
            if seen:
                row["reply_state"] = max((kind for _, kind in seen), key=rollup._REPLY_STAGE.get)
                done = [at for at, kind in seen if kind == "done"]
                if done and any(kind == "cancel" and at > min(done) for at, kind in seen):
                    row["reply_conflict"] = True
            expected.append(row)
        original = {root: list(rows) for root, rows in replies.items()}
        rollup._match_request_replies(requests, replies)
        assert [row for row, *_ in requests] == expected
        assert replies == original


def test_full_reply_scan_reads_each_thread_reply_once():
    class CountedReplies(list):
        visits = 0

        def __iter__(self):
            for row in super().__iter__():
                self.visits += 1
                yield row

    replies = CountedReplies([(1, "b", "done")] * 3000)
    requests = [({}, 1, 0, "a") for _ in range(3000)]
    rollup._match_request_replies(requests, {1: replies})
    assert all(row == {"reply_state": "done"} for row, *_ in requests)
    assert replies.visits == len(replies)
