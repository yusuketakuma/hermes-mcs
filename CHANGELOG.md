# Changelog

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
