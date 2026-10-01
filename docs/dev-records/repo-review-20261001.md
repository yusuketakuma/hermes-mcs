# リポジトリ全体レビュー（2026-10-01）

LINE WORKSの追加・他機能への集中影響レビューから対象を全リポジトリへ広げた。
開始時446パス（現存437、既に移動済みの旧nativeアダプター9パス）を割り当てた。
先行stability確認一覧と361パスのhashが一致し、その確認を再利用した。
同一ファイルをすべて再読したという意味ではない。変更・新規・領域間callerを
優先して実ソースを確認し、再現した5件を修正した。
各パスの方法・状態・最終hashは[確認一覧](repo-review-20261001-progress.json)へ保持する。

## 範囲

| 担当 | 領域 | 方法と判定 |
|---|---|---|
| core/ingest | runtime14・test/helper16、共通HTTP・DB・ロック・既読化・ジョブ・添付保持 | 開始時30/30が先行hashと一致。caller境界を追加実読し、異常JSON2件を修正 |
| extract/semantic/views/evaluation | 122パス、世代選択・公開・CAS・admission/budget・候補/確定・読取りsnapshot | 開始時122/122が先行hashと一致。公開から表示/通知までを追加実読し、資格情報消失1件を修正 |
| ops/deployment/installer | 47パス、設定・CLI・更新/rollback/復旧・既存資産・人承認・writer/cron/サービス所有 | 40パスの先行hashを再利用し、残る変更7パスとcallerを照合。診断URI1件を修正 |
| 親 | notify/3 adapters/Hermes plugin・CI/release/tooling・統合テスト・文書/資産/生成物 | 直前の実装・レビュー・固定SDK結果と同一hashの先行確認を再利用。Callback記録1件と導入/安全文書を修正 |

新旧native入口の同一module、Discordのsingle-post guard、Slackの再接続状態、
v1/v2/v3のgrant/receipt、scope/epoch切替・restore/unknown holdは直前のレビューと
合成回帰を再利用した。Slack/Discordの接続・認証はHermes公式のまま、
LINE WORKSだけが独立接続を所有する。Hermes Agent本体は変更していない。

## 確定した問題と修正

1. **異常な保存ジョブJSONで収集・command処理が停止**。
   `job_ops`の4解析と`Ledger.job_payload`が深いJSONの`RecursionError`を捕捉しなかった。
   既存`loads_dict`へ統一した。異常ジョブは既存の`invalid_payload`で隔離し、
   後続患者の保存、既存import操作による復旧、正常payloadを維持する。
2. **HTTP200の異常JSONが患者単位の失敗分類を抜ける**。
   不正UTF8・深いJSON・巨大整数を固定文言の`SchemaError`へ戻した。
   `MCSError`→batch.error→患者単位のincomplete経路へ届き、既読化を抑止する。
   既存の非JSONログイン応答の`SessionExpired`と正常応答は維持する。
3. **特殊文字を含む診断用DBパスが別DBを参照・作成**。
   `_queue_warnings`の未escapeなSQLite URIを既存`Path.resolve().as_uri()`へ統一した。
   `#`・`?`・`%23`を含むパスで正しいDBを参照し、読取り専用を維持する。
4. **検証済みcanonical所見から解釈条件が消える**。
   verifiedは肯定・患者本人・実施済みを意味しない。v2 docの実投影から
   否定・推測・家族・予定の4例を再現した。既存描画の属性名を再利用し、対象・
   極性・確度・状態・時点等を短縮本文より前に表示する。旧metadata無しは互換を保持。
   保存形式・世代選択・公開条件・臨床的な品質判定は変更していない。
5. **破損したLINE Callback完了記録で後続処理・状態表示が止まる**。
   期限整理とstatusに容量制限付きの共通readerを使用する。
   不正JSON・型・UTF8・状態値・過大記録はunknownとして保持する。
   後続入力を処理でき、同じCallbackは再実行しない。本文・秘密値は表示しない。

`SECURITY.md`の接続境界、AI導入手順のHermesなしLINE設定・常駐要否・
`--no-notify`条件、文書索引、生成Wikiの正本briefを現実装へ整合させた。
生成Wiki本文と履歴資料は直接編集していない。

