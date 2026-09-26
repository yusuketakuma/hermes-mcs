# インストールガイド

hermes-mcs の新規導入手順。導入形態は次の2つ:

- **Path A — hermes-agent アドオン**（推奨・全機能）: 収集→SQLite→
  Discord/Slack 通知＋対話カード。`./install.sh` が依存一式を導入する
- **Path B — スタンドアロン**（hermes-agent なし）: 収集→SQLite→
  `mcs_view` 閲覧＋構造化抽出。通知の配送は `hermes send` 必須のため
  この形態では送られない。後から Path A へ移行できる（§3-6）

## 0. 導入形態の選択

| 機能 | A: hermes アドオン | B: スタンドアロン |
|---|---|---|
| 未読収集 → SQLite → `mcs_view` 閲覧 | ✓ | ✓ |
| 全履歴アーカイブ・FTS5 検索 | ✓ | ✓ |
| 構造化抽出（ルール + ローカルLLM） | ✓ | ✓ |
| レビュー候補シグナル（検出） | ✓ | ✓（`mcs_view signals` で閲覧のみ） |
| Discord/Slack への通知配送 | ✓ | ✗ — `hermes send` が必須 |
| 対話カード・`/mcs` コマンド | ✓ | ✗ — gateway + plugin が必要 |
| semantic v4（shadow/enforce） | ✓ | ✓（通知連携のみ不可） |
| 定期実行の仕組み | hermes cron + launchd | launchd / crontab |
| `mcs_setup.py check` | 全項目検証 | `hermes CLI` 系エラーは想定内（§3-5） |

## 1. 共通の前提条件

| 項目 | 要件 |
|---|---|
| OS | macOS（Keychain・launchd を使用。他 OS は収集本体のみ手動で動くが未検証） |
| Homebrew | `brew` が使えること |
| Python | 3.11–3.13（hermes-agent が `<3.14` を要求） |
| Google Chrome | MCS にログインしたプロファイルで CDP ポート `:9333` を使う |
| ディスク | 約 10 GB（LLM モデル約 6 GB + DB・添付・ログ） |
| MCS アカウント | ログイン ID とパスワード（自動再ログイン `auto_login` で使用） |
| TYPESAFE_API_KEY | semantic/Jev 連携を使う場合のみ（`~/.mcs/.env` に保存） |
| 通知先アプリ | Path A のみ — Discord bot または Slack app（付録A/B で作成） |

## 2. Path A — hermes-agent アドオン（全機能）

### A-1. リポジトリの取得と依存の自動導入

```bash
git clone https://github.com/yusuketakuma/hermes-mcs.git
cd hermes-mcs
./install.sh            # 冪等: 何度実行しても既存分は skip
                        # 引数で HERMES_HOME を変えられる（既定 ~/.hermes）
```

`install.sh` が行うこと（6 ステージ）:

| # | 内容 |
|---|---|
| 1 | brew パッケージ: `git` `python@3.13` `uv` `llama.cpp` `google-chrome`(cask) |
| 2 | hermes-agent を pin 済み commit で `~/.hermes/hermes-agent` に clone・venv 構築・`~/.local/bin/hermes` shim を作成（既存 checkout があれば保持・pin 不一致は警告のみ） |
| 3 | `~/.hermes/plugins/mcs-discord-commands` をこのリポジトリの `hermes_plugin/` に symlink + `hermes plugins enable` |
| 4 | llama-server を `ai.mcs.llamaserver` LaunchAgent で常駐化（`127.0.0.1:8080`・`-np 2`・Qwen3.5-9B GGUF 約6GB を `~/.hermes/models/` へ DL。hermes 管理の `ai.hermes.llamacpp` が既にあれば skip） |
| 5 | `mcs_setup.py services` — launchd agent 4件 + hermes cron 5件の配置・登録（§6 参照） |
| 6 | 復旧 watchdog `org.mcs.recovery` を独立系統で導入（`~/.mcs-recovery/mcs_recover.py`、15分間隔で中断した update を復旧） |

手動で残るのは最後の案内に表示される `mcs_setup.py init`（秘密情報と
選択が必要）だけ。

### A-2. 通知先（Discord / Slack）側の準備

