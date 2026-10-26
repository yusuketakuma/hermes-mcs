# 封印した抽出候補の評価と昇格契約

本書は 1.0.23 の監査項目11–15に対応する実装契約である。既定の `legacy` は変更しない。候補と証拠が一致しない `canonical` は `legacy` / `mode=off` に戻し、設定エラーを返す。

**ローカル生成と、同一候補の既存監査・最終claims/loops・描画・配送証跡を生成時に封印する経路を実装した。** 外部監査や配送をこのrunnerが実行することはない。証跡なしはdeferred、供給された証跡の欠測・不一致は拒否し、未観測を成功に補完しない。今回、実データ、実LLM、実サービス、実機での昇格は実行していない。

## 1. 正本と構成

| 契約 | 正本 |
|---|---|
| 候補指紋・報告検査・人承認receipt | [semantic_candidate.py](../../mcs/semantic/semantic_candidate.py) |
| 起動時の設定検証・policy指紋 | [semantic_policy.py](../../mcs/semantic/semantic_policy.py) |
| runtime/eval共通の合否判定 | [semantic_acceptance.py](../../mcs/semantic/semantic_acceptance.py) |
| 集計・由来・型検査 | [semantic_evaluation.py](../../mcs/semantic/eval/semantic_evaluation.py) |
| holdout・split・二重読み | [semantic_holdout.py](../../mcs/semantic/eval/semantic_holdout.py) |
| 私有snapshotの候補生成・採点CLI | [semantic_snapshot.py](../../mcs/semantic/eval/semantic_snapshot.py) |
| 既存証跡の生成時封印 | [semantic_snapshot_observations.py](../../mcs/semantic/eval/semantic_snapshot_observations.py) |
| 二重盲検資料と採点の結合 | [semantic_snapshot_blind.py](../../mcs/semantic/eval/semantic_snapshot_blind.py) |
| 新基準 | [g6-criteria-v2.json](../../evaluation/g6-criteria-v2.json) |
| 旧基準（比較用に保持） | [g6-criteria-v1.json](../../evaluation/g6-criteria-v1.json) |

定期処理は `semantic/eval/` をimportしない。共通の合否判定をruntime側に置き、評価CLIがそれを利用する。

## 2. 候補の同一性と旧設定

`candidate_fingerprint(cfg)` は次を束ねる。

- `EXTRACT_VERSION`、`PLAN_VERSION`、`PATIENT_CONTEXT_VERSION`、抽出prompt/schema、ロード時の抽出ソースdigest。
- semantic抽出・監査・描画・projection・設定・合否判定と、実効緊急表示 `structured_view` のロードされたコード・定数。
- `local_llm.resolve(cfg)` のendpoint/model、local_llm設定、semanticのJev model、`POLICY_VERSION`。
- semanticの閾値・calibration・fact_source等の解釈設定、extract設定（`patient_state_context` は未指定とfalseを同一視）、実効 `vital_urgency` 方針。
- Anthropicを利用できる場合のモデル、mode、許可route、患者範囲。自動有効化でも、私有設定から読めるキーの有無で利用経路を区別する。キーの値は指紋に含めない。

昇格証拠そのもの、承認参照、retry/時間budget、通知までの待機時間、semanticのmode、summary/loop mode、semanticのproject scopeは指紋から除く。承認日付だけで解釈cacheを変えない。Anthropicのキー登録・削除で利用経路が変わる場合や、利用中のmode・患者範囲を変える場合は、canonicalの旧承認を再利用しない。必要な再評価・再承認が済むまで、既存の候補不一致の診断と停止条件を保つ。キーを別の値へ交換しても、利用経路とモデル・範囲が同じならキーの値だけで候補を変えない。legacyの既存policy hashを維持し、canonical/shadowのpolicy hashには候補指紋を含める。

定数は配列・辞書・集合・正規表現・タイムゾーンを含め、型と値を安定した形式で束ねる。実行中の表示cacheや失敗回数など、明示した可変状態は除く。未知の定数型を黙って省略しない。この識別範囲の修正前に作成した候補の証拠・承認は流用せず、現候補で再評価・再承認する。

