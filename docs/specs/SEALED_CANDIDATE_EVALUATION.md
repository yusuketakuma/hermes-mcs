# 封印した抽出候補の評価と昇格契約

本書は 1.0.23 の監査項目11–15に対応する実装契約である。既定の `legacy` は変更しない。候補と証拠が一致しない `canonical` は `legacy` / `mode=off` に戻し、設定エラーを返す。

**評価用の候補生成・採点・昇格防御は実装済みだが、現在のローカル生成経路だけではG6を完了できない。** 監査、最終claims/loops、描画、配送の観測を生成していないためである。未観測を成功に補完しない。今回、実データ、実LLM、実サービス、実機での昇格は実行していない。

## 1. 正本と構成

| 契約 | 正本 |
|---|---|
| 候補指紋・報告検査・人承認receipt | [semantic_candidate.py](../../mcs/semantic/semantic_candidate.py) |
| 起動時の設定検証・policy指紋 | [semantic_policy.py](../../mcs/semantic/semantic_policy.py) |
| runtime/eval共通の合否判定 | [semantic_acceptance.py](../../mcs/semantic/semantic_acceptance.py) |
| 集計・由来・型検査 | [semantic_evaluation.py](../../mcs/semantic/eval/semantic_evaluation.py) |
| holdout・split・二重読み | [semantic_holdout.py](../../mcs/semantic/eval/semantic_holdout.py) |
| 私有snapshotの候補生成・採点CLI | [semantic_snapshot.py](../../mcs/semantic/eval/semantic_snapshot.py) |
| 新基準 | [g6-criteria-v2.json](../../evaluation/g6-criteria-v2.json) |
| 旧基準（比較用に保持） | [g6-criteria-v1.json](../../evaluation/g6-criteria-v1.json) |

定期処理は `semantic/eval/` をimportしない。共通の合否判定をruntime側に置き、評価CLIがそれを利用する。

## 2. 候補の同一性と旧設定

`candidate_fingerprint(cfg)` は次を束ねる。

- `EXTRACT_VERSION`、`PLAN_VERSION`、`PATIENT_CONTEXT_VERSION`、抽出prompt/schema、ロード時の抽出ソースdigest。
- semantic抽出・監査・描画・projection・設定・合否判定と、実効緊急表示 `structured_view` のロードされたコード・定数。
- `local_llm.resolve(cfg)` のendpoint/model、local_llm設定、semanticのJev model、`POLICY_VERSION`。
- semanticの閾値・calibration・fact_source等の解釈設定、extract設定（`patient_state_context` は未指定とfalseを同一視）、実効 `vital_urgency` 方針。

昇格証拠そのもの、承認参照、retry/時間budget、通知までの待機時間、mode、summary/loop mode、project scopeは指紋から除く。承認日付だけで解釈cacheを変えない。legacyの既存policy hashを維持し、canonical/shadowのpolicy hashには候補指紋を含める。

新トークンは完全長の `criteria_sha256:report_sha256:candidate_fingerprint`（194文字）。report hashはJSONファイルの空白を含むraw bytesではなく、`payload_hash(report)` のcanonical JSONを対象とする。起動時とdoctorの設定検査で再照合する。旧 `<criteria version>:<短いdigest>` や任意文字列は通らない。`PLAN_VERSION=1` から `2`、prompt、model、context flag、判定閾値の変更でも以前の証拠は失効する。

ロードコードのdigestにはPython code objectを使うため、Python環境・配置変更でも再評価が必要になる場合がある。指紋はモデルファイルの重み自体のhashではない。同じmodel idで重みを差し替えたことを証明する契約は未実装。

## 3. CLIと入力・出力境界

以下はコマンド形式であり、今回の作業で実データに対して実行したものではない。`PRIVATE` はrepo外の権限を管理した私有ディレクトリを指す。

```sh
python3 mcs/semantic/eval/semantic_snapshot.py generate \
  --input "$PRIVATE/snapshot.json" --config "$PRIVATE/candidate-config.json" \
  --output "$PRIVATE/run.json"

python3 mcs/semantic/eval/semantic_snapshot.py score \
  --input "$PRIVATE/run.json" --config "$PRIVATE/candidate-config.json" \
  --reviewed "$PRIVATE/reviewed-records.json" --output "$PRIVATE/report.json"
```

