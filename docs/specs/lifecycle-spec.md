# MCS ライフサイクル統合仕様書

初期インストール・更新・バックアップ・復旧・エラー時自動メンテナンスを
1つのライフサイクルとして規定する。各機能の詳細設計は個別文書に
委ねる（更新機構は `docs/dev-records/auto-update-plan.md`）。

凡例: ✅実装済み / 📋計画（未実装・auto-update-plan.md 参照）/
🔶将来拡張

**2026-09-24 時点**: 更新機構（§5・§7 の📋項目の大部分）は実装済み。
コード上の📋は「設計は確定・実装済みだが本機へのロールアウト
（install.sh 再実行・`update.mode` 設定）は未実施」を意味する。
auto 有効化（§11 P3）は人の判断事項として残る。

**2026-09-25 更新**: `git reset` 後に更新差分と同名の未追跡ファイルを
追加で自動削除する処理は、updater/recovery ともに廃止した。以下の
「外科的削除」は旧設計の記録であり、現行手順には含めない。
詳細は `docs/dev-records/auto-update-plan.md` 冒頭を参照する。

## 1. 対象範囲と非目標

対象: `~/.mcs`（git checkout 兼 runtime）+ `~/.hermes`（plugin
link・scripts・profile config・.env）+ launchd + hermes cron が構成する
**このマシンの稼働系**の、導入から更新・障害復旧までの全局面。

`runtime_mode: "standalone"`（[STANDALONE.md](../guides/STANDALONE.md)）では `~/.hermes` を
使わない。`ai.mcs.standalone`の単一hostが6定期ジョブ（wrapperは`~/.mcs/scripts`、
3600秒上限）・cmd/cmd_int取込・抽出worker2本を所有する。インタプリタは`~/.mcs/venv`。
更新marker中は背景処理を停止し、updater以外の子処理が完了したことをheartbeatで確認する。
更新後のhost再起動はgeneration付き要求を発行し、updaterを含む処理の完了後に行う。
復旧ツールは同venvでservicesを再同期し通知を`mcs_standalone send`で送る。
`deployment/requirements-standalone.txt`が変わるタグは自動適用しない
（`standalone_requirements_changed`）。venvが使えない場合は
`standalone_interpreter_unavailable`、独立入口を持たないタグへの更新・ロールバックは
`standalone_runtime_missing_in_target`で止まる。旧worktree版のcalendar agentの
自己再読込helperは互換用途で保持する。

非目標: hermes-agent 自体の更新（install.sh pin は手動）、brew 等の
依存物の自動更新、他マシンへの展開、MCS サーバ側への書き込み操作。

## 2. 不変条件（全フェーズ共通の安全原則）

| # | 原則 | 具体化 |
|---|---|---|
| I1 | stdlib-only コア | 新規外部依存を足さない（GitHub API も urllib） |
| I2 | 冪等 | install.sh/services/init は再実行で収束。差分のみ適用 |
| I3 | atomic write | 状態ファイルは tmp+`os.replace`、DB backup は検証後 publish |
| I4 | flock 排他 | ledger 書込みは `run.lock`、更新系は `update.lock`📋。順序は update.lock → run.lock |
| I5 | 人承認ゲート | MCS 書込み・要求操作・破壊的復元は人経路のみ |
| I6 | fail-closed | 検証不能な入力は受理しない（ops スキーマ・tag 形式・receipt 冪等） |
| I7 | 静かな健全性 | 正常時は出力しない（watchdog 慣例: stdout は失敗警報のみ） |
| I8 | データ消失禁止 | 既読化は snapshot 検証後、削除通知・復元上書きの罠を避ける |
| I9 | 秘密の分離 | secret は argv/設定ファイルに載せない（env/getpass → Keychain/.env） |

## 3. コンポーネントとストアの地図

