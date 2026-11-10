# P0 棚卸し — 更新経路・runtime/writer・lock DAG・baseline/移行物

作業ID: P0 / 仕様: `~/dev/csdc-autoupdate-spec/hermes_csdc_autoupdate_devin_v3_1/04_IMPLEMENTATION_PLAN.md` §2
実施: 2026-11-11 / Devin / repo: hermes-mcs `feat/mcs-autoupdate-v3-client` @ `e35a56b2`（worktree `/Users/yusuke/.mcs-wt/auto-update-v2-20261110`）
調査方法: `git grep`/`git ls-files` の全caller列挙＋主要関数の実読。実機profile（`~/.mcs`）の稼働状態は本worktreeからは未確認 — 「実機値」はdeployment雛形とコード既定からの記録であり、実機固有の差分は「未確認」と明記する。

---

## P0-a 更新経路の全caller

### A-1. facade `mcs/ops/mcs_update.py` → `update_cli.py` のサブコマンド

`update_cli.py` の dispatch（`main()` L307–）: 既定=no subcommand→`cmd_check`、`--post-merge`→`cmd_post_merge`（非公開引数）。subcommand: `check`/`apply`/`plan`/`status`/`rollback`/`recover`/`reinstall-done`。

| 入口 | 実装 | caller（全列挙） | profile | 親process | 入口時点のlock | 実data変更 | 外部送信 | 自己再起動 | 借用FD |
|---|---|---|---|---|---|---|---|---|---|
| `check`（既定/`check`） | `update_cli.cmd_check`→`apply()`経路（auto/picked判定） | `deployment/scripts/mcs_update.sh`（hermes cron日次、exec-onlyで`mcs_update.py check`へ置換実行） | hermes | cron/launchd | なし（内部で取得） | update_state.json journal・適用時はmerge/post-merge | Git fetch（tag確認）・通知spool | post-merge子を新codeでspawn | post-mergeへG/U/R FD継承 |
| `apply` | `update_cli.cmd_apply`→`update_apply.apply()`→`_call_legacy(_apply_pipeline)` | `scripts/mcs_upgrade.py`（subprocess）, `mcs_cli.py update apply` 経由 | manual/hermes | ユーザー端末 or upgrade guide | なし（内部でG/U/R取得） | git merge・journal・config receipt | 通知・Git | post-merge子spawn・services再起動 | 同上 |
| `plan` | `update_cli.cmd_plan` | `scripts/mcs_upgrade.py`（subprocess, JSON出力） | manual | ユーザー端末 | なし（読取り中心） | なし（変更せず確認） | Git fetch（--no-fetchで抑止） | なし | なし |
| `status` | `update_cli.cmd_status` | 手動 `mcs_update.py status`（health_watch/notifyの案内文が指す先。自動callerなし） | manual | ユーザー端末 | なし | なし | なし | なし | なし |
| `rollback` | `update_cli` rollback→`update_apply.rollback()` | `mcs_cli.py update rollback`→`os.execv mcs_update.py rollback`、`cmd_check`のpicked.rolled_back経路 | manual/cron | ユーザー端末 or cron | なし（内部で取得） | journal・restore（要承認時） | 通知 | services再起動 | なし |
| `recover` | `update_cli` recover→`recover_interrupted(_resume_provider_migration=True)` | `mcs_update.py recover` 手動。**注意**: `mcs_cli.py recover` は別物 — `~/.mcs-recovery/mcs_recover.py` を exec（下記A-4） | manual | ユーザー端末 | なし（内部で取得） | journal収束・post-merge完了 | 通知 | なし | なし |
| `reinstall-done` | `update_recovery.reinstall_done()` | `mcs_update.py reinstall-done` 手動のみ。install.sh中断後の完了記録 | manual | ユーザー端末 | なし | journal `reinstall_done` flag | 通知 | なし | なし |
| `--post-merge`（隠し） | `update_cli.cmd_post_merge`→`_adopt_post_merge` or `_legacy_post_merge` | `update_execution._run_post_merge` が **NEW code** で `mcs_update.py --post-merge` を subprocess spawn（唯一のcaller） | 子process | 更新中のupdater | **借用**: `CHILD_FDS_ENV`経由で親のG/U/R FDを`_adopt_post_merge`が検証・採用 | post-merge effects（services/migration/再起動） | 通知・サービス操作 | 自身は完了で終了（親がreap） | **G/U/R FD継承が本質** |

