"""Archived work follows active work without acquiring notification eligibility."""
import semantic_due_selection
import semantic_policy
from contextlib import contextmanager
import json
import time

import pytest

import extract
import extract_llm
import semantic_drain
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY
from ledger import Ledger
from semantic_testkit import BODY, _cfg, _FakeJev, _llm, _patient


@pytest.fixture
def led(tmp_path):
    store = _ledger(tmp_path)
    store.ensure_patient(1)
    store.ensure_patient(2)
    with store.db:
        store.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    try:
        yield store
    finally:
        store.close()


def test_archived_save_seeds_only_new_and_changed_input_without_notifying(led):
    old = _message(mid=9, project_id=1, body="合成の既存履歴")
    led.save_messages([old], project_id=1)
    current = _message(mid=10, project_id=1, body="合成の退所後の連絡", unread=True)
    led.save_messages([current], project_id=1, semantic=True,
                      notify={"source": "unread"})
    job = led.job_pending("semantic", 1, 10)
    assert job is not None
    payload = json.loads(job["payload"])
    assert payload["targets"] == [10] and not payload.get("eligible")
    assert payload["origin"].get("event_id") is None
    assert led.job_pending("semantic", 1, 9) is None
    led.save_messages([current], project_id=1, semantic=True,
                      notify={"source": "unread"})
    assert dict(led.job_pending("semantic", 1, 10)) == dict(job)
    changed = _message(mid=10, project_id=1, body="合成の変更後の連絡", unread=True)
    led.save_messages([changed], project_id=1, semantic=True,
                      notify={"source": "unread"})
    updated = json.loads(led.job_pending("semantic", 1, 10)["payload"])
    assert updated["generation"] != payload["generation"]
    assert updated["source_generation"] != payload["source_generation"]
    assert not updated.get("eligible") and updated["origin"].get("event_id") is None
    assert led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 0
    assert led.db.execute("SELECT notified_at FROM messages WHERE message_id=10").fetchone()[0] is None
    assert led.job_pending("semantic", 1, 9) is None


def test_archived_change_suppresses_a_previously_eligible_generation(led):
    led.save_messages([_message(mid=20, project_id=2, body="合成の在宅の連絡", unread=True)],
                      project_id=2, semantic=True, notify={"source": "unread"})
    before = json.loads(led.job_pending("semantic", 2, 20)["payload"])
    notices = [dict(r) for r in led.db.execute("SELECT * FROM notify_outbox")]
    with led.db:
        led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    led.save_messages([_message(mid=20, project_id=2, body="合成の退所後に変更", unread=True)],
                      project_id=2, semantic=True, notify={"source": "unread"})
    after = json.loads(led.job_pending("semantic", 2, 20)["payload"])
    assert after["source_generation"] != before["source_generation"]
    assert after["notification_free"] is True
    assert [dict(r) for r in led.db.execute("SELECT * FROM notify_outbox")] == notices


def test_rule_extraction_persists_active_posts_before_newer_archived_posts(led):
    for mid, pid, day in ((10, 1, 20), (20, 2, 19)):
        led.save_messages([_message(mid=mid, project_id=pid, body="合成記録、体温37度",
                                    posted_at=f"2026-09-{day}T00:00:00+09:00")], project_id=pid)
    assert extract.run_pending(led) == {"done": 2, "pids": [1, 2]}
    assert [r[0] for r in led.db.execute(
        "SELECT message_id FROM artifacts WHERE kind=? ORDER BY artifact_id", (extract.KIND,))
    ] == [20, 10]


@pytest.mark.parametrize("oldest_first", [False, True], ids=["newest", "oldest-backlog"])
def test_llm_selection_finishes_active_fresh_and_reextraction_before_archive(led, oldest_first):
    for mid, pid, day in ((10, 1, 21), (11, 1, 16), (20, 2, 18), (21, 2, 19)):
        led.save_messages([_message(mid=mid, project_id=pid, body="合成記録、体温37度",
                                    posted_at=f"2026-09-{day}T00:00:00+09:00")], project_id=pid)
    for mid, pid in ((11, 1), (20, 2)):
        led.artifact_add(extract_llm.KIND, "{}", project_id=pid, message_id=mid,
                         meta={"hash": _hash(led, mid),
                               "extract_version": extract_llm.EXTRACT_VERSION - 1})
    expected = [20, 21, 11, 10] if oldest_first else [21, 20, 10, 11]
    for limit in (1, 2, 4):
        rows, fresh = extract_llm._select_pending(led, limit, None, None, oldest_first)
        assert [r["message_id"] for r in rows] == expected[:limit]
        assert fresh == (None if oldest_first else sum(mid in (10, 21) for mid in expected[:limit]))


