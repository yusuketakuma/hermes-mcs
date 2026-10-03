# 接続契約・オーナー判断の引継ぎ記録

改訂日: 2026-10-02。v1.0.10のROADMAPから、接続契約と判断の本文を移設した。
共通ID・決定内容・合意待ちの境界を保持する。本文中の行番号・相手側実装の状況は当時の調査記録であり、現在の実装完了の証明ではない。
現行の優先順位は[ROADMAP](../ROADMAP.md)、接続の実装計画は[connector.md](connector.md)を参照。
スタンプの個人別データを送る権限・record型はこの移設では追加しない。

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

詳細と根拠は [`roadmap/connector.md`](connector.md) §2。

| ID | 決めること | 推奨 |
|---|---|---|
| CD-1 | 数値の canonical 表記と受信側の hash 検証 | RFC 8785（JCS）互換にする。規則は「整数値は整数表記（-0 は 0）、非整数は ECMAScript の数値表記（固定小数）。**指数表記になる値（|x|≥1e21・0<|x|<1e-6）・NaN・Infinity・safe integer 範囲外は拒否**」。Python は整数値 float を `1790000000.0`、非整数の小さい値を `1e-06` のような指数表記で出し、JS は `1790000000`・`0.000001` と出力するため（実測済み）、`_canonical` に ECMAScript `Number::toString` と同じ出力の数値フォーマッタを実装する（第2回計画レビュー修正）。**初回の実送信前が期限** |
| CD-2 | 分割集合の表現 | 各分割に meta・coverage・signals_truncated・**patient_coverage** を複製し、message / signal を排他的に分配（`message_body` は対応する message と同じ part）。任意項目 `part:{index,count,set}` を追加。**`set` は全 part の `records_sha256` を index 順に並べた canonical 配列の sha256** で分割集合を束縛し、受信側は (auth_id, generation, count, set) で集合化して異なる分割の混在を防ぐ（第2回計画レビュー修正）。受信側は集合が揃うまで「不完全」と表示。**先に揃った完全集合を current とし、別 `set` の後着 part は `409`/`generation_set_conflict` で拒否（第3回）** |
| CD-3 | `--only-with-facts` の意味と伝播 | 残す条件は facts 非空か tombstone。**CD-9 導入に伴い `message_body` を持つ message も残す**（facts なしの返信本文を落とさないため。2026-09-29・オーナー方針からの派生）。受信側は完全集合が届いた世代で、再掲されない前世代の staging を「MCS 側で現在は事実なし / 不明」に落とす。届かない返信を「返信なし」と表示しない。**降格は `fetch_state='complete'`・`history_floor` 非 null・`history_floor <= posted_at_ts <= coverage_ts`（検証済み範囲内）の item にだけ適用**し、floor より古い item は降格せず保持期限（30 日）で自然消滅（計画レビュー決定 2026-09-29、coverage_ts 境界は第2回で追加） |
| CD-4 | 取得完全性 | `coverage.collection` に `patients_incomplete`（`fetch_state≠'complete'` の件数）を追加。患者単位は CD-10 の `patient_coverage`（Q10 決定済み: 送る） |
| CD-5 | receipt | `mcs-ext-receipt/1`。envelope 単位の all-or-nothing、`rejected` は終端。採用件数・採用 / 却下は入れない。**receipt bundle**: 輸送上の便宜として NDJSON（1 行 1 receipt）を一括でやり取りでき、契約自体は不変（計画レビュー決定 2026-09-29） |
| CD-6 | C1 プロファイル | fields 7 種（`message_body`・`patient_coverage` を含む）を**明示列挙して必須化**（省略時の既定 `RECORD_TYPES` 拡張に新 record 型を含めない — 既存 auth が本文送付を暗黙許可しないため。第2回計画レビュー修正）、patients `"all"`、`max_snapshot_age_s` 必須（≤3600）、`retention_days` ≤ 30、meta・coverage 必須、stat・attachment は受信側でも拒否。producer と receiver の両方で強制。受信側は `snapshot_generated_at` と受信時刻で自前の鮮度閾値を持ち、超過は拒否でなく「古い」警告として age を常時表示する。**受信側閾値はテナント設定・既定 72 時間。マシン送信時の既定は C3 で決める**（計画レビュー決定 2026-09-29。「手渡し 24 時間・マシン 1 時間」の初期案は撤回）。窓付き送付 `since_days` は auth でなく hermes の config/CLI 引数で、受信側は `history_floor` で知る |
| CD-7 | signal 同一性 | 畳み込みを許容し明記。signal 件数の一致検証はしない |
| CD-8 | fixture 正本（Q7） | hermes-mcs を正本にし、zaitaku-calender へコピー。両 CI で `MANIFEST.sha256` を検証 |
| CD-9 | 本文 record `message_body` | `mcs-read-model/1` に新 record 型を追加（Q1 の決定による契約拡張。2026-09-29・オーナー。確定文言は計画レビュー決定 2026-09-29）: `message_id`（対応 message と同一世代・同一 part）・`body_text`（UTF-8 ≤8,192 bytes。`messages.body_text`（タグ除去済み）を送り、超過は送信側で UTF-8 文字境界で切詰め `body_truncated=true`）・`body_format`（enum。**v1 は `text` のみ**。`html` は予約語で受信側は拒否）・`body_sha256`（**送信した `body_text`（切詰め後）の UTF-8 bytes の sha256**。`content_hash`（本文 HTML の sha256）とは一致しない）・`body_truncated`・`sender_kind`（enum: `self_org` / `physician` / `nurse` / `care_manager` / `other_professional` / `patient_family` / `unknown`。`sender_kind` の `self_org` 判定は `mcs_signals._self_sets`（config の `signals.self_*` + `self_profile_v1` 既定値）を根拠にし、それ以外は profession/sender_type のキーワード写像、他組織の薬剤師・複数所属は `other_professional`（`self_org` を断定しない）。写像表は `docs/specs/external-export-contract.md` に置く。氏名・個人特定属性は送らない）。**送出条件（第2回計画レビュー修正）**: `body_state='full'` かつ `body_text` 非 null の message にだけ付ける（`snippet`/`unknown`/`deleted` は付けない → 受信側は「内容未取得」表示。`body_text=''` の file-only 投稿は空本文を送る）。`content_omitted` は投影の省略印で本文条件ではない。auth `fields` の明示列挙がある場合のみ許可（CD-6）。本文は自由文で PHI を含み得るため、受信側の staging は暗号化・read 監査が必須（zaitaku-calender `docs/adr-external-ingest-v1.md`） |
| CD-10 | 患者単位の完全性 record `patient_coverage` | `mcs-read-model/1` に新 record 型を追加（Q10 決定 2026-09-29・オーナー: 送る）: `project_id`（**fetch 対象の全 project — 0 message の患者も含む**。第3回）・`fetch_state`（enum: pending/complete/incomplete）・`coverage_ts`（**検証済み履歴取得範囲の上端 epoch 秒** — 「最終取得試行時刻」ではない。`ledger.coverage_ts()` の値で、0/未設定は null。第2回計画レビューで実装との不一致を訂正）・**`history_floor`（integer | null。オーナー判断 2026-09-29 で v1 に追加 — fixture 固定後の追加は契約 `/2` が要るため。窓付き送付を可能にする）**: ledger の `patients.history_floor` を写像（完了記録なし → null、-1 → 0＝先頭まで取得済み、正 → その epoch 秒。窓付き送付時は `max(floor, 窓の開始 epoch)`）。受信側は (a) `fetch_state` が complete でない患者、または世代に `patient_coverage` が欠ける患者を患者単位の「不明」とし、(b) CD-3 の降格は `history_floor` 非 null かつ `history_floor <= posted_at_ts <= coverage_ts` の item にだけ適用する。allowlist 追加は review 対象 |

