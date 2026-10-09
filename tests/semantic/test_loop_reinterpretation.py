"""A message re-evaluated in the same source/policy generation keeps only
the Loop candidates its current facts still produce: dropped ones are
marked invalidated (kept as history), never current, forked or adopted;
other messages' candidates and forks stay. Synthetic ledger only."""
import copy
import json
import time

import pytest

import request_loops
import semantic
import semantic_loops
from semantic_testkit import _cfg, _message, _pending_fact, _seeded


def _setup(tmp_path):
    db = _seeded(tmp_path)
    cfg, _ = semantic.semantic_config(_cfg())
    db.artifact_add("semantic_policy", semantic.policy_fingerprint(cfg))
    bundle = semantic.thread_bundle(db, 1, 1, [1, 2])
    return db, cfg, bundle


def _update(db, cfg, bundle, facts_by_target):
    return semantic_loops.update_loops(db, 1, bundle, facts_by_target, None, cfg,
                                       time.monotonic() + 30)


def _rows(db, mid=None):
    rows = db.artifacts("loop_candidate", project_id=1)
    return [(r["artifact_id"], json.loads(r["content"]), json.loads(r["meta"]))
            for r in rows if mid is None or r["message_id"] == mid]


def _current(db, bundle, cfg, mid=None):
    fp, policy = bundle["source_fingerprint"], semantic.policy_fingerprint(cfg)
    return [aid for aid, _, meta in _rows(db, mid)
            if semantic_loops._current_meta(meta, fp, policy)]


def _member(bundle, mid):
    return next(m for m in bundle["members"] if m["message_id"] == mid)


def test_dropped_candidate_is_invalidated_and_never_adoptable(tmp_path):
    db, cfg, bundle = _setup(tmp_path)
    try:
        before = _pending_fact(_member(bundle, 1))
        _update(db, cfg, bundle, {1: before})
        [(old_id, _, _)] = _rows(db, 1)
        assert request_loops.current_candidate(db.db, 1, old_id, 1)["artifact_id"] == old_id
        after = copy.deepcopy(before)
        after[0]["kind"] = "observation"        # reinterpreted: no longer an open item
        _update(db, cfg, bundle, {1: after})
        [(aid, _, meta)] = _rows(db, 1)          # history kept, nothing new
        assert aid == old_id and meta["invalidated"] is True
        assert meta["invalidated_reason"] == "reinterpreted"
        assert _current(db, bundle, cfg) == []
        with pytest.raises(ValueError, match="loop_candidate_stale"):
            request_loops.current_candidate(db.db, 1, old_id, 1)
        assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    finally:
        db.close()


def test_same_facts_again_change_nothing(tmp_path):
    db, cfg, bundle = _setup(tmp_path)
    try:
        facts = {1: _pending_fact(_member(bundle, 1))}
        _update(db, cfg, bundle, facts)
        first = _rows(db)
        assert _update(db, cfg, bundle, facts) == (0, True)
        assert _rows(db) == first
    finally:
        db.close()


def test_returning_item_revives_its_row_instead_of_duplicating(tmp_path):
    db, cfg, bundle = _setup(tmp_path)
    try:
        facts = _pending_fact(_member(bundle, 1))
        _update(db, cfg, bundle, {1: facts})
        [(old_id, _, _)] = _rows(db, 1)
        _update(db, cfg, bundle, {1: []})
        assert _current(db, bundle, cfg) == []
        _update(db, cfg, bundle, {1: facts})
        assert [aid for aid, _, _ in _rows(db, 1)] == [old_id]
        assert _current(db, bundle, cfg) == [old_id]
        assert "invalidated" not in _rows(db, 1)[0][2]
    finally:
        db.close()


def test_changed_non_identity_fields_keep_the_current_row(tmp_path):
    db, cfg, bundle = _setup(tmp_path)
    try:
        facts = _pending_fact(_member(bundle, 1))
        _update(db, cfg, bundle, {1: facts})
        [(old_id, _, _)] = _rows(db, 1)
        moved = copy.deepcopy(facts)
        moved[0]["occurred_at"] = "2026-09-20"   # not part of the open item's identity
        _update(db, cfg, bundle, {1: moved})
        assert [aid for aid, _, _ in _rows(db, 1)] == [old_id]
        assert _current(db, bundle, cfg) == [old_id]
    finally:
        db.close()


def test_other_messages_candidates_are_untouched(tmp_path):
    db, cfg, bundle = _setup(tmp_path)
    try:
        reply = copy.deepcopy(_pending_fact(_member(bundle, 1)))
        _update(db, cfg, bundle, {1: _pending_fact(_member(bundle, 1)), 2: reply})
        [(reply_id, _, _)] = _rows(db, 2)
        _update(db, cfg, bundle, {1: []})
        assert _current(db, bundle, cfg, 2) == [reply_id]
        assert _current(db, bundle, cfg, 1) == []
    finally:
        db.close()


def test_new_reply_fork_survives_and_invalidated_rows_are_never_forked(tmp_path):
    db, cfg, bundle = _setup(tmp_path)
    try:
        kept = _pending_fact(_member(bundle, 1))
        dropped = copy.deepcopy(kept)
        dropped[0]["statement"] = "別の未完了事項"
        _update(db, cfg, bundle, {1: kept + dropped})
        _update(db, cfg, bundle, {1: kept})       # the second item is reinterpreted away
        db.save_messages([_message(3, parent=1, body="承知しました。")])
        newer = semantic.thread_bundle(db, 1, 1, [3])
        _update(db, cfg, newer, {})                # a new reply: fork current items only
        forks = [content["description"] for aid, content, meta in _rows(db, 1)
                 if meta["fingerprint"] == newer["source_fingerprint"]]
        assert forks == [kept[0]["statement"]]
        # re-evaluating message 1 in the new generation keeps that fork
        [fork_id] = _current(db, newer, cfg, 1)
        _update(db, cfg, newer, {1: _pending_fact(_member(newer, 1))})
        assert _current(db, newer, cfg, 1) == [fork_id]
    finally:
        db.close()
