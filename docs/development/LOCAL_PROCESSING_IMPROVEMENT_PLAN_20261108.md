# ローカル処理改善計画（2026-11-08）

この文書は `LOCAL_PROCESSING_BOTTLENECKS_20261108.md`（task 24・
最終 `53f27ce8`）の根拠にもとづく改善計画です。計画確定後の実装指示と、
Codexへ実装を移管してDevinをテスト担当とする追加指示を反映しています。
Oracle相談・計画・実装はCodex、固定候補の検証実行はDevinが担当します。

## 実装と受入の現在地（2026-11-09）

| 項目 | 現在地 | 残る受入 |
|---|---|---|
| P0 | 既存の固定語彙・数値に対応する観測定義を追加 | 同条件での実機比較 |
| C1 | 同一候補の重複走査読取りを集約。`e785aba4`から後続修正を候補へ統合 | 統合候補の検証・本番での進捗確認 |
| B1 | `finish_snapshot`の検証期間だけロックを解放。`6d723310`までの安全修正を候補へ統合 | 統合候補の検証・本番での待ち時間と公開確認 |
| E・C2・B2・D・A | 以下の条件付き計画を維持 | C1/B1適用後の残差を観測して採否を決める |

B1はcopy自体を高速化する変更ではありません。deliver段とOCRの公開経路は
対象外です。合成検証の結果と、実機の速度効果・本番適用は区別します。
以下は起案時の根拠と設計を保持した本文です。本文中の前提照合・合成反証・
適用条件は、上の実装状態だけで全て実証済みとは扱いません。

## 1. 目的と現況根拠

目的：task 24 で実測した遅延要因に対し、安全契約を保持したまま
最小変更で有効成果／壁時計を改善する工程を決める。

基準：開発・検証は `ff038ef8` 系 source（この文書の作業中に
`53f27ce8` との mcs/adapters/deployment 差分 0 を再確認）。元の
観測窓は 2026-11-08 22:07:52–23:07:52 JST。レビューが 2026-11-09
へ跨いだが、ファイル名は起票日 11/08 のまま維持する。

確定事実（24 の bounded 実機集計とコード照合）

- LLM 後の run.lock 再取得 132–413 秒、後続 call 開始前の
  `budget_short`（reserve 540・LONG_MIN_CALL 620・LLM_CALL_RESERVE 300
  の機序と整合）。全収集 tick は 291–354.8 秒で run.lock 保持
  （transport のみ unlock）。holder の個別特定は未追跡。
- extract 53 call 中 repair 25。issue 件数（ev 75 等）は request 数
  ではなく、budget check 前に記録される validation 失敗項目数。
  repair_wins は
  nullable で成功率は導出不可。defer 理由 `empty_repair_candidate_binding`
  20 件は defer 理由の記録であり、20 LLM call・新費用を示さない。
  無消費（used=0・llm_s=0・progress なし）の binding hold は既存の
  selected 減算で独立 job を阻まない（`semantic_drain.py:878-886`）。
  20 件全てが無消費だったとは観測から確定しない。
- patient_context は選択 pass 3,309 で当該窓の新完了 0。
  `scan_incomplete` は正常有限 slice 終了・CAS 競合・中断のいずれも
  含み、コードだけで正常/障害を決められない。
- `/slots` 短窓 6 サンプルは全 idle。「GPU が常時詰まる」は未立証。

有力仮説（仮説のまま保持）

- writer（collector 等）の run.lock 長保持が再取得待ちの主因候補。
- ev 支配の脱落は first-pass 引用遵守ギャップの可能性があるが、
  `Validator.ev` は空・非 str・本文 None・非一意・不一致でも
  span None になるため全件を言い換えとは断定しない。
  言い換え/非一意/空/非 str/chunk 境界/同一引用の複数出現の内訳は
  未取得で、「全てモデル遵守ギャップ」「必ず高効果」とはしない。

## 2. 案比較表

