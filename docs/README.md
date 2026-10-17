# docs — 文書の索引

用途ごとに利用ガイド・開発資料・仕様を分けています。
現行の操作条件はコードと対応ガイドを参照し、過去の記録とは区別してください。

| 種別 | 文書 |
|---|---|
| 利用・導入 `guides/` | [利用者ガイド](guides/USER_GUIDE.md) · [初めての導入・更新](guides/EASY_SETUP.md) · [詳細な導入手順](guides/INSTALLATION.md) · [AIエージェント向け導入](guides/SETUP_AGENT.md) · [LINE WORKS接続](guides/LINEWORKS.md) |
| 開発・保守 `development/` | [開発・運用リファレンス](development/DEVELOPMENT.md) · [リリースノート規則](development/RELEASE_NOTES.md) · [README運用](development/README_MAINTENANCE.md) |
| 仕様 `specs/` | [ライフサイクル](specs/lifecycle-spec.md) · [外部出力契約](specs/external-export-contract.md) · [意味解析の評価](specs/semantic-evaluation.md) · [封印候補の評価・昇格](specs/SEALED_CANDIDATE_EVALUATION.md) · [意味解析の段階導入](specs/semantic-facts-v2-rollout.md) |
| 今後の計画 | [ロードマップ](ROADMAP.md) → [詳細計画](roadmap/) |
| 図・画面例 | [説明図](assets/) · [完全合成の画面例](screenshots/) · [Slack画像の更新手順](screenshots/slack-gallery/README.md) · [LINE WORKS画像の更新手順](screenshots/lineworks-gallery/README.md) |
| 過去の記録 `dev-records/` | [開発記録の索引](dev-records/README.md) · [自動更新の設計履歴](dev-records/auto-update-plan.md) |
| 過去リリースの原本 | [形式移行前の記録](releases/archive/) |

1.0.25の患者背景・職種別情報・操作変更は[実装計画](development/plans/PATIENT_CONTEXT_1.0.25.md)、[構造化契約](specs/patient-context.md)、[受入条件](development/acceptance/ACCEPTANCE_1.0.25.md)に対応付けています。

開発リファレンスの生成表は `scripts/development/update_readme.py`、
READMEの見直し記録は `development/readme-review.json` で管理します。

通知結果不明の管理者向けローカル手順は[通知の個別復旧](guides/NOTIFICATION_RECOVERY.md)を参照してください。planと書込み承認を分離し、復元保留や配送予算を維持します。
