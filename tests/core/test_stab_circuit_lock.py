"""Breaker state read-modify-write is serialized (flock), and the LLM
health marker the stall check reads is robust. Temp files only."""
import json
import threading

import mcs_util as util


def test_concurrent_failures_never_lose_a_count(tmp_path, monkeypatch):
    path = tmp_path / "llm_circuit.json"
    monkeypatch.setattr(util, "circuit_state_path", lambda _: path)
    monkeypatch.setattr(util, "_CIRCUIT_TRIP", 10_000)
    start = threading.Barrier(8)

    def worker():
        start.wait()
        for _ in range(25):
            util.circuit_failure(None)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert json.loads(path.read_text())["failures"] == 200


def test_health_marker_tracks_outage_and_recovery(tmp_path, monkeypatch):
    path = tmp_path / "llm_circuit.json"
    monkeypatch.setattr(util, "circuit_state_path", lambda _: path)
    assert util.llm_health(None) == {"last_success": None, "down_since": None}
    assert util.llm_down_for_s(None) is None
    util.circuit_success(None)
    first_ok = util.llm_last_success(None)
    assert first_ok is not None and util.llm_down_since(None) is None
    util.circuit_failure(None)
    since = util.llm_down_since(None)
    util.circuit_failure(None)
    assert util.llm_down_since(None) == since          # streak start kept
    assert util.llm_last_success(None) == first_ok     # history kept
    assert util.llm_down_for_s(None, now=since + 7200) == 7200
    util.circuit_success(None)
    assert util.llm_down_since(None) is None
    assert util.circuit_streak(None) == 0


def test_corrupt_health_marker_reads_unknown(tmp_path, monkeypatch):
    path = tmp_path / "llm_circuit.json"
    monkeypatch.setattr(util, "circuit_state_path", lambda _: path)
    (tmp_path / "llm_health.json").write_text(
        json.dumps({"down_since": "x", "last_success": float("inf")}))
    assert util.llm_health(None) == {"last_success": None, "down_since": None}
    util.circuit_failure(None)                         # still records
    assert util.llm_down_since(None) is not None
