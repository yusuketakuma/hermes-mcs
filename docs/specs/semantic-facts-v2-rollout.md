# semantic-facts/v2 rollout guide

`semantic_facts` is the canonical, evidence-backed representation of
clinical facts.  The legacy `extract_llm` path stays available as the
compatibility / shadow / rollback route.  This document covers the
rollout stages, the evidence gate, and the operating commands.

## Fact source modes

`semantic.fact_source` selects which extraction feeds consumers:

| mode        | canonical doc | consumers read |
|-------------|---------------|----------------|
| `legacy`    | not produced  | `extract_llm` artifacts only |
| `shadow`    | produced, not audited | `extract_llm` (canonical artifacts written for comparison; the Jev fact audit is canonical-only) |
| `canonical` | produced, must PASS audit | `canonical_projection` artifacts (legacy shape, canonical source) |

Set it through the activation command; it validates the resulting config
before writing. The default remains `legacy`.

```sh
python3 mcs/ops/mcs_setup.py fact-source shadow
python3 mcs/ops/mcs_setup.py fact-source canonical \
  --gate-evidence /private/evaluation/report.json \
  --confirm-human --reason reviewed_sealed_holdout
```

`canonical` requires the exact shipped [v2 criteria](../../evaluation/g6-criteria-v2.json),
a metrics-only report bound to the loaded runtime candidate and sealed local
run, and independent human approval. The command recomputes metric gates;
`gate.pass=true` or human-label declarations alone do not authorize promotion.
Synthetic data remains ineligible even when humans label it. The test split
needs at least 200 human-labelled, authorized real cases, the prescribed
patient/thread/stratum counts, metric denominators and complete observations.
See [sealed candidate evaluation](SEALED_CANDIDATE_EVALUATION.md) for the
input contract, exact thresholds, receipts and remaining acceptance work.

The config stores `fact_source_evidence`, `fact_source_approval`, and a full
`<criteria sha256>:<report sha256>:<candidate fingerprint>` token in
`fact_source_gate`. A separate owner-only approval receipt under
`data/semantic_approvals/` binds those values and the snapshot/holdout hashes.
Runtime rechecks both the evidence and receipt. Code, prompt, model ID,
interpretation settings or threshold changes invalidate the binding; missing
or mismatched evidence fails closed to legacy/off. The receipt is not a
signature against an attacker with the same OS account.

The original [v1 criteria](../../evaluation/g6-criteria-v1.json) remain for
historical comparisons. Old v1 reports and shortened version/report tokens
cannot activate the current candidate. `fact-source legacy` or `shadow`
clears config references but preserves approval history. `init --set` cannot
select canonical or change its pin; promotion goes through `fact-source`.

The sealed runner generates local-model candidates and scores sealed labels.
It does not yet join the existing audit, final claims/loops, rendering and
delivery receipts into that run. Its successful execution is therefore not
production G6 acceptance, and missing observations still fail the gate.

## Canonical pipeline

Per target message, inside the shared drain queue:

1. **Manifest** — the source text is atomized (clauses, list items,
   table rows, headings, attachment refs).  Atoms cover every source
   codepoint exactly once; each atom belongs to exactly one chunk's
   `core_atom_ids`.  Persisted as `semantic_source_manifest`.
2. **Preflight (Jev)** — per chunk per mandatory category, one presence
   adjudication (`present`/`absent`/`uncertain`), stored as `jev_pre`
   obligations.  Failures create `failed` obligations — they never
   silently close.
3. **Extraction** — `_FACT_V2_PROMPT` per chunk through the shared
   bounded local-LLM transport.  Evidence resolves only inside the
   owning chunk; a quote that lives elsewhere stays `unverified`.
   Completed chunks persist before the next request; cached chunks are
   reused only when fingerprint, revision, hash, boundaries, and schema
   all match.  Deterministic `extract_v1` hints merge as a union.
   Duplicate facts across chunks merge by stable `fact_id`.
4. **Obligations** — every (chunk, mandatory category) pair carries a
   deterministic obligation plus the Jev `jev_pre` obligation.
   `explicit_no_fact` requires an adjudicated absence with no
   contradicting signal.  Open/ambiguous/failed obligations keep the
   coverage `incomplete`.
5. **Relations** — `semantic_relations.reconcile_facts` emits
   `EXPLICIT_SUPERSESSION` only when temporal ordering is known;
   unordered conflicting pairs become `CONTRADICTION` — never silent
   latest-wins.
6. **Post-generation audit** — `audit_facts_v2` runs a bidirectional
   check: every verified fact against its own evidence quote inside
   the full source context, plus the source→facts coverage Choice.
   The audit artifact is keyed by `doc_hash`, so a repaired document
   is always re-audited.  Unevaluated work is `INCOMPLETE`/`PENDING`,
   never `PASS`.
