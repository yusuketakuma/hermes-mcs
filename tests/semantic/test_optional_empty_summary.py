"""Synthetic regression for an empty target in a multi-target bundle."""
import json
import time

import pytest
import semantic_drain
from semantic_testkit import _cfg, _llm, _seeded


def test_unverified_empty_target_does_not_fail_or_hide_other_target(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    original = semantic_drain.summarize

    def summarize(llm, bundle, mid, *args, **kwargs):
        result = original(llm, bundle, mid, *args, **kwargs)
        if mid == 1 and isinstance(result, tuple) and result[0] is not None:
            result[0]["claims"] = []
        return result

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    try:
        config = _cfg("assist", budget=0, loop_mode="off")
        for _ in range(2):
            semantic_drain.run_due(db, config, {"errors": []}, time.monotonic() + 300, llm_fn=_llm)
            job = db.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
            assert job["state"] == "pending" and job["attempts"] == 0
            assert not db.artifacts("semantic_audit", message_id=1)
            other = db.artifacts("semantic_audit", message_id=2)
            assert other and json.loads(other[-1]["content"])["status"] == "SKIPPED"
            db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            db.db.commit()
    finally:
        db.close()


def test_unverified_empty_does_not_overwrite_prior_negative(tmp_path, monkeypatch):
    import semantic_audit
    from semantic_jev import JevError
    from semantic_testkit import _FakeJev

    db = _seeded(tmp_path)

    def negative(client, bundle, summary, deadline, *args):
        client.last_error = JevError("protocol_error", "synthetic", retryable=True)
        return [{"code": "claim_contradicts"}, {"code": "support_unevaluated"}], False

    monkeypatch.setattr(semantic_audit, "audit_claims", negative)
    try:
        config = _cfg("assist", budget=100, loop_mode="off")
        semantic_drain.run_due(db, config, {"errors": []}, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=_llm)
        before = db.artifacts("semantic_audit", message_id=1)[-1]
        assert "claim_contradicts" in before["content"]
        db.db.execute("UPDATE artifacts SET content=json_set(content,'$.claims',json('[]')) "
                      "WHERE kind='semantic_summary' AND message_id=1")
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        db.db.commit()
        semantic_drain.run_due(db, _cfg("assist", budget=0, loop_mode="off"),
                               {"errors": []}, time.monotonic() + 300, llm_fn=_llm)
        after = db.artifacts("semantic_audit", message_id=1)[-1]
        assert after["artifact_id"] == before["artifact_id"]
        assert after["content"] == before["content"] and after["meta"] == before["meta"]
    finally:
        db.close()


def test_empty_candidate_stays_held_when_jev_returns(tmp_path, monkeypatch):
    from semantic_testkit import _FakeJev

    db = _seeded(tmp_path)
    original = semantic_drain.summarize

    def summarize(llm, bundle, mid, *args, **kwargs):
        result = original(llm, bundle, mid, *args, **kwargs)
        if mid == 1 and isinstance(result, tuple) and result[0] is not None:
            result[0]["claims"] = []
        return result

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    try:
        semantic_drain.run_due(db, _cfg("assist", budget=0, loop_mode="off"),
                               {"errors": []}, time.monotonic() + 300, llm_fn=_llm)
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        db.db.commit()
        semantic_drain.run_due(db, _cfg("assist", budget=100, loop_mode="off"),
                               {"errors": []}, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=_llm)
        job = db.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert job["state"] == "pending" and job["attempts"] == 0
        assert not db.artifacts("semantic_audit", message_id=1)
    finally:
        db.close()


def test_completed_sibling_notice_is_idempotent_while_empty_target_is_held(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    original = semantic_drain.summarize

    def summarize(llm, bundle, mid, *args, **kwargs):
        result = original(llm, bundle, mid, *args, **kwargs)
        if mid == 1 and isinstance(result, tuple) and result[0] is not None:
            result[0]["claims"] = []
        return result

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    try:
        for _ in range(2):
            semantic_drain.run_due(db, _cfg("enforce", budget=0, loop_mode="off"),
                                   {"errors": []}, time.monotonic() + 300, llm_fn=_llm)
            rows = db.db.execute("SELECT payload FROM notify_outbox WHERE kind='semantic_notice'").fetchall()
            assert [json.loads(row["payload"])["target_message_id"] for row in rows] == [2]
            db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            db.db.commit()
    finally:
        db.close()


def test_held_empty_does_not_create_notice_without_arrival_origin(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    original = semantic_drain.summarize

    def summarize(llm, bundle, mid, *args, **kwargs):
        result = original(llm, bundle, mid, *args, **kwargs)
        if mid == 1 and isinstance(result, tuple) and result[0] is not None:
            result[0]["claims"] = []
        return result

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    try:
        db.db.execute("UPDATE notify_outbox SET state='suppressed' WHERE kind='new_messages'")
        db.db.commit()
        semantic_drain.run_due(db, _cfg("enforce", budget=0, loop_mode="off"),
                               {"errors": []}, time.monotonic() + 300, llm_fn=_llm)
        assert db.artifacts("semantic_audit", message_id=2)
        assert not db.db.execute("SELECT 1 FROM notify_outbox WHERE kind='semantic_notice'").fetchone()
    finally:
        db.close()


def test_other_unfinished_target_prevents_partial_notice(tmp_path, monkeypatch):
    from semantic_testkit import _ledger, _message, _patient

    db = _ledger(tmp_path)
    patient = _patient(db)
    patient.messages = [_message(1)]
    patient.messages[0].replies = [_message(2, parent=1), _message(3, parent=1)]
    db.save_patient(patient, notify={"source": "unread"}, semantic=True)
    original = semantic_drain.summarize

    def summarize(llm, bundle, mid, *args, **kwargs):
        if mid == 3:
            return None, "deadline"
        result = original(llm, bundle, mid, *args, **kwargs)
        if mid == 1 and isinstance(result, tuple) and result[0] is not None:
            result[0]["claims"] = []
        return result

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    try:
        semantic_drain.run_due(db, _cfg("enforce", budget=0, loop_mode="off"),
                               {"errors": []}, time.monotonic() + 300, llm_fn=_llm)
        assert db.artifacts("semantic_audit", message_id=2)
        assert not db.db.execute("SELECT 1 FROM notify_outbox WHERE kind='semantic_notice'").fetchone()
    finally:
        db.close()


@pytest.mark.parametrize("newest_invalidated", [False, True])
def test_completed_pass_is_not_lost_after_sibling_deadline_recovers(tmp_path, monkeypatch, newest_invalidated):
    from semantic_testkit import _FakeJev, _ledger, _message, _patient

    db = _ledger(tmp_path)
    patient = _patient(db)
    patient.messages = [_message(1)]
    patient.messages[0].replies = [_message(2, parent=1), _message(3, parent=1)]
    db.save_patient(patient, notify={"source": "unread"}, semantic=True)
    original, available = semantic_drain.summarize, [False]

    def summarize(llm, bundle, mid, *args, **kwargs):
        if mid == 3 and not available[0]:
            return None, "deadline"
        result = original(llm, bundle, mid, *args, **kwargs)
        if mid == 1 and isinstance(result, tuple) and result[0] is not None:
            result[0]["claims"] = []
        return result

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    try:
        cfg = _cfg("enforce", budget=100, loop_mode="off")
        semantic_drain.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=_llm)
        assert json.loads(db.artifacts("semantic_audit", message_id=2)[-1]["content"])["status"] == "PASS"
        assert not db.db.execute("SELECT 1 FROM notify_outbox WHERE kind='semantic_notice'").fetchone()
        if newest_invalidated:
            db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                          "SELECT kind,project_id,message_id,content,model,json_set(meta,'$.invalidated',1),created_at "
                          "FROM artifacts WHERE kind='semantic_audit' AND message_id=2 ORDER BY artifact_id DESC LIMIT 1")
            db.db.commit()
        available[0] = True
        for _ in range(2):
            db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            db.db.commit()
            semantic_drain.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                                   jev_client=_FakeJev(), llm_fn=_llm)
            rows = db.db.execute("SELECT payload FROM notify_outbox WHERE kind='semantic_notice'").fetchall()
            # An unverified sibling also holds publication of the other one.
            expected = [] if newest_invalidated else [2, 3]
            assert sorted(json.loads(row["payload"])["target_message_id"] for row in rows) == expected
    finally:
        db.close()


