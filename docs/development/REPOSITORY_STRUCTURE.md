# 責務別の構成と再利用の境界

この文書はhermes-mcs開発候補の内部構造を説明します。調査した範囲と各候補の検証結果は、その候補の作業記録で確認してください。版の公開、本番適用、モジュール単体での独立配布を示すものではありません。

利用手順は[文書索引](../README.md)、モジュール・CLIの生成表と検証入口は[開発リファレンス](DEVELOPMENT.md)が正本です。この文書は依存方向、共有状態、副作用の所有者を説明します。

## 実行コードの所有先

| 所有先 | 責務・主要入口 | 状態と副作用 |
|---|---|---|
| `mcs/core/` | `ledger`の台帳writer、`ledger_schema`のschema/migration/構造検証、`mcs_contract`のMCS値/例外、`mcs_config`の設定検証、`scheduled_jobs`の定期ジョブ契約 | `Ledger`が接続とwriterのtransactionを所有。schema初期化は借用した接続上で行い、接続を開閉しない |
| `mcs/core/mcs_queries.py`・`message_observations` | 原文・artifactの現行性、表示装飾前の患者文脈、`patient_context_sources`の原典集合、保存済み観測の共通読取 | 呼出元の同一connectionとtransactionを借用し、open/close/commitしない。表示用の架空Ledgerを作らない |
| `mcs/core/patient_context_artifacts.py` | 患者文脈の原CHUNK・採用済み修復候補の選択、参照・範囲・part・元費用証拠の共通検証 | 表示と依存喪失の記録が同じ保存集合を読む。呼出元のconnectionとtransactionを借用し、Jev判定・認可・公開・通信を行わない |
| `mcs/core/writer_lock.py`・`safe_io` | writerの取得・コード/更新世代の照合、安全なファイル公開とFD所有権移管 | 取得したFDは呼出元が保持・解放する。借用接続をclose/commitせず、ファイル公開は既存の同期・置換順序を守る |
| `mcs/core/snapshots.py` | snapshotのコピー・候補検証・atomic publish | 一時接続・一時ファイル・検証用子プロセスを生成し、成功/失敗時の終了と破棄を所有 |
| `mcs/core/snapshot_access.py` | 借用snapshotの世代・時刻とtable存在確認 | 呼出元の同一connectionを借用し、open/close/commitしない |
| `mcs/core/worker_process.py` | 通信workerの終了後のpipe排出・FD閉鎖・wait | workerを生成・停止する呼出元から終了対象を受け取る。プロセスグループのTERM/KILL・停止判断は移さない |
| `mcs/ingest/` | `mcs_adapter`の認証/取得、`mcs_worker`の子process統括、`mcs_cdp`のWebSocket、`mcs_download`の添付stream、`mcs_payload`の応答正規化 | session/retry/全体deadline/最終ファイル置換はadapter。wireとpartialは子process側。`collection_stages`が取得・再ログイン・walk cursorのstageを実行し、`run_check`が順序・全体締切・run lockを所有 |
| `mcs/ingest/health_report.py` | healthの観測・集計・coverage | 借用connectionと過去health記録を読み、書込み失敗のincidentは`run_check`が記録 |
| `mcs/llm/` | `cloud_inference`の共通実行、`cloud_policy`のscope/予算、`cloud_backlog`の背景worker | 通信状態の読書きは明示された保存先/借用接続上で行う。抽出結果・claim・carryのDB保存と解放は担当workerが所有 |
| `mcs/llm/providers/` | `anthropic_client`・`openai_client`のprovider固有の通信 | provider別のendpoint・要求/応答・usage契約を維持し、設定やモデルを混ぜない |
| `mcs/llm/transport/` | `cloud_client`の共通試行処理、`llm_retry`の再試行分類、`local_llm`のローカル通信・turn lock | 既存の期限と送信状態を扱い、呼出元の保存・公開条件を引き受けない |
| `mcs/llm/admission/` | `llm_admission`の共有枠、`llm_availability`の接続・回路状態 | permit・接続状態の保存を所有し、未知の送信結果を成功や未送信に置き換えない |
| `mcs/extract/rules/` | 投稿のルール抽出と実行統括 | 純粋な投稿判定と、run lockを伴う保存入口を区別 |
| `mcs/extract/clinical/` | `clinical_scope`の本人/家族/時点、`clinical_values`の値・原文根拠・バイタル適格性、分割・薬局の共通判断 | scopeのbounded cacheは同moduleが所有。rule統括へ呼び戻さない |
| `mcs/extract/clinical/clinical_urgency.py` | 本人の根拠に基づく緊急度・QCの共通判断 | `mcs_queries`の現行原文/artifact読取を使い、通知・統計・rulesが表示moduleを経由せず利用。借用connectionを開閉・commitしない |
| `mcs/extract/llm/` | `extract_schema`、`extract_validation`、`extract_quality`、`extract_llm`の推論/再抽出/保存統括と、`extraction_context`の抽出試行ごとの固定参照情報 | schemaと検証は推論I/Oから分離。薬剤tokenの文法は`extract_schema`に一元化し、rules/qualityが直接利用。claim/carry/checkpointと保存は統括側 |
| `mcs/patients/` | `patient_context`の患者背景/薬局参照項目、`patient_context_projection`の原文に基づく時系列の投影、一患者一件の集約、`patient_context_plan`の純粋な選択/予算計画・原文fact構築と厳格検証 | `patient_context_refresh`が時刻・推論・再試行・保存と公開を所有。factの発見時刻は呼出元が明示する。読取は同じconnectionを借用し、公開前に原文/世代/scanを再照合 |
| `mcs/medications/` | 辞書照合・別名承認・薬剤履歴・登録情報との比較 | 公開master原本、私有辞書、患者の注記と人承認receiptを区別 |
| `mcs/semantic/` | claim/QC/公開、`semantic_publication`の公開transaction処理、`semantic_store`の読取、`semantic_transport`の推論状態 | `semantic_drain`が実行順・予算・jobを所有。`semantic_runtime`が明示されたjob判定/送信hookを扱い、`semantic`のCLI facadeへ戻らない |
| `mcs/semantic/semantic_conversion.py` | 変換manifest・予約・receipt・実行前の適格性 | runtimeと操作CLIが同じ実体を利用し、評価専用領域を経由しない。読取は借用接続、予約/receipt/job更新は呼出元writerの同一transaction |
| `mcs/semantic/semantic_notice.py` | 通知適格性・起源照合・配送重複確認・公開計画 | 通常の結果/計画/intent/状態遷移は呼出元writerの同一transaction。既存degraded通知はintentごとのcommitを維持し、描画と送信直前gateは借用読取に限定 |
| `mcs/semantic/semantic_assembly.py` | fact・根拠・依頼属性・進捗の組立て | 入力のコピーと返値を所有し、DB・推論・時刻・保存状態を持たない。`semantic_extraction`がlive/cache/repairの実行と借用connectionの読取を所有 |
| `mcs/notify/` | 対象選定、spec/part計画、描画、送信許可、journal照合、receipt | `notify_parts`がpart計画、`notify_scope`がscope、`notify_spool`が保存先と公開、`notify_sender`がDBを持たないCLI送信を所有。`notify_actions`と`notify_transport`が同一transactionの適用/配送事実を確定し、公開はcommit後に行う |
| `mcs/stamps/` | `stamp_contract`は種別・設定・集計検証、`stamp_read`は押下者の読取、`stamp_format`は表示、`stamp_write`は人承認操作 | 読取は同一connectionを借用する。承認と待ち行列はreceiptと同一TX、送信はcommit後のrun lock内。`MCSAdapter`が通信を所有し、DB復元で巻き戻さない試行記録で二重送信を防ぐ |
| `mcs/views/` | snapshot読取・scope/projection・統計/詳細/CLI | `registered_data`は登録情報、`clinical_display`は臨床値、`stats_contract`は集計範囲/結果形式、`message_metadata`はメンション・固定表示。保存済み観測の検証は`core/message_observations`。呼出元のDBを再openしない |
| `mcs/signals/` | 原文と抽出に基づく候補評価・根拠 | 候補artifact/通知状態の更新を持つ。表示や単なる未記録と臨床判断を混同しない |
| `mcs/export/` | C1 schema・レコード選択、`export_contract`の承認/envelope/receipt検証、`export_delivery`のjournal/配送/撤回/health | 純粋な検証と専用保存先への配送を区別。`ext_contract`は操作CLI・参照受信入口に限定。viewは呼出元の同一snapshotを借用し、搬出処理のために再openしない |
| `mcs/ops/` | 設定対話・診断・導入・更新・backup/restore・repair・CLI | `setup_wizard`は対話、`lifecycle_io`は秘密値/安全I/O、`runtime_inspection`は読取観測、`service_contract`は管理対象・実行環境選択・cron計画、`service_management`は実際の照合・適用・管理記録、`update_policy`は判断、`update_journal`は更新記録。`owned_process`は所有する子プロセスの停止/回収、`update_discovery`は期限付きGit実行と版探索、`update_launch`はwrapper起動を所有 |
| `mcs/ops/diagnostics/environment_diagnostics.py` | 接続・認証・プロセス源泉の診断 | 有限のpath/実行環境を受け、既存の期限付き観測を実行する。setupは対話・結果表示、updaterはbaseline比較を所有。旧setup診断入口は旧updater互換だけに残す |
| `mcs/ops/backup/backup_evidence.py`・`restore_evidence` | backup記録/inventory/復元準備と、損失report/一致する人承認receipt | 専用read-only接続の終了と、終了後のhash/atomic公開を所有。暗号化・操作を囲むロックはbackup統括、hold・journal・DB置換はupdaterが引き続き所有 |
| `adapters/common/` | 接続先共通の認可・患者scope・command/preview/confirm・配送worker | workerがscope lock/claim/再試行、registryが永続slot記録、journalが配送事実を所有 |
| `adapters/common/forms.py`・`card_controls.py` | フォーム定義と入力値検証、カード操作の分類とスタンプ表示名 | runnerが発行した選択肢・tokenを借用する純粋処理。フォームの保管・期限は各actions/registry、最終認可・適用はrunnerが所有 |
| `adapters/slack/presentation.py` | 本文・スレッド・患者情報のBlock Kit描画、表示上限と文字/リンクの安全な変換 | SDK・通信・保存を持たない。`cards.py`がspec検証と操作部品の組立て、`actions.py`が本人向け回答、`delivery.py`が配送を所有 |
| `adapters/{slack,discord,lineworks}/` | 接続先固有のUI・SDK・送信/受信 | 承認と共通scope検証を通し、SDKの許可ファイルと遅延importを維持 |
| `hermes_plugin/` | plugin登録・遅延bootstrap・host設定/identity変換・worker接続 | import時点ではadapterを読込まず、正しいcheckoutを解決して接続 |
| `mcs_standalone/` | 独立host・service・Runtime・ネイティブ接続 | Runtimeが子プロセス/slot/停止/回収を所有。共通設定はsetup CLIへ依存しない |