```
GitHub Releases (tag) ──検出──> mcs_update.py 📋
                                     │
./install.sh → brew/hermes/plugin/llm/services/recovery   [導入]
mcs_setup.py init → config.json / Keychain / .env / hermes plugin settings
mcs_setup.py services → ~/Library/LaunchAgents/* + ~/.hermes/scripts/* + cron
mcs_setup.py check → 検証ゲート
                                     │
~/.mcs/data/                         [runtime・gitignore]
  ledger.db            原本 SQLite（唯一の正本）
  backups/             ledger-YYYYMMDD.db（検証済・保持7）+ preupdate-*📋
  snapshots/           読取専用 snapshot（plugin が読む）
  run.lock / update.lock📋           flock 排他
  health.json          サブシステム健全状態（F20 契約）
  cmd/ cmd_int/ cmd_results/         コマンド inbox・結果
  discord_render/ discord_state/     カード manifest・state
  update_state.json📋  stages ジャーナル・試行履歴・rollback 情報
  update_in_progress.marker📋  quiesce 中の補助 launcher 沈黙フラグ
  service_manifest.json📋 services が管理した script/agent/cron の配置・登録内容
~/.mcs/.env                          MCS_PASSWORD・TYPESAFE_API_KEY
~/.mcs-recovery/mcs_recover.py📋     repo 外の凍結復旧プログラム（+.prev。復旧対象 checkout は同 dir の repo_path）
~/.hermes/.env                       DISCORD_BOT_TOKEN 等
Keychain 'mcs-adapter'               MCS パスワード
```

常駐: `local.mcs-cmd`（cmd drain）・`local.mcs-int`（card drain）・
`ai.mcs.extract-drainer{,-2}`・`ai.mcs.llamaserver`・`ai.hermes.gateway`・
`org.mcs.recovery`📋（更新 watchdog・install.sh 所有・gateway 非依存）。
定期: cron `mcs_check`（5分）・`mcs_deep`（durable drain）・
`mcs_llm_catchup`・`llamacpp daily restart`・`mcs_update`📋（日次）。

## 4. 初期インストール仕様 ✅

入口は `./install.sh` 1本（冪等・再実行安全）。`--preflight`
（別名 `--check-only`）は読取り専用の前提チェック（OK/WARN/NG +
`fix:`、NG があれば exit 1）、`--dry-run` はそれに各段の計画表示を
加える（どちらも書込みなし）。root 実行は拒否、未知オプションは
exit 2、別 checkout からの既存導入（`~/.mcs-recovery/repo_path`・
plugin symlink）への上書きは `--force-repo` 指定時のみ。

いずれかの段が失敗すると install 全体が非 0 で停止し、後続段は
実行しない。再実行で完了済みは skip、中断分（clone/checkout・venv・
pip install・モデル DL の `.part`）は続きから収束する。

| 段 | 内容 | 失敗時 |
|---|---|---|
| brew | git/python@3.13/uv/llama.cpp/Chrome(cask) | 停止（brew 不在・導入失敗） |
| hermes | pin commit clone + venv（`hermes` が PATH にあっても作成）+ `~/.local/bin/hermes` shim | 停止 |
| plugin | `~/.hermes/plugins/` symlink + `plugins enable` | 停止 |
| LLM | `:8080` 応答済 or hermes管理 plist 既存なら skip（hermes 管理 agent が未ロードなら warn で bootstrap 案内）。else モデル DL（再開可・任意 `MCS_MODEL_SHA256` 照合）+ `ai.mcs.llamaserver` bootstrap。plist 変更時は応答中なら warn で再読込を保留 | 停止 |
| services | `~/.mcs/data{,/cmd,/cmd_int}` 作成 + `mcs_setup.py services` へ委譲 | 停止 |
| recovery📋 | `mcs_recover.py` → `~/.mcs-recovery/`（旧版は `.prev`）+ `repo_path` に checkout を記録 + `org.mcs.recovery` launchd watchdog bootstrap（変更時・未ロード時のみ） | 停止 |

新規作成物は `umask 077`（所有者のみ）。完了時に `Installed. Summary:`
と次のコマンド（venv インタプリタのフルパス付き）を表示する。

