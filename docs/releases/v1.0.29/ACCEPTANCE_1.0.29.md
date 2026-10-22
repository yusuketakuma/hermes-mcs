# 1.0.29 受入記録

2026-10-22。状態は**公開前・最終検証中**です。比較元は公開版v1.0.28
（`6940d135f0e642b932b7164869f199e7cef89503`）、本番の実行ソースは`main` `8971a025`です。
公開候補は`release/v1.0.29`（`e42e9593`にリリース文書を加えた最終SHA）です。候補の検証、
本番適用、稼働確認、GitHub公開を別々に確認し、途中の成功を公開の受入へ読み替えません。
最終SHA・CI・本番操作の欄はリリースの進行役が記入します。

## 最終候補の検証追補（公開・本番適用前）

`576d42c63c3828a56509c10c36bc3311d20bfa89`のcleanな隔離checkoutで、
固定Python 3.14.8・pytest 9.1.1・ruff 0.17.1を使い、6 jobを直列実行しました。
全体は17,176 passed・2 skipped・41 subtests、macOS運用は4,405 passed、
standalone固定SDKは12 passed、Hermes固定SDKは28 passedです。
残りのhygiene・incident-gatesも終了0で、前後のcheckoutはcleanでした。
Hermesの固定refは`2c08ca7ade8f867d07e4bff6581be54405b0c9d0`、uvは0.13.1です。
Ubuntu runner固有の差は未検証で、hosted CI成功とは扱いません。

各jobは成功しましたが、最後の報告行だけがmacOS bash 3.2とUTF-8環境で
変数名を誤解釈し、runner全体は終了1でした。`${sha}`へ明示した1行修正と、
成功・失敗の両方の終了コード、SHA、保存記録を確認する回帰を追加し、
関連16テスト・ruff・shellcheck・diff検査は終了0です。Claudeの独立読取りレビューも受入済みです。
6 jobの処理と製品実行コードは変えていないため、上記の成功結果を再利用します。
runner全体を再実行して終了0を得た記録へ読み替えません。

導入・更新fixture関連は505 passed・41 subtests、生成・変更記録検査は終了0です。
前回本番`8971a025`からの最終CodeRabbitは2回実施し、採用した指摘は修正・回帰確認済みです。
2回目の未採用2件は、未取得本文を既読にしない契約と、本文のない統計項目を除く契約の保護が理由です。
この受入票は適用前の検証記録です。最終SHA、更新ツールの結果、稼働観測、
hosted CI未開始時の理由、公開後の画像表示は、進行役が別の配備記録へ残します。

## 取り込んだ変更

| 区分 | 内容 |
|---|---|
| 品質改善の統合 | G1〜G4、shortcut-debt 13件、chat-origin-auth、当初の01〜10（05・06はG4に含む）を`integrate/v129-wave1`へ統合 |
| レビュー指摘の修正 | CodeRabbit採用4件、ponytail-reviewの回帰テスト追加1件 |
| 全域監査の修正 | 監査の4件（未取得本文の既読化、Slackの接続失効後の送信、添付保存のdir fsync、予定と実施の時系列）とCodeRabbit指摘1件 |
| 追加2項目 | 収集エラーの分類と未取得本文の滞留観測、保存済み添付の読取り専用点検 |
| 指示ファイル | AGENTS.mdの整理、CLAUDE.mdの追加、OpenWiki（`openwiki/`と更新workflow）の廃止。実行時のコードは変えない |

変更記録35件と公開本文17項目の対応は[対応表](change-map.json)で全件確認しました。

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

## 公開前の差分レビュー追補

前回本番`8971a025`→候補`f3006e67`のCodeRabbit初回は7件（重複1件を含む6項目）。抽出完了の件数表示、ローカルCIの版読取り、拒否後のスタンプ表示、保留通知の不要な再公開、テストの権限設定を修正しました。患者コンテキストの重複集計という指摘は、通常の書込みが同じkeyを削除後に置換するため不採用です。保存済みのscan結果は原文・契約が変わった直後には次のworker更新まで古い状態を示し得ます。healthの観測値を、現在の候補承認や公開可否の証拠として使いません。最終レビューとCIの結果は別の配備記録へ残します。
