# Claude Codeへの引継ぎ: 安定稼働版1.0.13

2026-10-04、ユーザー指示「ここまでで中断。claudecodeに引継ぎさせる」により停止。
この文書は引継ぎであり、リリース完了・本番受入・公開の宣言ではない。

## 目的と境界

正本は`docs/development/plans/RELEASE_1.0.13.md`。旧1.0.13–1.0.15・追加提案・
検知済み修正・install/update/setup/doctorの全32成果物を1.0.13で完成させる。
範囲縮小・別版送りはしない。初期38タスクに指示更新/NAS案/recovery明示設定を
追加し、現在42項目。ツール上は26完了・16未完。
最後のrecovery明示設定unitは完成通知済みだが、親の確認とtodo完了反映は未実施。

- coreはstdlibのみ。安全ゲート・人承認/reason/receipt・snapshot timestamp・
  no-proxy/no-redirect・unknownとabsenceの区別・actor privacy・G6を維持。
- テストは完全合成の一時DB/スタブ。実MCS/Slack/Discord/LINE WORKS/Keychain/
  原本DB/ローカルLLM/Jevへアクセスしない。実投稿の匿名化fixtureも不可。
- 実データ・秘密・サービス再起動・配備・外部write・commit/push/tag/公開は
  明示された最終承認なしに行わない。この作業でそれらは実施していない。
- 元からある文書整合差分・Makefile・未追跡`8`/`recover`を保持。他者の変更を
  reset/restore/stash/clean/revertしない。workspace commitは作っていない。
- `AGENTS.md`にユーザーの並列開発/継続投入/継続計画レビュー指示を反映済み。
  agmsgは廃止、使わない。ファイル編集はapply_patch。

## 承認済みの追加判断

NAS案の正本:`docs/development/plans/BACKUP_NAS_PROPOSAL_1.0.13.md`。

- 最大30bundle、削除は手動、週1回verify、90日ごとの回復鍵訓練。
- RPOは24時間。実静的snapshotのパス・生成先行・転送時刻・周期と、
  保存済み復旧点の最大年齢の実測は未確定/未実施。
- 回復鍵はNAS/端末外の封印した紙。実担当・場所・信頼済みSHA receipt保管は
  未指定。鍵生成/取得/転記・custody確認・実訓練は未実施。鍵値をチャット/repo/
  代理人ツールへ送らない。既存keygenは明示human escrowで64文字hexを返す。
- `mcs-ext-export/2`へ明示分離し、旧/1のbytes/hash/ID/intent/grant/journal/CLIを
  保持する方針を採用。これは相手側受入・実外部送付の承認ではない。
- 安全な独立recovery Pythonの明示パス設定を採用。実パス/binary更新/配備は未承認。
- 薬剤辞書はユーザー指定:
  `https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/`の現在の医薬品master。
  自作別名表・別の薬価品目リストへ置換しない。
- #6の質問は未回答でtimeout。その後「best judgmentで継続」の指示に従い、
  結果不明は自動再送なし、既存heldは配送先の個別確認と人判断へ残した。
  実held解除や本番確認を承認済みとは扱わない。
- watchdog猶予60秒は未回答後の暫定選択。0–3600秒設定可能、0で無効。
  元からの直接CLIはopt-in。実wrapper配備は未実施。

## 追跡中の未完16項目

次のラベルを保ち、ローカル実装と本番/人手受入を混同せず進める。

