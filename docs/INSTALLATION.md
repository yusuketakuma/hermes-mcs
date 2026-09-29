# インストールガイド

> **AI エージェントにセットアップさせる場合:** 対話実行用の手順書は
> [SETUP_AGENT.md](SETUP_AGENT.md) にあります — その文書を
> エージェントに読み込ませれば、前提確認→形態選択→設定投入→検証
> までを対話的に実行します。

## 最短手順（Path A・初めての方向け）

**事前に用意するもの**（Python は不要 — `install.sh` が導入する）:

- macOS 13 以降の Mac（Apple silicon 推奨）に、普段使いのユーザーで
  ログインしていること（`sudo` や root では実行しない）
- Xcode Command Line Tools（`xcode-select --install`）と
  [Homebrew](https://brew.sh/)
- 空きディスク約 12 GB（LLM モデル 5.7 GB を含む）と、github.com・
  huggingface.co へ接続できるネットワーク
- MCS のログイン ID とパスワード
- 通知を使う場合: Discord bot または Slack app（作り方は付録A/B）

**手順**:

```bash
git clone https://github.com/yusuketakuma/hermes-mcs.git
cd hermes-mcs

./install.sh --preflight   # 1. 読取り専用の事前チェック（何も書き込まない）
                           #    NG 行の下の "fix:" を実行し、
                           #    "preflight: 0 blocker(s)" になるまで繰り返す
./install.sh --dry-run     # 2. （任意）各ステージで何が作られるかを表示
./install.sh               # 3. 本導入。途中で止まっても直して再実行すれば続きから進む
```

`install.sh` の最後に `Installed. Summary:` と、次に実行するコマンドが
**フルパスで**表示される。それをそのままコピーして実行する（下はその形。
`python` は install.sh が作った venv のインタプリタで、macOS 標準の
`python3` では動かない）:

```bash
PY=~/.hermes/hermes-agent/venv/bin/python
$PY mcs/ops/mcs_setup.py init       # 4. 設定ウィザード（ID・パスワード・通知先など）
$PY mcs/ops/mcs_setup.py services   # 5. 定期実行の登録を最新設定で再同期
$PY mcs/ops/mcs_setup.py check      # 6. 検証。問題は "blockers (N) — fix in this order" に順番付きで出る
$PY mcs/ops/mcs_setup.py doctor     #    （困ったとき）check + インタプリタ・launchd の状態一覧
```

**成功の目安**:

- `--preflight` の最終行が `preflight: 0 blocker(s), N warning(s)`（exit 0）
- `install.sh` が `Installed. Summary:` と 6 ステージ分の結果を表示して終わる
  （途中で止まった場合は `error: installation stopped; ...` が出る — §7-1）
- `check` の最終行が `check: OK (0 errors, N warnings)`（exit 0）。
  `warn` 行は動作を止めないが、内容は一度確認する
- `doctor` の `launchd :` 行で `local.mcs-cmd`・`local.mcs-int`・
  `ai.mcs.extract-drainer`・`ai.mcs.extract-drainer-rt`・`org.mcs.recovery`
  と、llama-server（`ai.hermes.llamacpp` か `ai.mcs.llamaserver` の
  どちらか）が `loaded`

メッセージ別の対処は §7 トラブルシューティング。詳細な仕組みは以下の各節。

## 概要

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
| `mcs_setup.py check` | 全項目検証 | `hermes CLI`・services 用インタプリタ等のエラーは想定内（§3 B-5） |

## 1. 共通の前提条件

| 項目 | 要件 |
|---|---|
| OS | macOS 13 以降（Keychain・launchd を使用。他 OS は収集本体のみ手動で動くが未検証） |
| 実行ユーザー | 普段使いのユーザー。`install.sh` は root / `sudo` 実行を拒否する |
| Xcode Command Line Tools | `xcode-select --install`（git と Homebrew が必要とする） |
| Homebrew | `brew` が使えること（Path A で `--no-brew` を使わない場合） |
| Python | 事前準備は不要 — Path A は `install.sh` が `python@3.13` を導入し、hermes-agent の venv（3.11–3.13、`<3.14`）を作る。`mcs_setup.py` 等は Python ≥3.10 が必要で、macOS 標準の `/usr/bin/python3`（3.9 系）では動かない |
| Google Chrome | MCS にログインしたプロファイルで CDP ポート `:9333` を使う |
| ディスク | 約 12 GB（LLM モデル約 5.7 GB + DB・添付・ログ。`--preflight` が確認する） |
| MCS アカウント | ログイン ID とパスワード（自動再ログイン `auto_login` で使用） |
| TYPESAFE_API_KEY | semantic/Jev 連携を使う場合のみ（`~/.mcs/.env` に保存） |
| 通知先アプリ | Path A のみ — Discord bot または Slack app（付録A/B で作成） |

## 2. Path A — hermes-agent アドオン（全機能）

### A-1. リポジトリの取得と依存の自動導入

```bash
git clone https://github.com/yusuketakuma/hermes-mcs.git
cd hermes-mcs
./install.sh --preflight   # 読取り専用の事前チェック（OK / WARN / NG と fix: を表示）
./install.sh               # 冪等: 何度実行しても既存分は skip、中断分は再開
```

`./install.sh -h` が全オプションを表示する。主なもの:

| オプション | 動作 |
|---|---|
| `--preflight`（別名 `--check-only`） | 前提条件を読取り専用で確認し `OK`/`WARN`/`NG` を表示。NG には `fix:` 行で直し方が付く。NG が1つでもあれば exit 1。何も書き込まない |
| `--dry-run` | preflight に加えて、各ステージが何を作成・変更するか（`[new]`/`[exists]`）を表示。何も書き込まない。NG があれば exit 1 |
| `--force-repo` | 別の checkout から導入済みの環境（plugin symlink・`~/.mcs-recovery/repo_path`・services）を、この checkout に切り替えることを許可する（§A-7） |
| `--no-brew` / `--no-llm` / `--no-plugin` / `--no-services` / `--no-recovery` | ステージ 1 / 4 / 3 / 5 / 6 をスキップ（下記） |
| `[HERMES_HOME]`（位置引数） | 既定 `~/.hermes`。既定以外は services ステージが非対応のため `--no-services` 併用が必須（無いと exit 2） |

- 未知のオプション（`-x` など `-` で始まる引数）や2つ目の位置引数は
  exit 2。root / `sudo` での実行は拒否する（exit 1）
- 環境変数 `HERMES_HOME` は使われない（引数と異なる値なら警告し、
  導入先は引数か既定 `~/.hermes`）
- 新しく作るファイル・ディレクトリは所有者のみ読み書き可（`umask 077`。
  既存ファイルの権限は変えない。brew の導入物は brew 既定の権限）
- git worktree から実行すると警告する — services と plugin がその
  worktree を指すため、削除すると壊れる。main checkout から実行する

一部導入済みの環境ではステージ単位でスキップできる:

```bash
./install.sh --no-llm        # 自前の LLM サーバを使う
                             #   （MCS は 127.0.0.1:8080 の
                             #     OpenAI 互換エンドポイントを期待）
./install.sh --no-plugin     # プラグイン導入を自分で管理する
                             #   （interactive=off のテキスト通知のみ
                             #     なら不要）
./install.sh --no-brew       # brew 管理外の依存を自前で用意済み
./install.sh --no-services   # launchd/cron 登録を後で行う
./install.sh --no-recovery   # 復旧 watchdog を導入しない
```

フラグ無しでも検出ベースで skip する: `:8080` で OpenAI 互換
エンドポイントが応答し、`ai.mcs.llamaserver` の plist も無ければ
llama-server の導入・モデル DL をスキップし、導入済みの
`plugins.enabled`・launchd agent・hermes cron・`~/.hermes/hermes-agent`
checkout はそのまま残る。`:8080` を占有する自前サーバがある場合、
そのサーバが MCS の LLM として使われる（`/slots` が `SLOT_COUNT` と
一致するか `check` が検証する）。`hermes` が既に PATH にあっても
ステージ 2 の venv（`~/.hermes/hermes-agent/venv`）は作る — services が
全ジョブをこのインタプリタで起動するため。

`install.sh` が行うこと（6 ステージ）。**失敗したステージで install は
止まり**（非 0 終了・`error: installation stopped; repair the failed stage
and re-run install.sh`）、後続ステージは実行されない。原因を直して
再実行すれば、完了済みの部分は skip され中断箇所から続く:

| # | 内容 | 再実行・失敗時 |
|---|---|---|
| 1 | brew パッケージ: `git` `python@3.13` `uv` `llama.cpp` `google-chrome`(cask)。導入済みは skip | `brew` が無い・`brew install` 失敗で停止 |
| 2 | hermes-agent を pin 済み commit で `~/.hermes/hermes-agent` に clone・venv 構築（`pip install -e hermes-agent[messaging]`）・`~/.local/bin/hermes` shim を作成。既存 checkout は保持し、pin 不一致は警告のみ。MCS のものでない shim ファイルは上書きしない | 中断した clone/checkout・Python の無い venv・失敗した pip install は再実行時にやり直す。clone・venv・pip の失敗で停止 |
| 3 | `~/.hermes/plugins/mcs-discord-commands` をこのリポジトリの `hermes_plugin/` に symlink + `hermes plugins enable`（discord/slack 両対応の1プラグイン。導入済みなら skip） | 同名の非 symlink がある・enable 失敗で停止 |
| 4 | llama-server を `ai.mcs.llamaserver` LaunchAgent で常駐化（`127.0.0.1:8080`・`-np 2`・Qwen3.5-9B GGUF 約 5.7 GB を `~/.hermes/models/` へ DL）。hermes 管理の `ai.hermes.llamacpp` の plist があれば導入せず、未ロードで `:8080` も無応答なら bootstrap コマンドを警告で案内。plist 内容が変わった場合、サーバが応答中なら再読込せず適用コマンドを警告で案内（処理中の要求を切らない）、無応答なら再読込 | モデル DL は `.part` から続きを再開（`curl -C -`）。`MCS_MODEL_SHA256` を設定すると DL 後に sha256 を照合し、不一致なら `.part` を消して停止。`llama-server` 不在・DL 失敗・bootstrap 失敗で停止 |
| 5 | `~/.mcs/data{,/cmd,/cmd_int}` を作成し、`mcs_setup.py services` — launchd agent 4件 + hermes cron 6件の配置・登録（§6 参照） | services が問題を報告したら停止 |
| 6 | 復旧 watchdog `org.mcs.recovery` を独立系統で導入（`~/.mcs-recovery/mcs_recover.py`、旧版は `.prev`。復旧対象の checkout を `~/.mcs-recovery/repo_path` に記録。15分間隔で中断した update を復旧）。内容・記録が変わった時か未ロード時だけ再読込 | bootstrap 失敗で停止 |

最後に `Installed. Summary:` として各ステージの結果と、次に実行する
コマンド（venv インタプリタのフルパス付きの `init`・非対話版の例・
`services`・`check`）が表示される。手動で残るのは `mcs_setup.py init`
（秘密情報と選択が必要）だけ。

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
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py init
```

（`mcs_setup` は Python ≥3.10 が必要。install.sh が用意した venv
インタプリタを使うのが確実 — macOS 標準の `/usr/bin/python3` は
3.9 系で古い）

対話ウィザードが全 config キーをセクション別に案内する（各項目に
説明と既定値を表示、Enter でそのまま進行。関連機能がオフの項目は
自動スキップ）。`init` が行うこと:

- `~/.mcs/config.json` を生成（0600）
- Keychain `mcs-adapter` へ MCS パスワードを登録
- `~/.mcs/.env` に `MCS_PASSWORD`（Keychain ロック中のフォールバック）
  と `TYPESAFE_API_KEY`（環境変数で渡した場合）を保存
- `notify.interactive=discord`/`slack` を選んだ場合、hermes 側へも
  書き込む（Slack は `slack_*` settings キーにマップされる）:
  - プラグイン settings を serving profile の config.yaml へ
    （`hermes -p <profile> config set` 経由 — snapshot/inbox/
    allowlists/scope 一式）
  - `DISCORD_BOT_TOKEN`（Slack は `SLACK_BOT_TOKEN`+
    `SLACK_APP_TOKEN`）を同 profile の `.env` へ（stdin 経由、
    argv には載せない。既設定済みのトークンは残る）

非対話でも実行できる（CI・再現用）:

```bash
MCS_SETUP_PASSWORD=<mcs-pass> TYPESAFE_API_KEY=<key> \
DISCORD_BOT_TOKEN=<token> \
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py init --yes \
    --login-id <ID> --notify-target discord:<チャンネルID> \
    --set 'notify.interactive="discord"' \
    --set 'notify.discord={"profile":"P","application_id":"A","guild_id":"G","channel_id":"C"}' \
    --plugin-profile <serving profile> \
    --plugin-user-ids <uid> --plugin-chat-ids <chid> \
    --plugin-project-ids <pid>
```

`--set` の値は JSON（文字列は内側の引用符が必要）。例は
`init --help` の末尾にも表示される。

- 既存の `~/.mcs/config.json` が壊れている（JSON として読めない・
  オブジェクトでない）場合、`init` は何も書かずに停止する（exit 1）。
  手で直すか、`init --yes` で `config.json.corrupt-<日時>`（0600・
  中身はそのまま）へ退避して既定値から作り直す
- `init` は終了時に `check` を自動実行し、その結果を終了コードにする

設定キー全一覧は §4 を参照。個別キーは `--set KEY=JSON`（例: `--set self_posts=true`）、Jev 連携は `--semantic-mode`（`off`/`shadow`/`enforce`）で指定できる。

### A-4. サービス登録と検証

```bash
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py services   # launchd + hermes cron + gateway
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py check      # 必須条件の検証（exit 1 で失敗）
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py doctor     # check + 環境の事実一覧（診断・不具合報告用）
```

- `services` — launchd agent 4件（cmd/cmd_int WatchPaths・extract
  drainer×2）を配置・bootstrap、hermes cron 6件を登録（冪等・
  `--dry-run` で確認可。内容差分は reconcile）。`notify.interactive`
  が `discord`/`slack` のとき `hermes gateway install` + `start` で
  gateway 常駐化も行う。全 wrapper・plist は
  `~/.hermes/hermes-agent/venv/bin/python` で起動するよう描画される
  ため、このインタプリタが無い（または Python ≥3.10 でない）場合は
  何も描画せず exit 1 で止まる（install.sh の再実行でステージ 2 が
  venv を作り直す）
- `check` — 次の順に検証し、エラーを優先度順に並べる:
  1. 定期ジョブの実行基盤: services 用インタプリタ
     （`~/.hermes/hermes-agent/venv/bin/python`）、`hermes` が launchd の
     最小 PATH（`~/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin`）でも解決
     できるか、復旧 watchdog `org.mcs.recovery`（導入有無・repo 版との
     差分・`~/.mcs-recovery/repo_path` がこの checkout か・ロード状態）、
     llama-server agent（`ai.hermes.llamacpp`/`ai.mcs.llamaserver`）の
     ロード状態
  2. config: 読める JSON か、必須キーの型、任意キーの範囲
  3. マシン: Keychain 存在と読取可否、Chrome バイナリ、ローカルLLM
     到達性とスロット数、`hermes send` の解決、gateway の supervised
     状態、semantic 有効時の TYPESAFE_API_KEY、LaunchAgent の配置と
     ロード状態、admission broker の経路表
  4. 配置 drift: `~/.hermes/scripts/` の wrapper が repo から描画した
     内容と一致するか

  出力は `warn :` / `error:` 行の後、エラーがあれば
  `blockers (N) — fix in this order:` に番号付きで要約（各項目に
  `fix:` 行）、最後に `check: OK|FAIL (N errors, N warnings)`。
  上から順に直して再実行する
- `doctor` — 実行中の Python・services 用インタプリタとその可否・
  `hermes` の解決先（現在の PATH と launchd PATH）・repo・config の
  パス・各 launchd agent（MCS 4件・`org.mcs.recovery`・llama-server
  2候補）の `loaded`/`not loaded` を表示してから `check` を実行する
  （終了コードも `check` と同じ）

### A-5. 動作確認

```bash
# 収集（手動で1回実行 — 以後は cron が定期実行）
PY=~/.hermes/hermes-agent/venv/bin/python   # install.sh が作った venv（標準の python3 3.9 系は不可）
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
  だけ届いてスレッド本文・添付が欠落する（2026-09 実機事案）。
  リンクボタン（`🔗 MCSで開く`）や人名表示（`parts.mentions`）を含む
  カードは、旧 worker では配送されず保留される（再起動後に配送）
- **カードを操作できる人を増やす（Discord）** — 薬局スタッフ全員に
  ロールを付け、そのロール ID を `allowed_role_ids` に入れる:
  `python3 mcs/ops/mcs_setup.py init --plugin-role-ids <role id>[,<role id>]`
  （または `hermes -p <profile> config set
  plugins.entries.mcs-discord-commands.settings.allowed_role_ids
  '["<role id>"]'`）。guild ID（@everyone ロール）は指定できない。
  ロールを取得できないメンバーは許可されない（fail closed）。ユーザー単位なら `allowed_user_ids` に列挙する。
  いずれも設定後に `hermes gateway restart`。Slack は
  `slack_allowed_user_ids` に複数の member ID を列挙する
- Slack カードを使う場合は `notify.interactive="slack"` +
  `notify.slack` ブロック（`profile`・`application_id`・`team_id`・
  `channel_id`、**guild_id は不可**）。wizard/init の対応で
  プラグイン settings（`slack_adapter_enabled: true` +
  `slack_team_id`/`slack_application_id`/`slack_channel_id`/
  `slack_allowed_user_ids`/`slack_profile`/`project_ids`/
  `data_root`）と `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` を serving
  profile に自動で書き込む — 既設定の値は保持される

### A-7. checkout の場所と移動

導入物は install.sh を実行した checkout の**絶対パス**を指す:
`~/.hermes/plugins/mcs-discord-commands` の symlink、services が描画する
wrapper・plist（`__REPO__`）、復旧ツールが復旧する checkout を記録した
`~/.mcs-recovery/repo_path`（1行のパス。無い場合 `mcs_recover.py` は
`~/.mcs` を復旧対象とみなす）。

- **checkout を移動・再 clone したら、新しい場所で `./install.sh` を
  再実行する**（ステージ 3・5・6 が新しいパスへ張り替える）。移動だけ
  では plugin・定期ジョブ・watchdog が旧パスを指したまま壊れる
- 旧 checkout がまだ存在する場合、install.sh は
  `this machine is installed from another checkout (<旧パス>)` で停止する
  （preflight では `existing install points at another checkout` の NG）。
  切り替える意図なら `./install.sh --force-repo`、そうでなければ旧 checkout
  側の `install.sh` を使う。旧パスが既に存在しなければ競合扱いにならない
- 記録のずれは `check` が
  `recovery watchdog recovers <パス>, not this checkout <パス>` として警告する

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
PY=python3.13                              # brew の python@3.13。以下のコマンドはすべて $PY で実行
$PY -V                                     # Python 3.13.x と出ること
```

MCS のスクリプトは Python ≥3.10 が必要。素の `python3` は新規 Mac では
`/usr/bin/python3`（3.9 系）になり `mcs_setup requires Python >= 3.10`
で止まるため使わない。B-4 の wrapper にもこのインタプリタのパスが
焼き込まれる。以下は同じシェルで `PY` を設定した前提。

### B-2. 設定（`mcs_setup.py init`）

```bash
$PY mcs/ops/mcs_setup.py init
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

# 常駐サーバ（パスは XML としてエスケープして配置）
$PY - <<'PYSETUP'
from pathlib import Path
import re
import shutil
from xml.sax.saxutils import escape
home = Path.home()
llama = shutil.which("llama-server")
if not llama:
    raise SystemExit("llama-server not found")
values = {"__LLAMA_BIN__": llama,
          "__MODEL__": str(home / ".hermes/models/Qwen3.5-9B-Q4_K_M.gguf"),
          "__HERMES_HOME__": str(home / ".hermes")}
template = Path("deployment/launchagents/ai.mcs.llamaserver.plist").read_text()
pattern = "|".join(re.escape(key) for key in values)
body = re.sub(pattern, lambda match: escape(values[match.group()]), template)
target = home / "Library/LaunchAgents/ai.mcs.llamaserver.plist"
target.parent.mkdir(parents=True, exist_ok=True)
(home / ".hermes/logs").mkdir(parents=True, exist_ok=True)
target.write_text(body)
PYSETUP
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/ai.mcs.llamaserver.plist"
```

抽出を使わないならこの節はスキップしてよい（`mcs_setup check` が
LLM endpoint の警告を出すが収集自体は動く）。

### B-4. スケジューリング（crontab）

通知を送らない wrapper を生成し、主要ジョブを crontab で実行する。
既存の収集ジョブがある場合は、重複して登録せず、その起動経路も確認する:

```bash
# スタンドアロン用 wrapper を正本からレンダリングする。
# shell 引数を引用し、通知を送らない runner の起動に --no-notify を追加する。
# __PYTHON__ にはこのコマンドを実行したインタプリタ（$PY）が入る。
$PY - <<'PYSETUP'
from pathlib import Path
import re
import shlex
import sys
repo = Path.cwd()
home = Path.home()
values = {"__PYTHON__": sys.executable, "__REPO__": str(repo),
          "__DATA__": str(home / ".mcs/data")}
pattern = "|".join(re.escape(key) for key in values)
out = home / ".mcs/scripts"
out.mkdir(parents=True, exist_ok=True)
for name in ("mcs_check", "mcs_deep", "mcs_llm_catchup"):
    body = (repo / "deployment/scripts" / (name + ".sh")).read_text()
    body = body.replace("run_check.py", "run_check.py --no-notify")
    body = re.sub(pattern, lambda match: shlex.quote(values[match.group()]), body)
    target = out / (name + ".sh")
    target.write_text(body)
    target.chmod(0o755)
PYSETUP

crontab -e
```

```cron
*/5 * * * * $HOME/.mcs/scripts/mcs_check.sh
7,37 * * * * $HOME/.mcs/scripts/mcs_deep.sh
30 22 * * * $HOME/.mcs/scripts/mcs_llm_catchup.sh
```

`mcs_check.sh` には夜間間引き（22-06時は :00/:20/:40 を起点とする各5分窓で実行。
分の値が20で割った余り5未満なら実行）が組み込み済み — 5分 cron のまま貼ればスクリプト側が間引く。
この構成では `mcs_setup services` の既定 wrapper・cmd watcher・Hermes cron を
併用しない。既定の起動経路には `--no-notify` がなく、Hermes がある環境では
通知を送信し得る。抽出は上記の定期ジョブから実行される。

### B-5. 動作確認と `check` の読み方

```bash
$PY mcs/ingest/run_check.py --json --download-files --mark-read --no-notify
$PY mcs/views/mcs_view.py status
$PY mcs/ops/mcs_setup.py check
```

`mcs_setup.py check` はスタンドアロンでは次をエラー/警告として報告
する — **想定内**（Path A 用の実行基盤が無いだけで、収集・閲覧・
抽出は動作する）:

- error `interpreter ~/.hermes/hermes-agent/venv/bin/python is missing
  or not executable`（Path A の services が使うインタプリタ。B-4 の crontab 構成では
  使わない）
- error `hermes CLI not resolvable (...) — notifications cannot be sent`
  （配送経路が無い）
- warn `LaunchAgent local.mcs-cmd not installed` 等 4件、
  `update recovery watchdog (org.mcs.recovery) not installed`、
  `scripts not deployed to ~/.hermes/scripts`
- warn `no llama-server LaunchAgent`（B-3 を行わない場合）

このため Path B では `check` の exit 1 自体は失敗の判定に使えない。
これ以外のエラー（`mcs_login_id`・Keychain・Chrome 等）は Path A と
同じ基準で対処する。

### B-6. 後から通知を有効にする（Path A への移行）

```bash
./install.sh --preflight                        # NG が無いことを確認
./install.sh                                    # hermes-agent + plugin + services
PY=~/.hermes/hermes-agent/venv/bin/python       # 以後は install.sh が作った venv を使う
$PY mcs/ops/mcs_setup.py init                   # notify.interactive=discord 等を設定
$PY mcs/ops/mcs_setup.py services && $PY mcs/ops/mcs_setup.py check
```

移行後は B-4 の crontab 行を削除する（services が登録する hermes cron と
二重実行になる）。`--no-notify` を付けて運用していた場合は wrapper の該当フラグを除き、
outbox に残った pending は次回 flush で配送対象になる。
`notify_max_age_h` は取り込み時の抑制なので、既存の待機分は送信再開前に確認する。Discord/Slack アプリの
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
| `daily_digest.enabled` | bool | `false` | 朝の日次ダイジェスト（件数と ID のみ）を `notify_target` に送る |
| `daily_digest.hour_jst` | int(0-23) | `8` | 日次ダイジェストを送る時刻（JST。この時刻以降の最初の実行で1日1回） |
| `self_posts` | bool | `false` | 自分の投稿も取り込んで通知（latest probe 経由） |
| `deep_history` | bool | `true` | 初回に全履歴を遡って保存 |
| `discover_archived` | bool | `false` | アーカイブ済み患者も収集対象にする |
| `trickle_pages` | int(1-40) | `3` | 1回の実行で履歴を遡るページ数 |
| `job_budget_seconds` | num | 既定 | 内部処理の時間予算・秒 |
| `signals.notify` | bool | `false` | 確認候補を通知に出す |
| `signals.digest` | bool | `true`（キー未設定時。ウィザード既定は `false`） | 複数候補をダイジェストにまとめる |
| `signals.digest_interval_h` | num | 既定 | ダイジェスト間隔・時間 |
| `signals.self_organizations` | list[str] | 自動検出 | 自施設名（MCS プロフィールから自動検出を上書き） |
| `signals.self_professions` | list[str] | 自動検出 | 自職種（同上） |
| `signals.request_targets` | list[str] | なし | 依頼先として数える宛名 |
| `signals.med_exclude_names` | list[str] | なし | 薬剤判定から除外する語 |
| `local_llm.url` | str | `http://127.0.0.1:8080/v1/chat/completions` | ローカルLLMのエンドポイント（loopback http のみ — それ以外は `check` が拒否。別ポートの自前サーバを指せる） |
| `local_llm.model` | str | `Qwen3.5-9B` | モデル名（OpenAI 互換 API の `model` フィールド） |
| `semantic.mode` | choice | `off` | `off`以外は本文を外部 Jev API へ送信。`shadow`=記録のみ / `enforce`=判定に使用 |
| `semantic.project_ids` | list[int] | — | 対象プロジェクトID（mode が off 以外では必須） |
| `semantic.extract_qc` | choice | `off` | `annotate`=抽出結果への Jev 監査注記 |
| `semantic.daily_request_budget` | int | 既定 | Jev 呼出の1日上限 |
| `update.mode` | choice | `off` | `off`/`notify`/`auto` — 自己更新ポリシー |
| `update.auto_delay_h` | num | — | auto 時の適用遅延（0=検出次第即適用） |
| `update.include_prerelease` | bool | `false` | プレリリースを更新対象に含める |
| `health.max_missed_runs` | int | `2` | 欠測許容回数。昼5分・夜20分の予定と完了猶予から判定 |

## 5. 秘密情報の配置

| 場所 | 内容 | 備考 |
|---|---|---|
| Keychain `mcs-adapter` | MCS パスワード | `auto_login` がフォーム投入時に読む |
| `~/.mcs/.env` (0600) | `MCS_PASSWORD`（Keychain ロック中のフォールバック）・`TYPESAFE_API_KEY` | 平文 — FileVault/物理セキュリティ前提 |
| hermes profile `.env` | `DISCORD_BOT_TOKEN` / `SLACK_BOT_TOKEN`+`SLACK_APP_TOKEN` | `init` または `hermes config set --stdin` で書込み（argv に載せない） |

## 6. スケジュール構成（Path A 導入後）

収集ジョブは **hermes cron 6件 + launchd 5件** のハイブリッド
（services 所有: cron 6件と launchd 4件 `local.mcs-cmd`・`local.mcs-int`・
`ai.mcs.extract-drainer`・`ai.mcs.extract-drainer-rt`。install.sh 所有:
復旧 watchdog `org.mcs.recovery`。別に LLM サーバ常駐の
`ai.mcs.llamaserver` / `ai.hermes.llamacpp`）。

ジョブ・スケジュール・実行スクリプトの一覧表は
[deployment/launchagents/README.md](../deployment/launchagents/README.md)
に一本化している。正本はコードの `mcs/ops/mcs_setup.py` の `CRON_JOBS`
（hermes cron）と `AGENT_LABELS`（launchd）で、`services` はこれを
登録する。22-06時の未読チェック間引き（:00/:20/:40 起点の各5分窓 —
30分のセッション失効上限を下回るため）は `mcs_check.sh` 内で行う。

## 7. トラブルシューティング

まず `./install.sh --preflight`（導入前・導入途中）または
`~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py doctor`
（導入後）を実行し、出力されたメッセージを下の表で引く。どちらも
直し方を `fix:` 行に出すので、基本はそれを上から順に実行して再実行する。
表中の `$PY` は `~/.hermes/hermes-agent/venv/bin/python`。

### 7-1. `install.sh --preflight` / `install.sh` のメッセージ

| メッセージ | 原因 | 対処 |
|---|---|---|
| `NG running as root` / `error: do not run install.sh as root or with sudo` | `sudo` や root で実行した | 普段のユーザーで `sudo` なしに再実行 |
| `NG macOS X is too old` | macOS 13 未満 | macOS をアップデート |
| `NG Xcode Command Line Tools missing` / `NG git missing` | 開発ツール未導入 | `xcode-select --install` |
| `NG Homebrew not installed` / `error: brew not found` | Homebrew 未導入 | `fix:` 行の Homebrew 公式インストールコマンドを実行（brew を使わないなら `--no-brew`） |
| `NG no Python 3.11–3.13 on PATH` | `--no-brew` 等で brew が Python を入れられない | `brew install python@3.13`（brew があれば stage 1 が入れるので NG にならない） |
| `NG disk: only N GB free` | 空き容量不足（モデル込みで約 12 GB） | 空きを作って再実行 |
| `NG network: cannot reach github.com` / `huggingface.co` | clone・モデル DL に必要な通信ができない | ネットワーク・プロキシを確認し `curl -I <URL>` で疎通確認 |
| `NG port 8080 is taken by a process that is not an LLM server` | 別のプロセスが :8080 を使用 | `lsof -nP -iTCP:8080 -sTCP:LISTEN` で特定して停止、または自前 LLM なら `--no-llm` |
| `NG existing install points at another checkout: <パス>` / `error: this machine is installed from another checkout` | 別の checkout から導入済み | 旧 checkout の `install.sh` を使うか、切り替えるなら `./install.sh --force-repo`（§A-7） |
| `NG custom HERMES_HOME (...) is not supported by the services stage` | `~/.hermes` 以外を引数に指定 | 既定の `~/.hermes` を使うか `--no-services` を付ける |
| `NG <HERMES_HOME>/hermes-agent exists but is not a git checkout` / `cannot read HEAD of ...` | hermes-agent ディレクトリが壊れている | `fix:` 行の `mv ... .bak` で退避して再実行（stage 2 が clone し直す） |
| `WARN hermes-agent checkout is at X, not the validated pin` | 既存 checkout が検証済み commit と違う（保持される） | そのままでも続行可。揃えるなら `fix:` 行の `git ... checkout --detach <pin>` |
| `WARN running from a git worktree` | worktree から実行している | main checkout から実行する |
| `WARN Google Chrome missing` | Chrome 未導入（brew があれば stage 1 が入れる） | `brew install --cask google-chrome` |
| `unknown option: ...`（exit 2） | 存在しないオプション | `./install.sh -h` で確認 |
| `error: installation stopped; repair the failed stage and re-run install.sh` | 直前の `error:` 行のステージで停止した | その `error:` の指示に従って直し、`./install.sh` を再実行（完了分は skip） |
| `error: model download failed — re-run install.sh to resume` | モデル DL が途中で失敗 | そのまま `./install.sh` を再実行（`.part` から再開） |
| `error: model checksum mismatch` | `MCS_MODEL_SHA256` と DL 結果が不一致 | 値を確認して再実行（`.part` は削除済みで最初から DL） |
| `error: installing hermes-agent into ... failed` | pip install の失敗（通信・コンパイラ） | 上に出たエラーを直して再実行（pip install をやり直す） |
| `error: llama-server not found` | llama.cpp 未導入 | `brew install llama.cpp`、または `--no-llm` |
| `error: plugins enable failed` | hermes CLI・profile 設定の問題 | hermes の設定を直すか profile の `config.yaml` の `plugins.enabled` に `mcs-discord-commands` を追加して再実行 |
| `error: services reported problems` | stage 5 の `mcs_setup.py services` が失敗 | 直前の services の出力を確認、`$PY mcs/ops/mcs_setup.py doctor` で診断して再実行 |
| `warn: ~/.local/bin is not on PATH` | shell から `hermes` が見えない | シェルの profile で `~/.local/bin` を PATH に追加（定期ジョブは独自の PATH を使うので影響しない） |
| `warn: ai.mcs.llamaserver plist changed but the running server was kept` | plist 更新時にサーバが応答中だった | 処理が空いた時に表示された `launchctl bootout ...; launchctl bootstrap ...` を実行 |
| `warn: hermes-managed .../ai.hermes.llamacpp.plist exists but is NOT loaded` | hermes 管理の LLM agent が止まっている | 表示された `launchctl bootstrap ...` を実行 |

### 7-2. `mcs_setup.py check` / `doctor` のメッセージ

| メッセージ | 原因・対処 |
|---|---|
| `mcs_setup requires Python >= 3.10` | 素の `python3`（3.9 系）で実行した — `$PY` で実行する |
| `interpreter ~/.hermes/hermes-agent/venv/bin/python is missing or not executable` | 定期ジョブが起動できない — `./install.sh` を再実行（stage 2 が venv を作り直す）→ `$PY mcs/ops/mcs_setup.py services`。`services` もこの状態では何も描画せず止まる（Path B では想定内） |
| `hermes resolves here (...) but not on the launchd PATH` | 手元の shell では見えるが定期ジョブから見えない — 表示の `ln -s <hermes> ~/.local/bin/hermes`、または `init --set hermes_bin='"<パス>"'` |
| `config.json is unreadable or invalid` / init の `config: ... nothing written` | `~/.mcs/config.json` が壊れている — 手で直すか `$PY mcs/ops/mcs_setup.py init --yes` で `config.json.corrupt-<日時>` へ退避して作り直す |
| `missing required key: mcs_login_id` / `notify_target` | `init` で再登録（両方とも必須） |
| `Keychain entry 'mcs-adapter' not found` | パスワード未登録 — `init` で登録（`.env` `MCS_PASSWORD` があれば警告に格下げ） |
| `Keychain entry ... unreadable` | login keychain がロック中 — `security unlock-keychain` か GUI ログイン。再起動後も収集が必要な場合は `init` の `.env` フォールバック設定を確認 |
| `Chrome binary missing` | Chrome が `/Applications` に無い — `brew install --cask google-chrome` |
| `local LLM endpoint not reachable (http://127.0.0.1:8080/v1/models)` | llama-server 未起動 — Path A-1/§B-3。収集自体は動く（警告） |
| `llama-server advertises N slots` | `-np` が選択スロット数（2）未満 — plist の `-np 2` を確認 |
| `LaunchAgent ai.hermes.llamacpp`（または `ai.mcs.llamaserver`）`installed but not loaded — the local LLM is down` | LLM サーバが止まっている — 表示の `launchctl bootstrap gui/<uid> <plist>` |
| `no llama-server LaunchAgent (...)`（警告） | 自前サーバを `local_llm.url` で使うなら問題なし。そうでなければ `./install.sh`（stage 4） |
| `update recovery watchdog (org.mcs.recovery) not installed`（警告） | `./install.sh`（stage 6。`--no-recovery` で導入しなかった場合は想定内） |
| `~/.mcs-recovery/mcs_recover.py differs from the repo copy`（警告） | 復旧ツールが古い — `./install.sh` を再実行（旧版は `.prev` に残る） |
| `recovery watchdog recovers <パス>, not this checkout <パス>`（警告） | checkout を移動した・別 checkout から実行している — 正しい checkout で `./install.sh`（§A-7） |
| `LaunchAgent org.mcs.recovery installed but not loaded` | 表示の `launchctl bootstrap gui/<uid> ~/Library/LaunchAgents/org.mcs.recovery.plist` |
| `LaunchAgent <label> installed but not loaded` | MCS の agent が止まっている — `$PY mcs/ops/mcs_setup.py services` |
| `LaunchAgent <label> not installed`（警告） | `services` 未実行 — `$PY mcs/ops/mcs_setup.py services` |
| `deployed scripts differ from the repo in ~/.hermes/scripts` | 更新後に services を再実行していない — `$PY mcs/ops/mcs_setup.py services` |
| `scripts not deployed to ~/.hermes/scripts`（警告） | `services` 未実行 — `$PY mcs/ops/mcs_setup.py services` |
| `launchd gui/<uid> unreachable from this session`（警告） | ssh 等 GUI セッション外で実行している — ログイン中の端末で再実行して確認 |
| `hermes CLI not resolvable` | hermes 未導入（Path B では想定内。Path A なら `install.sh` 再実行か `hermes_bin` 設定） |
| `hermes gateway is not supervised` | `services` を実行（`hermes gateway install`+`start` で常駐化） |
| `hermes_plugin/ is newer than the running gateway`（警告） | `hermes gateway restart`（§A-6） |
| `TYPESAFE_API_KEY is not resolvable` | semantic 有効時に必須 — `~/.mcs/.env` に登録 |

### 7-3. その他の症状

| 症状 | 原因・対処 |
|---|---|
| `session_expired` 通知が来る | セッション失効 — tick 内で `auto_login` が `_recover_session`→フォーム投入をその場で試行。失敗時のみこの通知が来る（detail の `auto_login=<state>` を確認。`manual_required`/`keychain_locked` は上記 Keychain 節）。成功時は代わりに `session_recovered` 通知が来て run は継続する |
| カードだけ届きスレッド本文が無い | gateway が旧 plugin を保持 — `hermes gateway restart`（§A-6） |

### セッション失効・Keychain 状態の読み方

README「導入方法」から移設（文言は同じ。段落を項目に分けた）。

- セッション切れは tick 内の失敗点で `auto_login` が1回試行され、成功すれば
  その run のまま再開する。結果は通知に出る — 成功なら `session_recovered`
  (`run N: <stage>: ...`)、失敗なら従来どおり `session_expired` で detail に
  `auto_login=<state>` が付く。run log の `relogin_attempts` に試行記録が残る。
- `keychain_locked` は「エントリはあるが login
  keychain がロック中で読めない」状態 — `security unlock-keychain` または
  GUI ログインで解除してから次回 run を待てばよい(エントリ再登録は不要)。
- `~/.mcs/.env` の `MCS_PASSWORD` はリブート直後のロック中にも効く
  フォールバック(`mcs_setup init` が Keychain と併記する; 平文のため
  FileVault/物理セキュリティ前提)。頻発する場合はログイン状態と Keychain の
  読み取り可否を確認し、自動ロックを収集失敗の回避策として無条件に解除しない。
- `manual_required` はエントリ未登録かつ .env 未設定、またはフォーム非検出
  — `mcs_setup init` で再登録する。

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
SLACK_BOT_TOKEN=xoxb-…
SLACK_APP_TOKEN=xapp-…
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
