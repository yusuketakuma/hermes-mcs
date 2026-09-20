# R-00 Baseline Manifest — MCS refactor (spec MCS-REFACTOR-FIRST-20260920)

Recorded: 2026-09-20. Phase R implementation authorized; commit+push to main permitted.

## Baseline

| item | value |
|---|---|
| repo | `/Users/yusuke/.mcs` (remote `yusuketakuma/mcs-adapter`, branch `main`) |
| baseline_commit | `a8896ec` (spec reference `d86237e` + 1 fix commit on top; spec's own note: reference point ≠ checkout target — preserved) |
| dirty_fingerprint | clean tree (`git status` empty, no stash) |
| runtime (production) | `/Users/yusuke/.hermes/hermes-agent/venv/bin/python` = Python 3.11.15 |
| runtime (test/probe) | `/opt/homebrew/bin/python3.13` = 3.13.15 (no pytest module — tests MUST run under the hermes venv) |
| sqlite | 3.53.4; ledger schema `user_version=7` |
| test baseline | `venv/bin/python -m pytest test_mcs_ingestion.py test_mcs_features.py -q` → **86 passed, 0.79s**; isolation: `tmp_path` + monkeypatched module constants; one e2e stub fails on any external request |

## Entry points / triggers (all share `data/run.lock` fcntl.flock)

| entry | trigger | args |
|---|---|---|
| `run_check.py` | launchd `local.mcs-check` (every 15 min) | `--json --download-files` |
| `run_check.py` | launchd `local.mcs-cmd` (WatchPaths `data/cmd`) | `--json --download-files` |
| `run_check.py` | launchd `local.mcs-deep` (min 7) | `--json --jobs-only` |
| `init_data.py` | manual bulk import | `--days/--since/--pages/--chunk/--deadline/--project`; takes flock |
| `mcs_view.py` | CCO/sandboxed read | snapshot-only reads + `requests create/update` → enqueue |
| `extract.py`/`extract_llm.py`/`rollup.py` | manual CLIs | write to DB **without flock** — see candidate FIX-R00-01 |

Config: `config.json` → `discord_channel_id`, `mcs_login_id`, `deep_history`, `trickle_pages`, `notify_bot_profile`, `discover_archived`. Malformed config → `{}` + per-key validation (bool/int range) with `config:*_invalid` errors.

## D01–D15 → code map

|域|実現箇所|要点|
|---|---|---|
|D01 設定/起動/CLI|`run_check.main`,`_config`,`_err_str`;各main()|exit 0/1/2/3;`--jobs-only`,`--no-notify`,`--mark-read`等|
|D02 認証/CDP/再ログイン|`mcs_adapter.MCSAdapter`: bootstrap_token,ensure_session,_token_via_cdp(CDP 127.0.0.1:9333),auto_login(Chrome+Keychain),check_session|tokenはcache file。SessionExpired→exit 2|
|D03 HTTP契約|`_request`,`_get`,`_assert_allowed_url`,`_NoRedirect`,`_SameHostRedirect`,`download`,`_open_download`|redirect制御・host検査|
|D04 未読/返信|`list_unread`,`fetch_unread_messages`,`fetch_thread`(paginated, cap=10p),`fetch_unread_replies`,`_norm_message`|body_state full/snippet/unknown|
|D05 discovery/履歴|`list_projects`,`list_archived_kartes`,`fetch_history`,`check_project_delta`;`job_ops`:seed_discovery,run_discovery,seed_trickle,run_history_jobs,merge_full_replies;ledger:history_floor/cursor/target,coverage_ts|floor=-1=完了,coverage=検証済み境界|
|D06 archive|`upsert_patient_info(is_archived)`,`archive_head_since`,`is_archived`;run_discovery `include_archived`;history_head jobs;notifier.flush L484 outbox_suppress|flag+head job同一Tx、抑止はflush時点判定|
|D07 SQLite/migration|`Ledger.__init__/_preflight/_init/_migrate/_migrate_body/_backfill_v3/_script`;`LedgerReader`(ro);`publish_snapshot`,`valid_mcs_db`|v1→v7,user_version同一Tx更新|
|D08 job/command|ledger fetch_jobs系(job_add/_job_add_tx/due/done/defer/retry/fail,pending_reply_jobs,replies_without_job);`job_ops.drain_commands`;`mcs_requests`:parse/read/validate/apply/enqueue,command_receipts|uuid+payload_hash receipt、人手確認必須|
|D09 添付|ledger:_save_attachments,attachments_due(priority_mids),attachment_saved/failed;adapter.download;run_check.stage_attachments;notifier._collect_files|通知参照添付の優先DL(sha256検証)|
|D10 既読|`mark_read/was_marked`,read_marks;`mark_patient_read`;stage_unreadのgate(fetch_state=='complete'のみ、intent先行記録、unknown≠confirmed)|manual flag `--mark-read`|
|D11 抽出/LLM/rollup|`extract.py`(extract_v1 rules),`extract_llm.py`(llama.cpp 127.0.0.1:8080,no proxy/redirect,_validate),`rollup.py`(patient_rollup,dirty検出,atomic replace)|content_hash連動stale検出|
|D12 Discord|`notifier.py`全般(_format_event,_structured_lines,_multipart,_post,_retry_after,_delivery_fingerprint,_progress,flush);ledger outbox_*|chunk receipt,fingerprint,4xx/サイズでfile落としfallback|
|D13 閲覧/requests/CCO|`mcs_view.View`(snapshot固定、generation binding,cursor),CLI;`mcs_requests.candidates`|snapshot以外書込不可、create/updateはenqueueのみ|
|D14 backup/snapshot|`maintenance.daily_backup`(検証→atomic,keep7),`rotate_log`,`publish_snapshot`;ledger.publish_snapshot|snapshot_meta generation_id|
|D15 scheduler|run_check flock/deadline480/stage順/runs表;3 launchd plists;86 tests|単一writer排他|

## REF/FIX候補（R-00調査で発見。判定は各Rで実施）

- **FIX-R00-01**: `extract.py`,`extract_llm.py`,`rollup.py` の手動CLIが flock 無しでDB書込 → **FIXとして実施済み**（`46cffb0`）。各main()が共通 `acquire_run_lock()` をLedger open前に取得し、保持中は `{"error":"lock_held"}` + exit 3。`extract --stats` は LedgerReader(ro) のままロック無し。回帰テスト+2件
- **維持候補**: 本コードベースは既に複数回のレビュー(Oracle B01-B28等)と特性テストを通過済み。構造は責務分割済み(facade的run_check + 領域別module)。「維持」判定が主になる見込み — RF-INV-07通り良好な領域は触らない
- 観察メモ: run_checkの`_config()`はmalformed時`{}`→各keyはdefault適用+error記録。silent-defaultは意図的か → D01で確認
- `notifier._env`/`_config`/`_token`/`_channel_id` — env/config解決がflush毎に実行されるか要確認（D12）

## 進捗記録（Phase R 実施ログ）

### 2026-09-20 R-02/R-05 一部実施 — commit `9d2c6d1` (REF), `46cffb0` (FIX)

**REF `9d2c6d1`** — `mcs_util.py` 新設（load_config / html_to_text / NoRedirect / no_proxy_opener / acquire_run_lock）。重複排除:
- `_config` 4箇所（run_check, notifier, mcs_adapter._config, init_data側）→ `load_config` facade化
- `_NoRedirect` 3箇所（mcs_adapter, extract_llm, notifier）→ `NoRedirect`
- `ProxyHandler({})` opener 3箇所 → `no_proxy_opener`
- `_html_to_text` 2箇所（notifier, ledger）→ `html_to_text`（完全一致を確認済み）
- flock実装 2箇所（run_check, init_data）→ `acquire_run_lock`（同一lockfile・同一非ブロッキング意味論）
- `notifier._channel_id` が独自ファイル読みしていたのを `_config()` 経由に統一
- 契約維持: download redirectは `_SameHostRedirect`+allowlistのまま、lock semanticsは fd-close解放のみ（LOCK_UN省略は等価）

**FIX `46cffb0`** — 上記FIX-R00-01。`test_run_lock_excludes_second_writer`（同一路径への第二writer拒否・closeで解放）、`test_cli_writer_holds_run_lock`（保持中 exit 3 / 解放後 proceed）追加。

**証拠**: REF中間状態で baseline 86 passed（新規2テストはロック欠如を正しく検出し失敗=特性テストとして機能）、FIX後 88 passed。`git diff --check` clean。ruff新規指摘ゼロ（`ledger.py`のdead `import re`を除去）。全書込入口のロック網羅をgrep+目視で確認（Ledger( call site 5箇所全て）。launchd起動対象はrun_checkのみ。`mcs_requests`はcmdキューatomic writeのみでDBロック対象外（設計通り）。

## 副作用一覧（テスト隔離対象）

外部: MCS HTTP(TOKEN必須),CDP ws(9333),Chrome起動,Keychain(`security`CLI),Discord POST,llama.cpp(8080)。FS: data/(ledger.db,attachments,cmd,backups,snapshots,run.lock,run.log),chrome-profile,token_cache.json,config.json,.env。DB: 全write系API。

## 試験コマンド

```
cd adapter && /Users/yusuke/.hermes/hermes-agent/venv/bin/python -m pytest test_mcs_ingestion.py test_mcs_features.py -q
lint: ruff check <files>（既存指摘多数・変更差分のみ評価）
live run: run_check.py --json --download-files（flock排他・本番副作用あり）
```
