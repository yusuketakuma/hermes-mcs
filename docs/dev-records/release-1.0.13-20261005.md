# 1.0.13 リリース準備記録 — 2026-10-05

依頼: 全worktreeをmainへ統合し、repository-release skillで1.0.13を公開、本番へ適用。
文書作業は隔離worktree、開始HEAD `36ee3e3af5b91dca6ccdff229aa80506049d4d98`、編集前clean。

## 統合と監査の再利用

main祖先にある古いworktreeは重複再適用しない。release-1.0.11のdirty53ファイルは全てmain祖先4978c96と完全一致、roadmap20のOpenWiki blockもmainへ統合済み。dirtyは保持。
親の統合treeと監査record H3b58ba7の一致、最終COMPLETE receiptと独立レビュー受入を[受入票](../development/ACCEPTANCE_1.0.13.md)へ束縛。

## 生成とREADME照合

公開前1.0.13候補は既にCHANGELOG/archiveにあり、直接buildは既存版を拒否するため隔離stagingで候補の記録を再集約。
既存88件＋追加43件＝131件を既存build（version1.0.13/date2026-10-05）で生成し、結果だけを正本へ反映。旧1.0.12以下・既存archive88件の内容は保持。
最初のstagingはリンク安全検査で外向きsymlinkを拒否し、正本未変更で終了。trackedファイルのコピーへ変更してbuild成功。
READMEは10分収集の既定・表示/押下者・本人操作・外部送信/未取得/候補の境界・復旧Python・全旧版更新入口・文書導線を照合。SDK境界/隔離配布物の合成受入完了と実API/人手/実運用の別条件へ訂正。

## 本文・画像・受入の検証

文書作業のチェック: release_notes.py check、readme_release.py --check、update_readme.py --check、Slack7組/LINE WORKS2組の生成器 --check、git diff --check は全てexit0。既存archive88件と移動43件のbytes、1.0.12以下CHANGELOGの完全保持をassertで確認。Releaseタイトル/本文をCHANGELOGから /tmp/mcs-release-1.0.13-final-{title.txt,body.md} へexport済み。

runner初回はPATHのPython3.14にpytestがなくexit1。既存runnerのMCS_TEST_PYTHONに /Users/yusuke/.mcs/.venv/bin/python を指定して隔離再実行し、tests/releaseは22 passed・26 subtests passed、exit0（1.97秒）。成功結果を初回環境失敗へ上書きしない。全体runtime/SDK/archive結果は受入票の既存証拠を再利用し、文書編集から実機成功を推定しない。
本番Python3.11 whole runnerはhermes_yaml不足でintegration collect失敗を保持（親実行）。tests/結果・最終CI・公開・本番適用は親の後工程。
commit/push/tag/公開/サービス再起動・実データ操作は文書担当では実施していない。
