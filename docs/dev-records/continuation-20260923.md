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

# 2026-09-22 REPO-WIDE AUDIT LOOP (run: audit-20260922a)

- B = 88a72cfb9956af9b20468396400039a8a034bb75 (main, clean)
- 本runはcommit禁止。累積差分はworking treeに保持。
- 対象: mcs/ 33modules + hermes_plugin + ci/ + install.sh + Makefile +
  deployment/ + scripts/ + tests/ (89 files, ~33k LOC)
- 既知監査済み: job_ops fetch_jobs retry/defer全経路 (AUDIT-J03)、
  extract_llm (J04)、request_loops (J05)、notifier signal経路。

## coverage matrix
| area | scope | state | evidence |
|------|-------|-------|----------|
| C01-C12 | 全module | DONE | 全39モジュール静査完了(下記) |
| X01 | install.sh/deployment/launchagents | DONE | pin/venv bootstrap健全 |
| X02 | pin/lockfile/CI | DONE | pin=16390dc80d(fork main HEAD確認済み)・ci.yml/ gates/mine_gates |
| X03 | snapshot/export/maintenance | DONE | ro分離・atomic write・backup検証 |
| X04 | mcs_adapter/ingest/notifier/hermes send | DONE | redirect分離・DL上限・send gate |
| X06 | semantic_*/extract_llm/plugin | DONE | 全17 semantic modules + extract系 |
| X07 | signals/review/summary_review | DONE | fingerprint/policy/revision三重bind |
| X05/X08 | 該当なし | DONE | |

## findings
(随時追記)

### FIX-KC1 (P2): Keychain locked を missing credential と区別

**発見**: login keychain がロック中(または UI 非対話 context)だと
`security find-generic-password -w` が rc=36 で失敗し、
`_keychain_password` が None を返すため `auto_login` は
`manual_required` に倒れる — 実際は「エントリは存在するが読めない」
で、復旧は unlock であり再登録ではない。通知 detail にも reason が
載らず診断不能だった。2026-09-22 に実発生(全エントリで rc=36 を
実測確認、missing は rc=44)。

**修復**:
- `mcs_adapter.KeychainLocked` 例外を追加。`_keychain_password` は
  rc=36 または stderr の "interaction is not allowed"/"keychain is
  locked" で raise、missing(rc=44/"could not be found")は従来通り None。
- `auto_login` は `"keychain_locked"` を返す。
- `MCSError.detail` を属性として保持(従来は str(e) のみ)。
- `run_check._err_str` が detail を含める → `finish_run` status と
  session_expired 通知に `auto_login=keychain_locked` が出る。
- `mcs_setup check` が entry 存在+`-w` 読取可能性を別段で検査し、
  locked を unlock 手順つきで報告(対話 session では ACL prompt を
  一度だけ出し得る)。
- README に回復・再発防止(set-keychain-settings)を記載。

**回帰**: tests/test_mcs_ingestion.py に7件(分類・auto_login 状態・
_err_str detail)、test_mcs_setup.py に1件(locked/missing 区別)。
実機 `mcs_setup check` で locked 診断を確認済み。

### FIX-HIST1 (P2→defensive): sort=pinned 順序違反で cutoff 誤認証を防止

**発見**: `fetch_history` は `sort=pinned` で取得し、ページ内で最初に
cutoff 以下(created_at <= since)のメッセージを見た時点で walk を打ち切り
`reached=True` を返していた。API が「ピン留め先頭+残り時系列」の順序を
返す場合、古い pinned メッセージが先頭に来ると即 stop → 未取得範囲を
カバー済みとして floor/coverage に誤認証し得る(ROADMAP C01 は API 契約
未検証の懸念として記録済み)。

**修復**: cutoff 以下メッセージを見ても即 stop せずページ末尾まで走査。
後続に cutoff 超のメッセージがあれば順序違反とみなし `unordered=True`、
以降は cutoff stop を無効化して has_next 終端まで歩く(自然終端のみ
reached)。順序が正常なら従来どおり早期停止。below-cutoff メッセージは
従来通り格納しない。

**残存する限界**: pinned メッセージのみでページ全体が埋まる場合
(>=per_page 件)は依然誤停止し得る — API 契約 fixture による確定が
必要(ROADMAP C01 継続)。

**回帰**: test_mcs_ingestion に3件(順序正常時の早期停止維持、
順序違反→自然終端到達、違反+未終端→非認証)。

### FIX-BE1 (P3): brain_export の front-matter/table エスケープ

`title:` が YAML 非エスケープで名前の `:`/改行で front-matter 破壊、
`_cell` が改行を潰さずテーブル行分割可能。`json.dumps` による YAML
quoted scalar + `_cell` の改行空白化で修復。回帰テスト1件(敵対的
patient_name/medication 名)。

## 全モジュール静査の完了 (2026-09-23 最終パス)

mcs/ 全39モジュール + hermes_plugin + ci/ + scripts/ + install.sh +
Makefile + ci.yml + conftest.py + integration test を静査完了。
このパスで確認した残件:

- `semantic_llm` — 厳密JSON検証・絶対日付のみ解決・claimはfact index
  必須・promptサイズ上限で incomplete 返却。健全
- `semantic_quantities` — claim text+evidence quote から決定論的に
  数量抽出・未知複合単位は未検証扱い・不一致/欠落/曖昧を明示 finding。
  健全
- `semantic_evaluation` — split 間の account/project/thread/case
  漏洩拒否・frozen candidate sha256・human label provenance 必須・
  denominator 0 を gate 理由化・Wilson 区間。健全
- `semantic_blind` — 3方式シャッフル+coordinator key 分離・unblind は
  worksheet sha256 完全一致必須・merge_labels は frozen candidate と
  照合・split leak 拒否。健全
- `semantic_bench`/`extract_bench` — 凍結 corpus・0o600 出力・
  `--jev` は明示的 operator 操作として budget 外注記。健全
- `mcs_util`/`maintenance` — env_value の file-pinned lookup・
  no-redirect/no-proxy opener・run lock・backup verify+atomic。健全
- `mcs_setup` — typesafe config 規則・Keychain probe(FIX-KC1)・
  .env 0600 merge・validator 自体の例外も error 化。健全
- `ci/mine_gates`・`scripts/*`・`conftest.py`(socket/proc 遮断)・
  `plugin.yaml`・integration test — 健全

**OPTIONAL 観察(確定不具合ではない)**:

- `ledger.outbox_mark` の `(OSError,...)` 経路は attempt 消費するが
  hold しない → 永続配信失敗は 3600s backoff cap で無限リトライ
  (outbox_due limit=20 で bounded、at-least-once 設計として一貫)。
  無限ループ自体は設計意図の範囲内
- retry ループ内の一部 `KeyError` が transient 分類 → 最大 ~1回/時の
  再試行。実害限定のため据置き

**検証結果**: `pytest 546 passed`・`ruff clean`・`ci/gates.py 8/8`・
`mine_gates --check clean`(FIX-KC1/HIST1/BE1 を manifest 登録)・
`update_readme.py --check` drift なし。install.sh pin は local fork
`16390dc80d`(merged fork main HEAD)と一致確認済み。

**残存する未完了項(監査外・運用観察領域)**: AUDIT-J03 の実運用観察
項目・AUDIT-J04(抽出網羅性)・AUDIT-J05(Open Loop 関連)・EVAL-J06
(人間ラベル評価)・ROADMAP C01(sort=pinned の API 契約 fixture 確定)。

## 監査後半 — 独立レビュー finding の修復 (FIX-BE2/RU1/ID1/ID2/UR1/G1/G2/SU1)

全モジュール監査完了後、explore 系の独立レビューが 8 件の finding を
報告。全件をコードで検証・確定し修復した(semantic/drain/runtime/Jev/
QC/evaluation 領域の追加監査は新規欠陥なしで完了)。

| ID | 修復 |
|---|---|
| FIX-BE2 | `brain_export` の stale cleanup が `--out` 配下の任意 `p*.md` を削除し得た → 生成名 `p<int>.md` のみに限定 |
| FIX-RU1 | `rollup.msgs_by_ts` が NULL posted_at_ts に一致せず症状日付が文字列 `"0"` になった → `(posted_at_ts or 0)` で同一 coercion、posted_at 欠損時は `msg:<id>` |
| FIX-ID1 | `init_data` の `except SessionExpired` は `fetch_history` が error を埋め込むため未到達 → batch.error/merged.error 上の SessionExpired で 1回だけ auto_login → cursor から再開。list_projects 失敗・再認証後の再失敗も JSON 契約で返す |
| FIX-ID2 | `list_projects` が malformed `last_message.created_at` を 0 に潰し live project を inactive 化し得た → `SchemaError` |
| FIX-UR1 | `update_readme.render` が generator 例外を warning のみで吞み `--check` が未検証 README を pass し得た → failed generator を収集し両モードで exit 1・部分書換えなし |
| FIX-G1 | `gate_snapshot_readonly` が別行の `uri` token で connect 行を免除し mode=ro 抜けを見逃し得た → connect 行自体に `mode=ro`/`immutable` を要求 |
| FIX-G2 | `gate_records_isolation`/`mine_gates` が dev-records 直下のみ走査し nested 記録を見逃した → `rglob` 化(heatmap 含む) |
| FIX-SU1 | `mcs_setup init` の `--password`/`--typesafe-key` argv が shell history/`ps` に残った → 削除し `MCS_SETUP_PASSWORD`/`TYPESAFE_API_KEY` 環境変数または getpass に限定。Keychain 書込みは `security -i` stdin + read-back 検証 + 失敗時 delete |

