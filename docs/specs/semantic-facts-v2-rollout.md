# semantic-facts/v2 rollout guide

`semantic_facts` is the canonical, evidence-backed representation of
clinical facts.  The legacy `extract_llm` path stays available as the
compatibility / shadow / rollback route.  This document covers the
rollout stages, the evidence gate, and the operating commands.

## Jevが利用できない場合（2026-10-29）

意味解析を有効にしている環境では、Jevが利用可能な間は従来の検証を行います。未設定、日次予算0、日上限・背景留保への到達、または正規のサービス障害では、Jevの検証を省略して処理を続けます。省略は `SKIPPED` と理由を保存し、`PASS` や実施済みの検証件数には加えません。設定不正、利用期限切れ、原文未取得、未確定の要求、既に判明した不一致は省略の対象にしません。

canonicalの省略結果は `semantic_facts_v4_skipped` に保存します。従来の `semantic_facts_v4`・`canonical_projection` に未検証の結果を混ぜません。明示的に対応した読取処理だけが、原文・処理世代・policy・文書のハッシュ・要約と検証記録の対応を確認して利用します。従来のPASS専用の読取処理は、この新しい種別を選びません。

患者コンテキストの原文検証と要約検証も、それぞれ別の省略記録を持ちます。検証済み件数と省略件数は分けます。患者コンテキストでは、Jevが復旧した場合に既存の処理枠で再照合します。投稿の新規解析と、原文・処理世代の変更に伴う再解析でもJevを利用します。省略結果は実際に再評価されるまで `SKIPPED` のまま保持し、完了済み投稿の処理を再照合待ちだけで未完了には戻しません。原文が変わった結果や配送済み通知を無条件に再送しません。canonicalへの昇格に必要な評価証拠と人の承認を、この変更で自動更新することはありません。

以下のPASS専用の処理説明は、Jevの検証を実施した経路を示します。省略した経路の状態と読取条件は本節に従います。

## Fact source modes

`semantic.fact_source` selects which extraction feeds consumers:

| mode        | canonical doc | consumers read |
|-------------|---------------|----------------|
| `legacy`    | not produced  | `extract_llm` artifacts only |
| `shadow`    | produced, not audited | `extract_llm` (canonical artifacts written for comparison; the Jev fact audit is canonical-only) |
| `canonical` | produced; PASS or separately recorded SKIPPED | PASS: `canonical_projection` / `semantic_facts_v4`; SKIPPED: explicitly supported readers only |

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
It binds existing audit, final claims/loops, rendering and delivery receipts
from a private snapshot to the same source, candidate, policy and generation
before sealing that run. Blind worksheets and an independent authorization
receipt support review; no external audit or delivery is invented or invoked.
Successful execution is not production G6 acceptance, and missing or mismatched
observations still fail the gate.

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
   reused only when fingerprint, revision, hash, boundaries, schema and
   the generation computed from the row's own model all match, and the
   producer is proven (a cloud chunk by its own receipt; a local or
   unproven chunk only when its row model is the configured local model).
   The source fingerprint does not name the local model (it keeps the
   shipped default `Qwen3.5-9B` as a hash-compatibility constant), so a
   local model change keeps cloud chunks, facts, projections and
   summaries and redoes locally produced ones while `fact_source` is
   `legacy` (the exception for `shadow`/`canonical` is in "Local model
   change" below).  Deterministic `extract_v1` hints merge as a union.
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
   admission does not apply) runs `semantic_publication.reproject_stale`, which
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

These are live API operations, separate from the isolated test runner.
Register `TYPESAFE_API_KEY` in this MCS installation's owner-only `.env`
(mode `0600`); process environment variables or Hermes' `.env` are not used.
If `semantic.mode` is explicitly configured, also set a positive
`semantic.daily_request_budget`: omitting the budget retains the previous
zero-request limit, including for manual `wire_smoke`, `jev-value`, and
live benchmark calls. These tools share the daily budget with normal work;
every HTTP attempt, retry and model-list GET counts toward a maximum of
10000 requests per JST day. A lower configured limit remains effective.
Manual live tools may bypass semantic mode and project selection, so only
run them with explicitly authorized input and API access.

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

## Local model change

