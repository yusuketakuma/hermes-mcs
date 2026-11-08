"""Synthetic backlog cloud routing, accounting and run-lock loss contracts."""
import extract_llm_admission
import extract_llm_carry
import extract_llm_context
import extract_llm_errors
import extract_llm_prepare
import extract_llm_progress
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
import semantic_due_selection
import semantic_job_processor
import json
import os
import sqlite3
import time
from contextlib import contextmanager
import pytest
import anthropic_client
import cloud_history_scope
import cloud_policy
import extract_llm
import cloud_inference
import inference_dependency_loss
import semantic_drain
import semantic_transport as transport
import writer_lock
from semantic_testkit import _message, _patient
from semantic_cloud_testkit import cloud as cloud, direct as direct, rows as rows, snapshot as snapshot
from test_cloud_response_recovery import legacy_cloud_routes


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

    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', Provider)
    with legacy_cloud_routes():
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
    with legacy_cloud_routes():
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
    with legacy_cloud_routes():
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
            with pytest.raises(transport.runtime.LLMNotSent, match='cloud_unit_unresolved'):
                chat(prompt)
    reserves, results = rows(led, "cloud_usage_reserve"), rows(led, "cloud_usage_result")
    assert len(calls) == len(reserves) == len(results) == 1
    result = json.loads(results[0]["meta"])
    assert result["error_kind"] is None and result["usage"]["output_tokens"] == 10
    assert float(result["cost_usd"]) > 0 and "synthetic-hidden" not in json.dumps(result)
    assert semantic_runtime._circuit_state(led, kind="cloud_circuit")["consecutive_failures"] == 0


@pytest.mark.parametrize("detail,stage", [
    ("synthetic refusal", "cloud_not_sent"),
    ("checkpoint_unverified", "checkpoint_unverified"),
    ("cloud_admission_unavailable", "cloud_guard"),
])
def test_budget_refusal_keeps_settled_receipts_without_discard(cloud, monkeypatch, detail, stage):
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
            if detail == 'cloud_admission_unavailable':
                raise cloud_inference.CloudObservationIncomplete(detail)
            raise extract_llm.cloud_client.CloudNotSent('budget_exceeded', detail)

        def discard(self):
            self.discarded = True

    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', Provider)
    with pytest.raises(transport.runtime.LLMNotSent, match=stage):
        with legacy_cloud_routes():
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
        with legacy_cloud_routes():
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
        raise cloud_inference.CloudFallback('synthetic_unavailable')

    monkeypatch.setattr(cloud_inference.CloudInferenceUnit, '__call__', unavailable)
    def local_chat(prompt, **kwargs):
        local.append(transport.inference_model())
        return '{"facts": []}'

    with legacy_cloud_routes():
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct, local_fallback=local_chat,
                                         fallback_current=lambda mid: True) as chat:
            assert json.loads(chat('synthetic first')) == {'facts': []}
            assert json.loads(chat('synthetic second')) == {'facts': []}
    assert len(attempts) == 1 and local == [transport.llm_model()] * 2
    assert not rows(led, 'cloud_usage_reserve')


@pytest.mark.parametrize('change', ['during', 'between'])
def test_local_fallback_rechecks_scope_on_each_call(cloud, monkeypatch, change):
    led, cfg, job, _ = cloud
    state = {'current': True, 'calls': 0}
    def unavailable(provider, *args, **kwargs):
        raise cloud_inference.CloudFallback('synthetic_unavailable')
    def local_chat(prompt, **kwargs):
        state['calls'] += 1
        if change == 'during':
            state['current'] = False
        return '{"facts": []}'
    monkeypatch.setattr(cloud_inference.CloudInferenceUnit, '__call__', unavailable)
    with legacy_cloud_routes():
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: state['current'], direct, local_fallback=local_chat,
                                         fallback_current=lambda mid: True) as chat:
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
        raise cloud_inference.CloudFallback('synthetic_unavailable')

    monkeypatch.setattr(cloud_inference.CloudInferenceUnit, '__call__', unavailable)
    with legacy_cloud_routes():
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct, local_fallback=lambda *a, **kw: pytest.fail('local'),
                                         fallback_current=lambda mid: state != 'partial') as chat:
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
    monkeypatch.setattr(extract_llm_progress, '_qc_flagged_sql', extract_llm._qc_flagged_sql)
    monkeypatch.setattr(extract_llm_qc, '_qc_flagged_sql', extract_llm._qc_flagged_sql)
    monkeypatch.setattr(extract_llm_selection, '_qc_flagged_sql', extract_llm._qc_flagged_sql)
    with legacy_cloud_routes():
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
    with legacy_cloud_routes():
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct) as chat:
            if denial == "cap":
                # The configured route must remain recoverable. A new request is
                # still refused before any reservation or HTTP attempt.
                assert callable(chat)
                with pytest.raises(semantic_drain.runtime.LLMNotSent):
                    chat("synthetic historical input")
            else:
                assert chat is None
    assert not calls and not rows(led, "cloud_usage_reserve")


def test_eligible_background_job_after_one_hour_uses_cloud(cloud):
    led, cfg, job, calls = cloud
    with led.db:
        led.db.execute("UPDATE messages SET notified_at=? WHERE message_id=1", (time.time() - 3600,))
        payload = {**json.loads(job['payload']), 'eligible': True}
        led.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?", (json.dumps(payload), job['job_id']))
    job['payload'] = json.dumps(payload)
    with legacy_cloud_routes():
        selected = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1)[0]
    assert [row['job_id'] for row in selected] == [job['job_id']]
    with legacy_cloud_routes():
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120, lambda: True, direct) as chat:
            assert chat and json.loads(chat('synthetic historical prompt')) == {'facts': []}
    assert len(calls) == 1


