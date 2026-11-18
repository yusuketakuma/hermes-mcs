"""Earlier-registry counts use stored evidence and real semantic classification (synthetic)."""
import json
import sqlite3
import time

import pytest

import semantic_drain
import semantic_jev
import semantic_policy
import semantic_store
import semantic_transport
from semantic_prior_registry import latest_post_assessment, prior_registry_remaining
from semantic_testkit import _FakeJev, _cfg, _ledger, _llm, _message, _patient


BUMPED = "2099-01-01"


@pytest.fixture
def store(tmp_path):
    ledger = _ledger(tmp_path)
    _patient(ledger)
    try:
        yield ledger
    finally:
        ledger.close()


def _counts(**values):
    return {**dict.fromkeys(("runnable", "held", "failed", "paused", "excluded", "unassessed"), 0), **values}


def _evaluated(store):
    cfg = _cfg(loop_mode="off")
    store.save_messages([_message(1)], project_id=1, semantic=True)
    for _ in range(3):
        semantic_drain.run_due(store, cfg, {"errors": []}, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=_llm)
    assert store.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic'").fetchone()[0] == "done"
    _, meta = latest_post_assessment(store.db, 1, 1)
    assert meta["registry"] == semantic_jev.REGISTRY_VERSION
    row = store.db.execute(
        "SELECT m.message_id,m.project_id,m.parent_id,m.body_state,m.body_text,"
        "p.project_id AS patient_id,p.fetch_reason FROM messages m "
        "LEFT JOIN patients p ON p.project_id=m.project_id WHERE m.message_id=1").fetchone()
    with semantic_transport.config_model_scope(cfg):
        assert semantic_store.reprocess_target_state(
            store, row, semantic_policy.runtime_config(cfg)[0], cfg=cfg) == "current"
    return cfg


def _source_changes(store):
    store.save_messages([_message(2, parent=1), _message(3)], project_id=1, semantic=True)
    store.save_messages([_message(1, body="合成の本文を変更しました。")], project_id=1, semantic=True)


def test_posts_without_an_assessment_are_not_a_prior_registry_cohort(store):
    cfg = _cfg(loop_mode="off")
    assert latest_post_assessment(store.db, 1, 1) == (None, None)
    store.save_messages([_message(1), _message(2)], project_id=1, semantic=True)
    assert prior_registry_remaining(store, cfg) == _counts()


def test_old_registry_is_runnable_with_patient_context_disabled(store, monkeypatch):
    cfg = _evaluated(store)
    cfg["extract"] = {"patient_context": {"enabled": False}}
    monkeypatch.setattr(semantic_jev, "REGISTRY_VERSION", BUMPED)
    assert prior_registry_remaining(store, cfg) == _counts(runnable=1)
    for _ in range(32):
        result = semantic_drain.reprocess_step(store, cfg)
        if result.get("phase") in ("waiting", "complete"):
            break
    else:
        pytest.fail("bounded synthetic reprocess did not settle")
    for _ in range(3):
        semantic_drain.run_due(store, cfg, {"errors": []}, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=_llm)
    assert prior_registry_remaining(store, cfg) == _counts()


def test_new_arrivals_and_edits_do_not_enlarge_the_old_registry_cohort(store, monkeypatch):
    cfg = _evaluated(store)
    monkeypatch.setattr(semantic_jev, "REGISTRY_VERSION", BUMPED)
    before = prior_registry_remaining(store, cfg)
    assert before == _counts(runnable=1)
    _source_changes(store)
    assert prior_registry_remaining(store, cfg) == before


def test_current_registry_stays_out_after_source_changes(store):
    cfg = _evaluated(store)
    assert prior_registry_remaining(store, cfg) == _counts()
    _source_changes(store)
    assert prior_registry_remaining(store, cfg) == _counts()


@pytest.mark.parametrize("fault,wanted", [
    ("failed", "failed"), ("paused", "paused"), ("scope", "excluded"),
    ("permission", "excluded"), ("deleted", "excluded"),
    ("empty", "excluded"), ("unavailable", "excluded"), ("held", "held"),
])
def test_counts_keep_existing_target_classifications(store, monkeypatch, fault, wanted):
    cfg = _evaluated(store)
    monkeypatch.setattr(semantic_jev, "REGISTRY_VERSION", BUMPED)
    if fault == "failed":
        with store.db:
            store.db.execute("UPDATE fetch_jobs SET state='failed',attempts=6 WHERE kind='semantic'")
    elif fault == "paused":
        store.artifact_add("semantic_control", "paused", project_id=1)
    elif fault == "scope":
        cfg["semantic"]["project_ids"] = [2]
    elif fault == "permission":
        with store.db:
            store.db.execute("UPDATE patients SET fetch_reason='permission_denied' WHERE project_id=1")
    elif fault == "deleted":
        with store.db:
            store.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
    elif fault == "empty":
        with store.db:
            store.db.execute("UPDATE messages SET body_state='full',body_text='' WHERE message_id=1")
    elif fault == "unavailable":
        with store.db:
            store.db.execute("UPDATE messages SET body_state='snippet' WHERE message_id=1")
    else:
        cfg["semantic"].update(summary_mode="off", loop_mode="off")
    assert prior_registry_remaining(store, cfg) == _counts(**{wanted: 1})


