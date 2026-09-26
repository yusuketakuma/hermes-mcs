# 自動アップデート機構 — 設計計画

## 復旧時の未追跡ファイルの扱い（2026-09-25更新）

現行実装では、更新差分に同名があるという理由だけで未追跡ファイルを
自動削除しない。`mcs_update` と独立 recovery の双方からこの削除処理を廃止した。
`git reset` による追跡済みファイルの復旧は維持する。その後に未追跡ファイルを
名前の一致だけで追加削除する処理を行わない。
以下の「外科的削除（tag-fileset と untracked の一致）」の記述は過去の設計・
レビュー記録であり、現在の復旧手順として使わない。利用者が更新中に同名ファイルを
作成した場合も、更新処理が生成したものと名前だけでは区別できないためである。

**実装済み**（2026-09-24）。実装ファイル: `mcs/ops/mcs_update.py`、
`deployment/scripts/mcs_update.sh`、`deployment/recovery/mcs_recover.py`、
`deployment/launchagents/org.mcs.recovery.plist`、`mcs_setup.py` の
manifest/reconcile・`mcs_operations`/`mcs_requests` の projectless ops・
`hermes_plugin` の preview/confirm・`maintenance.preupdate_backup`。
テスト: `tests/ops/test_mcs_update.py`・`tests/meta/test_mcs_recover.py`・
`tests/plugin/test_update_ops.py`。既定モード `off`（config の
`update.mode` で opt-in）。

決定済み: 適用モードは `update.mode` で切替（`off`/`notify`/`auto`・
既定 `off`）、更新チェックは日1回（`10 5 * * *`）。

## 目的と範囲

GitHub Releases (`yusuketakuma/hermes-mcs`, semver tag) に新バージョンが公開されたとき:

1. 検出して通知する（存在 + リリース内容）
2. 安全性を事前チェックする
3. 承認または自動で適用する
4. 失敗時・事後チェック不合格時にロールバックする

範囲外: hermes-agent 自体の更新（install.sh の pin 更新は手動運用）、
依存物（brew パッケージ・モデル）の更新、本 repo 以外の更新。

## 前提（コード照合済み・2026-09-24）

- デプロイ = git checkout そのもの（`~/.mcs`）。`data/` は gitignore で更新対象外。
- バージョン = git tag。コード内に VERSION 定数はない → 比較は tag/SHA ベース。
- `acquire_run_lock(data/run.lock)` が既存 — apply はこのロックを取得して
  収集 run と排他にする。
- `ops.*` コマンド経路: Discord `/mcs` → `data/cmd` → `drain_commands` →
  `mcs_requests.apply_command`（receipt 先着・command_id 冪等）→
  `validate_ops`（フィールド allowlist 制）→ `mcs_operations.apply_tx`。
- **制約A**: `drain_commands` は run lock 内で実行される。ops ハンドラが
  ロックを同期取得するとデッドロック → `ops.update_apply` は「承認記録 +
  detached 起動」に限定し、実 apply は別プロセスで lock 待ちする。
- **制約B**: `ops.update_apply` を受理するには現行コードがコマンドを
  知っている必要がある（ブートストラップ: updater 自体の初回デプロイ後から
  Discord 承認が有効になる）。
- システム通知は outbox 経由: `ledger.outbox_add(kind, ...)` →
  `notifier.flush` → `_format_event` が `kind` 分岐・`_target` が
  `session_expired`/`run_failed` を `notify_system_target` へ振る。
  **`semantic_notice` は mode=enforce ゲートがあり流用不可** →
  新 kind `update_notice` を追加し `_format_event` と `_target` の
  system グループに1分岐ずつ足す（payload は凍結テキストを持つ）。
- **DB スキーマは user_version で厳格管理**: `Ledger.__init__` は
  `PRAGMA user_version > SCHEMA_VERSION` を拒否（ledger.py:104）—
  **列追加でも番号が上がれば旧コードは DB を開けない**。
  「additive なら戻せる」という前提は誤りだったため撤回する。
- **`merge --ff-only` の原子性は限定的**: HEAD/ refs は原子的だが、
  checkout 中断で「HEAD=旧版・一部ファイル新版」の混在ツリーが
  再現された（MERGE_HEAD なし・merge --abort 不可）。段階単位の
  永続化ジャーナルとツリー整合検証が必須。
- **`.gitignore` はデータ保護ではない**: 候補ツリーが ignored パスを
  追跡すると merge がローカル設定を上書きし、rollback でファイル自体が
  消える。候補ツリーに保護パスが含まれないことの必須チェックが必要。
- MCS コアは stdlib 限定 → GitHub 連携は `git` + `urllib`（公開 repo は
  認証不要）。`gh` には依存しない。
- 常駐プロセス: `ai.mcs.extract-drainer{,-rt}` は KeepAlive 常駐、
  `local.mcs-{cmd,int}` は WatchPaths 起動だが run.lock 内で動く。
  **run.lock は常駐 drainer を隔離しない** — 停止と再起動の明示手順が必要。

## 構成

```
mcs/ops/mcs_update.py            新設: status / check / apply / rollback / recover
deployment/scripts/mcs_update.sh cron wrapper（exec-first のみ・fallback なし）
deployment/recovery/mcs_recover.py  repo 外配置の凍結復旧プログラム
deployment/launchagents/org.mcs.recovery.plist  独立 watchdog（launchd・非 cron）
mcs/ops/mcs_setup.py             CRON_JOBS+AGENT_LABELS 分割・manifest・update.* 検証
mcs/ops/mcs_operations.py        validate_ops 登録 + ops handler（receipt_json 固定）
mcs/ops/mcs_requests.py          validate() の projectless ops 例外（S1）
hermes_plugin/__init__.py        _CONTROL_FIELDS/_confirm/projectless 認可（S2）
mcs/ingest/notifier.py           outbox kind `update_notice` 追加（_format_event + _target）
mcs/core/maintenance.py          preupdate_backup + 参照ベース prune
deployment/scripts/mcs_llm_catchup.sh  marker 検査（quiesce 中の再 spawn 防止）
install.sh                       recovery 配置 + watchdog bootstrap（6/6 段）
tests/ops/test_mcs_update.py     新設（一時 git repo fixture + subprocess スタブ）
tests/meta/test_mcs_recover.py   新設
data/update_state.json           状態ファイル（gitignore 済み領域）
data/update_in_progress.marker   quiesce 中の補助 launcher 沈黙フラグ
data/service_manifest.json       services の管理対象記録
```

## ① 検出 + 存在通知（check）

- `git ls-remote --tags origin` で tag 一覧取得（fetch しない・refs 不変）。
  tag は `^v?(\d+)\.(\d+)\.(\d+)$` で**数値パースして最大を選ぶ**
  （文字列比較だと `v1.0.10 < v1.0.2` と誤判定。非 semver tag・
  `-beta` 等の pre-release は既定で除外）— `current` は
  `git describe --tags`（現在 `v1.0.2-33-ge8c5746` → 最近祖先 v1.0.2）
  と比較。
- 新しければ `data/update_state.json` に記録:
  `{latest_tag, latest_sha, current_tag, current_sha, notes, first_seen,
    notified_at}`（承認状態は state ではなく receipt が正本 — ⑥）。
- 通知は `latest_tag` 単位で dedup — 同一 tag は1回だけ送信。
- **outbox 書込みは run.lock を NB 取得してから行う** — tick 実行中は
  通知 enqueue を skip（検出記録は済むため次回 check で再送）。
  `update_state.json` 自体の書込みは `update.lock`（run.lock 不要）。
- 通知文言: `🆕 MCS v1.0.3 公開（現在 v1.0.2）— 内容はスレッド / 適用:
  /mcs {"op":"control","phase":"preview","action":"update_apply",
  "tag":"v1.0.3","reason":"..."}` → confirm（S2 — 実際に受理される
  envelope 形式で記載）。schema_bump を含む tag では「rollback に
  DB 復元を要し適用〜復元間の記録が失われる」を明示。

## ② 内容のお知らせ

- `https://api.github.com/repos/<owner>/<repo>/releases/tags/<tag>` を
  urllib で GET（公開 repo・認証不要・日1回なら rate limit 安全圏）。
  取得失敗時は tag/SHA/差分サマリだけで通知（ノートなしでも通知は落とさない）。
- 本文 = タイトル + ノート冒頭 ~1500字（**`@`/`#` 等の Discord
  メンション構文は通知前に無害化** — リリースノート内の
  `@everyone`/`@here` がそのまま投稿されると全員に ping する）+
  **環境影響サマリ**:
  `git diff --name-status <cur>..<tag>`（fetch 後のため apply 時 or
  notes 取得時に `git fetch --tags` して参照）から以下を抽出して警告行化:
  - `mcs_setup.py`/`config` 関連の変更 → 「新しい必須設定の可能性」
  - `deployment/` 配下の変更 → 「services 再実行が必要」
  - `install.sh` の変更 → 「依存追加の可能性」
  - `hermes_plugin/` の変更 → 「gateway restart が必要」
- 全文は `mcs_update.py status` で確認可能。

## ③ 事前チェック（apply の前段ゲート — 全部通過しないと適用しない）

