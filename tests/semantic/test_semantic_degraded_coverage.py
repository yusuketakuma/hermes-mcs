"""One completed target must not hide its unassessed sibling arrival."""
import semantic_notice
import semantic_policy
import semantic_store
import json
import pytest

from semantic_notice import arrival_ids
from semantic_testkit import _cfg, _seeded


@pytest.fixture
def unreadable_json(monkeypatch):
    raw = '{"nested":' + '[' * 1500 + '0' + ']' * 1500 + '}'
    original_loads = json.loads
    try:
        original_loads(raw)
    except RecursionError:
        return raw

    # Newer iterative decoders still exercise the older supported runtime failure.
    def loads(value, *args, **kwargs):
        if value == raw:
            raise RecursionError("synthetic decoder depth")
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)
    return raw


def test_degraded_notice_requires_all_arrival_targets_to_pass(tmp_path):
    db = _seeded(tmp_path)
    try:
        cfg, _ = semantic_policy.semantic_config(_cfg("enforce"))
        with db.db:
            db.db.execute("UPDATE notify_outbox SET created_at=0")
        fp = semantic_store.thread_bundle(db.db, 1, 1)["source_fingerprint"]
        db.artifact_add("semantic_audit", json.dumps({"status": "PASS"}),
                        project_id=1, message_id=1,
                        meta={"fingerprint": fp, "audit_status": "PASS"})
        assert semantic_notice.emit_degraded(db, cfg) == 1
        assert semantic_notice.emit_degraded(db, cfg) == 0
        assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    finally:
        db.close()


def test_attempted_base_delivery_never_creates_degraded_duplicate(tmp_path):
    db = _seeded(tmp_path)
    try:
        cfg, _ = semantic_policy.semantic_config(_cfg("enforce"))
        with db.db:
            db.db.execute("UPDATE notify_outbox SET created_at=0,state='failed',attempts=1")
        assert semantic_notice.emit_degraded(db, cfg) == 0
        with db.db:
            db.db.execute("UPDATE notify_outbox SET attempts=0,progress=?",
                          (json.dumps({"sent": ["unknown-receipt"]}),))
        assert semantic_notice.emit_degraded(db, cfg) == 0
    finally:
        db.close()


@pytest.mark.parametrize("payload", [[], {"message_ids": [[1]]},
                                      {"message_ids": [True]},
                                      {"message_ids": [2**63]},
                                      {"message_ids": [10**100]},
                                      {"message_ids": [1, 2**63]}])
def test_malformed_arrival_never_authorizes_notice(tmp_path, payload):
    db = _seeded(tmp_path)
    try:
        cfg, _ = semantic_policy.semantic_config(_cfg("enforce"))
        with db.db:
            db.db.execute("UPDATE notify_outbox SET created_at=0,payload=?",
                          (json.dumps(payload),))
        assert semantic_notice.origin_event_id(db.db, 1, [1]) is None
        assert semantic_notice.emit_degraded(db, cfg) == 0
    finally:
        db.close()


def test_arrival_ids_retains_sqlite_integer_maximum():
    assert arrival_ids(json.dumps({"message_ids": [1, 2**63 - 1]})) == [1, 2**63 - 1]


def test_unreadable_arrival_never_authorizes_notice(tmp_path, unreadable_json):
    db = _seeded(tmp_path)
    try:
        cfg, _ = semantic_policy.semantic_config(_cfg("enforce"))
        with db.db:
            db.db.execute("UPDATE notify_outbox SET created_at=0,payload=?",
                          (unreadable_json,))
        assert semantic_notice.origin_event_id(db.db, 1, [1]) is None
        assert semantic_notice.emit_degraded(db, cfg) == 0
        assert db.db.execute("SELECT payload FROM notify_outbox").fetchone()[0] == unreadable_json
        assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize("column", ["payload", "progress"])
def test_unreadable_attempt_keeps_notice_origin_unknown(tmp_path, unreadable_json, column):
    db = _seeded(tmp_path)
    try:
        origin = semantic_notice.origin_event_id(db.db, 1, [1])
        notice = db.outbox_add("semantic_notice", 1,
                              {"src_event_id": origin, "target_message_id": 1})
        with db.db:
            db.db.execute(f"UPDATE notify_outbox SET state='failed',attempts=1,{column}=? "
                          "WHERE event_id=?", (unreadable_json, notice))
        assert semantic_notice.origin_event_id(db.db, 1, [1]) is None
        row = db.db.execute(f"SELECT state,attempts,{column} FROM notify_outbox WHERE event_id=?",
                            (notice,)).fetchone()
        assert tuple(row) == ("failed", 1, unreadable_json)
        assert db.db.execute("SELECT count(*) FROM notify_outbox").fetchone()[0] == 2
    finally:
        db.close()


@pytest.mark.parametrize("column", ["payload", "progress"])
def test_unreadable_arrival_does_not_block_later_degraded_notice(tmp_path, unreadable_json, column):
    db = _seeded(tmp_path)
    try:
        cfg, _ = semantic_policy.semantic_config(_cfg("enforce"))
        with db.db:
            db.db.execute(f"UPDATE notify_outbox SET created_at=0,{column}=?",
                          (unreadable_json,))
        valid = db.outbox_add("new_messages", 1, {"message_ids": [1], "source": "synthetic-correction"})
        with db.db:
            db.db.execute("UPDATE notify_outbox SET created_at=0 WHERE event_id=?", (valid,))
        jobs = [tuple(row) for row in db.db.execute("SELECT job_id,state,attempts FROM fetch_jobs")]
        assert semantic_notice.emit_degraded(db, cfg) == 1
        notices = db.db.execute("SELECT payload FROM notify_outbox WHERE kind='semantic_notice'").fetchall()
        assert len(notices) == 1
        payload = json.loads(notices[0][0])
        assert payload["src_event_id"] == valid and payload["target_message_ids"] == [1]
        assert payload["degraded"] is True
        assert db.db.execute(f"SELECT {column} FROM notify_outbox WHERE event_id=1").fetchone()[0] == unreadable_json
        assert [tuple(row) for row in db.db.execute("SELECT job_id,state,attempts FROM fetch_jobs")] == jobs
        assert db.db.execute("SELECT count(*) FROM artifacts WHERE kind='semantic_usage'").fetchone()[0] == 0
        assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    finally:
        db.close()
