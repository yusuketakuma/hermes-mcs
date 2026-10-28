"""Own ledger schema, migrations and relation guards on a borrowed connection.

The initializer owns its DDL/backfill transactions. The Ledger constructor owns
connection creation, connection settings and close; read audits own no transaction.
The caller supplies the supported version, whose static literal remains in ledger.
"""

import sqlite3
import re
import time
from contextlib import suppress

from mcs_queries import posted_epoch
from mcs_util import html_to_text, loads_dict


class MigrationError(RuntimeError):
    """Fail closed when an existing schema cannot be migrated losslessly."""


_GUARD_RELATIONS = (
    ("artifacts", "message_id, project_id",
     "a.message_id IS NOT NULL AND NOT EXISTS "
     "(SELECT 1 FROM messages m WHERE m.message_id=a.message_id "
     "AND (a.project_id IS NULL OR m.project_id=a.project_id))"),
    ("attachments", "message_id",
     "NOT EXISTS (SELECT 1 FROM messages m "
     "WHERE m.message_id=a.message_id)"),
)
# AFTER UPDATE OF ... WHEN: only a real change to an indexed column
# rewrites the FTS row (see _install_fts_update_trigger)
_FTS_AU_MARK = "AFTER UPDATE OF body_text, sender_name ON messages"
_FTS_AU_SQL = f"""
  CREATE TRIGGER messages_au
  {_FTS_AU_MARK}
  WHEN old.body_text IS NOT new.body_text
    OR old.sender_name IS NOT new.sender_name
  BEGIN
    UPDATE messages_fts SET body_text=new.body_text,
      sender_name=new.sender_name
    WHERE rowid=old.message_id;
    INSERT INTO messages_fts(rowid, body_text, sender_name)
    SELECT new.message_id, new.body_text, new.sender_name
    WHERE NOT EXISTS(SELECT 1 FROM messages_fts
                     WHERE rowid=new.message_id);
  END"""
# a shadow relation's orphan count is health evidence, not a gate: an
# open within this window of the last confirmed recount trusts it
RELATION_RECOUNT_S = 3600
_GUARD_TRIGGERS = frozenset(
    [f"g1_{table}_msg_{suffix}" for table, _, _ in _GUARD_RELATIONS
     for suffix in ("ins", "upd")]
    + [f"relation_audit_messages_{suffix}" for suffix in ("del", "upd", "replace")])


REQUEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(
  request_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL,
  source_hash TEXT NOT NULL, title TEXT NOT NULL,
  assignee TEXT, due_date TEXT,
  status TEXT NOT NULL CHECK(status IN ('open','in_progress','done','cancelled')),
  revision INTEGER NOT NULL CHECK(revision > 0),
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_requests_project ON requests(project_id,request_id);
CREATE TABLE IF NOT EXISTS command_receipts(
  command_id TEXT PRIMARY KEY NOT NULL, payload_hash TEXT NOT NULL,
  project_id INTEGER, request_id INTEGER,
  outcome TEXT NOT NULL CHECK(outcome IN ('applied','rejected')),
  receipt_json TEXT NOT NULL, processed_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_receipts_request ON command_receipts(request_id);
CREATE TABLE IF NOT EXISTS snapshot_meta(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  generation_id TEXT NOT NULL, generated_at REAL NOT NULL);
"""


NOTIFICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_cards(
  card_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK(kind IN ('thread','signal','digest','patient_history')),
  project_id INTEGER,
  root_message_id INTEGER,
  anchor_key TEXT NOT NULL,
  profile TEXT, application_id TEXT, guild_id TEXT, channel_id TEXT,
  transport TEXT NOT NULL DEFAULT 'discord',
  team_id TEXT,
  message_id TEXT, thread_id TEXT,
  thread_state TEXT NOT NULL DEFAULT 'none'
    CHECK(thread_state IN ('none','created','failed','deleted')),
  source_generation INTEGER NOT NULL DEFAULT 1,
  source_fp TEXT,
  presentation_generation INTEGER NOT NULL DEFAULT 1,
  content_fp TEXT,
  ui_revision INTEGER NOT NULL DEFAULT 1,
  desired_render_rev INTEGER NOT NULL DEFAULT 0,
  applied_render_rev INTEGER NOT NULL DEFAULT 0,
  ui_state TEXT,
  delivery_state TEXT NOT NULL DEFAULT 'pending'
    CHECK(delivery_state IN ('pending','delivered','update_failed',
                             'delivery_unknown','message_deleted','revoked')),
  last_delivery_error TEXT,
  revoked_at REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  layout INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS idx_ncards_state
  ON notification_cards(delivery_state);
CREATE TABLE IF NOT EXISTS notification_intent_batches(
  event_id INTEGER PRIMARY KEY REFERENCES notify_outbox(event_id),
  frozen_payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  route_epoch INTEGER NOT NULL,
  transport TEXT NOT NULL DEFAULT 'discord',
  scope_json TEXT,
  sealed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notification_intent_cards(
  event_id INTEGER NOT NULL REFERENCES notify_outbox(event_id),
  card_id INTEGER NOT NULL REFERENCES notification_cards(card_id),
  coverage TEXT NOT NULL,
  required_render_rev INTEGER NOT NULL DEFAULT 0,
  delivery_id TEXT REFERENCES notification_renders(delivery_id),
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK(state IN ('pending','delivered','suppressed')),
  PRIMARY KEY(event_id, card_id));
CREATE INDEX IF NOT EXISTS idx_nic_card
  ON notification_intent_cards(card_id, state);
CREATE TABLE IF NOT EXISTS notification_renders(
  delivery_id TEXT PRIMARY KEY,
  card_id INTEGER REFERENCES notification_cards(card_id),
  op TEXT NOT NULL CHECK(op IN ('create','update','revoke','notice')),
  render_rev INTEGER NOT NULL,
  manifest_id INTEGER REFERENCES notification_view_manifests(manifest_id),
  route_epoch INTEGER NOT NULL,
  profile TEXT, application_id TEXT, guild_id TEXT, channel_id TEXT,
  transport TEXT NOT NULL DEFAULT 'discord',
  team_id TEXT,
  spec_json TEXT,
  spec_published INTEGER NOT NULL DEFAULT 0,
  first_published_at REAL,
  payload_hash TEXT NOT NULL,
  correlation TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL DEFAULT 'queued'
    CHECK(state IN ('queued','sending','delivered','not_sent',
                    'unknown','held','cancelled')),
  parts_state TEXT NOT NULL DEFAULT 'none',
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  UNIQUE(card_id, render_rev));
CREATE INDEX IF NOT EXISTS idx_nrenders_card
  ON notification_renders(card_id);
CREATE TABLE IF NOT EXISTS notification_delivery_attempts(
  attempt_id TEXT PRIMARY KEY,
  delivery_id TEXT NOT NULL REFERENCES notification_renders(delivery_id),
  begin_command_id TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL
    CHECK(state IN ('granted','delivered','not_sent','unknown')),
  message_id TEXT, error_code TEXT,
  worker_id TEXT,
  created_at REAL NOT NULL, finished_at REAL);
CREATE INDEX IF NOT EXISTS idx_nattempts_delivery
  ON notification_delivery_attempts(delivery_id);
-- Durable per-render delivery plan: card/thread/body_part/
-- attachment_part identities bound to the sealed spec (generation,
-- render_rev, scope, ordered hashes). A delivered card never implies
-- complete delivery — full completion requires every planned part
-- delivered. 'held' marks dependents blocked by a failed thread.
CREATE TABLE IF NOT EXISTS notification_render_parts(
  delivery_id TEXT NOT NULL,
  part_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('card','thread','body_part',
                                    'attachment_part')),
  idx INTEGER NOT NULL,
  payload_sha256 TEXT, bytes INTEGER, name TEXT, attachment_id INTEGER,
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK(state IN ('pending','delivered','not_sent','unknown','held')),
  remote_id TEXT, error_code TEXT, attempt_id TEXT,
  updated_at REAL,
  PRIMARY KEY(delivery_id, part_id));
CREATE TABLE IF NOT EXISTS notification_view_manifests(
  manifest_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id INTEGER NOT NULL REFERENCES notification_cards(card_id),
  render_rev INTEGER NOT NULL,
  source_generation INTEGER NOT NULL,
  presentation_generation INTEGER NOT NULL,
  digest INTEGER NOT NULL DEFAULT 0,
  shown TEXT NOT NULL,
  invalidated INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_nmanifests_card
  ON notification_view_manifests(card_id);
CREATE TABLE IF NOT EXISTS notification_acknowledgements(
  ack_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id INTEGER NOT NULL REFERENCES notification_cards(card_id),
  manifest_id INTEGER NOT NULL REFERENCES notification_view_manifests(manifest_id),
  actor TEXT NOT NULL,
  command_id TEXT NOT NULL UNIQUE,
  receipt_ref TEXT,
  created_at REAL NOT NULL,
  withdrawn_at REAL);
CREATE TABLE IF NOT EXISTS notification_triage(
  card_id INTEGER PRIMARY KEY REFERENCES notification_cards(card_id),
  owner TEXT, defer_until REAL,
  state TEXT NOT NULL DEFAULT 'open'
    CHECK(state IN ('open','assigned','deferred')),
  revision INTEGER NOT NULL DEFAULT 0,
  last_actor TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS notification_action_tokens(
  token TEXT PRIMARY KEY,
  card_id INTEGER NOT NULL REFERENCES notification_cards(card_id),
  action TEXT NOT NULL,
  params TEXT,
  need_source_gen INTEGER, need_manifest_id INTEGER,
  need_ui_rev INTEGER, need_request_rev INTEGER,
  expires_at REAL NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ntokens_card
  ON notification_action_tokens(card_id);
CREATE TABLE IF NOT EXISTS notification_meta(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  notify_dirty INTEGER NOT NULL DEFAULT 0);
-- Post-restore holds: a DB rewind can erase attempt/render rows that
-- the delivery journal proves had external effects. Each row pins one
-- held scope until an operator (ops.card_resolve) or a factual receipt
-- releases it — a held card never re-issues a render.
CREATE TABLE IF NOT EXISTS notification_restore_holds(
  hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id INTEGER REFERENCES notification_cards(card_id),
  delivery_id TEXT,
  attempt_id TEXT,
  events_json TEXT,
  reason TEXT NOT NULL,
  scope_json TEXT,
  held_at REAL NOT NULL,
  released_at REAL,
  release_command_id TEXT);
CREATE INDEX IF NOT EXISTS idx_nholds_active
  ON notification_restore_holds(card_id, released_at);
-- ⏰ one reminder per task, stage and due date — the durable once-only
-- marker (a changed due date re-arms). event_id NULL = recorded without
-- sending (first-activation baseline), and request_id 0 / 'baseline' marks
-- that the baseline ran.
CREATE TABLE IF NOT EXISTS notification_task_reminders(
  request_id INTEGER NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('due','overdue','baseline')),
  due_date TEXT NOT NULL,
  event_id INTEGER,
  created_at REAL NOT NULL,
  PRIMARY KEY(request_id, stage, due_date));
"""


def preflight(db, tables: set[str], *, version: int, schema_version: int):
    """Reject ambiguous existing rows before any persistent change."""
    if version > schema_version:
        raise MigrationError(f"unsupported schema version {version}")
    if {"attachments_v1", "read_marks_v1"} & tables:
        raise MigrationError("interrupted migration requires review")
    errors = {"attachments": "duplicate attachment keys",
              "read_marks": "duplicate read mark keys"}
    for table in KEY_FENCES:
        if table not in tables:
            continue
        cols = {r[1] for r in db.execute(
            f'PRAGMA table_info("{table}")')}
        if _key_fence_breached(db, table, cols):
            raise MigrationError(errors[table])


def initialize(db, *, schema_version: int) -> bool:
    """Migrate owned connections, restoring FK settings even after rollback."""
    row = db.execute("SELECT sql FROM sqlite_schema WHERE name='notification_cards'").fetchone()
    rebuild = row is not None and "'patient_history'" not in row[0]
    foreign_keys = db.execute("PRAGMA foreign_keys").fetchone()[0]
    legacy = db.execute("PRAGMA legacy_alter_table").fetchone()[0]
    if db.in_transaction:
        raise MigrationError("schema migration requires an idle connection")
    try:
        if rebuild:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("PRAGMA legacy_alter_table=OFF")
        return _initialize(db, schema_version=schema_version)
    finally:
        if db.in_transaction:
            db.rollback()
        db.execute(f"PRAGMA foreign_keys={foreign_keys}")
        db.execute(f"PRAGMA legacy_alter_table={legacy}")


def _initialize(db, *, schema_version: int) -> bool:
    db.executescript("""
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
          reason_code TEXT,
          UNIQUE(kind, project_id, message_id));
        """)
    had_fts = db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='messages_fts'"
    ).fetchone() is not None
    # A newly created FTS table and its backfill must commit together.
    # Otherwise a failed backfill looks already initialized on reopen.
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("""
              CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
              USING fts5(body_text, sender_name)""")
        fts = True
    except sqlite3.OperationalError:
        fts = False
    # adds v3 columns first — indexes/triggers depend on them
    _migrate(db, schema_version=schema_version, fts=fts, fts_created=fts and not had_fts)
    db.executescript("""
          CREATE INDEX IF NOT EXISTS idx_patients_name ON patients(patient_name) WHERE patient_name IS NOT NULL;
          CREATE INDEX IF NOT EXISTS idx_messages_project_time
            ON messages(project_id, posted_at_ts);
          CREATE INDEX IF NOT EXISTS idx_messages_notified
            ON messages(notified_at) WHERE notified_at IS NOT NULL;
          CREATE INDEX IF NOT EXISTS idx_messages_posted_ts
            ON messages(posted_at_ts);
          CREATE INDEX IF NOT EXISTS idx_messages_parent
            ON messages(parent_id);
          CREATE INDEX IF NOT EXISTS idx_messages_thread_time
            ON messages(project_id, COALESCE(parent_id,message_id), posted_at_ts);
          CREATE INDEX IF NOT EXISTS idx_artifacts_lookup
            ON artifacts(kind, project_id, message_id);
          CREATE INDEX IF NOT EXISTS idx_semantic_control_epoch
            ON artifacts(project_id, artifact_id DESC) WHERE kind='semantic_control';
          CREATE INDEX IF NOT EXISTS idx_requests_source_msg
            ON requests(source_message_id);
          CREATE INDEX IF NOT EXISTS idx_artifacts_kind_msg
            ON artifacts(kind, message_id);
          CREATE INDEX IF NOT EXISTS idx_artifacts_kind_created
            ON artifacts(kind, created_at);
          CREATE INDEX IF NOT EXISTS idx_fetch_jobs_pending_due
            ON fetch_jobs(kind, next_try) WHERE state='pending';
          CREATE INDEX IF NOT EXISTS idx_fetch_jobs_control_scan
            ON fetch_jobs(kind, project_id, state, job_id);
          CREATE INDEX IF NOT EXISTS idx_attachments_pending
            ON attachments(attachment_id) WHERE state='pending';
          CREATE INDEX IF NOT EXISTS idx_outbox_due_states
            ON notify_outbox(event_id) WHERE state IN ('pending','failed');
          CREATE INDEX IF NOT EXISTS idx_outbox_kind_created
            ON notify_outbox(kind, created_at);
          CREATE INDEX IF NOT EXISTS idx_fetch_jobs_failed_recent
            ON fetch_jobs(updated_at, kind) WHERE state='failed';
          CREATE UNIQUE INDEX IF NOT EXISTS uq_attachments_msg_file
            ON attachments(message_id, file_id);
          CREATE INDEX IF NOT EXISTS idx_messages_reply_first_seen
            ON messages(first_seen) WHERE parent_id IS NOT NULL;
          CREATE INDEX IF NOT EXISTS idx_messages_reply_open
            ON messages(parent_id, project_id) WHERE parent_id IS NOT NULL
              AND (body_state IS NULL OR body_state NOT IN ('full','deleted'));
          CREATE INDEX IF NOT EXISTS idx_nrenders_state_updated
            ON notification_renders(state, updated_at);
          CREATE INDEX IF NOT EXISTS idx_nparts_pending
            ON notification_render_parts(delivery_id) WHERE state='pending';
        """)
    if fts:
        db.executescript("""
              CREATE TRIGGER IF NOT EXISTS messages_ai
              AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, body_text, sender_name)
                VALUES (new.message_id, new.body_text, new.sender_name);
              END;
            """)
        _install_fts_update_trigger(db)
    # only _migrate moves user_version, inside its own commit (Oracle F03)
    return fts


def _install_fts_update_trigger(db) -> None:
    """Keep messages_fts in step with indexed columns only.

        Re-observing a stored post rewrites updated_seen/reply_count on
        every tick; a column-less AFTER UPDATE trigger rewrote its FTS row
        each time. The trigger fires only when body_text or sender_name
        really changed. A DB from before this rule carries the old
        trigger, which is replaced in one transaction (additive DDL,
        schema version unchanged; an older writer keeps the new one
        through its CREATE TRIGGER IF NOT EXISTS)."""
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='messages_au'").fetchone()
    if row is not None and _FTS_AU_MARK in (row[0] or ""):
        return
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("DROP TRIGGER IF EXISTS messages_au")
        db.execute(_FTS_AU_SQL)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def relation_guards_current(db) -> bool:
    """True when no recorded guard decision or evidence would change.

        Read-only (WAL readers never block writers). An ``enforce`` relation
        is trusted without a recount while its triggers exist and its
        ``ledger_relation_audit`` mode still matches: the only paths that can
        orphan an enforced child (a messages DELETE, key UPDATE or replacement
        in another project) clear that row through the ``relation_audit_messages_*`` triggers. ``shadow``
        relations are recounted because ``existing_count`` is health evidence
        — at most once per RELATION_RECOUNT_S: every process opens the
        ledger, and a full orphan COUNT on each open grew with the corpus.
        A recount that confirms the evidence refreshes its timestamp.
        """
    recounted = False
    db.execute("BEGIN")
    try:
        try:
            guards = {r[0]: (r[1], r[2]) for r in db.execute(
                "SELECT relation,mode,existing_count FROM ledger_relation_guards")}
            verified = dict(db.execute(
                "SELECT relation,mode FROM ledger_relation_audit").fetchall())
        except sqlite3.OperationalError:
            return False
        triggers = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
        if not _GUARD_TRIGGERS <= triggers:
            return False
        fresh = _relation_recount_fresh(db, _relation_marker(db))
        for table, _columns, condition in _GUARD_RELATIONS:
            mode, existing = guards.get(table, (None, None))
            if mode is None or verified.get(table) != mode:
                return False
            if mode == "shadow" and not fresh:
                recounted = True
                if db.execute(
                        f"SELECT COUNT(*) FROM {table} a WHERE {condition}"
                ).fetchone()[0] != existing:
                    return False
    finally:
        db.commit()
    if recounted:
        _mark_relation_recount(db)
    return True


def _relation_marker(db) -> str:
    """Cheap evidence of changes that can move a shadow count without
    clearing ledger_relation_audit: new shadow observations, and new
    parent messages at any id (which can resolve an orphan). Child deletions
    are covered only by the RELATION_RECOUNT_S bound."""
    shadow = db.execute(
        "SELECT COALESCE(SUM(shadow_count),0) FROM ledger_relation_guards"
    ).fetchone()[0]
    # MCS ids are not insertion-ordered: a backfilled parent can land
    # below MAX(message_id), so the row count moves the marker too
    newest, total = db.execute(
        "SELECT MAX(message_id), COUNT(*) FROM messages").fetchone()
    return f"{shadow}:{newest}:{total}"


def _relation_recount_fresh(db, marker: str) -> bool:
    """True when shadow evidence was confirmed within RELATION_RECOUNT_S
    and nothing in ``marker`` moved since."""
    try:
        row = db.execute(
            "SELECT counted_at,marker FROM ledger_relation_recount "
                "WHERE singleton=1").fetchone()
    except sqlite3.OperationalError:
        return False
    if row is None or row[1] != marker:
        return False
    at = row[0]
    return (isinstance(at, int | float) and not isinstance(at, bool)
            and 0 <= time.time() - at < RELATION_RECOUNT_S)


def _mark_relation_recount(db) -> None:
    """Record a confirmed recount (best effort: only skips future work)."""
    with suppress(sqlite3.Error):
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS ledger_relation_recount("
                    "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                    "counted_at REAL NOT NULL, marker TEXT NOT NULL)")
            db.execute(
                "INSERT INTO ledger_relation_recount(singleton,counted_at,marker) "
                    "VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                    "counted_at=excluded.counted_at,marker=excluded.marker",
                (time.time(), _relation_marker(db)))


def install_relation_guards(db, *, new_database: bool = False,
                             audit: bool = False) -> None:
    # Audit and activation share a writer lock: no unguarded write can
    # slip between the zero-count check and trigger installation.
    # Dirty relations stay shadow-only, including after observed shadow
    # violations are repaired; their counters require owner review.
    # The writer lock is taken only when the read-only check finds a
    # decision/evidence change; the recount then repeats under the lock.
    if not audit and relation_guards_current(db):
        return
    relations = _GUARD_RELATIONS
    db.execute("BEGIN IMMEDIATE")
    with db:
        db.execute("""
                CREATE TABLE IF NOT EXISTS ledger_relation_guards(
                  relation TEXT PRIMARY KEY,
                  mode TEXT NOT NULL CHECK(mode IN ('shadow','enforce')),
                  existing_count INTEGER NOT NULL,
                  shadow_count INTEGER NOT NULL DEFAULT 0)
            """)
        for table, columns, condition in relations:
            count = db.execute(
                f"SELECT COUNT(*) FROM {table} a WHERE {condition}"
            ).fetchone()[0]
            db.execute("""
                    INSERT INTO ledger_relation_guards(relation,mode,existing_count)
                    VALUES(?, ?, ?)
                    ON CONFLICT(relation) DO UPDATE SET
                      existing_count=excluded.existing_count,
                      mode=CASE WHEN ledger_relation_guards.mode='enforce'
                                     AND excluded.existing_count=0
                                     AND ledger_relation_guards.shadow_count=0
                                THEN 'enforce' ELSE 'shadow' END
                    WHERE existing_count!=excluded.existing_count
                       OR mode!=CASE WHEN mode='enforce'
                                     AND excluded.existing_count=0 AND shadow_count=0
                                     THEN 'enforce' ELSE 'shadow' END
                """, (table, "enforce" if new_database and not count else "shadow", count))
            for suffix, event in (
                    ("ins", "INSERT"), ("upd", f"UPDATE OF {columns}")):
                db.execute(f"""
                        CREATE TRIGGER IF NOT EXISTS g1_{table}_msg_{suffix}
                        BEFORE {event} ON {table}
                        WHEN {condition.replace('a.', 'NEW.')}
                        BEGIN
                          SELECT RAISE(ABORT,'g1: {table} message relation')
                          WHERE (SELECT mode FROM ledger_relation_guards
                                 WHERE relation='{table}')='enforce';
                          UPDATE ledger_relation_guards
                          SET shadow_count=shadow_count+1
                          WHERE relation='{table}' AND mode='shadow';
                        END
                    """)
        db.execute("""
                CREATE TABLE IF NOT EXISTS ledger_relation_audit(
                  relation TEXT PRIMARY KEY, mode TEXT NOT NULL)
            """)
        db.execute("DELETE FROM ledger_relation_audit")
        db.execute("""
                INSERT INTO ledger_relation_audit(relation,mode)
                SELECT relation,mode FROM ledger_relation_guards
                WHERE relation IN ('artifacts','attachments')
            """)
        # Parent-side changes can orphan children the g1 triggers already
        # admitted; they invalidate the audit so the next open recounts.
        for suffix, event, when in (
                ("del", "DELETE", "1"),
                ("upd", "UPDATE OF message_id, project_id",
                 "OLD.message_id IS NOT NEW.message_id "
                 "OR OLD.project_id IS NOT NEW.project_id")):
            db.execute(f"""
                    CREATE TRIGGER IF NOT EXISTS relation_audit_messages_{suffix}
                    AFTER {event} ON messages WHEN {when}
                    BEGIN DELETE FROM ledger_relation_audit; END
                """)
        # REPLACE skips DELETE triggers with SQLite's default settings.
        # Probe before replacement, while the previous parent still exists.
        db.execute("""
                CREATE TRIGGER IF NOT EXISTS relation_audit_messages_replace
                BEFORE INSERT ON messages
                WHEN EXISTS(SELECT 1 FROM messages
                            WHERE message_id=NEW.message_id
                              AND project_id IS NOT NEW.project_id)
                BEGIN DELETE FROM ledger_relation_audit; END
            """)
    _mark_relation_recount(db)


def _script(db, s: str):
    """Run a ;-separated DDL/DML script inside the caller's transaction
    (executescript commits implicitly — do NOT use it mid-migration)."""
    for st in s.split(";"):
        if st.strip():
            db.execute(st)


def _migrate(db, *, schema_version: int, fts: bool, fts_created: bool = False):
    """Normalize a supported pre-existing db without lossy recovery.
    The v3 backfill runs only on an upgrade (old user_version) or
    when this open created messages_fts — e.g. a DB written by a
    Python without FTS5 — since current writers fill the derived
    columns and triggers keep the index in sync."""
    def cols(t):
        return {r[1] for r in
                db.execute(f"PRAGMA table_info({t})")}
    # duplicate-key fence: preflight already ran before initialize
    # the schema version fence moves WITH the migration commit — a
    # crash must never leave "new columns, old version" behind, or an
    # old writer would keep writing a DB whose one-time backfills
    # already ran (Oracle F03)
    old_version = db.execute("PRAGMA user_version").fetchone()[0]
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    try:
        rebuilt_cards = _migrate_body(db, cols, old_version)
        if old_version < schema_version or fts_created:
            backfill_v3(db, fts=fts)
        if rebuilt_cards:
            for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall():
                name = row[0].replace('"', '""')
                if any(fk[2] == "notification_cards" for fk in db.execute(f'PRAGMA foreign_key_list("{name}")')):
                    if db.execute(f'PRAGMA foreign_key_check("{name}")').fetchone() is not None:
                        raise MigrationError("notification card foreign key violation during migration")
        db.execute(f"PRAGMA user_version={schema_version}")
        db.commit()
    except Exception:
        db.rollback()
        raise


def _migrate_body(db, cols, old_version: int):
    if "initial_import" not in cols("patients"):
        db.execute("ALTER TABLE patients ADD COLUMN initial_import TEXT")
    db.execute("""
          CREATE TABLE IF NOT EXISTS message_revisions(
            message_id INTEGER NOT NULL, seq INTEGER NOT NULL,
            content_hash TEXT NOT NULL, prev_content_hash TEXT,
            body_state TEXT NOT NULL, observed_at REAL NOT NULL,
            PRIMARY KEY(message_id,seq))
        """)
    if "reason_code" not in cols("fetch_jobs"):
        db.execute("ALTER TABLE fetch_jobs ADD COLUMN reason_code TEXT")
    db.execute("""
          CREATE TABLE IF NOT EXISTS message_metadata(
            message_id INTEGER NOT NULL REFERENCES messages(message_id),
            source TEXT NOT NULL CHECK(source IN ('capture','shadow')),
            content TEXT NOT NULL, checked_at REAL NOT NULL,
            last_error TEXT,
            PRIMARY KEY(message_id,source))
        """)
    db.execute("""
          CREATE TABLE IF NOT EXISTS message_reaction_actors(
            message_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
            reaction_type TEXT NOT NULL, profession TEXT,
            observed_at REAL NOT NULL,
            PRIMARY KEY(message_id,actor_id,reaction_type))
        """)
    actor_cols = {r[1] for r in db.execute(
        "PRAGMA table_info(message_reaction_actors)")}
    for col in ("actor_name", "organization", "removed_at"):
        if col not in actor_cols:   # additive within the current schema
            kind = "REAL" if col == "removed_at" else "TEXT"
            db.execute(
                f"ALTER TABLE message_reaction_actors ADD COLUMN {col} {kind}")
    # per-thread read acknowledgement: a reply's unread state lives
    # on its thread, not on the project flag the unread stage clears
    db.execute("""
          CREATE TABLE IF NOT EXISTS thread_read_marks(
            parent_id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
            last_reply_id INTEGER NOT NULL, status TEXT NOT NULL,
            marked_at REAL NOT NULL)
        """)
    db.execute("""
          CREATE TABLE IF NOT EXISTS message_reaction_actor_fetch(
            message_id INTEGER PRIMARY KEY, complete_at REAL,
            checked_at REAL NOT NULL, last_error TEXT)
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
        _script(db, ";".join(adds) + ";")
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
        _script(db, ";".join(adds) + ";")
    if "last_seen" in cols("messages"):
        db.execute(
          "UPDATE messages SET updated_seen=last_seen "
              "WHERE updated_seen IS NULL AND last_seen IS NOT NULL")
    # attachments: v1 PK/file_id shape != v2 attachment_id — rebuild
    if "attachment_id" not in cols("attachments"):
        _script(db, """
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
            db.execute(ddl)
    # read_marks: v1 has surrogate id PK; v2 needs (project_id,snapshot_ts)
    if "id" in cols("read_marks"):
        _script(db, """
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
        db.execute(
          "ALTER TABLE read_marks ADD COLUMN status TEXT DEFAULT 'unknown'")
    if "history_floor" not in cols("patients"):
        db.execute(
          "ALTER TABLE patients ADD COLUMN history_floor INTEGER")
    if "history_page" not in cols("patients"):
        db.execute(
          "ALTER TABLE patients ADD COLUMN history_page INTEGER DEFAULT 0")
    if "probe_mid" not in cols("patients"):
        db.execute(
          "ALTER TABLE patients ADD COLUMN probe_mid INTEGER")
    if "progress" not in cols("notify_outbox"):
        db.execute(
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
        _script(db, ";".join(adds) + ";")
    if "notified_at" not in cols("messages"):
        db.execute(
            "ALTER TABLE messages ADD COLUMN notified_at REAL")
    if old_version < 7:
        from mcs_util import positive
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
        for r in db.execute(
                "SELECT payload FROM notify_outbox "
                    "WHERE kind='new_messages'"):
            pl = loads_dict(r["payload"] or "{}")
            if pl is None:
                # a quarantined non-object payload must not abort the
                # whole migration (F07) — it carries no usable ids
                continue
            ids = pl.get("message_ids")
            if isinstance(ids, list):
                intended.update(i for i in ids if positive(i))
        now = time.time()
        db.execute(
            "UPDATE messages SET notified_at=? WHERE is_unread=0",
            (now,))
        if intended:
            ids = list(intended)
            # Keep every statement below the legacy SQLite 999-variable limit.
            for offset in range(0, len(ids), 900):
                batch = ids[offset:offset + 900]
                db.execute(
                    "UPDATE messages SET notified_at=? WHERE message_id IN ("
                    + ",".join("?" * len(batch)) + ")",
                    [now, *batch])
    if "kind" not in cols("runs"):
        db.execute(
          "ALTER TABLE runs ADD COLUMN kind TEXT DEFAULT 'tick'")
    _script(db, REQUEST_SCHEMA)
    # Interactive delivery uses module-SCHEMA, like mcs_requests;
    # this table does not require its own schema-version bump.
    if "route" not in cols("notify_outbox"):
        db.execute(
          "ALTER TABLE notify_outbox ADD COLUMN route TEXT NOT NULL"
              " DEFAULT 'text'")
    _script(db, NOTIFICATION_SCHEMA)
    # Additive transport scope: old rows and immutable spec bytes remain
    # Discord. A Slack workspace is never stored in the guild column.
    for table in ("notification_cards", "notification_renders",
                  "notification_intent_batches"):
        if "transport" not in cols(table):
            db.execute(
                f"ALTER TABLE {table} ADD COLUMN transport TEXT "
                    "NOT NULL DEFAULT 'discord'")
        column = "scope_json" if table == "notification_intent_batches" else "team_id"
        if column not in cols(table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    # Card face layout: rows from before it keep layout 1 forever
    if "layout" not in cols("notification_cards"):
        db.execute(
            "ALTER TABLE notification_cards ADD COLUMN layout INTEGER "
                "NOT NULL DEFAULT 1")
    rebuilt_cards = _expand_card_kinds(db)
    # Durable per-part rollup (T7): 'none' marks renders planned
    # before the part ledger existed.
    if "parts_state" not in cols("notification_renders"):
        db.execute(
            "ALTER TABLE notification_renders ADD COLUMN parts_state "
                "TEXT NOT NULL DEFAULT 'none'")
    # 📊 daily summary notices (#31) are card-less renders: the
    # outbox event they deliver, kept after gc strips spec_json
    if "intent_event_id" not in cols("notification_renders"):
        db.execute(
            "ALTER TABLE notification_renders ADD COLUMN "
                "intent_event_id INTEGER")
    # ☐/✅ 確認 toggle: a withdrawn ack stays as an audit row
    if "withdrawn_at" not in cols("notification_acknowledgements"):
        db.execute(
            "ALTER TABLE notification_acknowledgements "
                "ADD COLUMN withdrawn_at REAL")

    return rebuilt_cards


def _expand_card_kinds(db):
    """Rebuild the card parent without renaming it out from under child FKs."""
    sql = db.execute("SELECT sql FROM sqlite_schema WHERE name='notification_cards'").fetchone()[0]
    if "'patient_history'" in sql:
        return False
    if db.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise MigrationError("card migration requires owner FK isolation")
    updated, count = re.subn(
        r"CHECK\s*\(\s*kind\s+IN\s*\(\s*'thread'\s*,\s*'signal'\s*,\s*'digest'\s*\)\s*\)",
        "CHECK(kind IN ('thread','signal','digest','patient_history'))", sql, flags=re.I)
    if count != 1:
        raise MigrationError("unsupported notification card constraint")
    objects = [r[0] for r in db.execute(
        "SELECT sql FROM sqlite_schema WHERE tbl_name='notification_cards' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL")]
    seq = db.execute("SELECT seq FROM sqlite_sequence WHERE name='notification_cards'").fetchone()
    columns = ','.join('"' + r[1].replace('"', '""') + '"'
                       for r in db.execute("PRAGMA table_info(notification_cards)"))
    # sqlite_schema's CREATE prefix may quote the original name.
    updated = re.sub(r'(?i)^(CREATE TABLE\s+)(?:"notification_cards"|notification_cards)',
                     r'\1notification_cards_new', updated, count=1)
    db.execute(updated)
    db.execute(f"INSERT INTO notification_cards_new({columns}) SELECT {columns} FROM notification_cards")
    db.execute("DROP TABLE notification_cards")
    db.execute("ALTER TABLE notification_cards_new RENAME TO notification_cards")
    for statement in objects:
        db.execute(statement)
    if seq is not None:
        db.execute("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name='notification_cards'", (seq[0],))
    return True


def backfill_v3(db, *, fts: bool):
    """Idempotently fill derived columns for pre-v3 rows."""
    rows = db.execute(
        "SELECT message_id, posted_at, posted_at_ts, body_html,"
            " body_text IS NULL AS no_text FROM messages "
            "WHERE posted_at_ts IS NULL OR posted_at_ts=0 OR body_text IS NULL").fetchall()
    for r in rows:
        epoch = posted_epoch(r["posted_at"])
        if epoch is None and r["posted_at_ts"] is None \
                and not r["no_text"]:
            # an unparseable posted_at stays NULL — rewriting it on
            # every open only churns rows and the FTS trigger
            continue
        db.execute(
            "UPDATE messages SET posted_at_ts=CASE WHEN posted_at_ts IS NULL OR posted_at_ts=0 "
                "THEN ? ELSE posted_at_ts END,"
                " body_text=COALESCE(body_text,?) WHERE message_id=?",
            (epoch, html_to_text(r["body_html"]), r["message_id"]))
    if fts:
        db.execute("""
              INSERT INTO messages_fts(rowid, body_text, sender_name)
              SELECT message_id, body_text, sender_name FROM messages
              WHERE body_text IS NOT NULL AND message_id NOT IN
                (SELECT rowid FROM messages_fts)""")


# Static recovery-DB contract, one copy for valid_mcs_db, preflight,
# ledger_audit and the bounded backup inventory. Source columns are the ones
# recovery cannot reconstruct (_migrate_body adds derived columns, not these).
STATIC_SOURCE_COLUMNS = {
    "runs": frozenset({"run_id", "started_at", "finished_at",
                       "snapshot_ts", "status", "error"}),
    "patients": frozenset({"project_id", "project_type", "patient_name",
                           "disease", "station_name", "url", "last_seen"}),
    "messages": frozenset({"message_id", "project_id", "parent_id",
                           "sender_id", "sender_name", "sender_type",
                           "profession", "organization", "posted_at",
                           "body_html", "reply_count", "first_seen"}),
}
STATIC_BASE_COLUMNS = {
    "attachments": frozenset({"attachment_id", "message_id", "file_id",
                              "name", "url", "local_path", "bytes",
                              "sha256", "state", "downloaded_at",
                              "created_at"}),
    "read_marks": frozenset({"project_id", "snapshot_ts", "marked_at"}),
    "notify_outbox": frozenset({"event_id", "kind", "project_id", "payload",
                                "state", "attempts", "next_try",
                                "accepted_ref", "created_at", "updated_at"}),
    "artifacts": frozenset({"artifact_id", "kind", "project_id", "message_id",
                            "content", "model", "meta", "created_at"}),
    "fetch_jobs": frozenset({"job_id", "kind", "project_id", "message_id",
                             "parent_id", "payload", "state", "attempts",
                             "next_try", "created_at", "updated_at"}),
    "message_metadata": frozenset({"message_id", "source", "content",
                                   "checked_at", "last_error"}),
}
# Pre-attachment_id table layout (no attachment_id column).
LEGACY_ATTACHMENT_COLUMNS = frozenset({"message_id", "file_id", "name", "url",
                                       "downloaded_path", "first_seen"})
# table: (marker column, key) — with the marker, a duplicate key is ambiguous.
KEY_FENCES = {"attachments": ("attachment_id", "message_id,file_id"),
              "read_marks": ("id", "project_id,snapshot_ts")}


def static_required_tables(version: int) -> set[str]:
    """Tables a static ledger of this schema version must have.

    Older backups may lack later tables; initialize creates them on reopen."""
    required = set(STATIC_SOURCE_COLUMNS)
    if version >= 5:
        required |= {"attachments", "notify_outbox", "read_marks",
                     "artifacts", "fetch_jobs"}
    if version >= 8:
        required.add("message_metadata")
    return required


def static_required_columns(table: str, columns: set[str]) -> frozenset[str]:
    """Columns a present contract table needs, given the columns it has."""
    if table == "attachments" and "attachment_id" not in columns:
        return LEGACY_ATTACHMENT_COLUMNS
    required = STATIC_SOURCE_COLUMNS.get(table) or STATIC_BASE_COLUMNS[table]
    if table == "read_marks" and "id" in columns:
        required |= {"status"}
    return required


def _key_fence_breached(db: sqlite3.Connection, table: str, columns: set[str]) -> bool:
    """True when a fenced table with its marker column has a duplicate key."""
    fence = KEY_FENCES.get(table)
    return bool(fence and fence[0] in columns and db.execute(
        f'SELECT 1 FROM "{table}" GROUP BY {fence[1]} '
        "HAVING COUNT(*) > 1 LIMIT 1").fetchone())


def static_contract_ok(db: sqlite3.Connection, *, schema_version: int) -> bool:
    """True when an open static candidate meets the recovery schema contract.

    Checks journal_mode=delete, the supported version, no interrupted
    migration, the version's required tables, each present table's columns
    and the ambiguous-key fences. SQLite errors propagate, so each caller
    keeps its own budget, connection and failure mapping."""
    if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
        return False
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if not 0 <= version <= schema_version:
        return False
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if ({"attachments_v1", "read_marks_v1"} & tables
            or not static_required_tables(version) <= tables):
        return False
    if version >= 10 and "initial_import" not in {
            r[1] for r in db.execute("PRAGMA table_info(patients)")}:
        return False
    for table in (*STATIC_SOURCE_COLUMNS, *STATIC_BASE_COLUMNS):
        if table not in tables:
            continue  # initialize creates absent tables for older backups.
        columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        if (not static_required_columns(table, columns) <= columns
                or _key_fence_breached(db, table, columns)):
            return False
    return True