| チェック | 不合格時 |
|---|---|
| tracked tree が clean（`git status --porcelain -uno`） | 中止 — ローカル変更保護 |
| **保護パス非含有（ルールベース）** — 候補 `git ls-tree -r <tag>` の全パスを `git check-ignore --stdin` に通し ignored 規則合致を全拒否 + 固定集合 {`data/`, `config.json`, `.env`, `chrome-profile/`} 接頭辞一致 + casefold/NFC 正規化 + **mode 検査**（symlink 120000/gitlink 160000 は拒否・許可は 100644/100755/040000） | 中止 — ignored ファイル保護は gitignore ではなくこの検査 |
| untracked 衝突 — `ls-files --others --exclude-standard` ∩ `diff --name-only HEAD..tag` | 中止 — 衝突ファイル名を通知 |
| `git merge-base --is-ancestor HEAD <tag>`（ff-only 成立） | 中止 — 分岐を踏まない |
| tag が semver かつ current より新しい + **peeled commit SHA が check 時記録と一致** | 中止 — 付替え/降格防止 |
| **スキーマ互換**: 候補の `SCHEMA_VERSION`（`git show <tag>:mcs/core/ledger.py` から抽出）と現行 DB の `PRAGMA user_version` を比較（下表） | 条件付き中止 |
| **新コード preflight**: `git ls-tree` 列挙 + `git cat-file blob` で `mcs/` 一式を tmp に再構成（`git archive` 不使用 — `.gitattributes` 潜脱のため）し、**本番と同じ Python** で `validate_config(現行 config)` を subprocess 実行（呼出契約不一致は「preflight 不能」として区別） | 中止 — 必須キー不足は「init 実行が必要」通知 |
| `validate_config` 合格（現行コード・config のみ） | 中止 — 設定不健全 |
| 空き容量 ≥ `ledger.db` × 2 + 作業領域 | 中止 — バックアップ不能 |
| `run.lock` 取得可能（有限リトライ） | 延期 — run 中を待つ |
| hermes CLI 解決可能 | 中止 — 通知・再起動不能 |

**スキーマ互換の判定**:

| 関係 | 扱い |
|---|---|
| `new_ver == cur_ver` | 安全 — コードのみの rollback で完結 |
| `new_ver > cur_ver` | **auto は中止**（旧コードが migrated DB を拒否するため code-only rollback 不能）。notify モードでは「要 init + DB 復元を伴う rollback」を明示して承認待ち |
| `new_ver < cur_ver` | 中止 — 新コードが現行 DB を拒否する（実質起こり得ないが防御） |

※ 完全な `check`（環境 probe 含む）は事前に置かない — Keychain locked
等の環境 error は reboot 直後 = auto が走る時に発生し得るため恒常
ブロックになる。環境 error と更新起因の error は事後チェックの
ベースライン差分で区別する（⑤）。

## ④ 適用（apply）— ステージジャーナル方式

**中断は HEAD では判定不能**（混在ツリーが再現された）。進行を
`update_state.json` の `stages` 配列に逐段記録し、復旧は「最後の
完了段階 + 実ツリー整合」の両方で決める。

段階: `local_checks → remote_verify → fetch → tag_checks → backup →
lock → applying → quiesce → merge → post_merge(child: services →
restart → postcheck → applied) → gateway_restart → done`。
各段階の完了を atomic write（tmp→fsync→os.replace→dir fsync）で記録。
`applying` は quiesce **より前**に書く（S7 — 停止操作より先に中断
マーカーを永続化）。

```
0. `update_state.json` を lock-free 読み → `applying`/`stages` 残存
   なら復旧経路へ（⑤）
1. ローカル事前チェック: porcelain(-uno) clean・semver 昇順・
   現行コード `validate_config`・容量・hermes CLI 解決
2. **remote 真値照合**: `git ls-remote origin "refs/tags/<tag>"
   "refs/tags/<tag>^{}"` — peel 行があればその sha、なければ
   （lightweight tag）直行の sha を commit sha とする。
   check/承認時に記録した sha と不一致 → 中止（付替え=要再レビュー）
3. `git fetch --tags origin` — **tag reject 由来の非ゼロ終了を許容**
   し、結果は fetch 後の `rev-parse refs/tags/<tag>^{commit}` で判定
4. tag コンテンツ検査（fetch 済みオブジェクトに対して）:
   - 保護パス非含有（**存在ベースではなくルールベース**）:
     `git ls-tree -r <tag>` の全パスを `git check-ignore --stdin` に
     通し ignored 規則に合致する候補を全拒否 + 固定集合
     {`data/`・`config.json`・`.env`・`chrome-profile/`} の接頭辞一致
     + casefold/NFC 正規化（`Data/` 等の case-insensitive FS 対策）
   - mode 付き `ls-tree` でエントリ種別検査: 許可は 100644/100755/
     040000 のみ。**symlink(120000)・gitlink(160000) は原則拒否**、
     `.gitattributes`/`.gitmodules` の差分は通知対象（export-subst/
     変形で検証内容と checkout 内容が乖離し得るため）
   - untracked 衝突: `ls-files --others`（裸・ignored 含む）∩
     `diff --name-only HEAD..tag`
   - スキーマ互換: 候補 `SCHEMA_VERSION`（AST/regex 抽出）vs
     `PRAGMA user_version`
   - 新コード preflight: `git ls-tree` 列挙 + `git cat-file blob` で
     `mcs/` 一式を tmp に再構成（**`git archive` は候補側の
     `.gitattributes` で export-ignore/export-subst され潜脱可能 —
     使わない**）→ `sys.executable` で
     `python -c "sys.path.insert(0,'<tmp>/mcs'); import _mcs_path;
     import mcs_setup; ..."` を subprocess 実行（timeout 付き。
     呼出契約は版間で変わり得るため「preflight 不能」を区別して通知）
   - `install.sh` 差分検出 → 差分ありは auto 中止（新依存の可能性）
     / notify は「install.sh 再実行が必要」と警告
5. `data/backups/preupdate-<ts>.db`（検証済み backup）+
   `service_manifest.json` 現行版を state に退避
6. `update.lock` を先に取得（以降の `update_state.json` 書込みは全て
   このロック下で直列化・lock 不在が「apply 進行中」の生存信号）→
   `acquire_run_lock`（LOCK_NB・30s×40回=最大20分、tick 最大8分を
   考慮）→ **両方を終了まで保持**（順序は常に update→run。run.lock
   保持者は update.lock を取らないためデッドロックしない）。
   ※ **lock 取得直後に再検証**: porcelain clean・state 再読
     （無 lock 期間の lost update 防止）・`HEAD == 承認時 base_sha`・
     `stages` を空に初期化
7. **`applying: {tag, sha, prev_sha, plugin_changed, schema_bump,
   backup_path, command_id, at}` を記録 — quiesce より先に書く**
   （quiesce 中の crash で drainer 恒久停止しないよう、中断マーカーを
   停止操作より前に永続化。tmp→fsync→os.replace→dir fsync）
8. quiesce: 先に **`data/update_in_progress.marker` を作成**（起動時に
   marker を検査して即 exit するよう `mcs_llm_catchup.sh` 等の
   補助 launcher 全てを変更 — cron 発火で drainer を再 spawn する
   経路を塞ぐ）→ `launchctl bootout
   gui/<uid>/ai.mcs.extract-drainer{,-rt}` → `launchctl print` 失敗を
   検証 + **`pgrep -f "extract_llm.py|semantic_drain.py"` の stray
   sweep**（TERM→消失確認）。marker は restart 成功後に除去
   （rollback/abort 経路でも必ず除去 — finally 相当 + recover 経路でも
   判定後に除去）。
   ※ drainer の最終 artifact 書込みは write 単位で run.lock を取るが
     claim/checkpoint/release は lock 無しで書き続ける — run.lock は
     推論結果の破棄を防ぐだけで真の停止手段は quiesce
9. `git merge --ff-only refs/tags/<tag>` → `rev-parse HEAD == sha`
   かつ porcelain clean を検証
10. **post-merge 段階は新コードの subprocess で実行**: 親（旧コード・
    lock 保持のまま）が `subprocess.run([sys.executable,
    mcs_update.py, "--post-merge"])` を呼ぶ — 新コード側で
    services reconcile・agent 再起動・postcheck・通知 enqueue を実行。
    ※ `os.execv` は不可: PEP 446 で os.open の fd は non-inheritable
     （FD_CLOEXEC）のため exec で lock fd が閉じてしまう。親が生きて
     lock を保持し続け、子は lock を取らない `--locks-held` モードで
     動く設計とする。新版が import 不能 → 子が即死 → 親が⑤へ
11. （子内で）`mcs_setup.py services`（manifest reconcile）
12. 再起動+稼働版確認: drainer 2件 bootstrap（新コードで再起動）+
    KeepAlive 常駐のみ新 PID 確認、watcher 型は loaded 確認
13. 事後チェック（⑤の陽性確認方式 — 新コード subprocess で実行）
14. （子内で）`applied` を**履歴リスト**に追加 `{tag, sha, prev_sha,
    backup_path, schema_bump, plugin_changed, command_id, at}`
    （rollback に必要な全情報を保持）+ `executed[command_id]` 記録 +
    成功通知（outbox enqueue — schema_bump 時は新コードの Ledger が必須
    なため子で実行）
15. 子終了後、親が locks を解放し `plugin_changed` なら gateway restart
    を発行 — **`launchctl kickstart -k gui/<uid>/ai.hermes.gateway` か
    detached `hermes gateway restart` を fire-and-forget で実行し
    同期 wait しない**（cron 経由の updater は gateway の子孫 —
    gateway の graceful drain が updater 自身を待つ自己待機を避ける。
    restart 発行は `applied` 記録の**後** — restart で kill されても
    復旧判定は applied+done で完結。updater の subprocess env からは
    `_HERMES_GATEWAY`/`HERMES_SUPERVISED_CHILD` 等の marker を除去）