`semantic_blind.snapshot_records` は明示的 `?mode=ro` URI connect に
正規化(FIX-G1 の新検出と整合)。

**検証(最終・全実測)**: `pytest 562 passed`・`uvx ruff clean`・
`ci/gates.py 8/8`・`mine_gates --check` 全 incident covered/tracked・
`update_readme.py --check` clean。全テスト合成fixtureのみ・外部I/O
遮断(conftest)を維持。

**残存**: AUDIT-J03 実運用観察・AUDIT-J04/J05・EVAL-J06・ROADMAP C01
(sort=pinned API 契約 fixture)は引き続き open。outbox 無限リトライ
(bounded backoff)・一部 KeyError の transient 分類は設計範囲内として
据置き。

## 実運用観察ラウンド (snapshot 02:47 実測) + FIX-SD1

公開snapshot(7643万B・189患者・15,398 msg・26,028 artifacts)を
read-only観察し、残存 AUDIT 項目を実データで検証した。

### AUDIT-J03 — 運用観察完了(coveredへ)

- `discovery` job: 永続pending設計どおり日次自己再スケジュール
  (next_try 未来・updated_at 日次更新) — 飢餓なし
- `reply` 19件全 done: attempts<=6 の有界 retry で最終解決
  (body_incomplete/thread_incomplete の再発 error は全て解決済みの
  履歴ノイズ)
- `attachments`: 10件 failed も全て attempts=6 打止め — 有界
- `notify_outbox`: 40件全 accepted・滞留/失敗なし
- `extract_qc` 1363件 due・attempts=0: max_jobs=4/tick・eligible順
  FIFOで消化中(過去24hで534件done、~22/h) — 設計内 backlog
- `semantic` 8件 pending: 非eligible層のFIFOでQC群(低位job_id)の後
  — 約2.5日待ちは設計内(§19.1 到着優先)。OPTIONAL: QCをsemantic
  より低位 tier にする検討余地
- 中間 state ('running'等) の滞留ゼロ、runs 445ok/59partial/
  1crash(旧) — partial主体は reply/attach/budget の一時的事由

### AUDIT-J04 — 抽出側は設計内確認、facts側は件数不足で open 継続

- extract_v1 網羅 97.4%: 欠落390件は全て body_text='' (抽出対象外)
- extract_llm: 新着~100%(7d 81/81、30d 413/414)、旧文 55.9% は
  per-tick cap(limit=15/budget)での archive 消化中 — 設計内
- llm error artifacts: v2 は全て attempts>=5 枯渇(設計打止め)、
  v1-era 66件は v2 budget で再試行キュー入り(posted_at降順の後方)
- 長文: corpus最大~1.9KBで単一chunk、メッセージ粒度の durable
  retry で再開担保 — chunk内 resume は不要水準
- 低confidence: QC annotate層が item-vs-body で検証(534 done、
  backlog消化中)
- semantic_facts 実績3件・loop 0件 — Jev facts網羅性は本番件数
  不足のため open 継続

### AUDIT-J05 — 機構健全・実績なしで open 継続

- loop_candidate/loop_event ゼロ: facts 供給が少なく loop 機構未発動。
  revision鍵・世代bind・時系列guardの設計は静査で確認済み
- requests/command_receipts 0件: ops 経路未使用

### 確定修復: FIX-SD1

`semantic_drain._write_result` が deferred/PENDING の度に byte同一の
summary+audit pair を追記していた(実測: 同一 summary_id が 13分で
3行 — Jev 到達不能期の再監査)。`_current` での同一 outcome 検出を
追加し冪等化。summary内容・audit_status・publication_mode・findings
が一致する場合のみ書込みを抑止 — status 遷移(PENDING→PASS)は新規
行として正しく残る。

### 検証

`pytest 564 passed`・ruff clean・gates 8/8・mine_gates clean
(FIX-SD1 登録、AUDIT-J03 covered、J04/J05/J06 観察結果を summary に記録)

## semantic-facts/v2 canonical実装 (omo計画 `mcs-clinical-completeness-jev-qc` 引き継ぎ)

omoは計画書のみでusage limit停止・実装未着手のため、本worktreeで T1–T16 を実装。
dirty tree出発点は `.omo/evidence/mcs-clinical-completeness-jev-qc/task-1-starting-tree/` に保全。

### 実装サマリ

- T2 `mcs/semantic_facts.py`: 契約凍結(validator/enum/ID/quantity)、eval criteria拡張、contract tests
- T3 `semantic_extraction`: atom/chunk ownership — 全codepointを1回のみcover、core排他、heading依存保持
- T4 `mcs/local_llm.py`: 共有bounded transport(loopback/no-proxy/no-redirect)、finish_reason/usageをthread-local経路で収集(legacy出力不変)
- T5 `extract_facts_v2`: `_FACT_V2_PROMPT`網羅抽出、obligation×mandatory category、chunk単位persist/resume、v1 hints union、fact_id重複merge、`validate_facts_doc`検証
- T6 `mcs/semantic_relations.py`: latest-wins排除 — 順序確定時のみEXPLICIT_SUPERSESSION、不明時CONTRADICTION
- T7 `fact_source`ゲート: legacy|shadow|canonical + `mcs_setup fact-source` activation(g6 evidence pin `g6-v1:<sha>`)
- T8 `jev_preflight`: chunk×category presence adjudication → `jev_pre` obligations(failure→failed、present無fact→open維持)
- T9 `audit_facts_v2`: 双方向監査(fact→evidence支持 + source→facts coverage)、doc_hash keyの監査artifact、INCOMPLETE=PENDING扱い
- T10 `repair_facts_v2` + `_FACT_V2_REPAIR_SUFFIX`: rejected factの所有chunkのみ再プロンプト、receipt artifactで1dispatch/generation bounded、修復docは再監査
- T11 `mandatory_render`: verified facts + non-terminal obligationsの決定的描画層(notice `■ 抽出済み事実`)
- T12 consumer migration: `project_v2_doc_legacy`(lossy明示) + `canonical_projection` artifact + `current_fact_pred`(hash-current投影がextract_llmをshadow) — stats/queries/signals/rollup配線
- T13 queue fairness: 非eligible段でsemantic>extract_qc優先 + `pending_by_kind`/`done_by_kind`/`job_metrics.kind` observability
- T14 `evaluate_jev_incremental` + `mcs_setup jev-value`: deterministic vs Jev-only findings分離、mandatory_recall、evaluated=Falseならgate不pass
- T15 `docs/semantic-facts-v2-rollout.md`: modes/gate/pipeline/failure semantics/artifacts
- T16 `run_shadow_e2e` + `scripts/semantic_shadow_e2e.py`: extract→reconcile→audit→repair→renderのstage可視e2e driver(実Qwen接続時にそのままbench可)

### 新規テストファイル

test_semantic_fact_contract / test_semantic_atomization / test_local_llm /
test_semantic_facts_v2 / test_semantic_relations / test_fact_source /
test_semantic_facts_audit / test_semantic_repair / test_semantic_mandatory_render /
test_canonical_projection / test_jev_value

### 検証 (最終wave実測)

- `pytest`: **671 passed** (564→+107)
- `ruff check mcs/ tests/ scripts/`: clean
- `ci/gates.py`: **8/8 pass** / `mine_gates --check`: clean
- `update_readme.py --check`: clean (43 modules/66 test filesへ再生成)
- shadow e2e report: `.omo/evidence/mcs-clinical-completeness-jev-qc/task-16-shadow-e2e-report.json`
  (local LLM未到達環境では extract=incomplete/audit=INCOMPLETE と正直に記録 — pass捏造なし)

### 残存・未検証面 (honest scope)

- 実Qwen+実Jevでのbench実行は未実施(本環境で127.0.0.1:8080非稼働) — driverはready
- canonical runtime drain end-to-end(audit+repair artifact chain)は単体・統合テスト済み、
  本番shadow稼働観察は未実施
- `canonical_projection`は明示的lossy — allergy/adverse/vital/preference/otherは
  legacy slotなし(consumer docs参照)
- multi-message threadのrelationsはrun_shadow_e2eのactive_facts越しで動作、
  drain側persist path(T12後半のrelation永続化)は未接続

## 全体レビュー所見と修復 (R-round)

T1-T16実装後の全diffレビューで確定した欠陥を最小修復。全て再現検証→修復→回帰テスト。

