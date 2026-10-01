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
- Slack/Discord の対話カード addon は Hermes が接続済みの client と interaction
  だけを使う。Discord SDK は必要な関数内で遅延 import し、独自 Bot・token 取得・
  REST 接続は行わない。`asyncio` の許可は待機・ローカル I/O の thread 移譲・
  cancellation に限定する
- LINE WORKS は `adapters/lineworks/` の独立接続だけが Bot REST・JWT 認証を所有する。
  通信先は固定の公式 URL、proxy・redirect・送信の自動再試行は無効。
  秘密値は明示した権限制限付きファイルから読み、環境から自動取得しない。
  Callback は公開 HTTPS の reverse proxy から loopback へ転送し、署名・Bot・
  ドメイン・部屋・許可ユーザー・時刻を検証してから保管する。
  成否不明の送信・操作は自動再実行しない。起動・停止用の asyncio API は
  この独立プロセスの入口に限定し、Slack/Discord の権限を増やさない
- 通知を設定した場合、本文・要約・送信対象の添付は Discord 等の設定先へ送る。
  対話カードを有効にすると、タスクの期限リマインドも `notify_target` へ
  患者名・タスク内容・担当者名を含むテキストで送る。
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

### 機能・運用上の制約（詳細）

README「現在の機能・運用上の制約」から移設。

- 患者一覧（rollup）は取得済み投稿から作る暫定集約です。未取得・未抽出・
  訂正前の記録があり得るため、確定した処方一覧や依頼台帳の代わりにはなりません。
- 添付は保存・通知とメタ情報の管理までで、画像・PDF等の内容は意味解析していません。
- 長文の構造化抽出は分割処理しますが、要約は本文と文脈を含む入力上限を超えると
  生成を止めて `input_oversize`／`NEEDS_REVIEW` とします。全件の要約完了は保証しません。
- 日次 SQLite backup は同じマシン上の保存です。別媒体への退避と復元手順の実証は
  この実装では保証しておらず、端末故障時の復旧保証にはなりません。
- `semantic --status` と `semantic_observe.py` の既存 `audit_statuses` 等は
  再試行・旧世代を含む `history` です。`current_quality` は現在の本文・文脈・
  policy に一致する要約と監査の組を、設定対象の保存済みメッセージ数で評価します。
  `NEEDS_REVIEW` は監査完了に含みますが PASS には含めず、無効時や設定未指定時は
  現行品質を算出しません。これは医学的な正確性の保証ではありません。

## 人工知能（AI）の使用箇所と情報の行き先

README「仕組み」「人工知能（AI）の使用箇所と情報の行き先」から移設。

収集した記録の保存と構造化抽出はローカルで行います。通知を設定した
場合は本文・要約と送信対象の添付を Discord 等の設定先へ送ります。
任意の意味チェック・抽出監査を有効化した場合は、本文と必要なスレッド
文脈を TypeSafe Jev API へ送ります。「ローカルLLM」はシステム全体の
外部送信禁止を意味しません。

患者の記録をどこへ送るかは重要なので、使うAIと送付先を明示します。

- **文章の整理**（要約・薬名や症状の拾い上げ）— このマシンの中だけで
  動くローカルAI（Qwen3.5-9B）を使い、この推論経路では外部へ送りません
- **整理結果の意味チェック（任意・既定は OFF）** — 設定で
  `semantic.mode` を `shadow`/`assist`/`enforce` にした場合のみ、TypeSafe
  Jev API（外部サービス）に確認用の設問と本文を送ります
- **抽出結果の監査（任意・既定は OFF）** — `semantic.extract_qc` を
  `"annotate"` に設定した場合のみ、抽出済み項目が本文に裏付け
  られているかを Jev が確認し、結果へ注記として記録します
  （監査は注記を保存し、裏付け不足等があれば後続のローカル再抽出を1回行います）。対象は投稿日時が直近60日以内の
  記録です。60日超・日時不明の記録は対象外として区別し、過去の監査結果は
  引き続き閲覧できます。
- **知識ストア向け出力** — `brain_export.py` は snapshot から患者名・病名・
  要約・薬剤等の PHI を含む Markdown をローカルに書き出します。匿名化はしません。
  出力後の知識ストアへの同期や LLM への入力は別経路で、その送信先・権限は
  同期先の運用と設定で管理する必要があります。
  機械向け `export.jsonl` は許可した集計項目・ID・状態へ限定し、省いた内容は
  `content_omitted` で示します。外部配送の契約と認可条件は
  [外部エクスポート仕様](docs/specs/external-export-contract.md)を参照してください。

ローカルLLM の稼働構成・TypeSafe Jev の契約と再試行などの技術詳細は
[docs/development/DEVELOPMENT.md](docs/development/DEVELOPMENT.md) の「付録B. AI・推論エンジンの技術詳細」を参照。

## 報告

脆弱性・秘密情報の混入・データ取扱いの問題を見つけた場合は、
公開の Issue ではなくリポジトリ管理者へ直接連絡すること
（患者情報を含む可能性があるため公開報告は避ける）。
