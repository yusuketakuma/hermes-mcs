# M3 設計案 — bridge / 全入口loader / Q lock / lock DAG統合

作成: Devin、2026-11-10。対象spec: `hermes_csdc_autoupdate_devin_v3_1` 02§1.1/1.2/4/11.1、04 W04–W06、09 M3行。
前提: P0棚卸し（`docs/dev-records/p0-auto-update-inventory.md`）の実測DAG・caller証拠を正本とする。C0契約mirror済み。
**本書は設計案であり製品コード未着手。** 実profile未測定・`deployment/sdc-release-lock.json`欠損のため `AUTO_ELIGIBLE=false` を維持する。bridge実実行・実機・外部変更は未承認。

## 1. 成果物と配置

| spec | 成果物 | 配置案 |
|---|---|---|
| W04 | bridge（ControlManifest/MigrationIntent/初期baseline/v1→v3明示移行） | 新規 `mcs/ops/release_auto_bridge.py` |
| W05 | Q寿命gate・prepared fence・世代/boot・quarantine admission | 新規 `mcs/core/runtime_entry.py` ＋ 全launcher配線 |
| W05/W06境界 | profile別入口のQ適用・独立owner起動slot | launcher群（§3.3）＋ service管理側の起動枠 |
| lock DAG | Q実装・C参加設計・DAG文書確定 | `adapters/sdc/storage.py`（NAMES/LIMITS/lock method追加）＋ P0文書C節改訂 |

M4以降（owner orchestration `release_auto_owner.py`、prepare/drain、capability→effect、quarantine/activation、recovery、status表示、publisher workflow）は本設計の範囲外。ただしbridgeが配備するclosureにはそれらの固定コードが含まれる前提でpathを確保する。

## 2. Q lock（`runtime-entry.lock`）設計

### 2.1 実体と配置

- **path**: control root `~/.mcs-release-control/<sha256(canonical runtime_home)>/runtime-entry.lock`。v2 file表（auto-update-v2.md L451）と同じcontrol root配置。G/A/Oと同じStore管理下に置き、2 root構造を増やさない。
- **Store統合**: `Store.NAMES`へ`runtime-entry.lock`追加（現行33 name→34 name）、`LIMITS`へ上限追加。`Store.runtime_entry_lock(*, shared=False)`を`release_entry_lock`（storage.py L673–684）と同型で新設 — `_release_entry_fd`相当のreentry検出・`shared`bool検証・context exitでのfd/記録解放を踏襲。`_file_lock`の`LOCK_NB`性質は維持（Qの待機はcaller側bounded pollで行い、flock自体は非待機）。
- **inode不変**: 導入後にfileを削除・再作成しない。recovery marker（`_pin_recovery_marker`）と同様にdev/ino identityをbootstrap時に照合する（§3.2）。
- **mode**: 0600 regular single-link、nofollow — Storeの既存file規律に従う。

### 2.2 取得規則（02§4の表に対応）

| process種別 | Q mode | 寿命 |
|---|---|---|
| 全業務入口（§3.3のloader対象） | SH | process起動〜終了（子/孫へFD継承） |
| code切替（v3 PREPARED採用/quarantine遷移） | EX | source確認まで保持、変更後に解放 |
| owner/recovery CLI（独立slot起動） | 取得しない（業務processではない）。code切替phaseでのみEX | phase毎に解放 |
| managed config writer | Q SH（業務入口bootstrapで取得済み）→ C | process寿命 |

- **SH保持＝code切替不可の証明**: flock意味論により、Q EX取得成功=管理下業務processのSH保持者が全て退役したことの証拠になる（AT055/068）。非協調process（旧worker・別checkout・旧watchdog・手動python起動）はQを取らないためEX取得を妨げない — これは「Qだけで全process検証済みとしない」（AT090）の裏返しで、**適格性判定は別系統**（§6）で行う。
- **FD継承**: 既存`lifetime_fds`/`_inherited_fds`機構（update_lifecycle_context.py）を再利用。bootstrapがQ SHのfdをlifetime leaseへ登録し、post-merge子・service子へdup継承。新規のFD受渡機構は作らない。
- **`unlocked_transport`×Q**: R解放区間（通信中）はQ SHを**保持したまま**。Qはprocess寿命lockでありRの一時解放と独立。code切替はQ EXが必要なので、R解放中のprocessもQ SHを持つ限り切替をブロックする — 仕様の「Q/R/C/A/M/Oは各phaseに従い解放」と整合（Qはphase=process寿命）。

