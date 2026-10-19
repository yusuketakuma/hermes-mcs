"""Synthetic Jev wire-contract tests (FIX-J01).

These tests exercise only the adapter boundary.  They never contact the
TypeSafe service and keep the normalized internal distribution separate from
the API's ``probabilities`` field.
"""
import json
import time

import pytest

import semantic_jev as jev


def _noul():
    return {"q": jev.noul_question("inspect state.target", "yes", "no")}


def _choice():
    return {"q": jev.choice_question("classify state.target", {
        "yes": "supported", "no": "unsupported"})}


def _choice_answer(**overrides):
    answer = {
        "type": "choice",
        "choice": "yes",
        "confidence": 0.9,
        "probabilities": {"yes": 0.9, "no": 0.1},
    }
    answer.update(overrides)
    return answer


def test_choice_question_uses_criteria_wire_field():
    q = jev.choice_question("i", {"yes": "Y", "no": "N"})
    assert q["type"] == "choice"
    assert q["criteria"] == {"yes": "Y", "no": "N"}
    assert "options" not in q


def test_choice_response_uses_probabilities_and_normalizes_distribution():
    out = jev.validate_answers(
        {"model": jev.JEV_MODEL,
         "answers": {"q": _choice_answer()},
         "usage": {"input_tokens": 3, "output_tokens": 2,
                    "total_tokens": 5}},
        _choice(), jev.JEV_MODEL)
    assert out["answers"]["q"]["distribution"] == {"yes": 0.9, "no": 0.1}
    assert out["usage"] == {"input_tokens": 3, "output_tokens": 2,
                              "total_tokens": 5}


def test_answer_type_is_required_and_matches_question():
    for questions, answer in (
        (_noul(), {"noul": 0.9}),
        (_noul(), {"type": "choice", "noul": 0.9}),
        (_choice(), _choice_answer(type="noul")),
    ):
        with pytest.raises(jev.JevError, match="protocol_error"):
            jev.validate_answers(
                {"model": jev.JEV_MODEL, "answers": {"q": answer},
                 "usage": {"input_tokens": 0, "output_tokens": 0}},
                questions, jev.JEV_MODEL)


def test_choice_probabilities_require_bounds_sum_and_maximum_choice():
    for probabilities in (
        {"yes": 1.1, "no": -0.1},
        {"yes": float("nan"), "no": 0.0},
        {"yes": 10**1000, "no": 0.0},
        {"yes": 0.6, "no": 0.6},
        {"yes": 0.2, "no": 0.8},
        {"yes": 0.9, "other": 0.1},
    ):
        with pytest.raises(jev.JevError, match="protocol_error"):
            jev.validate_answers(
                {"model": jev.JEV_MODEL,
                 "answers": {"q": _choice_answer(
                     probabilities=probabilities)},
                 "usage": {"input_tokens": 0, "output_tokens": 0}},
                _choice(), jev.JEV_MODEL)


def test_usage_is_required_but_extensions_are_ignored():
    valid = jev.validate_answers(
        {"model": jev.JEV_MODEL,
         "answers": {"q": {"type": "noul", "noul": 0.5}},
         "usage": {"input_tokens": 1, "output_tokens": 2,
                    "future_counter": "ignored"}},
        _noul(), jev.JEV_MODEL)
    assert valid["usage"] == {"input_tokens": 1, "output_tokens": 2}
    for usage in (None, {}, [], {"input_tokens": 1},
                  {"output_tokens": 1}, {"input_tokens": -1,
                                         "output_tokens": 1},
                  {"input_tokens": True, "output_tokens": 1},
                  {"input_tokens": 1.5, "output_tokens": 1},
                  {"input_tokens": 1, "output_tokens": "1"},
                  {"input_tokens": 1, "output_tokens": 1,
                   "total_tokens": -1}):
        with pytest.raises(jev.JevError, match="protocol_error"):
            jev.validate_answers(
                {"model": jev.JEV_MODEL,
                 "answers": {"q": {"type": "noul", "noul": 0.5}},
                 "usage": usage}, _noul(), jev.JEV_MODEL)


