# Governed external export contract (T16)

Provider-neutral design for exporting the T15 machine read model
(`mcs-read-model/1` records from `brain_export.py`) to an external
knowledge store — **disabled by default**. No connector, endpoint,
credential, auto-sync job or cloud dependency is shipped. The only
implementation is `mcs/ops/ext_contract.py`, which demonstrates the
contract against `LocalSink`, an in-process fake consumer backed by a
directory, and `HandoffSink`, a local staging directory that never
acknowledges its own delivery. Both are manual local paths, not connectors.

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

## Explicit C1 envelope (`mcs-ext-export/2`)

`/1` bytes, hashes, IDs, intents, default grants and journals are unchanged.
`/2` is selected only by `--c1` and an authorization whose `fields` lists
exactly the seven C1 types (`meta`, `coverage`, `message`, `message_body`,
`patient_coverage`, `signal`, `signals_truncated`), `patients: "all"`,
`max_snapshot_age_s <= 3600` and `retention_days <= 30`; a missing `fields`
refuses with `auth_fields_required`. Human confirmation, reason, expiry and
revocation are checked as for `/1`, again at send time.

- Canonical JSON (`c1_contract.canonical_json`) sorts keys by code point,
  writes integral numbers as integers and other floats in fixed notation,
  and refuses NaN/Infinity, |x| >= 1e21, 0 < |x| < 1e-6 and integers outside
  the safe range. It is a restricted subset, not arbitrary RFC 8785.
- `message_body` pairs one `body_state: full` message in the same part:
  `body_text` (UTF-8 <= 8,192 bytes, truncated on a character boundary with
  `body_truncated: true`), `body_format: text`, `body_sha256` of the sent
  bytes and `sender_kind`. Only the root `body_text` of this record is
  exempt from the forbidden-key scan.
- `patient_coverage` has one row per source patient: `fetch_state`
  (`pending`/`complete`/`incomplete`), `coverage_ts` (verified upper bound or
  null) and `history_floor` (null = no completed walk, 0 = from the start,
  positive = lower epoch; a window raises it to the window start).
- Split envelopes carry `part: {index, count, set}`. Records hashes come
  first, then `set` = hash of the index-ordered records hashes, then the ID.
  The `/2` ID and intent bind contract version and part, so `[A][B,C]` and
  `[A][B][C]` never share an identity. Unsplit envelopes omit `part`.
- Wire bytes are capped at 1,048,576 before parsing.

`sender_kind` stays `unknown` unless `--classify-senders` is given with a
snapshot. Then: the resolved self sender ID or an explicitly configured
`signals.self_organizations` match is `self_org`; otherwise a single mapped
profession is `physician` (医師), `nurse` (看護師) or `care_manager`
(ケアマネ/ケアマネジャー/介護支援専門員); any other or mixed profession —
including our own profession at another organization — is
`other_professional`; no recorded profession is `unknown`. A profession
alone never yields `self_org`. `patient_family` is reserved: no recorded
sender attribute grounds it today. Counterpart agreement on this mapping
is still required before production use.

### C0 golden fixtures

`tests/ops/ext_fixtures.py` regenerates `tests/ops/fixtures/ext_contract_c0/`:
12 accepted cases (05 is a three-part set), 23 rejected cases, one
generated-only oversize case (15, never committed), 6 receipts and 4
withdrawal directives, with expected codes in `index.json`. Every rejected
envelope has one defect and is rejected with the same code by the producer
validator, the wire parser and the reference receiver. The pin has three
layers: in-envelope hashes/IDs, `MANIFEST.sha256` (`shasum -a 256 -c`
compatible) and the fixture set ID, the SHA-256 of that manifest:

`0366f232e9d8aa1e84ab89ecee7612628589839db989726df902b192f71755e8`

The counterpart repository must record the same ID. That joint agreement,
real receipt and production acceptance are not established by these tests.

## Delivery outcomes

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
- `rejected` — a matching explicit rejection receipt. Terminal; later
  accepted receipts or another delivery call cannot turn it into success.
- `withdrawn` / `delete_held` — see below.

## Receipts and manual handoff