C0 の合意事項は CD-1〜CD-10 のほか、次を含む（両文書で同一。zaitaku-calender `ROADMAP.md` §4.7）: 受け入れ record 型（7 種）、同一性キー（上記）、拒否コード表、**撤回指示書の形式と輸送**（`mcs-ext-withdraw/1` 提案: `contract`・`envelope_id`・`auth_id`・理由コードのみ、4 KiB 以下、自由文なし。hermes `withdraw` が指示書ファイルを outbox に原子的に生成し、zaitaku のアップロード画面で受領して削除 receipt を返す — 現行は `sink.delete` のみで相手に届かない（第2回計画レビュー修正）。withdraw が原本より先に届く場合と part 分割の一部だけが withdraw された世代の扱いも決める）、**item の版管理用 payload hash の範囲**（`content_hash`・本文メタ・facts・relations・state 等の canonical hash。本文のみの変更を別版にする。第2回計画レビュー追加。**配列は要素の canonical JSON 文字列のコードポイント昇順にソートして入力**。第3回）、受信側の鮮度閾値（CD-6 参照）、サイズ上限の扱い（受信 wire bytes で 1,048,576 B。手渡しは canonical 出力をそのまま運び wire ≡ canonical。上限ちょうどの受理と +1 byte の拒否を固定。`docs/roadmap/connector.md` §2 (5)）、`source` の対応付け（同一性キー参照）。

