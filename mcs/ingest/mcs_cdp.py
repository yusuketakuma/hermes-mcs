"""Short-lived Chrome DevTools WebSocket framing and evaluation."""
from __future__ import annotations

import base64
from contextlib import suppress
import hashlib
import json
import os
import re
import socket
import time
import urllib.parse

from mcs_contract import BootstrapError


def profile_chrome_pids(profile_dir: str, chrome_bin: str, *,
                        timeout: float = 10) -> list[int]:
    """Pids of Chrome main processes of exactly this dedicated profile; [] when unknown."""
    import subprocess
    if (not profile_dir or not os.path.isabs(profile_dir)
            or not chrome_bin or not os.path.isabs(chrome_bin)):
        return []
    # the next token must be a flag or URI (about:blank too): "<profile> copy" is another profile
    flag = re.compile(r"(?:^|\s)--user-data-dir=" + re.escape(profile_dir)
                      + r"(?:\s--|\s[a-z][a-z0-9+.-]*:|$)")
    try:
        result = subprocess.run(["ps", "-axww", "-o", "pid=,command="],
                                capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    pids = []
    for line in result.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        # ps flattens argv: paths may contain spaces, but Chrome's flags follow its executable.
        program, separator, _ = command.partition(" --")
        if (pid.isdigit() and int(pid) != os.getpid() and separator
                and os.path.realpath(program) == os.path.realpath(chrome_bin)
                and flag.search(command)
                and " --type=" not in f" {command}"):
            pids.append(int(pid))
    return pids


class _WSConn:
    """Minimal RFC 6455 client — Chrome DevTools ws:// on loopback.

    The declared dependency set is stdlib-only (AGENTS.md); the old
    `import websockets` worked only because the production interpreter
    happened to be another project's venv. Covers exactly what CDP eval
    needs: upgrade handshake (with Accept verification), masked client
    text frames, server fragmentation reassembly, ping->pong, close.
    """
    _MAX_MSG = 8 * 1024 * 1024

    def __init__(self, url: str, timeout: float):
        try:
            u = urllib.parse.urlparse(url)
            host, port = u.hostname, u.port or 80
        except ValueError:
            raise BootstrapError("cdp_ws_url_invalid") from None
        if u.scheme != "ws" or not host:
            raise BootstrapError("cdp_ws_url_invalid")
        self._deadline = time.monotonic() + timeout
        self._buf = bytearray()
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        self._sock = socket.create_connection((host, port), timeout=timeout)
        try:
            self._handshake(host, port, path)
        except BaseException:
            self.close()
            raise

    def _handshake(self, host: str, port: int, path: str):
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall(
            (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
             "Upgrade: websocket\r\nConnection: Upgrade\r\n"
             f"Sec-WebSocket-Key: {key}\r\n"
             "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = self._read_until(b"\r\n\r\n", 65536)
        lines = head.split(b"\r\n")
        accept = next((ln.split(b":", 1)[1].strip() for ln in lines[1:]
                       if ln.lower().startswith(b"sec-websocket-accept:")),
                      None)
        want = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
            .encode()).digest())
        if not lines[0].startswith(b"HTTP/") or b" 101" not in lines[0] \
                or accept != want:
            raise BootstrapError("cdp_ws_handshake")

    def _fill(self):
        if time.monotonic() > self._deadline:
            raise BootstrapError("cdp_ws_timeout")
        chunk = self._sock.recv(65536)
        if not chunk:
            raise BootstrapError("cdp_ws_closed")
        self._buf += chunk

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _read_until(self, marker: bytes, cap: int) -> bytes:
        while marker not in self._buf:
            if len(self._buf) > cap:
                raise BootstrapError("cdp_ws_too_large")
            self._fill()
        idx = self._buf.index(marker) + len(marker)
        if idx > cap:
            raise BootstrapError("cdp_ws_too_large")
        out = bytes(self._buf[:idx])
        del self._buf[:idx]
        return out

    def _send_frame(self, opcode: int, payload: bytes):
        n = len(payload)
        head = bytearray([0x80 | opcode])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self._sock.sendall(bytes(head) + masked)

    def send_text(self, text: str):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_message(self) -> bytes:
        """Reassemble one complete data message; answer pings inline."""
        message = bytearray()
        started = False
        while True:
            b0, b1 = self._read_exact(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            masked, ln = b1 & 0x80, b1 & 0x7F
            if masked:
                raise BootstrapError("cdp_ws_protocol")
            if ln == 126:
                ln = int.from_bytes(self._read_exact(2), "big")
            elif ln == 127:
                ln = int.from_bytes(self._read_exact(8), "big")
            # Validate before reading the advertised payload: a corrupt CDP
            # frame must not allocate beyond the message budget. Control
            # frames have their own RFC 6455 bound and cannot be fragmented.
            if opcode in (0x8, 0x9, 0xA):
                if not fin or ln > 125:
                    raise BootstrapError("cdp_ws_protocol")
            elif len(message) + ln > self._MAX_MSG:
                raise BootstrapError("cdp_ws_too_large")
            payload = self._read_exact(ln) if ln else b""
            if opcode == 0x9:
                self._send_frame(0xA, payload)   # ping -> pong
                continue
            if opcode == 0xA:
                continue                         # pong — ignore
            if opcode == 0x8:
                raise BootstrapError("cdp_ws_closed")
            if opcode in (0x1, 0x2):
                if started:
                    raise BootstrapError("cdp_ws_protocol")
                started = True
            elif opcode != 0x0 or not started:
                raise BootstrapError("cdp_ws_protocol")
            message.extend(payload)
            if fin:
                return bytes(message)

    def close(self):
        with suppress(OSError):
            self._sock.close()


def _connected_peer_owned(conn, peer_profile: dict) -> bool:
    """Verify the server end of this established socket before sending credentials."""
    import subprocess

    def remaining():
        seconds = conn._deadline - time.monotonic()
        if seconds <= 0:
            raise BootstrapError("cdp_ws_timeout")
        return min(10, seconds)

    pids = profile_chrome_pids(peer_profile["profile_dir"],
                               peer_profile["chrome_bin"], timeout=remaining())
    lsof = next((p for p in ("/usr/sbin/lsof", "/usr/bin/lsof")
                 if os.access(p, os.X_OK)), None)
    if not pids or lsof is None:
        return False
    server, client = conn._sock.getpeername(), conn._sock.getsockname()

    def endpoint(address):
        host, port = address[:2]
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"

    wanted = endpoint(server) + "->" + endpoint(client)
    host = f"[{server[0]}]" if ":" in server[0] else server[0]
    try:
        listing = subprocess.run(
            [lsof, "-nP", "-a", f"-iTCP@{host}:{server[1]}",
             "-sTCP:ESTABLISHED", "-Fpun"], capture_output=True, text=True,
            timeout=remaining())
    except (OSError, subprocess.SubprocessError):
        return False
    if listing.returncode != 0:
        return False
    owners, pid, uid = [], None, None
    for line in listing.stdout.splitlines():
        if line[:1] == "p":
            pid = int(line[1:]) if line[1:].isdigit() else None
            uid = None
        elif line[:1] == "u":
            uid = int(line[1:]) if line[1:].isdigit() else None
        elif line == "n" + wanted:
            owners.append((pid, uid))
    return bool(owners) and all(
        pid in pids and uid == os.getuid() for pid, uid in owners)


def evaluate(ws_url: str, expression: str, timeout: float = 15, *,
             peer_profile: dict | None = None):
    """Runtime.evaluate over the minimal ws client; returns the
    result.value, or raises BootstrapError/MCSError — never a bare
    KeyError on a malformed CDP reply."""
    conn = _WSConn(ws_url, timeout)
    try:
        if peer_profile is not None and not _connected_peer_owned(conn, peer_profile):
            raise BootstrapError("cdp_peer_unowned")
        conn.send_text(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expression,
                       "returnByValue": True}}))
        while True:
            m = json.loads(conn.recv_message())
            if isinstance(m, dict) and m.get("id") == 1:
                result = m.get("result")
                if not isinstance(result, dict):
                    raise BootstrapError("cdp_eval_malformed")
                inner = result.get("result")
                return inner.get("value") if isinstance(inner, dict) \
                    else None
    finally:
        conn.close()