7. **Targeted repair** — on `NEEDS_REVIEW`, `repair_facts_v2`
   re-prompts only the chunks owning rejected facts.  One dispatch per
   generation, bounded by the persisted `semantic_facts_repair`
   receipt.  Rejected facts are removed; repaired items merge through
   `fact_id`; obligations re-link conservatively.
8. **Mandatory rendering** — `mandatory_render` lists every verified
   fact and discloses every non-terminal obligation in the stored
   summary and the rendered notice, even if the model summary dropped
   them.  Each line carries the stated quantity, polarity, epistemic,
   workflow status, action, valid time and actor (codes, omitted when
   `unknown`) next to its `ID:`/`証拠:` binding.
9. **Projection** — an audited doc also writes `canonical_projection`:
   the legacy `extract_llm` content shape (meds/symptoms/events/
   requests), honestly lossy (allergy, adverse events, vitals,
   preferences, observations have no legacy slot).  Read-side consumers
   (`mcs_stats`, `mcs_queries`, `mcs_signals`, `rollup`) prefer a
   hash-current projection over `extract_llm` via `current_fact_pred`.
   The legacy slots never assert what the canonical doc leaves open:
   adherence/administration reports are observations (not `meds`); a
   cancelled workflow or a performed/reported stop is `past`; a
   medication event with no, `hold` or `consider` action, an endpoint
   of a `CONTRADICTION`/`UNRESOLVED` relation, or a fact without a
   bound evidence quote is `unverified` (never a current patient
   medication or a completed care transition); requests keep the full
   statement plus an `unverified` flag.  Rows carry
   `meta.projection_version` (`semantic_projection.PROJECTION_VERSION`);
   `canonical_projection` and `semantic_facts_v4` rows are reused only
   when that version (and the audited `doc_hash`) match, otherwise the
   message's next reprocessing writes a superseding row.  Rows already
   written by an older version are brought forward without waiting for
   reprocessing: each semantic drain tick (after `invalidate_projections`,
   before the endpoint breaker check — no model or Jev call, so LLM
   admission does not apply) runs `semantic_v4.reproject_stale`, which
   takes at most `REPROJECT_LIMIT` (25) CURRENT rows (the reader
   predicates `current_projection_id`/`current_v4_id`, body not
   deleted) whose `projection_version` is missing or older, re-renders
   them with `project_v2_doc_legacy` from the stored audited document,
   and appends a superseding row with the same binding meta (fingerprint,
   policy, hash, doc_hash, engine/extract version, model) plus the new
   `projection_version` and `reprojected_from`; the patient's rollup is
   dropped for rebuild.  The document must be the generation's newest
   `semantic_facts_v2` row with the bound `doc_hash`, complete coverage
   and a stored PASS fact audit; otherwise the row is left serving, marked
   `reproject_skipped` (counted by reason in the drain's `reproject`
   result) and is updated only when the message is drained again.
   Invalidated and superseded rows are never touched, and non-PASS
   generations have no v4 row to resurrect.  The pass runs whenever
   `semantic.mode` is not `off`, whatever the current `fact_source`:
   rows published during a canonical period stay readable after a
   switch back to `shadow`/`legacy` and are re-derived from their own
   bound fingerprint/policy/doc_hash.  With `mode: off` nothing runs.

## Artifacts

| kind | contents |
|------|----------|
| `semantic_source_manifest` | atoms + chunk ownership for the fingerprint |
| `semantic_extraction_chunk_v2`  | resumable per-chunk extraction records |
| `semantic_facts_v2`        | the canonical contract document |
| `semantic_facts_audit`     | post-generation audit verdicts + findings (doc_hash keyed) |
| `semantic_facts_repair`    | the one-shot repair receipt per generation |
| `canonical_projection`     | legacy-shaped read model for consumers |

## Failure semantics

- Missing input, failed chunks, dropped items, unverified facts, open
  obligations, unevaluated audits → `incomplete` / `PENDING` /
  `NEEDS_REVIEW` / `INCOMPLETE`.  Nothing degrades to `PASS`.
- Canonical mode never falls back to the legacy projection: when the
  canonical doc or its audit is incomplete, the generation holds.
- Jev outages classify via `_jev_failure_class`: `resource` waits,
  retryable errors retry, hard failures stop the job — each recorded.

## Evaluating Jev's incremental value

```sh
python3 mcs/ops/mcs_setup.py jev-value \
  --cases evaluation/semantic_completeness_cases.json \
  --out /tmp/jev-value.json
```

Runs canonical extraction + audit over the labelled corpus and reports
`audit.deterministic_findings` vs `audit.jev_incremental_findings`
(per-fact support verdicts, coverage misses), `extraction
.mandatory_recall`, and `gate.pass` (true only when every case was
actually evaluated).  Without `TYPESAFE_API_KEY` the audit cannot run
and the report records `evaluated: false` — never a synthetic pass.

The Jev incremental-value experiment is a separate synthetic comparison; its report is not the sealed v2 activation evidence described above.
