# Semantic offline evaluation (WP08)

`mcs/semantic/semantic_evaluation.py` evaluates a fixed semantic bundle and a
candidate result against a reviewed label. It is an offline JSONL CLI. It does
not import the model client, open an attachment, read a bundle path, or send a
request. Successful runs write one aggregate JSON report and nothing to
stdout. Validation errors contain only a line/field code; source text is never
printed.

Run it with explicit versions and acceptance criteria:

```sh
python mcs/semantic/semantic_evaluation.py \
  --input cases.jsonl \
  --manifest manifest.json \
  --criteria criteria.json \
  --output report.json
```

The manifest fixes the input versions. The minimum fields are:

```json
{
  "version": "manifest-v1",
  "bundle_version": "bundle-v1",
  "candidate_version": "candidate-v1",
  "label_version": "label-v1"
}
```

Each JSONL line is one case. `case_id`, `split`, `account_id`, `project_id`,
and `thread_id` are required. `split` is `dev`, `calibration`, or `test`.
The same account, project, or thread may not occur in more than one split. A
bundle and candidate must carry the versions named by the manifest.

Attachment entries are metadata only. `path` must be relative to the fixture
root chosen by the caller; the evaluator never checks whether it exists or
opens it. `context_before` can identify the preceding message context used by
the fixed bundle. Candidate `attachment_refs` are checked against the bundle's
attachment IDs, so an attachment path cannot silently become a new evidence
source.

A label has `source: "human"` or `source: "synthetic"`, a `facts` list, and a
`loops` list whose entries use `loop_id` and boolean `resolved`. Fact IDs are
the matching key. Label facts own `important`, medication, negation, time, and
speaker relation fields. Candidate facts and claims use `fact_id`/`fact_refs`.
The label must annotate every candidate claim with the
same `claim_id`, plus `critical`, `supported`, `final`, and
`covered_gold_fact_ids`. Final recall uses only covered facts from human
supported final claims; critical overclaim uses human critical claims marked
unsupported. Candidate critical flags, fact references, and any
`final_fact_ids` metadata are never treated as truth.

The criteria file fixes the gate. `required_metrics` defaults to all WP08
metrics, but keeping it explicit is recommended:

```json
{
  "version": "criteria-v1",
  "required_metrics": [
    "important_fact_recall", "final_recall", "critical_overclaim",
    "medication_recall", "negation_recall", "time_recall",
    "speaker_relation_recall", "loop_conformity",
    "loop_false_resolution", "loop_precision", "loop_unresolved_miss_rate",
    "defer_rate"
  ],
  "minimums": {
    "important_fact_recall": 0.90,
    "final_recall": 0.90,
    "medication_recall": 0.90,
    "negation_recall": 0.90,
    "time_recall": 0.90,
    "speaker_relation_recall": 0.90,
    "loop_conformity": 0.90,
    "loop_precision": 0.90
  },
  "maximums": {
    "critical_overclaim": 0.0,
    "loop_false_resolution": 0.0,
    "loop_unresolved_miss_rate": 0.0,
    "defer_rate": 0.20
  },
  "required_splits": ["test"],
  "min_human_labels": 1
}
```

Every rate reports `correct`, `errors`, `denominator`, `rate`, and a 95%
Wilson interval. The report also contains split and overall results for
important-fact extraction recall, final recall, critical overclaim, medication,
negation, time, speaker relation, loop conformity, false loop resolution, and
defer rate. Latency reports p50/p95 in milliseconds; usage reports case count,
missing count, totals, mean, p50, and p95 for requests and token counters.

The gate is false when human labels are insufficient, a required split is
empty, a required metric or telemetry section has a zero denominator, a
threshold is missed, or any critical overclaim is present. `g6_eligible`
additionally requires every case label to be human. The current task has no
real human labels, so synthetic fixtures and their passing unit tests do not
constitute G6 or a production
quality claim; a reviewed label file and approved criteria must be supplied
later. Thresholds are evaluated against the held-out `test` split; the
aggregate report is descriptive and cannot hide a test failure.

The focused regression is run through the isolated project runner:

```sh
MCS_TEST_PYTHON=/path/to/python scripts/run_tests.sh \
  tests/semantic/test_semantic_evaluation.py
```

Loop conformity uses the union of labeled and predicted loop IDs as its denominator. Matching resolution states count as correct; omitted labeled loops and extra candidate loops count as errors. This is set agreement, not precision alone. Label loops must enumerate the expected set for the fixed bundle.

