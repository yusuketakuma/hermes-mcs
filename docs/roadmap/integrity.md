# 整合性・命名・運用前提の詳細計画（#7〜#10）

2026-10-04版割当: 旧1.0.13〜1.0.15の残件は全て安定稼働版1.0.13へ集約。
成果物・CLI・受入の正本は[1.0.13開発計画](../development/RELEASE_1.0.13.md)。
当時の調査・設計例と現在の実装状態を区別し、既存実装は再実装しない。

### 2026-10-04追記: #7/#8/#9のローカル実装

[ledger_audit](../../mcs/core/ledger_audit.py)は`--db`で指定した静的DBをread-onlyに読み、
件数・分類とguard状態を返します。[ledger](../../mcs/core/ledger.py)にはartifacts・attachmentsの
投稿参照guardとschema9の本文なしhash/revision履歴（M1）があります。
旧DBのshadow件数がゼロでも観測期間が完了したとは扱わず、ownerの証拠確認と
明示的な有効化判断を必要とします。旧行の削除・修復や他FKへの拡張を受入済みとしません。

[doctor](../../mcs/ops/mcs_setup.py)は選択runtimeと配備recoveryのPython/SQLite版、
SDK配布metadataを診断します。metadata取得・合成互換試験は実SDK接続や配備証拠ではありません。
runtime canonical追随の統合は確認中です。以下の初期調査時点の「版確認コードなし」
等は歴史記述として保持し、本番監査・owner観測・実機ゲートを省略しません。

[`docs/ROADMAP.md`](../ROADMAP.md) の #7 FK/CHECK の導入、#8 編集履歴と命名、#9 SQLite runtime 版の確認、#10 独立実装レビューの詳細計画。

2026-10-03照合: 以下の「現状」・行番号・実測・固定commitは2026-09-29の調査記録。
現在の版割当・公開条件は[ROADMAP](../ROADMAP.md)を正とする。#7/#9は1.0.13、#8は未割当。
#8-M1→#4の修復実施→C1本番の依存は残るため、#4のplanやC1合成開発だけで本番投入できない。
#10の旧固定commit全体レビューを今回繰り返さず、依頼された1.0.11の変更と重要な既存経路を照合する。
末尾の即時着手順は当時の提案であり、現在の版割当を上書きしない。未決owner判断は今回実装しない。

- 基準: v1.0.6 のコード（2026-09-29 調査）。行番号はこの時点のもの。
- 表記: 【実行確認】= 合成入力で実行して確認、【未検証】= 実データ・実機・実 API に触れないと分からない点。
- 実験は `git archive` の複製と合成 DB だけで行った（実データ・実 DB は読んでいない）。オーナー判断は項目内で `#N-Dk` と呼ぶ（一覧は ROADMAP の「オーナー判断一覧」）。

## 0. 結論と旧 ROADMAP 記述の訂正

- **#9（S）**: 版数は旧 ROADMAP どおり（sqlite.org/wal.html）。hermes の Python は SQLite 3.53.1 で安全。ただし **recovery watchdog が使う `/usr/bin/python3` は 3.51.0 で影響範囲**。repo に版確認のコードは 0 件。
- **#7（M。real FK 化は L）**: 本題は「どの制約なら安全か」。中核表の FK は設計上不可のものが多い。価値が高いのは artifacts ↔ messages ↔ project の整合と attachments → messages。real FK は table 再構築と `schema_bump`（auto 更新不可）を伴うため、version 据え置きの guard trigger と、先行する読取り監査を推奨する。
- **#8（S〜M）**: 編集履歴は `_upsert_message` の 1 か所で hash chain を取れる（試作で全テスト green）。本文の保持は削除の意図と衝突するのでオーナー判断。命名は、`current_med_period` / `possibly_deleted` が外部出力に出ていないため、rollup と brain_export の範囲で足りる。接続に出るのは `signal_type` の enum 名。
- **#10**: 前提が陳腐化している。全体レビューは廃止し、v1.0.6 の末尾 18 コミットに絞った独立レビュー 1 回に置き換える。

