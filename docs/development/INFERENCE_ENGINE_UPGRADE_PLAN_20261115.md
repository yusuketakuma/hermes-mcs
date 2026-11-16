# 推論エンジン v1.0 改版計画（統合案・訂正反映版）

この計画は読取りだけで作りました。ファイルの編集、テストの実行、外部への接続はしていません（HEAD d0fa64f）。土台は採点がいちばん高かった Design 4（段階移行・安全重視）です。Design 1 からは取得と増分処理を、Design 3 からは誤断定検査と評価の固定を取り入れました。審査で「安全でない」「確認できていない」とされた案は、外すか所有者の確認待ちにしています。コードで確かめた訂正（A〜F）もすべて反映しました。

前提にした既存の計画は次のとおりで、ここでは再提案しません。
- `docs/development/PATIENT_CONTEXT_REBUILD_PLAN_20261115.md`
- `PATIENT_CONTEXT_TIMELINE_DESIGN_20261115.md`
- `PATIENT_CONTEXT_P0_PLAN_20261115.md`
- `CLOUD_SEND_FOUNDATION_DESIGN_20261115.md`
- `CLOUD_API_REVIEW_20261115.md`
- `PATIENT_CONTEXT_ROUTING_REVIEW_20261115.md`（子が0本で止まる問題、:60）
- `PATIENT_CONTEXT_STABILITY_REVIEW_20261115.md`（M1、M4、P0-5 を含む）

---

## 要点（非開発者向け）

- **「バージョンアップ」の進め方。** 版の番号は2種類あります。
  - 画面や記録に出る公開版「1.0」（`mcs/core/inference_version.py:7`）。
  - 抽出の中で使う内部の世代番号（`:9` の `LLM_EXTRACT_VERSION=11` など）。

  取得項目の追加とプロンプトの改善は、**内部の世代を1回上げるだけで実現できます**。公開版を 1.1 に上げると、抽出・品質確認・意味解析・Jev・患者コンテキスト・集約をすべてやり直すことになります。さらに今のコードは、1.0 の作業を 1.1 に引き継げません。そのため**公開版は 1.0 のまま据え置くことを推奨します**。
  
  ただし仕様 `docs/specs/inference-engine-v1.md:28` は「推論の意味を変える変更では推論版を更新する」と定めています。項目の追加はこの変更に当たる可能性があります。1.0 は今も「開発目標のみ」（`inference_version.py:6`）の扱いなので、受入前の 1.0 の中で改める余地はあります。仕様をどう解釈するか、または改定するかを含めて、所有者の決定 O1 で確認してください。
- **処理を軽くします。** 抽出の固定の指示文（約13.7K字）は、ローカルでは slot の prefix cache、クラウドでは cache_heads で再利用されています。毎回まるごと送り直しているわけではありません。縮めた効果が出るのは、主に slot の窓（16384トークン/slot）の空きと、キャッシュの書込みです。重複した出力を減らし、変わっていない投稿は推論し直さないようにします。挨拶だけの投稿を前置フィルタで除外する範囲を広げると、LLM の呼出しも Jev の品質確認も減ります。
- **取得項目を足します。** 薬の剤形・飲む時点・処方期間を取ります。測定値の日時と条件は、既存の labs の形を使います。ほかに予定の日付と相手、摂取量・水分量・最終排便（既存の nutrition・excretion カテゴリに載せる）を取ります。どれも原文の引用を必須にします。アレルギーと ACP は、誤って断定したときの危険が大きいので、extract/llm では次の世代に回します。意味解析 v2 ではすでに抽出しています。
- **安定性とメモリ。** 確認済みの不具合を、版を変えずに先に直します。主なものは次のとおりです。
  - 常駐 worker が、一時的な DB エラーで落ちる。
  - 設定の書込み中に、子プロセスが落ちる。
  - 補助機能の失敗で、抽出が止まる。
  - 選択処理で、過去のジョブを行ごとに走査する。
  - 配信 journal が無期限に増える。
  - LINE WORKS の受信本文が、一時ファイルとして残る。
- **費用は、全件の再処理1回分を正しく見積もります。** 抽出を世代12にすると、次の3つも全件やり直しになります。
  - Jev による抽出の品質確認（extract_qc）。
  - 添付の推論。
  - 患者コンテキストの再走査。

  患者コンテキストは、配備から作り直しまで止めておけば二重払いを防げます。
- **表示は、再抽出が終わるまで一部が劣化します。** 集約と測定値は、世代11の結果を表示し続けます。一方で薬の時系列と QC の状態は、世代11の行を扱えなくなります。まだ再抽出していない新しい項目は、「なし」ではなく「未取得」と表示します（既存の計画 M1 に従います）。

---

## 現状の問題（根拠つき）

### 版の扱い
- コードが読めるのは {0.1, 現行} の2つの版だけです。
  - 読む側の `_class_scope`（`mcs/semantic/semantic_due_selection.py:245-249`）と `_adopt_pending_arrival`（`:262-`）が引き継げるのは、LEGACY（0.1）だけです。
  - `operation_contract.py:124-126` は、それ以外の版を `unsupported_inference_version` で拒否します。
  - `brain_export.py:410` も、旧 rollup を許すのは 0.1 だけです。
  - このまま 1.1 に上げると、1.0 の pending job が処理されないまま残り、export も止まります。
- 版を直書きで判定している箇所があります。
  - `semantic_store.py:364`
  - `semantic_intake_hold.py:376,441`
  - `semantic_due_selection.py:56`
  - `operation_contract.py:125`
  - `extract_quality.py:210-214`
  - `views/extraction_audit.py:45`
  - `extract/eval/audit_cohort.py:490-498`
  - `semantic_qc.py:114,132,141,155`（SQL に `'{CURRENT_VERSION}'` を直接埋め込んでいます。実在を確かめました）
- 旧 worker の書込みを拒否する fence は、`inference_version.py` の mtime が変わっただけでも働きます（`extract_llm_source_contract.py:253-282`）。版の値を変えない編集でも、全 writer が止まります。

### 抽出（mcs/extract/llm）
- 固定の先頭 `_PROMPT_HEAD` は約13,714字です。内訳は仕様が約6.0K、例が約7.7K です（`extract_llm_prompts.py:14,42,123`）。出力上限は `MAX_TOKENS=1600`（`:11`）です。
  - この先頭はキャッシュで再利用されています。縮めた効果は、slot の窓（16384トークン/slot。`semantic_llm.py:205-209`）の余裕と、キャッシュ書込みの削減に出ます。
  - 修復用の先頭 `_REPAIR_HEAD`（`:227`）は549字しかありません。クラウドでキャッシュの対象にならないのは、`mcs/llm/cloud_inference.py:342` が明記しているとおりの意図した設計です。
