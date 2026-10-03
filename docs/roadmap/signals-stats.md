# シグナル・統計の詳細計画（#12〜#14・#17〜#19）

[`docs/ROADMAP.md`](../ROADMAP.md) の #12 緊急度エスカレーション、#13 日次 digest、#14 変更エピソードの紐付け、#17 職種間やり取りの構造と応答時間、#18 業務負荷レポート、#19 シグナル精度のフィードバックの詳細計画。

2026-10-03照合: 以下の「現状」・行番号・実測・節番号は2026-09-29の調査記録。
現在の版割当・公開条件は[ROADMAP](../ROADMAP.md)を正とし、末尾の全体着手順は上書きしない。
#13の基本日次digestは`notify_digest.build_text/maybe_enqueue`と`run_check._deliver`に実装済み。
1.0.11は本人反応件数、1.0.12は#25の一覧を既存digestへ追加する。下記の新規digest・cron提案を重複実装しない。
F-2のルール緊急度問題は1.0.15で是正予定。#17は1.0.13、#14/#18/#19は未割当で未決判断を今回実装しない。

- 基準: v1.0.6 のコード（2026-09-29 調査）。行番号はこの時点のもの。
- 表記: 【実行確認】= 合成入力で実行して確認、【未検証】= 実データ・実機・実 API に触れないと分からない点。
- 規模の目安: S = 半日〜1 日、M = 2〜4 日、L = 1 週間以上（実装 + テスト + docs。運用での測定は別）。
- オーナー判断は項目内で `#N-Dk` と呼ぶ（一覧は ROADMAP の「オーナー判断一覧」）。

## 0. 結論

- 6 項目とも新テーブル・スキーマ変更なしで実装できる。使うのは `artifacts` / `notify_outbox` / 読み取り専用 stat の再利用。plugin の変更が要るのは #19 の任意拡張だけ。
- 旧 ROADMAP の実害ある誤り:
  1. **#12 の前提「否定 / 時制は v4 で対応済」は LLM レーンだけ**。ルール抽出は否定を見ない。【実行確認】「急ぎではありません」「緊急の対応は不要です」「明日すぐに連絡します」がすべて `urgency=high` になる。これが第一報の警告表示とシグナルの昇格に直結する（F-2）。加えて interactive カードは緊急度を一切描画しない。
  2. #13 の「open シグナル」を既定の digest tier のまま出すと、依頼・期限系の 3 型が入り、「期限・予定・依頼は含めない」と矛盾する。
  3. #17 の `text_candidates` を埋めると、export allowlist（`export_schema.py:85`）により brain_export が全体失敗する。
  4. #18 の `comm_concentration` は stat ではなく signal。`doc_burden` / `workload` はどの preset にも入らず、定期出力は皆無。
  5. #19 は現データでは再 open 率以外を算出できない（却下理由は自由文、採用・解決原因の記録なし、digest カードに却下ボタンなし）。
- 推奨順: #19（ラベル基盤）と #12a（表示）を並行 → #13 → #12b（shadow → on）→ #18 → #17 → #14（#11 と #19 のデータ蓄積後）。

## 1. 共通制約（全項目に効く）

1. **書込み位置**: 新規に `Ledger(` を生成できるのは `ci/gates.py:23` の `LEDGER_WRITERS` だけ（`gate_snapshot_readonly` :222-265、`gate_writer_lock` :268-330）。書込みを伴う新機能は、run_check の tick が持つ `ledger` を引数で受ける関数にする（signals と同型: `run_check.py:714-721`）。閲覧・集計は `mcs_view.View`（読取専用 snapshot）。別プロセスの新規 writer・cron は作らない。
2. **export allowlist は fail-closed**:
   - 新 stat を `PRESETS['operational'|'pharmacy']` に入れると `stat_not_exportable`（`export_schema.py:172`）。
   - 新 signal 型を `DETECTORS` に足すと、signal_type enum（:123-128）にないため `aggregate_field_type_invalid`（:161）。
   - 既存 stat の形を変えても同様（例 :85）。
   - どれも `brain_export.run` 全体が失敗する。`PRESETS` / `DETECTORS` と allowlist の整合を直接検証するテストはない（rg 0 件）。**共通ガードテストを先に入れる（F-4、S）**。
3. **plugin**: 変更すると `hermes gateway restart` が必要（`AGENTS.md`）。spec は未知キーを丸ごと拒否する（`adapters/common/spec.py:57-73`）。カード表示の追加は既存の container 型（heading / text / field / quote / meta: :73）で表現し、plugin 無変更にする。`notification_cards.kind` は CHECK 制約（`notify_cards.py:80`）で固定され、新 kind はテーブル再作成が要る。新機能は text route（`hermes send`）に載せる。
4. **config**: 新キーは `mcs_setup.CONFIG_RULES`（`mcs_setup.py:101-118`）と `docs/guides/INSTALLATION.md` の設定表に追加する。未知キーは warn のみ（`mcs_setup.py:233`）。送信先の直書きは禁止（`gate_notify_fail_closed`: `ci/gates.py:333`）。
5. **tick 粒度**: 昼 5 分・夜 20 分（`deployment/scripts/mcs_check.sh:13-24`）。`--jobs-only`（7・37 分）も derive → deliver を通る（`run_check.py:1195-1198`）。分単位の閾値の下限はこれ。
6. **完了時の共通チェック**: `python3 scripts/development/update_readme.py --check`、`python3 ci/gates.py`、ruff、`scripts/run_tests.sh tests/<領域>/`。`tests/views/views_testkit.SCHEMA` は最小スキーマ（patients に `history_floor` 等がない）なので、coverage / feedback 系は実 `Ledger`（tmp）か SCHEMA 拡張で書く。
7. **文言**: 「記録が見つからない ≠ 対応がなかった」を全出力で保持する（`mcs_signals.py:11-13`）。未取得・不明を 0 にしない（`mcs_stats.py:14-15`）。

## #12 緊急度エスカレーション

**目的**: 緊急の可能性が高い投稿を見落とさせない。追加する価値は次の 4 点。
- (a) 第一報の後に緊急と判明した投稿の再通知。
- (b) 未確認の間の再通知。
- (c) カード上の可視化。
- (d) チャネル単位の格上げ。

「即時配信」自体は新着通知が既に即時（tick 粒度）。個人宛ルーティングは作らない（ROADMAP §6）。

