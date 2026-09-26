# Governed external export contract (T16)

Provider-neutral design for exporting the T15 machine read model
(`mcs-read-model/1` records from `brain_export.py`) to an external
knowledge store — **disabled by default**. No connector, endpoint,
credential, auto-sync job or cloud dependency is shipped. The only
implementation is `mcs/ops/ext_contract.py`, which demonstrates the
contract against `LocalSink`, an in-process fake consumer backed by a
directory.

## Authorization (`mcs-ext-auth/1`)

Every export attempt requires a JSON authorization file, validated by
`load_authorization()` at build time AND again at send time:

| field | meaning |
|---|---|
| `auth_id` | unique authorization identifier — appears on every audit record |
| `purpose` | the stated purpose; copied into the envelope verbatim |
| `actor` | who authorized (human identity — `confirm_human: true` required) |
| `destination` | a *label* for the destination, never a live endpoint |
| `scope` | must be `aggregate`; `detail` is refused unconditionally |
| `patients` | `"all"` or a list of `project_id`s — per-patient eligibility |
| `fields` | whitelist of record types (`meta`,`coverage`,`stat`,`signal`,`attachment`,`message`,`signals_truncated`) |
| `expires_at` | unix timestamp; expired authorizations refuse |
| `max_snapshot_age_s` | bound on snapshot staleness at build time |
| `retention_days` | retention the destination must apply |
| `revoked` | `true` refuses every subsequent attempt, including queued ones |
| `confirm_human`, `reason`, `created_at` | human-confirmation provenance |

Unknown fields are **rejected** — typo tolerance is how authorizations
silently widen. Adding a real connector additionally requires a named
provider, an access review, and a separate permission.

## Envelope (`mcs-ext-export/1`)

`build_envelope(records, auth, snapshot_generated_at)` produces:

- `envelope_id` — deterministic `sha256(auth_id, generation, records_sha256)`;
  the idempotency key. Same inputs → same id → at-most-once effect.
- `snapshot_generation_id` + `snapshot_generated_at` — the immutable
  source generation. Rotation is detectable, never silently mixed.
- `scope`, `purpose`, `destination`, `retention_days`, `record_count`,
  `records_sha256`, and the `records` themselves.

Build-time refusals: stale snapshot (`max_snapshot_age_s`), record type
not whitelisted by the authorization, any record carrying a forbidden
key (`body_text`, `statement`, `evidence_quote`, `sender`, `name`,
`patient_name`, `note`, …), non-aggregate scope, wrong record contract.
Patient filtering drops ineligible `message` records and their
`attachment` rows (attachments follow the parent message's
eligibility).

## Delivery outcomes — honest three-state

`GovernedExporter.deliver()` journals per `envelope_id`:

- `acked` — sink stored the payload AND wrote an ack. Terminal success;
  a repeated `deliver()` is a no-op (`already_acked`), never a
  duplicate send.
- `held` (`reason: ack_unknown`) — the payload may or may not have
  arrived. **Held, never retried blindly.** `reconcile()` inspects the
  sink: a stored payload reconciles to `acked` without retransmission;
  an absent payload stays held for a human.
- `refused` — authorization/scope/field violation. Audited, nothing sent.
- `withdrawn` / `delete_held` — see below.

## Withdrawal and deletion propagation

`withdraw(envelope_id)` issues a delete directive to the destination
and journals its acknowledgement **independently**: an unacknowledged
delete is `delete_held`, not claimed. A withdrawn envelope refuses
subsequent `deliver()` calls.

## Audit

Every attempt appends to `state_dir/audit.jsonl` — `deliver`,
`reconcile`, `withdraw` with their outcomes (`acked`, `held`,
`refused`, `withdrawn`, `delete_held`, …), timestamps and envelope ids.
Refusals are audited as deliberately as sends.

## PHI classification and lifecycle

Exportable records are aggregate-scope only: ids, hashes, counts,
states, typed fact/relation identifiers. Raw patient content (message
bodies, statements, evidence quotes, sender/patient names, attachment
file names, signal notes) is forbidden by construction and checked at
both producer and sink. Destination retention is bounded by
`retention_days`; withdrawal propagates deletion per envelope.

## Incident response

- Suspected unauthorized send → set `revoked: true`; every subsequent
  attempt refuses and is audited; `withdraw()` propagates deletion.
- Lost acknowledgement → the journal holds `unknown`; reconcile before
  any resend; resending requires a new authorization, not a retry flag.
- Wrong scope detected downstream → sink-side `FORBIDDEN_KEYS` check
  is the second wall; the record never stores.

## What this contract deliberately does NOT include

- No real endpoint, credential store, TLS profile or SDK client.
- No scheduler/cron/service wiring — export runs only when invoked.
- No detail-scope export path, under any authorization.
- No silent retry of PHI after an unknown outcome.