- `events` と `event_records` を二重に出力させ、検証側で作り直しています（`extract_validation.py:968-974`）。`event_records.span`（文字位置）も LLM に推定させています。
  - `events` だけが返ったときは、手がかり語からレコードを作る代替経路があります（`clinical_events.py:858-871`）。
- `reply`（`extract_llm_prompts.py:127`）と、detail の `medication_kind/prescriber/response/certainty/valid_period`（`:31`）は、使われていないわけではありません。
  - detail は区別なく下流へ流れています（`mcs/patients/rollup_builder.py:1063`、`mcs/views/patient_context_view.py:81-103,228`）。表示の見出しにも使われています（`mcs/notify/notify_views.py:235,1346-1347`。DETAIL_LABELS の定義は `patient_context.py:84-121`）。
  - `reply` は評価資産の採点に使われています（`mcs/extract/eval/extract_bench.py:284-393`）。
- 例3（`:61`）に、原文にない推測「在宅酸素は調剤対象外」があります。仕様の「推測で補わない」に反します。
- ルール抽出を、同じ本文に何度も計算し直しています。
  - `extract_llm_prepare.py:90,330`
  - `extract_llm_engine.py:75,96,269`
  - `extract_llm_proofs.py:247,376,815,890,1028` ほか
  - `event_patterns` も、呼出しのたびに compile しています（`rules/extract.py:210-218`）。
- 出力が上限で切れると、その呼出しを捨てて分け直します（`extract_llm_engine.py:321-324,483-496`）。
- 前置フィルタの対象は、挨拶3種だけです（`extract_llm_source_contract.py:422`）。
- バッチ経路は、実測で正味マイナスだったため既定で無効です（`extract_llm_runner.py:281`、`_BATCH_K=0`：`extract_llm_source_contract.py:355`）。

### 意味解析（mcs/semantic）
- 再利用の鍵が、スレッド全体の `bundle_fingerprint` です（`semantic_store.py:45-70`。全員の本文と5つのプロンプトをまとめて hash しています）。
  - 返信が1件増えるだけで、既存の投稿の抽出・照合・薬剤詳細まで失効します。費用は投稿数の2乗で増えます。
  - チャンクのキャッシュにも同じ fp が入っています（`semantic_fact_stages.py:149-151`、`semantic_extraction.py:919`）。
- v2 の事実抽出は投稿日時を受け取らないのに、ISO 日付を求めています（`semantic_llm.py:71`、`semantic_extraction.py:57-58`）。
- 要約の修復で「JSON:」が二重になります（`semantic_llm.py:264-270`）。
- 要約のプロンプトは、対象を先に置き、スレッド文脈を後ろに置いています（`:133-160`）。
  - このため prefix cache が効きません。
  - 12000字を超えると要約が空になります（`:210-229`）。
- Jev の QC 質問に抽出側の判定（緊急度）を入れているので、確認が独立していません（`semantic_qc.py:218`）。
- `invalidate_projections` は、射影の全行を fetchall します（`semantic_publication.py:975-1024`）。

### 取得（mcs/ingest）
- 添付のダウンロードは attachment_id の古い順です（`ledger_capture.py:870-891`）。OCR は活動順に処理するので（`attachment_ocr.py:735-741`）、順番が合っていません。
- ルール抽出は、LIMIT なしで本文まで fetchall します（`rules/extract.py:426-438`）。
- `/projects` の応答に含まれる `last_message` を、未読の経路では捨てています（`mcs_payload.py:78-115`）。ただし、この値に返信や編集が反映されるかは、実機で確かめていません。
- `merge_full_replies` は、payload の返信がそろっていれば取得せずに retire します（`mcs/ingest/job_ops.py:362-367`）。DB の件数で取得を省く案は、返信の編集が thread の取得でしか届かないため採りません（Blk-A を参照）。

---

## 改版の方針

1. **公開版 `CURRENT_VERSION='1.0'` は据え置き（推奨）、内部世代だけを1回上げます。**
   - `LLM_EXTRACT_VERSION` 11→12（`inference_version.py:9`）
   - `RULE_VERSION` 29→30（`rules/extract.py:96`）
   - `PATIENT_CONTEXT_VERSION` 4→5（`extract_llm_source_contract.py:101`）

   仕様 `inference-engine-v1.md:28` との整合は、O1 で所有者に確認します。
2. **版を変えない安定性ブロックを、最初に単独で出します。**
3. **公開版を将来上げるための土台（R0）を、挙動を変えずに先に入れます。** `inference_version.py` を編集するので、配備時には fence によって再起動が必須になります。
4. **意味解析の再利用鍵を、投稿単位・段ごとに分けます。** 移行補助を付け、意味解析の層で Jev を判定し直す費用を0にします。抽出の QC（extract_qc）には、この効果は及びません。
5. **全件の再抽出は、既存計画 B3 の1回にまとめます。** その前にカナリア（G2）、後に完了ゲート（G3）を置きます。世代11の成果物は、G3 を通るまで残します。配備から S3 の作り直しまでは、`extract.patient_context.enabled=false` を保ちます。
6. **外した案と理由**
   - Design 2 の公開版 1.1 化、`clinical-record/2` による抽出器の一本化、provider の統合（M3）: 費用・依存・戻せない危険が最も大きいと審査されたため。
   - fence の判定を mtime から版の値に変える案: 安全ゲートを緩める方向になるため。
   - 古い root の観測保存（Design 1 A3）: 収集と既読の経路に触れるため。
   - `last_message` を使って GET を省く案: 実機での意味の確認（O4）が済むまで保留します。
   - `merge_full_replies` で DB の件数を見て取得を省く案: 編集が届かなくなる危険があり、収集の経路（MCS の負荷規則）にも触れるため保留します。
   - `_REPAIR_HEAD` をクラウドの cache_heads に加える案: 短すぎてキャッシュされないため（`cloud_inference.py:342`）。
   - バッチ経路を効率化策として使う案: 既定で無効で、実測では正味マイナスだったため。

---

## 取得項目の追加（LLM_EXTRACT_VERSION=12、RULE_VERSION=30）

すべての項目で原文の引用を必須にし、値は正規化しません。原文と照合できない値は落とします。

