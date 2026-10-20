"""Shared worker collection preserves bounded waits and transport ownership."""
from io import BytesIO
import subprocess
from types import SimpleNamespace

import pytest

import bounded_http
import mcs_worker
import worker_process


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired("synthetic", 5), OSError(), ValueError(),
])
def test_failed_drain_closes_all_pipes_before_bounded_wait(failure):
    events = []
    pipes = [BytesIO() for _ in range(3)]

    def communicate(*, timeout):
        events.append(("communicate", timeout))
        raise failure

    def wait(*, timeout):
        assert all(pipe.closed for pipe in pipes)
        events.append(("wait", timeout))
        raise subprocess.TimeoutExpired("synthetic", timeout)

    process = SimpleNamespace(stdin=pipes[0], stdout=pipes[1], stderr=pipes[2],
                              communicate=communicate, wait=wait)
    worker_process.reap_worker(process)
    assert events == [("communicate", 5.0), ("wait", 0.1)]


@pytest.mark.parametrize("transport", ["http", "mcs"])
def test_transport_kills_then_collects_with_shared_reaper(monkeypatch, transport):
    events = []

    def communicate(**kwargs):
        raise subprocess.TimeoutExpired("synthetic", kwargs["timeout"])

    process = SimpleNamespace(communicate=communicate,
                              kill=lambda: events.append("kill"))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    def reap(child):
        assert child is process
        events.append("reap")

    monkeypatch.setattr(worker_process, "reap_worker", reap)
    monkeypatch.setattr(mcs_worker, "_worker_command", lambda: ["synthetic-worker"])
    with pytest.raises(TimeoutError):
        if transport == "http":
            bounded_http.bounded_http_request("http://127.0.0.1:8080/", "GET", None, 10)
        else:
            mcs_worker.bounded_call({"operation": "api"}, timeout=10)
    assert events == ["kill", "reap"]