| 案 | 内容 | 根拠/反証 | 順位 |
|---|---|---|---|
| P0 | 既存証跡の基準固定・必要最小の観測項目定義 | 計測なしに改善を判定できない | 最初 |
| C1 | ctx 計画の重複 read 除去（最小確定案） | 同一条件の純粋 read を 1 組減らす。小さく確実 | P0 の次 |
| B1 | writer 保持の局所短縮（条件付き設計・合成反証後に採用判断） | 再取得 132–413 秒の実測に直撃する候補。前提照合済み・必須 2 条件つき | C1 の次 |
| E | repair 負荷（first-pass evidence 指示の最小再配置） | ev 支配だが内訳未確定・仮説として限定 | 残差次第 |
| C2 | source 集合の 1 回取得→共通 builder | scope 差の再導出が必須で単純 filter 不可 | C1 後の残差で判定 |
| B2 | derive 計算の chunk 単位 TX 外化 | derive 全体 unlock は不可。実計算割合不明 | B1 後の残差で判定 |
| D | context 走査の反復削減 | no-due 証明できない。107>8 で効く証拠なし | 低順位 |
| A | 予算・実行設定の限定比較 | 実推論打切りまたは供給不足＋資源余力の証拠がある時のみ可逆比較。B/C/D の全面棄却を必須にしない | 限定条件 |

実作業順：**P0 → C1 → B1 → 同条件比較 → 残差に応じて E/C2/B2/D/A**。
C1 と B1 の設計・合成 fixture 準備は独立。局所合成 A/B は 1 要因
ずつ。本番適用は再評価費用を確認した固定候補単位とし、複合候補
では各施策の実機寄与率を断定しない。契約移行直後と定常を分離
する。

初手にしないもの：タイムアウト一律延長・昼 slot0 借用・worker 増・
新永続 cache・新 queue・品質/unknown gate の緩和。

## 3. 最初の実装単位／IF／保全条件

### C1（最小確定案）

- `patient_context_refresh_readiness._pending` へ既存 `observed_out`
  パターンの optional dict を追加し、導出済みの stored を
  `observed_out['scans']` に格納。戻り値 tuple は不変。
- `patient_context_refresh._select_patient` で直後の `_current_scans`
  を同じ stored 参照に置換。再利用は同一候補・同一 connection・
  同一 readTX・同一 source_scope・contract・cfg・estimate のみで、
  一候補の計画内で破棄。write/await/transport/別 pid/別 scope を
  越えて再利用しない。新 cache・永続状態なし。
- Recording・read_witness・`_capture_readiness`・
  `_readiness_for_write`・送信直前・採用前再検証は不変。
- 目標：SCAN/CHUNK＋品質 receipt の重複 read 1 組 → 0。
  同 fixture の selection/lane/source_sig は完全同等。
  既存 caller は未指定のまま不変。合成受入は既存 readwitness /
  authorizer 契約を必ず維持し、estimate・proof_for・scope の異なる
  caller で取得物を共用しない。

呼出例（イメージ）：`observed = {}` → `_pending(..., observed_out=observed)`
→ `scans = observed['scans']`（`_select_patient` 側は `_current_scans`
を同じ stored 参照へ置換）。

合成受入（C1）

- 8 件未準備＋9 件目 ready で活動順が中断しても公平性が保たれる
- scope・cfg・proof の異なる caller で取得物を共用しない
- 資料 QC・権限変更時は送信・採用を拒否する
- readwitness 拒否・unknown 会計は既存どおり動く
- 同一入力で出力が完全同等、かつ重複 SQL/read が削減される

### B1（条件付き設計・前提照合済み・合成反証後に採用判断）

Oracle followup と readonly source 照合の結論：**既存の協調 lock
規約内で「合成反証に進める条件付き設計」**。未実装・未テストで
採用や速度改善は確定していない。既存 `unlocked_transport` を
挿すだけの実装は不可。C1 を先行する計画と矛盾しない。

前提の照合結果（readonly・`ff038ef8` 系 source）

