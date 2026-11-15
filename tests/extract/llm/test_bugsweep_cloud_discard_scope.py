"""A paid cloud answer is closed as discarded only when its input provably changed."""
from contextlib import nullcontext
import json
import time

import pytest

import cloud_inference
import cloud_policy
import extract_llm as worker
import extract_llm_carry
import extract_llm_runner
from anthropic_client import AnthropicClient
from test_anthropic_runtime import old, routed as routed, _use_real_cloud_accounting
from test_cloud_response_recovery import legacy_cloud_routes


@pytest.mark.parametrize("write,owned,expected", [
    (False, True, []), (False, False, []),     # past the update fence: never written
    (True, False, [1]),                        # the unit's gated discard decides on proof
])
def test_carry_never_writes_a_disposition_past_the_update_fence(monkeypatch, write, owned, expected):
    called = []
    monkeypatch.setattr(extract_llm_carry.writer_lock, "writer_blocked", lambda: False)
    monkeypatch.setattr(extract_llm_carry, "_owns_current_source", lambda *_a: owned)
    monkeypatch.setattr(extract_llm_carry, "_release", lambda *_a: None)
    row = {"project_id": 1, "message_id": 1, "_cloud_discard": lambda: called.append(1)}

    def commit(**_kw):
        pytest.fail("a fenced or unowned carry must not commit")
    extract_llm_carry._settle_carry(None, [(row, time.time() + 600, commit)], final=True,
                                    write=write, lock_context=nullcontext(True))
    assert called == expected


def _answer(self, method, body, timeout, **kwargs):
    return 200, {}, json.dumps({"model": worker.anthropic_client.MODEL, "stop_reason": "end_turn",
        "content": [{"type": "text", "text": '{"symptoms":[]}'}],
        "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()


def _setup(ledger, monkeypatch, request):
    old(ledger)
    with ledger.db:   # a settled, history-acknowledged row the background reextract may select
        ledger.db.execute("UPDATE messages SET first_seen=?", (time.time() - 601,))
        ledger.db.execute("UPDATE message_revisions SET observed_at=?", (time.time() - 601,))
        cloud_policy.capture_history_tx(ledger.db, "synthetic-ack", time.time())
    _use_real_cloud_accounting(monkeypatch)
    monkeypatch.setattr(worker.cloud_backlog, "request_slot", lambda *_a: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, "owner_alive", lambda: True)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(AnthropicClient, "request", request)

    def infer(_body, *, deadline, provider=None, **_kwargs):
        if provider is not None:   # the local fallback calls without a provider
            provider("synthetic prompt", deadline=deadline, max_tokens=100)
        return {"symptoms": []}

    monkeypatch.setattr(worker, "llm_extract", infer)
    monkeypatch.setattr(extract_llm_runner, "llm_extract", worker.llm_extract)


@pytest.mark.parametrize("changed", [False, True])
def test_unverifiable_currentness_keeps_the_paid_answer_recoverable(routed, monkeypatch, changed):
    ledger, cfg, _, _, _ = routed
    posts, stale = [], [False]

    def request(self, method, body, timeout, **kwargs):
        posts.append(1)
        stale[0] = True             # e.g. a later reply changed the thread snapshot
        if changed:
            with ledger.db:
                ledger.db.execute("UPDATE messages SET content_hash=? WHERE message_id=1", ("f" * 64,))
        return _answer(self, method, body, timeout)

    _setup(ledger, monkeypatch, request)
    owns = extract_llm_runner._owns_current_source
    monkeypatch.setattr(extract_llm_runner, "_owns_current_source",
                        lambda *a, **k: not stale[0] and owns(*a, **k))
    with legacy_cloud_routes():
        first = worker.run_pending(ledger, limit=1, budget_s=180, cloud_only=True)
    assert first["done"] == 0 and len(posts) == 1
    receipt, = ledger.artifacts("cloud_usage_result")
    assert json.loads(receipt["meta"])["http_class"] == "http_200"
    if changed:
        disposition, = ledger.artifacts("cloud_usage_disposition")
        assert json.loads(disposition["meta"])["disposition"] == "discarded_scope"
        return
    assert not ledger.artifacts("cloud_usage_disposition")
    stale[0] = False
    with ledger.db:
        ledger.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='extract_claim'")
    with legacy_cloud_routes():
        later = worker.run_pending(ledger, limit=1, budget_s=180, cloud_only=True)
    assert later["done"] == 1 and len(posts) == 1
    assert not ledger.artifacts("cloud_usage_disposition")


def test_rate_limited_reservation_is_not_discarded_when_the_scope_changes(routed, monkeypatch):
    ledger, cfg, _, _, _ = routed
    posts = []

    def request(self, method, body, timeout, **kwargs):
        posts.append(1)
        cfg["cloud_llm"]["mode"] = "off"   # a proven change lands during the backoff
        return 429, {}, json.dumps({"type": "error", "error": {"type": "rate_limit_error"}}).encode()

    _setup(ledger, monkeypatch, request)
    with legacy_cloud_routes():
        worker.run_pending(ledger, limit=1, budget_s=180, cloud_only=True)
    assert len(posts) == 1
    receipt, = ledger.artifacts("cloud_usage_result")
    assert json.loads(receipt["meta"])["http_class"] == "http_429"
    assert not ledger.artifacts("cloud_usage_disposition")


def test_proven_scope_change_still_closes_a_billed_answer(monkeypatch):
    unit = object.__new__(cloud_inference.CloudInferenceUnit)
    unit._denial_proven, unit._changed = True, lambda: False
    assert unit._proven_change() is True
    unit._denial_proven = False
    assert unit._proven_change() is False
    unit._changed = lambda: (_ for _ in ()).throw(RuntimeError("unreadable"))
    assert unit._proven_change() is False
    unit._changed = None            # callers without a proof keep the old rule
    assert unit._proven_change() is True
