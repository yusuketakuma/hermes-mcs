"""Synthetic anthropic runtime paid lifecycle contracts with isolated shared fixtures."""
import extract_llm_admission
import extract_llm_carry
import extract_llm_context
import extract_llm_engine
import extract_llm_errors
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
from contextlib import contextmanager
import json
import os
import threading
import time
import pytest
from anthropic_client import AnthropicClient
import cloud_policy
import extract_llm as worker
import cloud_inference
import mcs_util
from extract_testkit import _ledger
from anthropic_runtime_testkit import _assert_legacy_retained as _assert_legacy_retained, _bound_source as _bound_source, _current_artifacts as _current_artifacts, _use_real_cloud_accounting as _use_real_cloud_accounting, old as old, routed as routed
from test_cloud_response_recovery import legacy_cloud_routes


@pytest.mark.parametrize("revoke", ["none", "scope", "source", "updater", "key"])
def test_paid_cloud_carry_survives_deadline_and_lease_renewal_but_not_revocation(
        routed, monkeypatch, tmp_path, revoke):
    led, cfg, calls, *_ = routed
    old(led)
    prior = [dict(row) for row in led.artifacts(worker.KIND)]
    blocked = [False]
    def acquire():
        return None if blocked[0] else os.open(tmp_path / "writer", os.O_CREAT | os.O_RDWR)
    original = worker.llm_extract
    def extract(*args, **kwargs):
        value = original(*args, **kwargs)
        blocked[0] = True
        return value
    monkeypatch.setattr(worker, "acquire_run_lock", acquire)
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', worker.acquire_run_lock)
    monkeypatch.setattr(extract_llm_resident, 'acquire_run_lock', worker.acquire_run_lock)
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    with legacy_cloud_routes():
        report = worker.run_pending(led, budget_s=180, per_write_lock=True, write_wait_s=1, carry=[])
    carry = report["carry"]
    assert report["done"] == 0 and len(carry) == 1 and len(calls) == 1
    row, lease, _ = carry[0]
    assert row["_cloud_lease"] == lease
    original_clock = time.monotonic
    monkeypatch.setattr(worker.time, "monotonic", lambda: original_clock() + 200)
    if revoke == "scope":
        cfg["cloud_llm"]["exclude_project_ids"] = [1]
    elif revoke == "source":
        led.db.execute("UPDATE messages SET content_hash='changed' WHERE message_id=1")
        led.db.commit()
    elif revoke == "updater":
        monkeypatch.setattr(worker.writer_lock, "updater_state", lambda: (False, 2))
    elif revoke == "key":
        monkeypatch.setattr(cloud_policy, "api_key", lambda *a: None)
    blocked[0] = False
    worker._settle_carry(led, carry)
    assert not carry and len(calls) == 1
    artifacts = led.artifacts(worker.KIND)
    _assert_legacy_retained(led, prior)
    current = _current_artifacts(led)
    assert len(current) == (1 if revoke == "none" else 0)
    assert len(artifacts) == len(prior) + len(current)
    if current:
        assert json.loads(current[0]["meta"])["backend"] == "anthropic"


def test_dryrun_counts_committed_post_once_after_lock_carry(routed, monkeypatch, tmp_path):
    led, cfg, calls, *_ = routed
    cfg["cloud_llm"]["mode"] = "dry_run"
    old(led)
    blocked = [False]
    def acquire():
        return None if blocked[0] else os.open(tmp_path / "writer", os.O_CREAT | os.O_RDWR)
    def extract(*args, **kwargs):
        blocked[0] = True
        return {}
    monkeypatch.setattr(worker, "acquire_run_lock", acquire)
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', worker.acquire_run_lock)
    monkeypatch.setattr(extract_llm_resident, 'acquire_run_lock', worker.acquire_run_lock)
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    with legacy_cloud_routes():
        report = worker.run_pending(led, budget_s=180, per_write_lock=True, write_wait_s=1, carry=[])
    assert len(report["carry"]) == 1 and not led.artifacts("cloud_usage_dryrun")
    blocked[0] = False
    worker._settle_carry(led, report["carry"])
    assert len(led.artifacts("cloud_usage_dryrun")) == 1 and not calls
    assert worker.run_pending(led, budget_s=180)["done"] == 0
    assert len(led.artifacts("cloud_usage_dryrun")) == 1


