# hermes-mcs ロードマップ

改訂日: 2026-09-29（v1.0.6 / `f947042` 時点。詳細計画を追加）
対になる文書: zaitaku-calender `ROADMAP.md`。接続フェーズの ID（C0〜C4）、未決事項の番号（Q1〜Q12。Q11・Q12 は zaitaku-calender 側が起票し本書へ同期した新規）、契約決定の番号（CD-1〜CD-10）、契約版は両文書で共通。片方の C0/Q/CD 見出し行だけを変えた場合は C0 を完了扱いにしない。
旧版（Oracle レビュー統合版、機能候補 v2 30 項目）は git 履歴（`git show f947042:docs/ROADMAP.md`）を参照。変更の詳細は `CHANGELOG.md`。

**本書は索引**。項目ごとの実装計画（目的・現状の根拠・設計方針・成果物・受入条件・依存・規模・オーナー判断）は `docs/roadmap/` にある。

| 領域 | 項目 | 詳細計画 |
|---|---|---|
| 取得・アーカイブ | #1〜#6 | [`roadmap/acquisition.md`](roadmap/acquisition.md) |
| 整合性・命名・運用前提 | #7〜#10 | [`roadmap/integrity.md`](roadmap/integrity.md) |
| 抽出 | #11・#15・#16 | [`roadmap/extraction.md`](roadmap/extraction.md) |
| シグナル・統計 | #12〜#14・#17〜#19 | [`roadmap/signals-stats.md`](roadmap/signals-stats.md) |
| zaitaku-calender 接続 | C0〜C4（hermes-mcs 側） | [`roadmap/connector.md`](roadmap/connector.md) |

**ID の扱い**: 項目は §4 の番号（#1〜#19）で呼ぶ。旧版にある ID は A1-x / B1-x / C1-x、懸念 C01〜C07、機能候補 v2 の #1〜#30 で、「旧版の出典」列の「優先順位 N」は旧版「機能候補の優先順位（基盤修正完了後）」の N 番目を指す。本書で新設する ID は次の 4 種。
- `F-n`: 先に直す既存不具合・ガード（§3）。
- `#N-Dk`: 項目 N のオーナー判断（§9）。
- `CD-n`: C0 で決める契約事項（§5）。
- `Q1〜Q12`: 接続のオーナー判断（§9。両文書で共通）。

## 1. 位置づけ

- hermes-mcs は **MCS から情報を取得して抽出する**ことに集中する。業務ワークフロー（依頼・フォロー・引継ぎ・報告書・処方正本・予定）の正本は zaitaku-calender。
- 最上位ゴール: 「取得漏れを検知できる fail-closed 長期アーカイブ」として承認されること。旧版の判定 CHANGES_REQUESTED はまだ解消していない（旧版 L8-13）。
- 接続では、取得した事実と本文（`message_body` record、CD-9。オーナー判断 2026-09-29）を**未採用の候補として** zaitaku-calender に渡す。確定は zaitaku-calender 側で人が行う。

### 責務分担表（zaitaku-calender `ROADMAP.md` と同じ内容）

| 領域 | hermes-mcs | zaitaku-calender |
|---|---|---|
| MCS からの取得・欠落の検知・原本アーカイブ | 正本（`mcs/ingest/`、`mcs/core/ledger.py`） | 持たない |
| MCS 本文・添付の抽出（薬剤言及・症状・検査値・OCR） | 担当。出力は候補のみ（`mcs/extract/`、`mcs/semantic/`） | 抽出しない（LLM による事実生成・OCR による自動確定は OUT） |
| MCS 新着・緊急連絡のリアルタイム通知 | 担当（Discord/Slack。`mcs/notify/`（`notify_flush.py` 等）、即時配信への昇格判定は `mcs/ops/mcs_signals.py` の `_urgency_high`（:1043 付近）と呼び出し側（:1194・:1260・:1277。昇格行の詳細は未確認）。通知の正は hermes の即時通知） | 外部リマインダーは追加しない |
| 患者・処方・臨床プロファイル・ケアチームの正本 | 持たない | 正本 |
| 予定・訪問・フォロー・引継ぎ・Work Queue・報告書・算定候補 | 持たない（依頼台帳は既存機能の範囲で凍結） | 正本 |
| 外部患者 ID ⇔ 内部患者の対応付け | project_id を送るだけ | 人が確定する append-only の対応表 |
| 取込データの採用 | しない | 薬剤師が既存画面で記録（出典付き） |
| 受領確認 | `GovernedExporter.reconcile` は `LocalSink` 前提。receipt ファイルで照合するには C1 で `HandoffSink`・`parse_receipt` を追加し、`_valid_ack` の status 未検査（F-3）を直す | 受領 receipt を返す（形式は CD-5 を採用するか C0 で合意） |
| 撤回 | `withdraw` は `sink.delete` を呼ぶだけで zaitaku に運ぶ指示書の形式が無い（C0 で合意。`docs/roadmap/connector.md` §2）。削除 receipt で照合 | 未採用 staging を削除して削除 receipt を返す。採用済み記録は残し出典の失効を表示 |
| MCS への書き戻し | しない | しない（SCP-07） |
| 逆方向（zaitaku-calender → hermes-mcs）のデータ | 受け取らない（receipt を除く） | 返さない（receipt を除く） |

## 2. 完了済み基盤（履歴）

詳細は git 履歴と `CHANGELOG.md` を参照。

- **Phase A 安全境界と起動**: keep_read_status の全経路注入、mark_as_read 応答の厳密検証、login origin 固定、通知先 fallback 廃止、redirect 拒否・proxy 無効、fresh DB 起動、migration 原子化、段階別 status（A1-1〜A2-3）。閲覧（`mcs/views/mcs_view.py`）と依頼台帳（`mcs/ops/mcs_requests.py:15-35`）も追加済み。
- **Phase B 取得完全性**: `coverage_ts`、MessageBatch、返信欠落と reply job、has_new_replies、history_floor、`fetch_jobs` 耐久キュー、返信添付、同一 tx 化、deadline 伝播、添付の隔離（B1-1〜B3-1）。
- **Phase C 復旧性・派生データ**: backup の quick_check と原子置換（C1-1。`mcs/core/maintenance.py:47-77`、`mcs/core/ledger.py:1856-1970`）、stale 判定、LLM 出力検証、backoff、rollup 修正、分割通知の progress 検証（C2-1〜C3-1）。
- **CCO 分離（A3）**: 読み取り専用 snapshot、`LedgerReader`、`publish_snapshot`、`generation_id`。接続の出典（どの世代の事実か）に使う。
- **更新・復旧経路の強化（v1.0.6）**: launchd bootstrap の `launchctl print` 検証、更新経路の launchctl 時間上限、consent hold をあらゆる escalate で維持、escalate 通知の重複抑止、Discord/Slack の確定・取消の一本化（詳細は `CHANGELOG.md` の 1.0.6）。
- **外部出力契約の骨格**: `GovernedExporter` の deliver・`reconcile`（`mcs/ops/ext_contract.py:555`）・`withdraw`（同 :601）は実装済み。journal に `held` は書かれず、結果不明は `sent` として残る（`held` は読取り時に許容するだけ: 同 :449-451,:513）。CLI（同 :660-687）は `--auth/--records/--state/--sink` の deliver だけを公開し、sink は `LocalSink`（同 :327）のみ。
- **その他の完了**: 2026-09-19 追加検証（paginate 例外境界、schema_error、thread_incomplete、mark_result_unknown）、discovery job（C02）、proxy 非継承（C04）、添付保存名（C05、今後保存するもの）、mark_as_read の定期実行（2026-09-23 承認。旧版 優先順位 9）。

## 3. 先に直す既存不具合とガード

