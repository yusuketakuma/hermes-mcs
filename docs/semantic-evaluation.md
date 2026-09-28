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
`loops` list whose entries use `loop_id` and boolean `resolved`. A human label
must also bind a durable labelling `receipt` (`receipt_id`, `labelled_at`,
`reviewer`); the validator checks its shape, while authenticity stays the
review process's job — a bare `source` string cannot mint provenance. Fact IDs
are the matching key. Label facts own `important`, medication, negation, time, and
speaker relation fields. Candidate facts and claims use `fact_id`/`fact_refs`.
The label must annotate every candidate claim with the
same `claim_id`, plus `critical`, `supported`, `final`, and
`covered_gold_fact_ids`. Final recall uses only covered facts from human
supported final claims; critical overclaim uses human critical claims marked
unsupported. Candidate critical flags, fact references, and any
`final_fact_ids` metadata are never treated as truth.

`delivered_fact_ids` continues the verified -> rendered -> delivered chain:
mandatory gold facts absent from the candidate's delivered ID set score
against `delivered_fact_recall`, so a fact lost between render and transport
fails the same way a dropped render does. An absent list scores as nothing
delivered rather than being skipped.

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

`semantic_blind.fixed_bundle_outputs(bundle, target_id, candidate, final, baseline_fn)` は保存bundleの指紋を再計算し、対象revision/project、候補/最終artifactのpolicy/mode/stageを照合してから比較本文を作る。candidate/finalはcontent/metaをJSON decodeしたartifact行を渡す。baseline_fnへは固定bundle内の対象本文と、同じスレッドの先行投稿から組み立てた文脈を渡す。実行側が既存 `extract_llm.llm_extract` を明示的に渡せば既存prompt・3000文字上限・validatorを再利用するが、このヘルパー自身はモデル接続を選択しない。baseline生成失敗は比較不能とする。NEEDS_REVIEWの最終候補も品質評価に含めるが、監査合格へ変換しない。

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

## 要約v4次期推論エンジン統合実装計画（段階実装中）

この節は既存のG6評価契約を変えず、次期**推論エンジンv4**の実装・検証・切替条件を定める。七領域と要約刷新を一つにしたタスク、依存関係、障害復旧、Discord/Slackのスレッド配送、導入更新、外部連携の詳細は、Hermes作業場の `/Users/yusuke/.hermes/hermes-agent/.omo/plans/mcs-seven-domain-reliability.md` のIS-1〜IS-8 / Todo 1〜20を正本とする。ここではv4の臨床品質・速度・排他・版移行を読み切れる契約にする。計画段階であり、実MCS、患者データ、ローカルLLM、Jev、Discord/Slack、本番DBや稼働設定にはアクセスも変更もしない。

### 計画作成時の制約と区別する版

以下の表は2026-09-25の計画作成時点の課題を記録したもの。現在の実装には修正済みの項目も含まれる。移行処理の現時点の保留条件は後述の「実装上の保留条件（2026-09-27）」を参照する。

| 現行の契約 | 確認した制約 | v4での決定 |
| --- | --- | --- |
| 旧`extract_llm.EXTRACT_VERSION=3`（出力JSON形はv2） | 抽出・QC・viewの選択、失敗再試行に世代番号を使用。単純に4へ上げると旧投稿が再候補となり、混在workerは結果を3→4→3へ逆行させ得る。旧kindの置換保存は旧v3行も削除する（`mcs/extract/extract_llm.py:54,1251-1290,1610-1668`）。 | v4 PASSまでは旧結果を明示的な現行版として保持し、**別の保存境界**に`engine_version=4`と公開派生の`extract_version=4`を記録する。対象ごとの安全な切替後は旧LLM生成成果物を上書きできるが、旧抽出定数だけをin-placeで変更しない。 |
| canonical facts `semantic-facts/v2` / semantic artifact schema `2026-09-20` | これらはエンジンv3/4と独立。現行の事実生成→別の要約生成→Jev監査→条件付き全文修正は複数呼出し（`mcs/semantic/semantic_drain.py:235-337,659-749`）。 | evidence、facts、claim、通知を一つの**世代付きjob/receipt列**で結ぶ。現行fact schemaを無条件に改版しない。 |
| 旧PASSのcache / 出力 | `policy_fingerprint`に`fact_source`がなく、切替後に古い要約を再利用し得る（`mcs/semantic/semantic_policy.py:160-163`）。確認済みfactは40件で切られ、通常の通知追記は`mandatory_facts`を読まない（`mcs/semantic/semantic_render.py:33-83`; `mcs/semantic/semantic_send_gate.py:334-393`）。 | fact source・engine・生成入力・修復結果・依存relation世代をcache/公開の照合対象にする。専用通知と通常/返信表示の両面でverified fact ID集合を全件追跡し、無言の件数省略を禁止する。 |

