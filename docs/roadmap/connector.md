# zaitaku-calender 接続（C0〜C4）の hermes-mcs 側の詳細計画

[`docs/ROADMAP.md`](../ROADMAP.md) §4 の接続フェーズのうち、**hermes-mcs 側の成果物**を、実装に着手できる粒度まで掘り下げた計画。相手側（zaitaku-calender）の成果物は、対の文書 `ROADMAP.md` にある。フェーズ ID（C0〜C4）、未決事項の番号（Q1〜Q12。Q11・Q12 は zaitaku-calender 側が起票し本書へ同期した新規）、契約版（`mcs-ext-export/1`・`mcs-ext-auth/1`・`mcs-read-model/1`）、C0 の契約決定（CD-1〜CD-10）は両文書で共通。

- 基準: v1.0.6 のコード（2026-09-29 調査）。行番号はこの時点のもの。
- 表記: 【実行確認】= 合成入力でローカル実行して確認、【未検証】= 実データ・実機・実 API・相手側実装に触れないと分からない点。
- 実データは読んでいない。相手側（TypeScript）の受信実装は存在しないため、相手側の挙動は Node での簡易実験だけで確認した。
- オーナー判断は項目内で `#N-Dk`（他項目）または `Q1〜Q12`（接続）と呼ぶ。

## 0. 結論

1. **既存の deliver / reconcile / withdraw・journal・audit は C1 でほぼ再利用できる**。`LocalSink` を自己 ack しない形（既存の `drop_ack` / `drop_delete_ack`: `ext_contract.py:341-342`）にし、receipt を `acks/`・`deletions/` に置くだけで、held → acked と delete_held → withdrawn が既存コードで動くことを実行で確認した。新規に要るのは、入力の選別、分割、receipt の厳格な parser、`rejected` 終端、CLI、health。`ext_contract.main` を呼ぶテストは現在 0 件。
2. **旧 ROADMAP §4 には、C0 で決めないと fixture を固定できない食い違いが 3 点ある**: (a) `coverage` に取得完全性が入っていない、(b) canonical JSON の数値表記が言語間で一致しない、(c) `fields` による除外は「除外」ではなく build 拒否。詳細は §1。
3. 規模は C0 = M、C1 = L、C2 = L（ゲート付き。着手不要）、C3 = M、C4 = S。
4. ベースライン（実行済み）: `scripts/run_tests.sh tests/ops/test_ext_contract.py tests/views/test_read_model.py tests/ops/test_brain_export.py` は 75 passed。`ci/gates.py` は 8/8、`ci/mine_gates.py --check` OK、`scripts/update_readme.py --check` exit 0。

## 1. 旧 ROADMAP §4 と実コードの不整合（重大度順）

| # | 度 | 旧 ROADMAP の記述 | 実コード（実測） | 対応 |
|---|---|---|---|---|
| 1 | 高 | coverage / meta / signals_truncated / patient_coverage は「取得未完了を『不明』と表示する」入力（§4・完了条件） | `coverage.collection` は patients / messages / deleted / extraction_eligible の 4 個数だけ（`read_model.py:273-302`、`export_schema.py:119-122`）。患者別の `fetch_state`（pending / complete / incomplete: `mcs_adapter.py:333`）・`coverage_ts`・`history_floor`（`ledger.py:304-305,373-375`）は現行のどの record にも出ない → **Q10 決定（送る）により `patient_coverage` record（CD-10）で出す** | CD-4、CD-10、Q10（決定済み） |
| 2 | 高 | attachment・stat は `fields` で除外する | 除外ではなく**全体拒否**。未許可 type があると `record_type_unauthorized:<type>` で build 失敗（`ext_contract.py:217-218`、`test_ext_contract.py:96-102`）。brain_export の `export.jsonl` は stat と attachment を必ず含む（`brain_export.py:249-251,260-261`）。【実行確認】許可 fields だけの auth に raw の `export.jsonl` を渡すと失敗する | C1 に未許可 type の前段除去（縮小だけ、件数を出力）を追加。build の拒否は維持 |
| 3 | 高 | 両 repo で `records_sha256` を検証する前提 | `_canonical`（:71-73）は Python の float 表記。【実行確認】`1790000000.0` は Python で `1790000000.0`、JS で `1790000000`。Node で受信側を模して再計算したところ、小数付きの時刻では一致し、整数値の float が 1 つでもあると不一致だった。既存テストの `NOW=1_800_000_000.0`（`test_ext_contract.py:13`）は `NOW-10` も整数値 float なので、流用した fixture はこの罠を埋め込む | CD-1。初回の実送信前に決める |
| 4 | 中 | journal 状態 held / delete_held / withdrawn（`ext_contract.py:23-26`）、health に held 件数 | journal に `held` は書かれない。結果不明は `sent`（:513）で、`held` は読取り時に許容するだけ（:449-451,:497）。:23-26 は docstring | health は `sent` + `held` を held として数える |
| 5 | 中 | 受信側が 7 種・meta・coverage を要求する | 参照 receiver `_validate_envelope`（:296-324）は stat・attachment も coverage 欠落も受理する（実行で確認）。7 種と meta / coverage の必須は builder 側だけの規則 | CD-6 |
| 6 | 中 | meta を `max_snapshot_age_s` 超過判定に使う（zaitaku 側） | `max_snapshot_age_s` は auth の項目で envelope にない（:258-271）。判定は送信側だけ（:197-200、deliver 時の再構築: :482） | 受信側の鮮度閾値は別途合意（CD-6） |
| 7 | 中 | 返信の検知は `parent_id` と `pharmacist_request_unanswered` | (a) `--only-with-facts` は facts のない返信 record を落とす（ただし CD-3 の改訂で `message_body` を持つ message は残す）。(b) `pharmacist_request_unanswered` は「薬剤師宛の依頼に自組織の投稿がない」検知（`mcs_signals.py:472-523`、:509）で、他職種の返信到着ではない。(c) 削除・編集で facts が消えた message も届かず、受信側に古い staging が残る（`read_model.py:155-158,:209-247`） | CD-3 |
| 8 | 中 | 分割上限は 1 MiB（UTF-8 の JSON 本文） | 何を測るか未定義。`bounded_http` は本文を既定 separators で再 JSON 化する（`bounded_http.py:100-101`）。実測で canonical より 7〜8% 大きい | 上限は envelope 全体の canonical バイト数と定義（§2 (5)） |
| 9 | 低 | C1: `fields` を 7 種に固定する | 現在の既定は省略時に全 7 種（:201。CD-9・CD-10 の追加で全 9 種になる）。auth を作る CLI はない。既存テストと integration は明示 fields を使う（`test_ext_contract.py:26`、`integration/test_mcs_recovery_narrative.py:742`） | ライブラリ既定は変えず、C1 プロファイルで強制（CD-6） |
| 10 | 低 | C4 のファイルは `notify_flush.py`・`notify_render.py` | 名前の出力は 4 ファイル。`notify_cards.py:1075-1085`（Discord スレッド名）と `semantic/semantic_render.py:194-196,253-256`（要約通知の `【名前】`）が抜けていた | §6 |
| 11 | 低 | signal の同一性は type + project_id + evidence の sha256 | `med_change_no_followup` は evidence の `med`・`mention_count` が allowlist 外で落ちる（`mcs_signals.py:337-342` 対 `export_schema.py:130-133`）。同じ message_ids に複数の med があると同一性が衝突し、1 件に畳まれる | CD-7 |
| 12 | 低 | 行番号 | CLI の generated_at は :676-678。`_SCHEMAS` は :116-147（:148 は `RECORD_TYPES`）。「held / delete_held / withdrawn（:23-26）」は docstring で、実定義は :409-411,:449-451 | 文書修正 |
| 13 | 低（要確認） | `body_state` enum に `"null"`（`export_schema.py:55`） | adapter は `deleted`/`full`/`snippet`/`unknown` のみ生成し（`mcs_adapter.py:458-469`）、`"null"` 文字列を出す経路はない。一方で DB の `body_state` 列は NULL 可（`read_model.py:125` が素通し、`ledger.py:1368` は `or "unknown"` で写像）で、`_select` の None パス（`export_schema.py:152-153`）は enum 検査を通らず JSON `null` として残り得る — 契約上の扱い（文字列 `"null"` か JSON null か）は要確認 | C0 で fixture に含めるか決める |

