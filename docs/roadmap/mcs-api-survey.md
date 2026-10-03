# MCS公開クライアントのAPI調査（#23〜#29の根拠）

調査日: 2026-10-02。対象: `https://www.medical-care.net/index.html`（SPA入口）が参照する
公開JavaScript 221本（chunk 218本、`main-SAHZA53Q.js` ほか）。
認証なしのGETで取得し、実行せずに読んだ。`/api/`は一度も呼んでおらず、Cookie・トークンも送っていない。
転載したJS・実APIの生応答・押下者データはrepoへ保存しない。
再調査時はURLだけで同一版と判断せず、hashと関数を照合する（ハッシュ付きアセットは更新・削除され得る）。

以下は**クライアントの挙動**であり、サーバ側の保証・応答の全項目・副作用は実API確認（22-A）まで未確認。
出典は`chunk-`を省いたハッシュ名@文字オフセットの目安。

## 1. 集計

| 項目 | 値 |
|---|---|
| API呼出し箇所 / 異なる経路 | 479 / 432（GET 214・POST 218。PUT/PATCH/DELETEは無く、更新・削除も`POST …/delete`型） |
| APIのprefix | `location.origin + "/api/v2t"`（4SYRONP7@1809） |
| リアルタイム経路 | WebSocketは通話（Amazon Chime）のみ。SSE・Web Pushなし。更新はポーリング |
| ポーリング | タイムライン10秒（`messages/latest?after=`）、ホーム60秒（`/projects/status?after=`ほか）、アプリ更新600秒（`HEAD /index.html`）。すべて`no_extend_session=1`付き（4SYRONP7@126474, @131685） |

## 2. 投稿オブジェクトに同梱される項目（追加GETなしで取れる候補）

| 項目 | クライアントが読む形 | 出典 | 活用先 |
|---|---|---|---|
| `reactions[]` | `{type, count, self_reacted}`。種別は`good / viewed / accepted / thanked / completed` | IGUQKMKD@559178, @132900, @103735 | #22 第0層 |
| `mentions[]` | `{type: user|station|project, user.id, station.id, station.count.staffs}` | 4SYRONP7@98935 | #23 宛先判定 |
| `is_bookmarked` | スタンプ押下後の1件再取得で`reactions / is_bookmarked / count / delete_user`を上書き | IGUQKMKD@12772 | #23 しおり |
| `is_pinned` / `project.has_pinned_message` | `sort=pinned`で先頭に並ぶ | IGUQKMKD@666516 | #23 ピン留め |
| `count.thread_messages` | 既存adapterが使用 | — | 既知 |

`include_meta`との関係: タイムライン初回は`include_meta`付き、2ページ目以降・1件再取得・ポーリングは無し。
それでもクライアントは`reactions`をnullガードなしで参照する（IGUQKMKD@570689）。
サーバが常に返すかは22-Aで確認する。

## 3. 再取得・横断取得の経路

| 経路 | params（呼出し元で確認） | 用途 | 出典 | 確認事項 |
|---|---|---|---|---|
| `GET /projects/{pid}/messages` | `message_id`, `per_page=1`, `exclude_terminated_ex_application`, `keep_read_status`（未読・しおり画面のみ） | 1件再取得 | IGUQKMKD@9211 | `reactions`同梱、既読影響 |
| `GET /messages` | `message_ids`, `include_oldest_unread_thread_message_id` | 複数投稿の一括取得 | IGUQKMKD@16811 | ID数上限、`keep_read_status`の要否 |
| `GET /messages/mentioned` | `page`, `per_page=20`, `unread`, `include_meta`, `increment_count=1` | 自分宛メンションの横断一覧 | OLVJNCFG@2037 | `increment_count`は送らない。既読影響 |
| `GET /messages/bookmarked` | `page`, `per_page` | しおりの横断一覧 | FUWQG6Z4@2163 | 既読影響 |
| `GET /projects/status` | `after=<paginate.timestamp>`, `no_extend_session=1` | 更新有無の1ビット（`projects.updated`） | BWGEFJIG@25888 | 反応・編集で立つか、対象範囲 |
| `GET /projects/unread` | `page`, `per_page` | 未読画面 | NPNUVRFQ@5027 | 既存の`/projects?include_meta=1`との差 |
| `GET /messages/{id}/reactions` | `reaction_type`, `page`, `per_page=50`, `timestamp`, `include_meta`（初回のみ） | 種別別の押下者（`users[]`） | TJHGJOQA@1708, IGUQKMKD@112014 | 既読影響、ページ終端 |
| `GET /messages/{id}/user_reactions` | 同上 | 全種別の押下者（`reactions[]{reaction_type, user}`） | TJHGJOQA@1802, IGUQKMKD@112460 | 1人複数種別、時刻の有無 |

