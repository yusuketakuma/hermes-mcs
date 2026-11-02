"""Synthetic backlog routing, accounting and run-lock loss regressions."""
import hashlib
import json
import os
import select
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest

import anthropic_client
import cloud_policy
import extract_llm
import cloud_inference
import inference_dependency_loss
import semantic_drain
import semantic_transport as transport
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
    monkeypatch.setattr(cloud_inference, 'load_config', lambda *a: cfg)
    monkeypatch.setattr(semantic_drain, "load_config", lambda *a: cfg)
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


def test_qc_purpose_uses_separate_fixed_provider_and_restores_context(cloud, monkeypatch):
    led, cfg, job, _ = cloud
    calls, created = [], []

    class Provider:
        def __init__(self, _ledger, _cfg, *, purpose, unit_key, **kwargs):
            self.selection = {'provider': 'openai', 'model': purpose,
                              'effort': 'high' if purpose == 'quality_repair' else 'medium',
                              'max_tokens': 32768 if purpose == 'quality_repair' else 8192}
            self.model = purpose
            self.contract = {'backend': 'openai', 'model': purpose, 'prompt': purpose}
            self.completed_reservations = []
            self.possibly_sent = False
            self.unit_key = unit_key
            created.append(self)

        def __call__(self, prompt, *, deadline, max_tokens):
            calls.append((self.model, max_tokens))
            self.completed_reservations.append({'reservation_id': self.model})
            return {'status': 200, 'text': '{"facts": []}', 'finish_reason': 'stop'}

        def discard(self):
            pass

    monkeypatch.setattr(cloud_inference, '_CloudProvider', Provider)
    with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                     lambda: True, direct) as chat:
        chat('normal')
        with pytest.raises(ValueError), transport.quality_repair():
            chat('qc repair')
            assert transport.inference_model() == 'quality_repair'
            assert transport.inference_requests() == ['backlog', 'quality_repair']
            raise ValueError('synthetic')
        assert transport.inference_model() == 'backlog'
        assert transport._inference_purpose.get() == 'backlog'
        chat('normal again')
    assert calls == [('backlog', 8192), ('quality_repair', 32768), ('backlog', 8192)]
    assert len(created) == 2 and created[0].unit_key == created[1].unit_key
    assert transport._job_model.get() is None and transport._job_providers.get() is None


def test_history_without_false_eligible_uses_cloud_and_settles(cloud):
    led, cfg, job, calls = cloud
    assert "eligible" not in json.loads(job["payload"])
    model = transport.llm_model()
    with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                     lambda: True, direct) as chat:
        assert transport.inference_model() == anthropic_client.MODEL
        assert transport.llm_model() == model
        assert json.loads(chat("Synthetic facts JSON:")) == {"facts": []}
    assert transport.llm_model() == model
    assert len(calls) == len(rows(led, "cloud_usage_reserve")) == len(rows(led, "cloud_usage_result")) == 1
    reserve = json.loads(rows(led, "cloud_usage_reserve")[0]["meta"])
    assert reserve["route"] == "semantic_backlog"
    assert not cloud_policy.usage_report(led)["pricing_unverified"]


@pytest.mark.parametrize("kind", ["thinking", "redacted_thinking"])
def test_thinking_only_length_holds_summary_without_format_repair_or_paid_resend(cloud, monkeypatch, kind):
    import semantic_runtime
    import semantic_store
    led, cfg, job, calls = cloud

    def request(self, method, body, timeout, **kwargs):
        calls.append(body)
        return 200, {}, json.dumps({"model": anthropic_client.MODEL, "stop_reason": "max_tokens",
            "content": [{"type": kind, "thinking" if kind == "thinking" else "data": "synthetic-hidden"}],
            "usage": {"input_tokens": 10, "output_tokens": 10}}).encode()

    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", request)
    bundle = semantic_store.thread_bundle(led.db, 1, 1)
    scfg = semantic_drain.runtime_config(cfg)[0]
    deadline = time.monotonic() + 120
    with transport.backlog_inference(led, cfg, job, deadline, lambda: True, direct,
                                     local_fallback=lambda *a, **kw: pytest.fail("paid local fallback")) as chat:
        summary, reason, error, sent_error, _ = semantic_drain._obtain_summary(
            led, scfg, bundle, chat, lambda *args: None, 1, 1, bundle["source_fingerprint"],
            "synthetic-policy", [], {}, None, deadline, diagnostics={})
        assert summary is None and reason == "model" and sent_error is None
        assert error["code"] == "json_object_invalid" and transport.last_llm_failure() == "length_stop"
        assert not rows(led, semantic_drain.KIND_SUMMARY_FORMAT_REPAIR)
        assert len(calls) == 1
        prompt = calls[0]["messages"][0]["content"]
        # the same paid logical request stays fenced: the refusal is a definite
        # not-sent that defers the unit instead of failing or falling back
        with pytest.raises(transport.runtime.LLMNotSent, match='cloud_not_sent'):
            chat(prompt)
    reserves, results = rows(led, "cloud_usage_reserve"), rows(led, "cloud_usage_result")
    assert len(calls) == len(reserves) == len(results) == 1
    result = json.loads(results[0]["meta"])
    assert result["error_kind"] is None and result["usage"]["output_tokens"] == 10
    assert float(result["cost_usd"]) > 0 and "synthetic-hidden" not in json.dumps(result)
    assert semantic_runtime._circuit_state(led, kind="cloud_circuit")["consecutive_failures"] == 0


def test_budget_refusal_keeps_settled_receipts_without_discard(cloud, monkeypatch):
    """A pre-send refusal is a harmless budget wait: the provider that already
    settled a chunk keeps its receipt — only scope/lease loss may discard."""
    import extract_llm
    led, cfg, job, _ = cloud
    calls, created = [], []

    class Provider:
        def __init__(self, _ledger, _cfg, *, unit_key=None, **kwargs):
            self.selection = {'provider': 'openai', 'model': 'synthetic',
                              'effort': 'medium', 'max_tokens': 8192}
            self.model = 'synthetic'
            self.contract = {'backend': 'openai', 'model': 'synthetic', 'prompt': 'synthetic'}
            self.completed_reservations = []
            self.possibly_sent = False
            self.unit_key = unit_key
            self.discarded = False
            created.append(self)

        def __call__(self, prompt, *, deadline, max_tokens):
            calls.append(prompt)
            if len(calls) == 1:
                self.completed_reservations.append({'reservation_id': 'synthetic'})
                return {'status': 200, 'text': '{"facts": []}', 'finish_reason': 'stop'}
            raise extract_llm.cloud_client.CloudNotSent('budget_exceeded', 'synthetic refusal')

        def discard(self):
            self.discarded = True

    monkeypatch.setattr(cloud_inference, '_CloudProvider', Provider)
    with pytest.raises(transport.runtime.LLMNotSent, match='cloud_not_sent'):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct) as chat:
            assert json.loads(chat('synthetic first')) == {'facts': []}
            chat('synthetic second')
    assert len(created) == 1 and created[0].completed_reservations
    assert not created[0].discarded


def test_same_prompt_for_distinct_targets_has_separate_reservations(cloud):
    led, cfg, job, calls = cloud
    led.save_messages([_message(2, parent=1, body='synthetic reply')])
    snapshot(led)
    with transport.inference_targets():
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct) as chat:
            for target in (1, 2):
                transport.set_inference_target(target)
                assert json.loads(chat('same synthetic prompt')) == {'facts': []}
    assert len(calls) == 2
    assert [row['message_id'] for row in rows(led, 'cloud_usage_reserve')] == [1, 2]