任意段階の失敗 → ⑤ロールバック。**段階8以降の中止/失敗経路では必ず
drainer を復帰**。run.lock 保持中は 15分 tick・cmd drain・drainer の
最終書込みが skip/遅延（veto の有効期限は apply 開始まで）。
全 git 呼出に subprocess timeout（ls-remote 15s/fetch 120s/merge 60s）
+ `GIT_HTTP_LOW_SPEED_LIMIT` を規定 — hang が update.lock を
恒久占有しないようにする。
```

## ⑤ ロールバック・中断復旧

### 復旧の判定（ジャーナル駆動 — HEAD 単独では判定しない）

トリガ: `applying` 残存 **または `stages` 非空かつ `done` 未到達**
（quiesce 中の crash は `applying` 前でも検出できるよう）。
recover/updater ともに **update.lock→run.lock をこの順で取得**してから
判定に入る（復旧中に 15分 tick が Ledger を開かないよう遮断）。

実測: `HEAD` / `status --porcelain -uno` / `.git/` 内の lock 残存
（`MERGE_HEAD`・`index.lock`・`HEAD.lock`・`refs/**/*.lock`）。

**段階0 — stale lock 除去（最初に評価）**: update.lock を NB 取得
できた = apply プロセス不在が保証される → `.git/**/*.lock` を検査し
残存 lockfile を除去してから判定へ（除去不能ならエスカレート）。

| 状態（上から順に評価） | 判定 | 動作 |
|---|---|---|
| `MERGE_HEAD` 残存 | merge 中断（`--ff-only` では通常作られない — 外部要因の防御ケース） | `git merge --abort` → `HEAD==prev_sha` 検証 → 通知。失敗/不一致はエスカレート |
| `stages` が merge 未完了 かつ `HEAD==prev_sha` かつ clean | merge 前に停止 | flag/stages 除去 + drainer 復帰 + 「中断（適用前）」通知 |
| merge 未完了だがツリーが dirty/混在 | **checkout 中断** | `reset --hard prev_sha` + 外科的削除 → `HEAD==prev` && clean 検証 → 通知 |
| `HEAD==tag sha` かつ clean | merge 済みで停止 | **最後の完了段階から追走**（services→restart→postcheck）。追走不能（新版破損）なら rollback へ |
| どれにも分類不能 | — | 何もせず人にエスカレート（repo を壊さない） |

※ `index.lock` 単独残存は段階0で除去され、除去後の状態で上表を
評価する（`merge --abort` 不可だが `reset --hard` の前に必須の処理）。

**外科的削除**（`git clean` は使わない — `-x` なしでも全 untracked を
消しユーザーの放置ファイルを巻き込む）:
`diff --name-only prev_sha tag` ∩ `ls-files --others`（`-z`・
`--full-name`）に属するファイルのみ削除。空 dir は残してよい。
apply 窓内に同名 untracked を作った稀な競合の可能性は通知に明記。

### ロールバック本体

前提: update.lock→run.lock 取得済み・drainer quiesce 済み（独立実行の
`ops.update_rollback`・recover 経路も同じ前提を先に満たす）。

1. `git reset --hard <prev_sha>` + 外科的削除 — 事前に clean を
   検証済みなので prev_sha の tracked ツリーは一意に復元される
2. **schema_bump を含む apply の rollback のみ** DB 復元をここで行う
   （drainer 再起動**より先** — 旧コードが migrated DB を開いて
   MigrationError ループにならないよう先に戻す）:
   `preupdate-*.db` を検証・復元。**復元は消失を伴う**: 適用〜復元間に
   書かれた記録（受信履歴・notify_outbox・read_marks）が失われ、
   送信済み Discord 投稿は取り消せない（再通知は server 側 read_marks
   で防がれる）。notify モードでは承認時にこの消失を明示する
3. **サービス復元** — 2層に分ける:
   - **メンバーシップ reconcile は `mcs_recover.py`（repo 外の凍結
     コード）が担当**: apply 時に退避した manifest スナップショットを
     正本として「現在あるが snapshot にない管理対象」を削除、
     「snapshot にあるが現在ないもの」を再登録。repo の旧コードに
     reconcile 機能がなくても復元できる（初回 rollback でも成立）
   - **コンテンツ再レンダリングは旧コードの `mcs_setup.py services`**
     が担当（plist/script の中身は旧版のものに戻る）
   - drainer 再起動、`plugin_changed` なら `gateway restart`
4. 結果を記録 + 通知

対象決定: `applied` 履歴の最新エントリの `prev_sha` へ戻す。
`applied` が履歴リストのため複数 apply 後も「直近 applied の
prev_sha = 1世代前」が一意に決まる。対象なしは `nothing_to_rollback`
を記録して冪等終了。

### 独立復旧プログラム（install 時配置）

復旧ツール自身が更新対象に含まれる問題への対策: **`install.sh` が
`~/.mcs-recovery/mcs_recover.py` を repo 外に配置**（stdlib+git のみ、
hermes/discord/新コード不要で起動可能）。

- **責務**: `update_state.json` を読み、⑤の判定表を実行 —
  stale `.git/*.lock` 除去 → git 復旧（reset+外科削除+検証）→
  **manifest スナップショット駆動の membership reconcile**
  （desired set ではなく snapshot 正本で cron/agent の追加・削除を
  復元 — 旧コードに reconcile がなくても成立）→ repo の旧コード
  `mcs_setup.py services` を呼ぶ（コンテンツ再レンダリング）→
  `data/recovery_report.json` 記録 + 可能なら hermes 経由で通知
- **起動経路**: `mcs_update.sh` wrapper は冒頭 `exec` のみで fallback
  を持たない（exec と fallback 検知は論理両立しない — exec は
  プロセス置換）。**検出は独立 launchd agent が担う**:
  `~/Library/LaunchAgents/org.mcs.recovery.plist`（`StartInterval` 定期・
  install.sh 所有・`ai.mcs.*`/`local.mcs-*` 所有規則に非該当の label）が
  `mcs_recover.py --if-stale` を実行。**hermes cron ではない** —
  scheduler は gateway プロセス内で動くため gateway 死亡時に
  watchdog も死ぬ。launchd agent は gateway 非依存
- **判定**: `update.lock` を NB 取得 — 取得不可なら apply 生存中 →
  何もしない。取得可 + `applying`/`stages` 残存 + 最終 stage 記録から
  閾値（例 30分）超過 → 復旧実行。repo コードの import probe が
  失敗しても git 復旧部分は単独で動く
- state スキーマが不明（将来の `"v"` 変更）なら保守的にエスカレート
- install.sh 再実行時のみ更新、前世代を `mcs_recover.py.prev` として保持
- 手動入口: `python3 ~/.mcs-recovery/mcs_recover.py`

### 成功判定（陽性確認方式 — 「新規 error なし」では不十分）

apply/rollback 後に**以下がすべて実行可能かつ合格**であること:

- `HEAD == 期待 sha` かつ tracked tree clean
- 新コードの `validate_config` 合格 + Ledger が新コードで開ける
  （`user_version` ≤ 新 `SCHEMA_VERSION`）
- desired な launchd agent がすべて loaded かつ apply 後起動の PID で
  running（drainer 2 + watcher 2）
- `interactive=discord` 時は gateway が supervised
- manifest reconcile 完了（desired 外の所有 cron/agent が残っていない）
- ベースライン差分で新規 error なし（既存 error は記録のみ）

**検証不能（launchctl 呼出失敗等）は成功と区別** — `unverifiable`
として fail-closed に rollback し、理由を区別して通知。

## ⑥ モード・設定・コマンド

```json
"update": {
  "mode": "notify"        // off    = 検出も通知もしない（killswitch）
                          // notify = 通知のみ・/mcs で承認して適用
                          // auto   = 事前チェック通過で無人適用
}
```

- `validate_config`: `update` は object、`update.mode` は
  `"off"|"notify"|"auto"`。ウィザード: 収集ポリシー節に1項目追加。

### 承認キュー — receipt 駆動（ファイル要求は廃止）

**単一 `update_request.json` 設計は廃止**。receipt 自体を承認の正本にする。

**受理経路 — 2層の変更が必須（S1/S2 で発覚した致命的欠落）**:

現行コードでは `ops.update_apply` は**全層で拒否される**:
`hermes_plugin/__init__.py` の `_dispatch` は `{"op":...}` 形式のみ受理
（`{"cmd":...}` は `bad_command`）、`_CONTROL_FIELDS`/`_confirm` の
allowlist に update ops なし、`_authorize` は `project_id` 必須、
`mcs_requests.validate()` の `positive(project_id)` ゲートが
`validate_ops` 到達前に `bad_project_id` で弾く（`ops.card_resolve`
だけが早期 return で迂回）。「project_id 不要・実装確認済み」は
カード経路の `_authorized`（別関数）の誤認だった → **撤回**。

変更が必要な層:
- `hermes_plugin/__init__.py`: `_CONTROL_FIELDS` に `update_apply`/
  `update_rollback` を登録、`_confirm` allowlist に追加、
  projectless 認可の迂回（`ops.card_resolve` 同型 — user/chat
  allowlist は維持）
- `mcs_requests.py` `validate()`: projectless ops 対象 cmd set への
  早期 return を `ops.card_resolve` 同型で追加
- `mcs_operations.py` `validate_ops`: 2コマンドのスキーマ登録
  （`tag` は semver 厳格・`reason` 必須。`human_confirmed`/`actor` は
  envelope 層で既に必須）
- 通知文言は実際に受理される形式に: `{"op":"control","phase":"preview",
  "action":"update_apply","tag":"v1.0.3","reason":"..."}` → confirm

**承認の確定と実行**:
- handler（`apply_tx` 内）は `ls-remote` で tag→peeled sha を解決
  （timeout 15s・失敗は `rejected: tag_unresolvable`）し
  `{tag, target_sha, base_sha, scheduled: true, cmd}` を receipt_json
  に記録 — **`scheduled` は outcome 値ではなく receipt_json 内
  フィールド**（outcome CHECK は `applied|rejected` のみ）
- **detached spawn は receipt commit の後** — `apply_tx` は tx 内の
  ため spawn せず `extra` を返し、`drain_commands`/`apply_command` の
  commit 後に `mcs_update.sh` を `Popen(close_fds=True,
  start_new_session=True)` で起動（**`os.system`/nohup/`&` 禁止** —
  lock fd 継承で run.lock が永久保持される）。spawn された updater は
  引数を信じず receipt scan で承認を再検証してから apply する
  （commit 失敗時の spawn は receipt 不在で no-op に自然収束）
- updater の走査: `mode=ro` sqlite で `command_receipts` を読み
  `json_extract(receipt_json,'$.cmd')='ops.update_apply'` かつ
  `outcome='applied'` かつ `executed` 未記録のものを対象とする
  （`cmd` 列は存在しない — receipt_json 内フィールドを使う）
- **`executed` は試行終了時に記録**（成否不問 — 成功のみ記録だと
  失敗 apply が永久に「未実行」で F3 の抑止が崩れる）。
  結果詳細は `attempts` に記録
- **veto**: 後続の `ops.update_rollback` receipt が pending
  `update_apply` を取消（cancelled として `executed` に記録）。
  適用済みの undo は「直近 applied の prev_sha へ」、対象なしは
  `nothing_to_rollback` で冪等終了
- **複数 pending**: 1起動で最新 tag の1件のみ実行（他は superseded
  として executed 記録）— 複数 apply の逐次実行はしない
- **`update.mode` の遷移規則**: mode は各 entry point の開始時に評価。
  `off` 中は pending 承認の実行も抑止（receipt は残る）。実行中の
  apply は mode 変更では中断しない（中断の方が危険）。
  `auto_delay_h` の起算点は `first_seen`
- **冪等性の保証**: apply は `HEAD==target_sha` で早期 return
  （lock 取得直後に判定 — backup/quiesce より前）。
  rollback は冪等ではないため executed 消失時の二重実行を
  「対象 applied が存在するか」で防ぐ

**状態ファイルスキーマ（`update_state.json`・`"v":1`）**:
`{v, latest_tag, latest_sha, first_seen, notified_at,
applying: {tag, sha, prev_sha, plugin_changed, schema_bump,
backup_path, command_id, at} | null, stages: [...],
applied: [{tag, sha, prev_sha, backup_path, schema_bump,
plugin_changed, command_id, at}], attempts: {tag: {...}},
executed: {command_id: {result, at}}, manifest_snapshot: {...},
baseline_check: [...]}` — 新 apply 開始時に `stages` は必ず
空に初期化する（前回 attempt の残滓で誤判定しない）。

### サービス管理の共通化（install↔update↔rollback）

`services` が「書き換えたもの」を記録する **managed manifest** を導入:

- `data/service_manifest.json`: `{scripts:{name:sha256},
  agents:{label:plist_sha256}, cron:{script:sched}}` を services が
  毎回 atomic 書込み（tmp→fsync→os.replace）。apply 前にスナップショットを
  state に退避
- **所有判定は manifest 駆動**（ラベル prefix だけに依存しない —
  `ai.mcs.*` のような広い規則は install.sh 所有の
  `ai.mcs.llamaserver` まで呑み込むため**禁止**）:
  管理対象 = manifest 記録済みエントリ ∪ コード内 desired set
  （CRON_JOBS/AGENT_LABELS）に含まれる名前。**明示除外リスト**
  `{ai.mcs.llamaserver, org.mcs.recovery}` は常に非所有。
  agent の所有はさらに `ai.mcs.extract-*`/`local.mcs-*` の範囲に限定
- services の強化: loaded plist が desired と不一致なら hint 表示でなく
  bootout+bootstrap で収束、cron は sched 差分も照合し `cron edit` で
  収束。script/plist の書込みは tmp+os.replace で atomic にする
  （現行の逐次 write は中断で半分ファイルを残す）。`hermes cron list`
  は `--json` がないため tolerant parse とし、パース不能な出力は
  `unverifiable`（R21）
- rollback: メンバーシップ復元は recover.py が snapshot 正本で実行
  （⑤参照）— 旧コードに reconcile 実装がなくても初回 rollback で成立

### install↔update↔rollback 共通仕様

| 項目 | 揃える内容 |
|---|---|
| 配置先・Python | `mcs_setup.py` の HERMES_PY 定数を唯一の正本とし、install.sh・wrapper 生成・復旧プログラムがすべてそれを使う |
| サービス管理 | install/services/update/rollback が同一の managed manifest + 所有名前空間規則を使い、配置内容・登録内容・稼働状態を照合 |
| plugin・LLM | **install.sh 側の所有物**（plugin symlink・llamaserver plist・モデル GGUF）と services 側の所有物（rendered scripts・4 launchd・cron・gateway）を分離。services は自分の範囲のみ reconcile。plugin コードは symlink 追従のため gateway restart で十分 |
| 必須条件 | Python バージョン・Hermes API 到達・config 妥当性・DB スキーマ互換を共通 preflight とする。範囲外の依存更新（hermes-agent pin 等）が必要なら適用前に中止 |
| 復旧手段 | 初期導入時から `~/.mcs-recovery/` に独立復旧経路を配置し、更新成功後も前世代と復旧情報（`applying`・manifest スナップショット）を保持 |

## サプライチェーン上の注意（auto モード）

`auto` は「repo に push/tag できる者 = このマシンで MCS 資格情報を持つ
コードを実行できる者」と等価。GitHub アカウント侵害時の影響を抑える
選択肢として、auto でも**検出通知→次回 check で apply**（=24h 程度の
veto 猶予）にする方式を検討する。既定値の決定と併せて判断する。
（ff-only の祖先検証は history 改ざんへの防御であり、push 権限自体への
防御ではない点に注意。）

## ⑦ 通知まとめ

| イベント | 経路 | 頻度 |
|---|---|---|
| 新 tag 検出 | notify_system_target → Discord | tag ごとに1回 |
| apply 成功/失敗/ロールバック | 同上 | 都度 |
| check/apply の異常（API 不可等） | run.log + health.json | 静かに記録（毎回通知しない） |

## ⑧ テスト方針（絶対ルール準拠）

- 実 GitHub/実 hermes/実 MCS には触れない。`git init` した一時 repo に
  tag を打った fixture + `subprocess`/`urllib` を monkeypatch。
- ケース: 新 tag 検出→通知 dedup、ノート取得失敗時のフォールバック、
  dirty tree で apply 中止、ff-only 不可で中止、apply 成功→state 記録、
  事後チェック失敗→reset --hard 復旧、receipt 冪等（同一 command_id 再送）。
- **障害注入テストが必須**（レビューで実際に再現されたクラスを網羅）:
  - 各段階（local_checks/remote_verify/fetch/tag_checks/backup/lock/
    applying/quiesce/merge/post_merge/gateway_restart）での強制終了 →
    ジャーナル判定が正しい復旧経路を選ぶ。**特に quiesce 直後・
    `applying` 記録直後の kill**（drainer 恒久停止しないこと）
  - checkout 中断の混在ツリー（HEAD=旧・一部新・MERGE_HEAD なし）→
    reset+外科削除で clean 復元
  - stale `.git/index.lock` 残存 → 段階0で除去され判定が進む
  - 候補ツリーが ignored パス（data/・config.json）を追跡 → precheck 中止
  - 候補に symlink/gitlink・`.gitattributes` export-ignore/export-subst
    で preflight ツリーと実ツリーが乖離 → 中止（archive 潜脱の再現）
  - annotated tag → peeled SHA 照合 / lightweight tag → peel 行欠落の
    fallback / remote 付替え → 中止
  - receipt commit 失敗 → 承認が存在しないため apply されない。
    commit 前 spawn の再現 → receipt scan で no-op
  - `ops.update_apply` が plugin `_dispatch`/`_CONTROL_FIELDS`/
    `_confirm`/projectless 認可・`mcs_requests.validate()` の各層で
    受理されること（回帰防止）
  - quiesce 中の catchup 発火 → marker で drainer 再 spawn しない
  - gateway 起源の apply で restart が自己待機しない（fire-and-forget）
  - drainer 停止失敗/再起動失敗 → fail-closed + drainer 復帰試行
  - 容量不足・launchctl 不可 → `unverifiable` として扱われる
  - schema_bump タグ → auto で中止・notify で消失明示の承認待ち。
    schema_bump apply の rollback は DB 復元が drainer 再起動より先
  - `update_state.json` 破損 → `unknown` としてエスカレート（無操作）

## フェーズ

| Phase | 内容 | リスク |
|---|---|---|
| P1 | `check`+通知+status（②③・読取専用）+ 復旧プログラム配置 + manifest 記録開始 | ゼロ |
| P2 | `apply`+事前/事後チェック+`rollback`+`ops.*` コマンド（①④⑤⑥）+ 障害注入テスト | 中 |
| P3 | `update.mode` 運用決め・auto 有効化判断 | — |

## 実装計画

### ファイル単位の変更一覧

**新規:**

| ファイル | 役割 |
|---|---|
| `mcs/ops/mcs_update.py` | updater 本体。`status`/`check`/`apply`/`rollback`/`recover` サブコマンド。stdlib のみ |
| `deployment/scripts/mcs_update.sh` | wrapper。`exec "$PY" "$REPO/mcs/ops/mcs_update.py" "$@"` のみ（F17 — fallback は exec と論理両立しないため持たない。検出は独立 watchdog に委譲） |
| `deployment/recovery/mcs_recover.py` | repo 外配置の凍結復旧プログラム（stdlib+git のみ）。⑤判定表の実行・stale `.git/*.lock` 除去・manifest スナップショット駆動のメンバーシップ reconcile・quiesce。install.sh が `~/.mcs-recovery/` にコピー |
| `deployment/launchagents/org.mcs.recovery.plist` | 独立 watchdog（`StartInterval`・`mcs_recover.py --if-stale` を実行）。**hermes cron ではない** — gateway 内の scheduler は gateway 死亡で止まる + `hermes cron` は `~/.hermes/scripts/` 外の script を指せない制約があるため launchd agent とする（install.sh 所有・所有規則非該当 label） |
| `tests/ops/test_mcs_update.py` | updater テスト（一時 git repo fixture + subprocess/urllib stub） |
| `tests/meta/test_mcs_recover.py` | 復旧プログラムのテスト |

**変更:**

| ファイル | 変更内容 |
|---|---|
| `mcs/ops/mcs_setup.py` | `CRON_JOBS` に `("MCS update check", "10 5 * * *", "mcs_update.sh")` 追加。`AGENT_LABELS` を `RESIDENT_LABELS`（drainer 2・quiesce/再起動対象）と `WATCHER_LABELS`（local.mcs-*・loaded 確認のみ）に用途分割。`services` に manifest 記録（`data/service_manifest.json`: `{scripts:{name:sha256}, agents:{label:plist_sha256}, cron:{script:sched}}`）+ reconcile（plist 不一致→bootout+bootstrap・cron sched 差分→`cron edit`・所有規則で desired 外を `cron rm`/`bootout`+削除・`cron list` は tolerant parse）。`validate_config` に `update` ブロック（`mode:off\|notify\|auto`・`auto_delay_h` 非負 int・`include_prerelease` bool）。wizard に `update.mode` 1項目 |
| `mcs/ops/mcs_operations.py` | `validate_ops` に `ops.update_apply`（`tag` 任意・semver 厳格検証・`human_confirmed`/`actor`/`reason` 必須）と `ops.update_rollback` を登録。`apply_tx` に分岐追加: handler は `ls-remote` で tag→peeled sha 解決（timeout 15s・失敗は `rejected: tag_unresolvable`）→ receipt_json に `{cmd,tag,target_sha,base_sha,scheduled:true}` を固定して返却。**spawn は handler 内で行わない** — receipt commit 後に `drain_commands`/`apply_command` 層が `Popen(close_fds=True,start_new_session=True)` で `mcs_update.sh` を起動（commit 失敗時の spawn は receipt 不在で no-op に収束）。`mcs_update.sh` 未配置は `rejected: updater_not_deployed` |
| `mcs/ops/mcs_requests.py` | `validate()`: `ops.update_apply`/`ops.update_rollback` を projectless ops 対象として `ops.card_resolve` 同型の早期 return 経路に追加（`positive(project_id)` ゲートが validate_ops 到達前に弾くため必須 — S1） |
| `hermes_plugin/__init__.py` | `/mcs` の受理経路: `_CONTROL_FIELDS` に `update_apply`/`update_rollback` 登録、`_confirm` allowlist 追加、projectless 認可の迂回（card_resolve 同型・user/chat allowlist 維持）— 現在の `{"cmd":...}` は `_dispatch` で `bad_command`（S2） |
| `mcs/ingest/notifier.py` | `update_notice` kind を `_format_event`（payload の凍結テキストをそのまま出力）と `_target`（system グループ → `notify_system_target`）に追加。sanitize ヘルパー（`@everyone`/`@here`/`<@id>`/`<@&id>`/`<#id>` の無害化） |
| `mcs/core/maintenance.py` | `preupdate_backup(ledger_path, ts)` — `daily_backup` と同じ `.backup`→`valid_mcs_db`→atomic rename で `data/backups/preupdate-<ts>.db`。`prune_preupdate_backups(referenced_paths)` — **参照ベース保持**（state が参照する backup は残し、未参照のみ期限 prune） |
| `install.sh` | 6/6 段追加: `deployment/recovery/mcs_recover.py` → `~/.mcs-recovery/mcs_recover.py` コピー（既存は `.prev` に保持）+ `org.mcs.recovery.plist` を `~/Library/LaunchAgents/` に配置し bootstrap（install 所有・gateway 非依存の watchdog） |
| `deployment/scripts/mcs_llm_catchup.sh`（及び drainer を spawn し得る補助 launcher 全般） | 起動時に `data/update_in_progress.marker` を検査して即 exit — quiesce 中の cron 発火による drainer 再 spawn を防止（S8） |
| `README.md`/`AGENTS.md`/`deployment/launchagents/README.md` | updater 経路・復旧経路・`update` 設定の記述 + `update_readme.py` でマーカー再生成 |

### mcs_update.py の内部構成

```
state 層    load_state()          # lock-free read・破損は "unknown" 扱い
            save_state()          # update.lock 内で tmp→fsync→os.replace→dir fsync
            journal(stage)        # stages[] への逐段記録
検出層      detect_latest()       # ls-remote "refs/tags/*" + peel 行 → semver 最大値
            current_version()     # git describe --tags / merge-base fallback
            fetch_notes(tag)      # GitHub API urllib + sanitize
            impact_summary(tag)   # diff --name-status → 設定/plist/install.sh/plugin 影響行
ゲート層    precheck_local()      # porcelain(-uno)・semver・現行 validate_config・容量・hermes
            precheck_tag(tag,sha) # ls-tree 列挙+check-ignore 規則ベース保護パス・
                                  #   mode 検査(symlink/gitlink 拒否)・casefold/NFC・
                                  #   untracked∩diff・SCHEMA_VERSION 抽出・
                                  #   ls-tree+cat-file blob で tmp 再構成 preflight・
                                  #   install.sh 差分（archive 不使用 — .gitattributes 潜脱）
            verify_remote_sha()   # apply 時の ls-remote 再照合（lightweight fallback 含む）
            postcheck(baseline)   # 陽性確認一式 + ベースライン差分（新コード subprocess）
実行層      quiesce()             # RESIDENT_LABELS bootout + print 失敗検証 +
                                  #   pgrep stray sweep（catchup spawn の非 launchd 経路）
            restart_agents()      # bootstrap + PID/loaded 確認
            restart_gateway()     # fire-and-forget（kickstart -k / detached restart・
                                  #   env marker 除去・self-wait 回避）
            merge_and_verify()    # merge --ff-only + HEAD==sha + clean
            apply(tag, sha, cid)  # 15 段パイプライン（lock→journal→quiesce→merge→
                                  #   新コード subprocess post-merge）
            rollback()            # reset+外科削除+DB復元(schema_bump)+snapshot
                                  #   reconcile+restart — run→update lock + quiesce 前提
            recover_interrupted() # 判定表（⑤・stale lock 除去込み）
承認層      scan_pending_approvals()  # mode=ro sqlite・receipt_json の
                                      # json_extract('$.cmd') で走査・executed 管理・
                                      # veto（後続 rollback receipt で cancel）・
                                      # 複数 pending は最新 tag のみ
```

### P1 タスク分解（読取専用 + 下地）

| # | タスク | 対応指摘 |
|---|---|---|
| P1-1 | `mcs_update.py` skeleton: state 層（lock-free read・fsync write・`"v":1`）・`status` | F7/R13/R17 |
| P1-2 | `detect_latest()`: ls-remote peel 解析（annotated 対応）・semver tuple 比較・pre-release 除外 | B2/F15 |
| P1-3 | `current_version()`・dedup（`first_seen`/`notified`）・`check` サブコマンド | ② |
| P1-4 | `fetch_notes()` + sanitize + `impact_summary()`（check 時に fetch して差分分析 — refs/objects 取得のみで worktree 不変） | ③F4 |
| P1-5 | notifier に `update_notice` kind + sanitize helper | F4 |
| P1-6 | mcs_setup: `update` ブロック検証 + wizard 項目 + `CRON_JOBS` 追加 + `mcs_update.sh` テンプレート | F10/F17 |
| P1-7 | `services` の manifest 記録開始（reconcile は P2、記録だけ先行 — apply 前のスナップショットが必要なため） | R6 |
| P1-8 | `mcs_recover.py`（判定表・stale lock 除去・snapshot reconcile・quiesce）+ install.sh 6/6 段 + `org.mcs.recovery.plist` watchdog（launchd・非 cron） | R5/R19/S6 |
| P1-9 | P1 テスト: ls-remote parse（annotated/lightweight）・semver 比較・dedup・sanitize・config 検証・manifest 記録・recover 配置 | — |

### P2 タスク分解（適用・復旧・承認）

| # | タスク | 対応指摘 |
|---|---|---|
| P2-1 | ゲート層の各関数を個別実装（④段階4 の全項目 — check-ignore 規則・mode 検査・casefold/NFC・ls-tree+cat-file preflight） | R3/R10/B1/S3 |
| P2-2 | lock 取得（run→update 順・有限リトライ 30s×40）+ quiesce（bootout+print 検証+**pgrep stray sweep**）+ restart_agents（稼働版検証） | R4/R9/R11/F13/S8 |
| P2-3 | journal + `apply()` パイプライン + `merge_and_verify` + **post-merge 新コード subprocess**（親が lock 保持・子は `--locks-held`） | R1/R12/F18/S4 |
| P2-4 | services reconcile（manifest 差分 + manifest 駆動所有判定 + llamaserver 除外 + `cron edit`/`cron rm` + atomic render） | R6/R21/S5 |
| P2-5 | `postcheck()` 陽性確認 + `unverifiable` 区別（新コード subprocess） | 成功判定 |
| P2-6 | `rollback()`（reset+外科削除→schema_bump 時 DB 復元→snapshot reconcile→drainer/gateway 復帰）+ `recover_interrupted()` 判定表（stale lock 除去込み） | R1/R2/F8/S7 |
| P2-7 | **plugin 受理経路**（`__init__.py` 3層 + `mcs_requests.py` projectless 例外）+ `scan_pending_approvals()`（json_extract・executed=試行終了・veto・複数 pending）+ ops handler 2件（commit 後 spawn） | R7/R8/R15/R16/S1/S2/S9 |
| P2-8 | `maintenance.preupdate_backup` + 参照ベース prune | F9/B3 |
| P2-9 | health.json に `update` 状態を表面化（applying/apply_failed/latest） | 可観測性 |
| P2-10 | **障害注入テスト一式**（⑧の網羅リスト + quiesce→journal 間 kill・catchup respawn・gateway 起源 restart・`.gitattributes` preflight 潜脱・stale `.git/*.lock`・lightweight tag peel 欠落） | 全般 |
| P2-11 | 手動ロールアウト: このマシンで `./install.sh` + `services` を実行し updater/watchdog を初配置（制約B — ops 受理はこれ以降） | ブートストラップ |

### P3 タスク分解（運用）

- `update.mode` 既定値の決定（`notify` 推奨・`auto` は猶予付きで opt-in）
- auto 有効化の判断・`auto_delay_h` の運用値
- `update_notice` の文言・頻度の運用確認

### 依存関係と順序の制約

- **manifest 記録（P1-7）と recovery 配置（P1-8）は P2 より先に必須** —
  最初の apply が rollback 点と復旧経路を必要とするため
- **ops コマンド（P2-7）は updater デプロイ後にのみ有効**（制約B）—
  ただしコードは同時にリリースしてよい（未配置時は rejected で graceful）
- **P2-11 の手動ロールアウトが updater の実稼働起点** — それ以前の
  `check` は state を書くだけ（`mcs_update.sh` 未配置なら cron も未登録で
  安全）
- **`update.mode` が未設定（キー不在）の既定は `off`** — 明示的な
  opt-in を必須にする（kill switch が既定挙動）

## 残オープン

- `update.mode` の既定値 + auto 時の veto 猶予（即時 or 次回 check）—
  実装完了後に決定
- `ops.update_apply` の Discord 受理は updater デプロイ後から有効（制約B）
- `hermes-agent` pin 更新を install.sh 変更として扱うか、別仕組みにするか
- tag の署名検証は行わない（現リリース未署名）。ff-only の祖先検証で
  十分とする判断は実装時に再確認
- cron 時刻: `llamacpp daily restart`(04:00)・`mcs-llm-catchup`(22:30) と
  被らない早朝帯（例: 05:10）
- pre-release tag（`-beta` 等）の扱い: 既定では検出対象外とし、
  必要なら `update.include_prerelease` で opt-in

## 敵対的レビュー指摘と対応（2026-09-24）

コード照合で発見した問題と、計画への反映済み対応:

| # | 指摘 | 深刻度 | 対応 |
|---|---|---|---|
| F1 | `git status --porcelain` は untracked を含む → 放置ファイルが更新を永続ブロック | 中 | `-uno` に限定、untracked 衝突は merge abort で検出（③） |
| F2 | 事前ゲートに完全 `check` を置くと Keychain locked 等の環境 error で恒常ブロック（reboot 直後が典型） | 高 | 事前は `validate_config` のみ、事後はベースライン差分方式（③・対応策） |
| F3 | 失敗 apply が日次 cron で毎朝再試行+通知 = スパム | 高 | `apply_failed` を tag 単位で記録・自動再試行禁止（⑤） |
| F4 | リリースノート経由の Discord メンション注入（`@everyone`） | 中 | 通知前にメンション構文を無害化（②） |
| F5 | `tag` が git 引数注入の入口になり得る | 高 | `^v?\d+\.\d+\.\d+$` 検証 + `refs/tags/` 参照（④⑥） |
| F6 | rollback 時の `gateway restart` 漏れ（plugin 差分が戻った場合） | 中 | rollback 手順に追加（⑤） |
| F7 | `update_state.json` への check/apply/ops 並行アクセス | 中 | 専用 `update.lock` + atomic write（⑥） |
| F8 | apply 中断時の状態不明瞭 | 中 | HEAD 3態判定 → **R1 でジャーナル駆動に拡張**（⑤判定表） |
| F9 | `ledger-pre-update-*.db` が日次ローテーションの glob に呑まれる | 低 | 別 prefix `preupdate-*`（④） |
| F10 | killswitch 不在 | 中 | `update.mode:"off"` 追加（⑥） |
| F11 | auto = push 権限者のコードを無人実行（サプライチェーン） | 高 | 脅威を明記 + 検出→次回 apply の猶予案を提示（⑥） |
| F12 | 必須 config 追加リリースは auto 適用不能（post-check 必敗） | 中 | 設計上正しい挙動として明記 + `init` 案内通知（⑤） |
| F13 | `acquire_run_lock` は LOCK_NB → 「待つ」は自前実装が必要 | 中 | 有限リトライ方式を明記（④） |
| F14 | ops ハンドラは `mcs_update.sh` 未配置を受理し得る | 低 | 未配置時 `rejected: updater_not_deployed`（⑥） |
| F15 | semver 文字列比較は `v1.0.10 < v1.0.2` と誤判定 | 中 | 数値パースで比較・非 semver tag は除外（①） |
| F16 | merge 中の launchd 発火で混在バージョン import crash | 低→重大 | ~~許容~~ → **R4 で quiesce+稼働版確認に変更**（④段階5/9） |
| F17 | `services` が実行中の `mcs_update.sh` を再レンダリング → bash 逐次読みで自己破壊 | 中 | wrapper は `exec` を冒頭に置き本体は Python 側（対応策） |
| F18 | fetch 失敗後に `applying` が残ると中断と誤判定 | 中 | `applying` 記録を fetch+SHA照合の後に固定（④） |
| F19 | 成功通知が新コードの障害で失敗 → apply を誤ロールバックし得る | 中 | 通知失敗は apply 成否と分離・記録のみ（④） |

## 第2回レビュー指摘と対応（2026-09-25・再現検証済み）

| # | 指摘 | 深刻度 | 対応 |
|---|---|---|---|
| R1 | HEAD==prev_sha は「無変更」を意味しない — checkout 中断で MERGE_HEAD なしの混在ツリーが再現 | 重大 | ⑤ジャーナル駆動判定（stages+HEAD+porcelain+MERGE_HEAD）+ 外科的削除。merge 原子性の記述撤回 |
| R2 | `user_version > SCHEMA_VERSION` を旧コードが拒否（ledger.py:104）— additive でも rollback 不能 | 重大 | ③スキーマ互換判定: `new>cur` は auto 中止・notify は DB 復元の消失を明示。DB 復元セマンティクスを⑤に明記 |
| R3 | `.gitignore` は保護にならない — 候補が ignored パスを追跡すると merge が設定を上書き、rollback で消失 | 重大 | ③保護パス非含有チェック（`ls-tree` ∩ 保護集合）を必須化 |
| R4 | run.lock は常駐 drainer を隔離しない + lock 前に DB を開く経路がある | 重大 | ④quiesce（bootout+停止検証）・再起動+稼働版確認。schema_bump 更新は auto 対象外で lock 外 migration を封じる |
| R5 | 復旧ツールが更新対象に含まれる — 新版が壊れると復旧まで死ぬ | 重大 | `~/.mcs-recovery/mcs_recover.py` を install 時に repo 外配置（stdlib+git のみ）。検出は独立 launchd watchdog（S6 — wrapper fallback は S4 で撤回）+ 手動入口 |
| R6 | services 再実行は収束しない（loaded skip・名前一致 skip・新版追加ジョブ残留） | 重大 | managed manifest + 所有名前空間規則（⑥）。配置・登録・稼働状態を照合し desired 外を削除 |
| R7 | 単一 `update_request.json` は上書き消失 + receipt commit 前に実行され得る | 重大 | receipt 駆動承認キューに変更（receipt commit = 承認確定）。`command_id` 単位の実行管理・SHA ピン |
| R8 | `ops.update_apply` は現 plugin の allowlist/フィールド検証で拒否される。全体操作の権限設計が不足 | 重大 | `human_confirmed`/`actor`/`reason` 必須 + `validate_ops` 登録（⑥）— **S1/S2 で受理経路の全層閉塞が再発見され、plugin/mcs_requests の変更を追加** |
| B1 | 単一ファイル preflight は `_mcs_path` 不在で起動不能（再現済み） | 中 | `mcs/` 一式を tmp に再構成（ls-tree+cat-file — S3 で archive は撤回）+ 本番同一 Python で実行 |
| B2 | annotated tag のオブジェクト SHA と commit SHA の混同 | 中 | `^{commit}` peeled SHA で統一（検出・照合・HEAD 比較） |
| B3 | 「最新3件」保持は fetch 失敗の繰返しで復旧点を押し出す | 中 | 参照ベース保持（actionable な rollback 点の backup は消さない） |

## 第3回レビュー指摘と対応（2026-09-25・修正設計への再検証）

| # | 指摘 | 深刻度 | 対応 |
|---|---|---|---|
| R9 | 旧ロック設計は ABBA デッドロック（handler: run→update ↔ apply: update→run） | 高 | receipt 駆動化で handler が update.lock を不要に（lock-free read）+ 両 lock 保持時は常に run→update（F7 修正） |
| R10 | precheck が fetch より前にあるのに tag コンテンツ検査（ls-tree/schema/preflight）を含む — 実行不能な順序 | 高 | ④の段階を再整列（S7 でさらに `applying` を quiesce 前に移動）: local → remote_verify → fetch → tag_checks → backup → lock → applying → quiesce → merge |
| R11 | lock 取得までの無 lock 期間に working tree が変わり得る（precheck〜lock の TOCTOU） | 中 | lock 取得直後に porcelain clean・state・base_sha を再検証（④段階6） |
| R12 | `git fetch` は移動した tag を更新しない → local rev-parse 照合では remote 付替えを見逃す | 中 | apply 時の照合は `ls-remote`（remote 真値）で行い、不一致は中止+再レビュー通知。local ref sha も一致確認（④段階2） |
| R13 | `os.replace` だけではクラッシュで rename が失われ得る（dir fsync 不在） | 低 | journal 書込みを tmp→fsync→os.replace→dir fsync に規定（④段階7） |
| R14 | preflight で候補コードを実行すること自体が信頼露出（候補が悪意あるコードを含み得る） | 中 | auto の遅延適用（検出→翌日）が人のレビュー猶予として機能。subprocess+timeout で隔離。inherent リスクとして明記（④段階4） |
| R15 | ops handler の tag→sha 解決は drain 内でネットワーク要求（run.lock 保持中） | 低 | ls-remote に短 timeout。失敗は receipt `rejected: tag_unresolvable` |
| R16 | updater の receipt 走査で `Ledger()` を開くと migration/init 副作用があり得る | 中 | receipt 走査は `mode=ro` の生 sqlite3 で `command_receipts` を直接読む（Ledger init を通さない） |
| R17 | `update_state.json` 破損時の扱いが未定義 | 中 | 読めない state = `unknown` → 何もせずエスカレート。`"v"` フィールドで将来のスキーマ変更を識別（F7 修正） |
| R18 | apply 長期化中に 15分 tick/drainer 書込みが skip される | 低 | 仕様として明記 — drainer は write 単位 lock で自然に待機・再駆動（④段階8 注記）。S8 で「lock 無しの claim/checkpoint 書込み経路あり」に修正 |
| R19 | recovery watchdog が services reconcile の所有規則で消され得る | 中 | watchdog は launchd agent `org.mcs.recovery`（install.sh 所有・明示除外リスト入り）に変更（S6 — hermes cron はスクリプト配置先制約+gateway 依存で不適） |
| R20 | 成功判定の「agent running」は WatchPaths 型に誤適用（非 resident は PID なし） | 低 | KeepAlive 常駐のみ PID 確認、watcher 型は loaded 確認（④段階12） |
| R21 | `hermes cron list` に `--json` がない → reconcile のパースが脆い | 中 | 出力を tolerant parse・unparseable は `unverifiable`。sched 差分は `cron edit` で収束（⑥ manifest 節に明記） |

~~検証済みで問題なし: merge の原子性~~ — **撤回**: HEAD/refs の原子性は
あるが checkout 中断の混在ツリーが再現された（R1）。`data/` の
`reset --hard` 耐性は「候補ツリーが data/ 配下を追跡しない」前提でのみ
成立（R3 — 保護パス検査が前提条件になった）。command_id 冪等と
ops allowlist ゲート（`allowed_user_ids`/`allowed_chat_ids`）は検証済み。

## 第4回レビュー指摘と対応（2026-09-25・独立サブエージェント4系統）

git/FS・プロセス並行性・設計論理・実装実現性の4系統で計80件超の生指摘。
重複を統合した一覧（3系統が独立に発見した欠陥を含む）:

| # | 指摘 | 深刻度 | 対応 |
|---|---|---|---|
| S1 | `mcs_requests.validate()` の `positive(project_id)` ゲートが `validate_ops` 到達前に projectless ops を `bad_project_id` で弾く — 「project_id 不要」はカード経路 `_authorized`（別関数）の誤認 | 重大 | `ops.update_apply`/`update_rollback` を card_resolve 同型の早期 return 経路に追加（mcs_requests.py を変更一覧に追加） |
| S2 | `/mcs` 受理経路が全層で閉塞: `_dispatch` は `{"op":...}` のみ受理（`{"cmd":...}` = `bad_command`）、`_CONTROL_FIELDS`/`_confirm` allowlist 不在、`_authorize` は project_id 必須 | 重大 | `hermes_plugin/__init__.py` の3層変更を計画に追加。通知文言は `{"op":"control","phase":"preview","action":"update_apply",...}` 形式に修正 |
| S3 | `git archive <tag> mcs` preflight は候補側 `.gitattributes`（export-ignore/export-subst）で潜脱可能 — 検証ツリーと実 checkout が乖離し得る | 高 | `git ls-tree` 列挙 + `git cat-file blob` で raw blob 再構成に変更。`.gitattributes`/`.gitmodules` の差分は通知対象 |
| S4 | post-merge を旧コードで実行すると schema_bump 後の Ledger 開放・通知 enqueue が必ず失敗。`os.execv` による自己 re-exec は PEP 446 の non-inheritable fd（FD_CLOEXEC）で lock を失う | 高 | 親プロセスが lock を保持し続け、新コードの `--post-merge` サブプロセス（`--locks-held` モード）が post-merge 段階を実行 |
| S5 | 所有規則 `ai.mcs.*` は install.sh 所有の `ai.mcs.llamaserver` を呑み込む — reconcile が LLM サーバーを削除し得る | 高 | 所有判定を manifest 駆動に変更 + 明示除外リスト `{ai.mcs.llamaserver, org.mcs.recovery}` + agent 所有は `ai.mcs.extract-*`/`local.mcs-*` に限定 |
| S6 | watchdog を hermes cron に置けない — `hermes cron --script` は `~/.hermes/scripts/` 内しか指せず、gateway 内 scheduler は gateway 死亡で止まる（最も watchdog が必要な局面で死亡） | 高 | 独立 launchd agent `org.mcs.recovery`（StartInterval・install.sh 所有・gateway 非依存）に変更 |
| S7 | quiesce→`applying` 記録の窓で crash すると drainer が恒久停止したまま（`applying` 不在で復旧トリガが発火しない） | 高 | `applying` 記録を quiesce より前・lock 取得直後に移動。復旧トリガも「`applying` または `stages` 非空」に拡張 |
| S8 | `mcs_llm_catchup.sh` が quiesce 中に cron 発火して drainer を再 spawn し得る + drainer の claim/checkpoint/release は run.lock 無しで書き続ける（「write は自然に塞がれる」は最終 artifact のみ正しい） | 高 | quiesce 冒頭に `data/update_in_progress.marker` 作成 + 全補助 launcher が marker で即 exit + `pgrep` stray sweep |
| S9 | handler が `apply_tx`（tx 内）で spawn すると receipt commit 前に実行され得る + `os.system`/nohup/`&` は lock fd を子に継承し永久保持され得る | 高 | spawn は commit 後の drain 層で `Popen(close_fds=True, start_new_session=True)`。spawn 側は receipt scan で承認を再検証（引数を信じない） |
| S10 | `.git/index.lock`・`HEAD.lock`・`refs/**/*.lock` の stale 残存が復旧判定表にない — `index.lock` 残存は reset すら拒否する | 中 | 復旧手順の段階0に stale lock 除去を追加（update.lock NB 取得成功 = apply 不在の保証の下で実施） |
| S11 | rollback の順序未定義 — drainer 先に再起動すると旧コードが migrated DB を開いて MigrationError ループ。rollback/recover の lock 取得も未定義 | 中 | rollback 手順を reset→（schema_bump 時）DB 復元→サービス復元→再起動の順に固定。rollback/recover も run→update lock + quiesce を前提化 |
| S12 | gateway restart を同期呼出しすると自己待機でデッドロック — cron 経由 updater は gateway の子孫で、graceful drain が updater 自身を待つ | 高 | fire-and-forget（`launchctl kickstart -k` or detached `hermes gateway restart`）・`applied` 記録後に発行・env marker（`_HERMES_GATEWAY` 等）除去 |
| S13 | `applied` 記録に `prev_sha`/`backup_path`/`schema_bump`/`plugin_changed` がないと成功後 rollback が不能 | 高 | `applied` を履歴リスト化し全 rollback 情報を保持（state スキーマに規定） |
| S14 | `command_receipts` に `cmd` 列は存在しない + `scheduled` は outcome CHECK（`applied|rejected` のみ）に違反 | 中 | 走査は `json_extract(receipt_json,'$.cmd')`、`scheduled` は receipt_json 内フィールド |
| S15 | lightweight tag は `ls-remote ...^{}` 行が返らず peel 解析が空に。`git fetch --tags` は移動 tag の reject で非ゼロ終了し得る | 中 | `ls-remote` は `refs/tags/<tag>` と `^{}` の両パターンで問い peel 行なしは直行 sha。fetch 成否は終了コードでなく post-fetch `rev-parse` で判定 |
| S16 | 保護パス検査が「実在 ignored ファイル」基準では不十分 — 将来作られるファイル・ルールだけのパスを見逃す | 中 | `git check-ignore --stdin` で tag の全パスに ignore 規則を適用（ルールベース）+ 固定集合 + casefold/NFC 正規化 |
| S17 | `executed` の記録時点が未定義 — 成功のみ記録だと失敗 apply が永久「未実行」で F3 抑止が崩れる。rollback は冪等でない | 中 | `executed` は試行終了時に成否不問で記録（結果は `attempts`）。rollback 二重実行は「対象 applied 存在」で防ぐ。apply は `HEAD==target` で早期 return |
| S18 | veto・`update.mode` 遷移・複数 pending の意味論が未定義 | 中 | 後続 `update_rollback` receipt が pending apply を cancel・`off` 中は実行抑止（receipt 保持）・実行中 apply は中断しない・1起動で最新 tag のみ実行 |
| S19 | `update_state.json` の統一スキーマ未定義 — `stages` が前回 attempt の残滓で誤判定し得る | 中 | `"v":1` スキーマを規定（⑥）。新 apply 開始時に `stages` 初期化 |
| S20 | lock 取得前に読んだ state（承認対象・base_sha）と lock 取得後の実状態が乖離し得る | 中 | lock 取得直後に state 再読 + `HEAD==承認時 base_sha` 検証（④段階6） |
| S21 | run.lock リトライ 30s×20=10分は tick 最大（RUN_DEADLINE 8分）+drainer write 窓に対して余裕が薄い | 低 | 30s×40=最大20分に引上げ |
| S22 | services の script/plist 書込みが非 atomic — 中断で半分ファイルを残す | 中 | tmp+os.replace で atomic render に変更（services 強化に明記） |
| S23 | git 呼出の hang が update.lock を恒久占有し得る（flock はプロセス死まで解放されない） | 中 | 全 git subprocess に timeout + `GIT_HTTP_LOW_SPEED_LIMIT` 規定 |
| S24 | recover.py が将来の `"v"` 変更を読めない場合の扱い未定義 | 低 | 不明スキーマは保守的にエスカレート（何もしない）に規定 |
| S25 | rollback のサービス復元が旧コードの reconcile 実装に依存 — P2 初回 rollback で reconcile が存在しない | 中 | メンバーシップ復元を recover.py（manifest snapshot 正本）に移し、repo 側 services はコンテンツ再レンダリングのみ担当（⑤に2層分離を明記） |

## 対応策の詳細設計

各指摘の具体的な実装方式:

### F1 untracked による誤ブロック / 衝突検出
- `git status --porcelain -uno` で tracked 変更のみ判定。
- untracked 衝突は事前に正確に報告する:
  `git ls-files --others --exclude-standard` ∩
  `git diff --name-only HEAD <tag>` — 衝突ファイル名を通知に載せ、
  人が削除/移動してから再承認する運用。apply 自体は `merge` の
  abort に任せる。

### F2/F12 事前・事後チェックの分離（誤ブロックと誤ロールバックの両方を防ぐ）
- **事前** = `validate_config`（現行コード）+ ロック/容量/git 健全性のみ。
  環境 probe（Keychain/Chrome/LLM/gateway）は事前に置かない。
- **新コード preflight**: merge 前に `git ls-tree` 列挙 +
  `git cat-file blob` で `mcs/` 一式を tmp dir に再構成
  （`git archive` は候補側 `.gitattributes` の export-ignore/
  export-subst で潜脱可能なため不使用 — S3。`mcs_setup.py` 単体では
  `_mcs_path` ブートストラップ不在で起動不能 — B1）し、
  **本番と同じ Python** で `validate_config(現行 config)` を
  subprocess 実行 — 必須キー追加を merge 前に検出し、
  「`init` で設定追加が必要」と通知して中止（apply→rollback の
  空転を回避）。呼出契約の版間差は「preflight 不能」として区別。
- **事後** = 完全 `check` だが**ベースライン差分方式**: apply 前の
  `check` 出力を記録し、事後に**新規に出た error のみ**を rollback
  トリガとする。既存の環境 error（例: keychain_locked）は更新起因で
  ないため warning 通知に留める — 更新前から壊れていた環境で
  ロールバックが空転しない。

### F3 失敗 tag の再試行抑止
- `update_state.json` に `attempts: {tag: {result, at, reason}}`。
- `apply_failed` の tag は check/apply が自動では再挑戦しない。
  再挑戦は `ops.update_apply`（人の再承認）または新 tag 出現のみ。

### F4 通知テキストの sanitize
- `update_notice` の本文生成時に `@everyone`/`@here`/`<@...>`/
  `<@&...>`/`<#...>` を正規表現で無害化（`@`→`＠` 等）してから
  outbox に積む。本文は凍結テキスト（sender は再生成しない）。

