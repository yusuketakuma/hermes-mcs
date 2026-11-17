"""Synthetic regression for an empty target in a multi-target bundle."""
import semantic_job_processor
import semantic_summary_stages
import json
import time

import pytest
import semantic_drain
from semantic_testkit import _cfg, _llm, _seeded


def test_first_empty_candidate_gets_one_repair_and_real_audit(tmp_path, monkeypatch):
    from semantic_testkit import _FakeJev

    db = _seeded(tmp_path)
    original = semantic_drain.summarize
    calls = []

    def summarize(llm, bundle, mid, *args, **kwargs):
        if mid == 1 and kwargs.get('feedback'):
            import semantic_transport
            assert semantic_transport._inference_purpose.get() == 'backlog'
        result = original(llm, bundle, mid, *args, **kwargs)
        if mid == 1:
            calls.append(bool(kwargs.get('feedback')))
            if not kwargs.get('feedback') and isinstance(result, tuple) and result[0] is not None:
                result[0]['claims'] = []
        return result

    monkeypatch.setattr(semantic_drain, 'summarize', summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
    try:
        cfg = _cfg('assist', budget=100, loop_mode='off')
        semantic_drain.run_due(db, cfg, {'errors': []}, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=_llm)
        assert calls == [False, True]
        assert len(db.artifacts('semantic_repair', message_id=1)) == 1
        audit = db.artifacts('semantic_audit', message_id=1)[-1]
        assert json.loads(audit['content'])['status'] == 'PASS'
        assert json.loads(db.artifacts('semantic_summary', message_id=1)[-1]['content'])['claims']
    finally:
        db.close()


@pytest.mark.parametrize('stage,refunded', [('llm_next_call', True), ('llm_response', False)])
def test_empty_repair_refunds_only_proven_pre_dispatch_stop(tmp_path, monkeypatch, stage, refunded):
    import semantic_runtime
    from semantic_testkit import _FakeJev

    db = _seeded(tmp_path)
    original = semantic_drain.summarize
    repairs = []

    def summarize(llm, bundle, mid, *args, **kwargs):
        if mid == 1 and kwargs.get('feedback'):
            repairs.append(True)
            if len(repairs) == 1:
                raise semantic_runtime.RuntimeBudget(stage)
        result = original(llm, bundle, mid, *args, **kwargs)
        if mid == 1 and not kwargs.get('feedback') and isinstance(result, tuple) and result[0] is not None:
            result[0]['claims'] = []
        return result

    monkeypatch.setattr(semantic_drain, 'summarize', summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
    try:
        cfg = _cfg('assist', budget=100, loop_mode='off')
        semantic_drain.run_due(db, cfg, {'errors': []}, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=_llm)
        assert len(db.artifacts('semantic_repair', message_id=1)) == (0 if refunded else 1)
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        db.db.commit()
        semantic_drain.run_due(db, cfg, {'errors': []}, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=_llm)
        assert len(repairs) == (2 if refunded else 1)
        assert len(db.artifacts('semantic_repair', message_id=1)) == 1
        if not refunded:
            assert not db.artifacts('semantic_audit', message_id=1)
    finally:
        db.close()


def test_empty_repair_never_publishes_after_source_change(tmp_path, monkeypatch):
    from semantic_testkit import _FakeJev, _message

    db = _seeded(tmp_path)
    original = semantic_drain.summarize
    changed = []

    def summarize(llm, bundle, mid, *args, **kwargs):
        result = original(llm, bundle, mid, *args, **kwargs)
        if mid == 1 and kwargs.get('feedback') and not changed:
            db.save_messages([_message(1, body='完全合成の変更後本文。')])
            changed.append(True)
        elif mid == 1 and isinstance(result, tuple) and result[0] is not None:
            result[0]['claims'] = []
        return result

    monkeypatch.setattr(semantic_drain, 'summarize', summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
    try:
        cfg = _cfg('assist', budget=100, loop_mode='off')
        semantic_drain.run_due(db, cfg, {'errors': []}, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=_llm)
        assert changed
        assert not any(json.loads(row['content']).get('status') in ('PASS', 'SKIPPED')
                       for row in db.artifacts('semantic_audit', message_id=1))
        assert not db.db.execute("SELECT 1 FROM notify_outbox WHERE kind='semantic_notice' "
                                 "AND json_extract(payload,'$.target_message_id')=1").fetchone()
    finally:
        db.close()


def test_unverified_empty_target_does_not_fail_or_hide_other_target(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    original = semantic_drain.summarize
    calls = []

    def summarize(llm, bundle, mid, *args, **kwargs):
        result = original(llm, bundle, mid, *args, **kwargs)
        if mid == 1:
            calls.append(bool(kwargs.get('feedback')))
            summary = result[0] if isinstance(result, tuple) else result
            if summary is not None:
                summary['claims'] = []
        return result

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
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
        assert calls == [False, True]
        assert len(db.artifacts('semantic_repair', message_id=1)) == 1
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
        if mid == 1:
            summary = result[0] if isinstance(result, tuple) else result
            if summary is not None:
                summary["claims"] = []
        return result

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
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
        if mid == 1:
            summary = result[0] if isinstance(result, tuple) else result
            if summary is not None:
                summary["claims"] = []
        return result

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
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
        if mid == 1:
            summary = result[0] if isinstance(result, tuple) else result
            if summary is not None:
                summary["claims"] = []
        return result

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
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
        if mid == 1:
            summary = result[0] if isinstance(result, tuple) else result
            if summary is not None:
                summary["claims"] = []
        return result

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
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
        if mid == 1:
            summary = result[0] if isinstance(result, tuple) else result
            if summary is not None:
                summary["claims"] = []
        return result

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
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
            # A sibling PASS whose newest audit cannot verify it is audited
            # again (not held forever); only its new bound pair publishes it.
            assert sorted(json.loads(row["payload"])["target_message_id"] for row in rows) == [2, 3]
        if newest_invalidated:
            audit = db.artifacts("semantic_audit", message_id=2)[-1]
            summary = db.artifacts("semantic_summary", message_id=2)[-1]
            assert json.loads(audit["meta"]).get("invalidated") is None
            assert json.loads(audit["meta"])["summary_artifact_id"] == summary["artifact_id"]
            assert json.loads(audit["content"])["status"] == "PASS"
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
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
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


def _assert_target_unpublished(db, summary_row):
    import semantic_send_gate
    import semantic_store

    meta = json.loads(summary_row["meta"])
    bundle = semantic_store.thread_bundle(db.db, 1, 1)
    assert semantic_send_gate.current_summary(
        db.db, 1, 1, bundle, meta["policy_fingerprint"]) is None
    assert not db.db.execute(
        "SELECT 1 FROM notify_outbox WHERE kind='semantic_notice' "
        "AND json_extract(payload,'$.target_message_id')=1").fetchone()


@pytest.mark.parametrize("oversize", ["char_limit", "backend_rejected"])
def test_oversize_repair_preserves_negative_pair_after_restart(tmp_path, monkeypatch, oversize):
    import mcs_util
    import semantic_audit
    import semantic_llm
    import semantic_runtime
    import semantic_transport
    from semantic_testkit import _FakeJev, _ledger

    db = _seeded(tmp_path)
    original_summary = semantic_drain.summarize
    original_audit = semantic_audit.audit_claims
    summary_calls, audit_calls, llm_calls = [], [], []
    repairing = [False]
    negative = [{"code": "claim_not_supported", "claim": "synthetic"},
                {"code": "claim_contradicts", "claim": "synthetic-negative"}]
    if oversize == "char_limit":
        negative[0]["claim"] += "x" * semantic_llm.PROMPT_CHAR_LIMIT

    def llm(prompt):
        llm_calls.append(repairing[0])
        if repairing[0] and oversize == "backend_rejected":
            raise semantic_runtime.LLMRejected("synthetic_context_limit")
        result = _llm(prompt)
        if result is not None:
            semantic_transport._record_inference("local")
        return result

    def summarize(llm_fn, bundle, mid, *args, **kwargs):
        repairing[0] = mid == 1 and bool(kwargs.get("feedback"))
        if mid == 1:
            summary_calls.append(repairing[0])
        try:
            result = original_summary(llm_fn, bundle, mid, *args, **kwargs)
            if repairing[0]:
                assert result["_input_oversize"] is True
            return result
        finally:
            repairing[0] = False

    def audit(client, bundle, summary, *args):
        findings, evaluated = original_audit(client, bundle, summary, *args)
        if summary["target_message_id"] == 1:
            audit_calls.append(True)
            assert evaluated
            findings += negative
        return findings, evaluated

    def run():
        semantic_drain.run_due(db, _cfg("enforce", budget=100, loop_mode="off"),
                               {"errors": []}, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=llm)

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
    monkeypatch.setattr(semantic_audit, "audit_claims", audit)
    try:
        run()
        before_summary, latest_summary = db.artifacts("semantic_summary", message_id=1)
        before_audit, latest_audit = db.artifacts("semantic_audit", message_id=1)
        before_content = json.loads(before_summary["content"])
        content = json.loads(latest_summary["content"])
        audit_content = json.loads(latest_audit["content"])
        before_meta, meta = json.loads(before_audit["meta"]), json.loads(latest_audit["meta"])
        proof = json.loads(latest_summary["meta"])["provider_provenance"]
        assert before_content["claims"]
        assert content == {**before_content, "audit_status": "NEEDS_REVIEW"}
        assert audit_content["status"] == "NEEDS_REVIEW"
        assert audit_content["findings"] == json.loads(before_audit["content"])["findings"] + [
            {"code": "repair_unavailable"}]
        assert all(finding in audit_content["findings"] for finding in negative)
        assert meta["claim_input_binding"] == before_meta["claim_input_binding"]
        assert meta["claim_audit"] == before_meta["claim_audit"] and meta["claim_audit"]
        assert meta["summary_artifact_id"] == latest_summary["artifact_id"]
        assert meta["summary_hash"] == mcs_util.payload_hash(content)
        assert proof == {**json.loads(before_summary["meta"])["provider_provenance"],
                         "result_hash": mcs_util.payload_hash(content)}
        assert proof["providers"] == ["local"] and proof["complete"] is True
        assert meta["repair_count"] == 1
        assert len(db.artifacts("semantic_repair", message_id=1)) == 1
        assert summary_calls == [False, True] and audit_calls == [True]
        assert llm_calls.count(True) == (oversize == "backend_rejected")
        _assert_target_unpublished(db, latest_summary)
        saved_summary, saved_audit = dict(latest_summary), dict(latest_audit)
        calls_before_restart = list(llm_calls)
        db.close()
        db = _ledger(tmp_path)
        for _ in range(2):
            db.db.execute("UPDATE fetch_jobs SET state='pending',next_try=0 WHERE kind='semantic'")
            db.db.commit()
            run()
            assert dict(db.artifacts("semantic_summary", message_id=1)[-1]) == saved_summary
            assert dict(db.artifacts("semantic_audit", message_id=1)[-1]) == saved_audit
            assert llm_calls == calls_before_restart
            assert summary_calls == [False, True] and audit_calls == [True]
            assert len(db.artifacts("semantic_repair", message_id=1)) == 1
            _assert_target_unpublished(db, saved_summary)
    finally:
        db.close()


@pytest.mark.parametrize("interruption", ["unavailable", "budget", "not_sent"])
def test_empty_repair_candidate_survives_interrupted_audit(
        tmp_path, monkeypatch, interruption):
    import copy
    import semantic_audit
    import semantic_runtime
    import semantic_transport
    from semantic_jev import JevError
    from semantic_testkit import _FakeJev

    db = _seeded(tmp_path)
    original_obtain = semantic_drain._obtain_summary
    original_summary = semantic_drain.summarize
    original_audit = semantic_audit.audit_claims
    phase = ["negative"]
    injected = []
    interrupted = []
    summary_calls = []
    marker = "synthetic-recovered-repair"

    def llm(prompt):
        result = _llm(prompt)
        if result is not None:
            semantic_transport._record_inference("local")
        return result

    def summarize(llm_fn, bundle, mid, *args, **kwargs):
        result = original_summary(llm_fn, bundle, mid, *args, **kwargs)
        if mid == 1:
            repair = bool(kwargs.get("feedback"))
            summary_calls.append(repair)
            if repair and result is not None:
                result["limitations"] = [marker]
        return result

    def obtain(*args, **kwargs):
        result = original_obtain(*args, **kwargs)
        if phase[0] == "repair" and args[6] == 1 and not injected:
            summary, reason, error, sent_error, producer = result
            assert summary is not None
            summary = copy.deepcopy(summary)
            summary["claims"] = []
            injected.append(True)
            return summary, reason, error, sent_error, producer
        return result

    def audit(client, bundle, summary, *args):
        if summary["target_message_id"] == 1:
            if phase[0] == "negative":
                client.last_error = JevError("protocol_error", "synthetic", retryable=True)
                return [{"code": "claim_contradicts", "claim": "synthetic"},
                        {"code": "support_unevaluated"}], False
            if phase[0] == "repair":
                assert summary["claims"] and summary["limitations"] == [marker]
                interrupted.append(True)
                if interruption == "budget":
                    raise semantic_runtime.RuntimeBudget("synthetic_audit")
                if interruption == "not_sent":
                    raise semantic_runtime.LLMNotSent("synthetic_audit")
                client.last_error = JevError("transport", "synthetic", retryable=True)
                return [{"code": "support_unevaluated"}], False
        return original_audit(client, bundle, summary, *args)

    def retry():
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        db.db.commit()

    def run(cfg):
        return semantic_drain.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                                      jev_client=_FakeJev(), llm_fn=llm)

    def sibling_notices():
        return [tuple(row) for row in db.db.execute(
            "SELECT event_id,payload FROM notify_outbox WHERE kind='semantic_notice' "
            "AND json_extract(payload,'$.target_message_id')=2 ORDER BY event_id")]

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
    monkeypatch.setattr(semantic_drain, "_obtain_summary", obtain)
    monkeypatch.setattr(semantic_job_processor, '_obtain_summary', semantic_drain._obtain_summary)
    monkeypatch.setattr(semantic_audit, "audit_claims", audit)
    try:
        cfg = _cfg("enforce", budget=100, loop_mode="off")
        run(cfg)
        old_audit = db.artifacts("semantic_audit", message_id=1)[-1]
        old_summary = db.artifacts("semantic_summary", message_id=1)[-1]
        assert json.loads(old_audit["content"])["status"] == "PENDING"
        assert "claim_contradicts" in old_audit["content"]
        assert not db.artifacts("semantic_repair", message_id=1)
        before_notices = sibling_notices()
        assert len(before_notices) <= 1

        phase[0] = "repair"
        retry()
        run(cfg)
        assert injected and interrupted
        for kind, before in (("semantic_audit", old_audit), ("semantic_summary", old_summary)):
            after = db.artifacts(kind, message_id=1)[-1]
            assert (after["artifact_id"], after["content"], after["meta"]) == (
                before["artifact_id"], before["content"], before["meta"])
        assert len(db.artifacts("semantic_repair", message_id=1)) == 1
        assert summary_calls == [False, True]
        # The repair itself must survive the interrupted audit in persistent
        # storage; the original pair above remains unchanged.
        assert db.db.execute(
            "SELECT 1 FROM artifacts WHERE project_id=1 AND message_id=1 AND content LIKE ?",
            (f"%{marker}%",)).fetchone()
        during_notices = sibling_notices()
        assert len(during_notices) <= 1
        assert not before_notices or during_notices == before_notices

        phase[0] = "recovered"
        retry()
        run(cfg)
        current_audit = json.loads(db.artifacts("semantic_audit", message_id=1)[-1]["content"])
        current_summary = json.loads(db.artifacts("semantic_summary", message_id=1)[-1]["content"])
        assert current_audit["status"] == "PASS"
        assert current_summary["claims"] and current_summary["limitations"] == [marker]
        assert summary_calls == [False, True]
        assert len(db.artifacts("semantic_repair", message_id=1)) == 1
        after_notices = sibling_notices()
        assert len(after_notices) <= 1
        assert not during_notices or after_notices == during_notices
        retry()
        run(cfg)
        assert sibling_notices() == after_notices
        assert summary_calls == [False, True]
        assert len(db.artifacts("semantic_repair", message_id=1)) == 1
    finally:
        db.close()


class _SyntheticCrash(BaseException):
    """The process dies mid-call: nothing after it in the pass runs."""


_STOP_PHASES = ("paused_after_answer", "paused_after_verdict", "stale_after_answer")
# A stop after only answers that raise no finding: nothing negative to keep.
_POSITIVE_STOP_PHASES = {"paused_after_supports": "off", "budget_after_supports": "budget"}
_LOW_CONFIDENCE = "paused_after_low_confidence"


@pytest.fixture
def interrupted_empty_repair(tmp_path, monkeypatch):
    """Create a bound negative pair and an interrupted, nonempty repair."""
    import copy
    import semantic_audit
    import semantic_runtime
    import semantic_transport
    from semantic_jev import JevError
    from semantic_testkit import _FakeJev, _ledger
    from types import SimpleNamespace

    state = SimpleNamespace(db=_seeded(tmp_path), phase="negative", summaries=[], audits=[], llm_calls=0)
    original_obtain = semantic_drain._obtain_summary
    original_summary = semantic_drain.summarize
    original_audit = semantic_audit.audit_claims
    injected = []

    def llm(prompt):
        state.llm_calls += 1
        result = _llm(prompt)
        if result is not None:
            semantic_transport._record_inference("local")
        return result

    def summarize(llm_fn, bundle, mid, *args, **kwargs):
        result = original_summary(llm_fn, bundle, mid, *args, **kwargs)
        if mid == 1:
            state.summaries.append(bool(kwargs.get("feedback")))
            if kwargs.get("feedback") and result is not None:
                result["limitations"] = ["synthetic-interrupted-repair"]
        return result

    def obtain(*args, **kwargs):
        result = original_obtain(*args, **kwargs)
        if (state.phase in ("partial", "unavailable", "finding_only", "crash", "two_claims", *_STOP_PHASES,
                            *_POSITIVE_STOP_PHASES, _LOW_CONFIDENCE)
                and args[6] == 1 and not injected):
            summary, reason, error, sent_error, producer = result
            assert summary is not None
            summary = copy.deepcopy(summary)
            summary["claims"] = []
            injected.append(True)
            return summary, reason, error, sent_error, producer
        return result

    def audit(client, bundle, summary, *args):
        if summary["target_message_id"] == 1:
            state.audits.append(state.phase)
            if state.phase == "negative":
                client.last_error = JevError("protocol_error", "synthetic", retryable=True)
                return [{"code": "claim_contradicts", "claim": "synthetic"},
                        {"code": "support_unevaluated"}], False
            if state.phase == "unavailable":
                client.last_error = JevError("transport", "synthetic", retryable=True)
                return [{"code": "support_unevaluated"}], False
            if state.phase == "crash":
                raise _SyntheticCrash()
            if state.phase in _STOP_PHASES:
                # A contradicting answer is observed in memory, then the pass
                # stops: a pause before the next Jev batch, or (stale) after
                # another worker took the job, or a pause after the verdict.
                claim_id = summary["claims"][0]["claim_id"]
                summary["_claim_audit"] = {claim_id: {"choice": "contradicts", "confidence": 0.9}}
                if state.phase == "stale_after_answer":
                    state.db.db.execute("UPDATE fetch_jobs SET attempts=attempts+1 WHERE kind='semantic'")
                    state.db.db.commit()
                    raise semantic_runtime.RuntimeStale("jev_attempt")
                state.db.artifact_add("semantic_control", "paused", project_id=1)
                if state.phase == "paused_after_answer":
                    raise semantic_runtime.RuntimeOff("jev_attempt")
                return [{"code": "claim_contradicts", "claim": claim_id, "confidence": 0.9}], True
            if state.phase in _POSITIVE_STOP_PHASES or state.phase == _LOW_CONFIDENCE:
                # Only a `supports` answer was observed when the pass stops:
                # a confident one (nothing to hold) or one below the match
                # threshold (an adverse finding).
                claim_id = summary["claims"][0]["claim_id"]
                summary["_claim_audit"] = {claim_id: {
                    "choice": "supports", "confidence": 0.05 if state.phase == _LOW_CONFIDENCE else 0.95}}
                if _POSITIVE_STOP_PHASES.get(state.phase) == "budget":
                    raise semantic_runtime.RuntimeBudget("jev_attempt")
                state.db.artifact_add("semantic_control", "paused", project_id=1)
                raise semantic_runtime.RuntimeOff("jev_attempt")
            if state.phase == "finding_only":
                assert not summary.get("_claim_audit")
                client.last_error = JevError("transport", "synthetic", retryable=True)
                return [{"code": "claim_contradicts", "claim": "synthetic-repair"},
                        {"code": "support_unevaluated"}], False
        return original_audit(client, bundle, summary, *args)

    class PartialJev(_FakeJev):
        def evaluate(self, target, questions, deadline):
            if target.get("target", {}).get("id") == "claims":
                self.requests_made += 1
                claim_id = next(iter(questions))
                error = JevError("transport", "synthetic", retryable=True)
                error.partial_answers = {claim_id: {
                    "type": "choice", "choice": "contradicts", "confidence": 0.9,
                    "probabilities": {option: 1.0 / len(questions[claim_id]["criteria"])
                                      for option in questions[claim_id]["criteria"]}}}
                raise error
            return super().evaluate(target, questions, deadline)

    def run(jev=True, client=None, **settings):
        if client is None and jev:
            client = PartialJev() if state.phase == "partial" else _FakeJev()
        return semantic_drain.run_due(state.db, _cfg("enforce", budget=100, loop_mode="off", **settings),
                                      {"errors": []}, time.monotonic() + 300,
                                      jev_client=client, llm_fn=llm)

    def retry():
        state.db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        state.db.db.commit()

    def restart():
        state.db.close()
        state.db = _ledger(tmp_path)

    state.run, state.retry, state.restart = run, retry, restart
    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
    monkeypatch.setattr(semantic_drain, "_obtain_summary", obtain)
    monkeypatch.setattr(semantic_job_processor, '_obtain_summary', semantic_drain._obtain_summary)
    monkeypatch.setattr(semantic_audit, "audit_claims", audit)
    try:
        run()
        state.old_audit = dict(state.db.artifacts("semantic_audit", message_id=1)[-1])
        state.old_summary = dict(state.db.artifacts("semantic_summary", message_id=1)[-1])
        assert json.loads(state.old_audit["content"])["status"] == "PENDING"
        assert "claim_contradicts" in state.old_audit["content"]
        assert not state.db.artifacts("semantic_repair", message_id=1)
        yield state
    finally:
        state.db.close()


def test_oversize_empty_repair_keeps_prior_pair_after_restart(interrupted_empty_repair, monkeypatch):
    import semantic_llm

    state = interrupted_empty_repair
    original_summary = semantic_drain.summarize
    oversize_results = []

    def summarize(llm, bundle, mid, *args, **kwargs):
        repair = mid == 1 and bool(kwargs.get("feedback"))
        if repair:
            kwargs["feedback"] = [*kwargs["feedback"], "x" * semantic_llm.PROMPT_CHAR_LIMIT]
        result = original_summary(llm, bundle, mid, *args, **kwargs)
        if repair:
            assert result["_input_oversize"] is True
            oversize_results.append(True)
        return result

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
    state.phase = "unavailable"
    llm_calls = state.llm_calls
    state.retry()
    state.run()

    def assert_held():
        assert dict(state.db.artifacts("semantic_summary", message_id=1)[-1]) == state.old_summary
        assert dict(state.db.artifacts("semantic_audit", message_id=1)[-1]) == state.old_audit
        assert state.llm_calls == llm_calls
        assert state.summaries == [False, True] and state.audits == ["negative"]
        assert oversize_results == [True]
        [repair] = state.db.artifacts("semantic_repair", message_id=1)
        assert json.loads(repair["meta"])["empty_repair"] is True
        assert state.db.job_state("semantic", 1, 1) == "pending"
        _assert_target_unpublished(state.db, state.old_summary)

    assert_held()
    state.restart()
    state.phase = "recovered"
    for _ in range(2):
        state.retry()
        state.run()
        assert_held()


def _assert_resumed_needs_review(state, answers, llm_calls, adverse="claim_contradicts"):
    """The recorded negative answers were committed as NEEDS_REVIEW: never a PASS, no new summary."""
    assert state.db.job_state("semantic", 1, 1) == "done"
    assert state.llm_calls == llm_calls and state.summaries == [False, True]
    assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
    statuses = [json.loads(row["content"])["status"]
                for row in state.db.artifacts("semantic_audit", message_id=1)]
    assert "PASS" not in statuses and statuses[-1] == "NEEDS_REVIEW"
    audit = state.db.artifacts("semantic_audit", message_id=1)[-1]
    summary = state.db.artifacts("semantic_summary", message_id=1)[-1]
    content, meta = json.loads(audit["content"]), json.loads(audit["meta"])
    assert adverse in [finding["code"] for finding in content["findings"]]
    assert meta["claim_audit"] == answers
    assert meta["summary_artifact_id"] == summary["artifact_id"] and meta["repair_count"] == 1


def test_partial_negative_empty_repair_resumes_to_needs_review_after_restart(interrupted_empty_repair):
    """A Jev error after a contradicting answer records it; the resumed audit commits NEEDS_REVIEW.

    The one claim is already answered, so nothing is asked again: the
    recorded answer is committed as it was observed."""
    state = interrupted_empty_repair
    state.phase = "partial"
    state.retry()
    state.run()
    assert state.summaries == [False, True]
    assert state.audits == ["negative", "partial"]
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    meta = json.loads(candidate["meta"])
    assert meta["audit_state"] == "held"
    partial = meta["partial_answers"]
    assert len(partial["answers"]) == 1 and "evaluated" not in partial
    answer = next(iter(partial["answers"].values()))
    assert answer["choice"] == "contradicts" and answer["confidence"] == 0.9
    assert any(finding["code"] == "claim_contradicts" for finding in partial["findings"])
    assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
    assert dict(state.db.artifacts("semantic_audit", message_id=1)[-1]) == state.old_audit
    llm_calls = state.llm_calls

    state.restart()
    state.phase = "recovered"
    for _ in range(3):
        state.retry()
        state.run()
        if state.db.job_state("semantic", 1, 1) != "pending":
            break
    assert state.audits == ["negative", "partial"]
    _assert_resumed_needs_review(state, partial["answers"], llm_calls)


def test_finding_without_partial_answer_keeps_empty_repair_held(interrupted_empty_repair):
    state = interrupted_empty_repair
    state.phase = "finding_only"
    state.retry()
    state.run()
    assert state.summaries == [False, True]
    assert state.audits == ["negative", "finding_only"]
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    meta = json.loads(candidate["meta"])
    assert meta["audit_state"] == "held"
    assert meta["partial_answers"]["answers"] == {}
    assert meta["partial_answers"]["findings"] == [
        {"code": "claim_contradicts", "claim": "synthetic-repair"}]
    assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
    llm_calls = state.llm_calls

    state.restart()
    state.phase = "recovered"
    for _ in range(2):
        state.retry()
        state.run()
        assert state.llm_calls == llm_calls
        assert state.summaries == [False, True]
        assert state.audits == ["negative", "finding_only"]
        assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
        current = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
        assert (current["artifact_id"], current["content"], current["meta"]) == (
            candidate["artifact_id"], candidate["content"], candidate["meta"])
        audit = state.db.artifacts("semantic_audit", message_id=1)[-1]
        assert (audit["artifact_id"], audit["content"], audit["meta"]) == (
            state.old_audit["artifact_id"], state.old_audit["content"], state.old_audit["meta"])
        job = state.db.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert job["state"] == "pending"
        assert not state.db.db.execute(
            "SELECT 1 FROM notify_outbox WHERE kind='semantic_notice' "
            "AND json_extract(payload,'$.target_message_id')=1").fetchone()


def test_crash_during_empty_repair_audit_reaudits_after_restart(interrupted_empty_repair, monkeypatch):
    """A process that dies while the repaired text is audited leaves no answer.

    The recorded ``inflight`` state has no observed answer, so the repaired
    text is audited again (no new summary, no second repair) instead of
    waiting forever; it is published only from that new audit."""
    state = interrupted_empty_repair
    state.phase = "crash"
    state.retry()
    with pytest.raises(_SyntheticCrash):
        state.run()
    assert state.summaries == [False, True] and state.audits == ["negative", "crash"]
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    meta = json.loads(candidate["meta"])
    assert meta["audit_state"] == "inflight" and meta["partial_answers"] == {}
    llm_calls = state.llm_calls

    state.restart()
    state.db.db.execute("UPDATE fetch_jobs SET state='pending' WHERE kind='semantic'")
    state.db.db.commit()
    state.phase = "recovered"
    reasons = []
    real_why = semantic_job_processor._why
    monkeypatch.setattr(semantic_job_processor, "_why",
                        lambda diagnostics, reason: reasons.append(reason) or real_why(diagnostics, reason))
    for _ in range(3):
        state.retry()
        state.run()
        if state.db.job_state("semantic", 1, 1) != "pending":
            break
    assert "empty_repair_audit_not_ready" not in reasons
    assert state.db.job_state("semantic", 1, 1) == "done"
    assert state.llm_calls == llm_calls
    assert state.summaries == [False, True]
    assert state.audits == ["negative", "crash", "recovered"]
    assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
    audit = state.db.artifacts("semantic_audit", message_id=1)[-1]
    summary = state.db.artifacts("semantic_summary", message_id=1)[-1]
    assert json.loads(audit["content"])["status"] == "PASS"
    assert json.loads(summary["content"])["limitations"] == ["synthetic-interrupted-repair"]
    assert json.loads(audit["meta"])["summary_artifact_id"] == summary["artifact_id"]


def _notice_for_target(db, mid=1):
    return db.db.execute(
        "SELECT 1 FROM notify_outbox WHERE kind='semantic_notice' "
        "AND json_extract(payload,'$.target_message_id')=?", (mid,)).fetchone()


@pytest.mark.parametrize("phase,adverse", [("paused_after_answer", "claim_contradicts"),
                                           (_LOW_CONFIDENCE, "claim_low_confidence")])
def test_negative_answer_observed_before_a_stop_is_committed_as_needs_review(interrupted_empty_repair,
                                                                           phase, adverse):
    """A negative answer seen before a pause is recorded, so a later audit cannot outvote it.

    The pause stops the pass before the next Jev batch, with no verdict. The
    observed answer (a contradiction, or a ``supports`` below the match
    threshold) holds the candidate. The only claim is already answered, so
    the resumed audit asks nothing (with Jev still skipped, too): the record
    is committed as observed, as NEEDS_REVIEW, never a PASS."""
    state = interrupted_empty_repair
    state.phase = phase
    state.retry()
    state.run()
    assert state.summaries == [False, True] and state.audits == ["negative", phase]
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    meta = json.loads(candidate["meta"])
    assert meta["audit_state"] == "held"
    assert len(meta["partial_answers"]["answers"]) == 1
    assert [finding["code"] for finding in meta["partial_answers"]["findings"]] == [adverse]
    assert "evaluated" not in meta["partial_answers"]
    assert json.loads(state.db.artifacts("semantic_audit", message_id=1)[-1]["content"])["status"] == "PENDING"
    llm_calls = state.llm_calls

    state.db.artifact_add("semantic_control", "running", project_id=1)
    state.restart()
    state.phase = "recovered"
    for _ in range(3):
        state.retry()
        state.run(jev=False)
        if state.db.job_state("semantic", 1, 1) != "pending":
            break
    assert state.audits == ["negative", phase]
    _assert_resumed_needs_review(state, meta["partial_answers"]["answers"], llm_calls, adverse)


@pytest.mark.parametrize("jev_back", [True, False])
def test_evaluated_negative_verdict_stopped_before_commit_commits_needs_review(interrupted_empty_repair,
                                                                              jev_back):
    """A pause after an evaluated negative verdict, before its commit, ends in NEEDS_REVIEW.

    The verdict and its finding are recorded with the candidate. After
    resuming, with Jev back or still skipped, that recorded verdict takes the
    commit path: no new audit, no new local-model call, and the negative is
    surfaced for review (never a PASS); the committed result then consumes
    the reservation."""
    state = interrupted_empty_repair
    state.phase = "paused_after_verdict"
    state.retry()
    state.run()
    assert state.summaries == [False, True] and state.audits == ["negative", "paused_after_verdict"]
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    meta = json.loads(candidate["meta"])
    assert meta["audit_state"] == "held"
    assert meta["partial_answers"]["evaluated"] is True
    assert [answer["choice"] for answer in meta["partial_answers"]["answers"].values()] == ["contradicts"]
    assert [finding["code"] for finding in meta["partial_answers"]["findings"]] == ["claim_contradicts"]
    assert dict(state.db.artifacts("semantic_audit", message_id=1)[-1]) == state.old_audit
    assert not _notice_for_target(state.db)
    llm_calls = state.llm_calls

    state.db.artifact_add("semantic_control", "running", project_id=1)
    state.restart()
    state.phase = "recovered"
    for _ in range(3):
        state.retry()
        state.run(jev=jev_back)
        if state.db.job_state("semantic", 1, 1) != "pending":
            break
    assert state.llm_calls == llm_calls and state.summaries == [False, True]
    assert state.audits == ["negative", "paused_after_verdict"]
    assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
    audit = state.db.artifacts("semantic_audit", message_id=1)[-1]
    summary = state.db.artifacts("semantic_summary", message_id=1)[-1]
    content, audit_meta = json.loads(audit["content"]), json.loads(audit["meta"])
    assert content["status"] == "NEEDS_REVIEW"
    assert "claim_contradicts" in [finding["code"] for finding in content["findings"]]
    assert audit_meta["summary_artifact_id"] == summary["artifact_id"]
    assert audit_meta["repair_count"] == 1
    assert [answer["choice"] for answer in audit_meta["claim_audit"].values()] == ["contradicts"]
    assert json.loads(summary["content"])["claims"] == json.loads(candidate["content"])["claims"]
    assert state.db.job_state("semantic", 1, 1) == "done"
    audits = len(state.db.artifacts("semantic_audit", message_id=1))
    state.retry()
    state.run()
    assert len(state.db.artifacts("semantic_audit", message_id=1)) == audits
    assert state.audits == ["negative", "paused_after_verdict"]


def test_replay_crash_in_commit_keeps_recorded_negative_verdict(interrupted_empty_repair, monkeypatch):
    """A crash inside the commit of a replayed negative verdict loses nothing.

    The replay does not rewrite the candidate before its commit, so after the
    crash the recorded verdict is still there; the next run commits it as
    NEEDS_REVIEW (no new audit, never a PASS from a fresh audit)."""
    import semantic_notice

    state = interrupted_empty_repair
    state.phase = "paused_after_verdict"
    state.retry()
    state.run()
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    assert json.loads(candidate["meta"])["partial_answers"]["evaluated"] is True
    state.db.artifact_add("semantic_control", "running", project_id=1)
    llm_calls = state.llm_calls
    state.restart()

    real_plan = semantic_notice.record_notice_plan_tx
    crashes = []

    def crash_once(ledger, pid, root, mid, *args, **kwargs):
        if mid == 1 and not crashes:
            crashes.append(True)
            raise _SyntheticCrash()
        return real_plan(ledger, pid, root, mid, *args, **kwargs)

    monkeypatch.setattr(semantic_notice, "record_notice_plan_tx", crash_once)
    state.phase = "recovered"
    state.retry()
    with pytest.raises(_SyntheticCrash):
        state.run()
    assert crashes == [True]
    current = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    assert (current["artifact_id"], current["meta"]) == (candidate["artifact_id"], candidate["meta"])
    assert dict(state.db.artifacts("semantic_audit", message_id=1)[-1]) == state.old_audit
    assert not _notice_for_target(state.db)

    state.restart()
    state.db.db.execute("UPDATE fetch_jobs SET state='pending' WHERE kind='semantic'")
    state.db.db.commit()
    for _ in range(3):
        state.retry()
        state.run()
        if state.db.job_state("semantic", 1, 1) != "pending":
            break
    assert state.db.job_state("semantic", 1, 1) == "done"
    assert state.llm_calls == llm_calls and state.summaries == [False, True]
    assert state.audits == ["negative", "paused_after_verdict"]
    statuses = [json.loads(row["content"])["status"]
                for row in state.db.artifacts("semantic_audit", message_id=1)]
    assert "PASS" not in statuses and statuses[-1] == "NEEDS_REVIEW"
    audit = state.db.artifacts("semantic_audit", message_id=1)[-1]
    assert "claim_contradicts" in [finding["code"] for finding in json.loads(audit["content"])["findings"]]


def _two_claim_repair(monkeypatch):
    """The repaired text carries two claims, so its audit can stop between them."""
    original = semantic_drain.summarize

    def summarize(llm_fn, bundle, mid, *args, **kwargs):
        result = original(llm_fn, bundle, mid, *args, **kwargs)
        if mid == 1 and kwargs.get("feedback") and isinstance(result, dict) and result.get("claims"):
            second = json.loads(json.dumps(result["claims"][0]))
            second["claim_id"] += "_second"
            second["text"] += "（合成の二件目）"
            result["claims"].append(second)
        return result

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_summary_stages, "summarize", semantic_drain.summarize)


def _claim_jev(state, stop=None):
    """Jev that answers ``contradicts`` to the first claim batch, then stops on the second.

    Without ``stop`` it answers every claim ``supports``. Claim batches it
    received are kept in ``claims``."""
    import semantic_runtime
    from semantic_testkit import _FakeJev

    class ClaimJev(_FakeJev):
        claims = []

        def evaluate(self, target, questions, deadline):
            if target.get("target", {}).get("id") == "claims":
                self.claims.append(sorted(questions))
                if stop is not None and len(self.claims) == 2:
                    self.requests_made += 1
                    if stop == "budget":
                        raise semantic_runtime.RuntimeBudget("jev_attempt")
                    state.db.artifact_add("semantic_control", "paused", project_id=1)
                    raise semantic_runtime.RuntimeOff("jev_attempt")
                if stop is not None:
                    out = super().evaluate(target, questions, deadline)
                    for answer in out["answers"].values():
                        answer["choice"] = "contradicts"
                    return out
            return super().evaluate(target, questions, deadline)

    client = ClaimJev()
    client.claims = []
    return client


@pytest.mark.parametrize("stop", ["off", "budget"])
def test_negative_answer_before_a_stop_resumes_audit_to_needs_review(interrupted_empty_repair, monkeypatch, stop):
    """A negative answer seen before a stop, with no verdict, is completed, not held forever.

    The first claim answers ``contradicts``, then the call for the second
    claim stops (pause or budget). While Jev is skipped the candidate waits
    with its record. With Jev back, only the unanswered claim is asked; the
    recorded answer is reused as it was (never asked again or upgraded) and
    the full audit commits NEEDS_REVIEW, never a PASS."""
    state = interrupted_empty_repair
    _two_claim_repair(monkeypatch)
    state.phase = "two_claims"
    stopping = _claim_jev(state, stop)
    state.retry()
    state.run(client=stopping, max_questions_per_request=1)
    assert state.summaries == [False, True] and len(stopping.claims) == 2
    first, second = stopping.claims[0][0], stopping.claims[1][0]
    assert first != second
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    meta = json.loads(candidate["meta"])
    assert meta["audit_state"] == "held" and "evaluated" not in meta["partial_answers"]
    assert meta["partial_answers"]["answers"] == {first: {"choice": "contradicts", "confidence": 0.9}}
    assert [finding["code"] for finding in meta["partial_answers"]["findings"]] == ["claim_contradicts"]
    assert dict(state.db.artifacts("semantic_audit", message_id=1)[-1]) == state.old_audit
    assert not _notice_for_target(state.db)
    llm_calls = state.llm_calls

    state.db.artifact_add("semantic_control", "running", project_id=1)
    state.restart()
    for _ in range(2):
        # Jev skipped: nothing can be evaluated, the record waits unchanged.
        state.retry()
        state.run(jev=False, max_questions_per_request=1)
        current = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
        assert (current["artifact_id"], current["meta"]) == (candidate["artifact_id"], candidate["meta"])
        assert dict(state.db.artifacts("semantic_audit", message_id=1)[-1]) == state.old_audit
        assert state.db.job_state("semantic", 1, 1) == "pending"
        assert not _notice_for_target(state.db)

    resumed = _claim_jev(state)
    for _ in range(3):
        state.retry()
        state.run(client=resumed, max_questions_per_request=1)
        if state.db.job_state("semantic", 1, 1) != "pending":
            break
    assert resumed.claims == [[second]]
    assert state.db.job_state("semantic", 1, 1) == "done"
    assert state.llm_calls == llm_calls and state.summaries == [False, True]
    assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
    statuses = [json.loads(row["content"])["status"]
                for row in state.db.artifacts("semantic_audit", message_id=1)]
    assert "PASS" not in statuses and statuses[-1] == "NEEDS_REVIEW"
    audit = state.db.artifacts("semantic_audit", message_id=1)[-1]
    summary = state.db.artifacts("semantic_summary", message_id=1)[-1]
    content, audit_meta = json.loads(audit["content"]), json.loads(audit["meta"])
    assert {"code": "claim_contradicts", "claim": first, "confidence": 0.9} in content["findings"]
    assert audit_meta["claim_audit"] == {first: {"choice": "contradicts", "confidence": 0.9},
                                         second: {"choice": "supports", "confidence": 0.9}}
    assert audit_meta["summary_artifact_id"] == summary["artifact_id"]
    assert audit_meta["repair_count"] == 1
    assert [claim["claim_id"] for claim in json.loads(summary["content"])["claims"]] == [first, second]
    audits = len(state.db.artifacts("semantic_audit", message_id=1))
    state.retry()
    state.run(client=resumed, max_questions_per_request=1)
    assert len(state.db.artifacts("semantic_audit", message_id=1)) == audits
    assert resumed.claims == [[second]]


def _defective_batch_jev(state, first=None, reask="stop"):
    """Jev that answers a two-claim batch with ``first`` for the first claim and an invalid second answer.

    ``first`` is a choice or ``"invalid"``; an invalid answer fails Jev
    validation, so ``evaluate_once_more`` asks it again. That re-ask stops
    (``reask="stop"``, a pause) or is invalid again (``"invalid"``).
    Without ``first`` every claim is answered ``supports``. Claim batches
    it received are kept in ``claims``."""
    import semantic_runtime
    from semantic_jev import JevError
    from semantic_testkit import _FakeJev

    class DefectiveJev(_FakeJev):
        claims = []

        def evaluate(self, target, questions, deadline):
            if target.get("target", {}).get("id") != "claims":
                return super().evaluate(target, questions, deadline)
            self.claims.append(sorted(questions))
            out = super().evaluate(target, questions, deadline)
            if first is None:
                return out
            if len(self.claims) > 1 and reask == "stop":
                state.db.artifact_add("semantic_control", "paused", project_id=1)
                raise semantic_runtime.RuntimeOff("jev_attempt")
            ids = sorted(questions)
            choices = dict(zip(ids, [first, "invalid"])) if len(self.claims) == 1 else {}
            valid, failed = {}, []
            for qid in ids:
                choice = choices.get(qid, "invalid")
                if choice == "invalid":
                    failed.append(qid)
                else:
                    valid[qid] = {"type": "choice", "choice": choice, "confidence": 0.9,
                                  "distribution": out["answers"][qid]["probabilities"]}
            error = JevError("protocol_error", f"{failed[0]}:synthetic_choice_invalid")
            error.answers, error.failed = valid, failed
            raise error

    client = DefectiveJev()
    client.claims = []
    return client


@pytest.mark.parametrize("first,reask,held", [
    ("contradicts", "stop", True), ("contradicts", "invalid", True),
    ("invalid", "stop", False), ("supports", "stop", False)])
def test_valid_answers_beside_an_invalid_one_are_recorded_before_the_reask(
        interrupted_empty_repair, monkeypatch, first, reask, held):
    """Answers that passed validation are recorded before their invalid sibling is asked again.

    One batch answers the first claim ``first`` and the second with an
    invalid choice; the re-ask of the invalid claims stops (pause) or is
    invalid again. A valid negative answer is recorded and holds the
    candidate: the resumed audit asks only the other claim and commits
    NEEDS_REVIEW, never a PASS from a later supporting answer. An invalid
    answer is not a judgement of the claim, so with no negative answer the
    candidate is ``ready`` and the next pass audits every claim again; a
    PASS then comes only from that complete audit."""
    state = interrupted_empty_repair
    _two_claim_repair(monkeypatch)
    state.phase = "two_claims"
    stopping = _defective_batch_jev(state, first, reask)
    state.retry()
    state.run(client=stopping, max_questions_per_request=2)
    assert state.summaries == [False, True] and len(stopping.claims) == 2
    claim_a, claim_b = stopping.claims[0]
    assert stopping.claims[1] == ([claim_b] if first != "invalid" else [claim_a, claim_b])
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    meta = json.loads(candidate["meta"])
    if held:
        assert meta["audit_state"] == "held" and "evaluated" not in meta["partial_answers"]
        assert meta["partial_answers"]["answers"] == {claim_a: {"choice": "contradicts", "confidence": 0.9}}
        assert [f["code"] for f in meta["partial_answers"]["findings"]] == ["claim_contradicts"]
    else:
        assert meta["audit_state"] == "ready" and meta["partial_answers"] == {}
    assert dict(state.db.artifacts("semantic_audit", message_id=1)[-1]) == state.old_audit
    assert not _notice_for_target(state.db)

    state.db.artifact_add("semantic_control", "running", project_id=1)
    state.restart()
    resumed = _defective_batch_jev(state)
    for _ in range(3):
        state.retry()
        state.run(client=resumed, max_questions_per_request=2)
        if state.db.job_state("semantic", 1, 1) != "pending":
            break
    assert resumed.claims == ([[claim_b]] if held else [[claim_a, claim_b]])
    assert state.db.job_state("semantic", 1, 1) == "done"
    assert state.summaries == [False, True]
    assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
    statuses = [json.loads(row["content"])["status"]
                for row in state.db.artifacts("semantic_audit", message_id=1)]
    audit_meta = json.loads(state.db.artifacts("semantic_audit", message_id=1)[-1]["meta"])
    if held:
        assert "PASS" not in statuses and statuses[-1] == "NEEDS_REVIEW"
        assert audit_meta["claim_audit"][claim_a] == {"choice": "contradicts", "confidence": 0.9}
    else:
        assert statuses[-1] == "PASS"
        assert audit_meta["claim_audit"] == {claim: {"choice": "supports", "confidence": 0.9}
                                             for claim in (claim_a, claim_b)}


@pytest.mark.parametrize("corruption", ["evaluated_false", "unknown_code", "no_findings",
                                        "missing_answer", "extra_key", "answer_invalid",
                                        "unknown_claim"])
def test_unverifiable_recorded_verdict_keeps_empty_repair_held(interrupted_empty_repair, corruption):
    """Only a verifiable record of negative answers is resumed and committed.

    A record whose ``evaluated`` flag is not true, that carries a finding
    outside the negative claim codes, has no finding or no answer, answers a
    claim the candidate does not have, holds an invalid answer, or has
    another shape keeps the candidate held: no new audit, no commit, no
    notice."""
    state = interrupted_empty_repair
    state.phase = "paused_after_verdict"
    state.retry()
    state.run()
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    meta = json.loads(candidate["meta"])
    record = meta["partial_answers"]
    if corruption == "evaluated_false":
        record["evaluated"] = False
    elif corruption == "answer_invalid":
        next(iter(record["answers"].values()))["choice"] = "maybe"
    elif corruption == "unknown_claim":
        del record["evaluated"]
        record["answers"]["claim_synthetic_unknown"] = {"choice": "contradicts", "confidence": 0.9}
    elif corruption == "unknown_code":
        record["findings"] = [{**record["findings"][0], "code": "claim_quantity_untraced"}]
    elif corruption == "no_findings":
        record["findings"] = []
    elif corruption == "missing_answer":
        record["answers"] = {}
    else:
        record["status"] = "PASS"
    state.db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                        (json.dumps(meta, ensure_ascii=False), candidate["artifact_id"]))
    state.db.db.commit()
    state.db.artifact_add("semantic_control", "running", project_id=1)
    llm_calls = state.llm_calls
    state.restart()
    state.phase = "recovered"
    for _ in range(2):
        state.retry()
        state.run()
        assert state.llm_calls == llm_calls and state.summaries == [False, True]
        assert state.audits == ["negative", "paused_after_verdict"]
        assert dict(state.db.artifacts("semantic_audit", message_id=1)[-1]) == state.old_audit
        assert state.db.job_state("semantic", 1, 1) == "pending"
        assert not _notice_for_target(state.db)


@pytest.mark.parametrize("corruption", ["unanswered_claim", "support_unevaluated_only"])
def test_unverifiable_partial_record_keeps_empty_repair_held(interrupted_empty_repair, monkeypatch,
                                                             corruption):
    """A partial record is resumed only when each finding is a negative claim code on an answered claim.

    The repaired text has two claims and the first answer was recorded
    before a stop. A finding naming the claim not yet answered, or a record
    whose only finding is ``support_unevaluated`` (not a negative answer),
    keeps the candidate held: the unanswered claim is not asked, nothing is
    committed and no notice is queued."""
    state = interrupted_empty_repair
    _two_claim_repair(monkeypatch)
    state.phase = "two_claims"
    stopping = _claim_jev(state, "off")
    state.retry()
    state.run(client=stopping, max_questions_per_request=1)
    first, second = stopping.claims[0][0], stopping.claims[1][0]
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    meta = json.loads(candidate["meta"])
    record = meta["partial_answers"]
    assert meta["audit_state"] == "held" and set(record["answers"]) == {first}
    if corruption == "unanswered_claim":
        record["findings"] = [{**record["findings"][0], "claim": second}]
    else:
        record["answers"] = {first: {"choice": "supports", "confidence": 0.9}}
        record["findings"] = [{"code": "support_unevaluated", "claim": first}]
    state.db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                        (json.dumps(meta, ensure_ascii=False), candidate["artifact_id"]))
    state.db.db.commit()
    state.db.artifact_add("semantic_control", "running", project_id=1)
    llm_calls = state.llm_calls
    state.restart()

    resumed = _claim_jev(state)
    for _ in range(2):
        state.retry()
        state.run(client=resumed, max_questions_per_request=1)
        assert resumed.claims == []
        assert state.llm_calls == llm_calls and state.summaries == [False, True]
        assert dict(state.db.artifacts("semantic_audit", message_id=1)[-1]) == state.old_audit
        assert state.db.job_state("semantic", 1, 1) == "pending"
        assert not _notice_for_target(state.db)