| 項目 | 内容 | 取り方 | 接続先 |
|---|---|---|---|
| meds | `form`（OD錠・散・貼付など）、`timing`（朝食後・就寝前）、`days`／`period`（原文のまま） | LLM。ルールは剤形語で補助（`extract_schema.py:161`） | `_rx_period_expiry/_rx_period_lapsed`（`mcs_signals.py:269-330`）に統合する。今の根拠は v1 の `med_periods` だけ |
| 測定値の日時と条件 | `vital_records` は新設しない。labs が持つ `measured_on`・`condition`・`subject`・`status`（`extract_schema.py:100-111`、`extract_validation.py:950-957`、プロンプト :29）の形に vitals を寄せる | LLM（ルールの vital_mentions を拡張） | `rollup_builder.py:768-794` の推定し直しを置き換える。紐付けは `grounded_vitals`（`clinical_values.py:665`）で検証する。並行する構造を作らないので、二重集計の危険も下がる |
| event_records の最上位 | `date_text`、`actor` | LLM | 予定欄と follow-up。照合は `clinical_events.py:686` と共通にする |
| 栄養・排泄（A12） | `intake_amount`（〇割）、`fluid_volume`（mL）、`last_bowel` | ルールが主体 | 最上位に新しい欄を作らない。既存の patient_context カテゴリ nutrition・excretion と、detail の food_texture（`patient_context.py:121,135`）に載せる。脱水などの判定はしない |
| 検査名（A15） | `_LAB_NAMES` に WBC・Plt・TP・T-Bil・Ca・Mg・NH3・PT・TSH・薬物濃度などを加える（`clinical_values.py:252,278`） | 既存の labs の検証 | 単位も拡張が必須（下記）。名前の正本 `docs/roadmap/extraction.md`（`clinical_values.py:250`）も同時に更新する。単位が合わない値は candidate |
| 既存 detail の接続 | `missed_doses`・`residual_quantity`・`support_method` | 新しい抽出は不要 | `_adherence_concern`（`mcs_signals.py:515-606`）。断定しない文言の「候補」として出す |

- **A15 の単位の拡張。** `_LAB_UNITS`／`_LAB_ITEM_UNITS`（`clinical_values.py:270-280`）に、/μL・万/μL・μg/dL・秒・μIU/mL・ng/mL などを足します。足さないと、新しい検査名の値がすべて candidate に落ちます。略称が単位や一般語とぶつかる組（Mg と mg、Ca、TP）は、境界の条件と forbid ケースで検証します。
- **次の世代に回すもの。** A5 アレルギー（allergen/reaction）と A13 ACP は、**extract/llm の範囲でだけ**回します。誤って断定したときの危険が大きいためです（共有カードに出さない方針は `structured_view.py:561`）。意味解析 v2 はすでに `allergy_intolerance` と `acp_discussion` を抽出しています（`semantic_llm.py:62,83`）。device_setting・rescue_count・administration_modification の範囲は O2 で決めます。
- **語彙の正本化。** schema はすでに `CATEGORIES/DETAIL_KEYS` から生成されています（`extract_schema.py:5,116,122`）。手書きで残っている次の2か所を、`patient_context.py:12-122` から生成する形に揃えます。
  - プロンプト `extract_llm_prompts.py:29,31`
  - `extract/eval/pattern_cards.py:130-142`

  v2 プロンプトに欠けている7カテゴリと8キーも、同じ正本から補います。
- **バッチ経路。** 既定では無効ですが、`SCHEMA_BATCH`・`_BATCH_HEAD`・`_BATCH_FMT`（`extract_llm_prompts.py:180-186`）も同じ世代12で揃えます。直さないと、有効にしたときに契約が食い違います。

---

## プロンプトの改善

**抽出（世代12に含めます）**
- 例を6,000字以下に圧縮します。
  - 例3の推測を削除します。
  - 例8・10の三重引用を短くします。
  - event_records、labs 形式の vitals、refers_to の完全合成の例を足します。
- `events` は出力させず、event_records から作ります（既存の `extract_validation.py:968-974`）。
  - 手がかり語からレコードを作る代替経路（`clinical_events.py:858-871`）が実質的に使われなくなります。その影響もテストで確かめます。
- `span` は、プロンプトと schema の要求から外します。値は `locate_quote_span`（定義は `mcs/core/mcs_util.py:530`）で補います。
- `reply` と detail の5キーは、どれも下流で使われています（表示の見出しと評価の採点）。O7 で表示の有無を確かめてから決めます。削除するなら、表示と `extract_bench.py:284-393` を同じ版で直します。
- ルール候補ブロック（`extract_llm_prompts.py:354-369`、上限3500字）から、evidence 系の項目を外します。
- 修復プロンプトに schema の要約を入れます。キャッシュの対象には加えません。
- max_tokens と分割は、呼出しの前に決めます（`_OUTPUT_BUDGET=850`、`clinical_chunking.plan_chunks`）。こうして、出力が上限で切れて呼出しを捨てる回数を減らします。
- ルール候補を (message_id, content_hash, RULE_VERSION) の組でメモ化し、1回の実行の中で共有します。`event_patterns` は、モジュールの読込み時に1回だけ compile します。
- 前置フィルタを広げます。
  - ルール候補が空で、短いお礼・了解・挨拶だけの投稿は、LLM を呼びません。
  - ただし、スレッドの文脈から返信かどうかを判定する必要がある投稿は除きます。
  - 除外した成果物は extract_qc の対象外です（`semantic_qc.py:120-121` の `prefilter IS NULL`）。そのため Jev の要求も減ります。
  - 見落とし（偽陰性）は評価資産で測ります。
- 世代11は、世代番号ではなく `prompt_head` と `schema` の組で `extract_llm_historical_contracts.json` に登録します（`extract_llm_historical.py:22-27`）。

**契約を変えない改善（すぐ入れられます）**
- 患者コンテキストの走査と選択の固定先頭（instruction→scan_schema）が、キャッシュに足る長さかを確かめます。足りていれば cache_heads に加えます。抽出の `_REPAIR_HEAD` は対象外です。

