# P0 棚卸し — 更新経路・runtime/writer・lock DAG・baseline/移行物

作業ID: P0 / 仕様: `~/dev/csdc-autoupdate-spec/hermes_csdc_autoupdate_devin_v3_1/04_IMPLEMENTATION_PLAN.md` §2
実施: 2026-11-11 / Devin / repo: hermes-mcs `feat/mcs-autoupdate-v3-client`（worktree `/Users/yusuke/.mcs-wt/auto-update-v2-20261110`）
改訂: r47レビュー指摘を反映 — caller検索の非切詰め保存・A→M→O実証拠・C-FD借用・Store read/write caller対応・D-4記述修正
調査方法: `git grep`/`git ls-files`の全caller列挙を `docs/dev-records/p0-auto-update-inventory/` のraw証拠fileへ非切詰め保存＋主要関数の実読。実機profile（`~/.mcs`）の稼働状態は本worktreeから未確認 — 「実機値」はdeployment雛形とコード既定からの記録であり、実機固有の差分は「未確認」と明記する。

## 証拠file一覧（非機密・git管理下）

| file | 内容 |
|---|---|
| `p0a-callers.txt` | P0-a指定入口（`apply` wrapper/`_apply_pipeline`/`cmd_check`/`--post-merge`/`rollback`/`recover`/`_run_native_release`/`install.sh`/`mcs_upgrade`/`reinstall_done`/`settings_migration_receipt`）の全caller grep結果（tests/integration/changes/docs除外、非切詰め。`apply`はwrapper呼出3箇所＋facade re-export callerを収録） |
| `p0b-callers.txt` | writer_lock全API（`acquire_run_lock`/`unlocked_transport`/`wait_run_lock`/`writer_blocked`/`updater_state`/`RunLockLost`）+ `sqlite3.connect` + store read/write呼出しの全caller grep結果 |
| `p0c-lock-callers.txt` | Store lock/method caller（`release_*_lock`/`store.lock()`/`guard_credential`/`diagnostic_state_lock`等）+ `control.read/write`呼出し + `release_state._read/_write` caller |
| `p0d-store-access.txt` | `store.read('…')`/`store.write('…')`リテラル引数呼出し + `_file_lock('…')` + `Store(`生成箇所 |
| `p0d-datafiles.txt` | notify spool file名・recovery `.prev`・data/配下file名のgrep結果 |

---

## P0-a 更新経路の全caller

### A-0. facade構造

`mcs/ops/mcs_update.py` はfacade。`update_cli.py`へre-exportし、`main()`のdispatch: 既定（subcommand省略）→`cmd_check`、`--post-merge`→`cmd_post_merge`（help抑止の隠し引数）。subcommand: `check`/`apply`/`plan`/`status`/`rollback`/`recover`/`reinstall-done`（`update_cli.py` L260–310）。`MCS_JOB_PID`を環境変数へ設定してからdispatch（services子がlaunchd jobをこのPID終了後にreloadする契約、L303）。

### A-1. 入口別caller対応表