残る1ステップ `mcs_setup.py init`（対話ウィザード）:

- 全 config キーを5節で網羅（Enter=現状/既定・ゲートで不要項目を skip・
  `-` で任意キー削除・`--set KEY=JSON` で非対話も可）
- `interactive=discord|slack` 選択時は公開 CLI のみで: plugin settings を
  `hermes -p <profile> config set`（serving profile・allowlist は質問）、
  Discord または Slack の token を profile .env へ（env または getpass）
- Keychain `mcs-adapter` 登録 + `.env` フォールバック併記
- `data/`・`data/cmd`・`chrome-profile/` を作成
- 対話通知の gateway 同期と、終了時の `check` を自動実行。
  初回は `services`・`check` の手動再実行は不要（未登録・drift の復旧時は別）

`services`（冪等）: `deployment/scripts/*.sh` レンダリング →
launchd 4件 bootstrap（差分は bootout+bootstrap 案内）→ cron を
script 名で dedup 登録（現行5件 + 📋`mcs_update`）→
`interactive=discord|slack` なら gateway を `status`→未 supervised なら
`install`+`start`。📋manifest 記録・reconcile・atomic render は
更新仕様で追加。

`services` は services 用インタプリタ
（`~/.hermes/hermes-agent/venv/bin/python`）が無ければ何も描画せず
exit 1。

`check`（exit 1 で失敗）: 実行基盤（services 用インタプリタ・
launchd PATH での `hermes` 解決・`org.mcs.recovery` の導入/drift/
`repo_path`/ロード・llama-server agent のロード）→ config 型検証 →
Keychain・Chrome・LLM・トークン解決・launchd 4件・gateway
supervised（discord/slack 時）→ wrapper drift の順に検証し、末尾に
`blockers (N) — fix in this order` を出す。`doctor` は環境の事実
（インタプリタ・hermes 解決先・repo・launchd 状態）を出してから
`check` を実行する。`init` は壊れた config.json では停止し、
`--yes` 時のみ `config.json.corrupt-<ts>` へ退避して続行する。

## 5. 更新仕様 📋

詳細は `docs/dev-records/auto-update-plan.md`。要約:

- **検出**: 日次 `mcs_update` cron が `git ls-remote --tags` で semver
  最大 tag を検出 → release notes を GitHub API で取得 → sanitize して
  `update_notice` outbox → `notify_system_target` へ（tag 単位 dedup）
- **適用**（stages ジャーナル駆動 — local_checks→remote_verify→
  fetch→tag_checks→backup→lock→applying→quiesce→merge→
  post_merge(services→restart→postcheck→applied)→gateway_restart→done
  を逐段記録・fsync 永続化。**`applying` は quiesce より前に書く**
  — 停止操作より先に中断マーカーを永続化）:
  ローカルチェック（clean・semver・現行 validate_config・容量・hermes）→
  `ls-remote` で remote 真値と記録 sha を照合（付替え検出）→ fetch →
  tag 検査（**保護パス非含有をルールベースで**（check-ignore+固定集合+
  casefold/NFC）・mode 検査（symlink/gitlink 拒否）・untracked 衝突・
  **スキーマ互換**・ls-tree+cat-file の一式 preflight・install.sh 差分）→
  `preupdate-*` backup+manifest 退避 → update.lock→run.lock（両保持・
  lock 下で clean+state+base_sha 再検証）→ `applying` 記録 →
  **quiesce**（`update_in_progress.marker`→drainer bootout+停止検証+
  stray sweep）→ `merge --ff-only` → **post-merge は新コードの
  subprocess で実行**（services reconcile・drainer 再起動+稼働版確認・
  事後チェック・`applied` 記録・通知）→ gateway restart は
  fire-and-forget（自己待機回避）