| ID | 欠陥 | 修復 |
|---|---|---|
| FIX-SE1 | hint dedup(`kind`+normalize(statement))で破棄されたv1 hint factの`fact_id`がobligation `fact_ids`に残存→`doc_obligation_fact_unknown`クラッシュ。同一薬剤2回記載(開始/中止)・subject差異で実データ到達可能 | `semantic_extraction.py`: dedup時にobligation内の`fact_id`を存続factへ差替え+`obligation_ids`をunion |
| FIX-SE2 | `_cached_chunks`がv2 `_evidence`をv1キー(`start_codepoint`)で検証→evidence持ちv2 chunkが常にcache miss→durable-prefix resume無効 | `start`/`end`と`start_codepoint`/`end_codepoint`の両キー表記を受理 |
| FIX-SE3 | `_atomize`が先頭whitespace-only pieceを最初のatomに含めず→`atom_coverage_incomplete`クラッシュ(先頭改行の投稿)。連続空白pieceで`pending`が上書きされる副次欠陥も同時存在 | `atom_start=pending`化+先頭runの最古startを`min`で保持 |
| FIX-SR1 | `mandatory_render`が`max_facts=40`超過分のverified factを無告知省略 | 省略件数をlimitationとして告知 |

### レビューで確認済み・修復不要と判定した項

- canonical+gate無し → `semantic_config`の`if errors: mode=off`でfail-closed(実測)
- 投影`meta.hash`=`member["revision"]`=`content_hash`で`current_fact_pred`のshadowing正しく動作
- drainの`kind`優先ソートはeligible段にも効く(「非eligible段」計画より広いがsemantic jobのlatency優先として意図的)
- `repair_facts_v2`のstatus再導出は`fact_ids`有無のみ — drain経路はcomplete docのみ来るため実害なし(APIレベルでは注意)
- `_v2_llm_subject`の`person:*`→`other`はdocstring(→family記述)と不整合 — 動作はsafe側

### 検証 (修復後実測)

- `pytest`: **675 passed** (+4新規回帰)
- `ruff`: clean / `ci/gates.py`: 8/8 / `mine_gates --check`: clean / `update_readme --check`: clean
- `gates-coverage.json`: FIX-SE1/SE2/SE3/SR1登録

### 残存(据置き)

- `_integrity_note`はdegrade再試行を数えず`calls`過少計上(観測のみ)
- `evaluate_jev_incremental`で`source_fact_coverage_ambiguous`/`low_confidence`がdeterministic側に計上(分類ラベルのみ)
- gate tokenは設定ファイル文字列 — 手編集でevidence pin迂回可能(operator trust境界内)
- `_KIND_CATEGORY`が`semantic_render`/`semantic_extraction`で重複定義

### 追加上級修復 (R-round 2)

| ID | 欠陥 | 修復 |
|---|---|---|
| FIX-SE4 | `repair_facts_v2`はdropped itemを含むchunkのpartial factsをmerge — extractionのfail-the-chunk意味論と非対称 | dropped>0のchunkはlink rollbackのみでmergeしない |
| FIX-SE5 | repairのstatus再導出が`fact_ids`有無だけで`failed`/`pending` chunk所有obligationを`covered`に昇格し得た | owner chunkのstatusがfailed/pendingなら非terminal statusを保持 |
| — | `_v2_llm_subject` docstringが`person:*`→familyと記述(実装は`other`) | docstring訂正 — 実装はsafe側で正しかった |

検証: **677 passed** (+2), ruff clean, gates 8/8, mine_gates clean, README check clean。

### 第3ラウンドレビュー (consumer未移行・fetch境界・docs照合)

追加精査の結果:

| # | 判定 | 内容 |
|---|---|---|
| doc修正 | **修正済み** | rollout doc: shadow行「produced, audited」→実装は監査canonical-onlyなので「produced, not audited」に訂正。artifact表 `semantic_facts_chunk_v2` → 実kind `semantic_extraction_chunk_v2` に訂正 |
| R11 | **残存リスク(報告)** | `fetch_history`の`sort=pinned` unordered検出はページ内のみ — ページ末尾がbelow-cutoffで終わり次ページにabove-cutoffが続く境界ケースは`reached=True`で早期終了し得る(コード内コメント自身の「never certify via cutoff」と矛盾)。旧実装(途中break)より大幅に狭いが残存 |
| consumer判定 | **正当(未移行で正しい)** | `mcs_requests.candidates`/`notifier`/`mcs_view` pending mirror/`semantic_observe`/`_qc_seed`/`_qc_job`/`run_check`/`summary_review`の`extract_llm`参照は全てextract pipeline自身またはprojectionが持たないフィールドが対象 — extract_llm pipelineは`fact_source`非依存で稼働継続するため機能不全なし。canonical由来の`request_pending`がsuggestionに載らないのは既知のcoverage gap(投影はlossy) |
| 検証済み健全 | ✅ | `current_extract_pred`は`extract_version`を要求しないためprojection行が正しく通過/`meta.hash` binding正しい / `_current`は`artifact_id DESC`でrepair後doc+audit整合 / `payload_hash({f,e})`のdoc_hashはaudit入力と同じ写像域 / repair manifest再構築+doc_chunk_ids一致確認 / `_normalise_facts_v2`のobligationリンクはevidence atom owner決定でchunk-local保証 / `llm_chat`→`local_llm.chat`はmax_tokens=1400・temperature=0・thinking無効でparity / `_opener_request`が`_OPENER`patch seamを保持 / thread-local integrity集計の`note_start`境界がpool thread再利用で正しい / `_keychain_store`は`security -i` stdin経由でargv秘匿+read-back verify+失敗時delete / bench corpus 12件全て`evidence_quote`⊂body / テスト改変は全て追加のみ・弱体化assertionなし |
| 軽微(据置) | — | `items_dropped` limitationはdead code(drop→chunk failで`dropped`常に0; 実観測は`dropped_in_failed_chunk`で報告済み) / `evaluate_jev_incremental`の`source_fact_coverage_ambiguous|low_confidence`がdeterministic側計上(Jev由来だが) / `run_shadow_e2e`のrepair呼出しがtry未装備(1件のrepair例外でrun全体abort) / `audit_facts_v2`が`ev["quote"]`を直接参照(contract検証済みdoc前提) |

検証: **677 passed**, ruff clean, gates 8/8, mine_gates clean, README --check clean。

### 第4ラウンド: 残存修復 + 構造リファクタリング

#### 修復

| ID | 欠陥 | 修復 |
|---|---|---|
| FIX-AD1 | `fetch_history`(sort=pinned)がページ末尾のbelow-cutoff itemをcutoff認証に使用し、`has_next`未消化でも`reached=True`で早期終了し得た — 次ページにabove-cutoff itemがあると欠落→cursor進行で恒久miss | コード内コメントの規定どおり「below-cutoff itemはスキップするが終端認証は`has_next`のみ」に変更。`test_history_stops_at_ordered_cutoff`は旧意味論を固定していたため`test_history_ordered_cutoff_still_walks_to_natural_end`へ更新+`test_history_pinned_straggler_at_page_boundary_keeps_walking`追加 |
| — | `evaluate_jev_incremental`が`source_fact_coverage_ambiguous`/`low_confidence`をdeterministic側に計上(Jev由来なのに) | `JEV_FINDING_CODES`に2code追加 |
| — | `mcs_requests.candidates`が`canonical_projection`未対応 — canonicalの`request_pending`がsuggestionに乗らない | `canonical_projection`をfetch対象に追加しhash-current projectionが`extract_llm`をshadow(current_extract_predはextract_version非要求なので投影行が正しく通過)。stale projectionはshadowしない逆ケースもテスト |
| — | (前ラウンド記録の据置2件は前セッションで修復済み: `run_shadow_e2e` repair try-wrap、`audit_facts_v2` evidence quote guard) | — |

#### リファクタリング (facade+siblings規約に沿う)

| 対象 | 変更 |
|---|---|
| `KIND_CATEGORY` | `semantic_extraction`/`semantic_render`の重複定義を`semantic_facts.py`へ集約(契約taxonomyの正本) |
| `evaluate_source_fact_coverage`+`COVERAGE_OPTIONS`+`_coverage_incomplete` | `semantic_extraction`→`semantic_audit`へ移動(Jev評価の凝集性)。drainとテストのimport更新 |
| v2→legacy投影層 | `semantic_extraction`末尾~220行→新規`mcs/semantic_projection.py`(`project_v2_facts`/`project_v2_doc_legacy`+変換表)。drainとテストの参照更新 |
| extract_qcサブシステム | `semantic_drain`の229行→新規`mcs/semantic_qc.py`(`_qc_seed`/`_qc_questions`/`_process_qc_job`/`QC_MAX_ITEMS`)。drainはtop-level re-exportで`semantic_drain._qc_seed`等のpatch面を保全、qc側は`_eval_chunked`/`_jev_failure_class`を関数内lazy importで循環回避 |
| `_process_job_inner`のfact stage | ~200行のcanonical/legacy分岐ブロック→`_fact_stage`関数へ抽出。member-loopの`break`を`{"outcome": "incomplete"\|"retryable"\|"hard_fail"}`の明示戻り値に変換、callerがflag mapping。drain 1317→1117行 |
| dead code | v1/v2両`extract_facts`の`dropped` accumulator+`items_dropped` limitation(drop→chunk failで常に0)を除去、`"dropped": 0`の契約keyは維持 |
| `_sha256`重複 | file-hash版2箇所(`mcs_operations`/`mcs_refstats`)→`mcs_util.file_sha256`へ集約。text-hash版は`semantic_extraction._sha256`→`sf._sha256`に統一 |

検証: **679 passed**(+2: AD1境界回帰・mcs_requests canonical shadow), ruff clean, gates 8/8, mine_gates clean(FIX-AD1登録・FIX-HIST1のテスト名更新), README再生成+check clean。

