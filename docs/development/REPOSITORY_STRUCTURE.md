# 責務別の構成と再利用の境界

この文書は1.0.28開発候補の内部構造を説明します。現時点では統合検証と全領域の調査が進行中です。版の公開、本番適用、モジュール単体での独立配布を示すものではありません。

利用手順は[文書索引](../README.md)、モジュール・CLIの生成表と検証入口は[開発リファレンス](DEVELOPMENT.md)が正本です。この文書は依存方向、共有状態、副作用の所有者を説明します。

## 実行コードの所有先

| 所有先 | 責務・主要入口 | 状態と副作用 |
|---|---|---|
| `mcs/core/` | `ledger`の台帳writer、`ledger_schema`のschema/migration/構造検証、`mcs_contract`のMCS値/例外、`mcs_config`の設定検証、`scheduled_jobs`の定期ジョブ契約 | `Ledger`が接続とwriterのtransactionを所有。schema初期化は借用した接続上で行い、接続を開閉しない |
| `mcs/core/mcs_queries.py` | 連携サマリー・artifact履歴・原文参照の共通読取 | 呼出元の同一connectionとtransactionを借用し、open/close/commitしない。表示用の架空Ledgerを作らない |
| `mcs/core/snapshots.py` | snapshotのコピー・候補検証・atomic publish | 一時接続・一時ファイル・検証用子プロセスを生成し、成功/失敗時の終了と破棄を所有 |
| `mcs/core/snapshot_access.py` | 借用snapshotの世代・時刻とtable存在確認 | 呼出元の同一connectionを借用し、open/close/commitしない |
| `mcs/ingest/` | `mcs_adapter`の認証/取得、`mcs_worker`の子process統括、`mcs_cdp`のWebSocket、`mcs_download`の添付stream、`mcs_payload`の応答正規化 | session/retry/全体deadline/最終ファイル置換はadapter。wireとpartialは子process側、取得stage順は`run_check` |
| `mcs/ingest/health_report.py` | healthの観測・集計・coverage | 借用connectionと過去health記録を読み、書込み失敗のincidentは`run_check`が記録 |
| `mcs/llm/` | 推論通信、turn lock、admissionと接続状態 | モデル呼出しの寿命と通信状態。抽出結果のDB保存は担当workerが所有 |
| `mcs/extract/rules/` | 投稿のルール抽出と実行統括 | 純粋な投稿判定と、run lockを伴う保存入口を区別 |
| `mcs/extract/clinical/` | `clinical_scope`の本人/家族/時点、`clinical_values`の値と原文根拠、分割・薬局の共通判断 | scopeのbounded cacheは同moduleが所有。rule統括へ呼び戻さない |
| `mcs/extract/llm/` | `extract_schema`、`extract_validation`、`extract_quality`、`extract_llm`の推論/再抽出/保存統括 | schemaと検証は推論I/Oから分離。claim/carry/checkpointと保存は統括側 |
| `mcs/patients/` | `patient_context`の患者背景/薬局参照項目、状態参照・一患者一件の集約・コンテキスト作成 | 集約の読取はconnectionを借用し、artifact置換はwriterが所有。公開前に原文/世代/scanを再照合 |
| `mcs/medications/` | 辞書照合・別名承認・薬剤履歴・登録情報との比較 | 公開master原本、私有辞書、患者の注記と人承認receiptを区別 |
| `mcs/semantic/` | 計画/claim/QC/公開と保存、`semantic_transport`の推論状態 | `semantic`はCLI。処理側はCLI facadeへ戻らず担当moduleへ直接依存 |
| `mcs/semantic/semantic_assembly.py` | fact・根拠・依頼属性・進捗の組立て | 入力のコピーと返値を所有し、DB・推論・時刻・保存状態を持たない。`semantic_extraction`がlive/cache/repairの実行と借用connectionの読取を所有 |
| `mcs/notify/` | 対象選定、spec/part計画、描画、送信許可、journal照合、receipt | `notify_parts`がpart計画、`notify_scope`がscope読取、`notify_actions`が操作適用、`notify_action_tokens`がtoken、`notify_recovery`が人承認復旧を所有。機械の配送事実は`notify_transport`で同一transactionに確定 |
| `mcs/views/` | snapshot読取・scope/projection・統計/詳細/CLI | `registered_data`は登録情報の読取、`clinical_display`は臨床値の表示。DBを再openしない |
| `mcs/signals/` | 原文と抽出に基づく候補評価・根拠 | 候補artifact/通知状態の更新を持つ。表示や単なる未記録と臨床判断を混同しない |
| `mcs/export/` | C1 schema・レコード選択・封筒・受信契約 | 純粋な変換、snapshot読取、専用保存先への受信を区別 |
| `mcs/ops/` | 設定対話・診断・導入・更新・backup/restore・repair・CLI | `setup_wizard`は対話、`lifecycle_io`は秘密値/安全I/O、`runtime_inspection`は読取観測、`update_policy`は判断、`update_journal`は更新記録。実機操作は各ライフサイクル入口が所有 |
| `adapters/common/` | 接続先共通の認可・患者scope・command/preview/confirm・配送worker | workerがscope lock/claim/再試行、registryが永続slot記録、journalが配送事実を所有 |
| `adapters/{slack,discord,lineworks}/` | 接続先固有のUI・SDK・送信/受信 | 承認と共通scope検証を通し、SDKの許可ファイルと遅延importを維持 |
| `hermes_plugin/` | plugin登録・遅延bootstrap・host設定/identity変換・worker接続 | import時点ではadapterを読込まず、正しいcheckoutを解決して接続 |
| `mcs_standalone/` | 独立host・service・Runtime・ネイティブ接続 | Runtimeが子プロセス/slot/停止/回収を所有。共通設定はsetup CLIへ依存しない |

