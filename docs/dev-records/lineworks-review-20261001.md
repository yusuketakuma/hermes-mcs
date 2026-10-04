# LINE WORKS全経路の再レビュー（2026-10-01）

LINE WORKS独立接続・共通配送・Slack/Discordの移動後互換・導入経路を再確認し、
再現した不具合をhermes-mcs内だけで修正した。Hermes Agent本体は変更していない。
実資格情報・実DB・実接続・配信・サービス起動・配備・pushは行っていない。
先行する変更は保護し、レビュー開始時の変更124パスのhashを一時記録へ保存した。

## 確認範囲

| 領域 | 確認した対象・結果 |
|---|---|
| 公式通信・認証 | `adapters/lineworks/client.py`・`config.py`。JWT RS256、scope、token期限、201受理、upload、固定HTTPS、容量、proxy/redirect禁止、429、未知結果の無再送。追加修正なし |
| Callback | `server.py`・`__main__.py`。署名・Bot・ドメイン・部屋・許可ユーザー・時刻・JSON/HTTP境界、durable queue、期限・停止。修正して回帰検証 |
| 表示・配送 | `cards.py`・`delivery.py`、`hermes_plugin/mcs_delivery/`、`mcs/notify/notify_cards.py`・`notify_transport.py`・`notify_cmds.py`・`notify_render.py`。中立spec/grant/receipt、本文・表示末尾・追加ボタン・添付・停止/復旧/scope/epoch。既存回帰で確認 |
| 閲覧・人承認 | `actions.py`・`mcs/ops/mcs_requests.py` と共通Registry/envelopes/text/projects。新しい閲覧、冪等発行、DM入力、理由・本人確定・receipt、scope再確認。修正して回帰検証 |
| テキストoutbox | `notify_flush.py`。独立CLI、stdin、進捗・unknown、sealed attachmentとledger SHA。送信前のpin喪失を修正 |
| Slack/Discord互換 | `adapters/slack/`・`adapters/discord/`、旧`hermes_plugin/mcs_*`入口、`card_workers.py`、共通配送。公式Hermes接続を維持。固定版Hermes/実SDKを隔離再検証 |
| 導入・更新 | `service.py`・CLI入口、`mcs_setup.py`・`mcs_update.py`・既存recoveryの`plugin_changed`利用、`install.sh`・CI・導入文書。runtime/source分離と移動後の更新検知を修正 |
| 検証・文書 | 追加回帰、標準runner、lint・CIゲート・README生成・release記録・Slack画像の整合を確認 |

親が配送・添付と領域間契約を確認し、3担当が通信/Callback、配送/操作、
導入/既存接続を独立にレビューした。その後に担当を限定して合成回帰を追加した。
反復する全リポジトリ監査は行っていない。収集・抽出・意味解析等の既存差分は
今回の静的レビュー対象へ無関係に拡張せず、標準の全体回帰で検証する。