**現状**
- 抽出（ルール）: `_URGENT`（至急 / 緊急 / 早急 / すぐに / 急ぎ / 救急 / 搬送）の部分一致で high（`mcs/extract/v1/extract.py:48, 304-306`）。否定・時制は見ない。【実行確認】上記 3 文が high。専用テストなし。
- 抽出（LLM）: `urgency: high|routine`（`extract_llm.py:98` プロンプト、:306 schema、:915-919 検証、:1131-1138 チャンク統合は any-high、:1186 修復で high を落とさない）。
- QC: Jev（既定 OFF）が high / routine / unclear を再判定し、不一致を再抽出へ回す（`semantic_qc.py:156-165, 279-323`、`extract_llm.py:1641-1650, 1712`）。
- 合成ベンチ `evaluation/extract_cases.json` は 21 件で、urgency 期待 high が 2、routine が 12。精度算出には不足。
- 昇格の実体:
  - 述語 `_urgency_high`（`mcs_signals.py:1043-1058`）は、evidence の最後の message を extract_llm / extract_v1 の**どちらか**で判定する。
  - 使用は open 遷移時だけ: digest → immediate（:1190-1197）、`payload["urgent"]`（:1260-1261, :1277-1278。rescue で継承: :1122-1123）。
  - 送信時に text 先頭行へ「原投稿が urgency:high」を付与（`notify_flush.py:267-270`）。述語の重複が `notify_flush.py:92-97`。
  - open 後に urgency が変わっても再判定しない（`_notify_opened` は newly だけ: :1171-1206）。
  - 緊急度は「単独シグナルにせず通知の修飾に使う」が既定方針（`docs/dev-records/signal-priorities-20260924.md:24`）。
- 通知経路:
  - 警告マークは text route だけ（`notify_flush.py:369, 378`）。interactive カードは緊急度を描画しない（`notify_cards.py` / `notify_render.py` / `structured_view._head_lines`: :154 に urgency 参照なし）。`urgent` payload も card 経路は無視する（`notify_cards.py:1235-1257`）。
  - `notify.interactive` の既定は off。wizard は discord へ誘導するため、本番の実態は【未検証】。
  - 警告マーク・urgent 経路のテストは `tests/ops/test_mcs_signals.py:1227-1250, 1448-1473` だけ。`rg '⚠' tests integration` は 0 件。
- 後追い判明: tick は derive（ルール抽出 → LLM 最大 15 件・既定 90s、oldest_first: `run_check.py:685-697`）→ deliver の順（:1195-1198）。LLM urgency が第一報に間に合う保証はなく、後から high になっても再通知経路がない。
- 確認状態は既にカードにある: ack / 担当 / 保留（`notification_acknowledgements` / `notification_triage`: `notify_cards.py:188-202`。適用は :1548-1565, 1656-1700）。保留 24h（:61）は sweep が期限到来で open に戻す（:1877-1886）。ack は manifest（shown）単位（:1665-1685）。text route に ack はない。
- 旧 ROADMAP の訂正:
  1. `:1043-1060` は述語だけ。昇格判断は :1190-1197、flag は :1260 / :1277、表示は `notify_flush.py:267-270`。
  2. 「否定 / 時制は v4 で対応済」（旧 v2 #18、優先順位 6）は LLM レーンだけ。`_urgency_high` と `_urgency` はルール high も同格に扱う。
  3. §1 表の「リアルタイム」は tick 昼 5 分・夜 20 分。
  4. 「個人メンション」に相当するコードは repo にない（rg 0 件）。Hermes 側のメンション扱いは【未検証】。

**設計方針**
- **12a（表示。plugin 不要）**
  - `structured_view.message_urgency(db, mid)` を新設し、`notify_flush._urgency` と `mcs_signals._urgency_high` の重複を置換する。level と source（llm / rule）を返す。
  - `_head_lines` に 1 行足す: 「緊急度: 高（AI抽出）」/「緊急語を含む（機械照合）」。`structured_lines` は text / card 共有（:325）なので同時に反映される。
  - LLM 由来は `latest_artifact(db,'extract_llm',mid)` で読む。canonical_projection が shadow するため、`latest_fact_artifact` では拾えない。
  - 既存カードは sweep が source_fp（`fact_generations`）差分で update render する（`notify_render.py:165-211`、`notify_cards.py:1847-1911`）。編集は通知を鳴らさない。
- **12b（再通知。通知層の機能で、signal 型は増やさない）**: 新モジュール `mcs/notify/notify_urgent.py`（`ledger` 引数だけ）を `run_check._deliver` の flush 直前に呼ぶ。
  - 対象: 通知済み投稿で urgency=high が current（hash 一致）のもの。既定は LLM 由来だけ（`source: llm`）。ルール由来だけは表示止まり。
  - E1（後追い通知）: 第一報より後に high が確定した場合に、1 回だけ `urgent_notice`。判定は urgency artifact の `created_at` > 当該投稿を含む受理済み intent の `updated_at`。
  - E2（未確認再通知、cards だけ）: `after_min`（既定 30）経過で再通知。以降 `repeat_min`（60）× `max_repeats`（2）で打ち切り。text route には ack がないので E2 は出さず E1 だけ。
  - 停止条件: 該当 message が shown の manifest に ack 行あり / triage が assigned・deferred（期限内）/ 自施設投稿が ts 以降にある（`_self_post_exists`: `mcs_signals.py:212-231` を再利用）/ 依頼登録済み（:234-239）/ 削除・編集（hash 変化）/ archived / urgency が high でなくなった / mode が off。
  - 宛先: 既定は `notify_target`。任意で `escalation.target`（チャネル単位。`_target`: `notify_flush.py:68-79` に kind を追加）。メンション構文は生成しない。
  - 状態: outbox 行（`kind='urgent_notice'`、project_id 付きなので archived gate `notify_flush.py:947-953` が効く）。`payload={message_id, hash, stage}`（本文なし）。dedupe は (message_id, hash, stage) の JSON クエリで、`_notify_suppressed`（`mcs_signals.py:1209-1231`）と同型。shadow は `state='suppressed'` かつ `payload.shadow=true` の行だけ積み、送信しない。payload に `digest` / `signal_key(s)` キーを使わない（`notify_flush._hold_event`: :524-533 が signal 救出に入るため）。
  - 送信直前 gate（`_StaleSend`）で上記の停止条件を再検査する。文面は「至急の可能性（抽出による判定・要原文確認）／未確認N分」で、本文・投稿者名なし、患者名は送信時解決だけ（`_signal_text`: :170-199 と同型）。
  - 上限: `max_per_day`（既定 10）と room 冷却。
  - config: `escalation:{mode:off|shadow|on, source, after_min, repeat_min, max_repeats, max_per_day, target?}`。
