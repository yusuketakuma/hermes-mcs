# 1.0.17 リリース受入票

2026-10-09（日本時間）。前公開版はv1.0.16（`746ca0d6fde61fc1f6ec3aa5146f48815132d837`）。安定化ブランチを、既存Slack hotfixを保持してmainへ統合したソースは`38b43643ee5eb73bb19e417d6b5b4df7ea3da9e2`。新機能を加えない。本文はローカル監査・合成受入の根拠と制約を記録し、公開や本番の安定稼働をこの票だけで完了扱いしない。

## 全体監査と再利用した証拠

基準は[1.0.16受入票](ACCEPTANCE_1.0.16.md)と参照先の全体監査。全追跡ファイルと追加資産を私有inventoryで追跡し、各ファイルの現在hashと根拠を照合した。統合時の1,508ファイルは直前の検証済みソースと全件一致。現在sourceの手動確認、差分・共有呼出元の確認、テスト実行のみの証拠、過去計画・歴史記録の範囲限定の確認を区別する。未読を全文精査済みと扱わず、全ファイルの再読を主張しない。

| 領域 | 確認対象・受入 |
|---|---|
| 無駄・リファクタ | core/ingest/extract/semantic/views/ops、adapter/plugin/standaloneの入口と共有経路。既存helper・stdlibを使用し、機械分割・新規依存・将来機能を追加しない。ショートカットは支持するwriter/安全契約と容量上限を確認して保持 |
| フォルダ・ファイル | flat import・互換入口・生成元/画像・配備パスを維持。履歴migration、過去archive、公式公開原本、私有実データを混同しない |
| 性能・負荷 | 高密度本文の重複走査、receipt走査の寿命、既存owner枠、再処理の予算を確認。モデル/並列枠/日次上限を増やさず、有界な再構築を使用。実運用の改善率は未測定 |
| 正しさ・互換・保存 | 深いJSON・大整数、本文/患者/世代束縛、測定値と予定、原文・修正結果・Loopの整合、旧schemaの通知証拠、transaction・保存失敗・冪等性を失敗再現と回帰へ照合 |
| 安全・プライバシー | 復元同意と損失報告/backup、復旧一時配置の所有、失効カードとproject scope、秘密のエラー表示、tenant/receipt/添付、no-redirect/no-proxyを維持。PHI・実投稿をfixtureへ使わない |
| 依存・CI・供給経路 | stdlib coreと固定SDK/pin、公式wheel/sourceのhash、CI権限を照合。固定Hermes実SDK21件、standalone実SDK25件のオフライン受入を再利用。2026-10-09にGitHub Advisory Databaseで独立実行の直接/固定推移依存14pinを照合し、該当範囲0・未解釈範囲0。未公開脆弱性の不存在は主張しない。最終SHAの必須CIは公開前に別途照合 |
| テスト・導入・配布 | マージ後のtests/integration一式10,157 passed・11 skipped・34 subtests passed。新規導入は隔離HOMEと合成設定・stubで配置/失敗/再実行/診断を確認。公開済み全更新元を個別に追跡し、v1.0.16の実DDLを更新元へ追加 |
| 文書・画面・運用 | README5領域、Slack先頭8画面とLINE WORKS、旧緊急度ラベル、メニュー順、本文/添付の契約を照合。SVG/PNG/hashと視認を区別し、架空の説明図を実画面と扱わない。リリース本文は56記録の技術根拠と個別適用条件を保持して利用目的別に編集 |

## 主要な修正と保存する境界

- 遅延Slack返信を元スレッドへ新規投稿し、チャンネルへbroadcastする。更新・再試行・過去の一斉再送とは分離し、配送証拠・保留・単一attemptを保持。
- 壊れた保存JSON/IDで収集・抽出・通知を止めず、最新の読取り不能な連携サマリーを古い登録内容で置き換えない。未確認と登録0件を区別。
- 本人/他者・時点・引用、血圧の片側や長い数値、care_eventと予定を整合。保存済み集約と旧投影でも不一致値を現在値へ戻さない。
- 修正後の原文とcanonical結果・表示・依頼候補を束縛。失敗済み/人の確認待ち/修正済みの旧結果は自動再修正しない。正式な依頼・通知履歴は保持。
- 更新・バックアップ・復旧の終了/保存/個別同意を確認し、失敗・中断を成功扱いしない。所有を証明できない一時配置・未追跡ファイル・過去ログは自動削除しない。