## 運用コードと開発資産の配置

`mcs/ops/`直下は公開CLI、旧版の更新ツールが起動する入口、固定パスや隣接ファイルの契約を持つものを保持します。内部ヘルパーは用途ごとに置き、従来のflat import名を維持します。

| 配置 | まとめる処理 |
|---|---|
| `mcs/ops/setup/` | 設定対話、APIキー入力、設定の保存・退避、安全な導入時I/O |
| `mcs/ops/update/` | 更新方針、版の探索、起動、移行、journalと移行receipt |
| `mcs/ops/backup/` | バックアップ設定、記録の照合、復元準備と人承認の証明 |
| `mcs/ops/diagnostics/` | 添付・クラウド・環境・実行状態の読取り診断 |
| `mcs/ops/commands/` | 操作・依頼・失敗ジョブのCLIと共通契約 |
| `mcs/ops/services/` | 常駐処理の構成と照合・適用 |
| `mcs/medications/drug_info.py` | 公開医薬品情報の照会 |

`settings_migration_reconcile.py`は旧ランチャーの固定入口として直下に残します。`status_fields.json`は固定CLIとの隣接読取りを維持します。CSDC専用コードの配置は専任担当の所有範囲を保ちます。

SDKチェックは`scripts/development/`、オフライン評価の実行コードは`scripts/evaluation/`へまとめます。評価入力・基準・manifestは`evaluation/`に保持し、内容とハッシュを変えません。説明図のHTML原稿とSVGは`docs/assets/diagrams/`にそろえます。履歴記録と過去版のfixtureは、当時のパスを保持します。