## 2. C0 契約合意

**目的**: 両 repo が同じバイト列で「受理 / 拒否（期待コード付き）/ receipt」を判定できる状態を、合成 fixture と sha256 で固定する。hermes-mcs 側の実作業は、未決点を決めて、参照実装と golden fixture に反映すること。

**現状**
- 契約の骨格は実装済み。envelope 13 項目（`ext_contract.py:258-272`）、受信側の検証（:296-324）、ack の形（:355-359）、削除 ack の形（:372-376）。
- fixture の置き場も hash pin の慣行もない。`fixtures/` も `*.sha256` も repo になく、`evaluation/`・`ci/` にも pin はない。
- 【実行確認】現行の参照実装に対する拒否コード（各例は 1 欠陥だけ）: `forbidden_field:statement`、`forbidden_field:sender`、`record_field_not_exportable`、`envelope_integrity_invalid`（sha・count・id の改ざん）、`snapshot_generation_mixed`、`sink_scope_not_aggregate`、`sink_contract_mismatch`、`envelope_fields_invalid`、`envelope_id_invalid`。受理例（meta・coverage・message × 2〔うち 1 件は同 kind の fact 2 件〕・signal）は `build_envelope` と `_validate_envelope` を通る（3,128 B）。

**設計方針**

(1) C0 で決める契約事項（CD-1〜CD-10。両文書で同一）:

| ID | 決めること | 推奨 |
|---|---|---|
| CD-1 | 数値の canonical 表記と受信側の hash 検証 | 規則は「整数値は整数表記（-0 も 0）、非整数は ECMAScript の数値表記、指数表記・NaN・Infinity・safe integer 範囲外は拒否」、キーは code point 順、空白なしで RFC 8785（JCS）互換にする。実装は `_canonical` の前処理 10 行程度。受信側は parse 後に JCS で再計算する。fixture に整数値 float の入力（`accepted/06`）を入れ、両実装で一致を確認する |
| CD-2 | 分割集合の表現 | 各分割に meta・coverage・signals_truncated・**patient_coverage** を同一内容で複製し、message / signal を排他的に分配する（**`message_body` は対応する message と同じ part に置く**）。envelope に任意項目 `part:{"index":1..N,"count":N}` を追加する（許可集合 :309-313 に任意として足す。`envelope_id` と `_intent_hash` には含めない）。受信側は (auth_id, snapshot_generation_id, part.count) で集合化し、揃うまで「不完全」と表示する。これがないと分割の欠落が「記録なし」に見える |
| CD-3 | `--only-with-facts` の意味と伝播 | 残す条件は facts 非空、`body_state=deleted`（tombstone）、**または `message_body` を持つ**（CD-9 導入に伴う拡張。facts なしの返信本文を落とさないため）。受信側は、完全集合が届いた世代で、前世代の staging のうち再掲されないものを「MCS 側で現在は事実なし / 不明」に落とす（削除はしない）。届かない返信を受信側は「返信なし」と表示しない。Q2 = on が決定済み（facts 前提の fixture）。Q2 = off なら message は body 持ちと tombstone だけになる。**降格の適用範囲（計画レビュー決定 2026-09-29）**: (b) 降格は、その患者の `patient_coverage.history_floor` が非 null で、かつ `posted_at_ts >= history_floor` の item にだけ適用する。`history_floor` が null の患者の item は降格しない。(c) `history_floor` より古い item は降格せず、保持期限（30 日）で自然に消える |
| CD-4 | 取得完全性 | `coverage.collection` に `patients_incomplete`（`fetch_state≠'complete'` の件数、算出不能は null）を追加する（read_model の既存 `suppress` 流儀に合わせる）。患者単位は `patient_coverage` record（CD-10。Q10 決定済み: 送る）。#3 の `known_gaps` を後で加える余地がある |
| CD-5 | receipt | 下記 (4)。envelope 単位の all-or-nothing（参照 receiver が最初の不正で全体拒否する挙動と同じ: :304-307）。`rejected` は終端。**receipt bundle（計画レビュー決定 2026-09-29）**: 手渡しの往復回数を減らすため、受信側は NDJSON（1 行 1 `mcs-ext-receipt/1` オブジェクト）の bundle を 1 回でダウンロードでき、hermes は `reconcile --receipts PATH`（単一 JSON・NDJSON・ディレクトリ）で一括取込する。契約 `mcs-ext-receipt/1` 自体は不変で、bundle は輸送上の便宜 |
| CD-6 | C1 プロファイル | fields は 7 種（`message_body`・`patient_coverage` を含む）、patients は `"all"`、`max_snapshot_age_s` 必須（≤3600）、`retention_days` ≤ 30（Q3 の min(retention, 30) と producer 側を一致させる。Q3 決定済み: PHI として扱う）、meta・coverage 各 1 件必須、stat・attachment は受信側でも拒否する。producer と receiver の両方で強制し、期待コードを固定する。**`since_days`（窓付き送付。計画レビュー決定 2026-09-29）**: auth の項目ではなく hermes の `config.json` の `ext_export` プロファイル / CLI 引数で直近 N 日の message に絞れる。受信側は `patient_coverage.history_floor`（CD-10）で窓の有無を知る（`history_floor = max(ledger の floor, 窓の開始 epoch)`）。`max_snapshot_age_s` は送信側だけの検査（§1-6）なので、受信側は `snapshot_generated_at` と受信時刻で自前の鮮度閾値を持ち、超過は拒否でなく「古い」警告として age を常時表示する。**受信側の閾値はテナント設定・既定 72 時間（計画レビュー決定 2026-09-29）。マシン送信時の既定は C3 で決める**（「手渡し 24 時間・マシン送信 1 時間」の初期案は撤回） |
| CD-7 | signal 同一性 | 畳み込みを許容し、両文書に明記する。signal 件数の一致検証はしない |
| CD-8 | fixture 正本（Q7） | hermes-mcs を正本にする（Python の参照実装から生成するため）。zaitaku-calender へコピーし、両 CI で `MANIFEST.sha256` を検証する。変更は同一変更単位（両文書とも同じ運用） |
| CD-9 | 本文 record `message_body`（Q1 の決定による契約拡張。2026-09-29・オーナー。確定文言は計画レビュー決定 2026-09-29） | `mcs-read-model/1` の `_SCHEMAS` に新 record 型 `message_body` を追加する: `message_id`（必須。対応する `message` record と同一世代・同一 part に置く）・`body_text`（string。UTF-8 で **8,192 bytes 以下**。hermes は `messages.body_text`（タグ除去済み: `ledger.py:7-9`）を送る。超過は送信側で UTF-8 の文字境界で切詰め、`body_truncated=true`）・`body_format`（格納形式の enum。**v1 の値は `text` のみ**。`html` は予約語で v1 の受信側は拒否する）・`body_sha256`（**送信した `body_text`（切詰め後）の UTF-8 bytes の sha256**。受信側は自己整合性として再計算する。`content_hash`（本文 HTML の sha256: `ledger.py:1055`）とは一致しない）・`body_truncated`（bool）・`sender_kind`（enum: `self_org` / `physician` / `nurse` / `care_manager` / `other_professional` / `patient_family` / `unknown`。氏名・個人特定属性は送らない。hermes 側で `sender_type`・`profession`・`organization`（`ledger.py:167-168`、`mcs_adapter.py:375-381,476`）から写像し、写像表は `docs/external-export-contract.md` に置く。判定不能は `unknown`）。`content_omitted=true` の message には付けない。body の編集・削除は `body_sha256` の変化または body の消失として表れ、message の tombstone（`body_state=deleted`）で本文も消える。本文は自由文で PHI を含み得るため、受信側の staging は暗号化・read 監査が必須（zaitaku-calender `docs/adr-external-ingest-v1.md`） |
| CD-10 | 患者単位の完全性 record `patient_coverage`（Q10 の決定による契約拡張。2026-09-29・オーナー: 送る。`history_floor` の追加はオーナー判断 2026-09-29） | `mcs-read-model/1` の `_SCHEMAS` に新 record 型 `patient_coverage` を追加する: `project_id`（必須）・`fetch_state`（enum: `pending` / `complete` / `incomplete`。`mcs_adapter.py:333` の既存値集合）・`coverage_ts`（その患者の最終取得試行の epoch 秒。未試行は null）・**`history_floor`（integer | null）**: hermes ledger の `patients.history_floor`（`ledger.py:898-916` 周辺。NULL/0 = 完了記録なし、-1 = 時系列の先頭まで取得済み、正 = 取得済み範囲の下限 epoch）を次のように写像する — 完了記録なし → `null`、-1 → `0`（先頭まで取得済み。下限なし）、正 → その epoch 秒。窓付き送付（`since_days`、CD-6）のときは `max(ledger の floor, 窓の開始 epoch)`（ledger が完了記録なしなら `null` のまま）。fixture 固定後の追加は契約 `/2` が要るため v1 に含める。受信側の規則: (a) `fetch_state` が `complete` でない患者、または世代に `patient_coverage` が欠ける患者を患者単位の「不明」と表示する（zaitaku-calender `ROADMAP.md` §4.4）。(b) CD-3 の降格は `history_floor` が非 null で `posted_at_ts >= history_floor` の item にだけ適用する（`history_floor` が null の患者は降格しない）。(c) `history_floor` より古い item は降格せず保持期限（30 日）で自然に消える。分割では meta・coverage と同じく各 part に複製する（CD-2）。allowlist の追加なのでレビュー対象 |