@pytest.mark.parametrize("abort", [False, True])
def test_dryrun_estimate_is_atomic_with_successful_extraction(routed, monkeypatch, abort):
    led, cfg, *_ = routed
    cfg["cloud_llm"]["mode"] = "dry_run"
    old(led)
    prior = [dict(row) for row in led.artifacts(worker.KIND)]
    row = _bound_source(led)
    row["_cloud_mode"] = "dry_run"
    record = cloud_policy.dryrun_tx
    def estimate(*args, **kwargs):
        assert led.db.in_transaction
        record(*args, **kwargs)
        assert led.db.in_transaction
        if abort:
            raise RuntimeError("synthetic_save_failure")
    monkeypatch.setattr(cloud_policy, "dryrun_tx", estimate)
    if abort:
        with pytest.raises(RuntimeError, match="synthetic_save_failure"):
            worker._replace_current(led, row, "{}")
        assert [dict(item) for item in led.artifacts(worker.KIND)] == prior
        assert not led.artifacts("cloud_usage_dryrun")
    else:
        assert worker._replace_current(led, row, "{}")
        assert len(led.artifacts("cloud_usage_dryrun")) == 1
        current = _current_artifacts(led)
        assert len(current) == 1 and json.loads(current[0]["meta"])["extract_version"] == worker.EXTRACT_VERSION
        _assert_legacy_retained(led, prior)
    assert not led.db.in_transaction


@pytest.mark.parametrize("parallel", [False, True])
def test_validated_cloud_unit_does_not_expire_before_commit(routed, monkeypatch, parallel):
    led, _, calls, *_ = routed
    old(led)
    if parallel:
        old(led, 2)
    prior = [dict(row) for row in led.artifacts(worker.KIND)]
    original = worker.llm_extract
    clock = time.monotonic
    ready = threading.Barrier(2) if parallel else None
    def extract(*args, **kwargs):
        value = original(*args, **kwargs)
        # Both pool workers complete validation before owner scheduling
        # consumes the remaining inference budget.
        if ready is not None:
            ready.wait(timeout=5)
        monkeypatch.setattr(worker.time, "monotonic", lambda: clock() + 200)
        return value
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        report = worker.run_pending(led, budget_s=180)
    count = 2 if parallel else 1
    assert report["done"] == count and len(calls) == count
    current = _current_artifacts(led)
    assert len(current) == count
    assert all(json.loads(item["meta"])["backend"] == "anthropic" for item in current)
    _assert_legacy_retained(led, prior)


def test_completed_http_uses_result_dispatch_and_never_labels_only_deadline_as_scope(routed, monkeypatch):
    led, cfg, calls, _, _ = routed
    clock = time.monotonic
    outcomes, dispatches = [], []
    def complete(self, prompt, *, deadline, max_tokens, before_attempt, reserve_fn, after_result,
                 cache_prefix=None):
        before_attempt()
        reservation = reserve_fn({"model": cloud_policy.MODEL, "max_tokens": max_tokens}, 1)
        before_attempt()
        calls.append(prompt)
        assert clock() < deadline
        # The HTTP response completed on time, then its owner callback waited.
        monkeypatch.setattr(worker.time, "monotonic", lambda: clock() + 200)
        after_result(reservation, {"http_class": "http_200"})
        return {"status": 200, "finish_reason": "stop", "text": '{"symptoms":[]}'}
    monkeypatch.setattr(worker.anthropic_client.AnthropicClient, "complete", complete)
    monkeypatch.setattr(cloud_policy, "record_result", lambda ledger, reservation, metadata: outcomes.append(metadata))
    def dispatch(fn, *, result=False):
        dispatches.append(result)
        return fn()
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=clock() + 180, current=lambda: True, dispatch=dispatch)
    with legacy_cloud_routes():
        assert provider("synthetic", deadline=clock() + 180, max_tokens=100)["text"] == '{"symptoms":[]}'
    assert outcomes == [{"http_class": "http_200"}]
    assert dispatches[-2:] == [True, True]
    with legacy_cloud_routes(), pytest.raises(cloud_inference.CloudFallback):
        provider("new-send", deadline=clock() + 180, max_tokens=100)
    assert calls == ["synthetic"]