- **モード**: `update.mode` = `off|notify|auto`。notify の承認は
  `ops.update_apply` — **receipt commit が承認確定の境界**（receipt
  駆動キュー、`command_id` 単位の実行管理、`{tag,target_sha,base_sha}`
  ピン・`human_confirmed`/`actor`/`reason` 必須・projectless）。
  plugin の `_CONTROL_FIELDS`/`_confirm`/projectless 認可と
  `mcs_requests.validate()` は `update_apply`/`update_rollback` の
  preview/confirm・検証済みコマンドに対応済み。任意の `cmd` や
  人承認情報のない要求を受理するものではない。
  auto は猶予付き（検出→翌日 check で apply）を既定案とする
- **スキーマ互換**: 候補の `SCHEMA_VERSION` > 現行 DB の
  `user_version` なら auto は中止（旧コードが migrated DB を拒否する
  ため code-only rollback 不能）。notify は DB 復元の消失を明示して承認待ち
- **ロールバック**: 失敗/不合格/`unverifiable`（検証不能）→
  `reset --hard prev_sha` + 外科的削除（tag-fileset∩untracked）→
  （schema_bump 時のみ）`preupdate-*` DB 復元 → manifest スナップ
  ショット駆動のサービス復元（membership=recover.py、コンテンツ=
  旧コード services）→ drainer/gateway 再起動 → 通知。
  **中断復旧はジャーナル+実測（HEAD/porcelain/stale .git lock）で判定**
  （HEAD 単独では混在ツリーと区別不能 — 再現指摘済み）。
  失敗 tag は自動再試行しない
- **独立復旧経路**: `~/.mcs-recovery/mcs_recover.py`（install 時配置・
  repo 外・stdlib+git のみ — 新版コードが壊れていても git 復旧+
  manifest snapshot reconcile が実行可能）。**検出は独立 launchd
  agent `org.mcs.recovery` が担う**（StartInterval・gateway 非依存 —
  hermes cron は `~/.hermes/scripts/` 外を指せず gateway 内 scheduler
  は gateway 死亡で止まるため不適）。wrapper 自体は `exec` のみで
  fallback を持たない
- **ブートストラップ**: updater 自体は1回手動デプロイが必要。
  `ops.update_apply` の受理もそれ以降

## 6. バックアップ仕様

| 対象 | 方式 | 保持 | 状態 |
|---|---|---|---|
| ledger.db（日次） | `daily_backup`: sqlite `.backup`→tmp→`valid_mcs_db` 検証→atomic rename | 7件（`BACKUP_KEEP`） | ✅ |
| ledger.db（暗号化 offsite） | `mcs_backup.py offsite`: 静的日次 DB→gzip→固定 OS OpenSSL→encrypt-then-MAC→復号検証→公開・SHA照合 | 承認済み `max_snapshots` の容量上限。自動 prune なし | CLI・設定/health接続実装（親側検証中）。定期登録・実機反映は未完了 |
| ledger.db（更新前） | `preupdate-<ts>.db` 同パターン・別 prefix で日次ローテーションと分離 | **参照ベース**（actionable rollback 点が参照する限り保持） | 📋 |
| snapshot | `publish_snapshot`: backup→tmp→DELETE journal→verify→atomic rename。plugin/CCO の読取専用コピー | 単一 latest | ✅ |
| 添付ファイル | `prune_attachments` が古い実体を削除（行は state='pruned' で name/bytes/sha256/url 残留→再DL可） | 週ポリシー | ✅ |
| config/.env | git 外・暗号化なし。**バックアップ対象外** — 再生成は `init` | — | ✅（仕様） |
| ログ | `rotate_log`: >5MB → `.1`（1世代） | 1世代 | ✅ |

原則: バックアップは**検証しないと publish しない**（broken file は
fresh backup を塞がない・Oracle B24）。原本への復元上書きは人手のみ
（新しい原文を失う復旧は禁止）。

暗号化 offsite の入力 policy、全必須フィールド、鍵と独立 SHA receipt の
保管、端末喪失手順は [バックアップ・復元ガイド](../guides/BACKUP.md)を正本とする。
ローカル日次 DB と offsite の保持・外部保存承認は別であり、予定時刻や
grace をこの仕様から補わない。`status` は私有ローカル記録を読むだけで、
バックアップ成功を DB 整合監査・取得完全性・医療上の対応の証明にしない。