@pytest.mark.parametrize("forced", ["PASS", "REPAIR_REQUIRED"])
def test_resumed_record_never_ends_in_a_releasable_status(interrupted_empty_repair, monkeypatch, forced):
    """The resumed audit of a recorded negative ends only in NEEDS_REVIEW or PENDING.

    Even if the status rule returned PASS (or REPAIR_REQUIRED) for the
    resumed record, the result is kept PENDING: nothing is committed or
    published from an answer that was never asked again."""
    state = interrupted_empty_repair
    state.phase = "paused_after_verdict"
    state.retry()
    state.run()
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    assert json.loads(candidate["meta"])["partial_answers"]["evaluated"] is True
    state.db.artifact_add("semantic_control", "running", project_id=1)
    llm_calls = state.llm_calls
    state.restart()

    real_status = semantic_job_processor.audit_status_for
    calls = []

    def status_for(code_f, jev_f, evaluated, repaired):
        calls.append(forced)
        real_status(code_f, jev_f, evaluated, repaired)
        return forced

    monkeypatch.setattr(semantic_job_processor, "audit_status_for", status_for)
    state.phase = "recovered"
    for _ in range(2):
        state.retry()
        state.run()
        assert state.llm_calls == llm_calls and state.summaries == [False, True]
        assert state.audits == ["negative", "paused_after_verdict"]
        statuses = [json.loads(row["content"])["status"]
                    for row in state.db.artifacts("semantic_audit", message_id=1)]
        assert "PASS" not in statuses and "REPAIR_REQUIRED" not in statuses
        assert dict(state.db.artifacts("semantic_audit", message_id=1)[-1]) == state.old_audit
        assert dict(state.db.artifacts("semantic_summary", message_id=1)[-1]) == state.old_summary
        assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
        assert state.db.job_state("semantic", 1, 1) == "pending"
        assert not _notice_for_target(state.db)
    assert calls


