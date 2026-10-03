# 1.0.11 ワークツリー統合・マージ後検証（2026-10-03）

## 範囲と結果

ユーザーのローカルマージ・全体レビュー・テスト・修正指示に従い、mainの既存ロードマップ修正を保持したうえで、release作業ツリーの53ファイルとfeature/stamps-1.0.11の4コミットを隔離統合した。元の作業ツリーは変更しない。

- 開始main: `9ea015d`。ロードマップの既存7件修正を`8f38eb1`に記録。
- 統合: `4978c96`（releaseの未コミット成果を保存）、`2290b42`（featureブランチのマージ）、`f6976e6`（統合後修正）。mainへfast-forwardマージ。
- 衝突は日次サマリーと計画文書。スタンプ観測・共通サマリー・更新手順を併存させ、既存の取得/公開境界・明示自己設定・signalライフサイクル・#8-M1依存を保持した。
- 収集・DB移行・自己識別・返信比較・capture/shadow保存、通知の本文/表示/配送結果・再送上限、3接続先の認可とscope、更新のバックアップ/復元承認、文書・変更履歴を照合した。今回の範囲で未修正のマージ阻害事項なし。

## 修正

1. 本人スタンプ集計にサマリーの許可患者範囲を適用。captureだけを使いshadowは非公開を維持する合成回帰を追加。
2. 定期サマリーの明示日数を表示内容と記録した期間へ反映。日数未指定は従来の前回終端方式を保持。`days:3`と`日数:1`の合成回帰を追加。
3. snapshot更新日時を読取り開始時に取得し、消失時のOSErrorを固定エラーに変換。読取り後のファイル削除でも回答できる合成回帰を追加。
4. 独立Slackの実SDKテストの登録数を新規サマリーコマンドを含む6へ更新。接続時のassert失敗でもhost停止を通知し、無限再接続待ちを防ぐ。初回SDK実行は21件進行後に停止し、その隔離テスト子だけを終了。修正後に全23件が成功。
5. Discordコマンドの担当名は`name:`で指定する契約へ文書を訂正。公開範囲は実機未確認のため患者名を表示しない。Slackの返答はSDK実装を確認し、別Webhookクライアントを作る`respond`から既存の`_say/chat.postEphemeral`へ変更。固定URL/no-redirect/no-proxy/no-retryを維持し、認可外・チャンネル不明の入力には返答しない。実SDKのslash command配送で非公開回答を確認。
6. 変更記録12件から正規generatorで未公開1.0.11のCHANGELOG/READMEを再生成。1.0.10以前の本文が完全一致することを確認し、READMEの5観点の確認記録も更新。

## 検証

- 隔離統合版の初回全体: **4340 passed、4 skipped、26 subtests passed**（261.06秒）。
- 集計・snapshot修正後の局所回帰: **76 passed**。最終Slack返答修正後のadapter回帰 **29 passed**、独立SDK一式 **23 passed、exit 0**。最終変更はこの局所検証で再確認した（全体4344件はその前の統合版）。
- mainマージ後の`make test`: **4344 passed、4 skipped、26 subtests passed**（260.03秒）。4 skipはSDK依存対象であり、通常一式だけではSDK成功と扱わない。
- Hermes固定ref `fd50a275e2616118c48fe07e7e1c878782b15ccd`、隔離Python 3.11.14で4対象 **14 passed、0 failed**。CIのPython 3.13とは異なる。
- 独立SDKの固定requirementsを照合した隔離Python 3.13.16で3対象 **23 passed、skipなし、exit 0**。
- `make check gates`: lint・README生成差分・release見直し記録成功、静的gate **8/8成功**、既存incidentのcoverage成功。
- `release_notes.py check`成功。Slack合成画面7組・LINE WORKS合成画面2組のgenerator `--check`成功。
- `git diff --check`成功。ソースワークツリーを保持し、秘密値・患者データをfixtureや記録へ含めない。テストは一時HOME/合成DB/スタブ・通信遮断で実行した。

## 未実施・制約

実MCS・実配送・実画面での受入、サービス再起動、push・tag・Release公開は行っていない。第1層shadowの未読保持実証・最終手動受入は既存のACCEPTANCE_1.0.11に残す。ローカル合成テストとSDK境界の成功は、それらやGitHub CI・配備の成功証明ではない。
