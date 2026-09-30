"""mcs_adapter / mcs_worker wire contract: list_unread / mark_read /
list_projects / fetch_* pagination / fetch_latest / self_profile,
websocket framing, Keychain + auto_login, request/worker deadlines,
assert_allowed_url.  In-memory fakes only — conftest blocks sockets."""

import base64
import hashlib
import json
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcs_adapter
from ingest_testkit import _message


class _ProjectsAdapter(mcs_adapter.MCSAdapter):
    def __init__(self, has_next):
        self.has_next = has_next

    def _get(self, path, params=None, extend_session=True):
        return {"projects": [], "paginate": {"has_next": self.has_next}}


def test_project_inventory_fails_when_page_cap_is_incomplete():
    with pytest.raises(mcs_adapter.MCSError) as error:
        _ProjectsAdapter(True).list_projects(max_pages=1)
    assert error.value.kind == "pages_exceeded"


def test_unread_reply_snippet_remains_missing():
    parent = _message(mid=10)
    parent.replies = [_message(mid=20, state="snippet", parent_id=10,
                               unread=True)]
    adapter = mcs_adapter.MCSAdapter()
    adapter.fetch_thread = lambda *_: [
        _message(mid=20, state="snippet", parent_id=10)
    ]

    result = adapter.fetch_unread_replies(parent)

    assert result.missing == [20]


def test_contradictory_mark_read_response_is_unknown(monkeypatch):
    # the mark read is a GET on the message list (keep_read_status
    # omitted); confirmation comes from the detail read afterwards —
    # oldest_unread_message still present means the mark did not hold
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda path, params=None, **k: {
        "project": {"is_archived": False,
                    "oldest_unread_message": {"id": 7}},
    }
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter.mark_patient_read(1, 123)
    assert error.value.kind == "mark_result_unknown"


def test_mark_read_confirms_when_oldest_unread_is_gone():
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda path, params=None, **k: (
        {"project": {"is_archived": False}}     # detail: key absent = read
        if path == "/projects/1" else {"messages": [], "paginate": {}})
    assert adapter.mark_patient_read(1, 123)["project"]["is_archived"] is False


def test_mark_read_empty_project_is_unknown():
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda path, params=None, **k: (
        {"project": {}}                        # no is_archived — not real
        if path == "/projects/1" else {"messages": [], "paginate": {}})
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter.mark_patient_read(1, 123)
    assert error.value.kind == "mark_result_unknown"


def _unread_project(pid: int, unread: bool = True) -> dict:
    return {"id": pid, "type": "medical", "is_unread": unread,
            "karte": {"id": pid * 10, "last_name": "T", "first_name": "P",
                      "disease": "",
                      "station": {"name": "st"}}}


class _UnreadListAdapter(mcs_adapter.MCSAdapter):
    """Serves /projects pages from a schedule of
    (timestamp, has_next, projects) tuples, one entry per request."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def _get(self, path, params=None, extend_session=True):
        ts, has_next, projs = self.pages[len(self.calls)]
        self.calls.append(params["page"])
        return {"projects": projs,
                "paginate": {"timestamp": ts, "has_next": has_next}}


def test_list_unread_filters_read_projects_and_uses_earliest_ts():
    adapter = _UnreadListAdapter([
        (100, True, [_unread_project(11), _unread_project(12, unread=False)]),
        # page timestamps are a per-request server clock — they legitimately
        # differ between pages; the snapshot keeps the earliest so mark-read
        # can never cover a post made after the walk started.
        (105, False, [_unread_project(13)]),
    ])

    snap = adapter.list_unread()

    assert snap.timestamp == 100
    assert adapter.calls == [1, 2]
    assert [p.project_id for p in snap.patients] == [11, 13]


def test_list_unread_fails_when_pagination_sticks():
    adapter = _UnreadListAdapter([
        (100, True, [_unread_project(11)]),
        (100, True, [_unread_project(11)]),  # same ids again
    ])

    with pytest.raises(mcs_adapter.SchemaError) as error:
        adapter.list_unread()
    assert "not advancing" in error.value.detail


def test_list_unread_rejects_missing_is_unread():
    adapter = _UnreadListAdapter([
        (100, False, [{"id": 11, "type": "medical", "karte": {}}]),
    ])

    with pytest.raises(mcs_adapter.SchemaError) as error:
        adapter.list_unread()
    assert "is_unread" in error.value.detail


def test_list_unread_fails_on_schema_error():
    class Adapter(mcs_adapter.MCSAdapter):
        def __init__(self):
            self.calls = 0

        def _get(self, path, params=None, extend_session=True):
            self.calls += 1
            return {"projects": []}  # paginate missing

    adapter = Adapter()
    with pytest.raises(mcs_adapter.SchemaError):
        adapter.list_unread()
    assert adapter.calls == 1


def test_history_returns_saved_pages_and_error():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if params["page"] == 1:
                return {"messages": [{"id": 1, "comment": "ok"}],
                        "paginate": {"has_next": True}}
            return {"messages": "broken", "paginate": {"has_next": False}}

    batch = Adapter().fetch_history(1, 0, max_pages=2)
    assert [m.message_id for m in batch.messages] == [1]
    assert batch.pages == 1
    assert batch.reached is False
    assert isinstance(batch.error, mcs_adapter.SchemaError)


def test_invalid_nested_text_is_reported_as_schema_error():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {
                "projects": [{
                    "id": 1,
                    "karte": {},
                    "last_message": {"created_at": 7},
                }],
                "paginate": {"has_next": False},
            }

    with pytest.raises(mcs_adapter.SchemaError):
        Adapter().list_projects()


@pytest.mark.parametrize("kind", ["unread", "history"])
def test_pagination_schema_failure_retains_completed_pages(kind):
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            page = params["page"]
            return {"messages": [{"id": page, "comment": "body"}],
                    "paginate": {"has_next": True if page == 1 else "false"}}

    adapter = Adapter()
    batch = (adapter.fetch_unread_messages(1, 123, max_pages=2)
             if kind == "unread" else adapter.fetch_history(1, 0, max_pages=2))
    assert [m.message_id for m in batch.messages] == [1]
    assert batch.pages == 1 and not batch.reached
    assert isinstance(batch.error, mcs_adapter.SchemaError)


def test_invalid_history_date_does_not_certify_cutoff():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"messages": [{"id": 1, "comment": "body",
                                  "created_at": "invalid"}],
                    "paginate": {"has_next": True}}

    batch = Adapter().fetch_history(1, 123, max_pages=1)
    assert not batch.reached
    assert isinstance(batch.error, mcs_adapter.SchemaError)


_CUTOFF = int(datetime.fromisoformat(
    "2026-09-21T00:00:00+09:00").timestamp())


def test_history_ordered_cutoff_still_walks_to_natural_end():
    """FIX-AD1: under sort=pinned a below-cutoff tail never certifies
    'reached' — the walk continues until has_next is false, because a
    page-boundary pinned straggler could resume above the cutoff."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if params["page"] == 1:
                return {"messages": [
                    {"id": 3, "comment": "n2",
                     "created_at": "2026-09-22T00:00:00+09:00"},
                    {"id": 2, "comment": "n1",
                     "created_at": "2026-09-21T12:00:00+09:00"},
                    {"id": 1, "comment": "old",
                     "created_at": "2020-01-01T00:00:00+09:00"}],
                    "paginate": {"has_next": True}}
            return {"messages": [
                {"id": 4, "comment": "older",
                     "created_at": "2019-01-01T00:00:00+09:00"}],
                    "paginate": {"has_next": False}}

    batch = Adapter().fetch_history(1, _CUTOFF, max_pages=5)
    assert [m.message_id for m in batch.messages] == [3, 2]
    assert batch.reached and batch.pages == 2 and batch.error is None