@pytest.mark.parametrize("phase", sorted(_POSITIVE_STOP_PHASES))
def test_positive_answer_observed_before_a_stop_is_audited_again(interrupted_empty_repair, phase):
    """A stop after only confident ``supports`` answers holds nothing.

    No negative answer or finding was observed, so the candidate is left
    for a new, complete audit on the next run instead of waiting forever;
    the PASS comes only from that new audit, never from a run with Jev
    skipped."""
    state = interrupted_empty_repair
    state.phase = phase
    state.retry()
    state.run()
    assert state.summaries == [False, True] and state.audits == ["negative", phase]
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    meta = json.loads(candidate["meta"])
    assert meta["audit_state"] in ("ready", "inflight") and meta["partial_answers"] == {}
    assert dict(state.db.artifacts("semantic_audit", message_id=1)[-1]) == state.old_audit
    assert not _notice_for_target(state.db)
    llm_calls = state.llm_calls

    state.db.artifact_add("semantic_control", "running", project_id=1)
    state.restart()
    state.phase = "skipped"
    for _ in range(2):
        # While Jev is skipped no audit is evaluated: nothing is passed or sent.
        state.retry()
        state.run(jev=False)
        assert state.llm_calls == llm_calls and state.summaries == [False, True]
        assert state.db.job_state("semantic", 1, 1) == "pending"
        assert json.loads(state.db.artifacts("semantic_audit", message_id=1)[-1]["content"])["status"] != "PASS"
        assert not _notice_for_target(state.db)
    state.phase = "recovered"
    for _ in range(3):
        state.retry()
        state.run()
        if state.db.job_state("semantic", 1, 1) != "pending":
            break
    assert state.audits == ["negative", phase, "skipped", "skipped", "recovered"]
    assert state.llm_calls == llm_calls and state.summaries == [False, True]
    assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
    assert state.db.job_state("semantic", 1, 1) == "done"
    audit = state.db.artifacts("semantic_audit", message_id=1)[-1]
    summary = state.db.artifacts("semantic_summary", message_id=1)[-1]
    assert json.loads(audit["content"])["status"] == "PASS"
    assert json.loads(audit["meta"])["summary_artifact_id"] == summary["artifact_id"]
    assert json.loads(summary["content"])["limitations"] == ["synthetic-interrupted-repair"]