| 項目 | 旧 ROADMAP / 依頼時の記述 | 評価と証拠 |
|---|---|---|
| #7 | REFERENCES は `notify_cards` だけ 13 箇所、`ledger.py:122` で FK ON、中核 FK なし | 正確（`notify_cards.py:107,115,116,119,127,130,150,178,190,191,197,205,222`）。ただし CHECK は既存（`mcs_requests.py:22,23,29,33`、`notify_cards.py:80,89,99,121,128,141,153,167,172,200,214`）。「導入」の対象は ingest の中核表に限る。`schema_bump` の制約と、reader の version allowlist の記載がなかった |
| #7 | SQLite は `ALTER ... ADD CONSTRAINT` できない | 部分的に陳腐化。3.53.0（2026-04-09）以降は CHECK と `ALTER COLUMN SET NOT NULL` が可（【実行確認】runtime 3.53.1 で ok。既存行を検証し、違反があれば `constraint failed`）。FOREIGN KEY は不可（構文エラー）。system の 3.51.0 は CHECK も不可。配備が混在するため依存できない。3.53 で書いた DB は 3.51.0 でも読み書きでき制約も効く（実測） |
| #8 | 接続先で「確定」と誤読させない | 不正確。`current_med_period` と `possibly_deleted` は `mcs-read-model/1` にも ext_contract にも出ない（【実行確認】`mcs/` 内の出現は `rollup.py` と `brain_export.py` だけ）。接続に出て過剰名になり得るのは `signal_type` の enum（`export_schema.py:123-127`）。note は集約スコープで落ちる |
| #8 | full → full で履歴が残らない | 正確。ただし full → deleted でも本文が消える（`ledger.py:1092,1097`、`tests/core/test_core_misc.py:258-275` で固定）点が抜けていた |
| #9 | 3.51.3+ / 3.44.6 / 3.50.7 | 正確 |
| #10 | 「Oracle 実装レビューは未完了」 | 陳腐化。原文は旧版の 2026-09-19 時点の修正が対象で、以後 `v1.0.0..v1.0.6` で 262 コミットあり、対象自体が置き換わっている（詳細は #10） |

## #7 FK/CHECK の導入（C06）

**目的**: patient 間の artifact 混入や孤児添付を DB 層で防ぎ、既存データの現況を読取り専用の監査で把握する。確定した不具合ではなく要検証（ROADMAP §7）。

**現状**

中核表と暗黙の関係:

| 表 | 暗黙の関係 | 判定の要点 |
|---|---|---|
| `patients`（`ledger.py:156`） | 根 | — |
| `messages`（:164） | project_id → patients、parent_id → messages（自己参照） | 下表 |
| `attachments`（:173、UNIQUE :228） | message_id → messages | 書込みは upsert 直後の同一 tx（`_save_tree`: :583-585） |
| `artifacts`（:192） | (message_id, project_id) → messages | 書込み 12 箇所（`artifact_add_tx`: :1808、`mcs_operations.py`、`rollup.py:342`、`mcs_signals.py:148` ほか）。message_id NULL は rollup と signal |
| `notify_outbox`（:179） | project_id → patients（NULL 可）。子表の FK は既存 | `event_id` は外部 spec の `intent_event_ids` が参照（`adapters/common/spec.py:63,139-142`） |
| `read_marks`（:188） | project_id → patients | 書込みは `run_check.py:458,461`（unknown / confirmed だけ） |
| `fetch_jobs`（:197） | project_id / message_id | sentinel あり: `job_add("discovery", 0)`（`job_ops.py:659`）、`message_id DEFAULT 0`（`ledger.py:201`）、UNIQUE(kind, project_id, message_id) |
| `runs`（:152） | なし | status は running / crashed / ok / partial / session_expired / failed |
| `requests`・`command_receipts`・`snapshot_meta`（`mcs_requests.py:17-34`） | requests → messages、receipts → requests | CHECK は既存 |
| `notification_*`（`notify_cards.py:77-231`） | FK 13 本。欠けているのは cards.project_id / root_message_id、render_parts.delivery_id / attachment_id | `restore_holds` は意図的に FK なし（:216-219） |
| JSON 内の参照 | `notify_outbox.payload.message_ids`、`fetch_jobs.payload.targets`、`artifacts.meta.hash` 等 | FK 不可。hash 束縛で担保（`mcs_queries.py:42-66`） |

migration 機構:
- `SCHEMA_VERSION=7`（`ledger.py:33`）。`Ledger.__init__`（:109-132）は、version が新しければ拒否（:114-116）→ 中断 migration の残骸を検査（:117-120）→ `_preflight` で曖昧な行を拒否（:134-148）→ `foreign_keys=ON`（:122）→ `_init`（:150-250）の順に進む。
- `_migrate`（:259-291）は `BEGIN IMMEDIATE` → `_migrate_body` → `user_version` 更新 → commit / 失敗時 rollback（Phase A の A2-2 で原子化）。`user_version` は DDL と一緒に rollback される（実測）。
- `_script`（:252-257）は `;` で分割するため trigger は流せない。FTS trigger は `_init` 末尾で `executescript`（:231-248）。
- `requests` と `notify` は module の SCHEMA を version 据え置きで追加している（:439-449。コメント :441-443 に「snapshot reader に拒否させない」）。reader の許可 version は `mcs_view.py:62-63` の `(5, 6, 7)`。
- `mcs_update.py:494-517` と `:1207-1216` により、SCHEMA_VERSION が上がる release は auto 適用を止められる（「人の receipt のみ」）。rollback は DB 復元で消失を明示（`docs/dev-records/auto-update-plan.md:556`、`docs/specs/lifecycle-spec.md:215`）。
- 既存の migration テスト: `tests/core/test_ledger_candidate_validation.py:10`（全 version を parametrize）、`tests/core/test_core_misc.py:133-186`、`tests/ops/test_mcs_features.py:59-66`。

