# MCS Discord コマンド

既存Hermesのnative Discord受信とallowlistを使う独立plugin。Hermes側には
optional `command_context` とnative入力provenanceの対応が必要。
モデルtoolや任意shellは登録せず、`/mcs <JSON>` だけを登録する。

現状の範囲はstatus、snapshot閲覧、正式依頼と限定運用操作のpreview/confirm、receipt閲覧。
Loop候補の採用も既存requestのpreview/confirmを使う。

## 配置と設定

MCS checkout全体を読める配置で、対象Hermes profileの`plugins/mcs-discord-commands`を
この`hermes_plugin`ディレクトリへのsymlinkにする。pluginだけをコピーすると
隣の`adapter`を参照できない。snapshotは公開済みread-only copy、inboxは既存の限定
書込ディレクトリを指定する。原本DB・backup・credentialsは公開しない。

設定例（値は合成値。実際の許可scopeへ置き換える）：

```yaml
plugins:
  enabled: [mcs-discord-commands]
  entries:
    mcs-discord-commands:
      settings:
        snapshot: /mount/mcs/snapshots/ledger-snapshot.db
        inbox: /mount/mcs/cmd
        allowed_user_ids: ["123456789"]
        allowed_chat_ids: ["987654321"]
        project_ids: [1]
```

設定は呼出しごとに読み直す。全scope必須、未設定は拒否。
Hermesの既存allowlistによる現在の認可も毎回必要。bot、internal、relay、転送本文、
添付由来本文、書換え／結合済み／recovery入力は承認に使えない。
設定・配置の稼働環境への適用とDiscordでの実送信は、本実装の検証では行っていない。

## 操作

```text
/mcs {"op":"status","project_id":1}
/mcs {"op":"read","kind":"requests","project_id":1}
/mcs {"op":"request","phase":"preview","action":"create","project_id":1,"source_message_id":10,"title":"確認した依頼内容","reason":"原文を確認し対応が必要と判断"}
/mcs {"op":"request","phase":"preview","action":"update","project_id":1,"request_id":3,"patch":{"status":"done"},"reason":"実施結果を確認"}
```

previewは書込みなし。応答の`payload`（操作者、原文hash、更新時のrevisionを含む）、
`origin`（user/chat/scope/profile）、`payload_hash`を確認する。
人が同じ送信元scopeから、これらをそのまま含む次のJSONを送る：

```json
{"op":"request","phase":"confirm","payload":{},"origin":{},"payload_hash":"previewで返った値"}
```

空のobjectは説明用で、実際にはpreviewのobjectを入れる。
confirm時は認可・scopeと最新snapshotの原文hash／revisionを再確認してinboxへenqueueする。
`queued`は正式依頼への適用完了ではない。既存MCS runnerが原本の現在状態を再確認し、
command ID/hashで重複を防ぎ、依頼とreceiptを同一transactionで保存する。

receipt照会にはconfirm応答の **`receipt.command_id`と`receipt.payload_hash`** を使う。
previewの`payload_hash`はoriginも含む確認用hashで、receipt用hashとは異なる。
新snapshotにreceiptが反映されるまでは未処理／未反映として表示される。

```text
/mcs {"op":"read","kind":"receipt","project_id":1,"command_id":"receiptのID","payload_hash":"receiptのhash"}
```

この確認は明示的なnative人手操作を受け取るもので、previewを読んだことの暗号学的証明ではない。
CCOの自然文やモデル出力だけでは確定できない。正式依頼の自動作成・自動完了はしない。

## Loop候補の採用

`read`の`kind:"loops"`で候補の原文根拠を確認し、同じ薬剤・対象行為・期間かを人が判断する。
採用するrequestのpreviewに`loop_artifact_id`と`loop_match_confirmed:true`を加える。

