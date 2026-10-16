# 1.0.21 実装・リリース候補受入票

2026-10-13作成・2026-10-14（日本時間）更新。ローカル候補の確認記録。**最終RC・公開・本番適用は未受入**。
実装の全範囲は[計画](../plans/RELEASE_1.0.21.md)のWP0–8・R1–6・V1–3で追跡する。

## 対象と公開条件

- 前回公開・本番反映を確認した版はv1.0.20、commit `bbca0e09484f9115e88660ba764a7a79071d88e3`。
- 候補は`feat/v1.0.21-operations`。基準mainは`7a4e0c60d9257867f8a31e1c087849e6cf1d17be`。未コミット・未追跡の追加を含むため、HEADだけをレビュー対象SHAにしない。
- 基準mainにあるカードworkerの設定不足記録の修正も、本版の変更記録へ含める。
- 本票作成時点では、companionの公開、MCSのpush/PR/merge/tag/Release、本番更新・再起動、実データ操作を実施していない。
- CodeRabbit CLIは2026-10-13のユーザー指示で追加確認なしの実行を承認済み。固定差分の分割レビューと必要な修正・検証まで続行する。前版本番SHAから固定候補への差分を対象に、品質改善後・最終RCと高コスト全体検証の前に実施する。前版の分割レビュー成功を本版の成功と扱わない。
- companionの公開取得、固定候補の実SDK、新規導入・更新、全体検証、exact SHAの必須CIが揃うまで公開可能と判定しない。

## 実装の受入範囲

| 対象 | 確認できたこと | 維持する区別・制限 |
|---|---|---|
| doctorの運用診断 | 容量上限付きの通常ファイル読取り、保存記録の鮮度、抽出充足率、工程別待ち件数、中断・backup状態 | 詳細DB集計を毎回追加しない。旧記録・欠測・不正値は未確認 |
| 詳細observe | 読取専用snapshot、工程/世代別の待ち状態、履歴取得遅延、同世代実績からの速度 | 10秒、20万項目、読取本文累計64MiBなどで制限。超過は終了2、部分集計を正常結果に混ぜない（merge後の追補: 最後に読む現行監査充足率だけの超過は、その項目を確認不能とする`partial`で終了2） |
| 処理の見通し | 同一世代の重複しない完了、壁時計の同一区間を使用 | 5分以上・3件以上などの条件付き。新着流入・再試行待ち・失敗・欠測は見通しへ使わない |
| 通知の待ち理由 | reseatの全8経路、総数と内訳の同一母集団、監視警告への表示 | 警報しきい値・抑止・再送条件を維持。通常signalの数え漏れ修正で件数は増え得る。前回partsの確定待ちを強制再送で解消しない |
| health保存 | SQL/Pythonの期限・件数・行容量・lock待ちの上限、外側の書込みと接続設定の保持 | 追加集計失敗は追加項目だけ未確認。元の件数集計が失敗すれば従来のhealth失敗経路を維持 |
| 中断記録 | 過去24時間の検出件数・記録理由、health→monitor/doctor | 検出時刻を発生時刻と断定しない。過去行を書き換えない |
| 日次backup | local/offsiteの区別、通常ファイルのsize/mtimeと検証印の照合、時間切れ後の次回成功 | DBを開かず、marker256バイト/走査256件/0.5秒を上限。単一OS I/Oの強制停止保証ではない |
| リファクタ | 抽出条件の共用、分類159別名と表示順の保持、testkit共用、旧説明整理 | schema・公開CLI・対象条件・fixture境界を保持。行数だけの分割なし |

## 検証結果と独立確認

Python3.14.7・SQLite3.53.1のmacOS環境、`scripts/run_tests.sh`による一時HOME・合成DB・socket遮断を使用。
実サービス、実LLM、Keychain、原本DBへ接続していない。重なるテスト群の件数を合算しない。

