# スタンドアローンモードの計画・検証記録

## 目的と互換性

Hermes Agentをインストールせず、hermes-mcsの収集・保存・検索・抽出・通知・
カード操作・人承認・定期実行・監視・更新・復旧を動かす。
Hermesがある環境では従来の連携も独立モードも動作する。
上流Hermes Agentのコード、実データ、実サービスには変更を加えない。

`runtime_mode`は`hermes`または`standalone`。未指定は従来の`hermes`とする。
収集・解析コアは標準ライブラリのみを維持し、接続先の既存公式SDKだけを
独立モード専用venvへ導入する。既存の配送・カード・操作・承認契約を再利用する。

開始時点は`c5a7716dda213263ab3f46388be3336db2730c79`、作業treeに差分なし。
最初の指示からの依存調査と局所初稿を保存した段階で、追加された実施順を反映した。
以下の計画レビューを終えてから実装を再開する。

## 実施順

1. 計画：実ソース・呼出元・公式仕様から依存と受入条件を確定する。
2. 計画レビュー：機能同等性、認証境界、二重送信、起動・更新・復旧の穴を確認する。
3. 実装：共通ランタイム、接続、運用・導入を責務ごとに実装する。
4. テスト：局所回帰、実SDKのオフライン統合、既存全体テストを実行する。
5. レビュー：両モードの呼出契約・失敗経路・安全ゲートと最終差分を確認する。
6. コード整理：確認した重複・不要コードと入口の不整合を整理する。
7. フォルダ・リポジトリ全体整理：全追跡ファイルの配置・導線・生成物・設定例を
   点検し、今回の変更と既存構成を整合させる。実データや履歴資産は削除しない。
8. 再テスト：整理で影響した検証とプロジェクト必須チェックを実行する。

## 実装単位

| 領域 | 実装と保持する契約 |
|---|---|
| モード・パス | `mcs/core/mcs_runtime.py`でmodeとvenv/scripts/modelsの所有先を解決。既存設定は同じHermes経路へ |
| 独立入口 | `mcs_standalone/`にinit/check/run/send/status/service、scope検証、0600認証ファイル、task寿命管理 |
| Discord | `adapters/discord/standalone.py`で公式SDK接続。現行Supervisor、カード、thread、添付、modal、preview/confirm、`/mcs`を再利用 |
| Slack | `adapters/slack/standalone.py`でBolt Socket Mode接続。現行Supervisor、Block Kit、本人確認、thread、添付を再利用 |
| LINE WORKS | 現在の独立接続を継続し、独立hostから起動・停止する |
| テキスト通知 | `notify_flush`が選択modeのCLIへ配送。同じjournal、添付pin、unknown抑止を維持 |
| 定期・常駐処理 | 独立hostが既存6定期ジョブ、2抽出drainer、cmd/cmd_intの処理を所有。LLMと復旧watchdogはnative supervisorが管理 |
| 導入・診断 | `install.sh --mode standalone`、独立venv、モード別init/check/doctor/services。Hermes未導入を正常な状態として扱う |
| 更新・復旧 | update marker中はbackgroundを停止し再生成しない。updaterは維持。完了後再起動要求でhostが後処理後に再execする |
| 互換・排他 | Hermes factoryにも選択modeを検査。mode切替ではmanifestが所有する旧jobだけを停止し、共有gatewayを停止しない |
| 文書・配置 | README、導入/AI手順、安全文書、AGENTS、変更記録、生成表を現在の実装へ同期 |

独立hostの状態は`data/standalone-status.json`にPID・generation・時刻・子PIDと
job名・更新marker状態だけを記録し、本文・名前・秘密値は記録しない。
`data/standalone-restart.request`は承認済み更新の後処理へ使用する。
状態だけで生存を推定せず、TTLと実PIDを検査する。

## 計画レビューで確認するリスク

- 送信のみを独立させるとcron、watcher、更新、復旧がHermes必須のまま残る。
- 設定未指定時の挙動を変更しない。両mode・旧pluginが同じinboxを消費しない。
- SDKのproxy環境取得、redirect、DEBUG本文ログ、送信retryを明示的に制御する。
- workspace/application/channelを認証結果に照合し、設定・認可変更は受信/送信を停止する。
- 成否不明の送信を再実行しない。確認・理由・receipt・snapshot・添付pinを緩めない。
- Discord `/mcs`の登録で既存の他コマンドを一括上書きしない。
- update中にnative hostを停止すると自身のupdaterも失われるため、markerと終了後execで協調する。
- 復旧watchdogはrepoが壊れても診断でき、秘密値や送信先を推測しない。
- SDK・接続例外を独立入口だけへ限定し、既存SDK禁止・coreの標準ライブラリgateを維持する。
- 既存の環境値検索が`~/.hermes/.env`へfallbackするため、独立modeは自身の明示設定だけを読む。
- 接続先SDKが所有するinteraction taskも回収し、認可変更後の処理中confirmを継続させない。

