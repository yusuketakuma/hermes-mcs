"""SQLite ledger for MCS unread capture + history archive.

Schema v5 (reuse-oriented):
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
import html
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path


SCHEMA_VERSION = 5


class MigrationError(RuntimeError):
    """Fail closed when an existing schema cannot be migrated losslessly."""


def _html_to_text(h: str) -> str:
    h = re.sub(r"<br\s*/?>", "\n", h or "")
    h = re.sub(r"</(p|div|li)>", "\n", h)
    return html.unescape(re.sub(r"<[^>]+>", "", h)).strip()


def _posted_epoch(posted_at: str) -> int | None:
    if not posted_at:
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(posted_at).timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


class Ledger:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, timeout=30)
        try:
            self.db.row_factory = sqlite3.Row
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise MigrationError(f"unsupported schema version {version}")
            tables = {r[0] for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if {"attachments_v1", "read_marks_v1"} & tables:
                raise MigrationError("interrupted migration requires review")
            self._preflight(tables)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA busy_timeout=30000")
            self._init()
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        except Exception:
            self.db.close()
            raise

    def _preflight(self, tables: set[str]):
        """Reject ambiguous existing rows before any persistent change."""
        checks = (("attachments", "attachment_id", "message_id,file_id",
                   "duplicate attachment keys"),
                  ("read_marks", "id", "project_id,snapshot_ts",
                   "duplicate read mark keys"))
        for table, marker, keys, error in checks:
            if table not in tables:
                continue
            cols = {r[1] for r in self.db.execute(
                f"PRAGMA table_info({table})")}
            if marker in cols and self.db.execute(
                    f"SELECT 1 FROM {table} GROUP BY {keys} "
                    "HAVING COUNT(*) > 1 LIMIT 1").fetchone():
                raise MigrationError(error)

    def _init(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs(
          run_id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at REAL, finished_at REAL,
          snapshot_ts INTEGER, status TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS patients(
          project_id INTEGER PRIMARY KEY,
          project_type TEXT, patient_name TEXT, disease TEXT,
          station_name TEXT, url TEXT,
          fetch_state TEXT DEFAULT 'pending',
          fetch_reason TEXT,
          last_complete_fetch REAL,
          last_seen REAL, created_at REAL);
        CREATE TABLE IF NOT EXISTS messages(
          message_id INTEGER PRIMARY KEY,
          project_id INTEGER, parent_id INTEGER,
          sender_id INTEGER, sender_name TEXT, sender_type TEXT,
          profession TEXT, organization TEXT,
          posted_at TEXT, posted_at_ts INTEGER, is_unread INTEGER,
          body_html TEXT, body_text TEXT, body_state TEXT,
          content_hash TEXT, reply_count INTEGER,
          first_seen REAL, updated_seen REAL);
        CREATE TABLE IF NOT EXISTS attachments(
          attachment_id INTEGER PRIMARY KEY AUTOINCREMENT,
          message_id INTEGER, file_id TEXT, name TEXT,
          url TEXT, local_path TEXT, bytes INTEGER, sha256 TEXT,
          state TEXT DEFAULT 'pending',
          downloaded_at REAL, created_at REAL);
        CREATE TABLE IF NOT EXISTS notify_outbox(
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT, project_id INTEGER,
          payload TEXT,            -- sanitized descriptor, never raw bodies
          state TEXT DEFAULT 'pending',
          attempts INTEGER DEFAULT 0,
          next_try REAL,
          accepted_ref TEXT,
          created_at REAL, updated_at REAL);
        CREATE TABLE IF NOT EXISTS read_marks(
          project_id INTEGER, snapshot_ts INTEGER, marked_at REAL,
          status TEXT DEFAULT 'unknown',
          PRIMARY KEY(project_id, snapshot_ts));
        CREATE TABLE IF NOT EXISTS artifacts(
          artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT, project_id INTEGER, message_id INTEGER,
          content TEXT, model TEXT, meta TEXT,
          created_at REAL);
        CREATE TABLE IF NOT EXISTS fetch_jobs(
          job_id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT,            -- 'reply' | 'history'
          project_id INTEGER,
          message_id INTEGER DEFAULT 0,   -- 0 for patient-level jobs
          parent_id INTEGER,
          payload TEXT,         -- json extras (since, page cursor, ...)
          state TEXT DEFAULT 'pending',
          attempts INTEGER DEFAULT 0, next_try REAL,
          created_at REAL, updated_at REAL,
          UNIQUE(kind, project_id, message_id));
        """)
        try:
            self.db.execute("""
              CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
              USING fts5(body_text, sender_name)""")
            self._fts = True
        except sqlite3.OperationalError:
            self._fts = False
        self._migrate()   # adds v3 columns first — indexes/triggers depend on them
        self.db.executescript("""
          CREATE INDEX IF NOT EXISTS idx_messages_project_time
            ON messages(project_id, posted_at_ts);
          CREATE INDEX IF NOT EXISTS idx_messages_parent
            ON messages(parent_id);
          CREATE INDEX IF NOT EXISTS idx_artifacts_lookup
            ON artifacts(kind, project_id, message_id);
          CREATE UNIQUE INDEX IF NOT EXISTS uq_attachments_msg_file
            ON attachments(message_id, file_id);
        """)
        if self._fts:
            self.db.executescript("""
              CREATE TRIGGER IF NOT EXISTS messages_ai
              AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, body_text, sender_name)
                VALUES (new.message_id, new.body_text, new.sender_name);
              END;
              CREATE TRIGGER IF NOT EXISTS messages_au
              AFTER UPDATE ON messages BEGIN
                UPDATE messages_fts SET body_text=new.body_text,
                  sender_name=new.sender_name
                WHERE rowid=old.message_id;
                INSERT INTO messages_fts(rowid, body_text, sender_name)
                SELECT new.message_id, new.body_text, new.sender_name
                WHERE NOT EXISTS(SELECT 1 FROM messages_fts
                                 WHERE rowid=new.message_id);
              END;
            """)
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    def _script(self, s: str):
        """Run a ;-separated DDL/DML script inside the caller's transaction
        (executescript commits implicitly — do NOT use it mid-migration)."""
        for st in s.split(";"):
            if st.strip():
                self.db.execute(st)

    def _migrate(self):
        """Normalize a supported pre-existing db without lossy recovery."""
        def cols(t):
            return {r[1] for r in
                    self.db.execute(f"PRAGMA table_info({t})")}
        if "attachment_id" in cols("attachments"):
            duplicate = self.db.execute("""
              SELECT 1 FROM attachments
              GROUP BY message_id,file_id HAVING COUNT(*) > 1 LIMIT 1
            """).fetchone()
            if duplicate:
                raise MigrationError("duplicate attachment keys")
        if "id" in cols("read_marks"):
            duplicate = self.db.execute("""
              SELECT 1 FROM read_marks
              GROUP BY project_id,snapshot_ts HAVING COUNT(*) > 1 LIMIT 1
            """).fetchone()
            if duplicate:
                raise MigrationError("duplicate read mark keys")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._migrate_body(cols)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self._backfill_v3()

    def _migrate_body(self, cols):
        c = cols("patients")
        adds = []
        for col, ddl in [("fetch_state",
                          "ALTER TABLE patients ADD COLUMN fetch_state TEXT DEFAULT 'pending'"),
                         ("fetch_reason",
                          "ALTER TABLE patients ADD COLUMN fetch_reason TEXT"),
                         ("last_complete_fetch",
                          "ALTER TABLE patients ADD COLUMN last_complete_fetch REAL"),
                         ("created_at",
                          "ALTER TABLE patients ADD COLUMN created_at REAL"),
                         ("coverage_ts",
                          "ALTER TABLE patients ADD COLUMN coverage_ts INTEGER"),
                         ("history_target",
                          "ALTER TABLE patients ADD COLUMN history_target INTEGER")]:
            if col not in c:
                adds.append(ddl)
        if adds:
            self._script(";".join(adds) + ";")
        c = cols("messages")
        adds = []
        for col, ddl in [("body_state",
                          "ALTER TABLE messages ADD COLUMN body_state TEXT"),
                         ("content_hash",
                          "ALTER TABLE messages ADD COLUMN content_hash TEXT"),
                         ("updated_seen",
                          "ALTER TABLE messages ADD COLUMN updated_seen REAL")]:
            if col not in c:
                adds.append(ddl)
        if adds:
            self._script(";".join(adds) + ";")
        if "last_seen" in cols("messages"):
            self.db.execute(
              "UPDATE messages SET updated_seen=last_seen "
              "WHERE updated_seen IS NULL AND last_seen IS NOT NULL")
        # attachments: v1 PK/file_id shape != v2 attachment_id — rebuild
        if "attachment_id" not in cols("attachments"):
            self._script("""
              ALTER TABLE attachments RENAME TO attachments_v1;
              CREATE TABLE attachments(
                attachment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER, file_id TEXT, name TEXT,
                url TEXT, local_path TEXT, bytes INTEGER, sha256 TEXT,
                state TEXT DEFAULT 'pending',
                downloaded_at REAL, created_at REAL);
              INSERT INTO attachments(message_id,file_id,name,url,
                local_path,state,downloaded_at,created_at)
                SELECT message_id,file_id,name,url,downloaded_path,
                  CASE WHEN downloaded_path IS NOT NULL
                       THEN 'downloaded' ELSE 'pending' END,
                  first_seen,first_seen FROM attachments_v1;
              DROP TABLE attachments_v1;
            """)
        for col, ddl in [("attempts",
                          "ALTER TABLE attachments ADD COLUMN attempts INTEGER DEFAULT 0"),
                         ("next_try",
                          "ALTER TABLE attachments ADD COLUMN next_try REAL"),
                         ("error",
                          "ALTER TABLE attachments ADD COLUMN error TEXT")]:
            if col not in cols("attachments"):
                self.db.execute(ddl)
        # read_marks: v1 has surrogate id PK; v2 needs (project_id,snapshot_ts)
        if "id" in cols("read_marks"):
            self._script("""
              ALTER TABLE read_marks RENAME TO read_marks_v1;
              CREATE TABLE read_marks(
                project_id INTEGER, snapshot_ts INTEGER, marked_at REAL,
                status TEXT DEFAULT 'unknown',
                PRIMARY KEY(project_id, snapshot_ts));
              INSERT INTO read_marks(project_id,snapshot_ts,
                marked_at,status)
                SELECT project_id,snapshot_ts,marked_at,
                  COALESCE(status,'unknown') FROM read_marks_v1;
              DROP TABLE read_marks_v1;
            """)
        elif "status" not in cols("read_marks"):
            self.db.execute(
              "ALTER TABLE read_marks ADD COLUMN status TEXT DEFAULT 'unknown'")
        if "history_floor" not in cols("patients"):
            self.db.execute(
              "ALTER TABLE patients ADD COLUMN history_floor INTEGER")
        if "history_page" not in cols("patients"):
            self.db.execute(
              "ALTER TABLE patients ADD COLUMN history_page INTEGER DEFAULT 0")
        if "progress" not in cols("notify_outbox"):
            self.db.execute(
              "ALTER TABLE notify_outbox ADD COLUMN progress TEXT")
        c = cols("messages")
        adds = []
        for col, ddl in [
                ("posted_at_ts",
                 "ALTER TABLE messages ADD COLUMN posted_at_ts INTEGER"),
                ("is_unread",
                 "ALTER TABLE messages ADD COLUMN is_unread INTEGER"),
                ("body_text",
                 "ALTER TABLE messages ADD COLUMN body_text TEXT")]:
            if col not in c:
                adds.append(ddl)
        if adds:
            self._script(";".join(adds) + ";")
        if "kind" not in cols("runs"):
            self.db.execute(
              "ALTER TABLE runs ADD COLUMN kind TEXT DEFAULT 'tick'")
        from mcs_requests import SCHEMA
        self._script(SCHEMA)

    def _backfill_v3(self):
        """Fill derived columns for pre-v3 rows (idempotent, chunked)."""
        rows = self.db.execute(
            "SELECT message_id, posted_at, body_html FROM messages "
            "WHERE posted_at_ts IS NULL OR posted_at_ts=0 OR body_text IS NULL").fetchall()
        for r in rows:
            self.db.execute(
                "UPDATE messages SET posted_at_ts=CASE WHEN posted_at_ts IS NULL OR posted_at_ts=0 "
                "THEN ? ELSE posted_at_ts END,"
                " body_text=COALESCE(body_text,?) WHERE message_id=?",
                (_posted_epoch(r["posted_at"]), _html_to_text(r["body_html"]),
                 r["message_id"]))
        if self._fts:
            self.db.execute("""
              INSERT INTO messages_fts(rowid, body_text, sender_name)
              SELECT message_id, body_text, sender_name FROM messages
              WHERE body_text IS NOT NULL AND message_id NOT IN
                (SELECT rowid FROM messages_fts)""")
        self.db.commit()

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

    def _save_attachments(self, m, now: float):
        for a in m.attachments:
            self.db.execute("""
              INSERT INTO attachments
                (message_id,file_id,name,url,created_at)
              VALUES(?,?,?,?,?)
              ON CONFLICT(message_id,file_id) DO UPDATE SET
                name=COALESCE(NULLIF(excluded.name,''),attachments.name),
                url=COALESCE(NULLIF(excluded.url,''),attachments.url)
            """, (m.message_id, a.file_id, a.name, a.url, now))

    def _save_tree(self, m, new_ids: list, now: float):
        """One message + its attachments + replies (with their attachments).
        Replies carry files too — a single shared path keeps them from
        silently dropping."""
        if self._upsert_message(m):
            new_ids.append(m.message_id)
        self._save_attachments(m, now)
        for r in m.replies:
            if self._upsert_message(r):
                new_ids.append(r.message_id)
            self._save_attachments(r, now)

    def _outbox_insert(self, kind, project_id, payload):
        """In-transaction outbox insert — caller must hold `with self.db`."""
        now = time.time()
        cur = self.db.execute("""
          INSERT INTO notify_outbox(kind,project_id,payload,state,next_try,
            created_at,updated_at)
          VALUES(?,?,?,'pending',?,?,?)
        """, (kind, project_id, json.dumps(payload, ensure_ascii=False),
              now, now, now))
        return cur.lastrowid

    def save_patient(self, p, notify: dict | None = None) -> list:
        """Whole patient block + optional notify intent in ONE transaction —
        a crash between message save and outbox insert must not be able to
        lose the notification (Oracle B11). notify is a payload template;
        message_ids is filled with the new ids."""
        now = time.time()
        new_ids = []
        with self.db:  # commit on success, rollback on exception
            self.db.execute("""
              INSERT INTO patients(project_id,project_type,patient_name,disease,
                station_name,url,fetch_state,fetch_reason,last_complete_fetch,
                last_seen,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(project_id) DO UPDATE SET
                project_type=excluded.project_type,
                patient_name=excluded.patient_name,
                disease=excluded.disease,
                station_name=excluded.station_name,
                url=excluded.url,
                fetch_state=excluded.fetch_state,
                fetch_reason=excluded.fetch_reason,
                last_complete_fetch=COALESCE(excluded.last_complete_fetch,
                                             patients.last_complete_fetch),
                last_seen=excluded.last_seen
            """, (p.project_id, p.project_type, p.patient_name, p.disease,
                  p.station_name, p.url, p.fetch_state, p.fetch_reason,
                  now if p.fetch_state == "complete" else None,
                  now, now))
            for m in p.messages:
                self._save_tree(m, new_ids, now)
            if notify and new_ids:
                pl = dict(notify)
                pl["message_ids"] = new_ids
                self._outbox_insert("new_messages", p.project_id, pl)
        return new_ids

    def upsert_patient_info(self, p):
        """Identity fields only — for history imports; does NOT touch
        fetch_state/fetch_reason/last_complete_fetch."""
        now = time.time()
        with self.db:
            self.db.execute("""
              INSERT INTO patients(project_id,project_type,patient_name,disease,
                station_name,url,last_seen,created_at)
              VALUES(?,?,?,?,?,?,?,?)
              ON CONFLICT(project_id) DO UPDATE SET
                project_type=excluded.project_type,
                patient_name=excluded.patient_name,
                disease=excluded.disease,
                station_name=excluded.station_name,
                url=excluded.url,
                last_seen=excluded.last_seen
            """, (p.project_id, p.project_type, p.patient_name, p.disease,
                  p.station_name, p.url, now, now))

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

    def set_history_floor(self, project_id: int, floor: int):
        # floor <= 0 means the walk reached the END of the timeline —
        # store -1 so "fully imported" stays distinguishable from
        # "never floored" (0/NULL), and -1 <= any since so all the
        # `floor <= since` skip checks behave correctly.
        self.db.execute(
            "UPDATE patients SET history_floor=? WHERE project_id=?",
            (-1 if floor <= 0 else floor, project_id))
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
        """The `since` epoch the current history_page cursor belongs to.
        A different requested cutoff means a NEW deepen — the cursor only
        resumes work for the same target (Oracle B09)."""
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

    def save_messages(self, msgs, project_id: int | None = None,
                      notify: dict | None = None) -> list:
        """Backfill path: upsert messages (+reply attachments) without
        touching patient fetch_state. Optional notify intent lands in the
        same transaction. Returns ids of newly-inserted messages."""
        new_ids = []
        now = time.time()
        with self.db:
            for m in msgs:
                self._save_tree(m, new_ids, now)
            if notify and new_ids and project_id:
                pl = dict(notify)
                pl["message_ids"] = new_ids
                self._outbox_insert("new_messages", project_id, pl)
        return new_ids

    def _upsert_message(self, m) -> int:
        """Returns 1 if newly inserted. Never downgrades a stored 'full' body
        to a later 'snippet'."""
        now = time.time()
        chash = hashlib.sha256((m.body_html or "").encode()).hexdigest()
        existed = self.db.execute(
            "SELECT 1 FROM messages WHERE message_id=?",
            (m.message_id,)).fetchone() is not None
        body_text = _html_to_text(m.body_html)
        # unparseable posted_at -> NULL so COALESCE keeps the stored epoch
        # instead of overwriting a valid value with 0 (Oracle T31)
        posted_ts = _posted_epoch(m.posted_at)
        self.db.execute("""
          INSERT INTO messages(message_id,project_id,parent_id,sender_id,
            sender_name,sender_type,profession,organization,posted_at,
            posted_at_ts,is_unread,body_html,body_text,body_state,
            content_hash,reply_count,first_seen,updated_seen)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(message_id) DO UPDATE SET
            posted_at=CASE
              WHEN excluded.posted_at_ts IS NOT NULL THEN excluded.posted_at
              ELSE messages.posted_at END,
            body_html=CASE
              WHEN excluded.body_state='full' THEN excluded.body_html
              WHEN messages.body_state='full' THEN messages.body_html
              ELSE excluded.body_html END,
            body_text=CASE
              WHEN excluded.body_state='full' THEN excluded.body_text
              WHEN messages.body_state='full' THEN messages.body_text
              ELSE excluded.body_text END,
            body_state=CASE
              WHEN excluded.body_state='full' THEN 'full'
              WHEN messages.body_state='full' THEN 'full'
              ELSE excluded.body_state END,
            is_unread=MAX(COALESCE(messages.is_unread,0),
                          COALESCE(excluded.is_unread,0)),
            posted_at_ts=COALESCE(excluded.posted_at_ts,
                                  messages.posted_at_ts),
            content_hash=CASE
              WHEN excluded.body_state='full' THEN excluded.content_hash
              WHEN messages.body_state='full' THEN messages.content_hash
              ELSE excluded.content_hash END,
            reply_count=excluded.reply_count,
            updated_seen=excluded.updated_seen
        """, (m.message_id, m.project_id, m.parent_id, m.sender_id,
              m.sender_name, m.sender_type, m.profession, m.organization,
              m.posted_at, posted_ts, int(bool(m.is_unread)),
              m.body_html, body_text, m.body_state, chash, m.reply_count,
              now, now))
        return 0 if existed else 1

    def patient_fetch_failed(self, project_id: int, reason: str):
        with self.db:
            self.db.execute("""
              INSERT INTO patients(project_id,fetch_state,fetch_reason,
                last_seen,created_at) VALUES(?,?,?,?,?)
              ON CONFLICT(project_id) DO UPDATE SET
                fetch_state='incomplete', fetch_reason=excluded.fetch_reason,
                last_seen=excluded.last_seen
            """, (project_id, "incomplete", reason[:200],
                  time.time(), time.time()))

    def last_seen_ts(self, project_id: int) -> float:
        r = self.db.execute(
            "SELECT MAX(updated_seen) t FROM messages WHERE project_id=?",
            (project_id,)).fetchone()
        return r["t"] or 0.0

    def known_patients(self) -> list:
        return self.db.execute(
            "SELECT project_id,patient_name FROM patients").fetchall()

    def high_watermark(self, project_id: int) -> int:
        """Epoch of the newest stored message posted_at (0 if none)."""
        r = self.db.execute(
            "SELECT MAX(posted_at_ts) p FROM messages WHERE project_id=?",
            (project_id,)).fetchone()
        return int(r["p"] or 0) if r else 0

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

    def job_add(self, kind: str, project_id: int, message_id: int = 0,
                parent_id: int | None = None, payload: dict | None = None,
                next_try: float = 0) -> int | None:
        now = time.time()
        # a done/failed job for the same key must be REVIVED by a new
        # request — plain INSERT OR IGNORE would silently drop re-import
        # requests forever. An in-flight job keeps its progress.
        cur = self.db.execute("""
          INSERT INTO fetch_jobs(kind,project_id,message_id,
            parent_id,payload,state,next_try,created_at,updated_at)
          VALUES(?,?,?,?,?,'pending',?,?,?)
          ON CONFLICT(kind,project_id,message_id) DO UPDATE SET
            state='pending',payload=excluded.payload,attempts=0,
            next_try=excluded.next_try,updated_at=excluded.updated_at
          WHERE fetch_jobs.state != 'pending'
        """, (kind, project_id, message_id, parent_id,
              json.dumps(payload or {}), next_try or now, now, now))
        self.db.commit()
        return cur.lastrowid

    def job_due(self, limit: int = 20, kind: str | None = None,
                after_job_id: int = 0) -> list:
        q = """
          SELECT * FROM fetch_jobs
          WHERE state='pending' AND next_try <= ? AND job_id > ?
        """
        params: list = [time.time(), after_job_id]
        if kind is not None:
            q += " AND kind=?"
            params.append(kind)
        q += " ORDER BY job_id LIMIT ?"
        params.append(limit)
        return self.db.execute(q, params).fetchall()

    def job_done(self, job_id: int):
        self.db.execute(
            "UPDATE fetch_jobs SET state='done',updated_at=? WHERE job_id=?",
            (time.time(), job_id))
        self.db.commit()

    def job_defer(self, job_id: int, retry_in: float,
                  payload: dict | None = None):
        """Reschedule WITHOUT consuming an attempt — used for progress
        checkpoints and waiting on sub-jobs, not failures."""
        if payload is not None:
            self.db.execute(
                "UPDATE fetch_jobs SET payload=?,next_try=?,updated_at=? "
                "WHERE job_id=?",
                (json.dumps(payload), time.time() + retry_in,
                 time.time(), job_id))
        else:
            self.db.execute(
                "UPDATE fetch_jobs SET next_try=?,updated_at=? "
                "WHERE job_id=?", (time.time() + retry_in,
                                  time.time(), job_id))
        self.db.commit()

    def job_retry(self, job_id: int, retry_in: float = 300,
                  max_attempts: int = 8):
        self.db.execute(
            "UPDATE fetch_jobs SET attempts=attempts+1,next_try=?,updated_at=? "
            "WHERE job_id=?", (time.time() + retry_in, time.time(), job_id))
        self.db.execute(
            "UPDATE fetch_jobs SET state='failed' "
            "WHERE job_id=? AND attempts>=?",
            (job_id, max_attempts))
        self.db.commit()

    def job_fail(self, job_id: int):
        self.db.execute(
            "UPDATE fetch_jobs SET state='failed',updated_at=? WHERE job_id=?",
            (time.time(), job_id))
        self.db.commit()

    def history_job(self, project_id: int) -> dict | None:
        return self.job_pending("history", project_id)

    def job_exists(self, kind: str, project_id: int,
                   message_id: int = 0) -> bool:
        return self.db.execute(
            "SELECT 1 FROM fetch_jobs WHERE kind=? AND project_id=? "
            "AND message_id=? LIMIT 1", (kind, project_id, message_id)
        ).fetchone() is not None

    def job_state(self, kind: str, project_id: int,
                  message_id: int = 0) -> str | None:
        row = self.db.execute(
            "SELECT state FROM fetch_jobs WHERE kind=? AND project_id=? "
            "AND message_id=? LIMIT 1", (kind, project_id, message_id)
        ).fetchone()
        return row["state"] if row else None

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
        r = self.db.execute("""
          SELECT COUNT(*) c FROM fetch_jobs
          WHERE kind='reply' AND project_id=? AND state!='done'
        """, (project_id,)).fetchone()
        return r["c"]

    def attachment_saved(self, attachment_id: int, path: str,
                         nbytes: int, sha256: str):
        self.db.execute("""
          UPDATE attachments SET local_path=?,bytes=?,sha256=?,
            state='downloaded',downloaded_at=?,error=NULL
          WHERE attachment_id=?
        """, (path, nbytes, sha256, time.time(), attachment_id))
        self.db.commit()

    def attachment_failed(self, attachment_id: int, kind: str,
                          retry_in: float = 900, max_attempts: int = 6):
        """Retryable failure -> backoff; attempts exhausted -> 'failed'
        (quarantined out of the pending queue instead of retrying forever)."""
        self.db.execute("""
          UPDATE attachments SET attempts=attempts+1,next_try=?,error=?,
            state=CASE WHEN attempts+1>=? THEN 'failed' ELSE 'pending' END
          WHERE attachment_id=?
        """, (time.time() + retry_in, kind[:80], max_attempts, attachment_id))
        self.db.commit()

    def attachments_due(self, limit: int = 50) -> list:
        return self.db.execute("""
          SELECT attachment_id,message_id,file_id,name,url FROM attachments
          WHERE state='pending' AND url != ''
            AND COALESCE(next_try,0) <= ?
          ORDER BY attachment_id LIMIT ?
        """, (time.time(), limit)).fetchall()

    def pending_attachments(self) -> list:
        return self.db.execute("""
          SELECT attachment_id,message_id,file_id,name,url FROM attachments
          WHERE state='pending' AND url != ''
        """).fetchall()

    # ---------- notify outbox ----------

    def outbox_add(self, kind: str, project_id: int | None, payload: dict) -> int:
        now = time.time()
        cur = self.db.execute("""
          INSERT INTO notify_outbox(kind,project_id,payload,state,next_try,
            created_at,updated_at)
          VALUES(?,?,?,'pending',?,?,?)
        """, (kind, project_id, json.dumps(payload, ensure_ascii=False),
              now, now, now))
        self.db.commit()
        return cur.lastrowid

    def outbox_due(self, limit: int = 20) -> list:
        return self.db.execute("""
          SELECT event_id,kind,project_id,payload,attempts,progress
          FROM notify_outbox
          WHERE state IN ('pending','failed') AND next_try <= ?
          ORDER BY event_id LIMIT ?
        """, (time.time(), limit)).fetchall()

    def outbox_progress(self, event_id: int, next_chunk: int,
                        sent_ids: list, fingerprint: str):
        """Partial-send receipt: resume a multi-chunk event where it stopped
        instead of resending already-accepted chunks (Oracle B25)."""
        self.db.execute("""
          UPDATE notify_outbox SET progress=?,updated_at=? WHERE event_id=?
        """, (json.dumps({"next": next_chunk, "sent": sent_ids,
                           "fingerprint": fingerprint}),
              time.time(), event_id))
        self.db.commit()

    def outbox_hold(self, event_id: int):
        """Quarantine an event whose partial-send receipt is unsafe."""
        self.db.execute(
            "UPDATE notify_outbox SET state='failed',next_try=NULL,updated_at=? "
            "WHERE event_id=?", (time.time(), event_id))
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

    def find_patients(self, name: str) -> list:
        """Space-insensitive substring match on patient_name.
        '赤尾' / '赤尾 眞' / '赤尾眞' all match '赤尾 眞'."""
        t = name.replace(" ", "").replace("　", "")
        return self.db.execute("""
          SELECT * FROM patients
          WHERE REPLACE(REPLACE(patient_name,' ',''),'　','') LIKE ?
          ORDER BY patient_name
        """, ("%" + t + "%",)).fetchall()

    def fuzzy_search(self, term: str, limit: int = 50) -> list:
        """Substring search across body_text, sender_name and patient_name.
        Japanese-friendly: ignores whitespace; multi-term = AND.
        Use this when FTS5 (token-based) misses unsegmented Japanese."""
        terms = [t.replace(" ", "").replace("　", "")
                 for t in term.split() if t.strip()]
        if not terms:
            return []
        wh = " AND ".join(
            "(REPLACE(REPLACE(m.body_text,' ',''),'　','') LIKE ?"
            " OR REPLACE(REPLACE(m.sender_name,' ',''),'　','') LIKE ?"
            " OR REPLACE(REPLACE(p.patient_name,' ',''),'　','') LIKE ?)"
            for _ in terms)
        params = []
        for t in terms:
            params += ["%" + t + "%"] * 3
        return self.db.execute(f"""
          SELECT m.message_id, m.project_id, m.parent_id, m.sender_name,
                 m.posted_at, m.body_text, m.body_state,
                 p.patient_name
          FROM messages m
          LEFT JOIN patients p ON p.project_id = m.project_id
          WHERE {wh}
          ORDER BY m.posted_at_ts DESC LIMIT ?
        """, (*params, limit)).fetchall()

    def patient_timeline(self, project_id: int, limit: int = 100,
                         before: tuple | int | None = None) -> list:
        """Viewer path: top-level messages newest-first.
        `before` is a (posted_at_ts, message_id) composite cursor — same-ts
        messages at a page boundary must stay reachable (Oracle T25)."""
        if before is not None:
            ts, mid = (before, 0) if isinstance(before, int) else before
            return self.db.execute("""
              SELECT * FROM messages
              WHERE project_id=? AND parent_id IS NULL
                AND (posted_at_ts<? OR (posted_at_ts=? AND message_id<?))
              ORDER BY posted_at_ts DESC, message_id DESC LIMIT ?
            """, (project_id, ts, ts, mid, limit)).fetchall()
        return self.db.execute("""
          SELECT * FROM messages
          WHERE project_id=? AND parent_id IS NULL
          ORDER BY posted_at_ts DESC, message_id DESC LIMIT ?
        """, (project_id, limit)).fetchall()

    def thread(self, parent_id: int) -> list:
        return self.db.execute("""
          SELECT * FROM messages WHERE parent_id=?
          ORDER BY posted_at_ts ASC
        """, (parent_id,)).fetchall()

    def artifact_add(self, kind: str, content: str, project_id: int = None,
                     message_id: int = None, model: str = "",
                     meta: dict | None = None) -> int:
        cur = self.db.execute("""
          INSERT INTO artifacts(kind,project_id,message_id,content,model,
            meta,created_at) VALUES(?,?,?,?,?,?,?)
        """, (kind, project_id, message_id, content, model,
              json.dumps(meta or {}, ensure_ascii=False), time.time()))
        self.db.commit()
        return cur.lastrowid

    def artifacts(self, kind: str, project_id: int = None,
                  message_id: int = None) -> list:
        q = "SELECT * FROM artifacts WHERE kind=?"
        params: list = [kind]
        if project_id is not None:
            q += " AND project_id=?"; params.append(project_id)
        if message_id is not None:
            q += " AND message_id=?"; params.append(message_id)
        return self.db.execute(q + " ORDER BY artifact_id", params).fetchall()

    def close(self):
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
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self._fts = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='messages_fts'").fetchone() is not None


