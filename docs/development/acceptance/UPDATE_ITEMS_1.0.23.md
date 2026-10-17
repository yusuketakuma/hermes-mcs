# 1.0.23 取り込み対応表（更新項目 → 成果物、導入・更新・復旧の実入口）

2026-10-17。作業branch `release/v1.0.23-prep`、未コミット差分を含む作業ツリーの読取り記録。
判定の正本は [受入票](ACCEPTANCE_1.0.23.md)。本表は「どの項目がどのコード・テスト・設定・資産・文書・配布物に対応するか」と
「導入・更新・復旧・配布・起動の実入口がこの版でどう変わり、何で検証したか」を1か所で追跡する。
品質合格・実環境受入・公開可否を意味しない。実データ・実サービス・実LLM・本番配備は未実施。

状態の語: **成功**（合成fixtureで検証）、**失敗**、**未確認**（検証未実施または環境不足）、**対象外**（この版で変更なし・該当なし）。
環境不足や権限不足は成功へ置き換えない。

## 1. 導入・更新・復旧・配布・起動の実入口

| 入口 | 役割 | 1.0.23での変更 | 検証 | 状態 |
|---|---|---|---|---|
| `install.sh` | 新規導入（`--preflight` / `--mode hermes\|standalone` / `--recovery-python`）。段階6で復旧ツールを repo 外へ配置 | 変更なし | `tests/meta/test_install_sh*.py`・`test_install_local.py`・`test_install_runtime_migration.py`（合成） | 対象外（回帰は最終差分で再実行） |
| `scripts/mcs_upgrade.py` | 更新の起動役。TARGET tag の updater を現 checkout に対して実行。stdlib のみ | 変更なし | `tests/ops/test_mcs_upgrade.py` | 対象外 |
| `mcs/ops/mcs_update.py plan` | 読取専用の更新計画（阻害要因・schema_bump） | 変更なし | `tests/ops/test_supported_update_paths.py`（v1.0.0〜v1.0.22 → 候補、fixture は `tests/fixtures/schema_upgrade/`） | 成功（Codex 側: schema/更新364件成功） |
| `mcs/ops/mcs_update.py apply` | 停止 → backup → merge → post-merge → 再起動。失敗時は `_rollback_tree` で自動巻戻し | `_rollback_tree` 先頭に layout 下限ガード（§2 項目2） | `tests/ops/test_mcs_update.py`・`test_update_*.py`・`test_mcs_update_rollback_floor.py` | 成功（216件、Python 3.13。3.14 は §4） |
| `mcs/ops/mcs_update.py rollback` | 直前 applied の prev_sha へ戻す（人の指示） | journal/quiesce 前に `_rollback_backup_error` と同列で layout 下限ガードを評価。拒否は `rollback_failed` receipt と通知のみで副作用なし | 同上 | 成功 |
| `mcs/ops/mcs_update.py recover` / `recover_interrupted` | 中断 apply/rollback の収束。rollback 形 journal は `_rollback_tree` 経由 | `_rollback_tree` のガードで網羅 | `tests/ops/test_update_recovery_stability.py`・`test_update_rollback_guards.py` | 成功 |
| `deployment/recovery/mcs_recover.py` + `org.mcs.recovery.plist`（`--if-stale`、900秒） | repo 外の独立 watchdog。更新で壊れた新版から復旧経路を守る | 変更なし。reset は head==target / head==prev への同一コード再整列と `merge --abort`（新コード未実行）のみで、新コードから旧コードへ移す経路が無いため layout ガード不要（§2 項目2 の確認結果） | `tests/meta/test_mcs_recover*.py`・`test_recovery_*.py`・`integration/test_mcs_recovery_narrative.py` | 対象外（確認済み） |
| `deployment/scripts/mcs_update.sh` | launchd から `mcs_update.py check` を定期実行 | 変更なし | `tests/ops/test_mcs_cli.py` | 対象外 |
| `deployment/launchagents/*.plist`（extract-drainer×2、llamaserver、standalone、cmd、health、int、recovery） | 定期ジョブ・worker の起動 | 変更なし。manifest 照合は `_reconcile_membership` | `tests/ops/test_update_membership_shape.py`・`test_lifecycle_standalone.py` | 対象外 |
| `mcs/ops/mcs_setup.py services` / `check` / `doctor` | 配備・診断 | `doctor` に worker 所有者・起動証跡・layout 件数の表示（Codex） | `tests/ops/test_mcs_setup_doctor.py`・`test_mcs_setup.py`（Python 3.14 必須） | 成功（Codex 側 313件）。3.13 venv では `requires Python >= 3.14` で不成立＝環境要件であり失敗ではない |
| Hermes gateway 起動（`hermes_plugin/card_workers.py`） | Slack/Discord worker の起動。所有者 profile 不明・不一致は起動拒否 | `_owner_identity` / `_owner_allowed`（Codex） | `tests/plugin/test_card_worker_owner.py`、固定SDK lane（§3） | 成功（局所）／固定SDK lane は fixture 1件不足 |
| `mcs_standalone/` | Hermes 無しの単一 host | 変更なし | `tests/ops/test_mcs_setup_standalone*.py`・`integration/test_standalone_*.py` | 対象外 |
| `lineworks_adapter/` → `adapters/lineworks/` | 独立 LINE WORKS adapter | 配送側: 要約待機・改訂上限・同一カード token 再利用（Codex） | `tests/adapters/lineworks/`・`tests/notify/test_notify_lineworks.py` | 成功（局所） |
| 配布物 `resources/drug-master/` | 公式 master 原本と manifest | importer 変更後に bundle 再検証（Codex） | 受入票「公式薬剤master」行 | 成功（ZIP SHA 一致、activation=false） |
| 更新時の運用手順 `docs/guides/UPGRADE_AGENT.md` §1.0.23 | AI/人向け更新手順 | layout3 後の rollback 制約、全 profile 再起動、hold 非解除を追記（Codex） | `readme_release.py --check`（最終差分で） | 未確認（最終文書整合待ち） |