詳細計画の調査で見つかった、本番の出力や運用に関わる既存の問題。いずれも本書の作成時点では未修正。

| ID | 内容 | 影響 | 対応 | 規模 |
|---|---|---|---|---|
| F-1 | **未確認の検査値がカードに確定表示される**。`structured_view._lab_line`（`mcs/views/structured_view.py:195-208`）が `unverified` を見ない。薬・症状・依頼は見ている。【実行確認】値が自分の evidence と矛盾する場合も、evidence がない場合も「検査:」行に出る | Discord/Slack のカードと text 通知に、根拠のない検査値が確定として出る | #15-A（値と evidence の突合、読み側で「検査候補（未確認）」に分離） | S |
| F-2 | **ルール抽出が否定表現でも緊急度 high にする**。`_URGENT` の部分一致（`mcs/extract/v1/extract.py:48,304-306`）。【実行確認】「急ぎではありません」「緊急の対応は不要です」「明日すぐに連絡します」がすべて high | 第一報の警告表示（`notify_flush.py:267-270`）と signal の即時配信への昇格（`mcs_signals.py:1043-1058,1190-1197`）が、緊急でない投稿で起きる | #12a（出所ラベル）と #12-D5（ルール由来を同格に扱うか）。ルール側の否定ガードは #12c（RULE_VERSION の bump を伴う） | S（表示）/ M（12c） |
| F-3 | **外部出力の receipt が `status` を無視して acked にする**。`ext_contract._valid_ack`（:546-553）は id・sha・records・acked_at だけを見る。`status:"rejected"` でも acked になる | 手動・未運用のため潜在。C1 で receipt を扱う前に直す | C1 の G3（`parse_receipt` と `_valid_ack` の status 検査） | S |
| F-4 | **PRESETS / DETECTORS と export allowlist の整合を検証するテストがない**。食い違うと `brain_export.run` 全体が `stat_not_exportable` / `aggregate_field_type_invalid` で失敗する（`export_schema.py:159-161,172`） | 新しい stat / signal 型を足すたびに、export 全体が止まるリスク | 共通ガードテスト（drift guard。C0 の成果物と共通） | S |

コード外の運用リスク（実装計画ではなく判断事項）: **この Mac は FileVault Off・Time Machine の保存先なし**（2026-09-29 確認）。原本 DB と日次バックアップは同一ディスク上の平文だけで、端末の故障・盗難で復旧点が残らない。#1 と同時に判断する（`#1-D6`）。

## 4. 項目一覧（#1〜#19）

規模: S = 半日〜1 日、M = 2〜4 日、L = 1 週間以上（実装 + テスト + docs。運用での測定は別）。「旧版の出典」は旧 ROADMAP の項目名・ID。

| # | 項目 | 旧版の出典 | 規模 | 主な依存 | 要点 |
|---|---|---|---|---|---|
| 1 | 暗号化オフサイトバックアップと復元訓練 | v2 #30、Phase C C1-2、優先順位 1 | L（1a M / 1b S / 1c S） | #1-D1〜D6 | 検証済みの復旧点を暗号化して端末外へ。復元の本体は実装済みで、欠けているのはオフサイトの取得と復号、drill と記録。OS 同梱の `/usr/bin/openssl` + HMAC |
| 2 | MCS API 取得契約の fixture 化 | 懸念 C01、追加検証「残件の優先順」 | S〜M | なし | ReplayWorker + 完全合成の fixture、`keep_read_status` の全経路（thread 取得は未固定）、順序に依存しないことのプロパティテスト |
| 3 | 取得不能返信の分類 | 追加検証「残件の優先順」 | M | #2 | `fetch_jobs.error` に理由を記録（添付の前例）。`mcs_view` の `not_recorded` 固定を解消。`known_gaps` を併記 |
| 4 | 既存データの修復 | 旧版「既存データの修復順」 | M + 運用 | #1a、#3、#5、#8-M1 | 読取り専用の `plan`、実施記録（`record` / `finalize`）、reconcile の完了記録。**C1 の本番投入の前提** |
| 5 | 通信中の deadline | 追加検証「残件の優先順」 | M（下限だけなら S） | — | MCS / LLM の通信経路は実装済み。残りは `hermes send` の最小予算、非通信ステージの上限、プロセス全体の watchdog |
| 6 | 通知受付直後のクラッシュ時の重複 | 追加検証「残件の優先順」 | S〜M | #5 の下限 | text 経路の重複対策は実装済み（write-ahead marker）。残りは restore ゲート、hold の理由、種別ごとの配送方針 |
| 7 | FK/CHECK の導入 | 懸念 C06 | S / M / L | #9、監査 | 監査 → version 据え置きの guard trigger → 任意で real FK。中核表の FK は設計上不可のものが多い |
| 8 | 編集履歴と命名 | 懸念 C07 | S〜M | C0 の前 | `message_revisions`（hash chain、本文なし）、rollup 2 キーの改名、wire enum の判断 |
| 9 | SQLite runtime 版の確認 | 懸念（SQLite 版。旧版に ID なし） | S | — | `mcs_setup check` に probe。hermes の Python は 3.53.1 で安全、system の `/usr/bin/python3`（watchdog）は 3.51.0 で影響範囲 |
| 10 | 独立実装レビュー | 追加検証末尾 | M | — | 全体レビューは廃止。v1.0.6 の末尾 18 コミット（update / recovery / confirm ゲート）に絞った 1 回 |
| 11 | 薬剤名正規化（drug_map） | v2 #3、優先順位 3 | M | #11-D1 | 派生 artifact `med_ref`（成分候補、注釈だけ）。辞書は `data/` に置き、repo に入れない。`mcs-read-model/1` は不変 |
| 12 | 緊急度エスカレーション | v2 #18、優先順位 6 | 12a S / 12b M | 12b は #13 の後 | 12a: 緊急度の表示（F-2）。12b: 後追い判明・未確認の再通知（shadow → on）。新 signal 型・メンションは作らない |
| 13 | 日次 digest | v2 #1 | M | #12b と直列 | text route、件数と一覧（ID のみ）、取得状況を常時開示、既定 off。既存 signals digest と併存 |
| 14 | 変更エピソードの紐付け | v2 #16 | L | #11、#19 | `med_change_no_followup` の誤検知を閉じる方向だけで減らす。shadow → 人手監査 → on |
| 15 | 検査値の抽出 | v2 #10 | A S / B M / C M | #11 の `fold` | A: 既存 labs の安全化（F-1）。B: ルール labs（RULE_VERSION 7）。C: 正規化。時系列ビューは作らない |
| 16 | 添付 OCR と分類（ローカル） | v2 #9、優先順位 5 | L | #16-D1〜D4 | macOS Vision（osascript）+ sandbox で新規添付だけ。結果は未確認候補で、通知・export・Jev に出さない |
| 17 | 職種間やり取りの構造と応答時間 | v2 #20 | M（v2 L） | — | 永続テーブルなしの導出。職種群 × {n, 中央値, p90, 未応答数}。小セル抑制。個人データなし |
| 18 | 業務負荷レポートと集計 | v2 #27、優先順位 7（集計部分） | M | #13 | `coverage_gaps` stat + 週次・月次レポート。取得未完了範囲を必ず併記。定期出力の仕組みは新規 |
| 19 | シグナル精度のフィードバック | v2 #28 | M | — | ラベル基盤（resolution の原因、`reason_code`）と `st_signal_feedback`。内部だけ。検知条件への自動フィードバックはしない |

### 調査で分かった旧記述の誤り（各詳細計画に根拠）

