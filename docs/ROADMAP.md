# MCS Monitor — 開発計画 (Oracle GPT-6 Pro レビュー統合版)

Source: oracle session `mcs-system-review` (2026-09-19, gpt-6-pro verified,
thinking=Pro). 回答全文: `~/.oracle/briefs/mcs-system-review-answer.md`、
ブリーフ: `~/.oracle/briefs/mcs-system-review-brief.md`。
36の再現ケースで検証された指摘を一次コードで再確認済み。

## 判定: CHANGES_REQUESTED

基盤構成(API-first / SQLite / ローカル抽出 / outbox)は維持。ただし
「取得漏れを検知できる fail-closed 長期アーカイブ」としては未承認。
最大の問題: **「保存済みメッセージの最大時刻」を「そこまで欠落なく
取得できた時刻」として扱っている**。

### 2026-09-19 追加検証

- 未読・履歴とも、paginate検証を部分取得の例外境界内へ移動。
  後続ページの不正応答でも完了ページを呼出元へ返して保存できる。
- cutoff判定に使う不正日時をschema_errorとし、古い投稿と見なして
  history完了を誤認する経路を修正。再取得時の日時文字列とepochも同時に保持・更新する。
- thread応答が明示的に次ページを示す場合はthread_incomplete。
  ページ取得の実API契約は未検証であり、全返信取得の解決とは扱わない。
- 既読応答のdataが配列等でもmark_result_unknownを返し、AttributeErrorを防止。
- 回帰テスト40件成功。通信スタブ付きtick統合試験で、実SQLite・派生処理・
  backup・snapshot・2回実行時の重複防止を確認。本番再取込や既存floor変更は未実施。
- 残件の優先順: pinned順序とthread契約、取得不能返信の分類、通知受付直後の
  クラッシュ時重複、通信中deadline、復元訓練とFK導入。
- Oracle実装レビューはchrome-disconnectedのため未完了。

## Phase A — 安全境界と起動 (最優先)

### 閲覧・依頼管理の追加 (2026-09-19)

- `mcs_view.py`: 状況一覧、患者限定検索、親投稿/返信/添付の根拠付き閲覧、未確定依頼候補。
- schema v5: 人が確定したrequestsと原子的command_receipts。更新はrevisionと現行source hashで競合拒否。
- snapshotの世代情報と条件に束縛したcursor。完了記録と無欠落保証を区別し、原本DBを公開しない。
- ルール/LLM抽出は候補表示に再利用し、臨床依頼の自動登録・外部送信・自動完了は行わない。
- 旧schema移行、実SQLite競合、receipt失敗rollback、再送、CLIからキュー・公開後参照まで隔離試験済み。
- 反映先で47件の試験とRuffが成功。新規3機能のOracle再レビュー
  `mcs-three-features-fix-review` はPASS（既存の履歴完全性等まで承認した判定ではない）。
- 不明日時がwriter再openでepoch 0になる経路を修正。既存の0は元日時で再判定し、実在するepoch 0は保持。
- 通常の22:45 tickでv5移行、22:47に新snapshot公開・quick_check・実CLIのstatus参照を確認。
  既存の取込はpartialのまま正しく表示し、実患者の依頼レコードは作成・変更していない。
- 操作例と制約はREADME「閲覧・依頼管理」を参照。上記の履歴完全性等の未検証残件は引き続き残る。

| ID | 問題 | 修正 | 検証済 |
|---|---|---|---|
| A1-1 | `keep_read_status=1` が unread 取得のみ。thread/history/latest に無い | 全メッセージ取得経路へ共通注入 | ✅コード確認 |
| A1-2 | `mark_as_read` の `200 {}` を confirmed 記録 | 応答契約を厳密検証(`project.is_unread is False` 以外 unknown)、intent 先行保存、mark_read 既定 unknown | ✅コード確認 |
| A1-3 | `_login_page` が host 無検査で `authentication/login` 含む tab を選択 → 資格情報を別 origin へ注入し得る | scheme+host+path 厳密固定、入力/submit JS 内でも location.origin 再確認 | ✅コード確認 |
| A1-4 | 通知先が `DISCORD_HOME_CHANNEL` へ fallback;`--no-notify` でも失効/失敗 flush が全 outbox(患者本文含む)を排出 | 承認済み宛先のみ固定、設定不備=送信停止、no_notify を全経路適用(運用通知は別契約) | ✅コード確認 |
| A1-5 | Discord 送信が redirect 追従で Bot token を別 origin へ漏洩し得る | proxy無効+redirect拒否+origin固定の専用 opener | ✅コード確認 |
| A2-1 | **fresh DB が起動不可**:`_migrate` が存在しない `messages.last_seen` を無条件参照 | 列存在確認、schema version 管理 | ✅コード確認 |
| A2-2 | migration 非原子的(attachments rename 後の copy 失敗で旧データ取り残し) | 全体を1トランザクション化、中間状態の復旧判定 | ✅コード確認 |
| A2-3 | backfill失敗/deadline超過/extract・rollup・notify・backup失敗でも `status='ok'`・exit 0 | 段階別 status、最外周の例外境界、error は型/codeのみ記録 | ✅コード確認 |

