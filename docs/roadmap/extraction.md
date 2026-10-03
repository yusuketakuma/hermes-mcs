# 抽出の詳細計画（#11・#15・#16）

[`docs/ROADMAP.md`](../ROADMAP.md) の #11 薬剤名正規化、#15 検査値の抽出、#16 添付 OCR と分類の詳細計画。

2026-10-03照合: 以下の「現状」・行番号・容量値・節番号は2026-09-29の調査記録。
現在の版割当・公開条件は[ROADMAP](../ROADMAP.md)を正とする。#11/#15は1.0.14、#16は未割当。
F-1の未確認検査値の表示分離は現行`structured_view._lab_lines`に実装済み。下記の「最優先・未修正」は当時の状態であり、#15全体の完成を意味しない。
辞書・外部保存・OCRの未決判断や外部送信範囲を今回変更せず、既存の抽出世代・モデルも維持する。

- 基準: v1.0.6 のコード（2026-09-29 調査）。行番号はこの時点のもの。
- 表記: 【実行確認】= 合成入力でローカル実行して確認、【未検証】= 実データ・実機・実 API に触れないと分からない点。
- 実データは読んでいない。オーナー判断は項目内で `#N-Dk` と呼ぶ（一覧は ROADMAP の「オーナー判断一覧」）。

## 0. 結論

- 3 項目とも C0/C1 を遅らせない。出力はすべてローカルの候補で、`mcs-read-model/1` の allowlist（`mcs/ops/export_schema.py:68-147`）は変更しない。allowlist に足すなら、C0 fixture の再合意と `mcs-ext-auth/2`（C2 / Q9）の別判断になる。
- **最優先は #15 の既存 labs 表示の安全化（F-1）**。合成入力で「値が自分の evidence と矛盾する」場合も「evidence がない」場合も、Discord/Slack カードの「検査:」行に確定表示されることを実行確認した。薬・症状・依頼は `unverified` を尊重しているが、検査値だけが抜けている。
- 番号の対応: §3-11 = 旧 v2 #3（優先順位 3）、§3-15 = v2 #10、§3-16 = v2 #9（優先順位 5）。

### 旧 ROADMAP の誤り・古い点

1. **§3-15 は既存実装を書いていなかった**。本文由来の labs 抽出は extract v4 で実装済み: `extract_llm.py:95`（プロンプト）、`:293-303`（schema）、`:789-826`（`_Validator.labs`）、`rollup.py:137-141,183-184`（`recent_labs`）、`structured_view.py:195-208,336-337`（「検査:」行）、`semantic_qc.py:22`（QC 対象に labs）、テスト `tests/extract/test_extract_llm.py:2061-2118`。旧版の「#9 依存」（添付依存）は本文由来の検査値には無関係。実際にないのは、v1 ルール・日付・基準範囲・bench・値と evidence の突合・unverified の扱い（下記 #15）。
2. **§3-11 の引用が弱かった**。`zaitaku-calender docs/operations.md:151` は 2026-09-05 時点の snapshot で、`:142` に「現在値として使用しない」とある。G-OPS-2 の定義は `docs/plans/implementation-plan.md:140,202`。利用条件の根拠は `docs/operations-common-master.md:111`（SSK の一般利用条件は無断使用・転載を制限）。`:113` の運営者「問題なし」は他薬局への配信・再配布の件で、根拠 URL もなく、hermes-mcs のローカル利用の許諾ではない。
   「YJ に体系を合わせる」も正確ではない。zaitaku 側の `code_system` は `ssk`（9 桁数字）と `yj`（12 桁 `[0-9A-Z]`）の 2 種だけ（`migrations/0028_master_drug_identifier_assertions.sql:17,24-25`）。YJ だけでは製品・有効期間が一意でない（`docs/operations-common-master.md:190`）。名前照合では製品レベル ID を返せない。
3. **§3-16 は「ローカル」の中身が未整理だった**。実機の llama-server は vision 不可。添付の実体は 14 日で削除され（`maintenance.py:25,164-230`）、`pruned` は終端で再取得経路がない（`ledger.py:1657-1662` は `state='pending'` だけ）。`docs/specs/lifecycle-spec.md:194` の「再 DL 可」は未実装の可能性がある。過去添付の OCR は §3-4 の補完とは別実装が要る。
4. **§4 C2「型付き値（vital_lab 等）」の出所が未定義だった**。semantic 層の `vital_lab` は statement / quantity の自由文字列だけ（`semantic_facts.py:372-410`）。legacy 投影は labs / vitals を出さない（`semantic_projection.py:174-270`）。canonical 有効時は読み側が extract_llm を隠す（`mcs_queries.py:167-186`）。C2 の型付き値の正本は #15 の型付き labs にすべきで、C2 の「依存」に §3-15 が必要。
5. 軽微: dev-records の O03「添付 OCR 未実装」（`docs/dev-records/review-20260923.md:82-83`）は ROADMAP の ID 体系にない。

## #11 薬剤名正規化（drug_map）