@pytest.mark.parametrize("raw", [None, '{"registry":null}', '{"registry":1}', "{", "[]"])
def test_unverifiable_latest_assessment_remains_visible_without_fallback(store, monkeypatch, raw):
    cfg = _evaluated(store)
    stored, meta = latest_post_assessment(store.db, 1, 1)
    original = store.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (stored["artifact_id"],)).fetchone()
    if raw is None:
        meta.pop("registry")
        raw = json.dumps(meta)
    aid = store.artifact_add(original["kind"], original["content"], project_id=1,
                             message_id=1, model=original["model"], meta=meta)
    with store.db:
        store.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (raw, aid))
    monkeypatch.setattr(semantic_jev, "REGISTRY_VERSION", BUMPED)
    latest, parsed = latest_post_assessment(store.db, 1, 1)
    assert latest["artifact_id"] == aid
    if raw in ("{", "[]"):
        assert parsed is None
    else:
        assert type(parsed.get("registry")) is not str
    assert prior_registry_remaining(store, cfg) == _counts(unassessed=1)
    store.save_messages([_message(2)], project_id=1, semantic=True)
    assert prior_registry_remaining(store, cfg) == _counts(unassessed=1)


def test_fact_level_assessments_do_not_replace_the_post_registry(store, monkeypatch):
    cfg = _evaluated(store)
    stored, meta = latest_post_assessment(store.db, 1, 1)
    post_aid = stored["artifact_id"]
    store.artifact_add("semantic_assess", "{}", project_id=1, message_id=1,
                       meta={**meta, "fact_id": "synthetic-fact"})
    assert latest_post_assessment(store.db, 1, 1)[0]["artifact_id"] == post_aid
    monkeypatch.setattr(semantic_jev, "REGISTRY_VERSION", BUMPED)
    assert prior_registry_remaining(store, cfg) == _counts(runnable=1)


def test_legitimate_off_keeps_old_cohort_diagnostics(store, monkeypatch):
    _evaluated(store)
    monkeypatch.setattr(semantic_jev, "REGISTRY_VERSION", BUMPED)
    assert prior_registry_remaining(store, _cfg("off")) == _counts(held=1)


def test_invalid_configuration_is_not_an_empty_count(store):
    cfg = _cfg()
    cfg["semantic"]["mode"] = "invalid"
    with pytest.raises(ValueError, match="^semantic_prior_registry_config_invalid$"):
        prior_registry_remaining(store, cfg)


def test_read_failure_does_not_publish_partial_or_zero_counts(store):
    cfg = _evaluated(store)

    def deny_artifacts(action, table, _column, _database, _source):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == "artifacts" else sqlite3.SQLITE_OK

    store.db.set_authorizer(deny_artifacts)
    try:
        with pytest.raises(ValueError, match="^semantic_prior_registry_unavailable$"):
            prior_registry_remaining(store, cfg)
    finally:
        store.db.set_authorizer(None)
    assert not store.db.in_transaction


def test_counting_does_not_write_or_commit_an_enclosing_transaction(store, monkeypatch):
    cfg = _evaluated(store)
    monkeypatch.setattr(semantic_jev, "REGISTRY_VERSION", BUMPED)
    before = store.db.execute("SELECT attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()[0]
    store.db.execute("UPDATE fetch_jobs SET attempts=2 WHERE kind='semantic'")
    changes = store.db.total_changes
    assert prior_registry_remaining(store, cfg) == _counts(runnable=1)
    assert store.db.in_transaction
    assert store.db.total_changes == changes
    assert store.db.execute("SELECT attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()[0] == 2
    store.db.rollback()
    assert store.db.execute("SELECT attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()[0] == before


def test_counting_keeps_the_supplied_model_configuration(store, monkeypatch):
    cfg = _evaluated(store)
    monkeypatch.setattr(semantic_jev, "REGISTRY_VERSION", BUMPED)
    original = semantic_transport.llm_conf
    seen = []

    def supplied_only(value=None):
        assert value is cfg
        seen.append(value)
        return original(value)

    monkeypatch.setattr(semantic_transport, "llm_conf", supplied_only)
    assert prior_registry_remaining(store, cfg) == _counts(runnable=1)
    assert seen == [cfg]


def test_non_cohort_posts_do_not_require_body_reads(store):
    cfg = _evaluated(store)
    store.save_messages([_message(2)], project_id=1, semantic=True)

    def deny_bodies(action, table, column, _database, _source):
        return (sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ
                and table == "messages" and column in ("body_text", "body_html") else sqlite3.SQLITE_OK)

    store.db.set_authorizer(deny_bodies)
    try:
        assert prior_registry_remaining(store, cfg) == _counts()
    finally:
        store.db.set_authorizer(None)