`parse_receipt()` validates `mcs-ext-receipt/1` without accepting unknown
fields. A receive receipt has `status: accepted` or `status: rejected`;
pending and absent status are not success. Accepted receipts bind the
envelope id, records hash and count, with per-type counts that sum to the
total. Rejections carry bounded machine reason codes, not free text.
Delete receipts explicitly report `deleted: true` or `false`; false
retains `delete_held`.

`GovernedExporter.import_receipts(path, sink)` accepts a JSON receipt,
NDJSON bundle or local directory. It validates the whole input before
import, then checks the journal's sink and intent bindings. `HandoffSink`
stages a local envelope and returns no self-ack. An independently obtained
matching receipt can settle it without another send; terminal receipt
import removes the local staging payload and retains the journal/audit.
Removal of a staging copy is not evidence of deletion at a real receiver.

The exact historical LocalSink receive/delete shapes remain compatible
for already bound journals. An unbound old journal is not automatically
given a new identity, authorization or sink. `/2` journals additionally
bind the contract, part and per-type counts; a `/2` receipt settles only
with matching per-type `accepted` counts. The external receiver's
acceptance remains a separate, unfinished agreement.

## Manual local CLI

The entrypoint is `python3 mcs/ops/ext_contract.py`. Paths are explicit;
there is no implicit private configuration, endpoint or scheduled send.

| Subcommand | Inputs | Result |
|---|---|---|
| `deliver` | `--auth --records --state --sink`; optional `--sink-kind local\|handoff` | The default LocalSink is a fake consumer, not an external upload |
| `handoff` | `--auth --state --sink` and `--records` or (with `--c1`) `--snapshot`; `/2` options `--only-with-facts --since-days N --max-bytes --classify-senders --dry-run`. No arguments reads the private `ext_export` profile | Writes local staging, returns `held / ack_unknown`, never self-acks |
| `reconcile` | `--state --sink` and exactly one of `--envelope-id`, `--all`, `--receipts` | Imports independent JSON/NDJSON/directory receipts or checks the existing journal |
| `withdraw` | `--state --sink` and `--envelope-id` or `--generation`; `--reason` | A handoff stages `withdrawals/<id>.json` and stays `delete_held` until a delete receipt arrives |
| `health` | `--state`; optional `--auth --sink --max-entries` | Read-only counts, fixed reasons and ages; no actor, payload text or secret values |
| `receive` | `--receiver-root --source-label`; `--input` with a new `--receipts-out` | Synthetic `/2` reference receiver: writes an NDJSON receipt bundle and reports receiver diagnostics separately |
| `link-hints` | optional `--snapshot --limit --cursor` | Hermes local terminal only; never a file or wire output |

`reconcile` and `withdraw` default to `--sink-kind handoff`; use `local`
explicitly for the fake consumer. Successful settlement exits 0; refusal
or rejection exits 1; unresolved reconciliation, withdrawal or health
exits 2. Creating handoff staging exits 0 while still reporting `held`;
exit 0 does not mean that an external receiver accepted it.
The old four-flag invocation without a subcommand remains supported,
including its exit 1 for a held LocalSink delivery.

Health defaults to 1,000 journal entries, permits at most 10,000, and
reads at most 64 KiB per file. Missing, malformed or truncated evidence
is unknown, not healthy. Optional sink checks establish local payload
presence only, not the external receiver's integrity or receipt.
`receive` turns contract refusals into terminal `rejected` receipts (or
`deleted: false` for a refused directive) and exits 2; receiver storage
faults write no receipt, so the sender stays held. Its transport tally is
never a claim that a collection is complete or current; the receiver's own
diagnostics report that separately. It is a local reference, not the
counterpart's encrypted staging.

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

For a handoff sink the directive is `mcs-ext-withdraw/1`: exactly
`contract`, `envelope_id`, `auth_id` and a `reason` code
(`operator_request`, `authorization_revoked`, `content_correction`,
`generation_set_conflict`), no free text, at most 4,096 bytes, written
atomically to the outbox. Staging cleanup (`discard`) never creates a
directive. `withdraw --generation` expands one generation from the durable
journal, not from the outbox, one directive per envelope (per part). A
directive arriving first leaves a tombstone that rejects the later
envelope; withdrawing one part leaves that set partial at the receiver.

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
