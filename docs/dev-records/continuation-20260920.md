# MCS 継続実装・検証台帳

対象仕様: `MCS-REFACTOR-FIRST-20260920`。添付原本 SHA-256:
`4975e5dd68977b65277bd44057c3bb80cfef8eaed990070fd40b587415695205`。

## 今回の基準と権限

- 基準 HEAD: `842125da858d4bddc09e7e778bee5e6068c521d9`。
- 基準 adapter tree: `cb2e0b0c095abb109070bba756af3b4782870706`。
- 元 repo: `yusuketakuma/mcs-adapter`、`/Users/yusuke/.mcs`、main、開始時 clean。
- 作業先: `/Users/yusuke/.codex/worktrees/mcs-20260920-continuation`、上記 HEAD の独立 detached worktree。
- 試験用 Python 3.11.15 / SQLite 3.53.1。以前の報告の SQLite 版とは区別する。
- 元 checkout は起動定義から使われるため、逐次編集しない。実 DB、認証値、Chrome profile、実設定は今回の試験対象外。
- 今回はローカルコード・合成テスト・文書のみ。commit、push、配備、再起動、実送信は行わない。過去の文書に記された許可を今回の許可として再利用しない。
- Oracle はユーザー指示で不使用。Luna max を実装担当として利用。

## 既存フェーズの再照合

Phase R の履歴は `a8896ec` → REF `9d2c6d1` → FIX `46cffb0`。
Phase J はその後に追加され、今回開始前に既に存在する。
`phase-r-record.md` の過去 PASS を現在 tree の PASS として流用しない。
`phase-j-record.md` の「実装済み」も各要件の検証を代替しない。

| 領域 | 現行入口 | 継続時の扱い・検証 |
|---|---|---|
| D01 設定/CLI | run_check / mcs_util / 各 main | 既存入口維持、semantic設定・OFF経路を再検証 |
| D02 認証/CDP | mcs_adapter | 維持候補、合成試験のみ。Keychain実行は未実施 |
| D03 HTTP | mcs_adapter / mcs_util | 既存MCS経路維持、新Jev契約は別FIX |
| D04 未読/返信 | run_check / job_ops / mcs_adapter | 既存取込回帰、部分本文を監査完了にしない |
| D05 discovery/履歴 | job_ops / init_data | 維持候補、通知資格と意味jobを分離 |
| D06 archive | ledger / job_ops / notifier | archive抑止・予約原子性の回帰 |
| D07 SQLite | ledger | schema v7、既存表再利用。世代/CASの不足を点検 |
| D08 job/command | ledger / job_ops / semantic | 累積予算・世代・停止反映の不足を点検 |
| D09 添付 | run_check / mcs_adapter / notifier | 既存原文・添付配送を維持し回帰 |
| D10 ACK | run_check / mcs_adapter | 手動方針維持、実既読は未実施 |
| D11 抽出/rollup | extract / extract_llm / rollup | 既存成果物の選択維持。新監査の網羅性は別検証 |
| D12 Discord | notifier / semantic | 再送・part間停止・source世代を再検証 |
| D13 CCO/requests | mcs_view / mcs_requests | 既存requests正本・人手確認維持、候補の世代を点検 |
| D14 backup/snapshot | maintenance / ledger | 合成DBの復旧・reader回帰。実配備証拠は未再取得 |
| D15 起動/予算 | run_check / shared lock | 外部通信を遮断する試験入口を整備、既存優先順維持 |

## 変更単位と未完了事項

| ID | 分類 | 要件・問題 | 検証状態 |
|---|---|---|---|
| TEST-01 | TEST | HOME/資格情報/通信/Keychainから隔離する共通runner | 実装済み。基準HEADの148試験を隔離実行し成功。子プロセス一般の通信sandboxではない |
| FIX-J01 | FIX | Choice wireは公式のcriteria/probabilitiesと不一致。answer.type検査も不足 | 公式契約に修正、wire契約試験追加。実APIは未実施 |
| FIX-J02 | FIX | 送信part間のOFF/source/policy再確認、文脈世代確認不足 | 6件の合成配送試験成功。part毎の表示契約を追加検証中 |
| AUDIT-J03 | 要照合 | job内停止、累積retry、修復予約、旧workerの状態変更 | 未完了 |
| AUDIT-J04 | 要照合 | 原文→facts網羅性、低confidence、入力完全性、長文再開 | 未完了 |
| AUDIT-J05 | 要照合 | Open Loopのthread/対象/改訂一致、正式依頼との関連 | 未完了 |
| EVAL-J06 | FEAT/検証 | 人間ラベル評価・校正・段階別gate、CLI操作 | 未完了 |

