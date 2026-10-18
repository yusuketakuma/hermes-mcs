# 通知partの配送結果を確認する

この手順は、ローカルの運用者が、配送結果が`unknown`になった本文・添付等のpartを個別に確認するためのものです。チャットの操作ツールには公開していません。カード全体の復旧は従来の`control card_resolve`を使います。

## 1. 保存された計画を読む

公開snapshotを指定し、対象の配送IDとpart IDを照合します。

```sh
python3 mcs/views/mcs_view.py --snapshot /absolute/path/ledger-snapshot.db \
  card_part_plan --delivery-id DELIVERY_UUID --part-id PART_ID
```

結果はJSONです。保存された通知先scope、配送ID、part ID、attempt ID、manifestとpayloadのSHA256、期待状態`unknown`を返します。患者本文や添付内容は返しません。対象なし・対象がunknown以外・カード本体の場合はエラーと終了コード1になります。計画の表示は承認・enqueue・再送を行いません。

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
| `evidence.proof` | `mark_not_sent`の場合、`no_journal_started`・`api_rejected`・`remote_absent`のいずれか |
| `evidence.duplicate_risk_accepted` | LINE WORKSで`remote_absent`を根拠にする場合、重複リスクを人が受け入れた時だけ`true` |

送信結果が不明というだけで`mark_not_sent`を選ばないでください。既存journalの配送成功記録と矛盾する証拠、読めないjournal、古い計画は既存validatorと適用処理が拒否します。

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

## 4. 未発行のSlack通知を送らず取り消す

送信しないことを運用者が決めた場合は、`mcs/ops/notification_cancel.py`で対象の通知IDを指定します。
対象は、まだrenderへ発行していない更新通知（未発行の対応行が1件だけ）と、全送信試行が
`not_sent`で再送上限に達した集計通知に限ります。既に発行した通知、本文の部分送信記録がある通知、
配送結果不明の通知自体は拒否します。過去の別通知に結果不明の部品があっても、その記録は変更しません。

```sh
python3 mcs/ops/notification_cancel.py --root /absolute/path/to/mcs \
  plan --event-id EVENT_ID > /absolute/private/cancel-plan.json
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
退避完了を確認してからワーカーを戻します。原投稿・配送記録・設定は保持します。