### 2.3 DAG上の位置

目標順序 `G→U→Q→R→C→A→M→O` に対し：

- 業務processはQ SHのみ保持し、G/U/R/C/A/M/Oを同時保持しない（Rはwriter区間のみ、かつQ SHの内側）。
- updaterのv3採用sequence（spec表）: `G→U→Q(EX)→R→C→A→M→O`。Q EX取得時点で管理下業務processは全退役済みのためR/Cの競合は非協調writerのみ — それをC lockと最終snapshot照合で排除する設計。
- **追加edge（確定）**: managed config writerは全て業務入口bootstrapでQ SHをprocess寿命保持するため、config書込みは必ず`Q(SH)内側`で行われ`Q→C`順が自動的に成立する。`config_edit.apply`/`provider_settings.commit_locked(lock_fd=)`のFD借用関係（P0 C-4）は変更しない。updater側は採用sequenceで`…→Q(EX)→R→C→…`の順にCを取得し最終snapshot整合を行う。legacy config writerでQ非参加のものは「参加しないGUI/手動編集」として最終snapshot変化で拒否する（02§4表の規定どおり — 強制防御済みとは呼ばない）。
- **逆順edge不在の根拠**: Cを取る経路（config_edit/provider_settings）はrelease lockを一切取らず、Q/Cが未存在の現行コードに逆順edgeは存在し得ない。v3追加時に`C→Q`を作らないことが要点 — config writerは必ずQ SH取得後にのみCへ進む。

## 3. `runtime_entry.py` bootstrap設計

### 3.1 責務（02§1.2）

全業務入口は**アプリimportより前**に固定bootstrapを通す：

1. control root/storeの存在・owner/mode/nofollow検証（Store既存規律）
2. `runtime-entry.lock`のidentity照合（dev/ino/open-file-description — borrowed FDは別openで取り直さず検証、02§4）
3. private control state・start-intent・root/lock identity・許可generationの読取りと整合検査
4. Q SH取得＋lifetime lease登録（FDを子へ継承可能に）
5. pycache policy確定（§3.4）
6. 許可された起動のみ実行。拒否時は業務コードをimportしない

### 3.2 prepared fence / 世代

- OwnerState v3の`active`/StartIntent（`release-start-intent.json`、M2実装済み）と整合しない起動を拒否。単独marker消失を許可とみなさない（v2 spec §2）。
- **許可generationの供給源（確定）**: `OwnerState.runtime_generation`。`finalize_verified`（release_auto_state.py L367）がVERIFIED終端でのみ`outcome.runtime_generation`へ更新する単調fieldであり、「現在認可されたruntime世代」の正本として適格。`active.candidate_generation`は未認可の候補でありbootstrapは許可しない。v3未移行（control root/owner不存在）のdeviceはlegacy modeとしてQを取得せず通常起動 — Qの存在自体がv3適用済みの印。

### 3.3 入口coverage（P0 inventory A-1/B-7より）

| 入口 | 種別 | Q適用 |
|---|---|---|
| `mcs/ops/mcs_update.py`全subcommand | CLI | SH（業務入口） |
| `mcs_update.sh` cron | 定期 | SH |
| `install.sh` | 保守（code適用側） | Q EX — code面を変更する保守操作。別承認の保守作業としてのみ実行（§8決定4） |
| `mcs_standalone` 独立実行 | 常駐 | SH |
| `hermes_plugin` gateway | 常駐 | SH — **Hermes起動経路へ契約を適用できないprofileはauto不適格**（02§1.2） |
| `lineworks_adapter` | 独立CLI/常駐 | SH＋run.lock遵守確認（W06、P0未確認#2） |
| recovery入口（`org.mcs.recovery`） | 独立slot | Q非保持・code切替時EX（02§1.2「owner/復旧CLIは独立実行枠」） |
| `mcs_cli` 手動command | CLI | SH（B-6の全commandが対象 — config書込みもQ SH内側でCへ進む） |