【実行確認】（合成、runtime SQLite 3.53.1）:
- 既存の rename-first 手順（`ledger.py:330-369`）を FK 親表に使うと、子表の REFERENCES が `"..._old"` に書き換わる（`foreign_keys` の ON / OFF に関係なく）。
- `PRAGMA foreign_keys=OFF` は tx 内では無効。`BEGIN` の前に切り替える必要がある。
- `DROP TABLE messages` で FTS trigger（`messages_ai` / `messages_au`）と index が消える。`messages_fts` 本体は残る（standalone FTS5）。
- **AUTOINCREMENT の高水位が再構築で失われる**（`sqlite_sequence` 5 → 3、次の id は 4 で再利用）。`artifacts` は日常的に DELETE され、id は QC・通知・semantic が参照する。
- ingest 表への trigger は、`foreign_keys=OFF` の接続でも効く（`maintenance.py:177` の素の接続も含む）。

テスト影響（複製で trigger により各制約を再現し `tests/` 全体を実行。`integration/` は未実行）:

| 制約セット | 結果 |
|---|---|
| H1: attachments → messages と artifacts → messages | 20 failed（8 ファイル。fixture が未保存 message 向けに artifact / attachment を作る） |
| H2: H1 + artifact の project 一致 | 25 failed。追加の 5 件は `tests/views/test_structured_view.py:189-196` など、`artifact_add(project_id=2, message_id=1)` で混入を作り reader が拒否することを確認するテスト。現状は書込み時に混入を防げていない証拠 |
| H3: messages / read_marks / notify_outbox → patients | 288 failed + 20 errors |
| H4: 8 列の domain CHECK | 4 failed。すべて `attachments.state='done'`（本番は `downloaded` しか書かない） |

合成の micro-benchmark（indicative。負荷で変動）: guard trigger 3 本で artifact insert は 12〜20µs → 17〜22µs / 行程度。`artifacts` 90k 行 / 181MB の再構築は 0.9〜4.6 秒、WAL が約 190MB 増加。実 DB のサイズと負荷は【未検証】。

**設計方針（推奨は 3 段）**
- **Step 0**: 読取り専用の監査（先行）。
- **Step 1**: version 据え置きの guard trigger。ingest 表は再構築しない。
- **Step 2（任意）**: real FK / CHECK は、次に不可避な schema bump に相乗りする。

判定表:

| 制約 | 判定 | 理由 |
|---|---|---|
| artifacts (message_id, project_id) → messages（message_id NULL は免除、project_id NULL は存在だけ検査） | 採用（最優先） | reader が個別に防御している（`mcs_queries.py:64`、`read_model.py:148`）。DB 層で構造的に防ぐ |
| attachments.message_id → messages | 採用（監査 0 の後） | ingest 経路なので enforce は監査後 |
| read_marks.status、messages.body_state（NULL 可）、counters ≥ 0 | 任意 | テスト影響なし（H4 の違反は attachments.state だけ） |
| attachments.state、notify_outbox.state、fetch_jobs.state、runs.status の列挙 | 見送り | 状態が進化中（`pruned` / `withdrawn` の追加履歴）。table CHECK は変更のたびに再構築が要る |
| messages.project_id → patients | 不採用 | 308 テスト破壊。本番は patient 先行のはず（`init_data.py:113,158`、`job_ops.py:179`）だが要監査 |
| messages.parent_id → messages | 恒久不採用 | 親未取得の返信は「取得漏れ」として記録すべき状態。FK だと同一 tx の batch ごと rollback し収集が止まる |
| fetch_jobs の FK | 不可 | sentinel 0 |
| requests → messages | 不採用 | 元投稿の消失を許容する設計（`docs/development/DEVELOPMENT.md:515-516`）。将来の PHI purge とも衝突 |
| command_receipts → requests | 不可 | rejected receipt は不存在 ID を持ち得る |
| notification_restore_holds の FK | 不可 | 巻き戻しで消えた行を意図的に参照する |
| notify_outbox の再構築 | 禁止 | `event_id` が外部 spec と子表 FK に参照される |

guard trigger の形（`_init` の FTS trigger 直後、`ledger.py:248` 付近に置く。`CREATE TRIGGER IF NOT EXISTS`、名前に世代 `g1_` を付ける）:

```sql
CREATE TRIGGER IF NOT EXISTS g1_artifacts_msg_ins BEFORE INSERT ON artifacts
WHEN NEW.message_id IS NOT NULL AND NOT EXISTS
  (SELECT 1 FROM messages WHERE message_id=NEW.message_id
     AND (NEW.project_id IS NULL OR project_id=NEW.project_id))
BEGIN SELECT RAISE(ABORT,'g1: artifacts message/project mismatch'); END;
-- 同条件で BEFORE UPDATE OF message_id, project_id
```

- 旧行は検査されず、新規書込みだけが対象になる（NOT VALID 相当）。誤検知時は `DROP TRIGGER` + 新名で差し替えられる。
- shadow 案: RAISE の代わりに件数ログ表へ INSERT し、N 日クリーンなら enforce に切り替える。ingest 表に対して推奨。

