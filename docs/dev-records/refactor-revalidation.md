# Refactor revalidation ledger

This document records the offline revalidation for RT-034 through RT-040 in
`MCS-REFACTOR-FIRST-20260920`. It is evidence for the current worktree only;
the historical RF-CODE result in `docs/phase-r-record.md` remains tied to
`46cffb0`.

## Fixed tree and scope

The current committed base inspected here is
`842125da858d4bddc09e7e778bee5e6068c521d9`. The historical RF-CODE tree is
`46cffb0`. The committed diff from that tree contains 11 paths, 4,525 added
and 35 removed lines, including `semantic.py`, `semantic_jev.py`, semantic
tests, notifier, ledger, view, job, and run-check changes. The worktree also
has uncommitted semantic/notifier test and harness files; therefore this is a
revalidation record, not a new RF-CODE PASS.

The inspection commands are reproducible without reading the live checkout:

```text
git diff --stat 46cffb0..HEAD
git diff --name-status 46cffb0..HEAD
git status --short
git diff --check
```

No live database, token, config, Chrome profile, MCS request, Discord request,
Keychain lookup, commit, push, or deployment was used for this record.

## RT mapping

| Requirement | Evidence | Result and boundary |
|---|---|---|
| RT-034 | `adapter/test_mcs_recovery_contract.py::test_interrupted_backup_retains_last_verified_generation` | **PASS for synthetic path.** A real SQLite source is backed up, `backup()` is interrupted for a new generation, the prior verified file remains byte-identical and valid, and the next run removes the abandoned `.tmp` before publishing. |
| RT-035 | `adapter/test_mcs_recovery_contract.py::test_independent_restore_preserves_relations_fts_snapshot_and_attachment_hash` | **PASS for synthetic path.** An independent directory receives the DB, attachment directory, and manifest. Patient/message/attachment counts, parent relation, FTS hits, read-only quick check, snapshot reader, and manifest/file hash are compared logically. SQLite byte identity is intentionally not required. |
| RT-036 | `adapter/test_mcs_recovery_contract.py::test_explicit_snapshot_cli_survives_foreign_cwd_and_home` | **PASS for the explicit snapshot route.** The JSON facade runs from a different CWD and HOME and returns exit 0 without creating a HOME-relative `.mcs`. Other legacy import/command variants remain outside this test. |
| RT-037 | `adapter/conftest.py` plus the test command below | **PASS for this lane.** The harness blocks URL/socket access, Keychain/Chrome subprocesses, and resets HOME. The recovery test has no external fixture. |
| RT-038 | The RT-035 test uses one fixed fixture, counts SQLite backup calls, and applies `SYNTHETIC_WALL_LIMIT_SECONDS = 5.0` declared before timing. | **PARTIAL.** Current-path synthetic evidence records one backup operation and a bounded smoke time. It is not an old/new production performance benchmark; real-device latency, peak memory, and a complete request budget comparison are **NOT_TESTED**. |
| RT-039 | `git diff --stat/name-status 46cffb0..HEAD` and the current dirty status | **Historical ordering verified.** `46cffb0` contains neither semantic.py nor semantic_jev.py and precedes the Phase J commit `a7f6b66`. The current candidate is a post-feature tree, not a replacement pre-feature RF-CODE tree. Its regression acceptance remains separate. |
| RT-040 | Current HEAD and dirty worktree inspection | **REVALIDATION REQUIRED.** The historical evidence is invalid for changed code/config/schema and must not be reused as a current PASS. The semantic/notifier changes require their own gate and regression result. |

## Commands and result

Run the owned contract file with the project runner and the pinned test
interpreter:

```text
MCS_TEST_PYTHON=/Users/yusuke/.hermes/hermes-agent/venv/bin/python \
  scripts/run_tests.sh adapter/test_mcs_recovery_contract.py -q
```

The expected result for this file is three passing tests. The fixture is
synthetic and uses a temporary directory; the timing value is only a stable
smoke bound for this tiny database. It must not be reported as production
throughput or device performance.

