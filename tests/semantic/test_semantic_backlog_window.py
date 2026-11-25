"""Synthetic semantic windows gate fresh sends without retiring paid inputs."""
import json
import time
from types import SimpleNamespace

import pytest

import cloud_policy
import semantic_candidate
import semantic_drain
import semantic_due_selection
import semantic_runtime
import semantic_transport
from semantic_cloud_testkit import cloud as cloud, direct, rows


def _open_window(cfg):
    cfg["cloud_llm"].update(
        provider="anthropic", semantic_backlog_until="2999-01-01T00:00:00+09:00",
        daily_cost_limit_usd=30, monthly_cost_limit_usd=300,
        provider_budgets={name: {"daily_cost_limit_usd": 15, "monthly_cost_limit_usd": 150}
                          for name in ("anthropic", "openai")})


@pytest.mark.parametrize("until", [None, "2999-01-01T00:00:00+09:00", "2000-01-01T00:00:00+09:00"])
def test_window_changes_do_not_change_received_input_generation(until):
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": "anthropic",
                         "semantic_backlog_until": None}}
    changed = {"cloud_llm": {**cfg["cloud_llm"], "semantic_backlog_until": until}}
    assert semantic_runtime.config_generation(changed) == semantic_runtime.config_generation(cfg)
    assert semantic_runtime.config_generation({"cloud_llm": {"semantic_backlog_until": until}}) \
        == semantic_runtime.config_generation({"cloud_llm": {"semantic_backlog_until": None}})
    assert semantic_runtime.config_generation({"cloud_llm": {"semantic_backlog_until": until}}) \
        != semantic_runtime.config_generation({})
    changed["cloud_llm"]["project_ids"] = [2]
    assert semantic_runtime.config_generation(changed) != semantic_runtime.config_generation(cfg)


