"""Synthetic idle-capacity tests: no real LLM, host probes or production DB."""
import os
import sys
from types import SimpleNamespace

import pytest

import extract_llm as worker
import mcs_util
import patient_nightly
from extract_testkit import _ledger


@pytest.fixture
def capacity(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    cfg = {"extract": {"patient_context_nightly": {"enabled": True}}}
    monkeypatch.setattr(worker, "_SLOT_OVERRIDE", 2)
    monkeypatch.setattr(worker, "load_config", lambda: cfg)
    monkeypatch.setattr(mcs_util, "_updater_state", lambda: (False, 123))
    monkeypatch.setattr(worker, "circuit_open_s", lambda led: 0)
    monkeypatch.setattr(worker, "disk_free_mb", lambda led: 100000)
    monkeypatch.setattr(worker, "_nightly_memory_available", lambda: True)
    monkeypatch.setattr(worker, "_slots_busy", lambda deadline: {1: False, 2: False})
    monkeypatch.setattr(worker, "legacy_admissions", lambda led, cfg: None)
    selections = []
    def select(led, limit, admitted_ids, shard, oldest_first):
        assert led is ledger and limit == 1
        assert admitted_ids is None and shard is None and oldest_first is False
        return selections, 0
    monkeypatch.setattr(worker, "_select_pending", select)
    calls = []

    def run_due(led, config, *, deadline, admit):
        assert led is ledger and config == cfg
        assert deadline > worker.time.monotonic()
        assert worker.local_llm.request_slot() == 2
        assert admit()
        calls.append(admit)
        return {"status": "done", "done": 1}

    monkeypatch.setitem(sys.modules, "patient_nightly", SimpleNamespace(run_due=run_due, options=patient_nightly.options, night_window=lambda now: ("synthetic-night", now + 900)))
    yield ledger, cfg, selections, calls
    ledger.close()


def run(capacity, held=lambda: False, stop=None):
    return worker._background_nightly(capacity[0], held, (False, 123), stop)


def test_idle_only_and_rechecks_new_arrival(capacity):
    assert run(capacity)["done"] == 1
    capacity[2].append({"message_id": 1})
    assert not capacity[3][0]()
    assert run(capacity)["status"] == "deferred"


@pytest.mark.parametrize("slot", [0, 1, None])
def test_other_slots_never_enter_engine(capacity, monkeypatch, slot):
    monkeypatch.setattr(worker, "_SLOT_OVERRIDE", slot)
    assert run(capacity)["status"] == "off"
    assert not capacity[3]


def test_disabled_has_no_probes(capacity, monkeypatch):
    capacity[1]["extract"]["patient_context_nightly"]["enabled"] = False
    monkeypatch.setattr(worker, "_slots_busy", lambda deadline: pytest.fail("probe while off"))
    assert run(capacity)["status"] == "off"


@pytest.mark.parametrize("gate", ["held", "expired", "memory", "disk", "unknown_disk", "circuit", "slot", "probe", "update"])
def test_capacity_gates(capacity, monkeypatch, gate):
    held, stop = lambda: False, None
    if gate == "held":
        def held():
            return True
    elif gate == "expired":
        stop = worker.time.monotonic() - 1
    elif gate == "memory":
        monkeypatch.setattr(worker, "_nightly_memory_available", lambda: False)
    elif gate in ("disk", "unknown_disk"):
        monkeypatch.setattr(worker, "disk_free_mb", lambda led: 0 if gate == "disk" else None)
    elif gate == "circuit":
        monkeypatch.setattr(worker, "circuit_open_s", lambda led: 60)
    elif gate in ("slot", "probe"):
        monkeypatch.setattr(worker, "_slots_busy", lambda deadline: {1: False, 2: True} if gate == "slot" else None)
    elif gate == "update":
        monkeypatch.setattr(mcs_util, "_updater_state", lambda: (True, 124))
    assert run(capacity, held, stop)["done"] == 0
    assert not capacity[3]


@pytest.mark.parametrize("kind", ["semantic", "extract_qc"])
@pytest.mark.parametrize("delay", [-1, 0, 60])
def test_only_due_jobs_block_nightly(capacity, monkeypatch, kind, delay):
    now = [1000.0]
    monkeypatch.setattr(worker.time, "time", lambda: now[0])
    capacity[0].db.execute(
        "INSERT INTO fetch_jobs(kind,state,next_try,created_at,updated_at) VALUES(?,'pending',?,0,0)",
        (kind, now[0] + delay))
    result = run(capacity)
    if delay <= 0:
        assert result["status"] == "deferred"
        assert not capacity[3]
    else:
        assert result["done"] == 1
        now[0] += delay
        # A retry becoming due during inference also prevents publication.
        assert not capacity[3][0]()


def test_failure_isolated_and_no_sensitive_error(capacity, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private text")
    monkeypatch.setitem(sys.modules, "patient_nightly", SimpleNamespace(run_due=fail, options=patient_nightly.options, night_window=lambda now: ("synthetic-night", now + 900)))
    assert run(capacity) == {"status": "error", "done": 0, "error": "RuntimeError"}


@pytest.mark.parametrize("percent,expected", [(9, False), (10, True), (50, True), (101, False)])
def test_macos_memory(monkeypatch, percent, expected):
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    def probe(argv, **kwargs):
        assert argv == ["/usr/bin/memory_pressure", "-Q"]
        assert kwargs["timeout"] == 2
        return SimpleNamespace(stdout=f"System-wide memory free percentage: {percent}%")
    monkeypatch.setattr(worker.subprocess, "run", probe)
    assert worker._nightly_memory_available() is expected


@pytest.mark.parametrize("text,expected", [
    ("MemAvailable: 2097152 kB\nMemTotal: 8388608 kB\n", True),
    ("MemAvailable: 1024 kB\nMemTotal: 8388608 kB\n", False),
    ("MemTotal: 8388608 kB\n", False),
])
def test_linux_memory(monkeypatch, text, expected):
    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(worker.Path, "read_text", lambda self: text)
    assert worker._nightly_memory_available() is expected


@pytest.mark.parametrize("idle,slot,expected", [(True, 2, 1), (False, 2, 0), (True, 0, 0)])
def test_resident_integration(monkeypatch, idle, slot, expected):
    now, calls = [0.0], []
    monkeypatch.setattr(mcs_util, "_updater_state", lambda: (False, 123))
    monkeypatch.setenv("MCS_LLM_SLOT", "1")
    previous_slot = worker._SLOT_OVERRIDE
    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(worker, "_slots_busy", lambda deadline: {1: False, 2: False})
    monkeypatch.setattr(worker, "load_config", lambda: {})
    monkeypatch.setattr(worker, "Ledger", lambda *args: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(worker.sys, "argv", ["extract_llm", "--all", "--slot", str(slot), "--stop-after", "1"])
    monkeypatch.setattr(worker, "run_pending", lambda *args, **kwargs: {
        "done": 0 if idle else 1, "failed": 0, "left": 0 if idle else 1,
        "selected": 0 if idle else 1})
    def nightly(*args):
        calls.append(args)
        return {"status": "deferred", "done": 0}
    monkeypatch.setattr(worker, "_background_nightly", nightly)
    assert worker.main() == 0
    assert len(calls) == expected
    assert os.environ["MCS_LLM_SLOT"] == "1"
    assert worker._SLOT_OVERRIDE == previous_slot


def test_config_failure_isolated(capacity, monkeypatch):
    monkeypatch.setattr(worker, "load_config", lambda: (_ for _ in ()).throw(ValueError("private")))
    assert run(capacity) == {"status": "error", "done": 0, "error": "ValueError"}


def test_memory_probe_failure_defers(monkeypatch):
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    def fail(*args, **kwargs):
        raise worker.subprocess.TimeoutExpired("memory_pressure", 2)
    monkeypatch.setattr(worker.subprocess, "run", fail)
    assert not worker._nightly_memory_available()


def test_update_during_engine_signals_resident_exit(capacity, monkeypatch):
    def engine(led, cfg, *, deadline, admit):
        monkeypatch.setattr(mcs_util, "_updater_state", lambda: (False, 124))
        assert not admit()
        return {"status": "deferred", "done": 0}
    monkeypatch.setitem(sys.modules, "patient_nightly", SimpleNamespace(run_due=engine, options=patient_nightly.options, night_window=lambda now: ("synthetic-night", now + 900)))
    assert run(capacity)["run_lock_lost"] == "updated"


def test_configuration_change_cancels_publication(capacity, monkeypatch):
    assert run(capacity)["done"] == 1
    monkeypatch.setattr(worker, "load_config", lambda: {
        "extract": {"patient_context_nightly": {"enabled": False}}})
    assert not capacity[3][0]()


def test_omitted_options_enable_idle_worker(capacity):
    capacity[1].clear()
    assert run(capacity)["done"] == 1
    assert len(capacity[3]) == 1


@pytest.mark.parametrize("value", [None, [], {"enabled": "true"}, {"unknown": True}])
def test_malformed_options_fail_closed(capacity, value):
    capacity[1]["extract"]["patient_context_nightly"] = value
    assert run(capacity)["status"] == "off"
    assert not capacity[3]


def test_daytime_skips_all_capacity_probes(capacity, monkeypatch):
    monkeypatch.setattr(sys.modules["patient_nightly"], "night_window", lambda now: None)
    monkeypatch.setattr(worker, "_nightly_memory_available", lambda: pytest.fail("daytime memory probe"))
    monkeypatch.setattr(worker, "_slots_busy", lambda deadline: pytest.fail("daytime slot probe"))
    monkeypatch.setattr(worker, "_select_pending", lambda *args: pytest.fail("daytime backlog probe"))
    assert run(capacity) == {"status": "off", "done": 0}
    assert not capacity[3]
