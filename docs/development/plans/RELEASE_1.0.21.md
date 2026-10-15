# 1.0.21 実装・受入計画

基準: `7a4e0c60d9257867f8a31e1c087849e6cf1d17be`。2026-10-13作成・2026-10-14更新。
ユーザー承認: 多角的に精査した全計画の実装と必須検証を完了まで継続する。
この表の未完を省略して版全体の完成としない。公開・本番適用・実データ操作は別ゲート。

## 目的と不変条件

収集・抽出・配送の残件と停滞理由を判断でき、バックアップ・復旧状態を正しく確認できる版。
既存DBスキーマ、CLI/保存形式、承認・配送receipt・認可を維持する。抽出世代は、2026-10-15に範囲へ加えた服薬管理・対象者と測定時点の修正をまとめて1回だけ8（ルール世代21）へ更新し、それ以外の世代（患者文脈3、semantic等）は維持する。
新たな観測の失敗は当該項目を確認不能にし、既存の収集失敗やhealth保存失敗を隠さない。
全体運用情報を患者カードへ露出しない。診断は件数・時刻・固定理由コードだけを返す。
プロンプト、本文、患者識別情報、私有パス、生の例外文を新しい診断へ載せない。

## 全作業の追跡

状態: 未着手 / 実装中 / 部分検証済 / 検証済 / 完了 / 条件待ち。証拠を確認してから完了にする。

