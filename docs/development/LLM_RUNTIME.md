# 推論経路と処理結果の所有者

この文書は開発候補のコードにある推論経路、状態と保存の境界を説明します。
実機の稼働状態や生成品質を保証する文書ではありません。情報の送信先と許可は
[SECURITY](../../SECURITY.md)、配置と起動引数は
[スケジューリング構成](../../deployment/launchagents/README.md#3レーン構成)、
評価の受入は[意味解析の評価](../specs/semantic-evaluation.md)と
[封印候補の昇格](../specs/SEALED_CANDIDATE_EVALUATION.md)が正本です。

## 呼出しから保存まで

| 処理 | 呼出し・入力の所有者 | 検証・保存の所有者 |
|---|---|---|
| 投稿の構造化抽出 | [extract_llm](../../mcs/extract/llm/extract_llm.py)が原文、ルールのヒント、必要な参考文脈を構成 | [extract_validation](../../mcs/extract/llm/extract_validation.py)がschemaと原文根拠を検証し、[extract_quality](../../mcs/extract/llm/extract_quality.py)が修復・置換の可否を判断。claim・carry・checkpoint・artifact保存はextract_llm |
| 意味解析・修復 | [semantic_extraction](../../mcs/semantic/semantic_extraction.py)等が入力を構成し、[semantic_transport](../../mcs/semantic/semantic_transport.py)が形式probeと通信状態を所有 | [semantic_drain](../../mcs/semantic/semantic_drain.py)がjobのclaim・予算・保存・公開を統括。QCと候補承認は別の契約 |
| 患者コンテキスト作成 | [patient_context_refresh](../../mcs/patients/patient_context_refresh.py)が一患者の根拠と差分を選択 | 同moduleがsource/config/scanの現行性を保存直前に再確認し、患者単位のartifactを原子的に置換 |
| 推論版と全件再処理 | [inference_version](../../mcs/core/inference_version.py)が推論版を定義し、既存workerが保存済み対象を有限件ずつ列挙 | [semantic_drain](../../mcs/semantic/semantic_drain.py)が既存の予約・進捗で各段階の現行性を確認。原文消失や保留を成功へ変換しない |
| 時系列と現在状態の投影 | [patient_context_state](../../mcs/patients/patient_context_state.py)が根拠付きfactと依存喪失の記録を受け取る | 通信・DB書込みを行わない判断処理。保存と公開はcontext刷新・rollupの各所有者が行う |
| 根拠が失われた場合の保全 | [inference_dependency_loss](../../mcs/core/inference_dependency_loss.py)が原文・派生物の破棄前に依存を捕捉 | 呼出元の既存transactionで、本文・引用・用量を含まない記録を保存。読取りでは現在状態への誤昇格を防ぎ、削除された原典の代わりに使わない |
| 任意の意味チェック・抽出監査 | [semantic_jev](../../mcs/semantic/semantic_jev.py)が固定Jevモデルの通信・応答契約を所有 | drainのscope・予算・pause・回路と、各評価/QCの根拠検証を通して結果を保存 |

共通のローカル通信は[local_llm](../../mcs/llm/local_llm.py)、
短命子プロセスによるHTTPの期限・kill/reap・環境隔離・応答上限は
[bounded_http](../../mcs/core/bounded_http.py)が所有します。
借用したDBを通信層がcloseしたり、推論成功だけで公開済みにしたりしません。

[推論v1.0のルールと再処理契約](../specs/inference-engine-v1.md)は独立した開発候補の仕様です。合成検証、実生成の評価、全対象の再処理、公開の受入を分けて管理します。

再処理の進捗は既存の`semantic_sched`に保存します。`source_epoch`は原典・権限、`analysis_epoch`は採用した解析・患者要約・集約の証拠、`job_state_epoch`は実際のjob状態遷移を追跡します。最後の値は待機の再確認に使い、推論の意味、試行回数、通知資格を変更する世代にはしません。状態変更と値の更新は同じtransactionで行います。

走査中は上限IDとcursorを保持し、新着や解析の採用ごとに先頭へ戻しません。後続の対象は次の有限範囲で処理します。同じ入力で評価済みのjobは、患者全体の完了証拠が不足するだけで再投入しません。入力と進捗が同じ待機中は全件を再走査せず、保留・停止・失敗・復元不能な根拠の不足を理由別に保持します。新着が無限に続く条件で全体完了を保証するものではありません。

## レーンとadmissionは別の契約

レーン1/2/3は`local_llm.LANES`のwire `id_slot` 0/1/2です。
背景レーン2/3は1つのturn lockで交代します。処理順、plistとwrapperの正本は
[3レーン構成](../../deployment/launchagents/README.md#3レーン構成)を参照してください。

共有brokerは`MCS_LLM_ADMISSION`によるopt-inです。
[llm_admission.DEFAULT_ROUTES](../../mcs/llm/llm_admission.py)では、MCSのextract・semantic・QC・benchは
`BACKLOG`、Hermes対話等は`RT`へ束縛されます。レーン1という名前からbrokerの`RT`や有効状態を推測しません。
送信後のtimeoutや`unknown`はbackendの終了証拠と区別し、permitを無条件に再利用しません。

## 生成と再試行の上限

以下はコードの既定値であり、各呼出しはjob全体の残予算でも制限されます。

| 呼出し | timeout | 出力上限 | 正本 |
|---|---|---|---|
| 投稿抽出の単発 | 300秒 | 1600 tokens | `extract_llm.TIMEOUT / MAX_TOKENS` |
| semanticの通常呼出し | 600秒 | 4096 tokens | `semantic_transport.LLM_TIMEOUT / LLM_MAX_TOKENS` |
| 患者コンテキストの一呼出し | 300秒 | 1600 tokens | `patient_context_refresh.CALL_SECONDS`と呼出し引数 |

モデル・endpointは呼出元の設定と`local_llm.resolve()`から解決します。
`temperature=0`だけで実生成が完全に決定的になるとは扱いません。
長文は全区間をchunkで処理し、参考contextを原文evidenceへ付け替えません。

投稿抽出は`json_schema → json_object → plain`、semanticは`json_object → plain`の
形式probe・降格経路を持ちます。probeのbusyを形式拒否として固定しません。
投稿抽出の`--batch`既定は0（単発）です。明示した`--batch K`では条件を満たす入力をまとめますが、
各itemの原文照合を省かず、欠落や無効itemは既存の残予算内で単発処理します。
schemaの正本は[extract_schema](../../mcs/extract/llm/extract_schema.py)です。

修復・再抽出は品質判断と回数・残予算の上限を持ちます。原文が裏付ける否定や他者情報を
失う置換は`held_prior`で止めます。保存ロック待ちの完成結果はclaimと世代へ束縛して保持し、
原文変更・lease喪失時は解放します。患者コンテキストは終日処理で、JST 03:30が作業日の境界です。

## Jevと評価の境界

通常の意味チェック・監査は`semantic.mode`、認証、scope、日次予算等を満たした場合に呼び出します。
`semantic.extract_qc="annotate"`は抽出結果へ注記し、裏付け不足等の後続ローカル再抽出は1回に制限します。
モデルは`jev-1.13.0`へ固定し、応答モデル不一致を拒否します。
429/5xx/transportのretryはjobの残予算内、401/403はretryしません。

評価CLIには、通常のmodeとは別に実LLM/Jevを呼ぶ入口もあります。
Jevの実通信は手動評価・モデル一覧の取得・再試行も同じ日次上限へ数えます。
キーによる自動有効化と上限の設定は[Jev・Anthropicの設定](../guides/ANTHROPIC.md)を参照してください。
`eval/`や`local_only`という名前だけでofflineとは判断せず、引数と呼出先を確認してください。
`extract_bench --mock-ok`はfixtureとvalidatorの整合検査であり、実生成の精度や速度の証明ではありません。
通常テストは[隔離runner](DEVELOPMENT.md#検証)で合成DB・通信スタブを使います。
実推論の比較には、別途許可された専用環境と固定入力が必要です。

## 変更時の確認先

schema・prompt・検証・品質・意味判定の実装を動かす場合は、
`extract_llm._LOADED_SOURCE_DIGESTS`と`semantic_candidate`の定義module収集の両方を確認します。
挙動を保つ移動でも候補指紋は変わり、旧候補や保留receiptの再判定が必要になることがあります。
hashを固定して通す方法や、単なる再exportで変更検知から外す方法は使いません。

測定値は観測できた範囲だけを扱います。`probe_runs`は形式検出処理の巡回数で、HTTP要求数ではありません。
`cache_hit_ratio`は両fieldを観測した呼出しの`Σcache_n / (Σcache_n + Σprompt_n)`で、欠測はnullです。
非streaming経路のTTFTや、取得していないtoken数を推定値で埋めません。
