# 1.0.29 受入記録

2026-10-22。状態は**公開前・最終検証中**です。比較元は公開版v1.0.28
（`6940d135f0e642b932b7164869f199e7cef89503`）、本番の実行ソースは`main` `8971a025`です。
公開候補は`release/v1.0.29`（`e42e9593`にリリース文書を加えた最終SHA）です。候補の検証、
本番適用、稼働確認、GitHub公開を別々に確認し、途中の成功を公開の受入へ読み替えません。
最終SHA・CI・本番操作の欄はリリースの進行役が記入します。

## 取り込んだ変更

| 区分 | 内容 |
|---|---|
| 品質改善の統合 | G1〜G4、shortcut-debt 13件、chat-origin-auth、当初の01〜10（05・06はG4に含む）を`integrate/v129-wave1`へ統合 |
| レビュー指摘の修正 | CodeRabbit採用4件、ponytail-reviewの回帰テスト追加1件 |
| 全域監査の修正 | 監査の4件（未取得本文の既読化、Slackの接続失効後の送信、添付保存のdir fsync、予定と実施の時系列）とCodeRabbit指摘1件 |
| 追加2項目 | 収集エラーの分類と未取得本文の滞留観測、保存済み添付の読取り専用点検 |
| 指示ファイル | AGENTS.mdの整理、CLAUDE.mdの追加、OpenWiki（`openwiki/`と更新workflow）の廃止。実行時のコードは変えない |

変更記録34件と公開本文16項目の対応は[対応表](change-map.json)で全件確認しました。

## 検証済みの範囲

| 検証 | 結果 |
|---|---|
| 全体suite（追加2項目を含む候補） | `0581fcb1`で`scripts/run_tests.sh tests integration -ra`を実行。17,152件成功・失敗0・skip 2（専用の固定SDK lane）・subtest 41、終了0。警告14件はDiscord SDKの非推奨通知 |
| 全体suite（品質改善の統合） | `4b43e708`で失敗0・終了0 |
| 最終候補との同一性 | `e42e9593`の実行コードとテストは`0581fcb1`と同じ。差分は指示ファイル・OpenWikiの削除・テストのdocstring 1行で、実行時の挙動は変わらない（進行役が確認） |
| CodeRabbit | 統合差分（`e4906760`→`4b43e708`、151ファイル）は1回目5件中4件採用、2回目0件。監査修正（`5e8bb5c0`→`59b93be5`）と追加2項目（17ファイル）はそれぞれ2回目0件 |
| 独立レビュー | 統合のCodeRabbit修正、ponytail-review指摘、監査4件と追補、追加2項目の各実装をClaude・Codexが相互に受入 |
| 静的・生成 | make lint・安全gate・事故由来gate・README生成・変更記録の検査が終了0（各段階）。リリース文書後の最終確認は進行役が記入 |

確認は合成データと一時DBだけで行いました。実MCS・実チャット・ローカルLLMの応答品質、実機での添付点検・診断表示は、この表の範囲外です。

## 公式医薬品マスター

2026-10-22 15:21（JST）に[公式のdownload menu](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/)を新しく確認しました。令和8年度版の医薬品マスターは最終更新日令和8年10月13日・19,286件で、同梱の20261013版と同じ記載です。

`downloadMenu/yFile`をプロキシ無効・リダイレクト拒否・サイズ上限付きで取得したZIP（1,158,682 bytes）は、SHA256 `c3efc8aa772b9bf54cfa992e8df3bc82480f86f35bbeb52522d0997144140fb5`、member `y_20261013.csv`で、同梱版とバイト単位で一致しました。`check_drug_master_bundle.py`は終了0（layout R08rec3-medicine-42、19,286行、`activation: false`、operator承認必須）です。更新はないため、同梱版をそのまま配布します。利用条件のページと一次仕様書は今回見直していません。

## 導入・更新経路

[更新経路manifest](../../../tests/fixtures/schema_upgrade/update-paths.json)は、更新元29版（v1.0.0〜v1.0.28）・現行48経路と、過去の根拠26経路を区別します。v1.0.28の正式tagの原本DDLから作った合成DBで1.0.29への経路を確認します。旧版のupdater本体を実行した検証、実Git・実機の配備は含みません。経路の検証結果は進行役が記入します。

## 依存と供給元

Node.jsを使う唯一の箇所だったOpenWikiの更新workflowを廃止しました。最終候補のGit上に、Node.js・npmを使うworkflow・scriptと`package.json`は残っていません。Hermes companionの固定commit（`2c08ca7`）と固定SDKは1.0.28から変えていません。

## 説明図

新しい診断の流れを[説明図](collection-diagnostics.png)（SVG原稿から描画、390×760）にしました。PNGを原寸で開き、390px幅で文字の欠け・重なりがないことを確認しました。根拠のソースとhashは[visual-source.json](visual-source.json)にあります。実画面や実測結果ではありません。CHANGELOGの1.0.29（新機能「収集エラーの内訳と未取得本文の経過時間を確認」の直後）と、同じCHANGELOGからexportするGitHub Release本文に、公開tagの安定URL `https://raw.githubusercontent.com/yusuketakuma/hermes-mcs/v1.0.29/docs/releases/v1.0.29/collection-diagnostics.png` で掲載します。tag `v1.0.29` のpush前はこのURLに到達できません。公開後のURL到達とGitHub Release上の表示は進行役が確認します。

## 未完了・別工程

| 工程 | 状態 |
|---|---|
| 最終SHAの必須CI | 未記入（進行役） |
| 本番適用 | 未実施 |
| 稼働確認 | 未実施 |
| tag・GitHub Release・push | 未実施 |