### rollback 下限ガードの帰結（利用者影響）

v1.0.23 は schema 変更を伴わない（`update-paths.json`: v1.0.22 と shape-11 を共有）。
したがって rollback で更新前 DB の自動復元は無く、layout3 のカードが1件でも作成された後は v1.0.22 以前への rollback を
恒久的に拒否する。出口は「v1.0.23 に留まる」か「人承認の restore 手順で更新前 backup を復元してから戻す」。
旧コードの直接起動・DB 直接書換えによる回避は手順として提供しない。schema 変更を伴う更新では復元される backup 側の内容で判定する。
記録: `changes/updater-rollback-layout-floor.json`。

## 2. レビュー項目 1〜32 の対応

| 項目 | 内容（受入票の要約） | コード | テスト | 設定・資産・文書・変更記録 | 状態（受入票） |
|---|---|---|---|---|---|
| 1 | 既存 layout 固定・移行対象の読取診断 | `mcs/notify/notify_cards.py` `layout_migration_counts`（`health.json` の `cards.layout_inventory`） | `tests/notify/test_card_layout.py` | `changes/delivery-layout-safety.json`、UPGRADE_AGENT §1.0.23 | 実装・局所検証済み |
| 2 | rollback 時の再投稿防止 | `mcs/ops/mcs_update.py` `_target_card_layout` / `_rollback_layout_error`（`rollback()` 前判定と `_rollback_tree` backstop） | `tests/ops/test_mcs_update_rollback_floor.py`（7件） | `changes/updater-rollback-layout-floor.json`、UPGRADE_AGENT §1.0.23・§7 | 実装・局所検証済み（§1 の全呼出元確認を含む） |
| 3 | worker 所有者と起動証跡の doctor/health 診断 | `mcs/views/card_worker_status.py` `record`/`status`/`diagnose`、`mcs/ops/mcs_setup.py` `cmd_doctor` | `tests/ingest/test_card_worker_status.py`、`tests/ops/test_mcs_setup_doctor.py` | `changes/worker-owner-evidence.json` | 実装・局所検証済み |
| 4 | 所有者不明時の拒否・固定SDK 属性契約 | `hermes_plugin/card_workers.py` `_owner_identity`/`_owner_allowed` | `tests/plugin/test_card_worker_owner.py`、`integration/test_hermes_slack.py` | `changes/slack-profile-worker.json` | 実装済み・SDK 検証: §3（fixture 不足1件） |
| 5 | LINE WORKS 文書・画面の整合 | — | `scripts/development/generate_lineworks_gallery.py --check` | `docs/guides/LINEWORKS.md`、`docs/screenshots/lineworks-gallery/01-delivery.*`・`02-task-confirm.png`・`assets.json` | 生成 check・目視済み |
| 6 | 1.0.23 更新手順 | — | `readme_release.py --check`（最終） | `docs/guides/UPGRADE_AGENT.md` §1.0.23 | 更新済み・updater 最終契約と照合待ち |
| 7 | 1.0.22 からの更新 fixture | `mcs/ops/mcs_update.py plan/apply` | `tests/ops/test_supported_update_paths.py`、`tests/ops/test_release_origin_tick.py` | `tests/fixtures/schema_upgrade/update-paths.json`・`origins.json`（v1.0.22 = f8c6c20d を追加） | 364件成功・tick は編集静止後 |
| 8 | 名前付き受入ゲートと固定候補 | — | — | `ACCEPTANCE_1.0.23.md`、本表 | 追跡中 |
| 9 | 待機理由行列・Discord 所有者照合 | `mcs/ingest/health_watch.py` `_wait_label`/`_wait_summary`/`_reason_rows`、`hermes_plugin/card_workers.py` | `tests/ingest/test_health_watch_wait_reasons.py`、`tests/plugin/test_card_worker_owner.py` | `changes/delivery-wait-progress.json` | 実装・局所検証済み |
| 10 | LINE WORKS 要約待機（最大120秒）・公開条件・改訂上限（最大3件） | `mcs/notify/present_lineworks.py`、`adapters/lineworks/delivery.py`、`mcs/notify/notify_cards.py` `_lineworks_update_budget` | `tests/notify/test_notify_lineworks.py`、`tests/notify/test_delivery_v1023.py` | `changes/delivery-layout-safety.json` | 実装・最終局所検証中 |
| 11 | 候補指紋と独立人承認 receipt | `mcs/semantic/semantic_candidate.py` `candidate_fingerprint`/`promotion_problem`、`mcs/semantic/semantic_acceptance.py` | `tests/semantic/test_sealed_candidate.py`、`tests/semantic/test_fact_source.py` | `changes/sealed-candidate-evaluation.json`、`docs/specs/SEALED_CANDIDATE_EVALUATION.md` | 実装・局所検証済み |
| 12 | 実データと合成の品質証拠区別 | `mcs/semantic/eval/semantic_evaluation.py`・`semantic_snapshot.py` | `tests/semantic/test_semantic_evaluation.py` | `docs/specs/semantic-evaluation.md`、`evaluation/request_following_review_manifest.json` | 実装・局所検証済み |
| 13 | holdout 封印・split・blind 二重評価 | `mcs/semantic/eval/semantic_holdout.py` `holdout_payload`、`semantic_snapshot.py` | `tests/semantic/test_sealed_candidate.py` | `docs/specs/SEALED_CANDIDATE_EVALUATION.md` | 実装・局所検証済み |
| 14 | 分母・Wilson・緊急度/AI優先・A/B 基準 | `mcs/core/stats_math.py` `wilson_interval`、`mcs/semantic/semantic_policy.py` | `tests/semantic/test_semantic_evaluation.py` | `evaluation/g6-criteria-v2.json`、`ci/gates.py` | 実装・追補検証中 |
| 15 | 私有 snapshot のローカル評価 runner | `mcs/semantic/eval/semantic_snapshot.py` | 同上 | `docs/specs/semantic-facts-v2-rollout.md` | 実装済み・実データ/モデル受入未実施 |
| 16 | 薬剤言及履歴と取得範囲 | `mcs/views/drug_timeline.py`、`mcs/ops/mcs_drug.py` | `tests/views/test_drug_timeline.py` | `changes/drug-timeline-alias-workflow.json`、`docs/specs/DRUG_MASTER_WORKFLOWS.md` | 実装・局所検証済み |
| 17 | 別名提案・承認・rollback | `mcs/ops/drug_alias.py`、`mcs/ops/mcs_drug.py`（alias-propose / alias-receipt） | `tests/ops/test_drug_alias.py` | 同上（`--confirm-human`・reason・command-id 必須） | 実装・局所検証済み |
| 18 | 自動 export 禁止・私有表示境界 | `mcs/views/drug_reconciliation.py`・`drug_timeline.py`（読取のみ、追加取得・export なし） | `tests/views/test_drug_reconciliation.py` | `docs/specs/external-export-contract.md`、`changes/drug-history-reconciliation.json` upgrade | 局所検証済み |
| 19 | 復元 collect-only・配送 hold の可視化 | `mcs/ops/mcs_backup.py`（drill）、`mcs/views/card_worker_status.py` | `tests/ops/test_mcs_backup.py` | `changes/backup-evidence-separation.json`、`docs/guides/BACKUP.md`・`NOTIFICATION_RECOVERY.md` | 実装・局所検証済み |
| 20 | RPO 鮮度・年齢/遅延の区別・override なし | `mcs/ops/mcs_backup.py` | 同上 | `docs/guides/BACKUP.md` | 実装・文書化済み |
| 21 | offsite/verify/escrow 証跡分離 | `mcs/ops/mcs_backup.py` `offsite`/`keygen`（key-fd・Keychain） | 同上（50件） | `changes/backup-evidence-separation.json` | 実装済み・最終差分再検証待ち |
| 22 | 全 profile 再起動と稼働証拠 | `mcs/views/card_worker_status.py`、`mcs/ops/mcs_setup.py doctor` | `tests/ops/test_mcs_setup_doctor.py` | UPGRADE_AGENT §1.0.23（`worker_started` 確認） | 診断実装済み・更新経路確認中 |
| 23 | 表示変更の分類・更新注意 | — | `release_notes.py check`（最終） | `changes/*.json` の category/upgrade（表示変更は `changed`、gateway 再起動を明記） | 最終文書整合待ち |
| 24 | clean install・22→23 plan/apply | `install.sh`、`mcs/ops/mcs_update.py` | `tests/meta/test_install_sh*.py`、`tests/ops/test_supported_update_paths.py` | §1 | 更新回帰済み・配布経路確認中 |
| 25 | standalone 所有判定と表示 | `mcs/views/card_worker_status.py`、`mcs/ops/mcs_setup.py` | `tests/ops/test_mcs_setup_standalone.py` | — | 実装・局所検証済み |
| 26 | layout 変化で保留/上限を解除しない | `mcs/notify/notify_cards.py`（`_cleanup_budget_held`、`_pending_reason`） | `tests/notify/test_delivery_v1023.py` | `changes/delivery-layout-safety.json` | 実装・局所検証済み |
| 27 | LINE WORKS 同一カード/トークン再利用 | `mcs/notify/notify_cards.py` `_mint_token`、`adapters/lineworks/delivery.py` | `tests/notify/test_notify_lineworks.py`、`tests/adapters/lineworks/test_registry_delta.py` | 同上 | 実装・局所検証済み |
| 28 | 要約/スタンプと本文 chunk 分離（新規 layout3） | `mcs/notify/notify_cards.py` `_split_body_chunks`、`mcs/notify/notify_render.py`、`adapters/slack/cards.py` `thread_part_blocks`、`adapters/slack/delivery.py` `_blocks_call`、`adapters/discord/cards.py`・`delivery.py` | `tests/notify/test_notify_parts.py`・`test_stamps_display.py`、`tests/adapters/slack/test_slack_thread_blocks.py`、`tests/adapters/discord/test_discord_attachment_spoiler.py` | `changes/thread-post-summary-order.json`・`ui-slack-thread-collapse.json`・`ui-discord-attachment-spoiler.json`、`docs/screenshots/slack-gallery/01-overview.*` | 新規 layout3 で実装・旧 layout1/2 固定 |
| 29 | 要約有界待機・改訂上限・時計だけの再送抑制 | `mcs/notify/notify_cards.py` `_lineworks_update_budget`/`_pending_reason` | `tests/notify/test_delivery_v1023.py` | `changes/delivery-layout-safety.json` | 実装・局所検証済み |
| 30 | 配送進捗中の誤った停滞警報抑制 | `mcs/ingest/health_watch.py`、`mcs/ingest/run_check.py` | `tests/ingest/test_health_stalls.py` | `changes/delivery-wait-progress.json` | 合成300件 backlog 検証済み |
| 31 | unknown part の plan・承認・矛盾検査・receipt | `mcs/views/mcs_view.py` `card_part_plan`、`ops.card_part_resolve` receipt、`mcs/notify/notify_cards.py` | `tests/notify/test_card_part_resolve.py`、`tests/views/test_card_part_cli.py` | `changes/card-part-recovery-cli.json`、`docs/guides/NOTIFICATION_RECOVERY.md` | 実装・合成検証済み（チャットへ操作公開なし） |
| 32 | 部品上限の可視化・原文保持 | `mcs/notify/notify_cards.py` `THREAD_PART_LIMIT`/`_split_body_chunks` | `tests/notify/test_notify_parts.py` | `changes/delivery-layout-safety.json` | 実装・局所検証済み |

