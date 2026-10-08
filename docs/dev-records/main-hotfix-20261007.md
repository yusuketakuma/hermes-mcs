# main統合とアラートhotfixの適用・影響確認

2026-10-07、ユーザーが現在ブランチ全体のmain統合、本人以外を本人として扱うアラートの修正、該当スレッドへの追加投稿、UIUX、遅延解析の上書き/途中表示、統合後の重点確認・修正、コード共有化/整理を明示依頼した。

## 実装と統合

- develop/1.0.16の全変更を496c58bへcommitし、mainを同commitへfast-forwardした。既存mainと並行変更をreset/stash/cleanしていない。外部push・PR・公開・tagは実施していない。
- 本人/family/other/unknown、否定、過去/予定/条件、報告者、複合人物表現を原文spanで判定。本人の現在の急変は維持し、至急返信等の依頼は臨床的な本人急変と分離。家族の値・イベントは原記録として保持する。
- 通知・集約・シグナルの同じ主体guardと旧cacheのclinical読取り保護を整合。旧/minschema/parsed互換とprofileを維持し、家族の薬剤中止で本人の同薬を消さない。
- urgent/signalはverified source MID/coverage/recipient/receiptに束縛した既存threadへ追加する。未確定時は保留、別channelへfallbackしない。確認・担当・却下・タスク操作を維持。日次一覧/system noticeは患者単一threadへ混ぜず従来指定先を維持。LINE WORKSのnative thread配送は未対応holdを明示する。
- 長文/高密度を意味境界で事前分割し、全文/薬名と用法/主体/共有predicateを保持。referenceだけをcore evidenceへ昇格させない。source/model/prompt/plan-bound checkpointから再開する。
- 遅延解析は既存投稿を更新し、途中countsだけを「解析更新中」、確認保留を別表示、完了後に解除。進捗だけで確認のsource世代を失効させず、unsettled senderと競合させない。commit後の共通refreshはdurable queueのみ、SDK直接送信なし。
- 描画内の進捗読取りをMIDごとに一度へ共有。動的plugin/互換import/独立復旧・migrationはcaller実証済みのため維持。削除/移動の安全な確定候補はなく、不要な機械的分割はしていない。JGはHTTP402により利用不能、31コードファイルのローカル照合へ切替えた。全repoのdeadcode不存在は主張しない。

## 検証と修正

一式9003成功/3SDK skipで28失敗を捕捉。旧signal channel-root前提は元source deliveryとnative receiptの合成fixtureへ更新し、操作/権限/復旧/未知送信のassertを維持した。実不具合はdb=None/minrow/旧schemaの互換、sender未設定/CLI欠如判定順序で、root causeを修正した。期待を弱める・skipを加える・安全gateを外す対応はしていない。

- 影響主要6領域（adapters/extract/semantic/notify/views/ops）:6665 passed/3SDK skip、残1件のmissing-exe順序は関連70件で修正/再検証。無変更領域の一式成功結果は再利用。
- source/frame互換とnative dismiss等276件、db=None/digest等86件、preview/notify等80+100件、主体guard220件を確認。
- 統計のrequest_only/scope_or_evidence_held分類は143件、refresh/文境界等21件が成功。句点を次区間へ送って引用を切る共有text_chunksバグはindex+1で修正し、関連48件と独立quote回帰を確認。
- main統合後の重要経路110件（主体・旧cache・signals・thread routing・進捗・刷新・統計・Slack/Discord native stub）が成功。
- CI範囲ruff、gates10/10、変更記録、生成文書、README同期/リンク、diff検査、Slack合成8画面のSVG/PNG/hash一致が成功。画像07/08は目視で文字切れ・重なりを確認した。

合成SDK/stubの成功を実患者の臨床精度・検出率・実端末操作の受入と同一視しない。実SDK依存が無い3moduleはskipであり成功とは扱わない。任意のcanonical昇格gateやclinical_metadata有効化は変更していない。

## 実機適用

既存update lock/停止/再開手順でMCS workerを一時停止し、私有data/backups/main-hotfix-20261007-173004へ台帳のSQLite backupと設定を保存した。DB integrity_check=ok、owner-onlyのdirectory/fileを確認し、原本と履歴を削除していない。

main 496c58bへの統合後、抽出worker再起動を検証し、gateway restart reportはsupervisor_restart_verified。Hermes gatewayの実PID生存・home一致と、両抽出worker/command watcher/復旧watchdogのloadを確認した。user domainのgatewayとgui domainのMCS servicesを区別して確認した。

