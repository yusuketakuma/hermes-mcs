# 2026-10-04 全機能の接続経路とレビュー修正

## 対象と変更

最新5コミットのレビューで確認した4件を修正し、既存機能も全経路の対象とする指示に従った。
ローカル実装・合成データ検証のみ。配備・外部送信・実データ操作は行っていない。

- 不正な再取得で残った以前のスタンプを現在の有効値として扱わない。shadow取得のエラーが新しければ本人対応の判定を保留する。不正なshadowはcaptureへ公開しない。
- 本人の承知・完了で解消したPRUも設定された候補監視期間内は再取得対象に残し、取消を公開した場合はシグナルを再開する。既存の間隔・backoff・件数上限を維持する。
- 押した人の取得と鮮度判定は最新の正常なcapture/shadow観測を参照し、shadow公開の有効・無効に依存させない。
- スタンプの件数ゼロと種別省略を同じ件数として比較する。

既存のDiscordコマンド検証・snapshot・preview/confirm・cmd inbox・receiptを再利用した。
Slackは既存Bolt接続に `/mcs`、LINE WORKSは本人1:1トークに `mcs <JSON>` を追加した。
Discordの既存入口を拡張して、QC・read model・統計・シグナル・metadata_reportの閲覧と
シグナル除外・抽出訂正・ポリシー変更・参照統計承認・配送手動解決を同じ契約へ接続した。
患者指定のレポートは対象患者に限定し、全体集計はsnapshot内の全患者に権限がある場合だけ許可する。

## 経路の検証範囲

| 構成 | Discord | Slack | LINE WORKS |
| --- | --- | --- | --- |
| Hermesあり | native command context / `/mcs` | 既存Bolt / `/mcs` | 独立署名Callback / 本人トーク |
| Hermesなし | standalone native context / `/mcs` | standalone Bolt / `/mcs` | 独立署名Callback / 本人トーク |

`tests/adapters/standalone/test_native_commands.py` の36件は6構成に対して共通の
全18閲覧kind・status・全13運用操作のpreview/confirm/queueを検証する。
正式依頼は作成・更新・原本への合成適用・公開snapshot・receiptまで確認する。
別患者・別操作者・偽actor・別接続先・変更されたシグナル参照・異なるhashでの確定を拒否する。
全体集計は一部患者権限では拒否し、全患者権限では成功する。
運用操作13種の原本側実行は既存opsテストが担い、経路テストで実環境の更新や復元を実行しない。
本文・添付・要約・カード・DM入力・署名Callbackの既存テストも全体検証に含める。

固定SDK検証はOSのnetwork禁止・秘密ファイルの内容読取り禁止・一時領域以外への書込み禁止と
共通conftestの通信/Keychain/Chrome遮断の下で実行した。Hermesのインストール済みsite-packagesは
コードと依存の参照だけを許可し、稼働設定・会話・認証情報にはアクセスしない。

- 独立SDK: Python 3.13.16、discord.py 2.7.1、slack-bolt 1.30.0、slack-sdk 3.45.0。統合23件成功。
- Hermes: 固定ソース `fd50a275e2616118c48fe07e7e1c878782b15ccd`、Python 3.11.14、discord.py 2.7.1、slack-bolt 1.30.0、slack-sdk 3.44.1。統合14件成功。CIのPython構成とは区別する。

最初の全体実行では新規テストをtests/adapters直下に置いたため、flat importにより
SDK不在のdiscordディレクトリがnamespaceとして見つかり9件が失敗した。
既存のstandaloneテスト領域へ移し、skip条件を弱めずに原因を除いた。
生成文書のテスト数も再生成した。SDK検証の隔離設定を調整した途中の失敗ログは一時領域に保持した。
Slackの新規コマンド登録に伴いSDKのlistener数は6から7へ更新した。

## 最終検証

- `scripts/run_tests.sh -p no:cacheprovider`: 4,477件成功、4件スキップ、26 subtests成功。SDK不在等のスキップは固定環境の別検証と区別する。
- 6構成の36件はreceiptの種別境界の追加確認後も対象実行で成功した。
- 全指定範囲のruff成功。安全ゲート8/8成功。mine_gates、README生成整合、readme_release整合、diff whitespaceチェック成功。

## 適用と制約

Slackアプリに `/mcs` と `commands` を登録し再インストールする必要がある。
更新コードの反映には該当Hermes gatewayまたは独立アダプターの再起動が必要。
LINE WORKSはHermesにnative接続がないため、どちらの構成も同じ独立アダプターを使用する。
既定の取得・通知・公開・承認条件は変更しない。原本の既読化や人承認ゲートは維持する。
実サービスへの接続・送信・稼働プロセスへの適用は未実施であり、合成/SDK検証成功と同一視しない。