def test_not_sent_cloud_falls_back_locally_for_the_rest_of_the_unit(cloud, monkeypatch):
    led, cfg, job, _ = cloud
    attempts, local = [], []

    def unavailable(provider, *args, **kwargs):
        attempts.append(True)
        raise extract_llm.CloudFallback('synthetic_unavailable')

    monkeypatch.setattr(cloud_inference._CloudProvider, '__call__', unavailable)
    def local_chat(prompt, **kwargs):
        local.append(transport.inference_model())
        return '{"facts": []}'

    with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                     lambda: True, direct, local_fallback=local_chat,
                                     fallback_current=lambda: True) as chat:
        assert json.loads(chat('synthetic first')) == {'facts': []}
        assert json.loads(chat('synthetic second')) == {'facts': []}
    assert len(attempts) == 1 and local == [transport.llm_model()] * 2
    assert not rows(led, 'cloud_usage_reserve')


@pytest.mark.parametrize('change', ['during', 'between'])
def test_local_fallback_rechecks_scope_on_each_call(cloud, monkeypatch, change):
    led, cfg, job, _ = cloud
    state = {'current': True, 'calls': 0}
    def unavailable(provider, *args, **kwargs):
        raise extract_llm.CloudFallback('synthetic_unavailable')
    def local_chat(prompt, **kwargs):
        state['calls'] += 1
        if change == 'during':
            state['current'] = False
        return '{"facts": []}'
    monkeypatch.setattr(cloud_inference._CloudProvider, '__call__', unavailable)
    with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                     lambda: state['current'], direct, local_fallback=local_chat,
                                     fallback_current=lambda: True) as chat:
        if change == 'between':
            assert json.loads(chat('synthetic first')) == {'facts': []}
            state['current'] = False
        with pytest.raises(transport.runtime.LLMNotSent):
            chat('synthetic next')
    assert state['calls'] == 1


@pytest.mark.parametrize('state', ['paid_unknown', 'partial'])
def test_cloud_fallback_never_mixes_a_paid_or_existing_partial_unit(cloud, monkeypatch, state):
    led, cfg, job, _ = cloud

    def unavailable(provider, *args, **kwargs):
        provider.possibly_sent = state == 'paid_unknown'
        raise extract_llm.CloudFallback('synthetic_unavailable')

    monkeypatch.setattr(cloud_inference._CloudProvider, '__call__', unavailable)
    with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                     lambda: True, direct, local_fallback=lambda *a, **kw: pytest.fail('local'),
                                     fallback_current=lambda: state != 'partial') as chat:
        if state == 'partial':
            with pytest.raises(transport.runtime.LLMNotSent):
                chat('synthetic input')
        else:
            assert chat('synthetic input') is None


def test_local_only_feedback_is_filtered_before_the_cloud_selection_limit(cloud, monkeypatch):
    led, cfg, _, _ = cloud
    led.save_messages([_message(mid=2, body="完全合成の過去記録。")])
    snapshot(led)
    monkeypatch.setattr(extract_llm, "_qc_flagged_sql", lambda *a, **kw: "SELECT 1 WHERE m.message_id=1")
    selected, _ = extract_llm._select_pending(led, 1, None, None, False,
        cloud_only=True, prioritize_patients=True)
    assert [row["message_id"] for row in selected] == [2]


@pytest.mark.parametrize("denial", ["recent", "off", "cap", "scope", "route"])
def test_local_boundaries_do_not_send(cloud, denial):
    led, cfg, job, calls = cloud
    if denial == "recent":
        with led.db:
            led.db.execute("UPDATE messages SET notified_at=? WHERE message_id=1", (time.time(),))
    elif denial == "off":
        cfg["cloud_llm"]["mode"] = "off"
    elif denial == "cap":
        cfg["cloud_llm"]["daily_cost_limit_usd"] = 0
    elif denial == "scope":
        cfg["cloud_llm"]["project_ids"] = []
    else:
        cfg["cloud_llm"]["routes"] = ["reextract"]
    with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                     lambda: True, direct) as chat:
        assert chat is None
    assert not calls and not rows(led, "cloud_usage_reserve")


def test_eligible_background_job_after_one_hour_uses_cloud(cloud):
    led, cfg, job, calls = cloud
    with led.db:
        led.db.execute("UPDATE messages SET notified_at=? WHERE message_id=1", (time.time() - 3600,))
        payload = {**json.loads(job['payload']), 'eligible': True}
        led.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?", (json.dumps(payload), job['job_id']))
    job['payload'] = json.dumps(payload)
    selected = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1)[0]
    assert [row['job_id'] for row in selected] == [job['job_id']]
    with transport.backlog_inference(led, cfg, job, time.monotonic() + 120, lambda: True, direct) as chat:
        assert chat and json.loads(chat('synthetic historical prompt')) == {'facts': []}
    assert len(calls) == 1


def test_observe_counts_history_jobs_without_reporting_realtime_as_cloud(cloud):
    import cloud_cli
    led, cfg, job, _ = cloud
    settings = cloud_policy.cloud_config(cfg)[0]
    settings["routes"] = ["semantic_backlog"]
    counts = cloud_cli._route_counts(led.db, cfg, settings, time.monotonic() + 5)
    assert counts["semantic_backlog"]["targets"] == 1
    assert counts["semantic_backlog"]["requests_estimate"] == 2
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
            (json.dumps({**json.loads(job["payload"]), "eligible": True}), job["job_id"]))
    counts = cloud_cli._route_counts(led.db, cfg, settings, time.monotonic() + 5)
    assert counts["semantic_backlog"]["targets"] == 1


@pytest.mark.parametrize("change", ["new", "edit", "aba", "reply", "missing_scope"])
def test_unproven_or_changed_prompt_members_stay_local(cloud, change):
    led, cfg, job, calls = cloud
    if change == "missing_scope":
        with led.db:
            led.db.execute("DELETE FROM artifacts WHERE kind='cloud_history_scope'")
    elif change == "reply":
        led.save_messages([_message(2, parent=1, body="完全合成の新着返信")])
    elif change == "new":
        led.save_messages([_message(2, body="完全合成の新着")])
        job["message_id"] = 2
    else:
        led.save_messages([_message(body="完全合成の編集")])
        if change == "aba":
            led.save_messages([_message()])
    with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                     lambda: True, direct) as chat:
        assert chat is None
    assert not calls


@pytest.mark.parametrize("change", ["new", "edit", "notify", "unknown_age"])
def test_resume_does_not_capture_current_realtime_even_without_notification(cloud, change):
    led, _, _, _ = cloud
    if change == "new":
        led.save_messages([_message(2)])
        mid = 2
    else:
        mid = 1
        if change == "edit":
            led.save_messages([_message(body="完全合成の直近編集")])
        else:
            with led.db:
                if change == "notify":
                    led.db.execute("UPDATE messages SET notified_at=? WHERE message_id=1", (time.time(),))
                else:
                    led.db.execute("UPDATE messages SET first_seen=NULL WHERE message_id=1")
    with led.db:
        led.db.execute("BEGIN IMMEDIATE")
        cloud_policy.capture_history_tx(led.db, "synthetic-current-resume", time.time())
    assert not cloud_policy.history_thread_allowed(led.db, 1, mid)