**意味解析（段ごとの鍵を入れた後に入れます。失効するのは該当する段だけです）**
- v2 抽出に投稿日時を渡します。`event_time` は「原文の表現」と「解決できたときだけ ISO」の組にします。抽出 A1 の日付規則（`extract_llm_prompts.py:121,175-178`）は、共通の断片にします。
- 要約のプロンプトは、共通の文脈を先頭に、対象を末尾に置きます。文脈は前後 k 件か、抽出済みの事実に縮めます。
- 「JSON:」の二重化は、B4 と同じく `rsplit` で直します（`semantic_extraction.py:1267` と同じ方式）。C2（`_FORMAT_REPAIR_SUFFIX`）も指紋に含めます。
- 誤記「0起き」を「0始まり」に直します（`semantic_llm.py:141`）。
- Jev の QC 質問から抽出側の判定を外し、P 系の label_ja を日本語にします。この変更で Jev の判定が全件失効するので、作り直しと同じ回にまとめるかを O8 で決めます。
- Jev の要求をまとめます。
  - 同じ state を使う問いを、1つの要求にします（`semantic_policy.py:71,144`）。
  - ローカル LLM の `category_presence` と重複している preflight（`semantic_extraction.py:849-872`）を省きます。

**コードによる誤断定検査（Design 3。LLM の費用はかかりません。`extract_validation` に置きます）**
- negated=true なのに、evidence に否定の手がかり語がない。
- 引用に本人以外を示す語があるのに、subject が本人になっている。
- 投稿日時がないのに、ISO 日付が入っている。
- 「なし」と書いた detail に、原文の否定表現がない。

これらに当たった値は candidate に落とし、表示は断定しない文言にします。手がかり語を増やすと誤検知も増えます。そのため、Design 3 への審査の指摘どおり、評価資産の forbid ケースで偽陽性も測ります。

---

## 安定性・メモリーリーク対策（確認済みの不具合ごと。どれも版の契約は変えません）

範囲の注意があります。患者コンテキストで古い走査行が溜まる問題とメモリの問題は、既存の M4 と P0-5（`PATIENT_CONTEXT_STABILITY_REVIEW_20261115.md:50-63`）の範囲なので、ここには含めません。項目4〜6はレーン3の担当（P0-6、ROUTING レビュー :60）と重なるので、着手前に担当を調整します。

1. **`semantic_due_selection.py:682-775`（`_cloud_selection_page`）**
   - 不具合:
     - 走査の SQL に `kind` と `state` の条件がなく、done/failed の semantic・QC 行まで評価しています。
     - 一番の負荷は、行ごとに走る `latest_activity_sql` の相関副問合せです。
     - heap が持つのは (順位, job_id) の組だけですが、`_cloud_selection_progress` がモジュールの global として周回をまたいで残ります（`:684-689`）。
     - upper が固定されるため、消化し切るまで新着が選ばれません。
     - `NOT INDEXED`（`:691-698`）は、PK の範囲走査を意図したものです。
   - 修正:
     - SQL に `state='pending' AND kind IN(...)` の条件を足すことに絞ります。
     - 毎回 `job_id>upper` の新着分を追加で走査します。
     - ON CONFLICT で done→pending に戻った行（`ledger.py:776-777`）は、TTL での作り直しで拾います。
     - P0-6 の route と重なり得るので、担当を先に決めます。
2. **`adapters/common/worker.py:261-280`（journal の horizon）**
   - 不具合:
     - 基準が `backups/*.db` すべての最古 mtime です。
     - 自動では削除しない `patient-context-reset-*.db`（`local_backup.py:346-357`）、schema_bump の preupdate、manual のコピーが1つでもあると、圧縮（`:300-311`）が二度と成立しません。
     - ScanCache（`journal.py:320-380`）と `_spent_deliveries`（`worker.py:1260-1266`）も、それに比例して増えます。
   - 修正:
     - 基準を `local_backup._backup_class`/`generations()` と共通の判定にし、上限（O5）を設けます。
     - reset のコピーは復元に使えるので（`docs/guides/ANTHROPIC.md:279`）、単純には除外しません。
     - 上限より古いバックアップから restore するときは、配信状態を照合不能として扱い、全件 hold して人が照合します。
3. **`extract_llm_resident.py:471-476`（lane 1 の昼間分岐）**
   - 不具合: `_settle_carry` が try（`:477`）の外にあります。sqlite の 'database is locked'（busy_timeout は30秒）で常駐 worker が終了し、メモリ上の carry も失われます。
   - 修正: この分岐を、既存の try（`:600-609`）の中へ移します。`_write_lock` は送出せず、RunLockLost は BaseException の派生なので、RunLockLost 用の処理は要りません。
4. **`cloud_backlog.py:764`（context worker）**
   - 不具合: `load_config()` の `ProviderSettingsUnreadable` を捕捉していません。設定の commit 中などに子プロセスが落ち、親が次に周回するまで最長で約15分止まります。
   - 修正: `:428` の `wait()` を両 worker 共通の補助関数にし、context worker も使うようにします。
5. **`cloud_backlog.py:271-275`（helper の解決）**
   - 不具合:
     - 子の起動が要らないときも、`helper_path(require_installed=True)` が LogInstallError や OSError を出します。
     - `last_sync` はすでに `helper_path` より前で更新されています（`cloud_backlog.py:228-230`）。
     - それでも resident は `pause(30)` の後、10秒の間隔を過ぎた状態で再び送出し、毎周 `run_pending` を飛ばします（`extract_llm_resident.py:497-498,600-609`）。そのため lane 2 の抽出と意味解析が、無期限に0件になります。
   - 修正:
     - helper は `target>len(children)` のときだけ解決します。
     - resident 側で sync の失敗を分けて捕捉し、`run_pending` まで処理を進めます。
     - 失敗の記録は1回にまとめます（`mcs_standalone/runtime.py:704-719` と同じ扱い）。
6. **`cloud_backlog.py:182-205` と `extract_llm_resident.py:101-120`（空きメモリの判定。優先度は低）**
   - 不具合:
     - 親と子の判定が食い違うのは Linux だけです。親は「総量の10%」、子は「max(1GiB, 10%)」で判定し、総量が約7.7GiB 未満のホストでは子が起動しては即終了します。
     - darwin（本番の Mac）では、親も子も10%です（`cloud_backlog.py:193-202`、`extract_llm_resident.py:103-110`）。
     - 子側の判定 `_context_memory_available` を使うのは、context の worker だけです（`cloud_backlog.py:765-766`）。
   - 修正: standalone の Linux に限った問題として扱います。判定を1つの関数にまとめ、起動直後に終了した子には再起動のバックオフをかけます。