- 作らないもの: 新 signal 型、メンション、専用テーブル。
- **12c（任意・別判断）**: ルール抽出の否定 / 時制ガード。`RULE_VERSION` 6→7 で全 v1 が削除・再生成される（`extract.py:64, 308-326`）。urgency の消費者は `notify_flush._urgency` と `mcs_signals._urgency_high` だけで影響は小さいが、rollup の一括再構築が走る。

**成果物**
- 12a: `structured_view.py`（述語 + 1 行）、`notify_flush.py` / `mcs_signals.py`（述語置換）、tests。
- 12b: `notify_urgent.py`、`notify_flush.py`（`_format_event` 分岐 :297-340、`_target`）、`run_check.py`（1 呼出）、`mcs_setup.py`（CONFIG_RULES・検証）、`INSTALLATION.md`、`CHANGELOG.md`、`tests/notify/test_notify_urgent.py`。

**受入条件とテスト（合成のみ）**
- `message_urgency`: LLM high / rule high / 両方 / hash 不一致 / deleted / LLM routine + rule high の各挙動を固定する。
- text と card の両方で high 投稿に緊急度が出て、routine には出ない。LLM 到着後に card が update render になる（新 intent なし）。
- 述語置換後も `tests/ops/test_mcs_signals.py:1227-1250, 1448-1473` が不変。
- E1 が 1 件だけ（再評価で増えない）。第一報前から high の投稿には E1 なし。E2 は `now` 注入で決定的に `after_min` / `repeat_min` / `max_repeats` を守る。
- 停止条件（ack、assigned、deferred の期限内・期限後、自施設投稿、依頼登録、削除・編集、archived、降格、mode off）を各 1 件。enqueue 後に停止条件が成立すると、送信直前に terminal drop。部分送信ありは hold（`_fail_event`: `notify_flush.py:868-929` の規約）。
- shadow は送信されない。text route では E2 が出ない。payload と文面に `<@` / `@here` / `@everyone` / 投稿者名 / 本文センチネルがない。`max_per_day` と room 冷却。夜間 20 分 tick でも取りこぼさず遅延だけ。
- `len(DETECTORS)` 不変（ガードテスト）。

**依存・順序**: 12a → 12b。共通述語は #13 と独立。12b は `_format_event` / run_check / CONFIG_RULES で #13 と同じ箇所を触るため直列。12b の shadow 評価は #19 があると楽（必須ではない）。

**規模**: 12a=S、12b=M（全体 M）。

**オーナー判断・リスク**
- `#12-D1` 対象を LLM だけにするか、ルール語も含めるか。
- `#12-D2` `after_min` / `repeat_min` / `max_repeats`。
- `#12-D3` 専用チャネルの要否。
- `#12-D4` 文面に本文抜粋を入れるか（既定は入れない）。
- `#12-D5` 警告表示の現挙動（ルール由来を同格表示）を弱めるか。
- 通知疲れ: LLM high の適合率は【未検証】（ベンチが少なく、実データも未読）。card 表示の追加で、既存の live card が一度 update される（sweep は 1 tick 50 件まで、編集のみで通知なし）。`notify_target` と card 用チャネルが別構成だと、再通知が別チャネルに出る【未検証】。Hermes 側のメンション既定は【未検証】。

## #13 日次 digest

**目的**: 朝 1 通で件数と一覧（ID のみ）を出し、取得状況を必ず開示する。文章要約は含めない。§4 C4 で Work Queue 代替を再評価するため、最小実装・既定 off にする。

**現状**
- 既存の signals digest: digest tier の open ごとに未送信 digest intent へ畳み込み、最初の 1 件から `digest_interval_h`（既定 24h）後に 1 通。固定時刻ではない（`mcs_signals.py:997-1010`、`_digest_add` :1131-1168、`_notify_opened` :1171-1206）。対象は新規 open だけで、`signals.notify:true` が必須（:859、既定 false）。digest tier は 11 型中 8 型で、`request_overdue` / `request_aging` / `rx_period_expiry` を含む（:1005-1008）。
- カードは `kind='digest'`（`notify_cards.py:80, 1235-1244`、`PAGE_DIGEST=5`: `notify_render.py:19`）。ack は「このページを確認」（:597-598）、却下ボタンなし（:628-630）。
- text 経路: `flush`（`notify_flush.py:932-976`）は `_format_event`（:297-340）で kind 分岐し、未知 kind は `_message_notice` に落ちる（:340）。新 kind は分岐の追加が必須。患者内容は `notify_target` だけ（`_target`: :68-79）。本文は送信時に台帳から引く方針（:12-14）。1900 字で分割（:45）。
- 材料:
  - 新着は `messages.first_seen`（初回観測だけ設定・更新なし: `ledger.py:1051-1122`）。
  - `is_unread` は sticky（MAX: :1105-1107）。既読化は各 tick（`run_check.py:453-465`）なので「現在未読」ではない。
  - 取得未完了は `patients.fetch_state` / `fetch_reason` / `history_floor`（-1=自然終端、>0=cutoff、0/NULL=完了記録なし: `ledger.py:898-916`）/ `coverage_ts`、`fetch_jobs`、`attachments.state`、`messages.body_state`、`runs(status,error,kind)`、outbox の held（`state='failed'` かつ `next_try` NULL: `run_check.py:117-125`）。
  - `health.json` は tick 末尾で書く（:1202）ので、digest は DB を使う。open signals は `mcs_signals.current_open`（:1283-1317）。
- `/mcs` plugin は stats / signals を出せない（`hermes_plugin/__init__.py:31-35` `_READ_KINDS`）。配信は outbox 経由だけ。
- 旧 ROADMAP の訂正: (1) 「open シグナル」に上記 3 型が含まれ、「期限・予定・依頼は含めない」と矛盾する。(2) 「未読の多職種連絡」が未定義。(3) 既存 signals digest との重複整理が未記載。

