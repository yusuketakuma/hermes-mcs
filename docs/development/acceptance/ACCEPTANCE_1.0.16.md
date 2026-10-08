# 1.0.16 リリース受入票

2026-10-08（日本時間）。前公開版は `v1.0.15`（`7ae6ee47210608759bd14b7b2ed86aa73606b1c2`）、準備開始時のソースは `5c4702af682701b084f11630625a395d4ab85b80`。本票は隔離環境でのローカル受入とソース照合を記録する。最終SHAの必須CI、タグ、公開本文は公開前に別途照合し、この票だけで公開完了とはしない。本番checkout・稼働サービス・実データを変更する作業ではない。過去ログは保持する。

## 全体監査

基準は[1.0.15の全体受入](ACCEPTANCE_1.0.15.md)と、その参照先のround3/round2全体監査。追跡対象の現SHA256を過去の各ファイル証拠と照合し、同一内容だけを再利用した。開始時の1405対象のうち827件で基準hash一致を確認。変更・新規対象をhash一致やテスト成功だけで全文精査済みとは扱わない。停止中の別の全域品質改善計画は再開せず、リリースに必要な共有入口と全8領域を確認した。

| 領域 | 確認対象・状態 |
|---|---|
| コードの無駄・リファクタ | 収集、台帳、抽出、意味解析、表示、通知、各接続先、standaloneの共有入口を確認。既存helper・stdlibを優先し、行数だけの分割や履歴削除は行わない |
| フォルダ・ファイル | flat import・互換入口・実行領域・配備パス・生成元/生成物・変更記録の置き場所を照合。公開masterを配布資産とし、fixture・私有辞書と分離 |
| 性能・負荷 | 一覧の照合を200件ずつにまとめ、未公開の職種集計を省く。完全合成の比較で同一結果を確認。実負荷・メモリリーク・臨床精度の保証とはしない |
| 正しさ・保存・互換 | 用量の同じ事実の引用との照合、新旧投影・保存済み集約、日時と整数の境界、通知世代、配送結果の順序、旧台帳migration、更新と復旧を合成回帰で確認 |
| セキュリティ・プライバシー | scope/tenant/hash/世代、no-proxy/no-redirect、保存先制限、snapshot、人承認/reason/receiptを保持。患者原文・実master行をfixtureに使わず、秘密値・実データ操作なし |
| 依存・CI・供給経路 | core stdlib、既存固定SDK14pin、Hermes固定refとinstaller/CIの一致を確認。2026-10-08にGitHub Global Security Advisoriesで14pinを再照会し一致0。未知・未収録の問題の不存在を主張しない。追加依存なし |
| テスト・配布・導入更新 | OS隔離・空環境・合成データで既存runnerを使用。新規導入と全公開版からの更新を別々に追跡。実SDKは最終SHAの既存CIで確認し、実接続・実機導入は別条件 |
| 文書・画面・運用 | README5領域、導入/更新/復旧/安全ガイド、説明図、master出典、変更記録と生成器を照合。Slack8・LINE WORKS2のSVG/PNGを同期。説明図と実画面を区別 |

確認した実行Pythonは148ファイル。接続/通知側65、core/ingest/ops37、extract/semantic/views45、共通bootstrap1の各領域を対応付け、同一hashの基準証拠と現内容・共有呼出元を組み合わせる。sourceの確認範囲と、全追跡ファイルの全文再読・別の品質改善計画の完了は区別する。

## 指摘と解消

- 投稿の人物・時点、胸痛/片側血圧・薬剤の根拠、次回訪問予定の書式を修正。旧結果は通常の世代更新で再処理し、未記載や未取得を推測しない。
- 元スレッドがない患者アラートは、配送済みの自分の投稿を再利用し、同じ状態で作り直さない。
- 薬剤の用量を事実監査と公開前の決定的な照合へ含め、旧投影/保存済み集約/read_modelから旧値が現在値として残る経路も閉じた。未知用量・legacyの契約を維持し、履歴は残す。
- 通知先の世代を送信直前に確認。長いDiscordタスク一覧を分割し、各操作を残す。不正な型の配送フラグを再公開する。
- 配送開始より先に走査された結果を保持し、走査窓を循環する。結果保存失敗時も処理上限32を超えない。未知の送信を未送信と推測しない。
- 並列意味解析の日次上限超過を合成再現し、予約前の保存済み使用量と予約書込みを同じtransactionへまとめる。枠不足・不明な使用量は送信せず待機し、試行回数を消費しない。
- 極端な監視時刻でも警報を継続し、取得応答の整数保存境界、大量の旧台帳更新、保守走査の間隔を修正。

