# 通知partの配送結果を確認する

この手順は、ローカルの運用者が、配送結果が`unknown`になった本文・添付等のpartを個別に確認するためのものです。後述のSlackグループ共有では、準備不足により実行しなかった記録がある`not_sent`も扱います。チャットの操作ツールには公開していません。カード全体の復旧は従来の`control card_resolve`を使います。

管理者は、保存された`data/health.json`の`cards.unknown_parts`で対象の配送IDとpart IDを確認できます。
一覧は最大20件で、保存時点の情報です。載っていない対象を配送済み・0件とは判断せず、
以下の公開snapshotと配送先の証拠で個別に確認してください。health全体には他の運用情報も含まれるため、
外部やチャットへそのまま貼り付けないでください。`mcs doctor`のworker起動・復元保留の表示だけでは、
個々のpartの配送結果は確定しません。

`data/health.json`の`cards.recovery_parts`は、個別の確認が必要なpartを探すための一覧です。
`unknown`のpartに加え、添付の配送先identity確認待ちで`held`のpartと、依存する記録がなく`not_sent`で止まったSlackの`file_group`を含みます。
各項目は`delivery_id`・`part_id`・`kind`・`state`・`transport`・固定の`error_code`・`attempt_id`・`age_s`・`flow`を持ちます。
`status=plannable`は対応する計画が実際に作れること、`status=blocked`は`blocker`の固定コードが計画できない理由を示します。
`flow`は計画に使う系統で、`card_part_resolve`は`card_part_plan`、`attachment_identity_confirm`は`attachment_identity_plan`に対応します。
`total`は一覧化前の候補総数、`truncated`は20件を超えて切り捨てたかどうかです。取消済みのrenderと退役した通知サービスは含みません。
Discordのグループ構成員が共有アンカーへ誘導される場合は`approval_part_id`と`group_approval`を示し、承認はグループ全体に対して行います。
一覧への掲載は承認・送信・配送成功を意味せず、実際の復旧可否は計画と適用時の再照合が決めます。

一覧が20件を超える場合は、公開snapshotに対してページ単位で続きを読めます。

```sh
python3 mcs/views/mcs_view.py --snapshot /absolute/path/ledger-snapshot.db \
  recovery_parts --offset 20 --limit 20
```

`--offset`は読み飛ばす件数、`--limit`は1〜20のページサイズです。並び順は更新時刻・配送ID・part IDで固定され、同じsnapshotではページ間で重複・欠落しません。`total`は全候補数、`truncated`は続きがあるかどうかです。healthの`recovery_parts`は先頭20件だけを示します。

### journalの証拠を事前に確認する

`card_part_check`は、計画が作れるpartについて、稼働中のデータ領域の配送journalだけを読み、証拠の状態を返す読み取り専用コマンドです。snapshotは計画の同一性にだけ使い、journalは`--data-dir`で明示したliveのデータルートから読みます。journalルートをsnapshotや`--cmd-dir`の親から推測することはありません。

```sh
python3 mcs/views/mcs_view.py --snapshot /absolute/path/ledger-snapshot.db \
  card_part_check --delivery-id DELIVERY_UUID --part-id PART_ID \
  --data-dir /absolute/path/to/live-data
```

`journal_check`は`passed`・`blocked`・`unknown`の3値で、証明ごとの結果は`journal_checks`に`passed`・`blocked`・`unknown`で分かれます。`passed`は確認済みのjournal上で少なくとも1つの証明に矛盾がないこと、`blocked`は全ての証明がjournal上の固定理由で拒否されること、`unknown`はjournalが読めない・不完全・読取り上限（32MiB・100000行・5秒）を超えた・対象範囲の行が無い・存在しない場合です。`journal.state`は`clean`・`incomplete`・`unreadable`・`read_limited`・`absent`で、`absent`は対象の配送・part範囲に対応するjournal行が0件だったことを示します。空のjournalや別の配送の行だけでは`passed`になりません。`journal`には対象範囲の行数・phase・開始・配送済み記録の有無など、固定語彙の事実だけを返し、journalの行そのものや例外文は返しません。観測できなかった項目は0やfalseではなく`null`で示し、部分的な証拠は採用しません。`proofs_checked`は照合した証明の一覧、`journal_blocked_reasons`は`blocked`の証明があるときの固定理由、`journal.reason`は読取り上限超過のとき`journal_read_limit`です。`journal_checks`の`passed`はその証明をjournalが否定しないことだけを意味し、`remote_absent`の配送先不在は外部確認が別途必要です。