C0 の前の 2 つの決定は**両方決定済み（2026-09-29・オーナー）**: #8-D2 は wire enum 名を**現行名のまま**（改名しない）、`prev_content_hash` は**追加しない**（fixture 固定後の追加は契約 `/2` が要るため C0 で決定）。

C0 の合意事項は CD-1〜CD-10 のほか、次を含む（両文書で同一。zaitaku-calender `ROADMAP.md` §4.7）:
- **撤回指示書の形式と認証**: `withdraw()` は `sink.delete` を呼ぶだけで、zaitaku-calender へ運ぶ指示書の形式・認証が未定義。提案は `mcs-ext-withdraw/1`: `envelope_id`・`auth_id`・理由コード（自由文なし）のみ、4 KiB 以下。受信側は未採用 staging を削除して削除 receipt を返す。withdraw が原本より先に届く場合（tombstone を先に置き後着を拒否）と part 分割の一部だけが withdraw された世代の扱いもここで決める。合意までは受信側 C1 の完了条件から外し、管理者による未採用 staging の即時 purge 手順で代替する。
- **受信側の鮮度閾値**（CD-6）と**サイズ上限の扱い**: 上限は canonical envelope 全体で 1,048,576 B。wire は再 JSON 化で +7〜8% 膨らむため、(a) 送信前検査を約 900,000 B に絞るか、(b) 受信側が生 bytes を扱うかを決める。上限ちょうどの受理と +1 byte の拒否を fixture と route テストに含める。
- **`source` の対応付け**: message の論理キーの `source` は envelope のフィールドに対応しない。`destination` または `auth_id` へ対応付けるか、受信側の接続ラベルとするかを決める。
- **`content_hash` の null 取扱い**: wire で任意（null あり）。UNIQUE キーに入れない方針（同一性キー参照）のため、受信側で明示的な `'null'` 値へ正規化するか必須化を求めるかを決める。
- **message 同一性からの `content_hash` 除外**: 本書 2026-09-29 初版の ROADMAP §5 の文言（type + project_id + message_id + content_hash）は、編集のたびに別行となり supersede・撤回が壊れるため破棄する（ROADMAP §5 の新文言が両文書の正）。
- **拒否コード表**: 上記 (2) の期待コードに受信側独自のコード（`record_type_not_accepted:<type>`、`envelope_coverage_missing`、`envelope_meta_missing`、`envelope_too_large` 等）を合わせて固定する。

(2) fixture の中身:
- 固定値: `FIXED_NOW=1_790_000_000.5`、generation `gen-c0-0001`、`generated_at=FIXED_NOW-30.25`、auth_id `auth-c0-synth-1`。ID と hash はすべて合成で、`content_hash` は `sha256("SYNTHETIC-C0-<mid>")`。
- `accepted/`（受理）:
  - `01-basic`: meta、coverage（全構造）、message 1001（同 kind `medication_event` の fact 2 件、fact_id・workflow_status・evidence_ids は別、relation `TRANSITION` 1 件）、message 1002（`parent_id:1001`、fact 1 件）、signal（`pharmacist_request_unanswered`、`evidence.message_ids=[1001]`、`content_omitted:true`）。必須分。
  - `02-signals-truncated`: 01 に `signals_truncated{total}` を加えた例。
  - `03-heartbeat`: meta と coverage だけ。
  - `04-no-facts`: `facts=[]` で `state:pending` の message。
  - `05-split-1of3`〜`3of3`: 小さい max_bytes で強制した 3 分割。
  - `06-int-valued-float`: `generated_at=1790000000.0`（CD-1 を強制する例）。
  - `07-message-body`: message 1003 に対応する `message_body`（`body_format="text"`・`body_sha256` 一致）を持つ例。`sender_kind` は `self_org`・`physician`・`unknown` の少なくとも 3 値を含める（CD-9）。
  - `08-body-truncated`: 8 KiB 超の本文を切詰め `body_truncated=true` とした例。
  - `09-omitted-no-body`: `content_omitted=true` の message に `message_body` が無い受理例。
  - `10-patient-coverage`: `fetch_state=incomplete` の患者を含む `patient_coverage`（CD-10）の受理例。
  - `11-history-floor`: `history_floor` が正・0・null の 3 患者を含む `patient_coverage` の受理例（CD-10 の `history_floor` 写像を固定）。
