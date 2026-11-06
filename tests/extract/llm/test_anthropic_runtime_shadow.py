"""Synthetic anthropic runtime shadow contracts with isolated shared fixtures."""
import extract_llm_carry
import extract_llm_resident
import extract_llm_runner
import extract_llm_store
from contextlib import contextmanager
import json
import time
import pytest
import cloud_policy
import extract_llm as worker
import cloud_inference
from extract_testkit import _hash
from anthropic_runtime_testkit import _assert_legacy_retained as _assert_legacy_retained, _bound_source as _bound_source, _current_artifacts as _current_artifacts, old as old, routed as routed
from test_cloud_response_recovery import legacy_cloud_routes


def test_shadow_comparison_follows_commit_and_never_replaces_local(routed, monkeypatch):
    led, cfg, calls, *_ = routed
    cfg["cloud_llm"]["mode"] = "shadow"
    old(led)
    phases = []
    def extract(body, **kwargs):
        provider = kwargs.get("provider")
        phases.append("cloud" if provider else "local")
        if provider:
            assert worker._current(led, 1, _hash(led))
            assert not led.db.in_transaction
            provider("synthetic", deadline=kwargs["deadline"], max_tokens=100)
        return {}
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        report = worker.run_pending(led, budget_s=180)
    assert report["done"] == 1 and phases == ["local", "cloud"] and len(calls) == 1
    assert "backend" not in json.loads(led.artifacts(worker.KIND)[0]["meta"])
    assert len(led.artifacts("cloud_compare_v1")) == 1


@pytest.mark.parametrize("field", ["subject", "negated", "status", "evidence", "project_id", "message_id", "content_hash"])
def test_shadow_normalization_binds_source_scope_attributes_and_quote_span(field):
    source = {"source": "chat", "project_id": 1, "message_id": 1, "content_hash": "synthetic-a",
              "body_text": "合成の症状なし。別の合成の症状あり。"}
    item = {"text": "症状", "subject": "patient", "negated": False, "status": "current",
            "evidence": "合成の症状なし。"}
    local = {"symptoms": [item]}
    changed_source, changed_item = dict(source), dict(item)
    if field in ("project_id", "message_id", "content_hash"):
        changed_source[field] = 2 if field != "content_hash" else "synthetic-b"
    else:
        changed_item[field] = {"subject": "family", "negated": True, "status": "past",
                               "evidence": "別の合成の症状あり。"}[field]
    before = worker._comparison_extract(source, local)
    assert before == worker._comparison_extract(dict(source), local)
    assert before != worker._comparison_extract(changed_source, {"symptoms": [changed_item]})


def test_shadow_comparison_unknown_measurements_are_not_fabricated_zeros(routed):
    led, *_ = routed
    worker._cloud_compare(led, route="reextract", project_id=1, local_facts={"synthetic"})
    meta = json.loads(led.artifacts("cloud_compare_v1")[0]["meta"])
    assert meta["units"] == 1 and meta["validated_units"] == 0
    for field in ("cloud_facts", "matched", "validated", "rejected", "held", "repairs"):
        assert meta[field] is None
    assert meta["rejection_observations"] == meta["held_observations"] == meta["repair_observations"] == 0


def test_failed_shadow_attempt_keeps_unit_and_unknown_quality_denominators(routed, monkeypatch):
    led, cfg, _, *_ = routed
    cfg["cloud_llm"]["mode"] = "shadow"
    old(led)
    def extract(body, **kwargs):
        if kwargs.get("provider"):
            raise cloud_inference.CloudFallback("synthetic_transport")
        return {}
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        assert worker.run_pending(led, budget_s=180)["done"] == 1
    meta = json.loads(led.artifacts("cloud_compare_v1")[0]["meta"])
    assert meta["units"] == 1 and meta["validated_units"] == 0
    assert meta["cloud_facts"] is meta["rejected"] is meta["held"] is None
    assert meta["repairs"] is None
    assert meta["rejection_observations"] == meta["held_observations"] == meta["repair_observations"] == 0
    assert "backend" not in json.loads(led.artifacts(worker.KIND)[0]["meta"])


def test_shadow_comparison_records_observed_unit_denominators(routed):
    led, *_ = routed
    worker._cloud_compare(led, route="reextract", project_id=1,
        local_facts={"a", "b"}, cloud_facts={"b"}, rejected=3, held=1, repairs=2)
    meta = json.loads(led.artifacts("cloud_compare_v1")[0]["meta"])
    assert meta["units"] == meta["validated_units"] == meta["validated"] == 1
    assert meta["matched"] == 1 and meta["local_facts"] == 2
    assert meta["rejected_units"] == meta["rejection_observations"] == 1
    assert meta["held"] == meta["held_observations"] == 1
    assert meta["repaired_units"] == meta["repair_observations"] == 1


