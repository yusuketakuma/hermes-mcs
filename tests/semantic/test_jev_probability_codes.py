"""A rejected Jev choice distribution names which rule it broke — a fixed
code, never the response, an option name or a value."""
import semantic_jev
import math

import pytest

import semantic_jev as jev

OPTIONS = {"a": "A", "b": "B", "c": "C"}


def _validate(answer):
    q = {"q1": jev.choice_question("i", OPTIONS)}
    return jev.validate_answers(
        {"usage": {"input_tokens": 1, "output_tokens": 1}, "model": jev.JEV_MODEL,
         "answers": {"q1": {"type": "choice", "confidence": 0.7, **answer}}},
        q, jev.JEV_MODEL)


@pytest.mark.parametrize("answer,code", [
    ({"choice": "a", "probabilities": {"a": 0.5, "b": 0.3, "z": 0.2}},
     "probabilities_option_unknown"),
    ({"choice": "a", "probabilities": {"a": 0.5, "b": "0.3", "c": 0.2}},
     "probabilities_value_invalid"),
    ({"choice": "a", "probabilities": {"a": 1.5, "b": 0.3, "c": 0.2}},
     "probabilities_value_invalid"),
    ({"choice": "a", "probabilities": {"a": math.nan, "b": 0.3, "c": 0.2}},
     "probabilities_value_invalid"),
    ({"choice": "a", "probabilities": {"a": 0.7, "b": 0.3}},
     "probabilities_option_missing"),
    ({"choice": "a", "probabilities": {"a": 0.5, "b": 0.3, "c": 0.3}},
     "probabilities_sum_off_ge_1e-3"),
    ({"choice": "b", "probabilities": {"a": 0.5, "b": 0.3, "c": 0.2}},
     "probabilities_choice_not_max"),
])
def test_each_rejected_distribution_names_its_rule(answer, code):
    with pytest.raises(jev.JevError) as error:
        _validate(answer)
    assert error.value.kind == "protocol_error"
    assert error.value.detail == f"q1:{code}"
    # still one malformed answer: retried within the bounded attempts
    assert semantic_jev.failure_class(error.value) == "retry"


def test_a_valid_distribution_is_still_accepted():
    out = _validate({"choice": "a", "probabilities": {"a": 0.5, "b": 0.3, "c": 0.2}})
    assert out["answers"]["q1"]["distribution"] == {"a": 0.5, "b": 0.3, "c": 0.2}


def test_a_tie_with_the_choice_is_still_accepted():
    out = _validate({"choice": "b", "probabilities": {"a": 0.4, "b": 0.4, "c": 0.2}})
    assert out["answers"]["q1"]["choice"] == "b"


# exact binary fractions so the boundary is not blurred by float rounding
@pytest.mark.parametrize("off,code", [
    (2 ** -10, "probabilities_sum_off_lt_1e-3"),      # 0.00098 above 1
    (-(2 ** -10), "probabilities_sum_off_lt_1e-3"),   # 0.00098 below 1
    (2 ** -9, "probabilities_sum_off_ge_1e-3"),       # 0.00195 above 1
    (-(2 ** -9), "probabilities_sum_off_ge_1e-3"),
])
def test_a_sum_rejection_says_how_far_off_it_was(off, code):
    """2026-10-10: a 7-option distribution was rejected for its sum; whether
    Jev rounds slightly or returns an unnormalised distribution decides any
    later change, so the code carries the size of the gap — a fixed bin
    (|sum - 1| below or at least 1e-3), never the value."""
    with pytest.raises(jev.JevError) as error:
        _validate({"choice": "a", "probabilities": {"a": 0.5 + off, "b": 0.25, "c": 0.25}})
    assert error.value.detail == f"q1:{code}"


def test_a_gap_within_the_tolerance_is_still_accepted():
    out = _validate({"choice": "a", "probabilities": {"a": 0.5 + 2 ** -21, "b": 0.25, "c": 0.25}})
    assert out["answers"]["q1"]["choice"] == "a"
