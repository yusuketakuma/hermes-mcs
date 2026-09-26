# Phase J Record — TypeSafe Jev 意味評価レイヤー実装記録 (spec MCS-REFACTOR-FIRST-20260920)

Status: **候補版マージ済み（`mcs-continuation-20260921` 採用）・G2 実API評価済み / mode=shadow 稼働中（assist/enforce は観察後）**
Recorded: 2026-09-20

## 1. 実装構成（WP対応）

| WP | 内容 | 実装 |
|---|---|---|
| WP-00/01 | config契約・データ契約 | `adapter/semantic_policy.py`: `semantic_config()`（fail-closed検証、`project_ids` は非off時必須）・`policy_fingerprint`。`adapter/semantic_store.py`: bundle/fingerprint/`_current`（prompt文込み） |
| WP-02 | durable job生成 | `ledger._semantic_seed_tx` — `save_patient`/`save_messages`/`save_thread_replies` の `semantic=` flagで通知意図と同一Tx内に `fetch_jobs(kind='semantic')` を生成。**新規保存された全messageが評価対象**（既読到着・backfill・reply-job分を含む — 通知資格とは分離、INV-20）。通知経路seedは `payload.eligible=true` を持ち、mergeで消えない（drain優先度用） |
| WP-03 | Jev client | `adapter/semantic_jev.py`: `JevClient`（固定model `jev-1.13.0`、注入可能 `post_fn`）、P01–P12 proposition registry、`validate_answers` 厳格検証 |
| WP-04 | fact/evidence候補 + summary | `adapter/semantic_llm.py`: `extract_facts`（内部で `adapter/semantic_extraction.py`: `extract_facts_resumable` — chunk単位で永続化・retryは完了prefix再利用）+ `summarize`（共通Claim schema、構造化inputs+Jev verdictsを参考信号としてprompt同梱） |
| WP-05 | claim監査・coverage監査・repair | `adapter/semantic_audit.py`: `audit_code`（参照整合性・span一致・coverage・数量照合）+ `audit_claims`（Jev per-claim choice）+ `adapter/semantic_quantities.py` + 1回限りrepair（`semantic_repair` artifact で永続予約） |
| WP-06 | Open Loop候補 | `adapter/semantic_loops.py`: `update_loops` — `loop_candidate`/`loop_event` artifacts。正式requestへの昇格は既存 `mcs_requests` の human_confirmed 経路のみ |
| WP-07 | renderer + degraded notice | `adapter/semantic_render.py`: `render_notice`（§20.1形式、複数対象のgenerationは `要約対象` 行で当該postを明示）/ `render_degraded`（§19.3 minimal）。shadowでは `notify_plan` artifactのみ、enforceのみ outbox `semantic_notice`（**監査PASS対象ごとに1件**、per-target delivery_key で冪等） |
| WP-07b | 運用機構（候補版追加分） | `adapter/semantic_runtime.py`（job payload/attempt上限）、`semantic_assessment.py`（薬剤detail評価）、`semantic_blind.py`/`semantic_evaluation.py`（盲評価）、`mcs_operations.py`（pause/resume）、`request_loops.py`/`summary_review.py`、`hermes_plugin/`（Discord確認） |
| WP-08 | 評価scaffold | `semantic.py --status`（job集計・audit status分布・loop候補数・日次Jev使用量） |
| WP-09 | runbook/rollback | 本ドキュメント §6–8 |

接続点: `run_check.main` が `semantic=` flagを `stage_unread`/`stage_backfill`/`run_reply_jobs` に通し、`stage_derive` の後・notify flushの前に `semantic.run_due` をdrain（tick・`--jobs-only` 双方）。`mcs_view` に `semantic`/`loops` 読取view追加（snapshot経由のみ）。

## 2. フラグ意味論（spec §22）