| 入口（仕様指定） | 実装本体 | caller（grep全件、`p0a-callers.txt`参照） | profile | 親process | 入口時点の保持lock | 入口内の取得 | 解放 | FD継承/借用 | 外部callback/待機 |
|---|---|---|---|---|---|---|---|---|---|
| `check`（既定） | `update_cli.cmd_check` L89 → auto/picked判定 → **`update_apply.apply`直呼出** L192（picked: `apply(picked["tag"],picked["target_sha"],picked["command_id"],picked.get("base_sha"))`）/ L195（auto: `apply(auto[0],auto[1],None)`）→ `apply` L116 → `_call_legacy` → `_apply_pipeline` L129 | `deployment/scripts/mcs_update.sh`（exec `mcs_update.py check`）、`update_cli`既定（L269）/`check` sub（L291）。**同名別関数注意**: `mcs_setup.py cmd_check`（setup診断 L836、`cmd_check(args)`）と`notify_cmds._human_cmd_check`は別物。picked.rollback時はL190で`rollback()`へ分岐 | hermes cron | cron/launchd | なし（pipeline進入前はGit fetch・`_drug_info_notices`通知待機のみ） | pipeline内でU→R | pipeline終了時 | post-merge子へU/R FD | Git fetch・通知・pre-check待機 |
| `apply` | `update_cli.cmd_apply` L211 → `apply()` L212 → `apply` L116 → `_apply_pipeline` | `apply` subcommand dispatch L271/284（`mcs_update.py apply` — 手動実行 + `scripts/mcs_upgrade.py` subprocess）。**`cmd_check`は`cmd_apply`を経由せず`apply()`を直呼出**（L192/L195 — check行参照）。facade `mcs_update.apply`（L309 re-export）のcallerはtest専用: `test_mcs_update_restore.py` L681/778、`test_mcs_update_apply.py` L323、`test_mcs_update.py` L745（`update_cli.apply`をmonkeypatch）。`release_apply_owner._run_native_release` L1047は`apply()`を通らず`mcs_update._apply_pipeline`を`_release_cap`付きで直呼出 | manual/hermes | ユーザー端末 or cron | mcs_upgrade経由時はG(SH)を`_release_scope`で保持 | U→R（`_apply_pipeline`内） | 同上 | 同上 | `--confirm-human`/`--reason`は未公開候補のみ |
| `plan` | `cmd_plan` L218 | `mcs_upgrade.py` subprocess（`--no-fetch`併用） | manual | ユーザー端末 | G(SH)（`_release_scope`内） | なし（読取り） | G(SH)解放 | G FDを`_lifetime_fds`でgit子へpass_fds | Git fetch抑止時はlocalのみ |
| `--post-merge` | `cmd_post_merge` L237 → `_adopt_post_merge` L248 or `_legacy_post_merge` | `update_execution._run_post_merge` L227 が**NEW code**でsubprocess spawn（唯一のproduction caller） | 更新子process | updater | **借用**: `CHILD_FDS_ENV`経由で親FDを`_adopt_post_merge`が検証・採用。標準経路はU+Rの2FD、release経路はG/U/Rの3FD | なし（親が保持したまま） | 子終了で親がreap | **本質的にFD借用**: `MCS_UPDATE_RUN_LOCK_FD`/`MCS_UPDATE_LOCK_FD` env + `pass_fds` + `owned_process._inherited_fds` | `start_new_session=True`、timeout=T_POST_MERGE(+T_INSTALL if reinstall)、`_stop_update_child`でkillpg |
| `rollback` | `update_cli` L296 → `update_apply.rollback` L616 | `mcs_cli.py update rollback`→`os.execv mcs_update.py rollback`、`cmd_check`のpicked.rollback経路 | manual/cron | ユーザー端末 or cron | なし | U→R（L630/655） | 同上 | なし | restore時は人承認receipt経路 |
| `recover` | `recover_interrupted` `update_recovery.py` L233 | `mcs_update.py recover` 手動（`_resume_provider_migration=True`）。**別系統**: `mcs_cli.py recover`→`~/.mcs-recovery/mcs_recover.py` exec、`org.mcs.recovery` launchd 900s→`deployment/recovery/mcs_recover.py` | manual/独立 | ユーザー端末 / launchd | なし | U(L100/246)→R(`_acquire_run_lock_wait(tries=2)` L254) | journal収束後 | なし | `_report`でrecovery_report.jsonへ記録、通知送信 |
| `reinstall-done` | `update_recovery.reinstall_done` L95 | `mcs_update.py reinstall-done` 手動のみ | manual | ユーザー端末 | なし | なし | — | なし | journal `reinstall_done` flag更新+通知 |
| `_apply_pipeline` | `update_apply.py` L129 | `update_apply.apply` L125（`_call_legacy`）、`release_apply_owner._run_native_release` L1047（`_release_cap`付きin-process、借用locks） | 両方 | updater or release owner | `_release_cap`時はG/U/R借用、通常時はなし | 通常: U(L380)→R(L468 `_acquire_run_lock_wait` 40tries×30s=20min)。借用時は`_claim_effect_entry`で既保持FDを検証 | pipeline終了 | release借用時は`_EFFECT_*` in-memory capability+`owned_process.lifetime_fds` | settings_migration_receipt.pending gate L145、`_run_post_merge` L581 |
| `_run_native_release` | `release_apply_owner.py` L826 | **production caller無し** — `git grep`全件で定義+test呼出し（`test_release_apply_owner_native_apply.py:461,507`）のみ。v1署名release apply owner機構全体（`_issue_planning_consent` L142・`_prepare_native_plan` L469・本関数）がtest専用でdormant | — | — | — | G(L888 `release_entry_lock()`)→U(L890 `acquire_update_lock`)→R(L895 `acquire_run_lock` bounded loop)→preflight→A(L913/923 `release_authority_lock`)→O(L929 `release_owner_lock`) | lifetime ExitStack | G/U/R FDを`_verify_inherited_lock`検証後`lifetime_fds`で子寿命へ束縛、`_StartedEffect.locks/lock_identities`記録 | `_run_effect_post_merge`へ3FD借用 |
| `install.sh` | repo root shell | `mcs_cli.py install`→`os.execv`、手動実行。updater自体は呼ばない | install | ユーザー端末 | `data/update.lock`を`_lock_file`で確認(L210/276) — 更新中は拒否/待機 | `install_options.py bootstrap-runtime-apply`（settings移行） | — | `MCS_UPDATE_REINSTALL`尊重、runtime.sh L664でrun.lock継承を要求する経路あり | venv構築・launchd job render |
| `mcs_upgrade` | `scripts/mcs_upgrade.py` | `mcs_cli.py update`（guide/plan/apply）→subprocess。`--installed-standalone`対応 | manual | ユーザー端末 | **`_release_scope`でG(SH)を保持**（`_open_release_gate` L61–98: `release-entry.lock`へ`LOCK_SH|LOCK_NB`、`release-owner.json`/`release-owner-activation.json`存在時は`release_entry_hold`で拒否） | なし（子へ委譲） | `_release_scope`終了時 | `_lifetime_fds()`でG FDを全subprocessへ`pass_fds` | 対話guide・`subprocess.run` timeout=600 |
| `reinstall_done` | `update_recovery.py` L95 | `mcs_update.py reinstall-done` のみ | manual | ユーザー端末 | なし | なし | — | なし | `update_state.json`のapplying.reinstall_done更新 |
| `settings_migration_receipt` | `mcs/ops/settings_migration_receipt.py` | `install_options.py`（L153,176,542,558,591,600,660,670,796 — split移行可否を広くgate）、`update_apply.py` L145,486、`update_lifecycle_context.py` L238,252、`update_planning.py` L143,195、`update_runtime_migration.py`経路、`settings_migration_reconcile.py` | install/update両方 | 各親process | `_legacy_scope`（G SH gate）経由のcallerあり | receipt `pending()`判定のみ（lock取得なし） | — | — | pending中はapply/updateを拒否（fail-closed） |

### A-2. 標準apply pipelineのlock規律（`update_apply._apply_pipeline` docstring+実測）

