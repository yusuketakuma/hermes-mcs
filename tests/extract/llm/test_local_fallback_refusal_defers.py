"""A refused local fallback defers its row instead of aborting the whole extraction pass."""
from contextlib import nullcontext
import json

import cloud_backlog
import cloud_inference
import extract_llm as worker
import extract_llm_prepare
import extract_llm_runner
from anthropic_client import AnthropicClient
from test_anthropic_runtime import old, routed as routed, _use_real_cloud_accounting
from test_cloud_response_recovery import legacy_cloud_routes


def test_refused_local_context_defers_the_row_and_the_pass_returns(routed, monkeypatch):
    ledger, cfg, _, _, _ = routed
    old(ledger)
    posts, stale = [], [False]
    _use_real_cloud_accounting(monkeypatch)
    monkeypatch.setattr(worker.cloud_backlog, "request_slot", lambda *_a: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, "owner_alive", lambda: True)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)

    def request(self, method, body, timeout, **kwargs):
        posts.append(1)
        stale[0] = True             # the unit can no longer be verified after the paid answer
        return 200, {}, json.dumps({"model": worker.anthropic_client.MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": '{"symptoms":[]}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()

    monkeypatch.setattr(AnthropicClient, "request", request)
    owns = extract_llm_runner._owns_current_source
    monkeypatch.setattr(extract_llm_runner, "_owns_current_source",
                        lambda *a, **k: not stale[0] and owns(*a, **k))
    prepare = extract_llm_prepare._prepare_jobs

    def refuse_local(ledger, rows, *args, blocked_reasons=None, **kwargs):
        if kwargs.get("allow_cloud") is False:      # the local re-prepare after the cloud attempt
            blocked_reasons["prepare_context_load_superseded"] = 1
            return [], 0, 1
        return prepare(ledger, rows, *args, blocked_reasons=blocked_reasons, **kwargs)

    monkeypatch.setattr(extract_llm_runner, "_prepare_jobs", refuse_local)

    def infer(_body, *, deadline, provider=None, **_kwargs):
        if provider is not None:
            provider("synthetic prompt", deadline=deadline, max_tokens=100)
        return {"symptoms": []}

    monkeypatch.setattr(worker, "llm_extract", infer)
    monkeypatch.setattr(extract_llm_runner, "llm_extract", worker.llm_extract)
    with legacy_cloud_routes():
        report = worker.run_pending(ledger, limit=1, budget_s=180)
    # The pass returns (it used to raise CloudFallback out of run_pending) and the row waits.
    assert len(posts) == 1 and report["done"] == 0 and report["deferred"] == 1
    assert sum(report["defer_reasons"].values()) == 1
    receipt, = ledger.artifacts("cloud_usage_result")
    assert json.loads(receipt["meta"])["http_class"] == "http_200"


def test_cloud_fallback_code_is_observed_without_free_text():
    assert cloud_backlog._exception_observation(
        cloud_inference.CloudFallback("cloud_local_context_unavailable")) == {
            "class": "CloudFallback", "error_code": "cloud_local_context_unavailable"}
    assert cloud_backlog._exception_observation(
        cloud_inference.CloudFallback("synthetic provider text")) == {"class": "CloudFallback"}
