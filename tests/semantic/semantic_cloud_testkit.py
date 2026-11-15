"""Synthetic cloud fixture and accounting helpers contracts."""
import extract_llm_context
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_store
import semantic_due_selection
import semantic_job_processor
import json
import time
import pytest
import anthropic_client
import cloud_policy
import extract_llm
import cloud_inference
import semantic_drain
import writer_lock
from semantic_testkit import _ledger, _message, _patient, _cfg


def snapshot(led):
    with led.db:
        led.db.execute("UPDATE messages SET first_seen=?", (time.time() - 601,))
        led.db.execute("UPDATE message_revisions SET observed_at=?", (time.time() - 601,))
        cloud_policy.capture_history_tx(led.db, "synthetic-resume", time.time())


def continuous_snapshot(led):
    command = "synthetic-continuous-resume"
    meta = {"command_id": command, "actor": "synthetic", "reason": "synthetic"}
    receipt = {**meta, "outcome": "applied", "control": "running"}
    with led.db:
        led.db.execute("BEGIN IMMEDIATE")
        cloud_policy.capture_history_tx(led.db, command, time.time(), continuous=True)
        led.db.execute("INSERT INTO artifacts(kind,content,meta) VALUES('cloud_control','running',?)",
                       (json.dumps(meta),))
        led.db.execute("INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
                       (command, "synthetic-hash", None, None, "applied", json.dumps(receipt), time.time()))


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    _patient(led)
    led.save_messages([_message()])
    led.semantic_seed(1, [1], {"source": "history_import"})
    snapshot(led)
    job = dict(led.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone())
    cfg = _cfg("assist", fact_source="shadow", summary_mode="shadow",
               loop_mode="shadow", job_budget_seconds=1260)
    cfg["cloud_llm"] = {"mode": "on", "project_ids": [1]}
    monkeypatch.setattr(extract_llm, "load_config", lambda *a: cfg)
    monkeypatch.setattr(extract_llm_context, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda *a: cfg)
    monkeypatch.setattr(semantic_drain, "load_config", lambda *a: cfg)
    monkeypatch.setattr(semantic_due_selection, 'load_config', semantic_drain.load_config)
    monkeypatch.setattr(semantic_job_processor, 'load_config', semantic_drain.load_config)
    monkeypatch.setattr(cloud_policy, "api_key", lambda *a: "synthetic-key")
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 1))
    calls = []

    def request(self, method, body, timeout, **kw):
        assert not led.db.in_transaction
        calls.append(body)
        return 200, {}, json.dumps({
            "model": anthropic_client.MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": '{"facts": []}'}],
            "usage": {"input_tokens": 10, "output_tokens": 10}}).encode()

    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", request)
    yield led, cfg, job, calls
    led.close()


def direct(call, *args, **kw):
    return call(*args, **kw)


def rows(led, kind):
    return led.db.execute("SELECT * FROM artifacts WHERE kind=?", (kind,)).fetchall()
