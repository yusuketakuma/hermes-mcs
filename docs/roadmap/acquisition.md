# 取得・アーカイブの詳細計画（#1〜#6）

[`docs/ROADMAP.md`](../ROADMAP.md) の #1〜#6 の詳細計画。

2026-10-03照合: 以下の「現状」・行番号・端末実測・節番号は2026-09-29の調査記録。
現在の優先順位・版割当・公開条件は[ROADMAP](../ROADMAP.md)を正とし、本書の実施順は項目内の依存順として読む。
1.0.11の#2は投稿メタの完全合成契約を先行する。従来取得契約のfixture化全体、#1・#3〜#6は後続。
#4は1.0.15ではplanまでで、本番修復・C1本番投入は実施記録と未決判断が揃うまで行わない。
新定期ジョブを設計する際は、Hermesのcronと独立hostの所有を分け、standaloneへHermes cronやworkerを重複登録しない。

- 基準: v1.0.6 のコード（2026-09-29 調査）。行番号はこの時点のもの。
- 表記: 【実行確認】= 合成入力で実行して確認、【未検証】= 実データ・実機・実 API に触れないと分からない点。
- 実データ（`data/`・`config.json`・`.env`・`token_cache.json`・`chrome-profile/`）は読んでいない。
- オーナー判断は項目内で `#N-Dk` と呼ぶ。一覧と決める時点は ROADMAP の「オーナー判断一覧」にある。

## 0. 前提と制約（複数項目に効く）

1. **この Mac は FileVault Off、Time Machine の保存先なし**（2026-09-29、`fdesetup status` / `tmutil destinationinfo` の読み取りで確認）。原本 DB（`data/ledger.db`）と日次バックアップ（`data/backups/ledger-*.db`）は同一ディスク上の平文だけ。`mcs/ingest/mcs_adapter.py:736-743` の「FileVault or physical security is assumed」がこの端末では成立していない。コード外の設定だが、#1 と同時に判断する（#1-D6）。
2. **CI ゲートが設計を拘束する**（`ci/gates.py`）。
   - write-mode で `Ledger(` を開けるのは `LEDGER_WRITERS`（:23）だけ。それ以外の `sqlite3.connect` は `mode=ro` / `immutable` が必須（`maintenance.py` と `llm_admission.py` は例外: :219、:222-266）。
   - `Ledger(` を開く経路は `acquire_run_lock` に到達すること（`gate_writer_lock` :268）。
   - `docs/dev-records/` に置く記録は `.md` / `.json` のみ（`gate_records_isolation` :357）。`FIX-`・`BUG-`・`INCIDENT-`・`AUDIT-`・`EVAL-`・`TEST-` 接頭辞の ID を書くと `ci/gates-coverage.json` への登録が要る（`ci/mine_gates.py`）。
   - 帰結: #1 の drill と #4 の plan は `Ledger()` を使わず、`valid_mcs_db` と読み取り専用接続だけで作る。
3. **restore の本体は実装済み**。`mcs_update._restore_db`（`mcs/ops/mcs_update.py`）、`_replace_database`、`_restore_loss_report`、`notify_reconcile.reconcile_after_restore`（`mcs/notify/notify_reconcile.py`）がある。合成の復元検証もテストとして存在する（`tests/core/test_ledger_recovery_contract.py`: 件数・親子・FTS・添付 hash・snapshot 閲覧）。欠けているのは、オフサイト媒体からの取得と復号、運用者が実行して記録を残す drill だけ。
4. **journal 圧縮の保証範囲が #1 と #6 に効く**。`adapters/common/worker.py:183-230` は `data/backups/*.db` の最古 mtime−1 日より古い journal 行だけを捨てる。`hermes_plugin/README.md:211-214` は「`data/backups` 外の複製から restore する運用は保証の対象外」と明記している。オフサイトから 7 日より古い世代を戻すと、配送の照合証拠が失われうる。
5. **`restore_pending` マーカーが止めるのは card の grant だけ**（`mcs/notify/notify_transport.py:183-188`）。text 経路の `notify_flush.flush()` は参照しない（`mcs/notify/notify_flush.py:932-976`）。

## #1 暗号化オフサイトバックアップと復元訓練

**目的**: 端末喪失・ディスク故障・盗難後も、検証済みの復旧点から原本 DB を復元できる状態にする。復元できることを定期的に実証して記録する。

**現状**
- 復旧点は `maintenance.daily_backup`（`mcs/core/maintenance.py:47-77`）が作る `ledger-YYYYMMDD.db` の 7 世代だけ（`BACKUP_KEEP=7` :23）。流れは `.backup` → tmp → `valid_mcs_db` → `publish_tmp`（`mcs/core/mcs_util.py`）。毎 tick の `_housekeeping`（`mcs/ingest/run_check.py:1043-1061`）から呼ばれ、当日分に対して毎 tick `valid_mcs_db`（quick_check）が走る。
- `preupdate-*` は別 prefix（`maintenance.py:80-104`）で、update_state 未参照のものは日次で削除される（:107-141）。
- `valid_mcs_db` は `mcs/core/ledger.py:1892-1970`、`publish_snapshot` は :1856-1889。旧 ROADMAP の `:1858-1900` は両者の途中で不正確だった。
- 暗号化・オフサイトの実装はない。`rg -n 'encrypt|decrypt|cipher|aes|restore_drill' mcs scripts deployment hermes_plugin integration ci tests` は 0 件。旧 ROADMAP の `rg 'encrypt\|restore_drill'` は ripgrep では `\|` がリテラルなので 0 件は当然で、根拠になっていなかった。
- 制限は明文化済み: `SECURITY.md:57-58`、`SECURITY.md:44-47`、`docs/specs/lifecycle-spec.md:187-200`。
- 規模: 2026-09-23 の記録で約 76MB・189 患者・15,398 メッセージ（`docs/dev-records/continuation-20260923.md:224`）。現在値は【未確認】。暗号化・転送のコストは小さい。
- 外部プロセスの前例: `security`（`mcs_adapter.py:720-722`、`mcs_setup.py:587-630`）、`launchctl`（`mcs_util.py:351-373`）、`git`、`hermes`（`notify_flush.py:64-66,654`）。`gate_stdlib_only`（`ci/gates.py:96-122`）が見るのは Python の import だけで、core の subprocess は禁止されていない（禁止は plugin の subprocess: `ci/gates.py:174-`）。
- 【実行確認】OS 同梱の `/usr/bin/openssl` は LibreSSL 3.3.6、PATH 先頭の Homebrew は OpenSSL 3.6.4。cron wrapper は PATH を `$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin` に固定する（`deployment/scripts/mcs_check.sh:7-8`）ので、実行時は LibreSSL になる。
  - `openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha256 -salt -pass stdin -in F -out G` は両者で相互に復号でき（`Salted__` 形式）、鍵導出は約 0.36 秒。
  - AEAD（GCM）は `enc` で使えない。暗号文を 1 バイト反転しても復号はエラーなく成功し、平文が 17 バイト変わった。CBC 単体には改ざん検知がない。
  - 誤パスフレーズは "bad decrypt" になるが、パディング検査だけなので確実ではない。平文 hash での確認が要る。
  - `gpg` は Homebrew 側にしかなく、`age` は未インストール。

