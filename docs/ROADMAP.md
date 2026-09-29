# hermes-mcs ロードマップ

改訂日: 2026-09-29（v1.0.6 / `f947042` 時点）
対になる文書: zaitaku-calender `ROADMAP.md`。本改訂と同時に作成し、両文書を同じ変更単位で追加する（2026-09-29 時点で zaitaku-calender の main・作業ツリーのどちらにも `ROADMAP.md` はまだない）。接続フェーズの ID（C0〜C4）、未決事項の番号（Q1〜Q9）、契約版は両文書で共通。
旧版（Oracle レビュー統合版、機能候補 v2 30 項目）は git 履歴（`git show f947042:docs/ROADMAP.md`）を参照。変更の詳細は `CHANGELOG.md` を参照。

**ID の扱い**: 旧版にある ID は A1-x/B1-x/C1-x、懸念 C01〜C07、機能候補 v2 の #1〜#30 だけ。本書の「旧版の出典」列にある「優先順位 N」は、旧版「機能候補の優先順位（基盤修正完了後）」の N 番目を指す（旧版では番号 prefix なし）。本書では新しい ID を振らない。

## 1. 位置づけ

- hermes-mcs は **MCS から情報を取得して抽出する**ことに集中する。業務ワークフロー（依頼・フォロー・引継ぎ・報告書・処方正本・予定）の正本は zaitaku-calender。
- 最上位ゴール: 「取得漏れを検知できる fail-closed 長期アーカイブ」として承認されること。旧版の判定 CHANGES_REQUESTED はまだ解消していない（旧版 L8-13）。
- 接続では、取得した事実を**本文を含めず・未採用の候補として** zaitaku-calender に渡す。確定は zaitaku-calender 側で人が行う。

### 責務分担表（zaitaku-calender `ROADMAP.md` と同じ内容）

| 領域 | hermes-mcs | zaitaku-calender |
|---|---|---|
| MCS からの取得・欠落の検知・原本アーカイブ | 正本（`mcs/ingest/`、`mcs/core/ledger.py`） | 持たない |
| MCS 本文・添付の抽出（薬剤言及・症状・検査値・OCR） | 担当。出力は候補のみ（`mcs/extract/`、`mcs/semantic/`） | 抽出しない（LLM による事実生成・OCR による自動確定は OUT） |
| MCS 新着・緊急連絡のリアルタイム通知 | 担当（Discord/Slack。`mcs/notify/`（`notify_flush.py` 等）、シグナルの即時配信への昇格は `mcs/ops/mcs_signals.py:1043-1060`） | 外部リマインダーは追加しない |
| 患者・処方・臨床プロファイル・ケアチームの正本 | 持たない | 正本 |
| 予定・訪問・フォロー・引継ぎ・Work Queue・報告書・算定候補 | 持たない（依頼台帳は既存機能の範囲で凍結） | 正本 |
| 外部患者 ID ⇔ 内部患者の対応付け | project_id を送るだけ | 人が確定する append-only の対応表 |
| 取込データの採用 | しない | 薬剤師が既存画面で記録（出典付き） |
| 受領確認 | 既存の `GovernedExporter.reconcile` で held→acked（照合元を receipt ファイルに拡張） | 受領 receipt を返す（形式は C0 で固定） |
| 撤回 | 既存の `GovernedExporter.withdraw` で削除指示を送り、削除 receipt で照合 | 未採用 staging を削除して削除 receipt を返す。採用済み記録は残し出典の失効を表示 |
| MCS への書き戻し | しない | しない（SCP-07） |
| 逆方向（zaitaku-calender → hermes-mcs）のデータ | 受け取らない（receipt を除く） | 返さない（receipt を除く） |

## 2. 完了済み基盤（履歴）

詳細は git 履歴と `CHANGELOG.md` を参照。

