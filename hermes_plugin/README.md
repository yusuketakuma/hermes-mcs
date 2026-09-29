# MCS Hermes plugin（Discord コマンド／Discord・Slack カード）

plugin 名は互換のため `mcs-discord-commands` のまま。`/mcs` コマンドは Discord のみ、
インタラクティブカード worker は Discord（`mcs_discord`）と Slack（`mcs_slack`）の両方を持つ。

既存Hermesのnative Discord受信とallowlistを使う独立plugin。Hermes側には
optional `command_context` とnative入力provenanceの対応が必要。
セットアップで bot token を保存する場合は `hermes config set --stdin` の対応も必要。
未対応版で秘密値を引数に渡すfallbackは行わない。これらの対応を含むHermes版を
用意し、配布時には検証したcommitを固定する。手元の未コミット差分の成功だけで、
既存の固定pinが対応済みとは扱わない。
モデルtoolや任意shellは登録せず、`/mcs <JSON>` だけを登録する。

現状の範囲はstatus、snapshot閲覧、正式依頼と限定運用操作のpreview/confirm、receipt閲覧。
Loop候補の採用も既存requestのpreview/confirmを使う。

## 配置と設定

MCS checkout全体を読める配置で、対象Hermes profileの`plugins/mcs-discord-commands`を
この`hermes_plugin`ディレクトリへのsymlinkにする。pluginだけをコピーすると
隣の`mcs/`を参照できない。既定profile（`~/.hermes`）へのsymlinkと
`hermes plugins enable mcs-discord-commands`は`./install.sh`のstage 3が行う
（checkoutを移動したら新しい場所で再実行。別checkoutを指す既存linkは
`--force-repo`指定時だけ張り替える）。snapshotは公開済みread-only copy、inboxは既存の限定
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
        # 任意: snapshot の patients テーブルにある全 project を
        # 追加で許可 — 新規患者が設定編集なしで機能する
        project_ids_auto: true
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

## システム全体の運用承認（update / rollback / restore）

```text
/mcs {"op":"control","phase":"preview","action":"update_apply","tag":"v1.0.6","target_sha":"<40桁>","base_sha":"<40桁>","reason":"リリースノートを確認し適用"}
/mcs {"op":"control","phase":"preview","action":"update_rollback","reason":"適用後の不具合を確認し戻す"}
/mcs {"op":"control","phase":"preview","action":"restore_approve","report_id":"<通知のreport_id>","backup_sha256":"<通知の値>","backup_schema":7,"reason":"喪失レポートを確認しDB置換を承認"}
```

`update_apply`／`update_rollback`／`restore_approve` は **install 全体**に効く操作で、
`project_id` を持たない。認可は `allowed_user_ids` と `allowed_chat_ids` だけで、
`project_ids` による project 制限は**かからない**（特定 project だけを許可した利用者でも
承認できる）。confirm は他の control と同じ preview → 同一 payload/origin/hash の手順。
`restore_approve` は喪失レポート（`report_id`）とbackupの sha256/schema に束縛され、
別の承認では代用できない。適用・巻戻し・DB置換の実行と結果は runner 側の receipt と
運用通知で確認する。

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
        # 任意: このロールを持つメンバーもカードを操作できる
        # （channel・project の確認はユーザー許可と同じ）
        allowed_role_ids: ["<guild role id>"]