**目的**: 商品名・一般名・剤形付き・全半角・かなの表記ゆれを「同一成分の候補」に束ね、stats（`meds` / `med_mentions`）と rollup の薬剤集計を改善する。言及 → 候補まで。確定はしない。zaitaku-calender へは渡さない。#14（変更エピソードの紐付け）と semantic relation の前提になる。

**現状**
- v1: `extract.py:56-59`（`_MED_TOKEN` / `_MED_CTX`）、`:222-234`（name + dose だけ。単位は mg / μg / mcg / g / mL）、`:64`（RULE_VERSION=6）。
  - 【実行確認】「ロキソニン錠60mg」→ name=「ロキソニン錠」、「アムロジピンOD錠5mg」→「アムロジピンOD錠」（剤形が name に混入）。
  - 【実行確認】半角カナ「ﾛｷｿﾆﾝ錠60mg」・ひらがな「ろきそにん60mg」は未抽出（NFKC 未適用）。
- v4（extract_llm）: meds は `{name, dose, action, status, subject, negated, route, freq, prn, evidence, unverified}`。name は本文の表記そのまま（`:232-254`、`:688-744`）。merge は (name, subject, action)（`:1094-1101`）。EXTRACT_VERSION=4（`:62`）。
- semantic: FACT_KINDS に `medication_event` / `medication_exposure`（`semantic_facts.py:28-33`）。`validate_fact` は固定キーで薬剤 ID の欄がない（`:372-410`）。正規化は NFKC + casefold だけ（`:127-128`）。legacy 投影の name は最長カタカナ / 英数トークンで、特定不能は「処方薬」（`semantic_projection.py:50-57,209-232`）。v1 hint は `medication_exposure` 化（`semantic_extraction.py:827-832`）。
- 読み側は `$.meds[].name` の表層一致で動く。優先順位は v4 > canonical > extract_llm（`mcs_queries.py:167-186`）。rollup `_med_states`（`rollup.py:255-293`）、stats `st_meds`（`mcs_stats.py:378-404`）・`st_med_mentions`（:407-422。`needs` に drug_map: :721-724、notes に「未実装」: :401-402）、signals は (room, 表層名) のエピソード（`mcs_signals.py:250-329`）、structured_view は「薬剤候補（未確認）」の規約（`structured_view.py:293`）、brain_export（`brain_export.py:201-205`）、semantic relation は表層トークンで同一実体を判定（`semantic_relations.py:43,78-87`）。
- export: `_STATS.meds` は `action_totals` / `distinct_names`（整数）だけ（`export_schema.py:96-100`）。`_FACT` に薬剤欄なし（:107-115）。`read_model._fact_relations` は 5 キーだけをコピー（`read_model.py:229-240`）。stat は C1 で送らない。契約は aggregate 限定（`docs/specs/external-export-contract.md:22,117,137`）。
- 辞書・コード・アルゴリズムはリポジトリにない（`docs/guides/USER_GUIDE.md:362`、`docs/development/DEVELOPMENT.md:341-342` が未整備を明記）。
- 拘束: stdlib のみ（`ci/gates.py:96-129`）。ruff target は py310（`pyproject.toml:2`）。CCO / plugin は snapshot だけを読む（`docs/development/DEVELOPMENT.md:593-594`、`deployment/README.md`）ので、リーダー側で辞書ファイルを読める保証がない。

**設計方針**
1. **保存先は派生 artifact `med_ref`（新 kind。schema 変更なし。artifacts は汎用: `ledger.py:192-196`）**。writer 側で message 単位に生成する。
   - content: `{refs:[{i(source meds の index), name, status: resolved|ambiguous|unresolved|generic, cands:[{system, code, display, kind: ingredient|class, method: alias|stem|fuzzy, candidate:true}]}]}`。
   - meta: `{hash, source_kind, source_artifact_id, dict_id, dict_sha256, resolver_version}`。QC の source 束縛と同型（`mcs_queries.py:79-97`）。
   - 不採用案（extract_llm / semantic の内容や meta に書く）の理由: EXTRACT_VERSION を上げると全件の LLM 再抽出（`extract_llm.py:1614-1629,2103-2141`、3〜5 t/s: `:55-58`）と QC 再投入（Jev）になる。semantic-facts/v2 の契約変更になる。3 種の fact kind すべて・複数 writer に書く必要があり、不変性前提（`_replace_current` 等）に干渉する。
   - 利点: CCO が辞書を持たなくても snapshot 内で読める。辞書・resolver の更新は LLM なしで安価に再導出できる。出所（辞書版）が残る。rollup は DB だけに依存したまま保てる。
   - 生成は tick の derive（`run_check.py:646-` の extract_llm 後・rollup 前）で、deadline 付き。