def test_history_pinned_straggler_at_page_boundary_keeps_walking():
    """FIX-AD1 regression: an old item ending a page must not terminate
    the walk — the next page can resume above the cutoff."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if params["page"] == 1:
                return {"messages": [
                    {"id": 3, "comment": "new",
                     "created_at": "2026-09-22T00:00:00+09:00"},
                    {"id": 9, "comment": "pinned-old-at-boundary",
                     "created_at": "2020-01-01T00:00:00+09:00"}],
                    "paginate": {"has_next": True}}
            return {"messages": [
                {"id": 2, "comment": "new-on-page-2",
                     "created_at": "2026-09-21T12:00:00+09:00"}],
                    "paginate": {"has_next": False}}

    batch = Adapter().fetch_history(1, _CUTOFF, max_pages=5)
    assert [m.message_id for m in batch.messages] == [3, 2]
    assert batch.reached and batch.pages == 2


def test_history_pinned_order_violation_walks_to_natural_end():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if params["page"] == 1:
                return {"messages": [
                    {"id": 9, "comment": "pinned-old",
                     "created_at": "2020-01-01T00:00:00+09:00"},
                    {"id": 3, "comment": "new",
                     "created_at": "2026-09-22T00:00:00+09:00"}],
                    "paginate": {"has_next": True}}
            return {"messages": [
                {"id": 2, "comment": "new2",
                 "created_at": "2026-09-21T12:00:00+09:00"}],
                "paginate": {"has_next": False}}

    batch = Adapter().fetch_history(1, _CUTOFF, max_pages=5)
    assert [m.message_id for m in batch.messages] == [3, 2]
    assert batch.reached and batch.pages == 2 and batch.error is None


def test_history_order_violation_without_end_is_not_certified():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"messages": [
                {"id": 9 - params["page"], "comment": "pinned-old",
                 "created_at": "2020-01-01T00:00:00+09:00"},
                {"id": 100 + params["page"], "comment": "new",
                 "created_at": "2026-09-22T00:00:00+09:00"}],
                "paginate": {"has_next": True}}

    batch = Adapter().fetch_history(1, _CUTOFF, max_pages=2)
    assert [m.message_id for m in batch.messages] == [101, 102]
    assert not batch.reached and batch.pages == 2


def test_paginated_thread_is_not_reported_complete():
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda *a, **k: {
        "messages": [{"id": 2, "comment": "body"}],
        "paginate": {"has_next": True}}
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter.fetch_thread(1, 1)
    assert error.value.kind == "thread_incomplete"


def test_malformed_mark_response_stays_unknown():
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda path, params=None, **k: (
        {"data": [1]}                        # no project object at all
        if path == "/projects/1" else {"messages": [], "paginate": {}})
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter.mark_patient_read(1, 123)
    assert error.value.kind == "mark_result_unknown"


def test_assert_allowed_url_bad_port_is_mcserror():
    """A malformed port (':bad', out-of-range, broken bracket) makes
    urlparse's .port raise ValueError — it must surface as MCSError
    'url_not_allowed', not leak as a bare ValueError that escapes
    stage_attachments' except MCSError and poisons the whole tick."""
    for bad in ("https://www.medical-care.net:bad/x",
                "https://www.medical-care.net:99999/x",
                "https://synthetic@www.medical-care.net/f",
                "https://www.medical-care.net/f#fragment",
                "https://[::1/x"):
        with pytest.raises(mcs_adapter.MCSError) as e:
            mcs_adapter._assert_allowed_url(bad)
        assert e.value.kind == "url_not_allowed"
    # allowed still passes
    mcs_adapter._assert_allowed_url("https://www.medical-care.net/f")
    mcs_adapter._assert_allowed_url("https://www.medical-care.net:443/f")


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
    conn = mcs_adapter._WSConn.__new__(mcs_adapter._WSConn)
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
    with pytest.raises(mcs_adapter.BootstrapError, match="handshake"):
        conn._handshake("127.0.0.1", 9222, "/x")


