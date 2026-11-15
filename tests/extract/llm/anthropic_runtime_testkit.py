"""Synthetic anthropic runtime testkit contracts with isolated shared fixtures."""
import extract_llm_context
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
import json
import re
import threading
import time
import pytest
from anthropic_client import AnthropicClient
import cloud_policy
from cloud_policy import record_disposition_tx as real_record_disposition_tx
from cloud_policy import record_result as real_record_result
from cloud_policy import usage_reserver as real_usage_reserver
import extract_llm as worker
import cloud_inference
import inference_dependency_loss as dependency_loss
import inference_version as inference
import mcs_queries
from extract_testkit import _hash, _ledger, _message


@pytest.fixture
def routed(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "max_concurrency": 2}}
    monkeypatch.setattr(worker, "load_config", lambda: cfg)
    monkeypatch.setattr(extract_llm_context, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', worker.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(worker.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(worker, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm_runner, '_WRITER_STATE', worker._WRITER_STATE)
    monkeypatch.setattr(extract_llm_source_contract, '_WRITER_STATE', worker._WRITER_STATE)
    monkeypatch.setattr(worker.extract, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(cloud_policy, "api_key", lambda *a: "synthetic-key")
    monkeypatch.setattr(cloud_policy, "block_reason", lambda led, cfg, route, pid, **_kwargs:
                        None if cloud_policy.eligible(cfg, route, pid) else "scope")
    monkeypatch.setattr(worker, "disk_free_mb", lambda db: 100000)
    monkeypatch.setattr(extract_llm_resident, 'disk_free_mb', worker.disk_free_mb)
    monkeypatch.setattr(extract_llm_runner, 'disk_free_mb', worker.disk_free_mb)
    monkeypatch.setattr(worker, "_prefilter_enabled", lambda: False)
    monkeypatch.setattr(extract_llm_runner, '_prefilter_enabled', worker._prefilter_enabled)
    monkeypatch.setattr(worker, "circuit_open_s", lambda db: 0)
    monkeypatch.setattr(extract_llm_resident, 'circuit_open_s', worker.circuit_open_s)
    monkeypatch.setattr(extract_llm_runner, 'circuit_open_s', worker.circuit_open_s)
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
    def record(ledger, reservation, metadata, **_answer):
        assert threading.get_ident() == owner
        ledger.db.execute("SELECT 1").fetchone()
        results.append(reservation)
    monkeypatch.setattr(cloud_policy, "record_result", record)
    def disposition(ledger, reservation, reason):
        assert threading.get_ident() == owner and ledger.db.in_transaction
        return ledger.artifact_add_tx("cloud_usage_disposition", "{}",
                                      meta={"attempt": reservation["n"], "disposition": reason})
    monkeypatch.setattr(cloud_policy, "record_disposition_tx", disposition)
    class Client(AnthropicClient):
        def __init__(self, key):
            assert key == "synthetic-key"
        def complete(self, prompt, *, deadline, max_tokens, before_attempt, reserve_fn, after_result,
                     cache_prefix=None):
            before_attempt()
            reservation = reserve_fn({"prompt": prompt, "max_tokens": max_tokens}, 1)
            before_attempt()
            calls.append(prompt)
            after_result(reservation, {"http_class": "http_200"})
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
    monkeypatch.setattr(extract_llm_provider, '_probe_format', worker._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', worker._probe_format)
    return bodies, posts, rejected


def _use_real_cloud_accounting(monkeypatch):
    monkeypatch.setattr(cloud_policy, "usage_reserver", real_usage_reserver)
    monkeypatch.setattr(cloud_policy, "record_result", real_record_result)
    monkeypatch.setattr(cloud_policy, "record_disposition_tx", real_record_disposition_tx)
    monkeypatch.setattr(cloud_policy, "pricing_valid", lambda *args, **kwargs: True)
    monkeypatch.setattr(worker.anthropic_client, "AnthropicClient", AnthropicClient)