旧`extract_llm`とcanonical経路を単に足して呼ぶのではなく、v4の生成を一つのjobとして段階化する。「一つの推論ロジック」は1本の無制限promptではない。現行`semantic_llm`には28,000文字超の要約入力をstubにする境界があり（`mcs/semantic/semantic_llm.py:198-245`）、長文は原文の所有範囲と重複文脈を区別したbounded chunkで網羅する。既存の3,000文字単位の耐久checkpointを失わず、chunk完了集合が原文の全範囲を覆わなければPASSにしない。

### 一次推論から自己修正・公開まで

1. **S0/S1 準備・一次推論**: `extract_v1`の確定的ヒント、atom/chunk境界、原文revision/添付完全性を固定。ローカルLLMでchunkごとに根拠span付きfactsと初期claim候補を生成し、出力長・JSON・全chunk完了と`semantic-facts/v2`を検証する。空・打切り・open obligation・欠けた入力は対象chunkと理由をPENDING/NEEDS_REVIEWへ残す。
2. **S2 二次評価**: 検証済み候補についてJevがfact→固有根拠と原文→factsのcoverageを判定し、根拠/field/主語/時点/否定の不一致をID付きfindingsとする。Jevは事実の削除・書換え・欠落を正当化しない。budget不足・不正応答・未評価時はPASSに進めず、既存OFF/shadow/assist/enforceと外部PHI送信の明示設定を維持する。
3. **S3/S4 必要箇所の二次ローカル修復と再監査**: schema/evidence/coverage/Jev findingsが指すatom・factだけ修復し、採用済みfactを固定する。モデル呼出し**前**に世代と入力digestに結んだ`started` receiptを耐久予約し、修復文書を検証し直してJevで再監査する。事実修復は世代ごと最大1 dispatch。停止後も回数を戻さず、2回目の不合格はNEEDS_REVIEW。修復なしの完全なS2 PASSは不要な再問をしない。
4. **S5/S6/S7 事実集合・可読要約・公開監査**: Jev PASSで検証された全facts/relationsから必須情報を**確定的に全件描画**する。概説は最終factsだけからローカルLLMで生成し、claim参照とJev公開監査を行う。誤主張だけに別の上限付き要約修復を行い再監査する。事実セット不足や無根拠claimは「要約済み」とせず、原文と未完了理由を明示する。事実修復と要約修復のreceipt/予算は別。
5. **S8 診断保存とv4公開**: `PASS|PENDING|NEEDS_REVIEW|STALE`の診断receiptを全件保存する。**PASSかつ現行source/relation/activationに一致する場合だけ**、v3形の読取用投影を独立kindから公開し、対象ごとの現行版ポインタを原子的にv4へ切り替える。非PASSを`extract_llm`の現行抽出としてmintしない。現行`mcs_queries.current_extract_pred`はaudit statusを見ないため、`extract_version=4`だけを付けた非PASS行は読めてしまう。