### 医薬品マスター活用 D1〜D5

| ID | コード | CLI（`mcs/ops/mcs_drug.py`） | テスト | 文書・変更記録 |
|---|---|---|---|---|
| D1 | `mcs/extract/drug_map.py`、`mcs/views/drug_timeline.py` | check-surface / status | `tests/ops/test_drug_master_reference_workflows.py`、`tests/views/test_drug_timeline.py` | `docs/specs/DRUG_MASTER_WORKFLOWS.md`、`changes/drug-master-reference-workflows.json` |
| D2 | `mcs/extract/drug_map.py`（規格/剤形/単位） | check-surface | 同上 | 同上 |
| D3 | `mcs/ops/import_drug_master.py`、`mcs/views/drug_master_compare.py` | master-impact / install-bundled | `tests/ops/test_mcs_drug_master_cli.py` | 同上、`resources/drug-master/` manifest |
| D4 | `mcs/views/drug_timeline.py` | （timeline） | `tests/views/test_drug_timeline.py` | `changes/drug-history-reconciliation.json` |
| D5 | `mcs/views/drug_reconciliation.py` | （reconcile） | `tests/views/test_drug_reconciliation.py` | 同上 |

いずれも処方正本・臨床同等性・服用中を自動確定しない。別名適用は `--confirm-human`・reason・command-id を必要とする（項目17）。