**設計方針**
- **暗号化**: `/usr/bin/openssl enc`（絶対パス）を subprocess で呼ぶ。Python stdlib に対称暗号はなく、自前の暗号は書かない。`hdiutil`（暗号化イメージ）は attach/detach が要り、単一ファイルの検証・移送に向かない。パスフレーズは `-pass stdin` で渡し（argv に出さない）、データは `-in ファイル` で渡す。
- **改ざん検知は stdlib で補う**: manifest に `plain_sha256`・`enc_sha256`・サイズ・schema version・テーブル件数（PHI を含まない集計だけ）・KDF/openssl 版を持たせ、manifest 全体を HMAC-SHA256 で署名する（`hmac.compare_digest` で検証、MAC 鍵は `hashlib.pbkdf2_hmac` などで導出）。
- **処理順**: 最新の valid な `ledger-*.db` → gzip（stdlib）→ `openssl enc` → manifest+HMAC → 対象ディレクトリへ「tmp → fsync → rename、manifest は最後（commit マーカー）」→ read-back で sha 検証 → 世代 prune。
- **オフサイト先はディレクトリパスに限定**（config `backup.offsite_dir`）。外付け暗号化 SSD、NAS の mount、iCloud Drive フォルダなど何でも指せる。core は新依存を持たず、未 mount は fail-closed。HTTP などの network 転送は core に入れない。Time Machine（暗号化した外付け/NAS）の併用は運用面で有効だが、DB の妥当性検証も HMAC も持たないので本項目の代替にはならない。
- **鍵**: 32 バイトのランダム鍵を `keygen` で生成し、Keychain の別 service（例 `mcs-backup`）に保存する（`mcs_setup._keychain_store` を service 引数化して再利用）。オーナーが 1 回だけ表示される値を端末外へエスクローする。端末を失うと Keychain も失われるため、エスクローがなければ復号できない。Keychain が locked のときは fail-closed（前例 `KeychainLocked`: `mcs_adapter.py:78-83`）。`.env` フォールバックは既定で採用しない（FileVault Off のため）。
- **実行**: 別 cron ジョブにする（`deployment/scripts/mcs_offsite.sh` を `CRON_JOBS`（`mcs_setup.py:1473-1480`）に追加）。tick に組み込むと `RUN_DEADLINE_S` と run.lock を消費する。対象は静的な検証済みファイルなので run.lock は不要。
- **health**: `data/backup_state.json` を作り、`_health()`（`run_check.py:103-183`）に `backup` 節（最終オフサイト時刻・最終 verify・最終 drill と閾値超過）を足す。超過は `degraded` にして既存の health_watch に通知させる。
- **復元訓練（drill）**:
  - 手順: 取得 → HMAC/sha 検証 → 復号・展開（0700 の scratch）→ `valid_mcs_db` → `PRAGMA integrity_check`・`foreign_key_check` → `user_version` と件数の manifest 一致 → 集計 → scratch 削除 → 記録。
  - 集計項目: `patients` の `fetch_state` / `history_floor`（-1・正・NULL）/ `coverage_lag` / `is_archived`、`messages` の `body_state` と最新 `posted_at_ts`、`incomplete_reply_roots`（`mcs_view.py:201-205` と同じ SQL）、`fetch_jobs` の kind×state、`attachments` の state、`notify_outbox` の state（held）、`runs` の最終完了時刻（= RPO）。
  - 再収集できない `requests` と `command_receipts` の件数は必ず人手で確認する。
  - `quick_check` だけでは不足なので `integrity_check` を足す。記録は `data/drills/*.json` と手書きの `docs/dev-records/*.md`（件数・hash・所要時間だけ。PHI は書かない）。
  - drill は自動 verify（Keychain の鍵）と、四半期の人手 drill（エスクロー鍵を手入力）の二段にする。後者だけがエスクローの正しさを証明する。
- **実機復元（新端末）**: 既存の `data/ledger.db` があれば拒否する（`docs/specs/lifecycle-spec.md:200`「原本上書きは人手のみ」）。配置後に `notify_cards.mark_restored(DATA, by="manual")`（`notify_cards.py:351-364`）で送信 hold を掛ける。再収集分の再通知を避けるため、初回 tick 前に `notify_max_age_h`（`ledger.py:773-794`）を絞る。`--no-notify` は送信を止めるだけで outbox への登録は止めない（`run_check.py:1006,1118` のみで参照）。添付本体は保存対象外（14 日で prune: `maintenance.py:25`）。

**成果物**
- 新規: `mcs/ops/mcs_backup.py`（`keygen` / `offsite` / `verify` / `drill` / `restore` / `status`）、`deployment/scripts/mcs_offsite.sh`、`tests/ops/test_mcs_backup.py`。
- 変更: `mcs/core/maintenance.py`（ハッシュ・原子コピー・世代 prune の補助）、`mcs/ops/mcs_setup.py`（`CONFIG_RULES` に `backup` 追加、`_validate_backup`、`check` の probe、`_keychain_store` の service 引数化）、`mcs/ingest/run_check.py`（health）、`deployment/launchagents/README.md`、`tests/meta/test_deployment_scripts.py`。
- 文書: `SECURITY.md:57-58`、`SECURITY.md:44-47`、`docs/specs/lifecycle-spec.md`、`docs/guides/INSTALLATION.md`、`docs/development/DEVELOPMENT.md`（`scripts/development/update_readme.py` で再生成）。
- `AGENTS.md`: OS 同梱 `/usr/bin/openssl` を許可する 1 行の例外（#1-D4 の承認が前提）。