| ID | 成果物・受入条件 | 所有 | 状態・証拠 |
|---|---|---|---|
| WP0 | 指標・理由・鮮度・互換性契約と全件追跡 | Codex、Claude確認 | 完了: 本票に指標・理由・鮮度・互換性と全WP/R/Vの証拠/未確認を対応 |
| WP1 | 工程/世代/対象を区別した残数、実測速度、条件付き見通し | Codex | 検証済: 保持済みjobは固定群集計・直近64 ID照合へ。101,000件回帰を含む145件成功、Claude独立受入。累計読取り64MiB超は仕様上の確認不能 |
| WP2 | 履歴遅延の確認済み理由・継続/停止/不明の区別 | Codex | 検証済: 詳細observeで件数・理由を分類、health保存も統合済。進捗差分145件とClaude独立受入 |
| WP3 | reseat全8経路の理由、内訳、旧/不正記録の区別 | Claude実装、Codex統合 | 完了: 元のP2 2件を正規経路＋境界13件で解消。thread保留は実際の解決時に除去し、signal/urgent/parts/unknownの限定6件で独立受入 |
| WP3-D | delivered card / pending intent の有限再現と処置 | Claude実装、Codex確認 | 検証済: parts確定後に次renderへ進む正常経路を確認、強制再送は不採用 |
| WP4 | 前回未完了件数・検出時刻・記録理由の読取表示 | Codex統合、Claude確認 | 検証済: health保存→monitor/doctorと独立レビュー受入。中断なしNone・旧抽出世代の非表示・SQLite例外互換も277件/独立受入済 |
| WP5 | local/offsite区別、局所観測、遅延/中断/再試行/旧backup保持の検証 | Claude、Codex統合 | 検証済: 8bcada00統合、FIFO/巨大印/リンク/走査を有限に制限。関連293件と独立14件成功 |
| WP6 | 軽量doctorとhealth統合、必要な認可済み表示、局所失敗の分離 | Codex/Claude | 検証済: health5群224件・実local helper統合293件成功、既知障害/外側writeを保持。独立受入済 |
| WP7 | 合成基準、時間/メモリ/DB負荷/新着待ちの比較、必要な改善 | Codex、Claude確認 | 合成検証済: 共通出力15組一致、allocation/SQL/WAL、新着+backfill/任意admission、40回反復を比較。本番性能は対象外 |
| WP8-H | Hermes v0.21.7 companion固定、導入/更新/SDK | Codex | 条件待ち: install/updater/CI pin一致。2026-10-14に候補pinを`f36c1da0`へ更新（旧`dc42a048`から作業状態12件を配布treeから除外、実装bytes不変）し、固定SHAが取得できないときに1回再試行する導入修正を追加。f36の固定Hermes SDK laneは21件成功（skip 0）。導入・更新350件と最終書式のinstaller81件は成功、追加差分5ファイルのCodeRabbit指摘/漏れ0、独立レビューACCEPT。旧候補dc42では2026-10-14に最終treeで固定Hermes SDK lane 21件成功（pin archive 18,325ファイル一致）。standalone専用laneも最終treeで12件成功（skip 0）。公開取得は当時HTTP422で未受入、exact CIも未完了だった（その後成功）。2026-10-14追補: companion f36を公開branchからGit経路で取得しSHA/tree一致、Draft PR #54 `eda24601`のrequired CI成功（後続headは別確認、merge/tag/Release/本番は未受入） |
| WP8-D | changes/README/画面例/ガイド/受入票の同期 | Codex、2026-10-14最終集約Claude | ローカル準備完了: 全ファイル見直しの16件を加えた全28件を生成器で未公開1.0.21（2026-10-14）へ再集約し、本文13項目・技術詳細28件・README要約・見直し記録・受入票を更新。同日、導入修正の記録を追加し、全29件（本文14項目・技術詳細29件）とした。独立確認は`v1021-final-docs-check-20261014` |
| R1 | 同値の抽出対象条件を既存定数へ集約 | Codex | 完了: ELIGIBLE_MESSAGE共用、18境界/実呼出を含む194件成功、独立受入済 |
| R2 | pending総数と内訳の母集団を共通化 | Codex契約、Claude利用 | 完了: 総数/内訳/fallbackは同じpredicate。通常signalのNOT(NULL)による数え漏れを修正し、7 payload境界と実経路を独立確認 |
| R3 | patient_context分類定義の整理、表示も保持 | Claude実装、Codex確認 | 完了: b33950bcを独立確認・同一bytesで統合。159別名/属性map/表示見出し・順序一致、6件成功 |
| R4 | 観測/集計/表示の責務、軽量/詳細経路の分離 | Codex | 検証済: 単一読取transaction/SQL・Python期限/サイズ制限を維持し、大量保持jobの固定群集計へ改善。共有64MiBは迂回しない |
| R5 | 残存する不要コード/命名/旧説明の有限整理 | 各所有者 | 検証済: observe説明、共通predicate、extract旧世代説明を更新。旧P15のrepair_prompt修正は既済、対象外 |
| R6 | 関連fixtureのtestkit集約、順序独立性 | Codex | 完了: audit pairをsemantic_testkitへ移動、単独13/統合199件成功、Claude独立受入済 |
| V1 | 関連回帰・必須静的ゲート・相互独立レビュー | 両者 | ローカル検証完了: 各component回帰と相互独立レビュー、全Ruff/gates10/mine/notes/readme/diff成功。全体候補の別ゲートはV2で保持 |
| V2 | 差分レビュー、RC固定、全体/固定SDK/導入更新検証 | 両者 | ローカル検証完了: 初回CodeRabbit 7分割72ファイル（指摘/取りこぼし0）に加え、全ファイル見直しの差分151ファイルを5分割→指摘7件（4修正・3根拠付き不採用）→修正9ファイルの再レビュー0件の2巡。tested `5c8ddcfc`でtests+integration 11,938件成功・終了0・ソース不変（専用SDK3件skipを区別）。固定Hermes SDK laneは旧候補dc42で2026-10-14に21件成功（新候補f36でも21件成功）。standalone専用SDK laneも2026-10-14に12件成功。公開companion取得（当時HTTP422）・exact CIは当時未受入（その後成功）。2026-10-14追補: companion f36を公開branchからGit経路で取得しSHA/tree一致、Draft PR #54 `eda24601`のrequired CI成功（後続headは別確認、merge/tag/Release/本番は未受入） |
| V3 | 公式医薬品master再照合・配布検証・リリース文書 | Claude統合、Codex確認 | ローカル準備完了: 20261013全19,286行converter成功、PDF/termsと原本hash独立照合。2026-10-14 05:47 JSTの再取得でも同じSHA・bundle checker ok。初回18観点を確認し追加対策を独立受入。承認/有効化は別操作 |

## 初回医薬品マスター更新の追加範囲