### A-2. `apply`/`_apply_pipeline` の直接caller

| caller | file:function | lock関係 |
|---|---|---|
| `update_apply.apply()` | `mcs/ops/update_apply.py:125` `_call_legacy(_apply_pipeline, …)` | `_release_cap=None`時は通常経路でG/U/Rを自前取得。`_release_cap`指定時は`release_apply_owner._claim_effect_entry`で**借用**（下記） |
| `release_apply_owner._run_native_release` | `mcs/ops/release_apply_owner.py:826` → L1047 `mcs_update._apply_pipeline(None, core['commit'], …, _release_cap=capability)` | **親がG/U/Rを保持したままin-process呼出し**。`_EFFECT_CAPS`/`_EFFECT_PARENTS`/`_EFFECT_ARTIFACTS`のin-memory capabilityでFD/参照を受渡し |

**重要な未接続事実**: `_run_native_release` のproduction callerは**存在しない**。`git grep`全件で定義(L826)とtest呼出し（`tests/ops/test_release_apply_owner_native_apply.py:461,507`）のみ。v1署名release apply owner機構（G/U/R取得→preflight→A→O→`_apply_pipeline`借用→post-merge子）は実装済みだが**dormant**。v3のbridge/owner配線時に「既存v1経路が実際に発火する条件」を再確認する必要がある（現行の実更新はtag方式`mcs_update.py`経路のみ）。

### A-3. `cmd_check` のcaller

- `deployment/scripts/mcs_update.sh`: hermes cron日次。`exec __PYTHON__ -I __RUN_LOG_HELPER__ exec --data-dir __DATA__ -- __PYTHON__ __REPO__/mcs/ops/mcs_update.py check`。exec-only設計（script自己書換え競合回避、F17）。復旧はorg.mcs.recoveryの責務でfallbackなし。
- `update_cli` 既定動作（subcommand省略時）と`check` subcommand。
- `mcs_upgrade.py`は`check`相当を`--no-fetch`等で呼ぶが、直接`mcs_update.py check`は呼ばない（plan/apply/rollbackをsubprocess）。

### A-4. 独立復旧系（updaterではないが更新を左右する）

| 入口 | file | profile/親 | lock | 備考 |
|---|---|---|---|---|
| `org.mcs.recovery` launchd | `deployment/launchagents/org.mcs.recovery.plist`→`deployment/recovery/mcs_recover.py`（`__RECOVERY_PYTHON__`、repo外 `~/.mcs-recovery/` コピー） | hermes / launchd 900s | 独自（repo lockと別系統。`mcs_recovery_core.py`/`mcs_recovery_database.py`） | 中断更新の検出・`recover`/`reinstall-done`相当を独立環境で実行。repoの`mcs_update.py`を信用しない設計 |
| `mcs_cli.py recover` | `~/.mcs-recovery/mcs_recover.py`を`os.execv` | manual | 同上 | 対話`--interactive`または`--status` |
| `install.sh` | repo root | manual | `data/update.lock`を`_lock_file`で確認(L210,276)。`MCS_UPDATE_REINSTALL`尊重。`install_options.py bootstrap-runtime-apply`を呼ぶ（settings移行であってupdater呼出しではない） | updaterをspawnしない。update.lock保持中はinstallを拒否/待機する関係 |
| `deployment/install/runtime.sh` | installer内部 | install子 | 同上（updater lock言及L751,664） | venv構築・runtime migration時にrun.lock継承を要求する経路あり（L664 comment） |
| `scripts/mcs_upgrade.py` | `mcs update`の実体 | manual | `_lifetime_fds()`で親FD継承してsubprocess | `mcs_update.py` plan/apply/rollbackをsubprocess呼出し。guide対話・`--installed-standalone`対応 |

### A-5. `settings_migration_receipt` のcaller（更新をgateするreceipt）

`pending()`判定のcaller: `install_options.py`（L153,176,542,558,591,600,660,670,796 — split mode移行の可否を広くgate）、`update_apply.py:145,486`、`update_lifecycle_context.py:238,252`、`update_planning.py:143,195`、`update_runtime_migration.py`経路。receipt実体は`data/`配下（P0-d参照）。

### A-6. 「送信・自己再起動」の集約