- 正規 restore は同一 run.lock を保持：update_apply（→
  update_restore/finally）・中断復旧 update_recovery・独立復旧
  mcs_recover が同じ leaf を保持。recovery_core/safe_io は
  O_CREAT/NOFOLLOW で既存 leaf を開き truncate しない。
  `mcs_backup.restore` は NEW dir のみ。対応 source 範囲に
  run.lock pathname の unlink/replace/move 経路はなし。
- 非協調な外部 unlink→検査直後の差替えは stat で防ぐと書かず、
  規約外操作として停止する。新 coord・永続 generation は追加
  しない。

必須 2 条件

1. restore を含む writer 全体が同じ安定 run.lock で排他されること
   （pathname 差替えで oldfd/newfd が分裂しないこと・照合済み）。
2. snapshot 専用の書込禁止状態を物理 held と分離し、元例外を保持
   して全終端 caller へ伝えること。

unlock 範囲と IF スケッチ（`writes_allowed` 境界と一体）

- snapshot の private copy 取得は run.lock 下で行い、unlock するのは
  `_valid_snapshot_candidate` の検証期間だけ（既存
  `unlocked_transport` を使用）。`.snapshot.lock` は全期間保持する。
- 順序：reacquire＋fence 確認 → fsync/rename → marker → dirtyCAS。
  copy/publish/marker/dirtyCAS の既存 lock 規約は変えない。
- IF：keyword-only の `verification_context(*, deadline)` は
  context manager を返す factory とし、`run_check` →
  `maintenance.refresh_snapshot` / `_refresh_snapshot_locked` /
  `publish_snapshot` → `snapshots._publish_snapshot_locked` へ透過
  する。optional 引数の既定は None で、未指定 caller は旧動作の
  まま不変。
- 所有範囲：ingest の `run_check` ＋ `core/{maintenance,snapshots}`。
  既存 writer helper を再利用し、共有 `writer_lock` の意味変更は
  別レビュー扱い。

最小 seam（確定）

- main の snapshot 呼出し期間だけ、local `writes_allowed` を共有
  （永続化なし・他 LLM 契約不変・物理 held と分離）。factory 前は
  False、有限 deadline＋再取得＋updater/restore/DB/runlock
  identity＋publication fence の確認成功でのみ True へ。
  後段の pubfence 失敗でも False へ戻す伝達を必要条件とする。
- 元例外 object/traceback は yield 直近で保持。再取得成功後は
  必要な安全判定を行って元例外を再 raise（通常 validation 失敗は
  既存 partial 処理で可）。再取得等の二次失敗でも元例外を保持し
  `writes_allowed=False`、generic/`_finish_run`/outer
  RunLockLost 等の全終端 write を遮断。fd 保持の偽装・手動 close
  はしない。
- `_run_stage` は finally の数値記録のみで exception を catch
  しない（照合済み）。no-write 時は `result.ok=False`・未公開・
  固定理由＋exit で失敗を示し、health/outbox/finish_run 補正を
  書かない。`finish_run` は snapshot 前に記録済みのため
  running→crashed としない。
- sourceDB identity は当該 Ledger を開いた writer-owned 時点と
  照合可能な基準を持つこと。snapshot 直前の path stat だけでは
  古い connection との一致を証明しない。copy/unlock 前と再取得後
  で照合し、DB 不明は no-write。runlock の fd/path identity も
  再照合（異常検知であり、任意外部改変の完全防御ではない）。

維持（r6 継承）

- `.snapshot.lock` LOCK_NB 維持・既存 caller 間 ABBA なし。
  reacquire timeout・他 publisher partial 増加は受入観測項目。
- copy/publish/marker/dirtyCAS の lock 維持、通常 writer の
  point-in-time 許容、copy 前 signature と dirty 世代 CAS 不変。
- 有限 absolute deadline のみ有効化（None は旧動作）。validator
  終了時刻は unlock scope 内で採時、reacquire を verify_s へ混入
  しない。reacquire 後と replace 直前でも deadline 確認。
