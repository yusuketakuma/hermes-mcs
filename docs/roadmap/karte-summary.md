# #21 MCS「患者連携サマリー」の取り込みと活用

2026-10-04版割当: 旧1.0.13〜1.0.15の残件は全て安定稼働版1.0.13へ集約。
成果物・CLI・受入の正本は[1.0.13開発計画](../development/plans/RELEASE_1.0.13.md)。
当時の調査・設計例と現在の実装状態を区別し、既存実装は再実装しない。


作成日: 2026-09-30。状態: 計画（実装は #20 の実装群の後、同じ worktree で行う）。ユーザー要望は「MCS に今日追加された連携サマリー機能をシステムで活用する」。

2026-10-03照合: GET・`stage_karte_summary`・artifact保存・閲覧・抽出文脈・digestは現行コードに実装済み。
以下は当時の設計と確認記録であり未着手計画として再実装しない。利用実態は[ROADMAP](../ROADMAP.md)の記録を参照。
追加投資は保留。新着のない患者のサマリー変更を次の新着まで拾えない制約と、書込み・既読状態を変えない条件は維持する。
当時のプローブ例は通常患者を探索するため、今回の22-A専用投稿確認へそのまま流用しない。

## 調査結果（MCS 側の機能）

MCS の web アプリ（Angular SPA、`www.medical-care.net`）のバンドルを認証なしで取得して解析した。表示名は **「患者連携サマリー」**（フォーム見出し「連携サマリー」、副題「医療･介護側に表示されます」、プレースホルダ「患者･利用者の病歴、ACP、緊急連絡先、注意点など、多職種で常に共有したい情報を要約して入力してください。」、上限 150 字）。患者（karte）単位の共有メモで、既存の「連携ノート」（`memo`）とは別項目。タイムライン上に折りたたみ表示され、更新があると「更新」バッジが付き、閲覧者ごとに既読状態を持つ。

| 操作 | API（`/api/v2t` 配下、Bearer 認証） | 本計画での扱い |
|---|---|---|
| 取得 | `GET /kartes/{karte_id}/memo_summary` → `{memo_summary: {is_editable: bool, is_read: bool, read_style: "single_line"｜"multi_line", comment?, user?, updated_at?}}`。**未登録の患者では `comment`・`user`・`updated_at` が無い**（実応答で確認、2026-09-30） | 使う（読み取りのみ） |
| 更新 | `POST /kartes/{karte_id}` body `{memo_summary: "<text>"}` | 使わない（MCS への書き戻しはしない: SCP-07） |
| 既読・表示状態 | `POST /kartes/{karte_id}/memo_summary/read_status` `{is_read: true}` / `{read_style: "single_line"｜"multi_line"}` | 使わない（他の閲覧者の未読表示を変えない） |
| karte_id の取得 | `GET /projects?include_meta=1` の各 project 行の `karte.id`（adapter が既に読む `karte` オブジェクト） | `patients.karte_id` として保存 |

実応答はオーナー実行で確認済み（2026-09-30、5 件中 5 件に `karte.id` あり。project 行の `karte` は `first_name/id/is_confirmed/labels/last_name/medical_project/station/user` で、一覧側にはサマリーの更新日時が無いため差分検知は個別 GET に頼る）。確認に使ったスクリプト（キー名と型のみ出力）:

```
cd ~/.mcs && ~/.hermes/hermes-agent/venv/bin/python - <<'PY'
import sys, os, json; sys.path.insert(0, os.path.abspath('mcs')); import _mcs_path
import mcs_adapter as A; ad = A.MCSAdapter(token_cache=A.CACHE)
rows = ad._get('/projects', {'include_meta': 1, 'per_page': 3, 'page': 1, 'include_paginate_totals': 0}).get('projects') or []
kid = next(r['karte']['id'] for r in rows if (r.get('karte') or {}).get('id'))
ms = ad._get(f'/kartes/{kid}/memo_summary')
print({k: (type(v).__name__ if not isinstance(v, dict) else sorted(v)) for k, v in ms.items()})
PY
```

## 目的と境界

- 患者連携サマリーを**読み取り専用**で取り込み、(1) 患者 rollup と 🧾 患者サマリーに出す、(2) 抽出の参照専用文脈に加える、(3) 日次ダイジェストに「更新あり」の件数と ID を出す。
- 書き戻し・既読化・新しい通知種別・外部送信（`mcs-read-model/1` allowlist）は含めない。zaitaku-calender への受渡しは C2 と CD 契約の別判断。
- 本文（comment）は PHI としてローカルに保持し、通知テキスト（digest・リマインド）には載せない。カードの 🧾 患者サマリー view は既に本文を表示する面なので、そこに 1 行載せるのは可とする（`#21-D1`）。

## 設計（既存機構の再利用）

