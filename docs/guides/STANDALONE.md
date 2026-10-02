# Hermesなしで動かす

`runtime_mode=standalone`で、収集・保存・検索・抽出・通知・カード操作・
人承認・定期実行・監視・更新・復旧をhermes-mcsだけで動かします。
既存の設定はモード未指定なら`hermes`を維持します。

## 導入

macOSの前提条件は[インストールガイド](INSTALLATION.md#1-共通の前提条件)を参照します。
リポジトリを取得したルートで、以下を順に実行します。

```bash
./install.sh --mode standalone --preflight
./install.sh --mode standalone
PY="$HOME/.mcs/venv/bin/python3"
$PY mcs/ops/mcs_setup.py init --runtime-mode standalone
$PY mcs/ops/mcs_setup.py check
$PY -m mcs_standalone status
```

installerはHermesを取得・設定せず、独立venvへ[公式SDKの固定バージョン](../../deployment/requirements-standalone.txt)を導入します。
ローカルLLM、独立host、repo外の復旧watchdogも登録します。
ウィザードはMCSのログイン、配信先、操作・閲覧範囲と非表示の秘密入力を案内します。
既存値は保持されます。認証情報不足のまま起動成功と扱いません。

自前のローカルLLMを使う場合は`--no-llm`、配置内容を先に見る場合は`--dry-run`を追加できます。
収集・解析は標準ライブラリのみ、接続SDKは独立入口だけが使用します。

## SlackとDiscordの接続準備

Slackは[Slack接続設定](INSTALLATION.md#付録b-slack-接続設定hermes-agent-リポジトリより転記)のApp作成・権限を使い、
Socket Modeを有効にします。Bot tokenとApp-level tokenを非表示入力で登録します。
Hermesのmanifest生成・profile設定・gateway起動は使いません。

Discordは[Discord接続設定](INSTALLATION.md#付録a-discord-接続設定hermes-agent-リポジトリより転記)のBot作成・権限を使い、
Bot tokenを非表示入力で登録します。カード・添付・modal・本人確認・`/mcs`は既存の処理契約を使います。
同じBot tokenのHermes接続を同時に起動しないでください。

設定の例は架空の識別子です。実際のworkspace/application/channelに置き換えます。
秘密値は`config.json`へ書きません。

```json
{
  "runtime_mode": "standalone",
  "notify_target": "slack:C0123456789",
  "notify": {
    "interactive": "slack",
    "slack": {
      "profile": "default",
      "application_id": "A0123456789",
      "team_id": "T0123456789",
      "channel_id": "C0123456789",
      "allowed_user_ids": ["U0123456789"],
      "project_ids": [101],
      "project_ids_auto": false
    }
  }
}
```

Discordは`notify.discord`に`application_id`・`guild_id`・`channel_id`と同じ
actor/project範囲を設定します。`allowed_role_ids`は任意で、@everyoneは許可できません。
`project_ids_auto=true`の場合も公開snapshotにあるプロジェクトだけを許可します。
テキスト通知のみで`interactive=off`にしても、配信先のscopeと資格情報は必須です。

秘密情報は`~/.mcs/data/slack-credentials.json`（`bot_token`・`app_token`）または
`discord-credentials.json`（`bot_token`）へ0600で保存します。
既存worktree版のroot配下 `.env`（0600）も互換入力として利用できます。
`init`はその明示ファイルをJSONへ取り込み、既存ファイルは削除しません。
JSONが存在して壊れている場合は `.env` へfallbackせず停止します。
CLIのrun/sendは秘密値をargvで受け取らず、環境やHermes profileから自動取得しません。
再設定は`$PY -m mcs_standalone init --transport slack`または`discord`を使います。

LINE WORKSは[専用手順](LINEWORKS.md)で認証・公開HTTPS Callbackを設定します。
独立hostがLINE WORKSアダプターも起動するため、別の常駐アダプターと併用しません。

## 常駐・診断

macOSの`ai.mcs.standalone`が、6定期ジョブ、抽出worker2本、cmd/cmd_int取込と接続を所有します。
LLMとrepo外の復旧watchdogは別のnative supervisorが所有します。
個々の定期ジョブや抽出workerをcrontab・Hermes cron・LaunchAgentへ重複登録しません。

```bash
$PY mcs/ops/mcs_setup.py services --dry-run
$PY mcs/ops/mcs_setup.py services
$PY mcs/ops/mcs_setup.py doctor
$PY -m mcs_standalone status
```

サービスを使わず端末で動かすなら`$PY -m mcs_standalone run`を使います。
hostは同じdata rootに1つだけ起動できます。本文・秘密値を含まないheartbeatとlive PIDで稼働を判定します。
ログは`~/.mcs/data/standalone.log`、専用venv/scripts/modelsは`~/.mcs/`に置きます。

Linuxでは`$PY -m mcs_standalone service`コマンドがsystemd user serviceの候補を生成します。
`mcs_setup services`はユーザーsystemdへ配置します。Chrome・ローカルLLMなどの
既存macOS向け導入は別途必要です。Linux候補の生成・隔離テストと実機検証は区別します。

## 既存Hermes環境から切り替える

```bash
./install.sh --mode standalone
$HOME/.mcs/venv/bin/python3 mcs/ops/mcs_setup.py init --runtime-mode standalone
$HOME/.mcs/venv/bin/python3 mcs/ops/mcs_setup.py services
```

`services`は以前のmanifestに記録されたMCS所有cron・workerだけを確認して停止します。
停止を確認できなければ、新しいserviceを起動しません。
共有Hermes gatewayは停止しません。旧pluginは独立モードのinboxを処理しませんが、
同じBot認証情報で二重に接続しないようHermes側の該当接続を停止・無効化してください。
手動crontab、manifest外のworker、単体LINE WORKSサービスは別途確認して停止します。

Hermesへ戻す場合は`./install.sh --mode hermes`、HermesのPythonで`init --runtime-mode hermes`と
`services`を実行します。独立サービスが停止したことを確認してからHermesの該当接続を使います。
実データや配送履歴を削除する必要はありません。

## 更新・復旧

既存の承認・理由・receipt、送信不確実性の抑止、添付pin、安全な既読化を維持します。
更新marker中はhostが背景処理を止め、自分のupdaterを維持します。
更新後はdrainer復帰をlive PIDで検証し、hostが処理・ロック解放後に新コードで再起動します。
repo外watchdogはhostが停止した場合も復旧を試みます。

独立モード非対応タグへの更新・ロールバックは変更前に拒否します。
導入前のタグへ戻す必要がある場合、先にHermesモードへ移行してください。
実MCS・実テナント・実LLM・稼働サービスの動作は、隔離したテストの成功だけでは検証済みになりません。