新トークンは完全長の `criteria_sha256:report_sha256:candidate_fingerprint`（194文字）。report hashはJSONファイルの空白を含むraw bytesではなく、`payload_hash(report)` のcanonical JSONを対象とする。起動時とdoctorの設定検査で再照合する。旧 `<criteria version>:<短いdigest>` や任意文字列は通らない。`PLAN_VERSION=1` から `2`、prompt、model、context flag、判定閾値の変更でも以前の証拠は失効する。

ロードコードのdigestにはPython code objectを使うため、Python環境・配置変更でも再評価が必要になる場合がある。指紋はモデルファイルの重み自体のhashではない。同じmodel idの重み差替え検知は、この識別契約の保証範囲外でありモデル資産管理で扱う。

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

`generate` はsnapshotの `records`、`manifest`、`data_origin` を読む。`real_authorized` では `authorization_receipt_sha256`（64桁hex）を要求する。`--authorization "$PRIVATE/authorization.json"` により入力/configと別保存の許可receiptも必須とする。receiptは厳格なキー集合 `schema_version=1/purpose=sealed_candidate_evaluation/data_origin=real_authorized/snapshot_sha256/holdout_manifest_sha256/receipt_id/authorized_by/authorized_at` を持つ。snapshot_sha256はauthorization_receipt_sha256を除いた入力全体、holdout hashはmanifestと照合し、receipt全体のhashを入力のauthorization_receipt_sha256と照合する。未指定・不一致は拒否する。署名・人の本人性・許可発行者の権限を自動認証する仕組みではなく、許可台帳の管理は人運用に残る。合成はこのreceiptで実由来へ昇格しない。

recordsの基本形は既存[評価仕様](semantic-evaluation.md)の識別子とbundleを使用する。各recordは `case_id/account_id/project_id/thread_id/patient_id/split/stratum/data_origin/bundle` を持つ。bundleの `messages` は `message_id/body` と必要な `revision/posted_at/body_state/patient_reports` を持つ。実効AI優先判定にはbundleの `project_type`、必要なら `patient_name` を使う。project_type欠測は患者本人と決めずgroup相当で保留する。生成入力にcandidateやlabelを持ち込まない。

`run.json` は本文を含むrecords、生成されたcandidate/documents、同一bundleのpatient-state off/on出力、候補指紋、snapshot hash、予測全体のhashを保存する。`external_audit_used=false`、`local_only=true` を記録する。**runはmetrics-onlyではないためrepo・CI・一般共有への出力対象にしない。**

生成時の `run_seal_sha256` はrecordsに加え、manifest（reviewer名簿・split registry等）、候補指紋、runの生成元情報、予測hashも束ねる。採点とblindレビュー入口で照合し、生成後の誤変更や別runとの取り違えを拒否する。旧形式のrunは再生成し、既存ファイルへhashだけを後付けして継続しない。同一OS所有者による意図的なhash再計算を防ぐ署名ではない。

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

reviewerの実在や実際に候補名を伏せて読んだことをhashだけで証明するものではない。新runnerのblind-prepare/mergeが方式名を伏せた独立資料と二重読み・裁定の結合を提供する（§7）。資料の個別配布、reviewerの独立性と本人性の確認は人運用である。

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

AI優先は新しい順位付けclassifierではない。`clinical_urgency.message_urgency_details(...).source == 'llm'` による「緊急度高」表示を対象とする。snapshot adapterは同じ生成artifactと本文をメモリSQLiteへ置き、その既存判定を実行する。生の `urgency=high` だけでは本人・臨床・根拠のgateを通ったことにしない。candidate/labelの `ai_priority` はbool限定、抽出失敗は欠測。group、他者、条件的な緊急連絡がAI優先へ昇格しないことを合成テストする。

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

## 7. 項目11–15の受入状況

