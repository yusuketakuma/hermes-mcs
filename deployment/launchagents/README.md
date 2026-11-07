# スケジューリング構成

`runtime_mode=standalone`では`ai.mcs.standalone`の単一hostが下表と同じ7定期ジョブ、
cmd/cmd_int取込、抽出worker3本と接続を所有する。Hermes cronや個別MCS LaunchAgentは
併用しない。LLMとrepo外復旧watchdogは別のnative serviceを維持する。
導入・切替は[独立モードの手順](../../docs/guides/STANDALONE.md)を参照。
以下のHermes cron構成と配置先は`runtime_mode=hermes`（未指定の既存設定）のもの。

収集ジョブは **hermes cron（標準スケジューラ）7件 + launchd 6件** の
ハイブリッド。別途、LLM サーバ常駐用の `ai.mcs.llamaserver.plist`
（KeepAlive サーバであってジョブではない。install.sh 所有・stage 4 で
配置）が同ディレクトリにある。

下表がジョブ一覧の唯一の表（`docs/guides/INSTALLATION.md` §6 はここを参照する）。
正本はコード — hermes cronは `mcs/core/scheduled_jobs.py` の `CRON_JOBS`、
services所有のlaunchdは `mcs/ops/services/service_contract.py` の `AGENT_LABELS`。変更時は両方を合わせる。

| ジョブ | スケジュール | 実行系 |
|---|---|---|
| 未読チェック `run_check.py --json --download-files --mark-read` | `*/10 * * * *`（24時間） | hermes cron (`mcs_check.sh`) |
| 保存済み添付のOCR・ローカル解析 `mcs_ocr.py --run` | `* * * * *` | hermes cron (`mcs_ocr.sh`) |
| ヘルス監視 `health_watch.py` | `*/5 * * * *` | hermes cron (`mcs_health.sh`) |
| durable-job drain `run_check.py --json --jobs-only` | `10,40 * * * *` | hermes cron (`mcs_deep.sh`) |
| 失敗ジョブの bounded retry 登録 | `0 */6 * * *`（解析は常駐worker） | hermes cron (`mcs_llm_catchup.sh`) |
| llama-server 再起動（idle待ち・最大15分） | `0 4 * * *` | hermes cron (`llamacpp_restart_if_idle.sh`) |
| 更新チェック `mcs_update.py check` | `10 5 * * *` | hermes cron (`mcs_update.sh`) |
| 更新中断の復旧 `mcs_recover.py --if-stale` | 15分間隔 | launchd `org.mcs.recovery`（独立・install.sh 所有） |
| コマンド取込 `run_check.py --json --download-files --mark-read` | `data/cmd/` WatchPaths（イベント駆動） | launchd `local.mcs-cmd` |
| 対話カードコマンド `run_check.py --json --commands-only` | `data/cmd_int/` WatchPathsに加え、StartInterval/ThrottleIntervalを5秒に設定（通常のサービス再配置で反映） | launchd `local.mcs-int` |
| ヘルス監視の予備 `health_watch.py` | 300秒間隔（hermes cron が詰まっても警報する。flock・重複抑止は cron 側と共有） | launchd `local.mcs-health` |
| 抽出・semantic/QC 常駐drainer（レーン2） | KeepAlive・24時間・idle poll 120s | launchd `ai.mcs.extract-drainer` |
| 抽出・semantic/QC 常駐drainer（レーン3） | KeepAlive・24時間・idle poll 120s | launchd `ai.mcs.extract-drainer-2` |

MCSサーバーの負荷対策として、定期的なMCSアクセスを5分から10分間隔へ変更した。
ヘルス監視はローカルの保管状態を確認するため5分間隔を維持する。
添付OCRの毎分ジョブも保存済み資料だけを読み、MCSサーバーへ取りに行かない。
実行中のOCRジョブがあれば重ねず、次回に処理を引き継ぐ。