- **Phase A 安全境界と起動**: keep_read_status の全経路注入、mark_as_read 応答の厳密検証、login origin 固定、通知先 fallback 廃止、redirect 拒否・proxy 無効、fresh DB 起動、migration 原子化、段階別 status（A1-1〜A2-3）。閲覧（`mcs/views/mcs_view.py`）と依頼台帳（`mcs/ops/mcs_requests.py:15-35`）も追加済み。
- **Phase B 取得完全性**: `coverage_ts`、MessageBatch、返信欠落と reply job、has_new_replies、history_floor、`fetch_jobs` 耐久キュー、返信添付、同一 tx 化、deadline 伝播、添付の隔離（B1-1〜B3-1）。
- **Phase C 復旧性・派生データ**: backup の quick_check と原子置換（C1-1。`mcs/core/maintenance.py:48`、`mcs/core/ledger.py:1858-1900`）、stale 判定、LLM 出力検証、backoff、rollup 修正、分割通知の progress 検証（C2-1〜C3-1）。
- **CCO 分離（A3）**: 読み取り専用 snapshot、`LedgerReader`、`publish_snapshot`、`generation_id`。接続の出典（どの世代の事実か）に使う。
- **外部出力契約の骨格**: `GovernedExporter` の deliver・`reconcile`（`mcs/ops/ext_contract.py:555`）・`withdraw`（同 :601）と journal 状態 held/delete_held/withdrawn（同 :23-26）は実装済み。CLI（同 :660-687）は `--auth/--records/--state/--sink` の deliver だけを公開し、sink は `LocalSink`（同 :327）のみ。
- **その他の完了**: 2026-09-19 追加検証（paginate 例外境界、schema_error、thread_incomplete、mark_result_unknown）、discovery job（C02）、proxy 非継承（C04）、添付保存名（C05、今後保存するもの）、mark_as_read の定期実行（2026-09-23 承認。旧版 優先順位 9）。

## 3. hermes-mcs の今後（優先度順）

