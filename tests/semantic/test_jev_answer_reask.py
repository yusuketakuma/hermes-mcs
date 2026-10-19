"""A malformed Jev answer is asked once more before the stage fails.

2026-10-10: medication-detail rejections (speaker_basis, polarity,
change_kind) each retried the whole job — a pass and an attempt — and the
retried jobs then completed, i.e. the defects were transient."""
import semantic_drain
import json
import time

import pytest

import semantic_jev as jev
import semantic_runtime as runtime
from semantic_testkit import _cfg, _FakeJev, _llm, _seeded

DEFECT = jev.JevError("protocol_error", "speaker_basis:probabilities_sum_off_ge_1e-3")


class _Sequence:
    """Raises the queued outcomes (errors) in order, then answers."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def evaluate(self, state, questions, deadline):
        self.calls += 1
        if self.outcomes:
            error = self.outcomes.pop(0)
            if error is not None:
                raise error
        return {"answers": {q: {"ok": True} for q in questions}}


def _ask(client):
    return jev.evaluate_once_more(client, {}, {"q": {}}, time.monotonic() + 60)


def test_a_malformed_answer_is_asked_once_more():
    client = _Sequence(DEFECT)
    assert _ask(client) == {"answers": {"q": {"ok": True}}}
    assert client.calls == 2


def test_a_second_malformed_answer_is_raised():
    second = jev.JevError("protocol_error", "status:probabilities_invalid")
    client = _Sequence(DEFECT, second)
    with pytest.raises(jev.JevError) as error:
        _ask(client)
    assert error.value is second and client.calls == 2


@pytest.mark.parametrize("error", [
    jev.JevError("auth_error", "http_401"),
    jev.JevError("protocol_error", "answers_missing"),   # envelope: no colon
    jev.JevError("transport", "reset", retryable=True),
    jev.JevError("budget_exceeded", "daily_cap", retryable=True)])
def test_other_failures_are_not_asked_again(error):
    client = _Sequence(error)
    with pytest.raises(jev.JevError) as raised:
        _ask(client)
    assert raised.value is error and client.calls == 1


def _body(answer):
    return json.dumps({"model": jev.JEV_MODEL, "answers": {"q": answer},
                       "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()


GOOD = {"type": "noul", "noul": 0.5}
BAD = {"type": "noul", "noul": 1.5}              # q:noul_invalid


def test_the_real_client_reserves_and_counts_both_sends(monkeypatch):
    bodies = [_body(BAD), _body(GOOD)]
    reserved = []
    client = jev.JevClient(api_key="synthetic", max_attempts=1,
                           post_fn=lambda body, timeout: (200, {}, bodies.pop(0)))
    guarded = runtime.bind_jev(client, lambda stage: None,
                               lambda body, timeout: reserved.append(1))
    out = jev.evaluate_once_more(guarded, {}, {"q": jev.noul_question("i", "t", "f")},
                                 time.monotonic() + 60)
    assert out["answers"]["q"]["noul"] == 0.5
    assert client.requests_made == 2 and len(reserved) == 2
    assert client.last_error is None


def test_a_runtime_guard_on_the_second_send_propagates(monkeypatch):
    """The re-ask goes through the same guards: past the transport cutoff
    it is a free defer of the pass, never swallowed as a Jev failure."""
    clock = [1000.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(jev.time, "monotonic", lambda: clock[0])

    def post(body, timeout):
        clock[0] += 100.0                     # the first answer lands late
        return 200, {}, _body(BAD)

    client = jev.JevClient(api_key="synthetic", max_attempts=1, attempt_timeout=30,
                           post_fn=post)
    guarded = runtime.bind_jev(client, lambda stage: None, None, cutoff=1050.0)
    with pytest.raises(runtime.RuntimeBudgetShort):
        jev.evaluate_once_more(guarded, {}, {"q": jev.noul_question("i", "t", "f")},
                               1900.0)
    assert client.requests_made == 1


class _OnceMalformed(_FakeJev):
    """speaker_basis answers malformed once, like 2026-10-10's rejections."""

    def __init__(self):
        super().__init__()
        self.defects = 0
        self.last_error = None

    def evaluate(self, state, questions, deadline):
        self.last_error = None
        if "speaker_basis" in questions and not self.defects:
            self.defects += 1
            self.requests_made += 1
            self.last_error = DEFECT
            raise DEFECT
        return super().evaluate(state, questions, deadline)


def test_a_transient_medication_detail_defect_no_longer_retries_the_job(tmp_path):
    db = _seeded(tmp_path)
    client = _OnceMalformed()
    try:
        out = semantic_drain.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 300,
                               jev_client=client, llm_fn=_llm, max_jobs=1)
        [metric] = [m for m in out["job_metrics"] if m["kind"] == "semantic"]
        assert client.defects == 1
        assert metric["status"] == "done", metric.get("retry_reason")
        attempts = db.db.execute(
            "SELECT attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()[0]
        assert attempts == 0
    finally:
        db.close()
