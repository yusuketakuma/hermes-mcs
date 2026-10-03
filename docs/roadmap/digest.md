# 要約サマリー（日次ダイジェスト刷新）と表示形式の共通化（1.0.11 #31・#32）

改訂日: 2026-10-03。状態: 第1段階・第2段階を実装（2026-10-03）。第2段階の設計は§6。

## 1. 目的と受入

- **#31 要約サマリー**: 既存の日次ダイジェスト（`mcs/notify/notify_digest.py`、平文1通）を、
  見やすいカード型に作り直す。定期配信に加え、許可ユーザーが**いつでも自分専用で**呼び出せ、
  対象患者を絞り込める。
- **#32 表示形式の共通化**: Slack・Discord・LINE WORKS・テキスト（`hermes send`/CLI）の書式差を、
  1つの表示モデルと各チャットの描画関数で吸収する。新しい表示を作るときに各チャット用の整形を書かない。

受入:
- 同じサマリー内容が4経路で崩れずに表示される（各経路の文字数・ブロック数・ボタン上限を超えない。
  超える分は「他N件」に畳み、途中で切れた表示を出さない）。合成テストで4経路の出力を固定する。
- 定期配信は従来どおり1日1回・`notify_target`へ（第1段階はテキスト。カード化は第2段階）。
- 呼出しは本人だけに見える（Slack ephemeral・Discord ephemeral・LINE WORKSは本人DM）。認可は既存の許可ユーザー・
  プロジェクト範囲をそのまま使い、範囲外の患者を出さない。
- 「記録が見つからない≠対応がなかった」の注記と取得欠落の常時表示を維持する。本文・要約文は出さない（件数・ID・患者名のみ）。

## 2. 現状（照合済み）

| 項目 | 現状 | 根拠 |
|---|---|---|
| 日次ダイジェスト | 平文1通。新着（職種別）・緊急度高・取得状況・連携サマリー更新・アラート・滞留・タスク件数 | `notify_digest.build_text` |
| 配信 | outbox `daily_digest` → `notify_flush`がテキストのまま中継。カード経路は`new_messages`/`signal`のみ | `notify_flush.py:330`、`notify_cards.INTERACTIVE_KINDS` |
| 表示モデル | `mcs-card-render/v1`の`parts`（`containers`: heading/text/field/quote/meta、`footer`、`action_rows`）と予算検査 | `adapters/common/spec.py` |
| 各チャット描画 | Slack Block Kit（`slack/cards.render`）、Discord Components V2（`discord/cards.build_view`）、LINE WORKS（`lineworks/cards.render`＋超過分の分割送信）、テキスト（`notify_render.display_text`） | 各`cards.py` |
| 本人専用の返答 | カードのボタン→cmd_int→`notify_views`→本人へephemeral/DM。📋自分のタスク・🗂未確認・🔎検索・🧾患者要約 | `notify_cards._act_view`、`notify_views.py` |
| コマンド | Discordの`/mcs`（JSON、gatewayはsnapshot参照）。Slack・LINE WORKSにコマンドは無い | `hermes_plugin/__init__.py:885`、`adapters/slack/actions.py:139` |
| 患者属性 | `patients.station_name`（MCSのステーション名＝施設）、`messages.organization/profession`、自局判定`signals.self_organizations`、`requests.assignee` | `ledger.py:173`、`mcs_signals._self_sets` |

## 3. 設計

### 3.1 #32 表示の共通化（既存`parts`を唯一の表示モデルにする）

レビューで、カードの描画（Slack Block Kit・Discord Components V2・LINE WORKS＋超過分の分割送信・
テキストの`display_text`）は既に`parts`から種別に依存せず動くことを確認した。足りないのは
「内容を組む側が上限を意識しなくてよい」部分と、本人専用の返答（ephemeral/DM）の書式差の吸収だけ。

