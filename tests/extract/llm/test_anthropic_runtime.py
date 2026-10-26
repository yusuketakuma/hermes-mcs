"""Synthetic provider units, owner-thread reservations and local fallback."""
from contextlib import contextmanager
import json
import os
import threading
import time

import pytest

import cloud_policy
import extract_llm as worker
from extract_testkit import _hash, _ledger, _message


@pytest.fixture
def routed(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "max_concurrency": 2}}
    monkeypatch.setattr(worker, "load_config", lambda: cfg)
    monkeypatch.setattr(worker.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(cloud_policy, "api_key", lambda *a: "synthetic-key")
    monkeypatch.setattr(cloud_policy, "block_reason", lambda led, cfg, route, pid:
                        None if cloud_policy.eligible(cfg, route, pid) else "scope")
    monkeypatch.setattr(worker, "disk_free_mb", lambda db: 100000)
    monkeypatch.setattr(worker, "_prefilter_enabled", lambda: False)
    monkeypatch.setattr(worker, "circuit_open_s", lambda db: 0)
    owner = threading.get_ident()
    calls = []
    reserved = []
    results = []
    def reserve_factory(ledger, config, **identity):
        assert threading.get_ident() == owner
        def reserve(body, timeout):
            assert threading.get_ident() == owner
            ledger.db.execute("SELECT 1").fetchone()
            item = {"identity": identity, "n": len(reserved)}
            reserved.append(item)
            return item
        return reserve
    monkeypatch.setattr(cloud_policy, "usage_reserver", reserve_factory)
    def record(ledger, reservation, metadata):
        assert threading.get_ident() == owner
        ledger.db.execute("SELECT 1").fetchone()
        results.append(reservation)
    monkeypatch.setattr(cloud_policy, "record_result", record)
    def disposition(ledger, reservation, reason):
        assert threading.get_ident() == owner and ledger.db.in_transaction
        return ledger.artifact_add_tx("cloud_usage_disposition", "{}",
                                      meta={"attempt": reservation["n"], "disposition": reason})
    monkeypatch.setattr(cloud_policy, "record_disposition_tx", disposition)
    class Client:
        def __init__(self, key):
            assert key == "synthetic-key"
        def complete(self, prompt, *, deadline, max_tokens, before_attempt, reserve_fn, after_result):
            before_attempt()
            reservation = reserve_fn({"prompt": prompt, "max_tokens": max_tokens}, 1)
            before_attempt()
            calls.append(prompt)
            after_result(reservation, {"http_class": "success"})
            return {"status": 200, "finish_reason": "stop", "text": "{}"}
    monkeypatch.setattr(worker.anthropic_client, "AnthropicClient", Client)
    yield led, cfg, calls, reserved, results
    led.close()


def old(led, mid=1, pid=1):
    led.save_messages([_message(mid, "完全合成の記録。", project_id=pid)])
    led.artifact_add(worker.KIND, "{}", project_id=pid, message_id=mid,
        meta={"hash": _hash(led, mid), "extract_version": worker.EXTRACT_VERSION - 1})


@pytest.mark.parametrize("local_circuit", [0, 60])
def test_parallel_http_hooks_stay_on_ledger_owner(routed, monkeypatch, local_circuit):
    led, cfg, calls, reserves, results = routed
    old(led, 1)
    old(led, 2)
    monkeypatch.setattr(worker, "circuit_open_s", lambda db: local_circuit)
    monkeypatch.setattr(worker, "_probe_format", lambda **kw: pytest.fail("cloud probed local GPU"))
    report = worker.run_pending(led, budget_s=180, workers=1, batch_k=4)
    assert report["done"] == 2 and report["failed"] == 0
    assert len(calls) == len(reserves) == len(results) == 2
    assert {r["identity"]["unit_key"] for r in reserves} == {"1", "2"}
    for artifact in led.artifacts(worker.KIND):
        meta = json.loads(artifact["meta"])
        assert meta["backend"] == "anthropic"
        assert meta["actual_model"] == cloud_policy.cloud_config(cfg)[0]["model"]
        assert meta["prompt"] == worker._cloud_contract(cfg)["prompt"] and "code_contract" not in meta
        assert meta["config_generation"] == cloud_policy.config_generation({"cloud_llm": worker._cloud_settings(cfg)})
    assert not led.artifacts("extract_llm_chunk")


def test_missing_key_and_scope_leave_local(routed, monkeypatch):
    led, cfg, *_ = routed
    assert worker._cloud_mode(led, cfg, "reextract", 2) == "off"
    monkeypatch.setattr(cloud_policy, "api_key", lambda *a: None)
    assert worker._cloud_mode(led, cfg, "reextract", 1) == "off"


@pytest.mark.parametrize("flag", ["qc_src", "thin_src", "human_src", "attempts"])
def test_old_version_quality_error_precedence_stays_local(routed, flag):
    led, *_ = routed
    old(led)
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row.update(qc_src=None, thin_src=None, human_src=None, attempts=0)
    assert worker._cloud_reextract(led, row)
    row[flag] = 1
    assert not worker._cloud_reextract(led, row)


def test_new_post_and_edited_body_are_not_version_reextract(routed):
    led, *_ = routed
    old(led)
    led.db.execute("UPDATE messages SET content_hash='changed' WHERE message_id=1")
    led.db.commit()
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row.update(qc_src=None, thin_src=None, human_src=None, attempts=0)
    assert not worker._cloud_reextract(led, row)


@pytest.mark.parametrize("change", ["scope", "stop", "mode", "daily_cap", "monthly_cap"])
def test_scope_change_while_reserving_never_starts_http(routed, monkeypatch, change):
    led, cfg, calls, reserves, _ = routed
    old(led)
    original = cloud_policy.usage_reserver
    def factory(*args, **kwargs):
        reserve = original(*args, **kwargs)
        def changed(body, timeout):
            result = reserve(body, timeout)
            if change == "scope":
                cfg["cloud_llm"]["exclude_project_ids"] = [1]
            elif change == "stop":
                monkeypatch.setattr(cloud_policy, "block_reason", lambda *args: "paused")
            elif change == "daily_cap":      # lowered between reservation and the send
                cfg["cloud_llm"]["daily_request_budget"] = 0
            elif change == "monthly_cap":
                cfg["cloud_llm"]["monthly_cost_limit_usd"] = 0
            else:
                cfg["cloud_llm"]["mode"] = "shadow"
            return result
        return changed
    monkeypatch.setattr(cloud_policy, "usage_reserver", factory)
    local = []
    def extract(body, **kwargs):
        if kwargs.get("provider"):
            kwargs["provider"]("synthetic", deadline=kwargs["deadline"], max_tokens=100)
        else:
            local.append(kwargs)
        return {}
    monkeypatch.setattr(worker, "llm_extract", extract)
    report = worker.run_pending(led, budget_s=180)
    assert report["done"] == 1 and not calls and len(reserves) == 1
    assert not led.artifacts("cloud_usage_disposition")
    assert len(local) == 1
    artifact = led.artifacts(worker.KIND)[0]
    assert "backend" not in json.loads(artifact["meta"])


def test_injected_call_keeps_existing_json_validation(routed):
    led, cfg, *_ = routed
    provider = worker._CloudProvider(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kw: fn())
    assert worker.llm_extract("合成の記録", deadline=time.monotonic() + 180, provider=provider) == {}


def test_cloud_failure_restarts_whole_post_with_local_checkpoint_contract(routed, monkeypatch):
    led, cfg, calls, *_ = routed
    old(led)
    trace = []
    def extract(body, **kwargs):
        trace.append((kwargs.get("provider") is not None, kwargs["chunks_in"].copy()))
        if kwargs.get("provider"):
            kwargs["chunks_out"][0] = {"summary": "synthetic-cloud"}
            raise worker.CloudFallback("synthetic_failure")
        assert not kwargs["chunks_in"]
        return {}
    monkeypatch.setattr(worker, "llm_extract", extract)
    result = worker.run_pending(led, budget_s=180)
    assert result["done"] == 1 and trace == [(True, {}), (False, {})]
    assert "backend" not in json.loads(led.artifacts(worker.KIND)[0]["meta"])
    assert not calls


def test_active_hold_stays_local_until_exact_upgrade_contract_changes(routed, monkeypatch):
    led, *_ = routed
    old(led)
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row.update(qc_src=None, thin_src=None, human_src=None, attempts=0)
    prior = worker._prior_extract(led.db, row)
    with led.db:
        worker._record_hold_tx(led, row, prior, "protected_loss")
    assert not worker._cloud_reextract(led, row)
    # The existing receipt selector, rather than a new release flag, decides re-entry.
    monkeypatch.setattr(worker, "_upgrade_contract", lambda: "synthetic-next-contract")
    assert worker._cloud_reextract(led, row)


def test_same_source_is_checked_again_after_durable_reservation(routed, monkeypatch):
    led, cfg, calls, reserved, _ = routed
    valid = [True]
    original = cloud_policy.usage_reserver
    def factory(*args, **kwargs):
        reserve = original(*args, **kwargs)
        def changed(body, timeout):
            result = reserve(body, timeout)
            valid[0] = False
            return result
        return changed
    monkeypatch.setattr(cloud_policy, "usage_reserver", factory)
    provider = worker._CloudProvider(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 180,
        current=lambda: valid[0], dispatch=lambda fn, **kw: fn())
    with pytest.raises(worker.CloudFallback):
        provider("synthetic", deadline=time.monotonic() + 180, max_tokens=100)
    assert len(reserved) == 1 and not calls
    assert not led.artifacts("cloud_usage_disposition") and not led.db.in_transaction


def test_cloud_is_single_while_local_batch_structure_is_preserved():
    def item(index, mode):
        row = {"_cloud_mode": mode, "_chunk_size": worker._CHUNK_SIZE,
               "body_text": "synthetic", "thin_src": None}
        return (index, row, None, {}, {}, None, 1)
    claimed = [item(1, "off"), item(2, "off"), item(3, "on"),
               item(4, "off"), item(5, "off")]
    units = worker._plan_units(claimed, 2)
    assert [(kind, [t[0] for t in payload] if kind == "batch" else payload[0])
            for kind, payload in units] == [("batch", [1, 2]), ("single", 3), ("batch", [4, 5])]


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
    report = worker.run_pending(led, budget_s=180)
    assert report["done"] == 1 and phases == ["local", "cloud"] and len(calls) == 1
    assert "backend" not in json.loads(led.artifacts(worker.KIND)[0]["meta"])
    assert len(led.artifacts("cloud_compare_v1")) == 1


def test_commit_rechecks_cloud_permission_after_writer_transaction_starts(routed):
    led, *_ = routed
    old(led)
    before = [dict(row) for row in led.artifacts(worker.KIND)]
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    checked = []
    def revoked():
        assert led.db.in_transaction
        checked.append(True)
        raise worker.CloudFallback("scope_changed")
    row["_cloud_guard"] = revoked
    assert worker._replace_current(led, row, "{}") is False
    assert checked == [True]
    assert [dict(item) for item in led.artifacts(worker.KIND)] == before
    assert not led.db.in_transaction


@pytest.mark.parametrize("change", [{"monthly_cost_limit_usd": 9}, {"usd_jpy": 140},
    {"daily_request_budget": 10}, {"max_concurrency": 3}, {"exclude_project_ids": [2]}])
def test_operating_limits_do_not_change_saved_provider_contract(routed, change):
    _, cfg, *_ = routed
    before = worker._cloud_contract(cfg)
    cfg["cloud_llm"].update(change)
    assert worker._cloud_contract(cfg) == before
    assert "prompt" in before and "code_contract" not in before and "config_generation" not in before


def test_cloud_and_shadow_leave_legacy_integrity_notes_untouched(routed):
    led, cfg, *_ = routed
    provider = worker._CloudProvider(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kw: fn())
    notes = worker._note_list()
    prior = {"synthetic": "local"}
    notes[:] = [prior]
    assert worker.llm_extract("合成の記録", deadline=time.monotonic() + 180, provider=provider) == {}
    assert notes == [prior]
    notes.clear()


@pytest.mark.parametrize("revoke", ["none", "scope", "source", "updater", "key"])
def test_paid_cloud_carry_survives_deadline_and_lease_renewal_but_not_revocation(
        routed, monkeypatch, tmp_path, revoke):
    led, cfg, calls, *_ = routed
    old(led)
    blocked = [False]
    def acquire():
        return None if blocked[0] else os.open(tmp_path / "writer", os.O_CREAT | os.O_RDWR)
    original = worker.llm_extract
    def extract(*args, **kwargs):
        value = original(*args, **kwargs)
        blocked[0] = True
        return value
    monkeypatch.setattr(worker, "acquire_run_lock", acquire)
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
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
    assert len(artifacts) == 1
    assert json.loads(artifacts[0]["meta"])["extract_version"] == (
        worker.EXTRACT_VERSION if revoke == "none" else worker.EXTRACT_VERSION - 1)


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
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
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
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
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
        assert json.loads(led.artifacts(worker.KIND)[0]["meta"])["extract_version"] == worker.EXTRACT_VERSION
    assert not led.db.in_transaction



@pytest.mark.parametrize("parallel", [False, True])
def test_validated_cloud_unit_does_not_expire_before_commit(routed, monkeypatch, parallel):
    led, _, calls, *_ = routed
    old(led)
    if parallel:
        old(led, 2)
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
    report = worker.run_pending(led, budget_s=180)
    count = 2 if parallel else 1
    assert report["done"] == count and len(calls) == count
    assert all(json.loads(item["meta"])["backend"] == "anthropic" for item in led.artifacts(worker.KIND))



def test_completed_http_uses_result_dispatch_and_never_labels_only_deadline_as_scope(routed, monkeypatch):
    led, cfg, calls, _, _ = routed
    clock = time.monotonic
    outcomes, dispatches = [], []
    def complete(self, prompt, *, deadline, max_tokens, before_attempt, reserve_fn, after_result):
        before_attempt()
        reservation = reserve_fn({"model": cloud_policy.MODEL, "max_tokens": max_tokens}, 1)
        before_attempt()
        calls.append(prompt)
        assert clock() < deadline
        # The HTTP response completed on time, then its owner callback waited.
        monkeypatch.setattr(worker.time, "monotonic", lambda: clock() + 200)
        after_result(reservation, {"http_class": "success"})
        return {"status": 200, "finish_reason": "stop", "text": "{}"}
    monkeypatch.setattr(worker.anthropic_client.AnthropicClient, "complete", complete)
    monkeypatch.setattr(cloud_policy, "record_result", lambda ledger, reservation, metadata: outcomes.append(metadata))
    def dispatch(fn, *, result=False):
        dispatches.append(result)
        return fn()
    provider = worker._CloudProvider(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=clock() + 180, current=lambda: True, dispatch=dispatch)
    assert provider("synthetic", deadline=clock() + 180, max_tokens=100)["text"] == "{}"
    assert outcomes == [{"http_class": "success"}]
    assert dispatches[-2:] == [True, True]
    with pytest.raises(worker.CloudFallback):
        provider("new-send", deadline=clock() + 180, max_tokens=100)
    assert calls == ["synthetic"]


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
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    result = worker.run_pending(led, budget_s=180, per_write_lock=True, write_wait_s=1, carry=[])
    assert len(result["carry"]) == 1 and len(calls) == len(reserves) == len(results) == 1
    cfg["cloud_llm"]["mode"] = "off"
    blocked[0] = False
    worker._settle_carry(led, result["carry"])
    assert len(calls) == len(reserves) == len(results) == 1
    assert json.loads(led.artifacts("cloud_usage_disposition")[0]["meta"])["disposition"] == "discarded_scope"
    assert json.loads(led.artifacts(worker.KIND)[0]["meta"])["extract_version"] < worker.EXTRACT_VERSION



def test_failed_shadow_attempt_keeps_unit_and_unknown_quality_denominators(routed, monkeypatch):
    led, cfg, _, *_ = routed
    cfg["cloud_llm"]["mode"] = "shadow"
    old(led)
    def extract(body, **kwargs):
        if kwargs.get("provider"):
            raise worker.CloudFallback("synthetic_transport")
        return {}
    monkeypatch.setattr(worker, "llm_extract", extract)
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



@pytest.mark.parametrize("change_at", ["response_callback", "validated_unit"])
def test_first_scope_rejection_after_http_audits_once_and_keeps_local_fallback(routed, monkeypatch, change_at):
    led, cfg, calls, reserved, results = routed
    old(led)
    local, outcomes = [], []
    record = cloud_policy.record_result
    def recorded(ledger, reservation, metadata):
        outcomes.append(dict(metadata))
        return record(ledger, reservation, metadata)
    monkeypatch.setattr(cloud_policy, "record_result", recorded)
    original_complete = worker.anthropic_client.AnthropicClient.complete
    def complete(self, *args, **kwargs):
        value = original_complete(self, *args, **kwargs)
        if change_at == "response_callback":
            cfg["cloud_llm"]["mode"] = "off"
        return value
    monkeypatch.setattr(worker.anthropic_client.AnthropicClient, "complete", complete)
    original_extract = worker.llm_extract
    def extract(body, **kwargs):
        if kwargs.get("provider"):
            value = original_extract(body, **kwargs)
            if change_at == "validated_unit":
                cfg["cloud_llm"]["mode"] = "off"
            return value
        local.append(kwargs)
        return {}
    monkeypatch.setattr(worker, "llm_extract", extract)
    report = worker.run_pending(led, budget_s=180)
    assert report["done"] == 1 and len(local) == 1
    assert len(calls) == len(reserved) == len(results) == 1
    assert outcomes == [{"http_class": "success"}]
    annotations = led.artifacts("cloud_usage_disposition")
    assert len(annotations) == 1
    assert json.loads(annotations[0]["meta"])["disposition"] == "discarded_scope"
    meta = json.loads(led.artifacts(worker.KIND)[0]["meta"])
    assert "backend" not in meta and "config_generation" not in meta


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
    provider = worker._CloudProvider(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=unit_deadline, current=lambda: True,
        dispatch=lambda fn, **kwargs: fn())
    assert provider("synthetic", deadline=repair_deadline, max_tokens=100)["text"] == "{}"
    assert seen == [repair_deadline - worker.patient_context_refresh.COMMIT_MARGIN]
    assert provider.deadline == unit_deadline and len(reserved) == len(results) == 1


@pytest.mark.parametrize("remaining", [0, 4])
def test_commit_margin_exhaustion_never_reserves_or_sends(routed, remaining):
    led, cfg, calls, reserved, results = routed
    deadline = time.monotonic() + remaining
    provider = worker._CloudProvider(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=deadline, current=lambda: True,
        dispatch=lambda fn, **kwargs: fn())
    with pytest.raises(worker.CloudFallback, match="cloud_unit_deadline"):
        provider("synthetic", deadline=deadline, max_tokens=100)
    assert not calls and not reserved and not results
    assert not led.artifacts("cloud_usage_disposition") and not led.db.in_transaction


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
    assert worker.run_pending(led, budget_s=180)["done"] == 1
    assert len(calls) == len(reserved) == len(results) == 1
    assert "backend" not in json.loads(led.artifacts(worker.KIND)[0]["meta"])
    assert len(led.artifacts("cloud_usage_disposition")) == 1
    comparison = json.loads(led.artifacts("cloud_compare_v1")[0]["meta"])
    assert comparison["validated_units"] == 0 and comparison["cloud_facts"] is None


@pytest.mark.parametrize("loss,final_lock", [("source", True), ("source", False),
    ("lease", True), ("lease", False), ("none", False)])
def test_pending_write_loss_records_discard_but_lock_wait_does_not(routed, monkeypatch, loss, final_lock):
    led, _, calls, reserved, results = routed
    old(led)
    locks = []
    @contextmanager
    def locked(enabled, **kwargs):
        locks.append(enabled)
        if len(locks) == 3:
            if loss == "source":
                led.db.execute("UPDATE messages SET content_hash='changed' WHERE message_id=1")
            elif loss == "lease":
                led.db.execute("DELETE FROM fetch_jobs WHERE kind='extract_claim'")
            led.db.commit()
        # GC acquires first; the result write waits, then the final flush rechecks.
        yield len(locks) == 1 or len(locks) == 3 and final_lock
    monkeypatch.setattr(worker, "_write_lock", locked)
    report = worker.run_pending(led, budget_s=180, per_write_lock=True, write_wait_s=1, carry=[])
    assert report["done"] == 0 and len(locks) == 3
    assert len(calls) == len(reserved) == len(results) == 1
    assert len(led.artifacts("cloud_usage_disposition")) == int(loss != "none")
    assert len(report["carry"]) == int(loss == "none")
    if report["carry"]:
        worker._settle_carry(led, report["carry"], final=True)
        assert not report["carry"] and not led.artifacts("cloud_usage_disposition")
    assert json.loads(led.artifacts(worker.KIND)[0]["meta"])["extract_version"] < worker.EXTRACT_VERSION


def test_cloud_contract_follows_rule_versions_not_code_releases(routed, monkeypatch):
    _, cfg, *_ = routed
    before = worker._cloud_contract(cfg)
    monkeypatch.setattr(worker, "_upgrade_contract", lambda: "synthetic-release")
    assert worker._cloud_contract(cfg) == before


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
    worker.run_pending(led, budget_s=180)
    assert len(calls) == 1 and len(reserves) == 1


def test_each_new_reservation_reads_the_budget_in_force_now(routed, monkeypatch):
    led, cfg, *_ = routed
    seen = []
    monkeypatch.setattr(cloud_policy, "usage_reserver",
                        lambda ledger, config, **identity: lambda body, timeout: seen.append(
                            config["cloud_llm"]["daily_request_budget"]))
    provider = worker._CloudProvider(led, cfg, route="reextract", project_id=1, unit_key="1",
        deadline=time.monotonic() + 60, current=lambda: True, dispatch=lambda fn, **kw: fn())
    provider.reserve({"model": "x", "max_tokens": 1}, 1)
    cfg["cloud_llm"]["daily_request_budget"] = 0       # lowered while the unit is running
    provider.reserve({"model": "x", "max_tokens": 1}, 1)
    assert seen == [None, 0]


def test_operating_limits_alone_do_not_discard_a_waiting_context_unit():
    base = {"extract": {"x": 1}, "cloud_llm": {"mode": "on", "project_ids": [1], "max_concurrency": 2}}
    edited = {**base, "cloud_llm": {**base["cloud_llm"], "max_concurrency": 4,
                                    "monthly_cost_limit_usd": 5, "usd_jpy": 150, "daily_request_budget": 9}}
    assert worker._same_but_operating_limits(edited, base)
    assert not worker._same_but_operating_limits({**base, "cloud_llm": {**base["cloud_llm"], "project_ids": [2]}}, base)
    assert not worker._same_but_operating_limits({**base, "extract": {"x": 2}}, base)


def test_a_shadow_unit_is_pending_until_measured_and_counted_once(routed):
    led, *_ = routed
    def meta():
        return [json.loads(a["meta"]) for a in led.artifacts("cloud_compare_v1")]
    worker._cloud_compare(led, route="reextract", project_id=1, local_facts={"a"}, unit="u1", pending=True)
    assert [(m["uncompared"], m["units"], m["validated_units"]) for m in meta()] == [("pending", 1, 0)]
    worker._cloud_compare(led, route="reextract", project_id=1, local_facts={"a"}, cloud_facts={"a"}, unit="u1")
    assert [("uncompared" in m, m["units"], m["validated_units"], m["matched"]) for m in meta()] == [(False, 1, 1, 1)]
    # a late duplicate (retry, carried pass) never adds a second unit
    worker._cloud_compare(led, route="reextract", project_id=1, local_facts={"a"}, cloud_facts=set(), unit="u1")
    assert len(meta()) == 1 and meta()[0]["matched"] == 1


def test_shadow_audit_rows_are_never_written_without_the_run_lock(routed, monkeypatch):
    led, cfg, calls, *_ = routed
    cfg["cloud_llm"]["mode"] = "shadow"
    old(led)
    def committed():
        return any(json.loads(a["meta"]).get("extract_version") == worker.EXTRACT_VERSION
                   for a in led.artifacts(worker.KIND))
    @contextmanager
    def lock(enabled, wait_s=60):
        # the result commit gets the lock; the later shadow audit does not
        yield not committed() or not enabled
    monkeypatch.setattr(worker, "_write_lock", lock)
    def extract(body, **kwargs):
        if kwargs.get("provider"):
            kwargs["provider"]("synthetic", deadline=kwargs["deadline"], max_tokens=100)
        return {}
    monkeypatch.setattr(worker, "llm_extract", extract)
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
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row.update(_cloud_mode="shadow", content_hash=_hash(led, 1))
    if fail:
        def boom(*a, **k):
            raise RuntimeError("synthetic failure inside the commit transaction")
        monkeypatch.setattr(worker, "_comparison_extract", boom)
    before = led.artifacts(worker.KIND)
    try:
        worker._replace_current(led, row, json.dumps({"medications": []}))
    except RuntimeError:
        assert fail
    pending = led.artifacts("cloud_compare_v1")
    current = [a for a in led.artifacts(worker.KIND) if json.loads(a["meta"]).get("extract_version") == worker.EXTRACT_VERSION]
    if fail:
        assert not pending and not current and len(led.artifacts(worker.KIND)) == len(before)
    else:
        assert len(current) == 1 and len(pending) == 1
        assert json.loads(pending[0]["meta"])["uncompared"] == "pending" and row["_shadow_unit"]