#### 残存(据置き・記録のみ)

- `_integrity_note`はdegrade再試行を数えず`calls`過少計上(観測のみ)
- gate tokenは設定ファイル文字列 — 手編集でevidence pin迂回可能(operator trust境界内)
- `semantic_extraction.py`は1447行でv1/v2経路を同居 — さらなる分割は`_persist_chunk`/`_cached_chunks`/manifest共有のためimport絡みが増えるだけで留保

### 第5ラウンド: llama-server性能対策 + RTスロット予約 (2026-09-23)

依頼「Aの3つを実行。2スロットに落として、1つはリアルタイム優先に設定する。」

#### 変更 (runtime: ~/Library/LaunchAgents/ai.hermes.llamacpp.plist)

| 項目 | 変更前 | 変更後 |
|---|---|---|
| `-c` | 65536 | **16384** (KV cache ~1/4、1slotあたり8192) |
| `-np` | 3 | **2** |
| `--spec-type` | なし | **ngram-simple** (draft model不要・メモリ増なし) |
| `-ctk`/`-ctv` | q4_0 | **f16** (dequantペナルティ解消) |

#### 変更 (code: MCS背景スロットpin)

- `mcs/local_llm.py`: `BACKGROUND_SLOT = 0` 規約定数を追加
- `mcs/semantic.py` / `mcs/extract_llm.py`: 全LLM呼出しに `extra_payload={"id_slot": 0}` — **slot 1はRT用に常時空ける**

#### 実測 (同一prompt: 527tok in / 327tok out, 単発値・背景負荷あり)

| 設定 | decode |
|---|---|
| 変更前 (np3/c65536/q4_0) | 5.1 tok/s |
| np2/c16384/q4_0 | 5.6 tok/s |
| +ngram (q4_0) | 5.4 tok/s |
| +f16 KV (spec無し) | 5.0 tok/s |
| **f16 KV + ngram (最終)** | **6.3 / 6.2 / 6.9 tok/s** (~15-20%改善) |

補助効果: **swap 15.9GB→4.7GB** (64K ctx KV+長期稼働プロセスのswap滞留が解放)。prefix cache hit時 prefill 4.4s→0.3s。ngram acceptance 29% (42/144 tokens, ログ実測)。

#### 検証

- `semantic.llm_chat` 実呼出しが **slot 0に着地** (task=1, tokens=20)・slot 1未使用を確認
- llama-server再起動後 `/slots`: slot 0/1 各 n_ctx=8192・launchdラベル `ai.hermes.llamacpp` 稼働
- focused pytest (`local_llm|extract_llm|semantic`): 136件全pass / ruff clean

#### 判断・留保

- **ollama embedding server** (qwen3-embedding:0.6b, port 52632, ~900MB): ollama keep-aliveで~4分後auto-unload。GBrain埋め込みで使用中のため手動停止せず
- **gbrain reranker** (port 8081): RSS 2MBで無害
- **Chrome等ユーザープロセス**: 明示許可なく停止しない — 必要ならユーザー判断
- decode絶対値 ~6.5tok/s はbase M4 (GPU共有+Metal) 環境の実能力。さらなる短縮はモデル小型化 or 出力契約圧縮 (quote→offset等) が必要
- RT側クライアント (Hermes agent等) が `id_slot=1` を明示送信するかは未配線 — 現状は「slot 0占有回避」による準予約。完全予約にはRT側にもid_slot送信が必要

### 第6ラウンド: スロット命名1/2 + RT配線 + 夜間掃除 + 04:00再起動 (2026-09-23)

依頼「スロット名を1と2に名称変更。RT側の配線を実行。不要プロセスの自動掃除を夜間に実行。早朝4:00に自動再起動する設定。」

#### スロット命名 (logical 1/2 → wire id_slot 0/1)

- `mcs/local_llm.py`: `SLOT_1` (background) / `SLOT_2` (real-time) 論理名を定義。`BACKGROUND_SLOT=SLOT_1-1=0`, `REALTIME_SLOT=SLOT_2-1=1` — wire `id_slot`は0-basedのまま、人間向け命名は1-based
- `mcs/semantic.py` / `mcs/extract_llm.py`: `local_llm.BACKGROUND_SLOT`経由で変更不要

#### RT側配線 (Hermes → 論理slot 2)

- `~/.hermes/config.yaml` + 全10プロファイル (`profiles/*/config.yaml`): `custom_providers` Hermes Local entryに `extra_body: {id_slot: 1}` を追加 — provider経路の全呼出し (agent turn + auxiliary) がslot 2にpin
- `context_length: 65536 → 32768`: per-slot n_ctxの実値に整合 (Hermesが適切にcompactするよう実態反映)

#### launchd最終設定 (ai.hermes.llamacpp)

| 項目 | 値 | 理由 |
|---|---|---|
| `-c` | **65536** | per-slot 32768 — 実績p99=1168・最大43KのHermesプロンプトをカバー (8K/slotではRT側が破綻すると判明し再調整。旧3slot時22016/slotより大) |
| `-np` | **2** | slot 1=MCS背景専用 / slot 2=RT専用 |
| `--spec-type` | ngram-simple | acceptance 29%実績・メモリ増なし |
| `-ctk/-ctv` | **q4_0** | 32K×2slotでf16だとKV ~9.4GBと過剰。q4_0なら~2.3GB |

#### 新規LaunchAgents (稼働登録済み)

- `ai.hermes.llm-cleanup` — 毎日 **03:30** `~/.hermes/scripts/nightly_process_cleanup.sh` 実行: `ollama stop`でモデル全unload (on-demand reload) + 3時間超のorphan `chrome-headless-shell` kill。Chrome.app/gbrain reranker(2MB)は対象外
- `ai.hermes.llamacpp-restart` — 毎日 **04:00** `launchctl kickstart -k gui/501/ai.hermes.llamacpp` (長期稼働のメモリ膨張を日次リセット)

#### 実測検証

- `/slots`: slot 0/1 各 `n_ctx=32768`
- RT呼出し (`id_slot:1`) → wire slot 1着地 / MCS `semantic.llm_chat` → wire slot 0着地 (双方向確認)
- cleanup script実走: `qwen3-embedding:0.6b` graceful unload確認、ログ出力正常
- focused pytest 56件pass / ruff clean

#### 留意

- `kickstart -k`はlaunchd登録済み引数で再起動 — plist編集後は`bootout`+`bootstrap`が必要 (今回初回kickstartで旧argsのまま再起動しn_ctx=8192のままだった件で実証)
- prompt >32768の呼出しは依然overflowし得る (過去最大43K、発生頻度 ~0.2%)。さらなるheadroomが必要なら`-c`引上げかchunkingが別途必要

### 第7ラウンド: ローカルLLM設定の敵対的レビュー + モデル調査 (2026-09-23)

依頼「再度ローカルLLM設定で問題がないか多角的敵対的にレビュー。新しいLLMモデルがないか、性能として耐え得るものがないかを調査して」

#### 敵対的レビュー結果

| # | 確認項目 | 結果 |
|---|---|---|
| 1 | **未pinリクエストがslot 2を奪うか** | **奪う（実証）** — `id_slot`未指定は空きslotに着地（wire slot 1にtask着地確認）。予約は強制でなく慣行 → gbrainの`llama-server:Qwen3.5-9B`ルート(expansion_model/chat_model)が未pinでslot 2を汚染し得た。**修復**: `~/.gbrain/config.json`の`provider_chat_options`に`id_slot: 0`追加（AI SDK openai-compatibleがproviderOptionsをbodyにspreadする実装を確認済み） |
| 2 | `extra_body`のmerge semantics | **安全** — `chat_completions.py:578`で`request_overrides["extra_body"]`は`extra_body.update(v)`のdeep-merge。reasoning/thinking無効化と共存 |
| 3 | `id_slot`範囲外値 | **fail-openでサイレント** — `id_slot=5`/`-1`も受理されunpin扱い。設定ミスは検出不能（留保） |
| 4 | `context_length: 32768` vs 出力予算 | **安全** — `_compute_threshold_tokens`が`max_tokens`を織込み、overflow errorはcompaction経路で処理 |
| 5 | `probe_format`未pin | **修復** — request bodyに`id_slot: BACKGROUND_SLOT`追加（20tok probeでもslot 2を汚染し得た） |
| 6 | 04:00再起動がin-flight要求をkill | **修復** — `llamacpp_restart_if_idle.sh`で`/slots`の`processing`確認、ビジー時スキップ |
| 7 | llamacpp.log無制限肥大 (18MB→) | **修復** — cleanup scriptに50MiB超でローテーション追加 |
| 8 | KeepAlive+設定ミスのcrash loop | 留保 — launchdが10s間隔でスロットル。plist lint済みで現状安全 |
| 9 | 残りの未pin呼出し元 | `/v1/models` GETのみ（slot不使用）— `mcs_setup` health check等。問題なし |

#### モデル調査結果 (16GB M4・llama.cpp GGUF前提)

