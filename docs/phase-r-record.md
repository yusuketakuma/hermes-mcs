# Phase R Record — MCS全体リファクタリング記録 (spec MCS-REFACTOR-FIRST-20260920)

Status: **RF-CODE 判定済み** / RF-OPS 未完了項目あり / Phase J (Jev) 実装済み — `docs/phase-j-record.md` 参照（mode=off 既定、実API評価 G2 未実施）
Recorded: 2026-09-20

## 1. 対象ツリーと基準

| item | value |
|---|---|
| repo | `/Users/yusuke/.mcs` → `yusuketakuma/mcs-adapter` @ `main` |
| baseline | `a8896ec`（R-00 manifest: `docs/r00-baseline.md`） |
| RF-CODE code tree | `46cffb0`（baseline + `9d2c6d1` REF + `46cffb0` FIX。docs commitは記録用で対象ツリー外） |
| 変更対象 | `adapter/` 13 modules + 2 test files。`docs/`。`data/`, `chrome-profile/`, `config.json`, `token_cache.json` は gitignore 通り非対象 |

## 2. 領域棚卸し → 判定（D01–D15 → R-02..R-09）

凡例: **維持**=契約・構造とも変更不要と確認 / **変更**=REF実施 / **修復**=FIX実施

| R | 領域 | 判定 | 根拠 |
|---|---|---|---|
| R-02 | 入口・設定・依存・副作用 (D01/D02/D03/D15) | **変更** | `_config`×4→`load_config`、`_NoRedirect`×3→`NoRedirect`、`ProxyHandler({})`×3→`no_proxy_opener`、flock×2→`acquire_run_lock`（`9d2c6d1`）。malformed-config観察メモ解決: `config:*_invalid` が `result.errors` に記録され run status=partial で可視化される（run_check 346-366）— silent ではなく意図的設計 |
| R-03 | 台帳・Tx・読取専用 (D07/D13/D14) | **維持** | writer=WAL、publish先snapshot=DELETE+`mode=ro&immutable=1`、`LedgerReader` はro URIでDDL/DML不可、migration v1→v7は単一Tx+user_version更新。変更は dead `import re` 除去と `_html_to_text`→`html_to_text` 移動のみ |
| R-04 | 未読・返信・discovery・履歴・アーカイブ (D04/D05/D06/D10) | **維持** | `fetch_thread` pagination(cap10)・body_state遷移・floor/ coverage単調性・archive抑止は特性テスト+本番検証済み（reply_jobs drained、13reply全頁）。リファクタでは未変更 |
| R-05 | ジョブ・command・排他・deadline (D08/D15) | **変更+修復** | REF: flock実装を `acquire_run_lock` に統一（lockfile・意味論同一、fd close=解放）。FIX `46cffb0`: `extract.py`/`extract_llm.py`/`rollup.py` が無lockで書込んでいた欠陥を修復（lock_held→exit3、`--stats`はroで無lockのまま）。deadline/stage順/drain_commands/receiptsは維持 |
| R-06 | 添付・Discord配送 (D09/D12) | **維持** | `_multipart`/`_post`/`_retry_after`/`_delivery_fingerprint`/`_progress`/flush の例外分類（receipt_invalid→hold=重複防止）確認済み。変更は `_channel_id` を `_config()` 経由に統一（REF内）。優先DL・sha256検証はテスト済み |
| R-07 | 抽出・ローカルLLM・rollup (D11) | **維持** | LLM endpoint `http://127.0.0.1:8080` loopback固定+`no_proxy_opener(NoRedirect)`、出力は`_validate`経由。`medications`は「chat内出現」であり現在処方箋ではない語義を維持。rollupはcontent_hash連動dirty検出+atomic replace |
| R-08 | 閲覧・正式依頼・CCO境界 (D13) | **維持** | `mcs_view`はsnapshot固定+generation binding+ro接続のみ。`mcs_requests`は`--confirm-human`必須・canonical JSON+sha256 receipt・cmdキューatomic write（DBロック対象外は設計通り—書込先が別store） |
| R-09 | 保守・起動・復旧・配布 (D01/D14/D15) | **維持** | `daily_backup`(verify→atomic,keep7)/`rotate_log`/`publish_snapshot`(generation_id)はrun_check内でlock下実行。launchd 3 plistは `run_check.py` のみ起動（手動CLIはcron対象外）を確認 |

## 3. REF/FIX/FEAT 分離

- **REF** `9d2c6d1` — `mcs_util.py` 新設と7ファイルの重複排除。動作変更なし（中間状態でbaseline 86件全パスを確認）
- **FIX** `46cffb0` — 手動writer CLIの単一writer規約違反修復 + 特性テスト2件追加
- **FEAT** — なし（この時点ではPhase J未着手。Jev client/DB/評価job/Open LoopはRF-CODE前に実装していない。Phase J実装は別記録 `phase-j-record.md`）

