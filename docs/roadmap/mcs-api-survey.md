# MCS公開クライアントのAPI調査（#23〜#29の根拠）

2026-10-04版割当: 旧1.0.13〜1.0.15の残件は全て安定稼働版1.0.13へ集約。
成果物・CLI・受入の正本は[1.0.13開発計画](../development/plans/RELEASE_1.0.13.md)。
当時の調査・設計例と現在の実装状態を区別し、既存実装は再実装しない。

### 2026-10-04追記: metadataのローカル入口

[project_metadata](../../mcs/ingest/project_metadata.py)は対象を明示したケアチーム・
構造化薬歴/観測値・consultationsの取得/正規化/保存を、
[cross_lists](../../mcs/ingest/cross_lists.py)はmentions/bookmarks横断取得を提供します。
いずれもopt-inのCLIと安全な対象・応答分類を持ち、opt-inなしで取得しません。
入口の存在は実APIの副作用・全対象の契約確認や公開承認の完了を意味しません。
以下の公開クライアント初期調査と実API確認記録は当時の証拠として保持し、
#23〜#29のD判断・C1・実API/出典/公開条件を変更しません。

調査日: 2026-10-02。対象: `https://www.medical-care.net/index.html`（SPA入口）が参照する
公開JavaScript 221本（chunk 218本、`main-SAHZA53Q.js` ほか）。
2026-10-02の静的調査では認証なしのGETで取得し、実行せずに読んだ。
その時点では`/api/`を一度も呼ばず、Cookie・トークンも送っていない。2026-10-03の実API確認は§7に分けて記録する。
転載したJS・実APIの生応答・押下者データはrepoへ保存しない。
再調査時はURLだけで同一版と判断せず、hashと関数を照合する（ハッシュ付きアセットは更新・削除され得る）。

§1〜§6の静的調査結果は**クライアントの挙動**であり、サーバ側の保証・応答の全項目・副作用とは区別する。
出典は`chunk-`を省いたハッシュ名@文字オフセットの目安。

2026-10-03レビュー: 優先順位・版割当・公開条件は[ROADMAP](../ROADMAP.md)を正とする。
当初の「専用投稿のみ、対象ID未指定・実API未実施」は過去の状態である。
2026-10-03のユーザー指示で専用投稿限定を解除し、任意の対象選定・通常患者ルームの読取りを許可された。
#26/#27の件数調査も原1.0.11の22-A一括確認に保持する。新GETの定期shadowは既定off、実機適用なし。
その後のユーザー指示でエージェントによる実機検証を中止し、最終の実画面/API確認はユーザー自身が行う。
投稿・押下・取消・しおり/ピン変更は未実施。原確認票の全項目を[最終手動確認票](stamps.md)に残す。

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
| `GET /projects/{id}/consultations`・`/{cid}`・`/{cid}/responses` | group相談（`purpose, status, is_unread, last_response`）と回答 | 4WFEVJDT@951, @1147, @6035、IGJY24TN@213342 | #27 | group単位の利用有無と患者データへの関連付け。患者向け経路とは未確認。回答にも別のリアクションあり |
| `GET /kartes/{kid}` | 氏名・生年月日・住所・電話・保険・患者番号・病名・連携ノート・ラベルなど40項目 | UHQUQDWW@241, M2MZSSUK@6248 | 使わない | 機微項目のため取得対象外。病名・ラベルが必要なら項目を限定して別判断 |
| `GET /kartes/{kid}/trails` | 患者情報の変更履歴 | UHQUQDWW@1433 | 未割当 | 差分構造が未追跡 |
| `GET /users/self/message_templates` | 定型文の項目（text / datetime / checkbox / fixed_text） | IGUQKMKD@593277 | 任意（自投稿の決定的解析） | 利用の有無 |
| `GET /users/self/count`・`/badges` | 未読・メンション等の件数（`home.unread`, `home.unread_mention`ほか24種） | WT4242C5@50118, 4SYRONP7@129506 | 任意（digestの件数） | 既存の未読走査との整合 |

調査対象の公開クライアントで確認できなかったもの: 既読者一覧・既読数、汎用の予定・訪問計画API、タスク/ToDo、通知フィード、横断の反応取得。サーバ全体のAPI不存在を証明するものではない。
主治医・緊急連絡先・アレルギー・既往歴の専用項目も無く、画面は連携サマリーへの自由記述を案内している。

### 相談の対象条件（2026-10-03の計画訂正）

原計画は#26と#27を同じ「登録患者数」の調査に載せたが、相談は公開UIでgroup向けの機能だった。
患者ルームへの100GETを相談利用調査の前提にした点を訂正する。22-Aから#27の確認を外す変更ではない。
公開JSの確認済みキャッシュを読んだだけで、この再調査では実API/実画面を操作していない。