- `update.lock`(U)を**最初のjournal write前**に取得しpipeline全体で保持 → 「journal存在+lock空き=writer死亡」が成立
- `run.lock`(R)は長いpre-check後に取得（`_acquire_run_lock_wait`: 40tries×30s interval=最大20分、`RUN_LOCK_TRIES`/`RUN_LOCK_INTERVAL`）
- lock順序は **U→R**（Gは標準経路に無し — Gはrelease owner経路のみ）
- `_release_cap`借用時: `release_apply_owner._claim_effect_entry`が`_EFFECT_CAPS`/`_EFFECT_PARENTS`/`_EFFECT_ARTIFACTS`のin-memory registryから`_StartedEffect`を検証・借用。`type(_release_entry) is _StartedEffect`・`sha==reference.target_commit`・`len(locks)==3`・`run_lock_fd==locks[2]`を`_run_post_merge`冒頭で再検証（`update_execution.py` L208–212）
- release経路のpost-merge spawnは`release_apply_owner._run_effect_post_merge`（L1333）が担当し、内部で`mcs_update.py --post-merge`をsubprocess spawn（L1396）。標準経路の`update_execution._run_post_merge`（L200–）と対になる

### A-3. `mcs_upgrade.py` のG(SH)参加（P0-c関連の重要事実）

- `_open_release_gate`（L61–98）: `~/.mcs-release-control/<ns>/release-entry.lock`を`O_RDWR|O_CREAT|O_NOFOLLOW|O_NONBLOCK`でopen→fstat検証（regular/owner/nlink=1/size=0/mode0600）→`LOCK_SH|LOCK_NB`→**`release-owner.json`/`release-owner-activation.json`が存在すれば`release_entry_hold`で拒否**（v1 release owner状態の存在中はupgrade guideを起動しない）
- `_release_scope` contextmanagerが`_RELEASE_GATE_FD` globalへ保持、`_lifetime_fds()`が全`subprocess.run`/`Popen`へ`pass_fds`で継承
- 同一ロジックが`release_apply_checks._open_release_gate`/`_legacy_scope`（stdlib-only複製、「Copied into the independent 3.9/3.10 closures」comment）にも存在し、`settings_migration_reconcile.py`等が使用

### A-4. 独立recovery系（updaterではないが更新を左右）

| 入口 | file | profile/親 | lock | 備考 |
|---|---|---|---|---|
| `org.mcs.recovery` | `deployment/launchagents/org.mcs.recovery.plist`→`deployment/recovery/mcs_recover.py`（`__RECOVERY_PYTHON__`、repo外`~/.mcs-recovery/`コピー） | hermes/launchd 900s | 独自系統（`mcs_recovery_core.py`/`mcs_recovery_database.py`）。repoのU/R/G/A/Oとは別 | 中断更新検出・recover/reinstall-done相当を独立環境で実行。repo `mcs_update.py`を信用しない設計 |
| `mcs_cli.py recover` | `~/.mcs-recovery/mcs_recover.py`を`os.execv`（`--interactive`/`--status`/`--if-stale`） | manual | 同上 | plist ProgramArguments照合・python executable検証を経てexec |
| `install.sh` | repo root | manual/install子 | `data/update.lock`確認（L210,276） | updaterをspawnしない。`install_options.py bootstrap-runtime-apply`はsettings移行 |
| `deployment/install/runtime.sh` | installer内部 | install子 | update.lock/run.lock言及（L664,751） | runtime migration時にupdaterのinherited run.lockを要求する経路あり |

### A-5. 「送信・自己再起動」集約

- 外部送信: Git fetch（check/plan/applyのtag確認）、通知spool経由の人向け通知（`notify_spool`/`notify_flush`）、launchd bootout/bootstrap（services再起動）
- 自己再起動: post-merge子がNEW codeで`--post-merge`実行→services再起動→親reap。標準経路はU+Rの2FD継承、release経路はG/U/Rの3FD継承
- dormant経路（`_run_native_release`系）: 同じ`_apply_pipeline`+`_release_cap`借用構造、post-mergeは`release_apply_owner._run_effect_post_merge`

---

## P0-b runtime/writer の入口

### B-1. `mcs/core/writer_lock.py` API（全実装確認済み）

| API | 行 | 性質 |
|---|---|---|
| `RUN_LOCK` | L11 | `data/run.lock` path定数 |
| `UPDATE_MARKER_NAME` | L12 | `update_in_progress.marker` |
| `RUN_LOCK_REACQUIRE_S`/`RUN_LOCK_POLL_S` | L14–15 | 150s / 0.2s |
| `acquire_run_lock(path)` | L56 | `open_lock_file`→`flock(LOCK_EX\|LOCK_NB)`。fd返却or None。**docstring: 全ledger writerが保持すべきlock**。例外: resident extract_llm drainerは結果write毎に取得、fenced bookkeepingはlockless（`extract_llm._write_lock`） |
| `code_stamp()`/`code_stamp_checked()` | L81/93 | `mcs/**/*.py`最新mtime_ns=コード世代 |
| `RunLockLost` | L108 | BaseException。`held`/`updated`属性。updater window検出用 |
| `writer_blocked()` | L127 | update marker存在 or `notify_spool.restore_writer_hold`でTrue |
| `updater_state()` | L147 | `(writer_blocked(), code_stamp())` |
| `unlocked_transport(db, lock_fd, wait_s, deadline)` | L152 | transport中のみ`LOCK_UN`→DB作業前にbounded再取得（既定150s、deadline上限）。`db.in_transaction`中はRuntimeError。marker出現or stamp変化で`RunLockLost(held=True)`、期限切れで`RunLockLost(held=False, updated=…)` |
| `wait_run_lock(path, wait_s, expected_code_stamp, marker_path)` | L190 | poll取得+世代fence: 取得直後にmarker有or stamp不一致ならfd返却しNone（旧code混在防止） |