2. **stage 1 は注釈だけ**。rollup の medications 行に `ref`（`PERIOD_CHECK_VERSION` 2→3 で再構築: `rollup.py:47,385`）、brain_export の「成分候補（未確認）」列、stats の `by_ingredient_candidate`（resolved / ambiguous / unresolved の分母付き。`DEFINITION_VERSION` 更新: `mcs_stats.py:31`）を足す。`_med_states` のキー・`med_is_patient_current`・signals のエピソード・カードは表層名のまま。誤統合で「A の stop が B を隠す」を作らないため。集約キー化（stage 2）は bench と人手サンプル確認の後（#11-D2）。
3. **`mcs-read-model/1` は不変**。新キーは `project_record` が落とし `content_omitted:true` になる（`export_schema.py:164-190`）。回帰テストで固定する。
4. **識別子**: `system` の語彙は zaitaku と同名（`ssk|yj`。master 行に `generic_code` あり: `0093_ssk_change_type_9_periods.sql:54`）。ただし名前照合は成分レベルだけを返し、製品レベル YJ/SSK を「解決済み」として返さない。zaitaku 側の確定照合は `code_system+code` の完全一致（`docs/functional-spec-v0.1.md:21`）。マスターの内容は import しない。最小案は、辞書に持たせたローカル成分 ID だけ。YJ 先頭 7 桁 = 成分という理解は一般知識で【未検証】。
5. **辞書**: 実辞書はリポジトリに入れない。`data/drug_map.json`（`data/` は gitignore 済み）に置き、なければ `unavailable`（stats の既存規約）。schema `mcs-drug-map/1`: `dict_id`, `source{name, url, terms_checked_on, approved_by}`, `entries[{id, kind, display, aliases[], codes{}, forms[]}]`。ロード時に sha256 を固定する。alias 衝突は ambiguous として保持し、上書きしない。`approved_by` 欠落は synthetic 扱いで、実データに適用しない（fail-closed）。
6. **照合（stdlib のみ）**:
   - `fold()`: `unicodedata.normalize("NFKC")` → casefold → ひらがな→カタカナ（+0x60）→ 長音・ハイフン統一 → `・` / 空白 / 括弧の除去。
   - `split_surface()`: 剤形語（錠 / OD錠 / カプセル / 顆粒 / 散 / DS / シロップ / テープ / パップ / 軟膏 / 点眼 / 注 / 坐剤…）、規格、屋号「」を分離する。剤形は `route` と別に保持する。
   - 照合順: 完全一致 alias → 剤形・規格除去後の完全一致。
   - **fuzzy は自動関連付けしない**。【実行確認】difflib 類似度は、LASA ペアのタキソール/タキソテール 0.91、サクシン/サクシゾン 0.89 が、正当な綴りゆれのワーファリン/ワルファリン 0.83、アムロジピン/アムロジビン 0.83 より高い。閾値で分離できない。fuzzy は `cands` に `method:"fuzzy"` の「参考」として載せるだけにする。
   - 【実行確認】`fold` で「ろきそにん」「ﾛｷｿﾆﾝ錠」は「ロキソニン(錠)」と一致する。ブランド → 成分（ロキソニン→ロキソプロフェン 0.62）は文字列類似では解けず、辞書が必須。
   - 総称（薬 / 処方薬 / 降圧薬…）は class か unresolved にする（rollup が除外する名前と揃える: `rollup.py:267`）。
7. **候補の提示**: 既存の「候補（未確認）」規約に合わせる。例「成分候補: X（辞書 dict_id@sha8・未確認）」。ambiguous は「複数候補」とし、成分名は出さない。確認操作は作らない（現行薬の正本は zaitaku-calender）。

**成果物**
- `mcs/extract/drug_map.py`（世代横断のモジュールなので `extract/` 直下。ライブラリだけで `Ledger(` を直接 open しないため `LEDGER_WRITERS` への追加は不要）。
- tick の配線、`rollup.py` / `mcs_stats.py` / `brain_export.py` の注釈、バージョン更新。
- `tests/extract/test_drug_map.py`（合成辞書は Python 定数。架空名）、`tests/views/test_mcs_stats.py` 追記、export の回帰テスト。
- `docs/guides/USER_GUIDE.md:362` と `docs/development/DEVELOPMENT.md:341-342` の更新、`scripts/development/update_readme.py` 実行。
- コミットグループ案: ① fold / split / 辞書ロード ② `med_ref` の導出と tick の配線 ③ rollup / stats / brain_export の注釈と version bump ④ docs。

**受入条件とテスト（合成のみ）**
- 架空辞書の table-driven テスト: 全半角・ひらがな・剤形 / 規格 / 屋号付き・長音の揺れが期待成分に解決する。類似度 0.9 以上の架空 LASA ペアが注釈されない。alias 衝突は ambiguous、総称語は resolved にならない。辞書なしは `unavailable`、`approved_by` なしは実データに非適用。dict / resolver の変更で再導出され、同一なら冪等、hash 変化・source artifact の置換で再導出、message 削除で非表示。
- 回帰: 同一成分の別名 2 件が rollup で別行のまま残る。片方の stop がもう片方を suppressed にしない。signals は非変更。stats の `by_name_month` は従来値不変。新ブロックは分母付きで、分母 0 は null。新キーが `project_record` で落ちる。
- bench: 決定的処理なので LLM bench ではなく、凍結した合成コーパス（表層 → 期待 / forbid）で P/R と forbid=0 を固定する。実データの確認はローカルだけ（distinct 名を人手レビュー、結果は repo に入れない）。stage 2 への移行条件（提案値: 人手 200 件サンプルで誤結合 0）は #11-D2 で確認する。
- 必須チェック: `scripts/run_tests.sh tests/extract/ tests/views/ tests/ops/`、CI 範囲の ruff、`update_readme.py --check`、`ci/gates.py`、`ci/mine_gates.py --check`。

