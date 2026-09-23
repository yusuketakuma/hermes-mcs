# シグナル優先順位1–8 実装レビュー記録 (2026-09-24)

起点: `[MCS] レビュー候補 (med_change_no_followup)` が「同じ内容が2回」と
報告された案件。調査で配送二重ではなく**同一投稿由来の別薬剤シグナル**
（インスリン／在宅酸素、どちらもメッセージ 77363042）と判明し、さらに
抽出が「管理は出来ない」を `action:stop` に誤分類していたことが発覚。
ユーザー指示「優先順位1〜8をまとめて実装。コードをできるだけ共有化」に
基づく実装の記録。

## 検証した実データの事実（読み取り専用）

- open 中 `med_change_no_followup` の **45% (23/51) が薬剤師投稿由来**
  — 自分の報告へのフォローアップを自分に通知する構造的矛盾。
- 薬剤師宛の抽出依頼 370件、うち7日内応答確認なし 194件。
- `requests.to` 分布: 不明1,633 > 医師1,077 > 家族857 > 看護師593 >
  薬剤師370。医師宛の薬関連依頼 371件は薬局不可視の先行情報。
- 退院/転院言及137件のうち直近60日で薬変更共起なしは8件（量は小）。
- 「出来ない系」誤分類は change-action 言及1,894件中4件（稀だが信頼毀損）。
- `urgency:high` 570件は一般急性期報告が主 — 単独通知には不適。

## 実装（優先順位対応）

| # | 要求 | 実装 |
|---|---|---|
| 1 | 自己投稿抑制 | 自己同一性は **MCS `GET /users/self` から自動取得**（氏名・職種・所属施設 → `self_profile_v1` artifact、変化時のみ追記）。`signals.self_organizations`/`self_professions` は手動オーバーライド。`_self_author_pred` で検知時除外 — 既存 open は証拠消失で resolved（append-only のまま自然クリーンアップ）。自己投稿は `_self_post_exists` で応答者としても扱う |
| 2 | 薬剤師宛未応答 | `pharmacist_request_unanswered`。to が「薬」含有 or `request_targets`。窓 `request_response_days`(3d)。unverified 除外・依頼登録済み除外・応答者投稿で抑制。文言は「記録上の応答を確認できませんでした」 |
| 3 | 出来ない系誤分類 | `mcs_queries.MED_NOT_CAPABILITY_SQL`/`med_capability_evidence` を共有化 — med_change と `transition_cooccurrences`（統計側も）両方に適用。抽出プロンプトにも「能力・実施可否は action:none」と例3を追加 |
| 4 | 医師宛処方依頼 | `rx_request_visibility`。他職種宛（薬宛・不明を除く）で action が 処方/薬/内服/残薬/一包化 含有のみ — FYI 文言 |
| 5 | アドヒアランス | `adherence_concern`。meds の negated or capability evidence + 本文フレーズ（否定形「残薬はありません」は除外する tail-negation チェック付き） |
| 6 | 応答意味論 | `_self_post_exists`（職種 OR 自己組織の後続投稿）＋ `_request_registered`（依頼登録済み）を全応答系検知器で共有 |
| 7 | 階層化/digest | `SIGNAL_TIERS` + `sig_units`/`med_group_key` 共有。immediate: pharmacist_request/discharge/transition。他は digest — 未送信 digest intent に key 畳み込み、`next_try` 遅延（既定24h）。`urgency:high` は immediate 昇格＋送信文に「原投稿が urgency:high」表示 |
| 8 | 退院/症状 | `discharge_notice`（共起なしの裸退院 — 共起出現で resolved し transition へ引継ぎ）、`symptom_after_med_change`（同一投稿内結合のみ — room窓共起は量過多で却下） |

## 共有化したもの

- `mcs_queries`: `MED_NOT_CAPABILITY_SQL`・`MED_CAPABILITY_PATTERNS`・
  `med_capability_evidence` — シグナル側と統計側で同一 predicate。
- `mcs_signals`: `_self_sets`（自己同一性）、`_self_author_pred`（SQL 除外）、
  `_self_post_exists`・`_request_registered`（応答検出）、`_med_excludes`、
  `med_group_key`/`sig_units`（併合単位 — enqueue と send-time で共有）、
  `_urgency_high`、`SIGNAL_TIERS`/`_digest_add`。
- `notifier._signal_unit_text` — 単一/併合/ダイジェスト各ユニットの描画を一本化。
- `ledger._outbox_insert`/`outbox_add_tx` に `next_try`（digest 遅延）。

## 設計判断と根拠

- **検知時除外 vs 通知時抑制**: 自己投稿・capability・除外薬剤は検知時除外を
  採用。既存 open は append-only の「証拠消失→resolved」で自然解消され、
  本番の自己発火バックログ（23件）も設定投入だけで掃除される。
- **digest は pending payload merge**: 未送信 intent は id のみ保持で、送信時
  renderer が open メンバーだけを再グループ化して描画 — 解決済みメンバーは
  落ち、全員解決なら終端ドロップ（既存の fail-closed と同一意味論）。
  payload を途中改変するのは未送信 digest のみ（id 追加は安全）。
- **`to:不明` は rx_request_visibility の対象外**: 宛先不明の依頼を薬剤師宛と
  推測しない（精度優先）。薬剤師宛は「薬」含有 or 明示 `request_targets`。
- **symptom_after_med_change は同一投稿結合のみ**: room×時間窓の共起は実測で
  量過多と判明したため厳格化。
- **urgency は修飾子**: 570件の一般急性報告を薬剤師通知にしないため、単独
  シグナルにはせず immediate 昇格フラグとしてのみ使用。

## 設定（本番 config.json に投入済み — 施設名は実環境の値、記録上は仮名）

