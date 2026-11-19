"""The context child reaches the real context engine without occupying a local turn."""
from types import SimpleNamespace
import json
import sqlite3

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
    # Lane 3 serves only the patients the cloud does not: it never sends. With a usable key the
    # patient is the cloud's, and the lane skips it; without one, the lane does its local work.
    monkeypatch.setattr(resident.extract_llm_call_context, "_SLOT_OVERRIDE", local_llm.LANES[3])
    monkeypatch.setattr(resident, "HOME", str(tmp_path))
    lane = resident._background_context_refresh(ledger, initial)
    assert lane["status"] != "off" and not posts
    if cloud_ready:
        assert lane["reason"] == "patient_context_other_role" and lane["wait_other_role"] == 1
        assert not local_calls
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


def test_context_retry_delay_backs_off_for_selection_busy_and_cloud_defers():
    assert cloud_backlog._context_retry_delay({"done": 1}) == 1
    assert cloud_backlog._context_retry_delay(
        {"done": 0, "reason": "patient_context_selection_busy"}) == 20
    assert cloud_backlog._context_retry_delay(
        {"done": 0, "reason": "patient_context_waiting"}) == 5
    assert cloud_backlog._context_retry_delay({"done": 0}) == 5
    # A full send slot frees within seconds; a cap, a circuit or a pending result does not.
    assert cloud_backlog._context_retry_delay(
        {"done": 0, "reason": "patient_context_cloud_deferred:cloud_capacity"}) == 5
    for code in ("daily_cap", "circuit_open", "env_file_unreadable", "result_pending", ""):
        assert cloud_backlog._context_retry_delay(
            {"done": 0, "reason": "patient_context_cloud_deferred:" + code}) == 60
    assert cloud_backlog._context_retry_delay({"done": 0, "reason": "patient_context_cloud_deferred"}) == 60
    assert cloud_backlog._context_retry_delay({"done": 0, "reason": "patient_context_cloud_budget"}) == 60
    # Every patient the pass reached is served by the other worker role.
    assert cloud_backlog._context_retry_delay({"done": 0, "reason": "patient_context_other_role"}) == 60
    assert cloud_backlog._context_retry_delay({"done": 0, "reason": "patient_context_role_changed"}) == 5
    assert cloud_backlog._context_retry_delay({"done": 0, "reason": "patient_context_cloud_not_sent"}) == 5
    assert cloud_backlog._context_retry_delay({"done": 0, "reason": 7}) == 5
    assert cloud_backlog._context_retry_delay({"done": 1, "reason": "patient_context_cloud_deferred:daily_cap"}) == 1


@pytest.mark.parametrize("stop,expected", [
    ("cloud_capacity", "cloud_capacity"), ("daily_cap", "daily_cap"), ("mode_off", "mode_off"),
    ("http_5xx", "http_5xx"), ("result_missing", "result_missing"), ("local_pinned", "local_pinned"),
    ("unknown", "unknown"), ("synthetic private text", "unclassified"), ("result_pending", "unclassified"),
    ("a" * 81, "unclassified"), (7, "unclassified"), (["daily_cap"], "unclassified"),
])
def test_context_pass_reports_only_a_declared_cloud_refusal_code(stop, expected):
    record = cloud_backlog._context_pass_observation({
        "status": "deferred", "reason": "patient_context_cloud_deferred:" + str(stop), "cloud_refusal": stop})
    # The reason keeps its fixed base; the stop code passes only when the classifier declares it.
    assert record["reason"] == "patient_context_cloud_deferred" and record["cloud_refusal"] == expected
    assert "cloud_refusal" not in cloud_backlog._context_pass_observation(
        {"status": "deferred", "reason": "patient_context_waiting"})


def test_context_pass_reports_only_fixed_bounded_diagnostics():
    result = {"status": "deferred", "done": 0, "reason": "patient_context_waiting",
              "scanned": 8, "scan_ms": 900000, "lane": "B", "project_id": 123,
              "error": "synthetic private source", "patient": "synthetic private patient"}
    assert cloud_backlog._context_pass_observation(result) == {
        "status": "deferred", "done": 0, "reason": "patient_context_waiting",
        "scanned": 8, "scan_ms": 900000, "lane": "B"}
    assert cloud_backlog._context_pass_observation({"status": "complete", "done": 0}) == {
        "status": "complete", "done": 0}


@pytest.mark.parametrize("reason", [
    "source_quality_unavailable:RuntimeStale",
    "source_quality_unavailable:patient_context_audit_mismatch",
    "summary_quality_unavailable:ValueError",
    "patient_context_waiting:anything",
])
def test_context_pass_classifies_suffixed_reasons_by_fixed_base(reason):
    record = cloud_backlog._context_pass_observation({"status": "deferred", "reason": reason})
    assert record["reason"] == reason.partition(":")[0]
    assert ":" not in record["reason"]