- runlock fd は親借用・子へ非継承。budget 延長なし。default
  caller は不変。

合成反証の条件（完全な検証表）

- 別 process の通常 commit で dirty 世代が保持される
- copy/publish/marker/dirty の各手順が既存どおり保持される
- 非 blocking `.snapshot.lock` の他 publisher・prune との競合
- 同版 restore・DB 交換・hold の開始/終了
- DB/lock/fence 不一致で no-write になる
- deadline 内 verify と期限後 reacquire が分離して扱われる
- false validator 通常 partial・各種例外・取消 × 再取得失敗/
  restore 失敗/cleanup 二次失敗
- old ledger＋sourcepath 不一致、finish_run 済みの失敗、DB A→B
- unreaped 子 process の publication fence
- default factory・None deadline で旧動作のまま

安全違反 1 件で不採用。性能面も writer wait 改善なし・snapshot
partial/RT 遅延増なら戻す。この no-write/例外境界が最小差分で
閉じなければ B1 を延期し、C1 だけを採用判断する。大規模な共通
writer protocol の作り直しへ自動拡張しない。

### E（repair・残差対応の比較案）

- ev 75 は issue 数であり request 数ではない。一意引用失敗の機序は
  確定だが、言い換え/非一意/空・非 str/本文 None/chunk 境界/同一
  引用の複数出現の内訳は未取得のため効果は仮説のまま。E は条件
  付きの評価案であり高効果確定ではない。
- 完全一致指示は既にあるので文言の重複追加はせず、既存 prompt を
  最小再配置・短い実行可能な引用指示へ置換する案を比較する。
- 出力を減らして repair だけ減らす退行は、precision/recall・否定・
  対象・時制・数値・根拠で拒否。
- `locate_quote_span`・validator・one-shot・品質 threshold は不変。
- 評価は既存 `extract_bench`＋frozen 完全合成 corpus を優先して
  使う。反例（重複引用・chunk 境界・否定等）が既存 corpus に無い
  場合のみ、完全合成の必要最小 case を追加してよい（新 fixture の
  絶対不要とは固定しない）。
- `--mock-ok` は検証器の健全性確認だけで、prompt 改善の効果証明
  ではない。mock 成功は実モデルでの品質改善とは区別する。
  実モデル比較は許可済み隔離評価環境での別工程で、今回は実行しない。
- prompt 変更の contract/fingerprint と旧 checkpoint への影響は
  実装前に全 caller を確認する。checkpoint は新機構が既にあるので
  追加しない。repair_wins 3/25 など不完全な率の提示は禁止。

### 所有範囲と代表テスト

| 案 | 実装担当 | 所有 path | 既存の代表テスト（実在確認済み） |
|---|---|---|---|
| C1 | Devin | `patients/` の 2 source（`patient_context_refresh_readiness`・`patient_context_refresh`） | `tests/patients/test_context_scan_integration.py`・`test_patient_readiness_witness.py`・`test_readiness_cursor_witness.py`・`test_patient_context_refresh.py` |
| B1 | Devin（設計は Codex 確定済） | ingest の `run_check` ＋ `core/{maintenance,snapshots}`＋既存 writer helper の再利用 | `tests/core/test_snapshot_refresh_timing.py`・`test_snapshot_failure_reason.py`・`test_snapshot_maintenance.py`・`test_run_lock_error_cleanup.py`・`tests/ingest/test_derive_transaction_ownership.py` |

テストは変更影響に応じて節目ごとの batch を選定する。毎回の全件
実行は必須としない（§5 テスト境界と同じ規約）。

## 4. 段階別実施順と担当

