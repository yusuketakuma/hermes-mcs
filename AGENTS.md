# AGENTS.md — hermes-mcs

エージェント作業用の最小指示。詳細は `README.md`・`SECURITY.md`。

## これは何か（機能サマリ）

MedicalCareStation (MCS) の医療・介護チャットを収集・解析するローカルシステム:

- 24時間5分間隔で未読収集。LLM は新着・対話用1枠とバックログ用2枠→ SQLite(`data/ledger.db`) → Slack / Discord / LINE WORKS 通知
- `self_posts` 設定で自投稿・他者先読み投稿を毎 tick `latest` probe →
  未保管の最新 id があれば bounded 履歴取得して取り込み・新着通知
  （`stage_self_probe`。`latest` は `{is_self_only,message:{id}}` のみ返し
  after フィルタ無し。取り切れなかった id は `patients.probe_mid` に
  記録して再取得ループを抑止）
- 全履歴アーカイブ・FTS5 全文検索・患者タイムライン
- 構造化抽出: ルール `extract_v1` + ローカルLLM `extract_llm`（外部送信なし）。
  抽出スキーマ、QC、再抽出、v4 の公開条件は `mcs/extract/`・`mcs/semantic/` と
  関連仕様を照合する。モデル名や既定値をこの要約から固定的に推定しない。
- 読み取り専用統計・アラートシグナル・人承認の依頼管理
- Hermes addon(`hermes_plugin/`): Discord / Slack で閲覧・preview/confirm と配送。
  LINE WORKS は `adapters/lineworks/` の独立プロセスで同じ配送・人承認契約を使う
- `runtime_mode`: 未指定/`hermes` は上記の Hermes 経由。`standalone` は Hermes なしで
  全機能を動かす（`mcs_standalone/` が Slack/Discord 接続・送信、定期ジョブは
  launchd `ai.mcs.cron.*`、接続は `ai.mcs.standalone`）。`docs/guides/STANDALONE.md`

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
  個別モジュールの一覧は `docs/development/DEVELOPMENT.md` の生成表を参照。
  — importは変わらず `import ledger`。エントリポイントが `mcs/` ルートを
  sys.path に挿れて `import _mcs_path`（.py を持つ全サブディレクトリを
  任意の深さで import root として登録）する2行ブートストラップを持つ。
  `mcs/` 直下に import 可能なモジュールは `_mcs_path.py` のみ
- `tests/` — pytest。`mcs/` と同じ領域名のサブディレクトリに配置。
  `adapters/` は接続先別のテスト、`plugin/` はHermes連携のテスト
  （`conftest.py` が tests/ 各サブディレクトリを sys.path 挿入して
  テスト間ヘルパーimportを維持 + socket 遮断ガード）
- `evaluation/` — 評価資産一式（ベンチcases・G6基準・注釈ガイド・
  rehearsal結果）
- `adapters/` — `slack/`・`discord/`（Hermes公式接続を利用する表示・配送・操作） ·
  `lineworks/`（独自Bot API・JWT認証・署名Callback・配送・DM入力/確定・CLI/サービス候補） ·
  `common/`（接続先共通のpaths・journal・registry・envelopes・spec・text・worker）
- `lineworks_adapter/` — 独立LINE WORKS CLIの互換入口（`python -m lineworks_adapter`）
- `mcs_standalone/` — `runtime_mode=standalone` の Slack/Discord 接続（run）・
  テキスト送信（send）・診断（check）。既存 `adapters/` の Supervisor と
  `hermes_plugin` の `/mcs` handler を Hermes の代わりに起動する
- `hermes_plugin/` — `mcs_discord/`・`mcs_slack/`（`adapters/`への互換import入口） ·
  `mcs_delivery/`（`adapters/common/`への互換import入口） ·
  `card_workers.py`(worker 設定解決・factory) · `projects.py`
- `integration/` — Hermes 連携・複数領域の統合テスト
- `deployment/` — 配備用スクリプト・設定候補（変更だけでは実機適用しない）
- `docs/` — `guides/`（利用・導入） · `development/`（開発・保守） ·
  `specs/`（仕様） · `roadmap/`（計画） · `dev-records/`（検証・設計履歴） ·
  `assets/`・`screenshots/`（図・完全合成の画面例）
- `scripts/` — `run_tests.sh`・`keychain_to_env.py`（実行・運用入口） ·
  `development/`（文書生成・リリース・画面生成・合成検証）

`hermes_plugin/` は長寿命の Hermes gateway が起動時に読込む。変更を
有効化するには `hermes gateway restart` が必要 — 再起動なしでは
runner が発行する新形式 spec を旧世代 worker が処理し、card は
届くが companion thread の本文・添付が欠落する（2026-09 実例）。
再起動は配備の明示範囲に含まれる場合に行い、未適用なら結果報告に残す。
LINE WORKS の起動・更新は独立アダプターの再起動が必要で、Hermes gateway
だけでは起動しない。導入・公開HTTPS Callback・常駐手順は `docs/guides/LINEWORKS.md`。

## コマンド

