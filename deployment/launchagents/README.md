# スケジューリング構成

収集ジョブは **hermes cron（標準スケジューラ）5件 + launchd 5件** の
ハイブリッド。別途、LLM サーバ常駐用の `ai.mcs.llamaserver.plist`
（KeepAlive サーバであってジョブではない。install.sh 所有・stage 4 で
配置）が同ディレクトリにある。

| ジョブ | スケジュール | 実行系 |
|---|---|---|
| 未読チェック `run_check.py --json --download-files --mark-read` | `*/5 * * * *`（スクリプト内で 22-06時は :00/:20/:40 のみ実行に間引き — 30分のセッション失効上限を下回るため） | hermes cron (`mcs_check.sh`) |
| durable-job drain `run_check.py --json --jobs-only` | `7,37 * * * *` | hermes cron (`mcs_deep.sh`) |
| semantic/QC 夜間drain（`MCS_LLM_SLOT=1`・slot 1 pin） | `30 22 * * *`（drain 最大55分 — cron script timeout 3600s 内に収束） | hermes cron (`mcs_llm_catchup.sh`) |
| llama-server 再起動（idle待ち・最大15分） | `0 4 * * *` | hermes cron (`llamacpp_restart_if_idle.sh`) |
| 更新チェック `mcs_update.py check` | `10 5 * * *` | hermes cron (`mcs_update.sh`) |
| 更新中断の復旧 `mcs_recover.py --if-stale` | 15分間隔 | launchd `org.mcs.recovery`（独立・install.sh 所有） |
| コマンド取込 `run_check.py --json --download-files --mark-read` | `data/cmd/` WatchPaths（イベント駆動） | launchd `local.mcs-cmd` |
| 対話カードコマンド `run_check.py --json --commands-only` | `data/cmd_int/` WatchPaths（イベント駆動） | launchd `local.mcs-int` |
| extract_llm 常駐drainer（shard 0/2・slot 0） | KeepAlive・常駐poll(120s) | launchd `ai.mcs.extract-drainer` |
| extract_llm RT貸与drainer（shard 1/2・`--lend-rt`） | KeepAlive・常駐poll(120s) | launchd `ai.mcs.extract-drainer-rt` |

wrapperスクリプトの正本は `deployment/scripts/`（`__PYTHON__`/`__REPO__`/`__DATA__`
プレースホルダ付き）。実機の `~/.hermes/scripts/` へは下記の置換コマンドで
生成する — 直接編集すると repo との drift になる。

## セットアップ

**自動化（推奨）**: `python3 mcs/ops/mcs_setup.py services` が下記の
手順をすべて実行する（冪等・`--dry-run` で確認可）。launchd 4件の
bootstrap と hermes cron 5件の登録は既存分をスキップする。plist 本文・
cron スケジュールの差分は reconcile する（loaded でも内容が違えば
bootout→bootstrap、schedule 差分は `hermes cron edit`、desired 外の
所有 agent/cron は削除）。実績は `data/service_manifest.json` に記録
され、更新・ロールバックの reconcile 正本になる。
`./install.sh` からも自動で呼ばれる。

**復旧 watchdog（install.sh が別系統で所有）**: `org.mcs.recovery`
は hermes cron に乗らない独立 launchd agent（StartInterval 900、
`/usr/bin/python3` で `~/.mcs-recovery/mcs_recover.py --if-stale`）。
gateway 死亡・新版破損でも動くことが目的のため services の所有・
reconcile 対象外。手動実行は `python3 ~/.mcs-recovery/mcs_recover.py`
（`--status` で状態診断）。

手動で行う場合の手順:

```bash
REPO=$(cd ../.. && pwd)          # このリポジトリの checkout パス
PY=$HOME/.hermes/hermes-agent/venv/bin/python
DATA=$HOME/.mcs/data

# 1. hermes cron スクリプト配置
mkdir -p ~/.hermes/scripts
for s in mcs_check mcs_deep mcs_llm_catchup llamacpp_restart_if_idle mcs_update; do
  sed -e "s|__PYTHON__|$PY|g" -e "s|__REPO__|$REPO|g" -e "s|__DATA__|$DATA|g" \
      "$REPO/deployment/scripts/$s.sh" > ~/.hermes/scripts/$s.sh
  chmod +x ~/.hermes/scripts/$s.sh
done

# 2. hermes cron 登録（--no-agent: stdout空=成功時沈黙、alert行のみ通知）
hermes cron create "*/5 * * * *" --name "MCS unread check" \
  --script mcs_check.sh --no-agent --deliver local
hermes cron create "7,37 * * * *" --name "MCS durable drain" \
  --script mcs_deep.sh --no-agent --deliver local
hermes cron create "30 22 * * *" --name "MCS LLM catchup" \
  --script mcs_llm_catchup.sh --no-agent --deliver local
hermes cron create "0 4 * * *"  --name "llamacpp daily restart" \
  --script llamacpp_restart_if_idle.sh --no-agent --deliver local
hermes cron create "10 5 * * *"  --name "MCS update check" \
  --script mcs_update.sh --no-agent --deliver local

# 3. launchd plist（services 管理の4件は同じ置換規則）
for p in local.mcs-cmd local.mcs-int ai.mcs.extract-drainer ai.mcs.extract-drainer-rt; do
  sed -e "s|__PYTHON__|$PY|g" -e "s|__REPO__|$REPO|g" -e "s|__DATA__|$DATA|g" \
      "$REPO/deployment/launchagents/$p.plist" > ~/Library/LaunchAgents/$p.plist
  launchctl load ~/Library/LaunchAgents/$p.plist
done
```

残る2件は install.sh 所有で置換規則も異なる（services の reconcile 対象外）:

- `org.mcs.recovery` — `__RECOVERY__`/`__DATA__` を置換（install.sh stage 6）
- `ai.mcs.llamaserver` — `__LLAMA_BIN__`/`__MODEL__`/`__HERMES_HOME__` を
  置換（install.sh stage 4。実機で hermes 管理の `ai.hermes.llamacpp`
  が既存なら導入自体を skip）

手動で配置する場合は install.sh の sed コマンドをそのまま使う。

| プレースホルダ | 置換者 | 例 |
|---|---|---|
| `__PYTHON__` | `mcs_setup.py services` | `/Users/you/.hermes/hermes-agent/venv/bin/python` |
| `__REPO__` | `mcs_setup.py services` | このリポジトリの checkout パス（例 `/Users/you/hermes-mcs`） |
| `__DATA__` | `mcs_setup.py services`・install.sh（recovery） | データ dir（例 `/Users/you/.mcs/data`） |
| `__RECOVERY__` | install.sh | 復旧ツール dir（`~/.mcs-recovery`） |
| `__LLAMA_BIN__` | install.sh | llama-server バイナリ（例 `/opt/homebrew/bin/llama-server`） |
| `__MODEL__` | install.sh | モデル gguf（例 `~/.hermes/models/Qwen3.5-9B-Q4_K_M.gguf`） |
| `__HERMES_HOME__` | install.sh | hermes home（`~/.hermes`） |

> 注意: パス変更時は plist の ProgramArguments と cron wrapper の双方を
> 更新すること（`adapter/` → `mcs/` 移動時に実機 plist が旧パスで失敗した実績あり）。
> launchd 直管理だった `local.mcs-check`/`local.mcs-deep` は 2026-09 に
> hermes cron へ移行済み（実行履歴・incident が `hermes cron runs` に残る）。

## extract_llm ドレーナー構成

backlog drain は **shard 分割 + slot 制御** で多重化する（2026-09 導入）:

- `ai.mcs.extract-drainer`: `--all --shard 0/2 --slot 0` — 日中も常駐し、
  5分 tick は `oldest_first` で反対側から進むため選択が重ならない。
- `ai.mcs.extract-drainer-rt`: `--all --shard 1/2 --lend-rt` — 全call前に
  `/slots` を照会し、RT slot が空いていれば借用する（polite lending）。
  RT 要求が来れば最大 1 call 分だけ queue 待ちさせる trade-off。
  `/slots` 照会失敗時は slot 0 に fallback するため、サーバ停止中も
  stall しない。