| mode | seeding | drain | artifact | outbox | 備考 |
|---|---|---|---|---|---|
| off（既定） | しない | しない | しない | しない | `semantic.seed()` CLIでも拒否。job境界でconfig再検証しOFF反映（AT-060） |
| shadow | する | する | semantic_*/loop_*/notify_plan | **触れない** | 既存 reader（extract_v1/extract_llm/rollup/notify kind）の選択に一切混入しない |
| assist | する | する | 同左 | 触れない | `mcs_view semantic/loops` で人間が読む |
| enforce | する | する | 同左 | `semantic_notice` intent経由 | PASS監査済み要約のみ。対象が保存済み `new_messages` 起点eventに被覆される場合のみ（INV-20/§20.3）。既存sender/fingerprint/receipt機構を再利用（INV-14） |

不正値は全て fail-closed: `mode` 不正→off、`project_ids` 不正→空（処理対象なし）、`model` が固定ID以外→off。

## 3. 不変条件の実装

| INV | 実装 |
|---|---|
| INV-03 ACK非干渉 | semantic jobは `fetch_state`/`mark_read`/`is_unread` に一切触れない（コード上の分離: ledger save系への `semantic` flagはjob行追加のみ） |
| INV-05 最小送信 | `jev_state` は opaque id + body + sender type/profession のみ。患者名・他スレッドは送信しない |
| INV-06 同一Tx | `_semantic_seed_tx` は save_* の `with self.db` 内で実行 |
| INV-07 証拠必須 | `audit_code`: reported_fact claimの `evidence_refs` 空 → `claim_without_evidence`、span不一致→`evidence_span_mismatch`、revision差→`evidence_revision_mismatch` |
| INV-08 未検証の提示禁止 | unverified quoteは `validation_status='unverified'`（span無し=裏付け無しとして記録）。audit PASS外は通知に載らない |
| INV-10 repair 1回限り | `semantic_audit.meta.repair_count` が世代を跨いで永続化 — 再起動後も1回を超えない。job途中defer時も完了済み対象のsummary+auditを先行commitするため再評価・再repairにリセットされない |
| INV-11/19 loop→request自動化禁止 | `loop_candidate` は advisory artifactのみ。正式requestは `mcs_requests` の既存人為確認経路 |
| INV-13 local LLM隔離 | `llm_chat` は loopback固定・`no_proxy_opener(NoRedirect)`・tool無し |
| INV-14 確定payload | `semantic_notice` payloadに最終テキストを凍結格納。senderは再生成しない |
| INV-15 fingerprint | `bundle_fingerprint` = canonical(member revisions + model + registry + schema + policy)。`_current` はfp一致artifactのみ現行扱い |
| INV-16 shadow非干渉 | artifact kindは `semantic_*`/`loop_*`/`notify_plan` のみ — 既存readerのkind選択に混入しない。outboxはenforceのみ |
| INV-20 通知資格と起点の分離 | `semantic_notice` のenqueue条件は `_notify_src_event` が**対象messageごとに** outbox 内の保存済み `new_messages` event被覆を確認すること（§20.3「保存済み起点eventから決める」）。history_import／replay・既読到着のみで保存されたmessageはartifactのみ。送信時にもnotifierが `src_event_id` の生存・非suppressを再検査 |
| INV-21 stale昇格禁止 | commit直前にbundle fp再計算+job行生存確認。不一致→全出力 `STALE` 記録+job defer（再評価） |

## 4. AT対応テストマップ