def test_observe_counts_history_jobs_without_reporting_realtime_as_cloud(cloud):
    import cloud_cli
    led, cfg, job, _ = cloud
    settings = cloud_policy.cloud_config(cfg)[0]
    settings["routes"] = ["semantic_backlog"]
    with legacy_cloud_routes():
        counts = cloud_cli._route_counts(led.db, cfg, settings, time.monotonic() + 5)
    assert counts["semantic_backlog"]["targets"] == 1
    assert counts["semantic_backlog"]["requests_estimate"] == 2
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
            (json.dumps({**json.loads(job["payload"]), "eligible": True}), job["job_id"]))
    with legacy_cloud_routes():
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

    with legacy_cloud_routes():
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
        with legacy_cloud_routes():
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
    monkeypatch.setattr(semantic_job_processor, '_process_job_inner', semantic_drain._process_job_inner)
    monkeypatch.setattr(semantic_drain.semantic_store, "refresh_cards_allowed", lambda *a: False)
    timing = {"s": 0.0}
    model = transport.llm_model()
    with legacy_cloud_routes():
        assert semantic_drain._process_job(
            led, semantic_drain.runtime_config(cfg)[0], job, None,
            lambda *a, **kw: pytest.fail("local GPU used"), time.monotonic() + 120,
            lane="backlog", diagnostics={}, cloud_transport=direct, llm_timing=timing) == "done"
    assert len(calls) == 1 and timing["s"] > 0
    assert transport.llm_model() == model


@pytest.mark.parametrize("cloud_only", [False, True])
def test_cloud_stages_remain_current_for_normal_readers_after_context_exit(cloud, monkeypatch, cloud_only):
    import semantic_store
    import semantic_publication
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
        monkeypatch.setattr(semantic_publication, "reproject_stale", lambda *a: pytest.fail("worker repeated corpus projection"))
        monkeypatch.setattr(semantic_drain.llm_availability, "circuit_open_s", lambda *a: 60)
    result = {"errors": []}
    with legacy_cloud_routes():
        out = semantic_drain.run_due(led, cfg, result, time.monotonic() + 1200,
                                    jev_client=_PassJev(), lane="backlog", max_jobs=1,
                                    cloud_only=cloud_only)
    assert out["done"] == 1 and not result["errors"], (out, result)
    assert calls
    fp = semantic_store.thread_bundle(led.db, 1, 1)["source_fingerprint"]
    for kind in ("semantic_summary", "semantic_audit"):
        assert semantic_store.current_artifact(led.db, kind, 1, fp) is not None
    published = rows(led, semantic_publication.KIND_V4)
    assert published and json.loads(published[-1]["meta"])["fingerprint"] == fp
    assert published[-1]["model"] == transport.llm_model()
    assert json.loads(published[-1]["meta"])["inference_requests"]
    semantic_publication.invalidate_projections(led.db, semantic_drain.runtime_config(cfg)[0])
    assert semantic_store.current_artifact(led.db, semantic_publication.KIND_V4, 1, fp) is not None
    from extraction_progress import message_progress
    row = led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    assert message_progress(led.db, row, cfg=cfg)["state"] == "complete"


