"""Synthetic provenance boundaries for adopted extraction results."""
import json

import pytest

import extract
import extract_llm as worker
import cloud_inference
import inference_dependency_loss
import mcs_util
from extract_testkit import _ledger, _message


def _local(monkeypatch, outputs):
    answers = iter(outputs)
    monkeypatch.setattr(worker, "_probe_format", lambda **kw: "plain")
    monkeypatch.setattr(worker.local_llm, "admission_enabled", lambda: False)
    def chat(*args, **kwargs):
        answer = next(answers)
        if "items" not in answer:
            answer = {"symptoms": [], **answer}
        return {"status": 200, "finish_reason": "stop", "text": json.dumps(answer)}
    monkeypatch.setattr(worker.local_llm, "chat", chat)


@pytest.mark.parametrize("name", ["anthropic", "openai"])
def test_cloud_result_is_identified_by_the_actual_provider(name):
    class Provider(cloud_inference.CloudInferenceUnit):
        model = worker.anthropic_client.MODEL if name == "anthropic" else worker.openai_client.MODEL

        def __init__(self):
            pass

        def __call__(self, *args, **kwargs):
            return {"status": 200, "finish_reason": "stop", "text": '{"summary":"合成","symptoms":[]}'}

    meta = {}
    result = worker.llm_extract("完全合成の記録です。", provider=Provider(), meta_out=meta)
    assert result is not None
    assert meta["_provider_evidence"] == {"providers": [name], "complete": True}


def test_local_result_and_old_checkpoint_are_distinguished(monkeypatch):
    _local(monkeypatch, [{"summary": "合成"}])
    meta = {}
    result = worker.llm_extract("完全合成の記録です。", meta_out=meta)
    assert result is not None
    assert meta["_provider_evidence"] == {"providers": ["local"], "complete": True}
    cached_meta = {}
    cached = worker.llm_extract("完全合成の記録です。", chunks_in={0: result}, meta_out=cached_meta)
    assert cached is not None
    assert cached_meta["_provider_evidence"] == {"providers": [], "complete": False}


def test_mixed_cached_and_fresh_chunks_are_incomplete(monkeypatch):
    _local(monkeypatch, [{"summary": "合成"}])
    body = "合成記録一。合成記録二。"
    meta = {}
    result = worker.llm_extract(body, chunk_size=len("合成記録一。"),
                                chunks_in={0: {"summary": "合成"}}, meta_out=meta)
    assert result is not None
    assert meta["_provider_evidence"] == {"providers": ["local"], "complete": False}


def _checkpointed(ledger, monkeypatch):
    ledger.save_messages([_message(body="完全合成の記録です。")])
    row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = inference_dependency_loss.source_binding(ledger.db, row["project_id"], 1)
    row["_target"] = ("local", "synthetic")
    _local(monkeypatch, [{"summary": "合成"}])
    meta = {}
    with worker.pin_target(row["_target"]):
        result = worker.llm_extract(row["body_text"], meta_out=meta,
            on_chunk=lambda key, value: worker._persist_chunks(ledger, row, {key: value}))
    assert result is not None and meta["_provider_evidence"]["complete"] is True
    return row