def test_answer_observed_before_a_stale_stop_is_not_recorded(interrupted_empty_repair):
    """After another worker took the job, this worker writes nothing.

    The observed answer cannot be recorded safely (the job is no longer this
    worker's), so the candidate keeps its ``inflight`` row with no answer and
    its next audit is a new, complete one over the current facts."""
    state = interrupted_empty_repair
    state.phase = "stale_after_answer"
    state.retry()
    state.run()
    assert state.audits == ["negative", "stale_after_answer"]
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    meta = json.loads(candidate["meta"])
    assert meta["audit_state"] == "inflight" and meta["partial_answers"] == {}
    assert dict(state.db.artifacts("semantic_audit", message_id=1)[-1]) == state.old_audit
    assert not _notice_for_target(state.db)

    state.restart()
    state.phase = "recovered"
    for _ in range(3):
        state.retry()
        state.run()
        if state.db.job_state("semantic", 1, 1) != "pending":
            break
    assert state.audits == ["negative", "stale_after_answer", "recovered"]
    assert state.summaries == [False, True]
    assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
    audit = state.db.artifacts("semantic_audit", message_id=1)[-1]
    summary = state.db.artifacts("semantic_summary", message_id=1)[-1]
    assert json.loads(audit["content"])["status"] == "PASS"
    assert json.loads(audit["meta"])["summary_artifact_id"] == summary["artifact_id"]


