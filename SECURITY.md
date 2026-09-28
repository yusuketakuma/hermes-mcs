# Security

## データ取扱い

このソフトウェアは医療・介護記録を扱う。以下をリポジトリに入れてはいけない:

- 患者・利用者の氏名・本文・添付ファイル（`data/`、`chrome-profile/`）
- 認証情報・トークン（`.env`、`config.json`、`token_cache.json`、
  Keychain 登録内容、`DISCORD_BOT_TOKEN`、`TYPESAFE_API_KEY`）
- `mcs_view` の閲覧出力（本文・投稿者を含む）— 共有ログ・外部LLM・
  公開リポジトリへの転送禁止

`.gitignore` と CI の `hygiene` ジョブで追跡混入を検査しているが、
運用者の確認が最終防衛線。

## 設計上の安全境界

- 収集は GET 中心、既読化は fetch_state=complete + ledger commit 済み +
  snapshot timestamp 必須の三重ゲート
- API はリダイレクト拒否・proxy 無効。Bearer は許可 origin 以外へ送らない
- ローカルLLM の推論経路は loopback 固定・proxy 無効
- 対話カード addon は Hermes が接続済みの Discord client と interaction
  だけを使う。SDK は必要な関数内で遅延 import し、独自 Bot・token 取得・
  REST 接続は行わない。`asyncio` の許可は待機・ローカル I/O の thread 移譲・
  cancellation に限定し、process 起動や別の network transport は許可しない
- 通知を設定した場合、本文・要約・送信対象の添付は Discord 等の設定先へ送る。
  閲覧用 snapshot と `mcs_view` 出力にも PHI が含まれ得るため、閲覧権限と転送先を管理する
- Jev 連携は `semantic.mode` 等の明示設定に従い、本文・必要なスレッド文脈を
  外部 API へ送る。本文を DATA 扱いにしても匿名化されるわけではない
- `brain_export.py` は患者名・病名・要約・薬剤等の PHI を含む Markdown を
  ローカル出力する。出力後の知識ストア同期・LLM 利用は別のデータ経路であり、
  エクスポート実行だけではそれらの外部送信を許可したことにならない
- 人承認操作（依頼登録・更新、シグナル却下、閾値変更、更新適用・ロールバック・
  DB 復元同意）は `--confirm-human` + receipt 記録が必須で、依頼・却下・閾値変更は
  `reason` も必須（core の validator が強制）
- 自己更新の信頼境界は GitHub/TLS と approval receipt。タグ署名は
  検証しないため `update.mode=auto` は「GitHub リポジトリへの
  push 権限 = このマシンでのコード実行」を意味する — auto は
  リモート管理を完全に信頼できる場合のみ有効化すること

## 復旧と解析の限界

日次 SQLite backup はローカル保存であり、別媒体の複製・端末喪失後の復元は
未保証。患者 rollup は暫定集約、添付内容は未解析で、要約の入力上限を超える
記録は処理を停止して要確認とする。解析履歴の PASS 件数を、全保存記録の
現行品質や確定した臨床判断として扱わない。

## 報告

脆弱性・秘密情報の混入・データ取扱いの問題を見つけた場合は、
公開の Issue ではなくリポジトリ管理者へ直接連絡すること
（患者情報を含む可能性があるため公開報告は避ける）。