def test_source_changed_after_http_keeps_cost_and_discards(cloud):
    led, cfg, job, calls = cloud
    current = [True]

    def changed(call, *args, **kw):
        result = direct(call, *args, **kw)
        current[0] = False
        return result

    with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                     lambda: current[0], changed) as chat:
        assert chat("Synthetic facts JSON:") is None
    assert len(calls) == len(rows(led, "cloud_usage_result")) == 1
    assert rows(led, "cloud_usage_disposition")


def test_lost_run_lock_does_not_write_results_or_disposition(cloud):
    led, cfg, job, calls = cloud

    def lost(call, *args, **kw):
        direct(call, *args, **kw)
        raise writer_lock.RunLockLost(held=False)

    with pytest.raises(writer_lock.RunLockLost):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, lost) as chat:
            chat("Synthetic facts JSON:")
    assert len(calls) == len(rows(led, "cloud_usage_reserve")) == 1
    assert not rows(led, "cloud_usage_result")
    assert not rows(led, "cloud_usage_disposition")


def test_real_process_wrapper_selects_cloud_and_resets_model(cloud, monkeypatch):
    led, cfg, job, calls = cloud

    def stages(ledger, scfg, selected, jev, llm, deadline, **kw):
        assert transport.inference_model() == anthropic_client.MODEL
        assert json.loads(llm("Synthetic facts JSON:")) == {"facts": []}
        return "done"

    monkeypatch.setattr(semantic_drain, "_process_job_inner", stages)
    monkeypatch.setattr(semantic_drain.semantic_store, "refresh_cards_allowed", lambda *a: False)
    timing = {"s": 0.0}
    model = transport.llm_model()
    assert semantic_drain._process_job(
        led, semantic_drain.runtime_config(cfg)[0], job, None,
        lambda *a, **kw: pytest.fail("local GPU used"), time.monotonic() + 120,
        lane="backlog", diagnostics={}, cloud_transport=direct, llm_timing=timing) == "done"
    assert len(calls) == 1 and timing["s"] > 0
    assert transport.llm_model() == model


@pytest.mark.parametrize("cloud_only", [False, True])
def test_cloud_stages_remain_current_for_normal_readers_after_context_exit(cloud, monkeypatch, cloud_only):
    import semantic_store
    import semantic_v4
    from candidate_testkit import approved_config
    from semantic_testkit import _canonical_cfg, _llm_v2, _PassJev
    led, cfg, _, calls = cloud
    cfg.clear()
    cfg.update(approved_config({**_canonical_cfg(), "cloud_llm": {"mode": "on", "project_ids": [1]}}))
    led.save_messages([_message(body="アムロジピン5mgを継続します。")])
    led.semantic_seed(1, [1], {"source": "history_import"})
    snapshot(led)

    def request(self, method, body, timeout, **kw):
        calls.append(body)
        return 200, {}, json.dumps({
            "model": anthropic_client.MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": _llm_v2(body["messages"][0]["content"])}],
            "usage": {"input_tokens": 10, "output_tokens": 10}}).encode()

    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", request)
    monkeypatch.setattr(transport, "llm_chat", lambda *a, **kw: pytest.fail("local GPU used"))
    if cloud_only:
        monkeypatch.setattr(semantic_drain, "reprocess_step", lambda *a, **kw: pytest.fail("worker repeated corpus seed"))
        monkeypatch.setattr(semantic_v4, "reproject_stale", lambda *a: pytest.fail("worker repeated corpus projection"))
        monkeypatch.setattr(semantic_drain.llm_availability, "circuit_open_s", lambda *a: 60)
    result = {"errors": []}
    out = semantic_drain.run_due(led, cfg, result, time.monotonic() + 1200,
                                jev_client=_PassJev(), lane="backlog", max_jobs=1,
                                cloud_only=cloud_only)
    assert out["done"] == 1 and not result["errors"], (out, result)
    assert calls
    fp = semantic_store.thread_bundle(led.db, 1, 1)["source_fingerprint"]
    for kind in ("semantic_summary", "semantic_audit"):
        assert semantic_store.current_artifact(led.db, kind, 1, fp) is not None
    published = rows(led, semantic_v4.KIND_V4)
    assert published and json.loads(published[-1]["meta"])["fingerprint"] == fp
    assert published[-1]["model"] == transport.llm_model()
    assert json.loads(published[-1]["meta"])["inference_requests"]
    semantic_v4.invalidate_projections(led.db, semantic_drain.runtime_config(cfg)[0])
    assert semantic_store.current_artifact(led.db, semantic_v4.KIND_V4, 1, fp) is not None
    from extraction_progress import message_progress
    row = led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    assert message_progress(led.db, row, cfg=cfg)["state"] == "complete"


def test_full_cloud_capacity_defers_without_usage_or_local_fallback(cloud):
    import cloud_backlog
    import semantic_runtime
    led, cfg, job, calls = cloud
    cfg["cloud_llm"]["max_concurrency"] = 1
    with cloud_backlog.request_slot(extract_llm.HOME, 1):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct) as chat:
            with pytest.raises(semantic_runtime.LLMNotSent, match="cloud_capacity"):
                chat("Synthetic facts JSON:")
    assert not calls and not rows(led, "cloud_usage_reserve")


def test_realtime_process_never_uses_cloud_transport(cloud, monkeypatch):
    led, cfg, job, calls = cloud
    local = []
    def stages(ledger, scfg, selected, jev, llm, deadline, **kw):
        assert llm('synthetic prompt') == 'synthetic local'
        return 'done'
    monkeypatch.setattr(semantic_drain, '_process_job_inner', stages)
    monkeypatch.setattr(semantic_drain.semantic_store, 'refresh_cards_allowed', lambda *a: False)
    assert semantic_drain._process_job(
        led, semantic_drain.runtime_config(cfg)[0], job, None,
        lambda *a, **kw: local.append(True) or 'synthetic local', time.monotonic() + 120,
        lane='realtime', diagnostics={}, cloud_transport=direct) == 'done'
    assert local and not calls and not rows(led, 'cloud_usage_reserve')
    assert not semantic_drain._select_cloud_due(led, 'realtime', ('semantic',), False, 1)[0]


def test_cloud_only_process_never_enters_local_stage_when_scope_changes(cloud, monkeypatch):
    led, cfg, job, calls = cloud
    cfg["cloud_llm"]["mode"] = "off"
    monkeypatch.setattr(semantic_drain, "_process_job_inner", lambda *a, **kw: pytest.fail("local fallback"))
    assert semantic_drain._process_job(
        led, semantic_drain.runtime_config(cfg)[0], job, None,
        lambda *a, **kw: pytest.fail("local GPU used"), time.monotonic() + 120,
        lane="backlog", diagnostics={}, cloud_transport=direct, cloud_only=True) == "deferred"
    assert not calls


