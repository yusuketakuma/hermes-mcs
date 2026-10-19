# 責務別の構成と再利用の境界

この文書は1.0.28候補の構成を説明します。ファイルを目的から探しやすくし、
別のシステムへ移すときに必要な依存・保存先・安全条件を確認できるようにします。
フォルダを分けても、独立して導入できるPythonパッケージになったわけではありません。
本番へ反映された版は、リリース記録と更新結果で別に確認してください。

## 何をどこへ置くか

| 配置 | 責務 | ここへ混ぜないもの |
|---|---|---|
| `mcs/core/` | 台帳・SQL共通条件・期限付きHTTP・ファイル保存・ロックなどの基盤 | モデル固有の推論制御、チャット画面 |
| `mcs/llm/` | ローカルLLMへの要求、スロット・順番制御、任意のadmission管理 | 医療情報の抽出仕様、通知先への接続 |
| `mcs/extract/rules/` | ルールによる投稿の構造化 | LLMの起動・配送 |
| `mcs/extract/llm/` | 投稿単位のLLM抽出、検証、修復、再試行、常駐worker | 患者単位の集約、SDK接続 |
| `mcs/extract/clinical/` | 臨床値・所有者・単位・数量・分割境界の共通規則 | 台帳の管理CLI、実サービス接続 |
| `mcs/extract/eval/` | 抽出の合成評価器 | 定期処理から呼ぶ業務処理 |
| `mcs/patients/` | 患者背景・状態・継続的なコンテキスト作成・集約 | 投稿取得、薬剤辞書そのもの |
| `mcs/medications/` | 薬剤辞書照合・別名・経過・突合・マスター比較 | 原本ZIPの保管、辞書の無承認有効化 |
| `mcs/signals/` | 保存済み情報からのシグナル検出 | チャット配送、モデルへの要求 |
| `mcs/semantic/` | 意味解析・品質確認・正規化された事実と依頼の管理 | 接続先SDK、配布マスター原本 |
| `mcs/export/` | C1出力契約・レコード選択・封筒・参照受信器 | 管理CLI入口、無承認の外部送信 |
| `mcs/ingest/` | 収集・定期処理の段階制御・監視 | チャット画面の描画 |
| `mcs/notify/` | 通知の内容、対象、操作と配送計画の組立て | プラットフォームの接続認証 |
| `mcs/notify/presentation/` | `present_*`によるSlack・Discord・LINE WORKS向けの表示整形 | SDK呼出、送信済み記録の管理 |
| `mcs/views/` | 公開snapshotなどの読取りと表示 | 収集・更新の実行 |
| `mcs/ops/` | 利用者・管理者向けCLI、導入・更新・復旧・承認操作 | 再利用するC1契約ライブラリ |
| `adapters/` | 接続先ごとの配送・操作・SDK境界と共通配送基盤 | ルール・医療値の解釈 |
| `hermes_plugin/` | Hermes接続、カードworkerと互換入口 | 独立接続用の認証経路 |
| `mcs_standalone/` | 独立hostの起動・定期処理・接続の管理 | 新しい医療判定 |

`extract/rules/`は旧`extract/v1/`、`extract/llm/`は旧`extract/v4/`の責務名です。
この変更で`extract.py`・`extract_llm.py`というモジュール名は変えません。
`RULE_VERSION = 24`、`EXTRACT_VERSION = 9`、artifactのkindや保存形式も、
フォルダ名に合わせて変更しません。`semantic_v4`は別の意味解析エンジンであり、
LLM抽出の保存世代を表すものではありません。

## importと起動の約束

`mcs/`配下は従来のflat importを維持します。たとえば`import local_llm`、
`import extract`、`import patient_context`で読み込みます。
`import mcs.llm.local_llm`のような新しいパッケージAPIを導入する変更ではありません。

実行入口は`mcs/`を`sys.path`へ加えて`_mcs_path`をimportします。
`_mcs_path.py`はPythonファイルのある下位ディレクトリを再帰登録します。
移動したCLIでは、現在の深さから正しい`mcs/`を求める必要があります。
同じstemのPythonファイルを別の責務フォルダに複製するとflat名前空間が衝突するため、
コピーによる互換実装は作りません。

フォルダの移動時には、import以外にもCLI・launchagent・wrapper・インストーラー・
更新/復旧ツール・子プロセスの起動パス・評価資産の相対パスを更新します。
移動済みのcheckoutと旧コードを読み込んだ常駐プロセスを混在させた状態を、
適用完了として扱いません。

## 再利用したい単位と必要な依存

下表の「必要な範囲」は主要な依存です。完全な配布用ファイル一覧ではありません。
関数内importを含む推移的な依存もあり、移植先で各単位の合成テストを通して確定します。
単に左列のフォルダだけをコピーする方法では再利用できません。