- `rejected/`（拒否。1 欠陥だけで、他の hash・count・id は再計算）:
  - `01` `forbidden_field:statement`（`facts[0].statement`）、`02` `forbidden_field:sender`（message 直下）、`03` `record_field_not_exportable`（`facts[0].future_text`）、`04` 同（`message.memo`）
  - `05` `envelope_integrity_invalid`（`records_sha256` 改ざん）、`06` 同（`record_count`）、`07` 同（`envelope_id`）
  - `08` `snapshot_generation_mixed`、`09` `sink_scope_not_aggregate`、`10` `envelope_fields_invalid`、`11` `envelope_id_invalid`
  - C1 プロファイル分（新規実装が要る。現行の参照実装は受理する）: `12` `record_type_not_accepted:stat`、`13` `envelope_coverage_missing`、`14` `envelope_meta_missing`
  - `16` `forbidden_field:sender_name`（`message_body` 直下の氏名系キー）、`17` `body_sha256` と `body_text` の不一致（自己整合性違反）、`18` 8 KiB 超なのに `body_truncated` なし（contract violation）、`19` `patient_coverage` の `fetch_state` が enum 外、`20` `message_body` の `body_format` が enum 外（`html`。v1 は予約語を拒否）、`21` `patient_coverage` の `history_floor` が負数
  - `15` `envelope_too_large` は 1 MiB 超になるため生成だけで、コミットしない
  - 01〜11 の期待コードは現行実装に対する実測と一致。
- `receipts/`: `receive-accepted`、`receive-rejected`、`receive-mismatch-sha`（送信側は held 維持）、`delete-deleted`、`delete-failed`（`deleted:false`）、`bad-unknown-field`（parser 拒否）。
- `index.json` に各 fixture の `expect`（accept / reject）、`code`、`profile` を持たせる。

(3) 置き場所と pin:
- 置き場所は `tests/ops/fixtures/ext_contract_c0/`（tests は mcs の領域名をミラーする規約）。パスに `data/` 要素や `config.json` を含めない（CI hygiene が `git ls-files | grep -E '(^|/)(data/|…config\.json$)'` で落とす: `.github/workflows/ci.yml`）。
- 生成器 `tests/ops/ext_fixtures.py` は tests/ops が sys.path に入る（`tests/conftest.py:29-31`）ので import できる。golden test は「再生成バイト == コミット済みバイト」かつ `MANIFEST.sha256` の照合とする。
- pin は 3 層: (1) envelope 内の `records_sha256` と `envelope_id`（決定的: :257,276-280）、(2) `MANIFEST.sha256`（`shasum -a 256 -c` 互換）、(3) MANIFEST 自体の sha256 を「fixture set id」として `docs/external-export-contract.md` と相手 repo の文書に同一文言で記載する。

(4) receipt（`mcs-ext-receipt/1`。既存の ack と互換な上位集合）:

```json
{"contract":"mcs-ext-receipt/1","kind":"receive","status":"accepted","envelope_id":"<24hex>",
 "records_sha256":"<64hex>","records":5,
 "accepted":{"meta":1,"coverage":1,"message":2,"signal":1,"signals_truncated":0},"acked_at":1790000100.5}
{"contract":"mcs-ext-receipt/1","kind":"receive","status":"rejected","envelope_id":"<24hex>",
 "records_sha256":"<64hex|省略可>","reasons":["forbidden_field:statement"],"rejected_at":1790000100.5}
{"contract":"mcs-ext-receipt/1","kind":"delete","envelope_id":"<24hex>","deleted":true,"deleted_at":1790000200.5}
```

- 未知キーは拒否（`AUTH_FIELDS` と同じ方針: :57-62,:117-120）。`envelope_id` は `[0-9a-f]{24}`（:290-293）。`records` は journal の `record_count` と一致（`_valid_ack`: :546-553）。`accepted` の合計は `records` に等しい。
- `reasons` は最大 20 件、各 `^[a-z][a-z0-9_]*(:[A-Za-z0-9_.-]{1,64})?$`（ContractError コードと同形。自由文は禁止）。`acked_at`・`deleted_at` は既存の必須名を維持（:546-553、:647-649）。ファイルは 64 KiB 以内。
- 採用件数・採用 / 却下は入れない。逆方向のデータ流に当たる（ROADMAP §6）。
- `signature` は `/1` に入れない（YAGNI）。送信側は journal との束縛（`envelope_id`・`records_sha256`・`records`）で照合し、C3 は TLS + token で経路認証する。手渡しでの receipt 偽造は「偽の acked」を生むが、受信側の実状態は変わらない。必要になれば C3 で `hmac`（stdlib）を任意項目として足す。
- 【実行確認】現行の `_valid_ack` は `status` を見ない。`status:"rejected"` の receipt でも、id・sha・records・acked_at が揃えば acked にしてしまう（F-3）。receipt は必ず strict parser を通し、`_valid_ack` にも status 検査を足す（C1）。

(5) 1 MiB と分割:
- 上限は `len(_canonical(envelope).encode("utf-8")) <= 1_048_576`（envelope 全体、両端含む）。
- 実測（合成）: message は 599 B（facts なし）、758 B（fact 1）、1,179 B（fact 3 + relation 1）。meta は約 150 B、coverage は約 600 B、signals_truncated は約 100 B。1 MiB あたり約 919〜1,811 message。共有 record の複製コストは 1 分割あたり約 0.07%。
- 試作: 3,000 message（fact 3 件）を貪欲充填し、4 分割（1,047,833 / 1,047,839 / 1,047,834 / 405,177 B）、決定的、各分割に meta + coverage、0.14 秒。
- `records_sha256`・`envelope_id` は分割ごと。`snapshot_generation_id`・`snapshot_generated_at`・`auth_id` は全分割で共通。受信側の 1 リクエスト当たり record 数の上限は受信側の事情次第なので、C0 で byte 上限と併記して決める。