**受入条件とテスト（合成のみ）**
- 合成 `Ledger` で `daily_backup` → `offsite` → `verify` → `drill` を通す。注入する `passphrase_provider` を使い、Keychain は触らない（`tests/conftest.py` は `security` を遮断している）。
- 誤パスフレーズ、暗号文の 1 バイト反転、manifest 改ざん、暗号文の切り詰めが、すべて検知される（非ゼロ終了）。
- 対象が未 mount または書込不可なら、部分成果物を残さず fail-closed になる。転送が中断しても manifest は存在しない。
- 対象ディレクトリのファイルに SQLite magic（`SQLite format 3`）が含まれない。
- `subprocess.run` を捕捉し、argv にパスフレーズが含まれない。ファイルは 0600、ディレクトリは 0700。
- 保持: 新世代が検証済みになるまで旧世代を消さない。
- `openssl` が無い、または機能しない場合は fail-closed。`pytest.skip` は CI の ubuntu 側でだけ許す。
- drill の出力に患者名・本文が含まれない（許可トークンだけの正規表現テスト）。
- 既存の `test_independent_restore_preserves_relations_fts_snapshot_and_attachment_hash`（RT-035/038）の検証項目を drill が満たす。
- `ci/gates.py` が pass する（`Ledger(` を使わない）。

**依存・順序**: 単独で着手できる。オーナー判断 #1-D1〜D6 が先に要る。#4 の「独立 backup」の前提になる。分割は 1a（`offsite` + `verify` + テスト、M）→ 1b（cron・health・文書、S）→ 1c（初回の実機 drill、S + オーナー作業）。

**規模**: L（コード自体は M）。新規 1 モジュール（約 400 行）と、設定・cron・health・文書・テストの多点変更。

**オーナー判断・リスク**
- `#1-D1` 医療情報の外部保存の許可範囲。暗号化済みでも、外部媒体・クラウドへ PHI が出る。組織規程やガイドラインの確認が要り、`SECURITY.md` の外部送信の原則と整合させる。承認前は実装だけ進めて実行を保留する。
- `#1-D2` オフサイト先。最初は物理媒体か別拠点 NAS を推奨する。クラウドは承認後。
- `#1-D3` 鍵エスクローの保管者・保管場所。鍵を失うと復号できないことを受容するか。
- `#1-D4` OS 同梱 openssl を「新しい外部依存」に含めるか。含めないとする例外を `AGENTS.md` に明記する案。将来 macOS から LibreSSL が外れたら fail-closed で検知する。
- `#1-D5` 世代（例: 日次 7 + 週次 4 + 月次 3）と RPO（日次 backup は日付名なので最大約 24 時間）。
- `#1-D6` **FileVault を有効にするか**（別件だが同時に判断する）。鍵と DB が同じ端末にある設計は、端末そのものの侵害には無力で、防げるのはオフサイト媒体の紛失・盗難だけ。
- journal の保証範囲（前提 4）: 7 日より古い世代からの restore は手順化が要る。journal 圧縮の horizon をオフサイトの最古世代まで広げるか、古い世代の restore を「全 hold + 手動確認」の手順にするか。
- scratch に残る平文 DB の掃除を drill の冒頭でも行う。
- 【未確認】現行 `config.json`、現在の DB サイズ、Ubuntu CI 上の openssl の挙動。

## #2 MCS API 取得契約の fixture 化（C01）

**目的**: `sort=pinned` の順序、thread pagination、paginate 欠損に関する取得契約をテスト資産として固定し、API の変更や実装の退行を検知できるようにする。

**現状**
- クライアント側の安全化は既にある。履歴は below-cutoff を skip するだけで、終端の認証は `has_next` のみ（FIX-AD1。`mcs/ingest/mcs_adapter.py:1257-1264`、経緯は `docs/dev-records/continuation-20260923.md:119-136,389`）。実 API が「pinned 先頭 + 時系列」を返すかは仮説のままで、観測記録はない。
- 既存テスト: `tests/ingest/test_adapter_wire.py:158-320`（履歴・pinned・pagination・スキーマ欠損）、:712-720、:723-741、:743-802（worker deadline）、`tests/ingest/test_job_drain.py:1005-1027`（thread pagination）。
- ただし大半は `_get` を差し替えた inline stub で、HTTP → `_request` → parse → パラメータ検証の経路を通らない。
  - `worker=` seam（`mcs_adapter.py:498-507`）を使うのは mark・添付・bootstrap のテストだけ。
  - 履歴の `sort=pinned` や `per_page` を assert するテストは見当たらない。
  - `keep_read_status` を assert するのは stage テスト 2 箇所だけ（`tests/ingest/test_run_check_stages.py:92,403`）。**thread 取得（`mcs_adapter.py:1039`）に `keep_read_status` が付くことは、どのテストも固定していない**。付かなければ MCS 側で既読化される書込副作用が出る（`mcs_adapter.py:1393-1401`、A1-1）。
- `fetch_latest`（`mcs_adapter.py:1289`）は `keep_read_status` を付けない。既読の副作用がないことの実証記録は、リポジトリ内で見つけられなかった【要確認。`self_posts=true` で毎 tick 呼ぶ】。
- thread 応答に `paginate` キーがないときは「単一ページ」扱い（`mcs_adapter.py:1040-1046`）。履歴・未読が `paginate` 欠損を SchemaError にするのと非対称で、これを固定するテストはない。
- `reply_count` に tombstone が含まれるかも未確認。`merge_full_replies` は削除済み返信を「取得済」に数える（`mcs/ingest/job_ops.py:281-289`）が、view の `incomplete_reply_roots` は `body_state='full'` だけを数える（`mcs/views/mcs_view.py:201-205`）。二者の扱いが食い違う。
- `docs/development/DEVELOPMENT.md:275` は「pinned 順・返信ページング等は未検証」と明記している。
- 旧 ROADMAP の評価: 「合成 fixture で固定」は方向として妥当。ただし固定する対象の多く（履歴・thread の pagination 挙動）は既に inline stub で固定済み。未着手なのは fixture の資産化、request 側のパラメータ検証、`keep_read_status` の全経路検証、未確認の契約の記録。

