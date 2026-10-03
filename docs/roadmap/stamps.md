# MCSスタンプの取得・活用計画（#22、1.0.11〜1.0.13。押下は#28、1.1.0）

調査日: 2026-10-02。基準: v1.0.10 / `9ea015d`。
状態: 公開JavaScriptの静的調査とローカルソース照合まで完了。同日のレビューで、投稿オブジェクトに
反応情報が同梱されることと再取得経路を確認し、取得設計を3層に改めた。実API・実投稿・本番DBは未確認。
本書は実装・公開・実データ検証の完了を宣言するものではない。

## 1. 確認した取得契約

公式サイトの公開アセットを認証なしで取得し、実行せずに読んだ。
以下はクライアントの挙動であり、サーバ側の保証とは区別する。

### 投稿オブジェクトの反応情報（第0層の根拠）

タイムラインの各メッセージは`reactions[]`を持ち、要素は`{type, count, self_reacted}`。
公式画面はこの3項目だけでスタンプの件数と「自分が押したか」を描画する
（`chunk-IGUQKMKD.js` 約559178・約132900）。`viewed`は要素が無くても件数0として補われる。
初回取得は`include_meta`付きだが、2ページ目以降・1件再取得・ポーリングでは`include_meta`が無く、
それでも`reactions`をnullガードなしで参照する。サーバが常に返すかは22-Aで確認する。
`count.reactions`・`is_reacted`は相談機能の項目で、スタンプとは無関係。

スタンプ押下後、公式画面は楽観更新をせず、その投稿を
`GET /projects/{pid}/messages?message_id={id}&per_page=1`で再取得して`reactions / is_bookmarked / count / delete_user`を
上書きする（約12772）。スレッド画面は`GET /messages?message_ids=…`で親投稿を一括取得する（約16811）。
10秒ポーリングは新着IDだけを見るため、既存投稿の反応はポーリングで更新されない。

### 押下者一覧（第2層の根拠）

| 用途 | GET経路（公式クライアント） | 一覧のキー・条件 |
|---|---|---|
| 全種類の押下一覧 | `/api/v2t/messages/{message_id}/user_reactions` | `reactions`。各行の`reaction_type`と`user`を読む |
| 種類を指定した押下者一覧 | `/api/v2t/messages/{message_id}/reactions` | `users`。`reaction_type`を指定する |

両画面は`per_page=50`で、共通ページャは`page`を1から進め、`has_next`を優先し無ければ`total_pages`を使う。
初回だけ`include_meta`を要求し、応答の`message.reactions`から種類別件数を表示する。
`include_paginate_totals`はfalse。初回取得後は`include_meta`が自動で外れる。
一覧の呼出しは`reaction_type / timestamp / include_meta / include_paginate_totals / page / per_page`だけを送り、
`keep_read_status`・`no_extend_session`は付けない。既読やセッションへの影響は未確認。
行で参照されるuser項目は`id, type, is_anonymous, 氏名, medium_icon_url, is_confirmed, is_authenticated,
specialist_categories[0].name, stations[0].name`。押下時刻の項目は無く、`reacted_at`類は221本のどこにも無い。
横断の反応取得API・通知フィード・既読者一覧APIは無い。

### 種別と意味の境界

| コード | 公式画面の名称 | 本システムで示す事実 |
|---|---|---|
| `viewed` | 見ました | 「見ました」スタンプが観測された |
| `accepted` | 承知 | 「承知」スタンプが観測された |
| `thanked` | 感謝 | 「感謝」スタンプが観測された |
| `good` | いいね | 「いいね」スタンプが観測された |
| `completed` | 完了 | 「完了」スタンプが観測された |

`all`は画面の一覧タブであり保存する種別ではない。未知の将来種別は黙って捨てず「未知」として保持する。
`viewed`と投稿の`is_unread`・既読化receiptは別の状態として扱う。
`accepted`は担当引受の承認、`completed`は正式なタスク完了を保証しない。
押下者が担当者かはスタンプ単体から推定しない。押下していない人を未読・未対応・怠慢と判定しない。

### 更新・取消・時刻

押下は`POST /messages/{id}/reactions`（`reaction_type`）、取消は`POST /messages/{id}/reactions/delete`（種別なし）。
1.0.xではどちらも実装しない。公式画面は`self_reacted`の種別を押すと取消を送る。
押下・取消の時刻、1人が複数種類を保持できるか、取消後の再押下は未確認。
一覧の`paginate.timestamp`や取得時刻を押下時刻として記録しない。
検出周期の間に押して取り消された操作は復元できない。

