# 評価資産とG6基準

現行の昇格判定は、`semantic_candidate.promotion_problem()`が[g6-criteria-v2.json](g6-criteria-v2.json)を読み、[封印候補評価の契約](../docs/specs/SEALED_CANDIDATE_EVALUATION.md)に従います。人手評価・対象患者/thread・層別件数・各指標の分母・独立承認が必要です。合成試験の成功は昇格の証拠にしません。

推論エンジンv1.0の開発・全件再処理・評価は[推論版の仕様](../docs/specs/inference-engine-v1.md)に従います。実生成の品質と既存G6の昇格を別々に確認し、未測定を合格にしません。

## G6初期運用基準 v1（比較用の旧基準）

評価担当・運用責任者：ユーザー本人。2026-09-21に基準設定を実装担当へ委任されたため、この版を初期基準として固定する。既存の例示criteriaとは区別する。医療的安全性の保証ではなく、候補版の昇格判定基準である。

旧基準での比較には`g6-criteria-v1.json`を評価器へ指定する。現行の昇格判定へは流用しない。重要fact・最終要約・薬剤/否定/時制/投稿者関係の保持率98%以上、Loop適合/一致95%以上、critical overclaim・誤解決・未解決見落とし0件、保留率30%以下とする。

- 最終test集合は人手評価200件以上。校正に使用した患者/threadは最終testから外す。合成ラベルを人手ラベルに付け替えない。
- 3方式を同じ固定bundleで盲検比較し、主要指標の分母・誤数・95%区間を併記する。重要な希少事象も含め、指標分母が0なら合格しない。200件だけで十分な精度を証明したとは扱わない。
- critical誤りは1件でも修正と回帰試験が済むまでenforceへ進めない。人手確認が追いつかない場合も昇格を保留する。
- 性能条件：全巡回p95が15分未満、既存取得・通知の優先順序と予算を維持する。患者別意味処理時間・要求/token数・最古job age・欠測率を報告する。未計測は合格証拠にしない。性能条件は現在の品質JSON gateとは別に運用責任者が確認する。
- shadow/assistで運用しながら調整する場合、理由・変更日を記録してv2以降を作る。同じtest結果を見て閾値を下げて合格とせず、次の固定評価集合で再評価する。
- 品質gateが通っても自動配備・enforce切替はしない。権限/原文・添付配送/復旧など他gateも必要。

現時点で実評価の合格を主張しない。本人確認された実人手ラベル・許可済みデータ・実測値は未取得。


2026-09-21：ユーザーより本評価データは「未準備」と回答あり。
`rehearsal-comparison.jsonl`は非医療の架空事務連絡1件による記入練習。
3出力はAIが手作成した説明用例で、実モデル出力・人手ラベル・G6評価件数ではない。
`semantic_blind.py`の通常CLIで定性比較票を作成可能。定量評価には使わない。

## 抽出の合成資料と世代11の基準値

投稿ごとの抽出を採点する合成資料です。本文はすべて創作で、実投稿も匿名化した実投稿も含みません。採点器は `mcs/extract/eval/extract_bench.py`、使い方は [DEVELOPMENT.md の「抽出品質の評価」](../docs/development/DEVELOPMENT.md#抽出品質の評価) にあります。

| ファイル | 内容 |
|---|---|
| `extract_cases.json` | 薬・症状・イベント・依頼・返信など、抽出全般 |
| `extract_cases_labs.json` | 検査値 |
| `extract_cases_patient_state.json` | 背景参照 |
| `extract_cases_injection.json` | 本文や参照文に混入した命令 |
| `extract_cases_safety_history.json` | アレルギー・副作用歴と ACP の誤断定（否定、家族、別患者、予定・仮定、引用された過去記録、疑い、対照） |
| `extract_baseline_gen11_safety_history.json` | 上の資料に対する世代11の基準値。`score` の入力形式で、集計値は持たない |
| `extract_baseline_rule30_safety_history.json` | 同じ完全合成資料に対する規則抽出30・文脈4の出力。旧基準値は変更せず保持 |

- 合成ベンチは回帰ゲートです。合格しても実データでの品質は示さず、昇格の根拠にしません。
- 基準値に入っているのは、通信なしで再現できるルール抽出（RULE 29、文脈4）の `patient_context` だけです。ルール抽出の症状・薬・イベントは採点器が前提とする形と違うので、対象外として数値を持たせていません。
- **LLM 抽出の世代11は未測定です。** 実モデルの実行が必要で、完全合成の資料を使う所有者承認の手動実行（`run`）の報告を `score` に渡して埋めます。未測定を合格や0件として読みません。
- 世代11には、主体・時制・否定の軸を持つアレルギー欄がありません。この欄の再現率0・誤断定0は「欄なし（未取得）」で、誤断定が無いという意味ではありません。採点の報告は `fields_without_output` でこれを区別します。
- アレルギー欄の出力形（`extract_bench_assertions.py` の冒頭）は評価側の仮の形です。欄を初めて出す世代が形を決めたら、同じファイルの `units` だけを合わせます。
- 新しい資料は意味解析の評価（`semantic_bench`）と Jev の評価（`mcs_setup`）の許可一覧には足していません。外部や別の評価へ送る本文の範囲を広げないためです。

規則抽出30の基準値は、旧基準値のメタデータを引き継ぎ、`generations` を実行時の
`extract.RULE_VERSION` と `extract_llm.PATIENT_CONTEXT_VERSION` にした上で、次の既存の
抽出手順で全39件の `outputs` を生成しました。集計値やLLM出力は手作成していません。
`tests/extract/evaluation/test_safety_history_evaluation.py` が現行出力との完全一致と、
新旧両方のオフライン採点を検証します。

```python
outputs = {
    case["id"]: {"patient_context": extract.extract_message(
        case["body"], case["posted_at"]).get("patient_context", [])}
    for case in cases
}
```

```sh
python3 mcs/extract/eval/extract_bench.py score \
  --cases evaluation/extract_cases_safety_history.json \
  --outputs evaluation/extract_baseline_rule30_safety_history.json \
  --tag rule30 --out baseline-rule30.json
```