| 検証 | 結果・範囲 |
|---|---|
| 観測関連8群 | 199件成功。stored/詳細observe、metrics、進捗、通知内訳、drain世代、fixtureを含む |
| health関連5群 | 224件成功。COUNT維持・外側transaction・設定復元・既知障害を隠さないことを確認 |
| 抽出条件の共用 | 194件成功。18境界とrule/read model/pendingの実呼出経路を含む |
| backup統合後8群 | 293件成功。実helper→health保存→monitor/doctor、期限中断→次回成功、旧backup保持、master未来版境界を含む |
| 分類整理 | 159別名・属性map・表示見出し/順序の同値を比較、関連6件成功 |
| master/installer/CI scripts | 180件成功。全19,286行は別途実converterでRAM上検証 |
| 有限独立レビュー | 観測・health/R1・分類・master・backupを確認。修正前FIFO停止/巨大marker/リンク追跡を再現し、修正後14件成功 |
| 相互レビュー | Codex/Claudeのcomponent正式requiredレビューを受入。最後の通知追補も独立6件で受入。全体候補の未確認条件は別記 |
| 固定候補の全体検証 | `scripts/run_tests.sh tests/ integration/ -q -r fEs`が終了0、失敗0、ソース差分なしで完了（12分46秒）。専用SDK条件の3件はスキップであり成功に含めない |
| 公開取得・exact CI | 未実施。ローカル全体検証の成功を公開取得・固定SDK・最終RC受入に読み替えない（2026-10-14追補: companion公開取得とPR #54 `eda24601`のrequired CIは成功。詳細は「Hermes固定と導入・更新」） |

生の数値、実行条件、source hash、採用/不採用の根拠は非機密のローカル品質記録`v1.0.21`に保持する。

### CodeRabbit分割レビューと全体検証

2026-10-13、CLI 0.9.0で前回本番`bbca0e09484f9115e88660ba764a7a79071d88e3`からの差分を7分割で確認した。各分割の対象は統合後の同じtree `b091b9603966281dfc6b444b864df783e89e2d6c`に固定し、比較元だけその分割を本番状態へ戻したローカルcommitとした。全72テキストファイルが`outcome=completed`・終了0、取りこぼし0、指摘0。修正がないためCodeRabbitの再実行は不要とした。PNGと公式ZIPの2点は、既存の表示・SHA・全行変換検証で確認し、CLIのテキストレビュー件数へ含めない。

レビュー後のローカル検証snapshotは`256695ea9433afc4e85af1d975bc4f2f1baa72b1`。Python3.14.7・pytest9.1.1で全体を一度実行し、終了0と対象ソースの不変を確認した。Slack Bolt未導入のモジュール1件と、明示SDK laneを要求する2件はスキップ。Discord SDK内部の非推奨API警告14件は保持し、テストや警告条件は変更しない。本節などの結果記録だけを検証後に更新した。

companion固定SHAはリモート取得確認でHTTP422（該当commitなし）だったため、当時は公開供給とexact CIが未受入だった（その後の公開取得とrequired CIの成功は「Hermes固定と導入・更新」に記録）。追加課金・push・公開・本番変更は実施していない。

### レビュー後の追加修正

Claudeの独立レビューで、完了ジョブが蓄積したDBの全件読取りにより、詳細observe全体が件数上限へ達する問題を合成101,000件で確認した。件数集計を固定16群、直近実績は最大64 ID/記録の世代照合へ分け、145件と独立再レビューで受入した。旧抽出世代の充足表示、中断なしの表現、SQLite例外属性の互換性も277件と独立レビューで確認した。累計64MiB上限は維持するため、保持payloadが上限を超えれば詳細observeは毎回確認不能となる（merge後の追補: 超過が最後の現行監査充足率の集計で起きた場合だけ、その項目を確認不能とした`partial`で終了2）。軽量doctorとは別経路であり、実機の保持量は未確認。

通知診断の独立レビューでは、予定前の通常signalがNOT(NULL)で件数から落ちる問題と、過去のthread保留が現在の配送結果不明を隠す問題を修正した。正規経路・7種のpayloadなど13件で確認し、追加したthread復旧の処理はsignal/urgent・期限後fallback・parts receipt・同じreceiptの再適用を含む6件で独立受入した。初回の現役保留を残し、配送先が確定した時点で保留キーだけを除去する。しきい値・再送・receiptの判定条件は維持するが、従来の数え漏れが件数に加わるため警告が出る場合がある。