### フェーズ（hermes-mcs 側の成果物）

| フェーズ | hermes-mcs 側の成果物 | 依存 | 規模 |
|---|---|---|---|
| **C0 契約合意（両 repo 共同）** | CD-1〜CD-10 と C0 合意事項（撤回指示書・受信側鮮度閾値・サイズ上限・`source` 対応付け・`content_hash` の null 取扱い）の合意。旧 #25（iCal）を廃止して本接続に置換。参照実装の変更（数値正規化・`part`・profile・`parse_receipt`・coverage 拡張・`message_body`/`patient_coverage` record 追加）、合成 fixture 一式（受理 12・拒否 23（`15` は生成のみ・コミットしない）・receipt 6・withdraw 4。connector.md §2(2) の一覧と一致。本文・patient_coverage・part.set・payload hash・境界数値ケースを含む — 第2回計画レビューで拡張）と `MANIFEST.sha256`、drift guard（F-4）。**完了条件に Q6 の判断記録を含める** | zaitaku-calender C0（同時）、#8-D2（決定済み: 現行名維持）、**残りは CD-1〜CD-10 の合意**（Q1〜Q4・Q7・Q8(b)・Q10・Q11 は 2026-09-29 決定済み、Q6 は記録済み） | M |
| **C1 手渡し取込（未採用 staging）** | `ext_contract.main()` の subcommand 化（deliver / reconcile / withdraw / health / handoff / link-hints）、`select_records`（前段除去と `--only-with-facts`）、`split_envelopes`（1 MiB）、`HandoffSink`（自己 ack しない）、`rejected` 終端と `_valid_ack` の status 検査（F-3）、health。E2E は合成 ledger で完結 | C0、CD-1。**本番投入は #4 の実施記録と Q6 の判断後だけ**。合成での開発・テストは切り離して進めてよい | L |
| **C2 採用導線** | 契約改訂（`mcs-ext-auth/2`、`scope` の第 3 値）と Q9 の承認が前提。**承認されるまで hermes-mcs 側の成果物なし**。型付き値の正本は #15 | zaitaku-calender C2、Q9。zaitaku-calender C2 は hermes-mcs C2 を待たない | L |
| **C3 マシン送信（任意）** | `ext_transport.py`（`HttpsSink`、endpoint policy、Keychain token）。実行は手動のまま。自動再送はしない | Q6・Q8、契約付録、C1 | M |
| **C4 hermes-mcs 通知の縮退（任意）** | `notify.show_patient_names`（既定 true）。名前の出力は 5 ファイル（`notify_render.py`、`notify_views.py`、`notify_cards.py`、`notify_flush.py`、`semantic/semantic_render.py`）。日次 digest の Work Queue 代替を再評価（Q5） | zaitaku-calender C2 の運用実績、Q5 | S |