| 項目 | 実装済み経路 | 人運用・実環境での受入待ち、要件外の保証 |
|---|---|---|
| 11 候補同一性 | 版/prompt/schema/model/設定/実効バイタル方針、tokenと独立承認receipt、不一致legacy/off | 実機配備後の照合は未実施。同一model idの重みhash固定は今回の識別契約外 |
| 12 データ由来 | manifest/record一致、合成人labelでG6不可、別保存の利用許可receiptをsnapshot/holdout/purposeへ結合 | 許可者の権限・本人性は運用確認。署名基盤は提供しない。実データ評価未実施 |
| 13 holdout/レビュー | source封印、split registry、一意receipt、名簿、二重読み/裁定hash、層別下限、blind-prepare/merge | registry完全性・reviewer実在・独立blind実施の確認は人運用 |
| 14 基準/安全指標 | v1保持/v2、分母/Wilson、緊急度/実効AI優先、同一case A/B生成と採点 | A/B採点rubricをレビュー前に人が固定。実データ達成値は未測定 |
| 15 封印runner | repo/CI外local-only生成、同一source/candidate/policy/generationの既存証跡読取り、生成時封印、後付け予測変更拒否、metrics-only報告 | 外部監査/配送をrunnerから実行しない。既存観測の品質と実サービス受領、本番G6は未検証 |

### 私有snapshotの観測adapter

`record.bundle.target_message_ids` に評価対象のmessage IDを重複なしの整数配列で明示できる。全messagesとruntime bundleは文脈として保持してsource fingerprintを照合する一方、facts・A/B・実効AI表示・finals・lifecycleの採点対象は指定投稿だけとする。runtime drainがrootまたは選択targetだけを処理した複数memberのthreadを取り込める。対象外の返信に現世代のfacts/finalsを要求せず、文脈の変更はsource不一致として拒否する。対象集合はholdoutと観測bundle hashに含まれ、採点時の後付け変更を認めない。省略時は従来通り全member対象。空・重複・bundle外のIDは拒否する。

各recordの任意の `sealed_observations` に次を含める。外部DBへの接続や後付けmetricsの入力ではなく、許可されたsnapshotの実artifact/outbox行を、固定schemaのインメモリSQLiteへ読み込む。

- `schema_version:1`、入力bundleの `bundle_sha256`、現在の `candidate_fingerprint` と `policy_fingerprint`。
- `bundle`: runtimeの `thread_bundle` 形。全memberのproject/message/body/revision/posted_atを入力と照合し、`bundle_fingerprint(..., local_model=現在model)` を再計算する。
- `generation_sha256`: 評価対象として生成されたdocumentのdoc配列hash（元bundleのmember順）。保存済みsemantic_facts_v2も、同じ生成docの `_doc_hash` と完全一致する必要がある。
- `artifacts`: artifact_id/project_id/message_id/kind/content/meta/created_at。許可kindはfacts-v2、fact-audit、summary、summary-audit、loop-candidate/event/rejected、request-loop-linkのみ。content/metaはJSON objectで、全行のsource/policy/project/messageを照合する。request-loop-linkは既存保存形式に従いcontent内のsource_fingerprint/policy_fingerprintを使う。
- `notify_outbox`: event_id/project_id/kind/state/payload/progress。semantic_noticeのみ。実際のchunk receiptを既存 `fact_lifecycle` で検査する。`requests`: project_id/request_id/status/revision/title。
- `finals`: 各評価対象につきfinal_id/audit_id/summary_sha256。最新のsummaryと、runtime `_write_result` が同一transactionで直後に保存するauditの連続ID対を照合する。PASSとtarget revisionを要求し、古い対への差替えを拒否する。
- `loop_ids`: snapshot内のloop_candidate artifact IDの昇順全件。`effective_loop_state` で現世代を読む。報告上の完了候補はresolvedへ変換せず、人が結合したrequestが全てdone/cancelledのときだけresolvedとする。

