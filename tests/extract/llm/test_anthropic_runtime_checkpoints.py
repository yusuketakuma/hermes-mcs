"""Synthetic anthropic runtime checkpoints contracts with isolated shared fixtures."""
import extract_llm_engine
import extract_llm_prepare
import extract_llm_progress
import extract_llm_proofs
import extract_llm_provider
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
import json
import time
import pytest
from anthropic_client import AnthropicClient
import cloud_policy
from cloud_policy import record_result as real_record_result
from cloud_policy import usage_reserver as real_usage_reserver
import extract_llm as worker
import cloud_inference
from extract_testkit import _hash, _ledger, _message
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
    monkeypatch.setattr(extract_llm_runner, '_persist_chunks', worker._persist_chunks)
    with legacy_cloud_routes():
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
    monkeypatch.setattr(extract_llm_runner, '_persist_chunks', worker._persist_chunks)
    with legacy_cloud_routes():
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
    monkeypatch.setattr(extract_llm_runner, '_persist_chunks', worker._persist_chunks)
    with legacy_cloud_routes():
        if stop == "owner":
            with pytest.raises(RuntimeError, match="synthetic-owner-checkpoint-stop"):
                worker.run_pending(led, limit=2, workers=2, budget_s=180, cloud_only=True)
        else:
            assert worker.run_pending(led, limit=2, workers=2, budget_s=180, cloud_only=True)["done"] == 0
    assert clock() - started < 3 and rejected
    assert 1 <= len(posts) <= 2 and not [post for post in posts if post["after_rejection"]]
    assert not _current_artifacts(led)


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
    monkeypatch.setattr(extract_llm_engine, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_prepare, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_provider, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_resident, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_selection, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_source_contract, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_store, 'HOME', worker.HOME)
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
    monkeypatch.setattr(extract_llm_runner, '_persist_chunks', worker._persist_chunks)
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
    monkeypatch.setattr(extract_llm_provider, '_probe_format', worker._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', worker._probe_format)

    with legacy_cloud_routes():
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
