# 自動更新 v2.0 — 実装タスク分解（Devin作業用）

作成: 2026-11-10。対象仕様: `docs/specs/auto-update-v2.md`（統合読取り版を保存）。
作業worktree: `~/.mcs-wt/auto-update-v2-20261110`（HEAD `5337981c`、detached）。
仕様基準SHA `e85763b5` はHEADの祖先（126 commits差）。両repoで実装。push/PR/merge/tag/公開/配備/実機操作は行わない。

## 1. パッケージ欠落（要ユーザー確認）

貼付されたのは統合版 `FULL_SPEC.md` のみ。以下の機械可読ファイルは未受領:

- `contracts/contracts-v2.schema.json`（JSON Schema Draft2020-12 正本）
- `interfaces.pyi`（関数境界stub）
- `contracts/acceptance-tests.json`（98項目の機械可読正本）
- 正常11例・否定15例の合成fixture
- `deployment-parameters.example.json`
- `validation/check_package.py` 一式

FIELD_CATALOGは全70 $defsのfield一覧を持つが、**primitive $defs（`Uuid`/`Sha256`/`Token`/`Slug`/`Commit`/`Tag`/`Uint`/`UInt64Decimal`/`RawJson`/`Positive`）のpattern・境界は未定義**。schemaはcatalog+本文から再構成可能だが、厳密なregex/上限は推測になる。実装着手前に原本ZIPの所在を確認するか、再構成したschemaを「暫定・原本照合待ち」として扱う。

## 2. 基準SHA→現HEAD差分（仕様関連pathのみ）

`git diff e85763b5..5337981c` の仕様関連pathは9ファイルのみ。SRC02–16の参照先は実質不変:

- `mcs/core/error_codes.py` +150行（固定エラーコードregistry新設。v2の`mcs.release-error.v2`コード体系と統合要検討）
- `mcs/core/{maintenance,mcs_util,snapshots}.py`、`mcs/ops/update_runtime_migration.py` 軽微
- `deployment/sdc-release-owner.json` 1行、`deployment/launchagents/README.md`、`deployment/scripts/mcs_offsite.sh`
- `adapters/sdc/`・`mcs/ops/{update_apply,update_cli,mcs_update,release_apply_owner,release_apply_checks,release_owner_source,sdc_support_signatures}.py`・`mcs/core/writer_lock.py`・`mcs/ops/update_services.py`・`.github/workflows/`：**差分ゼロ**

→ 仕様のSRC根拠は現HEADでそのまま有効。P0対応表は「差分なし確認済み」として簡略化できる。

## 3. repo役割分担

|repo | worktree/場所 | 担当 |
|---|---|---|
| hermes-mcs | `~/.mcs-wt/auto-update-v2-20261110` | W01–W12, W14, W15（端末側・publisher・CI） |
| csdc（SDC中央） | `/Users/yusuke/workspace/csdc` | W13（中央backend API・永続化・publisher OIDC面） |

csdc側の既存資産: `fleet/releases/{contracts,apply-wire,apply,signatures,registration}.js`（R5a/b契約・署名・登録）、`fleet/support/`（support/restart契約）、`src/security.js`（HttpError/strictJSON/exactFields）、`fleet/access.js`（JWKS/OIDC系）、`fleet/migrations/`+`migrations/`。W13は新規 `/v2/mcs/...` 面として追加し、既存device HMAC認証を拡張する。実backend repoはcsdcで確定（要Plans.md記録）。

## 4. タスク分解

### Phase 0 — P0棚卸し（仕様必須・読取り中心）

|ID|内容|成果物|
|---|---|---|
|P0-a|`apply`/`_apply_pipeline`/`cmd_check`/`--post-merge`/`rollback`/`recover`/`_run_native_release`/`install.sh`/`mcs_upgrade`/`reinstall_done`/`settings_migration_receipt`の全caller列挙。file/function/profile/親process/lock/実data変更/外部送信/再起動/借用FDを記録|`docs/dev-records/auto-update-inventory.md`|
|P0-b|writer_lock全acquire・unlocked_transport・wait_run_lock、SQLite境界、通知sender、課金、OCR、backup/offsite、手動CLI入口。Hermes/standalone/LINE WORKSをprofile別一覧|同上|
|P0-c|`config_edit._edit_lock`実配置を`git ls-files`/`git grep`で解決（`mcs/ops/config_edit.py`の存在を仮定しない）。lock DAGと目標`G→U→Q→R→C→A→M→O`の逆行edge特定|同上|
|P0-d|`adapters/sdc/storage.py`の`NAMES`/`LIMITS`（L20–36確認済）と全callerからfilename表生成。`deployment/sdc-release-lock.json`は欠損→「実profile準備待ち」と記録（fixtureで隠さない）|同上|