| 候補 | 評価 |
|---|---|
| **NuExtract3-4B** (numind, Q4_K_M 2.6GB) | 実bench検証済み: decode **8.2-9.5 tok/s**（9B比~1.5x）。3case中2件でcontract形状JSON生成、1件は1400tok切捨て失敗（9Bと同型）。ただし`validate_facts_doc`で`doc_version_mismatch` — **厳密contract非互換**。形状は合うが完全準拠せず。MCS専用抽出サーバ化するには prompt tuning or schema適合層が必要。ファイルは`~/.hermes/models/`に保持 |
| Qwen3.5-0.8B/4B draft spec | **upstream broken** — llama.cpp issue #20039: hybrid DeltaNetのstate rollback未対応で`partial sequence removal`不可。ngram-simpleが現実解（acceptance 29%実績） |
| Gemma 4 26B-A4B MoE | ~17-20tok/s報告(M5)だがQ4で~14GB — 16GB機では他processと併存不可 |
| LFM2.5-8B-A1B | ~30tok/sの報告あるがstrict-contract適性未検証 |

**結論**: 現行`Qwen3.5-9B`継続が妥当。NuExtract3は「速度+抽出特化」で有望だがcontract適合に改良要 — 本格評価は`bench/semantic_completeness_cases.json`全12caseの二モデル比較が次段階（要実行判断）。

#### 最終計測 (確定値)

`-c 65536 -np 2 -ctk/-ctv q4_0 --spec-type ngram-simple`: decode **6.3 tok/s**・wall 57.1s・prompt 4.8s（prefix cache後~0.3s）。swap 15.9GB→~4.7GB（最大圧力源解消）。

### 第8ラウンド: Hermes公式ルートへの移行 (2026-09-23)

依頼「モデルは変更しない。修正を継続。hermes公式ルートに可能な限り載せること」

#### 公式ルート移行

| 対象 | Before | After |
|---|---|---|
| 夜間掃除 03:30 | 独自LaunchAgent `ai.hermes.llm-cleanup` | **`hermes cron` job `nightly-llm-cleanup`** (`--script --no-agent`, 公式watchdogパターン, job id a892f0ae0cb1) |
| 04:00再起動 | 独自LaunchAgent `ai.hermes.llamacpp-restart` | **`hermes cron` job `llamacpp-daily-restart`** (job id f925ef37ae12)。実行体は`llamacpp_restart_if_idle.sh`(idle-check wrapper)のまま |
| RT pin | `extra_body` (既に公式knob) | 継続 |
| gbrain pin | — | `provider_chat_options` (gbrain公式config) |

独自plistはbootout+削除済み。`hermes cron list`で両job active・next run確認。

#### llama-server自体はlaunchd継続 (判断記録)

- `local_runtime` managed serverはrouter mode (port 18434・Hermes lifecycle依存・per-model child) — MCS/gbrain/Hermes共用の固定multi-slot構成と非互換
- 「自分のllama-serverを使う」は公式サポート済みパターン (`local-models.md`: "the managed runtime is a default, not a requirement") → launchd常駐が正しい境界

#### gbrain pinのwire検証 (完了)

- `com.gbrain.serve`再起動で`provider_chat_options.id_slot:0`を有効化
- MCP `synthesize`実呼出し (4340in/153out, `llama-server:Qwen3.5-9B`) が **slot 0に着地・slot 1 untouched** — gbrain→8080経路のpin実効確認
- これで8080の全既知消費者 (Hermes/MCS/gbrain) がpin済み: **slot 2のRT保護が実効**

#### 残存留保

- `id_slot`範囲外はfail-openでサイレントunpin (llama.cpp仕様、検出不能)
- cron jobsはgateway依存 — gatewayはlaunchd KeepAliveで常駐するため実害小、catch-up windowあり
- NuExtract3 GGUFは`~/.hermes/models/`保持・不使用 (モデル不変更の指示)

### 第9ラウンド: pin回帰テスト + 完全検証 + README整合 (2026-09-23)

依頼「次に進む」— LLM関連変更の完全回帰検証とドキュメント整合。

#### 追加 (pin回帰テスト, `tests/test_local_llm.py`)

- `test_llm_chat_pins_background_slot`: `semantic.llm_chat`が`extra_payload={"id_slot": BACKGROUND_SLOT}`を渡すことをassert（`local_llm.chat`をfake化して引数捕捉）
- `test_probe_format_pins_background_slot`: `probe_format`のrequest bodyに`id_slot`が乗ることをassert
- `test_extract_llm_call_pins_background_slot`: legacy extract経路のpinをassert

#### README整合修正

- `README.md` ローカルLLM節が旧記述（`-np 3`・「id_slotを付けない」）のままだった → 現行構成（`-np 2`・論理slot 1/2・pin規約・`SLOT_1`/`SLOT_2`正本）に更新。手書き節（GENERATED marker外）のため直接編集

#### 完全検証結果

- `pytest`: **682 passed** (679 + pin回帰3件)
- `ruff check mcs/ tests/ scripts/`: clean
- `ci/gates.py`: **8/8**
- `ci/mine_gates --check`: clean
- `update_readme.py --check`: clean

#### 残存（ユーザー操作待ち）

- live ingestion実走行: macOS Keychain unlockが必要（検出は`keychain_locked`で正しくfail-closed済み）

### 第10ラウンド: 実処理状況の監査とバックログ消化改善 (2026-09-23)

依頼「実際の処理状況は？」→「全て正常動作するように改善」。

#### 訂正（前回報告の誤り）

- 「Keychainロックで新規取込み停止」は**不正確だった**。Keychainは確かに
  ロック中(`User interaction is not allowed`)だが、Chromeセッションが
  生存しており`auto_login`はCDP経由でトークン取得可能 — `token_cache.json`
  は10分毎に更新され、run 564で`new_messages:1`を実際に取得。
  fetchは正常動作中。Keychainロックの実害は**Chromeセッション失効時**に
  潜伏するのみ。

#### 実状態監査で判明した真の問題

- `extract_llm` backlog **11,582件** — tickの`llm_budget_cap=90s`で
  ~4-6件/run → 消化に約20日。実質停滞。
- `extract_qc` backlog **1,281件** — `run_due`の`max_jobs=4`で~3日。
- `semantic` pending 50件（shadow mode）。

#### 修復

1. **`extract_llm.py --all`に`--stop-after`追加** — catch-upウィンドウを
   時間制限付きに（04:00 llama restartにquiet枠を確保）。
2. **`--workers`デフォルト 3→1** — pin導入後は全callがslot 0に直列化
   するためworkers>1はサーバ側queueに乗るだけ。help記述も実態に修正。
3. **`run_check.py`: jobs-only runの`llm_budget_cap` 90→240s**、
   `run_due`の`max_jobs` 4→12（既存の`max_jobs=8 if jobs_only`
   パターンに整合。fetch skipでdeadline余裕があるため）。
4. **`extract_llm --all`ドレーナー即時起動**（nohup, PID 12709）—
   per-write lockでtickと共存、全call slot 0 pin、slot 1(RT)保全を
   `/slots`で実測確認。
5. **hermes cron `mcs-llm-catchup`**（job 535400e964b4、毎日01:10、
   `mcs_llm_catchup.sh`、2.5hウィンドウで03:40終了→04:00 restartと
   非競合、single-instanceガード付き）。
6. **`security set-keychain-settings`試行** — ロック中はrc=36で不可。
   自動ロック無効化はユーザーが一度unlockした後の操作となる（記録）。

#### 検証

- `pytest`: 682 passed・ruff clean・gates 8/8・mine_gates clean
- drainer実動作: `/slots`でslot 0 processing=True・task counter進行
  を実測（slot 1は untouched）

#### 残存

- Keychain自動ロック無効化: ユーザーによる1回のunlockが前提
- extract_llm backlog ~11.5Kは常駐drainer+夜間windowで数日で消化見込み

### 第11ラウンド: mcs/ フォルダ分離リファクタ (2026-09-23)

依頼「コード内を整理。フォルダ分けを全部行う」。

#### 方針: 物理分割 + flat import維持

`gates.py`が"flat-local imports only"を不変条件として強制しているため、
真のパッケージ化（全import書換）ではなく**物理分割+既存import無変更**を選択。
200+のimport文と38テストファイルをbyte-identicalで維持。

```
mcs/
  _mcs_path.py   # bootstrap helper（root唯一のimportable module）
  core/      init_data ledger local_llm maintenance mcs_util        (5)
  ingest/    mcs_adapter job_ops notifier run_check                  (4)
  extract/   extract extract_bench extract_llm rollup                (4)
  semantic/  semantic.py + semantic_* 20本                           (21)
  ops/       brain_export mcs_* view/stats/queries/requests/signals
             /setup/refstats request_loops summary_review            (11)
```

#### 機構

- エントリポイント16本に2行ブートストラップ: `sys.path.insert(0, <mcs root>)`
  + `import _mcs_path` — `_mcs_path.py`が全第一層subdirをsys.path先頭へ登録
  （prepend = local moduleがsite-packages同名をshadowしない）。bare
  `sys.path.insert`はruff E402免除のためlint cleanを維持
- `conftest.py`/`hermes_plugin/_adapter_modules`/`integration/`も同一idiom

#### 修正一覧

- 12箇所の旧bootstrap書換え + `__main__`4ファイル(mcs_view/semantic_blind/
  semantic_evaluation/semantic_observe)へbootstrap追加
