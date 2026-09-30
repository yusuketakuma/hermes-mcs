"""Fixed lanes let ingestion run during inference without duplicate jobs."""
import os
import time

import local_llm
import mcs_util
import semantic
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
        out = semantic.run_due(db, _cfg(), result, time.monotonic() + 480,
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
    monkeypatch.setattr(semantic, '_process_job',
                        lambda ledger, cfg, job, *a, **k: seen.append(job['message_id']) or 'stale')
    try:
        semantic.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 480,
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
        semantic.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 480,
                         jev_client=_FakeJev(), llm_fn=_llm, max_jobs=1,
                         lane='realtime')
        assert seen == [original['message_id']]
        root['eligible'] = False
        db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                      (json.dumps(root), original['job_id']))
        db.db.commit()
        seen.clear()
        out = semantic.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 480,
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
        semantic.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 480,
                         llm_fn=_llm, jev_client=client, run_lock_fd=fd)
        assert calls
        assert client._http_request is http
        assert mcs_util.acquire_run_lock(path) is None
    finally:
        os.close(fd)
        db.close()