実機の復旧Python明示値が3.14.8で現契約3.11〜3.13範囲外だったため、導入済みHermes3.11.15の独立実体をsetup init --recovery-pythonで明示選択。servicesによる所有/idle検証と同期後、installer --no-brew --no-llm --no-servicesで外置きtoolを原本へ同期した。旧recovery世代は.prevへ保持。最初のinstallerはdesired/deployed driftで何も書かず停止し、既存手順に沿って修復後returncode0。設定差分はrecovery_pythonのみで、認証情報/通知先/権限/解析設定を維持した。実機setup check returncode0、保留marker無し、main作業ツリーcleanを確認した。

新LLM方式の中間ビルドは同一投稿の私有一時DB保存・読み戻し一致を703.9秒/1sliceで確認。元の934.5秒/2sliceとはcache条件・粒度が異なるため速度改善率と扱わず、最終hotfixの実精度/網羅性評価とは区別する。全実データ評価はローカル限定、原文/名前/識別子/実値は報告・fixture・外部サービスへ含めていない。一時DBと使い捨てharnessは終了後に削除した。

## API確認と制約

Slackの既存Bot投稿の上書きは[chat.update](https://docs.slack.dev/reference/methods/chat.update/)、元threadへの追加は[chat.postMessage](https://docs.slack.dev/reference/methods/chat.postmessage)の既存ID経路を利用する。Bot自身が所有する投稿とscope/receiptが前提。LINE WORKSの現[Bot API](https://developers.worksmobile.com/jp/docs/bot-api/?lang=ja)と[トークルーム送信](https://developers.worksmobile.com/jp/docs/bot-channel-message-send?lang=ja)では当該native thread/既存message更新契約を確認できず、未実装として保留する（非対応を推測で補完しない）。実Slackへの試験投稿を追加して成功を装う操作は行っていない。

## 追加修正の本番適用とローカル履歴整理

同日、ユーザーの「本番環境に適用」と「マージ済みの不要なブランチ・ワークツリーを削除」の明示指示により、後続のレビュー修正・スレッド表示・正常監視通知の抑制を適用した。

- 修正ソースをmainの`e4b2bf1`へcommit。ローカル111ブランチを照合し、96件はmainの祖先、残る15件・44コミットはすべて既適用の同等パッチで、非等価パッチや独自merge commitは無かった。15件の履歴を`cc5e779`へ統合し、ソースtreeが`e4b2bf1`から変わっていないことを検証した。
- `data/backups/production-followup-20261007-210258/`へGit履歴bundle、未commitのソース差分と新規ファイル、設定とSQLite backupを保管。bundle検証・DB integrity_check=ok・schema 9一致・私有権限を確認した。不要worktreeの未分類ignoredファイル1件もbyte一致を検証して保全した。原本DB・設定・保存履歴を消去していない。
- 既存update/run lockとquiesce/resume手順を利用。Gatewayは所有検証付きの再起動報告`supervisor_restart_verified`、実gatewayの生存・`hermes_home`一致・ソース更新後の起動を確認。両抽出workerは別PIDへ置換し、コマンドwatcherと復旧watchdogのロードも確認した。設定のbytesは適用前と一致。
- 余分なworktreeの実機plist・plugin・cron/wrapper・復旧先への参照が無く、追跡変更・未追跡ファイルも無いことを確認して2件削除。全local headsがmainの祖先になったことを再検証し、main以外110件を削除した。現在はmainの1ブランチ・1worktree。リモートrefs・tag・GitHub公開は変更していない。詳細な削除対象と復元先は私有backup内の`cleanup.json`に記録した。
- 本番`mcs_setup check`はreturncode 0。LLMのモデル・slot probe再確認はerrors/warningsとも0、既存LLM serviceも稼働。新しい監視観測はstatus=ok・alert=false・disk_alert=falseで、送信待ちの補足だけから正常警報を出していないことを確認した。実患者の内容・原文・秘密値は出力していない。

今回の修正後の合成統合は2,105 passed/8 skipped、CI gates10/10、ruff・文書・SVG/PNG整合が成功。本番Python3.11.15と導入SDKの版は確認し、同runtimeの隔離されたedit-only/guard試験34件は成功した。Gateway SDK統合2経路は隔離HOMEでHermes bootstrapが再execした先にpytestが無く未完了のため、実API受理やGateway統合成功とは扱わない。導入済みPython/SDKの確認前後の版は一致しており、依存追加・変更は適用操作として実施していない。

正常・正常復帰の監視警報を抑え、異常とディスク低容量の警報は維持する。Slack/Discordの自動要約・解析進捗は親カードへ集約し、スレッド本文と添付見出しは要約を重ねない。既存named区画の更新は同一スレッドの配送証明付き投稿IDに限定し、結果不明・容量不足は保留する。旧形式の凍結投稿とLINE WORKSの編集制約は保持し、過去の全投稿を一括削除していない。