@pytest.mark.parametrize("lane", [None, "backlog", "realtime"], ids=["fair", "backlog", "realtime"])
@pytest.mark.parametrize("max_jobs", [1, 4])
def test_semantic_selection_keeps_archive_after_active_across_cohorts(led, lane, max_jobs):
    # Older archived semantic/QC jobs, including a once-live arrival, precede the active jobs.
    for mid, pid, kind, eligible in (
            (10, 1, semantic_drain.QC_JOB_KIND, False),
            (11, 1, semantic_drain.JOB_KIND, True),
            (20, 2, semantic_drain.JOB_KIND, False),
            (21, 2, semantic_drain.JOB_KIND, True),
            (22, 2, semantic_drain.QC_JOB_KIND, False)):
        led.job_add(kind, pid, message_id=mid,
                    payload={VERSION_KEY: CURRENT_VERSION, "targets": [mid], "eligible": eligible,
                             "generation": f"synthetic-{mid}"})
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET next_try=1,created_at=1,updated_at=1 WHERE project_id=2")
        led.db.execute("UPDATE fetch_jobs SET next_try=2,created_at=2,updated_at=2 WHERE project_id=1")
    due, *_ = semantic_drain._select_due(
        led, lane, (semantic_drain.JOB_KIND, semantic_drain.QC_JOB_KIND), True, max_jobs)
    pids = [r["project_id"] for r in due]
    assert pids and pids[0] == 2
    assert pids == sorted(pids, key=lambda pid: pid == 1)
    assert {r["message_id"] for r in due if r["project_id"] == 2} >= ({21} if lane == "realtime" else set())


def test_archived_arrival_cannot_hide_the_only_active_backfill_slot(led):
    led.job_add(semantic_drain.JOB_KIND, 1, message_id=10,
                payload={VERSION_KEY: CURRENT_VERSION, "eligible": True, "generation": "synthetic-archived"})
    led.job_add(semantic_drain.JOB_KIND, 2, message_id=20,
                payload={VERSION_KEY: CURRENT_VERSION, "generation": "synthetic-active"})
    due, *_ = semantic_drain._select_due(led, None, (semantic_drain.JOB_KIND,), False, 1)
    assert [r["project_id"] for r in due] == [2]


@pytest.mark.parametrize("lane", [None, "backlog"], ids=["fair", "backlog"])
def test_semantic_archived_work_remains_runnable_after_active_work_finishes(led, lane):
    led.job_add(semantic_drain.JOB_KIND, 1, message_id=10,
                payload={VERSION_KEY: CURRENT_VERSION, "generation": "synthetic-archived"})
    led.job_add(semantic_drain.JOB_KIND, 2, message_id=20,
                payload={VERSION_KEY: CURRENT_VERSION, "generation": "synthetic-active"})
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET state='done' WHERE project_id=2")
    due, *_ = semantic_drain._select_due(led, lane, (semantic_drain.JOB_KIND,), False, 1)
    assert [r["message_id"] for r in due] == [10]


def test_backlog_retains_overscan_for_each_active_kind(led, monkeypatch):
    monkeypatch.setattr(semantic_drain, "_BACKLOG_TURN", {"kind": semantic_drain.JOB_KIND})
    monkeypatch.setattr(semantic_due_selection, '_BACKLOG_TURN', semantic_drain._BACKLOG_TURN)
    max_jobs = 1
    for mid in range(100, 100 + max_jobs + 8):
        led.job_add(semantic_drain.QC_JOB_KIND, 2, message_id=mid,
                    payload={VERSION_KEY: CURRENT_VERSION})
    led.job_add(semantic_drain.JOB_KIND, 2, message_id=20, payload={VERSION_KEY: CURRENT_VERSION})
    led.job_add(semantic_drain.QC_JOB_KIND, 1, message_id=10, payload={VERSION_KEY: CURRENT_VERSION})
    due, qc, *_ = semantic_drain._select_due(
        led, "backlog", (semantic_drain.JOB_KIND, semantic_drain.QC_JOB_KIND), True, max_jobs)
    assert len(qc) == max_jobs + 8
    assert len(due) == max_jobs + 9
    assert due[-1]["message_id"] == 20
    assert {r["project_id"] for r in due} == {2}


