"""Synthetic anthropic runtime contracts with isolated shared fixtures."""
import extract_llm_admission
import extract_llm_carry
import extract_llm_context
import extract_llm_engine
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
import time
from datetime import datetime
import pytest
from anthropic_client import AnthropicClient as AnthropicClient
import cloud_policy
from cloud_policy import record_result as real_record_result  # noqa: F401 - public synthetic accounting helper
from cloud_policy import usage_reserver as real_usage_reserver  # noqa: F401 - public synthetic accounting helper
import extract_llm as worker
import cloud_inference
import inference_version as inference
import mcs_util
from extract_testkit import _ledger, _message
from anthropic_runtime_testkit import _assert_legacy_retained as _assert_legacy_retained, _bound_source as _bound_source, _current_artifacts as _current_artifacts, _use_real_cloud_accounting as _use_real_cloud_accounting, old as old, routed as routed
from test_cloud_response_recovery import legacy_cloud_routes


@pytest.mark.parametrize(("route", "purpose", "model", "effort", "tokens"), [
    ("reextract", "backlog", "gpt-6-luna", "medium", 8192),
    ("patient_context", "patient_context", "gpt-6.1-sol", "medium", 16384),
    ("patient_context", "quality_repair", "gpt-6.1-sol", "high", 32768),
])
def test_cloud_unit_pins_profile_through_body_and_reservation(routed, monkeypatch, route, purpose, model, effort, tokens):
    from contextlib import nullcontext
    led, cfg, _calls, reserved, _results = routed
    cfg["cloud_llm"]["provider"] = "openai"
    monkeypatch.setattr(cloud_policy, "provider_key", lambda provider, root=None: "synthetic-key")
    monkeypatch.setattr(worker.cloud_backlog, "request_slot", lambda *_args: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, "owner_alive", lambda: True)
    received = []
    original = worker.openai_client.OpenAIClient
    class Client(original):
        def request(self, method, body, timeout, **kwargs):
            received.append(body)
            return 200, {}, json.dumps({"status": "completed", "model": model, "service_tier": "default",
                "output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": '{}'}]}],
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3,
                          "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}}).encode()
    monkeypatch.setattr(worker.openai_client, "OpenAIClient", Client)
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route=route, purpose=purpose, project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 120,
        current=lambda: True, dispatch=lambda fn, **_kwargs: fn())
    with legacy_cloud_routes():
        response = provider("synthetic prompt", deadline=time.monotonic() + 120, max_tokens=1600)
    assert response["status"] == 200 and len(received) == len(reserved) == 1
    assert received[0]["model"] == model and received[0]["reasoning"] == {"effort": effort}
    assert received[0]["max_output_tokens"] == tokens
    assert dict(reserved[0]["identity"]["selection"]) == dict(provider.selection)
    assert provider.contract == cloud_inference.cloud_provider_contract(selection=provider.selection)