- #1: 根拠コマンド `rg 'encrypt\|restore_drill'` は ripgrep では `\|` がリテラルで、0 件は当然だった（正規表現に直しても 0 件で結論は同じ）。
- #6: 「重複」は text 経路では既に対策済み（write-ahead marker。旧 F19）。
- #5: 通信経路（MCS / LLM）の deadline は実装済み。
- #12: 「否定 / 時制は v4 で対応済」は LLM レーンだけ。ルール抽出は否定を見ない（F-2）。
- #15: 本文由来の labs 抽出は v4 で実装済み。ないのは、値の突合・unverified の扱い・ルール labs・日付・基準範囲・bench。
- #18: `comm_concentration` は stat ではなく signal。`workload` / `doc_burden` / `professions` はどの preset にも入らず、定期出力の仕組みもない。
- #8: `current_med_period` / `possibly_deleted` は外部出力に出ていない。接続に出るのは `signal_type` の enum 名。
- #17: `text_candidates` を埋めると export allowlist で brain_export が全体失敗する。
- #7: SQLite 3.53 以降は `ALTER … ADD CONSTRAINT CHECK` が可（FOREIGN KEY は不可）。ただし system の 3.51.0 は不可で、配備が混在する。

## 5. zaitaku-calender との接続

契約: 送付単位 `mcs-ext-export/1`、認可 `mcs-ext-auth/1`、record 型 `mcs-read-model/1`（`mcs/ops/ext_contract.py:43-44`、`mcs/ops/export_schema.py:106-148`）。本文・statement・evidence 引用・送信者・患者名・病名は、`export_schema.py` の record ごとの allowlist（`_SCHEMAS`、:116-147。未知キーは拒否）によって構造的に送れない — ただし CD-9 で追加する `message_body` record は本文を明示的に運ぶ例外（オーナー判断 2026-09-29）。`FORBIDDEN_KEYS`（`ext_contract.py:46-52`）は、よくある本文系キーを早く見つけるための診断用拒否リストにすぎない。スキーマを変える場合（C2 を含む）は allowlist の変更として扱い、レビュー対象とする。

**受け入れる record 型（両文書で共通）**: `meta`・`coverage`・`message`・`message_body`（CD-9）・`patient_coverage`（CD-10）・`signal`・`signals_truncated`。`attachment`・`stat` は C1 では送らない。
- `fields` から外すと**除外ではなく build 全体が拒否される**（`record_type_unauthorized:<type>`: `ext_contract.py:217-218`）。brain_export の `export.jsonl` は stat と attachment を必ず含むので、C1 は前段で未許可 type を除去する（件数を出力）。ライブラリの既定（省略時は全 7 種: :201）は変えず、C1 プロファイル（CD-6）で強制する。
- `meta` は snapshot 時刻・世代の入力。`max_snapshot_age_s` は auth の項目で envelope にない（:258-271）ので、受信側の鮮度閾値は別途合意する（CD-6）。
- `coverage`・`signals_truncated`・`patient_coverage`（CD-10）は「未取込・取得未完了を『不明』と表示する」ための入力。`coverage.collection` は patients / messages / deleted / extraction_eligible / `patients_incomplete`（CD-4）の件数、患者別の取得状態は `patient_coverage` record（CD-10。Q10 決定済み: 送る）で出す。`auth.patients` を患者リストに絞ると、coverage・signals_truncated は envelope から除かれる（`ext_contract.py:230-235`）ので、C1 は `patients:"all"` にする。

**同一性キー（C0 で合意。両文書で同じ文言）**:
- `message`: 論理キー = (`organization_id`, `source`, `project_id`, `message_id`)。**`content_hash` は同一性に含めない**（含めると編集のたびに別行となり旧版が残り、supersede・撤回が壊れる。migration 後は forward-only で直せない）。`content_hash` と payload hash（`content_hash`・本文メタ（`body_sha256`・`body_format`・`body_truncated`・`sender_kind`）・facts・relations・extraction・state・`body_state`・`posted_at_ts` の canonical hash。範囲は §5 の C0 合意事項・CD-9/第2回修正案に同じ）は**変化検知**にだけ使う。`content_hash` は wire で任意（`null` あり: `export_schema.py:137-142`）で、NULL を含む列は UNIQUE キーに入れない（明示的な `'null'` 値へ正規化するか C0 で必須化を求める）。item の粒度は message 単位で、`facts[]` は配列のまま保持する。同じ kind の fact が 1 message に複数あってもよい（`facts: [_FACT]`、`export_schema.py:137-146`）。同じ論理キーで payload hash が異なる場合、より新しい `snapshot_generated_at` の行を current とし、旧行は受信側で `superseded_by` で結んで残す。遅れて届いた古い世代は current にならない。
- `signal`: 同一性 = `signal_type`＋`project_id`＋evidence の正規化 JSON の sha256。`detected_at` は属性として持ち、キーに含めない（signal には message_id・content_hash がなく、evidence 内の id は任意: `export_schema.py:123-135`）。evidence は任意で allowlist 外のキー（`med` 等）が落ち、同じ `message_ids` の複数 med は 1 件に畳まれる（CD-7）。件数一致は検証しない。signal は世代ごとの断面で、受信側では完全な世代（`signals_truncated` なし）に載らなくなったら「MCS 側で現在は検出されていない」と表示し、削除も「解決」扱いもしない。`signals_truncated` の世代では消滅か切詰めか判別できないので「不明」。
- `meta`・`coverage`・`signals_truncated`: envelope ごとの状態として 1 件ずつ保存し、同一性は `envelope_id`。
- `fact_id` は文言で変わるため参照用にとどめる（`mcs/semantic/semantic_facts.py:185-205`）。
- `source` は envelope のどのフィールドにも対応しない（`ext_contract.py:258-271`）。C0 で `destination` または `auth_id` へ対応付けるか、zaitaku 側の接続ラベルとするかを決める。

### C0 で決める契約事項（CD-1〜CD-10。両文書で同一）

詳細と根拠は [`roadmap/connector.md`](roadmap/connector.md) §2。

