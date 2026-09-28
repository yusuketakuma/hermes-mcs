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