## 4. 検証証拠

| 検証 | 結果 |
|---|---|
| baseline特性テスト | REF中間状態: **86 passed**（新規2テストはロック欠如を検出してfail — 特性テストとして機能） |
| 最終テスト | **88 passed** (`venv/bin/python -m pytest`) |
| lint | `ruff check` 新規指摘ゼロ（16件は全てbaseline由来の既存指摘。dead import 1件除去） |
| `git diff --check` | clean |
| ロック網羅 | `Ledger(` 書込呼出5箇所全てが `acquire_run_lock` 取得済み。lockfile・非ブロッキング・fd-close解放の意味論は全入口で同一 |
| 本番E2E（REF+FIX後） | `run_check.py --json --download-files --no-notify` → run_id 169, `ok:true`, `errors:[]`, projects 2, threads 17, trickle 3患者(15/10/20 new), extracted 44, extract_llm 4, rollups 93, `snapshot:true`, 158.2s。送信ゼロを確認 |
| 回復 | `publish_snapshot` が本番で生成成功（snapshot:true）。`mcs_view`はsnapshot_meta+DELETE mode gateでro維持 |

## 5. 保存確認（不変条件）

- **単一writer**: scheduled tickと全手動writer CLIが同一lockfileで排他。lock_held時exit 3 ✓
- **人為確認**: `mcs_requests` は `--confirm-human` 必須のまま（変更なし）✓
- **原文データ**: message/attachment保存経路は未変更。sha256検証・`.part`失敗時削除を維持 ✓
- **通知資格**: outbox意図と本体の同一Tx、archive抑止、receipt_invalid→hold を維持 ✓
- **既読ポリシー**: `mark_patient_read` はsnapshot_ts必須・`mark_result_unknown`≠confirmedを維持（`--mark-read` flag継続）✓
- **ネットワーク隔離**: APIはNoRedirect、downloadはallowlisted HTTPS+443のみ、Authorization headerをredirect先へ送らない（fresh request）✓
- **LLM隔離**: loopback固定・no proxy・no redirect ✓
- **薬剤語義**: 「messages中の出現」のみ。現在処方推論なし ✓

## 6. 未実施・残余

- 未実施の検証: `auto_login` Keychain経路の実実行（今回session有効で未発動。コードパス自体はdedupのみで未変更）。`--mark-read` 実run（意図的に未実行 — 検証目的で既読化しない方針）
- 既存lint指摘16件（E702/E741/F401）はbaseline由来として残置 — Phase Rでは意図的に触らず（純粋整形は別変更とする分離方針）
- FEAT/Jev: RF-CODE通過後に `phase-j-record.md` として実装済み（OFF既定・実API評価未実施）

## 7. RF-CODE 判定: **PASS**

判定根拠: (a) 全13 moduleの契約レビュー実施・領域別判定を記録、(b) 特性テスト88件全パス（REF中間でのfail→FIX後passで差分検証を構成）、(c) 本番E2Eで全ステージ正常・送信抑止確認、(d) REF/FIXを別commitで分離、(e) 不変条件の保存を項目別に確認、(f) lint/diff-check clean。

対象ツリー: **`46cffb0`**（= baseline `a8896ec` + `9d2c6d1` REF + `46cffb0` FIX）

## 8. RF-OPS（本番運用ゲート）未完了項目

- [ ] 複数tick連続観察（launchd 15分間隔×数回のrun結果確認）
- [ ] lock競合の実発生確認（tick実行中の手動CLI → lock_held動作）
- [ ] snapshot→mcs_view読取の手動確認
- [ ] rollback手順の実演（下記9）
- [ ] run.log/backup rotation の長期観察

## 9. 展開・有効化・停止・rollback

- **展開**: `git push` 済み（`a8896ec..69b10ae`）。コードは次回launchd tickから自動適用（plist変更不要）
- **停止**: `launchctl unload ~/Library/LaunchAgents/local.mcs-*.plist`（3本）。data/は無変更で残る
- **rollback**: `git revert 46cffb0 9d2c6d1` または `git reset --hard a8896ec` + push。DB schema v7はREF/FIXで未変更のため戻し可能。lock追加FIXをrevertすると手動CLIの並走raceが復活する点に注意
- **Jev有効化（将来）**: OFF→shadow→assist→enforce の段階導入。OFF/shadowでは本pipelineに副作用を持たない設計を維持すること

## 10. Blocker

なし。RF-CODEは通過。Phase Jは OFF 既定実装として `phase-j-record.md` に記録済み。
