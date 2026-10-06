# インストールガイド

このガイドはv1.0.13仕様のinstall/update/setup/doctorと復旧手順に対応する。
実機受入・公開の状況は[開発・受入計画](../development/plans/RELEASE_1.0.13.md)で追跡する。

### 2026-10-04追記: 現在のローカル入口

[共通CLI](../../mcs/ops/mcs_cli.py)は実装済みです。初回は
`sh scripts/mcs install`、導入後は`mcs install / update / setup / doctor`を利用できます。
`install.sh`が`~/.local/bin/mcs`を配置するため、同ディレクトリをPATHへ追加します。
`mcs setup`は既存の`init`へ委譲し、既存設定・非表示の秘密入力を維持します。
下記の`install.sh`、`mcs_setup.py init/check/doctor`、更新bootstrapは引き続き利用できます。

`mcs doctor --json`は既定で通信・秘密取得・患者DBの参照をせず、
未検査と正常を区別します。選択runtime・配備recoveryのPython/SQLiteとSDK配布metadataの
表示は[診断実装](../../mcs/ops/mcs_setup.py)に従い、SDKの実接続互換を証明しません。
`--probe llm`や`--probe services`は明示した対象だけを追加確認します。
launcherの選択runtime追随、backup lifecycle、緊急度の後追い通知は統合検証中です。
この追記は1.0.13の公開・実機配備・本番有効化の完了を意味しません。

> **AI エージェントにセットアップさせる場合:** 対話実行用の手順書は
> [SETUP_AGENT.md](SETUP_AGENT.md) にあります — その文書を
> エージェントに読み込ませれば、前提確認→形態選択→設定投入→検証
> までを対話的に実行します。

**LINE WORKS を使う場合:** 独自接続アダプターの
[導入・接続手順](LINEWORKS.md)へ進んでください。本体の依存導入後に
`mcs_setup.py init` と `python -m lineworks_adapter init/check/run` で設定します。
LINE WORKS 通知には Hermes の通知 plugin・gateway は不要です。

## 最短手順（Path A・初めての方向け）

**事前に用意するもの**（Python は不要 — `install.sh` が導入する）:

- macOS 13 以降の Mac（Apple silicon 推奨）に、普段使いのユーザーで
  ログインしていること（`sudo` や root では実行しない）