## 主要な依存方向

- 共通commandと認可は`adapters/common/`が正本であり、Hermesとstandaloneがそれを利用します。共通処理はhost pluginからprivate helperを借りません。
- 入力フォームは`forms`、結果の日本語表示は`text`、カード操作の分類は`card_controls`が正本です。SDKの部品・上限・イベント形式は各adapterに残します。Slackの本文描画を利用する回答/配送は`presentation`へ直接依存し、カードmoduleのprivate関数を借りません。
- 抽出の補完済み判定は`extract_quality`へ集約します。患者状態・薬剤履歴・監査は同じ判定を利用し、それぞれの原文/世代/context照合と保留条件は自身で維持します。
- 同梱薬剤masterの読取りは`import_drug_master`が所有します。導入CLI・更新前確認・参照情報は同じ読取りを使い、CLI初期化を経由しません。semanticのjob種別は`semantic_policy`に置き、観測CLIがdrain統括を読み込まない構成にします。
- 取得stageは`collection_stages`が認証回復の共通予算と部分保存・snapshot既読化の順序を保って実行します。統括はmodule全体やCLI引数を注入せず、必要な値と借用adapter/writerを渡します。stage側は接続を開閉せず、既存のwriterのtransaction境界を使います。
- MCSのwireはworkerから`mcs_cdp`/`mcs_download`を直接呼び、親adapterのprivate実装へ戻りません。添付のURL検証は`mcs_download`が共通化し、親だけがtoken取得と最終ファイル置換を行います。
- MCSの値・例外は`mcs_contract`、登録データの検証は`project_metadata_contract`が正本です。読取側が通信クライアントや取得CLIの実装をimportする必要を減らします。
- ルールとLLMは共通の臨床scope/値を利用します。臨床scopeが抽出統括へ戻る循環は作りません。
- LLM統括はschema/検証/品質を呼びます。純粋な検証moduleは`extract_llm`のglobals、endpoint、call notesを操作しません。
- semanticの処理はtransport/store/QC/renderの正本を呼び、CLI moduleへの遅延importやmodule全体の引数注入を使いません。
- semanticのlive/cache/repairは同じ純粋な組立てを利用します。cache再構築は呼出元のconnectionを借用し、擬似writerやLLMの代用品を作りません。chunk・preflight・関係判定が未完了なら再構築成功として返しません。
- semanticの停止理由と品質診断は各job、補助抽出の集計は各passが所有し、必要な処理へ明示的に渡します。入れ子の実行や次のQC jobへ診断を持ち越しません。公平性のturnと再投入の抑制時刻は、常駐workerの状態として別に保持します。
- セマンティックの本文・分割・要約表示は`semantic_render`、各送信直前の現行性/権限/停止判定は`semantic_send_gate`が所有します。配送側は各境界で現在の設定を渡し、表示用wrapperを認可の代わりにしません。
- Jevへのguard・予算予約・通信期限は呼出し単位で渡し、同じclientの可変hookを付け替えません。jobの診断とpassの集計、再入時の状態を混同しません。
- 患者コンテキスト作成の選択/予算は純粋な計画へ分け、原文と時刻の観測、再試行と公開はworkerが所有します。終日処理し、作業日はJST 00:00に切り替わります。
- 原典QAの質問識別子は`semantic_audit`、元予約・回答・判定方針・PASSの読取検証は`semantic_runtime`が正本です。クラウド原典の依存記録は全選択範囲の証拠を照合し、現在の設定・権限・公開許可は呼出元で別に確認します。
- 集計と抽出監査は`stats_contract`の同じ期間/患者範囲/上限を使います。表示上限と全体件数を分け、監査の定義識別子は実際の計算契約に結び付けます。
- 通知のspec計画、同一TXのjournal記録、commit後のatomic publishの順序を保ちます。parts/scopeを分けてもtransactionの所有を分散しません。
- スタンプは読取り・表示・操作を`stamps/`へ集約します。一般のメタデータ検証は`core/message_observations`に置き、収集や送信が表示moduleを経由しない構成にします。操作の設定・本人照合・復元時の停止条件は[スタンプ管理契約](../specs/stamp-management.md)が正本です。
- 人承認の復旧は`notify_recovery`から`notify_transport`の借用transaction上の確定処理を使います。復旧receiptと状態を同時にcommitし、spec公開はその後に行います。読取側は`notify_scope`へ直接依存します。
- 薬剤照合の進捗と現行参照の読取は`drug_map`の実体が正本です。統計/通知/薬剤履歴は同じsnapshotと一巡の進捗を共有し、テスト用の関数同一性で別経路へ分岐しません。
- 共通hashの検証は`mcs_util`、版タグの文法は`update_policy`が正本です。操作契約や表示がqueue CLIを経由せず、queueの保存・ロック・順序は`mcs_requests`が所有します。hostの遅延bootstrap後に必要な処理を読む契約は維持します。
- 独立Slack/Discordのネイティブcommandは`common.commands.run_native`が同期処理の実終了まで待ち、呼出元のlistenerを途中で解放しません。接続先が現在の設定・停止状態を検証し、`mcs_requests.enqueue`がqueue lock取得後と公開直前にその検証を実行します。公開後の保存結果不明とreceiptは既存queueが所有し、応答の取消を未送信扱いにはしません。
- 薬局の参照項目は`patient_context`、保存済み履歴のSQLは`mcs_queries`が正本です。抽出・semantic・表示から集約writerを経由せずに利用し、解釈に影響する両moduleをsource digestとcandidate定義の変更検知に含めます。
- 表示用のバイタルは`clinical_display`、登録情報のprojectionは`registered_data`が正本です。表示limitでcoverageや全体件数を変えません。
- 更新の遠隔版探索はreceiptの書込transactionに入る前に行い、ローカルのwrapper/設定はreceipt確定の境界で再確認します。更新journal・復元同意・rollbackはupdater、切り離し起動はcommit後の呼出元が所有します。独立復旧ツールはcheckout破損時にも起動できる自己完結のprocess処理を保持します。

