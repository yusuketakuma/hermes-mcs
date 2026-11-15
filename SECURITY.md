# Security

## データ取扱い

このソフトウェアは医療・介護記録を扱う。以下をリポジトリに入れてはいけない:

- 患者・利用者の氏名・本文・添付ファイル（`data/`、`chrome-profile/`）
- 認証情報・トークン（`.env`、`config.json`、`token_cache.json`、
  Keychain 登録内容、`DISCORD_BOT_TOKEN`・`SLACK_BOT_TOKEN`・`SLACK_APP_TOKEN`、
  `TYPESAFE_API_KEY`）
- `mcs_view` の閲覧出力（本文・投稿者を含む）— 共有ログ・外部LLM・
  公開リポジトリへの転送禁止

`.gitignore` と CI の `hygiene` ジョブで追跡混入を検査しているが、
運用者の確認が最終防衛線。

## 設計上の安全境界

- 収集は GET 中心。患者単位の既読化は fetch_state=complete + ledger commit 済み +
  snapshot timestamp 必須の三重ゲート。返信の自動既読化だけは、明示された運用条件として
  収集時点に限定するsnapshot条件を使わない。`--mark-read`付き定期収集で、直近7日以内に
  保存した返信のスレッドを1回最大10件確認し、未保存・抜粋のみの返信を先に補完する。
  保存済みの抜粋が取得の再試行上限に達した場合は、補完を諦めて既読化を進める。
  未保存の返信が残る場合は既読にしない。未読状態の再確認で成功を記録する。
  確認後に届いた返信も既読になる場合があり、読取り時に見つかった追加返信は取得ジョブへ
  登録する。失敗・確認不能は未確認のまま再試行する
- API はリダイレクト拒否・proxy 無効。Bearer は許可 origin 以外へ送らない
  - 限定例外は添付ファイルの取得（GET）だけ。取得元は `https://www.medical-care.net`（443）に限る。
    同じ host 内の redirect と、署名付き URL を返す `files.medical-care.net` への redirect（1回）だけを追い、
    後者は Authorization を付けない新しい要求で取得する。許可外の redirect 先は拒否し、
    proxy は常に無効（`mcs/ingest/mcs_download.py`）
- MCSスタンプの追加・変更・取消は、操作者1人とMCSアカウントを明示した場合だけ既定で有効（`stamps.enabled=false`で無効）。操作できるのはその操作者だけで、カードのスタンプ列の1回押し（理由は自動記録）またはフォームの確認画面を通して依頼する。
  保存済みの承認・投稿の版・患者範囲を照合し、同じセッションの本人IDを再取得してから送る。
  送信前に私有領域`data/stamp_operations/`へ記録を同期し、POSTは1回だけ試みる。
  結果不明はGETで照合し、自動再送しない。この記録を伴わない別配置へのDB復元では、古い操作を実行しない。
  スタンプは既読化や業務タスクの完了とは別に扱う。詳細は[スタンプ管理契約](docs/specs/stamp-management.md)を参照。
- 自動ログインが使うChromeのCDP（127.0.0.1の待受）は、同じMacのすべてのOSアカウントから到達できる。
  そのため、ログイン可能な別のアカウントがあるMacでは、自動ログインはCDP付きChromeを起動せず、
  パスワードも入力せずに`manual_required:multi_user_host`で止まる。単一ユーザーのMacで運用する
- ローカルLLM の推論経路は loopback 固定・proxy 無効
- Hermes モード（`runtime_mode` 未指定/`hermes`）では、Slack/Discord の対話カード
  addon は Hermes が接続済みの client と interaction だけを使う。Discord SDK は必要な関数内で遅延 import し、独自 Bot・token 取得・
  REST 接続は行わない。`asyncio` の許可は待機・ローカル I/O の thread 移譲・
  cancellation に限定する
- `runtime_mode=standalone` では `adapters/{slack,discord}/standalone.py` が
  `deployment/requirements-standalone.txt` の固定版公式 SDK（`~/.mcs/venv`）で
  Slack/Discord に接続する。トークンはroot配下の私有JSON（優先）または `.env`（0600）からのみ読み、
  SDK は関数内で import、起動時に proxy 環境変数を除去、メンションは無効化
  （Slackの独立CLI本文はプレーンテキスト）、テキスト送信先は `notify_target` /
  `notify_system_target` のチャンネルに限定する。成否不明の送信は再送しない
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
- MCSの私有 `.env` にキーがありmode未指定なら、Jevはassist、選択したAnthropicまたはOpenAIはonで自動使用する。既定の優先順はAnthropic、OpenAIとする。
  本文・患者文脈には患者名等が含まれ得る。匿名化はしない。明示OFF・対象・低い予算は維持する。
  キーの登録前に送信先・対象と契約を確認する（[設定と保持条件](docs/guides/ANTHROPIC.md)）