The relevant logical comparison fields are fixed before execution: patient,
message, attachment counts; reply `parent_id`; FTS message ids; SQLite
`quick_check`; snapshot reader timeline/thread/attachment rows; manifest byte
length and SHA-256; backup call count; and CLI JSON/exit status. IDs and
generated snapshot timestamps are not compared as byte values because they are
expected to vary.

## Gate disposition

RT-034 through RT-036 now have local synthetic evidence. RT-038 has only the
bounded current-path smoke comparison described above. RT-039 historical sequencing is verified below. RT-040 requires current
candidate regression evidence and a frozen candidate fingerprint. The historical
RF-CODE record applies only to `46cffb0`; feature work downstream does not erase
that historical boundary and does not inherit its test results. RT-038 resource
comparison remains partial.

## Historical phase boundary verification

Read-only Git inspection confirmed this sequence:
`a8896ec` baseline → `9d2c6d1` REF → `46cffb0` FIX →
`669a11a` R-10 record → `a7f6b66` Phase J feature.
`git merge-base --is-ancestor 46cffb0 HEAD` returned 0.
`git ls-tree -r --name-only 46cffb0 adapter/semantic.py adapter/semantic_jev.py`
returned no paths. The pre-J record is available with
`git show 669a11a:docs/phase-r-record.md` and states RF-CODE PASS for `46cffb0`.
These checks verify sequencing and the recorded historical verdict; they do not
reproduce its historical live tests or certify the present dirty candidate.

## Current D01–D15 connection recheck (2026-09-21)

Compared actual file bytes with `git show 46cffb0:adapter/<file>` after the
297-test checkpoint. Seven modules are byte-identical: mcs_adapter, mcs_util,
extract, extract_llm, rollup, maintenance, init_data. The coordinator, ledger,
job_ops, notifier, requests and view differ; their historical PASS is not
substituted for current tests. The following classification describes the
post-RF additions, not a second pre-feature refactor.

| Domain | Current disposition and boundary | Current evidence / limit |
|---|---|---|
| D01 configuration/CLI | FEAT in semantic, view, requests; original config/lock helper unchanged | semantic CLI, operation CLI, foreign-CWD snapshot tests; deployment config untouched |
| D02 authentication/CDP | Maintain byte-identical MCS adapter | No new live login or Keychain test |
| D03 MCS HTTP | Maintain byte-identical adapter; separate FEAT Jev transport | Partial-page/unknown ACK regressions; Jev synthetic POST succeeded; no new real MCS call |
| D04 unread/replies | Maintain parser/transport; coordinator passes semantic seed into existing save | Added commit-boundary, snippet hydration, attachment-only tests |
| D05 history/discovery | Maintain retrieval; extend existing writer paths with semantic seed | Added 401/timeout and old-parent/read-reply connection tests |
| D06 archive | Maintain lifecycle; semantic eligibility excludes archived data | Existing archive registration/notification suppression/reappearance tests in current suite |
| D07 SQLite | FEAT/FIX within existing writer and transaction ownership | Commit interruption, source/attachment reseeding rollback, job generations; no new live migration |
| D08 jobs/inbox | FEAT bounded semantic jobs and ops commands, FIX retry/CAS boundaries | Budget/manual retry/circuit/pause and authenticated command receipts |
| D09 attachments | Existing download unchanged; FEAT revision-bound semantic context | Attachment hash/metadata changes seed atomically; unparsed content explicit |
| D10 ACK | Maintain adapter and optional stage gate | Added save-before-ACK and durable unknown regressions; no live ACK or policy change |
| D11 extraction/rollup | Existing three modules unchanged; FEAT separate semantic extraction/assessment/summary | Legacy output selectors retained; quantity and coverage guards; G6 human quality untested |
| D12 notification | FEAT in existing notifier, no second sender | Frozen parts, policy/source/PASS gate, raw/attachment regression; no live delivery |
| D13 view/requests/CCO | FEAT standalone Hermes plugin and generic native-input context | Synthetic native Discord→allowlist→preview/confirm→inbox/receipt integration passes; real permissions untested |
| D14 backup/recovery | Maintain byte-identical maintenance | Synthetic interrupted backup/independent restore/read-only snapshot; no production restore |
| D15 scheduler/budget | Existing run coordinator/lock, FEAT budgeted semantic tail | Real synthetic tick order, no-work deferral, dispatched-work retry; real device occupancy unmeasured |

