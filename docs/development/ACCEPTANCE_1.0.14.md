# 1.0.14 リリース受入票

2026-10-06（日本時間）。公開基準は `v1.0.13`、確認開始HEADは
`d2263af95a9127d73a985ebff93f2b168824b515` と開始時の未コミット差分。
**公開・実機反映済み。返信の自動既読化は合成回帰12件で確認。**
下記の準備時記録と、末尾の最終公開・実機反映結果を区別する。

## 返信既読化の条件変更

返信の自動既読化が追加の必須要件となった。2026-10-06のユーザー明示指示
「制限を撤廃してください」により、返信だけsnapshot契約の事前確認条件を撤廃した。
未保存・snippetの先行取得、保存commit、unknown intent、結果再確認を維持する。
収集後に届いた返信も既読になり得る。読取り時に見つかった追加返信は取得ジョブへ
登録するが、この競合窓の未読保持を保証しない。患者単位のsnapshot条件、
人承認・receipt・通信・実データ保護は維持する。実API・実データ・稼働サービスは操作していない。

2026-10-06に認証なしで公開クライアントのJavaScript 221本を取得し、実行せず静的確認した。
`chunk-IGUQKMKD.js` の `Xr.query` は条件を `queryByThread` へ渡し、
`chunk-TJHGJOQA.js` の同関数はスレッドGETへ渡す。収集時点に限定した
既読化の専用操作・サーバー保証は確認できない。静的実装はサーバー実証ではない。
IGUQKMKDのSHA256は `4bde434e942340707edb2d45e1c02bb01e08897574878071440f3151585eca98`。
取得本文は一時領域だけに置き、リポジトリへ転載しない。

## 全体監査の範囲

| 領域 | 状態・根拠 |
|---|---|
| 設計・構造・責務 | 既存全域監査の未変更部分を再利用し、前版以後の主要入口・共有経路を確認 |
| 不要コード・重複・整理 | 互換入口・歴史migrationを維持。リリースに伴う削除・整理を追加しない |
| 冗長な処理・性能 | 新着優先・保存直後再描画・LINE WORKS再投稿抑制の実経路を確認。改善率は未測定 |
| 正しさ・互換性・保存 | 旧版DDLと保存/更新/rollback契約を確認。返信の未保存・抜粋取得、失敗unknownと競合返信jobを回帰検証 |
| セキュリティ・プライバシー | 返信snapshotは明示指示による例外。合成fixture、患者snapshot・人承認・receipt・通信制限を維持。実接続は未実施 |
| 依存・CI・供給経路 | コアstdlib、standalone固定依存、Hermes pinとCI同値。最終SHAのCIは未実施 |
| テスト・build・配布物 | 隔離runnerで検証中。旧監査のSDK/配布物成功を今回の変更へ転用しない |
| 文書・画面・運用・release資産 | READMEの5領域と17変更記録を照合。Slack7組/LINE WORKS2組の生成整合成功。Release生成・公開は未実施 |

再利用元は `full-maintenance-20261005/inventory.json` と同監査の最終受入記録。
REVIEWED/sourceの現存対象と `v1.0.13` のhash一致を確認し、前版以後の61実装ファイルを
別途横断した。変更後の全行独立精査や実機受入の完了は主張しない。

## 導入・更新と復旧

- 新規導入: 既存 `tests/meta/test_install_sh*.py` は隔離HOMEでモード選択・再実行・
  中断・依存失敗・復旧Python選択を検証する。brew/SDKの実導入・サービス適用は別条件。
- 更新元はv1.0.0〜v1.0.13全14版、Hermes14構成・standalone4構成の18経路。
  v1.0.0〜2は手動更新、Hermes v1.0.3〜13は対象版bootstrap、standalone v1.0.10〜13はhost経由。
  schema9を維持し、同schemaのrollbackは更新後の保存記録を保持する。
- 全14版の現tagからledgerと依存SCHEMAのblobを取得し、記録hashとの一致を確認。
  当時の初期化・migration関数だけをASTで抽出し、空のin-memory DBのDDLと
  合成fixtureのDDLを照合して全14版一致。10版のcommit来歴だけを現tagへ修正した。
  過去26経路の旧updater実行結果は履歴であり、今回の再実行結果とは区別する。
- schema変更時のrollbackは損失報告・個別同意・receiptを維持。
  SDK/brew導入の副作用が巻き戻るとは扱わない。
- `integration/test_mcs_recovery_narrative.py`: 6 passed。
  合成の取込・保存・解析・配送・部分失敗・同意待ち復元を確認。
