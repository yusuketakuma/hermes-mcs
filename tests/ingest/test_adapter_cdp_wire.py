"""Loopback CDP framing, Chrome profile identity and hung-tab recovery contracts."""
import mcs_contract
import json
import subprocess
import time
from types import SimpleNamespace
import pytest
import mcs_adapter
import mcs_cdp


class _FakeSock:
    """In-memory socket — no real network (conftest blocks sockets)."""
    def __init__(self, incoming: bytes = b""):
        self.incoming = bytearray(incoming)
        self.sent = bytearray()
        self.closed = False

    def recv(self, n):
        out = bytes(self.incoming[:n])
        del self.incoming[:n]
        return out

    def sendall(self, data):
        self.sent += data

    def close(self):
        self.closed = True


def _ws_frame(payload: bytes, opcode=0x1, fin=True, mask=False) -> bytes:
    b0 = (0x80 if fin else 0) | opcode
    n = len(payload)
    if n < 126:
        head = bytes([b0, (0x80 if mask else 0) | n])
    elif n < 65536:
        head = bytes([b0, (0x80 if mask else 0) | 126]) + n.to_bytes(2, "big")
    else:
        head = bytes([b0, (0x80 if mask else 0) | 127]) + n.to_bytes(8, "big")
    if not mask:
        return head + payload
    key = b"\x11\x22\x33\x44"
    return head + key + bytes(b ^ key[i & 3] for i, b in enumerate(payload))


def _ws_conn(incoming: bytes = b""):
    conn = mcs_cdp._WSConn.__new__(mcs_cdp._WSConn)
    conn._sock = _FakeSock(incoming)
    conn._buf = bytearray()
    conn._deadline = time.monotonic() + 5
    return conn