def test_archived_single_slot_turn_survives_empty_active_pass_and_reopen(led):
    led.job_add(semantic_drain.JOB_KIND, 1, message_id=10,
                payload={VERSION_KEY: CURRENT_VERSION, "eligible": True, "generation": "synthetic-arrival"})
    led.job_add(semantic_drain.JOB_KIND, 1, message_id=11,
                payload={VERSION_KEY: CURRENT_VERSION, "generation": "synthetic-backfill"})
    path = led.db.execute("PRAGMA database_list").fetchone()[2]
    store = led
    try:
        for expected in ("arrival", "backfill", "arrival"):
            due, _qc, arrivals, backfill, mutation = semantic_drain._select_due(
                store, None, (semantic_drain.JOB_KIND,), False, 1)
            assert [r["message_id"] for r in due] == [10 if expected == "arrival" else 11]
            assert (len(arrivals), len(backfill)) == ((1, 0) if expected == "arrival" else (0, 1))
            assert mutation["turn"] == expected
            state = semantic_drain._sched_state(store)
            state[mutation["turn_key"]] = mutation["turn"]
            semantic_drain._sched_write(store, state)
            store.close()
            store = Ledger(path)
            assert semantic_drain._sched_state(store)["archived_turn"] == expected
            assert "turn" not in semantic_drain._sched_state(store)
    finally:
        store.close()


@pytest.mark.parametrize("first_pid, second_pid", [(1, 2), (2, 1)])
def test_patient_classes_keep_independent_turns_after_real_drain_and_reopen(
        led, first_pid, second_pid):
    def seed_pair(store, pid, mid):
        _patient(store, pid)
        with store.db:
            store.db.execute("UPDATE patients SET is_archived=0 WHERE project_id=?", (pid,))
        store.save_messages([_message(mid=mid, project_id=pid, body=BODY, unread=True)],
                            project_id=pid, semantic=True, notify={"source": "unread"})
        store.save_messages([_message(mid=mid + 1, project_id=pid, body=BODY)],
                            project_id=pid, semantic=True)
        if pid == 1:
            with store.db:
                store.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")

    def drain(store):
        result = {"errors": []}
        out = semantic_drain.run_due(store, _cfg("shadow", budget=100000), result,
                               time.monotonic() + 300, jev_client=_FakeJev(),
                               llm_fn=_llm, max_jobs=1)
        assert out["done"] == 1 and not out["failed"] and not result["errors"]
        return out["lanes"]

    seed_pair(led, first_pid, 30)
    assert drain(led) == {"arrival": 1, "backfill": 0}
    assert drain(led) == {"arrival": 0, "backfill": 1}
    path = led.db.execute("PRAGMA database_list").fetchone()[2]
    led.close()
    store = Ledger(path)
    try:
        seed_pair(store, second_pid, 40)
        assert drain(store) == {"arrival": 1, "backfill": 0}
        sched = semantic_drain._sched_state(store)
        assert sched["turn"] == sched["archived_turn"] == "arrival"
    finally:
        store.close()


def test_realtime_long_defer_fills_from_archive_until_active_generation_changes(led):
    led.save_messages([_message(mid=20, project_id=2, body="合成の変更前", unread=True)],
                      project_id=2, semantic=True, notify={"source": "unread"})
    active = led.job_pending(semantic_drain.JOB_KIND, 2, 20)
    led.job_add(semantic_drain.JOB_KIND, 1, message_id=10,
                payload={VERSION_KEY: CURRENT_VERSION, "eligible": True, "generation": "synthetic-archived"})
    deferred = {str(active["job_id"]): {"g": json.loads(active["payload"])["generation"], "n": 1}}
    due, *_ = semantic_drain._select_due(
        led, "realtime", (semantic_drain.JOB_KIND,), False, 1, deferred)
    assert [r["message_id"] for r in due] == [10]
    led.save_messages([_message(mid=20, project_id=2, body="合成の変更後", unread=True)],
                      project_id=2, semantic=True, notify={"source": "unread"})
    due, *_ = semantic_drain._select_due(
        led, "realtime", (semantic_drain.JOB_KIND,), False, 1, deferred)
    assert [r["message_id"] for r in due] == [20]


