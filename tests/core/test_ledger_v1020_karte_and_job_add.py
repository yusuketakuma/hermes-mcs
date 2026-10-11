"""Ledger regressions: karte_id change with identical summary, job_add return id."""

import read_model
from ingest_testkit import _ledger, _unread_patient


def _karte_patient(pid, karte_id):
    p = _unread_patient(pid)
    p.karte_id = karte_id
    return p


def test_karte_id_change_with_identical_summary_stays_fetched(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_karte_patient(1, 100))
    assert db.karte_summary_store(1, 100, None) is True
    db.upsert_patient_info(_karte_patient(1, 200))
    assert db.karte_id(1) == 200

    assert db.karte_summary_store(1, 200, None) is True  # new karte, same content
    rows = read_model._project_metadata(db.db, "aggregate", 1, None)
    assert [r["state"] for r in rows["records"]] == ["fetched_empty"]
    assert db.karte_summary_store(1, 200, None) is False  # now a true duplicate
    assert len(db.artifacts("karte_summary", project_id=1)) == 2
    db.close()


def test_job_add_returns_the_jobs_own_id_or_none(tmp_path):
    db = _ledger(tmp_path)
    first = db.job_add("history", 1, payload={"since": 0})
    for pid in range(2, 7):  # later inserts move the connection's lastrowid
        db.job_add("history", pid, payload={"since": 0})
    assert db.job_add("history", 1, payload={"since": 5}) is None  # pending kept

    db.db.execute("UPDATE fetch_jobs SET state='done' WHERE job_id=?", (first,))
    db.db.commit()
    assert db.job_add("history", 1, payload={"since": 9}) == first  # revived
    db.close()