| 順 | 項目 | 旧版の出典 | 内容 |
|---|---|---|---|
| 1 | 暗号化オフサイトバックアップと復元訓練 | v2 #30、Phase C C1-2、優先順位 1、追加検証「残件の優先順」（復元訓練） | 原本アーカイブの復旧点を確保。コード上に暗号化・復元訓練の実装はない（`mcs/`・`scripts/`・`deployment/` を `rg 'encrypt\|restore_drill'` で 0 件）。`CHANGELOG.md` の restore は更新復旧（mcs_update）の話で DB 復元訓練とは別物 |
| 2 | MCS API 取得契約の fixture 化 | 懸念 C01、追加検証「残件の優先順」（pinned 順序と thread 契約） | `sort=pinned` の順序、thread pagination、paginate 欠損を合成 fixture で固定 |
| 3 | 取得不能返信の分類 | 追加検証「残件の優先順」 | 欠落の理由を区別して記録する |
| 4 | 既存データの修復 | 旧版「既存データの修復順」節 | 独立 backup → A 系 → coverage/import job → 通知なし再照合（GET のみ）→ 返信・添付の補完 → hash/artifact 再生成 → rollup 再構築。history_floor は無条件に信用しない。**実施記録を残す。C1 の本番投入の前提条件** |
| 5 | 通信中の deadline | 追加検証「残件の優先順」 | 収集 tick の耐久性 |
| 6 | 通知受付直後のクラッシュ時の重複 | 追加検証「残件の優先順」 | 結果不明は照合に回す（C1 の held/reconcile と同じ原則） |
| 7 | FK/CHECK の導入 | 懸念 C06、追加検証「残件の優先順」（FK 導入） | 整合性監査の後に追加。`REFERENCES` は `mcs/notify/notify_cards.py` の通知系テーブルにだけある（13 箇所）。`mcs/core/ledger.py:122` で `PRAGMA foreign_keys=ON` だが、中核テーブルに FK はない |
| 8 | 編集履歴と命名 | 懸念 C07 | full→full で編集履歴が残らない。`current_med_period`／`possibly_deleted` を保証の強さに合わせて弱める（接続先で「確定」と誤読させない） |
| 9 | SQLite runtime 版の確認 | 懸念（SQLite runtime 版確認。旧版に ID なし） | WAL-reset 修正版（3.51.3+/3.44.6/3.50.7） |
| 10 | 独立実装レビュー（1 回） | 追加検証末尾「Oracle 実装レビューは未完了」 | v1.0.6 を対象に範囲を取り直す。不要と判断したら廃止 |
| 11 | 薬剤名正規化（drug_map） | v2 #3、優先順位 3 | 言及→候補まで。hermes-mcs 内部の正規化にとどめ、zaitaku-calender への依存・受け渡しは持たない。識別子の**体系だけ**を zaitaku-calender と合わせる（YJ コードなど）。マスター本体は zaitaku-calender から持ち出さない（医薬品 release は利用条件の承認が要る外部ゲート G-OPS-2。zaitaku-calender `docs/operations.md:151`）。独自に入手する場合は一次資料の利用条件を確認してオーナーが承認する。テスト用の辞書は完全合成のみ。自動確定はしない |
| 12 | 緊急度エスカレーション | v2 #18、優先順位 6 | 既存の昇格処理（`mcs/ops/mcs_signals.py:1043-1060`）の延長 |
| 13 | 日次 digest | v2 #1 | 範囲は MCS の新着、取得の未完了・失敗、urgency、open シグナル、未読の多職種連絡。**旧 #1 の「直近 72h の要約」は含めない**（件数と一覧まで。文章要約はしない）。期限・予定・依頼は含めない |
| 14 | 変更エピソードの紐付け | v2 #16 | MCS 投稿間の参照（変更の言及→観察の言及）。目的は誤検知の削減 |
| 15 | 検査値の抽出 | v2 #10 | 抽出のみ。時系列ビューは廃止（§6） |
| 16 | 添付 OCR と分類（ローカル） | v2 #9、優先順位 5 | 結果は未確定の候補 |
| 17 | 職種間やり取りの構造と応答時間の可視化 | v2 #20 | `interaction_links` を整備し、相談→回答の応答時間・未応答滞留を hermes-mcs 内部統計として可視化する。`st_open_loop_aging`（`mcs/views/mcs_stats.py:583-635`）は現在 formal requests だけを集計し、本文由来の候補は `text_candidates: unavailable`（同 :627-628）。`interaction_links` 整備後にここを埋める。本文由来候補は依頼台帳に書き込まない表示のみなので、台帳の凍結とは衝突しない |
| 18 | 業務負荷レポートと集計 | v2 #27、優先順位 7（集計部分） | 既存統計（doc_burden/workload/comm_concentration）の定期出力。旧 優先順位 7 の「高度集計（取得未完了範囲の明示付き）」をここに統合し、取得未完了の範囲を必ず併記する（PDF 報告書部分は zaitaku-calender へ移管、§5） |
| 19 | シグナル精度のフィードバック | v2 #28 | hermes-mcs 内部の人手ラベル（却下理由・採用率・再 open 率）だけで評価する。zaitaku-calender での採用・却下の取得は逆方向のデータ流（臨床判断由来の PHI）であり、双方向連携の契約（zaitaku-calender `docs/domain-model-decision.md:206`）と別承認が要るため C0〜C4 の範囲外（§6・§7） |

## 4. zaitaku-calender との接続

契約: 送付単位 `mcs-ext-export/1`、認可 `mcs-ext-auth/1`、record 型 `mcs-read-model/1`（`mcs/ops/ext_contract.py:43-44`、`mcs/ops/export_schema.py:106-148`）。本文・statement・evidence 引用・送信者・患者名・病名は、`export_schema.py` の record ごとの allowlist（`_SCHEMAS`、同 :116-148。未知キーは拒否）によって構造的に送れない。`FORBIDDEN_KEYS`（`ext_contract.py:46-52`）は、よくある本文系キーを早く見つけるための診断用拒否リストにすぎない。スキーマを変える場合（C2 を含む）は allowlist の変更として扱い、レビュー対象とする。