- 外部送信: Git fetch（check/plan/applyのtag確認）、通知spool経由の人向け通知（`notify_spool`/`notify_flush`）、サービス再起動（launchd bootout/bootstrap）。
- 自己再起動: post-merge子processが新codeで`--post-merge`を実行→services再起動→親reap。`update_execution._run_post_merge`がFD継承・timeout/kill-groupを管理。
- `_run_native_release`経路（dormant）: 同じ`_apply_pipeline`+`_release_cap`借用構造だが、post-merge子は`release_apply_owner._run_effect_post_merge`（`update_execution.py:212`）が担当。

---

## P0-b runtime/writer の入口

### B-1. `mcs/core/writer_lock.py` APIと性質

| API | 実体 | 性質 |
|---|---|---|
| `RUN_LOCK` | `data/run.lock` | flock対象file |
| `acquire_run_lock(path)` | L56 | `LOCK_EX\|LOCK_NB`非ブロッキング。fd返却or None。**全ledger writerが保持すべきlock**（docstring明記）。例外: resident extract_llm drainerは結果write毎に取得しfenced bookkeepingはlockless（`extract_llm._write_lock`） |
| `unlocked_transport(db, lock_fd, wait_s, deadline)` | L152 | transport中のみlock解放→DB作業前に再取得。再取得は`RUN_LOCK_REACQUIRE_S=150s`またはdeadlineまでbounded poll。更新marker出現or`code_stamp`変化で`RunLockLost(held=True)`、期限切れで`RunLockLost(held=False, updated=…)` |
| `wait_run_lock(path, wait_s, expected_code_stamp, marker_path)` | L190 | poll取得＋世代fence: 取得直後にmarker有or code_stamp不一致ならfdを返却しNone（旧code混在防止） |
| `writer_blocked()` | L127 | `data/update_in_progress.marker`存在 or `notify_spool.restore_writer_hold`でTrue |
| `updater_state()` | L147 | `(writer_blocked(), code_stamp())` tuple |
| `code_stamp()`/`code_stamp_checked()` | L81/93 | `mcs/**/*.py`の最新mtime_ns = コード世代stamp |

### B-2. writer_lock caller一覧（37 file、用途別分類）

「writer」= ledger/DBへ書込む境界。「admission/transport」= lock解放して通信する境界。「fence確認」= `updater_state`/`writer_blocked`で更新世代を検査。

| 分類 | file（repo相対、`git grep`全件） |
|---|---|
| extract系 writer/fence | `mcs/extract/rules/extract.py`, `mcs/extract/llm/extract_llm.py`, `extract_llm_carry.py`, `extract_llm_errors.py`, `extract_llm_resident.py`, `extract_llm_runner.py`, `extract_llm_source_contract.py`, `mcs/extract/attachment_inference.py`, `mcs/extract/attachment_ocr.py` |
| semantic系 writer | `mcs/semantic/semantic.py`, `semantic_candidate.py`, `semantic_drain.py`, `semantic_due_selection.py`, `semantic_intake_hold.py`, `semantic_jev.py`, `semantic_transport.py` |
| patients/rollup writer | `mcs/patients/rollup.py`, `patient_context_quality_controller.py`, `patient_context_quality_receipts.py`, `patient_context_refresh.py`, `patient_context_refresh_state.py` |
| ingest writer/admission | `mcs/ingest/run_check.py`（`RunLockLost`処理含む）, `health_recovery.py`, `health_report.py`, `init_data.py`, `project_metadata.py`, `cross_lists.py` |
| llm transport/admission | `mcs/llm/cloud_backlog.py`（11箇所・owner_alive/process生存確認含む）, `mcs/llm/cloud_inference.py` |
| 手動CLI writer | `mcs/ops/mcs_ocr.py`（13箇所・最多）, `mcs/ops/cloud_cli.py`, `mcs/ops/commands/job_cli.py`, `mcs/ops/notification_cancel.py`, `mcs/ops/ocr_failure_ack.py` |
| standalone scheduler | `mcs_standalone/runtime.py` |
| updater自身 | `mcs/ops/update_lifecycle_context.py`（UPDATE_LOCK/RUN_LOCK取得・`_acquire_run_lock_wait`）, `mcs/ops/update_apply.py`, `mcs/ops/update_recovery.py`, `mcs/ops/mcs_update.py`, `mcs/ops/release_apply_owner.py` |
| CI/meta | `ci/gates.py`（検査用、製品経路ではない） |

