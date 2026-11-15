# 患者コンテキストの機能・安定性レビュー（2026-11-15）

コードを読んで行ったレビューの記録です（HEAD `eef4e0d`）。テストは実行していません。「要検証」と書いたものは、まだ再現していません。
クラウド送信と切替の論点は [切替レビュー](PATIENT_CONTEXT_ROUTING_REVIEW_20261115.md) にまとめています。

## 要点

- **原因も解除方法も示されないまま、永久に止まる患者が出る。**
  - 原因は依存喪失（変更前の根拠が失われたこと）と、資料単位の保留です。
  - 依存喪失の記録は消去しても残るため、全体の消去でも直りません。
- **「記載なし」と言い切る表示が1か所ある。**
  - 退院・移行時の経過で、未抽出・未取得の投稿があっても「記載なし」と表示します。
  - 絶対ルール（記録が見つからないことと対応がなかったことを区別する）に反します。
- **履歴の多い患者ほど遅くなる。**
  - 古い契約の調査結果が溜まり続けます。
  - それを全件読む処理があり、一部は書込みロックの中で行われます。
- **状況を確かめる手段がほとんど働いていない。**
  - doctor は全患者を1秒で数えようとして、ほぼ unknown になります。保留があっても「healthy」と報告します。
  - 「👤」は、ローカルだけの設定や大きな患者ではいつも「—」です。
- **（要検証）並行作業の競合。**
  - 作業者を複数にすると、同じ患者の A と B/C が競合し得ます。
  - 先に保存した側が、他方の支払い済みの結果を捨て、生成枠も1回分失わせます。

## 必ず直す

### M1. 未抽出・未取得を「記載なし」と表示する

- 該当: `mcs/views/patient_context_view.py:979-980`、`mcs/notify/notify_views.py:1360-1361`
- 対応
  - 文言を「記載が見つかりません」にします。
  - 解析・取得が完了していない間は、行を出さないか、「未完了のため未確認」と表示します。

### M2. 解除できない永久停止に、理由も対処も示されない

- 該当
  - 依存喪失: `inference_dependency_loss.read`（`unscoped_gap`・terminal）、`patient_context_refresh_readiness.py:841-843`
  - 資料単位の保留: `patient_context_refresh.py:1409`、`patient_context_refresh_sources.py:296,307,316`
- 対応
  - 理由の固定コードごとの件数を `lane_metrics`・`status`・🧾 に出します。
  - 患者1人単位の再点検・解除を、既存の人承認経路（preview-hash・`--confirm-human`・reason・receipt）に `--project-id` として加えます。
  - terminal な依存喪失は「解除できない」と明示します。

### M3. （要検証）同じ患者の並行作業に排他がない

- 該当: `patient_context_refresh.py:512-629,1380-1396,494-500`
- 対応
  - 確保のトランザクションで、患者の PROGRESS に `unit_lease_until` を書きます。
  - 選択時は、期限内のリースがある患者を飛ばします。

### M4. 古い調査結果が溜まり続け、書込みロックの中で全件を読む

- 該当
  - `patient_context_refresh_state.py:157-195`（同じ backend_slot・同じ unit_key の行だけを削除している）
  - `_scan_sig`（`patient_context_refresh_state.py:314-331`）が全行の content を読む
  - `_current_scans_snapshot`（`patient_context_refresh_sources.py:194-212`）
  - `lane_metrics` の相関副問合せ（`patient_context_refresh_readiness.py:589-594,616-620`）
- 対応
  - maintenance に、現行の契約でない SCAN・CHUNK 行の削除を加えます。ただし次の行は消しません。
    - P0 の pin で残す行
    - 依存喪失が参照している行
    - 公開版が参照している行
  - `_scan_sig` は `(artifact_id, meta)` を基本にします。
  - `lane_metrics` は `MAX(artifact_id) GROUP BY` で最新行を選ぶ形にします。

## 直した方がよい

- **S1. 1患者の不正な行が全体を止める。**
  - 該当: `patient_context_refresh.py:473-479`、`patient_context_refresh_readiness.py:595-596,635-649`
  - 対応: 患者ごとに固定コードとして数え、次の患者へ進みます。
- **S2. することがないときも空回りする。**
  - 該当: `patient_context_refresh.py:346-363,571-573`
  - 患者が9人以上いると、対象がなくても常に `scan_incomplete` になります。
- **S3. A の先送りに打ち切りも間隔の伸長もない。**
  - 該当: `patient_context_refresh.py:1417-1421`
- **S4. doctor が状況を正しく示さない。**
  - 該当: `mcs_setup.py:928-948`
  - 対応: 集計 SQL で状態別の件数を出し、保留や依存喪失があれば warning にします。
- **S5. 🧾 の状態行の訳が足りない。**
  - 該当: `notify_views.py:1437-1447`
  - 18状態のうち8つしか訳がなく、次の作成予定時刻も出していません。
- **S6. 🧾 のフォールバックが原因を記録しない。**
  - 該当: `notify_actions.py:401-405`
- **S7. 「前回から変わったこと」の初回に全履歴が並ぶ。**
  - 該当: `notify_actions.py:591`
  - CLI の既定は7日です。
- **S8. 「👤」の進捗がほとんど「—」になる。**
  - 該当: `patient_context_progress.py:14,180-184`
- **S9. 開くたびの再計算が重い。**
  - 該当: `patient_context_view.py:298-317`、`patient_context_refresh_readiness.py:353-366`
- **S10. 未束縛の変数で UnboundLocalError になる経路がある。**
  - 該当: `patient_context_refresh.py:1057,1407,1425`
- **S11. 作成枠の記録の不正や時計のずれで、A が理由なく止まる。**
  - 該当: `patient_context_quota.py:58-71,80-81`

## あるとよい

- `patient_context_refresh.py:30-180` の141個の再公開を整理します（`find_unused_code.py` で確かめてから行います）。
- `not window` が常に偽、分岐の中の `import semantic_policy`、`scan_plan` の `covered` が常に False、の3点を直します。
- 進捗の snapshot と生成枠の記録が削除されない問題を直します。
- `_frozen_context_progress` が、カードの書込みトランザクションの中で witness を再実行している点を直します。

## 機能の提案（価値とコストの順）

1. 🧾 に「状況と対処」の1行を出す（価値 高、コスト 低）。
2. 患者単位の再点検・解除コマンド（価値 高、コスト 中）。
3. 全体像を職種の分類で絞り込む（価値 中〜高、コスト 低）。
4. 引継ぎ・訪問前ノートの全文テキスト。本人のみに出す（価値 中、コスト 低）。
5. 運用者向けの患者別の状況表示 `mcs_view patient_context --view status`。本文は出さない（価値 中、コスト 低）。

## 文書とコードの食い違い

- 仕様 :27 のルール世代が「24」になっていますが、コードは 29 です。
- README.md:108・CHANGELOG:91 の「👤 73%」には、ローカルだけの設定ではいつも「—」になることが書かれていません。
- 仕様 :172 は排他区間を「短い」としていますが、実際は患者の規模に比例します。
- 仕様 :130 には、患者が9人以上いると常に `scan_incomplete` になることが書かれていません。
- 仕様 :142 の「128根拠」は累積の上限ではないとされていますが、SCAN の checkpoint と採用した修復の facts は累積で切り詰めています。
- 「前回からの変化」の初回の基準が0（全履歴）であることが書かれていません。CLI の既定（7日）とも違います。
