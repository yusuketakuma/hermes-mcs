# 継続版の受入条件対応表

対象は `continuation-20260920.md` に固定した基準と候補worktree。
「部分」は記載試験だけでは条件全体を立証しない意味。現在の全体ゲートは未完了。
合成モデルの固定回答で、医療文章の判定精度を立証しない。

| AT | コード／既存試験への対応 | 現在の不足・証拠の範囲 |
|---|---|---|
| 001–010 | ledger / job_ops / run_check、既存取込suite | 末尾の個別照合を参照。保存・中断・部分取得・添付のみ・別ID・既読返信の11ケースを追加し全体297件成功。実ACK範囲は未実測、現方針を維持 |
| 011–014 | semantic_jev、test_semantic_jev_contract、test_registry_scope_is_explicit 等 | 合成wire検証とquestion ID改名の相関検証。固定model jev-1.13.0の実受理は合成POST1回で確認（2026-09-21） |
| 015–016 | JevClient retry、semantic_runtime | 累積attemptとrequest予算の合成試験あり。非retryableは最初のjob試行でfailed、同入力の自動tickでは再送しない |
| 017–018 | extract_facts / thread_bundle、test_long_body_chunked_fully | durable chunk再開とsource→facts網羅性検査を実装、test_semantic_extraction。実文章の網羅精度は未評価 |
| 019 | thread_bundle / semantic_loops | test_semantic_scope_edgesで別患者/別thread/非整数target混入と破損JSON/非object/空・null対象を共通validatorが拒否し、workerが外部呼出し前にfailed化することを検証 |
| 020–027 | fact schema / MED_DETAIL_QUESTIONS / audit_claims | semantic_assessmentで薬剤event×dimension別に評価・再開しworkerへ統合。test_semantic_assessment。人手評価は未実施 |
| 028–030 | audit_code / audit_claims、test_claim_audit_sees_surrounding_context / test_evidence_span_locate | span終端超過を拒否するtest_semantic_input_contract成功。test_semantic_acceptance_edgesでemoji・結合文字のcodepoint引用とUTF-16誤offset拒否を検証。実意味支持の精度は未評価 |
| 031 | extraction / semantic_evaluation | source→facts欠落検査とdurable chunk実装・合成試験済み。実ラベル評価なし |
| 032–033 | repair loop、test_repair_once_then_needs_review | dispatch前の耐久repair予約を実装。test_semantic_repair_recoveryでcrash後の二重実行防止を検証 |
| 034 | run_due / degraded notice、test_jev_unavailable_leaves_pending | test_semantic_degraded_coverageで全arrival対象の監査確認とbase送信試行後の重複抑止を検証。send gateで世代・PASS・通知資格を再検証する合成回帰あり |
| 035–036 | bundle_fingerprint / runtime / snapshot view | 原文・metadata・promptの改訂を検査。policy指紋をreader/senderで検証。添付更新と再解析seedの同一Tx／中断rollbackも合成検証 |
| 037–038 | semantic_loops / mcs_view、test_semantic_loop_generations / test_semantic_loop_view | scope/revision/時系列/低confidenceの構造ガード。新replyで現行候補・進展eventを再評価するE2Eあり。語義精度は未評価 |
| 039 | mcs_requests / hermes_plugin / integration/test_hermes_discord | native Discord入力→Hermes現在allowlist→限定plugin→preview/confirm→inbox/receiptを合成E2E。実Discord受信と本番権限配置は未実施 |
| 040 | origin eligibility / immutable loop candidates | replay/import通知抑止に加え、test_semantic_acceptance_edgesで人手完了済みrequestへのhistory取込・Loop再評価後も状態/revision/receipt数が不変であることを検証 |
| 041–043 | notify_flush、test_semantic_delivery、既存配送suite | part間停止・凍結receipt・mention/宛先契約。実Discord未送信 |
| 044 | fixed Jev endpoint / no proxy / no redirect / sanitized errors | 模擬wire境界あり。認証値混入全経路の最終確認が必要 |
| 045–046 | shared run lock / runtime deadlines / local LLM timeout | job token/CASで旧workerの更新拒否を合成検証。短命urllib workerでDNS/readを絶対deadline内にkill/reapし、loopback slow-trickleを検証 |
| 047–048 | maintenance / snapshot View、test_mcs_recovery_contract | 合成backup中断・独立復元・読取り成功。実権限配置は未実施 |
| 049–050 | test_shadow_no_outbox / test_off_no_seed_no_drain、既存全suite | test_semantic_feature_modesでshadow成果物のmode保存とenforce切替時の再評価を検証。summary_reviewでassist比較と人手採用記録を分離。現チェックポイント全体286件成功。後続変更には再確認が必要 |
| 051 | mcs_requests / request_loops / receipts / source_hash / expected_revision | native人手確定、原文hash/revision再検査、操作者・理由・候補指紋を記録。Loop採用リンクを同一Tx保存し、正式状態はrequestsから表示。重複抑止・stale拒否・rollbackと実Hermes経路の合成E2Eを検証。実Discordは未実施 |
| 052 | ledger fetch_jobs semantic kind / artifacts / existing outbox | 新queueなし。semantic固有世代・累積attemptの最小拡張を検証中 |
| 053–054 | semantic_config / run_due / runtime budget | OFF回帰あり。test_semantic_tick_budgetで実tick/SQLite/lock/snapshotを通し、取得・抽出→通知flush→semanticの順序と予算待ちpending/attempts保持を検証。通知flushはspy、実送信・実LLM占有量は未検証 |
| 055–056 | immutable semantic_bundle / stored source event eligibility | history通知抑止・旧snapshot保持の合成回帰あり |
| 057 | semantic_runtime token / CAS / usage reservation | job token/CASで旧workerの結果を拒否。実運用の並走観察は未実施 |
| 058 | semantic_runtime / semantic seed / mcs_operations / test_semantic_manual_retry | 同入力の累積上限で停止し、人手の追加1〜3試行と理由をreceiptに保存。再起動・再seed・重複confirmで上限を増やさず、新入力では旧追加枠を除去。Jev/LLM呼出し後の時間切れも累積attemptを消費し有限停止。呼出し前の予算待ちは消費せずdefer。Discord認証経路は合成結合検証、実運用未実施 |
| 059–060 | replay dedup / runtime boundary guards / notify_flush gates | 重複replayとpart間OFFの回帰。test_semantic_pauseで外部応答後のpauseが次の通信と結果昇格を止めることを検証 |
| 061–063 | notify_flush frozen parts / fp gate / semantic-only selectors | part毎header/link・原文添付維持の合成試験。send gateでproject/sourceevent/targetrevision/PASS/publicationmode/policyを検査。実送信は未実施 |
| 064 | Jev固定model / models一覧は情報扱い | 承認範囲の合成POST1回が成功し固定model echoを確認。成功後GET1回のID一覧は空。固定versionをaliasへ切り替えていない |
| 065–068 | continuation台帳 / refactor-revalidation / この表 | 既存signatureで統合、基準148件、稼働checkout無変更。最終差分の再照合が必要 |

