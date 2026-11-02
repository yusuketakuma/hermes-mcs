"""Synthetic lane-3 patient-context tests: no real LLM, host probes or production DB."""
import writer_lock
import os
import sys
from types import SimpleNamespace

import pytest

import extract_llm as worker
import cloud_inference
import patient_context_refresh
from extract_testkit import _hash, _ledger, _message, _v2_artifact
from semantic_testkit import _canonical_cfg, _drain, _PassJev, _seeded_two

LANES = worker.local_llm.LANES


@pytest.fixture
def capacity(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    cfg = {"extract": {"patient_context_nightly": {"enabled": True}}}
    monkeypatch.setattr(worker, "_SLOT_OVERRIDE", LANES[3])
    monkeypatch.setattr(worker, "load_config", lambda: cfg)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 123))
    monkeypatch.setattr(worker, "circuit_open_s", lambda led: 0)
    monkeypatch.setattr(worker, "disk_free_mb", lambda led: 100000)
    monkeypatch.setattr(worker, "_context_memory_available", lambda: True)
    calls = []

    def run_due(led, config, *, deadline, admit, complete):
        assert led is ledger and config == cfg
        assert deadline > worker.time.monotonic()
        assert worker.local_llm.request_slot() == LANES[3]
        assert admit()
        calls.append((admit, complete))
        return {"status": "done", "done": 1}

    monkeypatch.setitem(sys.modules, "patient_context_refresh", SimpleNamespace(run_due=run_due, options=patient_context_refresh.options))
    yield ledger, cfg, calls
    ledger.close()


def run(capacity, stop=None, state=(False, 123)):
    return worker._background_context_refresh(capacity[0], state, stop)


@pytest.mark.parametrize("slot", [LANES[1], LANES[2], None])
def test_other_lanes_never_enter_engine(capacity, monkeypatch, slot):
    monkeypatch.setattr(worker, "_SLOT_OVERRIDE", slot)
    assert run(capacity)["status"] == "off"
    assert not capacity[2]


def test_disabled_has_no_probes(capacity, monkeypatch):
    capacity[1]["extract"]["patient_context_nightly"]["enabled"] = False
    for probe in ("_context_memory_available", "disk_free_mb", "circuit_open_s"):
        monkeypatch.setattr(worker, probe, lambda *a: pytest.fail("probe while off"))
    assert run(capacity)["status"] == "off"
    assert not capacity[2]


@pytest.mark.parametrize("gate", ["expired", "memory", "disk", "unknown_disk", "circuit", "update", "mid_update"])
def test_capacity_gates(capacity, monkeypatch, gate):
    stop, state = None, (False, 123)
    if gate == "expired":
        stop = worker.time.monotonic() - 1
    elif gate == "memory":
        monkeypatch.setattr(worker, "_context_memory_available", lambda: False)
    elif gate in ("disk", "unknown_disk"):
        monkeypatch.setattr(worker, "disk_free_mb", lambda led: 0 if gate == "disk" else None)
    elif gate == "circuit":
        monkeypatch.setattr(worker, "circuit_open_s", lambda led: 60)
    elif gate == "update":
        monkeypatch.setattr(writer_lock, "updater_state", lambda: (True, 124))
    elif gate == "mid_update":
        state = (True, 123)
        monkeypatch.setattr(writer_lock, "updater_state", lambda: state)
    assert run(capacity, stop, state)["status"] == "deferred"
    assert not capacity[2]


def test_engine_gets_extraction_complete_rule(capacity):
    ledger, _, calls = capacity
    ledger.save_messages([_message(mid=1, project_id=1), _message(mid=2, project_id=2)])
    _v2_artifact(ledger, 1, _hash(ledger, 1), project_id=1)
    assert run(capacity)["done"] == 1
    admit, complete = calls[0]
    assert admit() and callable(complete)
    assert complete(1) is True    # every post extracted under EXTRACT_VERSION
    assert complete(2) is False   # one eligible post still pending