@pytest.mark.parametrize("reason", [
    "synthetic-private:detail", "unknown_base:x", ":leading", "a" * 100 + ":x",
])
def test_context_pass_rejects_suffixed_reasons_with_unknown_base(reason):
    record = cloud_backlog._context_pass_observation({"status": "deferred", "reason": reason})
    assert record["reason"] == "unclassified"


@pytest.mark.parametrize("status,key,value,expected", [
    ("failed", "reason", "cloud_result_discarded", "cloud_result_discarded"),
    ("failed", "reason", "patient_source_quality_incomplete", "patient_source_quality_incomplete"),
    ("failed", "reason", "ValueError", "ValueError"),
    ("error", "error", "RuntimeError", "RuntimeError"),
    ("error", "error", "AnthropicError", "AnthropicError"),
    ("error", "reason", "patient_context_waiting", "patient_context_waiting"),
    ("failed", "reason", "synthetic-private", "unclassified"),
    ("failed", "reason", "synthetic private text", "unclassified"),
    ("failed", "reason", "secret/path/../x", "unclassified"),
    ("failed", "reason", "a" * 81, "unclassified"),
    ("error", "error", "synthetic-private", "unclassified"),
    ("error", "error", "合成エラー", "unclassified"),
    ("error", "error", None, "unclassified"),
])
def test_context_pass_reports_bounded_failed_and_error_codes(status, key, value, expected):
    result = {"status": status, "done": 0, key: value}
    assert cloud_backlog._context_pass_observation(result)["reason"] == expected


def test_context_pass_keeps_deferred_reasons_strictly_whitelisted():
    for reason in ("cloud_result_discarded", "ValueError", "RuntimeStale"):
        record = cloud_backlog._context_pass_observation({"status": "deferred", "reason": reason})
        assert record["reason"] == "unclassified"


@pytest.mark.parametrize("reason", [
    "patient_context_generation_active",
    "patient_context_generation_clock_changed",
    "patient_context_generation_daily_limit",
    "patient_context_generation_interval",
    "patient_summary_quality_lock",
    "patient_summary_quality_budget_changed",
    "patient_summary_quality_source_changed",
    "summary_quality_unknown",
    "source_quality_unknown",
    "patient_context_role_changed",
    "patient_context_other_role",
])
def test_context_pass_reports_known_deferral_reasons(reason):
    record = cloud_backlog._context_pass_observation({"status": "deferred", "reason": reason})
    assert record["reason"] == reason


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


@pytest.mark.parametrize("value", ["synthetic-private", "合成の未知理由", 1, True, ["bookmark_cas"], {"x": 1}])
def test_context_pass_rejects_arbitrary_scan_refusals(value):
    record = cloud_backlog._context_pass_observation({"status": "deferred", "scan_refusal": value})
    assert record["scan_refusal"] == "unclassified"


def test_context_pass_omits_absent_scan_refusal():
    record = cloud_backlog._context_pass_observation({"status": "deferred", "scan_refusal": None})
    assert "scan_refusal" not in record


@pytest.mark.parametrize("key,value", [
    ("waiting", 17), ("waiting", -1), ("waiting", True), ("waiting", 1.5), ("waiting", "3"),
    ("scan_reads", 17), ("scan_reads", -1), ("scan_interrupts", 2), ("scan_interrupts", True),
    ("covered", 1), ("covered", "true"), ("covered", 0),
])
def test_context_pass_omits_scan_stats_outside_type_or_bounds(key, value):
    record = cloud_backlog._context_pass_observation({"status": "deferred", key: value})
    assert key not in record


