# 1.0.13 安定稼働版の全体レビュー・文書整合

2026-10-04。ユーザー指定は、旧1.0.13〜1.0.15・追加提案・検知済み修正を
一つの1.0.13に集約し、最初に全体レビューと文書整合を行うこと。
この記録は公開前の設計レビューであり、実装完了・公開可否の最終監査ではない。

## 対象・証拠

- main / `fb099ee7f86387d0a60a70329bbdaed7eb08591f`。公開v1.0.12のtagは`fb87584`。
  v1.0.12はGitHub Release公開済み。fb099eeのci・Japanese release notesもsuccess。
- 追跡550ファイル: Python318、shell8、Markdown93。
  mcs72、adapters36、tests176、docs107、deployment20、openwiki24などを全件メタデータで分類。
- [直前の全体レビュー](refactor-1.0.12-20261004.md)の最終ソースと同じHEAD。
  runtimeソース差分がないことを確認し、同一領域の既存証拠を再利用。
  source/testを今回全行読み直したという意味ではない。
- fresh確認は共有取得/job、rule urgency、緊急度表示/通知、送信journal/restore、
  export enum、導入/更新/setup/doctor、CI/依存/配布、各計画と現行ガイド。
  追跡Markdown93件を読み取り、現行・仕様・歴史・生成の区分と参照を調べた。
- 既存Makefileのuv警告修正と未追跡`8`/`recover`を保持。
  私有config、原本DB、患者データ、稼働ログ、秘密値は検査対象にしない。

## 全領域のレビュー台帳

8領域に全追跡対象を分類した。領域の分類は全ファイルの全動的経路を実測した意味ではない。
未実装の計画と現行不具合を分け、制約を成功へ読み替えない。

| 領域・対象群 | 確認・再利用した証拠 | 結果と残る制約 |
|---|---|---|
| 1. correctness/契約: mcs、adapters、plugin、standalone、LINE WORKS | 最終ソースの既存レビューと共有経路の再読 | F-2・F-3、失敗理由の不足。既存restore/actor/却下理由コード等を再実装対象にしない |
| 2. 保存/整合/migration: core、ops、update、配備資産 | ledger・maintenance・bootstrapと過去版更新記録 | #1/#3/#4/#7/#8の残件。実DB整合・端末喪失からの復元は未実測 |
| 3. 安全/権限/プライバシー: 全接続先、export、approval | scope・journal・no-proxy等の既存証拠、exportの現12型照合 | 新規氏名・group・外部契約の有効化は別判断。実サービス設定/送信を検証していない |
| 4. 性能/async/資源: ingest、extract、semantic、notify、worker | deadline・restore/shared workerと既存budget tests | #5/#6・品質/容量実測は未完了。過去の件数・速度を現在の実測として使わない |
| 5. テスト/評価: tests176、integration7、evaluation6、conftest | 現HEADの合成全体4481 passed/4 skipped/26 subtests、同HEAD CI success | sourceを変えていないため結果を再利用。全テスト本文の再読・全分岐網羅ではなく、SDK/実モデル/実接続は別証拠 |
| 6. 文書/利用導線: root、docs、component README、openwiki | 93 Markdownの棚卸し、guideとCLIの照合 | 旧版割当・未公開記述・#19/CLI現状を訂正。生成wikiを正本として手編集しない |
| 7. 依存/供給/運用: workflows、ci、deployment、scripts、依存固定 | 固定SDK、lazy import、CI permissions、bootstrap、runtimeメタデータ | 下記SQLiteの影響版を検出。全外部依存の最新advisory網羅検査、実機scheduled runtime確定は未実施 |
| 8. license/配布/asset: LICENSE、README、manifests、synthetic galleries、archive | 私有ライセンス・生成元/配布境界と既存gallery証拠 | 患者情報を配布・fixtureへ含めない。実画面の新規目視ではない。新CLIはまだ配布していない |

## 指摘と1.0.13への反映

### F-2: 緊急度の誤判定

`mcs/extract/v1/extract.py:304`の部分一致は、
合成の「急ぎではありません」「緊急の対応は不要です」「明日すぐに連絡します」を
全てhighにした。真の至急例もhigh。共有判定は
`mcs/views/structured_view.py:165`、通知昇格は`mcs/ops/mcs_signals.py:1199`。
保存済み世代・再抽出・rollup・表示・通知を含めて修正する。

