"""No-network regressions for the local model's absolute HTTP deadline."""
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error

import pytest

import extract_llm
import local_llm
import semantic_jev as jev


@pytest.mark.parametrize("entry", ["chat", "extract", "probe", "slots", "models"])
def test_every_local_request_uses_deadline_and_reaps_worker(monkeypatch, entry):
    processes = []

    class SlowProcess:
        returncode = None

        def __init__(self, command, **kwargs):
            self.command, self.options = command, kwargs
            self.killed, self.reaped = False, False
            processes.append(self)

        def communicate(self, input=None, timeout=None):
            if timeout is not None:
                self.envelope = json.loads(input)
                self.timeout = timeout
                raise subprocess.TimeoutExpired(self.command, timeout)
            self.reaped = True
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = -9

    monkeypatch.setattr(jev.subprocess, "Popen", SlowProcess)
    monkeypatch.setattr(jev.time, "monotonic", lambda: 100.0)
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-secret")
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "schema")
    monkeypatch.setattr(extract_llm, "_LEND_RT", entry == "slots")
    monkeypatch.setattr(extract_llm, "_SLOT_OVERRIDE", None)
    deadline = 100.25
    if entry == "chat":
        assert local_llm.chat("synthetic", deadline=deadline) is None
    elif entry == "extract":
        assert extract_llm._llm_call("synthetic", deadline=deadline) is None
    elif entry == "probe":
        assert local_llm.probe_format(
            local_llm.ENDPOINT, "synthetic", {}, deadline=deadline) == "plain"
    elif entry == "slots":
        assert extract_llm._choose_slot(deadline) == local_llm.request_slot()
    else:
        assert not extract_llm._llm_up(deadline)
    assert len(processes) == 1
    process = processes[0]
    assert process.killed and process.reaped
    assert 0 < process.timeout <= 0.25
    assert process.envelope["api_key"] is None
    assert "TYPESAFE_API_KEY" not in process.options["env"]
    assert process.envelope["method"] == (
        "GET" if entry in ("slots", "models") else "POST")


@pytest.mark.parametrize("wrapped", [False, True])
def test_worker_preserves_connection_refused_as_unreachable(monkeypatch, wrapped):
    reason = ConnectionRefusedError("synthetic refusal")
    error = urllib.error.URLError(reason) if wrapped else reason

    class RefusingOpener:
        def open(self, request, timeout):
            raise error

    envelope = {"endpoint": local_llm.ENDPOINT, "method": "POST",
                "api_key": None, "timeout": 1, "body": {}}
    output = io.StringIO()
    with monkeypatch.context() as worker_patch:
        worker_patch.setattr(jev, "no_proxy_opener", lambda *args: RefusingOpener())
        worker_patch.setattr(jev.sys, "stdin", io.TextIOWrapper(
            io.BytesIO(json.dumps(envelope).encode())))
        worker_patch.setattr(jev.sys, "stdout", output)
        assert jev._http_worker_main() == 0
    wire_result = output.getvalue().encode()
    assert json.loads(wire_result)["error"] == "connection_refused"

    class FinishedProcess:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            return wire_result, b""

    monkeypatch.setattr(jev.subprocess, "Popen", lambda *args, **kw: FinishedProcess())
    err = {}
    assert local_llm.chat("synthetic", error_out=err) is None
    assert err == {"kind": "unreachable"}
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "schema")
    monkeypatch.setattr(extract_llm, "_LEND_RT", False)
    assert extract_llm._llm_call("synthetic") is extract_llm._DEFERRED


def test_real_worker_slow_body_is_killed_and_reaped_without_network(tmp_path, monkeypatch):
    """An actual worker blocked in a synthetic response read cannot outlive its budget."""
    ready = tmp_path / "read-started"
    script = f"""
import sys, time
sys.path.insert(0, {str(Path(jev.__file__).parents[1])!r})
import _mcs_path
import semantic_jev as jev
class SlowResponse:
    status = 200
    headers = {{}}
    def read(self, limit):
        with open({str(ready)!r}, 'w') as marker:
            marker.write('ready')
        data = bytearray()
        for _ in range(1000):
            time.sleep(0.02)
            data.extend(b'x')
        return bytes(data)
    def close(self):
        pass
class SyntheticOpener:
    def open(self, request, timeout):
        return SlowResponse()
jev.no_proxy_opener = lambda *args: SyntheticOpener()
raise SystemExit(jev._http_worker_main())
"""
    popen = jev.subprocess.Popen
    processes = []

    def spawn(command, **kwargs):
        process = popen([sys.executable, "-c", script], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(jev.subprocess, "Popen", spawn)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline"):
        local_llm.bounded_request(local_llm.ENDPOINT, "POST", {}, 30,
                                  deadline=started + 2)
    assert time.monotonic() - started < 5
    assert ready.exists(), "the synthetic slow read must have started"
    assert len(processes) == 1
    assert processes[0].returncode is not None
    assert processes[0].poll() == processes[0].returncode
