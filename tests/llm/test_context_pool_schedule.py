"""The context child reaches the real context engine without occupying a local turn."""
from types import SimpleNamespace
import json

import pytest

import cloud_backlog
import cloud_inference
import cloud_policy
import extract_llm
import extract_llm_provider
import extract_llm_resident as resident
import extract_llm_source_contract
import local_llm
import mcs_util
import patient_context_refresh
import writer_lock
from test_context_cloud_update_identity import cloud, real_cloud, runtime
from test_patient_context_refresh import document

__all__ = ["cloud", "real_cloud", "runtime"]
_REAL_CLOUD_MODE = extract_llm_provider._cloud_mode


@pytest.mark.parametrize("cloud_ready", [True, False])
def test_context_child_finishes_real_guarded_pipeline(runtime, real_cloud, monkeypatch, tmp_path, cloud_ready):
    ledger, clock, local_calls = runtime
    cfg, posts = real_cloud
    if not cloud_ready:
        monkeypatch.setattr(cloud_policy, "api_key", lambda *_: None)
        # The cloud transport fixture pins mode=on for its paid tests. Use the
        # real key-aware selector to exercise the child caller's local route.
        monkeypatch.setattr(extract_llm, "_cloud_mode", _REAL_CLOUD_MODE)
        monkeypatch.setattr(extract_llm_provider, "_cloud_mode", _REAL_CLOUD_MODE)
    events = []
    monkeypatch.setattr(mcs_util, "load_config", lambda: cfg)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    monkeypatch.setattr(resident, "disk_free_mb", lambda _: 100000)
    monkeypatch.setattr(resident, "disk_floor_mb", lambda: 1)
    monkeypatch.setattr(resident, "_context_memory_available", lambda: True)
    monkeypatch.setattr(resident, "circuit_open_s", lambda _: 0)
    monkeypatch.setattr(cloud_backlog, "_event", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(cloud_backlog, "owner_alive", lambda: not document(ledger))

    def sleep(seconds):
        assert len(events) < 40, events
        clock.mono += seconds

    fake_time = SimpleNamespace(monotonic=lambda: clock.mono, sleep=sleep)
    monkeypatch.setattr(resident, "time", fake_time)
    monkeypatch.setattr(cloud_backlog, "time", fake_time)
    initial = writer_lock.updater_state()
    # Normal lane 3 does not call the cloud engine or wait for its requests.
    monkeypatch.setattr(resident.extract_llm_call_context, "_SLOT_OVERRIDE", local_llm.LANES[3])
    assert resident._background_context_refresh(ledger, initial) == {"status": "off", "done": 0}
    assert not posts
    cloud_backlog._context_worker(ledger, initial, 0)
    assert document(ledger) is not None
    assert bool(posts) is cloud_ready and bool(local_calls) is not cloud_ready
    assert len(ledger.artifacts("cloud_usage_reserve")) == len(posts)
    assert len(ledger.artifacts("cloud_usage_result")) == len(posts)
    assert all(name == "patient_context_pass" for name, _ in events)


def test_context_child_does_not_enter_after_source_generation_changes(runtime, real_cloud, monkeypatch):
    ledger, _, _ = runtime
    _, posts = real_cloud
    initial = writer_lock.updater_state()
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (True, initial[1]))
    cloud_backlog._context_worker(ledger, initial, 0)
    assert not posts and not document(ledger)


def test_context_pass_reports_only_fixed_bounded_diagnostics():
    result = {"status": "deferred", "done": 0, "reason": "patient_context_waiting",
              "scanned": 8, "scan_ms": 900000, "lane": "B", "project_id": 123,
              "error": "synthetic private source", "patient": "synthetic private patient"}
    assert cloud_backlog._context_pass_observation(result) == {
        "status": "deferred", "done": 0, "reason": "patient_context_waiting",
        "scanned": 8, "scan_ms": 900000, "lane": "B"}
    assert cloud_backlog._context_pass_observation({"status": "complete", "done": 0}) == {
        "status": "complete", "done": 0}


@pytest.mark.parametrize("value", [True, False, -1, 1.5, "synthetic-private", [], {}, 2**63])
def test_context_pass_rejects_arbitrary_status_reasons_lanes_and_counts(value):
    result = {key: value for key in ("status", "reason", "lane", "done", "scanned", "scan_ms")}
    assert cloud_backlog._context_pass_observation(result) == {"status": "unclassified", "reason": "unclassified"}


@pytest.mark.parametrize("key,value", [("done", 2), ("scanned", 9), ("scan_ms", 900001)])
def test_context_pass_omits_counts_outside_the_real_unit_limits(key, value):
    assert cloud_backlog._context_pass_observation({"status": "deferred", key: value}) == {
        "status": "deferred", "reason": "unclassified"}