1. #1 暗号化バックアップ と 鍵回復 と 復元訓練 を 実装する
2. #9 明示recovery Python 設定 と 配備対象 の 整合 を 実装する
3. #11 薬剤名正規化 と 曖昧性 の 合成評価 を 実装する
4. #20-D E canonical追従 と 品質容量 G6 を 検証する
5. #22 スタンプ取得 表示 鮮度 と 公開条件 を 完成する
6. #23 横断メンション しおり取得 と 非干渉 を 完成する
7. #24 ケアチーム の 保持閲覧 と 更新状態 を 実装する
8. #25 応答状態 と 自分宛未観測 の 回帰 を 完成する
9. #26 構造化薬歴 観測値 の 取得表示 を 実装する
10. #27 group相談 の 取得契約 と 安全な 関連付け を 実装する
11. F-3 F-4 C0 C1 receipt と export契約 を 整合する
12. B-D 変更後 の 全文書 と 日本語changes を 整合する
13. #10 全変更 と 受入証拠 の 独立レビュー を 完了する
14. 全範囲 回帰 SDK 更新経路 と 必須CI を 検証する
15. 1.0.13 リリース文書 と README見直し を 完成する
16. 未完条件 と 外部操作 の 具体的な 次手順 を 報告する

## 最新の実装と検証

詳細な時系列/失敗修正は`docs/dev-records/stability-1.0.13-20261004.md`。

### backup / runtime

`mcs/ops/mcs_backup.py`は暗号化/HMAC・独立SHA receipt・private policy・VM/時間制限の
readonly plan/preflight・復元新規先・durable backup/verify/drill/custody状態を実装。
`mcs_restore.py`はDB/bundle/marker/destination identityに束縛した専用同意。
approve/resume後も`hold_all`で配送を保留し、collection再開と配送再開を分離する。
関連471件、preflight141/70件、backup設定297件/scheduler ownership210件成功。

RPO cadence追加は`backup.snapshot`（静的絶対パス）と`snapshot_dir`を排他的にし、
単一minute＋重複なし1–24 hourのcronを共有parserで処理。旧daily/6基本job/opt-inを
保持。実renderでfresh・古い/時刻不明・source世代を検証した148件成功。
これは実RPO24hの達成ではない。

危険/不明SQLiteをservices退役/描画/起動、update lock/journal/中断回復、
installer recovery配備/bootstrapより前で阻害。Ops439・installer68・matrix182件成功。

**最終通知されたrecovery明示設定unit（親の確認待ち）:**

- `install.sh --recovery-python ABSOLUTE_PATH`
- `mcs setup init --yes --recovery-python ABSOLUTE_PATH`
- private configの`recovery_python`に保存。既定`/usr/bin/python3`、自動代替なし。
- recovery>=3.9、安全SQLite、repo/update tree外を検査。desiredと実配置を区別。
  所有済み/待機中の旧jobはservicesで修復可能。
- recovery plistは実選択をrender。standalone/Hermes隔離SDK環境で各480件成功、
  Ruff/shell構文/static8/8/incident/diff成功。
- `DEVELOPMENT.md`生成driftが残る。shellcheckの既存`install.sh:734` SC2015は残る。
- 所有7pathは解放済み。変更記録:`changes/20261004-recovery-python-selection.json`。

親の確認用読取りevalは中断後に完了通知を受けた。副作用なし、引継ぎ作成以外の
実装/検証を再開していない。全出力は次に保存されているが、
引継ぎ先で利用できなければ該当3ファイルを読み直す:
`/Users/yusuke/.omo/agent/sessions/--Users-yusuke-.mcs--/2026-10-04T02-14-29-700Z_01a104b0-fa44-7e46-bd59-dce4ae9e495c-artifacts/local/detached-eval-call_GNjnpNZZO2DfsH8qXFkETpw4_fc_07fee2acfe5dea24016ac2513d744c87d0bc4280c05025dc38.log`

### C0/C1

新規pure modules:

- `c1_contract.py`:限定canonical、明示7型profile、body/full messageの対応・8192 UTF-8
  bytes/hash、null保持。279件、独立Bun数値oracle28,597件一致。任意JCSとは主張しない。
- `c1_records.py`:caller-owned snapshot transactionで組立/選別。全source patients、
  floor/upper unknown保持、body/message対、sender_kindはunknown固定。263件成功。
