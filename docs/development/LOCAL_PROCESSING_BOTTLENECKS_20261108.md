# ローカル処理が遅い理由の考察（2026-11-08）

この文書は、ローカル処理（個別抽出・semantic/QC・添付OCR・患者コンテキスト）が
遅い要因を整理したものです。根拠は2系統で、区別して扱います。

- **Devin側**：コード照合と合成probe（一時ディレクトリのlockのみ。
  新規推論要求・DB書込み・設定変更・再起動は不使用）。
- **Root側**：本番のpass記録ログを bounded（直近60分・末尾16MiB制限）かつ
  固定語彙の数値集計のみ読み取ったもの。ID・本文・生ログは含まない。
  参照先は私有監査 `bounded-pass-summary.json` /
  `bounded-pipeline-phases.json` / `slot-memory-window.json` と
  `oracle-answer.md`（code-only相談・本番データ未送信）。

実稼働値は「記録されたpassの合計」であり、並行workerが重なるため
壁時計時間の占有率ではありません。性能断定はせず、測定済みの事実と
未確認を分けます。

## 1. 現況表（Rootの bounded 集計・実機値）

対象：`ff038ef8`、Apple M4 / 16GiB、Qwen3.5-9B-Q4_K_M。
観測窓は 22:07:52–23:07:52 JST（60分）。機体状況：swap使用約4.26GiB、
`memory_pressure` free 22%（10%閾値はその時点で通過）、disk空き20.6GiB、
thermal警告の記録なし。

| 領域 | 実機値 |
|---|---|
| extract pass | 282 records。done 4 / failed 0 / selected 11 / lock_lost 7 / held_prior 5 / carried 5 |
| extract LLM calls | 53 = first 28 + repair 25。repair_wins 3、checkpoint_reuses 1、length_stops 0 / resplits 0 |
| extract call総和 | prompt_ms 1,302,324.6 / predicted_ms 2,548,380.2 / tokens 249,127 / prompt_n 219,169 / cache_n 5,895（repair_prompt_n 42,148） |
| semantic pass | done_by_kind = semantic 1 + extract_qc 4。lock_busy 15/25 pass。defer理由： empty_repair_candidate_binding 20、budget_short 5 |
| transport総和 | llm_transport 1,800.5s / llm_reacquire 1,334.4s / jev_transport 9.3s / jev_reacquire 142.5s（並行passの総和） |
| pass別再取得 | LLM後 run.lock 再取得 132.35 / 154.49 / 244.16 / 390.05 / 413.35 秒。transport 178–393s、reprocess 8.9–39.8s（行境界上限でhard 5秒ではない） |
| 全収集tick | 4回とも291–354.8秒。内訳 fetch 115–129s / derive 78–129s / snapshot 48–75s（verify 42–55sが支配）。run_checkは取得〜closeまでrun.lock保持（LLM区間のみunlock） |
| patient_context | 3,309 records・done 0。scan_incomplete 2,628 / waiting 681。30分窓の scan_ms 中央値716ms（min 9 / max 95,062）。waiting 324件の scanned8=305 / 7=19、中央値1,442.5ms |
| /slots 短窓 | 23:15:54–23:16:19の6回GETで3枠すべてidle（短窓のため長期稼働率ではない） |
| semantic実設定 | fact_source/loop_mode/summary_mode = shadow、assist、QC annotate、job_budget 1260。shadowは旧factsを消費しつつv2比較文書を生成する経路で、新canonical自動採用ではない |
| shadow_v2 | v2抽出段階の試行7 / incomplete 5 / 1,808.9s（再取得待ちを内包しtransportと加算不可。試行数は `semantic_fact_stages.py:156` のstageカウンタでHTTP call数ではない） |
| systemwide | vm_page deltas: Compressions 350,466 / Decompressions 304,150（他アプリとこの調査自体を含みMCS原因断定不可） |

## 2. 実機で確認した要因（優先度順）

### P1 推論後の run.lock 再取得待ち → 後続call開始前の予算不足

- 実測：再取得 132–413秒（最大413.35s）が複数passで記録され、
  該当passは `budget_short` / `deferred_short` へ。
- 機序（コード照合）：`semantic_due_selection.py:377-388` の
  `BACKLOG_COMMIT_RESERVE_S=540` と `semantic_transport.py:553`
  `LONG_MIN_CALL_S=620`。reserve=min(540, max(0, 1260-620-60))=540 で、
  transport窓は 1260-540=720 が上限（上流deadline・準備でさらに短縮）。
  後続callは `semantic_runtime.py:579-600` の `LLM_CALL_RESERVE_S=300`
  で残300秒を要求するため、推論200秒＋再取得400秒なら残120<300で
  model HTTP timeoutではなく call開始前に `budget_short` となる。
  実passの長い再取得と整合する。