## 全ファイルの追加レビュー・リファクタリング

前節のCodeRabbit・全体検証は追加作業前の候補を対象とする。追加作業の基準snapshotは `306ee8a069bfc749d1e2d0870a367fc38b595a56`。当初1,652ファイルを台帳化し、OpenWiki配下24件を除く1,628件を領域別に確認した。OpenWiki更新workflowも参照対象から除外する。実行sourceの本文確認、テストのAST・契約確認、文書・変更記録の構造検証、公開原本のbyte照合を区別し、全行精読・実機検証と同一視しない。新規ファイルは最終台帳へ追補する。

この追加作業で、LLM予約前の入力検証、日次backup検証印と状態記録の読取り、医薬品参照名の検証、Jev応答のJSONエラー処理を共用した。不正なQC項目による表示エラーと、観測終了時刻NaNの統計算入も修正した。既読化途中で失敗しても、先行ページで取得できた返信IDと未読の根拠を取得予約へ保持する。失敗した既読化を確認済みにせず、失敗ページの内容やprocess crashの窓は推定しない。

私有辞書・修復記録・参照統計の保存/復旧では、directory同期と論理的な書込み結果を区別する。保存後の同期失敗は作成済み出力を保持し、再試行による既存ファイルの上書きを避ける。特殊ファイルや上限超過の評価入力は一部だけを採用せず拒否する。Release同期の拒否テストは不意の書込みを確実に検出するようにし、子プロセスの終了テストは孫の起動を確認してから実施するようにした。

**追加候補のローカル固定レビュー・全体検証は完了。** 最終台帳は1,689件、OpenWiki関連25件を除く1,664件（新規37件）で、対象漏れ・未処理0。各ファイルの確認深度とSDK実行制約5件は区別して保持する。

CodeRabbit CLI 0.9.0の初回は追加差分151テキストファイルを5分割して完了。7指摘のうち4件を修正し、3件は固定候補のコード・生成器・合成テストを根拠に不採用とした。修正した9ファイルを3分割して再レビューし、全scopeが終了0・outcome=completed・対象漏れ0・追加指摘0。初回と合わせ2巡で終了した。Codex/Claudeの独立レビューと限定追補も受入済み。

全体テストの固定snapshotは `5c8ddcfc7c791bc1c85b5d1036e5960aa1743807`、treeは `39f91ef470ad7df564bb76485019394cb8219997`。`scripts/run_tests.sh tests/ integration/ -r fEs` をPython3.14.7・pytest9.1.1で1回実行し、`11938 passed, 3 skipped, 14 warnings, 41 subtests passed in 735.54s (0:12:15)`、終了0・実行前後の対象ファイル差分0を確認した。所要時間は12.5分。スキップ理由と警告内容は別記し、未実行を成功へ含めない。静的チェック9種も成功し、変更していない対象の成功結果は再利用した。本節の結果記録だけを全体検証後に更新した。

追加の復旧同期は一方の失敗後も残りを試し、新規辞書ディレクトリは各親を公開前に同期する。収集のDB保存失敗を認証再試行へ隠す提案は不採用とし、当該実行を失敗で止める。ただし保存できなかった返信IDを後続tickで必ず回収する保証、実停電・実機disk性能は未検証。

非機密の詳細はローカル品質記録 `v1.0.21-full-refactor` のファイル別coverage、修正前/後log、指摘採否、独立受入へ保持する。追加の挙動変更16件は `changes/` に記録し、2026-10-14に未公開1.0.21へ集約した（「文書・全体監査の扱い」）。companion公開供給・exact CI・専用SDK・配備条件は引き続き別工程であり、今回のローカル成功をリリース受入に読み替えない。

## 性能・メモリの有限測定