`runtime_mode: "standalone"`では同じ時刻の7ジョブを単一hostが実行し、
wrapperは`~/.mcs/scripts/`、Pythonは`~/.mcs/venv/bin/python3`になる。
通知接続の有無にかかわらず`ai.mcs.standalone`を配置し、個別のcalendar agentは登録しない。
モード変更時はmanifestの所有ジョブを照合する。Hermesへ戻す際は新cronの検証後に
以前の独立サービスを退役させる。詳細は[独立モードの手順](../../docs/guides/STANDALONE.md)。

wrapperスクリプトの正本は `deployment/scripts/`（`__PYTHON__`/`__REPO__`/`__DATA__`/`__RUN_LOG_HELPER__`
プレースホルダ付き）。実機の `~/.hermes/scripts/` へは下記の生成処理で
生成する — 直接編集すると repo との drift になる。

## セットアップ

**自動化（推奨）**: `~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py services` が下記の
手順をすべて実行する（冪等・`--dry-run` で確認可）。launchd 5件の
bootstrap と hermes cron 7件の登録は既存分をスキップする。plist 本文・
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
エラー表示）/ `doctor --probe services`（サービス診断の状態とエラー・警告件数）。
`doctor`単独ではLLM・サービス・認証情報・データを調べず、未確認の範囲は
`not_checked`と表示する。

独立モードの`services`は`~/.mcs/venv/bin/python3`・`~/.mcs/scripts/`で描画し、
単一host serviceを登録する。稼働中はnative hostを強制停止せず、処理完了後の
再起動を要求する。mode切替は旧manifest所有のMCS cron/agentだけを停止し、
停止を検証できなければ新serviceを起動しない。手動登録や共有gatewayは対象外。

**復旧 watchdog（install.sh が別系統で所有）**: `org.mcs.recovery`
は hermes cron に乗らない独立 launchd agent（StartInterval 900、
選択した独立Pythonと検証済みのログhelperを通して `~/.mcs-recovery/mcs_recover.py --if-stale` を起動）。
既定（`/opt/homebrew/bin/python3.14`・`/usr/local/bin/python3.14`・`/usr/bin/python3.14`の順で最初に実行できるもの）も含め、Python 3.14以上とSQLiteのWAL修正を
事前に検証する。必要なら`install.sh --recovery-python /absolute/path/to/python`
または`mcs setup init --recovery-python /absolute/path/to/python`で
`recovery_python`を明示する。更新対象のcheckoutやvenvに依存するPythonは使えない。
gateway死亡・新版破損でも動くことが目的のため、通常のmanifest所有対象とは別系統にする。
`services`は未配置のwatchdogを有効化しない。明示選択と配置済みPythonが異なる場合だけ、
所有・停止状態・ロックを検証して配置を修正する。稼働中や所有を検証できない場合は修正せず終了する。
手動の状態確認は公式wrapperの `mcs recover --status` を使う。
配置済みplistから独立Pythonと、ログhelperを使う場合の配置・保存先を検証し、
確認できなければ停止する。復旧対象の checkout は install.sh が
`~/.mcs-recovery/repo_path` に記録する（無ければ `~/.mcs`）。

配置内容を確認してから適用する場合:

```bash
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py services --dry-run
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py services
```

（mcs_setup の実行は Python ≥3.14 必須。`/usr/bin/python3` は 3.9 系で動かない —
install.sh が作る venv インタプリタを使う）

配置先パスは shell・XML ごとにエスケープしてテンプレートへ埋め込むため、
空白や記号を含むパスでも上記の生成処理を使う。

残る2件は install.sh が初回配置し、置換規則も異なる:

- `org.mcs.recovery` — `__RECOVERY_PYTHON__`/`__RECOVERY__`/`__DATA__`/`__RUN_LOG_HELPER__` を置換
  （install.sh stage 6、明示したPython選択の差分だけは上記の条件でservicesが修正）
