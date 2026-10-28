# Hermesなしで動かす

`runtime_mode=standalone`で、収集・保存・検索・抽出・通知・カード操作・
人承認・定期実行・監視・更新・復旧をhermes-mcsだけで動かします。
既存の設定はモード未指定なら`hermes`を維持します。

## 初めて導入する場合

今回の初回受入候補はmacOS・Apple silicon・Slackのstandaloneです。正式対応や新しいMacでの受入完了を示すものではなく、既存のHermes設定とinstallerの既定値は維持します。
[能力・有効条件と受入の状況](INSTALLATION.md#capabilities-acceptance)は導入ガイドの単一表を参照してください。

[はじめての導入ガイド](EASY_SETUP.md)でMacの準備とリポジトリの取得を済ませ、
`./install.sh`の連携方式で`2) MCS 単独で使う`を選びます。
Hermes Agentと連携する標準の導入は`1)`です。独立実行ではHermesを導入せず、
MCS専用の実行環境がSlack・Discordの接続と定期処理を担当します。
LINE WORKSの通知先は、どちらの方式でも後の初期設定で選びます。

[共通CLI](../../mcs/ops/mcs_cli.py)から導入する場合は、取得したリポジトリのルートで
`sh scripts/mcs install --mode standalone`を使えます。
導入後は`~/.local/bin/mcs setup`で初期設定、`~/.local/bin/mcs doctor`で診断、
`~/.local/bin/mcs update`で対話形式の更新案内を開きます。
既存の`install.sh --mode standalone`、独立venvの`mcs_setup.py`、
`python -m mcs_standalone`による管理手順は下記のとおり利用できます。

## 導入・診断と実機確認の境界

standaloneの外部bootstrap applyはhostの協調停止を駆動できず阻害されるため、
共通入口の提供を外部apply対応の完了と扱いません。

doctorの選択Python/SQLite・recovery・SDK配布metadataは
[ローカル診断](../../mcs/ops/mcs_setup.py)の結果であり、実SDK認証・接続・
常駐プロセスへの反映とは別です。launcherの選択runtime追随、backup lifecycle、
緊急度の後追い通知の統合証拠は確認中です。実機適用と配備の受入条件は変更しません。

## 導入

macOSの前提条件は[インストールガイド](INSTALLATION.md#1-共通の前提条件)を参照します。
リポジトリを取得したルートで、以下を順に実行します。

```bash
./install.sh --mode standalone --preflight
./install.sh --mode standalone
```

installer内で初期設定を完了した場合は再度initを実行せず、下の診断へ進みます。後回し・中断した場合だけ`~/.local/bin/mcs setup`で再開します。従来の直接入口`$HOME/.mcs/venv/bin/python3 mcs/ops/mcs_setup.py init --runtime-mode standalone`も利用できます。

```bash
PY="$HOME/.mcs/venv/bin/python3"
$PY mcs/ops/mcs_setup.py check
$PY -m mcs_standalone status
```

MCSの収集前にはChromeのログインとCDPポート`:9333`も準備します。[導入ガイドの動作確認](INSTALLATION.md#a-5-動作確認)の冒頭を確認し、同節のHermes用Pythonを使う例では上の`$PY`に読み替えます。収集・通知・既読化の確認は、許可した範囲で行います。

installerはHermesを取得・設定せず、独立venvへ[公式SDKの固定バージョン](../../deployment/requirements-standalone.txt)を導入します。
ローカルLLM、独立host、repo外の復旧watchdogも登録します。
ウィザードはMCSのログイン、配信先、操作・閲覧範囲と非表示の秘密入力を案内します。
既存値は保持されます。認証情報不足のまま起動成功と扱いません。

自前のローカルLLMを使う場合は`--no-llm`、配置内容を先に見る場合は`--dry-run`を追加できます。
収集・解析は標準ライブラリのみ、接続SDKは独立入口だけが使用します。

## SlackとDiscordの接続準備

Slackは[Slack接続設定](INSTALLATION.md#付録b-slack-接続設定hermes-agent-リポジトリより転記)のApp作成・権限を使い、
Socket Modeを有効にします。Bot tokenとApp-level tokenを非表示入力で登録します。
付録BのStep 1は`From scratch`を選び、Step 2〜7と9で権限・トークン・ユーザーID・Botの招待を準備します。Step 8のHermes用`.env`設定は使わず、本ガイドの専用ファイルへ登録します。Hermesのmanifest生成・profile設定・gateway起動は使いません。

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
`$PY -m mcs_standalone init --transport slack`または`discord`で認証情報を登録できます。
既存の認証情報がある場合は、保持・変更希望・中止を選べます。変更希望は、古い接続や
送信処理の停止を確認できる仕組みが整うまで保留し、認証情報は変更しません。
`--yes`と端末を使わない実行では、既存の認証情報を保持します。
`.env`に対象の認証情報が一部しかない場合や、読み取れない場合は、新規登録に進まず
管理担当者への確認を案内します。入力中の`.env`の変更を検出した場合も保存を中止します。
入力を非表示にできない端末では、認証情報の入力を中止します。

LINE WORKSは[専用手順](LINEWORKS.md)で認証・公開HTTPS Callbackを設定します。
独立hostがLINE WORKSアダプターも起動するため、別の常駐アダプターと併用しません。

## 常駐・診断

macOSの`ai.mcs.standalone`が、定期6ジョブ、抽出worker2本、cmd/cmd_int取込と接続を所有します。
`backup.enabled=true`の場合は、offsite backupも同じhostの定期ジョブとして追加されます。
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

### 自己回復と外部監視

- Slack/Discordの接続が切れても（トークン失効・障害・起動時のネット断）、hostは止まりません。
  接続だけを30秒から倍々（上限30分）の間隔で再接続し、収集・抽出・定期ジョブは続けます。
  設定・認証情報の変更による終了はすぐ再接続します。hostの全体再起動は設定変更・
  `services`の再起動要求・停止シグナルのときだけです。
- 子プロセス（抽出worker・cmd・定期ジョブ）の異常終了も30秒から倍々（上限30分）で再起動し、
  成功で間隔を戻します。回数は`status`の`exits`に、接続の状態は`connector`に出ます。
- 独立した監視`local.mcs-health`（Linuxは`mcs-health.timer`）をhostの外に置きます。
  `services`が配置し、5分ごとに`health.json`と`standalone-status.json`を読んで、
  hostの停止、同じ子の3回連続失敗、接続の10分超の未回復、2時間超の更新待ち、
  未読収集の成功が途絶えたことを、コードと回数だけの警報で通知先へ送ります。
  `doctor`も同じ内容と監視の配置・稼働を確認します。
- 時刻指定のジョブは、スリープ・再起動・前回実行中で逃した時刻を後で1回だけ実行します
  （`slot_catchup`、実行中で飛ばした時刻は`slot_skipped_running`をログに記録）。
  最後に実行した時刻は`data/standalone-schedule.json`に保存します。
- 再起動待ちは実行中の時刻指定ジョブを最大10分待ち、超えると停止して再起動後に追い付き実行します。
  更新（`update`）とcmd/cmd_intの処理は中断しません。
- 更新markerが残っても、更新処理（`update.lock`の保持者）がなく更新記録も空なら、
  hostは60秒後にmarkerを孤立と判断して消し、処理を再開します（recoveryと同じ判定）。
  更新記録が残る場合は止めたままにし、復旧（macOSはrepo外watchdog、Linuxは
  `mcs_update.py recover`）に任せます。
- hostが強制終了（SIGKILL）された後の再起動では、前回の子プロセスを確認し、
  抽出workerは停止して起動し直し、定期ジョブ・cmdは終了まで待って二重実行を避けます。
- 状態ファイルは内容が変わったときと5秒ごとにだけ書き、ディスク満杯でも
  hostは落ちずにログへ1回記録します。

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

ローカルLLMのモデル（約5.7 GB）の保存先はモードごとに分かれます（Hermes: `~/.hermes/models`、
独立: `~/.mcs/models`）。そのまま切り替えると`install.sh`が新しい保存先へ再ダウンロードし、
元の場所のモデルは自動では削除しません。再ダウンロードを避けるには、`install.sh`の前に移動します。

```bash
mkdir -p ~/.mcs/models
mv ~/.hermes/models/Qwen3.5-9B-Q4_K_M.gguf ~/.mcs/models/
```

`:8080`のLLMを自分で用意する場合は`--no-llm`を付けます。再ダウンロードした場合、
元の場所のモデルは不要になれば手動で削除できます。Hermesへ戻す場合も同様で、移動の向きが逆になります。

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