lost-renderの患者scopeについては、通常の限定患者チャット読取りから私有復旧IDを取得する経路や新たな権限昇格を確認しなかった。同じsystem許可principalは既存仕様で更新・復元等の端末全域の承認権限を持つ。私有復旧IDをoperatorへ明示して行う操作と通常の患者閲覧を区別し、global通知との互換性を推測で変更していない。

## 公式医薬品マスター

当該リリース作業で[公式menu](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/)と現在適用の[yFile](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/yFile)を新しく照合。20260930版・19,272行・member `y_20260930.csv`。bounded/no-proxy/no-redirect取得ZIPの1,157,440 bytesとSHA256 `820f173981b1601e718ef110ffba55fbea1c04d07abb6a4297b4281db0eecccf` は同梱manifestと一致し、更新なしを確認した。原本全行を実converterのRAM内検証へ通し、bundle checkerは成功。`conversion_held=terms_unconfirmed`、`activation=false`と運用者承認必須を保持。私有pin・辞書・本番設定の作成や有効化はしない。

## 新規導入・全過去版からの更新

新規導入は一時HOME・合成設定・stubでinstallerのHermes/standalone選択、配置・再実行・失敗、初期設定、サービス描画と診断を確認。合成の取込→保存→解析→通知、復元の同意待ちと部分失敗は既存integrationで確認する。brew/SDKの実導入、実認証・MCS/LLM/Jev/各通知先への接続、実hostの協調停止は成功扱いしない。

[更新元manifest](../../../tests/fixtures/schema_upgrade/origins.json)と[更新経路](../../../tests/fixtures/schema_upgrade/update-paths.json)は公開済みv1.0.0〜15全16版、Hermes16＋standalone6の22構成を追跡する。v0〜2は手動、v3〜15は対象版bootstrap、standaloneのv10〜15はhost経由で、外部applyは拒否する。Git/process/serviceはstub、SQLite・元の全列/添付/FTS保持・backup・locks・journal・migration・中断/再実行・同意付き復旧は実コードを使う。

v1.0.15のexact tagから空DBのschemaを隔離環境で生成し、shape-8の全既存定義の一致と加法索引6件を確認してshape-9へ保存。schema番号だけで旧fixtureを代用しない。公開版全件と既存pre-release形状のmatrix238件が成功。未確認のschema0〜4/6の完全原DDLという既存originの制約は保持し、非互換downgradeを無条件に安全としない。

## 検証の制約と公開条件

ローカル機能検証はPython3.11.15/SQLite3.46.1の既存Dockerイメージを使用。外部通信なし、空の許可環境、source/toolchain読取り専用、scratch限定書込み、CPU1・RAM2GiB・process128・file64MiB・時限付き。scratch容量は製品の容量ゲートを満たす4GiBとし、合成ランチャーはその中だけで実行可能にする。実行資産や依存を新たに取得しない。実機で必要な安全なSQLite/runtimeの診断成功をこの環境の成功から推定しない。

初回一括は一時領域不足で中断、初期fixtureでは現行投影版の指定不足、合成実行ファイルを実行できないsandbox設定、実Git作業ツリー依存、生成文書driftを検知した。失敗を成功へ置き換えず、環境・fixtureの原因を確認してから再検証する。期待値・旧版拒否・安全ゲートを弱めない。

最終一式・lint・static gates・生成/ノート/画像/リンク・実SDK専用CIと公開結果は、固定した最終候補SHAに対して確認する。タグ前の必須CIとタグ専用チェックを省略しない。本番更新・再起動・ログ削除・実データ再処理はこの受入の実施範囲外。

## ローカル受入の確定結果

- 全量: 9,592 passed・11 skipped・34 subtests passed、配送結果保持の旧期待3件を検知。製品codeを変えず、実grant/receiptとrunner並行処理へfixtureを整合し、該当3件を含む22件が成功（0.75秒）。同一のruntime source・依存・条件にある他の成功を再利用し、未変更領域を再実行して新しい成功件数を作らない。
- 旧版更新matrix238件、用量/抽出/読取り2,265件、日次上限31件、receipt関連324件、core/ingest1,920件が成功。重複する対象を合算して全量件数にしない。
- CI範囲ruff・shellcheck・差分空白検査が成功。追跡1419対象の保護パス/secret-patternの一致0。新規受入文書等も公開前に同じgateへ通す。
- SDK未導入とOS/runtime固有のlocal skipは残す。固定SDKとHermesの受入は既存CIのexact最終SHAへ委ねる。
- 公式利用条件ページも2026-10-08に再確認し、PDL1.0参照・出典/加工表示と例外を確認。これは運用者による採用承認ではない。
