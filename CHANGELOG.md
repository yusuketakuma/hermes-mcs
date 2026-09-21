# Changelog

## [1.0.1] — 2026-09-22

構造化抽出の精度向上プログラム（スキーマ拡張・文脈注入・出力保証・
QC 検証の4系統）と、人承認済み参照セットによる統計回帰ゲートを追加。

### Features
- `extract_llm` v2 スキーマ — meds `action/status/subject/negated`、
  symptoms `status`、requests `from/due`、全項目に `evidence` スパン
  （本文内一意照合）。完全合成 few-shot、スレッド context 注入
  （DATA 境界 + 無害化 + meta 記録）
- `response_format` 自動検出 — `json_schema → json_object → plain`
  の段階降格 + cooldown 再 probe。長文は `_chunks` 全文カバレッジ +
  決定的マージ（状態遷移保持、マルチチャンク要約破棄）
- Jev QC — `extract_qc` ジョブを `run_due` 共有 claim で実行
  （OFF/circuit/paused/予算の全ガード共有）。項目別 noul 裏付け +
  urgency 監査を artifact に注記のみ記録（抽出の変更・抑制なし）
- `mcs/extract_bench.py` — フィールド別 P/R/F1 ベンチハーネス +
  完全合成回帰ケース14件（`bench/extract_cases.json`）
- `mcs/mcs_refstats.py` — 承認済み参照セット統計検証:
  `capture` → `control refstat_approve --confirm-human`（バイト列
  SHA-256 を `refstat_approval_v1` artifact に記録）→ `verify`
  （match/drift/regression/unverified/superseded）

### Fixes
- consumer フィルタ統一 — `med_is_patient_current` 述語を
  rollup/notifier/stats/signals/cooccurrence で共有し、否定・家族・
  過去言及が「現在服用中」集計に混入しないよう修正
- v1→v2 lazy-replace 移行 — 成功書込みと同一 tx で旧行を置換、
  poison 行は保持、`--all` ドレイナーの非終了経路を解消
- `test_semantic_runtime` 日次境界フレーク — 時刻を JST 正午に固定

### Docs
- README: 「MCS データで何が追えるのか」節追加、mermaid → SVG 資産化、
  モジュール/シグナル/統計/CLI 表の自動生成を `update_readme.py` に統合
- AGENTS.md をエージェント向け最小指示に凝縮

## [1.0.0] — 2026-09-21

`mcs-adapter` から `hermes-mcs` として独立リポジトリ化し、内部構造を
シンプル化した最初のリリース。

### Layout
- `adapter/` → `mcs/`（実行モジュール37本）、テスト49本を `tests/` へ分離
- `hermes_plugin/`（/mcs Discord コマンド）、`integration/`（hermes E2E）を維持
- `docs/dev-records/` に開発記録を集約、`deployment/launchagents/` に
  launchd plist テンプレート3種を追加

### Features (既存機能の集約)
- 15分間隔の MCS 未読収集 + Discord 通知（構造化→原文の2段投稿）
- 全履歴アーカイブ（ページカーソルで中断再開）
- FTS5 全文検索 + 日本語空白無視の部分一致、患者タイムライン
- 構造化抽出: ルール `extract_v1` + ローカルLLM `extract_llm`(Qwen3.5-9B)
- 患者ロールアップ、読み取り専用統計、レビュー候補シグナル6種
- 人承認の依頼管理（`--confirm-human`+`reason`+receipt）
- 人による却下（`signal_dismiss`）と承認済み閾値ポリシー（`signal_policy`）
- 通知クールダウン（既定7日）、deadline 部分実行の安全側 resolve 抑制

### Infrastructure
- `mcs/mcs_setup.py` — init（Keychain/config/.env プロビジョニング）+ check
- `scripts/update_readme.py` — README モジュール表の自動生成
- GitHub Actions: lint-test / readme-sync / hygiene
- `pyproject.toml` で ruff・pytest 設定を一元化、`Makefile` 追加
- `LICENSE`（proprietary）・`SECURITY.md`・`AGENTS.md` 新設