収集する値:

| 用途 | 値 | 入手先 |
|---|---|---|
| Discord | `application_id` | Developer Portal → General Information |
| Discord | `guild_id`（サーバーID） | Discord 開発者モード → サーバー名右クリック |
| Discord | `channel_id`（カード投稿先） | 同上 → チャンネル右クリック |
| Discord | `DISCORD_BOT_TOKEN` | Developer Portal → Bot → Reset Token（**一度しか表示されない**） |
| Discord | 自分の user ID | 開発者モード → 自分の名前右クリック → Copy User ID |
| Slack | `SLACK_BOT_TOKEN`（`xoxb-`） | api.slack.com → Install App |
| Slack | `SLACK_APP_TOKEN`（`xapp-`） | 同上 → Socket Mode で `connections:write` 付き生成 |
| Slack | `team_id`（ワークスペースID） | Slack 管理画面や API |
| Slack | `application_id`（api_app_id） | api.slack.com → Basic Information |
| Slack | `channel_id` | チャンネル名 → チャンネル詳細 → 最下部の ID |
| Slack | member ID（許可ユーザー） | プロフィール → ⋮ → Copy member ID |

アプリ・bot の作成手順は hermes-agent リポジトリのドキュメントを
**付録A（Discord）・付録B（Slack）に転記済み** — そちらをそのまま
使える。MCS 側で必要な権限は付録内の「MCS に必要な最小権限」を参照。

### A-3. `mcs_setup.py init` — 設定ウィザード

```bash
python3 mcs/ops/mcs_setup.py init
```

対話ウィザードが全 config キーをセクション別に案内する（各項目に
説明と既定値を表示、Enter でそのまま進行。関連機能がオフの項目は
自動スキップ）。`init` が行うこと:

- `~/.mcs/config.json` を生成（0600）
- Keychain `mcs-adapter` へ MCS パスワードを登録
- `~/.mcs/.env` に `MCS_PASSWORD`（Keychain ロック中のフォールバック）
  と `TYPESAFE_API_KEY`（環境変数で渡した場合）を保存
- `notify.interactive=discord` を選んだ場合、hermes 側へも書き込む:
  - プラグイン settings を serving profile の config.yaml へ
    （`hermes -p <profile> config set` 経由 — snapshot/inbox/
    allowlists/scope 一式）
  - `DISCORD_BOT_TOKEN` を同 profile の `.env` へ（stdin 経由、
    argv には載せない）

非対話でも実行できる（CI・再現用）:

```bash
MCS_SETUP_PASSWORD=<mcs-pass> TYPESAFE_API_KEY=<key> \
DISCORD_BOT_TOKEN=<token> \
python3 mcs/ops/mcs_setup.py init --yes \
    --login-id <ID> --notify-target discord:<チャンネルID> \
    --set 'notify.interactive="discord"' \
    --set 'notify.discord={"profile":"P","application_id":"A","guild_id":"G","channel_id":"C"}' \
    --plugin-profile <serving profile> \
    --plugin-user-ids <uid> --plugin-chat-ids <chid> \
    --plugin-project-ids <pid>
```

設定キー全一覧は §4 を参照。

### A-4. サービス登録と検証

```bash
python3 mcs/ops/mcs_setup.py services   # launchd + hermes cron + gateway
python3 mcs/ops/mcs_setup.py check      # 必須条件の検証（exit 1 で失敗）
```

- `services` — launchd agent 4件（cmd/cmd_int WatchPaths・extract
  drainer×2）を配置・bootstrap、hermes cron 5件を登録（冪等・
  `--dry-run` で確認可。内容差分は reconcile）。`notify.interactive`
  が `discord`/`slack` のとき `hermes gateway install` + `start` で
  gateway 常駐化も行う
- `check` — config 必須キーの型、Keychain 存在と読取可否、Chrome
  バイナリ、ローカルLLM 到達性とスロット数、`hermes send` の解決、
  gateway の supervised 状態、semantic 有効時の TYPESAFE_API_KEY、
  LaunchAgent 配置、admission broker の経路表を検証する

### A-5. 動作確認

