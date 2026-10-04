# 過去版からの安全な更新（1.0.11 #30）

2026-10-04版割当: 旧1.0.13〜1.0.15の残件は全て安定稼働版1.0.13へ集約。
成果物・CLI・受入の正本は[1.0.13開発計画](../development/RELEASE_1.0.13.md)。
当時の調査・設計例と現在の実装状態を区別し、既存実装は再実装しない。

### 2026-10-04追記: #30のrepo内回帰資産

[update-paths.json](../../tests/fixtures/schema_upgrade/update-paths.json)は元26経路の
版・runtime・根拠と現行13公開版16構成を区別し、
[合成回帰](../../tests/ops/test_supported_update_paths.py)でplan/apply/rollback/
bootstrap/reinstall・旧版手動条件・個別復旧同意を検証します。
テスト実行はGit履歴に依存しません。過去runner/rawログはrepoに保持されておらず、
旧target updater全体の26経路再実行や原schema0〜4/6の再現とは主張しません。
実Git・host協調停止・SDK導入・実機配備と、統合中のbackup lifecycleは別ゲートです。

改訂日: 2026-10-03。状態: 実装済み（2026-10-03、計画レビュー・実装再レビュー反映）。

## 1. 目的と受入

v1.0.0〜v1.0.10のどの版からでも、AIエージェントが手順書を読み、
計画確認→ユーザー承認→適用（データ移行込み）→検証→完了報告まで進められるようにする。

受入:
- 現在版・移行先・経路・阻害要因・再導入要否・schema変更・再起動対象・各版の「更新時の注意」を、
  書込みなしの`plan`で一度に確認できる（JSON出力あり）。
- 更新を実行するのは常に**移行先版のコード**。旧版の更新処理の有無・不具合に依存しない。
- 自動経路（ロック・巻戻し付き）はv1.0.3以降が移行元。v1.0.0〜1.0.2は旧treeに`services`が無く自動巻戻しが
  成立しないため、`plan`が`legacy_source_manual`を阻害として返し、手順書の手動経路（事前バックアップ・旧ラベル停止・
  merge・install.sh・services・check、巻戻しは手動）へ案内する。
- 既存の安全経路（update.lock/run.lock、journal、DBの事前バックアップ、drainer停止、ff-only merge、
  新コードでのpost-merge検証、失敗時のtree巻戻し・schema変更時のDB復元、復旧watchdog）をそのまま使う。
- install.shや独立モードSDK固定版が変わるタグも、操作者の明示指定（`--reinstall`）で同じ経路のまま適用できる。
  自動更新・receipt経路は従来どおり再導入を拒否する。
- 合成fixtureのテストで、旧版相当のrepoから移行先コードによるplan/applyの判定を固定する。

## 2. 現状の穴（9ea015d時点の照合）

| 穴 | 根拠 |
|---|---|
| v1.0.0〜1.0.2には`mcs_update.py`が無い | `git cat-file -e v1.0.2:mcs/ops/mcs_update.py`が失敗。v1.0.0は`mcs/`直下の平置き |
| `apply`は**旧版のコード**で走り、post-mergeだけ新コード | `_run_post_merge`は親の`__file__`を起動。旧版の判定・不具合は新版で直せない |
| install.sh・独立SDK固定版が変わるタグは常に中止 | `precheck_tag`の`install_sh_changed`/`standalone_requirements_changed`。1.0.9以前→1.0.11はinstall.shが変わる |
| v1.0.2〜1.0.7の常駐`ai.mcs.extract-drainer-rt`（KeepAlive）を`quiesce`が止めない | `RESIDENT_LABELS`は2ラベルのみ。stray sweep後にlaunchdが再起動する |
| 版をまたぐ「更新時の注意」を集める手段が無い | CHANGELOGの`### 更新時の注意`に版ごとに散在 |
| AI向けの更新手順書が無い | `SETUP_AGENT.md`は新規導入のみ |

データ移行の現状: ledgerは開いた時に列の有無で加法migrationを行い（`Ledger._migrate`）、
DBは`data/ledger.db`1本（`llm_admission.db`は一時的な調停用）。applyは毎回`preupdate-*.db`を取得し、
postcheckでLedgerを開くため、移行はpost-merge内で実行・検証される。別の移行枠組みは作らない。

## 3. 設計

### 3.1 起動役 `scripts/mcs_upgrade.py`（新規・標準ライブラリのみ・repo非import）

移行先タグから取り出して実行する。現在のcheckoutの版を問わない。

```bash
git -C ~/.mcs fetch --tags origin
git -C ~/.mcs show v1.0.11:scripts/mcs_upgrade.py > "$TMPDIR/mcs_upgrade.py"
"$PY" "$TMPDIR/mcs_upgrade.py" --repo ~/.mcs plan --to v1.0.11 --json
"$PY" "$TMPDIR/mcs_upgrade.py" --repo ~/.mcs apply --to v1.0.11 [--reinstall --install-arg=--no-llm ...]
```

処理: Python≥3.10とrepo（gitのtop-level）の確認 → `--to`（省略時は最新の安定版タグ）をsemver検証 → `<tag>:mcs/`を
`ls-tree -rz`/`cat-file`で0700の一時dirへ展開（`precheck_tag`と同じ方式。git archiveの属性展開を避ける）
→ `MCS_UPDATE_REPO=<repo>`を設定して展開先の`mcs_update`を呼ぶ → 一時dirを削除。
移行先にこの起動役が無い版（≤1.0.10）は対象外。