## 3. 固定 Hermes SDK lane（`sh scripts/check_pinned_sdks.sh hermes`）

| 項目 | 内容 |
|---|---|
| 固定 commit | `f36c1da0016ce14c9c531cb9514ab4b0b667240d`（CI pin。fork branch `mcs-pin/v0.21.7-20261014` で公開を確認） |
| 実行環境 | 作業ツリー外の隔離 clone、Python 3.14 venv、`[messaging]` + dev group + pytest 9.1.1。生サービス接続なし |
| 結果 | 20 passed / 1 failed |
| 失敗 | `integration/test_hermes_slack.py::test_real_hermes_discovery_keeps_slack_inert_until_opt_in`。test の host（素の `SimpleNamespace`）に `_owner_profile` が無く、項目4の所有者判定が `owner_unverified` で worker 起動を拒否。固定 Hermes 本体は `gateway/platforms/base.py` で `_owner_profile` を初期化・設定するため実 adapter は通る。fixture 側へ `_owner_profile="cco"` を与える修正が必要（guard 所有者側） |
| 限界 | `MCS_HERMES_SOURCE` と `MCS_HERMES_REFERENCE` を同一 clone にしたため pin 一致比較は自明。証明範囲は「plugin が固定 commit 上で動く」まで |

## 4. 本表の検証記録と未確認事項

