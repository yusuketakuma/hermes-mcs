---
type: architecture
title: Human-Approved Requests, Views and External Export
description: The read-only read model, review-candidate signals, receipt-first human-confirmed command processing, and the governed external export contract that is disabled by default.
tags: [requests, command-receipts, read-model, signals, export, safety]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T03:50:50.528Z
sources:
  - id: openwiki-source-da3a85b531158686640e6715
    resource: repo://mcs/ops/ext_contract.py
  - id: openwiki-source-2f5194c4bdfb7f3382a2928f
    resource: repo://mcs/ops/mcs_requests.py
  - id: openwiki-source-b458133671e3587692efdb6e
    resource: repo://mcs/ops/mcs_signals.py
  - id: openwiki-source-b8c3ea7489f0a1f8326c831d
    resource: repo://mcs/views/read_model.py
generated: { by: "claude-code", at: "2026-09-29T03:50:50.528Z" }
---

# Human-Approved Requests, Views and External Export

This layer never acts on its own. Views only read, signals only suggest, requests and updates change state only through a human-confirmed command, and export leaves the process only under an explicit authorization.

## Read model

`mcs/views/read_model.py` is the single machine-readable surface shared by stats, pharmacy views and exports. Every read binds to one published snapshot (`snapshot_meta.generation_id`). Each derived record reports one of four states instead of silently falling back to an obsolete projection:

| State | Meaning |
|---|---|
| `current` | A valid artifact bound to the message's current `content_hash` exists. |
| `stale` | Artifacts exist, but all point at a superseded body hash or are invalidated. |
| `pending` | No artifact row exists. This is not evidence that nothing happened. |
| `unknown` | Rows exist but none can be classified. |

This carries the project rule that "no record found" does not mean "no action was taken".

## Review-candidate signals

`mcs/ops/mcs_signals.py` runs inside the main check pipeline (`stage_derive`). Candidates are append-only `signal_v1` artifacts, one row per lifecycle transition, and the latest row is the current state. A signal only says that review may be warranted, never that care was missed. Thresholds are fixed constants unless a human approves an override through the `ops.signal_policy` command. Statistics never feed back into detection. Notifications reach `notify_outbox` only when config `signals.notify` is true, and queued intents are suppressed at send time if the flag was turned off since.

## Human-confirmed requests

`mcs/ops/mcs_requests.py` stores requests (`open`, `in_progress`, `done`, `cancelled`) and processes command files with no network access and no automatic task creation.

- **Strict parsing.** Commands are capped at 16 KiB, and duplicate keys, non-finite numbers and excessive nesting are rejected. Command files are opened with `O_NOFOLLOW`.
- **Receipt-first, idempotent.** `apply_command` requires a UUID `command_id` and hashes the canonical payload. The outcome (`applied` or `rejected`) is committed to `command_receipts` in the same transaction as the state change. Replaying a `command_id` with the same payload returns the stored receipt, and a different payload is rejected as `command_id_conflict`.
- **Schema floor.** Request tables need schema v5 or later.
<!-- openwiki: broken internal link [/openwiki/operations/deployment-and-updates.md] link "/openwiki/operations/deployment-and-updates.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- **Updates ride the same path.** `ops.update_apply` is approved through a receipt, with network-bound sha resolution done before the write transaction so the DB writer lock is not held over a network round trip. See [Deployment and updates](/openwiki/operations/deployment-and-updates.md).

## Governed external export

`mcs/ops/ext_contract.py` is a provider-neutral authorization, envelope and journal layer over the read model. It is disabled by default and contains no network code. The only destination implemented is `LocalSink`, a directory-backed fake consumer. A real connector needs a named provider, an access review and separate permission (`docs/external-export-contract.md`).

Enforced in code:

- Every attempt is bound to an `mcs-ext-auth/1` authorization file (purpose, actor, destination, scope, per-patient eligibility, expiry, retention). Revoked, expired, malformed or detail-scope authorizations are refused before any bytes move.
- Records are aggregate-scope only and pass a field whitelist at both producer and sink, so raw patient content keys are rejected.
- Sends are idempotent by `envelope_id`. An acknowledged envelope is never re-sent, and an unacknowledged one is held rather than retried, leaving the outcome `unknown` until reconciled.
- Withdrawal sends a delete directive whose acknowledgement is journaled separately. Every action, including refusals, is appended to an audit log.

## Related

<!-- openwiki: broken internal link [/openwiki/architecture/ledger-storage.md] link "/openwiki/architecture/ledger-storage.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- [Ledger storage](/openwiki/architecture/ledger-storage.md) holds `requests`, `command_receipts` and artifacts.