- 共通出力15組は基準7a4e0c60と一致。2,048件の合成入力でcurrent_qualityのPython割当peakは427.6→58.1KiB、observeは441.6→73.3KiB。実測対象関数の比較であり、プロセス全体のRSS削減率ではない。
- 8,192件・機能offの件数集計はpeak1590.4→4.6KiB。enabled時のSQL8,193回は変わらず、高速化全般を主張しない。
- WAL並行write5回は読取りsnapshotを維持して完了。40回のobserve反復の残留量も比較したが、長期メモリリークがない証明ではない。
- 新着3件/履歴1件の選択を保って全16件完了。通常RT1/BG2枠と任意admissionの直列契約を分け、待機終了後の再開と占有0を確認した。
- 実機の残数・現在速度・長時間稼働・低速ディスク性能は本測定から推定しない。

## 公式医薬品マスター

2026-10-13に[公式menu](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/)と[yFile](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/yFile)を新規取得。固定URL、no-proxy/no-redirect、容量・時間上限を使用した。

| 項目 | 確認値 |
|---|---|
| 版・件数 | 20261013、19,286行、全行42項目 |
| ZIP | 1,158,682 bytes / SHA256 `c3efc8aa772b9bf54cfa992e8df3bc82480f86f35bbeb52522d0997144140fb5` |
| CSV | `y_20261013.csv`、6,083,884 bytes / SHA256 `6fa8f08ca4341ac530e5f4081d3fd3bbebbfcdd278c0d0ec0f172a0c1af018d1` |
| 一次仕様 | R08rec3は前回同一。R08rec1の改版と利用条件を取得し、医薬品節の変更区分・日付番兵・期限なし・一般名省略条件を確認 |
| 実変換 | 全行をruntime converterへ通し、12,195 entries・約5.14MB（作成日metadata追加前の測定）。`terms_unconfirmed`で保留、容量上限内 |

2026-10-14 05:47（JST、`checked_at` 2026-10-13T20:47:48Z）に同じ条件で公式yFileを再取得し、HTTP200・1,158,682 bytes・同じSHA256・member `y_20261013.csv`（6,083,884 bytes）で10/13の受入版と一致した。`check_drug_master_bundle.py`もok（19,286行・`R08rec3-medicine-42`・`activation:false`・人承認必須）。公開前の再確認として2026-10-14 09:45（JST、`checked_at` 2026-10-14T00:45:17Z）に同じ条件で公式menu（HTTP200）とyFileを取得し、HTTP200・1,158,682 bytes・同じSHA256・member `y_20261013.csv`（6,083,884 bytes）で同梱版と一致したため、更新なしとして既存bundleを配布する。統合後の公開前にも2026-10-14 11:36（JST、`checked_at` 2026-10-14T02:36:35Z）に同じ条件で再取得し、menu・yFileともHTTP200、SHA256と`y_20261013.csv`が同梱版と一致した。

拡張候補 `c48fb07d` のリリース準備として、2026-10-15 18:47（JST、取得時刻 `2026-10-15T09:47:45Z`）に公式menu・yFileを新しく取得した。no-proxy/no-redirect、45秒・32MiB上限で両方HTTP200を確認し、ZIPの1,158,682 bytes・SHA256、member `y_20261013.csv`、CSVの6,083,884 bytes・SHA256は上表および同梱manifestと一致した。更新はなく、20261013版を継続同梱する。現在のruntime converterを使う同梱検査も全19,286行で成功（`status:ok`、終了0）。利用条件・採用判断の入力がない変換は引き続き`terms_unconfirmed`で保留され、私有辞書は作成・有効化していない。この確認は公式配布資産の照合であり、実LLM評価・最終RC・公開の受入を意味しない。

旧20260930原本は保持。[manifest](../../../resources/drug-master/20261013/manifest.json)の`activation:false`と人承認必須を維持する。
配布原本の検証であり、私有辞書の採用・承認・有効化・実データ再処理は行わない。公開原本の行をfixtureへ転用しない。

### 初回更新の追加対策

