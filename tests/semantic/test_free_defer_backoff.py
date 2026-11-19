"""A free deterministic semantic hold (no model time, no Jev request, no
saved stage) waits NOT_SENT_BACKOFF_S and hands its slot to the next due
job, while LLM-availability defers and holds that spent model time keep
the one-minute retry.

Synthetic fixtures, temp DB and stubbed processor only."""
import json
import time

import pytest

import semantic_drain
import semantic_runtime as runtime
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _FakeJev, _cfg, _ledger, _message, _patient


@pytest.fixture
def jobs_db(tmp_path):
    db = _ledger(tmp_path)
    _patient(db)
    yield db
    db.close()


def _job(db, mid, targets=None):
    now = time.time() - 10
    payload = {VERSION_KEY: CURRENT_VERSION, "targets": targets or [mid],
               "eligible": True, "generation": f"g{mid}"}
    with db.db:
        return db.db.execute(
            "INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,"
            "payload,state,attempts,next_try,created_at,updated_at)"
            " VALUES('semantic',1,?,NULL,?,'pending',0,?,?,?)",
            (mid, json.dumps(payload, sort_keys=True), now, now, now)).lastrowid


def _row(db, job_id):
    return db.db.execute("SELECT * FROM fetch_jobs WHERE job_id=?", (job_id,)).fetchone()


def _processor(monkeypatch, *, code='audit_empty_unverified', not_sent=None, model=False):
    # Only the processor's result contract is injected; selection, slot
    # accounting and the generation-fenced transitions stay real.
    served = []

    def process(db, scfg, job, jev_client, llm_fn, deadline, **kw):
        served.append(job['message_id'])
        if job['message_id'] == 999:
            assert runtime.transition(db, runtime.JobToken.from_row(job), 'done')
            return 'done'
        semantic_drain._why(kw['diagnostics'], code)
        if model:
            llm_fn('synthetic scheduler control')
        kw['diagnostics']['not_sent'] = not_sent
        return 'deferred'

    monkeypatch.setattr(semantic_drain, '_process_job', process)
    return served


def _run(db, **kw):
    return semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                                  time.monotonic() + 300, jev_client=_FakeJev(),
                                  llm_fn=lambda prompt: '{}', lane='backlog',
                                  max_jobs=1, **kw)


@pytest.mark.parametrize('prioritize', [False, True])
@pytest.mark.parametrize('code', ['audit_empty_unverified', 'empty_repair_candidate_binding'])
def test_free_hold_hands_slot_on_and_waits_an_hour(jobs_db, monkeypatch, prioritize, code):
    held = _job(jobs_db, 100)
    _job(jobs_db, 999)
    served = _processor(monkeypatch, code=code)
    out = _run(jobs_db, prioritize_patients=prioritize)
    # before the change the held head consumed the only slot: served == [100]
    assert served == [100, 999]
    assert out['done'] == 1 and out['deferred'] == 1
    row = _row(jobs_db, held)
    assert row['state'] == 'pending' and row['attempts'] == 0
    # before the change: updated_at + 60
    assert row['next_try'] == row['updated_at'] + semantic_drain.NOT_SENT_BACKOFF_S


@pytest.mark.parametrize('kind', ['model_time', 'llm_turn_busy', 'probe_unsent'])
def test_paid_or_llm_unavailable_defer_keeps_minute_and_slot(jobs_db, monkeypatch, kind):
    held = _job(jobs_db, 100)
    _job(jobs_db, 999)
    served = _processor(monkeypatch, model=kind == 'model_time',
                        not_sent=None if kind == 'model_time' else kind)
    out = _run(jobs_db, prioritize_patients=True)
    assert served == [100] and out['done'] == 0 and out['deferred'] == 1
    if kind == 'model_time':
        assert out['job_metrics'][0]['llm_s'] > 0
    row = _row(jobs_db, held)
    assert row['attempts'] == 0
    assert row['next_try'] == row['updated_at'] + 60


def test_multi_target_free_hold_hands_slot_on_but_keeps_minute(jobs_db, monkeypatch):
    # A later target may wait on a resource without a diagnostic code.
    held = _job(jobs_db, 100, targets=[100, 101])
    _job(jobs_db, 999)
    served = _processor(monkeypatch)
    _run(jobs_db)
    assert served == [100, 999]
    row = _row(jobs_db, held)
    assert row['next_try'] == row['updated_at'] + 60


def test_realtime_lane_free_hold_is_unchanged(jobs_db, monkeypatch):
    held = _job(jobs_db, 100)
    served = _processor(monkeypatch)
    semantic_drain.run_due(jobs_db, _cfg("shadow"), {"errors": []},
                           time.monotonic() + 300, jev_client=_FakeJev(),
                           llm_fn=None, lane='realtime', max_jobs=1)
    assert served == [100]
    row = _row(jobs_db, held)
    assert row['next_try'] == row['updated_at'] + 60


def test_reseed_after_input_change_returns_hold_to_now(jobs_db, monkeypatch):
    jobs_db.save_messages([_message(100)], project_id=1)
    jobs_db.semantic_seed(1, [100], {"source": "synthetic"})
    job_id = jobs_db.db.execute(
        "SELECT job_id FROM fetch_jobs WHERE kind='semantic' AND message_id=100").fetchone()[0]
    _processor(monkeypatch)
    _run(jobs_db)
    assert _row(jobs_db, job_id)['next_try'] > time.time() + 3000
    edited = _message(100, body="合成の別本文です。")
    jobs_db.save_messages([edited], project_id=1)
    jobs_db.semantic_seed(1, [100], {"source": "synthetic"})
    row = _row(jobs_db, job_id)
    assert row['state'] == 'pending' and row['next_try'] <= time.time()