## 2. 公開ソースの根拠

ハッシュ付きアセットは更新・削除され得る。再調査時はhashと関数を照合する。
転載したJSや実APIの生応答をrepoへ保存しない。offsetは取得文字列の0始まり位置の目安。
2026-10-02のレビューで下記5本の現存とSHA-256の一致を再確認した。

| 公開ソース | 確認箇所 | SHA-256 |
|---|---|---|
| [取得サービス](https://www.medical-care.net/chunk-TJHGJOQA.js) | `queryReactionUsersToMessage` / `queryUserReactionsToMessage`（約1708 / 1802）、POST押下・取消（約1541 / 1618） | `f4e20176d7bd2f0abbf5214185ef16f936280074460684820f6571c0e8b5fcdd` |
| [一覧表示・タイムライン](https://www.medical-care.net/chunk-IGUQKMKD.js) | 種別定義（約103735）、一覧dialog（約110307〜）、タイムラインの反応描画（約132900、559178）、1件再取得（約9211、12772）、一括取得（約16811） | `4bde434e942340707edb2d45e1c02bb01e08897574878071440f3151585eca98` |
| [共通ページャ](https://www.medical-care.net/chunk-KBHQOTFV.js) | `setPerPage` / `processAfterSearch` / `queryMore`（約144800〜149900）、`autoClearIncludeMetaAfterInitialSearch`（約144641） | `c37f8c0e84e263612ea486442b306c5db1627517710b481b798bc27a98bbced5` |
| [共通API・表示定義](https://www.medical-care.net/chunk-4SYRONP7.js) | `/api/v2t`のprefix（約1809）、`doApiGet`（約126066）、`no_extend_session`（約126474）、種別の表示名（約236293） | `10a17a4d432f830b93900e782d822934f73cfc7647ab343741fc6eab1fa3f7e1` |
| [取得サービスの継承入口](https://www.medical-care.net/chunk-QFLOAJOQ.js) | 共通サービスへの継承・import | `9c4cce6d1e65f22a3cce48a4711a1f31015de3d12136b8963dbec11d626006cc` |

共通ヘルパーの`$meta.no_extend_session`は`?no_extend_session=1`を付ける。本adapterは既に
`extend_session=False`として同じ指定を実装しているので、反応系のGETにもそれを使う。
本調査では認証・API呼出し・スタンプの押下や取消を行っていない。
他の経路の一覧は[API調査](mcs-api-survey.md)。

## 3. 現行コードとの接続点

| 領域 | v1.0.10の現状 | 実装の方針 |
|---|---|---|
| [MCS adapter](../../mcs/ingest/mcs_adapter.py) | `_norm_message`は`count.thread_messages`だけを読み、`reactions`を捨てる。`extend_session=False`・deadline・認証・通信境界がある | `_norm_message`で`reactions[]`を解析（第0層）。1件・一括再取得と押下者一覧のGETを追加（第1・2層） |
| [収集tick](../../mcs/ingest/run_check.py) | 未読・履歴・返信・self probe・reconcile・連携サマリーの段階がある。`stage_karte_summary`は上限/tick・余白・理由コード付きのbounded GETの前例 | 同じ型で監視集合の再取得段階を追加する。本文通知を遅らせない |
| [自己識別](../../mcs/ops/mcs_signals.py) | `self_profile_v1.sender_id`は実測None。自局名簿`station_staff_v1`の`is_self`は未使用。`rollup.reply_state`は氏名文字列で送信者を比較 | F-7: `is_self`の`staff_id`を本人IDの既定にし、自投稿・本人反応・自局反応をIDで判定する |
| [ledger](../../mcs/core/ledger.py) | 投稿本文hash・取得job・artifact・snapshotがある | 加法migration。反応は本文のupsertと別文で更新し、hash・通知・semanticの判定に触れない |
| [閲覧](../../mcs/views/mcs_view.py) / [読み取りモデル](../../mcs/views/read_model.py) | スタンプの投影は無い | snapshotから件数・本人フラグ・観測時刻・取得状態を読む |
| [カード](../../mcs/notify/notify_render.py) / [Slack](../../adapters/slack/cards.py) | 脚注に担当・確認・タスク。本文はthread。未確認一覧はカード単位 | 脚注に本人反応を併記、未確認一覧で反応済みを末尾へ。既存配送契約を維持 |
| [抽出](../../mcs/extract/v4/extract_llm.py) / [rollup](../../mcs/extract/rollup.py) | 本文の依頼候補と返信種別、`reply_state`（未表示） | 返信・スタンプ・メンションを別の根拠として併記し、LLM再抽出を誘発しない |
| [シグナル](../../mcs/ops/mcs_signals.py) | `pharmacist_request_unanswered`の応答判定はルーム内の自局投稿か台帳登録 | #22-D5に従い本人スタンプ・スレッド内返信・メンションを応答判定に加える（1.0.12） |
| [外部出力allowlist](../../mcs/ops/export_schema.py) | 反応のrecord型は無い | 1.0.xでは既存外部契約を拡張しない |

## 4. 3層の取得設計

| 層 | 取得元 | 追加GET | 得られるもの | 個人情報 | 版 |
|---|---|---|---|---|---|
| 0 件数と本人反応 | 既存の未読・履歴・reconcile・self probeの応答 | なし | 全保管投稿の種別別件数、本人が押したか、観測時刻 | なし | 1.0.11 |
| 1 監視集合の鮮度更新 | 1件再取得または`message_ids`一括取得 | 上限/tick固定 | 「医師が承知を押した」を数分以内に反映 | なし | 1.0.11 shadow、1.0.12 有効化 |
| 2 押下者 | `user_reactions`（必要なら`reactions`） | 件数・本人フラグが変わった投稿だけ、または要求時 | 誰が押したか | あり | 1.0.13、#22-D2後 |

- **第0層**: `reactions`キーの有無を`files_present`と同じ要領で保持し、キー無しは「未取得」、空配列は「0件」。投稿ごとに種別別件数・本人フラグ・観測時刻を保存する。count 0の種別が省略される場合は欠落を0とみなさず「未記載」とする（22-Aで確定）。
- **第1層**: 監視集合は「自分のルート投稿7日分」「未応答の薬剤師宛候補」「未確認カードの投稿」（#22-D1）。鮮度の古い順、上限/tick固定、deadline余白、失敗は理由コードと再試行間隔。古い投稿の変化は既存reconcileの回転で拾い、遅延を表示で開示する。全投稿の毎tick再取得はしない。
- **第2層**: 論理キーは投稿ID・押下者ID・種別。全ページを作業集合に集め、検証後に同じtransactionでcurrentを更新する。途中成功で旧集合を削除しない。消失は比較可能な完全取得同士でだけ「前回取得後に一覧から消失」として記録する。保持するのはID・種別・観測時刻・最小の表示情報（#22-D3）。写真・連絡先は収集しない。
- 共通: 認可拒否・429・期限切れ・schema不正を「0件」にしない。スタンプのpollだけでMCS既読化、`patients.coverage_ts`更新、本文の新着通知、抽出版の更新を行わない。新しいGETは`no_extend_session=1`を既定にする。

## 5. 実装順序と受入ゲート

| 段階 | 版 | 成果物 | 進める条件・完了判定 |
|---|---|---|---|
| 22-A 契約確認 | 1.0.11 | オーナー実行のキー名・型だけ出す確認記録 | 患者情報なしテスト投稿。下記の確認票。POSTは実行しない |
| 22-B 第0層と自己識別 | 1.0.11 | `_norm_message`、加法migration、F-7、合成回帰 | 本文hash不変、0件/未取得の区別、再起動・復旧・snapshot整合 |
| 22-C 第1層（shadow） | 1.0.11 | 監視集合の再取得段階、状態・鮮度・予算の可視化 | 本文収集・既読化・通知の予算を侵食しない。shadowで保存だけ行う |
| 22-D 本人反応の表示 | 1.0.11 | カード脚注・未確認一覧・自投稿表示・digest件数 | 取得未完了と0件を区別。既存本文・添付配送を維持し、重複再送・自動メンションを生まない |
| 22-E 第1層の有効化と応答状態 | 1.0.12 | 自分の投稿への反応、#25の併記と一覧 | shadowの照合、#22-D5の判断 |
| 22-F 第2層 | 1.0.13 | 押下者の取得・閲覧 | #22-D2・D3の判断、完全取得・誤取消防止の回帰 |
| 22-G 押下（#28） | 1.1.0 | 人承認の押下経路と送信後検証 | §7 |

### テスト投稿での確認票（22-A、実APIは未実行）

| 確認 | 操作・比較 | 記録する結論 |
|---|---|---|
| 投稿オブジェクト | 既存の未読・履歴経路の応答に`reactions / mentions / is_bookmarked / is_pinned`が載るか。`include_meta`なしでも載るか | キー名、型、count 0種別の省略有無 |
| 本人フラグ | 本人・協力者が押した投稿を比較 | `self_reacted`が収集アカウント本人を指すか |
| 自動付与 | 両GETと再取得の前後で`viewed`と`is_unread`を比較。画面を開く操作とは別に測る | GETが既読や`viewed`を変えるか |
| 再取得経路 | `message_id=&per_page=1`と`message_ids=`で同じ投稿を取る | `reactions`同梱、ID数上限、`keep_read_status`の要否 |
| 押下者一覧 | 両GETで0件・1件・複数種類を見る。合成検証は49/50/51/100/101件 | 必須キー、ID型、終端、1人複数種別、時刻の有無 |
| 更新中のページング | 取得途中にテスト投稿へ追加・取消 | `timestamp`の断面意味、件数整合、再試行条件 |
| 返信・自投稿 | rootと返信、自分と他者の投稿で比較 | message IDの適用範囲、権限不足の応答 |
| セッション | `no_extend_session=1`を各経路で付けて比較 | 受理の有無、期限切れの扱い |
| 横断取得 | `/messages/mentioned`（`increment_count`なし）、`/messages/bookmarked` | 既読・セッションへの影響（#23-D1） |
| 更新有無 | `/projects/status?after=`を押下の前後で比較 | `updated`が反応で立つか |

実確認は専用の患者情報なし投稿に限定し、通常の患者ルームを対照群に使わない。
押下・取消はテスト参加者が公式画面で行う。POST経路を自動で実行しない。
repoには完全合成fixtureと非機密の確認結果だけを残す。

### 必須の合成回帰

第0層: 0件と未取得、未知種別、不正値・欠損ID、count 0の省略、再起動、DB更新・復旧・snapshot、本文hash不変、
通知・semanticの変更判定に影響しないこと、自投稿・本人反応のID判定（同名の別人を混同しない）。
第1層: 監視集合の選択・上限・余白・理由コード、shadowで表示に出ないこと、本文収集の予算不変。
第2層: ページ境界、複数種類・複数人、同じ件数での交代、重複、循環/逆行ページ、途中429/認証切れ、
`timestamp`不整合、期限切れ、取消と再押下、投稿削除、他投稿・他projectへの押下者混入なし。
共通: 本文返信との混同なし、誤った自動完了なし、Slack本文・添付の配送回帰なし、Hermes/独立モードの同じ表示契約。
実APIにアクセスする確認と、ネットワーク遮断のpytestは別工程として記録する。

## 6. 活用の範囲

- **1.0.11**: カード脚注と未確認一覧に「MCS: 本人 見ました/承知/完了」、自投稿に「自分」表示、digestに本人反応の件数。氏名なし。
- **1.0.12**: 自分の投稿への反応（カード・digest「反応が観測されていない自分の投稿」）、`reply_state`・loop関係との併記、
  `pharmacist_request_unanswered`の応答判定への反映（#22-D5）、患者横断「自分宛で応答未観測」一覧（#25）。
- **1.0.13**: 押下者の閲覧（ローカル先行）、ケアチームと組み合わせた「医師の見ましたが未観測」（#24）。
- 全版共通: 正式な割当・承認・完了や既存signalの解消を自動実行しない。応答時間の統計、個人別の評価、
  押下イベントの復元、外部export・Jev/LLMへの押下者データ追加、自動督促は必須範囲にしない。

## 7. 押下（#28、1.1.0）の前提

- 経路: Slackカードの操作選択とCLI → preview → confirm（reason）→ cmd_int → adapterの専用POST → receipt → 第1層の再取得で`self_reacted`を検証。
- 対象: 本文hash一致・未削除・未アーカイブの投稿。許可する種別は#28-D1（初期案は見ました・承知）。
- 冪等: 送信結果不明を成功扱いせず、再取得で確認するまで「送信中」。重複押下は再取得結果で吸収する。
- 禁止: 自動押下・自動取消。自動経路からPOSTを呼べないことをテストで固定する。取消はMCS画面で行い、本システムからの取消は後続版で判断する。
- 前提: 1.0.15の完了（#1・#6・F-3）、POST契約のテスト投稿確認、S-D1。

判断ID: `#22-D1`（監視集合・周期・予算）、`#22-D2`（氏名表示）、`#22-D3`（観測履歴・表示情報の保持）、
`#22-D4`（テスト投稿・参加者・実API確認の範囲）、`#22-D5`（本人スタンプを記録上の応答に数えるか）、
`#28-D1`（押下を許可する種別・対象・操作者）。本文は[ROADMAP §9](../ROADMAP.md#9-決める事項)。
実装日数は22-Aの実確認後に見積もる。公開JSの発見だけを根拠に納期・網羅性を約束しない。