| 検証 | 結果 |
|---|---|
| `tests/ops/test_mcs_update_rollback_floor.py` + `test_mcs_update.py` + `test_update_rollback_guards.py`（Python 3.13） | 216 passed |
| `tests/ops/` 全体（Python 3.13 venv） | 98 failed / 3015 passed。失敗は `mcs_setup requires Python >= 3.14` 由来（setup/doctor/drug_map/cli/origin_tick）で、更新・復旧経路の失敗ではない |
| `tests/ops/` 全体（Python 3.14、lane venv） | 本表作成時点で実行中。結果は受入票へ反映する |
| `ruff check mcs/ops/mcs_update.py tests/ops/test_mcs_update_rollback_floor.py` | passed |
| `changes/updater-rollback-layout-floor.json` JSON 妥当性 | passed |

未確認・対象外の扱い:

- 実機での `install.sh --preflight`・`mcs_upgrade.py`・`rollback`・`mcs_recover.py` の実行、gateway 再起動、実配送は未実施（公開・本番適用は別工程）。
- 項目4の fixture 不足は本表の作成者の所有外。修正後に lane を再実行して本表と受入票を更新する。
- 不要コード・配置・文書整理は [REPOSITORY_HYGIENE_1.0.23.md](REPOSITORY_HYGIENE_1.0.23.md)（削除根拠のある候補0）。本表では重複調査しない。
- 全ワークツリー集約は [WORKTREE_INTEGRATION_1.0.23.md](WORKTREE_INTEGRATION_1.0.23.md)。