- holder：各待ち時点のholder PIDは未追跡で、全てcollector原因とは断定しない。
  ただし全収集tickが291–354.8秒・run.lock保持（transportのみunlock）で、
  再取得待ちの機序と一致する。fetch/derive/snapshotの長区間が調査対象。
- 優先度：**最高**。実遅延が計測済みで、後続callの開始条件を直撃する。
- 受入測定：変更前後で reacquire分布・budget_short件数・done/wall-clockを
  同一窓で比較。RT遅延・carry滞留の悪化がないこと。
- 対策の境界：共有writer保持工程の純粋な計画部分を外へ出しcommit時に
  現在性を再検証する方向（Oracle採用順 B優先）。commit reserve・QC・
  unknown保護を削る変更・request timeoutの単純延長はこの開始条件を
  変えないため対策にならない。

### P2 repair・holdの負荷

- 実測：call 53件中 repair 25件（repair_prompt_n 42,148）。
  held_prior 5・lock_lost 7・`empty_repair_candidate_binding` 20。
- 補足：repair_wins 3 はnullable・未記録項目があり 3/25 の成功率は
  導出不可。held_prior は以前の結果の保全、lock_lost は保存carryへの
  移行で、いずれもデータ消失ではない。ev 75 / items 23 / reasons 10 等は
  issue件数であって失敗request数ではない。
- 意味：repair呼出しは全体の約半分で、限られた枠・予算のかなりの部分を
  占める。原資料増でchunk・修復が増える一般機序と、今回の実観測を分ける。
- 優先度：高。
- 受入測定：repair理由分布・同一passの反復回数（率の計測は同一対象・
  観測完全性を確認してから）。
- 対策案：発生理由の内訳から修復不能な束縛の多さを確認してから
  対象を絞る。一律な抑制はしない。

### P3 patient_context の反復走査

- 実測：選択pass 3,309件・当該窓の新完了記録（done）0。
  scan_incomplete 2,628 / waiting 681。3,309 はLLM/cloud callではなく
  選択passの件数。
- **重要な反証**：scan_incomplete は正常な有限slice終了でも発生する
  （1pass最大8反復・`seen` はcallごとreset。13患者fixtureの
  `test_partial_pass_never_reports_complete` でpin）。ただしCAS競合・
  観測中断・SQLite/OSError等も同じ理由コードに入るため、コードだけで
  正常とも障害とも決められない。waitingの大半（305/324）は scanned=8。
- コード上の候補：`_select_patient` が候補ごとに global PROGRESS の
  pid0 CASを2回行い、他worker競合時に None → scan_incomplete となる経路
  （CAS競合の実比率は未確認）。最初の8候補が未準備でも9件目が実行可能な
  反例があるため「空走査」とは断定しない。
- 優先度：中（当該窓の新完了ゼロだが、走査自体の支配性は反証あり）。
- 受入測定：未走査・future next_try・readiness待ち・CAS失敗・deadline・
  読取重複を分けて集計。

### P4 systemwideメモリ圧縮（因果未特定）

- 実測：swap約4.26GiB（vm_stat 4364MiB）使用、短窓で Compressions 350k /
  Decompressions 304k。
- 反証：他アプリとこの調査自体を含む全systemwide値であり、MCSへの
  因果は未特定。`memory_pressure` 10%閾値はその時点で通過。
- 優先度：低〜中。16GiBで合計49152（3枠各16384）のコンテキストを持つ
  常駐モデルと shadow 比較文書生成が載る構成上の背景として記録。

### P5 shadow 比較実行の負担

- 実測：semantic実設定は shadow（fact_source/loop_mode/summary_mode）。
  shadow_v2 は v2抽出段階の試行7 / 1,808.9s（再取得待ちを内包）。
- 意味：旧factsを消費しつつv2比較文書を生成する経路が、本番と同じ
  枠・予算を消費する。不要と判断してOFF・自動canonical化は
  不可（目的と承認の確認が先）。
- 優先度：中（目的確認後に負荷対効果を評価する領域）。

## 3. コード上の候補と反証（Devin側の合成probe・照合）

構造の前提（コード照合）：

- llama-server（`deployment/launchagents/ai.mcs.llamaserver.plist`）：
  `-c 49152 -np 3`（3枠×16384 tokens）、`-ngl 99`、`-fa on`、
  `-ctk/-ctv q4_0`、`-ub 512`、`--spec-type ngram-simple`、
  `--cache-ram 0`、`-t` 指定なし。KeepAlive・4時のidle再起動あり。
  テンプレートと実launchargsの一致はRootが別途照合済み（全既適用とは
  断定しない範囲を分離）。
