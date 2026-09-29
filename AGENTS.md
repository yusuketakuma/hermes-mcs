# AGENTS.md — hermes-mcs

エージェント作業用の最小指示。詳細は `README.md`・`SECURITY.md`。

## これは何か（機能サマリ）

MedicalCareStation (MCS) の医療・介護チャットを収集・解析するローカルシステム:

- 5分間隔で未読収集（夜間 22-06時はスクリプト内で20分間隔に間引き —
  30分のセッション失効上限を下回る設計）→ SQLite(`data/ledger.db`) → Discord 通知
- `self_posts` 設定で自投稿・他者先読み投稿を毎 tick `latest` probe →
  未保管の最新 id があれば bounded 履歴取得して取り込み・新着通知
  （`stage_self_probe`。`latest` は `{is_self_only,message:{id}}` のみ返し
  after フィルタ無し。取り切れなかった id は `patients.probe_mid` に
  記録して再取得ループを抑止）
- 全履歴アーカイブ・FTS5 全文検索・患者タイムライン
- 構造化抽出: ルール `extract_v1` + ローカルLLM `extract_llm`（外部送信なし）。
  抽出スキーマ、QC、再抽出、v4 の公開条件は `mcs/extract/`・`mcs/semantic/` と
  関連仕様を照合する。モデル名や既定値をこの要約から固定的に推定しない。
- 読み取り専用統計・レビュー候補シグナル・人承認の依頼管理
- Hermes addon(`hermes_plugin/`): Discord / Slack で閲覧・preview/confirm と配送

## 構成

- `mcs/` — 実行モジュール。**flat import維持のまま第一層サブディレクトリに分割**:
  `core/`(DB・共通処理・LLM admission) · `ingest/`(収集・health監視) ·
  `notify/`(通知・配送整合) · `extract/`(抽出・評価) · `semantic/`(意味解析) ·
  `views/`(読み取りモデル・統計) · `ops/`(依頼・運用・外部出力契約)
  `extract/` 内は推論エンジン世代でフォルダ分け: `v1/`(ルール抽出
  `extract.py`) · `v2/`/`v3/`(in-place 置換で退役した旧 extract_llm —
  README のみ、旧実装は git 履歴) · `v4/`(現行 `extract_llm.py` と
  `extract_bench.py`)。`semantic/` は v4 canonical エンジン群と
  世代横断の QC・評価基盤のため世代分割しない。`rollup.py` は
  v1+v4 を読む世代横断集約で `extract/` 直下に残す。
  個別モジュールの一覧は `docs/DEVELOPMENT.md` の生成表を参照。
  — importは変わらず `import ledger`。エントリポイントが `mcs/` ルートを
  sys.path に挿れて `import _mcs_path`（.py を持つ全サブディレクトリを
  任意の深さで import root として登録）する2行ブートストラップを持つ。
  `mcs/` 直下に import 可能なモジュールは `_mcs_path.py` のみ
- `tests/` — pytest。`mcs/` と同じ領域名のサブディレクトリに配置
  （`conftest.py` が tests/ 各サブディレクトリを sys.path 挿入して
  テスト間ヘルパーimportを維持 + socket 遮断ガード）
- `evaluation/` — 評価資産一式（ベンチcases・G6基準・注釈ガイド・
  rehearsal結果）
- `hermes_plugin/` — `mcs_discord/`(Discord worker) · `mcs_slack/`(Slack worker) ·
  `mcs_delivery/`(transport中立の配送基盤: paths・journal・registry・envelopes・spec・text・worker) ·
  `card_workers.py`(worker 設定解決・factory) · `projects.py`
- `integration/` — Hermes 連携・複数領域の統合テスト
- `deployment/` — 配備用スクリプト・設定候補（変更だけでは実機適用しない）
- `docs/` — 仕様・開発資料・検証記録
- `scripts/` — `run_tests.sh`、`update_readme.py`、`keychain_to_env.py`、
  `semantic_shadow_e2e.py`

