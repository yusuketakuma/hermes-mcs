"""Executed work that exceeds its deadline must consume the retry budget."""
import time

import pytest

import semantic
from semantic_testkit import _FakeJev, _cfg, _llm, _seeded


@pytest.mark.parametrize('slow_stage', ['jev', 'llm'])
def test_post_call_deadline_is_bounded(tmp_path, monkeypatch, slow_stage):
    clock = [100.0]
    monkeypatch.setattr(semantic.time, 'monotonic', lambda: clock[0])
    calls = []

    class Client(_FakeJev):
        def evaluate(self, *args, **kwargs):
            result = super().evaluate(*args, **kwargs)
            if slow_stage == 'jev':
                calls.append('jev')
                clock[0] += 46
            return result

    def llm(prompt, timeout=None):
        if slow_stage == 'llm':
            calls.append('llm')
            clock[0] += 46
        return _llm(prompt)

    db = _seeded(tmp_path)
    try:
        for expected in range(1, 7):
            with db.db:
                db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            semantic.run_due(db, _cfg('shadow'), {'errors': []}, clock[0] + 300,
                             jev_client=Client(), llm_fn=llm)
            row = db.db.execute("SELECT state,attempts,next_try FROM fetch_jobs WHERE kind='semantic'").fetchone()
            assert row['attempts'] == expected
            assert row['next_try'] > time.time()
        assert row['state'] == 'failed' and len(calls) == 6
        semantic.run_due(db, _cfg('shadow'), {'errors': []}, clock[0] + 300,
                         jev_client=Client(), llm_fn=llm)
        assert len(calls) == 6
    finally:
        db.close()


def test_budget_before_dispatch_defers_without_attempt(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    monkeypatch.setattr(semantic.runtime, 'job_deadline', lambda *args: 0)
    client = _FakeJev()
    try:
        semantic.run_due(db, _cfg('shadow'), {'errors': []}, time.monotonic() + 300,
                         jev_client=client, llm_fn=_llm)
        row = db.db.execute("SELECT state,attempts,next_try FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert tuple(row[:2]) == ('pending', 0)
        assert row['next_try'] > time.time()
        assert not client.calls
    finally:
        db.close()


def test_next_call_short_of_reserve_defers_without_attempt(tmp_path, monkeypatch):
    """Each pass dispatches one long model call, persists its stage,
    and defers (attempts untouched) when the next call cannot start
    within LLM_CALL_RESERVE_S; the job completes over several runs
    from the durable stages (2026-09-30: multi-call jobs burned all
    six attempts on deadline overruns while progressing)."""
    clock = [100.0]
    monkeypatch.setattr(semantic.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(semantic.runtime.time, 'monotonic', lambda: clock[0])
    calls = []

    def llm(prompt, timeout=None):
        calls.append(prompt[:6])
        clock[0] += 200          # one long generation, inside the deadline
        return _llm(prompt)

    db = _seeded(tmp_path)
    try:
        cfg = _cfg('shadow', job_budget_seconds=450.0)
        runs = 0
        while runs < 6:
            with db.db:
                db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            before = len(calls)
            out = semantic.run_due(db, cfg, {'errors': []}, clock[0] + 450,
                                   jev_client=_FakeJev(), llm_fn=llm)
            runs += 1
            row = db.db.execute("SELECT state,attempts,next_try FROM fetch_jobs WHERE kind='semantic'").fetchone()
            assert row['attempts'] == 0
            assert len(calls) - before <= 1          # at most one long call per pass
            assert out['progressed'] == 1
            if row['state'] == 'done':
                break
            assert tuple(row[:2]) == ('pending', 0)
            assert row['next_try'] > time.time()
            assert out['deferred'] == 1 and out['done'] == 0
        assert row['state'] == 'done' and 2 <= runs <= 6
        # cached stages are never regenerated: one facts artifact per target
        facts = db.db.execute("SELECT COUNT(*), COUNT(DISTINCT message_id) "
                              "FROM artifacts WHERE kind='semantic_facts'").fetchone()
        assert facts[0] == facts[1] >= 1
    finally:
        db.close()


def test_budget_short_stops_the_pass_before_the_next_job(tmp_path, monkeypatch):
    """After one job yields on the call reserve, the pass ends: the next
    due job's FIRST call is exempt from the reserve gate and would be
    dispatched into a budget that cannot fit a long generation (a
    guaranteed timeout at the lane deadline, wasted model time)."""
    clock = [100.0]
    monkeypatch.setattr(semantic.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(semantic.runtime.time, 'monotonic', lambda: clock[0])
    calls = []

    def llm(prompt, timeout=None):
        calls.append(prompt[:6])
        clock[0] += 200
        return _llm(prompt)

    from semantic_testkit import _ledger, _message, _patient
    db = _ledger(tmp_path)
    try:
        p = _patient(db)
        p.messages = [_message(1), _message(3, body='別の投稿です。')]
        p.messages[0].replies = [_message(2, parent=1)]
        p.messages[1].replies = [_message(4, parent=3)]
        db.save_patient(p, notify={'source': 'unread'}, semantic=True)
        assert db.db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='semantic'").fetchone()[0] == 2
        out = semantic.run_due(db, _cfg('shadow', job_budget_seconds=450.0),
                               {'errors': []}, clock[0] + 450,
                               jev_client=_FakeJev(), llm_fn=llm)
        # one long call, then the reserve is short: no second job starts
        assert len(calls) == 1
        assert out['deferred'] == 1 and out['done'] == 0 and out['failed'] == 0
        assert [m['status'] for m in out['job_metrics']] == ['deferred_short']
        rows = db.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchall()
        assert [tuple(r) for r in rows] == [('pending', 0), ('pending', 0)]
    finally:
        db.close()


def test_short_defer_without_progress_charges_an_attempt(tmp_path, monkeypatch):
    """2026-09-30: a job whose passes persisted nothing looped as a free
    deferred_short (19 passes, attempts frozen, Jev spent each time).
    With no stage artifact written for its project, the pass is charged
    like any other overrun and the job fails after the bounded attempts."""
    import semantic_drain
    db = _seeded(tmp_path)
    monkeypatch.setattr(semantic_drain, "_process_job",
                        lambda *a, **k: "deferred_short")
    monkeypatch.setattr(semantic, "_process_job",
                        lambda *a, **k: "deferred_short")
    try:
        for _ in range(8):
            with db.db:
                db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            semantic.run_due(db, _cfg('shadow'), {'errors': []},
                             time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm)
        row = db.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert row['state'] == 'failed' and row['attempts'] >= 1
    finally:
        db.close()


def test_foreign_artifacts_do_not_count_as_progress(tmp_path):
    """An extract_llm row from a drainer, or another project's semantic
    row, never reads as this job's progress."""
    import semantic_drain
    db = _seeded(tmp_path)
    try:
        base = semantic_drain._last_stage_artifact(db, 1)
        db.artifact_add("extract_llm", "{}", project_id=1, message_id=1)
        db.artifact_add("semantic_facts", "{}", project_id=2, message_id=9)
        assert semantic_drain._last_stage_artifact(db, 1) == base
        db.artifact_add("semantic_facts", "{}", project_id=1, message_id=1)
        assert semantic_drain._last_stage_artifact(db, 1) > base
    finally:
        db.close()
