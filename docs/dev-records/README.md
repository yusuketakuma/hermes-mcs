# dev-records — 開発記録の索引

当時の判断・検証・レビュー結果を残した証跡。現在の仕様や操作手順の正本ではない
（現行は `docs/DEVELOPMENT.md` とコード）。ファイル名は変更しない — 他文書が
`file:line` で引用している。CI（`ci/gates.py`・`ci/mine_gates.py`）はこのディレクトリの
`.md` / `.json` だけを許可し、再帰的に走査する。

| 記録 | 日付 | 内容 |
|---|---|---|
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
