"""Automatic provider selection preserves explicit rollout and durable request caps."""
import json
import time

import pytest

import run_check
import semantic
import semantic_drain
import semantic_jev as jev
import semantic_policy
import semantic_runtime as runtime
import semantic_store
from semantic_testkit import _FakeJev, _seeded


@pytest.mark.parametrize("mode", ["off", "shadow", "assist"])
def test_explicit_mode_without_budget_keeps_zero_requests(mode):
    resolved, errors = semantic_policy.semantic_config(
        {"semantic": {"mode": mode, "project_ids": [1]}}, key_present=True)
    assert not errors
    assert resolved["mode"] == mode
    assert resolved["daily_request_budget"] == 0


def test_key_auto_runs_real_assessment_without_promoting_notifications(tmp_path, monkeypatch):
    monkeypatch.setattr(jev, "api_key", lambda root=None: "synthetic-unused-key")
    db = _seeded(tmp_path)
    client = _FakeJev()
    try:
        result = {"errors": []}
        assert run_check._semantic_enabled(db, {}, result)
        out = semantic_drain.run_due(
            db, {}, result, time.monotonic() + 300, jev_client=client,
            llm_fn=lambda *_: pytest.fail("disabled summary must not call LLM"))
        assert out["mode"] == "assist" and out["done"] == 1
        assert client.requests_made > 0
        assert db.artifacts("semantic_assess", message_id=1)
        assert not db.artifacts("semantic_summary", message_id=1)
        assert not db.db.execute("SELECT 1 FROM notify_outbox WHERE kind='semantic_notice'").fetchone()
        assert run_check._semantic_skipped({}, "background_worker")["mode"] == "assist"
    finally:
        db.close()


@pytest.mark.parametrize("block", [{"mode": "off"}, {"project_ids": []}])
def test_explicit_disable_or_empty_scope_prevents_auto_replay(tmp_path, monkeypatch, block):
    monkeypatch.setattr(jev, "api_key", lambda root=None: "synthetic-unused-key")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"semantic": block}))
    db = _seeded(tmp_path)
    try:
        before = db.db.execute("SELECT COUNT(*) FROM fetch_jobs").fetchone()[0]
        assert semantic.seed(db, 1, cfg_path=str(path)) is None
        assert db.db.execute("SELECT COUNT(*) FROM fetch_jobs").fetchone()[0] == before
    finally:
        db.close()


def test_removed_key_closes_auto_mode_at_side_effect_guard(tmp_path, monkeypatch):
    key = ["synthetic-unused-key"]
    monkeypatch.setattr(jev, "api_key", lambda root=None: key[0])
    db = _seeded(tmp_path)
    try:
        row = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
        args = dict(deadline=time.monotonic() + 30, expected_config_generation=None,
                    expected_mode="assist", cfg_path="synthetic-config.json",
                    load_cfg=lambda _: {}, parse_cfg=semantic_policy.runtime_config)
        runtime.guard(db, runtime.JobToken.from_row(row), **args)
        key[0] = None
        with pytest.raises(runtime.RuntimeOff):
            runtime.guard(db, runtime.JobToken.from_row(row), **args)
    finally:
        db.close()


def test_retry_cannot_exceed_hard_daily_500_cap(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    posts = []
    monkeypatch.setattr(jev.time, "sleep", lambda _: None)
    try:
        db.artifact_add("semantic_usage", "{}", model=jev.JEV_MODEL,
                        meta={"jev_requests": 499})
        row = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
        reserve = runtime.usage_reserver(
            db, runtime.JobToken.from_row(row), kind="semantic_usage", model=jev.JEV_MODEL,
            project_id=1, message_id=1, daily_request_budget=9999)
        def post(body, timeout):
            posts.append(body)
            return 429, {"Retry-After": "0"}, b"{}"
        client = jev.JevClient(api_key="synthetic-unused-key", post_fn=post)
        question = {"q": jev.noul_question("Synthetic", "yes", "no")}
        with pytest.raises(jev.JevError) as failure:
            runtime.bind_jev(client, lambda _: None, reserve).evaluate(
                {"target": {"text": "synthetic"}}, question, time.monotonic() + 30)
        assert failure.value.kind == "budget_exceeded"
        assert len(posts) == 1
        assert semantic_store.jev_usage_today(db) == 500
    finally:
        db.close()