`hermes_plugin/` は長寿命の Hermes gateway が起動時に読込む。変更を
有効化するには `hermes gateway restart` が必要 — 再起動なしでは
runner が発行する新形式 spec を旧世代 worker が処理し、card は
届くが companion thread の本文・添付が欠落する（2026-09 実例）。
再起動は配備の明示範囲に含まれる場合に行い、未適用なら結果報告に残す。

## コマンド

```bash
scripts/run_tests.sh                # tests/ 一式（一時HOME・認証環境の隔離）
ruff check mcs/ tests/ hermes_plugin/ integration/ ci/ scripts/ deployment/ conftest.py  # CIと同じ範囲
python3 scripts/update_readme.py    # README 生成ブロック再生成（CI が drift 検出）
python3 scripts/update_readme.py --check
python3 ci/gates.py
python3 ci/mine_gates.py --check
```

`make test|lint|readme|check` も利用可（uv があれば ephemeral 実行）。
テスト対象を絞る場合も `scripts/run_tests.sh tests/<領域>/` を使う。
`integration/` は必要な対象を同 runner に指定する。Hermes 実SDKとの統合は
CI の pinned Hermes 環境で別に検証されるため、ローカル pytest 成功とは区別する。
`python3 mcs/ops/mcs_setup.py check` / `make setup-check` は実機向けの診断であり、
合成 fixture によるテストの代わりに実行しない。

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
生成ブロックは `docs/DEVELOPMENT.md`（開発・運用リファレンス）に置く —
`README.md` は利用者向けなので生成表は持たない。
新しい第一層サブディレクトリを足す場合はブートストラップが自動対応するが、
`AGENTS.md` の構成説明と `deployment/` のパス表記も更新する。
docstring 先頭文は公開されるので1文要約にする。

<!-- BEGIN DEVFLOW MANAGED -->
## Devflow 共通運用（managed block — この block 内のみ devflow が更新する）

- 中央管理: `~/.config/devflow/`（registry/policy/roles/bin）,
  task 正本: `~/.local/state/devflow/tasks/mcs/<task_id>/`,
  worktree: `~/.herdr/worktrees/devflow/mcs/<task_id>/`
- agmsg team `devflow-mcs` seats: planner(codex) / builder(devin) / reviewer(codex)。
  delivery: codex seats=turn（`.codex/hooks.json` Stop+PostToolUse hook）、
  builder=off（手動受信 `bash ~/.agents/skills/agmsg/scripts/inbox.sh devflow-mcs builder`）。
  agmsg は通知用のみ — task 状態の正本は PLAN/STATUS/git/VERIFY/REVIEW。
- 役割定義: `~/.config/devflow/roles/`（planner=計画のみ / builder=worktree内実装 /
  reviewer=独立レビュー・修正禁止）
- 実行権限: Astra planner/reviewer = Auto 相当（codex `-s workspace-write
  -a on-request` + `approvals_reviewer="user"` + `sandbox_workspace_write.network_access=false`。
  workspace-write は業務コードへの書込みを技術的に禁止しない — 計画/レビュー専任は
  role 規約と diff 検査で守る）。Devin builder = Bypass（`--permission-mode dangerous`、
  OS sandbox 無し — 境界は role 規約と devflow 権限 deny ルール）。
- task packet: PLAN.md ACCEPTANCE.md STATUS.json HANDOFF.md VERIFY.md REVIEW.md。
  PLAN/ACCEPTANCE は `devflow ready` で hash 固定。変更は Planner へ差し戻し新版で。
- 外部操作禁止: push/PR/merge/deploy/外部送信/本番・実データ変更は明示承認のみ。
  commit は Builder が PLAN の Commit Group 設計に沿った検証済み論理グループ単位で
  worktree 内の作業 branch にのみ行う。秘密情報・患者情報を agmsg/文書に含めない。
- 受入: ACCEPTED は技術的受入のみ。merge/deploy の許可ではない。
<!-- END DEVFLOW MANAGED -->