- originsのmetadata契約: 1 passed。実Git更新・host協調停止・実接続・配備は未実施。

## 検証状況

`make lint`、`ci/gates.py`（10/10）、`ci/mine_gates.py --check`、
`release_notes.py check`、`update_readme.py --check`、両galleryの`--check`、
`git diff --check` は成功。初回の `scripts/run_tests.sh -x` は
6247 passed / 6 skipped / 10 subtests passed / 1 failed（398.31秒）。
失敗は `tests/release/test_readme_release.py::ReadmeReleaseTest::test_repository_readme_matches_latest_changelog_and_review`。
版の不一致を検出して停止したため、残りのテストは未実施。skipしたSDK検証の成功も主張しない。
初回はCHANGELOGが1.0.13、先行README見直し記録が1.0.14だった。
1.0.14のbuildと17記録のarchive移動、README生成・同期検査でこの不一致を解消した。
開始時の未コミット変更は保持してリリース候補へ統合する。

条件変更後の `tests/ingest/test_thread_read.py`: 12 passed（0.15秒）。
既読完了、再実行、不完全返信の先行取得、競合返信の登録、失敗unknownの保持、
不正/未完了ページの拒否を合成で確認した。

旧版schema/update経路の対象テストは成功。release検証は23 passedと26 subtests passed。
初回停止後の未実施範囲 `tests/semantic tests/views integration` は
1404 passed / 5 skipped（17.44秒）。standalone SDK未導入と明示SDK laneのskipは
最終SHAの固定SDK/Hermes CIで別途受け入れる。存在しない `tests/standalone` を
指定した試行はexit4/no testsであり、対象を訂正して上記検証を行った。

ここまでが準備時の記録。最終commit・CI・公開・実機反映の結果は次節を正とする。

## 最終公開と実機への反映（2026-10-06）

- 公開commitは `3d9e324bd8fdf493213a4df506d62ccca8edf24a`。
  remoteの `v1.0.14^{}` と一致し、安定版Releaseの公開状態・タイトル・本文を
  CHANGELOG exportと照合した。[Release](https://github.com/yusuketakuma/hermes-mcs/releases/tag/v1.0.14)。
- 同SHAのCIは6ジョブ成功。全量は7668 passed / 11 skipped / 26 subtests passed、
  固定版Hermesは17 passed、standalone SDKは25 passed。
  [CI](https://github.com/yusuketakuma/hermes-mcs/actions/runs/37376299175)、
  [タグ検証](https://github.com/yusuketakuma/hermes-mcs/actions/runs/37377319173)とも成功。
- 同SHAの隔離archiveはPython3.10/3.13で46項目成功、新規導入合成テスト76件成功。
  1015 trackedファイルの内容・権限を照合し、検証後も不変だった。
- 実機はHermes/Slack構成。対象版bootstrapのplanはroute=noop、阻害なし、schema9。
  ソース取得・DB移行は不要だった。既存更新ロックと停止markerを使って抽出を停止し、
  `mcs_setup.py services`で所有サービスを同期してから、抽出ワーカー2本とgatewayを再起動した。
  いずれも旧PIDから新PIDへ変わり、launchdのrunning状態を確認した。
  gatewayのログを本文非表示で照合し、再起動後のSlack接続を確認した。
  残るimportエラーの記録は10月1日〜2日の履歴で、今回の再起動後の記録ではなかった。
- 実機のcheckはexit0、エラー0・警告1。doctorの設定・Python/SQLite・復旧runtime・
  サービス・ローカルLLM診断はhealthy。認証や原本データの全経路受入とは区別する。
  警告はlogin Keychainのロックで、既存の代替認証は設定済み。ロック設定や認証情報は変更しない。
- READMEの機能・画面・導入・安全・文書導線を再照合し、要約の表示上限、患者snapshotと
  返信の例外、7日/最大10件、新返信の競合と追加取得、更新導線を補足した。
  SECURITYの条件も揃えた。生成ブロックとCHANGELOGは不変。
  文書関連テストは23 passed / 26 subtests passed。
- 実MCSへの手動収集・既読化、通知の試験送信、実データの修復・再処理は行っていない。
  通常運用のサービスを再開したことと、操作画面・実配送の受入を区別する。
  タグ内画像の内容一致はAPIで確認済みだが、ブラウザーが利用できずRelease画像の
  実画面表示は未確認。文書修正は今回のローカル差分で、公開タグの付替えは行わない。