- `mcs_refstats.py`: bootstrapがflat importより後にあった問題を順序修正
- `semantic_observe.py`: 関数内late import(3箇所)を検出しbootstrap追加
- `extract_bench.py`: `../bench` → `../../bench`（深さ1層増加対応）
- `ci/gates.py`: glob→rglob、`__pycache__`除外、`parent==MCS`→`is_relative_to`
- `update_readme.py`: glob→rglob、表記を`mcs/<sub>/<file>`へ
- `Makefile`/`install.sh`/`README.md`/`AGENTS.md`/`mcs_setup`docstring: 全パス更新
- `deployment/local.mcs-cmd.plist` + live plist（bootout/bootstrap再登録済）
- hermes scripts 4本: mcs_check/mcs_deep/mcs_llm_catchup/gbrain_nightly
- tests 2ファイル: subprocess経路の`mcs/mcs_view.py`→`mcs/ops/mcs_view.py`

#### 検証

- pytest **682 passed**（移動後全件）
- ruff clean（mcs/tests/scripts/hermes_plugin/integration）
- gates 8/8・mine_gates clean・readme --check clean
- 全16エントリポイント `--help` スモーク通過・`semantic_jev`自己再実行経路確認
- 実run_check起動→lock_held正常応答（新パスで構造疎通）
- 稼働中drainer(PID 12709)は移動前load済みimport群で継続動作、
  初バッチ43件done確認（backlog 11582→11535）

## ラウンド12: LLMボトルネック分析と改善（extract_llm backlog対策）

### 計測で確定したボトルネック構造
- slot 0の実容量 ≈ 93 call/h（extract_llm 53 + QC 40 で共有・実測一致）
- per-call内訳: prompt eval 128 tok/s・decode 5-6 tok/s（M4 16GBで理論~20+の半分以下）
- 主因: メモリ枯渇（swap 10.5GB・free 62MB）+ ollama embedding server常駐2.2GB + Metal競合
- ngram-simple spec decode: 実タスクacceptance 5-19%（JSON定型で有効）→ 保持。合成benchでは中立

### 却下した改善案（証拠あり）
- 短文(<50c)スキップ: 1,736件中19%が実ファクトを含む（40字で要介護認定+期限を抽出した実績）→ recall損失で不採用
- spec除去: A/B同等→実ワークロードで正のため保持

### 実装
1. `OLLAMA_KEEP_ALIVE=2m`をbrew plistに追加→ollama再起動（embeddingがidle時auto-unload・~2.2GB GPU/host解放）
2. extract_llm `--slot N`（id_slot上書き）+ `--shard I/N`（message_id%N分割・drainer間の構造的排他）
3. `mcs_llm_catchup.sh`をdual-shard化: shard1/2→slot1を夜間のみ貸与、shard0/2→slot0（既存drainer稼働時はskip）
4. 日中drainerを`--shard 0/2 --slot 0`で再起動（PID 43371）— nightlyとの重複排除
5. `--shard/--slot/--stop-after`は`--all`必須にガード（黙殺防止）

### 見込み効果
- 夜間window(2.5h)が~2x化: +~130件/夜 → 合計 ~1,400件/日 → 11.5K backlogを~8日→実質短縮
- メモリ解放効果はdecode速度に直結する可能性（要観測）

### 未実施（選択肢として保留）
- `-c 65536→49152`（KV~0.6GB削減、>24K promptがcontext-shiftで縮退 — 要判断）
- 夜間slot1へのQC drainer追加（現状QCはslot0共有）
- 86件の永久失敗メッセージの個別reset

## ラウンド13: 処理能力向上・品質担保の追加施策

### 追加実装（全て実機適用済み）
1. **tick↔drainer衝突排除**: `run_pending(oldest_first=True)`を追加しrun_check tickに配線 — drainer(DESC/newest)とtick(ASC/oldest)が逆方向から消化し、同一行の二重LLM処理を構造的に排除（方向分離）
2. **drainer常駐化**: `ai.mcs.extract-drainer` launchd job新設（KeepAlive・shard 0/2・slot 0）— 手動nohupを廃止し監督付き常駐化。oldest_firstの前提（drainer生存）を保証
3. **夜間window拡大**: `mcs-llm-catchup` 01:10→**22:30**・WINDOW_S 9000→18000（5h）— slot 1貸与時間を2倍化、終了03:30で04:00再起動と非競合維持
4. **`-ub 1024`**: prompt eval ~128→140 tok/s（~10%・僅少だが無害）
5. deployment/launchagents/に`ai.mcs.extract-drainer.plist`テンプレ+README更新

### 証拠で却下
- **content_hashメモ化**（~300件≒2.6%）: 同hashでもthread contextが異なり出力非同一 → 品質同一性が保てず却下
- **短文スキップ**: 実測で19%が真ファクト含有（前回・再確認）

### 残オプション（判断待ち・報告のみ）
- **全日polite slot-1貸与**: 各call前に/slots確認→slot1 idleなら借用。容量~2xだがRT着信時最大~1call(20-60s)待ち — RT保証とのトレードオフで未実施
- `-c 49152`: KV~0.6GB削減・>24K prompt（実績25件）はcontext-shift縮退
- QC drainerのslot1夜間追加（現状QCはslot0共有で律速はextract側）

---

## Round 13 — 残オプション全実行（polite lending・-c 49152・夜間QC drain）

ユーザー指示「残オプションを全て実行」により、前回報告のみだった3施策を全て実装。

### 実装

1. **`local_llm.request_slot()`**（core/local_llm.py）: `MCS_LLM_SLOT=<N>`
   環境変数でプロセス単位の wire id_slot を上書き。未設定時は
   `BACKGROUND_SLOT`（=0）で既定不変。`semantic.llm_chat` の呼出しも
   `BACKGROUND_SLOT` 直書きから `request_slot()` へ変更 — 全経路が
   1点で slot を決定する規約に統一。

2. **全日 polite slot-1 貸与**（extract_llm `--lend-rt`）:
   `_choose_slot()` が全 call 前に `/slots` を照会（2s timeout）、
   RT slot が `is_processing:false` なら借用。probe 失敗・busy は
   slot 0 へ fallback — サーバ停止中も stall しない。`--slot` との
   同時指定は refuse。RT 到着時の最悪待ちは ~1 call 分（実測では
   llama.cpp が slot を time-slice するため両 drainer 稼働中でも
   RT テスト呼出しは 3.3s で応答）。新設 launchd
   `ai.mcs.extract-drainer-rt`（KeepAlive・`--shard 1/2 --lend-rt`）
   で shard 1/2 を全日担当 — **背景容量 ~2x** が本施策の最大効果。

3. **`-c 49152`**（ai.hermes.llamacpp.plist・再起動済み）:
   per-slot 24576・KV cache ~0.6GB 解放でメモリ圧を緩和。
   >24K prompt（実績 ~25件）は llama.cpp の context-shift で
   縮退する可能性 — 全体のわずかな割合でトレードオフを受理。

4. **夜間 QC drain**（`semantic_drain.py --drain` CLI 新設）:
   ~2分 iteration 毎に run lock を取得して `run_due` を回すため
   15分 tick を最大 1 iteration しか遅延させない。
   `mcs_llm_catchup.sh` は extract shard 起動を廃止し（launchd
   drainer が全日カバー）、`MCS_LLM_SLOT=1 semantic_drain --drain
   --stop-after 18000` で extract_qc/semantic ジョブを slot 1 から
   消化。drainer 全滅時の gap-fill は残置。実機スモークで
   9バッチ×2件 done（~0.3s/job）を確認。deferred-only バッチ時の
   hot-loop を防止（done=0 なら 10s backoff）。

5. **ollama `OLLAMA_KEEP_ALIVE=2m`** の plist 正式化: 前回の設定が
   plist 未保存で失効していたのを `sh.brew.ollama.plist` の
   EnvironmentVariables に追加・再起動して恒久化。

6. 回帰テスト `tests/test_slot_routing.py` 新設（8件）:
   request_slot 既定/env/不正値、_choose_slot の override 優先・
   lend-idle・busy/probe失敗 fallback。

7. `ci/gates.py` の `LEDGER_WRITERS` に `semantic_drain.py` 追加
   （drain engine は正当な writer・`acquire_run_lock` 使用で
   writer_lock ゲートも適合）。deployment/launchagents/ に
   `ai.mcs.extract-drainer-rt.plist` 追加・README 更新。

### 検証

- pytest **694 passed**・ruff clean・gates **8/8**・mine_gates clean・README clean
- llama-server `-c 49152` で健康応答（/health ok）
- `launchctl list`: ai.mcs.extract-drainer + ai.mcs.extract-drainer-rt 両者稼働
- /slots 実測: slot 0 processing（shard 0/2）、slot 1 に rt-drainer が
  借用実行・RT テスト呼出し 3.3s 応答
- semantic_drain 実機 smoke: extract_qc 18件 done・lock 競合なし

### トレードオフ（報告義務）

- **RT最悪遅延**: polite lending 中に RT 呼出しが来ると最大 ~1 call
  （数十秒、実測は数秒）の queue 待ち。品質は無影響。
- **-c 49152**: >24K prompt の極長メッセージは context-shift 縮退
  （該当は ~25件/14.6K）。
