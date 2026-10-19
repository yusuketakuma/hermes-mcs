"""Re-evaluating an origin message in a NEW generation whose
facts no longer yield an open item must not make that item current via the
older generation's fork path. Synthetic ledger only."""
import semantic_policy
import semantic_store
import copy
import json
import time

import semantic_loops
from semantic_testkit import _cfg, _message, _pending_fact, _seeded


def _update(db, cfg, bundle, facts_by_target):
    return semantic_loops.update_loops(db, 1, bundle, facts_by_target, None,
                                       cfg, time.monotonic() + 30)


def _current_descriptions(db, bundle, cfg, mid):
    fp, policy = bundle["source_fingerprint"], semantic_policy.policy_fingerprint(cfg)
    out = []
    for row in db.artifacts("loop_candidate", project_id=1):
        meta = json.loads(row["meta"])
        if row["message_id"] == mid and semantic_loops._current_meta(meta, fp, policy):
            out.append(json.loads(row["content"])["description"])
    return sorted(out)


def test_new_generation_reinterpretation_does_not_fork_dropped_item(tmp_path):
    db = _seeded(tmp_path)
    try:
        cfg, _ = semantic_policy.semantic_config(_cfg())
        old = semantic_store.thread_bundle(db, 1, 1, [1, 2])
        member = next(m for m in old["members"] if m["message_id"] == 1)
        item_a = _pending_fact(member)
        _update(db, cfg, old, {1: item_a})            # generation 1 yields A

        db.save_messages([_message(3, parent=1, body="承知しました。")])
        new = semantic_store.thread_bundle(db, 1, 1, [1])   # generation 2
        assert new["source_fingerprint"] != old["source_fingerprint"]
        item_b = copy.deepcopy(item_a)
        item_b[0]["statement"] = "別の未完了事項"      # gen 2 reinterprets: only B

        _update(db, cfg, new, {1: item_b})
        first = _current_descriptions(db, new, cfg, 1)
        _update(db, cfg, new, {1: item_b})            # identical call again
        second = _current_descriptions(db, new, cfg, 1)
        # Same facts for the same message/generation must give one state,
        # and an item the current facts no longer yield is never current.
        assert first == second == ["別の未完了事項"]
    finally:
        db.close()