**設計方針**
- **契約を固定できないなら、契約に依存しないことを固定する**。順序に依存しない安全性を、seed 固定のプロパティテストで固定する。項目の多重集合を任意にページ分割し（pinned 先頭・完全ランダム・境界にはみ出し）、`fetch_history` / `fetch_thread` が必ず全対象を返すこと、`reached` は最終ページ（`has_next=false`）でだけ真になることを表明する。
- 形状 fixture（実応答の写しではない完全合成の JSON）を `tests/ingest/fixtures/mcs_wire/` に置く。projects の各ページ、messages の履歴 pinned 例、thread の p1/p2/`paginate` 欠損/tombstone、latest、project detail の既読/未読、`has_next` 型不正、snippet のみ、など。`index.json` に契約版（例 `mcs-wire/1`）・ルートテンプレート・期待パラメータ・不変条件 ID・各 fixture の sha256・`synthetic: true` を持たせ、各不変条件に `verified: synthetic | live-shape-probe | unverified` を付ける。
- `tests/ingest/wire_replay.py`（testkit）に `ReplayWorker` を作り、`MCSAdapter(worker=...)` に渡す。`(method, path, params)` → fixture の base64 応答を返し、全リクエストを記録する。未知ルートは fail-closed。message 系ルートで `keep_read_status=1` がなければテスト失敗。`sort=pinned`・`per_page`・`page` を検証する。
- 完全合成の担保: 値は予約した ID 帯（例 9×10⁸ 台）、氏名 `SYNTHETIC_*`、本文 `SYNTHETIC BODY n`。fixture の全文字列値が許可パターンに一致することを検査するテストを置く。
- オーナー実行の任意プローブ `scripts/mcs_wire_probe.py`（CI 対象外）: 既存の adapter 公開メソッドだけを使い（`keep_read_status` 注入済みの GET のみ）、本文・氏名は出さず**形状の署名だけ**を出す（`has_next` の型、order の単調性、thread の `paginate` 有無、`count.thread_messages` と実件数、tombstone の扱い、`/messages/latest` の既読副作用の有無）。結果を fixture の `verified` に反映する。実 MCS に触れるので、実行はオーナーの明示承認の下でだけ行う。
- 判断が要る変更: thread の `paginate` 欠損を SchemaError（fail-closed）にするか。現状維持は `merge_full_replies` の `len(got) < reply_count` 検査（`job_ops.py:289-295`）に依存する（#2-D1）。

**成果物**
- 新規: `tests/ingest/fixtures/mcs_wire/*.json`（約 12 件）、`tests/ingest/wire_replay.py`、`tests/ingest/test_wire_contract.py`、`scripts/mcs_wire_probe.py`（任意）。
- 変更（任意）: `mcs/ingest/mcs_adapter.py`（thread の `paginate` 欠損を厳格化する場合だけ）、`docs/development/DEVELOPMENT.md:275`（検証状況の記述）。

**受入条件とテスト（合成のみ）**
- 全 message 取得経路（未読・履歴・thread・thread window）で `keep_read_status=1` が付くことを ReplayWorker で固定する。`fetch_history` に `sort=pinned` が付くことも固定する。
- プロパティテスト（seed 固定）で、pinned 先頭・完全ランダム・境界跨ぎでも、欠落なし・誤認証なしを確認する。
- thread の `has_next` 真で `max_pages` 到達なら `thread_incomplete`。window 再開で完了ページを保持する。`paginate` 欠損の挙動を明示的に固定する（現状維持または厳格化）。
- `has_next` が非 bool、空ページで `has_next` 真、ページ間の重複 id を扱う。
- fixture に実データがないことの lint が通る。fixture の sha256 が `index.json` と一致する。
- 既存の 62 テストが無変更で通る。ネットワーク遮断は `tests/conftest.py` のガードのまま。

**依存・順序**: なし。#3（返信分類）と #4（floor の信頼度）の前提知識を固めるので先行して着手できる。

**規模**: S〜M。テストと fixture が中心で、production コードの変更は任意。ReplayWorker と 12 件程度の fixture、約 20 テスト。

**オーナー判断・リスク**
- 合成 fixture が固定するのは「我々の理解」であり、実 API の契約ではない。実 API の確認は、オーナー実行のプローブでしかできない。プローブは承認範囲を明示して実施する。
- `#2-D1` thread の `paginate` 欠損を fail-closed にするか。厳格化は、API が実際に欠損させたときに取得が止まる代わりに、黙って切れなくなる。
- 【未確認】実 API の pinned 順序、`paginate` 欠損の有無、`count.thread_messages` が tombstone を含むか、`/messages/latest` の既読副作用。

## #3 取得不能返信の分類

**目的**: 取得できなかった返信について、理由（再試行で直る / 恒久的に取れない / 削除済み / 説明不能）を耐久的に記録し、view と C1 の coverage で区別できるようにする。「記録が見つからない ≠ 対応がなかった」を保つ。

**現状**
- 欠落の記録先は 5 つ。
  1. `fetch_jobs` の `kind IN ('reply','thread')`（state / attempts / next_try / payload.page のみ: `mcs/core/ledger.py:197-207`）。
  2. `messages.body_state`（`unknown|snippet|full|deleted`。`TERMINAL_BODY_STATES` は full / deleted: `ledger.py:41`）。
  3. 親の `reply_count` と保存済み full 返信の差（view で導出のみ: `mcs/views/mcs_view.py:201-205`）。
  4. run 単位の `runs.error`（`ledger.py:508-512` で 500 文字、最大 8 件連結: `run_check.py:1084-1087`）。
  5. 患者単位の `patients.fetch_reason`（未読経路だけが更新する最新 1 件: `run_check.py:377-410`）。
- `fetch_jobs` に理由の列はない。**`mcs_view` の status は全 job グループの `reason` を `not_recorded` に固定している**（`mcs_view.py:210-211`、`docs/development/DEVELOPMENT.md:273` も同趣旨）。
- 再試行の会計は `job_retry(max_attempts=8)`（`ledger.py:1273-1286`）。上限で `failed` になるが、理由は残らない。`failed` の返信 job は floor 認証を止めない（`pending_reply_jobs` は pending だけを数える: `ledger.py:1534-1543`）。つまり取れなかった返信があっても floor が確定しうる。
- 実際に出る理由: adapter は `network_error`・`http_error`・`forbidden`（`mcs_adapter.py:902`）・`session_expired`・`schema_error`・`deadline_exceeded`・`pages_exceeded`・`thread_incomplete`（:1068）・`no_token`・`bad_snapshot_ts`・`mark_result_unknown`（:1423）・`download_*`・`url_not_allowed`・`response_too_large`（`mcs_worker.py`）を出す。患者側では `parent_body_incomplete`（`run_check.py:378`）・`replies_missing`（:407）、job 側では `replies_missing`（`job_ops.py:421`）・`window_stalled`（:541）。
- view の語彙 `REASONS`（`mcs_view.py:23-29`）との食い違い: 未使用の `body_incomplete` が入っている一方、`parent_body_incomplete`・`forbidden`・`download_empty`・`mark_result_unknown` は入っておらず `other_error` として表示される。
- 前例: 添付は `attachment_failed(kind)`（`ledger.py:1614-1649`）が恒久/一時を分類して `attachments.error` に記録する（恒久は `_ATTACH_PERMANENT` :45-46）。view にも出る（`mcs_view.py:216-223`）。返信 job にはこれがない。
- `mark_result_unknown` は既読化の話で、返信欠落の理由ではない。記録先は `read_marks.status='unknown'`（`ledger.py:188-191`）と run のエラー文字列（`run_check.py:463-465`）。旧 ROADMAP §2 で完了扱いなのは「kind を出す」ところまで。
- 2026-09-23 の実観測では返信 job 19 件はすべて完了した（`docs/dev-records/continuation-20260923.md:227-233`）。発生頻度は低く、予防的な整備。