**設計方針**
- 新モジュール `mcs/notify/notify_digest.py`。tick 内（`stage_derive` 末尾、`signals.evaluate` 直後: `run_check.py:714-721`）で `maybe_enqueue(ledger, cfg, now)` を呼ぶ。`hour_jst` 以降かつ当日分が未作成なら、outbox に text intent（`kind='daily_digest'`）を 1 件。dedupe は `payload.date` の JSON クエリ。遅い tick は catch-up、翌日は新規。`payload={date, since, until}` だけ。`digest` / `signal_key(s)` キーは使わない。描画は送信時（`_format_event` に分岐を追加）。archived を除外して再描画。config off なら `_StaleSend`。
- 内容（固定ブロック）:
  1. **新着**: 件数・ルーム数・職種別件数。`first_seen` ∈ 窓、かつ `notify_max_age_h` 以内（履歴取込を除外）。archived・deleted 除外。
  2. **取得状況（常に出す）**: `fetch_state='incomplete'` のルーム数と ID（理由コード）。待機・失敗 job（kind 別）、`body_state≠full` 件数、添付 failed / pending。24h の run status 内訳と最終 ok 時刻、outbox held 件数。0 件でも「未完了として記録された範囲: なし（完全性の保証ではありません）」を出す（`gapless_verified` は常に false: `mcs_view.py:175-235`）。
  3. **緊急度**: 窓内で high の件数（LLM / ルール別）と (project, message) 一覧。cards なら未確認数。
  4. **open シグナル**: 型別件数と前回比の新規数。`request_overdue` / `request_aging` / `rx_period_expiry` は除外（依頼・期限）。`signals.notify:true` のときだけ出す。
  5. **未確認の多職種連絡**: 既定案は、窓内到着（`is_unread=1` かつ自施設外）の職種別件数。cards なら未 ack スレッド数を併記。定義は #13-D2。
- 配信面: 既存 signals digest と併存が既定。要約・points・本文は出さない。患者名は `include_names`（既定 false、ID のみ）。
- config: `daily_digest:{enabled:false, hour_jst:8, max_list:10, include_names:false, send_when_empty:true}`。
- 共通ヘルパ `mcs/views/mcs_coverage.py`（`coverage_summary(db, …)`）を作り、#18 が拡張する。
- 作らないもの: card 化（CHECK 制約）、新 cron / launchd（別プロセス writer は `LEDGER_WRITERS` と run.lock に抵触。夜間も tick は動く）。

**成果物**: `notify_digest.py`、`mcs_coverage.py`、`notify_flush.py` の分岐、`run_check.py` のフック、`mcs_setup.py`、`INSTALLATION.md`、`tests/notify/test_notify_digest.py`。

**受入条件とテスト（合成のみ）**
- 件数境界: `first_seen` の窓境界、`notify_max_age` 超の履歴取込を除外、archived・deleted 除外。
- 出力に合成の本文・氏名・要約・points が 0 件（`include_names=false` で患者名なし）。
- 取得状況ブロックはデータ 0 件でも出る。incomplete ルームは ID + 理由コード、held outbox は件数で出る。
- `request_overdue` / `request_aging` / `rx_period_expiry` が open でも出ない。`signals.notify=false` なら signal ブロックなし。
- 冪等: 同日 2 回 enqueue で 1 件。DB 再オープン後も 1 件。hour 前は作らない。hour 後の遅い tick で catch-up。翌日は新規。
- config off へ切り替えた後は `_StaleSend` で終端。archived ルームは再描画で除外。
- 1900 字以内（一覧は上限 + 「他N件」）。`kind='daily_digest'` が `_message_notice` に落ちない。`--no-notify` では enqueue だけで flush しない。既存 signals digest の挙動が不変。「記録なし ≠ なかった」の注記がある。

**依存・順序**: #12b と同一箇所を触るため直列。`mcs_coverage` を #18 が再利用するので、#13 を #18 より先に。#19 とは独立。

**規模**: M（小）。

**オーナー判断・リスク**
- `#13-D1` 患者名を出すか、ID だけか。
- `#13-D2` 「未読の多職種連絡」の定義。
- `#13-D3` 送信時刻・休日・空日の heartbeat。
- `#13-D4` signals digest との統合（tier 'daily' で open 通知を抑止）を行うか。
- `#13-D5` C4 で置換予定のため、維持コストをどこまで許すか。
- 収集アカウントと人手アカウントの関係次第で「未読」の意味が変わる【未検証】。配信先は `notify_target`（患者内容を許容する宛先）。

## #14 変更エピソードの紐付け

**目的**: `med_change_no_followup` の誤検知（実際はフォロー済みなのに open のまま）を減らす。新規シグナルは増やさず、閉じる方向だけとする（`open_new ⊆ open_old` を不変条件にする）。

**現状**
- 変更判定は `CHANGE_ACTIONS`（`mcs_queries.py:23-25`）、現行患者の薬に限定（`med_is_patient_current` :201、`MED_PATIENT_CURRENT_SQL` :227）、能力表現を除外（`MED_NOT_CAPABILITY_SQL` :242）。
- `_med_followup`（`mcs_signals.py:250-340`）: エピソード = (room, 空白正規化した薬表記)（:332）。「後続」は room の**任意の後続投稿**か依頼登録（:307-321）。窓は `followup_days`=7、対象 90 日、変更は `ops.signal_policy` だけ（`THRESHOLDS` :62-74）。【実行確認】変更言及の 9 日後（窓外）に同薬の観察言及（action none）を置いても open のまま。これが「閉じる側」の具体的な FP。
- 関連検知: `symptom_after_med_change` は同一投稿内の結合だけ（:729-787）。`transition_reconciliation` は discharge / transfer と薬変更の ±14 日共起（:431-459、`mcs_queries.py:282-325`）。
- 統計側 `st_med_change_followup`（`mcs_stats.py:513-562`）が同じ窓ロジックを独立実装している。signal と stat は同一述語であるべき（`mcs_queries.py` の docstring）。
- episode 用の artifact / table は存在しない。`open_loop_aging.needs` に名前があるだけ（`mcs_stats.py:732-734`）。USER_GUIDE は interaction_links だけに言及（`docs/guides/USER_GUIDE.md:361-363`）。adapter が保存する参照情報は `parent_id` と `reply_count` だけ（`mcs_adapter.py:439-485`）。
- 旧 ROADMAP の訂正: (1) 旧 v2 #16 は #3 drug_map 依存だが、新 #14 は依存を落としている。薬名は表記ゆれ未統合（`mcs_stats.py:378-380`）。(2) `open_loop_aging.needs=[interaction_links, episode_links]` は #14 と #17 を結合しているが、コード docstring（:585-587）と USER_GUIDE は interaction_links だけが必要と述べている。needs を分離する。(3) 「誤検知の削減」を測るラベルがない（#19 が前提）。