## 統括に残す状態と互換入口

`Ledger`はwriter接続とtransaction、`run_check`はrun lock・stage順序・締切・終了記録を所有します。`extract_llm`はclaim/carry/checkpointと検証後の保存を一つの流れで扱い、`semantic_drain`はjobのclaim/予算/再開を扱います。これらの状態を複数のmoduleが同時に管理する分割は行いません。用途別のSQL・純粋な計画・検証・公開処理は各正本へ分離します。

配送workerはscope lock/claim/停止と再開を統括し、registryは永続slot、journalは配送履歴を所有します。存在を確認する全spec集合と、今回処理する集合を混同しません。standalone Runtimeは定期job・子process同一性・停止/回収を所有し、hostはSDK接続とtask終了を所有します。

更新処理はlock・承認・journal・rollbackを保持し、探索・子process・診断・service適用を各正本へ渡します。旧版が新版の`mcs/`だけを配置して行う設定検証と、旧setup診断入口は互換契約です。独立復旧ツールはcheckout破損時にも起動するため、同じ値/判定を使う必要があっても開発treeからのimportには置き換えません。 所有するFDをstreamへ渡す短い処理も独立ツール内で完結させ、切り離し子processへ必要な実装を読み込み時に凍結します。借用FDの読取は元のownerが解放します。

