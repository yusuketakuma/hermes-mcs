"""Cross-client RT/BACKLOG admission boundary for the shared local
inference backend (T20).

Legacy callers retain ``O_BACKLOG * O_RT = 0``. Scheduled MCS callers
may mix classes only with distinct physical slots held by the transport.
Every permit counts from pre-send reservation through confirmed backend
terminal retirement, including queued, cancel-pending and unknown work.

Design contract:

- **Durable epoch** — a monotonically increasing integer in its own
  SQLite store. A broker restart with any non-terminal permit, an
  unproven owner-death fence, or a dirty epoch flag rotates the epoch
  and stays CLOSED until a trusted caller confirms the backend is empty;
  old-epoch permits are fenced and can never admit again.
- **Class from route, not payload** — the broker looks up a named route
  registered with a fixed class; a caller-supplied class value can never
  grant RT. Unknown routes are rejected. Caller authentication must be
  enforced separately by the deployment boundary.
- **Permit lifecycle** — ``admitted → sent → terminal`` (the legacy
  ``reserved`` state is never written but still counts as occupying)
  with the honest side states ``waiting`` (RT registered, backlog
  draining), ``unknown`` (completion unproven — still occupies its
  class), and ``cancel_pending`` (a cancel verb is not an ack).
  Nothing leaves a class's occupancy set except a confirmed terminal
  retirement or an epoch fence.
- **Fail closed** — transport timeout, socket drop, expired lease and
  ``/slots`` idle samples are NEVER proof of backend retirement.
  Unknown completions hold admission closed until reconciled with
  backend evidence. Owner death or PID replacement cannot retire an
  already-dispatched request: the server may still have it queued.
  Only lease-expired ``reserved``/``admitted``/``waiting`` permits with
  neither a send timestamp nor a token may be fenced as ``owner_dead``
  after confirmed owner loss. A live owner's permit is never reclaimed.
- **Admission token** — each admitted→sent permit mints an
  ``epoch:permit:nonce`` token bound to its live permit and class.
  Validation rejects stale epochs and cross-class tokens but does not
  consume a valid token. Broker-only backend reachability is a separate
  deployment requirement, not a property established by this module.
- **Protected BG window** — an RT arrival registers waiting intent and
  closes new BACKLOG admission atomically, then waits for in-flight
  background work to reach a confirmed terminal state (bounded by the
  permit deadline). RT never overlaps; a continuously busy RT stream
  delays backlog and ``status()`` exposes the oldest backlog age as a
  capacity signal — backlog progress is never claimed unconditionally.
- **MCS physical slots** — only the scheduled transport supplies a slot,
  after acquiring its flock. Unbound legacy permits block this mode;
  bound unknown permits keep their slot. Neither expiry nor owner loss
  retires sent work. The default epoch-opening contract is unchanged.
- **Default off** — MCS call paths route through the broker only when
  config.json ``local_llm.admission`` is true or, with that key unset,
  ``MCS_LLM_ADMISSION`` is set. Until a staged rollout proves
  direct-connection rejection on the deployed topology, the legacy
  slot-pinning path stays the default.
"""
from __future__ import annotations

import datetime
import hashlib
import fcntl
import os
import re
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from functools import wraps
from threading import RLock

CLASSES = ("RT", "BACKLOG")

# registered routes -> class. A client may only claim the class bound
# to its route at registration time; the broker rejects anything else.
DEFAULT_ROUTES = {
    # Calls remain BACKLOG unless the explicit MCS new-arrival scope
    # maps them to mcs.realtime while owning a physical slot.
    "mcs.extract": "BACKLOG",
    "mcs.semantic": "BACKLOG",
    "mcs.qc": "BACKLOG",
    "mcs.bench": "BACKLOG",
    "mcs.realtime": "RT",
    # Hermes surfaces — interactive is RT; cron/delegated/aux work is
    # background. Auxiliary hooks are observer-only and never an
    # enforcing gate, so they get no route at all.
    "hermes.interactive": "RT",
    "hermes.cron": "BACKLOG",
    "hermes.delegated": "BACKLOG",
    # GBrain queries are user-facing → RT.
    "gbrain.query": "RT",
}

# permit states that still occupy backend capacity for their class.
# 'reserved' is never written now; it stays so rows an older broker may
# have left behind are still counted and fenced.
OCCUPYING = ("reserved", "admitted", "sent", "unknown",
             "cancel_pending")