押下者の行で参照されるuser項目: `id, type, is_anonymous, last_name/first_name, medium_icon_url, is_confirmed,
is_authenticated, specialist_categories[0].name, stations[0].name`。時刻の項目は無い。

## 4. 患者・チーム・構造化データ

| 経路 | 取れるもの | 出典 | 活用先 | 確認事項 |
|---|---|---|---|---|
| `GET /projects/{id}/members` | `users[]{type, is_director, is_deletable, is_self, station, specialist_categories}`, `count` | 3X6DIYSL@7734, HB7B2UZS@208 | #24 ケアチーム | 1患者1GET。退出者は`/members/all` |
| `GET /kartes/{kid}/medication_periods` | `medication_periods[]{begin_date, end_date, medicine_informations[]{id, name}}`。1期間最大20剤。用法・用量・処方元は無い | UHQUQDWW@626, @1812 | #26 構造化薬歴 | 登録のある患者数 |
| `GET /kartes/{kid}/observation_items`・`/lab_test_items`・`/{item}/values` | 項目（`analyte_tag`, 基準値, 型 scalar/max_min/left_right）と値（`observation_issued_at`, `unit`） | M2MZSSUK@10778, @17899 | #26 観測値・#15 | 項目名はサーバのマスタ依存 |
| `GET /projects/{id}/consultations`・`/{cid}`・`/{cid}/responses` | 相談（`purpose, status, is_unread, last_response`）と回答 | 4WFEVJDT@951, @1147, @6035 | #27 | 利用の有無。回答にも別のリアクションあり |
| `GET /kartes/{kid}` | 氏名・生年月日・住所・電話・保険・患者番号・病名・連携ノート・ラベルなど40項目 | UHQUQDWW@241, M2MZSSUK@6248 | 使わない | 機微項目のため取得対象外。病名・ラベルが必要なら項目を限定して別判断 |
| `GET /kartes/{kid}/trails` | 患者情報の変更履歴 | UHQUQDWW@1433 | 未割当 | 差分構造が未追跡 |
| `GET /users/self/message_templates` | 定型文の項目（text / datetime / checkbox / fixed_text） | IGUQKMKD@593277 | 任意（自投稿の決定的解析） | 利用の有無 |
| `GET /users/self/count`・`/badges` | 未読・メンション等の件数（`home.unread`, `home.unread_mention`ほか24種） | WT4242C5@50118, 4SYRONP7@129506 | 任意（digestの件数） | 既存の未読走査との整合 |

無いもの: 既読者一覧・既読数、汎用の予定・訪問計画API、タスク/ToDo、通知フィード、横断の反応取得。
主治医・緊急連絡先・アレルギー・既往歴の専用項目も無く、画面は連携サマリーへの自由記述を案内している。

## 5. 書込み系と対象外

- POST 218本は1.0.xで実装しない。1.1.xの候補は`POST /messages/{id}/reactions`（押下）、
  `POST /messages/{id}/reactions/delete`（取消）、`POST /projects/{pid}/messages/{mid}/messages`（返信）、
  `POST /projects/{pid}/mark_as_read`（既読化。現行は未読一覧GETの既読化を使用）。
- GETでも使わないもの: `/books/{bid}/items/{iid}/count`（閲覧数を増やす）、
  `/users/self/additional_authentications/totp/backup_codes`（秘密値）、`/payments/*`、`/users/check_*`、`/zzz_sample`。
- `/messages/mentioned`の`increment_count=1`は効果不明のため送らない。

## 6. 22-Aで確認する項目（この調査由来）

1. 既存の未読・履歴経路の応答に`reactions / mentions / is_bookmarked / is_pinned`が載るか。`include_meta`なしでも載るか。count 0の種別が省略されるか。
2. `self_reacted`が収集アカウント本人を指すか。`viewed`が閲覧で自動付与されるか、GETで変化しないか。
3. `GET /messages?message_ids=`の受理上限と`keep_read_status`の要否。1件再取得との差。
4. `/messages/mentioned`・`/messages/bookmarked`が既読・セッションに影響しないか。
5. `/projects/status?after=`の`updated`が反応・編集で立つか。
6. `no_extend_session=1`が各経路で受理されるか。
7. #26/#27の件数: `medication_periods`・`observation_items`・`consultations`が登録されている患者数（本文は出力しない）。
8. 1.1.0の準備としてPOST契約の確認はテスト投稿で別途行う（1.0.xでは実行しない）。