```text
/mcs {"op":"request","phase":"preview","action":"create","project_id":1,"source_message_id":10,"title":"確認した依頼内容","reason":"薬剤・行為・期間が一致する候補を採用","loop_artifact_id":25,"loop_match_confirmed":true}
```

更新も同じ指定を使える。候補とrequestの起点messageが一致する必要がある。
preview／confirm／原本適用時に原文・スレッド指紋・設定世代を照合し、古い候補を拒否する。
引用のmessage/revision・参照ID・文字範囲・原文一致も検査する。
根拠不足の候補は閲覧できるが`adoption_eligible:false`となり、正式採用はできない。
採用リンクとrequestとreceiptは同一transactionで保存する。
`loops`の`linked_requests`と`requests`の`loop_links`から関連を表示し、正式状態はrequest行から読む。
新しい候補世代へ既存リンクを自動移行せず、必要なら改めて人が確認する。

## 限定運用操作

assist要約の比較・採用は次の操作を使う。

```text
/mcs {"op":"read","kind":"comparison","project_id":1,"message_id":10}
/mcs {"op":"control","phase":"preview","action":"adopt_summary","project_id":1,"message_id":10,"reason":"原文と両要約を比較して候補を採用"}
```

既存の`extract_llm`要約と候補の本文差分、根拠世代、採用可能性を表示する。
採用には現行原文に対応する旧要約と、assistで作成した現行のPASS候補が必要。
返信を含むスレッドの取得が未完了の場合も採用できない。
旧要約が未作成・古い・失敗の場合は比較不能を明示し、先に既存経路の処理を待つ。
preview応答のpayload/origin/hashを通常のcontrol confirmで返すと既存inboxへ投入する。
原本適用時にも比較hashを確認し、採用者・理由・候補IDをreceiptとartifactへ同時記録する。
比較表示の`candidate.adopted`は今の比較内容に対する採用記録の有無を示す。
原文や旧要約の変更で以前の採用はcurrentではなくなる。過去の採用記録は保持する。
この操作は自動通知、原文ACK、正式依頼、既存要約artifactを書き換えない。

```text
/mcs {"op":"read","kind":"operations","project_id":1}
/mcs {"op":"control","phase":"preview","action":"scan","project_id":1,"days":14,"pages":10}
/mcs {"op":"control","phase":"preview","action":"retry","project_id":1,"job_id":3}
/mcs {"op":"control","phase":"preview","action":"retry","project_id":1,"job_id":3,"additional_attempts":2,"reason":"障害原因を解消し追加2試行を許可"}
/mcs {"op":"control","phase":"preview","action":"pause","project_id":1,"feature":"semantic"}
/mcs {"op":"control","phase":"preview","action":"resume","project_id":1,"feature":"semantic"}
```

preview応答のpayload/origin/hashを確認し、`op:"control", phase:"confirm"`で同じ値を送る。
正式依頼と同じinbox、認証、scope、receiptを使用する。

- scanは既存projectへの有限の履歴取得要求。daysは1〜365、pagesは1〜40。
  既存の履歴walkのcursorと累積attemptを保持して深掘りする。取得済みfloor内なら拒否される。
  MCSへの投稿や既読方針の変更は行わない。
- retryは選択したsemantic jobだけ。snapshotのpayload hashを確定時と原本側で再確認し、
  累積attemptsを保持する。通常は6回上限で停止し、doneは再開しない。repair予約も消さない。
  上限到達後は原因を確認し、`additional_attempts:1〜3`と`reason`を明示して追加を許可できる。
  previewの`retry_budget`で累積回数と変更前後の上限を確認する。追加数はジョブ試行数であり、
  HTTP要求数ではない。日次要求・実行時間の予算は維持する。同じcommandの再送で枠は増えない。
  同入力の再seed／再起動では上限を増やさず、原文世代が変わった場合も旧追加枠は引き継がない。
