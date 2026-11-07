"""SQLite ledger for MCS unread capture + history archive.

Schema v11 (reuse-oriented):
  runs          : one row per check run (kind: tick | init)
  patients      : per-project fetch state + history_floor (deepest completed
                  cutoff) + history_page (resume cursor for deep imports)
  messages      : dedup by message_id; body_html + body_text (pre-stripped,
                  for display/FTS/LLM reuse); body_state snippet|full|unknown;
                  posted_at + posted_at_ts (epoch, indexed range queries);
                  is_unread flag preserved; content_hash for edit detection
  messages_fts  : FTS5 index over body_text/sender_name (trigger-maintained)
  attachments   : file records w/ sha256+bytes+state
  artifacts     : derived data store — LLM summaries, triage, tags, exports
  notify_outbox : durable pending/in_flight/accepted/failed notify events
  read_marks    : read-acknowledgement intents + results (unknown != success)

Transactions: save_patient()/save_messages() run inside `with self.db` —
a mid-write failure rolls back the whole block.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
from time import monotonic
import uuid
from pathlib import Path
from contextlib import contextmanager, suppress

from mcs_util import RETRY_LATER_4XX, loads_dict


import ledger_schema
from ledger_capture import (
    LedgerCaptureMixin,
    TERMINAL_BODY_STATES as TERMINAL_BODY_STATES,
    _ATTACH_PERMANENT as _ATTACH_PERMANENT,
    _patient_name_for_write as _patient_name_for_write,
    message_write_failure_diagnostic as message_write_failure_diagnostic,
)
from ledger_semantic_sources import (
    LedgerSemanticSourcesMixin,
    _bump_inference_epoch_tx as _bump_inference_epoch_tx,
    bump_inference_analysis_epoch_tx as bump_inference_analysis_epoch_tx,
    bump_semantic_job_state_epoch_tx as bump_semantic_job_state_epoch_tx,
)
import initial_import_state
from ledger_schema import MigrationError  # noqa: F401 - retained construction exception API
from mcs_queries import iter_artifacts, karte_summary_current, karte_summary_row
from snapshots import publish_snapshot  # noqa: F401 - retained public ledger API


SCHEMA_VERSION = 12
JOB_REASON_CODES = frozenset({
    "invalid_payload", "schema_error", "fetch_error", "network_error",
    "worker_error", "worker_timeout", "deadline_exceeded", "session_expired",
    "replies_missing", "window_stalled", "waiting_replies",
})


def job_reason_code(value: object) -> str | None:
    """Recognized acquisition reason, without raw exception text."""
    if isinstance(value, str) and (
            value in JOB_REASON_CODES or re.fullmatch(r"http_[45][0-9]{2}", value)):
        return value
    return None


def job_reason_class(value: object) -> str:
    """Classify availability without claiming an absent clinical action."""
    code = job_reason_code(value)
    if code is None:
        return "unknown"
    if code in ("schema_error", "invalid_payload"):
        return "structural"
    if code in ("replies_missing", "window_stalled"):
        return "incomplete"
    if code == "session_expired":
        return "authentication"
    if code == "waiting_replies":
        return "deferred"
    if code.startswith("http_") and int(code[5:]) < 500 \
            and int(code[5:]) not in RETRY_LATER_4XX:
        return "unavailable"
    return "transient"


# overlap between a verified boundary and an archived patient's final
# head reconciliation walk
HEAD_SYNC_OVERLAP_S = 120

# a failed 連携サマリー GET keeps its project out of the due/fill
# selection this long, so one broken karte cannot burn every tick's cap
KARTE_SUMMARY_BACKOFF_S = 6 * 3600
KARTE_SUMMARY_REFRESH_S = 24 * 3600  # deep runs refresh source summaries daily
# Stamp re-read interval follows the thread's newest post, on roots and
# replies alike. Older threads are checked less often; after the final
# tier no further re-read occurs and the last observation is retained.
# 第2層: a complete actor set older than this is refetched even when the
# post's counts and self flag look unchanged — actors can swap silently




# WAL size a checkpoint truncates back to (bytes)
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024


class Ledger(LedgerCaptureMixin, LedgerSemanticSourcesMixin):
    def __init__(self, path: str, *, notify_all_replies: bool = False):
        self.notify_all_replies = notify_all_replies is True
        self.db = sqlite3.connect(path, timeout=30)
        try:
            os.chmod(path, 0o600)   # PHI store: never umask-loose
            self.db.row_factory = sqlite3.Row
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            ledger_schema.preflight(self.db, tables, version=version,
                                    schema_version=SCHEMA_VERSION)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA busy_timeout=30000")
            # macOS fsync() leaves data in the drive cache; no-ops elsewhere.
            # Set before _init so the first schema writes/migration are covered.
            self.db.execute("PRAGMA fullfsync=ON")
            self.db.execute("PRAGMA checkpoint_fullfsync=ON")
            self._fts = ledger_schema.initialize(self.db, schema_version=SCHEMA_VERSION)
            ledger_schema.install_relation_guards(self.db,
                new_database=not tables, audit=version != SCHEMA_VERSION)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            # a checkpoint truncates the -wal file back to this size; without
            # it one long reader leaves a WAL at its high-water mark forever
            self.db.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}")
            for side in (path + "-wal", path + "-shm"):
                if os.path.exists(side):
                    os.chmod(side, 0o600)
        except BaseException:
            self.db.close()
            raise


    # ---------- runs ----------

    def begin_run(self, snapshot_ts: int | None, kind: str = "tick") -> int:
        # caller holds the process lock — any 'running' row is a crashed run
        self.db.execute(
            "UPDATE runs SET status='crashed',finished_at=? "
            "WHERE status='running'", (time.time(),))
        cur = self.db.execute(
            "INSERT INTO runs(started_at,snapshot_ts,status,kind) "
            "VALUES(?,?,?,?)",
            (time.time(), snapshot_ts, "running", kind))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, error: str = ""):
        self.db.execute(
            "UPDATE runs SET finished_at=?,status=?,error=? WHERE run_id=?",
            (time.time(), status, error[:500], run_id))
        self.db.commit()

    # ---------- patients + messages ----------

    @contextmanager
    def _intake_transaction(self):
        """Identify failures after a held intent was attempted, including commit errors."""
        previous = getattr(self, '_intake_hold_touched', False)
        self._intake_hold_touched = False
        try:
            with self.db:
                yield
        except Exception as exc:
            if self._intake_hold_touched:
                exc.semantic_intake_hold_write_failed = True
            raise
        finally:
            self._intake_hold_touched = previous


    def _reply_job_unread(self, m) -> bool:
        """An open reply job flagged {"unread": true} (stage_thread_read
        acknowledged the reply before it was stored)."""
        row = self.db.execute(
            "SELECT payload FROM fetch_jobs WHERE kind='reply' "
            "AND project_id=? AND message_id=? AND state != 'done'",
            (m.project_id, m.message_id)).fetchone()
        return bool(row) and (loads_dict(row["payload"] or "{}")
                              or {}).get("unread") is True

    def _retire_reply_job(self, m, now: float):
        """A terminal body landing in the ledger retires any queued reply
        job for it — same commit as the save, so a burnt-out or pending
        job can never outlive the body it exists to fetch (Oracle F05).
        Non-terminal replies are NOT enqueued here — the walk's merge and
        the reply-job drain own that decision."""
        if m.body_state in TERMINAL_BODY_STATES:
            self._retire_reply_job_id(m.project_id, m.message_id, now)

    def _retire_reply_job_id(self, project_id, message_id, now: float):
        """Close any open reply job for one message (no commit; the caller's save owns it)."""
        self.db.execute(
            "UPDATE fetch_jobs SET state='done',reason_code=NULL,updated_at=? "
            "WHERE kind='reply' AND project_id=? AND message_id=? AND state != 'done'",
            (now, project_id, message_id))

    def _outbox_insert(self, kind, project_id, payload, next_try=None,
                       route=None):
        """In-transaction outbox insert — caller must hold `with self.db`.
        next_try delays when the drain first picks the event up
        (scheduled digests); default is immediately."""
        now = time.time()
        # eligible kinds are marked 'interactive' at insert; the
        # interactive flag itself is evaluated at delivery time, so a
        # kill switch never strands a sealed card pipeline
        from notify_scope import INTERACTIVE_KINDS
        if route not in ("interactive", "text"):
            route = "interactive" if kind in INTERACTIVE_KINDS else "text"
        cur = self.db.execute("""
          INSERT INTO notify_outbox(kind,project_id,payload,state,next_try,
            route,created_at,updated_at)
          VALUES(?,?,?,'pending',?,?,?,?)
        """, (kind, project_id, json.dumps(payload, ensure_ascii=False),
              next_try if next_try is not None else now, route, now, now))
        return cur.lastrowid


    def is_archived(self, project_id: int) -> bool:
        r = self.db.execute(
            "SELECT is_archived a FROM patients WHERE project_id=?",
            (project_id,)).fetchone()
        return bool(r and r["a"])

    def _update_group_metadata_tx(self, p):
        """Store only a group identity that belongs to the observed group id."""
        from mcs_contract import valid_group_name, valid_id

        observed_type = getattr(p, "project_type", None)
        if observed_type != "group":
            if isinstance(observed_type, str) and observed_type.strip():
                self.db.execute(
                    "UPDATE patients SET group_id=NULL,group_name=NULL WHERE project_id=? "
                    "AND project_type='group' AND (group_id IS NOT NULL OR group_name IS NOT NULL)",
                    (p.project_id,))
            return
        id_present = getattr(p, "group_id_present", False) is True
        name_present = getattr(p, "group_name_present", False) is True
        if not (id_present or name_present):
            return
        row = self.db.execute(
            "SELECT group_id,group_name FROM patients WHERE project_id=? AND project_type='group'",
            (p.project_id,)).fetchone()
        if row is None:
            return
        old_id, old_name = row["group_id"], row["group_name"]
        group_id, name = old_id, old_name
        observed_name = valid_group_name(getattr(p, "group_name", None))
        if id_present:
            observed_id = getattr(p, "group_id", None)
            group_id = observed_id if valid_id(observed_id) else None
            if group_id is None:
                name = None
            elif group_id != old_id:
                name = observed_name if name_present else None
            elif name_present:
                name = observed_name
        elif name_present and observed_name is None:
            name = None
        if group_id != old_id or name != old_name:
            self.db.execute(
                "UPDATE patients SET group_id=?,group_name=? WHERE project_id=? "
                "AND project_type='group' AND group_id IS ? AND group_name IS ?",
                (group_id, name, p.project_id, old_id, old_name))

    def update_group_metadata(self, p):
        """Refresh existing group display metadata without changing collection state."""
        with self.db:
            self._update_group_metadata_tx(p)

    def upsert_patient_info(self, p, is_archived=None, *, notify_new=False,
                            observation_ts: int | None = None, observation_source="server"):
        """Identity fields only — for history imports; does NOT touch
        fetch_state/fetch_reason/last_complete_fetch.

        is_archived=None preserves the flag; True/False writes it inside
        the SAME upsert statement, so archive state can never be
        half-registered by a crash between two commits (Oracle F1).
        notify_new reserves a notifying history walk for a new/reappearing
        live project in that same transaction. New cohorts aggregate initial
        history; older patients retain the existing age-bound intent behavior.

        Returns (created, archive_transitioned): archive_transitioned is
        True when the row ends up archived and was not before — covers
        both a live 0 -> 1 flip and a brand-new archived registration;
        both need the atomic history_head reservation."""
        flag = None if is_archived is None else int(bool(is_archived))
        now = time.time()
        with self.db:
            self.db.execute('UPDATE patients SET project_id=project_id WHERE 0')
            row = self.db.execute(
                "SELECT is_archived a,project_type,karte_id,patient_name FROM patients WHERE project_id=?",
                (p.project_id,)).fetchone()
            prev = bool(row["a"]) if row else False
            import inference_dependency_loss as dependency_loss
            old_karte = (dependency_loss.capture_current_karte_tx(self.db, p.project_id)
                if row is not None and getattr(p, 'karte_id', None) is not None
                and row['karte_id'] != p.karte_id else None)
            # VALUES gets COALESCE(flag,0) for the NOT NULL column on
            # fresh inserts; the UPDATE side re-binds the raw flag so
            # NULL means "preserve" rather than "clear" (COALESCE against
            # excluded.is_archived would never see NULL)
            self.db.execute("""
              INSERT INTO patients(project_id,project_type,patient_name,disease,
                station_name,url,karte_id,is_archived,last_seen,created_at)
              VALUES(?,?,?,?,?,?,?,COALESCE(?,0),?,?)
              ON CONFLICT(project_id) DO UPDATE SET
                group_id=CASE WHEN patients.project_type IS NOT excluded.project_type
                              THEN NULL ELSE patients.group_id END,
                group_name=CASE WHEN patients.project_type IS NOT excluded.project_type
                                THEN NULL ELSE patients.group_name END,
                project_type=excluded.project_type,
                patient_name=excluded.patient_name,
                disease=excluded.disease,
                station_name=excluded.station_name,
                url=excluded.url,
                karte_id=COALESCE(excluded.karte_id, patients.karte_id),
                is_archived=COALESCE(?, patients.is_archived),
                last_seen=excluded.last_seen
            """, (p.project_id, p.project_type, _patient_name_for_write(p, row), p.disease,
                  p.station_name, p.url, getattr(p, "karte_id", None),
                  flag, now, now, flag))
            if getattr(p, "project_type", None) == "group":
                self._update_group_metadata_tx(p)
            if old_karte is not None:
                dependency_loss.record_tx(self.db, old_karte)
            if (row is None or flag is not None and bool(flag) != prev
                    or row['project_type'] != p.project_type
                    or getattr(p, 'karte_id', None) is not None and row['karte_id'] != p.karte_id):
                self.bump_inference_source_epoch_tx()
            if flag == 1 and not prev:
                # live -> archived transition: the final head-sync
                # reservation must be durable in the SAME commit —
                # a separate job_add could be lost to a crash, leaving a
                # floored patient that nothing re-visits (Oracle R1).
                # 'history_head' is a distinct job kind so a pending
                # deep-walk never absorbs it (R2). A pending head from a
                # PREVIOUS archive cycle must still restart at page 1 —
                # new posts arrived during reactivation (F02) — and keep
                # the deeper (smaller) since of the two reservations.
                since = self.archive_head_since(p.project_id)
                old = self.job_payload("history_head", p.project_id)
                if old and type(old.get("since")) is int:
                    since = min(since, old["since"])
                self.job_add_tx("history_head", p.project_id, payload={
                    "since": since, "page": 1, "trickle": True},
                    reset_pending=True)
            if notify_new and flag == 0 and row is None:
                self._start_initial_import_tx(p.project_id, observation_ts, now,
                                              observation_source=observation_source)
            if notify_new and flag == 0 and prev:
                # Registration and its first notifying walk must survive together.
                self.job_add_tx("history", p.project_id, payload={
                    "since": 0, "page": 1, "pages": 3, "trickle": False,
                    "notify": True, "notify_source": "discovery"},
                    reset_pending=prev)
        return (row is None, bool(flag) and not prev)

    def _start_initial_import_tx(self, project_id, observation_ts, now, *, observation_source="server"):
        initial_import_state.start_tx(
            self, project_id, observation_ts if observation_ts is not None else int(now), now,
            boundary_source=observation_source if observation_ts is not None else "local")
        self.job_add_tx("history", project_id, payload={
            "since": 0, "page": 1, "pages": 3, "trickle": False,
            "notify": True, "notify_source": "discovery"})

    def archive_head_since(self, project_id: int) -> int:
        """Anchor for an archived patient's final head reconciliation.
        coverage_ts is the VERIFIED upper boundary — everything at or
        below it was fetched by a completed walk. floor=-1 alone does
        NOT certify the current high watermark: a later unread-path
        store bumps the watermark past verified coverage without
        fetching the gap below (Oracle F01). No boundary (0) falls
        back to a conservative full walk."""
        return max(0, self.coverage_ts(project_id) - HEAD_SYNC_OVERLAP_S)

    def ensure_patient(self, project_id: int) -> bool:
        """Bare row so history_floor/cursor writes never no-op on an
        unknown project (e.g. cmd-queue import targets).
        Returns True when the row was newly created."""
        cur = self.db.execute("""
          INSERT OR IGNORE INTO patients(project_id,last_seen,created_at)
          VALUES(?,?,?)
        """, (project_id, time.time(), time.time()))
        self.db.commit()
        return cur.rowcount > 0

    def history_floor(self, project_id: int) -> int:
        r = self.db.execute(
            "SELECT history_floor f FROM patients WHERE project_id=?",
            (project_id,)).fetchone()
        return (r and r["f"]) or 0

    def unread_cap_cleared(self, project_id: int, oldest_id: int) -> bool:
        """An unread-capped patient may be acknowledged only when the
        history walk has certified everything the unread screen could
        not show: the server's oldest unread message is stored with a
        full (or server-deleted) body, the certified floor reaches
        at/below its posting time (or the whole timeline, -1), verified
        coverage is contiguous up to the newest stored message, all
        parent bodies in the unread time range are full or deleted, and no
        reply fetch is pending. Anything less keeps the patient
        incomplete (never acknowledged)."""
        row = self.db.execute(
            "SELECT posted_at_ts FROM messages WHERE project_id=? "
            "AND message_id=? AND body_state IN ('full','deleted')",
            (project_id, oldest_id)).fetchone()
        if row is None or type(row["posted_at_ts"]) is not int:
            return False
        floor = self.history_floor(project_id)
        if not (floor == -1 or 0 < floor <= row["posted_at_ts"]):
            return False
        wm = self.high_watermark(project_id)
        if not (wm > 0 and self.coverage_ts(project_id) >= wm):
            return False
        # History traversal may certify unknown/bodyless roots. That is not
        # enough to acknowledge the unread range; old roots outside it do not
        # block today's acknowledgement, but an unknown posting time does.
        if self.db.execute(
                "SELECT 1 FROM messages WHERE project_id=? "
                "AND COALESCE(parent_id,0)=0 "
                "AND COALESCE(body_state,'') NOT IN ('full','deleted') "
                "AND (typeof(posted_at_ts)!='integer' OR posted_at_ts>=?) "
                "LIMIT 1", (project_id, row["posted_at_ts"])).fetchone():
            return False
        # a reply whose fetch gave up ('failed' is not pending) still
        # holds only its snippet — acknowledging would hide it for good
        if self.db.execute(
                "SELECT 1 FROM messages WHERE project_id=? "
                "AND COALESCE(parent_id,0)!=0 "
                "AND COALESCE(body_state,'') NOT IN ('full','deleted') "
                "LIMIT 1", (project_id,)).fetchone():
            return False
        return self.pending_reply_jobs(project_id) == 0

    def set_history_floor(self, project_id: int, floor: int):
        # floor <= 0 means the walk reached the END of the timeline —
        # store -1 so "fully imported" stays distinguishable from
        # "never floored" (0/NULL), and -1 <= any since so all the
        # `floor <= since` skip checks behave correctly.
        # Monotonic: -1 is absorbing, and a positive floor may only
        # deepen — a shallow re-completion must never regress it (R5).
        new = -1 if floor <= 0 else floor
        self.db.execute(
            "UPDATE patients SET history_floor=? WHERE project_id=? AND "
            "(history_floor IS NULL OR history_floor=0 "
            "OR ? < history_floor)",
            (new, project_id, new))
        self.db.commit()

    def history_cursor(self, project_id: int) -> int:
        r = self.db.execute(
            "SELECT history_page p FROM patients WHERE project_id=?",
            (project_id,)).fetchone()
        return (r and r["p"]) or 0

    def set_history_cursor(self, project_id: int, page: int):
        self.db.execute(
            "UPDATE patients SET history_page=? WHERE project_id=?",
            (page, project_id))
        self.db.commit()

    def history_target(self, project_id: int) -> int:
        """The `since` epoch the current history_page cursor is walking
        toward — updated on each run so the view reflects the active
        deepen target (P-3)."""
        r = self.db.execute(
            "SELECT history_target t FROM patients WHERE project_id=?",
            (project_id,)).fetchone()
        return (r and r["t"]) or 0

    def set_history_target(self, project_id: int, target: int):
        self.db.execute(
            "UPDATE patients SET history_target=? WHERE project_id=?",
            (target, project_id))
        self.db.commit()

    def reset_history_cursor(self, project_id: int, target: int):
        """Create the patient row and bind target+cursor atomically."""
        now = time.time()
        with self.db:
            self.db.execute("""
              INSERT OR IGNORE INTO patients(project_id,last_seen,created_at)
              VALUES(?,?,?)
            """, (project_id, now, now))
            self.db.execute("""
              UPDATE patients SET history_target=?,history_page=1
              WHERE project_id=?
            """, (target, project_id))


    def patient_fetch_failed(self, project_id: int, reason: str):
        with self.db:
            # Never create a stub: it would bypass atomic initial-history registration.
            previous = self.db.execute('SELECT fetch_reason FROM patients WHERE project_id=?', (project_id,)).fetchone()
            self.db.execute("""
              UPDATE patients SET fetch_state='incomplete',fetch_reason=?,last_seen=? WHERE project_id=?
            """, (reason[:200], time.time(), project_id))
            if previous is not None and (previous[0] == 'forbidden') != (reason[:200] == 'forbidden'):
                self.bump_inference_source_epoch_tx()

    def known_patients(self) -> list:
        return self.db.execute(
            "SELECT project_id,patient_name FROM patients").fetchall()

    def frontier_patients(self) -> list:
        """Patients eligible for per-tick frontier/backfill polling —
        archived patients are excluded; they are imported once via
        durable history jobs, not re-walked every tick (Oracle Q2)."""
        return self.db.execute(
            "SELECT project_id,patient_name FROM patients "
            "WHERE is_archived=0 ORDER BY project_id").fetchall()

    def high_watermark(self, project_id: int) -> int:
        """Epoch of the newest stored message posted_at (0 if none)."""
        r = self.db.execute(
            "SELECT MAX(posted_at_ts) p FROM messages WHERE project_id=?",
            (project_id,)).fetchone()
        return int(r["p"] or 0) if r else 0

    def has_message(self, message_id: int) -> bool:
        return bool(self.db.execute(
            "SELECT 1 FROM messages WHERE message_id=?",
            (message_id,)).fetchone())

    def probe_marker(self, project_id: int) -> int | None:
        """Last `latest`-probed message id for this project — lets the
        self-probe skip an id it already fetched for but could not store
        (e.g. a reply only reachable via its thread), instead of
        re-running the bounded history fetch every tick."""
        r = self.db.execute(
            "SELECT probe_mid FROM patients WHERE project_id=?",
            (project_id,)).fetchone()
        return r["probe_mid"] if r else None

    # ---------- 連携サマリー (karte_summary artifacts) ----------

    def karte_id(self, project_id: int) -> int | None:
        r = self.db.execute(
            "SELECT karte_id FROM patients WHERE project_id=?",
            (project_id,)).fetchone()
        return r["karte_id"] if r else None


    def karte_summary_store(self, project_id: int, karte_id: int,
                            payload: dict | None) -> bool:
        """Persist a fetched 連携サマリー; False when it matches the stored one.

        payload=None is the unregistered form and is stored as
        empty=true/comment=null so 空 (fetched, nothing registered) stays
        distinct from 未取得 (never fetched). An unchanged summary (same
        sha256 and karte_id) writes no new artifact — only the newest row's
        fetched_at moves. Either way the project's due flag is cleared."""
        p = payload or {}
        user = p.get("user") or {}
        content = {
            "comment": p.get("comment"),
            "updated_at": p.get("updated_at") or "",
            "updater": {"profession": user.get("profession") or "",
                        "name": user.get("name") or ""},
            "is_editable": p.get("is_editable"),
            "empty": payload is None,
        }
        text = json.dumps(content, ensure_ascii=False, sort_keys=True)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        now = time.time()
        row = karte_summary_row(self.db, project_id)
        unchanged = None
        if row:
            with suppress(ValueError, TypeError, RecursionError):
                meta = json.loads(row["meta"] or "{}")
                # karte_id too: readers bind the newest row to patients.karte_id
                if (isinstance(meta, dict) and meta.get("sha256") == sha
                        and meta.get("karte_id") == karte_id):
                    meta["fetched_at"] = now
                    unchanged = (json.dumps(meta, ensure_ascii=False), row["artifact_id"])
        if unchanged is not None:
            self._karte_summary_write(project_id, lambda: self.db.execute(
                "UPDATE artifacts SET meta=? WHERE artifact_id=?", unchanged), source_changed=False)
            return False
        meta = {"karte_id": karte_id, "fetched_at": now, "sha256": sha}
        self._karte_summary_write(project_id, lambda: self.artifact_add_tx(
            "karte_summary", text, project_id=project_id, meta=meta))
        return True

    def _karte_summary_write(self, project_id: int, write, *, source_changed=True) -> None:
        """Run write() and the due clear as one unit, then commit as before.

        A caller's already-open transaction is committed with it on success
        (the method has always owned that commit); on failure only this unit
        is undone through a savepoint, so the caller's earlier writes stay."""
        savepoint = f"karte_summary_{uuid.uuid4().hex}" if self.db.in_transaction else None
        if savepoint:
            self.db.execute(f"SAVEPOINT {savepoint}")    # fails before anything is written
        try:
            if not self.db.in_transaction:
                self.db.execute('BEGIN IMMEDIATE')
            import inference_dependency_loss as dependency_loss
            footprint = dependency_loss.capture_current_karte_tx(self.db, project_id) if source_changed else None
            write()
            if footprint is not None:
                dependency_loss.record_tx(self.db, footprint)
            self._clear_karte_summary_due(project_id)
            if source_changed:
                self.bump_inference_source_epoch_tx()
            self.db.commit()
        except BaseException:
            if self.db.in_transaction:                    # SQLite may already have rolled back
                self._undo_karte_summary_write(savepoint)
            raise

    def _undo_karte_summary_write(self, savepoint) -> None:
        """Undo the unit; if that fails, drop the whole uncommitted transaction,
        and if even that fails close the connection so nothing can commit it."""
        if savepoint:
            try:
                self.db.execute(f"ROLLBACK TO {savepoint}")
                self.db.execute(f"RELEASE {savepoint}")
                return
            except sqlite3.Error:
                pass                                     # the caller's open writes are lost below
        try:
            self.db.rollback()
        except sqlite3.Error:
            self.db.close()

    def _clear_karte_summary_due(self, project_id: int):
        self.db.execute(
            "UPDATE patients SET karte_summary_due=0,"
            "karte_summary_failed_at=NULL WHERE project_id=?",
            (project_id,))

    def karte_summary_failed(self, project_id: int):
        """Record a failed 連携サマリー GET; the project sits out selection for KARTE_SUMMARY_BACKOFF_S."""
        self.db.execute(
            "UPDATE patients SET karte_summary_failed_at=? WHERE project_id=?",
            (time.time(), project_id))
        self.db.commit()

    def karte_summary_mark_due(self, project_id: int, due: bool = True):
        """Flag (or unflag) a project for a 連携サマリー GET on the next run.

        Durable: set by the save paths that stored new chat (unread root,
        reply job, self-probe import) and kept when the stage defers or
        fails a GET retryably; cleared by karte_summary_store, or by the
        stage on a non-retryable failure (the next new message re-arms
        it). The flag persists for a project whose karte_id is still
        unknown and fires once it is."""
        self.db.execute(
            "UPDATE patients SET karte_summary_due=? WHERE project_id=?",
            (1 if due else 0, project_id))
        self.db.commit()

    def karte_summary_due(self) -> list:
        """Projects flagged for a summary GET that have a karte_id, newest last_seen first.

        Newest first so a project that just stored chat wins the per-tick
        cap over an older carry-over."""
        return [r["project_id"] for r in self.db.execute(
            "SELECT project_id FROM patients WHERE karte_summary_due=1 "
            "AND karte_id IS NOT NULL AND COALESCE(is_archived,0)=0 "
            "AND COALESCE(karte_summary_failed_at,0)"
            "<? ORDER BY last_seen DESC, project_id",
            (time.time() - KARTE_SUMMARY_BACKOFF_S,))]

    def karte_summary_current(self, project_id: int) -> dict | None:
        """Newest stored summary using this ledger connection."""
        return karte_summary_current(self.db, project_id)

    def karte_summary_missing(self, limit: int) -> list:
        """Live projects with a karte_id and no karte_summary artifact, oldest last_seen first."""
        return [r["project_id"] for r in self.db.execute("""
            SELECT project_id FROM patients p
            WHERE karte_id IS NOT NULL AND is_archived=0
              AND COALESCE(karte_summary_failed_at,0)<? AND NOT EXISTS(
              SELECT 1 FROM artifacts a
              WHERE a.kind='karte_summary' AND a.project_id=p.project_id)
            ORDER BY last_seen, project_id LIMIT ?""",
            (time.time() - KARTE_SUMMARY_BACKOFF_S, limit))]

    def karte_summary_stale(self, limit: int) -> list:
        """Live fetched summaries due for a daily refresh, oldest successful fetch first."""
        now = time.time()
        return [r["project_id"] for r in self.db.execute("""
            SELECT p.project_id, CASE WHEN json_valid(a.meta)
                  THEN CASE WHEN json_type(a.meta,'$.fetched_at') IN ('integer','real')
                            THEN json_extract(a.meta,'$.fetched_at') END
                  END fetched_at
            FROM patients p JOIN artifacts a ON a.artifact_id=(
              SELECT MAX(s.artifact_id) FROM artifacts s
              WHERE s.kind='karte_summary' AND s.project_id=p.project_id)
            WHERE p.karte_id IS NOT NULL AND COALESCE(p.is_archived,0)=0
              AND COALESCE(p.karte_summary_failed_at,0)<?
              AND (fetched_at IS NULL OR fetched_at<=? OR fetched_at>?)
            ORDER BY CASE WHEN fetched_at BETWEEN 0 AND ? THEN fetched_at ELSE 0 END,
                     p.last_seen, p.project_id LIMIT ?""",
            (now - KARTE_SUMMARY_BACKOFF_S, now - KARTE_SUMMARY_REFRESH_S,
             now, now, limit))]

    def set_probe_marker(self, project_id: int, message_id: int):
        self.db.execute(
            "UPDATE patients SET probe_mid=? WHERE project_id=?",
            (message_id, project_id))
        self.db.commit()

    def coverage_ts(self, project_id: int) -> int:
        """Confirmed-complete history coverage watermark (epoch). Distinct
        from high_watermark: a stored newer message must never advance the
        *verified* range (Oracle B06)."""
        r = self.db.execute(
            "SELECT coverage_ts c FROM patients WHERE project_id=?",
            (project_id,)).fetchone()
        return (r and r["c"]) or 0

    def set_coverage(self, project_id: int, ts: int):
        self.db.execute(
            "UPDATE patients SET coverage_ts=MAX(COALESCE(coverage_ts,0),?) "
            "WHERE project_id=?", (ts, project_id))
        self.db.commit()

    def coverage_lag(self, project_id: int) -> int:
        """Seconds between newest stored message and confirmed coverage —
        the observability signal for unfetched gaps."""
        wm = self.high_watermark(project_id)
        return max(0, wm - self.coverage_ts(project_id))

    # ---------- fetch jobs (durable retry units) ----------

    def job_add_tx(self, kind: str, project_id: int, message_id: int = 0,
                    parent_id: int | None = None, payload: dict | None = None,
                    next_try: float = 0, reset_pending: bool = False,
                    revive_failed: bool = True):
        """job_add's INSERT ... ON CONFLICT without the commit — for callers
        holding `with self.db` (e.g. the archive-transition reservation).
        reset_pending=True also replaces a pending row's payload/cursor —
        reserved for transition events where the queued walk no longer
        covers what the new transition demands (Oracle F02).
        revive_failed=False leaves a burnt-out (failed) row alone."""
        now = time.time()
        kept = [] if reset_pending else ["'pending'"]
        if not revive_failed:
            kept.append("'failed'")
        where = (f"WHERE fetch_jobs.state NOT IN ({','.join(kept)})"
                 if kept else "")
        return self.db.execute(f"""
          INSERT INTO fetch_jobs(kind,project_id,message_id,
            parent_id,payload,state,next_try,created_at,updated_at)
          VALUES(?,?,?,?,?,'pending',?,?,?)
          ON CONFLICT(kind,project_id,message_id) DO UPDATE SET
            state='pending',payload=excluded.payload,attempts=0,reason_code=NULL,
            next_try=excluded.next_try,updated_at=excluded.updated_at
          {where}
        """, (kind, project_id, message_id, parent_id,
              json.dumps(payload or {}), next_try or now, now, now))

    def job_add(self, kind: str, project_id: int, message_id: int = 0,
                parent_id: int | None = None, payload: dict | None = None,
                next_try: float = 0, revive_failed: bool = True) -> int | None:
        # a done/failed job for the same key must be REVIVED by a new
        # request — plain INSERT OR IGNORE would silently drop re-import
        # requests forever. An in-flight job keeps its progress.
        # revive_failed=False is for periodic re-discovery (not a request).
        cur = self.job_add_tx(kind, project_id, message_id, parent_id,
                               payload, next_try,
                               revive_failed=revive_failed)
        self.db.commit()
        if cur.rowcount < 1:  # kept an in-flight row; lastrowid is stale here
            return None
        return self.db.execute(
            "SELECT job_id FROM fetch_jobs WHERE kind=? AND project_id=?"
            " AND message_id=?", (kind, project_id, message_id)).fetchone()[0]

    def job_set_flag(self, kind: str, project_id: int, message_id: int,
                     key: str) -> None:
        """Set payload[key]=true on a pending job — job_add keeps a
        pending row's payload, so a flag must be merged in separately."""
        row = self.job_pending(kind, project_id, message_id)
        if row is None:
            return
        pl = loads_dict(row["payload"] or "{}")
        if pl is None or pl.get(key) is True:
            return
        pl[key] = True
        self.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                        (json.dumps(pl), row["job_id"]))
        self.db.commit()

    def _commit_job_update(self, job_id):
        try:
            row = self.db.execute("SELECT project_id,kind FROM fetch_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is not None and row["kind"] in ("history", "reply", "thread"):
                try:
                    initial_import_state.refresh_capture_tx(self, row[0], time.time())
                except ValueError as exc:
                    if str(exc) != "initial_import_invalid":
                        raise
                    self.db.execute("UPDATE fetch_jobs SET state='failed',reason_code='schema_error' WHERE job_id=?",
                                    (job_id,))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def job_done(self, job_id: int):
        self.db.execute(
            "UPDATE fetch_jobs SET state='done',reason_code=NULL,updated_at=? WHERE job_id=?",
            (time.time(), job_id))
        self._commit_job_update(job_id)

    def job_defer(self, job_id: int, retry_in: float,
                  payload: dict | None = None, *, reason_code: str | None = None,
                  reset_attempts: bool = False):
        """Reschedule WITHOUT consuming an attempt — used for progress
        checkpoints and waiting on sub-jobs, not failures."""
        if reason_code is not None and job_reason_code(reason_code) is None:
            raise ValueError("invalid_job_reason")
        if reset_attempts:
            # a rotating job's budget is per pass, not per lifetime
            self.db.execute("UPDATE fetch_jobs SET attempts=0 WHERE job_id=?",
                            (job_id,))
        if payload is not None:
            self.db.execute(
                "UPDATE fetch_jobs SET payload=?,next_try=?,updated_at=?,reason_code=? "
                "WHERE job_id=?",
                (json.dumps(payload), time.time() + retry_in,
                 time.time(), reason_code, job_id))
        else:
            self.db.execute(
                "UPDATE fetch_jobs SET next_try=?,updated_at=?,reason_code=? "
                "WHERE job_id=?", (time.time() + retry_in,
                                  time.time(), reason_code, job_id))
        self._commit_job_update(job_id)

    def job_retry(self, job_id: int, retry_in: float = 300,
                  max_attempts: int = 8, *, payload: dict | None = None,
                  reason_code: str | None = None, fail_fast: bool = True):
        if reason_code is not None and job_reason_code(reason_code) is None:
            raise ValueError("invalid_job_reason")
        if payload is not None:
            self.db.execute(
                "UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                (json.dumps(payload), job_id))
        self.db.execute(
            "UPDATE fetch_jobs SET attempts=attempts+1,next_try=?,updated_at=?,reason_code=? "
            "WHERE job_id=?", (time.time() + retry_in, time.time(), reason_code, job_id))
        # permanent classes (4xx except 429, schema/payload mismatch) cannot
        # heal by retrying: fail on the first attempt, like attachment_failed
        # fail_fast=False keeps a rotating job (reconcile) on the plain
        # attempt budget while still recording the code in this commit
        permanent = fail_fast and job_reason_class(reason_code) in (
            "unavailable", "structural")
        self.db.execute(
            "UPDATE fetch_jobs SET state='failed' "
            "WHERE job_id=? AND (attempts>=? OR ?)",
            (job_id, max_attempts, permanent))
        self._commit_job_update(job_id)

    def job_fail(self, job_id: int, *, reason_code: str | None = None):
        if reason_code is not None and job_reason_code(reason_code) is None:
            raise ValueError("invalid_job_reason")
        self.db.execute(
            "UPDATE fetch_jobs SET state='failed',updated_at=?,reason_code=? WHERE job_id=?",
            (time.time(), reason_code, job_id))
        self._commit_job_update(job_id)

    def history_jobs_due(self) -> list:
        """All due history jobs, least-recently-touched first. Every
        defer/retry bumps updated_at, so a job that was just worked moves
        to the back — a repeatedly incomplete patient cannot starve the
        rest of the queue (Oracle F8). A history_head with no coverage lag
        (nothing stored past verified coverage) is a re-certification the
        backfill re-queues every tick — it goes behind every job that can
        close a real gap."""
        return self.db.execute("""
          SELECT * FROM fetch_jobs j
          WHERE kind IN ('history','history_head')
            AND state='pending' AND next_try <= ?
          ORDER BY kind='history_head' AND
            COALESCE((SELECT MAX(posted_at_ts) FROM messages m
                      WHERE m.project_id=j.project_id),0)
            <= COALESCE((SELECT coverage_ts FROM patients p
                         WHERE p.project_id=j.project_id),0),
            updated_at, job_id
        """, (time.time(),)).fetchall()

    def history_job(self, project_id: int) -> dict | None:
        return self.job_pending("history", project_id)

    def job_state(self, kind: str, project_id: int,
                  message_id: int = 0) -> str | None:
        row = self.db.execute(
            "SELECT state FROM fetch_jobs WHERE kind=? AND project_id=? "
            "AND message_id=? LIMIT 1", (kind, project_id, message_id)
        ).fetchone()
        return row["state"] if row else None

    def job_payload(self, kind: str, project_id: int,
                    message_id: int = 0) -> dict | None:
        """Parsed payload of the job row in ANY state (pending/done/
        failed) — for resume rules that must inspect a failed job's
        last cursor. Malformed payloads return None."""
        r = self.db.execute(
            "SELECT payload FROM fetch_jobs WHERE kind=? AND project_id=? "
            "AND message_id=? LIMIT 1", (kind, project_id, message_id)
        ).fetchone()
        if not r:
            return None
        return loads_dict(r["payload"] or "{}")

    def job_pending(self, kind: str, project_id: int,
                    message_id: int = 0) -> dict | None:
        """Active (state='pending') job of `kind` for a patient
        (message_id=0 sentinel for patient/system-level jobs)."""
        r = self.db.execute("""
          SELECT * FROM fetch_jobs
          WHERE kind=? AND project_id=? AND message_id=? AND state='pending'
          ORDER BY job_id DESC LIMIT 1
        """, (kind, project_id, message_id)).fetchone()
        return dict(r) if r else None


    def pending_reply_jobs(self, project_id: int) -> int:
        """Live reply-fetch work only. 'failed' rows are terminal
        give-ups — merge revives them to 'pending' when a later walk
        re-encounters the incomplete reply, so counting them here
        would block floor certification forever on a burnt-out job."""
        r = self.db.execute("""
          SELECT COUNT(*) c FROM fetch_jobs
          WHERE kind IN ('reply','thread') AND project_id=? AND state='pending'
        """, (project_id,)).fetchone()
        return r["c"]

    def replies_without_job(self, limit: int = 50) -> list:
        """Stored non-terminal replies with NO reply fetch_job in any
        state — replies saved outside a merge (unread path, sibling
        saves) never got a retry reservation and would stay
        'snippet'/'unknown' forever. A 'done' or burnt-out 'failed'
        row counts as reconciled — only the never-queued get seeded."""
        return self.db.execute("""
          SELECT m.message_id,m.project_id,m.parent_id FROM messages m
          WHERE m.parent_id IS NOT NULL
            AND (m.body_state IS NULL
                 OR m.body_state NOT IN ('full','deleted'))
            AND NOT EXISTS(SELECT 1 FROM fetch_jobs j
              WHERE j.kind='reply' AND j.project_id=m.project_id
                AND j.message_id=m.message_id)
          LIMIT ?
        """, (limit,)).fetchall()


    def pending_notify_message_ids(self) -> list[int]:
        """message_ids referenced by unsent notify events — their
        attachments jump the download queue so flush() can attach them."""
        from mcs_util import positive
        out = []
        for r in self.db.execute("""
          SELECT payload FROM notify_outbox
          WHERE kind='new_messages' AND state IN ('pending','failed')
            AND next_try IS NOT NULL
        """):
            p = loads_dict(r["payload"])
            ids = p.get("message_ids") if p is not None else None
            if isinstance(ids, list):
                out.extend(m for m in ids if positive(m))
        return out

    # ---------- notify outbox ----------

    def outbox_add_tx(self, kind: str, project_id: int | None,
                      payload: dict, next_try: float | None = None,
                      route: str | None = None) -> int:
        """outbox_add's INSERT without the commit — callers holding
        `with self.db` can land a notification intent in the same
        transaction as the artifacts that justify it (spec §18.3).
        next_try delays first pickup (scheduled digests)."""
        return self._outbox_insert(kind, project_id, payload, next_try,
                                   route)

    def outbox_add(self, kind: str, project_id: int | None, payload: dict) -> int:
        rid = self.outbox_add_tx(kind, project_id, payload)
        self.db.commit()
        return rid

    def outbox_due(self, limit: int = 20, *, history_fair: bool = False) -> list:
        now = time.time()
        sql = ("SELECT event_id,kind,project_id,payload,attempts,progress,route,state,next_try "
               "FROM notify_outbox WHERE state IN ('pending','failed') AND next_try <= ? ")
        if not history_fair or limit <= 1:
            return self.db.execute(sql + "ORDER BY event_id LIMIT ?", (now, limit)).fetchall()
        live_sql = sql + "AND kind!='patient_history' ORDER BY event_id LIMIT ?"
        live = self.db.execute(live_sql, (now, max(1, limit - 2))).fetchall()
        history = self.db.execute(
            sql + "AND kind='patient_history' ORDER BY event_id LIMIT ?",
            (now, limit - len(live))).fetchall()
        if len(history) + len(live) < limit:
            live = self.db.execute(live_sql, (now, limit - len(history))).fetchall()
        return sorted([*live, *history], key=lambda row: row["event_id"])

    def outbox_progress(self, event_id: int, next_chunk: int,
                        sent_ids: list, fingerprint: str,
                        sending: int | None = None):
        """Partial-send receipt: resume a multi-chunk event where it stopped
        instead of resending already-accepted chunks (Oracle B25).
        `sending` marks the chunk whose send just began but is not yet
        acknowledged — a crash inside that window leaves an uncertain
        delivery the next flush must hold, not resend (F19)."""
        self.db.execute("""
          UPDATE notify_outbox SET progress=?,updated_at=? WHERE event_id=?
        """, (json.dumps({"next": next_chunk, "sent": sent_ids,
                           "fingerprint": fingerprint,
                           "sending": sending}),
              time.time(), event_id))
        self.db.commit()

    def outbox_hold(self, event_id: int, reason: str | None = None):
        """Quarantine an event whose partial-send receipt is unsafe;
        `reason` is a stable code recorded as progress.hold_reason."""
        from mcs_queries import HOLD_PROGRESS_SET
        if reason is None:
            self.db.execute(
                "UPDATE notify_outbox SET state='failed',next_try=NULL,"
                "updated_at=? WHERE event_id=?", (time.time(), event_id))
        else:
            self.db.execute(
                "UPDATE notify_outbox SET state='failed',next_try=NULL,"
                f"{HOLD_PROGRESS_SET},updated_at=? WHERE event_id=?",
                (reason, time.time(), event_id))
        self.db.commit()

    def outbox_defer(self, event_id: int, delay: float, commit: bool = True):
        """Push a still-pending event's next pickup out by ``delay``
        seconds — no attempt consumed, state unchanged."""
        now = time.time()
        self.db.execute(
            "UPDATE notify_outbox SET next_try=?,updated_at=? "
            "WHERE event_id=?", (now + delay, now, event_id))
        if commit:
            self.db.commit()

    def outbox_suppress(self, event_id: int):
        """Terminal drop for events that must never reach Discord —
        e.g. queued before the patient was archived (Oracle F2). Unlike
        outbox_mark it consumes no attempt and leaves no retry timer."""
        self.db.execute(
            "UPDATE notify_outbox SET state='suppressed',next_try=NULL,"
            "updated_at=? WHERE event_id=?", (time.time(), event_id))
        self.db.commit()

    def outbox_mark(self, event_id: int, state: str, accepted_ref: str = "",
                    retry_in: float = 60):
        self.db.execute("""
          UPDATE notify_outbox SET state=?,attempts=attempts+1,
            next_try=?,accepted_ref=?,updated_at=? WHERE event_id=?
        """, (state, time.time() + retry_in, accepted_ref,
              time.time(), event_id))
        self.db.commit()

    # ---------- read marks ----------

    def mark_read(self, project_id: int, ts: int, status: str = "unknown"):
        self.db.execute("""
          INSERT INTO read_marks(project_id,snapshot_ts,marked_at,status)
          VALUES(?,?,?,?)
          ON CONFLICT(project_id,snapshot_ts) DO UPDATE SET
            marked_at=excluded.marked_at,status=excluded.status
        """, (project_id, ts, time.time(), status))
        self.db.commit()

    def was_marked(self, project_id: int, ts: int) -> bool:
        r = self.db.execute("""
          SELECT 1 FROM read_marks
          WHERE project_id=? AND snapshot_ts=? AND status='confirmed'
        """, (project_id, ts)).fetchone()
        return r is not None

    def thread_read_candidates(self, since: float, limit: int, *, exclude_projects=()) -> list:
        """(project_id, parent_id, last_reply_id) of threads holding a
        stored reply newer than their last confirmed read — newest first.
        Only stored replies qualify: a thread is never cleared before
        its replies (and their notify intents) are in the ledger."""
        return self.db.execute("""
          SELECT m.project_id, m.parent_id, MAX(m.message_id) AS last_reply_id
          FROM messages m
          LEFT JOIN thread_read_marks t ON t.parent_id=m.parent_id
          WHERE m.parent_id IS NOT NULL AND m.first_seen>=?
            AND NOT EXISTS (SELECT 1 FROM json_each(?) excluded WHERE excluded.value=m.project_id)
            AND NOT EXISTS (SELECT 1 FROM fetch_jobs j WHERE j.kind='semantic_intake_unstored_v1'
                            AND j.project_id=m.project_id AND j.message_id=0)
            AND (t.parent_id IS NULL OR t.status!='confirmed'
                 OR m.message_id>t.last_reply_id)
          GROUP BY m.project_id, m.parent_id
          ORDER BY MAX(m.first_seen) DESC LIMIT ?
        """, (since, json.dumps(sorted(set(exclude_projects))), limit)).fetchall()

    def stored_reply_ids(self, parent_id: int, *, project_id: int | None = None) -> set:
        """Replies settled for a thread clear: full/deleted bodies, plus
        stored incomplete ones whose reply job burnt out — waiting on a
        failed job would block the thread (and a THREAD_READ_MAX slot)
        until it ages out of the window."""
        return {r[0] for r in self.db.execute("""
          SELECT m.message_id FROM messages m
          WHERE m.parent_id=? AND (? IS NULL OR m.project_id=?)
            AND (m.body_state IN ('full','deleted')
            OR EXISTS(SELECT 1 FROM fetch_jobs j WHERE j.kind='reply'
                      AND j.project_id=m.project_id
                      AND j.message_id=m.message_id AND j.state='failed'))
        """, (parent_id, project_id, project_id))}

    def message_owners(self, message_ids) -> dict:
        """Stored project_id of each given message id, whatever its body state
        or parent: saves refuse to move a message to another project."""
        ids = list(message_ids)
        owners = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            owners.update((r[0], r[1]) for r in self.db.execute(
                "SELECT message_id,project_id FROM messages WHERE message_id IN ("
                + ",".join("?" * len(chunk)) + ")", chunk))
        return owners

    def mark_thread_read(self, project_id: int, parent_id: int,
                         last_reply_id: int, status: str) -> None:
        self.db.execute("""
          INSERT INTO thread_read_marks(parent_id,project_id,last_reply_id,
                                        status,marked_at)
          VALUES(?,?,?,?,?)
          ON CONFLICT(parent_id) DO UPDATE SET
            project_id=excluded.project_id,
            last_reply_id=excluded.last_reply_id,
            status=excluded.status, marked_at=excluded.marked_at
        """, (parent_id, project_id, last_reply_id, status, time.time()))
        self.db.commit()

    # ---------- reuse queries ----------

    def search(self, fts_query: str, limit: int = 50) -> list:
        """FTS5 search over message bodies/senders. Query syntax is FTS5's
        (words, AND/OR, "phrases", prefix*). Returns message rows joined with
        patient names — ids/dates only where possible."""
        if not self._fts:
            return []
        return self.db.execute("""
          SELECT m.message_id, m.project_id, m.parent_id, m.sender_name,
                 m.posted_at, m.body_text, m.body_state,
                 p.patient_name
          FROM messages_fts f
          JOIN messages m ON m.message_id = f.rowid
          LEFT JOIN patients p ON p.project_id = m.project_id
          WHERE messages_fts MATCH ?
          ORDER BY m.posted_at_ts DESC LIMIT ?
        """, (fts_query, limit)).fetchall()

    def artifact_add_tx(self, kind: str, content: str,
                        project_id: int = None, message_id: int = None,
                        model: str = "", meta: dict | None = None) -> int:
        """artifact_add's INSERT without the commit — callers holding
        `with self.db` can commit artifacts together with the job state
        transition they belong to (spec §18.3, INV-16)."""
        cur = self.db.execute("""
          INSERT INTO artifacts(kind,project_id,message_id,content,model,
            meta,created_at) VALUES(?,?,?,?,?,?,?)
        """, (kind, project_id, message_id, content, model,
              json.dumps(meta or {}, ensure_ascii=False), time.time()))
        return cur.lastrowid

    def artifact_add(self, kind: str, content: str, project_id: int = None,
                     message_id: int = None, model: str = "",
                     meta: dict | None = None) -> int:
        rid = self.artifact_add_tx(kind, content, project_id, message_id,
                                   model, meta)
        self.db.commit()
        return rid

    def artifacts(self, kind: str, project_id: int = None,
                  message_id: int = None) -> list:
        return list(self.iter_artifacts(kind, project_id, message_id))

    def iter_artifacts(self, kind: str, project_id: int = None,
                       message_id: int = None, *, descending: bool = False):
        """Read artifact history using this ledger connection."""
        return iter_artifacts(self.db, kind, project_id, message_id,
                              descending=descending)

    def close(self):
        completed = self.__dict__.pop("_patient_context_completed", None)
        try:
            if completed is not None:
                completed.close()
        finally:
            self.__dict__.pop("_cloud_usage_tally", None)
            self.__dict__.pop("_cloud_window_tally", None)
            self.db.close()


class LedgerReader(Ledger):
    """Read-only view for sandboxed consumers (CCO in Docker).
    Opens mode=ro, runs NO migrations/DDL/DML — the original Ledger
    constructor writes on open (migration + FTS backfill), which
    breaks the read-only contract (Oracle B13). Any write attempt fails
    closed at the sqlite layer."""

    def __init__(self, path: str):
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        self.db = sqlite3.connect(uri, uri=True,
                                  timeout=30)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA busy_timeout=30000")
            self._fts = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='messages_fts'").fetchone() is not None
        except BaseException:
            self.db.close()
            raise


def valid_mcs_db(path: str, *, deadline: float | None = None) -> bool:
    """Validate a static recovery/publication candidate without sidecars."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    if deadline is not None and monotonic() >= deadline:
        return False
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        db = sqlite3.connect(uri, uri=True)
        try:
            if deadline is not None:
                db.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" \
                    or not ledger_schema.static_contract_ok(db, schema_version=SCHEMA_VERSION):
                return False
            return deadline is None or monotonic() < deadline
        finally:
            db.close()
    except (OSError, sqlite3.DatabaseError):
        return False