## Release gates

RF-CODEの過去結果を今回の候補へ転記しない。Rの再検証は
`refactor-revalidation.md` の実施／未実施区分を参照する。
G0–G5はコード・統合証拠の対応付けが完了していないため全体PASSではない。
G2の限定実API smokeは2026-09-21に成功（合成POST1回、成功後GET1回、固定model echo jev-1.13.0）。モデル一覧は空。G6の正式基準はevaluation/g6-criteria-v1.jsonへ固定済み（ユーザー本人が評価担当、基準設定を委任）。人手ラベル評価、G7のcanary／配備は未実施。
`semantic-evaluation.md` の合成評価器試験はG6の代わりではない。

## 最新のローカル回帰結果

`MCS_TEST_PYTHON=/Users/yusuke/.hermes/hermes-agent/venv/bin/python scripts/run_tests.sh adapter -q`
で **336 passed in 6.96s**（pause再接続・盲検candidate全体固定修正を含む最新候補）。
Hermes候補のrunnerから`integration/test_hermes_discord.py`も最新候補で1件成功（runner wall 0.9秒）。
実際のplugin discovery、Discord event構築、GatewayRunnerのallowlist判定、
原本側drainを合成入力で接続した。稼働Discordへは接続していない。
Hermes拡張回帰の旧4件失敗は外部bot-conversation pluginの相対配置不足。テストと実pluginコードを一時領域へコピーして必要な配置を再現し、同じrunnerで39件全成功（6.3秒）。稼働配置・候補ソースは変更していない（詳細はcontinuation台帳）。
G2/G6/G7を、この成功件数で代替しない。

## 不変条件の照合（2026-09-21）

以下はコード・合成試験への対応であり、実モデルの医療的精度や実運用を合格としない。
独立レビューのINV-05旧指摘は、その後のscope validatorと7件の回帰で解消済み。