| 境界 | 現在の動作 |
|---|---|
| ファイル | repo内の入力・設定・出力を拒否。`CI` 環境で拒否。既存入力は通常ファイルかつgroup/other権限なし。最終ファイルをnofollowで一度だけ読み込む |
| 容量 | snapshot/reviewedは64 MiB、configは1 MiBまで |
| JSON | 重複キー、NaN、Infinity、overflowする浮動小数点を拒否 |
| 出力 | 新規ファイルのみ、0600、排他的作成。既存run/reportを上書きしない |
| モデル | HTTPのloopback（127.0.0.1、localhost、::1）限定。既存のローカルLLM経路を使用。Jev clientを作らない。共有admission有効環境は拒否し、保護を自動解除しない |
| 副作用 | 本番DB、通知先、MCS、Keychainには接続しない。本文と中間出力は私有runに残る |
| 終了値 | 処理成功0、入力/出力エラー2。**scoreの0はG6合格ではない**。`report.gate`を確認する |

`generate` はsnapshotの `records`、`manifest`、`data_origin` を読む。`real_authorized` では `authorization_receipt_sha256`（64桁hex）を要求する。この値の形式とsnapshotとの結合は検査するが、外部の利用許可台帳の実在・権限まで認証する仕組みではない。データ利用許可の確認は別途必要である。

recordsの基本形は既存[評価仕様](semantic-evaluation.md)の識別子とbundleを使用する。各recordは `case_id/account_id/project_id/thread_id/patient_id/split/stratum/data_origin/bundle` を持つ。bundleの `messages` は `message_id/body` と必要な `revision/posted_at/body_state/patient_reports` を持つ。実効AI優先判定にはbundleの `project_type`、必要なら `patient_name` を使う。project_type欠測は患者本人と決めずgroup相当で保留する。生成入力にcandidateやlabelを持ち込まない。

`run.json` は本文を含むrecords、生成されたcandidate/documents、同一bundleのpatient-state off/on出力、候補指紋、snapshot hash、予測全体のhashを保存する。`external_audit_used=false`、`local_only=true` を記録する。**runはmetrics-onlyではないためrepo・CI・一般共有への出力対象にしない。**

現在のlatency/usageはcanonical facts生成部分の計測であり、先行するA/B抽出を含む全工程の性能値ではない。

`reviewed-records.json` はrunのrecords配列と同順・同数で、各recordへ `label` だけを追加したJSON配列である。candidate、本文、A/B出力、識別子等の変更は拒否する。候補指紋と予測hashが一致する同じrunだけを採点する。

## 4. 由来、封印、split、人レビュー

manifestとrecordの `data_origin` は `synthetic` または `real_authorized`。未指定の旧データはsyntheticとして扱う。manifestの `source=fully_synthetic` とreal_authorizedの併記、manifestとrecordの由来不一致は拒否する。人がラベルを付けても合成データの由来は変わらず、G6用の実データ件数には加算しない。

`manifest.holdout_manifest_sha256` は `payload_hash(semantic_holdout.holdout_payload(records))`。case_id順に並べた識別子、split、patient、stratum、由来、bundleが対象で、候補生成前に固定する。候補出力は別の予測hashへ固定する。

`manifest.split_registry` は `dev/calibration/test` の3キーを持ち、それぞれpatient_id/thread_idの登録配列を持つ。現在の評価ファイルにないdev/calibrationも登録し、testと患者・threadを共有させない。全recordの登録を要求する。未登録の過去の開発利用まで自動発見するものではなく、登録集合の完全性は運用確認が必要。

人labelは既存の `receipt.receipt_id/labelled_at/reviewer` に加え、次の証跡を持つ。

| 項目 | 検査 |
|---|---|
| `manifest.reviewers` | 許可したreviewer識別子の名簿。私有入力にのみ保持 |
| `label.reviews` | 2件、異なる名簿内reviewer。各件に一意receipt_id、`blind=true`、candidate_sha256、label_sha256 |
| candidate_sha256 | `payload_hash(record.candidate)` と一致 |
| label_sha256 | labelからreviews/adjudication/receiptを除いた内容のhash |
| `label.adjudication` | 2件の判断が最終labelと一致しない場合、別の名簿内reviewer・一意receipt・最終label hashを要求 |