# every non-terminal state: OCCUPYING plus RT 'waiting' intent
LIVE = OCCUPYING + ("waiting",)
_LIVE_IN = f"({','.join('?' * len(LIVE))})"
TERMINAL = ("terminal", "fenced")
# Occupancy lease exceeds semantic_policy's maximum 1260 s job budget.
# Expiry reclaims only proven-unsent permits of a dead or replaced owner.
LEASE_S = 1800.0
# Terminal rows older than this are pruned, a bounded batch per acquire.
RETENTION_S = 7 * 86400.0
PRUNE_BATCH = 100
RECLAIM_BATCH = 20
# Completion-unproven states stay occupying until confirmed terminal
# retirement or an existing fence. The hold only scopes reconciliation counts.
UNRESOLVED = ("unknown", "cancel_pending")
UNKNOWN_HOLD_S = LEASE_S


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


# Owner start identity separates a reused pid from the recorded owner.
# ps runs with a fixed locale/zone so the same process always yields the
# same token; each probe is bounded and a whole reclaim pass is capped.
_PS_ENV = {"PATH": "/bin:/usr/bin", "LC_ALL": "C", "TZ": "UTC"}
PROBE_TIMEOUT_S = 1.0
PROBE_BUDGET_S = 2.0


_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_TICKS = re.compile(r"0|[1-9][0-9]{0,19}", re.ASCII)
_PROC_TOKEN = re.compile(r"proc:(?:0|[1-9][0-9]{0,19})", re.ASCII)
# raw C-locale ``lstart``: day of month space-padded to two columns
_PS_LSTART = re.compile(
    r"[A-Z][a-z]{2} [A-Z][a-z]{2} [ 1-3][0-9] [0-9]{2}:[0-9]{2}:[0-9]{2}"
    r" [0-9]{4}", re.ASCII)
_PS_TOKEN = re.compile(
    r"ps:([A-Z][a-z]{2}) ([A-Z][a-z]{2}) ([1-9]|[12][0-9]|3[01])"
    r" (?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9] ([0-9]{4})", re.ASCII)


def _identity_scheme(token) -> str | None:
    """``"proc"`` or ``"ps"`` for a well-formed start identity token;
    None for anything else (legacy, corrupt or unknown scheme)."""
    if not isinstance(token, str):
        return None
    if _PROC_TOKEN.fullmatch(token):
        return "proc"
    m = _PS_TOKEN.fullmatch(token)
    if m is None or m[2] not in _MONTHS:
        return None
    try:
        day = datetime.date(int(m[4]), _MONTHS.index(m[2]) + 1, int(m[3]))
    except ValueError:
        return None
    return "ps" if _DAYS[day.weekday()] == m[1] else None


def _stat_start(raw: bytes, pid: int) -> str | None:
    """Start time (field 22, clock ticks since boot) of ``pid``'s Linux
    ``/proc/<pid>/stat`` record, or None when the record is malformed."""
    head = f"{pid} (".encode("ascii")
    _, sep, tail = raw.rpartition(b")")
    fields = tail.split()
    if not raw.startswith(head) or not sep or len(fields) < 20:
        return None
    try:
        ticks = fields[19].decode("ascii")
    except UnicodeDecodeError:
        return None
    return "proc:" + ticks if _TICKS.fullmatch(ticks) else None


