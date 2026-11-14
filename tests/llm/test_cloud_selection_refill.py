"""Same-pass replacement (request-wait-scheduling r2): a verified refusal of an already settled
same request that began no work frees its slot for the next unvisited candidate. The real
runner and CloudInferenceUnit run with the routed transport stubs; nothing is resent."""
import pytest

import cloud_inference
import cloud_policy
from anthropic_client import AnthropicError
from test_anthropic_runtime import routed as routed
from cloud_selection_progress_testkit import _cloud_pass as _cloud_pass, _select as _select, \
    cloud_candidates as cloud_candidates

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


def test_a_parallel_pass_keeps_its_original_slots(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    head = _select(led, "extract")[0]["message_id"]
    _refuse(monkeypatch, {head}, "repeat_success")
    report = _cloud_pass(led, limit=2)
    assert report["refilled"] == 0


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


def test_a_distinct_request_of_the_same_message_follows_the_normal_guarded_send(cloud_candidates, monkeypatch):
    """A different logical request is not refused by the guard: it sends once, with no refill."""
    led, _, calls, reservations, results = cloud_candidates
    report = _cloud_pass(led)
    assert report["refilled"] == 0 and report["done"] == 1
    assert len(calls) == len(reservations) == len(results) == 1


def test_a_saved_paid_checkpoint_disqualifies_the_candidate(cloud_candidates, monkeypatch):
    import extract_llm_runner
    led, _, calls, reservations, results = cloud_candidates
    head = _select(led, "extract")[0]["message_id"]
    _refuse(monkeypatch, {head}, "repeat_success")
    capture = extract_llm_runner._capture_checkpoint_state

    def with_saved_work(db, r, context):
        saved = capture(db, r, context)
        if r["message_id"] == head:
            r["_work_seen"] = True     # as if a paid chunk had been saved for this candidate
        return saved
    monkeypatch.setattr(extract_llm_runner, "_capture_checkpoint_state", with_saved_work)
    report = _cloud_pass(led)
    assert report["refilled"] == 0 and not calls


def test_refused_candidates_keep_their_claims_and_return_after_expiry(cloud_candidates, monkeypatch):
    """Retained-claim amplification: every refused row keeps its bounded claim, so neither
    selection sees it until the claim expires; a result arriving meanwhile is seen then."""
    led, _, calls, reservations, results = cloud_candidates
    rows = _select(led, "extract")
    refused = {row["message_id"] for row in rows[:3]}
    _refuse(monkeypatch, refused, "repeat_success")
    report = _cloud_pass(led)
    assert report["refilled"] == 3 and report["done"] == 1
    held = led.db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0]
    assert held >= 3
    assert not refused & {row["message_id"] for row in _select(led, "extract")}
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='extract_claim'")
    assert refused <= {row["message_id"] for row in _select(led, "extract")}


def test_a_reservation_or_recovered_answer_marks_the_whole_unit_as_started(routed, monkeypatch):
    """Reserve-commit-then-exception and a used recovered answer both count before any refusal."""
    import time
    led, cfg, *_ = routed
    unit = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    unit._verified_repeat = True
    assert unit.replacement_eligible is True
    try:
        unit.reserve({"prompt": "synthetic", "max_tokens": 1}, 1)
    except Exception:
        pass     # a commit-then-raise still began work
    assert unit.effect_started is True and unit.replacement_eligible is False
    repaired = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic-repair", deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    repaired._verified_repeat = True
    try:
        repaired.spawn_quality_repair()
    except Exception:
        pass
    assert repaired.replacement_eligible is False      # a started repair is work