| AT | test |
|---|---|
| AT-011 | `test_registry_scope_is_explicit` |
| AT-012 | `test_noul_without_confidence_ok` |
| AT-013 | `test_noul_type_confusion_rejected` |
| AT-014 | `test_choice_validation` / `test_answer_set_mismatch_rejected` |
| AT-015 | `test_retryable_status_backoff` |
| AT-016 | `test_nonretryable_fail_fast` / `test_no_api_key` |
| AT-019 | `test_bundle_fixed_shape` |
| AT-035 | `test_stale_generation_not_promoted` |
| AT-049 | `test_shadow_no_outbox` / `test_degraded_notice_enforce_only` |
| AT-053 | `test_off_no_seed_no_drain` |
| AT-055 | `test_history_import_never_notifies` / `test_replay_of_imported_message_no_notice` / `test_arrival_merge_keeps_eligibility` / `test_notice_suppressed_when_src_event_suppressed` |
| AT-058 | `test_pending_audit_retries_then_passes`（途中監査保留はbounded retry）／`test_pending_assess_artifact_dedup`（同一wait-stateは重複記録しない） |
| AT-059 | `test_replay_dedup_current_fp` |
| AT-060 | `test_off_flip_mid_drain` |
| AT-064 | `test_model_mismatch_rejected` / `test_config_fail_closed`（model検査） |
| AT-065 | `test_snapshot_view_semantic` |
| AT-067 | `test_config_fail_closed` / `test_daily_budget_exhausted` |
| INV-06 | `test_ingest_seeds_semantic_job` / `test_no_semantic_flag_no_job` |
| INV-07 | `test_claim_without_evidence_audit` / `test_evidence_span_locate` |
| INV-10 | `test_repair_once_then_needs_review` |
| INV-11/19 | `test_loop_candidate_no_request_write` |
| INV-14 | `test_notifier_semantic_notice_render` |
| INV-15 | `test_fingerprint_covers_revisions` |
| INV-16 | `test_shadow_full_pipeline`（全kind存在+outbox未接触） |
| — 第2レビュー修正 | `test_enforce_enqueues_notice_per_target`（複数対象のclaimが全て届く）/ `test_backfill_seeds_read_arrivals` / `test_reply_save_seeds_read_replies`（既読・import分も全coverage対象）/ `test_oversized_response_fails_fast`（非retryable即中断）/ `test_quantity_untraced_needs_quote`（AT-025/026厳格化）/ `test_deferred_run_preserves_completed_results`（defer時の部分commit+repair budget永続化）/ `test_project_filter_defers_not_starves`（queue公平性）/ `test_loop_candidate_records_history`（§17.3 history導出+occurred_at） |

## 5. 検証証拠

| 検証 | 結果 |
|---|---|
| 全テスト | **345 passed**（adapter suite 全体、候補版マージ後） |
| lint | 新規コード0件（16件全てbaseline E702/E741/F401 — `git stash` で baseline と完全一致を確認済み） |
| OFF regression | `mode:"off"` で job生成0・drain即return・非semantic系88件のPhase R系テスト全パス |

## 6. 検証記録と未確認範囲

以下は当時の作業記録を技術面に限定して整理したものです。実データ由来の
個別識別子、投稿本文、診療情報、利用実績、稼働設定値は含めていません。
この整理に際して試験・実API通信・実データ照会を新たに実施してはいません。
過去の結果を現在のコードや配備状態の検証成功として扱わないでください。

### 記録されている技術検証

- **G2の限定疎通確認**：合成文を使用する `semantic_jev.py --smoke --live`
  の実API実行が記録されています。要求・応答の契約検査、固定モデルの
  応答識別、確率値の検査を通過したという記録です。モデル一覧取得と
  固定モデルでの評価要求は別の確認として扱っています。
- **choice型の契約修正**：要求側の必須 `criteria` と応答側の
  `probabilities` に対応しました。対応後に支持・非支持の双方の応答を
  確認したという記録があります。この確認に使用された個別の入力内容は
  本書に保持せず、合成試験だったとの説明も付加しません。
- **候補版採用時の回帰確認**：候補ブランチ採用に伴う回帰試験、保存済み
  ソースhashの照合、既存の読取り・通知・予算・scope契約の確認が記録
  されています。結果は当時の候補版に対するもので、現在のtree全体へ
  引き継がれるものではありません。
- **責務分離後の接続確認**：policy、保存、LLM、監査、描画、drainの分割と、
  テストが使用するpatch箇所の維持が記録されています。先行する分割構成は
  候補版採用時に置き換えられており、当時の構成図を現行配置とみなしません。
- **追加回帰の対象**：seedのscope・origin、job優先順位、対象role、
  過大入力の保留、要約への評価結果の受渡し、閾値の順序検証、通知保留の
  集計、残り時間によるLLM呼出し制限、日次予算の時刻境界、transport例外、
  原文更新時の再seed、重複した再スケジュールの抑制を確認した記録があります。