公式一次資料は [JWT](https://developers.worksmobile.com/jp/docs/auth-jwt)、
[Bot API](https://developers.worksmobile.com/jp/docs/bot-api)、
[Callback](https://developers.worksmobile.com/jp/docs/bot-callback)、
[Upload](https://developers.worksmobile.com/jp/docs/file-upload)、
[Rate limits](https://developers.worksmobile.com/jp/docs/rate-limits) を照合した。
systemdのパス処理は [公式ソース](https://github.com/systemd/systemd/blob/main/src/core/load-fragment.c)
も参照した。公式uploadの`Filedata`/`FileData`表記差は未解消の制約として維持する。

コード探索は、先行する[安定性調査](stability-20261001.md)の安全なソースコピーに
対するjg結果を再利用し、今回の既知パス・シンボル・callerは直接読取りとrgで照合した。
今回jgの追加外部送信は行っていない。機密・患者情報・data・mcs-exportsは対象外。

## 再現した問題と修正

1. **設定保存先とcheckoutの不一致**。CLIの既定保存先を本体と同じ`~/.mcs`へ揃え、
   診断にHOMEを渡す。サービスはソースcheckoutのentryとruntimeのdataを分離する。
2. **移動後のnative adapter更新の見落とし**。Slack/Discordのsourceを旧worker警告、
   更新影響、永続化する`plugin_changed`へ含める。LINE更新は独立プロセスの再起動を案内する。
3. **Callback開始renameの非永続化**。directory fsync後だけイベントを操作へ渡す。
   fsync失敗は処理開始せず、workingを保持する。
4. **無効化後に旧Callback serverが存続**。設定invalidを停止条件とし、server・lockを解放する。
5. **期限切れの未処理入力を先に実行**。pendingを取る前に20分超の本文を削除してunknown化する。
6. **再閲覧で古いresultを先取り**。新しい閲覧・検索・一覧は共通UUID4を使い、runnerの新結果を待つ。
7. **ローカルpublish失敗後の再クリック不能**。処理中の明示再クリックだけ同一request IDで冪等発行する。
8. **カード更新・publish中断で本人の結果追跡が欠落**。人承認followupを発行前に永続化し、
   現在の本人・scope・epoch・projectで結果を確認する。旧button token失効で既に実施したreceiptを捨てない。
9. **添付の保管時pinが送信時に失われる**。LINE text経路でもledger SHAを保持し、
   封印前後のファイル置換・未保管ファイルを子プロセス起動前に拒否する。

無人での再送や人承認の省略は追加していない。既存の認証ファイルを`init`が保持する
動作に合わせ、診断文と導入手順に権限修正・保護した退避・再入力を具体化した。
`project_ids`は既存Slack/Discordと共通の操作権限であり、自動通知の対象は本体の
収集・通知設定に従うことも手順で明示した。今回新しい通知フィルタは追加していない。

## 検証

- 新しい操作・Callbackと既存LINE adapterの集約: **63 passed**。
- 添付pin・既存text outboxの集約: **47 passed**。
- setup/update/LINE診断: **286 passed**。サービス: **11 passed**。
  最初の集約の1失敗は追加fixtureの`mkdir(parents=True)`不足で、修正後にサービス全件を再実行した。
- 修正後の標準全体回帰（`scripts/run_tests.sh`、tests/ + integration/）:
  **3,472 passed、1 skipped、19 subtests passed、223.14秒、exit 0**。
- CIと同じ固定版Hermes・実Discord/Slack SDKの4ファイル:
  **14 passed、0 failed、skipなし、2.4秒、exit 0**。
  一時sourceとHOMEで隔離し、既存venvは読取り専用で使用。
  Python 3.11.15、Hermes `fd50a275e2616118c48fe07e7e1c878782b15ccd`、
  discord.py 2.7.1、slack-sdk 3.44.1、slack-bolt 1.30.0。
  詳細な隔離は [SDK記録](hermes-sdk-20261001.md) と同じ。
- `make lint`、`ci/gates.py`（8/8）、`ci/mine_gates.py --check`、
  README生成check・release記録check・README release整合・合成Slack画像7点の整合・
  `git diff --check`: 成功。

全体のskipは実Discord SDK必須のテストで、別の固定版SDK検証で実行した。
通常環境でのHermes-bound integration収集除外も固定版検証で補った。
CIのPython 3.13でGitHub Actionsを実行したという意味ではない。

最終差分はレビュー開始時から既存16パスの必要箇所と新規4パスだけを変更した。
残る先行変更108パスはhash一致し、先行ファイルの削除はない。
HEADは開始時と同じ `830496a0e5d6b33c28276c74311aaa93825590e8`。
コード・テストの最終変更後に全体回帰を実行し、その後は記録と手順の説明だけを整えた。

一時ログは `/tmp/mcs-lineworks-review-{full,sdk,lint,flush,actions-runtime}-20261001.log`、
導入担当は `/tmp/mcs-lineworks-review-install-regression-20261001.log` と
`/tmp/mcs-lineworks-review-service-final-20261001.log`。
始点manifestは `/tmp/mcs-lineworks-review-start-20261001.json`。
恒久的な主要結果は本書へ記録する。

## 制約と適用

実LINE WORKSの認証・管理者制限・upload受理・公開TLS Callbackは未検証。
実配備時は承認された完全合成テナントで接続確認を行う。
Linuxのネイティブsystemd解析と実サービス稼働も未検証。
バグが一切ないことをテスト結果から保証するものではない。

更新時は [導入手順](../guides/LINEWORKS.md)に従い、LINE独立プロセスのcheckと再起動を行う。
Slack/Discord変更は対象Hermes gatewayへ再起動で反映する。
実サービスを今回再起動したという意味ではない。
共有記憶は先行作業でTransport closedとなったため、保存成功を主張せずローカル記録を保持する。