## 探索・公式根拠・制約

安全確認したPython/shellソース121ファイル（2,622,420 bytes）の一時コピーへ
jgを実行した。mcs全領域・adapters・plugin・独立CLI・scripts・deployment・ciが対象。
25関連ファイルを得たが、12リクエスト上限で**discovery incomplete、exit 2**。
全領域の網羅証明にせず、rg・実ソース・caller・担当棚卸し・合成回帰で補完した。
秘密値・PHI/PII・実DB・config/env・患者export・依存ソースは検索対象外。

[公式Service Account認証](https://developers.worksmobile.com/jp/docs/auth-jwt)と
[公式Callback仕様](https://developers.worksmobile.com/jp/docs/bot-callback)を再照合した。
API認証・署名・許可先・人承認条件は維持している。
生成Wikiの古い外部Jev経路・Slack/LINE・既読化・リンク記述は先行記録を再利用し、
正本briefへLINEを追加した。再生成・生成結果の照合は未実施。
実サービス/テナント、原本DB、Keychain、実LLM/Jev、実モデル品質、人間評価、
実配送、Linux/macOSのサービス反映、push/deployは未実施。

## 検証

- core/ingest: **560 passed**、新規回帰10件（12.49秒）。
- setup/LINE診断: **151 passed**、特殊URI回帰3件（6.24秒）。
- views/関連通知・投影・描画: **403 passed**（3.10秒）。
- LINE Callback/runtime: **66 passed**、破損記録回帰5件（0.33秒）。
- 最終全体: **3532 passed、1 skipped、23 subtests passed**（235.26秒、exit 0）。
  既定環境でSDK不在のDiscord実SDK検証はskipし、固定SDK環境で別途成功した。
- 固定Hermes `fd50a275e2616118c48fe07e7e1c878782b15ccd`:
  **4ファイル・14 passed、0 failed**（2.1秒、exit 0）。Python 3.11.15、
  discord.py 2.7.1、slack-sdk 3.44.1、slack-bolt 1.30.0。
  一時ソース・HOME・認証/通信guardを使用し、既存venvは依存の読取りのみ。
  CIのPython 3.13環境や実配送を検証したという意味ではない。
- `make lint`、`ci/gates.py` **8/8**、`ci/mine_gates.py --check`、
  `update_readme.py` / `--check`、`readme_release.py --check`、
  `release_notes.py check`、`generate_slack_gallery.py --check`（合成7画面）は成功。
- `shellcheck install.sh scripts/*.sh deployment/scripts/*.sh`、`bash -n install.sh`、
  forbidden/secret/stale-reference hygiene・plugin manifest sanity、`git diff --check`は成功。
- 開始時446パスのうち変更は18パス、新規は変更記録・本書・確認一覧の3パスのみ。
  残る428パスの開始hashは一致し、既存ファイルの消失はない。
  最終確認一覧の448パスは、現存439ファイルのhashと移動済み9パスの状態が一致。
  確認一覧自身のhashは自己参照を避け省略する。

開始hashは`/tmp/mcs-repo-review-start-20261001.json`、JG結果は
`/tmp/mcs-repo-review-jg-20261001.log`。主要結果と確認方法は本書・確認一覧に保持する。
全件テスト成功も、あらゆるバグの不存在や実環境の成功を保証するものではない。

## コミット準備（同日）

ユーザーの全変更コミット・push指示に合わせ、収集・抽出、意味解析、表示・出力、
接続・導入、文書・検証記録の5グループへ整理した。各実行時修正と変更記録を
同じcommitへ含めるため、今回の集約変更記録1件を収集・所見・LINE/診断の
3件へ分けた。元の6つの技術説明と更新条件を各領域へ引き継いだ。
実行コード・テスト・設定は変更せず、上記の全体テスト・固定SDK結果を再利用する。
上記448パス・新規3パスは全体レビュー完了時の数であり、記録分割後の
確認一覧は450パス（自己JSONを除外）、現状は451パスとなる。
push未実施の記述は全体レビュー時点の状態を表す。配備・稼働サービス変更は含めない。