@pytest.mark.parametrize("key,value", [
    ("waiting", 0), ("waiting", 16), ("scan_reads", 16), ("scan_interrupts", 1),
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


@pytest.mark.parametrize('key,limit', [
    ('wait_no_eligible_sources', 8), ('wait_no_due_lane', 16),
    ('wait_generation_quota', 8), ('wait_readiness_unverified', 16),
    ('wait_dependency_incomplete', 16), ('skip_patient_invalid', 8),
    ('wait_patient_busy', 8), ('wait_other_role', 64),
])
def test_context_pass_wait_branch_counters_are_strict_and_bounded(key, limit):
    for valid in (0, limit):
        assert cloud_backlog._context_pass_observation({'status': 'deferred', key: valid})[key] == valid
    for invalid in (True, False, -1, limit + 1, 1.0, '合成の非公開値', None, [], {}):
        assert key not in cloud_backlog._context_pass_observation({'status': 'deferred', key: invalid})
    assert 'wait_unregistered' not in cloud_backlog._context_pass_observation({
        'status': 'deferred', 'wait_unregistered': 1})


@pytest.mark.parametrize("wait,expected", [(60, 60), (1, 1), (0, None), (61, None), (-1, None),
                                           (1.5, None), (True, None), ("60", None)])
def test_context_pass_reports_the_bounded_run_lock_wait(wait, expected):
    record = cloud_backlog._context_pass_observation(
        {"status": "deferred", "done": 0, "reason": "run_lock", "lock_wait_s": wait})
    assert record.get("lock_wait_s") == expected and record["reason"] == "run_lock"


@pytest.mark.parametrize("guard,expected", [
    ("cloud_unit_unresolved", "cloud_unit_unresolved"), ("OperationalError", "OperationalError"),
    ("synthetic private text", "unclassified"), ("secret/path", "unclassified"),
    ("a" * 81, "unclassified"), (7, "unclassified"),
])
def test_context_pass_reports_only_a_bounded_guard_refusal_code(guard, expected):
    record = cloud_backlog._context_pass_observation(
        {"status": "deferred", "reason": "patient_context_cloud_not_sent", "guard_error": guard})
    assert record["reason"] == "patient_context_cloud_not_sent" and record["guard_error"] == expected


def test_guard_refusal_code_keeps_the_cloud_kind_and_drops_text():
    import cloud_client
    import patient_context_refresh
    code = patient_context_refresh._guard_refusal_code
    assert code(cloud_client.CloudError("cloud_unit_unresolved", detail="synthetic private")) \
        == "cloud_unit_unresolved"
    assert code(cloud_client.CloudNotSent("cloud_budget_blocked")) == "cloud_budget_blocked"
    assert code(ValueError("synthetic private patient")) == "ValueError"
    assert code(sqlite3.OperationalError("database is locked")) == "OperationalError"


def test_context_child_waits_through_unreadable_provider_settings(monkeypatch, tmp_path):
    events, sleeps, reads = [], [], []
    initial = (False, "synthetic-current")
    monkeypatch.setattr(writer_lock, "updater_state", lambda: initial)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))

    def load():
        reads.append(True)
        if len(reads) <= 2:
            raise mcs_util.ProviderSettingsUnreadable("synthetic-private")
        return {}
    monkeypatch.setattr(mcs_util, "load_config", load)
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda _: {"mode": "on", "max_concurrency": 1})
    monkeypatch.setattr(resident, "_context_memory_available", lambda: True)
    monkeypatch.setattr(cloud_backlog, "owner_alive", lambda: not any(e == "patient_context_pass" for e, _ in events))
    monkeypatch.setattr(cloud_backlog, "_event", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(cloud_backlog.time, "sleep", sleeps.append)
    monkeypatch.setattr(resident, "_background_context_refresh",
                        lambda *a, **kw: {"status": "complete", "done": 0})
    # A settings commit in progress must not end the child (the parent restarts it only later).
    cloud_backlog._context_worker(object(), initial, 0)
    assert len(reads) == 3 and sleeps[:2] == [10, 10]
    assert [e for e, _ in events] == ["cloud_backlog_deferred", "patient_context_pass"]
    assert events[0][1] == {"reason": "provider_settings_unreadable"}
    assert "synthetic-private" not in json.dumps(events)


def test_context_pass_patient_invalid_reason_and_skip_code_are_bounded():
    record = cloud_backlog._context_pass_observation({
        'status': 'deferred', 'reason': 'patient_context_patient_invalid',
        'skip_patient_invalid': 1, 'skip_patient_code': 'patient_context_source_scope_invalid'})
    assert record['reason'] == 'patient_context_patient_invalid'
    assert record['skip_patient_invalid'] == 1
    assert record['skip_patient_code'] == 'patient_context_source_scope_invalid'
    for bad in ('合成の非公開値', 'synthetic private text', 7, 'x' * 81):
        assert cloud_backlog._context_pass_observation({
            'status': 'deferred', 'skip_patient_code': bad})['skip_patient_code'] == 'unclassified'


def test_context_child_holds_the_cloud_role_and_retries_a_failed_hold(monkeypatch, tmp_path):
    """Local lanes see the cloud lane served only through the child's ROLE_CLOUD hold."""
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings",
                        lambda cfg: {"mode": "on", "max_concurrency": 1})
    monkeypatch.setattr(resident, "_context_memory_available", lambda: True)
    initial = (False, 1)
    monkeypatch.setattr(writer_lock, "updater_state", lambda: initial)
    monkeypatch.setattr(cloud_backlog, "_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(cloud_backlog, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda seconds: None))
    holds = []
    real_hold = cloud_backlog.hold_role

    def hold(root, name):
        assert name == cloud_backlog.ROLE_CLOUD
        holds.append(name)
        return None if len(holds) == 1 else real_hold(root, name)   # the first hold fails

    monkeypatch.setattr(cloud_backlog, "hold_role", hold)
    seen = []

    def context_pass(ledger, state, *, cloud_worker):
        assert cloud_worker and state == initial
        seen.append(cloud_backlog.role_alive(tmp_path, cloud_backlog.ROLE_CLOUD))
        return {"status": "deferred", "done": 0}

    monkeypatch.setattr(resident, "_background_context_refresh", context_pass)
    monkeypatch.setattr(cloud_backlog, "owner_alive", lambda: len(seen) < 3)
    cloud_backlog._context_worker(object(), initial, 0)
    # The failed hold is retried on the next pass, and only until it is held.
    assert seen == [False, True, True] and len(holds) == 2
    assert cloud_backlog.role_alive(tmp_path, cloud_backlog.ROLE_CLOUD) is False   # released on exit