def test_paid_carry_scope_revocation_records_disposition_without_touching_result(routed, monkeypatch, tmp_path):
    led, cfg, calls, reserves, results = routed
    old(led)
    blocked = [False]
    def acquire():
        return None if blocked[0] else os.open(tmp_path / "writer", os.O_CREAT | os.O_RDWR)
    original = worker.llm_extract
    def extract(*args, **kwargs):
        result = original(*args, **kwargs)
        blocked[0] = True
        return result
    monkeypatch.setattr(worker, "acquire_run_lock", acquire)
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', worker.acquire_run_lock)
    monkeypatch.setattr(extract_llm_resident, 'acquire_run_lock', worker.acquire_run_lock)
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    with legacy_cloud_routes():
        result = worker.run_pending(led, budget_s=180, per_write_lock=True, write_wait_s=1, carry=[])
    assert len(result["carry"]) == 1 and len(calls) == len(reserves) == len(results) == 1
    cfg["cloud_llm"]["mode"] = "off"
    blocked[0] = False
    worker._settle_carry(led, result["carry"])
    assert len(calls) == len(reserves) == len(results) == 1
    assert json.loads(led.artifacts("cloud_usage_disposition")[0]["meta"])["disposition"] == "discarded_scope"
    assert json.loads(led.artifacts(worker.KIND)[0]["meta"])["extract_version"] < worker.EXTRACT_VERSION