def test_ws_handshake_verifies_accept_key():
    import base64 as b64
    import hashlib as hl
    conn = _ws_conn()

    class Sock(_FakeSock):
        def sendall(self, data):
            super().sendall(data)
            # extract the client key, then feed a valid 101 response
            # carrying the matching Sec-WebSocket-Accept
            req = data.decode().split("\r\n")
            k = next(line.split(": ", 1)[1] for line in req
                     if line.startswith("Sec-WebSocket-Key:"))
            accept = b64.b64encode(hl.sha1(
                (k + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                .encode()).digest()).decode()
            self.incoming += (f"HTTP/1.1 101 Switching Protocols\r\n"
                              f"Upgrade: websocket\r\n"
                              f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                              ).encode()

    conn._sock = Sock()
    conn._handshake("127.0.0.1", 9222, "/devtools/page/x")
    sent = conn._sock.sent.decode()
    assert sent.startswith("GET /devtools/page/x HTTP/1.1")
    assert "Upgrade: websocket" in sent


def test_ws_handshake_rejects_wrong_accept():
    conn = _ws_conn(b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Sec-WebSocket-Accept: wrong\r\n\r\n")
    with pytest.raises(mcs_contract.BootstrapError, match="handshake"):
        conn._handshake("127.0.0.1", 9222, "/x")


@pytest.mark.parametrize("length", [17, 65537])
def test_ws_header_terminator_cannot_bypass_header_limit(length):
    # A terminator in the final read must not certify an oversized header.
    conn = _ws_conn(b"x" * (length - 4) + b"\r\n\r\n")
    cap = 16 if length == 17 else 65536
    with pytest.raises(mcs_contract.BootstrapError, match="cdp_ws_too_large"):
        conn._read_until(b"\r\n\r\n", cap)
    # Bytes after a valid header belong to frames, not to the header budget.
    header = b"x" * (cap - 4) + b"\r\n\r\n"
    conn = _ws_conn(header + b"synthetic-frame")
    assert conn._read_until(b"\r\n\r\n", cap) == header
    assert conn._read_exact(len(b"synthetic-frame")) == b"synthetic-frame"


def test_ws_constructor_closes_socket_after_failed_handshake(monkeypatch):
    sock = _FakeSock(b"HTTP/1.1 403 Forbidden\r\n\r\n")
    monkeypatch.setattr(mcs_cdp.socket, "create_connection",
                        lambda *args, **kwargs: sock)
    with pytest.raises(mcs_contract.BootstrapError, match="cdp_ws_handshake"):
        mcs_cdp._WSConn("ws://127.0.0.1:9222/x", 5)
    assert sock.closed


def test_ws_constructor_closes_socket_after_invalid_unicode_url(monkeypatch):
    sock = _FakeSock()
    monkeypatch.setattr(mcs_cdp.socket, "create_connection",
                        lambda *args, **kwargs: sock)
    with pytest.raises(UnicodeEncodeError):
        mcs_cdp._WSConn("ws://127.0.0.1:9222/devtools/\ud800", 5)
    assert sock.closed
    assert sock.sent == b""


def test_ws_constructor_allocates_buffer_before_acquiring_socket(monkeypatch):
    connections = []

    def opened(*args, **kwargs):
        sock = _FakeSock()
        connections.append(sock)
        return sock

    def allocation_failed(*args, **kwargs):
        raise MemoryError("synthetic buffer allocation failed")

    monkeypatch.setattr(mcs_cdp.socket, "create_connection", opened)
    monkeypatch.setattr(mcs_cdp, "bytearray", allocation_failed, raising=False)
    with pytest.raises(MemoryError, match="synthetic buffer allocation failed"):
        mcs_cdp._WSConn("ws://127.0.0.1:9222/devtools/synthetic", 5)
    assert connections == []


@pytest.mark.parametrize("close_failure", [False, True])
def test_ws_constructor_preserves_interruption_during_handshake(monkeypatch, close_failure):
    sock = _FakeSock()
    interruption = KeyboardInterrupt("synthetic interrupted handshake")

    def interrupted(*args):
        raise interruption

    def close():
        sock.closed = True
        if close_failure:
            raise OSError("synthetic close failure")

    monkeypatch.setattr(mcs_cdp.socket, "create_connection",
                        lambda *args, **kwargs: sock)
    monkeypatch.setattr(mcs_cdp._WSConn, "_handshake", interrupted)
    monkeypatch.setattr(sock, "close", close)
    with pytest.raises(KeyboardInterrupt) as failure:
        mcs_cdp._WSConn("ws://127.0.0.1:9222/devtools/synthetic", 5)
    assert failure.value is interruption
    assert sock.closed


def test_ws_recv_reassembles_fragments_and_answers_ping():
    payload = _ws_frame(b'{"id":1,"par', fin=False) \
        + _ws_frame(b"ping!", opcode=0x9) \
        + _ws_frame(b'tial"}', opcode=0x0, fin=True)
    conn = _ws_conn(payload)
    assert conn.recv_message() == b'{"id":1,"partial"}'
    # a pong frame was sent in reply to the ping
    pong = bytes(conn._sock.sent)
    assert pong[0] & 0x0F == 0xA and pong[0] & 0x80
    n = pong[1] & 0x7F
    mask = pong[2:6]
    assert bytes(b ^ mask[i & 3] for i, b in enumerate(pong[6:6+n])) \
        == b"ping!"


def test_ws_recv_close_and_oversize_fail():
    conn = _ws_conn(_ws_frame(b"bye", opcode=0x8))
    try:
        conn.recv_message()
    except mcs_contract.BootstrapError:
        pass
    else:
        raise AssertionError("close frame must end the connection")
    conn = _ws_conn(_ws_frame(b"x" * 100, fin=False))
    conn._MAX_MSG = 10
    with pytest.raises(mcs_contract.BootstrapError, match="too_large"):
        conn.recv_message()


def test_ws_recv_rejects_masked_server_frames():
    conn = _ws_conn(_ws_frame(b'{"id":1}', mask=True))
    with pytest.raises(mcs_contract.BootstrapError, match="cdp_ws_protocol"):
        conn.recv_message()


@pytest.mark.parametrize(("prefix", "opcode"), [(b"", 0x1),
                          (_ws_frame(b"12345678", fin=False), 0x0)])
def test_ws_rejects_oversize_from_header_before_reading_body(prefix, opcode):
    # No payload follows the advertised length: rejecting before reading it
    # prevents both unbounded buffering and waiting for an impossible frame.
    conn = _ws_conn(prefix + bytes([0x80 | opcode, 127])
                    + (11).to_bytes(8, "big"))
    conn._MAX_MSG = 16 if prefix else 10
    with pytest.raises(mcs_contract.BootstrapError, match="cdp_ws_too_large"):
        conn.recv_message()


@pytest.mark.parametrize(("opcode", "fin", "length"), [(0x9, True, 126),
                                               (0x9, False, 1),
                                               (0xA, True, 126),
                                               (0x8, True, 126)])
def test_ws_rejects_invalid_control_frame_before_reading_body(opcode, fin, length):
    head = bytes([(0x80 if fin else 0) | opcode, 126])
    conn = _ws_conn(head + length.to_bytes(2, "big"))
    with pytest.raises(mcs_contract.BootstrapError, match="cdp_ws_protocol"):
        conn.recv_message()


def test_ws_eval_roundtrip_and_id_match(monkeypatch):
    """_ws_eval waits for the frame whose id matches the request."""
    request = {}
    reply = _ws_frame(json.dumps({"id": 99, "result": {}}).encode()) \
        + _ws_frame(json.dumps(
            {"id": 1, "result": {"result": {"value": "tok123"}}}).encode())
    conn = _ws_conn(reply)
    real_send = conn.send_text

    def send(text):
        request.update(json.loads(text))
        real_send(text)

    conn.send_text = send
    monkeypatch.setattr(mcs_cdp, "_WSConn", lambda *a, **k: conn)
    assert mcs_cdp.evaluate("ws://127.0.0.1:9/x", "1+1", 5) == "tok123"
    assert request["method"] == "Runtime.evaluate"
    assert request["params"]["returnByValue"] is True


def test_chrome_launch_disables_on_device_model_download(monkeypatch):
    import subprocess
    a = mcs_adapter.MCSAdapter()
    launched = []
    monkeypatch.setattr(a, "_cdp_up", lambda: bool(launched))
    monkeypatch.setattr(a, "_sleep_bounded", lambda s: None)
    monkeypatch.setattr(a, "_stop_profile_chrome", lambda profile, chrome: False)
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: launched.append(argv))
    a._ensure_chrome("/synthetic/profile", "/synthetic/chrome")
    features = [x for x in launched[0] if x.startswith("--disable-features=")]
    assert features == ["--disable-features=OptimizationGuideModelDownloading,"
                        "OptimizationHintsFetching,"
                        "OptimizationGuideOnDeviceModel"]


def _tab(ident, host="www.medical-care.net"):
    return {"type": "page", "id": ident, "url": f"https://{host}/home",
            "webSocketDebuggerUrl": f"ws://127.0.0.1:9333/devtools/page/{ident}"}


@pytest.mark.parametrize("kind,closed", [("network_error", True),
                                         ("deadline_exceeded", False)])
def test_hung_mcs_tab_is_closed_and_verified(monkeypatch, kind, closed):
    """A browser that answers /json/version can still hold an MCS tab
    whose renderer never answers — every token read then times out.
    Only that tab closes; a run deadline is not evidence of a hang."""
    a = mcs_adapter.MCSAdapter()
    hung, live, other = "A" * 32, "B" * 32, "C" * 32
    tabs = [_tab(hung), _tab(live), _tab(other, "example.invalid")]
    calls = []

    def cdp_json(path, method="GET", timeout=5):
        calls.append(path)
        if path.startswith("/json/close/"):
            tabs[:] = [t for t in tabs if t["id"] != path.rsplit("/", 1)[1]]
            raise mcs_contract.MCSError("network_error")   # plain-text reply
        return list(tabs)

    def cdp_eval(ws, expr, timeout=15):
        assert expr == "1" and timeout == mcs_adapter.HUNG_PROBE_S
        if ws.endswith(hung):
            raise mcs_contract.MCSError(kind, retryable=True)
        return 1

    monkeypatch.setattr(a, "_cdp_json", cdp_json)
    monkeypatch.setattr(a, "_cdp_eval", cdp_eval)
    assert a._heal_hung_pages() == int(closed)
    assert a.healed_tabs == int(closed)
    assert (f"/json/close/{hung}" in calls) is closed
    assert not any(p.endswith(live) or p.endswith(other) for p in calls)


def test_ensure_chrome_heals_tabs_only_when_browser_is_up(monkeypatch):
    a = mcs_adapter.MCSAdapter()
    healed = []
    monkeypatch.setattr(a, "_heal_hung_pages", lambda: healed.append(1))
    monkeypatch.setattr(a, "_cdp_up", lambda: True)
    a._ensure_chrome("/synthetic/profile", "/synthetic/chrome")
    assert healed == [1]


def test_heal_never_raises_when_cdp_listing_fails(monkeypatch):
    a = mcs_adapter.MCSAdapter()

    def down(*args, **kwargs):
        raise mcs_contract.MCSError("network_error")
    monkeypatch.setattr(a, "_cdp_json", down)
    assert a._heal_hung_pages() == 0


def test_cdp_down_stops_only_this_profiles_chrome_before_relaunch(monkeypatch):
    """A hung browser (or the profile opened without the debugging port)
    absorbs a same-profile relaunch, so CDP never comes up. Only main
    processes of exactly this profile are stopped; helpers, other
    profiles and prefix-matching paths are left alone."""
    import signal
    a = mcs_adapter.MCSAdapter()
    listing = "\n".join([
        "101 /Apps/Chrome --remote-debugging-port=9333 --user-data-dir=/p/chrome-profile https://x",
        "102 /Apps/Chrome Helper --type=renderer --user-data-dir=/p/chrome-profile",
        "103 /Apps/Chrome --user-data-dir=/p/chrome-profile-other",
        "104 /Apps/Chrome --user-data-dir=/p/other",
        "105 /usr/bin/python3 /tmp/synthetic-launcher.py --user-data-dir=/p/chrome-profile",
        "106 /Apps/Chrome Other --user-data-dir=/p/chrome-profile",
    ])
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=listing, returncode=0))
    alive, sent = {101, 105, 106}, []

    def kill(pid, sig):
        if sig == 0:
            if pid not in alive:
                raise ProcessLookupError
            return
        sent.append((pid, sig))
        if sig == signal.SIGKILL:
            alive.discard(pid)

    monkeypatch.setattr(mcs_adapter.os, "kill", kill)
    monkeypatch.setattr(a, "_sleep_bounded", lambda s: None)
    assert a._stop_profile_chrome("/p/chrome-profile", "/Apps/Chrome") is True
    # SIGTERM ignored (hung) -> escalates to SIGKILL, only for pid 101
    assert sent == [(101, signal.SIGTERM), (101, signal.SIGKILL)]
    assert a.chrome_restarted is True
    assert a._stop_profile_chrome("relative/profile", "/Apps/Chrome") is False
    assert a._stop_profile_chrome("/p/chrome-profile", "relative/chrome") is False