def test_cloud_and_resident_selection_prioritize_active_latest_patient(cloud):
    led, cfg, _, _ = cloud
    cfg["cloud_llm"]["project_ids"] = None
    for pid in (2, 3):
        _patient(led, pid)
        led.save_messages([_message(pid, pid=pid)])
        led.semantic_seed(pid, [pid], {"source": "history_import"})
    with led.db:
        led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=3")
        for mid in (1, 2, 3):
            led.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?", (mid * 100, mid))
    snapshot(led)
    selected = semantic_drain._select_cloud_due(led, "backlog", ("semantic",), False, 3)[0]
    assert [r["project_id"] for r in selected] == [2, 1, 3]
    selected = semantic_drain._select_due(led, "backlog", ("semantic",), False, 3,
                                         prioritize_patients=True)[0]
    assert [r["project_id"] for r in selected] == [2, 1, 3]
    selected, _ = extract_llm._select_pending(led, 3, None, None, False,
                                              cloud_only=True, prioritize_patients=True)
    assert [r["project_id"] for r in selected] == [2, 1, 3]


@pytest.mark.parametrize("lane", ["semantic", "extract"])
@pytest.mark.parametrize("change", ["reply", "edit", "aba"])
def test_cloud_selection_requires_current_thread_receipt(cloud, lane, change):
    led, _, _, _ = cloud
    if change == "reply":
        led.save_messages([_message(2, parent=1, body="完全合成の新しい返信。")])
    else:
        led.save_messages([_message(body="完全合成の変更。")])
        if change == "aba":
            led.save_messages([_message()])

    def select():
        if lane == "semantic":
            return semantic_drain._select_cloud_due(led, "backlog", ("semantic",), False, 1)[0]
        return extract_llm._select_pending(led, 10, None, None, False,
            cloud_only=True, prioritize_patients=True)[0]

    assert not select()
    snapshot(led)
    assert 1 in [row["message_id"] for row in select()]


@pytest.mark.parametrize("lane", ["semantic", "extract"])
def test_selection_reads_receipt_once_for_multiple_candidates(cloud, lane):
    led, _, _, _ = cloud
    for mid in range(2, 16):
        led.save_messages([_message(mid, body="完全合成の記録。")])
        led.semantic_seed(1, [mid], {"source": "history_import"})
    snapshot(led)
    statements = []
    led.db.set_trace_callback(statements.append)
    try:
        if lane == "semantic":
            selected = semantic_drain._select_cloud_due(led, "backlog", ("semantic",), False, 10)[0]
        else:
            selected = extract_llm._select_pending(led, 10, None, None, False,
                cloud_only=True, prioritize_patients=True)[0]
    finally:
        led.db.set_trace_callback(None)
    assert len(selected) >= 10
    assert sum(s.lstrip('- ').startswith("SELECT content,meta FROM (SELECT content,meta FROM artifacts")
               for s in statements) == 1


@pytest.mark.parametrize("lane", ["semantic", "extract"])
def test_selection_checks_each_thread_once_per_snapshot(cloud, monkeypatch, lane):
    led, _, _, _ = cloud
    for mid in range(2, 8):
        led.save_messages([_message(mid, parent=1, body="完全合成の返信。")])
        led.semantic_seed(1, [mid], {"source": "history_import"})
    snapshot(led)
    checked = []
    original = cloud_policy._history_thread_allowed

    def count(*args):
        checked.append(1)
        return original(*args)

    monkeypatch.setattr(cloud_policy, "_history_thread_allowed", count)
    if lane == "semantic":
        # Semantic jobs collapse to the thread root, so exercise multiple
        # member candidates through the same check used by its selector.
        with cloud_policy.history_selection_check(led.db) as check:
            selected = [check(1, mid) for mid in range(1, 8)]
        assert all(selected)
    else:
        selected = extract_llm._select_pending(led, 10, None, None, False,
            cloud_only=True, prioritize_patients=True)[0]
    assert len(selected) >= 2
    assert len(checked) == 1


def test_selection_failure_preserves_outer_transaction_and_uninstalls_check(cloud, monkeypatch):
    led, _, _, _ = cloud
    progress = []
    led.db.set_progress_handler(lambda: progress.append(1) and 0, 1)
    led.db.execute("BEGIN")
    led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    check = None
    try:
        def fail(*args, **kwargs):
            raise ValueError("synthetic_binding_error")
        monkeypatch.setattr(inference_dependency_loss, "source_binding", fail)
        with pytest.raises(ValueError, match="synthetic_binding_error"):
            with cloud_policy.history_selection_check(led.db) as check:
                check(1, 1)
        assert led.db.in_transaction
        assert led.db.execute("SELECT is_archived FROM patients WHERE project_id=1").fetchone()[0] == 1
        assert progress
        assert check(1, 1) == 0
        before = len(progress)
        led.db.execute("SELECT 1").fetchone()
        assert len(progress) > before
    finally:
        led.db.set_progress_handler(None, 0)
        led.db.rollback()


def test_selection_cancel_cleans_up_without_replacing_progress_handler(cloud):
    led, _, _, _ = cloud
    led.db.execute("BEGIN")
    calls = []
    check = None
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupt"):
            with cloud_policy.history_selection_check(led.db) as check:
                def cancel_once():
                    calls.append(1)
                    return int(len(calls) == 1)
                led.db.set_progress_handler(cancel_once, 1)
                check(1, 1)
        assert led.db.in_transaction
        count = len(calls)
        assert check(1, 1) == 0
        led.db.execute("SELECT 1").fetchone()
        assert len(calls) > count
    finally:
        led.db.set_progress_handler(None, 0)
        led.db.rollback()


def test_persistent_cancel_leaves_no_selection_transaction(cloud):
    led, _, _, _ = cloud
    calls = []
    check = None
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupt"):
            with cloud_policy.history_selection_check(led.db) as check:
                def cancel():
                    calls.append(1)
                    return 1
                led.db.set_progress_handler(cancel, 1)
                check(1, 1)
        assert not led.db.in_transaction
        count = len(calls)
        with pytest.raises(sqlite3.OperationalError, match="interrupt"):
            led.db.execute("SELECT 1").fetchone()
        assert len(calls) > count
    finally:
        led.db.set_progress_handler(None, 0)
    assert check(1, 1) == 0


def test_selection_uses_one_snapshot_but_fresh_guard_sees_revocation(cloud):
    led, _, _, _ = cloud
    path = led.db.execute("PRAGMA database_list").fetchone()[2]
    with cloud_policy.history_selection_check(led.db) as check:
        other = sqlite3.connect(path)
        try:
            other.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
                ("cloud_history_scope", '{"version":1,"sources":{}}', "synthetic", "{}", time.time()))
            other.commit()
        finally:
            other.close()
        assert check(1, 1) == 1
    assert not cloud_policy.history_thread_allowed(led.db, 1, 1)


@pytest.mark.parametrize("lane", ["semantic", "extract"])
def test_selection_preserves_caller_cursor_and_transaction(cloud, lane):
    led, _, _, _ = cloud
    progress = []
    led.db.set_progress_handler(lambda: progress.append(1) and 0, 1)
    led.db.execute("BEGIN")
    led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    outer = led.db.execute("SELECT 10 UNION ALL SELECT 20 UNION ALL SELECT 30")
    try:
        assert outer.fetchone()[0] == 10
        if lane == "semantic":
            selected = semantic_drain._select_cloud_due(
                led, "backlog", ("semantic",), False, 1, deadline=time.monotonic() + 60)[0]
        else:
            selected = extract_llm._select_pending(led, 1, None, None, False,
                cloud_only=True, prioritize_patients=True)[0]
        assert [row["message_id"] for row in selected] == [1]
        assert [row[0] for row in outer] == [20, 30]
        assert led.db.in_transaction
        assert led.db.execute("SELECT is_archived FROM patients WHERE project_id=1").fetchone()[0] == 1
        before = len(progress)
        led.db.execute("SELECT 1").fetchone()
        assert len(progress) > before
    finally:
        outer.close()
        led.db.set_progress_handler(None, 0)
        led.db.rollback()