### F5 tag の検証と SHA ピン
- `ops.update_apply` の `tag` は `^v?\d+\.\d+\.\d+$` で厳格検証
  （`validate_ops` のフィールド schema で）。
- apply 時の照合は **`git ls-remote origin "refs/tags/<tag>"
  "refs/tags/<tag>^{}"` の remote 真値**で行う — `git fetch` は移動
  した既存 tag を更新しないため local `rev-parse` は記録 sha のまま
  残り、local 照合だけでは remote 付替えを見逃す（R12）。peel 行が
  あればその sha、なければ（lightweight）直行 sha を commit sha と
  する（S15）。不一致は中止+要再レビュー通知。merge 対象の local
  ref sha も一致確認する。
- **annotated tag は tag オブジェクト SHA と commit SHA が異なる**
  （実リポジトリにも存在）。検出・照合・merge・HEAD 比較はすべて
  `^{commit}` peeled commit SHA で統一する。

### F6 rollback 時の gateway restart
- apply 時に `plugin_changed`（`hermes_plugin/` の diff 有無）を
  state に記録。rollback は reset 後に `services` + このフラグを見て
  `gateway restart` を実行。

### F7 状態ファイルの直列化（R9 で修正 — 旧設計は ABBA デッドロックあり）
- **`update_state.json` の読取は lock-free**（tmp+os.replace の atomic
  publish により破損した読取は起きない）。lock は writer のみが取る。
