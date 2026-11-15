# 患者コンテキスト作り直し計画（2026-11-15）

この文書は、所有者との相談（2026-11-15）で決めた、患者コンテキストとクラウド処理の作り直し計画です。
実装の進み具合は末尾の「実装と受入の現在地」に記録します。本文は決定事項の正本で、実装済みであることを示すものではありません。
実機の操作（配備・消去・作り直し・有効化）は、各段階の前に所有者の明示承認を得てから行います。この文書はそれらの承認になりません。

## 1. 目的

- 患者コンテキスト機能が正常に稼働すること。
- 途中の処理（中間の記録・送信経路・worker の構成）は作り直してよい。既存のエラー状態はすべて破棄して作り直す。
- 患者コンテキストを、システム内のほかの機能から使う入口を同時に実装する。
- リポジトリ全体から不要なコードを一掃する。

## 2. 現状の問題（2026-11-15 のコード調査）

1. 新しいクラウド送信は `patient_context` だけ（`cloud_policy.send_route`）だが、`reextract`・`semantic_backlog` の送信ユニット、
   並行比較、添付のクラウド段、`CloudBacklogPool` の旧 worker、`luna` の設定が過去記録の回復のためだけに残っている。
2. 会計・送信の状態は `artifacts` へ追記する JSON 行で持ち、毎回の走査で突き合わせる。走査上限（100000行・16MB）を超えると状態は `unknown` になる。
3. `CloudInferenceUnit` はコンストラクタだけで約300行のクロージャに責務が集中している。`cloud_policy.py` は1633行。
4. 価格表の再確認期限が約30日ごとのリリースと連動し、期限を過ぎると全送信が止まる（Anthropic は 2026-11-25 だった）。
5. 患者コンテキスト側ではプロンプトキャッシュを使っていない。クラウド worker の寿命はレーン2常駐に従属している。

## 3. 決定事項

| 項目 | 決定 |
|---|---|
| 処理の構成 | 患者コンテキストを作る処理を1本にまとめ、ローカルLLMは送信先の1つとして扱う。今のローカル専用経路は削除する。ローカルの枠の規則（昼：新着・対話1と背景2、夜：背景3、turn lock、admission）はそのまま引き継ぐ |
| 配備 | 完成・検証後に Mac の稼働版（`~/.mcs`）へ配備する。配備は改めて承認を得る |
| 消去の範囲 | 患者コンテキストの処理状態・調査結果・公開版・品質判定・生成枠と、`patient_context` route の送信・会計記録を全消去する。意味解析・添付OCRの過去のクラウド結果の根拠となる会計記録は残す |
| 使用額 | 消去時に、今日・今月の使用額の合計だけを引き継ぐ |
| 作り直し中の表示 | 公開中の患者コンテキストはすぐに全部非表示にし、作り直せた患者から順に表示を再開する |
| 予備表示 | 「基本のまとめ」の予備表示（`patient_summary_text`）は削除し、表示を患者コンテキストに一本化する |
| 費用と速度 | 期間予算を設定して短期で作り直す。作成回数の制限（1日4回・3時間間隔）は維持する。金額は直前の `cloud-llm estimate` を見て決める |
| 会計の保存形式 | 新テーブルは作らず、`artifacts` 上で論理要求1件を1レコードとして状態を書き換える |
| 互換コード | Mac で稼働中の版からの更新に必要なものだけ残す |

## 4. 新しい入口と機能

- **B1 通知カード**: 新着報告のカードに、その患者の「前回から変わったこと」等の要点を数行と、全体を開くボタンを付ける。Slack・Discord・LINE WORKS に対応し、Slack 画面例も再生成する。
- **B2 訪問前ノート**:
  - 宛先: 担当者ごとの個人メッセージ。担当候補はチャット参加者から出し、操作者が既存の人承認経路（receipt 付き）で承認した対応だけを使う。共有チャットには送らない。
  - 予定日: 担当者が登録した「薬局の次回訪問予定日」を優先し、無い患者だけ薬局の職員の投稿から読み取る。読み取った日は登録された日と区別して表示する。
  - 時刻: 予定日の朝8:00に届ける。ノートは夜の間に用意し、8:00は送信直前の原文整合の確認だけを行う。
  - 予定不明: 予定日を確認できない担当患者は、週に1回、担当者に一覧で知らせる。