def test_cloud_selection_releases_run_lock_for_another_process(cloud, tmp_path, monkeypatch):
    led, cfg, _, calls = cloud
    lock_path = tmp_path / "synthetic-run.lock"
    lock_fd = writer_lock.acquire_run_lock(str(lock_path))
    assert lock_fd is not None
    script = """\
import fcntl, os, sys
sys.stdin.readline()
fd = os.open(sys.argv[1], os.O_WRONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print('acquired', flush=True)
finally:
    os.close(fd)
"""
    proc = subprocess.Popen([sys.executable, "-c", script, str(lock_path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    original = cloud_policy.history_target_ready
    observed = []

    def probe(db, pid, mid):
        if not observed:
            proc.stdin.write("go\n")
            proc.stdin.flush()
            ready, _, _ = select.select([proc.stdout], [], [], 10)
            assert ready, "synthetic lock probe did not finish"
            observed.append(proc.stdout.readline().strip())
        return original(db, pid, mid)

    monkeypatch.setattr(cloud_policy, "history_target_ready", probe)
    monkeypatch.setattr(semantic_drain, "_process_job", lambda *args, **kwargs: "deferred")
    try:
        semantic_drain.run_due(led, cfg, {"errors": []}, time.monotonic() + 1200,
                               lane="backlog", max_jobs=1, cloud_only=True, run_lock_fd=lock_fd)
        assert observed == ["acquired"]
        assert proc.wait(timeout=10) == 0
        assert writer_lock.acquire_run_lock(str(lock_path)) is None
        assert not calls and not rows(led, "cloud_usage_reserve")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()
        os.close(lock_fd)


def test_incomplete_cloud_selection_reports_hold_without_starting_work(cloud, tmp_path, monkeypatch):
    led, cfg, job, calls = cloud
    lock_fd = writer_lock.acquire_run_lock(str(tmp_path / "synthetic-run.lock"))
    assert lock_fd is not None
    deadline = time.monotonic() + 1200
    observed = []

    def interrupted(*args, **kwargs):
        observed.append((kwargs.get("deadline"), kwargs.get("isolated")))
        raise semantic_drain._CloudSelectionIncomplete("deadline")

    monkeypatch.setattr(semantic_drain, "_select_cloud_due", interrupted)
    monkeypatch.setattr(semantic_drain, "_process_job_inner",
                        lambda *args, **kwargs: pytest.fail("incomplete selection started work"))
    try:
        out = semantic_drain.run_due(led, cfg, {"errors": []}, deadline,
                                     lane="backlog", max_jobs=1, cloud_only=True, run_lock_fd=lock_fd)
        assert observed == [(deadline, True)]
        assert out.get("cloud_selection_incomplete") is True
        assert not out["done"] and not calls and not rows(led, "cloud_usage_reserve")
        current = led.db.execute("SELECT state,attempts FROM fetch_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        assert tuple(current) == ("pending", job["attempts"])
    finally:
        os.close(lock_fd)


def test_cloud_selection_refill_reuses_original_deadline(cloud, tmp_path, monkeypatch):
    led, cfg, _, calls = cloud
    for mid in range(2, 12):
        led.save_messages([_message(mid, body="完全合成の過去記録。")])
        led.semantic_seed(1, [mid], {"source": "history_import"})
    snapshot(led)
    lock_fd = writer_lock.acquire_run_lock(str(tmp_path / "synthetic-run.lock"))
    assert lock_fd is not None
    deadline = time.monotonic() + 1200
    original = semantic_drain._select_cloud_due
    selections, attempted = [], []

    def select_recorded(*args, **kwargs):
        if kwargs.get("isolated"):
            selections.append((kwargs.get("deadline"), kwargs.get("isolated")))
        return original(*args, **kwargs)

    @contextmanager
    def held_prefix(_ledger, job_id):
        attempted.append(job_id)
        yield len(attempted) > 9

    monkeypatch.setattr(semantic_drain, "_select_cloud_due", select_recorded)
    monkeypatch.setattr(semantic_drain, "_job_lock", held_prefix)
    monkeypatch.setattr(semantic_drain, "_process_job", lambda *args, **kwargs: "deferred")
    try:
        semantic_drain.run_due(led, cfg, {"errors": []}, deadline,
                               lane="backlog", max_jobs=1, cloud_only=True, run_lock_fd=lock_fd)
        assert len(attempted) >= 10 and len(selections) >= 2
        assert all(item == (deadline, True) for item in selections)
        assert not calls and not rows(led, "cloud_usage_reserve")
    finally:
        os.close(lock_fd)


def test_cloud_selection_refill_deadline_does_not_count_remaining_jobs(cloud, tmp_path, monkeypatch):
    led, cfg, _, calls = cloud
    for mid in range(2, 12):
        led.save_messages([_message(mid, body="完全合成の過去記録。")])
        led.semantic_seed(1, [mid], {"source": "history_import"})
    snapshot(led)
    lock_fd = writer_lock.acquire_run_lock(str(tmp_path / "synthetic-run.lock"))
    assert lock_fd is not None
    original = semantic_drain._select_cloud_due
    selections, attempted, statements = [], [], []
    deadline = time.monotonic() + 1200

    def select_until_refill(*args, **kwargs):
        if kwargs.get("isolated"):
            selections.append(kwargs.get("deadline"))
            if len(selections) == 2:
                raise semantic_drain._CloudSelectionIncomplete("deadline")
        return original(*args, **kwargs)

    @contextmanager
    def held_prefix(_ledger, job_id):
        attempted.append(job_id)
        yield False

    monkeypatch.setattr(semantic_drain, "_select_cloud_due", select_until_refill)
    monkeypatch.setattr(semantic_drain, "_job_lock", held_prefix)
    monkeypatch.setattr(semantic_drain, "_process_job_inner",
                        lambda *args, **kwargs: pytest.fail("incomplete refill started work"))
    led.db.set_trace_callback(statements.append)
    try:
        out = semantic_drain.run_due(led, cfg, {"errors": []}, deadline,
                                     lane="backlog", max_jobs=1, cloud_only=True, run_lock_fd=lock_fd)
        assert len(attempted) == 9 and selections == [deadline, deadline]
        assert out.get("cloud_selection_incomplete") is True and out["left"] is None
        assert out["cloud_selection_reason"] == "deadline"
        assert not any("SELECT COUNT(*) FROM fetch_jobs WHERE kind IN (" in sql for sql in statements)
        assert not calls and not rows(led, "cloud_usage_reserve") and not rows(led, "semantic_usage")
    finally:
        led.db.set_trace_callback(None)
        os.close(lock_fd)


@pytest.mark.parametrize("change", ["job", "source", "receipt", "v2_source", "v2_revision",
                                     "v2_attachment_add", "v2_attachment_withdraw"])
def test_cloud_selection_rechecks_changes_after_unlock(cloud, tmp_path, monkeypatch, change):
    import semantic_store
    led, cfg, job, calls = cloud
    if change == "v2_attachment_withdraw":
        with led.db:
            led.db.execute("INSERT INTO attachments(message_id,file_id,name,state) VALUES(1,?,?,?)",
                           ("synthetic-file", "synthetic.pdf", "pending"))
    if change.startswith("v2_"):
        continuous_snapshot(led)
        original_scope = cloud_policy.history_thread_snapshot(led.db, 1, 1)
        original_bundle = semantic_store.thread_bundle(led.db, 1, 1, [1])["source_fingerprint"]
        assert original_scope and cloud_policy.history_thread_allowed(led.db, 1, 1)
        assert cloud_policy.history_target_ready(led.db, 1, 1)
    lock_path = tmp_path / "synthetic-run.lock"
    lock_fd = writer_lock.acquire_run_lock(str(lock_path))
    assert lock_fd is not None
    original = semantic_drain._select_cloud_due
    db_path = led.db.execute("PRAGMA database_list").fetchone()[2]
    changed = []

    def select_and_change(*args, **kwargs):
        batch = original(*args, **kwargs)
        if kwargs.get("isolated") and not changed and batch[0]:
            with sqlite3.connect(db_path) as other:
                if change == "job":
                    payload = {**json.loads(job["payload"]), "synthetic_revision": 1}
                    other.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                                  (json.dumps(payload), job["job_id"]))
                elif change in ("source", "v2_source"):
                    body = "完全合成の変更後本文。"
                    html = f"<p>{body}</p>"
                    other.execute("UPDATE messages SET body_text=?,body_html=?,content_hash=? "
                                  "WHERE project_id=1 AND message_id=1",
                                  (body, html, hashlib.sha256(html.encode()).hexdigest()))
                elif change == "v2_revision":
                    other.execute("INSERT INTO message_revisions(message_id,seq,content_hash,prev_content_hash,"
                                  "body_state,observed_at) SELECT m.message_id,"
                                  "(SELECT COALESCE(MAX(seq),1)+1 FROM message_revisions WHERE message_id=m.message_id),"
                                  "m.content_hash,m.content_hash,m.body_state,? FROM messages m "
                                  "WHERE m.project_id=1 AND m.message_id=1", (time.time() - 1200,))
                elif change == "v2_attachment_add":
                    other.execute("INSERT INTO attachments(message_id,file_id,name,state) VALUES(1,?,?,?)",
                                  ("synthetic-file", "synthetic.pdf", "pending"))
                elif change == "v2_attachment_withdraw":
                    other.execute("UPDATE attachments SET state='withdrawn' WHERE message_id=1 AND file_id=?",
                                  ("synthetic-file",))
                else:
                    other.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
                                  ("cloud_history_scope", '{"version":1,"sources":{}}', "synthetic", "{}", time.time()))
            changed.append(True)
        return batch

    monkeypatch.setattr(semantic_drain, "_select_cloud_due", select_and_change)
    monkeypatch.setattr(semantic_drain, "_process_job_inner",
                        lambda *args, **kwargs: pytest.fail("stale selection started work"))
    try:
        out = semantic_drain.run_due(led, cfg, {"errors": []}, time.monotonic() + 1200,
                                     lane="backlog", max_jobs=1, cloud_only=True, run_lock_fd=lock_fd)
        assert changed
        assert out.get("cloud_selection_changed") is True
        if change.startswith("v2_"):
            assert cloud_policy.history_thread_allowed(led.db, 1, 1)
            assert cloud_policy.history_target_ready(led.db, 1, 1)
            if change.startswith("v2_attachment_"):
                assert cloud_policy.history_thread_snapshot(led.db, 1, 1) == original_scope
                assert semantic_store.thread_bundle(led.db, 1, 1, [1])["source_fingerprint"] != original_bundle
            else:
                assert cloud_policy.history_thread_snapshot(led.db, 1, 1) != original_scope
        assert not calls and not rows(led, "cloud_usage_reserve") and not rows(led, "semantic_usage")
        current = led.db.execute("SELECT state,attempts FROM fetch_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        assert tuple(current) == ("pending", job["attempts"])
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize("first", ["local", "cloud"])
def test_cached_chunks_are_not_mixed_across_backends(cloud, monkeypatch, first):
    import semantic_extraction
    import semantic_store
    from semantic_testkit import _llm_v2
    led, cfg, job, calls = cloud
    led.save_messages([_message(body="アムロジピン5mgを継続します。")])
    snapshot(led)
    bundle = semantic_store.thread_bundle(led.db, 1, 1)

    def request(self, method, body, timeout, **kw):
        calls.append(body)
        return 200, {}, json.dumps({
            "model": anthropic_client.MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": _llm_v2(body["messages"][0]["content"])}],
            "usage": {"input_tokens": 10, "output_tokens": 10}}).encode()

    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", request)

    def extract(fn):
        return semantic_extraction.extract_facts_v2(fn, bundle["members"][0], ledger=led,
            project_id=1, source_fingerprint=bundle["source_fingerprint"])

    def local(prompt, **kwargs):
        transport._record_inference("local")
        return _llm_v2(prompt, **kwargs)

    def use(backend):
        if backend == "local":
            return extract(local)
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct) as cloud_fn:
            return extract(cloud_fn)

    use(first)
    before = [dict(row) for row in rows(led, semantic_extraction.KIND_CHUNK_V2)]
    assert before
    count = len(calls)
    def assemble(fn):
        return semantic_drain._fact_v2_document(
            led, bundle["members"][0], 1, 1, bundle["source_fingerprint"],
            "synthetic-policy", "shadow", None, fn, time.monotonic() + 120)

    if first == "local":
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct) as cloud_fn:
            assemble(cloud_fn)
    else:
        assemble(local)
    assert len(calls) == count + (1 if first == "local" else 0)
    after = [dict(row) for row in rows(led, semantic_extraction.KIND_CHUNK_V2)]
    assert len(after) == len(before) + 1
    first_provider = "local" if first == "local" else "anthropic"
    second_provider = "anthropic" if first == "local" else "local"
    assert json.loads(before[0]["meta"])["provider_provenance"]["provider"] == first_provider
    assert json.loads(after[-1]["meta"])["provider_provenance"]["provider"] == second_provider
    assembled = rows(led, semantic_drain.KIND_FACTS_V2)
    assert assembled and json.loads(assembled[-1]["meta"])["provider_provenance"]["providers"] == [second_provider]
    stored = assembled[-1]
    doc, meta = json.loads(stored['content']), json.loads(stored['meta'])
    producer = {}
    rebuilt, reason = semantic_extraction.rebuild_facts_v2_cached(
        bundle['members'][0], led.db, bundle['source_fingerprint'], 1, doc, meta,
        producer_out=producer)
    assert reason is None and rebuilt == doc
    assert producer == {'providers': [second_provider], 'complete': True}
    meta['provider_provenance']['result_hash'] = 'synthetic-invalid'
    rebuilt, reason = semantic_extraction.rebuild_facts_v2_cached(
        bundle['members'][0], led.db, bundle['source_fingerprint'], 1, doc, meta)
    assert rebuilt is None and reason == 'producer_unverified'


def test_resident_history_turn_selects_cloud_batch_without_expanding_realtime(monkeypatch):
    now = [0.0]
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "max_concurrency": 4}}
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(extract_llm.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(extract_llm, "load_config", lambda: cfg)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(extract_llm, "Ledger", lambda *a: type("DB", (), {"close": lambda s: None})())
    monkeypatch.setattr(extract_llm, "legacy_admissions", lambda *a: None)
    monkeypatch.setattr(extract_llm, "_recent_arrivals", lambda *a: {99})
    monkeypatch.setattr(extract_llm, "_settle_carry", lambda *a, **kw: None)
    monkeypatch.setattr(extract_llm, "_background_semantic", lambda *a: {
        "job_metrics": [{"kind": "semantic", "status": "done"}]})
    monkeypatch.setattr(extract_llm.sys, "argv", [
        "extract_llm", "--all", "--lane", "2", "--semantic", "--stop-after", "150"])
    seen = []

    def run(*a, **kw):
        now[0] += 30
        seen.append(kw)
        return {"done": 1, "failed": 0, "left": 1, "selected": 1, "carry": []}

    monkeypatch.setattr(extract_llm, "run_pending", run)
    assert extract_llm.main() == 0
    history = [r for r in seen if r["oldest_first"]]
    assert history and history[0]["limit"] == 4
    assert history[0]["allow_cloud"] and history[0]["background"]
    assert all(r["limit"] == 1 and r["allow_cloud"] and r["background"]
               for r in seen if not r["oldest_first"])


def test_cloud_selection_resumes_pages_and_preserves_sql_order(cloud, monkeypatch):
    from types import SimpleNamespace
    led, cfg, _, calls = cloud
    cfg['cloud_llm']['project_ids'] = None
    for pid in range(2, 7):
        _patient(led, pid)
        led.save_messages([_message(pid, pid=pid)])
        led.semantic_seed(pid, [pid], {'source': 'history_import'})
    with led.db:
        for pid, ts in [(1, 100), (2, 300), (3, 300), (4, None), (5, 400), (6, 500)]:
            led.db.execute('UPDATE messages SET posted_at_ts=? WHERE project_id=?', (ts, pid))
        led.db.execute('UPDATE patients SET is_archived=1 WHERE project_id=6')
        led.db.execute('DELETE FROM patients WHERE project_id=5')
    snapshot(led)
    monkeypatch.setattr(semantic_drain, '_CLOUD_SELECTION_PAGE', 2)
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)
    cursors = []
    for _ in range(3):
        clock[0] = 0.0
        led.db.set_trace_callback(lambda sql: clock.__setitem__(0, 1.0)
            if sql.startswith('SELECT j.job_id,j.kind') else None)
        try:
            with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='deadline'):
                semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=1.0)
        finally:
            led.db.set_trace_callback(None)
        cursors.append(semantic_drain._cloud_selection_progress['cursor'])
        with led.db:
            led.db.execute("UPDATE patients SET station_name='synthetic' WHERE project_id=1")
    assert cursors == sorted(set(cursors)) and len(cursors) == 3
    clock[0] = 0.0
    result = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=10.0)[0]
    assert [row['project_id'] for row in result] == [5, 2, 3, 1, 4, 6]
    assert not calls and not led.db.in_transaction


