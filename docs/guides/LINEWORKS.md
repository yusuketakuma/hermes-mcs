# LINE WORKS 接続・導入手順

LINE WORKS は、このリポジトリの独自アダプターで接続します。Hermes の
LINE WORKS adapter、追加の Python パッケージは不要です。Slack と同じ
要約・原文・送信対象の添付をトークルームへ配信し、操作の確認は本人との
1:1 トークで行います。通知を有効にすると患者名・本文・添付が設定先へ
送られるため、施設の利用方針と配信対象を先に決めてください。

## 1. 先に本体を導入する

既存 checkout がある場合はそのルートを使います。新規の場合は
[インストールガイド](INSTALLATION.md)の導入形態を選び、本体を設定します。
LINE WORKS の通知には Hermes の通知 plugin・gateway を使いません。
既存の Slack/Discord plugin 設定や gateway を削除する必要もありません。

Hermesアドオン形態（Path A）の場合:

```bash
./install.sh --preflight
./install.sh
PY=~/.hermes/hermes-agent/venv/bin/python
```

Hermesなしの形態（Path B）の場合は、同ガイドのB-1〜B-4で本体・Pythonと
定期実行を設定し、`PY=python3.13`を使います。LINE WORKSの接続手順は共通です。
通知を送る実行には`--no-notify`を付けません。カード・操作を使う場合は
独立アダプターも常駐させます。テキストだけなら常駐は不要です。

設定とdataの既定保存先は本体と共通の `~/.mcs` です。checkoutが別の場所でも
同じ設定を使います。保存先を変更する場合は全コマンドへ `--root <保存先>` を
指定し、本体の保存先とも合わせます。ソースとPythonはcheckout側を使います。

本体の収集・定期実行を設定した後、以下の接続設定を行います。秘密値の
入力はユーザー自身の端末で行い、チャット・argv・ログには貼りません。

## 2. LINE WORKS 管理画面で準備する

1. Developer Console で API アプリを作り、`bot.message` scope、
   Client ID、Client Secret、Service Account、Private Key を用意します。
2. Developer Console で Bot を登録し、Bot ID と Bot Secret を控えます。
   複数人のトークルームへ招待できる Bot ポリシーを有効にします。
3. 管理者画面でも Bot をドメインへ登録し、「サービス中」にします。
4. 配信先トークルームに Bot を招待し、メニューからチャンネル ID を
   確認します。許可するユーザー ID、ドメイン ID、対象 MCS
   プロジェクト ID を確認します。
5. 正規 CA の証明書を持つ公開 HTTPS の Callback URL を設定し、
   message と postback イベントを有効にします。公開 URL は
   reverse proxy からローカルの `127.0.0.1:8788/lineworks/callback`
   へ転送します。自己署名証明書は使えません。

