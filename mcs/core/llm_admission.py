"""Cross-client RT/BACKLOG admission boundary for the shared local
inference backend (T20).

One broker ahead of the shared llama.cpp backend enforces the
invariant ``O_BACKLOG * O_RT = 0`` at all times, where O counts every
authorized request from **pre-send reservation through confirmed
backend terminal retirement** — server-queued, cancel-pending and
response-unknown requests included.

Design contract:

- **Durable epoch** — a monotonically increasing integer in its own
  SQLite store. A broker restart with any non-terminal permit (or a
  dirty epoch flag) rotates the epoch and stays CLOSED until the
  backend is re-established known-empty; old-epoch permits are fenced
  and can never admit again.
- **Class from route, not payload** — a client authenticates as a
  named route registered with a fixed class; a caller-supplied class
  value can never grant RT. Unknown routes are rejected.
- **Permit lifecycle** — ``reserved → admitted → sent → terminal``
  with the honest side states ``waiting`` (RT registered, backlog
  draining), ``unknown`` (completion unproven — still occupies its
  class), and ``cancel_pending`` (a cancel verb is not an ack).
  Nothing leaves a class's occupancy set except a confirmed terminal
  retirement or an epoch fence.
- **Fail closed** — transport timeout, socket drop, expired lease and
  ``/slots`` idle samples are NEVER proof of backend retirement.
  Unknown completions hold admission closed until reconciled with
  backend evidence. The one exception is a hard-killed owner: a permit
  whose lease (``LEASE_S``, well past any client deadline) expired AND
  whose recording process no longer exists (its socket closed with it)
  is fenced as ``owner_dead`` during acquisition, so a SIGKILL'd
  request cannot hold its class while other handles keep the store
  open. A live owner's permit is never reclaimed.
- **Admission token** — each admitted→sent permit mints a single-use
  ``epoch:permit:nonce`` token. The backend access control (in
  production: the backend reachable only through the broker; in the
  synthetic harness: the fake backend) rejects any request without a
  live token — direct POSTs, stale-epoch replays and cross-class
  tokens all fail closed.
- **Protected BG window** — an RT arrival registers waiting intent and
  closes new BACKLOG admission atomically, then waits for in-flight
  background work to reach a confirmed terminal state (bounded by the
  permit deadline). RT never overlaps; a continuously busy RT stream
  delays backlog and ``status()`` exposes the oldest backlog age as a
  capacity signal — backlog progress is never claimed unconditionally.
- **Default off** — MCS call paths route through the broker only when
  ``MCS_LLM_ADMISSION`` is set. Until a staged rollout proves
  direct-connection rejection on the deployed topology, the legacy
  slot-pinning path stays the default.
"""
from __future__ import annotations

import hashlib
import fcntl
import os
import sqlite3
import time
from contextlib import contextmanager
from functools import wraps
from threading import RLock

CLASSES = ("RT", "BACKLOG")

# registered routes -> class. A client may only claim the class bound
# to its route at registration time; the broker rejects anything else.
DEFAULT_ROUTES = {
    # MCS background work — new-arrival extraction, semantic drain
    # (S1/S3/S6), QC probes, backfill: all BACKLOG.
    "mcs.extract": "BACKLOG",
    "mcs.semantic": "BACKLOG",
    "mcs.qc": "BACKLOG",
    "mcs.bench": "BACKLOG",
    # Hermes surfaces — interactive is RT; cron/delegated/aux work is
    # background. Auxiliary hooks are observer-only and never an
    # enforcing gate, so they get no route at all.
    "hermes.interactive": "RT",
    "hermes.cron": "BACKLOG",
    "hermes.delegated": "BACKLOG",
    # GBrain queries are user-facing → RT.
    "gbrain.query": "RT",
}

# permit states that still occupy backend capacity for their class
OCCUPYING = ("reserved", "admitted", "sent", "unknown",
             "cancel_pending")
TERMINAL = ("terminal", "fenced")
# Occupancy lease: 3x the longest client timeout (semantic LLM_TIMEOUT
# 600s). Only combined with a dead owner process is expiry acted on.
LEASE_S = 1800.0
# Terminal rows older than this are pruned, a bounded batch per acquire.
RETENTION_S = 7 * 86400.0
PRUNE_BATCH = 100
RECLAIM_BATCH = 20


