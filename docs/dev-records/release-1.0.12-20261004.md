# 1.0.12 ローカルのリリース準備

対象: mainのHEAD `b5b2958`と今回の未commit差分。版はユーザー指定の1.0.12、日付は日本時間2026-10-04。
1.0.12のlocal/remoteタグとGitHub Releaseがないことを確認した。生成・検証のみを実施し、commit/push/tag/公開/配備/稼働サービスの再起動はしていない。
既存の未追跡ファイル `8` / `recover` は読まず、変更もしていない。

## 成果物と訂正

- 変更記録2件のdetails欠落と禁止された山括弧を修正し、release_notes.py checkを成功させた。
- 既存生成器のbuildでCHANGELOGとREADME最新変更を1.0.12へ同期し、入力記録をchanges/archive/1.0.12へ格納した。
- READMEの機能・画面例・導入・安全・導線をソースに照合し、readme-review.jsonを具体的な新版の記録へ更新した。Slackの /mcs未実装という古い画面例説明を訂正した。画面の生成元/画像は変更不要で、両ギャラリーのcheckを成功させた。
- [新版の受入票](../development/ACCEPTANCE_1.0.12.md)は原1.0.11の未確認事項を引き継ぎ、公開完了とはしていない。
- Releaseのタイトルと本文は同じ1.0.12のCHANGELOGからexportした。一時成果物: /tmp/mcs-release-1.0.12-title-20261004.txt と /tmp/mcs-release-1.0.12-20261004.md。GitHubへ送信していない。

## 確認領域と制約

既存の全体レビュー記録と今回の経路レビュー・成功テストを再利用する。今回のリリース準備で無関係な整理や追加の全体レビューを始めない。

| 領域 | 照合した対象・証拠 | 判定・制約 |
| --- | --- | --- |
| コードの無駄・共有処理 | adapters/common/commands.py、hermes_plugin/__init__.pyと呼出元。既存parse/dispatch/preview/confirm/receiptを再利用 | 今回の差分確認済み。追加抽象化・依存なし |
| 配置・生成物・互換入口 | adapters、plugin互換入口、flat import、tests配置、DEVELOPMENT生成 | 今回の差分確認済み。誤ったnamespace判定をテスト配置で修正済み |
| 処理予算・性能 | ledgerのmetadata/actor対象、run_check、backoff/TTL/件数予算 | 既存予算を保持。実データ規模の性能計測は未実施 |
| 正しさ・保存・互換 | metadata/signal/view、合成回帰、旧版Ledger生成DB→schema8 | 確認範囲内で成功。各旧版checkoutの全更新は未確認 |
| 安全・プライバシー | actor/scope/患者権限/参照hash、6経路拒否ケース、安全ゲート、候補ファイルhygiene | 今回の差分確認済み。実サービスの認可・表示範囲は未確認 |
| 依存・CI・供給経路 | 固定SDK/固定Hermes pin、install.shとrequirements、workflow | 依存変更なし、固定SDK合成統合成功。最終SHAのCIは未確認 |
| テスト・build・導入/更新 | 全体4477、6経路36、固定SDK37、release専用22、既存導入/更新回帰、旧DB smoke | ローカル成功。新規の実依存導入/サービス起動と全更新元CLIの実施は未確認 |
| 文書・画面・運用・release資産 | CHANGELOG、README5項目、ガイド、画面ソース対応、受入票、export | ローカル同期/形式成功。MCS実画面はユーザー担当で未実施 |

## 検証

機能ソース・依存は先行成功時と同一のため、全体4,477件/26 subtests、6構成36件、固定SDK37件、安全ゲート8項目の成功を再利用した。4件スキップは実環境の成功へ読み替えない。
新版のrelease専用は22件/26 subtests成功。shellcheck、Slack7組・LINE WORKS2組の画像check、候補30ファイルの秘密パターン/禁止パス検査は成功。unknownの既存未追跡ファイルは対象外。
release_notes.py check、readme_release.py --check、update_readme.py --check、git diff --checkを成功させた。
追加の導入/更新対象テストは106件成功（81.46秒）。対象はtest_install_sh、test_install_sh_standalone、test_mcs_upgrade、test_lifecycle_standalone、test_ledger_candidate_validation。実サービス・本番設定は使用しない。

旧DB追加確認では、OSのnetwork禁止・秘密内容読取り禁止・一時領域外書込み禁止の下で、v1.0.0〜v1.0.10各タグのLedgerソースを一時コピーし、合成患者・投稿・artifactを保存した。
そのDBを現行Ledgerでschema7→8へ移行し、保存行・バックアップの旧schema/行・2回の再open・quick_checkを確認した。11版とも成功。
共通import helperは現行を使っており、完全な旧checkout・全設定・全更新CLI・rollback実行を証明しない。
スクリプトとログはタスクの隔離一時領域 historical_db_check.py / historical-db-check.log に保持する。現行形式DBのuser_versionだけを変えたfixtureではない。

## リリースskillの更新

ユーザーの追加指示により、正本 ~/.agents/skills/repository-release/SKILL.md と references/pre-release-audit.mdへ、新規インストール・過去版からの更新の受入を追加した。
対応を宣言する全更新元の追跡、実入口、旧形式の合成資産、保存データ保持、中断/再実行/復旧/rollback、環境不足を未確認とする境界を含む。既存の公開・配備権限を拡張しない。
skill-creatorの形式検証が成功し、相対参照とClaude Codeの正本symlinkを確認した。Codexはこのセッションのr0正本を参照する。他hostが更新後に実行したという検証はしていない。
代表判断は、新版リリース準備では導入/全更新元を追跡する、監査だけでは修正/公開をしない、単なる文書修正にリリースを起動しない、実機権限がなければ隔離検証と未確認報告に留める、の4ケースを本文に照合した。別モデルによるforward-testはしていない。

## 未完了条件

[1.0.12受入票](../development/ACCEPTANCE_1.0.12.md)の新規導入・全更新元の未確認条件、MCS原要求のユーザー実機確認と新コマンドの実接続確認を残す。
最終commitとそのexact SHAの必須CIは未実施。これらを解消してからタグ/公開へ進む。公開の承認と配備の承認は別であり、ここではどちらも実施していない。
