"""Synthetic provider units, owner-thread reservations and local fallback."""
from contextlib import contextmanager
import json
import os
import re
import threading
import time
from datetime import datetime

import pytest

from anthropic_client import AnthropicClient
import cloud_policy
import cloud_client
from cloud_policy import record_disposition_tx as real_record_disposition_tx
from cloud_policy import record_result as real_record_result
from cloud_policy import usage_reserver as real_usage_reserver
import extract_llm as worker
import cloud_inference
import inference_dependency_loss as dependency_loss
import inference_version as inference
import mcs_queries
import mcs_util
from extract_testkit import _hash, _ledger, _message


@pytest.fixture
def routed(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "max_concurrency": 2}}
    monkeypatch.setattr(worker, "load_config", lambda: cfg)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(worker.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(worker, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(worker.extract, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(cloud_policy, "api_key", lambda *a: "synthetic-key")
    monkeypatch.setattr(cloud_policy, "block_reason", lambda led, cfg, route, pid, **_kwargs:
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
    response = provider("synthetic prompt", deadline=time.monotonic() + 120, max_tokens=1600)
    assert response["status"] == 200 and len(received) == len(reserved) == 1
    assert received[0]["model"] == model and received[0]["reasoning"] == {"effort": effort}
    assert received[0]["max_output_tokens"] == tokens
    assert dict(reserved[0]["identity"]["selection"]) == dict(provider.selection)
    assert provider.contract == cloud_inference.cloud_provider_contract(selection=provider.selection)


@pytest.mark.parametrize("change", ["none", "answer", "prompt", "contract", "owner", "unit", "missing_hash", "missing_finish"])
def test_repair_answer_evidence_requires_exact_settled_request(tmp_path, monkeypatch, change):
    import hashlib

    ledger = _ledger(tmp_path)
    try:
        ledger.save_messages([_message(1, "完全合成の記録です。", project_id=1)])
        cfg = {"cloud_llm": {"mode": "on", "provider": "openai", "project_ids": [1]}}
        selection, errors = cloud_policy.resolve_profile(cfg, route="reextract", purpose="quality_repair")
        assert not errors
        monkeypatch.setattr(cloud_policy, "pricing_valid", lambda now=None, *, model=None: True)
        client = worker.openai_client.OpenAIClient("synthetic", model=selection["model"], effort=selection["effort"])
        prompt, answer = "synthetic prompt", '{"synthetic":"answer"}'
        body = client.body(prompt, selection["max_tokens"], None)
        owner = {"lease": "synthetic-lease"}
        reservation = real_usage_reserver(ledger, cfg, route="reextract", project_id=1, message_id=1,
            unit_key="synthetic-unit", owner=owner, selection=selection)(body, 30)
        usage = {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3,
                 "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}
        result = {"model": selection["model"], "request_fp": reservation["request_fp"],
                  "http_class": "http_200", "usage": usage, "error_kind": None,
                  "answer_finish_reason_v1": "stop"}
        if change != "missing_hash":
            result["answer_text_sha256_v1"] = hashlib.sha256(answer.encode()).hexdigest()
        if change == "missing_finish":
            result.pop("answer_finish_reason_v1")
        real_record_result(ledger, reservation, result)
        row = ledger.db.execute("SELECT artifact_id FROM artifacts WHERE kind='cloud_usage_reserve'").fetchone()
        contract = cloud_inference.cloud_provider_contract(selection=selection)
        args = {"reservation_artifact_id": row[0], "reservation_id": reservation["reservation_id"],
                "route": "reextract", "project_id": 1, "message_id": 1,
                "unit_key": "synthetic-unit", "owner": owner, "prompt": prompt,
                "answer_text": answer, "contract": contract}
        if change == "answer":
            args["answer_text"] += "tampered"
        elif change == "prompt":
            args["prompt"] += "tampered"
        elif change == "contract":
            args["contract"] = {**contract, "prompt": "0" * 16}
        elif change == "owner":
            args["owner"] = {"lease": "changed"}
        elif change == "unit":
            args["unit_key"] = "other-unit"
        if change == "none":
            assert cloud_inference.verify_cloud_repair_evidence(ledger.db, **args)["reservation"] == reservation
            from types import SimpleNamespace
            provider = SimpleNamespace(completed_reservations=[reservation], ledger=ledger,
                contract=contract, purpose="quality_repair", dispatch=lambda fn, **_kwargs: fn())
            origin = worker._cloud_origin_for_checkpoint(provider, prompt, answer)
            assert origin["answer_text"] == answer and origin["reservation_artifact_id"] == row[0]
            assert origin["reservation_id"] == reservation["reservation_id"] and origin["witness"]
        else:
            with pytest.raises(ValueError):
                cloud_inference.verify_cloud_repair_evidence(ledger.db, **args)
    finally:
        ledger.close()


def test_repair_source_span_distinguishes_repeated_split_text(monkeypatch):
    monkeypatch.setattr(worker, "plan_chunks", lambda text, size: [
        text[index:index + size] for index in range(0, len(text), size)])
    assert worker._chunk_span("ABCDABCD", 4, "1:1", {1: 2}) == ("CD", 6)
    assert worker._chunk_span("ABCDABCD", 4, "0:1", {0: 2}) == ("CD", 2)


def test_pending_repair_origin_rebuilds_prompt_from_current_source(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace

    ledger = _ledger(tmp_path)
    try:
        ledger.save_messages([_message(1, "完全合成の経過です。", project_id=1)])
        row = _bound_source(ledger)
        cfg = {"cloud_llm": {"mode": "on", "provider": "openai", "project_ids": [1]}}
        selection, errors = cloud_policy.resolve_profile(cfg, route="reextract", purpose="backlog")
        assert not errors
        monkeypatch.setattr(cloud_policy, "pricing_valid", lambda now=None, *, model=None: True)
        contract = cloud_inference.cloud_provider_contract(selection=selection)
        row.update(_target=("", selection["model"]), _provider_contract=contract)
        prefix, _ = worker._extract_prompt_context(None, None, None)
        hints = worker._rule_hints(row)
        hint = worker._hint_block(hints) if hints else ""
        prompt = (prefix + (worker._HINT_HEAD + hint + worker._HINT_TAIL if hint else "")
                  + worker._target_head(row["posted_at"]) + row["body_text"] + worker._PROMPT_TAIL)
        answer = json.dumps({"symptoms": []})
        client = worker.openai_client.OpenAIClient("synthetic", model=selection["model"], effort=selection["effort"])
        request = client.body(prompt, selection["max_tokens"], None)
        reservation = real_usage_reserver(ledger, cfg, route="reextract", project_id=1, message_id=1,
            unit_key="1", owner={"lease": 123.0}, selection=selection)(request, 30)
        usage = {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3,
                 "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}
        real_record_result(ledger, reservation, {"model": selection["model"], "request_fp": reservation["request_fp"],
            "http_class": "http_200", "usage": usage, "error_kind": None,
            "answer_text_sha256_v1": hashlib.sha256(answer.encode()).hexdigest(), "answer_finish_reason_v1": "stop"})
        provider = SimpleNamespace(completed_reservations=[reservation], ledger=ledger, contract=contract,
            purpose="backlog", dispatch=lambda fn, **_kwargs: fn())
        origin = {**worker._cloud_origin_for_checkpoint(provider, prompt, answer), "qc_src": None}
        candidate = worker.extract_validation.validate_piece(json.loads(answer), row["body_text"], row["body_text"],
                                                              {}, False, None, source_offset=0)
        candidate.pop("reply", None)
        assert worker._checkpoint_quality_origin(ledger.db, row, None, 0, worker._CHUNK_SIZE, {}, origin, candidate) == origin
        altered = {**row, "body_text": "別の完全合成の経過です。"}
        assert worker._checkpoint_quality_origin(ledger.db, altered, None, 0, worker._CHUNK_SIZE, {}, origin, candidate) is None
        assert worker._checkpoint_quality_origin(ledger.db, row, None, 0, worker._CHUNK_SIZE, {},
                                                  {**origin, "answer_text": "{}"}, candidate) is None
    finally:
        ledger.close()


def test_different_quality_profile_is_not_sent_without_final_replay(monkeypatch):
    from types import SimpleNamespace

    calls, states = [], []
    monkeypatch.setattr(worker, "_llm_call", lambda *_args, **_kwargs: calls.append("normal") or {"symptoms": []})
    monkeypatch.setattr(worker.extract_quality, "completeness_issues", lambda *_args: ["synthetic-missing"])
    monkeypatch.setattr(worker.extract_quality, "is_thin", lambda *_args: False)
    child = SimpleNamespace(selection={"model": "synthetic-quality"})
    monkeypatch.setattr(cloud_policy, "resolve_profile", lambda *_args, **_kwargs: (child.selection, []))
    class Provider:
        selection = {"model": "synthetic-normal"}
        _profile_cfg = {}
        def dispatch(self, fn, **_kwargs):
            return fn()
        def spawn_quality_repair(self):
            return child
    def checkpoint(_key, value):
        states.append(value.get("_quality_status"))
        value.quality_started_id = len(states)
        return True
    result = worker.llm_extract("完全合成の経過です。", hints={}, provider=Provider(),
                                deadline=time.monotonic() + 180, on_chunk=checkpoint)
    assert result is worker._DEFERRED
    assert calls == ["normal"] and states == ["pending"]


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

    class PausedProvider(cloud_inference.CloudInferenceUnit):
        paused = False

        def __call__(self, prompt, **kwargs):
            if len(pieces) == 2 and not self.paused:
                self.paused = True
                raise cloud_inference.CloudBusy("synthetic-before-next-child")
            return super().__call__(prompt, **kwargs)

    monkeypatch.setattr(worker.anthropic_client, "AnthropicClient", AnthropicClient)
    monkeypatch.setattr(AnthropicClient, "request", wire)
    monkeypatch.setattr(cloud_policy, "record_result", recorded)
    deadline = time.monotonic() + 600
    provider = PausedProvider(led, cfg, route="reextract", project_id=1, unit_key="synthetic-split",
        deadline=deadline, current=lambda: True, dispatch=lambda fn, **kw: fn())
    completed, splits = {}, {}
    with pytest.raises(cloud_inference.CloudBusy, match="synthetic-before-next-child"):
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
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1, unit_key="synthetic-invalid",
        deadline=deadline, current=lambda: True, dispatch=lambda fn, **kw: fn())
    with pytest.raises(cloud_inference.CloudFallback, match="cloud_unit_failed"):
        worker.llm_extract("完全合成の本文です。", provider=provider, deadline=deadline,
                           chunks_out=completed, on_split=lambda *args: splits.append(args))
    assert not splits and not completed
    assert len(calls) == len(reservations) == len(results) == len(metadata) == 1
    assert metadata[0]["error_kind"] == "protocol_error"


def _split_checkpoint_cloud_jobs(routed, monkeypatch, workers):
    led, cfg, _, _, _ = routed
    cfg["cloud_llm"]["max_concurrency"] = workers
    bodies = {mid: "".join(f"完全合成{mid}番区間{i}の居住記録です。" for i in range(6)) for mid in (1, 2)}
    for mid, body in bodies.items():
        led.save_messages([_message(mid, body)])
        led.artifact_add(worker.KIND, "{}", project_id=1, message_id=mid,
            meta={"hash": _hash(led, mid), "extract_version": worker.EXTRACT_VERSION - 1})
    with led.db:
        led.db.execute("UPDATE messages SET first_seen=?", (time.time() - 601,))
        led.db.execute("UPDATE message_revisions SET observed_at=?", (time.time() - 601,))
        cloud_policy.capture_history_tx(led.db, "synthetic-checkpoint-ack", time.time())
    posts, rejected = [], set()

    def wire(self, method, request, timeout, **kwargs):
        content = request["messages"][0]["content"]
        prompt = content if isinstance(content, str) else "".join(block["text"] for block in content)
        piece = prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
        mid = int(re.search(r"完全合成([12])番", piece)[1])
        posts.append({"message_id": mid, "piece": piece, "after_rejection": mid in rejected})
        length = piece == bodies[mid]
        content = ([{"type": "thinking", "thinking": "synthetic"}] if length else
                   [{"type": "text", "text": json.dumps({"symptoms": [], "patient_context": [
                       {"category": "living", "subject": "patient", "text": piece, "evidence": piece}]})}])
        return 200, {}, json.dumps({"model": worker.anthropic_client.MODEL,
            "stop_reason": "max_tokens" if length else "end_turn", "content": content,
            "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()

    monkeypatch.setattr(worker.anthropic_client, "AnthropicClient", AnthropicClient)
    monkeypatch.setattr(AnthropicClient, "request", wire)
    monkeypatch.setattr(worker, "_probe_format", lambda **kwargs: pytest.fail("cloud probed local GPU"))
    return bodies, posts, rejected


@pytest.mark.parametrize("workers,reject", [(1, "split"), (2, "split"), (1, "child"), (2, "child")],
                         ids=["serial-split", "parallel-split", "serial-child", "parallel-child"])
def test_run_pending_rejected_checkpoint_stops_further_cloud_posts(routed, monkeypatch, workers, reject):
    led, _, _, reservations, results = routed
    bodies, posts, rejected = _split_checkpoint_cloud_jobs(routed, monkeypatch, workers)
    before = [dict(row) for row in led.artifacts(worker.KIND)]
    persist = worker._persist_chunks
    saved_splits = []

    def rejected_checkpoint(ledger, row, chunks, context=None, **kwargs):
        is_split = kwargs.get("split_size") is not None
        if is_split == (reject == "split"):
            rejected.add(row["message_id"])
            return False
        accepted = persist(ledger, row, chunks, context, **kwargs)
        if is_split and accepted:
            saved_splits.append(row["message_id"])
        return accepted

    monkeypatch.setattr(worker, "_persist_chunks", rejected_checkpoint)
    report = worker.run_pending(led, limit=2, workers=workers, budget_s=180, cloud_only=True)
    assert rejected == set(bodies) and report["done"] == 0
    assert not [post for post in posts if post["after_rejection"]], posts
    assert len(posts) == len(reservations) == len(results) == (2 if reject == "split" else 4)
    assert sorted(saved_splits) == ([] if reject == "split" else [1, 2])
    assert not _current_artifacts(led)
    _assert_legacy_retained(led, before)


@pytest.mark.parametrize("workers", [1, 2], ids=["serial", "parallel"])
def test_run_pending_acknowledged_split_keeps_full_cloud_coverage(routed, monkeypatch, workers):
    led, _, _, reservations, results = routed
    bodies, posts, rejected = _split_checkpoint_cloud_jobs(routed, monkeypatch, workers)
    persist, saved_splits = worker._persist_chunks, []

    def acknowledged(ledger, row, chunks, context=None, **kwargs):
        accepted = persist(ledger, row, chunks, context, **kwargs)
        if kwargs.get("split_size") is not None:
            assert accepted
            saved_splits.append(row["message_id"])
        return accepted

    monkeypatch.setattr(worker, "_persist_chunks", acknowledged)
    report = worker.run_pending(led, limit=2, workers=workers, budget_s=180, cloud_only=True)
    assert report["done"] == 2 and not rejected and sorted(saved_splits) == [1, 2]
    assert len(posts) == len(reservations) == len(results) == 6
    current = _current_artifacts(led)
    assert len(current) == 2
    for row in current:
        assert "".join(item["text"] for item in json.loads(row["content"])["patient_context"]) == bodies[row["message_id"]]


@pytest.mark.parametrize("stop", ["deadline", "owner"])
def test_run_pending_split_ack_wait_releases_on_deadline_or_owner_stop(routed, monkeypatch, stop):
    led, _, _, _, _ = routed
    bodies, posts, rejected = _split_checkpoint_cloud_jobs(routed, monkeypatch, 2)
    persist, clock = worker._persist_chunks, time.monotonic
    started = clock()

    def stopped(ledger, row, chunks, context=None, **kwargs):
        if kwargs.get("split_size") is None:
            return persist(ledger, row, chunks, context, **kwargs)
        rejected.update(bodies)
        if stop == "owner":
            raise RuntimeError("synthetic-owner-checkpoint-stop")
        accepted = persist(ledger, row, chunks, context, **kwargs)
        monkeypatch.setattr(worker.time, "monotonic", lambda: started + 181)
        return accepted

    monkeypatch.setattr(worker, "_persist_chunks", stopped)
    if stop == "owner":
        with pytest.raises(RuntimeError, match="synthetic-owner-checkpoint-stop"):
            worker.run_pending(led, limit=2, workers=2, budget_s=180, cloud_only=True)
    else:
        assert worker.run_pending(led, limit=2, workers=2, budget_s=180, cloud_only=True)["done"] == 0
    assert clock() - started < 3 and rejected
    assert 1 <= len(posts) <= 2 and not [post for post in posts if post["after_rejection"]]
    assert not _current_artifacts(led)


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
        assert meta["prompt"] == cloud_inference.cloud_provider_contract()["prompt"] and "code_contract" not in meta
        assert meta["config_generation"] == cloud_policy.config_generation({"cloud_llm": cloud_inference.resolve_cloud_settings(cfg)})
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
    report = worker.run_pending(led, budget_s=180)
    assert report["done"] == 1 and not calls and len(reserves) == 1
    assert not led.artifacts("cloud_usage_disposition")
    assert len(local) == 1
    artifact = led.artifacts(worker.KIND)[0]
    assert "backend" not in json.loads(artifact["meta"])


def test_injected_call_keeps_existing_json_validation(routed):
    led, cfg, *_ = routed
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kw: fn())
    assert worker.llm_extract("合成の記録", deadline=time.monotonic() + 180, provider=provider) == {"symptoms": []}


def test_openai_uses_selected_key_model_and_contract(routed, monkeypatch):
    led, cfg, *_ = routed
    cfg["cloud_llm"]["provider"] = "openai"
    monkeypatch.setattr(cloud_policy, "provider_key", lambda name, root=None: "synthetic-openai" if name == "openai" else None)
    seen = []

    class Client:
        def __init__(self, key, *, model, effort):
            assert key == "synthetic-openai"
            assert (model, effort) == ("gpt-6-luna", "medium")

        def complete(self, prompt, **kwargs):
            kwargs["before_attempt"]()
            seen.append(True)
            return {"status": 200, "text": '{"symptoms":[]}', "finish_reason": "stop", "model": "gpt-6-luna"}

    monkeypatch.setattr(worker.openai_client, "OpenAIClient", Client)
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
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
            raise cloud_inference.CloudFallback("synthetic_failure")
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
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
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
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
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
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
        unit_key="synthetic", deadline=clock() + 180, current=lambda: True, dispatch=dispatch)
    assert provider("synthetic", deadline=clock() + 180, max_tokens=100)["text"] == '{"symptoms":[]}'
    assert outcomes == [{"http_class": "success"}]
    assert dispatches[-2:] == [True, True]
    with pytest.raises(cloud_inference.CloudFallback):
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
            raise cloud_inference.CloudFallback("synthetic_transport")
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


def _use_real_cloud_accounting(monkeypatch):
    monkeypatch.setattr(cloud_policy, "usage_reserver", real_usage_reserver)
    monkeypatch.setattr(cloud_policy, "record_result", real_record_result)
    monkeypatch.setattr(cloud_policy, "record_disposition_tx", real_record_disposition_tx)
    monkeypatch.setattr(cloud_policy, "pricing_valid", lambda *args, **kwargs: True)
    monkeypatch.setattr(worker.anthropic_client, "AnthropicClient", AnthropicClient)


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
    monkeypatch.setattr(cloud_inference, 'load_config', load)
    monkeypatch.setattr(AnthropicClient, "request", request)
    deadline = time.monotonic() + 180
    def provider():
        return cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1, unit_key="synthetic",
                                     deadline=deadline, current=lambda: True, dispatch=lambda fn, **kwargs: fn())

    first = provider()
    with pytest.raises(cloud_inference.CloudFallback, match="cloud_unit_changed"):
        first("synthetic", deadline=deadline, max_tokens=100)
    reserves, results = led.artifacts("cloud_usage_reserve"), led.artifacts("cloud_usage_result")
    assert len(posts) == len(reserves) == len(results) == 1
    reservation, result = json.loads(reserves[0]["meta"]), json.loads(results[0]["meta"])
    assert result["reservation_id"] == reservation["reservation_id"]
    assert result["http_class"] == "http_200" and result["error_kind"] is None
    assert result["usage"]["input_tokens"] == 10 and result["usage"]["output_tokens"] == 5
    assert result["disposition"] == "discarded_scope" and not led.db.in_transaction
    unavailable[0] = False
    with pytest.raises(cloud_inference.CloudFallback):
        first("synthetic", deadline=deadline, max_tokens=100)
    with pytest.raises(cloud_client.CloudNotSent) as blocked:
        provider()("synthetic", deadline=deadline, max_tokens=100)
    assert (blocked.value.kind, blocked.value.detail) == ("budget_exceeded", "cloud_unit_unresolved")
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
        if reads[0] >= fails_on_read:
            raise error_type("synthetic")
        return cfg
    monkeypatch.setattr(worker, "load_config", load)
    monkeypatch.setattr(cloud_inference, 'load_config', load)
    monkeypatch.setattr(AnthropicClient, "request", lambda *args, **kwargs: pytest.fail("unexpected post"))
    deadline = time.monotonic() + 180
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1, unit_key="synthetic",
                                     deadline=deadline, current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    with pytest.raises(cloud_inference.CloudFallback, match="cloud_unit_changed"):
        provider("synthetic", deadline=deadline, max_tokens=100)
    reserves, results = led.artifacts("cloud_usage_reserve"), led.artifacts("cloud_usage_result")
    assert len(reserves) == len(results) == (1 if fails_on_read == 4 else 0)
    if results:
        result = json.loads(results[0]["meta"])
        assert result["http_class"] == "not_sent" and result["usage"] is None
        assert result["reservation_id"] == json.loads(reserves[0]["meta"])["reservation_id"]
    assert not provider.possibly_sent and not led.db.in_transaction


def test_transient_read_error_after_reservation_stays_unsent_and_discarded(routed, monkeypatch):
    led, cfg, _, _, _ = routed
    _use_real_cloud_accounting(monkeypatch)
    reads = [0]
    def load():
        reads[0] += 1
        if reads[0] == 4:
            raise mcs_util.ProviderSettingsUnreadable("synthetic")
        return cfg
    monkeypatch.setattr(worker, "load_config", load)
    monkeypatch.setattr(cloud_inference, 'load_config', load)
    monkeypatch.setattr(AnthropicClient, "request", lambda *args, **kwargs: pytest.fail("unexpected post"))
    deadline = time.monotonic() + 180
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1, unit_key="synthetic",
                                     deadline=deadline, current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    with pytest.raises(cloud_inference.CloudFallback, match="cloud_unit_changed"):
        provider("synthetic", deadline=deadline, max_tokens=100)
    result, = led.artifacts("cloud_usage_result")
    meta = json.loads(result["meta"])
    assert meta["http_class"] == "not_sent" and meta["usage"] is None
    disposition, = led.artifacts("cloud_usage_disposition")
    assert json.loads(disposition["meta"])["disposition"] == "discarded_scope"
    assert not provider.possibly_sent and not led.db.in_transaction


def test_capacity_read_failure_discards_previously_settled_response(routed, monkeypatch):
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
    monkeypatch.setattr(cloud_inference, 'load_config', load)
    monkeypatch.setattr(AnthropicClient, "request", request)
    deadline = time.monotonic() + 180
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1, unit_key="synthetic",
                                     deadline=deadline, current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    provider("synthetic", deadline=deadline, max_tokens=100)
    unavailable[0] = True
    with pytest.raises(cloud_inference.CloudFallback, match="cloud_unit_changed"):
        provider("synthetic", deadline=deadline, max_tokens=100)
    result, = led.artifacts("cloud_usage_result")
    meta = json.loads(result["meta"])
    assert len(posts) == 1 and meta["http_class"] == "http_200"
    assert meta["usage"]["input_tokens"] == 10 and meta["usage"]["output_tokens"] == 5
    disposition, = led.artifacts("cloud_usage_disposition")
    assert json.loads(disposition["meta"])["disposition"] == "discarded_scope"
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
    original = worker.llm_extract
    def extract(body, **kwargs):
        if kwargs.get("provider"):
            original(body, **kwargs)      # the paid attempt is sent and settled
            raise cloud_inference.CloudFallback("cloud_incomplete_response")
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
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="reextract", project_id=1,
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
        def __init__(self, root):
            assert root == str(tmp_path)
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
    monkeypatch.setattr(worker, "Ledger", lambda *args: ledger)
    monkeypatch.setattr(ledger, "close", lambda: events.append("ledger_closed"))
    monkeypatch.setattr(worker.cloud_backlog, "CloudBacklogPool", Pool)
    monkeypatch.setattr(worker, "load_config", lambda: {})
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: {"mode": "off", "max_concurrency": 1})
    monkeypatch.setattr(worker, "legacy_admissions", lambda *args: None)
    monkeypatch.setattr(worker.extract_validation, "vital_threshold_policy", lambda cfg: None)
    monkeypatch.setattr(worker.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(worker, "_writer_current", lambda *, full=False: True)
    monkeypatch.setattr(worker, "_write_lock", locked)
    monkeypatch.setattr(worker, "_release", release)
    monkeypatch.setattr(worker, "run_pending", run)
    monkeypatch.setattr(worker, "_background_semantic", lambda *args: events.append("semantic") or {"done": 0})
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
    monkeypatch.setattr(worker.writer_lock, "HOME", str(tmp_path))
    monkeypatch.setattr(inference, "__file__", str(source))
    monkeypatch.setattr(worker, "_INFERENCE_SOURCE_STAT", source_mtime)
    monkeypatch.setattr(worker, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(worker.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(worker, "Ledger", lambda *args: ledger)
    monkeypatch.setattr(ledger, "close", lambda: events.append("ledger_closed"))
    monkeypatch.setattr(worker.cloud_backlog, "CloudBacklogPool", Pool)
    monkeypatch.setattr(worker, "load_config", load)
    monkeypatch.setattr(cloud_inference, 'load_config', load)
    monkeypatch.setattr(worker, "legacy_admissions", lambda *args: None)
    monkeypatch.setattr(worker.extract_validation, "vital_threshold_policy", lambda cfg: None)
    monkeypatch.setattr(worker, "_write_lock", locked)
    monkeypatch.setattr(worker, "_release", release)
    monkeypatch.setattr(worker, "run_pending", run)
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


def test_paid_carry_survives_lowered_cap_and_blocks_next_send(routed, monkeypatch, tmp_path):
    led, cfg, _, _, _ = routed
    _use_real_cloud_accounting(monkeypatch)
    monkeypatch.setattr(worker, "HOME", str(tmp_path))
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
    monkeypatch.setattr(worker, "llm_extract", extract)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
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
    with pytest.raises(cloud_client.CloudNotSent) as denied:
        next_unit("different synthetic request", deadline=deadline, max_tokens=100)
    assert (denied.value.kind, denied.value.detail) == ("budget_exceeded", "daily_cap")
    assert len(posts) == len(led.artifacts("cloud_usage_reserve")) == len(led.artifacts("cloud_usage_result")) == 1
    assert not led.db.in_transaction


@pytest.mark.parametrize('split', ['none', 'initial', 'repair'],
                         ids=['one-leaf', 'initial-length-split', 'high-repair-length-split'])
@pytest.mark.parametrize('damage', ['none', 'proof_missing', 'answer_changed', 'repair_answer_changed', 'receipt_missing'])
def test_high_repair_final_proof_survives_checkpoint_cleanup(routed, monkeypatch, tmp_path, split, damage):
    """Paid repairs remain verifiable after the original checkpoint rows are removed."""
    from contextlib import nullcontext

    led, cfg, _, _, _ = routed
    _use_real_cloud_accounting(monkeypatch)
    cfg['cloud_llm']['provider'] = 'openai'
    monkeypatch.setattr(worker, 'HOME', str(tmp_path))
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *_args: 'synthetic-key')
    monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *_args: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)
    unit = '本人に発熱と咳があります。本人の血圧120/80です。'
    second = '本人は寒気があります。血圧120/80を確認しました。'
    body = unit + ('\n' + second if split != 'none' else '')
    led.save_messages([_message(1, body, project_id=1)])
    led.artifact_add(worker.KIND, '{}', project_id=1, message_id=1,
        meta={'hash': _hash(led), 'extract_version': worker.EXTRACT_VERSION - 1})
    with led.db:
        led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=1', (time.time() - 601,))
        led.db.execute('UPDATE message_revisions SET observed_at=? WHERE message_id=1', (time.time() - 601,))
        cloud_policy.capture_history_tx(led.db, 'synthetic-final-proof', time.time())

    sends, states = [], []
    original_persist = worker._persist_chunks
    def persist(ledger, row, chunks, context=None, **kwargs):
        accepted = original_persist(ledger, row, chunks, context, **kwargs)
        if accepted and kwargs.get('split_size') is None:
            for value in chunks.values():
                states.append(value.get('_quality_status') or 'completed')
        return accepted
    monkeypatch.setattr(worker, '_persist_chunks', persist)
    real_client = worker.openai_client.OpenAIClient
    class Client(real_client):
        def request(self, method, request, timeout, **kwargs):
            assert timeout > 0 and not led.db.in_transaction
            prompt = request['input']
            piece = prompt.rsplit('<<<\n', 1)[1].split('\n>>>', 1)[0]
            sends.append((request['model'], request['reasoning']['effort'], piece))
            truncated = (piece == body and (split == 'initial' and request['reasoning']['effort'] != 'high'
                         or split == 'repair' and request['reasoning']['effort'] == 'high'))
            symptoms = [{'text': text, 'status': 'ongoing', 'subject': 'patient', 'evidence': text}
                        for text in ('発熱', '咳', '寒気') if text in piece]
            answer = {'symptoms': symptoms}
            if request['reasoning']['effort'] == 'high' and '血圧120/80' in piece:
                answer['vitals'] = {'sbp': 120, 'dbp': 80}
            return 200, {}, json.dumps({
                'status': 'incomplete' if truncated else 'completed',
                **({'incomplete_details': {'reason': 'max_output_tokens'}} if truncated else {}),
                'model': self.model, 'service_tier': 'default',
                'output': [{'type': 'reasoning'}] if truncated else [{'type': 'message', 'role': 'assistant', 'content': [
                    {'type': 'output_text', 'text': json.dumps(answer, ensure_ascii=False)}]}],
                'usage': {'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3,
                          'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}}).encode()
    monkeypatch.setattr(worker.openai_client, 'OpenAIClient', Client)
    monkeypatch.setattr(worker, '_probe_format', lambda **_kwargs: pytest.fail('unexpected local inference'))

    result = worker.run_pending(led, limit=1, workers=1, budget_s=600, cloud_only=True)
    assert result['done'] == 1, (result, states, sends)
    assert 'pending' in states and 'started' in states and 'completed' in states
    assert any(model == 'gpt-6.1-sol' and effort == 'high' for model, effort, _ in sends)
    assert all(model == 'gpt-6-luna' for model, effort, _ in sends if effort != 'high')
    if split == 'repair':
        full_high = [index for index, (model, effort, piece) in enumerate(sends)
                     if model == 'gpt-6.1-sol' and effort == 'high' and piece == body]
        assert len(full_high) == 1
        assert any(piece != body for _, _, piece in sends[full_high[0] + 1:])
        results = [json.loads(row['meta']) for row in led.artifacts('cloud_usage_result', project_id=1, message_id=1)]
        assert any(row.get('model') == 'gpt-6.1-sol' and row.get('answer_finish_reason_v1') == 'length'
                   for row in results)
    assert not led.artifacts('extract_llm_chunk', message_id=1)
    [final] = _current_artifacts(led)
    content = json.loads(final['content'])
    assert '__mcs_internal_proof_v1' in content
    assert content['vitals']['sbp'] == 120 and content['vitals']['dbp'] == 80
    message = led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
    final_integrity = json.loads(final['meta'])['integrity']
    assert worker.progress_for_message(led.db, message, model='gpt-6-luna')['state'] == 'complete'
    assert len(final_integrity['completeness']) == final_integrity['completeness_chunks']
    if split == 'repair':
        assert '0' not in final_integrity['completeness']
    first_sends = len(sends)
    evidence_failures = []

    if damage == 'proof_missing':
        content.pop('__mcs_internal_proof_v1')
    elif damage == 'answer_changed':
        for checkpoint in content['__mcs_internal_proof_v1']['rows']:
            checkpoint_content = json.loads(checkpoint['content'])
            origin = checkpoint_content.get('__quality_origin_v1')
            if isinstance(origin, dict) and isinstance(origin.get('answer_text'), str):
                origin['answer_text'] += ' '
                checkpoint['content'] = json.dumps(checkpoint_content, ensure_ascii=False)
                break
        else:
            pytest.fail('missing paid answer in protected checkpoint')
    elif damage == 'repair_answer_changed':
        import hashlib
        for checkpoint in content['__mcs_internal_proof_v1']['rows']:
            checkpoint_content = json.loads(checkpoint['content'])
            checkpoint_meta = json.loads(checkpoint['meta'])
            repair = checkpoint_content.get('__quality_repair_v1')
            if not isinstance(repair, dict) or ('split_size' in checkpoint_meta) != (split == 'repair'):
                continue
            response = repair['response']
            receipt = cloud_policy.reservation_evidence(led.db, response['reservation_artifact_id'])
            expected_finish = 'length' if split == 'repair' else 'stop'
            assert receipt['result']['answer_finish_reason_v1'] == expected_finish
            assert receipt['result']['answer_text_sha256_v1'] == hashlib.sha256(response['answer_text'].encode()).hexdigest()
            response['answer_text'] += ' '
            checkpoint['content'] = json.dumps(checkpoint_content, ensure_ascii=False)
            if 'provider_leaf' in checkpoint_meta:
                checkpoint_meta['provider_leaf']['result_hash'] = worker._provider_result_hash(checkpoint['content'])
                checkpoint['meta'] = json.dumps(checkpoint_meta, ensure_ascii=False)
            else:
                assert expected_finish == 'length'
            assert receipt['result']['answer_text_sha256_v1'] != hashlib.sha256(response['answer_text'].encode()).hexdigest()
            break
        else:
            pytest.fail('missing requested paid repair answer')
        original_evidence = cloud_inference.verify_cloud_repair_evidence
        def evidence(*args, **kwargs):
            try:
                return original_evidence(*args, **kwargs)
            except ValueError:
                evidence_failures.append((kwargs['reservation_id'], kwargs['finish_reason']))
                raise
        monkeypatch.setattr(cloud_inference, 'verify_cloud_repair_evidence', evidence)
    if damage in ('proof_missing', 'answer_changed', 'repair_answer_changed'):
        with led.db:
            changed_meta = json.loads(final['meta'])
            if damage in ('answer_changed', 'repair_answer_changed'):
                changed_meta['internal_proof_hash'] = worker._internal_proof_hash(content['__mcs_internal_proof_v1'])
            led.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                           (json.dumps(content, ensure_ascii=False), json.dumps(changed_meta), final['artifact_id']))
    elif damage == 'receipt_missing':
        with led.db:
            rows = led.artifacts('cloud_usage_result', project_id=1, message_id=1)
            assert rows
            if split == 'repair':
                rows = [row for row in rows if json.loads(row['meta']).get('model') == 'gpt-6.1-sol'
                        and json.loads(row['meta']).get('answer_finish_reason_v1') == 'length']
                assert len(rows) == 1
            led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (rows[-1]['artifact_id'],))
    expected = 'complete' if damage == 'none' else 'attention'
    assert worker.progress_for_message(led.db, message, model='gpt-6-luna')['state'] == expected
    if damage == 'repair_answer_changed':
        assert evidence_failures == [(response['reservation_id'], expected_finish)]
    assert len(sends) == first_sends
