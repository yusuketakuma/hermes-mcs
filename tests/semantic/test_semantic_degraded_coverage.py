"""One completed target must not hide its unassessed sibling arrival."""
import json
import pytest

import semantic
from test_mcs_semantic import _cfg, _seeded


def test_degraded_notice_requires_all_arrival_targets_to_pass(tmp_path):
    db = _seeded(tmp_path)
    try:
        cfg, _ = semantic.semantic_config(_cfg("enforce"))
        with db.db:
            db.db.execute("UPDATE notify_outbox SET created_at=0")
        fp = semantic.thread_bundle(db, 1, 1)["source_fingerprint"]
        db.artifact_add("semantic_audit", json.dumps({"status": "PASS"}),
                        project_id=1, message_id=1,
                        meta={"fingerprint": fp, "audit_status": "PASS"})
        assert semantic._emit_degraded(db, cfg) == 1
        assert semantic._emit_degraded(db, cfg) == 0
        assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    finally:
        db.close()


def test_attempted_base_delivery_never_creates_degraded_duplicate(tmp_path):
    db = _seeded(tmp_path)
    try:
        cfg, _ = semantic.semantic_config(_cfg("enforce"))
        with db.db:
            db.db.execute("UPDATE notify_outbox SET created_at=0,state='failed',attempts=1")
        assert semantic._emit_degraded(db, cfg) == 0
        with db.db:
            db.db.execute("UPDATE notify_outbox SET attempts=0,progress=?",
                          (json.dumps({"sent": ["unknown-receipt"]}),))
        assert semantic._emit_degraded(db, cfg) == 0
    finally:
        db.close()


@pytest.mark.parametrize("payload", [[], {"message_ids": [[1]]},
                                      {"message_ids": [True]}])
def test_malformed_arrival_never_authorizes_notice(tmp_path, payload):
    db = _seeded(tmp_path)
    try:
        cfg, _ = semantic.semantic_config(_cfg("enforce"))
        with db.db:
            db.db.execute("UPDATE notify_outbox SET created_at=0,payload=?",
                          (json.dumps(payload),))
        assert semantic._notify_src_event(db, 1, [1]) is None
        assert semantic._emit_degraded(db, cfg) == 0
    finally:
        db.close()