- **夜間 slot 1 共有**: QC drain と rt-drainer が slot 1 を共用 —
  22:30–03:30 は両者 queue 分割、片方のみなら占有。

---

## Round 14 — 全体無駄監査（処理効率レビュー）

ユーザー指示「処理の無駄がないか全体確認」で全パイプラインを監査。

### 発見・修正した実害

1. **`llamacpp_restart_if_idle.sh` の JSON キー誤り（重大・長期潜伏）**:
   `/slots` のフィールドは `is_processing` だが `s.get('processing')` を
   参照 → busy 判定が恒に 0 → **04:00 再起動が in-flight call を毎日
   kill** していた（ログに SKIP 記録皆無）。kill された call は
   attempts++ → 86件の永久失敗の一因と推定。さらに修正後は24/7
   drainer 下で永久 skip 化するため、**bounded wait** に再設計:
   `is_processing` で10秒毎・最大15分 poll → idle gap（drainer の
   call間 ~1s）を捉えて再起動、15分経っても busy なら再起動実行
   （失うのは高々1-2 call・自動 retry で回復）— 衛生再起動の死文化を
   両方向で解消。

2. **`--all` drainer の終了→KeepAlive respawn churn**: queue 空
   （`left==0`）または selectable ゼロ（永久失敗のみ残る定常状態）
   で exit → KeepAlive が30秒毎に respawn → 毎回 DELETE+全件 SELECT
   を回す永久 churn。`left` が永久失敗86件を含むため `left==0` には
   永遠に到達しない設計も問題。**常駐 poll 化**: 該当条件で
   `sleep(120) → continue` — respawn churn 解消・新着メッセージも
   2分以内に pick up。`--stop-after` の gap-fill は従来どおり
   window 終了で exit。

### 監査済み・無駄なし（理由つき）

- **tick(ASC)↔drainer(DESC)**: shard 間は `message_id%2` で構造的排他。
  tick は unsharded だが逆方向のため、queue 中央残り ~65件以下でのみ
  同一行を選び得る — その場合も `_current()` が二重書込みを防止
  （LLM call 自体の重複は bounded・稀）。
- **QC/semantic jobs**: CAS 遷移（state+attempts+payload 全一致
  UPDATE）で二重 commit 不能 + run lock で同時実行不可。
- **QC seeding**: `run_due` 内 `_qc_seed(limit=32)`/iteration — 夜間
  drain loop で律速解消済み。
- **ollama**: KEEP_ALIVE=2m 実測で機能（15:24呼出→15:26失効予定）。
- **gbrain nightly(02:00)**: window 内で slot 1 共有だが深夜 batch
  同士の queue 分割は想定内。transcripts ingest は content-hash skip
  で再実行コストほぼゼロ。
- **`--lend-rt` probe**: call 毎の /slots GET は ~2ms・無視可能。
- **semantic_drain**: deferred-only バッチは 10s backoff（hot-loop
  防止済み）、`left==0` で queue_empty 終了。

### 承知の上で残置（正直な報告）

- **全メッセージ atomicity**: retry はメッセージ全 chunk を再処理
  （証跡完全性のため設計上の意図的選択）。
- **batch-50 途中 crash**: KeepAlive で再開するが完了済み call は
  in-memory 喪失 → 最大1 batch 分の再実行。
- **`left` に永久失敗行が残る**: cosmetic。drainer は poll 継続で
  問題なし。個別 reset は `mcs_requests`/SQL で可能（未実施）。
- **メモリ圧**: 16GB・swap ~7GB は依然高いが `-c 49152` と
  ollama keepalive で緩和中。ハード上限由来。

### 検証
pytest 694 passed・ruff clean・gates 8/8・README clean。

---

## Round 15 — `-c 49152` 退行の差し戻し + TIMEOUT 根本修正

### 検出された退行（Round 13 施策の実害）

`-c 65536→49152` 適用後、両 drainer が `failed:50` 連発。llamacpp.log で
`alloc: making room for prompt cache entry → cancel task` が40分で501件
確認 — per-slot n_ctx 24576 では retained prompt cache が context を
埋め尽くし、新着 task が running task の cache entry を evict →
実行中 task が cancel される。**即 `-c 65536` へ差し戻し・再起動、
cancel 停止を確認。**

### 副産物として発見した真の根本欠陥

cancel 調査中に判明: decode がメモリ圧下で **3-5 t/s** まで低下して
おり、legitimate な ~1K token 出力は ~250s 必要 — だが `TIMEOUT=90`
のため client disconnect → server cancel → **decode 済み ~90s 分を
全破棄 + attempts 消費**。これが従来からの散発的失敗の主因の一つ。

**`TIMEOUT 90→300`** に引上げ（コメントで根拠記録）。実測で
180s/646token の call が完走 — 旧値なら破棄されていた成果。

### 回収した backlog

- 永久失敗 error artifact 28件（attempts>=5・v2）を run lock 下で
  DELETE → 新 budget で再キュー化（drainer が自然に拾う）
- attempts=6 の行7件は tick↔drainer の同時選択 race の痕跡 —
  bounded で `_current()` が二重書込み防止済み、容認

### 最終構成（確定）

```
extract drainer  slot 0  shard 0/2  --all --slot 0        KeepAlive
extract drainer  slot 1* shard 1/2  --all --lend-rt       KeepAlive
                                     (*RT idle 時のみ借用)
semantic drain   slot 1  夜間 22:30-03:30  MCS_LLM_SLOT=1
llama-server     -c 65536 -np 2 -fa on -ctk/ctv q4_0 -ub 1024
TIMEOUT=300  OLLAMA_KEEP_ALIVE=2m  restart_if_idle=is_processing修正済
```

### 検証

- pytest 694 passed・ruff clean・gates 8/8
- server `-c 65536`・両 slot processing・cancel 消失
- drainer 新コード再起動済み（TIMEOUT=300 + 常駐 poll 適用）

---

## Round 16 — Keychainロックの完全自動化（.env fallback）

ユーザー選択: `.env`フォールバック導入（FileVault OFFの平文リスクを承知の上）。

### 実装

- `mcs_adapter._login_password()` 新設: Keychain → 失敗/locked時
  `env_value("MCS_PASSWORD")` へfallback。`keychain_locked` と
  `manual_required` の区別は維持（envも無い場合のみ各state返却）
- `mcs_setup cmd_init`: `MCS_SETUP_PASSWORD` 指定時に Keychain へ
  加えて `~/.mcs/.env` にも `MCS_PASSWORD` を併記（0600）。
  keychain書込み失敗でも .env は残る（reboot fallback が成立）
- `check_environment`: `MCS_PASSWORD` が .env にあれば locked/missing
  keychain は error→warning に降格（フォールバックで無人稼働可能）
- 回帰テスト5件: keychain優先・locked時env・locked+env無し・
  entry無し+env・checkでのwarning降格

### ユーザー操作（1回のみ必要）

`.env`への初回書込みはパスワード入力が必要 — 会話に載せないため
以下のどちらかを実行:

```bash
# A) keychainから自動コピー（unlock 1回で完結・再入力不要）
security unlock-keychain   # ログインパスワード入力
python3 - <<'PY'
import subprocess, sys, os
sys.path.insert(0, "/Users/yusuke/.mcs/mcs"); import _mcs_path
from mcs_setup import _env_write, ENV_PATH
pw = subprocess.run(["security","find-generic-password","-s",
    "mcs-adapter","-w"],capture_output=True,text=True).stdout.strip()
assert pw, "keychain read failed"
_env_write(ENV_PATH, {"MCS_PASSWORD": pw}); print("MCS_PASSWORD written")
PY

# B) 直接指定
MCS_SETUP_PASSWORD='<pw>' python3 mcs/ops/mcs_setup.py init --yes
```

### 検証
pytest pass・ruff clean・gates 8/8。現状はenv未設定のためlockedは
依然errorとして正しく報告される（設定後はwarningへ降格）。

### Round 16 追記 — 完了

ユーザーが `security unlock-keychain` + `scripts/keychain_to_env.py`
を実行し `MCS_PASSWORD` が `.env` に書込み済み（0600）。
`mcs_setup check` → **OK (0 errors, 0 warnings)** — keychain 問題は
解消。再起動後の完全無人稼働が成立（Keychainロックでも .env
fallback で auto_login が動作）。スクリプトは
`scripts/keychain_to_env.py` として恒久化（再実行・ローテ対応可）。

## 初期設定ファイル更新（再起動後の構成を正本化）

- `deployment/scripts/` 新設 — hermes cron wrapper 4本の正本を repo 管理化
  （`mcs_check`/`mcs_deep`/`mcs_llm_catchup`/`llamacpp_restart_if_idle`、
  `__PYTHON__`/`__REPO__`/`__DATA__`プレースホルダ）。従来 `~/.hermes/scripts/`
  のみに存在し drift リスクがあった。生成物と実機スクリプトは意味的に一致確認済み
- `deployment/launchagents/README.md` 全面更新 — 全ジョブ表（cron 4 + launchd 3）、
  コピー&ペースト可能なセットアップ手順（スクリプト配置→cron登録→plist install）、
  `is_processing` キー誤りの注意書き、ollama KEEP_ALIVE 手順を追加
