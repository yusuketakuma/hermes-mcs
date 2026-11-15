"""Synthetic anthropic runtime checkpoints contracts with isolated shared fixtures."""
import extract_llm_engine
import extract_llm_prepare
import extract_llm_progress
import extract_llm_proofs
import extract_llm_runner
import json
import time
import pytest
from anthropic_client import AnthropicClient
import cloud_policy
from cloud_policy import record_result as real_record_result
from cloud_policy import usage_reserver as real_usage_reserver
import extract_llm as worker
import cloud_inference
from extract_testkit import _ledger, _message
from anthropic_runtime_testkit import _assert_legacy_retained as _assert_legacy_retained, _bound_source as _bound_source, _current_artifacts as _current_artifacts, _split_checkpoint_cloud_jobs as _split_checkpoint_cloud_jobs, _use_real_cloud_accounting as _use_real_cloud_accounting, routed as routed
from test_cloud_response_recovery import legacy_cloud_routes


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
        with legacy_cloud_routes():
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
        with legacy_cloud_routes():
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
    monkeypatch.setattr(extract_llm_engine, 'plan_chunks', worker.plan_chunks)
    monkeypatch.setattr(extract_llm_prepare, 'plan_chunks', worker.plan_chunks)
    monkeypatch.setattr(extract_llm_progress, 'plan_chunks', worker.plan_chunks)
    monkeypatch.setattr(extract_llm_proofs, 'plan_chunks', worker.plan_chunks)
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
        with legacy_cloud_routes():
            reservation = real_usage_reserver(ledger, cfg, route="reextract", project_id=1, message_id=1,
                unit_key="1", owner={"lease": 123.0}, selection=selection)(request, 30)
        usage = {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3,
                 "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}
        with legacy_cloud_routes():
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
    monkeypatch.setattr(extract_llm_engine, '_llm_call', worker._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', worker._llm_call)
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

    def recorded(ledger, reservation, outcome, **answer):
        metadata.append(dict(outcome))
        return record(ledger, reservation, outcome, **answer)

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
    with legacy_cloud_routes(), pytest.raises(cloud_inference.CloudBusy, match="synthetic-before-next-child"):
        worker.llm_extract(body, deadline=deadline, provider=provider, hints={}, chunks_out=completed,
                           on_split=lambda key, size: splits.__setitem__(key, size))
    assert len(completed) == 1 and splits and len(prompts) == 2
    paid_prefix = list(prompts)
    meta = {}
    with legacy_cloud_routes():
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

    def recorded(ledger, reservation, outcome, **answer):
        metadata.append(dict(outcome))
        return record(ledger, reservation, outcome, **answer)

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
    with legacy_cloud_routes(), pytest.raises(cloud_inference.CloudFallback, match="cloud_unit_failed"):
        worker.llm_extract("完全合成の本文です。", provider=provider, deadline=deadline,
                           chunks_out=completed, on_split=lambda *args: splits.append(args))
    assert not splits and not completed
    assert len(calls) == len(reservations) == len(results) == len(metadata) == 1
    assert metadata[0]["error_kind"] == "protocol_error"