| INV | 現在の証拠 | 残る範囲 |
|---|---|---|
| 01–02 | stage_unreadのcomplete/save gate、取込・返信不足・mark応答unknownの既存試験 | 実サービスのACK対象集合の確認。現manual-only方針は維持 |
| 03 | run_checkのACKとsemantic分離、test_semantic_tick_budget | 実Jev障害と認証管理の同時運用 |
| 04 | status_report、snapshot view、pending/degraded試験 | 運用観察 |
| 05 | thread_bundleのproject/root/target検査、test_semantic_scope_edges | 複数実アカウント運用の配置 |
| 06 | fact status/polarity、semantic_assessment | 引用・予定・実施等の実文意味精度（G6） |
| 07 | audit_codeのrevision/quote/codepoint検査、Unicode回帰 | 意味上の支持はG6 |
| 08 | extraction technical status、attachment context、source coverage試験 | 実添付・実モデルでの検証 |
| 09 | audit_status_forとnotify_flush PASS gate、low-confidence回帰 | 実モデル校正 |
| 10 | durable repair reservation、test_semantic_repair_recovery | 実運用の障害注入 |
| 11 | Loopはartifactのみ、完了済みrequest不変の回帰 | 実オペレータ評価 |
| 12–13 | fixed endpoint/model、proxy/redirect禁止、stdin credential、transport回帰 | 合成実API接続は確認済み。実配置のOS egress隔離は未検証 |
| 14 | 既存Outboxのみ、frozen chunk/receipt回帰 | Discord実送信 |
| 15 | bundle/policy fingerprint、source/attachment/context改訂、send gate | quantity policy2026-09-21.1で全体286件成功。Hermes合成結合1件の成功は最終分母・小数修正前。数量guardの既知指摘は読取り専用再レビューで解消確認 |
| 16 | mode分離、shadow/OFF通知byte同一試験 | 稼働環境での非干渉観察 |
| 17 | durable job/outbox、pause/resume/circuit試験 | 長期停止/再開観察 |
| 18 | attempt/件数/権限/revision/正式状態はコード管理 | semantic_quantitiesで参照引用の値/単位/回数/期間/scopeを比較し、欠落/未知換算/複数関係を保留する回帰を追加。日本語分母・先頭ゼロ省略の既知指摘は読取り専用再レビューで解消確認。臨床上の薬剤と数量の意味精度は未評価 |
| 19 | native Discord preview/confirm、request/source hash/revision/receipt、Loop link同一Tx | 実Discord受信と権限配置 |
| 20 | origin/eligible、history/replay/archive抑止回帰 | 実履歴運用 |
| 21 | JobToken/CAS、旧worker/config/source拒否、part間停止 | 実並列worker観察 |
| 22 | OFF seed/drain停止、feature modes、tick順序と予算待ち回帰 | 実モデル占有量/費用 |
| 23 | raw/attachment/frozen receipt、shadow/OFF同一通知回帰 | 実宛先/時間帯/添付配送 |
| 24 | AT対応表、refactor-revalidation、候補manifest | 数量修正後の候補保存済み。全体ゲート照合は未完了 |

参考固定候補: `/Users/yusuke/.codex/artifacts/mcs-candidate-20260921-010805/`。
この候補は数量監査不足の再現対象であり、数量修正後の成功証拠として再利用しない。

数量修正後のソース固定候補: `/Users/yusuke/.codex/artifacts/mcs-candidate-20260921-013719/`。MCS 72パス、Hermes 10パスを基準HEADへのoverlayとして保存し、archive内容と現ファイルのhash一致を確認。このsnapshotはG2実接続結果の文書追記より前であり、最新文書の完全なsnapshotではない。

## §27 gate disposition（2026-09-21時点）

この表の初回判定対象は013719候補（MCS base 842125da、Hermes base b6e863a2）。以降は旧targets修正・評価器・計測の製品変更があり、013719のソース固定を現worktreeへ流用しない。最新変更はcontinuation台帳を参照。NOT_TESTEDは機能不存在を意味せず、ゲート全体の成立をまだ立証していない意味。

