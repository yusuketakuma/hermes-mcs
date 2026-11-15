# OpenAI・Anthropic API 利用の全体レビュー（2026-11-15）

読取りだけのレビューの記録。HEAD `068bf9d` 時点のコードを対象にした。実APIの呼出し・キーの使用はしていない。
Anthropic の公式資料（料金・effort・thinking・errors・versioning）は 2026-11-15 に確認した。OpenAI の公式資料はこの開発環境のネットワークから到達できず、OpenAI 側の事実はすべて外部確認待ちである。

健全と確認した点: 予約なしで送る経路はない。少なく計上する経路はない（不明な分は最大額に寄せる）。proxy は使わず、redirect は拒否し、TLS は既定の検証を使う。API キーは stdin でだけ渡り、ログやエラーに出ない。

## 必ず直す

1. **料金表の期限とリリース**（2026-11-15 追記: PR #69 で OpenAI の Luna・Sol も 2026-11-15 に再確認し、期限を 2026-12-15 に延ばした版をこのブランチへ取り込んだ。残る対応は 11-25 より前のリリースと配備）
   - 料金表の期限は次のとおり。
     - 稼働版の Haiku: 2026-11-25（JST）
     - repo の 11-15 版: 2026-12-15
     - OpenAI の Luna: 2026-11-28
     - OpenAI の Sol: 2026-11-30
   - 期限を過ぎると patient_context はローカルに切り替わる。日付の境目に当たると、全提供元の送信が人の再開操作まで止まる（下記の「直した方がよい 3」）。
   - OpenAI の料金を公式で再確認し、同じリリース候補に入れて、11-25 より前に配備する。
   - 該当: `mcs/llm/providers/anthropic_client.py:103-118`、`openai_client.py:113-132`、`cloud_usage_accounting.py:167-175`
2. **1試行120秒の上限と大きな出力上限が噛み合っていない**
   - `CloudClient` の試行上限は120秒以下で、送信は非ストリーミング。一方、profile の出力上限は 16384〜32768 で、思考の時間も含まれる。
   - 120秒を超えると「送信後のタイムアウト」になる。最大額で計上され、再送は禁止され、その単位は保留されたままになる。
   - 対応は、profile ごとの試行上限、標準ライブラリでのストリーミング、出力上限の引き下げのいずれか。新しい送信の土台（S2）で行う。先に実機の `cloud-llm log` で timeout の件数を確かめる。
   - 該当: `mcs/llm/transport/cloud_client.py:54-59`、`cloud_policy.py:388`、`anthropic_client.py:84-86`

## 直した方がよい

1. **256KiB を超える200応答で全体が止まる**
   - 原因: `bounded_http.MAX_RESPONSE_BYTES` を超えると `protocol_error`、usage 不明 → `pricing_unverified` となり、全体が人の resume まで止まる。
   - 対応: クラウドだけ、max_tokens から決めた上限で読む。上限を超えたらその単位だけを保留する。
2. **5xx/529 の再試行が実際には成立していない**（所有者の決定 2026-11-15: 待たずに再試行をやめ、ローカルの処理に回す）
   - 待機はするが、2回目の予約は `repeat_failed` で拒否される。
   - 対応はどちらかを選ぶ: 再試行をやめるか、usage の無い 5xx を「確実に拒否された」扱いに加えて本当に再試行するか。文書とテストも合わせる。
3. **料金期限が時刻だけで全体停止を起こす**
   - 送信前の確認と予約の間で日付が変わると、期限切れになる。
   - 対応: 予約の先頭で `pricing_valid` を見て、回路を動かさない拒否にする。
   - `cloud_cli` の resume・診断・予告は、既定モデルの期限だけでなく、profile が選ぶ全モデルの最も早い期限を見る。
4. **クラウドで構造化出力を使っていない**
   - JSON が壊れた有料回答は、`repeat_success` によってその単位を塞ぐ。
   - 対応: S2 の要求の形に、Anthropic は `output_config.format`、OpenAI は `text.format: json_schema` を入れる案（各モデルの対応は外部確認）。
