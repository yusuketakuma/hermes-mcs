# AGENTS.md — hermes-mcs

エージェント作業用の最小指示。詳細は `README.md`・`SECURITY.md`。

## これは何か（機能サマリ）

MedicalCareStation (MCS) の医療・介護チャットを収集・解析するローカルシステム:

- 15分間隔で未読収集 → SQLite(`data/ledger.db`) → Discord 通知
- 全履歴アーカイブ・FTS5 全文検索・患者タイムライン
- 構造化抽出: ルール `extract_v1` + ローカルLLM `extract_llm`(Qwen3.5-9B、外部送信なし)
- 読み取り専用統計・レビュー候補シグナル6種・人承認の依頼管理
- Hermes addon(`hermes_plugin/`): Discord `/mcs <json>` で閲覧・preview/confirm

## 構成

- `mcs/` — 実行モジュール（flat import: `sys.path` に `mcs/` を挿入して `import ledger`）
- `tests/` — pytest（`conftest.py` が sys.path 挿入 + socket 遮断ガード）
- `hermes_plugin/` · `integration/`(hermes E2E) · `deployment/` · `docs/`
- `scripts/` — `run_tests.sh`、`update_readme.py`

## コマンド

```bash
python -m pytest                    # 全テスト（pyproject: testpaths=tests）
ruff check mcs/ tests/              # lint（pyproject: select=E4,E7,E9,F）
python3 scripts/update_readme.py    # README 生成ブロック再生成（CI が drift 検出）
python3 mcs/mcs_setup.py check      # 実機の必須条件検証
```

`make test|lint|readme|check` も利用可（uv があれば ephemeral 実行）。

## 絶対ルール

- **依存は標準ライブラリのみ**。新しい外部 import を加えない
- テストは一時DB+スタブのみ。実 MCS・Discord・Keychain・原本DB・
  ローカルLLM・Jev へ**一切アクセスしない**
- 患者データ・秘密情報は repo に入れない（`data/`・`.env`・`config.json`等は ignore 済み）
- **安全ゲートを壊さない**: 既読化は snapshot timestamp 必須、no-redirect/no-proxy、
  人承認操作は `--confirm-human`+`reason`+receipt 経路のみ
- 「記録が見つからない」≠「対応がなかった」— 候補提示はこの区別を保持
- 大きな凝集モジュール（`ledger.py`/`mcs_adapter.py`/`semantic_drain`）を
  行数だけで機械分割しない

## README 自動生成

`mcs/*.py` 追加・docstring 変更・検知器/統計/サブコマンド追加時は
`python3 scripts/update_readme.py` を実行（`GENERATED:*` マーカー内を再生成）。
docstring 先頭文は公開されるので1文要約にする。