- `c1_envelopes.py`:明示/2、未分割はpart省略、records hash→set→ID。partと版をID/intentへ
  束縛、最終wire bytesで分割、raw parse前1MiB上限、集合不足evidence=None。
  383件＋独立Bunのfixed hash/ID/intent成功。
- `c1_receiver.py`:ReferenceReceiver(root,source_label,...)のprivate保存、完全集合current、
  authに依存しないitem key、A→B→A、限定欠落降格、本文消失、到着前撤回、保持期限。
  receiver/receipt87件成功。diagnosticsは理由/件数のみ、viewはprivate本文を含む。
  **CLIはまだこのreceiverへ接続していない**。

親の`ext_contract.py`統合:

- `load_authorization(c1=True)`で新7型明示・全患者・max age<=3600・retention<=30に加え、
  既存human/reason/expiry/revocationを検査。新grantは旧builderへ入れない。
- root `message_body.body_text`だけの例外。その他/入れ子/旧経路の禁止キー維持。
- `/2` validator/intent dispatch、LocalSink.receive_wire、payloadのみ新canonical、
  journalは旧serializer。journalへexport_contract/part/実型別件数を保存。
- send-time再認可、予約後held/拒否/撤回の再送禁止。新bodyは型別件数不一致や
  旧略式ackではsettleしない。旧binding済みjournal/ackは維持。関連140件成功。
- producer CLI `--c1 --snapshot/--records --only-with-facts --since-days --max-bytes
  --dry-run`。全partを先に検証。新state/outboxは既存所有/private mode/symlink検査。
- 引数なしhandoffはprivate config `ext_export{auth,state_dir,outbox,since_days}`から読み、
  `HOME/data/snapshots/ledger-snapshot.db`を使う。旧明示形式は旧契約のまま。
  snapshot/新旧CLI関連458件（5.12s）・Ruff/static8/8成功。
- `link-hints`はHermes＋stdout TTYだけ、redirect/standaloneはsource読取り前に拒否。
  patient_name/project_id/最終posted_at_ts、既存View._pageで世代束縛cursor。
  file/wire出力・自動patient associationなし。68件（1.28s）・Ruff/static8/8成功。

親による修正:

- `typing.assert_never`をc1_recordsから除去（declared Python3.10下限のため）。
- JSONValueはread-only Sequence/Mapping型へ整合。runtimeはbuiltin JSON treeだけを受理。
  c1_envelopesのboundaryはobjectで実検証し、型エラーをcast/ignoreで隠していない。
- envelope metadataのstr narrowingをlocal変数へ修正。flat importのLSP制約は残る。
- 旧CLIの固定records SHA/ID/intent goldenを独立SHA計算で追加し39件成功。
- `/2`認識後に陳腐化した旧拒否テストは、`/2`を`/1`へ偽装した入力拒否へ更新。
- 失敗した新fixtureは正当に直した: HandoffSinkの既存空dir生成をmtime/内容不変で
  検査、存在しないDATA定数をHOMEへ、8192bytes bodyより小さい成功上限を修正、
  `messages.is_deleted`という不存在列を除去し既存high_watermarkと同じ最終投稿日に。

**C0/C1の重要な残件:**

- ReferenceReceiverをnew local CLIへ接続し、輸送ackとcomplete/currentを混同しない。
- 撤回指示書wire・reason enum・世代撤回・before-arrival/rearrival/partialの接続。
  HandoffSink.discard()はdelete()を呼ぶので、後始末が撤回指示書生成になる設計を避ける。
- sender分類（profession単独でself_orgにしない）、source label/current/retention合意。
- C0 fixture受理12、拒否01–24のうち15は生成のみ（23実ファイル）、receipt6、
  withdraw4、生成器/index/MANIFEST/hash pin・独立受信側一致。件数を縮めない。
- 共同counterpart合意・実受領・本番受入は未実施。現在のgreenはそれらを証明しない。

### official medicine master