**依存・順序**: 独立（#4 修復にも C1 にも非依存）。辞書の入手はコードと並行できる（合成で先行）。`fold()` は #15 と共有するので #11 を先に。#14 の前提。

**規模**: M（コード）。辞書の入手・利用条件は外部ゲートで期間不定。

**オーナー判断・リスク**
- `#11-D1` 辞書の出所と利用条件の承認。候補は、厚労省の薬価基準収載品目リスト、MEDIS の標準医薬品マスター、SSK の医薬品マスター、PMDA、またはオーナー自作の別名表。いずれも一次資料の利用条件は【未確認】で、SSK は無断転載の制限がある。自作案の運用は、stats の unresolved 上位を curation キューにする。
- `#11-D2` stage 1 を注釈だけにするか、集約まで許すか。
- `#11-D3` YJ 等のコードを保持するか。
- `#11-D4` stats の新指標を将来 C2 で出すか。
- `#11-D5` alias の保守者。
- 技術リスク: LASA 誤結合、配合剤（複数成分）、薬価改定・販売名変更による陳腐化、英字略称、tick 時間への影響【未計測】。

## #15 検査値の抽出

**目的**: 本文（後に添付 #16）の検査結果を、確認可能な候補として型付きで取り出す。時系列ビューは作らない（ROADMAP §6）。Q9 が承認された場合に、C2 が再抽出なしで投影できる形にする。

**現状**
- v1: vitals だけ（`extract.py:72-83,254-265`）。labs キーはない。【実行確認】「Cr 1.2 mg/dL、eGFR 45、K 4.1、HbA1c 7.2%、INR 2.1」→ `{}`。「血糖値145」は bs に拾われず、「血糖 145」は拾う。
- extract_llm: labs = `{name, value(数値|短文字列), unit, flag(high|low), evidence}`。`_LAB_FLAGS`（:571）、`_Validator.labs`（:789-826: name≤40、unit≤15、value 文字列≤30、evidence 検証、なければ unverified）。schema は `additionalProperties:false`（:303）。
  - vitals には本文最近接ラベルでの再照合がある（`_vitals_guard`: :428-515）が、labs にはない。merge は name だけで重複排除（:1109-1115）。
  - 【実行確認】value=12 に evidence「Cr 1.2 mg/dL」を付けても通過する。date / ref キーは黙って落ちる。"1.2" は文字列のまま。
  - 【実行確認】unverified の「Na 140」が「検査: Cr 12mg/dL・Na 140mEq/L」と確定表示される（`structured_view.py:195-208`）。meds / symptoms / requests は unverified を尊重している（:258, :218, :311）。
  - 表示先: `structured_lines` → `notify_flush.py:367`、`notify_render.py:149-163` → Discord/Slack。
  - rollup `recent_labs` は name キー・最新のみ・上限 15・`at` は投稿日時で測定日ではない（`rollup.py:85,137-141,183-184`）。読み手はない（brain_export は vitals / meds だけ: `brain_export.py:192-205`）。
- semantic: `vital_lab` は必須カテゴリ（`semantic_facts.py:38-42,54`）だが、値は文字列だけ。legacy 投影に labs / vitals はなく、canonical_facts に statement が載るだけ（`semantic_projection.py:174-270,282-325`）。canonical / v4 が現行だと型付き labs / vitals が読み側から消え、「バイタル・検査｜statement」の自由文だけが残る（`mcs_queries.py:167-186`、`structured_view.py:127-151`）。canonical の有効化ゲートは 2026-09-23 時点で未設定（`docs/dev-records/continuation-20260923.md:1115`）。現在の設定値は【未確認】。v1 hint は vitals → vital_lab に変換するが、証拠引用が `str(value)` で曖昧（`semantic_extraction.py:835-838`）。
- QC: Jev（外部 API・既定 OFF: `SECURITY.md:79-86`、`SECURITY.md:28`）。対象順は vitals, meds, symptoms, labs, events、上限 16 件の round-robin（`semantic_qc.py:21-22,123-130`）。質問文は labs で汎用（:149-154）。順序は `tests/views/test_qc_view.py:131-145`、`tests/extract/test_extract_qc.py:596-606` で固定。
- bench: `extract_bench._score_case` は labs の採点なし（`extract_bench.py:120-200`）。`evaluation/extract_cases.json` は 21 件で labs の期待は 0 件。既存コーパスは FROZEN（`extract_bench.py:4-6`、corpus_sha256 比較 :324-327）。
- zaitaku の測定値語彙: `measurement_type ∈ {height, weight, egfr, creatinine, ast, alt, other}`、value は 10 進文字列 ≥0（20 桁以内）、unit 必須、`measured_on` 必須（`src/validation/patient/patient-clinical-profile.ts:20-22,133-152`）。hermes-mcs の vitals に weight / height はない（`extract_llm.py:407`）。