### F-3: receipt statusの未検査

`mcs/ops/ext_contract.py:531`は他項目一致の合成ackで
accepted/rejected/pending/statusなしを全て受理する。
現LocalSinkはstatusを返さないため、検査だけを足す修正は不可。
C0/C1で送受双方・旧journal互換・rejected終端を定義し、合成で検証する。
現行参照契約が既にstatus必須だったという主張ではない。

### #9: ローカルSQLiteの影響版

実DBを開かず、各Pythonの版とsqlite3.sqlite_versionだけを確認した。

| ローカル入口 | Python | SQLite |
|---|---|---|
| /usr/bin/python3（CommandLineToolsへ解決） | 3.9.6 | 3.51.0 |
| /opt/homebrew/bin/python3（python@3.14へ解決） | 3.14.8 | 3.53.4 |
| repo .venv/bin/python | 3.13.12 | 3.50.4 |

[SQLite公式のWAL-reset bug](https://www.sqlite.org/wal.html)は、
WALで複数接続が同時write/checkpointする条件の破損を説明し、
3.51.3以降・backport 3.50.7/3.44.6に修正がある。
上の3.51.0/3.50.4は影響範囲。破損を観測・再現したわけではない。
本番のscheduled Pythonは今回私有設定を読んで確定しておらず、
Homebrewの新しいPythonがあるだけで本番修正済みとはしない。
1.0.13のinstaller/update/doctorで実使用runtimeを確認し、修正版を満たさない入口を阻害として扱う。

### 既存実装と追加すべき残件

- #3: `mcs/views/mcs_view.py:233`はjob理由がnot_recorded。
  ledgerの永続理由とknown_gapsを追加し、未知/欠落/ゼロを分ける。
- #5/#6: card/adapter restore gateとjournalは既存。
  `notify_flush.py`のtext経路は別であり、残余予算・復元hold・理由の受入を追加する。
- #19: `mcs_operations.py:125`のreason_codes検査、signal_dismiss保存、
  `mcs_signals.py:1436`のfeedback reason_codes集計は既にある。
  自由文だけ・集計なしという旧詳細計画を訂正。解消原因・率/小標本・非干渉の残件を扱う。
- #20: 20-A/B/Cの実装とcanonical/G6/実モデル・容量の未検証を分離。
  新たな臨床判断や閾値緩和を追加しない。
- F-4: 現DETECTORSとexport signal_type enumは12型が一致。
  旧課題を現在の不一致として主張せず、PRESETS/stat/privacyを含む整合回帰を残す。
- B-T: `tests/ops/test_ext_contract.py:346`のtime.sleep(0.02)は競合を確実に作らない。
  signalを先に準備する同期テストへの置換を実装工程で行う。
- doctorは既存。install.sh→init、bootstrap→plan/applyを再利用する統一入口が不足。
  現cmd_doctor→cmd_checkはcheck_environmentも行うため、新入口のlocal/probe境界を実装時に分離する。

## 文書整理・保持と復旧

ROADMAPと全詳細計画、導入/更新guide、公開版受入の現在状態を整合する。
CHANGELOG、changes/archive、仕様、migration/評価の証拠、
ci/mine_gatesが採掘するincident記録、合成gallery、生成wikiは保持する。
当時の失敗・件数・版割当を歴史記録から消して現在の成果へ書き換えない。

削除対象は`docs/dev-records/handoff-1.0.11-claude-20261003.md`の1件、5497 bytes。
完了済み統合作業の一時引継ぎで、現行README/docs/openwiki/tests/ciのbacklinkと
incident IDはない。統合結果・受入・未検証事項は
[merge記録](merge-1.0.11-20261003.md)、
[1.0.11受入](../development/ACCEPTANCE_1.0.11.md)、
[1.0.12受入](../development/ACCEPTANCE_1.0.12.md)と現計画へ引き継ぐ。
Gitの`fb099ee:docs/dev-records/handoff-1.0.11-claude-20261003.md`で復旧可能。
当時のLINE WORKS部品上限・サマリーlock内処理等の未検証は、全通知先/長文/予算の回帰対象とする。
ユーザーの「古い過去の文書は削除して良い」による、復旧可能な不要ソース文書の整理。
データ・archive・共有履歴のpurgeは行わない。

## 初期工程の検証と次工程

文書リンク、生成文書、release記録同期、static/incident gates、diffを確認する。
runtime実装を変更していないため、同HEADの全体テスト成功を再利用する。
checksの実行結果は最終報告に記載し、未実行の実API/SDK/モデル/配備を成功扱いにしない。

次工程は[開発・受入計画](../development/RELEASE_1.0.13.md)の順に実装する。
今回、runtime・患者データ・私有設定・常駐サービス・公開状態を変更しない。

## 実装工程の統合記録

以下は初期レビュー後のローカル実装であり、上の初期工程の状態を上書きしない。
実DB・秘密・患者情報・サービス再起動・公開操作は対象外。全成果物の最終受入は未完了。

### 期限・health・DB関連guard

- `run_check`はstageを計時し、全体期限超過後の追加作業を次回へ残す。
  終了記録は継続し、healthの`run.elapsed_s`・`overshoot_s`・`slowest_stage`で
  超過を`degraded`として説明する。watchdogは猶予を明示した場合のみ有効。
- 関連guardは投稿参照とartifactのproject一致だけを検査し、
  `foreign_keys=OFF`でも作用する。既存DBの初回導入は監査ゼロでもshadowから始め、
  再openだけではenforceへ昇格しない。新規DBのゼロ監査と既存DBの観測期間を区別する。
  違反のある旧行は修復・削除せず保持する。
- 初回統合回帰で2件の旧fixture不整合を検出した。添付の順序テストは
  参照先の合成投稿を追加し、readerのscope検査は関連guardだけを外して
  旧不整合を意図的に注入するよう修正した。判定assertionは保持した。
- 合成runnerでDBguard・watchdog・tick・healthの205件が成功。
  追加の実`_main`経路では仮想時計を期限超過へ進め、
  後続stageの非呼出し・run保存・実health公開を検証して1件成功。
  対象Ruffとstatic gatesは成功。実tickの分布・watchdog猶予・配備受入は未確認。

### 薬剤辞書pipelineと鍵保存service

- `drug_map`の候補導出をLLM抽出後・rollup前に接続し、
  候補の変更・退役対象projectをrollupの再構築対象へ合流する。
  `config.json`の`drug_map.path`と`sha256`を検査する。未承認・不在・無効な辞書から
  候補を公開せず、無効理由は値や辞書内容を含まない固定コードで記録する。
- 完全合成辞書を実loader・derive・現行source参照へ通し、
  rollup前の候補生成と設定削除後の候補退役を検証した。
  pipeline・tick・setup・doctorの合計300件が成功し、対象Ruffも成功。
  本番辞書の保管責任・利用条件・承認は未確定。
- `_keychain_store`は既存serviceを既定のまま、別serviceの明示を受け付ける。
  値は従来どおりstdinだけで渡し、read-backと失敗時の復旧も同じserviceへ束縛する。
  合成Keychain回帰4件・対象Ruff・差分チェックが成功。実Keychainは未使用。

### 並列実装の継続

完了通知ごとに次の独立実装を投入する方式を`AGENTS.md`へ記録した。
統合失敗や依存変更で前提が変わった箇所は計画をレビュー・再設計し、
受入条件と次の投入順を改めて実装する。全員終了待ちの一括バリアは置かない。
最終の全範囲回帰、生成文書同期、独立レビューと実機・外部ゲートは別途残る。

### 横断メンション・しおりの明示入口

`cross_lists.py`へstorage-onlyのCLIを接続した。既存DB・dataset・token cacheと
`--read-only-get`の明示が必要で、writer lock取得前にDB writerやadapterを開かない。
bookmarkedへ根拠のないunread filterを送らず、暗黙login・定期取得・公開を行わない。
完全合成HTTP応答を実worker・adapter・CLI・artifact保存へ通し、
取得・非干渉・snapshot閲覧の69件が成功。対象Ruffとstatic gatesも成功した。
実APIの非空応答・未読true保持・ページ継続の受入は未実施。

### #1 NAS方針と専用復元同意

ユーザーはNAS案を選択した。[方針案](../development/BACKUP_NAS_PROPOSAL_1.0.13.md)
はレビュー用で、実行用policy・実媒体・鍵作成・実患者データ操作の承認ではない。
保存パス・identity・容量・鍵/receiptの別保管・保持/RPOの採用は未確定。
日次snapshotとoffsiteの時刻差だけでもRPO24時間を超え得る点を明示した。

バックアップ設定・healthの297件、Hermes/独立hostのjob所有の210件が成功。
新端末は専用`mcs_restore.py`のplan/approve/resumeでDB・bundle・配置先と
human reason/receiptを束縛し、収集再開とhold_allの配送保留を分離する。
関連471件が成功。readonly preflightはVM/時間上限とmemory tempを適用し、
中断をunknownと扱う。関連141件、最終preflight70件が成功。
実NAS・実DB・Keychain・サービス操作は行っていない。

### 後追い通知・未観測一覧・canonical統合

後追い緊急通知をtickへ接続し、interactive初報は同じwriter transactionの
sealed表示集合だけから証拠を保存する。外scope・不存在IDを証拠に含めず、
実カード受理後のE1・重複回避を含む299件が成功。有効化は既定offを維持する。
本人宛未観測一覧は明示publicationと鮮度を要求するprimary API/CLIへ接続し、
87件が成功。観測不足を未応答・未読・臨床完了には変換しない。

semantic全体は773件成功。最初の13件のguard違反にはfixture不足だけでなく、
global cohortのmessage_id=0と項目receiptのproject_id=0という製品側の参照不整合が
あった。globalはNULLへ、項目は実投稿scopeへ直し、不存在targetは内容のIDを
保持したglobal診断にする。旧行の削除・guard無効化・期待値の弱化は行っていない。
別候補世代のprompt/cache/extraction/stagingを導入したが、旧既定と公開/Loopは
維持し、本番昇格に人手200件以上・G6/calibrationを要求する。

### 統合後の必須ゲート

CIと同じ全対象のRuff、static gates（8/8）、incident記録の
`ci/mine_gates.py --check`、`git diff --check`が成功した。
RuffはPATHとrepo venvにはなかったため、既存のpinned SDK検証venvにある
実行ファイルを使用した。依存の追加やプロジェクト設定の変更は行っていない。
incident台帳の`AUDIT-J04`と`EVAL-J06`は未検証のままであり、
ゲート成功を本番facts網羅性や人間ラベル評価の成功とは扱わない。
隔離HOME・環境と合成DBによる初回全体回帰は5,847件成功、6件skip、
26 subtests成功、2件失敗（355.30秒）。
失敗は緊急度の旧表示期待値と、候補版README見直し記録を公開版の正本へ
先行配置した版不一致だった。表示テストは共通のAI抽出ラベルとの一致へ更新し、
候補記録は`readme-review-1.0.13.json`へ分離した。
公開版の生成要約を保持したまま、正本の5項目を1.0.12に対して見直した。
失敗領域の修正回帰は172件と10 subtestsが成功（1.91秒）。
対象Ruff・README版同期/参照検査・差分チェックも成功した。
修正後の全体回帰は別に実行し、対象領域の成功を全体成功とは扱わない。
#6-D1/D2は質問が30分で未回答となり、best judgmentで継続する指示に従って、
結果不明の自動再送なし・既存heldの個別確認を実装方針として選択した。
明示回答・本番heldの確認・解除承認を得たとは扱わない。

修正後の全体回帰は5,849件成功、6件skip、26 subtests成功（334.23秒、exit 0）。
この結果はその時点の差分に対する証拠で、後続の手渡しCLIと人手レビュー資産の
追加を含む最終差分の成功とはまだ扱わない。
6件skipは通常venvにないDiscord/Slack SDKの4ファイルと、明示SDK laneを
必要とするmetadata/source検査2件。integrationだけの理由確認は6件成功・
5件skip（1.47秒）で、残る1ファイルは実Discord SDKテストだった。
これらは`check_pinned_sdks.sh`のHermes/standalone両laneへ含まれており、
同一SDK関連差分で取得済みの16件/25件・skipなしの成功を再利用する。
この証拠は実ネットワーク・認証・稼働プロセスやLinux CIの成功ではない。
導入の対話テストに残っていた固定sleepは、実プロンプトのpipe可読イベントを
購読して回答する方式へ変更した。導入・receiptの関連35件と対象Ruffが成功した。
receipt仕様は実装済みのaccepted/rejected/delete・独立手渡し・旧journal互換へ
整合したが、C0の数値正規化・本文/患者別coverage・part/profile・fixture固定は
未完了のまま追跡する。C1の手動CLIは独立したローカル実装として継続投入した。

### #6診断の完了と#9の起動前阻害

textの保留理由は保存されていたがhealthには件数しか出ていなかった。
`health.notify.held_reasons`へ既知の安定コード別件数を追加し、未記録・不正JSONは
not_recorded、未知値はunknownに集約する。自由文・本文・IDは出さず、
pendingをheld集計に含めない。関連200件（2.43秒）、対象Ruff、static 8/8、
変更記録検査と差分チェックが成功した。#6のローカル実装・合成受入を完了し、
本番heldの確認・個別解除・実配送は未実施として残す。

#9のdoctorは実行対象を診断するが、servicesは実行ファイルの存在確認だけで
配備へ進み、updateのprecheck_localも選択対象のSQLiteを検査していなかった。
installerのrecoveryもtemplateの`/usr/bin/python3`を検査せずbootstrapする。
これらはdoctorの成功だけでは覆えない起動前阻害の不足として、
既存のbounded metadata probeを共有して修正する独立実装を継続投入した。
実runtimeの自動切替・実機配備・新しいrecovery設定は承認したとは扱わない。

### C1のローカル手動CLI

`ext_contract.py`へdeliver/handoff/reconcile/withdraw/healthを接続した。
旧4フラグ形式とaggregate認可・hash契約を保持し、handoffは自己ackを出さない。
receiptはJSON/NDJSON/ディレクトリから人が取り込み、結果不明の自動再送はしない。
healthは項目数とファイル容量を制限し、本文・actor・秘密を出さず読取り不変を検証した。
関連159件、最終CLI39件、対象Ruff・static/incident・変更記録/差分検査が成功した。
exit 0のhandoffはローカルstaging作成だけで、外部受領成功ではない。
C0の数値・本文/coverage・分割/profile・共同fixture合意、C1の選別/分割・
link-hints・撤回wire・実受信は未完のままで、契約タスク全体は完了扱いにしない。

ユーザーはNAS方針案の最大30bundle・手動削除・週1回verify・
90日ごとの回復鍵訓練を採用した。方針案と継続記録へ反映したが、
RPO・実行時刻・保存先/容量/identity・鍵とreceiptの保管担当は未確定。
実行用policy・実NAS保存・鍵作成・実データ復元・サービス適用は行っていない。

### #30資産化と#20-Eのレビュー待ち資産

#30は26経路の版/runtime/根拠をmanifestに保持し、13公開版16構成を現行の
plan/apply/rollback/bootstrap/reinstallへ通すrepo内の合成資産として完了した。
関連182件と、その後の全体5,849件の成功を再利用する。元runner/raw log・
旧target binary全体の再実行・実Git/host/SDK適用は検証済みと主張しない。

#20-Eは完全創作の220会話を固定し、原文票と合成提案を分離した。
原文・対象文は各220件で重複なし、11領域を各20件含み、全件pending・
promotion_eligible=false・人手検証済み0件を維持する。関連92件、
対象Ruff・AST/JSON・static 8/8が成功した。ツールは本人receipt・モデル出力を
生成しない。人手200件・全G6分母・校正・lifecycle・capacity・昇格は未完了。

C0の継続設計レビューで、旧canonicalの置換による保存hash変更と、
異なるpart集合が同じ旧envelope IDを持つ問題を確認した。
旧契約を保持し、新契約は版・partをID/intentに束縛する修正案を記録した。
wire版はオーナーへ確認中で、旧互換・純粋検証・snapshot選別を独立して先行する。
旧CLIのrecords hash・ID・intentは固定JSON bytesから独立SHA計算したgoldenへ
束縛し、関連39件（0.58秒）と対象Ruffが成功した。
生成器の83module/210testfileへ同期後、文書生成・README版同期・変更記録・
Slack画面資産・差分チェックも成功。これは最終#10レビューではない。

NASのRPOはユーザーが24時間を選択した。`max_rpo_seconds=86400`を方針へ
反映するが、既存の日次snapshotと転送時刻だけで達成したとは扱わない。
新しい静的snapshotを用意する手順・実周期・媒体接続と、保存済み復旧点の
最大年齢の検証を実機受入に残す。実データへの書込み・設定適用は未実施。

C1の純粋検証を新規`c1_contract.py`へ分離した。canonical/profile/7型record、
full本文の対応・8192 UTF-8 bytes/hashを検査し、旧canonical/grant/APIを変えない。
関連279件・独立Bun数値oracle 28,597件・対象Ruff・static/incidentが成功した。
限定数値/Unicode域の検証であり、任意JCSや人承認・失効・実送付の証拠ではない。
wire名・envelope ID・partと既存状態機械への接続は未完了。

#9の起動前阻害を実装・合成受入として完了した。servicesは退役/描画/起動前、
update applyはlock/journal/中断回復前、installerはrecovery配備/bootstrap前に
選択実行ファイルをprobeし、未知・危険なSQLiteを拒否する。親runtimeや
代替Pythonへの自動切替をしない。Ops439件・installer68件・matrix182件、
対象Ruff・shell構文・static/incident・変更記録/差分検査が成功した。
固定system PythonがSQLite3.51.0なら配備は停止する。独立recovery Pythonの
明示設定契約をオーナーへ確認中で、実機の更新・適用は実施していない。

続いてオーナーは新`mcs-ext-export/2`への分離と独立recovery Pythonの明示設定を
採用した。相手側受入・実送付・binary更新・実runtime path・実機配備は承認したと
扱わない。新版認可は明示grant・全患者・鮮度/保持上限に加え、人承認/理由/失効を
既存検査へ接続し、旧envelopeへの漏れを防ぐ。関連118件（0.68秒）、対象Ruffと
static 8/8が成功した。本文例外・snapshotと合せた後続回帰は別途確認する。

本文例外は`message_body`のroot `body_text`だけに限定し、旧経路・他record・
自由項目・nested private keyは引き続き拒否する。新snapshotのPython3.11専用
`typing.assert_never` importは宣言済み3.10下限を超えるため、安定した境界拒否へ
置換した。C1/auth/snapshot/旧receiptとCLIの329件（1.16秒）・対象Ruff・
static 8/8・変更記録と差分検査が成功。実Python3.10や実機配備の成功とは扱わない。

C1 envelope単体は383件と独立Bun hash/ID/intentで成功し、受信referenceの
集合current・item版・限定欠落降格・到着前撤回・保持期限は別単位87件成功。
これらを最終C0共同pin・相手側受入と混同しない。

export/2を既存GovExporterの予約・send-time再認可・typed receiptへ接続した。
最初の8件の失敗は、親の新テストがHandoffSink constructorの既存の空dir生成を
見落としたもの。拒否前後のdir mtime・全内容不変を検査するよう正当に直し、
関連140件（0.86秒）、対象Ruff・static 8/8・変更記録と差分検査が成功した。

snapshot/引数なしprofile/dry-run CLIを接続した。親の初稿は存在しないDATA定数を
使い、8192bytes本文より小さい6000bytes上限を成功fixtureに指定していた。
正本HOME/data/snapshotsと収まる上限へ修正し、/2対応で陳腐化した旧拒否期待は
新版を/1と誤表示した場合の拒否へ更新した。関連458件（5.12秒）、対象Ruffと
static 8/8が成功。旧の固定bytes/hash/ID/intentは維持し、実source/設定は未使用。

オーナーは診療報酬情報提供サービスの医薬品masterを指定した。offline converterは
合成91件成功。指定公開masterをメモリ上で取得し、ZIP hash・42項目19,272行と、
本編16/18/19ページの変更区分・廃止日番兵・一般名codeの定義を確認した。
実masterをrepo/fixtureへコピーせず、実辞書生成・設定/DB有効化は未実施。
出所とpin候補はdocs/specs/official-drug-master.mdに記録した。

回復鍵の媒体はユーザーが封印した紙を選択した。NAS/端末外の媒体方針として
記録したが、実鍵の生成/取得/転記や保管確認・訓練は未実施で、custody成功に
変換しない。担当/場所と信頼済みSHA receiptの保管は未指定。
24時間RPOのローカル能力は明示静的snapshotと複数hourのcronへ拡張し、
関連148件、対象Ruff/shell/static/incident/変更記録/差分検査が成功した。
旧日次設定・6基本ジョブ・opt-inは維持し、実RPO達成を未検証として残す。

## Claude Code引継ぎ後の実装・検証（2026-10-04）

引継ぎ文書の未完16項目をローカル実装・合成検証の範囲で閉じた。実機・実API・人手・
相手repo・オーナー判断の条件は[開発・受入計画](../development/RELEASE_1.0.13.md)§6に残す。

- #9 recovery明示設定unitを親として確認。update-path合成fixtureがrecovery実行ファイルを
  更新対象tree内に置いていた不備を、repo外へ置く形に修正（規則自体は維持）。182件成功。
- C1: 撤回指示書`mcs-ext-withdraw/1`（理由コード4種・4,096 bytes）、`withdraw --generation`
  （journalから展開）、合成参照受信側の`receive`（receipt bundle、受信側障害ではreceiptを作らない）、
  明示opt-inの`--classify-senders`（職種だけでself_orgにしない）、/2でも`forbidden_field`を返す検証、
  C0 fixture一式（受理12・拒否23＋生成のみ15・receipt 6・withdraw 4）とfixture set ID。
- #24×22-F: ケアチーム医師の「見ました」未観測人数（名簿・押下者が完全かつ最新の時だけ）。
- #11: 公式master由来の合成評価（forbid=0）。#24-D1: 氏名保持/表示の明示CLIフラグ（既定off）。
- 最終差分の#10独立レビュー（一度）: blocker F-2の真の至急の取りこぼし、major4件（receiveが
  outbox内receiptを拒否扱い、復元後の新着textの隔離、新規Macでinstall.shが無言終了、実行ビット消失）
  とminorを修正し回帰を追加。relation違反でhealthがdegradedに残る点は、オーナー確認を要する
  意図的なfail-closedとして維持。
- B-T: 対話インストールテストは回答直後のstdin closeで`script(1)`がEOFを先に送る競合があり、
  回答の消費を示す見出しを待ってから閉じる方式へ変更。負荷下8並列で成功。
- Hermes更新（main→local/org-build、c8873b7）の影響調査: plugin ctx APIとSlack `_get_client`は不変、
  更新後HEADで統合レーン14件・mcs adapter/integration成功。Slackのslashは
  `allowed_channels`外・`ignored_channels`内で無視されるようになったが、現設定に該当なし。
  hermes-mcs側の修正は不要。c8873b7は公開remoteにないためCI/installerのpinは変更しない。
- 固定SDKレーン: standalone 25件・Hermes 16件、skipなし成功。

## 差分レビュー・全リポジトリ監査とリリース準備（2026-10-05）

- v1.0.12以降の差分レビュー（10領域×2観点、各指摘を3人で反証）: blocker 0・major 5・minor 20。
  1.0.12からの更新が既定のrecovery Pythonで毎回rollbackする問題（M2）、/2認可のhealth誤判定、
  restore中のalert取りこぼし、緊急度の時刻語による取りこぼし、辞書変更のrollup未反映を含め全件修正し、
  各修正を2人でレビュー。未追跡の`8`/`recover`は所有者確認待ちとして保持。
- 未実装項目の再監査で見つかった16件（恒久的取得失敗の即時failed、取得失敗理由の表示補完、
  保留理由の統一、health watcherの理由表示、期限で後回しにした段のhealth記録、検査値の合成評価、
  C1静的ゲート、LINE WORKS導入案内、非同期テストの同期化など）を実装・レビュー。
- 全リポジトリ8観点監査（バグ・無駄・リファクタ・デッドコード・セキュリティ・依存・仕様・薬剤師価値）:
  2人の反証で確定した約60件を修正、約30件はオーナー判断待ち（既定値・契約・外部受入に関わるもの）。
- テストの隔離事故: init系テストが実`hermes gateway install`を呼び、2026-10-04 23:05に実gatewayの
  LaunchAgentを一時HOMEで上書きした。利用者の`hermes gateway install && restart`で復旧。
  tests/conftest.pyで実hermes/launchctlを遮断（子プロセスはPATHのguard）、該当テストをstub化。
- 生成ブロック・Ruff全範囲・gates 10/10・incident・README同期・Slack画面例・changes・shellcheck・
  固定SDK（standalone 25 / Hermes 16）・全体回帰6,748件成功（skip 2）。CHANGELOG 1.0.13を生成。