```bash
# 収集（手動で1回実行 — 以後は cron が定期実行）
PY=~/.hermes/hermes-agent/venv/bin/python   # または python3
$PY mcs/ingest/run_check.py --json --download-files --mark-read

# 状態確認
$PY mcs/views/mcs_view.py status
$PY mcs/views/mcs_view.py signals
cat ~/.mcs/data/health.json                # collection/extraction 状態
tail ~/.mcs/data/run.log                   # 実行ログ
```

Discord を使う場合は、対象チャンネルで新着投稿があるとカードが投稿
され、コンパニオンスレッド `💬 患者名 — MM-DD` に本文＋添付が届く。

### A-6. 運用上の注意

- **`hermes_plugin/` のコードを変更・更新したら `hermes gateway
  restart` が必須** — gateway は plugin を起動時に読み込む長寿命
  プロセス。再起動しないと新形式 spec を旧 worker が処理し、カード
  だけ届いてスレッド本文・添付が欠落する（2026-09 実機事案）
- Slack カードを使う場合は `notify.interactive="slack"` +
  `notify.slack` ブロック（`profile`・`application_id`・`team_id`・
  `channel_id`、**guild_id は不可**）と、プラグイン settings の
  `slack_adapter_enabled: true` + `slack_team_id`/`slack_application_id`/
  `slack_channel_id`/`slack_allowed_user_ids`/`slack_profile`/
  `project_ids`/`data_root` を `hermes config set` で手動設定する
  （wizard は Discord のみ対応）

## 3. Path B — スタンドアロン（hermes-agent なし）

収集・保存・閲覧・抽出は hermes-agent なしで動く。**通知系は全て
`hermes send` 経由のためこの形態では送られない**（outbox に pending
として残る。`--no-notify` で送信試行自体を抑止できる）。後から
hermes-agent を追加して Path A に移行できる（§3-6）。

### B-1. 依存の手動導入

`install.sh` は hermes-agent 自体も導入するため使わず、必要なもの
だけを導入する:

```bash
brew install python@3.13 llama.cpp        # uv は任意（依存なし・標準libのみ）
brew install --cask google-chrome
git clone https://github.com/yusuketakuma/hermes-mcs.git
cd hermes-mcs
```

### B-2. 設定（`mcs_setup.py init`）

```bash
python3 mcs/ops/mcs_setup.py init
```

- `mcs_login_id` — MCS のログインID
- `notify_target` — config 検証上の必須キー。通知を送らない運用でも
  便宜値を入れる（例: `local`）。実際の送信は `--no-notify` で抑止
- `notify.interactive` は `off` のまま — Discord/Slack カードは
  gateway なしでは動かない
- MCS パスワードは Keychain `mcs-adapter` + `~/.mcs/.env`
  `MCS_PASSWORD` フォールバックに保存（自動再ログイン用）
- Chrome は MCS ログイン済みプロファイルで `--remote-debugging-port=9333`
  を付けて起動しておく

### B-3. ローカルLLM（extract_llm / semantic を使う場合のみ）

`install.sh` stage 4 相当を手動で行う:

```bash
# モデル（約6GB）
mkdir -p ~/.hermes/models
curl -fL -o ~/.hermes/models/Qwen3.5-9B-Q4_K_M.gguf \
  "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q4_K_M.gguf"

# 常駐サーバ（launchd テンプレートをレンダリングして登録）
REPO=$(pwd)
sed -e "s|__LLAMA_BIN__|$(brew --prefix)/bin/llama-server|g" \
    -e "s|__MODEL__|$HOME/.hermes/models/Qwen3.5-9B-Q4_K_M.gguf|g" \
    -e "s|__HERMES_HOME__|$HOME/.hermes|g" \
    "$REPO/deployment/launchagents/ai.mcs.llamaserver.plist" \
    > ~/Library/LaunchAgents/ai.mcs.llamaserver.plist
mkdir -p ~/.hermes/logs
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/ai.mcs.llamaserver.plist
```

抽出を使わないならこの節はスキップしてよい（`mcs_setup check` が
LLM endpoint の警告を出すが収集自体は動く）。

### B-4. スケジューリング（launchd + crontab）

