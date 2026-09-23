# Changelog

## [1.0.2] — 2026-09-23

全体レビュー（46ファイル・実測）に基づく安全性・収集完全性・抽出
正確性の大規模修復と、canonical semantic-facts v2・通知経路の
一本化・抽出並列化・運用導線の追加。既存の運用操作・設定はそのまま
使える。通知の宛先設定だけ `discord_channel_id` から `notify_target`
へ移行している（済みの環境は変更不要）。

### 動作が変わるもの

- **通知の配送経路を `hermes send` に一本化** — Discord API への
  独自実装（urllib・multipart・429 再送）を全廃し、Hermes の
  送信コマンドに委譲。config は `notify_target`（例
  `"discord:1550…"`）を参照し、`"slack:#mcs"` への変更だけで
  配送先を切り替えられる。outbox・receipt・分割再開などの配送
  保証機構は維持。拡張子のない添付には原本名の拡張子を持つ
  hardlink 別名を付けて送る（Discord で画像として表示されるため）
- **レビュー候補シグナルの通知が具体的になった** — 患者名と
  根拠となった最新の投稿の抜粋、末尾に `/mcs timeline` で確認する
  導線を表示。患者行が欠損・投稿が削除されている場合は従来の
  基本文で送る。通知の保存データは従来通り ID と固定文のみで、
  本文は送信時に台帳から解決する
- **抽出結果の QC 監査の対象が「直近60日以内の投稿」に拡大** —
  従来の3日窓から変更。窓を超えた未処理の監査ジョブは遅れて
  実行せず完了扱いで回収し、対象外の記録の過去の監査結果は
  そのまま閲覧できる。`mcs_view qc` の集計が対象内件数
  （eligible）・未実施（pending）・対象外（out_of_scope）を
  分けて報告する
- **ローカルLLMのスロット割当を再編** — llama.cpp は `-np 2` の
  2スロット構成のままだが、論理名を「slot 1 = 背景 / slot 2 =
  リアルタイム」に整理し、wire `id_slot` は 0-based（背景が `0`、
  RT が `1`）。**v1.0.1 と wire の割当が入れ替わっている**
  （旧: id_slot 0=RT・1=背景）。抽出などの背景処理は既定で
  slot 1 に固定し、対話系のために slot 2 を空ける。例外は
  `extract_llm --lend-rt`（全呼出し前に `/slots` を照会し、RT
  スロットが空いている時だけ借用）と `MCS_LLM_SLOT=<N>`（プロセス
  単位のオーバーライド。夜間 semantic drain が slot 2 を使う）。
  形式検出 probe も同じ決定点を通るため、オーバーライドや
  `--slot`/`--lend-rt` が probe 通信にも正しく効く
- **Keychain がロック中でも収集を継続できる** — `auto_login` が
  Keychain 読取に失敗した場合、`~/.mcs/.env` の `MCS_PASSWORD` に
  フォールバックする（`mcs_setup init` が Keychain と併記する。
  平文のため FileVault・物理セキュリティ前提）。再ログイン結果は
  run status と通知の detail に出る
- **`extract_llm` が並列実行できる** — `run_pending` の LLM 呼出しを
  ワーカー並列化（`--all --workers N`、既定はサーバスロット数）。
  DB 読み書きは呼出しスレッド専有で直列化し、形式検出は fan-out 前に
  1回だけ行う。実測で約100件/時→約294件/時
- **夜間の semantic/QC 一括処理** — `semantic_drain --drain` と
  launchd 常駐 drainer（`ai.mcs.extract-drainer[-rt]`、shard 分割）を
  追加。`deployment/scripts/` に定期実行スクリプトの正本を集約

### 修正した問題（安全性・収集完全性）

- **通知テキストが配送コマンドの制御構文として解釈されないよう
  無害化** — 投稿本文や抽出結果に紛れた `MEDIA:` 等のディレクティブ
  形式が `hermes send` のファイル添付機能を起動しないよう、通知
  組立ての入力境界で無効化
- **削除・編集・添付撤回が現行ビューに反映される** — MCS 側で
  消えた投稿・更新された本文・取り下げられた添付が、保存済み
  記録の現行状態として正しく扱われる
- **履歴収集がページ上限で止まらない** — 上限に達した取得は永続
  cursor と継続ジョブで残りを引き継ぐ。途中失敗しても既取得分を
  保持したまま再開できる
- **返信スニペットの親投稿が既読化されない** — 一覧の抜粋表示だけで
  本文未取得の投稿が既読対象に混入しない
- **履歴ジョブの沈黙ループを解消** — walk 途中のエラーが attempt を
  消費せず 300 秒間隔で無限に再試行し続けた経路を、通常の再試行
  会計に統一（8回で可視な失敗へ）。セッション切れは従来通り
  attempt を消費しない
- **WebSocket プロトコル検証** — CDP 接続を websockets 依存から
  標準ライブラリの RFC 6455 実装に置き換え、マスクされたサーバ
  フレーム等のプロトコル違反を拒否
- **ワーカー分離と絶対期限** — `mcs_transport` で API 取得・添付・
  Chrome 入出力を子プロセスに隔離し、絶対時刻の期限を伝播。
  期限超過は kill+回収し、通信は loopback と許可 origin に限定