def test_full_cloud_capacity_defers_without_usage_or_local_fallback(cloud):
    import cloud_backlog
    import semantic_runtime
    led, cfg, job, calls = cloud
    cfg["cloud_llm"]["max_concurrency"] = 1
    with cloud_backlog.request_slot(extract_llm.HOME, 1):
        with legacy_cloud_routes():
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
    monkeypatch.setattr(semantic_job_processor, '_process_job_inner', semantic_drain._process_job_inner)
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
    monkeypatch.setattr(semantic_job_processor, '_process_job_inner', semantic_drain._process_job_inner)
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
    with legacy_cloud_routes():
        selected = semantic_drain._select_cloud_due(led, "backlog", ("semantic",), False, 3)[0]
    assert [r["project_id"] for r in selected] == [2, 1, 3]
    with legacy_cloud_routes():
        selected = semantic_drain._select_due(led, "backlog", ("semantic",), False, 3,
                                             prioritize_patients=True)[0]
    assert [r["project_id"] for r in selected] == [2, 1, 3]
    with legacy_cloud_routes():
        selected, _ = extract_llm._select_pending(led, 3, None, None, False,
                                                  cloud_only=True, prioritize_patients=True)
    assert [r["project_id"] for r in selected] == [2, 1, 3]


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
            with legacy_cloud_routes():
                selected = semantic_drain._select_cloud_due(led, "backlog", ("semantic",), False, 10)[0]
        else:
            with legacy_cloud_routes():
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

    monkeypatch.setattr(cloud_history_scope, "_history_thread_allowed", count)
    if lane == "semantic":
        # Semantic jobs collapse to the thread root, so exercise multiple
        # member candidates through the same check used by its selector.
        with cloud_policy.history_selection_check(led.db) as check:
            selected = [check(1, mid) for mid in range(1, 8)]
        assert all(selected)
    else:
        with legacy_cloud_routes():
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
            with legacy_cloud_routes():
                selected = semantic_drain._select_cloud_due(
                    led, "backlog", ("semantic",), False, 1, deadline=time.monotonic() + 60)[0]
        else:
            with legacy_cloud_routes():
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
    monkeypatch.setattr(semantic_due_selection, '_select_cloud_due', semantic_drain._select_cloud_due)
    monkeypatch.setattr(semantic_drain, "_process_job_inner",
                        lambda *args, **kwargs: pytest.fail("incomplete selection started work"))
    monkeypatch.setattr(semantic_job_processor, '_process_job_inner', semantic_drain._process_job_inner)
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
        with legacy_cloud_routes():
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
        with legacy_cloud_routes():
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
    monkeypatch.setattr(extract_llm_context, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(extract_llm, "_open_current_ledger",
                        lambda **kwargs: type("DB", (), {"close": lambda s: None})())
    monkeypatch.setattr(extract_llm_resident, '_open_current_ledger', extract_llm._open_current_ledger)
    monkeypatch.setattr(extract_llm, "_writer_current", lambda **kw: True)
    monkeypatch.setattr(extract_llm_carry, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_errors, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_resident, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_runner, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_selection, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_source_contract, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract_llm.cloud_backlog.CloudBacklogPool, "sync", lambda *a, **kw: None)
    monkeypatch.setattr(extract_llm, "legacy_admissions", lambda *a: None)
    monkeypatch.setattr(extract_llm_admission, 'legacy_admissions', extract_llm.legacy_admissions)
    monkeypatch.setattr(extract_llm_resident, 'legacy_admissions', extract_llm.legacy_admissions)
    monkeypatch.setattr(extract_llm, "_recent_arrivals", lambda *a: {99})
    monkeypatch.setattr(extract_llm_resident, '_recent_arrivals', extract_llm._recent_arrivals)
    monkeypatch.setattr(extract_llm, "_settle_carry", lambda *a, **kw: None)
    monkeypatch.setattr(extract_llm_resident, '_settle_carry', extract_llm._settle_carry)
    monkeypatch.setattr(extract_llm_runner, '_settle_carry', extract_llm._settle_carry)
    monkeypatch.setattr(extract_llm, "_background_semantic", lambda *a: {
        "job_metrics": [{"kind": "semantic", "status": "done"}]})
    monkeypatch.setattr(extract_llm_resident, '_background_semantic', extract_llm._background_semantic)
    monkeypatch.setattr(extract_llm.sys, "argv", [
        "extract_llm", "--all", "--lane", "2", "--semantic", "--stop-after", "150"])
    seen = []

    def run(*a, **kw):
        now[0] += 30
        seen.append(kw)
        return {"done": 1, "failed": 0, "left": 1, "selected": 1, "carry": []}

    monkeypatch.setattr(extract_llm, "run_pending", run)
    monkeypatch.setattr(extract_llm_resident, 'run_pending', extract_llm.run_pending)
    assert extract_llm.main() == 0
    history = [r for r in seen if r["oldest_first"]]
    assert history and history[0]["limit"] == 4
    assert history[0]["allow_cloud"] and history[0]["background"]
    assert all(r["limit"] == 1 and r["allow_cloud"] and r["background"]
               for r in seen if not r["oldest_first"])


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
        with legacy_cloud_routes():
            out = semantic_drain.run_due(led, cfg, {'errors': []}, time.monotonic() + 1200,
                                         lane='backlog', max_jobs=1, cloud_only=True)
        assert out['done'] == 1
    assert served == list(range(1, 13))
    assert not calls and not rows(led, 'cloud_usage_reserve')


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
    monkeypatch.setattr(semantic_job_processor, '_process_job_inner', semantic_drain._process_job_inner)
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


@pytest.mark.parametrize('reason,expected', [('cloud_unit_unresolved', 'cloud_unit_unresolved'),
                                             ('synthetic-private-reason', 'unclassified')])
def test_semantic_defer_counts_are_fixed_and_do_not_expose_unknown_reason(cloud, monkeypatch, reason, expected):
    led, cfg, _, calls = cloud
    def process(*args, **kwargs):
        kwargs['diagnostics']['not_sent'] = reason
        return 'deferred'
    monkeypatch.setattr(semantic_drain, '_process_job', process)
    with legacy_cloud_routes():
        out = semantic_drain.run_due(led, cfg, {'errors': []}, time.monotonic() + 1200,
                                    lane='backlog', max_jobs=1, cloud_only=True)
    assert out['deferred'] == 1 and out['defer_reasons'] == {expected: 1}
    assert not calls and not rows(led, 'cloud_usage_reserve')


def _member_targets(led, job):
    """Grow the job's targets with a reply member, then re-capture the scope."""
    led.save_messages([_message(2, parent=1, body='synthetic member reply')])
    snapshot(led)
    payload = json.loads(job['payload'])
    payload['targets'] = [1, 2]
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                       (json.dumps(payload), job['job_id']))
    job['payload'] = json.dumps(payload)


def test_owned_member_reservation_recovers_paid_under_closed_route(cloud):
    led, cfg, job, calls = cloud
    _member_targets(led, job)
    with transport.inference_targets('synthetic-gen'):
        with legacy_cloud_routes():
            with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                             lambda: True, direct) as chat:
                transport.set_inference_target(2)
                assert json.loads(chat('synthetic member prompt')) == {'facts': []}
        assert len(calls) == len(rows(led, 'cloud_usage_reserve')) == 1
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda *a, **kw: pytest.fail('paid local fallback')) as chat:
            transport.set_inference_target(2)
            assert json.loads(chat('synthetic member prompt')) == {'facts': []}
    # The paid response was recovered on the member unit — no second POST.
    assert len(calls) == len(rows(led, 'cloud_usage_reserve')) == len(rows(led, 'cloud_usage_result')) == 1


def test_owned_member_unknown_unit_holds_without_local_restart(cloud):
    led, cfg, job, calls = cloud
    _member_targets(led, job)
    # A reservation already paid for member 2 whose outcome never arrived.
    selection, errors = cloud_policy.resolve_profile(cfg, route='semantic_backlog', purpose='backlog')
    assert not errors
    reserver = cloud_policy.usage_reserver(
        led, {'cloud_llm': cloud_inference.resolve_cloud_settings(cfg)},
        route='semantic_backlog', project_id=1, message_id=2,
        unit_key=f"semantic:{job['job_id']}:",
        owner={'semantic_target_id': 2, 'semantic_thread_root': 1},
        input_binding='ab' * 32, selection=selection)
    body = anthropic_client.AnthropicClient('synthetic').body('synthetic member prompt', 100, None)
    with legacy_cloud_routes():
        reserver(body, 30)
    local = []
    with transport.inference_targets('synthetic-gen'):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p)) as chat:
            assert chat is not None
            transport.set_inference_target(2)
            with pytest.raises(transport.runtime.LLMNotSent, match='cloud_unit_unresolved'):
                chat('synthetic member prompt')
    assert not local and not calls
    assert len(rows(led, 'cloud_usage_reserve')) == 1