reviewerの実在や実際に候補名を伏せて読んだことをhashだけで証明するものではない。新runnerはblind worksheetの作成・配布UIを持たず、runのA/B識別子も伏せない。独立二重読みの準備、blind化、裁定記録は私有のレビュー手順で実施する。これを自動化した完全なblind運用経路は未実装である。

## 5. v2の合否と各metricの対象

testで人ラベル付きreal_authorizedを200件以上、患者20以上、thread50以上必要とする。`medication/negation/urgency/patient_state` の各stratumは40件以上。**全必須metricに分母200以上を要求するため、総case数200だけで十分とは限らない。** 必須metricの欠測・分母0は不合格。全件のlatency、requests/input/output token観測とfact lifecycleも必要。

| metric群 | gate |
|---|---|
| important/final、medication/negation/time/speaker_relation、relation_accuracy | 成功率のWilson 95%下限 ≥ 0.98 |
| loop_conformity、loop_precision | 点推定 ≥ 0.95 |
| mandatory/rendered/delivered_fact_recall、evidence_closure | 点推定 = 1.0 |
| critical_overclaim、loop_false_resolution、loop_unresolved_miss_rate、silent_drop | 観測誤り0。母集団の誤り率0を証明したとは扱わない |
| defer_rate | 点推定 ≤ 0.30 |
| urgency_recall | 人labelがhighのcaseを分母とし、candidateもhighの割合 ≥ 0.98 |
| ai_priority_accuracy | 既存runtimeの実効AI緊急表示を正解labelのboolと比較、正解率 ≥ 0.95 |
| patient_state_ab_noninferiority | 同一bundleでcontext onの人採点がoff以上となるcaseの割合 = 1.0 |

AI優先は新しい順位付けclassifierではない。`structured_view.message_urgency_details(...).source == 'llm'` による「緊急度高」表示を対象とする。snapshot adapterは同じ生成artifactと本文をメモリSQLiteへ置き、その既存判定を実行する。生の `urgency=high` だけでは本人・臨床・根拠のgateを通ったことにしない。candidate/labelの `ai_priority` はbool限定、抽出失敗は欠測。group、他者、条件的な緊急連絡がAI優先へ昇格しないことを合成テストする。

A/Bは同じbundleのhashと異なるoff/on候補指紋を保持する。labelの `patient_state_ab` に `off_sha256/on_sha256`（各arm全体のhash）、`off_score/on_score`（0〜1）を持たせる。armのhash不一致、欠測、劣化は成功件数へ加算しない。採点rubric自体は本実装で定義していないため、レビュー前に固定する必要がある。これは臨床的な非劣性マージンを用いた推測統計試験ではなく、同一caseの非劣化ゲートである。

## 6. 昇格と独立した人承認

完全な受入証拠が揃った場合のコマンド形式は次のとおり。

```sh
python3 mcs/ops/mcs_setup.py fact-source canonical \
  --gate-evidence "$PRIVATE/report.json" \
  --confirm-human --reason reviewed_sealed_holdout
```

reportはshipped v2と一致し、候補指紋が現在の設定と一致し、metrics-onlyでなければならない。合否booleanを信用せず、件数・率・Wilson値を再計算してgateを再評価する。原文、case識別子、自由記述を含む報告や未知の報告構造は拒否する。エラーで入力本文やgateの自由記述を表示しない。

明示確認がある場合に限り、`$MCS_ROOT/data/semantic_approvals/<binding hash>.json`（MCS_ROOT未指定時は `~/.mcs/data/semantic_approvals/`）へ0600でreceiptを排他的に作成する。receiptはcandidate、criteria、report、snapshot、holdoutのhashと固定reasonを束ねる。同一receiptの再試行は同じbindingの場合だけ許す。receiptはreport/configと別に保存する。

configにはmetrics-onlyの `semantic.fact_source_evidence`、完全長token `fact_source_gate`、receipt識別子 `fact_source_approval` を保存する。起動時は別保存のreceiptまで照合する。reportの `pass=true` だけでは承認を作れない。同じOS所有者が承認ストア自体を書き換える攻撃への署名・権限分離基盤は提供しない。

`fact-source legacy` / `fact-source shadow` はこれらの設定上の参照を外す。過去の私有receiptは履歴として残す。`init --set` はcanonical選択・pinの変更経路にならない。doctorは既存のconfiguration checkに不一致を出す。

## 7. 項目11–15の受入状況と残る実装

