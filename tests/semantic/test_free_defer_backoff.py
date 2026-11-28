"""A free deterministic semantic hold (no call that may have run a model or
sent a request, no Jev request, no saved stage) waits NOT_SENT_BACKOFF_S and
hands its slot to the next due job, while LLM-availability defers and holds
that ran a model keep the one-minute retry.

Synthetic fixtures and temp DB only; the processor or, for the real-path
tests, only the transport boundary is stubbed."""
import json
import time
from contextlib import contextmanager

import pytest

import semantic_drain
import semantic_job_processor
import semantic_runtime as runtime
import semantic_store
import semantic_transport
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _FakeJev, _cfg, _ledger, _llm, _message, _patient


@pytest.fixture
def jobs_db(tmp_path):
    db = _ledger(tmp_path)
    _patient(db)
    yield db
    db.close()


def _job(db, mid, targets=None, **extra):
    now = time.time() - 10
    payload = {VERSION_KEY: CURRENT_VERSION, "targets": targets or [mid],
               "eligible": True, "generation": f"g{mid}", **extra}
    with db.db:
        return db.db.execute(
            "INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,"
            "payload,state,attempts,next_try,created_at,updated_at)"
            " VALUES('semantic',1,?,NULL,?,'pending',0,?,?,?)",
            (mid, json.dumps(payload, sort_keys=True), now, now, now)).lastrowid


def _row(db, job_id):
    return db.db.execute("SELECT * FROM fetch_jobs WHERE job_id=?", (job_id,)).fetchone()


def _processor(monkeypatch, *, code='audit_empty_unverified', not_sent=None, model=False,
               held_codes=None):
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
        if held_codes is not None:
            targets = runtime.parse_payload(job)['targets']
            kw['diagnostics']['held_target_codes'] = {
                t: held_codes for t in targets}
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


@pytest.mark.parametrize('held_codes', ['empty_repair_candidate_binding', 'audit_empty_unverified'])
def test_multi_target_hold_of_every_target_waits_an_hour(jobs_db, monkeypatch, held_codes):
    held = _job(jobs_db, 100, targets=[100, 101])
    _job(jobs_db, 999)
    served = _processor(monkeypatch, code=held_codes, held_codes=held_codes)
    _run(jobs_db)
    assert served == [100, 999]
    row = _row(jobs_db, held)
    assert row['state'] == 'pending' and row['attempts'] == 0
    assert row['next_try'] == row['updated_at'] + semantic_drain.NOT_SENT_BACKOFF_S


def test_multi_target_hold_with_a_non_deterministic_target_keeps_minute(jobs_db, monkeypatch):
    held = _job(jobs_db, 100, targets=[100, 101])
    _processor(monkeypatch, code='empty_repair_candidate_binding',
               held_codes='empty_repair_marker_binding')
    _run(jobs_db)
    row = _row(jobs_db, held)
    assert row['next_try'] == row['updated_at'] + 60


def test_multi_target_binding_holds_no_longer_starve_reprocess_rows(jobs_db, monkeypatch):
    """Live shape: more deterministic multi-target holds than one backlog
    pass scans (max_jobs=1 inspects 16 rows), with a notification-free
    reprocess row ordered last. Before the fix each hold came back after
    60 s, so every later pass rescanned the same holds and the reprocess
    row never ran."""
    holds = [_job(jobs_db, mid, targets=[mid, mid + 1000])
             for mid in range(100, 120)]
    reprocess = _job(jobs_db, 999, reprocess_only=True, notification_free=True)
    served = _processor(monkeypatch, code='empty_repair_candidate_binding',
                        held_codes='empty_repair_candidate_binding')
    _run(jobs_db)
    assert 999 not in served and len(served) == 16
    with jobs_db.db:
        # more than the one-minute retry, less than the hourly wait
        jobs_db.db.execute("UPDATE fetch_jobs SET next_try=next_try-120,"
                           " updated_at=updated_at-120 WHERE kind='semantic'")
    del served[:]
    out = _run(jobs_db)
    assert served == [116, 117, 118, 119, 999]
    assert out['done'] == 1
    assert _row(jobs_db, reprocess)['state'] == 'done'
    for job_id in holds:
        row = _row(jobs_db, job_id)
        assert row['state'] == 'pending' and row['attempts'] == 0
        assert row['next_try'] == row['updated_at'] + semantic_drain.NOT_SENT_BACKOFF_S


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