**設計方針**
- 添付の前例（F11）を返信 job に写す。
  - `fetch_jobs` に nullable な `error TEXT` を加法 migration で追加する（前例 `_migrate_body` の `ledger.py:349-355`、`attachments.error`）。SCHEMA_VERSION は 7 のまま。
  - `job_retry` / `job_fail` / `job_defer` の呼び出し側（`job_ops.py:361,378-392,417-419,428` ほか）で、分類済みの理由を書く。`job_done` と revive（`_job_add_tx`）で消す。payload の JSON キーは `job_add` の ON CONFLICT（`ledger.py:1218-1220`）で消えるため不採用。
- 閉じた語彙（安定トークン）を定義する。
  - 一時: `network_error`・`deadline_exceeded`・`http_5xx`・`http_429`・`session_expired`。
  - 恒久: `forbidden`・`http_4xx`・`schema_error`。
  - 構造: `thread_incomplete`・`replies_missing`・`window_stalled`。
  - 状態: `deleted`（tombstone。失敗ではない）。
  - 既定: `not_recorded`（旧行）。
- 恒久系は即 `failed` にする（`attachment_failed` と同じ）。`SessionExpired` は従来どおり attempt を消費しない。
- view: `mcs_view.py:210-211` の固定値をやめ、`GROUP BY kind,state,error` で出す。`REASONS` に不足分を足し、未使用の `body_incomplete` を整理する。`incomplete_reply_roots` の tombstone の扱いを `merge_full_replies` と統一する（実 API の確認とセット）。`mcs_view` は旧 snapshot（列がない）でも読めるよう `PRAGMA table_info` で存在確認する。
- floor との関係（#3-D1）: 恒久欠落があっても floor を確定させるが、status に `known_gaps` 件数を併記し、C1 の coverage では「不明」として扱う。厳しくすると floor が永久に付かず、deep import が止まる。

**成果物**
- 変更: `mcs/core/ledger.py`（migration、`job_retry` 系、`job_add` 系）、`mcs/ingest/job_ops.py`（分類の書込）、`mcs/views/mcs_view.py`（status）、`docs/development/DEVELOPMENT.md:273`。新規ファイルなし（テストは既存ファイルへ追加）。

**受入条件とテスト（合成のみ）**
- `tests/ingest/test_job_drain.py` の流儀（偽 adapter が `MCSError(kind, status=...)` を投げる）で確認する。各 kind が `fetch_jobs.error` に記録される。恒久系は 1 回で `failed`、一時系は 8 回で `failed`。`SessionExpired` は attempt 非消費。成功時と revive 時に `error` が消える。
- 列のない v7 DB を `Ledger` で開くと migration で列が追加される（`test_interrupted_v7_migration_reruns_backfill`: `tests/ingest/test_job_drain.py:1055` の型）。
- 列のない旧 snapshot を `mcs_view` が読め、status に理由が出る。
- tombstone 入りスレッドで、view の `incomplete_reply_roots` と job 側の完了判定が一致する。
- 恒久欠落で floor は付くが `known_gaps>0` が併記される。

**依存・順序**: #2（tombstone と `reply_count` の意味）の後が望ましい。#4 の前に入れる（修復の判定に理由が要る）。

**規模**: M。ledger の migration と job 会計、job_ops の 3〜5 箇所、view、約 10 テスト。

**オーナー判断・リスク**
- `#3-D1` 恒久欠落があっても floor を確定させるか。恒久系の即 `failed` は floor 確定を早めるので、`known_gaps` の併記と、C1 での「不明」扱いを必須にする。
- 旧 snapshot との互換（view の列存在確認）は必須。
- 【未確認】実 API での `count.thread_messages` と tombstone の関係。

## #4 既存データの修復

**目的**: 過去のコードで作られた取得状態（特に、返信失敗時にも確定しえた `history_floor`）を、独立 backup の下で検証・補完・再生成し、実施記録を残す。C1 の本番投入の前提条件を満たす。

**現状**
- 旧 ROADMAP の順序: 独立 backup → A 系 → coverage/import job → 通知なし再照合（GET のみ）→ 返信・添付の補完 → hash/artifact 再生成 → rollup 再構築。各段のツールは次のとおり。
  - 独立 backup: `maintenance.preupdate_backup`（`mcs/core/maintenance.py:80-104`）。CLI はない。`preupdate-*` は update_state 未参照だと日次 backup 時に削除される（:77,135-141）ので、修復用には別 prefix か別媒体が要る。
  - A 系: コードは v1.0.6 で反映済み。migration は `Ledger` を開くと走る（`ledger.py:150-291`）。`user_version=7`（:33）を確認する。
  - coverage/import job: `data/cmd` の import（`job_ops.py:74-87,177-206`、形式は `docs/development/DEVELOPMENT.md:147`）、`init_data.py`（`mcs/ingest/init_data.py:39-`）、discovery / `seed_trickle`（`job_ops.py:652-790`）。**`floor <= since` の患者は import を skip する**（`job_ops.py:181-183`、`init_data.py:127-130`）。疑わしい floor は再取込されず、floor を無効化する手段もない（`set_history_floor` は単調: `ledger.py:904-917`）。
  - 通知なし再照合（GET のみ）: `reconcile` job（`job_ops.py:570-647`）。`save_messages` に notify を渡さない（:623-624）。GET のみで `keep_read_status=1`（`mcs_adapter.py:1228-1229`）。ただし対象は floor 済み患者だけ（:574-576）で、1 呼出しあたり 2 頁 × 2 患者（:48-50）。全頁を歩き切ると 6 時間 park して最初からやり直す（:629-633）ので、「終わった」記録が残らない。
  - 返信・添付: reply job（`job_ops.py:313-429`）、`replies_without_job`（`ledger.py:1545-1560`）、`stage_attachments`（`run_check.py:616-641`）。署名 URL が更新されると failed の添付は自動で pending に戻る（`ledger.py:530-552`）。reconcile の再歩行がこの再開を誘発する。
  - hash/artifact 再生成: v1 は `extract._delete_stale`（`mcs/extract/v1/extract.py:310-329`）と `extract.py --all`、v4 は `extract_llm` の stale 削除と drainer、semantic は `ops.retry`。
  - rollup 再構築: `rollup.py`（`mcs/extract/rollup.py:406-427`。`--all` は実際には無視されて常に全件）と dirty 検出（:353-391）。