| 項目 | 確認済み実装 | 未完・未確認 |
|---|---|---|
| 11 候補同一性 | 版/prompt/schema/model/設定/実効バイタル方針を固定、tokenと独立receipt照合、不一致legacy/off、旧証拠失効 | 実機配備後のコード・モデル照合は未実施。同一model idの重み差替え検知なし |
| 12 データ由来 | manifest/record由来一致、合成への人labelでG6不可、実由来件数下限 | 実データ利用許可台帳との独立照合は未実装、実データ評価未実施 |
| 13 holdout/レビュー | source封印、split registry、一意receipt、名簿、二重読み/裁定のhash契約、層別下限 | registry完全性・reviewer実在・blind実施の確認は人運用。新runner専用blind worksheet経路は未実装 |
| 14 基準/安全指標 | v1保持/v2追加、分母/Wilson、緊急度、実効AI優先、同一case A/Bの生成・採点gate | A/B採点rubricの固定と実データの達成値は未確認。独立classifierは追加しない |
| 15 封印runner | repo/CI外、local-only、ロード候補刻印、予測変更拒否、metrics-only report、人承認照合 | **G6を通せるend-to-end生成経路は未完**。Jev監査、最終claims/loops、verified/rendered/delivered観測を生成していない |

項目15の残作業は、既存 `semantic_lifecycle.fact_lifecycle` 等が読む実際の監査・描画・配送証跡を、同一source/candidate/policyへ束ねた私有snapshotから取り込む経路である。現在の `score` はlabel以外の後付け変更を拒否するため、後からcandidateのlifecycle配列を書き足して合格させることはできない。観測receiptを候補と一緒に封印する生成段階の統合と、その一致・欠測・世代差異の回帰テストが必要となる。local-only実行で外部監査を実施したと捏造することや、未配送を配送済みへ補完することは解決策に含めない。

### local-only受入と本番G6の境界

項目15のlocal-model-only生成・封印・採点という実装経路は合成スタブで検証済みである。実モデル接続・実データ品質・配送を含む本番G6の受入は未実施であり、同じ完了状態にしない。

| 区分 | コード根拠と残作業 |
|---|---|
| 既存機能・実環境検証待ち | [semantic_v4.py](../../mcs/semantic/semantic_v4.py) のfact監査判定、[semantic_llm.py](../../mcs/semantic/semantic_llm.py) の最終要約、[semantic_loops.py](../../mcs/semantic/semantic_loops.py) のloop更新、[semantic_render.py](../../mcs/semantic/semantic_render.py) の描画、[semantic_lifecycle.py](../../mcs/semantic/eval/semantic_lifecycle.py) の監査/描画/配送receipt読取りは既存機能。今回その実データ品質・実サービス受領を確認していない |
| 新runnerへの統合が未実装 | [semantic_snapshot.generate](../../mcs/semantic/eval/semantic_snapshot.py) はfactsとA/B抽出を生成するが、上記最終claims/loopsとlifecycle証跡を同一候補に結び付けて封印するadapterを持たない。外部呼出しを追加せず、許可済み私有snapshotの同一世代receiptを読取り、欠測・不一致を保ったまま生成時に固定する統合が必要 |
| 盲検資料の既存機能と追加統合 | [semantic_blind.py](../../mcs/semantic/eval/semantic_blind.py) は従来3方式の匿名化資料・方式復元を持つ。新runnerのpatient-state A/Bと二重レビュー契約に対応した資料生成adapterは未実装。reviewer実在・許可台帳・blind実施の独立確認は運用上の受入事項 |

## 8. 合成検証

[sealed candidate回帰](../../tests/semantic/test_sealed_candidate.py)は、候補不一致、PLAN変更、合成人label、receipt重複、seal/split/二重読み/裁定、分母・Wilson、新指標欠測、原文混入、loopback制約、予測改変、別保存承認不在、重複JSONキー・非有限値、既存実効AI表示との一致を検査する。

[fact-source回帰](../../tests/semantic/test_fact_source.py)は実際の設定書込みと旧token拒否、人承認なしでの拒否を検査する。`candidate_testkit.approved_config` の集計・receiptは全て捏造した単体テスト入力であり、公開用の品質結果でも実データの昇格証拠でもない。

標準の隔離runnerで実行する。実患者データ・原本DB・ローカルLLM・Jevは使用しない。