| 単位と入口 | 必要な範囲 | 入出力・副作用 |
|---|---|---|
| LLM transport: `local_llm.resolve`, `chat`, `probe_format`, `admitted_chat`, `admitted_probe_format` | `local_llm`、`llm_admission`、`bounded_http`、`mcs_util`とその依存。呼出元の期限・route・slot設定 | loopbackへのHTTP、HTTP子プロセス、背景ターンのファイルロック。admission有効時は専用SQLiteとプロセス識別も使用 |
| Admission: `llm_admission.Broker` | `llm_admission`、専用DBの保存先、バックエンドの完了を確認する運用 | DB・epoch・permitを書き込む。`/slots`がidleという観測だけで不明な要求を完了扱いにしない |
| 投稿のルール: `extract.extract_message` | `extract`、`clinical_values`、`patient_context`、`semantic_quantities`、`mcs_util`など。flat import上の基盤も必要 | 本文・投稿日からdictを作る。`run_pending`とCLIは別で、台帳の読書きとrun lockを伴う |
| 臨床共通: `clinical_values.patient_item_scope`, `patient_current_vitals`, `lab_candidate`, `medication_surface` | 臨床共通群に加え`extract`の本人・家族・時制判定。`clinical_chunking.plan_chunks`は`mcs_util`、`pharmacy_cues`と`semantic_quantities`はそれぞれの共通規則 | 値・引用・所有者を検証する。臨床共通フォルダだけで本人判定が完結するわけではない |
| 投稿のLLM抽出: `extract_llm.llm_extract` | LLM transport、ルール、臨床共通、患者背景、`extraction_refresh`など。workerまで含める場合は台帳、semantic/QC、設定・ロック・checkpointも必要 | 推論と根拠検証。`run_pending`・CLIはartifact、再試行、持ち越し結果、claimを保存する |
| 患者背景の構造化: `patient_context.extract_context`, `context_items`, `merged_context` | `patient_context`、`clinical_values`とその本人判定依存、`mcs_util` | 本文・抽出結果から背景情報を作る。接続先から投稿を取得する関数ではない |
| 患者状態と集約: `patient_state_context.prepare`, `load`; `rollup.build_rollup`, `rebuild`, `dirty_projects` | 患者群、台帳/SQL条件、`read_model`、依頼管理、薬剤照合、semantic projection、統計の共通処理 | 集約・参照の読取りと、`rebuild`等のキャッシュ書込みを区別する。返却情報には患者情報が含まれ得る |
| コンテキスト作成: `patient_nightly.run_due` | 患者群、LLM抽出、ルール、臨床分割、read model、台帳とrun lock。`admit`・`complete`・deadlineを渡すworker | 推論、scan/progress/artifact保存。公開可否、世代の完了条件、保存直前の入力再照合を維持 |
| 薬剤照合: `drug_map.load`, `DrugMap`, `current_refs`, `derive` | `drug_map`、臨床値正規化、SQL条件・依頼管理、承認済み私有辞書と設定。別名操作は`drug_alias`とsafe保存基盤 | `load`等の辞書読取りと、`derive`・別名適用の台帳書込みを区別する |
| 薬剤の照会: `drug_timeline.timeline`, `drug_reconciliation.reconcile`, `drug_master_compare.compare` | 患者状態、semantic、views、project metadata、`import_drug_master`など各照会の依存 | 主に読取り。辞書の読込みや公開masterの比較は、本番辞書の承認・有効化を代行しない |
| シグナル: `mcs_signals.evaluate`, `evidence_fp`, `signal_notice_text` | 台帳/SQL、臨床・ルール、運用状態、metadata、本人識別、structured view | 評価は状態・artifact等の書込みを伴う経路。文言整形だけの関数と分けて扱う |
| C1契約: `c1_contract.validate_profile`, `validate_record`; `c1_envelopes.build_envelopes`, `parse_envelope`, `validate_collection` | `c1_contract`、`c1_envelopes`、`export_schema`、`pharmacy_cues`、`stats_math` | レコード・封筒の検証と変換。これらの入口だけで外部へ送信しない |
| C1選択/受信: `c1_records.select_records`, `assemble_records`; `c1_receiver.ReferenceReceiver` | 前項に加えread model、signalsとその依存。受信器は`mcs_util`と専用保存先 | 選択はsnapshotを読む。受信器の生成・取込みは私有ディレクトリと記録を作るため、純粋変換関数と区別 |
| 通知の表示: `present_slack`, `present_discord`, `present_lineworks`の`face`, `parts_text`等 | `notify_render`と各表示関数の依存。LINE WORKS表示は`notify_cards`にも依存 | 表示dict/文字列を作る。SDK送信はadapterの別責務で、表示群だけを移して配送機能にはならない |

### 依存を切り離す前に残すもの

`clinical_values`の本人判定・現在値読取り・書込みガードは関数内で`extract`を読み、
`extract`は臨床語彙と`patient_context`を読みます。`patient_context`も臨床値を使います。
この循環はフォルダ分離では解消しません。現時点では関連群を一緒に移植し、
名前の似た別実装へ置き換えたり、遅延importを一律に先頭へ移したりしません。