- 「floor を疑う」判定手段も、修復の実施記録を残す仕組みもない。既存の記録類は `runs`、`command_receipts`、`restore_report.json`、`docs/dev-records/*.md`（手書き）だけ。
- `mcs_view` は常に `gapless_verified:false`・`exact_missing_ranges:null`（設計上「完全を主張しない」）。
- 旧 ROADMAP の評価: 「通知なし再照合」は reconcile job として既にあり、足りないのは対象範囲（floor 済みだけ）・完了記録・進捗の可視化。実行時間の見積もりは実データが読めないので【未確認】。

**設計方針**
- **読み取り専用の `plan`（dry-run）**: 最新 snapshot を `LedgerReader`（ro）で読み、修復候補を集計する。書込みも MCS 接続もない。
  - 患者ごとの floor 分類（trusted / suspicious: 返信欠落・coverage なし・pending job）。
  - `incomplete_reply_roots`、reply/thread job の state×理由（#3 後）、添付の state 別、stale artifact 件数（`_delete_stale` の SELECT 版）、dirty rollup。
  - 各段に「使う既存コマンド」「GET 数の上限見積」「可逆性」「前提」を付けた `plan.json` を出す。
- **書込みは既存の入口だけ**を使う（`data/cmd` の import、通常 tick、`--jobs-only`、`extract.py --all`、`rollup.py`）。修復ツール自体は `Ledger()` を開かない（CI ゲート `gate_writer_lock`・`gate_snapshot_readonly` を満たす）。
- floor は消さない。`plan` が「floor の信頼度」を導出する（永続化しない）。reconcile の全頁完了を記録するため、job の payload に `passes`・`last_pass_at` を足す（`run_reconcile_jobs` の完了分岐: `job_ops.py:629-633`）。reconcile の seed 対象を「floor が suspicious な患者」に広げるかは判断事項（#4-D1）。
- **実施記録**: `record` サブコマンドが `data/repair/<repair_id>/log.jsonl` に追記（fsync）する。各段の before/after の集計値、開始・終了時刻、コード版（`git describe`）、`user_version`、backup の名前と sha、操作者、エラー件数を持つ。最後に `finalize` で件数だけの `docs/dev-records/*.md` を出す。記録に `FIX-`・`AUDIT-` 等の接頭辞 ID を入れない（`ci/mine_gates.py`）。
- **段の順序ゲート**: 直前の段の記録がなければ、次の段の `record` を拒否する（理由付きの skip だけ可）。
- backup の段は、#1 のオフサイト 1a が済んでいれば「独立媒体」を満たす。なければ一時的に `repair-<ts>-` prefix の手動複製を別ディスクへ置く。

**成果物**
- 新規: `mcs/ops/mcs_repair.py`（`plan` / `record` / `finalize`）、`tests/ops/test_mcs_repair.py`。
- 変更: `mcs/ingest/job_ops.py`（reconcile の完了記録、seed 対象の拡張は任意）、`mcs/core/maintenance.py`（修復用 backup の prefix、または CLI 露出）、`docs/development/DEVELOPMENT.md`（`update_readme.py` で再生成）。

**受入条件とテスト（合成のみ）**
- 合成 DB（v7）で `plan` が決定的な集計と手順を出す。DB ファイルの hash が実行前後で不変（書込みゼロ）。
- 出力に患者名・本文が含まれない（許可トークンだけの検査）。旧 snapshot（列がない）でも `plan` が動く。
- `record` が追記のみ・fsync・順序ゲートを守る。
- reconcile の完了記録が、全頁完了時にだけ `passes` を増やす（既存 `test_reconcile_full_pass_parks_then_rotates`: `tests/ingest/test_job_drain.py:1239` の拡張）。
- `ci/gates.py` が pass する。

**依存・順序**: #1（独立 backup）、#3（理由の記録）、#5（重い再歩行中の tick 耐久性）が前提。C1 の本番投入と Q6 の判断より前に完了させる（ROADMAP §4 C1）。RULE_VERSION の再生成（#15-B）はこの「hash/artifact 再生成」と同じ窓で 1 回にまとめる。

**規模**: M（`plan` 側）+ オーナー運用（実行）。実行時間は患者数 × 頁数で、実行前に見積もる。

**オーナー判断・リスク**
- `#4-D1` 疑わしい floor の扱い（再歩行だけか、対象を広げるか）。
- `#4-D2` MCS への GET 負荷とセッションへの影響の許容。
- `#4-D3` 実行時の記録・sign-off の担当者。
- 修復中に semantic job が増える（LLM 時間）。夜間 drain 枠との兼ね合い。
- 【未確認】実データの規模（患者数・頁数）、reconcile が完了するまでの日数。

## #5 通信中の deadline

**目的**: 1 回の収集 tick が `RUN_DEADLINE_S` を大きく超えて走り続けたり、時間切れが原因で「不確実な送信」を作ったりしないようにする。

**現状**
- `RUN_DEADLINE_S = 480`（`mcs/ingest/run_check.py:60`）。deadline はロック取得後に始まる（:1149-1156）。ロック待ちは最大 150 秒（`LOCK_WAIT_S` :204）で deadline に含まれない。
- MCS API・添付・CDP・Keychain・Chrome 起動は、いずれも deadline に縛られる。`_request` → `_io` → `bounded_call` が worker を `min(timeout, deadline)` で kill・回収する（`mcs_adapter.py:513-532,871-913`、`mcs/ingest/mcs_worker.py:30-95`）。テストは `tests/ingest/test_adapter_wire.py:743-802`（api / download / bootstrap）、:712-720、:917-963。ローカル LLM と Jev も worker 経由で kill され、`need_s` の下限ゲートがある（`mcs/extract/v4/extract_llm.py:1017-1049`）。**つまり、MCS / LLM の通信経路は実装済み**（旧 ROADMAP の F13）。
- 超えうる箇所:

| 箇所 | 上限の仕組み | 超過の可能性 | 根拠 |
|---|---|---|---|
| `hermes send`（`mcs/notify/notify_flush.py:641-666`） | `subprocess.run(timeout=min(180, remain))` | 超えない。ただし `remain` が小さいと途中打切りで `_SendUncertain` → hold になり、**deadline が不確実性を作る**。下限がない | :647-657、:663-666 |
| `extract.run_pending`（`extract.py:332-357`） | なし | 超える。`RULE_VERSION` 更新後は全件を 1 tick で再抽出し、1 行ごとに commit（`ledger.py:1814-1820`） | `run_check.py:655-659` |
| `rollup.rebuild_many`（`rollup.py:394-403`） | なし | 超えうる | `run_check.py:701-712` |
| `daily_backup` の再検証と `publish_snapshot` | なし（毎 tick） | DB サイズに比例。約 76MB では秒オーダー | `maintenance.py:55`、`run_check.py:1089` |
| SQLite の busy | `timeout=30`・`busy_timeout=30000` | 1 文あたり最大 +30 秒 | `ledger.py:110,123` |
| `bounded_call` の kill 後の回収 | `process.communicate()` に timeout なし | 理論上のみ | `mcs_worker.py:65,70` |
| プロセス全体 | 外側は hermes cron の script timeout 3600 秒（テスト定数） | 固着するとロックを最大 3600 秒保持 | `tests/meta/test_deployment_scripts.py:12-14` |