正本説明:`docs/specs/official-drug-master.md`。
指定endpoint `downloadMenu/yFile`をメモリ上のみで読取り（実患者データではない）。
ZIP1,157,440bytes、SHA
`820f173981b1601e718ef110ffba55fbea1c04d07abb6a4297b4281db0eecccf`、
member `y_20260930.csv`、cp9326,079,806bytes、19,272行すべて42項目/Y。
変更区分0=18,250、3=1,022、general code/text=7,737、廃止日全行99999999。
実master行をrepo/fixtureへコピーしていない。

一次資料R08rec1の医薬品節16ページ:
0同じ、1抹消、3新規、5変更、9廃止（他masterの復活2を流用しない）。
18ページ:未廃止は99999999、経過措置なしは0。
19ページ:general code/標準記載は一般名処方master由来、なければ省略。
R08rec3の222–223ページが42項目layout。
共通MHLW利用規約は例外なしの場合PDL1.0、出典/加工表示を要求。

`import_drug_master.py`offline converterは合成91件成功。API convert(path,pin)/
write_private/main。pin`mcs-official-drug-master-pin/1`、layout
`R08rec3-medicine-42`、edition/member/as_of/SHA/status_policy/承認metadataを明示。
general prescription/product identityを辞書/2へ分離、旧/1維持、code prefixで成分/YJを
推定しない。最大長かな除外、衝突は複数候補、report-only/明示新規0600出力のみ。
実全件変換/私有dictionary作成/設定有効化は未実施。
status_policyの公式根拠は親がunit完成後に確認済み、未確認値として推測しない。

### canonical評価とその他

220件の完全創作・非医療会話、source/proposal分離、11領域各20件、全件pending、
人手検証済み0、promotion_eligible=false。validator実行済み、関連92件成功。
`evaluation/request_following_review.py validate|export|export-proposals`。
本人200件・全G6分母・実candidate比較・calibration/lifecycle/capacity・昇格は未完。
ツールはhuman receiptやモデル出力を捏造しない。
非医療220件だけで薬剤等の全G6分母が揃うとは主張しない。

他の完成済みunit: deadline/watchdog、shadow/enforce relation guard・監査、hash-only
revision、acquisition契約・persistent reasons/known_gaps、repair plan、typed lab/
drug candidate、role latency/privacy、signal feedback、summary/urgency render、
後追い通知のtick/初報seal接続、cross-list primary CLI、metadata comparisonと
未観測一覧のprimary API/CLI、group scoped guard。
具体的証拠とpathsはstability記録と`changes/20261004-*.json`を読む。
新GET/publication/氏名/groupは既定off・合成受入と実非空API受入を区別。

global cohortのmessage_id=0とitem receiptのproject_id=0は製品側不整合として、
globalをNULL、itemを実scopeへ修正。guardを弱めず正当な不足fixtureを補い、
semantic全体773件成功。後追い通知299件、未観測一覧87件等も成功。
health.notify.held_reasonsは既知コード別件数、不明/不正JSONを安全に集約、200件成功。
#30は26歴史経路/13公開版16構成のrepo合成資産182件成功。
元runner/log、旧target binary全体、pre-release原DDL0–4/6、実Git/host/SDK配備は未検証。

## 検証状態と次の順序

全体回帰の最新成功は**5,849 passed・6 skipped・26 subtests、334.23s**。
これは最近のC1/official importer/recovery extension前。最終差分を覆わない。
6skipは通常venvのSDK不足4ファイル＋明示SDK lane2件。
同一SDK関連差分で、別の固定SDK lane Hermes16/standalone25件・skipなし成功済み。

RuffがPATH/repo venvにない。既存実行ファイル:
`/tmp/mcs-pinned-sdk-Hy9hxx/hermes-venv/bin/ruff`。
通常テストは`sh scripts/run_tests.sh`（HOME/環境隔離）。
SDKスクリプトは`scripts/check_pinned_sdks.sh`、runtime/pinsは既存記録参照。
LSPのflat import/generic既存診断・JSON Biome未導入を成功扱いにしないが、
関係ない新LSP/dependencyを追加しない。新型エラーは修正する。

