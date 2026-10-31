# MCS セットアップ実行手順書（AIエージェント用）

人が直接操作する手順は[はじめての導入・更新](EASY_SETUP.md)を参照してください。対話設定は推奨コースと全項目コースを選べます。エージェントの非対話引数と承認手順は本書のとおりです。

このガイドは現行のinstall/update/setup/doctorと、下記のinstall.sh・init・bootstrap・plan/apply・doctorの手順を説明する。
統一入口は `sh scripts/mcs install`、導入後は `mcs setup`・`mcs update`・`mcs doctor`。
導入する公開タグの仕様はCHANGELOGと[更新ガイド](UPGRADE_AGENT.md)で確認する。合成検証と実機受入は区別し、実接続・鍵・通知先の受入は各環境で確認する。
1.0.13の歴史的な開発・実機受入条件は[開発・受入計画](../releases/v1.0.13/RELEASE_1.0.13.md)に保持する。

> **この文書は AI エージェント（Devin / Claude Code / Codex 等）が
> 読み込み、ユーザーと対話しながら MCS を自動セットアップするための
> 実行手順書です。** 人間向けの設定リファレンス・経路説明は
> [INSTALLATION.md](INSTALLATION.md) を参照してください。

**LINE WORKS が指定された場合:** [LINEWORKS.md](LINEWORKS.md)を併読し、
本体の依存導入後に MCS の `notify.interactive=lineworks` と明示した操作範囲を
設定します。`lineworks_adapter init` は秘密値をユーザー自身の端末で入力し、
`check`（ローカル診断）→`run`（独自プロセス）の順に進めます。
公開 HTTPS Callback の配備・テナントへの試験送信は、その範囲の承認を使います。
Hermes の plugin 設定・gateway を LINE WORKS 用に追加しません。
常駐させる場合は `python -m lineworks_adapter service` で候補を生成し、
[常駐サービス手順](LINEWORKS.md#常駐サービスの候補を作る)に従って
plist/unit を検証します。サービス配置・起動は既存承認の範囲で行います。
候補生成だけでは常駐開始と報告しません。checkout と Python の絶対パスを
確認し、手動 `run` との二重起動を避けます。設定・dataの既定保存先は
本体と同じ `~/.mcs` で、checkoutと分離できます。`--root`を使う場合は
全コマンド・サービス候補・本体の保存先を揃えます。

## 0. エージェントへの実行契約（最初に読むこと）

セットアップ実行が依頼された範囲で適用する。文書の閲覧・レビューだけでは、
インストール、既読化、通知送信、サービス起動の承認は追加されない。
現在の明示指示・既存承認・AGENTS.md を優先する。

以下を厳守して実行すること:

1. **フェーズを順に実行する。** 各ステップは【実行】→【検証】のペア。
   検証をスキップして次に進まない。
2. **【ユーザー確認】は未決事項だけを確認する。** 既存の回答・承認・
   設定を再利用する。権限や対象が変わる不明点は確認し、それに依存しない
   許可済み作業を続ける。秘密値や対象IDを捏造しない。
3. **秘密情報は会話・コマンドライン・ログ・コミットに出さない。**
   §0-1 の秘密情報プロトコルに従う。
4. **既存状態を保護する。** 再実行前に設定・登録・receipt を照合する。
   既存承認の範囲で変更し、未承認の破壊操作・配備・外部送信だけ確認する。
   同じ操作を承認済みなら聞き直さない。
5. **失敗時。** 【失敗時】の対処を実行し、それでも解決しなければ
   原因を報告してユーザー判断を待つ。自己判断でスキップしない。
6. **完了時。** §7 の完了報告フォーマットで結果を出力する。

作業ディレクトリはこの checkout のリポジトリルートに揃える。開いている
cwd に `./install.sh` があれば、その checkout をそのまま使う。無い場合だけ
既存の clone 先を探すか、許可済みの clone を行ってそのルートへ移動する。
以後のコマンドはすべて同じルートで実行し、`cd hermes-mcs` を繰り返さない。

### 0-1. 秘密情報プロトコル

対象: **MCS パスワード・Discord bot token・Slack token・
TYPESAFE_API_KEY・LINE WORKS の Client Secret / Bot Secret / Private Key**。

- **禁止:** ユーザーにチャットへ平文で貼らせること。自分で生成した
  値を入れること。argv（`ps` に残る）に直接渡すこと。
- **方法 X（ユーザーが直接入力できる安全な tty がある場合）:**
  ユーザーの端末の bash 内で `read -rs` で受け取り、同じシェルプロセス内で
  `export` して使う。`init` の非対話実行が secrets を env で
  読む（`MCS_SETUP_PASSWORD`・`TYPESAFE_API_KEY`・`DISCORD_BOT_TOKEN`・
  `SLACK_BOT_TOKEN`・`SLACK_APP_TOKEN`）。

  ```bash
  read -rs -p "MCS パスワード: " MCS_SETUP_PASSWORD; echo; export MCS_SETUP_PASSWORD
  ```

- **方法 Y（tty が無い/ユーザーに任せたい場合）:** secrets を含む
  コマンドはユーザー自身の端末で実行してもらう。完了したら
  エージェントが結果だけを検証する（例: `.env` の存在と Keychain
  エントリの有無を確認する — **中身は表示しない**）。

## 質問画面の案内

`install.sh` の導入方法の選択、進捗、エラーと次の操作は日本語で表示します。
導入後は表示された `mcs setup` を実行して、アカウント・通知先・通知形式・
接続先ごとの権限・通知のまとめ方・朝のまとめ・収集範囲・AI・追加解析の順に設定します。
使わない接続先の質問と見出しは表示しません。

- 質問の日本語を先に読み、必要なら下の「設定項目」で設定キーを確認できます。
- Enter は既存の値を保持します。既存値がなければ表示された既定値を使います。
  必須項目に既定値がなければ、入力するまで先へ進みません。
- はい／いいえの質問は `はい`・`いいえ` または `y`・`n` で回答できます。
  選択式の質問は表示された番号でも、`hermes` など従来の値でも回答できます。
- 複数のIDは半角カンマで区切ります。ユーザー名・チャンネル名とIDは別物です。
  接続先IDの取得手順は本書のPhase 4、[導入ガイド](INSTALLATION.md)、
  [LINE WORKS](LINEWORKS.md) の該当する導入手順を参照してください。
- パスワードとトークンは本人の端末で非表示入力します。AIとの会話へ貼り付けません。
- Jev・Anthropic・OpenAIのキーは `mcs setup api-keys` の「1」、利用先と予算は「5」で設定できます。初期設定では、OpenAIの用途別モデルと推論度を保存前に表示します。利用先の設定では、API別・合計の上限を保存前に、用途別モデルと推論度を保存後に表示します。本文と文脈の送信先、費用、保持条件を利用者に確認してもらいます。
- 外部Jev APIへ本文を送る追加解析は、`auto`が既定です。このMCSの`.env`に
  キーが登録され、modeが未指定なら`assist`を使います。対象範囲と上限も
  未指定なら全対象・1日10000要求です。明示した`off`・対象範囲・低い上限は
  保持し、キーがない場合は送信しません。

質問を省略する既存の `--yes`・`--set` は引き続き使用できます。
コマンド名、オプション名、設定キー、機械処理用JSONの形式は変更していません。
OSや依存ソフトが返す技術的な詳細には英語が含まれることがあります。

## Phase 1 — 前提条件の確認

既存の回答・導入形態・通知先・操作範囲を最初にまとめ、未決事項だけを
一度に確認する。依存導入と収集開始は分けて進める。Chrome の CDP 起動や
MCS ログインは依存導入を妨げないため、初回収集の直前に確認する。

以下を順に確認する。Path B と決まっている場合は install.sh に
`--mode standalone`を付ける。不合格は【失敗時】
の指示に従う。**Python の事前確認はしない** — 新規 Mac の `python3` は
3.9 系で、Path A では `install.sh` が `python@3.14` と hermes-agent の
venv を用意する。MCS のスクリプト（`mcs_setup.py` 等）は素の `python3` で
実行しない（Path A は `~/.hermes/hermes-agent/venv/bin/python`、
Path B は `~/.mcs/venv/bin/python3`）。

| # | チェック | コマンド | 合格条件 |
|---|---|---|---|
| 1 | macOS である | `uname -s` | `Darwin` |
| 2 | 現 checkout のルート | `ls ./install.sh`。無ければ既存 clone 先へ移動する。新規取得が必要なら `git clone https://github.com/yusuketakuma/hermes-mcs.git` → `cd hermes-mcs`（git が無ければ先に `xcode-select --install`） | ルートの `./install.sh` が存在 |
| 3 | 前提一式（読取り専用） | `./install.sh --preflight` | exit 0・最終行 `事前確認: 導入を妨げる問題 0件、注意 N件` |
| 4 | 既存の MCS 設定 | `ls ~/.mcs/config.json 2>/dev/null` | 有無だけでは初期設定の完了を判断しない。既存値を表示せず、保持して不足項目を確認する |

`--preflight` は何も書き込まず、root 実行・macOS 版・Xcode CLT・
Homebrew・Python（brew が入れられない場合のみ 要対処）・git・空き容量・
github.com / huggingface.co への疎通・既存 hermes-agent checkout・
別 checkout からの既存導入・Chrome・:8080 の使用状況を
`正常`/`注意`/`要対処` で出す。要対処と一部の注意には `対処:` 行が付く。

【失敗時】

- 3: **要対処の行ごとに、その下の `対処:` を実行してから `--preflight` を
  再実行する**（要対処がなくなるまで）。ソフトウェアの導入を伴う fix
  （Homebrew 公式インストーラ・`xcode-select --install`・
  `brew install ...`）はユーザー承認のうえで実行、または GUI 操作が
  要るのでユーザー自身に実行してもらう。次は必ず【ユーザー確認】:
  - 「既存の導入先は別のフォルダです」 → 旧 checkout を使うか、
    `--force-repo` でこの checkout に切り替えるか（既存導入の張替え）
  - 「ポート8080を別のプロセスが使用しています」 →
    そのプロセスを止めるか、`--no-llm` で自前サーバを使うか
  - 「常駐サービスは独自の HERMES_HOME に対応していません」 → 既定 `~/.hermes` にするか `--no-services` か
  - 「管理者 root で実行されています」 → エージェント自身が sudo/root で動いていないか確認
  注意は続行可。内容はユーザーに伝える（例: 「Google Chrome が未導入です」 は
  stage 1 が入れない構成なら `brew install --cask google-chrome`）
- 4: 初回インストールが作った空の設定ファイル、または実行方法・復旧用 Python の選択だけが入った設定ファイルは、初期設定の完了を意味しない。保存された選択を保持し、ソフトの準備が完了していれば同じ checkout の初期設定から再開する。値をチャットに出さない。不正・読取不能な場合は削除や上書きをせず調査する。
- 4: それ以外の既存 `~/.mcs/config.json` がある → 指示・回答から維持/再設定が
  決まっていればその方針を使う。未決の場合だけ **【ユーザー確認】**:
  「既存の MCS 設定が見つかりました。上書きせず維持して確認のみ
  進めますか？それとも `init` で再設定しますか？」

## Phase 2 — 導入形態の選択【ユーザー確認】

今回の初回受入候補はmacOS・Apple silicon・SlackのPath Bです。この候補が指定済みなら確認を繰り返さず、`--mode standalone`を明示してPhase 6へ進みます。既存設定と指定がある場合はそれを維持します。installerのEnter既定はHermesのままで、候補の選択を正式対応・実機受入済みとは扱いません。

導入形態がまだ決まっていなければ、ユーザーに次を尋ねる:

> 導入形態を選んでください:
> **A**: hermes-agent アドオン（推奨・全機能 — Discord/Slack 通知・
> 対話カード・`/mcs` コマンドあり）
> **B**: スタンドアローン（全MCS機能。Slack/Discord/LINE WORKSの
> 通知・操作をHermesなしで使う）

- A → Phase 3
- B → Phase 6

## Phase 3 — Path A: 依存の自動導入

### 3-1. install.sh

Phase 1 の `--preflight` が exit 0 であることが前提。

【実行】

```bash
# Phase 1 で確認した同じリポジトリルートで実行
./install.sh --dry-run   # 任意: 作成・変更されるものを確認（何も書き込まない）
./install.sh             # Phase 1 の【ユーザー確認】で決めたフラグ（--no-llm / --force-repo 等）を付ける
```

【検証】`ソフトの準備が完了しました` と 6 ステージ分の結果が表示される。
終了コード0でも、初期設定を後回しにした場合は利用開始前の作業が残る。
初期設定を開始して失敗・中断した場合は非0となるが、ソフトの再導入は不要。
表示された再開コマンドと、次の確認で段階を区別する:

```bash
~/.hermes/hermes-agent/venv/bin/python -V        # 3.14（以後の mcs_setup はこのインタプリタ）
ls -la ~/.hermes/plugins/mcs-discord-commands    # symlink がこの checkout の hermes_plugin/ を指す
curl -sf -m 3 http://127.0.0.1:8080/v1/models    # llama-server 応答（--no-llm なら自前サーバ）
cat ~/.mcs-recovery/repo_path                    # この checkout の絶対パス（--no-recovery 以外）
```

導入結果に続いて表示される `init`・`services`・`check` のコマンド
（venv インタプリタと `mcs_setup.py` のフルパス付き）を Phase 5 で使う。

【失敗時】install.sh は失敗したステージで止まり、`エラー: <原因と直し方>`
と `エラー: 導入を中断しました。失敗した段階の原因を解消し、install.sh を再実行してください` を出す（後続ステージは実行されない）。`エラー:` 行の指示を
実行し、同じフラグで `./install.sh` を再実行する — 完了済みの部分は
skip され、中断した clone/checkout・venv・pip install・モデル DL
（`.part` から再開）は続きから進む。よくある原因と対処は
INSTALLATION.md §7-1。`注意:` 行（`~/.local/bin` が PATH に無い、
llama plist の再読込保留、hermes 管理 LLM agent の未ロード等）は
install を止めないが、表示されたコマンドをユーザーに案内する。

## Phase 4 — Path A: 通知先の準備【ユーザー確認】

### 4-1. 通知先の選択

【ユーザー確認】未決の場合のみ確認。既に指定されていればその経路へ進む。

> 通知先を選んでください:
> **slack**（推奨）/ **discord** / **lineworks** / **off**（通知なし —
> テキスト通知も含め配送しない）

- `slack` → 4-2
- `discord` → 4-3
- `lineworks` → [専用導入手順](LINEWORKS.md)。Phase 5のMCS設定では
  `notify.lineworks`の許可範囲を指定し、専用CLIで秘密値を入力・診断する。
  Slack/Discord用plugin設定・gateway同期は行わない
- `off` → Phase 6 の通知を送らない構成を使う。`interactive=off` は
  テキスト配送を止めないため、全 runner に `--no-notify` が必要。
  既存の Hermes 定期ジョブがある場合は、承認範囲内で停止・置換を確認する

### 4-2. Slack の場合（推奨）

INSTALLATION.md 付録B を案内し、次を収集:

1. `SLACK_BOT_TOKEN`（xoxb-）・`SLACK_APP_TOKEN`（xapp-）— secrets
2. `team_id`・`application_id`（api_app_id）・`channel_id`
3. `slack_allowed_user_ids`（member ID カンマ区切り）
4. `slack_profile`・`project_ids`

Slack も `init` が設定ブロックとトークンを書き込む。`init` を使えない
場合の手動設定手順は 5-3 を参照。

### 4-3. Discord の場合

未取得の値をまとめて確認する。各値の取り方は
INSTALLATION.md 付録A を案内する（Developer Portal での手順を
必要に応じて説明する）。

【ユーザー確認 — 値の収集】

1. `DISCORD_BOT_TOKEN`（§0-1 の秘密情報プロトコルで受け取る）
2. `application_id`（General Information の Application ID）
3. `guild_id`（サーバーID — 開発者モードで右クリック）
4. `channel_id`（カード投稿先チャンネルID）
5. `allowed_user_ids`（カード操作を許可する Discord user ID・
   カンマ区切り）
6. `allowed_chat_ids`（`/mcs` を受け付けるチャンネルID — 通常は
   channel_id と同じ）
7. `plugin_profile`（Discord を受け持つ hermes profile —
   分けていなければ空欄＝既定）
8. `project_ids`（対象の MCS project ID — `mcs_view status` で
   確認するか後で plugin に `project_ids_auto: true`）

【ユーザー確認 — bot 準備の確認】

> Discord Developer Portal で bot を作成済みですか？未作成なら
> INSTALLATION.md 付録A の Step 1–7 を案内します。**Message
> Content Intent・Server Members Intent・Create Public
> Threads・Send Messages in Threads・Attach Files の権限が
> 必要です。**

## Phase 5 — Path A: init・services・check

installer 内で初期設定を完了した場合は `init` を繰り返さず、結果を確認して次へ進む。後回し・中断した場合は保存済みの設定を保持し、同じ checkout の `mcs setup` から再開する。

### 5-1. 設定ウィザード実行

【ユーザー確認】対話ウィザードか非対話かを選んでもらう:

- **対話（推奨・初回）:** ユーザー自身の端末で
  `~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py init`
  を実行してもらう（`mcs_setup` は Python ≥3.14 必須 — install.sh が
  用意した venv インタプリタ）。全項目が
  説明付きで順に出る。
- **非対話:** エージェントが収集した値で実行（secrets は §0-1 経由）:

  ```bash
  # secrets は §0-1 に従って同じ端末の環境変数に準備済み
  ~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py init --yes \
      --login-id <ID> --notify-target slack:<channel_id> \
      --set 'notify.interactive="slack"' \
      --set 'notify.slack={"profile":"P","application_id":"A","team_id":"T","channel_id":"C"}' \
      --plugin-profile <profile> \
      --plugin-user-ids <uid,...> \
      --plugin-project-ids <pid,...>
  ```

  Discord の場合は `notify.interactive="discord"` +
  `notify.discord={"profile","application_id","guild_id","channel_id"}`
  と `--notify-target discord:<channel_id>`・`--plugin-chat-ids` を使い、
  トークンは `DISCORD_BOT_TOKEN` で渡す。

LINE WORKSは[専用導入手順](LINEWORKS.md#3-mcs-の配送先と操作範囲を設定する)の
設定を使い、`--plugin-*`やSlack/Discordトークンを指定しません。
MCS `init`の後に、ユーザー端末で`python -m lineworks_adapter init/check`を
行ってからMCS `check`を再実行します。秘密値を`--set`や環境変数へ渡しません。

`init` は既存の `~/.mcs/config.json` が壊れているか安定して読み取れないと、
通常は保存せずに止まる。
**【ユーザー確認】** 手で直すか、`--yes` で `config.json.corrupt-<日時>` に
退避して既定値から作り直すか。退避は、本人所有で内容を確認できる1 MiB以下の通常ファイル
またはシンボリックリンクを扱う。開始時に1 MiB超、読取不能、FIFO、複数のハードリンクが
ある通常ファイルの場合は退避せず停止する。コピーや保存に失敗した場合は表示された案内に従って状態を確認する。親フォルダーが
modeで本人以外に書き込みが許可される場合も停止し、シンボリックリンクではmodeで本人以外の
アクセスが許可される場合も停止する。ACLによるアクセスは別途確認する。
`init` は最後に `check` を自動実行する。

【検証】`config`・`.env`・Keychain が書かれたこと:

```bash
~/.hermes/hermes-agent/venv/bin/python - <<'PYSETUP'
import json
from pathlib import Path
cfg = json.loads((Path.home() / ".mcs/config.json").read_text())
assert isinstance(cfg, dict)
print("config JSON object: ok")
PYSETUP
ls -l ~/.mcs/.env                    # 0600・中身は見せない
security find-generic-password -s mcs-adapter >/dev/null && echo keychain-ok
```

### 5-1a. 任意機能のセットアップ案内

`init` の対話の最後に、任意機能の案内（現在はCSDC: 状態監視・遠隔支援）を「既定に従う／案内を表示する／案内を省略する」から選びます。この版の既定は「省略」で、空欄のEnterは現在の選択を保持します。選ぶのは案内の表示だけで、CSDCの登録・同意・送信はここでは行いません。この任意項目は端末から実行したときだけ質問します。端末以外では質問せず、この項目の選択も変えません（`--yes` ではこの項目自体を表示しません。初期設定のほかの設定は従来どおり保存されます）。後から変える場合は端末で `mcs setup features`（PATH未設定なら `~/.local/bin/mcs setup features`）を実行します。内容は[状態監視の説明](SDC_SELF_MONITORING.md)を参照してください。

### 5-2. サービスの再同期（必要時のみ）

通常の初回導入では install.sh が cron/launchd を登録済みで、`init` が
gateway を同期するため、この操作は不要。`--no-services` を使った場合、
または最終チェックが配置 drift・未登録を報告した場合だけ実行する。

【実行】

```bash
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py services
```

【検証】cron/launchd/gateway が登録されたこと:

```bash
hermes cron list --all | grep -iE "mcs|llamacpp"   # 6件登録
launchctl print gui/$(id -u) 2>/dev/null | grep -E "mcs|llamaserver" | head
hermes gateway status                # "supervised" が含まれる
```

LINE WORKSにはgateway状態の検証を適用せず、独立プロセスと公開Callback経路を
検証します。常駐用候補の生成・配置は[専用サービス手順](LINEWORKS.md#常駐サービスの候補を作る)。

### 5-3. Slack の手動設定（`init` を使わない場合のみ）

```bash
hermes config set plugins.entries.mcs-discord-commands.settings.slack_adapter_enabled true
# slack_team_id / slack_application_id / slack_channel_id /
# slack_allowed_user_ids / slack_profile / project_ids / data_root /
# snapshot を同様に設定（INSTALLATION.md 付録B「MCS に必要な設定」参照）
```

### 5-4. 必須条件の検証

【検証】`init` の自動チェックで exit 0・結果行
`設定確認: 正常 （問題0件、注意N件）` を確認する。設定・サービスを後から
直した場合だけ `~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py check`
を再実行する。

【失敗時】`check` はエラーを優先度順（実行基盤 → config → マシン →
配置 drift）に並べ、最後に `動作を妨げる問題がN件あります。次の順で修正してください:` として
番号付きの要約と `対処:` 行を出す。**1 番から順に直して `check` を
再実行する**（上位の原因が下位のエラーを引き起こしていることがある）。
報告用の要約には `doctor` を使う — ローカルの読取りだけで範囲ごとの
状態と件数を出し、`check` は実行しない。`not_checked` は正常を意味せず、
終了コードは `blocked` の範囲があるときだけ 1（`check` が要修正 でも 0 に
なり得る）。`--probe llm` / `--probe services` で明示した範囲だけ追加確認する。
blocker 一覧と launchd agent の状態は `check` で確認する:

```bash
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py doctor
```

メッセージ別の対処は INSTALLATION.md §7-2。主なもの:

- 実行用Pythonが見つからない、または実行できない → `./install.sh` 再実行
  （stage 2 が venv を作り直す）→ `services`
- 定期実行からHermesを呼び出せない → 表示の
  `ln -s ... ~/.local/bin/hermes`
- 必須設定が不足している → `init` で再登録
- Keychainの登録内容を読み取れない → `security unlock-keychain` を
  ユーザーに依頼
- 常駐処理の設定はあるが起動していない → 表示の
  `launchctl bootstrap ...`（MCS 5件なら `services` 再実行）
- 配置済みのスクリプトが配布版と異なる → `services` 再実行
- 復旧監視が別の保存先を参照している → 正しい
  checkout で `./install.sh`（移動した場合。別 checkout が残っていれば
  `--force-repo` は【ユーザー確認】）
- `hermes gateway is not supervised` → `services` 再実行
- `llama-server advertises N slots` → plist の `-np 3` 確認

### 5-5. 動作確認

[専用Chromeとセッション確認](INSTALLATION.md#初回の専用chromeとセッション確認両モード共通)に従い、専用profile準備・本人ログイン・CDP稼働・MCSセッション・初回収集を別状態で確認する。CDP応答だけで認証成功とせず、CDPもtoken cacheもない初回の`bootstrap_error`を自動再ログイン待ちだけで解消すると仮定しない。`manual_required:multi_user_host`は停止を保持し、専用OSユーザー追加だけで解決扱いしない。
実データの取得・通知・既読化が既存の承認範囲に含まれることを確認し、
範囲外ならその操作の承認だけを得てから実行する。

【実行】

```bash
PY=~/.hermes/hermes-agent/venv/bin/python
$PY mcs/ingest/run_check.py --json --download-files --mark-read
$PY mcs/views/mcs_view.py status
```

【検証】`run_check` が exit 0 で終了し、`status` に患者・メッセージ
数が出る。新着投稿があれば通知が届くことをユーザーに確認してもらう。

【失敗時】`data/mcs.log` の必要部分をローカルで確認し、状態・件数・
エラー分類だけを報告する。`session_expired`
なら `auto_login` の復旧を待つか INSTALLATION.md §7 の Keychain 節を案内。

## Phase 6 — Path B: スタンドアローン

Hermesなしで全MCS機能を使用する経路です。[STANDALONE.md](STANDALONE.md)と
INSTALLATION.md §B-1に従い、同じcheckoutから次を実行します。
Slack・Discordを新しく接続する場合は、先に[アプリと許可範囲の準備](STANDALONE.md#slackとdiscordの接続準備)を確認します。Path AのHermes用manifest・profile・gateway設定は使いません。

```bash
./install.sh --mode standalone --preflight
./install.sh --mode standalone
```

installer内で初期設定を完了した場合は、次の`mcs setup`を繰り返しません。後回し・中断した場合だけ`~/.local/bin/mcs setup`で再開します。既存の直接入口は`$HOME/.mcs/venv/bin/python3 mcs/ops/mcs_setup.py init --runtime-mode standalone`です。
設定後の診断と独立hostの状態は、同じcheckoutから確認します。

```bash
PY="$HOME/.mcs/venv/bin/python3"
$PY mcs/ops/mcs_setup.py check
$PY -m mcs_standalone status
```

Chromeは[両モード共通の初回準備](INSTALLATION.md#初回の専用chromeとセッション確認両モード共通)に従います。専用profile準備・本人ログイン・CDP稼働・MCSセッション・初回収集は別々に確認し、同節のPath A用Pythonではなく上の`$PY`を使います。収集・通知・既読化は承認した範囲でだけ確認します。

導入・外部配信・既読化の承認は§0の実行契約に従います。既に承認された範囲は
聞き直さず、未知のMCSログイン・通知先・操作ユーザー・プロジェクト範囲だけ確認します。
Bot token等は端末の非表示入力に委ね、チャットやargvへ含めません。既存の秘密値は保持します。
`--yes`では秘密値を環境から推測せず、不足時は`credentials_missing`で停止します。

SlackはBot/App token、DiscordはBot tokenを専用0600ファイルへ保存します。
LINE WORKSの認証・HTTPS Callbackは[専用手順](LINEWORKS.md)に従います。
LINE WORKSは独立host内で起動するため、同じ資格情報で別アダプターを常駐させません。

旧Hermes modeから移行する場合、`services`が旧manifest所有MCSジョブを停止します。
手動crontabなどmanifest外の既存経路は確認し、明示範囲内だけで停止してください。
独立hostは6定期ジョブ・2抽出worker・cmd/cmd_int取込を所有し、同じジョブを別途登録しません。

`check`の失敗は想定内として無視しません。モード・専用venv・scope・資格情報・常駐状態・
文書生成とのdriftを確認し、固定エラーコードで原因を報告します。
実MCS取得・通知・カード操作の検証は許可された対象だけで行い、未実施なら明記します。
Linuxのsystemd候補は実機適用・検証済みとは報告しません。

## Phase 7 — 完了報告

セットアップ完了時に次の形式で報告する:

```text
【MCS セットアップ完了】
- 導入形態: A(hermes アドオン) / B(スタンドアロン)
- 事前チェック: install.sh --preflight = 導入を妨げる問題 0件 / 注意N件
- install.sh: 導入完了（runtime_modeと使用フラグ: …）
- 設定: ~/.mcs/config.json（notify_target=…、interactive=…）
- 秘密情報: Keychain mcs-adapter=登録済み / .env=設定済み
- LINE WORKS利用時: 認証ファイル=権限確認済み、独立プロセス=稼働/不要/未起動
- スケジュール: hermes cron=N件 / launchd=N件 / standalone host=稼働・未起動
- 検証: mcs_setup check = exit 0 / 警告N件（内容: …）
- 初回 run: 成功（messages=N, patients=N）/ 失敗（原因: …）
- 通知先: discord:… / slack:… / lineworks:… / なし
- 次の注意: Slack/Discord plugin更新はgateway、LINE WORKS更新は独立プロセスへ反映
```

未達項目があれば完了報告の末尾に「残タスク」として列挙する。