Report schema v2 adds `loop_precision` (correct ID and resolution state / predicted loops) and `loop_unresolved_miss_rate` (gold unresolved loops omitted or wrongly resolved / gold unresolved loops). Both retain denominators, error counts and Wilson intervals. Zero denominators cannot pass the gate; thresholds remain the operator’s explicit criteria. Example numbers above are illustrative, not approved production criteria.

Held-out gate eligibility requires latency, request count, input tokens and
output tokens for every test case. A missing case cannot disappear from the
cost/latency denominator just because another case has telemetry. Missing
values remain visible in descriptive aggregates and produce
`telemetry_missing:<field>` gate reasons. Optional total_tokens is not
required when its input/output components are available.

### 実行時の計測値

`run_due` の結果（巡回JSONの `semantic`）は、OFF以外で次を返す。

- `elapsed_s`: semantic drain全体の実測時間。巡回全体の `elapsed_s` とは別の範囲。
- `oldest_pending_job_age_s`: drain開始時の最古pending jobの経過秒数。未来の再試行待ちも含み、pendingなしはnull。
- `job_metrics`: 実際に処理を開始したjobごとの `job_id`、`project_id`、`generation`、`status`、`elapsed_s`、開始時の `job_age_s`、`jev_requests`。捕捉した例外もerrorとして含む。開始前に予算・停止条件で保留したjobは含まない。

経過時間は単調時計で測定する。本文・氏名・認証値は追加しない。jobの世代を確定・更新する証拠ではなく、staleを含む実行観測値である。要求数は既存の予算台帳と同じ実要求差分を用い、台帳への二重加算はしない。

この出力だけでは患者単位/run単位のp50/p95評価は完成しない。同じ患者の複数jobの合計は意味処理に費やした時間であり、取得・待機・通知を含む患者全体の遅延ではない。プロセス強制終了で結果JSONが出なかった実行の計測値を復元するものでもない。API token使用量と評価器への集計方法は下記の節を参照。

### run JSONLのオフライン集計

評価CLIへ `--runs runs.jsonl` を追加すると、出力の `runtime` に実測値のp50/p95・合計・観測数・欠測数を追加する。各行は `{"account_id":"評価用account識別子","result":{...run_checkの結果JSON...}}` とする。account IDはエクスポート側で明示し、異なる台帳の同じrun IDを混同しない。同一account/runの重複行と同じrun内の重複jobはエラーにする。

`run_elapsed_s` は巡回全体、`semantic_elapsed_s` は意味処理drain、`patient_semantic_work_s` はaccount/run/project内で開始したjob処理時間の合計を観測単位とする。患者の受付から通知までの遅延ではない。`patient_jev_requests` と `run_jev_requests` は意味処理で観測したJev要求数だけを集計する。開始前に保留した患者数は出力から確定できないため、患者別欠測数はnullとし、未観測を0秒として補完しない。最古job ageはdrain開始時の値を集計する。

この統計は性能観測の補助であり、意味品質のG6合格条件を置き換えない（`gate_evidence: false`）。token使用量は下記の方法で集計する。強制終了したjobの計測復元と、患者の取得・待機・通知を含む遅延評価は未完了。

### Jev token観測

job_metricsの `usage` は、検証済み応答の `input_tokens` / `output_tokens` の合計、`reported_requests`、要求数との差分 `unreported_requests` を持つ。通信失敗・不正応答の使用量は不明であり、token合計はその場合には下限にすぎない。応答検証後に世代変更等で結果が不採用となっても、返却済みusageは計上する。任意のtotal_tokensは補作しない。これはプロセス内で観測できた使用量であり、課金明細や強制終了後の復元を保証しない。評価CLIの患者/run別token集計は次節のとおり。

### 患者/run別token集計

`--runs` の `runtime.jev_usage` に患者別・run別の観測を集計する。`observed_total` は返却済みtokenの合計で、不明要求を含む場合は総使用量の下限である。`complete_p50` / `complete_p95` は全要求のusageが揃った観測だけを対象とし、その分母と不完全観測数を併記する。成功応答だけの分布には選択の偏りがあり、全実行の性能と同一視しない。

`reported_requests` / `unreported_requests` / `missing_jobs` と、job_metricsのない `unobserved_runs` を示す。usageのない旧jobは欠測とする。要求数とusageの内訳が一致しない入力、応答0件なのに正のtokenを持つ入力は拒否する。患者単位と測定範囲は時間集計と同じであり、課金照合や強制終了時の復元は引き続き範囲外。

### 3方式の盲検配布資料