| 段 | 変更 | 規模 |
|---|---|---|
| adapter | `UnreadPatient` に `karte_id`（`karte.id`、不正なら `SchemaError`）。`fetch_memo_summary(karte_id) -> dict | None` を `_get` の上に追加。`is_editable`・`is_read`・`read_style` は必須（型不正は `SchemaError`）。`comment`（str, ≤ 600 字で切る）・`updated_at`・`user`（`profession`/`name` のみ）は任意で、`comment` が無ければ「未登録」として `None` を返す。他のキーは捨てる | S |
| ledger | `patients.karte_id` を加法 migration で追加（version 据え置き）。取得結果は `artifacts` kind=`karte_summary`（project_id、content=`{comment, updated_at, updater:{profession,name}, is_editable, empty: bool}`、meta=`{karte_id, fetched_at, sha256}`）。**未登録は `empty: true`・`comment: null` で保存する**（「空」と「未取得」を区別するため）。同じ sha256 なら書かない（履歴は artifact で残る）。読み側は `latest_artifact(db, "karte_summary", project_id)` | S |
| 取得段（`run_check`） | 新 stage `stage_karte_summary`: **(a) その tick で新規チャット（ルート・返信・self probe 由来を含む）を保管した project は毎回 GET して都度更新**（オーナー指示 2026-09-30）。(b) `--jobs-only`（deep run、:07/:37）では `karte_summary` artifact がまだ無い project を最古順に最大 10 件（初回埋めのみ。定期再取得はしない）。1 tick の上限 12 要求、超えた分は次 tick に持ち越し、deadline を守る。失敗は `result["karte_summary"]["errors"]` に理由コードで残し、次回に再試行（`fetch_jobs` は使わない）。session 失効は既存経路 | S |
| rollup | `patient_rollup` に `karte_summary`（comment、updated_at、updater の職種、empty）を追加。`PERIOD_CHECK_VERSION` 据え置き | S |
| 表示 | `notify_views.patient_summary_text` に 1 行: 登録あり →「連携サマリー（MCS・更新 MM/DD・職種）: 先頭 80 字」、**空 →「連携サマリー（MCS）: 空」**、未取得 →「連携サマリー（MCS）: 未取得」。`mcs_view status` の project 行に `karte_summary_at`・`karte_summary_empty` | S |
| 抽出文脈 | `extract_llm._thread_context` と同じ参照専用ブロックとして「患者連携サマリー（参照専用・evidence 引用禁止）」を対象本文の前に注入（≤ 150 字）。既存の文脈上限（`_CTX_TOTAL_MAX`）に含める。chunk checkpoint は文脈 hash が変わるため新着からのみ影響。`EXTRACT_VERSION` 据え置き | S |
| digest | `notify_digest.build_text` に「連携サマリー更新: n 件（project ID 一覧）」。本文は載せない。`daily_digest.include_names` の規則に従う | S |

`ponytail:` 取得の対象選択は「新規チャットがあった project は都度」＋「未取得 project の初回埋め 10 件/deep run」の 2 規則だけにする。全 project の毎 tick 取得（102 GET/tick）はしない。新着の無い患者のサマリー変更は次の新着まで反映されない（一覧 API に更新日時が無いため。差分検知が取れると分かれば規則を差し替える）。

## 安全・安定性

- 読み取りのみ。`mark_as_read` 系の GET（`unread=1&timestamp`）は呼ばない。`extend_session` は既定のまま。
- 取得段は unread 収集・通知の後に置き、予算超過時は skip（tick を `partial` にしない）。
- comment は artifact と rollup にのみ保持。`brain_export`/`export_schema` の allowlist は変えない（`read_model` に出さない）。
- 抽出文脈への注入は DATA 扱い（既存の `_CTX_HEAD` と同じ禁止文）。注入の可否はオーナー判断（`#21-D2`）。既定は注入する（本文と同じ PHI 範囲で、ローカル LLM のみ）。

## テスト（一時 DB・stub・完全合成）

- adapter: 合成応答で schema 検証（不正な型・過長 comment・未登録形 `{is_editable,is_read,read_style}` → `None`）。`karte.id` の欠落は `SchemaError`。
- ledger: 同一 sha は再保存しない、変更で新 artifact、`latest_artifact` が最新を返す。
- stage: 対象選択（新規チャットのあった project は毎回、未取得の初回埋めは deep run のみ 10 件、上限 12/tick と持ち越し）、deadline で打ち切り、失敗コードの記録、同一 tick 内の重複 GET なし。
- rollup／notify_views／mcs_view: 表示行と 80 字の切り詰め、空は「空」、未取得は「未取得」。
- extract_llm: 文脈ブロックの注入、`evidence` がサマリー文から取られたら破棄される（既存の本文限定照合で担保）。
- digest: 件数と ID、本文非表示、`include_names` の扱い。

## 容量

HTTP GET は 1 tick に最大 12 件（現状の self_probe 102 件より小さい）。LLM は 1 呼び出しあたり文脈 +150 字（約 1 秒）。

## 判断・依存

- `#21-D1`: 🧾 患者サマリー view に連携サマリー本文の先頭 80 字を出すか（既定: 出す）。
- `#21-D2`: 抽出文脈に注入するか（既定: 注入する）。
- `#21-D3`: digest に件数・ID を出すか（既定: 出す。本文は出さない）。
- 未登録（`comment` 無し）の患者は `empty: true` の artifact を書き、表示は「空」。取得したことが無い患者は「未取得」。`is_editable` は保存するが表示しない（自局が編集できるかは MCS 側の権限で、本システムは書き戻さない）。
- 依存: #20 の実装群（`extract_llm`・`rollup`・`run_check` を同時に触るため直列）。C1 の allowlist とは独立。

規模: 合計 M（adapter S・ledger S・stage S・表示 S・文脈 S・digest S）。実装順: adapter＋ledger＋stage → rollup／表示 → 抽出文脈 → digest。
