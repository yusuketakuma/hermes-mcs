"""prior_registry_remaining (N_sem) counts pre-deploy posts by reprocess state (synthetic)."""
import json

import pytest

from inference_version import CURRENT_VERSION, VERSION_KEY
import semantic_jev as jev
import semantic_policy
from semantic_prior_registry import prior_registry_remaining
import semantic_store
from semantic_testkit import _cfg, _ledger, _message, _patient

OLD = "2000-01-01"


@pytest.fixture
def store(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        yield ledger
    finally:
        ledger.close()


def _assess(db, mid, registry, pid=1, meta=None):
    db.artifact_add(
        "semantic_assess",
        json.dumps({"verdicts": {}, "target_message_id": mid}),
        project_id=pid, message_id=mid, model=jev.JEV_MODEL,
        meta={"registry": registry, **(meta or {})})


def _done_job(db, pid, root, state="done"):
    db.db.execute(
        "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,"
        "next_try,created_at,updated_at) VALUES('semantic',?,?,?,?,0,0,0)",
        (pid, root, json.dumps({VERSION_KEY: CURRENT_VERSION}), state))
    db.db.commit()


def _held(db, mid, pid, scfg):
    """Current summary + NEEDS_REVIEW audit + done current job -> state 'held'."""
    fp = semantic_store.thread_bundle(db.db, pid, mid)["source_fingerprint"]
    policy = semantic_policy.policy_fingerprint(scfg)
    common = {VERSION_KEY: CURRENT_VERSION, "fingerprint": fp,
              "policy_fingerprint": policy}
    db.artifact_add("semantic_summary", json.dumps({"claims": [], "limitations": []}),
                    project_id=pid, message_id=mid, meta=common)
    db.artifact_add("semantic_audit", json.dumps({"status": "NEEDS_REVIEW", "findings": []}),
                    project_id=pid, message_id=mid, meta=common)
    _done_job(db, pid, mid)


def test_posts_are_counted_by_reprocess_state(store):
    cfg = _cfg()
    scfg = semantic_policy.runtime_config(cfg)[0]
    for pid in (1, 2, 3, 4, 5):
        _patient(store, pid)
    for mid, pid in ((1, 1), (2, 1), (3, 1), (4, 2), (5, 3), (6, 4),
                     (7, 1), (8, 1), (9, 1), (10, 5), (11, 99), (12, 1)):
        store.save_messages([_message(mid, pid=pid)], project_id=pid)
    # runnable: old-registry assessment, nothing else
    _assess(store, 1, OLD)
    # not counted: current-registry assessment
    _assess(store, 2, jev.REGISTRY_VERSION)
    # unassessed: m3 has no assessment; m12's only rows are fact details
    _assess(store, 12, OLD, meta={"fact_id": "f1"})
    # paused
    store.artifact_add("semantic_control", "paused", project_id=2)
    _assess(store, 4, OLD, pid=2)
    # failed
    _assess(store, 5, OLD, pid=3)
    _done_job(store, 3, 5, state="failed")
    # held
    _assess(store, 6, OLD, pid=4)
    _held(store, 6, 4, scfg)
    # excluded: deleted / empty_body / body_unavailable / permission / scope
    for mid, body_state, body_text in ((7, "deleted", "x"), (8, "full", ""),
                                       (9, "unavailable", "x")):
        store.db.execute("UPDATE messages SET body_state=?,body_text=? WHERE message_id=?",
                         (body_state, body_text, mid))
        _assess(store, mid, OLD)
    store.db.execute("UPDATE patients SET fetch_reason='forbidden' WHERE project_id=5")
    _assess(store, 10, OLD, pid=5)
    _assess(store, 11, OLD, pid=99)
    store.db.commit()
    assert prior_registry_remaining(store, cfg) == {
        "runnable": 1, "held": 1, "failed": 1, "paused": 1,
        "excluded": 5, "unassessed": 2}


def test_latest_assessment_decides(store):
    _patient(store)
    store.save_messages([_message(1)], project_id=1)
    _assess(store, 1, OLD)
    _assess(store, 1, jev.REGISTRY_VERSION)
    assert prior_registry_remaining(store, _cfg()) == {
        "runnable": 0, "held": 0, "failed": 0, "paused": 0,
        "excluded": 0, "unassessed": 0}


def test_unreadable_assessment_meta_is_unknown_evidence(store):
    _patient(store)
    store.save_messages([_message(1)], project_id=1)
    store.artifact_add("semantic_assess", "{}", project_id=1, message_id=1,
                       meta=None)
    store.db.execute("UPDATE artifacts SET meta='not json'")
    store.db.commit()
    assert prior_registry_remaining(store, _cfg())["held"] == 1


def test_new_arrivals_and_body_edits_do_not_grow_runnable(store):
    cfg = _cfg()
    _patient(store)
    store.save_messages([_message(1)], project_id=1)
    _assess(store, 1, OLD)
    before = prior_registry_remaining(store, cfg)
    assert before["runnable"] == 1 and before["unassessed"] == 0
    store.save_messages([_message(2, pid=1, parent=1)], project_id=1)
    store.db.execute("UPDATE messages SET body_text='別の合成本文',content_hash='edited' "
                     "WHERE message_id=1")
    store.db.commit()
    after = prior_registry_remaining(store, cfg)
    assert after["runnable"] == before["runnable"]
    assert after["unassessed"] == before["unassessed"] + 1


def test_patient_context_disabled_still_counts(store):
    cfg = _cfg()
    _patient(store)
    store.save_messages([_message(1)], project_id=1)
    _assess(store, 1, OLD)
    expected = prior_registry_remaining(store, cfg)
    disabled = {**cfg, "extract": {"patient_context": {"enabled": False}}}
    assert prior_registry_remaining(store, disabled) == expected
    assert expected["runnable"] == 1
