# 接続アダプター

通知先ごとの接続・表示・操作処理と、状態監視・遠隔支援の接続コードをまとめています。

| フォルダ | 接続方法 | 実装の範囲 |
| --- | --- | --- |
| `slack/` | Hermes の既存 Slack 接続 | カード表示、本文・添付配送、全機能の `/mcs <JSON>`、操作・確認 |
| `discord/` | Hermes の既存 Discord 接続 | カード表示、本文・添付配送、全機能の `/mcs <JSON>`、操作・確認 |
| `lineworks/` | 独自の Bot API 接続 | 認証、配送、署名付き callback、本人トークの `mcs <JSON>`、操作・確認、起動 CLI |
| `sdc/` | 状態監視・遠隔支援（CSDC） | 接続コードのみ同梱。1.1.0では未提供で、新規導入は既定OFF |
| `common/` | 接続先共通の基盤 | 共通コマンド・患者権限、配送 grant、journal、receipt、registry、render-spec、共通テキスト |

Slack・Discord は Hermes が所有する接続・認証・allowlist を使用します。
独立モードの接続は各接続先の `standalone.py` に置きます。独立モードの導入は
[専用手順](../docs/guides/STANDALONE.md)を参照してください。
LINE WORKS の導入・権限・callback 設定は [導入手順](../docs/guides/LINEWORKS.md)を参照してください。

配送 grant、journal、receipt、registry、render-spec の共通処理は
`adapters/common/` にあり、通知先ごとの実装を重複させません。

import は `adapters.slack.*` / `adapters.discord.*` / `adapters.common.*` を使います。
Hermes plugin のカード worker もリポジトリのルートを `sys.path` に加えてから
これらを直接読み込みます（`hermes_plugin/` 配下の別名パッケージはありません）。
共通コードの変更は、対象 Hermes gateway と LINE WORKS独立アダプターの再起動で反映します。

JSONコマンドは既存のsnapshot閲覧・依頼管理・運用承認を共有します。Slackの応答は
本人専用、LINE WORKSの応答は本人1:1トークです。プレビューと確定は同じ接続先・
送信元scopeで行い、別接続先への転用は拒否します。新しい依存や認証経路は追加しません。

共通コマンドの正本は `common/dispatch.py`、本人・患者権限の検証は
`common/access.py` と `common/projects.py`、人承認の preview/confirm は
`common/human_commands.py` です。接続先は認証済みの本人・部屋・組織情報を構成し、
共通処理が毎回 allowlist・患者 scope・原文版・payload hash・origin を再検証します。
Discord のハンドラーは設定取得関数を借用し、呼出しごとに設定を再取得します。
`common/mcs_api.py` が同じ checkout の flat import と JSON 応答を所有し、
共通処理から `hermes_plugin` への依存はありません。

snapshot の接続と View は各読取り・確認処理が生成して必ず閉じます。
患者集合の短期 cache は `common/projects.py`、配送 journal の事実照会は
`common/journal.py` が所有します。送信状態・scope lock・journal cache の寿命は
引き続き DeliveryWorker が一元管理し、接続先の Supervisor が task 完了後に lock を解放します。
