# 候補版の切替・停止・復旧条件

本書は未実行の作業手順。RF-OPS/G7の合格証拠ではない。本番切替、サービス再起動、設定・権限変更、実配送は未承認・未実施。

## 切替前に確定する対象

- MCS候補：`/Users/yusuke/.codex/worktrees/mcs-20260920-continuation`、基準HEAD `842125da858d4bddc09e7e778bee5e6068c521d9`。
- Hermes候補：`/Users/yusuke/.codex/worktrees/hermes-mcs-discord-20260920`、基準HEAD `b6e863a213777c3d9e75a9ae4e7a2c0b8a33566d`。
- 固定overlay `mcs-candidate-20260921-g0`はその時点の候補。後続の盲検candidate固定修正・文書修正を含まないため、切替対象としては再固定が必要。
- 実起動定義の全writer、Python実行パス、作業ディレクトリ、HOME、Hermes profile、plugin参照先、再起動対象を切替記録に明示する。候補repo内にはlaunchd定義がない。実配置の読み取り結果は末尾に記録する。

`run_check.py`はcheckoutの場所ではなくHOME配下の`.mcs/data/ledger.db`、`config.json`、`token_cache.json`等を参照する。候補ディレクトリから起動しても本番と隔離されない。ローカル検証は既存の隔離runnerを使用する。`--no-notify`も取得やDB書込みを無効化する指定ではない。

## 承認後の切替順序

1. 選定した固定候補と依存環境を別配置へ準備し、manifestと全ファイルhashを照合する。稼働中ソースへファイル単位で上書きしない。
2. 全writerの起動を止め、進行中処理の終了と共通lock解放を確認する。単に定期起動を止めただけで既存processが終了したとは判断しない。
3. SQLiteの整合backupと添付manifestを保存する。別ディレクトリへの復元でquick_check・件数・親返信関係・FTS・添付hashを検査する。原本への復元で試さない。
4. semanticをOFFにした設定で、承認済み起動入口を整合した候補版へ切り替える。既読方針は現行のままとし、自動ACKを追加しない。
5. 限定した観察範囲で既存取得・raw/添付通知・job・receipt・snapshotを確認する。実送信を許可されていない範囲には送らない。CCOには公開snapshotと限定inboxだけを渡し、原本・backup・credentialsを見せない。
6. Hermes側のprofile別plugin配置とnative認証を確認する。実Discordでpreview→confirm→原本適用→receipt反映を検証する場合は対象user/chat/projectと試験操作を事前に限定する。
7. OFFの回帰とRF-OPS成立後に、別途承認されたデータ・API予算でshadowへ進める。assist/enforceは各評価を経て別に判定する。G6未成立のままenforceへ進めない。

## 停止と復旧

semanticのOFFは新規seed・通信・自動drainを止める。pending、artifact、receipt、原文は削除しない。既に送信済みの通知や処理中の外部要求は取り消せない。

projectのpauseはOFFと異なり、意味機能ONで到着した新入力のpending jobを保存して実行を保留する。resumeで再取得なしに続行する。Discordのqueued応答では停止未完了で、原本のapplied receiptを確認する。

取り違え・許可外送信・secret混入・古い結果の上書き・重大な未支持要約公開・耐久job喪失があれば該当機能を停止する。取得・台帳自体が危険ならwriterも停止する。コードを戻す場合は現schemaとreader/writer互換性を確認し、対応する整合版へ切り替える。古いbackupで現DBを上書きして新しい原文を失う復旧、全artifact削除、過去全件再通知は行わない。

## 未完了の証拠

現在あるのは隔離環境の回帰・合成復旧・合成Discord結合と限定Jev smoke。実起動配置、OS権限/egress、実機再起動・復元、canary、200件以上の人手評価と性能実測は別途必要。実APIの既承認POST 1回・GET 1回は実施済みで、追加評価の予算として再利用しない。


## 実配置の読み取り確認（2026-09-21）