- **B3 過去投稿の抽出し直し**: 過去の投稿を1回だけ抽出し直す。背景はルール抽出（`extract/rules/`）が拾った主病名・既往等を使う。患者コンテキストの作り直しより先に行う。
- **B5 新着の推論に患者コンテキストを使う**:
  - 使い始める条件（患者ごと）: ①公開中で品質確認に合格した版がある ②公開版の作成時点までに保存されていた資料の反映率（`source_coverage` の `included / observed`）が80%以上 ③直近30日の資料はすべて反映済み。
  - しきい値は設定で変えられるようにし、作り直し後に反映率ごとの患者数（本文なし）と背景あり・なしの比較を見て所有者と見直す。
  - 渡す内容: 主病名・既往・今の薬・アレルギー・直近の経過に絞り、上限を設ける。患者ごとの固定部を先頭に置いてローカルのプロンプトキャッシュを効かせる。
  - 対象: 新着投稿の構造化抽出と意味解析の両方（リアルタイム枠・背景枠）。条件を満たさない患者はルール抽出の背景を使う。
  - 背景は参考だけで、抽出結果の根拠は新着投稿の原文だけで検証する。公開版が失効していれば使わない。抽出結果ごとに使った背景の版・署名を残し、患者コンテキストが変わっても抽出し直さない。

## 5. 開発の段階

| 流れ | 段階 | 内容 |
|---|---|---|
| A | S0 | 価格表の更新と、期限7日前からの予告。作り直しを待たず先にリリースする |
| A | S1 | 判定基準：完全合成の入力と固定したスタブ応答で、最終産物・送信可否・安全上の不変条件・ローカル枠の規則を固定する |
| A | S2 | 新しい処理を1本で実装する。①計画（通信・DBなし）②要求記録 ③実行（provider差の吸収）④採用（照合と封印）⑤読取り（封印済み成果物を原文署名だけで検証）。候補指紋の列挙（`extract_llm_source_contract.py`・`semantic_candidate.py`）も更新する |
| A | S3 | 消去と作り直しのコマンド：人承認と receipt → バックアップ → 失敗コード別件数の記録 → `cloud-llm stop` と送信中要求の終了待ち → 使用額の引き継ぎ → 消去 |
| A | S4 | 仕様・ガイドの改定、`changes/*.json`、README 生成部分の更新 |
| B | B1〜B5 | 上記4章。B5 は A の読取り部分と B3 の背景受け渡しの後に着手する |
| C | C1〜C3 | 未使用コードの検査スクリプト、分野ごとの削除、互換コードの整理。安全ゲート・`ci/gates.py` の許可リスト・`ledger.py` 等の機械分割禁止・`deployment/` のパス表記は変えない |

## 6. 実機の手順（各段階の前に承認を得る）

1. 稼働中の版を確認する。
2. 新コードを配備する。Hermes gateway と LINE WORKS アダプターを再起動し、Hermes 経由と standalone の両方で動作を確認する。
3. バックアップを取り、失敗コード別の件数を記録して全消去する。
4. 過去の投稿を抽出し直す（ルール抽出の背景、ローカル、1回だけ）。この間は患者コンテキストを作れない。所要時間は配備前に件数から見積もる。
5. 患者コンテキストを作り直し、表示を再開する（期間予算）。完了後に `cloud-llm resume --confirm-human` で再開記録を作る。
6. 担当者を承認し、訪問前ノートの配信を始める。
7. 稼働を判定する：作成済み・部分作成・未作成の件数、失敗コード別件数、5回以上失敗した対象、費用、期間内完了を、3で記録した件数と比べる。

問題があれば `update_restore` で前の版に戻し、3のバックアップからDBを復元する。

## 7. 実装と受入の現在地