`extract_llm`・`patient_nightly`・`patient_state_context`にも相互依存があります。
常駐worker、患者コンテキスト作成、過去分抽出を移す場合は、持ち越し結果・lease・
入力のhash・世代・公開状態の再確認まで含めて扱います。
フォルダ分離だけを理由に推論・公開・再解析の条件を変えません。

## 保存先・設定・初期化

`mcs_util.HOME`はimport時に`MCS_ROOT`を解決し、未指定なら`~/.mcs`を使用します。
台帳・snapshot・設定・ロック等の基点になります。移植や合成テストでは、
import前に隔離した基点を指定し、モジュールを読み込んだ後の環境変数変更だけで
保存先が切り替わると考えないでください。

`ledger.Ledger`は書込み用の接続・初期化を担い、schemaは9です。
`LedgerReader`と公開snapshotの読取りは、それとは別の入口です。
既存台帳の読取り確認に、書込み用初期化を代用しません。
`read_model`の`current/stale/pending/unknown`、snapshotの世代、根拠のhashを保持し、
取得できない状態を「0件」や「対応なし」に変換しません。

薬剤辞書には公式公開masterと、利用方針を承認して変換・有効化した私有辞書があります。
`resources/drug-master/`の原本・manifest・版・hashを維持し、
原本の同梱を本番辞書への自動承認・自動有効化と混同しません。
`config.json`・`.env`・`data/`・私有辞書・鍵・実投稿を移植用fixtureへ含めません。

## チャット接続を持ち出す場合

`adapters/common/`はrender-spec、grant、registry、journal、receipt等の共通配送契約です。
接続先別adapterはこれに加えて通知の内容・依頼管理・snapshot読取りなどを使います。
SDKやカード描画だけをコピーしても、再送制御・操作承認・配送記録は揃いません。

| 接続 | 実接続の所有者と必要な境界 |
|---|---|
| Hermes経由のSlack/Discord | Hermesが接続・認証・allowlistを所有。`hermes_plugin/`と対象adapterが連携し、別のBotやREST認証経路を追加しない |
| 独立Slack/Discord | 各`standalone.py`が固定版の公式SDKを遅延import。独立venvと独立host、私有設定の読取り、起動時のproxy除去が必要 |
| LINE WORKS | `adapters/lineworks/`が認証・Bot REST・署名callback・独立起動を所有。stdlib/OpenSSL、固定公式URL、期限・容量制限、no-redirect/no-proxyを維持 |

抽出・収集コアに新しい外部依存を加えません。Hermes接続のDiscord UIで許可された
遅延SDK importと、独立接続のSDK importは例外の場所を維持します。
送信元・接続先・scopeを結び付ける操作確認、送信結果が不明な場合の保留、
`--confirm-human`・reason・receiptの承認経路を移植先でも省略しません。

## テスト・ツール・資料

テストは実装責務に対応させ、`tests/llm/`、`tests/patients/`、
`tests/medications/`と、`tests/extract/`下のrules・llm・clinical・evaluationを使います。
共有の`extract_testkit.py`や領域を跨ぐ統合テストは、用途を保って配置します。
既存の再帰testkit登録を使い、同名テストモジュールを増やしません。

`scripts/run_tests.sh`による一時HOME・環境隔離と、conftestの実接続遮断を維持します。
実SDKの契約確認は`integration/`の専用環境・収集ガードで区別します。
配置変更の成功を、実LLM・実サービス・本番データでの検証成功に置き換えません。

| 資産 | 配置と扱い |
|---|---|
| 公開済みの文書/画面/リリース生成入口 | `scripts/development/`。既存の実行パスを維持 |
| 実通信を行う調査入口 | `scripts/diagnostics/`。通常の合成テストと区別 |
| 明示実行する評価・LLMベンチ | `scripts/evaluation/`。ベンチの停止条件・実行窓承認を維持 |
| 固定の合成評価入力・基準・レビュー資産 | `evaluation/`。封印manifestや基準hashを配置整理のついでに変更しない |
| CIと安全ゲート | `ci/`、`.github/`。責務分離のために検査対象や必須条件を弱めない |
| 現行の操作・開発・契約 | `docs/guides/`、`docs/development/`、`docs/specs/` |
| 計画・版別記録・事故の証跡 | `docs/roadmap/`、`docs/releases/`、`docs/dev-records/`。履歴を現行手順として書き換えない |
| 利用者向け画面と説明図 | `docs/screenshots/`、`docs/assets/`。原稿と生成物、manifestの対応を維持 |
| 変更記録 | 未公開分は`changes/`、公開済みは`changes/archive/版/`。履歴を削除しない |

構成変更の検証では、移動したファイルの数だけでなく、CLI起動、flat import、
台帳の保存・公開契約、生成文書、配備パス、SDK境界まで確認します。
全追跡ファイルの移動・維持・保護の対応は、今回の整理作業の台帳で追跡します。