def test_unowned_job_stays_local_under_closed_route(cloud):
    led, cfg, job, calls = cloud
    _member_targets(led, job)
    # A reservation bound outside this job's payload targets holds nothing here.
    led.save_messages([_message(3, body='synthetic unrelated thread')])
    selection, errors = cloud_policy.resolve_profile(cfg, route='semantic_backlog', purpose='backlog')
    assert not errors
    reserver = cloud_policy.usage_reserver(
        led, {'cloud_llm': cloud_inference.resolve_cloud_settings(cfg)},
        route='semantic_backlog', project_id=1, message_id=3,
        unit_key='semantic:other-job:',
        owner={'semantic_target_id': 3, 'semantic_thread_root': 3},
        input_binding='ab' * 32, selection=selection)
    with legacy_cloud_routes():
        reserver(anthropic_client.AnthropicClient('synthetic').body('synthetic other prompt', 100, None), 30)
    with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                     lambda: True, direct) as chat:
        assert chat is None
    assert not calls and len(rows(led, 'cloud_usage_reserve')) == 1


@pytest.mark.parametrize('meta', [{'note': 'synthetic route-less meta'}, '{broken'])
def test_semantic_owned_unit_holds_on_unverifiable_reservation(cloud, meta):
    led, cfg, job, calls = cloud
    _member_targets(led, job)
    with led.db:
        if isinstance(meta, str):
            led.db.execute(
                "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                "VALUES('cloud_usage_reserve',1,2,'{}','synthetic',?,?)", (meta, time.time()))
        else:
            led.artifact_add_tx('cloud_usage_reserve', '{}', project_id=1, message_id=2,
                                model='synthetic', meta=meta)
    local = []
    with transport.inference_targets('synthetic-gen'):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p)) as chat:
            assert chat is not None
            transport.set_inference_target(2)
            with pytest.raises(transport.runtime.LLMNotSent, match='cloud_unit_unresolved'):
                chat('synthetic member prompt')
    assert not local and not calls
    assert len(rows(led, 'cloud_usage_reserve')) == 1


def _mixed_targets(led, job, order, bodies=None):
    """Grow the job's payload targets into `order` with reply members."""
    bodies = bodies or {}
    led.save_messages([_message(mid, parent=1,
                                body=bodies.get(mid, 'synthetic reply'))
                       for mid in order])
    snapshot(led)
    payload = json.loads(job['payload'])
    payload['targets'] = list(order)
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                       (json.dumps(payload), job['job_id']))
    job['payload'] = json.dumps(payload)


def _member_reservation(led, cfg, job, mid):
    """A durable reservation for member `mid` whose outcome never arrived."""
    selection, errors = cloud_policy.resolve_profile(cfg, route='semantic_backlog', purpose='backlog')
    assert not errors
    reserver = cloud_policy.usage_reserver(
        led, {'cloud_llm': cloud_inference.resolve_cloud_settings(cfg)},
        route='semantic_backlog', project_id=1, message_id=mid,
        unit_key=f"semantic:{job['job_id']}:",
        owner={'semantic_target_id': mid, 'semantic_thread_root': 1},
        input_binding='ab' * 32, selection=selection)
    body = anthropic_client.AnthropicClient('synthetic').body('synthetic member prompt', 100, None)
    with legacy_cloud_routes():
        reserver(body, 30)


def test_closed_route_independent_member_goes_local_while_owned_member_holds(cloud):
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, [3, 2])
    _member_reservation(led, cfg, job, 2)
    local = []
    with transport.inference_targets('synthetic-gen'):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p)) as chat:
            assert chat is not None
            transport.set_inference_target(3)
            chat('synthetic independent prompt')
            assert local == ['synthetic independent prompt']
            # A later owned member must not join the job-level local switch:
            # its unresolved unit stays a hold, never a POST or local result.
            transport.set_inference_target(2)
            with pytest.raises(transport.runtime.LLMNotSent, match='cloud_unit_unresolved'):
                chat('synthetic member prompt')
    assert local == ['synthetic independent prompt'] and not calls
    assert len(rows(led, 'cloud_usage_reserve')) == 1


def test_closed_route_owned_member_holds_before_independent_member_goes_local(cloud):
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, [2, 3])
    _member_reservation(led, cfg, job, 2)
    local = []
    with transport.inference_targets('synthetic-gen'):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p)) as chat:
            transport.set_inference_target(2)
            with pytest.raises(transport.runtime.LLMNotSent, match='cloud_unit_unresolved'):
                chat('synthetic member prompt')
            transport.set_inference_target(3)
            chat('synthetic independent prompt')
    assert local == ['synthetic independent prompt'] and not calls
    assert len(rows(led, 'cloud_usage_reserve')) == 1


