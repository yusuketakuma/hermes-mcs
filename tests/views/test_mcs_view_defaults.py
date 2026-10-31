"""mcs_view default paths, run_stats OSError fallback and coverage patients.

Synthetic temp DBs and stubs only; no live data, no network."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import mcs_stats
import mcs_view
import read_model
from semantic_testkit import _ledger, _patient


def test_mcs_view_default_paths_follow_mcs_root(tmp_path):
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import mcs_view; "
            "a = mcs_view._parser().parse_args(['status']); "
            "print(a.snapshot); print(a.cmd_dir)")
    proc = subprocess.run(
        [sys.executable, "-c", code, str(Path(mcs_view.__file__).parent)],
        env={**os.environ, "MCS_ROOT": str(tmp_path)},
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    snapshot, cmd_dir = proc.stdout.split()
    assert snapshot == str(tmp_path / "data/snapshots/ledger-snapshot.db")
    assert cmd_dir == str(tmp_path / "data/cmd")


def test_run_stats_marks_oserror_stat_unavailable(tmp_path, monkeypatch):
    def boom(db, scope):
        raise OSError(84, "synthetic out-of-range timestamp")
    monkeypatch.setitem(mcs_stats.REGISTRY["overview"], "fn", boom)
    db = _ledger(tmp_path)
    out = mcs_stats.run_stats(db.db, 1_790_000_000, {"stat": "overview"})
    stat = out["stats"]["overview"]
    assert stat["status"] == "unavailable"
    assert stat["reason"] == "data_error:OSError"


def test_coverage_missing_patients_table_stays_unmeasured():
    db = sqlite3.connect(":memory:")
    cov = read_model._coverage(db, [], None)
    assert cov["collection"]["patients"] is None


class _FailingPatients:
    def __init__(self, db):
        self._db = db

    def execute(self, sql, *args):
        if "FROM patients" in sql:
            raise sqlite3.OperationalError("synthetic disk I/O error")
        return self._db.execute(sql, *args)


def test_coverage_patients_db_error_is_not_hidden(tmp_path):
    db = _ledger(tmp_path)
    _patient(db)
    assert read_model._coverage(db.db, [], None)["collection"]["patients"] == 1
    with pytest.raises(sqlite3.OperationalError):
        read_model._coverage(_FailingPatients(db.db), [], None)


def test_artifact_view_omits_private_replay_proof_without_changing_clinical_content():
    import json

    raw = json.dumps({"summary": "synthetic clinical result",
                      "__mcs_internal_proof_v1": {"answer": "synthetic private replay"}})
    row = {"meta": '{"synthetic":true}', "content": raw}
    meta, content = mcs_view._artifact_json(row)
    assert meta == {"synthetic": True}
    assert content == {"summary": "synthetic clinical result"}
    assert "synthetic private replay" not in json.dumps(content)
    assert json.loads(row["content"])["__mcs_internal_proof_v1"]["answer"] == "synthetic private replay"