P0完了条件: 経路対応表・実lock DAG・bridge対象・backend integration slots・非対象機能が明確。

### Phase 1 — 契約基盤（W01–W03、RED試験先行）

|ID|作業|依存|主な検証|
|---|---|---|---|
|W01|新`adapters/sdc/release_auto_contract.py`＋contracts JSON再構成。`parse_typed`/`canonical_typed`/`hash_v2`、ENC01–04、SEM01–34、v1 dispatch保持|P0|AT036–041の構造/意味vector、v1未知schema拒否、purpose分離|
|W02|`mcs/ops/sdc_support_signatures.py`へv2 purpose wrapper。独立pins/expected、historical分離|W01|AT037。旧用途受入集合不変|
|W03|新`adapters/sdc/release_auto_state.py`＋`storage.py`の`NAMES`/`LIMITS`追加（start-intent/migration-v2/auto-private/runtime-proof/runtime-entry.lock/evidence/）。OwnerState load/validate/CAS、capacity、intent/spent/terminal/outbox|W01|AT075–083 fault injection、連続2更新、ACK遅延でexecution_revision不変|

### Phase 2 — 端末bootstrap・drain・effect（W04–W06, W08–W10）

|ID|作業|依存|主な検証|
|---|---|---|---|
|W04|新`mcs/ops/release_auto_bridge.py`＋既存recovery一式。ControlManifest/MigrationIntent/初期baseline/v1→v2明示移行|W03|AT086–090。pending/unknown拒否、crash各点、旧入口閉鎖、`.prev`維持|
|W05|新`mcs/core/runtime_entry.py`＋各launcher。Q寿命gate、prepared fence、世代/boot、quarantine admission|W03|AT055,068–073。import途中切替禁止、全入口coverage|
|W06|`update_services.py`/service_management/standalone/Hermes/LINE WORKS入口。profile別drain/退役/起動観測、独立owner起動|W05|AT049–052。R外drain、強制kill非流用、deadlockなし|
|W08|新`mcs/ops/release_auto_owner.py`＋既存owner private dispatch。stage→prepare→grant→consume→effect→proof、clock/nonce/budgets|W04–W06|正常経路前進、未知時証拠保持。AT046–065|
|W09|`mcs/ops/update_apply.py`へprivate `apply_code_only_v2(capability)`。forward-only policy分離|W08|AT066,084。post-merge失敗でreset/DBrestoreゼロ、v1回帰|
|W10|新`mcs/ops/release_runtime_proof.py`＋独立recovery generation。quarantine/activation/proof/観測的終結|W08|AT070–079。HEAD/PIDだけ不可、boot/loadedsource/heartbeat束縛|

### Phase 3 — transport・publisher・中央（W07, W11–W13）

|ID|作業|repo|依存|主な検証|
|---|---|---|---|---|
|W07|新`adapters/sdc/release_auto_transport.py`。固定JSON v2 path＋artifact stream（256MiB/300s/no-redirect）|MCS|W01|AT042–045|
|W11|新`scripts/development/release_publish.py`＋`.github/workflows/release-publish.yml`。identify/package/authorize/publish/verify-register|MCS|W13|AT001–028,029–035。fork拒否、samebytes retry、OIDC/ID検証|
|W12|`ci.yml`/`release-notes.yml`/release generators。request/evidence出力、ノート維持、draft責任移管|MCS|W11|AT026。新旧workflow競合なし|
|W13|csdc中央backend。`/v2/mcs/releases/{select,metadata,artifact,start,status,cancel-prestart,result}`＋`/v2/mcs/publications/{reserve,objects/{sha256},finalize,register}`。10 table・TX-PUBLISH/START/ABORT/RESULT・CAS/cohort/grant/result・OIDC publisher面|CSDC|W01契約共有|client共通vector、tenant分離、署名key分離、rate budgets。AT016–025,092–095の中央側|

W13のcsdc設計メモ: 既存`fleet/`に新module（仮`fleet/auto-update/`）＋migration追加。device面は既存HMAC device authを専用pathへ拡張、publisher面は`fleet/access.js`系OIDCを再利用し`reserve_publication`/`freeze_assets`/`sign_release`/`register_publication`のみ許可。既存release契約（R5a/b）validatorを再利用し、wire schemaはW01と共有vectorで一致させる。**中央は署名鍵を持たず認証済み内容だけをsignする**（外部signer段階分け）。

### Phase 4 — 結線・受入（W14, W15）

