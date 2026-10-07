# MCS 更新実行手順書（AIエージェント用）

このガイドはv1.0.15仕様のinstall/update/setup/doctorと復旧手順に対応する。
1.0.15の合成検証・公開・実機反映の状況は[リリース受入票](../development/acceptance/ACCEPTANCE_1.0.15.md)で追跡する。
過去の1.0.13実機受入計画は[開発・受入計画](../development/plans/RELEASE_1.0.13.md)に残す。

### 1.0.15への更新で確認すること

追加設定は不要です。DB schemaは9を維持し、検索・取得・配送用の索引を加法的に追加します。
台帳の保存データを削除する移行や、抽出モデルの変更はありません。古い版から更新する場合は、その間のschema変更・導入条件も`plan`で確認します。

- Hermesモードはgatewayと抽出ワーカー2本、独立実行は使用中のhost・通知アダプター・抽出ワーカーへ新しいコードを反映します。LINE WORKSの独立アダプターも再起動してください。この版への更新直後は旧更新コードの再起動処理に頼らず、§6で稼働プロセスの反映を確認します。
- `recovery_tool_changed`がtrueなら、復旧watchdogの**本体**も同期します。`mcs setup services`だけではcheckout外の本体を更新しません。§2の表に従い、導入済みの機能とopt-outを維持して`install.sh ... --no-services`を再実行します。復旧機能を使う環境では、この同期を`--no-recovery`で省略しません。既存の安全な`recovery_python`を維持し、別のPythonへ自動で切り替えません。
- 通知文に患者名・投稿内容が含まれます。端末の通知プレビュー設定を確認してください。更新後の稼働確認は、実端末での受信・表示・カード操作の確認とは区別します。

### 2026-10-04追記: 現在の更新入口と回帰資産

[共通CLI](../../mcs/ops/mcs_cli.py)の`mcs update plan`は取得済みタグだけを対象にし、
fetchせずに既存bootstrapへ委譲します。阻害時は非ゼロ終了であり、表示できたことを
適用可能と扱いません。既存の`mcs_upgrade.py`・`mcs_update.py`による手順は残ります。
v1.0.0〜1.0.2は手動更新条件を保持し、standaloneの外部applyは阻害されるためhost経由の
既存手順を使います。apply・rollbackには従来の停止・バックアップ・承認契約が適用され、
schema巻戻しの個別復旧同意を更新承認で代用できません。

[更新経路manifest](../../tests/fixtures/schema_upgrade/update-paths.json)は過去26経路の根拠と、
現行15公開版・20構成のplan/apply/rollback/bootstrap/reinstall回帰を区別します。
テストは完全合成でGit履歴に依存しませんが、旧target updater全体の26経路再実行、
実Git・host協調停止・SDK導入・実機配備を検証したものではありません。
launcherの選択runtime追随とbackup lifecycleは合成回帰で検証し、
ローカルCLI提供や合成検証を実機受入・配備完了と読み替えません。

> **この文書は AI エージェント（Claude Code / Codex / Devin 等）が読み込み、
> 既存の MCS 導入を過去版（v1.0.0〜）から新しい版へ、データ移行を含めて
> 更新するための実行手順書です。** 新規導入は [SETUP_AGENT.md](SETUP_AGENT.md)、
> 人向けの設定・サービスの説明は [INSTALLATION.md](INSTALLATION.md) を参照します。

## 0. 実行契約（最初に読むこと）

- **更新は配備に当たる。** ユーザーが更新を明示依頼した範囲でだけ実行する。
  文書の閲覧・`plan`だけでは適用の承認にならない。
- 各ステップは【実行】→【検証】の順。検証を飛ばさない。失敗時は【失敗時】を実行し、
  解決しなければ原因（エラートークン）を報告してユーザー判断を待つ。