`mcs_setup.py services` は launchd agent を配置するが、hermes cron
登録は hermes CLI 不在のため skip される（`[note] cron: hermes not
resolvable — skipped`）。主要ジョブは crontab で代替する:

```bash
# wrapper スクリプトをレンダリング（services が ~/.hermes/scripts に配置済み。
# 手動の場合:）
REPO=$(pwd); PY=$(command -v python3); DATA=$HOME/.mcs/data
mkdir -p ~/.mcs/scripts
for s in mcs_check mcs_deep mcs_llm_catchup; do
  sed -e "s|__PYTHON__|$PY|g" -e "s|__REPO__|$REPO|g" -e "s|__DATA__|$DATA|g" \
      "$REPO/deployment/scripts/$s.sh" > ~/.mcs/scripts/$s.sh
  chmod +x ~/.mcs/scripts/$s.sh
done
# mcs_check.sh に --no-notify を足す（通知を送らない運用）:
sed -i '' 's|--mark-read >>|--mark-read --no-notify >>|' ~/.mcs/scripts/mcs_check.sh

crontab -e
```

```cron
*/5 * * * * $HOME/.mcs/scripts/mcs_check.sh
7,37 * * * * $HOME/.mcs/scripts/mcs_deep.sh
30 22 * * * $HOME/.mcs/scripts/mcs_llm_catchup.sh
```

`mcs_check.sh` には夜間間引き（22-06時は :00/:20/:40 のみ実行）が
組み込み済み — 5分 cron のまま貼ればスクリプト側が間引く。
launchd の extract drainer / cmd watcher は `mcs_setup services` で
hermes なしでも登録される。

### B-5. 動作確認と `check` の読み方

```bash
python3 mcs/ingest/run_check.py --json --download-files --mark-read --no-notify
python3 mcs/views/mcs_view.py status
```

`mcs_setup.py check` はスタンドアロンでは次をエラー/警告として報告
する — **想定内**:

- `hermes CLI not resolvable — notifications cannot be sent`
  （配送経路が無いだけ。収集・閲覧・抽出は動作する）

これ以外のエラー（`mcs_login_id`・Keychain・Chrome）は Path A と
同じ基準で対処する。

### B-6. 後から通知を有効にする（Path A への移行）

```bash
./install.sh                                    # hermes-agent + plugin + services
python3 mcs/ops/mcs_setup.py init               # notify.interactive=discord 等を設定
python3 mcs/ops/mcs_setup.py services && python3 mcs/ops/mcs_setup.py check
```

`--no-notify` を付けて運用していた場合は wrapper の該当フラグを除き、
outbox に残った pending は次回 flush で配送される（古いものは
`notify_max_age_h` で送らない設定も可能）。Discord/Slack アプリの
作成は付録A/B（hermes-agent リポジトリのドキュメント転記）の手順。

## 4. config.json 設定リファレンス

`~/.mcs/config.json`（0600）。`init` のウィザードが全項目を案内する
（`--set KEY=JSON` で非対話設定も可）。必須は `mcs_login_id` と
`notify_target` の2つのみ。