**設計方針**
- **A. 既存 labs の安全化（S。バージョン据置。F-1）**
  1. `_Validator.labs` に数値突合を足す。値が evidence / 本文の数値トークンにないときは unverified にし、`drops["labs"]` に理由を記録する。`_repair_issues`（:1204-1219）が 1 回だけ修復再問する。
  2. 読み側ガード（既存行に再抽出なしで効く）。`_lab_line` と rollup `recent_labs` で、unverified・値 / evidence の不一致を「検査候補（未確認）」へ分離する。
  3. bench: `_match_lab`（name 折畳み一致 + 値 + 指定時 unit / flag）、labs の採点・forbid・失敗ケース分岐。**新コーパスは別ファイル `evaluation/extract_cases_labs.json`**。既存の FROZEN を壊さない。
- **B. ルール labs（M。RULE_VERSION 6→7）**
  - `extract.py` に `_LAB_LEXICON`（Cr / クレアチニン、eGFR、K、Na、HbA1c、INR、CRP、Hb、Alb、AST/GOT、ALT/GPT、BNP、BUN…）を足し、value / unit / (H|L|高|低|↑↓) / 基準範囲 / 明示日付（`_ymd`: :86-110 の past モード。なければ null で、投稿日を代用しない）を取る。
  - 文脈ゲート（採血 | 検査 | 結果 | 値、または単位付き。1〜2 文字名は数値隣接 + 単位必須）で「Kさん3回」等の偽陽性を抑える。
  - LLM へは hint として自動供給される（:1360-1372、上限 3500 字）。`_V1_CATEGORY_FIELDS["vital_lab"]`（`semantic_extraction.py:669`）と `_v1_hint_items`（:835-838）に labs を足す。
  - RULE_VERSION の影響: `_delete_stale`（`extract.py:310-329`）が全 v1 行を削除する。`run_pending`（:332-357）は limit も deadline もなく、全件を 1 行 1 commit で再生成する（`ledger.py:1814-1820`）。呼び出しは `run_check.py:657`。全患者の rollup が再構築される（`rollup.py:361-363,390`）。LLM コストはないが tick 内で長時間化する恐れがある【規模は実データ非参照のため未計測】。事前に `extract.py --all --limit`（:365,404-405）を run lock 下で分割実行するか、`run_pending` に limit を足す。旧行がない間は v1 依存フィールド（med_periods 等）が一時的に欠ける。semantic 側は RULE_VERSION を参照しない（rg で extract*.py 以外に出現なし）ので、既存の semantic 生成物は無効化されない。
- **C. 正規化（M。派生・バージョンなし）**
  - `labs_norm`: コード内 lexicon なので snapshot リーダーでも使える。analyte キーと単位の表記正規化だけで、換算はしない（`semantic_quantities.py` の no-conversion 方針と同じ）。value は 10 進文字列（≥0）、`measured_on` は明示日付だけ。zaitaku 語彙への写像は egfr / creatinine / ast / alt 以外を other とする。
  - 優先順位は既存規約（`structured_view.py:14-25`）。LLM が labs を出したら LLM 優先、なければルール結果を「検査候補（未確認）」とする。
  - rollup `recent_labs` は analyte キー・測定日優先で直近 N（提案 3）。時系列ビュー化しない。`PERIOD_CHECK_VERSION` を更新する。
  - OCR 文字混同（HbAIc / HbAlc、Sp02）は lexicon で吸収する（`A[1lI]c`、`Sp[O0]2`）。
- **D. LLM schema 拡張（date / ref）**: **EXTRACT_VERSION は上げない**。上げると全件再抽出（:1614-1629,2103-2141）、QC 再投入（Jev 予算）、chunk checkpoint 失効（:1819,1859,1888）が起きる。旧世代の再処理で backlog が増える旨は `docs/dev-records/review-20260923.md:76-77`。日付・基準範囲はルール（B）で取る。LLM に取らせるなら thin 再抽出方式（`_thin_pending_sql`: :1578-1599）で「検査の手掛かりのある投稿だけ」を 1 回再 pending にする。extract_llm から新モジュールを import する場合は `_LOADED_SOURCE_DIGESTS`（:63-66）に追加し、常駐 drainer を再起動する（`deployment/launchagents/README.md:95-112`）。
- **E. 添付由来**は #16 の OCR テキストに B のルールを適用する（LLM なし）。#16 の後。

**成果物**
- `extract.py` / `extract_llm.py` / `structured_view.py` / `rollup.py` の改修、`labs_norm`、`extract_bench` の拡張、`evaluation/extract_cases_labs.json`、tests、docs。
- コミットグループ案: ① A(1)(2) ② A(3) bench ③ B（RULE_VERSION は単独コミットで戻せる形）④ C ⑤ docs。