`python3 mcs/semantic/semantic_blind.py --input comparison.jsonl --output-dir new-packet`

各JSONL行は `case_id`, `account_id`, `project_id`, `split`（dev/calibration/test）, `bundle_fingerprint`, `source_text`, `outputs` を持つ。outputsはbaseline/assisted/auditedの3キーで、それぞれ `{"bundle_fingerprint":"同じ固定入力の指紋","text":"比較対象の出力本文"}` を渡す。

新規ディレクトリにworksheet.jsonlとcoordinator-key.jsonlを作る。評価者へはworksheetだけを渡し、keyは管理者が保持する。ファイルは0600、ディレクトリは0700で作成し、既存ディレクトリへ上書きしない。方式名・実行metadataを評価票へ含めず、ケース毎にA/B/Cの並びをランダム化する。本文の語調から方式が推測される可能性は残る。human_labelsはnullであり、自動正解ラベルは生成しない。

入力が申告する指紋一致を検査するが、生成来歴を証明するものではない。既存DBからの生成は後述の明示的snapshotモードを使う。同一固定bundleからのbaseline生成と出力の来歴照合が済んだデータを用いる。未監査/要確認出力を含み得る研究評価用資料であり、臨床利用向けの監査済み表示ではない。作成失敗時は資料を配布せず、別の新規ディレクトリで作り直す。

`semantic_blind.fixed_bundle_outputs(bundle, target_id, candidate, final, baseline_fn)` は保存bundleの指紋を再計算し、対象revision/project、候補/最終artifactのpolicy/mode/stageを照合してから比較本文を作る。candidate/finalはcontent/metaをJSON decodeしたartifact行を渡す。baseline_fnへは固定bundle内の対象本文だけを渡す。実行側が既存 `extract_llm.llm_extract` を明示的に渡せば既存prompt・3000文字上限・validatorを再利用するが、このヘルパー自身はモデル接続を選択しない。baseline生成失敗は比較不能とする。NEEDS_REVIEWの最終候補も品質評価に含めるが、監査合格へ変換しない。

このヘルパーは後述のsnapshot CLIから呼ばれる。実モデルでの生成は未実行。評価用本文はsummary/pointsまたはclaims/limitationsを同じ改行形式にし、方式名の接頭辞を追加しない。

### snapshotからの明示的生成

`python3 mcs/semantic/semantic_blind.py --input selections.jsonl --snapshot evaluation.db --generate-local-baseline --output-dir new-packet`

このモードの各入力行はcase_id/account_id/project_id/splitと、正の整数bundle_id/candidate_id/final_id（artifactsのID）を指定する。SQLiteはmode=roで開き、1つのread transactionで選択結果を取得して閉じてから生成する。全ケースの来歴・splitを先に検査し、既存ローカルllm_extractだけでbaselineを生成する。Jevは呼ばない。出力先を先に新規作成するため、既存資料への再実行はモデル呼出し前に拒否する。

account_idは台帳の運用上の識別子を明示する（SQLiteから外部アカウント認証を立証しない）。このモードは対象本文を既存loopbackモデルへ送るので、評価対象を限定した承認済みsnapshotでのみ実行する。今回の検証は一時DBと合成baselineであり、実患者本文の生成は実行していない。

snapshotモードの評価票source_textは、対象message IDと、親返信関係・revision・投稿時刻・投稿者ID/種別/職種・本文状態・原文を含む整形JSONである。本文の単純連結で対象や発言者を失わない。方式名・監査結果は原文文脈へ追加しない。添付ファイル自体を読み出したり、添付内容を解析済みとして扱うものではない。

管理者対応表にはsnapshot選択元のbundle_id/candidate_id/final_idと、ラベル未入力の初期評価票のSHA256も保持する。hashはUTF-8、ensure_ascii=false、sort_keys=true、separators=(",", ":")で正規化したJSONに対する値。後で評価票と管理者対応表の取り違えを検出するためのもので、電子署名や人手ラベルの真正性証明ではない。元のsnapshot・selectionと初期評価票を保存し、入力済みラベルとは区別して管理する。

### 人手記入後の方式復元

評価者はworksheet各行のhuman_labelsだけを、A/B/Cをキーとした空でない評価objectへ変更する。例 `{"A":{"notes":"要点欠落あり"},"B":{"notes":"…"},"C":{"notes":"…"}}`。具体的ラベル項目・基準は評価開始前に固定する。

`python3 mcs/semantic/semantic_blind.py --input completed.jsonl --unblind-key coordinator-key.jsonl --output-dir new-reviewed`