**受け入れる record 型（両文書で共通）**: `meta`・`coverage`・`message`・`signal`・`signals_truncated`。`attachment`・`stat` は C1 では送らない（`mcs-ext-auth/1` の `fields` で除外。`fields` を省略すると全 7 種が許可される: `ext_contract.py:201`）。
- `meta` は snapshot 時刻・世代の入力で、zaitaku-calender の snapshot 時刻表示と `max_snapshot_age_s` 超過判定に使う（`ext_contract.py:197-199`、CLI は generated_at を meta record から取る: 同 :678-679）。
- `coverage`・`signals_truncated` は「未取込・取得未完了を『不明』と表示する」ための入力。`auth.patients` を患者リストに絞ると、この 2 種は envelope から除かれる（`ext_contract.py:230-235`）。C0 で「C1 は `patients: "all"`」とするか、「coverage のない envelope は zaitaku-calender 側で全範囲を『不明』と扱う」かを決める。

**同一性キー（C0 で合意。両文書で同じ文言）**:
- `message`: item の粒度は message 単位で、`facts[]` は配列のまま保持する。同一性 = `type`＋`project_id`＋`message_id`＋`content_hash`。同じ kind の fact が 1 message に複数あってもよい（`facts: [_FACT]`、`export_schema.py:137-146`）。
- `signal`: 同一性 = `signal_type`＋`project_id`＋`evidence` の正規化 JSON の sha256。`detected_at` は属性として持ち、キーに含めない（signal には message_id・content_hash がなく、evidence 内の id は任意: `export_schema.py:123-135`）。
- `meta`・`coverage`・`signals_truncated`: envelope ごとの状態として 1 件ずつ保存し、同一性は `envelope_id`。
- `fact_id` は文言で変わるため参照用にとどめる（`mcs/semantic/semantic_facts.py:185-205`）。

**未決事項を決める時点（両文書で同じ文）**: C0 で決める Q＝Q1・Q2・Q3・Q4・Q6・Q7、C2 までに Q5・Q9、C3 前に Q8。

| フェーズ | hermes-mcs 側の成果物 | 依存 |
|---|---|---|
| **C0 契約合意（両 repo 共同）** | 旧 #25（iCal）を廃止して本接続に置換。受け入れ record 型・同一性キー・受領/削除 receipt の JSON 形式・分割上限（1 MiB = 1,048,576 バイト、UTF-8 の JSON 本文）を合意。合成 fixture 1 組（受理: meta・coverage・message（同じ kind の fact 2 件を含む）・signal、拒否: 禁止キー・未知フィールド・hash 不一致、receipt 例）と sha256 を固定。**完了条件に Q6 のオーナー判断の記録を含める** | zaitaku-calender C0（同時に実施） |
| **C1 手渡し取込（未採用 staging）** | `ext_contract.main()`（`mcs/ops/ext_contract.py:660-687`）に: `--only-with-facts` 前段フィルタ（`records_sha256` はフィルタ後に計算。coverage・meta は常に残す）、1,048,576 バイト以下への envelope 分割（各分割に meta・coverage をどう付けるかは C0 で決定）、`fields` の既定を C0 合意の 5 種にし meta・coverage を欠く envelope は作らない。既存の `GovernedExporter.withdraw/reconcile`（`ext_contract.py:555, 601`）を CLI サブコマンドとして露出し、照合元を `LocalSink` から zaitaku-calender の受領/削除 receipt ファイルに拡張。health に held 件数と認可の残り日数。返信の検知は新しい record 型を作らず、`message.parent_id` と `pharmacist_request_unanswered` 等の既存 signal で表す（`export_schema.py:124-127, 137`）。`tests/ops/` の合成テスト、`scripts/update_readme.py` | C0。本番投入（実データでの最初の envelope 作成）は §3-4 修復の実施記録と Q6 の判断後のみ。合成 fixture による開発・テストは Q6 と切り離して進めてよい |
| **C2 採用導線** | **`docs/external-export-contract.md` の改訂（`scope` は `aggregate` 固定で `detail` は無条件拒否: L22、aggregate の定義: L117、「No detail-scope export path, under any authorization.」: L137）とオーナーの明示承認が前提。改訂されるまで hermes-mcs 側の C2 成果物はなし**。改訂された場合のみ `mcs-ext-auth/2` で型付き値（allergy・ADE・vital_lab の数値・単位・日付）を allowlist の変更として投影する。原文は送らない | zaitaku-calender C2、Q9（契約改訂）。zaitaku-calender C2 は hermes-mcs C2 を待たない |
| **C3 マシン送信（任意）** | HttpsSink（stdlib urllib、redirect 拒否、proxy なし、token は Keychain から取り argv に出さない）。実行は手動のまま | Q6 の決定、Q8、契約上の endpoint・receipt 照会仕様（C0 または C3 で合意する契約付録）。相手フェーズへの相互依存は持たない |
| **C4 hermes-mcs 通知の縮退（任意）** | Discord/Slack カードから患者名を外し通知だけにする（`mcs/notify/notify_flush.py`、`notify_render.py`）。§3-13 日次 digest を zaitaku-calender の Work Queue で代替できるか再評価（代替に Work Queue の変更が要る場合は Q5 の決定に従う） | zaitaku-calender C2 の運用実績、Q5 |

