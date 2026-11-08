# ローカル処理が遅い理由の考察（2026-11-08）

この文書は、ローカル処理（個別抽出・semantic/QC・添付OCR・患者コンテキスト）が
遅い要因を、コード照合と合成probeで整理したものです。実測の性能値ではなく、
構造上の制約とその根拠を示します。原因ごとに 観測／コード根拠／反証／対策案／
優先度／受入測定／未確認 を並べます。

本番データ・実投稿・患者ID・名前・本文・生私有ログは使っていません。
測定はすべて一時ディレクトリ内の合成lockによるものです。

## 構造の前提（コード照合）

- llama-server 起動引数（`deployment/launchagents/ai.mcs.llamaserver.plist`）：
  `-c 49152 -np 3`（3枠×16384 tokens）、`-ngl 99`、`-fa on`、
  `-ctk/-ctv q4_0`、`-ub 512`、`--spec-type ngram-simple`、`--cache-ram 0`。
  `-t` 指定なし（llama.cpp既定のスレッド数）。KeepAlive・4時のidle再起動あり。
- local_llm transport（`mcs/llm/transport/local_llm.py`）：
  loopback `127.0.0.1:8080`、MAX_TOKENS=1400、TIMEOUT=90。
- extract_llm（`mcs/extract/llm/extract_llm_source_contract.py`）：
  TIMEOUT=300、MAX_TOKENS=1600。`_BATCH_K=0` でbatch既定OFF
  （拒否されたbatchは単発へ再試行）。`_BATCH_PER_ITEM_S=55.0` で
  batch内の1件あたりdecode想定55秒。
- 枠スケジュール（`mcs/llm/admission/llm_slot_schedule.py`）：
  物理3枠を flock で管理。realtime は intent/active lock で優先。
  昼（JST 08–20）の backlog は枠1,2のみ。夜（20–08）は枠0も借用可。
  旧workerの排他 `llm_turn.lock` が生きている間は新しい物理turnを
  全て待たせる（直列化）。
- 常駐drainer（`deployment/launchagents/README.md`）：
  `ai.mcs.extract-drainer`（lane2）・`ai.mcs.extract-drainer-2`（lane3）は
  KeepAlive・idle poll 120s・`--all --workers 1 --semantic`。
  `ai.mcs.extract-drainer-night`（lane1・夜用）。旧 rt drainer は撤去済みで、
  新着は10分tick経路。
- cron（`mcs/core/scheduled_jobs.py`）：unread check `*/10`、
  health `*/5`、durable drain `10,40`、OCR `* * * * *`、
  retry maintenance `0 */6`、llama restart `0 4`。
- 添付OCR（`mcs/extract/attachment_ocr.py`・`mcs/ops/mcs_ocr.py`）：
  別runtime（network禁止）の YomiToku DocumentAnalyzer **CPUのみ**。
  `cv2/torch` ともにスレッド1固定。ページは200DPI相当で描画。
  `timeout_seconds` 既定120（上限600）。drain予算900s・
  local:ocr = 3:1 の段階割当、LOCAL_CALL_S=300。
- 既存の性能道具：`scripts/evaluation/llm_lane_bench.sh`
  （-ub 128/256/512 と serial/concurrent の A/B。owner実行・
  worker/service停止が前提で今回は未実行）。

## 合成probe（この文書での測定）

`probe_slot_schedule.py`：一時ディレクトリに lock ファイルを作り、
`llm_slot_schedule.turn` の割当結果と待ち時間だけを測定。
実LLM・DB・Keychain・本番lockは使わない。deadline=0.5s。

| 条件 | 結果 | 待ち |
|---|---|---|
| backlog・全枠空 | slot 1 | ~1ms |
| backlog・slot1占有 | slot 2 | ~0.3ms |
| backlog・昼・slot1+2占有 | None | 500ms超で待ち尽くし |
| backlog・legacy排他中 | None | 500ms超で待ち尽くし |
| backlog・RT intent保持中 | None | 500ms超で待ち尽くし |
| backlog・夜・slot1+2占有 | slot 0（借用） | ~0.3ms |
| realtime・全枠空 | slot 0 | ~1ms |
| realtime・slot0占有 | slot 1 | ~1ms |

## 原因候補（評価中・断定なし）

### C1 昼間のbacklog並列度は2枠まで

- 観測：probeで昼固定・枠1+2占有時はdeadlineまで枠を取れない。
- コード根拠：`turn()` の `allowed = SLOTS if realtime or night() else (1,2)`。
- 反証：昼に枠0を開けるのは新着・対話の応答保護で、制約というより仕様。
- 対策案：夜間の借用は既にある。昼の枠0一時借用はRT優先設計と衝突するため
  要設計（実装しない）。