各launcherは`python -I -S`推奨形態で固定bootstrapを読み、Q取得・runtime検証後にsite/flat-import pathを明示設定。`-B`単独・sourceless pyc・shadowing・未許可PYTHONPATHを拒否（AT069）。

### 3.4 pycache policy

- 世代別pycache prefixをCLI引数と`sys.pycache_prefix`の**両方**で確定・照合。`-B`だけで旧pycを読まない扱いにしない（02§1.2、AT069）。
- candidate import前にbootstrapがimport rootを固定 — 「import途中のcode切替禁止」（AT068）はQ SH寿命で担保。

### 3.5 bypass防止

- `--role=updater`等の外部引数だけでgateを迂回しない（02§1.2）。owner/recovery CLIは**サービス管理が所有する独立実行枠**から起動し、起動側が私有証拠を検証する。**証拠の正本（確定）**: 親processがservice管理unit（launchd plist/cron wrapper等deployment管理の起動script）であり、かつcontrol root内の私有fileへの**継承FD**を持つこと。argvのrole申告・環境変数の自己申告は資格にしない。FDのdev/ino identityをcontrol root記録と照合する（borrowed FDは別openで取り直さない — 02§4）。具体的な私有file名・記録fieldは実装時に`release_auto_bridge`のControlManifest/OwnerState側へ追加する（§8決定4）。

## 4. bridge設計（W04）

### 4.1 実行形態

- DEV01: 初回bridgeは**別承認の保守作業**。稼働checkoutでは開発・実行しない。人の承認receipt（`human_receipt_id`、MigrationIntent schema必須）を入力に取る単発CLI。
- 入口自体もruntime_entry bootstrapを通すかは§8-Q4（bridgeは業務processではないがcode面を触る — EX相当の扱いを要検討）。

### 4.2 検証項目（02§1.1/DEV01）

端末identity、trusted root、実依存profile、base release/source、control closure、独立復旧器、全管理入口、所有者の継続同意を照合。不適格が1つでもあれば`AUTO_ELIGIBLE=false`で中止（推測で進めない）。

### 4.3 ControlManifest構築と配備

- `mcs.release-control-manifest.v3`（schema mirror済み）を構築：`entrypoints`(≤16)・`files`(≤512、path/mode/sha256)・`dependency_policy_sha256`・`recovery_generation_sha256`。contract層`parse_typed`/`canonical_typed`/`hash_v3`で検証・hash化（M1済み）。
- closureには署名/currentness、strict parser、状態/START、lock、固定Git effect、readiness採用、独立復旧loaderを含める。**動的import先と標準外dependencyもmanifestへ明示**（02§1.1「未検査の遅延importをtrustedと呼ばない」）。
- 自身と全source manifestを一覧に含めず循環を作らない。`sdc-release-owner.json`（全source manifest）は広い検証対象として維持し、control closure digest同一を通常更新で必須化。v1 owner_revisionとv3認可版を混同しない。
- bridgeはclosureを**既存独立復旧領域のcontent-addressed generation**へ配備。第二のUpdater・別権限/別stateを作らない。**更新実行者がmutable checkoutからhelperを遅延importする構成は禁止** — closure内の固定codeのみ実行。

### 4.4 baseline初期化とv1→v3移行

