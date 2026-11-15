# 新しいクラウド送信の土台（S2 第1段）設計（2026-11-15）

[作り直し計画](PATIENT_CONTEXT_REBUILD_PLAN_20261115.md) の S2 第1段の設計です。最新の `main`（`883464c`）のコードを読んで作成しました。
所有者の決定に従い、まず患者コンテキストだけを新しい土台に載せ、稼働を確かめてから、過去投稿の再抽出・意味解析の積み残しも同じ土台へ移します。
判断が必要な点は末尾の「未決事項」にまとめています。決まるまでは、そこに書いた推奨案で設計しています。

## 1. 方針

- 患者コンテキスト専用の新しい送信ユニット `CloudSendUnit` を作る。既存の `CloudInferenceUnit`（`mcs/llm/cloud_inference.py:124`）は、再抽出・意味解析・添付OCRのために第2段まで残す。
- 最終産物（公開版・表示・使用額・`cloud-llm plan/estimate/log/compare/outstanding` の出力・安全上の決まり）は変えない。判定基準は `tests/patients/test_patient_context_golden.py`・`test_patient_context_golden_cloud.py`・`test_patient_context_cloud_invariants.py`。
- 新しいテーブルは作らない。論理要求1件を `artifacts` の1行（kind `cloud_request_v1`）として持ち、状態を書き換える。
- 既存の追記行 `cloud_usage_reserve/result/disposition` は、今と同じ関数・同じ内容で書き続ける（二重書き込み）。理由：
  - `cloud-llm log` が行ごとに出力する（`cloud_cli.py:589-604`）。
  - 使用額・予算・outstanding・estimate がこれらの行を直接読む（`cloud_policy.py:1053-1056, 1189-1214`、`cloud_observation.py:97-133`、`cloud_cli.py:341-385`）。
  - 公開済み成果物の照合値は、同じ予約に属する行全体のハッシュ（`cloud_usage_accounting.py:553-554`）。
- 要求記録は「状態と冪等キーの索引」、追記行は「金額と証拠の正本」とする。判断では記録を引き、参照先の行を既存の検証関数で確かめる。食い違えば保留にする（送信もローカル代替もしない）。

## 2. 現状の責務（患者コンテキストが使うもの）

| 関心事 | 現在の場所 |
|---|---|
| profile 解決・選択の固定 | `cloud_inference.py:131-142`、`cloud_policy.py:354-412` |
| 試行ごとの再判定（設定の再読込・範囲・updater・鍵・上限の引き下げ・期間・停止・回路・価格） | `cloud_inference.py:174-243` |
| 予約 | `cloud_inference.py:263-280` → `cloud_policy.usage_reserver`（1544-1605） |
| 結果の保存・範囲喪失時の処分 | `cloud_inference.py:281-309` → `record_result`（1686-1801） |
| 破棄 | `cloud_inference.py:321-337` → `record_disposition_tx`（1804-1823） |
| 保存済み回答の回復 | `cloud_inference.py:375-405` → `recover_response`（1627-1683） |
| 送信前の履歴確認 | `cloud_inference.py:407-426` → `request_send_block_reason`（1515-1541）→ `_guard_repeated_request`（847-953） |
| 同時実行枠 | `cloud_backlog.request_slot`（151-180） |
| 試行ループ・誤り分類・通信 | `cloud_client.py:72-182`、`providers/*.py`、`bounded_http` |
| ローカル代替の安全確認 | `guard_context_local`（`cloud_inference.py:457-474`）→ `guard_local_fallback`（`cloud_policy.py:1025-1034`） |

患者コンテキストが使わないもの：checkpoint fence（`dispatch_current`）、transport の差替え、`cache_heads`、品質修正の子ユニット、同じパスでの差替え、`owns_*`。

現行の挙動として写すもの：
- 5xx の再試行は事実上起きない。2回目の予約が `repeat_unsettled` で拒否されるため（`cloud_policy.py:941-944`）。再予約できるのは確認済みの拒否（not_sent・429・OpenAI の quota 系、`cloud_policy.py:1608-1612`）だけ。
- 1回の呼出し・1試行で `load_config` を約10回読み、guard 1回ごとに `resolve_settings` が3回走って鍵ファイルを読む。新しい土台では3回（採用時の確認を含めて4回）にする。

