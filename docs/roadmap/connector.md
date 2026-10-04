# zaitaku-calender 接続（C0〜C4）の hermes-mcs 側の詳細計画

2026-10-04版割当: 旧1.0.13〜1.0.15の残件は全て安定稼働版1.0.13へ集約。
成果物・CLI・受入の正本は[1.0.13開発計画](../development/RELEASE_1.0.13.md)。
当時の調査・設計例と現在の実装状態を区別し、既存実装は再実装しない。

### 2026-10-04追記: ローカルreceipt実装

[ext_contract](../../mcs/ops/ext_contract.py)は`mcs-ext-receipt/1`のaccepted/delete結果を照合し、
LocalSinkとHandoffSinkの取込・失効・抑制・削除状態を扱います。
Handoffの自己ackや受領拒否を成功として扱わず、journalと監査を保持します。
[export allowlist](../../mcs/ops/export_schema.py)の検査はローカル合成の根拠であり、
外部サービスの同意・接続・削除保証を証明しません。
元のC0〜C4、CD-1〜CD-10、Q1〜Q12と公開条件は保持し、実送付・配備を受入済みとしません。

[`docs/ROADMAP.md`](../ROADMAP.md#6-zaitaku-calenderとの接続c0c4) の接続フェーズのうち、**hermes-mcs 側の成果物**の詳細計画。相手側（zaitaku-calender）の成果物は、対の文書 `ROADMAP.md` にある。フェーズ ID（C0〜C4）、未決事項の番号（Q1〜Q12。Q11・Q12 は zaitaku-calender 側が起票し本書へ同期した新規）、契約版（`mcs-ext-export/1`・`mcs-ext-auth/1`・`mcs-read-model/1`）、C0 の契約決定（CD-1〜CD-10）は両文書で共通。

2026-10-03照合: 以下の「現状」・行番号・相手側未実装の記述・実測は2026-09-29の調査記録。
現在の優先順位・版割当は[ROADMAP](../ROADMAP.md)を正とし、合意済みの判断本文は[引継ぎ記録](connector-decisions.md)に保持する。
C1の1.0.13割当はC0合意後の合成開発だけ。本番は#8-M1→#4の修復実施、#10の必要な独立レビュー・Q6・相手側ゲート後。
今回の1.0.11でC0/C1の契約・allowlist・相手repo・外部送付は変更しない。

### 2026-10-03の訂正提案（未合意・実装しない）

- **CD-9 自局判定**: 下記の「organizationまたはprofession一致」と「他組織の薬剤師はother_professional」は両立しない。
  `_self_sets`の既定professionは「薬剤師」であり、職種だけでは自局を特定できない。
  F-7の自局送信者IDを根拠にし、ID不明時は明示された自局organizationの一致だけを補助にする案をC0で再合意する。
  profession単独から`self_org`を断定しない。この提案は既存CD本文を変更せず、未合意の間は送出実装をしない。
- **C3 結果不明後の送付**: 新しい`auth_id`を作ること自体は未実行の証拠にならない。
  旧送付のreceipt/保存先を照合し、未受領または撤回完了が確認できるまで別IDでの送付も始めない。
  新authによる手動送付の条件を契約付録で明記する。自動再送禁止は維持する。

- 基準: v1.0.6 のコード（2026-09-29 調査）。行番号はこの時点のもの。
- 表記: 【実行確認】= 合成入力でローカル実行して確認、【未検証】= 実データ・実機・実 API・相手側実装に触れないと分からない点。
- 実データは読んでいない。相手側（TypeScript）の受信実装は存在しないため、相手側の挙動は Node での簡易実験だけで確認した。
- オーナー判断は項目内で `#N-Dk`（他項目）または `Q1〜Q12`（接続）と呼ぶ。

## 0. 結論

1. **既存の deliver / reconcile / withdraw・journal・audit は C1 でほぼ再利用できる**。`LocalSink` を自己 ack しない形（既存の `drop_ack` / `drop_delete_ack`: `ext_contract.py:341-342`）にし、receipt を `acks/`・`deletions/` に置くだけで、held → acked と delete_held → withdrawn が既存コードで動くことを実行で確認した。新規に要るのは、入力の選別、分割、receipt の厳格な parser、`rejected` 終端、CLI、health。`ext_contract.main` を呼ぶテストは現在 0 件。
2. **旧 ROADMAP §4 には、C0 で決めないと fixture を固定できない食い違いが 3 点ある**: (a) `coverage` に取得完全性が入っていない、(b) canonical JSON の数値表記が言語間で一致しない、(c) `fields` による除外は「除外」ではなく build 拒否。詳細は §1。
3. 規模は C0 = M、C1 = L、C2 = L（ゲート付き。着手不要）、C3 = M、C4 = S。
4. ベースライン（実行済み）: `scripts/run_tests.sh tests/ops/test_ext_contract.py tests/views/test_read_model.py tests/ops/test_brain_export.py` は 75 passed。`ci/gates.py` は 8/8、`ci/mine_gates.py --check` OK、`scripts/development/update_readme.py --check` exit 0。

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

**2026-10-04の互換設計修正（新export/2への分離をオーナー採用済み）**

旧`export/1`の`_canonical`を置換すると、保存済みrecords hash・ID・intentが変わる。
旧の認可既定7型・通常aggregate出力・journalを固定し、新しい数値表記と
本文/coverage/profileは明示した新契約へ分離する。wire名はオーナーが選択した
`mcs-ext-export/2`で、相手側の契約合意・受入は別途必要。
下表の「既存`_canonical`へ追加」は旧serializerの全体置換として実装しない。

また、分割`[A][B,C]`と`[A][B][C]`では先頭partのrecordsが同じでも集合が違う。
下表の「partをID/intentへ含めない」はこの場合を区別できないため、
新契約では契約版と`part:{index,count,set}`をID・intentへ束縛する。
先に各records hash、次にset、最後にIDを計算し、hashの循環を作らない。
旧`/1`の式は変更しない。契約版は選択済みで、hermes側のfixture pinは下記のとおり固定済み。
相手側の同一pin合意は未完了。

自局判定は職種一致だけでは行わず、sender identityと組織の根拠を別途合意する。
合意前の新しい本文候補は`sender_kind=unknown`を保持する。世代撤回の列挙は
受領後に消えるoutboxではなくjournalを使う設計とし、stagingの後始末を
撤回指示書の生成と混同しない。以下の旧設計文と異なる点はこの修正案で追跡する。

2026-10-04（引継ぎ後）のhermes側ローカル実装: 撤回指示書`mcs-ext-withdraw/1`
（理由コード4種・4,096 bytes・自由文なし）をhandoff outboxへ生成し、
`withdraw --generation`はjournalから展開する。合成参照受信側は`receive`
コマンドで受理・拒否・削除receiptのNDJSONを作り、受領件数と完全集合を分けて
報告する。`sender_kind`は既定unknownで、`--classify-senders`の明示時だけ
自局ID・設定済み自組織・職種から分類する（職種だけでは`self_org`にしない）。
C0 fixture（受理12・拒否23＋生成のみ15・receipt 6・withdraw 4）・
`MANIFEST.sha256`・fixture set IDを`tests/ops/fixtures/ext_contract_c0/`と
`docs/specs/external-export-contract.md`に固定した。相手repoとの同一ID合意、
CD-1〜CD-10の共同合意、実受領・本番受入は未完了。

先行した純粋検証は`mcs/ops/c1_contract.py`に実装済み。`canonical_json`は
有限値・safe integer・固定小数域・Unicodeを検査する限定canonicalで、
任意のRFC 8785互換やraw JSONの重複キー解決を保証するものではない。
`validate_profile`は明示7型・全患者・3600秒以内の鮮度・30日以内の保持を
検査するが、人承認・失効・送付直前再認可は別工程。
`validate_record`はbody/full messageのproject/message/generation一致と
8192 UTF-8 bytes/hash等を検査する。この単位はwireを生成せず、新版のID・part・
送出経路は別の統合単位で実装する。
関連279件と独立Bun数値oracle 28,597件の一致を確認し、旧経路は変更していない。

| ID | 決めること | 推奨 |
|---|---|---|
| CD-1 | 数値の canonical 表記と受信側の hash 検証 | RFC 8785（JCS）互換。規則は「整数値は整数表記（-0 は 0）、非整数は ECMAScript の数値表記（固定小数）。**指数表記になる値（|x| ≥ 1e21 または 0 < |x| < 1e-6）・NaN・Infinity・safe integer 範囲外は拒否**」。Python の `repr`/`json.dumps` は `1e-06` のような指数を出す（JS は `0.000001`。実測済み）ため、`_canonical` に専用の数値フォーマッタを実装する（ECMAScript `Number::toString` と同じ出力）。キーは code point 順、空白なし。受信側は parse 後に再計算する。fixture は整数値 float（`accepted/06`）に加えて、非整数の固定小数（`0.000001` を含む受理例）と指数範囲の値の拒否例を入れる（第2回計画レビュー修正。従来の「実装は前処理 10 行程度」は不十分だった）。**受理境界の注記（第3回）**: 入力 lexeme の指数表記（`1e0` 等）は parse で正規化されるため受理し canonical 再出力で整合を見る（検査不能）。オブジェクトキーの重複は両実装とも last-wins で一致するが wire での重複検査は行わない — 本規則は JCS の受理範囲を絞った subset であり、両側が同じ parse→再 canonical の手順を踏む限り self-consistent である |
| CD-2 | 分割集合の表現 | 各分割に meta・coverage・signals_truncated・**patient_coverage** を同一内容で複製し、message / signal を排他的に分配する（**`message_body` は対応する message と同じ part に置く**）。envelope に任意項目 `part:{"index":1..N,"count":N,"set":"<hex64>"}` を追加する（許可集合 :309-313 に足す）。**`set` は分割集合の束縛値**: 送信側は分割確定後、全 part の `records_sha256` を index 順に並べた配列の canonical JSON の sha256 を計算し、全 part に同じ `set` を書く（第二パス。`records_sha256` 自体は `records` だけの hash で変わらない）。受信側は (auth_id, snapshot_generation_id, part.count, part.set) で集合化し、index の重複・欠落・`set` の不一致を検査して揃うまで「不完全」と表示する — 同一生成を異なる分割設定で二度切った場合に part を混在させて「揃った」と誤認しないため（第2回計画レビュー修正）。`part` は `envelope_id`・`_intent_hash` の入力に含めない（各 part の `records` が排他的なため `records_sha256` が既に part を区別する）。**別 `set` の完全集合が後着した場合（第3回）**: 同一生成で先に揃った完全集合を current とし、異なる `set` を持つ後着 part は `409` / receipt `rejected`（理由コード `generation_set_conflict`）で拒否する — 二度切りは operator ミスか改ざんの疑いで機械判定できず fail closed とする。解消は正しい側の withdraw → 再送。同一 `set` の再送は冪等（replay） |
| CD-3 | `--only-with-facts` の意味と伝播 | 残す条件は facts 非空、`body_state=deleted`（tombstone）、**または `message_body` を持つ**（CD-9 導入に伴う拡張。facts なしの返信本文を落とさないため）。受信側は、完全集合が届いた世代で、前世代の staging のうち再掲されないものを「MCS 側で現在は事実なし / 不明」に落とす（削除はしない）。届かない返信を受信側は「返信なし」と表示しない。Q2 = on が決定済み（facts 前提の fixture）。Q2 = off なら message は body 持ちと tombstone だけになる。**降格の適用範囲（計画レビュー決定 2026-09-29、第2回で coverage_ts 境界を追加）**: (b) 降格は、その患者の `patient_coverage` で `fetch_state='complete'`・`history_floor` が非 null で、かつ `history_floor <= posted_at_ts <= coverage_ts`（検証済み範囲内）の item にだけ適用する。`coverage_ts` より新しい item は未検証の頭部間隙なので降格しない。`history_floor` が null の患者は降格しない。(c) `history_floor` より古い item は降格せず、保持期限（30 日）で自然に消える |
| CD-4 | 取得完全性 | `coverage.collection` に `patients_incomplete`（`fetch_state≠'complete'` の件数、算出不能は null）を追加する（read_model の既存 `suppress` 流儀に合わせる）。患者単位は `patient_coverage` record（CD-10。Q10 決定済み: 送る）。#3 の `known_gaps` を後で加える余地がある |
| CD-5 | receipt | 下記 (4)。envelope 単位の all-or-nothing（参照 receiver が最初の不正で全体拒否する挙動と同じ: :304-307）。`rejected` は終端。**receipt bundle（計画レビュー決定 2026-09-29）**: 手渡しの往復回数を減らすため、受信側は NDJSON（1 行 1 `mcs-ext-receipt/1` オブジェクト）の bundle を 1 回でダウンロードでき、hermes は `reconcile --receipts PATH`（単一 JSON・NDJSON・ディレクトリ）で一括取込する。契約 `mcs-ext-receipt/1` 自体は不変で、bundle は輸送上の便宜 |
| CD-6 | C1 プロファイル | fields は 7 種（`message_body`・`patient_coverage` を含む）を**明示列挙して必須化**（省略時の既定 `RECORD_TYPES` 拡張に新 record 型を含めない — `build_envelope` の `auth.get("fields", RECORD_TYPES)`（`ext_contract.py`）で既存 auth が本文送付を暗黙許可することを防ぐ。第2回計画レビュー修正）、patients は `"all"`、`max_snapshot_age_s` 必須（≤3600）、`retention_days` ≤ 30（Q3 の min(retention, 30) と producer 側を一致させる。Q3 決定済み: PHI として扱う）。**`fields` 必須化の移行（第3回）**: `fields` 未記載の既存 auth ファイルは profile 検査で fail closed の明示エラー（`auth_fields_required`、終了コード 1）とし、エラーメッセージに対処法（auth に 7 種を明示列挙）を出す。新規 `auth create`（計画中）は `fields` を必須生成する、meta・coverage 各 1 件必須、stat・attachment は受信側でも拒否する。producer と receiver の両方で強制し、期待コードを固定する。**`since_days`（窓付き送付。計画レビュー決定 2026-09-29）**: auth の項目ではなく hermes の `config.json` の `ext_export` プロファイル / CLI 引数で直近 N 日の message に絞れる。受信側は `patient_coverage.history_floor`（CD-10）で窓の有無を知る（`history_floor = max(ledger の floor, 窓の開始 epoch)`）。`max_snapshot_age_s` は送信側だけの検査（§1-6）なので、受信側は `snapshot_generated_at` と受信時刻で自前の鮮度閾値を持ち、超過は拒否でなく「古い」警告として age を常時表示する。**受信側の閾値はテナント設定・既定 72 時間（計画レビュー決定 2026-09-29）。マシン送信時の既定は C3 で決める**（「手渡し 24 時間・マシン送信 1 時間」の初期案は撤回） |
| CD-7 | signal 同一性 | 畳み込みを許容し、両文書に明記する。signal 件数の一致検証はしない |
| CD-8 | fixture 正本（Q7） | hermes-mcs を正本にする（Python の参照実装から生成するため）。zaitaku-calender へコピーし、両 CI で `MANIFEST.sha256` を検証する。変更は同一変更単位（両文書とも同じ運用） |
| CD-9 | 本文 record `message_body`（Q1 の決定による契約拡張。2026-09-29・オーナー。確定文言は計画レビュー決定 2026-09-29） | `mcs-read-model/1` の `_SCHEMAS` に新 record 型 `message_body` を追加する: `message_id`（必須。対応する `message` record と同一世代・同一 part に置く）・`body_text`（string。UTF-8 で **8,192 bytes 以下**。hermes は `messages.body_text`（タグ除去済み: `ledger.py:7-9`）を送る。超過は送信側で UTF-8 の文字境界で切詰め、`body_truncated=true`）・`body_format`（格納形式の enum。**v1 の値は `text` のみ**。`html` は予約語で v1 の受信側は拒否する）・`body_sha256`（**送信した `body_text`（切詰め後）の UTF-8 bytes の sha256**。受信側は自己整合性として再計算する。`content_hash`（本文 HTML の sha256: `ledger.py:1055`）とは一致しない）・`body_truncated`（bool）・`sender_kind`（enum: `self_org` / `physician` / `nurse` / `care_manager` / `other_professional` / `patient_family` / `unknown`。氏名・個人特定属性は送らない。`self_org` の判定は `mcs_signals._self_sets`（`signals.self_organizations` / `signals.self_professions` config + `self_profile_v1` artifact の既定値: `mcs_signals.py:155-186`）を根拠にし、organization または profession が自組織集合に一致する場合に限る。それ以外は profession のキーワードで `physician`/`nurse`/`care_manager`、sender_type で `patient_family` を判定し、他組織の薬剤師や複数所属は `other_professional` に落とす（`self_org` を断定しない）。写像表は `docs/specs/external-export-contract.md` に置く。判定不能は `unknown`）。**送出条件（第2回計画レビュー修正）**: `body_state='full'` かつ `body_text` が非 null の message にだけ付ける（`snippet`・`unknown`・`deleted` では付けない → 受信側は「内容未取得」と表示。file-only 投稿（`body_text=''`）は空本文の record を送る）。`content_omitted` は投影で落ちたフィールドがあることの印であり本文の取得可否を示さないため、付随条件は `body_state` だけで決める（従来の「`content_omitted=true` の message には付けない」は撤回）。body の編集・削除は `body_sha256` の変化または body の消失として表れ、message の tombstone（`body_state=deleted`）で本文も消える。本文は自由文で PHI を含み得るため、受信側の staging は暗号化・read 監査が必須（zaitaku-calender `docs/adr-external-ingest-v1.md`） |
| CD-10 | 患者単位の完全性 record `patient_coverage`（Q10 の決定による契約拡張。2026-09-29・オーナー: 送る。`history_floor` の追加はオーナー判断 2026-09-29） | `mcs-read-model/1` の `_SCHEMAS` に新 record 型 `patient_coverage` を追加する: `project_id`（必須。**fetch 対象の全 project について 1 件ずつ出す — message が 0 件の患者も含む**（出ない患者は受信側で「不明」となるため。第3回計画レビューで明記））・`fetch_state`（enum: `pending` / `complete` / `incomplete`。`mcs_adapter.py:333` の既存値集合）・`coverage_ts`（**検証済みの履歴取得範囲の上端 epoch 秒**: この時点以下の投稿は全件取得済みが確認された境界。`ledger.coverage_ts()`（`ledger.py:1175-1188`。`set_coverage` は完了した walk でだけ進む `job_ops.py:503-522`）の値。0/未設定は null。**「最終取得試行時刻」ではない** — 第2回計画レビューで現行実装との不一致を確認し訂正。「最新の取得試行はいつか」は契約に含めない（必要になれば将来の契約版で別フィールドとして追加））・**`history_floor`（integer | null）**: hermes ledger の `patients.history_floor`（`ledger.py:898-916` 周辺。NULL/0 = 完了記録なし、-1 = 時系列の先頭まで取得済み、正 = 取得済み範囲の下限 epoch）を次のように写像する — 完了記録なし → `null`、-1 → `0`（先頭まで取得済み。下限なし）、正 → その epoch 秒。窓付き送付（`since_days`、CD-6）のときは `max(ledger の floor, 窓の開始 epoch)`（ledger が完了記録なしなら `null` のまま）。fixture 固定後の追加は契約 `/2` が要るため v1 に含める。受信側の規則: (a) `fetch_state` が `complete` でない患者、または世代に `patient_coverage` が欠ける患者を患者単位の「不明」と表示する（zaitaku-calender `ROADMAP.md` §4.4）。(b) CD-3 の降格は `fetch_state='complete'`・`history_floor` が非 null で `history_floor <= posted_at_ts <= coverage_ts`（検証済み範囲内）の item にだけ適用する（`history_floor` が null の患者は降格しない）。(c) `history_floor` より古い item は降格せず保持期限（30 日）で自然に消える。分割では meta・coverage と同じく各 part に複製する（CD-2）。allowlist の追加なのでレビュー対象 |

C0 の前の 2 つの決定は**両方決定済み（2026-09-29・オーナー）**: #8-D2 は wire enum 名を**現行名のまま**（改名しない）、`prev_content_hash` は**追加しない**（fixture 固定後の追加は契約 `/2` が要るため C0 で決定）。

C0 の合意事項は CD-1〜CD-10 のほか、次を含む（両文書で同一。zaitaku-calender `ROADMAP.md` §4.7）:
- **撤回指示書の形式と輸送**（第2回計画レビューで輸送を具体化）: `withdraw()` は現在 `sink.delete` を呼ぶだけで相手に届かない（合成再現で確認。指示書ファイルは生成されない）。`mcs-ext-withdraw/1`: `contract`・`envelope_id`・`auth_id`・理由コード enum（自由文なし）のみ、4 KiB 以下。**輸送は手渡しファイル**: handoff 系 sink では `withdraw` が指示書を outbox に原子的に生成（`.tmp` → rename）し、zaitaku-calender 側は envelope と同じアップロード画面で受領し、処理成功後に削除 receipt（`mcs-ext-receipt/1` の `deleted` 形）を返して receipt bundle（NDJSON）に含める。指示書は `envelope_id` 単位（part 分割では part 単位）で、一部 part のみ撤回はその envelope の staging だけが消える（世代は「不完全」のまま残る）。**世代全体の撤回は hermes `withdraw --generation <id>` が対象世代の outbox 上の全 envelope_id に 1 通ずつ指示書を展開して生成する**（wire は変えない。第3回計画レビュー）。受信側は `contract` を見て指示書に 4,096 B の別上限を適用する（envelope 用 1 MiB 上限を共用しない）。原本より先に届く withdraw は tombstone を先に置いて後着を拒否。本文の暗号文は item 単位の保持で、withdraw が消すのはその envelope の item link — 残 link が 0 かつ未採用なら削除する（zaitaku-calender `ROADMAP.md` §4.3 の多対多規則）。合意までは受信側 C1 の完了条件から外し、管理者による未採用 staging の即時 purge 手順で代替する。
- **item の版管理（payload hash 範囲。第2回計画レビュー追加）**: 受信側は同一論理キーの版を `payload_sha256` で区別する。対象は `content_hash`・`body_sha256`・`body_format`・`body_truncated`・`sender_kind`・`facts`・`relations`・`extraction` 各 `.state`・`state`・`body_state`・`posted_at_ts` の canonical JSON の sha256 とする — 本文だけの変更・本文消失・`sender_kind` 変更・A→B→A も別版として扱い、古い世代の遅着は current にしない。**配列の決定性（第3回）**: canonical はオブジェクトキーのみ整列し配列要素は並べ替えないため、ハッシュ入力に入る配列（`facts`・`relations`・signal の evidence 内配列を含む）は、各要素を canonical JSON にした文字列のコードポイント昇順にソートしてから入力する（wire の出現順に依存しない）。signal 同一性 hash の evidence 正規化にも同じ規則を適用する。互換 fixture・contract test にこれらのケースを含める。
- **受信側の鮮度閾値**（CD-6）と**サイズ上限の扱い**: 上限は受信 wire bytes で 1,048,576 B。手渡しでは送信側が canonical 出力そのものをファイル化して運ぶため wire ≡ canonical が保てる（実測: canonical ちょうど 1,048,576 B は受理、末尾 +1 byte で 413 — 第2回計画レビュー）。送信前検査は約 900,000 B に絞り C3 の再 JSON 化の増分にも余裕を持たせる。受信側は受信 bytes の sha256 を envelope メタに保持する（原本は保持しない。第2回計画レビューで hash の必要性を確認）。上限ちょうどの受理と +1 byte の拒否を fixture と route テストに含める。
- **`source` の対応付け**: message の論理キーの `source` は envelope のフィールドに対応しない。`destination` または `auth_id` へ対応付けるか、受信側の接続ラベルとするかを決める。
- **`content_hash` の null 取扱い**: wire で任意（null あり）。UNIQUE キーに入れない方針（同一性キー参照）のため、受信側で明示的な `'null'` 値へ正規化するか必須化を求めるかを決める。
- **message 同一性からの `content_hash` 除外**: 本書 2026-09-29 初版の ROADMAP §5 の文言（type + project_id + message_id + content_hash）は、編集のたびに別行となり supersede・撤回が壊れるため破棄する（ROADMAP §5 の新文言が両文書の正）。
- **拒否コード表**: 上記 (2) の期待コードに受信側独自のコード（`record_type_not_accepted:<type>`、`envelope_coverage_missing`、`envelope_meta_missing`、`envelope_too_large`、`generation_set_conflict`、`auth_fields_required` 等）を合わせて固定する。

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
  - `09-no-body-states`: `body_state` が `snippet` / `unknown` / `deleted`（tombstone）で `message_body` を持たない受理例（CD-9 送出条件。`content_omitted` との関係でなく `body_state` で決まる。第2回計画レビュー修正）。
  - `10-patient-coverage`: `fetch_state=incomplete` の患者を含む `patient_coverage`（CD-10）の受理例。
  - `11-history-floor`: `history_floor` が正・0・null の 3 患者を含む `patient_coverage` の受理例（CD-10 の `history_floor` 写像を固定）。
  - `12-fixed-point-float`: `generated_at` 以外に `0.000001`・`1.5` のような非整数の固定小数を含む受理例（CD-1。Python repr が `1e-06` を出す値で両実装の一致を固定）。
- `rejected/`（拒否。1 欠陥だけで、他の hash・count・id は再計算）:
  - `01` `forbidden_field:statement`（`facts[0].statement`）、`02` `forbidden_field:sender`（message 直下）、`03` `record_field_not_exportable`（`facts[0].future_text`）、`04` 同（`message.memo`）
  - `05` `envelope_integrity_invalid`（`records_sha256` 改ざん）、`06` 同（`record_count`）、`07` 同（`envelope_id`）
  - `08` `snapshot_generation_mixed`、`09` `sink_scope_not_aggregate`、`10` `envelope_fields_invalid`、`11` `envelope_id_invalid`
  - C1 プロファイル分（新規実装が要る。現行の参照実装は受理する）: `12` `record_type_not_accepted:stat`、`13` `envelope_coverage_missing`、`14` `envelope_meta_missing`
  - `16` `forbidden_field:sender_name`（`message_body` 直下の氏名系キー）、`17` `body_sha256` と `body_text` の不一致（自己整合性違反）、`18` 8 KiB 超なのに `body_truncated` なし（contract violation）、`19` `patient_coverage` の `fetch_state` が enum 外、`20` `message_body` の `body_format` が enum 外（`html`。v1 は予約語を拒否）、`21` `patient_coverage` の `history_floor` が負数
  - 第2回計画レビューで追加: `22` `body_state` が JSON `null`（wire の enum は `full`/`snippet`/`unknown`/`deleted` のみ。producer は DB の NULL を `'unknown'` に正規化して送り、null を出さない）、`23` `message_body` の orphan（対応 message が同じ part に無い、または `body_state≠'full'` の message に付随。自己整合性コード）、`24` `part` の形式違反（`index=0`・`index>count`・`count<2`・`set` が 64 hex でない）。part の `set` 不一致・別分割の混在は 1 envelope では完結しないため受信側の集合検査（ペア fixture）で固定する
  - `15` `envelope_too_large` は 1 MiB 超になるため生成だけで、コミットしない
  - 01〜11 の期待コードは現行実装に対する実測と一致。
- `receipts/`: `receive-accepted`、`receive-rejected`、`receive-mismatch-sha`（送信側は held 維持）、`delete-deleted`、`delete-failed`（`deleted:false`）、`bad-unknown-field`（parser 拒否）。
- `withdraw/`（`mcs-ext-withdraw/1` 指示書。合意待ちだが生成器の構造だけ先に置く）: `01-basic`（受理）、`02` 理由コードが enum 外（拒否）、`03` 自由文を含む未知キー（拒否）、`04` 4 KiB 超（拒否。指示書専用の別上限）。
- `index.json` に各 fixture の `expect`（accept / reject）、`code`、`profile` を持たせる。

(3) 置き場所と pin:
- 置き場所は `tests/ops/fixtures/ext_contract_c0/`（tests は mcs の領域名をミラーする規約）。パスに `data/` 要素や `config.json` を含めない（CI hygiene が `git ls-files | grep -E '(^|/)(data/|…config\.json$)'` で落とす: `.github/workflows/ci.yml`）。
- 生成器 `tests/ops/ext_fixtures.py` は tests/ops が sys.path に入る（`tests/conftest.py:29-31`）ので import できる。golden test は「再生成バイト == コミット済みバイト」かつ `MANIFEST.sha256` の照合とする。
- pin は 3 層: (1) envelope 内の `records_sha256` と `envelope_id`（決定的: :257,276-280）、(2) `MANIFEST.sha256`（`shasum -a 256 -c` 互換）、(3) MANIFEST 自体の sha256 を「fixture set id」として `docs/specs/external-export-contract.md` と相手 repo の文書に同一文言で記載する。

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
- `docs/specs/external-export-contract.md` の改訂: `part`、receipt、C1 プロファイル、canonical 数値、サイズ、失敗コード表。同文書の「含まないもの」（L133-138）は C3 まで維持する。
- 参照実装: `_canonical` の数値正規化、`part` の任意項目、`_validate_envelope` の profile・サイズ検査、`parse_receipt`、coverage 拡張（CD-4。`read_model._coverage` と `export_schema` の allowlist）、`message_body`・`patient_coverage` の record 追加と生成（CD-9・CD-10。`export_schema.py` の `_SCHEMAS`/`RECORD_TYPES` と read_model 由来の生成器）。`patient_coverage.history_floor` の写像（ledger の NULL/0 → null、-1 → 0、正 → その epoch。窓付き送付時は `max(floor, 窓の開始 epoch)`）、`sender_kind` の写像表を `docs/specs/external-export-contract.md` に置く、`select_records` の `since_days` 窓（CD-6）を含める。
- fixture、生成器、golden test、`MANIFEST.sha256`。
- drift guard test: `export_schema` の enum が `semantic_facts.FACT_KINDS` / `WORKFLOW_STATUSES` / `RELATION_TYPES`、`mcs_signals.DETECTORS`、`read_model.EXTRACTION_KINDS` と一致すること（F-4）。現状は 5 系統とも一致を実測したがテストはない。食い違うと `project_record` の ValueError（`export_schema.py:159-161`）で export 全体が止まる。
- Q6 の判断記録。`docs/dev-records/` に置く場合は `FIX-`・`TEST-` 等の ID 表記を避ける（`ci/mine_gates.py:36-39` が manifest 登録を要求する）。

**受入条件とテスト（合成のみ）**
- 追加先は `tests/ops/test_ext_contract.py`: golden とマニフェスト。accepted は `_validate_envelope` を通り、JCS の再実装で `records_sha256` が一致する。rejected は期待コード。receipt は parser の accept / reject と journal 遷移（accepted → acked、rejected → rejected、mismatch → held）。oversize は生成して `envelope_too_large`。drift guard。
- 共通コマンド（全フェーズ）: `scripts/run_tests.sh tests/ops/ tests/views/test_read_model.py`、CI 範囲の ruff（`make lint`。ローカルは uv 経由）、`python3 scripts/development/update_readme.py --check`、`python3 ci/gates.py && python3 ci/mine_gates.py --check`。
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
- `C1_FIELDS=("meta","coverage","message","message_body","patient_coverage","signal","signals_truncated")` と `apply_c1_profile(auth)`（CD-6）を追加する。C1 プロファイルは `fields` の明示列挙を必須とし（省略時の全種既定で本文が暗黙許可されないようにする。第2回計画レビュー）、ライブラリの既定（:201）は変えない。
- patients が `"all"` 以外だと build が coverage を落とす（:230-235）ので、C1 では `envelope_coverage_missing` で拒否する。

B. 入力選別と分割:
- `select_records(records, allowed, only_with_facts, since_days)`: 未許可 type を落とし、種別ごとの件数を返す（縮小だけ）。`--only-with-facts` は CD-3 の規則で message を落とす（facts 非空・tombstone・`message_body` 持ちは残す）。meta・coverage・patient_coverage・signal・signals_truncated は常に残す。`since_days` を与えたときは `posted_at_ts >= 窓の開始 epoch` の message（とそれを参照する `message_body`）に絞り、窓を適用したときは `patient_coverage.history_floor` に窓の開始を反映する（CD-10）。フィルタ後に build するので、`records_sha256` は自然にフィルタ後になる。
- `split_envelopes(records, auth, gen_at, max_bytes)`: 共有 records（meta・coverage・patient_coverage・signals_truncated）を各分割に複製し、message / signal を入力順に貪欲充填する（`message_body` は対応する message と同じ part。CD-2）。**第二パスで `part.set` を束縛する（CD-2 修正案）**: 全 part の `records_sha256` を確定してから index 順の canonical 配列の sha256 を計算し、各 part の `part` に書き込んでから `envelope_id`・`_intent_hash` を確定する。各分割を `build_envelope` で構築し、実サイズを検査する。単一 record が超過なら `record_too_large`。message / signal が 0 件でも、共有だけの envelope を 1 つ作る。全分割を先に構築し、1 つでも拒否なら journal に触れない。その後に順次 deliver する。
- **`message_body`・`patient_coverage` の生成（第2回計画レビュー追加）**: read_model の message 投影とは別の専用クエリで snapshot に対して行う。`message_body` は `body_state='full'` かつ `body_text` IS NOT NULL の message に対応付け（空文字列は送る。CD-9 送出条件）、`sender_kind` は `_self_sets`（`mcs_signals.py:155-186`）と profession/sender_type の写像表で決める。`patient_coverage` は `patients.fetch_state`・`ledger.coverage_ts()`・`patients.history_floor` から生成する（`coverage_ts` は検証済み範囲の上端）。`body_state` が DB NULL の message は `'unknown'` に正規化して送る（wire に JSON null を出さない）。

C. receipt と sink:
- `parse_receipt()`（CD-5）。journal に `rejected` 終端を追加する（:449-451、`_reconcile` の終端 :573、`_deliver` の prior 判定 :493-510）。`_valid_ack` に status 検査を足す（:546-553）。
- `HandoffSink(LocalSink)`: `drop_ack` / `drop_delete_ack` を恒久 True にし、dir を 0700 にする（`LocalSink` の dir は既定で 0755 になる: 実測）。outbox への書込みだけを行う。`reconcile --receipts PATH` は strict parse の後に `acks/`・`deletions/`・`rejections/` へ原子的に置き、既存の `reconcile`（:566-599）と `_delete_ack`（:638-650）に任せる。終端（acked / rejected / withdrawn）に達したら outbox の payload を削除する（journal と audit は残す）。brain_export の 62 日 sweep（`brain_export.py:32,267-292`）の対象外になる PHI 近傍ファイルを残さないため。`sent` の payload が欠けていたら、health で `payload_missing` と報告する。
- `_set_journal(..., "sent", ...)` に `snapshot_generation_id` と `part` を保存する。
- **撤回指示書の出力（第2回計画レビュー追加）**: `withdraw --sink-kind handoff` は `sink.delete` だけでなく、`mcs-ext-withdraw/1` 指示書（`contract`・`envelope_id`・`auth_id`・理由コード、4 KiB 以下）を outbox の `withdrawals/` へ `.tmp` → rename で原子的に書き出す。手渡しで相手へ運び、返ってくる削除 receipt（`mcs-ext-receipt/1` の `deleted` 形）は `reconcile --receipts` の対象に含めて `_delete_ack` の経路に流す。指示書の ledger への tombstone 記録は withdraw 時点で既に行われている。

D. CLI（main を subcommand 化。サブコマンドなしは従来どおり deliver として動かす）:
- `deliver --auth --records --state --sink [--only-with-facts] [--max-bytes N] [--dry-run] [--sink-kind handoff|local]`
- `reconcile --state --sink (--envelope-id ID | --all) [--receipts PATH]`（`PATH` は単一の receipt JSON・receipt bundle の NDJSON・receipt ファイルを含むディレクトリのいずれかを受ける。計画レビュー決定 2026-09-29: receipt bundle に対応し一括取込する）
- `handoff`（計画レビュー決定 2026-09-29: **日常コマンドは引数なし**）。`config.json` に `ext_export{auth, state_dir, outbox, since_days}` プロファイルを持ち、`handoff` 1 つで export → 選別 → 分割 → outbox 書出し → 要約表示まで行う。`handoff` と `reconcile --receipts PATH` の 2 コマンドで一周する（既存の deliver の明示フラグ形式は互換として維持する）
- `link-hints`（計画レビュー決定 2026-09-29: hermes ローカル専用）。`project_id`・患者名（ledger の `patients.patient_name`）・最終投稿日の一覧を hermes 機の端末に表示するだけの subcommand。ファイル出力・送付はしない。氏名は wire に載せない原則は不変
- `withdraw --state --sink --envelope-id ID [--reason CODE]`（`--sink-kind handoff` では上記 C の通り `withdrawals/` に指示書を出力する）
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
- 上記のコード、tests、`docs/specs/external-export-contract.md` の運用章、`SECURITY.md` の人承認操作一覧への追記、`scripts/development/update_readme.py` の再実行。
- 新規 `mcs/**/*.py` または `tests/**/test_*.py` の追加は `DEVELOPMENT.md` の件数（`scripts/development/update_readme.py:89-93`）を変える。CI の PR 検査が `git diff --exit-code` で落とすので、必ず再実行する。
- gate を 2 つ追加する: (1) `ext_contract.py` / `export_schema.py` が network・subprocess 系の module を import しないこと。(2) tick 経路（`mcs/ingest`・`mcs/notify`・`deployment/`）が `ext_*` を参照しないこと。（1.0.13 合成開発で実装済み: `ci/gates.py` の `ext_contract_offline`・`tick_no_ext`。対象は `ext_contract`・`export_schema`・`c1_*`、tick 側は `mcs_standalone/` も含む。直接の import・参照だけを検査し、`mcs_util` 経由の `urllib` 読込のような間接 import は対象外。回帰テストは `tests/meta/test_c1_export_gates.py`）
- コミットグループ案（Devflow の検証済み論理グループ単位）: G1 契約層（`_canonical` 正規化、`part`、profile、サイズ、drift guard。C0 の参照実装と重なる）、G2 選別と分割、G3 receipt・`rejected`・HandoffSink・outbox の後始末、G4 CLI と health（と任意の auth）、G5 golden fixture・文書・README 再生成・gate。

**受入条件とテスト（合成のみ。CLI は現状 0 件なので新設）**
- `--only-with-facts` の後で hash が計算される。未許可 type の除去。tombstone を残す。
- 分割の各性質: 上限内、meta + coverage が各分割にある、message が排他的で和集合が全件、決定的な id、`part` の整合、単一 record 超過の拒否、0 件で heartbeat。
- 7 種・meta・coverage の強制（builder と receiver）。`message_body` と `message` の同一世代・`body_sha256` 一致・8 KiB/`body_truncated`・`body_state≠'full'` への付随禁止・`patient_coverage` の `fetch_state` enum を fixture で固定する。
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
- 契約は aggregate 固定。`scope` は `aggregate` だけ許可（`ext_contract.py:126`、`docs/specs/external-export-contract.md` L22,L117,L137）。`_FACT` は識別子と状態だけ（`export_schema.py:107-115`）。
- fact の `quantity` は自由文字列の便宜項目で、型付き値ではない（`semantic_projection.py:116-118`）。`semantic_quantities.py:1-8` は「便宜項目を読まない」と明記している。数値を evidence quote から決定的に取り出す層はない（`semantic_quantities` は既存の主張との照合だけを行う）。C2 の型付き値の正本は #15 の型付き labs にする（`docs/roadmap/extraction.md`）。

**設計方針（改訂承認後だけ）**
- `AUTH_CONTRACT` を `/2` にする（:43）。`scope` に第 3 値（例 `typed`）を足す（:126）。`detail` は引き続き無条件に拒否する。envelope 側の `scope` も整合させる。
- `export_schema._FACT` に typed 値を allowlist として追加する。値は finite_number、単位は閉じた enum、日付は `_date`（:26-27）。`read_model.SCOPES`（:36）と `_fact_relations`（:209-247）に scope 分岐を足す。
- 送るのは evidence quote と機械照合できた値だけ。未確定は送らない。fixture と receipt の手順は C1 を再利用する。

**成果物**: 改訂されるまで hermes-mcs 側はなし。

**受入条件とテスト**: 改訂後だけ、合成のみ。allowlist の変更なので、golden の再固定とレビューが必須。

**依存**: Q9、`docs/specs/external-export-contract.md` の detail 禁止条項（L22・L137）の改訂とオーナー承認、#15。zaitaku-calender C2 は待たない。

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
- 当時調査した名前の出力は次の4ファイル。現行の患者サマリーを含む`notify_views.py`も対象として、C4着手時に呼出元を再照合する（計5ファイル）:
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