def _proc_started(pid) -> str | None:
    """Kernel start identity of ``pid`` — None whenever it cannot be read
    unambiguously (gone, unreadable, malformed or timed out)."""
    if type(pid) is not int or pid <= 0:
        return None
    if os.path.exists("/proc/self/stat"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                return _stat_start(handle.read(4096), pid)
        except OSError:
            return None
    try:
        out = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                             env=_PS_ENV, capture_output=True,
                             timeout=PROBE_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not isinstance(out.stdout, bytes):
        return None
    try:
        text = out.stdout.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not _PS_LSTART.fullmatch(text):
        return None
    token = "ps:" + " ".join(text.split())
    return token if _identity_scheme(token) == "ps" else None


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

    def __init__(self, path: str, routes: dict | None = None, slots: int = 2):
        self._mutex = RLock()
        self.path = path
        self.routes = dict(routes or DEFAULT_ROUTES)
        # T19: same-class concurrency is bounded by the measured slot
        # count — beyond it a permit defers ('class_full'), never
        # queues silently past the backend's parallel width
        self.slots = max(1, int(slots))
        self._identity = None       # (pid, start identity) of this process
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
                else:
                    # A live owner avoids full recovery, not unsafe-fence checks.
                    with self._write_tx():
                        pass
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
        with self._write_tx(check_fences=False):
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
                owner_pid INTEGER,
                owner_started TEXT)""")
            cols = {r[1] for r in self.db.execute(
                "PRAGMA table_info(permits)")}
            if "owner_pid" not in cols:
                # pre-existing rows keep NULL: their owner is unprovable
                self.db.execute(
                    "ALTER TABLE permits ADD COLUMN owner_pid INTEGER")
            if "owner_started" not in cols:
                # NULL keeps the pid-only liveness verdict for old rows
                self.db.execute(
                    "ALTER TABLE permits ADD COLUMN owner_started TEXT")
            if "admitted_at" not in cols:
                self.db.execute(
                    "ALTER TABLE permits ADD COLUMN admitted_at REAL")
                # backend occupancy began at creation for every permit that
                # ever left 'waiting' — 'waiting' itself holds no slot
                self.db.execute(
                    "UPDATE permits SET admitted_at=created_at "
                    "WHERE state NOT IN ('waiting')")
            if "physical_slot" not in cols:
                # Old permits remain unbound and block physical-slot admission.
                self.db.execute("ALTER TABLE permits ADD COLUMN physical_slot INTEGER")
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS permits_epoch_state "
                "ON permits(epoch,state)")

    # ---------- epoch lifecycle ----------

    def _meta(self) -> sqlite3.Row:
        return self.db.execute(
            "SELECT epoch,state,rt_waiting FROM admission_meta "
            "WHERE singleton=1").fetchone()

    @contextmanager
    def _write_tx(self, *, check_fences=True):
        """Lock the store before reading a state used for admission.

        ``with sqlite3.Connection`` commits writes but does not begin a
        transaction for a preceding SELECT. Two clients could otherwise
        both observe an empty opposite class and reserve it.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if check_fences:
                self._fence_unsafe_epoch()
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _rotate_epoch(self, epoch: int) -> None:
        """Fence live permits and close the next epoch inside the caller's tx."""
        self.db.execute(
            "UPDATE permits SET state='fenced',"
            " outcome='epoch_fence' WHERE epoch=? AND state IN"
            f" {_LIVE_IN}", (epoch, *LIVE))
        self.db.execute(
            "UPDATE admission_meta SET epoch=?,"
            " state='closed', rt_waiting=0, rotated_at=?"
            " WHERE singleton=1", (epoch + 1, time.time()))

    def _fence_unsafe_epoch(self) -> None:
        """Close hidden owner-death work before dispatch or pruning in this tx."""
        row = self._meta()
        if row is None:
            return
        # Unsent provenance cannot contradict either persisted send marker.
        unsafe = self.db.execute(
            "SELECT 1 FROM permits WHERE epoch=? AND state='fenced'"
            " AND outcome='owner_dead' AND (proof IS NOT 'owner_dead_unsent'"
            " OR token IS NOT NULL OR sent_at IS NOT NULL) LIMIT 1",
            (row["epoch"],)).fetchone()
        if unsafe:
            self._rotate_epoch(row["epoch"])

    def _recover(self) -> None:
        """Rotate the epoch when the previous process exited with any
        live permit or an owner-death fence without unsent provenance.
        A stale pre-rotation request must find only a fenced world.
        The new epoch opens CLOSED until a trusted caller confirms the
        backend is empty through ``open_epoch``."""
        with self._write_tx():
            row = self._meta()
            if row is None:
                self.db.execute(
                    "INSERT INTO admission_meta"
                    "(singleton,epoch,state,rt_waiting,rotated_at)"
                    " VALUES(1,1,'closed',0,?)", (time.time(),))
                return
            live = self.db.execute(
                "SELECT COUNT(*) FROM permits WHERE epoch=? AND state "
                f"IN {_LIVE_IN}",
                (row["epoch"], *LIVE)).fetchone()[0]
            if live or row["state"] == "dirty":
                self._rotate_epoch(row["epoch"])

    @_serialized
    def open_epoch(self, verify_empty) -> bool:
        """Reopen on the trusted caller's assertion that the backend is empty.

        The broker does not independently verify this callback's result.
        The caller needs evidence that no queued or decoding work remains;
        idle ``/slots`` samples alone do not establish that assertion.
        Fence first, run the callback outside the transaction, and reopen
        only if the epoch still matches that assertion's starting point.
        """
        with self._write_tx():
            epoch = self._meta()["epoch"]
        if not callable(verify_empty) or not verify_empty():
            return False
        with self._write_tx():
            if self._meta()["epoch"] != epoch:
                return False
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
        return rt > 0 and bg > 0 and not self._physical_permits_valid(row["epoch"])

    def _physical_permits_valid(self, epoch):
        slots = [r[0] for r in self.db.execute(
            "SELECT physical_slot FROM permits WHERE epoch=? AND state IN "
            f"({','.join('?' * len(OCCUPYING))})", (epoch, *OCCUPYING))]
        return (all(type(slot) is int and 0 <= slot < self.slots for slot in slots)
                and len(set(slots)) == len(slots))

    @_serialized
    def held_slots(self):
        """Unproven legacy work holds every slot; bound unknown work keeps its slot."""
        with self._write_tx():
            self._reclaim_and_prune()
            meta = self._meta()
            if meta["state"] != "open" or meta["rt_waiting"] or not self._physical_permits_valid(meta["epoch"]):
                return set(range(self.slots))
            return {r[0] for r in self.db.execute(
                "SELECT physical_slot FROM permits WHERE epoch=? AND state IN "
                f"({','.join('?' * len(OCCUPYING))})", (meta["epoch"], *OCCUPYING))}

    @_serialized
    def acquire(self, client: str, cls: str, job_gen: str | None = None,
                request_id: str | None = None, *, physical_slot=None) -> dict:
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
        if physical_slot is not None and (type(physical_slot) is not int or not 0 <= physical_slot < self.slots):
            return {"admitted": False, "reason": "physical_slot_invalid"}
        # A failed identity probe is retryable on the next acquisition.
        if self._identity is not None and self._identity[1] is None:
            self._identity = None
        with self._write_tx():
            meta = self._meta()
            if meta["state"] != "open":
                return {"admitted": False, "reason": "epoch_closed",
                        "epoch": meta["epoch"]}
            self._reclaim_and_prune()
            meta = self._meta()
            epoch = meta["epoch"]
            if physical_slot is not None:
                if not self._physical_permits_valid(epoch) or meta["rt_waiting"]:
                    return {"admitted": False, "reason": "legacy_inflight", "epoch": epoch}
                occupied = self.db.execute("SELECT 1 FROM permits WHERE epoch=? AND physical_slot=? "
                    f"AND state IN ({','.join('?' * len(OCCUPYING))})",
                    (epoch, physical_slot, *OCCUPYING)).fetchone()
                if occupied:
                    return {"admitted": False, "reason": "slot_occupied", "epoch": epoch}
                if _identity_scheme(self._owner()[1]) is None:
                    return {"admitted": False, "reason": "owner_identity_unavailable", "epoch": epoch}
                return self._admit(epoch, client, cls, job_gen, request_id, physical_slot=physical_slot)
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
            if _identity_scheme(self._owner()[1]) is None:
                return {"admitted": False, "reason": "owner_identity_unavailable",
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
                        "request_id,state,created_at,owner_pid,"
                        "owner_started)"
                        " VALUES(?,?,?,?,?,'waiting',?,?,?)",
                        (epoch, client, cls, job_gen, request_id,
                         time.time(), *self._owner()))
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
        """Inside the acquire write tx: fence proven-unsent expired reservations
        of dead owners, then prune a bounded batch of old terminal rows."""
        now = time.time()
        epoch = self._meta()["epoch"]
        stale = self.db.execute(
            "SELECT permit_id,cls,state,owner_pid,owner_started FROM permits"
            " WHERE epoch=? AND state IN ('reserved','admitted','waiting')"
            " AND sent_at IS NULL AND token IS NULL"
            " AND owner_pid IS NOT NULL"
            " AND COALESCE(sent_at,admitted_at,created_at)<?"
            " ORDER BY permit_id LIMIT ?",
            (epoch, now - LEASE_S, RECLAIM_BATCH)).fetchall()
        # A live PID without comparable start identity remains held;
        # owner loss can fence only these proven-unsent reservations.
        probes = {}
        budget = time.monotonic() + PROBE_BUDGET_S
        for p in stale:
            if _pid_alive(p["owner_pid"]) and not self._replaced(
                    p["owner_pid"], p["owner_started"], probes, budget):
                continue
            self.db.execute(
                "UPDATE permits SET state='fenced', outcome='owner_dead',"
                " proof='owner_dead_unsent', terminal_at=? WHERE permit_id=?",
                (now, p["permit_id"]))
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

    def _owner(self) -> tuple:
        """(pid, start identity) recorded on this process's permits —
        re-read after a fork so a child never inherits its parent's."""
        pid = os.getpid()
        if self._identity is None or self._identity[0] != pid:
            self._identity = (pid, _proc_started(pid))
        return self._identity

    def _replaced(self, pid, recorded, probes, budget) -> bool:
        """True only when a live ``pid`` provably belongs to a process
        other than the recorded owner — both identities well-formed in
        the same scheme and different. Each pid is probed at most once
        per pass, and none after the pass budget."""
        scheme = _identity_scheme(recorded)
        if scheme is None:
            return False
        if pid == os.getpid():
            current = self._owner()[1]
        else:
            if pid not in probes:
                probes[pid] = (_proc_started(pid)
                               if time.monotonic() < budget else None)
            current = probes[pid]
        return _identity_scheme(current) == scheme and current != recorded

    def _admit(self, epoch, client, cls, job_gen, request_id, *, physical_slot=None) -> dict:
        """Insert an 'admitted' permit — created_at == admitted_at since
        the permit never waited."""
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO permits(epoch,client,cls,job_gen,"
            "request_id,state,created_at,admitted_at,owner_pid,"
            "owner_started,physical_slot)"
            " VALUES(?,?,?,?,?,'admitted',?,?,?,?,?)",
            (epoch, client, cls, job_gen, request_id, now, now,
             *self._owner(), physical_slot))
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
            if p["physical_slot"] is not None and not self._physical_permits_valid(meta["epoch"]):
                return {"sent": False, "reason": "physical_slot_conflict"}
            if p["physical_slot"] is None and self._occupancy(self.db, meta["epoch"], other):
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
        with self._write_tx():
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

    # ---------- reconciliation ----------

    def _aged_unresolved(self, epoch: int, now: float) -> list:
        return [r["permit_id"] for r in self.db.execute(
            "SELECT permit_id FROM permits WHERE epoch=? AND state IN"
            f" ({','.join('?' * len(UNRESOLVED))})"
            " AND COALESCE(sent_at,admitted_at,created_at)<?"
            " ORDER BY permit_id LIMIT ?",
            (epoch, *UNRESOLVED, now - UNKNOWN_HOLD_S, RECLAIM_BATCH))]

    def reconcile_unknown(self, evidence) -> dict:
        """Report held permits without trusting caller-supplied retirement evidence.

        This compatibility callback has no request-bound verification
        contract. Neither a proof string nor idle slots exclude server-queued
        work, so leave completion-unproven permits to terminal or epoch fences.
        """
        with self._mutex:
            epoch = self._meta()["epoch"]
            candidates = self._aged_unresolved(epoch, time.time())
        if not candidates:
            return {"reconciled": 0}
        return {"reconciled": 0, "held": len(candidates)}

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
        # Unresolved work may still be queued or decoding; expose its held width.
        unresolved, oldest_unresolved = self.db.execute(
            "SELECT COUNT(*), MIN(COALESCE(sent_at,admitted_at,created_at))"
            " FROM permits WHERE epoch=? AND state IN"
            f" ({','.join('?' * len(UNRESOLVED))})",
            (meta["epoch"], *UNRESOLVED)).fetchone()
        return {"epoch": meta["epoch"], "state": meta["state"],
                "rt_waiting": meta["rt_waiting"],
                "occupying": occ,
                "slots": self.slots,
                "unresolved": unresolved,
                "oldest_unresolved_age_s": (
                    max(0.0, time.time() - oldest_unresolved)
                    if oldest_unresolved is not None else None),
                "overlap": bool(occ["RT"] and occ["BACKLOG"]) and not self._physical_permits_valid(meta["epoch"]),
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