def test_ws_constructor_closes_socket_after_failed_handshake(monkeypatch):
    sock = _FakeSock(b"HTTP/1.1 403 Forbidden\r\n\r\n")
    monkeypatch.setattr(mcs_adapter.socket, "create_connection",
                        lambda *args, **kwargs: sock)
    with pytest.raises(mcs_adapter.BootstrapError, match="cdp_ws_handshake"):
        mcs_adapter._WSConn("ws://127.0.0.1:9222/x", 5)
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
    except mcs_adapter.BootstrapError:
        pass
    else:
        raise AssertionError("close frame must end the connection")
    conn = _ws_conn(_ws_frame(b"x" * 100, fin=False))
    conn._MAX_MSG = 10
    with pytest.raises(mcs_adapter.BootstrapError, match="too_large"):
        conn.recv_message()


def test_ws_recv_rejects_masked_server_frames():
    conn = _ws_conn(_ws_frame(b'{"id":1}', mask=True))
    with pytest.raises(mcs_adapter.BootstrapError, match="cdp_ws_protocol"):
        conn.recv_message()


@pytest.mark.parametrize(("prefix", "opcode"), [(b"", 0x1),
                          (_ws_frame(b"12345678", fin=False), 0x0)])
def test_ws_rejects_oversize_from_header_before_reading_body(prefix, opcode):
    # No payload follows the advertised length: rejecting before reading it
    # prevents both unbounded buffering and waiting for an impossible frame.
    conn = _ws_conn(prefix + bytes([0x80 | opcode, 127])
                    + (11).to_bytes(8, "big"))
    conn._MAX_MSG = 16 if prefix else 10
    with pytest.raises(mcs_adapter.BootstrapError, match="cdp_ws_too_large"):
        conn.recv_message()


@pytest.mark.parametrize(("opcode", "fin", "length"), [(0x9, True, 126),
                                               (0x9, False, 1),
                                               (0xA, True, 126),
                                               (0x8, True, 126)])
def test_ws_rejects_invalid_control_frame_before_reading_body(opcode, fin, length):
    head = bytes([(0x80 if fin else 0) | opcode, 126])
    conn = _ws_conn(head + length.to_bytes(2, "big"))
    with pytest.raises(mcs_adapter.BootstrapError, match="cdp_ws_protocol"):
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
    monkeypatch.setattr(mcs_adapter, "_WSConn", lambda *a, **k: conn)
    assert mcs_adapter._ws_eval("ws://127.0.0.1:9/x", "1+1", 5) == "tok123"
    assert request["method"] == "Runtime.evaluate"
    assert request["params"]["returnByValue"] is True


def test_keychain_password_returns_secret(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="pw123\n",
                                        stderr=""))
    assert a._keychain_password() == "pw123"


def test_token_cache_never_overwrites_predictable_staging_link(tmp_path):
    cache = tmp_path / "cache" / "token.json"
    cache.parent.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("preserve")
    Path(str(cache) + ".tmp").symlink_to(unrelated)
    adapter = mcs_adapter.MCSAdapter(token_cache=str(cache))
    adapter._write_cache("synthetic-token")
    assert unrelated.read_text() == "preserve"
    assert adapter._read_cache() == "synthetic-token"
    assert cache.stat().st_mode & 0o777 == 0o600


def test_download_uses_private_staging_without_touching_existing_part_link(tmp_path):
    destination = tmp_path / "file"
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("preserve")
    Path(str(destination) + ".part").symlink_to(unrelated)

    def worker(payload, **kwargs):
        Path(payload["partial"]).write_bytes(b"synthetic")
        return {"bytes": 9, "sha256": hashlib.sha256(b"synthetic").hexdigest()}

    adapter = mcs_adapter.MCSAdapter(worker=worker)
    adapter._token = "synthetic-token"
    adapter.download("https://www.medical-care.net/f", str(destination))
    assert unrelated.read_text() == "preserve"
    assert destination.read_bytes() == b"synthetic"
    assert not destination.is_symlink()
    assert destination.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("operation", ["api", "cdp_json"])
def test_worker_rejects_oversized_json_response(monkeypatch, operation):
    import io
    import mcs_worker
    import mcs_util

    class Response(io.BytesIO):
        status = 200
        headers = {}

    response = Response(b'"' + b"x" * 64 + b'"')
    monkeypatch.setattr(mcs_worker, "MAX_JSON_BYTES", 64)
    monkeypatch.setattr(mcs_util, "no_proxy_opener",
                        lambda *args: SimpleNamespace(open=lambda *a, **k: response))
    with pytest.raises(mcs_worker.WorkerError) as error:
        mcs_worker._execute({"operation": operation, "timeout": 1,
                             "url": mcs_adapter.API + "/projects" if operation == "api"
                             else "http://127.0.0.1:9333/json/list",
                             "method": "GET", "headers": {}})
    assert error.value.kind == "response_too_large"
    assert response.closed


def test_keychain_password_missing_returns_none(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=44, stdout="",
                                        stderr="could not be found"))
    assert a._keychain_password() is None


def test_keychain_password_locked_raises(monkeypatch, tmp_path):
    """A locked keychain (rc 36 / interaction-not-allowed) must surface as
    KeychainLocked — it is recoverable by unlock, NOT a missing entry."""
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=36, stdout="", stderr=""))
    with pytest.raises(mcs_adapter.KeychainLocked):
        a._keychain_password()


def test_keychain_password_locked_by_stderr_text(monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout="",
            stderr="User interaction is not allowed"))
    with pytest.raises(mcs_adapter.KeychainLocked):
        a._keychain_password()



def test_chrome_launch_disables_on_device_model_download(monkeypatch):
    import subprocess
    a = mcs_adapter.MCSAdapter()
    launched = []
    monkeypatch.setattr(a, "_cdp_up", lambda: bool(launched))
    monkeypatch.setattr(a, "_sleep_bounded", lambda s: None)
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: launched.append(argv))
    a._ensure_chrome("/synthetic/profile", "/synthetic/chrome")
    features = [x for x in launched[0] if x.startswith("--disable-features=")]
    assert features == ["--disable-features=OptimizationGuideModelDownloading,"
                        "OptimizationHintsFetching,"
                        "OptimizationGuideOnDeviceModel"]

