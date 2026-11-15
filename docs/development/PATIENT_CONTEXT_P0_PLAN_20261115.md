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
`rule` と `llm` は、資料ごとの `extraction_sig` で置き換えます。

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

- 対象は、現行で無効になっていない scan chunk・scan・公開版の meta の `cloud_receipts` が参照する予約です。
- その予約の reserve・result・disposition の各行を、まとめて残します。
- meta が不正な患者は、その患者の行をすべて残します。

使用額・集計・再送防止の判定は日付の窓で絞って読むので、影響しません。

## 所有者の決定（未決）

- **D1: 資料ごとの束縛を内容基準にするか**
  - 現状: `extraction_sig` と `derived_summary_sig` は、抽出・意味解析の artifact の id と版に結び付いています。そのため、抽出や意味解析の世代を上げるたびに全資料の scan が順に失効し、クラウドの費用は作り直しと同じくらいかかります。
  - 推奨: 実際に渡した入力の内容（抽出の `patient_context` 配列と補助要約の本文）の hash に結び付けます。
- **D2: 品質確認の識別値の範囲**
  - 現状: `_quality_code()` はほぼ全コードを含みます。そのため、リリースのたびに全資料の品質確認が失効し、Jev の再確認が終わるまでクラウドの公開版が非表示になります。
  - 推奨: 識別値を次の形にし、品質確認の意味が変わるときだけ手動の版定数 `QA_VERSION` を上げます。
    - source-QA: `digest([scan 契約, QA_VERSION])`
    - 要約QA: `digest([scan 契約, 要約契約, QA_VERSION])`

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

- 設計書は、契約に入るのを plan・projection・dependency_loss のhashだけとしています。しかし実際には、`patient_context_scan.py` は契約に入っておらず、品質確認は `_quality_code` を通じてほぼ全コードに結び付いています。
- 設計書は projection を要約側として扱っていますが、scan 側の `annotate` を含んでいます。
- `PATIENT_CONTEXT_VERSION` は `extract_llm_source_contract.py:40` で定義され、`extract_llm.PATIENT_CONTEXT_VERSION` として参照されています。
- `CLOUD_SEND_FOUNDATION_DESIGN_20261115.md` §6 の maintenance の行番号がずれています（実際は修復の pin が :1093、添付OCRの pin が :1225）。
