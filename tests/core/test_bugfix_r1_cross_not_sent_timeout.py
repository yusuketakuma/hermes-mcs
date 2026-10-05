"""A deadline that expires before the body is sent is reported as not sent."""
import bounded_http
import local_llm
import pytest

URL = "http://127.0.0.1:9/v1/chat/completions"


def test_pre_popen_expiry_is_not_sent(monkeypatch):
    clock = iter([100.0, 100.0, 200.0])
    monkeypatch.setattr(bounded_http.time, "monotonic", lambda: next(clock))
    with pytest.raises(bounded_http.NotSentTimeout):
        bounded_http.bounded_http_request(URL, "GET", None, 5, deadline=150.0)


def test_post_popen_pre_communicate_expiry_is_not_sent(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(bounded_http.time, "monotonic", lambda: now[0])

    class FakeProcess:
        returncode = None
        sent = None

        def __init__(self, *a, **k):
            now[0] = 200.0  # deadline passes while the worker starts

        def poll(self):
            return None

        def kill(self):
            self.returncode = -9

        def communicate(self, input=None, timeout=None):
            FakeProcess.sent = input
            return b"", b""

    monkeypatch.setattr(bounded_http.subprocess, "Popen", FakeProcess)
    with pytest.raises(bounded_http.NotSentTimeout):
        bounded_http.bounded_http_request(URL, "GET", None, 5, deadline=150.0)
    assert FakeProcess.sent is None


@pytest.mark.parametrize("error,kind", [
    (bounded_http.NotSentTimeout("x"), "unreachable"),
    (TimeoutError("x"), "transport"),
])
def test_chat_classifies_not_sent_timeout(error, kind):
    def request_fn(*_a):
        raise error
    out = {}
    assert local_llm.chat("p", endpoint=URL, request_fn=request_fn,
                          error_out=out) is None
    assert out["kind"] == kind