def test_held_prior_pair_releases_empty_repair_reservation(interrupted_empty_repair, monkeypatch):
    """The pair a repair was reserved over is held (rows kept, marked invalid).

    No result of the repair was committed and the candidate would otherwise
    be recovered (its provenance proven, no recorded answer or finding), so
    the reservation reserves nothing: the target takes the ordinary path,
    which audits the bound repaired candidate again (no new summary, no
    second repair) instead of waiting forever on
    ``empty_repair_prior_unverified``."""
    import semantic_publication

    state = interrupted_empty_repair
    state.phase = "unavailable"
    state.retry()
    state.run()
    assert state.summaries == [False, True]
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    meta = json.loads(candidate["meta"])
    assert meta["audit_state"] == "ready" and state.db.job_state("semantic", 1, 1) == "pending"
    llm_calls = state.llm_calls
    with state.db.db:
        semantic_publication._hold_derivatives(state.db, 1, 1, meta["fingerprint"], "synthetic_hold")
    state.restart()

    state.phase = "recovered"
    reasons = []
    real_why = semantic_job_processor._why
    monkeypatch.setattr(semantic_job_processor, "_why",
                        lambda diagnostics, reason: reasons.append(reason) or real_why(diagnostics, reason))
    for _ in range(3):
        state.retry()
        state.run()
        if state.db.job_state("semantic", 1, 1) != "pending":
            break
    assert "empty_repair_prior_unverified" not in reasons
    assert state.db.job_state("semantic", 1, 1) == "done"
    assert state.llm_calls == llm_calls and state.summaries == [False, True]
    assert state.audits == ["negative", "unavailable", "recovered"]
    assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
    audit = state.db.artifacts("semantic_audit", message_id=1)[-1]
    summary = state.db.artifacts("semantic_summary", message_id=1)[-1]
    assert json.loads(audit["meta"])["summary_artifact_id"] == summary["artifact_id"]
    assert json.loads(audit["content"])["status"] == "PASS"
    assert json.loads(summary["content"])["limitations"] == ["synthetic-interrupted-repair"]


