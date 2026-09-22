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