def test_cloud_selection_rechecks_scope_and_deleted_job_after_incomplete(cloud, monkeypatch):
    from types import SimpleNamespace
    led, cfg, job, calls = cloud
    for mid in range(2, 5):
        led.save_messages([_message(mid)])
        led.semantic_seed(1, [mid], {'source': 'history_import'})
    snapshot(led)
    monkeypatch.setattr(semantic_drain, '_CLOUD_SELECTION_PAGE', 1)
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)
    led.db.set_trace_callback(lambda sql: clock.__setitem__(0, 1.0)
        if sql.startswith('SELECT j.job_id,j.kind') else None)
    try:
        with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='deadline'):
            semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=1.0)
    finally:
        led.db.set_trace_callback(None)
    with led.db:
        led.db.execute('DELETE FROM fetch_jobs WHERE job_id=?', (job['job_id'],))
        led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=(SELECT MAX(artifact_id) "
                       "FROM artifacts WHERE kind='cloud_history_scope')",
                       ('{"version":1,"sources":{}}',))
    clock[0] = 0.0
    result = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=10.0)[0]
    assert result == [] and not calls and not led.db.in_transaction


def test_cloud_selection_refill_advances_job_ids_without_reselecting_excluded(cloud):
    led, _, _, calls = cloud
    for mid in range(2, 16):
        led.save_messages([_message(mid)])
        led.semantic_seed(1, [mid], {'source': 'history_import'})
    snapshot(led)
    first = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1)[0]
    first_ids = [row['job_id'] for row in first]
    second = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1,
                                              excluded=first_ids)[0]
    second_ids = [row['job_id'] for row in second]
    assert len(first_ids) == 9 and second_ids
    assert first_ids == sorted(first_ids) and second_ids == sorted(second_ids)
    assert max(first_ids) < min(second_ids)
    assert not calls