```json
"signals": {"notify": true,
            "self_organizations": ["〈所属施設名〉"],
            "self_professions": ["薬剤師"],
            "med_exclude_names": ["在宅酸素"]}
```

`tiers`・`digest:false`・`digest_interval_h`・`request_targets` は未設定
（既定値）。self_organizations/self_professions は `/users/self` 由来の
`self_profile_v1` artifact があればそちらが既定となり、config は
オーバーライドとして機能する（本番は両方を設定済み = 取得失敗時の
フォールバックでもある）。

## 残る限界（正直な記録）

- 「応答なし」には電話・次回訪問・次回処方で対応済みのものが含まれる —
  真の見落とし率は運用で評価する段階。文言はあくまで記録上の確認。
- `to:不明` 1,633件中の薬剤師宛は回収困難（宛名パターンの範囲のみ）。
- 別評価で開いた digest-tier 以外のメンバー併合は対象外（非digest pending
  intent の改変はしない方針を維持）。
- `sig` 検知器は例外時に型単位でスキップ（既存どおり）— 新規5検知器も
  同じ isolate 意味論に乗る。

## 追記: 実装レビューで修正した問題（同日）

- `_adherence_phrases`: 「残薬ありません」「飲み忘れていない」等の
  否定形が誤発火 → `_NEGATE_RE` に `ません`/`ていない`/`していない` を
  追加。「管理は出来ない」系（末尾の否定自体が懸念）は tail-check を
  行わない `ADHERENCE_TERMINAL` へ分離。phrase scan に SQL LIKE
  prefilter を追加し全件走査を解消
- `_urgency_high` が extract_llm のみ読み extract_v1 の
  urgency:high を見落としていた → 両 kind を確認（notifier._urgency
  と同じ意味論）
- 隔離（`outbox_hold` = state=failed + next_try NULL）された digest
  intent のメンバー喪失 → `_digest_add`/`_notify_suppressed` が
  `next_try IS NOT NULL` を要求するよう修正し、`_hold_event` が
  「送信未開始が証明できる」hold 時に open メンバーを新 digest intent
  へ救出（`rescue_digest_members`）。部分送信の可能性がある場合は
  救出しない（重複リスク優先）
- `_signal_unit_text` の併合不能フォールバックが先頭メンバーのみ
  描画していた → 全メンバー個別描画に変更
- digest intent は project_id=None で flush の archived ゲートを
  素通り → 送信時にメンバー単位で archived 判定を追加
- `to LIKE '%薬%'` がタスク的宛先（「薬の確認」）を薬剤師宛に誤認
  → `PHARM_TARGET_SQL`（薬剤師/薬局/調剤）に限定。visibility 側も
  鏡像否定で整合
- `_self_sets` が明示的な空リストと未設定を区別できなかった →
  config の list は空でも優先、artifact の空 professions も
  authoritative（プロフィール自体が無い時のみ薬剤師既定）
- `self_profile` の sender_id 型正規化（int/str 以外→None）
- `_adherence_concern` の meds 経路に status!='past' を追加

## 追記2: 未読通知の古日付抑制（同日）

一括追加される患者は過去分のチャットが「未読」として列挙される
ため、投稿から `notify_max_age_h` 時間以上経過した未読メッセージは
通知 intent を作らず取り込む（`save_patient(notify_max_age_s=)`）。
古いメッセージは `notified_at` を記録して消費済みにし、後の再
未取得で復活しない。投稿日時不明（posted_at_ts NULL）は古い証明
がないため従来通り通知する fail-open 設計。既読化・抽出・シグナル
評価は従来通り走る — 抑制されるのは Discord 通知だけ。
本番 config: `notify_max_age_h: 48`（未設定=従来通り全通知）。
`discover_archived` は既に有効済みだった。

## 追記3: 年齢フィルタを全通知経路へ適用（同日・レビュー指摘）

`notify_max_age_s` は当初 `save_patient`（未読 stage）にのみ適用
されていたが、`new_messages` intent は `save_messages`
（backfill、source="history"）と `save_thread_replies`
（reply_job）でも生成される。`fetch_history`/`_thread_page` も
`keep_read_status=1` で `is_unread` を保持するため、未読取得が
200件上限を超えた患者や、未読返信を持つスレッドの drain が
古い投稿を通知してしまう抜け道だった。`--jobs-only` run は
stage_unread を通らないため消費マークも間に合わない。

`_filter_notify_age` を共有化し3経路すべてに適用 — 古い投稿は
どの fetch が拾っても同じく「取り込み＋消費、通知なし」。
`run_reply_jobs`/`stage_backfill` へ `notify_max_age_s` を配管
（main() の config 解釈を jobs_only 分岐の外へ移動し、jobs-only
run にも有効化）。

## 追記4: hold 救出の全kind化 + 原子化（同日・多角レビュー指摘）

前回の digest 救出は `digest:true` payload のみ対象だったが、同一の
沈黙喪失は merged signal_keys intent・単一 signal_key intent・
`new_messages` intent（notified_at は intent 生成時に消費済みの
ため再通知経路が存在しない）にもあった。`rescue_digest_members`
を汎用化し signal 系は open メンバーのみ再エンキュー（非digestは
遅延なし・urgent 引継ぎ・単一なら signal_key 形に戻す）、
`new_messages` は同一 message_ids を再エンキュー。

救出と hold を同一 commit に統合（間でクラッシュすると旧intentと
救出intentが両方 live になり二重送信し得た）。rescue 生成物には
`rescue_of` マーカーを付与し、救出済み intent が再度 quarantine
されても再救出しない単発化 — 救出の無限連鎖を防ぐ。

`posted_at_ts=0`（migration が「解析不能」として扱う値）を年齢
フィルタで NULL と同じく fail-open 扱いに修正。