## 3. モジュール構成

| モジュール | 責務 | 主な公開関数 |
|---|---|---|
| `mcs/llm/admission/cloud_send_admission.py` | 例外3種（`CloudFallback`・`CloudBusy`・`CloudObservationIncomplete`。`cloud_inference` から同じオブジェクトを再公開する）、試行ごとの設定スナップショット、試行の可否判断 | `snapshot()`・`check_attempt()`・`check_scope()`・`caps()`・`tightened()` |
| `mcs/llm/cloud_request_plan.py` | 計画（通信・DBなし）。要求本文・指紋・予約 meta を作る | `plan_request()`・`reservation_meta()` |
| `mcs/llm/cloud_request_record.py` | 要求記録の状態遷移、送信可否とローカル代替の判断、各トランザクション | `decide_send()`・`decide_local()`・`reserve_tx()`・`mark_sent_tx()`・`settle()`・`discard_tx()`・`saved_answer()`・`prune_tx()` |
| `mcs/llm/cloud_send.py` | 実行。`CloudClient` への配線、同時実行枠、例外の対応付け | `CloudSendUnit`・`guard_local()` |
| `mcs/llm/cloud_adoption.py` | 採用。受領の再確認、採用と破棄 | `receipts()`・`adopt_tx()`・`commit_check()` |

`CloudSendUnit` の属性・例外・例外メッセージは、患者コンテキストが今使っている形（`patient_context_refresh.py:876-955`、`patient_context_quality_controller.py:177-193, 406-423`）と同じにする。
通信部分は再利用し、`CloudClient.complete` に省略可能な `before_send=None` だけを足す。provider と `bounded_http` は変えない（no-redirect/no-proxy を維持）。

設定を読むのは、1回の呼出しにつき次の3回とする。
1. 呼出し開始時（計画・送信可否・予約の判断に使う）
2. POST の直前（予約の待ちの間に上限が下がったかを比べるだけ）
3. 応答の後（範囲の喪失を確かめ、呼出し末尾の確認にも使う）

## 4. 要求記録（kind `cloud_request_v1`）

- project_id・message_id は予約行と同じ値にし、既存の索引 `idx_artifacts_lookup` で引く。新しい索引は作らない。
- `CLOUD_USAGE_KINDS`（`ledger_schema.py:661`）には加えない。照合値・tally・log・outstanding の計算に入らない。
- meta（作成後は不変）：`route`・`purpose`・`unit_key`・`request_key`（回復用の厳密なキー）・`logical_key`（再送防止用）・`local_key`（要約のローカル代替用）・`request_fp`・`input_binding`・`selection`・`owner_fp`。
- content（CAS で更新）：`state`（reserved / possibly_sent / settled / adopted / discarded）・`outcome`（answered / rejected / failed / unknown / not_sent）・`attempts`（予約行・結果行の対応。上限16件、超えたら保留）・`disposition`・`adopted_by`。

| 遷移 | 書くトランザクション |
|---|---|
| なし → reserved | 予約 tx（予約行の INSERT と同じ tx。品質修正の確認があるため、記録はフックの後に書く） |
| reserved → possibly_sent | POST の直前の短い CAS（外側に tx が開いていれば not_sent） |
| reserved → settled(not_sent) | POST 前の確認失敗・期限切れ |
| possibly_sent → settled(outcome) | `record_result` と同じ tx。範囲を失っていれば discarded |
| settled(rejected / not_sent) → reserved | 次の試行の予約 tx |
| settled(answered) → adopted | 呼出元の公開 tx の中 |
| settled → discarded | discard tx（`record_disposition_tx` の行も追記） |

判断の規則：
- 同じ `logical_key` に未決着・failed・discarded の記録があれば保留。
- answered で同じ `request_key` なら、新しい予約なしで回復する。
- 記録に結び付かない patient_context の予約行があれば保留する。
- ローカル代替は、記録がすべて rejected / not_sent か、usage なしの HTTP 4xx/5xx のときだけ許す。
- 保持期間の削除（`maintenance._prune_cloud_usage`）は、同じ tx の中で、参照する予約行がすべて消えた記録だけを消す。

## 5. 移行手順（小さなコミット）

