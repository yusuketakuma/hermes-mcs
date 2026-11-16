# 患者コンテキスト P0（作り直し前の土台）実装計画（2026-11-15）

[拡張の設計](PATIENT_CONTEXT_TIMELINE_DESIGN_20261115.md)の P0 を、ファイル単位に具体化したものです。読み取りだけの調査に基づいています。
P0 は、1回だけ行う消去と作り直しの前に必ず入れる変更です。後から入れると、全患者の資料ごとの調査（scan）をやり直すことになります。

## 結論

1. **契約の分割にはモジュールの移動が要る**
   - `patient_context_plan.py` には、scan 側と要約側の処理が同居しています。
   - `patient_context_projection.py` には、scan 側の `annotate` が同居しています（`annotate` の結果は scan 事実の `temporal` として保存されます）。
   - そこで、要約側を `patient_context_selection.py`（新）へ、`annotate` を `patient_context_temporal.py`（新）へ移してから、ファイルhashを scan 契約と要約契約に振り分けます。
2. **「要約（A）だけを作り直す」を実際に成立させるには、決定が2つ必要**
   - D1: 資料ごとの束縛を何に結び付けるか。
   - D2: 品質確認の識別値の範囲。
   - どちらも作り直しの後に変えると、全 scan または全品質確認が失効します。
3. **62日問題は、保守の削除処理に pin を1つ足せば直る**
   - 保守の削除処理（`maintenance._prune_cloud_usage`）に、scan chunk の `cloud_receipts` が参照する受領行を残す pin を足します。
   - 添付OCR用の `_attachment_cloud_pins` と同じ形です。
4. **v2 の文書形式は P0 では不要**
   - 分割の後なら、v2 は要約契約の変更だけで入れられます（A の作り直しだけで済みます）。
   - v2 が必要とする項目は、保存済みの scan 事実と台帳にすでにあります。

## 契約の分割

### scan 契約（`snapshot["contract"]`）

```
{"version": "patient-context-nightly/1", VERSION_KEY: CURRENT_VERSION,
 "chunks": PLAN_VERSION, "facts": 2, "context": PATIENT_CONTEXT_VERSION,
 "scan_code": sha256[plan.py（scan 部分）, patient_context_scan.py, patient_context_temporal.py],
 "model": ..., ["provider": ...]}
```

### 要約契約（`snapshot["summary_contract"]`、新設）

```
{"version": "patient-context-summary/1",
 "code": sha256[patient_context_selection.py, patient_context_projection.py, inference_dependency_loss.py]}
```

### 削除するキー

`context_checks`・`state_code`・`dependency_code`・`rule`・`llm` を削除します。
`rule` と `llm` は、資料ごとの `extraction_sig` で置き換えます（P0-2a では残し、P0-2b で削除しました）。

### 移動する処理

| 移動するもの | 移動先 |
|---|---|
| `select_facts`・`summary_*` | `patient_context_selection.py` |
| quality_controller の `_summary_input` | `patient_context_selection.py` |
| `lane_rotation` | `patient_context_schedule.py` |

### 署名への反映

- `source_sig` に要約契約を含めます。こうすると、A の同一判定・公開判定・生成枠のキーが要約契約に自動で結び付きます。
- `semantic_reprocess` に要約契約を加え、従来の dependency_code の被覆を保ちます。

### 一覧の更新

指紋と配置の一覧（`_LOADED_SOURCE_PATHS`・`semantic_candidate._MODULES`・HELPERS・`test_module_layout`）に、新しい2つのモジュールを足します。

## 根拠の選び方の重み付け

- 区分ごとの重みで巡回します: current 4、conflict 3、plan 2、unknown 2、history 1。
- 各区分の中では、臨床日（なければ投稿日）が新しい順に並べ、分類は交互に取ります。
- 他の区分の事実が尽きたら、history で残りを埋めます。
- これで、他の区分が残っている間の history の割合は 1/12 以下になります（現在は約1/5）。
- golden の `local_three_posts.json` は、並び順だけを意図して更新します。

## 項目単位の失効

- **保存**: 新しく作る公開版の meta に `item_binding` と `quality_identity` を保存します。
  - `item_binding`: 全体にかかわる部分の digest、資料ごとの署名、依存喪失の鍵ごとの digest
  - `quality_identity`: 品質確認の各識別値