@pytest.mark.parametrize("error_type", [mcs_util.ProviderSettingsUnreadable, OSError, ValueError])
def test_paid_response_is_settled_when_guard_cannot_read_settings(routed, monkeypatch, error_type):
    led, cfg, _, _, _ = routed
    _use_real_cloud_accounting(monkeypatch)
    unavailable = [False]
    posts = []

    def load():
        if unavailable[0]:
            raise error_type("synthetic")
        return cfg

    def request(self, method, body, timeout, **kwargs):
        posts.append(1)
        unavailable[0] = True
        return 200, {}, json.dumps({"model": worker.anthropic_client.MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": '{"symptoms":[]}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()

    monkeypatch.setattr(worker, "load_config", load)
    monkeypatch.setattr(extract_llm_context, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', worker.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', load)
    monkeypatch.setattr(AnthropicClient, "request", request)
    deadline = time.monotonic() + 180
    def provider():
        return cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1, unit_key="synthetic",
                                     deadline=deadline, current=lambda: True, dispatch=lambda fn, **kwargs: fn(),
                                     input_binding="a" * 64)

    first = provider()
    with legacy_cloud_routes(), pytest.raises(cloud_inference.CloudObservationIncomplete, match="cloud_guard_unverified"):
        first("synthetic", deadline=deadline, max_tokens=100)
    reserves, results = led.artifacts("cloud_usage_reserve"), led.artifacts("cloud_usage_result")
    assert len(posts) == len(reserves) == len(results) == 1
    reservation, result = json.loads(reserves[0]["meta"]), json.loads(results[0]["meta"])
    assert result["reservation_id"] == reservation["reservation_id"]
    assert result["http_class"] == "http_200" and result["error_kind"] is None
    assert result["usage"]["input_tokens"] == 10 and result["usage"]["output_tokens"] == 5
    assert result.get("disposition") is None and not led.db.in_transaction
    assert not led.artifacts("cloud_usage_disposition")
    assert not first.discarded and first.final_disposition is None
    saved = [tuple(row) for row in led.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
    unavailable[0] = False
    assert first("synthetic", deadline=deadline, max_tokens=100)['reused_response'] is True
    assert provider()("synthetic", deadline=deadline, max_tokens=100)['reused_response'] is True
    assert [tuple(row) for row in led.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == saved
    assert len(posts) == len(led.artifacts("cloud_usage_reserve")) == len(led.artifacts("cloud_usage_result")) == 1


@pytest.mark.parametrize("fails_on_read", [1, 2, 3, 4])
@pytest.mark.parametrize("error_type", [mcs_util.ProviderSettingsUnreadable, OSError, ValueError])
def test_unreadable_guard_never_posts_and_settles_a_reserved_unsent_attempt(
        routed, monkeypatch, fails_on_read, error_type):
    led, cfg, _, _, _ = routed
    _use_real_cloud_accounting(monkeypatch)
    reads = [0]
    def load():
        reads[0] += 1
        reserved = led.db.execute("SELECT 1 FROM artifacts WHERE kind='cloud_usage_reserve' LIMIT 1").fetchone()
        # Keep the post-reservation scenario tied to the actual accounting
        # boundary, independent of extra read-only pre-send checks.
        if (bool(reserved) if fails_on_read == 4 else reads[0] >= fails_on_read):
            raise error_type("synthetic")
        return cfg
    monkeypatch.setattr(worker, "load_config", load)
    monkeypatch.setattr(extract_llm_context, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', worker.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', load)
    monkeypatch.setattr(AnthropicClient, "request", lambda *args, **kwargs: pytest.fail("unexpected post"))
    deadline = time.monotonic() + 180
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1, unit_key="synthetic",
                                     deadline=deadline, current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    reason = "cloud_guard_unverified" if fails_on_read in (1, 4) else "cloud_config_unverified"
    with legacy_cloud_routes(), pytest.raises(cloud_inference.CloudObservationIncomplete, match=reason):
        provider("synthetic", deadline=deadline, max_tokens=100)
    reserves, results = led.artifacts("cloud_usage_reserve"), led.artifacts("cloud_usage_result")
    assert len(reserves) == len(results) == (1 if fails_on_read == 4 else 0)
    if results:
        result = json.loads(results[0]["meta"])
        assert result["http_class"] == "not_sent" and result["usage"] is None
        assert result["reservation_id"] == json.loads(reserves[0]["meta"])["reservation_id"]
    assert not led.artifacts('cloud_usage_disposition')
    assert not provider.discarded and provider.final_disposition is None
    assert not provider.possibly_sent and not led.db.in_transaction


def test_transient_read_error_after_reservation_stays_unsent_without_scope_discard(routed, monkeypatch):
    led, cfg, _, _, _ = routed
    _use_real_cloud_accounting(monkeypatch)
    failed = [False]
    def load():
        reserved = led.db.execute("SELECT 1 FROM artifacts WHERE kind='cloud_usage_reserve' LIMIT 1").fetchone()
        if reserved and not failed[0]:
            failed[0] = True
            raise mcs_util.ProviderSettingsUnreadable("synthetic")
        return cfg
    monkeypatch.setattr(worker, "load_config", load)
    monkeypatch.setattr(extract_llm_context, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', worker.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', load)
    monkeypatch.setattr(AnthropicClient, "request", lambda *args, **kwargs: pytest.fail("unexpected post"))
    deadline = time.monotonic() + 180
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1, unit_key="synthetic",
                                     deadline=deadline, current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    with legacy_cloud_routes(), pytest.raises(cloud_inference.CloudObservationIncomplete, match="cloud_guard_unverified"):
        provider("synthetic", deadline=deadline, max_tokens=100)
    result, = led.artifacts("cloud_usage_result")
    meta = json.loads(result["meta"])
    assert meta["http_class"] == "not_sent" and meta["usage"] is None
    assert not led.artifacts("cloud_usage_disposition")
    assert not provider.discarded and provider.final_disposition is None
    assert not provider.possibly_sent and not led.db.in_transaction


def test_config_read_failure_preserves_previously_settled_response(routed, monkeypatch):
    led, cfg, _, _, _ = routed
    _use_real_cloud_accounting(monkeypatch)
    unavailable, posts = [False], []
    def load():
        if unavailable[0]:
            raise mcs_util.ProviderSettingsUnreadable("synthetic")
        return cfg
    def request(self, method, body, timeout, **kwargs):
        posts.append(1)
        return 200, {}, json.dumps({"model": worker.anthropic_client.MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": '{"symptoms":[]}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()
    monkeypatch.setattr(worker, "load_config", load)
    monkeypatch.setattr(extract_llm_context, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', worker.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', load)
    monkeypatch.setattr(AnthropicClient, "request", request)
    deadline = time.monotonic() + 180
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1, unit_key="synthetic",
                                     deadline=deadline, current=lambda: True, dispatch=lambda fn, **kwargs: fn(),
                                     input_binding="b" * 64)
    with legacy_cloud_routes():
        provider("synthetic", deadline=deadline, max_tokens=100)
    unavailable[0] = True
    with legacy_cloud_routes(), pytest.raises(cloud_inference.CloudObservationIncomplete, match="cloud_guard_unverified"):
        provider("synthetic", deadline=deadline, max_tokens=100)
    result, = led.artifacts("cloud_usage_result")
    meta = json.loads(result["meta"])
    assert len(posts) == 1 and meta["http_class"] == "http_200"
    assert meta["usage"]["input_tokens"] == 10 and meta["usage"]["output_tokens"] == 5
    assert not led.artifacts("cloud_usage_disposition")
    assert not provider.discarded and provider.final_disposition is None
    assert json.loads(result['content'])['text'] == '{"symptoms":[]}'
    assert not led.db.in_transaction


def test_http_deadline_reserves_existing_commit_margin_without_changing_unit_deadline(routed, monkeypatch):
    led, cfg, _, reserved, results = routed
    unit_deadline = time.monotonic() + 180
    repair_deadline = unit_deadline - 30
    seen = []
    original = worker.anthropic_client.AnthropicClient.complete
    def complete(self, prompt, **kwargs):
        seen.append(kwargs["deadline"])
        return original(self, prompt, **kwargs)
    monkeypatch.setattr(worker.anthropic_client.AnthropicClient, "complete", complete)
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=unit_deadline, current=lambda: True,
        dispatch=lambda fn, **kwargs: fn())
    with legacy_cloud_routes():
        assert provider("synthetic", deadline=repair_deadline, max_tokens=100)["text"] == '{"symptoms":[]}'
    assert seen == [repair_deadline - worker.patient_context_refresh.COMMIT_MARGIN]
    assert provider.deadline == unit_deadline and len(reserved) == len(results) == 1


@pytest.mark.parametrize("remaining", [0, 4])
def test_commit_margin_exhaustion_never_reserves_or_sends(routed, remaining):
    led, cfg, calls, reserved, results = routed
    deadline = time.monotonic() + remaining
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=deadline, current=lambda: True,
        dispatch=lambda fn, **kwargs: fn())
    with pytest.raises(cloud_inference.CloudFallback, match="cloud_unit_deadline"):
        provider("synthetic", deadline=deadline, max_tokens=100)
    assert not calls and not reserved and not results
    assert not led.artifacts("cloud_usage_disposition") and not led.db.in_transaction


@pytest.mark.parametrize("loss,final_lock", [("source", True), ("source", False),
    ("lease", True), ("lease", False), ("none", False)])
def test_pending_write_loss_records_discard_but_lock_wait_does_not(routed, monkeypatch, loss, final_lock):
    led, _, calls, reserved, results = routed
    old(led)
    result_locks = []
    @contextmanager
    def locked(enabled, **kwargs):
        # GC may or may not run, depending on its process-wide cadence.
        # Select result and flush lock attempts by the observed HTTP call.
        if not calls:
            yield True
            return
        result_locks.append(enabled)
        if len(result_locks) == 2:
            if loss == "source":
                led.db.execute("UPDATE messages SET content_hash='changed' WHERE message_id=1")
            elif loss == "lease":
                led.db.execute("DELETE FROM fetch_jobs WHERE kind='extract_claim'")
            led.db.commit()
        # The result write waits; the final flush rechecks after the mutation.
        yield len(result_locks) == 2 and final_lock
    monkeypatch.setattr(worker, "_write_lock", locked)
    monkeypatch.setattr(extract_llm_carry, '_write_lock', worker._write_lock)
    monkeypatch.setattr(extract_llm_resident, '_write_lock', worker._write_lock)
    monkeypatch.setattr(extract_llm_runner, '_write_lock', worker._write_lock)
    with legacy_cloud_routes():
        report = worker.run_pending(led, budget_s=180, per_write_lock=True, write_wait_s=1, carry=[])
    assert report["done"] == 0 and len(result_locks) == 2
    assert len(calls) == len(reserved) == len(results) == 1
    assert len(led.artifacts("cloud_usage_disposition")) == int(loss != "none")
    assert len(report["carry"]) == int(loss == "none")
    if report["carry"]:
        worker._settle_carry(led, report["carry"], final=True)
        assert not report["carry"] and not led.artifacts("cloud_usage_disposition")
    assert json.loads(led.artifacts(worker.KIND)[0]["meta"])["extract_version"] < worker.EXTRACT_VERSION


def test_unreadable_config_never_auto_enables_the_paid_route(routed):
    # load_config() answers {} when config.json cannot be read; with a key in
    # .env the absent-block default would be "on", so {} must read as off
    led, *_ = routed
    assert cloud_inference.resolve_cloud_settings({})["mode"] == "off"
    assert worker._cloud_mode(led, {}, "reextract", 1) == "off"
    # A readable config uses the spending defaults, without changing its scope.
    defaults = cloud_inference.resolve_cloud_settings({"mcs_login_id": "synthetic"})
    assert defaults["mode"] == "on" and defaults["project_ids"] is None
    assert (defaults["daily_cost_limit_usd"], defaults["monthly_cost_limit_usd"]) == (25, 75)
    assert cloud_inference.resolve_cloud_settings({"mcs_login_id": "synthetic", "cloud_llm": {
        "daily_cost_limit_usd": None, "monthly_cost_limit_usd": None}})["mode"] == "off"
    assert cloud_inference.resolve_cloud_settings({"mcs_login_id": "synthetic",
                                   "cloud_llm": {"daily_cost_limit_usd": 2}})["mode"] == "on"


def test_paid_cloud_failure_with_the_local_breaker_open_records_the_retry(routed, monkeypatch):
    led, cfg, calls, reserves, results = routed
    old(led)
    monkeypatch.setattr(worker, "circuit_open_s", lambda db: 60)
    monkeypatch.setattr(extract_llm_resident, 'circuit_open_s', worker.circuit_open_s)
    monkeypatch.setattr(extract_llm_runner, 'circuit_open_s', worker.circuit_open_s)
    original = worker.llm_extract
    def extract(body, **kwargs):
        if kwargs.get("provider"):
            original(body, **kwargs)      # the paid attempt is sent and settled
            raise cloud_inference.CloudFallback("cloud_incomplete_response")
        pytest.fail("local must not run while its breaker is open")
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        report = worker.run_pending(led, budget_s=180)
    assert report["deferred"] == 1 and report["done"] == 0 and len(calls) == len(results) == 1
    [error] = [json.loads(a["meta"]) for a in led.artifacts(worker.KIND) if json.loads(a["meta"]).get("error")]
    assert error["attempts"] == 1 and error["next_try"] > time.time()
    # the retry backoff keeps the next pass from re-sending the same body
    assert worker.run_pending(led, budget_s=180)["done"] == 0 and len(calls) == 1


@pytest.mark.parametrize("updated_carry", ["omitted", "returned"])
@pytest.mark.parametrize("release_fails", [False, True])
def test_lane_two_updated_exits_without_reusing_stale_carry(tmp_path, monkeypatch, updated_carry, release_fails):
    ledger = _ledger(tmp_path)
    close = ledger.close
    events, sleeps = [], []
    ledger.artifact_add("synthetic_paid_result", "{}", meta={"status": "received"})
    saved = [dict(row) for row in ledger.artifacts("synthetic_paid_result")]
    pending = {"project_id": 1, "_cloud_discard": lambda: events.append("discard")}
    lease = time.time() + 600
    def no_commit(**kwargs):
        pytest.fail("stale worker must not commit carried result")
    carried = [(pending, lease, no_commit)]

    class Pool:
        def __init__(self, root, *, context_only):
            assert root == str(tmp_path)
            assert context_only is True
        def sync(self, cfg, *, ledger):
            events.append("pool_sync")
        def close(self):
            events.append("pool_closed")

    @contextmanager
    def locked(*args, **kwargs):
        yield True

    def release(db, row, token):
        assert db is ledger and row is pending and token == lease
        events.append("release")
        if release_fails:
            raise RuntimeError("synthetic release failure")

    calls = []
    def run(db, **kwargs):
        calls.append(list(kwargs["carry"]))
        if len(calls) == 1:
            return {"done": 1, "failed": 0, "left": 1, "carry": list(carried)}
        assert len(calls) == 2 and calls[-1] == carried
        result = {"done": 0, "failed": 0, "left": None, "updated": True}
        if updated_carry == "returned":
            result["carry"] = list(carried)
        return result

    monkeypatch.setattr(worker, "HOME", str(tmp_path))
    monkeypatch.setattr(extract_llm_engine, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_prepare, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_provider, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_resident, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_selection, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_source_contract, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_store, 'HOME', worker.HOME)
    monkeypatch.setattr(worker, "Ledger", lambda *args: ledger)
    monkeypatch.setattr(extract_llm_resident, 'Ledger', worker.Ledger)
    monkeypatch.setattr(ledger, "close", lambda: events.append("ledger_closed"))
    monkeypatch.setattr(worker.cloud_backlog, "CloudBacklogPool", Pool)
    monkeypatch.setattr(worker, "load_config", lambda: {})
    monkeypatch.setattr(extract_llm_context, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', worker.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: {"mode": "off", "max_concurrency": 1})
    monkeypatch.setattr(worker, "legacy_admissions", lambda *args: None)
    monkeypatch.setattr(extract_llm_admission, 'legacy_admissions', worker.legacy_admissions)
    monkeypatch.setattr(extract_llm_resident, 'legacy_admissions', worker.legacy_admissions)
    monkeypatch.setattr(worker.extract_validation, "vital_threshold_policy", lambda cfg: None)
    monkeypatch.setattr(worker.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(worker, "_writer_current", lambda *, full=False: True)
    monkeypatch.setattr(extract_llm_carry, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_errors, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_resident, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_runner, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_selection, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_source_contract, '_writer_current', worker._writer_current)
    monkeypatch.setattr(worker, "_write_lock", locked)
    monkeypatch.setattr(extract_llm_carry, '_write_lock', worker._write_lock)
    monkeypatch.setattr(extract_llm_resident, '_write_lock', worker._write_lock)
    monkeypatch.setattr(extract_llm_runner, '_write_lock', worker._write_lock)
    monkeypatch.setattr(worker, "_release", release)
    monkeypatch.setattr(extract_llm_carry, '_release', worker._release)
    monkeypatch.setattr(extract_llm_runner, '_release', worker._release)
    monkeypatch.setattr(worker, "run_pending", run)
    monkeypatch.setattr(extract_llm_resident, 'run_pending', worker.run_pending)
    monkeypatch.setattr(worker, "_background_semantic", lambda *args: events.append("semantic") or {"done": 0})
    monkeypatch.setattr(extract_llm_resident, '_background_semantic', worker._background_semantic)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setenv("MCS_LLM_SLOT", "synthetic")
    monkeypatch.setenv(worker.local_llm.TURN_LOCK_ENV, "synthetic")
    monkeypatch.setattr(worker.sys, "argv", ["extract_llm", "--all", "--lane", "2", "--semantic"])
    try:
        assert worker.main() == 0
        assert len(calls) == 2 and sleeps == [1]
        assert events.count("semantic") == 0
        assert events.count("discard") == events.count("release") == 1
        assert events[-2:] == ["pool_closed", "ledger_closed"]
        assert [dict(row) for row in ledger.artifacts("synthetic_paid_result")] == saved
    finally:
        close()


def test_paid_carry_survives_lowered_cap_and_blocks_next_send(routed, monkeypatch, tmp_path):
    led, cfg, _, _, _ = routed
    _use_real_cloud_accounting(monkeypatch)
    monkeypatch.setattr(worker, "HOME", str(tmp_path))
    monkeypatch.setattr(extract_llm_engine, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_prepare, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_provider, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_resident, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_selection, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_source_contract, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_store, 'HOME', worker.HOME)
    cfg["cloud_llm"]["daily_request_budget"] = 5
    old(led)
    posts = []
    blocked = [False]

    def request(self, method, body, timeout, **kwargs):
        posts.append(1)
        return 200, {}, json.dumps({
            "model": worker.anthropic_client.MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": '{"symptoms":[]}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }).encode()

    monkeypatch.setattr(AnthropicClient, "request", request)
    def acquire():
        return None if blocked[0] else os.open(tmp_path / "writer", os.O_CREAT | os.O_RDWR, 0o600)
    original = worker.llm_extract
    def extract(*args, **kwargs):
        value = original(*args, **kwargs)
        blocked[0] = True
        return value
    monkeypatch.setattr(worker, "acquire_run_lock", acquire)
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', worker.acquire_run_lock)
    monkeypatch.setattr(extract_llm_resident, 'acquire_run_lock', worker.acquire_run_lock)
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    with legacy_cloud_routes():
        report = worker.run_pending(led, budget_s=180, per_write_lock=True, write_wait_s=1, carry=[])
    carry = report["carry"]
    reserves, results = led.artifacts("cloud_usage_reserve"), led.artifacts("cloud_usage_result")
    assert len(carry) == len(posts) == len(reserves) == len(results) == 1
    first_reservation = json.loads(reserves[0]["meta"])
    first_result = json.loads(results[0]["meta"])
    assert first_result["reservation_id"] == first_reservation["reservation_id"]
    assert first_result["http_class"] == "http_200" and first_result["error_kind"] is None
    assert first_result["usage"]["input_tokens"] == 10 and first_result["usage"]["output_tokens"] == 5

    # The cap is valid and already reached by the first paid request.
    cfg["cloud_llm"]["daily_request_budget"] = 1
    assert not cloud_policy.cloud_config(cfg)[1]
    blocked[0] = False
    worker._settle_carry(led, carry)
    assert not carry and len(_current_artifacts(led)) == 1
    assert json.loads(_current_artifacts(led)[0]["meta"])["backend"] == "anthropic"
    assert not led.artifacts("cloud_usage_disposition")
    assert json.loads(led.artifacts("cloud_usage_result")[0]["meta"]) == first_result

    deadline = time.monotonic() + 180
    next_unit = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
        message_id=2, unit_key="synthetic-next", deadline=deadline, current=lambda: True,
        dispatch=lambda fn, **kwargs: fn())
    with legacy_cloud_routes(), pytest.raises(cloud_inference.CloudFallback, match="cloud_send_blocked"):
        next_unit("different synthetic request", deadline=deadline, max_tokens=100)
    assert not next_unit.possibly_sent
    assert len(posts) == len(led.artifacts("cloud_usage_reserve")) == len(led.artifacts("cloud_usage_result")) == 1
    assert not led.db.in_transaction


def test_new_unit_deadline_preserves_paid_response_without_local_fallback(routed, monkeypatch):
    led, cfg, _, _, _ = routed
    _use_real_cloud_accounting(monkeypatch)
    posts, local = [], []

    def request(self, method, body, timeout, **kwargs):
        posts.append(1)
        return 200, {}, json.dumps({"model": worker.anthropic_client.MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": '{"symptoms":[]}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()

    monkeypatch.setattr(AnthropicClient, "request", request)
    deadline = time.monotonic() + 180
    def unit(end):
        return cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
            unit_key="synthetic-paid-deadline", input_binding="c" * 64, deadline=end,
            current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    with legacy_cloud_routes():
        assert unit(deadline)("synthetic", deadline=deadline, max_tokens=100)["text"] == '{"symptoms":[]}'
    saved = [tuple(row) for row in led.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
    expired = time.monotonic() + 1
    blocked = unit(expired)
    def mixed_call():
        try:
            return blocked("synthetic", deadline=expired, max_tokens=100)
        except cloud_inference.CloudObservationIncomplete:
            raise
        except cloud_inference.CloudFallback:
            local.append(1)
    with pytest.raises(cloud_inference.CloudObservationIncomplete, match="cloud_unit_deadline"):
        mixed_call()
    assert not local and not blocked.discarded and blocked.final_disposition is None
    assert [tuple(row) for row in led.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == saved
    assert unit(deadline)("synthetic", deadline=deadline, max_tokens=100)["reused_response"] is True
    assert len(posts) == 1 and not led.artifacts("cloud_usage_disposition")
    assert [tuple(row) for row in led.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == saved
    assert not led.db.in_transaction