The latest adapter run is 297 passed in 6.65s, and Hermes synthetic integration
passes at the same source state (1 test, runner wall 1.1s). These are local
regression evidence for changed boundaries, not RF-OPS/G7 approval or G6
semantic-quality evidence. Historical REF/FIX commit separation is preserved;
this continuation's additions and repairs are recorded in the continuation
ledger without creating commits.

## RT001–033 evidence index for the continuation

Specification §9 was re-read against the current test bodies and source
entry points. This index distinguishes current regression coverage from the
old/new equivalence required by REF. A current test passing alone does not
prove differential equivalence. `test_mcs_ingestion.py` is abbreviated `I`,
`test_mcs_features.py` is `F` below. All identified current tests belong to the
297-test checkpoint; no additional live calls were made for this index.

| RT | Current evidence and unresolved scope |
|---|---|
| 001 | continuation baseline records base/dirty state and baseline failures; candidate overlays preserve changes. Latest test additions still need a new frozen overlay. |
| 002 | D01–D15 table above maps actual files and present evidence. |
| 003 | I.test_tick_real_storage_snapshot_and_replay exercises real storage/CLI stage replay. Full old/new request and CLI differential remains unproven. |
| 004 | I reply merge, sibling-save and snippet tests; native parent relation assertions. No complete historical differential fixture set. |
| 005 | I.test_backfill_page_failure_keeps_saved_page_without_coverage and pagination page-cap/schema tests preserve partial pages. |
| 006 | I.test_snippet_update_keeps_body_and_hash_aligned and source revision tests preserve full body; all deleted transitions not individually re-audited. |
| 007 | I discovery register/recovery and archived job tests retain existing discovery path. |
| 008 | I one-page cursor, monotonic floor, pending history/head coexistence tests. Multiple-run historical differential not independently reproduced. |
| 009 | I.test_backfill_recovers_read_reply_on_old_parent_without_hiding_gaps; no claim of live pinned-order completeness. |
| 010 | I.test_archived_registration_atomic_and_transition checks archive/job atomicity. |
| 011 | I.test_discovery_archived_off_skips_kartes was read in full: it creates an archived pending history_head, rejects any archived enumeration call, runs discovery with include_archived=False, then drains the existing job and asserts done. The combined scenario is already covered; no duplicate test added. |
| 012 | I archive suppression, old-history reply, read-reply tests; no new qualification from replay/import. |
| 013 | MCS adapter and util byte-identical to RF tree; current parser/transport tests. No new live fetch/redirect test. |
| 014 | MCS authentication unchanged; actual Keychain/foreign-origin login is not tested in this continuation. |
| 015 | I commit-boundary and durable unknown ACK tests; snapshot timestamp validation unchanged. Real new-arrival race intentionally untested. |
| 016 | Historical REF schema unchanged; current v4/v6 interrupted migration tests inspected. No new schema migration introduced by these boundary tests. |
| 017 | I.test_unread_commit_boundary_preserves_work_before_ack, attachment reseed rollback and request/Loop transaction rollback. |
| 018 | save_patient owns its existing transaction and calls semantic seed inside it. Whole-writer transaction-duration/differential audit remains incomplete. |
| 019 | I.test_identical_text_and_time_preserve_distinct_message_ids_and_projects; hash/full-body revision tests. Multi-account same numeric ID is outside current global-ID schema evidence. |
| 020 | F inbox rejection/commit-before-unlink, request replay/atomicity tests. Existing command validation rather than a second queue. |
| 021 | Existing cursor/job preservation tests plus finite manual semantic retry and unchanged-generation seed tests. Full historical serialized-job corpus unavailable. |
| 022 | I run-lock second-writer and CLI lock tests; coordinator keeps the shared lock. No real service concurrency intervention. |
| 023 | Semantic real tick order, budget-before/after-dispatch, bounded transport trickle tests. Real device occupancy unmeasured. |
| 024 | I attachment ledger identity and cumulative-size tests; attachment source hash/metadata binding. |
| 025 | I.test_notify_pending_attachments_jump_download_queue. |
| 026 | I rejection-to-text and ambiguous-error no-fallback tests; adapter cleanup unchanged. All physical disk-failure points not exercised. |
| 027 | Semantic OFF/shadow frozen output and original delivery regressions. Full historical byte comparison for every attachment combination not claimed. |
| 028 | Frozen progress/receipt tests and semantic send gate between parts. Real Discord 429/receipt failure not injected. |
| 029 | I no-notify real tick and missing-channel tests; notifier patient destination has no fallback. Every possible exception branch not enumerated. |
| 030 | extract/extract_llm/rollup byte-identical to RF tree; separate semantic code does not replace legacy prompt or selector. |
| 031 | I LLM retry after edit, malformed retry metadata, old artifact and malformed rollup tests. |
| 032 | F request atomicity/replay/revisions and native Hermes synthetic integration; reason/Loop links preserve requests as authoritative state. |
| 033 | F snapshot generation/read-only/cursor scope tests and foreign-CWD snapshot CLI test. Real OS read-only deployment still untested. |