2026-10-13のユーザー追加指示に基づき、入力から私有辞書、明示設定、導出、表示、切戻しまで18観点を[正式仕様の確認表](../../specs/official-drug-master.md#初回更新で追加した防御と確認範囲)で追跡する。

- 破損DEFLATEの安全拒否、作成基準日/次の見直し日の記録、公式仕様通りの最終日包含を修正。converter/bundle/operator統合100件成功。
- 読取り途中I/O障害の旧候補抑制/原薬剤名保持/復旧とFD解放、日付情報のSHA検証と旧辞書互換を169件で確認。
- 明示drug_map設定の保存前pin検証を327件で確認。未指定init・参考辞書・旧辞書への切戻しを維持。
- Claude独立レビューでinput/runtime/config/最終日の各修正を受入済。statusの独立14条件と文言追補3条件を受入済。未記録/未来の基準日は「未確認」、検証済み基準日で将来境界がない場合だけ「なし」と表示する。
- 強制終了/実機停電・遅い実ディスク・旧通知の遡及撤回は未検証/保証対象外。既存辞書を保持し、私有辞書の承認・有効化を自動化しない。

## 指標契約

- 対象範囲、工程、世代、単位、観測区間、元の観測時刻を識別する。
- 現行成果物の充足率、実行可能な残件、過去の試行実績は別指標。工程間を合算しない。
- 共通の本文対象条件は実行可能性ではない。admission/lease/poison/next_tryを省略しない。
- 速度は同じ単位の有効な完了実績/壁時計の観測区間。worker時間の合計や残数差を分母/分子へ代用しない。
- 世代/対象/区間を束縛できない実績からETAを算出しない。旧記録・速度ゼロ・欠測・打切りは理由付きで不明。
- 一つの内訳は同じDB読取断面で算出。別キャッシュ間は個別counted_atを保持し、全項目同時点とは言わない。
- doctorの追加運用項目は保存healthの軽量読取。既存doctorのruntime/source検査は維持する。詳細observeはmode=roの短い読取transaction、ロック待ち/SQL実行の上限、finallyで接続解放。
- 上限到達を全件成功やゼロへ変換しない。非中断可能なOS I/OをSQL deadlineで即停止できるとは主張しない。
- SQLだけでなくJSON解析後のPython反復にも期限を伝播する。単一行・累計文字量・処理項目数を制限し、
  超過時は部分集計を成功結果へ混ぜない。共有集計のstream化では、通常status表示側も分母と内訳を同じ行集合から導く。

## 追加診断の契約

既存healthキー・overall・state_reasonsの意味と警報抑止は維持。追加項目のエラーはその項目のunknownに限定。
healthファイルがstaleなら、保存された理由を現在原因として使わない。

### 通知

`notify.pending_by_reason`: pending総数と同一母集団で集計し、内訳合計を一致させる。
予定前の除外は明示digest=1だけとし、通常signalがNOT(NULL)で落ちる既存の数え漏れを修正する。
件数増加で警告が出る場合があるが、しきい値・抑止・送信時刻・再送条件は変えない。
理由候補は以下。実装で分岐条件を再照合し、各条件の合成ケースを残す。

| reseat経路 | 理由候補 |
|---|---|
| notice早期保留 | notice_render_live / notice_interactive_off / notice_digest_unset / notice_superseded |
| notice発行後 | notice_render_issued |
| alert結果待ち | alert_outcome_pending |
| alert既存render | alert_render_live |
| alert発行後 | alert_render_issued |
| signal無効・封印済 | signals_off_sealed |
| intent未完了 | intent_cards_unsettled、配送状態の内訳 |
| intent発行後 | intent_cards_issued |

thread_hold/hold_reasonの既存情報も尊重。waitは最終記録理由と時刻であり配送可否の根拠ではない。
更新時刻不一致はstale。一致だけで最新原因とは保証しない。不正JSON/型・未知値は不明へ分類し件数を落とさない。
progressのreceipt、既存キー、不正記録の証拠を保持。終状態でのwait削除は不要だがpending再突入時の誤表示を防ぐ。
長時間停滞、設定による保留、配送結果不明を通常の待機に隠さない。自動再送条件は変えない。

WP3-Dの合成再現結果: card自体がdeliveredでも、そのrenderの本文/添付partsがpending/unknownなら
次renderを発行しない。parts確定後のsweepで追補を含む次renderが発行され、そのreceiptで次intentもacceptedとなる。
これは既存の安全条件であり、force renderを追加しない。診断はcard_parts_pendingとcard_parts_unknownを区別する。
後者は照合待ちとして要確認に含める。実機で同じ原因と断定するものではない。

### 中断

既存crashed/finished_atを読取り、finished_atは検出時刻として表示する。理由未記録はunknown。
一つの推測理由を複数runへ書かない。過去行の一括書換え、新規stack dump、OS起動時刻による断定をしない。
読取りだけで足りる場合はledger.begin_runの署名/動作を変えない。

### バックアップ

`backup.local`: 不在/読取不能/検証記録不一致/記録一致と経過時間を区別する。
marker一致は既存のsize/mtime検証記録との整合であり、今の再検証・耐改ざん・端末外復旧の保証ではない。
毎tickにDB open/quick_check/鍵取得を追加しない。offsite無効でもlocal観測は行える。

## リファクタリング境界

- R1: run_check/read_modelは定数の文字列一致を確認済み。extract(v1)は空白を除けば一致し、境界値で意味の同値を検証する。
  semantic_observeは既にpending_predを共用、置換対象ではない。古い共用コメントも整合する。
- R3: 最終分類、副作用歴→adverse_events、別名全件、表示順/見出し（notify_viewsもLABELSを使う）を保持。
- R5: 動的参照/互換入口を確認してから削除。行数分割、一括rename、新規抽象基盤は行わない。
- R6: 関連テストの共有fixtureのみ既存testkitへ。単独/一括の両実行で順序非依存を確認。
- brain_exportの保持中dir_fdへのfsyncをpath再openのhelperへ置換しない。
  更新中のimport境界、sealed/hash、認可、添付閉じ込めの類似コードを無条件統合しない。

## 検証行列と順序

### 依存関係と着手順

1. 詳細観測と通知記録は独立して進め、関連回帰を先に固定する。完了済みの共通条件の検証を繰り返さない。
2. run_checkとmaintenanceの旧セッション予約は、ユーザーの管理承認後、旧PID/start/PGID全終了・現session不一致を確認し、
   重複4予約だけを正規handoff経路で解除済み。旧taskはpendingに戻し履歴と未完了状態を保持した。
   証跡は`legacy-reservation-release.json`。R1/R2/health統合とlocal backup helperは完了し、受入証拠を保持する。
3. healthの生成・保存・読取り・表示を同じ合成経路で確認する。consumer単体の成功では運用機能完成にしない。
4. R3、公式master、Hermes companionのローカル検証は独立して進める。source統合後に文書・配布物を最終同期する。
5. 差分レビューの対象を固定し、必要な修正が完了してから最終全体検証へ進む。公開/本番の承認待ちとは分離する。

### 観点別の受入条件

| 観点 | 失敗し得る条件 | 合格の根拠 |
|---|---|---|
| 利用者の判断 | 充足率を残数、予定到達を実行中、記録なしを0と誤読 | 工程/対象/世代/単位、個別時刻と不明理由をJSON・文章の両方で確認 |
| 世代と並行更新 | 設定A→B→A、run内設定変更、読取途中の書込み | 混在実績を速度/ETAから除外し、件数と内訳は同一断面を維持 |
| 障害と復旧 | 古いhealth、未記録の理由、収集失敗と観測失敗の併発 | 既知のfailed/degradedを隠さず、追加項目だけunknown。既存警報抑止の意味を保持 |
| 配送と承認 | delivered cardにpending/unknown parts、再開後の追補 | parts確定後に正規receipt経路で進む。unknownを強制再送せず理由を示す |
| バックアップ | 遅いdisk、締切、途中停止、再試行、古い成功記録 | 前回の検証済みbackupを保持、有限合成試験で次回の再試行を確認。local/offsiteを混同しない |
| 負荷と資源 | 大量/長文/壊れたJSON、SQLite待機、長いPython走査 | 期限/容量上限と接続解放、超過時unavailable、同条件の時間・allocation・SQL測定 |
| 更新と互換性 | 新旧reader/worker、過去版更新、辞書版・SDKの差 | 新規導入と対応する更新経路を追跡。履歴・保存形式・承認境界を保持 |

### 証拠と測定上の限界

- 統合8群199件: `~/.local/state/mcs-quality/v1.0.21/focused-observation-tests.log`。
- 観測比較: 同directoryの`observation-benchmark.json`/`.md`。5条件×3関数、warmup1回+計時5回。
  実環境はPython 3.14.7/SQLite 3.53.1（実行ファイル名や過去の測定環境から版を推定しない）。
  共通出力15組一致、Python allocationの減少を確認。enabledのSQL量は減っておらず、CPU高速化・実LLM速度とはしない。
- 継続処理と反復メモリ: `wp7-scheduling-memory.json`/`.md`。新着3件/backfill1件の進行、任意admissionの終端解放、
  observe40回の残留量を測定した。通常の固定slot（RT1/BG2）と任意admission（クラスの同時混在を許さない）は別契約。
  強制GCを反復ごとに実行していない。有限の合成測定を長時間無リーク・遅いdisk・本番速度の保証へ外挿しない。
- health統合5群224件成功。0.25秒/10,000行/単一行2MiBの追加観測上限に達しても、従来COUNT、overall/state_reasons、
  外側write transaction、接続の容量上限とロック待ち設定を保持。local helper実体も統合し、293件でproducerから表示まで確認。
- 公式master取得: `official-master-fetch.json`。2026-10-13T10:41:16Zに新規取得、memberは`y_20261013.csv`。
  ZIPのhash/件数一致だけで全件変換・仕様適合や本番辞書採用を受入済みにしない。

### 必須検証の全体順序

1. 基準/契約/所有を固定→R1/R2と動作保持テスト。
2. WP1〜5を独立実装→各関連回帰→WP6統合。run_check/mcs_queries/共有文書はCodex単独編集。
3. 新旧記録、旧reader、新旧worker、途中終了、rollback後のwait鮮度、破損JSON、観測失敗、並行更新を確認。
4. WP7: 固定seed/規模/短長文/新着+backlogの合成基準を作成。環境・繰返し回数・許容差を改善前に固定。
   p95など標本不足の統計を使わず、時間・RSS・DB負荷・待機を同条件比較。実LLM測定は通常テストから分離。
5. R3〜6採用対象を完了。振舞い変更と整理は分離した差分で確認。
6. release-mcsに従う差分レビュー→修正→RC→全体検証。前回本番SHAを比較元として確認、CodeRabbit分割の対象漏れ/成否を追跡。
7. 新規導入と宣言する全過去版更新を追跡。条件不変の成功証拠は再利用、未実施は明示。

既存runner `scripts/run_tests.sh` で一時HOME/合成DB/スタブのみを使う。
必須: CI範囲Ruff、gates、mine_gates、README生成/Release整合、必要な画面生成検証、固定SDK、導入更新、最終全体テスト。
参照テスト: semantic_observe、health_stalls、run_watchdog、run_check_stages、backup各群、notifyのreseat/hold/receipt、setup_doctor。
レビュー担当は実装担当以外。成功件数だけで範囲全体の証明にしない。

## 別ゲートと未解決事項

- offsite有効化: 保存先、鍵回収、保持、RPO、drill間隔のオーナー判断後。policy/bundle/receipt一致と復元対象外を確認。
- Hermes companion公開/main反映、外部push/PR/merge/tag/Release、本番適用は承認範囲を別に確認。
- GBrainの時間切れはLLM競合の証明ではない。共有資源の観測は単独/競合確認済/不明を分け、通常運用区間を黙って除外しない。
- ディスク整理は所有・未統合変更・復旧を確認する別操作。worktree/backupを自動削除しない。
- 本番適用後24〜48時間（夜間を含む）の観測は別の運用受入であり合成テストで代替しない。
- WP3-Dは仕様通り/再現なし/確定欠陥を記録し、重大な確定欠陥なら公開阻害。仮説だけで独立作業を止めない。

## 完了監査

全WP/R/Vについて成果物と実行証拠を照合する。未確認・条件待ちは完了にしない。
本番操作の未承認を実装の省略理由にせず、可能なコード・検証・文書を先に完了する。
実装成果と公開/運用成果を区別して報告する。
