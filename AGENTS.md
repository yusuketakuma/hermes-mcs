# AGENTS.md — hermes-mcs

MCS の医療・介護チャットを収集・解析するローカルシステム。詳細は `README.md`・`SECURITY.md`・
`docs/development/REPOSITORY_STRUCTURE.md`。このMacへの導入・更新・復旧の依頼は開発作業ではない。
`README.md` 冒頭「AIエージェントへ」から `docs/guides/SETUP_AGENT.md`（新規）か
`docs/guides/UPGRADE_AGENT.md`（導入済み・復旧）を選んで従う。

## 安全・設計上の制約

- tag・Release 公開・常駐サービスの再起動・本番DBや実データの変更も、ユーザーが明示した範囲だけ行う。
- 収集・解析コアは標準ライブラリのみ。新しい外部依存を足さない。例外（Hermes 同梱 `discord.py` の関数内遅延 import、
  `runtime_mode=standalone` の `adapters/{slack,discord}/standalone.py` が使う `deployment/requirements-standalone.txt` の固定版 SDK、
  `adapters/lineworks/` の独自認証・Bot REST・署名Callback、adapter ごとの `asyncio` 許可範囲）の正確な許可リストは
  `ci/gates.py` が持つ。許可を通常の adapter へ広げない。Hermes モードでは Slack/Discord の独自 Bot・認証情報・REST 接続を作らない。
  standalone のトークンは root 配下の私有JSON（優先）か `.env`（0600）だけから読み、SDK は関数内で import し、proxy 環境変数は起動時に除去する。
- テストは一時DB+スタブのみ。実 MCS・Discord・Slack・LINE WORKS・Keychain・原本DB・ローカルLLM・Jev に一切アクセスしない。
- 患者データ・秘密値を repo に入れない（`data/`・`.env`・`config.json` 等は ignore 済み）。ベンチ・few-shot・fixture は完全合成のみ（実投稿の匿名化も不可）。
- 安全ゲートを壊さない。既読化は snapshot timestamp 必須、通信は no-redirect/no-proxy（例外は `SECURITY.md` の添付取得だけ）、人承認操作は `--confirm-human`+`reason`+receipt 経路だけ。
- 「記録が見つからない」≠「対応がなかった」。未対応・未返信の候補は断定しない文言で区別し、未取得・取得失敗は明示する。
  観測済みの0件（スタンプなし等）は行ごと省略してよい。チャット表示に責任回避の但し書きや「AI判定」等の出所ラベルを付けない（所有者 2026-10-08）。
- `ledger.py`・`mcs_adapter.py`・`semantic_drain.py` を行数だけで機械分割しない。
- Hermes plugin の変更は gateway 再起動まで反映されず、旧 worker が新 spec を処理すると本文・添付が欠落し得る。LINE WORKS は独立アダプターの
  再起動が必要（`docs/guides/LINEWORKS.md`）。未適用なら報告する。`deployment/` の編集は実機に適用されない。

## コマンド

```bash
scripts/run_tests.sh tests/notify/       # 対象を絞って実行（一時HOME・認証環境の隔離）
ruff check mcs/ tests/ hermes_plugin/ adapters/ lineworks_adapter/ mcs_standalone/ integration/ ci/ scripts/ deployment/ conftest.py  # CIと同じ範囲
python3 scripts/development/update_readme.py --check     # 生成ブロックの drift 検査（再生成は --check なし）
python3 ci/gates.py
python3 ci/mine_gates.py --check
```

