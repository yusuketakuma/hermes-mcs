# リリース記録

版ごとの計画・受入結果・公開時の画面例を `v版番号/` にまとめています。
記録にある「候補」「合成検証済み」「未実施」は当時の状態です。公開・本番反映の成功はそれぞれの証拠で確認し、計画やテスト成功だけから補いません。
利用者向けの変更点は [CHANGELOG](../../CHANGELOG.md)、現在の手順は[文書の主索引](../README.md)を参照してください。

## 版別の記録

| 版 | 記録 |
|---|---|
| 1.0.28（候補） | [受入・残条件](v1.0.28/ACCEPTANCE_1.0.28.md) · [スタンプの説明図](v1.0.28/README.md) |
| 1.0.27 | [受入・残条件](v1.0.27/ACCEPTANCE_1.0.27.md) · [統合対応表](v1.0.27/integration-manifest.json) · [処理の説明図](v1.0.27/README.md) |
| 1.0.26 | [受入・公開前の残件](v1.0.26/ACCEPTANCE_1.0.26.md) · [統合対応表](v1.0.26/integration-manifest.json) · [画面例](v1.0.26/README.md) |
| 1.0.25 | [受入](v1.0.25/ACCEPTANCE_1.0.25.md) · [患者背景の計画](v1.0.25/PATIENT_CONTEXT_1.0.25.md) · [画面例](v1.0.25/README.md) |
| 1.0.24 | [受入](v1.0.24/ACCEPTANCE_1.0.24.md) · [画面例](v1.0.24/README.md) |
| 1.0.23 | [受入](v1.0.23/ACCEPTANCE_1.0.23.md) · [調査](v1.0.23/AUDIT_1.0.23.md) · [構成確認](v1.0.23/REPOSITORY_HYGIENE_1.0.23.md) · [更新取り込み](v1.0.23/UPDATE_ITEMS_1.0.23.md) · [ワークツリー統合](v1.0.23/WORKTREE_INTEGRATION_1.0.23.md) · [図の準備](v1.0.23/VISUAL_PREPARATION.md) |
| 1.0.22 | [受入](v1.0.22/ACCEPTANCE_1.0.22.md) |
| 1.0.21 | [受入](v1.0.21/ACCEPTANCE_1.0.21.md) · [計画](v1.0.21/RELEASE_1.0.21.md) · [画面例](v1.0.21/README.md) |
| 1.0.20 | [受入](v1.0.20/ACCEPTANCE_1.0.20.md) · [README確認](v1.0.20/readme-review.json) · [画面例](v1.0.20/README.md) |
| 1.0.19 | [受入](v1.0.19/ACCEPTANCE_1.0.19.md) |
| 1.0.18 | [受入](v1.0.18/ACCEPTANCE_1.0.18.md) |
| 1.0.17 | [受入](v1.0.17/ACCEPTANCE_1.0.17.md) |
| 1.0.16 | [受入](v1.0.16/ACCEPTANCE_1.0.16.md) · [計画](v1.0.16/RELEASE_1.0.16.md) · [長文処理の設計](v1.0.16/LONG_MESSAGE_1.0.16.md) |
| 1.0.15 | [受入](v1.0.15/ACCEPTANCE_1.0.15.md) · [通知プレビューの図と原稿](v1.0.15/) |
| 1.0.14 | [受入](v1.0.14/ACCEPTANCE_1.0.14.md) |
| 1.0.13 | [受入](v1.0.13/ACCEPTANCE_1.0.13.md) · [計画](v1.0.13/RELEASE_1.0.13.md) · [NASバックアップ提案](v1.0.13/BACKUP_NAS_PROPOSAL_1.0.13.md) |
| 1.0.12 | [受入](v1.0.12/ACCEPTANCE_1.0.12.md) |
| 1.0.11 | [受入](v1.0.11/ACCEPTANCE_1.0.11.md) |

1.0.10以前を含む版横断の開発・検証記録は [dev-records](../dev-records/README.md) にあります。ここは事故の再発防止を照合するCIの入力でもあるため、ファイル名と配置を維持しています。

## 配置の決め方

- その版だけの計画・受入票・README確認・説明図は `releases/v版番号/` に置きます。公開前の記録も同じ場所で未完了項目を追跡します。
- 現在も使うリリース手順は [development/RELEASE_NOTES.md](../development/RELEASE_NOTES.md)、画像の準備手順は [VISUAL_PREPARATION.md](VISUAL_PREPARATION.md) に集約します。
- 実行時の変更記録は [changes/](../../changes/README.md) から既存の生成器で集約します。履歴をここへ複製しません。
- 形式移行前の原本と、git履歴へ移した監査記録の取り出し方は [archive/](archive/) にあります。

## 旧配置から探す

| 旧配置 | 現在の配置 |
|---|---|
| `docs/development/acceptance/*1.0.N.md` | `docs/releases/v1.0.N/` の同名ファイル |
| `docs/development/plans/*1.0.N.md` | `docs/releases/v1.0.N/` の同名ファイル |
| `docs/development/readme-review-1.0.20.json` | [v1.0.20/readme-review.json](v1.0.20/readme-review.json) |
| `docs/development/AUDITS.md` | [archive/AUDITS.md](archive/AUDITS.md) |

本文の判断・検証結果・hashは保持し、現在たどる相対リンクと参照先だけを調整しています。過去の在庫表などにある旧パスは当時の配置です。旧URL・行番号で引用された原文は、その記録のcommitや公開タグで確認できます。