監査は既存fact-auditのdoc hash/PASS/coverageを検査する。verified/rendered/deliveredはそれぞれ独立したreceiptから取り、前段の成功から次段を推定しない。最終claimsは保存済みsummaryを読み、整数fact_refsは同じdocの既存 `project_v2_facts` 順から安定fact IDへ変換する。全claimの参照が検証済みfact集合に含まれなければ拒否する。candidateの属性比較用には、polarity→negation、event_time/valid_time→time、subject/actor→speaker_relation、既存projectionのdrug_ref→medicationを明示的に写す。人labelもこの構造で採点する。

観測を供給しない場合は従来通りdeferredでlifecycle欠測を保持する。供給した場合の欠測・別世代・不正receiptはrun生成を拒否する。配送0件を証明するreceiptは0件として保持し、未配送を成功へ補完しない。生成完了後はcandidateと観測bindingを予測hashに含め、scoreはlabel以外の変更を拒否する。同じOS所有者が全入力とhashを作り直す攻撃を防ぐ署名基盤ではない。

### 新runnerの二重盲検資料

```sh
python3 mcs/semantic/eval/semantic_snapshot.py blind-prepare \
  --input "$PRIVATE/run.json" --config "$PRIVATE/candidate-config.json" \
  --reviewers reviewer-one reviewer-two --output "$PRIVATE/review-pack.json"

python3 mcs/semantic/eval/semantic_snapshot.py blind-merge \
  --input "$PRIVATE/run.json" --config "$PRIVATE/candidate-config.json" \
  --key "$PRIVATE/coordinator-key.json" \
  --reviews "$PRIVATE/review-one.json" "$PRIVATE/review-two.json" \
  --output "$PRIVATE/reviewed-records.json"
```

prepare出力は `worksheets`（2名分）と `coordinator_key` を含む私有packである。管理者は各worksheetだけを各reviewerへ個別配布し、keyとpack全体は渡さない。方式のA/B順序とreview_idはreviewerごとに無作為化し、source/candidate/arm内容の変更をmergeで拒否する。本文のPII匿名化を保証する資料ではない。

人が各caseのhuman_labelsに通常gold label（source=human、本人のreceiptを含む）、scores {A,B}、独立したreview_receipt_id、blind:trueを記入する。adapterがreceiptや採点を捏造することはない。不一致は別reviewerの裁定documentを `--adjudications` に指定する。裁定documentは第一worksheetの全caseをコピーしてreviewerを第三者に変更し、その人のhuman_labelsを記入する。merge結果を既存scoreの--reviewedへ渡す。全出力は0600・新規排他的作成、repo/CI外のみ。

完全合成200caseのgenerate→receipt読取り→score回帰で、全必須metricの分母と数値閾値が充足可能であることを確認する。合成由来と人レビュー不足によってG6は不合格を維持する。これは実データG6達成の報告ではない。

## 8. 合成検証

[生成時観測回帰](../../tests/semantic/test_snapshot_observations.py)と[盲検資料回帰](../../tests/semantic/test_snapshot_blind.py)は、source/candidate/policy/generation/doc不一致、監査・配送欠測、改変、私有CLI出力、許可receiptと全metric観測を検査する。

[sealed candidate回帰](../../tests/semantic/test_sealed_candidate.py)は、候補不一致、PLAN変更、合成人label、receipt重複、seal/split/二重読み/裁定、分母・Wilson、新指標欠測、原文混入、loopback制約、予測改変、別保存承認不在、重複JSONキー・非有限値、既存実効AI表示との一致を検査する。

[fact-source回帰](../../tests/semantic/test_fact_source.py)は実際の設定書込みと旧token拒否、人承認なしでの拒否を検査する。`candidate_testkit.approved_config` の集計・receiptは全て捏造した単体テスト入力であり、公開用の品質結果でも実データの昇格証拠でもない。

標準の隔離runnerで実行する。実患者データ・原本DB・ローカルLLM・Jevは使用しない。

二重読み資料は各投稿の評価対象/文脈のみを区別し、元の投稿IDは表示しない。候補の内容をA/B出力と照合すると設定armを推測できる場合があり、資料生成だけでは実際の盲検を保証しない。一致済みの二重判断は第三者裁定で置き換えず、不一致の項目だけを裁定する。