def test_new_checkpoint_restores_evidence_after_restart(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    try:
        row = _checkpointed(ledger, monkeypatch)
        ledger.close()
        ledger = _ledger(tmp_path)
        _, saved = worker._saved_chunk_state(ledger.db, row)
        assert worker._leaf_evidence(saved[0]) == {"providers": ["local"], "complete": True}
        meta = {}
        with worker.pin_target(row["_target"]):
            result = worker.llm_extract(row["body_text"], chunks_in=saved, meta_out=meta)
        assert result is not None
        assert meta["_provider_evidence"] == {"providers": ["local"], "complete": True}
    finally:
        ledger.close()


def test_interrupted_multichunk_resumes_with_verified_leaf(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    try:
        body = "合成記録一。合成記録二。"
        size = len("合成記録一。")
        ledger.save_messages([_message(body=body)])
        row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        row["_source_binding"] = inference_dependency_loss.source_binding(ledger.db, row["project_id"], 1)
        row["_target"] = ("local", "synthetic")
        _local(monkeypatch, [{"summary": "合成一"}])
        original = worker._llm_call
        calls = []

        def stop_after_first(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs) if len(calls) == 1 else worker._DEFERRED

        monkeypatch.setattr(worker, "_llm_call", stop_after_first)
        with worker.pin_target(row["_target"]):
            first = worker.llm_extract(body, chunk_size=size,
                on_chunk=lambda key, value: worker._persist_chunks(
                    ledger, row, {key: value}, chunk_size=size))
        assert first is worker._DEFERRED
        ledger.close()
        ledger = _ledger(tmp_path)
        _, saved = worker._saved_chunk_state(ledger.db, row, chunk_size=size)
        assert worker._leaf_evidence(saved[0]) == {"providers": ["local"], "complete": True}
        monkeypatch.setattr(worker, "_llm_call", original)
        _local(monkeypatch, [{"summary": "合成二"}])
        meta = {}
        with worker.pin_target(row["_target"]):
            result = worker.llm_extract(body, chunk_size=size, chunks_in=saved, meta_out=meta)
        assert result is not None and result is not worker._DEFERRED
        assert meta["_provider_evidence"] == {"providers": ["local"], "complete": True}
    finally:
        ledger.close()


@pytest.mark.parametrize("tamper", ["content", "digest", "binding", "plan", "contract", "provider", "mixed"])
def test_checkpoint_evidence_fails_closed_on_tampering(tmp_path, monkeypatch, tamper):
    ledger = _ledger(tmp_path)
    try:
        row = _checkpointed(ledger, monkeypatch)
        checkpoint = ledger.artifacts("extract_llm_chunk", message_id=1)[-1]
        meta = json.loads(checkpoint["meta"])
        content = checkpoint["content"]
        if tamper == "content":
            value = json.loads(content)
            value["summary"] = "別の合成結果"
            content = json.dumps(value)
        elif tamper in ("provider", "mixed"):
            meta["provider_leaf"]["providers"] = (["anthropic"] if tamper == "provider"
                                                     else ["local", "anthropic"])
        else:
            field = {"digest": "result_hash", "binding": "source_binding",
                     "plan": "plan_version", "contract": "provider_contract"}[tamper]
            meta["provider_leaf"][field] = "changed"
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?",
                              (content, json.dumps(meta), checkpoint["artifact_id"]))
        _, saved = worker._saved_chunk_state(ledger.db, row)
        assert 0 in saved and worker._leaf_evidence(saved[0]) is None
        run_meta = {}
        with worker.pin_target(row["_target"]):
            result = worker.llm_extract(row["body_text"], chunks_in=saved, meta_out=run_meta)
        assert result is not None and run_meta["_provider_evidence"]["complete"] is False
    finally:
        ledger.close()


def test_batch_marks_only_routed_valid_items(monkeypatch):
    _local(monkeypatch, [{"items": [{"i": 0, "summary": "合成", "symptoms": []},
                                     {"i": 1, "summary": "合成", "symptoms": []},
                                     {"i": 1, "summary": "重複", "symptoms": []}]}])
    rows = [(i, {"body_text": "合成の記録です。", "posted_at": "2032-06-01"},
             None, {}, None, None, None) for i in range(2)]
    status, results, meta = worker._run_batch_call(rows, ("local", "model"), 10**12, None)
    assert status == "ok" and set(results) == {0}
    assert meta["_provider_evidence"] == {"providers": ["local"], "complete": True}


def test_prior_evidence_requires_exact_content_and_source():
    content = '{"summary":"合成"}'
    proof = {"version": 1, "stage": "extract", "providers": ["anthropic"],
             "complete": True, "result_hash": mcs_util.payload_hash(json.loads(content)),
             "source_binding": "source-1"}
    prior = {"content": content, "model": worker.anthropic_client.MODEL,
             "meta": json.dumps({"source_binding": "source-1", **cloud_inference._CLOUD_CONTRACT,
                                 "actual_model": worker.anthropic_client.MODEL,
                                 "provider_provenance": proof})}
    assert worker._prior_provider_evidence(prior, content, "source-1") == {
        "providers": ["anthropic"], "complete": True}
    for other_content, binding in ((content, "source-2"), ('{"summary":"変更"}', "source-1")):
        assert worker._prior_provider_evidence(prior, other_content, binding) == {
            "providers": [], "complete": False}
    prior["meta"] = "{}"
    assert not worker._prior_provider_evidence(prior, content, "source-1")["complete"]


@pytest.mark.parametrize("names", [["anthropic"], ["local", "anthropic"]])
def test_prior_provider_change_cannot_override_local_route(names):
    content = '{"summary":"合成"}'
    proof = {"version": 1, "stage": "extract", "providers": names, "complete": True,
             "result_hash": mcs_util.payload_hash(json.loads(content)), "source_binding": "source-1"}
    prior = {"content": content, "model": "synthetic-local",
             "meta": json.dumps({"source_binding": "source-1", "actual_model": "synthetic-local",
                                 "provider_provenance": proof})}
    assert worker._prior_provider_evidence(prior, content, "source-1") == {
        "providers": [], "complete": False}


@pytest.mark.parametrize("tamper", ["meta_duplicate", "proof_duplicate", "meta_nonfinite",
                                   "content_duplicate", "content_nonfinite"])
def test_prior_json_ambiguity_cannot_supply_provenance(tamper):
    content = '{"summary":"合成"}'
    proof = {"version": 1, "stage": "extract", "providers": ["local"], "complete": True,
             "result_hash": mcs_util.payload_hash(json.loads(content)), "source_binding": "source-1"}
    meta = json.dumps({"source_binding": "source-1", "actual_model": "synthetic-local",
                       "provider_provenance": proof})
    if tamper == "meta_duplicate":
        meta = meta[:-1] + ', "extra": 1, "extra": 1}'
    elif tamper == "proof_duplicate":
        meta = meta.replace('"complete": true', '"complete": true, "complete": true')
    elif tamper == "meta_nonfinite":
        meta = meta[:-1] + ', "extra": 1e999}'
    elif tamper == "content_duplicate":
        content = '{"summary":"合成", "summary":"合成"}'
    else:
        content = '{"summary":"合成", "extra":NaN}'
    prior = {"content": content, "meta": meta, "model": "synthetic-local"}
    assert worker._prior_provider_evidence(prior, content, "source-1") == {
        "providers": [], "complete": False}


@pytest.mark.parametrize("tamper", ["meta_duplicate", "proof_duplicate", "meta_nonfinite",
                                   "content_duplicate", "content_nonfinite"])
def test_checkpoint_json_ambiguity_keeps_cache_but_drops_provenance(tmp_path, monkeypatch, tamper):
    ledger = _ledger(tmp_path)
    try:
        row = _checkpointed(ledger, monkeypatch)
        checkpoint = ledger.artifacts("extract_llm_chunk", message_id=1)[-1]
        raw_meta, raw_content = checkpoint["meta"], checkpoint["content"]
        if tamper == "meta_duplicate":
            raw_meta = raw_meta[:-1] + ', "extra": 1, "extra": 1}'
        elif tamper == "proof_duplicate":
            raw_meta = raw_meta.replace('"complete": true', '"complete": true, "complete": true')
        elif tamper == "meta_nonfinite":
            raw_meta = raw_meta[:-1] + ', "extra": 1e999}'
        elif tamper == "content_duplicate":
            raw_content = raw_content[:-1] + ', "summary": "合成"}'
        else:
            raw_content = raw_content[:-1] + ', "extra": NaN}'
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?",
                              (raw_content, raw_meta, checkpoint["artifact_id"]))
        _, saved = worker._saved_chunk_state(ledger.db, row)
        assert 0 in saved and saved[0]["summary"] == "合成"
        assert worker._leaf_evidence(saved[0]) is None
        meta = {}
        with worker.pin_target(row["_target"]):
            result = worker.llm_extract(row["body_text"], chunks_in=saved, meta_out=meta)
        assert result is not None and meta["_provider_evidence"]["complete"] is False
    finally:
        ledger.close()


