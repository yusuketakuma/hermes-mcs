# 2026-09-23 継続記録

## AUDIT-J03 部分消化: batch.error の attempt 不消費

**発見**: `run_history_jobs` で `adapter.fetch_history` が walk 途中の
`MCSError` を `batch.error` として埋め込んで返す経路（`mcs_adapter.py`
`except MCSError: error=e; break`）は `job_defer(300)` で attempt を
消費しない。raise 経路の `MCSError` は `job_retry`（8回で failed）なのに
対し、埋め込み error は 300 秒間隔で同一ページを無限に再 walk し、
永続的失敗（削除済み project・権限喪失等）でも `failed` に到達しない
沈黙ループだった。

**修復**: `elif batch.error:` 分岐を `job_retry(job_id, 300)` に変更。
埋め込み `SessionExpired` は raise 経路と同様 attempt 非消費で
`job_defer` + 再 raise（auth 失敗は run 全体の中断であって job の
失敗ではない）。P-2（再 seed での revive）は `job_add` upsert の
`attempts=0` で維持。

**回帰**: `test_history_batch_error_consumes_attempts`（8回で failed
到達）、`test_history_batch_session_expired_stays_attempt_free`
（attempts=0 維持 + 再 raise）追加。

**残**: AUDIT-J03 の他項目（job 内停止の粒度、修復予約の網羅性、
旧 worker 状態変更の棚卸し）は未消化 — manifest では open 継続。

## AUDIT-J03 残領域: fetch_jobs retry/defer 全経路の静的監査

**範囲**: `job_ops.run_history_jobs` / `run_reply_jobs` / `run_discovery`
/ `seed_trickle`、`run_check.stage_unread` / `stage_backfill` /
`stage_attachments`、`semantic_drain` の drain ループ、および
`ledger.job_retry`/`job_defer`/`job_fail` のプリミティブ。

**結論**: batch.error 経路（前項で修復済み）以外に attempt 会計の
不整合は見つからなかった。各経路の判定:

- raised `MCSError` → `job_retry`（attempt 消費、8回で failed）: 正常
- invalid payload → `job_fail`: 即時可視化、正常
- `stalls >= HISTORY_STALL_LIMIT` → `job_fail`: 検証不能windowの
  可視失敗、正常
- `batch.reached` + reply pending → `job_defer(600*stalls, max 3600)`:
  attempt 非消費だが、配下 reply job は自身の attempt 上限で
  failed→pending 除外されるため、history job は必ず floor 判定へ
  収束する。正当な待機
- checkpoint `job_defer(0)`: 進行中 walk の途中経過保存。正常
- 埋め込み `SessionExpired` → attempt 非消費 defer + raise: auth
  失敗は job 失敗ではなく run 中断。raise 経路と一致、正常
- `run_reply_jobs`: fetch_thread MCSError / body_incomplete /
  not-in-got は全て `job_retry`。SessionExpired は raise。正常
- `run_discovery` / `seed_trickle`: 常駐 job の attempt 非消費は
  F7 設計（永久 pending で自己 reschedule）。write_failed /
  archived sweep 失敗も DISCOVERY_RETRY_S defer で収束。正常
- `stage_unread` / `stage_backfill`: fetch_jobs 非依存の
  ステートレススキャン。失敗は `fetch_state='incomplete'` /
  `result['errors']` に可視記録され、既読化・coverage 前進を
  ブロックする。沈黙ループなし。正常
- `semantic_drain`: token CAS 遷移 + `attempts >= limit` の早期
  failed close。例外・status='retry' は attempt 消費、
  status='failed' は max_attempts=1 で即終了。paused / mode=off /
  out-of-scope の defer は解除までの正当な待機。`status='stale'`
  は別 generation が所有するため無遷移が正しい。正常
- `attachment_failed`（ledger.py:1397）: max_attempts=6 で failed
  化。正常

**残**: ジョブ系 attempt 会計は全経路で健全と判定。AUDIT-J03 の
残項目はジョブ内停止粒度・修復予約網羅性の*実運用観察*（静态監査の
射程外）と EVAL-J06（人間ラベル評価）のみ。manifest では open 継続。