- **読み取り**: `source_sig` が一致しない場合は、次のように扱います。
  - 全体にかかわる部分（範囲・契約・権限・保存状態）が変わっていれば、従来どおり全体を非表示にします。
  - そうでなければ、変わった資料の事実と、喪失のあった鍵（entity × axis）の事実だけを外します。
  - 要約は `prune_summary` で同じ方法で外します。`patient_context_view._evidence` の既存の絞り込みも、同じ関数に寄せます。
  - 外した後は、既存の barrier 付きの再 projection で区分を付け直します。
- **品質確認**: 再取得はしません。代わりに次のことを確かめます。
  - `audit_key` に対する PASS 行が1件ある。
  - `failure_key` に対する FAIL 行がない。
  - 回答が、選択した事実には supports、それ以外には omission_ok である。
  - 修復が入っている版と SKIPPED の版は、P0 では全体を非表示にします。
- **表示**: `coverage["rechecking"] = N` を出し、画面に「訂正があった記録 N件は再確認中」と表示します。0件のときは行ごと省略します。
  - アレルギー判定は、`rechecking` があれば失敗側に倒します。

## 62日問題

`_context_scan_cloud_pins` を新設し、次の受領行を残します。

- 対象は、現行の版（`current_meta_pred` と同じ判定）の scan chunk・scan・公開版の meta の `cloud_receipts` が参照する予約です。`invalidated` は見ません（改訂 5）。
- 要約修復の予約も残します。`quality_repair_ref` は予約を直接指さず、修復候補の行（`{artifact_id, digest}`）を指します。予約は、同じ `input_key` を持つ `patient_context_summary_quality_repair_v1` の meta（`reservation_artifact_id`・`reservation_id`）から取ります。現行の版の修復行は、公開版に `quality_repair_ref` が無くても残します（`_profiled_summary_repair_exists` が同じ予約を読むため）。
- その予約の reserve・result・disposition の各行を、`reservation_id` で追ってまとめて残します。同じ `reservation_id` を持つ別患者の行も、矛盾の証拠として残します。
- meta が不正な患者（JSON として読めない・重複キー・版の値が不正・`cloud_receipts` や `quality_repair_ref` の形が不正・`quality_repair_ref` から予約へ辿れない）は、その患者の reserve・result・disposition をすべて残します。
- `cloud_receipts` キーが無いクラウド由来の行は、指し先が無いので残しません。旧形式の行のほか、現行の版でも回答済みの予約が無い単位はこのキーを持ちません（`patient_context_refresh.py` は `answered_reservations` が空のとき書かない）。
  この行は62日後に受領行が消えると、`guard_context_origins` が履歴0件として `patient_context_cloud_source_unverified` で拒否します（`test_summary_without_receipt_refs_is_refused_after_retention` が固定）。患者単位で受領行を残すか、作り直しで解消するかは所有者の判断待ちです。
- 参照が残る限り、受領行は削除されません。置き換えられた古い chunk・過去の公開版も現行の版なら参照として数えるので、古い行の整理（P0-5）か消去をするまで受領行は増えます。

使用額と月次・期間の集計は日付の窓で絞って読むので、残した行は影響しません。
再送防止（`cloud_policy._verified_request_history`）は窓なしで読むので、残した行は同じ入力の判定に使われ続けます。
result だけが残ると `cloud_unit_unresolved` で送信が止まるため、予約単位で丸ごと残します。

再現テストは `tests/patients/test_context_receipt_retention.py` です。pin が無いと、次の3か所が受領行の欠落で拒否します。

- 要約の作り直し・更新: `guard_cloud_sources` → `guard_context_origins` → `reservation_evidence_batch` が `ValueError` を出し、`patient_context_cloud_not_sent`（`guard_error=ValueError`）で先送りになる。
- 公開版の再確認: `guard_context_origins`（unit `summary`）が `ValueError` を出す。
- 修復済みの要約の表示: `_summary_repair_publication` → `verify_cloud_repair_evidence` が通らず、`context_view_input` が None を返す。

## 所有者の決定（未決）

- **D1: 資料ごとの束縛を内容基準にするか**
  - 現状: `extraction_sig` は、チャット投稿では抽出結果の内容・モデル・meta の hash、添付では解析結果の id と hash に結び付いています。`derived_summary_sig` は補助要約の artifact の id と版に結び付いています。そのため、抽出や意味解析の世代を上げるたびに全資料の scan が順に失効し、クラウドの費用は作り直しと同じくらいかかります。
  - 推奨: 実際に渡した入力の内容（抽出の `patient_context` 配列と補助要約の本文）の hash に結び付けます。
