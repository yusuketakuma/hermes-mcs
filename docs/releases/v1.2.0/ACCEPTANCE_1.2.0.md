# v1.2.0 受入準備票 — 未公開

この票は v1.2.0 の変更記録と説明資料を現在の候補へ準備した作業の記録です。候補全体の合格、本番適用、公開の完了を示すものではありません。

## 今回の対象

- v1.1.2（タグ 2026-11-08）以降の変更記録438件を `changes/archive/1.2.0/` へ全件収録。形式は `release_notes.py` の検証を通過する厳格スキーマへ統一し、category の規定外値（improved 等）・独自スキーマ・summary 280字超過・detail/ref 中の記号を全件修正した（`cfff1edb7`・`b11ddfa14`・`e9cdf7751`・`a45f75f9d` と本票作成時の54件の追補修正）。
- CHANGELOG の v1.2.0 節・README のリリース案内を生成し、`--summary-file` による比較表と説明図2点を導入部へ掲載（`572c30a2e`）。公開本文は上限内の91,815字。
- 説明図は通知カード・クラウドAIの2点（SVG正本・PNG描画）。来歴とhashは `visual-source.json`、438件の対応表は `change-map.json`。
- `docs/development/readme-review.json` を 1.2.0 へ更新し、README本文の見直し結果を記録済み。

## 検証済み（この作業の範囲・ローカル実測）

対象は `release/prep-20261121` 先端 `572c30a2e` と、本票と同じ変更で加えたテストスタブ修正。実行日は 2026-11-21（JST）。

- `release_notes.py check`: 合格（archive 438件・CHANGELOG構造・詳細ファイル 821,133B・本文 91,815字）。
- `scripts/run_tests.sh tests/release/`: 59 passed + 128 subtests。
- `scripts/run_tests.sh tests/ingest/test_derive_transaction_ownership.py`: 15 passed。main では `fetch_error` が失敗していた（`_fail_run` の `alert` kwarg にテストスタブが未対応）。スタブ修正と fetch_error の期待値（finish・health の記録）を合わせて再検証し合格。失敗修正＋影響範囲の再検証として記録する。
- `update_readme.py --check`: drift なし。
- `readme_release.py --check`: 合格。
- `ruff check`（CI同一範囲）: All checks passed。
- `ci/gates.py`: 10/10 gates pass。
- `ci/mine_gates.py --check`: all extracted incidents covered or tracked。
- `check_drug_master_bundle.py`: 同梱 bundle は公式最新版 20261112（MHLW download menu との一致を 2026-11-21 に確認）。有効化は要オペレーター承認で未実施。
- 図版: PNG 2点（1960×840）を縮小表示で文字欠落・重なりを確認。`visual-source.json` に hash を記録。
- `patient_context_quality_identity.QA_VERSION`: 確認の結果 bump 不要と判断。`fb6ffbfab` 以降の `patient_context_quality_controller.py`・`_receipts.py` の差分は送信経路・skip理由（REVIEW_HELD 追加）・deferred 条件の変更で、PASS 判定と答えの解釈は `QUALITY_FUNCTIONS`・`QUALITY_CONSTANTS` の hash 対象内の変更に限られる。`semantic_usage_receipts.py` に差分なし。

## 未実行・残条件

- 全体 suite（pytest-linux/macos matrix）: この候補SHAでは未実行。
- CI の全6 lane（lint-test・hygiene・standalone-sdk・incident-gates・macos・hermes-integration）: `release/prep-20261121` の先端 `572c30a2e` では実行記録なし。PR または tag run での実測が必要で、draft・docs mode の成功は受入の実測に使えない。
- 独立レビュー（別コンテキストのレビュアーによる最終検証）: 未実施。
- tag `v1.2.0` の作成・GitHub Release の公開・説明図 PNG の release asset 添付と到達・表示確認: 未実施。本文中の画像URLは公開後に照合が必要。
- 本番適用・常駐サービスの再起動: 未実施。

## 参照

- 変更の要点と収録物: [README.md](README.md)
- 全438件の対応表: [change-map.json](change-map.json)
- 図の来歴・hash: [visual-source.json](visual-source.json)