Jevの`Choice`は同じ`state`に複数設問を送れるが、現行fact/claim監査は対象ごとに**異なる根拠state**を構築する（`mcs/semantic/semantic_audit.py:76-160,284-369`）。全claimの無条件一括化は根拠混入の危険がある。完全に同じ順序の根拠集合を持つ組だけを任意の最適化候補とし、単独方式と合成stub契約・実モデル品質をそれぞれ比較するまでは既存の分割監査を維持する。要求数は対象件数、coverage、再試行で変わり、2回/投稿などの定数ではない。[TypeSafe Choice](https://docs.typesafe.ai/primitives/choice.md)、[citation check](https://docs.typesafe.ai/cookbooks/citation_check.md)、[model limits](https://docs.typesafe.ai/models.md)に従い、日次予算の既定OFFを無断で有効化せず、送信する臨床文脈を既存許可範囲より拡大しない。

表示・配送では`verified_fact_ids == rendered_fact_ids == delivered_fact_ids`を監査対象単位で要求する。物理文字数を超える場合は既存の分割/再開機構を使い、40件目・41件目・120件目、および本文の第5part以降を切らない。カード、スレッド本文、添付、通常追記と専用semantic通知は異なる表面なので各々のsource世代/part ID/bytes/hash/配送状態を照合し、未送・結果不明を完了と偽らない。生の新着通知をsemantic処理待ちで遅らせない。

### 旧v1・v2・v3の推論成果をv4へ順次置換する

**チャット本文・原文revisionは保存し続ける。** 上書きできるのはLLM推論後の生成成果物であり、原文、出典、受入済み配送receiptを消す指示ではない。`extract_v1`の規則成果物、旧`extract_llm`世代、canonical投影を種類別に棚卸しする。旧JSONの`schema v2`と`semantic-facts/v2`は**データ形式名**で、旧推論エンジンv2の存在や全対象の未移行を意味しない。v1/v2/v3として実在する旧推論成果を、source/revision/既存artifact IDと依存読取先で列挙する。

旧版を無制限に一度に再推論せず、対象・依存閉包・総call/token/retry予算・期限・再開cursorを固定した有限cohortを順番に処理する。v4を同じ原文に対して生成・Jev監査し、全必須factsと旧版だけが供給していた情報（例: `extract_v1`由来の薬剤期間）を新readerで表現できると確認してから、対象ごとの現行選択を**原子的にv4 PASSへ置換**する。`extract_v1`の規則成果物はLLM生成ファイルの上書き許可とは別扱い。旧workerの遅延保存は世代fenceで拒否し、旧QC・通知・集計のartifact参照を移行または終端確認する。旧QCの判定をv4への監査成功として付け替えず、旧成果物IDを参照する必要があればメタデータだけのtombstone/監査receiptへ移す。旧LLM生成payload/ファイルの物理的な上書き・削除は別の有限cleanup manifest（対象ID、source世代、`retire_after_at`、検証済み復旧手段を固定）で、参照切替と復旧用保持期間が過ぎた場合だけ許す。manifestや復旧確認が無ければ自動削除しない。最低限の版・source hash・判定・置換先ID・処理時点を示す監査receiptは残し、チャット本文は対象外にする。

v4がPENDING/NEEDS_REVIEW、原文変更中、Jev未評価、旧参照未移行ならその対象の旧成果を上書きせず、旧版と未完了理由を表示する。cohortを繰り返して旧版の現行選択を減らし、残数・最古滞留・失敗理由を可視化する。「全件v4完了」は未処理旧版や未解決例外が残る間は宣言しない。旧生成payloadを既に上書きした対象で問題が出た場合、存在しない旧ファイルへ黙って戻さず、保存済み原文から許可済みの有限再処理または原文+pendingへ移る。原文の長期保存に必要なバックアップ/復元可能性は別の運用検証対象であり、現行のローカル日次backupだけで端末喪失への耐性を保証しない。

**実装上の保留条件（2026-09-27）**: `semantic_v4.run_cohort` は原文・依存閉包を固定し、ジョブと受付receiptを同一transactionで保存する。件数上限はcohort全期間で共有し、再起動で補充しない。cohortはv3抽出器への受付許可を与えない。現在のJev adapterには送信前のtoken上限制御がないため、cohort由来の解析ジョブはモデル呼出し前に `token_budget_not_enforceable` を記録して停止する。通常の新着解析の経路とは別の保留条件であり、総call/token/retry予算の強制を実装済みとは扱わない。`retire_payloads` も、別のcleanup manifestと復旧検証を受け取る実装がない間は `cleanup_manifest_and_recovery_required` として旧payloadを保持する。変換receiptとv4 PASSだけでは物理削除を許可しない。

### 実測で選ぶスロット数とバックログ/リアルタイムの厳密な非重複

現行テンプレートはllama-server `-np 2 -c 65536`で、複数枠は同じdecode batchを共有する（`deployment/launchagents/ai.mcs.llamaserver.plist`; [llama.cpp server batching](https://github.com/ggml-org/llama.cpp/blob/d81aef19941e145d04f88fb180ea89a67d052ab5/tools/server/README-dev.md#batching)）。**3枠を必須値にしない**。1枠・現行2枠・3枠をまず同じ入力と排他条件で比較し、4枠以上もコンテキストとメモリが成立する場合に候補とする。総コンテキスト65536を3枠へ均等割りするなら算術上約21,845token/slot、従来の約32,768token/slotを維持するには総量98304と追加KVメモリが必要だが、実際の`n_ctx`/KV配置は稼働するbinaryで確認する。本機は`hw.model=Mac16,10`、搭載メモリ16GiBと確認したM4 Mac miniで、Apple公称120GB/sはこの処理の実効帯域ではない（[モデル識別](https://support.apple.com/en-us/102852)、[技術仕様](https://support.apple.com/en-us/121555)）。

本番には全ての実際の接続元（MCS、Hermes主処理/補助/委譲/cron、GBrainを含めて検出した経路）を分類する**共通の強制受付境界**が先に必要。MCSの自動新着抽出は「新着優先BACKLOG」であり、人が待つ対話RTとは区別する。classは認証済み経路と操作種別で決め、clientの自己申告だけでRTへ昇格させない。backend直結・旧URL・未分類・fallbackを拒否できない場合、MCSだけのslot固定では非重複を保証できずv4の本番切替はNO-GOとする。受付世代epochと要求の終端を耐久記録し、`O_BACKLOG * O_RT = 0`を**送信前予約からbackend終端確認まで**維持する。server内の待機要求、取消し待ち、結果不明、Jev/QC中のjobも反対classへの切替を許さない。RT到着と新規背景受付の停止を原子的に行い、背景の自然完了または旧backend終了の確認**後**にRTを送る。stock serverへcancelを送っただけ、timeout・切断・`/slots`の一瞬の空きでは終了とみなさない。controller/backend再起動・旧epochの要求・結果不明は双方を閉じて照合する。RT継続流入中にも背景progressを保証するには実測で長さを決めた背景保護時間帯が必要だが、その間のRTは待機/延期し、排他を緩めない。滞留年齢・受付待ち・資源不足を明示する。

候補枠数はいずれも同一class内の並列処理だけに使う。認可された隔離環境で実モデルを**同じworkloadと排他条件**で比較し、実RAM/KV/swap、per-slot`n_ctx`、入力の完全性、tokens/s・確認済み完了件数/h、取得→確認済み表示のp50/p95、RT受付待ちと品質を測る。まず欠落・OOM・実害のあるswap・品質低下・全巡回p95<15分違反・RT p95回帰のある候補を除外し、残りからRT遅延と検証済み処理容量を比較して採用数を決める。3枠が劣れば1枠または2枠を採用でき、測定で4枠以上が安全かつ優位ならそれも選べる。syntheticサーバ試験で排他を証明しても実機容量を証明しない。配備時は採用した枠数、設定・測定receiptと2枠など検証済みの戻し先を固定する。実測・稼働設定変更・外部Jev呼出しはこの文書作業では実行しない。

### 段階導入と合格条件

1. **契約・移行**: 実装時には合成temp DBで旧v1/v2/v3の実在する成果物+QC→v4 PASS/失敗→旧worker遅延完了→現行版切替→旧生成payloadの上書き→rollbackを実行し、原文・既発行receiptを保ったまま非PASSや遅延旧結果がv4のcurrentを覆わないことを示す。新保存契約を知らない旧drainer/tick/手動workerを停止・退役させてから共存を始める。v3エンジンによる履歴の新規推論受付は既定0、旧成果物の**v4への再解析**は対象source/依存閉包と期限を固定した有限cohortで順次進める。総LLM/Jev要求・token・再試行上限、QC遅延高水位、停止/再開cursorを永続化し、再起動/日付変更で補充しない。旧履歴全件の処理完了はv4新着移行の前提にしないが、未移行履歴に依存する患者現在値を「完全なv4」と表示しない。旧PASSのfact-source切替は新しい内部fingerprintを要求する。
2. **shadow比較**: v4は新着・許可された有限backfillだけを処理し、visibleな旧版選択と通知を変えずに、S0–S8のreceipt、全factの描画、通常/専用通知の合成結果を比較する。Jev未設定なら勝手に許可や予算を付与せず、未評価をPASSにしない。途中停止、予算不足、入力変更、旧worker混在、41/120件、本文第5part、候補枠数・複数クライアントの競合を合成故障注入する。
3. **品質・性能ゲート**: `evaluation/g6-criteria-v1.json`のheld-out testで人手ラベル**200件以上**、`mandatory_fact_recall=rendered_fact_recall=evidence_closure=1.0`、`silent_drop=critical_overclaim=loop_false_resolution=0`、既存のrecall/precision/defer閾値を一つも緩めない。薬剤師の負担軽減は盲検比較で確認時間・照合手順と誤見逃しを実測して別に証明する。`evaluation/README.md`の**全巡回p95<15分**を維持し、従来2枠に対して同条件でRT遅延の非劣化と計測誤差を超える処理容量改善を確認する。合成評価だけで臨床G6合格や短縮を宣言しない。
4. **本番移行の停止点**: 原文の継続保存・旧参照の安全な切替・全クライアント入口排他・採用枠数での実モデル容量・G6人手評価・version-bound activationのいずれかが欠ければ切替しない。configだけでcanonical/enforceを有効化しない。運用者が別途承認した設定と評価receiptに従い、小さな対象→段階拡大し、問題時には新規v4受付を閉じ、まだ旧生成成果が存在する対象だけ旧版を明示選択する。既に上書き済みなら原文+pendingへ戻し、有限の再処理なしに旧結果を復元したと偽らない。既発行通知・原文は切替操作で削除しない。

本節は現行実装の合格証明ではない。コードと資料の照合表、反例、合成実行の範囲、未測定条件はHermes作業場の調査記録 `/Users/yusuke/.hermes/hermes-agent/.omo/ulw-research/20260925-112553/SYNTHESIS.md` に残す。今ある実証は完全合成の41件欠落、policy fingerprint衝突、Jev回答欠落、非PASS v4 reader誤選択と、既存のmandatory表示・修復テスト6件の成功までである。