### B-2. writer_lock caller分類（`p0b-callers.txt`全件、file単位集約）

| 分類 | file |
|---|---|
| extract writer/fence | `mcs/extract/rules/extract.py`, `mcs/extract/llm/extract_llm.py`, `extract_llm_carry.py`, `extract_llm_errors.py`, `extract_llm_resident.py`, `extract_llm_runner.py`（`unlocked_transport` L1019）, `extract_llm_source_contract.py`, `mcs/extract/attachment_inference.py`, `mcs/extract/attachment_ocr.py` |
| semantic writer | `mcs/semantic/semantic.py`, `semantic_candidate.py`, `semantic_drain.py`（`unlocked_transport` L389,406,493）, `semantic_due_selection.py`, `semantic_intake_hold.py`, `semantic_jev.py`, `semantic_transport.py` |
| patients/rollup writer | `mcs/patients/rollup.py`, `patient_context_quality_controller.py`, `patient_context_quality_receipts.py`, `patient_context_refresh.py`, `patient_context_refresh_state.py` |
| ingest writer/admission | `mcs/ingest/run_check.py`（`unlocked_transport` L974、`RunLockLost`処理）, `health_recovery.py`, `health_report.py`, `init_data.py`, `project_metadata.py`, `cross_lists.py` |
| llm transport/admission | `mcs/llm/cloud_backlog.py`（11箇所・owner_alive/process生存確認）, `mcs/llm/cloud_inference.py` |
| 手動CLI writer | `mcs/ops/mcs_ocr.py`（13箇所・最多）, `mcs/ops/cloud_cli.py`, `mcs/ops/commands/job_cli.py`, `mcs/ops/notification_cancel.py`（`apply()` L273）, `mcs/ops/ocr_failure_ack.py`（`apply()` L69） |
| standalone scheduler | `mcs_standalone/runtime.py` |
| updater自身 | `mcs/ops/update_lifecycle_context.py`（`UPDATE_LOCK`/`RUN_LOCK` path・`acquire_update_lock` L312・`_acquire_run_lock_wait` L317・`_POST_MERGE_UPDATE_FD` ContextVar L164）, `mcs/ops/update_apply.py`, `mcs/ops/update_recovery.py`, `mcs/ops/mcs_update.py`, `mcs/ops/release_apply_owner.py` |
| CI/meta | `ci/gates.py`（検査用、製品経路ではない） |

### B-3. SQLite境界

- 正本DB: `mcs/core/ledger.py` — `sqlite3.connect(path, timeout=30)` L113、URI接続 L1214,1235。`PRAGMA foreign_keys=ON`/`busy_timeout=30000`。`with self.db` transaction（L17 docstring）
- `sqlite3.connect` callerは`p0b-callers.txt`に全件保存（30+ file: extract/llm store系・adapters/common・recovery DB tool等）
- **境界規則**: run.lock保持下での書込み、または`unlocked_transport`で通信中のみ解放→再取得後に書込み。v3は「管理対象起動・新規admission・既存結果確定」の3境界へ接続。全SQL改修は対象外
- recovery系: `deployment/recovery/mcs_recovery_database.py`が独立にDBを扱う（updater lockとは別系統）

### B-4. 通知sender

- `mcs/notify/notify_spool.py`: spool dir=`notify_dirs(root)`の9 dir（`discord_render`/`discord_state`/`slack_render`/`slack_state`/`lineworks_render`/`lineworks_state`/`flags`/`cmd_int`/`cmd_results`、L34–38）。file: `flags/notify.json`（plugin DB-free view）、`notify_gc_cursor.json`、`<delivery_id>.json`、`*.json.claimed`、restore系（`restore_pending.json`/`restore_reconcile.json`/`restore.json`/`backup_restore_approval.json`/`backup_restore_resume.json`/`backup_restore_blocked.json`）
- `notify_spool.py` L111が`update_state.json`を読み、L120で`marker.get("by") in ("mcs_update","mcs_recover")`を識別 — **更新中の通知抑止/識別は既存機構あり**
- `notify_flush.py`: `_MAX_FILES=10`/`_MAX_FILES_BYTES=23MiB`、`update_apply` control op L372、更新関連固定文言 L278/349
- 送信本体はadapter側`MCSAdapter` — v3の「送信」境界はここへ接続

### B-5. 課金reservation / OCR / backup

- 課金: `mcs/llm/admission/llm_admission.py` — 共有local LLM slotのRT/BACKLOG admission。permitはpre-send reservation〜confirmed backendまで計数。`cloud_inference`/`cloud_backlog`が`unlocked_transport`で通信中lock解放。**v3 drain/退役との境界はこことwriter_lockの交差**
- OCR: `mcs/ops/mcs_ocr.py`（手動CLI、writer_lock最多）＋`mcs/extract/attachment_ocr.py`/`attachment_inference.py`（定期job `mcs_ocr.sh`経由）
- backup/offsite: `deployment/scripts/mcs_offsite.sh`→`mcs/ops/mcs_backup.py offsite`。**`data/update_in_progress.marker`存在時はexit 0でskip**（L30–32）。`__BACKUP_ENABLED__`/`__BACKUP_POLICY__`のrendered opt-in必須（既定OFF）

### B-6. 手動CLI入口（`mcs/ops/mcs_cli.py` dispatch、全command）

