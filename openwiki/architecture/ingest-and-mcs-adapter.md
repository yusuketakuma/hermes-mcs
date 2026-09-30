---
type: architecture
title: "Ingest: MCS Adapter, Worker and Tick Pipeline"
description: How each scheduled tick collects unread messages, history and self-post probes from MedicalCareStation, the API contract and safety rules of the adapter, deadline-bounded workers, durable fetch jobs and the independent health watcher.
tags: [ingest, mcs-adapter, run-check, tick, health-watch, safety]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T03:50:50.528Z
sources:
  - id: openwiki-source-ee2efc2bc9f6995878dadd8d
    resource: repo://mcs/core/bounded_http.py
  - id: openwiki-source-eba7d998ca76a62781f267a5
    resource: repo://mcs/ingest/health_watch.py
  - id: openwiki-source-95f73a009da693afac09ce91
    resource: repo://mcs/ingest/job_ops.py
  - id: openwiki-source-a0cb91bc60d328eac6651e2f
    resource: repo://mcs/ingest/mcs_adapter.py
  - id: openwiki-source-80df5ee12df515b2c1d28c2d
    resource: repo://mcs/ingest/run_check.py
generated: { by: "claude-code", at: "2026-09-29T03:50:50.528Z" }
---

# Ingest: MCS Adapter, Worker and Tick Pipeline

## Responsibility split

`mcs/ingest/run_check.py` is the launchd/cron entry point and only orchestrates. It owns argument parsing, the run lock, the deadline, the stage order, run status and the outermost exception boundary. Everything else is delegated:

| Module | Owns |
|---|---|
| `mcs_adapter.py` | API, CDP token bootstrap, auth and downloads. It is the only network surface for MCS. |
| `mcs_worker.py` | Isolated workers with absolute deadlines for API, attachment and Chrome I/O. |
| `job_ops.py` | Command-file ingest, `fetch_jobs` drains, discovery, trickle deep-import seeding, thread merge. |
<!-- openwiki: broken internal link [/openwiki/architecture/ledger-storage.md] link "/openwiki/architecture/ledger-storage.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| `core/ledger.py` | Schema, writes, outbox and jobs. See [Ledger storage](/openwiki/architecture/ledger-storage.md). |
| `core/maintenance.py` | Verified daily backup, log rotation, snapshot publish. |

## Tick stages

A tick runs fetch stages first, then derive stages (`_stage_fetch`, then `stage_derive`):

1. `stage_unread` collects unread projects and messages.
2. `stage_backfill` continues history imports.
3. `stage_self_probe` handles the `self_posts` setting. It probes `messages/latest` for each configured project on every tick. If the newest id is not stored, it does a bounded history fetch and notifies about the new posts. `latest` returns only `{is_self_only, message:{id}}` and has no `after` filter, so ids that could not be fully fetched are recorded in `patients.probe_mid` to stop a refetch loop.
4. `stage_attachments` downloads files.
<!-- openwiki: broken internal link [/openwiki/architecture/extraction-and-semantic.md] link "/openwiki/architecture/extraction-and-semantic.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/architecture/notification-delivery.md] link "/openwiki/architecture/notification-delivery.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
5. `stage_derive` runs rule and LLM extraction, rollups, signals and the notify flush. See [Extraction and semantic](/openwiki/architecture/extraction-and-semantic.md) and [Notification and delivery](/openwiki/architecture/notification-delivery.md).

Overnight (22:00–06:00) the script thins ticks to every 20 minutes, staying under the roughly 30-minute session expiry. A scheduled tick waits briefly for the run lock, because the nightly semantic drain releases it between batches.

Exit codes: `0` ok (per-patient partial failures appear in `result.errors`), `1` bootstrap/network/schema failure, `2` session expired mid-run (after one `auto_login` retry), `3` another instance holds the lock.

## Adapter contract and safety rules

The adapter uses the `/api/v2t` REST API with a Bearer token read from the web app's `localStorage` through CDP. Unread projects are enumerated from the project list because the dedicated unread route returns 403. Messages are fetched per project with the collection snapshot `timestamp`, thread replies through the thread route, and files through anonymous URLs.

Enforced in the adapter:

- No redirects and no proxy auto-detection, and the scheme and host are pinned to `www.medical-care.net`, so the Bearer token can never leave that origin.
- Errors carry structured info only. Response bodies never enter exceptions or logs.
- Fetch completeness is explicit: `complete`, `incomplete` or `schema_error`.
- Message ids must be positive ints before they reach the ledger.
- Downloads are allowlist-checked, size-capped, streamed and atomically renamed.

**Read-ack gate.** `--mark-read` marks a patient only when its `fetch_state` is `complete` and the ledger commit succeeded. It always sends the exact snapshot timestamp from `list_unread()`, followed by project-detail confirmation. A response that fails to parse records `unknown`, never `confirmed`.

## Bounded workers

`mcs_worker.bounded_call` runs each request in a fresh subprocess with an absolute deadline. `core/bounded_http.py` is the shared mechanism for the Jev API and the loopback LLM. The body and credential travel over stdin and never argv, the worker follows no proxy or redirect, response size is capped, and the parent kills and reaps a worker that outlives the budget. Which endpoints may receive a credential is the caller's policy. An unauthenticated request is pinned to a loopback URL.

## Durable fetch jobs

`job_ops` records a command file as a durable `fetch_job` before consuming it. A file that does not parse yet (possibly mid-write) stays in place and is retried on the next drain. History jobs carry their own page cursor and resume with one page of overlap. `trickle` jobs get a small page budget per run and execute only while ample deadline remains.

## Health watching

`run_check` exits 0 even on partial runs, and a dead producer leaves the last `health.json` behind. `mcs/ingest/health_watch.py` therefore never trusts exit codes. It re-derives status from the file itself (presence, parseability, freshness) against `health.tick_interval_s` (default 300) and `health.max_missed_runs` (default 2), counting the actual scheduled ticks so the overnight thinning is not reported as a stall. Alerts contain status codes and counters only, and `data/health_watch_status.json` is published atomically.
