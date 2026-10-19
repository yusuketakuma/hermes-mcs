# 責務別の構成と再利用の境界

この文書は1.0.28開発候補の内部構造を説明します。現時点では統合検証と全領域の調査が進行中です。版の公開、本番適用、モジュール単体での独立配布を示すものではありません。

利用手順は[文書索引](../README.md)、モジュール・CLIの生成表と検証入口は[開発リファレンス](DEVELOPMENT.md)が正本です。この文書は依存方向、共有状態、副作用の所有者を説明します。

## 実行コードの所有先

| 所有先 | 責務・主要入口 | 状態と副作用 |
|---|---|---|
| `mcs/core/` | `ledger`の台帳writer、`ledger_schema`のschema/migration/構造検証、`mcs_contract`のMCS値/例外、`mcs_config`の設定検証、`scheduled_jobs`の定期ジョブ契約 | `Ledger`が接続とwriterのtransactionを所有。schema初期化は借用した接続上で行い、接続を開閉しない |
| `mcs/core/mcs_queries.py`・`message_observations` | 原文・artifactの現行性、表示装飾前の患者文脈、保存済み観測の共通読取 | 呼出元の同一connectionとtransactionを借用し、open/close/commitしない。表示用の架空Ledgerを作らない |
| `mcs/core/writer_lock.py`・`safe_io` | writerの取得・コード/更新世代の照合、安全なファイル公開とFD所有権移管 | 取得したFDは呼出元が保持・解放する。借用接続をclose/commitせず、ファイル公開は既存の同期・置換順序を守る |
| `mcs/core/snapshots.py` | snapshotのコピー・候補検証・atomic publish | 一時接続・一時ファイル・検証用子プロセスを生成し、成功/失敗時の終了と破棄を所有 |
| `mcs/core/snapshot_access.py` | 借用snapshotの世代・時刻とtable存在確認 | 呼出元の同一connectionを借用し、open/close/commitしない |
| `mcs/ingest/` | `mcs_adapter`の認証/取得、`mcs_worker`の子process統括、`mcs_cdp`のWebSocket、`mcs_download`の添付stream、`mcs_payload`の応答正規化 | session/retry/全体deadline/最終ファイル置換はadapter。wireとpartialは子process側。`collection_stages`が取得・再ログイン・walk cursorのstageを実行し、`run_check`が順序・全体締切・run lockを所有 |
| `mcs/ingest/health_report.py` | healthの観測・集計・coverage | 借用connectionと過去health記録を読み、書込み失敗のincidentは`run_check`が記録 |
| `mcs/llm/` | 推論通信、turn lock、admission、`llm_availability`の接続・回路状態 | 通信状態の読書きは明示された保存先/借用接続上で行う。抽出結果・claim・carryのDB保存と解放は担当workerが所有 |
| `mcs/extract/rules/` | 投稿のルール抽出と実行統括 | 純粋な投稿判定と、run lockを伴う保存入口を区別 |
| `mcs/extract/clinical/` | `clinical_scope`の本人/家族/時点、`clinical_values`の値と原文根拠、分割・薬局の共通判断 | scopeのbounded cacheは同moduleが所有。rule統括へ呼び戻さない |
| `mcs/extract/llm/` | `extract_schema`、`extract_validation`、`extract_quality`、`extract_llm`の推論/再抽出/保存統括 | schemaと検証は推論I/Oから分離。claim/carry/checkpointと保存は統括側 |
| `mcs/patients/` | `patient_context`の患者背景/薬局参照項目、状態参照・一患者一件の集約、`patient_nightly_plan`の純粋な選択/予算計画 | `patient_nightly`が時刻・推論・再試行・保存と公開を所有。読取は同じconnectionを借用し、公開前に原文/世代/scanを再照合 |
| `mcs/medications/` | 辞書照合・別名承認・薬剤履歴・登録情報との比較 | 公開master原本、私有辞書、患者の注記と人承認receiptを区別 |
| `mcs/semantic/` | claim/QC/公開、`semantic_v4`の公開transaction処理、`semantic_store`の読取、`semantic_transport`の推論状態 | `semantic_drain`が実行順・予算・jobを所有。`semantic_runtime`が明示されたjob判定/送信hookを扱い、`semantic`のCLI facadeへ戻らない |
| `mcs/semantic/semantic_assembly.py` | fact・根拠・依頼属性・進捗の組立て | 入力のコピーと返値を所有し、DB・推論・時刻・保存状態を持たない。`semantic_extraction`がlive/cache/repairの実行と借用connectionの読取を所有 |
| `mcs/notify/` | 対象選定、spec/part計画、描画、送信許可、journal照合、receipt | `notify_parts`がpart計画、`notify_scope`がscope、`notify_spool`が保存先と公開、`notify_sender`がDBを持たないCLI送信を所有。`notify_actions`と`notify_transport`が同一transactionの適用/配送事実を確定し、公開はcommit後に行う |
| `mcs/views/` | snapshot読取・scope/projection・統計/詳細/CLI | `registered_data`は登録情報、`clinical_display`は臨床値、`stats_contract`は集計範囲/結果形式、`message_metadata`は保存済み観測の検証。呼出元のDBを再openしない |
| `mcs/signals/` | 原文と抽出に基づく候補評価・根拠 | 候補artifact/通知状態の更新を持つ。表示や単なる未記録と臨床判断を混同しない |
| `mcs/export/` | C1 schema・レコード選択・封筒・受信契約 | 純粋な変換、snapshot読取、専用保存先への受信を区別 |
| `mcs/ops/` | 設定対話・診断・導入・更新・backup/restore・repair・CLI | `setup_wizard`は対話、`lifecycle_io`は秘密値/安全I/O、`runtime_inspection`は読取観測、`service_contract`は管理対象・実行環境選択・cron計画、`service_management`は実際の照合・適用・管理記録、`update_policy`は判断、`update_journal`は更新記録。`owned_process`は所有する子プロセスの停止/回収、`update_discovery`は期限付きGit実行と版探索、`update_launch`はwrapper起動を所有 |
| `mcs/ops/environment_diagnostics.py` | 接続・認証・プロセス源泉の診断 | 有限のpath/実行環境を受け、既存の期限付き観測を実行する。setupは対話・結果表示、updaterはbaseline比較を所有。旧setup診断入口は旧updater互換だけに残す |
| `mcs/ops/backup_evidence.py`・`restore_evidence` | backup記録/inventory/復元準備と、損失report/一致する人承認receipt | 専用read-only接続の終了と、終了後のhash/atomic公開を所有。暗号化・操作を囲むロックはbackup統括、hold・journal・DB置換はupdaterが引き続き所有 |
| `adapters/common/` | 接続先共通の認可・患者scope・command/preview/confirm・配送worker | workerがscope lock/claim/再試行、registryが永続slot記録、journalが配送事実を所有 |
| `adapters/{slack,discord,lineworks}/` | 接続先固有のUI・SDK・送信/受信 | 承認と共通scope検証を通し、SDKの許可ファイルと遅延importを維持 |
| `hermes_plugin/` | plugin登録・遅延bootstrap・host設定/identity変換・worker接続 | import時点ではadapterを読込まず、正しいcheckoutを解決して接続 |
| `mcs_standalone/` | 独立host・service・Runtime・ネイティブ接続 | Runtimeが子プロセス/slot/停止/回収を所有。共通設定はsetup CLIへ依存しない |

