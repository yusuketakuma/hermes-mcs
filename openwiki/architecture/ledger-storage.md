---
type: architecture
title: "Ledger: SQLite Storage Model"
description: The SQLite ledger (schema v7) that stores messages, artifacts, the notify outbox and read-mark intents, plus its open-time safety checks, read-only reader, verified backups and snapshot publishing.
tags: [ledger, sqlite, fts5, backup, snapshot, migration]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T03:50:50.528Z
sources:
  - id: openwiki-source-342a8a9131923f8c3221fec9
    resource: repo://mcs/core/ledger.py
  - id: openwiki-source-0385144b250938a6f9a0affb
    resource: repo://mcs/core/maintenance.py
  - id: openwiki-source-97ee6113d7018c66e4feb4b6
    resource: repo://mcs/core/mcs_queries.py
generated: { by: "claude-code", at: "2026-09-29T03:50:50.528Z" }
---

# Ledger: SQLite Storage Model

`mcs/core/ledger.py` owns the local database (`data/ledger.db`). Every other stage reads or writes through it, so its contracts define what "stored", "delivered" and "read" mean.

## Main tables (schema v7)

- `runs` — one row per check run (`tick` or `init`).
- `patients` — per-project fetch state, `history_floor` (deepest completed cutoff), `history_page` (resume cursor for deep imports), `probe_mid` (self-post probe guard).
- `messages` — deduplicated by `message_id`. It keeps `body_html` and pre-stripped `body_text`, `body_state` (`snippet`, `full` or `unknown`), `posted_at` with an indexed epoch `posted_at_ts`, the `is_unread` flag, and a `content_hash` used for edit detection.
- `messages_fts` — FTS5 index over body text and sender, maintained by triggers.
- `attachments` — file records with sha256, bytes and state.
- `artifacts` — the derived-data store (extractions, summaries, signals, semantic generations). Rows are append-only in several kinds, and the latest row is the current state.
- `notify_outbox` — durable `pending`, `in_flight`, `accepted` and `failed` events.
- `read_marks` — read-acknowledgement intents and results, where `unknown` is never treated as success.
<!-- openwiki: broken internal link [/openwiki/architecture/human-approval-and-external-export.md] link "/openwiki/architecture/human-approval-and-external-export.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- `requests` and `command_receipts` — see [Human-approved requests](/openwiki/architecture/human-approval-and-external-export.md).

Multi-row writes such as `save_patient()` and `save_messages()` run inside `with self.db`, so a mid-write failure rolls the whole block back.

## Opening the database

`Ledger.__init__` is deliberately strict:

- The file is `chmod 0600` (and `-wal` and `-shm` too), because it is a PHI store.
- A `user_version` newer than `SCHEMA_VERSION` raises `MigrationError`, so old code never runs against a newer schema. Leftover `attachments_v1` or `read_marks_v1` tables mean an interrupted migration and also raise, requiring human review.
- It sets `foreign_keys=ON`, `busy_timeout=30000`, `journal_mode=WAL` and `synchronous=FULL`.
- Opening runs migration and FTS backfill, so opening is itself a write.

`LedgerReader` is the read-only variant for sandboxed consumers. It opens with `mode=ro` and runs no migration, DDL or DML, so any write fails at the SQLite layer. It exists because the normal constructor writes on open.

## Shared read predicate

`mcs/core/mcs_queries.py` holds one definition of "current extraction" (valid JSON, hash-matched to the message body, not an error record). Both `mcs_stats` and `mcs_signals` use it so they cannot drift. It holds constants, SQL fragments and iterators only, with no connection of its own.

## Backup and snapshot

`mcs/core/maintenance.py` runs at the end of each tick, and each step is failure-isolated by the caller.

<!-- openwiki: broken internal link [/openwiki/operations/deployment-and-updates.md] link "/openwiki/operations/deployment-and-updates.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- `daily_backup` makes one verified `.backup` per day. It writes a temp file, switches it to `journal_mode=DELETE`, checks it with `valid_mcs_db` (`quick_check`, journal mode, `user_version` in range), then publishes atomically. A present-but-broken file never blocks a new backup. The newest 7 are kept. `preupdate_backup` serves the [self-update](/openwiki/operations/deployment-and-updates.md) flow.
- `publish_snapshot` regenerates the read-only snapshot for the sandboxed consumer through the same backup, verify and atomic-rename path.
- Log rotation (5 MiB) and attachment pruning (14 days) live here too.

## Recovery interaction

<!-- openwiki: broken internal link [/openwiki/architecture/notification-delivery.md] link "/openwiki/architecture/notification-delivery.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
A restored `ledger.db` rewinds card and attempt rows while external effects (posted messages, journals) persist. [Notification and delivery](/openwiki/architecture/notification-delivery.md) explains the reconciliation gate that handles this. `tests/core/test_ledger_recovery_contract.py` covers the real writer, verified backup, snapshot publisher and read-only reader on synthetic data.
