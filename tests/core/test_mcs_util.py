"""Synthetic regressions for shared filesystem and text utilities."""
import json
import math

import pytest

import mcs_util as util


def test_atomic_write_basename_and_failure_preserve_destination(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "result.json"
    path.write_text("old")

    def fail(handle):
        handle.write("partial")
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError):
        util.atomic_write("result.json", fail)
    assert path.read_text() == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["result.json"]
    util.atomic_write("result.json", lambda f: f.write("new"), mode=0o600)
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
    monkeypatch.setattr(util, "circuit_state_path", lambda _: path)
    assert math.isfinite(util.circuit_open_s(None))
    util.circuit_failure(None)
    saved = json.loads(path.read_text())
    assert type(saved["failures"]) is int
    assert 0 <= saved["streak"] <= 4
    assert math.isfinite(util.circuit_open_s(None))


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_invalid_disk_floor_retains_default_guard(monkeypatch, value):
    monkeypatch.setenv("MCS_DISK_GUARD_MB", value)
    assert util.disk_floor_mb() == 512.0


def test_whitespace_only_quote_has_no_evidence_span():
    assert util.locate_quote_span(" \n", "\t") is None


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
def test_invalid_chunk_size_is_rejected(size):
    with pytest.raises(ValueError, match="chunk_size"):
        util.text_chunks("synthetic", size)
