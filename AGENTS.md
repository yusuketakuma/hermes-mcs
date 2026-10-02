# AGENTS.md — hermes-mcs

エージェント作業用の最小指示。詳細は `README.md`・`SECURITY.md`。

## システムと入口

MCS の医療・介護チャットを収集・解析するローカルシステム。
5分間隔の未読収集、`self_posts` の最新投稿 probe、履歴保管・FTS5検索、
ルール抽出とローカルLLM抽出、統計・シグナル、人承認の依頼管理を持つ。
LLM は新着・対話1枠とバックログ2枠。抽出世代・QC・公開条件・モデルの
既定は `mcs/extract/`・`mcs/semantic/` と仕様を照合し、要約から固定しない。
`stage_self_probe` は未保管の最新 id を bounded 履歴取得し、取得できない
id は `patients.probe_mid` に記録して繰返し取得を抑える。

| 領域 | 入口・制約 |
|---|---|
| 実行 | `mcs/` の `core/`・`ingest/`・`notify/`・`extract/`・`semantic/`・`views/`・`ops/`。モジュール表は `docs/development/DEVELOPMENT.md` |
| import | flat import を維持。エントリポイントは `mcs/` を sys.path に追加し `_mcs_path` を import。`.py` のある配下を任意の深さで登録する。直下の import モジュールは `_mcs_path.py` のみ |
| 抽出 | `extract/v1/` はルール、`v4/` は現行LLM。v2/v3 の旧実装は git 履歴。`extract/rollup.py` と `semantic/` は世代横断 |
| 通知 | `adapters/{slack,discord,lineworks,common}/`。`hermes_plugin/` は Hermes 接続・互換入口、`lineworks_adapter/` は独立CLI入口 |
| 独立実行 | `runtime_mode=standalone` の `mcs_standalone/` が単一host・6定期ジョブ・cmd取込・抽出worker2本を所有。Hermes未指定は従来経路。`docs/guides/STANDALONE.md` |
| 検証 | `tests/` は実行領域に対応、`integration/` は統合、`evaluation/` は完全合成の評価資産。conftest がヘルパー import と socket 遮断を担う |
| 文書・配備 | 利用は `docs/guides/`、開発は `docs/development/`、契約は `docs/specs/`。`deployment/` は配備候補で、編集だけでは実機適用しない |

Hermes plugin の更新反映には gateway 再起動が必要。旧 worker が新 spec を
処理すると本文・添付が欠落し得るため、配備が明示範囲にあるときだけ再起動し、
未適用なら報告する。LINE WORKS は独立アダプターの起動・再起動が必要。
公開 Callback・常駐手順は `docs/guides/LINEWORKS.md`。

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
  接続は作らない。例外は `runtime_mode=standalone` の
  `adapters/{slack,discord}/standalone.py` と `mcs_standalone/{slack,discord}_runtime.py` で、
  `deployment/requirements-standalone.txt` の固定版公式 SDK（独立 venv）で接続し、
  トークンはroot配下の私有JSON（優先）または `.env`（0600）だけから読む。
  SDK import は関数内に限り、
  proxy 環境変数は起動時に除去する。
  LINE WORKS はHermesに接続機能がないため `adapters/lineworks/` だけが独自の
  認証・Bot REST・署名Callbackを所有する（stdlib・OpenSSL、固定公式URL、
  no-redirect/no-proxy、期限・容量制限、秘密値の環境自動取得なし）。
  Hermes接続のadapterの `asyncio` は `sleep`・`to_thread`・`CancelledError` に限定する。
  独立Slack/Discordの `standalone.py` は接続・task終了待ちのasyncioも使用する。
  正確なSDK/asyncio許可は `ci/gates.py` に限定し、通常adapterへ拡張しない。
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

## 現行の連携方針

ユーザー指示（2026-10-02）により agmsg は廃止。以下の古い managed block の
agmsg 通知・所有者宣言・受信コマンド・フックは実行しない。保存済み履歴は保持する。
Devflow の PLAN/STATUS/git/VERIFY/REVIEW 正本、役割分離、書込み所有権、
人承認・外部操作の境界は維持する。現存しない中央設定を推測で再作成したり、
別の連携サービス・常駐処理へ自動置換したりしない。

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
