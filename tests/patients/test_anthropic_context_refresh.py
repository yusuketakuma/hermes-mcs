"""Synthetic per-unit context routing; no keys, GPU, HTTP or source data."""
import json

import pytest

import extract_llm as worker
import patient_context_refresh as context_refresh
from test_patient_context_refresh import CFG, document, runtime, seed, stamp, step

__all__ = ["runtime"]


@pytest.fixture
def cloud(runtime, monkeypatch):
    led, clock, local_calls = runtime
    requests = []
    mode = ["on"]
    failure = [False]
    monkeypatch.setattr(worker.cloud_policy, "api_key", lambda root=None: "synthetic-key")
    monkeypatch.setattr(worker, "_cloud_settings", lambda cfg: worker.cloud_policy.cloud_config(
        {"cloud_llm": {"mode": mode[0], "project_ids": [1]}})[0])
    monkeypatch.setattr(worker, "_cloud_mode", lambda *args: mode[0])
    class Provider:
        model = "claude-haiku-5-5"
        def __init__(self, ledger, cfg, **kwargs):
            assert ledger is led
            self.current = kwargs["current"]
            self.contract = worker._cloud_contract(cfg)
        def guard(self):
            if not self.current():
                raise worker.CloudFallback("synthetic_stale")
        def __call__(self, prompt, **kwargs):
            self.guard()
            if failure[0]:
                raise worker.CloudFallback("synthetic_failure")
            payload = json.loads(prompt)
            requests.append(payload)
            if "reports" in payload:
                result = {"fact_ids": [item["id"] for item in payload["reports"][:12]]}
            else:
                body = payload["source"]
                result = {"patient_context": [{"category": "living", "text": body,
                    "evidence": body, "subject": "unspecified", "details": []}]}
            return {"status": 200, "finish_reason": "stop", "text": json.dumps(result)}
    monkeypatch.setattr(worker, "_CloudProvider", Provider)
    return requests, mode, failure


def drain(runtime, *, local_admit=lambda: True):
    for _ in range(30):
        result = step(runtime, local_admit=local_admit)
        if result["status"] == "complete":
            return
        assert result["status"] == "progressed", result
    pytest.fail("synthetic context did not finish")


def test_daytime_cloud_needs_no_local_gpu_capacity(runtime, cloud):
    led, clock, local_calls = runtime
    clock.wall = stamp("2026-10-18T12:00:00")
    seed(led, body="見出しのない完全合成の居住記録。")
    assert led.db.execute("SELECT first_seen FROM messages WHERE message_id=1").fetchone()[0] <= clock.wall
    drain(runtime, local_admit=lambda: False)
    assert cloud[0] and not local_calls
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG)
    scan = led.db.execute("SELECT meta FROM artifacts WHERE kind=?", (context_refresh.SCAN,)).fetchone()
    meta = json.loads(scan[0])
    assert meta["provider"] == worker._cloud_contract(CFG)
    assert meta["model"] == "claude-haiku-5-5"
    assert meta["contract"] == context_refresh._contract(CFG, 1)


def test_failed_cloud_retries_local_with_its_model(runtime, cloud):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[2][0] = True
    drain(runtime)
    assert local_calls and not cloud[0]
    for row in led.db.execute("SELECT meta FROM artifacts WHERE kind IN (?,?)", (context_refresh.KIND, context_refresh.SCAN)):
        meta = json.loads(row[0])
        assert "provider" not in meta and meta["model"] == "synthetic"


def test_failed_cloud_does_not_bypass_local_admission(runtime, cloud):
    led, _, local_calls = runtime
    seed(led)
    # An explicit source fact makes the first summary actually call its provider.
    assert context_refresh._snapshot(led.db, 1, CFG)["sources"][0]["items"]
    cloud[2][0] = True
    result = step(runtime, local_admit=lambda: False)
    assert result["status"] == "deferred"
    assert not local_calls and document(led) is None