@pytest.mark.parametrize("local_circuit", [0, 60])
@pytest.mark.parametrize("background", [False, True])
def test_parallel_http_hooks_stay_on_ledger_owner(routed, monkeypatch, local_circuit, background):
    led, cfg, calls, reserves, results = routed
    if background:
        led.save_messages([_message(mid, "完全合成の過去記録。", project_id=1) for mid in (1, 2)])
        with led.db:
            led.db.execute("UPDATE messages SET first_seen=?", (time.time() - 601,))
            led.db.execute("UPDATE message_revisions SET observed_at=?", (time.time() - 601,))
            cloud_policy.capture_history_tx(led.db, "synthetic-resume", time.time())
    else:
        old(led, 1)
        old(led, 2)
    prior = [dict(row) for row in led.artifacts(worker.KIND)]
    monkeypatch.setattr(worker, "circuit_open_s", lambda db: local_circuit)
    monkeypatch.setattr(extract_llm_resident, 'circuit_open_s', worker.circuit_open_s)
    monkeypatch.setattr(extract_llm_runner, 'circuit_open_s', worker.circuit_open_s)
    monkeypatch.setattr(worker, "_probe_format", lambda **kw: pytest.fail("cloud probed local GPU"))
    monkeypatch.setattr(extract_llm_provider, '_probe_format', worker._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', worker._probe_format)
    with legacy_cloud_routes():
        report = worker.run_pending(led, budget_s=180, workers=1, batch_k=4, background=background)
    assert report["done"] == 2 and report["failed"] == 0
    assert len(calls) == len(reserves) == len(results) == 2
    assert {r["identity"]["unit_key"] for r in reserves} == {"1", "2"}
    current = _current_artifacts(led)
    assert len(current) == 2
    if not background:
        _assert_legacy_retained(led, prior)
    for artifact in current:
        meta = json.loads(artifact["meta"])
        assert meta["backend"] == "anthropic"
        assert meta["actual_model"] == cloud_policy.cloud_config(cfg)[0]["model"]
        assert meta["prompt"] == cloud_inference.cloud_provider_contract()["prompt"] and "code_contract" not in meta
        assert meta["config_generation"] == cloud_policy.config_generation({"cloud_llm": cloud_inference.resolve_cloud_settings(cfg)})
    assert not led.artifacts("extract_llm_chunk")


def test_missing_key_and_scope_leave_local(routed, monkeypatch):
    led, cfg, *_ = routed
    assert worker._cloud_mode(led, cfg, "patient_context", 2) == "off"
    monkeypatch.setattr(cloud_policy, "api_key", lambda *a: None)
    assert worker._cloud_mode(led, cfg, "patient_context", 1) == "off"


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


def test_background_without_prior_result_uses_cloud_but_recent_arrival_does_not(routed):
    led, *_ = routed
    led.save_messages([_message(1, "完全合成の過去記録。", project_id=1)])
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row.update(qc_src=None, thin_src=None, human_src=None, attempts=0)
    assert not worker._cloud_reextract(led, row)
    assert not worker._cloud_reextract(led, row, background=True)
    with led.db:
        led.db.execute("UPDATE messages SET first_seen=?", (time.time() - 601,))
        led.db.execute("UPDATE message_revisions SET observed_at=?", (time.time() - 601,))
        cloud_policy.capture_history_tx(led.db, "synthetic-resume", time.time())
    assert worker._cloud_reextract(led, row, background=True)
    with led.db:
        led.db.execute("UPDATE messages SET notified_at=? WHERE message_id=1", (time.time() - 3600,))
    assert worker._cloud_reextract(led, row, background=True)
    with led.db:
        led.db.execute("UPDATE messages SET notified_at=? WHERE message_id=1", (time.time(),))
    assert not worker._cloud_reextract(led, row, background=True)


def test_explicit_local_only_never_selects_provider(routed, monkeypatch):
    led, *_ = routed
    old(led)
    with led.db:
        led.db.execute("UPDATE messages SET first_seen=?", (time.time() - 601,))
        led.db.execute("UPDATE message_revisions SET observed_at=?", (time.time() - 601,))
        cloud_policy.capture_history_tx(led.db, "synthetic-resume", time.time())
    original = worker._prepare_jobs
    modes = []

    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        modes.extend(row["_cloud_mode"] for row, *_ in result[0])
        return result

    monkeypatch.setattr(worker, "_prepare_jobs", prepare)
    monkeypatch.setattr(extract_llm_runner, '_prepare_jobs', worker._prepare_jobs)
    monkeypatch.setattr(worker, "llm_extract", lambda *a, **kw: {})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    worker.run_pending(led, allow_cloud=False, background=True, budget_s=180)
    assert modes and modes == ["off"]
    assert not routed[2]


@pytest.mark.parametrize("change", ["scope", "stop", "mode", "daily_cap", "monthly_cap", "window_start"])
def test_scope_change_while_reserving_never_starts_http(routed, monkeypatch, change):
    led, cfg, calls, reserves, _ = routed
    old(led)
    def ago(days):
        return datetime.fromtimestamp(time.time() - days * 86400, cloud_policy.JST).replace(microsecond=0).isoformat()
    if change == "window_start":
        cfg["cloud_llm"].update(window_start=ago(1), window_budget_usd=100)
    original = cloud_policy.usage_reserver
    def factory(*args, **kwargs):
        reserve = original(*args, **kwargs)
        def changed(body, timeout):
            result = reserve(body, timeout)
            if change == "scope":
                cfg["cloud_llm"]["exclude_project_ids"] = [1]
            elif change == "stop":
                monkeypatch.setattr(cloud_policy, "block_reason", lambda *args, **kwargs: "paused")
            elif change == "daily_cap":      # lowered between reservation and the send
                cfg["cloud_llm"]["daily_request_budget"] = 0
            elif change == "monthly_cap":
                cfg["cloud_llm"]["monthly_cost_limit_usd"] = 0
            elif change == "window_start":   # an earlier start counts more spending
                cfg["cloud_llm"]["window_start"] = ago(30)
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
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        report = worker.run_pending(led, budget_s=180)
    waits = change in ('stop', 'daily_cap', 'monthly_cap', 'window_start')
    assert report["done"] == int(not waits) and not calls and len(reserves) == 1
    assert report['deferred'] == int(waits)
    if waits:
        assert report['defer_reasons'] == {'cloud_guard': 1}
    assert not led.artifacts("cloud_usage_disposition")
    assert len(local) == int(not waits)
    artifact = led.artifacts(worker.KIND)[0]
    assert "backend" not in json.loads(artifact["meta"])


def test_injected_call_keeps_existing_json_validation(routed):
    led, cfg, *_ = routed
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="patient_context", project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kw: fn())
    assert worker.llm_extract("合成の記録", deadline=time.monotonic() + 180, provider=provider) == {"symptoms": []}


