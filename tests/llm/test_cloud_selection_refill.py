"""Same-pass replacement (request-wait-scheduling r2): a verified refusal of an already settled
same request that began no work frees its slot for the next unvisited candidate. The real
runner and CloudInferenceUnit run with the routed transport stubs; nothing is resent."""
import pytest

import cloud_inference
import extract_llm
import cloud_policy
from anthropic_client import AnthropicError
from test_anthropic_runtime import routed as routed
from cloud_selection_progress_testkit import _cloud_pass as _cloud_pass, _select as _select, \
    _complete_cloud_tx as _complete_cloud_tx, cloud_candidates as cloud_candidates
from anthropic_runtime_testkit import _split_checkpoint_cloud_jobs as _split_checkpoint_cloud_jobs, \
    _use_real_cloud_accounting as _use_real_cloud_accounting
from test_cloud_response_recovery import legacy_cloud_routes

__all__ = ["routed", "cloud_candidates"]


def _refuse(monkeypatch, refused, refusal):
    """The shared guard refuses the given messages with a fixed classification."""
    real = cloud_policy.request_send_block_reason
    seen = []

    def guard(ledger, cfg, body, **kwargs):
        if kwargs.get("message_id") in refused:
            seen.append(kwargs["message_id"])
            raise cloud_policy._classified(AnthropicError("budget_exceeded", "cloud_unit_unresolved"), refusal)
        return real(ledger, cfg, body, **kwargs)
    monkeypatch.setattr(cloud_policy, "request_send_block_reason", guard)
    return seen