共通ルール:
- 実行は手動のみ。tick に組み込まない（`docs/specs/external-export-contract.md:136`）。
- 結果不明は held のまま receipt と照合する。自動再送しない。
- 同一性は上記「同一性キー」に従う。
- `facts[]` は semantic 層の current な成果物（`semantic_facts_v4`、なければ `canonical_projection`）があるときだけ埋まる（`mcs/views/read_model.py:209-218`）。`canonical_projection` も semantic 層の artifact で、`config.json` の `semantic` 設定によるゲートがある（`mcs/semantic/semantic.py:1-15`）。facts が空でも、message の存在・状態（`content_hash`・`body_state`・extraction 状態・`parent_id`）、coverage、signal は届く。
- 返信の検知は新しい record 型を作らず、`message.parent_id` と既存 signal で表す。ただし `pharmacist_request_unanswered` は「薬剤師宛の依頼に自組織の投稿がない」検知で、他職種の返信到着ではない（`mcs_signals.py:472-523`）。


## 9. オーナー判断一覧

### 接続（zaitaku-calender と共通の番号）

決める時点: **C0 の残りは CD-1〜CD-10 の合意のみ**（Q1・Q2・Q3・Q4・Q7・Q8(b)・Q10・Q11 は 2026-09-29・オーナー決定済み。Q6 は記録済み: 許可・本文を含む staging の受入・保存、zaitaku-calender `docs/adr-external-ingest-v1.md` §4。実データ投入と本番投入は zaitaku-calender `ROADMAP.md` §10.3 のゲートの対象のまま。合成 fixture の開発は進めてよい）。C2 までに Q5・Q9、C3 前に Q8(a)。

1. ~~本文なしで足りるか~~ → **決定済み（2026-09-29・オーナー）**: 本文を送る。`mcs-read-model/1` に `message_body` record を追加（CD-9）。`docs/specs/external-export-contract.md`・`export_schema.py`・両側 fixture の改訂が要る。
2. ~~semantic 層（`semantic_facts_v4` または `canonical_projection`）を常時動かすか~~ → **決定済み（2026-09-29・オーナー）**: 常時動かす。facts が届く前提で fixture を作る。
3. ~~kind×project_id の PHI としての扱いと保持期限~~ → **決定済み（2026-09-29・オーナー）**: PHI として扱う（本文を含むため staging は PHI）。保持 = min(envelope の `retention_days`, 30 日)、起点は `received_at`（zaitaku-calender `docs/adr-external-ingest-v1.md` §3-4）。`content_hash` は本文 HTML の sha256 で、短文・定型文は推測可能（`mcs/core/ledger.py:1055`）。
4. ~~患者対応付けと採用を pharmacist に限るか、clerk にも許すか~~ → **決定済み（2026-09-29・オーナー）**: 対応付け・採用とも clerk にも許す（capability で制御し職種強制はしない。zaitaku-calender `ROADMAP.md` §4.5）。
5. Work Queue に「未確認の staging 行あり」を導出コードとして足すか（zaitaku-calender `docs/plans/implementation-plan.md:318` との両立）。C4 の digest 代替の可否もこれに従う。
6. MCS から取得したデータを別システムへ転送・保存することが許されるか。根拠は zaitaku-calender `docs/domain-model-decision.md` の外部連携条項（接続先 ID と確認済み内部 ID の明示対応: L182、双方向連携の事前契約: L206）、MCS 利用規約、患者同意、院内規程。SHR-10／SCP-07（zaitaku-calender `docs/specs/visit-report-spec-v1.md:637, 82`）は「MCS への書き戻しをしない」ことの根拠としてだけ使う。**決まるまで C1 の本番投入（実データによる最初の envelope 作成と zaitaku-calender 本番へのアップロード）以降に進まない**。合成 fixture による開発・テストは進めてよい。**記録済み（2026-09-29・オーナー: 許可。本文を含む `message_body` を含む staging の受入・保存、zaitaku-calender `docs/adr-external-ingest-v1.md` §4）**。
7. ~~fixture の正本をどちらに置くか~~ → **決定済み（2026-09-29・オーナー）: hermes-mcs を正本**（CD-8 の推奨どおり。zaitaku-calender へコピーし `MANIFEST.sha256` で一致確認）。あわせて確定: `#8-D2` wire enum 名は現行名のまま（改名しない）、`prev_content_hash` は追加しない。
8. (a) C3 の構成（D1 直接 binding か RPC か、mTLS 必須か（`bounded_http` は Bearer 固定で mTLS・Cloudflare Access 系ヘッダに未対応）、専用 Worker の権限を書込みのみに絞るか、Access service token かアプリ層 Bearer か。zaitaku-calender `ROADMAP.md` §4.9）。(b) ~~zaitaku-calender の P0 より先に接続へ着手するか~~ → **決定済み（2026-09-29・オーナー）: P0 完了後。適用範囲はコード実装と本番投入**（zaitaku 側 S2 以降、C1 本番投入）。C0 の契約合意・fixture 固定・hermes-mcs 側の参照実装変更（CD-9・CD-10 等）は進める。
9. 型付き値（allergy・ADE・vital_lab の値）の送付を認めるか。認める場合は `docs/specs/external-export-contract.md` の detail 禁止条項（L22・L137）の改訂とオーナーの明示承認が要る。hermes-mcs C2 の前提。
10. ~~患者単位の取得完全性を zaitaku-calender へ送るか~~ → **決定済み（2026-09-29・オーナー）: 送る**。新 record 型 `patient_coverage`（`project_id`・`fetch_state`・`coverage_ts`。allowlist の変更）を CD-10 として起票し `export_schema.py` に追加する。全体件数（`patients_incomplete`、CD-4）と併せて患者単位の「不明」を出せる。
11. ~~人が MCS のルームを開いて `project_id` から患者を特定する導線~~ → **決定済み（2026-09-29・オーナー）**: テナント設定の MCS ベース URL から `project_id` 単位のリンクを組み立てる。message 単位の直リンクは未確認のため約束しない。
12. 真正性: 署名（`mcs-ext-export/2`、Worker secret の HMAC 等）が要るか。契約改訂とオーナー承認が要る。決まるまで zaitaku-calender C1 は人が真正性を担保し、画面に upload 者と source を表示する（zaitaku-calender `ROADMAP.md` §4.1）。