**成果物**
- `docs/external-export-contract.md` の改訂: `part`、receipt、C1 プロファイル、canonical 数値、サイズ、失敗コード表。同文書の「含まないもの」（L133-138）は C3 まで維持する。
- 参照実装: `_canonical` の数値正規化、`part` の任意項目、`_validate_envelope` の profile・サイズ検査、`parse_receipt`、coverage 拡張（CD-4。`read_model._coverage` と `export_schema` の allowlist）、`message_body`・`patient_coverage` の record 追加と生成（CD-9・CD-10。`export_schema.py` の `_SCHEMAS`/`RECORD_TYPES` と read_model 由来の生成器）。`patient_coverage.history_floor` の写像（ledger の NULL/0 → null、-1 → 0、正 → その epoch。窓付き送付時は `max(floor, 窓の開始 epoch)`）、`sender_kind` の写像表を `docs/external-export-contract.md` に置く、`select_records` の `since_days` 窓（CD-6）を含める。
- fixture、生成器、golden test、`MANIFEST.sha256`。
- drift guard test: `export_schema` の enum が `semantic_facts.FACT_KINDS` / `WORKFLOW_STATUSES` / `RELATION_TYPES`、`mcs_signals.DETECTORS`、`read_model.EXTRACTION_KINDS` と一致すること（F-4）。現状は 5 系統とも一致を実測したがテストはない。食い違うと `project_record` の ValueError（`export_schema.py:159-161`）で export 全体が止まる。
- Q6 の判断記録。`docs/dev-records/` に置く場合は `FIX-`・`TEST-` 等の ID 表記を避ける（`ci/mine_gates.py:36-39` が manifest 登録を要求する）。

**受入条件とテスト（合成のみ）**
- 追加先は `tests/ops/test_ext_contract.py`: golden とマニフェスト。accepted は `_validate_envelope` を通り、JCS の再実装で `records_sha256` が一致する。rejected は期待コード。receipt は parser の accept / reject と journal 遷移（accepted → acked、rejected → rejected、mismatch → held）。oversize は生成して `envelope_too_large`。drift guard。
- 共通コマンド（全フェーズ）: `scripts/run_tests.sh tests/ops/ tests/views/test_read_model.py`、CI 範囲の ruff（`make lint`。ローカルは uv 経由）、`python3 scripts/update_readme.py --check`、`python3 ci/gates.py && python3 ci/mine_gates.py --check`。
- integration の E2E（`test_mcs_recovery_narrative.py:556-575`）は無改変で緑のままであること（ライブラリの既定を変えないため）。

**依存**: zaitaku-calender C0（同時）、**残りは CD-1〜CD-10 の合意**（Q1〜Q4・Q7・Q8(b)・Q10・Q11・#8-D2 は 2026-09-29 決定済み、Q6 記録済み）。

**規模**: M。実作業は S〜M で、合意コストが支配的。

**オーナー判断・リスク**
- CD-1 は初回の実送信前が期限。送信後に変えると、整数値 float を含む envelope で `records_sha256` の互換が崩れる。
- CD-4 は allowlist の変更なのでレビュー対象。受信側の完全集合の規則（CD-3）は、zaitaku-calender の実装負荷が大きい。
- Q6 は「決まるまで本番投入不可」の外部ゲート。
- 送らないもの: signature、その他の新 record 型（`message_body`・`patient_coverage` 以外）。氏名・個人特定の属性フィールド・添付・型付き値（Q9）は引き続き送らない。

## 3. C1 手渡し取込（未採用 staging）

**目的**: 実データ不要の合成環境で、`export.jsonl` → 分割 envelope → 手渡し → receipt 照合 → 撤回 → health までを、手動 CLI だけで完結させる。

**現状（既にある / ないもの）**
- 既にある: auth 検証（`ext_contract.py:100-157`）、build（:184-273）、受信側の検証（:296-324）、journal / audit / lock（:405-463）、deliver（:465-543）、reconcile（:555-599）、withdraw（:601-635）、`LocalSink`（:327-402）。
- ない:
  1. CLI は deliver だけ。4 フラグ全必須（:660-687）、テスト 0 件、`_mcs_path` ブートストラップなし（同 dir import で動く）。
  2. 入力の選別、分割、`part`。
  3. C1 プロファイル、meta / coverage の必須。
  4. receipt parser と `rejected`。
  5. 手渡し用 sink。`LocalSink.receive` は自分で ack を書く（:355-359）ので、そのままでは outbox に使えない。書いた瞬間に acked になってしまう。
  6. health。
  7. auth の作成経路。
  8. journal に generation・part がない（:513-517）。
  9. outbox の後始末。
  10. 手動専用を守る gate。
- auth の作成・承認の現状: 手書き JSON。検証は `load_authorization` だけ（`confirm_human is True`、actor・reason が非空、未失効・未 revoke: :100-157）。`confirm_human` は自己申告で、作成 CLI も receipt もない。`--confirm-human` + reason + receipt 経路は mcs_view の requests / control 用（`mcs_view.py:815,823,884-896`、`mcs_requests.py:135-140,368`）で、ext export は通らない。`SECURITY.md:33-35` の人承認操作の一覧にもない。テストは JSON を直書き（`test_ext_contract.py:21-34`、`integration/test_mcs_recovery_narrative.py:737-748`）。`created_at` は非負有限値だけ、`expires_at` は上限なし、`retention_days` は正整数だけ検査される。

**設計方針**

A. envelope 層（`ext_contract.py`）:
- CD-1 の `_canonical` 正規化を実装する（:71-73）。
- `build_envelope(..., part=None, require=())` を用意する。`require=("meta","coverage")` で欠落時に `envelope_meta_missing` / `envelope_coverage_missing` とし、`part` を出力する。
- `_validate_envelope` に optional `part`、サイズ上限 `envelope_too_large`、profile 検査を足す。
- `C1_FIELDS=("meta","coverage","message","message_body","patient_coverage","signal","signals_truncated")` と `apply_c1_profile(auth)`（CD-6）を追加する。ライブラリの既定（:201）は変えない。
- patients が `"all"` 以外だと build が coverage を落とす（:230-235）ので、C1 では `envelope_coverage_missing` で拒否する。

B. 入力選別と分割:
- `select_records(records, allowed, only_with_facts, since_days)`: 未許可 type を落とし、種別ごとの件数を返す（縮小だけ）。`--only-with-facts` は CD-3 の規則で message を落とす（facts 非空・tombstone・`message_body` 持ちは残す）。meta・coverage・patient_coverage・signal・signals_truncated は常に残す。`since_days` を与えたときは `posted_at_ts >= 窓の開始 epoch` の message（とそれを参照する `message_body`）に絞り、窓を適用したときは `patient_coverage.history_floor` に窓の開始を反映する（CD-10）。フィルタ後に build するので、`records_sha256` は自然にフィルタ後になる。
- `split_envelopes(records, auth, gen_at, max_bytes)`: 共有 records（meta・coverage・patient_coverage・signals_truncated）を各分割に複製し、message / signal を入力順に貪欲充填する（`message_body` は対応する message と同じ part。CD-2）。各分割を `build_envelope` で構築し、実サイズを検査する。単一 record が超過なら `record_too_large`。message / signal が 0 件でも、共有だけの envelope を 1 つ作る。全分割を先に構築し、1 つでも拒否なら journal に触れない。その後に順次 deliver する。