### B-3. SQLite境界

- 正本DB: `mcs/core/ledger.py` — `sqlite3.connect(path, timeout=30)`（L113）、URI接続（L1214,1235）。`PRAGMA foreign_keys=ON`/`busy_timeout=30000`。save_patient/save_messages等は`with self.db` transaction。
- ledger.dbを開く他module多数（extract/llm store系・adapters/common・recovery DB tool等、`git grep sqlite3.connect`で30+ file）。**境界規則**: run.lock保持下での書込み、または`unlocked_transport`で通信中のみ解放→再取得後に書込み。v3では「管理対象起動・新規admission・既存結果確定」の3境界へ接続し、全SQL変更は対象外。
- recovery系: `deployment/recovery/mcs_recovery_database.py`が独立にDBを扱う（updater lockとは別系統）。

### B-4. 通知sender

- `mcs/notify/notify_spool.py` / `notify_flush.py`: data配下のspool経由で人へ通知。`notify_spool.py:120`で`marker.get("by") in ("mcs_update","mcs_recover")`を識別。`notify_flush.py:372`に`update_apply` control op、L278/349に更新関連の固定文言。**更新中の通知抑止/識別は既存機構あり**。
- 送信本体はadapter側（`MCSAdapter`）— v3の「送信」境界はここへ接続。

### B-5. 課金reservation / OCR / backup

- 課金: `mcs/llm/admission/llm_admission.py` — 共有local LLM slotのRT/BACKLOG admission。permitはpre-send reservation〜confirmed backendまで計数。`cloud_inference`/`cloud_backlog`が`unlocked_transport`で通信中lock解放。**v3 drain/退役との境界はこことwriter_lockの交差**。
- OCR: `mcs/ops/mcs_ocr.py`（手動CLI、writer_lock最多使用）＋`mcs/extract/attachment_ocr.py`/`attachment_inference.py`（定期job `mcs_ocr.sh`経由）。
- backup/offsite: `deployment/scripts/mcs_offsite.sh`→`mcs/ops/mcs_backup.py offsite`。**`data/update_in_progress.marker`存在時はexit 0でskip**（L30-32）— 更新中backup抑止は実装済み。
- `mcs_offsite.sh`は`__BACKUP_ENABLED__`/`__BACKUP_POLICY__`のrendered opt-in必須（既定OFF）。

### B-6. 手動CLI入口（`mcs/ops/mcs_cli.py` dispatch）

| command | 実体 | 備考 |
|---|---|---|
| `install` | `os.execv /bin/sh install.sh` | 導入 |
| `update` | guide/plan/apply→`scripts/mcs_upgrade.py` subprocess、rollback→`os.execv mcs_update.py rollback` | `MCS_LIFECYCLE_PINNED`未設定時はruntime選択pythonへ`os.execv`で再exec |
| `setup`/`doctor` | `mcs_setup.py` | init/services/check/features/api-keys/slack-channels |
| `recover` | `~/.mcs-recovery/mcs_recover.py` exec | repo外独立tool |
| `connect`/`connection`/`disconnect`/`rotate-key` | `sdc_cli.py` | SDC credential操作 |
| `inventory` | `sdc_inventory_cli.py` | |
| `support` | `sdc_support_cli.py` | |
| `diagnostics` | `sdc_diagnostic_cli.py` | |
| `jobs` | `job_cli.py` | 失敗job調査/再試行 |
| `drug` | `mcs_drug.py` | 医薬品master確認 |

`scripts/mcs`はshell bootstrapで`mcs/ops/mcs_cli.py`へdispatch（dirではない）。

### B-7. profile別 runtime/writer 一覧