`passed`でも配送結果・worker停止・人の承認・配送先のidentityは証明されません。それらは`required_external_checks`に固定で示し、適用時に全て再照合されます。`remote_absent`の証明はjournalだけでは配送先の不在を示さないため、外部での確認が別途必要です。heldのpartはこのコマンドの対象外で、`flow`に従い`attachment_identity_plan`側で確認します。計画が作れないpartは`card_part_plan`と同じ固定エラーを返します。

### カード本体が届いていても本文・添付が未完了の場合

送信待ちの一覧が0件でも、最新カードの本文・添付などが未完了の場合があります。
`data/health.json`の`notify.incomplete_parts`は、使用中の通知サービスでカード本体の配送が確認できた計画について、
本文・添付・スレッドの未完了件数と状態別の内訳を示します。
送信結果不明、未送信、保留、送信待ちの部品が残る間は、監視に`notification_parts_incomplete`が表示されます。
通常の送信中の一時的な状態で警報を繰り返さないよう、この未完了だけが原因の場合は、同じ理由を3回連続で観測してから警報を送ります。

古い計画に残る部品も同じカード・通知先・部品・内容の送達を後続の記録で確認できるまで件数に含めます。
カード本体だけを更新しても、古い本文・添付の未完了は解消しません。取消済み・削除済みのカードと、切替前の通知サービスはこの件数に含めません。
`by_plan.current`は、同じカードに、より新しいカード本体の配送成功記録がない計画の未完了部品です。
`by_plan.superseded`は、その記録がある古い計画の未完了部品です。新しい計画が送信待ち・保留・送信結果不明なら分類は変わりません。
どちらも未完了の総数に含み、古い計画に分類されたことだけでは配送済みや対応不要とは判断しません。
取得できない添付も後続の対応部品の送達を確認できなければ未完了に残します。取得不能による未送信と送信結果不明は、状態別の内訳で区別します。取得できないという記録だけでは説明文を利用者へ届けた証拠にしません。
観測が制限時間を超えた場合や記録を読めない場合は、`status=unknown`、件数は`null`となり、同じ監視コードで確認が必要な状態を示します。観測できなかった状態を0件とは判断しません。
この監視は配送記録の確定や再送を行いません。結果が不明な部品は、以下の個別確認と人承認の手順で扱ってください。

## 1. 保存された計画を読む

公開snapshotを指定し、対象の配送IDとpart IDを照合します。

```sh
python3 mcs/views/mcs_view.py --snapshot /absolute/path/ledger-snapshot.db \
  card_part_plan --delivery-id DELIVERY_UUID --part-id PART_ID
```

結果はJSONです。保存された通知先scope、配送ID、part ID、attempt ID、manifestとpayloadのSHA256、期待状態を返します。期待状態は通常`unknown`で、後述のSlackグループ共有だけは`not_sent`も対象です。患者本文や添付内容は返しません。対象なし・復旧対象外の状態・カード本体の場合はエラーと終了コード1になります。計画の表示は承認・enqueue・再送を行いません。

snapshotは保存時点の情報です。現行状態と異なれば適用時に拒否されます。古い計画のhashやattemptを手で書き換えて通してはいけません。

## 2. 人が配送先と証拠を照合する

対象workerを停止したことを確認し、配送先と保存journalを照合します。計画の通知先scopeと、実際に調べた配送先が同じであることを確認してください。

承認用JSONは計画のscope・ID・hash・`expected_state`を保ち、`version`と`cmd`を除いたものを基にします。次の項目を追加します。`human_confirmed`と`project_id`は入力に含めません。

