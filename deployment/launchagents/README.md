# スケジューリング構成

`runtime_mode=standalone`では`ai.mcs.standalone`の単一hostが下表と同じ6定期ジョブ、
cmd/cmd_int取込、抽出worker2本と接続を所有する。Hermes cronや個別MCS LaunchAgentは
併用しない。LLMとrepo外復旧watchdogは別のnative serviceを維持する。
導入・切替は[独立モードの手順](../../docs/guides/STANDALONE.md)を参照。
以下のHermes cron構成と配置先は`runtime_mode=hermes`（未指定の既存設定）のもの。

収集ジョブは **hermes cron（標準スケジューラ）6件 + launchd 5件** の
ハイブリッド。別途、LLM サーバ常駐用の `ai.mcs.llamaserver.plist`
（KeepAlive サーバであってジョブではない。install.sh 所有・stage 4 で
配置）が同ディレクトリにある。

下表がジョブ一覧の唯一の表（`docs/guides/INSTALLATION.md` §6 はここを参照する）。
正本はコード — hermes cron は `mcs/ops/mcs_setup.py` の `CRON_JOBS`、
services 所有の launchd は同 `AGENT_LABELS`。変更時は両方を合わせる。

| ジョブ | スケジュール | 実行系 |
|---|---|---|
| 未読チェック `run_check.py --json --download-files --mark-read` | `*/10 * * * *`（24時間） | hermes cron (`mcs_check.sh`) |
| ヘルス監視 `health_watch.py` | `*/5 * * * *` | hermes cron (`mcs_health.sh`) |
| durable-job drain `run_check.py --json --jobs-only` | `10,40 * * * *` | hermes cron (`mcs_deep.sh`) |
| 失敗ジョブの bounded retry 登録 | `0 */6 * * *`（解析は常駐worker） | hermes cron (`mcs_llm_catchup.sh`) |
| llama-server 再起動（idle待ち・最大15分） | `0 4 * * *` | hermes cron (`llamacpp_restart_if_idle.sh`) |
| 更新チェック `mcs_update.py check` | `10 5 * * *` | hermes cron (`mcs_update.sh`) |
| 更新中断の復旧 `mcs_recover.py --if-stale` | 15分間隔 | launchd `org.mcs.recovery`（独立・install.sh 所有） |
| コマンド取込 `run_check.py --json --download-files --mark-read` | `data/cmd/` WatchPaths（イベント駆動） | launchd `local.mcs-cmd` |
| 対話カードコマンド `run_check.py --json --commands-only` | `data/cmd_int/` WatchPaths（イベント駆動） | launchd `local.mcs-int` |
| 抽出・semantic/QC 常駐drainer（slot 0） | KeepAlive・24時間・idle poll 120s | launchd `ai.mcs.extract-drainer` |
| 抽出・semantic/QC 常駐drainer（slot 2） | KeepAlive・24時間・idle poll 120s | launchd `ai.mcs.extract-drainer-2` |

`runtime_mode: "standalone"`では同じ時刻の6ジョブを単一hostが実行し、
wrapperは`~/.mcs/scripts/`、Pythonは`~/.mcs/venv/bin/python3`になる。
通知接続の有無にかかわらず`ai.mcs.standalone`を配置し、個別のcalendar agentは登録しない。
モード変更時はmanifestの所有ジョブを照合する。Hermesへ戻す際は新cronの検証後に
以前の独立サービスを退役させる。詳細は[独立モードの手順](../../docs/guides/STANDALONE.md)。

wrapperスクリプトの正本は `deployment/scripts/`（`__PYTHON__`/`__REPO__`/`__DATA__`
プレースホルダ付き）。実機の `~/.hermes/scripts/` へは下記の置換コマンドで
生成する — 直接編集すると repo との drift になる。

## セットアップ

