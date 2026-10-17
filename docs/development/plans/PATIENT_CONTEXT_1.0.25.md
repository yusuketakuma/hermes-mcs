# 1.0.25 患者コンテキスト v3 実装・受入計画

基準: v1.0.24 / 35b5a553。ユーザー提示の2026-10-17付v3仕様書2点を正本として以下を全件追跡する。第二入力源は同日の追加指示で今回実装せず、将来の正式接続・権限・人確認した患者リンク・転載抑制・取消伝播を必要条件として残す。単一MCSの機能を先に完成させる。 後続のユーザー指示「既定で有効にする」により、夜間生成・限定閲覧は省略時ともにtrueへ変更する。既存環境にも適用し、明示falseと不正設定時の無効化は維持する。

既存の抽出・引用検査・artifacts・読取り・LLM admission・slot2 workerを利用する。新しいDBテーブル、常駐プロセス、依存、汎用フレームワークは追加しない。夜間の永続化・再開を一つの実装に集約し、各画面は共通根拠から投影する。コード増加を抑えるが受入条件は削らない。

| ID | 必須要件 | 主な実装/証拠（実装後に結果を追記） |
|---|---|---|
| N1 | 23:00以上03:30未満JST・開始側の日付night_id・絶対deadline・04:00非競合 | `patient_nightly.night_window/_ask/run_due`、`test_night_window`・`test_insufficient_time_never_starts_inference`・`test_crossing_end_during_inference_discards_unfinished_publication` |
| N2 | A差分/B過去走査/C原文点検、余力60/30/10、公平性、初回・cursor・backoff・翌夜再開 | `run_due/_pending/_current_scans`、`test_patients_take_turns_and_incomplete_records_not_counted_analyzed`・`test_chunk_checkpoint_survives_next_night_without_repeating_prefix`・投稿別backoffテスト |
| N3 | slot2のみ、リアルタイム・抽出・semantic/QC・elastic hold・busy・update・disk/memory優先 | `extract_llm._background_nightly`、`tests/extract/test_nightly_worker.py`、実送信本文のslot固定テスト `test_unbrokered_nightly_request_sends_pinned_slot` |
| N4 | 完全な投稿だけ処理、長文を小区間、既存抽出を先に使い不完全/未分類だけ追加LLM | `_sources/_scan` と既存 `plan_chunks`、長文checkpoint・不完全出力・未知本文・選択投稿だけのhydrateテスト |
| N5 | 新着なしの追加発見、追加なしなら文章不変、全点検済みならLLMゼロ | `test_completed_work_does_not_repeat_llm_next_night`・`test_old_unlabelled_report_is_discovered_without_new_message`・`test_fully_extracted_lines_skip_extra_scan_and_duplicate_summary` |
| N6 | 個別の古い背景を最新分類の別話題で消さない、19分類32属性を保持 | `_all_facts/nightly_view_input`、古い別項目保持・256件選択外の詳細保持・80件超の背景表示テスト |
| N7 | 原文・主体・否定・予定・条件・時点・測定日を保持。要約の逆意味/無根拠拒否 | `_facts/_validated_facts`、家族・否定・予定を含む段落保持、投稿日時再検証、4画面の原文表示回帰、抽出の過去測定値・同一投稿検査値テスト |
| N8 | source_sigに本文/抽出世代/取得状態/患者対応/閲覧範囲/model/prompt。通常再取得時刻は除外 | `_source_sig/_snapshot/_contract` の本文hash・抽出内容/世代・患者scope・モデル拘束、通常再取得不変・削除/名寄せ変更の回帰 |
| N9 | LLM前入力固定、推論中DB書込みtxnなし、保存直前再照合、変更時非公開、途中非公開 | 推論時txn不保持をfixtureで検証、入力変更・別接続の同時commit・期限切れ・タイムアウト再開テスト |
| N10 | artifactsのみ、上限付き進捗、日中rollup不変、patient_state_context既定off/自動注入なし | `_replace` は既存artifactsのみ。karte_summaryはNULLの患者単位SCAN。既存rollupとstate_contextを分離、`test_linked_summary_checkpoint_has_no_fake_message_reference` |
| N11 | as_ofと未反映新着、削除・改訂・対応取消・権限失効で旧文章抑制 | `nightly_view_input`、変更/削除/名寄せ取消・権限失効・新着未反映・公開off・破損文章の抑制テスト |
| N12 | 取得範囲/解析範囲/項目根拠充足/独立人手評価を別指標、未取得・添付未解析明記 | `_coverage` と本人向け表示、unknown本文の分母・添付未解析・人手精度None・取得範囲の保持を確認 |
| U1 | 患者背景と最近の変化、前回確認以降の新着/編集/遡及/追加発見の区別 | `evidence/background/changes`、4種の発見区分と削除観測時点、利用者別checkpoint・同一操作再送・全8画面の認可テスト |
| U2 | 質問・依頼・返答候補、正式対応完了や未対応を推測しない | `replies` と既存 `current_cached_refs`、`test_replies_never_claim_untreated_or_formal_completion`・削除済み依頼除外 |
| U3 | 訪問前の必要な確認事項・既存予定（追加指示により一律3件上限なし）、診療指示は生成しない | `previsit`、上限なし/投稿重複排除・削除表示・否定/予定/家族の原文表示テスト |
| U4 | 薬/症状/ADL/栄養/機器/入退院の時系列、出来事と投稿時刻、因果なし | `timeline/_mentions`、薬・症状・出来事日・投稿時刻の保持、本人向け画面でも原文修飾を保持 |
| U5 | 記載の相違と時点・条件併記、別時点を矛盾と断定/採決しない | `discrepancies`、同じ項目/単位/条件だけ比較し、時点不明・別時点を区別する合成テスト |
| U6 | 職種による欄順のみ、閲覧権限は既存境界を維持 | `role_order`、内容集合不変テスト、actor違い拒否・本人向け表示の接続先回帰 |
| U7 | 引継ぎ用記録、確認範囲と残事項、正式担当・予定を自動確定しない | `handoff` は共通根拠の確認範囲/原文確認事項/返答未発見を投影。全セクション到達テスト、正式依頼状態を更新する経路なし |
| U8 | 原文根拠付き患者質問検索、取得範囲、原文外回答なし | `patient_search_view/_question_term` に限定質問の検索語変換、投稿番号/日時/原文/取得範囲。`tests/notify/test_patient_question_search.py` と既存検索回帰 |
| U9 | 退院・移行時の経過と情報不足、正式処方と区別 | `transition_course`、入退院・移行の原文を中心に前後14日の投稿と項目の記載不足を表示。移行/原文保持テスト |
| O1 | enabled/publication省略時true（既存環境にも適用）、明示false尊重、不正値false、型検証、doctor状態、明示offで既存処理不変 | `options`・setup/doctor・worker、`tests/ops/test_nightly_setup.py` と省略/false/不正値/実データ非probeのテスト |
| O2 | R0合成評価、R1私有snapshot/R2本番shadow/R3限定閲覧の操作と判定条件 | `docs/specs/patient-context.md` のR0〜R3。R0は合成テスト、R1/R2実測と臨床精度評価は未実施・別の運用許可が必要 |
| F1 | 第2入力源と複数チャット整理 | 明示指示による将来対応。正式接続・人確認リンク・権限・取消伝播・転載抑制の前提を仕様に記録 |

