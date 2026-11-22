**通知を止めない不具合修正と内部コードの整理**

v1.2.1は、公開に失敗したスタンプ結果があとの通知を止めたり、過去投稿の編集が新着の通知まで抑制したりする不具合を直した版です。復元後照合の記録、統計の集計境界、独立実行の時計確認、OCR・CSDCの不具合も修正し、ゲートウェイ再起動中は未開始の定期処理が早めに譲るようになりました。再起動が必要な条件と、承認済み参照統計の再承認が必要な場合は「更新時の注意」にまとめています。

![v1.2.1の通知修正：公開に失敗したスタンプ結果は再試行時刻を持って順番待ちの後ろへ回り、過去投稿の編集は新着の通知を抑制しない説明図](https://github.com/yusuketakuma/hermes-mcs/releases/download/v1.2.1/notification-flow-v121.png)

### 新機能

- **ゲートウェイ再起動要求中は未開始の定期処理が早く譲る**
  ゲートウェイの再起動ヘルパーが動いている間、まだロックを取得していない定期処理（添付OCR・患者コンテキスト更新など）は最大60秒の待機を待たずに早めに終了し、再起動を妨げません。実行中の処理は中断されません。

- **自動公開の固定ポリシー・署名ピン・ツール実測記録を整備**
  GitHub Release への自動公開ワークフローが参照する公開ポリシー（リポジトリ・所有者・CIワークフローの実ID）、計画署名検証用の公開鍵ピン、公開実行環境のツールバージョン・ハッシュの実測記録を追加しました。公開はまだ実行されず、残りの資料と環境設定が揃うまでは安全側に停止します。

### 改善

- **wire parseの共通処理を一本化**
  利用者向けの実行時動作に変更はありません。auto更新v3契約で、parse_typedとparse_documentに重複していたdecode・JSON parse・ENC01検査を共通helperに整理しました。

- **auto契約の未使用定数を削除**
  利用者向けの実行時動作に変更はありません。auto更新v3契約検証コードで、どこからも参照されないモジュール定数を削除しました。

- **owner stateの未使用宣言を削除**
  利用者向けの実行時動作に変更はありません。v3 owner stateコードで、どこからも参照されない定数とprivate helperを削除しました。

- **apply契約の未使用hash_apply_reportを削除**
  利用者向けの実行時動作に変更はありません。apply契約検証コードで、どこからも呼ばれない公開ハッシュ関数を削除しました。

- **restart契約の未使用validateラッパーを削除**
  利用者向けの実行時動作に変更はありません。restart契約検証コードで、どこからも呼ばれない5つの公開ラッパー関数を削除しました。

- **telemetryの未使用_LEVELSを削除**
  利用者向けの実行時動作に変更はありません。telemetry収集コードで、どこからも参照されないprivate定数を削除しました。

### 不具合修正

- **負荷時に不安定だった通知性能ベンチの測定条件を修正**
  実時間の予算を持つ perf テストを通常テストの並列 shard から切り離し、Linux CI の slice 1 で直列実行にしました。件数・走査上限・公開成功だけを見る小規模テストは時間制限なしで全 run で実行します。

- **未精算結果の処理場所を記録し、上限変更後の回答再利用にモデルと effort を照合する**
  背景クラウド処理で、定期処理の外で保存・解放した未精算結果は `cloud_carry_settled` イベントに発生箇所（`trigger`: `loop` または `exit`）を記録するようになりました。出力上限の変更で送信の指紋が変わっても、プロンプト・モデル・effort が一致する精算済み回答は再利用されます。

- **復元照合の保留記録の残課題を修正**
  journal保持期間を超えた復元で保留された送信記録つきattemptを、journalの記録が無いだけの証拠で解除できなくなりました。`restore_reconcile.json`が再実行で解除待ちの保留を空欄にしなくなりました。

- **過去投稿の編集で同じスレッドの新着通知まで止まる問題を修正**
  初期取得した過去の投稿を編集すると、同じスレッドの別の正当な新着返信を含む解析ジョブ全体が「通知なし」になっていました。新着の到着記録と通知対象は残っているため、無関係な過去編集で新着の通知まで抑制される状態でした。修正後は、配送前の実到着イベントを持つジョブへ過去編集がマージされてもジョブ全体に通知なしが付かず、新着対象だけが通常どおり通知されます。編集された過去投稿自体はこれまでどおり通知されません。

- **承認レシートの読み取り失敗が「未承認」と誤表示される問題を修正**
  スナップショットの破損や旧スキーマで承認レシートの照会に失敗したとき、`mcs_refstats verify` が「承認されていない」と誤って表示することがありました。読み取り失敗は明示エラーとして返るようになりました。

- **macOS版テレメトリのos_versionを文字列で送信するよう修正**
  CSDCのmacOS版で、OS版番号としてmac_verのversioninfoタプルがそのまま入り、reportでJSON配列として送信されていました。release文字列を正しく選ぶよう修正し、契約どおり文字列のos_versionが送られます。

- **公開に失敗したスタンプ結果が後続の結果通知を詰まらせる問題を修正**
  確定・却下済みのスタンプ操作で結果の公開に失敗した行は、失敗するたびに60秒から倍々（上限1時間）の再試行時刻を持ち、その時刻までは順番待ちの後ろへ回ります。公開に失敗する行がたまっても、あとに確定した正常な操作の結果通知とカード再描画が止まらなくなりました。確定した操作結果や受付記録は公開失敗で書き換えられず、行の除外・削除・公開済み扱いも行いません。

- **時計巻戻り後の未来heartbeatをhostの正常な生存証拠にしない**
  独立実行モードの健康監視で、hostの状態記録の時刻が未来にあるとき「hostの時計を確認できていません」と警告し、正常とは判定しなくなりました。これまでは時計が巻き戻ったあと、プロセスが残っているだけで、止まった記録が未来時刻に追い付くまで正常と判定されることがありました。

- **until を as_of に切り詰めたとき as_of ちょうどの投稿が集計から外れる問題を修正**
  統計の `--until` に `as_of` より後の日付を指定すると、`as_of` と同じ秒の投稿が集計から外れ、`--until` を省略したときと結果が違うことがありました。切り詰めた境界は `as_of` を含むようになり、同じ凍結点の統計が再現できるようになりました。

### 更新時の注意

- 追加操作は不要です。

- 追加操作は不要です。Anthropic の用途別 profile 未設定で送信上限が 1600 トークンに留まる問題は v1.2.0 で修正済みです。

- 追加操作は不要です。再起動ヘルパーと定期処理の両方がこの版へ更新された環境で有効になります。

- gatewayと常駐の抽出プロセスの再起動が必要です。既存の保留の理由は書き換えず（遡及適用なし）、保持期間内の復元で記録された`unknown_attempt`の保留は従来どおり`no_journal_started`で解除できます。既存の解除操作・証拠形式・通知判定は変わりません。

- 追加操作は不要です。初期履歴の個別通知なしと明示replayの通知抑制の方針は変わりません。

- 追加操作は不要です。既存の未公開行は初回の選択時に未試行として扱われ、公開済みの行は再送されません。

- 追加操作は不要です。警告が出たら host の時計と時刻同期を確認してください。

- 抽出監査の承認済み参照統計は、範囲関数の定義が変わったため `definition_changed` と表示されることがあります。その場合は参照の再取得と再承認を行ってください。それ以外に追加操作は不要です。

### 技術詳細

<details>
<summary>技術詳細・根拠を表示</summary>

#### 負荷時に不安定だった通知性能ベンチの測定条件を修正

- 通常テストはすべての event で perf marker を deselect し、タグ・週次・手動実行では pytest-linux slice 1 の shard 完了後に直列で実行します。
- pull request では perf step を実行しません。
- perf の失敗や0件収集は従来の lint-test ゲートを失敗にします。証跡は slice の diagnostics artifact に残ります。
- 根拠: tests/notify/test_perf_cards.py、.github/workflows/ci.yml、scripts/development/ci_local.sh

#### 未精算結果の処理場所を記録し、上限変更後の回答再利用にモデルと effort を照合する

- `cloud_carry_settled` には `done`・`held_prior`・`deferred` の件数と、保留理由の固定コード（`defer_reasons`）、発生箇所（`trigger`: `loop` または `exit`）を記録します。メッセージ id などの個別情報は含みません。定期処理（run_pending）内で保存・解放された件数は従来どおり `cloud_backlog_pass.done` に含まれ、二重に数えません。
- 出力上限の変更は送信の封筒指紋を書き換えますが、プロンプト自体は同一です。精算済みの回答を論理指紋・モデル・effort が一致するときだけ再利用します。モデルや effort が変わった送信は再利用せず、従来の再送判定（検証済みの成功は別選択で再処理でき、不明瞭な送信は拒否）のままです。
- 戻すときは、このコミットを revert します。上限を変えて送信した後に戻しても記録は読めますが、用途別 profile を有効化した後に戻す場合は、その送信の記録が無いときに限ってください。
- 根拠: mcs/llm/cloud_policy.py、mcs/llm/cloud_backlog.py、mcs/llm/cloud_inference.py

#### ゲートウェイ再起動要求中は未開始の定期処理が早く譲る

- 再起動ヘルパーは起動時に data/gateway_restart_intent.json を発行し、終了時に自分のものだけ削除する。
- ロック待ちの定期処理は、有効期限内・スキーマ一致・ヘルパープロセスの生存と起動時刻が一致する意図ファイルを検出すると restart_intent_active として待機を打ち切る。
- 添付OCRはこの場合 reason=restart_yield で終了コード0を返す。意図ファイルが無い・不正・期限切れのときは従来どおり最大60秒待機する。
- 根拠: deployment/recovery/mcs_recovery_gateway.py、mcs/core/writer_lock.py、mcs/ops/mcs_ocr.py、mcs/patients/patient_context_refresh.py、mcs/patients/patient_context_quality_controller.py

#### 復元照合の保留記録の残課題を修正

- 保持期間を超えた復元で、送信記録が残るのにjournalに記録の無いattemptは`unknown_attempt:beyond_horizon`で保留され、`mark_not_sent`の`proof`は`remote_absent`か`api_rejected`に限られ、`no_journal_started`は`journal_cannot_vouch`で拒否されます。既存の`unknown_attempt`の保留は移行・書き換えせず、従来の解除経路のままです。
- `restore_reconcile.json`に、解除されていない全保留の`active_holds`（hold_id・attempt_id・delivery_id・reason・card_id・scope）、保留が閉じ込めている送信待ちの合計`active_events_held`、破損した保存記録の有無を示す`active_holds_incomplete`を追加しました。再実行で新しい保留が無くても未解除の保留と件数は同じ値で報告されます。
- 根拠: mcs/notify/notify_reconcile.py、mcs/notify/notify_recovery.py

#### 過去投稿の編集で同じスレッドの新着通知まで止まる問題を修正

- ジョブの通知可否は保存済みの new_messages 到着イベントで判定し、eligible フラグだけを通知許可にしません。到着イベントが消費済み・抑制済みのジョブへの過去編集マージは従来どおり通知なしを付けます。
- 対象ごとの通知判定（実到着イベントの被覆）に任せるため、新しい通知抑制状態は追加していません。
- 根拠: mcs/core/ledger_semantic_sources.py、tests/semantic/test_mcs_semantic_source_jobs.py

#### 承認レシートの読み取り失敗が「未承認」と誤表示される問題を修正

- 承認レシート照会で発生した例外をこれまで「承認記録なし」と同一扱いしていました。照会の失敗は `main()` の既存の明示エラー経路（sqlite3.Error → safe_error_code）に届くようになり、承認済みの参照統計が誤って未承認と表示されることはありません。レシート行が存在しない場合だけを未承認として扱います。
- 根拠: mcs/ops/mcs_refstats.py、tests/ops/test_mcs_refstats.py

#### 自動公開の固定ポリシー・署名ピン・ツール実測記録を整備

- deployment/release-publisher/policy.json にリポジトリ・所有者・CIワークフローの実IDを固定しました。
- deployment/release-publisher/publisher-toolchain.lock に GitHub 実行環境（ubuntu-latest・Python 3.14）の git・gh・python の実測値を記録しました。
- deployment/release-publisher/pins.json に publication-plan / release-acceptance 署名検証用の RSA-3072 公開鍵ピンを追加しました（kid=81ac02bd…、対応する秘密鍵はorgシークレット MCS_PUBLISH_PLAN_SIGNING_PRIVATE_KEY に保存）。
- 実測値を取り直す手動ワークフロー publisher-toolchain-measure を追加しました。
- 根拠: deployment/release-publisher/、.github/workflows/publisher-toolchain-measure.yml、deployment/release-publisher/pins.json

#### wire parseの共通処理を一本化

- 共通処理を_parse_and_scanへ集約し、parse_typed・parse_documentは上限検査とdefinition照合だけを担います。
- 根拠: adapters/sdc/release_auto_contract.py

#### auto契約の未使用定数を削除

- AUTO_PAYLOAD_BYTES・AUTO_ERROR_SCHEMA・AUTO_PURPOSESを削除。purpose一覧は利用中のAUTO_PURPOSE_DEFINITIONSが担います。規約interfaceの例外型HoldError・PersistenceUnknownはcontracts/mcs-auto/v3/interfaces.pyiが宣言するため保持しました。
- 根拠: adapters/sdc/release_auto_contract.py

#### owner stateの未使用宣言を削除

- INTENT_SCHEMA・MIGRATION_NAME・_ref_keyを削除。'mcs.start-intent.v3'はschema JSON側のconstが正本で、'release-migration-v3.json'はrelease_auto_migration.NAMEとstorage.pyの一覧が担います。
- 根拠: adapters/sdc/release_auto_state.py

#### apply契約の未使用hash_apply_reportを削除

- hash_apply_reportを削除。hash_apply_intent・hash_apply_approval・hash_apply_payload・hash_apply_preview・hash_apply_local_receipt・hash_apply_state・hash_apply_change_bindingは既存の呼出元に利用されているため保持しました。
- 根拠: adapters/sdc/release_apply_contract.py

#### restart契約の未使用validateラッパーを削除

- validate_restart_intent・validate_restart_preview・validate_restart_operation・validate_restart_grant・validate_restart_local_receiptを削除。全kindの検証は既存のvalidate_restart_recordディスパッチが担い、利用中のapproval・registration・acceptanceラッパーは保持しました。
- 根拠: adapters/sdc/restart_contract.py

#### telemetryの未使用_LEVELSを削除

- _LEVELSを削除。log entryのlevelは既存の_log_levelがerror・warning・infoだけを返すため、このallowlistは参照されていませんでした。
- 根拠: adapters/sdc/telemetry.py

#### macOS版テレメトリのos_versionを文字列で送信するよう修正

- platform.mac_ver()の第1要素releaseをos_name判定とos_versionに使います。
- 取得不可・非文字列の値は値として入れず、型・長さ・改行の既存条件を維持します。
- 根拠: adapters/sdc/telemetry.py

#### 公開に失敗したスタンプ結果が後続の結果通知を詰まらせる問題を修正

- drain の公開段階は、次回試行時刻（未設定なら作成時刻）に達した未公開の確定行を、試行時刻と artifact_id の順で従来どおり最大4件選びます。失敗した行は次回時刻が先へ進むため、後続の行に処理が回ります。
- 公開試行の前に試行回数と次回時刻を短いDBトランザクションで保存し、そのあと既存の原子的ファイル公開を行います。公開成功後だけ result_published が立ち、ファイル公開と完了記録の間で中断しても期限到来後に同じ確定結果を再公開して復旧します。
- 収集結果の stamps に診断として publish_pending（期限未到来を含む未公開の確定行数）、publish_oldest_wait_s（最古の未公開行の待ち秒数）、publish_failed（その drain の公開失敗数）、publish_retry_repaired（補正した再試行情報の行数）を追加しました。
- 再試行情報の破損（型不正・負数・非有限値・上限を超えた将来時刻・SQLite の整数上限に達した試行回数）は正常値に補正します。次回時刻の破損は即時対象にし、試行回数の破損は保存された次回時刻を待ってから再試行します。
- 再描画の失敗は公開失敗と区別し、復旧は既存の spool 復旧に委ねます。
- 根拠: mcs/stamps/stamp_write.py、tests/stamps/test_stamp_write.py、tests/stamps/test_stamp_restore_safety.py

#### 時計巻戻り後の未来heartbeatをhostの正常な生存証拠にしない

- 記録の更新時刻が許容誤差（2秒、mcs_standalone の状態検証と同じ幅）を超えて未来にある場合は「hostの時計を確認できていません」として異常扱いします。プロセスの消失は時計に依存しない証拠なので、あわせて確認できる場合は `standalone_host_down` も併記します。
- 更新時刻は現在でも、接続の失敗開始・更新開始・収集の最終成功・起動時刻が未来にある場合は、その観測を期限の判断にも正常の根拠にもせず、「hostの時計を確認できていません」として degraded にします。
- 既存の子プロセス異常・接続断・更新停滞・収集停滞の監視、保留と復旧の権限は変わりません。強制再起動は追加しません。
- 根拠: mcs/ingest/health_watch.py、#185

#### until を as_of に切り詰めたとき as_of ちょうどの投稿が集計から外れる問題を修正

- until を as_of に切り詰めた記録を scope に持ち、その場合は as_of と同じく until の時刻を含む境界で絞ります。明示的に指定された until はこれまでどおり半開区間（指定時刻を含まない）です。退院・転帰と薬剤変更の共起集計（mcs_stats の±14日窓）も同じ基準に揃えました。
- 根拠: mcs/views/stats_contract.py、mcs/views/mcs_stats.py、tests/views/test_mcs_stats.py

</details>