def test_shadow_scope_change_during_validation_records_discard(routed, monkeypatch):
    led, cfg, calls, reserved, results = routed
    cfg["cloud_llm"]["mode"] = "shadow"
    old(led)
    def extract(body, **kwargs):
        if provider := kwargs.get("provider"):
            provider("synthetic", deadline=kwargs["deadline"], max_tokens=100)
            cfg["cloud_llm"]["mode"] = "off"
        return {}
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        assert worker.run_pending(led, budget_s=180)["done"] == 1
    assert len(calls) == len(reserved) == len(results) == 1
    assert "backend" not in json.loads(led.artifacts(worker.KIND)[0]["meta"])
    assert len(led.artifacts("cloud_usage_disposition")) == 1
    comparison = json.loads(led.artifacts("cloud_compare_v1")[0]["meta"])
    assert comparison["validated_units"] == 0 and comparison["cloud_facts"] is None


def test_operating_limit_change_while_reserving_still_sends_the_paid_unit(routed, monkeypatch):
    led, cfg, calls, reserves, _ = routed
    old(led)
    original = cloud_policy.usage_reserver
    def factory(*args, **kwargs):
        reserve = original(*args, **kwargs)
        def changed(body, timeout):
            result = reserve(body, timeout)
            cfg["cloud_llm"].update(max_concurrency=3, usd_jpy=140)
            return result
        return changed
    monkeypatch.setattr(cloud_policy, "usage_reserver", factory)
    def extract(body, **kwargs):
        kwargs["provider"]("synthetic", deadline=kwargs["deadline"], max_tokens=100)
        return {}
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        worker.run_pending(led, budget_s=180)
    assert len(calls) == 1 and len(reserves) == 1


def test_each_new_reservation_reads_the_budget_in_force_now(routed, monkeypatch):
    led, cfg, *_ = routed
    seen = []
    monkeypatch.setattr(cloud_policy, "usage_reserver",
                        lambda ledger, config, **identity: lambda body, timeout: seen.append(
                            config["cloud_llm"]["daily_request_budget"]))
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1, unit_key="1",
        deadline=time.monotonic() + 60, current=lambda: True, dispatch=lambda fn, **kw: fn())
    provider.reserve({"model": "x", "max_tokens": 1}, 1)
    cfg["cloud_llm"]["daily_request_budget"] = 0       # lowered while the unit is running
    provider.reserve({"model": "x", "max_tokens": 1}, 1)
    assert seen == [None, 0]


def test_operating_limits_alone_do_not_discard_a_waiting_context_unit():
    base = {"extract": {"x": 1}, "semantic": {"mode": "assist", "until": None},
            "cloud_llm": {"mode": "on", "project_ids": [1], "max_concurrency": 2}}
    edited = {**base, "cloud_llm": {**base["cloud_llm"], "max_concurrency": 4,
                                    "monthly_cost_limit_usd": 5, "usd_jpy": 150, "daily_request_budget": 9,
                                    "window_budget_usd": 10, "window_start": "2026-10-28T00:00:00+09:00",
                                    "until": "2026-10-30T23:59:59+09:00"},
              "semantic": {**base["semantic"], "until": "2026-10-30T23:59:59+09:00"}}
    assert worker._same_but_operating_limits(edited, base)
    assert base["semantic"]["until"] is None
    assert not worker._same_but_operating_limits({**base, "cloud_llm": {**base["cloud_llm"], "project_ids": [2]}}, base)
    assert not worker._same_but_operating_limits({**base, "extract": {"x": 2}}, base)
    assert not worker._same_but_operating_limits({**base, "semantic": {"mode": "off"}}, base)


def test_a_shadow_unit_is_pending_until_measured_and_counted_once(routed):
    led, *_ = routed
    def meta():
        return [json.loads(a["meta"]) for a in led.artifacts("cloud_compare_v1")]
    worker._cloud_compare(led, route="reextract", project_id=1, local_facts={"a"}, unit="u1", pending=True)
    worker._cloud_compare(led, route="reextract", project_id=1, local_facts={"a"}, unit="u1", pending=True)
    assert [(m["uncompared"], m["units"], m["validated_units"]) for m in meta()] == [("pending", 1, 0)]
    worker._cloud_compare(led, route="reextract", project_id=1, local_facts={"a"}, cloud_facts={"a"}, unit="u1")
    assert [("uncompared" in m, m["units"], m["validated_units"], m["matched"]) for m in meta()] == [(False, 1, 1, 1)]
    # a late duplicate (retry, carried pass) never adds a second unit
    worker._cloud_compare(led, route="reextract", project_id=1, local_facts={"a"}, cloud_facts=set(), unit="u1")
    assert len(meta()) == 1 and meta()[0]["matched"] == 1
    worker._cloud_compare(led, route="reextract", project_id=1, local_facts={"a"}, unit="u1", pending=True)
    assert len(meta()) == 1 and meta()[0]["matched"] == 1


