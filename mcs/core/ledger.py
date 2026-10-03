"""SQLite ledger for MCS unread capture + history archive.

Schema v8 (reuse-oriented):
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
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from contextlib import suppress

from mcs_util import html_to_text, loads_dict, publish_tmp


SCHEMA_VERSION = 8

# overlap between a verified boundary and an archived patient's final
# head reconciliation walk
HEAD_SYNC_OVERLAP_S = 120

# a failed 連携サマリー GET keeps its project out of the due/fill
# selection this long, so one broken karte cannot burn every tick's cap
KARTE_SUMMARY_BACKOFF_S = 6 * 3600
METADATA_SHADOW_INTERVAL_S = 30 * 60
METADATA_SHADOW_BACKOFF_S = 6 * 3600

# body_state values meaning "nothing more to fetch" — shared by the
# ledger's job-reconciliation and the walk checkpoint logic
TERMINAL_BODY_STATES = ("full", "deleted")

# attachment download failures that retrying the SAME url can never fix
# (a fresh url from the server revives the row — see _save_attachments)
_ATTACH_PERMANENT = frozenset({
    "download_too_large", "url_not_allowed", "download_empty"})


class MigrationError(RuntimeError):
    """Fail closed when an existing schema cannot be migrated losslessly."""


def _posted_epoch(posted_at: str) -> int | None:
    if not posted_at:
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(posted_at).timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def karte_summary_block(content: dict) -> dict:
    """The read-model shape one stored 連携サマリー content maps to —
    shared by the patient rollup and the 🧾 summary line so the two
    never drift on the same artifact."""
    updater = content.get("updater") \
        if isinstance(content.get("updater"), dict) else {}
    return {"comment": content.get("comment"),
            "updated_at": content.get("updated_at"),
            "updater_profession": updater.get("profession"),
            "empty": bool(content.get("empty"))}


def _enqueue_attachment_followup_tx(db, attachment_id, project_id, message_id, now):
    if db.execute("""
        SELECT 1 FROM notify_outbox
        WHERE kind='attachment_followup' AND state != 'suppressed'
          AND json_valid(payload)
          AND json_extract(payload,'$.attachment_id')=?
        LIMIT 1""", (attachment_id,)).fetchone():
        return False
    db.execute("""
        INSERT INTO notify_outbox(kind,project_id,payload,state,next_try,
          route,created_at,updated_at)
        VALUES('attachment_followup',?,?,'pending',?,'text',?,?)
    """, (project_id, json.dumps({"attachment_id": attachment_id,
                                 "message_id": message_id}), now, now, now))
    return True


def enqueue_ready_attachment_followups_tx(db, event_id, now) -> int:
    """Queue downloaded files covered by accepted cards in this transaction."""
    rows = db.execute("""
        SELECT DISTINCT a.attachment_id,m.project_id,m.message_id
        FROM notify_outbox o
        JOIN notification_intent_batches b ON b.event_id=o.event_id
        JOIN notification_intent_cards ic ON ic.event_id=o.event_id
        JOIN json_each(b.frozen_payload,'$.message_ids') frozen
        JOIN json_each(ic.coverage) covered ON covered.value=frozen.value
        JOIN messages m ON m.message_id=frozen.value
        JOIN attachments a ON a.message_id=m.message_id
        WHERE o.event_id=? AND o.kind='new_messages' AND o.state='accepted'
          AND ic.state='delivered' AND frozen.type='integer'
          AND a.state='downloaded' AND COALESCE(a.local_path,'')!=''
          AND m.body_state IS NOT 'deleted'
          -- a file sealed into a card's durable part manifest is owned
          -- by the part pipeline (T7); the text followup would be a
          -- second send of the same file
          AND NOT EXISTS(SELECT 1 FROM notification_render_parts rp
                         WHERE rp.attachment_id=a.attachment_id
                           AND rp.state IN ('pending','delivered',
                                            'held','unknown'))
    """, (event_id,)).fetchall()
    return sum(_enqueue_attachment_followup_tx(
        db, row["attachment_id"], row["project_id"], row["message_id"], now)
        for row in rows)


class Ledger:
    def __init__(self, path: str, *, notify_all_replies: bool = False):
        self.notify_all_replies = notify_all_replies is True
        self.db = sqlite3.connect(path, timeout=30)
        try:
            os.chmod(path, 0o600)   # PHI store: never umask-loose
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
            for side in (path + "-wal", path + "-shm"):
                if os.path.exists(side):
                    os.chmod(side, 0o600)
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
        had_fts = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='messages_fts'"
        ).fetchone() is not None
        try:
            self.db.execute("""
              CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
              USING fts5(body_text, sender_name)""")
            self._fts = True
        except sqlite3.OperationalError:
            self._fts = False
        # adds v3 columns first — indexes/triggers depend on them
        self._migrate(fts_created=self._fts and not had_fts)
        self.db.executescript("""
          CREATE INDEX IF NOT EXISTS idx_messages_project_time
            ON messages(project_id, posted_at_ts);
          CREATE INDEX IF NOT EXISTS idx_messages_parent
            ON messages(parent_id);
          CREATE INDEX IF NOT EXISTS idx_artifacts_lookup
            ON artifacts(kind, project_id, message_id);
          CREATE INDEX IF NOT EXISTS idx_requests_source_msg
            ON requests(source_message_id);
          CREATE INDEX IF NOT EXISTS idx_artifacts_kind_msg
            ON artifacts(kind, message_id);
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

    def _migrate(self, fts_created: bool = False):
        """Normalize a supported pre-existing db without lossy recovery.
        The v3 backfill runs only on an upgrade (old user_version) or
        when this open created messages_fts — e.g. a DB written by a
        Python without FTS5 — since current writers fill the derived
        columns and triggers keep the index in sync."""
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
        # the schema version fence moves WITH the migration commit — a
        # crash must never leave "new columns, old version" behind, or an
        # old writer would keep writing a DB whose one-time backfills
        # already ran (Oracle F03)
        old_version = self.db.execute("PRAGMA user_version").fetchone()[0]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._migrate_body(cols, old_version)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if old_version < SCHEMA_VERSION or fts_created:
            self._backfill_v3()

    def _migrate_body(self, cols, old_version: int):
        self.db.execute("""
          CREATE TABLE IF NOT EXISTS message_metadata(
            message_id INTEGER NOT NULL REFERENCES messages(message_id),
            source TEXT NOT NULL CHECK(source IN ('capture','shadow')),
            content TEXT NOT NULL, checked_at REAL NOT NULL,
            last_error TEXT,
            PRIMARY KEY(message_id,source))
        """)
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
                          "ALTER TABLE patients ADD COLUMN history_target INTEGER"),
                         ("karte_id",
                          "ALTER TABLE patients ADD COLUMN karte_id INTEGER"),
                         ("karte_summary_due",
                          "ALTER TABLE patients ADD COLUMN karte_summary_due "
                          "INTEGER NOT NULL DEFAULT 0"),
                         ("karte_summary_failed_at",
                          "ALTER TABLE patients ADD COLUMN "
                          "karte_summary_failed_at REAL"),
                         ("is_archived",
                          "ALTER TABLE patients ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0")]:
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
        if "probe_mid" not in cols("patients"):
            self.db.execute(
              "ALTER TABLE patients ADD COLUMN probe_mid INTEGER")
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
        if "notified_at" not in cols("messages"):
            self.db.execute(
                "ALTER TABLE messages ADD COLUMN notified_at REAL")
        if old_version < 7:
            # reconstruct the old "stored == notified" boundary precisely:
            # mark read messages (can never notify) plus every message
            # already covered by an outbox intent. Unread messages with no
            # intent stay NULL and will notify once when next observed —
            # that also recovers anything a reply-job drain stored
            # silently before the unread-only intent existed (R4).
            # Keyed on old_version, not column presence: a DB that was
            # interrupted between the ALTER and the version bump re-runs
            # this idempotent fill safely (F03). Runs AFTER the is_unread
            # column add above — older schemas may not have it yet.
            intended = set()
            for r in self.db.execute(
                    "SELECT payload FROM notify_outbox "
                    "WHERE kind='new_messages'"):
                try:
                    pl = json.loads(r["payload"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(pl, dict):
                    # a quarantined non-object payload must not abort the
                    # whole migration (F07) — it carries no usable ids
                    continue
                ids = pl.get("message_ids")
                if isinstance(ids, list):
                    intended.update(i for i in ids if type(i) is int)
            now = time.time()
            self.db.execute(
                "UPDATE messages SET notified_at=? WHERE is_unread=0",
                (now,))
            if intended:
                self.db.execute(
                    "UPDATE messages SET notified_at=? WHERE message_id IN ("
                    + ",".join("?" * len(intended)) + ")",
                    [now, *intended])
        if "kind" not in cols("runs"):
            self.db.execute(
              "ALTER TABLE runs ADD COLUMN kind TEXT DEFAULT 'tick'")
        from mcs_requests import SCHEMA
        self._script(SCHEMA)
        # Interactive delivery uses module-SCHEMA, like mcs_requests;
        # this table does not require its own schema-version bump.
        if "route" not in cols("notify_outbox"):
            self.db.execute(
              "ALTER TABLE notify_outbox ADD COLUMN route TEXT NOT NULL"
              " DEFAULT 'text'")
        from notify_cards import SCHEMA as NOTIFY_CARDS_SCHEMA
        self._script(NOTIFY_CARDS_SCHEMA)
        # Additive transport scope: old rows and immutable spec bytes remain
        # Discord. A Slack workspace is never stored in the guild column.
        for table in ("notification_cards", "notification_renders",
                      "notification_intent_batches"):
            if "transport" not in cols(table):
                self.db.execute(
                    f"ALTER TABLE {table} ADD COLUMN transport TEXT "
                    "NOT NULL DEFAULT 'discord'")
            column = "scope_json" if table == "notification_intent_batches" else "team_id"
            if column not in cols(table):
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        # Durable per-part rollup (T7): 'none' marks renders planned
        # before the part ledger existed.
        if "parts_state" not in cols("notification_renders"):
            self.db.execute(
                "ALTER TABLE notification_renders ADD COLUMN parts_state "
                "TEXT NOT NULL DEFAULT 'none'")
        # ☐/✅ 確認 toggle: a withdrawn ack stays as an audit row
        if "withdrawn_at" not in cols("notification_acknowledgements"):
            self.db.execute(
                "ALTER TABLE notification_acknowledgements "
                "ADD COLUMN withdrawn_at REAL")

    def _backfill_v3(self):
        """Idempotently fill derived columns for pre-v3 rows."""
        rows = self.db.execute(
            "SELECT message_id, posted_at, posted_at_ts, body_html,"
            " body_text IS NULL AS no_text FROM messages "
            "WHERE posted_at_ts IS NULL OR posted_at_ts=0 OR body_text IS NULL").fetchall()
        for r in rows:
            epoch = _posted_epoch(r["posted_at"])
            if epoch is None and r["posted_at_ts"] is None \
                    and not r["no_text"]:
                # an unparseable posted_at stays NULL — rewriting it on
                # every open only churns rows and the FTS trigger
                continue
            self.db.execute(
                "UPDATE messages SET posted_at_ts=CASE WHEN posted_at_ts IS NULL OR posted_at_ts=0 "
                "THEN ? ELSE posted_at_ts END,"
                " body_text=COALESCE(body_text,?) WHERE message_id=?",
                (epoch, html_to_text(r["body_html"]), r["message_id"]))
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
                url=COALESCE(NULLIF(excluded.url,''),attachments.url),
                -- a file listed again after being withdrawn is current
                -- server-side: restore it with its download state intact.
                -- A FRESH url (signed links rotate) revives a row that
                -- failed on the stale one — back to pending, attempts
                -- cleared (F11)
                state=CASE WHEN attachments.state='withdrawn'
                           THEN CASE WHEN attachments.local_path IS NOT NULL
                                     THEN 'downloaded' ELSE 'pending' END
                           WHEN attachments.state IN ('pending','failed')
                                AND NULLIF(excluded.url,'') IS NOT NULL
                                AND excluded.url <> attachments.url
                           THEN 'pending'
                           ELSE attachments.state END,
                attempts=CASE WHEN attachments.state='withdrawn' THEN 0
                              WHEN attachments.state IN ('pending','failed')
                                AND NULLIF(excluded.url,'') IS NOT NULL
                                AND excluded.url <> attachments.url
                              THEN 0 ELSE attachments.attempts END,
                next_try=CASE WHEN attachments.state='withdrawn' THEN NULL
                              WHEN attachments.state IN ('pending','failed')
                                AND NULLIF(excluded.url,'') IS NOT NULL
                                AND excluded.url <> attachments.url
                              THEN NULL ELSE attachments.next_try END,
                error=CASE WHEN attachments.state='withdrawn' THEN NULL
                           WHEN attachments.state IN ('pending','failed')
                                AND NULLIF(excluded.url,'') IS NOT NULL
                                AND excluded.url <> attachments.url
                           THEN NULL ELSE attachments.error END
            """, (m.message_id, a.file_id, a.name, a.url, now))
        # reconcile the set only when the response enumerated `files`
        # (complete list, possibly empty) or the post is deleted —
        # a response that simply omitted the key must not withdraw rows
        if not getattr(m, "files_present", False) \
                and m.body_state != "deleted":
            return
        keep = [a.file_id for a in m.attachments
                if getattr(m, "body_state", "") != "deleted"]
        if keep:
            self.db.execute(f"""
              UPDATE attachments SET state='withdrawn'
              WHERE message_id=? AND state NOT IN ('withdrawn')
                AND file_id NOT IN ({','.join('?' * len(keep))})
            """, (m.message_id, *keep))
        else:
            self.db.execute("""
              UPDATE attachments SET state='withdrawn'
              WHERE message_id=? AND state != 'withdrawn'
            """, (m.message_id,))

    def _save_tree(self, m, new_ids: list, now: float,
                   project_id: int | None = None):
        """One message + its attachments + replies (with their attachments).
        Replies carry files too — a single shared path keeps them from
        silently dropping."""
        expected_project = m.project_id if project_id is None else project_id
        for message in [m, *m.replies]:
            if message.project_id != expected_project:
                raise ValueError("message_project_mismatch")
            if self._upsert_message(message):
                new_ids.append(message.message_id)
            self._save_attachments(message, now)
            self._retire_reply_job(message, now)

    def _semantic_generation_snapshot(self, messages) -> dict:
        """Capture touched rows and their thread generation before a batch."""
        keys = set()
        for message in messages:
            rows = [message, *(getattr(message, "replies", None) or [])]
            for row in rows:
                project_id = getattr(row, "project_id", None)
                message_id = getattr(row, "message_id", None)
                parent_id = getattr(row, "parent_id", None)
                root = message_id if parent_id is None else parent_id
                if project_id is not None and root is not None:
                    keys.add((project_id, message_id, root))
        snapshot = {}
        generations = {}
        for project_id, message_id, root in keys:
            thread_key = (project_id, root)
            if thread_key not in generations:
                generations[thread_key] = self._semantic_source_generation(
                    project_id, root)
            snapshot[(project_id, message_id)] = (
                root,
                generations[thread_key],
                self._semantic_message_fingerprint(project_id, message_id),
            )
        return snapshot

    def _has_canonical_projections(self) -> bool:
        return self.db.execute(
            "SELECT 1 FROM artifacts WHERE kind IN "
            "('canonical_projection','semantic_facts_v4') LIMIT 1"
        ).fetchone() is not None

    def _invalidate_thread_projections(self, project_id: int, root: int):
        changed = self.db.execute("""
          UPDATE artifacts SET meta=json_set(meta,'$.invalidated',json('true'))
          WHERE kind IN ('canonical_projection','semantic_facts_v4')
            AND project_id=?
            AND json_valid(meta)
            AND json_extract(meta,'$.invalidated') IS NOT 1
            AND message_id IN (SELECT message_id FROM messages
              WHERE project_id=? AND (message_id=? OR parent_id=?))
        """, (project_id, project_id, root, root)).rowcount
        if changed:
            self.db.execute(
                "DELETE FROM artifacts WHERE kind='patient_rollup' AND project_id=?",
                (project_id,))

    def _semantic_changed_ids(self, before: dict) -> dict[int, list[int]]:
        """Return touched message IDs whose thread input actually changed."""
        changed: dict[int, list[int]] = {}
        generations = {}
        for (project_id, message_id), (root, previous, row_before) \
                in before.items():
            thread_key = (project_id, root)
            if thread_key not in generations:
                generations[thread_key] = self._semantic_source_generation(
                    project_id, root)
            current = generations[thread_key]
            row_after = self._semantic_message_fingerprint(project_id, message_id)
            if current != previous and row_after != row_before:
                changed.setdefault(project_id, []).append(message_id)
                self._invalidate_thread_projections(project_id, root)
        for ids in changed.values():
            ids.sort()
        return changed

    def _retire_reply_job(self, m, now: float):
        """A terminal body landing in the ledger retires any queued reply
        job for it — same commit as the save, so a burnt-out or pending
        job can never outlive the body it exists to fetch (Oracle F05).
        Non-terminal replies are NOT enqueued here — the walk's merge and
        the reply-job drain own that decision."""
        if m.body_state in TERMINAL_BODY_STATES:
            self.db.execute(
                "UPDATE fetch_jobs SET state='done',updated_at=? "
                "WHERE kind='reply' AND project_id=? AND message_id=? "
                "AND state != 'done'",
                (now, m.project_id, m.message_id))

    def _outbox_insert(self, kind, project_id, payload, next_try=None):
        """In-transaction outbox insert — caller must hold `with self.db`.
        next_try delays when the drain first picks the event up
        (scheduled digests); default is immediately."""
        now = time.time()
        # eligible kinds are marked 'interactive' at insert; the
        # interactive flag itself is evaluated at delivery time, so a
        # kill switch never strands a sealed card pipeline
        from notify_cards import INTERACTIVE_KINDS
        route = "interactive" if kind in INTERACTIVE_KINDS else "text"
        cur = self.db.execute("""
          INSERT INTO notify_outbox(kind,project_id,payload,state,next_try,
            route,created_at,updated_at)
          VALUES(?,?,?,'pending',?,?,?,?)
        """, (kind, project_id, json.dumps(payload, ensure_ascii=False),
              next_try if next_try is not None else now, route, now, now))
        return cur.lastrowid

    def save_patient(self, p, notify: dict | None = None,
                     semantic: bool = False,
                     notify_max_age_s: float | None = None) -> list:
        """Whole patient block + optional notify intent in ONE transaction —
        a crash between message save and outbox insert must not be able to
        lose the notification (Oracle B11). notify is a payload template;
        message_ids is filled with the new ids. semantic=True also seeds
        durable semantic-eval jobs for the same messages in the SAME
        commit (INV-06).

        notify_max_age_s drops messages posted more than this many
        seconds ago from the notification (bulk-added patients surface
        as 'unread' with months-old history — those are imported and
        consumed, never announced). A NULL/unparseable posted_at cannot
        be proven old, so it still notifies. Stale ids are marked
        notified so they never resurface as candidates."""
        now = time.time()
        new_ids = []
        before_semantic = (self._semantic_generation_snapshot(p.messages)
                           if semantic or self._has_canonical_projections() else {})
        with self.db:  # commit on success, rollback on exception
            self.db.execute("""
              INSERT INTO patients(project_id,project_type,patient_name,disease,
                station_name,url,karte_id,fetch_state,fetch_reason,
                last_complete_fetch,last_seen,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(project_id) DO UPDATE SET
                project_type=excluded.project_type,
                patient_name=excluded.patient_name,
                disease=excluded.disease,
                station_name=excluded.station_name,
                url=excluded.url,
                karte_id=COALESCE(excluded.karte_id, patients.karte_id),
                fetch_state=excluded.fetch_state,
                fetch_reason=excluded.fetch_reason,
                last_complete_fetch=COALESCE(excluded.last_complete_fetch,
                                             patients.last_complete_fetch),
                last_seen=excluded.last_seen,
                -- the unread path only sees live projects: presence here
                -- is positive proof of reactivation (Oracle F4)
                is_archived=0
            """, (p.project_id, p.project_type, p.patient_name, p.disease,
                  p.station_name, p.url, getattr(p, "karte_id", None),
                  p.fetch_state, p.fetch_reason,
                  now if p.fetch_state == "complete" else None,
                  now, now))
            for m in p.messages:
                self._save_tree(m, new_ids, now, p.project_id)
            changed_semantic = (self._semantic_changed_ids(before_semantic)
                                if before_semantic else {})
            # The unread endpoint makes all newly stored posts eligible;
            # previously stored posts are eligible only when THIS fetch
            # still reports them unread (the stored flag is sticky).
            fresh_unread = [m.message_id for m in p.messages
                            if m.is_unread] + [
                t.message_id for m in p.messages for t in m.replies
                if t.is_unread]
            notify_ids = (list(dict.fromkeys(
                new_ids + self._unnotified(fresh_unread))) if notify else [])
            self._queue_saved_messages_tx(
                p.project_id, new_ids, changed_semantic.get(p.project_id, []),
                notify_ids, notify=notify, semantic=semantic, now=now,
                notify_max_age_s=notify_max_age_s)
        return new_ids

    def _queue_saved_messages_tx(self, project_id, new_ids, changed_ids,
                                  notify_ids, *, notify, semantic, now,
                                  notify_max_age_s):
        """Keep notification eligibility and semantic coverage in the save
        transaction for unread, backfill and reply arrivals alike."""
        if not (notify or semantic) or self.is_archived(project_id):
            return
        origin = {"source": "history_import"}
        if notify:
            origin = {"source": notify.get("source")}
            # Recent arrivals also notify when the server already marks
            # them read. A bounded age and known timestamp keep historical
            # imports from announcing old or undated read context.
            if new_ids and notify_max_age_s is not None:
                recent = self.db.execute(
                    "SELECT message_id FROM messages WHERE message_id IN ("
                    + ",".join("?" * len(new_ids))
                    + ") AND posted_at_ts >= ?",
                    [*new_ids, now - notify_max_age_s]).fetchall()
                notify_ids = list(dict.fromkeys(
                    notify_ids + self._unnotified([r[0] for r in recent])))
            # Opted-in realtime notification paths cover every new
            # reply. Bulk imports have no notify template and stay quiet.
            reply_ids = []
            if self.notify_all_replies and new_ids:
                replies = self.db.execute(
                    "SELECT message_id FROM messages WHERE message_id IN ("
                    + ",".join("?" * len(new_ids))
                    + ") AND parent_id IS NOT NULL AND parent_id != 0",
                    new_ids).fetchall()
                reply_ids = self._unnotified([r[0] for r in replies])
            unbounded = set(reply_ids)
            notify_ids = self._filter_notify_age(
                [mid for mid in notify_ids if mid not in unbounded],
                now, notify_max_age_s)
            notify_ids = list(dict.fromkeys(notify_ids + reply_ids))
            if notify_ids:
                payload = dict(notify, message_ids=notify_ids)
                origin["event_id"] = self._outbox_insert(
                    "new_messages", project_id, payload)
                self._mark_notified(notify_ids, now)
        if semantic:
            # Every new/changed post is evaluation input, including
            # already-read and age-filtered arrivals. Notification
            # eligibility is separately derived from the origin event.
            seed_ids = sorted(set(new_ids) | set(notify_ids) | set(changed_ids))
            if seed_ids:
                self._semantic_seed_tx(project_id, seed_ids, origin)

    def _filter_notify_age(self, ids: list, now: float,
                           max_age_s: float | None) -> list:
        """Drop ids posted more than max_age_s ago from the candidate
        set — bulk-added patients surface as 'unread' carrying
        months-old history; those are imported and consumed, never
        announced. NULL/unparseable posted_at cannot be proven old, so
        it is kept. Stale ids are marked notified in the SAME commit so
        a later fetch reporting them unread cannot resurrect them.
        Shared by every notify path (unread, backfill, reply_job) —
        an old post is equally stale no matter which fetch saw it.
        Caller holds `with self.db`."""
        if not ids or max_age_s is None:
            return ids
        q = ("SELECT message_id FROM messages WHERE message_id IN ("
             + ",".join("?" * len(ids)) + ") AND "
             "(posted_at_ts IS NULL OR posted_at_ts=0 "
             "OR posted_at_ts >= ?)")
        keep = {r["message_id"] for r in self.db.execute(
            q, (*ids, now - max_age_s))}
        stale = [i for i in ids if i not in keep]
        self._mark_notified(stale, now)
        return [i for i in ids if i in keep]

    def _unnotified(self, ids: list) -> list:
        """Of caller-selected candidates, return rows never notified.
        Caller holds `with self.db`."""
        if not ids:
            return []
        q = ("SELECT message_id FROM messages WHERE notified_at IS NULL "
             "AND message_id IN (" + ",".join("?" * len(ids)) + ")")
        return [r["message_id"] for r in self.db.execute(q, ids)]

    def _mark_notified(self, ids: list, now: float):
        """Caller holds `with self.db` — same commit as the outbox intent,
        so a crash cannot produce a notified-but-unqueued message."""
        if not ids:
            return
        self.db.execute(
            "UPDATE messages SET notified_at=? WHERE message_id IN ("
            + ",".join("?" * len(ids)) + ")", [now, *ids])

    def is_archived(self, project_id: int) -> bool:
        r = self.db.execute(
            "SELECT is_archived a FROM patients WHERE project_id=?",
            (project_id,)).fetchone()
        return bool(r and r["a"])

    def upsert_patient_info(self, p, is_archived=None):
        """Identity fields only — for history imports; does NOT touch
        fetch_state/fetch_reason/last_complete_fetch.

        is_archived=None preserves the flag; True/False writes it inside
        the SAME upsert statement, so archive state can never be
        half-registered by a crash between two commits (Oracle F1).

        Returns (created, archive_transitioned): archive_transitioned is
        True when the row ends up archived and was not before — covers
        both a live 0 -> 1 flip and a brand-new archived registration;
        both need the atomic history_head reservation."""
        flag = None if is_archived is None else int(bool(is_archived))
        now = time.time()
        with self.db:
            row = self.db.execute(
                "SELECT is_archived a FROM patients WHERE project_id=?",
                (p.project_id,)).fetchone()
            prev = bool(row["a"]) if row else False
            # VALUES gets COALESCE(flag,0) for the NOT NULL column on
            # fresh inserts; the UPDATE side re-binds the raw flag so
            # NULL means "preserve" rather than "clear" (COALESCE against
            # excluded.is_archived would never see NULL)
            self.db.execute("""
              INSERT INTO patients(project_id,project_type,patient_name,disease,
                station_name,url,karte_id,is_archived,last_seen,created_at)
              VALUES(?,?,?,?,?,?,?,COALESCE(?,0),?,?)
              ON CONFLICT(project_id) DO UPDATE SET
                project_type=excluded.project_type,
                patient_name=excluded.patient_name,
                disease=excluded.disease,
                station_name=excluded.station_name,
                url=excluded.url,
                karte_id=COALESCE(excluded.karte_id, patients.karte_id),
                is_archived=COALESCE(?, patients.is_archived),
                last_seen=excluded.last_seen
            """, (p.project_id, p.project_type, p.patient_name, p.disease,
                  p.station_name, p.url, getattr(p, "karte_id", None),
                  flag, now, now, flag))
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
                self._job_add_tx("history_head", p.project_id, payload={
                    "since": since, "page": 1, "trickle": True},
                    reset_pending=True)
        return (row is None, bool(flag) and not prev)

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
        coverage is contiguous up to the newest stored message, and no
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

    def save_messages(self, msgs, project_id: int | None = None,
                      notify: dict | None = None,
                      semantic: bool = False,
                      notify_max_age_s: float | None = None,
                      notify_all_new: bool = False) -> list:
        """Backfill path: upsert messages (+reply attachments) without
        touching patient fetch_state. Optional notify intent lands in the
        same transaction. Returns ids of newly-inserted messages.
        notify_max_age_s drops stale posts from the intent (bulk-added
        patients carry months-old unread history).
        notify_all_new widens notification eligibility from this fetch's
        unread posts to every newly-stored row — used by the self-post
        probe, whose whole point is catching posts the unread set can
        never contain (own posts are is_unread=0 by definition).
        With notify_max_age_s, newly stored read posts with a known
        recent timestamp also qualify through the shared save gate.
        notify_all_replies on the ledger exempts new replies from the age
        and unread gates only when this call supplies a notify template."""
        new_ids = []
        now = time.time()
        before_semantic = (self._semantic_generation_snapshot(msgs)
                           if semantic or self._has_canonical_projections() else {})
        with self.db:
            for m in msgs:
                self._save_tree(m, new_ids, now, project_id)
            changed_semantic = (self._semantic_changed_ids(before_semantic)
                                if before_semantic else {})
            if project_id:
                # Previously stored rows qualify when freshly unread;
                # the shared gate also admits new recent read arrivals.
                fresh_unread = [m.message_id for m in msgs
                                if m.is_unread] + [
                    t.message_id for m in msgs for t in m.replies
                    if t.is_unread]
                if notify_all_new:
                    fresh_unread = list(dict.fromkeys(
                        fresh_unread + new_ids))
                notify_ids = self._unnotified(fresh_unread) if notify else []
                self._queue_saved_messages_tx(
                    project_id, new_ids, changed_semantic.get(project_id, []),
                    notify_ids, notify=notify, semantic=semantic, now=now,
                    notify_max_age_s=notify_max_age_s)
        return new_ids

    def save_thread_replies(self, replies: list, project_id: int,
                            notify: dict | None = None,
                            semantic: bool = False,
                            notify_max_age_s: float | None = None) -> list:
        """Reply-job drain path: persist a fetched thread's replies AND
        reconcile their reply-job states in the SAME commit (Oracle F05).
        A reply whose body is now terminal retires its queued job (a
        burnt-out one would otherwise block floor certification forever);
        a still-incomplete reply reserves a durable retry.
        notify_max_age_s drops stale replies from the intent — a thread
        drained for a bulk-added patient can carry months-old unread
        replies that must not announce. New read replies with a known
        timestamp inside the bound also qualify for notification;
        notify_all_replies exempts new replies from that bound."""
        new_ids = []
        now = time.time()
        before_semantic = (self._semantic_generation_snapshot(replies)
                           if semantic or self._has_canonical_projections() else {})
        with self.db:
            for m in replies:
                self._save_tree(m, new_ids, now, project_id)
                # reconcile against the STORED body, not the fetched one:
                # upsert never downgrades 'full', so a refetch returning
                # a snippet for an already-full reply must not spawn an
                # endless retry job — and any stale job for it must be
                # retired here (_save_tree's retire only fires on a
                # terminal FETCH state)
                st = self.db.execute(
                    "SELECT body_state s FROM messages WHERE message_id=?",
                    (m.message_id,)).fetchone()
                if st and st["s"] in TERMINAL_BODY_STATES:
                    self.db.execute(
                        "UPDATE fetch_jobs SET state='done',updated_at=? "
                        "WHERE kind='reply' AND project_id=? "
                        "AND message_id=? AND state != 'done'",
                        (now, project_id, m.message_id))
                else:
                    # never revive a failed sibling: two replies the
                    # thread API keeps returning as snippets would
                    # otherwise reset each other's attempts forever and
                    # pending_reply_jobs could never reach 0
                    self._job_add_tx("reply", project_id, m.message_id,
                                     parent_id=m.parent_id,
                                     revive_failed=False)
            changed_semantic = (self._semantic_changed_ids(before_semantic)
                                if before_semantic else {})
            notify_ids = (self._unnotified(
                [m.message_id for m in replies if m.is_unread]) if notify else [])
            self._queue_saved_messages_tx(
                project_id, new_ids, changed_semantic.get(project_id, []),
                notify_ids, notify=notify, semantic=semantic, now=now,
                notify_max_age_s=notify_max_age_s)
        return new_ids

    def _upsert_message(self, m) -> int:
        """Returns 1 if newly inserted. Never downgrades a stored 'full' body
        to a later 'snippet'."""
        now = time.time()
        chash = hashlib.sha256((m.body_html or "").encode()).hexdigest()
        existing = self.db.execute(
            "SELECT project_id FROM messages WHERE message_id=?",
            (m.message_id,)).fetchone()
        if existing is not None and existing["project_id"] != m.project_id:
            raise ValueError("message_project_mismatch")
        body_text = html_to_text(m.body_html)
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
            -- history/snippet responses may omit metadata; keep the
            -- authoritative values already hydrated in the ledger.
            parent_id=COALESCE(excluded.parent_id, messages.parent_id),
            sender_id=COALESCE(excluded.sender_id, messages.sender_id),
            sender_name=CASE
              WHEN excluded.sender_name != '' THEN excluded.sender_name
              ELSE messages.sender_name END,
            sender_type=CASE
              WHEN excluded.sender_type != '' THEN excluded.sender_type
              ELSE messages.sender_type END,
            profession=CASE
              WHEN excluded.profession != '' THEN excluded.profession
              ELSE messages.profession END,
            organization=CASE
              WHEN excluded.organization != '' THEN excluded.organization
              ELSE messages.organization END,
            posted_at=CASE
              WHEN excluded.posted_at_ts IS NOT NULL THEN excluded.posted_at
              ELSE messages.posted_at END,
            body_html=CASE
              WHEN excluded.body_state='deleted' THEN ''
              WHEN excluded.body_state='full' THEN excluded.body_html
              WHEN messages.body_state IN ('full','deleted') THEN messages.body_html
              ELSE excluded.body_html END,
            body_text=CASE
              WHEN excluded.body_state='deleted' THEN ''
              WHEN excluded.body_state='full' THEN excluded.body_text
              WHEN messages.body_state IN ('full','deleted') THEN messages.body_text
              ELSE excluded.body_text END,
            body_state=CASE
              WHEN excluded.body_state='deleted' THEN 'deleted'
              WHEN excluded.body_state='full' THEN 'full'
              WHEN messages.body_state IN ('full','deleted') THEN messages.body_state
              ELSE excluded.body_state END,
            is_unread=MAX(COALESCE(messages.is_unread,0),
                          COALESCE(excluded.is_unread,0)),
            posted_at_ts=COALESCE(excluded.posted_at_ts,
                                  messages.posted_at_ts),
            content_hash=CASE
              WHEN excluded.body_state='deleted' THEN excluded.content_hash
              WHEN excluded.body_state='full' THEN excluded.content_hash
              WHEN messages.body_state IN ('full','deleted') THEN messages.content_hash
              ELSE excluded.content_hash END,
            reply_count=excluded.reply_count,
            updated_seen=excluded.updated_seen
        """, (m.message_id, m.project_id, m.parent_id, m.sender_id,
              m.sender_name, m.sender_type, m.profession, m.organization,
              m.posted_at, posted_ts, int(bool(m.is_unread)),
              m.body_html, body_text, m.body_state, chash, m.reply_count,
              now, now))
        self._save_message_metadata(m, source="capture", now=now)
        return 0 if existing is not None else 1

    def _save_message_metadata(self, m, *, source, now):
        metadata = getattr(m, "metadata", {})
        errors = getattr(m, "metadata_errors", [])
        if not metadata and not errors and source == "capture":
            return
        row = self.db.execute(
            "SELECT content FROM message_metadata WHERE message_id=? AND source=?",
            (m.message_id, source)).fetchone()
        content = loads_dict(row["content"]) if row else {}
        content.update({key: {"value": value, "observed_at": now}
                        for key, value in metadata.items()})
        self.db.execute("""
          INSERT INTO message_metadata(message_id,source,content,checked_at,last_error)
          VALUES(?,?,?,?,?) ON CONFLICT(message_id,source) DO UPDATE SET
            content=excluded.content,checked_at=excluded.checked_at,
            last_error=excluded.last_error
        """, (m.message_id, source, json.dumps(content, ensure_ascii=False),
              now, ",".join(errors) or None))

    def save_metadata_shadow(self, m, *, error=None):
        """Save refresh observations without changing chat, coverage or delivery."""
        row = self.db.execute(
            "SELECT project_id FROM messages WHERE message_id=?", (m.message_id,)).fetchone()
        if row is None or row["project_id"] != m.project_id:
            raise ValueError("message_project_mismatch")
        if error is not None:
            from types import SimpleNamespace
            m = SimpleNamespace(message_id=m.message_id, metadata={}, metadata_errors=[error])
        with self.db:
            self._save_message_metadata(m, source="shadow", now=time.time())

    def metadata_watch_targets(self, limit=5, *, now=None):
        """Oldest observations in the bounded active watch set, excluding backoff."""
        from mcs_signals import self_sender_id
        now = time.time() if now is None else now
        return self.db.execute("""
          SELECT m.message_id,m.project_id,m.parent_id,COUNT(*) OVER() AS due_total FROM messages m
          JOIN patients p ON p.project_id=m.project_id
          LEFT JOIN message_metadata md ON md.message_id=m.message_id AND md.source='shadow'
          WHERE p.is_archived=0 AND m.body_state IS NOT 'deleted'
            AND (md.checked_at IS NULL OR md.checked_at<=? -
              CASE WHEN md.last_error IS NULL THEN ? ELSE ? END)
            AND ((m.sender_id=? AND m.parent_id IS NULL AND m.posted_at_ts>=?)
              OR EXISTS(SELECT 1 FROM notification_cards c
                WHERE c.project_id=m.project_id AND c.root_message_id=COALESCE(m.parent_id,m.message_id)
                  AND c.delivery_state='delivered'
                  AND NOT EXISTS(SELECT 1 FROM notification_acknowledgements a
                    JOIN notification_view_manifests vm ON vm.manifest_id=a.manifest_id
                    WHERE a.card_id=c.card_id AND a.withdrawn_at IS NULL
                      AND vm.source_generation=c.source_generation
                      AND vm.shown=(SELECT shown FROM notification_view_manifests
                        WHERE card_id=c.card_id ORDER BY manifest_id DESC LIMIT 1)))
              OR EXISTS(SELECT 1 FROM artifacts a,json_each(
                CASE WHEN json_valid(a.content) THEN a.content ELSE '{}' END,'$.evidence.message_ids') e
                WHERE a.kind='signal_v1' AND json_valid(a.content) AND json_valid(a.meta)
                  AND a.project_id=m.project_id AND e.value=m.message_id
                  AND json_extract(a.content,'$.type')='pharmacist_request_unanswered'
                  AND json_extract(a.content,'$.state')='open'
                  AND NOT EXISTS(SELECT 1 FROM artifacts newer
                    WHERE newer.kind=a.kind AND json_valid(newer.meta)
                      AND json_extract(newer.meta,'$.key')=json_extract(a.meta,'$.key')
                      AND newer.artifact_id>a.artifact_id)))
          ORDER BY COALESCE(md.checked_at,0),m.message_id LIMIT ?
        """, (now, METADATA_SHADOW_INTERVAL_S, METADATA_SHADOW_BACKOFF_S,
              self_sender_id(self.db), now - 7*86400, limit)).fetchall()

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

    def known_patients(self) -> list:
        return self.db.execute(
            "SELECT project_id,patient_name FROM patients").fetchall()

    def frontier_patients(self) -> list:
        """Patients eligible for per-tick frontier/backfill polling —
        archived patients are excluded; they are imported once via
        durable history jobs, not re-walked every tick (Oracle Q2)."""
        return self.db.execute(
            "SELECT project_id,patient_name FROM patients "
            "WHERE is_archived=0").fetchall()

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

    def _karte_summary_row(self, project_id: int):
        return self.db.execute(
            "SELECT artifact_id,content,meta FROM artifacts "
            "WHERE kind='karte_summary' AND project_id=? "
            "ORDER BY artifact_id DESC LIMIT 1", (project_id,)).fetchone()

    def karte_summary_store(self, project_id: int, karte_id: int,
                            payload: dict | None) -> bool:
        """Persist a fetched 連携サマリー; False when it matches the stored one.

        payload=None is the unregistered form and is stored as
        empty=true/comment=null so 空 (fetched, nothing registered) stays
        distinct from 未取得 (never fetched). An unchanged summary (same
        sha256) writes no new artifact — only the newest row's
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
        row = self._karte_summary_row(project_id)
        if row:
            with suppress(json.JSONDecodeError, TypeError):
                meta = json.loads(row["meta"] or "{}")
                if isinstance(meta, dict) and meta.get("sha256") == sha:
                    meta["fetched_at"] = now
                    self.db.execute(
                        "UPDATE artifacts SET meta=? WHERE artifact_id=?",
                        (json.dumps(meta, ensure_ascii=False),
                         row["artifact_id"]))
                    self._karte_summary_stored(project_id)
                    return False
        self.artifact_add("karte_summary", text, project_id=project_id,
                          meta={"karte_id": karte_id, "fetched_at": now,
                                "sha256": sha})
        self._karte_summary_stored(project_id)
        return True

    def _karte_summary_stored(self, project_id: int):
        self.db.execute(
            "UPDATE patients SET karte_summary_due=0,"
            "karte_summary_failed_at=NULL WHERE project_id=?",
            (project_id,))
        self.db.commit()

    def karte_summary_failed(self, project_id: int):
        """Record a failed 連携サマリー GET; the project sits out selection for KARTE_SUMMARY_BACKOFF_S."""
        self.db.execute(
            "UPDATE patients SET karte_summary_failed_at=? WHERE project_id=?",
            (time.time(), project_id))
        self.db.commit()

    def _karte_summary_due(self, project_id: int, flag: int):
        self.db.execute(
            "UPDATE patients SET karte_summary_due=? WHERE project_id=?",
            (flag, project_id))
        self.db.commit()

    def karte_summary_mark_due(self, project_id: int, due: bool = True):
        """Flag (or unflag) a project for a 連携サマリー GET on the next run.

        Durable: set by the save paths that stored new chat (unread root,
        reply job, self-probe import) and kept when the stage defers or
        fails a GET retryably; cleared by karte_summary_store, or by the
        stage on a non-retryable failure (the next new message re-arms
        it). The flag persists for a project whose karte_id is still
        unknown and fires once it is."""
        self._karte_summary_due(project_id, 1 if due else 0)

    def karte_summary_due(self) -> list:
        """Projects flagged for a summary GET that have a karte_id, newest last_seen first.

        Newest first so a project that just stored chat wins the per-tick
        cap over an older carry-over."""
        return [r["project_id"] for r in self.db.execute(
            "SELECT project_id FROM patients WHERE karte_summary_due=1 "
            "AND karte_id IS NOT NULL AND COALESCE(karte_summary_failed_at,0)"
            "<? ORDER BY last_seen DESC, project_id",
            (time.time() - KARTE_SUMMARY_BACKOFF_S,))]

    def karte_summary_current(self, project_id: int) -> dict | None:
        """Newest stored 連携サマリー content plus fetched_at; None if never fetched."""
        row = self._karte_summary_row(project_id)
        if not row:
            return None
        try:
            content = json.loads(row["content"])
            meta = json.loads(row["meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(content, dict):
            return None
        content["fetched_at"] = (meta.get("fetched_at")
                                 if isinstance(meta, dict) else None)
        return content

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

    def _job_add_tx(self, kind: str, project_id: int, message_id: int = 0,
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
            state='pending',payload=excluded.payload,attempts=0,
            next_try=excluded.next_try,updated_at=excluded.updated_at
          {where}
        """, (kind, project_id, message_id, parent_id,
              json.dumps(payload or {}), next_try or now, now, now))

    def job_add(self, kind: str, project_id: int, message_id: int = 0,
                parent_id: int | None = None, payload: dict | None = None,
                next_try: float = 0) -> int | None:
        # a done/failed job for the same key must be REVIVED by a new
        # request — plain INSERT OR IGNORE would silently drop re-import
        # requests forever. An in-flight job keeps its progress.
        cur = self._job_add_tx(kind, project_id, message_id, parent_id,
                               payload, next_try)
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
                  max_attempts: int = 8, *, payload: dict | None = None):
        if payload is not None:
            self.db.execute(
                "UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                (json.dumps(payload), job_id))
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

    def _semantic_attachments(self, message_id: int) -> list:
        return [dict(row) for row in self.db.execute(
            "SELECT attachment_id,file_id,name,bytes,sha256,state FROM attachments "
            "WHERE message_id=? AND state != 'withdrawn' "
            "ORDER BY attachment_id", (message_id,))]

    def _semantic_member(self, row) -> dict:
        """Canonical member dict of the thread-input digest contract —
        one shape shared by the per-thread and per-message fingerprints."""
        body = row["body_text"] or ""
        return {
            "message_id": row["message_id"],
            "parent_id": row["parent_id"],
            "revision": row["content_hash"]
                        or hashlib.sha256(body.encode()).hexdigest(),
            "body_state": row["body_state"] or "unknown",
            "reply_count": row["reply_count"] or 0,
            "sender_id": row["sender_id"],
            "sender_name": row["sender_name"] or "",
            "sender_type": row["sender_type"] or "",
            "profession": row["profession"] or "",
            "organization": row["organization"] or "",
            "posted_at": row["posted_at"] or "",
            "attachments": self._semantic_attachments(row["message_id"]),
        }

    @staticmethod
    def _semantic_digest(payload) -> str:
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()

    def _semantic_source_generation(self, project_id: int, root: int) -> str:
        """Digest the stored thread input used by a semantic seed.

        This is deliberately semantic-specific.  Generic fetch jobs retain
        their existing revive/reset contract; semantic retries need a stable
        input generation so replaying the same input cannot silently erase
        its accumulated attempts.
        """
        rows = self.db.execute(
            "SELECT message_id,parent_id,content_hash,body_state,body_text,"
            "reply_count,sender_id,sender_name,sender_type,profession,"
            "organization,posted_at FROM messages WHERE project_id=? "
            "AND (message_id=? OR parent_id=?) "
            "ORDER BY posted_at_ts,message_id", (project_id, root, root))
        members = [self._semantic_member(row) for row in rows]
        return self._semantic_digest(members)

    def _semantic_message_fingerprint(self, project_id: int,
                                      message_id: int) -> str | None:
        """Digest one touched row using the thread input fields."""
        row = self.db.execute(
            "SELECT message_id,parent_id,content_hash,body_state,body_text,"
            "reply_count,sender_id,sender_name,sender_type,profession,"
            "organization,posted_at FROM messages "
            "WHERE project_id=? AND message_id=?",
            (project_id, message_id)).fetchone()
        if row is None:
            return None
        return self._semantic_digest(self._semantic_member(row))

    def _semantic_seed_tx(self, project_id: int, message_ids: list,
                          origin: dict):
        """Durable semantic-eval seeding — caller holds `with self.db` so
        job creation commits (or rolls back) with the message and notify
        intent that triggered it (INV-06, spec §18.1). Keyed by thread
        ROOT so one job covers a post plus its replies; a new arrival on
        an evaluated thread revives the job as a new input generation."""
        # Pause holds execution, not durable intake; OFF callers do not seed.
        if not message_ids:
            return
        q = ("SELECT message_id,COALESCE(parent_id,message_id) r "
             "FROM messages WHERE project_id=? AND message_id IN ("
             + ",".join("?" * len(message_ids)) + ")")
        roots: dict[int, list] = {}
        for r in self.db.execute(q, [project_id, *message_ids]):
            roots.setdefault(r["r"], []).append(r["message_id"])
        now = time.time()
        for root, ids in roots.items():
            existing = self.db.execute("""
              SELECT job_id,state,attempts,payload FROM fetch_jobs
              WHERE kind='semantic' AND project_id=? AND message_id=?
            """, (project_id, root)).fetchone()
            eligible = isinstance(origin, dict) \
                and origin.get("event_id") is not None
            notification_free = isinstance(origin, dict) \
                and origin.get("notification_free") is True
            source_generation = self._semantic_source_generation(
                project_id, root)
            if existing is not None:
                try:
                    old_pl = json.loads(existing["payload"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    old_pl = {}
                old_pl = old_pl if isinstance(old_pl, dict) else {}
                stored_targets = old_pl.get("targets")
                old_targets = ({x for x in stored_targets if type(x) is int}
                               if isinstance(stored_targets, list) else set())
                old_origin = old_pl.get("origin")
                old_eligible = bool(old_pl.get("eligible"))
                old_notification_free = bool(old_pl.get(
                    "notification_free"))
                pl = dict(old_pl)
                pl["targets"] = sorted(old_targets | set(ids))
                # once a job descends from a notify intent it keeps that
                # provenance — a later history/replay merge must not
                # demote its drain priority nor erase which arrival
                # event seeded it (INV-20 keeps eligibility derived
                # from the stored event; 'eligible' is ordering
                # metadata only)
                if eligible or not (
                        isinstance(pl.get("origin"), dict)
                        and pl["origin"].get("event_id") is not None):
                    pl["origin"] = origin
                if eligible:
                    pl["eligible"] = True
                    # An actual arrival event is the explicit opt-in for a
                    # notification.  It supersedes a replay's suppression
                    # marker, even when the old origin is being replaced.
                    pl.pop("notification_free", None)
                elif notification_free:
                    # Keep this marker at the payload level as well as in
                    # the incoming origin: a prior eligible origin may be
                    # retained for provenance, but this seed must still be
                    # ineligible for notification generation.
                    pl["notification_free"] = True
                input_changed = (old_targets != set(pl["targets"])
                                 or pl.get("source_generation")
                                 != source_generation)
                payload_changed = (input_changed
                                   or old_origin != pl.get("origin")
                                   or old_eligible != bool(pl.get("eligible"))
                                   or old_notification_free
                                   != bool(pl.get("notification_free")))
                # A missing generation is a legacy row.  Add one before any
                # worker can observe it, so old and new payloads are never
                # confused by an ID-only check.
                if payload_changed or existing["state"] != "pending" \
                        or not isinstance(pl.get("generation"), str):
                    pl["generation"] = uuid.uuid4().hex
                pl["source_generation"] = source_generation
                attempts = (0 if input_changed
                             else int(existing["attempts"] or 0))
                if input_changed:
                    self._invalidate_thread_projections(project_id, root)
                    # A source edit is a new budget and may not inherit a
                    # human retry extension or the command token that
                    # invalidated the prior worker.
                    pl.pop("manual_attempt_limit", None)
                    pl.pop("retry_command_id", None)
                    state = "pending"
                else:
                    from semantic_runtime import attempt_limit
                    state = "pending"
                    if (existing["state"] in {"failed", "pending"}
                            and attempts >= attempt_limit(pl)):
                        # Re-seeding the same exhausted input must not revive
                        # a job that only an explicit human retry may extend.
                        state = "failed"
                self.db.execute(
                    "UPDATE fetch_jobs SET state=?,payload=?,"
                    "attempts=?,next_try=?,updated_at=? WHERE job_id=?",
                    (state, json.dumps(pl, ensure_ascii=False, sort_keys=True),
                     attempts, now, now, existing["job_id"]))
            else:
                payload = {"targets": sorted(set(ids)),
                           "origin": origin,
                           "generation": uuid.uuid4().hex,
                           "source_generation": source_generation}
                if eligible:
                    payload["eligible"] = True
                elif notification_free:
                    payload["notification_free"] = True
                self.db.execute("""
                  INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,
                    payload,state,next_try,created_at,updated_at)
                  VALUES(?,?,?,?,?,'pending',?,?,?)
                """, ("semantic", project_id, root, None,
                      json.dumps(payload, ensure_ascii=False, sort_keys=True),
                      now, now, now))

    def semantic_seed(self, project_id: int, message_ids: list,
                      origin: dict):
        """Standalone seed for explicit replay — same job shape as the
        ingest path, committed on its own (spec §18.5)."""
        with self.db:
            self._semantic_seed_tx(project_id, message_ids, origin)

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

    def attachment_saved(self, attachment_id: int, path: str,
                         nbytes: int, sha256: str, semantic: bool = False):
        with self.db:
            row = self.db.execute(
                "SELECT m.project_id,m.message_id,COALESCE(m.parent_id,m.message_id) root "
                "FROM attachments a "
                "JOIN messages m ON m.message_id=a.message_id WHERE attachment_id=?",
                (attachment_id,)).fetchone()
            before = (self._semantic_message_fingerprint(
                row["project_id"], row["message_id"])
                if row and (semantic or self._has_canonical_projections()) else None)
            now = time.time()
            self.db.execute("""
              UPDATE attachments SET local_path=?,bytes=?,sha256=?,
                state='downloaded',downloaded_at=?,error=NULL
              WHERE attachment_id=?
            """, (path, nbytes, sha256, now, attachment_id))
            if row and before is not None and before != self._semantic_message_fingerprint(
                    row["project_id"], row["message_id"]):
                self._invalidate_thread_projections(row["project_id"], row["root"])
                if semantic:
                    self._semantic_seed_tx(row["project_id"], [row["message_id"]],
                                           {"source": "attachment"})
            if row:
                self._attachment_followup_tx(row, attachment_id, now)

    def _attachment_followup_tx(self, row, attachment_id: int, now: float):
        """The body notice for this message was already ACCEPTED before
        this file finished downloading — it provably went out without
        the file, so queue an attachment-only follow-up receipt (F11).
        Deduped on payload.attachment_id: one follow-up per file ever."""
        sent = self.db.execute("""
          SELECT 1 FROM notify_outbox o
          WHERE o.kind='new_messages' AND o.state='accepted'
            AND o.updated_at < ? AND CASE
              WHEN EXISTS(SELECT 1 FROM notification_intent_batches b
                          WHERE b.event_id=o.event_id) THEN
                EXISTS(SELECT 1 FROM notification_intent_batches b
                  JOIN notification_intent_cards ic ON ic.event_id=b.event_id
                  JOIN json_each(b.frozen_payload,'$.message_ids') frozen
                  JOIN json_each(ic.coverage) covered ON covered.value=frozen.value
                  WHERE b.event_id=o.event_id AND ic.state='delivered'
                    AND frozen.type='integer' AND frozen.value=?)
              ELSE json_valid(o.payload) AND EXISTS(
                SELECT 1 FROM json_each(json_extract(o.payload,'$.message_ids'))
                WHERE value=?) END
          LIMIT 1""", (now, row["message_id"], row["message_id"])).fetchone()
        if not sent:
            return
        _enqueue_attachment_followup_tx(
            self.db, attachment_id, row["project_id"], row["message_id"], now)

    def attachment_failed(self, attachment_id: int, kind: str,
                          retry_in: float = 900, max_attempts: int = 6,
                          semantic: bool = False):
        """Classified failure recording (F11). PERMANENT kinds (4xx other
        than 408/429, oversize, policy blocks) can never succeed on the
        same url -> 'failed' now; a fresh signed url on re-save revives
        the row (see _save_attachments). Transient kinds back off to the
        attempt cap as before."""
        permanent = (
            kind in _ATTACH_PERMANENT
            or (kind.startswith("http_")
                and kind[5:].isdigit()
                and 400 <= int(kind[5:]) < 500
                and int(kind[5:]) not in (408, 429)))
        with self.db:
            source = self.db.execute(
                "SELECT m.project_id,m.message_id,a.state,"
                "COALESCE(m.parent_id,m.message_id) root FROM attachments a "
                "JOIN messages m ON m.message_id=a.message_id WHERE attachment_id=?",
                (attachment_id,)).fetchone() if (
                    semantic or self._has_canonical_projections()) else None
            self.db.execute("""
              UPDATE attachments SET attempts=attempts+1,next_try=?,error=?,
                state=CASE WHEN ? OR attempts+1>=? THEN 'failed'
                           ELSE 'pending' END
              WHERE attachment_id=?
            """, (time.time() + retry_in, kind[:80], permanent,
                  max_attempts, attachment_id))
            if source:
                state = self.db.execute("SELECT state FROM attachments WHERE attachment_id=?",
                                        (attachment_id,)).fetchone()[0]
                if state != source["state"]:
                    self._invalidate_thread_projections(source["project_id"], source["root"])
                    if semantic:
                        self._semantic_seed_tx(source["project_id"], [source["message_id"]],
                                               {"source": "attachment"})

    def attachments_due(self, limit: int = 50,
                        priority_mids: list[int] | None = None) -> list:
        """priority_mids: message_ids whose attachments jump the queue —
        unsent notify events need their files before flush() posts."""
        prio = [m for m in (priority_mids or []) if type(m) is int][:500]
        if not prio:
            return self.db.execute("""
              SELECT attachment_id,message_id,file_id,name,url FROM attachments
              WHERE state='pending' AND url != ''
                AND COALESCE(next_try,0) <= ?
              ORDER BY attachment_id LIMIT ?
            """, (time.time(), limit)).fetchall()
        ph = ",".join("?" * len(prio))
        return self.db.execute(f"""
          SELECT attachment_id,message_id,file_id,name,url FROM attachments
          WHERE state='pending' AND url != ''
            AND COALESCE(next_try,0) <= ?
          ORDER BY CASE WHEN message_id IN ({ph}) THEN 0 ELSE 1 END,
            attachment_id LIMIT ?
        """, (time.time(), *prio, limit)).fetchall()

    def pending_notify_message_ids(self) -> list[int]:
        """message_ids referenced by unsent notify events — their
        attachments jump the download queue so flush() can attach them."""
        out = []
        for r in self.db.execute("""
          SELECT payload FROM notify_outbox
          WHERE kind='new_messages' AND state IN ('pending','failed')
            AND next_try IS NOT NULL
        """):
            try:
                p = json.loads(r["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            ids = p.get("message_ids") if isinstance(p, dict) else None
            if isinstance(ids, list):
                out.extend(m for m in ids if type(m) is int)
        return out

    # ---------- notify outbox ----------

    def outbox_add_tx(self, kind: str, project_id: int | None,
                      payload: dict, next_try: float | None = None) -> int:
        """outbox_add's INSERT without the commit — callers holding
        `with self.db` can land a notification intent in the same
        transaction as the artifacts that justify it (spec §18.3).
        next_try delays first pickup (scheduled digests)."""
        return self._outbox_insert(kind, project_id, payload, next_try)

    def outbox_add(self, kind: str, project_id: int | None, payload: dict) -> int:
        rid = self.outbox_add_tx(kind, project_id, payload)
        self.db.commit()
        return rid

    def outbox_due(self, limit: int = 20) -> list:
        return self.db.execute("""
          SELECT event_id,kind,project_id,payload,attempts,progress,route
          FROM notify_outbox
          WHERE state IN ('pending','failed') AND next_try <= ?
          ORDER BY event_id LIMIT ?
        """, (time.time(), limit)).fetchall()

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

    def outbox_hold(self, event_id: int):
        """Quarantine an event whose partial-send receipt is unsafe."""
        self.db.execute(
            "UPDATE notify_outbox SET state='failed',next_try=NULL,updated_at=? "
            "WHERE event_id=?", (time.time(), event_id))
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
        q = "SELECT * FROM artifacts WHERE kind=?"
        params: list = [kind]
        if project_id is not None:
            q += " AND project_id=?"
            params.append(project_id)
        if message_id is not None:
            q += " AND message_id=?"
            params.append(message_id)
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
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    os.chmod(dest_dir, 0o700)
    dest = os.path.join(dest_dir, "ledger-snapshot.db")
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".snapshot.", suffix=".tmp")
    os.close(fd)  # private from creation, including throughout the backup
    try:
        src = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                              uri=True)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
                with dst:
                    dst.execute("INSERT OR REPLACE INTO snapshot_meta("
                                "singleton,generation_id,generated_at) "
                                "VALUES(1,?,?)",
                                (str(uuid.uuid4()), time.time()))
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
        finally:
            src.close()
        if not valid_mcs_db(tmp):
            return None
        publish_tmp(tmp, dest)
        # A reader keeps its old inode or opens the complete new generation.
        return dest
    finally:
        with suppress(OSError):
            os.unlink(tmp)


def valid_mcs_db(path: str) -> bool:
    """Validate a static recovery/publication candidate without sidecars."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        db = sqlite3.connect(uri, uri=True)
        try:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" \
                    or db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                return False
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if not 0 <= version <= SCHEMA_VERSION:
                return False
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if {"attachments_v1", "read_marks_v1"} & tables:
                return False
            # _migrate_body adds derived columns, not these source fields.
            # Older backups may lack tables that _init creates on reopen.
            core_columns = {
                "runs": {"run_id", "started_at", "finished_at",
                         "snapshot_ts", "status", "error"},
                "patients": {"project_id", "project_type", "patient_name",
                             "disease", "station_name", "url", "last_seen"},
                "messages": {"message_id", "project_id", "parent_id",
                             "sender_id", "sender_name", "sender_type",
                             "profession", "organization", "posted_at",
                             "body_html", "reply_count", "first_seen"},
            }
            if not core_columns.keys() <= tables:
                return False
            if version >= 5 and not {
                    "attachments", "notify_outbox", "read_marks",
                    "artifacts", "fetch_jobs"} <= tables:
                return False
            if version >= 8 and "message_metadata" not in tables:
                return False
            base_columns = {
                **core_columns,
                "attachments": {"attachment_id", "message_id", "file_id",
                                "name", "url", "local_path", "bytes",
                                "sha256", "state", "downloaded_at",
                                "created_at"},
                "read_marks": {"project_id", "snapshot_ts", "marked_at"},
                "notify_outbox": {"event_id", "kind", "project_id", "payload",
                                  "state", "attempts", "next_try",
                                  "accepted_ref", "created_at", "updated_at"},
                "artifacts": {"artifact_id", "kind", "project_id", "message_id",
                              "content", "model", "meta", "created_at"},
                "fetch_jobs": {"job_id", "kind", "project_id", "message_id",
                               "parent_id", "payload", "state", "attempts",
                               "next_try", "created_at", "updated_at"},
            }
            if "message_metadata" in tables:
                base_columns["message_metadata"] = {
                    "message_id", "source", "content", "checked_at", "last_error"}
            for table, required in base_columns.items():
                if table not in tables:
                    continue  # _init creates absent tables for older backups.
                columns = {row[1] for row in db.execute(
                    f"PRAGMA table_info({table})")}
                if table == "attachments" and "attachment_id" not in columns:
                    required = {"message_id", "file_id", "name", "url",
                                "downloaded_path", "first_seen"}
                elif table == "read_marks" and "id" in columns:
                    required = required | {"status"}
                if not required <= columns:
                    return False
                # Match the ambiguous-key fence in Ledger._preflight before
                # a recovery candidate can replace the live database.
                key_check = {
                    "attachments": ("attachment_id", "message_id,file_id"),
                    "read_marks": ("id", "project_id,snapshot_ts"),
                }.get(table)
                if key_check and key_check[0] in columns and db.execute(
                        f"SELECT 1 FROM {table} GROUP BY {key_check[1]} "
                        "HAVING COUNT(*) > 1 LIMIT 1").fetchone():
                    return False
            return True
        finally:
            db.close()
    except (OSError, sqlite3.DatabaseError):
        return False