| ID | 決めること | 推奨 |
|---|---|---|
| CD-1 | 数値の canonical 表記と受信側の hash 検証 | RFC 8785（JCS）互換にする。規則は「整数値は整数表記（-0 は 0）、非整数は ECMAScript の数値表記（固定小数）。**指数表記になる値（|x|≥1e21・0<|x|<1e-6）・NaN・Infinity・safe integer 範囲外は拒否**」。Python は整数値 float を `1790000000.0`、非整数の小さい値を `1e-06` のような指数表記で出し、JS は `1790000000`・`0.000001` と出力するため（実測済み）、`_canonical` に ECMAScript `Number::toString` と同じ出力の数値フォーマッタを実装する（第2回計画レビュー修正）。**初回の実送信前が期限** |
| CD-2 | 分割集合の表現 | 各分割に meta・coverage・signals_truncated・**patient_coverage** を複製し、message / signal を排他的に分配（`message_body` は対応する message と同じ part）。任意項目 `part:{index,count,set}` を追加。**`set` は全 part の `records_sha256` を index 順に並べた canonical 配列の sha256** で分割集合を束縛し、受信側は (auth_id, generation, count, set) で集合化して異なる分割の混在を防ぐ（第2回計画レビュー修正）。受信側は集合が揃うまで「不完全」と表示 |
| CD-3 | `--only-with-facts` の意味と伝播 | 残す条件は facts 非空か tombstone。**CD-9 導入に伴い `message_body` を持つ message も残す**（facts なしの返信本文を落とさないため。2026-09-29・オーナー方針からの派生）。受信側は完全集合が届いた世代で、再掲されない前世代の staging を「MCS 側で現在は事実なし / 不明」に落とす。届かない返信を「返信なし」と表示しない。**降格は `fetch_state='complete'`・`history_floor` 非 null・`history_floor <= posted_at_ts <= coverage_ts`（検証済み範囲内）の item にだけ適用**し、floor より古い item は降格せず保持期限（30 日）で自然消滅（計画レビュー決定 2026-09-29、coverage_ts 境界は第2回で追加） |
| CD-4 | 取得完全性 | `coverage.collection` に `patients_incomplete`（`fetch_state≠'complete'` の件数）を追加。患者単位は CD-10 の `patient_coverage`（Q10 決定済み: 送る） |
| CD-5 | receipt | `mcs-ext-receipt/1`。envelope 単位の all-or-nothing、`rejected` は終端。採用件数・採用 / 却下は入れない。**receipt bundle**: 輸送上の便宜として NDJSON（1 行 1 receipt）を一括でやり取りでき、契約自体は不変（計画レビュー決定 2026-09-29） |
| CD-6 | C1 プロファイル | fields 7 種（`message_body`・`patient_coverage` を含む）を**明示列挙して必須化**（省略時の既定 `RECORD_TYPES` 拡張に新 record 型を含めない — 既存 auth が本文送付を暗黙許可しないため。第2回計画レビュー修正）、patients `"all"`、`max_snapshot_age_s` 必須（≤3600）、`retention_days` ≤ 30、meta・coverage 必須、stat・attachment は受信側でも拒否。producer と receiver の両方で強制。受信側は `snapshot_generated_at` と受信時刻で自前の鮮度閾値を持ち、超過は拒否でなく「古い」警告として age を常時表示する。**受信側閾値はテナント設定・既定 72 時間。マシン送信時の既定は C3 で決める**（計画レビュー決定 2026-09-29。「手渡し 24 時間・マシン 1 時間」の初期案は撤回）。窓付き送付 `since_days` は auth でなく hermes の config/CLI 引数で、受信側は `history_floor` で知る |
| CD-7 | signal 同一性 | 畳み込みを許容し明記。signal 件数の一致検証はしない |
| CD-8 | fixture 正本（Q7） | hermes-mcs を正本にし、zaitaku-calender へコピー。両 CI で `MANIFEST.sha256` を検証 |
| CD-9 | 本文 record `message_body` | `mcs-read-model/1` に新 record 型を追加（Q1 の決定による契約拡張。2026-09-29・オーナー。確定文言は計画レビュー決定 2026-09-29）: `message_id`（対応 message と同一世代・同一 part）・`body_text`（UTF-8 ≤8,192 bytes。`messages.body_text`（タグ除去済み）を送り、超過は送信側で UTF-8 文字境界で切詰め `body_truncated=true`）・`body_format`（enum。**v1 は `text` のみ**。`html` は予約語で受信側は拒否）・`body_sha256`（**送信した `body_text`（切詰め後）の UTF-8 bytes の sha256**。`content_hash`（本文 HTML の sha256）とは一致しない）・`body_truncated`・`sender_kind`（enum: `self_org` / `physician` / `nurse` / `care_manager` / `other_professional` / `patient_family` / `unknown`。`sender_kind` の `self_org` 判定は `mcs_signals._self_sets`（config の `signals.self_*` + `self_profile_v1` 既定値）を根拠にし、それ以外は profession/sender_type のキーワード写像、他組織の薬剤師・複数所属は `other_professional`（`self_org` を断定しない）。写像表は `docs/external-export-contract.md` に置く。氏名・個人特定属性は送らない）。**送出条件（第2回計画レビュー修正）**: `body_state='full'` かつ `body_text` 非 null の message にだけ付ける（`snippet`/`unknown`/`deleted` は付けない → 受信側は「内容未取得」表示。`body_text=''` の file-only 投稿は空本文を送る）。`content_omitted` は投影の省略印で本文条件ではない。auth `fields` の明示列挙がある場合のみ許可（CD-6）。本文は自由文で PHI を含み得るため、受信側の staging は暗号化・read 監査が必須（zaitaku-calender `docs/adr-external-ingest-v1.md`） |
| CD-10 | 患者単位の完全性 record `patient_coverage` | `mcs-read-model/1` に新 record 型を追加（Q10 決定 2026-09-29・オーナー: 送る）: `project_id`・`fetch_state`（enum: pending/complete/incomplete）・`coverage_ts`（**検証済み履歴取得範囲の上端 epoch 秒** — 「最終取得試行時刻」ではない。`ledger.coverage_ts()` の値で、0/未設定は null。第2回計画レビューで実装との不一致を訂正）・**`history_floor`（integer | null。オーナー判断 2026-09-29 で v1 に追加 — fixture 固定後の追加は契約 `/2` が要るため。窓付き送付を可能にする）**: ledger の `patients.history_floor` を写像（完了記録なし → null、-1 → 0＝先頭まで取得済み、正 → その epoch 秒。窓付き送付時は `max(floor, 窓の開始 epoch)`）。受信側は (a) `fetch_state` が complete でない患者、または世代に `patient_coverage` が欠ける患者を患者単位の「不明」とし、(b) CD-3 の降格は `history_floor` 非 null かつ `history_floor <= posted_at_ts <= coverage_ts` の item にだけ適用する。allowlist 追加は review 対象 |

C0 の合意事項は CD-1〜CD-10 のほか、次を含む（両文書で同一。zaitaku-calender `ROADMAP.md` §4.7）: 受け入れ record 型（7 種）、同一性キー（上記）、拒否コード表、**撤回指示書の形式と輸送**（`mcs-ext-withdraw/1` 提案: `contract`・`envelope_id`・`auth_id`・理由コードのみ、4 KiB 以下、自由文なし。hermes `withdraw` が指示書ファイルを outbox に原子的に生成し、zaitaku のアップロード画面で受領して削除 receipt を返す — 現行は `sink.delete` のみで相手に届かない（第2回計画レビュー修正）。withdraw が原本より先に届く場合と part 分割の一部だけが withdraw された世代の扱いも決める）、**item の版管理用 payload hash の範囲**（`content_hash`・本文メタ・facts・relations・state 等の canonical hash。本文のみの変更を別版にする。第2回計画レビュー追加）、受信側の鮮度閾値（CD-6 参照）、サイズ上限の扱い（受信 wire bytes で 1,048,576 B。手渡しは canonical 出力をそのまま運び wire ≡ canonical。上限ちょうどの受理と +1 byte の拒否を固定。`docs/roadmap/connector.md` §2 (5)）、`source` の対応付け（同一性キー参照）。

### フェーズ（hermes-mcs 側の成果物）

