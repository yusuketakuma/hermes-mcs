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
    assert {"owner_pid", "admitted_at", "owner_started"} <= cols


# ---------- owner identity: a reused pid is not the recorded owner ----------

PS_OLD = "ps:Thu Jan 1 00:00:00 1970"
PS_NEW = "ps:Fri Oct 9 01:23:45 2026"
PROC_OLD, PROC_NEW = "proc:1000", "proc:2000"


def _owned(broker, permit_id, started):
    with broker.db:
        broker.db.execute("UPDATE permits SET owner_started=? WHERE permit_id=?",
                          (started, permit_id))


def _probe(monkeypatch, broker, value):
    monkeypatch.setattr(adm, "_proc_started", lambda p: value)
    broker._identity = None        # re-read this process's identity


def test_permits_record_owner_start_identity(broker):
    acq = broker.acquire("mcs.extract", "BACKLOG")
    started = broker._permit(acq["permit_id"])["owner_started"]
    assert started and started == adm._proc_started(os.getpid())
    assert adm._identity_scheme(started) in ("proc", "ps")


@pytest.mark.parametrize("recorded,current", [(PS_OLD, PS_NEW),
                                              (PROC_OLD, PROC_NEW)])
def test_reused_pid_of_expired_owner_is_reclaimed(broker, monkeypatch,
                                                  recorded, current):
    # the recorded owner died and its pid now names this live process
    pid = _sent_rt(broker, os.getpid(), adm.LEASE_S + 1)
    _owned(broker, pid, recorded)
    _probe(monkeypatch, broker, current)
    assert broker.acquire("mcs.extract", "BACKLOG")["admitted"]
    assert _state(broker, pid) == ("fenced", "owner_dead")
    assert not broker.overlap()


def test_reused_pid_of_waiting_rt_releases_waiting_flag(broker, monkeypatch):
    bg = broker.acquire("mcs.extract", "BACKLOG")
    rt = broker.acquire("gbrain.query", "RT")
    assert rt["reason"] == "waiting"
    broker.terminal(bg["permit_id"], "done")
    with broker.db:
        broker.db.execute(
            "UPDATE permits SET created_at=created_at-?, owner_started=?"
            " WHERE permit_id=?", (adm.LEASE_S + 1, PROC_OLD, rt["permit_id"]))
    _probe(monkeypatch, broker, PROC_NEW)
    assert broker.acquire("mcs.extract", "BACKLOG")["admitted"]
    assert broker.status()["rt_waiting"] == 0


@pytest.mark.parametrize("recorded,current,age", [
    (PS_OLD, PS_OLD, adm.LEASE_S * 10),        # still the recorded owner
    (None, PS_NEW, adm.LEASE_S * 10),          # legacy row: identity unknown
    (PS_OLD, None, adm.LEASE_S * 10),          # probe unreadable/ambiguous
    (PS_OLD, PS_NEW, 1),                       # lease not expired
    ("ps:old", PS_NEW, adm.LEASE_S * 10),      # corrupt stored identity
    ("garbage", PROC_NEW, adm.LEASE_S * 10),   # unknown stored scheme
    (PROC_OLD, PS_NEW, adm.LEASE_S * 10),      # cross-scheme: not comparable
    (PS_OLD, PROC_NEW, adm.LEASE_S * 10),
    ("proc:01", PROC_NEW, adm.LEASE_S * 10),   # non-canonical number
])
def test_live_or_unprovable_owner_is_never_reclaimed(broker, monkeypatch,
                                                     recorded, current, age):
    pid = _sent_rt(broker, os.getpid(), age)
    _owned(broker, pid, recorded)
    _probe(monkeypatch, broker, current)
    assert broker.acquire("mcs.extract", "BACKLOG")["reason"] == "rt_pending"
    assert _state(broker, pid) == ("sent", None)


def test_identity_probe_runs_once_per_owner_per_pass(broker, monkeypatch):
    calls = []
    other = 900001
    with broker.db:
        broker.db.executemany(
            "INSERT INTO permits(epoch,client,cls,state,created_at,"
            "owner_pid,owner_started) VALUES(?,?,?,?,?,?,?)",
            [(broker.epoch(), "gbrain.query", "RT", "sent",
              time.time() - adm.LEASE_S - 1, other, PS_OLD)] * 3)
    monkeypatch.setattr(adm, "_pid_alive", lambda p: True)
    monkeypatch.setattr(adm, "_proc_started",
                        lambda p: calls.append(p) or PS_OLD)
    assert broker.acquire("mcs.extract", "BACKLOG")["reason"] == "rt_pending"
    assert calls == [other]


def test_identity_probes_stop_at_the_pass_budget(broker, monkeypatch):
    with broker.db:
        broker.db.executemany(
            "INSERT INTO permits(epoch,client,cls,state,created_at,"
            "owner_pid,owner_started) VALUES(?,?,?,?,?,?,?)",
            [(broker.epoch(), "gbrain.query", "RT", "sent",
              time.time() - adm.LEASE_S - 1, 900000 + i, PS_OLD)
             for i in range(adm.RECLAIM_BATCH)])
    monkeypatch.setattr(adm, "PROBE_BUDGET_S", -1.0)
    monkeypatch.setattr(adm, "_pid_alive", lambda p: True)
    monkeypatch.setattr(adm, "_proc_started", lambda p: pytest.fail("probed"))
    assert broker.acquire("mcs.extract", "BACKLOG")["reason"] == "rt_pending"