- **lock-free 読みにより ops ハンドラは `update.lock` を一切取らない**
  — 承認は receipt commit のみで確定するため handler 側に lock 不要。
  これで旧設計（handler が run.lock 保持中に update.lock を取得 ↔
  apply が update.lock 保持中に run.lock を待機）の **ABBA デッド
  ロックを構造的に排除**する。
- writer（check/apply/recover）は `data/update.lock`（flock）。
  両 lock を持つのは apply のみで順序は常に **update.lock → run.lock**。
  apply が update.lock を保持し続けること自体が「apply 進行中」の
  生存信号になる（watchdog/recover は NB 試行で busy=生存と判定）。
- state の破損（読めない JSON）は「unknown」扱い — 何もせず
  エスカレート（触って壊さない）。`"v"` フィールドで将来の
  スキーマ変更に備える。

### F8 apply 中断の復旧（R1 で拡張 — 3態判定は撤回）
- **HEAD 単独では判定不能**（混在ツリーが再現された）。
  ⑤の判定表（stages ジャーナル + HEAD + porcelain + MERGE_HEAD）に
  置き換え済み。`git merge --abort` は MERGE_HEAD がある場合のみ有効。

### F9 backup 命名と保持（B3 で修正 — 「最新3件」は撤回）
- `data/backups/preupdate-<Ymd-HMS>.db`（`ledger-*` glob に混入しない）。
- 保持ポリシー: **復旧に必要な世代は消さない** — `update_state.json` が
  参照する actionable な rollback 点（各 `applying`/`applied` 記録に紐付く
  `backup_path`・`prev_sha`・`schema_ver`）に対応する backup は保持し、
  参照されないものだけを期限で prune。「最新3件」だと fetch 失敗の
  繰り返しで必要な復旧点が押し出されるため、件数ではなく参照で管理。