- `mcs_setup check` — LaunchAgent 3件（drainer×2 + local.mcs-cmd）未設置時に
  warning を追加（容量系のため error ではない）
- `install.sh` 末尾案内を新構成に更新

## 全体レビュー(多角監査) — 発見と修復

**修復済み**:
- `~/.gbrain/nightly-maintenance.sh`(孤立旧コピー)のbrain_exportパスを
  `mcs/ops/`へ修正 — 実稼働の`gbrain_nightly.sh`は正しかった
- transport失敗の区別: `local_llm.chat(error_out=)`追加、
  connection-refusedは`_DEFERRED`(attempts非消費)・timeoutは従来通り失敗
  — 再起動/llama起動中のバッチがattemptsを踏み襲う設計を解消
  (既存endpoint_downガードもあるがDEFERRED経路でfailed計上も回避)
- `run_pending`結果に`deferred`追加 → drainerは全件deferred時30sバックオフ
  (旧来: `_next_retry=None`でexit→KeepAlive 30s churn再発の恐れ)
- `maintenance.rotate_log`をdrain log群(extract_drain[_rt]/extract_llm/
  semantic_drain)にも拡張 — 常駐drainerの無制限増大を防止
- 死骸清掃: `adapter/`(空dir)・`data/mcs.db`(0B)・`.env.save`・
  旧backup shm/wal・`mcs/.pytest_cache`/`mcs/.ruff_cache`
- docs SVG内の旧パス `mcs/mcs_view.py` → `mcs/ops/mcs_view.py`

**観測(対応保留・正直な報告)**:
- attachments 10GB・5268件 — 保持ポリシー無し(archive設計だが無制限成長)
- decode ~3t/s(16GB・swap~9GBのメモリ圧)が根本律速 → >900tok出力は
  TIMEOUT=300に届き失敗するケースが残る
- extract_llm 24h実績 ~708件/日(backlog ~6.8K) — 予測~1,500/日を下回る
  (再起動直後の失敗バッチ+メモリ圧の影響)
- gbrain側のnightly失敗(transcripts ingest/dream "legacy writer"拒否)は
  mcs管轄外だが併記

## 残課題対応(ユーザー判断)

- **①attachments 14日TTL**: `maintenance.prune_attachments`新設 —
  downloaded_at>14dのpayloadをunlink、`state='pruned'`+local_path=NULL
  (name/bytes/sha256/urlは保持)。pending/failedは不触。housekeeping
  stageで毎tick実行。`attachments_pruned`をrun結果に記録
- **②decode低速の原因特定**: llamacpp.log全履歴分析で —
  np=1時代 17.1t/s → np=2 16.6t/s → **np=3時代 7.4t/s** → np=2復帰後も
  7.7→5.9→3.7→**3.6t/s**と漸次低下。`-c 49152`(差戻し済)や`-ub 1024`
  ではなく、**PhysMem 15G中 13G wired・残73MB**の蓄積的メモリ圧が主因
  (Metal確保がwired計上・np=3期以降の他プロセス成長+swap蓄積)。
  再起動後もswap 9GBで回復せず。恒久策はHW増設のみ、現状は
  backlog消化による自然緩和待ち
- **③実効~708/日**: 様子見(安定後に再計測)
- **④gbrain nightly失敗**: mcs管轄外として様子見

## canonical有効化前修正 C01–C07(監査報告対応)

- **C01** `semantic_projection._mid()`: v2 JSONのmessage_id(string)を
  projection境界でint正規化 — evidence/bundleのintキー照合が
  文字列idで常に失敗していた。回帰: test_canonical_projection
- **C02** `semantic_drain._process_job_inner`: `v2_docs_by_target`新設 —
  複数target処理時に最終memberのv2_docが全targetのmandatory_renderに
  漏れていた。回帰: test_canonical_drain(2target・別薬剤で隔離検証)
- **C03** coverage不完全docの有界再開: 保存済みincomplete docは
  1回だけ再抽出(coverage_retry meta記録)。retry済はneeds_reviewで
  駐留し永久ループしない。回帰: resume→retry記録→park をE2E検証
- **C04** fact監査はevaluated:trueのみ再利用 — evaluated:falseの
  途中失敗artifactがverdict扱いで世代を固定するのを解消。
  回帰: evaluated:false seed→drainで再監査artifact確認
- **C05** `semantic.llm_chat`に`local_llm.acceptance_error`接続 —
  finish_reason=lengthのJSONパース可能応答もincomplete扱い(None)
- **C06** `mcs_queries._current_projection_id`: projection現行版を
  MAX(artifact_id)で一意化 — 同bodyの旧世代(旧policy/schema)が
  残存しても最新generationのみ読取。新しい空projectionが旧非空を
  正当に置換。rollup dirty監視にcanonical_projection追加で
  世代変更→再生成を連動。回帰: 2世代projectionで最新のみ選択
- **C07** care_event射影に型付きゲート: polarity=negated・
  非patient主体(family/person等)・workflow cancelled/on_holdを
  keyword分類の前に除外 — 否定退院・家族退院・計画取消が
  患者イベントに混入しない。回帰: 5ケース否定+1陽性

検証: pytest **757 passed** · ruff clean · README regen(69 test files)
· gates 8/8 · mine_gates clean · shellcheck clean

未解決(正直な報告): canonical有効化は依然ゲート未設定のまま。
実データでのE2E(shadow比較bench)は未実施 — 合成drain検証のみ。

## 継続レビュー(他エージェント並行変更との統合後) N01–N04 + R系

### N系 — 統合後に発見・修復した欠陥

- **N01/N02** `invalidated` projectionが読み側を素通り:
  `semantic_store.invalidate_projections`・ledger側
  `_invalidate_thread_projections` が `meta.invalidated` を立てても、
  `mcs_requests.candidates` の `has_projection` EXISTSとartifact選択が
  フラグを見ず、失効projectionがextract_llmをshadowし続け、かつ
  全projection失効時はELSE側`IS NULL`もfalseになり事実カバレッジが
  完全喪失し得た。`_current_projection_id`(mcs_queries)側は並行作業で
  既に`IS NOT 1`済。candidates側に invalidated 除外を適用した上で、
  「現行projection妥当性述語」を`mcs_queries.current_projection_pred`
  として単一ソース化 — `_current_projection_id`・`candidates`双方が
  同一片を共有し、error/malformed projectionによるshadow喪失経路も
  閉塞(N02のdrift元を構造的に排除)。
- **N03** `semantic_metrics.current_quality`: `members[mid]`が
  cross-project parent等のデータ異常でKeyError → status_report全体
  停止の余地。`.get`+`thread_member_missing` reasonへ変更。
- **N04** `semantic_drain._process_job_inner`: `fact_source`をmember
  ループ外へhoist(scfg由来の定数を毎iteration再取得していた)。

### QC realtime-only の再適用(ユーザー指示との衝突を解決)

- 並行エージェントの書換えで`QC_REALTIME_MAX_AGE_S`ゲートが消失し、
  全期間QC(`test_seed_preserves_archive_coverage`等が旧投稿のQCを
  許可)へ変更されていた。ユーザー明示指示「QCはリアルタイム処理のみに
  適用する」が優位と判断し、改善済みの`source_artifact_id`世代bind
  設計を維持したままゲートを再適用:
  - `_qc_seed`: `COALESCE(m.posted_at_ts,0) >= now-3日`で絞込み
    (posted_at不明はfail-closedで除外) + 窓外投稿のpending jobを
    seed毎にDELETEでreap
  - `_process_qc_job`: 処理時点で窓外へ老化したjobはJev request
    不发・artifact不書で`done`化
  - テスト3本をrealtime-only仕様へ反転
    (`test_seed_skips_archive_posts`/`test_seed_reaps_stale_archive_jobs`/
    `test_process_qc_job_skips_aged_post`)
- **記録上の齟齬**: review-20260923.mdは「QC F15–F17 全期間の
  未登録仕事を公平に処理」を意図的決定と明記。アーカイブ全期間QCを
  望む場合は設定knob化が妥当 — 現状はユーザー指示のrealtime-only。

### 新規モジュール監査(並行作業分)

- `mcs_transport.py`: 子プロセスworkerで絶対期限・env allowlist・
  stderr遮断・kill+reap。`_loopback_url`でcdp系をloopback限定、
  api opは`API+"/"`prefix限定 — 健全。
- `semantic_metrics.py`: 履歴artifact集計と現行世代品質を分離 — 健全
  (N03のguard追加のみ)。
- `semantic_observe.py`: read-only snapshot、config非供給時は
  `config_not_supplied`でfail-closed — 健全。
- `mcs_adapter._io`: 全network経路(cdp_json/cdp_eval/api/download)が
  bounded_call経由でdeadline伝播 — 健全。

検証: pytest **829 passed** · ruff clean · README --check clean ·
shellcheck clean · gates **8/8** · mine_gates --check clean ·
git diff --check clean

未解決(正直な報告): コミット/プッシュ/デプロイ未実施。実MCS/Jev/
Discord/Keychainへの実E2E未実施(全テスト合成)。O01バックアップ復元
手順・O02臨床状態統合・O03添付OCRは未実装のまま。通知のexactly-onceは
非保証(不確実配送はholdで照合待ち)。canonical有効化ゲート未設定。