- Xcode Command Line Tools（`xcode-select --install`）と
  [Homebrew](https://brew.sh/)
- 空きディスク約 12 GB（LLM モデル 5.7 GB を含む）と、github.com・
  huggingface.co へ接続できるネットワーク
- MCS のログイン ID とパスワード
- 通知を使う場合: Slack app（推奨・作り方は付録B）、Discord bot（付録A）、または[LINE WORKS Bot](LINEWORKS.md)

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
$PY mcs/ops/mcs_setup.py init       # 4. 本体設定・最終チェック。Slack/Discordではplugin・gatewayも同期
$PY mcs/ops/mcs_setup.py doctor     #    （困ったとき）範囲別の件数だけの読取り診断（check の代わりではない）
```

`init` が `check: OK` で終われば初回設定は完了です。`--no-services` で
導入した場合や、更新後に配置 drift が出た場合だけ、`services` → `check`
を実行します。LINE WORKSでは[接続ガイド](LINEWORKS.md)で認証・Callback・独立常駐を設定し、再診断してください。
収集開始前に Chrome を MCS にログインしたプロファイルで
CDP ポート `:9333` 付きで起動してください（§A-5）。

**成功の目安**:

- `--preflight` の最終行が `preflight: 0 blocker(s), N warning(s)`（exit 0）
- `install.sh` が `Installed. Summary:` と 6 ステージ分の結果を表示して終わる
  （途中で止まった場合は `error: installation stopped; ...` が出る — §7-1）
- `check` の最終行が `check: OK (0 errors, N warnings)`（exit 0）。
  `warn` 行は動作を止めないが、内容は一度確認する
- `check` が OK であれば `local.mcs-cmd`・`local.mcs-int`・
  `ai.mcs.extract-drainer`・`ai.mcs.extract-drainer-2`・`org.mcs.recovery`
  と、llama-server（`ai.hermes.llamacpp` か `ai.mcs.llamaserver` の
  どちらか）のロード状態も確認済み（未ロードは `check` の error になる）。
  `doctor` は launchd の状態を表示しない

メッセージ別の対処は §7 トラブルシューティング。詳細な仕組みは以下の各節。

## 概要

hermes-mcs の新規導入手順。導入形態は次の2つ:

- **Path A — hermes-agent アドオン**（推奨・全機能）: 収集→SQLite→
  Discord/Slack 通知＋対話カード。`./install.sh` が依存一式を導入する
- **Path B — スタンドアローン**（Hermesなし・全MCS機能）: 収集・保存・閲覧・
  抽出・Slack/Discord/LINE WORKS通知と操作・定期実行・監視・更新を独立して実行する。
  `./install.sh --mode standalone`で導入する。[専用手順](STANDALONE.md)を参照

## 0. 導入形態の選択

B は `install.sh --mode standalone`（[STANDALONE.md](STANDALONE.md)）の場合。
§3 の手動最小構成では Slack/Discord の配送・カードは使えない。

| 機能 | A: hermes アドオン | B: スタンドアロン |
|---|---|---|
| 未読収集 → SQLite → `mcs_view` 閲覧 | ✓ | ✓ |
| 全履歴アーカイブ・FTS5 検索 | ✓ | ✓ |
| 構造化抽出（ルール + ローカルLLM） | ✓ | ✓ |
| アラートシグナル（検出） | ✓ | ✓ |
| Discord/Slack への通知配送 | ✓ — Hermes公式接続 | ✓ — 独立公式SDK接続 |
| Slack/Discordの対話カード・`/mcs` | ✓ | ✓ |
| LINE WORKSへの通知・本人1:1での操作 | ✓ — 独立接続を設定 | ✓ — 独立接続を設定 |
| semantic v4（shadow/enforce） | ✓ | ✓ |
| 定期実行の仕組み | hermes cron + launchd | 独立hostをnative supervisorで常駐 |
| `mcs_setup.py check` | 全項目検証 | 独立モードの全必須条件を検証 |

## 1. 共通の前提条件

| 項目 | 要件 |
|---|---|
| OS | macOS 13 以降（Keychain・launchd を使用。他 OS は収集本体のみ手動で動くが未検証） |
| 実行ユーザー | 普段使いのユーザー。`install.sh` は root / `sudo` 実行を拒否する |
| Xcode Command Line Tools | `xcode-select --install`（git と Homebrew が必要とする） |
| Homebrew | `brew` が使えること（Path A で `--no-brew` を使わない場合） |
| Python | 事前準備は不要 — Path A は `install.sh` が `python@3.13` を導入し、hermes-agent の venv（3.11–3.13、`<3.14`）を作る。`mcs_setup.py` 等は Python ≥3.10 が必要で、macOS 標準の `/usr/bin/python3`（3.9 系）では動かない |
| SQLite | 定期ジョブ・復旧 watchdog を実際に動かす Python がリンクする SQLite が WAL-reset 修正済みであること: 3.51.3 以降、または 3.50 系は 3.50.7 以降・3.44 系は 3.44.6 以降。`sqlite3` コマンドの版ではなく、その Python の `sqlite3.sqlite_version` で判定する。macOS 標準 `/usr/bin/python3` の SQLite（3.51.0 など）は対象外のため、復旧 watchdog には `--recovery-python` で安全な独立 Python を指定するか `--no-recovery` を使う。影響版・版不明は `services`・`update`・installer stage 6 が変更前に停止する |
| Google Chrome | MCS にログインしたプロファイルで CDP ポート `:9333` を使う |
| ディスク | 約 12 GB（LLM モデル約 5.7 GB + DB・添付・ログ。`--preflight` が確認する） |
| MCS アカウント | ログイン ID とパスワード（自動再ログイン `auto_login` で使用） |
| TYPESAFE_API_KEY | semantic/Jev 連携を使う場合のみ（`~/.mcs/.env` に保存） |
| 通知先アプリ | Slack app・Discord botはPath Aで設定（付録A/B）。LINE WORKS Botは両形態で[独立接続](LINEWORKS.md)を設定 |

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
| `--mode hermes\|standalone` | 実行方式。省略時は既存 `config.json` の `runtime_mode` を引き継ぎ、それも無い初回の対話実行では尋ねる（Enter=hermes）。非対話は hermes。standalone では位置引数 `HERMES_HOME` は指定不可（[STANDALONE.md](STANDALONE.md)） |
| `--recovery-python PATH` | 復旧 watchdog（stage 6）を実行する既存の独立 Python の絶対パス。何かを書き込む前に Python ≥3.9 と SQLite の WAL-reset 修正を検証し、更新されるチェックアウト内の実行ファイルは拒否する。私有 `config.json` の `recovery_python` に保存される（後から `mcs_setup.py init --yes --recovery-python <絶対パス>` でも変更可）。未指定時は `/usr/bin/python3`。代替 Python の自動選択・導入はしない |
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
| 4 | llama-server を `ai.mcs.llamaserver` LaunchAgent で常駐化（`127.0.0.1:8080`・`-np 3`・Qwen3.5-9B GGUF 約 5.7 GB を `~/.hermes/models/` へ DL）。hermes 管理の `ai.hermes.llamacpp` の plist があれば導入せず、未ロードで `:8080` も無応答なら bootstrap コマンドを警告で案内。plist 内容が変わった場合、サーバが応答中なら再読込せず適用コマンドを警告で案内（処理中の要求を切らない）、無応答なら再読込 | モデル DL は `.part` から続きを再開（`curl -C -`）。`MCS_MODEL_SHA256` を設定すると DL 後に sha256 を照合し、不一致なら `.part` を消して停止。`llama-server` 不在・DL 失敗・bootstrap 失敗で停止 |
| 5 | `~/.mcs/data{,/cmd,/cmd_int}` を作成し、`mcs_setup.py services` — launchd agent 4件 + hermes cron 6件の配置・登録（§6 参照） | services が問題を報告したら停止 |
| 6 | 復旧 watchdog `org.mcs.recovery` を独立系統で導入（`~/.mcs-recovery/mcs_recover.py`、旧版は `.prev`。復旧対象の checkout を `~/.mcs-recovery/repo_path` に記録。15分間隔で中断した update を復旧）。内容・記録が変わった時か未ロード時だけ再読込 | macOS では配備前に復旧用 Python（既定 `/usr/bin/python3`、または `--recovery-python`）の Python ≥3.9 と SQLite の WAL-reset 修正を検査し、影響版・版不明なら tool・plist を配備せず停止（自動で別 Python へ切り替えない）。bootstrap 失敗でも停止 |

最後に `Installed. Summary:` として各ステージの結果と、次に実行する
コマンド（venv インタプリタのフルパス付きの `init`・非対話版の例・
復旧用の `services`・`check`）が表示される。手動で残るのは `mcs_setup.py init`
（秘密情報と選択が必要）だけ。

### A-2. 通知先（Slack / Discord）側の準備

通知先は **Slack を推奨**します（カード操作がコンパクトで、導入も
`hermes slack` コマンド一発。Discord も同じカード形式で使えます）。

収集する値:

| 用途 | 値 | 入手先 |
|---|---|---|
| Slack | `SLACK_BOT_TOKEN`（`xoxb-`） | api.slack.com → Install App |
| Slack | `SLACK_APP_TOKEN`（`xapp-`） | 同上 → Socket Mode で `connections:write` 付き生成 |
| Slack | `team_id`（ワークスペースID） | Slack 管理画面や API |
| Slack | `application_id`（api_app_id） | api.slack.com → Basic Information |
| Slack | `channel_id` | チャンネル名 → チャンネル詳細 → 最下部の ID |
| Slack | member ID（許可ユーザー） | プロフィール → ⋮ → Copy member ID |
| Discord | `application_id` | Developer Portal → General Information |
| Discord | `guild_id`（サーバーID） | Discord 開発者モード → サーバー名右クリック |
| Discord | `channel_id`（カード投稿先） | 同上 → チャンネル右クリック |
| Discord | `DISCORD_BOT_TOKEN` | Developer Portal → Bot → Reset Token（**一度しか表示されない**） |
| Discord | 自分の user ID | 開発者モード → 自分の名前右クリック → Copy User ID |

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
- `notify.interactive=slack`/`discord` を選んだ場合、hermes 側へも
  書き込む（Slack は `slack_*` settings キーにマップされる）:
  - プラグイン settings を serving profile の config.yaml へ
    （`hermes -p <profile> config set` 経由 — snapshot/inbox/
    allowlists/scope 一式）
  - `SLACK_BOT_TOKEN`+`SLACK_APP_TOKEN`（Discord は
    `DISCORD_BOT_TOKEN`）を同 profile の `.env` へ（stdin 経由、
    argv には載せない。既設定済みのトークンは残る）

非対話でも実行できる（CI・再現用）:

```bash
# secrets は対話入力か安全な端末で環境変数に準備し、履歴に平文を残さない
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py init --yes \
    --login-id <ID> --notify-target slack:<チャンネルID> \
    --set 'notify.interactive="slack"' \
    --set 'notify.slack={"profile":"P","application_id":"A","team_id":"T","channel_id":"C"}' \
    --plugin-profile <serving profile> \
    --plugin-user-ids <uid> \
    --plugin-project-ids <pid>
```

`--set` の値は JSON（文字列は内側の引用符が必要）。Discord の場合は
`notify.interactive="discord"` +
`notify.discord={"profile","application_id","guild_id","channel_id"}`・
`--notify-target discord:<チャンネルID>`・`--plugin-chat-ids`、
トークンは `DISCORD_BOT_TOKEN`。例は `init --help` の末尾にも表示される。

- 既存の `~/.mcs/config.json` が壊れている（JSON として読めない・
  オブジェクトでない）場合、`init` は何も書かずに停止する（exit 1）。
  手で直すか、`init --yes` で `config.json.corrupt-<日時>`（0600・
  中身はそのまま）へ退避して既定値から作り直す
- `init` は通知プラグイン設定の後、対話通知を使うときは gateway を同期し、
  `check` を自動実行する。設定書込み・同期・チェックの失敗は成功扱いにしない

設定キー全一覧は §4 を参照。個別キーは `--set KEY=JSON`（例: `--set self_posts=true`）、Jev 連携は `--semantic-mode`（`off`/`shadow`/`enforce`）で指定できる。

### A-4. サービスの再同期と再検証（更新・復旧時）

過去版からの更新（データ移行・install.sh再実行を含む）は、AIエージェントに
[更新実行手順書](UPGRADE_AGENT.md)を読ませて実行できます。

```bash
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py services   # launchd + hermes cron + gateway
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py check      # 必須条件の検証（exit 1 で失敗）
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py doctor     # 範囲別の件数だけの読取り診断（不具合報告用）
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
- `doctor` — 既定ではローカルの読取りだけで、configuration・
  interpreter・runtime・recovery_runtime などの範囲ごとに状態と
  error/warning の件数だけを表示する（パス・値・メッセージは出さない）。
  `check` は実行しない。`llm`・`services`・`credentials`・`data` は既定で
  `not_checked` で、`not_checked` は正常を意味しない。`--probe llm` /
  `--probe services` で明示した範囲だけ追加確認する。終了コードは
  いずれかの範囲が `blocked` のときだけ 1。drift・`hermes` の解決・
  Keychain・launchd agent のロード状態を含む blocker 一覧は `check` で確認する

### A-5. 動作確認

Chrome を MCS にログインしたプロファイルで CDP ポート `:9333` 付きで
起動し、`curl -sf -m 3 http://127.0.0.1:9333/json/version` が JSON を返す
ことを確認します。以下は実データの取得・通知・既読化を伴うので、実行する
範囲を決めてから進めます。

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
  リンクボタン（`MCSで開く`）や人名表示（`parts.mentions`）を含む
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

## 3. Path B — スタンドアローン（Hermesなし・全MCS機能）

収集・保存・検索・抽出に加え、Slack / Discord / LINE WORKSの通知、
対話カード、人承認操作、定期実行、監視、更新・復旧を独立して動かせます。
Hermesが導入済みでも`runtime_mode=standalone`を明示すれば独立モードを使えます。
未指定の既存設定は従来のHermesモードを維持します。

### B-1. 導入・設定

同じcheckoutのルートで実行します。macOSの共通前提条件は§1を参照してください。

```bash
./install.sh --mode standalone --preflight
./install.sh --mode standalone
PY="$HOME/.mcs/venv/bin/python3"
$PY mcs/ops/mcs_setup.py init --runtime-mode standalone
$PY mcs/ops/mcs_setup.py check
$PY -m mcs_standalone status
```

installerは独立venvと公式接続SDK、ローカルLLM、常駐プロセスと復旧watchdogを
用意します。ウィザードがMCS設定・配信先・許可ユーザー・プロジェクト範囲と
認証情報を設定します。初回設定中は接続が起動できず再試行する場合があります。
設定後の`check`はHermes未導入をエラー扱いせず、実際の必須条件を検証します。
`check: FAIL`を想定内として無視しないでください。

### B-2. 接続・常駐・更新

[独立モードの接続・設定・移行手順](STANDALONE.md)を使用します。
SlackはBot/App token、DiscordはBot tokenをowner-onlyの専用ファイルへ保存します。
LINE WORKSの秘密情報・HTTPS Callbackは[LINE WORKS手順](LINEWORKS.md)に従います。

独立hostが既存と同じ6定期ジョブ、cmd/cmd_int取込、抽出worker2本を所有します。
Hermes cron、手動crontab、旧抽出LaunchAgentを同時に登録しないでください。
既存モードからの切替では`services`が旧manifest所有のMCSジョブだけを停止します。
手動登録のジョブや別プロセスのLINE WORKSアダプターは、対象を確認して別途停止します。
共有Hermes gatewayや他用途のジョブは停止しません。

独立モードの更新後はhost自身が処理終了・ロック解放後に新コードで再起動します。
独立モード非対応の旧タグへは戻せません。必要なら先にHermesモードへ移行します。

## 4. config.json 設定リファレンス

`~/.mcs/config.json`（0600）。`init` のウィザードが全項目を案内する
（`--set KEY=JSON` で非対話設定も可）。必須は `mcs_login_id` と
`notify_target` の2つのみ。

| キー | 型 | 既定 | 説明 |
|---|---|---|---|
| `runtime_mode` | choice | `hermes` | `hermes` / `standalone`。未指定は既存Hermes連携を維持 |
| `mcs_login_id` | str | — （必須） | MCS のログインID |
| `notify_target` | str | — （必須） | 通知の送り先。`discord:<チャンネルID>`・`slack:#ch` 等 `hermes send --to` 形式、又は独自接続の `lineworks:<トークルームID>`。通知を送らない運用では便宜値 |
| `notify.interactive` | choice | `off` | `discord`/`slack`/`lineworks`=対話カード / `off`=テキストのみ |
| `notify.discord.profile` | str | — | 配送に使う hermes プロファイル（interactive=discord 時必須） |
| `notify.discord.application_id` | str | — | Discord アプリケーションID（同上） |
| `notify.discord.guild_id` | str | — | Discord サーバーID（同上） |
| `notify.discord.channel_id` | str | — | カードの投稿先チャンネルID（同上） |
| `notify.slack.profile/application_id/team_id/channel_id` | str | — | Slack 版の配送スコープ（interactive=slack 時必須。guild_id 不可） |
| `notify.lineworks` | object | — | 独自アダプターの Bot・ドメイン・部屋・許可ユーザー・プロジェクト範囲。[設定手順](LINEWORKS.md) |
| `notify.operator` | str | なし | 運用者の Discord ユーザーID |
| `notify.card_thread` | bool | `true`（ウィザード既定。キー未設定のconfigではオフ） | 患者スレッドごとにカードのコンパニオンスレッドを立て本文・添付を配送 |
| `notify.card_thread_archive_min` | int | `10080` | 設定キーのみ（自動アーカイブは未実装） |
| `notify.route_epoch` | int | `1` | 配送先を変えたとき +1 する番号 |
| `notify_bot_profile` | str | なし | 通知投稿に使う hermes プロファイル（`[a-z0-9_-]+`） |
| `notify_system_target` | str | なし | 障害・システム通知の送り先（空欄=`notify_target` と同じ） |
| `notify_max_age_h` | num | なし | この時間より古い投稿は通知しない。設定時は期間内の新規取込みを既読でも通知（既読投稿は投稿時刻が必要） |
| `notify_all_replies` | bool | false | リアルタイム通知経路で新規取得した返信を既読・投稿時刻によらず全件通知。返信には `notify_max_age_h` を適用しない。履歴一括取込み・保存済み返信の再送は対象外 |
| `hermes_bin` | str | 自動検出 | `hermes` コマンドのパス |
| `daily_digest.enabled` | bool | `false` | 朝の日次ダイジェスト（件数と ID のみ）を `notify_target` に送る |
| `daily_digest.hour_jst` | int(0-23) | `8` | 日次ダイジェストを送る時刻（JST。この時刻以降の最初の実行で1日1回） |
| `daily_digest.include_names` | bool | `false` | 一覧の project ID に患者名を添える（送信先は `notify_target`） |
| `self_posts` | bool | `false` | 自分の投稿も取り込んで通知（latest probe 経由） |
| `metadata_shadow` | bool | `false` | 監視対象のスタンプを保存のみ再取得。未読保持の実証と監視集合・予算の運用合意後に限り有効化する。shadowの反応値はカード・digest・CLIの反応表示に出さず、CLIでは取得状態・日時・理由のみ参照できる |
| `metadata_refresh_publish` | bool | `false` | `metadata_shadow`の再取得が成功した値をカード・digest・CLIの反応表示へ反映する。失敗時と保存済みのshadow値は反映しない。未読保持の実証と`mcs/views/metadata_report.py`での照合後に限り有効化する |
| `metadata_actors` | bool | `false` | `metadata_shadow=true`も必要。直近7日に動きのあったスレッドの全投稿（先頭・返信）について、スタンプを押した人の氏名・所属・職種を取得し、Slack・DiscordのスレッドとLINE WORKSの原文投稿に氏名・職種を表示する（アイコンは保存しない）。最後の観測状態は無期限に保持し、取消観測時刻を残す（再押下時は解除、全操作履歴は復元しない）（#22-D2・D3、2026-10-03）。shadowと同じtickの予算内で最大4件。氏名の保存・通知先の閲覧範囲と未読保持を確認して有効化する |
| `deep_history` | bool | `true` | 初回に全履歴を遡って保存 |
| `discover_archived` | bool | `false` | アーカイブ済み患者も収集対象にする |
| `trickle_pages` | int(1-40) | `3` | 1回の実行で履歴を遡るページ数 |
| `job_budget_seconds` | num | 既定 | 内部処理の時間予算・秒 |
| `signals.notify` | bool | `false` | アラートを通知に出す |
| `signals.digest` | bool | `true`（キー未設定時。ウィザード既定は `false`） | 複数候補をダイジェストにまとめる |
| `signals.digest_interval_h` | num | 既定 | ダイジェスト間隔・時間 |
| `signals.self_reaction_response` | bool | `false` | 依頼投稿そのものに本人の承知・完了スタンプが観測されたら、薬剤師宛依頼の未応答候補から外す。見ました・他者のスタンプ・未取得は数えない。切替え時に既存候補のresolve・再openが起き得る（#22-D5） |
| `signals.self_organizations` | list[str] | 自動検出 | 自施設名（MCS プロフィールから自動検出を上書き） |
| `signals.self_professions` | list[str] | 自動検出 | 自職種（同上） |
| `signals.request_targets` | list[str] | なし | 依頼先として数える宛名 |
| `signals.med_exclude_names` | list[str] | なし | 薬剤判定から除外する語 |
| `urgency_escalation.mode` | choice | `off` | `off`/`shadow`/`on`。現在のLLM抽出で緊急度highの投稿の再確認候補（E1/E2）。`shadow`は送信せず監査のみ、`on`は`notify_target`へ通知。有効化は確認者・通知先の運用合意後 |
| `urgency_escalation.room_cooldown_min` | num(>0) | — （mode が off 以外では必須） | 同じ部屋への再確認通知の冷却時間・分。未指定・不正値では機能が無効のままとなり、`check` がエラーを出す |
| `urgency_escalation.after_min` / `repeat_min` | num(>0) | `30` / `60` | 初回表示から再確認までの分 / 再通知の間隔・分 |
| `urgency_escalation.max_repeats` / `max_per_day` | int(>=0) | `2` / `10` | 1投稿あたりの再通知回数 / 1日の上限 |
| `urgency_escalation.source` | choice | `llm` | 判定元。`llm`のみ対応 |
| `vital_urgency.mode` | choice | `off` | `off`/`flag`/`high`。バイタル数値の決定論的閾値判定。`flag` は閾値超過の測定値を確認用に記録・表示するのみ、`high` は緊急度を高にし閾値根拠を urgency_evidence にする。バイタル欄は測定対象・時制を保持しないため、本人以外の測定・条件節・過去報告・測定不能の文脈は近傍テキストのヒューリスティックで除外する（完全ではない）。有効化・閾値は臨床責任者の承認が前提 |
| `vital_urgency.thresholds` | object | SpO2≤90 / SBP≤90 / SBP≥180 / BS≤70 | 閾値の上書き（`spo2_lte` / `sbp_lte` / `sbp_gte` / `bs_lte`） |
| `local_llm.url` | str | `http://127.0.0.1:8080/v1/chat/completions` | ローカルLLMのエンドポイント（loopback http のみ — それ以外は `check` が拒否。別ポートの自前サーバを指せる） |
| `local_llm.model` | str | `Qwen3.5-9B` | モデル名（OpenAI 互換 API の `model` フィールド） |
| `semantic.mode` | choice | `off` | `off`以外は本文を外部 Jev API へ送信。`shadow`=記録のみ / `enforce`=判定に使用 |
| `semantic.project_ids` | list[int] | — | 対象プロジェクトID（mode が off 以外では必須） |
| `semantic.extract_qc` | choice | `off` | `annotate`=抽出結果への Jev 監査注記 |
| `semantic.daily_request_budget` | int | 既定 | Jev 呼出の1日上限 |
| `update.mode` | choice | `off` | `off`/`notify`/`auto` — 自己更新ポリシー |
| `update.auto_delay_h` | num | — | auto 時の適用遅延（0=検出次第即適用） |
| `update.include_prerelease` | bool | `false` | プレリリースを更新対象に含める |
| `health.max_missed_runs` | int | `2` | 欠測許容回数。10分間隔の予定実行と完了猶予から判定 |

## 5. 秘密情報の配置

| 場所 | 内容 | 備考 |
|---|---|---|
| Keychain `mcs-adapter` | MCS パスワード | `auto_login` がフォーム投入時に読む |
| `~/.mcs/.env` (0600) | `MCS_PASSWORD`（Keychain ロック中のフォールバック）・`TYPESAFE_API_KEY`。`runtime_mode=standalone` の互換トークン入力。`init`は私有JSONへ取り込み、run/sendはJSONを優先する | 平文 — FileVault/物理セキュリティ前提 |
| `~/.mcs/data/{slack,discord}-credentials.json` (0600) | 独立Slack/DiscordのBot/App token。`mcs_standalone init`が非表示入力または既存の私有 `.env`から登録 | run/sendが優先して検証。秘密値の環境自動取得なし |
| hermes profile `.env`（Path A のみ） | `DISCORD_BOT_TOKEN` / `SLACK_BOT_TOKEN`+`SLACK_APP_TOKEN` | `init` または `hermes config set --stdin` で書込み（argv に載せない） |

## 6. スケジュール構成（Path A 導入後）

収集ジョブは **hermes cron 6件 + launchd 5件** のハイブリッド
（services 所有: cron 6件と launchd 4件 `local.mcs-cmd`・`local.mcs-int`・
`ai.mcs.extract-drainer`・`ai.mcs.extract-drainer-2`。install.sh 所有:
復旧 watchdog `org.mcs.recovery`。別に LLM サーバ常駐の
`ai.mcs.llamaserver` / `ai.hermes.llamacpp`）。

ジョブ・スケジュール・実行スクリプトの一覧表は
[deployment/launchagents/README.md](../../deployment/launchagents/README.md)
に一本化している。正本はコードの `mcs/ops/mcs_setup.py` の `CRON_JOBS`
（hermes cron）と `AGENT_LABELS`（launchd）で、`services` はこれを
登録する。MCSサーバーの負荷対策として、未読収集を5分から24時間10分間隔へ変更した。
`mcs_check.sh` に夜間の間引きはない。
既存環境では更新後に `mcs setup services` で取得ジョブを再設定する。
`health.tick_interval_s` を300秒と明示している場合は、
`mcs setup init --yes --set health.tick_interval_s=600` で600秒へ変更する。
未指定時の既定は600秒。ローカルのヘルス監視自体は5分間隔を維持する。

## 7. トラブルシューティング

まず `./install.sh --preflight`（導入前・導入途中）または
`~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py check`
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
| `error: services reported problems` | stage 5 の `mcs_setup.py services` が失敗 | 直前の services の出力を確認、`$PY mcs/ops/mcs_setup.py check` の blocker を直して再実行 |
| `recovery template interpreter unsafe or unverified`（preflight NG）/ `desired recovery interpreter unsafe/unknown or deployed selection drift — nothing written` / `recovery desired interpreter unsafe or unverified — nothing in stage 6 installed` | 復旧 watchdog 用 Python（既定 `/usr/bin/python3`）が Python <3.9、または SQLite の WAL-reset 修正が無い・版不明。導入済みジョブと選択が異なる場合も停止 | 修正済み SQLite をリンクした既存の独立 Python を `--recovery-python <絶対パス>` で指定して再実行するか、`--no-recovery` で stage 6 を省く。既存ジョブとの不一致は `mcs_setup.py init --yes --recovery-python <絶対パス>` で希望を保存し、復旧ジョブが実行中でない時に `mcs_setup.py services` で修復 |
| `warn: ~/.local/bin is not on PATH` | shell から `hermes` が見えない | シェルの profile で `~/.local/bin` を PATH に追加（定期ジョブは独自の PATH を使うので影響しない） |
| `warn: ai.mcs.llamaserver plist changed but the running server was kept` | plist 更新時にサーバが応答中だった | 処理が空いた時に表示された `launchctl bootout ...; launchctl bootstrap ...` を実行 |
| `warn: hermes-managed .../ai.hermes.llamacpp.plist exists but is NOT loaded` | hermes 管理の LLM agent が止まっている | 表示された `launchctl bootstrap ...` を実行 |

### 7-2. `mcs_setup.py check` / `doctor` のメッセージ

| メッセージ | 原因・対処 |
|---|---|
| `mcs_setup requires Python >= 3.10` | 素の `python3`（3.9 系）で実行した — `$PY` で実行する |
| `service interpreter lacks the SQLite WAL-reset fix (need >=3.51.3, 3.50.7 or 3.44.6)` / `runtime SQLite WAL-reset fix missing` | 定期ジョブ（または復旧 watchdog）を実際に動かす Python のリンクする SQLite が影響版。`services`・`update` は変更前に停止し、`doctor` は `checks.runtime` / `checks.recovery_runtime` を blocked にする（`runtimes.*` は事実の表示だけ） — 修正済み SQLite（§1 の要件）をリンクした Python で venv を作り直す。復旧側は `--recovery-python` / `init --recovery-python` で安全な独立 Python を指定する |
| `interpreter ~/.hermes/hermes-agent/venv/bin/python is missing or not executable` | 定期ジョブが起動できない — `./install.sh` を再実行（stage 2 が venv を作り直す）→ `$PY mcs/ops/mcs_setup.py services`。`services` もこの状態では何も描画せず止まる（独立モードでは専用venvを診断する） |
| `hermes resolves here (...) but not on the launchd PATH` | 手元の shell では見えるが定期ジョブから見えない — 表示の `ln -s <hermes> ~/.local/bin/hermes`、または `init --set hermes_bin='"<パス>"'` |
| `config.json is unreadable or invalid` / init の `config: ... nothing written` | `~/.mcs/config.json` が壊れている — 手で直すか `$PY mcs/ops/mcs_setup.py init --yes` で `config.json.corrupt-<日時>` へ退避して作り直す |
| `missing required key: mcs_login_id` / `notify_target` | `init` で再登録（両方とも必須） |
| `Keychain entry 'mcs-adapter' not found` | パスワード未登録 — `init` で登録（`.env` `MCS_PASSWORD` があれば警告に格下げ） |
| `Keychain entry ... unreadable` | login keychain がロック中 — `security unlock-keychain` か GUI ログイン。再起動後も収集が必要な場合は `init` の `.env` フォールバック設定を確認 |
| `Chrome binary missing` | Chrome が `/Applications` に無い — `brew install --cask google-chrome` |
| `local LLM endpoint not reachable (http://127.0.0.1:8080/v1/models)` | llama-server 未起動 — Path A-1 または [スタンドアローン導入](STANDALONE.md)。収集自体は動く（警告） |
| `llama-server advertises N slots` | `-np` が選択スロット数（3）未満 — plist の `-np 3` を確認 |
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
| `hermes CLI not resolvable` | hermes 未導入（Hermesモードのエラー。独立モードならSTANDALONE.mdを確認。Path Aなら `install.sh` 再実行か `hermes_bin` 設定） |
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

### 任意: `/mcs-summary`（本人専用の要約サマリー）

カードの「全体の新着集計」に加え、Slackのslash commandからも呼び出せます（v1.0.11〜）。
使う場合だけ、Slackアプリ設定で次を追加して**アプリを再インストール**します。

- **Slash Commands** → Create New Command: Command `/mcs-summary`、
  Short Description「MCS 要約サマリー（本人専用）」、Usage Hint `all / mine / station:施設名 / project:ID / days:1-7`
  （Socket Modeでは Request URL は不要）
- **OAuth & Permissions** の Bot Token Scopes に `commands`（manifest生成済みなら含まれているか確認）

Hermes連携では plugin settings の `snapshot` が必要です（未設定だと「元データが設定されていません」と返します）。
返答は押した本人だけに見え（ephemeral）、`slack_allowed_user_ids` 以外のユーザーは拒否されます。
同名のコマンドを他アプリ・Hermes側で登録していないことを確認してください。

### 任意: `/mcs <JSON>`（全機能の閲覧・依頼管理・運用承認）

Slackアプリの **Slash Commands** に `/mcs`、Usage Hint `<JSON>` を登録し、
Bot Token Scopesの `commands` を確認してアプリを再インストールします。
Socket ModeではRequest URLは不要です。`/mcs-summary` は引き続き使えます。
許可ユーザー・固定チャンネル・公開snapshot・cmd inboxとinteractive設定が必要です。
Hermesでは既存Slack接続を使い、独立モードでは同じコマンドを独立SDK接続に登録します。

```text
/mcs {"op":"read","kind":"qc","project_id":1}
/mcs {"op":"read","kind":"signals","project_id":1}
```

全コマンドと人承認のpreview/confirm形式は[plugin手順](../../hermes_plugin/README.md)を参照してください。
変更コードの反映にはHermes gatewayまたは独立Slackアダプターの再起動が必要です。