## 公式医薬品masterの当該リリース確認

[公式menu](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/)の現在適用R08医薬品は20260930版・19,272行。固定URLの[yFile](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/yFile)をno-proxy/no-redirect、1応答32MiB・全体90秒の上限で新しく取得した。

| 検査 | 結果 |
|---|---|
| ZIP | 1,157,440 bytes・SHA256 `820f173981b1601e718ef110ffba55fbea1c04d07abb6a4297b4281db0eecccf`。同梱と一致 |
| member/CSV | `y_20260930.csv`・SHA256 `ac1e7ab8db086e31b117fe2df2563d500859de9220a80ccf9b746b62b1d5a73c`。同梱と一致 |
| 仕様・利用条件 | 現行R08rec3/R08rec1と厚労省PDL1.0参照を再取得・照合。manifestの参照確認日を更新 |
| 運用境界 | 原本を継続同梱。`activation=false`、operator承認必須。私有pinや辞書を作成/有効化しない。公式行をfixtureにしない |

## 新規導入と全過去版更新

[origins](../../../tests/fixtures/schema_upgrade/origins.json)と[update paths](../../../tests/fixtures/schema_upgrade/update-paths.json)を正本とする。v1.0.0〜16の全17公開版、Hermes17経路とstandalone7経路を追跡。v0〜2の手動条件と、standaloneのhost起動条件、schema番号0〜4/6の完全な原DDLが証明されていない既存の制約を保持する。番号や新規現行形式だけで旧版互換を代用しない。

新規v1.0.16の更新元には21の回帰parameterを追加。`tests/core`と`tests/ops`の隔離一式は3,065件成功し、fixture/source hashとartifactを親が照合した。

v1.0.16 exact tagから空の合成DBだけを用いて取得した[shape-10](../../../tests/fixtures/schema_upgrade/shape-10.sql)はschema9のまま`notification_cards.layout`を追加する。他の既存定義はshape-9と正規化一致を照合し、現行codeから架空の旧DDLを作らない。SQLite・旧列/添付/FTS・backup・migration・journal・同意付き復旧を実コードで確認し、Git/host/サービスはstubで分離する。

新規導入のinstaller・設定・サービス・診断と、合成取込→保存→解析→通知、復元の保留/部分失敗は既存integrationで確認する。実brew導入・実認証・実MCS/LLM/Jev/チャット操作・実host停止は合成成功から推定しない。

## 最終検証・公開・本番の区別

最終source SHAの必須CI（lint-test、hygiene、incident-gates、standalone-sdk、hermes-integration、readme-sync、Release Notes Check）成功、タグの対象SHA、下書き/公開本文を公開前に確認する。Release本文はこの版のCHANGELOGからexportし、別作文やtag付替えはしない。

本番適用はユーザーが2026-10-09に明示承認。原本DBの事前backupをprivate配置で検証し、公開後に既存の排他/処理停止/反映/再起動を行う。実データ・患者名・秘密値を公開記録へ含めず、既存の返信や成否不明配送を一斉再送しない。gateway接続・所有worker・取得/抽出/配送の稼働証拠と、長期運用の安定性は別々に判定する。元のHermes agentコード・依存・設定は編集しない。

ローカル検証はPython3.11.15/SQLite3.46.1、Linuxのネットワークなし隔離環境。source/toolchain読取り専用、CPU1・RAM2GiB・process128・file64MiB・scratch4GiB・期限付き。11skipは成功に含めない。macOS/SQLite3.53.1での実業務、臨床精度・網羅性・長期安定稼働をこれらの成功から推定しない。