def test_empty_repair_keeps_negative_before_later_jev_skip(tmp_path, monkeypatch):
    import semantic_audit
    from semantic_testkit import _FakeJev

    db = _seeded(tmp_path)
    original_summary = semantic_drain.summarize
    original_audit = semantic_audit.audit_claims
    repair_calls = []

    def summarize(llm, bundle, mid, *args, **kwargs):
        result = original_summary(llm, bundle, mid, *args, **kwargs)
        if mid == 1 and kwargs.get("feedback"):
            repair_calls.append(mid)
            if result is not None:
                result["claims"] = []
        return result

    def audit(client, bundle, summary, *args):
        if client is not None and summary["target_message_id"] == 1:
            return [{"code": "claim_not_supported", "claim": "synthetic"}], True
        return original_audit(client, bundle, summary, *args)

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_audit, "audit_claims", audit)
    try:
        semantic_drain.run_due(db, _cfg("assist", budget=100, loop_mode="off"),
                               {"errors": []}, time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm)
        [first] = db.artifacts("semantic_audit", message_id=1)
        assert json.loads(first["content"])["findings"] == [{"code": "claim_not_supported", "claim": "synthetic"}]
        assert json.loads(first["meta"])["repair_count"] == 1
        assert len(db.artifacts("semantic_repair", message_id=1)) == 1
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        db.db.commit()
        semantic_drain.run_due(db, _cfg("assist", budget=0, loop_mode="off"),
                               {"errors": []}, time.monotonic() + 300, llm_fn=_llm)
        current = json.loads(db.artifacts("semantic_audit", message_id=1)[-1]["content"])
        assert current["status"] == "PENDING"
        assert "claim_not_supported" in {item["code"] for item in current["findings"]}
        assert repair_calls == [1]
    finally:
        db.close()