**受入条件とテスト（合成のみ）**
- A: 値不一致 → unverified、unverified の検査 →「検査候補（未確認）」の回帰テスト。既存の `test_validate_v4_labs`（`test_extract_llm.py:2061-2072`）、`test_structured_view_shows_v4_detail`（:2075-2098）、`test_multichunk_labs…`（:2105-2118）は維持する。
- bench: 合成 labs コーパスで P/R を報告し、forbid 違反 0。既存コーパスの各 field は同一 corpus_sha256 の paired report で変化なしを確認する（`extract_bench.py:321-339`）。目標値（提案 P≥0.95、R≥0.80）はオーナーに確認する。`--mock-ok` で期待値が `_validate` を通る（:258-268）。ケース例: 基準内外、単位表記ゆれ、全角、前回→今回、家族の値、予定 / 依頼だけ、値なし、別検査名の混同、複数日付。
- B: ルール単体（表記・単位ゆれ・範囲・H/L・日付解決・偽陽性ネガ）。RULE_VERSION bump の移行テスト（`test_extraction_review.py:229` の rule_version-1 パターンを流用）。合成 5 万行 fixture で再生成時間を計測して、未計測を解消する。
- C: 換算しないこと、value≥0・20 桁、`measured_on` は明示だけ、unverified の分離。
- QC: labs の局所・決定的検査（値 ∈ evidence、単位 ∈ 許可表、analyte の妥当域）を Jev と独立に足す。QC の順序・16 件上限は不変。Jev QC を labs に使う範囲は #15-D5。
- export: labs 由来キーが `project_record` で落ちる（allowlist 不変）ことを固定する。
- 必須チェックは #11 と同じ。

**依存・順序**: A は独立で最優先。B / C は #11 の `fold` を再利用する。D / E は #16 依存。C2 / Q9 の前提。B の RULE_VERSION 再生成は、§3-4「hash/artifact 再生成」と同じ窓で 1 回にまとめる。

**規模**: A=S、B=M、C=M。全体 M。

**オーナー判断・リスク**
- `#15-D1` `recent_labs` の保持数（1 か N か。ROADMAP §6 の時系列廃止との境界）。
- `#15-D2` weight / height を vitals に足すか。
- `#15-D3` 型付き値を C2 で出すか（Q9。allowlist 変更 + `mcs-ext-auth/2` + `docs/specs/external-export-contract.md` の改訂）。
- `#15-D4` RULE_VERSION bump の時期。
- `#15-D5` Jev QC（外部）の適用範囲。
- 誤値表示（A で緩和）、単位・基準の混在（HbA1c の NGSP / JDS、Cr の mg/dL と μmol/L）、前回値 / 今回値の取り違え、`bs`（血糖）が vitals と検査の境界にあること。

## #16 添付 OCR と分類（ローカル）

**目的**: 画像 / PDF の添付をローカルだけで「種別分類 + テキスト化」し、未確定の候補として参照可能にする（処方箋 / 検査結果 / 写真 / 書類）。確定・外部送信・自動登録はしない。

**現状**
- 保存: `attachments` テーブル（`ledger.py:173-178`、追加列 :349-354）。MCS API は url / name だけで MIME を持たない（`mcs_adapter.py:294-298,423-437`）。実体は `data/attachments/<attachment_id>`（拡張子なし。ディレクトリ 0700: `run_check.py:59,624,1142-1143`）。DL は許可ホストだけ・64MiB 上限・`.part` → rename（`mcs_adapter.py:54-59,1334-1391`）。「隔離」の実体は failed 状態 + 試行上限 + .part 清掃（`ledger.py:43-45,1614-1649`）で、ファイル種別の検査やマルウェア隔離はない。tick では `--download-files` 時だけ `stage_attachments`（`run_check.py:616-641,941`）。
- 保持: 14 日で実体削除 → `pruned`（`maintenance.py:25,164-230`）。`pruned` は終端で再取得経路なし。`withdrawn` は次回 prune まで実体が残る（:200）。
- 抽出側: 添付は未解析（`SECURITY.md:54`、`SECURITY.md:44`、O03）。v1 は本文の「写真|画像|添付」で `media_ref` を立てるだけ（`extract.py:171-172`）。semantic は添付メタを bundle に載せ（`semantic_store.py:86-101`）、「添付内容は解析していません」と限定する（`semantic_llm.py:287-289`）。`attachments_complete` は設定元がなく常に True（`semantic_extraction.py:1122-1123`）。
- 既存の外部露出: 通知設定時は添付の実体が Discord 等へ転送される（`SECURITY.md:26`、`notify_flush.py:583-611`）。Jev（外部）は semantic / QC 有効時に本文を送る（`SECURITY.md:79-86`）。OCR テキストをそこへ流さないこと。
- ローカル LLM は text 専用: `local_llm.chat` は `messages[].content` が str（`local_llm.py:373-379`）、`_validate_chat_args` は prompt を str に限定（:339-343）。実機の llama-server に対する `GET /props`（loopback。本文は送っていない）で `modalities.vision=false`、total_slots=2、n_ctx=32768/slot、Qwen3.5-9B-Q4_K_M を確認した。plist に `--mmproj` はない（`deployment/launchagents/ai.mcs.llamaserver.plist:6-20`）。`~/.hermes/models` は 0.8B と 9B の gguf だけで mmproj もない。モデル系列は VLM 可能でも未ロード。mmproj の入手可否とメモリ余裕は【未検証】。デコードは 3〜5 t/s（`extract_llm.py:55-58`）で、VLM の長い出力は 1 枚数分になり、2 slot の抽出 drainer を塞ぐ。
- 受付境界: `DEFAULT_ROUTES`（`llm_admission.py:60-75`）は mcs.extract / semantic / qc / bench=BACKLOG。`MCS_LLM_ADMISSION` 有効時は未登録ルートが恒久拒否され、`mcs_setup check` が検査する（`mcs_setup.py:534-556`）。既定 OFF（`local_llm.py:114-120`）。
- OCR / vision の実装はリポジトリに存在しない。
- 【実行確認】（合成画像・PDF だけ。macOS 26.5 / arm64、Swift 6.3.3 CLT）:
  - `osascript -l JavaScript` から Vision `VNRecognizeTextRequest`（accurate、ja-JP + en-US）が、新規 Python 依存なしで動作する。日本語行を正しく認識した。初回 8.7 秒（OS 側モデル初期化と推定）、以降 0.2 秒。
  - `env -i` の最小環境で動作し、壊れたファイルは `{"ok":false}` で正常に失敗する。`-e` スクリプト + `--` で argv を渡せる（.py 内に文字列で埋め込める）。
  - `sandbox-exec '(deny network*)'` 下でも同じ結果（同 profile で curl は失敗。遮断は有効）。
  - PDFKit で PDF のテキスト層を直接取得できる（OCR 不要）。`VNClassifyImageRequest` は document / printed_page を返す。
  - **誤読**: 「HbA1c」→「HbAIc」「HbAlc」、「SpO2」→「Sp02」「Sp0296%」（語結合で数値化け）。解像度を縮小すると誤りが非単調に変化する。confidence は 0.5 / 0.3 の粗い値だけで閾値ゲートに使えない。→ 必ず「未確認候補 + 元画像参照」とし、数値は人が原画像で確認する。