- Scope: the guarantee below (a configured `local_llm.model` or
  `local_llm.url` change keeps cloud results) holds for
  `semantic.fact_source=legacy`, the default. For `shadow` and
  `canonical`, `semantic_policy.policy_fingerprint` includes
  `candidate_fingerprint`, which hashes `local_llm.resolve(cfg)` and the
  whole `local_llm` block. A local model or URL change there changes the
  policy fingerprint, so every policy-bound artifact (summary, audit,
  candidate, projection, v4), cloud-produced ones included, is redone
  once. For `canonical` the approval evidence is bound to the same
  fingerprint: the change is a config error
  (`semantic_fact_source_evidence_mismatch`, semantic falls back to
  `legacy`/off) until the candidate is approved again. Whether the
  candidate fingerprint should drop the local model is an open owner
  decision (the same open item as in `docs/specs/patient-context.md`,
  "品質確認の識別値（P0-2c）"); a strict xfail test
  (`tests/semantic/test_local_model_change_semantic.py::test_shadow_policy_ignores_a_configured_local_model_change`)
  marks it.
- Readers that hold a config (patient-context readiness and progress,
  the notice summary block and render state, derived summaries, metrics)
  judge locally produced rows by that config's local model
  (`semantic_transport.config_model_scope`), never by a host
  configuration read per message. The drain, the reprocess pass and job
  (`run_due`, `reprocess_step`, `reprocess_job`) and the other
  scope-decorated entries read the host configuration at most once per
  call (`with_local_model_scope`), as their writers and
  `execution_generation` do.
- Progress (`progress_for_message`): a current publication that awaits
  its local redo shows `processing` while its chunks are redone, not
  `attention`.
- Fallback: `semantic_job_processor.local_fallback_allowed` lets a member
  fall back to the local model unless a proven cloud chunk, or an
  unproven chunk of the configured local model, exists for it; an
  unproven chunk of another local model is reused by neither side.
- Input identity: `semantic_store.bundle_fingerprint` names no backend.
  The `local_model` key holds `_LEGACY_FINGERPRINT_LOCAL_MODEL`
  (`Qwen3.5-9B`) only so that existing fingerprints stay equal.
- Primary assessment request identity: the thread-wide fingerprint also
  moves with stored fields Jev never receives (`reply_count`, attachment
  download state, sender display name and organization).
  `semantic_store.assessment_input` hashes what one primary assessment
  asks (target, same-thread context, active questions, Jev model) and is
  recorded as `meta.jev_input` on every primary `semantic_assess` row,
  skipped rows included. It names the whole assessment, not an HTTP
  request: the client may split the questions and drops answered ones
  on retry. When a target has no row under the new
  fingerprint and the newest row recorded for the same `jev_input` and
  policy is complete and valid, `semantic_job_processor._carry_assessment`
  writes its verdicts under the new fingerprint with `jev_requests: 0`
  and `carried_fingerprint`, and no request is sent. A new or edited
  thread member changes the context and is asked again. Medication-detail
  rows, fact audits and claim audits remain keyed by the thread
  fingerprint.
- Producer rule: `semantic_policy.LOCAL_MODEL_GATED_KINDS` (facts v1/v2,
  projections, v4, summary, candidate, both chunk kinds) are current only
  while `extraction_provenance.local_producer_current` accepts the row:
  a complete proof naming only cloud providers is always accepted; any
  other row is accepted only when its `model` column equals the
  configured local model. A row that never recorded a model keeps its
  earlier fingerprint-only identity. `current_artifact` and the send gate
  (`_paired_summary`) apply it to the latest matching row and never fall
  back to an older one.
- Not gated (`LOCAL_MODEL_UNGATED_KINDS`): Jev results, repair and format
  repair reservations (a spent repair is never refunded), loop
  completion receipts and bookkeeping kinds. A guard test classifies
  every `KIND_*` constant.
- Loop completion receipts compare `runtime.interpretation_generation`,
  which is `execution_generation` computed with the shipped default
  model. `execution_generation` itself still names the local model and
  re-queues reprocess-stamped jobs after a model change.