- **D2: 品質確認の識別値の範囲**
  - 現状: `_quality_code()` はほぼ全コードを含みます。そのため、リリースのたびに全資料の品質確認が失効し、Jev の再確認が終わるまでクラウドの公開版が非表示になります。
  - 推奨: 識別値を次の形にし、品質確認の意味が変わるときだけ手動の版定数 `QA_VERSION` を上げます。
    - source-QA: `digest([scan 契約, QA_VERSION])`
    - 要約QA: `digest([scan 契約, 要約契約, QA_VERSION])`
  - 実装（P0-2c）: 走査契約と要約契約は既に識別値の入力（`source_input_key`・`input_key`）にあるため、`assessment.code` は `[QA_VERSION, 品質確認のコードだけのhash]` にしました（`patient_context_quality_identity.py`）。hash の範囲と手動で上げる条件は `docs/specs/patient-context.md`「品質確認の識別値（P0-2c）」にあります。

## コミットの順序

1. **P0-1 純粋な移動**
   - selection・temporal を新設し、import と各一覧を更新します。
   - 契約のキーは変えず、被覆は保ちます。golden は不変です。
2. **P0-2 契約の分割と D1・D2**
   - 既存の契約テストを改定し、契約の分割テストを新設します。
3. **P0-3 重み付けと 62日の pin**
   - golden の並び順を意図して更新します。
4. **P0-4 項目単位の失効**
   - item_binding、縮退読取り、表示行、テスト。

配備の前に `extract.patient_context.enabled=false` と `cloud-llm stop` を済ませます（消去の前提条件と同じです）。

## 文書とコードの食い違い

- 設計書は、契約に入るのを plan・projection・dependency_loss のhashだけとしています。しかし実際には、`patient_context_scan.py` は契約に入っておらず、品質確認は `_quality_code` を通じてほぼ全コードに結び付いていました（P0-2c で品質確認のコードだけに狭めました）。
- 設計書は projection を要約側として扱っていますが、scan 側の `annotate` を含んでいます。
- `PATIENT_CONTEXT_VERSION` は `extract_llm_source_contract.py:40` で定義され、`extract_llm.PATIENT_CONTEXT_VERSION` として参照されています。
- `CLOUD_SEND_FOUNDATION_DESIGN_20261115.md` §6 の maintenance の行番号がずれています（修復の pin は `_source_repair_cloud_pins`、添付OCRの pin は `_attachment_cloud_pins`、患者コンテキストの pin は `_context_scan_cloud_pins`。行番号は変わるので関数名で参照する）。

## 改訂（2026-11-15、計画レビュー・切替レビュー・安定性レビューと所有者の決定の反映）

### 所有者の決定

| 項目 | 決定 |
|---|---|
| D1 | 推奨どおり。scan への入力として実際に渡した内容と、それを作る処理（見出しルールの item 導出を含む）の関数単位 hash に結び付ける。`extraction_ready`・`canonical_context` は残す。 |
| D2 | 推奨どおり。安全ガードとして、`assessment.code = digest([QA_VERSION, qa_code])` にする。<br>qa_code は次の関数単位 hash（`semantic_candidate._code_bytes` の仕組みを使う）。<br>・`audit_patient_summary_chunk`<br>・`patient_source_quality_pages`<br>・`audit_patient_quality_page`<br>・選択肢の定数<br>・`JEV_MODEL`<br>・`_quality_expected`<br>・Jev の要求文 |
| 停止の扱い（切替レビュー a） | 理由で分ける。<br>・一時的な理由（送信枠・キーの一時的な読み取り失敗・回路・その日の上限）は先送りにする。<br>・確定的な理由（期間の終了・月の上限・所有者の停止・mode=off）はローカルで処理する。 |
| 切替の既定（b・c・e・f） | まとめて採用。<br>・送信後に結果不明になった資料は、ローカルで代替する。<br>・ローカルのモデルを変えても、クラウドの成果物は失効させない。<br>・作り直しの間は、患者ごとに完結させてから次へ進む。<br>・夜のローカルは2並列にする。 |
| 走査のモデル（d） | 完全合成の評価で、Haiku/Luna・小さい出力上限と比べてから決める。 |

