# 1.0.10 リリース準備・Hermes/Slack稼働確認

2026-10-02、ユーザー指定の1.0.10を `repository-release` skill と
`docs/development/RELEASE_NOTES.md` に従って準備した。公開は対象外。

## 順序と復元地点

1. 独立稼働の不具合修正を `f5b3e8d`、配置整理を `22bb3fb` に分けてcommit。
2. Hermes gatewayのlaunchd管理とlive PID、5分周期の収集成功、Slackの通知キューを確認。
3. コード変更後の新着3件の配送receiptと部品別の完了状態を確認。
4. 指定版のCHANGELOG・README要約・READMEの5項目の見直し記録を生成・検証。

## 稼働・配送の確認

実データは読取り専用DBで確認し、患者本文・氏名・認証情報・通知先IDは出力しなかった。
現在の選択はHermesとSlack。gatewayはlaunchd配下で稼働し、収集も成功している。
Slackの待機中renderは0件。

関連する実行ソースの最終編集は16:27:36 JST。
その後の新着は16:37、17:17、17:52に配送され、いずれもcard・thread・body_partが
`delivered`、renderは`delivered / complete`、各部品にremote receiptがある。
16:37の通知にはattachment_partもあり、同じく配送済みreceiptを確認した。
新たなテスト通知や既存通知の再送はしていない。

healthの通知注意には旧Discordの配送不明2件が残っている。
現在のSlackへ再送・移し替えず保持した。Slackの新着停止を示す状態ではない。
今回、稼働モード変更・gateway再起動・サービス再登録・資格情報変更は行っていない。

## リリース準備の検証

- CHANGELOGは既存buildで1.0.10・2026-10-02を明示して生成。
- READMEの機能・画面例・導入・安全・導線を今回の変更へ照合し、根拠を更新。
- 未リリース記録5件をarchiveし、開始時とbytes一致を確認。
- Release本文とタイトルは1.0.10のCHANGELOGから既存exportで生成。
- release/metaの隔離検証: **213 passed、26 subtests passed**、79.92秒、exit 0。
- release記録、README生成・要約同期・リンク、安全ゲート8/8、diff検査は成功。
- 実行コードは直前の全体検証から変更なし。
  **3738 passed、26 subtests passed**、インストール済みHermes SDKの隔離検証14件成功を再利用。
  合成画像のSlack7組・LINE WORKS2組も変更なしで検査成功。
- 読取り専用のリモート確認でv1.0.10 tag・Releaseは存在しなかった。

push・tag・GitHub Release作成/公開・GitHub CIは実施していない。
公開へ進む場合は、この準備commitをpushしてexact SHAの必須CIを確認し、
tag workflowの下書き生成・本文一致を検証する。
実接続の確認は現在のHermes/Slackだけで、独立モード・Discord・LINE WORKSの実テナント検証とは区別する。