### 3.2 `mcs_update.py`・`mcs_util.py`の変更（移行先版の更新処理）

1. repo rootを`mcs_util.REPO`に一元化する（`MCS_UPDATE_REPO`があればそれ、無ければ`__file__`由来）。
   `mcs_update.REPO`と`mcs_setup.REPO_ROOT`はこれを参照する。一時dir起動でも`_baseline_check`・services描画・
   `check_environment`が実checkout基準になる（B1）。
2. `_run_post_merge`は`REPO/mcs/ops/mcs_update.py`を起動する（通常運用では同一パス）。
3. `plan(tag)`（新規・読取り専用）: 既存`precheck_local`/`precheck_tag`のエラートークンを
   `blockers` / `reinstall`（`install_sh_changed`・`standalone_requirements_changed`）/ `schema_bump`に分類し、
   現在版・live `user_version`・journalと同意保留の有無・移行元が≤1.0.2か（`legacy_source_manual`）、
   再起動対象（gateway: 既存`plugin_changed`の差分計算を再利用、LINE WORKS: `adapters/lineworks`・`lineworks_adapter`の差分）、
   復旧ツール差分（情報のみ。install.sh再実行の案内）、CHANGELOGの(現在版, 移行先]の`### 更新時の注意`、
   次に実行するコマンドを返す。`--json`対応。
4. `apply(..., reinstall=False, install_args=())`: `reinstall`はCLIの`apply`（`command_id is None`かつ
   自動経路でない）だけが渡せる。再導入系2エラーを阻害から外し、`applying.reinstall`・`install_args`をjournalに記録する。
   receipt・自動経路は従来どおり中止。`install_args`は`--no-llm`・`--no-brew`・`--no-plugin`・`--no-recovery`のみ
   （`--mode`は許可しない。install.shが既存configから判定する）。
5. `_post_merge()`内のservicesの前で、`applying.reinstall`なら`REPO/install.sh <install_args> --no-services`を
   stdin無しで実行する（上限3600秒、post-merge全体の上限も延長）。`recover_interrupted`のpost-merge再開でも
   再実行される。失敗は`install_failed`で既存のbail→tree巻戻し。install.shの副作用（brew・venv）は巻き戻さない。
6. `quiesce`は旧常駐ラベル`ai.mcs.extract-drainer-rt`がloadedならbootoutする（再起動対象には含めない。
   新版のservicesが所有ラベルを整理し、巻戻し時は旧版のservicesが再作成する）。
7. 独立モード: hostの協調停止を前提とする`quiesce`のため、起動役からの外部applyは`plan`で
   `standalone_external_apply`として阻害に出し、host経由の更新と再導入手順を案内する。
   <!-- ponytail: standalone外部applyは未対応。需要が出たらhost停止を検証してquiesceを分岐する -->

schema変更の手動許可（auto設定時）はSCHEMA_VERSIONがv1.0.0〜現行で7のまま不変なため今回は扱わない。
1.0.11でschemaを上げる場合に同時に設計する。

### 3.3 AI用手順書 `docs/guides/UPGRADE_AGENT.md`（新規）

`SETUP_AGENT.md`と同じ形式（【実行】→【検証】→【失敗時】）。
0 実行契約（更新は配備相当。ユーザーの明示依頼が範囲。秘密値・stash/reset禁止。停止時間とネットワーク依存〔brew/pip/hermes〕を事前に伝える。
導入時のopt-out〔--no-llm等〕は記録が無いのでユーザーに確認）→
1 repo・Python・導入形態の特定 → 2 起動役の取得と`plan` → 3 阻害の解消（dirty tree・config・中断journal・同意保留は
ユーザー判断、修正コマンドの提示）→ 4 ユーザー確認（注意事項・再導入・schema・再起動を要約）→ 5 `apply` →
6 後処理（gateway・LINE WORKSの再起動、`check`）→ 7 検証（`status`・`check`・再`plan`がnoop）→
8 失敗時・ロールバック（`mcs_update.py rollback`、DB復元の同意）→ 9 完了報告。

### 3.4 文書・記録

ROADMAP §4/§5へ#30を追加、`INSTALLATION.md`の更新節から手順書へリンク、
`changes/upgrade-agent-20261003.json`、`DEVELOPMENT.md`生成ブロックの再生成。

## 4. 検証

- `tests/ops/test_mcs_upgrade.py`（新規・`ops_testkit._make_repo`の合成repo）:
  起動役の展開と`MCS_UPDATE_REPO`（baseline・servicesが実checkout基準）、≤1.0.2移行元の阻害、旧drainerラベルのbootout、plan分類（install.sh差分→reinstall、dirty→blocker、schema bump、注意の版範囲）、
  `install_args`の拒否、receipt/自動経路での再導入拒否、operator時のschema変更許可、post-mergeでのinstall.sh呼出し（stub）・再開時の再実行・失敗時bail。
- 既存`test_mcs_update.py`の回帰、`ruff`、`update_readme.py --check`。実機の更新・launchd・ネットワークは実行しない。

## 5. 範囲外

複数版の段階適用（migrationが累積・加法のため直接移行で足りる）、install.sh副作用の巻戻し、
独立モードの外部apply、Hermes本体の版管理。
