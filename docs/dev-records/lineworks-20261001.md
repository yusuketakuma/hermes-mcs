# LINE WORKS独立接続とアダプター整理（2026-10-01）

hermes-mcs内に公式Bot APIを使うLINE WORKS接続を実装した。Hermes Agent本体の
コードは変更していない。Slack/Discordは公式Hermes接続を維持する。
実環境への配信・配備・gateway再起動・pushは行っていない。

## 完了した範囲

- 接続実装を `adapters/slack/`・`adapters/discord/`・`adapters/lineworks/` に整理。
  既存Slack/Discord importとLINE WORKSの独立CLI入口を維持した。
- JWT RS256認証、token cache、固定公式URLの単回REST送信、二段階添付upload、
  生HTTP本文のHMAC署名検証、許可ドメイン・部屋・ユーザー・MCSプロジェクトの照合。
- 中立render-spec・grant・journal・receiptへLINE WORKS v3を追加。
  本文・表示末尾・追加ボタン・添付を封印した配送パーツで送る。
  LINE WORKSの上限内へ分割し、要約・原文・送信対象の添付を保存する。
- 私的DMでの入力・検索・担当者一覧、プレビュー・理由・本人の確定。
  人承認操作は既存のcommand/receipt経路へ渡す。
- 更新は新規投稿し、旧ボタンと既に開いているDM確認を送信前に無効化。
  更新・取り下げが配送不明でも旧確認を確定できず、無効化を永続化して再起動後も保持する。
  不明な送信・処理中クラッシュを自動再送せず、
  配送直前のroute epoch/停止/復旧確認、プロセス間の書込み直列化と429待機を実装。
- 初回設定時のrunner-ownedフォルダ/flags準備、秘密値の端末入力・ローカル診断、
  macOS/Linuxサービス候補生成、ユーザー向け・AI向けの導入手順を追加。

公式根拠と導入・障害時の手順は [LINEWORKS.md](../LINEWORKS.md)。
一次資料はLINE WORKS DevelopersのJWT、Bot API、channel/user送信、button/action、
Callback、添付/upload、rate limitの各公式ページで、リンクを同手順書に保持する。
コードとテストを正本として、既存の中立配送・Slack/Discord呼出関係を照合した。
先行する全体調査の検索範囲と制約は [安定性記録](stability-20261001.md) に保持する。
実テナント・患者データ・実配信・実モデルは調査・テストしていない。

## 実行した検証

| 検証 | 結果 |
|---|---|
| 標準 `scripts/run_tests.sh`（tests/ + integration/） | 3,434 passed、1 skipped、19 subtests passed、233.87秒、exit 0 |
| 最終変更後の通知・plugin・setup・サービスの集約回帰 | 983 passed、1 skipped、68.80秒、exit 0 |
| LINE WORKS・導入・互換入口7ファイル（最後の無効化修正前） | 168 passed、exit 0 |
| 最後の認証入力・token応答境界の変更に対応するclient + ローカル診断 | 42 passed、exit 0 |
| Hermes固定版・実Discord/Slack SDKの4ファイル | 14 passed、skipなし、exit 0 |
| `make lint`（新adapters/とCLI入口も対象） | 成功 |
| `ci/gates.py` | 8/8成功 |
| `ci/mine_gates.py --check` | 全defect incidentがcovered/tracked |
| README生成・release記録・README release整合・Slack画像整合 | 成功 |
| `git diff --check` | 成功 |

全体テスト後の互換入口・入力境界・旧確認の無効化は影響範囲のテストで再検証した。
全体3,434件を最終変更後に再度実行したという意味ではない。
全体のskipは実Discord SDK必須のテストで、別の固定版SDK検証で実行した。
Hermes-bound integrationの通常環境での収集除外も固定版検証で補った。

SDK sourceはCIと同じ `fd50a275e2616118c48fe07e7e1c878782b15ccd` の一時コピー。
既存venvのSDKを読取り専用で使い、HOME・資格情報・通信・Keychain・実DBを隔離した。
Python 3.11.15、discord.py 2.7.1、slack-sdk 3.44.1、slack-bolt 1.30.0。
CIのPython 3.13上でGitHub Actionsを実行したという意味ではない。
詳細な隔離方法は [SDK記録](hermes-sdk-20261001.md) と同じ。

一時ログは `/tmp/mcs-lineworks-full-20261001.log`、
`/tmp/mcs-lineworks-final-regression-20261001.log` と
`/tmp/mcs-lineworks-sdk-20261001.log`。恒久記録として主要結果を本書に保持する。

## 未検証と適用条件

実LINE WORKSの認証・送信・管理者制限・公開HTTPS Callbackは未検証。
公式uploadのHTTP例 `Filedata` とcurl例 `FileData` の表記差はHTTP例に従い、
実接続での受理は未確認。エラー時に別表記で自動再uploadしない。
macOSサービス候補のネイティブplist構文は合成テストで検証したが、
Linuxのネイティブsystemd解析・サービス稼働は未検証。
実配備時は対象と権限を定めた完全合成テナント試験を行う。
既存Slack/Discordの実機適用にはHermes gatewayの再起動、LINE WORKSには
独立プロセスの導入・起動と公開Callback経路の維持が必要。
テスト成功はバグが一切ないことや実患者データによる安全性の証明ではない。

共有記憶は接続確認後の検索でTransport closedとなり、保存していない。
患者情報・秘密値・原本ログを記録せず、確認済みの結果を本ローカル記録へ残した。