def test_final_receipt_is_bound_and_stale_source_cannot_write(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    try:
        ledger.save_messages([_message(body="完全合成の記録です。")])
        monkeypatch.setattr(worker.writer_lock, "updater_state", lambda: (False, 1))
        monkeypatch.setattr(worker, "_WRITER_STATE", (False, 1))
        monkeypatch.setattr(extract, "_WRITER_STATE", (False, 1))
        monkeypatch.setattr(worker, "_rerender_cards", lambda *a, **kw: None)
        row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        row["_source_binding"] = inference_dependency_loss.source_binding(ledger.db, row["project_id"], 1)
        row["_provider_evidence"] = {"providers": ["local"], "complete": True}
        content = '{"summary":"合成"}'
        assert worker._replace_current(ledger, row, content)
        saved = ledger.artifacts(worker.KIND, message_id=1)[-1]
        proof = json.loads(saved["meta"])["provider_provenance"]
        assert proof == {"version": 1, "stage": "extract", "providers": ["local"],
                         "complete": True, "result_hash": mcs_util.payload_hash(json.loads(content)),
                         "source_binding": row["_source_binding"]}
        row["_kept_prior"] = True
        row["_provider_evidence"] = {"providers": ["anthropic"], "complete": True}
        assert worker._replace_current(ledger, row, content)
        retained = json.loads(ledger.artifacts(worker.KIND, message_id=1)[-1]["meta"])["provider_provenance"]
        assert retained == proof
        row["_source_binding"] = "stale"
        assert not worker._replace_current(ledger, row, '{"summary":"古い対象"}')
        assert len(ledger.artifacts(worker.KIND, message_id=1)) == 1
    finally:
        ledger.close()


def test_prefilter_does_not_claim_a_provider(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    try:
        ledger.save_messages([_message(body="こんにちは。")])
        monkeypatch.setattr(worker.writer_lock, "updater_state", lambda: (False, 1))
        monkeypatch.setattr(worker, "_WRITER_STATE", (False, 1))
        monkeypatch.setattr(extract, "_WRITER_STATE", (False, 1))
        monkeypatch.setattr(worker, "_rerender_cards", lambda *a, **kw: None)
        row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        row["_source_binding"] = inference_dependency_loss.source_binding(ledger.db, row["project_id"], 1)
        assert worker._mark_prefiltered(ledger, row)
        meta = json.loads(ledger.artifacts(worker.KIND, message_id=1)[-1]["meta"])
        assert meta["prefilter"] == "no_signal"
        assert meta["provider_provenance"]["providers"] == []
        assert meta["provider_provenance"]["complete"] is False
    finally:
        ledger.close()


def test_failed_cloud_falls_back_to_local_without_cloud_evidence(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    try:
        ledger.save_messages([_message(body="完全合成の記録です。")])
        monkeypatch.setattr(worker.writer_lock, "updater_state", lambda: (False, 1))
        monkeypatch.setattr(worker, "_WRITER_STATE", (False, 1))
        monkeypatch.setattr(extract, "_WRITER_STATE", (False, 1))
        monkeypatch.setattr(worker, "_rerender_cards", lambda *a, **kw: None)
        monkeypatch.setattr(worker, "load_config", lambda: {})
        monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
        monkeypatch.setattr(worker, "_cloud_reextract", lambda *a, **kw: True)
        monkeypatch.setattr(worker, "_cloud_mode", lambda *a, **kw: "on")
        monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: {"provider": "anthropic", "max_concurrency": 1})
        monkeypatch.setattr(worker, "_prefilter_enabled", lambda: False)
        monkeypatch.setattr(worker, "circuit_open_s", lambda db: 0)
        monkeypatch.setattr(worker, "disk_free_mb", lambda db: 100000)
        _local(monkeypatch, [{"summary": "合成"}])

        class FailedCloud:
            def __init__(self, *args, **kwargs):
                self.possibly_sent = True

            def __call__(self, *args, **kwargs):
                raise cloud_inference.CloudFallback("cloud_unit_failed")

        monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", FailedCloud)
        outcome = worker.run_pending(ledger, budget_s=300)
        assert outcome["done"] == 1
        proof = json.loads(ledger.artifacts(worker.KIND, message_id=1)[-1]["meta"])["provider_provenance"]
        assert proof["providers"] == ["local"] and proof["complete"] is True
        import processing_route
        states = processing_route.provider_states(ledger.db, 1, 1)
        assert states["local"] == "done" and states["anthropic"] == "unknown"
    finally:
        ledger.close()