5. **Haiku の旧契約（max_tokens 1600）で思考が上限を食う**
   - Haiku 5.5 は思考が既定で有効で、思考トークンは max_tokens に含まれる。
   - 対応: 新しい契約で思考を無効にするか、上限を見直す。
6. **提供元のエラーの誤分類**
   - Anthropic の支出上限の 400 を `contract_error` として扱っている。
   - Retry-After の無い月額上限の 429 を3回試行している。
   - 対応: Retry-After の無い 429 は再試行しない。支出上限の 400 は固定文言で照合し、照合できたものだけ `payment_required` にする。
7. **提供元名による分岐と定数が重複している**
   - 共通部にある提供元名の分岐: `cloud_policy.py` の要求の検証・profile 解決、`cloud_inference.py`、`extract_llm_provider.py`。
   - 重複している定数: 既定の max_tokens 1600、用途ごとの上限表、API キーの変数名。
   - 対応: S2 で `provider.wire_check` を作り、各 provider モジュールを正本に寄せる。

## あるとよい

- `anthropic-version` は `2023-06-01` のままで、最新は `2026-10-21`。本コードは影響を受けない。
- `CloudClient.complete` の `reserve_fn` を必須にし、予約なしの送信を型で防ぐ。
- 残り時間を超える Retry-After のときは、待たずに失敗させる。
- 拒否時の detail を提供元間で揃える。
- patient_context の `source_identity`（message_id・署名・content_hash）を送らない（S2 で行う）。
- 回路と cloud_control の行の model が、常に Haiku の名前で記録されている。
- API キーの形式（提供元ごとの接頭辞）を確かめていない。
- テストの本番 endpoint 遮断が、Popen を差し替えたときに効かない（`tests/conftest.py:226-237`）。

## 外部確認が必要

- OpenAI:
  - `gpt-6-luna`・`gpt-6.1-sol` の料金（長文の閾値と倍率を含む）
  - `prompt_cache_options` の引数名
  - usage の `cache_write_tokens` と `cache_write_tokens_details` の形
  - 応答の `model` と `service_tier` の値
  - 429 の quota 系コード
  - `store:false` のときの保持条件と ZDR
- Anthropic:
  - 応答の `model` の値
  - 思考の signature の最大サイズ
  - 構造化出力への対応
  - 429/5xx/529 が課金されるか
  - キャッシュされる最小の接頭辞長

## 文書とコードの食い違い

1. **5xx の再試行**: `docs/guides/ANTHROPIC.md` の「429・5xx の再試行は従来どおり」は、実装と合わない（上記の「直した方がよい 2」）。
2. **上限の引き下げ**: 「上限を引き下げた後の未送信の試行は予約額を計上したまま」とあるが、実際は not_sent として0円で精算される。該当: ANTHROPIC.md、`cloud_send_admission.tightened` の docstring。
3. **README の説明**: README は「個別投稿のクラウド再抽出は行わない」「意味解析はローカル」としている。実際は `*_backlog_until` の期間中、クラウドへ送る。
4. **キャッシュ**: ANTHROPIC.md はシステムプロンプトをキャッシュの接頭辞にするとしているが、実際に使うのは再抽出の経路だけ。
5. **料金期限の予告**: 実装は既定モデルの料金表だけを見ている。
6. **`provider_registry` の docstring**: 「名前で分岐しない」としているが、実装と食い違う。
7. **`bounded_http` の docstring**: 主な有料経路（Anthropic/OpenAI）を利用者として書いていない。
8. **医薬品情報の照会**: SECURITY.md は「TinyFish だけ」としているが、`drug_info.py` に呼出元のない Exa の経路が残っている。
9. **秘密値の一覧**: SECURITY.md の秘密値一覧に `MCS_ANTHROPIC_API_KEY`/`MCS_OPENAI_API_KEY` がない。
10. **設計書の行番号**: `CLOUD_SEND_FOUNDATION_DESIGN_20261115.md` の行番号がずれている。
11. **添付OCR**: 設計書は「添付OCRのため CloudInferenceUnit を残す」としているが、新規送信は到達せず、必要なのは回復の経路だけ。
