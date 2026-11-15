"""Breaker state read-modify-write is serialized (flock), and the LLM
health marker the stall check reads is robust. Temp files only."""
import llm_availability
import json
import threading



def test_concurrent_failures_never_lose_a_count(tmp_path, monkeypatch):
    path = tmp_path / "llm_circuit.json"
    monkeypatch.setattr(llm_availability, 'circuit_state_path', lambda _: path)
    monkeypatch.setattr(llm_availability, '_CIRCUIT_TRIP', 10_000)
    start = threading.Barrier(8)

    def worker():
        start.wait()
        for _ in range(25):
            llm_availability.circuit_failure(None)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert json.loads(path.read_text())["failures"] == 200


def test_health_marker_tracks_outage_and_recovery(tmp_path, monkeypatch):
    path = tmp_path / "llm_circuit.json"
    monkeypatch.setattr(llm_availability, 'circuit_state_path', lambda _: path)
    assert llm_availability.llm_health(None) == {"last_success": None, "down_since": None}
    assert llm_availability.llm_down_for_s(None) is None
    llm_availability.circuit_success(None)
    first_ok = llm_availability.llm_last_success(None)
    assert first_ok is not None and llm_availability.llm_down_since(None) is None
    llm_availability.circuit_failure(None)
    since = llm_availability.llm_down_since(None)
    llm_availability.circuit_failure(None)
    assert llm_availability.llm_down_since(None) == since          # streak start kept
    assert llm_availability.llm_last_success(None) == first_ok     # history kept
    assert llm_availability.llm_down_for_s(None, now=since + 7200) == 7200
    llm_availability.circuit_success(None)
    assert llm_availability.llm_down_since(None) is None
    assert llm_availability.circuit_streak(None) == 0


def test_corrupt_health_marker_reads_unknown(tmp_path, monkeypatch):
    path = tmp_path / "llm_circuit.json"
    monkeypatch.setattr(llm_availability, 'circuit_state_path', lambda _: path)
    (tmp_path / "llm_health.json").write_text(
        json.dumps({"down_since": "x", "last_success": float("inf")}))
    assert llm_availability.llm_health(None) == {"last_success": None, "down_since": None}
    llm_availability.circuit_failure(None)                         # still records
    assert llm_availability.llm_down_since(None) is not None