- **`init_data` の `--deadline` が実際に効く** — 列挙開始前に
  アダプタへ期限を束縛するよう修正（従来は指定が伝播しなかった）

### 修正した問題（抽出・意味処理の正確性）

- **canonical projection が読み取り経路を素通りしない** — 無効化・
  形式不正・期限切れの projection が旧来の抽出結果を覆い隠したり、
  逆に新しい壊れた projection が有効な旧 projection を隠したり
  しないよう、「現在使用可能な projection」の判定を
  `current_projection_pred`/`current_projection_id` に単一ソース化。
  統計・シグナル・QC・rollup・drain・依頼候補の全読取経路が同じ
  判定を使う。projection 判定は JSON の object 型・エラーフラグ・
  本文 hash 一致・非無効化・同一 project を全て確認する
- **canonical 有効化の前提欠陥（C01–C07）を修復** — projection 境界の
  message_id 型正規化、target 別の v2_doc 隔離、coverage 不完全時の
  有界再試行、評価済み監査のみ再利用、`finish_reason=length` の
  中途生成の拒否、現行版 projection の一意化と rollup 連動、
  care_event の型付きゲート
- **QC 監査と抽出の世代ずれを防止** — QC の realtime 窓判定を
  source_artifact_id への世代束縛に変更し、監査実行時に抽出が
  置き換わっていた場合のずれを排除
- **抽出の証跡（evidence）を本文・出所に束縛** — semantic-facts の
  evidence が参照元 message・revision と一致し、atom/body の
  範囲内に収まることを検証。本文に無い根拠や出所の違う根拠は
  採用しない
- **同じ文面でも内容が違えば「重複」にしない** — 事実の関係判定で
  EXACT_DUPLICATE は臨床属性・時間属性が完全に一致する場合のみ。
  文面が同じでも属性が異なるものは UNRESOLVED として残し、
  mandatory render でも fact_id 単位で保持（同文面の別事実は
  主語・時刻・ID 注記つきで両方表示）
- **空白だけの薬名・症状を捨てる** — 抽出結果の検証で空白のみの
  `meds.name`/`symptoms.text` を棄却し、棄却数を `_items_dropped`
  に計上
- **抽出ベンチの採点に依頼（requests）を追加** — 期待・禁止の
  フィールド適合率・再現率に requests が含まれ、精度退行を
  検出できる
- **semantic_shadow_e2e を単独実行できる** — `_mcs_path`
  ブートストラップを追加し、リポジトリ外からの実行でも flat
  import が解決される

### 新しい運用機能

- **`mcs_view qc`** — extract_qc の集計（評価済み/未評価/判定別/
  urgency 不一致/未処理）と要注意メッセージ一覧、`--message-id` で
  項目別判定の全履歴を表示。判定値の日本語凡例つき（NO_MATCH は
  「事実が存在しない」ではなく「本文に裏付けがない」の意味）。
  本文が変わって古くなった QC 行は集計・一覧から除外
- **`brain_export`** — 公開 snapshot から患者 rollup 由来の要約を
  外部知識ストア向け Markdown として生成（read-only・atomic・冪等。
  message 本文は含めないが PHI を含むため同期先の権限管理は運用側）
- **`semantic-facts v2`** — canonical 抽出・義務レンダリング・
  semantic QC・v2→legacy projection・coverage 監査。`--shard`/
  `--slot`/`--lend-rt`・常駐 poll・oldest_first 対応
- **`semantic_metrics` / `semantic_observe`** — 履歴と現行世代の品質を
  分離した集計と、read-only snapshot を使う日次確認コマンド
- **install.sh が hermes-agent を自動導入** — 未導入なら pin 済み
  commit を clone・venv 構築・plugin リンクまで実施。pin 不一致は
  警告のみ。`keychain_to_env.py` で Keychain→.env の同期も可能

### 内部構造・開発者向け

- **`mcs/` を第一層サブディレクトリに再編**（44ファイル） —
  `core/`（ledger・local_llm・maintenance・init_data・mcs_util）・
  `ingest/`（adapter・transport・job_ops・notifier・run_check）・
  `extract/`・`semantic/`・`ops/`。`import ledger` 等の flat import は
  `_mcs_path` ブートストラップで維持
- **CI ゲート強化** — `ci/gates.py` に失敗履歴由来の静的ゲート8本
  （stdlib 限定・platform 直 API 禁止・plugin sandbox・snapshot
  read-only 契約・writer flock・fail-closed 通知・install pin・
  records 健全性）。`ci/mine_gates.py` が dev-record の FIX-/AUDIT- ID
  とゲートのカバレッジを照合
- **依存は標準ライブラリのみを維持** — websockets 依存を除去。
  テストは全て一時 DB + 合成 fixture（実 MCS・Discord・Keychain・
  ローカルLLM・Jev へアクセスしない）
- **検証** — pytest 886 件全通過・ruff clean・CI ゲート 8/8・
  mine_gates・README 生成物 drift なし

### Docs