- `make lint`・`make readme`・`make check` も使える（uv があれば ephemeral 実行）。`integration/` も同じ runner に対象を指定する。
- `run_tests.sh` は `MCS_TEST_PYTHON`→`$VIRTUAL_ENV`→repo 直下の `.venv` の順に Python を選ぶ。worktree には `.venv` が無いので、pytest の入った Python を `MCS_TEST_PYTHON` に指定する。
- ユーザー指示（2026-10-31）により、失敗項目を修正して再検証が通れば、既存結果と合わせて全体を合格扱いとし、全体 suite・同一SHAの全6ジョブの再実行は必須にしない。実行済み・失敗後に再検証済み・未実行を区別し、実行していないCIを実測成功と記録しない。記録方法は `docs/development/RELEASE_NOTES.md` に従う。
- 導入先 `~/.mcs`（`MCS_ROOT`）は稼働中の checkout で、原本DB・設定を持つ。開発・検証は git worktree で行い、そこの未commit変更を巻き込まない。
- 全体 suite はこの Mac では走らせない（所有者 2026-11-15）。稼働中の収集とディスク・メモリを取り合い、収集が止まるため。既存の承認範囲で統合用のブランチ（main 以外）を push し、既存の `ci.yml` を `gh workflow run ci.yml --ref <branch>` で実行する。Linux・macOS の検証用の job が走る。Actions の利用条件と課金はリポジトリやランナーの設定に従う。公開用の `publication-request` と、pull request だけで動く生成物の検査は走らないので、公開へは進まず、生成物の検査は上の `update_readme.py --check` をローカルで実行する。main の push・merge・tag・本番適用は別の承認が要る。
- この Mac で対象を絞った pytest を走らせる前に、収集（`run_check.py --json --download-files`）か別の pytest が動いていないかを確かめる。動いていれば上限を決めて待ち、空かなければ始めない。検出を表示するだけで続行しない。
- macOS の BSD `sed` は `\b` を扱わない。単語境界の置換は `perl -pi` を使う。改名後の呼出元確認は `git grep` の結果を切り詰めずに全件見る。
- Hermes 実SDKとの統合は CI の pinned Hermes 環境で別に検証される。ローカル pytest の成功と区別する。
- `python3 mcs/ops/mcs_setup.py check` / `make setup-check` は実機診断。合成 fixture のテストの代わりに実行しない。

## 構成

- `mcs/`: `core/`（保存基盤）・`llm/`（共通推論）・`ingest/`・`extract/`・`patients/`・`medications/`・`signals/`・`semantic/`・`stamps/`・`notify/`・`views/`・`export/`・`ops/`。
  flat import を維持する。エントリポイントは `mcs/` を sys.path に追加して `_mcs_path` を import する（`.py` のある配下を任意の深さで登録）。
  `mcs/` 直下の import モジュールは `_mcs_path.py` だけ。登録対象のサブディレクトリ間で同名モジュールが重複すると停止する。直下の旧ファイルはこの重複検査の対象外なので、配備時に残さない。
- 共通推論: `llm/providers/` はprovider固有の通信、`llm/transport/` は共通通信・再試行・ローカルturn lock、`llm/admission/` は共有枠・接続/回路状態を所有する。
  `cloud_inference`・`cloud_policy`・`cloud_backlog` は `llm/` 直下に置き、従来のflat import名を維持する。
- 抽出: `extract/rules/`＝ルール（v1）、`extract/llm/`＝現行LLM（旧配置v4）、`extract/clinical/`＝臨床値の共通処理。`patients/rollup.py` と
  `semantic/` は世代横断。世代番号は配置名と独立し、v2/v3 は git 履歴にだけある。`extract/eval/`・`semantic/eval/` は定期処理から読まない。
  LLM 枠は昼間（JST 08〜20時）が新着・対話1と背景2、夜間（20〜08時）は背景3。夜間も新着が次の空き枠を優先し、実行中の処理は中断しない。抽出世代・QC・公開条件・モデルの既定は `mcs/extract/`・`mcs/semantic/` と仕様で確かめ、要約から固定しない。
- 収集: MCS サーバー負荷のため未読収集は10分間隔。`stage_self_probe` は未保管の最新 id を bounded 履歴取得し、取れない id は `patients.probe_mid` に記録して再取得を抑える。
- 通知: `adapters/{slack,discord,lineworks,common}/`（独立接続も接続先配下）。`hermes_plugin/` は Hermes 接続・互換入口、`lineworks_adapter/` は独立CLI入口。
  推奨通知先と README 先頭画面は Slack、Discord は対応する選択肢として残す。
