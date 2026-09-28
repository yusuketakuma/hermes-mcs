"""Changing interpretation policy invalidates cached analysis and unsent text."""
import json
import time

import pytest
import ledger
import mcs_view
import notify_flush
import semantic
from semantic_testkit import _cfg, _FakeJev, _llm, _seeded


def test_policy_change_rechecks_analysis_and_freezes_old_notification(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    try:
        original = _cfg("enforce")
        semantic.run_due(db, original, {"errors": []}, time.monotonic() + 300,
                         jev_client=_FakeJev(), llm_fn=_llm)
        old_summary = db.artifacts("semantic_summary", message_id=1)[-1]
        event = db.db.execute("SELECT * FROM notify_outbox WHERE kind='semantic_notice' LIMIT 1").fetchone()
        assert event is not None
        monkeypatch.setattr(notify_flush, "_config", lambda: _cfg("enforce", project_ids=[]))
        with pytest.raises(notify_flush._DeferredSend):
            notify_flush._semantic_gate(db, event, json.loads(event["payload"]))
        changed = _cfg("enforce", match_threshold=.8)
        monkeypatch.setattr(notify_flush, "_config", lambda: changed)
        with pytest.raises(notify_flush._StaleSend):
            notify_flush._semantic_gate(db, event, json.loads(event["payload"]))
        db.semantic_seed(1, [1], {"source": "replay"})
        fake = _FakeJev()
        semantic.run_due(db, changed, {"errors": []}, time.monotonic() + 300,
                         jev_client=fake, llm_fn=_llm)
        assert fake.requests_made > 0
        current = db.artifacts("semantic_summary", message_id=1)[-1]
        assert current["artifact_id"] != old_summary["artifact_id"]
        old_meta, new_meta = json.loads(old_summary["meta"]), json.loads(current["meta"])
        assert old_meta["fingerprint"] == new_meta["fingerprint"]
        assert old_meta["policy_fingerprint"] != new_meta["policy_fingerprint"]
        snapshot = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snapshots"))
        view = mcs_view.View(snapshot)
        try:
            rows = view.read("semantic", project=1, message_id=1)["semantic"]["semantic_summary"]
            historical = next(r for r in rows if r["artifact_id"] == old_summary["artifact_id"])
            assert historical["effective_status"] == "STALE"
        finally:
            view.close()
    finally:
        db.close()


def test_new_policy_does_not_renotify_an_attempted_delivery(tmp_path):
    db = _seeded(tmp_path)
    try:
        semantic.run_due(db, _cfg("enforce"), {"errors": []}, time.monotonic() + 300,
                         jev_client=_FakeJev(), llm_fn=_llm)
        events = db.db.execute("SELECT event_id FROM notify_outbox WHERE kind='semantic_notice'").fetchall()
        assert events
        for row in events:
            db.outbox_mark(row["event_id"], "accepted", accepted_ref="synthetic-receipt")
        db.semantic_seed(1, [1], {"source": "replay"})
        semantic.run_due(db, _cfg("enforce", match_threshold=.8), {"errors": []},
                         time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm)
        assert db.db.execute("SELECT count(*) FROM notify_outbox WHERE kind='semantic_notice'").fetchone()[0] == len(events)
    finally:
        db.close()


def test_attempted_notice_does_not_hide_a_new_recorded_arrival(tmp_path):
    db = _seeded(tmp_path)
    try:
        origin = semantic._notify_src_event(db, 1, [1])
        assert origin is not None
        db.outbox_add("semantic_notice", 1,
                      {"src_event_id": origin, "target_message_id": 1})
        with db.db:
            db.db.execute("UPDATE notify_outbox SET state='failed',attempts=1 "
                          "WHERE kind='semantic_notice'")
        assert semantic._notify_src_event(db, 1, [1]) is None
        db.outbox_add("new_messages", 1, {"message_ids": [1], "source": "correction"})
        replacement = semantic._notify_src_event(db, 1, [1])
        assert replacement is not None and replacement != origin
        db.outbox_add("semantic_notice", 1,
                      {"src_event_id": replacement, "target_message_id": 1})
        with db.db:
            db.db.execute("UPDATE notify_outbox SET state='accepted' "
                          "WHERE kind='semantic_notice'")
        assert semantic._notify_src_event(db, 1, [1]) is None
    finally:
        db.close()