## 主要な依存方向

- 共通commandと認可は`adapters/common/`が正本であり、Hermesとstandaloneがそれを利用します。共通処理はhost pluginからprivate helperを借りません。
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
- 患者コンテキスト作成の選択/予算は純粋な計画へ分け、原文と時刻の観測、再試行と公開はworkerが所有します。終日処理とJST03:30の作業日境界は維持します。
- 集計と抽出監査は`stats_contract`の同じ期間/患者範囲/上限を使います。表示上限と全体件数を分け、監査の定義識別子は実際の計算契約に結び付けます。
- 通知のspec計画、同一TXのjournal記録、commit後のatomic publishの順序を保ちます。parts/scopeを分けてもtransactionの所有を分散しません。
- 人承認の復旧は`notify_recovery`から`notify_transport`の借用transaction上の確定処理を使います。復旧receiptと状態を同時にcommitし、spec公開はその後に行います。読取側は`notify_scope`へ直接依存します。
- 薬局の参照項目は`patient_context`、保存済み履歴のSQLは`mcs_queries`が正本です。抽出・semantic・表示から集約writerを経由せずに利用し、解釈に影響する両moduleをsource digestとcandidate定義の変更検知に含めます。
- 表示用のバイタルは`clinical_display`、登録情報のprojectionは`registered_data`が正本です。表示limitでcoverageや全体件数を変えません。
- 更新の遠隔版探索はreceiptの書込transactionに入る前に行い、ローカルのwrapper/設定はreceipt確定の境界で再確認します。更新journal・復元同意・rollbackはupdater、切り離し起動はcommit後の呼出元が所有します。独立復旧ツールはcheckout破損時にも起動できる自己完結のprocess処理を保持します。

