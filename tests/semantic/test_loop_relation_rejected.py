"""A malformed relation answer is an INCOMPLETE receipt, never a relation."""
import semantic_policy
import semantic_store
import json
import time

import pytest

import semantic_drain
import semantic_jev as jev
import semantic_loops
from semantic_testkit import _cfg, _FakeJev, _llm, _message, _pending_fact, _seeded

CODE = "relation:probabilities_sum_off_ge_1e-3"


class _RejectingJev(_FakeJev):
    """Returns a malformed relation answer for trigger m2 only."""

    def __init__(self, error=None, **kw):
        super().__init__(**kw)
        self.reject = error or jev.JevError("protocol_error", CODE)
        self.last_error = None

    def evaluate(self, state, questions, deadline):
        self.last_error = None
        if state["target"]["id"] == "m2":
            self.requests_made += 1
            self.last_error = self.reject
            raise self.reject
        return super().evaluate(state, questions, deadline)


def _two_pairs(tmp_path):
    """One open item (message 1) and two later replies -> two pairs."""
    db = _seeded(tmp_path)
    db.save_messages([_message(3, parent=1, body="別件の返信です。")])
    config, _ = semantic_policy.semantic_config(_cfg())
    db.artifact_add("semantic_policy", semantic_policy.policy_fingerprint(config))
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [2, 3])
    facts = {1: _pending_fact(bundle["members"][0])}
    return db, config, bundle, facts


def _run(db, config, bundle, facts, client, scope):
    rejections = []
    _, complete = semantic_loops.update_loops(
        db, 1, bundle, facts, client, config, time.monotonic() + 30,
        retry_scope=scope, rejections=rejections)
    return complete, rejections


def _rows(db, kind):
    return [(json.loads(c), json.loads(m)) for c, m in db.db.execute(
        "SELECT content, meta FROM artifacts WHERE kind=?", (kind,))]


def test_a_rejected_pair_is_recorded_and_later_pairs_still_run(tmp_path):
    """2026-10-10: one pair's relation answer was rejected (sum gap >= 1e-3)
    on two passes in a row; every pair behind it stayed unevaluated and the
    job used up its attempts. The rejection is now an INCOMPLETE receipt
    and the remaining pairs are evaluated in the same pass."""
    db, config, bundle, facts = _two_pairs(tmp_path)
    try:
        complete, rejections = _run(db, config, bundle, facts,
                                    _RejectingJev(), "cmd-a")
        assert complete is False
        assert rejections == [{"code": CODE, "cached": False}]
        events = _rows(db, semantic_loops.KIND_LOOP_EVENT)
        assert [e["trigger_message_id"] for e, _ in events] == [3]
        [(receipt, meta)] = _rows(db, semantic_loops.KIND_LOOP_REJECTED)
        # fixed code + pair identity only: no choice, probabilities or text
        assert set(receipt) == {"status", "loop_artifact_id", "trigger_message_id",
                                "trigger_revision", "code"}
        assert receipt["status"] == "INCOMPLETE"
        assert receipt["trigger_message_id"] == 2 and receipt["code"] == CODE
        assert meta["retry_scope"] == "cmd-a" and meta["jev_requests"] == 2
    finally:
        db.close()


def test_the_same_retry_scope_does_not_resend_the_rejected_pair(tmp_path):
    db, config, bundle, facts = _two_pairs(tmp_path)
    try:
        _run(db, config, bundle, facts, _RejectingJev(), "cmd-a")
        client = _RejectingJev()
        complete, rejections = _run(db, config, bundle, facts, client, "cmd-a")
        assert complete is False
        assert rejections == [{"code": CODE, "cached": True}]
        assert client.requests_made == 0
        assert len(_rows(db, semantic_loops.KIND_LOOP_REJECTED)) == 1
    finally:
        db.close()