def test_closed_route_paid_member_recovers_while_independent_member_goes_local(cloud):
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, [3, 2])
    with transport.inference_targets('synthetic-gen'):
        with legacy_cloud_routes():
            with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                             lambda: True, direct) as chat:
                transport.set_inference_target(2)
                assert json.loads(chat('synthetic member prompt')) == {'facts': []}
        assert len(calls) == len(rows(led, 'cloud_usage_reserve')) == 1
        local = []
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p)) as chat:
            transport.set_inference_target(2)
            assert json.loads(chat('synthetic member prompt')) == {'facts': []}
            assert not local
            transport.set_inference_target(3)
            chat('synthetic independent prompt')
    # The paid member answer was adopted without another POST; only the
    # independent member took the local switch.
    assert local == ['synthetic independent prompt'] and len(calls) == 1
    assert len(rows(led, 'cloud_usage_reserve')) == len(rows(led, 'cloud_usage_result')) == 1


@pytest.mark.parametrize('meta', [{'note': 'synthetic route-less meta'}, '{broken'])
def test_closed_route_unverifiable_member_reservation_holds_after_sibling_switch(cloud, meta):
    """A reservation bound to the member's message that cannot prove member
    ownership — route-less or unparsable meta — is unproven, never 'not
    owned': the sibling's local switch must not release it to the local path.
    While member evidence cannot even be read, member 3's own send is also
    unprovable (the pre-send check marks it possibly_sent), so it holds too
    rather than silently replacing a request that may have left."""
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, [3, 2])
    with led.db:
        if isinstance(meta, str):
            led.db.execute(
                "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                "VALUES('cloud_usage_reserve',1,2,'{}','synthetic',?,?)", (meta, time.time()))
        else:
            led.artifact_add_tx('cloud_usage_reserve', '{}', project_id=1, message_id=2,
                                model='synthetic', meta=meta)
    local = []
    with transport.inference_targets('synthetic-gen'):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p)) as chat:
            transport.set_inference_target(3)
            with pytest.raises(transport.runtime.LLMNotSent, match='cloud_unit_unresolved'):
                chat('synthetic independent prompt')
            transport.set_inference_target(2)
            with pytest.raises(transport.runtime.LLMNotSent, match='cloud_unit_unresolved'):
                chat('synthetic member prompt')
    assert local == [] and not calls
    assert len(rows(led, 'cloud_usage_reserve')) == 1


@pytest.mark.parametrize('order', [[2, 3], [3, 2]])
@pytest.mark.parametrize('held,expects_local', [
    ('member_unit', True), ('broken_meta', False),
    ('route_less_meta', False)])
def test_process_job_member_hold_keeps_independent_member_local(cloud, order, held, expects_local):
    """The real _process_job: a member-bound hold stops only that member —
    the independent member still extracts and summarizes locally in the same
    pass, the job defers, nothing is resent or locally replaced.

    With member 2's evidence unreadable (broken/route-less), member 3's own
    pre-send check cannot complete either — it is marked possibly_sent and
    holds too, so only its local absence is asserted. With a readable unit,
    member 3 still completes locally."""
    from semantic_testkit import _PassJev, _llm_v2
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, order, bodies={2: 'アムロジピン5mgを継続します。',
                                            3: 'メトホルミン500mgが出ています'})
    if held == 'member_unit':
        _member_reservation(led, cfg, job, 2)
    elif held == 'broken_meta':
        with led.db:
            led.db.execute(
                "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                "VALUES('cloud_usage_reserve',1,2,'{}','synthetic','{broken',?)", (time.time(),))
    else:
        with led.db:
            led.artifact_add_tx('cloud_usage_reserve', '{}', project_id=1, message_id=2,
                                model='synthetic', meta={'note': 'synthetic route-less meta'})
    local_prompts = []
    def local(prompt, **kwargs):
        local_prompts.append(prompt)
        return _llm_v2(prompt)
    status = semantic_drain._process_job(
        led, semantic_drain.runtime_config(cfg)[0], job, _PassJev(), local,
        time.monotonic() + 900, lane="backlog", diagnostics=semantic_drain._job_diagnostics(),
        cloud_transport=direct)
    assert status == 'deferred'
    assert not calls and len(rows(led, 'cloud_usage_reserve')) <= 1
    assert any('メトホルミン' in p for p in local_prompts) is expects_local
    assert not any('アムロジピン' in p for p in local_prompts)


@pytest.mark.parametrize('order', [[2, 3, 4], [4, 3, 2]])
def test_process_job_unproven_chunk_holds_its_member_only(cloud, order):
    """A member's own persisted chunk whose producer cannot be proven refuses
    the local fallback — but only for that member: the owned member stays
    unresolved, the clean member still completes locally."""
    from semantic_testkit import _PassJev, _llm_v2
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, order, bodies={2: 'アムロジピン5mgを継続します。',
                                            3: 'メトホルミン500mgが出ています',
                                            4: 'ロサルタン50mgを開始しました'})
    _member_reservation(led, cfg, job, 2)
    with led.db:
        # member 3's own persisted chunk cannot prove its producer — its
        # local fallback must refuse while member 4's stays clear.
        led.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
            "VALUES('semantic_extraction_chunk',1,3,'{}','synthetic','{broken',?)", (time.time(),))
    local_prompts = []
    def local(prompt, **kwargs):
        local_prompts.append(prompt)
        return _llm_v2(prompt)
    status = semantic_drain._process_job(
        led, semantic_drain.runtime_config(cfg)[0], job, _PassJev(), local,
        time.monotonic() + 900, lane="backlog", diagnostics=semantic_drain._job_diagnostics(),
        cloud_transport=direct)
    assert status == 'deferred'
    assert not calls and len(rows(led, 'cloud_usage_reserve')) == 1
    assert any('ロサルタン' in p for p in local_prompts)
    assert not any('アムロジピン' in p for p in local_prompts)
    assert not any('メトホルミン' in p for p in local_prompts)


@pytest.mark.parametrize('order', [[2, 3], [3, 2]])
@pytest.mark.parametrize('variant', ['unverifiable_cfg', 'mode_omitted', 'key_gone',
                                     'explicit_off', 'dry_run', 'out_of_scope'])