- transport `local_llm.py`：loopback 127.0.0.1:8080、MAX_TOKENS=1400、
  TIMEOUT=90。`extract_llm_source_contract.py` は別経路で TIMEOUT=300・
  MAX_TOKENS=1600。`_BATCH_K=0`（batch既定OFF・拒否時単発再試行）、
  `_BATCH_PER_ITEM_S=55.0`。
- 常駐drainer：lane2・lane3の2本＋夜間lane1、`--all --workers 1
  --semantic`。新着は10分tick経路。
- cron（`scheduled_jobs.py`）：unread `*/10`、health `*/5`、durable
  `10,40`、OCR 毎分、retry `0 */6`、llama restart `0 4`。
- 既存の性能道具：`scripts/evaluation/llm_lane_bench.sh`
  （-ub 128/256/512・serial/concurrentのA/B。owner実行・worker/service
  停止前提で今回は未実行・記録のみ）。

合成probeは `llm_slot_schedule.turn` を一時ディレクトリのlockで呼び、
割当と待ちだけを測定（deadline 0.5秒・実LLM/DB不使用）。

| 条件 | 結果 | 待ち |
|---|---|---|
| backlog・全枠空 | slot 1 | ~1ms |
| backlog・slot1占有 | slot 2 | ~0.3ms |
| backlog・昼固定・slot1+2占有 | None | 500ms超で待ち尽くし |
| backlog・legacy `llm_turn.lock` 排他中 | None | 500ms超で待ち尽くし |
| backlog・RT intent保持中 | None | 500ms超で待ち尽くし |
| backlog・夜固定・slot1+2占有 | slot 0（借用） | ~0.3ms |
| realtime・全枠空 | slot 0 | ~1ms |
| realtime・slot0占有 | slot 1 | ~1ms |

- **昼間backlog 2枠制限**：仕様（昼は枠1,2のみ・夜は枠0借用可）。
  今回の夜間観測窓の支配原因ではないため反証へ格下げ。昼の枠0借用は
  RT保護と競合し依頼外なので提案しない。
- **legacy `llm_turn.lock` 直列化**：旧worker排他中は新turn全てが待つ
  （合成証明）。現行workerが実際に握る頻度は未確認。
- **RT intent による譲り**：仕様どおりの優先制御。実頻度は未確認。
- **単一GPU共有**：物理3枠は1つのQwen3.5-9Bで時分割されるため、
  枠を増やしても3倍とは限らない（実hardware throughput未測定）。
  `-fa on`・KV q4_0・`-ub 512`・`--spec-type ngram-simple`・
  `--cache-ram 0` は起動plistと実launchargsがRoot照合で一致済み。
  なおRoot実見は常駐3本＋患者context子10本・各枠の実値16384で、
  child10本を「LLM同時10枠」と同義にはしない。/slots短窓は全idleで
  「GPUが常時詰まる」は立証されていない。
- **carry保存待ちによる仕事供給**：`extract_llm_resident.py:480-498`・
  `512-515` で、carry（完成結果の保存待ち）が残る限り新しい推論・
  semanticへ進まず保存処理へ戻る（物理LLM slotはtransport後に解放済）。
  lock_lost 7と保存待ちが仕事供給へも影響しうるコード経路。
  ただし各idle時点の原因をcarryへ断定はしない。
- **`_wait_slot_idle` 占有**：transport切断後も枠を保持するcompletion未知
  保護。頻度未確認。
- **`_BATCH_PER_ITEM_S=55.0`・`_BATCH_K=0`**：1件55秒は推定定数であり
  実遅延の観測ではない。実値は上表の prompt_ms/predicted_ms 側。
  batch有効化は原因未立証のため、後段の合成比較候補に留める。
- **`invalidate_s` / `reproject_s`**：当窓の実測は invalidate 0–0.002秒・
  reproject 約0.001–0.004秒で、主因ではない（将来規模では監視対象）。
- **Jev 通信**：transport 合計9.3秒に対し Jev再取得は142.5秒。
  Jev通信自体を主因とする証拠はない（待ちの所在はlock側）。
- **idle poll**：`extract_llm_resident.py:433-460, 631-660` で
  `_IDLE_FLOOR_S=5`→`min(30, 2倍)` の指数backoffか next_due起床。
  120秒は circuit-open/disk_low・再試行なし時のみ。新着は10分tick。