def test_verified_repeat_refusal_at_the_head_reaches_the_next_candidate(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    head = _select(led, "extract")[0]["message_id"]
    seen = _refuse(monkeypatch, {head}, "repeat_success")
    report = _cloud_pass(led)
    assert seen == [head]                                   # refused once, never retried
    assert report["refilled"] == 1 and report["done"] == 1
    assert len(calls) == len(reservations) == len(results) == 1   # only the healthy candidate sent
    assert report["defer_reasons"] == {"cloud_unit_unresolved": 1}


@pytest.mark.parametrize("refusal", ["receipt_inconsistent", "disposition", "result_missing"])
def test_other_refusals_keep_consuming_the_slot(cloud_candidates, monkeypatch, refusal):
    led, _, calls, reservations, results = cloud_candidates
    head = _select(led, "extract")[0]["message_id"]
    _refuse(monkeypatch, {head}, refusal)
    report = _cloud_pass(led)
    assert report["refilled"] == 0 and report["done"] == 0
    assert not calls and not reservations and not results


def test_all_candidates_refused_is_finite_and_sends_nothing(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    everyone = {row["message_id"] for row in _select(led, "extract")}
    seen = _refuse(monkeypatch, everyone | set(range(1, 200)), "repeat_success")
    report = _cloud_pass(led)
    assert report["done"] == 0 and not calls and not reservations and not results
    assert len(seen) == len(set(seen))                      # each candidate visited at most once
    assert report["refilled"] == len(seen) - 1 and report["refilled"] <= 64
    assert report["deferred"] >= len(seen)


def _in_flight(monkeypatch):
    """The highest number of cloud requests running at once in the pass."""
    import threading
    import extract_llm as worker
    stub = worker.anthropic_client.AnthropicClient
    state = {"now": 0, "most": 0}
    guard = threading.Lock()

    class Counted(stub):
        def complete(self, prompt, **kwargs):
            with guard:
                state["now"] += 1
                state["most"] = max(state["most"], state["now"])
            try:
                return super().complete(prompt, **kwargs)
            finally:
                with guard:
                    state["now"] -= 1
    monkeypatch.setattr(worker.anthropic_client, "AnthropicClient", Counted)
    return state


def test_a_parallel_pass_gives_a_freed_slot_to_the_next_candidate(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    head = _select(led, "extract")[0]["message_id"]
    _refuse(monkeypatch, {head}, "repeat_success")
    state = _in_flight(monkeypatch)
    report = _cloud_pass(led, limit=2)
    assert report["refilled"] == 1 and report["done"] == 2
    assert len(calls) == len(reservations) == len(results) == 2 and state["most"] <= 2


def test_a_parallel_pass_with_every_claim_refused_stays_finite(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    rows = _select(led, "extract")
    seen = _refuse(monkeypatch, {row["message_id"] for row in rows}, "repeat_success")
    report = _cloud_pass(led, limit=2)
    assert report["done"] == 0 and not calls and len(seen) == len(set(seen))
    assert report["refilled"] == len(seen) - 2 and report["refilled"] <= 64


def test_a_parallel_pass_with_both_first_claims_refused_refills_two(cloud_candidates, monkeypatch):
    """Several claimed at once: each freed slot is refilled once, never above the limit."""
    led, _, calls, reservations, results = cloud_candidates
    rows = _select(led, "extract")
    _refuse(monkeypatch, {rows[0]["message_id"], rows[1]["message_id"]}, "repeat_success")
    state = _in_flight(monkeypatch)
    report = _cloud_pass(led, limit=2)
    assert report["refilled"] == 2 and report["done"] == 2 and state["most"] <= 2


def test_a_parallel_worker_crash_cleans_up_and_never_refills(cloud_candidates, monkeypatch):
    """A worker that raises never reaches the handler: its rows stay pending, their claims are
    released by the pass's finally and nothing is refilled."""
    import extract_llm_runner
    led, _, calls, reservations, results = cloud_candidates

    def crash(body, **kwargs):
        raise RuntimeError("synthetic worker crash")
    monkeypatch.setattr(extract_llm_runner, "llm_extract", crash)
    report = _cloud_pass(led, limit=2)
    assert report["refilled"] == 0 and report["done"] == 0 and not calls
    assert led.db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0] == 0


def _unit(*, verified, started, children=()):
    unit = cloud_inference.CloudInferenceUnit.__new__(cloud_inference.CloudInferenceUnit)
    unit._verified_repeat, unit._effect_started, unit._children = verified, started, list(children)
    return unit


@pytest.mark.parametrize("unit,eligible", [
    (_unit(verified=True, started=False), True),
    (_unit(verified=True, started=True), False),            # an earlier chunk reserved or was recovered
    (_unit(verified=False, started=False), False),          # any other refusal
    (_unit(verified=True, started=False, children=[_unit(verified=False, started=True)]), False),  # a repair sent
])
def test_replacement_needs_a_verified_refusal_and_no_work_in_the_whole_unit(unit, eligible):
    assert unit.replacement_eligible is eligible


@pytest.mark.parametrize("refusal,verified", [
    ("repeat_success", True), ("repeat_failed", True), ("repeat_unsettled", True),
    ("result_missing", False), ("disposition", False), ("receipt_inconsistent", False),
])
def test_only_the_guard_repeat_branch_marks_a_verified_repeat(refusal, verified):
    error = cloud_policy._classified(AnthropicError("budget_exceeded", "cloud_unit_unresolved"), refusal)
    assert (error.kind, error.detail) == ("budget_exceeded", "cloud_unit_unresolved")
    assert error.verified_repeat is verified
    assert getattr(AnthropicError("budget_exceeded", "cloud_unit_unresolved"), "verified_repeat", False) is False


def test_a_source_change_seen_by_the_handler_does_not_free_the_slot(cloud_candidates, monkeypatch):
    """The refusal alone is not terminal: the handler's currentness check decides."""
    import extract_llm_runner
    led, _, calls, reservations, results = cloud_candidates
    head = _select(led, "extract")[0]["message_id"]
    seen = _refuse(monkeypatch, {head}, "repeat_success")
    owns = extract_llm_runner._owns_current_source
    monkeypatch.setattr(extract_llm_runner, "_owns_current_source",
                        lambda ledger, r, lease: False if seen and r["message_id"] == head else owns(ledger, r, lease))
    report = _cloud_pass(led)
    assert report["refilled"] == 0 and not calls and not reservations


@pytest.mark.parametrize("failure", ["sqlite", "value", "not_sent"])
def test_read_or_observation_failures_and_plain_not_sent_never_refill(cloud_candidates, monkeypatch, failure):
    import sqlite3
    import cloud_client
    led, _, calls, reservations, results = cloud_candidates
    head = _select(led, "extract")[0]["message_id"]
    real = cloud_policy.request_send_block_reason

    def guard(ledger, cfg, body, **kwargs):
        if kwargs.get("message_id") == head:
            if failure == "sqlite":
                raise sqlite3.OperationalError("synthetic busy")
            if failure == "value":
                raise ValueError("synthetic unreadable receipt")
            raise cloud_client.CloudNotSent("budget_exceeded", "deadline")
        return real(ledger, cfg, body, **kwargs)
    monkeypatch.setattr(cloud_policy, "request_send_block_reason", guard)
    report = _cloud_pass(led)
    assert report["refilled"] == 0 and not calls and not reservations and not results


def _refuse_on_call(monkeypatch, message_id, refused_calls, refusal="repeat_success"):
    """The real guard runs, except that the given calls for one message are refused."""
    real = cloud_policy.request_send_block_reason
    count = {"n": 0}

    def guard(ledger, cfg, body, **kwargs):
        if kwargs.get("message_id") == message_id:
            count["n"] += 1
            if count["n"] in refused_calls:
                raise cloud_policy._classified(AnthropicError("budget_exceeded", "cloud_unit_unresolved"), refusal)
        return real(ledger, cfg, body, **kwargs)
    monkeypatch.setattr(cloud_policy, "request_send_block_reason", guard)
    return count


def _split_pass(led):
    with legacy_cloud_routes():
        return extract_llm.run_pending(led, limit=1, workers=1, budget_s=180, cloud_only=True)


@pytest.mark.parametrize("real_accounting", [False, True], ids=["stub-accounting", "durable-reservation"])
def test_an_earlier_sent_chunk_followed_by_a_refusal_never_frees_the_slot(routed, monkeypatch, real_accounting):
    """Two chunks through the real runner and client: the first chunk posts (with a durable
    reservation row when accounting is real), a later chunk is refused as a verified repeat."""
    led, _, _, reservations, results = routed
    bodies, posts, rejected = _split_checkpoint_cloud_jobs(routed, monkeypatch, 1)
    if real_accounting:
        _use_real_cloud_accounting(monkeypatch)
    count = _refuse_on_call(monkeypatch, 1, set(range(2, 50)))
    report = _split_pass(led)
    assert count["n"] >= 2                                  # the refusal came after a send
    assert {post["message_id"] for post in posts} == {1}    # the next candidate never started
    assert report["refilled"] == 0
    if real_accounting:
        assert led.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='cloud_usage_reserve'").fetchone()[0] >= 1


def test_a_really_saved_chunk_disqualifies_a_later_verified_refusal(routed, monkeypatch):
    """A first pass saves a paid chunk checkpoint; in the next pass the same message's first
    guard call is refused. Its saved work keeps the slot from being given away."""
    import extract_llm_runner
    led, _, _, reservations, results = routed
    bodies, posts, rejected = _split_checkpoint_cloud_jobs(routed, monkeypatch, 1)
    persist, saved = extract_llm._persist_chunks, []

    def counted(ledger, row, chunks, context=None, **kwargs):
        accepted = persist(ledger, row, chunks, context, **kwargs)
        if accepted and row["message_id"] == 1 and kwargs.get("split_size") is None:
            saved.extend(value for value in chunks.values() if value)   # a paid chunk result, not a split marker
        return accepted
    monkeypatch.setattr(extract_llm, "_persist_chunks", counted)
    monkeypatch.setattr(extract_llm_runner, "_persist_chunks", extract_llm._persist_chunks)
    # call 1: the whole body stops at length and splits; call 2: the first child succeeds and is saved
    _refuse_on_call(monkeypatch, 1, set(range(3, 50)))
    _split_pass(led)
    assert saved and all(item.get("patient_context") for item in saved)
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='extract_claim'")
    posts.clear()
    _refuse_on_call(monkeypatch, 1, set(range(1, 50)))
    report = _split_pass(led)
    assert report["refilled"] == 0 and {post["message_id"] for post in posts} <= {1}


def test_a_recovered_paid_answer_followed_by_a_refusal_never_frees_the_slot(routed, monkeypatch):
    """The exact recovery returns a saved paid answer for the first chunk (a length stop, so the
    message splits); the next chunk is refused as a verified repeat."""
    led, _, _, reservations, results = routed
    bodies, posts, rejected = _split_checkpoint_cloud_jobs(routed, monkeypatch, 1)
    recovered = []

    def recover(ledger, **kwargs):
        if kwargs.get("message_id") == 1 and not recovered:
            recovered.append(True)
            return ({"reservation": {"reservation_id": "e" * 32}},
                    {"status": 200, "text": "", "finish_reason": "length"})
        return None
    monkeypatch.setattr(cloud_policy, "recover_response", recover)
    _refuse_on_call(monkeypatch, 1, set(range(1, 50)))
    report = _split_pass(led)
    assert recovered and report["refilled"] == 0
    assert 2 not in {post["message_id"] for post in posts}


def test_a_rate_limited_head_keeps_its_retry_rules_and_never_refills(routed, monkeypatch):
    led, _, _, reservations, results = routed
    bodies, posts, rejected = _split_checkpoint_cloud_jobs(routed, monkeypatch, 1)
    import anthropic_client

    def limited(self, method, request, timeout, **kwargs):
        posts.append({"message_id": 1, "piece": "", "after_rejection": False})
        return 429, {}, b'{"type":"error","error":{"type":"rate_limit_error"}}'
    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", limited)
    report = _split_pass(led)
    assert report["refilled"] == 0 and {post["message_id"] for post in posts} == {1}


def test_refused_rows_keep_their_claims_for_both_selections_and_a_late_result_is_seen(cloud_candidates, monkeypatch):
    """Retained-claim amplification: each refused row keeps its bounded claim, so neither the
    cloud nor the local selection takes it until expiry; a result written meanwhile by another
    worker is what the next selection sees."""
    led, _, calls, reservations, results = cloud_candidates
    rows = _select(led, "extract")
    refused = {row["message_id"] for row in rows[:3]}
    _refuse(monkeypatch, refused, "repeat_success")
    report = _cloud_pass(led)
    assert report["refilled"] == 3 and report["done"] == 1
    local = {row["message_id"] for row in extract_llm._select_pending(led, 64, None, None, False)[0]}
    assert not refused & {row["message_id"] for row in _select(led, "extract")} and not refused & local
    late = next(row for row in rows if row["message_id"] == min(refused))
    with led.db:
        led.db.execute("BEGIN IMMEDIATE")
        _complete_cloud_tx(led, late)
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='extract_claim'")
    again = {row["message_id"] for row in _select(led, "extract")}
    assert (refused - {late["message_id"]}) <= again and late["message_id"] not in again


def test_a_healthy_row_beyond_the_lookahead_is_reached_in_a_later_pass(cloud_candidates, monkeypatch):
    """Every candidate inside the 64-row lookahead is refused: the pass ends finitely, and the
    held claims let the next pass reach a healthy row that was outside it."""
    from test_anthropic_runtime import old
    led, _, calls, reservations, results = cloud_candidates
    import time
    old(led, 65)
    with led.db:   # the same history window and capture as the fixture's 64 rows
        led.db.execute("UPDATE messages SET first_seen=? WHERE message_id=65", (time.time() - 1200,))
        led.db.execute("UPDATE message_revisions SET observed_at=? WHERE message_id=65", (time.time() - 1200,))
        cloud_policy.capture_history_tx(led.db, "synthetic-claim-refill-65", time.time())
    first = {row["message_id"] for row in _select(led, "extract")}
    assert len(first) == 64 and 65 not in first
    _refuse(monkeypatch, first, "repeat_success")
    report = _cloud_pass(led)
    assert report["done"] == 0 and not calls and report["refilled"] <= 64
    assert _cloud_pass(led)["done"] == 1 and len(calls) == 1      # the held claims let row 65 through


def test_a_durable_reservation_then_an_exception_never_frees_the_slot(routed, monkeypatch):
    """Through the real client body and accounting: the real reserver commits its row, then the
    attempt raises before any POST."""
    from anthropic_runtime_testkit import real_usage_reserver
    led, _, _, _, _ = routed
    bodies, posts, rejected = _split_checkpoint_cloud_jobs(routed, monkeypatch, 1)
    _use_real_cloud_accounting(monkeypatch)

    def commit_then_raise(ledger, config, **identity):
        reserve = real_usage_reserver(ledger, config, **identity)

        def wrapped(body, timeout):
            reserve(body, timeout)
            raise RuntimeError("synthetic failure after the durable reservation")
        return wrapped
    monkeypatch.setattr(cloud_policy, "usage_reserver", commit_then_raise)
    report = _split_pass(led)
    reserved = led.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='cloud_usage_reserve'").fetchone()[0]
    settled = led.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='cloud_usage_result'").fetchone()[0]
    assert reserved == 1 and settled == 0 and not posts and report["refilled"] == 0


def test_a_not_sent_timeout_through_the_runner_never_refills(routed, monkeypatch):
    """The real client meets a proven not-sent transport timeout: it is settled once as
    not_sent under the existing rules, never retried in this pass and never refilled."""
    import anthropic_client
    led, _, _, reservations, results = routed
    bodies, posts, rejected = _split_checkpoint_cloud_jobs(routed, monkeypatch, 1)
    attempts = []

    def unsent(self, method, request, timeout, **kwargs):
        attempts.append(True)
        raise anthropic_client.bounded_http.NotSentTimeout("synthetic")
    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", unsent)
    report = _split_pass(led)
    assert len(attempts) == len(reservations) == len(results) == 1
    assert report["refilled"] == 0 and report["done"] == 0


def test_a_recovered_answer_then_a_refused_quality_repair_never_frees_the_slot(cloud_candidates, monkeypatch):
    """Recovery hands back a paid answer, the engine asks for a quality repair, and that repair's
    request reaches the guard and is refused as a verified repeat: the used recovery keeps the
    slot. With the same quality profile the engine reuses the root unit, so this pins that the
    repair request is guarded, not that a separate repair child was created."""
    import extract_llm as worker
    led, _, calls, reservations, results = cloud_candidates
    head = _select(led, "extract")[0]["message_id"]
    recovered = []

    def recover(ledger, **kwargs):
        if kwargs.get("message_id") == head and not recovered:
            recovered.append(True)
            return ({"reservation": {"reservation_id": "e" * 32}},
                    {"status": 200, "text": '{"symptoms":[]}', "finish_reason": "stop"})
        return None
    monkeypatch.setattr(cloud_policy, "recover_response", recover)
    monkeypatch.setattr(worker.extract_quality, "completeness_issues", lambda *_args: ["synthetic-missing"])
    monkeypatch.setattr(worker.extract_quality, "is_thin", lambda *_args: False)
    seen = _refuse(monkeypatch, {head}, "repeat_success")
    report = _cloud_pass(led)
    assert recovered and seen == [head]         # the repair's own request reached the guard
    assert report["refilled"] == 0 and not calls


def test_a_reservation_or_a_started_repair_marks_the_whole_unit(routed, monkeypatch):
    """Inside the real unit: entering reserve (even when it then raises) and starting a repair
    both count as work before any refusal is seen."""
    import time
    led, cfg, *_ = routed

    def failing_reserver(ledger, config, **identity):
        def reserve(body, timeout):
            raise RuntimeError("synthetic commit-then-raise")
        return reserve
    monkeypatch.setattr(cloud_policy, "usage_reserver", failing_reserver)
    unit = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    with pytest.raises(Exception):
        unit.reserve({"prompt": "synthetic", "max_tokens": 1}, 1)
    unit._verified_repeat = True
    assert unit.effect_started is True and unit.replacement_eligible is False
    repaired = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic-repair", deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    repaired.spawn_quality_repair()
    repaired._verified_repeat = True
    assert repaired.replacement_eligible is False


@pytest.mark.parametrize("limit", [1, 2], ids=["serial", "parallel"])
def test_a_failed_refill_preparation_keeps_the_other_paid_work(cloud_candidates, monkeypatch, limit):
    """Candidate A is a verified repeat, the preparation of its replacement raises, and in the
    parallel pass candidate B already holds a paid answer: B is still handled and saved,
    refilling stops for the pass, and every claim is cleaned up by the existing finally."""
    import extract_llm_runner
    led, _, calls, reservations, results = cloud_candidates
    head = _select(led, "extract")[0]["message_id"]
    seen = _refuse(monkeypatch, {head}, "repeat_success")
    prepare = extract_llm_runner._prepare_jobs
    raised = []

    def failing_after_refusal(*args, **kwargs):
        if seen:
            raised.append(True)
            raise RuntimeError("synthetic preparation failure")
        return prepare(*args, **kwargs)
    monkeypatch.setattr(extract_llm_runner, "_prepare_jobs", failing_after_refusal)
    report = _cloud_pass(led, limit=limit)
    assert seen == [head] and raised and report["refilled"] == 0
    # the stop is reported with a fixed class, never silently
    assert report["refill_stopped"] is True and report["refill_stop_error"] == "runtime_error"
    assert report["done"] == limit - 1 and len(calls) == len(results) == limit - 1
    held = led.db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0]
    assert held <= 1            # only the refused unit's bounded claim may remain


def test_a_lost_run_lock_during_refill_still_propagates(cloud_candidates, monkeypatch):
    import extract_llm_runner
    import writer_lock
    led, _, calls, reservations, results = cloud_candidates
    head = _select(led, "extract")[0]["message_id"]
    seen = _refuse(monkeypatch, {head}, "repeat_success")
    prepare = extract_llm_runner._prepare_jobs

    def lost_after_refusal(*args, **kwargs):
        if seen:
            raise writer_lock.RunLockLost(held=False)
        return prepare(*args, **kwargs)
    monkeypatch.setattr(extract_llm_runner, "_prepare_jobs", lost_after_refusal)
    with pytest.raises(writer_lock.RunLockLost):
        _cloud_pass(led)