@pytest.mark.parametrize("change,released", [
    ("newer_summary", True), ("newer_audit", True),
    ("recorded_audit_newer", False), ("recorded_audit_missing", False),
])
def test_empty_repair_prior_supersession_rules(interrupted_empty_repair, monkeypatch, change, released):
    """Only a newer row (or an in-place invalidated one) supersedes the recorded pair.

    A recorded prior row that is gone, or newer than the current one, proves
    nothing and keeps ``empty_repair_prior_unverified``; a newer summary or
    audit row releases the reservation to the ordinary path."""
    state = interrupted_empty_repair
    state.phase = "unavailable"
    state.retry()
    state.run()
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    assert json.loads(candidate["meta"])["audit_state"] == "ready"
    db = state.db.db
    if change in ("newer_summary", "newer_audit"):
        row = state.old_summary if change == "newer_summary" else state.old_audit
        db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                   "SELECT kind,project_id,message_id,content,model,meta,created_at FROM artifacts "
                   "WHERE artifact_id=?", (row["artifact_id"],))
    elif change == "recorded_audit_newer":
        meta = json.loads(candidate["meta"])
        meta["prior"][1]["id"] += 1000
        db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                   (json.dumps(meta, ensure_ascii=False), candidate["artifact_id"]))
    else:
        db.execute("DELETE FROM artifacts WHERE artifact_id=?", (state.old_audit["artifact_id"],))
    db.commit()
    llm_calls = state.llm_calls
    state.restart()

    state.phase = "recovered"
    reasons = []
    real_why = semantic_job_processor._why
    monkeypatch.setattr(semantic_job_processor, "_why",
                        lambda diagnostics, reason: reasons.append(reason) or real_why(diagnostics, reason))
    for _ in range(2):
        state.retry()
        state.run()
        if state.db.job_state("semantic", 1, 1) != "pending":
            break
    assert state.llm_calls == llm_calls and state.summaries == [False, True]
    assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
    notice = state.db.db.execute(
        "SELECT 1 FROM notify_outbox WHERE kind='semantic_notice' "
        "AND json_extract(payload,'$.target_message_id')=1").fetchone()
    if released:
        assert "empty_repair_prior_unverified" not in reasons
        assert state.audits == ["negative", "unavailable", "recovered"]
        assert state.db.job_state("semantic", 1, 1) == "done"
        audit = state.db.artifacts("semantic_audit", message_id=1)[-1]
        summary = state.db.artifacts("semantic_summary", message_id=1)[-1]
        assert json.loads(audit["content"])["status"] == "PASS"
        assert json.loads(audit["meta"])["summary_artifact_id"] == summary["artifact_id"]
    else:
        assert reasons and set(reasons) == {"empty_repair_prior_unverified"}
        assert state.audits == ["negative", "unavailable"]
        assert state.db.job_state("semantic", 1, 1) == "pending"
        assert notice is None