7. **`adapters/common/worker.py:749-751`（resume の blocked_group）**
   - 不具合: 3人の検証者のうち2人が成立、1人が「条件は狭く、負荷は件数に依存しない」と判断しました。attachment に remote がないまま確定した後に止まった delivery は、2秒ごとに journal の全行を走査し続けます。
   - 修正: `_drive_parts` の終端で、これ以上進めない blocked 状態を最終状態として記録します。未確認の送信は再送しません（「Legacy absence never proves a group was not sent」は維持します）。
8. **`adapters/common/journal.py:186` と `worker.py:368-374`（途中で切れた末尾行）**
   - 判断: 意図された fail-closed かどうかで、検証者の判断が分かれました。fail-closed は維持します。
   - 修正:
     - `mcs_setup check` で、切れた行を含むファイルを表示します。
     - 隔離は `--confirm-human`+`reason`+receipt の人承認経路だけで行い、切れた attempt は unknown として settle します。
     - 「末尾の切れ」と「途中の破損」を区別する案は、started の証跡を失う危険があるので採りません。
     - 手作業の復旧手順（`hermes_plugin/README.md:278-291`）に、LINE WORKS の state の場所を足します。
9. **`llm/admission/llm_admission.py:395-423`（open_epoch）**
   - 不具合: 本番コードに open_epoch の呼出元がありません。admission を有効にすると（既定はオフで opt-in）、epoch_closed のまま全推論が保留されます。setup check は警告を出すだけです（`mcs/ops/diagnostics/environment_diagnostics.py:324-340`）。
   - 修正: 受付を開ける責任者が実装されるまで、admission=true を setup check のエラーにします（O6）。
10. **`adapters/lineworks/server.py:116-130`（一時ファイル）**
    - 不具合: `.atomic-*.tmp` が期限切れの削除対象に入っていないため、強制停止のたびに受信本文が平文で残ります。
    - 修正: LINE WORKS の expire の同じロック区間で、mtime が1200秒を超えた `.atomic-*.tmp` を、中身を読まずに unlink します。
    - `mcs/core/maintenance.py:295` の接頭辞 `.atomic.` は、mcs 側の `safe_io.ATOMIC_TMP_PREFIX`（`mcs/core/safe_io.py:55`）なので置き換えません。maintenance が見るのは `cmd_results`・`*_render`・`*_state` だけです（`maintenance.py:308-309`）。adapters の state をそちらで掃除する場合も、`.atomic-` を**追加**するだけにします。
11. **`_CALL_NOTES` と carry（測定してから判断）**
    - notes は呼出しごとに clear されています（`extract_llm_engine.py:147-148`、`extract_llm_runner.py:129-130`）。`provider_response` が持つのは最後の1件だけです。carry は lease の期限で release されます（`extract_llm_carry.py:52-84`）。
    - リークの根拠はありません。常駐 worker の RSS を長時間測り、増え続ける場合にだけ扱います。
12. **一時的なピークメモリ**
    - 修正:
      - ルール抽出の fetchall（`rules/extract.py:426-438`）を keyset 方式にします（`_CHUNK=200` に合わせます）。RULE_VERSION 30 の全件再実行より前に入れます。
      - `invalidate_projections` をページ分割します。
      - mcs_signals の fetchall を、`json_extract` や fetchmany に置き換えます。

---

## 新しい契約と版

| 対象 | 変更 | 備考 |
|---|---|---|
| `CURRENT_VERSION` | **1.0 のまま**（推奨。O1 で確認） | 仕様 `docs/specs/inference-engine-v1.md:28` は意味の変更での更新を求めている。1.0 は受入前（`inference_version.py:6`）なので、その中で改める解釈か、仕様の改定が要る |
| R0 | `inference_version.py` に `KNOWN_PREVIOUS=('0.1',)` と `classify(meta)→current/previous/legacy/unknown` を置き、上に挙げた直書き（`semantic_qc.py` の SQL を含む）を集約する。`_adopt_pending_arrival`/`_class_scope` を一般化する | 今の値では挙動は変わらない。ただし mtime の fence で全 writer が止まるので、配備時の再起動は必須 |
| `LLM_EXTRACT_VERSION` | 11→12 | schema・指示文・例・前置フィルタ・バッチ経路を1回にまとめる。世代11は `prompt_head`+`schema` の組で historical に登録する。`tests/core/test_inference_version.py:74` の固定値も、意図した変更として直す |
| `RULE_VERSION` | 29→30 | A12、DETAIL_LABELS の見出し語、ルールの補助、A15 の単位 |
| `PATIENT_CONTEXT_VERSION` | 4→5（`extract_llm_source_contract.py:101`） | 文脈の hash（`extract_llm_context.py:198`）と QC の hash（`extract_llm_qc.py:55`）に入るため上げる。P0-2a の scan 契約の分割に載せる |
| 意味解析の鍵 | 抽出・照合・薬剤詳細は、投稿単位の `assembly.source_fingerprint`（`semantic_assembly.py:17-27`）と段ごとのプロンプト hash にする。要約と公開だけはスレッド全体の鍵 | 移行補助: 保存済みの旧 bundle fp が再計算値と**完全一致**する成果物は、新しい鍵を記録するだけにして Jev を呼ばない。それ以外は計算し直す |
| Jev `REGISTRY_VERSION` | 命題一覧の内容 hash を併記する | 今は日付を手で更新しているだけ（`semantic_jev.py:68`） |
| 添付の契約 `attachment_extract_v1` | `EXTRACT_VERSION` と `PATIENT_CONTEXT_VERSION` を含む（`mcs/extract/attachment_extraction.py:30-34`） | P0-2a の切り離しが先に済めば、添付の推論は失効しない。済まなければ全件失効する |

---

## 実装ブロック（順序・依存・既存計画との関係）