`mcs_backup.py verify` は Keychain の鍵による検証、`drill` は人が外部 escrow
から鍵を回復して検証する運用に分ける。訓練の保持コピーは平文・同意待ちであり、
終了後の cleanup とクラッシュ残留の処理はオーナーの承認方針に従う。
添付の DB 行・保存済み hash は含むが、添付実体・config/.env・Keychain・
配送 journal は bundle に含めない。件数・hash・時刻・所要時間以外の
患者情報や秘密値を共有証拠へ入れない。

## 7. 復旧仕様（故障クラス別）

| 故障 | 検出 | 回復経路 | 自動性 |
|---|---|---|---|
| セッション失効 | run exit 2 | `auto_login`（Keychain→.env 順）・失効通知 dedup | ✅自動再試行 |
| Keychain locked | `keychain_locked` status・check | .env フォールバックで継続・GUI 解除で自然治癒 | ✅（fallback） |
| 認証情報喪失 | `manual_required` | `mcs_setup init` で再登録 | 人手 |
| 収集run 重複/クラッシュ | run.lock / 次回 tick | 次回 run が差分継続（cursor/attempts は durable） | ✅自己治癒 |
| 通知配送失敗 | outbox state | `next_try` リトライ（3600s）・永久不可は `outbox_hold` | ✅ |
| カード配送クラッシュ | claim 残存 | `notify_transport` の claim 回収・`ops.card_resolve` | ✅/人手 |
| apply 中断📋 | `applying`/`stages` 残存 or watchdog stale 検出 | stages+HEAD+porcelain+stale `.git` lock のジャーナル判定 → reset+外科削除 / 段階追走 / merge --abort / 人へ。repo 外の `mcs_recover.py` が新版破損時も起動可能 | ✅+人手 |
| apply 不合格・検証不能📋 | 事後差分 check / `unverifiable` | 自動 rollback（reset+外科削除+（schema_bump 時）DB 復元+snapshot reconcile+restart）→ 通知 | ✅ |
| DB 破損 | `valid_mcs_db` 失敗 | backups/ から人が復元（原本上書き禁止・別配置で検証後） | 人手のみ |
| 端末喪失・暗号化 offsite からの回復 | 独立 SHA receipt・MAC・schema/件数・鮮度の検証 | 外部 escrow 鍵を明示 FD で入力→`drill`→`restore`。新規私有配置限定、DBより先に `awaiting_consent` を保持 | 人手。配置後も稼働再開しない |
| schema_bump 更新📋 | 事前互換判定 | auto 中止。notify 承認時のみ適用し、rollback は DB 復元（消失を明示） | 人手 |
| updater 自身の破損📋 | watchdog 定期検出（launchd `org.mcs.recovery`）/ repo import probe 失敗 | `~/.mcs-recovery/mcs_recover.py` が git 復旧+manifest snapshot reconcile+旧コード services を実行 | ✅ |
| quiesce 中 crash📋 | `applying` 残存（停止操作前に記録済み） | recover が判定表で復帰 — drainer 恒久停止を防ぐ | ✅ |