**設計方針**
- 定義: エピソード = (project_id, 薬キー)。薬キーは現行 = 表記正規化、#11 後は成分 ID。観察 = 最新の変更言及より後の同薬言及（action none 含む、誰の投稿でも自施設含む、`med_is_patient_current` を適用）。
- 閉じ方は加算だけ: 既存の抑制（窓内後続投稿・依頼登録）は不変。追加抑制は「最新の変更言及より後に、同薬の観察言及が now までに存在する」。
- 保存: 新テーブル・新 artifact は作らない。読取時に導出する（候補エピソードごとの per-candidate クエリで足りる）。薬キーの再正規化（#11）で移行が不要になる。説明責任は resolved 行の `resolution:{cause:"episode_observation", message_id}` に残す（#19 と共有）。
- ロールアウト: `THRESHOLDS["episode_closure"]=(0,0,2)`（0=off, 1=shadow, 2=on）。`ops.signal_policy --confirm-human`（`mcs_operations.py:541-571`）でだけ変更する。"no adaptive tuning"（`mcs_signals.py:14-17`）と整合する。shadow は `evaluate()` の戻りに `would_close`（key と link message_id、ID のみ）を返し、`episode_shadow_v1` artifact を (key, link) 単位で 1 回追記する（審査キュー）。
- 誤検知削減の測定:
  1. shadow diff: `S_old` と `S_new` を比較し、`S_new ⊆ S_old` を機械検証する。suppressed = `S_old − S_new`。
  2. 人手監査: suppressed の全件（または 30 件以上）を原記録（`mcs_view thread/evidence`）で確認する。**偽リンク（未フォローなのに閉じた）が 0/N** を on の条件にする。結果は件数だけ dev-records に残す。
  3. 事後: #19 の型別却下率（理由コード = 誤検知 / 対応済み）と再 open 率の前後比較。n と区間を併記し、因果は断定しない。
  4. 過去再生: `evaluate(ledger, cfg, now=…)`（:851-856 は now 引数あり）を台帳コピーに対し複数の now で走らせる。本番 DB は不変。
- 対象外: `symptom_after_med` の cross-post 結合（USER_GUIDE / docstring のとおり FP を増やす）、`transition_reconciliation` の絞り込み。

**成果物**: `mcs_queries.later_med_observations(db, pid, med_key, after_ts, until_ts)`、`mcs_signals.py`（`_med_followup`・`THRESHOLDS`・resolved の resolution・evaluate の戻り）、`st_med_change_followup` の追随、tests、`DEVELOPMENT.md`、監査手順の dev-record 雛形。

**受入条件とテスト（合成のみ）**
- 窓外 9 日後の同薬観察: mode 2 で close、mode 0 / 1 で open（実行確認そのもの）。
- 別薬の後続は close しない。否定・家族・past・能力表現・unverified の言及は観察に数えない。自施設の観察は数える。変更言及の再言及は観察でなくエピソード更新。後続の変更言及は新エピソード。archived ルームの扱い。
- property test: 合成 200 ルーム（seed 固定）で `open_new ⊆ open_old`。
- mode 1 は `signal_v1` 行を増減させず `episode_shadow_v1` だけ。resolved 行に resolution が入る。signal と stat が同一の合成 DB で同一集合を返す。mode 変更は `ops.signal_policy` 経由だけで、範囲外値は拒否。候補 1,000 件でクエリ回数が候補数に線形。

**依存・順序**: 本来は #11（薬名正規化）の後。表記一致版は先行できる。#19 が前提（測定）。`mcs_signals.evaluate` を #19 と共有するため、#19 を先にマージする。

**規模**: L（実装だけなら M。shadow・人手監査を含む）。

**オーナー判断・リスク**
- `#14-D1` 閉じる条件を「同薬の言及」までにするか、観察語（評価・症状）まで絞るか。
- `#14-D2` 窓外でも閉じてよいか（タイムリネス懸念 vs 陳腐化）。
- `#14-D3` on の判定基準（偽リンク許容 0 でよいか）。
- 過剰クローズによる見逃しの隠蔽（無関係な同薬言及、例「残薬確認」で閉じる）、表記ゆれ、実データでの FP 率は【未検証】。

## #17 職種間やり取りの構造と応答時間

**目的**: 相談 → 回答の応答時間と未応答滞留を、個人単位データを出さずに内部統計として可視化する。本文由来の候補は台帳へ書かない。

**現状**
- `st_open_loop_aging`（`mcs_stats.py:584-640`）は requests 台帳だけを集計し、status は `partial`、`text_candidates: unavailable`（:634-635。テスト `tests/views/test_mcs_stats.py:187`）。
- 使えるデータ: `messages.parent_id`（返信は root への 1 段: `mcs_adapter.py:488-494`）と `reply_count`。profession はカンマ結合の複数値（:374-379）、organization は station 名（:382-387）、`sender_id`、`posted_at_ts`。mention / 宛先 / 引用フィールドは adapter が保存しない（`_norm_message`: :439-485）。raw 応答にあるかは【未検証】。テキスト由来は extract_llm の `requests[{to,from,action,due}]`（`extract_llm.py:99`。to は 医師 | 看護師 | 薬剤師 | ケアマネ | 介護士 | 家族 | 不明）。
- 応答判定の既存実装は `_pharmacist_request`（`mcs_signals.py:472-522`）。宛先 = 薬剤師系、応答 = 自施設・自職種の投稿か依頼登録（:212-239）。room 内の後続投稿基準で、thread 非依存。
- 別系統: semantic の `loop_candidate` / `loop_event`（`semantic_loops.py:42-255`、閲覧 `mcs_view.py:520-637`）は Jev（外部）依存で `semantic.mode` 既定 off。stats は使っていない。
- export: `_STATS['open_loop_aging']['text_candidates']` は `{"status": _enum("unavailable")}` だけ（`export_schema.py:85`）。埋めると `aggregate_field_type_invalid`（:161）で brain_export 全体が失敗する。`professions` stat は profession_map 未実装（`mcs_stats.py:717-719, 277-278`）。
- 旧 ROADMAP の訂正: (1) 行番号ずれ。実際は :584-640、`text_candidates` は :634-635。(2) 「interaction_links を整備」は永続テーブルを要しない。(3) 埋めるには allowlist とテストの同時更新が必須。(4) mention 仮定の根拠なし。