- CI は ubuntu-latest（`.github/workflows/ci.yml`）で osascript が使えない。

**設計方針**
- **前提決定（オーナー）**: 対象範囲（新規添付だけ先行）、OCR テキストの保持・表示範囲（通知・brain_export・export に出さない）、macOS 標準機能（osascript / Vision / PDFKit）の subprocess 利用が `AGENTS.md`「新しい外部依存を加えない」に抵触しないことの明示承認（#16-D1〜D3）。
- **Stage 1（M〜L）ローカル OCR + 分類**
  - `mcs/extract/attach_ocr.py`（ライブラリ）。実行は `[sandbox-exec -p '(version 1)(allow default)(deny network*)'] /usr/bin/osascript -l JavaScript -e <埋込 JXA> -- <path>`。環境は最小（`bounded_http._worker_environment`: :139-144 と同型）。deadline で kill・reap（:192-215 と同型）。出力 JSON はサイズ上限 + スキーマ検証。macOS 以外や osascript 不在は `unavailable`（クラッシュしない）。
  - 種別は magic bytes（JPEG / PNG / GIF / WebP / HEIC / PDF / TIFF）で判定する（拡張子も MIME もないため）。画像は縮小せず原寸で OCR する。PDF はテキスト層優先で、空なら画像化して Vision にかける。サイズ・ページ・画素に上限を設ける。
  - 分類は 2 層: structural（text_document / photo_like / pdf_text / unreadable / unsupported。OCR 行数・文字量・classify）と、subtype 候補（処方箋・お薬手帳・検査結果 等。NFKC 折畳み後のキーワード + ファイル名で判定し、空でもよい）。すべて `candidate:true` / `unverified:true`。
  - 保存は新 artifact kind `attachment_ocr`（schema 変更なし）。meta: `{attachment_id, sha256, ocr_version, engine, os_version, lang, pages, error/attempts/next_try(extract_llm.py:1450-1500 の規約)}`。content: `{structural, subtypes, text(≤64KB), text_sha256, lines}`。pending = downloaded かつ現行 `(attachment_id, sha256, ocr_version)` の artifact なし。
  - current 判定は sha256 一致 かつ `state≠withdrawn` かつ `message.body_state≠'deleted'`（tombstone 規則: `structured_view.py:10-12`）。withdrawn / 削除で不可視化 + sweep。`pruned` は OCR テキストを無効化しない（#16-D4）。
  - 配線は `stage_attachments` 直後に `stage_attachment_ocr`（時間予算付き）。14 日窓内に処理する。バックログ用 CLI を作る場合は `LEDGER_WRITERS`（`ci/gates.py:23-26`）への追加と `acquire_run_lock`（:268-330）が要る。
  - 遮断: OCR テキストを semantic bundle・Jev・通知・brain_export・export に入れない。CI ゲート案（`ci/gates.py` の AST 形式）は、`attach_ocr` が urllib / socket / http を import しないこと、`semantic_*` が `attach_ocr` を import しないこと。
  - 表示は `mcs_view attachments` に OCR 状態・分類だけ（テキストは明示要求時）。カードには載せない。
