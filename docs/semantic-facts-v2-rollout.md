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

Set it through the activation command — it validates the resulting
config before writing:

```sh
python3 mcs/ops/mcs_setup.py fact-source shadow
python3 mcs/ops/mcs_setup.py fact-source canonical --gate-evidence report.json
```

`canonical` requires `--gate-evidence`: a `semantic_evaluation` report
scored against the shipped `evaluation/g6-criteria-v1.json` — its
`criteria_sha256` and `criteria_version` must match that file (normalized
and hashed the way the evaluator embeds them) — with `schema_version`,
`gate.pass` and `gate.g6_eligible` true, `gate.reasons` an empty list, and
`label_provenance.human` at least the shipped `min_human_labels` (200).
Synthetic, failing or foreign-criteria reports are rejected.  The config
stores a pinned token `<criteria version>:<report sha256-prefix>` (the
prefix is the shipped criteria `version`); selecting a non-canonical
source clears the pin.  A canonical config without the pin is rejected by
the production validator (`config: semantic_fact_source_gate_required`).
`mcs_setup.py init --set` cannot select `canonical` or change its pin
(including through a whole `semantic` object); an unchanged existing
selection passes through, and promotion goes only through `fact-source`.

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
5. **Relations** — `semantic_relations.reconcile` emits
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
   message's next reprocessing writes a superseding row.  Existing rows
   keep their old content until the message is reprocessed.

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
