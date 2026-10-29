"""Synthetic backlog routing, accounting and run-lock loss regressions."""
import json
import time

import pytest

import anthropic_client
import cloud_policy
import extract_llm
import semantic_drain
import semantic_transport as transport
import writer_lock
from semantic_testkit import _ledger, _message, _patient, _cfg


def snapshot(led):
    with led.db:
        led.db.execute("UPDATE messages SET first_seen=?", (time.time() - 601,))
        led.db.execute("UPDATE message_revisions SET observed_at=?", (time.time() - 601,))
        cloud_policy.capture_history_tx(led.db, "synthetic-resume", time.time())


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


def test_local_only_feedback_is_filtered_before_the_cloud_selection_limit(cloud, monkeypatch):
    led, cfg, _, _ = cloud
    led.save_messages([_message(mid=2, body="完全合成の過去記録。")])
    snapshot(led)
    monkeypatch.setattr(extract_llm, "_qc_flagged_sql", lambda *a, **kw: "SELECT 1 WHERE m.message_id=1")
    selected, _ = extract_llm._select_pending(led, 1, None, None, False,
        cloud_only=True, prioritize_patients=True)
    assert [row["message_id"] for row in selected] == [2]


@pytest.mark.parametrize("denial", ["arrival", "off", "cap", "scope", "route"])
def test_local_boundaries_do_not_send(cloud, denial):
    led, cfg, job, calls = cloud
    if denial == "arrival":
        job["payload"] = json.dumps({**json.loads(job["payload"]), "eligible": True})
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
    assert counts["semantic_backlog"]["targets"] == 0


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


@pytest.mark.parametrize("first", ["local", "cloud"])
def test_cached_chunks_keep_their_actual_backend_without_duplicate_paid_calls(cloud, monkeypatch, first):
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

    def use(backend):
        if backend == "local":
            return extract(_llm_v2)
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
        assemble(_llm_v2)
    assert len(calls) == count
    assert [dict(row) for row in rows(led, semantic_extraction.KIND_CHUNK_V2)] == before
    expected = transport.llm_model() if first == "local" else anthropic_client.MODEL
    assert json.loads(before[0]["meta"])["actual_model"] == expected
    assembled = rows(led, semantic_drain.KIND_FACTS_V2)
    assert assembled and assembled[-1]["model"] == transport.llm_model()


def test_resident_history_turn_selects_cloud_batch_without_expanding_realtime(monkeypatch):
    now = [0.0]
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "max_concurrency": 4}}
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(extract_llm.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(extract_llm, "load_config", lambda: cfg)
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
    assert all(r["limit"] == 1 and not r["allow_cloud"] for r in seen if not r["oldest_first"])