1. 規約: 新しい表示は`parts`（`containers`/`footer`）だけで組む。各チャット用の文字列を機能側で作らない。
2. `adapters/common/spec.py`に`fit_parts(parts)`: 予算（`MAX_TOTAL_TEXT`・`MAX_COMPONENTS`）を超える場合、
   後ろの節の行から畳み「…他N件」を付ける。見出し・注記は残す。
3. `adapters/common/text.py`に`parts_text(parts, dialect)`: `discord`（`##`見出し・`-#`補足）、
   `slack`（`*見出し*`）、`plain`（LINE WORKS・CLI。`【見出し】`）。`view_answer`は結果に`parts`があればこれで返す
   （分割は既存`split_body`）。各チャットの返答処理は変更しない。
4. Slackの本人専用返答は、`parts`があれば`slack/cards`の描画をBlock Kitとして添える（テキストはfallback）。
   描画本体は既存`render`からparts部分を`render_parts`として切り出して共用する。

### 3.2 #31 サマリーの内容（カード）

```
📊 MCS サマリー（10-03 08:00 JST・直近24時間）  対象: 担当患者 12人
新着 34件・緊急度高 2件・アラート 5件・未完了タスク 7件（期限切れ 1）
■ 要対応
・緊急度高: 山田 太郎（○○訪問看護）message 123 …
・期限切れ/本日期限タスク: …
・滞留アラート（3日超・未確認）: …
■ 新着（患者別 上位10）  ・山田 太郎 5件（医師2・看護師3） …
■ 連携サマリー更新 / ■ 取得状況（常時表示）
[未確認カード] [自分のタスク]        ← 既存の一覧ボタン（呼出し時のみ）
※ 取得済みの記録から数えた件数です…
```

- 先頭に要約1行（件数）、次に「要対応」（緊急度高・期限切れ/本日期限タスク・滞留アラート）、
  その後に患者別の新着（件数降順・上位10・職種内訳）、連携サマリー更新、取得状況、注記。空の節は出さない
  （取得状況と注記だけは常に出す）。
- 患者名: 定期配信は従来の`daily_digest.include_names`に従う。本人専用の呼出しは既存の一覧表示と同じく
  患者名を出す（本人にだけ見えるため）。どちらもプロジェクト範囲外は出さない。
- 追加集計: 本日期限のタスク件数、未確認カード件数（`notify_views.unacked_view`の件数）。
- `build(db, cfg, since, until, scope, names) -> parts`を1つにし、`build_text`は`display_text(build(...))`に置換。

### 3.3 対象患者の絞込み（フィルター）

| 指定 | 意味 | 根拠データ |
|---|---|---|
| `all`（既定） | プロジェクト範囲の全患者（アーカイブ除く） | 現行と同じ |
| `mine`（担当・記録上） | 押した本人の名前が担当者に一致する未完了タスクがある患者 | `requests.assignee`＋`notify_views.assignee_matches`（📋と同じ照合） |
| `station:<名前>` | `patients.station_name`に部分一致。複数可（OR） | `patients.station_name` |
| `project:<id,…>` | 指定プロジェクト | — |
| `days:<1-7>` | 集計期間（既定1日） | — |

- 種類の異なる指定はAND（例: `mine station:○○ days:3`）。定期配信の既定は設定`daily_digest.scope`（既定`all`、`mine`は不可）。
- 定期配信で`days:`/`日数:`を明示すると、配信時点から指定日数の期間を毎回集計する（期間は重複し得る）。日数未指定時は従来どおり前回配信までの終端から集計する。
- 「担当」は台帳上の記録であり正式な担当割当ではない（正本はzaitaku-calender側）。表示に「担当（記録上）」と書く。
- 自局設定・環境共通の患者リストは利用者個人の担当を表さないため使わない。

### 3.4 呼出し経路（本人専用）

