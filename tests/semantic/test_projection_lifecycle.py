"""Independent synthetic expiry expectations and same-transaction rollback contracts."""
import json
import sqlite3

import pytest

import semantic_projection
import semantic_publication
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _seeded

_CURRENT = {VERSION_KEY: CURRENT_VERSION}


@pytest.mark.parametrize("meta,off,enabled,source,revivable,expected", [
    (None, True, False, None, False, ("expire", "off")),
    (None, False, True, None, False, ("expire", "metadata")),
    ({"policy_fingerprint": "p", "fingerprint": "s"}, False, True, "s", False, ("expire", "metadata")),
    ({**_CURRENT, "policy_fingerprint": "p", "fingerprint": "s"}, True, False, "s", True, ("expire", "off")),
    ({**_CURRENT, "policy_fingerprint": "p", "fingerprint": "s"}, False, False, "s", True, ("expire", "fact_source")),
    ({**_CURRENT, "policy_fingerprint": "old", "fingerprint": "s"}, False, True, "s", True, ("expire", "policy")),
    ({**_CURRENT, "policy_fingerprint": "p", "fingerprint": "old"}, False, True, "s", True, ("expire", "source")),
    ({**_CURRENT, "policy_fingerprint": "p", "fingerprint": "s"}, False, True, None, True, ("expire", "source")),
    ({**_CURRENT, "policy_fingerprint": "p", "fingerprint": "s"}, False, True, "s", False, ("keep", None)),
    ({**_CURRENT, "policy_fingerprint": "p", "fingerprint": "s", "invalidated": True}, False, True, "s", False, ("expire", "source")),
    ({**_CURRENT, "policy_fingerprint": "p", "fingerprint": "s", "invalidated": True}, False, True, "s", True, ("revive", None)),
])
def test_projection_decisions_preserve_off_policy_source_and_pass_precedence(meta, off, enabled, source, revivable, expected):
    before = json.dumps(meta, sort_keys=True)
    assert semantic_projection.projection_transition(meta, off=off, enabled=enabled,
        policy="p", source_fingerprint=source, revivable=revivable) == expected
    assert json.dumps(meta, sort_keys=True) == before


def test_projection_expiry_and_rollup_eviction_rollback_together(tmp_path):
    ledger = _seeded(tmp_path)
    try:
        aid = ledger.artifact_add("canonical_projection", "{}", project_id=1,
            message_id=1, meta={**_CURRENT, "fingerprint": "synthetic", "policy_fingerprint": "p"})
        rollup = ledger.artifact_add("patient_rollup", "{}", project_id=1, meta=_CURRENT)
        original = ledger.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0]
        ledger.db.execute("CREATE TEMP TRIGGER deny_rollup_delete BEFORE DELETE ON artifacts "
                          "WHEN OLD.kind='patient_rollup' BEGIN SELECT RAISE(ABORT, 'synthetic eviction failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="synthetic eviction failure"):
            semantic_publication.invalidate_projections(ledger.db, {"mode": "off"})
        assert not ledger.db.in_transaction
        assert ledger.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0] == original
        assert ledger.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (rollup,)).fetchone()
        ledger.db.execute("DROP TRIGGER deny_rollup_delete")
        assert semantic_publication.invalidate_projections(ledger.db, {"mode": "off"}) == 1
        meta = json.loads(ledger.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0])
        assert meta["invalidated"] is True and meta["invalidated_reason"] == "off"
        assert not ledger.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (rollup,)).fetchone()
        assert not ledger.db.in_transaction
        assert ledger.db.execute("SELECT 1").fetchone()[0] == 1
    finally:
        ledger.close()


def test_projection_expiry_without_candidates_skips_the_rowid_walk(tmp_path):
    ledger = _seeded(tmp_path)
    seen = []
    try:
        assert ledger.db.execute(
            "SELECT 1 FROM artifacts WHERE kind IN ('canonical_projection','semantic_facts_v4') LIMIT 1"
        ).fetchone() is None
        ledger.db.set_trace_callback(seen.append)
        assert semantic_publication.invalidate_projections(ledger.db, {"mode": "off"}) == 0
        ledger.db.set_trace_callback(None)
        assert any("SELECT 1 FROM artifacts WHERE kind IN" in statement for statement in seen)
        assert not any("+a.kind" in statement for statement in seen)
    finally:
        ledger.db.set_trace_callback(None)
        ledger.close()