reviewed.jsonlに方式別の記入値を保存する。元評価票のhashをhuman_labels=nullとして再検査し、原文/選択肢変更、review IDの重複/欠落、未記入、方式対応表の不正を拒否する。model/DBには接続しない。この段階は対応の復元だけであり、記入者が人である証明、記入内容の妥当性、semantic_evaluationへの品質ラベル変換、合否判断は行わない。

評価reportにはcriteria_versionに加え、正規化済みcriteria全体とcriteria_sha256を含める。任意の余剰キーではなく、実際にgateへ適用した閾値・対象指標・必要split・最小人手件数を固定する。同じ版名のファイルが書き換わっても内容差を検出できる。正式な初期基準は `evaluation/g6-criteria-v1.json`。このhashは評価開始前に固定したことの時刻証明にはならないため、評価計画とともに元ファイルを保管する。

### 方式を伏せた主張ID

評価票の各choiceにclaims（claim_id/text）とlimitationsを同じ形式で表示する。IDは各choice内でc1,c2…とし、元モデルやartifactの識別子を露出しない。snapshotモードではbaselineのsummary/points、補助/監査後のclaimsを保持し、limitationsを主張へ混ぜない。改行を含む主張も分割しない。

既生成テキスト入力で細分化する場合はoutputにclaim_textsとlimitationsを指定し、両者を改行結合した値がtextと一致する必要がある。省略時は本文全体をc1として扱う。これは人が決める原子的な事実分解を代行するものではない。human_labelsのclaimsには、そのchoiceのclaim_idを使う。定量評価への構造化facts/loopsの対応は引き続き必要。

### 品質評価JSONLへのラベル結合

`python3 mcs/semantic/semantic_blind.py --input completed.jsonl --unblind-key coordinator-key.jsonl --evaluation-records candidate-records.jsonl --manifest manifest.json --method audited --output-dir new-labelled`

`evaluation.jsonl`を品質評価器へ渡せる。入力candidate-recordsは既存評価schemaの未ラベルrecordで、bundle.fingerprintを比較資料と一致させ、candidate.claimsのclaim_id/textを表示choiceと同一順序にする。評価票作成時に各outputのevaluation_candidateへcandidate全体を渡しておく必要がある。facts/loops/statusは評価票のpredictionsにも表示し、usage/latency/versionを含む全candidateのhashを管理者キーに保存する。結合時に全体一致を要求し、補作や差し替えを受理しない。本文だけで作った旧評価票やsnapshot比較票は定性比較用で、定量結合には使用できない。

completedの各human_labels[A/B/C]には自由記述だけでなく評価器のlabel schema（明示source/version/facts/claims/loops）が必要。全対象集合・account/project/split・bundle指紋・主張本文を照合し、元ラベルがあるrecordは上書きしない。既存validatorで構造を検査する。syntheticはsyntheticのまま保持し、humanへの自動昇格はしない。人間による記入の真正性と、固定前の構造化factsの生成来歴は別途確認が必要。


### 定量評価の実行順序

1. 許可済みの固定入力・3方式の実出力から、方式ごとの未ラベルcandidate-recordsを準備する。計測値の欠測を0で埋めない。
2. comparison.jsonlの各outputへ対応candidate全体をevaluation_candidateとして含める。claim_textsの順番をcandidate.claimsと揃え、IDはc1,c2…にする。原文・候補・方式対応を保存する。
3. 次の最初のコマンドで評価票を作り、本人がworksheetのhuman_labelsだけを記入してcompleted.jsonlとして保存する。
4. 残りのコマンドでラベルを結合し、初期基準v1による結果を作る。他の方式も別の出力先と対応candidate-recordsで評価する。

```sh
python3 mcs/semantic/semantic_blind.py --input comparison.jsonl --output-dir new-packet
# 人手記入後に以下を実行する。completed.jsonlは自動生成しない。
python3 mcs/semantic/semantic_blind.py --input completed.jsonl \
  --unblind-key new-packet/coordinator-key.jsonl \
  --evaluation-records audited-records.jsonl --manifest manifest.json \
  --method audited --output-dir new-labelled
python3 mcs/semantic/semantic_evaluation.py --input new-labelled/evaluation.jsonl \
  --manifest manifest.json --criteria evaluation/g6-criteria-v1.json \
  --output audited-report.json
```

これらのファイル名は入力例であり、実人手ラベル入りのデータを同梱したという意味ではない。snapshotモードが返す定性比較資料に架空の構造化factsや計測値を補って定量評価へ進めない。