```

カード操作の認可は `allowed_user_ids` **または** `allowed_role_ids`
（押した人の guild ロールのいずれか）＋ `allowed_chat_ids` ＋ project。
`allowed_role_ids` は `/mcs` コマンドと install 全体の承認には効かない。
`mcs_setup.py init --plugin-role-ids 111,222` で書き込める。許可されない
クリックには本人だけに「権限がありません。」を返す（無応答にしない）。

`data_root` には runner が管理する `discord_render/` `discord_state/` `flags/`
`cmd_int/` `cmd_results/` が必要。**この機能は gateway 常駐が前提** — Hermes の
「gateway なしで送信」経路ではカードの配送もボタン応答も動かない。

動作の要点:

- 配送は claim → `transport_begin` → runner の永続 grant → `started` fsync →
  Discord HTTP → `result` fsync → `transport_receipt` の順。`started` より前の
  クラッシュは `not_sent`、以降は `unknown` として記録し、worker は unknown を
  自動再送しない（operator の `card_resolve` で解決）。
  discord.py 2.7 の HTTPClient は1回の送信呼出しの内部で POST も含め
  429・500/502/504/524・接続リセットで最大5回まで再送するため、作成系 POST
  （カード・スレッド作成・本文・添付）は単発に制限する: Hermes の bot が持つ
  aiohttp session の `request` を一度だけ包み、MCS 配送タスクの送信中（ContextVar）
  に限り、直前の POST 応答が 429 以外（5xx・接続リセット等）なら次の POST を
  wire に出す前に `DiscordRetrySuppressed` で止める。初回が確定済みかもしれない
  ので結果は `unknown`（自動再送しない）。429 は Discord が処理せず拒否した応答
  なので SDK の backoff 後の再送はそのまま通す（重複しない）。Hermes 自身の
  送信は ContextVar 外なので従来どおり再試行される。この保護は検証済みの
  discord.py 2.7.1（client の user_agent で判定）と private session 属性に依存し、
  それ以外の版・属性欠落では作成系を送らず `not_sent`/`retry_policy_unknown` で
  止める（Slack の単発 client と同じ fail closed）。編集・削除・取得は対象外。
- journal(`discord_state/journal-*.jsonl`、長寿命 worker は `journal-<id>~<n>.jsonl`
  へ segment 回転)と scope 別 registry(`registry-*.json`)は fsync 永続化。
  再起動時に未レポート結果の receipt 再送と未完 attempt の保守的決済を行う。
  閉じた journal は起動時と回転時に圧縮するが、落とすのは決済済み・unknown 以外・
  claim も未送 part もない attempt で、かつ `data/backups/*.db` の最古 backup より
  1日以上古い行だけ（restore 後の照合証拠を残すため。backup が無ければ圧縮しない。
  `data/backups` 外の複製から restore する運用はこの保証の対象外）。
  scope ごとの fcntl lock で同一配送先の sender は1つ。
- runner と常駐 worker は別々に更新される。worker が知らない spec key
  （新機能）を含む render は、カードだけ送って機能を落とすのではなく丸ごと保留し
  `spec_rejected error=unsupported_*_key` を1回ログする。`hermes gateway restart`
  で新しい worker を読み込むと配送される。
  旧 `registry.json` は保持し、配送 claim・未確定フォームは送信元 scope が
  一致する記録だけ引き継ぐ。scope 情報のない旧 token cache はクリック時に再生成し、
  旧 followup の結果は `/mcs` の receipt 照会で確認する。
- DB 復元後は `data/restore_pending.json` が全 grant を止め、runner の各 tick が
  journal と復元 DB を照合する（`restore_reconcile.json` に結果、held があれば
  ops 通知は新しい hold を記録した回だけ1回）。journal に解析できない行があると
  その file は tainted となり、marker は自動では外れない。復旧手順:
  1. Hermes gateway を停止して worker の追記を止め、該当 `journal-*.jsonl`
     （`discord_state/`・`slack_state/`）を別の場所へ複製保存する（原本の証跡を失わない）。
  2. `restore_reconcile.json` の `held` と壊れた行の前後の `attempt_id` を照合し、
     該当 scope が配送先 channel に投稿済みかを人が確認する。
  3. 壊れた行だけを journal から取り除く（他の行は順序ごと保持）。
     壊れた行が `started`/`result` を隠していた可能性があるため、2 の確認が
     済むまで行わない。
  4. 次の tick で照合が再実行され、tainted が無くなれば marker が外れる。
     残った hold は `ops.card_resolve`（operator 検証済みの rebind/resume）で解除し、
     gateway を起動し直す。
- ボタンは `mcs:a:`、モーダルは `mcs:m:`、確認は `mcs:c:` の custom_id のみを
  処理し、他の interaction は一切応答しない。actor・application・guild・channel
  （modal submit では message も）は各段階で再検証する。
  通知操作の `command_id` は重複適用を防ぐ固定 ID、`request_id` はクリック／
  フォーム送信ごとの応答 ID。過去の結果ファイルを今回の承認や本文閲覧に使わない。
- ボタンは状態表示を兼ねる（`☐ 確認`⇄`✅ 確認済み`、`👤 担当する`⇄
  `👤 担当中`）。ラベル・スタイルは runner が render ごとに決め、同じ操作を
  押し直すと取消・担当解除になる（古い表示での二度押しは吸収）。フッターの
  人名は `<@id>` メンション。Discord はカードの送信・編集・スレッド本文の
  すべてを `allowed_mentions=none` で送るので名前表示のみで通知は鳴らない。
  Slack はメンションを含むフッターだけ mrkdwn にし、`<@U…>` 以外の文字は
  エスケープする（Slack は編集では通知しないが、カードの再投稿時には
  通知されうる）。`🔗 MCSで開く` は token を持たない URL ボタン
  （Slack はクリック通知を ack するだけ）。
- `📝 タスク作成`/`🚫 却下`/`⚠ 抽出の誤りを報告` は runner が返す pin 済み
  params + render context から `request.create` / `ops.signal_dismiss` /
  `ops.extract_feedback` を組み立て、preview → 本人確認
  → enqueue の順で、既存の human_confirmed ゲートを通す。📝 のフォームは
  クリック結果の `form`（抽出済み依頼の下書き・担当者候補）で作る。
  担当者候補は runner の `notify_cards.assignee_choices()`（MCS の自局
  スタッフ一覧 `station_staff_v1`、無ければ自局名で投稿した送信者）で、
  plugin は台帳を読まない。候補があれば選択肢（Discord は Label+Select、
  Slack は static_select）＋手入力欄、無ければ手入力のみ（既定値は押した人の
  表示名）。入力欄の定義は `mcs_delivery/text.py` の `modal_fields()` に
  両 transport 共通でまとめてある。`確定` が command を
  書込み中に押された `取消`／二度目の `確定` は「処理中」と答え、取り消したとは
  報告しない（書込み失敗時は確認が再び有効になる）。
  project scope は `確定` だけを制限する: preview 後に project が scope 外に
  なった確認は `確定` に「権限がありません。」と答えるが、本人の `取消` は
  （何も enqueue しないため）許可 user・channel・card の確認だけで受け付ける。
- `🚫 却下` のフォームは理由コードの選択（`false_positive` / `already_handled` /
  `duplicate` / `out_of_scope` / `other`、表示は 誤検知 / 対応済み / 重複 /
  対象外 / その他）＋任意メモ。`ops.signal_dismiss` に `reason_code` を付け、
  メモが空なら区分名を `reason` にする。更新前に開いたフォーム（自由記述の
  `reason` だけ）は従来どおり `reason_code` なしで送る。
- 4行目の `📋 自分のタスク`・`🗂 未確認一覧`・`🔎 この患者を検索` は view 操作
  （ephemeral 応答のみ、状態を変えない）。runner は `action:"list"` と
  `list`（`title`/`head`/`items[{project_id, group?, text}]`/`more`/`empty`/
  `notes`）を返し、plugin は `items` を `project_ids`（`project_ids_auto`）で
  絞ってから `text.list_messages()` で 15 件＋「他N件」に整形する。runner は
  plugin の project scope を知らないため、この絞り込みは plugin の責務。
  入力を伴うクリックは notification envelope の任意フィールド `input`
  （`{"name"}` = 📋 の押した人の表示名、`{"query"}` = 🔎 のキーワード、各
  120 字以内）で渡し、`command_id` の後半を actor＋input のハッシュにする
  （入力が変わっても前回の receipt と衝突しない）。🔎 はクリックで検索
  モーダルを開き、送信で同じ card token を `input.query` 付きで再送する
  （preview/確認なし）。一覧・検索結果の本文は command receipt に保存しない。
  表示名は Discord では `display_name`、Slack ではペイロードの `user.name`
  （ユーザー名）で、担当者欄との照合は runner の
  `notify_render.assignee_matches()`。

## インタラクティブカード（mcs_slack）

runner の transport が `slack` のとき、Hermes の Slack adapter が持つ native app と
client を使って同じ durable worker（claim/grant/journal/receipt）で配送する。
独自 token・独自接続は持たない。

```yaml
      settings:
        data_root: /path/to/.mcs/data
        slack_adapter_enabled: true
        slack_team_id: "T..."
        slack_application_id: "A..."
        slack_channel_id: "C..."
        slack_profile: mcs             # 省略時は Hermes profile
        slack_allowed_user_ids: ["U..."]
        project_ids: [1]
```

- runner の flags が `interactive: true` かつ `transport: slack` のときだけ起動する。
  状態は `slack_render/`・`slack_state/`、command は Discord と共通の `cmd_int/`。
- workspace は `auth.test` で team を確認してから送る。送信・ephemeral 返信は
  retry handler を外した単発 client を使う。
- Slack adapter が同一プロセス内で再接続し app を作り直した場合、新しい app に
  結線された supervisor が旧 supervisor を止めて scope lock を引き継ぐ。
- 更新 render で同じ添付を再送しないための突合せは、返信の file object の
  name・size・sha256 一致を条件にしている。Slack の file object が sha256 を
  返さない場合は突合せが成立せず、更新時に同じファイルが再 upload され得る
  （安全側。実 API 応答での確認は未実施）。

## 合成入力での検証

MCSから`scripts/run_tests.sh tests/plugin`を実行する。
Hermes対応候補のcheckoutから結合テストを実行する：

```sh
scripts/run_tests.sh /absolute/path/to/mcs/integration/test_hermes_discord.py -q
```

実plugin discovery、native Discord event構築、gateway dispatch、公開snapshot、限定inbox、
原本側drainまでをtempディレクトリ内で通す。実際のDiscord／MCSサービスには接続しない。