| Gate | 判定 | 証拠／残件 |
|---|---|---|
| RF-BASE / RF-CODE | 歴史的PASS記録あり、現候補へは流用不可 | phase-r-recordの46cffb0、refactor-revalidationの履歴順序。現候補はPhase J後の別tree |
| RF-OPS | NOT_TESTED | 本番起動・権限配置・実機復旧・切替は未実施 |
| G0 | PASS（Phase J基準・接続点照合） | RF-CODE対象46cffb0からの履歴順序、D01–D15接続点、変更分類をrefactor-revalidationで照合。g1-validationの全adapter hash再一致を確認。最新overlayはmcs-candidate-20260921-g0に固定。RF-CODEのRT全比較や実配備をこの判定に含めない |
| G1 | PASS（ローカルコード・合成結合） | docs/g1-validation.jsonの対象hash。全保存入口seed後rollback、改訂/添付、OFF/shadow、tick優先順、pause中新着保持とDB再接続後resumeを確認。実機/電断/ACK範囲は別gate |
| G2 | 部分PASS | 実POST1回で固定model・厳密応答契約成功、成功後GET1回。primitive/scope/transportは合成回帰。実配置egressと認証値混入全経路の最終照合は未完 |
| G3 | 部分PASS | quote/revision/Unicode/数量/repair/coverageの合成回帰と286件成功。実文の否定・時制・関係保持精度は未評価 |
| G4 | 部分PASS | Hermes native入力の合成E2E、人手preview/confirm、source hash/revision/receipt、Loopリンクrollback。実Discord受信・権限配置は未実施 |
| G5 | 部分PASS | senderの凍結part、資格、OFF/pause、縮退、原文添付の合成回帰。実配送は未実施 |
| G6 | NOT_TESTED | 基準v1を固定し、3方式資料作成・方式復元CLIと計測集計を実装。実際の盲検人手ラベルと許可済みデータの性能評価は未実施。offline合成試験で代替しない |
| G7 | NOT_TESTED | RF-OPSと実機canaryを未実施。候補は本番へ未反映 |
| GA | 本件の必須完成条件外 | §27に従い現manual-only方針を維持。自動ACKの検証・有効化は追加しない |

正式判定のPASSは該当ゲート全項目が成立した場合だけ使用する。上表の「部分PASS」は局所証拠の説明であり、ゲート通過を意味しない。

## AT001–010の個別照合（進行中）

仕様§23と現コード本文を照合。以下は名前の一致だけによるPASS判定ではない。

| AT | 確認した実装・試験の内容 | 追加で必要な証拠 |
|---|---|---|
| 001 | run_check.stage_unreadはsave_patient例外時にcontinueしACKへ進まない | test_unread_commit_boundary_preserves_work_before_ackのbefore_commitでseed中断→全Tx rollback→再open→再取得・ACKを検証（2ケース中1件） |
| 002 | stage_unreadはsave_patientへnotifyとsemanticを渡す。実tick試験は再開後message/outboxを検査 | 同testのafter_commitで停止→再open後message/outbox/semantic job各1件、ACKなし→再開して重複なしを検証（2ケース中1件） |
| 003 | test_unread_reply_snippet_remains_missingはsnippet IDをmissingに保持。stage_unreadはmissingごとにreply jobを作る | test_incomplete_fetch_or_ack_never_becomes_confirmedのsnippetケースで実reply merge→stage→再open後snippet/incomplete/reply pendingとACKなしを確認 |
| 004 | test_attachment_only_post_remains_full_and_explicitly_unparsedで実parser→save_patient→再open→bundle→summarizeを検証 | 空文字本文をfullとして保持し添付metadataとsemantic jobが残る。空claimsの合成LLM応答にも添付未解析limitationsを明示。実添付内容の解析はしない |
| 005 | test_history_returns_saved_pages_and_errorは2ページ目schema失敗で1ページ目保持・reached=falseを確認 | test_backfill_page_failure_keeps_saved_page_without_coverageで実pagination→backfill→再openを通し、2ページ目401/timeoutでも1ページ目と解析jobを保持、coverage不変を確認。401は上位へ再throw、timeoutはerrorとして返す。次tickの再試行全体はこの試験の対象外 |
| 006 | stage_backfillはconfirmed coverageまで照合する入口。test_backfill_seeds_read_arrivalsが存在 | test_backfill_recovers_read_reply_on_old_parent_without_hiding_gapsでcutoff以前の親と新しい既読返信を実history parserから保存。snippetならreply jobを残しcoverage不変、fullなら前進、通知なし。UI表示全体は別途照合 |
| 007 | mark_patient_readはsnapshot timestamp必須。実tick回帰はmark_read無効でread_marks=0 | サーバ側のACK対象集合は未実測。GAは必須外、当時の自動ACKなし方針を維持（2026-09-23 明示承認で定期実行に --mark-read 追加済み） |
| 008 | mark_patient_readは200空objectや矛盾をunknown扱い。stage_unreadは送信前にunknownを保存 | 同testで200空objectとnetwork_errorを注入し、実mark_patient_read経路→再open後unknown、was_marked=falseを確認 |
| 009 | test_identical_text_and_time_preserve_distinct_message_ids_and_projectsで同本文・同時刻の4投稿を2projectに保存し再配送 | 全messageとproject別outbox、4解析jobを保持し重複なし。現schemaはmessage_idをglobal PKとするため、複数accountの同ID共存はこの証拠の範囲外 |
| 010 | ambiguous migration試験は旧/新両tableを残してMigrationError、duplicate attachment試験はjournal変更前に停止 | 新規DBは全temp fixture、旧v4はtest_snapshot_migration_readonly_and_generation、中断v6→v7はtest_interrupted_v7_migration_reruns_backfillで既存成功証拠あり。全DDL箇所での強制終了は未実施 |

