"""A proposition-registry bump re-queues completed semantic jobs once, whatever seeded them (synthetic).

Jobs are created the ways production creates them: a live arrival with a
notification event, a history import, an initial-history seed and the
reprocess pass. Only the last one writes an execution marker."""
import json
import time

import pytest

from inference_version import CURRENT_VERSION, VERSION_KEY
import semantic_drain as drain
import semantic_jev as jev
import semantic_policy
import semantic_runtime as runtime
import semantic_store
from semantic_testkit import _FakeJev, _cfg, _ledger, _llm, _message, _patient

BUMPED = "2099-01-01"
ORIGINS = ("live_arrival", "history_import", "initial_history", "reprocess_seed")


@pytest.fixture
def store(tmp_path):
    ledger = _ledger(tmp_path)
    _patient(ledger)
    try:
        yield ledger
    finally:
        ledger.close()


def _job(store):
    return store.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic' AND project_id=1 AND message_id=1").fetchone()


def _row(store):
    return store.db.execute(
        'SELECT m.message_id,m.project_id,m.parent_id,m.body_state,m.body_text,'
        'p.project_id AS patient_id,p.fetch_reason FROM messages m '
        'LEFT JOIN patients p ON p.project_id=m.project_id WHERE m.message_id=1').fetchone()


def _state(store, cfg):
    return semantic_store.reprocess_target_state(store, _row(store), semantic_policy.runtime_config(cfg)[0], cfg=cfg)


def _drain(store, cfg, rounds=3):
    for _ in range(rounds):
        drain.run_due(store, cfg, {"errors": []}, time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm)


def _settle(store, cfg):
    """Reprocess until the bounded pass rests on a waiting/complete bookmark."""
    for _ in range(32):
        report = drain.reprocess_step(store, cfg)
        if report.get("phase") in ("waiting", "complete"):
            return report
    pytest.fail("bounded synthetic reprocess did not settle")


def _evaluated(store, cfg, origin):
    """Create the job through ``origin`` and drain it until its results are current."""
    message = _message(1)
    if origin == "live_arrival":
        store.save_messages([message], project_id=1, semantic=True, notify={"source": "unread"})
    elif origin == "history_import":
        store.save_messages([message], project_id=1, semantic=True)
    elif origin == "initial_history":
        store.save_messages([message], project_id=1)
        store.semantic_seed(1, [1], {"source": "initial_history", "notification_free": True})
    else:
        store.save_messages([message], project_id=1)
        _settle(store, cfg)
    payload = json.loads(_job(store)["payload"])
    assert ("execution_contract" in payload) is (origin == "reprocess_seed")
    _drain(store, cfg)
    assert _job(store)["state"] == "done" and _state(store, cfg) == "current"


def _per_post_assessment(store):
    rows = store.db.execute("SELECT meta FROM artifacts WHERE kind='semantic_assess' AND message_id=1 "
                            "ORDER BY artifact_id DESC").fetchall()
    return next(meta for meta in map(lambda r: json.loads(r[0]), rows) if "fact_id" not in meta)