@pytest.mark.parametrize("returncode", [0, 1])
def test_profile_stop_checks_configured_executable_with_spaces(monkeypatch, tmp_path, returncode):
    import signal
    chrome = tmp_path / "Google Chrome.app/Contents/MacOS/Google Chrome"
    chrome.parent.mkdir(parents=True)
    chrome.touch()
    alias = tmp_path / "chrome-alias"
    alias.symlink_to(chrome)
    listing = (f"101 {chrome} --user-data-dir=/synthetic/profile\n"
               "102 /usr/bin/python3 /synthetic/launcher.py --user-data-dir=/synthetic/profile\n")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kw:
                        SimpleNamespace(stdout=listing, returncode=returncode))
    alive, sent = {101, 102}, []

    def kill(pid, sig):
        if sig == 0:
            if pid not in alive:
                raise ProcessLookupError
        else:
            sent.append((pid, sig))
            alive.discard(pid)

    monkeypatch.setattr(mcs_adapter.os, "kill", kill)
    adapter = mcs_adapter.MCSAdapter()
    monkeypatch.setattr(adapter, "_sleep_bounded", lambda *_: None)
    assert adapter._stop_profile_chrome("/synthetic/profile", str(alias)) is (returncode == 0)
    assert sent == ([(101, signal.SIGTERM)] if returncode == 0 else [])


@pytest.mark.parametrize("url", ["ws://192.0.2.1:9222/x", "ws://example.test:9222/x",
                                 "ws://127.0.0.1.example.test:9222/x"])
def test_ws_refuses_non_loopback_before_connecting(monkeypatch, url):
    monkeypatch.setattr(mcs_cdp.socket, "create_connection",
                        lambda *a, **k: pytest.fail("must not connect"))
    with pytest.raises(mcs_contract.BootstrapError, match="cdp_ws_url_invalid"):
        mcs_cdp._WSConn(url, 5)