- 実 tick の所要時間の分布は未確認。`runs` 表に `started_at` / `finished_at` が残るので、コードなしで測れる（下記）。途中で kill された run は次の `begin_run` が `crashed` に更新する（`ledger.py:498-501`）。`health_watch` の猶予は 480 秒固定（`mcs/ingest/health_watch.py:41`）。
- 旧 ROADMAP の評価: 「収集 tick の耐久性」という枠は妥当だが、通信経路は既に完了している。残りは `hermes send` の下限、非通信ステージ、プロセス全体の上限。

**設計方針**
1. **まず測る**（コード不要）。オーナーが snapshot に対して `SELECT kind,status,COUNT(*),MAX(finished_at-started_at),AVG(finished_at-started_at) FROM runs WHERE finished_at IS NOT NULL GROUP BY kind,status;` を実行する（実データの集計になるので、調査側では未実行）。
2. **`hermes send` の最小予算**: `_send` と `_send_text` のループで、残りが `SEND_MIN_BUDGET_S`（例 30 秒）未満なら送信を始めない（`_SendFailed` で proven-not-begun。マーカーも戻る）。既存の LLM の `need_s` 前例（`extract_llm.py:1017-1023`）に倣う。#6 と共通の修正。
3. `extract.run_pending` と `rollup.rebuild_many` に `deadline` を渡す。1 tick の処理量の上限と、まとめ commit（`artifact_add_tx`: `ledger.py:1801-1812` が既にある）で fsync を減らす。
4. housekeeping: 計測で問題が出た場合のみ、`daily_backup` の毎 tick 再検証を「mtime・サイズ不変なら省略」に、`publish_snapshot` の頻度を見直す（CCO が新しい snapshot を待つ前提の確認が要る）。
5. **プロセス全体の上限**: `faulthandler.dump_traceback_later(RUN_DEADLINE_S+猶予, exit=True)`（stdlib）。固着位置のトレースが `run.log` に残り、次 tick が `crashed` にする。トレースは関数名・行だけで PHI を含まない。順序上、手順 2 を先に入れる（上限で kill されうる箇所を非通信ステージに限り、送信途中の kill による held の増加を避ける）。
6. health に `run.elapsed_s`・`overshoot_s`・`slowest_stage` を足し、閾値超過で `degraded` にする。

**成果物**
- 変更: `mcs/ingest/run_check.py`（stage 計時・health・watchdog）、`mcs/notify/notify_flush.py`（下限）、`mcs/extract/v1/extract.py`、`mcs/extract/rollup.py`、`mcs/core/maintenance.py`（条件付き）、`mcs/ingest/health_watch.py`（閾値）。新規ファイルなし（テストは既存ファイルへ追加）。

**受入条件とテスト（合成のみ）**
- `run_check.main()` を `_point_run_check_at`（`tests/ingest/test_run_check_stages.py:1425` 付近）で動かし、遅いステージの stub が deadline を越えたとき、後続のステージが省略され、全体が想定時間内に戻る。
- `notify_flush`: 残り時間が下限未満のとき `subprocess.run` が呼ばれずマーカーが残らない。十分あるときは従来どおり。
- `extract.run_pending(deadline=...)` が途中で止まり、次回に続きから処理する。まとめ commit でも結果が同じ。
- watchdog を子プロセスで検証する（短い秒数で arm → 非ゼロ終了 → 次の `begin_run` が `crashed`）。health に `run` 節が出る。

**依存・順序**: 手順 2（送信下限）は #6 と同時に入れる。手順 5（watchdog）は手順 2 の後。#4 の重い再歩行の前に完了させる。

**規模**: M（送信の下限だけなら S）。複数モジュールの小変更と 10 件前後のテスト。

**オーナー判断・リスク**
- `#5-D1` watchdog を入れるか、猶予秒数をどうするか。kill は crash と同じ扱いで、text 経路の in-flight 送信は hold になる（手順 2 で緩和）。
- `#5-D2` `publish_snapshot` の頻度を下げると CCO の鮮度が下がる。
- 【未確認】実 tick の所要時間の分布、macOS のスリープ中の `time.monotonic()` の進み方（deadline 計算への影響）、hermes cron の実機の script timeout 設定値。

## #6 通知受付直後のクラッシュ時の重複

**目的**: 通知の配送で、送信が受理された直後のクラッシュが重複投稿や原因不明の送信保留を生まないようにする。結果不明は照合に回す。

**現状**
- **旧 ROADMAP の「重複」は text 経路では既に対策済みで、記述が陳腐化していた**。`outbox_progress(sending=)` の write-ahead マーカー（`mcs/core/ledger.py:1713-1727`）が、送信前に `sending=i+1` を書き（`notify_flush.py:684-696`）、次回に `sending > next` を見つけたら再送せず hold する（:805-812）。導入は 392f030（2026-09-23、F19）で、旧 ROADMAP の元の残件メモ（2026-09-19）より後。テストは `tests/notify/test_notify_flush.py:110-138`、`tests/notify/test_notify_flush_paths.py:334-380`。
- card 経路（`INTERACTIVE_KINDS = {new_messages, signal}`: `mcs/notify/notify_cards.py:53`）は journal 方式。`started` を HTTP 前に fsync し、`result` を応答直後に fsync してから receipt を出す（`adapters/common/journal.py:1-25`、`worker.py:1-21`）。`started` 後に途絶えたら `unknown` になり再送しない。運用者は `ops.card_resolve`（`mcs/notify/notify_transport.py:602-790`）で解決する。テストは `tests/adapters/discord/test_mcs_discord.py:735-812`、`tests/notify/test_notify_reconcile.py`。
- 残る実際の課題:
  1. 送信の途中打切り（#5 の下限欠如）が「不確実」を作る（`notify_flush.py:647-657`）。
  2. text 経路で hold された event に、**解決手段も一覧も理由の記録もない**。`_hold_event`（:504-567）は理由を持たない。view は件数だけ（`run_check.py:121-124,165-169`）。`ops.card_resolve` は card 専用。hold は health を `degraded` にし続ける。
  3. text 経路の対象には alert 系（`session_expired`・`session_recovered`・`run_failed`・`update_notice` など: `notify_flush.py:74-78`）が含まれる。これらが不確実で hold されると、異常を知らせる通知自体が黙る。alert は「重複しても良い」側が安全。
  4. **restore 後**、text 経路は `restore_pending` に縛られない（前提 5）。復元で巻き戻った DB の pending event がそのまま再送されうる。加えて、再収集された投稿が新規扱いで再通知される（`--no-notify` は送信だけを止める: `run_check.py:1006,1118`）。
  5. Hermes/SDK 内部の再 POST による重複は core からは防げない（`adapters/common/worker.py:10-14`、`hermes_plugin/README.md:196-207`。作成系 POST は card 経路だけ単発化）。