| フェーズ | hermes-mcs 側の成果物 | 依存 | 規模 |
|---|---|---|---|
| **C0 契約合意（両 repo 共同）** | CD-1〜CD-10 と C0 合意事項（撤回指示書・受信側鮮度閾値・サイズ上限・`source` 対応付け・`content_hash` の null 取扱い）の合意。旧 #25（iCal）を廃止して本接続に置換。参照実装の変更（数値正規化・`part`・profile・`parse_receipt`・coverage 拡張・`message_body`/`patient_coverage` record 追加）、合成 fixture 一式（受理 12・拒否 23（`15` は生成のみ・コミットしない）・receipt 6・withdraw 3。connector.md §2(2) の一覧と一致。本文・patient_coverage・part.set・payload hash・境界数値ケースを含む — 第2回計画レビューで拡張）と `MANIFEST.sha256`、drift guard（F-4）。**完了条件に Q6 の判断記録を含める** | zaitaku-calender C0（同時）、#8-D2（決定済み: 現行名維持）、**残りは CD-1〜CD-10 の合意**（Q1〜Q4・Q7・Q8(b)・Q10・Q11 は 2026-09-29 決定済み、Q6 は記録済み） | M |
| **C1 手渡し取込（未採用 staging）** | `ext_contract.main()` の subcommand 化（deliver / reconcile / withdraw / health / handoff / link-hints）、`select_records`（前段除去と `--only-with-facts`）、`split_envelopes`（1 MiB）、`HandoffSink`（自己 ack しない）、`rejected` 終端と `_valid_ack` の status 検査（F-3）、health。E2E は合成 ledger で完結 | C0、CD-1。**本番投入は #4 の実施記録と Q6 の判断後だけ**。合成での開発・テストは切り離して進めてよい | L |
| **C2 採用導線** | 契約改訂（`mcs-ext-auth/2`、`scope` の第 3 値）と Q9 の承認が前提。**承認されるまで hermes-mcs 側の成果物なし**。型付き値の正本は #15 | zaitaku-calender C2、Q9。zaitaku-calender C2 は hermes-mcs C2 を待たない | L |
| **C3 マシン送信（任意）** | `ext_transport.py`（`HttpsSink`、endpoint policy、Keychain token）。実行は手動のまま。自動再送はしない | Q6・Q8、契約付録、C1 | M |
| **C4 hermes-mcs 通知の縮退（任意）** | `notify.show_patient_names`（既定 true）。名前の出力は 4 ファイル（`notify_render.py`、`notify_cards.py`、`notify_flush.py`、`semantic/semantic_render.py`）。日次 digest の Work Queue 代替を再評価（Q5） | zaitaku-calender C2 の運用実績、Q5 | S |

共通ルール:
- 実行は手動のみ。tick に組み込まない（`docs/external-export-contract.md:136`）。
- 結果不明は held のまま receipt と照合する。自動再送しない。
- 同一性は上記「同一性キー」に従う。
- `facts[]` は semantic 層の current な成果物（`semantic_facts_v4`、なければ `canonical_projection`）があるときだけ埋まる（`mcs/views/read_model.py:209-218`）。`canonical_projection` も semantic 層の artifact で、`config.json` の `semantic` 設定によるゲートがある（`mcs/semantic/semantic.py:1-15`）。facts が空でも、message の存在・状態（`content_hash`・`body_state`・extraction 状態・`parent_id`）、coverage、signal は届く。
- 返信の検知は新しい record 型を作らず、`message.parent_id` と既存 signal で表す。ただし `pharmacist_request_unanswered` は「薬剤師宛の依頼に自組織の投稿がない」検知で、他職種の返信到着ではない（`mcs_signals.py:472-523`）。

## 6. zaitaku-calender へ移す・既に実装済みの機能

hermes-mcs では今後作らない。「旧版の出典」は v2 #N と優先順位 N。

| 旧版の出典 | 項目 | 扱い | 根拠（zaitaku-calender） |
|---|---|---|---|
| 優先順位 2、v2 #2 | 依頼 lifecycle・Discord 完結 | **Discord 完結は廃止**（zaitaku-calender の `src` に Discord への参照はない）。依頼・期限・再開は zaitaku-calender の follow-up/handoff と Work Queue で扱う（担当は患者の担当薬剤師。follow-up event に担当者の列はない）。hermes-mcs の依頼台帳は、担当・期限・状態が既存（`mcs/ops/mcs_requests.py:15-24`、検証付き編集は同 :99-111）。新しい lifecycle 機能（ack 状態・Discord 完結・返信の自動検知による状態変更）は追加しない。既存の `request_overdue`/`request_aging` signal（`mcs/ops/mcs_signals.py:790-791`）と `st_open_loop_aging`（`mcs/views/mcs_stats.py:584-640`）は凍結台帳の範囲で維持する（廃止する場合は別途判断）。MCS スレッドの返信は §5 のとおり `message.parent_id` と既存 signal で渡す | `src/worker/routes/visits/visits.ts:83-101`、`migrations/0065_visit_follow_up_events.sql:15-45`、`src/shared/domain/work-items.ts:12-17` |
| 優先順位 7（PDF 部分）、v2 #4 | PDF 報告書・訪問報告ドラフト | 訪問ごとの報告書・PDF は実装済み。**月次まとめは zaitaku-calender G-RPT-1（P1、設計 gate 待ちで未着手）**。hermes-mcs はドラフト生成をしない。集計部分は #18 | `src/worker/routes/reports/reports.ts:34-36`、`src/shared/reports/*`、`docs/plans/implementation-plan.md:153, 244-269` |
| v2 #6 | 確認済み現行薬リスト | 実装済み（処方正本） | `src/worker/routes/prescriptions/*` |
| v2 #22 | 引継ぎサマリ | 訪問単位の引継ぎ記録は実装済み。**患者の現状・注意点・未解決事項を 1 ページにまとめるサマリは未実装**（必要なら G-BCP-1 の持ち出しリストと合わせて zaitaku-calender で検討） | `src/app/features/resources/VisitHandoffPanel.tsx:32-34`、`docs/plans/implementation-plan.md:145, 207` |
| v2 #24 | 複数薬剤師対応 | 担当の割り当て（`assigned_pharmacist_id`）と役割ベースの権限は実装済み。**職員別の不在・代理は G-SCH-2（P1、未着手）**。hermes-mcs 側の通知ルーティング・個人別ダイジェストは §7 で廃止 | `src/shared/authorization-policy.ts`、`src/shared/domain/work-items.ts:17`、`docs/plans/implementation-plan.md:147` |
| v2 #26 | 算定・報告提出状況 | 一部実装済み（訪問単位の残務 `billing_confirmation_pending`・`care_manager_report_pending`・`physician_report_pending`）。**月次の追跡は G-BILL-2（P2、未着手）。算定要件未達（回数上限）の判定は OUT** | `src/shared/domain/work-items.ts:12-14`、`docs/plans/implementation-plan.md:156, 318` |
| v2 #5, #11, #12, #14, #15, #17 | トレーシングレポート、腎機能用量、相互作用、疑義照会台帳、リコンシリエーション、アドヒアランス介入 | zaitaku-calender の候補。hermes-mcs は既存抽出で得られる言及を C1 の facts として渡すだけ | zaitaku-calender `ROADMAP.md` §5 |
| v2 #19 | 終末期の麻薬準備・変化点検知 | 麻薬の準備は zaitaku-calender G-NAR-1（P3）。hermes-mcs は専用の変化点検知を作らない。eol は既に `extract_llm` の events にある（`mcs/extract/v4/extract_llm.py:92`）。read-model の fact kind（`care_event` 等）として eol を区別して渡せるかは C0 で確認し、渡せない場合は新しい record 型を作らない | zaitaku-calender `ROADMAP.md` §5、`docs/plans/implementation-plan.md:160` |
| v2 #8, #13 | 訪問前ブリーフ、症状×副作用照合 | 接続で扱う（C1/C2）。`next_planned` を予定の根拠にしない | `src/app/features/visit-workspace/VisitClinicalReferences.tsx` |
| v2 #21 | 関係職種ディレクトリ | **接続では扱わない**。送信者は allowlist にない（`export_schema.py:116-147`）ため送れず、read-model に職種・組織の record もない。zaitaku-calender の既存 care-team 手入力で吸収する | `PatientCareTeamPanel.tsx`、`src/worker/routes/patients/patients.ts:86-88` |

既存機能の扱い:
- **Discord/Slack カード**: MCS 取得通知として維持する（チャネル単位の既存配送のまま）。患者名の除去は C4。
- **signals（11 種）**: MCS 内部のシグナルとして維持し、C1 で `signal` record として渡す（`export_schema.py:123-128`）。
- **依頼台帳（`mcs/ops/mcs_requests.py`）**: 既存機能（担当・期限・状態・人承認ゲート `--confirm-human`・`reason`・receipt）のまま凍結。新しい lifecycle 機能は追加しない。

## 7. 見直し・廃止した項目