|ID|作業|依存|
|---|---|---|
|W14|`update_cli.py`/scheduler/status/doctor。顧客v2ルーティング、legacy auto抑止、固定状態/理由、outbox retry|W08–W10＋v1回帰・連続2更新・fault injection通過後。既定OFF|
|W15|tests/docs/changes記録。05の98項目受入票、既存回帰、現行受入票|全W|

並列化（仕様§5準拠）: W07・W11/W12はW01–W03完了後に独立着手可。W13は契約確定後にcsdc側で独立着手可。W14は最後。

## 5. AT分類→W対応（98項目）

- PUB(28): W11/W12/W13 — GitHub公開・受入・OIDC・公開順序
- ART(7): W04/W11 — manifest/lock/closure/pack検査
- CON(6): W01/W02 — strict parse/署名purpose/境界
- NET(4): W07/W13 — transport上限・stream・redirect
- DEV(40): W03–W10 — 状態機械・lock・一回性・crash・連続更新
- MIG(5): W04 — v1→v2移行・復旧器世代
- OPS(8): W13/W14/W15 — 運用gate・容量・ACK・回帰

## 6. ブロッカー・未決事項

1. **機械可読パッケージ未受領**（§1）。schema/pyi/acceptance JSON/fixturesがないと厳密照合不可。
2. GitHub repo設定（PUB01–06: ruleset/tag作成制限/Immutable/environment `mcs-release-publish`/権限）は別承認の実設定。実装はfixture/mockで進める。
3. 実鍵pin・issuer/toolchain/publisher policy・中央URL・各profile・保守窓・人同意は未確定→`AUTO_ELIGIBLE=false`維持。
4. `deployment/sdc-release-lock.json`欠損＝実profile準備待ち（P0-dで記録、fixtureで隠さない）。
5. macOS native試験（L4）は別承認の検証機が必要。Linux stubで代替しない。
6. csdc側W13は中央の既存release/apply契約面に依存。TypeError修正（進行中）はその前提堅牢化。
7. `mcs/core/error_codes.py`新設（+150行）とv2 `mcs.release-error.v2`コード体系の統合方針をW01で確認。

## 7. csdc側の進行中作業（本タスクと並行）

- `fleet/releases/contracts.js`: options/null→TypeError修正済み（`opts`ガード、`releasePlanBlockers`のTypeError→400変換、`deriveReleaseModes` state検査）
- 未完: `apply-wire.js`/`signatures.js`/`registration.js`の同種修正、回帰テスト、`npm run check`
- T40(thread/subprocess終了)・`maximum_now` HTTP入口の確認
- stale文書（implementation-matrix/production-gates/readiness）更新

## 8. v3.1パッケージ実施状況（2026-11-10時点）

正本: `~/dev/csdc-autoupdate-spec/hermes_csdc_autoupdate_devin_v3_1/`（SHA256SUMS検証済み）。
csdc側は `feat/mcs-autoupdate-v3-central`（worktree `~/workspace/csdc-autoupdate-v3`）、
MCS側は `feat/mcs-autoupdate-v3-client`（本worktree）で実装。