| profile | 常駐/定期process（launchd/cron） | writer経路 |
|---|---|---|
| **hermes** | `ai.mcs.extract-drainer`×**3**（day/night/-2 — 固定2workerではない）、`ai.mcs.llamaserver`（共有LLM daemon、更新対象外区分）、`local.mcs-cmd`（cmd取込）、`local.mcs-health`（300s health_watch）、`local.mcs-int`（5s interval job）、`local.mcs-sdc`（300s `sdc_tick.py`→`sdc_cli.tick`→Store.tick.lock）、`org.mcs.recovery`（900s独立復旧）、cron `mcs_update.sh`（日次check）・`mcs_check.sh`（未読check）・他`mcs_*.sh` | drainers=結果write毎run.lock、cmd/semantic/ingest各job=run.lock保持writer、sdc tick=tick.lock系列、updater=U/R/G/A/O系列 |
| **standalone** | `ai.mcs.standalone`→`mcs_standalone/runtime.py` 単一host・7定期job・cmd取込・抽出worker3本 | runtime.pyがschedulerとして各jobを起動、個々のjobは同じwriter_lock経路 |
| **lineworks** | `lineworks_adapter/` 独立CLI（interactive="lineworks"） | adapter process自体は収集/通知、ledger writerはMCS本体側と同じrun.lock規則に従うかは**要確認**（adapter→MCS呼出し境界） |
| 監視・復旧自身 | `health_watch.py`（local.mcs-health）、`org.mcs.recovery` | 更新・復旧を「止めない」側。v3のQ/drain対象に含めるかはprofile設計で要判断（spec: 監視・復旧自身を区別） |

---

## P0-c lock の実DAG

### C-1. lock正本の解決（`git ls-files`/`git grep`で確認）

| 記号 | file | 実装 | 役割（02 §4） |
|---|---|---|---|
| G | `~/.mcs-security/release-entry.lock` | `adapters/sdc/storage.py` `Store.release_entry_lock(shared=)` L677 | owner/recovery間の変更排他。shared mode存在 |
| U | `data/update.lock` | `mcs/ops/update_lifecycle_context.py` `UPDATE_LOCK`/`acquire_update_lock()` | 既存journal/更新排他 |
| Q | `runtime-entry.lock` | **未存在**（v3新設予定） | code切替/全業務process寿命 |
| R | `data/run.lock` | `mcs/core/writer_lock.py` `RUN_LOCK`/`acquire_run_lock` | 既存writer排他 |
| C | `config.json.edit.lock` | **`mcs/ops/setup/config_edit.py` `_edit_lock`**（L131付近。`mcs/ops/config_edit.py`は存在しない — 仕様の警告どおり実配置を解決）。`provider_settings._verify_lock`も同一file | private設定snapshot最終整合 |
| A | `release-authority.lock` | `storage.py` `release_authority_lock()` L659 | 同意/取消/bootstrap/currentness |
| M | `tick.lock` | `storage.py` `_file_lock('tick.lock')` L602（`Store.lock()`/tick経路） | 端末identity/credential snapshot、monitor tick寿命 |
| O | `release-owner.lock` | `storage.py` `release_owner_lock()` L669 | 短いowner state CAS |
| 補助 | `support-state.lock`/`support-session.lock`/`restart-state.lock`/`diagnostic-state.lock` | 各`*_state.py`/`storage.py` | family別。`support_state.py`は「support-state.lock→tick.lock」順、`restart_execution.py`は「restart-state.lock→tick.lock」順を明記 |

### C-2. `_edit_lock` の実装（`mcs/ops/setup/config_edit.py`）

- `os.open(path, O_NOFOLLOW|O_NONBLOCK, 0o600)`→fstat検証（regular file・owner・mode 0600・link count 1・size 0）→`fcntl.flock(LOCK_EX|LOCK_NB)`→lock後identity再検証。`ConfigConflict("config_busy")`/`("config_lock_changed")`。
- caller: `adapters/slack/provision.py`, `mcs/medications/drug_alias.py`, `mcs/ops/cloud_cli.py`, `mcs/ops/install_options.py`（+`provider_settings._verify_lock`同一file経路）。
- **C lockとG/U/R/A/M/Oの間に直接の取得関係は確認されず** — config writerはrelease lockを取らない。specの「Cの実体・別設定fileのwriter・support lockとの関係はP0で全callerを照合」に対し、現時点の実測では**逆順edgeは未検出**。ただし「全caller網羅」は本表の列挙範囲であり、動的import経路・test以外の実運用callerの最終確認はM3で継続。

### C-3. 実測した取得順序（`_run_native_release`内、release_apply_owner.py L888–930）

