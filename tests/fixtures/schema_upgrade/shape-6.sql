CREATE TABLE artifacts(
          artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT, project_id INTEGER, message_id INTEGER,
          content TEXT, model TEXT, meta TEXT,
          created_at REAL);

CREATE TABLE attachments(
          attachment_id INTEGER PRIMARY KEY AUTOINCREMENT,
          message_id INTEGER, file_id TEXT, name TEXT,
          url TEXT, local_path TEXT, bytes INTEGER, sha256 TEXT,
          state TEXT DEFAULT 'pending',
          downloaded_at REAL, created_at REAL, attempts INTEGER DEFAULT 0, next_try REAL, error TEXT);

CREATE TABLE command_receipts(
  command_id TEXT PRIMARY KEY NOT NULL, payload_hash TEXT NOT NULL,
  project_id INTEGER, request_id INTEGER,
  outcome TEXT NOT NULL CHECK(outcome IN ('applied','rejected')),
  receipt_json TEXT NOT NULL, processed_at REAL NOT NULL);

CREATE TABLE fetch_jobs(
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

CREATE TABLE messages(
          message_id INTEGER PRIMARY KEY,
          project_id INTEGER, parent_id INTEGER,
          sender_id INTEGER, sender_name TEXT, sender_type TEXT,
          profession TEXT, organization TEXT,
          posted_at TEXT, posted_at_ts INTEGER, is_unread INTEGER,
          body_html TEXT, body_text TEXT, body_state TEXT,
          content_hash TEXT, reply_count INTEGER,
          first_seen REAL, updated_seen REAL);

CREATE VIRTUAL TABLE messages_fts
              USING fts5(body_text, sender_name);

CREATE TABLE notify_outbox(
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT, project_id INTEGER,
          payload TEXT,            -- sanitized descriptor, never raw bodies
          state TEXT DEFAULT 'pending',
          attempts INTEGER DEFAULT 0,
          next_try REAL,
          accepted_ref TEXT,
          created_at REAL, updated_at REAL, progress TEXT);

CREATE TABLE patients(
          project_id INTEGER PRIMARY KEY,
          project_type TEXT, patient_name TEXT, disease TEXT,
          station_name TEXT, url TEXT,
          fetch_state TEXT DEFAULT 'pending',
          fetch_reason TEXT,
          last_complete_fetch REAL,
          last_seen REAL, created_at REAL, coverage_ts INTEGER, history_target INTEGER, history_floor INTEGER, history_page INTEGER DEFAULT 0);

CREATE TABLE read_marks(
          project_id INTEGER, snapshot_ts INTEGER, marked_at REAL,
          status TEXT DEFAULT 'unknown',
          PRIMARY KEY(project_id, snapshot_ts));

CREATE TABLE requests(
  request_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL,
  source_hash TEXT NOT NULL, title TEXT NOT NULL,
  assignee TEXT, due_date TEXT,
  status TEXT NOT NULL CHECK(status IN ('open','in_progress','done','cancelled')),
  revision INTEGER NOT NULL CHECK(revision > 0),
  created_at REAL NOT NULL, updated_at REAL NOT NULL);

CREATE TABLE runs(
          run_id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at REAL, finished_at REAL,
          snapshot_ts INTEGER, status TEXT, error TEXT, kind TEXT DEFAULT 'tick');

CREATE TABLE snapshot_meta(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  generation_id TEXT NOT NULL, generated_at REAL NOT NULL);

CREATE INDEX idx_artifacts_lookup
            ON artifacts(kind, project_id, message_id);

CREATE INDEX idx_messages_parent
            ON messages(parent_id);

CREATE INDEX idx_messages_project_time
            ON messages(project_id, posted_at_ts);

CREATE INDEX idx_receipts_request ON command_receipts(request_id);

CREATE INDEX idx_requests_project ON requests(project_id,request_id);

CREATE UNIQUE INDEX uq_attachments_msg_file
            ON attachments(message_id, file_id);

CREATE TRIGGER messages_ai
              AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, body_text, sender_name)
                VALUES (new.message_id, new.body_text, new.sender_name);
              END;

CREATE TRIGGER messages_au
              AFTER UPDATE ON messages BEGIN
                UPDATE messages_fts SET body_text=new.body_text,
                  sender_name=new.sender_name
                WHERE rowid=old.message_id;
                INSERT INTO messages_fts(rowid, body_text, sender_name)
                SELECT new.message_id, new.body_text, new.sender_name
                WHERE NOT EXISTS(SELECT 1 FROM messages_fts
                                 WHERE rowid=new.message_id);
              END;

PRAGMA user_version=5;