C. receipt と sink:
- `parse_receipt()`（CD-5）。journal に `rejected` 終端を追加する（:449-451、`_reconcile` の終端 :573、`_deliver` の prior 判定 :493-510）。`_valid_ack` に status 検査を足す（:546-553）。
- `HandoffSink(LocalSink)`: `drop_ack` / `drop_delete_ack` を恒久 True にし、dir を 0700 にする（`LocalSink` の dir は既定で 0755 になる: 実測）。outbox への書込みだけを行う。`reconcile --receipts PATH` は strict parse の後に `acks/`・`deletions/`・`rejections/` へ原子的に置き、既存の `reconcile`（:566-599）と `_delete_ack`（:638-650）に任せる。終端（acked / rejected / withdrawn）に達したら outbox の payload を削除する（journal と audit は残す）。brain_export の 62 日 sweep（`brain_export.py:32,267-292`）の対象外になる PHI 近傍ファイルを残さないため。`sent` の payload が欠けていたら、health で `payload_missing` と報告する。
- `_set_journal(..., "sent", ...)` に `snapshot_generation_id` と `part` を保存する。

D. CLI（main を subcommand 化。サブコマンドなしは従来どおり deliver として動かす）:
- `deliver --auth --records --state --sink [--only-with-facts] [--max-bytes N] [--dry-run] [--sink-kind handoff|local]`
- `reconcile --state --sink (--envelope-id ID | --all) [--receipts PATH]`（`PATH` は単一の receipt JSON・receipt bundle の NDJSON・receipt ファイルを含むディレクトリのいずれかを受ける。計画レビュー決定 2026-09-29: receipt bundle に対応し一括取込する）
- `handoff`（計画レビュー決定 2026-09-29: **日常コマンドは引数なし**）。`config.json` に `ext_export{auth, state_dir, outbox, since_days}` プロファイルを持ち、`handoff` 1 つで export → 選別 → 分割 → outbox 書出し → 要約表示まで行う。`handoff` と `reconcile --receipts PATH` の 2 コマンドで一周する（既存の deliver の明示フラグ形式は互換として維持する）
- `link-hints`（計画レビュー決定 2026-09-29: hermes ローカル専用）。`project_id`・患者名（ledger の `patients.patient_name`）・最終投稿日の一覧を hermes 機の端末に表示するだけの subcommand。ファイル出力・送付はしない。氏名は wire に載せない原則は不変
- `withdraw --state --sink --envelope-id ID`
- `health --state [--auth FILE] [--list]`
- 任意で `validate --envelope FILE --profile c1`（fixture と受信側検証用）、`auth-create`、`auth-revoke`。
- exit code の注意: 手渡しでは held / ack_unknown が正常状態（受領待ち）。現行の「acked 以外は 1」（:682）は `--sink-kind handoff` では 0 にする。
- 出力は `envelopes[{envelope_id, part, records, bytes, status}]`、`dropped_types`、`messages_dropped`。`messages_kept:0` は警告として明示する。

