# MCS セットアップ実行手順書（AIエージェント用）

> **この文書は AI エージェント（Devin / Claude Code / Codex 等）が
> 読み込み、ユーザーと対話しながら MCS を自動セットアップするための
> 実行手順書です。** 人間向けの設定リファレンス・経路説明は
> [INSTALLATION.md](INSTALLATION.md) を参照してください。

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

### 0-1. 秘密情報プロトコル

対象: **MCS パスワード・Discord bot token・Slack token・
TYPESAFE_API_KEY**。

- **禁止:** ユーザーにチャットへ平文で貼らせること。自分で生成した
  値を入れること。argv（`ps` に残る）に直接渡すこと。
- **方法 X（ユーザーが直接入力できる安全な tty がある場合）:**
  ユーザーの端末の bash 内で `read -rs` で受け取り、同じシェルプロセス内で
  `export` して使う。`init` の非対話実行が secrets を env で
  読む（`MCS_SETUP_PASSWORD`・`TYPESAFE_API_KEY`・`DISCORD_BOT_TOKEN`）。

  ```bash
  read -rs -p "MCS パスワード: " MCS_SETUP_PASSWORD; echo; export MCS_SETUP_PASSWORD
  ```

- **方法 Y（tty が無い/ユーザーに任せたい場合）:** secrets を含む
  コマンドはユーザー自身の端末で実行してもらう。完了したら
  エージェントが結果だけを検証する（例: `.env` の存在と Keychain
  エントリの有無を確認する — **中身は表示しない**）。

## Phase 1 — 前提条件の確認

以下を順に実行し、全て合格してから Phase 2 へ。不合格は【失敗時】
の指示に従う。

| # | チェック | コマンド | 合格条件 |
|---|---|---|---|
| 1 | macOS である | `uname -s` | `Darwin` |
| 2 | Homebrew | `command -v brew` | パスが返る |
| 3 | Python 3.11–3.13 | `python3 -c 'import sys;print(sys.version_info[:2])'` | `(3, 11)`〜`(3, 13)` |
| 4 | Chrome が存在 | `ls -d "/Applications/Google Chrome.app"` | 存在 |
| 5 | CDP :9333 が開いている | `curl -sf -m 3 http://127.0.0.1:9333/json/version` | JSON が返る |
| 6 | 空き容量 ~10GB | `df -g ~ | awk 'NR==2{print $4}'` | 10 以上 |
| 7 | 既存の MCS 設定 | `ls ~/.mcs/config.json 2>/dev/null` | あれば【ユーザー確認】で再設定か維持かを聞く |

【失敗時】

- 2: brew 未導入 → ユーザーに公式インストール手順を案内して待機
- 4: Chrome 無し → `brew install --cask google-chrome`（ユーザー承認）
- 5: Chrome が CDP で起動していない → ユーザーに「MCS ログイン済み
  プロファイルで `--remote-debugging-port=9333` 付きで起動して
  ください」と依頼。起動確認できてから継続
- 7: 既存 `~/.mcs/config.json` がある → **【ユーザー確認】**
  「既存の MCS 設定が見つかりました。上書きせず維持して確認のみ
  進めますか？それとも `init` で再設定しますか？」

## Phase 2 — 導入形態の選択【ユーザー確認】

ユーザーに次を尋ねる:

> 導入形態を選んでください:
> **A**: hermes-agent アドオン（推奨・全機能 — Discord/Slack 通知・
> 対話カード・`/mcs` コマンドあり）
> **B**: スタンドアロン（収集・保存・閲覧のみ — 通知は送られません。
> 後から A に移行できます）

- A → Phase 3
- B → Phase 6

## Phase 3 — Path A: 依存の自動導入

### 3-1. clone と install.sh

【実行】

```bash
# 既に clone 済みなら skip（ls で確認してから）
git clone https://github.com/yusuketakuma/hermes-mcs.git
cd hermes-mcs
./install.sh
```

【検証】次が全て成立すること:

```bash
command -v hermes                          # hermes CLI
ls -la ~/.hermes/plugins/mcs-discord-commands   # symlink がある
curl -sf -m 3 http://127.0.0.1:8080/v1/models   # llama-server 応答
```

【失敗時】install.sh の出力をユーザーに見せる。hermes 未導入なら
スクリプトが pin 済み fork を導入する — 出力を確認し
`~/.local/bin` が PATH に無い場合は PATH 設定を案内する。
モデル DL 失敗時はスクリプトの warn に従い手動配置を案内。

## Phase 4 — Path A: 通知先の準備【ユーザー確認】

### 4-1. 通知先の選択

【ユーザー確認】

> 通知先を選んでください:
> **discord** / **slack** / **off**（通知なし — テキスト通知も
> 含め配送しない）

- `discord` → 4-2
- `slack` → 4-3
- `off` → Phase 6 の通知を送らない構成を使う。`interactive=off` は
  テキスト配送を止めないため、全 runner に `--no-notify` が必要。
  既存の Hermes 定期ジョブがある場合は、承認範囲内で停止・置換を確認する

### 4-2. Discord の場合

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

### 4-3. Slack の場合

INSTALLATION.md 付録B を案内し、次を収集:

1. `SLACK_BOT_TOKEN`（xoxb-）・`SLACK_APP_TOKEN`（xapp-）— secrets
2. `team_id`・`application_id`（api_app_id）・`channel_id`
3. `slack_allowed_user_ids`（member ID カンマ区切り）
4. `slack_profile`・`project_ids`

Slack は wizard 非対応のため、`init` 後に `hermes config set`
で手動設定する（5-3 参照）。

## Phase 5 — Path A: init・services・check

### 5-1. 設定ウィザード実行

【ユーザー確認】対話ウィザードか非対話かを選んでもらう:

- **対話（推奨・初回）:** ユーザー自身の端末で
  `~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py init`
  を実行してもらう（`mcs_setup` は Python ≥3.10 必須 — install.sh が
  用意した venv インタプリタ）。全項目が
  説明付きで順に出る。
- **非対話:** エージェントが収集した値で実行（secrets は §0-1 経由）:

  ```bash
  MCS_SETUP_PASSWORD=<sec> TYPESAFE_API_KEY=<key> DISCORD_BOT_TOKEN=<tok> \
  ~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py init --yes \
      --login-id <ID> --notify-target discord:<channel_id> \
      --set 'notify.interactive="discord"' \
      --set 'notify.discord={"profile":"P","application_id":"A","guild_id":"G","channel_id":"C"}' \
      --plugin-profile <profile> \
      --plugin-user-ids <uid,...> --plugin-chat-ids <chid,...> \
      --plugin-project-ids <pid,...>
  ```

【検証】`config`・`.env`・Keychain が書かれたこと:

```bash
python3 - <<'PYSETUP'
import json
from pathlib import Path
cfg = json.loads((Path.home() / ".mcs/config.json").read_text())
assert isinstance(cfg, dict)
print("config JSON object: ok")
PYSETUP
ls -l ~/.mcs/.env                    # 0600・中身は見せない
security find-generic-password -s mcs-adapter >/dev/null && echo keychain-ok
```

### 5-2. サービス登録

【実行】

```bash
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py services
```

【検証】cron/launchd/gateway が登録されたこと:

```bash
hermes cron list --all | grep -i mcs # 6件登録
launchctl print gui/$(id -u) 2>/dev/null | grep -E "mcs|llamaserver" | head
hermes gateway status                # "supervised" が含まれる
```

### 5-3. Slack の場合の追加設定（4-3 のときのみ）

```bash
hermes config set plugins.entries.mcs-discord-commands.settings.slack_adapter_enabled true
# slack_team_id / slack_application_id / slack_channel_id /
# slack_allowed_user_ids / slack_profile / project_ids / data_root /
# snapshot を同様に設定（INSTALLATION.md 付録B「MCS に必要な設定」参照）
```

### 5-4. 必須条件の検証

【実行】`~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py check` — exit 0 を期待

