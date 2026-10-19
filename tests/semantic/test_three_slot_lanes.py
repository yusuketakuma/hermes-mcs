"""Fixed lanes let ingestion run during inference without duplicate jobs."""
import semantic_qc
import os
import time

import local_llm
import mcs_util
import semantic_drain
from semantic_testkit import _cfg, _FakeJev, _llm, _seeded


def test_transport_releases_ingest_lock_but_keeps_job_claim(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    path = str(tmp_path / 'run.lock')
    fd = mcs_util.acquire_run_lock(path)
    seen = []

    def model(prompt):
        # A second collector can write while inference runs. A second
        # semantic worker cannot run the same job, even on another slot.
        other = mcs_util.acquire_run_lock(path)
        assert other is not None
        os.close(other)
        job = db.db.execute("SELECT job_id FROM fetch_jobs WHERE kind='semantic'").fetchone()
        with semantic_drain._job_lock(db, job[0]) as held:
            assert not held
        seen.append(prompt)
        return _llm(prompt)

    try:
        result = {'errors': []}
        out = semantic_drain.run_due(db, _cfg(), result, time.monotonic() + 480,
                               llm_fn=model, jev_client=_FakeJev(),
                               run_lock_fd=fd)
        assert out['done'] == 1 and seen and not result['errors']
        assert mcs_util.acquire_run_lock(path) is None
    finally:
        os.close(fd)
        db.close()


def test_realtime_selects_newest_arrival_background_selects_oldest(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    original = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
    payload = original['payload']
    import json
    old = json.loads(payload)
    old['eligible'] = False
    db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?", (json.dumps(old), original['job_id']))
    for mid in (2, 3):
        arrival = json.loads(payload)
        arrival['eligible'] = True
        db.job_add('semantic', 1, mid, payload=arrival)
    db.db.commit()
    seen = []
    monkeypatch.setattr(semantic_drain, '_process_job',
                        lambda ledger, cfg, job, *a, **k: seen.append(job['message_id']) or 'stale')

    def no_fairness(*a, **k):
        raise AssertionError('explicit lane must not compute fairness lanes')
    # an explicit lane selects its own rows; the lane=None fairness
    # computation would be discarded, so it must not run at all
    monkeypatch.setattr(semantic_drain, '_due_lanes', no_fairness)
    try:
        semantic_drain.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 480,
                         jev_client=_FakeJev(), llm_fn=_llm, max_jobs=1,
                         lane='realtime')
        assert seen == [3]
        # A fresh reply reseeds an old root; it must regain realtime
        # priority even though its original job_id/created_at are old.
        root = json.loads(payload)
        root['eligible'] = True
        db.db.execute("UPDATE fetch_jobs SET payload=?, updated_at=? WHERE job_id=?",
                      (json.dumps(root), time.time() + 1, original['job_id']))
        db.db.commit()
        seen.clear()
        semantic_drain.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 480,
                         jev_client=_FakeJev(), llm_fn=_llm, max_jobs=1,
                         lane='realtime')
        assert seen == [original['message_id']]
        root['eligible'] = False
        db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                      (json.dumps(root), original['job_id']))
        db.db.commit()
        seen.clear()
        out = semantic_drain.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 480,
                               jev_client=_FakeJev(), llm_fn=_llm, max_jobs=1,
                               lane='backlog')
        assert seen == [original['message_id']]
        assert out['job_metrics'][0]['cohort'] == 'backfill'
        assert local_llm.SLOT_COUNT == 3
        assert local_llm.REALTIME_SLOT not in local_llm.BACKGROUND_SLOTS
    finally:
        db.close()


def test_jev_receipt_is_committed_before_unlocked_transport(tmp_path, monkeypatch):
    import semantic_jev
    db = _seeded(tmp_path)
    path = str(tmp_path / 'run.lock')
    fd = mcs_util.acquire_run_lock(path)
    calls = []
    client = semantic_jev.JevClient(api_key='synthetic')

    def http(*args, **kwargs):
        assert not db.db.in_transaction
        other = mcs_util.acquire_run_lock(path)
        assert other is not None
        os.close(other)
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='semantic_usage'").fetchone()[0] > 0
        calls.append(args)
        return 400, {}, b'{}'

    monkeypatch.setattr(client, '_http_request', http)
    try:
        semantic_drain.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 480,
                         llm_fn=_llm, jev_client=client, run_lock_fd=fd)
        assert calls
        assert client._http_request is http
        assert mcs_util.acquire_run_lock(path) is None
    finally:
        os.close(fd)
        db.close()


