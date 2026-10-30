"""Synthetic provider units, owner-thread reservations and local fallback."""
from contextlib import contextmanager
import json
import os
import threading
import time
from datetime import datetime

import pytest

from anthropic_client import AnthropicClient
import cloud_policy
import extract_llm as worker
import inference_dependency_loss as dependency_loss
import inference_version as inference
import mcs_queries
from extract_testkit import _hash, _ledger, _message


@pytest.fixture
def routed(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "max_concurrency": 2}}
    monkeypatch.setattr(worker, "load_config", lambda: cfg)
    monkeypatch.setattr(worker.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(worker, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(worker.extract, "_WRITER_STATE", (False, 1))
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
        def complete(self, prompt, *, deadline, max_tokens, before_attempt, reserve_fn, after_result,
                     cache_prefix=None):
            before_attempt()
            reservation = reserve_fn({"prompt": prompt, "max_tokens": max_tokens}, 1)
            before_attempt()
            calls.append(prompt)
            after_result(reservation, {"http_class": "success"})
            return {"status": 200, "finish_reason": "stop", "text": '{"symptoms":[]}'}
    monkeypatch.setattr(worker.anthropic_client, "AnthropicClient", Client)
    yield led, cfg, calls, reserved, results
    led.close()


def old(led, mid=1, pid=1):
    led.save_messages([_message(mid, "完全合成の記録。", project_id=pid)])
    led.artifact_add(worker.KIND, "{}", project_id=pid, message_id=mid,
        meta={"hash": _hash(led, mid), "extract_version": worker.EXTRACT_VERSION - 1})


def _current_artifacts(led):
    return led.db.execute("SELECT a.* FROM artifacts a JOIN messages m ON a.message_id=m.message_id "
        "WHERE a.kind=? " + mcs_queries.current_extract_pred()
        + " AND json_extract(a.meta,'$.extract_version')=? ORDER BY a.artifact_id",
        (worker.KIND, worker.EXTRACT_VERSION)).fetchall()


def _bound_source(led):
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = dependency_loss.source_binding(led.db, row["project_id"], row["message_id"])
    return row


def _assert_legacy_retained(led, before):
    after = {row["artifact_id"]: dict(row) for row in led.artifacts(worker.KIND)}
    assert before and all(after[row["artifact_id"]] == row for row in before)
    assert all(inference.read_version(json.loads(row["meta"])) == inference.LEGACY_VERSION for row in before)


def test_cloud_empty_length_replans_the_whole_source_without_json_salvage():
    body = "".join(f"完全合成の居住記録{i:02d}です。" for i in range(12))
    seen, accepted, meta = [], [], {}

    def provider(prompt, **kwargs):
        piece = prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
        seen.append(piece)
        if len(piece) > len(body) // 2:
            return {"status": 200, "text": "", "finish_reason": "length"}
        accepted.append(piece)
        return {"status": 200, "finish_reason": "stop", "text": json.dumps({"symptoms": [], "patient_context": [
            {"category": "living", "subject": "patient", "text": piece, "evidence": piece}]})}

    result = worker.llm_extract(body, hints={}, provider=provider, meta_out=meta)
    assert isinstance(result, dict) and "".join(accepted) == body
    assert "".join(item["text"] for item in result["patient_context"]) == body
    assert seen[0] == body and body not in accepted
    assert meta["length_stops"] == meta["length_resplits"] == 1
    assert meta["calls"] == len(seen)


def test_paid_cloud_split_resumes_completed_children_without_resending(routed, monkeypatch):
    led, cfg, _, reservations, results = routed
    body = "".join(f"完全合成の居住記録{i:02d}です。" for i in range(12))
    prompts, pieces, metadata = [], [], []
    record = cloud_policy.record_result

    def recorded(ledger, reservation, outcome):
        metadata.append(dict(outcome))
        return record(ledger, reservation, outcome)

    def wire(self, method, request, timeout, **kwargs):
        content = request["messages"][0]["content"]
        prompt = content if isinstance(content, str) else "".join(block["text"] for block in content)
        piece = prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
        prompts.append(prompt)
        pieces.append(piece)
        length = len(piece) > len(body) // 2
        content = ([{"type": "thinking", "thinking": "synthetic-private-thinking"}] if length else
                   [{"type": "text", "text": json.dumps({"symptoms": [], "patient_context": [
                       {"category": "living", "subject": "patient", "text": piece, "evidence": piece}]})}])
        return 200, {}, json.dumps({"model": worker.anthropic_client.MODEL,
            "stop_reason": "max_tokens" if length else "end_turn", "content": content,
            "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()

    class PausedProvider(worker._CloudProvider):
        paused = False

        def __call__(self, prompt, **kwargs):
            if len(pieces) == 2 and not self.paused:
                self.paused = True
                raise worker.CloudBusy("synthetic-before-next-child")
            return super().__call__(prompt, **kwargs)

    monkeypatch.setattr(worker.anthropic_client, "AnthropicClient", AnthropicClient)
    monkeypatch.setattr(AnthropicClient, "request", wire)
    monkeypatch.setattr(cloud_policy, "record_result", recorded)
    deadline = time.monotonic() + 600
    provider = PausedProvider(led, cfg, route="reextract", project_id=1, unit_key="synthetic-split",
        deadline=deadline, current=lambda: True, dispatch=lambda fn, **kw: fn())
    completed, splits = {}, {}
    with pytest.raises(worker.CloudBusy, match="synthetic-before-next-child"):
        worker.llm_extract(body, deadline=deadline, provider=provider, hints={}, chunks_out=completed,
                           on_split=lambda key, size: splits.__setitem__(key, size))
    assert len(completed) == 1 and splits and len(prompts) == 2
    paid_prefix = list(prompts)
    meta = {}
    result = worker.llm_extract(body, deadline=deadline, provider=provider, hints={}, chunks_in=completed, splits_in=splits,
                                meta_out=meta)
    assert isinstance(result, dict) and meta["checkpoint_reuses"] == 1
    assert "".join(item["text"] for item in result["patient_context"]) == body
    assert prompts[:2] == paid_prefix and len(set(prompts)) == len(prompts)
    assert len(prompts) == len(reservations) == len(results) == len(metadata) == 3
    assert all(m["error_kind"] is None and m["usage"]["output_tokens"] == 5 for m in metadata)
    assert "synthetic-private-thinking" not in json.dumps(metadata)


@pytest.mark.parametrize("text", ["", '{"summary":"partial"}'])
def test_unrecoverable_cloud_length_is_bounded_and_never_an_empty_success(text):
    calls, splits = [], {}

    def provider(prompt, **kwargs):
        calls.append(prompt)
        return {"status": 200, "text": text, "finish_reason": "length"}

    meta = {}
    assert worker.llm_extract("合成本文", provider=provider, hints={}, meta_out=meta,
                              on_split=lambda key, size: splits.__setitem__(key, size)) is None
    assert meta["failure_reason"] == "split_exhausted" and splits
    assert len(calls) <= len("合成本文").bit_length() + 1


@pytest.mark.parametrize("block", [{"type": "thinking"}, {"type": "redacted_thinking", "data": 1}])
def test_malformed_thinking_never_creates_cloud_splits_or_extra_http(routed, monkeypatch, block):
    led, cfg, _, reservations, results = routed
    calls, metadata, splits, completed = [], [], [], {}
    record = cloud_policy.record_result

    def recorded(ledger, reservation, outcome):
        metadata.append(dict(outcome))
        return record(ledger, reservation, outcome)

    def wire(self, method, body, timeout, **kwargs):
        calls.append(body)
        return 200, {}, json.dumps({"model": worker.anthropic_client.MODEL,
            "stop_reason": "max_tokens", "content": [block],
            "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()

    monkeypatch.setattr(worker.anthropic_client, "AnthropicClient", AnthropicClient)
    monkeypatch.setattr(AnthropicClient, "request", wire)
    monkeypatch.setattr(cloud_policy, "record_result", recorded)
    deadline = time.monotonic() + 180
    provider = worker._CloudProvider(led, cfg, route="reextract", project_id=1, unit_key="synthetic-invalid",
        deadline=deadline, current=lambda: True, dispatch=lambda fn, **kw: fn())
    with pytest.raises(worker.CloudFallback, match="cloud_unit_failed"):
        worker.llm_extract("完全合成の本文です。", provider=provider, deadline=deadline,
                           chunks_out=completed, on_split=lambda *args: splits.append(args))
    assert not splits and not completed
    assert len(calls) == len(reservations) == len(results) == len(metadata) == 1
    assert metadata[0]["error_kind"] == "protocol_error"


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
    monkeypatch.setattr(worker, "_probe_format", lambda **kw: pytest.fail("cloud probed local GPU"))
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
        assert meta["prompt"] == worker._cloud_contract()["prompt"] and "code_contract" not in meta
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
    monkeypatch.setattr(worker, "llm_extract", lambda *a, **kw: {})
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
                monkeypatch.setattr(cloud_policy, "block_reason", lambda *args: "paused")
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
    assert worker.llm_extract("合成の記録", deadline=time.monotonic() + 180, provider=provider) == {"symptoms": []}


def test_openai_uses_selected_key_model_and_contract(routed, monkeypatch):
    led, cfg, *_ = routed
    cfg["cloud_llm"]["provider"] = "openai"
    monkeypatch.setattr(cloud_policy, "provider_key", lambda name, root=None: "synthetic-openai" if name == "openai" else None)
    seen = []

    class Client:
        def __init__(self, key):
            assert key == "synthetic-openai"

        def complete(self, prompt, **kwargs):
            kwargs["before_attempt"]()
            seen.append(True)
            return {"status": 200, "text": '{"symptoms":[]}', "finish_reason": "stop", "model": "gpt-6-luna"}

    monkeypatch.setattr(worker.openai_client, "OpenAIClient", Client)
    provider = worker._CloudProvider(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kw: fn())
    assert provider.model == provider.contract["model"] == "gpt-6-luna"
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
    row = _bound_source(led)
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


def test_saved_provider_contract_is_the_fixed_request_shape_and_a_fresh_copy():
    # operating limits, rule versions and code releases are not part of it
    before = worker._cloud_contract()
    assert before == {"backend": "anthropic", "model": worker.anthropic_client.MODEL, "prompt": before["prompt"]}
    assert len(before["prompt"]) == 16 and "code_contract" not in before and "config_generation" not in before
    before["model"] = "edited"
    assert worker._cloud_contract()["model"] == worker.anthropic_client.MODEL


def test_cloud_call_resets_prior_unit_integrity_and_records_once(routed):
    led, cfg, calls, reservations, results = routed
    provider = worker._CloudProvider(led, cfg, route="reextract", project_id=1,
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
        after_result(reservation, {"http_class": "success"})
        return {"status": 200, "finish_reason": "stop", "text": '{"symptoms":[]}'}
    monkeypatch.setattr(worker.anthropic_client.AnthropicClient, "complete", complete)
    monkeypatch.setattr(cloud_policy, "record_result", lambda ledger, reservation, metadata: outcomes.append(metadata))
    def dispatch(fn, *, result=False):
        dispatches.append(result)
        return fn()
    provider = worker._CloudProvider(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=clock() + 180, current=lambda: True, dispatch=dispatch)
    assert provider("synthetic", deadline=clock() + 180, max_tokens=100)["text"] == '{"symptoms":[]}'
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
    assert provider("synthetic", deadline=repair_deadline, max_tokens=100)["text"] == '{"symptoms":[]}'
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
    report = worker.run_pending(led, budget_s=180, per_write_lock=True, write_wait_s=1, carry=[])
    assert report["done"] == 0 and len(result_locks) == 2
    assert len(calls) == len(reserved) == len(results) == 1
    assert len(led.artifacts("cloud_usage_disposition")) == int(loss != "none")
    assert len(report["carry"]) == int(loss == "none")
    if report["carry"]:
        worker._settle_carry(led, report["carry"], final=True)
        assert not report["carry"] and not led.artifacts("cloud_usage_disposition")
    assert json.loads(led.artifacts(worker.KIND)[0]["meta"])["extract_version"] < worker.EXTRACT_VERSION



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
    row = _bound_source(led)
    row.update(_cloud_mode="shadow", content_hash=_hash(led, 1))
    if fail:
        def boom(*a, **k):
            raise RuntimeError("synthetic failure inside the commit transaction")
        monkeypatch.setattr(worker, "_comparison_extract", boom)
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


def test_unreadable_config_never_auto_enables_the_paid_route(routed):
    # load_config() answers {} when config.json cannot be read; with a key in
    # .env the absent-block default would be "on", so {} must read as off
    led, *_ = routed
    assert worker._cloud_settings({})["mode"] == "off"
    assert worker._cloud_mode(led, {}, "reextract", 1) == "off"
    # A readable config uses the spending defaults, without changing its scope.
    defaults = worker._cloud_settings({"mcs_login_id": "synthetic"})
    assert defaults["mode"] == "on" and defaults["project_ids"] is None
    assert (defaults["daily_cost_limit_usd"], defaults["monthly_cost_limit_usd"]) == (25, 75)
    assert worker._cloud_settings({"mcs_login_id": "synthetic", "cloud_llm": {
        "daily_cost_limit_usd": None, "monthly_cost_limit_usd": None}})["mode"] == "off"
    assert worker._cloud_settings({"mcs_login_id": "synthetic",
                                   "cloud_llm": {"daily_cost_limit_usd": 2}})["mode"] == "on"


def test_paid_cloud_failure_with_the_local_breaker_open_records_the_retry(routed, monkeypatch):
    led, cfg, calls, reserves, results = routed
    old(led)
    monkeypatch.setattr(worker, "circuit_open_s", lambda db: 60)
    original = worker.llm_extract
    def extract(body, **kwargs):
        if kwargs.get("provider"):
            original(body, **kwargs)      # the paid attempt is sent and settled
            raise worker.CloudFallback("cloud_incomplete_response")
        pytest.fail("local must not run while its breaker is open")
    monkeypatch.setattr(worker, "llm_extract", extract)
    report = worker.run_pending(led, budget_s=180)
    assert report["deferred"] == 1 and report["done"] == 0 and len(calls) == len(results) == 1
    [error] = [json.loads(a["meta"]) for a in led.artifacts(worker.KIND) if json.loads(a["meta"]).get("error")]
    assert error["attempts"] == 1 and error["next_try"] > time.time()
    # the retry backoff keeps the next pass from re-sending the same body
    assert worker.run_pending(led, budget_s=180)["done"] == 0 and len(calls) == 1


def test_run_window_end_stops_the_next_post_but_keeps_an_arrived_result(routed, monkeypatch):
    led, cfg, calls, reserved, _ = routed
    provider = worker._CloudProvider(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kw: fn())
    provider.guard()                                   # inside the window: the attempt may POST
    cfg["cloud_llm"]["until"] = "2000-01-01T00:00:00+00:00"
    with pytest.raises(worker.CloudFallback):
        provider.guard()                               # every later attempt or retry is refused
    provider.commit_guard()                            # a response that already arrived is still saved
    # A stop or expired pricing after the response stops new sends, not the paid result.
    with monkeypatch.context() as patch:
        patch.setattr(cloud_policy, "global_block_reason", lambda ledger: "paused")
        with pytest.raises(worker.CloudFallback):
            provider.guard()
        provider.commit_guard()
    cfg["cloud_llm"]["until"] = "2999-01-01T00:00:00+00:00"
    cfg["cloud_llm"]["window_start"], cfg["cloud_llm"]["window_budget_usd"] = "2999-01-01T00:00:00+00:00", 1
    cfg["cloud_llm"]["until"] = "2999-01-02T00:00:00+00:00"
    with pytest.raises(worker.CloudFallback):
        provider.guard()                               # before the period starts nothing is sent