【失敗時】INSTALLATION.md §7 の対応表に従う。主なもの:

- `missing required key` → `init` で再登録
- `Keychain entry ... unreadable` → `security unlock-keychain` を
  ユーザーに依頼
- `hermes gateway is not supervised` → `services` 再実行
- `llama-server advertises N slots` → plist の `-np 2` 確認

### 5-5. 動作確認

【実行】

```bash
PY=~/.hermes/hermes-agent/venv/bin/python
$PY mcs/ingest/run_check.py --json --download-files --mark-read
$PY mcs/views/mcs_view.py status
```

【検証】`run_check` が exit 0 で終了し、`status` に患者・メッセージ
数が出る。新着投稿があれば通知が届くことをユーザーに確認してもらう。

【失敗時】`data/run.log` の必要部分をローカルで確認し、状態・件数・
エラー分類だけを報告する。`session_expired`
なら `auto_login` の復旧を待つか INSTALLATION.md §7 の Keychain 節を案内。

## Phase 6 — Path B: スタンドアロン

### 6-1. 依存

【実行】

```bash
brew install python@3.13 llama.cpp       # 未導入のみ
brew install --cask google-chrome        # 未導入のみ
git clone https://github.com/yusuketakuma/hermes-mcs.git
cd hermes-mcs
```

### 6-2. init（通知は送らない前提の設定）

【ユーザー確認】以下の値を尋ねる:

1. `mcs_login_id`
2. MCS パスワード（§0-1）
3. `semantic` を使うか（off 推奨の初期値）

【実行】

```bash
MCS_SETUP_PASSWORD=<sec> python3 mcs/ops/mcs_setup.py init --yes \
    --login-id <ID> --notify-target local \
    --set 'notify.interactive="off"'
```

`notify_target` は検証上の必須キーのため便宜値 `local`（配送しない）。

### 6-3. ローカルLLM（抽出を使う場合のみ — ユーザーに要否を確認）

【ユーザー確認】構造化抽出・semantic を使いますか？（約6GBの
モデルDLが必要）

使う場合は INSTALLATION.md §B-3 の手順を実行。

### 6-4. スケジューリング

【実行】INSTALLATION.md §B-4 の手順で wrapper を
`~/.mcs/scripts/` にレンダリングし、ユーザーの crontab に:

```cron
*/5 * * * * $HOME/.mcs/scripts/mcs_check.sh
7,37 * * * * $HOME/.mcs/scripts/mcs_deep.sh
```

（`mcs_check.sh` には `--no-notify` を付与。夜間間引きは
スクリプト内に組込み済み）

この構成では `mcs_setup services` の既定 wrapper・cmd watcher・Hermes cron を
併用しない。既定の起動経路には `--no-notify` がなく、Hermes がある環境では
通知を送信し得る。既存ジョブの停止・置換が必要な場合は、その対象を確認し、
許可済みの範囲で行う。

### 6-5. 検証

```bash
python3 mcs/ingest/run_check.py --json --download-files --mark-read --no-notify
python3 mcs/views/mcs_view.py status
```

`mcs_setup check` は `hermes CLI not resolvable` エラーを出す —
スタンドアロンでは想定内。それ以外のエラーは対処する。

## Phase 7 — 完了報告

セットアップ完了時に次の形式で報告する:

```text
【MCS セットアップ完了】
- 導入形態: A(hermes アドオン) / B(スタンドアロン)
- 設定: ~/.mcs/config.json（notify_target=…、interactive=…）
- 秘密情報: Keychain mcs-adapter=登録済み / .env=設定済み
- スケジュール: hermes cron=N件 / launchd=N件 / crontab=N件
- 検証: mcs_setup check = exit 0 / 警告N件（内容: …）
- 初回 run: 成功（messages=N, patients=N）/ 失敗（原因: …）
- 通知先: discord:… / slack:… / なし
- 次の注意: hermes_plugin 変更時は `hermes gateway restart` が必要
```

未達項目があれば完了報告の末尾に「残タスク」として列挙する。