## 統括に残す状態と互換入口

`Ledger`はwriter接続とtransaction、`run_check`はrun lock・stage順序・締切・終了記録を所有します。`extract_llm`はclaim/carry/checkpointと検証後の保存を一つの流れで扱い、`semantic_drain`はjobのclaim/予算/再開を扱います。これらの状態を複数のmoduleが同時に管理する分割は行いません。用途別のSQL・純粋な計画・検証・公開処理は各正本へ分離します。

配送workerはscope lock/claim/停止と再開を統括し、registryは永続slot、journalは配送履歴を所有します。存在を確認する全spec集合と、今回処理する集合を混同しません。standalone Runtimeは定期job・子process同一性・停止/回収を所有し、hostはSDK接続とtask終了を所有します。

更新処理はlock・承認・journal・rollbackを保持し、探索・子process・診断・service適用を各正本へ渡します。旧版が新版の`mcs/`だけを配置して行う設定検証と、旧setup診断入口は互換契約です。独立復旧ツールはcheckout破損時にも起動するため、同じ値/判定を使う必要があっても開発treeからのimportには置き換えません。

## 移植・拡張時の境界

フォルダだけをコピーすれば独立動作するという契約ではありません。flat importのbootstrap、参照する共通module、入力/返値、設定、状態保存先、安全ゲートを一緒に確認してください。

| 変更したい内容 | 最初に確認する正本 | 一緒に確認する契約 |
|---|---|---|
| 接続先追加/host変更 | `adapters/common/access.py`, `dispatch.py`, `human_commands.py` | actor/origin/transport/患者scope/payload hash、例外秘匿、SDK遅延load |
| 定期ジョブ/設定検証 | `scheduled_jobs`, `mcs_config` | CLI互換、service生成、standalone catchup/停止、旧updaterのimport閉包 |
| 抽出の検証/品質判断 | `extract_validation`, `extract_quality`, `clinical_scope` | evidence/context、否定/他者/時点、held_prior、candidate定義とsource digest |
| 推論通信 | `local_llm`, `semantic_transport` | wire slot/turn/deadline、送信不明、carryと保存、回路状態 |
| fact・根拠・依頼属性の組立て | `semantic_assembly`, `semantic_facts` | 同一入力の順序と根拠、原文revision、未完了の保持、source digestとcandidate定義 |
| 患者の読取表示 | `read_model`, `registered_data`, `clinical_display` | 同一snapshot、患者scope、未取得/0件、limitと全体coverage |
| 通知のpart/再開 | `notify_parts`, `notify_journal`, `notify_transport` | 同一TX、receipt/冪等性、送信結果不明、commit後publish |
| 通知操作・人承認復旧 | `notify_actions`, `notify_action_tokens`, `notify_action_context`, `notify_recovery` | token/source/scopeの再照合、保存と公開の順序、worker証跡、結果不明の保持 |
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