## 主要な依存方向

- 共通commandと認可は`adapters/common/`が正本であり、Hermesとstandaloneがそれを利用します。共通処理はhost pluginからprivate helperを借りません。
- MCSのwireはworkerから`mcs_cdp`/`mcs_download`を直接呼び、親adapterのprivate実装へ戻りません。添付のURL検証は`mcs_download`が共通化し、親だけがtoken取得と最終ファイル置換を行います。
- MCSの値・例外は`mcs_contract`、登録データの検証は`project_metadata_contract`が正本です。読取側が通信クライアントや取得CLIの実装をimportする必要を減らします。
- ルールとLLMは共通の臨床scope/値を利用します。臨床scopeが抽出統括へ戻る循環は作りません。
- LLM統括はschema/検証/品質を呼びます。純粋な検証moduleは`extract_llm`のglobals、endpoint、call notesを操作しません。
- semanticの処理はtransport/store/QC/renderの正本を呼び、CLI moduleへの遅延importやmodule全体の引数注入を使いません。
- semanticのlive/cache/repairは同じ純粋な組立てを利用します。cache再構築は呼出元のconnectionを借用し、擬似writerやLLMの代用品を作りません。chunk・preflight・関係判定が未完了なら再構築成功として返しません。
- 通知のspec計画、同一TXのjournal記録、commit後のatomic publishの順序を保ちます。parts/scopeを分けてもtransactionの所有を分散しません。
- 人承認の復旧は`notify_recovery`から`notify_transport`の借用transaction上の確定処理を使います。復旧receiptと状態を同時にcommitし、spec公開はその後に行います。読取側は`notify_scope`へ直接依存します。
- 薬局の参照項目は`patient_context`、保存済み履歴のSQLは`mcs_queries`が正本です。抽出・semantic・表示から集約writerを経由せずに利用し、解釈に影響する両moduleをsource digestとcandidate定義の変更検知に含めます。
- 表示用のバイタルは`clinical_display`、登録情報のprojectionは`registered_data`が正本です。表示limitでcoverageや全体件数を変えません。

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