### F10 killswitch
- `update.mode:"off"` は check 自体を行わない（status/rollback の
  CLI は使える）。

### F11 サプライチェーン対策
- `auto` 既定は**猶予付き**: 検出時に通知、次回 check（翌日）までに
  `ops.update_rollback`/`update.mode` 変更がなければ apply。
  即時適用は `update.auto_delay_h: 0` で opt-in。
- 将来の強化案（任意）: tag 署名の導入 + `update.require_signed_tag`
  で auto に GPG 検証を必須化 — 現行リリースは未署名のため未対応。

### F13 run.lock 待ち
- LOCK_NB のため updater 側で `retry_interval`/`retry_max` を持つ
  有限待機（例: 30s × 20回 = 最大10分。run は通常1分以内）。

### F14 updater 未配置の受理拒否
- ops ハンドラは `~/.hermes/scripts/mcs_update.sh` の存在を確認し、
  なければ receipt `rejected: updater_not_deployed`。

### F15 semver 比較
- `^v?(\d+)\.(\d+)\.(\d+)$` の数値 tuple で比較。非 semver tag は
  検出対象外。`-beta`/`-rc` 等の suffix 付きは既定で除外
  （`update.include_prerelease` で opt-in）。

### F16 merge 中のプロセス混在（R4 で方針変更 — 「許容」は撤回）
- run.lock は常駐 drainer（`ai.mcs.extract-drainer{,-rt}`、KeepAlive）
  を隔離**しない** — 旧コードで書込みを継続し得る。
  また lock 取得前に DB を開く経路があり、起動時 migration を
  lock では防げない。
