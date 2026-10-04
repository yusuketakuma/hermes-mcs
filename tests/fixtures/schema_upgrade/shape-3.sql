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
          first_seen REAL, updated_seen REAL, notified_at REAL);

CREATE VIRTUAL TABLE messages_fts
              USING fts5(body_text, sender_name);

CREATE TABLE notification_acknowledgements(
  ack_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id INTEGER NOT NULL REFERENCES notification_cards(card_id),
  manifest_id INTEGER NOT NULL REFERENCES notification_view_manifests(manifest_id),
  actor TEXT NOT NULL,
  command_id TEXT NOT NULL UNIQUE,
  receipt_ref TEXT,
  created_at REAL NOT NULL,
  withdrawn_at REAL);

CREATE TABLE notification_action_tokens(
  token TEXT PRIMARY KEY,
  card_id INTEGER NOT NULL REFERENCES notification_cards(card_id),
  action TEXT NOT NULL,
  params TEXT,
  need_source_gen INTEGER, need_manifest_id INTEGER,
  need_ui_rev INTEGER, need_request_rev INTEGER,
  expires_at REAL NOT NULL, created_at REAL NOT NULL);

CREATE TABLE notification_cards(
  card_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK(kind IN ('thread','signal','digest')),
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
  created_at REAL NOT NULL, updated_at REAL NOT NULL);

CREATE TABLE notification_delivery_attempts(
  attempt_id TEXT PRIMARY KEY,
  delivery_id TEXT NOT NULL REFERENCES notification_renders(delivery_id),
  begin_command_id TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL
    CHECK(state IN ('granted','delivered','not_sent','unknown')),
  message_id TEXT, error_code TEXT,
  worker_id TEXT,
  created_at REAL NOT NULL, finished_at REAL);

CREATE TABLE notification_intent_batches(
  event_id INTEGER PRIMARY KEY REFERENCES notify_outbox(event_id),
  frozen_payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  route_epoch INTEGER NOT NULL,
  transport TEXT NOT NULL DEFAULT 'discord',
  scope_json TEXT,
  sealed_at REAL NOT NULL);

CREATE TABLE notification_intent_cards(
  event_id INTEGER NOT NULL REFERENCES notify_outbox(event_id),
  card_id INTEGER NOT NULL REFERENCES notification_cards(card_id),
  coverage TEXT NOT NULL,
  required_render_rev INTEGER NOT NULL DEFAULT 0,
  delivery_id TEXT REFERENCES notification_renders(delivery_id),
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK(state IN ('pending','delivered','suppressed')),
  PRIMARY KEY(event_id, card_id));

CREATE TABLE notification_meta(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  notify_dirty INTEGER NOT NULL DEFAULT 0);

CREATE TABLE notification_render_parts(
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

CREATE TABLE notification_renders(
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

CREATE TABLE notification_restore_holds(
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

CREATE TABLE notification_task_reminders(
  request_id INTEGER NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('due','overdue','baseline')),
  due_date TEXT NOT NULL,
  event_id INTEGER,
  created_at REAL NOT NULL,
  PRIMARY KEY(request_id, stage, due_date));

CREATE TABLE notification_triage(
  card_id INTEGER PRIMARY KEY REFERENCES notification_cards(card_id),
  owner TEXT, defer_until REAL,
  state TEXT NOT NULL DEFAULT 'open'
    CHECK(state IN ('open','assigned','deferred')),
  revision INTEGER NOT NULL DEFAULT 0,
  last_actor TEXT, updated_at REAL);

CREATE TABLE notification_view_manifests(
  manifest_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id INTEGER NOT NULL REFERENCES notification_cards(card_id),
  render_rev INTEGER NOT NULL,
  source_generation INTEGER NOT NULL,
  presentation_generation INTEGER NOT NULL,
  digest INTEGER NOT NULL DEFAULT 0,
  shown TEXT NOT NULL,
  invalidated INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL);

CREATE TABLE notify_outbox(
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT, project_id INTEGER,
          payload TEXT,            -- sanitized descriptor, never raw bodies
          state TEXT DEFAULT 'pending',
          attempts INTEGER DEFAULT 0,
          next_try REAL,
          accepted_ref TEXT,
          created_at REAL, updated_at REAL, progress TEXT, route TEXT NOT NULL DEFAULT 'text');

CREATE TABLE patients(
          project_id INTEGER PRIMARY KEY,
          project_type TEXT, patient_name TEXT, disease TEXT,
          station_name TEXT, url TEXT,
          fetch_state TEXT DEFAULT 'pending',
          fetch_reason TEXT,
          last_complete_fetch REAL,
          last_seen REAL, created_at REAL, coverage_ts INTEGER, history_target INTEGER, is_archived INTEGER NOT NULL DEFAULT 0, history_floor INTEGER, history_page INTEGER DEFAULT 0, probe_mid INTEGER);

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

CREATE INDEX idx_artifacts_kind_msg
            ON artifacts(kind, message_id);

CREATE INDEX idx_artifacts_lookup
            ON artifacts(kind, project_id, message_id);

CREATE INDEX idx_messages_parent
            ON messages(parent_id);

CREATE INDEX idx_messages_project_time
            ON messages(project_id, posted_at_ts);

CREATE INDEX idx_nattempts_delivery
  ON notification_delivery_attempts(delivery_id);

CREATE INDEX idx_ncards_state
  ON notification_cards(delivery_state);

CREATE INDEX idx_nholds_active
  ON notification_restore_holds(card_id, released_at);

CREATE INDEX idx_nic_card
  ON notification_intent_cards(card_id, state);

CREATE INDEX idx_nmanifests_card
  ON notification_view_manifests(card_id);

CREATE INDEX idx_nrenders_card
  ON notification_renders(card_id);

CREATE INDEX idx_ntokens_card
  ON notification_action_tokens(card_id);

CREATE INDEX idx_receipts_request ON command_receipts(request_id);

CREATE INDEX idx_requests_project ON requests(project_id,request_id);

CREATE INDEX idx_requests_source_msg
            ON requests(source_message_id);

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

PRAGMA user_version=7;