def test_openai_uses_selected_key_model_and_contract(routed, monkeypatch):
    led, cfg, *_ = routed
    cfg["cloud_llm"]["provider"] = "openai"
    monkeypatch.setattr(cloud_policy, "provider_key", lambda name, root=None: "synthetic-openai" if name == "openai" else None)
    seen = []

    original_client = worker.openai_client.OpenAIClient
    class Client(original_client):
        def __init__(self, key, *, model, effort):
            assert key == "synthetic-openai"
            assert (model, effort) == ("gpt-6.1-sol", "medium")
            super().__init__(key, model=model, effort=effort)

        def complete(self, prompt, **kwargs):
            kwargs["before_attempt"]()
            seen.append(True)
            return {"status": 200, "text": '{"symptoms":[]}', "finish_reason": "stop", "model": self.model}

    monkeypatch.setattr(worker.openai_client, "OpenAIClient", Client)
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="patient_context", project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kw: fn())
    assert provider.model == provider.contract["model"] == "gpt-6.1-sol"
    assert provider.contract["backend"] == "openai"
    assert worker.llm_extract("合成の記録", deadline=time.monotonic() + 180, provider=provider) == {"symptoms": []}
    assert seen == [True]


def test_cloud_failure_restarts_whole_post_with_local_checkpoint_contract(routed, monkeypatch):
    led, cfg, calls, *_ = routed
    old(led)
    trace = []
    def extract(body, **kwargs):
        trace.append((kwargs.get("provider") is not None, kwargs["chunks_in"].copy()))
        if kwargs.get("provider"):
            kwargs["chunks_out"][0] = {"summary": "synthetic-cloud"}
            raise cloud_inference.CloudFallback("synthetic_failure")
        assert not kwargs["chunks_in"]
        return {}
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        result = worker.run_pending(led, budget_s=180)
    assert result["done"] == 1 and trace == [(True, {}), (False, {})]
    assert "backend" not in json.loads(led.artifacts(worker.KIND)[0]["meta"])
    assert not calls