Step 2（real FK）の手順:
1. `PRAGMA foreign_keys=OFF` を BEGIN の前に実行する。
2. `BEGIN IMMEDIATE`。
3. 新表を作成 → 列の積集合をコピー → 旧表を `DROP` → 新表を `RENAME`（rename-first は使わない）。
4. index を再作成する（`idx_artifacts_lookup`、`idx_artifacts_kind_msg`、`uq_attachments_msg_file`）。
5. 旧 `sqlite_sequence` の値を `UPDATE sqlite_sequence` で復元する。
6. `foreign_key_check` が空でなければ ROLLBACK + `MigrationError`。
7. version を 8 に上げ、`mcs_view.py:62-63` の tuple を更新する。
8. commit の後に FK ON。

対象は `attachments` と `artifacts` だけ。`messages`（FTS と原本）と `notify_outbox` は触らない。

**成果物**
1. `mcs/views/ledger_audit.py`（AUDITS + read-only CLI、件数だけ出力）と `tests/views/test_ledger_audit.py`。新 module のため `scripts/development/update_readme.py` の再生成が必要。`ci/gates.py:223-263` により `mode=ro` で開くこと。
2. guard trigger（`ledger.py`）と `tests/core/test_ledger_guards.py`。fixture 修正 25 件（H2 の集合）。混入を作るテストは helper で `DROP TRIGGER` してから植える。
3. `CHANGELOG.md` と `docs/development/DEVELOPMENT.md`。
4. 監査件数だけの実施記録を `docs/dev-records/` に sanitized で残す。

監査クエリ（34 本を合成 DB で動作確認済み。抜粋）:

```sql
SELECT COUNT(*) FROM attachments a WHERE NOT EXISTS (SELECT 1 FROM messages m WHERE m.message_id=a.message_id);
SELECT COUNT(*) FROM artifacts a WHERE a.message_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.message_id=a.message_id);
SELECT COUNT(*) FROM artifacts a JOIN messages m ON m.message_id=a.message_id WHERE a.project_id IS NOT NULL AND a.project_id<>m.project_id;
SELECT COUNT(*) FROM messages m WHERE NOT EXISTS (SELECT 1 FROM patients p WHERE p.project_id=m.project_id);
SELECT COUNT(*) FROM requests r WHERE NOT EXISTS (SELECT 1 FROM messages m WHERE m.message_id=r.source_message_id AND m.project_id=r.project_id);
SELECT COUNT(*) FROM messages r WHERE r.parent_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM messages p WHERE p.message_id=r.parent_id); -- 情報のみ
PRAGMA foreign_key_check; PRAGMA quick_check;
```

読取り専用での実行方法:
- 対象は `data/snapshots/ledger-snapshot.db`（journal=DELETE の静的ファイル、tick ごとに再公開）または `data/backups/ledger-YYYYMMDD.db`。live の WAL DB は開かない。
- 接続は `file:…?mode=ro&immutable=1`（`valid_mcs_db` と同じ: `ledger.py:1898`）+ `PRAGMA query_only=ON` + 単一 `BEGIN`。
- 出力は件数だけ（本文・氏名・ID 列挙なし）。実行はオーナー。合成 DB で、書込みが `attempt to write a readonly database` で拒否されることを確認済み。

**受入条件とテスト（合成のみ）**
- 監査: 植えた違反ごとに件数が一致する。クリーン DB は全 0。ro 接続で書込みが拒否される。出力に本文の sentinel 文字列が含まれない。
- guard: 違反は `IntegrityError`。正当な経路は通る（`save_patient`、`save_messages`、reply 付き添付、message NULL の rollup / signal、legacy の project NULL、attachments の upsert）。v1 / v5 / v6 fixture を開ける。旧違反行があっても open は失敗しない。再 open で冪等。`foreign_keys=OFF` の素の接続でも効く。`valid_mcs_db` が trigger 付き DB を受理する。
- 全体は `scripts/run_tests.sh`、ruff、`ci/gates.py`、`update_readme.py --check`。
- Step 2 だけ追加で: 実バックアップの複製での rehearsal（所要時間と WAL 増加を測る）、AUTOINCREMENT 高水位の保持、`foreign_key_check` と `integrity_check`、FTS の件数一致、中断の故障注入。

**依存・順序**: #9 を先に（重い書込みの前に SQLite の安全性を確認）。監査は #4（修復）の前後で 2 回。Step 1 は監査が 0 か説明可能になってから。Step 2 は #1（復元訓練）の後、次の schema bump に相乗り。`message_revisions`（#8）は別表の追加で version 据え置きなので干渉しない。

**規模**: Step 0 は S、Step 1 は M、Step 2 は L。全体としては M（Step 2 を除く）。

**オーナー判断・リスク**
- `#7-D1` Step 2 を許容するか（`schema_bump` のため auto 更新不可、rollback は DB 復元）。
- `#7-D2` 旧違反の扱い（容認か修復か。PHI 行の削除はしない前提）。
- `#7-D3` 実 DB の監査の実行と、結果の記録範囲。
- guard は DB に残るため、code を rollback しても消えない。誤検知時の緊急 `DROP TRIGGER` 手順を用意し、shadow を先行させる。ingest 表への enforce は、違反時に patient の batch 全体が rollback される可用性リスクがある。artifacts を先に、attachments は監査 0 の後に enforce する。
- 【未検証】実 DB の行数・サイズ・孤児数、本番の trigger 負荷、再構築の実所要時間。