def test_auto_login_reports_keychain_locked(monkeypatch, tmp_path):
    """auto_login must distinguish a locked keychain from a missing
    credential — the run alert then names the real recovery action."""
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(mcs_adapter.time, "sleep", lambda s: None)
    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_login_page",
                        lambda: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(a, "_cdp_eval", lambda ws, expr: "need_both")

    def locked(*a, **k):
        raise mcs_adapter.KeychainLocked("mcs-adapter")
    monkeypatch.setattr(a, "_keychain_password", locked)
    monkeypatch.setattr(a, "_recover_session", lambda: False)
    assert a.auto_login() == "keychain_locked"


def test_auto_login_reports_manual_required_when_entry_missing(
        monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(mcs_adapter.time, "sleep", lambda s: None)
    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_login_page",
                        lambda: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(a, "_cdp_eval", lambda ws, expr: "need_both")
    monkeypatch.setattr(a, "_keychain_password", lambda *a, **k: None)
    monkeypatch.setattr(a, "_recover_session", lambda: False)
    assert a.auto_login() == "manual_required"


def test_auto_login_recovers_live_session_without_form(
        monkeypatch, tmp_path):
    """Token expiry is not logout — when the browser session is still
    alive, a fresh localStorage token + check_session ends the recovery
    before any form fill is attempted."""
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_token_via_cdp", lambda: "tok-12345678")
    monkeypatch.setattr(a, "check_session", lambda: True)
    monkeypatch.setattr(
        a, "_login_page",
        lambda: pytest.fail("login form must not be touched"))
    assert a.auto_login() == "ok"
    assert a._token == "tok-12345678"


def test_auto_login_no_form_means_redirected_when_session_live(
        monkeypatch, tmp_path):
    """A logged-in app redirects /authentication/login back home, so a
    missing form is evidence of a session — not of failure. Recover via
    the second live-session check instead of alerting."""
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(mcs_adapter.time, "sleep", lambda s: None)
    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_login_page",
                        lambda: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(a, "_cdp_eval", lambda ws, expr: "no_form")
    seq = iter([False, True])      # pre-check fails, post-no_form ok
    monkeypatch.setattr(a, "_recover_session", lambda: next(seq))
    assert a.auto_login() == "ok"


def test_auto_login_no_form_and_dead_session_is_manual(
        monkeypatch, tmp_path):
    a = mcs_adapter.MCSAdapter(token_cache=str(tmp_path / "t.json"))
    monkeypatch.setattr(mcs_adapter.time, "sleep", lambda s: None)
    monkeypatch.setattr(a, "_ensure_chrome", lambda *a, **k: None)
    monkeypatch.setattr(a, "_login_page",
                        lambda: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(a, "_cdp_eval", lambda ws, expr: "no_form")
    monkeypatch.setattr(a, "_recover_session", lambda: False)
    assert a.auto_login() == "manual_required:no_form"


def test_projects_malformed_last_message_timestamp_is_schema_error():
    """A malformed created_at must not silently become epoch 0 — that
    would make a live project look inactive to init_data (FIX-ID2)."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {
                "projects": [{
                    "id": 1, "type": "medical",
                    "karte": {"id": 5, "last_name": "S", "first_name": "T",
                              "station": {"name": "st"}},
                    "last_message": {"created_at": "not-a-date"},
                }],
                "paginate": {"has_next": False},
            }

    with pytest.raises(mcs_adapter.SchemaError,
                       match="last_message.created_at"):
        Adapter().list_projects()


def test_projects_without_last_message_get_zero_activity():
    """Absent last_message is legitimate — last_activity=0 simply skips
    the project in init_data's active filter."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {
                "projects": [{
                    "id": 1, "type": "medical",
                    "karte": {"id": 5, "last_name": "S", "first_name": "T",
                              "station": {"name": "st"}},
                }],
                "paginate": {"has_next": False},
            }

    ps = Adapter().list_projects()
    assert len(ps) == 1 and ps[0].last_activity == 0


def test_adapter_request_raises_deadline_exceeded():
    """F13: a propagated deadline cuts HTTP work before the wire —
    retries never extend past it."""
    a = mcs_adapter.MCSAdapter()
    a._token = "t"
    a.set_deadline(time.monotonic() - 1)
    with pytest.raises(mcs_adapter.MCSError) as e:
        a._request("GET", "/projects")
    assert e.value.kind == "deadline_exceeded"


def test_unread_thread_larger_than_default_window_can_complete():
    pages = []

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, **kwargs):
            page = params["page"]
            pages.append(page)
            start = (page - 1) * 10 + 1
            return {"messages": [{"id": mid, "comment": "synthetic"}
                                 for mid in range(start, min(start + 10, 102))],
                    "paginate": {"has_next": page < 11}}

    parent = _message(mid=1000)
    parent.reply_count = 101
    parent.replies = [_message(mid=101, parent_id=1000, state="snippet", unread=True)]
    batch = Adapter().fetch_unread_replies(parent)
    assert batch.missing == [] and batch.messages[0].body_state == "full"
    assert pages == list(range(1, 12))