- README を非エンジニア向けに全面改訂 — 平易な導入部・
  「MCS データで何が追えるのか」「蓄積・集計できるデータの全体像」
  「まだ取れないもの」の明示、図を SVG 化、用語を「チャットルーム」
  に統一、QC 対象範囲（直近60日）を明記
- `docs/semantic-facts-v2-rollout.md`・監査記録（AUDIT-J03・
  review-20260923）・launchagents の手順書を追加・更新

## [1.0.1] — 2026-09-22

構造化抽出（extract_llm）の精度向上と、統計の回帰確認ワークフロー。
既存の運用操作・設定はそのまま使え、移行作業は不要。

### 抽出結果が変わるもの

- **薬の抽出に「誰の・どの状態の薬か」が付く** — 各項目が
  患者本人の薬か・家族など他人の薬か（`subject`）、現在服用中か
  中止済みか計画中か（`status`）、否定言及か（`negated`）を区別する。
  その結果、通知・患者ロールアップ・薬関連統計で「家族の薬」や
  「中止した薬」が現在の薬として表示・集計されなくなる。
- **抽出の各項目に本文中の根拠箇所（`evidence`）が記録される** —
  通知や台帳の確認時に「どの記述から拾ったか」を照合できる。
  本文に無い根拠をでっち上げた項目は自動で捨てられる。
- **依頼の抽出に「誰からの依頼か」「期限」が付く**
- **3000字を超える長文も全文が抽出対象になる** — 以前は先頭しか
  見ていなかったため、後半に書かれた薬・症状・依頼も拾う。
- **スレッドの親投稿・直近返信を文脈として参照する** —
  「はい、大丈夫です」のような返信単体では意味が取れない投稿の
  抽出精度が上がる。

### 動作が変わるもの

- **ローカルLLMサーバの能力に応じて出力方式を自動選択** — JSON
  スキーマ強制に対応したサーバならそれを使い、非対応なら従来方式へ
  自動で降格・復帰する。設定変更は不要。
- **途中で失敗した抽出を成功扱いしない** — 長文の分割処理の一部が
  失敗した場合、そのメッセージ全体を失敗として再試行する
  （以前は中途半端な結果が「完了」として残り得た）。
- **v1 の旧抽出は v2 が成功した時点で自動的に置き換わる** —
  手動の移行操作は不要。抽出失敗の履歴は残る。

### 新しい運用機能

- **抽出結果の QC 検証（任意・既定 OFF）** — config に
  `semantic.extract_qc: "annotate"` を設定すると、抽出済み項目を
  Jev が裏付け確認し、判定を別 artifact に注記する。抽出結果自体は
  変更・抑制されない。日次予算・一時停止など既存のガードは全て
  そのまま効く。
- **抽出精度ベンチ** — `mcs/extract_bench.py` で合成ケース14件に
  対するフィールド別の適合率・再現率を計測できる（要ローカルLLM
  サーバ。オフライン検証は `--mock-ok`）。
- **統計の承認済み参照セット検証** — `mcs/mcs_refstats.py` で
  「人が確認して承認した統計結果」を基準として保存し、後日の
  snapshot に対して `verify` で再計算・突合できる。統計コードの
  変更が結果を変えていないか（regression）、データが変わったか
  （drift）を区別して報告する。承認は既存の `--confirm-human`
  コマンド経路のみ。

### Docs

- README に「MCS データで何が追えるのか」節を追加、図を mermaid
  から SVG に差し替え
- AGENTS.md を最小限の指示に整理

## [1.0.0] — 2026-09-21

`mcs-adapter` から `hermes-mcs` として独立リポジトリ化し、内部構造を
シンプル化した最初のリリース。

### Layout
- `adapter/` → `mcs/`（実行モジュール37本）、テスト49本を `tests/` へ分離
- `hermes_plugin/`（/mcs Discord コマンド）、`integration/`（hermes E2E）を維持
- `docs/dev-records/` に開発記録を集約、`deployment/launchagents/` に
  launchd plist テンプレート3種を追加

### Features (既存機能の集約)
- 15分間隔の MCS 未読収集 + Discord 通知（構造化→原文の2段投稿）
- 全履歴アーカイブ（ページカーソルで中断再開）
- FTS5 全文検索 + 日本語空白無視の部分一致、患者タイムライン
- 構造化抽出: ルール `extract_v1` + ローカルLLM `extract_llm`(Qwen3.5-9B)
- 患者ロールアップ、読み取り専用統計、レビュー候補シグナル6種
- 人承認の依頼管理（`--confirm-human`+`reason`+receipt）
- 人による却下（`signal_dismiss`）と承認済み閾値ポリシー（`signal_policy`）
- 通知クールダウン（既定7日）、deadline 部分実行の安全側 resolve 抑制

### Infrastructure
- `mcs/mcs_setup.py` — init（Keychain/config/.env プロビジョニング）+ check
- `scripts/update_readme.py` — README モジュール表の自動生成
- GitHub Actions: lint-test / readme-sync / hygiene
- `pyproject.toml` で ruff・pytest 設定を一元化、`Makefile` 追加
- `LICENSE`（proprietary）・`SECURITY.md`・`AGENTS.md` 新設
