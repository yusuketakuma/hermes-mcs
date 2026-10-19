# 文書を探す

操作する方は目的から、開発する方は仕様・構成から選んでください。
各版の受入結果や当時の計画は[リリース記録](releases/README.md)にまとめています。
導入した版の変更点は[CHANGELOG](../CHANGELOG.md)、現在の操作条件は対応するガイドとコードで確認できます。

## 導入・利用・復旧

| 目的 | 読む文書 |
|---|---|
| 初めて使う・日常の操作を知る | [初めての導入・更新](guides/EASY_SETUP.md) · [利用者ガイド](guides/USER_GUIDE.md) |
| 導入や接続を設定する | [詳細な導入手順](guides/INSTALLATION.md) · [Hermesを使わない導入](guides/STANDALONE.md) · [LINE WORKS接続](guides/LINEWORKS.md) |
| AIエージェントに新規導入を依頼する | [新規導入の手順](guides/SETUP_AGENT.md) · [一時フォルダから導入する場合](guides/LOCAL_AGENT_INSTALL.md) |
| 既存環境を更新・復旧する | [更新と中断時の復旧](guides/UPGRADE_AGENT.md) · [通知の個別復旧](guides/NOTIFICATION_RECOVERY.md) |
| バックアップや端末喪失に備える | [鍵の保管と復元](guides/BACKUP.md) |

通知の個別復旧は、読取り専用の計画を確認してから対象を指定する管理者向けの手順です。通知の送信結果が不明な場合や復元の承認待ちは、診断だけで解除されません。

## 仕様・開発・保守

| 目的 | 読む文書 |
|---|---|
| コードの場所と検証方法を知る | [責務・依存・移植範囲](development/REPOSITORY_STRUCTURE.md) · [リポジトリ構成と開発リファレンス](development/DEVELOPMENT.md#構成) · [安全上の制約](../SECURITY.md) |
| 患者情報の整理と表示を確認する | [患者背景の構造化契約](specs/patient-context.md) · [在宅薬局で扱う情報](specs/pharmacy-visit-information.md) |
| 医薬品マスターの配布と利用を確認する | [公式マスターの契約](specs/official-drug-master.md) · [照会・更新の手順](specs/DRUG_MASTER_WORKFLOWS.md) · [同梱原本](../resources/drug-master/README.md) |
| 更新・復旧・外部出力の境界を確認する | [ライフサイクル仕様](specs/lifecycle-spec.md) · [外部出力契約](specs/external-export-contract.md) |
| 抽出・意味解析を評価する | [意味解析の評価](specs/semantic-evaluation.md) · [封印候補の評価と昇格](specs/SEALED_CANDIDATE_EVALUATION.md) · [段階導入の記録](specs/semantic-facts-v2-rollout.md) · [評価基準と資産](../evaluation/README.md) |
| リリースやREADMEを更新する | [変更記録の書き方](../changes/README.md) · [リリースノート規則](development/RELEASE_NOTES.md) · [README運用](development/README_MAINTENANCE.md) |
| 実機へ配置する資産を調べる | [配備資産](../deployment/README.md) · [接続アダプター](../adapters/README.md) |
| 今後の計画を確認する | [ロードマップ](ROADMAP.md) · [分野ごとの詳細計画](roadmap/) |

ロードマップや詳細計画は記載された基準版・日付での判断です。実装・公開・本番反映の完了は、その版の受入記録で別々に確認してください。

## 画面例と過去の記録

| 種類 | 置き場所 |
|---|---|
| 現在の画面説明 | [画面例](screenshots/) · [Slack画像の更新手順](screenshots/slack-gallery/README.md) · [LINE WORKS画像の更新手順](screenshots/lineworks-gallery/README.md) |
| 構成や処理の説明図 | [図](assets/) · [HTML原稿とSVGの対応](assets/diagrams/README.md) |
| 版ごとの計画・受入・公開時の画像 | [リリース記録の索引](releases/README.md) |
| 版をまたぐ調査・不具合・検証の証跡 | [開発記録の索引](dev-records/README.md) |

`development/` は現行の開発手順、`releases/v版番号/` はその版の記録を置く場所です。旧 `development/acceptance/` と `development/plans/` の文書は、ファイル名を保って版別のフォルダへ移しました。旧URLは当時のタグで参照でき、移動先は[リリース記録の索引](releases/README.md)から探せます。

開発リファレンスの生成表は `scripts/development/update_readme.py`、現行READMEの見直し記録は `development/readme-review.json` で管理します。生成表は手で編集しません。
