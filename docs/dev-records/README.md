# dev-records — 開発記録の索引

1.0.13の[開発・受入計画](../development/plans/RELEASE_1.0.13.md)は当時の記録として残している。1.0.13・1.0.14は公開済みで、各版の変更は[CHANGELOG](../../CHANGELOG.md)を正本とする。


当時の判断・検証・レビュー結果を残した証跡。現在の仕様や操作手順の正本ではない
（現行は `docs/development/DEVELOPMENT.md` とコード）。ファイル名は変更しない — 他文書が
`file:line` で引用している。CI（`ci/gates.py`・`ci/mine_gates.py`）はこのディレクトリの
`.md` / `.json` だけを許可し、再帰的に走査する。

| 記録 | 日付 | 内容 |
|---|---|---|
| [stability-1.0.13-20261004.md](stability-1.0.13-20261004.md) | 2026-10-04 | 安定稼働版の全領域レビュー、既知修正、CLI、文書整理と制約 |
| [auto-update-plan.md](auto-update-plan.md) | 2026-09-24〜2026-09-28 | 実装済みの自動更新・復旧の設計とレビュー履歴（旧 `docs/` 直下から移動） |
| `r00-baseline.md` | 2026-09-20 | リファクタリングの基準点（R-00 manifest） |
| `phase-r-record.md` | 2026-09-20 | Phase R（全体リファクタリング）の記録 |
| `phase-j-record.md` | 2026-09-20 | Phase J（Jev 意味評価レイヤー）の実装記録 |
| `continuation-20260920.md` | 2026-09-20 | 継続実装・検証台帳 |
| `refactor-revalidation.md` | 2026-09-21 | オフライン再検証台帳 |
| `g1-validation.json` | 2026-09-21 | G1 ゲートの対象 hash |
| `acceptance-map.md` | 2026-09-21 | 受入条件対応表（旧 `docs/` 直下から移動） |
| `deployment-candidate.md` | 2026-09-21 | 候補版の切替・停止・復旧条件（旧 `docs/` 直下から移動） |
| `continuation-20260923.md` | 2026-09-23 | 継続記録 |
| `review-20260923.md` | 2026-09-23 | 実装差分レビューと修正 |
| `signal-priorities-20260924.md` | 2026-09-24 | シグナル優先順位 1–8 のレビュー（技術要件のみ） |
| `review-20260925.md` / `coverage-20260925.json` | 2026-09-25 | 全体レビューと対象一覧 |
| `refactor-20260927.md` / `refactor-20260927-progress.json` | 2026-09-27 | 全体確認とローカル改善、確認一覧 |
| `extraction-review-20260928.md` | 2026-09-28 | 抽出レビュー対応 |
| [stability-20261001.md](stability-20261001.md) / [確認一覧](stability-20261001-progress.json) | 2026-10-01 | 全リポジトリの安定性改善・導入手順の整理と検証 |
| [hermes-sdk-20261001.md](hermes-sdk-20261001.md) | 2026-10-01 | Hermes固定版・実Discord/Slack SDKの隔離検証14件成功 |
| [openwiki-update-path-20261001.md](openwiki-update-path-20261001.md) | 2026-10-01 | 生成brief・次回生成要件と外部送信の境界 |
| [lineworks-20261001.md](lineworks-20261001.md) | 2026-10-01 | LINE WORKS独立接続・アダプター整理・オフライン検証と導入条件 |
| [lineworks-review-20261001.md](lineworks-review-20261001.md) | 2026-10-01 | LINE WORKS全経路・既存接続・導入の再レビューと修正・最終回帰 |
| [lineworks-impact-20261001.md](lineworks-impact-20261001.md) | 2026-10-01 | LINE WORKS追加・adapter整理の他領域への集中影響レビューと回帰修正 |
| [repo-review-20261001.md](repo-review-20261001.md) / [確認一覧](repo-review-20261001-progress.json) | 2026-10-01 | 全リポジトリへの追加レビュー・5件の修正・領域間契約と最終検証 |

- [構成整理（2026-10-01）](structure-20261001.md) — 接続共通基盤・開発ツール・文書・テストの配置と検証。