@pytest.mark.parametrize("operation", ["api", "download", "bootstrap"])
def test_adapter_worker_deadline_reaps_slow_reader(tmp_path, monkeypatch, operation):
    """A blocked read cannot retain a worker or a partial attachment."""
    import mcs_worker
    script = tmp_path / "slow_worker.py"
    started_file = tmp_path / "reader-started"
    module_root = Path(mcs_adapter.__file__).resolve().parents[1]
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(module_root)!r})\n"
        "import _mcs_path, mcs_adapter, mcs_worker, mcs_util\n"
        "class SlowResponse:\n"
        "    status = 200\n"
        "    headers = {}\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *args): pass\n"
        "    def read(self, *args):\n"
        f"        with open({str(started_file)!r}, 'w') as f: f.write('started')\n"
        "        while True: time.sleep(0.02)\n"
        "class Opener:\n"
        "    def open(self, *args, **kwargs): return SlowResponse()\n"
        "mcs_adapter.no_proxy_opener = lambda *args: Opener()\n"
        "mcs_util.no_proxy_opener = lambda *args: Opener()\n"
        "raise SystemExit(mcs_worker.worker_main())\n", encoding="utf-8")
    monkeypatch.setattr(mcs_worker, "_worker_command",
                        lambda: [sys.executable, str(script)])
    processes = []
    original_popen = subprocess.Popen

    def spawn(command, **kwargs):
        assert "synthetic-bearer" not in repr(command)
        assert "SYNTHETIC_PASSWORD" not in kwargs["env"]
        process = original_popen(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setenv("SYNTHETIC_PASSWORD", "synthetic-only")
    monkeypatch.setattr(mcs_worker.subprocess, "Popen", spawn)
    adapter = mcs_adapter.MCSAdapter()
    adapter._token = None if operation == "bootstrap" else "synthetic-bearer"
    adapter.set_deadline(time.monotonic() + 2)
    destination = tmp_path / "downloaded"
    destination.write_bytes(b"previous-complete-file")
    started = time.monotonic()
    if operation == "download":
        def call():
            adapter.download("https://www.medical-care.net/f",
                             str(destination))
    else:
        def call():
            adapter._request("GET", "/projects", retries=0)
    with pytest.raises(mcs_adapter.MCSError) as error:
        call()
    assert error.value.kind == "deadline_exceeded"
    assert time.monotonic() - started < 6
    assert started_file.exists()
    assert len(processes) == 1 and processes[0].poll() is not None
    assert destination.read_bytes() == b"previous-complete-file"
    assert not Path(str(destination) + ".part").exists()
    assert not list(tmp_path.glob(".download-*.part"))


def test_adapter_worker_preserves_api_form_and_http_status(monkeypatch):
    import base64
    import io
    import urllib.error
    import urllib.parse
    import mcs_worker
    import mcs_util

    observed = []

    class Response(io.BytesIO):
        status = 200
        headers = {}

    class Opener:
        def open(self, request, timeout):
            observed.append(request)
            body = (b'{"project":{"is_archived":false}}'
                    if request.full_url.endswith("/projects/1")
                    else b'{"messages":[],"paginate":{}}')
            return Response(body)

    def opener(*handlers):
        assert handlers == (mcs_util.NoRedirect,)
        return Opener()

    monkeypatch.setattr(mcs_util, "no_proxy_opener", opener)
    adapter = mcs_adapter.MCSAdapter(worker=lambda payload, timeout, deadline:
        mcs_worker._execute(dict(payload, timeout=timeout)))
    adapter._token = "synthetic-bearer"
    assert adapter.mark_patient_read(1, 123)["project"]["is_archived"] is False
    # the mark is a GET on the message list — no body, Bearer header only —
    # carrying the same unread/timestamp filter the fetch used
    mark = observed[0]
    assert mark.data is None
    assert mark.get_method() == "GET"
    assert mark.get_header("Authorization") == "Bearer synthetic-bearer"
    q = urllib.parse.parse_qs(urllib.parse.urlparse(mark.full_url).query)
    assert q["unread"] == ["1"] and q["timestamp"] == ["123"]

    class ErrorBody:
        def read(self, *args):
            raise AssertionError("error body must never be read")

        def close(self):
            pass

    def denied(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "synthetic", {}, ErrorBody())

    monkeypatch.setattr(mcs_util, "no_proxy_opener",
                        lambda *handlers: SimpleNamespace(open=denied))
    with pytest.raises(mcs_adapter.SessionExpired) as error:
        adapter._request("GET", "/projects", retries=0)
    assert error.value.status == 401

    # The explicit worker seam keeps retry policy in the parent adapter.
    statuses = iter([503, 200])
    ok_body = b'{"ok":true}'
    adapter._worker = lambda payload, **kwargs: {
        "status": next(statuses), "headers": {},
        "body": base64.b64encode(ok_body).decode("ascii")}
    monkeypatch.setattr(adapter, "_sleep_bounded", lambda seconds: None)
    assert adapter._request("GET", "/projects")[1] == ok_body


def test_attachment_worker_keeps_redirect_and_atomic_file_contract(tmp_path, monkeypatch):
    import io
    import urllib.error
    import mcs_worker
    urls = []
    authorizations = []

    class Opener:
        def open(self, request, timeout):
            urls.append(request.full_url)
            authorizations.append(request.get_header("Authorization"))
            if len(urls) == 1:
                raise urllib.error.HTTPError(request.full_url, 302, "synthetic", {
                    "location": "https://files.medical-care.net/signed"}, None)
            return io.BytesIO(b"synthetic-attachment")

    monkeypatch.setattr(mcs_adapter, "no_proxy_opener", lambda *handlers: Opener())
    destination = tmp_path / "file"
    destination.write_bytes(b"old")

    def worker(payload, timeout, deadline):
        result = mcs_worker._execute(dict(payload, timeout=timeout))
        assert destination.read_bytes() == b"old"
        assert Path(payload["partial"]).read_bytes() == b"synthetic-attachment"
        return result

    adapter = mcs_adapter.MCSAdapter(worker=worker)
    adapter._token = "synthetic-bearer"
    result = adapter.download("https://www.medical-care.net/f", str(destination))
    assert authorizations == ["Bearer synthetic-bearer", None]
    assert destination.read_bytes() == b"synthetic-attachment"
    assert result == {"bytes": len(b"synthetic-attachment"),
                      "sha256": hashlib.sha256(b"synthetic-attachment").hexdigest()}
    assert not Path(str(destination) + ".part").exists()

    monkeypatch.setattr(mcs_adapter, "_MAX_DOWNLOAD_BYTES", 2)
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter.download("https://www.medical-care.net/f", str(destination))
    assert error.value.kind == "download_too_large"
    assert not Path(str(destination) + ".part").exists()
    assert destination.read_bytes() == b"synthetic-attachment"
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter.download("https://outside.invalid/f", str(destination))
    assert error.value.kind == "url_not_allowed"


def test_bootstrap_and_keychain_use_remaining_budget(monkeypatch):
    observed = []

    def worker(payload, timeout, deadline):
        observed.append((payload["operation"], timeout))
        assert 0 < timeout <= 1
        if payload["operation"] == "cdp_json":
            return {"value": [{"type": "page", "url": "https://www.medical-care.net/",
                              "webSocketDebuggerUrl": "ws://127.0.0.1:9333/x"}]}
        return {"value": '"synthetic-token"'}

    adapter = mcs_adapter.MCSAdapter(worker=worker)
    adapter.set_deadline(time.monotonic() + 1)
    assert adapter.bootstrap_token() == "synthetic-token"
    assert [operation for operation, _ in observed] == ["cdp_json", "cdp_eval"]

    def keychain(command, **kwargs):
        assert 0 < kwargs["timeout"] <= 1
        return SimpleNamespace(returncode=0, stdout="synthetic-password", stderr="")

    monkeypatch.setattr(subprocess, "run", keychain)
    assert adapter._keychain_password() == "synthetic-password"
    adapter.set_deadline(time.monotonic() - 1)
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter.bootstrap_token()
    assert error.value.kind == "deadline_exceeded"
    assert len(observed) == 2


def test_keychain_timeout_preserves_fallback_only_with_run_budget(monkeypatch):
    adapter = mcs_adapter.MCSAdapter()

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    monkeypatch.setattr(mcs_adapter, "env_value", lambda key: "synthetic-fallback")
    adapter.set_deadline(time.monotonic() + 60)
    assert adapter._login_password() == ("synthetic-fallback", True)
    adapter.set_deadline(time.monotonic() - 1)
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter._login_password()
    assert error.value.kind == "deadline_exceeded"


# ---------- self_profile (MCS-derived self identity) ----------


def test_self_profile_normalizes_user_envelope():
    """GET /users/self -> sender id + display name + professions +
    stations — the signal engine's default self identity."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            assert path == "/users/self"
            return {"user": {
                "id": 42, "last_name": "山田", "first_name": "薬剤",
                "specialist_categories": [{"name": "薬剤師"},
                                          {"name": "管理薬剤師"}],
                "stations": [{"id": 7, "name": "みどり薬局"}]}}

    p = Adapter().self_profile()
    assert p == {"sender_id": 42, "name": "山田 薬剤",
                 "professions": ["薬剤師", "管理薬剤師"],
                 "organizations": ["みどり薬局"],
                 "stations": [{"id": 7, "name": "みどり薬局"}]}


def test_self_profile_accepts_bare_user_object():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"id": 7, "last_name": "佐藤", "first_name": "花子",
                    "stations": [{"name": "みどり薬局"}]}

    p = Adapter().self_profile()
    assert p["sender_id"] == 7 and p["name"] == "佐藤 花子"
    assert p["professions"] == [] and p["organizations"] == ["みどり薬局"]


def test_self_profile_endpoint_unavailable_maps_kind():
    """An endpoint failure is re-raised with a stable kind so the
    caller can log-and-continue instead of failing the run."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            raise mcs_adapter.MCSError("http_error", "404", status=404)

    import pytest
    with pytest.raises(mcs_adapter.MCSError) as e:
        Adapter().self_profile()
    assert e.value.kind == "self_profile_unavailable"


def test_self_profile_empty_is_schema_error():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"user": {"id": 9}}

    import pytest
    with pytest.raises(mcs_adapter.SchemaError):
        Adapter().self_profile()