@pytest.mark.parametrize('known', [False, True])
def test_cloud_fallback_requires_a_known_attempt_outcome(routed, monkeypatch, known):
    led, _, calls, *_ = routed
    old(led)
    local = []
    def infer(_body, **kwargs):
        provider = kwargs.get('provider')
        if provider is not None:
            if known:
                provider('synthetic complete request', deadline=kwargs['deadline'], max_tokens=100)
            else:
                provider.possibly_sent = True
            raise cloud_inference.CloudFallback('synthetic_failed_unit')
        local.append(True)
        return {}
    monkeypatch.setattr(worker, 'llm_extract', infer)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        report = worker.run_pending(led, budget_s=180)
    assert report['done'] == int(known) and len(local) == int(known)
    assert len(calls) == int(known)
    if not known:
        assert report['defer_reasons']['cloud_unit_unresolved'] == 1
        assert not _current_artifacts(led)


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
    monkeypatch.setattr(extract_llm_qc, '_upgrade_contract', worker._upgrade_contract)
    monkeypatch.setattr(extract_llm_store, '_upgrade_contract', worker._upgrade_contract)
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
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="patient_context", project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 180,
        current=lambda: valid[0], dispatch=lambda fn, **kw: fn())
    with pytest.raises(cloud_inference.CloudFallback):
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


def test_commit_rechecks_cloud_permission_after_writer_transaction_starts(routed):
    led, *_ = routed
    old(led)
    before = [dict(row) for row in led.artifacts(worker.KIND)]
    row = _bound_source(led)
    checked = []
    def revoked():
        assert led.db.in_transaction
        checked.append(True)
        raise cloud_inference.CloudFallback("scope_changed")
    row["_cloud_guard"] = revoked
    assert worker._replace_current(led, row, "{}") is False
    assert checked == [True]
    assert [dict(item) for item in led.artifacts(worker.KIND)] == before
    assert not led.db.in_transaction


def test_saved_provider_contract_is_the_fixed_request_shape_and_a_fresh_copy():
    # operating limits, rule versions and code releases are not part of it
    before = cloud_inference.cloud_provider_contract()
    assert before == {"backend": "anthropic", "model": worker.anthropic_client.MODEL, "prompt": before["prompt"]}
    assert len(before["prompt"]) == 16 and "code_contract" not in before and "config_generation" not in before
    before["model"] = "edited"
    assert cloud_inference.cloud_provider_contract()["model"] == worker.anthropic_client.MODEL


def test_cloud_call_resets_prior_unit_integrity_and_records_once(routed):
    led, cfg, calls, reservations, results = routed
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="patient_context", project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kw: fn())
    notes = worker._note_list()
    prior = {"synthetic": "local"}
    notes[:] = [prior]
    meta = {}
    assert worker.llm_extract("合成の記録", deadline=time.monotonic() + 180,
                              provider=provider, meta_out=meta) == {"symptoms": []}
    assert len(notes) == 1 and notes[0] is not prior
    assert notes[0]["status"] == 200 and notes[0]["finish_reason"] == "stop"
    assert meta["calls"] == meta["first_calls"] == 1 and meta["repair_calls"] == meta["length_stops"] == 0
    assert len(calls) == len(reservations) == len(results) == 1
    notes.clear()


@pytest.mark.parametrize("change_at", ["response_callback", "validated_unit"])
def test_first_scope_rejection_after_http_audits_once_and_keeps_local_fallback(routed, monkeypatch, change_at):
    led, cfg, calls, reserved, results = routed
    old(led)
    local, outcomes = [], []
    record = cloud_policy.record_result
    def recorded(ledger, reservation, metadata, **answer):
        outcomes.append(dict(metadata))
        return record(ledger, reservation, metadata, **answer)
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
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        report = worker.run_pending(led, budget_s=180)
    assert report["done"] == 1 and len(local) == 1
    assert len(calls) == len(reserved) == len(results) == 1
    assert outcomes == [{"http_class": "http_200"}]
    annotations = led.artifacts("cloud_usage_disposition")
    assert len(annotations) == 1
    assert json.loads(annotations[0]["meta"])["disposition"] == "discarded_scope"
    meta = json.loads(led.artifacts(worker.KIND)[0]["meta"])
    assert "backend" not in meta and "config_generation" not in meta