| 確定したクライアントの挙動 | 根拠 |
|---|---|
| 一覧serviceのbase pathは`/projects/{id}/consultations`。queryをそのままGETへ渡し、`consultation_id`指定時だけpageを削除。相談固有のprefix overrideは無い | [4WFEVJDT@951](https://www.medical-care.net/chunk-4WFEVJDT.js)、[QFLOAJOQの継承](https://www.medical-care.net/chunk-QFLOAJOQ.js)、[4SYRONP7@128211](https://www.medical-care.net/chunk-4SYRONP7.js) |
| 共通helperは環境の`location.origin + "/api/v2t"`を使う構成。DIは同じhelper実装を全serviceへ渡す | [main-SAHZA53Q@87954・@109375](https://www.medical-care.net/main-SAHZA53Q.js)、[4SYRONP7@1809](https://www.medical-care.net/chunk-4SYRONP7.js) |
| 相談タブは`app-projects-group`のtimelineに配置。相談一覧のtitleもproject.groupを参照。通知設定はproject.typeがgroup以外なら拒否 | [IGJY24TN@213342・@214040・@68073・@13708](https://www.medical-care.net/chunk-IGJY24TN.js) |
| 通常一覧はpage/per_pageを使い、検索件数の既定は20。search/sort/purposes等は任意filter。service内に必須の追加query指定は見つからない | [IGJY24TN@11318・@12148](https://www.medical-care.net/chunk-IGJY24TN.js)、[KBHQOTFV@128557・@148392](https://www.medical-care.net/chunk-KBHQOTFV.js) |

確認したSHA-256: `4WFEVJDT`は`10f03e4a90048b781aa6d8f8b6ae5c5c4d029efae772b3df57575c06631c1170`、
`IGJY24TN`は`ea3406144692a68f0e399619a53a754aefccf5650d0fc837cc4c3b5ef0db3303`、
`main-SAHZA53Q`は`bee613e518b730ec84d344b973ad6cc036ee34e92e3e6edb1e210f6d48efa72b`。
共通helper/継承/ページャのSHAは[stampsの既存記録](stamps.md#2-公開ソースの根拠)と一致する。

**推測と未確認:** 臨床100ルームの全404は対象種別の違いに起因する可能性があるが、
サーバのgroup限定や404の原因を実証した結果ではない。権限不足・対象不在等を区別できず、
93患者不明・complete=falseという過去の取得結果は維持する。相談登録0患者とは扱わない。
また、groupの相談を患者データへ安全に関連付けられる根拠はまだ無い。

今後の#27確認は、type/groupが確認できるgroupを対象に登録のあるgroup数と取得範囲・不明を分ける。
一覧のクライアント相当条件はpage=1、per_page=20、include_paginate_totals=0で、
本システムの読取りではno_extend_session=1を付ける。これらを送れば必ず成功するというサーバ保証ではない。
エージェントは追加実行せず、ユーザーの最終手動確認へ渡す。
既存取得の確認候補キーはtype、group、is_archived、erasure_date、last_consultation.is_unread。
consultation_enabledはrelation用のクライアント生成オブジェクトでfalseにしている箇所
（[3X6DIYSL@17983](https://www.medical-care.net/chunk-3X6DIYSL.js)）があるが、
サーバの相談機能停止を判定するflagとしては確認できない。

## 5. 書込み系と対象外

- 調査した業務データのPOST経路は1.0.xで新たに実装しない。既存のログイン処理・通知先への配送はこの禁止の対象ではない。
- 1.1.xの候補は`POST /messages/{id}/reactions`（押下）、`POST /projects/{pid}/messages/{mid}/messages`（返信）。
  `POST /messages/{id}/reactions/delete`（取消）は調査済みだが1.1.0にも含めず公式画面で行う。
  `POST /projects/{pid}/mark_as_read`も調査済みだが変更対象外（現行はsnapshot timestamp付き未読一覧GETで既読化する）。
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
7. #26/#27の件数: `medication_periods`・`observation_items`は登録患者数、`consultations`は登録group数と取得範囲・不明を確認する。相談と患者データの関連付けは別に確認し、患者向けと仮定しない。元の確認目的を保持した対象条件の訂正である。本文・氏名・患者/投稿ID・生応答は出力/記録せず、取得不明を登録0件と扱わない。未達はユーザーの最終手動確認へ渡す。
8. 1.1.0の準備としてPOST契約の確認はテスト投稿で別途行う（1.0.xでは実行しない）。

## 7. 22-A実確認と残る受入（2026-10-03）

原1.0.11の一括確認を縮小せず、当該版の契約検証と後続版の先行調査を全件追跡する。
HTTP成功・型の確認・空集合の観測を、副作用の不存在や全利用条件の保証と同一視しない。
実機の取得設定変更、投稿・押下・取消・明示既読化は行っていない。
ユーザー指示によりエージェントの追加実機検証は中止。原22-Aの全確認は1.0.11に保持し、
未達を将来版へ移さずユーザーの最終手動確認待ちとする。

| 原確認項目・用途 | 調査した事実 | 未達・制約 |
|---|---|---|
| 既存未読/履歴応答のmetadata（1.0.11） | `/projects`全102ルームのinventoryを取得し完了。現時点の未読は0。履歴はinclude_meta有/無ともreactions/mentionsがlist、しおり/ピンはキー無し | 未読あり経路、count 0種別の省略規則、しおり/ピンの存在時の型は未実証 |
| 本人フラグ・viewed/既読非変更（1.0.11） | 1件再取得のtype/count/self_reactedはstr/int/bool、正規化成功、target一致。ルーム既読false→false。bounded履歴2ルーム内のself_reacted=true投稿と完全押下者一覧を、名簿の唯一is_self ID・同種別で照合して一致。陰性例も一致 | 未読true保持、viewedの自動付与・初回GET前後は未実証。exact応答は投稿単位のraw読取flagが欠落し、norm既定falseは証拠にしない。押下/取消操作はしていない |
| 1件/一括再取得（第1層） | 1件はkeep_read_status=1付きで成功。一括は同指定付き400、指定なし＋include_oldest_unread_thread_message_id=1で200・target返却 | 一括のID数上限、未読true保持、既読保持paramsの一般契約は未確定。現第1層実装は1件経路を使う |
| no_extend_session（全経路） | 指定付きGETの200、実cacheExpiredからの既存auto_login復旧、合成の期限切れ後optional GET打切りを確認 | 原session行の指定受理/期限切れの扱いは確認済み。実時間の非延長効果は追加の未実証事項で、原票への追加必須条件にしない。横断経路の副作用は別行で追跡 |
| mentioned/bookmarked（1.0.12の先行確認） | increment_countを送らず、no_extend_session=1で両GET200・messages空 | 非空応答、未読true保持、sessionへの副作用は未実証。取り込み/表示の実装は後続 |
| projects/status（先行確認） | afterにprojects.paginate.timestampを使い200、projects.updatedはbool | 押下/編集でupdatedが立つ対照は未実証 |
| 押下者一覧（1.0.13の先行確認） | keep_read_status付きは400。初回timestamp省略で全種類2行・viewed1行が件数整合。per_page=1で全種類2ページ/viewed1ページを各2walkし、固定timestamp・件数・終端・集合不変が整合。追加のempty reactions対照ではall/viewed各2walkとも0行・1ページ終端・件数一致。原49/50/51/100/101件を含む診断/metadata関連の合成191 passed | 空timestampは200空で不整合。初回は省略、継続は有効server timestampだけを使う。行の時刻キーは無し。1人複数種別はサンプルで未観測、更新中完全性は未実証。0行対照もraw読取flag無しで、未読保持の証拠ではない |
| 返信/自投稿・権限（1.0.11の確認票） | root/返信×自分/他者の4区分でtarget・投稿者ID・parent一致、reactions/mentions同梱、正規化エラー無し。対象ルームはinventoryで既読を確認 | 権限不足の実応答は判定不能。exact投稿flag無しのnorm既定falseやルーム既読を投稿の未読保持と扱わない。特定403や権限変更を追加必須にしない |
| #26/#27件数（1.0.13の先行調査） | 臨床100ルーム・karte ID重複除外93患者で薬歴/観測項目は各登録0/不明0、両complete=true。相談の公開UIがgroup対象と判明し、調査単位を訂正 | consultationsの患者ルーム100GET404は93患者不明・complete=falseとして維持。group利用数と患者関連付けは未確認、group GETは未実施。患者不明を登録0へ変更しない。ユーザー手動確認待ち |
| POST契約（1.1.0前の別工程） | 業務データPOSTは未実行 | 原1.0.xの禁止範囲を維持。1.0.11のAPI確認として実行しない |

第1層shadowの原受入は、上限内での実更新と状態・鮮度・予算の可視化まで含む。
定期/手動共通経路、5件/回・最大25秒・全体余白30秒・成功後30分/失敗後6時間をローカル実装した。
既定offは安全措置であり、未読保持の実証後の有効化・shadow照合を省略する完了条件ではない。
実装・合成回帰・実API・exact SHAのCI・配備は別に確認する。