Outstanding RT work is now explicit rather than hidden behind the overall
pass count: historical differential breadth, whole-writer transaction boundaries, and
operational-only checks.
This does not reopen unchanged historical REF work or authorize live tests.

RT011 follow-up: the existing combined test is included in the 297-test successful run. Reading its body resolved the previously recorded uncertainty; no code change or repeated test run was needed.

## RT018 transaction boundary inspection

Read the changed save_patient/save_messages/save_thread_replies bodies,
_semantic_seed_tx, mcs_requests.apply_command and the semantic promotion
block. The three ingestion owners use one `with db`; their added seed uses
SQL-only `_semantic_seed_tx`, not the committing `semantic_seed` wrapper.
Attachment update and seed follow the same owner pattern. The request owner
uses BEGIN IMMEDIATE and writes the request/Loop link or ops result plus
receipt before exiting; `mcs_operations.apply_tx` dispatches non-committing
SQL handlers. Semantic result, notification plan, outbox and done-CAS use
`artifact_add_tx`, `outbox_add_tx`, `transition_tx` under the promotion owner.
Loop matching explicitly runs before that transaction because it calls Jev.
Completed partial target results have a separate intentional durable save so
retry does not repeat already-consumed analysis and repair.

The inspection establishes these changed boundaries; fault/rollback tests in
the 297-test suite support them. It does not measure transaction duration or
prove absence of every possible nested commit across unchanged maintenance
and legacy writers. Those unchanged modules were byte-compared separately.
No network or live database was used for this review.

Historical REF diff inspection: `git diff a8896ec 9d2c6d1` covers seven
files (98 additions, 88 removals). Read every changed hunk: common config,
HTML conversion, no-redirect/no-proxy construction and flock extraction;
ledger schema and extraction prompt are not changed in that REF. This is
source-level evidence of the intended boundary, not a replacement for an
executed old/new behavior differential. The separate manual-writer lock FIX
remains tied to 46cffb0.

## Re-executed historical baseline and REF regression

On 2026-09-21 extracted all adapter files from a8896ec and 9d2c6d1 into
`/var/folders/yg/_v84mvr55kb5dqdpzhvm79bc0000gn/T/mcs-ref-comparison-9cuhynlk/`.
Added only the current isolated runner and conftest network/credential guard
to each temporary tree. Ran the same command in each root:

```
MCS_TEST_PYTHON=/Users/yusuke/.hermes/hermes-agent/venv/bin/python scripts/run_tests.sh adapter -q
```

- a8896ec: 86 passed in 1.00s.
- 9d2c6d1: 86 passed in 1.06s.

This independently reproduces the historical baseline/REF regression outcome
without live credentials, MCS, Discord, or Keychain. It does not run the
subsequent writer-lock FIX and does not prove every old/new output byte or
resource metric is identical. The tiny timing difference is not a performance
benchmark. These results apply to the extracted Git objects, not the current
post-feature candidate.

## Executed observable-output differential