## 受入条件と証拠

| 条件 | 必要な検証証拠 | 状態 |
|---|---|---|
| Hermesなしの全MCS機能 | Hermes import/CLI拒否環境で独立起動、6job・inbox・drainer・通知・操作・診断を実行する合成fixture | 未実施 |
| Hermesありの互換性 | mode未指定/hermesの既存テスト、pinned Hermes SDK統合、standalone選択時の排他 | 未実施 |
| Discord同等性 | 実SDKのserializationとstub通信、カード/原文/添付/native `/mcs`/本人confirm/receipt/再接続/停止 | 未実施 |
| Slack同等性 | 実SDK/Boltのstub通信、Socket Mode/カード/原文/添付/modal/本人confirm/receipt/再接続/停止 | 未実施 |
| LINE WORKS維持 | 既存テストと独立hostの起動・停止・設定変更 | 未実施 |
| 通信・認可の保護 | foreign scope/user、proxy/redirect、秘密ログ、unknown再送、古いボタン・確認、添付不整合の拒否 | 未実施 |
| 導入の簡便性 | 隔離したinstallerと設定/service fixture、Hermes clone/CLI不要、専用venv・診断・AI手順の一致 | 未実施 |
| 運用・切替・更新・復旧 | 両mode、旧所有jobのみ停止、marker中の子停止・再生成抑止、更新後の再exec、repo外復旧の合成検証 | 未実施 |
| レビュー・整理 | 実装後レビュー、追跡ファイル全体の配置点検、導線・import・生成物・最終diffの確認記録 | 未実施 |
| 最終検証 | 全テスト、SDK統合、lint、README同期、両gallery、release記録、incident gate・coverage、整理後再テスト | 未実施 |

実テナント、実MCS、原本DB、Keychain、実LLM、稼働サービスへは検証で接続しない。
これらの実機動作をオフライン検証の成功として報告しない。

## 公式情報とソース

- [Discord interaction](https://docs.discord.com/developers/interactions/receiving-and-responding)
- [discord.py intents](https://discordpy.readthedocs.io/en/stable/intents.html)
- [discord.py 2.7.1](https://github.com/Rapptz/discord.py/tree/v2.7.1)
- [Slack Socket Mode](https://docs.slack.dev/tools/bolt-python/concepts/socket-mode/)
- [Slack acknowledge](https://docs.slack.dev/tools/bolt-python/concepts/acknowledge/)
- [Slack Python SDK](https://docs.slack.dev/tools/python-slack-sdk/web/)
- [Slack bot identity](https://docs.slack.dev/reference/methods/bots.info/)
- [Slack upload SDK](https://docs.slack.dev/tools/python-slack-sdk/reference/web/async_client.html)
- `adapters/{common,discord,slack,lineworks}/`、`hermes_plugin/{__init__,card_workers,projects}.py`
- `mcs/notify/notify_flush.py`、`mcs/ops/{mcs_setup,mcs_update}.py`、`deployment/`、`install.sh`

Jevgrepは`mcs/notify`、`hermes_plugin`、`mcs/ops`、`deployment`、各接続先を検索した。
request-limitにより探索は不完全だったため、返された候補と呼出元を実ソースで補完した。
検索結果だけを機能同等性やテスト成功の証拠にはしない。

## 段階の記録

- 計画：依存調査、公式仕様、担当領域、受入条件を上記に記録した。
- 計画レビュー：2026-10-01、親エージェントが実ソースと各領域の調査結果を照合して実施した。
  `Supervisor`の寿命管理、nativeコマンド、6cron/4agent、固定インタプリタ、更新postcheck、
  repo外復旧、環境fallbackを確認した。送信のみの変更では不足する点を計画へ反映した。
  SDK instanceごとの通信制約・task回収、mode未指定の互換、旧workerのmode監視、
  update子を維持する単一host、TTLとPIDによる子復帰確認を受入条件に含めた。
  既存他コマンドの一括置換と共有gatewayの停止を避け、局所SDK例外だけを認める設計で承認した。
- 実装：計画レビューの上記条件に従って再開する。
- テスト以降：未実施。保存済み局所初稿は今後の検証対象であり、完了の証拠ではない。
