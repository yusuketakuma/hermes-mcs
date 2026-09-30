---
type: architecture
title: Notification and Delivery (Outbox, Cards, Hermes Plugin)
description: How notify_outbox events reach Discord/Slack — the plain `hermes send` path, interactive cards with grants and receipts, the journaled transport-neutral delivery worker, and post-restore reconciliation that holds ambiguous scopes instead of resending.
tags: [notify, outbox, cards, hermes-plugin, delivery-worker, reconcile]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T03:50:50.528Z
sources:
  - id: openwiki-source-57677a630342f42b9016bfbc
    resource: repo://hermes_plugin/mcs_delivery/worker.py
  - id: openwiki-source-f86141d78e33166367ee76be
    resource: repo://mcs/notify/notify_cards.py
  - id: openwiki-source-38c7e93bce7e141ca7d64353
    resource: repo://mcs/notify/notify_flush.py
  - id: openwiki-source-18032683d7d9cd45a76981ea
    resource: repo://mcs/notify/notify_reconcile.py
  - id: openwiki-source-2246bb375a528ea179f0c898
    resource: repo://mcs/notify/notify_transport.py
generated: { by: "claude-code", at: "2026-09-29T03:50:50.528Z" }
---

# Notification and Delivery

The pipeline has two lanes. Both start from `notify_outbox` rows in the ledger, and neither posts content that was not stored there first.

## Lane 1: plain notices (`notify_flush`)

`mcs/notify/notify_flush.py` drains `notify_outbox` through the standard `hermes send` path. The destination comes from `config.json` `notify_target` in `hermes send --to` syntax (for example `slack:#mcs` or `discord:1234`). Hermes owns platform connection, credentials, channel resolution and mention policy. If `notify_bot_profile` is set, sends run under that Hermes profile. Attachments travel as `MEDIA:<path>` references.

<!-- openwiki: broken internal link [/openwiki/architecture/extraction-and-semantic.md] link "/openwiki/architecture/extraction-and-semantic.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
Events carry message ids only. Content is looked up in the ledger at send time, so the outbox payload stays small. `flush()` is the single delivery driver. Before each post and between chunks it consults the semantic send gate, and it parks, suppresses or quarantines the intent on `DeferredSend`, `StaleSend` or `FreezeSend`. See [Extraction and semantic](/openwiki/architecture/extraction-and-semantic.md).

## Lane 2: interactive cards

Intents routed `interactive` are handled by `mcs/notify/notify_cards.py` and `notify_transport.py`:

- Intents are frozen into sealed batches and fanned out to durable cards (one intent to N cards). Each card is rendered as an immutable, neutral spec published atomically to `data/discord_render/<delivery_id>.json`.
- The runner (the `mcs/` process) is the only writer to the ledger. The plugin reads published snapshots and spec files and writes only the protected command inbox.
- The Discord HTTP call and the DB commit can never be one transaction. An attempt therefore holds exclusive per-card send ownership until its factual result is settled as `delivered`, `not_sent` or `unknown`.
- `unknown` is a first-class state. Time alone never makes it retryable. Only a real receipt or an operator `ops.card_resolve` does.
- `notify.interactive` (`discord` or `off`) is the kill switch, and `route_epoch` is bumped when the delivery route changes so old receipts cannot settle new attempts.

## Delivery worker (`hermes_plugin/mcs_delivery/worker.py`)

The worker is transport-neutral, with Discord (`mcs_discord`) and Slack (`mcs_slack`) supplying only the wire calls (`_perform`, `_maybe_thread`). The rest is shared: claim, `transport_begin`, grant, send, `transport_receipt`, journaled at every phase.

Ordering contract:

1. A claim marker is not a send grant.
2. The grant is durable runner-side before HTTP begins.
3. A `started` journal row is fsync'd before the request fires.
4. A `result` row is fsync'd before the receipt is published.

Crashes land between those records and are classified on the next start. A pre-HTTP grant settles as `not_sent`. Silence after HTTP began stays `unknown` for an operator. A new worker never retries an in-flight send. A transport SDK may still retry inside one `_perform` call (discord.py re-POSTs on some 5xx and ECONNRESET), so a lost response can duplicate a Discord post. The plugin README documents this. Filesystem work runs through `asyncio.to_thread` so the event loop never blocks.

`card_workers.py` binds per-platform supervisors at connect time. If the interactive settings are absent or incomplete, the plugin still serves `/mcs` but never binds the interaction surface. The plugin registers only `/mcs <JSON>`, with no model tools or arbitrary shell, and reads its config on every call.

## Post-restore reconciliation

Restoring `ledger.db` rewinds card, render and attempt rows while external effects persist. `mcs/notify/notify_reconcile.py` is the gate that runs before senders resume:

- A journal `result` row is a fact. Its receipt is re-applied through `apply_transport_receipt`, and identity checks (scope, `render_rev`, `payload_hash`, `route_epoch`, correlation) decide. A mismatch fails closed into a hold.
- External evidence whose attempt or render row the restore erased holds the scope and is never resent.
- A DB `granted` or `unknown` attempt with no journal rows is held as `unknown_attempt`.
- A provable pre-HTTP attempt settles as `not_sent`, and resending is safe.
- An unparsable journal line taints its file, upgrading conclusions built on absent evidence to holds.

Verdicts land in `data/restore_reconcile.json`, an ops `update_notice` alerts about held scopes, and the `restore_pending` marker blocks all send grants until the receipt is durable. Holds are released through `ops.card_resolve` or a factual receipt.

## Operational note

<!-- openwiki: broken internal link [/openwiki/operations/deployment-and-updates.md] link "/openwiki/operations/deployment-and-updates.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/architecture/ledger-storage.md] link "/openwiki/architecture/ledger-storage.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`hermes_plugin/` is loaded by the long-lived Hermes gateway at startup. Changes take effect only after `hermes gateway restart`. See [Deployment](/openwiki/operations/deployment-and-updates.md). Ledger tables are described in [Ledger storage](/openwiki/architecture/ledger-storage.md).