**追加の決定済み事項（2026-09-29）**:
- オーナー判断: (A1) タイムラインの行粒度は 1 message = 1 行（`occurred_on`・`sort_at` は MCS 投稿日 `posted_at_ts` 由来。受信日ではない）。(A2) `patient_coverage` に `history_floor` を v1 で送る（CD-10 更新。fixture 固定後の追加は契約 `/2` が要るため。窓付き送付を可能にする）。(A3) 履歴タブのタイムライン表示条件を `visit:read` に緩め、kind 単位の capability で行を制御（zaitaku 側 S5 で実装確認）。(A4) envelope 原本は保持しない（`records_sha256`・受信 bytes の sha256・receipt・envelope メタのみ残し、本文は item 側の暗号文にのみ持つ）。
- 計画レビュー決定（負担軽減方針）: receipt bundle（NDJSON 一括、CD-5）、`reconcile --receipts PATH` と `handoff`・`link-hints` の各 subcommand と `config.json` の `ext_export` プロファイル（connector.md §3 D）、`since_days` 窓付き送付と鮮度閾値のテナント設定・既定 72h（CD-6）、降格の `history_floor` 適用範囲（CD-3/CD-10）。zaitaku 側の表示・認可の決定（バッジ文言・tombstone 非表示・状態 1 行・概要タブの MCS カード・read 監査単位・adopt⇒read 含意）は zaitaku-calender `ROADMAP.md`・`docs/adr-external-ingest-v1.md` を正とする。
- **第2回計画レビュー（2026-09-29）の契約修正案 — CD 合意待ち**: §5 の CD 表に「第2回計画レビュー修正」とある箇所が対象。(1) CD-1 は指数になる値の拒否と ECMAScript `Number::toString` 互換フォーマッタを要する（Python repr が `1e-06` を出す実測差）。(2) CD-2 は `part` に `set`（全 part の `records_sha256` 配列の canonical hash）を足して分割集合を束縛し、別分割の混在を拒否。(3) CD-9 は `body_state='full'` かつ `body_text` 非 null のみに送出を限定し（`content_omitted` は本文条件ではない）、`fields` の明示列挙を必須化（CD-6）して既存 auth の暗黙許可を防ぐ。`sender_kind` の `self_org` は `mcs_signals._self_sets` のみを根拠にする。(4) CD-10 の `coverage_ts` は「検証済み履歴取得範囲の上端」で「最終取得試行時刻」ではない（実装との不一致を訂正。降格は `history_floor <= posted_at_ts <= coverage_ts` の範囲）。(5) 撤回指示書 `mcs-ext-withdraw/1` は hermes `withdraw` が outbox に原子的に生成する手渡しファイル（現行 `sink.delete` のみでは相手に届かない）。(6) item の版管理 payload hash に本文メタ（`body_sha256` 等）を含める。(7) サイズ上限は受信 wire bytes で固定（手渡しは canonical ≡ wire）。(8) fixture を受理 12・拒否 23（`15` は生成のみ・コミットしない）・receipt 6・withdraw 3 に拡張。zaitaku 側の対応修正（タイムライン fingerprint・MCSを除く・新着定義・signal の message 参照 id・フラグ off 時の withdraw/purge 継続・採用先 capability）は zaitaku-calender `ROADMAP.md`・`docs/adr-external-ingest-v1.md` を正とする。
- **第3回計画レビュー（2026-09-29）の修正案 — CD 合意待ち**: (1) `payload_sha256` 入力の配列は要素を canonical JSON 文字列のコードポイント昇順にソート（facts・relations・signal evidence。wire の出現順に依存しない。signal 同一性 hash も同規則）。(2) `patient_coverage` は fetch 対象の全 project（0 message を含む）について出す。(3) 同一生成で別 `set` の完全集合が後着した場合は `409`/`generation_set_conflict` で拒否（先着完全集合を保持。解消は withdraw→再送）。(4) 世代撤回は `withdraw --generation` が対象世代の全 envelope_id に指示書を展開（wire は envelope_id 単位のまま）。受信側は指示書に 4,096 B の別上限。(5) `fields` 未記載の既存 auth は `auth_fields_required` で fail closed（移行手順付き）。(6) CD-1 の受理境界注記（指数 lexeme は受理・重複キーは last-wins・JCS subset）。fixture を withdraw 4 件に拡張（`04` サイズ超過）。zaitaku 側の対応（削除 receipt の保持起点・外部行の title/id/除外フィルタ・`body_sha256` の oracle 注記・viewer の権限告知）は zaitaku-calender `ROADMAP.md`・`docs/adr-external-ingest-v1.md` を正とする。

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
| #20-D1〜D4 | 初期対象業務・評価ラベル / `reply_state` を通知カードにも出すか閲覧のみか / E2（enforce）・E3（canonical）の切替時期 / 人手ラベル 200 件の分割・期間・記入者と E2 の前提観測の閾値 | D1 は 20-A、D2 は 20-C、D3 は各切替の前、D4 は E1 の観測開始後 |
| #21-D1〜D3 | 患者サマリー view に本文先頭 80 字を出すか / 抽出文脈に注入するか / digest に件数・ID を出すか | #21 の実装前（既定は全て「出す・注入する」） |
| C4-D1〜D2 | 縮退の範囲（名前のみ / 投稿者名も / 本文・要約も）/ 既定値 | C4 の前 |
| CD-1〜CD-10 | §5 の契約事項 | C0（CD-1 は初回の実送信前が期限） |

旧版から引き継ぐ判断:
- 独立実装レビュー（#10）を、縮小版で行うか。
- §7 の v2 #24（担当者別ルーティング等の廃止）を確定するか。
- 本番の再取込・既存 floor の変更は未実施（旧版 2026-09-19 追加検証）。#4 で扱う。