| 段階 | 状態 | 残り |
|---|---|---|
| S0 | Anthropic の料金表を 2026-11-15 に公式ページで再確認（料金は 2026-10-26 版と同じ）。新版 `anthropic-haiku-5-5-2026-11-15`（期限 2026-12-15）を追加し、旧版は過去の予約の照合用に保持。新しい送信に使う版は日付で選ぶ（`current_price_version`）。期限7日前からの予告を `setup check` に追加 | OpenAI の料金ページ（developers.openai.com）はこの開発環境から接続できず未確認。`gpt-6-luna`（2026-11-28）・`gpt-6.1-sol`（2026-11-30）の期限前に確認が必要。リリースは未実施 |
| C1 | `scripts/development/find_unused_code.py` を追加（標準ライブラリの AST による名前ベースの検査）。初回は68候補 | 同名の定義があると検出できない。削除後に再実行すると新たな候補が出る |
| S1 | ローカル経路の golden テスト（`tests/patients/test_patient_context_golden.py`・期待値 `tests/fixtures/patient_context_golden/local_three_posts.json`）。合成の3投稿から公開版と10の表示セクションまでを固定 | クラウド経路・安全上の不変条件・ローカル枠の規則の判定基準は未作成 |
| S2（第1段） | 起動されていなかった旧クラウド経路を削除：`cloud_backlog` の抽出・意味解析ループ、抽出・意味解析の `cloud_only` 選択・準備・実行、添付OCRのクラウド段、`cloud-llm plan` の reextract・semantic_backlog 見積り。理由コード語彙を `mcs/core/defer_reasons.py` へ移動 | 所有済み単位の回復経路（`_cloud_owned`）は、Mac 側で旧route の未精算記録が0件であることを確かめてから削除する |
| C2（第1弾） | 本番から参照されない定義を削除：`release_apply_owner` の休眠中の v1 native release 一式、`MCSAdapter` の未使用 fetch 3件、admission の未使用 reconcile、評価用 `compare_workflow` 一式、`semantic_relations.classify_pair` ほか。テストが本体の挙動を観測するために使っていた薄いラッパー（`stage_ledger`・`admission_status`・`llm_last_success`・`build_text`・`current_acknowledged`・`has_private`・`priority_key`・`failure_roots`）は、テストを本体のAPIかテスト用補助に移してから削除 | 対象外として残したもの：自動更新 v2/v3（`release_auto_*`・`adapters/sdc/*`・`release_owner_source.verify_target`・`Runtime.restart_execute`）、仕様が名指しする `semantic_conversion` のcohort関数・`effective_loop_state`・request-following、`run_log.enable_rotation`（保守テストの前提で、ローテーションの有効化経路の確認が必要）、`drug_info` の旧Exa経路と `bounded_http` の公開MCP例外（応答解析のテストを TinyFish 経路へ移してから削除する） |
| その他 | 未着手 | — |

検証の記録（2026-11-15、このクラウド開発環境・Python 3.14.6・pytest 9.2.0）：

- 実行して合格: `tests/llm` 全体（修正後に失敗2件を再検証して合格）、`tests/patients/test_patient_context_golden.py`（2回）、`tests/meta/test_find_unused_code.py`、ruff（変更ファイル）、`ci/mine_gates.py --check`、`update_readme.py`（再生成）。
- 実行したが変更前から失敗: `tests/ops`・`tests/meta` のインストーラ・リリース系（377件）。変更前のコミット `c55f4c4` の worktree でも同じテストが失敗することを抜き取りで確認した（Linux コンテナでは動かない系統）。`ci/gates.py` の `plugin_sandbox`（`adapters/sdc/telemetry.py` が subprocess を import）も変更前から失敗。
- 未実行: 全体 suite、`tests/semantic`・`tests/patients`・`tests/extract` の全体、CI の6ジョブ。

S2 第1段（旧クラウド経路の削除）の検証（2026-11-15、同じ環境）：

- 実行: `tests/` の全領域と `integration/` を領域ごとに順に実行した（同時実行なし）。失敗は、変更前のコミット `c55f4c4` の worktree で同じテストIDを流し直して比較した。
- 変更で生じた失敗は、削除した名前への monkeypatch、旧引数の偽関数、削除した経路を前提とするテストだった。修正・削除した後、該当テストの再実行で合格した。
- 変更前から失敗していて今回も残るもの: インストーラ・リリース系（Linux コンテナで動かない系統）、`tests/notify` の mcslog 系・性能時間系、`tests/patients`・`tests/views` の一部、`tests/semantic/test_semantic_cloud.py::test_closed_route_ambiguous_member_history_never_switches_local`、`tests/release/test_sync_release_notes.py::SyncTest::test_sync_command_failure_fails_ci`、`ci/gates.py` の `plugin_sandbox`。`tests/notify/test_card_delivery_and_view_regressions.py::test_health_write_failed_alert_is_sent` は1回失敗し、再実行と変更前の両方で合格した（不安定）。
- 合格: CI と同じ範囲の ruff、`update_readme.py --check`、`ci/mine_gates.py --check`、golden テスト。
- 未実行: CI の6ジョブ、Hermes 実SDKとの統合（CI の pinned 環境）。
