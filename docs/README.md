# docs — 文書の索引

用途ごとに利用ガイド・開発資料・仕様を分けています。
現行の操作条件はコードと対応ガイドを参照し、過去の記録とは区別してください。

| 種別 | 文書 |
|---|---|
| 利用・導入 `guides/` | [利用者ガイド](guides/USER_GUIDE.md) · [導入手順](guides/INSTALLATION.md) · [AIエージェント向け導入](guides/SETUP_AGENT.md) · [LINE WORKS接続](guides/LINEWORKS.md) |
| 開発・保守 `development/` | [開発・運用リファレンス](development/DEVELOPMENT.md) · [リリースノート規則](development/RELEASE_NOTES.md) · [README運用](development/README_MAINTENANCE.md) |
| 仕様 `specs/` | [ライフサイクル](specs/lifecycle-spec.md) · [外部出力契約](specs/external-export-contract.md) · [意味解析の評価](specs/semantic-evaluation.md) · [意味解析の段階導入](specs/semantic-facts-v2-rollout.md) |
| 今後の計画 | [ロードマップ](ROADMAP.md) → [詳細計画](roadmap/) |
| 図・画面例 | [説明図](assets/) · [完全合成の画面例](screenshots/) · [Slack画像の更新手順](screenshots/slack-gallery/README.md) · [LINE WORKS画像の更新手順](screenshots/lineworks-gallery/README.md) |
| 過去の記録 `dev-records/` | [開発記録の索引](dev-records/README.md) · [自動更新の設計履歴](dev-records/auto-update-plan.md) |
| 過去リリースの原本 | [形式移行前の記録](releases/archive/) |

開発リファレンスの生成表は `scripts/development/update_readme.py`、
READMEの見直し記録は `development/readme-review.json` で管理します。
