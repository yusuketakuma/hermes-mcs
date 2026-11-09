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
| candidate scan 件数・同一 scope | pass 結果 `scanned`・`scan_ms`・（今回追加）`waiting`・`covered`・`scan_reads`・`scan_interrupts` | `scan_incomplete` は正常 slice 終了も含む。候補走査の中断（競合・期限/読取失敗等）は `scan_interrupts`、有限終了は区別して読む（推測で正常/障害を決めない） |
| snapshot 公開 age・失敗 | `snapshot_state`・`snapshot_failure`（`snapshots.last_failure()`）・`snapshot_timing`（`last_timing()` の copy_s/verify_s/publication_s/total_s） | 失敗理由は固定語彙のみ |
| 主要 KPI | 保存/採用された処理数・context 公開数／同条件 wallclock | raw call 数・tokens だけを成功にしない。NULL/観測中断は 0 にしない |

## 今回追加した最小計測（`_select_patient` の結果 stats・追加キーのみ）

- `waiting`：従来から数えていた待機候補数を結果へ出す（理由文は不変）。
- `covered`：候補全体を走査し切れたか。`waiting` と合わせて
  「全部見て待機」と「途中中断」を分ける。
- `scan_reads`：候補評価の `_pending` 呼出し試行数（論理計数）。
  `_pending` が内部で失敗した場合も試行として計上されるため、
  実際の stored-scan 読取り回数を直接示すものではない。
  実読取りの重複除去は `_current_scans` の呼出回数を数える spy
  テストが別途証明する。
- `scan_interrupts`：候補走査の中断（競合・期限/読取失敗等）の回数。
  `_ScanInterrupted` だけでなく SQLite/OSError・既知 bookmark
  ValueError の早期終了も計上する。有限 slice 正常終了は 0 のまま
  で、失敗の内訳は推測で分けない。

いずれも数値の追加のみで、本文・ID・例外文は含みません。
既存の status/reason の固定語彙と既存の受入判定は変えていません。

## B1（snapshot verification gate）の観測対応

適用範囲は run_check の `finish_snapshot` 段だけです（deliver 段・
mcs_ocr 等の他 caller は従来どおり run.lock を保持したまま公開）。
実装は `_valid_snapshot_candidate`（検証子プロセスの稼働期間）の
実行中だけ run.lock を解放し、Ledger 生成直後に捕捉した writer 基準
（DB path/inode）と run.lock fd/path・updater/restore・
publication fence を unlock 前と再取得後に照合してから公開へ進みます。
計画 §7 B1 の採用条件（安定 run.lock 規約照合済み・no-write/元例外
保持の合成検証）に対応する観測面は次の通りです。

| 観測 | 既存・追加の記録面 | 読み方 |
|---|---|---|
| 検証中の unlock | `_VerificationGate` が `unlocked_transport` を `_valid_snapshot_candidate` の期間だけ適用 | verify_s は unlock scope 内で採時 → 再取得時間を含まない（`last_timing().verify_s` で区別可能） |
| 再取得失敗 | `RunLockLost`（held: updater 介入／timeout: bounded wait 切れ）を `_SnapshotGateUnverified` へ束ねる → 結果 `snapshot_gate_unverified` + exit 1 | finish_snapshot 段では終端 write を遮断。他 transport の RunLockLost 契約は変えない |
| identity/fence 不一致・非有限 deadline | `_SnapshotGateUnverified` → 結果 `snapshot_gate_unverified` + exit 1 | finish_run は公開前に記録済み。finish_snapshot では health/outbox/実行記録の訂正書込みは行わない |
| 通常検証失敗 | `validator_false` 等の既存失敗語彙・既存 partial 処理 | gate の reacquire・identity が成功していれば `allowed` は True（通常部分失敗は変えない） |

`allowed` はローカルの共有フラグ（永続化なし・物理 held と分離）で、
有限 deadline・再取得・updater/restore・DB/runlock identity・
publication fence の確認がすべて成功したときだけ True になります。
`unchanged` で公開に至らず gate が一度も起動しなかった場合は
`attempted` が立たず、通常の partial/ok 処理を妨げません。
`verification_context` 未指定（`deadline=None` や run_check 以外の
caller）の経路は従来動作のままです。速度効果は合成検証のみで、
実機確認はしていません。