- `ai.mcs.llamaserver` — `__LLAMA_BIN__`/`__MODEL__`/`__HERMES_HOME__`/`__PYTHON__`/`__RUN_LOG_HELPER__`/`__DATA__` を
  置換（install.sh stage 4。実機で hermes 管理の `ai.hermes.llamacpp`
  が既存なら導入自体を skip）

この2件の配置は `install.sh` の該当 stage を使用する（XML エスケープを含む）。

| プレースホルダ | 置換者 | 例 |
|---|---|---|
| `__PYTHON__` | `mcs_setup.py services`・install.sh（LLM） | `/Users/you/.hermes/hermes-agent/venv/bin/python` |
| `__REPO__` | `mcs_setup.py services` | このリポジトリの checkout パス（例 `/Users/you/hermes-mcs`） |
| `__DATA__` | `mcs_setup.py services`・install.sh（LLM/recovery） | データ dir（例 `/Users/you/.mcs/data`） |
| `__RUN_LOG_HELPER__` | `mcs_setup.py services`・install.sh | 復旧ツール dir内の独立ログhelperの絶対パス（計画時は配置予定、実配置時は配置・hashを検証） |
| `__RECOVERY__` | install.sh・mcs_setup services（配置済み監視の修正時） | 復旧ツール dir（`~/.mcs-recovery`） |
| `__RECOVERY_PYTHON__` | install.sh・mcs_setup services | 更新対象から独立した、検証済みPythonの絶対パス |
| `__LLAMA_BIN__` | install.sh | llama-server バイナリ（例 `/opt/homebrew/bin/llama-server`） |
| `__MODEL__` | install.sh | モデル gguf（例 `~/.hermes/models/Qwen3.5-9B-Q4_K_M.gguf`） |
| `__HERMES_HOME__` | install.sh | runtime home（Hermesは`~/.hermes`、standaloneは`~/.mcs`） |

> 注意: パス変更時は plist の ProgramArguments と cron wrapper の双方を
> 更新すること（`adapter/` → `mcs/` 移動時に実機 plist が旧パスで失敗した実績あり）。
> launchd 直管理だった `local.mcs-check`/`local.mcs-deep` は 2026-09 に
> hermes cron へ移行済み（実行履歴・incident が `hermes cron runs` に残る）。

## 3レーン構成

llama-server は `-c 49152 -np 3`（各枠16384 tokens）。`id_slot` は0〜2の
物理枠を示す。JST 08:00〜20:00は新着用に1枠を空け、バックグラウンド処理は
残り2枠を使う。20:00〜08:00は3枠をバックグラウンド処理に使える。
夜間に新着が待ち始めたら、次に空いた枠を優先して割り当てる。実行中の処理は
中断せず、新着の処理中も残り2枠ではバックグラウンド処理を継続する。
全枠でGPUを共有するため、3枠は3倍の処理速度を意味しない。

- `ai.mcs.extract-drainer`: `--all --workers 1 --lane 2 --semantic`
- `ai.mcs.extract-drainer-2`: `--all --workers 1 --lane 3 --semantic`
- `ai.mcs.extract-drainer-night`: `--all --workers 1 --lane 1 --semantic`

前の2本は24時間動き、夜間用workerは昼間に新しい処理を始めない。
抽出とsemantic/QCの交互処理、アクティブ患者とアーカイブ済み患者の公平な選択は
既存の経路を使う。空のキューは5秒から最大30秒まで待機間隔を延ばし、既存の
再試行予定時刻が近ければ短縮する。処理できる項目があればこの待機を入れない。
クラウドによる患者コンテキスト作成はレーン2が所有する別の子プロセスで進める。
クラウドの応答待ちはローカル枠を占有しない。クラウドがoffのときの患者コンテキスト
作成はレーン3で継続し、対象判定は`patient_context_refresh.run_due`に従う。