| 項目 | 入力する内容 |
|---|---|
| `command_id` | 今回の操作を識別するUUID。成否不明の同じ操作を再確認する際は同じ値を使用 |
| `actor` | ローカル運用者の識別名 |
| `reason` | このpartの配送結果を確定する理由。患者本文や秘密値を含めない |
| `result` | `mark_delivered` または `mark_not_sent` |
| `remote_id` | `mark_delivered`の場合、照合済みの配送先投稿ID |
| `evidence.method` / `evidence.ref` | 確認方法と、非機密の証拠参照 |
| `evidence.worker_stopped` | 停止を確認した場合に限り`true` |
| `evidence.proof` | `mark_not_sent`の場合、`no_journal_started`・`api_rejected`・`remote_absent`のいずれか。Slack添付説明の更新前拒否には下記の`local_preflight_rejected`、準備不足で実行しなかったグループ共有には`dependency_rejected`を使用 |
| `evidence.duplicate_risk_accepted` | LINE WORKSで`remote_absent`を根拠にする場合、重複リスクを人が受け入れた時だけ`true` |

送信結果が不明というだけで`mark_not_sent`を選ばないでください。既存journalの配送成功記録と矛盾する証拠、読めないjournal、古い計画は既存validatorと適用処理が拒否します。

### Slackの添付説明だけが更新できない場合

添付が表示されていることと、その説明の更新が成功したことは別です。元のアップロードの成功記録を、説明更新の成功証拠として使わないでください。

通知ワーカーの`edit_preflight_held`は、書込み前の照合で止まったことを示します。
`reason`は固定コードだけで、本文・氏名・ファイル名・通知先IDは記録しません。

| reason | 確認する条件 |
|---|---|
| `edit_scope_unverified` | 保存した通知先・スレッド・前回の送信先IDとの対応 |
| `edit_history_unverified` | スレッド履歴を上限内ですべて取得できたか |
| `edit_reference_missing` | 保存した投稿・ファイルIDが履歴にあるか |
| `edit_thread_mismatch` | 対象投稿が保存済みスレッド内にあるか |
| `edit_author_unverified` | 接続時に確認したBot自身の投稿か。ファイルの所有者だけでは確認不可 |
| `edit_file_name_mismatch` | 保存したファイル名と、同じファイルIDの現在の名前が一致するか |
| `edit_file_size_mismatch` | 保存したバイト数と、同じファイルIDの整数の容量が一致するか。差だけで元のupload失敗とは判断しない |
| `edit_file_hash_mismatch` | 保存した内容hashと明示されたremote hashが一致するか |
| `edit_file_mode_unverified` / `edit_file_external_unverified` / `edit_file_editability_unverified` | hashが返らない場合に、hosted・外部ファイルでない・編集不可の条件を確認できるか |
| `edit_file_info_unverified` / `edit_file_info_permission` | 同じファイルIDのメタデータを取得・検証できたか。権限不足を内容一致とは扱わない |
| `edit_target_ambiguous` | 同じファイルを持つ更新先が複数ないか |
| `edit_native_client_unverified` / `edit_timestamp_unverified` | 送信クライアント・対象時刻IDを検証できたか |

このログ単独では配送記録を書き換えません。`edit_target_unverified`または
`edit_history_unverified`で保留されたSlackの添付説明には、
`result=mark_not_sent`と`evidence.proof=local_preflight_rejected`を指定できます。
この場合の「未送信」は説明の編集要求だけを指し、元のファイルを未送信とは扱いません。
`remote_id`を入力して更新先を指定することもできません。

適用処理が、完全なjournalの開始・更新前拒否・receipt、同じ試行のscopeとhash、
元のアップロード成功記録とスレッド・part・ファイル内容の一致を検証します。
別の未確定part、異なる送信結果、途切れた記録がある場合は拒否します。
過去の取消済み計画は、全partが未開始で、開始許可前の撤回をjournalとDBの両方で
確認できる場合だけ除外します。送信許可や開始後の撤回を未送信とは扱いません。
通知ワーカーを停止した証拠と人の承認は、この方法でも必要です。

承認が成立すると、**現在の内容**から既存投稿・添付を更新する計画を作り直します。
保存specが既に削除されていても、旧本文やhashを推測して復元しません。
新規投稿や再アップロードが必要な計画は拒否し、元の保留状態へ戻します。
承認receiptの`replacement_delivery_id`・`replacement_manifest_sha256`と
`content_basis=current_content`が新計画を示します。これは編集成功の証拠ではないため、
ワーカー復帰後に新しい配送receiptで結果を確認してください。