- 公開医薬品情報の照会はローカルTinyFishゲートウェイを使い、そのトークンファイルがない場合だけ
  無料・認証不要のExa MCP（`https://mcp.exa.ai/mcp`）へ接続する。同梱公開マスターと一致した薬品名、
  一般的な疾患のキーワード、公式URLだけを送り、患者情報・本文・私有辞書は送らない。
  不正なトークン・認証拒否・上限・通信障害ではExaへ切り替えない。通信の上限・期限と
  no-proxy/no-redirectを維持し、取得資料は医学的な正確性を評価していない参考情報として扱う
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

日次 SQLite backup は同じ端末の平文保存であり、端末喪失の対策ではない。
暗号化 offsite と新規私有配置への復元 CLI は実装されているが、
保存先・鍵 escrow・独立 SHA receipt・保持・平文作業領域の方針と実機受入が必要。
手順と同意待ちの停止点は [バックアップ・復元ガイド](docs/guides/BACKUP.md)を参照。
患者 rollup は暫定集約、添付内容は未解析で、要約の入力上限を超える
記録は処理を停止して要確認とする。解析履歴の PASS 件数を、全保存記録の
現行品質や確定した臨床判断として扱わない。

### 機能・運用上の制約（詳細）

README「現在の機能・運用上の制約」から移設。

- 患者一覧（rollup）は取得済み投稿から作る暫定集約です。未取得・未抽出・
  訂正前の記録があり得るため、確定した処方一覧や依頼台帳の代わりにはなりません。
- 添付は保存・通知とメタ情報の管理までで、画像・PDF等の内容は意味解析していません。
- 長文の構造化抽出は分割処理しますが、要約は本文と文脈を含む入力上限を超えると
  生成を止めて `input_oversize`／`NEEDS_REVIEW` とします。全件の要約完了は保証しません。
- 日次 SQLite backup と暗号化 offsite は別です。offsite は明示した媒体と
  私有ディレクトリ、固定 OS OpenSSL、encrypt-then-MAC、別途信頼した SHA receipt を
  要求します。鍵は専用 Keychain または正確に32バイトの明示 FD からだけ取得し、
  `.env` フォールバックはありません。鍵・receipt・媒体の独立した保管と
  人の escrow-key drill を実証しない限り、端末喪失後の回復を保証しません。
  端末・OS・アカウントの侵害や未保存情報を暗号化だけで解決するものではありません。
- offsite の復元は既存原本を上書きせず、新規ディレクトリに同意待ち hold を先に
  保存します。配置成功はサービス起動・通知再開の許可ではありません。
  添付の行・保存済み hash は保持しますが、添付実体・資格情報・配送 journal は
  bundle に含みません。保持世代の削除と平文 scratch・訓練 DB の清掃は
  オーナーの承認範囲で行い、unlink を安全消去とみなしません。
- `semantic --status` と `semantic_observe.py` の既存 `audit_statuses` 等は
  再試行・旧世代を含む `history` です。`current_quality` は現在の本文・文脈・
  policy に一致する要約と監査の組を、設定対象の保存済みメッセージ数で評価します。
  `NEEDS_REVIEW` は監査完了に含みますが PASS には含めず、無効時や設定未指定時は
  現行品質を算出しません。これは医学的な正確性の保証ではありません。

## 人工知能（AI）の使用箇所と情報の行き先

README「仕組み」「人工知能（AI）の使用箇所と情報の行き先」から移設。

収集した記録はローカルに保存します。通知を設定した場合は本文・要約と送信対象の添付を
Discord等の設定先へ送ります。Jev・Anthropic・OpenAIは、このMCSの `.env` のキーと設定に応じて
本文・患者文脈を外部へ送ります。「ローカルLLM」はシステム全体の外部送信禁止を意味しません。

患者の記録をどこへ送るかは重要なので、使うAIと送付先を明示します。

- **文章の整理**（要約・薬名や症状の拾い上げ）— このマシンの中だけで
  動くローカルAI（Qwen3.5-9B）が基本経路です。AnthropicまたはOpenAIが有効な場合、患者文脈の更新と旧版投稿の再抽出は外部APIへ送ります。失敗時のローカル処理も従来の受付・期限条件に従います
- **整理結果の意味チェック** — TypeSafe Jev APIへ確認用の設問と本文を送ります。
  `TYPESAFE_API_KEY` がありmode未指定なら `assist`、キーがなければ `off` です。
  自動有効化時の上限は再試行を含め日本時間の1日10000要求です。明示した `off` や低い予算は維持し、
  `enforce` や公開判定の評価・承認は省略しません
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
[推論経路と処理結果の所有者](docs/development/LLM_RUNTIME.md)を参照。

## 報告

脆弱性・秘密情報の混入・データ取扱いの問題を見つけた場合は、
公開の Issue ではなくリポジトリ管理者へ直接連絡すること
（患者情報を含む可能性があるため公開報告は避ける）。
