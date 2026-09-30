---
type: guide
title: Quickstart
description: Orientation for hermes-mcs (local MedicalCareStation chat collection, analysis and notification) and a task-to-page routing map for this wiki.
tags: [quickstart, routing, overview]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T03:50:50.528Z
sources:
  - id: openwiki-source-012f2c78e3b1446dfc35803f
    resource: repo://Makefile
  - id: openwiki-source-51183125cb4711a6f8997e74
    resource: repo://scripts/run_tests.sh
generated: { by: "claude-code", at: "2026-09-29T03:50:50.528Z" }
---

# Quickstart

hermes-mcs collects MedicalCareStation (MCS) medical and care chat on a 5-minute tick into a local SQLite ledger, extracts structure locally (rules plus a loopback LLM), and notifies Discord or Slack through Hermes. The core is stdlib-only, tests use synthetic data only, and consequential actions need human-confirmed commands. Source and tests are authoritative, and `AGENTS.md` holds the working rules.

## Where to read

| If you are working on… | Read |
|---|---|
<!-- openwiki: broken internal link [/openwiki/architecture/overview.md] link "/openwiki/architecture/overview.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| The big picture, module layout, `import ledger` flat imports | [Architecture overview](/openwiki/architecture/overview.md) |
<!-- openwiki: broken internal link [/openwiki/architecture/ingest-and-mcs-adapter.md] link "/openwiki/architecture/ingest-and-mcs-adapter.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| Collection, MCS API contract, read-marking, tick stages, health watch | [Ingest](/openwiki/architecture/ingest-and-mcs-adapter.md) |
<!-- openwiki: broken internal link [/openwiki/architecture/ledger-storage.md] link "/openwiki/architecture/ledger-storage.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| Tables, schema version, backups, snapshots, read-only reader | [Ledger](/openwiki/architecture/ledger-storage.md) |
<!-- openwiki: broken internal link [/openwiki/architecture/extraction-and-semantic.md] link "/openwiki/architecture/extraction-and-semantic.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| `extract_v1`, `extract_llm`, semantic drain, LLM admission broker | [Extraction and semantic](/openwiki/architecture/extraction-and-semantic.md) |
<!-- openwiki: broken internal link [/openwiki/architecture/notification-delivery.md] link "/openwiki/architecture/notification-delivery.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| Outbox, `hermes send`, cards, delivery worker, restore reconciliation | [Notification and delivery](/openwiki/architecture/notification-delivery.md) |
<!-- openwiki: broken internal link [/openwiki/architecture/human-approval-and-external-export.md] link "/openwiki/architecture/human-approval-and-external-export.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| Read model states, signals, requests and receipts, export contract | [Approvals and export](/openwiki/architecture/human-approval-and-external-export.md) |
<!-- openwiki: broken internal link [/openwiki/operations/deployment-and-updates.md] link "/openwiki/operations/deployment-and-updates.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| launchd/cron jobs, install, self-update, recovery, gateway restart | [Deployment and updates](/openwiki/operations/deployment-and-updates.md) |
<!-- openwiki: broken internal link [/openwiki/operations/testing-and-ci.md] link "/openwiki/operations/testing-and-ci.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| Running tests, lint, gates, project safety rules | [Testing and CI](/openwiki/operations/testing-and-ci.md) |

## Common commands

```bash
scripts/run_tests.sh tests/<area>/   # isolated pytest (temp HOME, no real services)
ruff check mcs/ tests/ hermes_plugin/ integration/ ci/ scripts/ deployment/ conftest.py
python3 scripts/update_readme.py --check
python3 ci/gates.py && python3 ci/mine_gates.py --check
```

## Things that surprise people

- Changing `hermes_plugin/` has no effect until `hermes gateway restart`.
- `deployment/` edits do not change the live machine.
- "No record found" is never reported as "no action was taken".
- Do not run `mcs_setup.py check` as a substitute for tests, since it inspects the real machine.
