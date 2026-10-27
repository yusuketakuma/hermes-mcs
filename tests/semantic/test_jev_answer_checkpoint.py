"""A received result must be checkpointed before a malformed answer is retried."""

import json
import time

import pytest

import semantic_jev as jev


QUESTION = jev.choice_question("synthetic", {"yes": "yes", "no": "no"})
YES = {"type": "choice", "choice": "yes", "confidence": 0.9,
       "probabilities": {"yes": 0.9, "no": 0.1}}
NO = {"type": "choice", "choice": "no", "confidence": 0.9,
      "probabilities": {"yes": 0.1, "no": 0.9}}
BAD = {"type": "choice", "choice": "bad", "confidence": 0.9,
       "probabilities": {"yes": 0.1, "no": 0.9}}


class Post:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.asked = []

    def __call__(self, body, timeout):
        self.asked.append(list(body["questions"]))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return 200, {}, json.dumps({"model": jev.JEV_MODEL, "answers": response,
                                    "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()


def run(post, callback, questions=None):
    client = jev.JevClient(api_key="synthetic", max_attempts=1, post_fn=post)
    return jev.evaluate_once_more(client, {}, questions or {"q0": QUESTION, "q1": QUESTION},
                                  time.monotonic() + 30, on_attempt_result=callback)


def test_negative_is_saved_before_only_invalid_question_is_reasked():
    post = Post({"q0": NO, "q1": BAD}, {"q1": YES})
    seen = []
    result = run(post, lambda answers, invalid: seen.append((answers, invalid)))
    assert post.asked == [["q0", "q1"], ["q1"]]
    assert seen[0][0]["q0"]["choice"] == "no" and seen[0][1] == {"q1"}
    assert seen[1][0]["q1"]["choice"] == "yes" and seen[1][1] == set()
    assert result["answers"]["q0"]["choice"] == "no"


@pytest.mark.parametrize("refuse", [False, "exception"])
@pytest.mark.parametrize("answers", [{"q0": NO, "q1": BAD}, {"q0": BAD, "q1": BAD}])
def test_failed_checkpoint_stops_before_next_post(refuse, answers):
    post = Post(answers, {"q0": YES, "q1": YES})
    seen = []

    def checkpoint(valid, invalid):
        seen.append((valid, invalid))
        if refuse == "exception":
            raise OSError("synthetic checkpoint failure")
        return False

    with pytest.raises((RuntimeError, OSError)):
        run(post, checkpoint)
    assert post.asked == [["q0", "q1"]]
    assert seen[0][1] == ({"q1"} if answers["q0"] is NO else {"q0", "q1"})
    assert bool(seen[0][0]) == (answers["q0"] is NO)


def test_second_defect_is_checkpointed_and_raised():
    post = Post({"q0": NO, "q1": BAD}, {"q1": BAD})
    seen = []
    with pytest.raises(jev.JevError):
        run(post, lambda valid, invalid: seen.append((valid, invalid)))
    assert post.asked == [["q0", "q1"], ["q1"]]
    assert seen[0][0]["q0"]["choice"] == "no"
    assert seen[1] == ({}, {"q1"})


def test_callback_mutation_cannot_change_result_or_retry_selection():
    post = Post({"q0": NO, "q1": BAD}, {"q1": YES})

    def mutate(valid, invalid):
        for answer in valid.values():
            answer["choice"] = "changed"
        invalid.clear()

    result = run(post, mutate)
    assert post.asked == [["q0", "q1"], ["q1"]]
    assert result["answers"]["q0"]["choice"] == "no"
    assert result["answers"]["q1"]["choice"] == "yes"


def test_timeout_is_not_reported_as_an_invalid_answer():
    post = Post(TimeoutError())
    seen = []
    with pytest.raises(jev.JevError) as error:
        run(post, lambda valid, invalid: seen.append((valid, invalid)))
    assert error.value.kind == "timeout" and not seen
    assert post.asked == [["q0", "q1"]]


def test_normal_success_notifies_once_and_callbacks_are_mutually_exclusive():
    post = Post({"q0": NO})
    seen = []
    result = run(post, lambda valid, invalid: seen.append((valid, invalid)), {"q0": QUESTION})
    assert seen == [(result["answers"], set())] and post.asked == [["q0"]]
    with pytest.raises(ValueError):
        jev.evaluate_once_more(None, {}, {}, 1, on_valid_answers=lambda _: None,
                               on_attempt_result=lambda _answers, _invalid: None)


def test_unknown_failed_ids_are_the_unvalidated_question_subset():
    error = jev.JevError("protocol_error", "q1:type_invalid")
    error.answers = {"q0": {"choice": "no"}}

    class Client:
        calls = []

        def evaluate(self, state, questions, deadline):
            self.calls.append(list(questions))
            if len(self.calls) == 1:
                raise error
            return {"answers": {"q1": {"choice": "yes"}}}

    client = Client()
    seen = []
    result = jev.evaluate_once_more(client, {}, {"q0": {}, "q1": {}}, 1,
                                   on_attempt_result=lambda valid, invalid: seen.append((valid, invalid)))
    assert client.calls == [["q0", "q1"], ["q1"]]
    assert seen[0] == ({"q0": {"choice": "no"}}, {"q1"})
    assert result["answers"] == {"q0": {"choice": "no"}, "q1": {"choice": "yes"}}


def test_missing_answer_in_a_success_is_a_protocol_error_not_a_partial_checkpoint():
    # The success-path checkpoint requires every requested answer; the client
    # must reject an incomplete 200 before any answer is stored or re-asked.
    post = Post({"q0": YES}, {"q1": NO})
    seen = []
    with pytest.raises(jev.JevError) as error:
        run(post, lambda answers, invalid: seen.append((answers, invalid)))
    assert error.value.kind == "protocol_error"
    assert post.asked == [["q0", "q1"]] and seen == []