def _pid_alive(pid) -> bool:
    """False only when no process with ``pid`` exists (ESRCH)."""
    if not isinstance(pid, int) or pid <= 0:
        return True     # unrecorded owner: never provably dead
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True     # EPERM: exists under another user
    return True


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=delete")
        conn.execute("PRAGMA busy_timeout=30000")
    except BaseException:
        conn.close()
        raise
    return conn


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        # Cached handles serve transport threads; only broker state work
        # is serialized, never an inference call or its waiting period.
        with self._mutex:
            return method(self, *args, **kwargs)
    return call


class Broker:
    """The admission broker. Owns its own SQLite store so a ledger
    restore cannot resurrect retired permits into a live epoch."""

    def __init__(self, path: str, routes: dict | None = None,
                 bg_window_s: float = 120.0, slots: int = 2):
        self._mutex = RLock()
        self.path = path
        self.routes = dict(routes or DEFAULT_ROUTES)
        self.bg_window_s = bg_window_s
        # T19: same-class concurrency is bounded by the measured slot
        # count — beyond it a permit defers ('class_full'), never
        # queues silently past the backend's parallel width
        self.slots = max(1, int(slots))
        # A fresh process owns epoch recovery only when no other broker
        # handle is alive. Shared lifetime locks let other clients join
        # the current epoch without fencing an in-flight request.
        self._owner_fd = os.open(path + ".owner.lock",
                                 os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(self._owner_fd,
                            fcntl.LOCK_EX | fcntl.LOCK_NB)
                recover = True
            except BlockingIOError:
                fcntl.flock(self._owner_fd, fcntl.LOCK_SH)
                recover = False
            self.db = _connect(path)
            try:
                self._initialize_store()
                if recover:
                    self._recover()
                    fcntl.flock(self._owner_fd, fcntl.LOCK_SH)
            except BaseException:
                self.db.close()
                raise
        except BaseException:
            os.close(self._owner_fd)
            raise
        self._closed = False

    def _initialize_store(self) -> None:
        """Create or migrate the broker's private durable store."""
        # one write transaction: two processes starting together must not
        # both see owner_pid missing and race the ALTER (duplicate column)
        with self._write_tx():
            self.db.execute("""
              CREATE TABLE IF NOT EXISTS admission_meta(
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                epoch INTEGER NOT NULL,
                state TEXT NOT NULL,
                rt_waiting INTEGER NOT NULL DEFAULT 0,
                rotated_at REAL)""")
            self.db.execute("""
              CREATE TABLE IF NOT EXISTS permits(
                permit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                epoch INTEGER NOT NULL,
                client TEXT NOT NULL,
                cls TEXT NOT NULL,
                job_gen TEXT,
                request_id TEXT,
                state TEXT NOT NULL,
                token TEXT,
                created_at REAL NOT NULL,
                admitted_at REAL,
                sent_at REAL,
                terminal_at REAL,
                outcome TEXT,
                proof TEXT,
                owner_pid INTEGER)""")
            cols = {r[1] for r in self.db.execute(
                "PRAGMA table_info(permits)")}
            if "owner_pid" not in cols:
                # pre-existing rows keep NULL: their owner is unprovable
                self.db.execute(
                    "ALTER TABLE permits ADD COLUMN owner_pid INTEGER")
            if "admitted_at" not in cols:
                self.db.execute(
                    "ALTER TABLE permits ADD COLUMN admitted_at REAL")
                # backend occupancy began at creation for every permit that
                # ever left 'waiting' — 'waiting' itself holds no slot
                self.db.execute(
                    "UPDATE permits SET admitted_at=created_at "
                    "WHERE state NOT IN ('waiting')")
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS permits_epoch_state "
                "ON permits(epoch,state)")

    # ---------- epoch lifecycle ----------

    def _meta(self) -> sqlite3.Row:
        return self.db.execute(
            "SELECT epoch,state,rt_waiting FROM admission_meta "
            "WHERE singleton=1").fetchone()

    @contextmanager
    def _write_tx(self):
        """Lock the store before reading a state used for admission.

        ``with sqlite3.Connection`` commits writes but does not begin a
        transaction for a preceding SELECT. Two clients could otherwise
        both observe an empty opposite class and reserve it.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _recover(self) -> None:
        """Rotate the epoch when the previous process exited with any
        live permit — a stale pre-rotation request must find only a
        fenced world. The new epoch opens CLOSED: an operator (or a
        verified-empty backend probe) must call ``open_epoch``."""
        row = self._meta()
        with self.db:
            if row is None:
                self.db.execute(
                    "INSERT INTO admission_meta"
                    "(singleton,epoch,state,rt_waiting,rotated_at)"
                    " VALUES(1,1,'closed',0,?)", (time.time(),))
                return
            live = self.db.execute(
                "SELECT COUNT(*) FROM permits WHERE epoch=? AND state "
                "IN ('reserved','admitted','sent','unknown',"
                "'cancel_pending','waiting')",
                (row["epoch"],)).fetchone()[0]
            if live or row["state"] == "dirty":
                new_epoch = row["epoch"] + 1
                self.db.execute(
                    "UPDATE permits SET state='fenced',"
                    " outcome='epoch_fence' WHERE epoch=? AND state IN"
                    " ('reserved','admitted','sent','unknown',"
                    "'cancel_pending','waiting')", (row["epoch"],))
                self.db.execute(
                    "UPDATE admission_meta SET epoch=?,"
                    " state='closed', rt_waiting=0, rotated_at=?"
                    " WHERE singleton=1", (new_epoch, time.time()))

    @_serialized
    def open_epoch(self, verify_empty) -> bool:
        """Reopen the current epoch after the backend is verified
        empty. ``verify_empty`` is a callable returning True only when
        the caller has REAL evidence the backend holds no work — a
        one-shot ``/slots`` idle sample is not evidence."""
        if not callable(verify_empty) or not verify_empty():
            return False
        with self.db:
            self.db.execute(
                "UPDATE admission_meta SET state='open'"
                " WHERE singleton=1")
        return True

    @_serialized
    def epoch(self) -> int:
        return self._meta()["epoch"]

    @_serialized
    def is_open(self) -> bool:
        return self._meta()["state"] == "open"

    # ---------- admission ----------

    def _occupancy(self, conn, epoch: int, cls: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM permits WHERE epoch=? AND cls=? "
            f"AND state IN ({','.join('?' * len(OCCUPYING))})",
            (epoch, cls, *OCCUPYING)).fetchone()[0]

    @_serialized
    def overlap(self) -> bool:
        """Invariant check: any moment with both classes occupying."""
        row = self._meta()
        rt = self._occupancy(self.db, row["epoch"], "RT")
        bg = self._occupancy(self.db, row["epoch"], "BACKLOG")
        return rt > 0 and bg > 0

    @_serialized
    def acquire(self, client: str, cls: str, job_gen: str | None = None,
                request_id: str | None = None) -> dict:
        """Reserve a permit. Returns a verdict dict — denial is data,
        never an exception. ``cls`` must equal the class bound to the
        registered ``client`` route: a client-supplied value cannot
        grant RT."""
        bound = self.routes.get(client)
        if bound is None:
            return {"admitted": False, "reason": "unknown_client"}
        if cls != bound:
            return {"admitted": False, "reason": "class_not_bound",
                    "bound_class": bound}
        with self._write_tx():
            self._reclaim_and_prune()
            meta = self._meta()
            if meta["state"] != "open":
                return {"admitted": False, "reason": "epoch_closed",
                        "epoch": meta["epoch"]}
            epoch = meta["epoch"]
            if cls == "BACKLOG":
                # an RT waiting or occupying closes BACKLOG admission
                rt_wait = meta["rt_waiting"]
                rt_occ = self._occupancy(self.db, epoch, "RT")
                if rt_wait or rt_occ:
                    return {"admitted": False, "reason": "rt_pending",
                            "epoch": epoch}
                # bounded concurrent backlog on measured safe slots —
                # excess work defers, never silently overflows width
                if self._occupancy(self.db, epoch, "BACKLOG") \
                        >= self.slots:
                    return {"admitted": False, "reason": "class_full",
                            "epoch": epoch}
            # RT registers waiting intent BEFORE any admission check
            # resolves — the flag itself is the atomic close of new
            # BACKLOG admission
            if cls == "RT":
                bg_occ = self._occupancy(self.db, epoch, "BACKLOG")
                if bg_occ:
                    self.db.execute(
                        "UPDATE admission_meta SET rt_waiting="
                        "rt_waiting+1 WHERE singleton=1")
                    cur = self.db.execute(
                        "INSERT INTO permits(epoch,client,cls,job_gen,"
                        "request_id,state,created_at,owner_pid)"
                        " VALUES(?,?,?,?,?,'waiting',?,?)",
                        (epoch, client, cls, job_gen, request_id,
                         time.time(), os.getpid()))
                    return {"admitted": False, "reason": "waiting",
                            "permit_id": cur.lastrowid,
                            "epoch": epoch}
                # own-class width is capped by the measured slot count
                if self._occupancy(self.db, epoch, "RT") >= self.slots:
                    return {"admitted": False, "reason": "class_full",
                            "epoch": epoch}
                # no BACKLOG occupying — RT goes straight to admitted
                return self._admit(epoch, client, cls, job_gen,
                                   request_id)
            return self._admit(epoch, client, cls, job_gen, request_id)

    def _reclaim_and_prune(self) -> None:
        """Inside the acquire write tx: fence lease-expired permits of
        dead owners, then delete a bounded batch of old terminal rows."""
        now = time.time()
        epoch = self._meta()["epoch"]
        stale = self.db.execute(
            "SELECT permit_id,cls,state,owner_pid FROM permits"
            " WHERE epoch=? AND state IN"
            " ('reserved','admitted','sent','unknown','cancel_pending',"
            "'waiting') AND owner_pid IS NOT NULL"
            " AND COALESCE(sent_at,admitted_at,created_at)<?"
            " ORDER BY permit_id LIMIT ?",
            (epoch, now - LEASE_S, RECLAIM_BATCH)).fetchall()
        # ponytail: pid reuse can make a dead owner look alive (the
        # permit stays until the epoch fence); it never frees a live one.
        for p in stale:
            if _pid_alive(p["owner_pid"]):
                continue
            self.db.execute(
                "UPDATE permits SET state='fenced', outcome='owner_dead',"
                " terminal_at=? WHERE permit_id=?", (now, p["permit_id"]))
            if p["cls"] == "RT" and p["state"] == "waiting":
                self.db.execute(
                    "UPDATE admission_meta SET rt_waiting="
                    "MAX(rt_waiting-1,0) WHERE singleton=1")
        # oldest rows by rowid only — the scan is bounded even when
        # nothing is due
        self.db.execute(
            "DELETE FROM permits WHERE permit_id IN (SELECT permit_id"
            " FROM permits ORDER BY permit_id LIMIT ?) AND state IN"
            f" ({','.join('?' * len(TERMINAL))})"
            " AND COALESCE(terminal_at,created_at)<?",
            (PRUNE_BATCH, *TERMINAL, now - RETENTION_S))

    def _admit(self, epoch, client, cls, job_gen, request_id) -> dict:
        """Insert an 'admitted' permit — created_at == admitted_at since
        the permit never waited."""
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO permits(epoch,client,cls,job_gen,"
            "request_id,state,created_at,admitted_at,owner_pid)"
            " VALUES(?,?,?,?,?,'admitted',?,?,?)",
            (epoch, client, cls, job_gen, request_id, now, now,
             os.getpid()))
        return {"admitted": True, "permit_id": cur.lastrowid,
                "epoch": epoch}

    @_serialized
    def _permit(self, permit_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM permits WHERE permit_id=?",
            (permit_id,)).fetchone()

    @_serialized
    def poll(self, permit_id: int) -> dict:
        """Promote a waiting RT permit once backlog fully drained.
        Returns the permit's live view; a stale-epoch permit is fenced
        and can never re-admit."""
        with self._write_tx():
            p = self._permit(permit_id)
            if p is None:
                return {"state": "unknown_permit"}
            meta = self._meta()
            if p["epoch"] != meta["epoch"]:
                return {"state": "fenced", "epoch": meta["epoch"]}
            if p["state"] == "waiting":
                if self._occupancy(self.db, meta["epoch"],
                                   "BACKLOG") == 0 \
                        and self._occupancy(self.db, meta["epoch"],
                                            "RT") < self.slots:
                    self.db.execute(
                        "UPDATE permits SET state='admitted',"
                        " admitted_at=? WHERE permit_id=? AND"
                        " state='waiting'",
                        (time.time(), permit_id))
                    self.db.execute(
                        "UPDATE admission_meta SET"
                        " rt_waiting=rt_waiting-1 WHERE singleton=1")
                    p = self._permit(permit_id)
            return {"state": p["state"], "epoch": p["epoch"],
                    "cls": p["cls"]}

    @_serialized
    def sent(self, permit_id: int, request_id: str | None = None) -> dict:
        """Transition admitted→sent and mint the backend token. Only a
        current-epoch admitted (or just-promoted waiting) permit may
        send — a stale or never-admitted permit fails closed."""
        p = self._permit(permit_id)
        if p is not None and p["state"] == "waiting":
            self.poll(permit_id)
        with self._write_tx():
            p = self._permit(permit_id)
            if p is None:
                return {"sent": False, "reason": "unknown_permit"}
            meta = self._meta()
            if p["epoch"] != meta["epoch"]:
                return {"sent": False, "reason": "stale_epoch"}
            if p["state"] != "admitted":
                return {"sent": False, "reason": f"state_{p['state']}"}
            # re-verify exclusivity inside the same write tx: an RT
            # token may only exist while no BACKLOG request occupies
            other = "BACKLOG" if p["cls"] == "RT" else "RT"
            if self._occupancy(self.db, meta["epoch"], other):
                return {"sent": False, "reason": "class_overlap"}
            nonce = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
            token = f"{meta['epoch']}:{permit_id}:{nonce}"
            self.db.execute(
                "UPDATE permits SET state='sent', token=?, request_id="
                "COALESCE(?,request_id), sent_at=? WHERE permit_id=?",
                (token, request_id, time.time(), permit_id))
            return {"sent": True, "token": token,
                    "epoch": meta["epoch"]}

    @_serialized
    def token_valid(self, token: str, cls: str,
                    request_id: str | None = None) -> dict:
        """Backend-side access control: the token must be a live 'sent'
        permit of the CURRENT epoch for the claimed class. Single-use
        — a valid token does not become invalid on read; the backend
        retires it via ``terminal``."""
        if not isinstance(token, str):
            return {"ok": False, "reason": "no_token"}
        parts = token.split(":")
        if len(parts) != 3:
            return {"ok": False, "reason": "malformed"}
        try:
            epoch = int(parts[0])
            permit_id = int(parts[1])
        except ValueError:
            return {"ok": False, "reason": "malformed"}
        meta = self._meta()
        if epoch != meta["epoch"]:
            return {"ok": False, "reason": "stale_epoch",
                    "epoch": meta["epoch"]}
        p = self._permit(permit_id)
        if p is None or p["token"] != token:
            return {"ok": False, "reason": "unknown_token"}
        if p["state"] != "sent":
            return {"ok": False, "reason": f"state_{p['state']}"}
        if p["cls"] != cls:
            return {"ok": False, "reason": "class_mismatch"}
        if request_id is not None and p["request_id"] is not None \
                and p["request_id"] != request_id:
            return {"ok": False, "reason": "request_mismatch"}
        return {"ok": True, "permit_id": permit_id, "cls": p["cls"],
                "epoch": epoch}

    @_serialized
    def terminal(self, permit_id: int, outcome: str,
                 proof: str | None = None) -> dict:
        """Confirmed backend terminal retirement — the ONLY way a
        permit leaves its class's occupancy set. ``proof`` is the
        backend-verifiable evidence (the fake backend's completion id;
        in production the broker's verified channel)."""
        with self._write_tx():
            p = self._permit(permit_id)
            if p is None:
                return {"terminal": False, "reason": "unknown_permit"}
            meta = self._meta()
            if p["epoch"] != meta["epoch"]:
                return {"terminal": False, "reason": "stale_epoch"}
            if p["state"] == "terminal":
                return {"terminal": True}
            self.db.execute(
                "UPDATE permits SET state='terminal', outcome=?,"
                " proof=?, terminal_at=? WHERE permit_id=?",
                (outcome, proof, time.time(), permit_id))
            if p["cls"] == "RT" and p["state"] == "waiting":
                self.db.execute(
                    "UPDATE admission_meta SET rt_waiting="
                    "rt_waiting-1 WHERE singleton=1")
            return {"terminal": True}

    def _live_permit(self, permit_id: int):
        """(permit, error) — error is the result dict to return when the
        verb may not proceed (unknown permit, stale epoch, or an
        already-terminal state reported as ok)."""
        p = self._permit(permit_id)
        if p is None:
            return None, {"ok": False, "reason": "unknown_permit"}
        if p["epoch"] != self._meta()["epoch"]:
            return p, {"ok": False, "reason": "stale_epoch"}
        if p["state"] in TERMINAL:
            return p, {"ok": True, "state": p["state"]}
        return p, None

    @_serialized
    def cancel(self, permit_id: int) -> dict:
        """A cancel verb is NOT an acknowledgement — the permit moves
        to cancel_pending and keeps occupying its class until a
        confirmed terminal lands."""
        with self._write_tx():
            p, err = self._live_permit(permit_id)
            if err:
                return err
            if p["state"] == "waiting":
                # never sent — cancelling a waiting intent retires it and
                # releases the RT-waiting flag immediately
                self.db.execute(
                    "UPDATE permits SET state='terminal',"
                    " outcome='cancelled_waiting', terminal_at=?"
                    " WHERE permit_id=?", (time.time(), permit_id))
                if p["cls"] == "RT":
                    self.db.execute(
                        "UPDATE admission_meta SET rt_waiting="
                        "rt_waiting-1 WHERE singleton=1")
                return {"ok": True, "state": "terminal"}
            self.db.execute(
                "UPDATE permits SET state='cancel_pending'"
                " WHERE permit_id=? AND state IN"
                " ('admitted','sent','unknown')", (permit_id,))
            return {"ok": True, "state": "cancel_pending"}

    @_serialized
    def mark_unknown(self, permit_id: int,
                     reason: str | None = None) -> dict:
        """Completion unproven (timeout, socket drop, vanished
        response) — the permit stays occupied. The class remains
        closed to the other class until reconciled with backend
        evidence."""
        with self._write_tx():
            p, err = self._live_permit(permit_id)
            if err:
                return err
            self.db.execute(
                "UPDATE permits SET state='unknown', outcome=?"
                " WHERE permit_id=?", (reason or "unknown", permit_id))
            if p["cls"] == "RT" and p["state"] == "waiting":
                # leaving ``waiting`` — terminal() only decrements from
                # that state, so the flag must be released here
                self.db.execute(
                    "UPDATE admission_meta SET rt_waiting="
                    "rt_waiting-1 WHERE singleton=1")
            return {"ok": True, "state": "unknown"}

    # ---------- observability ----------

    @_serialized
    def status(self) -> dict:
        meta = self._meta()
        occ = {}
        for cls in CLASSES:
            occ[cls] = self._occupancy(self.db, meta["epoch"], cls)
        oldest_bg = self.db.execute(
            "SELECT MIN(created_at) FROM permits WHERE epoch=? "
            f"AND cls='BACKLOG' AND state IN "
            f"({','.join('?' * len(OCCUPYING))})",
            (meta["epoch"], *OCCUPYING)).fetchone()[0]
        return {"epoch": meta["epoch"], "state": meta["state"],
                "rt_waiting": meta["rt_waiting"],
                "occupying": occ,
                "overlap": bool(occ["RT"] and occ["BACKLOG"]),
                "oldest_backlog_age_s": (
                    max(0.0, time.time() - oldest_bg)
                    if oldest_bg is not None else None)}

    @_serialized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.db.close()
        finally:
            os.close(self._owner_fd)