公式契約の根拠: [TypeSafe HTTP API](https://docs.typesafe.ai/api.md)
（2026-09-20に公開文書を取得。API推論リクエストは送信していない）。

## ゲート

現時点の全体判定は **未完了**。今回 tree の RF-CODE/G0〜G7 は
要件・試験の対応付けと結果確認前に PASS にしない。
G2の実API試験、G6の人間ラベルと正式基準、G7の配備/canaryは未実施。
コード修復とoffline試験を先に進め、これらをmockの成功で代用しない。

## 安全な受け渡し

候補版はこの worktree に保持する。稼働checkoutへの反映は別の配備操作。
旧版への復帰は候補版を採用しないことで成立し、実DBの巻戻しは不要。
今後schemaを変更する場合は、この記述を更新して互換性を別途検証する。

## 今回追加した合成検証の記録

以下は実モデル精度・実配備の証拠ではない。

- 入力契約3件: 変更前RED→修正後GREEN。metadata改訂・必要返信不足・低confidenceを検査。
- snapshot現行性1件: 変更前RED→修正後GREEN。旧artifactを改変せずSTALE表示。
- loop表示1件: 変更前RED→修正後GREEN。返信改訂で古い解決候補を採用しない。
- loop世代2件: 同一世代重複排除、別thread除外、原文／返信改訂を区別。新module単体成功、既存worker統合後の全体試験は別途必要。
- 通知表示1件: 変更前RED→修正後GREEN。全section・制約・提案区分の保持、offsetを考慮した最新時刻とJST変換。
- 回復3件: 合成DBのbackup中断、独立dir復元・添付hash・FTS・snapshot読取り。詳細は `refactor-revalidation.md`。
- 上記のうちsnapshot／loop／通知／回復をまとめて実行: 8 passed（2026-09-20）。

試験コマンドは `MCS_TEST_PYTHON=/Users/yusuke/.hermes/hermes-agent/venv/bin/python scripts/run_tests.sh ... -q`。
作業中の部分結果であり、変更確定後に影響suiteを実行して基準結果と区別する。

## 明示的な段階設定（候補版）

`semantic.mode` と `semantic.summary_mode`、`semantic.loop_mode` を分離した。
後二者の未設定は `off`。分類だけをshadowで試す場合、ローカル要約モデルや
Loop生成を呼ばない。要約を通知へ採用するには `mode` と `summary_mode` の両方が
`enforce` である必要がある。正式requestsの自動作成・自動完了は追加しない。

`threshold_mode` は既定 `shadow_only`。enforceには明示的な `calibrated` と
空でない `calibration_version` が必要。これは校正済み基準を指定する設定契約であり、
文字列を設定しただけでG6を達成するものではない。合成試験で使う
`synthetic-test-v1` は実運用校正ではない。未知のキー・不正型・逆転閾値はOFFになる。

閾値・固定model・校正版を `policy_fingerprint` で保存し、旧判定・未送信payloadの
再利用を拒否する。retry予算だけの変更では解析内容を無効にしない。
snapshotは取り込んだ最新policyを使って旧監査をSTALE表示する。
停止中や未発行snapshotが外部の現在設定を知るとは主張しない。

稼働設定は変更していない。候補版採用時は既存 `mode` だけの設定からの移行を
別途確認する必要がある。

### 継続修正：通知世代と薬剤イベント判定

- summary/auditに生成時の`publication_mode`を保存。shadowからenforceへの変更では旧要約をそのまま公開せず、要約・監査を再実行する。repairの入力世代ごとの上限は維持。
- 薬剤詳細を原文根拠のあるfact単位に分離し、dimensionごとの完了を耐久化。広域P01の陰性で抽出済み薬剤eventを除外しない。原文と抽出status/polarityの不一致は要確認にし、抽出結果を自動上書きしない。
- 縮退通知はbase通知の送信試行・part receiptがある場合に追加しない。通知資格の消費をmessage単独からarrival eventとの組に修正し、別途記録された訂正起点を古い試行で隠さない。
- `--replay 0`がdrainへfallthroughする経路を修正。0・負数・SQLite範囲外は副作用前に拒否する。
- 検証: medication assessment +既存semantic 63件成功、CLI +assessment 4件成功、policy generation 3件成功。途中の全体試験は209成功/7失敗で、並行修正中の配送fixtureとloop世代に関する失敗。これは最終全体合格の証拠ではない。
- policy版は`2026-09-20.3`へ更新。稼働checkout・設定・DB・サービスには適用していない。

### 継続修正：添付世代と恒久エラー

- 添付identity/name/hash/bytes/stateをsemantic jobの入力世代にも含めた。取得完了・失敗状態の変化と再解析seedは同一transaction。解析OFFではseedせず、同一内容の再保存・ローカルpathだけの変更でも再生成しない。
- `run_check.stage_attachments`から既存semantic有効判定を渡し、後から完了した添付で古い解析がdoneのまま残る経路を修正。新規通知資格は生成しない。
- Jevの非retryable失敗は初回job試行でfailedに隔離し、後続tickで同一要求をblind retryしない。retryable障害だけ既存の累積上限付き再試行を使う。
- 検証: 取込＋添付世代83件成功、semantic＋runtime64件成功。前段の全体試験221成功/1失敗はテスト用関数の引数不整合で、対象fixtureを修正後、取込suiteで成功を確認。最終全体判定は引き続き保留。

### 継続修正：明示スコープと再送上限

- §13.4に合わせ、`max_attempts_per_try`は1〜3に限定。4以上は設定エラーでOFFへ倒す。
- §13.5の開始条件として非OFF時の`project_ids`指定を必須化。未指定は空scope＋OFF。明示`null`は全project、配列は指定projectのみという既存の値の意味は維持。日次予算の既定0も維持する。
- 稼働設定は変更していない。導入時は対象project・費用予算・各機能modeを明示してから、段階評価する必要がある。
- 設定／feature3件、runtime4件成功。関連semantic60件も成功した。CCO認証接続はMCS repo内に存在せず、Hermes認証済み受信境界と接続する入口・権限scopeの確認待ち。

### 継続検証：transportと歴史上のR境界

- local LLMも短命HTTP workerの絶対deadlineを使用。localhostへAuthorizationを送らず、proxy/redirect無効・256KiB上限・timeout後kill/reapを維持。統合時点で全体224件成功（5.69秒）。
- Git上のREF→FIX→R完了記録→FEAT順序と、`46cffb0`にJevファイルがないことを直接確認。`refactor-revalidation.md`の「後続にFEATが存在するので歴史上の順序も立証できない」と読める記述を修正。過去の実機試験を再実施したという意味ではない。
- Loopのrelation評価は現行source/policy世代の候補だけに限定。根拠bodyが同じでも世代metadataを欠く旧artifactは評価へ昇格させない。既存の新reply時の再評価・同世代dedupは維持。関連66件成功。

### 継続修正：Jev障害時のdurable circuit

- 実際にJev要求を実行したjobの最終結果が、retryableな429/529/5xx/transport/timeoutとして3回連続した場合、300秒のcooldownを既存artifactsへ保存する。job内のHTTP再送回数とは別の、job結果単位のカウンタ。
- 次のjob開始前にopen状態を確認する。pending job、累積attempt、原文、receiptを削除・リセットしない。MCS取得はこの意味処理専用circuitの対象外。
- 期限後は再試行可能。成功したJev評価で連続失敗数をreset。read-only statusに`jev_circuit_open`を表示する。
- `test_semantic_circuit_drain.py`で3失敗→DB再open→通信ゼロでhold→attempt維持→期限後done/resetを検証（1 passed）。既存semantic＋CLI 61件成功。

### Discord CCO接続の作業開始

ユーザーが既存Hermes Discord連携を指定。稼働Hermesには変更せず、
`/Users/yusuke/.codex/worktrees/hermes-mcs-discord-20260920`
（基準`b6e863a213777c3d9e75a9ae4e7a2c0b8a33566d`）を作成。
既存plugin handler(raw_args)には操作者情報が渡らないため、genericな
optional `command_context`接続を追加し、認可を再確認する。
MCS固有分岐をHermes coreへ追加しない。新規discovery/dispatch2件と既存slash6件成功。
これは人手承認の完成ではない。native Discord入力とsynthetic/internal入力の
由来識別、および独立MCS pluginによるproject/chat/user scopeと正式依頼の確定処理は実装中。

### 継続検証：Discord native入力とcircuit回帰

- MCS全体228件成功（6.16秒）。circuit導入で既存retry試験のfake clockがcooldown中に進まなかったfixtureを修正し、累積6回のterminal上限は維持して検証。
- Hermes候補版でfresh Discord入力に元本文・message/interaction ID・送信者・chat/thread/scopeを束縛。synthetic、書換え、結合済み入力は正式承認用のnative provenanceを得ない。relay metadataからは設定できない。
- native slashのbot情報とinteraction IDも伝達。legacy pluginの単一引数契約を維持し、context対応handlerだけにread-only mappingを渡す。
- Hermesのslash/provenance16件、batch/attachment/backfill/provenance37件成功（provenance2件は重複）。稼働環境・Discordへは送信していない。独立MCS pluginの結合検証は継続中。

- 追加: 転送snapshot本文／添付からのtext injection／recovery入力もnative承認から除外。fresh/recovered/forwardedを実adapterメソッドで検証。Discordのコマンド引数をログへ残さず、通常／expired interaction双方を検証。関連35件成功。
- profileは受信後にroutingで確定するtopologyがあるためnative transport bindingに含めない。command contextには実際のruntime profileを渡す。認可はHermes既存のtransport profileを考慮する経路で再確認する。
- 追加回帰の`test_busy_session_ack.py`は35成功／4失敗。4件とも既存test helperがworktreeの隣に外部`plugins/bot-conversation/__init__.py`を要求し、存在しないためFileNotFoundError。成功扱いにはしない。今回のcontext／batch／mixed attachment対象14件は成功。外部pluginの配置・稼働資産は変更していない。

### Discord CCO接続：正式依頼の合成E2E

- 独立`hermes_plugin`が`/mcs <JSON>`を登録。status/read/request preview/confirm/receiptに限定。設定は毎回読み、Discord native人手・現allowlist・user/chat/project scopeを必須にする。
- previewは書込みなし。actorを実Discord user IDから生成し、原文hash／更新revisionとuser/chat/scope/profileを確認hashに束縛。confirmは現在のsnapshotとscopeを再確認し、既存inboxへenqueue。原本側は既存transaction・command ID/hash receiptで一度だけ適用。
- `integration/test_hermes_discord.py`をHermes候補の既存runnerで実行、1件成功（0.92秒）。実plugin discovery、native event builder、GatewayRunnerの実allowlist判定と取消し、内部/bot/別許可chat拒否、preview無書込み、inbox→原本drain、重複confirm時の一度だけ適用を検証。
- MCS全体230件成功（6.04秒）。構成・操作・配備前提は`hermes_plugin/README.md`。実Discord受信／投稿、稼働設定、実データ、本番サービスには未適用。
- CCOのscan/retry/pause/resume・Loop採用、assist比較は未完了。native bridgeの完成だけで仕様全体完了・配備可能とは判定しない。
- 最終保護: Discord認可拒否時のログと既存admin alertもcommand名だけにし、正式依頼の引数を残さない。slash auth＋既存warning producer回帰30件成功。実通知は送っていない。

### 2026-09-21 継続：限定運用操作とpause境界

- 前ターンはDiscord正式依頼bridgeの実装・実Hermes認証経路を含む結合証拠を追加した進捗ターン。独立レビューではMCS固有の新規成立欠陥なし。ただし一般dispatcherの例外fallthroughが残っていた。
- Hermesの認識済みplugin command例外をterminalな失敗応答に変更し、例外本文をログへ出さない。実`_handle_message`から例外pluginへ到達させ、LLMが呼ばれないことを検証（関連2件成功）。unknown command既存5件成功。生Discord受信の実機試験ではない。
- `ops.scan/retry/pause/resume`を既存inboxとcommand_receiptsのtransactionへ追加。scanは有限の履歴walk、retryはpayload照合＋累積attempt維持、pause/resumeはproject単位の既存artifact。別queue／DB／schedulerなし。
- pause適用後のsemantic seed、Jev/LLM各境界、結果昇格、縮退intent、summary描画、semantic配送を停止。原文・raw通知・receiptは保持。control_generationとretry_command_idにより旧workerのCASを失効させる。
- pauseは破損jobがあっても適用し、そのpayloadは上書きせず保持。resumeは設定mode/予算を上書きせず、過去全履歴の新規seedをしない。queuedとappliedは明確に区別する。
- canonical snapshot Viewと既存CLIへoperations/controlを追加。jobsのraw payloadを返さずID/state/attempts/hashのみ表示。操作CLI→inbox→receiptとpause境界の関連7件成功。
- 中間全体238件成功（6.18秒）。Discord control pluginの最終整理・結合テストは継続中。

### Discord CCO：操作理由の記録

- request.create/updateの新規Discord／CLI操作でreasonを必須化し、既存receiptへ認証由来actor・処理時刻・対象版とともに保存。確認hashは理由も含む。理由の欠落・不正値・改ざんを拒否する。
- 既存の旧形式pending commandはそのまま処理でき、理由未記録をNoneとして残す。過去の理由を補作せず、追加DBやqueueは作らない。
- 全adapter 245件成功（6.34秒）。実Hermes認証経路の合成結合1件成功（0.99秒）、requestとscan/retry/pause/resume、理由・actorの保存を確認。実Discord／本番設定は未変更。
- 次の未完了範囲はLoop候補と正式requestの人手採用リンク、assist比較・採用履歴、最終AT/INV/RT照合。G2/G6/G7は引き続き未実施。

### Loop候補の人手採用

- request.create/updateに任意のloop_refを追加。候補ID・原文スレッド指紋・policy指紋・同薬剤／行為／期間の人手確認を束縛する。起点message/projectが違う候補やstale候補は拒否する。
- 原本のBEGIN IMMEDIATE内で再検証し、request・既存artifactのrequest_loop_link・receiptを同時保存。リンク保存失敗時はrequestもrollback。同一commandの再処理はリンクを増やさない。
- Discord preview/confirmが同じ検証helperを使用。候補詳細はpreviewに表示し、確認payloadは候補IDと世代を固定する。依頼の状態はrequestsから読み、候補へコピーしない。
- 中間全adapter247件成功（6.46秒）。実Hermes経路の合成E2E1件成功（0.80秒）：候補採用→重複confirm→リンク1件→人手完了→表示更新・候補不変。初回E2Eは追加fixtureのphase欠落で拒否され、明示previewを追加して成功。
- 独立レビューとplugin追加境界試験を継続中。本番適用・実Discord送受信なし。assist比較／採用記録と最終ゲートは未完了。
- 追加plugin試験でcreate/update採用、policy変更後のconfirm拒否、receipt/linkを確認。不要な新規互換wrapperを削除した最終全体248件成功（6.04秒）。
- 独立レビューで根拠spanの採用時検証不足を発見し修正。引用message/revision・evidence参照・整数codepoint範囲・原文一致を検査する。欠落根拠は閲覧を保持しadoption_eligible=false、採用は拒否する。偽quote/超過span/bool IDの回帰を追加。
- 修正後全体249件成功（5.98秒）、実Hermes経路の合成結合1件成功（0.90秒）。稼働環境・実データは未変更。
- 読取り専用の再レビューで根拠検証指摘の解消を確認。この修正範囲に追加の確定欠陥なし（レビュー担当はテスト未実行）。

### assist比較・採用（実装中）

- 前ターンはLoop採用と原文引用境界を実装・検証した進捗ターン。
- 既存extract_llmとsemantic_summaryの明示比較、比較対象を固定するhash、ops.adopt_summaryを既存inbox/receipt経路へ接続中。採用は人手閲覧の選択記録であり、自動通知や既存selectorの切替ではない。
- ユーザーのAPIキー要否質問に対し、現実装がTYPESAFE_API_KEYを必要とすることを回答。値を出力せず存在だけ確認し、process env・~/.mcs/.env・~/.hermes/.envの全てで未設定。G2実API検証にはキー登録と実接続範囲の明示が必要。ローカル実装は継続可能。
- summary_reviewで既存要約＋pointsと候補claims＋limitationsを比較。baselineが未作成・失敗・空・古い場合は採用不可。候補の原文世代・target revision・policy・PASS・assist・pauseを検査し、確認hashを原本適用時に再照合する。
- 採用記録は既存semantic_adoption artifactとcommand_receiptへ同じTxで保存。過去履歴をcurrent=falseで残し、現在の採用はcandidate.adoptedで示す。自動通知・ACK・正式request・既存selectorを変更しない。
- 既存CLIのcomparison/control adopt_summaryとDiscord read/controlを接続。全体254件成功（6.38秒）、実Hermes経路の合成結合1件成功（0.87秒）。CLIとDiscord双方で比較→人手採用→再表示、重複確認で記録一件、通知outbox不変を確認。
- 独立レビューと最終整理は継続中。G2実API・G6実ラベル評価・G7配備は未実施。
- 次の受入棚卸しでAT-058／§13.5の追加不足を確認：ops.retryは累積attempt>=6を常に拒否し、semantic.seedも同入力のattemptを維持するため、上限到達後の明示再投入経路がない。無断resetではなく、人手の有限な追加試行許可を記録する経路が必要。現状は未達として扱う。
- assist独立レビューのpartial context指摘を修正。原文だけでなくbundle.content_quality=fullかつcontext_complete=trueを採用条件にした。同じpartial指紋とPASS/assistを持つ候補でも拒否し、記録を作らない回帰を追加。plugin正常fixtureは実返信数と宣言数を一致させた。
- 修正後全体255件成功（6.41秒）、実Hermes経路の合成結合1件成功（1.00秒）。全体要件の完了判定は未実施。AT-011〜040の読取り専用棚卸しをサブエージェントで開始、主担当は残りATを確認中。

### 累積上限と人手による追加試行（実装中）

- 前ターンはassist比較／採用とpartial context拒否を実装・検証した進捗ターン。
- AT-058の前記見立てを訂正：ops.retryは上限を拒否するが、ledger._semantic_seed_txが同入力failedをpendingに戻し、run_dueに累積上限の入口guardがないため、直接seedで上限を越えて再実行できる。再投入が一切ない問題ではなく、自動上限と明示許可の境界不足。
- 共通attempt_limitと実行入口のCAS停止を追加し、人手のadditional_attempts 1〜3だけ有限拡張する方針。累積回数・usage・repairをresetせず、同command再配送で追加枠を増やさない。新しい入力世代へ旧追加枠を持ち越さない。

- 実装を確認し全adapter 259件成功（6.58秒）。追加枠はjob試行数でありHTTP要求数ではない。既存の日次要求・時間・repair予算は維持する。
- failed上限到達時のDiscord preview/confirm→inbox→receiptを接続。追加許可でpayloadを変更し、同じattempt/stateを持つ旧worker tokenも拒否する。OFF/pause/project scope確認後に実行入口の上限を検査する。
- AT-029のemoji/結合文字spanとAT-040の人手完了request非更新を合成試験で追加。意味判定精度の証明ではない。修正前REDの独立照合と追加試行の読取り専用レビューは継続中。
- 実Hermes認証経路の合成結合を再実行し1件成功（1.04秒）。git diff --check成功。実API・実Discord・配備は未実施。

### 追加レビュー修正：期限超過と入力scope

- AT-058の修正前証拠を基準842125da858d4bddc09e7e778bee5e6068c521d9から抽出した6モジュールで取得。同一合成probeでfailed/attempts=6→同入力seedがpending/0へ戻りfake Jevを1回呼ぶことを確認。候補はfailed/6を保持し0回。主担当も両方をenv -iで再実行した。これは基準HEADとの比較であり、直前の未コミット版との比較とは区別する。
- 独立レビューでRuntimeBudgetをstale扱いする経路を発見。実行後の期限超過でもattempts=0のままとなるJev/LLM双方のREDをtest_semantic_budget_retryで確認した。呼出し実施後はretry、通信前の予算待ちはdeferredとして次回時刻を保存する。旧worker/OFFは従来の非更新を保持。
- AT-019の別thread/別患者targetを黙って除外する問題を合成REDで確認。共通thread_bundleがscopeと整数型を拒否し、workerは不正targetを通信前にfailedへ移す。根拠のない部分処理を完了扱いしない。question ID改名の相関検証も追加した。
- 修正後adapter全体264件成功（6.79秒）。追加部分の独立再レビューは継続中。実API/実Discord/配備なし。

### 不正jobの隔離と現在の運用境界

- 独立再レビューは時間切れのretry/defer分離を妥当と確認。一方、壊れたJSON、非object、明示null/空targetsをrootへ代用する漏れを指摘した。
- 5種類の不正payloadでREDを再現し、外部通信前にfailedへ隔離する修正後は全5ケース成功。targetsキー欠落だけは旧形式互換としてrootを維持する。全adapter269件成功（6.63秒）、diff check成功。
- 過去phase-j-recordの有効化・rollback記述は旧基準の記録。本候補の本番可否には流用しない。特に本候補ではjob内の次の通信/結果昇格でOFFを再検査し、原文やpending、artifact、receiptを削除せず停止する。

現在の次段階は以下。実施済みと必要条件を混同しない。

| 段階 | 現在の成果物・実行範囲 | 残る条件 |
|---|---|---|
| ローカル受入 | acceptance-map、隔離runner、合成SQLiteとHermes Discord経路 | 独立レビュー完了と最終候補fingerprint、INV/RT/ATの最終照合 |
| G2 | semantic_jev.py --smoke --live。固定合成文のみ。既存DBを読まない | TYPESAFE_API_KEY設定、有限な実要求の範囲確定、固定model/応答/egressの実証。まだ実行しない |
| G6 | semantic-evaluation.mdのJSONL/manifest/criteriaとoffline評価器 | 評価前に責任者が基準を固定し、人手ラベルとhold-out評価を行う。合成回答の単体試験は代用不可 |
| RF-OPS/G7 | 候補worktreeを維持しlive checkoutへ未反映 | 配備先・起動構成・権限・backup復元・canaryを具体化し、承認範囲で切替。自動ACKは追加しない |
| 機能停止 | semantic mode=offで新規seed/通信/drain停止、pending保持 | 本番設定変更は未実施。送信済み通知の取消やDB巻戻しを行わない |
| コード復旧 | 現行データを保全した整合版への切替と再照合 | 旧backupによる原本上書き、全artifact削除、無条件resetを復旧手段としない |
- 読取り専用の最終再レビューで不正payload隔離とRuntimeBudget分離の指摘解消を確認。この修正範囲に追加の確定欠陥なし。レビュー担当自身のテスト実行はなし。

### 有限なG2確認経路

- wire_smokeのみmax_attempts=1に固定。通常Jev clientのretry契約は維持。合成503の修正前POST3回→修正後1回を専用テストで再現し、主担当の関連8件再実行も成功。
- smokeは評価POST最大1回、成功時にmodel一覧GET最大1回。結果のrequests値は評価POST数でありGETを含めない。キーも実データもテストでは使用していない。実APIは未実行。
- AT-054追加検証：実run_check.main/SQLite/共通lock/snapshot経路で、原文・extract_v1が通知flushより前、通知flushがsemanticより前に実行されることを確認。通信前のbudget待ちはpending/attempts=0を保持。flushはspyでありDiscord配送成功や実LLM占有量は立証しない。
- 主担当の全adapter再実行271件成功（6.34秒）。INV-01〜24の要件別最終照合を読取り専用サブエージェントへ依頼中。

### INV-18/AT-026の数量監査不足（修正中）

- 主担当が合成fixtureの要約のみ300mg→300gへ変更し、原文/factを維持したままfake Jev支持下でPASSとなる経路を再現。独立レビューでもaudit_codeがfact.quantityの引用一致しか検査せずclaim値/単位を比較しないことを確認。
- pipeline回帰4例（300g、300μg、300mg/mL、500mg）は修正前すべてRED。通常の300mg×3回表記と原文300mgを1日3回の対応は維持する。
- claimが参照するfactの引用だけを使う数量検査を追加中。数値集合の一致で薬剤・量の関係を証明したとは扱わず、単位換算/濃度/1回量と1日量の対応不明を保留する。
- policyを2026-09-21.1へ更新し、旧PASSの再利用を止める。数量表記保持をsummary promptへ追記。通常の一回repairで解消しないfindingはNEEDS_REVIEWとなる。
- 新helperはサブエージェント実装中、統合後の全体試験は未実施。前の固定候補/271件結果はこの新差分の成功証拠ではない。
- 数量guard中間検証：正常paraphrase/repairを含む9件と全体280件成功（6.41秒）。その後、負号・千区切り・明示日量追加の3条件（-300mg、1,300mg、300mg/日）が引用300mgを1日3回から無findingとなる漏れを主担当が再現。pipeline回帰へ3例を追加し修正依頼。280件はこの追加前のチェックポイントで、数量監査の完了証拠ではない。
- 主担当が数量helperの仕上げを引継ぎ。数量なしの早期returnが削除検査を遮っていたため除去し、参照引用の数量・回数がclaimから部分的に落ちてもclaim_quantity_missingとする。未知単位・符号/桁区切り・濃度・日量は未検証として保持し、Jevの支持で解除しない。複数量/頻度の対応が曖昧なら保留。
- 数量削除pipelineテストの文字列置換はJSONの\u00d7表現に一致していなかったため、JSONをdecodeしてclaim本文を変更する形へ訂正。その実入力でも削除を保留し、正しいrepairはPASSとなる。
- helper/pipeline14件成功、adapter全体285件成功（6.51秒）。その後、300mg/日→300mgのscope削除も拒否する条件を追加し、関連14件再成功（0.20秒）。最終独立再レビューと変更後全体再実行は未完了。
- この数量guardは引用との値/単位/回数のdeterministic照合であり、臨床上の薬剤・対象・時制の意味精度を証明しない。自動単位換算はせず、換算不明・複数関係は要確認のまま残す。
- 数量scope最終変更後のadapter全体285件成功（6.75秒）、実Hermes認証経路の合成結合1件成功（0.84秒）、git diff --check成功。独立再レビューは継続。G2に向け、キーのローカル登録後に合成POST最大1回＋成功時GET1回の限定範囲をユーザーへ確認中。返答前の実API実行はしていない。
- ユーザーからG2の限定実行承認を受領：キー登録後、合成文評価POST最大1回、評価成功時のみmodel一覧GET1回。再承認不要。承認後に実clientと同じ_env解決で存在だけ確認しTYPESAFE_API_KEY未設定。キー値の表示・資格情報変更・実通信は行っていない。登録後にこの範囲で実行する。
- 追加レビューの日本語分母（/回・/錠・/包）の消失とleading decimal（.5mg/.6mg）の未検出を修正。分母を数量unitに保持し、未知の分母はunknownとして保留、先頭ゼロ省略をDecimalで扱う。新回帰RED→GREEN、関連15件成功、adapter全体286件成功（6.79秒）。指摘2件の解消確認を読取り専用で依頼。
- 同時点で実clientのキー解決を値非表示で再確認し未設定。限定API承認は有効だが、実要求はまだ0回。

### 数量guardの再レビュー完了

- 読取り専用の独立再レビューで、日本語分母の保持、未知分母の保留、.5mg/.6mgの識別と専用回帰を確認し、直前の2指摘の解消を確認。レビュー担当自身は変更・テスト実行なし。全仕様の完了判定ではない。
- 最新adapter結果は286件成功（6.79秒）。Hermes合成結合1件成功（0.84秒）は最終の分母・小数修正より前であり、同時点の結果として扱わない。
- キー登録操作について保存先ディレクトリと.envの存在・書込み可否、nanoの存在を確認。秘密値は表示せず、実API要求は未実施。

### 承認済みG2実接続（2026-09-21）

- ユーザーのキー登録完了後、python3 adapter/semantic_jev.py --smoke --liveを1回だけ実行。終了0、ok=true、model_echo=jev-1.13.0、noul=0.99、requests=1。固定合成文のPOST1回で実transport・厳密応答検証・固定model echoを確認。再試行なし、実MCS原文・DBを使用せず。
- 評価成功後にmodel一覧GET1回。models=[]、fixed_model_listed=false、models_errorなし。利用可能IDは得られていないが、固定versionの評価成功は上記POSTで確認。alias自動切替なし。キー値は表示・記録していない。
- この証拠は限定wire smoke。医療・業務意味精度、実運用コスト、OS egress隔離、G6人手評価、RF-OPS/G7配備を立証しない。

### AT001/002 保存境界の追加検証

- 実stage_unreadとSQLiteを用い、semantic seed中の例外（commit前）およびsave_patientのcommit直後の停止を注入。どちらもACK未実行。接続を閉じ再openし、前者はmessage/outbox/semantic jobなし、後者は各1件を確認。再取得後は各1件のまま、保存後にのみ合成ACKが実行された。
- runnerでtest_unread_commit_boundary_preserves_work_before_ackを実行し2件成功（0.10秒、81 deselected）。製品コードの変更なし。ACKはspyで実サービスには接続しない。OS強制終了/fsync障害の試験ではない。

### AT003/008 不完全取得とACK不明の耐久性

- 実fetch_unread_replies/stage_unread/SQLiteでsnippet返信のpending hydrate job・患者incomplete・ACKなしを再open後に確認。実mark_patient_readのtransportだけを合成200空object/network_errorに置換し、どちらも再open後unknown、confirmedなしを確認。
- 隔離runnerの専用3ケース成功（0.10秒、83 deselected）。製品コード変更なし、外部通信なし。実サーバのACK対象集合の証明とは区別する。

### AT005 ページ途中の401/timeout

- 実fetch_history→stage_backfill→SQLiteの接続で、2ページ目だけSessionExpired(401)/retryable timeoutを注入。1ページ目の原文とsemantic jobが再open後も存在し、coverageは前進しない。401は上位へ再throw、timeoutはerror記録のまま返る。
- 専用runner2件成功（0.09秒、86 deselected）。再ログインや次tickの再試行全体をこの試験だけで立証しない。製品コード変更・実通信なし。

### AT004 添付のみ投稿

- 実fetch_unread_messagesのparserからsave_patient、再open、thread_bundle、summarizeまで接続し、正式な空文字本文をfullで保持、添付metadataと解析jobを保存、添付未解析limitationsを明示することを確認。LLMのみ空claimsの合成応答、実添付ダウンロードなし。
- attachment contextファイル3件成功（0.09秒）。製品コード変更なし。

### AT009/010 個別照合

- 同時刻・同本文・同hashの別ID4件を2projectに保存し、再配送で原文・project別通知・解析jobが失われず増殖もしないことを確認。専用runner1件成功（0.09秒）。複数accountの同じ数値IDを同一DBへ入れる証明ではない（現schemaはglobal message_id）。
- 既存migration回帰のassertを確認：v4→現行では原文count/IDを保全し古いreaderを拒否、v6→v7のcolumn追加後中断ではversion fenceを根拠にbackfillを再実施、曖昧table/重複keyは非破壊で停止。過去286件結果内の試験であり、今回再実行はしない。

### AT006 古い親への既読返信の回収

- 実history parser→backfill→SQLiteで、cutoffより古い親に紐づく新しい既読返信を回収。full/snippetの2条件で、full時のみcoverage前進、snippetは再取得jobと旧coverageを保持。両方とも解析jobを持ち、既読履歴から通知意図は生成しない。
- 専用runner2件成功（0.10秒）。実APIでのsort/reconciliation保証やUI表示全体はこの合成試験の範囲外。

### 取得境界追加後の全体回帰

- 隔離runnerでadapter全体297件成功（6.65秒）。AT001–006/008/009の追加11ケースを含む。
- Hermes候補の隔離runnerで実plugin discovery/native Discord event/allowlist/inbox/receiptの合成結合1件成功（runner wall 1.1秒）。最終数量修正後も接続を確認。実Discordには接続しない。
- G2の承認済みPOST1回＋GET1回以降、追加の実API要求なし。全gate完了やG6品質評価の証拠へは拡大しない。

### RT011 の既存証拠確認

- test_discovery_archived_off_skips_kartesを全文確認。探索falseだけでなく、既存archived history_head pendingを事前作成し、kartes呼出し禁止→discovery→history drain→doneまで同一試験で確認していた。297件成功に含まれるため、重複試験は追加せず対応表の未確認記述を訂正した。
- 次の境界はRT018：public writerと_tx helper、command BEGIN IMMEDIATE、semantic結果保存の所有者を照合中。検索結果だけでは全経路のTx安全性を確定しない。

### FIX: 不正な旧semantic targetsによる原文保存失敗

- RT018照合中、_semantic_seed_txがtargets型検証より先に反復するため、null/int/boolの旧payloadでTypeErrorとなり原文保存Txを巻き戻すことを発見。patient/messages/replies全3入口×3不正値の9ケースでREDを再現。
- 共通helperで旧targetsがlistのときだけ整数IDを採用する。新たに取得した原文のDB由来IDで次世代をseedし、壊れた旧配列型で取り込みを停止させない。後段にあった無効な型補正を除去。worker自身の不正payload拒否は維持。
- 関連20件成功（0.21秒）、全adapter306件成功（6.86秒）。実MCS/API/Discordへの追加接続なし。変更は候補worktreeのみ。

### 旧targets修正の独立レビュー

- 読取り専用の限定レビューが完了し、今回差分に確定欠陥なし。list検査後の反復、厳密int要素、source世代・対象差分・累積attempt分岐の維持と3保存入口×3値の専用回帰を確認。レビュー担当自身のテスト実行はなし。
- ソース固定候補は /Users/yusuke/.codex/artifacts/mcs-candidate-20260921-015416/ 。MCS 72パス、Hermes 10パス。MCS fingerprint 0981337ec2900a667d08a546677f3c674991f3ca85189b57ddaae96dde9cfba3。今回レビュー結果の文書追記より前のsnapshotで、製品ソースは一致する。

### FIX: offline評価の余分なLoop候補の見落とし

- 正解集合にない候補Loopを追加してもloop_conformityの分母へ入らず合格できる不足を専用REDで確認。分母を正解/候補IDの和集合へ変更し、欠落と余分な候補の双方を誤りに数える。これは集合一致度でありprecision単独とは区別する。
- zero-denominator既存fixtureはcandidate loopsが残っていたため、真の空集合になるよう明示的に空にした。評価器ファイル11件成功。人手ラベル・G6実評価は未実施。precision/未解決見落としの個別指標と患者/run別telemetryは引き続き仕様照合が必要。

### §24.2 Loop個別評価指標

- loop_precisionとloop_unresolved_miss_rateを追加。余分な候補、未出力、誤った解決を別々に集計し、既存の分母/誤数/Wilson区間・明示criteriaへ接続。意味変更をreport schema v2に反映。
- 合成3条件を含め評価器14件成功（0.02秒）。閾値の例示は正式運用基準ではない。人手ラベル・run別telemetryは未完了。

### FIX: 評価telemetry欠落の合格見逃し

- 2件中1件のlatency/usage/requestsを欠落させてもgate=trueとなる3条件をREDで確認。test split全caseのlatencyとrequests/input_tokens/output_tokensが揃わなければtelemetry_missingで不合格にした。total_tokensは既存の任意性を維持。
- 評価器17件成功。計測値を補作せず、未計測は未計測のまま報告する。患者/run別集計と実測値収集は引き続き未完了。

### 評価器変更後の回帰

- 隔離runnerで全adapter313件成功（7.08秒）。Loop集合一致/precision/未解決見落としと欠落telemetry gateの追加7ケースを含む。最新差分はoffline評価器だけであり、実API/Discord経路は再実行しない。
- 評価器変更の分母・gate・互換性について読取り専用の限定レビューを依頼。患者単位/run単位の遅延はcase latencyの単純和から推定できないため、実測の単位を決めて扱う必要がある。現状のcase集計を患者/run p50/p95と表示しない。

### FIX: 評価criteriaの既定指標

- 文書に記載されたrequired_metrics省略時の全指標defaultが、tupleをlist専用validatorへ渡すため拒否されることをRED再現。既定値だけlist(METRICS)にし、入力側の厳密list検証は維持。
- 評価器18件成功（0.03秒）。独立レビュー担当へ追加差分を通知。全体313件結果はこの1行修正と追加1ケースより前のチェックポイント。

### 評価器レビューと最古待機時間

- 読取り専用レビューでLoop各指標の分母、test split全caseの欠落telemetry拒否、required_metrics既定値修正について追加確定欠陥なし。schema v2のzero-denominator拒否は文書化された契約と確認。レビュー担当は編集・テスト実行なし。
- semantic.status_reportへoldest_pending_job_age_sを追加。next_tryが未来の再試行待ちも含め、pending jobの最小created_atとの差を取得。pendingなしはNone。DB変更なしを専用試験で確認し、runtime/CLI関連6件成功（0.13秒）。
- この値は現在のCLI照会時点の経過時間であり、過去run時点の待機時間を復元する指標ではない。患者別処理時間とrun別評価集計は引き続き実装が必要。

### §24.2 意味処理の実測値出力

- 仕様を再読し、既存run_checkの全巡回elapsed_sとは別にrun_dueのdrain時間とjobごとの実測時間・project/generation・要求差分・開始時ageを結果JSONへ追加。患者名・本文・資格情報は追加しない。新規tableやusageの二重計上なし。
- 単調時計を固定した正常処理/捕捉例外の2条件で、drainとjobの測定範囲、予算台帳との要求数一致、OFFの無変更を確認。runtimeファイル7件成功（0.14秒）。患者全体の遅延やp50/p95の集計完成とは扱わない。
- 共通run_due経路変更後の隔離全体回帰：adapter 317件成功（7.01秒）。実API・Discordへの追加要求なし。候補worktreeのみ変更。

### §24.2 実測runのオフライン集計

- 前ターンは実測出力追加と全体317件成功により進捗あり。今回、評価CLIの任意--runs入力でaccount/runを識別し、患者別の意味処理時間・要求数、全巡回/drain時間、最古pending ageのp50/p95を集計した。
- 同一患者の複数jobと別account同IDを含む合成データで集計範囲を検証。run重複・NaN拒否、欠測runの非ゼロ補完を確認。評価器19件成功（0.02秒）、diff check成功。全体317件結果はこの集計追加前。
- 患者単位はaccount/run/projectで開始済みjobの処理時間。患者全体の遅延やG6実評価の完了には拡張しない。token使用量・強制終了時の実測復元は未完了。追加の実API要求なし。

### §24.2 Jev token観測の欠測分離

- 既存Jev応答validatorが要求するinput/output tokenをclient内で累積し、run_dueのjob差分へ接続。結果採用のafter_result guard前に観測するため、stale破棄でも返却済みusageを失わない。予算台帳の要求数は変更なし。
- 使用量が返らない要求をunreported_requestsとして分離。tokenを0使用と断定しない。検証済み応答2回（うちstale hook1回）と503失敗1回の合成試験、fake clientの欠測表示を確認。関連15件成功（0.15秒）。患者/run別token集計は引き続き必要。
- client共通経路変更後の隔離全体回帰：adapter 319件成功（6.76秒）。候補worktreeのみ変更、実API呼出し追加なし。

### §24.2 患者/run別token集計接続

- 前回のusage観測を評価CLIへ接続。患者/run別に観測token合計・完全観測だけのp50/p95・不完全観測数・使用量不明要求数・旧jobの欠測・未観測run数を出力する。
- 部分usageを完全な総量として扱わず、要求数との整合性と応答0件の正tokenを入力境界で拒否。完全/部分/旧形式の3患者を同じrunへ入れ、合計17tokenに対してrunの完全観測percentileがnullであることを検証。
- 評価器20件成功（0.03秒）。全adapter319件は前回チェックポイント。今回の変更はオフライン評価器と試験・文書のみ。追加実API通信なし。患者のend-to-end遅延、強制終了時の計測復元、人間ラベル評価は未完了。

### §24.2 計測から評価CLIまでの結合検証

- 実JevClient（POSTのみ合成応答）→応答validator→runtime hook/要求予約→run_due→job_metrics→JSONL→評価CLI→report JSONを接続した一時DB試験を追加。
- 正常完了で複数の実evaluate呼出しがあり、返却tokenの合計と要求予約台帳、reportの完全観測p50が一致することを確認。runtimeファイル8件成功（0.17秒）、diff check成功。外部ネットワーク・本番DB・実Discordは使用なし。
- 計測と集計の追加差分を既存の独立レビュアーへ読取り専用で依頼。レビュー結果待ち。製品コードの追加変更なし。

### §24.1 監査前候補の保全

- 既存summaryはrepair後の候補だけを保存するため、初回のJev補助要約と監査後候補を後から同じbundleで比較できないことをコードで確認。既存artifactへsemantic_candidate（pre_audit、入力/方針fingerprint、revision、mode）を初回のみ保存する。書込み前に世代guardを再検査し、通知/採用selectorには追加しない。
- 既存repair試験を異なる初回/修正版の文で拡張し、初回候補が修正版と同じfingerprintで保全されることを確認。fixtureは親・返信の2対象なので全LLM呼出し数を単一対象の2回とした当初assertは不適切で訂正した。
- これだけで盲検比較は完了しない。固定bundleからのbaseline生成、方式を隠す配布、正式人手ラベルは未完了。実API追加なし。
- 全adapter321件成功（6.89秒）、diff check成功。
- 計測部分の読取り専用レビュー完了：OFF非干渉、要求/token差分、account/run/project境界、欠測と重複処理に追加確定欠陥なし。今回のsemantic_candidate保存はレビュー依頼後の別変更であり、このレビューの対象外。

### 監査前候補の中断再開

- candidate保存直後のKeyboardInterrupt→DB close/reopen→run_due再開を合成試験。初回artifactの全フィールド不変と最終summary保存を確認。
- 再開時に保存済み同fingerprint/policy/mode候補を再利用し、同じ入力で別の初回要約を再生成しない。未処理の返信だけが要約を生成することを確認。fixtureには親/返信があり全生成禁止では未処理返信も阻害するため、試験をその契約に修正。
- runtime/repair recovery/semantic関連70件成功（2.07秒）。全体321件はこの修正前。diff check成功。実API追加なし。盲検配布・baseline生成・人手評価は引き続き未完了。

### §24.1 盲検比較資料の作成入口

- semantic_blind.pyを追加。入力済み3方式を同一bundle指紋に限定し、ランダムなA/B/C評価票と管理者対応表をローカル新規ディレクトリへ分離保存。患者split混在、重複case、不足方式、異なる指紋を拒否。人手ラベルは未入力のまま。
- 合成CLI試験1件成功（0.01秒）：対応表による出力復元、評価票のmetadata除外、key0600、上書き拒否、入力世代不一致、患者split漏れ拒否。実患者・モデル・ネットワークなし。
- baseline生成・DB由来の来歴照合・実人手評価は未完了。指紋の自己申告だけで生成の正当性を立証した扱いにはしない。

### 固定bundleと3方式出力の照合

- semantic_blind.fixed_bundle_outputsを追加。保存bundleのfingerprint再計算、対象full本文、revision/project、policy/mode、pre_auditと最終PASS/NEEDS_REVIEWを照合してから明示的baseline関数へ対象本文を渡す。
- 既存llm_extractが対象本文3000文字・既存prompt/validatorを使うことを確認。通常巡回へのbaseline追加なし。実モデル呼出しもなし。
- 一時DB由来bundleによる2件成功（0.08秒）。revision不一致はbaseline呼出し前に拒否。DB選択/CLIの自動接続と人手評価は未完了。diff check成功。

### snapshot選択から盲検資料CLIへの接続

- 明示artifact IDのselection入力と--snapshot/--generate-local-baselineを追加。read-only SQLiteの一貫したread transactionで取得・close後、全入力の来歴とsplitを検査して既存llm_extractを呼ぶ。通常モードは従来の既生成テキスト入力。
- 実semantic workerの合成出力→一時SQLite→CLI→資料作成を検証。DB論理dump不変、split漏れと既存出力先はbaseline追加呼出し前に拒否。専用3件成功（0.13秒）、diff check成功。
- 実モデル/実患者での生成・人手評価・正式基準は未実施。追加実API通信なし。全体の完了ゲートは未達。

### 盲検評価の原文文脈

- snapshot資料の原文表示が本文連結のみで、対象・親返信・投稿者・投稿時刻を失っていたため、評価者が時制/speaker関係を照合できる構造化表示へ変更。方式名や監査結果は含めない。
- 実worker→snapshot CLI合成試験で対象ID、親返信関係、投稿時刻、投稿者、baseline入力と原文の一致を確認。専用3件成功（0.11秒）、diff check成功。
- semantic_candidate保存/再開とsemantic_blindの限定読取り専用レビューを依頼。実モデル/実人手評価は未実施。実API追加なし。

### 盲検資料の生成元対応

- 管理者keyへ明示選択した3artifact IDと初期worksheet SHA256を記録。方式を伏せるworksheetには追加しない。snapshot CLI試験でID対応・hash一致・評価票への非混入を確認。
- IDは保持したsnapshot内の識別子であり、署名来歴や人手ラベルの真正性を証明しない。G6実評価は未実施。
- 候補保存/再開と盲検CLI追加後の隔離全体回帰：adapter325件成功（6.85秒）、diff check成功。実API要求追加なし。独立レビューは実行中。

### FIX: 盲検入力の患者/thread境界

- bundleの指紋だけが整合した別患者member混入を比較helperが受理するREDを確認。外部snapshotを入力する境界では指紋一致とscope妥当性は別条件。
- 共通fixed_bundle_outputsでproject/root/targetとmember IDの厳密正整数、同project、親返信関係、重複拒否、root/target存在を検査。直接helper/CLI双方でbaseline呼出し前に拒否。
- 専用3件成功（0.10秒）、diff check成功。全体325件はこの修正前の結果。独立レビュー担当へ修正を通知。実API追加なし。

### 比較資料の未解析文脈と独立レビュー修正

- 評価票の原文文脈へcontent_quality/context_complete/missing_replies、添付metadata、attachments_interpreted=falseを追加。実worker→CLI合成試験で添付の存在を保ち、署名URL/ローカルpathは出ないことを確認。
- 独立読取りレビュー完了。確定指摘1件：artifact行/metaが整合していてもcontent内のtarget_message_id/input_bundle_idが別対象のものを受理。対象IDすり替えのREDを再現し、共通helperで厳密int対象IDとbundle IDを検査して修正。専用3件成功（0.11秒）、diff check成功。
- レビュアーは編集・実テストを実施していない。盲検資料の添付metadata欠落の懸念も上記変更で対応。人手評価・実モデル評価は未実施。実API追加なし。

### 盲検ラベルの方式復元入口

- --unblind-keyを追加。human_labels以外を初期worksheet hashと照合し、同一review集合のA/B/C記入を管理者keyの方式へ戻す。未記入・本文改変・重複/欠落を拒否。生成モードとの併用不可。
- 専用4件成功（0.11秒）、diff check成功。評価値の内容は変換・採点せず、人手由来の証明やG6合格として扱わない。品質評価器へのラベル接続は未完了。
- G6担当者/正式基準が決定済みかを非同期で確認。回答待ちでも独立した実装・合成検証は継続可能。実API追加なし。

### G6評価担当と初期基準の確定

- ユーザー本人が評価担当。正式基準は未定との訂正と実装担当への設定委任を受領。evaluation/g6-criteria-v1.jsonとREADMEへ初期基準を固定。98%重要再現/関係保持、95%Loop適合/一致、critical/誤解決/未解決見落とし0、保留30%以下、最終人手test200件以上。調整は次版・別固定評価集合へ適用する。
- min_human_labelsを校正データの件数だけで満たせないよう、最終test件数も条件へ追加。合成ラベル拒否を維持。評価器21件成功、実criteria validator成功。
- 方式復元CLIのファイル出力と0600、account/project/worksheet hash保持を確認。盲検関連4件成功（0.10秒）。実人手評価・配備・追加API送信は未実施。

### 評価時criteriaの保存

- reportへ正規化済みcriteriaとSHA256を追加。版名だけを再利用して閾値が変わった場合も、適用内容を確認できる。同一内容のJSONキー順序変更ではhash不変。
- 評価器/runtime/盲検関連35件成功（0.35秒）。正式v1基準の固定とユーザー本人の担当をacceptance-mapへ反映。人手ラベルと実性能が未実施のためG6はNOT_TESTEDを維持。
- diff check成功、追加の実API/Discord通信なし。

### 評価ツール統合後の候補固定

- 隔離runnerで全adapter328件成功（6.79秒）。計測・盲検資料・snapshot生成・方式復元・正式基準v1と入力境界修正を含む。Hermes側はこの期間に追加変更なし。過去の合成Discord結合証拠は時点を区別して保持。
- ソースoverlayを新規artifactへ保存する。対象はgit差分と未追跡のソース/試験/文書のみ、DB・資格情報・稼働状態を含めない。保存先のmanifestを正確な固定点とする。G6実評価と実運用ゲートは未完了。

### FIX: 正解factの重要度欠測

- 品質評価器への人手入力を照合し、important欠落/文字列/整数を重要fact分母から黙って除外する不足を3条件REDで確認。label factのimportantを厳密boolean必須にして修正。
- 評価器/runtime関連34件成功（0.20秒）、diff check成功。annotation-guideに原文先行の正解fact作成、全claim照合、否定/時制/投稿者/Loopの記入と既存ツールの接続制約を明記。
- 023720保存候補はこの修正前。実人手評価・実モデル通信は未実施。基準v1の閾値は変更なし。

### 盲検主張IDの導入

- 各A/B/C出力へ共通形式のclaim_id/textとlimitationsを追加。snapshotでは既存主張単位を保持し、baselineはsummary/pointsを使う。本文内改行を機械分割しない。limitationsはclaimへ混ぜない。
- 手動入力側のclaim_texts/limitationsは表示本文との完全一致を要求。既存テキストのみ入力は全体c1として扱う制約を明記。専用4件成功（0.11秒）、diff check成功。
- 元artifactのIDではなく表示choice内のIDであり、モデル方式を新たに露出しない。人手の原子的fact定義や品質評価器への完全接続は未完了。実API追加なし。

### 盲検ラベルと定量評価の結合

- semantic_blindへ--evaluation-records/--manifest/--methodを追加し、検証済み記入票のラベルを未ラベル評価recordへ結合。case集合/account/project/split/bundle指紋と全claim ID/textを照合し、既存ラベルの上書きを拒否。
- 既存品質validatorを再利用。facts/loops/usageを補作せず、sourceも人手へ書き換えない。合成ラベルを結合したレポートがG6不合格のまま、未レビュー本文へすり替えた入力が拒否されることを確認。
- 盲検/評価器関連30件成功（0.15秒）、diff check成功。人手ラベル・固定実評価データの生成来歴は未取得。実API追加なし。

### ラベル結合CLIから品質レポートまで

- 既存の結合試験をファイル/CLI経路へ拡張。完成票＋管理者key＋未ラベルrecord＋manifest→semantic_blind CLI→evaluation.jsonl→semantic_evaluation CLI→reportまで接続。
- 合成ラベルのprovenanceが維持され、最終再現率の分母2が保たれ、g6_eligible=falseであることを確認。盲検関連5件成功（0.13秒）、diff check成功。
- annotation-guideの未接続記述を現状へ更新。正解factの人手定義と固定構造データ準備は残る。実人手評価・追加API送信は未実施。

### §27 G1の再照合開始

- 直前のユーザー向け報告の「残るのは実評価データと人手評価」は評価機能内の残件を指す。全体にはG0最終照合、G1証拠判定、RF-OPS/G7実運用なども残っており、全体の完了とはしない。
- §27のG1条件は解析jobの原子性と既存契約非干渉。save_patientのraw/outbox/job同一Tx、_semantic_seed_txのcaller所有、attachment更新とseed、実tickの既存通知優先と予算保留を現在コード/試験で再照合。
- ingest revisions/mcs ingestion/feature modes/tick budget/attachment contextの111件成功（0.86秒）。合成MCS応答・一時SQLite・実tickの範囲であり、実サービスACKやDiscord送信の証明ではない。
- 同じG1範囲の独立読取り監査を依頼。結果待ちでG1全体PASSへは変更しない。追加実API要求なし。

### G1全保存入口のseed後障害注入

- 既存の原子性証拠を強化し、save_patient/save_messages/save_thread_repliesの3入口で実seed SQLの後に例外を注入。原文/通知/jobの書込みが済んだ位置からrollbackさせ、DB close/reopen後のpatients/messages/notify_outbox/fetch_jobs全行が事前状態と一致することを確認。
- ingest revisionsファイル16件成功（0.15秒）、diff check成功。製品コード変更なし。独立監査担当へ追加証拠を通知。実サービス障害・ACK範囲の証明には拡張しない。

### FIX: pause中の新着解析job喪失

- G1主担当照合で、_semantic_seed_txがpause時に新着seedを捨て、resumeは既存pendingだけを戻すため原文が再取得されなければ解析されない不足を発見。新thread投稿をproject明示で保存する試験でjobなしのREDを確認。
- 共通seedからpauseによる省略を除去。意味処理ONの取り込みでは原文とpending jobを同一Tx保存し、pause中のworker/送信guardは維持。OFFは呼出元のsemantic=Falseで非生成を維持。過去全履歴の再seedではなく停止中の新入力だけを保持する。
- pause中0要求、resume後に新旧2job完了と新着summary生成を確認。pause/ingest revisions/runtime28件成功（0.37秒）。以前の台帳の「pauseはseed停止」はこの修正で撤回。
- 独立G1監査は追加確定欠陥なしとの結果だが、このpause修正より前の限定範囲。主担当で見つけた欠陥をそのレビュー結果で打ち消さない。実API追加なし。
- 共通ledger seed変更後の全adapter336件成功（7.03秒）。保存候補023720より後の製品変更。G1最終判定と候補再固定は別途更新が必要。

### G1ローカル判定

- pause中新着試験をDB close/reopenまで拡張し、停止状態とpending jobが耐久化され、resume後に再取得なしで解析できることを確認。pauseファイル3件成功（0.14秒）。製品コードは全体336件成功時から変更なし。
- §27 G1の解析job原子性・既存契約非干渉について、取得3入口のSQL seed後rollback、添付更新rollback、変更世代再seed、OFF/shadow、tick順序/予算保留、pause/resumeの証拠をまとめ、ローカルコード/合成結合のPASSを記録。対象adapter全PythonのSHA256をdocs/g1-validation.jsonへ固定。
- 独立レビューはpause修正前、pause修正は主担当のRED/GREENと全体回帰。電断・実サービス・G0/G6/G7へこの判定を拡張しない。diff check成功。


## G6 blind candidate binding fix (2026-09-21)

Confirmed RED: unchanged claim text allowed replacement of candidate facts
at label merge. Prepare now optionally freezes the complete evaluation_candidate,
shows facts/loops/status predictions on the worksheet, and hashes the complete
candidate into the coordinator key. Quantitative merge requires this frozen
candidate and rejects changed facts, loops, usage or status; legacy text-only
worksheets remain usable for qualitative unblinding only. No human provenance
is inferred. Paired blind/evaluator tests: 30 passed after the fix.

This changes only offline evaluation preparation and its test/documentation.
G1 runtime evidence still describes its recorded source tree; the G0 archive
predates this fix and is not the latest evaluation tool. No live calls or
production modifications occurred. Actual human labels remain pending.


## 再開後の実配置・G2確認

読み取りのみで3本の実launchd定義とinbox/snapshotのディレクトリを照合し、deployment-candidateへ記録した。既定Hermes profileのMCS plugin未配置を確認。他profile/ACL/ロード状態は未確認。機密値・原本DBは読まず、サービス操作なし。

G2のbounded_http_request→stdin worker→HTTP構築とJevClient.evaluateを再読。要求本文はmodel/state/questions、keyはstdin envelopeからAuthorizationへ渡る。argvには本文/keyを含めず、worker環境は許可キーだけ、stderrや例外本文を外部診断へ転記しない。proxy/redirectなし、deadline超過ではkill/reap。これらはコード境界の確認であり、OS egress隔離の証明ではない。追加の実API呼出しなし。


## 最新候補回帰とG4/G5再照合（2026-09-21）

- 最新worktreeで隔離runnerを実行：MCS全adapter **336 passed in 6.99s**。Hermes候補から実plugin discovery/native Discord/gateway/snapshot/inbox結合 **1 passed、runner wall 1.1s**。外部通信なし。
- G4：pluginのcontext型/hostフラグ、毎回のscope設定、confirmのorigin/actor/hash一致、source hash/request revision/Loop/assist比較再検証を読んだ。実結合試験はHermes allowlist変更・bot/internal・別channelの拒否、人手create/update、receipt、重複、pause/resume/retry/scan、有限追加試行、assist採用時のoutbox不変まで検証。これで実Discord配置や人手ラベルを代替しない。
- G5：flush本体の既存単一sender、archived抑止、宛先未設定時の失敗、partごとのsemantic gate、途中payload変更時hold、各part receipt、添付確定拒否のみtext fallback、429/network時に即fallbackしない経路を読んだ。test_semantic_send_gateはsource/target/policy/publication modeと縮退の起点資格を検査し、上記336件に含む。実配送・OS権限は未検証。
- 新しい確定不具合はこの読取り範囲では確認しなかった。G3全経路の最終照合、最新版固定、実権限検証は残件。既存Hermes拡張suiteの外部plugin依存問題は今回の単一結合成功で解消扱いにしない。


## G3：主張監査の発言者・投稿時刻欠落修正

要約生成はsender/posted_atを入力するが、audit_claimsは引用と本文のみを送っていた。実worker経由の既存context試験へ原文memberのメタデータ一致を追加し、修正前はmetadata不在でREDを確認。共通audit_claimsにevidence_metadata（message_id/parent_id/revision/posted_at/sender）を追加し、帰属・相対日付の照合に使う指示を加えた。本文や認証情報以外の無関係なデータは追加しない。

意味監査の入力契約が変わるためPOLICY_VERSIONを2026-09-21.2へ更新。旧policyの監査結果を現行PASSとして再利用させない。初回とrepair後の全claimは同じ監査関数を通る。引用範囲/revision/数量、source→facts/事実→claim、repair事前予約と全claim再監査の接続も再読した。

修正後の全adapter **336 passed in 6.96s**。実Jev/LLMは呼んでいないため、発言者・時制の実意味精度はG6未評価のまま。旧g1-validation/archiveのhashはこの変更以前であり、最新版固定時に更新する。


## Hermes外部plugin依存の再検証

既存test_busy_session_ack.pyの4失敗はparents[3]/plugins/bot-conversation参照先の不足。実pluginの対象hookはconfigをmockしたbot入力のroute attestationのみで、送信関数を呼ばないことを確認。テストファイルとplugin __init__.pyだけを一時領域へbyte同一コピーし、socket.connect拒否fixtureを追加。候補Hermesのscripts/run_tests.shから実行し **39 passed、runner wall 6.3s**。原本/候補ソース・稼働plugin配置は変更なし。

一時root：/var/folders/yg/_v84mvr55kb5dqdpzhvm79bc0000gn/T/mcs-hermes-plugin-check-tquhgmsb
テストSHA256：4811aaa28e9cac1c7c8b13df633ae52fcef688176884b7b9e47fece547468f5c
外部plugin SHA256：2202101e5f9674e65ce794fe6cbf7baf179ca52466261cba8f1c5ce746691444

旧35成功/4失敗を隠さず、必要なコード配置を満たした現在の成功証拠を追加する。Hermes全suiteの合格や本番plugin配置の変更を意味しない。固定archiveの製品コードはこの検証で不変、上記記録はarchive後の文書追記。