## 移植・拡張時の境界

フォルダだけをコピーすれば独立動作するという契約ではありません。flat importのbootstrap、参照する共通module、入力/返値、設定、状態保存先、安全ゲートを一緒に確認してください。

| 変更したい内容 | 最初に確認する正本 | 一緒に確認する契約 |
|---|---|---|
| 接続先追加/host変更 | `adapters/common/access.py`, `dispatch.py`, `human_commands.py` | actor/origin/transport/患者scope/payload hash、例外秘匿、SDK遅延load |
| 入力フォーム/カード操作/Slack本文 | `adapters/common/forms.py`, `card_controls.py`, `adapters/slack/presentation.py` | runner発行の選択肢とtoken、取消対象、表示順、患者scope、未知/未取得、各SDKの上限と再送条件 |
| 定期ジョブ/設定検証 | `scheduled_jobs`, `mcs_config` | CLI互換、service生成、standalone catchup/停止、旧updaterのimport閉包 |
| 抽出の検証/品質判断 | `extract_validation`, `extract_quality`, `clinical_scope` | evidence/context、否定/他者/時点、held_prior、candidate定義とsource digest |
| 推論通信 | `local_llm`, `semantic_transport` | wire slot/turn/deadline、送信不明、carryと保存、回路状態 |
| fact・根拠・依頼属性の組立て | `semantic_assembly`, `semantic_facts` | 同一入力の順序と根拠、原文revision、未完了の保持、source digestとcandidate定義 |
| 患者の読取表示 | `read_model`, `registered_data`, `clinical_display` | 同一snapshot、患者scope、未取得/0件、limitと全体coverage |
| 通知のpart/再開 | `notify_parts`, `notify_journal`, `notify_transport` | 同一TX、receipt/冪等性、送信結果不明、commit後publish |
| 通知操作・人承認復旧 | `notify_actions`, `notify_action_tokens`, `notify_action_context`, `notify_recovery` | token/source/scopeの再照合、保存と公開の順序、worker証跡、結果不明の保持 |
| MCSスタンプの追加・取消・表示 | `stamp_contract`, `stamp_read`, `stamp_format`, `stamp_write` | 確認済みの操作者とアカウント、投稿の版、receipt/TX、復元後の再送防止、未取得と0件の区別 |
| schema・台帳構造 | `ledger_schema`, `ledger`, `mcs_queries` | 接続の所有、同一TX、旧updaterの数値literal読取、既存DBのmigration/backup/rollback |
| 更新/復旧 | `mcs_update`, `update_policy`, `update_journal`, `mcs_recover` | 旧版が新treeを読むこと、journal/lock/rollback、独立復旧入口 |

