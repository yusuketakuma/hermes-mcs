---
type: architecture
title: System Architecture Overview
description: End-to-end view of hermes-mcs — the scheduled tick that collects MedicalCareStation chats into a local SQLite ledger, derives structure locally, notifies through Hermes, and the flat-import module layout that holds it together.
tags: [architecture, overview, module-layout, flat-import, data-flow]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T03:50:50.528Z
sources:
  - id: openwiki-source-b55abb67c2c8a6cb6ec7c7ea
    resource: repo://mcs/_mcs_path.py
  - id: openwiki-source-342a8a9131923f8c3221fec9
    resource: repo://mcs/core/ledger.py
  - id: openwiki-source-80df5ee12df515b2c1d28c2d
    resource: repo://mcs/ingest/run_check.py
generated: { by: "claude-code", at: "2026-09-29T03:50:50.528Z" }
---

# System Architecture Overview

hermes-mcs collects medical and care chat from MedicalCareStation (MCS) into a local ledger, analyzes it locally, and notifies Discord or Slack through Hermes. The core depends on the Python standard library only.

## Data flow

```
launchd tick (5 min; 20 min overnight)
  → run_check.py ──► mcs_adapter (only MCS network surface)
        │                 unread / history / self-post probe / attachments
        ▼
   ledger.db (SQLite, WAL) ◄── artifacts: extract_v1, extract_llm, signals, semantic
        │
        ├─► derive stage: rule + local-LLM extraction, rollups, signals
        ├─► notify_outbox ─► notify_flush ─► `hermes send`   (plain notices)
        │                └► notify_cards ─► spec files ─► Hermes plugin workers (cards)
        └─► published read-only snapshot ─► sandboxed readers, /mcs plugin commands
```

<!-- openwiki: broken internal link [/openwiki/architecture/ingest-and-mcs-adapter.md] link "/openwiki/architecture/ingest-and-mcs-adapter.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/architecture/ledger-storage.md] link "/openwiki/architecture/ledger-storage.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/architecture/extraction-and-semantic.md] link "/openwiki/architecture/extraction-and-semantic.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/architecture/notification-delivery.md] link "/openwiki/architecture/notification-delivery.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/architecture/human-approval-and-external-export.md] link "/openwiki/architecture/human-approval-and-external-export.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
Stage details: [Ingest](/openwiki/architecture/ingest-and-mcs-adapter.md), [Ledger](/openwiki/architecture/ledger-storage.md), [Extraction and semantic](/openwiki/architecture/extraction-and-semantic.md), [Notification and delivery](/openwiki/architecture/notification-delivery.md), [Approvals and export](/openwiki/architecture/human-approval-and-external-export.md).

## Design themes

- **The ledger is the source of truth.** Notification payloads carry ids and are hydrated at send time. Derived data is stored as artifacts bound to a message `content_hash`, so an edit makes older results stale rather than silently reused.
- **Fail closed on ambiguity.** "Unknown" is a first-class state for read marks, delivery attempts and export envelopes. It is never turned into success or retried on time alone.
- **Local by default.** Extraction uses a loopback llama.cpp server. Anything that leaves the machine goes through Hermes (notifications) or a governed contract (export, disabled by default).
- **Humans approve consequential actions.** Requests, updates and holds are released only through confirmed commands with receipts.
- **Absence of a record is not absence of care.** Views and signals preserve this distinction.

## Module layout and flat imports

Runtime code lives under `mcs/`, split into first-level areas: `core/` (DB, common, LLM admission), `ingest/`, `notify/`, `extract/` (with `v1/`, `v4/` and README-only `v2/`, `v3/`), `semantic/`, `views/` and `ops/`. Modules still import each other by flat name (`import ledger`).

That works through `mcs/_mcs_path.py`: an entry point inserts the `mcs/` root into `sys.path` and imports `_mcs_path`, which walks the tree and prepends every directory containing `.py` files, at any depth, ahead of installed packages. `_mcs_path.py` is the only importable module directly under `mcs/`. Adding a new first-level subdirectory needs no bootstrap change, though `AGENTS.md` and the `deployment/` path references must be updated.

Outside `mcs/`:

- `hermes_plugin/` — the Hermes addon: `mcs_delivery/` (transport-neutral delivery), `mcs_discord/`, `mcs_slack/`, `card_workers.py`, `projects.py`.
- `deployment/` — launchd plists and scripts (candidates only; editing them does not change the live machine).
<!-- openwiki: broken internal link [/openwiki/operations/testing-and-ci.md] link "/openwiki/operations/testing-and-ci.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- `tests/` and `integration/` — mirror the area names. See [Testing and CI](/openwiki/operations/testing-and-ci.md).
- `evaluation/`, `docs/`, `scripts/`, `ci/`.

Large cohesive modules (`ledger.py`, `mcs_adapter.py`, `semantic_drain`) are deliberately not split by line count.

## Related

<!-- openwiki: broken internal link [/openwiki/operations/deployment-and-updates.md] link "/openwiki/operations/deployment-and-updates.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- [Deployment and updates](/openwiki/operations/deployment-and-updates.md)
<!-- openwiki: broken internal link [/openwiki/quickstart.md] link "/openwiki/quickstart.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- [Quickstart](/openwiki/quickstart.md)