def test_process_job_owned_scope_proven_on_resolved_settings(cloud, variant, order, monkeypatch):
    """Ownership precedes settings: a config that cannot prove scope keeps
    the owned member held (unverifiable), a mode-omitted block resolves on
    via the key, a vanished key only computes 'off' — never a revocation —
    and only a declared retirement (explicit off / out of scope) releases
    the unit to local. In every held variant the independently provable
    unowned member completes locally without touching a CloudInferenceUnit
    or a credential — in either member order."""
    from semantic_testkit import _PassJev, _llm_v2
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, order, bodies={2: 'アムロジピン5mgを継続します。',
                                            3: 'メトホルミン500mgが出ています'})
    _member_reservation(led, cfg, job, 2)
    # The reservation was made under the working config; mutate it after,
    # as a deployment edit would.
    if variant == 'unverifiable_cfg':
        cfg['cloud_llm']['bogus_field'] = 1
    elif variant == 'mode_omitted':
        del cfg['cloud_llm']['mode']
    elif variant == 'key_gone':
        del cfg['cloud_llm']['mode']
        monkeypatch.setattr(cloud_policy, 'api_key', lambda *a: None)
        monkeypatch.setattr(cloud_policy, 'provider_key', lambda *a, **kw: None)
    elif variant == 'explicit_off':
        cfg['cloud_llm']['mode'] = 'off'
    elif variant == 'dry_run':
        cfg['cloud_llm']['mode'] = 'dry_run'
    else:
        cfg['cloud_llm']['project_ids'] = [2]
    local_prompts = []
    def local(prompt, **kwargs):
        local_prompts.append(prompt)
        transport._record_inference('local')   # the same trace llm_chat leaves
        return _llm_v2(prompt)
    status = semantic_drain._process_job(
        led, semantic_drain.runtime_config(cfg)[0], job, _PassJev(), local,
        time.monotonic() + 900, lane="backlog", diagnostics=semantic_drain._job_diagnostics(),
        cloud_transport=direct)
    assert not calls and len(rows(led, 'cloud_usage_reserve')) == 1
    facts_v2 = {row['message_id']: json.loads(row['meta']).get('provider_provenance')
                for row in rows(led, semantic_drain.KIND_FACTS_V2)}
    if variant in ('unverifiable_cfg', 'key_gone', 'mode_omitted'):
        # Held member: nothing local, nothing sent, the reservation stays,
        # and no terminal completed artifact exists for it.  The independent
        # member completes locally and its persisted result is local-derived.
        assert status == 'deferred'
        assert any('メトホルミン' in p for p in local_prompts)
        assert not any('アムロジピン' in p for p in local_prompts)
        assert facts_v2.get(3, {}).get('providers') == ['local']
        assert 2 not in facts_v2
    else:
        # Proven revocation: the owned member is released to local too.
        assert any('アムロジピン' in p for p in local_prompts)
        assert any('メトホルミン' in p for p in local_prompts)


def test_closed_route_ambiguous_member_history_never_switches_local(cloud, monkeypatch):
    """A strict non-member unit bound to the member's exact request plus a
    duplicated result is ambiguous accounting — never a proven-unbound
    member: recover_response's cloud_unit_unresolved leaves possibly_sent
    unset, and the CloudNotSent catch must not slip the member to local."""
    import mcs_util
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, [3, 2])
    _member_reservation(led, cfg, job, 2)
    # A complete strict non-member unit on member 3: no member fields, the
    # row/meta message ids match — provably not member 3's own reservation.
    # The real reserver's own guard refuses a second unresolved unit, so the
    # row clones the member-2 receipt with member fields stripped.
    original = dict(rows(led, 'cloud_usage_reserve')[0])
    meta = json.loads(original['meta'])
    meta.update(message_id=3, logical_target_id=None,
                owner={'semantic_thread_root': 1},
                unit_key=f"semantic:{job['job_id']}:{json.loads(job['payload']).get('generation', '')}",
                reservation_id='d' * 32)
    with led.db:
        led.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
            "VALUES('cloud_usage_reserve',1,3,'{}',?,?,?)",
            (original['model'], json.dumps(meta), time.time()))
        for _ in range(2):
            # Two results on one reservation can never settle — the unit's
            # history is ambiguous, so member 3 is never proven unbound.
            led.db.execute(
                "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                "VALUES('cloud_usage_result',1,3,'{}','synthetic',?,?)",
                (json.dumps({'reservation_id': 'd' * 32, 'route': 'semantic_backlog',
                             'unit_key': meta['unit_key'], 'message_id': 3}),
                 time.time()))
    local = []
    captured = {}
    real_reason = cloud_policy.request_send_block_reason
    def spy(ledger_, cfg_, body, **kw):
        if kw.get('message_id') == 3 and 'input_binding' not in captured:
            captured['input_binding'] = kw.get('input_binding')
            captured['request_fp'] = mcs_util.payload_hash(body)
        return real_reason(ledger_, cfg_, body, **kw)
    monkeypatch.setattr(cloud_policy, 'request_send_block_reason', spy)
    with transport.inference_targets('synthetic-gen'):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p),
                                         fallback_current=lambda mid: True) as chat:
            transport.set_inference_target(3)
            with pytest.raises(transport.runtime.LLMNotSent):
                chat('synthetic member prompt')
        assert 'request_fp' in captured
        with led.db:
            # Bind the strict non-member unit to member 3's exact request, so
            # the next pass reaches recover_response and its torn result set.
            meta.update(input_binding=captured['input_binding'],
                        request_fp=captured['request_fp'])
            led.db.execute("UPDATE artifacts SET meta=? "
                           "WHERE kind='cloud_usage_reserve' AND message_id=3",
                           (json.dumps(meta),))
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p),
                                         fallback_current=lambda mid: True) as chat:
            transport.set_inference_target(3)
            with pytest.raises(transport.runtime.LLMNotSent, match='cloud_fallback_unavailable'):
                chat('synthetic member prompt')
    assert not local and not calls
    assert len(rows(led, 'cloud_usage_reserve')) == 2
    assert len(rows(led, 'cloud_usage_result')) == 2


