"""A sent Jev request whose outcome is unknown is never resent, and a late paid answer is kept."""
import json

import pytest

import bounded_http
import semantic_jev as jev

QUESTIONS = {"q": jev.noul_question("synthetic", "yes", "no")}


def _answer(body, timeout):
    return 200, {}, json.dumps({"model": jev.JEV_MODEL,
        "answers": {key: {"type": "noul", "noul": .9} for key in body["questions"]},
        "usage": {"input_tokens": 3, "output_tokens": 2}}).encode()


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(jev.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(jev.time, "sleep", lambda seconds: None)
    return now


@pytest.mark.parametrize("error", [TimeoutError("http worker deadline exceeded"),
                                   TimeoutError("http worker timeout"),
                                   OSError("http worker transport failed")])
def test_unknown_outcome_after_dispatch_is_not_resent(clock, error):
    sent = []

    def post(body, timeout):
        sent.append(1)
        raise error

    client = jev.JevClient("synthetic", post_fn=post)
    with pytest.raises(jev.JevError) as raised:
        client.evaluate({}, QUESTIONS, 100.0)
    assert len(sent) == 1 and client.requests_made == 1
    assert raised.value.retryable is False and client.last_error is raised.value


@pytest.mark.parametrize("error", [bounded_http.NotSentTimeout("http worker deadline exceeded"),
                                   ConnectionRefusedError("http worker connection refused")])
def test_provably_unsent_attempt_still_retries(clock, error):
    sent = []

    def post(body, timeout):
        sent.append(1)
        if len(sent) == 1:
            raise error
        return _answer(body, timeout)

    client = jev.JevClient("synthetic", post_fn=post)
    assert set(client.evaluate({}, QUESTIONS, 100.0)["answers"]) == {"q"}
    assert len(sent) == 2


def test_answer_arriving_after_the_deadline_is_settled(clock):
    settled = []

    def post(body, timeout):
        clock[0] = 150.0          # the paid answer lands after the job deadline
        return _answer(body, timeout)

    client = jev.JevClient("synthetic", post_fn=post)
    result = client.evaluate({}, QUESTIONS, 100.0, after_result=lambda: settled.append(1))
    assert set(result["answers"]) == {"q"} and settled == [1]
    assert client.usage_totals["input_tokens"] == 3 and client.usage_totals["reported_requests"] == 1
    assert client.requests_made == 1 and client.last_error is None



def test_each_job_starts_without_an_earlier_jobs_jev_error(tmp_path, monkeypatch):
    import time
    import semantic_audit
    import semantic_drain
    from semantic_testkit import _cfg, _FakeJev, _llm, _seeded

    db = _seeded(tmp_path)
    seen = []

    def claim_audit(client, bundle, summary, deadline, *args):
        seen.append(getattr(client, "last_error", "unset"))
        return [{"code": "support_unevaluated"}], False

    monkeypatch.setattr(semantic_audit, "audit_claims", claim_audit)
    client = _FakeJev()
    client.last_error = jev.JevError("protocol_error", "synthetic")   # left by an earlier job
    try:
        semantic_drain.run_due(db, _cfg("assist", budget=100), {"errors": []},
                               time.monotonic() + 300, jev_client=client, llm_fn=_llm)
    finally:
        db.close()
    assert seen and seen[0] is None