def test_context_pass_reports_scan_refusal_and_bounded_scan_stats():
    result = {"status": "deferred", "reason": "patient_context_scan_incomplete",
              "scan_refusal": "bookmark_cas", "covered": False,
              "waiting": 3, "scan_reads": 2, "scan_interrupts": 1,
              "project_id": 123, "error": "synthetic private source"}
    assert cloud_backlog._context_pass_observation(result) == {
        "status": "deferred", "reason": "patient_context_scan_incomplete",
        "scan_refusal": "bookmark_cas", "covered": False,
        "waiting": 3, "scan_reads": 2, "scan_interrupts": 1}


@pytest.mark.parametrize("value", ["synthetic-private", 1, True, ["bookmark_cas"], {"x": 1}])
def test_context_pass_rejects_arbitrary_scan_refusals(value):
    record = cloud_backlog._context_pass_observation({"status": "deferred", "scan_refusal": value})
    assert record["scan_refusal"] == "unclassified"


def test_context_pass_omits_absent_scan_refusal():
    record = cloud_backlog._context_pass_observation({"status": "deferred", "scan_refusal": None})
    assert "scan_refusal" not in record


@pytest.mark.parametrize("key,value", [
    ("waiting", 1001), ("waiting", -1), ("waiting", True), ("waiting", 1.5), ("waiting", "3"),
    ("scan_reads", 17), ("scan_reads", -1), ("scan_interrupts", 9), ("scan_interrupts", True),
    ("covered", 1), ("covered", "true"), ("covered", 0),
])
def test_context_pass_omits_scan_stats_outside_type_or_bounds(key, value):
    record = cloud_backlog._context_pass_observation({"status": "deferred", key: value})
    assert key not in record


@pytest.mark.parametrize("key,value", [
    ("waiting", 0), ("waiting", 1000), ("scan_reads", 16), ("scan_interrupts", 8),
    ("covered", True), ("covered", False),
])
def test_context_pass_accepts_scan_stats_at_real_bounds(key, value):
    record = cloud_backlog._context_pass_observation({"status": "deferred", key: value})
    assert record[key] == value


def test_context_child_captures_only_this_pass_validated_writer_refusal(monkeypatch, tmp_path):
    events = []
    initial = (False, "synthetic-current")
    monkeypatch.setattr(writer_lock, "updater_state", lambda: initial)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda _: {"mode": "on", "max_concurrency": 1})
    monkeypatch.setattr(resident, "_context_memory_available", lambda: True)
    monkeypatch.setattr(cloud_backlog, "owner_alive", lambda: len(events) < 2)
    monkeypatch.setattr(cloud_backlog, "_event", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(cloud_backlog, "time", SimpleNamespace(sleep=lambda _: None))
    def refresh(*args, **kwargs):
        assert kwargs == {"cloud_worker": True}
        if not events:
            writer_lock.record_writer_refusal("source_generation_changed", "generation_fence")
        else:
            writer_lock.record_writer_refusal("synthetic-private", "synthetic-private")
        return {"status": "deferred", "done": 0, "reason": "synthetic-private", "error": "synthetic-private"}
    monkeypatch.setattr(resident, "_background_context_refresh", refresh)
    cloud_backlog._context_worker(object(), initial, 0)
    assert events[0][1] == {"status": "deferred", "done": 0, "reason": "unclassified",
                            "writer_reason": "source_generation_changed", "writer_stage": "generation_fence"}
    assert events[1][1] == {"status": "deferred", "done": 0, "reason": "unclassified"}
    assert "synthetic-private" not in json.dumps(events)


def test_context_child_exits_before_engine_when_import_fence_is_stale(runtime, real_cloud, monkeypatch, tmp_path):
    ledger, clock, _ = runtime
    cfg, posts = real_cloud
    events, sleeps, engine_calls = [], [], []
    run_due = patient_context_refresh.run_due
    def spy(*args, **kwargs):
        engine_calls.append(True)
        return run_due(*args, **kwargs)
    monkeypatch.setattr(patient_context_refresh, "run_due", spy)
    monkeypatch.setattr(mcs_util, "load_config", lambda: cfg)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    monkeypatch.setattr(resident, "disk_free_mb", lambda _: 100000)
    monkeypatch.setattr(resident, "disk_floor_mb", lambda: 1)
    monkeypatch.setattr(resident, "_context_memory_available", lambda: True)
    monkeypatch.setattr(cloud_backlog, "_event", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(cloud_backlog, "owner_alive", lambda: len(events) < 2)
    monkeypatch.setattr(cloud_backlog, "time", SimpleNamespace(sleep=lambda seconds: sleeps.append(seconds)))
    initial = writer_lock.updater_state()
    monkeypatch.setattr(extract_llm_source_contract, "_WRITER_STATE", (False, "synthetic-prior-generation"))
    cloud_backlog._context_worker(ledger, initial, 0)
    assert writer_lock.updater_state() == initial
    assert engine_calls == []
    assert sleeps == []
    assert events == [("patient_context_pass", {
        "status": "source_changed", "done": 0, "writer_reason": "source_generation_changed",
        "writer_stage": "generation_fence"})]
    assert not posts and not document(ledger)
    assert not ledger.artifacts("cloud_usage_reserve") and not ledger.artifacts("cloud_usage_result")
    assert "synthetic-prior-generation" not in json.dumps(events)
