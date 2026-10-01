# フォルダ・ファイル構成の整理（2026-10-01）

基点は `7a5533f7819920ee437db82951b449aff269b9db`。作業開始時のtreeはclean。
用途の混在を解消するため47ファイルを移動し、呼出元・生成先・CI・文書リンクを追従した。
実データ・認証情報・Hermes本体・稼働サービスは変更していない。

## 配置

| 対象 | 新しい配置 | 移動数 |
|---|---|---:|
| 接続先共通の配送実装 | `adapters/common/` | 7 |
| 接続別・共通テスト | `tests/adapters/{common,discord,slack,lineworks}/` | 18 |
| 文書生成・リリース・合成評価ツール | `scripts/development/` | 6 |
| 利用ガイド・開発資料・仕様・設計履歴 | `docs/{guides,development,specs,dev-records}/` | 13 |
| CCO候補・承認記録・説明 | `deployment/cco/` | 3 |

直下のファイル数は `docs/` が15→2、`scripts/` が8→2、
`tests/plugin/` が20→2。全ファイル数の削減ではなく、配置の整理を示す。
`docs/README.md`を用途別索引とし、開発リファレンスの生成表は
`docs/development/DEVELOPMENT.md`へ生成する。

## 保持した契約

- `mcs/`の既存領域・flat import、まとまったDB/収集/解析モジュールを維持。
- `hermes_plugin.mcs_delivery.*`は新正本と同一module。7実装ファイルは開始時とbytes同一。
  互換packageだけを残し、import順序や別のホストnamespaceでも共有状態を維持する。
- `install.sh`、`scripts/run_tests.sh`、`make`ターゲット、
  `python -m lineworks_adapter`の実行入口を維持。
- `evaluation/`、`changes/`、`ci/`、`integration/`、既存配備スクリプトは
  現在の責務に沿っているため配置を維持。未リリース記録はrelease buildまでarchiveしない。
- 既存dev-recordsのhash台帳・証跡、Release原本、CCO承認記録、生成OpenWiki本文は保持。
  履歴の旧パスは当時の位置を示す。OpenWikiは正本briefのみ新構成へ更新した。
- 共通コードの移動に合わせ、gatewayの診断・永続restart判定と
  LINE WORKSの更新影響案内に`adapters/common/`を含める。
  反映には稼働中のgatewayと独立LINE WORKSプロセスの再起動が必要。

## 確認した依存

安全確認済み126ソース（mcs全領域、adapters、plugin、独立CLI、開発ツール、
配備コード、CI、integration、pytest bootstrap）の一時コピーでJGを実行。
27関連ファイルを得たが16リクエスト上限でdiscovery incomplete（exit2）。
実ソースとrgで呼出元、相対path、ROOT計算、helper探索、SDK指定、
README生成・同期・Release契約を補完した。JGを網羅性の証明には用いない。
生成Wiki本文と原本DB・データ/config/envは探索対象外。

テストの子フォルダ`discord/`がSDKのnamespaceと衝突する問題を回帰で検出した。
共通テストを`tests/adapters/common/`へ集約し、直接.pyを持つディレクトリだけを
bootstrapに登録して原因を除いた。既存SDKテストのskip/assert条件は維持した。

## 検証

- 新しい共通pathを追加した再起動判定の回帰は、修正前3 failedを確認した。
- 接続・共有配送・plugin・通知: 884 passed、1 skipped（71.62秒）。
- setup・更新・installer: 315 passed（136.29秒）。
- 開発/リリースツール: 67 passed、23 subtests passed。
- 開発CLI6本は合成HOME・最小環境・repo外cwdから`--help`成功。
- `make lint`、static gates8/8、incident coverage、README生成/同期/変更記録検査、
  合成Slack画像7画面、shellcheck、bash構文、diff whitespaceは成功。
- 最終全体: **3548 passed、1 skipped、23 subtests passed**（235.71秒、exit0）。
  SDK不在環境のskipは、次の固定SDK環境で別途検証した。
- 固定Hermes `fd50a275e2616118c48fe07e7e1c878782b15ccd`:
  **4ファイル・14 passed、0 failed、skipなし**（1.8秒、exit0）。
  Python3.11.15、discord.py2.7.1、slack-sdk3.44.1、slack-bolt1.30.0。
  一時ソース7112 PythonファイルをGit blobへ照合し不一致0。
  4プロセスの一時HOME・空認証環境・socket/HTTP/Keychain/実MCS遮断と
  bytecode書込み無効をreceiptで確認。実機Hermes/venvは読取りのみ。
- 現行Markdown31ファイルの相対リンクは未解決0。
  47移動先を開始snapshotへ照合し、失われた既存ファイル0。
  新規追加はcommon package入口・変更記録・本書の3ファイルだけ。
  管理されたAGENTSブロックも元の内容を保持している。

主なログは `/tmp/mcs-structure-full-20261001.log`、
`/tmp/mcs-structure-sdk-20261001.log`、
`/tmp/mcs-structure-adapters-tests-final-20261001.log`。
固定SDKのguard結果は `/tmp/mcs-structure-sdk-result-20261001.json`、
新旧パス47件の対応は `/tmp/mcs-structure-final-moves-20261001.json`。
CIのPython3.13環境で検証したという意味ではない。

実MCS・Keychain・ローカルLLM/Jev・実配送・実テナント/TLS・サービス反映・
GitHub CIは検証していない。モデル・保存形式・取得範囲・通知先・既読化・
人承認・理由・receiptの条件は変更していない。