**設計方針**
- `interaction_links` は永続化せず、**名前付きの導出（SQL 定義）**とする。第 1 版は決定的リンクだけ:
  - `thread_reply`: root（`parent_id IS NULL`）と最初の reply の latency（`posted_at_ts` 差）。粒度 = (root 職種群 → 返信者職種群)。
  - root の `reply_count` > 保存済み full 返信数（`mcs_view._status` の `incomplete_reply_roots` と同定義）は「不明」として分離する（未応答と断定しない）。
  - 第 2 版 `request_response`: extract_llm の requests（confirmed だけ、宛先不明は除外）の後続 room 投稿を「応答候補」とする。`_pharmacist_request` の SQL を共有関数に抽出して再利用し、重複実装しない。mention は対象外。
- 個人データを出さない: 出力は職種群ペア × {n, median, p90（nearest-rank）, 未応答数, 年齢バケット}だけ。`sender_id`・名前・organization は出力にもキーにも使わない。n < k（既定 5）のセルは値を伏せて `small_cell` と表示する。未応答一覧は (project_id, message_id) だけ。allowlist に追加しない（第 1 版）。ローカル出力だけ。
- 職種正規化: profession を `", "` で分割し、`stats.profession_map`（config。既定 = 恒等、未対応 =「その他」）で群化する。自施設は `_self_sets` で判定する。
- 第 1 版は新 stat `interaction_latency`（T2）を追加し、`open_loop_aging` には触らない（export 結合を避ける）。第 2 版で `text_candidates` を実値化し、allowlist 拡張・テスト更新・README/USER_GUIDE 更新を同一変更で行う。
- 限界を出力に明示する: thread 返信だけが確定リンク。room の新規投稿での回答は候補。返信なし ≠ 未対応。

**成果物**: `mcs_queries.py`（`thread_reply_pairs`、`requests_awaiting_response`）、`mcs_stats.py`（`st_interaction_latency`、needs 修正、後続で `text_candidates`）、`profession_map`（config・`mcs_setup` 検証・`INSTALLATION.md`）、第 2 版だけ `export_schema.py`、tests/views、USER_GUIDE「まだ取れないもの」の更新。

**受入条件とテスト（合成のみ）**
- root + reply の latency が正確（同職種 / 異職種 / 複数職種の 3 パターン）。reply なし root の未応答判定。incomplete reply root は「不明」で未応答に数えない。
- 期間境界（JST 半開区間、`_scope` 再利用）。削除 reply は除外。複数職種の按分 / 重複の仕様固定。
- n < 5 は値 null + `small_cell`。分母 0 は null。
- 出力全文に送信者名・ID・施設名・本文の合成センチネルが 0 件（`--project` 指定でも）。
- 新 stat は PRESETS になく、`brain_export.run` が合成 snapshot で通る。
- 第 2 版: `text_candidates` 許可後も通る。request_response 候補は confirmed だけ・宛先不明除外・依頼登録済み除外・自施設応答で除外で、既存 signal と一致する。

**依存・順序**: `profession_map` は #18 の職種別でも使うため #17 が先。#14 とは needs の分離だけ。`mcs_stats.py` の REGISTRY と生成 docs を #18 / #19 と共有するため直列マージ。

**規模**: 第 1 版 M。`text_candidates` と allowlist まで含めると L。

**オーナー判断・リスク**
- `#17-D1` 小セル閾値 k と職種群の粒度。
- `#17-D2` `text_candidates` を出すか。
- `#17-D3` 職種 map の定義。
- 職種表記の実分布は【未検証】。少人数の職種は k 抑制でも推測できる。latency の人事評価への転用を防ぐため、個人別集計は作らず、出力に「個人評価に使わない」旨を記す。MCS で返信せず新規投稿する運用では過小評価になる。API に mention があるなら別設計【未検証】。

## #18 業務負荷レポート

**目的**: 薬局管理者向けに、夜間休日の連絡量・投稿偏り・依頼滞留を週次 / 月次で出力する。数値には必ず「取得未完了範囲」を併記する。個人別集計・個人評価は出さない。

**現状**
- 既存 stat（すべて snapshot 読取専用・ID のみ・分母付き `_ratio`: :40-44）: `workload`（:282-313）は曜日 × 時間帯（weekday_day = 平日 08-18 JST、weekday_night、weekend で、祝日は未考慮）。`doc_burden`（:316-344）は送信者集中 top1 / 5 / 10 と HHI（送信者 ID は出力しない）。`professions`（:264-279）、`patient_activity`（:233-261）、`open_loop_aging`（:584-640）。
- **`comm_concentration` は stat ではなく signal**（`mcs_signals.py:343-361`）。REGISTRY は 16 stat でそれを含まない（`mcs_stats.py:710-746`）。
- `workload` / `doc_burden` / `professions` はどの PRESETS にも入らず（:748-754）、allowlist にもない。【実行確認】REGISTRY − `_STATS` = professions, workload, doc_burden, canonical_facts, card_parts。preset に入れると brain_export が失敗する（`export_schema.py:172`）。
- 定期実行: repo のスケジューラに stat / brain_export ジョブはない（`mcs_setup.py:1473-1480` の `CRON_JOBS` は 6 件、`deployment/launchagents/README.md:5-19`）。「定期出力」は新規。前例は `brain_export.py`（Markdown + jsonl、保持 62 日）。
- 取得未完了の集約はない: `read_model._coverage`（`read_model.py:273-303`）は抽出状態と添付だけ。`mcs_view status`（`mcs_view.py:175-235`）は room 単位で `gapless_verified:false`、`exact_missing_ranges:null`。`st_data_quality`（`mcs_stats.py:128-191`）は fetched / parsed / timed の 3 段だけ。範囲別の集約はない。
- 旧 ROADMAP の訂正: (1) `comm_concentration` は stat でなく signal（旧 v2 #27 の記述の引継ぎ）。(2) 「既存統計の定期出力」ではなく、定期出力の仕組みごと新規。(3) 取得未完了の集約 stat がない。