def test_a_new_retry_scope_evaluates_the_rejected_pair_again(tmp_path):
    db, config, bundle, facts = _two_pairs(tmp_path)
    try:
        _run(db, config, bundle, facts, _RejectingJev(), "cmd-a")
        client = _FakeJev()
        complete, rejections = _run(db, config, bundle, facts, client, "cmd-b")
        assert complete is True and rejections == []
        assert client.requests_made == 1
        events = _rows(db, semantic_loops.KIND_LOOP_EVENT)
        assert sorted(e["trigger_message_id"] for e, _ in events) == [2, 3]
    finally:
        db.close()


def test_an_unlisted_answer_code_is_stored_as_unknown(tmp_path):
    """Only the fixed per-answer codes are kept; anything else becomes
    "unknown" so no response text can reach the receipt."""
    db, config, bundle, facts = _two_pairs(tmp_path)
    try:
        error = jev.JevError("protocol_error", "relation:SYNTHETIC_PRIVATE_RESPONSE")
        complete, rejections = _run(db, config, bundle, facts,
                                    _RejectingJev(error), "cmd-a")
        assert complete is False
        assert rejections == [{"code": "unknown", "cached": False}]
        [(receipt, _)] = _rows(db, semantic_loops.KIND_LOOP_REJECTED)
        assert receipt["code"] == "unknown"
        assert "SYNTHETIC_PRIVATE_RESPONSE" not in repr(_rows(db, semantic_loops.KIND_LOOP_REJECTED))
    finally:
        db.close()


@pytest.mark.parametrize("error", [
    jev.JevError("protocol_error", "answers_missing"),
    jev.JevError("auth_error", "unauthorized"),
    jev.JevError("transport", "reset", retryable=True)])
def test_other_jev_failures_still_stop_the_pass(tmp_path, error):
    """Envelope, auth and transport errors keep the old contract: the pass
    stops at the failing pair and nothing is recorded as rejected."""
    db, config, bundle, facts = _two_pairs(tmp_path)
    try:
        complete, rejections = _run(db, config, bundle, facts,
                                    _RejectingJev(error), "cmd-a")
        assert complete is False and rejections == []
        assert _rows(db, semantic_loops.KIND_LOOP_REJECTED) == []
        assert _rows(db, semantic_loops.KIND_LOOP_EVENT) == []
    finally:
        db.close()


@pytest.mark.parametrize("content,meta", [
    ("json_set(content,'$.code','relation:SYNTHETIC_PRIVATE_RESPONSE')", "meta"),
    ("json_remove(content,'$.status')", "meta"),
    ("json_set(content,'$.trigger_revision','')", "meta"),
    ("content", "json_set(meta,'$.registry','old-registry')"),
    ("content", "json_set(meta,'$.invalidated',json('true'))"),
])
def test_a_damaged_receipt_is_kept_but_not_trusted(tmp_path, content, meta):
    """A receipt that fails any check is history only: the pair is sent
    again, the row is not deleted, and no free text reaches the result."""
    db, config, bundle, facts = _two_pairs(tmp_path)
    try:
        _run(db, config, bundle, facts, _RejectingJev(), "cmd-a")
        with db.db:
            db.db.execute(f"UPDATE artifacts SET content={content}, meta={meta} "
                          "WHERE kind=?", (semantic_loops.KIND_LOOP_REJECTED,))
        client = _FakeJev()
        complete, rejections = _run(db, config, bundle, facts, client, "cmd-a")
        assert complete is True and rejections == []
        assert client.requests_made == 1
        assert len(_rows(db, semantic_loops.KIND_LOOP_REJECTED)) == 1
    finally:
        db.close()


