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

## 既存データの修復順

独立 backup → A 系修正 → coverage/import job 修復 → 通知なし履歴再照合 →
返信・添付補完 → hash/artifact 再生成 → rollup 再構築。
既存 history_floor は返信失敗でも設定され得たため無条件信用しない
(削除はせず疑わしい完了を区別して GET-only で再照合)。

## 完了条件

「もっと賢く要約できる」ではなく「取りこぼし・未完了・失敗を正しく記録し、
再実行と復元で回復できる」こと。
