"""Human pause commands hold semantic work without erasing source or delivery state."""
import json
import time
import uuid

import pytest

import mcs_requests
import notifier
import semantic
import semantic_runtime as runtime
from test_mcs_semantic import _seeded, _message, _cfg, _FakeJev, _llm
from test_semantic_delivery import _semantic_event


def _control(db, action):
    result = mcs_requests.apply_command(db, {
        'version': 1, 'cmd': 'ops.' + action, 'command_id': str(uuid.uuid4()),
        'actor': 'discord:synthetic-human', 'human_confirmed': True,
        'project_id': 1, 'feature': 'semantic'})
    assert result['outcome'] == 'applied', result


def test_pause_preserves_source_jobs_and_receipts_and_resume_is_explicit(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    try:
        job = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
        token = runtime.JobToken.from_row(job)
        _control(db, 'pause')
        assert not runtime.job_matches(db, token)
        db.save_messages([_message(3)], notify={'source': 'unread'}, semantic=True)
        assert db.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 3
        assert db.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind='semantic'").fetchone()[0] == 1
        client = _FakeJev()
        held = semantic.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 300,
                                jev_client=client, llm_fn=_llm)
        assert held['deferred'] == 1 and client.requests_made == 0
        assert semantic.status_report(db)['semantic_paused_projects'] == [1]
        _control(db, 'resume')
        assert not runtime.job_matches(db, token)  # pause/resume cannot revive an old worker
        current = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert current['attempts'] == job['attempts']
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        resumed = semantic.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 300,
                                   jev_client=client, llm_fn=_llm)
        assert resumed['done'] == 1 and client.requests_made > 0
        assert db.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind='semantic'").fetchone()[0] == 1
        event = _semantic_event(db)
        payload = json.loads(event['payload'])
        monkeypatch.setattr(notifier, '_config', lambda: _cfg('enforce'))
        _control(db, 'pause')
        with pytest.raises(notifier._DeferredSend, match='semantic_paused'):
            notifier._semantic_gate(db, event, payload)
        with pytest.raises(notifier._FreezeSend, match='semantic_paused'):
            notifier._semantic_gate(db, event, payload, in_progress=True)
        assert db.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 3
        assert db.db.execute('SELECT count(*) FROM command_receipts').fetchone()[0] == 3
    finally:
        db.close()


def test_pause_after_external_response_prevents_promotion_and_next_call(tmp_path):
    db = _seeded(tmp_path)
    class PauseAfterFirst(_FakeJev):
        def evaluate(self, *args, **kwargs):
            result = super().evaluate(*args, **kwargs)
            _control(db, 'pause')
            return result
    try:
        client = PauseAfterFirst()
        result = semantic.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 300,
                                  jev_client=client, llm_fn=_llm)
        assert client.requests_made == 1 and result['done'] == 0
        assert db.db.execute("SELECT count(*) FROM artifacts WHERE kind='semantic_summary'").fetchone()[0] == 0
        current = db.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert tuple(current) == ('pending', 0)
    finally:
        db.close()


def test_paused_arrival_is_durable_and_resumes_without_reimport(tmp_path):
    db = _seeded(tmp_path)
    try:
        _control(db, 'pause')
        db.save_messages([_message(3)], project_id=1,
                         notify={'source': 'unread'}, semantic=True)
        assert db.job_pending('semantic', 1, 3) is not None
        client = _FakeJev()
        held = semantic.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 60,
                                jev_client=client, llm_fn=_llm)
        assert held['done'] == 0 and client.requests_made == 0
        db.close()
        from ledger import Ledger
        db = Ledger(str(tmp_path / "ledger.db"))
        assert db.job_pending('semantic', 1, 3) is not None
        assert semantic.status_report(db)['semantic_paused_projects'] == [1]
        _control(db, 'resume')
        resumed = semantic.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 60,
                                   jev_client=client, llm_fn=_llm)
        assert resumed['done'] == 2
        assert db.artifacts('semantic_summary', message_id=3)
    finally:
        db.close()