| command | 実体 | 備考 |
|---|---|---|
| `install` | `os.execv /bin/sh install.sh` | 導入 |
| `update` | guide/plan/apply→`scripts/mcs_upgrade.py` subprocess、rollback→`os.execv mcs_update.py rollback` | `MCS_LIFECYCLE_PINNED`未設定時はruntime選択pythonへ`os.execv`で再exec |
| `setup`/`doctor` | `mcs_setup.py`（init/services/check/features/api-keys/slack-channels） | |
| `recover` | `~/.mcs-recovery/mcs_recover.py` exec | repo外独立tool |
| `connect`/`connection`/`disconnect`/`rotate-key` | `sdc_cli.py` | SDC credential操作 |
| `inventory` | `sdc_inventory_cli.py` | |
| `support` | `sdc_support_cli.py` | |
| `diagnostics` | `sdc_diagnostic_cli.py` | |
| `jobs` | `job_cli.py` | 失敗job調査/再試行 |
| `drug` | `mcs_drug.py` | 医薬品master確認 |

`scripts/mcs`はshell bootstrapで`mcs/ops/mcs_cli.py`へdispatch（dirではない）。

### B-7. profile別 runtime/writer 一覧

| profile | 常駐/定期process | writer経路 |
|---|---|---|
| **hermes** | `ai.mcs.extract-drainer`×**3**（day/night/-2 — 固定2workerではない）、`ai.mcs.llamaserver`（共有LLM daemon、更新対象外区分）、`local.mcs-cmd`（cmd取込）、`local.mcs-health`（300s health_watch）、`local.mcs-int`（5s interval job）、`local.mcs-sdc`（300s `sdc_tick.py`→`sdc_cli.tick`→`Store.lock()`=tick.lock）、`org.mcs.recovery`（900s独立復旧）、cron `mcs_update.sh`（日次check）・`mcs_check.sh`・`mcs_ocr.sh`・`mcs_offsite.sh`等 | drainers=結果write毎run.lock、cmd/semantic/ingest各job=run.lock保持writer、sdc tick=tick.lock系列、updater=U/R(+G/A/O release経路)系列 |
| **standalone** | `ai.mcs.standalone`→`mcs_standalone/runtime.py` 単一host・7定期job・cmd取込・抽出worker3本 | runtime.pyがschedulerとして各job起動、個々のjobは同じwriter_lock経路 |
| **lineworks** | `lineworks_adapter/` 独立CLI（interactive="lineworks"） | adapter→ledger writerのrun.lock遵守は**未確認**（M3/W06で確認） |
| 監視・復旧自身 | `health_watch.py`（local.mcs-health）、`org.mcs.recovery` | 更新・復旧を「止めない」側。v3のQ/drain対象に含めるかはprofile設計で要判断（spec: 監視・復旧自身を区別） |

---

## P0-c lock の実DAG

### C-1. lock正本 — **2つのstore rootに跨る**（重要な構造事実）

| root | 実path | 用途 |
|---|---|---|
| monitor/SDC store | `~/.mcs-security`（`Store(root)`既定） | state.json・credential・tick.lock・support/restart/diagnostic各file・inventory系 |
| release control store | `~/.mcs-release-control/<sha256(canonical runtime_home)>`（`release_apply_checks._control_root` L439–445） | release-owner.json・release-*.lock・release-*.json全て |

`Store` classは同一（`adapters/sdc/storage.py`）で`NAMES`/`LIMITS`を共有するが、**実fileはroot別**。lock DAGは2 rootを跨ぐ。

| 記号 | file（root） | 実装 | 役割（02 §4） |
|---|---|---|---|
| G | `release-entry.lock`（control） | `Store.release_entry_lock(shared=)` storage.py L673–684。`shared=True`で`LOCK_SH`、Falseで`LOCK_EX`。`_release_entry_fd`/`_release_entry_shared`をStore instanceへ記録（L679–683） | owner/recovery間の変更排他 |
| U | `data/update.lock`（data/） | `update_lifecycle_context.UPDATE_LOCK` L81、`acquire_update_lock()`=writer_lock.acquire_run_lock流用 L312 | 既存journal/更新排他 |
| Q | `runtime-entry.lock` | **未存在**（v3新設予定） | code切替/全業務process寿命 |
| R | `data/run.lock`（data/） | `writer_lock.RUN_LOCK`/`acquire_run_lock` | 既存writer排他 |
| C | `config.json.edit.lock`（config root） | **`mcs/ops/setup/config_edit.py` `_edit_lock`**（L131〜）。`mcs/ops/config_edit.py`は不存在。**同一fileへの並行取得経路**: `mcs/core/provider_settings.py` `_edit_lock_at`（L685、root handleのdir_fd経由open）+ `_verify_lock`（L671、`LOCK_EX\|LOCK_NB` flock+identity検証） | private設定snapshot最終整合 |
| A | `release-authority.lock`（control） | `Store.release_authority_lock()` storage.py L655–665。`_release_authority_fd`/`_release_authority_thread`を記録 | 同意/取消/bootstrap/currentness |
| M | `tick.lock`（monitor） | `Store._file_lock('tick.lock')` L602–607（`Store.lock()`）。`_tick_fd`/`_tick_thread`記録、reentry時`SDC_CREDENTIAL_LOCK_REENTRY` | 端末identity/credential snapshot、monitor tick寿命 |
| O | `release-owner.lock`（control） | `Store.release_owner_lock()` storage.py L667–671 | 短いowner state CAS |
| 補助 | `support-state.lock`/`support-session.lock`/`restart-state.lock`/`diagnostic-state.lock`（monitor） | 各`*_state.py`/`storage.py` L613,639,645,651 | family別 |

### C-2. `_file_lock` の共通性質（storage.py L688–701）