- **Stage 2（M）**: OCR テキストへ v1 ルール（薬剤 name + dose、vitals、#15 labs）を適用し、`ocr_hints` として同 artifact に保存する。「添付OCR候補（未確認）」として表示する。LLM は使わない。
- **Stage 3（任意・L）**: text-LLM で OCR テキストを構造化する場合は、新ルート `mcs.attach`（BACKLOG）を `DEFAULT_ROUTES` と `mcs_setup.py:539` の検査対象に足し、evidence は OCR テキストを source とする別契約にする。VLM は、mmproj の調達（モデル資産の調達 = 要承認）、llama-server の再起動、`local_llm.chat` の複数パート content、夜間窓だけの実行が要る。優先度は低い。

**成果物**
- `attach_ocr.py`、`run_check` の配線、`mcs_queries.py` に `current_ocr_pred`、`mcs_view` の表示、gates、tests。
- 実機 smoke 手順（手動スクリプト + `docs/dev-records`）。調査時の probe 手順を流用できる。
- `SECURITY.md:54` と `SECURITY.md:44` の更新、`update_readme.py`。
- コミットグループ案: ① magic sniff + runner + 契約検証（stub）② artifact / pending / current / tombstone ③ tick 配線 + gate ④ 分類 ⑤ 表示 / docs。

**受入条件とテスト（合成のみ）**
- CI は Linux なので、runner を注入した stub 中心にする: JSON 契約検証（過大・未知キー・不正型は fail-closed）、timeout kill、上限（サイズ / ページ / 画素）、magic sniff 表、分類ルール表（キーワード・photo_like）、冪等（同 sha256 は再処理しない、sha256 変更で再処理）、withdrawn / deleted の不可視化と sweep、osascript 不在 → unavailable、`candidate` / `unverified` の必須化、`LEDGER_WRITERS` / writer_lock ゲート、no-network AST ゲート、OCR テキストが semantic bundle・通知・export に出ないこと（export は `project_record` で落ちる）。
- QC / bench: 合成 OCR 出力コーパス（HbAIc / Sp02 / 桁結合の誤読パターン入り）で、後段（#15 lexicon の吸収、数値は常に unverified）を検証する。Jev QC は OCR テキストに使わない。OCR 精度は Mac 上の手動 bench（合成画像を Swift で生成し、文字誤り率を報告）。実画像の精度は未評価と明記する。
- 実機 smoke（手動）: launchd / hermes cron 配下で osascript + Vision が動くか。調査時の検証は対話シェルだけ【未検証】。
- 必須チェックは #11 と同じ。

**依存・順序**: Stage 1 は #4 に非依存（新規添付だけ）。過去添付は `pruned` の再取得経路の新規実装が前提。#15-D / E は Stage 2 の後。C1 と独立。オーナー決定が先。

**規模**: L（Stage 1 単独でも M〜L）。

**オーナー判断・リスク**
- `#16-D1` 対象範囲（新規添付だけ先行するか）。`#16-D2` OCR テキストの保持・表示範囲。`#16-D3` macOS 標準機能の subprocess 利用の承認。`#16-D4` `pruned` の添付の OCR テキストを無効化するか。
- 処方箋画像は氏名・保険者番号等の PHI 密度が高い。OCR テキストの保存・表示範囲と、既存の Discord 添付転送との整合を決める。
- 画像 / PDF デコーダの攻撃面。subprocess + sandbox-exec を併用するが、sandbox-exec は非推奨 API で、将来の macOS での継続性は【未検証】。
- Vision の結果は OS 版で変わる（`meta.os_version` + 再 OCR 方針）。macOS 限定。
- ANE / GPU が llama-server と競合するか【未計測】。初回のモデル資産取得のネットワーク要否【未検証。調査時はキャッシュ済みで sandbox 内でも動作】。
- VLM は現状不可。`attachments_complete` は未配線（常に True）。

## この領域の実施順

0. オーナー決定（並行）: #11-D1（辞書の出所と利用条件）、#16-D1〜D4、#15-D4（RULE_VERSION bump の時期）。
1. **#15-A（S）**: 既存 labs の安全化と bench。依存なし。現在の未確認 / 誤値表示経路の是正。
2. **#11 stage 1（M）**: `fold` / 辞書ローダ / `med_ref` / 注釈（合成辞書で先行、実辞書は承認後）。#15-A と並行できる。
3. **#15-B / C（M）**: ルール labs（RULE_VERSION 7 は §3-4 の再生成と同じ窓で 1 回にまとめる）と正規化（`fold` 再利用）。
4. **#16 stage 1（L、新規添付だけ）→ stage 2 → #15-D / E**。過去添付は再取得経路を別タスクにする。
5. 任意: #11 stage 2（集約キー化）、#16 stage 3（LLM / VLM）、C2 向けの型付き値の出力（Q9 承認後）。

全体の位置づけ: §3-1〜10（backup・修復など）と C0 / C1 が上位。#11 → #14、#15-C → C2 の順。3 項目とも C1 を遅らせない。
