# ローカル処理改善の観測定義（2026-11-09・P0）

`LOCAL_PROCESSING_IMPROVEMENT_PLAN_20261108.md`（確定 `37513cfa`）の
P0 作業です。改善判定に使う観測項目を、既存の固定語彙・数値へ
対応付けて定義します。新しい観測基盤は作りません。

基準 commit：`e4c4ec09`（`ff038ef8` 系 source + task 26 受入差分。
task 26 は `rollup*`・`health_report`・`run_check` の 1 行を含み、
本計画の対象である患者コンテキスト経路とは重なりません。B1 が
`run_check` を触るため、この累積版を基準にします）。

## 観測項目と既存面

| 項目（計画 §5） | 既存の記録面 | 区分けの扱い |
|---|---|---|
| writer 待ち p50/p95/timeout | pass 記録の `llm_reacquire_s`/`jev_reacquire_s`、`run_lock_lost_detail.reacquire_s`、結果 `lock_lost`/`held_prior`/`carried` | 総和は壁時計占有率ではない。`held`（updater 介入）と `timeout`（bounded wait 切れ）を分けて読む |
| carry age / RT 待ち | realtime admission の carry・枠待ちの既存記録、pass 結果 `carried` | 並行時間の合算と壁時計を混同しない |
| first/repair 分離 | extract call 記録の first/repair 件数・`repair_wins`（nullable）・`checkpoint_reuses`・`repair_prompt_n` | issue 件数は request 数ではない。成功率は repair_wins が nullable なので導出しない |
| candidate scan 件数・同一 scope | pass 結果 `scanned`・`scan_ms`・（今回追加）`waiting`・`covered`・`scan_reads`・`scan_interrupts` | `scan_incomplete` は正常 slice 終了も含む。CAS/中断は `scan_interrupts`、有限終了は区別して読む（推測で正常/障害を決めない） |
| snapshot 公開 age・失敗 | `snapshot_state`・`snapshot_failure`（`snapshots.last_failure()`）・`snapshot_timing`（`last_timing()` の copy_s/verify_s/publication_s/total_s） | 失敗理由は固定語彙のみ |
| 主要 KPI | 保存/採用された処理数・context 公開数／同条件 wallclock | raw call 数・tokens だけを成功にしない。NULL/観測中断は 0 にしない |

## 今回追加した最小計測（`_select_patient` の結果 stats・追加キーのみ）

- `waiting`：従来から数えていた待機候補数を結果へ出す（理由文は不変）。
- `covered`：候補全体を走査し切れたか。`waiting` と合わせて
  「全部見て待機」と「途中中断」を分ける。
- `scan_reads`：候補評価が実際に行った stored-scan 読取り回数
  （`_pending` 1 回ごとの読取りを計数）。C1 の受入では、同 fixture の
  `_current_scans` 実行回数が `scan_reads` と一致し、候補ごとの
  重複読取りが 0 になることを見る。
- `scan_interrupts`：`_ScanInterrupted`（CAS 競合・走査打切り）の
  回数。slice 正常終了・SQLite/OSError とは分けるが、SQLite/OS 失敗を
  推測で中断扱いはしない。

いずれも数値の追加のみで、本文・ID・例外文は含みません。
既存の status/reason の固定語彙と既存の受入判定は変えていません。

## B1（snapshot verification gate）の観測対応

実装は `_valid_snapshot_candidate`（検証子プロセスの稼働期間）の
実行中だけ run.lock を解放し、再取得後に updater/restore・
DB inode・run.lock fd/path・publication fence を確認してから
公開へ進みます。計画 §7 B1 の採用条件（安定 run.lock 規約照合済み・
no-write/元例外保持の合成検証）に対応する観測面は次の通りです。

| 観測 | 既存・追加の記録面 | 読み方 |
|---|---|---|
| 検証中の unlock | `_VerificationGate` が `unlocked_transport` を `_valid_snapshot_candidate` の期間だけ適用 | verify_s は unlock scope 内で採時 → 再取得時間を含まない（`last_timing().verify_s` で区別可能） |
| 再取得失敗 | `RunLockLost`（held: updater 介入／timeout: bounded wait 切れ）→ 既存 `run_lock_lost` 経路 | `allowed` は False のまま → 終端 write 遮断 |
| identity/fence 不一致 | `_SnapshotGateUnverified` → 結果 `snapshot_gate_unverified` + exit 1 | finish_run は公開前に記録済み。health/outbox/実行記録の訂正書込みは行わない |
| 通常検証失敗 | `validator_false` 等の既存失敗語彙・既存 partial 処理 | gate の reacquire・identity が成功していれば `allowed` は True（通常部分失敗は変えない） |

`writes_allowed` はローカルの共有フラグ（永続化なし・物理 held と
分離）で、有限 deadline・再取得・updater/restore・DB/runlock
identity・publication fence の確認がすべて成功したときだけ True
になります。`unchanged` で公開に至らず gate が一度も起動しなかった
場合は `attempted` が立たず、通常の partial/ok 処理を妨げません。
`verification_context` 未指定（`deadline=None` や
run_check 以外の caller）の経路は従来動作のままです。