def test_run_window_end_stops_the_next_post_but_keeps_an_arrived_result(routed, monkeypatch):
    led, cfg, calls, reserved, _ = routed
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="patient_context", project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kw: fn())
    provider.guard()                                   # inside the window: the attempt may POST
    cfg["cloud_llm"]["until"] = "2000-01-01T00:00:00+00:00"
    with pytest.raises(cloud_inference.CloudFallback):
        provider.guard()                               # every later attempt or retry is refused
    provider.commit_guard()                            # a response that already arrived is still saved
    # A stop or expired pricing after the response stops new sends, not the paid result.
    with monkeypatch.context() as patch:
        patch.setattr(cloud_policy, "global_block_reason", lambda ledger, **kwargs: "paused")
        with pytest.raises(cloud_inference.CloudFallback):
            provider.guard()
        provider.commit_guard()
    cfg["cloud_llm"]["until"] = "2999-01-01T00:00:00+00:00"
    cfg["cloud_llm"]["window_start"], cfg["cloud_llm"]["window_budget_usd"] = "2999-01-01T00:00:00+00:00", 1
    cfg["cloud_llm"]["until"] = "2999-01-02T00:00:00+00:00"
    with pytest.raises(cloud_inference.CloudFallback):
        provider.guard()                               # before the period starts nothing is sent


@pytest.mark.parametrize("mode", ["changed", "changed_release_fails", "unchanged", "changed_at_stop"])
def test_resident_checks_source_before_config_and_stop(tmp_path, monkeypatch, mode):
    ledger = _ledger(tmp_path)
    close = ledger.close
    events, calls, stale = [], [], [False]
    ledger.artifact_add("synthetic_paid_result", "{}", meta={"status": "received"})
    saved = [dict(row) for row in ledger.artifacts("synthetic_paid_result")]
    pending = {"project_id": 1, "_cloud_discard": lambda: events.append("discard")}
    lease = time.time() + 600
    def no_commit(**kwargs):
        pytest.fail("stale worker must not commit carried result")
    carried = [(pending, lease, no_commit)]
    source = tmp_path / "synthetic_inference.py"
    source.write_text("# synthetic generation\n")
    source_mtime = source.stat().st_mtime_ns
    clock = [100.0]

    class Pool:
        def __init__(self, root):
            assert root == str(tmp_path)
        def sync(self, cfg, *, ledger):
            events.append("pool_sync")
        def close(self):
            events.append("pool_closed")

    @contextmanager
    def locked(*args, **kwargs):
        yield True

    def load():
        events.append("config_read")
        if stale[0]:
            raise RuntimeError("synthetic unreadable configuration")
        return {}

    def release(db, row, token):
        assert db is ledger and row is pending and token == lease
        events.append("release")
        if mode == "changed_release_fails":
            raise RuntimeError("synthetic release failure")

    def run(db, **kwargs):
        calls.append(list(kwargs["carry"]))
        if len(calls) == 1:
            if mode != "unchanged":
                os.utime(source, ns=(source_mtime, source_mtime + 1_000_000_000))
                stale[0] = True
                if mode == "changed_at_stop":
                    clock[0] = 120.0
            return {"done": 1, "failed": 0, "left": 1, "carry": list(carried)}
        assert mode == "unchanged" and calls[-1] == carried
        return {"done": 0, "failed": 0, "left": None, "updated": True}

    monkeypatch.setattr(worker, "HOME", str(tmp_path))
    monkeypatch.setattr(extract_llm_engine, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_prepare, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_provider, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_resident, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_selection, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_source_contract, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_store, 'HOME', worker.HOME)
    monkeypatch.setattr(worker.writer_lock, "HOME", str(tmp_path))
    monkeypatch.setattr(inference, "__file__", str(source))
    monkeypatch.setattr(worker, "_INFERENCE_SOURCE_STAT", source_mtime)
    monkeypatch.setattr(extract_llm_source_contract, '_INFERENCE_SOURCE_STAT', worker._INFERENCE_SOURCE_STAT)
    monkeypatch.setattr(worker, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm_runner, '_WRITER_STATE', worker._WRITER_STATE)
    monkeypatch.setattr(extract_llm_source_contract, '_WRITER_STATE', worker._WRITER_STATE)
    monkeypatch.setattr(worker.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(worker, "Ledger", lambda *args: ledger)
    monkeypatch.setattr(extract_llm_resident, 'Ledger', worker.Ledger)
    monkeypatch.setattr(ledger, "close", lambda: events.append("ledger_closed"))
    monkeypatch.setattr(worker.cloud_backlog, "CloudBacklogPool", Pool)
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
    monkeypatch.setattr(worker, "legacy_admissions", lambda *args: None)
    monkeypatch.setattr(extract_llm_admission, 'legacy_admissions', worker.legacy_admissions)
    monkeypatch.setattr(extract_llm_resident, 'legacy_admissions', worker.legacy_admissions)
    monkeypatch.setattr(worker.extract_validation, "vital_threshold_policy", lambda cfg: None)
    monkeypatch.setattr(worker, "_write_lock", locked)
    monkeypatch.setattr(extract_llm_carry, '_write_lock', worker._write_lock)
    monkeypatch.setattr(extract_llm_resident, '_write_lock', worker._write_lock)
    monkeypatch.setattr(extract_llm_runner, '_write_lock', worker._write_lock)
    monkeypatch.setattr(worker, "_release", release)
    monkeypatch.setattr(extract_llm_carry, '_release', worker._release)
    monkeypatch.setattr(extract_llm_runner, '_release', worker._release)
    monkeypatch.setattr(worker, "run_pending", run)
    monkeypatch.setattr(extract_llm_resident, 'run_pending', worker.run_pending)
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: events.append("sleep"))
    args = ["extract_llm", "--all", "--lane", "2"]
    if mode == "changed_at_stop":
        args.extend(["--stop-after", "10"])
    monkeypatch.setattr(worker.sys, "argv", args)
    try:
        assert worker.main() == 0
        assert len(calls) == (2 if mode == "unchanged" else 1)
        assert events.count("pool_sync") == 1  # resuming carry admits no new pool work
        assert events.count("discard") == events.count("release") == 1
        assert events[-2:] == ["pool_closed", "ledger_closed"]
        assert [dict(row) for row in ledger.artifacts("synthetic_paid_result")] == saved
    finally:
        close()