上表の `.env` fallback は既存ログイン認証の経路であり、backup の専用
`mcs-backup` service には適用しない。新端末 restore は
サービス起動・配送照合・通知・原本置換を実行しない。
`awaiting_consent` は通常の reconcile でも解除しない。
既存 `ops.restore_approve` の人承認・reason・receipt は更新/rollback の
損失報告に束縛され、offsite の配置後 hold の汎用解除ではない。
新端末の採用・承認・照合は別の確定した契約が必要で、
[ガイドの停止点](../guides/BACKUP.md#consent)までは hold を維持する。
失われた journal の配送証拠を暗号化 DB で補えたとは主張しない。

## 8. エラー時自動メンテナンス仕様

`health.json` がサブシステム状態を公開（`collection`/`notify`/
`semantic`/`extract_qc`/`cards`…）。各状態の自動応答:

`health_watch.py` は24時間同じ `health.tick_interval_s`（既定300秒）と
`health.max_missed_runs`（既定2回）から古い `health.json` を判定する。
判定基準時刻は未読収集が最後に完了した
`unread_at`（無い旧形式では `at`）で、未読収集をしない `--jobs-only` の deep
実行が `at` を更新しても停止した未読チェックを隠さない。

| 状態 | 自動応答 | 実装 |
|---|---|---|
| collection=incomplete | mcs_check.sh の stdout 警報行（watchdog）+ 次回 tick で再試行 | ✅ |
| notify pending/failed | outbox リトライ + 閾値超過で hold・system 通知 | ✅ |
| extract drainer 滞留 | 背景2枠の常駐 drain・6時間ごとの bounded retry 登録 | ✅ |
| llama-server 停滞/異常 | `llamacpp daily restart` 04:00・最大15分 idle を待ち、busy 継続時も再起動 | ✅ |
| コマンド inbox 滞留 | `local.mcs-cmd`/`local.mcs-int` WatchPaths 常駐 + receipt 冪等で再送安全 | ✅ |
| apply_failed📋 | tag 単位で記録・自動再試行せず人の再承認待ち | 📋 |
| 更新 available📋 | `update_notice` 通知（dedup）+ `status` で詳細 | 📋 |

原則: **自動応答は「安全側の再試行・隔離・通知」に限定**。破壊的な
自己修復（DB 上書き・強制 checkout・MCS 書込み）は自動化しない。

## 9. 通知・可観測性

| チャンネル | 用途 |
|---|---|
| `notify_target`（カード/テキスト） | 患者コンテンツ・シグナル・依頼 |
| `notify_system_target`（無ければ notify_target） | セッション失効・run 失敗・更新📋・復旧 |
| `run.log`/`extract_drain*.log`/`semantic_drain.log` | 全実行ログ（rotate で bounded） |
| `health.json` | 機械可読な健全状態（外部監視・警報判定の正本） |
| `update_state.json`📋 | 更新機構の状態・試行履歴・rollback 情報 |

## 10. 責任分界

**自動でよい**: 検出・通知・冪等再配置・検証済 backup・再試行・
ff-only merge・検証済 rollback・ロック待ち・dedup・quiesce/再起動・
manifest reconcile・中断復旧の git 復旧部分（`mcs_recover.py`）。

**人承認が必須**: MCS への一切の書込み（既読化は snapshot 検証を
通る定期経路のみ）、要求の作成/更新（`--confirm-human`+receipt）、
ops コマンド経由の apply/rollback 指示、破壊的 DB 復元
（schema_bump 更新の rollback 含む）、`update.mode` の auto 化、
分類不能な repo 状態への介入。

**自動化しない**: GitHub への push/tag・リリース作成、hermes-agent
の pin 変更、秘密情報の repo への書き込み。

## 11. 導入順序

1. ✅ install.sh + init + services + check（現行）
2. 📋 P1: `mcs_update check`+通知（読取専用・リスクゼロ）
   + `~/.mcs-recovery/mcs_recover.py` + `org.mcs.recovery` watchdog
   の install 配置 + `service_manifest.json` の記録開始（P2 の前提）
3. 📋 P2: apply/rollback/`ops.*` コマンド（receipt 駆動 +
   plugin/`mcs_requests` の受理経路変更）+ stages ジャーナル +
   quiesce（marker+stray sweep）+ manifest reconcile +
   `preupdate-*` backup — **障害注入テスト（各段階 kill・混在ツリー・
   ignored 追跡・annotated tag・receipt 失敗・quiesce 中 crash・
   catchup respawn・`.gitattributes` 潜脱）を先行して全通過させる**
4. 📋 P3: `update.mode` 既定値決定・auto 有効化判断（猶予期間含む）
5. 🔶 tag 署名 + `require_signed_tag`・リリース manifest（スキーマ
   互換の明示宣言）・カードへの更新ボタン