def test_projection_expiry_pages_reads_but_commits_once(tmp_path, monkeypatch):
    """Peak memory: artifacts are read one page at a time, while the decisions
    still commit in one transaction, so a failure on a later page keeps none."""
    monkeypatch.setattr(semantic_publication, "_INVALIDATE_PAGE", 2)
    ledger = _seeded(tmp_path)
    try:
        ids = [ledger.artifact_add("canonical_projection", "{}", project_id=1, message_id=1,
                                   meta={**_CURRENT, "fingerprint": f"synthetic-{i}",
                                         "policy_fingerprint": "p"}) for i in range(5)]
        ledger.db.execute(
            "CREATE TEMP TRIGGER deny_last BEFORE UPDATE ON artifacts "
            f"WHEN OLD.artifact_id={ids[-1]} BEGIN SELECT RAISE(ABORT, 'synthetic page failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="synthetic page failure"):
            semantic_publication.invalidate_projections(ledger.db, {"mode": "off"})
        assert not ledger.db.in_transaction

        def invalidated():
            return [json.loads(ledger.db.execute(
                "SELECT meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0]).get("invalidated")
                for aid in ids]
        assert invalidated() == [None] * 5
        ledger.db.execute("DROP TRIGGER deny_last")
        assert semantic_publication.invalidate_projections(ledger.db, {"mode": "off"}) == 5
        assert invalidated() == [True] * 5
    finally:
        ledger.close()


def test_projection_expiry_scan_is_fixed_at_start(tmp_path, monkeypatch):
    """A projection published while the scan runs waits for the next scan."""
    monkeypatch.setattr(semantic_publication, "_INVALIDATE_PAGE", 2)
    ledger = _seeded(tmp_path)
    try:
        for i in range(3):
            ledger.artifact_add("canonical_projection", "{}", project_id=1, message_id=1,
                                meta={**_CURRENT, "fingerprint": f"synthetic-{i}", "policy_fingerprint": "p"})
        late = []
        transition = semantic_projection.projection_transition

        def publish_during_scan(meta, **kwargs):
            if not late:
                late.append(ledger.artifact_add("canonical_projection", "{}", project_id=1, message_id=1,
                    meta={**_CURRENT, "fingerprint": "synthetic-late", "policy_fingerprint": "p"}))
            return transition(meta, **kwargs)
        monkeypatch.setattr(semantic_projection, "projection_transition", publish_during_scan)
        assert semantic_publication.invalidate_projections(ledger.db, {"mode": "off"}) == 3
        meta = json.loads(ledger.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", late).fetchone()[0])
        assert "invalidated" not in meta
    finally:
        ledger.close()


def test_projection_expiry_page_query_walks_the_rowid_range(tmp_path, monkeypatch):
    """Each page must be a rowid range seek, not a kind-index scan re-sorted
    per page (which re-evaluates the JSON predicate for all remaining rows)."""
    ledger = _seeded(tmp_path)
    plans = []
    real = ledger.db

    class Spy:
        def __getattr__(self, name):
            return getattr(real, name)

        def __enter__(self):
            return real.__enter__()

        def __exit__(self, *exc):
            return real.__exit__(*exc)

        def execute(self, sql, *args):
            if "a.artifact_id>?" in sql:
                plans.append(" ".join(r[3] for r in real.execute("EXPLAIN QUERY PLAN " + sql, *args)))
            return real.execute(sql, *args)
    try:
        ledger.artifact_add("canonical_projection", "{}", project_id=1, message_id=1,
                            meta={**_CURRENT, "fingerprint": "synthetic", "policy_fingerprint": "p"})
        semantic_publication.invalidate_projections(Spy(), {"mode": "off"})
        assert plans and all("SEARCH a USING INTEGER PRIMARY KEY" in p for p in plans), plans
    finally:
        ledger.close()