## #8 編集履歴と命名（C07）

**目的**: 編集・削除の観測を残し、強すぎる名称を保証の強さに合わせる。

**現状**

上書きの実体:
- `_upsert_message`（`ledger.py:1051-1122`）で hash は `sha256(body_html)`（:1055）。この定義は初回 commit 6b532e5 から不変。
- full → full は無条件に上書きされる。`body_html`（:1091）・`body_text`（:1096）・`body_state`（:1101）・`content_hash`（:1110）が CASE で更新される。`updated_seen` だけが更新される（:1116）。snippet は full を下げない。
- full → deleted は本文を `''` に消去し、hash=sha256('')（:1092,1097,1111）。`tests/core/test_core_misc.py:258-275`（F02）で固定されている。
- `messages` の body 系は `_upsert_message` だけが書く（他は :482 の `body_text` 補完だけ）。捕捉点は 1 か所。

観測の限界:
- 編集を観測できる経路は reconcile だけ（`job_ops.py:43-50, 573-640`）。6h 周期、2 頁 / 回、2 患者 / 回。観測時刻は編集時刻ではなく、複数回の編集は 1 回に潰れる。#4 の再照合でも、最初の編集が痕跡なく上書きされる。
- MCS API が編集時刻を返すかは【未検証】（adapter は読まない）。

下流（staleness）:
- 現行判定は `meta.hash == messages.content_hash`（`mcs_queries.py:42-62`）。v1 は `_delete_stale`（`extract/v1/extract.py:310`）、v4 は `_replace_current`（`extract_llm.py:1534`）で古い artifact を消す。semantic は `_semantic_member` の revision = content_hash（`ledger.py:1351`）と `_invalidate_thread_projections`（:620）。通知は `_source_fp`（`notify_render.py:165`）、依頼は stale 表示（`mcs_view.py:388-390`）。履歴表は append-only でこれらは読まないため、影響なし。

文書の限界と乖離:
- `docs/development/DEVELOPMENT.md:279,515-517` は「hash だけでは編集前の原文は復元できない」と明記している。`docs/specs/semantic-evaluation.md:327` の「原文revisionは保存し続ける」は現実装（最新のみ）と乖離しているので、文言の整理が要る。

名称:
- `possibly_deleted`（`rollup.py:211-216`）は、`updated_seen` が「先頭メッセージの `updated_seen` − 21 日」より古い先頭 20 件。削除の証拠ではなく、tombstone（`body_state='deleted'`）も区別しない。reconcile が一巡していない履歴や NULL の旧行（`or 0`）も入る。挙動のテストがない（fixture の `[]` だけ: `tests/ops/test_brain_export.py:93,235`）。
- `current_med_period`（`rollup.py:142-145,186,220-268`）は、extract_v1 の規則正規表現による日付範囲のうち、start ≤ 当日 ≤ end かつ前後 16 字に 予定 | 検討 がない最初の 1 件。どの薬かは不明で、後続の「中止」で取り消されない（`med_state` とは独立）。
- 露出面: `brain_export.py:197-199,227-228`、`docs/guides/USER_GUIDE.md:73,313-315`、`docs/development/DEVELOPMENT.md:208-209`、`tests/extract/test_rollup_period.py:45,56,65,70,75`、`tests/ops/test_brain_export.py:84,93,235`。
- 接続側: `message` の allowlist は `export_schema.py:137-147`、`_BASE` の contract enum は :105。旧案の同一性`type+project_id+message_id+content_hash`は廃止され、論理キーに`content_hash`を含めない（[接続判断の正本](connector-decisions.md)）。hashは変化検知に使う。signal の note は「確定ではありません」と書くが（`mcs_signals.py:405-426`）、集約スコープでは出ないため、`rx_period_expiry` / `pharmacist_request_unanswered` / `med_change_no_followup` は enum 名だけが届く。

**設計方針**

(1) 履歴は M1 を推奨:

| 案 | 内容 |
|---|---|
| M0 | `messages` に `revision_count` と `body_changed_at` の 2 列（ALTER だけ、履歴なし） |
| **M1（推奨）** | 新表 `message_revisions`（hash chain だけ、本文なし） |
| M2 | M1 + 旧本文。オーナー判断 |

M1 の DDL:

```sql
CREATE TABLE IF NOT EXISTS message_revisions(
  message_id INTEGER NOT NULL, seq INTEGER NOT NULL,
  content_hash TEXT NOT NULL, prev_content_hash TEXT,
  body_state TEXT NOT NULL, observed_at REAL NOT NULL,
  PRIMARY KEY(message_id, seq));
```