- C1契約層 `732cae5` / M1 Python契約+署名wrapper `4adb2d70`,`e256740b` / I1 parity実測済み
- C2永続化 `a25450b`（migration 0020/0021、AutoRepository、slot guard）
- C3 consent `cb3738e`+レビューR1–R7修正 `457392b`,`112c3a9`,`9d9333b`（policy guard・receipt束縛）
- C4前半: artifact台帳・publication予約 `881e6b3`,`11c52fd`,`87ef9a3`
- C8一部: policy admin `112c3a9`（preview/approve/revoke+専用grant）
- M2前半: owner state基盤 `231ea529`、migration intent `724c13f7`
- P0棚卸し `docs/dev-records/p0-auto-update-inventory.md` 生成＋r47指摘反映改訂（P0-a〜d: 更新経路caller・writer_lock/runtime・実lock DAG・Store filename表。raw grep証拠5fileを`p0-auto-update-inventory/`へ非切詰め保存。`_run_native_release`/`_issue_planning_consent`/`_prepare_native_plan`全てtest専用dormant・Q未実装・2 store root跨ぎlock DAG・A→M→O実証・C-FD借用・`release-owner.json` v1/v3共有を記録）
- M3設計案 `docs/dev-records/m3-autoupdate-design.md`（bridge/runtime_entry/Q lock/lock DAG統合。Q=`runtime-entry.lock`をcontrol root新設・SH=業務process寿命/EX=code切替、C参加は`Q→C`案、dormant v1はv3新規経路提案、AUTO_ELIGIBLE=false条件7項、RED試験構成5file、Codex判断依頼6件。Devin単独開発へ移行し6判断を自前確定 `e12927fd`）
- M3実装: Q lock `0647ef75`（Store.runtime_entry_lock・install一度のみO_EXCL・NAMES 33→34）、runtime bootstrap `6ddecb84`＋FD継承 `adopt()/child_env()/pass_fds()/current()`、bridge核 `c1275567`（classify_sidecars/check_v1_blockers/build_control_manifest/initial_owner_value/plan_migration/drive — 副作用は注入hook）、bridge hooks `2447ae77`＋CLI `6481fb77`＋Q EX保持 `827d17b7`、入口配線 `a9e81eca`（mcs_standalone・lineworks_adapter・adapters.lineworks・hermes_plugin.register — app import前acquire。updater/CLI多重化層はQ EX循環待ち回避でQ非保持、§3.3確定版参照）、PYTHONPATH/PYTHONHOME拒否 `33b3c530`（AT069段階的担保）、fence整合 `acquire()/adopt()`のowner.active×start-intent相互検査（FENCE_INCONSISTENT）＋§6適格性評価 `evaluate_eligibility`（AUTO_ELIGIBLE唯一導出点・True厳密測定のみ受理）
- M3 RED試験（全て合成・GREEN確認済み）: `tests/core/test_q_lock_dag.py` 6件（Q EX=SH保持者0証明・Q→C独立・C→Q逆順edge静的不在・FD継承OFD共有・foreign root adopt拒否・fence不整合起動拒否）、`tests/core/test_launcher_coverage.py` 6件（`__main__.py`全件分類・managed入口存在/acquire/role迂回なし・子process継承面契約・standalone worker Q継承）、`tests/ops/test_bridge_eligibility.py` 24件（§6全前提Trueのみ適格・各欠落/非dict/非True値でfail-closed・AT090 unmanaged検出・bridge未完了拒否）。既存 `test_runtime_entry.py` 18件・`test_sdc_runtime_entry_lock.py` 6件・`test_release_auto_bridge.py` 24件・sdc contract/state 各件と合わせ M3関連 **126件 pass**（`scripts/run_tests.sh` 実測、ruff clean）
- W07 transport `8f26c910`: `adapters/sdc/release_auto_transport.py`（api-contract.jsonのdevice route導出・JSON 16KiB/16KiB・artifact 256MiB/64KiB chunk/総deadline300s/identity/redirect禁止/size・digest不一致即中止・200のみ成功・`mcs.release-error.v3` decode）＋`protocol.sign_release_auto`（既存HMACをv3固定11pathへ拡張）。**呼出側未実装のため未配線**。`tests/adapters/sdc/test_release_auto_transport.py` 38件 pass
- M4 owner orchestration `763c1954`: `mcs/ops/release_auto_owner.py`（deps注入状態機械 — deliver_outbox固定raw/ACK一致のみack、request_offer/stage_candidate期限・scope・適格・capacity・artifact hash検証、prepare_runtime STAGED→PREPARING→drain→Q EX→witness→PREPARED＋rolling予算/時間窓/DST曖昧fail-closed、request_start/consume_start決定論的request・全項目照合grant・exec_rev束縛・intent→SPENT→STARTED段階的耐久化、cancel_prestart SEM21 — STAGED/PREPARING=DEFERRED・PREPARED=abort証拠をevidence/耐久保存→operation_evidence記録(exec_rev前進でprepared束縛無効化)→中央cancel-prestart→CANCELLED_NO_EFFECT・送信失敗はABORT_PENDINGで証拠保持しresumeが同bytes冪等再送、resume/tick観測的復旧。CASはconsume同一lock集合）。`tests/ops/test_release_auto_owner.py` 36件 pass・ruff clean。**呼出側未配線**（起動接続はW14・既定OFF維持）
- M5 code-only effect `454fe6af`: `mcs/ops/release_auto_apply.py`（W09 — STARTEDを耐久化した同一processだけが`issue_effect_capability`のprivate seal付きcapability経由で一回だけ実行。固定bundle sha256/size照合→制限Git環境でfetch→exact source_commitへ`merge --ff-only`→HEAD/rev-parse・diff-index・control closure・契約・schema・profile再測定。不一致は自動rollbackせずactiveをUNKNOWN+error_codeへ記録しOutcome unknownをoutbox固定。owner APPLIED保存→auxiliary journalの順序。`tick()`は`deps.apply_effect`注入時のみconsume直後に実行、未注入ならSTARTEDで停止）。`tests/ops/test_release_auto_apply.py` 13件＋owner 36件 pass・ruff clean。**呼出側未配線**
- M6 runtime proof/activation `e38b3dfd`: `mcs/ops/release_runtime_proof.py`（W10中核 — `advance_runtime`がAPPLIED→CANDIDATE_READY→ACTIVATING→VERIFIEDを1 tick 1 phaseで前進。APPLIED: startup_nonce耐久化→quarantine_launch receipt→service_desires全件のServiceProof(resident=running+process_instance_id/pid/birth_witness/loaded_source/previous_heartbeat必須・timer=armed・disabled=stopped)→RuntimeProof(op/generation/boot_id/loaded_source/startup_nonce/activation_receipt束縛)をactive.runtime_proofへ保存。CANDIDATE_READY: proof全項目+現在観測の再照合→自分の更新fenceを同generationで解放→ACTIVATING。ACTIVATING: resident service毎にfresh heartbeat 2回(間隔≥5秒)→service/source再照合→Outcome verified+TerminalRecord VERIFIED(proof_sha256=hash_v3('runtime-proof')束縛)→`finalize_verified`でoutcome/verified_installed/binding/outbox/active終結をO lock下atomic確定→`retire_intent`。lockはDEV40どおりG/Uのみ保持。証拠不確実・service drift・loaded不一致・manifest未検証は全てOwnerErrorでphase保持(UNKNOWN非成功化)。観測は全てdeps注入で実process/network不使用）。`tests/ops/test_release_runtime_proof.py` 14件 pass・owner/apply/state回帰全緑・ruff clean。**quarantine実起動・native PID/birth採取・heartbeat収集の実deps実装は未配線**（W10残り・既定OFF維持）
- M6 quarantine親側 `d738fb78`: `mcs/ops/release_quarantine.py`（QuarantineDeps注入 — 制約env(operation/receipt/startup_nonce/generation/boot_id/pycache_prefix/runtime_home/state_dir/source_root・空PYTHONPATH/PYTHONHOME)で固定bootstrap経路からservice spawn、完全witness既存時の重複spawn回避、process生存/birth独立検査、witness読取・検証、service状態・loaded source照合、update fence解放。receipt_idはoperation決定論的でcrash後retryが同一IDで冪等装着可能）。`tests/ops/test_release_quarantine.py` 18件 pass・結合試験でAPPLIED→VERIFIED実file通過・ruff clean。**実process spawn・native PID/birth採取はdeps注入面のみ、実装未配線**
- M6 quarantine子bootstrap `a69e0536`: `mcs/core/quarantine_bootstrap.py`（`-I -S`制約envの`MCS_Q_*`検証 — UUID v4/SHA-256/整数・generation/path制約/service kind/pycache prefix。供給source_rootのみへenter、runtime entry bindingをactive ownerと照合しmissing/unmanaged/stale/mismatchを全拒否、witnessをservice main呼出前に確立、binding不整合はfail-closed）。`tests/core/test_quarantine_bootstrap.py` 23件 pass・probe相互運用検証・ruff clean。**deployment側`-I -S`launcher強制・世代別pycache prefix実配線は未実施**
- M7 recovery controller `c188903a`: `mcs/ops/release_auto_recovery.py`（closure世代一式検証`verify_generation` — generation dir名=sha256(canonical)一致・descriptor bytes==canonical・manifest全fileのpath/mode/sha256照合・未知file/symlink/欠落/混成を全拒否・必須closure部分集合検査。`observe_status` read-only phase報告。`recover_tick`観測的終結 — APPLIED/CANDIDATE_READY/ACTIVATINGのみ`deps.runtime_advance`経由で前進、STAGED以前・UNKNOWN・v1 pendingはhold報告のみで新規grant/effect/コード変更/DB復元を一切行わない）。`tests/ops/test_release_auto_recovery.py` 14件 pass（APPLIED→VERIFIED完走・start/result送信不増を検証）・ops回帰120件 pass・ruff clean。**呼出側未配線**
- M7 recovery世代crash injection `tests/meta/test_recovery_generation.py` 15件: 既存`deployment/recovery/`のentrypoint+4support+`prepare_recovery.py`世代機構へloader/hash manifest/runtime directory/entrypoint/.prev各点のcrash・改竄・混成を注入。loader改竄/ symlink/oversized/partial generation(flat完備でもbundled優先で失敗)・manifest重複代入・support hash不一致・既存世代dir改竄(上書き修復しない)・staging残滓無視・install crash(entry置換直前で旧entrypoint+両世代dir保全・再install冪等収束)・`.prev`が自世代のみ選択(旧世代削除で.prev失敗・現行継続)を全てfail-closed確認
- M8 health/status/legacy抑止: `mcs/ops/release_auto_health.py`（W14 — RuntimeProofはfinalizeで消えるため`_complete_verified`がfinalize前に`record_verified_binding`でbinding snapshot(op/grant/rollout/target/runtime_generation/boot_id/local_runtime_proof_id/scope)を`release-health.json`へ耐久化。`report_health`はpending無し→bindingからHealthObservation(schema検証済)を組立てseq+1でpending保存→lock外送信→HealthAck(report_id/observation_sha256/request_nonce/scope一致)でのみpending消去。送信失敗・ACK不一致は同一raw bytesで再送=SEM32/AT127準拠。`health_status` read-only DTO。binding無しでは報告を組立てない=unknownをhealthyにしない）。`mcs/ops/release_auto_status.py`（status_dto固定DTO — uninitialized/unreadable/idle/active/unknown区別、phase/error_codeはcontract enum値のみ通し生例外・path・hostnameを出さない。UNKNOWN phase→state='unknown'固定。`legacy_auto_blocked` — v3 owner state存在or読取不能でTrue=fail-closed抑止、file不在=未移行のみFalse）。`adapters/sdc/storage.py`へ`release-health.json` sidecar追加(NAMES/LIMITS 64KiB/read・write分類)。`release_runtime_proof._complete_verified`へfinalize前binding記録を配線(health側失敗はVERIFIEDを阻害しないtry隔離)。`tests/ops/test_release_auto_health.py` 11件 pass・runtime proof/owner/apply/state回帰93件 pass・ruff clean。**呼出側配線(実施済み)**: `update_cli.py`へ`_v3_state_root`/`_v3_auto_blocked`/`_v3_status_dto`追加 — `cmd_check`のlegacy auto適用を`_v3_auto_blocked()`でゲート(v3 state存在or読取不能→return 0、state_root未設定・dir未作成=未移行のみFalse、dir有りでStore不可→True fail-closed)。`cmd_status`出力へ`v3`キー追加(未設定時は省略、dir未作成=uninitialized、Store不可=unreadable)。人間承認picked経路は対象外。`tests/ops/test_update_cli_v3.py` 8件 pass・update系回帰140件 pass・changes/v3-status-legacy-auto-gate.json。**doctor表示も実施済み**: `release_auto_status.status_dto_at(state_root)`を追加しroot解決+失敗写像を集約、`update_cli._v3_status_dto`を委譲化。`mcs_setup.cmd_doctor`のreportへ`v3`キー追加(sdc.state_root未設定=None、`--share`共有レポートはallowlist対象外で非含有)。`tests/ops/test_mcs_setup_doctor.py`へ2件・`test_release_auto_health.py`へ`status_dto_at`6分岐1件追加、changes/v3-doctor-status.json。**残**: `report_health`のtransport送信側配線(scheduler/CLI — 中央endpoint設定が前提)
M6 quarantine実depsのprocess面 `mcs/ops/release_quarantine_native.py`: 実spawn(`native_spawner` — detached session・stdin/out/err遮断)・実proc観測(`native_proc_info` — os.kill生存確認+`default_birth`と同一計算のbirth witness、観測不能はfail-closed値)・実boot_id(`native_boot_id` — macOS kern.boottime/Linux boot_idをsha256化)・`native_quarantine_deps` factory。**実バグ2件修正**: `default_birth`が存在しないsysctl OID `kern.proc.pid.<pid>`を使い実機で常にBIRTH_UNAVAILABLEだった → `/bin/ps -o lstart=`+pid塩のsha256へ変更(PID非依存の絶対パスでconstrained envでも動作)。sysctl絶対パス候補も追加。`tests/ops/test_release_quarantine_native.py` 5件 — 実sleeper processでspawn→proc_info alive/birth一致→kill→dead検出を実測。47件(quarantine系)全緑・ruff clean。**残**: owner deps本体(transport/lock/git/signing面)の実配線 — advance_runtimeを完走させるにはQuarantineDeps以外の面も必要
- M9 publisher CLI `scripts/development/release_publish.py`: 6 command実装 — `inspect-event`(GitHub event+固定policy照合・repository.id/owner.id fork拒否・vX.Y.Z tag/commit/tag_oid検証→build-request.local.v1出力・GET相当のlocal処理のみ)、`check-acceptance`(同一SHA受入票照合→acceptance-check.local.v1)、`package`(固定asset集合のsha256/size計測→asset-list.local.v1+asset_set_sha256)、`reserve`/`publish`/`verify-register`(入力検証+dry-run plan/observation/record出力。`MCS_PUBLISH_EXECUTE=1`+OIDC token環境が揃う固定CIのみ実実行、それ以外は一切外部変更しない=仕様どおり)。全command path引数のみ・秘密値をstdoutへ出さない。`tests/meta/test_release_publish.py` 9件 pass・ruff clean。**W12一部実施**: `ci.yml`へ`publication-request` job追加 — tag push時のみ`publication-request.json`(repo ID/tag/tag OID/source SHA/upstream run ID/attempt・4KiB上限・未信頼hint)をartifact化。権威ある照合はpublisher identify側。`scripts/development/publication_request.py`新設+`ci_local.sh`へ7番目job mirror追加(shellcheck clean・runner/meta38件 pass)。**release-notes.yml照合済み(変更不要)**: `sync-existing`はrelease event時に`sync_release_notes.event_target`がGITHUB_EVENT_PATHのrelease.tag_name/id・stable tag・repository.full_name・GITHUB_REF一致を全検証し`synchronize(tag=,release_id=)`で当該releaseのみ対象。manual無指定時のみtag=Noneの一括確認 — 仕様の「release eventはそのtagだけ・manual無指定のみ一括」と一致。workflow本文は--tagを渡さないがenv経由で同等に制限される(文書と本文の差は移行試験で再照合する仕様事項)。日本語ノートcheck/export/sync・draft責任は現行維持、公開系一本化はrelease-publish.yml接続後。`tests/release/test_sync_release_notes.py` 13件 pass。**publisher-toolchain実測lock `scripts/development/publisher_toolchain.py`**: `measure`でgit/gh/pythonの解決path・`--version`・配布sha256を実測し`publisher-toolchain.lock`(schema `mcs.publisher-toolchain.v1`)へ記録(既存lockは上書き拒否)。`gh release verify`/`verify-asset`の能力を`--help`終了コードで確認し欠落はfail closed。`check`で現在toolchainとlockのtools/gh_capabilities照合(drift検出)。`tests/meta/test_publisher_toolchain.py` 7件 pass(偽tool PATH差替え・実測値のみ・捏造SHAなし=仕様どおり)。**残**: `release-publish.yml`新設(中央reserve/register API=C側前提のため未接続)・tagged lock/source manifest配備側適用
M7 closure配備側結線の一部: `_REQUIRED_CLOSURE`をbridge側`RECOVERY_REQUIRED_CLOSURE`へ単一正本化(recoveryはalias)し、`run_migration`がmanifest構築直後に必須8 fileの部分集合を検査 — 欠落は`CLOSURE_INCOMPLETE:<path>`で配備前拒否(配備後にrecoveryが動けないclosureを作らない)。`_cli_spec` fixtureを全必須file入り実manifest相当へ更新+拒否test追加。bridge/recovery 39件 pass・ruff clean。**recovery verify CLI追加**: `release_auto_recovery.main()` — `verify --generation-dir`がdescriptor(sdc-update-control.json)のcanonical bytesからmanifestを自己導出し`verify_generation`で一式照合(store/deps不要・read-only・closure単体実行可)。tamper/missing descriptor拒否test 3件追加(recovery 17件 pass)。status/tickはstore+deps注入が必要でtrusted driver側責務。**残**: trusted recovery driver配線・旧sidecar移行の実機適用
- I4 M側fault injection `tests/ops/test_release_auto_fault_injection.py` 8件: 連続更新A→B→Cをowner.tick経由で完走(一意release/manifest/publication identity・grant_id一意化・file_digest追従のFaultDeps)、stale code_seq offer拒否、APPLIED永続化後のStore/deps再作成(=process再起動相当)からruntime proof完走、consume済みgrant再送拒否、**consume_start耐久化点crash注入2件**(intent書込み後・START_INTENT前crash→open intent残存で再consumeは`INTENT_EXISTS`拒否・phase=PREPARED保持のfail-closed hold。START_INTENT永続化後・SPENT前crash→`observe_only`で保持・再consumeは`PHASE_MISMATCH`拒否=二重start防止。SPENT永続化後・STARTED前crash→spent記録が残りgrant消費済み扱い・`observe_only`保持、**retire_intent crash注入**(finalize_verified後・retire前crash→intent='open'残存でもterminal証拠で次op完走)、runtime proof遷移crash注入2件(`test_release_runtime_proof.py` — ACTIVATING永続化後・fence解放crash→`_complete_verified`冒頭の冪等再試行でVERIFIED。quarantine launch成功後・receipt永続化前crash→同一決定論的receipt_idで冪等再launch→CANDIDATE_READY))。**実バグ修正2件**: `release_auto_state.open_intent`が退役済みintent file残存で2件目opを永久に開始不可にしていた(I4が捕捉) — `phase='retired'`かつ対応terminalがoutcomesへ耐久化済みの場合のみ次op fenceへ置換、open残存・証拠不一致・破損は従来どおりfail-closed(`INTENT_EXISTS`/`INTENT_EVIDENCE`/`INVALID`)。**2件目**: `_complete_verified`が`finalize_verified`(outcomesへterminal耐久化)後・`retire_intent`前にcrashするとintent='open'が残り次opが永久停止 — open残存でも対応terminal証拠があればcrash残滓として置換許可(terminal未到達のopenは依然`INTENT_EXISTS`拒否)。state層test 2件追加。state21件+ops回帰113件+fault8件全緑・ruff clean
- M4 owner実depsの一部 `mcs/ops/release_auto_evidence.py`: deps契約の`evidence_store`/`evidence_load`実装 — control root(`runtime_entry.control_root`)配下`evidence/`へO_EXCL・0600・single-link・nofollowで耐久保存(file+dir fsync)。object_key=内容sha256で同bytes冪等・異bytes別key。loadはkey形式/regular/uid/mode/nlink/サイズ上限(8MiB/spec 03)に加え**内容hashとkeyの一致を検証**し改竄を`EVIDENCE_TAMPER`で拒否(owner側hash照合より早い層)。dir_fd walkでcontrol root末端2要素を0700/owner検証(`_open_release_gate`と同型)、evidence dir symlink・mode逸脱・非regular・途中切詰全てfail-closed。store既存同名は内容一致時のみ冪等受理・不一致は`EVIDENCE_CONFLICT`拒否。`tests/ops/test_release_auto_evidence.py` 10件 pass(roundtrip/冪等/私有mode/tamper/missing/bad key/size/symlink dir/mode逸脱/conflict)・gates 10/10・ruff clean。**owner deps本体の残り(transport/lock/git/signing面)は未配線**
- **実バグ修正(前commitのM8配線)**: `_v3_state_root`/`_doctor_v3_status`が`cfg['sdc']['state_root']`(SDC端末state)を読んでいた — v3正本は`control_root(runtime_home)`配下(spec 02/03・`runtime_entry.acquire`・`run_migration`・`_open_release_gate`が全て一致)。旧実装は移行後も常に`uninitialized`を返し`legacy_auto_blocked`が**fail-open**(抑止不発=二重更新リスク)だった。`control_root(HOME)`解決へ修正、`status`/`doctor`はcontrol root未作成時`uninitialized`を常時表示。decoyテスト(sdc.state_rootにowner stateがあっても無視)追加。update_cli_v3 9件+doctor関連+health 108件 pass・gates 10/10・ruff clean。evidence上限もspecの8MiB/fileへ修正
- 既知pre-existing失敗の再調査: `tests/adapters/standalone/test_runtime.py::test_jobs_quiesce_and_restart_after_the_updater_finishes` は現HEAD `8f26c910` で**再現せず** — 単独 pass・ファイル全体10件 pass・`tests/adapters/standalone/` 全616件 pass（98.59s）。報告時の失敗は順序/負荷依存のflakyか中間commitで解消。再発時は別途追跡
- 未完: C4 finalize/register（metadata asset生成方式はCodex確認中）、enrollment provisioning、
  C5–C8、M2残り（closure配備実体の実機適用は別承認）、M3残り（`-I -S` launcher形態のdeployment側強制はM5/M6・世代別pycache prefix実配線はM6 — `pycache_prefix`公開面は契約test済み）、M4残り（owner→実deps配線・W14起動接続はgate後）、M6残り（quarantine実deps実装・native PID/birth/heartbeat採取の実装）、M7残り（v3 closureの`_REQUIRED_CLOSURE`を実manifestへ反映する配備側結線・recovery controllerの実entrypoint/CLI化・旧sidecar移行の実機適用）、M8残り（report_health/status_dto/legacy_auto_blockedの呼出し側配線 — update_cli/scheduler/doctor接続はgate後）、M9残り（release-publish.yml新設・release-notes sync照合・toolchain lock・publisher実実行経路のOIDC/ID検証 — 中央APIはC側前提）、I2/I3/I5（C側handler/中央rollout/publish統合が前提のためM側単独では不可）、I6最終受入、I7実環境受入（別承認）
- flagは全て既定OFF。`AUTO_ELIGIBLE=false` 維持。`run_sandbox.py`未実行。push/PR/merge/tag/Release/配備/本番変更なし
