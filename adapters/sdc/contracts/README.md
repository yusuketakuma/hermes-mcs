# CSDC 共有契約の固定コピー

正本は CSDC リポジトリの `contracts/` です。このディレクトリは製品側の実装・互換試験に使う、同じ bytes の固定コピーです。`sources.json` の SHA-256 と受領記録を照合して更新し、コピーだけを手で書き換えて独自契約にしません。

- `fleet/`: 登録成功応答と完全合成の共通例。`pending_expires_at` は中央DB正本の epoch milliseconds。active になった後は履歴値です。
- `support/`: R4a 操作・制御要求・診断結果と、R4b 再起動の独立した schema・公開署名・制御 vector。秘密鍵、認証情報、実端末データは含みません。
- `provision/`: 認証済み管理画面から個別に導入する公開設定と、初回端末登録後のACK・状態照会の固定契約です。
  製品の固定host検証は末尾ドット付き数値hostを拒否します。中央のWHATWG URLが受理する形式でも、署名対象bytesの正規化や接続先の自動採用はしません。ACKの時刻は、保存済みbundleの発行・import期限とcaller側で照合します。
- `diagnostics/`: 独立同意のある固定診断イベントと案件別資料の契約です。R1 heartbeatとR4a診断結果の契約を置き換えません。
- 診断の `generated_at` は組立時刻、`observed_at` は元の観測時刻（根拠なしは null）です。report 外側の `observed_at` は receipt の記録時刻で `result.generated_at` と一致させます。collection の観測時刻を別診断へ流用しません。
- 登録応答は経路に対応する `$defs/claim`、`$defs/status`、`$defs/confirm` を選択して検証します。ルート union は経路ごとの認可・状態検証の代わりにはなりません。
- R4 署名は `SDC-SIGNED-v1`、LF、purpose、LF、kid、LF、復号した CJSON bytes（末尾LFなし）です。envelope の payload はその base64url 表現です。以前の草案の表現と混同しません。

中央APIの合成427件合格は提供結果として受領しています。契約の固定はMac署名検証、実機受入、顧客有効化の完了を意味しません。R1 heartbeat の通信・同意を遠隔支援の許可へ流用しません。

追加契約は初回支援同意の署名preview・同意receipt登録・個別承認record付きpoll・本文を再送しない結果照会です。`device_epoch` は操作/承認/grantすべて必須で、旧vectorへのfallbackはありません。previewの診断ID/担当者refはASCII昇順で一意、本人同意の代わりに中央の予定epochを採用しません。case-level `local_receipt_id` と操作ごとの `owner_generation` は別です。

既知の未実行admitも、中央に保存された同一scope・receiptの照合対象です。report:nullだけでは未実行確定とせず、端末の永続未開始記録と中央の取消・期限切れを合わせて判断します。照会から新grantや再実行を作りません。

## 再起動の独立契約部品（R4b）

`restart-wire.schema.json`・`restart-wire-vectors.json`・`restart-signature-vectors.json` は、別の個別変更同意に束縛する契約部品です。参照先の `restart-plan.schema.json` も中央正本と同じbytesで同梱します。当初の純契約合成465件に続き、中央API・現在権限のDB照合・Consoleを含む合成523件成功を受領しています。再起動の準備と同意のCLIは開発候補にありますが、遠隔再起動の実行と実機受入は未完です。既存R4aの9ファイルと診断の署名用途・経路は変更しません。

- intent→個別承認→確認したpreview全体のhash→ローカル変更receipt→accept→operationの順に束縛します。5分のpreviewは初回accept窓、grantは30秒以内の開始窓、追跡期限は初回acceptから15分で再送しても延びません。
- 署名の新3用途は独立したpin集合を要求します。履歴照合で期限切れの署名を検証できても、現在の権限や新しい実行許可にはなりません。
- 期待対象15項目は信頼済み登録・ローカル同意から渡します。受信payloadから期待値を作る自己照合では、別端末・別世代を防げません。
- 正常な署名、保存済みSPENT、別runtime世代の出現だけでは再起動成功としません。既存supervisorによる実終了と起動の因果証拠・同じlaunch IDの後継確認がない場合はunknownです。

製品側validatorは型・hash・レコード間の一致だけを検証します。Runtime自身の同一生存処理によるgrant取得、受付停止、更新との競合制御、永続化後の再検証と実drainは別工程です。製品の未完成な実行経路を推測で接続しません。

## 再起動APIの固定コピー

`restart-api.schema.json`・`restart-api-vectors.json` は中央の8種類の端末POSTと管理者用APIを記述します。この契約から製品が使うのは端末用8経路だけです。OpenAPIは相対schema参照を維持するためコピーせず、中央の正本 `docs/api/restart-openapi.yaml` のhashを `sources.json` に記録しています。R1と既存R4aの通信許可は広げません。

HTTP応答の `request_id` は要求の追跡値で、承認や実行証拠ではありません。accept応答は専用の不変ACK、pollは4レコードが全て存在するか全てnull、grantはnullを成功応答にしません。statusの `report:null` を未開始の証拠にせず、保存済みreceiptとの照合を続けます。

同一ID・同じbytesの再照会は新しい許可を作りません。元preflightやACKが過去になったことと、いま操作開始できることを分けて検証します。現在権限・期限・同意のCASは中央、SPENT・最終再検査・実drainと終了確認は製品所有者が担います。中央523件はSQLite adapterによる合成結果で、実D1、HTTP、Macサービスや鍵配布の受入を示しません。

## 初回登録基線の読取り