- pause/resumeは指定projectの意味機能全体が対象。**queuedは停止完了ではない**。
  MCS runnerがinboxを処理しreceiptがappliedになってから有効になる。
  適用後は次のJev/local LLM要求、結果昇格、semantic配送を止める。
  意味機能ONではpause中の新着も原文とpending jobを同一Txに保存し、resume後に再取得なしで処理する。
  原文取得・既存raw通知・job・receiptは保持する。実行中の外部要求を遡って取消しはしない。
  resumeは現在設定のmode/対象/予算を変更せず、残っているpending jobだけを再開する。
  停止中の過去全履歴を自動seedしない。破損したjobは上書きせず保持する。

既存CLIにも`operations --project ID`と
`control {scan,retry,pause,resume,adopt_summary} --project ID --confirm-human`を追加した。
controlはJSONをstdinから読み、既存requests CLIと同様にローカル操作者の明示確認を前提とする。
Discord利用時はこのCLIのactor自己申告ではなく、上記native platform認証を使う。

## インタラクティブカード（mcs_discord）

`notify.interactive: discord` を有効にした runner が発行するカード render spec を、
常駐 gateway の Discord Bot が配送・更新・削除し、ボタン／モーダル操作を
`data/cmd_int` 経由で runner に返す worker。`/mcs` コマンドとは独立に登録され、
SDK や設定がなくても `/mcs` 側は従来どおり動く。

```yaml
      settings:
        interactive: true            # card worker を有効化
        data_root: /path/to/.mcs/data
        profile: mcs                 # runner 側 discord scope と一致させる
        application_id: "<discord app id>"
        channel_id: "<配送先 channel id>"
        guild_id: "<guild id>"       # scope が guild を pin する場合
```

`data_root` には runner が管理する `discord_render/` `discord_state/` `flags/`
`cmd_int/` `cmd_results/` が必要。**この機能は gateway 常駐が前提** — Hermes の
「gateway なしで送信」経路ではカードの配送もボタン応答も動かない。

動作の要点:

- 配送は claim → `transport_begin` → runner の永続 grant → `started` fsync →
  Discord HTTP → `result` fsync → `transport_receipt` の順。`started` より前の
  クラッシュは `not_sent`、以降は `unknown` として記録し、unknown は自動再送しない
  （operator の `card_resolve` で解決）。
- journal(`discord_state/journal-*.jsonl`)と scope 別 registry(`registry-*.json`)は
  fsync 永続化。再起動時に未レポート結果の receipt 再送と未完 attempt の
  保守的決済を行う。scope ごとの fcntl lock で同一配送先の sender は1つ。
  旧 `registry.json` は保持し、配送 claim・未確定フォームは送信元 scope が
  一致する記録だけ引き継ぐ。scope 情報のない旧 token cache はクリック時に再生成し、
  旧 followup の結果は `/mcs` の receipt 照会で確認する。
- ボタンは `mcs:a:`、モーダルは `mcs:m:`、確認は `mcs:c:` の custom_id のみを
  処理し、他の interaction は一切応答しない。actor・application・guild・channel
  （modal submit では message も）は各段階で再検証する。
  通知操作の `command_id` は重複適用を防ぐ固定 ID、`request_id` はクリック／
  フォーム送信ごとの応答 ID。過去の結果ファイルを今回の承認や本文閲覧に使わない。
- `依頼`/`却下` は runner が返す pin 済み params + render context から
  `request.create` / `ops.signal_dismiss` を組み立て、preview → 本人確認
  → enqueue の順で、既存の human_confirmed ゲートを通す。

## 合成入力での検証

MCSから`scripts/run_tests.sh tests/plugin`を実行する。
Hermes対応候補のcheckoutから結合テストを実行する：

```sh
scripts/run_tests.sh /absolute/path/to/mcs/integration/test_hermes_discord.py -q
```

実plugin discovery、native Discord event構築、gateway dispatch、公開snapshot、限定inbox、
原本側drainまでをtempディレクトリ内で通す。実際のDiscord／MCSサービスには接続しない。