def test_cloud_selection_revalidates_partial_batch_after_deadline(cloud, monkeypatch):
    from types import SimpleNamespace
    led, _, job, calls = cloud
    led.save_messages([_message(2)])
    led.semantic_seed(1, [2], {'source': 'history_import'})
    snapshot(led)
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)
    ready = cloud_policy.history_target_ready
    checked = []

    def expire_on_second(db, pid, mid):
        checked.append(mid)
        if len(checked) == 2:
            clock[0] = 1.0
        return ready(db, pid, mid)

    monkeypatch.setattr(cloud_policy, 'history_target_ready', expire_on_second)
    with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='deadline'):
        semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1, deadline=1.0)
    assert checked == [1, 2] and not led.db.in_transaction
    monkeypatch.setattr(cloud_policy, 'history_target_ready', ready)
    clock[0] = 0.0
    selected = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1, deadline=10.0)[0]
    assert [row['job_id'] for row in selected] == [job['job_id']]
    later = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1,
                                            excluded=[job['job_id']], deadline=10.0)[0]
    assert [row['job_id'] for row in later] == [job['job_id'] + 1]
    assert not calls


def test_cloud_selection_invalidates_progress_when_config_changes(cloud, monkeypatch):
    from types import SimpleNamespace
    led, cfg, _, calls = cloud
    led.save_messages([_message(2)])
    led.semantic_seed(1, [2], {'source': 'history_import'})
    snapshot(led)
    monkeypatch.setattr(semantic_drain, '_CLOUD_SELECTION_PAGE', 1)
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)
    led.db.set_trace_callback(lambda sql: clock.__setitem__(0, 1.0)
        if sql.startswith('SELECT j.job_id,j.kind') else None)
    try:
        with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='deadline'):
            semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1, deadline=1.0)
    finally:
        led.db.set_trace_callback(None)
    old = semantic_drain._cloud_selection_progress['binding']
    cfg['cloud_llm']['project_ids'] = []
    clock[0] = 0.0
    selected = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1, deadline=10.0)[0]
    assert selected == [] and semantic_drain._cloud_selection_progress is None
    assert old is not None and not calls


def test_cloud_selection_memory_failure_is_incomplete(cloud, monkeypatch):
    led, _, _, calls = cloud

    def out_of_memory(*args, **kwargs):
        raise MemoryError('synthetic')

    monkeypatch.setattr(semantic_drain.mcs_util, 'payload_hash', out_of_memory)
    with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='memory'):
        semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1)
    assert semantic_drain._cloud_selection_progress is None
    assert not calls and not led.db.in_transaction