def test_self_profile_session_expired_passthrough():
    """An expired session stays SessionExpired — it must never be
    relabeled as a missing/unsupported endpoint."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            raise mcs_adapter.SessionExpired("token rejected")

    import pytest
    with pytest.raises(mcs_adapter.SessionExpired):
        Adapter().self_profile()


def test_self_profile_normalizes_sender_id():
    """A non-scalar user id (unexpected shape) degrades to None —
    never poisons the artifact with a dict."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"user": {"id": {"unexpected": 1},
                             "last_name": "山田", "first_name": "太郎",
                             "specialist_categories":
                                 [{"name": "薬剤師"}],
                             "stations": [{"name": "みどり薬局"}]}}

    p = Adapter().self_profile()
    assert p["sender_id"] is None
    assert p["name"] == "山田 太郎"
    assert p["professions"] == ["薬剤師"]
    assert p["organizations"] == ["みどり薬局"]


# ---------- fetch_latest (newest-message probe endpoint) ----------


class _LatestAdapter(mcs_adapter.MCSAdapter):
    """Serves /projects/{pid}/messages/latest responses verbatim."""
    def __init__(self, response):
        self.response = response
        self.calls = []

    def _get(self, path, params=None, extend_session=True):
        self.calls.append((path, params))
        return self.response


def test_fetch_latest_parses_newest_message():
    adapter = _LatestAdapter({
        "is_self_only": True,
        "message": {"id": 42}})
    out = adapter.fetch_latest(7)
    assert out == {"message_id": 42, "is_self_only": True}
    path, params = adapter.calls[0]
    assert path == "/projects/7/messages/latest"
    assert params is None


def test_fetch_latest_no_newer_message():
    for response in ({"is_self_only": False, "message": None},
                     {"is_self_only": False, "message": {}},
                     {"is_self_only": False}):
        out = _LatestAdapter(response).fetch_latest(7)
        assert out == {"message_id": None, "is_self_only": False}


@pytest.mark.parametrize("response", [
    {"message": {"id": "abc"}},
    {"message": {"id": 0}},
    {"message": {"id": 2**63}},
    {"message": {"id": None}},
    {"message": "not-a-dict"},
    {"message": []},
    {"is_self_only": "yes", "message": None},
])
def test_fetch_latest_rejects_malformed(response):
    adapter = _LatestAdapter(response)
    with pytest.raises(mcs_adapter.SchemaError):
        adapter.fetch_latest(7)