def _member_unit_reservation(led, cfg, job, mid, source, *, generation='other-gen',
                             phase='facts', rid='e' * 32, **overrides):
    """A durable member-bound reservation whose verified semantic unit
    identity is (mid, source, phase) via logical_unit_fp — cloned from a
    real reservation so every receipt shape field stays valid."""
    import mcs_util
    original = dict(rows(led, 'cloud_usage_reserve')[0])
    meta = json.loads(original['meta'])
    meta.update(message_id=mid, logical_target_id=mid,
                owner={'semantic_target_id': mid, 'semantic_thread_root': 1,
                       'semantic_source': source, 'semantic_generation': generation,
                       'semantic_phase': phase},
                logical_unit_fp=mcs_util.payload_hash(
                    {'target': mid, 'source': source, 'generation': generation,
                     'phase': phase}),
                unit_key=f"semantic:{job['job_id']}:{json.loads(job['payload']).get('generation', '')}",
                reservation_id=rid)
    meta.update(overrides)
    with led.db:
        led.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
            "VALUES('cloud_usage_reserve',1,?,'{}',?,?,?)",
            (mid, original['model'], json.dumps(meta), time.time()))


@pytest.mark.parametrize('order', [[2, 3], [3, 2]])
def test_closed_route_old_source_unit_does_not_own_new_source_member(cloud, order):
    """A member-bound reservation for a different verified source is a
    different paid unit: it stays a send guard but does not own the new
    source's member — local analysis proceeds while the owned member holds."""
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, order, bodies={2: 'アムロジピン5mgを継続します。',
                                            3: 'メトホルミン500mgが出ています'})
    _member_reservation(led, cfg, job, 2)
    _member_unit_reservation(led, cfg, job, 3, 'a' * 64)
    local = []
    with transport.inference_targets('synthetic-gen'):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p),
                                         fallback_current=lambda mid: True) as chat:
            transport.set_inference_target(3)
            chat('synthetic member prompt')
            assert local == ['synthetic member prompt']
            transport.set_inference_target(2)
            with pytest.raises(transport.runtime.LLMNotSent, match='cloud_unit_unresolved'):
                chat('synthetic member prompt')
            assert local == ['synthetic member prompt']
    assert not calls and len(rows(led, 'cloud_usage_reserve')) == 2


def test_closed_route_prior_real_source_unit_frees_edited_source_member(cloud):
    """A stored unit bound to the member's actual pre-edit source does not
    own the post-edit source: the compared identity is the live occurrence
    derivation — the same binding source_binding computes for the request."""
    import mcs_util
    from inference_dependency_loss import source_binding
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, [3, 2])
    _member_reservation(led, cfg, job, 2)
    old_source = mcs_util.payload_hash([(3, source_binding(led.db, 1, 3))])
    _member_unit_reservation(led, cfg, job, 3, old_source)
    with led.db:
        # A real post edit: body revision plus a fresh revision row, then the
        # receipted history scope re-captures the current occurrences.
        max_seq = led.db.execute(
            "SELECT COALESCE(MAX(seq),0) FROM message_revisions WHERE message_id=3").fetchone()[0]
        led.db.execute("INSERT INTO message_revisions(message_id,seq,content_hash,body_state,"
                       "observed_at) VALUES(3,?,?,?,?)",
                       (max_seq + 1, 'f' * 64, 'full', time.time() - 601))
        led.db.execute("UPDATE messages SET body_text=? WHERE message_id=3",
                       ('編集済みの合成本文。',))
    snapshot(led)
    # The edit really moved the derived identity — this is not a label trick.
    assert mcs_util.payload_hash([(3, source_binding(led.db, 1, 3))]) != old_source
    local = []
    with transport.inference_targets('synthetic-gen'):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p),
                                         fallback_current=lambda mid: True) as chat:
            transport.set_inference_target(3)
            chat('synthetic member prompt')
            assert local == ['synthetic member prompt']
    assert not calls and len(rows(led, 'cloud_usage_reserve')) == 2


def test_closed_route_phase_different_unit_keeps_prefix_current_hold(cloud):
    """A summary-phase unit is a different paid unit from the facts request,
    but the fallback_current prefix check still owns the member's local path
    — identity release never weakens the cloud-prefix mixing protection."""
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, [3, 2])
    _member_reservation(led, cfg, job, 2)
    _member_unit_reservation(led, cfg, job, 3, 'a' * 64, phase='summary')
    local = []
    with transport.inference_targets('synthetic-gen'):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p),
                                         fallback_current=lambda mid: False) as chat:
            transport.set_inference_target(3)
            with pytest.raises(transport.runtime.LLMNotSent, match='cloud_fallback_unavailable'):
                chat('synthetic member prompt')
    assert not local and not calls


def test_closed_route_duplicate_member_key_smuggled_row_still_holds(cloud):
    """A row hiding member fields behind a duplicate owner key stays a
    candidate via json_extract (first key) — the strict receipt parse must
    reject it before the member/nonmember classification, never skipped as
    a proven non-member unit."""
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, [3, 2])
    _member_reservation(led, cfg, job, 2)
    original = dict(rows(led, 'cloud_usage_reserve')[0])
    meta = json.loads(original['meta'])
    meta.pop('owner', None)
    # json_extract sees the first owner key, json.loads the last — the member
    # fields disappear before classification while the row stays a candidate.
    smuggled = ('{"owner":{"semantic_target_id":3},"owner":null,'
                + json.dumps(meta, separators=(',', ':'))[1:])
    with led.db:
        led.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
            "VALUES('cloud_usage_reserve',1,3,'{}',?,?,?)",
            (original['model'], smuggled, time.time()))
    local = []
    with transport.inference_targets('synthetic-gen'):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p),
                                         fallback_current=lambda mid: True) as chat:
            transport.set_inference_target(3)
            with pytest.raises(transport.runtime.LLMNotSent):
                chat('synthetic member prompt')
    assert not local and not calls


