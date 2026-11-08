# ローカル処理改善計画（2026-11-08・PLAN_ONLY）

この文書は `LOCAL_PROCESSING_BOTTLENECKS_20261108.md`（task 24・
最終 `53f27ce8`）の根拠にもとづく改善計画です。**計画とレビューまでが
今回の範囲で、製品実装の開始は別の明示指示が出てから**。
実装はDevin、Codexは計画・レビュー・Oracleを担当します。

本文は実装済みの記述ではありません。確定事実・有力仮説・未確認を
分けて扱い、残時間の見積りや性能改善量は捏造せず、工程の依存だけを
示します。

**現時点の状態：draft（B1 節は Oracle focused followup の追補待ちで
計画未確定）。**

## 1. 目的と現況根拠

目的：task 24 で実測した遅延要因に対し、安全契約を保持したまま
最小変更で有効成果／壁時計を改善する工程を決める。

確定事実（24 の bounded 実機集計とコード照合）

- LLM 後の run.lock 再取得 132–413 秒、後続 call 開始前の
  `budget_short`（reserve 540・LONG_MIN_CALL 620・LLM_CALL_RESERVE 300
  の機序と整合）。全収集 tick は 291–354.8 秒で run.lock 保持
  （transport のみ unlock）。holder の個別特定は未追跡。
- extract 53 call 中 repair 25。issue 件数（ev 75 等）は request 数
  ではなく、budget check 前に記録される分母。repair_wins は
  nullable で成功率は導出不可。`empty_repair_candidate_binding` の
  hold は used=0・llm_s=0 で selected 減算される既存保護
  （`semantic_drain.py:878-886`）で、新費用を生まない。
- patient_context は選択 pass 3,309 で当該窓の新完了 0。
  `scan_incomplete` は正常有限 slice 終了・CAS 競合・中断のいずれも
  含み、コードだけで正常/障害を決められない。
- `/slots` 短窓 6 サンプルは全 idle。「GPU が常時詰まる」は未立証。

有力仮説（仮説のまま保持）

- writer（collector 等）の run.lock 長保持が再取得待ちの主因候補。
- ev 支配の言い換え引用は first-pass 遵守ギャップの可能性があるが、
  言い換え/非一意/空/非 str/chunk 境界の内訳は未取得で
  「全てモデル遵守ギャップ」「必ず高効果」とはしない。

## 2. 案比較表

| 案 | 内容 | 根拠/反証 | 順位 |
|---|---|---|---|
| P0 | 既存証跡の基準固定・必要最小の観測項目定義 | 計測なしに改善を判定できない | 最初 |
| C1 | ctx 計画の重複 read 除去（最小確定案） | 同一条件の純粋 read を 1 組減らす。小さく確実 | P0 の次 |
| B1 | writer 保持の局所短縮（候補・追補待ち） | 再取得 132–413 秒の実測に直撃する候補 | C1 の次 |
| E | repair 負荷（first-pass evidence 指示の最小再配置） | ev 支配だが内訳未確定・仮説として限定 | 残差次第 |
| C2 | source 集合の 1 回取得→共通 builder | scope 差の再導出が必須で単純 filter 不可 | C1 後の残差で判定 |
| B2 | derive 計算の chunk 単位 TX 外化 | derive 全体 unlock は不可。実計算割合不明 | B1 後の残差で判定 |
| D | context 走査の反復削減 | no-due 証明できない。107>8 で効く証拠なし | 低順位 |
| A | 期限延長・worker 増加 | 実推論打切りまたは供給不足＋資源余力の証拠がある時のみ可逆比較。B/C/D の全面棄却を必須にしない | 限定条件 |

実作業順：**P0 → C1 → B1 → 同条件比較 → 残差に応じて E/C2/B2/D/A**。
C1 と B1 の設計・合成 fixture 準備は独立だが、本番比較は 1 変更ずつ。

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
  既存 caller は未指定のまま不変。

### B1（候補・追補待ち・計画未確定）

- 候補：private snapshot コピー完成後の `_valid_snapshot_candidate`
  の中だけ run.lock を解放。copy/publish/marker/dirtyCAS は保持、
  `.snapshot.lock` は全期間保持かつ LOCK_NB 取得は不変。
  既存 `unlocked_transport` helper を再利用し、optional の
  verification context factory を run_check → maintenance →
  snapshots へ透過（default なしは既存動作）。
- 要解決：同 code restore の DB os.replace / lockfile 交換 / 期限 /
  child fd / RunLockLost。現在 run_check は RunLockLost held=True
  でも finish_run＋health を書くため、helper を挟むだけでは不可。
  検証後 stale/所有不明は古い ledger/health へ追記しない最小境界が
  必要。fd 所有を偽装しない。