**設計方針**
- **`coverage_gaps` stat（T0）を新設**。期間 [since, until) に対する取得未完了の集約:
  - room の履歴完了判定: `history_floor` が -1、または 0 < floor ≤ since なら「期間先頭まで完了記録あり」。それ以外は「履歴未完了の可能性」。
  - 併せて `fetch_state='incomplete'`、`body_state≠full` の件数と範囲、incomplete reply roots、pending / failed `fetch_jobs`、添付 failed / pending、`coverage_lag>0` を集計する。
  - `affected_share` = 影響 room の期間内投稿数 / 総投稿数（`_ratio`）。
  - 常時付記: 「完了記録あり ≠ 欠落なし（`gapless_verified=false`）」「連絡量は下限になり得る／偏り指標は方向不定」「削除・編集の反映は遅れ得る」。
- **レポート生成** `mcs_view.py report --period week|month [--as-of]`:
  - 1 snapshot generation に固定し、`contract:"mcs-workload-report/1"` と `snapshot_generation_id` を付ける。
  - 新 preset `workload_report` = [data_quality, coverage_gaps, patient_activity, professions, workload, doc_burden, open_loop_aging]。`operational` / `pharmacy` は不変。
  - 出力は `data/reports/{weekly,monthly}/…{json,md}`。0700、集計だけ、ID のみ、患者名・送信者名なし、保持 400 日。JSON は固定形の allowlist、MD は分母・注記付き。
  - fail-closed: coverage が計算できない場合は「取得状況判定不能・参考値」バナーを付ける。coverage なしの数値だけの出力は不可。
- 配信は Phase 1 でローカルファイルだけ。Phase 2（任意）で tick 内から `report_notice`（見出し数値 + 取得未完了の要約）を outbox へ。#13 と同じ周期ヘルパを使う。
- スケジュール: 読取専用なので hermes cron が適合する。`deployment/scripts/mcs_report.sh` + `CRON_JOBS` に週次・月次。run.lock 不要。実機適用は別承認（`AGENTS.md`）。
- 出さないもの: 送信者別集計、患者名、本文、個人別応答時間。

**成果物**: `mcs_stats.py`（`coverage_gaps`、preset、REGISTRY）、`mcs/views/mcs_report.py` + `mcs_view.py` の subcommand `report`、`deployment/scripts/mcs_report.sh`、`mcs_setup.CRON_JOBS`、tests/views・tests/meta、docs（生成表・README・INSTALLATION）、`CHANGELOG.md`。

**受入条件とテスト（合成のみ）**
- coverage 計算を例外にすると、レポートはバナー + `coverage.status='unavailable'` になる。coverage なしの数値だけの出力は不可。
- 履歴未完了ルーム（floor NULL、`fetch_state` incomplete、snippet body）が ID・理由コード・件数で出て、完了ルームは出ない。floor 境界（-1 / 0 / == since / > since）の 4 ケース。`affected_share` の分子分母。
- 期間境界（JST 半開区間）。分母 0 は null。夜間・週末帯の定義が `st_workload` と一致する。
- 出力全文に送信者名・患者名・施設名・本文のセンチネルが 0 件。report は DB を書き換えない（read-only snapshot で書込み試行が失敗）。同一入力で出力がバイト一致。`generation_id` が `snapshot_meta` と一致する。
- `PRESETS['operational'|'pharmacy']` が不変で、`brain_export.run` が通る（ガード）。
- `mcs_report.sh` のプレースホルダ・構文テスト（`tests/meta/test_deployment_scripts.py` 同型）。`update_readme --check`。

**依存・順序**: #13 の `mcs_coverage` を範囲版に拡張するため、#13 の後。`profession_map`（#17）は任意。`mcs_stats.py` の REGISTRY を #17 / #19 と共有するため直列。

**規模**: M。

**オーナー判断・リスク**
- `#18-D1` 読者と配信先（ファイルだけか Discord か。Discord は PHI の面が増える）。
- `#18-D2` 週次・月次の切り方（ISO 週 / JST）。
- `#18-D3` 「休日」の扱い。祝日カレンダーは未実装で、外部ライブラリは使えず、静的表は保守コストがある。当面は土日だけ。
- `#18-D4` 自施設分と全体の分割の要否。
- `#18-D5` 保持期間。
- 個人評価への転用は既存の note を踏襲して明記する。夜間投稿は勤務を意味しない（既存 note）。実データでのクエリ時間は【未検証】。

## #19 シグナル精度のフィードバック

**目的**: シグナルの実際の有用性を、内部の人手行動だけで評価し、閾値・文言を人が見直す材料にする。検知条件への自動フィードバックはしない（`mcs_signals.py:14-17`）。外部送信なし、zaitaku-calender への逆流なし（ROADMAP §6）。

**現状**
- `signal_v1` は append-only。open / superseded / resolved / dismissed が各 1 行（`meta.key`、`created_at`）。open 更新は `mcs_signals.py:887-910`。resolved（条件消失）は :911-926 で、**原因を記録しない**。dismissed は同証跡の間は抑止し、証跡変化で再 open（:888-899）。dismissed も条件消失で resolved になる（:916-926）。再 open 率は行履歴（resolved → open）から導出できる。
- 人手行動:
  - (a) 却下 `ops.signal_dismiss`（検証 `mcs_operations.py:124-136`、適用 :488-538）。dismissed 行に `dismissed_by` / `dismiss_reason`（**自由文 ≤2000**）/ `dismiss_command_id`。理由は構造化されていない。モーダルは単一テキスト（`mcs_discord/actions.py:371-376`、Slack `mcs_slack/actions.py:22`、envelope `adapters/common/envelopes.py:198-209`）。
  - (b) ack / 担当 / 保留（`notify_cards.py:1548-1565, 1656-1700`）。ack は manifest（shown = signal_keys）単位。
  - (c) 依頼登録 `request.create`。検知器は登録済み依頼を「応答」とみなして自動 resolved にする（`mcs_signals.py:234-239`）が、signal ↔ request の紐付けも resolved 原因も記録しない。
- **却下ボタンは単一キーの signal カードだけ**（`notify_cards.py:628-630`）。digest カード（既定で 11 型中 8 型: `mcs_signals.py:997-1009`）は「ページ確認」だけ（:597-598）。主要な型は Discord から却下できず CLI だけ。
- ack / dismiss が発生する前提は `signals.notify:true`（既定 false）かつ interactive カード。本番設定は【未検証】。
- 既存の集計はない。`mcs_refstats.py` は承認済み stat 基準との回帰差分、`evaluation/` は semantic 要約 G6 用で signal 用ではない。resolved / dismissed は export されない（`brain_export.py:299` は `current_open` だけ）。
- 旧 ROADMAP の訂正: 「却下理由・採用率・再open率」のうち、現データで算出できるのは再 open 率だけ。