@pytest.mark.parametrize('second_code', ['empty_repair_candidate_binding', 'empty_repair_marker_binding',
                                         None])
def test_real_processor_reports_every_target_hold(tmp_path, monkeypatch, second_code):
    """Through the real summary loop: when both targets of a two-target job
    are held by the empty-repair gate, the hourly wait applies only if each
    target's own cause is deterministic. Only the gate verdict is injected."""
    import semantic_job_processor
    import semantic_transport
    from semantic_testkit import _PassJev, _canonical_cfg, _llm_v2, _seeded_two
    monkeypatch.setattr(semantic_transport, "_LLM_FAILURE", None)
    db = _seeded_two(tmp_path)
    try:
        codes = {1: 'empty_repair_candidate_binding', 2: second_code}
        real_gate = semantic_job_processor._empty_repair_candidate

        def gate(_db, _pid, mid, *args, reason_out=None, **kwargs):
            if codes[mid] is None:     # the real gate: target 2 publishes and is reused
                return real_gate(_db, _pid, mid, *args, reason_out=reason_out, **kwargs)
            reason_out['code'] = codes[mid]
            return None, True

        monkeypatch.setattr(semantic_job_processor, '_empty_repair_candidate', gate)

        def llm(prompt):
            semantic_transport._record_inference("local")
            return _llm_v2(prompt)

        def run():
            with db.db:
                db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            return semantic_drain.run_due(db, _canonical_cfg(), {"errors": []},
                                          time.monotonic() + 300, jev_client=_PassJev(),
                                          llm_fn=llm, lane='backlog', max_jobs=1)
        # the first passes write the facts and stage receipts (progress);
        # the steady state that repeats for days writes nothing
        run()
        run()
        out = run()
        assert out['progressed'] == 0 and out['deferred'] == 1
        assert out['defer_reasons'] == {'empty_repair_candidate_binding': 1}
        row = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert json.loads(row['payload'])['targets'] == [1, 2]
        assert row['state'] == 'pending' and row['attempts'] == 0
        wait = (semantic_drain.NOT_SENT_BACKOFF_S
                if second_code != 'empty_repair_marker_binding' else 60)
        assert row['next_try'] == row['updated_at'] + wait
    finally:
        db.close()


_HELD = (100, 101)
_TAIL = 102


def _no_local_model(prompt, timeout=None, max_tokens=None):
    pytest.fail('the backlog cloud lane must not call the local model')


def _cloud_lane(monkeypatch, answer):
    """Replace only the transport boundary: the backlog cloud context yields
    ``answer(root, prompt)`` as its chat; the local model stays unused."""
    served = []

    @contextmanager
    def backlog_inference(ledger, cfg, job, deadline, current, transport, **kw):
        root = job['message_id']
        served.append(root)
        yield lambda prompt, timeout=None: answer(root, prompt)

    monkeypatch.setattr(semantic_transport, 'backlog_inference', backlog_inference)
    monkeypatch.setattr(semantic_transport, 'llm_chat', _no_local_model)
    return served


def _cloud_drain(db, max_jobs):
    # llm_fn left out: the backlog lane then routes through the cloud context
    return semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                                  time.monotonic() + 300, jev_client=_FakeJev(),
                                  lane='backlog', max_jobs=max_jobs)


def test_unsent_hold_stages_are_transport_pre_send_refusals():
    """The evidence exemption only covers stages the transport itself
    documents as raised before anything was dispatched."""
    assert semantic_job_processor.UNSENT_HOLD_STAGES
    assert semantic_job_processor.UNSENT_HOLD_STAGES <= semantic_transport._PRESEND_REFUSAL_STAGES
    assert semantic_job_processor.UNSENT_HOLD_STAGES <= semantic_transport.MEMBER_HOLD_STAGES


def _job_id(db, mid):
    return db.db.execute("SELECT job_id FROM fetch_jobs WHERE kind='semantic'"
                         " AND message_id=?", (mid,)).fetchone()[0]