Bot 登録は Console と管理者画面の二段階です。外部 LINE/LINE WORKS
ユーザーを含むトークルームでは Bot を使用できず、500 名以上では
プッシュ通知が動作しません。[公式 Bot 導入](https://developers.worksmobile.com/jp/docs/bot/)

公開 HTTPS・DNS・証明書・reverse proxy の配置は実環境の配備作業です。
ローカルアダプターの導入だけでは公開 URL は作成されません。

## 3. MCS の配送先と操作範囲を設定する

```bash
$PY mcs/ops/mcs_setup.py init
```

ウィザードで `notify.interactive=lineworks` を選択し、通知先を
`lineworks:<トークルームID>` にします。接続項目は次の通りです。

| MCS 設定 | 入れる値 |
|---|---|
| `notify.lineworks.profile` | `default`（独自アダプターの配送識別名） |
| `notify.lineworks.application_id` | Bot ID（正の十進文字列） |
| `notify.lineworks.team_id` | LINE WORKS ドメイン ID（正の十進文字列） |
| `notify.lineworks.channel_id` | 配信先トークルーム ID |
| `notify.lineworks.allowed_user_ids` | 操作を許可するユーザー ID の配列。1 件以上必須 |
| `notify.lineworks.project_ids` | ボタン・1:1トークでの操作を許可する MCS プロジェクト ID の配列。正整数 |
| `notify.lineworks.project_ids_auto` | 初回は `false`。`true` は公開済み scope snapshot から解決する場合のみ |

このプロジェクト範囲はSlack/Discordと共通の操作権限です。自動通知の対象は
本体の収集・通知設定に従うため、配信先トークルームにはその通知対象の閲覧権限が必要です。

対象を捏造したり、許可ユーザーを空にして全員に公開したりしません。
`team_id` は配送基盤の共通キー名で、Slack の workspace ID を入れる
項目ではありません。Bot ID・ドメイン・部屋・ユーザー・プロジェクトの
範囲を変えた場合は、配送先変更として `notify.route_epoch` を増やします。

## 4. 秘密値を入力してローカル診断する

Developer Console で取得した Private Key を所有者のみ読める場所へ置き、
権限を `0600` にします。端末で `openssl version` が動くことを確認します。
OpenSSL は JWT の RS256 署名に使用します。
[公式Service Account認証](https://developers.worksmobile.com/jp/docs/auth-jwt)

```bash
$PY -m lineworks_adapter init
$PY -m lineworks_adapter check
```

`init` は Client ID・Client Secret・Service Account・Bot Secret・
Private Key のパスを端末で受け取ります。秘密値は非表示入力です。
既定の秘密ファイルは `~/.mcs/data/lineworks-credentials.json`、権限は `0600`。
秘密ファイルと Private Key を git に追加しません。`init` は既存ファイルを
上書きしません。診断に失敗した場合は、ファイルの配置・所有者・`0600`権限と
秘密鍵のパスを端末で確認します。認証値を更新する場合は独立プロセスを停止し、
既存ファイルを所有者のみ読める場所へ退避してから `init` → `check` を行います。
退避した秘密値もチャットやログへ表示しません。

`check` はローカルの設定・権限・署名環境を確認する診断であり、LINE WORKS
への送信成功を証明しません。MCS の `init` が秘密ファイル未作成で失敗した
場合は、この手順の後に本体の `check` を再実行します。

## 5. 起動し、接続を確認する

```bash
$PY -m lineworks_adapter run
```

プロセスを動かした状態で、公開 Callback URL の転送設定を確認します。
サービスとして常駐させる場合も、この独自プロセスを起動します。
Hermes gateway の再起動では LINE WORKS adapter は起動しません。

```bash
$PY mcs/ops/mcs_setup.py check
```

実際の送信・Callback 確認は、承認されたテナント・部屋に完全合成の内容を
使って行います。導入確認のために実患者情報を試験送信しません。

### 常駐サービスの候補を作る

設定と `check` が完了した同じ checkout で、次を実行します。

```bash
$PY -m lineworks_adapter service
```

macOS は `~/.mcs/data/lineworks-service/ai.mcs.lineworks.plist`、Linux は
`~/.mcs/data/lineworks-service/hermes-mcs-lineworks.service` を `0600` で生成します。
このコマンドは候補ファイルだけを作り、サービスの登録・起動は行いません。
起動パスは絶対パスで記録されるため、checkout と Python の場所を維持します。
移動した場合は候補を再生成して配置を更新します。秘密値は候補に含めません。

macOS ではまず候補を検証し、サービス導入を行う範囲で配置・起動します。
既存サービスがある場合は現在の配置を確認してから更新します。

```bash
plutil -lint "$HOME/.mcs/data/lineworks-service/ai.mcs.lineworks.plist"
mkdir -p "$HOME/Library/LaunchAgents"
install -m 600 "$HOME/.mcs/data/lineworks-service/ai.mcs.lineworks.plist" "$HOME/Library/LaunchAgents/ai.mcs.lineworks.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/ai.mcs.lineworks.plist"
launchctl print "gui/$(id -u)/ai.mcs.lineworks"
```

Linux の systemd user service では次を使います。収集本体の Linux 動作は
この候補生成とは別の検証範囲です。

```bash
systemd-analyze --user verify "$HOME/.mcs/data/lineworks-service/hermes-mcs-lineworks.service"
mkdir -p "$HOME/.config/systemd/user"
install -m 600 "$HOME/.mcs/data/lineworks-service/hermes-mcs-lineworks.service" "$HOME/.config/systemd/user/hermes-mcs-lineworks.service"
systemctl --user daemon-reload
systemctl --user enable --now hermes-mcs-lineworks.service
systemctl --user status hermes-mcs-lineworks.service
```

ユーザーのログインセッションを超える稼働は OS のサービス運用設定に
従います。`run` の手動プロセスとサービスを同時に起動しません。
起動時の診断ログは `~/.mcs/data/lineworks_state/service.stdout.log` と
`service.stderr.log` に保存します。公開 HTTPS Callback の reverse proxy は
別途維持します。Hermes gateway の導入・再起動で代用しません。

LINE WORKSアダプターを更新した場合は `check` の後に独立プロセスを再起動します。
macOSの導入済みサービスは `launchctl kickstart -k "gui/$(id -u)/ai.mcs.lineworks"`、
Linuxは `systemctl --user restart hermes-mcs-lineworks.service` を使います。
手動起動の場合は既存プロセスを停止してから `run` します。配置先を移動した場合は
サービス候補を再生成・配置してから起動します。

## 配信・操作・障害時の違い

`notify.interactive=off`はカード・ボタン・Callbackの操作を停止し、
設定した宛先への従来テキスト通知は維持します。LINE WORKSの接続設定と
認証ファイルはテキスト配送にも必要です。接続のローカル診断はoffでも行えます。

Slack の同じ情報を LINE WORKS の本文・ボタン・添付へ変換します。
テキストは公式の 2,000 文字上限に合わせて分割します。送信内容は
トークルームの連続投稿となり、Slack のスレッドやモーダルとは異なります。
本文・ボタン本文中のLINE WORKSネイティブメンション（`<m ...>`）は
読み取れる文字へ変換し、記録内容から意図しないメンション通知を発生させません。
[公式テキスト仕様](https://developers.worksmobile.com/jp/docs/bot-send-text)

Bot API には送信済みメッセージの編集・削除・履歴照合・スレッド指定が
定義されていません。更新は新しいカードを投稿し、古いボタンと開いている
本人向けの確認も無効化します。更新・取り下げの送信結果が不明でも、
古い確認画面から確定できません。
送信結果が不明な場合は永続記録に残し、自動再送による二重配信を避けます。
HTTP 201 は API の受理であり、ユーザーの閲覧を示すものではありません。
[公式 Bot API](https://developers.worksmobile.com/jp/docs/bot-api)
・[送信仕様](https://developers.worksmobile.com/jp/docs/bot-channel-message-send)

操作は署名を検証した Callback と許可ユーザー・ドメイン・対象範囲を
照合して受け付けます。本人の確認・理由・receipt を必要とする操作は
その条件を維持します。Callback は失敗しても再送されないため、公開経路と
常駐プロセスの監視が必要です。
[公式署名検証](https://developers.worksmobile.com/jp/docs/bot-callback)
・[イベント応答](https://developers.worksmobile.com/jp/docs/bot-callback-message)

`python -m lineworks_adapter status` は未処理・不明のCallback件数だけを
表示し、入力内容やユーザーIDは出しません。処理中のクラッシュは自動で
再実行せず、不明として保持します。古い未処理・処理中の本文は20分経過後の
稼働tickで削除し、内容を含まない不明記録を残します。不明件数がある場合は
MCS側のreceipt・操作結果を確認してから、必要な操作だけ最新カードから
本人がやり直します。人承認操作の一括再実行はしません。

添付は Bot upload API で取得した URL にアップロード後、file ID で配信します。
URL と file ID の有効期限は 24 時間で、同じ URL への再アップロードは不可。
容量はテナント設定に従い、受信者側の共有ストレージを消費します。
独自アダプターは1ファイル24 MiBまでを受け付けます。管理者設定の上限が
それより小さい場合はそちらが優先されます。上限超過・拒否された添付は
配送未完了として残るため、添付も届いたと見なさず原本と配送結果を確認します。
公式 multipart HTTP 例の `Filedata` と curl 例の `FileData` には表記差が
あります。実環境での受理・容量・管理者制限は接続確認時に検証します。
[添付仕様](https://developers.worksmobile.com/jp/docs/bot-attachment-create)
・[アップロード](https://developers.worksmobile.com/jp/docs/file-upload)

429 の際は同じリクエストをその場で再送せず、次のAPI呼出しを抑止します。
公式の大小文字を区別しない `RateLimit-Reset` を読み、独立CLIと常駐プロセスの
間でも60秒の待機を共有します。同一リソースへの同時書込みを避け、
ドメイン全体の同時接続上限 5 に注意します。
[公式 API 使用上限](https://developers.worksmobile.com/jp/docs/rate-limits)

## 本人トークで全機能を操作する

許可された操作者がBotとの1:1トークで `mcs <JSON>` を送ると、公開snapshotの
閲覧・QC・統計・シグナル・依頼管理・運用承認を利用できます。
共有トークの自由入力はこの入口では処理しません。入力フォームの会話中はフォームを先に完了してください。

```text
mcs {"op":"read","kind":"qc","project_id":1}
mcs {"op":"request","phase":"preview","action":"create","project_id":1,"source_message_id":10,"title":"合成の確認依頼","reason":"本人が原文を確認"}
```

確定には応答のpayload・origin・payload_hashをそのまま含むconfirmを本人が送ります。
[共通の操作形式](../../hermes_plugin/README.md)の `/mcs` を `mcs` に置き換えてください。
同じ本人・通知scopeでの確定、最新snapshotでの参照照合、runnerでの再検証とreceiptを維持します。
Hermesあり・なしで機能は共通です。LINE WORKSの接続はどちらも独立アダプターが所有し、
更新コードの反映にはそのアダプターの再起動が必要です。