@pytest.mark.parametrize("cloud_facts", [None, set(), {"synthetic"}])
def test_uncomparable_local_source_stays_in_the_shadow_unit_denominator(routed, cloud_facts):
    led, *_ = routed
    worker._cloud_compare(led, route="patient_context", project_id=1,
                          local_facts=None, unit="unknown-span", pending=True)
    worker._cloud_compare(led, route="patient_context", project_id=1,
                          local_facts=None, cloud_facts=cloud_facts, unit="unknown-span")
    [artifact] = led.artifacts("cloud_compare_v1")
    meta = json.loads(artifact["meta"])
    assert meta["units"] == 1 and meta["local_facts"] is None and meta["matched"] is None
    assert meta["cloud_facts"] == (None if cloud_facts is None else len(cloud_facts))
    assert meta["validated_units"] == int(cloud_facts is not None)
    assert "uncompared" not in meta


def test_shadow_audit_rows_are_never_written_without_the_run_lock(routed, monkeypatch):
    led, cfg, calls, *_ = routed
    cfg["cloud_llm"]["mode"] = "shadow"
    old(led)
    def committed():
        return any(json.loads(a["meta"]).get("extract_version") == worker.EXTRACT_VERSION
                   for a in led.artifacts(worker.KIND))
    @contextmanager
    def lock(enabled, wait_s=60, *, deadline=None):
        # the result commit gets the lock; the later shadow audit does not
        yield not committed() or not enabled
    monkeypatch.setattr(worker, "_write_lock", lock)
    monkeypatch.setattr(extract_llm_carry, '_write_lock', worker._write_lock)
    monkeypatch.setattr(extract_llm_resident, '_write_lock', worker._write_lock)
    monkeypatch.setattr(extract_llm_runner, '_write_lock', worker._write_lock)
    def extract(body, **kwargs):
        if kwargs.get("provider"):
            kwargs["provider"]("synthetic", deadline=kwargs["deadline"], max_tokens=100)
        return {}
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        report = worker.run_pending(led, budget_s=180, per_write_lock=True)
    assert report["done"] == 1 and len(calls) == 1
    # the pending row was written with the commit, under its lock; the measured
    # result could not be written without the lock, so the unit stays uncompared
    [row] = [json.loads(a["meta"]) for a in led.artifacts("cloud_compare_v1")]
    assert row["uncompared"] == "pending" and row["units"] == 1 and row["validated_units"] == 0


@pytest.mark.parametrize("fail", [False, True])
def test_shadow_pending_row_commits_with_the_local_result_or_not_at_all(routed, monkeypatch, fail):
    led, cfg, *_ = routed
    cfg["cloud_llm"]["mode"] = "shadow"
    old(led)
    row = _bound_source(led)
    row.update(_cloud_mode="shadow", content_hash=_hash(led, 1))
    if fail:
        def boom(*a, **k):
            raise RuntimeError("synthetic failure inside the commit transaction")
        monkeypatch.setattr(worker, "_comparison_extract", boom)
        monkeypatch.setattr(extract_llm_runner, '_comparison_extract', worker._comparison_extract)
        monkeypatch.setattr(extract_llm_store, '_comparison_extract', worker._comparison_extract)
    before = [dict(row) for row in led.artifacts(worker.KIND)]
    if fail:
        with pytest.raises(RuntimeError, match="synthetic failure inside the commit transaction"):
            worker._replace_current(led, row, json.dumps({"medications": []}))
    else:
        assert worker._replace_current(led, row, json.dumps({"medications": []}))
    pending = led.artifacts("cloud_compare_v1")
    current = _current_artifacts(led)
    if fail:
        assert not pending and not current and len(led.artifacts(worker.KIND)) == len(before)
    else:
        assert len(current) == 1 and len(pending) == 1
        _assert_legacy_retained(led, before)
        assert json.loads(pending[0]["meta"])["uncompared"] == "pending" and row["_shadow_unit"]
