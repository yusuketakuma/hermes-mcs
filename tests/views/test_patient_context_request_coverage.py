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
        ordinary = rollup.build_rollup(store.db, 1)["recent_requests"]
        full = rollup.build_rollup(store.db, 1, request_limit=None)["recent_requests"]
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


def _apply_relations(requests, replies, monkeypatch, *, related=None):
    """Exercise per-request matching using explicit current synthetic Loop relations."""
    import request_loops

    mapping = {"ack": "acknowledges", "progress": "progress_report", "answer": "answers_question",
               "done": "completion_report", "cancel": "cancellation_report"}
    messages, events = [], {}
    for root, rows in replies.items():
        events[root] = []
        for at, sender, kind in rows:
            mid = len(messages) + 10000
            messages.append({"message_id": mid, "parent_id": root, "posted_at": f"synthetic-post-{mid}",
                             "posted_at_ts": at, "sender_id": sender, "body_state": "full"})
            relation = mapping[kind]
            events[root].append({"relation": relation, "reported_relation": relation,
                                 "confidence": 1, "stale": False, "trigger_message_id": mid,
                                 "trigger_revision": f"synthetic-revision-{mid}"})
    sources = [{"message_id": row["mid"], "artifact_id": i + 1} for i, (row, *_) in enumerate(requests)]
    loops = {i + 1: {"adoption_eligible": True, "description": row["ctx"],
                 "relation_events": [event for index, event in enumerate(events.get(root, []))
                                     if related is None or (i, index) in related]}
             for i, (row, root, *_rest) in enumerate(requests)}
    calls = []

    class Rows(list):
        def fetchall(self):
            return self

    class DB:
        def execute(self, _sql, _params):
            return Rows(sources)

    monkeypatch.setattr(request_loops, "latest_policy", lambda _: "synthetic")

    def current(_db, selected, **_kwargs):
        result = []
        for source in selected:
            aid = source["artifact_id"]
            calls.append(aid)
            result.append({**loops[aid], "artifact_id": aid,
                           "relation_events": [{**event, "loop_artifact_id": aid}
                                               for event in loops[aid]["relation_events"]]})
        return result

    monkeypatch.setattr(request_loops, "effective_loop_states", current)
    rollup._match_request_replies(DB(), 1, requests, [r[0] for r in requests], messages, 1000)
    return calls


def test_reply_relations_match_explicit_evidence_for_mixed_threads_and_times(monkeypatch):
    import random

    rng = random.Random(1026)
    senders = [None, 1, 2, 3, 4]
    # The expected relation vocabulary and precedence are a fixed independent
    # oracle, rather than copied from the implementation under test.
    ranks = {kind: index for index, kind in enumerate(('ack', 'progress', 'answer', 'done', 'cancel'))}
    relations = {'ack':'acknowledges','progress':'progress_report','answer':'answers_question',
                 'done':'completion_report','cancel':'cancellation_report'}
    kinds = tuple(ranks)
    for _ in range(100):
        replies = {root: [(rng.randrange(97, 108), rng.choice(senders), rng.choice(kinds))
                          for _ in range(40)] for root in range(1, 4)}
        requests = [({"mid": i, "ctx": f"架空依頼{i}"}, rng.randrange(1, 5),
                     rng.randrange(97, 108), rng.choice(senders)) for i in range(30)]
        related = {(i, j) for i, (_, root, *_rest) in enumerate(requests)
                   for j in range(len(replies.get(root, ()))) if rng.random() < .5}
        reply_ids = {(root, j): mid + 10000 for mid, (root, j) in enumerate(
            (root, j) for root, rows in replies.items() for j in range(len(rows)))}
        expected = []
        for i, (row, root, ts, sender) in enumerate(requests):
            row = dict(row)
            seen = [(at, kind, reply_ids[root, j]) for j, (at, other, kind) in enumerate(replies.get(root, ()))
                    if (i, j) in related and at > ts and sender is not None and other is not None and other != sender]
            if seen:
                row["reply_state"] = max((kind for _, kind, _ in seen), key=ranks.get)
                ordered = sorted(seen, key=lambda event: (event[0], event[2], relations[event[1]]))
                row['reply_events'] = [{'message_id': mid, 'revision': f'synthetic-revision-{mid}',
                    'loop_artifact_id': i + 1, 'posted_at': f'synthetic-post-{mid}', 'posted_at_ts': at,
                    'relation': relations[kind], 'reply_state': kind,
                    'same_time_unordered': sum(other_at == at for other_at, *_ in seen) > 1}
                    for at, kind, mid in ordered]
                # Conflicting reports remain a conflict regardless of which
                # came first; this does not choose a final business status.
                if {'done', 'cancel'} <= {kind for _, kind, _ in seen}:
                    row["reply_conflict"] = True
            expected.append(row)
        original = {root: list(rows) for root, rows in replies.items()}
        calls = _apply_relations(requests, replies, monkeypatch, related=related)
        assert [row for row, *_ in requests] == expected
        assert replies == original
        assert len(calls) == len(requests)


def test_thread_reply_does_not_answer_another_request_without_a_matching_relation(monkeypatch):
    requests = [({'mid':1,'ctx':text}, 1, 100, 1) for text in ('訪問日を確認してください。', '連絡先を確認してください。')]
    calls = _apply_relations(requests, {1:[(101, 2, 'done')]}, monkeypatch, related={(0, 0)})
    assert requests[0][0]['reply_state'] == 'done'
    assert len(requests[0][0]['reply_events']) == 1
    assert 'reply_state' not in requests[1][0] and 'reply_events' not in requests[1][0]
    assert len(calls) == len(set(calls)) == 2


def test_full_reply_scan_reads_each_current_candidate_once(monkeypatch):
    requests = [({"mid": i, "ctx": f"架空依頼{i}"}, i + 1, 100, 1) for i in range(3000)]
    replies = {i + 1: [(101, 2, "done")] for i in range(3000)}
    calls = _apply_relations(requests, replies, monkeypatch)
    assert all(row["reply_state"] == "done" for row, *_ in requests)
    assert len(calls) == len(set(calls)) == len(requests)