- OwnerState v3初期baselineを`release_auto_state.py`（M2実装済み）で構築: `schema_version=mcs.release-owner.v3`、`UNKNOWN_BASELINE`扱いを明示初期化で脱却。missing fileを初期化とみなさない規律は維持。
- MigrationIntentは`release_auto_migration.py`（M2実装済み）のphase機械`prepared→old_archived→owner_replaced→verified`を使用。`source_schema`は`none`/`mcs.release-owner.v1`。
- §11.1のsidecar分類（owner/activation、bootstrap、planning、plan、change、successor、authority、依存receipt、update journal、backup/restore hold）を`MigrationFile.disposition`（preserve/archive_exact/translate_verified/must_be_absent）へ写像。**未列挙recordがあれば自動移行を拒否**。
- 移行禁止条件（§11.1/AT086）: v1 spent/unknown、pending successor、未解決restore/start intent、applying/stages途中。途中停止のresumeは同一intent/before/afterのみ。

### 4.5 dormant v1機構の扱い（確定: v3新規経路）

P0で判明した`_run_native_release`/`_issue_planning_consent`/`_prepare_native_plan`は**全てtest専用dormant**（production caller皆無）。**決定: v3新規経路を採用し、dormant v1関数へproduction配線しない。**

根拠: (a) MigrationIntent契約が`source_schema=mcs.release-owner.v1`を「移行元」として扱い、v3正本を別に定義、(b) v1関数群はv1 schema・v1 lock順`G→U→R→A→O`前提でv3目標DAG`G→U→Q→R→C→A→M→O`と不整合（Q/C非対応）、(c) dormantコードへの配線は「既存経路再利用」に見せかけた実質新規配線で検証範囲を曖昧にする。v3 owner orchestrationはtask map W08=`release_auto_owner.py`（M4+範囲）として新規実装する。dormant関数はv1回帰試験の対象として保持し、削除は別判断。

## 5. lock DAG統合の確定作業

| 項目 | 状態 | M3での扱い |
|---|---|---|
| `G(EX)→U→R→A→M→O`（v1 dormant経路） | 実測済 | v3経路では未使用（§4.5）。v1回帰として保持 |
| `G(SH)→U→R`（標準update、外側G(SH)含む） | 実測済 | 変更しない。v3適用後もlegacy update経路の回帰対象 |
| family `state.lock→tick.lock` 系 | 実測済 | 変更しない。Q SHはこれらと独立（別root・別phase） |
| `Q`新設 | 未実装 | §2の通り実装。DAG位置`U→Q→R` |
| `C`のDAG位置 | 確定（§2.3） | `Q→C` — 全managed writerがQ SH内側でC取得。updater採用sequenceも`…Q(EX)→R→C→A…`順 |
| `_apply_pipeline` nested再帰DAG | 未精査 | M3実装時に静的追跡（P0未確認#5）— v3経路は新規owner orchestrationで旧pipelineを通らないため影響はlegacy回帰側 |
| `unlocked_transport`×Q | 確定（§2.2） | Q SHはprocess寿命で保持、R解放区間とも独立。code切替はQ EXが必要なためR解放中processも切替をブロック |

**逆順edge検査（現時点の静的結論）**: Q/Cが未存在のため、現行コードに`…→Q`や`…→C→(G/U/R/A/M/O)`の逆順edgeは存在し得ない。C追加時に`C→Q`を作らないこと（config writerは必ずQ SH取得後にC）が設計上の要点。runtime caller網羅はM3実装時に`git grep`非切詰めで再実施。

## 6. `AUTO_ELIGIBLE=false` 継続条件（明記）

以下が解消されるまで端末はauto適格にならない。**設計・合成試験はこの状態で進める**:

1. 実機profile未測定（`~/.mcs`のlaunchd loaded状態・cron登録・独自script — P0未確認#3）
2. `deployment/sdc-release-lock.json`欠損（実profile値を推測しない）
3. 旧worker/別checkout/旧watchdogの適格性未確認（M3完了条件、AT090）
4. Hermes起動経路への契約適用可否がprofile依存（02§1.2 — 適用不能profileはauto不適格のまま）
5. lineworks_adapterのrun.lock遵守が未確認（P0未確認#2、W06）
6. C lock↔release lockの動的交差・`_apply_pipeline`再帰DAGが未網羅（P0未確認#4/#5）
7. bridge未実施（DEV01別承認待ち）— v1 owner存在・v3未移行の端末は対象外

