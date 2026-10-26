"""Synthetic provider units, owner-thread reservations and local fallback."""
import json
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


def test_scope_change_while_reserving_never_starts_http(routed, monkeypatch):
    led, cfg, calls, reserves, _ = routed
    old(led)
    original = cloud_policy.usage_reserver
    def factory(*args, **kwargs):
        reserve = original(*args, **kwargs)
        def changed(body, timeout):
            result = reserve(body, timeout)
            cfg["cloud_llm"]["exclude_project_ids"] = [1]
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