def test_endpoint_is_exactly_allowlisted():
    assert jev.JEV_ENDPOINT in jev.JEV_ALLOWED_ENDPOINTS
    with pytest.raises(ValueError, match="model_not_allowed"):
        jev.JevClient(api_key="synthetic", model="jev-latest")
    for endpoint in (
        "https://api.typesafe.ai/v1/systemone?redirect=https://evil.test",
        "https://evil.test/v1/systemone",
        "http://api.typesafe.ai/v1/systemone",
    ):
        with pytest.raises(ValueError, match="endpoint_not_allowed"):
            jev.JevClient(api_key="synthetic", endpoint=endpoint)


def test_post_body_uses_official_choice_shape_without_network():
    sent = []

    def post(body, timeout):
        sent.append(body)
        return 200, {}, json.dumps({
            "model": jev.JEV_MODEL, "answers": {"q": _choice_answer()},
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }).encode()

    jev.JevClient(api_key="synthetic", post_fn=post).evaluate(
        {"target": {"text": "synthetic"}, "context": []},
        _choice(), time.monotonic() + 5)
    assert "criteria" in sent[0]["questions"]["q"]
    assert "options" not in sent[0]["questions"]["q"]


def test_usage_counts_returned_response_before_stale_hook_but_not_unknown_failure():
    def post(body, timeout):
        return 200, {}, json.dumps({
            "model": jev.JEV_MODEL, "answers": {"q": _choice_answer()},
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }).encode()

    client = jev.JevClient(api_key="synthetic", post_fn=post, max_attempts=1)
    client.evaluate({}, _choice(), time.monotonic() + 5)
    def stale():
        raise RuntimeError("stale")
    with pytest.raises(RuntimeError, match="stale"):
        client.evaluate({}, _choice(), time.monotonic() + 5, after_result=stale)
    assert client.usage_totals == {
        "input_tokens": 6, "output_tokens": 4, "reported_requests": 2}
    client._post_fn = lambda body, timeout: (503, {}, b"")
    with pytest.raises(jev.JevError):
        client.evaluate({}, _choice(), time.monotonic() + 5)
    assert client.requests_made == 3
    assert client.usage_totals["reported_requests"] == 2


@pytest.mark.parametrize("post_kind", ["none", "falsy_callable", "function"])
def test_constructor_preserves_truthiness_selection_with_stubbed_default(monkeypatch, post_kind):
    sent = []
    false_checks = []
    response = json.dumps({
        "model": jev.JEV_MODEL, "answers": {"q": _choice_answer()},
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }).encode()

    def request(self, method, body, timeout, endpoint=None, *, deadline=None):
        sent.append(("default", method, body, timeout, deadline))
        return 200, {}, response

    def post(body, timeout):
        sent.append(("custom", "POST", body, timeout, None))
        return 200, {}, response

    class FalsyPost:
        def __bool__(self):
            false_checks.append("selected")
            return False

        def __call__(self, body, timeout):
            pytest.fail("a falsy callable must retain the original default selection")

    monkeypatch.setattr(jev.JevClient, "request", request)
    supplied = None if post_kind == "none" else FalsyPost() if post_kind == "falsy_callable" else post
    client = jev.JevClient(api_key="synthetic", post_fn=supplied, max_attempts=1)
    assert false_checks == (["selected"] if post_kind == "falsy_callable" else [])
    deadline = time.monotonic() + 5
    result = client.evaluate({}, _choice(), deadline)
    assert len(sent) == 1
    assert sent[0][0] == ("custom" if post_kind == "function" else "default")
    assert sent[0][1] == "POST" and sent[0][2]["questions"] == _choice()
    assert sent[0][3] > 0
    assert sent[0][4] == (None if post_kind == "function" else deadline)
    assert result["answers"]["q"]["choice"] == "yes"
    assert client.requests_made == 1
    assert client.usage_totals == {"input_tokens": 3, "output_tokens": 2, "reported_requests": 1}
    assert false_checks == (["selected"] if post_kind == "falsy_callable" else [])


@pytest.mark.parametrize("value,expected", [(None, None), (True, None), (False, None),
    ("0.5", None), (10**1000, None), (float("nan"), None), (float("inf"), None),
    (0, 0.0), (1, 1.0), (0.5, 0.5), (-1, -1.0)])
def test_shared_wire_finite_value_parser_retains_original_type_contract(value, expected):
    assert jev.finite_value(value) == expected


def test_shared_wire_numeric_subclasses_retain_float_result():
    class NumericInt(int):
        pass
    result = jev.finite_value(NumericInt(1))
    assert result == 1.0 and type(result) is float