[18観点の確認表](../../specs/official-drug-master.md#初回更新で追加した防御と確認範囲)に取得・破損・日付・identity・容量・設定・導出・cache・切戻し・中断の根拠と制約を記録した。

- converter/bundle/operatorの100件成功。破損DEFLATEの安全拒否、基準日/境界の記録、最終日を含み翌日から除外する仕様を検証。
- loader/runtime169件成功。取得済みFDのI/O障害を無効辞書として扱い、旧候補を抑制・原薬剤名保持・復旧後再照合。旧形式の出所情報は書き換えない。
- 設定切替327件成功。明示指定時だけ保存前に実体/SHAを確認し、失敗時のconfig・secret・共有設定の無変更、参考辞書と旧版切戻しを確認。
- 上記はClaude独立受入済。statusも独立14条件を確認し、文言追補3条件を受入した。未記録/未来の基準日は「未確認」、検証済み基準日で将来境界がない場合だけ「なし」と表示する。
- 電源断・実機disk性能・配布済みsnapshot/通知の遡及変更は本試験で保証しない。私有辞書の採用・承認・有効化は未実施。

## Hermes固定と導入・更新

推奨候補は公式v0.21.7 `71d34bad1045610252eda02ee79549436896e784`を含むMCS companion `f36c1da0016ce14c9c531cb9514ab4b0b667240d`（tree `cce192e4fbd754a0a33c9d4fc84b5b9c43f18444`）。2026-10-14に旧候補`dc42a048bff5fcc545a2f1a1d3982292c13eb098`から作業用のsession/state/監査ログ12件だけを配布treeから除外した。実装bytesは不変で、元のローカル内容とGit履歴は保持し、開発規約・agent選択設定3件は非機密のため残した。新候補は当時未公開で、同日07:39 JSTに公開取得を確認した（下記）。以下の`dc42a048`の記録は旧候補に対するもの。
install・updater・CIの固定値を一致させ、従来の`6f35b1ed`を認識する。既存3.14環境のHermes checkoutを自動変更しない。

既存pin準備の独立受入と、companion契約19件・固定SDK21件・Hermes関連74件・MCS導入更新228件の成功証拠を対象同一性に応じて再利用する。公式版単体の2件不適合（本人確認context/認証stdin）はcompanionで解消。全Hermes suiteを実施済みとはしない。

2026-10-14 05:47（JST）の公開GitHub APIによる`dc42a048`の取得はHTTP422（No commit found）で、当時は公開供給が未受入だった。同時刻以降、最終tree（tested `5c8ddcfc`から実行・テスト・配備・resourceの775パスに差分なし）で`sh scripts/check_pinned_sdks.sh hermes`を実行し、`21 passed, 42 warnings in 27.70s`・終了0。隔離Hermes venv（Python3.14.8）のcheckoutはCI pin `dc42a048`のarchive全18,325ファイルと一致し、秘密envなし・テストのsocket遮断を維持した。これはローカルの固定SDK確認で、公開取得・exact CIの代わりではない。standalone専用SDK laneも最終treeで`sh scripts/check_pinned_sdks.sh standalone`を実行し、`12 passed, 14 warnings in 3.72s`・終了0・skip 0（新規の専用venv、Python3.14.8、`deployment/requirements-standalone.txt`とpytest9.1.1の実distribution一致、完全合成・一時HOME・socket遮断、775パスに差分なし）。警告はSDK内部の非推奨APIで、内容を保持する。新候補`f36c1da0`でも同じ固定Hermes SDK laneを2026-10-14に実行し、`21 passed, 42 warnings`・skip 0・終了0（16.68s）で、pinとsourceの一致を確認した。旧候補の契約19件は同laneに含まれるため、重ねて再実行していない。この時点では、公開取得とexact CIは新候補でも未受入だった。

リリース経路の監査を受け、新規導入で軽量cloneから通常cloneへ切り替わり固定SHAがブランチに含まれない場合だけ、同じoriginから固定SHAをfetchして1回再試行する修正を追加した。取得できない場合は実行環境の作成前に停止し、接続と公開状況の確認を案内する（変更記録`hermes-companion-supply`）。installerの焦点テスト3件（ローカルbare Gitと外部stubで、通常cloneへの切替後の取得成功・取得不能での停止・再開）は成功した。導入・更新の関連6ファイル350件は成功。ShellCheck対応の同値な条件式整理後、最終書式のinstaller全81件も再確認した。Ruff 0.17.0のCI全範囲、gates 10/10、mine_gates、ShellCheck、shell構文も成功。2026-10-14の追加修正依頼では、固定対象 `0201dcd4`（比較元 `88620eb4`）の5ファイルをCodeRabbit 0.9.0で初回レビューし、`outcome: completed`・指摘0・対象漏れ0・終了0。追加修正はなく再レビュー不要。omoの独立レビューもACCEPT。根拠はローカル品質記録 `v1.0.21/blockers-20261014/`。

新候補`f36c1da0`は2026-10-14 07:06 JST時点の公開GitHub APIでもHTTP422（該当commitなし）で、当時は公開操作が未承認だった（07:39 JSTの公開取得は下記）。新規導入・過去版更新は[origins](../../../tests/fixtures/schema_upgrade/origins.json)と[更新経路](../../../tests/fixtures/schema_upgrade/update-paths.json)で全件追跡する。v1.0.20起点のschema-only抽出で新shape-11（schema9、trigger変更1件/index追加2件）を追加し、全21版・32経路を347件の合成回帰で確認した。既存の旧版fixture・履歴26経路は保持する。最終候補の公開配布・exact SHAの必須CI・サービス受入は別工程。実API/サービス再起動と合成入口の検証は区別する。

2026-10-14 07:39 JST、companion `f36c1da0`はforkの新規branch `mcs-pin/v0.21.7-20261014`で公開済みとなり、認証情報を無効にした公開branchのSHA照合と、SHA指定のfresh fetchでSHA・tree `cce192e4`の一致を確認した。匿名GitHub APIはrate limit（HTTP403）のため、Git経路の取得を証拠とする。MCS候補はDraft PR #54（head `eda24601`、base `7a4e0c60`）で、07:56 JSTにrequired workflow（ci・Japanese release notes）が成功した。これは`eda24601`に対する結果で、後続のheadは別に確認する。この時点では、main merge・tag・Release・本番適用は未受入だった。

## 文書・全体監査の扱い

前版の全体監査と公開receiptを区別して再利用し、全追跡ファイル群の同一性、今回差分と共有経路を8領域へ対応づけた（final-audit-delta）。追加master修正・通知追補・生成文書のhashと採否を最終差分記録へ対応づけた。未知の依存条件や未確認の配布経路を「問題なし」にしない。

READMEは機能・画面例・導入・安全・導線の5項目を照合済。Slack8画面のカードUIは今回変更せず、運用診断の説明図を別に準備した。画像の外部公開・Release実表示は未実施。2026-10-14、全ファイル見直しで追加した16件を含む全28変更記録を、生成器（`release_notes.py build`、版1.0.21・日付2026-10-14）で未公開の1.0.21へ集約し直した。既存の12件はarchiveから戻して同じ手順で再生成し、本文は利用者の目的ごとに13項目へまとめ、28件すべての仕様・根拠は技術詳細の折りたたみに残した。README要約は`readme_release.py`で再生成し、同じCHANGELOGからRelease本文をexportした。旧版本文・旧版archiveは変更していない。同日、リリース経路の監査で追加した変更記録`hermes-companion-supply`を、既存28件のarchiveを変えずに加え、全29件（本文14項目・技術詳細29件）とした。merge後のレビュー（v1.0.20→main `f0646a3f`）で採用した修正はPR #55（`1dc65b03`、Codex独立受入・CodeRabbit承認・必須CI成功）で取り込み、その変更記録4件を既存29件のarchiveを変えずに加え、全33件（本文18項目・技術詳細33件）とした。続いて全PR・全worktree・remoteの統合（PR #57）で回収した22件の変更記録を、既存33件のarchiveを変えずに加え、全55件（本文23項目・技術詳細55件）とした。
