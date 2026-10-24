"""One update_loops call over two target messages revives and drops each
message's own candidates exactly as separate per-message calls do (synthetic)."""
import copy
import gc
import json
import time
import weakref

import semantic_loops
import semantic_policy
import semantic_store
from semantic_testkit import _cfg, _pending_fact, _seeded


def _setup(tmp_path):
    db = _seeded(tmp_path)
    cfg, _ = semantic_policy.semantic_config(_cfg())
    db.artifact_add("semantic_policy", semantic_policy.policy_fingerprint(cfg))
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [1, 2])
    return db, cfg, bundle


def _update(db, cfg, bundle, facts_by_target):
    return semantic_loops.update_loops(db, 1, bundle, facts_by_target, None, cfg,
                                       time.monotonic() + 30)


def _pair(bundle, mid):
    """Two distinct open items (keep, swap) originating in message ``mid``."""
    member = next(m for m in bundle["members"] if m["message_id"] == mid)
    first = _pending_fact(member)[0]
    second = copy.deepcopy(first)
    second["statement"] = first["statement"] + "（別件）"
    second["fact_id"] = first["fact_id"] + "-b"
    return first, second


def _state(db):
    """(message_id, description, invalidated) per candidate row, by row id."""
    return [(r["message_id"], json.loads(r["content"])["description"],
             bool(json.loads(r["meta"]).get("invalidated")))
            for r in db.artifacts("loop_candidate", project_id=1)]


def _prepared(tmp_path):
    tmp_path.mkdir()
    db, cfg, bundle = _setup(tmp_path)
    pairs = {mid: _pair(bundle, mid) for mid in (1, 2)}
    _update(db, cfg, bundle, {mid: [a, b] for mid, (a, b) in pairs.items()})
    _update(db, cfg, bundle, {mid: [a] for mid, (a, _) in pairs.items()})   # b dropped
    return db, cfg, bundle, pairs


def test_one_call_revives_and_drops_for_every_target_like_separate_calls(tmp_path):
    db, cfg, bundle, pairs = _prepared(tmp_path / "together")
    twin, twin_cfg, twin_bundle, twin_pairs = _prepared(tmp_path / "separate")
    try:
        before = _state(db)
        assert sum(invalidated for *_, invalidated in before) == 2
        # b returns (revive) and a disappears (drop), for both messages at once
        assert _update(db, cfg, bundle, {mid: [b] for mid, (_, b) in pairs.items()}) == (0, True)
        for mid, (_, b) in twin_pairs.items():
            assert _update(twin, twin_cfg, twin_bundle, {mid: [b]}) == (0, True)
        after = _state(db)
        assert after == _state(twin)
        assert len(after) == len(before) == 4        # revived in place, nothing duplicated
        for mid, (a, b) in pairs.items():
            own = {description: invalidated for m, description, invalidated in after if m == mid}
            assert own == {a["statement"]: True, b["statement"]: False}
    finally:
        db.close()
        twin.close()


class _Row(dict):
    """A weakref-able copy of one candidate row."""


class _TrackedDb:
    """Delegates to the real connection; candidate scans return tracked rows."""

    def __init__(self, db):
        self._db = db
        self.scans = []          # weakrefs to the rows each candidate scan returned
        self.first_alive = []    # first-scan rows still owned when each later scan starts

    def __getattr__(self, name):
        return getattr(self._db, name)

    def __enter__(self):
        return self._db.__enter__()

    def __exit__(self, *exc):
        return self._db.__exit__(*exc)

    def execute(self, sql, *args):
        cursor = self._db.execute(sql, *args)
        if not sql.startswith("SELECT artifact_id,message_id,content,meta FROM artifacts"):
            return cursor
        if self.scans:
            gc.collect()
            self.first_alive.append(sum(ref() is not None for ref in self.scans[0]))
        rows = [_Row(dict(row)) for row in cursor.fetchall()]
        self.scans.append([weakref.ref(row) for row in rows])
        return type("Cursor", (), {"fetchall": lambda _self: rows})()


def test_first_candidate_snapshot_is_released_before_the_post_target_reread(tmp_path):
    db, cfg, bundle, pairs = _prepared(tmp_path / "db")
    try:
        tracked_db = _TrackedDb(db.db)
        tracked = type("TrackedLedger", (), {
            "db": tracked_db, "__getattr__": lambda _self, name: getattr(db, name)})()
        _update(tracked, cfg, bundle, {mid: [b] for mid, (_, b) in pairs.items()})
        assert len(tracked_db.scans[0]) == 4           # one initial scan of the thread's rows
        assert tracked_db.first_alive[0] == 0          # none is still owned at the re-read
    finally:
        db.close()