各要求は`data/llm_slot_0.lock`〜`llm_slot_2.lock`のうち1つを保持する。
新着の待機は`llm_realtime.lock`、短い割当処理は`llm_schedule.lock`で調整する。
既存の`llm_turn.lock`は共有保持し、旧workerの排他保持が終わるまで新方式を開始しない。
同じMCS rootを使う新方式の要求同士では、同一物理枠へ同時送信しない。
任意の外部クライアントや別rootまでこの排他で制御できるとはみなさない。

任意のadmission brokerは既定OFFのまま。ONで有効なepochを持つ場合は、
要求の物理枠をpermitへ記録する。旧形式の枠未記録permitが残る間は全枠を保留し、
新形式の送信結果が不明なpermitはその枠を保留する。期限やPID消失だけでは解放しない。
brokerがOFFのときの通信失敗後の確認・終了判定は従来どおりで、backendに残った要求の
終了証明を新たに得る仕組みではない。実機容量と処理速度は各環境で確認する。

抽出は既存のclaim lease、semantic/QCはジョブ別flockで重複実行を防ぐ。
DB更新時はrun.lockを保持し、LLM・Jevの通信中だけ解放する。通信後はロックを
再取得し、世代・原資料・設定を再検証する。常駐workerの再取得待ちは3秒で、
間に合わない完了結果は次の周回へ持ち越して保存する。原文が変わった分と
claim leaseの残りが2分を切った分は解放して破棄する。長い推論中も10分取込みを継続する。
新着取込みは今回の投稿とarrival-seeded semantic jobを優先する。全抽出を新着扱いにはせず、
時間内に処理できない投稿は既存の保存済みキューへ残す。

`mcs_llm_catchup.sh` は6時間ごとの再試行登録のみ。失敗後6時間、同じ入力
最大3回、1回の登録で抽出30件・semantic20件という既存の上限は維持する。
LLMの処理をこのcronで起動したりRTへ貸与したりしない。

`local_llm.SLOT_COUNT=3`、`ROLLBACK_SLOT_COUNT=2`。範囲外のid_slotを
送るとunpinned扱いになるため、既存の範囲チェックは維持する。
背景CLIは `--lane 1|2|3` を許可し、旧 `--slot` 指定のworkerは引数エラーで起動しない。サービス同期で旧
`ai.mcs.extract-drainer-rt` を停止・削除し、新ラベルへ置き換える。
サーバの正本は `ai.mcs.llamaserver.plist`。既存の実機ラベルが
`ai.hermes.llamacpp` の場合は、明示適用時にこのテンプレートのLabelを
既存ラベルへ置換して描画し、idleを確認して再ロードする。install.sh は
既存のHermes管理サーバを自動で置き換えない。

## llama-server 再起動ガード

`llamacpp_restart_if_idle.sh` は `/slots` の **`is_processing`** フィールド
を見る。空でないslot一覧と各要素のboolean値を検証し、空・欠落・不正応答は
idleとみなさない。10秒毎・最大15分、単調時計で区切ってidleを待つ。
busyまたは状態不明のまま期限を迎えた場合は、その日の再起動を見送る。
状態確認は`127.0.0.1`へ直接接続し、環境のプロキシ設定とリダイレクトを使わない。
1回の取得は5秒・64KiB以内に制限する。idle確認とkickstartは原子的な操作ではなく、
確認直後に新しい要求が到着する短い競合窓は残る。

kickstart 対象のラベルは機で異なるため、スクリプトが loaded な方を
選ぶ: `ai.hermes.llamacpp`（hermes 管理・実機）を先に試し、
未ロードなら `ai.mcs.llamaserver`（repo テンプレート由来）に fallback。
どちらもロードされていなければ（`--no-llm`・自前サーバ運用）再起動を
skip してログに記録し exit 0 — 日次 cron の失敗にはしない。
`launchctl kickstart -k` が失敗した場合はその終了コードで非 0 終了し、
失敗行を出力する。

## ollama（embedding）

`OLLAMA_KEEP_ALIVE=2m` を ollama の LaunchAgent plist に設定し、
embedding model の常駐を防ぐ。llama-server の decode 競合緩和。