```bash
scripts/run_tests.sh                # tests/ 一式（一時HOME・認証環境の隔離）
ruff check mcs/ tests/ hermes_plugin/ adapters/ lineworks_adapter/ mcs_standalone/ integration/ ci/ scripts/ deployment/ conftest.py  # CIと同じ範囲
python3 scripts/development/update_readme.py    # README 生成ブロック再生成（CI が drift 検出）
python3 scripts/development/update_readme.py --check
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
  `adapters/discord/{actions,cards}.py` だけは Hermes 同梱の
  `discord.py` を関数内で遅延 import し、UI と既存 interaction の
  followup に使う。Hermes モードでは Slack/Discord の独自 Bot・認証情報・REST
  接続は作らない。例外は `runtime_mode=standalone` の `mcs_standalone/` だけで、
  `deployment/requirements-standalone.txt` の固定版公式 SDK（独立 venv）で接続し、
  トークンは `~/.mcs/.env`（0600）からのみ読む。SDK import は関数内に限り、
  proxy 環境変数は起動時に除去する。
  LINE WORKS はHermesに接続機能がないため `adapters/lineworks/` だけが独自の
  認証・Bot REST・署名Callbackを所有する（stdlib・OpenSSL、固定公式URL、
  no-redirect/no-proxy、期限・容量制限、秘密値の環境自動取得なし）。
  adapter の `asyncio` は `sleep`・`to_thread`・`CancelledError` に限定する。
  独立LINE WORKSの `__main__.py` だけは常駐起動/停止用の
  `Event`・`get_running_loop`・`wait_for`・`run` も使う
- テストは一時DB+スタブのみ。実 MCS・Discord・Slack・LINE WORKS・Keychain・原本DB・
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
`python3 scripts/development/update_readme.py` を実行（`GENERATED:*` マーカー内を再生成）。
生成ブロックは `docs/development/DEVELOPMENT.md`（開発・運用リファレンス）に置く —
`README.md` は利用者向けなので生成表は持たない。最新の変更要約だけは
`GENERATED:release`としてCHANGELOGから生成する。
通知先の推奨とREADMEの先頭画面はSlack。Discordは対応する選択肢として残す。
Slack画面例は完全合成の説明図を使い、`docs/screenshots/slack-gallery/README.md`の
ソース対応を確認する。画面変更時はSVG・PNGを一緒に再生成し、
`python3 scripts/development/generate_slack_gallery.py --check`で画像の整合を検証する。
新しい第一層サブディレクトリを足す場合はブートストラップが自動対応するが、
`AGENTS.md` の構成説明と `deployment/` のパス表記も更新する。
docstring 先頭文は公開されるので1文要約にする。

## リリースノート

- CHANGELOGとGitHub Releaseは毎回`docs/development/RELEASE_NOTES.md`の共通ルールを守る。
  段落は「新機能」「改善」「不具合修正」「動作・設定の変更」「更新時の注意」
  の順。空の分類は出さず、技術詳細は末尾で折りたたむ。
  各項目は太字の短いタイトルと次行の説明（原則1〜3文）で記載する。
- 実行時の挙動を追加・変更・修正・削除するときは、同じ作業内で
  `changes/<識別子>.json`を日本語で作成する。形式は`changes/README.md`。
- summaryは利用者への影響を先に書き、upgradeには必要な操作と適用条件を記録する。
  モデル・取得範囲・通知・既読化・承認条件・既定値の変更は省略しない。
  根拠のない性能数値、実患者情報、実投稿の匿名化例は使用しない。
- リリース準備を依頼されたら、`docs/development/RELEASE_NOTES.md`の手順に従い、記録から
  見出しと要約を作成し`release_notes.py build`でCHANGELOGを生成する。
  GitHub Release本文は同じversionのCHANGELOGからexportし、別に作文しない。
- 毎回のリリース準備で、READMEの機能・画面例・導入・安全・ドキュメント導線を
  新しい変更とソースに照らして見直す。`docs/development/README_MAINTENANCE.md`に従い、
  `docs/development/readme-review.json`に新しいversion・各項目の確認内容と根拠を記録する。
  変更不要でも照合結果を書く。versionだけの更新で済ませない。
  buildはREADMEの最新変更も更新する。生成部分は手編集せず、
  `python3 scripts/development/readme_release.py --check`で同期・記録・リンクを検証する。
- tag workflowは下書きを作り、mainのCHANGELOGを既存Releaseへ自動同期する。
  タイトル・本文以外は変更しない。公開済み本文の変更も先にCHANGELOGへ反映する。
  push・PR・tag・公開の承認は従来の規約に従う。

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

<!-- OPENWIKI:START -->

## OpenWiki

This repository has a generated `openwiki/` evidence index. It is optional just-in-time context, not required startup reading.

- Do not enumerate, preload, or search wikis at task start. Use retrieval when the user asks for it, when unfamiliar architecture or dependency behavior materially affects the task, or when source inspection leaves an important uncertainty. Stop once the question is grounded.
- When those conditions apply and OpenWiki retrieval tools are available, use `openwiki_search` for just-in-time context and `openwiki_read` for the relevant complete sections. If search returns `workspace_required`, ask which listed workspace to use and retry with its ID.
- Use `openwiki_list_workspaces` or `openwiki_list_wikis` when workspace membership itself needs to be discovered.
- If the retrieval tools are unavailable, read `openwiki/quickstart.md` and follow its links to the relevant pages.
- Treat source code and tests as authoritative. A brief's unknowns and review items are verification gaps, not automatic requirements.
- Prefer the narrowest quiet validation that proves the changed behavior. Preserve complete failure output.

The scheduled OpenWiki GitHub Actions workflow refreshes the repository wiki. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->