| キー | 型 | 既定 | 説明 |
|---|---|---|---|
| `mcs_login_id` | str | — （必須） | MCS のログインID |
| `notify_target` | str | — （必須） | 通知の送り先。`discord:<チャンネルID>`・`slack:#ch` 等 `hermes send --to` 形式。スタンドアロンでは便宜値 |
| `notify.interactive` | choice | `off` | `discord`/`slack`=対話カード / `off`=テキストのみ |
| `notify.discord.profile` | str | — | 配送に使う hermes プロファイル（interactive=discord 時必須） |
| `notify.discord.application_id` | str | — | Discord アプリケーションID（同上） |
| `notify.discord.guild_id` | str | — | Discord サーバーID（同上） |
| `notify.discord.channel_id` | str | — | カードの投稿先チャンネルID（同上） |
| `notify.slack.profile/application_id/team_id/channel_id` | str | — | Slack 版の配送スコープ（interactive=slack 時必須。guild_id 不可） |
| `notify.operator` | str | なし | 運用者の Discord ユーザーID |
| `notify.card_thread` | bool | `true`（ウィザード既定。キー未設定のconfigではオフ） | 患者スレッドごとにカードのコンパニオンスレッドを立て本文・添付を配送 |
| `notify.card_thread_archive_min` | int | `10080` | 設定キーのみ（自動アーカイブは未実装） |
| `notify.route_epoch` | int | `1` | 配送先を変えたとき +1 する番号 |
| `notify_bot_profile` | str | なし | 通知投稿に使う hermes プロファイル（`[a-z0-9_-]+`） |
| `notify_system_target` | str | なし | 障害・システム通知の送り先（空欄=`notify_target` と同じ） |
| `notify_max_age_h` | num | なし | この時間より古い未読は通知しない |
| `hermes_bin` | str | 自動検出 | `hermes` コマンドのパス |
| `self_posts` | bool | `false` | 自分の投稿も取り込んで通知（latest probe 経由） |
| `deep_history` | bool | `true` | 初回に全履歴を遡って保存 |
| `discover_archived` | bool | `false` | アーカイブ済み患者も収集対象にする |
| `trickle_pages` | int(1-40) | `3` | 1回の実行で履歴を遡るページ数 |
| `job_budget_seconds` | num | 既定 | 内部処理の時間予算・秒 |
| `signals.notify` | bool | `false` | 確認候補を通知に出す |
| `signals.digest` | bool | `false` | 複数候補をダイジェストにまとめる |
| `signals.digest_interval_h` | num | 既定 | ダイジェスト間隔・時間 |
| `signals.self_organizations` | list[str] | 自動検出 | 自施設名（MCS プロフィールから自動検出を上書き） |
| `signals.self_professions` | list[str] | 自動検出 | 自職種（同上） |
| `signals.request_targets` | list[str] | なし | 依頼先として数える宛名 |
| `signals.med_exclude_names` | list[str] | なし | 薬剤判定から除外する語 |
| `semantic.mode` | choice | `off` | `shadow`=記録のみ / `enforce`=判定に使用 |
| `semantic.project_ids` | list[int] | — | 対象プロジェクトID（mode が off 以外では必須） |
| `semantic.extract_qc` | choice | `off` | `annotate`=抽出結果への Jev 監査注記 |
| `semantic.daily_request_budget` | int | 既定 | Jev 呼出の1日上限 |
| `update.mode` | choice | `off` | `off`/`notify`/`auto` — 自己更新ポリシー |
| `update.auto_delay_h` | num | — | auto 時の適用遅延（0=検出次第即適用） |
| `update.include_prerelease` | bool | `false` | プレリリースを更新対象に含める |
| `health.max_missed_runs` | int | `4` | freshness deadline 係数（夜間間引きに合わせ25分相当） |

## 5. 秘密情報の配置

| 場所 | 内容 | 備考 |
|---|---|---|
| Keychain `mcs-adapter` | MCS パスワード | `auto_login` がフォーム投入時に読む |
| `~/.mcs/.env` (0600) | `MCS_PASSWORD`（Keychain ロック中のフォールバック）・`TYPESAFE_API_KEY` | 平文 — FileVault/物理セキュリティ前提 |
| hermes profile `.env` | `DISCORD_BOT_TOKEN` / `SLACK_BOT_TOKEN`+`SLACK_APP_TOKEN` | `init` または `hermes config set --stdin` で書込み（argv に載せない） |

## 6. スケジュール構成（Path A 導入後）

収集ジョブは **hermes cron 5件 + launchd 5件** のハイブリッド:

| ジョブ | スケジュール | 実行系 |
|---|---|---|
| 未読チェック `run_check --download-files --mark-read` | `*/5 * * * *`（スクリプト内で 22-06時は :00/:20/:40 のみに間引き — 30分のセッション失効上限を下回るため） | hermes cron |
| durable-job drain `--jobs-only` | `7,37 * * * *` | hermes cron |
| semantic/QC 夜間 drain | `30 22 * * *`（最大55分） | hermes cron |
| llama-server 再起動 | `0 4 * * *` | hermes cron |
| 更新チェック `mcs_update check` | `10 5 * * *` | hermes cron |
| 更新復旧 `mcs_recover --if-stale` | 15分間隔 | launchd `org.mcs.recovery` |
| コマンド取込 `data/cmd/` | WatchPaths（イベント駆動） | launchd `local.mcs-cmd` |
| 対話カード `data/cmd_int/` | WatchPaths | launchd `local.mcs-int` |
| extract_llm drainer（shard 0/2） | KeepAlive・120s poll | launchd `ai.mcs.extract-drainer` |
| extract_llm RT貸与drainer（shard 1/2） | 同上 | launchd `ai.mcs.extract-drainer-rt` |

