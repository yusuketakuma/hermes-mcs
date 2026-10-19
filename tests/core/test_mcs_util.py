"""Synthetic regressions for shared filesystem and text utilities."""
import llm_availability
import safe_io
import json
import math

import pytest
import mcs_util as util


def test_sentence_boundary_keeps_complete_quote_in_prior_chunk():
    assert util.text_chunks("前文。次文。末尾", 3) == ["前文。", "次文。", "末尾"]


def test_long_line_keeps_boundaries_without_repeated_suffix_copies(monkeypatch):
    copied = []

    class Segment(str):
        def __getitem__(self, key):
            result = super().__getitem__(key)
            if isinstance(key, slice):
                copied.append(len(result))
                return Segment(result)
            return result

    split = util.re.split
    monkeypatch.setattr(util.re, "split",
                        lambda *args: [Segment(s) for s in split(*args)])
    text = "x" * 100_007 + "。" + "y" * 19 + "。tail"
    chunks = util.text_chunks(text, 1000)
    assert chunks == ["x" * 1000] * 100 + ["x" * 7 + "。" + "y" * 19 + "。tail"]
    assert "".join(chunks) == text
    assert sum(copied) <= 2 * len(text), "repeated suffix copies exceed linear budget"


def test_atomic_write_basename_and_failure_preserve_destination(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "result.json"
    path.write_text("old")

    def fail(handle):
        handle.write("partial")
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError):
        safe_io.atomic_write("result.json", fail)
    assert path.read_text() == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["result.json"]
    safe_io.atomic_write("result.json", lambda f: f.write("new"), mode=0o600)
    assert path.read_text() == "new"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("state", [
    {"failures": "bad", "streak": []},
    {"failures": 2, "streak": 10**100},
    {"open_until": 10**1000},
    {"open_until": float("inf")},
])
def test_corrupt_circuit_state_recovers_without_unbounded_cooldown(
        tmp_path, monkeypatch, state):
    path = tmp_path / "circuit.json"
    path.write_text(json.dumps(state))
    monkeypatch.setattr(llm_availability, 'circuit_state_path', lambda _: path)
    assert math.isfinite(llm_availability.circuit_open_s(None))
    llm_availability.circuit_failure(None)
    saved = json.loads(path.read_text())
    assert type(saved["failures"]) is int
    assert 0 <= saved["streak"] <= 4
    assert math.isfinite(llm_availability.circuit_open_s(None))


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_invalid_disk_floor_retains_default_guard(monkeypatch, value):
    monkeypatch.setenv("MCS_DISK_GUARD_MB", value)
    assert llm_availability.disk_floor_mb() == 512.0


def test_whitespace_only_quote_has_no_evidence_span():
    assert util.locate_quote_span(" \n", "\t") is None


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
def test_invalid_chunk_size_is_rejected(size):
    with pytest.raises(ValueError, match="chunk_size"):
        util.text_chunks("synthetic", size)


def test_deep_json_is_not_a_stored_object(recursion_limited_json):
    deep = '{"nested":' + '[' * 10000 + '0' + ']' * 10000 + '}'
    assert util.loads_dict(deep) is None
    assert util.json_object(deep) is None


@pytest.mark.parametrize("raw", [b"\xff", b"[" * 10000 + b"]" * 10000],
                         ids=["invalid_utf8", "deep_json"])
def test_corrupt_circuit_bytes_do_not_stop_lane(tmp_path, monkeypatch, raw):
    path = tmp_path / "circuit.json"
    path.write_bytes(raw)
    monkeypatch.setattr(llm_availability, 'circuit_state_path', lambda _: path)
    assert llm_availability.circuit_open_s(None) == 0
    llm_availability.circuit_failure(None)
    assert json.loads(path.read_text())["failures"] == 1