def test_archived_new_and_changed_posts_finish_enforce_analysis_without_a_notice(led):
    _patient(led, 2)
    led.save_messages([_message(mid=20, project_id=2, body=BODY, unread=True)],
                      project_id=2, semantic=True, notify={"source": "unread"})
    before = json.loads(led.job_pending(semantic_drain.JOB_KIND, 2, 20)["payload"])
    assert before["eligible"] and before["origin"]["event_id"] is not None
    outbox = [dict(r) for r in led.db.execute("SELECT * FROM notify_outbox")]
    with led.db:
        led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    led.save_messages([
        _message(mid=20, project_id=2, body=BODY + "合成の追記。", unread=True),
        _message(mid=21, project_id=2, body=BODY, unread=True),
    ], project_id=2, semantic=True, notify={"source": "unread"})
    changed = json.loads(led.job_pending(semantic_drain.JOB_KIND, 2, 20)["payload"])
    fresh = json.loads(led.job_pending(semantic_drain.JOB_KIND, 2, 21)["payload"])
    assert changed["eligible"] and changed["origin"] == before["origin"]
    assert changed["generation"] != before["generation"]
    assert changed["notification_free"] is fresh["notification_free"] is True
    assert not fresh.get("eligible") and fresh["origin"].get("event_id") is None
    result = {"errors": []}
    out = semantic_drain.run_due(led, _cfg("enforce", budget=100000), result,
                           time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 2 and not out["failed"] and not result["errors"]
    for mid in (20, 21):
        assert led.artifacts(semantic_policy.KIND_SUMMARY, message_id=mid)
        plan = json.loads(led.artifacts(semantic_policy.KIND_PLAN, message_id=mid)[-1]["meta"])
        assert plan["audit_status"] == "PASS" and plan["publication_mode"] == "enforce"
        assert not plan.get("enqueued")
    assert not led.db.execute("SELECT 1 FROM notify_outbox WHERE kind='semantic_notice'").fetchone()
    assert [dict(r) for r in led.db.execute("SELECT * FROM notify_outbox")] == outbox


@pytest.mark.parametrize("mixed, archive_held, max_jobs, done, expected_turn", [
    (False, False, 1, 0, None),
    (True, False, 2, 1, None),
    (True, True, 2, 2, "arrival"),
], ids=["active-lock-denied", "archive-lock-denied", "archive-served"])
def test_fairness_turn_changes_only_when_its_contested_job_is_served(
        led, monkeypatch, mixed, archive_held, max_jobs, done, expected_turn):
    _patient(led, 2)
    led.save_messages([_message(mid=20, project_id=2, body=BODY, unread=True)],
                      project_id=2, semantic=True, notify={"source": "unread"})
    cohort_pid = 1 if mixed else 2
    if mixed:
        _patient(led, 1)
        led.save_messages([_message(mid=10, project_id=1, body=BODY, unread=True)],
                          project_id=1, semantic=True, notify={"source": "unread"})
    led.save_messages([_message(mid=11, project_id=cohort_pid, body=BODY)],
                      project_id=cohort_pid, semantic=True)
    if mixed:
        with led.db:
            led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")

    @contextmanager
    def job_lock(store, job_id):
        pid = store.db.execute("SELECT project_id FROM fetch_jobs WHERE job_id=?",
                               (job_id,)).fetchone()[0]
        yield mixed and (pid == 2 or archive_held)

    monkeypatch.setattr(semantic_drain, "_job_lock", job_lock)
    result = {"errors": []}
    out = semantic_drain.run_due(led, _cfg("shadow", budget=100000), result,
                           time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm,
                           max_jobs=max_jobs)
    assert out["done"] == done and not out["failed"] and not result["errors"]
    assert out["lock_skipped"].get(semantic_drain.JOB_KIND, 0) == (0 if archive_held else 2)
    sched = semantic_drain._sched_state(led)
    assert sched["arrival_selected"] == done and sched["backfill_selected"] == 0
    assert sched.get("archived_turn" if mixed else "turn") == expected_turn
    assert sched.get("turn" if mixed else "archived_turn") is None