## 7. トラブルシューティング

| `check` の出力 / 症状 | 原因・対処 |
|---|---|
| `missing required key: mcs_login_id` / `notify_target` | `init` で再登録（両方とも必須） |
| `Keychain entry 'mcs-adapter' not found` | パスワード未登録 — `init` で登録（`.env` `MCS_PASSWORD` があれば警告に格下げ） |
| `Keychain entry ... unreadable` | login keychain がロック中 — `security unlock-keychain` か GUI ログイン。頻発なら `security set-keychain-settings ~/Library/Keychains/login.keychain-db` で自動ロック無効化 |
| `Chrome binary missing` | Chrome が `/Applications` に無い — `brew install --cask google-chrome` |
| `local LLM endpoint 127.0.0.1:8080 not reachable` | llama-server 未起動 — Path A-1/§B-3。収集自体は動く（警告） |
| `llama-server advertises N slots` | `-np` が選択スロット数（2）未満 — plist の `-np 2` を確認 |
| `hermes CLI not resolvable` | hermes 未導入（Path B では想定内。Path A なら `install.sh` 再実行か `hermes_bin` 設定） |
| `hermes gateway is not supervised` | `services` を実行（`hermes gateway install`+`start` で常駐化） |
| `session_expired` 通知が来る | セッション失効 — `auto_login` が `_recover_session`→フォーム投入で復旧を試みる。`manual_required`/`keychain_locked` は上記 Keychain 節を参照 |
| カードだけ届きスレッド本文が無い | gateway が旧 plugin を保持 — `hermes gateway restart`（§A-6） |
| `TYPESAFE_API_KEY is not resolvable` | semantic 有効時に必須 — `~/.mcs/.env` に登録 |

## 付録A: Discord 接続設定（hermes-agent リポジトリより転記）

> 以下は `hermes-agent` リポジトリの
> `website/docs/user-guide/messaging/discord.md` から、MCS の通知・
> カード配送に必要な部分を転記したもの。最新の全文は
> hermes-agent checkout の同ファイルを参照。

### Discord の bot 作成（Step 1–8）

**Step 1: Create a Discord Application** — Go to the Discord Developer
Portal and sign in. Click **New Application**, enter a name, click
**Create**. On the **General Information** page, note the
**Application ID**.

**Step 2: Create the Bot** — In the left sidebar, click **Bot**.
Under **Authorization Flow**, set **Public Bot** ON (recommended) and
leave **Require OAuth2 Code Grant** OFF.

**Step 3: Enable Privileged Gateway Intents** — On the **Bot** page,
scroll to **Privileged Gateway Intents** and enable **Server Members
Intent** and **Message Content Intent** (both required — without Message
Content Intent the bot cannot see message text). Click **Save Changes**.
If your bot is in 100+ servers, Discord requires a verification
application for privileged intents.

**Step 4: Get the Bot Token** — Still on the **Bot** page, under
**Token** click **Reset Token** (2FA code if enabled). **Copy it
immediately — the token is shown only once.** Never share or commit it.

**Step 5: Generate the Invite URL** — Option A (recommended): sidebar →
**Installation** → enable **Guild Install** → **Discord Provided Link**;
under Default Install Settings select scopes `bot` and
`applications.commands` plus the permissions below. Option B (manual):

```text
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=274878286912
```

Required permissions: View Channels, Send Messages, Embed Links,
Attach Files, Read Message History. Recommended additions: Send
Messages in Threads, Add Reactions. Permission integers: minimal
`117760`, recommended `274878286912`.

**Step 6: Invite to Your Server** — open the invite URL, select your
server, **Authorize** (requires Manage Server permission).