第1段階: 既存カードの一覧ボタン群に「📊 サマリー」（view action `digest`）を追加（追加ボタンの先頭）。
押すと絞込み入力（🔎と同じ入力枠。Slack/Discordはmodal、LINE WORKSはDMの入力待ち）を開き、
空欄なら`all`、`mine`等を入力すると絞り込む。runnerのcmd_intで集計し、既存のcmd_results経路で本人へ返す
（Slack ephemeral・Discord ephemeral・LINE WORKS本人DM）。`_LIVE_VIEWS`に入れ、receiptに本文を残さない。
範囲制限は`projects.view_inputs`の`projects`をrunnerで必ず適用する（返答は本文のためadapter側の行フィルタが効かない）。
CLI: `python mcs/notify/notify_digest.py --print [--names] [filter…]`（名前は明示時だけ）。

第2段階（今回は実装しない）: Slack `/mcs-summary`（Slackアプリ設定と再インストール、Hermes側コマンドとの衝突確認が必要）、
Discord `/mcs`のop追加、LINE WORKSのDM自由入力。いずれも新しい認可入口になるため別に設計する。

### 3.5 定期配信

第1段階は従来の`notify_target`へのテキスト中継のまま、本文を`parts_text(build(...), "plain")`の新しい構成にする。
カード化は`notification_cards.kind`のCHECK制約（`thread/signal/digest`）と既存`digest`種別がシグナル集約に結び付いている
ため、種別追加のテーブル再作成migrationか`op=notice`の完了・再送経路の新設が必要。第2段階として別に設計する。

### 3.6 設定

`daily_digest.scope`（文字列、既定`all`。`mine`は不可）。
`mcs_setup`の型検査・initの質問表へ追加。新しい秘密値・権限設定は無い。

## 4. 検証

- `tests/notify/test_notify_digest.py`の既存17件を新構造で維持（件数・注記・名前のopt-in・1日1回・停止時破棄）。
- 追加: フィルター各種（範囲外を出さない、AND、days上限）、空節の非表示、`fit_parts`の畳み込み、
  4経路の描画スナップショット（合成データ）、Slack/Discord/LINE WORKSの呼出しが本人専用・認可外拒否、

- 実チャットへの送信は行わない。Slack slash commandの実テナント確認は別工程。

## 5. 範囲外

ユーザー別の既定フィルター保存、LLMによる文章要約、通知時刻のユーザー別設定、メンション・スタンプの集計
（1.0.11の#22/#23完了後に同じ`build`へ節を足す）。

## 6. 第2段階の計画（2026-10-03）

### 6.1 定期配信のカード化 — `op=notice`（カード行なしの一回限り投稿）

`notification_cards.kind`のCHECK拡張はテーブル再作成（外部キー有効のDB）になり「migrationは加法のみ」に反し、
旧版への巻戻し時に未知kindが残るため採らない。spec・各workerが既に持つ`op="notice"`（kind/card_key検査なし、
SlackとLINE WORKSは新規投稿と同じ処理、Discordは`single_post`）と、`card_id IS NULL`のrenderを扱える
`notify_transport`の受領処理を使う。

1. `daily_digest`を`INTERACTIVE_KINDS`へ加える。payloadは`text`（停止時のテキスト中継用）と`parts`の両方を凍結する。
2. `dispatch_intent`: kindが`daily_digest`ならカードを作らず、凍結`parts`から`op=notice`のspecを組み
   （`card_key`=`v1|notice|<event_id>`、ボタンなし、`delivery_scope(cfg)`宛、`intent_event_ids=[event_id]`）、
   `card_id=NULL`のrenderとして既存の永続化・spec公開・parts journalに載せる。`card_id`前提の処理（manifest・token・
   intent_cards・thread）は通さない。
3. 完了: notice renderの受領が`delivered`ならspecの`intent_event_ids`のoutboxを`accepted`、`not_sent`なら
   outboxを再試行可能（新しいdelivery_idで再発行、上限`MAX_RESEND`）、`unknown`は既存の保留・人の解決へ。
4. 停止スイッチ（`notify.interactive`オフ・`daily_digest.enabled`オフ）: 未発行なら`revert_to_text`でテキスト中継、
   日次オフなら既存どおり破棄。発行済みnoticeはテキストへ転じない（二重送信防止）。
