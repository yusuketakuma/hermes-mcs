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