**自動化（推奨）**: `~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py services` が下記の
手順をすべて実行する（冪等・`--dry-run` で確認可）。launchd 4件の
bootstrap と hermes cron 6件の登録は既存分をスキップする。plist 本文・
cron スケジュールの差分は reconcile する（loaded でも内容が違えば
bootout→bootstrap、schedule 差分は `hermes cron edit`、desired 外の
所有 agent/cron は削除）。実績は `data/service_manifest.json` に記録
され、更新・ロールバックの reconcile 正本になる。
`./install.sh` からも自動で呼ばれる（stage 5）。services は全 wrapper・
plist を `~/.hermes/hermes-agent/venv/bin/python` で起動するよう描画する
ため、このインタプリタが無ければ何も描画せず exit 1 で止まる
（`./install.sh` の再実行で stage 2 が venv を作り直す）。
描画される `__REPO__` は checkout の絶対パスなので、checkout を移動したら
新しい場所で `./install.sh` を再実行する（`docs/guides/INSTALLATION.md` §A-7）。
配置・ロード状態の確認は `mcs_setup.py check`（drift・未ロードを
エラー表示）/ `doctor`（各 label の loaded/not loaded 一覧）。

独立モードの`services`は`~/.mcs/venv/bin/python3`・`~/.mcs/scripts/`で描画し、
単一host serviceを登録する。稼働中はnative hostを強制停止せず、処理完了後の
再起動を要求する。mode切替は旧manifest所有のMCS cron/agentだけを停止し、
停止を検証できなければ新serviceを起動しない。手動登録や共有gatewayは対象外。

**復旧 watchdog（install.sh が別系統で所有）**: `org.mcs.recovery`
は hermes cron に乗らない独立 launchd agent（StartInterval 900、
`/usr/bin/python3` で `~/.mcs-recovery/mcs_recover.py --if-stale`）。
gateway 死亡・新版破損でも動くことが目的のため services の所有・
reconcile 対象外。手動実行は `/usr/bin/python3 ~/.mcs-recovery/mcs_recover.py`
（`--status` で状態診断）。復旧対象の checkout は install.sh が
`~/.mcs-recovery/repo_path` に記録する（無ければ `~/.mcs`）。

配置内容を確認してから適用する場合:

```bash
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py services --dry-run
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py services
```

（mcs_setup は Python ≥3.10 必須。`/usr/bin/python3` は 3.9 系で動かない —
install.sh が作る venv インタプリタを使う）

配置先パスは shell・XML ごとにエスケープしてテンプレートへ埋め込むため、
空白や記号を含むパスでも上記の生成処理を使う。

残る2件は install.sh 所有で置換規則も異なる（services の reconcile 対象外）:

- `org.mcs.recovery` — `__RECOVERY__`/`__DATA__` を置換（install.sh stage 6）
- `ai.mcs.llamaserver` — `__LLAMA_BIN__`/`__MODEL__`/`__HERMES_HOME__` を
  置換（install.sh stage 4。実機で hermes 管理の `ai.hermes.llamacpp`
  が既存なら導入自体を skip）

この2件の配置は `install.sh` の該当 stage を使用する（XML エスケープを含む）。

| プレースホルダ | 置換者 | 例 |
|---|---|---|
| `__PYTHON__` | `mcs_setup.py services` | `/Users/you/.hermes/hermes-agent/venv/bin/python` |
| `__REPO__` | `mcs_setup.py services` | このリポジトリの checkout パス（例 `/Users/you/hermes-mcs`） |
| `__DATA__` | `mcs_setup.py services`・install.sh（recovery） | データ dir（例 `/Users/you/.mcs/data`） |
| `__RECOVERY__` | install.sh | 復旧ツール dir（`~/.mcs-recovery`） |
| `__LLAMA_BIN__` | install.sh | llama-server バイナリ（例 `/opt/homebrew/bin/llama-server`） |
| `__MODEL__` | install.sh | モデル gguf（例 `~/.hermes/models/Qwen3.5-9B-Q4_K_M.gguf`） |
| `__HERMES_HOME__` | install.sh | runtime home（Hermesは`~/.hermes`、standaloneは`~/.mcs`） |
| `__RUNTIME_HOME__` | mcs_setup services | runtime home（LLM再起動ログの保存先） |

> 注意: パス変更時は plist の ProgramArguments と cron wrapper の双方を
> 更新すること（`adapter/` → `mcs/` 移動時に実機 plist が旧パスで失敗した実績あり）。
> launchd 直管理だった `local.mcs-check`/`local.mcs-deep` は 2026-09 に
> hermes cron へ移行済み（実行履歴・incident が `hermes cron runs` に残る）。

