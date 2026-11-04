# 1.1.2 受入記録

2026-11-05時点のリリース準備の記録です。Codexの通知根本対応（notification-root-01）の完了とmainへの統合を待って候補SHAを固定します。この票の準備・合成検証を、本番反映や公開の完了とは扱いません。

## 対象と受入範囲

| 対象 | この記録で固定する状態 |
|---|---|
| 前版 | v1.1.1（tag `0bd4eb9e`、2026-11-04公開） |
| 準備時のmain | `4c6727e272afda7c4c8f2c5e6e82c592f3960bbc`（v1.1.1以降118コミット） |
| 公開候補SHA | 未固定。Codexの通知根本対応完了・統合後に確定する |
| 版区分 | PATCH（v1.1.2）。全変更が互換な不具合修正・内部改善で、非互換変更・新機能なし |
| 変更記録 | `changes/archive/1.1.2/` に28件。対応表は [change-map.json](change-map.json) |

## リリース準備の実施済み項目（2026-11-05）

- 公式医薬品マスター確認: `https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/yFile` を新規取得。ZIP SHA256 `c3efc8aa772b9bf54cfa992e8df3bc82480f86f35bbeb52522d0997144140fb5`・member `y_20261013.csv` が同梱 manifest（`resources/drug-master/20261013/`）と一致。**更新なし・既存bundleをそのまま配布**
- CHANGELOG・README生成ブロック: `release_notes.py build --version 1.1.2 --date 2026-11-05` で生成。要約・比較表3行・説明図を追加
- 説明図: `cloud-continuity.svg` を正本とし、qlmanage+magickで390x860 PNGを生成。文字欠け・重なりの目視確認は未実施（[visual-source.json](visual-source.json)に記録）
- README見直し: `docs/development/readme-review.json` を v1.1.2 の照合結果で更新（5領域・変更不要の根拠を記録）
- 更新経路fixture: `update-paths.json` の target を v1.1.2 へ更新。tag未作成のため同tag schema検証は未実施と provenance に記録
- 静的検査: `release_notes.py check`・`readme_release.py --check`・`update_readme.py --check` を実行

## 未実施・公開前の条件

- Codexの `notification-root-01`（通知待ち5件/未完了部品709件の根本対応）完了とmain統合 → 候補SHA固定
- 全体テスト: Codexが `codex-20261105-full-deploy` で実行中。結果を受入証拠として利用し、失敗項目は修正後の再検証で判定する
- `tests/release/`・`tests/ops/test_supported_update_paths.py` 等のリリース関連テストを最終候補で実行
- Release本文export・画像asset添付・tag付け・公開は、ユーザーの明示指示と候補固定後に実施
- 本番適用は公開とは別工程。適用後の稼働確認を経てから公開する既存方針を維持する