- **運用機構の確認**：複数tickの観察、lock競合、snapshot読取り、当時の
  復旧手順の実演が別のPhase R記録にあります。対象版・条件を限定した
  証拠であり、現行環境の復元可能性や長期運用の健全性を保証しません。

### 未確認事項と結果の限界

- 限定的なwire疎通や支持・非支持の応答確認は、臨床的な意味精度、抽出の
  網羅性、実運用での誤判定率、人間ラベルを用いた品質評価の代わりには
  なりません。promptの構造検査も実モデル品質の保証にはなりません。
- 当時の要求予算は観測結果に基づき調整されていますが、本書は実績値や
  稼働値を保持しません。現在の費用上限や適切な予算を示す記録ではありません。
- 縮退通知の後に監査が通過した場合、別の配送識別子で追加通知され得る
  という設計上の留意事項が記録されています。配送のexactly-onceを
  保証したという検証ではありません。
- 当時の停止反映にはjob境界という制約が記録されています。後続実装で
  変更され得るため、現在の通信停止タイミングは現行コードと対応する
  試験で確認する必要があります。
- 長期のログ・backup保持、現在のサービス配置・権限、実通知、現行版での
  復旧演習は、この記録の整理によって新たに確認されたものではありません。

## 7. 有効化手順（OFF→shadow→assist→enforce）

`~/.mcs/config.json` に `"semantic"` block を追加:

```json
"semantic": {
  "mode": "shadow",
  "daily_request_budget": 40,
  "project_ids": null
}
```

- `TYPESAFE_API_KEY` は `~/.mcs/.env` または `~/.hermes/.env` に配置（payload/logには書かない）。
- `daily_request_budget: 0` は Jev呼出し禁止（local LLMのみ・assess pending で deferred）。
- 順序厳守: **shadow で `python semantic.py --status` の audit分布を観察 → assist で `mcs_view semantic --message-id` を人間が読む → enforce は両者が安定してから**。enforce到達前に `semantic_summary` PASS率と degraded送出率を確認すること。
- 停止は `"mode":"off"` のみで即時（次job境界から通信・seedingとも停止、pending jobは保存されたまま再開可能）。

## 8. rollback

- コード: `git revert <phase-j commits>`。DB schema変更なし（artifacts/fetch_jobs/notify_outbox 既存表のみ使用）のためrevertで完全に戻る。
- データ: `semantic_*`/`loop_*`/`notify_plan` artifact行は残るが非選択kindのため全readerが無視。除去する場合: `DELETE FROM artifacts WHERE kind IN (...)` — 任意操作で必須ではない。
- 送信済み `semantic_notice` は outbox `accepted` で履歴に残る — 取消不可（物理送信済みのため）。enforce移行は上記ゲート確認後のみ。

## 9. Blocker

- ~~実API wire shape の確認（G2）~~ → **2026-09-21 実施・通過**（§6 G2 項参照）。
- shadow期間中の audit分布観察（PASS率・NEEDS_REVIEW理由）が assist/enforce の実質的判定材料。2026-09-21 に `mode: shadow, daily_request_budget: 40` で稼働開始。

### 観察方法（shadow期間中の日次確認）

```bash
cd ~/.mcs/adapter && python3 semantic_observe.py          # 人間向けスナップショット
python3 semantic_observe.py --json >> ~/.mcs/data/observe.jsonl  # 時系列ログ
```

時系列は `data/run.log` の各 tick の `semantic` フィールド（done/deferred/left/jev_requests）でも追える。

## 10. 能力向上計測基盤（2026-09-21 追加）

`semantic_bench.py` — prompt/モデル変更を同一コーパスで A/B 計測する自動メトリクス:

```bash
python3 semantic_bench.py corpus --n 20 --out bench_corpus.json   # 実データ凍結
python3 semantic_bench.py run --corpus bench_corpus.json --tag A --out rA.json
python3 semantic_bench.py report rA.json rB.json                  # findings率の差分
python3 semantic_bench.py detect                                  # Jev検知プローブ(実API)
python3 semantic_bench.py calibrate                               # 閾値校正用分布
```

- **corpus/run**: bundle 凍結済みケースに extract_facts→summarize→audit_code を実行し、
  finding code 率を集計。人手採点なしで prompt/モデル変更の回帰を比較できる。
  `--jev` 指定で audit_claims 実呼出も追加（予算内）。
- **detect**: 合成不良ケース（否定反転・用量違い・時制錯誤・主体混同・無関係根拠 +
  陽性対照2件）を audit_claims に実APIで通し検知率を計測。
  **初回結果 9/9**（TP=7 FP=0 FN=0 TN=2、9リクエスト消費）— 否定反転・用量違い・
  時制錯誤・主体混同・無関係根拠・頻度違い・相対日付の全不良クラスを検知し、
  正しい claim 2件は通過。基本失敗クラスの検知能力は既に十分。
- **calibrate**: `semantic_audit` artifact の `meta.claim_audit`（drain が per-claim の
  choice+confidence を蓄積開始）の分布と match_threshold what-if スイープ。
- `semantic_observe.py` に `repaired:` 行を追加 — repair 経由で PASS 回復した率を観測。

### 実施済み改善（claim_without_evidence 対策）

1. `_locate_quote` に空白除去フォールバック — モデルが数字の周りに空白を挿入して
   引用する実態（`9 時から` vs 原文 `9時から`）をカバー。空白除去後の一意マッチを
   verbatim span に解決（`body[s:e]==quote` を維持）。
2. `semantic_extraction` は span 発見時に quote を原文 verbatim に置換。
3. `_SUMMARY_PROMPT` に2ルール追加: 「根拠候補なしの reported_fact は claim 化せず
   limitations へ」「claim は参照候補 statement の言い換えに留める」。

### A/B 実測（同一コーパス20件・実メッセージ）

| 指標 | baseline | v2（改善後） |
|---|---|---|
| findings/claim | 1.407 | **0.340** |
| claim_without_evidence | 24 | **3** |
| claim_quantity_unverified | 14 | 7 |
| fact_dropped | 0 | 4 |
| quantity_untraced / mismatch | 0 | 2 / 1 |

`claim_without_evidence` 88%減。新出の `fact_dropped` は「根拠なしclaimを落とした
結果、参照されない候補が残る」正常な副産物。残る `claim_quantity_unverified` は
claim数量と証拠quoteの対応づけ精度の課題（次段）。

別途判明: 抽出の `model` 失敗が 10/20 件 — llama.cpp 温度0でもバッチ並行で揺れ、
slot 1 backlog 処理との競合下で 90s timeout に達するケースがある。drain は
retryable として再試行する設計のため運用上は滞留のみ。

### モデルA/B手順（未実施・要サービス再起動）

代替 gguf を `~/.hermes/models/` に配置 → `ai.hermes.llamacpp.plist` の `-m` を切替 →
`launchctl kickstart -k gui/$(id -u)/ai.hermes.llamacpp` → 同一コーパスで
`semantic_bench.py run` を新旧で実行し `report` 比較。候補は Q5_K_M（品質↑・
RAM+~1GB・速度↓）。再起動中は backlog lane が止まる点に注意。

**ベースライン（2026-09-21 13:50頃）**: jobs done=24/pending=52/failed=0、eligible pending=0、audit = PENDING×4+unparsed×1、findings = `claim_without_evidence`×13・`support_unevaluated`×4・`summary_unavailable`×1、Jev 39/40、extract_llm backlog 残7591。pending の大半は非eligible（trickle/attachment由来）で bundle+assess までで done になる設計 — audit 到達は notify経路の新着のみ。初速所見: ローカルLLMが証拠なしclaimを多発（`claim_without_evidence` が最多）しており、監査が正しく止めている状態。PASS率の実測は新着 eligible job が来てから。