| 段階 | 作業 | 担当 | 依存 |
|---|---|---|---|
| P0 | 既存証跡の基準固定・必要最小の観測項目定義 | Devin（計画レビューはCodex） | なし |
| C1 | 重複 read 除去の最小実装＋合成 fixture | Devin | P0 の観測定義 |
| B1 | writer 保持の局所短縮（最小 seam 確定・合成反証後に採用判断） | Devin（設計はCodex確定済） | P0・必須条件 |
| 比較 | 同条件で 1 変更ずつ合成受入 | Devin | C1/B1 の単位完成 |
| 残差 | E/C2/B2/D/A の採否を残差で判定 | Codex 判定・Devin 実装 | 比較結果 |

**ロールアウト費用（必須確認・コード根拠）**

C1/E は「小さい差分だから再処理への影響ゼロ」としない。契約束縛は
ソース全体に及ぶ。

- `_LOADED_SOURCE_PATHS`（`extract_llm_source_contract.py:205-229`）は
  `patient_context_refresh_readiness` 等を含み、`_upgrade_contract`
  （`extract_llm_qc.py:51-53`）は全 digest を束縛、`_quality_code`
  （`patient_context_refresh_state.py:268-272`）は loaded digests＋
  `semantic_candidate` contract を束縛する。E の `_PROMPT_HEAD` は
  `_chunk_context(checkpoint=True)`（`extract_llm_context.py:135-147`）
  へ入る。
- したがってソース変更は旧 checkpoint/QC/promotion/held_prior 契約の
  再照合に及ぶ。隔離した旧版→候補の合成 fixture で、実装前に影響
  範囲を確認する。
- 新しい hash 再承認の偽装・既存 approval の流用・gate 緩和は不可。
  unknown/paid 履歴・人承認は保持する。
- 契約変更で増える再評価処理と、定常速度の改善を分けて受入判断する。
- C1 は先にローカル実装＋合成比較できる小単位だが、契約影響が広
  ければ本番先行適用の必然性はない。変更ごとに本番を再起動・再評
  価させず、独立 commit＋局所 A/B を済ませた 1 固定候補へまとめて、
  既存許可に沿って適用する。最初の実機比較で 1 要因だけにする場合
  も、この再評価費用を先に確認する。単純に「毎 commit 配備」とは
  しない。

## 5. 合成受入・将来実測・中止条件

**主要 KPI**：品質合格して保存/採用された処理数・context 公開数
／同条件 wallclock。raw call 数・試行 pass 数・tokens だけで成功に
しない。

**補助計測**：writer 待ち p50/p95/timeout・carry age、first/repair 分離、
candidate scan/SQL 件数/同一 scope、snapshot 公開 age、RT 待ち。
並行時間の総和を壁時計と混同せず、NULL/観測中断を 0 としない。
既存の固定語彙・数値イベントを優先し、実本文・ID・例外文・PHI を
計測へ持ち込まない。P0 の新分類は必要な分岐でのみ（slice/CAS/SQL
中断等を推測で決めない）。観測基盤を別製品として増築しない。

**停止条件**：旧版・別患者・失効権限・QC 不合格の誤採用、unknown の
不正解除、重複送信・重複または未承認課金、不正公開、CAS 上書きは
1 件でも停止・撤回。保護 hold や QC 拒否そのものは失敗扱いしない。
速度効果未証明・RT 公平性/carry/snapshot が悪化するなら次の拡張
前に rollback または不採用。旧データ・履歴・hold は削除せずコード
単位で戻す。

**テスト境界**：一時 DB＋完全合成＋stub/実 filelock 子 process のみ。
節目に関連 1 batch、成功結果を無変更で再実行しない。実機観測は
将来の本番適用承認後に、自然稼働の同時間・同設定窓で行う。局所
合成 A/B は 1 要因ずつ、本番は再評価費用を確認した固定候補単位
（§2 と同じ規約）。今回は新 pytest なし。

## 6. 保留案・未確認・文書差異

**保留案の条件**

- C2：source 集合の 1 回取得→共通 `_snapshot` builder へ渡す案。
  scope 依存 signature/OCR/権限/依存喪失/契約/hydrate/readwitness 差は
  再導出が必要で、単純な whole-snapshot filter は不可。C1 後に
  listing 支配が残る時のみ。