## 7. RED試験構成（合成・実装後に有効化）

| test file案 | 対象 | 主なcase（AT対応） |
|---|---|---|
| `tests/adapters/sdc/test_release_auto_bridge.py` | bridge検証/manifest/migration拒否 | manifest hash循環拒否、closure digest不一致、未列挙sidecar拒否、pending/unknown/spent/successor移行拒否（AT086）、crash各点resume（AT087）、`.prev`世代維持 |
| `tests/mcs_ops/test_runtime_entry.py` | bootstrap/Q/pycache | import前Q SH取得、Q EX中の新process SH拒否/待機（AT055）、import途中切替禁止（AT068）、旧pyc不採用（AT069）、start-intent不整合起動拒否、sourceless pyc/shadowing/PYTHONPATH拒否 |
| `tests/mcs_ops/test_q_lock_dag.py` | lock順序 | `Q→C`順序強制、C→Q逆順なし、Q EX取得=SH保持者0の証明、FD継承（子/孫）、borrowed FD identity照合 |
| `tests/mcs_ops/test_launcher_coverage.py` | 入口coverage | 全launcherがbootstrap経由、`--role`引数のみでgate迂回不可、独立slot起動の親identity検証、未対応profileのAUTO_ELIGIBLE=false |
| `tests/mcs_ops/test_bridge_eligibility.py` | 適格性 | §6の各条件でfalse、旧worker/別checkout/旧watchdog検出でfalse（AT090） |

全て一時HOME/DB/Git/署名fixture・偽transport/偽service。実機・実サービス・実鍵不使用。`run_sandbox.py`は別途。

## 8. 設計判断の記録（旧「Codex判断依頼」→ Devin決定、r58単独開発体制）

ユーザー直接指示「devinのみで開発するようにしてください」（Codex r58/r59）により、Codexレビュー待ちを解除。以下6件は既存仕様・コード・承認範囲の証拠に基づきDevinが決定した。新規権限・不可逆な仕様変更を要する項目はない。

| # | 論点 | 決定 | 根拠 |
|---|---|---|---|
| 1 | Q path | `runtime-entry.lock`をcontrol root（`~/.mcs-release-control/<sha256>/`、G/A/Oと同所）に新設。NAMES 33→34 | v2 file表（auto-update-v2.md L451）がcontrol root節に明記。2 root構造を増やさない |
| 2 | C参加方式 | 全managed writerがbootstrapでQ SHをprocess寿命保持→config書込みは必ず`Q→C`順。`config_edit.apply`自体は変更しない。legacy非参加writerはsnapshot変化で拒否 | 02§1.2「全業務入口はbootstrapを通る」+02§4「参加しないGUI/手動編集は最終snapshot変化で拒否」 |
| 3 | dormant v1機構 | v3新規経路（W08 `release_auto_owner.py`）。dormant v1関数はproduction配線せずv1回帰試験対象として保持 | §4.5の3根拠。MigrationIntentがv1を移行元として扱う契約と整合 |
| 4 | bypass防止・入口mode | owner/recovery CLIの資格正本=service管理unit親process＋control root私有fileへの継承FD（dev/ino照合）。argv/env自己申告は不認。install.sh=Q EX（code適用側・別承認保守）、mcs_cli=Q SH、bridge=Q EX相当の保守枠 | 02§1.2「独立実行枠」「--role引数で迂回しない」。既存FD継承機構（MCS_RELEASE_CHILD_FDS/lifetime_fds）と同型 |
| 5 | generation供給源 | `OwnerState.runtime_generation`（`finalize_verified`のみが更新する認可済み世代）。`active.candidate_generation`は未認可で不許可 | release_auto_state.py L367実装済み。VERIFIED終端のみ前進の単調field |
| 6 | W06境界 | M3は入口Q適用＋独立slot起動枠のみ。drain/retire/start観測はM4（prepare/drain）へ委譲 | 09のM4「prepare/drain…停止予算」と04 W06の機能が対応。M3完了条件は「適格性未確認ならauto不可」でdrain実装を要求しない |