- **既存状態を保護する。** checkout の未コミット変更を stash / reset / clean / checkout で消さない。
  `data/`・`config.json`・`.env` を手で編集・削除しない。秘密値を表示・入力させない
  （必要なら [SETUP_AGENT.md §0-1](SETUP_AGENT.md#0-1-秘密情報プロトコル) に従う）。
- 更新中は収集・抽出・通知が止まる。適用はタグの照合・取得でネットワークが必要。
  再導入（install.sh）を伴う場合はbrew・pipの導入で数分〜最大1時間かかる（既存のHermes本体の版は
  install.shが移動せず警告だけ出す）。開始前にユーザーへ伝える。
- **更新を実行するのは常に移行先版のコード。** 現在の checkout の更新機能は使わない。

## 1. 対象と Python の特定

| # | 確認 | コマンド | 合格条件 |
|---|---|---|---|
| 1 | checkout | `git -C ~/.mcs rev-parse --show-toplevel`（別の場所に導入した場合はそのパス。以下 `$REPO`） | パスが出る |
| 2 | 導入形態 | `python3 -c "import json;print(json.load(open('$HOME/.mcs/config.json')).get('runtime_mode','hermes'))"` | `hermes` か `standalone` |
| 3 | Python | hermes: `PY=~/.hermes/hermes-agent/venv/bin/python` / standalone: `PY=~/.mcs/venv/bin/python3`。`$PY -V` | 3.10 以上 |

素の `python3`（macOS の 3.9 系）で更新処理を実行しない。

## 2. 起動役の取得と計画（書込みなし）

【実行】移行先タグ（ユーザー指定。無ければ最新リリース。以下 `$TAG`、例 `v1.0.15`）から
起動役を取り出し、計画を出す。`fetch` はタグを取り込むだけで checkout を変えない。

```bash
git -C "$REPO" fetch --tags origin
WORK=$(mktemp -d)        # 本人だけが読める作業dir。共有の /tmp 直下に置かない
git -C "$REPO" show "$TAG:scripts/mcs_upgrade.py" > "$WORK/mcs_upgrade.py"
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" --no-fetch plan --to "$TAG"
```

【検証】JSON が出る。主な項目:

| 項目 | 意味 |
|---|---|
| `route` | `apply`（通常適用）/ `reinstall`（install.sh 再実行を伴う適用）/ `blocked`（§3で解消）/ `noop`（更新済み） |
| `current` / `target` | 現在のタグ・SHA・DB schema / 移行先 |
| `blockers` | 解消が必要なエラートークン |
| `reinstall` | `install_sh_changed`（install.sh 変更）・`standalone_requirements_changed`（独立モード SDK 固定版の変更） |
| `schema_bump` | DB の版変更。あれば事前バックアップからの復元が巻戻しに含まれる |
| `restarts` | 更新後に必要な再起動（`gateway`・`lineworks_adapter`） |
| `recovery_tool_changed` | 復旧 watchdog の変更。`reinstall` 経路なら自動で反映（`--no-recovery` を付けた場合を除く）。`apply` 経路では適用後に、承認を得て `./install.sh <opt-out> --no-services` を再実行する |
| `notes` | 現在版より新しく移行先までの各版の「更新時の注意」（CHANGELOG） |

1.0.13以降、macOS 標準 `/usr/bin/python3` の SQLite は WAL-reset 影響版のため、
`update apply`・`services` は復旧 watchdog の実行 Python が安全と確認できるまで停止する
（`plan` は merge 前に `recovery_runtime:` を blockers に出し、手順を `notes` に示す）。
オーナー承認のうえ、checkout 外にある既存の安全な独立 Python の絶対パスを `recovery_python` として保存する。
1.0.13 以降が入っていれば `mcs setup init --yes --recovery-python <path>` と `mcs setup services` で反映する。
1.0.12 以前から更新する場合は init にこのオプションが無いため、`~/.mcs/config.json` に
`"recovery_python": "<path>"` を手で追記してから `plan` を再実行する。所有済みで停止中の watchdog は
merge 後の `services` が置き換える。`reinstall` 経路では `--install-arg=--no-recovery` を付けて apply する
（install.sh は不一致の watchdog を置き換えずに停止するため）。自動で別 Python へは切り替えない。

`$TAG:scripts/mcs_upgrade.py` が無い（v1.0.10 以前を移行先にした）場合は、その版へはこの手順で更新できない。

## 3. 阻害要因の解消

`blockers` ごとに対処し、§2 の `plan` を再実行して `blockers` が空になるまで繰り返す。

| トークン | 対処 |
|---|---|
| `tree_dirty` | `git -C "$REPO" status --short` を示し、**【ユーザー確認】**。他の作業者の変更の可能性がある。勝手に退避・破棄しない |
| `config: …` / `new_config: …` | 現在 / 移行先の設定検証エラー。キーと理由を示し、承認後に `"$PY" "$REPO/mcs/ops/mcs_setup.py" init --set KEY=JSON` で直す（秘密値は扱わない） |
| `update_in_progress_or_interrupted` | 前回の更新が途中。`"$PY" "$REPO/mcs/ops/mcs_update.py" status` を示し、`recover` を実行してから再計画 |
| `restore_consent_pending` | DB 復元の人承認待ち。[INSTALLATION.md](INSTALLATION.md) の復元承認手順をユーザーが行う |
| `hermes_not_resolvable` / `standalone_interpreter_unavailable` | 実行基盤が無い。`./install.sh --preflight` の `fix:` を確認しユーザーと対処 |
| `insufficient_disk` | DB の2倍+64MB の空きが必要。ユーザーに空き容量の確保を依頼 |
| `not_fast_forward: …` | checkout が移行先の祖先でない（ローカルコミットや新しすぎる版）。**【ユーザー確認】** |
| `untracked_collision: …` | 未追跡ファイルが移行先の追跡ファイルと衝突。内容を示し **【ユーザー確認】** |
| `schema_downgrade:…` | 移行先が古い。対象タグを見直す |
| `schema_bump_auto_blocked` | `update.mode=auto` では schema 変更を適用しない。ユーザーと方針を決める（`notify` へ変更等） |
| `standalone_external_apply` | 独立モードは host 経由で更新する（[STANDALONE.md](STANDALONE.md#更新復旧)）。`reinstall` がある場合は同書の再導入手順 |
| `legacy_source_manual` | v1.0.0〜1.0.2 からの更新。§8 の手動経路へ |
| `reinstall_incomplete` | 前回の再導入が install.sh の途中で中断した。`status` を示し **【ユーザー確認】** の上で `cd "$REPO" && ./install.sh <前回と同じ opt-out> --no-services` → `"$PY" mcs/ops/mcs_setup.py services` → `"$PY" mcs/ops/mcs_update.py reinstall-done`（HEAD が再導入した版のときだけ完了を記録し、残りの後処理を復旧処理で完了）→ `check`。復旧処理は install.sh を無人で再実行しない |
| `recovery_incomplete`（復旧レポート） | 抽出処理の起動確認が未完了。中断記録を保持するため、起動失敗の原因を修復して `recover` を再実行する。同じ復旧の再試行では起動済み処理を停止し直さない |
| `update_state_corrupt` | `data/update_state.json` が読めない。内容を変えずに報告し、ユーザー判断を待つ |
| `tag_not_fetched` | `git fetch --tags origin` が失敗している。ネットワーク・タグ名を確認 |
| `validate_config_failed` / `preflight_failed: …` | 現在 / 移行先コードで設定検証が実行できない。出力を報告 |
| `standalone_runtime_missing_in_target` | 移行先が独立モード非対応。対象タグを見直す |
| `plan_failed: …` | 計画中の検査が失敗した。内容を報告し再試行の可否をユーザーと決める |
| その他（`protected_path`・`unsafe_path`・`ignored_path_tracked` 等） | 移行先タグの異常。適用せず報告 |

## 4. ユーザー確認

次をまとめて提示し、適用の承認を得る（既に同じ内容で承認済みなら聞き直さない）。

- 現在版→移行先、`route`、停止の見込み時間
- `notes` の要約（版ごと。必要な追加操作を明示）
- `reinstall` の場合: install.sh の再実行と、導入時に付けた opt-out の確認。
  記録が無いため、ユーザーに `--no-llm`（自前 LLM サーバ）・`--no-brew`・`--no-plugin`・`--no-recovery`
  のどれを使っていたか確認する（`--mode` は不要。設定から自動判定）
- `schema_bump` の有無、`restarts`

## 5. 適用

【実行】

```bash
# route=apply
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" --no-fetch apply --to "$TAG"
# route=reinstall（確認した opt-out だけを --install-arg=… で付ける）
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" --no-fetch apply --to "$TAG" --reinstall --install-arg=--no-llm
```

`--no-fetch` は起動役の事前取得を省くだけで、適用自体はタグの照合のためネットワークを使う。
適用は移行先版の `mcs_update.apply` が行う: 更新ロック取得 → 検査 → `data/backups/preupdate-*.db` 取得 →
実行ロック → 常駐処理の停止 → `git merge --ff-only` → 新コードで（再導入 →）サービス再同期 → 再起動 →
DB を開いて移行（加法 migration）→ 事後検証。失敗時は自動で元の SHA へ戻し、schema 変更があれば DB を復元する
（復元前の損失報告に人承認が必要な場合は保留になる）。install.sh の副作用（brew・venv）は巻き戻らない。

【検証】終了コード 0。0 以外は §7。

## 6. 後処理と検証

1. `restarts` に `gateway` があり Hermes モードなら、apply が再起動を依頼済み。`hermes gateway status` で稼働を確認する。
2. `lineworks_adapter` があれば、[LINEWORKS.md](LINEWORKS.md) の手順で独立アダプターを再起動し `python -m lineworks_adapter check`。1.0.15では自動更新・巻戻し・復旧後の再起動を追加しましたが、この版への初回更新後も明示的な再起動と反映確認を行います。
3. `notes` にある追加操作（承認済みのもの）を実行する。`apply` 経路で `recovery_tool_changed` が true なら §2 の表のとおり install.sh を再実行する。
4. 使用中のgatewayまたは独立host・通知アダプターと抽出ワーカー2本について、更新後に起動したプロセスであることと稼働状態を確認します。古いworkerが新しい通知形式を保留している場合、保存ファイルを削除して再送させず、更新したworkerで通常の配送を再開します。
5. 検証:

```bash
"$PY" "$REPO/mcs/ops/mcs_update.py" status     # applied の最後が $TAG、applying が null
"$PY" "$REPO/mcs/ops/mcs_setup.py" check       # exit 0
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" --no-fetch plan --to "$TAG"   # route=noop
rm -r "$WORK"
```

## 7. 失敗時・巻戻し

- apply が非0: `status` の `attempts[$TAG].detail` を報告する。自動巻戻し済みなら checkout は元の SHA。
  `rollback failed — escalate` の場合は追加操作をせず報告する（復旧 watchdog と人の判断に委ねる）。
- 適用後に問題が見つかり戻す場合（ユーザー承認後）: `"$PY" "$REPO/mcs/ops/mcs_update.py" rollback`。
  schema 変更があった更新は DB 復元を伴い、損失報告への人承認を求められることがある。
- install.sh が失敗した場合は出力末尾の `NG` / `fix:` を示し、ユーザーと対処してから §2 からやり直す。

## 8. v1.0.0〜1.0.2 からの手動経路

この3版には更新機能・サービス同期が無く、自動の巻戻しが成立しない。各手順をユーザーに確認しながら行う。

1. 停止: `launchctl list | grep -E 'ai\.mcs|local\.mcs'` で MCS のジョブを示し、承認後に
   `launchctl bootout gui/$(id -u)/<label>` で止める。`pgrep -fl 'run_check|extract_llm|semantic_drain'` が空になるのを確認。
   Hermes cron の MCS ジョブも `hermes cron list` で確認し一時停止する。
2. バックアップ: `sqlite3 ~/.mcs/data/ledger.db ".backup '$HOME/.mcs/data/backups/preupgrade-manual-$(date +%Y%m%d-%H%M%S).db'"`、
   取得したファイルで `PRAGMA integrity_check` が `ok`。現在の SHA を記録（`git rev-parse HEAD`）。
3. 更新: `git -C "$REPO" merge --ff-only "$TAG"`。
4. 再導入: `cd "$REPO" && ./install.sh`（§4 で確認した opt-out を付ける）。install.sh がサービスを配置する。
5. 検証: `"$PY" mcs/ops/mcs_setup.py check` が exit 0、§6 の 1〜3。
6. 巻戻し（必要時・承認後）: `git -C "$REPO" reset --keep <記録したSHA>`、DB を手順2のバックアップへ戻し、
   手順1で止めたジョブを再読込する。

## 9. 完了報告

```
MCS 更新: <現在版> → <移行先>（route=<…>）
- 適用: 成功 / 失敗（理由トークン）/ 巻戻し済み
- DB: schema <前>→<後>、事前バックアップ <パス>
- 再起動: gateway <済/不要>、LINE WORKS <済/不要/未実施>
- 検証: status <…>、check <exit>、再plan <noop>
- 各版の注意で実施した操作 / 未実施の操作と理由
```