@pytest.mark.parametrize("phase", ["partial", "finding_only", "paused_after_verdict"])
def test_held_prior_pair_keeps_observed_negative_empty_repair_held(interrupted_empty_repair, phase):
    """A superseded prior pair never frees a candidate with an observed negative answer.

    The release reserves nothing only for a candidate that would otherwise be
    recoverable with nothing recorded; a recorded partial answer, finding or
    evaluated negative verdict keeps it held, with no new audit, no commit
    and no notice."""
    import semantic_publication

    state = interrupted_empty_repair
    state.phase = phase
    state.retry()
    state.run()
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    meta = json.loads(candidate["meta"])
    assert meta["audit_state"] == "held"
    with state.db.db:
        semantic_publication._hold_derivatives(state.db, 1, 1, meta["fingerprint"], "synthetic_hold")
    state.db.artifact_add("semantic_control", "running", project_id=1)
    audits = state.db.artifacts("semantic_audit", message_id=1)[-1]["artifact_id"]
    llm_calls = state.llm_calls
    state.restart()

    state.phase = "recovered"
    for _ in range(2):
        state.retry()
        state.run()
        assert state.llm_calls == llm_calls and state.summaries == [False, True]
        assert state.audits == ["negative", phase]
        assert state.db.artifacts("semantic_audit", message_id=1)[-1]["artifact_id"] == audits
        assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
        assert state.db.job_state("semantic", 1, 1) == "pending"
        assert not state.db.db.execute(
            "SELECT 1 FROM notify_outbox WHERE kind='semantic_notice' "
            "AND json_extract(payload,'$.target_message_id')=1").fetchone()