共通ルール:
- 実行は手動のみ。tick に組み込まない（`docs/external-export-contract.md:136`）。
- 結果不明は held のまま receipt と照合する。自動再送しない。
- 同一性は上記「同一性キー」に従う。
- `facts[]` は semantic 層の current な成果物（`semantic_facts_v4`、なければ `canonical_projection`）があるときだけ埋まる（`mcs/views/read_model.py:209-218`）。`canonical_projection` も semantic 層の artifact で、`config.json` の `semantic` 設定によるゲートがある（`mcs/semantic/semantic.py:1-15`）。facts が空でも、message の存在・状態（`content_hash`・`body_state`・extraction 状態・`parent_id`）、coverage、signal は届く。

## 5. zaitaku-calender へ移す・既に実装済みの機能

hermes-mcs では今後作らない。「旧版の出典」は v2 #N と優先順位 N。

| 旧版の出典 | 項目 | 扱い | 根拠（zaitaku-calender） |
|---|---|---|---|
| 優先順位 2、v2 #2 | 依頼 lifecycle・Discord 完結 | **Discord 完結は廃止**（zaitaku-calender の `src` に Discord への参照はない）。依頼・期限・再開は zaitaku-calender の follow-up/handoff と Work Queue で扱う（担当は患者の担当薬剤師。follow-up event に担当者の列はない）。hermes-mcs の依頼台帳は、担当・期限・状態が既存（`mcs/ops/mcs_requests.py:15-24`、検証付き編集は同 :99-111）。新しい lifecycle 機能（ack 状態・Discord 完結・返信の自動検知による状態変更）は追加しない。既存の `request_overdue`/`request_aging` signal（`mcs/ops/mcs_signals.py:790-791`）と `st_open_loop_aging`（`mcs/views/mcs_stats.py:583-635`）は凍結台帳の範囲で維持する（廃止する場合は別途判断）。MCS スレッドの返信は §4 C1 のとおり `message.parent_id` と既存 signal で渡す | `src/worker/routes/visits/visits.ts:83-101`、`migrations/0065_visit_follow_up_events.sql:15-45`、`src/shared/domain/work-items.ts:12-17` |
| 優先順位 7（PDF 部分）、v2 #4 | PDF 報告書・訪問報告ドラフト | 訪問ごとの報告書・PDF は実装済み。**月次まとめは zaitaku-calender G-RPT-1（P1、設計 gate 待ちで未着手）**。hermes-mcs はドラフト生成をしない。集計部分は §3-18 | `src/worker/routes/reports/reports.ts:34-36`、`src/shared/reports/*`、`docs/plans/implementation-plan.md:153, 244-269` |
| v2 #6 | 確認済み現行薬リスト | 実装済み（処方正本） | `src/worker/routes/prescriptions/*` |
| v2 #22 | 引継ぎサマリ | 訪問単位の引継ぎ記録は実装済み。**患者の現状・注意点・未解決事項を 1 ページにまとめるサマリは未実装**（必要なら G-BCP-1 の持ち出しリストと合わせて zaitaku-calender で検討） | `src/app/features/resources/VisitHandoffPanel.tsx:32-34`、`docs/plans/implementation-plan.md:145, 207` |
| v2 #24 | 複数薬剤師対応 | 担当の割り当て（`assigned_pharmacist_id`）と役割ベースの権限は実装済み。**職員別の不在・代理は G-SCH-2（P1、未着手）**。hermes-mcs 側の通知ルーティング・個人別ダイジェストは §6 で廃止 | `src/shared/authorization-policy.ts`、`src/shared/domain/work-items.ts:17`、`docs/plans/implementation-plan.md:147` |
| v2 #26 | 算定・報告提出状況 | 一部実装済み（訪問単位の残務 `billing_confirmation_pending`・`care_manager_report_pending`・`physician_report_pending`）。**月次の追跡は G-BILL-2（P2、未着手）。算定要件未達（回数上限）の判定は OUT** | `src/shared/domain/work-items.ts:12-14`、`docs/plans/implementation-plan.md:156, 318` |
| v2 #5, #11, #12, #14, #15, #17 | トレーシングレポート、腎機能用量、相互作用、疑義照会台帳、リコンシリエーション、アドヒアランス介入 | zaitaku-calender の候補。hermes-mcs は既存抽出で得られる言及を C1 の facts として渡すだけ | zaitaku-calender `ROADMAP.md` §5 |
| v2 #19 | 終末期の麻薬準備・変化点検知 | 麻薬の準備は zaitaku-calender G-NAR-1（P3）。hermes-mcs は専用の変化点検知を作らない。eol は既に `extract_llm` の events にある（`mcs/extract/v4/extract_llm.py:92`）。read-model の fact kind（`care_event` 等）として eol を区別して渡せるかは C0 で確認し、渡せない場合は新しい record 型を作らない | zaitaku-calender `ROADMAP.md` §5、`docs/plans/implementation-plan.md:160` |
| v2 #8, #13 | 訪問前ブリーフ、症状×副作用照合 | 接続で扱う（C1/C2）。`next_planned` を予定の根拠にしない | `src/app/features/visit-workspace/VisitClinicalReferences.tsx` |
| v2 #21 | 関係職種ディレクトリ | **接続では扱わない**。送信者は allowlist にない（`export_schema.py:116-148`）ため送れず、read-model に職種・組織の record もない。zaitaku-calender の既存 care-team 手入力で吸収する | `PatientCareTeamPanel.tsx`、`src/worker/routes/patients/patients.ts:86-88` |