## 実施範囲と検証

本番DB・設定・スケジュールの変更、実データの外部送信、push・リリース公開・本番配備はこの実装作業では実施しない。R1/R2の実測や臨床精度を合成テストで代用したとは主張しない。私有snapshot評価・shadow運用の入口と手順を完成させ、実行にはそれぞれの運用者の許可を使う。

必須: 仕様の12受入項目の合成回帰、既存関連回帰、静的ゲート/README生成整合、固定差分CodeRabbit最大2回、Claudeによる独立coreレビュー、親の要件別完了監査。既存成功結果は同一コードなら再利用する。

## 完了確認（2026-10-17）

24項目を上表の実装・回帰テスト・運用手順に照合した。第2入力源は明示指示どおり将来対応。実装と合成検証は完了し、本番DB・設定・スケジュールは変更していない。変更は develop/v1.0.25 の未コミット差分として保持する。

- 全体検証: `tests/` と `integration/` を領域別に逐次実行。14,116件のうち初回14,057件成功・57件失敗・専用SDK laneの2件skip。57件は修正・固定後の再検証で全件解消した。helperだけのfixtures/supportディレクトリは収集0件であり、テスト失敗とは区別する。
- 失敗解消: ソース変更検知19件は編集終了後に19件成功。未適用の合成updaterが終了コード2を返す新契約にテストを合わせ、plan/applyの76件成功。ガードや本番コードを弱めて成功させていない。
- 最終差分: LINE WORKS配送中callback・停止処理、夜間slot送信本文、抽出時点/検査値、キュー順序、質問検索、原文/削除表示の538件成功。既存成功結果と修正後の影響範囲の結果を合わせて確認する。
- SDK: 固定Hermes v0.22.2 companion `b7758d8555931846f4b8f798d4ea9715f65077ca` の26件、standalone12件成功。通常suiteの2件skipを実SDK未検証のまま扱わない。Hermes統合の関連114件と、承認済みforkブランチのremote SHA・固定SHA fetchも確認した。
- 静的検証: Ruff、10安全ゲート、incident coverage、README生成/リリース導線、合成Slack図8件、diff checkを確認。
- CodeRabbit: 初回24ファイル完了。2回目はプロセス消失・完了記録なしのため未検証として保存し、ユーザーの明示承認で再実行。再実行は64/64ファイル・outcome=completed・exit0。10指摘（検査値の重複1件）を検証し、妥当な9件を修正。4回目の自動レビューは行わない。
- Claude独立coreレビューと親の要件別監査を実施。引用の段落保持、連携サマリーcheckpoint、同時書込み、投稿別backoff、画面再操作を修正。追加の親監査で、薬/症状の修飾が画面で落ちる問題と質問形式の検索不足を修正した。別の推論基盤は追加していない。
- OracleはGPT-6-pro/browser/Proで設計相談済み。引用からの構成・原文の意味保持・公開直前の競合確認・未知本文の未点検扱いを採用。既存slot/admission/artifactsを再利用した。

根拠はローカルの `~/.local/state/mcs-quality/v1.0.25/final-verification.json`、`final-suite/` の領域別ログ・収集一覧・失敗解消記録、`coderabbit-round3-disposition.json`。対象ファイルhashと基準SHAを保持する。実患者データ・本番・外部サービスへのテスト送信は行っていない。

R1私有snapshotの独立原文評価、R2本番shadowでの精度/遅延/占有実測、R3の運用受入は未実施。操作と判定条件を仕様へ記載し、別の運用許可を要する。上流未取得の削除・権限変更や実LLMの医学的正確性を、合成検証で確認済みとは主張しない。MCSのcommit/push/公開/本番配備はこの実装作業では未実施。
