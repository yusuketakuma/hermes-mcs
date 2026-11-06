"""Patient headings keep raw-name identity without repeated full-table scans."""
from contextlib import closing
from types import SimpleNamespace
import sqlite3

import pytest

import ledger
import ledger_schema
import job_ops
from common_testkit import make_ledger
from notify_render import patient_heading


def _rows(db):
    tables = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {name: sorted((tuple(row) for row in db.execute(
        'SELECT * FROM "' + name.replace('"', '""') + '"')), key=repr)
            for name in tables}


@pytest.mark.parametrize("reopen", [False, True])
def test_patient_headings_preserve_rows_and_bounded_lookup_work(tmp_path, monkeypatch, reopen):
    # One synthetic clock also stabilizes canonical relation-audit bookkeeping.
    monkeypatch.setattr(ledger_schema, "time", SimpleNamespace(time=lambda: 1800000000.0))
    store = make_ledger(tmp_path)
    try:
        rows = [(pid, f"合成患者{pid}", "合成施設", 0) for pid in range(1, 1001)]
        rows += [(2001, "合成同名", "施設甲", 0),
                 (2002, "合成同名", "施設乙", 1),
                 (2003, "合成同名 ", "", 0),
                 (2004, None, "施設甲", 0), (2005, "", "", 0),
                 (2006, "  ", "", 0), (2007, "合成é", "", 0),
                 (2008, "合成e\u0301", "", 0)]
        with store.db:
            store.db.executemany(
                "INSERT INTO patients(project_id,patient_name,station_name,is_archived) "
                "VALUES(?,?,?,?)", rows)
            store.db.execute("INSERT INTO runs(status) VALUES('done')")
        saved = _rows(store.db)
        if reopen:
            store.close()
            # Emulate an existing compatible DB predating this optional index.
            with closing(sqlite3.connect(tmp_path / "ledger.db")) as db, db:
                db.execute("DROP INDEX IF EXISTS idx_patients_name")
                assert db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
            store = make_ledger(tmp_path)
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
        assert _rows(store.db) == saved
        expected = {pid: f"合成患者{pid}（合成施設）" for pid in range(1, 1001)}
        expected.update({2001: "合成同名（施設甲） #2001", 2002: "合成同名（施設乙） #2002",
                         2003: "合成同名", 2004: "project 2004", 2005: "project 2005",
                         2006: "project 2006", 2007: "合成é", 2008: "合成e\u0301"})
        assert {pid: patient_heading(store.db, pid) for pid in expected} == expected
        assert patient_heading(store.db, 9999) == "project 9999"
        assert patient_heading(store.db, 0) == ""
        steps = 0

        def progress():
            nonlocal steps
            steps += 1
            return 0

        store.db.set_progress_handler(progress, 1)
        try:
            actual = [patient_heading(store.db, pid) for pid in range(1, 1001)]
        finally:
            store.db.set_progress_handler(None, 0)
        assert actual == [expected[pid] for pid in range(1, 1001)]
        # Broad deterministic work budget: a full patients scan per heading fails.
        assert 0 < steps < 90000
        assert _rows(store.db) == saved
        with store.db:
            store.db.execute("UPDATE patients SET patient_name='合成別名' WHERE project_id=2002")
        assert patient_heading(store.db, 2001) == "合成同名（施設甲）"
        assert patient_heading(store.db, 2002) == "合成別名（施設乙）"
        with store.db:
            store.db.execute("DELETE FROM patients WHERE project_id=2002")
        assert patient_heading(store.db, 2001) == "合成同名（施設甲）"
    finally:
        store.close()


def test_patient_name_index_preserves_all_patient_job_priority(tmp_path):
    store = make_ledger(tmp_path)
    try:
        with store.db:
            # Reverse names and a missing name expose a covering-index reorder.
            store.db.executemany(
                "INSERT INTO patients(project_id,patient_name,is_archived) VALUES(?,?,?)",
                [(10, "合成C", 0), (20, "合成B", 0), (30, "合成A", 0),
                 (40, None, 0), (50, "合成0", 1)])
        patients = [tuple(row) for row in store.db.execute("SELECT * FROM patients ORDER BY project_id")]
        assert [row["project_id"] for row in store.known_patients()] == [10, 20, 30, 40, 50]
        # The rollup CLI also enumerates all patients without a name predicate.
        assert [row[0] for row in store.db.execute("SELECT project_id FROM patients")] == [10, 20, 30, 40, 50]
        assert job_ops.seed_trickle(store) == 4
        jobs = [tuple(row) for row in store.db.execute(
            "SELECT job_id,project_id,kind,state FROM fetch_jobs ORDER BY job_id")]
        assert [row[1] for row in jobs] == [10, 20, 30, 40]
        assert all(row[2:] == ("history", "pending") for row in jobs)
        assert len({row[0] for row in jobs}) == 4
        assert job_ops.seed_trickle(store) == 0
        assert [tuple(row) for row in store.db.execute(
            "SELECT job_id,project_id,kind,state FROM fetch_jobs ORDER BY job_id")] == jobs
        assert [tuple(row) for row in store.db.execute("SELECT * FROM patients ORDER BY project_id")] == patients
    finally:
        store.close()
