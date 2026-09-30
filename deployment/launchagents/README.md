# スケジューリング構成

収集ジョブは **hermes cron（標準スケジューラ）6件 + launchd 5件** の
ハイブリッド。別途、LLM サーバ常駐用の `ai.mcs.llamaserver.plist`
（KeepAlive サーバであってジョブではない。install.sh 所有・stage 4 で
配置）が同ディレクトリにある。

下表がジョブ一覧の唯一の表（`docs/INSTALLATION.md` §6 はここを参照する）。
正本はコード — hermes cron は `mcs/ops/mcs_setup.py` の `CRON_JOBS`、
services 所有の launchd は同 `AGENT_LABELS`。変更時は両方を合わせる。

| ジョブ | スケジュール | 実行系 |
|---|---|---|
| 未読チェック `run_check.py --json --download-files --mark-read` | `*/5 * * * *`（スクリプト内で 22-06時は :00/:20/:40 起点の各5分窓に間引き — 詳細は `docs/INSTALLATION.md`） | hermes cron (`mcs_check.sh`) |
| ヘルス監視 `health_watch.py` | `*/5 * * * *` | hermes cron (`mcs_health.sh`) |
| durable-job drain `run_check.py --json --jobs-only` | `7,37 * * * *` | hermes cron (`mcs_deep.sh`) |
| semantic/QC 夜間drain（`MCS_LLM_SLOT=1`・slot 1 pin） | `30 22 * * *`（drain 最大55分 — cron script timeout 3600s 内に収束） | hermes cron (`mcs_llm_catchup.sh`) |
| llama-server 再起動（idle待ち・最大15分） | `0 4 * * *` | hermes cron (`llamacpp_restart_if_idle.sh`) |
| 更新チェック `mcs_update.py check` | `10 5 * * *` | hermes cron (`mcs_update.sh`) |
| 更新中断の復旧 `mcs_recover.py --if-stale` | 15分間隔 | launchd `org.mcs.recovery`（独立・install.sh 所有） |
| コマンド取込 `run_check.py --json --download-files --mark-read` | `data/cmd/` WatchPaths（イベント駆動） | launchd `local.mcs-cmd` |
| 対話カードコマンド `run_check.py --json --commands-only` | `data/cmd_int/` WatchPaths（イベント駆動） | launchd `local.mcs-int` |
| extract_llm 常駐drainer（shard 0/2・slot 0） | KeepAlive・常駐poll(120s)・**稼働は 20-07 時のみ**（`--active-hours 20-07`。日中は idle） | launchd `ai.mcs.extract-drainer` |
| extract_llm RT貸与drainer（shard 1/2・`--lend-rt`） | KeepAlive・常駐poll(120s)・稼働は 20-07 時のみ | launchd `ai.mcs.extract-drainer-rt` |

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
新しい場所で `./install.sh` を再実行する（`docs/INSTALLATION.md` §A-7）。
配置・ロード状態の確認は `mcs_setup.py check`（drift・未ロードを
エラー表示）/ `doctor`（各 label の loaded/not loaded 一覧）。

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
| `__HERMES_HOME__` | install.sh | hermes home（`~/.hermes`） |

> 注意: パス変更時は plist の ProgramArguments と cron wrapper の双方を
> 更新すること（`adapter/` → `mcs/` 移動時に実機 plist が旧パスで失敗した実績あり）。
> launchd 直管理だった `local.mcs-check`/`local.mcs-deep` は 2026-09 に
> hermes cron へ移行済み（実行履歴・incident が `hermes cron runs` に残る）。

## extract_llm ドレーナー構成

backlog drain は **shard 分割 + slot 制御** で多重化する（2026-09 導入）:

- `ai.mcs.extract-drainer`: `--all --shard 0/2 --slot 0 --active-hours 20-07` —
  常駐するが backlog を流すのは 20-07 時だけ（2026-09-30: 日中の backlog
  stream が slot 0 を占有し、tick 内 semantic 段が 300-450s 待って毎 tick
  480s に達したため）。窓の外は 60s 間隔で idle し、KeepAlive・watchdog・
  updater の常駐前提はそのまま。新着の抽出は日中も tick 内 extract 段が
  担う。5分 tick は `oldest_first` で反対側から進むため選択が重ならない。
  再発防止と自動復旧は tick 側（`run_check._run_semantic`）: semantic 段は
  `data/flags/llm_yield` を立てて最大 `SEMANTIC_YIELD_WAIT_S`（45s）だけ
  slot 0 の空きを待つ。常駐 drainer（`--slot`/`--lend-rt` 経路）は call ごとに
  この flag を見て、立っている間は送信せず defer するので、tick は drainer の
  進行中 1 call 分（約 30s）待つだけで slot を得る（窓内でも tick 内 semantic
  が動く）。flag は lane 終了時に消す。待っても空かなければ `skipped: slot_busy`
  で見送るが flag は立てたままにし、drainer は進行中 call を終えて休止、次の
  tick（夜間は 20 分後）が slot を得る — 夜間の新着も朝まで持ち越さず夜のうちに
  処理する（遅れは許容、オーナー 2026-09-30）。30 分より古い flag は tick の
  異常終了の残骸として無視する。末尾処理用に `SEMANTIC_TAIL_RESERVE_S`（60s）を
  常に残し、「自分の持ち時間の半分以上を使って完了 0 件」が
  `SEMANTIC_STARVE_STREAK`（3）tick 続くと
  `SEMANTIC_HOLD_S`（1h）だけ tick 内 semantic を休止する。休止中は
  health.json の `semantic_lane.hold_until` と `overall: degraded` で見え、
  期限が来れば自動解除（運用操作は不要）。durable な semantic job は消えず、
  休止明けか夜間 catch-up で処理される。
  semantic job 側（2026-09-30 の失敗 15 件の原因: v2 fact doc は 1 生成が
  約 300s で、1 job は生成 3〜4 回。450s の job 予算を超えるたび「retry」で
  試行を 1 回消費し、進捗していても 6 回で failed になっていた）:
  `semantic_runtime.LLM_CALL_RESERVE_S`（300s）— pass 内で先の呼び出しが
  完了済みなら、残り予算がこれ未満のとき次の呼び出しを始めず
  `RuntimeBudgetShort` → `deferred`（試行は消費しない）。stage 結果は
  artifact に残るので次 tick は続きから再開し、1 tick に長い生成 1 回ずつ
  進んで完了する。run_due は `progressed`（新しい stage artifact を残した
  job 数）を返し、tick の飢餓判定は「完了も進捗もない」場合だけ数える。
- `ai.mcs.extract-drainer-rt`: `--all --shard 1/2 --lend-rt --active-hours 20-07` — 全call前に
  `/slots` を照会し、RT slot が空いていれば借用する（polite lending）。
  RT 要求が来れば最大 1 call 分だけ queue 待ちさせる trade-off。
  RT が使用中なら slot 0 が idle の場合だけ slot 0 を使い、両方使用中
  なら 1 秒間隔で再照会して空きを待つ（call の deadline まで、deadline
  無しは最大300s）。処理中の slot へ id_slot pin を送ると llama-server が
  prompt cache を処理中 slot に load して `GGML_ASSERT(n <= tokens.size())`
  で abort するため、使用中の slot 0 へは fallback しない。待ちが時間切れ
  になると slot を選ばず、その call（format probe 含む）は送信せず deferred
  として残す（attempt は消費しない）。`/slots` 照会失敗時のみ slot 0 に
  fallback する（サーバ停止中は call も失敗するため stall しない）。
- `mcs_llm_catchup.sh`（hermes cron・22:30 起動・最大55分）:
  `MCS_LLM_SLOT=1` で `semantic_drain.py --drain` を走らせ、QC/semantic
  ジョブを深夜 window で slot 1 から消化する。`WINDOW_S=3300` は
  hermes cron の script timeout 既定 3600s 未満に収める上限 —
  超過すると毎回 kill される（tests/meta/test_deployment_scripts.py
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
-ub 512 --cache-ram 0`（per-slot 32768）— 同梱テンプレート
`ai.mcs.llamaserver.plist` の実値。実機の常駐ラベルは hermes 管理の
`ai.hermes.llamacpp` のまま（install.sh は既存の hermes 管理 agent を
検出してテンプレート導入を skip する）で、2026-09-29 時点の実機は
`-c 32768` で稼働し、`--cache-ram 0` だけを揃えた。
`--cache-ram 0`（prompt cache 無効）は llama.cpp b11146 の不具合回避:
id_slot 指定のリクエストが処理中の slot に届くと、defer より前に
prompt cache の save/load がその slot の状態を差し替え、hybrid
（Qwen3.5）の ngram draft 棄却時に `GGML_ASSERT(n <= tokens.size())`
（server-common.cpp:654）で abort する（2026-09-28〜29 に3回）。
全リクエストが slot を pin する運用では cache は実質使われないため、
無効化による性能差は小さい。`-c 49152` は
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
どちらもロードされていなければ（`--no-llm`・自前サーバ運用）再起動を
skip してログに記録し exit 0 — 日次 cron の失敗にはしない。
`launchctl kickstart -k` が失敗した場合はその終了コードで非 0 終了し、
失敗行を出力する。

## ollama（embedding）

`OLLAMA_KEEP_ALIVE=2m` を ollama の LaunchAgent plist に設定し、
embedding model の常駐(~2.2GB)を防ぐ。llama-server の decode 競合緩和。