def test_backlog_alternates_so_reseeded_qc_cannot_starve_semantic(tmp_path, monkeypatch):
    """2026-10-10: re-extraction re-pends old QC rows under their original
    (low) job_id; in plain job_id order they won every backlog pass and no
    semantic job ran for hours. The backlog lane alternates kinds."""
    db = _seeded(tmp_path)
    db.db.execute("UPDATE fetch_jobs SET job_id=100 WHERE kind='semantic'")
    db.db.execute(
        "INSERT INTO fetch_jobs(job_id,kind,project_id,message_id,payload,"
        "state,attempts,next_try,created_at,updated_at) "
        "VALUES (5,'extract_qc',1,1,'{}','pending',0,0,0,0)")
    db.db.commit()
    seen = []
    monkeypatch.setattr(semantic_drain, '_BACKLOG_TURN', {'kind': None})
    monkeypatch.setattr(semantic_qc, '_qc_seed', lambda *a, **k: 0)
    # 'stale' leaves both rows pending, as a QC re-pend does
    monkeypatch.setattr(semantic_drain, '_process_job',
                        lambda *a, **k: seen.append('semantic') or 'stale')
    monkeypatch.setattr(semantic_qc, '_process_qc_job',
                        lambda *a, **k: seen.append('extract_qc') or 'stale')
    try:
        for _ in range(3):
            semantic_drain.run_due(db, _cfg(extract_qc='annotate'), {'errors': []},
                             time.monotonic() + 480, jev_client=_FakeJev(),
                             llm_fn=_llm, max_jobs=1, lane='backlog')
        assert seen == ['semantic', 'extract_qc', 'semantic']
    finally:
        db.close()


def test_reseeded_old_qc_cannot_overtake_a_waiting_qc(tmp_path, monkeypatch):
    """A refreshed extraction queues a new QC generation, even when its
    job row predates another QC that has been waiting longer."""
    db = _seeded(tmp_path)
    clock = [time.time()]
    db.db.execute("UPDATE fetch_jobs SET state='done' WHERE kind='semantic'")
    for job_id, mid in ((5, 1), (100, 2)):
        db.db.execute(
            "INSERT INTO fetch_jobs(job_id,kind,project_id,message_id,payload,"
            "state,attempts,next_try,created_at,updated_at) "
            "VALUES (?,'extract_qc',1,?,'{}','pending',0,?,0,0)",
            (job_id, mid, clock[0] - 10))
    db.db.commit()
    seen = []
    monkeypatch.setattr(semantic_drain.time, 'time', lambda: clock[0])
    monkeypatch.setattr(semantic_drain, '_BACKLOG_TURN', {'kind': None})

    def reseed(ledger, now):
        # Same generation timing as _qc_seed's ON CONFLICT update.
        with ledger.db:
            ledger.db.execute(
                "UPDATE fetch_jobs SET state='pending',attempts=0,"
                "next_try=?,updated_at=? WHERE job_id=5", (now, now))

    def process(ledger, scfg, job, *args, **kwargs):
        seen.append(job['message_id'])
        with ledger.db:
            ledger.db.execute("UPDATE fetch_jobs SET state='done' WHERE job_id=?",
                              (job['job_id'],))
        return 'done'

    monkeypatch.setattr(semantic_qc, '_qc_seed', reseed)
    monkeypatch.setattr(semantic_qc, '_process_qc_job', process)
    try:
        for _ in range(3):
            semantic_drain.run_due(db, _cfg(extract_qc='annotate'), {'errors': []},
                             time.monotonic() + 480, jev_client=_FakeJev(),
                             llm_fn=_llm, max_jobs=1, lane='backlog')
            clock[0] += 1
        assert seen == [2, 1, 1]
    finally:
        db.close()