- `mcs_llm_catchup.sh`（hermes cron・22:30 起動・最大55分）:
  `MCS_LLM_SLOT=1` で `semantic_drain.py --drain` を走らせ、QC/semantic
  ジョブを深夜 window で slot 1 から消化する。`WINDOW_S=3300` は
  hermes cron の script timeout 既定 3600s 未満に収める上限 —
  超過すると毎回 kill される（tests/ops/test_deployment_scripts.py
  で固定）。extract drainer 死亡時は shard 0/2 の gap-fill を
  background で併走する。run lock は ~2 分 iteration 毎の取得なので
  定期 tick を餓死させない。残 backlog は翌晩に持ち越し。

`MCS_LLM_SLOT=<N>` はプロセス単位の wire id_slot オーバーライド
（`local_llm.request_slot()`）。T19 で選択済みのスロット数は
`local_llm.SLOT_COUNT = 2`（checked-in `-np 2`、ロールバック値は
`ROLLBACK_SLOT_COUNT = 1`）— `MCS_LLM_SLOT`・`--slot`・明示 probe
スロットはいずれも `0 <= N < SLOT_COUNT` 範囲外なら unpinned を
避けて background slot にフォールバックする（llama.cpp は範囲外
id_slot を unpinned 扱いするため）。サーバが選択数より少ない
slot を広告した場合は `mcs_setup check` がエラーとして報告し、
`service_manifest.json` の `llm_slots` に selected/rollback/plist
整合が記録される。llama-server のフラグは
`-c 65536 -np 2 --spec-type ngram-simple -fa on -ctk q4_0 -ctv q4_0
-ub 512 --cache-ram 1024`（per-slot 32768）— 同梱テンプレート
`ai.mcs.llamaserver.plist` の実値で、実機の hermes 管理ラベル
`ai.hermes.llamacpp` も同じ構成で稼働する（install.sh は既存の
hermes 管理 agent を検出してテンプレート導入を skip するため、実機の
常駐ラベルは `ai.hermes.llamacpp` のまま）。`-c 49152` は
prompt cache が slot context を埋め尽くして実行中 task が cancel
される退行が実測されたため差し戻し（2026-09-23）。

`MCS_LLM_ADMISSION=<path|1>` は全クライアント共通の RT/BACKLOG
受付境界（`mcs/core/llm_admission.py`、T20）を有効化する
プロセス単位のフラグ。**既定は off** — 実配備トポロジで
トークン無しの直接接続が拒否されることが認可付き段階的導入で
検証されるまで従来の slot pin 経路のまま。`1` は既定パス
`~/.mcs/data/llm_admission.db`、それ以外の値は broker SQLite の
パスとして解釈される。有効時は `mcs.semantic`/`mcs.extract` の全
呼出しが許可証を取得してから送信し、クラス（RT/BACKLOG）が同時に
占有することはない。接続拒否は `not_sent`、timeout/切断は
`unknown`（照合が済むまでクラスを閉じたまま）として durable に
記録される。ルート表に無い MCS 呼出し経路がある状態での有効化は
`mcs_setup check` がエラーとして報告する（blocked startup）。

## llama-server 再起動ガード

`llamacpp_restart_if_idle.sh` は `/slots` の **`is_processing`** フィールド
を見る（`processing` キーは存在しない — 誤キーは常に idle 判定となり
in-flight 要求を kill する実害があった）。24/7 drainer 常駐下では
瞬間 idle が滅多に無いため、10秒毎・最大15分 poll して gap を捉える。
15分 busy 継続なら再起動を実行（高々1-2 callが失敗→drainerが自動retry）。

kickstart 対象のラベルは機で異なるため、スクリプトが loaded な方を
選ぶ: `ai.hermes.llamacpp`（hermes 管理・実機）を先に試し、
未ロードなら `ai.mcs.llamaserver`（repo テンプレート由来）に fallback。
どちらも無ければ alert 行を出して失敗終了する。

## ollama（embedding）

`OLLAMA_KEEP_ALIVE=2m` を ollama の LaunchAgent plist に設定し、
embedding model の常駐(~2.2GB)を防ぐ。llama-server の decode 競合緩和。