- `_init` の `executescript` に追加する（既存 DB にも作られる。version bump 不要）。
- `_upsert_message` は既存行の `body_state, content_hash, first_seen` を読む。既存と新入力の両方が terminal（full / deleted）で hash が違う場合だけ記録する。**既存 hash が NULL の legacy 行は記録しない**（mutation で確認: この条件を外すと `message_revisions.content_hash` の NOT NULL 違反で収集経路が落ちる）。
- 初回の編集で seq=1（旧）と seq=2（新）を作り、A → B → A も連番で残す。INSERT は `OR IGNORE` 等で fail-soft にする（`_upsert_message` は収集の critical path で、例外は batch 全体を rollback する）。
- M2 を選ぶ場合の注意: `publish_snapshot` は DB 全体をコピーする（`ledger.py:1856-1889`）。別表にしても snapshot 生成時に DROP + VACUUM が要る。deleted の旧本文は F02 の意図と相容れない。

(2) read-model: `prev_content_hash`は追加しない（2026-09-29決定済み）。旧案の追加提案を再実施しない。messageの論理キーは維持し、payload hashによる版管理とsnapshot世代でcurrentを決める（[接続判断](connector-decisions.md)）。

(3) 命名: `current_med_period` → `med_period_candidate`、`possibly_deleted` → `unrefreshed_message_ids`（提案名）。brain_export の見出しと注記に「規則抽出・未確認」「削除の証拠ではない（tombstone は `coverage.collection.deleted`）」を入れる。USER_GUIDE と DEVELOPMENT を直す。`PERIOD_CHECK_VERSION` を 2 → 3 にする（`rollup.py:47`）。旧キーを持つ rollup は一度 tick で再構築される（`run_check.py:709-710`。全患者を 1 tick で再構築するコストは【未検証】）。brain_export は移行期に新旧キーの両方を読む。`possibly_deleted` の挙動テストを新設する（現状なし）。

(4) wire の enum（#8-D2）: A = 現行enum名維持を2026-09-29に決定済み。契約文書に意味と制約を明記する。旧案Bの改名は今回行わない。

**成果物**
- `ledger.py`（DDL + 約 20 行）と `tests/core/test_message_revisions.py`。
- `rollup.py` と `brain_export.py` の改名、docs（USER_GUIDE、DEVELOPMENT、CHANGELOG）、`possibly_deleted` の新テスト。
- (4) は `docs/specs/external-export-contract.md` への semantics 表。

**受入条件とテスト（合成のみ）**
- 遷移表: snippet → snippet、snippet → full（記録なし）、full の replay、full → snippet（無視）、full → full の編集（seq 1・2）、編集の replay（重複なし）、A → B → A、full → deleted、deleted → deleted、deleted → full、legacy NULL hash。
- 同一 tx で rollback される。旧本文がどこにも保存されない。snapshot に hash だけが載る。表のない旧 backup でも open できる（表が作られる）。
- 既存全体が green。試作では baseline 2617 に対し 2618 passed（NULL ガードの追加前の値。ガード追加後は core / ingest の 424 passed を再確認）。
- rollup の改名は、新旧キーの両読みと `dirty_projects` の再構築を検証する。

**依存・順序**: **M1 は #4 の再照合と C1 の本番投入より前**（先に入れないと最初の編集が痕跡なく消える）。命名と wire enum は C0 fixture の固定前に決める。`PERIOD_CHECK_VERSION` の bump は他の rollup 変更と相乗りする。

**規模**: M1 は S〜M、命名は S、wire enum は M（判断次第）、M2 は M〜L。

**オーナー判断・リスク**
- `#8-D1` 本文保持の可否と範囲（編集は保持し削除は purge、など）。
- `#8-D2` wire enum の A / B。
- 【未検証】同じ未編集投稿が endpoint 間で `comment` がバイト一致するか（偽編集）、API の編集時刻の有無、実 DB の NULL hash 行数。導入時は shadow カウンタを先行させる案もある。

## #9 SQLite runtime 版の確認

**目的**: WAL-reset 破損の版範囲外で ledger writer を動かし続ける。

**現状**
- sqlite.org/wal.html: 影響は 3.7.0〜3.51.2。修正は 3.51.3（2026-03-13）で、backport は 3.44.6 と 3.50.7。条件は WAL + 2 接続以上（別スレッド / プロセス）が同時に write / checkpoint すること。「タイミング依存で通常運用では起きにくい」との記述がある。3.45〜3.49 系には fix がない。
- MCS は WAL（`ledger.py:125`）で複数 writer プロセスが同時に開く（tick、resident drainer × 2、cmd / int）ため、前提は構造的に満たされる。手動 checkpoint は restore 経路だけ（`mcs_update.py:1727`、`mcs_recover.py:661`）で、writer 停止が前提。
- 実行 interpreter: `HERMES_PY`（`mcs_setup.py:1464-1465`）が `__PYTHON__` に展開される（:1889）。実機の drainer / cmd plist と `~/.hermes/scripts/mcs_check.sh:11` は `~/.hermes/hermes-agent/venv/bin/python`。gateway プロセスも同 interpreter。
- 【実行確認】（2026-09-29）:

| interpreter | SQLite | 備考 |
|---|---|---|
| `HERMES_PY`（3.11.15） | 3.53.1 | 安全 |
| Homebrew python3.14 | 3.53.4 | 安全 |
| `/usr/bin/python3`（3.9.6） | **3.51.0** | 影響範囲。使用箇所は `org.mcs.recovery.plist:8`（900 秒間隔） |
| `/usr/bin/sqlite3` CLI | 3.51.0 | 影響範囲 |

  - watchdog は通常 `mode=ro`、復元時だけ rw + checkpoint（`mcs_recover.py:638-662`）。Apple が backport 済みかは【未検証】。
- hermes-agent v0.19.0 は uv 管理 Python に SQLite 3.50.4 を同梱していた（NousResearch/hermes-agent#69784。2026-07-23 報告、PR #70055 で closed）。runtime の更新で版が変わり得る。
- repo に確認コードは 0 件（`sqlite_version` の grep）。`docs/guides/INSTALLATION.md:93` は Python 版だけ。plugin / gateway は DELETE journal の snapshot を ro で読むだけ（`mcs_view.py:65`、`hermes_plugin/projects.py:30-33`）で対象外。
- 既存の安全網は日次 backup と `quick_check`（`maintenance.py:47-77`、`ledger.py:1892+`、7 世代）。

**設計方針**
- `mcs_setup.py:383` の `check_environment` に probe を追加する（in-process と、HERMES_PY が別なら subprocess）。`docs/guides/INSTALLATION.md:251` の実行は HERMES_PY で、Makefile の `setup-check` は ambient python になるため、両方を見る。
- 判定は `v >= (3,51,3) or (3,50,7) <= v < (3,51,0) or (3,44,6) <= v < (3,45,0)`。重大度は **warn**（sqlite.org が緊急でないとしているため）。
- 古い場合の是正: (1) runtime を更新する（hermes 更新、または fixed 版の Python）。(2) resident agent を再起動する（`mcs_setup.py services`）。(3) 再度 `check`。
- 暫定対応: 日次 backup の quick_check に頼る。手動の `sqlite3` CLI で live DB に書かない（`-readonly` だけ）。DELETE journal への退避は最終手段（writer と reader が競合し、停止時だけ変更可なので、オーナー判断）。
- watchdog は stdlib だけ・system Python で動く独立性が要件のため、差し替えず「restore 時だけ rw」として記録する。

**成果物**
- `wal_reset_safe()` と `_sqlite_probe()`、`check_environment` への追記（約 30 行）。`tests/ops/test_mcs_setup.py` への追加。`docs/guides/INSTALLATION.md` に SQLite 要件の行と check 節（A-4）、`CHANGELOG.md`。

**受入条件とテスト（合成のみ）**
- 15 ケースの表: (3,51,2) F、(3,51,3) T、(3,50,6) F、(3,50,7) T、(3,44,5) F、(3,44,6) T、(3,45,0) F ほか。
- 3.51.2 で warn、3.51.3 で warn なし。HERMES_PY probe が 3.50.4 を返すと runtime の warn が出る。
- 既存の test double（`SimpleNamespace(returncode=0)` など）を壊さない。試作は `getattr(r,"stdout","")` で許容し、tests/ops・core・meta で 780 passed。
- 実機の `check` 実行は Keychain に触れるため未実施【未検証】。

**依存・順序**: 依存なし。最初に着手する。#7 Step 2 と #4 の長時間再歩行のゲートになる。

**規模**: S。

**オーナー判断・リスク**
- `#9-D1` warn か error か。`#9-D2` runtime 更新の時期。`#9-D3` `/usr/bin/python3` の 3.51.0 を restore 時だけのリスクとして容認するか（Apple の backport の有無は【未検証】）。

## #10 独立実装レビュー

**目的**: v1.0.6 の残余リスクを独立に潰す。不要なら廃止する。

**現状**
- 旧 ROADMAP の前提は陳腐化している。`v1.0.0..v1.0.6` は 262 コミット。独立レビュー起点の修正は済んでいる（`fbd8fa1` RA、`10ad126` RB、wave 1〜3 の todo 1〜21 は `0387d54`・`7369489`・`5719bc3`・`24cd552`）。dev-records に `review-20260923`・`review-20260925`・`refactor-20260927` がある。CI gates と約 2,600 テスト（v1.0.5 → v1.0.6 でテスト +9,192 / −1,586 行に対し production +3,466 / −710 行。assert 行数は 5,951 → 6,911 で、弱体化の兆候はない）。
- **最後の wave（`24cd552`、2026-09-29 04:11）以降の 18 コミット（`24cd552..1162f3d`）は、独立レビューの記録がない**。範囲は production 40 ファイル、+999 / −493 で、tag の直前 3 時間に集中している。内訳:
  - `mcs_update.py` +285 / −79
  - `mcs_recover.py` +199 / −25
  - Slack actions と confirm の一本化（`47bef09`、`83463d5`、`d8af787`）
  - `mcs_util.launchd_bootstrap`（`6a31b08`）
  - `journal.py` の差分読み（`fe85b0c`）
  - fail-closed 化（`623f1a6`）
  - 「挙動不変」の refactor（`3e8f4f2`、`abd0c94`、`fe45632`）