| 旧版の出典 | 項目 | 理由 |
|---|---|---|
| 優先順位 4、v2 #23 | Markdown timeline・読み取り専用 Web UI | UI は zaitaku-calender にある。PHI の表示面と認証境界を増やさない |
| v2 #10（時系列ビュー部分） | 検査値の時系列ビュー | 同上。hermes-mcs は抽出のみ（#15）。zaitaku-calender でも候補にしない（zaitaku-calender `ROADMAP.md` §5） |
| 優先順位 8 | 複数端末への snapshot 配布 | PHI の置き場所が増えるだけ |
| v2 #7 | MCS 返信ドラフト | 取得の範囲外。LLM による臨床文生成になる |
| v2 #24（hermes-mcs 側） | 通知の担当者別ルーティング・個人別ダイジェスト・担当不在時の代理 | 担当と不在の正本は zaitaku-calender（G-SCH-2）。hermes-mcs はチャネル単位の既存通知を維持し、担当者別の配送は作らない（オーナー確認: §9） |
| 優先順位 2、v2 #2（Discord 完結） | カードボタン⇄依頼台帳の完全連動 | 依頼の正本は zaitaku-calender（§6） |
| v2 #25 | iCal 出力 | 接続（C0〜C4）に置換 |
| v2 #29 | FHIR JP Core 出力 | 廃止（hermes-mcs でも zaitaku-calender でも作らない）。zaitaku-calender の G-EXT-1 は取込 adapter で FHIR 出力ではない（`docs/plans/implementation-plan.md:162`） |
| v2 #28 の双方向化 | zaitaku-calender での採用・却下を hermes-mcs へ戻す | 逆方向の PHI の流れ。双方向連携の契約（zaitaku-calender `docs/domain-model-decision.md:206`）と別承認が要る。再開する場合は両文書で新フェーズとして合意 |
| 旧「並び順の考え方」 | 報告書・薬剤師動線を上位にする前提 | 役割分担で不成立。取得完全性 → 抽出品質 → 接続の順に変更 |
| 旧 #10 の全体レビュー | 独立実装レビュー（全体） | 前提（2026-09-19 時点の修正）が陳腐化し、既存レビューが複数ある。縮小版（末尾 18 コミット）に置換（#10） |

## 8. 実施計画（フェーズ別）

依存の判定根拠と衝突マトリクスは §10 と各詳細計画にある。番号は §4 の項目、`F-n` は §3、`C0〜C4` は §5。

### Phase 0 — 判断と測定（コードなし。今すぐ）

- **判断**（§9 の「決める時点」が Phase 0 のもの）: `#1-D1〜D6`（FileVault の有効化を含む）、`#9-D1〜D3`、~~`#8-D2`~~（決定済み: wire enum 名は現行名のまま）、`#10-D1〜D2`、~~Q7~~（決定済み: fixture 正本は hermes-mcs）、**接続の C0 残りは CD-1〜CD-10 の合意のみ**（Q1〜Q4・Q7・Q8(b)・Q10・Q11・#8-D2・`prev_content_hash` 追加しない、は 2026-09-29 決定済み。**Q6 は記録済み**・許可・本文含む、zaitaku-calender `docs/adr-external-ingest-v1.md` §4）、CD-1〜CD-10 と C0 合意事項（撤回指示書・受信側鮮度閾値・サイズ上限・`source` 対応付け・`content_hash` の null 取扱い）、`#12-D5`（F-2 の扱い）。
- **測定**（オーナーが snapshot に対して実行。実データの集計になるので調査側では未実行）: `runs` の所要時間分布（#5）、text 経路の held 件数（#6）、`ledger_audit` の初回（#7 Step 0）、現在の DB サイズ（#1）。
- 目安: 実装は不要。Phase 1 の着手前提を揃える。

### Phase 1 — 先に直す・小さく安全（並行可・本番影響小）

- F-1（#15-A）、F-4（drift guard）、#9（SQLite probe）、#5 の送信下限 + #6 の restore ゲートの共通部分（S）、#2（fixture / ReplayWorker）。
- #19（ラベル基盤）‖ #12a（緊急度の表示。F-2）。`mcs_signals.py` を両方が触るのでマージ順に注意。
- #8-M1 + 命名（**#4 の再照合と C1 の本番より前**に入れる。先に入れないと最初の編集が痕跡なく消える）。
- C0（参照実装 + fixture）を zaitaku-calender C0 と同時に。#10 縮小レビューは並行（コード変更なし）。
- 完了の目安: 既存の誤表示（F-1・F-2）が是正され、以降の測定基盤（#2・#19）と契約の固定（C0）が揃う。

### Phase 2 — 復旧性と通知の土台

- #1a（`offsite` + `verify`）‖ #3（返信の分類）。触るファイルが分離している。
- #6 の残り（hold の理由、種別ごとの配送方針、resolver は実需確認後）→ #13（日次 digest。`mcs_coverage` の軽量版）。`notify_flush.py` / `run_check.py` / `CONFIG_RULES` が共通なので直列。
- #11 stage 1（合成辞書で先行）‖ #15-B/C の準備（RULE_VERSION 7 の窓を #4 と合わせる）。
- #7 Step 0 の 1 回目の結果を受けて Step 1 の要否を判断。

### Phase 3 — 運用の耐久性と接続の実装

- #5 の残り（非通信ステージの上限・health・watchdog）。
- #12b（shadow）。#13 のマージ後。
- #18（`coverage_gaps` の範囲版 + レポート + cron）。#1b / #1c（cron・health・文書・初回の実機 drill）。#7 Step 1（guard trigger。shadow → enforce）。
- **C1（L）**: G1 → G2 → G3 → G4 → G5。合成環境で完結する。

### Phase 4 — 本番投入と前提の完了

- **#4 修復**（`plan` → 実行 → 記録）: #1a・#3・#5・#8-M1 が揃ってから。RULE_VERSION 7（#15-B）は同じ窓で 1 回にまとめる。
- **C1 の本番投入**: #4 の実施記録と Q6 の判断後だけ。#10 の縮小レビューが完了していること。
- #17 第 1 版、#12b の shadow → on の判定、#16 stage 1（承認後）、C4。

### Phase 5 — 任意・後段

- #14（#11 の完了と #19 のラベルが十分に貯まってから）、#16 stage 2/3、#15-D/E、#17 第 2 版、C3、#7 Step 2、C2（Q9 の承認後だけ。着手を勧めない）。

## 9. オーナー判断一覧

### 接続（zaitaku-calender と共通の番号）

決める時点: **C0 の残りは CD-1〜CD-10 の合意のみ**（Q1・Q2・Q3・Q4・Q7・Q8(b)・Q10・Q11 は 2026-09-29・オーナー決定済み。Q6 は記録済み: 許可・本文を含む staging の受入・保存、zaitaku-calender `docs/adr-external-ingest-v1.md` §4。実データ投入と本番投入は zaitaku-calender `ROADMAP.md` §10.3 のゲートの対象のまま。合成 fixture の開発は進めてよい）。C2 までに Q5・Q9、C3 前に Q8(a)。

