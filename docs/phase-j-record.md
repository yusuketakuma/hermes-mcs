# Phase J Record — TypeSafe Jev 意味評価レイヤー実装記録 (spec MCS-REFACTOR-FIRST-20260920)

Status: **実装済み（mode=off が既定）/ G2 実API評価・本番観察は未実施**
Recorded: 2026-09-20

## 1. 実装構成（WP対応）

| WP | 内容 | 実装 |
|---|---|---|
| WP-00/01 | config契約・データ契約 | `adapter/semantic.py`: `semantic_config()`（fail-closed検証）、bundle/Fact/Claim/Audit validators・fingerprint |
| WP-02 | durable job生成 | `ledger._semantic_seed_tx` — `save_patient`/`save_messages`/`save_thread_replies` の `semantic=` flagで通知意図と同一Tx内に `fetch_jobs(kind='semantic')` を生成。**新規保存された全messageが評価対象**（既読到着・backfill・reply-job分を含む — 通知資格とは分離、INV-20）。通知経路seedは `payload.eligible=true` を持ち、mergeで消えない（drain優先度用） |
| WP-03 | Jev client | `adapter/semantic_jev.py`: `JevClient`（固定model `jev-1.13.0`、注入可能 `post_fn`）、P01–P12 proposition registry、`validate_answers` 厳格検証 |
| WP-04 | fact/evidence候補 + summary | `semantic.extract_facts`（local LLM + quote→codepoint span照合）、`summarize`（共通Claim schema） |
| WP-05 | claim監査・coverage監査・repair | `audit_code`（参照整合性・span一致・coverage）+ `audit_claims`（Jev per-claim choice）+ 1回限りrepair |
| WP-06 | Open Loop候補 | `update_loops` — `loop_candidate`/`loop_event` artifacts。正式requestへの昇格は既存 `mcs_requests` の human_confirmed 経路のみ |
| WP-07 | renderer + degraded notice | `render_notice`（§20.1形式、複数対象のgenerationは `要約対象` 行で当該postを明示）/ `render_degraded`（§19.3 minimal）。shadowでは `notify_plan` artifactのみ、enforceのみ outbox `semantic_notice`（**監査PASS対象ごとに1件**、per-target delivery_key で冪等） |
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
| 全テスト | **145 passed**（`pytest test_mcs_semantic.py test_mcs_ingestion.py test_mcs_features.py -q`、うちsemantic系57件） |
| lint | 新規コード0件（16件全てbaseline E702/E741/F401 — `git stash` で baseline と完全一致を確認済み） |
| OFF regression | `mode:"off"` で job生成0・drain即return・非semantic系88件のPhase R系テスト全パス |

## 6. 未実施・前提（正直な記録）

- **G2 実API評価 未実施**: wire shape `{"model","state","questions"}→{"model","answers":{qid:{...}}}` は mock のみで検証。実TypeSafe Jev APIの契約は `semantic_jev` 冒頭に assumption として明記。`JevClient.models()` は alias listingのみ — 固定versionの利用可否は承認済み synthetic-input smoke call で確認すること（alias自動切替は行わない）。
- **LLM prompt品質**: 抽出・要約promptは構造検証済みだが実モデル（Qwen3.5-9B等）での品質は未評価 — shadow観察で人間が audit分布を確認してから assist/enforce へ。
- **degraded/audit notifyの二重送信**: degraded送出後に遅れて PASS した場合、監査済み通知も別 delivery_key で送信され得る（両方とも正確・重複は新着通知とは別eventとして識別される）。
- **tick内 mid-run OFF**: job境界で config reload（`cfg_path` 指定時のみ）。ジョブ内部の外部呼出し途中でのOFF検知は次のjobまで遅延する。
- **RF-OPS残項**（phase-r-record §8 引継ぎ）: 複数tick観察・lock競合実演・rollback実演・長期log観察は本番運用で実施予定。

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

- 実API wire shape の確認（G2）。mock検証のみでは enforce 到達不可。
- shadow期間中の audit分布観察（PASS率・NEEDS_REVIEW理由）が assist/enforce の実質的判定材料。