1. git status/diffと正本・この引継ぎを確認し、既存/並行差分を保持。
2. 最新recovery completionを親目線で読み、対応todoを完了反映。
3. C1 receiver/withdraw/fixtures/分類合意の残件を実装・合成回帰。
   未実装/未合意を既存の多数passで受入済みにしない。
4. official converterの実pin/利用条件/curation、NASの媒体/path/容量/時刻/担当/
   custodyとreceipt、実API/人手200/G6/capacityは独立実装後に具体化して確認。
5. source凍結後、全体回帰・全範囲Ruff・static/incident・generated/README/gallery/
   SDK/更新matrixと必要CIを実行。source変更なしの成功は再利用。
6. 最終#10独立レビューは**一度だけ**。これまでの継続設計レビューは最終レビューではない。
7. 既存release規則で1.0.13のCHANGELOG/README reviewを仕上げる。公開や配備は別承認。

`DEVELOPMENT.md`生成表は最後の同期時83modules/210testfilesで、その後追加があるため
現在drift。`update_readme.py`のgeneratorをメモリ上でrenderしapply_patchで反映する。
純prose用の新テストは追加しない。

公開版CHANGELOGとREADMEのGENERATED:releaseは**1.0.12を維持**、1.0.13 build未実施。
`readme-review.json`は公開版1.0.12の5項目を確認済み。
候補記録は`readme-review-1.0.13.json`へ分離。release build時に最終照合して正本へ反映。

## 中断時の実行状態

- delegate unitsはすべてcompletion通知済み。最後のrecovery unitもpaths解放済み。
- 最新link-hints monitorは68passed/Ruff/static8/8/exit0完了。
- 読取りだけのrecovery確認evalも中断後に完了通知を受け、実行待ちは残っていない。
- ユーザーが中断したので、後着通知で実装・検証・公開を自動再開しない。
- Claude Codeへの別サービス送信・起動はまだ行っていない。このローカル文書を渡す。

## 引継ぎ後の完了状況（Claude Code、2026-10-05）

未完16項目はローカル実装・合成検証の範囲で完了した。外部条件は
[開発・受入計画](../development/plans/RELEASE_1.0.13.md)§6、経過は
[全体レビュー記録](stability-1.0.13-20261004.md)の末尾2節に記録した。

| 項目 | 到達点 |
|---|---|
| 1 #1 | ローカル完了。NAS・鍵custody・実drill・実RPOは外部条件 |
| 2 #9 | recovery明示設定を確認・完了。1.0.12からの更新経路（M2）も修正 |
| 3 #11 | 合成評価（forbid=0）完了。master pin承認・私有辞書作成は外部条件 |
| 4 #20-D/E | 比較・shadow集計資産まで完了。人手200件・G6・容量は外部条件 |
| 5–10 #22–#27 | ローカル完了（医師の見ました未観測人数を追加）。実API受入・氏名等のオーナー判断は外部条件 |
| 11 F-3/F-4/C0/C1 | 撤回指示書・receive・sender分類・C0 fixture一式とfixture set ID・静的ゲートまで完了。相手側合意は外部条件 |
| 12 B-D | 文書・日本語changes整合を完了 |
| 13 #10 | 独立レビュー（一度）と指摘修正を完了。追加でv1.0.12差分レビューと全リポジトリ監査を実施 |
| 14 | 全体回帰・Ruff・static/incident・生成物・README/gallery・固定SDK・update matrixを完了。最終SHAのCIはpush後 |
| 15 | CHANGELOG 1.0.13とreadme-review.jsonを生成（リリース準備）。公開は未承認 |
| 16 | 外部条件と次手順は計画§6と最終報告に記載 |