## Phase B — 取得完全性と耐久 job

| ID | 問題 | 修正 | 検証済 |
|---|---|---|---|
| B1-1 | **watermark 欠落固定化**:新しい未読保存で MAX(posted_at) が進み、それ以前の未回収が backfill 対象外に | `patients.coverage_ts` = 確認済み coverage。backfill は coverage-overlap まで歩き、完全歩行時のみ coverage=seen_max へ前進。`coverage_lag` で欠損を可視化 | ✅実 run で backfilled=1 を確認 |
| B1-2 | 複数ページ取得の途中失敗で成功済みページも保存されない | `MessageBatch(messages,pages,reached,error)` で完了ページと失敗を同時返却し、成功分を先に保存 | ✅回帰テスト |
| B1-3 | 返信欠落判定が無効(`unread_ids - set(merged)` は常に空) | `ReplyBatch.missing` と共通 `MergeResult` で full 本文だけを完了扱い。未取得返信は耐久 reply job へ | ✅回帰テスト |
| B1-4 | 古い親への新返信が cutoff break で捨てられる | reply preview の最新時刻を検査、cutoff 超過の返信を持つ親は `has_new_replies` フラグ → 全 thread 取得 | ✅コード確認 |
| B1-5 | 返信取得失敗・deadline でも history_floor を確定 | floor 確定条件 = 歩行完了 AND reply job 残0 AND deadline 未超過(init_data + history job 双方) | ✅コード確認 |
| B1-6 | 深掘りは floor 残存時に毎回 page 1 から再開し進まない | `fetch_jobs` 耐久キュー。job は payload 内 cursor で再開、1頁 overlap で頁シフト吸収。完了 job の再要求は ON CONFLICT で復活 | ✅単体検証 |
| B1-7 | **返信の添付が attachments に登録されない** | `save_messages` が親・返信共通で attachment upsert(UNIQUE index 重複排除済み) | ✅コード確認 |
| B1-8 | save_patient commit → outbox_add が別 tx | `save_patient(notify=...)` が messages+attachments+outbox を同一 `with self.db` tx へ | ✅コード確認 |
| B2-1 | cmd 実行前 unlink・`int()` で全体例外・要求喪失 | 厳密 `_valid_cmd`(type int 限定/範囲検証)→ `job_add` 受理後に unlink。失敗は run errors へ記録 | ✅コード確認 |
| B2-2 | 480s deadline が全体に効かない | deadline を backfill/jobs/attach/extract/LLM へ伝播、LLM 予算は残時間から算出 | ✅コード確認 |
| B3-1 | 添付失敗が pending 残留+`.part` 残存 | `attempts/next_try/error` 列 + attempts 上限で `failed` 隔離、download は `.part`→rename + 全失敗経路 cleanup | ✅コード確認 |

## Phase C — 復旧性・派生データ・通知

