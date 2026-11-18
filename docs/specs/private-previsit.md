# Private previsit delivery

This is a default-off, LINE WORKS-only path for a short previsit note to an
explicitly approved staff recipient. It does not change shared notification
cards or add a delivery queue.

## Public service API

`mcs/notify/private_previsit.py` exposes:

- `prepare_night(ledger, cfg, now=None)` prepares bounded content from
  `notify_views.patient_context_list(..., "previsit")` between 20:00 and
  05:59 JST. It makes no LLM call. It prepares the next morning's note from
  20:00 through 23:59 and the current morning's note from 00:00 through
  05:59.
- `dispatch_event(ledger, event, cfg, sender=None, now=None)` and
  `dispatch_due(ledger, cfg, sender=None, now=None, limit=20)` recheck live
  authorization, schedule, source and archive pins before dispatch. `sender`
  is a private-only interface; no generic notification sender is used.
- `notify_flush.flush` calls preparation and due dispatch on its existing
  periodic ticks, and filters the private event kind before any shared
  text or card handler. A prepared note is attempted on the first periodic
  tick at or after 08:00 JST; this is not an exact wall-clock guarantee.
  Missing night preparation is not regenerated during the day, and a later-day
  attempt is suppressed rather than replayed the following day.

The strict gate is `cfg.private_previsit.enabled is True` and
`cfg.private_previsit.owner_gate_approved is True`. Both values are absent or
false by default. No runtime configuration is installed by this change.

## Schedule, authorization and note pins

The registered pharmacy date (`ops.previsit_visit_date_set`) takes priority.
Without one, preparation accepts only one complete, unambiguous, explicit
pharmacist `planned` record from `read_source_schedule`. Incomplete or
truncated source coverage, unknown/ambiguous dates, conflicts, cancellations,
and missing dates skip preparation. The clinical `next_planned` field is not a
schedule source for this feature.

`ops.previsit_assignment_set` binds a human-confirmed staff ID, transport,
LINE WORKS account scope (`profile`, `application_id`, `team_id`, `route_epoch`)
and explicit recipient ID. `ops.previsit_assignment_revoke` appends a revoked
revision. These commands use the existing `ops.command` envelope, reason,
receipt and expected-revision checks. Artifacts include the command ID that
references the receipt. Free-text task owners and chart participants are not
authorization.

Preparation stores the formatted note and pins its assignment revision,
registered date, source-schedule projection, source message hashes and bounded
message watermark in an artifact. The outbox payload contains only the logical
dedup key and artifact reference. Deduplication is atomic in the same
`BEGIN IMMEDIATE` transaction as the note and outbox insert: one note per
project, approved recipient and visit day. Changes, revocation, archive,
cancellation or source edits suppress a prepared note. An uncertain state is
held rather than routed elsewhere.

The private note uses the patient-context view title as its identifying
header, states whether the date is registered or source-derived, and visibly
marks partial pages or character-limit truncation. Content is restricted to
the first bounded view page; it is not silently clipped.

## Delivery and limitations

The initial provider supports LINE WORKS only. It rechecks the configured bot,
profile, team and route epoch, project/user allowlists, and the approved
recipient immediately before the request; a nonempty `user_id` is mandatory.
It reuses `LineWorksClient.send_message` and `Sender.send` under the adapter's
API lock. Slack and Discord assignments are skipped with an explicit reason.
The private event is never sent through a configured shared channel.

The existing outbox records a committed sending marker before the provider is
called. Known not-sent results retry the same event with bounded delays;
accepted results are terminal; unknown outcomes and expired/crashed claims are
held without automatic resend. A restore-pending marker defers dispatch without
consuming an attempt. A missing provider, malformed state or failed private
verification holds the event.

This change adds no assignment UI, provider-auth setup, cron job, scheduler
configuration or real recipient mapping. It does not guarantee an exact
08:00 send or provide delivery for Slack/Discord. The LINE WORKS provider is
implemented and tested with a stub HTTP wire only; no credentials or live API
calls are included in the synthetic tests/demo.

## Release gate

Issue #250 remains an owner decision and a release/rollout gate. Do not set the
feature gate or add real mappings until that decision is recorded and the
owner approves private-note content and operating behavior. This work does not
widen shared cards and does not claim issue #251 or #89 is complete.