1. ~~本文なしで足りるか~~ → **決定済み（2026-09-29・オーナー）**: 本文を送る。`mcs-read-model/1` に `message_body` record を追加（CD-9）。`docs/external-export-contract.md`・`export_schema.py`・両側 fixture の改訂が要る。
2. ~~semantic 層（`semantic_facts_v4` または `canonical_projection`）を常時動かすか~~ → **決定済み（2026-09-29・オーナー）**: 常時動かす。facts が届く前提で fixture を作る。
3. ~~kind×project_id の PHI としての扱いと保持期限~~ → **決定済み（2026-09-29・オーナー）**: PHI として扱う（本文を含むため staging は PHI）。保持 = min(envelope の `retention_days`, 30 日)、起点は `received_at`（zaitaku-calender `docs/adr-external-ingest-v1.md` §3-4）。`content_hash` は本文 HTML の sha256 で、短文・定型文は推測可能（`mcs/core/ledger.py:1055`）。
4. ~~患者対応付けと採用を pharmacist に限るか、clerk にも許すか~~ → **決定済み（2026-09-29・オーナー）**: 対応付け・採用とも clerk にも許す（capability で制御し職種強制はしない。zaitaku-calender `ROADMAP.md` §4.5）。
5. Work Queue に「未確認の staging 行あり」を導出コードとして足すか（zaitaku-calender `docs/plans/implementation-plan.md:318` との両立）。C4 の digest 代替の可否もこれに従う。
6. MCS から取得したデータを別システムへ転送・保存することが許されるか。根拠は zaitaku-calender `docs/domain-model-decision.md` の外部連携条項（接続先 ID と確認済み内部 ID の明示対応: L182、双方向連携の事前契約: L206）、MCS 利用規約、患者同意、院内規程。SHR-10／SCP-07（zaitaku-calender `docs/specs/visit-report-spec-v1.md:637, 82`）は「MCS への書き戻しをしない」ことの根拠としてだけ使う。**決まるまで C1 の本番投入（実データによる最初の envelope 作成と zaitaku-calender 本番へのアップロード）以降に進まない**。合成 fixture による開発・テストは進めてよい。**記録済み（2026-09-29・オーナー: 許可。本文を含む `message_body` を含む staging の受入・保存、zaitaku-calender `docs/adr-external-ingest-v1.md` §4）**。
7. ~~fixture の正本をどちらに置くか~~ → **決定済み（2026-09-29・オーナー）: hermes-mcs を正本**（CD-8 の推奨どおり。zaitaku-calender へコピーし `MANIFEST.sha256` で一致確認）。あわせて確定: `#8-D2` wire enum 名は現行名のまま（改名しない）、`prev_content_hash` は追加しない。
8. (a) C3 の構成（D1 直接 binding か RPC か、mTLS 必須か（`bounded_http` は Bearer 固定で mTLS・Cloudflare Access 系ヘッダに未対応）、専用 Worker の権限を書込みのみに絞るか、Access service token かアプリ層 Bearer か。zaitaku-calender `ROADMAP.md` §4.9）。(b) ~~zaitaku-calender の P0 より先に接続へ着手するか~~ → **決定済み（2026-09-29・オーナー）: P0 完了後。適用範囲はコード実装と本番投入**（zaitaku 側 S2 以降、C1 本番投入）。C0 の契約合意・fixture 固定・hermes-mcs 側の参照実装変更（CD-9・CD-10 等）は進める。
9. 型付き値（allergy・ADE・vital_lab の値）の送付を認めるか。認める場合は `docs/external-export-contract.md` の detail 禁止条項（L22・L137）の改訂とオーナーの明示承認が要る。hermes-mcs C2 の前提。
10. ~~患者単位の取得完全性を zaitaku-calender へ送るか~~ → **決定済み（2026-09-29・オーナー）: 送る**。新 record 型 `patient_coverage`（`project_id`・`fetch_state`・`coverage_ts`。allowlist の変更）を CD-10 として起票し `export_schema.py` に追加する。全体件数（`patients_incomplete`、CD-4）と併せて患者単位の「不明」を出せる。
11. ~~人が MCS のルームを開いて `project_id` から患者を特定する導線~~ → **決定済み（2026-09-29・オーナー）**: テナント設定の MCS ベース URL から `project_id` 単位のリンクを組み立てる。message 単位の直リンクは未確認のため約束しない。
12. 真正性: 署名（`mcs-ext-export/2`、Worker secret の HMAC 等）が要るか。契約改訂とオーナー承認が要る。決まるまで zaitaku-calender C1 は人が真正性を担保し、画面に upload 者と source を表示する（zaitaku-calender `ROADMAP.md` §4.1）。

**追加の決定済み事項（2026-09-29）**:
- オーナー判断: (A1) タイムラインの行粒度は 1 message = 1 行（`occurred_on`・`sort_at` は MCS 投稿日 `posted_at_ts` 由来。受信日ではない）。(A2) `patient_coverage` に `history_floor` を v1 で送る（CD-10 更新。fixture 固定後の追加は契約 `/2` が要るため。窓付き送付を可能にする）。(A3) 履歴タブのタイムライン表示条件を `visit:read` に緩め、kind 単位の capability で行を制御（zaitaku 側 S5 で実装確認）。(A4) envelope 原本は保持しない（`records_sha256`・受信 bytes の sha256・receipt・envelope メタのみ残し、本文は item 側の暗号文にのみ持つ）。
- 計画レビュー決定（負担軽減方針）: receipt bundle（NDJSON 一括、CD-5）、`reconcile --receipts PATH` と `handoff`・`link-hints` の各 subcommand と `config.json` の `ext_export` プロファイル（connector.md §3 D）、`since_days` 窓付き送付と鮮度閾値のテナント設定・既定 72h（CD-6）、降格の `history_floor` 適用範囲（CD-3/CD-10）。zaitaku 側の表示・認可の決定（バッジ文言・tombstone 非表示・状態 1 行・概要タブの MCS カード・read 監査単位・adopt⇒read 含意）は zaitaku-calender `ROADMAP.md`・`docs/adr-external-ingest-v1.md` を正とする。
- **第2回計画レビュー（2026-09-29）の契約修正案 — CD 合意待ち**: §5 の CD 表に「第2回計画レビュー修正」とある箇所が対象。(1) CD-1 は指数になる値の拒否と ECMAScript `Number::toString` 互換フォーマッタを要する（Python repr が `1e-06` を出す実測差）。(2) CD-2 は `part` に `set`（全 part の `records_sha256` 配列の canonical hash）を足して分割集合を束縛し、別分割の混在を拒否。(3) CD-9 は `body_state='full'` かつ `body_text` 非 null のみに送出を限定し（`content_omitted` は本文条件ではない）、`fields` の明示列挙を必須化（CD-6）して既存 auth の暗黙許可を防ぐ。`sender_kind` の `self_org` は `mcs_signals._self_sets` のみを根拠にする。(4) CD-10 の `coverage_ts` は「検証済み履歴取得範囲の上端」で「最終取得試行時刻」ではない（実装との不一致を訂正。降格は `history_floor <= posted_at_ts <= coverage_ts` の範囲）。(5) 撤回指示書 `mcs-ext-withdraw/1` は hermes `withdraw` が outbox に原子的に生成する手渡しファイル（現行 `sink.delete` のみでは相手に届かない）。(6) item の版管理 payload hash に本文メタ（`body_sha256` 等）を含める。(7) サイズ上限は受信 wire bytes で固定（手渡しは canonical ≡ wire）。(8) fixture を受理 12・拒否 23（`15` は生成のみ・コミットしない）・receipt 6・withdraw 3 に拡張。zaitaku 側の対応修正（タイムライン fingerprint・MCSを除く・新着定義・signal の message 参照 id・フラグ off 時の withdraw/purge 継続・採用先 capability）は zaitaku-calender `ROADMAP.md`・`docs/adr-external-ingest-v1.md` を正とする。

### 項目別（詳細は各詳細計画の「オーナー判断・リスク」）