```
G(release_entry_lock) → U(acquire_update_lock) → R(acquire_run_lock, bounded loop)
  → preflight（G+U+R保持下）
  → A(release_authority_lock) ×2回（_current_change読取り）
  → O(release_owner_lock)（witness読取り）
```
- FDは`_verify_inherited_lock(fd, path)`でidentity検証後、`owned_process.lifetime_fds(locks)`で子process寿命へ束縛。`_StartedEffect.locks/lock_identities`に記録。
- 仕様目標 `G→U→R→A`（02 §4「現行A→M→OとG→U→R→Aの相対関係を維持」）と**一致**。A→Oも`A→M→O`系列の一部として整合。
- `A→M→O`系列: `release_authority_lock`保持下でtick.lock(M)を経てowner lock(O)へ、という順序がsupport/restart系の「state.lock→tick.lock」パターンと整合。**ただしA保持中にMを取得する実callerの直接証拠は未確認** — Mは`local.mcs-sdc`の`sdc_cli.tick`が`store.lock()`で取得する独立process経路。A/M/Oの同一process内nestingはrelease_apply_owner系でのみ確認、M絡みのnestingは**未確認として記録**。

### C-4. 未確認・要M3継続の点

- Q(`runtime-entry.lock`)は未実装 — M3で新設。全業務入口のbootstrap loaderが対象。
- `unlocked_transport`中のR解放は「通信中はwriter不在」だが、Q導入後は「code切替はQ EX、process寿命はQ SH」の別軸になる — 現行R解放がQと矛盾しないかはM3設計事項。
- nested callをR外へ移すだけの変更は禁止（仕様）— `_apply_pipeline`が内部で更にlockを取る場合の再帰的DAGはM3で精査。
- 他familyのlock（support-session等）とrelease lockの交差は現時点で未検出だが、同一Store root配下のためM3で再照合。

---

## P0-d baseline/移行物のfilename表

### D-1. `adapters/sdc/storage.py` `NAMES`/`LIMITS`（L20–38、実測）

| filename | LIMIT | owner/用途分類 |
|---|---|---|
| `endpoint.json` | 4096 | SDC endpoint設定 |
| `credential` | 4096 | 旧credential実体 |
| `credential-store.json` | mutation_contract.MAX_MARKER | credential mutation marker |
| `state.json` | 8MiB | monitor state |
| `tick.lock` | 0 | M lock（file lock、内容なし） |
| `inventory-policy.json` | 1MiB | inventory許可policy |
| `inventory-spool.json` | 2MiB | inventory spool |
| `inventory-status.json` | 16KiB | inventory状態 |
| `enrollment-consent.json` | 16KiB | enrollment同意 |
| `support-state.json` | 64KiB | support状態 |
| `support-state.lock` | 0 | support state lock |
| `support-session.lock` | 0 | support session lock |
| `support-bootstrap.json` | 16KiB | support bootstrap |
| `restart-state.json` | RESTART_STATE_MAX_BYTES | restart状態 |
| `restart-state.lock` | 0 | restart lock |
| `restart-execution.json` | RESTART_EXECUTION_MAX_BYTES | restart実行 |
| `restart-planning.json` | RESTART_PLANNING_MAX_BYTES | restart計画 |
| `provision-state.json` | 1MiB | provision状態 |
| `diagnostic-state.json` | 1MiB | diagnostic状態 |
| `diagnostic-anchor.json` | 4096 | diagnostic anchor |
| `diagnostic-state.lock` | 0 | diagnostic lock |
| `release-owner.json` | 8MiB | **v1/v3共有** — `release_state.py`(schema `mcs.release-owner.v1`)と`release_auto_state.py`(schema `mcs.release-owner.v3`)が同一file名を使用。`schema_version` constで判別、v3は`release-migration-v3.json`のintent経由でのみ移行 |
| `release-owner.lock` | 0 | O lock |
| `release-owner-activation.json` | 16KiB | v1 activation marker |
| `release-entry.lock` | 0 | G lock |
| `release-bootstrap.json` | ~36KiB | release bootstrap証拠 |
| `release-authority.lock` | 0 | A lock |
| `release-planning.json` | 16KiB | release計画 |
| `release-plan.json` | 64KiB | release plan |
| `release-change.json` | 128KiB | release変更 |
| `release-successor.json` | ~2.06MiB | successor登録 |
| `release-start-intent.json` | 32KiB | v3 StartIntent（`release_auto_state.py` INTENT_NAME） |
| `release-migration-v3.json` | 64KiB | v3 MigrationIntent（`release_auto_migration.py` NAME） |

