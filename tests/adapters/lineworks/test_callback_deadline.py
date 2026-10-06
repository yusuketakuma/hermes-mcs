"""A trickled callback cannot monopolize the single HTTP server indefinitely."""
import io
from types import SimpleNamespace

import pytest

from adapters.lineworks import server


@pytest.mark.parametrize("trickle", ["headers", "body", None])
def test_connection_deadline_bounds_buffered_reads_without_network(monkeypatch, trickle):
    clock, timers, accepted, closed = [0], [], [], []
    header = (b"POST /lineworks/callback HTTP/1.0\r\n"
              b"Content-Length: 10\r\nContent-Type: application/json\r\n"
              b"X-WORKS-Signature: synthetic\r\nX-WORKS-BotId: synthetic\r\n\r\n")
    request = header + b"0123456789"

    class Timer:
        def __init__(self, interval, callback):
            self.interval, self.callback = interval, callback
            self.started = self.cancelled = self.joined = self.fired = False
            timers.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def join(self):
            assert self.started and self.cancelled
            self.joined = True

    class Raw(io.RawIOBase):
        position = 0

        def readable(self):
            return True

        def readinto(self, buf):
            if closed or self.position == len(request):
                return 0
            slow = trickle == "headers" or (trickle == "body" and self.position >= len(header))
            size = 1 if slow else min(len(buf), len(header) - self.position) \
                if self.position < len(header) else len(request) - self.position
            if slow:
                clock[0] += 1  # each recv arrives within the 5-second idle timeout
                for timer in timers:
                    if timer.started and not timer.fired and clock[0] >= timer.interval:
                        timer.fired = True
                        timer.callback()
                if closed:
                    return 0
            data = request[self.position:self.position + size]
            buf[:len(data)] = data
            self.position += len(data)
            return len(data)

    monkeypatch.setattr(server, "HTTPServer", lambda _addr, handler: handler)
    monkeypatch.setattr(server.threading, "Timer", Timer)
    handler_type = server.callback_server(SimpleNamespace(
        accept=lambda *args: accepted.append(args) or 200))
    handler = object.__new__(handler_type)
    handler.connection = SimpleNamespace(shutdown=lambda *_args: closed.append("shutdown"),
                                         close=lambda: closed.append("close"))
    handler.rfile = io.BufferedReader(Raw())
    handler.wfile = io.BytesIO()
    handler.client_address = ("synthetic", 0)
    handler.server = SimpleNamespace(server_name="synthetic", server_port=0)
    handler.handle()
    if trickle:
        assert clock[0] <= 5
        assert accepted == [] and closed == ["shutdown", "close"]
    else:
        assert accepted == [(b"0123456789", "synthetic", "synthetic")]
        assert closed == []
    assert len(timers) == 1 and timers[0].cancelled and timers[0].joined


@pytest.mark.parametrize("failure", ["start", "socket", "handler"])
def test_deadline_lifecycle_on_startup_and_handler_errors(monkeypatch, failure):
    observed = []

    class Timer:
        def __init__(self, *_args):
            pass

        def start(self):
            observed.append("start")
            if failure == "start":
                raise RuntimeError("synthetic_start_failure")

        def cancel(self):
            observed.append("cancel")

        def join(self):
            observed.append("join")

    def handle(_handler):
        observed.append("handle")
        if failure == "socket":
            raise OSError("synthetic_closed_socket")
        raise RuntimeError("synthetic_handler_failure")

    monkeypatch.setattr(server, "HTTPServer", lambda _addr, handler: handler)
    monkeypatch.setattr(server.threading, "Timer", Timer)
    monkeypatch.setattr(server.BaseHTTPRequestHandler, "handle", handle)
    handler = object.__new__(server.callback_server(SimpleNamespace()))
    if failure == "socket":
        handler.handle()
    else:
        with pytest.raises(RuntimeError, match="synthetic_"):
            handler.handle()
    assert observed == (["start"] if failure == "start"
                        else ["start", "handle", "cancel", "join"])