| ID | 判断内容 | 決める時点 |
|---|---|---|
| #1-D1〜D6 | 医療情報の外部保存の許可 / オフサイト先 / 鍵エスクロー / OS 同梱 openssl を新しい外部依存に含めるか / 世代と RPO / **FileVault の有効化** | D1・D6 は今すぐ。他は #1a の着手前 |
| #2-D1 | thread の `paginate` 欠損を fail-closed にするか | #2 の実装時 |
| #3-D1 | 恒久欠落があっても floor を確定させるか（`known_gaps` 併記が前提） | #3 の実装前 |
| #4-D1〜D3 | 疑わしい floor の扱い / MCS への GET 負荷 / 記録・sign-off の担当 | #4 の実行前 |
| #5-D1〜D2 | watchdog の採否と猶予 / `publish_snapshot` の頻度 | #5 の手順 5 の前 |
| #6-D1〜D2 | at-least-once にする種別 / 既存の held の扱い | #6 の実装前 |
| #7-D1〜D3 | real FK（Step 2）を許容するか / 旧違反の扱い / 実 DB の監査の実行と記録範囲 | Step 1 の前 |
| #8-D1〜D2 | 本文保持の可否と範囲 / wire enum の改名（A 維持 / B 改名） | D2 は C0 の fixture 固定前 |
| #9-D1〜D3 | warn か error か / runtime 更新の時期 / system python 3.51.0 の容認 | 今すぐ |
| #10-D1〜D2 | 縮小レビューの採否 / reviewer の系統と席 | C1 本番投入の前 |
| #11-D1〜D5 | 辞書の出所と利用条件 / stage 1 のみか集約まで / YJ 等コード保持 / stats の新指標を C2 で出すか / alias の保守者 | D1 は先行。他は stage 1 の前 |
| #12-D1〜D5 | 対象を LLM のみか / after_min・repeat_min・max_repeats / 専用チャネル / 本文抜粋 / **ルール由来の警告表示を弱めるか（F-2）** | D5 は今すぐ。他は 12b の前 |
| #13-D1〜D5 | 患者名 / 「未読の多職種連絡」の定義 / 送信時刻・休日・空日 / signals digest との統合 / 維持コスト | #13 の前 |
| #14-D1〜D3 | 閉じる条件 / 窓外でも閉じてよいか / on の判定基準 | #14 の shadow 前 |
| #15-D1〜D5 | `recent_labs` の保持数 / weight・height / 型付き値を C2 で出すか / RULE_VERSION bump の時期 / Jev QC の適用範囲 | D4 は #4 と合わせて。他は B / C の前 |
| #16-D1〜D4 | 対象範囲 / OCR テキストの保持・表示範囲 / macOS 標準機能の subprocess 利用の承認 / `pruned` の扱い | 着手前 |
| #17-D1〜D3 | 小セル閾値 k と職種群 / `text_candidates` を出すか / 職種 map | 第 1 版の前 |
| #18-D1〜D5 | 読者と配信先 / 週次・月次の切り方 / 休日 / 自施設と全体の分割 / 保持期間 | #18 の前 |
| #19-D1〜D4 | 理由語彙 / digest 型の却下 UI / ack を採用に数えるか / n の下限 | #19 の前 |
| C4-D1〜D2 | 縮退の範囲（名前のみ / 投稿者名も / 本文・要約も）/ 既定値 | C4 の前 |
| CD-1〜CD-10 | §5 の契約事項 | C0（CD-1 は初回の実送信前が期限） |

旧版から引き継ぐ判断:
- 独立実装レビュー（#10）を、縮小版で行うか。
- §7 の v2 #24（担当者別ルーティング等の廃止）を確定するか。
- 本番の再取込・既存 floor の変更は未実施（旧版 2026-09-19 追加検証）。#4 で扱う。

## 10. 実施上の共通事項

- **完了時の共通チェック**: `scripts/run_tests.sh tests/<領域>/`（`integration/` は必要な対象を同 runner に指定）、CI と同じ範囲の ruff（`make lint`）、`python3 scripts/update_readme.py --check`、`python3 ci/gates.py`、`python3 ci/mine_gates.py --check`。新規の `mcs/**/*.py` または `tests/**/test_*.py` は `docs/DEVELOPMENT.md` の生成表を変えるので、`scripts/update_readme.py` の再実行が必要（CI の PR 検査が drift を落とす）。
- **書込み位置**: 新規に `Ledger(` を開けるのは `ci/gates.py:23` の `LEDGER_WRITERS` だけ。書込みを伴う新機能は run_check の tick が持つ `ledger` を引数で受ける関数にする。監査・plan・drill・レポートは読取り専用接続（`mode=ro`）で作る。
- **実機に触れない**: テストは一時 DB + stub と完全合成の fixture だけ。実 MCS・Discord・Keychain・原本 DB・ローカル LLM・Jev に触れる確認（プローブ・実機 drill・実 DB の監査）は、オーナー実行かオーナーの明示承認の下で行う。
- **配備の作法**: `hermes_plugin/` を変えたら `hermes gateway restart`、watchdog（`deployment/recovery/mcs_recover.py`）を変えたら `./install.sh --no-brew --no-llm --no-plugin --no-services`。`deployment/` の変更だけでは実機に適用されない。
- **衝突マトリクス**（同じファイルを複数項目が触る。直列でマージする）:

| ファイル / 領域 | 触る項目 | 順序 |
|---|---|---|
| `mcs/core/ledger.py`（加法 migration。version 据え置き） | #8-M1（`message_revisions`、`_upsert_message`）、#3（`fetch_jobs.error`）、#6（`hold_reason`）、#7 Step 1（guard trigger） | #8-M1 → #3 → #6 → #7 Step 1 |
| `mcs/notify/notify_flush.py` / `mcs/ingest/run_check.py` / `mcs_setup.CONFIG_RULES` | #5 の下限、#6、#13、#12b、#5 の残り、#1b | #5 下限 + #6 の共通部分 → #6 の残り → #13 → #12b → #5 の残り |
| `mcs/views/mcs_stats.py`（REGISTRY と生成 docs） | #11 stage 1、#17、#18、#19 | 直列 |
| `mcs/ops/mcs_signals.py`（`evaluate`） | #12a（述語置換）、#19、#14 | #12a と #19 はマージ順に注意 → #14 |
| `mcs/ops/ext_contract.py` / `export_schema.py` / `read_model.py` | C0〜C3、F-3・F-4、#8 の `prev_content_hash`（採用時） | フェーズ順。#11・#15・#16 は allowlist を変えない |
| `mcs/extract/v1/extract.py`（`RULE_VERSION`） | #15-B、#12c | 全 v1 が再生成されるので 1 回にまとめ、#4 の再生成窓と合わせる |
| `mcs/extract/rollup.py`（`PERIOD_CHECK_VERSION` 2→3） | #8 の命名、#11 stage 1 | 同一リリースで 1 回の bump にまとめる |
| `mcs/ops/mcs_setup.py`（`check_environment`・`CRON_JOBS`） | #1、#9、#13、#12b、#17、#18 | 直列 |

## 11. 完了条件

- 取りこぼし・未完了・失敗を正しく記録し、再実行と復元で回復できること（旧版から維持。「もっと賢く要約できる」ことは条件にしない）。
- 復元訓練を 1 回以上実施し、記録が残っていること（人手 drill でエスクロー鍵の正しさを確認）。#4 修復の実施記録があること。
- 既存の誤表示（F-1・F-2）が是正され、回帰テストがあること。
- 接続: 取得未完了を zaitaku-calender 側で「記録なし」ではなく「不明」と表示できること。C1 の入力は `coverage`（`patients_incomplete`: CD-4）・`patient_coverage`（患者単位: CD-10）・`meta`・`signals_truncated`。本文は `message_body`（CD-9）のみを経由し、それ以外の経路で本文が出ないこと（`export_schema.py` の allowlist と fixture で固定）。結果不明の送付と撤回が `sent`（held）／`delete_held` として残り、receipt で照合できること。Q6 の判断記録が C1 の本番投入より前にあること（記録済み: 2026-09-29）。
