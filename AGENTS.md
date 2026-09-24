# AGENTS.md — hermes-mcs

エージェント作業用の最小指示。詳細は `README.md`・`SECURITY.md`。

## これは何か（機能サマリ）

MedicalCareStation (MCS) の医療・介護チャットを収集・解析するローカルシステム:

- 15分間隔で未読収集 → SQLite(`data/ledger.db`) → Discord 通知
- `self_posts` 設定で自投稿・他者先読み投稿を毎 tick `latest` probe →
  未保管の最新 id があれば bounded 履歴取得して取り込み・新着通知
  （`stage_self_probe`。`latest` は `{is_self_only,message:{id}}` のみ返し
  after フィルタ無し。取り切れなかった id は `patients.probe_mid` に
  記録して再取得ループを抑止）
- 全履歴アーカイブ・FTS5 全文検索・患者タイムライン
- 構造化抽出: ルール `extract_v1` + ローカルLLM `extract_llm`(Qwen3.5-9B、外部送信なし)
  — スキーマ v2（薬剤 action/status/subject・症状 status・evidence スパン）
- 抽出項目の Jev QC 監査: `semantic.extract_qc:"annotate"` で `extract_qc`
  artifact に注記のみ記録（抽出の変更・抑制なし、drain ガード共有）
- 読み取り専用統計・レビュー候補シグナル6種・人承認の依頼管理
- Hermes addon(`hermes_plugin/`): Discord `/mcs <json>` で閲覧・preview/confirm

## 構成

- `mcs/` — 実行モジュール。**flat import維持のまま第一層サブディレクトリに分割**:
  `core/`(ledger・mcs_util・init_data・local_llm・maintenance) ·
  `ingest/`(mcs_adapter・mcs_transport・job_ops・notifier・run_check) ·
  `extract/`(extract・extract_llm・extract_bench・rollup) ·
  `semantic/`(semantic.py + semantic_* 20本) ·
  `views/`(読み取り専用: mcs_view・mcs_stats・mcs_queries・summary_review・structured_view) ·
  `ops/`(書き込み系: mcs_requests・mcs_operations・mcs_signals・mcs_setup・
  mcs_update・mcs_refstats・request_loops・brain_export)
  — importは変わらず `import ledger`。エントリポイントが `mcs/` ルートを
  sys.path に挿れて `import _mcs_path`（全サブディレクトリを import root
  として登録）する2行ブートストラップを持つ。`mcs/` 直下に import 可能な
  モジュールは `_mcs_path.py` のみ
- `tests/` — pytest。`mcs/` と同じ領域名のサブディレクトリに配置
  （`conftest.py` が tests/ 各サブディレクトリを sys.path 挿入して
  テスト間ヘルパーimportを維持 + socket 遮断ガード）
- `evaluation/` — 評価資産一式（ベンチcases・G6基準・注釈ガイド・
  rehearsal結果）
- `hermes_plugin/` · `integration/`(hermes E2E) · `deployment/` · `docs/`
- `scripts/` — `run_tests.sh`、`update_readme.py`

## コマンド

```bash
scripts/run_tests.sh                # 全テスト（一時HOME・認証環境の隔離）
ruff check mcs/ tests/ hermes_plugin/ integration/  # CIと同じ範囲
python3 scripts/update_readme.py    # README 生成ブロック再生成（CI が drift 検出）
python3 mcs/ops/mcs_setup.py check  # 実機の必須条件検証
```

`make test|lint|readme|check` も利用可（uv があれば ephemeral 実行）。
テスト対象を絞る場合も `scripts/run_tests.sh tests/<領域>/` を使う。

## 絶対ルール

- **収集・解析コアの依存は標準ライブラリのみ**。新しい外部依存を加えない。
  `hermes_plugin/mcs_discord/{actions,cards}.py` だけは Hermes 同梱の
  `discord.py` を関数内で遅延 import し、UI と既存 interaction の
  followup に使う。独自 Bot・認証情報・REST 接続は作らない。
  adapter の `asyncio` は `sleep`・`to_thread`・`CancelledError` に限定する
- テストは一時DB+スタブのみ。実 MCS・Discord・Keychain・原本DB・
  ローカルLLM・Jev へ**一切アクセスしない**
- 患者データ・秘密情報は repo に入れない（`data/`・`.env`・`config.json`等は ignore 済み）
  — ベンチ・few-shot・テスト fixture は**完全合成のみ**（実投稿の匿名化も不可）
- **安全ゲートを壊さない**: 既読化は snapshot timestamp 必須、no-redirect/no-proxy、
  人承認操作は `--confirm-human`+`reason`+receipt 経路のみ
- 「記録が見つからない」≠「対応がなかった」— 候補提示はこの区別を保持
- 大きな凝集モジュール（`ledger.py`/`mcs_adapter.py`/`semantic_drain`）を
  行数だけで機械分割しない

## README 自動生成

`mcs/**/*.py` 追加・docstring 変更・検知器/統計/サブコマンド追加時は
`python3 scripts/update_readme.py` を実行（`GENERATED:*` マーカー内を再生成）。
新しい第一層サブディレクトリを足す場合はブートストラップが自動対応するが、
`AGENTS.md` の構成説明と `deployment/` のパス表記も更新する。
docstring 先頭文は公開されるので1文要約にする。