**未列挙recordがあれば自動移行を拒否**（02 §253）— `NAMES`がallowlist正本。v3 migrationは`NAMES`外のfile検出で拒否する設計が必要。

### D-2. Store外のdata/配下ファイル（`git grep`/コード確認）

| file | 用途 | 根拠 |
|---|---|---|
| `data/update.lock` | U lock | `update_lifecycle_context.UPDATE_LOCK` |
| `data/run.lock` | R lock | `writer_lock.RUN_LOCK` |
| `data/update_state.json` | update journal（state/stages/applied/release_ref） | `update_journal`/`update_cli.STATE_PATH` |
| `data/update_in_progress.marker` | 更新中fence（writer_blocked・offsite skip・wait_run_lock判定） | `writer_lock.UPDATE_MARKER_NAME`、`mcs_offsite.sh` L30 |
| `data/mcs.log` | run log | `mcs_check.sh`等 |
| `data/ledger.db` | SQLite正本 | `ledger.py` |
| notify spool files | 通知queue | `notify_spool.py`/`notify_flush.py` |
| `.mcs-release-dependencies.json` | runtime依存receipt | `release_dependency_lock.py` |
| recovery `.prev` | 旧version退避 | `prepare_recovery.py`/`update_restore.py`系 |
| backup snapshot dir | offsite backup | `mcs_backup.py`/`mcs_offsite.sh` |

### D-3. `deployment/` のrelease関連file

- 存在: `deployment/sdc-release-owner.json`（owner設定）、`deployment/sdc-release-lock.schema.json`（schema雛形）
- **欠損: `deployment/sdc-release-lock.json`** — 実profile値は未生成。specの指示どおり「実profile準備待ち」と記録。生成手順は実装可能だが実環境の値を推測してcommitしない。

### D-4. v1→v3移行で注意すべき共有file

- `release-owner.json`: v1(`mcs.release-owner.v1`)とv3(`mcs.release-owner.v3`)が**同名fileを共用**。v3 `release_auto_state.load`はv1 fileをschema不一致で拒否（`UNKNOWN_BASELINE`）。移行は`release-migration-v3.json`のMigrationIntent（phase `prepared→old_archived→owner_replaced→verified`単調）経由のみ。**v1 fileをv3として読み替える経路は禁止済み**。
- `release-owner-activation.json`: v1専用（`mcs.release-owner-activation.v1`）。v3の対応物は別途設計。
- `release-start-intent.json`: v3新設（32KiB）。v1に対応物なし。

---

## P0完了条件との対応

| 完了条件 | 状態 |
|---|---|
| 現行経路対応表 | ✅ A-1〜A-6（caller・profile・親process・lock・data変更・送信・再起動・借用FDを記録） |
| 実lock DAG | ✅ C-1〜C-4（G/U/R/A/M/O/C実配置解決、実測順序G→U→R→A→O、未確認点を明示） |
| bridge対象 | 🟡 `_run_native_release`がdormant（production caller無し）を記録。M3でbridge/loader/Q設計へ接続 |
| backend integration slots | 🟡 CSDC側は別途（csdc P0文書参照）。MCS側は`sdc_tick`/`Connector.tick`/`Transport`が中央poll境界 |
| 今回触らない機能 | ✅ health_watch/notify文言・mcs_offsite・recovery独立系・llamaserver等を区分記録 |

## 未確認として残す項目（推測で埋めない）

1. `_run_native_release`のproduction起動条件 — v1署名release applyが実際に発火する経路（中央operation→端末dispatch）が本worktreeのgrep範囲では未検出。dormantの可能性が高いが、外部trigger（例: 別process/CLI）の存在はM3で再確認。
2. `A→M→O`の同一process内nesting直接証拠 — support/restart系の「state.lock→tick.lock」は確認したが、A保持中のM取得callerは未確認。
3. lineworks_adapter→ledger writerのrun.lock遵守 — adapter境界の実装詳細は本棚卸し範囲外、M3/W06で確認。
4. 実機（`~/.mcs`）のprofile差分 — deployment雛形ベース。実機のlaunchd loaded状態・cron登録・独自scriptは未確認。
5. C lock（config_edit）とrelease lockの動的交差 — 静的grepでは未検出。runtime caller網羅はM3継続。