この照合では実APIを追加実行せず、テストコードと製品コードを読み取った。既存286件の成功を上記未確認部分の証拠へ拡大しない。

2026-09-21追加確認: 最終数量修正後のHermes合成結合も上記のとおり再実行済み。以前の「最終分母修正前」という記載は過去チェックポイントの時系列を示す。ソース固定archive013719は追加11ケース以前であり、今回の297件に対応する完全snapshotではない。

## AT044 / INV12–13 の送信構築経路照合

- JevClient.evaluateが組むHTTP本文はmodel/state/questionsのみ。APIキーはbounded_http_requestのローカル子process stdin envelopeで別フィールドに渡り、workerがAuthorization headerへ設定する。子process argvに本文・キーは渡さない。ローカルIPC envelopeと外向きHTTP本文を混同しない。
- semantic.llm_chatは明示api_key=Noneでloopback endpointへ送る。共通transportの親側はキーありの場合をJev固定endpointへ、キーなしの場合をloopbackへ限定し、proxy/redirectを使わない。workerの応答サイズと絶対deadlineは既存transport回帰で検証されている。
- notify_flush._postはcontent/allowed_mentions/attachment metadataのみを本文に組み、bot tokenはAuthorization header。_channel_idは患者本文のfallback channelを拒否し、_OPENERはNoRedirect/no proxy。送信は既存flushのみ。
- test_real_transport_keeps_hooks_and_fixed_wire_contractの実loopback HTTP経路はBearer headerとwire primitive、予約hook順を検査。test_real_transport_does_not_follow_redirect_and_caps_bodyは302拒否と応答上限を確認。これらは297件成功に含まれる。今回コード本文とassertを読み取ったため、再実行なし。

この確認はアプリが資格情報を本文へ追加しない構築経路の証拠。原文に利用者が秘密値を書いた場合の汎用検出・除去や、OSの通信先制限を証明しない。実配置egress検証はRF-OPS/G7に残す。

最新FIX: _semantic_seed_txの旧targetsがnull/int/boolのときのTypeErrorを、反復前のlist検査で解消。新しい原文保存時の3入口×3値を検証し、workerの不正payload拒否は維持。全306件成功。独立レビューで当該修正に確定欠陥なしと確認。

評価器の更新: schema v2でLoop集合一致/precision/未解決見落としとtest splitのtelemetry完全性を検証。全adapter313件成功はその時点の結果。後続のrequired_metrics省略修正は評価器18件、oldest_pending_job_age_s追加はruntime/CLI6件で検証。最新全体件数として313を流用しない。


## §24.2 計測の現状（上記gate判定の補足）

- `semantic.run_due` はjob/drainの単調時計による時間、開始時age、Jev要求差分、検証済みtokenと不明要求を出力する。`run_check` の巡回全体の実測時間とは区別する。
- `semantic_evaluation --runs` はaccount/runを識別し、run/project内の患者別意味処理時間、巡回/drain時間、要求数、最古age、tokenの完全/不完全観測を集計する。旧形式・欠測・重複の扱いを試験済み。
- `test_real_client_usage_reaches_offline_report_through_worker` は実JevClientの通信のみ合成化し、job処理・要求予約台帳・JSONL・評価CLI・reportのtoken一致を検証する。実サービス性能を測定した試験ではない。
- 患者全体の取得/待機/通知時間、強制終了後の計測復元、許可済み実入力での性能、3方式の盲検比較と人手ラベル/正式基準は未完了。G6は引き続きNOT_TESTED。