@pytest.mark.parametrize("hold", [True, False])
def test_fact_rewrite_frees_recorded_negative_to_ordinary_path(interrupted_empty_repair, monkeypatch, hold):
    """A recorded negative held over one set of facts does not outlive a fact rewrite.

    The facts are rewritten after the reservation (with or without the prior
    pair held), so the candidate is no longer bound to the current facts: the
    target takes the ordinary path, which writes a new summary from the
    current facts and audits it (no second repair). The old candidate and its
    record are neither committed nor resumed."""
    import semantic_publication

    state = interrupted_empty_repair
    state.phase = "paused_after_verdict"
    state.retry()
    state.run()
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    meta = json.loads(candidate["meta"])
    assert meta["audit_state"] == "held" and meta["partial_answers"]["evaluated"] is True
    if hold:
        with state.db.db:
            semantic_publication._hold_derivatives(state.db, 1, 1, meta["fingerprint"], "synthetic_hold")
    facts = state.db.artifacts("semantic_facts", message_id=1)[-1]
    content = json.loads(facts["content"])
    content["facts"][0]["statement"] += "（合成の書き換え）"
    state.db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                        "SELECT kind,project_id,message_id,?,model,meta,created_at FROM artifacts "
                        "WHERE artifact_id=?", (json.dumps(content, ensure_ascii=False), facts["artifact_id"]))
    state.db.db.commit()
    state.db.artifact_add("semantic_control", "running", project_id=1)
    state.restart()

    reasons = []
    real_why = semantic_job_processor._why
    monkeypatch.setattr(semantic_job_processor, "_why",
                        lambda diagnostics, reason: reasons.append(reason) or real_why(diagnostics, reason))
    state.phase = "recovered"
    for _ in range(3):
        state.retry()
        state.run()
        if state.db.job_state("semantic", 1, 1) != "pending":
            break
    assert "empty_repair_audit_not_ready" not in reasons
    assert state.db.job_state("semantic", 1, 1) == "done"
    assert state.summaries == [False, True, False]
    assert state.audits == ["negative", "paused_after_verdict", "recovered"]
    assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
    audit = state.db.artifacts("semantic_audit", message_id=1)[-1]
    summary = state.db.artifacts("semantic_summary", message_id=1)[-1]
    audit_meta = json.loads(audit["meta"])
    assert audit_meta["summary_artifact_id"] == summary["artifact_id"] > candidate["artifact_id"]
    assert json.loads(summary["content"]).get("limitations") != ["synthetic-interrupted-repair"]
    # the PASS comes from the fresh audit of the new summary, never from the record
    assert json.loads(audit["content"])["status"] == "PASS"
    assert audit_meta["claim_audit"] != meta["partial_answers"]["answers"]


@pytest.mark.parametrize("corruption", [
    "binding", "candidate_content", "candidate_meta", "prior_summary", "prior_audit",
    "marker_content", "marker_policy", "marker_flag", "marker_replacement",
    "producer_proof", "candidate_schema", "candidate_stale", "marker_count_bool",
])
def test_empty_repair_rejects_corrupt_resume(interrupted_empty_repair, corruption):
    state = interrupted_empty_repair
    state.phase = "unavailable"
    state.retry()
    state.run()
    assert state.summaries == [False, True]
    assert state.audits == ["negative", "unavailable"]
    llm_calls = state.llm_calls
    candidate = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
    assert json.loads(candidate["meta"])["audit_state"] == "ready"
    [marker] = state.db.artifacts("semantic_repair", message_id=1)
    db = state.db.db

    if corruption == "marker_replacement":
        db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                   "SELECT kind,project_id,message_id,content,model,meta,created_at FROM artifacts "
                   "WHERE artifact_id=?", (marker["artifact_id"],))
    else:
        row = (state.old_summary if corruption == "prior_summary" else
               state.old_audit if corruption == "prior_audit" else
               marker if corruption.startswith("marker_") else candidate)
        content, meta = json.loads(row["content"]), json.loads(row["meta"])
        if corruption == "binding":
            meta["claim_input_binding"] = "invalid"
        elif corruption == "candidate_content":
            content["claims"][0]["text"] = "異なる合成結果"
        elif corruption == "candidate_meta":
            meta = None
        elif corruption in ("prior_summary", "prior_audit"):
            content["synthetic_tamper"] = True
        elif corruption == "marker_content":
            content["target_message_id"] = 2
        elif corruption == "marker_policy":
            meta["policy_fingerprint"] = "invalid"
        elif corruption == "marker_flag":
            meta["empty_repair"] = False
        elif corruption == "marker_count_bool":
            meta["repair_count"] = True
        elif corruption == "producer_proof":
            meta["provider_provenance"]["result_hash"] = "invalid"
        elif corruption == "candidate_schema":
            meta["schema"] = "invalid"
        elif corruption == "candidate_stale":
            meta["stale"] = True
        db.execute("UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?",
                   (json.dumps(content, ensure_ascii=False), json.dumps(meta, ensure_ascii=False),
                    row["artifact_id"]))
    db.commit()
    state.restart()
    state.phase = "recovered"
    state.retry()
    state.run()
    assert state.llm_calls == llm_calls
    assert state.summaries == [False, True]
    assert state.audits == ["negative", "unavailable"]
    audit = state.db.artifacts("semantic_audit", message_id=1)[-1]
    assert json.loads(audit["content"])["status"] == "PENDING"
    assert "claim_contradicts" in audit["content"]
    assert not state.db.db.execute(
        "SELECT 1 FROM notify_outbox WHERE kind='semantic_notice' "
        "AND json_extract(payload,'$.target_message_id')=1").fetchone()