def _status_adapter(monkeypatch, statuses):
    adapter = mcs_adapter.MCSAdapter(worker=lambda *a, **k: None)
    adapter._token = "synthetic-bearer"
    calls = []

    def worker(payload, **kwargs):
        path = urllib.parse.urlparse(payload["url"]).path
        calls.append(path)
        status = next(s for p, s in statuses.items() if path.endswith(p))
        return {"status": status, "headers": {},
                "body": base64.b64encode(b"{}").decode("ascii")}

    adapter._worker = worker
    monkeypatch.setattr(adapter, "_sleep_bounded", lambda seconds: None)
    return adapter, calls


def test_route_level_403_under_valid_session_is_not_expiry(monkeypatch):
    """A 403 while the session probe still succeeds (retired route,
    revoked project) is a per-request error — never a re-login trigger
    that aborts the whole run."""
    adapter, calls = _status_adapter(
        monkeypatch, {"/users/self/count": 200, "/projects/2/messages": 403})
    with pytest.raises(mcs_adapter.MCSError) as error:
        adapter._request("GET", "/projects/2/messages", retries=0)
    assert not isinstance(error.value, mcs_adapter.SessionExpired)
    assert error.value.kind == "forbidden" and error.value.status == 403
    assert calls[-1].endswith("/users/self/count")


def test_403_with_failing_session_probe_stays_expiry(monkeypatch):
    adapter, calls = _status_adapter(
        monkeypatch, {"/users/self/count": 403, "/projects/2/messages": 403})
    with pytest.raises(mcs_adapter.SessionExpired) as error:
        adapter._request("GET", "/projects/2/messages", retries=0)
    assert error.value.status == 403
    # the probe itself never recurses into another probe
    assert len(calls) == 2


def test_403_with_unreachable_session_probe_stays_expiry(monkeypatch):
    """A probe that itself fails must not replace the 403's expiry
    classification with its own network error."""
    adapter, calls = _status_adapter(
        monkeypatch, {"/users/self/count": 503, "/projects/2/messages": 403})
    with pytest.raises(mcs_adapter.SessionExpired):
        adapter._request("GET", "/projects/2/messages", retries=0)


# ---------- station_staffs (own pharmacy roster) ----------


def _staff_page(users, has_next):
    return {"paginate": {"current_page": 1, "per_page": 100,
                         "timestamp": 0, "total_entries": len(users),
                         "total_pages": 1, "has_next": has_next},
            "users": users}


def test_station_staffs_paginates_and_skips_station_accounts():
    calls = []
    pages = {
        (7, 1): _staff_page([
            {"id": 1, "last_name": "山田", "first_name": "花子",
             "specialist_categories": [{"name": "薬剤師"}],
             "is_self": True, "is_station_account": False},
            {"id": 2, "last_name": "みどり", "first_name": "薬局",
             "specialist_categories": [], "is_station_account": True}],
            True),
        (7, 2): _staff_page([
            {"id": 3, "last_name": "佐藤", "first_name": "一郎",
             "specialist_categories": [{"name": "事務"}]}], False),
    }

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            calls.append((path, params, extend_session))
            sid = int(path.split("/")[2])
            return pages[(sid, params["page"])]

    staff = Adapter().station_staffs([{"id": 7, "name": "みどり薬局"},
                                      {"id": "bad", "name": "x"}])
    assert staff == [
        {"staff_id": 1, "name": "山田 花子", "professions": ["薬剤師"],
         "station": "みどり薬局", "is_self": True},
        {"staff_id": 3, "name": "佐藤 一郎", "professions": ["事務"],
         "station": "みどり薬局", "is_self": False}]
    assert [c[0] for c in calls] == ["/stations/7/staffs"] * 2
    assert all(c[1]["per_page"] == 100 and c[2] is False for c in calls)