`restart-registration.schema.json`・`restart-registration-vectors.json` は追加の `POST /v2/support/restarts/services/read` だけを記述します。既存14 JSONは変更せず、追加OpenAPI `docs/api/restart-registration-openapi.yaml` はコピーせずに出典とhashを `sources.json` へ記録しています。中央555件成功は提供された合成結果です。

要求は専用の `sdc.support.restart-registration-read.v1` と、保存済みR4a案件の `case_id`・`local_receipt_id` だけです。応答は専用版名、同じ2 ID、現在有効なactive registration、HTTPの `request_id` に限ります。端末・tenant・revision・origin・pin・queryを要求へ追加せず、返却されたoriginやpinを採用する経路も作りません。既存8ルートの登録履歴照合では期限切れを許す挙動を維持し、この新読取りでは期限切れ・未来・revoked・nullを拒否します。

製品の契約validatorとrestart専用HMAC・transportはこの固定経路を扱います。取得は、真正に設定済みの固定originへのTLS接続と既存認証を前提とし、新しい署名用途を追加しません。後続callerは取得の本人同意、保存済みR4a案件基線・現在のmonitor binding・ローカルserviceとの独立照合を行い、初回の `service_revision` だけを登録応答から採用します。値を1と推測したり、既知scope/epoch/keyを応答で自動移行したり、previewからexpected15を埋めたりしません。R4a同意をplanning・変更許可へ流用せず、それぞれ別の同意が必要です。

同じ本文でも新しい読取りで、中央の成功枠は端末ごとUTC日60回です。明示的な初回準備だけで使い、定期pollへ接続しません。ACK不明時は同じ本文と新しいHMAC nonce/timeで初回に加えて最大3回、期限・取消・409で停止する中央契約です。開発候補には登録読取りの再試行、初回基線の保存、準備・同意のCLIがあります。遠隔再起動の実行、実機受入、真正origin/pinの運用配布は完了していません。

## 初回公開設定と診断イベントの追加契約

中央の固定パケット `c6e6537a` に従い、4つのJSONを同じbytesで保持します。中央の152ソースのhashと旧32ソースの不変性を照合しています。中央の合成660件成功は提供された結果で、製品の検証・実配布・実機受入の成功件数ではありません。OpenAPIは中央の相対参照を保ち、出典とhashだけを `sources.json` に記録します。

初回設定のbundleは、独立に把握した認証済みConsoleのorigin、表示されたexact hash、対象・root・scopeとの照合を必要とします。JSON自身の自己申告や構文検査、HTTPS形式だけでは真正な接続先・pinの証明になりません。root IDは私有領域のUUIDv4で、パスや物理端末の証明ではありません。初回monitorでは未発行deviceを要求せず、ローカルのimport記録と登録後のdevice ACKを別に扱います。取得済みstatusから新しいtrustを適用したり、失効した世代を復活させたりしません。

診断イベントは固定enumと件数・10分単位の時刻だけです。新規送信には独立した現地同意と中央同意を必要とし、患者情報・秘密・path・URL・例外文・生ログを収集しません。新規データの鮮度検証と、保存済み同ID・同bytesの履歴照合は別です。中央の `unknown` は未送信の証明ではなく、内容の期限切れも再実行の許可にはなりません。

契約validatorだけでは、送信や適用の許可を作りません。monitor・support・restartのprofile取り込みは `../provision.py` と `mcs/ops/sdc_provision_cli.py` の開発候補に接続しています。診断の独立同意・私有spool・既存R1 tickでの条件付き送信も開発候補にあります。手順は[状態監視ガイド](../../../docs/guides/SDC_SELF_MONITORING.md)を正本とします。1.1.0の新規導入ではCSDCは無効で、接続して提供しません。実actor/channel/pin配布・外部の復元基線・実observer sink・実D1/Macの受入がない状態を、顧客有効化済みとは扱いません。R1・既存R4の経路、署名用途、中央grant CAS以後最大30秒のin-flight契約は変更しません。

`sources.json` の該当する `status` と `product_runtime_connected` は、中央契約の受領と提供済み製品の接続状態を記録します。`development_candidate` は現在の開発候補を示し、中央・実機の受入や有効化を意味しません。

## releases

`releases/` の既存6ファイルは、CSDCのR5a/R5b公開契約と同一bytesです。出典とSHA-256は`sources.json`のrelease_apply_component/filesに記録します。完全合成の公開鍵・vectorだけを含み、秘密鍵・実設定は含みません。Pythonの契約・署名互換性だけを検証し、製品owner接続・実機の信頼基線・有効化・再接続成功は未受入です。中央の1062件は提供された中央合成検証の件数で、製品試験数ではありません。

### release登録の現在証明

`releases/registration.schema.json` と `registration-vectors.json` は、中央の追加契約を同じbytesで保持します。旧20契約と既存release用6契約のbytesは維持します。追加2契約の出典とhashは `sources.json` に記録します。

専用経路 `POST /v2/releases/apply/registration/current` は、登録ID、受入済み実装hash、現在の失効・期限・変更状態を確認するためのものです。専用purpose `release-apply-registration-current` の署名、独立に保持した公開pins、毎回生成する照合IDと実端末scopeを検証します。有効期間は最大30秒です。`expired` は期限切れと未発効の両方を含みます。monitor profileの世代更新で登録証拠hashが変わった場合は、中央登録との再照合・再投入が必要です。失効状態を瞬時に検知できる保証や、更新の実行許可にはなりません。実登録、真正な鍵配布、有効化、更新factoryへの接続は未完了です。