1. **Blk-S（安定性。上の1〜10と12）**
   - すぐ着手でき、P0 と並行して進められます。
   - ファイルの所有範囲は分けます。semantic_due_selection.py は P0-6 と調整し、項目4〜6はレーン3と調整します。M4 と P0-5 とは重複しません。
   - changes/*.json を作ります。
2. **Blk-R0（版の土台）**: Blk-S と並行して進められます。配備には再起動が必須です。
3. **Blk-A（取得。版は変えない）**
   - 添付を活動順に並べます（`ledger_capture.py:870-891`）。
   - keyset 化を入れます。
   - 優先度の信号を集める関数は、P0 の「選択の重み付け」の上に載せます。
   - `last_message` は O4 の確認後に入れます。`merge_full_replies` の取得省略は保留です（収集経路に触れるため）。
4. **Blk-EVAL**
   - 評価資産（evaluation/、`extract/eval/extract_bench.py`）で、改良前の基準値を固定します。
   - forbid ケース（否定・家族・予定と実施・日付の推測・注入・Mg/mg のような略称の衝突）と、`false_assertion` 指標を足します。
   - 1件あたりのトークン数と呼出し回数も出します。
   - 合成ベンチは回帰ゲートとして使い、昇格の根拠にはしません。
5. **P0-1〜P0-5**（既存の計画）。特に P0-2a の添付抽出の契約の切り離しを、Blk-X の配備より前に終えることを依存として扱います。
6. **Blk-I（意味解析の段ごとの鍵と移行補助）**: P0-2b の後に入れます。shadow の二重抽出（`semantic_fact_stages.py:58-80`）は、評価期間が終わったら止めます。
7. **Blk-X（抽出12・ルール30・文脈5・意味解析プロンプト・誤断定検査）**
   - 1つの候補にまとめます。
   - 評価資産での比較をゲート G1 にします。
   - 表示の文言を変える場合（未取得の表示、候補の文言）は、Slack 画面例の SVG と PNG を再生成し、`generate_slack_gallery.py --check` を通します。
8. **S2 と P0-6、v2 の配備**（既存の計画）。（2026-11-16 追記: [残り計画の組み直し](REMAINING_PLAN_REVIEW_20261116.md)は、全体像 v2（B4b）を作り直しの後へ回す案とした。所有者の確認は未記録。変わるのは要約契約だけで、走査はやり直さず、要約の作り直し1回分の費用がかかる）
9. **カナリア G2**: `inference_reprocess` の scope の上限で数人だけ再抽出し、取得率・引用の照合・トークン数・extract_qc の Jev 要求数を確かめます。
10. **B3 の全件再抽出（1回）→ 完了ゲート G3**: 全投稿が世代12か、取得失敗を明示した状態であることを確かめます。配備から S3 までは `extract.patient_context.enabled=false` を保ちます（P0 計画「コミットの順序」末尾の配備の注意と同じ扱い）。
11. **S3 で消去し、作り直しを1回だけ行います**（人の承認あり）。

P0 計画は「作り直しは P0 と v2 の配備の後に1回」（P0 計画末尾）としています。抽出12と B3 をその前に挟むと、作り直しが遅れます。順序は O9 で所有者に確認します。

実機作業（9〜11、配備、再起動）は、それぞれ所有者が明示した承認の範囲だけで行います。各段は `update_restore` で戻せるようにし、世代11の成果物は G3 を通過するまで削除しません。

---

## 版上げの費用と移行

- **抽出**
  - 世代12にすると、全件が pending になります（`extract_llm_selection.py:65`）。
  - 所要時間は「件数 ×（1件あたりの呼出し回数）× 75秒（`extract_llm_source_contract.py:359`）÷ 背景枠（夜3・昼2）」で、配備前に見積もります。
  - B3 と同じ1回の実行なので、追加の再抽出はありません。
  - 削減効果は、評価資産で実測するまで数値を書きません。
- **抽出の品質確認（extract_qc）**
  - QC のジョブは、成果物の `source_artifact_id` と `ver` に結び付いています（`mcs/semantic/semantic_qc.py:93-160`）。成果物が新しくなるたびに pending に戻ります。
  - QC が完了とみなされるのは、`jev_requests>0` のときだけです（`mcs/notify/processing_route.py:72-80`）。
  - したがって B3 の件数分の Jev 要求が、上限の10,000要求/日（`docs/specs/inference-engine-v1.md:158`）に掛かります。日数を事前に見積もります。
  - 前置フィルタで除外した成果物は QC の対象外なので（`semantic_qc.py:120-121`）、除外の分だけ要求が減ります。
- **添付の推論**
  - `attachment_extract_v1` が `EXTRACT_VERSION` と `PATIENT_CONTEXT_VERSION` を含むので（`attachment_extraction.py:30-34`）、P0-2a の切り離しが済んでいなければ、添付 OCR の推論（クラウドの reextract 経路）も全件失効します。
  - 切り離しの前に配備する場合は、費用と所要時間を見積りに加えます。
- **患者コンテキスト**
  - 契約にルール・LLM・文脈の各世代が入っています（`mcs/patients/patient_context_refresh_state.py:243-245`）。そのため配備した時点で、全患者の再走査が自動で始まります。
  - 配備から S3 まで `extract.patient_context.enabled=false` を保ち、作り直しの1回分だけ費用を払うようにします。
  - 抽出12より前に作り直すと、費用を2回払うことになります。
- **意味解析と Jev**
  - 移行補助があれば、段ごとの鍵への移行で、意味解析の層の Jev 再判定は発生しません。
  - プロンプトの変更で失効するのは、該当する段だけです。
  - 処理は日次・月次の予算の範囲で進みます。
  - O8 の変更を入れる場合は、cloud-llm plan/estimate で事前に概算します。
- **表示（確認済み）**
  - 公開版を据え置くので、「解析待ち」への切替えは起きません。
  - 集約（rollup）と測定値は、世代11を読み続けます。`current_extract_pred` は `extract_version` で絞っていないためです（`mcs/core/mcs_queries.py:270-277`）。
  - 一方で、B3 が終わるまで次の3か所が劣化します。
    - 薬の時系列は、世代が違うと `None` を返します（`mcs/medications/drug_timeline.py:24-27`）。
    - 処理経路の QC 状態は `unknown` になります（`processing_route.py:56`）。
    - 画面の QC は current=false になります（`mcs/views/mcs_view.py:394`）。
  - 新しい項目の「未取得」表示は、既存の計画 M1（`PATIENT_CONTEXT_STABILITY_REVIEW_20261115.md:26-31`）に従います。
- **配備**
  - fence が mtime で判定するため、R0 を含むすべての段で、gateway・LINE WORKS・standalone の再起動が必須です。手順書に明記します。
  - `deployment/` の編集は、実機には反映されません。

---

## テスト（すべて完全合成。一時 DB とスタブだけを使う）

- **R0**
  - 0.1・1.0・仮の1.1 の payload の分類、引継ぎ、unknown の拒否。
  - `KNOWN_PREVIOUS` が現在の値のとき、今の挙動と同じであること。
  - `tests/core/test_inference_version.py:74`（`LLM_EXTRACT_VERSION == 11`）を、意図した変更として12に直す。
- **Blk-S**
  - kind・state の条件で、done 行の評価と相関副問合せが走らないこと。upper より後の新着が選ばれること。
  - lane 1 の `_release` が 'database is locked' を出しても、ループが続くこと。
  - context worker が、設定を読めない状態を経ても続くこと。
  - helper がないときに sync が失敗しても、`run_pending` が実行されること。
  - 空きメモリの判定が、親と子で一致すること（Linux）。
  - blocked_group の resume が一度で終わること。
  - `.atomic-*.tmp` が期限で消え、mcs の `.atomic.` の掃除も変わらないこと。
  - admission=true が setup check でエラーになること。
  - horizon の上限を超えた restore で、全件 hold になること。
- **抽出**
  - プロンプト・DETAIL_KEYS・pattern_cards の一覧が、同じ生成元から作られること。
  - span と events をコードで補えること。`events` だけが返ったときの代替経路（`clinical_events.py:858-871`）が回帰しないこと。
  - labs 形式の vitals が、二重に集計されないこと。
  - 新しい検査名と単位が通ること。Mg/mg、Ca、TP の誤検出が起きないこと。
  - バッチ経路の schema が、単発の経路と一致すること。
  - 捏造した引用が落ちること。
  - 誤断定検査に当たった値が、candidate に落ちること。
  - 前置フィルタが、文脈のある挨拶を除外しないこと。除外した成果物が extract_qc の対象外になること。
  - ルール候補のメモ化で、1回の実行の中の計算が1回だけになること。
  - 未取得の項目が「なし」と表示されないこと（M1 と整合すること）。
- **意味解析**
  - 返信を1件足しても、既存の投稿の抽出キャッシュがヒットすること。
  - 旧 fp が完全一致するとき、Jev のスタブが一度も呼ばれないこと。
  - 投稿日時がないときは、text だけになること。
- **既存の golden**（`tests/patients/test_patient_context_golden*.py`）に、意図した差分以外の変化がないこと。
- **手続き**: changes/*.json を作ること、`update_readme.py` を実行すること。画面の文言を変えた場合は `generate_slack_gallery.py --check` を通すこと。

---

## リスク

- 前置フィルタで、短い臨床情報を落とすおそれがあります。条件をルール候補が空のときに限り、評価で偽陰性を測ります。
- schema が大きくなると、出力の長さ切れが増えるおそれがあります。出力の削減（events・span）と同じ版で相殺し、max_tokens は事前に決めます。
- vitals を labs の形に寄せるときに、移行期間だけ二重に集計するおそれがあります。rollup の読み元を1つに決めます。
- 移行補助に誤りがあると、古い評価が通ってしまいます。fp が完全一致する場合に限り、それ以外は計算し直します。
- B3 の後に契約の不足が見つかっても、2回目の再処理はできません。G1 と G2 を必ず通します。
- B3 が終わるまで、薬の時系列・処理経路の QC 状態・画面の QC が劣化します（`drug_timeline.py:24-27`、`processing_route.py:56`、`mcs_view.py:394`）。期間を短くし、利用者に事前に知らせます。
- extract_qc の Jev 要求と添付の再推論で、費用と日数が見積りを超えるおそれがあります。G2 で実測してから B3 に進みます。
- 配備から S3 までの間に `extract.patient_context.enabled` を戻し忘れると、患者コンテキストの費用を二重に払います。
- 安定性の修正は、通知の安全ゲートに触れます。fail-closed は崩さず、人承認の経路だけを足します。
- 合成ベンチに合格しても、実データでの品質は保証されません。

---

## 所有者の決定（推奨に◎）

- **O1 公開版**
  - ◎1.0 のまま、内部世代だけを上げる。あわせて、仕様 `inference-engine-v1.md:28` を「受入前の 1.0 の中で改める」と解釈するか、仕様を改定するかを決める。
  - 1.1 に上げる（R0 が前提。Jev を含む全段の再処理を、作り直しと同時に行う）。
  - 依頼文の「v1.0 のバージョンアップ」とずれるので、明示の確認をお願いします。
- **O2 取得項目の範囲**
  - ◎meds の form/timing/period、labs 形式の vitals、event_records の date_text/actor、A12（既存カテゴリに載せる）、A15（単位を含む）、既存 detail の接続。
  - extract/llm に A5 アレルギーと A13 ACP も含める。
- **O3 世代11の成果物の保持**
  - ◎G3 を通過した後も、一定期間残す。
  - すぐ消す。
- **O4 `last_message` の実機確認**
  - ◎読取りだけの確認を承認し、値の意味を確かめてから採用する。
  - 見送る。
- **O5 journal の horizon**
  - ◎日次の範囲と上限日数で打ち切る。それより古いバックアップからの restore は、全件 hold して人が照合する。
  - 今のまま。
- **O6 admission**
  - ◎呼出元ができるまで、admission=true を setup check のエラーにする。
  - open_epoch の担当を実装する。
- **O7 `reply` と detail の5キー**
  - ◎表示と評価での使われ方を確かめてから決める（表示の見出しとベンチで使われている）。
  - 削除し、表示とベンチを同じ版で直す。
  - 残す。
- **O8 Jev が失効する変更（QC の独立化・label_ja）**
  - ◎作り直しと同じ回にまとめる。
  - 見送る。
- **O9 B3 と作り直しの順序**
  - ◎抽出12と B3 を作り直しの前に挟み、作り直しを1回に保つ（作り直しは遅れる）。
  - P0 計画どおり先に作り直し、抽出12は後に回す（患者コンテキストの費用が2回かかる）。

---

## 文書とコードの食い違い（コードを正とします）

1. `inference_version.py:6` は 1.0 を「開発目標のみ」としていますが、実際には書込みと一致判定に使われています。`ACCEPTANCE_1.1.1.md:17` は「公開版1.0」としています。仕様 :3 と `changes/archive/1.1.0/inference-engine-v1.json` は「受入前」「開発候補」です。
2. 仕様 `inference-engine-v1.md:21-23` は RULE_VERSION を推論版から独立としています。しかしルール抽出も、推論版で書込み・失効します（`rules/extract.py:401,566-592`）。
3. 仕様 :118 の「改版前の job の通知資格を保持する」は、0.1→1.0 のときしか実装されていません（`semantic_due_selection.py:262-`）。
4. fence が `inference_version.py` の mtime の変化で止まることが、文書に書かれていません（`extract_llm_source_contract.py:256,274`）。
5. `PATIENT_CONTEXT_P0_PLAN_20261115.md:128` は `PATIENT_CONTEXT_VERSION` の定義を `extract_llm_source_contract.py:40` としていますが、実際は :101 です。`docs/specs/patient-context.md:27` は正しく書かれています（`extract_llm.PATIENT_CONTEXT_VERSION` は再公開名。`extract_llm.py:161`）。
6. CLAUDE.md は「v2/v3 は git 履歴にだけある」としていますが、`mcs/extract/v2/README.md` と `v3/README.md` が実在します。docstring にも旧世代名が残っています（`extract_llm_source_contract.py:406-412`、`extract_llm_prepare.py:124-128`、`extract_llm.py:20-23` の「clinical-plan/v3」。実際は `extract_llm_context.py:143` で v4 を使っています）。
7. CLAUDE.md は stage_self_probe が常に動く前提ですが、実際は `self_posts` が true のときだけ動きます（`run_check.py:586-600`）。docstring は「全員」としていますが、実装は WALK_TICK_CAP=40人ずつです（`collection_stages.py:820,832-834`）。
8. `mcs_adapter.py:7` は unread の応答に `last_message` が含まれると書いていますが、未読の経路はその値を捨てています（`mcs_payload.py:78-115`）。
9. `semantic_publication.py:3,10` は「S0–S7 は semantic_drain が実行」としていますが、実際に実行するのは `semantic_job_processor.py` です。同じ docstring で、S0–S7 と S0–S8 の表記も混在しています。
10. `semantic_fact_stages.py:120-122` は「S0 はモデル呼出しなし」としていますが、チャンクの preflight で Jev を呼ぶことがあります（`semantic_extraction.py:969-977`）。
11. `semantic_llm.py:205-209` は12000字を1枠に収まる上限としていますが、同じ枠を使う患者コンテキストは18000字です（`patient_context_plan.py:14`）。前提がそろっていません。`:141` の「0起き」は誤記です。
12. `adapters/common/paths.py:65` の一時ファイルの接頭辞は `.atomic-` です。一方 `mcs/core/maintenance.py:261,295` が掃除するのは、mcs の `.atomic.`（`safe_io.py:55`）だけです。adapters の一時ファイルを掃除する経路が、どこにも文書化されていません。
13. `hermes_plugin/README.md:283` の journal 復旧手順に、LINE WORKS の state の場所がありません。
14. `changes/archive/1.1.0/admission-live-fence-20261030.json:5` は open_epoch の信頼できる呼出元を前提にしていますが、実装されていません。`deployment/launchagents/README.md:156` も、epoch を開ける方法を示していません。
15. `ledger_schema.py:339` は kind を `'reply' | 'history'` としていますが、実際には semantic・extract_qc・extract_claim などの kind も使われています。
16. `docs/roadmap/extraction.md:39` は旧ファイルの位置（`extract_llm.py:95`）を指していますが、実体は `extract_llm_prompts.py` にあります。
17. 患者コンテキストの修復プロンプト（D3）は、同じ文字列が2か所に重複しています（`patient_context_quality_controller.py:81-83` と `patient_context_refresh_readiness.py:81-83`）。receipt を文字列の完全一致で照合しているので、片方だけ直すと照合が壊れます。
18. `extract/eval/pattern_cards.py:139` のコメント「not yet runtime DETAIL_KEYS」は古くなっています。該当のキーは、すでに `patient_context.py:110-120` にあります。
19. CLAUDE.md は standalone の SDK ファイルを `adapters/{slack,discord}/standalone.py` と書いていますが、`ci/gates.py:37-57` の鍵は `mcs_discord/…`・`mcs_slack/…` です。これは `_plugin_path` による対応付け（`ci/gates.py:80-89`）によるもので、表記がそろっていないだけです（軽微）。
20. 設計案・審査・前の版の計画の記述の訂正
    - 4つの設計案はすべて `PATIENT_CONTEXT_VERSION` を :100 としていますが、正しくは :101 です。
    - 審査員の1人は、`semantic_qc.py:114` 以降の SQL の直書きを「確認できない」としていました。実際には `:114,132,141,155` に実在します。
    - prompts 報告の `locate_quote_span`（`patient_context_plan.py:9`）は import 文です。定義は `mcs/core/mcs_util.py:530` にあります。
    - 前の版の計画にあった次の主張は撤回しました。
      - 「`_repair_prompt` の docstring と cache_heads が食い違う」: docstring の prefix cache はローカルの slot のことで（`extract_llm_prompts.py:257-259`）、クラウドで対象外なのは意図した設計です（`cloud_inference.py:342`）。
      - 「reply と detail の5キーは未使用」: 表示の見出しとベンチで使われています。
      - 「`maintenance.py:295` の接頭辞を `.atomic-` に揃える」: `.atomic.` は mcs 側の正しい接頭辞です。
      - 「`patient-context.md:27` の世代番号が古い」: 正しく書かれていました。
    - Jev の上限の出典は、仕様の :165 ではなく :158 です。
    - 安定性項目9の診断ファイルは、`mcs/ops/diagnostics/environment_diagnostics.py` です。

## 所有者の決定（2026-11-15）

- **O1 公開版の扱い**: 公開版は 1.0 のまま据え置き、内部の世代を上げる。
  - 上げる世代: 抽出 11→12、ルール 29→30、文脈 4→5。
  - 仕様 `inference-engine-v1.md:28` は「受入前の 1.0 の中で改める」と解釈し、そのことを仕様に明記する。
  - 将来 1.1 に上げるときの土台（R0）は先に入れておく。
- **O2 取得項目**: 推奨した範囲に加え、投稿ごとの抽出でもアレルギー・副作用（A5）と ACP（A13）を取る。
  - 誤った断定を防ぐ検査（否定・家族・予定と実施・推測の区別）を強める。
  - 原文の引用を必須にする。
- **O9 順序**: 次の順に進める。作り直しは1回だけにする。
  1. 新しい抽出（世代12）を入れる。
  2. 過去投稿を全件、再抽出する（B3）。
  3. 完了ゲート（G3）を通す。
  4. S3 で消去し、作り直す。
- **O3・O5・O6・O8・O4**: すべて推奨どおり。
  - O3: 旧世代の成果物は、G3 を通った後も一定期間残す。
  - O5: journal は上限日数で打ち切る。それより古いバックアップから戻すときは、全件を保留して人が照合する。
  - O6: admission=true は、setup check でエラーにする。
  - O8: Jev が失効する変更は、作り直しと同じ回にまとめる。
  - O4: `last_message` は、実機で読み取って確認できるまで保留する。
- **O7**: 推奨どおり、表示と評価での使われ方を確かめてから決める。