- Fact binding: a local model change can rewrite the facts of an
  unchanged fingerprint, so everything derived from them is judged on
  every attempt from durable state, never from what one pass remembers.
  `semantic_store.claim_input_binding` (claims + projected facts) is
  recorded on the summary audit, the pre-audit candidate and the
  empty-repair candidate; `semantic_store.fact_input_binding` on the
  source coverage evaluation. `semantic_store.built_over_current_facts`
  compares the recorded binding with the current facts; a row without
  one (written before bindings, an oversize stub, a PENDING
  `summary_unavailable` audit or a fact-stage park receipt) counts only
  while no current fact row was written after it.
  - Summary: the job processor (`_summary_built_over_current`) judges
    the current summary by the audit written with it
    (`semantic_store.paired_audit`, found through `summary_artifact_id`),
    so a later PENDING stub of a failed regeneration does not hide which
    facts it had. A summary reused as is (PASS/NEEDS_REVIEW) must also
    have been published with the current fact document: the latest S8
    receipt (`semantic_publication.published_doc_hash`) carries the
    document hash it was rendered and published with, the same document
    the send gate's `semantic_facts_v4` chain requires. A found S8
    receipt without a `doc_hash` (missing or null, e.g. written while
    the target had no document) counts as another document; with no S8
    receipt the summary counts only while no current fact row was written
    after it. Any other summary is dropped and taken through the
    candidate path below (a cloud summary over redone local facts
    included), never re-verified as is or deferred. Identically redone
    facts keep a summary that is still current for its producer with no
    call; a summary whose producer is not current (a local or unproven
    repaired summary after a local model change) is redone even then.
    The send gate (`_claim_binding_current`, `_canonical_chain_current`)
    stays the publication check; the former stop reason
    `prior_pass_unverified` was removed, and a PASS it cannot verify is
    audited again (see Reused PASS below).
  - Cost when only the S8 check drops a summary (its facts' projection
    unchanged, e.g. an S8 receipt without `doc_hash`), per target, at its
    next same-fingerprint run (reprocess, pending resume or hint rebuild;
    never at upgrade itself): with a usable candidate, one Jev
    claim-audit request and no summarizer call; without one, one
    summarizer call and one Jev claim-audit request. A paid cloud call is possible only in the second case and
    only when the summary route is cloud. It is one-time: the result
    commit writes an S8 receipt with the current `doc_hash` (an
    identical result keeps the stored summary row).
  - Candidate: a pre-audit candidate whose claims are bound to the
    current projected facts is audited and rendered with the current
    document (no summarizer call), also after a document change that
    leaves the projection unchanged — except a candidate a repair was
    reserved after (`_bound_candidate`): its audit rejected it, so the
    summarizer runs once without a repair (the spent repair is not
    repeated; a rejection again is NEEDS_REVIEW). An empty-repair
    candidate is written after its reservation and stays usable. Any
    other candidate is replaced.
  - Empty repair: a reservation reserves nothing once a final
    (PASS/NEEDS_REVIEW) result of its candidate was committed
    (`empty_repair_consumed`: an audit after the candidate counting the
    repair, paired by id and hash with a summary carrying the candidate's
    claims; judged on the latest `empty_repair` candidate of the same
    fingerprint linked to the marker, so a later redone pre-audit
    candidate does not hide it), when it was made before the current fact
    row and its candidate is not bound to the current facts
    (`empty_repair_over_earlier_facts`), when its candidate was written
    by a local model that is no longer current and would otherwise be
    recovered with nothing recorded — every recovery gate satisfied,
    provenance proven, `audit_state` `ready`/`inflight` and `partial_answers`
    `{}` (`empty_repair_candidate_producer`), or
    when the summary/audit pair it was reserved over was superseded
    without its result (a newer row, or
    a recorded row now invalidated or stale) and its candidate would
    otherwise be recovered (provenance proven, no recorded answer or
    finding). The target then takes the
    ordinary path: a bound candidate is audited again, otherwise it is
    summarized again; the spent repair stays recorded on its marker (no
    refund, no second repair; an empty result again is the usual
    permanent hold). What the audit of the repaired text observed is
    recorded before the pass can stop, and only a negative observation
    holds: an answer `contradicts`, `not_supported` or `ambiguous`, one
    below the match threshold, or another Jev audit finding
    (`support_unevaluated` alone is not one). An answer that is not a
    valid choice is not a judgement of its claim and is never recorded:
    that claim is asked again (once in the same call, then on the job's
    retry). The valid answers of the same response are recorded before
    that re-ask (`audit_claims` checkpoints them through
    `evaluate_once_more`'s answer callback), so a stop during it keeps a
    negative one. On a
    guard stop inside the audit (`LLMNotSent`, `RuntimeBudget`,
    `RuntimeOff` such as a pause, off, scope or review hold, or a
    `RuntimeStale` config change) the answers already received are
    judged by the same rule as the audit (`claim_answers_findings`):
    with a negative one they are written `held` with their findings;
    with only confident `supports` answers nothing negative is kept and
    the candidate is written `ready`, so the next pass runs a new,
    complete audit (those answers are asked again; a PASS comes only from
    that audit). When the audit returns, a verdict with findings is
    written `held` before the result commit, until that result is
    committed (`empty_repair_consumed`); an evaluated one also carries
    `evaluated: true`, a clean one writes `ready`. This record only
    holds or re-audits, so it is checked only for job ownership and an
    unchanged source (`owner_guard`). Only a crash, a stop after which
    the job is no longer this worker's (re-queued or taken over), or a
    changed source leaves `inflight` with no answer: the repaired text is
    then audited again, like a stored PENDING summary, and an answer
    observed before that stop is asked again. A Jev result refused by its
    result guard is not read, so it is not observed.
    Recorded negative answers: a stop after a negative answer, with or
    without an evaluated verdict, does not hold for good. When the `held`
    record has valid answers on some of the candidate's claims (all of
    them when it is `evaluated: true`) and at least one finding, all of
    them claim findings (`claim_contradicts`, `claim_not_supported`,
    `claim_ambiguous`, `claim_low_confidence`) on answered claims, the next
    pass recovers the candidate with that record and finishes its audit
    (`resume_claim_audit`): only the claims without a recorded answer are
    asked; the recorded answers and findings are kept as observed, never
    asked again or upgraded. When every claim is answered nothing is asked
    (also while Jev is skipped), and the result is committed without a new
    local-model call: NEEDS_REVIEW (never PASS; any other status is written
    PENDING), paired with a summary of the candidate's claims and counting
    the one repair, which consumes the reservation. While claims remain
    unanswered and Jev is skipped or unavailable, the target stays pending
    with the record unchanged and is retried later; a resumed audit that
    stops again writes the grown record (`held`, the earlier answers and
    findings kept), never `inflight` while a record exists, so a crash
    during or after the resumed audit never loses the recorded negative.
    The candidate is bound to the current facts by the usual recovery
    gates, so the combined audit covers them. A superseded prior pair does
    not release such a candidate; it stays held (deferred, no attempt
    spent) while it is bound to the current facts. A fact row written
    after the reservation that the candidate is not bound to releases it
    (`empty_repair_over_earlier_facts`), and a changed source fingerprint
    leaves it behind: the ordinary path then summarizes the current facts
    again and audits that summary (no second repair).
    Remaining holds: a recorded
    prior pair changed in place, gone, or newer than the current row
    (`empty_repair_prior_unverified`), and a candidate with a recorded
    finding or answer that cannot be verified as above (no answer, a
    finding on an unanswered claim or with another code, an invalid
    answer, an answer for another claim, `evaluated` not true or not
    covering every claim, another shape) (`empty_repair_audit_not_ready`),
    also when its prior pair was superseded; neither recovers
    automatically over the same facts, and a recorded negative is never
    released or outvoted by a later audit of the same claims and facts.
  - Reused PASS: a PASS whose pair the send gate cannot verify (e.g. an
    audit without `summary_artifact_id`/`claim_input_binding`) is not
    reused or deferred; the stored summary is audited again over the
    current facts and only that new pair can publish it. With the Jev
    audit skipped (disabled, budget, review hold) it is not cleared: the
    target stays PENDING with `prior_audit_unverified`, as for an unbound
    PENDING prior, until a real audit runs. An identical
    result is not recorded again only while the current summary and its
    audit are the newest, paired rows of their kinds and already hold
    every receipt field the new write would (summary and audit hashes,
    claim binding, target revision, publication mode, schema, status):
    a re-audited PASS over a pair the send gate refuses for any of these
    is written as a new pair, so it can publish.
  - Stage boundaries: facts → fact audit/projection (bound to the
    document hash) → medication details (fact id and options) →
    coverage (binding) → candidate (binding) → audit → repair
    reservation (`repair_count`/marker) → result commit (summary, audit,
    S8 and `semantic_facts_v4` in one transaction). An interruption at
    any boundary leaves only rows these checks judge again on the next
    attempt; `semantic_facts_v4` is republished in the result commit,
    which a summary over another document now always reaches. Chunks are
    bound to their producer.
- Legacy `semantic_facts` rows now carry `provider_provenance`
  (`stage: facts_v1`); rows written before it are judged as local.