### 計画の修正（レビューで見つかった漏れ）

1. **`temporal` を scan に保存しない**（計画レビュー M1）
   - `annotate` は、読取り時に要約側で計算する。fact id には `temporal` が含まれないので、作り直しの前なら形式変更の費用はかからない。
   - これで `patient_context_temporal.py` へ移す必要はなくなる。scan 契約は、引用の検証に必要な処理だけを覆う。
2. **契約から処理系の名前を外し、成果物ごとに出所を持たせる**（計画レビュー M2・切替 M5・U0）
   - 契約から、ローカルの model と provider の名前を外す。
   - 成果物ごとに `provenance {backend, model, profile}` を保存する。
   - 公開判定は、設定の「受け入れる出所」と照合する。
3. **添付抽出の契約から patient_context_* のモジュールを外す**（計画レビュー M3）
   - 配備の後、添付の抽出が落ち着くまでは、作り直しを始めない。
4. **D2 の識別値を使う箇所を全部変える**（計画レビュー M4）
   - `semantic_usage_receipts.py:634-637`
   - `_source_quality_hold_key`
   - `quality_schedule_key`
   - `_scans_from_read` の proof の鍵
5. **62日の pin の対象を広げる**（計画レビュー M5・Should-fix）
   - `quality_repair_ref` が参照する予約も対象に加える。
   - 対象は `current_meta_pred` を満たす行に限る。
   - reservation_id でも全体を追う。
   - 計画の「再送防止は日付の窓で読む」は誤り（窓なしで読む）なので記述を直し、テストを足す。
6. **項目単位の失効の条件を揃える**（計画レビュー M7・M8）
   - 縮退した読取りでは、`scan_sig`・`counts`・`_quality_capture`・`_summary_valid` を「変わっていない資料」に限って評価する。
   - 区分は、公開時の temporal_view と付け直し後のうち悪い方にする（繰り上げない）。
   - 保存済みの `assessment` が現行と一致することも必須にする。
   - 「訂正があった記録 N件」の N は、`original_source_signature`（本文・改訂・削除）が変わった資料だけを数える。
7. **単位の中の複数回送信と、送信後の不明**（切替 M1・決定 b）
   - ローカル代替の可否は、「この単位の有料回答が採用されたか」で決める。
   - 採用しなかった有料回答は、`superseded_local` として処分する。
   - これは S2 ②（要求記録）と一緒に入れる。
8. **安定性の修正を作り直しの前に入れる**（安定性レビュー）
   - M3: 同じ患者の並行作業の排他（`unit_lease_until`）。
   - M4: 古い SCAN/CHUNK の削除、`_scan_sig` の軽量化、`lane_metrics` の集計の改善。
   - S10: 未束縛の変数。
   - S1: 1患者の不正な行で全体が止まらないようにする。
   - 状態別の件数を doctor・🧾 に出す（M2・S4・S5）。
9. **作り直しが止まらない worker 構成**（切替 M4・U4）
   - 患者コンテキスト用の子プロセスが0本のときは、レーン3がローカル担当として処理する。
   - 夜は2並列にする。

### 改訂後の実装の順序

1. **P0-1**: 要約側を `patient_context_selection.py` へ移す（純粋な移動。被覆は保つ）。
2. **P0-2a**: 契約の分割と出所の保存（U0）。添付抽出の契約を切り離す。
   - P0-1 で `context_checks` が `patient_context_schedule.py` 全体を hash するようになり、レーン巡回だけでなく bookmark の予定組み `plan()` も一時的に被覆に入った。この分割で `plan()` を `context_checks` から外す。
3. **P0-2b**: D1（入力の内容で結び付ける）。
4. **P0-2c**: D2（qa_code と、識別値を使う全箇所）。
5. **P0-3a**: `select_facts` の重み付け（12枠の固定列にし、並べ替えの鍵の型を揃える）。
6. **P0-3b**: 62日の pin（`quality_repair_ref` を含む）。
7. **P0-4**: 項目単位の失効（上の 6 の条件を含む）。
8. **P0-5**: 安定性（排他・古い行の削除・集計の改善・未束縛の変数・状態別の件数の表示）。
9. **P0-6**: 送信先の判断の統一（理由の分類・route 関数・レーン3の担当・夜の2並列）。S2 ②③と合わせて入れる。

作り直し（消去）は、P0 と v2 の配備の後に1回だけ行う。