5. 送信先はカード用の配送先（`delivery_scope`）。`notify_target`ではない。患者名は従来どおり`include_names`。

### 6.2 コマンドからの呼出し

計算は📊と同じ`notify_digest.view`を、公開snapshot（`data/snapshots/ledger-snapshot.db`、DB全体のコピー）に
読取り専用で実行する共通関数`adapters/common/summary.py: answer(snapshot, text, name, allowed, dialect)`に集約する。
入力は`parse_scope`に加え`name:<名前>`（mine用、Discord・LINE WORKSで明示指定）を受ける。

| チャット | 入口 | 認可 | 返答 |
|---|---|---|---|
| Discord | `/mcs {"op":"summary","scope":"mine days:3 name:山田"}`（`hermes_plugin._dispatch`。Hermes・独立の両方） | 既存`_authorize_system`（許可ユーザー・チャンネル） | 既存`/mcs`と同じ返答経路。公開範囲は実機未確認のため患者名を出さず、担当名は`name:`で指定 |
| Slack | `/mcs-summary <scope>`（`app.command`。Hermes同居・独立の両方） | team・app・許可ユーザー（`_scope`と同じ検査） | 既存クライアントの`chat.postEphemeral`（Block Kit、呼出し元チャンネルの本人宛）。Slackアプリにslash command追加が必要 |
| LINE WORKS | Bot DMで「サマリー <scope>」（入力待ちセッションが無いとき） | 既存の許可ユーザー検査・DMのみ | 本人DM（`plain`） |

プロジェクト範囲は各adapterの静的scopeを`allowed`として必ず渡す。snapshotが無い・古い場合はその旨を返す。

### 6.3 検証

notice発行・受領で`accepted`・not_sent再発行・unknown保留・停止スイッチ・旧`text`経路の回帰、
3入口の認可拒否・本人専用・範囲外除外・`name:`、snapshot不在。実チャットへは送らない。
Slack slash commandの実テナント確認とHermes同居時の`/mcs`返答の可視性は実機工程として残す。

### 6.4 計画レビューの反映（2026-10-03）

- 発行は専用`_issue_notice`（renders行は`card_id`/`manifest_id`がNULL、`parts.manifest`はcard＋LINE WORKSの`display#`分割をDB非依存のhelperで封入、公開は既存`_publish_specs`）。
- 完了は`_settle_attempt`のnotice分岐: delivered→outbox`accepted`、not_sent→再試行可能（再入時に`MAX_RESEND`未満なら再発行、超えたら保留。`denied_*`は数えない）、unknown→据置き。
- 再入時: 日次オフ・翌日分の投入・カード機能オフで未送noticeを取消して`suppressed`。送れなかったnoticeのspec本文はgcで消す。
- `maybe_enqueue`はカード配送先だけでも投入できる。旧版への巻戻しでは未封印の当日分が消える旨を更新時の注意に書く。spec`kind`は付けない。
- コマンド: `answer(..., cfg)`（設定は`notify_max_age_h`・`signals`だけをroot`config.json`から読む）。`mine`は全入口で`name:<名前>`必須（Slackは表示名で補完）。
  Slackは平坦なslash payload用の検査、3秒以内のack後に計算、返答は既存の`chat.postEphemeral`（ephemeral）。`response_url`へ別接続を作らない。app manifestはrepoに無いため手順書へslash定義と`commands`scopeを追記。
  `/mcs`の`summary`は人が読む表示テキストを返す（失敗時は従来どおりJSON）。返答の公開範囲が未確認のため患者名は出さない。
- 実装再レビュー反映: 経路（epoch・transport・配送先）が変わった未送noticeは取消して再発行、`resend_exhausted`は即保留、
  テキスト経路でも翌日分があれば送らない、カード停止時にテキスト送信先が無ければ送らない、snapshotの読取り失敗は元データ無しとして返す。
