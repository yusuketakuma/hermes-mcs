"""Synthetic regression for an empty target in a multi-target bundle."""
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
        result = original(llm, bundle, mid, *args, **kwargs)
        if mid == 1:
            calls.append(bool(kwargs.get('feedback')))
            if not kwargs.get('feedback') and isinstance(result, tuple) and result[0] is not None:
                result[0]['claims'] = []
        return result

    monkeypatch.setattr(semantic_drain, 'summarize', summarize)
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
    monkeypatch.setattr(semantic_drain, "_obtain_summary", obtain)
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


@pytest.fixture
def interrupted_empty_repair(tmp_path, monkeypatch):
    """Create a bound negative pair and an interrupted, nonempty repair."""
    import copy
    import semantic_audit
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
        if state.phase in ("partial", "unavailable", "finding_only") and args[6] == 1 and not injected:
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

    def run():
        client = PartialJev() if state.phase == "partial" else _FakeJev()
        return semantic_drain.run_due(state.db, _cfg("enforce", budget=100, loop_mode="off"),
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
    monkeypatch.setattr(semantic_drain, "_obtain_summary", obtain)
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


def test_partial_negative_empty_repair_stays_held_after_restart(interrupted_empty_repair):
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
    assert len(partial["answers"]) == 1
    answer = next(iter(partial["answers"].values()))
    assert answer["choice"] == "contradicts" and answer["confidence"] == 0.9
    assert any(finding["code"] == "claim_contradicts" for finding in partial["findings"])
    assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
    llm_calls = state.llm_calls

    state.restart()
    state.phase = "recovered"
    for _ in range(2):
        state.retry()
        state.run()
        assert state.llm_calls == llm_calls
        assert state.summaries == [False, True]
        assert state.audits == ["negative", "partial"]
        assert len(state.db.artifacts("semantic_repair", message_id=1)) == 1
        current = state.db.artifacts(semantic_drain.KIND_CANDIDATE, message_id=1)[-1]
        assert current["artifact_id"] == candidate["artifact_id"]
        assert current["meta"] == candidate["meta"]
        audit = state.db.artifacts("semantic_audit", message_id=1)[-1]
        assert (audit["artifact_id"], audit["content"], audit["meta"]) == (
            state.old_audit["artifact_id"], state.old_audit["content"], state.old_audit["meta"])
        job = state.db.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert job["state"] == "pending"
        assert not state.db.db.execute(
            "SELECT 1 FROM notify_outbox WHERE kind='semantic_notice' "
            "AND json_extract(payload,'$.target_message_id')=1").fetchone()


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


@pytest.mark.parametrize("corruption", [
    "binding", "candidate_content", "candidate_meta", "prior_summary", "prior_audit",
    "marker_content", "marker_policy", "marker_flag", "marker_replacement",
    "audit_inflight", "producer_proof", "candidate_schema", "candidate_stale", "marker_count_bool",
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
        elif corruption == "audit_inflight":
            meta["audit_state"] = "inflight"
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
