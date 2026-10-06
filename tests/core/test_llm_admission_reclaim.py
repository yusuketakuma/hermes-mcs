"""Dead-owner permit reclamation and bounded terminal pruning (synthetic store)."""
import os
import subprocess
import sys
import time

import pytest

import llm_admission as adm


@pytest.fixture
def broker(tmp_path):
    b = adm.Broker(str(tmp_path / "adm.db"))
    b.open_epoch(lambda: True)
    yield b
    b.close()


def _dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()   # reaped: the pid no longer exists
    return child.pid


def _sent_rt(broker, owner_pid, age_s):
    acq = broker.acquire("gbrain.query", "RT")
    assert broker.sent(acq["permit_id"])["sent"]
    with broker.db:
        broker.db.execute(
            "UPDATE permits SET owner_pid=?, created_at=created_at-?,"
            " admitted_at=admitted_at-?, sent_at=sent_at-? WHERE permit_id=?",
            (owner_pid, age_s, age_s, age_s, acq["permit_id"]))
    return acq["permit_id"]


def _state(broker, pid):
    row = broker._permit(pid)
    return row["state"], row["outcome"]


def test_permits_record_owner_pid(broker):
    acq = broker.acquire("mcs.extract", "BACKLOG")
    assert broker._permit(acq["permit_id"])["owner_pid"] == os.getpid()


def test_expired_permit_of_dead_owner_is_reclaimed(broker):
    pid = _sent_rt(broker, _dead_pid(), adm.LEASE_S + 1)
    bg = broker.acquire("mcs.extract", "BACKLOG")
    assert bg["admitted"]
    assert _state(broker, pid) == ("fenced", "owner_dead")
    assert not broker.overlap()


@pytest.mark.parametrize("live", [True, False])
def test_live_owner_or_unexpired_lease_is_never_reclaimed(broker, live):
    owner, age = (os.getpid(), adm.LEASE_S * 10) if live else (_dead_pid(), 1)
    pid = _sent_rt(broker, owner, age)
    assert broker.acquire("mcs.extract", "BACKLOG")["reason"] == "rt_pending"
    assert _state(broker, pid) == ("sent", None)


def test_unrecorded_owner_is_never_reclaimed(broker):
    pid = _sent_rt(broker, None, adm.LEASE_S * 10)
    assert broker.acquire("mcs.extract", "BACKLOG")["reason"] == "rt_pending"
    assert _state(broker, pid)[0] == "sent"


def test_dead_owner_waiting_rt_releases_waiting_flag(broker):
    bg = broker.acquire("mcs.extract", "BACKLOG")
    rt = broker.acquire("gbrain.query", "RT")
    assert rt["reason"] == "waiting"
    broker.terminal(bg["permit_id"], "done")
    with broker.db:
        broker.db.execute(
            "UPDATE permits SET owner_pid=?, created_at=created_at-? WHERE permit_id=?",
            (_dead_pid(), adm.LEASE_S + 1, rt["permit_id"]))
    assert broker.acquire("mcs.extract", "BACKLOG")["admitted"]
    assert broker.status()["rt_waiting"] == 0


def test_terminal_pruning_is_bounded_and_keeps_recent_and_occupying(broker):
    old = time.time() - adm.RETENTION_S - 60
    with broker.db:
        epoch = broker.epoch()
        broker.db.execute(
            "INSERT INTO permits(epoch,client,cls,state,created_at,owner_pid)"
            " VALUES(?,?,?,?,?,?)", (epoch, "mcs.extract", "BACKLOG", "unknown",
                                     old, os.getpid()))
        broker.db.executemany(
            "INSERT INTO permits(epoch,client,cls,state,created_at,terminal_at)"
            " VALUES(?,?,?,?,?,?)",
            [(epoch, "mcs.extract", "BACKLOG", "terminal", old, old)] * 250
            + [(epoch, "mcs.extract", "BACKLOG", "fenced", old, None)] * 10
            + [(epoch, "mcs.extract", "BACKLOG", "terminal", old, time.time())] * 5)

    def count(where):
        return broker.db.execute(f"SELECT COUNT(*) FROM permits WHERE {where}").fetchone()[0]

    broker.acquire("gbrain.query", "RT")
    # the head window of PRUNE_BATCH rows holds the live row + 99 old terminals
    assert count("state='terminal'") == 255 - (adm.PRUNE_BATCH - 1)
    for _ in range(5):
        broker.acquire("gbrain.query", "RT")
    assert count("state IN ('terminal','fenced')") == 5
    assert count("state='unknown'") == 1


def test_concurrent_init_migrates_a_legacy_store_once(tmp_path, monkeypatch):
    """Two joiners (an old broker holds the shared owner lock) migrating
    the same legacy store: the second waits for the first's write
    transaction instead of failing on a duplicate ALTER."""
    import fcntl
    import sqlite3
    import threading
    path = str(tmp_path / "adm.db")
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE permits(permit_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " epoch INTEGER NOT NULL, client TEXT NOT NULL, cls TEXT NOT NULL,"
        " job_gen TEXT, request_id TEXT, state TEXT NOT NULL, token TEXT,"
        " created_at REAL NOT NULL, sent_at REAL, terminal_at REAL,"
        " outcome TEXT, proof TEXT)")
    legacy.commit()
    legacy.close()
    old = os.open(path + ".owner.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(old, fcntl.LOCK_SH)          # a still-running broker
    paused, release = threading.Event(), threading.Event()
    real = adm._connect

    class Paused:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *args):
            cur = self._conn.execute(sql, *args)
            if sql.startswith("PRAGMA table_info"):
                paused.set()
                release.wait(10)
            return cur

    calls = []
    monkeypatch.setattr(adm, "_connect", lambda p: (
        calls.append(p), Paused(real(p)) if len(calls) == 1 else real(p))[1])
    errors, brokers = [], []

    def init():
        try:
            brokers.append(adm.Broker(path))
        except Exception as e:      # noqa: BLE001 — surfaced below
            errors.append(e)
    first = threading.Thread(target=init)
    first.start()
    assert paused.wait(10)
    second = threading.Thread(target=init)
    second.start()
    time.sleep(0.5)                 # the joiner reaches the store first
    release.set()
    first.join(20)
    second.join(20)
    os.close(old)
    for b in brokers:
        b.close()
    assert errors == [] and len(brokers) == 2
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(permits)")}
    con.close()
    assert {"owner_pid", "admitted_at"} <= cols
