# スタンドアローンモード（Hermes なしで全機能）

`runtime_mode: "standalone"` を選ぶと、Hermes Agent をインストールせずに
収集・保存・検索・抽出・通知・Slack/Discord のカード操作・人承認・
Discord の `/mcs`・定期実行・更新・復旧がすべて動きます。
設定しない場合（既定の `hermes`）の動作は従来と同じです。

Hermes の AI チャット機能（Bot との会話）は Hermes 自身の機能なので、
このモードには含まれません。MCS の機能だけが対象です。

## 仕組み

| 役割 | Hermes モード | スタンドアローン |
|---|---|---|
| テキスト通知の送信 | `hermes send` | `mcs_standalone send`（同じ封印 JSON・添付照合） |
| カード配送・ボタン操作・確認 | gateway 上の plugin | 常駐 `ai.mcs.standalone`（同じ配送・承認コード） |
| Discord `/mcs` | Hermes の native command | 同じ handler を `/mcs args:<JSON>` として登録 |
| 定期ジョブ6件 | `hermes cron` | launchd `ai.mcs.cron.*`（同じ wrapper・同じ時刻） |
| 抽出 drainer・コマンド watcher | launchd | launchd（同じ。Python だけ `~/.mcs/venv`） |
| 更新後の再起動 | `ai.hermes.gateway` | `ai.mcs.standalone` |

Slack/Discord の SDK は `deployment/requirements-standalone.txt` の固定版だけを
`~/.mcs/venv` に入れます。収集・解析コアは標準ライブラリのままです。
`/mcs` の応答は本人にだけ見える（ephemeral）メッセージで返ります。

## 導入

`--mode` を付けずに `./install.sh` を端末で実行すると、初回は
「1) hermes / 2) standalone」を尋ねます。既に `config.json` に `runtime_mode` が
あればその方式を引き継ぎ、非対話の実行では従来どおり hermes になります。

```bash
./install.sh --mode standalone --preflight     # NG が無いことを確認
./install.sh --mode standalone                 # venv・SDK・launchd・復旧 watchdog
PY=~/.mcs/venv/bin/python3
$PY mcs/ops/mcs_setup.py init                  # ウィザード（下記）
$PY mcs/ops/mcs_setup.py services              # 接続プロセスを含めて登録
$PY mcs/ops/mcs_setup.py check
```

`init` のウィザードは `runtime_mode=standalone` のとき次も尋ねます。

- `notify.<slack|discord>.allowed_user_ids` — カード操作・`/mcs` を許可するユーザーID
- `notify.<slack|discord>.project_ids` — 対象とする MCS プロジェクトID
- Discord のみ `allowed_chat_ids`（`/mcs` を受け付けるチャンネル。空欄=カード投稿先）
  と `allowed_role_ids`（サーバーID=@everyone は不可）
- Bot トークン（入力は非表示）。`~/.mcs/.env`（0600）にだけ保存されます:
  `DISCORD_BOT_TOKEN`、`SLACK_BOT_TOKEN`、カードを Slack で使う場合は
  `SLACK_APP_TOKEN`（Socket Mode 用 `xapp-`）。実行時は `~/.hermes/.env` や
  シェルの環境変数を読みません。Hermes から切り替える場合は、`init` が
  `~/.hermes/.env` にある値を使うか確認してから `~/.mcs/.env` へ写します。