既存機能の扱い:
- **Discord/Slack カード**: MCS 取得通知として維持する（チャネル単位の既存配送のまま）。患者名の除去は C4。
- **signals（11 種）**: MCS 内部のシグナルとして維持し、C1 で `signal` record として渡す（`export_schema.py:123-128`）。
- **依頼台帳（`mcs/ops/mcs_requests.py`）**: 既存機能（担当・期限・状態・人承認ゲート `--confirm-human`・`reason`・receipt）のまま凍結。新しい lifecycle 機能は追加しない。

## 6. 見直し・廃止した項目

| 旧版の出典 | 項目 | 理由 |
|---|---|---|
| 優先順位 4、v2 #23 | Markdown timeline・読み取り専用 Web UI | UI は zaitaku-calender にある。PHI の表示面と認証境界を増やさない |
| v2 #10（時系列ビュー部分） | 検査値の時系列ビュー | 同上。hermes-mcs は抽出のみ（§3-15）。zaitaku-calender でも候補にしない（zaitaku-calender `ROADMAP.md` §5） |
| 優先順位 8 | 複数端末への snapshot 配布 | PHI の置き場所が増えるだけ |
| v2 #7 | MCS 返信ドラフト | 取得の範囲外。LLM による臨床文生成になる |
| v2 #24（hermes-mcs 側） | 通知の担当者別ルーティング・個人別ダイジェスト・担当不在時の代理 | 担当と不在の正本は zaitaku-calender（G-SCH-2）。hermes-mcs はチャネル単位の既存通知を維持し、担当者別の配送は作らない（オーナー確認: §7） |
| 優先順位 2、v2 #2（Discord 完結） | カードボタン⇄依頼台帳の完全連動 | 依頼の正本は zaitaku-calender（§5） |
| v2 #25 | iCal 出力 | 接続（C0〜C4）に置換 |
| v2 #29 | FHIR JP Core 出力 | 廃止（hermes-mcs でも zaitaku-calender でも作らない）。zaitaku-calender の G-EXT-1 は取込 adapter で FHIR 出力ではない（`docs/plans/implementation-plan.md:162`） |
| v2 #28 の双方向化 | zaitaku-calender での採用・却下を hermes-mcs へ戻す | 逆方向の PHI の流れ。双方向連携の契約（zaitaku-calender `docs/domain-model-decision.md:206`）と別承認が要る。再開する場合は両文書で新フェーズとして合意 |
| 旧「並び順の考え方」 | 報告書・薬剤師動線を上位にする前提 | 役割分担で不成立。取得完全性 → 抽出品質 → 接続の順に変更 |