- `fcntl.flock((LOCK_SH if shared else LOCK_EX) | LOCK_NB)` — **全てnon-blocking**。保持中は`BlockingIOError`で即失敗（待機しない）
- `tick.lock`のみ特別扱い: `credential-store.json`（mutation_contract.NAME）存在時は`O_RDWR`（file再生成しない）、不在時は`O_RDWR|O_CREAT`
- fdはcontext exitで`os.close` — flockはfd寿命

### C-3. 実測したlock取得順序（関数・区間つき）

**release_apply_owner系（control store + monitor store跨ぎ）:**

| 関数 | file:line | 取得順序（with nest実測） |
|---|---|---|
| `_issue_planning_consent` | release_apply_owner.py L147,153,162 | `control.release_authority_lock()`(A) **→** `monitor.lock()`(M=tick.lock) **→** 内側で`control.release_owner_lock()`(O)。**A→M→Oを同一processで実証** |
| `_prepare_native_plan` | L479,522 | `control.release_authority_lock()`(A) **→** `monitor.lock()`(M)。A→M |
| `_run_native_release` | L888–930 | `control.release_entry_lock()`(G,EX) **→** `acquire_update_lock()`(U) **→** `acquire_run_lock` loop(R) **→** preflight **→** `control.release_authority_lock()`(A)×2 **→** `control.release_owner_lock()`(O)。**G→U→R→A→O** |

**標準update pipeline（data/のみ、control store不使用）:**

| 関数 | file:line | 取得順序 |
|---|---|---|
| `_apply_pipeline` | update_apply.py L380,468 | `acquire_update_lock()`(U) **→** `_acquire_run_lock_wait()`(R、最大20分poll) |
| `rollback` | update_apply.py L630,655 | U **→** R（同順） |
| `recover_interrupted` | update_recovery.py L100/246,254 | U **→** R（`_acquire_run_lock_wait(tries=2)`） |

**SDC store内family lock（monitor root内、`p0c-lock-callers.txt`全件）:**

| 順序 | caller例 |
|---|---|
| support-state.lock → tick.lock | `support_state.py` docstring L167,194,500「Acquire support-state.lock before tick.lock」明記。`restart_client.py` L298,310はsupport-state.lock単独 |
| restart-state.lock → tick.lock | `restart_execution.py` L189 comment「Caller owns restart-state.lock then tick.lock」+ L221,275,298,357で`restart_state_lock(), store.lock()` nest。`restart_state.py` L208,234,268同様 |
| support-state.lock → restart-state.lock → tick.lock | `restart_planning.py` L217の3重nest（`support_state_lock(), restart_state_lock(), store.lock()`） |
| diagnostic-state.lock | `diagnostic_state.py` 11箇所で単独取得（tick.lockとnestしない設計 — L611 docstring「independent of credential and monitoring waits」） |
| tick.lock単独（長時間保持） | `sdc_cli.tick` L241 `with Store(...) as store, store.lock():` — tick全体（最大60s deadline）でM保持。`provision.py`は`store.lock()`を12箇所で短時間取得 |

**G lockの3つの参加形態:**

| 形態 | caller | mode |
|---|---|---|
| EX（排他） | `release_apply_owner._run_native_release` L888 `control.release_entry_lock()` | `LOCK_EX` — owner/recovery間の変更排他 |
| SH（共有）+拒否条件 | `mcs_upgrade.py` `_open_release_gate` L85 `LOCK_SH\|LOCK_NB` + `release-owner.json`/`activation`存在で`release_entry_hold` | `LOCK_SH` — upgrade guideはrelease owner状態の存在中は起動拒否 |
| SH（共有）+拒否条件 | `release_apply_checks._open_release_gate`/`_legacy_scope` L475–497（stdlib-only複製）→`settings_migration_reconcile.py` L119,143等 | 同上 |

### C-4. C lock（config.json.edit.lock）の取得経路とFD借用（r47指摘3対応）

- **入口1**: `config_edit._edit_lock`（`mcs/ops/setup/config_edit.py` L131〜）— caller: `adapters/slack/provision.py`, `mcs/medications/drug_alias.py`, `mcs/ops/cloud_cli.py`, `mcs/ops/install_options.py`
- **入口2**: `provider_settings._edit_lock_at`（`mcs/core/provider_settings.py` L685〜）— root handleのdir_fd経由で`config.json.edit.lock`をopen、`_verify_lock`（L671）が`LOCK_EX|LOCK_NB` flock+identity検証。split provider設定の`apply()`（L815 `with _edit_lock_at(root, expected) if lock_fd is None else nullcontext(lock_fd)`）が使用
- **FD借用関係**: `config_edit.apply` L225が`provider_settings.commit_locked(..., lock_fd=lock_fd)`へ**取得済みC FDを貸与**。`provider_settings`側は`lock_fd`非Noneなら`nullcontext(lock_fd)`で**再取得しない**（L815）。→ 2入口は独立lockではなく**同一lockの取得+借用**。P0-c/M3設計で「2系統の独立lock」と誤認しないこと
- **C lockとG/U/R/A/M/Oの直接取得関係は未検出** — config writerはrelease lockを取らない。動的import経路の最終網羅はM3継続

### C-5. 実DAGまとめ（確認済みedge）

```
release owner経路（control+monitor跨ぎ、全てLOCK_NB非待機）:
  G(EX) → U → R → [preflight] → A → M → O   （_run_native_release / _issue_planning_consent / _prepare_native_plan）

標準update経路（data/のみ）:
  U → R → [post-merge子がU/R FD借用]

SDC store内family（monitor root内）:
  support-state.lock → tick.lock(M)
  restart-state.lock → tick.lock(M)
  support-state.lock → restart-state.lock → tick.lock(M)  (restart_planning L217)
  diagnostic-state.lock（独立、tick.lockとnestしない）

G共有gate（upgrade/settings reconcile）:
  G(SH) — release-owner file存在で拒否、子へFD継承
```

