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
| `max_snapshot_age_s` | nonnegative finite bound on staleness, checked at build and delivery |
| `retention_days` | required positive integer; retention the destination must apply |
| `revoked` | `true` refuses every subsequent attempt, including queued ones |
| `confirm_human`, `reason`, `created_at` | human-confirmation provenance |

Unknown fields are **rejected** — typo tolerance is how authorizations
silently widen. Adding a real connector additionally requires a named
provider, an access review, and a separate permission.
Numeric limits reject booleans and nonfinite values. An explicit empty
`fields` grant permits no record types. `deliver(..., auth_path=...)`
requires the current authorization file even when an envelope is already built.
The current grant must match the envelope's identity, purpose, destination,
retention, record types and patient scope.

## Envelope (`mcs-ext-export/1`)

`build_envelope(records, auth, snapshot_generated_at)` produces:

- `envelope_id` — deterministic `sha256(auth_id, generation, records_sha256)`;
  the existing `/1` idempotency key. Creation wall time is excluded.
  The journal additionally binds destination, purpose, scope, retention and
  snapshot time. Reusing an authorization id cannot alter an already
  dispatched intent; a changed intent requires a distinct authorization id.
- `snapshot_generation_id` + `snapshot_generated_at` — the immutable
  source generation. Rotation is detectable, never silently mixed.
- `scope`, `purpose`, `destination`, `retention_days`, `record_count`,
  `records_sha256`, and the `records` themselves.

Build-time refusals: stale snapshot (`max_snapshot_age_s`), record type
not whitelisted by the authorization, any record carrying a forbidden
key (`body_text`, `statement`, `evidence_quote`, `sender`, `name`,
`patient_name`, `note`, …), non-aggregate scope, wrong record contract.
Patient filtering drops ineligible `message` and `signal` records and
their `attachment` rows (attachments follow the parent message).
Whole-snapshot coverage, signal totals and unscoped statistics are omitted
under a per-patient grant: deleting patient rows cannot narrow those totals.
Explicitly patient-scoped statistics also validate nested patient ids.

`export_schema.py` supplies the nested field/type allowlist shared by
the JSONL producer and the receiver. The producer projects numeric counts,
ids, hashes and validated states; raw medication names, period expressions,
signal context, arbitrary future fields and explanatory prose remain in the
local human-readable exports. `content_omitted: true` declares this projection.
Missing values remain unknown, and omitted fields must not be interpreted as
zero or as evidence of no clinical event. Machine records are complete JSON
objects of this aggregate projection; they are never byte-sliced.

## Delivery outcomes — honest three-state

`GovernedExporter.deliver()` journals per `envelope_id`:

A local file lock serializes journal decisions. The `sent` intent is flushed
and synchronized **before** calling the sink. Sink exceptions and lost replies
leave that intent held; a later authorization refusal never erases it.
The journal binds the local sink directory and delivery intent. Historical
journals lacking those bindings are held for inspection, never automatically
upgraded into a new send. Acknowledgements must match the
envelope id, record count and records hash; reconciliation additionally checks
the stored payload's integrity. Corrupt journals refuse delivery.

- `acked` — sink stored the payload AND wrote an ack. Terminal success;
  a repeated `deliver()` is a no-op (`already_acked`), never a
  duplicate send.
- `held` (`reason: ack_unknown`) — the payload may or may not have
  arrived. **Held, never retried blindly.** `reconcile()` inspects the
  sink: a matching stored payload and receipt reconcile to `acked` without
  retransmission; absent or unverified payload/receipt stays held for a human.
- `refused` — authorization/scope/field violation. Audited, nothing sent.
- `withdrawn` / `delete_held` — see below.

## Withdrawal and deletion propagation

`withdraw(envelope_id)` issues a delete directive to the destination
and journals its acknowledgement **independently**: an unacknowledged
delete is `delete_held`, not claimed. A withdrawn envelope refuses
subsequent `deliver()` calls.
Deletion intent is journaled before the sink call. Repeated withdrawal of an
unknown outcome only reconciles the receipt; it does not send another delete.
A late deletion receipt can settle `delete_held` to `withdrawn`. Reconciliation
never turns a withdrawal back into delivery success. Envelope ids are validated
before any journal, payload or receipt path is constructed.

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
- Wrong scope detected downstream → sink-side aggregate schema and integrity
  validation is the second wall; the record never stores.

## What this contract deliberately does NOT include

- No real endpoint, credential store, TLS profile or SDK client.
- No scheduler/cron/service wiring — export runs only when invoked.
- No detail-scope export path, under any authorization.
- No silent retry of PHI after an unknown outcome.
