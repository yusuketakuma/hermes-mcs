# Hermes 実SDKの隔離検証（2026-10-01）

前回の標準runnerでは未収集・skipだった4ファイルを、CIと同じHermes固定版と
実SDKで確認した。14 passed、0 failed、skipなし、終了コード0（runner集計2.5秒）。
実MCS・実配送・Keychain・実モデル・原本DB・稼働gatewayにはアクセスしていない。

## 対象と環境

| 対象 | 結果 |
|---|---|
| `integration/test_hermes_discord.py` | 1 passed |
| `integration/test_hermes_slack.py` | 3 passed |
| `integration/test_hermes_config.py` | 1 passed |
| `tests/plugin/test_discord_sdk_real.py` | 9 passed |

Hermes source: `fd50a275e2616118c48fe07e7e1c878782b15ccd`（CIとinstall.shの固定版）。
既存gitからtracked sourceだけを一時領域へarchiveした。既存venvは依存の読取りに
使用し、設定・認証・HOMEを読み込まず、依存の追加や更新は行っていない。
Python 3.11.15、pytest 9.1.1、pytest-asyncio 1.3.0、discord.py 2.7.1、
slack-sdk 3.44.1、slack-bolt 1.30.0。SDKは固定版と一致するが、CIのPython 3.13とは異なる。

Hermesの `scripts/run_tests.sh` で上記4ファイルを指定し、
`-j 2 --file-retries 0 -- -x -q -o addopts=` を使った。
空の環境から一時HOMEで実行し、pytest起動時に既存 `tests/conftest.py` の
安全ガードを全対象へ適用した。収集前にgateway/hermes_cliの解決先が
一時領域の固定版ソースであることとSDK版を検証した。

一時コピーのrunnerだけ、scratch cleanup先を共有 `/var/tmp/hermes-pytest` から
今回専用の一時ディレクトリへ変更した。プロジェクト・既存Hermes・稼働環境は変更していない。
初回はmacOSの `/var` と `/private/var` の表記差で検証用ソースパスassertが
収集前に失敗した。resolveで同じ実体か確認するよう検証用ガードを直し、再実行した。
テストの期待値や安全ガードは弱めていない。自動retryは無効。

成功ログ: `/tmp/mcs-sdk-tests-20261001.log`。
初回の検証用ガード失敗ログ: `/tmp/mcs-sdk-tests-20261001-harness-path-failure.log`。
一時ログは恒久保存物ではないため、主要結果と条件を本記録に保持する。

## 制約

実SDKのAPI・serializer・PluginManager・native adapter・CLI設定経路を合成データと
HTTP stubで検証した。実配送・配備・gateway再起動・実モデル品質の証明ではない。
GitHubの統合CI自体は今回起動していない。