@pytest.mark.parametrize('stage', ['cloud_unit_unresolved', 'llm_turn_busy'])
def test_real_unsent_cloud_holds_yield_to_the_tail(tmp_path, monkeypatch, stage):
    """#417 through the real run_due and _process_job, only the transport
    replaced: head jobs whose cloud send is refused as unresolved before any
    reservation stop re-heading the queue every minute, and the job behind
    them runs. Before the fix the refusal's measured time (llm_s > 0) and its
    member-level code kept the minute retry and the only slot, so the
    measured pass served [100] and left next_try = updated_at + 60. A busy
    GPU turn can clear within a minute and keeps both."""
    def answer(root, prompt):
        if root == _TAIL:
            return _llm(prompt)
        raise runtime.LLMNotSent(stage)

    db = _ledger(tmp_path)
    _patient(db)
    try:
        db.save_messages([_message(mid) for mid in _HELD], project_id=1)
        db.semantic_seed(1, list(_HELD), {"source": "synthetic"})
        held = [_job_id(db, mid) for mid in _HELD]
        served = _cloud_lane(monkeypatch, answer)
        # First pass: each head saves its bundle and pays its Jev assessment
        # before the refused call, so it is no free hold.
        first = _cloud_drain(db, len(_HELD))
        assert served == list(_HELD) and first['progressed'] == len(_HELD)
        for job_id in held:
            row = _row(db, job_id)
            assert row['state'] == 'pending' and row['attempts'] == 0
            assert row['next_try'] == row['updated_at'] + 60
        db.save_messages([_message(_TAIL)], project_id=1)
        db.semantic_seed(1, [_TAIL], {"source": "synthetic"})
        tail = _job_id(db, _TAIL)
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        del served[:]
        out = _cloud_drain(db, 1)
        if stage == 'llm_turn_busy':
            assert served == [_HELD[0]] and out['done'] == 0
            row = _row(db, held[0])
            assert row['next_try'] == row['updated_at'] + 60
            assert _row(db, tail)['state'] == 'pending'
            return
        assert served == [*_HELD, _TAIL]
        assert out['done'] == 1 and _row(db, tail)['state'] == 'done'
        assert out['progressed'] == 1    # only the tail saved anything
        metrics = {m['job_id']: m for m in out['job_metrics']}
        for job_id in held:
            metric, row = metrics[job_id], _row(db, job_id)
            # nothing paid or saved; the refusal's time is still measured
            assert metric['status'] == 'deferred' and metric['jev_requests'] == 0
            assert metric['retry_reason'] == 'cloud_unit_unresolved'
            assert metric['llm_s'] > 0
            assert row['state'] == 'pending' and row['attempts'] == 0
            assert row['next_try'] == row['updated_at'] + semantic_drain.NOT_SENT_BACKOFF_S
        # a re-seed after an input change makes the hold due again
        db.save_messages([_message(_HELD[0], body="合成の別本文です。")], project_id=1)
        db.semantic_seed(1, [_HELD[0]], {"source": "synthetic"})
        row = _row(db, held[0])
        assert row['state'] == 'pending' and row['next_try'] <= time.time()
    finally:
        db.close()


def test_answered_cloud_call_before_an_unsent_refusal_keeps_minute_and_slot(
        tmp_path, monkeypatch):
    """Control for the evidence split: an earlier cloud call of the same pass
    answered and may have been billed, so a later unresolved refusal is no
    free hold. Real run_due, _process_job and its cloud call wrapper; the
    stages only make the two calls and record the member hold as the
    processor does. Marking every cloud call unsent would wait an hour and
    serve the tail too."""
    def answer(root, prompt):
        if prompt == 'second':
            raise runtime.LLMNotSent('cloud_unit_unresolved')
        return '{}'

    def stages(ledger, scfg, job, jev_client, llm_fn, deadline, *, diagnostics, **kw):
        llm_fn('first')
        try:
            llm_fn('second')
        except runtime.LLMNotSent as error:
            semantic_drain._why(diagnostics, error.stage)
        return 'deferred'

    db = _ledger(tmp_path)
    _patient(db)
    try:
        db.save_messages([_message(_HELD[0]), _message(_TAIL)], project_id=1)
        db.semantic_seed(1, [_HELD[0]], {"source": "synthetic"})
        db.semantic_seed(1, [_TAIL], {"source": "synthetic"})
        served = _cloud_lane(monkeypatch, answer)
        monkeypatch.setattr(semantic_job_processor, '_process_job_inner', stages)
        monkeypatch.setattr(semantic_store, 'refresh_cards_allowed', lambda *a: False)
        out = _cloud_drain(db, 1)
        assert served == [_HELD[0]] and out['progressed'] == 0
        assert out['job_metrics'][0]['jev_requests'] == 0
        row = _row(db, _job_id(db, _HELD[0]))
        assert row['state'] == 'pending' and row['attempts'] == 0
        assert row['next_try'] == row['updated_at'] + 60
    finally:
        db.close()