- スタンプ: `mcs/stamps/` が種別・読取り・表示・人承認操作を所有。送信は操作者1人と MCS アカウントの設定時だけ既定ON（`stamps.enabled=false` で無効）。
  `stamp_write` の承認記録・復元用記録を通し、通信は既存の `MCSAdapter` だけ（`docs/specs/stamp-management.md`）。
- 独立実行: `runtime_mode=standalone` の `mcs_standalone/` が単一host・7定期ジョブ・cmd取込・抽出worker3本を所有（`docs/guides/STANDALONE.md`）。未指定は Hermes 経路。
- 検証: `tests/` は実行領域に対応、`integration/` は統合、`evaluation/` は完全合成の評価資産。conftest がヘルパー import と socket 遮断を担う。
- `resources/drug-master/`: 公式公開 master の原本と出典・版・hash の manifest（実患者データでも fixture でもない）。リリース時の更新確認・同梱検証と、私有辞書への変換・承認・有効化を分ける。
- 文書: 利用は `docs/guides/`、開発は `docs/development/`、契約は `docs/specs/`。第一層ディレクトリを足したら（ブートストラップは自動対応）この節と `deployment/` のパス表記も更新する。

## 開発レビュー

- ユーザー指示（2026-10-21）により、hermes-mcs の開発に必要な 既に利用を承認したレビューサービスへのコード・テスト・文書・関連する Git 差分とコンテキストの外部送信は承認済み。 利用を終了したサービスへの許可は引き継がない。同じ送信許可を聞き直さず、過去の未承認による保留は解除する。
- 送信内容に認証情報・PHI/PII・実投稿・本番データ・私有ログを含めない。必要なら対象 SHA を維持した独立 clone を使い、実データと不要な過去履歴を分離する。送信される全バイトを事前に確定できないことだけを、確認済みの開発資料のレビューを止める理由にしない。
- 既に完了した承認・検証や、今回に適用されない過去の停止条件を追加の必須工程にしない。skill の一般的な承認待ちよりこの明示許可を優先する。実際の課金・サービス障害・不足環境・実機未確認は事実として報告し、未実施を成功扱いしない。追加課金、公開・配備、本番変更の範囲は別の明示指示に従う。

## 生成物・変更記録・リリース

- `mcs/**/*.py` の追加、docstring の変更、検知器・統計・サブコマンドの追加では `update_readme.py` を実行する。生成表は `docs/development/DEVELOPMENT.md`、
  `README.md` は `GENERATED:release` だけ。生成部分は手編集しない。docstring 先頭文は公開されるので1文要約にする。
- Slack 画面例は完全合成の説明図。`docs/screenshots/slack-gallery/README.md` のソース対応を確認し、画面変更時は SVG・PNG を一緒に再生成して
  `python3 scripts/development/generate_slack_gallery.py --check` で検証する。
- 実行時の挙動を追加・変更・修正・削除したら、同じ作業内で `changes/<識別子>.json` を日本語で作る（形式 `changes/README.md`）。
  summary は利用者への影響を先に、upgrade に必要な操作と適用条件を書く。モデル・取得範囲・通知・既読化・承認条件・既定値の変更は省略しない。
  根拠のない性能数値・実患者情報・実投稿の匿名化例は書かない。
- リリース・準備・リリース前監査は `release-mcs` skill（正本 `~/.agents/skills/release-mcs/SKILL.md`）に従う。毎回公式医薬品マスターの更新を確認し、
  新版は同じ候補へ組み込んで出典・hash・版・利用条件・変換境界を検証する。確認できない新版を「更新なし」と扱わない。
  本番辞書を自動で承認・有効化しない。CHANGELOG・GitHub Release の書式と生成は `docs/development/RELEASE_NOTES.md`、
  README の見直し記録は `docs/development/README_MAINTENANCE.md` に従う。

## 並列開発

- 完了通知ごとに結果と差分を確認し、空いた担当へ次の独立タスクを渡す。全員の終了を待つ一括バリアは置かない。
