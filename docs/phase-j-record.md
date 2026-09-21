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

## 6. 未実施・前提（正直な記録）

- **G2 実API評価 実施済み**（2026-09-21）: `python semantic_jev.py --smoke --live` で合成文1リクエストを実 `evaluate()` 経路に送信し厳格検証を通過。結果: `{"ok": true, "model_echo": "jev-1.13.0", "noul": 0.99, "requests": 1}` — wire shape・model echo・noul/choice 検証とも実APIで conform。`/v1/models` listing はこのキーでは空リストを返した（`fixed_model_listed: false`）が、固定modelでの evaluate が成功したため契約上問題なし（listing は設計どおり非権威）。
- **choice 型 wire 契約の実検証**（2026-09-21、候補版マージ後）: 旧クライアントでは claim監査用 choice 質問が `contract_error:http_422` で全滅していた（API は choice 型にも `criteria` を必須とし、応答は `probabilities` キーを返す — 候補版は両方に対応済み）。マージ後コードで実API検証: 幻覚claim「臍下離開部膿瘍があり」→ `not_supported` (conf 1.0)、真正claim「頻脈が持続している」→ `supports` (conf 0.99) と**正しく識別**。旧 `extract_llm` 要約に実在しない「臍下離開部膿瘍」が混入していた実例に対し、Jev がまさにその種の誤りを拒否できることを確認 — 「良いclaimを通す・悪いclaimを止める」両方向の動作を実APIで確認済み。
- **候補版採用**（2026-09-21）: 並行ブランチ `mcs-continuation-20260921`（5コミット、+15k行、336テスト・G1 hash照合PASS・実API検証済みwire契約）を正本としてマージ。先行実装の facade+5モジュール分割は候補版の大規模分割（`semantic_runtime`/`extraction`/`assessment`/`blind`/`evaluation`/`quantities`/`mcs_operations`/`request_loops`/`summary_review`/`hermes_plugin`）に置換。先行側からの移植差分: parked集計分離（`_DeferredSend` 未送信を `skipped` から除外）、JST日次予算境界、verdicts→要約prompt配線、oversize meta永続化、seed() の project_ids スコープ、`models()` transport例外ラップ。閾値順序違反は候補版のfail-closed契約（mode=off）を採用。
- **責務分離リファクタ第2弾**（2026-09-21、マージ後整理）: `semantic.py` 1992行を責務別に分割 — `semantic_policy.py`（定数・config検証、leaf）、`semantic_store.py`（bundle/artifact helpers）、`semantic_llm.py`（prompt・抽出・要約）、`semantic_audit.py`（監査ゲート）、`semantic_render.py`（通知レンダリング）、`semantic_drain.py`（durable job worker 841行）。facade `semantic.py` は 261行（re-export + llm_chat transport + seed/status/CLI）。patch点（`semantic._process_job`/`audit_claims`/`thread_bundle`/`llm_chat`/`LLM_*`）は drain が `semantic.X` 経由で解決するため保持。`__all__` で re-export 面を文書化。併せて dead code 削除（ledger×7, mcs_adapter×2, semantic定数×4等）、`_env()` 3箇所を `mcs_util.env_value` へ、`_json_block` コピーを `mcs_util.json_object` へ統合。
- **LLM prompt品質**: 抽出・要約promptは構造検証済みだが実モデル（Qwen3.5-9B等）での品質は未評価 — shadow観察で人間が audit分布を確認してから assist/enforce へ。
- **degraded/audit notifyの二重送信**: degraded送出後に遅れて PASS した場合、監査済み通知も別 delivery_key で送信され得る（両方とも正確・重複は新着通知とは別eventとして識別される）。
- **tick内 mid-run OFF**: job境界で config reload（`cfg_path` 指定時のみ）。ジョブ内部の外部呼出し途中でのOFF検知は次のjobまで遅延する。
- **RF-OPS**（phase-r-record §8）: 複数tick観察・lock競合・snapshot読取・rollback実演は2026-09-20に実演済み。残るのは長期log/backup観察のみ（機構稼働中・傾向は継続確認）。
- **レビュー第3ラウンド修正**（2026-09-20）: `seed()` の origin を `{"source": ...}` 形に統一（replay由来が payload で識別可能に）。drain優先順位を `payload LIKE` 文字列一致から `json_extract` へ変更（区切り whitespace に非依存）。`payload.targets` 欠落時の root が `target` role を得るよう修正（loop-relation pass が黙って skip されていたedge）。回帰テスト3件追加、計148件パス。
- **レビュー第4ラウンド修正 + 責務分離リファクタ**（2026-09-21）: oversize stub の meta永続化（PENDING再監査で `_input_oversize` を復元し空stubのPASS化を防止）、`verdicts` を要約promptへ実装（`_verdicts_brief`）、`match_threshold > nomatch_threshold` の順序検証追加、notifier の enforce ゲートを `semantic_config` 正規化へ統一、parked semantic intent を `skipped` から分離し `parked` 集計へ（慢性 notify_incomplete の解消）、local LLM呼出しを tick残予算でクリップ、日次予算境界を UTC→JST 0時へ、`models()` の transport例外を JevError 化、`seed()` に project_ids スコープ適用、既存message編集時の再seed（`_upsert_message` が変更検知し3経路の seed に `changed_ids` を合流）、stale/loops持ち越し時の二重 defer を解消（`_process_job` が自己再スケジュールし drain は集計のみ）。モジュール分割: `semantic_model`/`semantic_llm`/`semantic_audit`/`semantic_loops`/`semantic_notice` + facade 化した `semantic.py`。**※この分割構成は同日の候補版マージで置換済み — 現行構成は §1 参照。**回帰テスト追加、計157件パス（semantic系69件）。

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

**ベースライン（2026-09-21 13:50頃）**: jobs done=24/pending=52/failed=0、eligible pending=0、audit = PENDING×4+unparsed×1、findings = `claim_without_evidence`×13・`support_unevaluated`×4・`summary_unavailable`×1、Jev 39/40、extract_llm backlog 残7591。pending の大半は非eligible（trickle/attachment由来）で bundle+assess までで done になる設計 — audit 到達は notify経路の新着のみ。初速所見: ローカルLLMが証拠なしclaimを多発（`claim_without_evidence` が最多）しており、監査が正しく止めている状態。PASS率の実測は新着 eligible job が来てから。