- validator 終了を scope 内採時し reacquire と分離、同一 absolute
  deadline で通算。通常の別 writer commit は point-in-time snapshot
  として許容し、新 dirty 世代を消さない。
- verify 42–55 秒を他 writer へ開放できる候補だが、fetch/derive を
  含む 132–413 秒の全部解消は約束しない。
- **Oracle の新競合反例 followup 待ち。追補で確定/差替えする。**

### E（repair・残差対応の比較案）

- ev 75 は issue 数であり request 数ではない。一意引用失敗の機序は
  確定だが内訳未取得のため効果は仮説のまま。
- 完全一致指示は既にあるので文言の重複追加はせず、既存 prompt を
  最小再配置・短い実行可能な引用指示へ置換する案を比較する。
- 出力を減らして repair だけ減らす退行は、precision/recall・否定・
  対象・時制・数値・根拠で拒否。
- `locate_quote_span`・validator・one-shot・品質 threshold は不変。
- 評価は既存 `extract_bench`＋frozen 完全合成 corpus を使う。
  `--mock-ok` は検証器の健全性確認だけで、prompt 改善の効果証明
  ではない。実モデル比較は許可済み隔離評価環境での別工程で、
  今回は実行しない。
- prompt 変更の contract/fingerprint と旧 checkpoint への影響は
  実装前に全 caller を確認する。checkpoint は新機構が既にあるので
  追加しない。repair_wins 3/25 など不完全な率の提示は禁止。

## 4. 段階別実施順と担当

| 段階 | 作業 | 担当 | 依存 |
|---|---|---|---|
| P0 | 既存証跡の基準固定・必要最小の観測項目定義 | 実装担当（計画レビューはCodex） | なし |
| C1 | 重複 read 除去の最小実装＋合成 fixture | 実装担当 | P0 の観測定義 |
| B1 | writer 保持の局所短縮（Oracle 追補で確定後） | 実装担当（設計確定はCodex/Oracle） | P0・追補 |
| 比較 | 同条件で 1 変更ずつ合成受入 | 実装担当 | C1/B1 の単位完成 |
| 残差 | E/C2/B2/D/A の採否を残差で判定 | Codex 判定・Devin 実装 | 比較結果 |

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

**停止条件**：旧版/別患者/権限失効/QC 不合格/unknown 解放/重複送信/
課金/不正公開/CAS 上書きは 1 件でも停止・撤回。速度効果未証明・
RT 公平性/carry/snapshot が悪化するなら次の拡張前に rollback または
不採用。旧データ・履歴・hold は削除せずコード単位で戻す。

**テスト境界**：一時 DB＋完全合成＋stub/実 filelock 子 process のみ。
節目に関連 1 batch、成功結果を無変更で再実行しない。実機観測は
将来の本番適用承認後に、自然稼働の同時間・同設定窓で 1 変更ずつ。
今回は新 pytest なし。

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
- A：300/600 等の一律拡大、-np/ctx/batch/threads/モデル/GPU を
  まとめて変更しない。1 因子の可逆比較だけ。3 枠/日夜/新着次空き
  優先/実行中非中断は維持。shadow 停止も承認目的を確認後の限定
  比較で、自動削除なし。

**未確認**

- B1 の新競合反例（Oracle followup 中・追補で確定）。
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
- 新発見：run_check held=True 末端 write は B1 で考慮必須。
  全経路 no-write と誤記しない。

## 7. 全案の受入・停止・担当

| 案 | 現状 | 採用条件 | 停止条件 | 担当 |
|---|---|---|---|---|
| P0 | 計画内・最初 | 観測項目が固定語彙・数値で定義できる | PHI/本文を計測へ持ち込む案 | 実装担当 |
| C1 | 最小確定案 | 同 fixture で重複 read 1 組→0・selection/lane/source_sig 同等 | scope/cfg/contract 差を同一視する設計 | 実装担当 |
| B1 | 候補・追補待ち | Oracle 追補で反例解決＋stale 非書込み境界が立つ | fd 所有偽装・旧 ledger への追記 | Codex 設計確定→Devin |
| E | 比較案 | ev 内訳分類＋bench A/B で precision/recall 不悪化 | 完全一致・validator・one-shot の弱体化案 | Codex 判定→Devin |
| C2 | 保留 | C1 後も listing 支配が残る | scope/権限/契約差の再導出省略 | Codex 判定 |
| B2 | 保留 | B1 後も計算割合が支配 | derive 全体 unlock 案 | Codex 判定 |
| D | 低順位 | complete＋全 covered が実証可能 | 一律延長・no-due 未証明での間引き | Codex 判定 |
| A | 限定条件 | 推論打切りまたは供給不足＋資源余力の証拠 | 一律拡大・複数因子の同時変更 | Codex 判定 |

（draft・B1 追補待ち）