- **OCR**：隔離runtime・network禁止・YomiToku CPUのみ・cv2/torch
  スレッド1固定・200DPI。本文抽出とは別の原本OCRで、`mcs_ocr.py` の
  LOCAL_CALL_S=300・原本OCR既定120秒・全drain 900秒・終端5。
  LLMと別経路だが同一機体のCPU/RAMを共有。実OCR所要時間は未測定。
  既知の品質holdは当該60分窓の新失敗ではなく、timeout延長で自動解決
  するとは限らない（別観測由来は時点不明として既知hold扱い）。

## 4. 優先対策と受入計測（Oracle採用順）

実再取得待ち132–413秒があるため **B（共有lock保持の短縮）を入口**。
実害が同程度なら既存同期を保てる **D/C** から最小1つ。**A（期限延長・
worker増加）** は、実際の推論打切りまたは準備済み仕事の供給不足と
資源余力を示す場合に限定して可逆比較。B/C/Dの全面棄却は必須にしない。

- **B**：collector/snapshot/derive/reprocess等の長い保持区間のうち
  1箇所の純粋な計画部分をlock外へ出し、commit時に source・permission・
  QC・updater世代の現在性を再検証する。`invalidate_projections` 全体を
  単純unlockする変更ではない。反証計測：保持区間と待機区間の重なり。
- **D**：同一 `next_try`・fair cursorの範囲で反復走査だけを減らす。
  反例（最初8件未準備でも9件目ready）を維持。未走査と真の実行対象なしを
  分けて集計するのが最も安い反証。
- **C**：同一read snapshot・同一条件の計画読取りだけを再利用。
  scope/contract/hydrate差・Recording・dependency修復後・別connection変更を
  同一視してはいけない。readiness・permission・quota・UNKNOWN hold判定を
  transport越しに使い回さない。
- **A**：実処理の打切り・供給不足の証拠がある場合のみ可逆比較。
  transportの実体がslot/writer/資源待ちなら延長は悪化させる。

維持する契約（全案共通）：source・currentness・permission・QC・provenance、
accounting・UNKNOWN（未送信と読み替えない・盲目的再送しない）、
CAS・更新境界（transport中にDB transactionを持ち込まない）、
realtime優先・公平性。誤った公開・権限/QC違反・二重計上・CAS上書き・
UNKNOWN解除は1件でも停止・撤回。

## 5. 未確認と説明食い違い

**未確認**

- 各再取得待ちの時点で run.lock を握っていた holder の特定
  （collector以外のwriterもあり得るため断定不可）。
- `/slots` 短窓は6サンプルのみで、長期稼働率・待ち中の占有率は不明。
- context scanのCAS競合実比率・worker反復の内訳（Root集計は件数のみ）。
- shadow比較実行の目的と公開対象差（Root側で追加照合）。
- systemwide圧縮がMCSに及ぼす実影響（他要因を含み分離不能）。
- 実OCR所要時間・添付ごとのtimeout完走率。
- Oracle実行の表示名は request gpt-6-pro・browser strategy
  select/thinking pro・ログ上のPro確認まで。backendの独立同定は不可で、
  UI picker一致の証拠は未記録・未確認として区別。

**文書とコードの食い違い（コード基準で記録）**

- `deployment/launchagents/README.md` の「idle poll 120s」は
  resident実装と不一致（実際は floor 5秒→最大30秒の指数backoffか
  next_due起床。120秒は circuit-open系・再試行なし時のみ）。
- `extract_llm_admission` docstringは「canonical source returns an empty
  set」とあるが、実装は `semantic_conversion.active_legacy_admissions(ledger)`
  を返す。
- resident `--workers` のヘルプは「同じ固定slot」とあるが、
  scheduled経路は物理枠を再配分する（`llm_slot_schedule`）。

## 6. 対象外と検証

**対象外・やらないこと**：安全gateの緩和・予算の一律増・品質確認省略は
しない。新規推論要求・DB書込み・設定変更・再起動は行っていない
（Root側の readonly loopback GET `/slots` と既存ログ・状態の
bounded観測はこの調査の範囲内）。
`llm_lane_bench.sh` はworker/service停止が前提のowner向け道具であり
この調査では実行しない（実測が必要になった場合はRoot判断で別途）。

**検証**

- 最小3node回帰（実データ・実LLMなし・`scripts/run_tests.sh`）：
  `test_partial_pass_never_reports_complete` /
  `test_saved_global_cas_detects_competing_owner` /
  `test_actual_transport_never_sends_two_requests_to_one_physical_slot`
  — 3件 pass（有限slice・競合保護・1物理slot二重送信なしを裏付け）。
- 合成probe：`probe_slot_schedule.py`（私有監査）の割当・待ち測定は
  上表のとおり。性能値ではなくlock割当の構造確認。
- 本節は測定・コード・反証を分離した記録であり、対策の実装・適用は
  行っていない。