通知先は `discord:<チャンネルID>` / `slack:<チャンネルID（C/G/D で始まる）>` の形で
指定します（チャンネル名・スレッド指定・ユーザー宛は Hermes モードのみ）。
Discord のフォーラムチャンネルには新しい投稿として送ります。上限を超える添付は
本文に注記して省き、Slack への添付に失敗しても本文は配送済みとして扱います。Slack/Discord アプリの作成は
[インストールガイド付録A/B](INSTALLATION.md#付録a-discord-接続設定hermes-agent-リポジトリより転記)
の手順1〜7と同じです（手順8の `~/.hermes/.env` と `hermes gateway` は不要。
トークンは `init` が `~/.mcs/.env` に保存します）。Slack は Socket Mode と
Interactivity を有効にしてください。Discord への添付は 10 MB（ブーストなしの
サーバー上限）を超えるものを本文に注記して省きます。

非対話で導入する場合（秘密値は対話入力か安全な端末で環境変数に準備し、
履歴に平文を残さない。[AI向け手順 §0-1](SETUP_AGENT.md) の `read -rs` を参照）:

```bash
MCS_SETUP_PASSWORD=... DISCORD_BOT_TOKEN=... $PY mcs/ops/mcs_setup.py init --yes \
  --runtime-mode standalone --login-id <MCSログインID> \
  --notify-target discord:<チャンネルID> \
  --set 'notify={"interactive":"discord","discord":{"profile":"default",
        "application_id":"<アプリID>","guild_id":"<サーバーID>",
        "channel_id":"<チャンネルID>","allowed_user_ids":["<ユーザーID>"],
        "project_ids":[<プロジェクトID>]}}'
```

## 確認

```bash
$PY mcs_standalone/__main__.py check     # 設定・トークン・SDK 版（通信なし）
$PY mcs/ops/mcs_setup.py doctor          # runtime と各 LaunchAgent の状態
tail -f ~/.mcs/data/standalone.log       # 接続プロセスのログ（本文・秘密値は出しません）
```

定期ジョブの出力は `~/.mcs/data/cron.log`、収集の詳細は従来どおり `run.log` です。

## Hermes モードからの切り替え

1. `install.sh --mode standalone` を実行し、`init` で上記の許可リストと
   トークンを設定します（Hermes の plugin 設定は自動では移しません）。
   `notify.<transport>.profile` は Hermes で使っていた値のままにすると、
   配送中のカードの記録をそのまま引き継げます。
2. `services` が Hermes cron の MCS ジョブを削除し、launchd のジョブと
   `ai.mcs.standalone` を登録します。
3. 同じ Bot を Hermes gateway も使っている場合は、Hermes からその Slack/Discord
   接続を外すか gateway を停止してください。同じ Bot に2つの接続があると、
   Slack はボタン操作を両方へ振り分けます。standalone 選択中は、再起動後の
   Hermes は MCS の `/mcs` とカード処理を登録しませんが、動作中の gateway は
   停止または再起動まで旧設定のままです。

Hermes モードへ戻すときは `./install.sh --mode hermes` を実行し、続けて
`~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py init` で Hermes plugin の
許可リストとトークン（Hermes profile 側の `.env`）を設定してから `services` と `check` を
実行します（`init` で `runtime_mode` だけを変えても方式は切り替わらず、
install.sh の実行を案内します）。`ai.mcs.cron.*` と `ai.mcs.standalone` は退役され、
Hermes cron が再登録され、gateway は MCS の処理を読み込み直すため再起動されます。
Hermes CLI がまだ使えない間は、収集が止まらないよう `ai.mcs.cron.*` を残します。
ローカル LLM のモデルは、どちらの方式で取得したものでも切り替え後にそのまま使い、
再ダウンロードしません。

## 更新とロールバック

更新後は `ai.mcs.standalone` が再起動されます。`deployment/requirements-standalone.txt`
（SDK の固定版）が変わるリリースは自動更新の対象外です（`standalone_requirements_changed`）。
タグを取得して `./install.sh` を再実行してください。スタンドアローン導入前のコミットへの
ロールバックは拒否します（`standalone_rollback_unsupported`）。その場合は先に Hermes
モードへ戻します。

## 送信結果の扱い

`mcs_standalone send` は `hermes send` と同じ約束を守ります。
0=配送済み、2=送信前の設定・入力エラーや添付の容量超過（添付を外して本文だけ再送）、
75=受理されていないことが
確実（backoff つきで再試行。繰り返し失敗する恒久的な原因は5回で送信保留へ移行）、
それ以外=成否不明（自動再送しない）。
メンションは常に無効化し、送信先は `notify_target` / `notify_system_target`
に書かれたチャンネルだけです。