@pytest.mark.parametrize("ids", [(True, 2), (1, True), (0, 2), (1, -2), ("1", 2)])
def test_pair_ids_must_be_positive_ints_not_bools(ids):
    """True == 1 in Python: a bool id must never match a real pair."""
    receipt = {"status": "INCOMPLETE", "loop_artifact_id": ids[0],
               "trigger_message_id": ids[1], "trigger_revision": "rev", "code": CODE}
    meta = {"policy_fingerprint": "p", "retry_scope": "s",
            "registry": jev.REGISTRY_VERSION}
    assert semantic_loops.rejected_relation_pair(receipt, meta, "p", "s") is None
    receipt.update(loop_artifact_id=1, trigger_message_id=2)
    assert semantic_loops.rejected_relation_pair(receipt, meta, "p", "s") == ((1, 2, "rev"), CODE)



@pytest.mark.parametrize("after,status,charged,reason", [
    # cache-only pass, or a later pair succeeded: last_error says nothing
    (None, "retry", True, "loops_jev_rejected"),
    # a later real failure keeps its own class
    (jev.JevError("auth_error", "unauthorized"), "failed", None, "loops_jev_hard"),
    (jev.JevError("protocol_error", "answers_missing"), "failed", None, "loops_jev_hard"),
    (jev.JevError("budget_exceeded", "daily"), "deferred", False, "loops_jev_resource"),
    (jev.JevError("transport", "reset", retryable=True), "retry", True,
     "loops_jev_retryable"),
])
def test_a_known_rejection_retries_only_when_nothing_else_failed(
        tmp_path, monkeypatch, after, status, charged, reason):
    """A later pair's success clears the client's last_error; the job must
    then still end "retry" (a bounded attempt) — a free "deferred" would
    re-run the skipped pair's job forever. A later hard or resource failure
    is handled as before; the fixed code is kept either way."""
    seen = {}

    def loops(ledger, pid, bundle, facts, client, scfg, deadline, *,
              retry_scope="", rejections=None):
        seen["scope"] = retry_scope
        if client is not None:
            client.last_error = after
            rejections.append({"code": CODE, "cached": True})
            return 0, False
        return 0, True

    monkeypatch.setattr(semantic_drain, "update_loops", loops)
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute(
                "UPDATE fetch_jobs SET payload=json_set(payload,"
                "'$.retry_command_id','cmd-a') WHERE kind='semantic'")
        attempts0 = db.db.execute(
            "SELECT attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()[0]
        client = _FakeJev()
        client.last_error = None
        out = semantic_drain.run_due(db, _cfg(), {"errors": []},
                               time.monotonic() + 300, jev_client=client,
                               llm_fn=_llm)
        [metric] = [m for m in out["job_metrics"] if m["kind"] == "semantic"]
        assert metric["status"] == status
        assert metric["retry_reason"] == reason
        assert metric["loops_rejected"] == CODE
        assert seen["scope"] == "cmd-a"
        attempts = db.db.execute(
            "SELECT attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()[0]
        if charged is not None:
            assert attempts == attempts0 + (1 if charged else 0)
    finally:
        db.close()


class _FlakyJev(_FakeJev):
    """Trigger m2 gets the given outcomes in order (an error to raise, or
    None for a valid answer); every other trigger is answered."""

    def __init__(self, *outcomes, on_call=None, target="m2"):
        super().__init__()
        self.outcomes = list(outcomes)
        self.m2 = 0
        self.on_call = on_call
        self.target = target

    def evaluate(self, state, questions, deadline):
        if state["target"]["id"] == self.target:
            self.m2 += 1
            if self.on_call:
                self.on_call()
            outcome = self.outcomes.pop(0)
            if outcome is not None:
                self.requests_made += 1
                raise outcome
        return super().evaluate(state, questions, deadline)


REJECT = jev.JevError("protocol_error", CODE)


def test_a_transient_rejection_is_asked_again_and_recovers(tmp_path):
    """Per-answer defects are sometimes transient (2026-10-10: medication
    detail rejections cleared on the job's retry). The pair is asked once
    more in the same pass; a valid answer is a normal relation event."""
    db, config, bundle, facts = _two_pairs(tmp_path)
    try:
        client = _FlakyJev(REJECT, None)
        complete, rejections = _run(db, config, bundle, facts, client, "cmd-a")
        assert complete is True and rejections == [] and client.m2 == 2
        events = _rows(db, semantic_loops.KIND_LOOP_EVENT)
        assert sorted(e["trigger_message_id"] for e, _ in events) == [2, 3]
        assert [m["jev_requests"] for e, m in events
                if e["trigger_message_id"] == 2] == [2]
        assert _rows(db, semantic_loops.KIND_LOOP_REJECTED) == []
    finally:
        db.close()


def test_a_repeated_rejection_is_recorded_after_one_reask(tmp_path):
    db, config, bundle, facts = _two_pairs(tmp_path)
    try:
        client = _FlakyJev(REJECT, REJECT)
        complete, rejections = _run(db, config, bundle, facts, client, "cmd-a")
        assert complete is False and client.m2 == 2
        assert rejections == [{"code": CODE, "cached": False}]
        assert len(_rows(db, semantic_loops.KIND_LOOP_REJECTED)) == 1
        events = _rows(db, semantic_loops.KIND_LOOP_EVENT)
        assert [e["trigger_message_id"] for e, _ in events] == [3]
    finally:
        db.close()


def test_a_different_failure_on_the_reask_stops_the_pass(tmp_path):
    db, config, bundle, facts = _two_pairs(tmp_path)
    try:
        client = _FlakyJev(REJECT, jev.JevError("auth_error", "unauthorized"))
        complete, rejections = _run(db, config, bundle, facts, client, "cmd-a")
        assert complete is False and rejections == [] and client.m2 == 2
        assert _rows(db, semantic_loops.KIND_LOOP_REJECTED) == []
        assert _rows(db, semantic_loops.KIND_LOOP_EVENT) == []
    finally:
        db.close()


def test_no_room_to_ask_again_leaves_the_pair_for_a_later_pass(tmp_path, monkeypatch):
    """The first malformed answer arrives at the deadline: no re-ask and no
    receipt — the pair stays unevaluated and a later pass asks it afresh."""
    clock = [1000.0]
    monkeypatch.setattr(semantic_loops.time, "monotonic", lambda: clock[0])

    def at_deadline():
        clock[0] = 1030.0

    db, config, bundle, facts = _two_pairs(tmp_path)
    try:
        client = _FlakyJev(REJECT, on_call=at_deadline)
        rejections = []
        _, complete = semantic_loops.update_loops(
            db, 1, bundle, facts, client, config, 1030.0,
            retry_scope="cmd-a", rejections=rejections)
        assert complete is False and rejections == [] and client.m2 == 1
        assert _rows(db, semantic_loops.KIND_LOOP_REJECTED) == []
    finally:
        db.close()


def test_a_first_rejection_on_the_last_call_of_the_cap_is_not_recorded(tmp_path, monkeypatch):
    """Review condition: the pass may send _PAIR_CAP calls. When the first
    malformed answer uses the last one, there is no room for the re-ask:
    no receipt, the pass is incomplete, and the next pass asks again."""
    monkeypatch.setattr(semantic_loops, "_PAIR_CAP", 2)
    db, config, bundle, facts = _two_pairs(tmp_path)
    try:
        client = _FlakyJev(REJECT, target="m3")     # m2 answers, then m3
        complete, rejections = _run(db, config, bundle, facts, client, "cmd-a")
        assert complete is False and rejections == [] and client.m2 == 1
        assert _rows(db, semantic_loops.KIND_LOOP_REJECTED) == []
        assert [e["trigger_message_id"] for e, _ in _rows(
            db, semantic_loops.KIND_LOOP_EVENT)] == [2]
        complete, rejections = _run(db, config, bundle, facts, _FakeJev(), "cmd-a")
        assert complete is True and rejections == []
        assert sorted(e["trigger_message_id"] for e, _ in _rows(
            db, semantic_loops.KIND_LOOP_EVENT)) == [2, 3]
    finally:
        db.close()
