"""Per-evaluation Jev guards and explicit deadlines stay isolated on one reused client."""
import json
import time

import pytest

import semantic_jev as jev
import semantic_runtime as runtime


def answer(body):
    return 200, {}, json.dumps({"model": jev.JEV_MODEL,
        "answers": {key: {"type": "noul", "noul": .9} for key in body["questions"]},
        "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()


def test_nested_same_client_keeps_hooks_deadlines_and_reservations_per_evaluation(monkeypatch):
    client = jev.JevClient(api_key="synthetic", max_attempts=1)
    events, deadlines = [], {}
    end = time.monotonic() + 30
    question = {"q": jev.noul_question("synthetic", "yes", "no")}
    inner = runtime.bind_jev(client, lambda stage: events.append("inner:" + stage),
        lambda body, timeout: events.append("inner:reserve"))
    def request(method, body, timeout, *, deadline):
        name = body["state"]["target"]["id"]
        deadlines[name] = deadline
        events.append(name + ":wire")
        if name == "outer":
            inner.evaluate({"target": {"id": "inner"}}, question, end - 1)
        return answer(body)
    monkeypatch.setattr(client, "request", request)
    outer = runtime.bind_jev(client, lambda stage: events.append("outer:" + stage),
        lambda body, timeout: events.append("outer:reserve"))
    outer.evaluate({"target": {"id": "outer"}}, question, end)
    assert events == ["outer:jev_attempt", "outer:reserve", "outer:wire",
                      "inner:jev_attempt", "inner:reserve", "inner:wire",
                      "inner:jev_result", "outer:jev_result"]
    assert deadlines == {"outer": end, "inner": end - 1}
    assert client.requests_made == 2
    assert client.usage_totals == {"input_tokens": 2, "output_tokens": 2, "reported_requests": 2}
    assert not any(name in vars(client) for name in ("before_attempt", "after_result", "reserve_fn", "_active_deadline"))


def test_pre_dispatch_guard_failure_leaves_next_evaluation_unbound():
    sent = []
    client = jev.JevClient(api_key="synthetic", max_attempts=1,
        post_fn=lambda body, timeout: sent.append(body) or answer(body))
    def refuse(stage):
        raise runtime.RuntimeStale(stage)
    held = runtime.bind_jev(client, refuse, lambda body, timeout: pytest.fail("reservation after stale guard"))
    end = time.monotonic() + 30
    question = {"q": jev.noul_question("synthetic", "yes", "no")}
    with pytest.raises(runtime.RuntimeStale):
        held.evaluate({}, question, end)
    assert client.requests_made == 0 and not sent
    allowed = runtime.bind_jev(client, lambda stage: None)
    allowed.evaluate({}, question, end)
    assert client.requests_made == 1 and len(sent) == 1


def test_fake_evaluation_timing_excludes_guard_work(monkeypatch):
    clock = [0.0]
    class Client:
        requests_made = 0
        def evaluate(self, state, questions, deadline):
            clock[0] += 2
            return {"answers": {}}
    def guard(stage):
        clock[0] += 3
    monkeypatch.setattr(runtime.time, "perf_counter", lambda: clock[0])
    timing = {"s": 0.0}
    evaluation = runtime.bind_jev(Client(), guard, timing=timing)
    assert evaluation.evaluate({}, {}, 100) == {"answers": {}}
    assert timing == {"s": 2.0}
    assert clock[0] == 8.0