この2種類の添付説明保留は、解決までspecを私有領域に保持します。
その他の終端計画は従来どおり整理し、保留解決後はこの計画も整理対象へ戻ります。

### 同じカードで複数の添付説明が保留された場合

`card_part_plan`に`--include-related`を付けると、同じカードの最新のSlack更新にある
未確定添付を`related_plans`へまとめます。主対象は通常の項目に残り、関連項目は
それぞれのscope・配送ID・part ID・attempt ID・hash・期待状態を保持します。
これはsnapshotの計画表示だけで、journalの証明や人の承認、worker停止確認を代行しません。

`related_plans`は、`mark_not_sent`と`local_preflight_rejected`を使う場合だけ指定できます。
主対象と関連項目の集合が、そのカードの未確定部品すべてに一致する必要があります。
対象漏れ、重複、異なるカード・配送計画、本文など別種の未確定部品がある場合は拒否します。
既存の未開始取消の例外を除き、対象を分割して順番に解除することはできません。

適用時はjournal全体を1回読み、全添付について前節の証跡と元の配送先を検証します。
全件を同じトランザクションで確定し、現在内容の編集計画を1件だけ作ります。
1件でも証拠が不足する、配送先が重複する、再計画が新規投稿・uploadを必要とする場合は、
全対象の確定と再計画を取り消します。成功receiptの`resolved_parts`が全対象のID・hash・
元の配送先を示します。公開途中で止まっても、保存済みの同じ計画だけが通常の復旧で再公開されます。

承認済みJSON全体には既存の16,384 bytes上限が適用されます。上限を超える計画を分割して
通してはいけません。各対象を確認した人の承認とworker停止の証拠は、まとめる場合も必要です。

### Slackで添付の準備不足によりグループ共有を実行しなかった場合

新しい通知計画のカード本体を送信したワーカーが、添付の準備結果を確認できずに
グループ共有を実行しなかった場合、`file_group`を`not_sent`・`stage_receipt_missing`として記録します。
この記録は、グループ共有APIを呼ぶ前の拒否結果です。過去のjournalに記録がないことから推定しません。
結果不明の添付は`unknown`のままです。各添付を配送先の証拠で確認し、通常の個別復旧で確定してください。

全添付の結果が確定しても、添付を準備した記録だけではカードへの共有完了を示しません。
内容が変わっていない場合は、`card_part_plan`で対象の`file_group`を読み、
`expected_state=not_sent`の計画に`result=mark_not_sent`と
`evidence.proof=dependency_rejected`を指定して、運用者が別途承認してください。
`remote_id`と`related_plans`は指定できません。ワーカーの停止確認、理由、人の承認はこの操作でも必要です。

適用処理は、保存されたscope・hash・attemptと完全なjournalの開始・拒否結果・receiptを照合します。
カードとスレッド、全添付の配送結果が確認でき、元のファイルID集合を再利用できる場合だけ、
既存の再計画処理で後継通知を作ります。別のファイルの追加や新規アップロードを含む計画は拒否します。
ローカル原本が削除済み（`pruned`）の添付でも、配送済みのリモートファイルIDと
`sha256`・`bytes`が一致する記録があれば、再計画はそのファイルを新規アップロードせず
既存IDの再利用として扱います。リモート側の実在は従来どおり`files.info`で照合し、
一致を確認できない場合は`unknown`のままです。証明のない削除済みファイルや、
集合が本当に変わった計画は引き続き拒否し、`取得失敗`とは表示しません。
承認の適用や後継計画の発行は共有完了の証拠ではありません。ワーカー復帰後の配信結果を確認してください。

結果不明の添付が残る間は、通常の内容更新も待機します。全添付の配送結果が確定し、
グループ共有を実行しなかった記録がある場合は、内容の変更や新しい通知をきっかけに、
従来の送信条件で通常の更新が進むことがあります。

試行記録が一度もない古い`pending`のグループ共有は、journalがそのpartを開始していないことを
`no_journal_started`で証明できる場合に限り、`expected_state=pending`・`result=mark_not_sent`で解決できます。
この場合もワーカーの停止確認・理由・人の承認は必要で、全添付の配送結果とカード・スレッド・ファイルID集合の
照合は`not_sent`の場合と同じです。journalが失われた・圧縮済みのため開始の有無を照合できないものは
この方法でも復旧できません。保存状態やattemptを手で作らず、元の記録を保持してください。
添付の確認だけを根拠に、過去のグループ共有を未送信とは判断しません。

