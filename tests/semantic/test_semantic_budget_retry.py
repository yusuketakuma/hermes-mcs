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