- 優先度：高（backlog処理速度の上限を直接決める）。
- 受入測定：昼に枠1+2占有時の完了率が上がるか、枠割当の変化と対象処理の
  完了件数を対応付けて測る。
- 未確認：実稼働での占有率（Rootの非機密集計待ち）。

### C2 1件あたりのdecode時間が長い

- 観測：`_BATCH_PER_ITEM_S=55.0` が1件55秒のdecode想定を示す
  （batch既定OFFでも推定値として残る）。TIMEOUT=300、MAX_TOKENS=1600。
- コード根拠：`extract_llm_source_contract.py`。
- 反証：推定定数であり実測ではない。実際のitem token量とdecode速度は
  Root側の非機密集計・llm timings（prompt_ms/predicted_ms）で確認する領域。
- 対策案：batch有効化（`_BATCH_K`>0）は既存仕組みだが品質確認の扱い要検証。
  出力長の上限見直しは品質影響があるため断定しない。
- 優先度：高。
- 受入測定：llama.cpp `timings` のprompt_ms/predicted_msと件数。
- 未確認：実際の1件あたり所要時間分布。

### C3 添付OCRはCPUのみ・単一スレッド

- 観測：`cv2.setNumThreads(1)`・`torch.set_num_threads(1)` 固定、
  `DocumentAnalyzer(device="cpu")`、200DPI描画、timeout既定120s。
- コード根拠：`attachment_ocr.py` の隔離runtime。
- 反証：単一スレッド化は常駐AIとのdecode競合を避ける意図的制約の可能性。
  OCRをGPU・複数スレッド化すると枠・メモリの取り合いで別の遅延を生む。
- 対策案：ページあたりのtimeout内完走率を先に見てから並列度を検討。
  一律増やさない。
- 優先度：中（添付が多い場合の律速）。
- 受入測定：添付1件・1ページあたりのOCR所要時間、timeout発生率。
- 未確認：実OCR所要時間（合成probeのみ、実runtime測定は未実施）。

### C4 旧workerとの直列化（legacy turn lock）

- 観測：probeで `llm_turn.lock` 排他中は新しい物理turnが全て
  deadlineまで待つ。
- コード根拠：`turn()` の legacy drain 部。
- 反証：旧workerの完了保護に必要な機構。legacyを持たない構成では影響なし。
- 対策案：legacy経路の残存範囲を棚卸し、移行完了すれば待ちが消える。
- 優先度：中（残存legacy経路がある場合のみ）。
- 受入測定：legacy turn保持時間と新turn開始までの遅延。
- 未確認：現行workerがlegacy lockを実際に握る頻度（Root側の観測領域）。

### C5 RT優先によるbacklogの譲り

- 観測：probeでRT intent lock保持中はbacklogが枠を取れない。
- コード根拠：`rt_waiting` 判定でbacklogは `allowed` を待つ。
- 反証：仕様どおりの優先制御。新着の応答性を守るための代償。
- 対策案：現状維持が前提。backlogの夜間消化で補う設計が既にある。
- 優先度：低〜中。
- 受入測定：RT intentの保持頻度・時間とbacklog待ちの相関。
- 未確認：実稼働でのRT intent頻度。

### C6 その他の構造的制約

- 単一GPU・単一モデル（Qwen3.5-9B・約5.7GB）で3枠のdecode共有。
  prompt評価とdecodeはGPU内で時分割され、枠ごとの並列であっても
  合計スループットは単一GPUに律速される。
- `--spec-type ngram-simple`・`-fa on`・`-ctk/-ctv q4_0`・`-ub 512` は
  既に適用済みの調整。未適用の空きは見えていない。
- 4時のidle再起動・embeddingモデルの追い出しはdecode競合緩和の
  既存対策であり、遅延対策として残置。
- transportが切れた要求は slot idle 待ちで枠を占有したままになる
  （`_wait_slot_idle`）。未知結果の重複送信を防ぐ安全機構であり、
  枠が長く塞がれる場合は遅延要因になり得る（頻度未確認）。

## 対象外・やらないこと

- 安全gateの緩和・予算の一律増・性能上げるための品質確認省略はしない。
- 実LLM・API・DB・Keychain・本番設定・再起動は行わない。
- `llm_lane_bench.sh` はworker/service停止が前提のowner向け道具であり
  この調査では実行しない。実測が必要になった場合はRoot判断で別途実施。

## 未確定領域（Root・別担当の根拠待ち）

- 実稼働の枠占有率・待ち分布・1件あたり所要時間（非機密集計）。
- context scan と枠/semantic経路の別担当調査結果。
- 上記とこの文書の候補の統合・優先度の最終決定。