| ID | 問題 | 修正 | 検証済 |
|---|---|---|---|
| C1-1 | 当日 backup が存在すれば 0-byte でも再作成しない | tmp→`PRAGMA quick_check`→`os.replace`。壊れた既存 backup は作り直し | ✅コード確認 |
| C1-2 | restore drill 未実施 | 未着手(機能候補の暗号化 backup と合わせて実施) | ⬜ |
| C2-1 | hash 欠損(NULL)が stale 判定をすり抜け | `hash IS NULL OR hash != content_hash` で stale 扱い(v1/llm 双方)。CLI 経路も hash 記録 | ✅実 run で 639 件再抽出を確認 |
| C2-2 | LLM 任意 dict 受理 → rollup TypeError 停止 | `_validate()` で型/enum/有限数値/長さを検証、不合格は成功 artifact にしない | ✅コード確認 |
| C2-3 | 一時 LLM 失敗が永久処理済み化 | `meta.error/attempts/next_try` 付き error artifact、backoff<5回 retry、旧 content._error は移行削除 | ✅コード確認 |
| C2-4 | **rollup が否定症状を残す** | sym_pos/sym_neg を日時比較で統合 — 新しい否定が同/包含語の肯定を解決 | ✅コード確認 |
| C2-5 | rollup 更新対象漏れ + DELETE→INSERT 非原子 | `dirty_projects()`(msg/artifact 新着検出)+ `with db:` 原子置換 + meta.generated_at | ✅dirty→rebuild→0 確認 |
| C2-6 | 年末日付で年跨ぎ逆転 | `_ymd(mode=past/future)` で posted_at 基準の年シフト、期間は start≤end 検証 | ✅コード確認 |
| C2-7 | posted_at 欠損で epoch 0 上書き | `_upsert_message` は posted_at_ts=0 なら既存値保持(※複合 cursor は timeline 検索が pk 順のため現状不要と判断) | ✅コード確認 |
| C3-1 | 分割通知の途中失敗で成功済み chunk 再送 | `outbox.progress{next,sent,fingerprint}` で送信表現を検証。不一致/不正進捗は保持して保留、429指定時間を短縮しない | ✅回帰テスト |

## CCO 分離 (A3 — 構造変更) ✅実装済(2026-09-19)

- ✅ 原本 `data/ledger.db` の mount 廃止 → `data/snapshots/ledger-snapshot.db`
  (ro) + `data/cmd/`(rw, inbox のみ) + `adapter/`(ro)。
- ✅ `LedgerReader`(mode=ro、migration なし、書込は sqlite 層で fail-closed)。
- ✅ `publish_snapshot()`: backup→tmp→DELETE journal化→schema/quick_check→atomic rename。終了済みrunを公開。
- ✅ Docker Desktop 経由の live WAL 共有を解消(C03)。
- ✅ snapshot 内へ generation_id/generated_at を追加。未完了状態は
  `mcs_view status` が `patients`/`messages`/`fetch_jobs`/`attachments` から表示。

## 懸念 (要検証 — 確定不具合ではない)

- C01: `sort=pinned` の順序保証・thread pagination・paginate 欠損の扱い — API 契約 fixture で固定
- C02: ✅解決 — `kind='discovery'` job が1日1回 `/projects` を列挙し、未読を
  出さない患者も自動登録 → `trickle` 深掘り(since=0)を seed。全93患者中
  43件が新規発見済み(2026-09-19)。全履歴は `local.mcs-deep`(07/37分
  `--jobs-only`)+ tick 余剰容量で漸進取得中
- C04: ✅解決 — extract_llm/notifier は ProxyHandler({}) の専用 opener で
  loopback/Discord 固定、proxy 継承なし
- C05: ✅今後の保存名は ledger `attachment_id`。送信前に実サイズと保存SHAを検証
- C06: FK 制約が未定義(PRAGMA だけ)→ 整合性監査後に FK/CHECK 追加
- C07: full→full は編集履歴を残さない;`current_med_period`/`possibly_deleted` の命名が保証より強い
- SQLite runtime 版確認(WAL-reset 競合修正は 3.51.3+/3.44.6/3.50.7)

## 機能候補の優先順位 (基盤修正完了後)

1. 暗号化 off-site backup(端末喪失対策、live DB 同期ではなく検証済み復旧点)
2. 依頼 lifecycle(open→ack→done、根拠 message 紐付け、操作者確定型)
3. 薬剤正規化(言及→候補→確認済み分離、規格/用量/単位/期間/開始中止)
4. Markdown timeline + 読取専用 UI(出典・世代表示)
5. 添付 OCR/画像説明(添付取得完全化が前提、ローカル処理)
6. 緊急度エスカレーション(shadow 運用から — 否定/時制修正が前提)
7. PDF 報告書・高度集計(取得未完了範囲の明示付き)
8. 複数端末(読取専用 view/snapshot 配布から)
9. mark_as_read 意味論検証(検証しても手動限定は維持、自動化は別途明示承認)
   → 2026-09-23 明示承認により定期実行に `--mark-read` を追加
   (mcs_check.sh・local.mcs-cmd.plist)。snapshot_ts 必須・
   fetch_state=complete ゲート・intent 先行記録は維持

## 機能候補 v2 (2026-09-28) — 薬剤師負荷軽減・多職種連携 30項目

v1.0.5 時点の実装(収集・2レーン抽出・rollup・16統計・11シグナル・
人承認依頼台帳・Discord/Slack カード)を前提に、次に開発する候補を
「薬剤師の負荷削減の直接性 × 既存資産で作れるか × 他機能の前提になるか」
で順位付けしたもの。上の9項目リストは経緯として残す(重複項目は本表が優先)。