1. 例外と純関数を `cloud_send_admission.py` へ移す（挙動は変えない）。候補指紋の列挙（`extract_llm_source_contract._LOADED_SOURCE_PATHS`・`semantic_candidate`）・`tests/semantic/test_split_source_contract.py`・`tests/meta/test_module_layout.py`・README 生成部分を更新する。
2. 計画とスナップショット。予約 meta と本文が現行と一致すること、設定を1回しか読まないことをテストで固定する。
3. `record_result`・`usage_reserver`・`record_disposition_tx` を「検証して行を作る」「tx の中で書く」に分ける（挙動は変えない）。
4. 要求記録と保持期間の削除。旧ユニットと新ユニットで `cloud_cli.observe()` と `cloud_observation.observe()` の結果が一致することを確かめる。
5. `CloudSendUnit` と採用（まだ呼ばない）。既存の安全契約（停止・結果不明の再送防止・上限の引き下げ・範囲の取消し・価格の期限・保存済み回答の再利用・ローカル代替の条件・reserved のまま残った要求の保留）を新しいユニットで確かめる。
6. `patient_context_refresh` を新しいユニットに替える。判定基準3本が変わらないこと、`tests/patients` 全体。
7. 品質修正の2経路（`patient_context_quality_controller.py:177, 406`、`patient_context_quality_receipts.py:612-636`）を替える。
8. 公開 tx の中で adopted を書く。
9. 後片付け：旧 API への参照が患者コンテキスト側に残っていないことを確かめ、残りの不変条件（上限の引き下げ・範囲の取消し）を判定基準に足し、`changes/*.json` と仕様を更新する。

内部構造に依存していて書き換えが必要な患者テストは、`tests/patients/` の `anthropic_context_testkit.py`・`test_context_cloud_update_identity.py`（fixture のみ）・`test_anthropic_context_refresh.py`・`test_anthropic_context_repairs.py`・`test_context_completed_carry.py`・`test_context_midnight_cloud_owner.py`・`test_context_empty_overview.py`・`test_keyless_context_paid_history.py`・`test_optional_patient_quality.py`・`test_patient_cloud_entry_resident.py`・`test_patient_context_generation_limits.py`・`test_patient_summary_quality.py`・`test_patient_summary_scheduling.py`。

## 6. リスク

- 二重書き込みは同じ tx の中で行うので原子的。記録の誤りが送信の許可につながらないよう、行を正本として検証する。
- POST 前の CAS で書込みが1回増える。競合や busy のときは not_sent で精算し、送信しない。
- 候補指紋の列挙が変わるため、配備後に品質判定が一巡やり直しになる（通常のリリースと同じ）。
- 要確認（現行の挙動、この段では変えない）：保持期間の固定対象は原典修復と添付OCRの行だけで（`maintenance.py:1077-1256`）、患者コンテキストの scan の受領行は62日で消える。その後に `guard_cloud_sources`（`patient_context_refresh_sources.py:367-401`）が通らなくなる可能性がある。

## 7. 決定事項と残りの未決事項

所有者の決定（2026-11-15）：

| 番号 | 論点 | 決定 |
|---|---|---|
| 1 | 記録のない patient_context の旧予約行 | 保留にし、S3 の全消去で片付ける。配備と消去は続けて行う |
| 2 | reserved のまま残った要求 | 条件付きで not_sent に戻す（下記） |
| 3 | adopted を書く時点 | 公開 tx の中で書く |

番号2の条件：POST の直前に記録を reserved から possibly_sent へ CAS で書き換え、これが成功したときだけ POST する。
したがって reserved のまま残っている記録は、POST が始まっていないことの証明になる。
担当の期限（記録に保存する lease）が切れた reserved の記録は、settled(not_sent) へ CAS で書き換え、予約額を0として精算する。
元の担当がその後に possibly_sent への CAS を試みても失敗するため、送信は起きない。possibly_sent 以降の記録は戻さず、保留する。

推奨案のまま進めるもの：新しい索引は作らない（4）、記録は予約行と一緒に消して保守の件数に数えない（5）、第1段は二重書き込みで進め、記録を正本にするかは第3段で判断する（6）、S3 の使用額の引き継ぎの表し方は S3 の設計で決める（7）。