- 再利用できる既存の hold / reconcile パターン: `outbox_progress(sending)` の write-ahead、`_hold_event` の「未送信が証明できる場合だけの救済」、`notification_restore_holds` と `ops.card_resolve`（operator の証拠付き resolve）、`GovernedExporter` の held / acked / delete_held と `reconcile`（`mcs/ops/ext_contract.py:555-600`）。
- 実際に held が発生しているかは未確認（実データ）。オーナーが snapshot に対して `SELECT kind,COUNT(*) FROM notify_outbox WHERE state='failed' AND next_try IS NULL GROUP BY kind;` を確認する。ゼロなら resolver は後回しでよい（実需が出てから）。

**設計方針**
1. #5 の手順 2（送信下限）を共通で入れる（最優先・小）。
2. `flush()` が `restore_pending` を見て、content 系（`new_messages`・`signal`・`attachment_followup`・`semantic_notice`）の送信を保留する。alert 系だけ通す（card 経路と同じ姿勢: `notify_transport.py:183-188`）。
3. 種別ごとの配送方針表を持つ。既定は at-most-once（現状）。alert 系は at-least-once（不確実でも hold せず、バックオフで再送）。どの種別を at-least-once にするかは #6-D1。
4. hold の理由を残す（`notify_outbox` に nullable な `hold_reason` を加法追加。前例 `progress`）。
5. hold 一覧を出す（本文なし: event_id・kind・age・progress・reason）。text 経路の解決コマンド（`ops.card_resolve` の型を踏襲）は、実需が確認できた後に段階導入する。`mark_delivered` は次の chunk へ進める、`mark_not_sent` は保留された chunk から再開する、と定義する。
6. 故障注入テストを網羅する（下記）。

**成果物**
- 変更: `mcs/notify/notify_flush.py`（下限・restore ゲート・方針表・理由記録）、`mcs/core/ledger.py`（`hold_reason` migration、`outbox_hold` の引数）、`mcs/ingest/run_check.py`（health に held の内訳）。
- 任意（実需確認後）: `mcs/ops/mcs_requests.py` と `mcs/ingest/job_ops.py`（resolve コマンド）、`mcs/views/mcs_view.py`（hold 一覧）。
- 文書: `hermes_plugin/README.md`、`docs/specs/lifecycle-spec.md` の該当節。

**受入条件とテスト（合成のみ）**
- text 経路の故障注入（`Crash(BaseException)` を注入点で送出 → 新しい `Ledger` で `flush` を再実行）。注入点は `outbox_progress(sending)` 直後、`hermes send` 成功直後（marker クリア前）、marker クリア後（accepted 前）、accepted 後。各点で `hermes send` の呼出回数が chunk あたり 1 回以下（重複なし）で、event は accepted か理由付き hold。alert 系（at-least-once）は再送が正確に 1 回。
- 下限テスト: 残り時間が下限未満なら送信が始まらず、marker が残らない。
- restore ゲート: `restore_pending` 中に content 系は送られず、alert 系は送られる。マーカー解除後に再開する。
- hold の理由が記録され、health に内訳が出る。
- 既存の F19 テスト（`test_notify_flush.py`、`test_notify_flush_paths.py`）と card 経路の既存テストが無変更で通る。

**依存・順序**: #5 の手順 2 と同時。restore 手順は #1 と整合させる。resolver は実需確認後。

**規模**: S〜M（手順 1〜4 と 6 は S〜M、resolver を含めると M）。

**オーナー判断・リスク**
- `#6-D1` at-least-once にする種別の選択（重複の許容と、沈黙の回避のトレードオフ）。
- `#6-D2` 既存の held の扱い（一括の再送は重複を生むので、実需確認後に個別判断）。
- Hermes/SDK 側の重複は core では防げない。plugin 側（別リポジトリ）の課題として残る。
- 【未確認】現行 `config.json` で interactive が有効か、hold の実発生件数、実 Discord/Slack での挙動。

## この領域の実施順

1. **コードなしで先に測る・決める**: #5 の tick 所要時間 SQL、#6 の held 件数 SQL（オーナーが snapshot に対して実行）。#1-D1〜D6（外部保存の許可・保管先・鍵エスクロー・openssl の扱い・FileVault）。理由: #1 は不可逆な損失に直結する（FileVault Off・Time Machine 未設定・同一ディスク）。判断待ちの間に他を進められる。
2. **小さく安全なもの**: #5 手順 2 と #6 の下限・restore ゲート（共通の S）、#2 の fixture / ReplayWorker。#5 手順 2 は #6 の「時間切れ由来の不確実」を直接潰す。#2 は本番影響がなく、#3・#4 の前提（契約の理解）を固める。両者は独立で並行できる。
3. **並行で 2 本**: #1 の 1a（`offsite` + `verify` + テスト）と #3。触るファイルが分離している（`maintenance.py` / 新規 `mcs_backup.py` と `ledger.py` / `job_ops.py` / `mcs_view.py`）。#3 は #4 の前提、#1a は #4 の「独立 backup」の前提。
4. **#5 の残り**（非通信ステージの上限・health・watchdog）と **#6 の残り**（hold 理由・方針表）: `notify_flush` が共通なので同じ枝で順に行う。watchdog は送信下限の後。
5. **#1 の 1b / 1c**（cron・health・文書・初回の実機 drill）。
6. **#4 の `plan` → 実行 → 記録**: #1 の独立 backup、#3 の理由記録、#5 の tick 耐久性が揃ってから。重い再歩行は tick を圧迫するため。実施記録と Q6 の判断が C1 の本番投入の前提（ROADMAP §4）。
7. **#6 の resolver**: hold の実発生が確認できた場合だけ。

番号順（#1→#6）と違うのは、#2 と #5 手順 2 が安価で他の前提になること、#4 が #1・#3・#5 に依存することによる。
