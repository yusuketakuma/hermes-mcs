# 接続アダプター

通知先ごとの接続・表示・操作処理をこのフォルダにまとめています。

| フォルダ | 接続方法 | 実装の範囲 |
| --- | --- | --- |
| `slack/` | Hermes の既存 Slack 接続 | カード表示、本文・添付配送、操作・確認 |
| `discord/` | Hermes の既存 Discord 接続 | カード表示、本文・添付配送、操作・確認 |
| `lineworks/` | 独自の Bot API 接続 | 認証、配送、署名付き callback、操作・確認、起動 CLI |
| `common/` | 接続先共通の基盤 | 配送 grant、journal、receipt、registry、render-spec、共通テキスト |

Slack・Discord は Hermes が所有する接続・認証・allowlist を使用します。
独立モードの接続は各接続先の `standalone.py`、保持した旧APIの実装は
`runtime_compat.py` に置きます。旧 `mcs_standalone.*_runtime` は同じmoduleを
返す互換入口として維持します。独立モードの導入は
[専用手順](../docs/guides/STANDALONE.md)を参照してください。
LINE WORKS の導入・権限・callback 設定は [導入手順](../docs/guides/LINEWORKS.md)を参照してください。

配送 grant、journal、receipt、registry、render-spec の共通処理は
`adapters/common/` にあり、通知先ごとの実装を重複させません。

既存の `hermes_plugin.mcs_slack.*`、`hermes_plugin.mcs_discord.*`、
`hermes_plugin.mcs_delivery.*` の import は
互換入口からこのフォルダの実装を読み込むため引き続き利用できます。
新しい import は `adapters.slack.*` / `adapters.discord.*` / `adapters.common.*` を利用できます。
共通コードの変更は、対象 Hermes gateway と LINE WORKS独立アダプターの再起動で反映します。