def test_owns_member_request_malformed_identity_owns_on_empty_history(cloud):
    """With no reservation history at all a malformed current identity still
    owns the member — an unproven request never releases the local path."""
    led, cfg, job, calls = cloud
    assert cloud_policy.owns_member_request(
        led.db, route='semantic_backlog', project_id=1, target_id=3,
        identity='not-a-tuple') is True
    assert cloud_policy.owns_member_request(
        led.db, route='semantic_backlog', project_id=1, target_id=3,
        identity=(3, 'b' * 64, 'facts')) is False
    assert cloud_policy.owns_member_request(
        led.db, route='semantic_backlog', project_id=1, target_id=3) is False


def test_closed_route_same_source_new_generation_member_still_holds(cloud):
    """A member-bound reservation for the same verified (target, source,
    phase) still owns the member — a new job generation or model alone does
    not release the member's local path."""
    import mcs_util
    from inference_dependency_loss import source_binding
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, [3, 2])
    _member_reservation(led, cfg, job, 2)
    _member_unit_reservation(led, cfg, job, 3,
                             mcs_util.payload_hash([(3, source_binding(led.db, 1, 3))]))
    local = []
    with transport.inference_targets('synthetic-gen'):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p),
                                         fallback_current=lambda mid: True) as chat:
            transport.set_inference_target(3)
            with pytest.raises(transport.runtime.LLMNotSent):
                chat('synthetic member prompt')
    assert not local and not calls and len(rows(led, 'cloud_usage_reserve')) == 2


@pytest.mark.parametrize('mutation', [
    # Row/meta identity contradiction: receipt validates meta internals but
    # the row cannot be trusted — never evidence of a different unit.
    {'message_id': 2},
    # Member fields present but semantic_source missing: the unit's identity
    # cannot be proven — unknown paid state still holds.
    {'owner': {'semantic_target_id': 3, 'semantic_thread_root': 1}},
    # Fingerprint tampered: identity fields alone cannot prove the unit.
    {'logical_unit_fp': 'f' * 64},
    # Contradictory member fields: target itself cannot be established.
    {'logical_target_id': 4},
])
def test_closed_route_unverifiable_member_unit_still_holds(cloud, mutation):
    """A member-bound row whose identity cannot be verified — contradicting
    its row, missing or tampered identity fields — still owns the member."""
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, [3, 2])
    _member_reservation(led, cfg, job, 2)
    _member_unit_reservation(led, cfg, job, 3, 'a' * 64, **mutation)
    local = []
    with transport.inference_targets('synthetic-gen'):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p),
                                         fallback_current=lambda mid: True) as chat:
            transport.set_inference_target(3)
            with pytest.raises(transport.runtime.LLMNotSent):
                chat('synthetic member prompt')
    assert not local and not calls


def test_closed_route_different_source_ambiguous_accounting_holds(cloud):
    """Even a verified different-source unit cannot release the member when
    the accounting history is ambiguous — the r69 financial proof still
    gates every local path."""
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, [3, 2])
    _member_reservation(led, cfg, job, 2)
    _member_unit_reservation(led, cfg, job, 3, 'a' * 64)
    with led.db:
        for _ in range(2):
            led.db.execute(
                "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                "VALUES('cloud_usage_result',1,3,'{}','synthetic',?,?)",
                (json.dumps({'reservation_id': 'e' * 32, 'route': 'semantic_backlog',
                             'unit_key': f"semantic:{job['job_id']}:"
                             f"{json.loads(job['payload']).get('generation', '')}",
                             'message_id': 3}),
                 time.time()))
    local = []
    with transport.inference_targets('synthetic-gen'):
        with transport.backlog_inference(led, cfg, job, time.monotonic() + 120,
                                         lambda: True, direct,
                                         local_fallback=lambda p, timeout=None: local.append(p),
                                         fallback_current=lambda mid: True) as chat:
            transport.set_inference_target(3)
            with pytest.raises(transport.runtime.LLMNotSent):
                chat('synthetic member prompt')
    assert not local and not calls


@pytest.mark.parametrize('identity,owned', [
    ((3, 'b' * 64, 'facts'), False),   # valid, different source — not owning
    ((2, 'b' * 64, 'facts'), True),    # binds another member's identity
    ((0, 'b' * 64, 'facts'), True),    # non-positive target
    (('3', 'b' * 64, 'facts'), True),  # non-int target
    ((3, 'nothex', 'facts'), True),    # source not 64-hex
    ((3, 'b' * 64, 'bogus'), True),    # phase outside enum
    ((3, 'b' * 64), True),             # wrong arity
    ('not-a-tuple', True),             # not a tuple at all
])
def test_owns_member_request_identity_verdicts(cloud, identity, owned):
    """A malformed non-None identity owns the member outright — an unproven
    current request never releases local. A valid different-source identity
    does not let a verified different unit own the member."""
    led, cfg, job, calls = cloud
    _mixed_targets(led, job, [3, 2])
    _member_reservation(led, cfg, job, 2)
    _member_unit_reservation(led, cfg, job, 3, 'a' * 64)
    assert cloud_policy.owns_member_request(
        led.db, route='semantic_backlog', project_id=1, target_id=3,
        identity=identity) is owned