def test_shadow_runs_only_after_local_commit_and_never_publishes_cloud(runtime, cloud, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    cloud[1][0] = "shadow"
    original = worker._CloudProvider
    class Shadow(original):
        def __call__(self, prompt, **kwargs):
            assert not led.db.in_transaction
            payload = json.loads(prompt)
            kind = context_refresh.KIND if "reports" in payload else context_refresh.SCAN
            assert led.db.execute("SELECT 1 FROM artifacts WHERE kind=?", (kind,)).fetchone()
            return super().__call__(prompt, **kwargs)
    monkeypatch.setattr(worker, "_CloudProvider", Shadow)
    drain(runtime)
    assert local_calls and cloud[0]
    assert led.artifacts("cloud_compare_v1")
    for row in led.db.execute("SELECT meta FROM artifacts WHERE kind IN (?,?)", (context_refresh.KIND, context_refresh.SCAN)):
        assert "provider" not in json.loads(row[0])


def test_crossing_0330_rejects_cloud_publication(runtime, cloud, monkeypatch):
    led, clock, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    clock.wall = stamp("2026-10-18T03:20:00")
    original = worker._CloudProvider
    class Crossing(original):
        def __call__(self, prompt, **kwargs):
            answer = super().__call__(prompt, **kwargs)
            clock.wall = stamp("2026-10-18T03:30:00")
            return answer
    monkeypatch.setattr(worker, "_CloudProvider", Crossing)
    # First unit publishes the empty deterministic selection; the scan sends next.
    step(runtime)
    result = step(runtime)
    assert result["status"] == "deferred"
    assert not led.artifacts(context_refresh.SCAN)


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_cloud_completed_data_is_not_reused_after_route_change(runtime, cloud, mode):
    led, _, local_calls = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    drain(runtime)
    assert not local_calls
    cloud[1][0] = mode
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    snapshot, _, pending = context_refresh._pending(led.db, 1, CFG)
    assert pending["A"] and pending["B"]
    assert not context_refresh._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])
    drain(runtime)
    assert local_calls and context_refresh.context_view_input(led.db, 1, cfg=CFG)
    for row in led.db.execute("SELECT meta FROM artifacts WHERE kind IN (?,?)", (context_refresh.KIND, context_refresh.SCAN)):
        assert "provider" not in json.loads(row[0])


def test_partial_cloud_scan_requires_same_code_and_config(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led)
    snapshot = context_refresh._snapshot(led.db, 1, CFG)
    source = snapshot["sources"][0]
    meta = {"source_sig": context_refresh._source_sig(source), "contract": snapshot["contract"],
            "provider": {**snapshot["contract"]["provider"], "config_generation": "0" * 64}}
    with led.db:
        context_refresh._replace(led, context_refresh.SCAN, 1, {"cursor": 1, "complete": False, "facts": []}, meta, 1)
    assert not context_refresh._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])
    meta["provider"] = snapshot["contract"]["provider"]
    with led.db:
        context_refresh._replace(led, context_refresh.SCAN, 1, {"cursor": 1, "complete": False, "facts": []}, meta, 1)
    assert context_refresh._current_scans(led.db, 1, snapshot["sources"], snapshot["contract"])[1]["cursor"] == 1
    monkeypatch.setattr(worker, "_upgrade_contract", lambda: "f" * 64)
    changed, _, pending = context_refresh._pending(led.db, 1, CFG)
    assert pending["B"] and changed["source_sig"] != snapshot["source_sig"]
    assert not context_refresh._current_scans(led.db, 1, changed["sources"], changed["contract"])


def test_scope_is_rechecked_after_writer_acquisition(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    original = worker._CloudProvider
    checked = []
    class RevokedAtCommit(original):
        def guard(self):
            checked.append(led.db.in_transaction)
            if led.db.in_transaction:
                raise worker.CloudFallback("synthetic_changed_while_waiting")
            return super().guard()
    monkeypatch.setattr(worker, "_CloudProvider", RevokedAtCommit)
    step(runtime)
    assert step(runtime)["status"] == "deferred"
    assert True in checked and not led.artifacts(context_refresh.SCAN)