仕様目標`G→U→Q→R→C→A→M→O`に対し、現行確認済みは`G→U→R→A→M→O`（release経路）と`U→R`（標準経路）とfamily内`state.lock→M`。**Q未実装、CのDAG位置は未確定**（C lockはrelease lockと無関係に動作 — snapshot境界分離が必要かはM3設計事項）。

### C-6. 未確認・要M3継続の点

- Q(`runtime-entry.lock`)未実装 — M3で新設。全業務入口のbootstrap loaderが対象
- `unlocked_transport`中のR解放とQ導入後の「code切替Q EX/process寿命Q SH」の整合 — M3設計事項
- `_apply_pipeline`内部で更にlockを取るnested呼出しの再帰的DAG — M3で精査（「nested callを丸ごとR外へ移すだけの変更は禁止」）
- lineworks_adapter→ledger writerのrun.lock遵守 — adapter境界詳細は本棚卸し範囲外
- C lock↔release lockの動的交差 — 静的grep未検出、runtime caller網羅はM3継続
- 実機`~/.mcs`のprofile差分（launchd loaded状態・cron登録・独自script）— deployment雛形ベース、実機未確認

---

## P0-d baseline/移行物のfilename表

### D-1. `adapters/sdc/storage.py` `NAMES`/`LIMITS`（L20–38、実測全35 name）

| filename | LIMIT | 分類 | 主なaccessor（`p0d-store-access.txt`/`p0c-lock-callers.txt`） |
|---|---|---|---|
| `endpoint.json` | 4096 | SDC endpoint設定 | `store.read` 12箇所（connector/transport/provision等） |
| `credential` | 4096 | 旧credential実体 | `store.read` 19箇所・`store.write` 3箇所 |
| `credential-store.json` | mutation_contract.MAX_MARKER | credential mutation marker | `store.read` 3箇所 |
| `state.json` | 8MiB | monitor state | `store.read` 23箇所・`store.write` 20箇所（最多） |
| `tick.lock` | 0 | M lock | `Store.lock()`/`_file_lock` |
| `inventory-policy.json` | 1MiB | inventory許可 | read 4・write 2 |
| `inventory-spool.json` | 2MiB | inventory spool | read 1・write 1 |
| `inventory-status.json` | 16KiB | inventory状態 | read 2・write 4 |
| `enrollment-consent.json` | 16KiB | enrollment同意 | read 4・write 1 |
| `support-state.json` | 64KiB | support状態 | support_state.py系 |
| `support-state.lock` | 0 | support lock | `support_state_lock()` |
| `support-session.lock` | 0 | support session lock | `Store` L645 |
| `support-bootstrap.json` | 16KiB | support bootstrap | read 1 |
| `restart-state.json` | RESTART_STATE_MAX_BYTES | restart状態 | restart_state.py系 |
| `restart-state.lock` | 0 | restart lock | `restart_state_lock()` |
| `restart-execution.json` | RESTART_EXECUTION_MAX_BYTES | restart実行 | read 1 |
| `restart-planning.json` | RESTART_PLANNING_MAX_BYTES | restart計画 | restart_planning.py系 |
| `provision-state.json` | 1MiB | provision状態 | provision.py系 |
| `diagnostic-state.json` | 1MiB | diagnostic状態 | diagnostic_state.py系 |
| `diagnostic-anchor.json` | 4096 | diagnostic anchor | diagnostic_state.py系 |
| `diagnostic-state.lock` | 0 | diagnostic lock | `diagnostic_state_lock()` 11箇所 |
| `release-owner.json` | 8MiB | **v1/v3共有**（後述D-4） | `release_state._read/_write`・`release_auto_state.load/save`・`control.read/write` |
| `release-owner.lock` | 0 | O lock | `release_owner_lock()` — release_state.py 7箇所・release_auto_state.py/migration各所 |
| `release-owner-activation.json` | 16KiB | v1 activation marker | `control.read(release_state.ACTIVATION)` L297等 |
| `release-entry.lock` | 0 | G lock | `release_entry_lock()`・`mcs_upgrade._open_release_gate`・`release_apply_checks._open_release_gate` |
| `release-bootstrap.json` | ~36KiB | release bootstrap証拠 | `control.read` L247,302 |
| `release-authority.lock` | 0 | A lock | `release_authority_lock()` — release_apply_owner L147,153,479,522,913,923 |
| `release-planning.json` | 16KiB | release計画 | `control.read/write` L167,176,177,482,525 |
| `release-plan.json` | 64KiB | release plan | `control.read/write` L444,539 |
| `release-change.json` | 128KiB | release変更 | `control.read/write` L624,649 |
| `release-successor.json` | ~2.06MiB | successor登録 | `control.write/read` L1122,1123 |
| `release-start-intent.json` | 32KiB | v3 StartIntent | `release_auto_state` INTENT_NAME（L20,399,400,405,440） |
| `release-migration-v3.json` | 64KiB | v3 MigrationIntent | `release_auto_migration` NAME（L15） |

**未列挙recordがあれば自動移行を拒否**（02 §253）— `NAMES`がallowlist正本。

### D-2. Store root別のfile所在