def test_cloud_selection_incomplete_invalid_suffix_advances(cloud, monkeypatch):
    from collections import Counter
    from types import SimpleNamespace
    led, _, _, calls = cloud
    for mid in range(2, 13):
        led.save_messages([_message(mid)])
        led.semantic_seed(1, [mid], {'source': 'history_import'})
    snapshot(led)
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)
    ready = cloud_policy.history_target_ready
    observed = Counter()

    def sparse(db, pid, mid):
        observed[mid] += 1
        if 2 <= mid <= 11:
            clock[0] += .4
            return False
        return ready(db, pid, mid)

    monkeypatch.setattr(cloud_policy, 'history_target_ready', sparse)
    delivered = []
    incomplete = 0
    for _ in range(15):
        clock[0] = 0.0
        try:
            batch = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1, deadline=1.0)[0]
        except semantic_drain._CloudSelectionIncomplete:
            incomplete += 1
            continue
        if batch:
            mid = batch[0]['message_id']
            delivered.append(mid)
            with led.db:
                led.db.execute("UPDATE fetch_jobs SET state='done' WHERE job_id=?", (batch[0]['job_id'],))
        if delivered == [1, 12]:
            break
    assert incomplete >= 2 and delivered == [1, 12]
    assert all(observed[mid] == 1 for mid in range(2, 12))
    assert not calls and not led.db.in_transaction


def test_cloud_run_due_keeps_unconsumed_lookahead_across_ticks(cloud, monkeypatch):
    led, cfg, _, calls = cloud
    for mid in range(2, 13):
        led.save_messages([_message(mid)])
        led.semantic_seed(1, [mid], {'source': 'history_import'})
    snapshot(led)
    served = []

    def process(ledger, scfg, job, jev, llm, deadline, **kwargs):
        served.append(job['message_id'])
        with ledger.db:
            ledger.db.execute("UPDATE fetch_jobs SET state='done' WHERE job_id=?", (job['job_id'],))
        return 'done'

    monkeypatch.setattr(semantic_drain, '_process_job', process)
    for _ in range(12):
        out = semantic_drain.run_due(led, cfg, {'errors': []}, time.monotonic() + 1200,
                                     lane='backlog', max_jobs=1, cloud_only=True)
        assert out['done'] == 1
    assert served == list(range(1, 13))
    assert not calls and not rows(led, 'cloud_usage_reserve')


def test_cloud_selection_highwater_defers_new_job_to_next_cycle(cloud, monkeypatch):
    from types import SimpleNamespace
    led, _, _, calls = cloud
    led.save_messages([_message(2)])
    led.semantic_seed(1, [2], {'source': 'history_import'})
    continuous_snapshot(led)
    with led.db:
        led.db.execute('UPDATE messages SET first_seen=?', (time.time() - 601,))
        led.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 601,))
    monkeypatch.setattr(semantic_drain, '_CLOUD_SELECTION_PAGE', 1)
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)
    led.db.set_trace_callback(lambda sql: clock.__setitem__(0, 1.0)
        if sql.startswith('SELECT j.job_id,j.kind') else None)
    try:
        with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='deadline'):
            semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=1.0)
    finally:
        led.db.set_trace_callback(None)
    led.save_messages([_message(3)])
    led.semantic_seed(1, [3], {'source': 'history_import'})
    with led.db:
        led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=3', (time.time() - 601,))
        led.db.execute('UPDATE message_revisions SET observed_at=? WHERE message_id=3', (time.time() - 601,))
    clock[0] = 0.0
    first = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=10.0)[0]
    assert [r['message_id'] for r in first] == [1, 2]
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET state='done' WHERE message_id IN (1,2)")
    assert not semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=10.0)[0]
    later = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=10.0)[0]
    assert [r['message_id'] for r in later] == [3]
    assert not calls


def test_cloud_selection_invalidates_replaced_database_inode(cloud, tmp_path, monkeypatch):
    from types import SimpleNamespace
    led, _, _, calls = cloud
    led.save_messages([_message(2)])
    led.semantic_seed(1, [2], {'source': 'history_import'})
    snapshot(led)
    monkeypatch.setattr(semantic_drain, '_CLOUD_SELECTION_PAGE', 1)
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)

    def interrupt_after_page():
        clock[0] = 0.0
        led.db.set_trace_callback(lambda sql: clock.__setitem__(0, 1.0)
            if sql.startswith('SELECT j.job_id,j.kind') else None)
        try:
            with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='deadline'):
                semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1, deadline=1.0)
        finally:
            led.db.set_trace_callback(None)

    interrupt_after_page()
    old = semantic_drain._cloud_selection_progress['binding']
    path = led.db.execute('PRAGMA database_list').fetchone()[2]
    replacement = tmp_path / 'synthetic-replacement.db'
    dest = sqlite3.connect(replacement)
    try:
        led.db.backup(dest)
    finally:
        dest.close()
    led.close()
    os.replace(replacement, path)
    led.db = sqlite3.connect(path)
    led.db.row_factory = sqlite3.Row
    interrupt_after_page()
    progress = semantic_drain._cloud_selection_progress
    assert progress['cursor'] == 1
    assert progress['binding'][0][0] == old[0][0]
    assert progress['binding'][0][1:] != old[0][1:]
    assert progress['binding'][1:] == old[1:]
    assert not calls and not led.db.in_transaction


@pytest.mark.parametrize("route,fail", [("fallback", False), ("fallback", True),
                                        ("cloud", False), ("local", False)])
def test_model_timing_counts_nested_local_fallback_once(cloud, monkeypatch, route, fail):
    led, cfg, job, _ = cloud
    clock = [100.0]
    calls = []
    monkeypatch.setattr(time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(semantic_drain.semantic_store, "refresh_cards_allowed", lambda *a: False)

    def local(prompt, timeout=None):
        calls.append(("local", prompt, timeout))
        clock[0] += 2.0
        if fail and len(calls) == 2:
            raise ValueError("synthetic failure")
        return "synthetic response"

    @contextmanager
    def fake_backlog(*args, local_fallback=None, **kwargs):
        def call(prompt, timeout=None):
            clock[0] += 1.0
            try:
                if route == "fallback":
                    return local_fallback(prompt, timeout=timeout)
                calls.append(("cloud", prompt, timeout))
                clock[0] += 2.0
                return "synthetic response"
            finally:
                clock[0] += 1.0
        yield call

    def stages(ledger, scfg, selected, jev, llm, deadline, **kwargs):
        llm("first", timeout=10)
        llm("second", timeout=10)
        return "done"

    monkeypatch.setattr(transport, "backlog_inference", fake_backlog)
    monkeypatch.setattr(semantic_drain, "_process_job_inner", stages)
    timed, acc = semantic_drain._timed_llm(local)
    acc["s"] = 7.0
    def process():
        return semantic_drain._process_job(
            led, semantic_drain.runtime_config(cfg)[0], job, None, timed, 10000,
            lane="backlog", diagnostics={}, llm_timing=acc,
            cloud_transport=direct if route != "local" else None)
    if fail:
        with pytest.raises(ValueError, match="synthetic failure"):
            process()
    else:
        assert process() == "done"
    assert calls == [("local" if route != "cloud" else "cloud", "first", 10),
                     ("local" if route != "cloud" else "cloud", "second", 10)]
    assert acc["s"] == (11.0 if route == "local" else 15.0)
