"""Rollout stages require independent, explicit feature configuration."""
import time

import semantic
from semantic_testkit import _cfg, _FakeJev, _llm, _seeded


def test_classification_only_does_not_generate_summary_or_loops(tmp_path):
    db = _seeded(tmp_path)
    calls = []
    try:
        cfg = {"semantic": {"mode": "shadow", "daily_request_budget": 50, "project_ids": [1]}}
        out = semantic.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                              jev_client=_FakeJev(), llm_fn=lambda p: calls.append(p))
        assert out["done"] == 1
        assert db.artifacts("semantic_assess", message_id=1)
        assert not calls
        assert not db.artifacts("semantic_summary", message_id=1)
        assert not db.artifacts("loop_candidate", project_id=1)
        db.semantic_seed(1, [1], {"source": "replay"})
        semantic.run_due(db, _cfg("shadow", loop_mode="off"), {"errors": []},
                         time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm)
        assert db.artifacts("semantic_summary", message_id=1)
        assert not db.artifacts("loop_candidate", project_id=1)
        assert not db.db.execute("SELECT 1 FROM notify_outbox WHERE kind='semantic_notice'").fetchone()
    finally:
        db.close()


def test_enforcement_requires_explicit_calibration_and_feature_flags():
    cfg, errors = semantic.semantic_config({"semantic": {"mode": "enforce"}})
    assert cfg["mode"] == "off" and "config: semantic_calibration_required" in errors
    cfg, errors = semantic.semantic_config(_cfg("enforce", summary_mode="off", loop_mode="off"))
    assert not errors and cfg["mode"] == "enforce"
    assert cfg["summary_mode"] == cfg["loop_mode"] == "off"
    cfg, errors = semantic.semantic_config(_cfg("shadow", max_attempts_per_try=4))
    assert cfg["mode"] == "off"
    assert "config: semantic_max_attempts_per_try_invalid" in errors
    cfg, errors = semantic.semantic_config({"semantic": {"mode": "shadow", "daily_request_budget": 50}})
    assert cfg["mode"] == "off" and cfg["project_ids"] == []
    assert "config: semantic_project_scope_required" in errors


def test_shadow_summary_is_reevaluated_before_enforce_publication(tmp_path):
    import json
    db = _seeded(tmp_path)
    try:
        semantic.run_due(db, _cfg("shadow"), {"errors": []}, time.monotonic() + 300,
                         jev_client=_FakeJev(), llm_fn=_llm)
        old = db.artifacts("semantic_summary", message_id=1)[-1]
        assert json.loads(old["meta"])["publication_mode"] == "shadow"
        db.semantic_seed(1, [1], {"source": "replay"})
        calls = []
        def llm(prompt):
            calls.append(prompt)
            return _llm(prompt)
        semantic.run_due(db, _cfg("enforce"), {"errors": []}, time.monotonic() + 300,
                         jev_client=_FakeJev(), llm_fn=llm)
        current = db.artifacts("semantic_summary", message_id=1)[-1]
        assert calls and current["artifact_id"] != old["artifact_id"]
        assert json.loads(current["meta"])["publication_mode"] == "enforce"
    finally:
        db.close()
