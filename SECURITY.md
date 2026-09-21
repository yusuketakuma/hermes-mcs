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
- ローカルLLM は loopback 固定・proxy 無効 — 患者記録は外部へ出ない
- Jev 連携は `semantic.mode` 明示有効時のみ。本文は DATA 扱い
- 人承認操作は `--confirm-human` + `reason` + receipt 記録が必須

## 報告

脆弱性・秘密情報の混入・データ取扱いの問題を見つけた場合は、
公開の Issue ではなくリポジトリ管理者へ直接連絡すること
（患者情報を含む可能性があるため公開報告は避ける）。
