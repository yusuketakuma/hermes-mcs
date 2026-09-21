# AGENTS.md — hermes-mcs

エージェント作業時の指示。人間向けの詳細は `README.md`。

## 構成

- `mcs/` — 実行モジュール（flat import、`sys.path` に `mcs/` を挿入して `import ledger` 等）
- `tests/` — pytest（`conftest.py` が `../mcs` を sys.path に挿入 + 環境隔離ガード）
- `hermes_plugin/` — `/mcs` コマンドを登録する plugin（`../mcs` を相対参照）
- `integration/` — hermes-agent checkout 上でのみ収集される E2E
- `deployment/` — cco 隔離設定・launchagents テンプレート
- `docs/` — 運用資料。開発記録は `docs/dev-records/`
- `scripts/` — `run_tests.sh`（sandbox env で pytest）、`update_readme.py`

## コマンド

```bash
python -m pytest              # 全テスト（pyproject で testpaths=tests）
ruff check mcs/ tests/        # lint（pyproject の select=E4,E7,E9,F）
python3 scripts/update_readme.py          # README モジュール表を再生成
python3 scripts/update_readme.py --check  # drift 検出
python3 mcs/mcs_setup.py check            # 実機環境の必須条件検証
```

`uv` があれば `uv run --with pytest python -m pytest` で依存なし実行できる。

## 規約

- **依存は標準ライブラリのみ**。`yaml`/`requests` 等を新たに import しない
- テストは一時DBと通信スタブのみ — 実 MCS・Discord・Keychain・原本DB・
  ローカルLLM・Jev へは絶対にアクセスしない（conftest が socket を遮断）
- **安全ゲートを壊さない**: 既読化ゲート（snapshot timestamp 必須）、
  no-redirect/no-proxy、定期実行は本文・氏名をログに出さない、
  人承認操作は `--confirm-human`+`reason`+receipt 経路のみ
- 「記録が見つからない」≠「対応がなかった」— 候補提示は必ずこの区別を保持
- 構造は facade+同階層 sibling 分解。強結合なモジュールを行数だけで
  機械分割しない（`ledger.py`/`mcs_adapter.py`/`semantic_drain` は凝集単位）
- 大きなモジュールは `semantic_*.py` のように機能別 prefix で並べる
- コメントは最小限。既存コメントを勝手に消さない

## README 自動生成

`scripts/update_readme.py` が `<!-- GENERATED:name -->` マーカー内を再生成:

- `modules` — `mcs/*.py` の docstring 先頭行
- `signals` — `mcs_signals.DETECTORS`（検知器名+docstring 先頭文）
- `stats` — `mcs_stats.REGISTRY`/`PRESETS`
- `cli` — `mcs_view` argparse サブコマンド

該当箇所を変更したら必ず `python3 scripts/update_readme.py` を実行
（CI が drift を検出する）。docstring の先頭文は公開されるので1文要約にする。