@pytest.mark.parametrize("scope_changed", [False, True])
@pytest.mark.parametrize("semantic_present", [False, True])
def test_context_result_survives_budget_window_edit_only(
        capacity, monkeypatch, scope_changed, semantic_present):
    _, cfg, _ = capacity
    cfg["cloud_llm"] = {"mode": "on", "project_ids": [1]}
    if semantic_present:
        cfg["semantic"] = {"mode": "assist", "until": None}
    current = [cfg]
    monkeypatch.setattr(worker, "load_config", lambda: current[0])
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: current[0])

    def run_due(ledger, config, *, admit, **kwargs):
        assert admit()
        current[0] = {**cfg, "cloud_llm": {**cfg["cloud_llm"],
            "window_budget_usd": 10, "window_start": "2026-10-28T00:00:00+09:00",
            "until": "2026-10-30T23:59:59+09:00"},
            "semantic": {**cfg.get("semantic", {}), "until": "2026-10-30T23:59:59+09:00"}}
        if scope_changed:
            current[0]["cloud_llm"]["project_ids"] = [2]
        return {"status": "done" if admit() else "source_changed", "done": int(admit())}

    monkeypatch.setitem(sys.modules, "patient_context_refresh",
        SimpleNamespace(run_due=run_due, options=patient_context_refresh.options))
    assert run(capacity)["done"] == int(not scope_changed)


@pytest.mark.parametrize("stale_field,stale_value", [
    ("hash", "outdated-source-hash"), ("invalidated", 1),
])
def test_canonical_pass_completes_patient_without_legacy_admission(
        tmp_path, stale_field, stale_value):
    ledger = _seeded_two(tmp_path)
    cfg = _canonical_cfg()
    try:
        assert worker.legacy_admissions(ledger, cfg) == set()
        assert worker.patient_extraction_complete(ledger, 1) is False

        # The normal canonical pipeline publishes the current v4 read model;
        # an empty legacy admission set does not mean extraction is complete.
        result = _drain(ledger, jev=_PassJev())
        assert result["done"] == 1 and result["failed"] == 0
        assert worker.legacy_admissions(ledger, cfg) == set()
        assert ledger.db.execute(
            "SELECT 1 FROM artifacts WHERE kind='extract_llm'").fetchone() is None
        assert worker.patient_extraction_complete(ledger, 1) is True

        # A stale projection of even one reply must hold the whole patient
        # again; canonical admission cannot bypass the source/publication gate.
        with ledger.db:
            changed = ledger.db.execute(
                "UPDATE artifacts SET meta=json_set(meta,?,?) "
                "WHERE kind='semantic_facts_v4' AND message_id=2",
                (f"$.{stale_field}", stale_value))
        assert changed.rowcount == 1
        assert worker.patient_extraction_complete(ledger, 1) is False
    finally:
        ledger.close()