## 7. 懸念・未決事項（オーナー判断）

旧版から引き継ぐもの:
- C01／C06／C07／SQLite 版は §3 に残す。確定した不具合ではなく要検証。
- 本番の再取込・既存 floor の変更は未実施（旧版 2026-09-19 追加検証）。
- 独立実装レビュー（§3-10）を再実施するか。
- §6 の v2 #24（担当者別ルーティング等の廃止）を確定するか。

接続（zaitaku-calender と共通の番号。決める時点: C0 で Q1・Q2・Q3・Q4・Q6・Q7、C2 までに Q5・Q9、C3 前に Q8）:
1. 本文なしで足りるか（MCS で原文を開いて確認する運用）。本文が要るなら `docs/external-export-contract.md` の改訂という別判断。
2. semantic 層（`semantic_facts_v4` または `canonical_projection`）を常時動かすか。動かさない場合 facts は空。message の存在・状態、coverage、signal は届く。
3. kind×project_id の PHI としての扱いと保持期限。zaitaku-calender の PHI 保持契約（取込 source artifact は作成から最大 30 日: zaitaku-calender `docs/domain-model-decision.md:53`）の範囲内で決める（保持 = min(envelope の `retention_days`, 30 日)）。
4. 患者対応付けと採用を pharmacist に限るか、clerk にも許すか。
5. Work Queue に「未確認の staging 行あり」を導出コードとして足すか（zaitaku-calender `docs/plans/implementation-plan.md:318` との両立）。C4 の digest 代替の可否もこれに従う。
6. MCS から取得したデータを別システムへ転送・保存することが許されるか。根拠は zaitaku-calender `docs/domain-model-decision.md` の外部連携条項（接続先 ID と確認済み内部 ID の明示対応: L182、双方向連携の事前契約: L206）、MCS 利用規約、患者同意、院内規程。SHR-10／SCP-07（zaitaku-calender `docs/specs/visit-report-spec-v1.md:637, 82`）は「MCS への書き戻しをしない」ことの根拠としてだけ使う。**決まるまで C1 の本番投入（実データによる最初の envelope 作成と zaitaku-calender 本番へのアップロード）以降に進まない**。合成 fixture による開発・テストは進めてよい。
7. fixture の正本をどちらに置くか（片方に置いてコピー、または両方に置き hash で一致確認）。
8. C3 の構成（D1 直接 binding か RPC か、mTLS 必須か）と、zaitaku-calender の P0（G-OPS-3、G-ON-1 など）より先に接続へ着手するか。
9. 型付き値（allergy・ADE・vital_lab の値）の送付を認めるか。認める場合は `docs/external-export-contract.md` の detail 禁止条項（L22・L137）の改訂とオーナーの明示承認が要る。hermes-mcs C2 の前提。

## 8. 完了条件

- 取りこぼし・未完了・失敗を正しく記録し、再実行と復元で回復できること（旧版から維持。「もっと賢く要約できる」ことは条件にしない）。
- 復元訓練を 1 回以上実施し、記録が残っていること。§3-4 修復の実施記録があること。
- 接続: 取得未完了の範囲を zaitaku-calender 側でも「記録なし」ではなく「不明」と表示できること（入力は C1 で必ず送る `coverage`・`meta`・`signals_truncated`）。本文を送る経路が存在しないこと。結果不明の送付と撤回が held／delete_held として残り、receipt で照合できること。Q6 の判断記録が C1 の本番投入より前にあること。
