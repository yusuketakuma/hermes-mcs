"""A synthetic local writer commits while real semantic cloud transport waits."""
import json
import os
import subprocess
import sys
import time

import anthropic_client
import cloud_policy
import semantic_drain
import semantic_store
import semantic_transport
import writer_lock
from candidate_testkit import approved_config
from semantic_testkit import _canonical_cfg, _llm_v2, _message, _PassJev
from test_semantic_cloud import cloud, rows, snapshot
from test_cloud_response_recovery import legacy_cloud_routes

__all__ = ["cloud"]


_CHILD = r'''
import fcntl, os, socket, sqlite3, sys

def denied(*args, **kwargs):
    raise AssertionError("synthetic child network denied")
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
assert sys.stdin.readline() == "go\n"
fd = os.open(sys.argv[1], os.O_WRONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    db = sqlite3.connect(sys.argv[2], timeout=0)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO synthetic_local_write VALUES(1)")
        db.commit()
        assert not db.in_transaction
    finally:
        db.close()
finally:
    os.close(fd)
print("committed", flush=True)
'''


def test_semantic_http_wait_allows_local_db_commit_and_retakes_run_lock(cloud, tmp_path, monkeypatch):
    led, cfg, _, calls = cloud
    cfg.clear()
    cfg.update(approved_config({**_canonical_cfg(), "cloud_llm": {"mode": "on", "project_ids": [1]}}))
    led.save_messages([_message(body="アムロジピン5mgを継続します。")])
    led.semantic_seed(1, [1], {"source": "history_import"})
    snapshot(led)
    with led.db:
        led.db.execute("CREATE TABLE synthetic_local_write(marker INTEGER PRIMARY KEY)")
    db_path = led.db.execute("PRAGMA database_list").fetchone()[2]
    lock_path = tmp_path / "synthetic-run.lock"
    lock_fd = writer_lock.acquire_run_lock(str(lock_path))
    assert lock_fd is not None
    observed = []
    generation = writer_lock.updater_state()

    def request(self, method, body, timeout, **kwargs):
        assert not led.db.in_transaction
        if not observed:
            proc = subprocess.Popen([sys.executable, "-I", "-c", _CHILD, str(lock_path), db_path],
                cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                stdout, stderr = proc.communicate("go\n", timeout=10)
                assert proc.returncode == 0, stderr
                assert stdout.strip() == "committed"
                assert led.db.execute("SELECT marker FROM synthetic_local_write").fetchall()[0][0] == 1
                observed.append("local_committed_during_http")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
        calls.append(body)
        return 200, {}, json.dumps({"model": anthropic_client.MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": _llm_v2(body["messages"][0]["content"])}],
            "usage": {"input_tokens": 10, "output_tokens": 10}}).encode()

    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", request)
    monkeypatch.setattr(semantic_transport, "llm_chat", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("local GPU must not be called")))
    result = {"errors": []}
    try:
        with legacy_cloud_routes():
            out = semantic_drain.run_due(led, cfg, result, time.monotonic() + 1200,
                jev_client=_PassJev(), lane="backlog", max_jobs=1, cloud_only=True, run_lock_fd=lock_fd)
        assert out["done"] == 1 and not result["errors"], (out, result)
        assert observed == ["local_committed_during_http"]
        assert writer_lock.updater_state() == generation
        assert writer_lock.acquire_run_lock(str(lock_path)) is None
        assert calls
        assert len(rows(led, "cloud_usage_reserve")) == len(rows(led, "cloud_usage_result")) == len(calls)
        assert all(json.loads(row["meta"])["http_class"] == "http_200"
                   for row in rows(led, "cloud_usage_result"))
        assert not rows(led, "cloud_usage_disposition")
        assert not cloud_policy.usage_report(led)["pricing_unverified"]
        fingerprint = semantic_store.thread_bundle(led.db, 1, 1)["source_fingerprint"]
        assert semantic_store.current_artifact(led.db, "semantic_summary", 1, fingerprint) is not None
        assert not led.db.in_transaction
    finally:
        os.close(lock_fd)
