"""A malformed answer no longer discards the valid answers beside it.

validate_answers stopped at the first defect, so a re-ask sent every
question of the request again. The first defect is still the error (same
code, same order); the valid answers ride on it and only the failed
questions are asked again."""
import json
import time

import pytest

import semantic_drain
import semantic_jev as jev
import semantic_runtime as runtime

NOUL = jev.noul_question("synthetic", "t", "f")
CHOICE = jev.choice_question("synthetic", {"a": "first", "b": "second"})
GOOD = {"type": "noul", "noul": 0.5}
BAD = {"type": "noul", "noul": 1.5}                       # {qid}:noul_invalid


def _raw(answers):
    return {"model": jev.JEV_MODEL, "answers": answers,
            "usage": {"input_tokens": 1, "output_tokens": 1}}


def test_the_first_defect_is_raised_with_the_valid_answers_beside_it():
    questions = {"q1": NOUL, "q2": NOUL, "q3": CHOICE}
    raw = _raw({"q1": GOOD, "q2": BAD,
                "q3": {"type": "choice", "choice": "z", "confidence": 0.9,
                       "probabilities": {"a": 0.5, "b": 0.5}}})
    with pytest.raises(jev.JevError) as error:
        jev.validate_answers(raw, questions, jev.JEV_MODEL)
    assert error.value.detail == "q2:noul_invalid"         # unchanged report
    assert error.value.answers == {"q1": {"type": "noul", "noul": 0.5}}
    assert error.value.failed == ["q2", "q3"]


def test_an_envelope_defect_carries_no_partial_answers():
    with pytest.raises(jev.JevError) as error:
        jev.validate_answers({"model": jev.JEV_MODEL, "answers": {},
                              "usage": {"input_tokens": 1, "output_tokens": 1}},
                             {"q1": NOUL}, jev.JEV_MODEL)
    assert error.value.detail == "answers_missing"
    assert getattr(error.value, "answers", None) is None


class _Post:
    """Answers each POST from a queue of {qid: answer} maps, recording the
    questions every request carried."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.asked = []

    def __call__(self, body, timeout):
        self.asked.append(sorted(body["questions"]))
        answers = self.responses.pop(0)
        return 200, {}, json.dumps(_raw(
            {q: answers[q] for q in body["questions"]})).encode()


def _client(post):
    client = jev.JevClient(api_key="synthetic", max_attempts=1, post_fn=post)
    guarded = runtime.bind_jev(client, lambda stage: None, None)
    return client, guarded


def test_only_the_failed_question_is_asked_again():
    post = _Post({"q1": GOOD, "q2": BAD, "q3": GOOD}, {"q2": GOOD})
    client, guarded = _client(post)
    out = jev.evaluate_once_more(guarded, {}, {"q1": NOUL, "q2": NOUL, "q3": NOUL},
                                 time.monotonic() + 60)
    assert post.asked == [["q1", "q2", "q3"], ["q2"]]
    assert sorted(out["answers"]) == ["q1", "q2", "q3"]
    assert client.requests_made == 2 and client.last_error is None


def test_a_second_defect_on_the_failed_question_is_raised():
    post = _Post({"q1": GOOD, "q2": BAD}, {"q2": BAD})
    _, guarded = _client(post)
    with pytest.raises(jev.JevError) as error:
        jev.evaluate_once_more(guarded, {}, {"q1": NOUL, "q2": NOUL},
                               time.monotonic() + 60)
    assert error.value.detail == "q2:noul_invalid"
    assert post.asked == [["q1", "q2"], ["q2"]]


def test_with_no_valid_answer_the_whole_request_is_asked_again():
    post = _Post({"q1": BAD, "q2": BAD}, {"q1": GOOD, "q2": GOOD})
    _, guarded = _client(post)
    out = jev.evaluate_once_more(guarded, {}, {"q1": NOUL, "q2": NOUL},
                                 time.monotonic() + 60)
    assert post.asked == [["q1", "q2"], ["q1", "q2"]]
    assert sorted(out["answers"]) == ["q1", "q2"]


def test_a_chunked_assessment_keeps_its_valid_answers():
    """The assessment sends its propositions in chunks of up to
    max_questions_per_request; one malformed answer re-asks only itself."""
    questions = {f"p{i}": NOUL for i in range(5)}
    first = {q: GOOD for q in questions} | {"p3": BAD}
    post = _Post(first, {"p3": GOOD})
    client, guarded = _client(post)
    out = semantic_drain._eval_chunked(guarded, {}, questions,
                                       time.monotonic() + 60, 12)
    assert sorted(out["answers"]) == sorted(questions)
    assert post.asked == [sorted(questions), ["p3"]]
    assert client.requests_made == 2