## 3スロット構成

llama-server は `-c 49152 -np 3`（各枠16384 tokens）。wire slot 1 は
新着MCS解析とHermesローカル対話の共用、slot 0/2 はバックログ専用。
Hermes のローカル custom provider も `extra_body.id_slot=1` に固定する。
通常対話の provider 選択は変更しない。全枠でGPUを共有するため、
3枠への変更は3倍の処理速度を意味しない。

- `ai.mcs.extract-drainer`: `--all --workers 1 --slot 0 --semantic`
- `ai.mcs.extract-drainer-2`: `--all --workers 1 --slot 2 --semantic`

両workerは24時間、抽出1件とsemantic/QC 1件を交互に、古い未処理分から
処理する。slot 2 のworkerは可変枠: RT枠（slot 1）が処理中の間は新しい
1件を始めず（バックログは slot 0 の1枠のみ）、RT枠の空きが2分続くと
再開する（`extract_llm._ELASTIC_*`、5秒ごとに `/slots` を確認。処理中の
1件は中断しない。log に `elastic_hold`/`elastic_resume`）。抽出は既存のclaim lease、semantic/QC はジョブ別flockで
重複実行を防ぐ。DB更新時は run.lock を保持し、LLM・Jev の実際の通信中
だけ解放する。通信後はロックを再取得し、既存の世代・source・config
ゲートで結果を照合する。長い推論中も10分取込みが進む。

新着取込みは slot 1 に固定し、今回取り込んだ投稿の抽出とarrival-seeded
semantic jobを新しい順に処理する。HermesがRT枠を使用中なら新着解析は
保管済みキューに残り、後続workerが処理する。夜間間引き・時間帯制限・
RT枠の貸与・tickによる背景worker休止は廃止した。

`mcs_llm_catchup.sh` は6時間ごとの再試行登録のみ。失敗後6時間、同じ入力
最大3回、1回の登録で抽出30件・semantic20件という既存の上限は維持する。
LLMの処理をこのcronで起動したりRTへ貸与したりしない。

`local_llm.SLOT_COUNT=3`、`ROLLBACK_SLOT_COUNT=2`。範囲外のid_slotを
送るとunpinned扱いになるため、既存の範囲チェックは維持する。
背景CLIの `--slot` は0/2だけを許可する。サービス同期で旧
`ai.mcs.extract-drainer-rt` を停止・削除し、新ラベルへ置き換える。
サーバの正本は `ai.mcs.llamaserver.plist`。既存の実機ラベルが
`ai.hermes.llamacpp` の場合は、明示適用時にこのテンプレートのLabelを
既存ラベルへ置換して描画し、idleを確認して再ロードする。install.sh は
既存のHermes管理サーバを自動で置き換えない。

## llama-server 再起動ガード

`llamacpp_restart_if_idle.sh` は `/slots` の **`is_processing`** フィールド
を見る（`processing` キーは存在しない — 誤キーは常に idle 判定となり
in-flight 要求を kill する実害があった）。24/7 drainer 常駐下では
瞬間 idle が滅多に無いため、10秒毎・最大15分 poll して gap を捉える。
15分 busy 継続なら再起動を実行（高々1-2 callが失敗→drainerが自動retry）。

kickstart 対象のラベルは機で異なるため、スクリプトが loaded な方を
選ぶ: `ai.hermes.llamacpp`（hermes 管理・実機）を先に試し、
未ロードなら `ai.mcs.llamaserver`（repo テンプレート由来）に fallback。
どちらもロードされていなければ（`--no-llm`・自前サーバ運用）再起動を
skip してログに記録し exit 0 — 日次 cron の失敗にはしない。
`launchctl kickstart -k` が失敗した場合はその終了コードで非 0 終了し、
失敗行を出力する。

## ollama（embedding）

`OLLAMA_KEEP_ALIVE=2m` を ollama の LaunchAgent plist に設定し、
embedding model の常駐(~2.2GB)を防ぐ。llama-server の decode 競合緩和。