def test_cloud_contract_is_stable_across_worker_context_and_window_expiry(monkeypatch):
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1]}}
    assert semantic_candidate._cloud_contract(cfg) is None
    assert not cloud_policy.semantic_planned(cloud_policy.cloud_config(cfg)[0])
    _open_window(cfg)
    clock = [cloud_policy.until_epoch("2998-12-31T23:59:59+09:00")]
    monkeypatch.setattr(cloud_policy, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(cloud_policy, "provider_key", lambda *a, **kw: "synthetic-key")
    monkeypatch.delenv(cloud_policy.BACKLOG_WORKER_ENV, raising=False)
    first = semantic_candidate._cloud_contract(cfg)
    assert first is not None
    assert not cloud_policy.route_open("semantic_backlog", cfg)
    monkeypatch.setenv(cloud_policy.BACKLOG_WORKER_ENV, "1")
    assert cloud_policy.route_open("semantic_backlog", cfg)
    assert semantic_candidate._cloud_contract(cfg) == first
    clock[0] += 1
    assert not cloud_policy.route_open("semantic_backlog", cfg)
    assert semantic_candidate._cloud_contract(cfg) == first
    cfg["cloud_llm"]["semantic_backlog_until"] = None
    assert not cloud_policy.route_open("semantic_backlog", cfg)
    assert semantic_candidate._cloud_contract(cfg) == first
    del cfg["cloud_llm"]["semantic_backlog_until"]
    assert semantic_candidate._cloud_contract(cfg) is None


def test_cloud_selection_requires_worker_and_live_window(cloud, monkeypatch):
    ledger, cfg, job, calls = cloud
    _open_window(cfg)

    def select():
        return semantic_due_selection._select_cloud_due(
            ledger, "backlog", ("semantic",), False, 1)[0]

    monkeypatch.delenv(cloud_policy.BACKLOG_WORKER_ENV, raising=False)
    assert select() == []
    monkeypatch.setenv(cloud_policy.BACKLOG_WORKER_ENV, "1")
    assert [row["job_id"] for row in select()] == [job["job_id"]]
    cfg["cloud_llm"]["semantic_backlog_until"] = "2000-01-01T00:00:00+09:00"
    assert select() == []
    assert calls == [] and not rows(ledger, "cloud_usage_reserve")


def test_selected_job_rechecks_window_before_any_model_work(cloud, monkeypatch):
    ledger, cfg, _job, calls = cloud
    _open_window(cfg)
    monkeypatch.setenv(cloud_policy.BACKLOG_WORKER_ENV, "1")
    original = semantic_due_selection._select_cloud_due
    selected = []

    def select_and_expire(*args, **kwargs):
        batch = original(*args, **kwargs)
        if batch[0] and not selected:
            selected.append(True)
            cfg["cloud_llm"]["semantic_backlog_until"] = "2000-01-01T00:00:00+09:00"
        return batch

    monkeypatch.setattr(semantic_drain, "_select_cloud_due", select_and_expire)
    monkeypatch.setattr(semantic_due_selection, "_select_cloud_due", select_and_expire)
    monkeypatch.setattr(semantic_drain, "_process_job",
                        lambda *a, **kw: pytest.fail("expired window started model work"))
    out = semantic_drain.run_due(ledger, cfg, {"errors": []}, time.monotonic() + 1200,
                                 lane="backlog", max_jobs=1, cloud_only=True)
    assert selected and out.get("cloud_selection_changed") is True
    assert calls == [] and not rows(ledger, "cloud_usage_reserve")


def test_expired_window_recovers_paid_answer_without_resend_or_local_switch(cloud, monkeypatch):
    ledger, cfg, job, calls = cloud
    _open_window(cfg)
    monkeypatch.setenv(cloud_policy.BACKLOG_WORKER_ENV, "1")
    prompt = "Synthetic semantic window facts JSON:"

    def forbidden_local(*args, **kwargs):
        pytest.fail("paid answer switched to local")

    def ask():
        with semantic_transport.inference_targets("synthetic-window-generation"):
            with semantic_transport.backlog_inference(
                    ledger, cfg, job, time.monotonic() + 120, lambda: True, direct,
                    local_fallback=forbidden_local) as chat:
                assert chat is not None
                semantic_transport.set_inference_target(job["message_id"])
                return json.loads(chat(prompt))

    assert ask() == {"facts": []}
    assert len(calls) == len(rows(ledger, "cloud_usage_reserve")) == 1
    before = [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
    cfg["cloud_llm"]["semantic_backlog_until"] = "2000-01-01T00:00:00+09:00"
    assert ask() == {"facts": []}
    assert len(calls) == 1
    assert [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == before


def test_nonworker_summary_stage_uses_cloud_while_facts_stay_local(cloud, monkeypatch):
    ledger, cfg, job, calls = cloud
    _open_window(cfg)
    monkeypatch.delenv(cloud_policy.BACKLOG_WORKER_ENV, raising=False)
    local_prompts = []
    summary_prompt = "Synthetic semantic window summary JSON:"

    def fallback(prompt, **kwargs):
        local_prompts.append(prompt)
        return '{"facts": []}'

    def ask(worker=False, include_facts=True):
        if worker:
            monkeypatch.setenv(cloud_policy.BACKLOG_WORKER_ENV, "1")
        else:
            monkeypatch.delenv(cloud_policy.BACKLOG_WORKER_ENV, raising=False)
        with semantic_transport.inference_targets("synthetic-window-generation"):
            with semantic_transport.backlog_inference(
                    ledger, cfg, job, time.monotonic() + 120, lambda: True, direct,
                    local_fallback=fallback) as chat:
                assert chat is not None
                if include_facts:
                    semantic_transport.set_inference_target(job["message_id"])
                    assert chat("Synthetic semantic window facts JSON:") == '{"facts": []}'
                semantic_transport.set_inference_target(job["message_id"], "summary")
                return chat(summary_prompt)

    assert json.loads(ask()) == {"facts": []}
    assert len(local_prompts) == len(calls) == len(rows(ledger, "cloud_usage_reserve")) == 1
    reserve = rows(ledger, "cloud_usage_reserve")[0]
    owner = json.loads(reserve["meta"])["owner"]
    assert owner["semantic_target_id"] == job["message_id"]
    assert owner["semantic_phase"] == "summary"

    assert json.loads(ask(worker=True, include_facts=False)) == {"facts": []}
    assert len(calls) == len(rows(ledger, "cloud_usage_reserve")) == 1


def test_summary_stage_keeps_recent_targets_out_of_cloud(cloud, monkeypatch):
    ledger, cfg, job, calls = cloud
    _open_window(cfg)
    monkeypatch.delenv(cloud_policy.BACKLOG_WORKER_ENV, raising=False)
    with ledger.db:
        ledger.db.execute("UPDATE messages SET first_seen=? WHERE message_id=?",
                          (time.time(), job["message_id"]))

    with semantic_transport.inference_targets("synthetic-window-generation"):
        with semantic_transport.backlog_inference(
                ledger, cfg, job, time.monotonic() + 120, lambda: True, direct,
                local_fallback=lambda prompt, **kwargs: '{"facts": []}') as chat:
            assert chat is None
    assert calls == [] and not rows(ledger, "cloud_usage_reserve")