## 3. 承認済みのJSONをenqueueする

承認用ファイルは運用者だけが読める場所へ0600で保存し、内容を確認した後に実行します。

```sh
python3 mcs/views/mcs_view.py --snapshot /absolute/path/ledger-snapshot.db \
  --cmd-dir /absolute/path/data/cmd \
  control card_part_resolve --confirm-human < /absolute/private/approved-part.json
```

CLIは`version`・`cmd`・`human_confirmed`を付け、既存のcommand inboxへenqueueします。`queued`は受付であり、適用や配送成功の証拠ではありません。worker停止の証拠、理由、LINE WORKSの重複リスク承認等を省略しても通りません。既存のcommand receiptで適用結果を確認してください。

適用時は最新のscope・hash・attempt・状態・journalを再照合します。`mark_not_sent`で計画を作り直す場合も、独立した復元保留や`update_failed`保留は解除しません。既存receiptのチャット向け表示も運用者限定のままです。

`control card_resolve --confirm-human`も引き続きproject指定なしで使えます。どちらの復旧操作もprojectは保存済みrenderから確定し、入力で変更しません。
カード本体の`card_resolve`では、Discordは従来どおり`transport`を省略し、`guild_id`を指定します。
Slackは`transport=slack`と`team_id`、LINE WORKSは`transport=lineworks`と`team_id`を指定します。
CLIが既存契約に従ってDiscord=1・Slack=2・LINE WORKS=3の版を付けるため、
入力JSONへ`version`を追加しないでください。part用の`card_part_resolve`は全接続先で版1のままです。
`card_resolve`の受付時にはsnapshot上のrenderの存在を追加条件にしません。
復元でrenderやattemptが失われた場合も、適用時に既存の復元保留・保存scope・journalを照合します。
受付できたことは、保留の解除や証拠の成立を意味しません。

## 4. 未発行の通知を送らず取り消す（Slack・Discord・LINE WORKS）

送信しないことを運用者が決めた場合は、`mcs/ops/notification_cancel.py`で対象の通知IDを指定します。
対象は、まだrenderへ発行していない更新通知（未発行の対応行が1件だけ）と、全送信試行が
`not_sent`で再送上限に達した集計通知に限ります。既に発行した通知、本文の部分送信記録がある通知、
配送結果不明の通知自体は拒否します。過去の別通知に結果不明の部品があっても、その記録は変更しません。
Slack・Discord・LINE WORKSの通知を扱い、それ以外の送信先は`unsupported_transport`で拒否します。

適用前に、対象通知の送信先ごとに配送側を停止します。停止の確認方法は送信先で異なります。

| 送信先 | 停止するもの | 適用時に確認する停止証跡 |
| --- | --- | --- |
| Slack・Discord | Hermes gatewayの通知ワーカー | `data/worker_status/<送信先>-*.json`の全起動記録が現行版で、記録されたプロセスが終了済み |
| LINE WORKS | 独立アダプター（例: `launchctl bootout "gui/$(id -u)/ai.mcs.lineworks"`、Linuxは`systemctl --user stop hermes-mcs-lineworks.service`） | `data/lineworks_state/send-*.lock`の全送信ロックを取得できる |

LINE WORKSアダプターは起動中ずっと送信ロックを持つため、ロックを取れれば停止済みです。
適用はそのロックを終了まで持ち続けるので、途中でアダプターが起動しても送信を始めません。
送信ロックが1つも無い（一度も起動していない）場合は`worker_stop_unverified`で拒否します。
複数の送信先を含む計画は、すべての送信先の停止証跡が必要です。

```sh
(umask 077
 python3 mcs/ops/notification_cancel.py --root /absolute/path/to/mcs \
   plan --event-id EVENT_ID > /absolute/private/cancel-plan.json)
# 通知ワーカーを正規の手順で停止し、停止を確認してから実行する。
python3 mcs/ops/notification_cancel.py --root /absolute/path/to/mcs apply \
  --plan /absolute/private/cancel-plan.json --command-id COMMAND_UUID \
  --actor OPERATOR --reason '運用者の指示で今回の通知を送らず取消' --confirm-human
```

