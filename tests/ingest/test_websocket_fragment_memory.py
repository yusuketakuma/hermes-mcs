"""In-memory frame streams keep assembly memory bounded by payload size."""
import tracemalloc

import pytest

import mcs_adapter


def _conn(frames):
    reads = iter(frames)
    conn = mcs_adapter._WSConn.__new__(mcs_adapter._WSConn)

    def read_exact(length):
        expected, value = next(reads)
        assert length == expected
        return value

    conn._read_exact = read_exact
    conn._send_frame = lambda *args: None
    return conn


def _frames(count, payload):
    for index in range(count):
        opcode = 1 if index == 0 else 0
        final = 0x80 if index == count - 1 else 0
        yield 2, bytes([final | opcode, len(payload)])
        if payload:
            yield len(payload), payload


@pytest.mark.parametrize("payload", [b"", b"x"], ids=["empty", "one_byte"])
def test_many_small_fragments_do_not_retain_per_frame_objects(payload):
    count = 100_000
    conn = _conn(_frames(count, payload))
    tracemalloc.start()
    try:
        result = conn.recv_message()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == payload * count
    print(f"synthetic_fragment_payload_bytes={len(result)} peak_traced_bytes={peak}")
    # Allow assembly over-allocation + final bytes; no list/join buffers per frame.
    assert peak < len(result) * 4 + 128_000


def test_empty_start_with_control_frames_accepts_continuations():
    conn = _conn([
        (2, b"\x01\x00"),
        (2, b"\x89\x01"), (1, b"p"),
        (2, b"\x8a\x00"),
        (2, b"\x00\x00"),
        (2, b"\x80\x01"), (1, b"x"),
    ])
    sent = []
    conn._send_frame = lambda opcode, body: sent.append((opcode, body))
    assert conn.recv_message() == b"x"
    assert sent == [(0xA, b"p")]


@pytest.mark.parametrize("headers", [
    [b"\x80\x00"],  # A continuation cannot start a message.
    [b"\x01\x00", b"\x81\x00"],  # An empty start still occupies a message.
    [b"\x02\x00", b"\x82\x00"],
])
def test_fragment_start_protocol_errors_do_not_depend_on_payload_length(headers):
    conn = _conn([(2, header) for header in headers])
    with pytest.raises(mcs_adapter.BootstrapError, match="cdp_ws_protocol"):
        conn.recv_message()


@pytest.mark.parametrize("opcode", [1, 2])
def test_complete_empty_data_frame_remains_valid(opcode):
    assert _conn([(2, bytes([0x80 | opcode, 0]))]).recv_message() == b""