- v1.0.5..v1.0.6 全体（net）は 199 ファイル、+13,048 / −2,380、109 コミット。net 上位は `mcs_update.py` +318 / −76（2,324 行中）、`mcs_recover.py` +261 / −37（1,058 行中）、`journal.py` +218 / −22（317 行中 76%）、`worker.py` +195 / −26、新規 `semantic_lifecycle.py` 278 行。
- 危険度の根拠: update / recovery だけが DB を置換し得る（`_replace_database` は `mcs_update.py:1704` と `mcs_recover.py:638` に同一コードが重複）。実 launchd・git・pgrep・Discord SDK は synthetic テストで証明できない。
- 作者は 109 コミットのうち 79 が Claude Opus 5.5 の co-author（Devin は 2）。「独立」は Claude と別系統（Codex / Oracle か人）と定義する。
- v1.0.6 は 2026-09-29 に配備済みのため、レビューで P0 / P1 が出た場合は修正リリースを前提とする。

**設計方針**
- **推奨**: 全体レビューは廃止する（前提の陳腐化・既存レビュー・review cascade の回避）。代わりに、対象を絞った独立レビューを 1 回行う。
- 範囲（優先順）:
  1. update / recovery / restore: `mcs_update.py`、`mcs_recover.py`、`install.sh`、`mcs_util.launchd_bootstrap`。
  2. 人承認ゲートと配送: `registry.take_confirm`、Discord / Slack actions、`journal.py` の差分読み、`worker.py`。
  3. 「挙動不変」の refactor 3 コミットが本当に不変か（差分テストで確認）。
- 形式:
  - 対象は固定 commit（`f947042`）。reviewer は読取り専用で、Devflow の reviewer seat（codex）または Oracle。
  - 成果物は `REVIEW.md`。finding を ID・severity（P0 = データ損失・PHI 漏えい・ゲート迂回、P1 = 誤状態・無音停止、P2 = 堅牢性）・file:line・再現手順（合成）・修正案で並べ、「確認して問題なし」の一覧と【未検証】の一覧を付ける。
  - rubric は `AGENTS.md` の絶対ルール: 標準ライブラリだけ、実 MCS / Discord / Keychain / LLM へ非接触、PHI を repo に入れない、`--confirm-human` 経路、記録なし ≠ 対応なし。意味精度・臨床精度・性能は対象外。
- 終了条件: P0 / P1 が 0。各 P0 / P1 に失敗 → 成功の回帰テストがある。P2 は ROADMAP へ振り分けるか、根拠付きで容認する。必須チェック（`scripts/run_tests.sh`、ruff、`update_readme.py --check`、`ci/gates.py`、`ci/mine_gates.py --check`）が通る。実機項目（gateway 再起動、watchdog の再配置、launchd の状態）はオーナー担当として、未検証のまま明記する。sanitized な記録を `docs/dev-records/` に残す。
- 廃止する場合の論拠: 前提が陳腐化している。既存レビューが複数ある。review cascade を避ける（Scope Guard）。以後は変更単位のレビュー（#7 Step 2、C1 本番投入）を受入条件に組み込めば足りる。ただし、末尾 18 コミットは未レビューで不可逆な経路に触れるため、範囲を絞れば実施価値がある。

**成果物**: `REVIEW.md`（sanitized）と、finding ごとの回帰テスト。

**受入条件とテスト（合成のみ）**: 上記の終了条件。再現は fault injection（launchctl・git・pgrep の異常、TTL 切れ、二重クリック、journal 巻き戻し）で行い、実 Discord / launchd には触れない。

**依存・順序**: C1 の本番投入前に完了させる。他項目と並行できる（コード変更なし）。

**規模**: M（レビュー工数）。

**オーナー判断・リスク**
- `#10-D1` 全体レビューを廃止し、縮小版を採るか。`#10-D2` reviewer の系統と席（Codex / Oracle / 人）。
- RA / RB / RD の成果物と範囲は repo に記録がなく【未検証】。

## この領域の実施順

1. **#9**（S）: 即時。他項目のゲート。
2. **#8-M1 + 命名**（S〜M）: #4 の再照合と C1 の本番より前。wire enum の判断と `prev_content_hash` の要否は、C0 fixture の固定前に決める。
3. **#10 縮小レビュー**: #8 と並行でよい。C1 の本番前に完了させる。
4. **#7 Step 0 監査**（S）: オーナーが read-only で実行。#4 の前後で 2 回。
5. **#7 Step 1 guard**（M）: 監査が 0 か、説明可能になってから。shadow → enforce。
6. **#7 Step 2 real FK**（L、任意）: #1（復元訓練）の後、次の schema bump と相乗り。

依存: #9 → (#7 Step 2、#4 の長時間再歩行) ／ #8-M1 → #4 → C1 本番 ／ #7 Step 0 → Step 1 ／ #1 → #7 Step 2 ／ #10 → C1。