**設計方針**
- **ラベル（既存データ + 最小追記）**: `adopted`（evidence の message_id 群に対し `requests.created_at ≥ detected_at` の依頼がある。近似。厳密化は将来 `request.create` に任意 `origin_signal_key`）、`acknowledged`（signal / digest カードの ack: `manifest.shown ∋ key`）、`dismissed` + `reason_code`、`auto_resolved` + cause、`open_unactioned`（通知済みで N 日行動なし）、`reopened`（resolved / dismissed → open の回数）。
- **追記（writer、後方互換）**:
  1. `evaluate()` の resolved 行に `resolution:{cause}` を記録する。cause は `request_registered` / `responder_post` / `evidence_aged_out` / `condition_cleared` / `dismissed_then_cleared`。既存述語（`_request_registered` :234、`_self_post_exists` :212）を再利用する。#14 が `episode_observation` を追加する。`_signal_content`（:814-831）は追加キーで壊れない。
  2. `_v_signal_dismiss`（`mcs_operations.py:125-126`）に任意の `reason_code`（enum: 誤検知 / 対応済み / 重複 / 対象外 / その他）を許可する。plugin 未更新でも従来どおり `reason` だけで動く。当面は reason 冒頭のタグ規約で分類し、未分類は件数だけ表示する。
  3. 任意（今回は非推奨）: plugin モーダルの拡張や digest の項目別却下ボタン。spec / token / plugin の変更となり、gateway 再起動が必要。
- **集計**: `st_signal_feedback`（T2 stat、snapshot 読取。PRESETS にも `_STATS` にも入れない）。型別に {opened, shown（delivered の card / page に載った）, acked, adopted, dismissed（reason_code 分布）, auto_resolved（cause 分布）, reopened, time-to-first-action の median / p90, open>30d 比率}。率は `_ratio`（分子分母）+ Wilson 区間。n < 20 は `insufficient_n` で率を出さない。actor 名・自由文は出力しない。
- **評価文書** `evaluation/signal-feedback-v1.md`: 定義、ラベル表、判定規則（例: n ≥ 20 かつ却下率 ≥ 50% → 見直し候補）。見直しは人が `ops.signal_policy` で承認し、自動適用はしない。月次のローカルレポートは #18 の report 基盤に相乗りできる（任意）。
- 内部限定の担保: PRESETS / allowlist 非登録、Discord へ送らない、actor と自由文を出力しない。

**成果物**: `mcs_signals.py`（resolution）、`mcs_operations.py`（reason_code）、`mcs_stats.py`（`st_signal_feedback`）、`evaluation/signal-feedback-v1.md`、`DEVELOPMENT.md`、tests/ops・tests/views、（任意）plugin。

**受入条件とテスト（合成のみ）**
- 合成 ledger で open → dismissed（`reason_code=誤検知`）→ 同証跡で抑止 → 証跡変化で再 open → resolved を再現し、opened / dismissed / reopened / auto_resolved が期待値になる。
- 依頼登録による自動 resolved が `cause=request_registered` かつ adopted に計上される。自施設投稿は `cause=responder_post`。horizon 外は `aged_out`。
- digest ページの ack が shown の全 key に ack として計上される。n < 20 は率 null + `insufficient_n`。
- 不正な `reason_code` は `bad_reason_code` で拒否。未指定は従来どおり受理（既存 `tests/ops/test_mcs_operations.py` / `test_mcs_operation_cli.py` の期待は不変）。
- 集計が DB を書かない。resolution のない旧行でも集計が落ちない。
- **検知不変**: feedback stat の実行前後で `_thresholds()` と `evaluate()` の結果が不変。
- 出力に actor 名・自由文の合成センチネルがない。`project_record({'type':'stat','name':'signal_feedback',…})` が `stat_not_exportable`。`DETECTORS` の全型が集計に現れる。

**依存・順序**: 最初に着手する。ラベルは時間で蓄積し、#12b / #14 の評価基盤になる。`mcs_signals.evaluate` を #14 と共有するため、#19 を先にマージする。plugin 拡張は任意で最後（gateway 再起動が必要）。

**規模**: M（stat だけなら S。writer 追記と `reason_code` で M。plugin 拡張は別途 M）。

**オーナー判断・リスク**
- `#19-D1` 理由語彙の確定。`#19-D2` digest 型の却下 UI（plugin 変更）を作るか。`#19-D3` ack を「採用」に数えるか。`#19-D4` n の下限。
- 却下率は誤検知率の代理にすぎない（「対応済み」で却下すれば真陽性でも却下）。未行動 ≠ 偽陽性。精度・再現率とは呼ばない【未検証の代理指標】。actor が少人数だと再識別できるため、actor 非出力を厳守する。

## この領域の実施順

1. **共通ガードテスト**（S）: PRESETS / DETECTORS ⊆ export allowlist（F-4）。
2. **Phase 1（並行できる。ファイルはおおむね非衝突）**: #19（`mcs_signals` / `mcs_operations` / `mcs_stats`）‖ #12a（`structured_view` / `notify`）。#12a の述語置換は `mcs_signals.py` にも触れるので、マージ順に注意する。
3. **Phase 2**: #13（`mcs_coverage` の軽量版 + 日次 digest）。#12b は #13 のマージ後に shadow を開始する（`_format_event` / run_check / CONFIG_RULES が衝突）。
4. **Phase 3**: #18（`coverage_gaps` の範囲版・report・cron 追加）。#12b の shadow → on の判定。
5. **Phase 4**: #17 第 1 版（`interaction_latency` + `profession_map`）。
6. **Phase 5**: #14 は #11 の完了と #19 のラベルが n 十分になってから。shadow → 人手監査 → on。#17 第 2 版（`text_candidates` + allowlist）は独立に後回し。

ROADMAP の番号順（12 → 13 → 14 → 17 → 18 → 19）から変える理由: #19 は測定基盤でラベルが時間依存。#12 を表示（12a）と再通知（12b）に分割し 12a を先行。#18 は #13 の coverage を再利用。#14 は #11 依存かつ L で、測定（#19）が先に要る。

衝突マトリクス: `mcs_stats.py` の REGISTRY と生成 docs は #17 / #18 / #19 で直列。`mcs_signals.evaluate` は #19 → #14。`notify_flush._format_event` / `run_check` / `mcs_setup.CONFIG_RULES` は #12b / #13 で直列。