いずれも現行の設計原則を維持する: 機械は候補提示まで、確定・登録・
外部送信は人承認(`--confirm-human`+`reason`+receipt)のみ、
「記録が見つからない ≠ 対応がなかった」。

### Tier A — 既存資産で即効性が高い

| # | 機能 | 内容 | 既存資産 / 依存 |
|---|---|---|---|
| 1 | 患者別・日次ブリーフィング | 毎朝「今日確認すべきこと」を患者単位に1通: open シグナル、未回答の薬剤師宛依頼、期限、次回予定、直近72hの要約 | signals digest tier、rollup、notify_outbox を束ねる |
| 2 | 依頼ワークフローの Discord 完結 | カードボタン⇄依頼台帳の完全連動(担当・期限・完了・再開)。スレッド返信の検知で「返信済み」を自動提示(確定は人) | card actions、mcs_requests、command_receipts |
| 3 | 薬剤名正規化辞書(drug_map) | 商品名→一般名/成分・規格・剤形の辞書と表記ゆれ統合。「ロキソニン/ロキソプロフェン」を同一薬として集計・追跡 | README 明記の未整備項目。#6/#10〜13/#16 の前提 |
| 4 | 訪問薬剤管理指導 / 居宅療養管理指導 報告書ドラフト | 月次で医師・ケアマネ宛報告書の下書きを患者別に生成(経過・薬剤変更・服薬状況・提案)。evidence リンク付き、人が確認して提出 | timeline、rollup、extract_llm summary/points |
| 5 | 服薬情報提供書(トレーシングレポート)ドラフト | 残薬・アドヒアランス・副作用疑いなど医師へ情報提供すべき事項を候補化し文書ドラフト化 | adherence_concern、symptom_after_med_change |
| 6 | 現行薬剤リストの「確認済み」台帳 | rollup の薬剤言及を mention→candidate→confirmed の3層に分け、薬剤師が確定した現行処方リストを保持(開始日・用量・処方元・根拠) | 旧候補3。requests と同じ人承認+receipt 経路 |
| 7 | MCS 返信ドラフト支援 | 依頼・質問投稿に対し根拠付きの返信案(確認事項・薬学的提案・注意点)をカード上に生成。コピーして人が投稿、自動送信なし | ローカルLLM slot 2(RT)、semantic_render |
| 8 | 訪問前ブリーフ | 抽出した `next_planned` の前日に、訪問先患者の変化点・確認事項・持参物候補をカードで提示 | extract_v1 next_planned、rollup |

### Tier B — 薬学的介入の質を上げる

| # | 機能 | 内容 | 既存資産 / 依存 |
|---|---|---|---|
| 9 | 添付 OCR・自動分類(ローカル) | 処方箋画像・検査値PDF・お薬手帳画像をローカルOCRし、種別分類(処方箋/検査/画像/書類)してテキストを抽出対象に | attachments 台帳は完備、内容解析が未実装 |
| 10 | 検査値(labs)抽出と時系列ビュー | Cr/eGFR/K/Na/HbA1c/INR 等を本文・添付から抽出し患者別に時系列表示 | QC に `labs` カテゴリあり。#9 依存 |
| 11 | 腎機能・検査値ベースの用量確認候補 | eGFR 等と現行薬リストから「用量確認した方がよい薬」を候補提示(断定なし、根拠と参照基準を表示) | #3、#6、#10 |
| 12 | 相互作用・重複投与・併用注意の候補チェック | 確認済み現行薬リストに新規開始薬が加わった時点で相互作用・同効薬重複を候補提示 | #3、#6 |
| 13 | 症状⇄副作用候補の照合 | `symptom_after_med_change` を拡張し、開始/増量薬の既知副作用と新規症状の一致を候補化 | 既存シグナル、#3 |
| 14 | 疑義照会・処方提案台帳 | 提案→医師回答→採否→転帰を記録。採用率・応答時間を統計化し薬学的介入の実績として出力 | requests と同構造、interaction_links |
| 15 | 退院・転院時リコンシリエーション・ワークシート | `transition_reconciliation` 検知時に「移行前の薬 / 移行後の薬 / 差分 / 確認チェック」を1画面で生成 | 既存シグナル、rollup、#6 |
| 16 | 薬剤変更エピソード追跡(episode_links) | 変更→観察→再評価の連鎖を薬剤単位に紐付けクローズ判定を明示。`med_change_no_followup` の誤検知を減らす | README 未整備項目、#3 |
| 17 | アドヒアランス介入トラッカー(valid_facts) | 残薬・飲み忘れの言及→介入(一包化・カレンダー等)→再評価を追跡し `adherence_events` 統計を `unavailable` から解放 | README 未整備項目 |
| 18 | 緊急度エスカレーション | `urgency:high` かつ薬剤師宛・未応答が N 分続いたら個人メンション/別チャネルに昇格。shadow 運用から段階導入 | 旧候補6。否定/時制は v4 で対応済 |
| 19 | 終末期局面の変化点検知 | eol イベント後の 30/14/7/3日指標に沿って症状管理薬・麻薬の準備・在庫確認候補を提示 | events(eol)、縦断解析の軸 |