`~/Library/LaunchAgents/local.mcs-{check,cmd,deep}.plist`をplistlibで読んだ。サービス操作はしていない。3本ともPythonは`/Users/yusuke/.hermes/hermes-agent/venv/bin/python`、入口は`/Users/yusuke/.mcs/adapter/run_check.py`。WorkingDirectory/EnvironmentVariablesの指定なし。

| label | 起動条件 | 引数 |
|---|---|---|
| local.mcs-check | 毎時00/15/30/45分、RunAtLoad=true | --json --download-files |
| local.mcs-cmd | WatchPaths=/Users/yusuke/.mcs/data/cmd | --json --download-files |
| local.mcs-deep | 毎時07/37分、RunAtLoad=false | --json --jobs-only |

これはplist上の定義であり、launchdへの現在のロード状態やprocessの生存を証明しない。checkとcmdのRunAtLoad/監視条件を踏まえ、切替時に意図しない巡回を起動しないよう3本を一体で扱う。

実ディレクトリは`.mcs/data/cmd`と`.mcs/data/snapshots`で、候補のjob_ops/maintenance定数と一致する。`.mcs/cmd`と`.mcs/snapshots`は存在しない。上位`.mcs`と`data`は0700、cmd/snapshotsは0755。末端modeだけからCCOのアクセス可否を判断できないため、別利用者・mount・ACLを含む実権限検証は未実施のまま。

既定profileの`/Users/yusuke/.hermes/plugins/mcs-discord-commands`は存在しない。他profileの配置は未確認。plugin導入済みとは報告しない。inbox/snapshot設定例の`/mount/mcs/...`は公開mount内の例であり、上記ホスト実パスと同一視しない。


## CCO profileの読み取り確認

`~/.hermes/profiles/cco`は0700、config.yamlは0600。profileのpluginsディレクトリは存在しない。設定のplugins.enabledはbot-conversationのみで、mcs-discord-commands entryなし。既定profileだけでなく、対象CCOにもMCS pluginの導入が未実施である。

CCO configにはterminal設定がない。既定profileにはterminal設定があるが、profile間継承や実効backendをこのファイル存在だけから推測しない。環境変数・runtime scope・sandbox/mount/ACLの実効経路は未確認。特に0700は同じOS利用者のprocess同士を隔離しないため、これをCCOの原本書込み禁止の根拠にしない。

実配備前には、CCOの実行主体とterminal/fileツールの実効権限を確定し、公開snapshotは読取可・inboxだけ書込可・原本/backup/credentialはアクセス不可を非機密canaryファイルで検証する。原本やcredentialを試し読みする方法は採らない。必要な配置/権限変更は具体的な切替対象へ含めてから承認を受ける。今回行ったのは設定キー・指定対象・パス属性の読み取りのみで、設定本文の外部送信やサービス変更はない。


## CCO terminal設定経路の確定

追加読取りでCCO `.env`にもTERMINAL_*指定がないと確認（秘密値は表示せず、terminal関連キーだけ抽出）。既定profileはterminal.backend=dockerだが、CCOにはterminal節がない。CCO Discord toolsetにはfileがあり、CLIにはfile/terminalがある。

候補Hermesのgateway.run._profile_runtime_scopeは対象homeへinstall_and_reset_profile_terminal_scopeを適用する。tools.terminal_scope.build_profile_terminal_scopeはDEFAULT_CONFIG→対象profileの.env→対象config.yamlを投影し、ambientや既定profile設定へfallbackしない。DEFAULT_CONFIG.terminal.backendはlocal。従って現在のCCO設定をこのrouted経路で解決するとlocalになる。これは設定とコードからの結論で、稼働process内の現在値や別の外部sandboxを実測したものではない。

この状態をsnapshot/inbox限定の隔離済み配置として承認対象にしない。profile専用backend・公開mount・fileツール経路・gateway pluginの実行主体を合わせた設定案が必要。単に既定profileのDocker設定をコピーすると無関係な公開mountも引き継ぐ可能性があるため、公開対象を列挙した最小設定で設計する。権限変更は未実施。