def test_failure_isolated_and_no_sensitive_error(capacity, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private text")
    monkeypatch.setitem(sys.modules, "patient_context_refresh", SimpleNamespace(run_due=fail, options=patient_context_refresh.options))
    assert run(capacity) == {"status": "error", "done": 0, "error": "RuntimeError"}


@pytest.mark.parametrize("percent,expected", [(9, False), (10, True), (50, True), (101, False)])
def test_macos_memory(monkeypatch, percent, expected):
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    def probe(argv, **kwargs):
        assert argv == ["/usr/bin/memory_pressure", "-Q"]
        assert kwargs["timeout"] == 2
        return SimpleNamespace(stdout=f"System-wide memory free percentage: {percent}%")
    monkeypatch.setattr(worker.subprocess, "run", probe)
    assert worker._context_memory_available() is expected


@pytest.mark.parametrize("text,expected", [
    ("MemAvailable: 2097152 kB\nMemTotal: 8388608 kB\n", True),
    ("MemAvailable: 1024 kB\nMemTotal: 8388608 kB\n", False),
    ("MemTotal: 8388608 kB\n", False),
])
def test_linux_memory(monkeypatch, text, expected):
    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(worker.Path, "read_text", lambda self: text)
    assert worker._context_memory_available() is expected


@pytest.mark.parametrize("idle", [True, False])
@pytest.mark.parametrize("lane,context_refresh_first", [("3", True), ("2", False)])
def test_resident_integration(monkeypatch, idle, lane, context_refresh_first):
    now, events = [0.0], []
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 123))
    monkeypatch.setenv("MCS_LLM_SLOT", "1")
    monkeypatch.setenv(worker.local_llm.TURN_LOCK_ENV, "/synthetic/turn.lock")
    previous_slot = worker._SLOT_OVERRIDE
    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(worker, "load_config", lambda: {})
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    monkeypatch.setattr(worker, "Ledger", lambda *args: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(worker.sys, "argv", ["extract_llm", "--all", "--lane", lane, "--stop-after", "1"])

    def pending(*args, **kwargs):
        events.append("run_pending")
        assert kwargs["carry"] == []
        return {"done": 0 if idle else 1, "failed": 0, "left": 0 if idle else 1,
                "selected": 0 if idle else 1, "carried": 0, "carry": []}
    monkeypatch.setattr(worker, "run_pending", pending)

    def context_refresh(led, state, stop):
        events.append("context_refresh")
        assert state == (False, 123) and stop is not None
        assert os.environ["MCS_LLM_SLOT"] == str(worker.local_llm.lane_slot(int(lane)))
        assert os.environ[worker.local_llm.TURN_LOCK_ENV].endswith("data/llm_turn.lock")
        return {"status": "deferred", "done": 0}
    monkeypatch.setattr(worker, "_background_context_refresh", context_refresh)
    assert worker.main() == 0
    assert events[0] == ("context_refresh" if context_refresh_first else "run_pending")
    assert ("context_refresh" in events) is context_refresh_first
    assert os.environ["MCS_LLM_SLOT"] == "1"
    assert os.environ[worker.local_llm.TURN_LOCK_ENV] == "/synthetic/turn.lock"
    assert worker._SLOT_OVERRIDE == previous_slot


def test_resident_exits_when_context_refresh_sees_update(monkeypatch):
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 123))
    monkeypatch.delenv("MCS_LLM_SLOT", raising=False)
    monkeypatch.delenv(worker.local_llm.TURN_LOCK_ENV, raising=False)
    monkeypatch.setattr(worker, "load_config", lambda: {})
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    monkeypatch.setattr(worker, "Ledger", lambda *args: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(worker.sys, "argv", ["extract_llm", "--all", "--lane", "3", "--stop-after", "1"])
    monkeypatch.setattr(worker, "run_pending", lambda *a, **k: pytest.fail("extraction after update"))
    monkeypatch.setattr(worker, "_background_context_refresh", lambda *a: {
        "status": "deferred", "done": 0, "run_lock_lost": "updated"})
    assert worker.main() == 0
    assert "MCS_LLM_SLOT" not in os.environ
    assert worker.local_llm.TURN_LOCK_ENV not in os.environ


def test_config_failure_isolated(capacity, monkeypatch):
    monkeypatch.setattr(worker, "load_config", lambda: (_ for _ in ()).throw(ValueError("private")))
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: (_ for _ in ()).throw(ValueError("private")))
    assert run(capacity) == {"status": "error", "done": 0, "error": "ValueError"}


def test_memory_probe_failure_defers(monkeypatch):
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    def fail(*args, **kwargs):
        raise worker.subprocess.TimeoutExpired("memory_pressure", 2)
    monkeypatch.setattr(worker.subprocess, "run", fail)
    assert not worker._context_memory_available()


def test_update_during_engine_signals_resident_exit(capacity, monkeypatch):
    def engine(led, cfg, *, deadline, admit, complete):
        monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 124))
        assert not admit()
        return {"status": "deferred", "done": 0}
    monkeypatch.setitem(sys.modules, "patient_context_refresh", SimpleNamespace(run_due=engine, options=patient_context_refresh.options))
    assert run(capacity)["run_lock_lost"] == "updated"


def test_configuration_change_cancels_publication(capacity, monkeypatch):
    assert run(capacity)["done"] == 1
    monkeypatch.setattr(worker, "load_config", lambda: {
        "extract": {"patient_context_nightly": {"enabled": False}}})
    assert not capacity[2][0][0]()


def test_omitted_options_enable_idle_worker(capacity):
    capacity[1].clear()
    assert run(capacity)["done"] == 1
    assert len(capacity[2]) == 1


@pytest.mark.parametrize("value", [None, [], {"enabled": "true"}, {"unknown": True}])
def test_malformed_options_fail_closed(capacity, value):
    capacity[1]["extract"]["patient_context_nightly"] = value
    assert run(capacity)["status"] == "off"
    assert not capacity[2]
