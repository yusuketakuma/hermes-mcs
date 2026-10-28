"""Independent synthetic expiry expectations and same-transaction rollback contracts."""
import json
import sqlite3

import pytest

import semantic_projection
import semantic_v4
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
            semantic_v4.invalidate_projections(ledger.db, {"mode": "off"})
        assert not ledger.db.in_transaction
        assert ledger.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0] == original
        assert ledger.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (rollup,)).fetchone()
        ledger.db.execute("DROP TRIGGER deny_rollup_delete")
        assert semantic_v4.invalidate_projections(ledger.db, {"mode": "off"}) == 1
        meta = json.loads(ledger.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0])
        assert meta["invalidated"] is True and meta["invalidated_reason"] == "off"
        assert not ledger.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (rollup,)).fetchone()
        assert not ledger.db.in_transaction
        assert ledger.db.execute("SELECT 1").fetchone()[0] == 1
    finally:
        ledger.close()
