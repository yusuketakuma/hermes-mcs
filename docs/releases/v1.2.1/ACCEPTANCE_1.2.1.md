# v1.2.1 受入準備票 — 未公開

この票は v1.2.1 の変更記録と説明資料を現在の候補へ準備した作業の記録です。候補全体の合格、本番適用、公開の完了を示すものではありません。

## 今回の対象

- v1.2.0（タグ 2026-11-21）以降の変更記録17件を `changes/archive/1.2.1/` へ収録。バグフィックス版として番号 `1.2.1` を指定（所有者 2026-11-22）。
- 内訳: 不具合修正9件（perf直列化・未精算結果の記録と回答再利用照合・復元照合の保留記録・過去編集の通知抑制・承認レシート誤表示・CSDC os_version・スタンプ結果の再試行・時計巻戻りのheartbeat・until境界）、新機能2件（再起動要求中の定期処理の譲り・自動公開資料の整備）、改善6件（sdc契約コードの未使用整理）。
- CHANGELOG の v1.2.1 節・README のリリース案内を生成し、説明図1点（通知を止めない修正、SVG正本・PNG描画）を導入部へ掲載。公開本文は上限内の20,079字。
- 説明図の来歴とhashは `visual-source.json`、17件の対応表は `change-map.json`。
- `docs/development/readme-review.json` を 1.2.1 へ更新し、README本文の見直し結果を記録済み。

## 検証済み（この作業の範囲・ローカル実測）

対象は `devin/release-1.2.1-prep` ブランチの作業ツリー（base `5bf39eeca`）。実行日は 2026-11-22（JST）。

- `release_notes.py check`: 合格（archive 17件・CHANGELOG構造・本文 20,079字）。
- `release_notes.py build --version 1.2.1`: 生成成功（archive移動・CHANGELOG・README更新）。
- `release_notes.py export --version 1.2.1`: 本文・タイトル出力成功。`release-notes-1.2.1.md`・`release-title-1.2.1.txt` に収録。
- `scripts/run_tests.sh tests/release/`: 59 passed + 128 subtests。
- `update_readme.py --check`: drift なし。
- `readme_release.py --check`: 合格。
- `ruff check`（CI同一範囲）: All checks passed。
- `ci/gates.py`: 10/10 gates pass。
- `ci/mine_gates.py --check`: all extracted incidents covered or tracked。
- `check_drug_master_bundle.py`: 同梱 bundle は公式最新版 20261112（MHLW download menu の医薬品マスター最終更新日・19,324件との一致を 2026-11-22 に確認）。有効化は要オペレーター承認で未実施。
- 図版: PNG 1点（1960×840）を1372×588相当へ縮小した表示で文字欠落・重なり・余白を確認。`visual-source.json` に hash を記録。
- `patient_context_quality_identity.QA_VERSION`: 確認の結果 bump 不要と判断。v1.2.0 以降の `patient_context_quality_controller.py` 差分は `writer_lock.restart_intent_active()` を待機条件へ加える1行のみで、PASS判定と答えの解釈を担う `QUALITY_FUNCTIONS`・`QUALITY_CONSTANTS` の hash 対象（`semantic_audit`・`semantic_jev`・`patient_context`・`patient_context_quality_identity` の各関数・定数）に変更なし。`patient_context_quality_receipts.py`・`semantic_usage_receipts.py` に差分なし。

## 未実行・残条件

- 全体 suite・CI の全6 lane: 本票作成時点ではリリースPRの CI run が最終の実測証拠になる。ローカル実測は上記の通り。
- 独立レビュー（別コンテキストのレビュアーによる最終検証）: Devin Review が PR に付く見込み。指摘があれば対応を記録する。
- tag `v1.2.1` の作成・GitHub Release の公開・説明図 PNG の release asset 添付と到達・表示確認: 未実施。本文中の画像URLは公開後に照合が必要。
- 本番適用・常駐サービスの再起動: 未実施。復元照合の修正の反映には gateway と常駐の抽出プロセスの再起動が必要（「更新時の注意」に記載）。

## 参照

- 変更の要点と収録物: [README.md](README.md)
- 全17件の対応表: [change-map.json](change-map.json)
- 図の来歴・hash: [visual-source.json](visual-source.json)