- **monitor root（`~/.mcs-security`）**: 上表の`endpoint.json`〜`diagnostic-state.lock`（release系を除く）
- **control root（`~/.mcs-release-control/<sha256(runtime_home)>`）**: `release-owner.json`/`release-owner.lock`/`release-owner-activation.json`/`release-entry.lock`/`release-bootstrap.json`/`release-authority.lock`/`release-planning.json`/`release-plan.json`/`release-change.json`/`release-successor.json`/`release-start-intent.json`/`release-migration-v3.json` — `Store` class共用だが**実fileはcontrol root配下**（`_control_store` L447–455が`Store(root.parent, create=True)`→`Store(root, create=True)`をyield）
- **data/ root（`~/.mcs/data`）**: `update.lock`(U)・`run.lock`(R)・`update_state.json`・`update_in_progress.marker`・`ledger.db`・`mcs.log`・notify spool dirs・`restore_report.json`・`recovery_report.json`・`restore_pending.json`・`.mcs-release-dependencies.json`（`release_dependency_lock.py`）・backup snapshot dir・recovery `.prev`（`prepare_recovery.py` L151 `os.replace(tmp, target+".prev")` — 旧version退避generation file）

### D-3. notify spool / recovery .prev の具体file（`p0d-datafiles.txt`）

- notify spool: `notify_dirs(root)`の9 dir（`discord_render`/`discord_state`/`slack_render`/`slack_state`/`lineworks_render`/`lineworks_state`/`flags`/`cmd_int`/`cmd_results`）。file: `flags/notify.json`、`notify_gc_cursor.json`、`<delivery_id>.json`、`*.json.claimed`、`restore_pending.json`/`restore_reconcile.json`/`restore.json`/`backup_restore_approval.json`/`backup_restore_resume.json`/`backup_restore_blocked.json`
- recovery `.prev`: `prepare_recovery.py` L151が`os.replace(str(previous_tmp), str(target) + ".prev")` — 対象script/fileの`.prev` suffix退避。`mcs_recover.py`/`update_recovery.py`/`update_restore.py`が`prev_sha`（git sha）と`.prev`（file世代）を区別して使用
- data/主要file: `ledger.db`（38箇所参照）、`update_state.json`（17）、`update_in_progress.marker`（10）、`restore_report.json`（4）、`recovery_report.json`（4）、`mcs.log`（3）、`restore_pending.json`（2）

### D-4. v1→v3共有fileの正確な記述（r47指摘4修正）

- `release-owner.json`: v1(`release_state.py`、schema `mcs.release-owner.v1`)とv3(`release_auto_state.py`、schema `mcs.release-owner.v3`)が**同名fileを共用**（共にcontrol root配下）
- `release_auto_state.load`（L63–72）の実際の拒否分類: **missing→`UNKNOWN_BASELINE`**（L65 `_check(raw is not None)`）、**v1 document等のparse/structural不一致→`INVALID`**（L68–69 `contract.parse_document`失敗時）、**schema_version不一致→`SCHEMA_MISMATCH`**（L70）。→ v1 fileは`UNKNOWN_BASELINE`ではなく`INVALID`/`SCHEMA_MISMATCH`で拒否（前版記述を修正）
- `release-owner-activation.json`: v1専用（`mcs.release-owner-activation.v1`）
- `release-start-intent.json`/`release-migration-v3.json`: v3新設（v1対応物なし）

### D-5. `deployment/` のrelease関連file

- 存在: `deployment/sdc-release-owner.json`（owner設定）、`deployment/sdc-release-lock.schema.json`（schema雛形）
- **欠損: `deployment/sdc-release-lock.json`** — 実profile値は未生成。「実profile準備待ち」と記録。生成手順は実装可能だが実環境の値を推測してcommitしない

---

## P0完了条件との対応（r47指摘を反映した再評価）

| 完了条件 | 状態 |
|---|---|
| 現行経路対応表 | ✅ A-0〜A-5（指定入口の全caller grep非切詰め保存＋入口別の保持/取得/解放/FD/callback/待機を記録） |
| 実lock DAG | ✅ C-1〜C-5（2 root跨ぎ構造・G 3形態・A→M→O実証・family lock順序・C-FD借用を関数/行つきで記録） |
| bridge対象 | 🟡 `_run_native_release`/`_issue_planning_consent`/`_prepare_native_plan`が**全てtest専用dormant**と判明 — v1 apply owner機構は実装済みだがproduction未配線。M3でbridge/loader/Q設計へ接続する際の重要前提 |
| backend integration slots | 🟡 MCS側は`sdc_tick`/`Connector.tick`/`Transport`が中央poll境界。CSDC側は別途 |
| 今回触らない機能 | ✅ health_watch/notify文言・mcs_offsite・recovery独立系・llamaserver等を区分記録 |

## 未確認として残す項目（推測で埋めない — M3接続に必要な静的境界を含む）

1. `_run_native_release`系のproduction trigger — v1署名release applyが実際に発火する経路（中央operation→端末dispatch）が本worktreeのgrep範囲では未検出。**v1 apply owner全public surfaceがtest専用dormant**のため、M3のbridge設計は「既存v1経路を再利用」ではなく「dormant機構の配線 or v3新規経路」の判断が必要
2. lineworks_adapter→ledger writerのrun.lock遵守 — adapter境界詳細は本棚卸し範囲外、M3/W06で確認
3. 実機（`~/.mcs`）のprofile差分 — deployment雛形ベース。実機のlaunchd loaded状態・cron登録・独自scriptは未確認
4. C lock↔release lockの動的交差 — 静的grep未検出。runtime caller網羅はM3継続
5. `_apply_pipeline`内部のnested lock呼出し再帰DAG — M3で精査
6. `unlocked_transport`中のR解放とQ導入後の整合 — M3設計事項
