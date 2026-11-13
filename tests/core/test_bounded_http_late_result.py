"""A worker that finished in time returns its answer even if the parent looks late."""
import json

import bounded_http


def test_bounded_http_returns_a_result_the_worker_finished_in_time(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(bounded_http.time, "monotonic", lambda: now[0])

    class Process:
        returncode = 0

        def communicate(self, input, timeout):
            now[0] = 13.0         # the parent observes completion after the deadline
            return json.dumps({"ok": True, "status": 200, "headers": {}, "body": "eA=="}).encode(), b""

    monkeypatch.setattr(bounded_http.subprocess, "Popen", lambda *args, **kwargs: Process())
    url = "https://api.anthropic.com/v1/messages"
    assert bounded_http.bounded_http_request(url, "POST", {}, 30, api_key="synthetic", deadline=12,
                                             allowed_endpoints=(url,)) == (200, {}, b"x")