Ran identical `test_ref_observable_probe.py` via the isolated runner in both
extracted trees: each 1 passed in 0.03s. Fixtures cover zero posts, a normal
HTML parent, a parent plus short reply, multiple projects, and duplicate
replay. Compared schema version, selected message IDs/project/parent/body/
quality/hash fields, new IDs, outbox intent payload, rendered notice text and
file list; both JSON outputs are byte-identical. No read mark was generated.
Generated timestamps were intentionally excluded; fixture inputs were equal.

Evidence and reproducible probe:
`/Users/yusuke/.codex/artifacts/mcs-ref-observable-comparison/`.
Output SHA256:
`f63ca1578ac9f2580913d4ef58bd495594a1fa32f47167e8da2ea812aa0301c7`.
This probe covers RT003/004/019 and text-only RT027 portions. It does not
cover real transport, downloaded attachment manifests, every CLI exit status,
or resource/operational behavior.

Attachment differential extension: both historical trees pass the identical attachment probe (1 test each, 0.03s). Three synthetic local files include same-name parent/reply files with different contents. Compared rendered notice text and ordered filename/byte-length/SHA256 manifests; results are identical, SHA256 8b7ffaee78f5877008d60286a925ec10387d50a2f457a11f8fa8522a457ff60d. Probe/results are preserved beside the text-only evidence. Absolute temporary paths are normalized to file identity; no upload/download occurred. This extends RT024/027 coverage but is not live Discord delivery.

## Historical FIX reproduction

Extracted 46cffb0 into the same isolated comparison root with current runner
and credential/network guards. Full historical suite: **88 passed in 0.93s**.
Then copied only its test_mcs_ingestion.py (the Git diff shows just the two
lock tests added) into the temporary 9d2c6d1 REF tree, leaving product code
unchanged, and ran `-k cli_writer_holds_run_lock --tb=short` through the runner.
The test failed as expected: `extract_llm.main()` returned 0 while another
writer held the lock, expected 3. This reproduces the reason for the separate
FIX rather than treating that unsafe old behavior as an equivalence contract.
The same test passes in the 46cffb0 88-test run. All DBs and lock files were
synthetic temporary files. The REF temporary test tree now includes those
FIX tests; the product files still correspond to 9d2c6d1.


## Current fingerprint and coordinator recheck (2026-09-21)

Revalidated every adapter Python hash in `docs/g1-validation.json`: no
mismatches. The seven unchanged modules listed above are still byte-identical
to `46cffb0`; `git diff --check` exits 0. Current regression evidence is
336 adapter tests passing, followed by three pause tests passing after adding
the close/reopen assertion; the earlier 297-test checkpoint is historical.

Read the complete current `run_check.py` and `job_ops.py` diffs against the
RF-CODE tree. Unread, backfill, reply retry, explicit/deep history and attachment
writers receive the same OFF-derived semantic flag. Existing notification
flush precedes semantic drain, which receives the existing deadline. Request
and ops commands share receipt-before-unlink processing. These are downstream
feature connections, not changes to the historical REF boundary.

This closes the current source-fingerprint uncertainty for these connections.
G0 remains incomplete: the full RT differential and resource comparison are
not established by these checks. G1's scoped PASS does not imply RF-OPS, G6,
or G7, and no live service was modified or contacted.


## G0 disposition correction

Specification §27 defines G0 as the RF-CODE baseline/current-diff and
connection recheck, not a rerun of all RF-CODE RT requirements. The preceding
incomplete G0 wording incorrectly bundled those distinct gates. G0 is PASS
for the current Phase J baseline and connection scope: historical tree
46cffb0, descendant base 842125da, D01–D15 mapping, unchanged-module byte
comparison, coordinator/job diff inspection, and all current adapter hashes
matching g1-validation.json. The downstream fixes/features remain classified
separately from the original REF/FIX commits.

The complete changed-file overlay (including Hermes connection changes) is
frozen in `/Users/yusuke/.codex/artifacts/mcs-candidate-20260921-g0/`.
Its per-file hashes, base commits and archive hashes identify the precise
candidate. This does not promote RF-OPS/G2–G7 or expand the historical
comparison coverage described above. Documentation-only changes do not
require repeating successful product tests.