### Tier C — 多職種連携の可視化・共有

| # | 機能 | 内容 | 既存資産 / 依存 |
|---|---|---|---|
| 20 | 相談→回答 応答時間・依頼SLA 可視化 | 職種間ペア別の応答時間、未応答滞留を集計。`open_loop_aging` を本文由来依頼まで拡張 | interaction_links 整備、mcs_stats T2 |
| 21 | 患者別 関係職種ディレクトリ | 担当医・訪看・ケアマネ・居宅を投稿者情報から自動抽出し人が確認して確定。「誰に聞くべきか」を即答 | `staff` コマンド、self_profile_v1 |
| 22 | 引き継ぎ・当番向けワンページサマリ | 担当交代・休日当番用に患者の現状・注意点・未解決事項を1ページ生成(根拠リンク付き) | rollup、signals、#6 |
| 23 | 読取専用 Web UI | snapshot 上の患者ページ・タイムライン・根拠表示・シグナル一覧をローカルWebで閲覧(書込なし) | 旧候補4、read_model |
| 24 | 複数薬剤師対応 | 患者→担当薬剤師の割当、通知ルーティング、個人別ダイジェスト、担当不在時の代理 | allowed_user_ids、#1 |
| 25 | カレンダー連携 | 次回訪問予定・依頼期限・報告書提出期限を iCal 出力(zaitaku-calender 連携も視野) | next_planned、requests due_date |
| 26 | 算定・報告書提出状況トラッカー | 月次訪問回数・報告書提出の有無を患者別に追跡し、提出漏れ・算定要件未達の候補を提示 | #4、events(visit) |
| 27 | 業務負荷レポート(週次/月次) | 夜間休日の連絡量、投稿偏り、依頼滞留を薬局管理者向けに定期出力(人員配置の材料) | doc_burden、workload、comm_concentration |

### Tier D — 基盤・拡張

| # | 機能 | 内容 | 既存資産 / 依存 |
|---|---|---|---|
| 28 | シグナル精度フィードバックループ | 却下理由・採用率・再open率から検知器ごとの精度を可視化し、閾値・プロンプト改善の評価基盤に | signal_dismiss receipt、mcs_refstats、evaluation/ |
| 29 | FHIR JP Core エクスポート | 確認済み薬剤リスト→MedicationStatement、検査値→Observation を出力し yrese / PH-OS と連携 | ext_contract の認可枠、#6、#10 |
| 30 | 暗号化オフサイトバックアップ+復元訓練 | 負荷軽減軸では最下位だが基盤としては最優先(旧候補1、C1-2 未着手)。端末喪失時の復旧点を確保 | maintenance.py backup |

### 並び順の考え方

- #1〜8 は新しい抽出や辞書なしに、既存の rollup・signals・requests・
  カード操作を「薬剤師の1日の動線」に組み替えるもの。報告書類(#4, #5)は
  在宅薬剤師の最大の事務負荷のため上位。
- #3 drug_map は単体では地味だが #6, #10〜13, #16 の精度を左右するため Tier A。
- Tier B は「候補提示に留め、判断は人」の原則を守れる範囲で介入の質を上げる。
- #30 は業務軸では低いが、実運用では先に済ませるべき基盤項目。

## 既存データの修復順

独立 backup → A 系修正 → coverage/import job 修復 → 通知なし履歴再照合 →
返信・添付補完 → hash/artifact 再生成 → rollup 再構築。
既存 history_floor は返信失敗でも設定され得たため無条件信用しない
(削除はせず疑わしい完了を区別して GET-only で再照合)。

## 完了条件

「もっと賢く要約できる」ではなく「取りこぼし・未完了・失敗を正しく記録し、
再実行と復元で回復できる」こと。