@pytest.mark.parametrize('boundary', ['settings', 'empty_settings', 'missing_settings', 'reservation_caps', 'explicit_scope'])
def test_intermediate_setting_reads_preserve_unknown_or_explicit_scope_category(routed, monkeypatch, boundary):
    led, cfg, calls, _reserves, _results = routed
    _use_real_cloud_accounting(monkeypatch)
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route='reextract', project_id=1,
        unit_key='synthetic-read-gap', deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    before = [tuple(row) for row in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    if boundary == 'settings':
        def unreadable():
            raise mcs_util.ProviderSettingsUnreadable('synthetic-private-read-failure')
        monkeypatch.setattr(cloud_inference, 'load_config', unreadable)
        with pytest.raises(cloud_inference.CloudObservationIncomplete):
            cloud_inference._current_cloud_settings()
    elif boundary in ('empty_settings', 'missing_settings'):
        monkeypatch.setattr(cloud_inference, 'load_config', lambda: {} if boundary == 'empty_settings' else None)
        with pytest.raises(cloud_inference.CloudObservationIncomplete, match='cloud_config_unverified'):
            cloud_inference._current_cloud_settings()
    elif boundary == 'reservation_caps':
        monkeypatch.setattr(cloud_inference, '_current_cloud_settings', lambda: {})
        with pytest.raises(cloud_inference.CloudObservationIncomplete):
            provider.reserve(provider.client.body('synthetic prompt', 100, None), 30)
    else:
        changed = cloud_inference.CloudFallback('cloud_unit_changed')
        def revoked():
            raise changed
        monkeypatch.setattr(cloud_inference, '_current_cloud_settings', revoked)
        with pytest.raises(cloud_inference.CloudFallback) as error:
            provider.reserve(provider.client.body('synthetic prompt', 100, None), 30)
        assert error.value is changed
        assert not isinstance(error.value, cloud_inference.CloudObservationIncomplete)
    assert not calls and not provider.possibly_sent and provider.final_disposition is None
    assert [tuple(row) for row in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert not led.db.in_transaction