計画ファイルは0600で保存します。適用は停止証跡・更新/実行ロック・計画と現在状態の一致を確認し、
変更前のDBを検証付きで退避してから、対象通知を取消状態（`suppressed`）へ変更します。
配送済み・未送信という事実の認定は行いません。集計の終端specは削除せず退避し、
DBと`data/notification_cancellations/COMMAND_UUID/receipt.json`に理由・結果を残します。
同じ計画とcommand IDでの再実行は同じreceiptを返し、途中で止まったspec退避だけを再開します。
退避完了を確認してからワーカー（LINE WORKSはアダプター）を戻します。原投稿・配送記録・設定は保持します。

### 送信結果不明のまま残る run_failed 通知の取り下げ

通常の取消は配送結果不明の通知自体を拒否しますが、復元・整合処理のあと
`restore_text_unverified` または `send_outcome_unknown` で保留された
`run_failed`（収集処理の失敗）通知には、証拠を消さない取り下げ経路があります。
対象はテキスト経路・プロジェクト未指定・失敗状態かつ再送予定なし・
カード配送の記録（intent batch・card・render・delivery attempt）が一切無い
通知だけです。payload・progress・attempts・accepted_ref は byte 単位で保持し、
配送済み・未送信の認定は行いません。

```sh
(umask 077
 python3 mcs/ops/notification_cancel.py --root /absolute/path/to/mcs \
   plan --event-id EVENT_ID --action retire_operational_alert > /absolute/private/retire-plan.json)
# update/run の排他ロックと lifecycle marker が空いていることを確認してから実行する。
python3 mcs/ops/notification_cancel.py --root /absolute/path/to/mcs apply \
  --plan /absolute/private/retire-plan.json --command-id COMMAND_UUID \
  --actor OPERATOR --reason '復元後に送信結果が不明なまま残る失敗通知を取り下げ' --confirm-human
```

この経路はカード配送の記録を持たないため送信先 transport が空になり、
通常取消が要求する通知ワーカー・アダプターの停止証跡は不要です。
適用時には update/run の排他ロック・lifecycle marker・復元保留の不在・
計画と現在状態の再照合・変更前 DB の検証付き退避・`--confirm-human` の
承認が引き続き必要です。ただし実際の送信子プロセスの終了は証明しないため、
孤児の送信子が後から配送する可能性は残ります。receipt には
`action: retire_operational_alert`・`delivery_outcome: unknown`・
`residual: orphan_delivery_unproven` が記録され、リモート投稿の取消・
削除・子プロセス停止は行いません。復元後の再照合で同じ保留理由の通知が
新たに見つかった場合も、この経路で個別に確認・適用します。


## OCR警報の運用受付と本文未回収

`output_schema_table_overlap` で自動再試行が3回に達した最新の失敗は、
対象を明示して運用警報を受付できます。受付はOCRの成功、本文回収、
臨床職による原本確認を意味しません。元の失敗と欠落の表示は残します。

1. 非公開の診断結果から対象の失敗artifactを確認します。患者名・本文・
   元ファイル名を操作理由や共有する計画へ記載しないでください。
2. `python3 mcs/ops/ocr_failure_ack.py --root "$HOME/.mcs" plan --artifact-id <対象artifact>`
   で計画を作成し、非公開ファイルに保存します。対象は1回最大20件です。
3. 計画を確認後、`python3 mcs/ops/ocr_failure_ack.py --root "$HOME/.mcs" apply --plan <計画ファイル> --command-id <UUID> --actor <操作者> --reason <受付理由> --confirm-human`
   で適用します。必要な場合だけ `--wait-seconds 600` で実行ロックの空きを待てます。
4. receiptの `ocr_outcome=failed` と `source_coverage=unrecovered` を確認します。
   `receipt_state=pending` はDB保存済み・receiptファイル出力待ちです。
   同じUUIDと同じ計画・理由で再実行し、新しい受付として実行しないでください。

原資料や失敗記録が変わると、古い受付記録を新しい失敗へ流用しません。
受付済みOCR以外の異常・照合不能・未観測部分は警報対象のままです。
同じ表セルの重複が生じる原因や、未回収本文の取得は、この受付では修復しません。