def test_station_staffs_failure_kinds():
    import pytest

    class Down(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            raise mcs_adapter.MCSError("http_error", "500", status=500)

    class Expired(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            raise mcs_adapter.SessionExpired("token rejected")

    class Bad(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"users": "nope"}

    st = [{"id": 7, "name": "みどり薬局"}]
    with pytest.raises(mcs_adapter.MCSError) as e:
        Down().station_staffs(st)
    assert e.value.kind == "station_staffs_unavailable"
    with pytest.raises(mcs_adapter.SessionExpired):
        Expired().station_staffs(st)
    with pytest.raises(mcs_adapter.SchemaError):
        Bad().station_staffs(st)


# ---- F-5: unread screen cap (80 rows) ----

def _raw_msg(mid):
    return {"id": mid, "comment": "synthetic", "user": {"id": 1},
            "created_at": "2026-09-19T00:00:00+09:00", "is_unread": True}


class _UnreadPagesAdapter(mcs_adapter.MCSAdapter):
    def __init__(self, pages, total=None):
        self.pages = pages          # list of message-count per page
        self.total = total

    def _get(self, path, params=None, extend_session=True):
        page = params["page"]
        n = self.pages[page - 1] if page <= len(self.pages) else 0
        base = (page - 1) * 100
        pag = {"has_next": page < len(self.pages)}
        if self.total is not None:
            pag["total_entries"] = self.total
        return {"messages": [_raw_msg(base + i + 1) for i in range(n)],
                "paginate": pag}


@pytest.mark.parametrize(("pages", "total", "capped"), [
    ([10] * 8, None, True),        # exactly the 80-row screen cap
    ([10] * 8, 80, True),          # cap with an honest total
    ([10, 2], 12, False),          # complete short walk
    ([10, 2], 30, True),           # server reports more than it listed
    ([3], None, False),
])
def test_unread_walk_flags_screen_cap(pages, total, capped):
    batch = _UnreadPagesAdapter(pages, total).fetch_unread_messages(1, 123)
    assert batch.reached and batch.error is None
    assert batch.capped is capped
    assert len(batch.messages) == sum(pages)


def test_unread_walk_page_cap_error_is_not_capped_flag():
    batch = _UnreadPagesAdapter([10] * 30).fetch_unread_messages(1, 123, max_pages=3)
    assert batch.error is not None and batch.error.kind == "pages_exceeded"
    assert batch.capped is False


@pytest.mark.parametrize(("detail", "expect"), [
    ({"project": {"is_archived": False, "oldest_unread_message": {"id": 7}}}, 7),
    ({"project": {"is_archived": False, "oldest_unread_message": None}}, None),
    ({"data": {"project": {"is_archived": True, "oldest_unread_message": {"id": 9}}}}, 9),
])
def test_oldest_unread_id_reads_project_detail(detail, expect):
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda path, params=None, **k: detail
    assert adapter.oldest_unread_id(1) == expect


@pytest.mark.parametrize("detail", [
    {}, {"project": {}}, {"project": {"is_archived": False,
                                       "oldest_unread_message": {"id": "x"}}},
])
def test_oldest_unread_id_rejects_malformed_detail(detail):
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda path, params=None, **k: detail
    with pytest.raises(mcs_adapter.SchemaError):
        adapter.oldest_unread_id(1)


# ---- F-5: fallback acknowledgement when the unread route rejects a capped project ----

def _mark_adapter(unread_kind, calls):
    adapter = mcs_adapter.MCSAdapter()

    def get(path, params=None, **k):
        calls.append((path, dict(params or {})))
        if path.endswith("/messages") and params and params.get("unread") == 1:
            raise mcs_adapter.MCSError(unread_kind, "GET messages", status=400)
        if path.endswith("/messages"):
            return {"messages": [], "paginate": {"has_next": False}}
        return {"project": {"is_archived": False, "oldest_unread_message": None}}
    adapter._get = get
    return adapter


def test_mark_read_fallback_uses_plain_list_only_when_allowed():
    calls = []
    adapter = _mark_adapter("http_error", calls)
    assert adapter.mark_patient_read(1, 123, fallback_plain=True)["project"]["is_archived"] is False
    plain = [p for path, p in calls if path.endswith("/messages") and "unread" not in p]
    assert plain == [{"per_page": 1, "page": 1, "include_paginate_totals": 0}]
    assert "keep_read_status" not in plain[0]        # the read must clear the flag


def test_mark_read_without_fallback_or_other_kinds_raises():
    calls = []
    with pytest.raises(mcs_adapter.MCSError) as e:
        _mark_adapter("http_error", calls).mark_patient_read(1, 123)
    assert e.value.kind == "http_error" and len(calls) == 1
    calls.clear()
    with pytest.raises(mcs_adapter.MCSError) as e:
        _mark_adapter("network_error", calls).mark_patient_read(1, 123, fallback_plain=True)
    assert e.value.kind == "network_error" and len(calls) == 1


# ---------- 連携サマリー: GET /kartes/{id}/memo_summary ----------

def _memo_adapter(memo_summary, calls=None):
    adapter = mcs_adapter.MCSAdapter()

    def _get(path, params=None, **k):
        if calls is not None:
            calls.append(path)
        return {"memo_summary": memo_summary}
    adapter._get = _get
    return adapter


def test_fetch_memo_summary_keeps_only_the_contract_fields():
    calls = []
    out = _memo_adapter({
        "is_editable": True, "is_read": False, "read_style": "multi_line",
        "comment": "合成サマリー", "updated_at": "2026-09-30T10:00:00+09:00",
        "user": {"profession": "看護師", "name": "合成 花子", "id": 9,
                 "email": "never@example.invalid"},
        "extra": {"dropped": True},
    }, calls).fetch_memo_summary(77)

    assert calls == ["/kartes/77/memo_summary"]
    assert out == {"comment": "合成サマリー",
                   "updated_at": "2026-09-30T10:00:00+09:00",
                   "is_editable": True,
                   "user": {"profession": "看護師", "name": "合成 花子"}}


def test_fetch_memo_summary_unregistered_form_is_none():
    assert _memo_adapter({"is_editable": True, "is_read": True,
                          "read_style": "single_line"}).fetch_memo_summary(1) is None


def test_fetch_memo_summary_truncates_oversize_comment():
    out = _memo_adapter({"is_editable": False, "is_read": True,
                         "read_style": "single_line",
                         "comment": "あ" * 1000}).fetch_memo_summary(1)
    assert len(out["comment"]) == mcs_adapter.KARTE_SUMMARY_MAX_CHARS
    assert out["updated_at"] == "" and out["user"] == {"profession": "",
                                                        "name": ""}


@pytest.mark.parametrize("memo", [
    None,                                                   # object missing
    {"is_read": True, "read_style": "single_line"},         # is_editable
    {"is_editable": "yes", "is_read": True, "read_style": "single_line"},
    {"is_editable": True, "is_read": 1, "read_style": "single_line"},
    {"is_editable": True, "is_read": True},                 # read_style
    {"is_editable": True, "is_read": True, "read_style": "single_line",
     "comment": 5},
    {"is_editable": True, "is_read": True, "read_style": "single_line",
     "comment": "x", "user": "nurse"},
    {"is_editable": True, "is_read": True, "read_style": "single_line",
     "comment": "x", "updated_at": 7},
])
def test_fetch_memo_summary_rejects_bad_shapes(memo):
    with pytest.raises(mcs_adapter.SchemaError):
        _memo_adapter(memo).fetch_memo_summary(1)


def test_project_row_without_karte_id_is_schema_error():
    row = _unread_project(11)
    del row["karte"]["id"]
    with pytest.raises(mcs_adapter.SchemaError, match="karte id"):
        _UnreadListAdapter([(100, False, [row])]).list_unread()
    row["karte"]["id"] = "11"
    with pytest.raises(mcs_adapter.SchemaError, match="karte id"):
        _UnreadListAdapter([(100, False, [row])]).list_unread()


def test_project_row_karte_id_lands_on_unread_patient():
    snap = _UnreadListAdapter([(100, False, [_unread_project(11)])]).list_unread()
    assert snap.patients[0].karte_id == 110