- B2：derive 全体 unlock は不可。`extract._extract_rows` の
  `extract_message` 純計算だけを chunk 単位 TX 外へ出し、source
  binding 再検証→既存 atomic 置換を条件に別設計。実計算割合不明の
  ため B1 後の残差で判定。
- D：waiting/scan_incomplete を一律 30 秒にしない（8 件 slice の次
  候補の公平性を守る）。`_next_due_in` は future hint で no-due 証明
  ではない。complete＋全対象 covered が実証できる状態のみ、既存の
  5→30 backoff/next_due を再利用。現状 107>8 で効く証拠なし、低順位。
- A（予算・実行設定の限定比較）：300/600 等の一律拡大、-np/ctx/
  batch/threads/モデル/GPU をまとめて変更しない。1 因子の可逆
  比較だけ。物理 LLM 3 枠・日夜帯・新着次空き優先・実行中非中断
  は維持（worker 子 process 数と物理 3 枠は別物）。メモリ圧縮・
  単一 GPU・共有資源・既存 batch OFF・shadow 目的は当初から断定
  しない。既存 `llm_lane_bench` 等の隔離評価が必要な条件付き案
  として位置付ける（新 bench framework は作らない）。shadow 停止
  も承認目的を確認後の限定比較で、自動削除なし。

**未確認**

- ev 脱落の内訳（言い換え/非一意/空/非 str/chunk 境界）。
- 各 wait 時点の holder 特定・長期枠稼働率・CAS 競合実比率・
  systemwide 圧縮の MCS 影響分離・実 OCR 所要時間。

**文書差異（該当実装時に説明整合として追跡・今回は修正しない）**

- `deployment/launchagents/README.md` の idle 120s vs 実装の通常
  5→30/next_due（120 は circuit-open 系のみ）。
- `extract_llm_admission` docstring「canonical 空集合」vs 実装の
  `active_legacy_admissions`。
- resident `--workers` ヘルプ「同じ固定 slot」vs scheduled 経路の
  物理枠再配分。

（補足）`run_check` held=True の末端 write は、今回新たに確認した
実装上の境界で B1 の必須条件へ組込み済み。文書差異そのものとは
断定しない。

## 7. 全案の受入・停止・担当

| 案 | 現状 | 採用条件 | 停止条件 | 担当 |
|---|---|---|---|---|
| P0 | 計画内・最初 | 観測項目が固定語彙・数値で定義できる | PHI/本文を計測へ持ち込む案 | 実装担当 |
| C1 | 最小確定案 | 同 fixture で重複 read 1 組→0・selection/lane/source_sig 同等 | scope/cfg/contract 差を同一視する設計 | 実装担当 |
| B1 | 条件付き設計・最小 seam 確定 | 安定 run.lock の既存規約は照合済み。no-write/元例外保持は実装・合成検証で証明が必要。最小 seam＋拡張合成反証を通過 | fd 所有偽装・旧 ledger への追記・安全違反 1 件・境界が最小差分で閉じない場合は延期 | Codex 設計確定→Devin |
| E | 比較案 | ev 内訳分類＋bench A/B で precision/recall 不悪化 | 完全一致・validator・one-shot の弱体化案 | Codex 判定→Devin |
| C2 | 保留 | C1 後も listing 支配が残る | scope/権限/契約差の再導出省略 | Codex 判定 |
| B2 | 保留 | B1 後も計算割合が支配 | derive 全体 unlock 案 | Codex 判定 |
| D | 低順位 | complete＋全 covered が実証可能 | 一律延長・no-due 未証明での間引き | Codex 判定 |
| A | 限定条件（予算・実行設定の比較） | 推論打切りまたは供給不足＋資源余力の証拠＋既存 lane bench での隔離評価 | 一律拡大・複数因子の同時変更・3 枠構成の変更 | Codex 判定 |

（計画確定稿・B1 は合成反証後に採用判断。採用/実装開始は別指示）