**Step 7: Find Your Discord User ID** — Discord Settings → Advanced →
Developer Mode ON → right-click your username → **Copy User ID**.
Channel/Server IDs are copied the same way.

**Step 8: Configure** — `hermes gateway setup`（対話）または
`~/.hermes/.env` に `DISCORD_BOT_TOKEN` と `DISCORD_ALLOWED_USERS`
を書き、`hermes gateway` で起動。MCS では `mcs_setup.py init` が
token を serving profile の `.env` に自動登録する（§A-3）。

### MCS に必要な最小権限（Discord）

カード配送・コンパニオンスレッドには上記に加えて:

- **Create Public Threads** — `💬 患者名 — MM-DD` スレッドを立てる
- **Send Messages in Threads** — スレッド内に本文・添付を投稿
- **Attach Files** — PDF・画像の添付配送

`Message Content Intent` は thread 内本文の remote-match（重複配送
防止）と `/mcs` コマンドのメッセージ照合に必要。

## 付録B: Slack 接続設定（hermes-agent リポジトリより転記）

> 以下は `hermes-agent` リポジトリの
> `website/docs/user-guide/messaging/slack.md` から転記。Hermes は
> Socket Mode（WebSocket・公開URL不要）で Slack と接続する。

### Slack のアプリ作成（Step 1–9）

**Step 1: Create a Slack App** — Option A（推奨）: `hermes slack
manifest --agent-view --write` で `~/.hermes/slack-manifest.json` を
生成し、[api.slack.com/apps](https://api.slack.com/apps) →
**Create New App** → **From an app manifest** に貼り付け（スコープ・
イベント・Socket Mode が自動設定される）。Option B: **From scratch**
で手動作成し Steps 2–6 を実施。

**Step 2: Bot Token Scopes** — **Features → OAuth & Permissions** の
Bot Token Scopes に追加: `chat:write`, `app_mentions:read`,
`channels:history`, `channels:read`, `groups:history`, `im:history`,
`im:read`, `im:write`, `mpim:history`, `mpim:read`, `users:read`,
`files:read`, `files:write`（channels:history/groups:history が無いと
チャンネルメッセージを受け取れない）。

**Step 3: Enable Socket Mode** — **Settings → Socket Mode** を ON、
`connections:write` スコープで **App-Level Token** を生成 →
`xapp-` トークンをコピー（`SLACK_APP_TOKEN`）。

**Step 4: Subscribe to Events** — **Features → Event Subscriptions**
を ON → Subscribe to bot events: `message.im`, `message.mpim`,
`message.channels`, `message.groups`（推奨）, `app_mention` → Save。

**Step 5: Enable the Messages Tab** — **Features → App Home** →
Show Tabs → **Messages Tab** ON → 「Allow users to send Slash commands
and messages from the messages tab」をチェック（これが無いと DM が
完全にブロックされる）。

**Step 6: Install App to Workspace** — **Settings → Install App** →
Install → Allow → `xoxb-` の **Bot User OAuth Token** をコピー
（`SLACK_BOT_TOKEN`）。スコープやイベントを変えたら再インストール
が必要。

**Step 7: Find User IDs** — ユーザー名 → View full profile → ⋮ →
**Copy member ID**（`U01...` 形式）。

**Step 8: Configure** — `~/.hermes/.env` に:

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
SLACK_ALLOWED_USERS=U01ABC2DEF3        # カンマ区切り Member ID
SLACK_HOME_CHANNEL=C01234567890        # 任意: cron/通知の既定ch
```

**Step 9: Invite the Bot** — 各チャンネルで `/invite @<app名>` を
実行（bot は自動参加しない）。

### MCS に必要な設定（Slack）

- runner 側 config: `notify.interactive="slack"` +
  `notify.slack={"profile","application_id","team_id","channel_id"}`
  （`guild_id` は不可 — team_id を使う）
- plugin settings（`hermes config set
  plugins.entries.mcs-discord-commands.settings.<key>` で手動）:
  `slack_adapter_enabled: true`, `slack_team_id`,
  `slack_application_id`, `slack_channel_id`,
  `slack_allowed_user_ids`, `slack_profile`, `project_ids`,
  `data_root`, `snapshot`