## 残す互換契約

`mcs/_mcs_path.py`が実行moduleを登録し、basenameを一意に保ちます。全面的なpackage import化や配布方式の変更はしていません。

`hermes_plugins.mcs`別名load、repo外`python -I`、SDK未導入時のplugin import、`python -m lineworks_adapter`と`python -m mcs_standalone`を維持します。内部テストだけのprivate再exportは互換契約に含めません。

旧updaterの読取に必要な`mcs/core/ledger.py`の`SCHEMA_VERSION`数値literal、`mcs/notify/notify_cards.py`の`CARD_LAYOUT`数値literal、`mcs_setup.validate_config()`入口を維持します。`ledger.publish_snapshot`は既存public利用のため残し、実装の正本は`snapshots`に置きます。

## 検証と現在の制約

テストは[開発リファレンス](DEVELOPMENT.md)の隔離runnerで行い、完全合成データ、一時DB、通信スタブのみを使います。評価専用資産は`evaluation/`と各領域の`eval/`に置き、定期処理から読み込みません。

責務移動でもsource digestとcandidate fingerprintは変わります。新しい定義moduleを変更検知から外さず、古い候補/保留receiptの再判定と原文根拠の保持を合成テストで確認します。これを抽出世代・モデル・臨床判断の変更とは扱いません。

全体の本文調査、統合テスト、独立レビューが未完の段階では、この文書だけを受入証跡や本番適用の許可として使用しないでください。確定した過去の受入票、変更archive、公式master原本は現在の候補に合わせて書き換えません。今回明示された端末固有値の除去は例外として一般化し、版・事故識別子・当時の判断と検証の意味を保持します。Git履歴や外部のRelease本文は変更していません。