def publish_snapshot(db_path: str, dest_dir: str) -> str | None:
    """Verified point-in-time copy for read-only consumers.
    backup -> tmp -> quick_check -> atomic rename; consumers never see a
    half-written snapshot. Returns the published path or None."""
    os.makedirs(dest_dir, exist_ok=True)
    tmp = os.path.join(dest_dir, "snapshot.tmp")
    dest = os.path.join(dest_dir, "ledger-snapshot.db")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    src = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                          uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        with dst:
            dst.execute("INSERT OR REPLACE INTO snapshot_meta VALUES(1,?,?)",
                        (str(uuid.uuid4()), time.time()))
    finally:
        dst.close()
        src.close()
    try:
        chk = sqlite3.connect(tmp)
        chk.execute("PRAGMA journal_mode=DELETE")
        chk.close()
    except sqlite3.DatabaseError:
        pass
    if not valid_mcs_db(tmp):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    os.replace(tmp, dest)
    # atomic rename: a consumer mid-open keeps its old inode (still valid),
    # a consumer opening after sees the new generation — no torn reads.
    return dest


def valid_mcs_db(path: str) -> bool:
    """Validate a static recovery/publication candidate without sidecars."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        db = sqlite3.connect(uri, uri=True)
        ok = db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        journal = db.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        db.close()
        return ok and journal == "delete" \
            and {"runs", "patients", "messages"} <= tables
    except (OSError, sqlite3.DatabaseError):
        return False
