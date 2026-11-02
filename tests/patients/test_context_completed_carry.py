"""Completed patient cloud units resume saving without another paid request."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

import anthropic_client
import cloud_client
import cloud_inference
import cloud_policy
import extract_llm as worker
import ledger as ledger_module
import notify_spool
import patient_context_refresh as refresh
import semantic_runtime
from test_anthropic_context_refresh import cloud as cloud, _SOURCE_REPAIR_PROVIDER
from test_patient_context_refresh import CFG, document, runtime as runtime, seed, step


def _completed(runtime, cloud, monkeypatch, lane):
    led, clock, local_calls = runtime
    settings, errors = cloud_policy.cloud_config({"cloud_llm": {
        "mode": "on", "provider": "anthropic", "project_ids": [1],
        "daily_request_budget": 100,
    }})
    assert not errors
    cfg = {**CFG, "cloud_llm": settings}
    monkeypatch.setattr(worker, "load_config", lambda: cfg)
    monkeypatch.setattr(cloud_inference, "load_config", lambda: cfg)
    monkeypatch.setattr(cloud_inference, "resolve_cloud_settings", lambda _cfg: settings)
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", _SOURCE_REPAIR_PROVIDER)
    monkeypatch.setattr(worker.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(cloud_policy, "api_key", lambda *_args: "synthetic-key")
    monkeypatch.setattr(cloud_policy, "pricing_valid", lambda *a, **kw: True)
    monkeypatch.setattr(worker.cloud_backlog, "owner_alive", lambda: True)
    resources = {"slots": 0, "wire": 0}

    @contextmanager
    def slot(*_args):
        resources["slots"] += 1
        try:
            yield True
        finally:
            resources["slots"] -= 1

    monkeypatch.setattr(worker.cloud_backlog, "request_slot", slot)
    seed(led, body="見出しのない完全合成の居住記録。")

    def sleep(seconds):
        clock.mono += seconds
        clock.wall += seconds

    fake_time = SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono,
                               perf_counter=lambda: clock.mono, sleep=sleep)
    for module in (worker, cloud_inference, cloud_client, cloud_policy,
                   anthropic_client, ledger_module, semantic_runtime, refresh):
        monkeypatch.setattr(module, "time", fake_time)
    posts, answers = [], []
    blocked = [False]

    def request(_client, method, body, timeout, *, deadline):
        assert method == "POST" and timeout > 0 and deadline > clock.mono
        assert not led.db.in_transaction
        assert len(led.artifacts("cloud_usage_reserve", project_id=1)) == len(posts) + 1
        payload = json.loads(body["messages"][0]["content"])
        phase = "A" if "reports" in payload else "B"
        answer = ({"fact_ids": [r["id"] for r in payload["reports"]]} if phase == "A" else
                  {"patient_context": [{"category": "living", "text": payload["source"],
                    "evidence": payload["source"], "subject": "unspecified", "details": []}]})
        text = json.dumps(answer)
        posts.append(phase)
        answers.append(text)
        resources["wire"] += 1
        try:
            blocked[0] = phase == lane
            response = {"model": anthropic_client.MODEL, "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": 10, "output_tokens": 2}}
            return 200, {"Request-Id": "synthetic-completed-unit"}, json.dumps(response).encode()
        finally:
            resources["wire"] -= 1

    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", request)
    monkeypatch.setattr(refresh, "acquire_run_lock", lambda: (
        None if blocked[0] else os.open(os.devnull, os.O_RDONLY)))
    for _ in range(15):
        outcome = step(runtime, cfg=cfg, local_admit=lambda: False)
        if blocked[0]:
            break
    assert posts[-1] == lane
    assert outcome["status"] == "deferred" and outcome["reason"] == "run_lock"
    assert outcome["lock_wait_s"] == 60
    unit = led.__dict__["_patient_context_completed"]
    assert not led.db.in_transaction and resources == {"slots": 0, "wire": 0}
    assert not any(isinstance(v, sqlite3.Cursor) for v in unit.gi_frame.f_locals.values())
    assert document(led, refresh.KIND if lane == "A" else refresh.SCAN) is None
    reserves = led.artifacts("cloud_usage_reserve", project_id=1)
    results = led.artifacts("cloud_usage_result", project_id=1)
    assert len(reserves) == len(results) == len(posts)
    for row in reserves:
        evidence = cloud_policy.reservation_evidence(led.db, row["artifact_id"])
        assert evidence["result"]["http_class"] == "http_200"
        assert evidence["result"]["answer_text_sha256_v1"] in {
            hashlib.sha256(text.encode()).hexdigest() for text in answers}
    receipts = [(r["artifact_id"], r["content"], r["meta"]) for r in reserves + results]
    assert not local_calls and not cloud[0]
    return {"led": led, "cfg": cfg, "blocked": blocked, "posts": posts,
            "unit": unit, "receipts": receipts, "resources": resources}


def _receipts(led):
    return [(r["artifact_id"], r["content"], r["meta"])
            for r in (led.artifacts("cloud_usage_reserve", project_id=1)
                      + led.artifacts("cloud_usage_result", project_id=1))]


@pytest.mark.parametrize("lane", ["A", "B"])
def test_completed_cloud_unit_resumes_only_save_after_sixty_second_lock_wait(
        runtime, cloud, monkeypatch, lane):
    pending = _completed(runtime, cloud, monkeypatch, lane)
    count = len(pending["posts"])
    pending["blocked"][0] = False
    outcome = step(runtime, cfg=pending["cfg"], local_admit=lambda: False)
    assert outcome["status"] == "progressed" and outcome["lane"] == lane
    assert len(pending["posts"]) == count
    assert "_patient_context_completed" not in pending["led"].__dict__
    assert pending["unit"].gi_frame is None
    assert _receipts(pending["led"]) == pending["receipts"]
    doc = document(pending["led"], refresh.KIND if lane == "A" else refresh.SCAN)
    assert doc and (doc["summary"] if lane == "A" else doc["complete"])
    assert pending["resources"] == {"slots": 0, "wire": 0}


@pytest.mark.parametrize("changed", ["source", "config", "owner", "restore", "restore_after_lock",
                                     "receipt", "deadline", "stop"])
def test_completed_cloud_unit_rechecks_save_fences_without_resending(
        runtime, cloud, monkeypatch, changed):
    pending = _completed(runtime, cloud, monkeypatch, "B")
    led, clock, _ = runtime
    cfg = pending["cfg"]
    def admit():
        return True
    if changed == "source":
        with led.db:
            led.db.execute("UPDATE messages SET first_seen=first_seen+1 WHERE message_id=1")
    elif changed == "config":
        cfg = copy.deepcopy(cfg)
        cfg["extract"]["patient_context_nightly"]["publication"] = False
    elif changed == "owner":
        monkeypatch.setattr(worker, "_writer_current", lambda **_: False)
    elif changed == "restore":
        notify_spool.mark_restored(notify_spool.data_root(led),
                                  by="mcs_update", phase="awaiting_consent")
    elif changed == "restore_after_lock":
        acquire = refresh.acquire_run_lock
        def restore_after_lock():
            fd = acquire()
            if fd is not None:
                notify_spool.mark_restored(notify_spool.data_root(led),
                                          by="mcs_update", phase="awaiting_consent")
            return fd
        monkeypatch.setattr(refresh, "acquire_run_lock", restore_after_lock)
    elif changed == "receipt":
        with led.db:
            led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.answer_text_sha256_v1',?) "
                           "WHERE kind='cloud_usage_result'", ("f" * 64,))
    elif changed == "deadline":
        clock.mono += 1000
        clock.wall += 1000
    else:
        def admit():
            return False
    before = _receipts(led)
    count = len(pending["posts"])
    changes = led.db.total_changes
    pending["blocked"][0] = False
    result = step(runtime, cfg=cfg, admit=admit, local_admit=lambda: False)
    assert result["status"] in ("source_changed", "deferred") and result["done"] == 0
    assert document(led, refresh.SCAN) is None and not led.artifacts(refresh.CHUNK, project_id=1)
    assert len(pending["posts"]) == count and _receipts(led) == before
    if changed.startswith("restore"):
        assert led.db.total_changes == changes
        assert not led.artifacts("cloud_usage_disposition", project_id=1)
    assert "_patient_context_completed" not in led.__dict__ and pending["unit"].gi_frame is None


def test_completed_unit_preserves_callers_transaction_until_it_can_save(runtime, cloud, monkeypatch):
    pending = _completed(runtime, cloud, monkeypatch, "B")
    led, _, _ = runtime
    led.db.execute("CREATE TEMP TABLE synthetic_borrowed(value TEXT)")
    led.db.execute("INSERT INTO synthetic_borrowed VALUES('kept')")
    before = led.db.total_changes
    pending["blocked"][0] = False
    outcome = step(runtime, cfg=pending["cfg"])
    assert outcome["reason"] == "transaction_active" and led.db.in_transaction
    assert led.db.total_changes == before and led.__dict__["_patient_context_completed"] is pending["unit"]
    assert led.db.execute("SELECT value FROM synthetic_borrowed").fetchone()[0] == "kept"
    led.db.rollback()
    count = len(pending["posts"])
    assert step(runtime, cfg=pending["cfg"])["status"] == "progressed"
    assert len(pending["posts"]) == count


def test_ledger_close_releases_one_completed_unit_without_writes(runtime, cloud, monkeypatch):
    pending = _completed(runtime, cloud, monkeypatch, "B")
    led = pending["led"]
    statements = []
    led.db.set_trace_callback(statements.append)
    led.close()
    assert pending["unit"].gi_frame is None and "_patient_context_completed" not in led.__dict__
    assert statements == [] and pending["resources"] == {"slots": 0, "wire": 0}