@pytest.mark.parametrize("origin", ORIGINS)
def test_registry_bump_reopens_a_completed_job_and_the_drain_makes_it_current(store, monkeypatch, origin):
    cfg = _cfg(loop_mode="off")
    scfg = semantic_policy.runtime_config(cfg)[0]
    _evaluated(store, cfg, origin)
    old_generation = runtime.execution_generation(scfg)
    old_fingerprint = semantic_store.thread_bundle(store.db, 1, 1, [1])["source_fingerprint"]
    monkeypatch.setattr(jev, "REGISTRY_VERSION", BUMPED)
    generation = runtime.execution_generation(scfg)
    assert generation != old_generation
    assert _state(store, cfg) == "repair_required"
    _settle(store, cfg)
    job = _job(store)
    payload = json.loads(job["payload"])
    assert job["state"] == "pending" and job["attempts"] == 0
    assert payload["execution_contract"] == 1 and payload["execution_generation"] == generation
    assert payload[VERSION_KEY] == CURRENT_VERSION
    _drain(store, cfg)
    fingerprint = semantic_store.thread_bundle(store.db, 1, 1, [1])["source_fingerprint"]
    assert fingerprint != old_fingerprint
    assert _job(store)["state"] == "done" and _state(store, cfg) == "current"
    summary = store.db.execute("SELECT meta FROM artifacts WHERE kind='semantic_summary' AND message_id=1 "
                               "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    assert json.loads(summary[0])["fingerprint"] == fingerprint
    assert _per_post_assessment(store)["registry"] == BUMPED


@pytest.mark.parametrize("bookmark", ["waiting", "complete"])
def test_a_resting_reprocess_bookmark_wakes_for_a_registry_bump(store, monkeypatch, bookmark):
    import patient_context_refresh
    import rollup
    cfg = _cfg(loop_mode="off")
    _evaluated(store, cfg, "live_arrival")
    if bookmark == "complete":
        monkeypatch.setattr(patient_context_refresh, "inference_coverage",
                            lambda *args: {VERSION_KEY: CURRENT_VERSION, "complete": True})
        rollup.rebuild(store, 1, cfg=cfg)
    assert _settle(store, cfg)["phase"] == bookmark
    before = dict(_job(store))
    assert dict(_job(store)) == before and before["state"] == "done"
    monkeypatch.setattr(jev, "REGISTRY_VERSION", BUMPED)
    _settle(store, cfg)
    job = _job(store)
    assert job["state"] == "pending" and job["attempts"] == 0
    assert json.loads(job["payload"])["execution_generation"] == runtime.execution_generation(
        semantic_policy.runtime_config(cfg)[0])


def test_adoption_is_one_time_and_keeps_the_retry_budget(store, monkeypatch):
    """A job that finishes again without current results under the same generation stays done."""
    cfg = _cfg(loop_mode="off")
    _evaluated(store, cfg, "live_arrival")
    monkeypatch.setattr(jev, "REGISTRY_VERSION", BUMPED)
    _settle(store, cfg)
    assert _job(store)["state"] == "pending"
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET attempts=2 WHERE kind='semantic'")
    assert runtime.transition(store, runtime.JobToken.from_row(_job(store)), "done")
    before = dict(_job(store))
    assert _state(store, cfg) == "repair_required"
    for _ in range(12):
        report = drain.reprocess_step(store, cfg)
    assert dict(_job(store)) == before
    assert report["waiting_reasons"]["messages"] == {"repair_required": 1}


def test_a_current_marker_less_job_is_left_alone(store):
    cfg = _cfg(loop_mode="off")
    _evaluated(store, cfg, "live_arrival")
    before = dict(_job(store))
    for _ in range(12):
        drain.reprocess_step(store, cfg)
    assert dict(_job(store)) == before


def test_an_exhausted_marker_less_job_is_not_revived_by_a_bump(store, monkeypatch):
    """Only an explicit human retry extends a failed job without an execution marker."""
    cfg = _cfg(loop_mode="off")
    _evaluated(store, cfg, "live_arrival")
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET state='failed',attempts=6 WHERE kind='semantic'")
    before = dict(_job(store))
    monkeypatch.setattr(jev, "REGISTRY_VERSION", BUMPED)
    assert _state(store, cfg) == "failed"
    for _ in range(12):
        drain.reprocess_step(store, cfg)
    assert dict(_job(store)) == before


def test_execution_generation_names_the_registry_but_not_local_labels(monkeypatch):
    base = semantic_policy.runtime_config(_cfg("assist"))[0]
    extra = semantic_policy.runtime_config(_cfg("assist", extra_propositions=["E01"]))[0]
    before = {name: runtime.execution_generation(scfg) for name, scfg in (("base", base), ("extra", extra))}
    loop = runtime.interpretation_generation(base)
    relabelled = {pid: {**p, "label_ja": p["label_ja"] + "（合成）"} for pid, p in jev.PROPOSITIONS.items()}
    monkeypatch.setattr(jev, "PROPOSITIONS", relabelled)
    assert runtime.execution_generation(base) == before["base"]
    assert runtime.execution_generation(extra) == before["extra"]
    optional = {pid: dict(p) for pid, p in jev.OPTIONAL_PROPOSITIONS.items()}
    optional["E01"]["label_ja"] += "（合成）"
    monkeypatch.setattr(jev, "OPTIONAL_PROPOSITIONS", optional)
    # policy_fingerprint hashes the enabled optional propositions whole
    assert runtime.execution_generation(base) == before["base"]
    assert runtime.execution_generation(extra) != before["extra"]
    monkeypatch.setattr(jev, "REGISTRY_VERSION", BUMPED)
    assert runtime.execution_generation(base) != before["base"]
    assert runtime.interpretation_generation(base) != loop