def test_forked_handle_records_its_own_identity(broker, monkeypatch):
    broker.acquire("mcs.extract", "BACKLOG")          # caches this pid
    monkeypatch.setattr(adm.os, "getpid", lambda: 424242)
    monkeypatch.setattr(adm, "_proc_started", lambda p: f"proc:{p}")
    acq = broker.acquire("mcs.extract", "BACKLOG")
    row = broker._permit(acq["permit_id"])
    assert (row["owner_pid"], row["owner_started"]) == (424242, "proc:424242")


def _stat(pid=123, comm=b"a b) c", start=b"22", pad=60):
    fields = [b"S"] + [str(i).encode() for i in range(4, pad)]
    if len(fields) > 19:
        fields[19] = start
    return str(pid).encode() + b" (" + comm + b") " + b" ".join(fields)


def test_proc_stat_start_parsing_is_strict():
    assert adm._stat_start(_stat(), 123) == "proc:22"
    assert adm._stat_start(_stat(), 124) is None            # other pid
    assert adm._stat_start(_stat(start=b"2x"), 123) is None
    assert adm._stat_start(_stat(start=b"022"), 123) is None
    assert adm._stat_start(_stat(start=b"9" * 21), 123) is None
    assert adm._stat_start(_stat(pad=20), 123) is None       # truncated
    assert adm._stat_start(b"123 (x S 4 5", 123) is None    # no ')'
    assert adm._stat_start(b"garbage", 123) is None
    assert adm._stat_start(b"\xff" + _stat(), 123) is None


def _ps(monkeypatch, stdout, returncode=0):
    class Done:
        pass
    done = Done()
    done.stdout, done.returncode = stdout, returncode
    monkeypatch.setattr(adm.os.path, "exists", lambda p: False)
    monkeypatch.setattr(adm.subprocess, "run", lambda *a, **k: done)


@pytest.mark.parametrize("stdout,token", [
    (b"Thu Jan  1 00:00:00 1970\n", PS_OLD),
    (b"Fri Oct  9 01:23:45 2026\n", PS_NEW),
    (b"Sat Feb 29 23:59:59 2020\n", "ps:Sat Feb 29 23:59:59 2020"),
])
def test_ps_lstart_accepts_only_the_fixed_c_utc_form(monkeypatch, stdout, token):
    _ps(monkeypatch, stdout)
    assert adm._proc_started(4321) == token


@pytest.mark.parametrize("stdout,returncode", [
    (b"", 0),
    (b"Thu Jan  1 00:00:00 1970\n", 1),                    # ps failed
    (b"Thu Jan 32 00:00:00 1970\n", 0),                    # invalid day
    (b"Fri Feb 29 00:00:00 2019\n", 0),                    # not a leap year
    (b"Mon Jan  1 00:00:00 1970\n", 0),                    # weekday mismatch
    (b"Thu Jan  1 24:00:00 1970\n", 0),                    # invalid hour
    (b"Thu Jan  1 00:00:00 1970 x\n", 0),                  # trailing text
    (b"Thu Jan  1 00:00:00 1970\nThu Jan  1 00:00:00 1970\n", 0),
    (b"Do  1 Jan 00:00:00 1970\n", 0),                     # localized
    ("Thu Jan  1 00:00:00 1970　".encode(), 0),        # non-ASCII
    (b"Thu Jan 1 00:00 1970\n", 0),                        # short
])
def test_ps_lstart_rejects_anything_else(monkeypatch, stdout, returncode):
    _ps(monkeypatch, stdout, returncode)
    assert adm._proc_started(4321) is None


def test_ps_probe_failure_is_none(monkeypatch):
    monkeypatch.setattr(adm.os.path, "exists", lambda p: False)

    def boom(*a, **k):
        raise adm.subprocess.TimeoutExpired("ps", 1)
    monkeypatch.setattr(adm.subprocess, "run", boom)
    assert adm._proc_started(4321) is None


def test_proc_started_of_gone_or_invalid_pid_is_none():
    assert adm._proc_started(_dead_pid()) is None
    for bad in (None, 0, -1, True, "1"):
        assert adm._proc_started(bad) is None


def test_legacy_store_gains_nullable_owner_started(tmp_path):
    import sqlite3
    path = str(tmp_path / "adm.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE permits(permit_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " epoch INTEGER NOT NULL, client TEXT NOT NULL, cls TEXT NOT NULL,"
        " job_gen TEXT, request_id TEXT, state TEXT NOT NULL, token TEXT,"
        " created_at REAL NOT NULL, admitted_at REAL, sent_at REAL,"
        " terminal_at REAL, outcome TEXT, proof TEXT, owner_pid INTEGER)")
    con.execute(
        "INSERT INTO permits(epoch,client,cls,state,created_at,admitted_at,"
        "owner_pid) VALUES(1,'mcs.extract','BACKLOG','terminal',1,1,1)")
    con.commit()
    con.close()
    b = adm.Broker(path)
    try:
        cols = {r[1] for r in b.db.execute("PRAGMA table_info(permits)")}
        assert "owner_started" in cols
        assert b.db.execute(
            "SELECT owner_started FROM permits").fetchone()[0] is None
    finally:
        b.close()
