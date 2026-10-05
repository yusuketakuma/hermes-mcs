"""Regression: preupdate backup survives prune before it is recorded, and a
failed backup never strands a partial *.db.tmp copy."""
import json
import os
import sqlite3
import time

import pytest

import ledger
import maintenance


def _source(tmp_path):
    src = tmp_path / "ledger.db"
    db = ledger.Ledger(str(src))
    db.ensure_patient(1)
    db.close()
    return str(src)


def test_unrecorded_fresh_preupdate_backup_survives_prune(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    state = tmp_path / "update_state.json"
    state.write_text(json.dumps({"applied": [], "applying": None}))
    fresh = maintenance.preupdate_backup(_source(tmp_path))
    stale = backups / "preupdate-old.db"
    stale.write_bytes(b"x")
    old = time.time() - maintenance.PREUPDATE_GRACE_S - 60
    os.utime(stale, (old, old))
    assert maintenance.prune_preupdate_backups(str(state)) == 1
    assert os.path.exists(fresh)
    assert not stale.exists()


@pytest.mark.parametrize("fn", ["preupdate_backup", "daily_backup"])
def test_failed_backup_removes_tmp(tmp_path, monkeypatch, fn):
    backups = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    src = _source(tmp_path)
    real_connect = sqlite3.connect

    class Boom:
        def __init__(self, conn):
            self.conn = conn

        def backup(self, dst):
            raise sqlite3.OperationalError("database or disk is full")

        def close(self):
            self.conn.close()

    def connect(path, *a, **kw):
        conn = real_connect(path, *a, **kw)
        return Boom(conn) if kw.get("uri") else conn

    monkeypatch.setattr(maintenance.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError):
        getattr(maintenance, fn)(src)
    assert [p for p in os.listdir(backups) if p.endswith(".tmp")] == []