- 対策: ④段階8の **quiesce**（marker→drainer bootout+停止検証+
  stray sweep）と段階12の**再起動+稼働版確認**（apply 後起動の
  PID を確認）。**S8 でさらに補強**: drainer の claim/checkpoint/
  release は run.lock 無しで書く経路があり、`mcs_llm_catchup.sh` が
  quiesce 中に再 spawn し得る — `update_in_progress.marker` で
  補助 launcher を沈黙させる。
- schema_bump を含む更新は auto では到達しないため、lock 外の
  migration は残課題として許容しない。
- cron 時刻を早朝（05:10）にして発火確率を下げるのは併用。

### 追加指摘（レビュー継続で発見）
- **F17 wrapper の自己書換え**: `services` が実行中の
  `mcs_update.sh` 自体を再レンダリングし得る。bash はスクリプトを
  逐次読むため実行中の書換えで破壊される → wrapper は
  `exec "$PY" .../mcs_update.py apply` を**最初の行**に置き、
  本体ロジックは全て Python 側に置く。
- **F18 `applying` 記録の位置を固定**（S7 で再修正）:
  「quiesce より前・lock 取得直後」に記録する。fetch/ネットワーク障害は
  全て applying 記録前に終了し、quiesce 中の crash でも `applying`
  残存として watchdog が捕捉できる（停止操作より前に中断マーカーを
  永続化するのが原則）。
- **F19 成功通知が新コードで失敗し得る**: post-apply の通知送信が
  新バージョンのバグで失敗しても apply 自体は成功とみなし、
  `health.json`/`run.log` に記録する（通知経路の故障でコードを
  ロールバックしない）。