E. health:
- journal/*.json を走査する。破損は `corrupt` に計上して落とさない。`sent` + `held` を held として数え、`delete_held`、`acked`、`withdrawn`、`refused`、`rejected` も出す。最古 held の age（`attempts` の ts）、auth の `{state, days_left}`（`load_authorization` は失効で例外になるので、raw の `expires_at` から計算する）。
- tick の `health.json` には入れない。手動専用（同文書 L136）で、`health.json` の `notify.held` は別の意味（`run_check.py:121-124,167`）。

F. auth の作成: 最小案は作らないこと（文書のテンプレートと health の期限表示だけで足りる）。本番前に承認証跡が要るなら、`auth-create --confirm-human --reason R --actor A [--days ≤90]` を足す（argparse の `required=True` は `mcs_view.py:815` と同型）。C1 プロファイルの既定値で 0600 書出しし、audit に `auth_create` を記録する。`auth-revoke --confirm-human --reason` で revoked を原子的に書き換える。reason に Q6 の判断記録の参照を含める運用にする。

やらないこと（YAGNI）: signature、CD-9・CD-10 以外の新 record 型、自動再送、スケジューラ。

**成果物**
- 上記のコード、tests、`docs/external-export-contract.md` の運用章、`SECURITY.md` の人承認操作一覧への追記、`scripts/update_readme.py` の再実行。
- 新規 `mcs/**/*.py` または `tests/**/test_*.py` の追加は `DEVELOPMENT.md` の件数（`scripts/update_readme.py:89-93`）を変える。CI の PR 検査が `git diff --exit-code` で落とすので、必ず再実行する。
- gate を 2 つ追加する: (1) `ext_contract.py` / `export_schema.py` が network・subprocess 系の module を import しないこと。(2) tick 経路（`mcs/ingest`・`mcs/notify`・`deployment/`）が `ext_*` を参照しないこと。
- コミットグループ案（Devflow の検証済み論理グループ単位）: G1 契約層（`_canonical` 正規化、`part`、profile、サイズ、drift guard。C0 の参照実装と重なる）、G2 選別と分割、G3 receipt・`rejected`・HandoffSink・outbox の後始末、G4 CLI と health（と任意の auth）、G5 golden fixture・文書・README 再生成・gate。

**受入条件とテスト（合成のみ。CLI は現状 0 件なので新設）**
- `--only-with-facts` の後で hash が計算される。未許可 type の除去。tombstone を残す。
- 分割の各性質: 上限内、meta + coverage が各分割にある、message が排他的で和集合が全件、決定的な id、`part` の整合、単一 record 超過の拒否、0 件で heartbeat。
- 7 種・meta・coverage の強制（builder と receiver）。`message_body` と `message` の同一世代・`body_sha256` 一致・8 KiB/`body_truncated`・`content_omitted` との排他・`patient_coverage` の `fetch_state` enum を fixture で固定する。
- receipt: 未知キー・型・reasons の形式・合計不一致の拒否、rejected は終端で再送されない、rejected status が acked にならない。
- HandoffSink: receipt の前は held、投入後に acked、削除 receipt で withdrawn、終端で payload 削除。
- CLI の一周と旧フラグの互換。health の件数・age・auth 日数・破損 journal。0700 権限。
- E2E: 合成 ledger に `canonical_projection` の行を直接入れ（`tests/views/test_read_model.py:25-38` の `_artifact` パターン）、`publish_snapshot`、brain_export、deliver（`--only-with-facts --max-bytes 4096`）、receipt、reconcile、withdraw、health の順。ネットワークと Keychain は `tests/conftest.py` のガード（:114-120）下で動く。

**依存**: C0（CD-1〜CD-10。特に CD-1）。本番投入（実データでの最初の envelope 作成）は #4 修復の実施記録と Q6 の判断後だけ（Q6 は記録済み: 2026-09-29）。合成での開発とテストは Q6 と切り離して進めてよい。

**規模**: L。実装約 500 行、テスト約 700 行、文書。

**オーナー判断・リスク**
- `patients:"all"` は archived 患者の message も送る。`read_model._message_records` に archived の絞り込みがない（`read_model.py:116-129`）。signal は archived を除外している（`mcs_signals.py:495`）。最小化したいなら select 層で除外する（snapshot DB の参照が要る）か、Q3 / Q6 で明示的に許容する。
- 毎回全件を送ると分割数が増える。10,000 message なら概算で数個〜10 個超の envelope。実データ非参照のため実件数は未確認。差分送付（`--changed-since` 等）が要るか判断が要る。
- `content_hash` は本文 HTML の sha256（`ledger.py:1055`）。短文・定型文は推測可能。Q3 の PHI 扱いに含める。
- brain_export は失敗時に前回の `export.jsonl` を残す（書込み順: :314-316）。古い世代を送らないよう `max_snapshot_age_s` を必須にする。
- 1 record の enum 不一致で export 全体が止まる。fail-closed だが可用性リスクがあり、drift guard で予防する。Q2 = off なら `--only-with-facts` は message 0 件になる。

## 4. C2 採用導線（改訂承認までは成果物なし）

**目的**: allergy・ADE・vital_lab の型付き値（数値・単位・日付）を projection として渡す。

**現状**
- 契約は aggregate 固定。`scope` は `aggregate` だけ許可（`ext_contract.py:126`、`docs/external-export-contract.md` L22,L117,L137）。`_FACT` は識別子と状態だけ（`export_schema.py:107-115`）。
- fact の `quantity` は自由文字列の便宜項目で、型付き値ではない（`semantic_projection.py:116-118`）。`semantic_quantities.py:1-8` は「便宜項目を読まない」と明記している。数値を evidence quote から決定的に取り出す層はない（`semantic_quantities` は既存の主張との照合だけを行う）。C2 の型付き値の正本は #15 の型付き labs にする（`docs/roadmap/extraction.md`）。

**設計方針（改訂承認後だけ）**
- `AUTH_CONTRACT` を `/2` にする（:43）。`scope` に第 3 値（例 `typed`）を足す（:126）。`detail` は引き続き無条件に拒否する。envelope 側の `scope` も整合させる。
- `export_schema._FACT` に typed 値を allowlist として追加する。値は finite_number、単位は閉じた enum、日付は `_date`（:26-27）。`read_model.SCOPES`（:36）と `_fact_relations`（:209-247）に scope 分岐を足す。
- 送るのは evidence quote と機械照合できた値だけ。未確定は送らない。fixture と receipt の手順は C1 を再利用する。

**成果物**: 改訂されるまで hermes-mcs 側はなし。

**受入条件とテスト**: 改訂後だけ、合成のみ。allowlist の変更なので、golden の再固定とレビューが必須。

**依存**: Q9、`docs/external-export-contract.md` の detail 禁止条項（L22・L137）の改訂とオーナー承認、#15。zaitaku-calender C2 は待たない。

**規模**: L。型付き値の決定的抽出（新規）が支配的。

**リスク**: aggregate の定義（同文書 L117）から外れるので、PHI 分類の再判断（Q6・Q3）が要る。誤抽出した単位・日付を確定と読ませない設計が要る。現時点で着手を勧めない。

## 5. C3 マシン送信（任意・手動のまま）

**目的**: 手渡しを HTTPS 送信に置き換える。実行は手動 CLI のままで、tick や launchd には組み込まない。

**現状**
- network コードはない（`ext_contract.py:4-9`、同文書 L133-138）。sink は directory 前提（`sink.root.resolve()`: :487,571,615、`_delete_ack` の `sink.root/deletions`: :640）。
- 再利用できる部品:
  - `mcs_util.NoRedirect` / `no_proxy_opener`（`mcs_util.py:318-329`）。
  - `bounded_http.bounded_http_request`（`bounded_http.py:147-`）: 別 interpreter で 1 リクエスト、秘密と本文は stdin 経由で argv に出ない、no-proxy / no-redirect（:109）、応答上限 262,144 B（:39）、絶対 deadline + kill。
  - `semantic_jev.JevClient` の `post_fn` 注入（:208-227）: テストの型としてそのまま使える。
- `bounded_http` の制約: GET / POST だけ（:92,:161）。Bearer と UA が固定（:102-104）。本文は既定 separators で再 JSON 化（:100-101。実測 +7〜8%）。3xx は HTTPError から status として返る（:112-113）。
- Keychain: 取得は `mcs_adapter._keychain_password`（:711-734）が `security find-generic-password -s <service> -w`（秘密は stdout、argv は service 名だけ。locked を区別）。保存は `mcs_setup._keychain_store`（:587-）が `security -i` の stdin で行う。`scripts/keychain_to_env.py` は MCS パスワードを `.env` へ複写する再起動用 fallback で、ext token には使わない（平文 at-rest を増やさない）。

**設計方針**
- 新規 `mcs/ops/ext_transport.py` を作り、`ext_contract.py` を network 禁止のまま保つ。
  - `HttpsSink`（receive・ack・has・matches・delete・delete_ack・binding）。
  - `endpoint_allowed(url)`: https だけ、host は設定と一致、port は 443 / 省略、userinfo / query / fragment なし、path prefix 固定。`envelope_id` は `_check_id`（:290-293）で 24 hex に限定してから path に埋め込む。
  - `keychain_token(service, run=subprocess.run)`: locked は `token_unavailable`。token は sink 構築時（journal に触る前）に取得する。`_deliver` は `sent` 記録の後に sink を呼ぶ（:513-519）ため、途中で失敗すると偽の held が残る。
- `GovernedExporter` を最小限リファクタする。`sink.root.resolve()` を `sink.binding()` へ、`_delete_ack` を `sink.delete_ack(eid)` へ変える。`LocalSink` は従来値を返す。
- endpoint は auth に入れない（同文書 L21 は label だけ）。`config.json` の `ext_export` は**単一ブロック**とし、C1 の `{auth, state_dir, outbox, since_days}`（§3 D）に任意キー `endpoint`・`keychain_service` を追加する。`mcs_setup.CONFIG_RULES`（:101-119）に validator（https 強制）を足す。
- 契約付録（C0 か C3 で合意）: `POST /ext-export/v1/envelopes`（`envelope_id` で冪等）、`GET …/receipts/{id}`、`POST …/envelopes/{id}/delete`（`bounded_http` は DELETE 不可）、`GET …/deletions/{id}`。
- status の写像: 2xx かつ receipt が journal と一致なら acked。422 かつ rejected receipt なら rejected。2xx でも receipt が不一致・欠落、401 / 403 / 408 / 429 / 5xx、timeout、接続断、3xx はすべて held（journal は `sent` のまま）。自動再送はしない。deliver が held の再送を拒否する挙動（:497-502）と、同文書 L138 のとおり。再送は新しい `auth_id`（→ 新 `envelope_id`）で行う。`reconcile` は GET だけで安全。
- canonical で 1 MiB 以下を送信前に検査する。受信側 route 上限は canonical 1,048,576 B で固定。wire 膨張（+7〜8%）は送信前検査を約 900,000 B に絞って吸収する（C0 合意事項 (a) と同じ）。`bounded_http` の raw body 対応は保留。

やらないこと（YAGNI）: 自動リトライ、スケジューラ、mTLS（Q8 次第）、token を `.env` へ複写。

**成果物**
- `ext_transport.py`、config validator、契約付録。`SECURITY.md`（「人工知能（AI）の使用箇所と情報の行き先」）と README の行き先要約の更新（現状は `SECURITY.md:20` 付近の Jev・通知先の記述だけ）。同文書の「含まないもの」章（L133-138）の改訂。README 再生成（新 mcs モジュール分）。`ext_contract.py` の `_mcs_path` ブートストラップ（`ext_transport` が `bounded_http` / `mcs_util` を import するため、`brain_export.py:14-18` と同じ 2 行）。

**受入条件とテスト（合成のみ）**
- fake `post_fn` で各 status の写像を確認し、`calls==1` で二重送信がないこと（held の後の deliver も送らない）。
- URL policy の拒否（http、別 host、userinfo、query、`..`）で `calls==0`。
- token の取得失敗で journal が無変更。token が argv・audit・journal・例外文字列に出ない。oversize を拒否。
- reconcile が 200 で acked、404 で held / not_received。withdraw は lost 時に delete_held で、再 withdraw は再送しない。
- conftest のガード（socket は `tests/conftest.py:114-118`、`security` プロセスは :79-92）下で通る。共有機構の redirect 拒否と応答上限は `tests/semantic/test_semantic_transport.py:161` で担保済みなので重複させない。

**依存**: Q6、Q8、同文書 L31-33 の 3 条件（named provider・access review・separate permission）、C1、契約付録。

**規模**: M。

**オーナー判断・リスク**
- `bounded_http` は Bearer 固定で、mTLS や Cloudflare Access 系の追加ヘッダには対応できない。Q8 次第で、`bounded_http`（Jev / LLM 経路にも影響するため別レビュー）を拡張するか、ext 専用 worker を作る。
- 送信先・権限・token 保管の承認が要る。応答が失われた場合は常に held となり、手動 reconcile の運用負担が残る。

## 6. C4 通知の縮退（任意）

**目的**: Discord / Slack 通知から患者名を外し、PHI の露出面を減らす。

**現状**
- 名前の出力は次の 4 ファイル（約 15 箇所）:
  - `notify_render.py:55-60` の `_patient_name`（関所）。呼出しは :183（thread の source_fp）、:186 と :210（signal の source_fp に名前が入る）、:266（signal カードの「患者」欄）、:290（本文表示）、:342（本文表示タイトル）、:374（thread カード見出し）。
  - `notify_cards.py:46,1075-1085` の `_thread_name`（Discord スレッド名 `💬 {name} — mm-dd`、`card_thread:true` 時: :1001-1004）。旧 ROADMAP になかった。
  - `notify_flush.py:187-191`（signal テキスト）、:353-354 と :379（新着本文の見出し）。
  - `semantic/semantic_render.py:194-196,253-256`（要約通知の `【名前】`）。旧 ROADMAP になかった。
- 名前以外の PHI もカードに載る。投稿者名（`_sender_tag`: :76-）、本文全文（`notify_render.py:270-273,298,337`、`BODY_MAX_CHARS`）、要約（`semantic_render`）。名前だけ外しても、本文中の氏名は残る。
- `hermes_plugin/` に名前を扱う箇所はない。runner 側だけの変更で、gateway 再起動は不要。
- config は `notify` ブロック（`notify_cards.py:22-30`、`notify_cfg` :282-284、検証は `mcs_setup.py:146-`）。テキスト経路は送信時に `_config()` を再読込する（`notify_flush.py:53-54,233`）。`_card_content`・`_source_fp`・`_card_body_text` は cfg を受けない（`notify_render.py:165,309,359`）。呼出元は `notify_cards.py:764,1057,1530,1584,1610` と `notify_transport.py:332`。

**設計方針**
- `notify.show_patient_names`（bool、既定 true で現状維持）を `_validate_notify` に追加する。
- `_patient_name` を唯一の関所にし、false なら `""` を返す。既存のフォールバックが働く（`project N`: `notify_cards.py:1076-1077`、`notify_render.py:374`、`notify_flush.py:379`）。signal の「患者」欄は空なら省略される（:266-268）。
- cfg の渡し方は 2 案。(a) `notify_render` にモジュール変数のポリシー（`set_display_policy(cfg)` を runner の dispatch 入口で呼ぶ）: 差分が小さいが隠れ状態になる。(b) 上記 3 関数以降へ cfg を引き回す: 署名変更が広い。推奨は (a)。`semantic_render` は、呼出側（`semantic_drain.py:1022`）で cfg を渡すか、同じ関所を置く。
- fingerprint に名前が入る（:183,:186,:210）ため、フラグの切替で、開いているカードは `source_changed` になり再 render される（`notify_transport.py:332-336`）。運用注記に書く。

**成果物**: 上記のコード、config 検証、tests、`SECURITY.md` / README の注記。#13 の digest の再評価メモ（文書だけ）。

**受入条件とテスト（合成のみ）**
- `tests/notify/` で `notify_testkit` の `_patient(led, name="合成患者B")` パターンを使う。
- フラグ off で、render spec（containers・parts・thread_name・footer）とテキスト通知に合成名が出ない。既定 on では現行とバイト同一（既存テストは無改変で緑）。digest は名前を含まない。fingerprint は決定的。切替で再 render される。

**依存**: zaitaku-calender C2 の運用実績、Q5。コードは独立に着手できる。

**規模**: S。`semantic_render` まで cfg 経路を通すなら M 寄り。

**オーナー判断・リスク**
- `C4-D1` 縮退の範囲: L1 = 名前だけ、L2 = 投稿者名も、L3 = 本文と要約も外して件数と MCS リンクだけ。ROADMAP の「患者名を外し通知だけにする」がどれを指すか確認が要る。
- `C4-D2` 既定値。名前を外した後に通知から患者を識別できない運用上の問題（`project N` だけになる）。
- 切替時の再 render。

## 実施順

1. **C0 の前提の決定**（コードなし）: **残りは CD-1〜CD-10 と上記の C0 合意事項の合意**（Q1〜Q4・Q7・Q8(b)・Q10・Q11・#8-D2・`prev_content_hash` 追加しない、は 2026-09-29 決定済み、Q6 記録済み）。特に CD-1 は初回の実送信前が期限。
2. **C0**（M）: 参照実装の変更（CD-1・CD-2・CD-4・CD-5・CD-6・CD-9・CD-10 の実装）、fixture・golden・MANIFEST、drift guard、文書。zaitaku-calender C0 と同時に進める（Q8(b): zaitaku 側コード実装は P0 後だが、C0 契約作業と hermes 側参照実装は進める）。
3. **C1**（L）: G1 → G2 → G3 → G4 → G5 の順。合成環境で完結。本番投入は #4 の実施記録と Q6 の判断後。
4. **C4**（S）: コードは独立に着手できる。運用開始の判断は zaitaku-calender C2 の実績後。
5. **C3**（M・任意）: Q6・Q8・契約付録の後。
6. **C2**（L・ゲート付き）: Q9 の承認後だけ。着手を勧めない。
