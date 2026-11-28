# MCS 更新実行手順書（AIエージェント用）

Hermes経由の導入・更新にはuvが必要です。Hermesのディレクトリで依存を解決し、安全性のための依存設定を適用します。uvがない場合は変更前に停止し、pipへ切り替えません。`--no-brew`を使う場合はuvを事前に導入してください。

### 更新前に認証情報ファイルの所有者と権限を確認する

一般の認証情報は環境変数を優先し、必要に応じて `~/.mcs/.env`、`~/.hermes/.env` の順に探します。更新前に、使っているファイルの所有者と権限を `ls -l` で確認してください。ファイルの中身を表示する必要はありません。
一般の読取りに使えるのは、MCSの実行ユーザーが所有し、グループと他のユーザーには権限がない、64 KiB以下の通常ファイルです。`0644` のファイルやシンボリックリンクなど、この条件を満たさないファイルは警告 `local_file_permissions_weak` を出して読み飛ばします。本人が所有する通常ファイルの権限が `0644` なら、対象のファイルを `chmod 600` で変更してください。Jev・Anthropic・OpenAIの実行時キーにはMCS専用の `~/.mcs/.env` を使い、権限は `0600` が必要です。

### 更新前に確認する設定

- AnthropicとOpenAIの合計費用上限は、省略時に1日80ドル・1か月350ドルです。明示した金額、`0`、`null`（その上限を解除）、対象範囲、`mode=off` を引き継ぎます。更新時に提供元別予算は追加しません。以前の予算で運用を続ける場合は、更新前に許可した日額・月額を明示してください。上限を確認・変更するには、[外部解析サービスの設定](ANTHROPIC.md)の `cloud-llm budget` と `cloud-llm providers` を使います。
- 共通設定、導入時の選択、Anthropic・OpenAI・Jevの設定は各施設の `config.json` にまとめます。未使用の提供元の設定も引き継ぎます。旧形式は移行先の更新ツールがサービスの再開前に照合して移行します。移行中の保留や不一致があれば§7で状態を確認してください。
- 更新終了後に認証情報を変更する場合は `~/.local/bin/mcs setup api-keys` を実行します。Jev・Anthropic・OpenAIにはMCSの `.env` を使います。通知先の認証情報は導入方式に応じた保存先を使います。
- 抽出ワーカーは `--lane 1|2|3` の3本です。更新後は§6でサービス定義と起動を確認してください。手動更新では `mcs_setup.py services` で反映します。Hermesは使用中の全プロファイルのgateway、独立実行はhost、LINE WORKSは独立アダプターも確認してください。
- スタンプを送らない環境でMCSアカウントと操作者1人を設定している場合は、`stamps.enabled=false` を明示してください。未完了または結果が不明な操作は `data/stamp_operations` の記録を保持し、照合後に扱います。
- 患者コンテキストの生成と限定閲覧は省略時に有効です。運用方針に合わせて[利用ガイド](USER_GUIDE.md)で設定を確認してください。医薬品辞書を使う場合は、同ガイドの手順で運用者が利用条件を確認して有効にします。
- CSDCの案内は既定で省略します。`mcs setup features` は案内の表示を変更します。専用ジョブの停止には `sdc.enabled=false` を指定します。接続と送信には[監視の登録と同意](SDC_SELF_MONITORING.md)が必要です。

### 古い実行環境からの更新

更新ツールはPython 3.10以上で起動できます。MCS本体と復旧監視にはPython 3.14以上と、WAL-reset問題を修正したSQLiteが必要です。3.14未満のvenvを使っている場合、再導入で旧環境を `<venv>.pre-python314` へ退避して作り直します。自動更新で実行環境の移行を求められた場合は§2から進めます。再導入中に電源断などで停止した場合は `reinstall_incomplete` の手順に従います。

Hermesの固定版はMCS companionのcommit `2c08ca7ade8f867d07e4bff6581be54405b0c9d0` です。認識済みの旧固定版は `--reinstall` で移行します。変更のないPython 3.14対応の独自checkoutを使い続ける場合は、`plan` と `apply` の両方に `--keep-hermes-checkout <40桁のcommit>` を指定してください。checkoutの変更やcommitの不一致があれば計画の段階で停止します。

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
  再導入（install.sh）を伴う場合はbrew・pipの導入で数分〜最大1時間かかる。認識済みの旧固定版Hermesは
  更新ツールの再導入経路で移行し、未知・変更中のcheckoutは停止する。開始前にユーザーへ伝える。
- **更新を実行するのは常に移行先版のコード。** 現在の checkout の更新機能は使わない。§2・§5の起動役は移行先の更新ツールと復旧ヘルパーを取り出します。旧 checkout の更新ツールを直接起動した場合や、旧 SDC owner からの初回適用では、新しいネイティブ再起動経路が使われるとは限りません。

## 1. 対象と Python の特定

| # | 確認 | コマンド | 合格条件 |
|---|---|---|---|
| 1 | checkout | 次の順で最初に見つかったものを使う（以下 `$REPO`）: `sed -n 1p ~/.mcs-recovery/repo_path` → `grep -o "/[^']*/mcs/ops/mcs_cli.py" ~/.local/bin/mcs`（末尾の `/mcs/ops/mcs_cli.py` を除く） → `readlink ~/.hermes/plugins/mcs-discord-commands`（末尾の `/hermes_plugin` を除く） → `git -C ~/.mcs rev-parse --show-toplevel` → `git -C ~/.local/share/hermes-mcs rev-parse --show-toplevel`（`install_local.py` の置き場所） | 候補が `git -C "$REPO" rev-parse --show-toplevel` で同じパスを返す。どれも無ければユーザーに確認する |
| 2 | 導入形態 | `python3 -c "import json;print(json.load(open('$HOME/.mcs/config.json')).get('runtime_mode','hermes'))"` | `hermes` か `standalone` |
| 3 | Python | hermes: `PY=<Hermesホーム>/hermes-agent/venv/bin/python`（既定は`~/.hermes`） / standalone: `PY=~/.mcs/venv/bin/python3`。`"$PY" -V` | 3.10 以上（更新ツールの入口。再導入で 3.14 未満の venv は 3.14 へ移行される） |

素の `python3`（macOS の 3.9 系）で更新処理を実行しない。

## 1-A. 対応する導入済みstandaloneの対話更新

この経路には、対応する更新プログラムと`mcs`コマンドが既に導入されている必要があります。未対応の更新プログラムでは開始せず、導入状態を確認します。既存のcheckout・設定・データを保持し、更新のために新しくcloneしないでください。

1. 計画だけを確認する場合は、次を実行します。取得済みの公開タグを対象にし、タグの追加取得や適用は行いません。対象のタグがない、または状態を確認できない場合は停止します。

   ```sh
   ~/.local/bin/mcs update plan
   ```

2. 更新を進める場合は、次の対話案内を開きます。画面で公開版の選択、更新内容と中断される処理、必要な再導入を順に確認します。

   ```sh
   ~/.local/bin/mcs update
   ```

3. 最後の実行確認で明示的に更新を選びます。Enterだけでは適用しません。再導入が必要な場合は、確認できた以前の導入設定を引き継ぎます。記録・復旧環境・確認後の実行方式・以前の導入設定・更新対象を照合できなければ停止し、未確認のオプションで強制しません。
4. 更新結果を読み、`~/.local/bin/mcs doctor`で残る確認と未検査項目を調べます。次に`~/.local/bin/mcs doctor --probe services`で、明示した常駐処理の追加診断を行い、最後に通知先で応答を確認します。ツールは計画した対象への適用と適用後の再計画を照合しますが、終了0だけで実接続・通知到着・全処理の正常稼働を確認したとは扱いません。実際の収集再開はこれらと分けて確認し、未確認ならそのまま残します。

旧版や未対応の導入済みupdater、外部から取り出した更新ツール、未公開候補は、この新しい経路では更新を開始しません。内部引数を手で追加して拒否を解除しないでください。表示された未対応・不一致・保留の理由を保持し、管理担当者に既存の導入状態の確認を依頼します。更新が中断した場合は[§7](#7-失敗時巻戻し)の状態確認から進み、同じ適用を繰り返しません。Hermes連携の外部bootstrapは次の§2を使います。

## 2. 起動役の取得と計画（書込みなし）

【実行】移行先タグ（ユーザー指定。無ければ最新リリース。以下 `$TAG`）から
起動役を取り出し、計画を出す。`fetch` はタグを取り込むだけで checkout を変えない。

```bash
git -C "$REPO" fetch --tags origin
WORK=$(mktemp -d)        # 本人だけが読める作業dir。共有の /tmp 直下に置かない
git -C "$REPO" show "${TAG}:scripts/mcs_upgrade.py" > "$WORK/mcs_upgrade.py"   # ${TAG}: zshでは "$TAG:s…" が修飾子になる
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" --no-fetch plan --to "$TAG"
```

【検証】JSON が出る。主な項目:

| 項目 | 意味 |
|---|---|
| `route` | `apply`（通常適用）/ `reinstall`（install.sh 再実行を伴う適用）/ `blocked`（§3で解消）/ `noop`（更新済み） |
| `current` / `target` | 現在のタグ・SHA・DB schema / 移行先 |
| `blockers` | 解消が必要なエラートークン |
| `reinstall` | `install_sh_changed`（install.sh 変更）・`standalone_requirements_changed`（独立モード SDK 固定版の変更）・`ocr_requirements_changed`（前回 `--with-yomitoku` を選択し、OCR の依存一覧・固定版一覧・導入スクリプト・モデルの定義のいずれかが変わった場合） |
| `schema_bump` | DB の版変更。あれば事前バックアップからの復元が巻戻しに含まれる |
| `restarts` | 更新後に必要な再起動（`gateway`・`lineworks_adapter`） |
| `recovery_tool_changed` | 復旧 watchdog の変更。`reinstall` 経路なら自動で反映（`--no-recovery` を付けた場合を除く）。`apply` 経路では適用後に、承認を得て `./install.sh <opt-out> --no-services` を再実行する |
| `install_args` | 前回の導入で省略した項目。設定の集約後は `~/.mcs/config.json` から読み取る。移行前だけ旧 `data/install_options.json` と照合する。`null` は未記録、読取不能、形式不正、または移行前の記録の不一致 |
| `notes` | 現在版より新しく移行先までの各版の「更新時の注意」（CHANGELOG） |

1.0.13以降、macOS 標準 `/usr/bin/python3` の SQLite は WAL-reset 影響版のため、
`update apply`・`services` は復旧 watchdog の実行 Python が安全と確認できるまで停止する
（`plan` は merge 前に `recovery_runtime:` を blockers に出し、手順を `notes` に示す）。
オーナー承認のうえ、checkout 外にある既存の安全な独立 Python の絶対パスを `recovery_python` として保存する。
1.0.13 以降が入っていれば `mcs setup init --yes --recovery-python <path>` と `mcs setup services` で反映する。
1.0.12 以前から更新する場合は init にこのオプションが無いため、`~/.mcs/config.json` に
`"recovery_python": "<path>"` を手で追記してから `plan` を再実行する。所有済みで停止中の watchdog は
merge 後の `services` が置き換える。`reinstall` 経路では `--install-arg=--no-recovery` を付けて apply する
（install.sh は不一致の watchdog を置き換えずに停止するため）。ただし、提供元設定の保存形式を変更する際に旧い復旧監視が入っている場合は §3-A の手順を優先する。自動で別 Python へは切り替えない。

`${TAG}:scripts/mcs_upgrade.py` が無い（v1.0.10 以前を移行先にした）場合は、その版へはこの手順で更新できない。

### 2-A. 公開前の固定候補をこのMacへ適用する場合

運用者が「本番で動作を確認してから公開する」と承認した場合は、`--candidate-sha` で未公開のローカル候補を指定できます。タグ・GitHub Releaseは作成しません。候補の40桁SHAと、レビュー時点の本番HEADの40桁SHAを固定し、変更をコミットしてから使います。branch名・省略SHA・tag object・tree・blobは指定できません。候補は本番checkoutのローカルGit objectとして存在し、本番HEADの子孫である必要があります。

```bash
CANDIDATE_SHA='<レビュー済み候補の40桁SHA>'
BASE_SHA='<レビュー時点の本番HEADの40桁SHA>'
WORK=$(mktemp -d)
git -C "$REPO" show "${CANDIDATE_SHA}:scripts/mcs_upgrade.py" > "$WORK/mcs_upgrade.py"
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" plan \
  --candidate-sha "$CANDIDATE_SHA" --base-sha "$BASE_SHA"
```

release owner を使わない通常更新で、提供元別設定に未移行の既存環境では、まず橋渡し版 `efd6df5a8870fc15aaf9ce749759f1b02a5f67ff`、続けて今回の候補を適用します。どちらもこの手順の更新プログラムを使い、それぞれの更新開始時点のHEADと導入引数を指定します。移行元を確認できない場合や、処理の停止・ロックを確認できない場合は、設定を分離せず停止します。旧版の3ファイルを使っている場合は、提供元ごとの内容と確定記録を照合したうえで、各施設の `config.json` にまとめます。移行後の設定変更には設定画面またはCLIを使います。承認待ちや送信結果不明の記録は解除しません。

v1.2.x 以前の版から更新する場合は、直接この候補を適用せず、まず v1.3.0 を適用して正常に終わることを確認してから続けてください。v1.2.x 以前の更新処理は設定の版3の記録を扱えず、直接の適用は移行元を検証できないため変更前に中止されます。

`plan`の`target.kind`は`local_candidate`、`target.tag`は`null`です。`blockers`が空になるまで§3の手順で対処します。候補に`install.sh`等の変更があれば、公開版と同じく`route=reinstall`になります。前回の導入内容とopt-outは§4で確認し、そのまま引き継いでください。復旧watchdogを導入済みなら省略しません。

明示承認後、`route=apply`なら次を実行します。`route=reinstall`では同じコマンドに`--reinstall`と、§4で確認した各`--install-arg=...`を追加します。理由には秘密値・患者情報を含めず、200字以内で記載してください。

```bash
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" apply \
  --candidate-sha "$CANDIDATE_SHA" --base-sha "$BASE_SHA" \
  --confirm-human --reason 'レビュー済み候補を本番で確認するため'
```

候補の取得・照合では通信しません。再導入を伴う場合の依存取得などは、通常の再導入と同じです。`--to`・`--tag`・`--sha`・`--command-id`との併用、自動更新・チャット承認からの候補指定には対応しません。`--confirm-human`はこのローカル適用への承認であり、DB復元の人承認を代行しません。

候補でも、保護path・設定・schema・更新前HEAD・fast-forwardの確認、バックアップ、update/run lock、更新記録、worker停止、新コードによるサービス同期・再起動・適用後確認は同じ更新処理を通ります。未公開候補と本人確認・理由を更新記録へ保存し、失敗・中断・rollbackは§7の既存手順で扱います。保留中の復元承認や送信結果不明は解除しません。

本番checkoutのmainを先に通常の`git merge`で進めると、その時点で稼働コードが置き換わります。開発worktreeでmainを祖先に含む候補を作り、更新処理がworkerを停止してからmainを進めてください。適用後は§6の稼働確認を行い、実際の動作を確認してから公開可否を判断します。

本番適用後はしばらく運用して確認し、見つかった不具合を修正してからタグ付け・GitHub Release・pushへ進みます。直後の正常な収集や合成テストだけで、運用中の確認が済んだとは扱いません。

後で公開するタグは、検証済みの候補そのもの、または修正を含む子孫コミットを指す必要があります。適用済みのコミットはsquash・rebase・amendで書き換えないでください。祖先を含まない版へは`not_fast_forward`で更新が止まります。別系統へ切り替える場合は直接resetせず、§7の巻戻し条件と承認を確認してから、既存の`rollback`で更新前のコミットへ戻します。候補の運用を続けるだけなら巻戻しは不要です。

## 3. 阻害要因の解消

`blockers` ごとに対処し、§2 の `plan` を再実行して `blockers` が空になるまで繰り返す。

| トークン | 対処 |
|---|---|
| `tree_dirty` | `git -C "$REPO" status --short` を示し、**【ユーザー確認】**。他の作業者の変更の可能性がある。勝手に退避・破棄しない |
| `config: …` / `new_config: …` | 現在 / 移行先の設定検証エラー。キーと理由を示し、承認後に `"$PY" "$REPO/mcs/ops/mcs_setup.py" init --set KEY=JSON` で直す（秘密値は扱わない） |
| `update_in_progress_or_interrupted` | 前回の更新が途中。`"$PY" "$REPO/mcs/ops/mcs_update.py" status` を確認し、§7の移行先版の更新ツールで復旧してから再計画 |
| `runtime_migration_pending`（復旧レポート） | Pythonや接続用ライブラリの移行が途中。定期の復旧ツールは環境・更新記録を保持して停止する。通常の復旧を繰り返さず、§7の移行先版の更新ツールを確認する |
| `restore_consent_pending` | DB 復元の人承認待ち。[バックアップ・復元ガイド §8](BACKUP.md#consent) の損失報告・バックアップhashに束縛された復元承認手順をユーザーが行う |
| `hermes_not_resolvable` / `standalone_interpreter_unavailable` | 実行基盤が無い。`./install.sh --preflight` の `対処:` を確認しユーザーと対処 |
| `insufficient_disk` | DBの2倍に、64MiBと設定された空き容量下限の大きい方を加えた空きが必要。下限の既定値は512MiB。原本と必要なバックアップを保護して容量を確保 |
| `homecare_backup_disk_low` | MCS 訪問薬剤管理の台帳（`data/homecare.db`）の更新前コピーの空きが足りない。台帳の2倍に空き容量下限を加えた空きが必要。原本と必要なバックアップを保護して容量を確保。コードは切り替わらないが、原本DBの更新前コピーは先に作られて残る |
| `not_fast_forward: …` | checkout が移行先の祖先でない（ローカルコミットや新しすぎる版）。**【ユーザー確認】** |
| `untracked_collision: …` | 未追跡ファイルが移行先の追跡ファイルと衝突。内容を示し **【ユーザー確認】** |
| `schema_downgrade:…` | 移行先が古い。対象タグを見直す |
| `schema_bump_auto_blocked` | `update.mode=auto` では schema 変更を適用しない。ユーザーと方針を決める（`notify` へ変更等） |
| `standalone_external_apply` | 独立モードは host 経由で更新する（[STANDALONE.md](STANDALONE.md#更新復旧)）。`reinstall` がある場合は同書の再導入手順 |
| `legacy_source_manual` | v1.0.0〜1.0.2 からの更新。§8 の手動経路へ |
| `reinstall_incomplete` | 前回の再導入が install.sh の途中で中断した。`status` を示し **【ユーザー確認】** の上で `cd "$REPO" && ./install.sh <前回と同じ opt-out（plan の install_args）> --no-services` → `"$PY" mcs/ops/mcs_setup.py services` → `"$PY" mcs/ops/mcs_update.py reinstall-done`（HEAD が再導入した版のときだけ完了を記録し、残りの後処理を復旧処理で完了）→ `check`。復旧処理は install.sh を無人で再実行しない |
| `reinstall_requires_upgrade_tool` | 1.0.29 以前の `mcs/ops/mcs_update.py` を直接実行して再導入を指定したため、更新の排他制御を引き継げず、再導入を始める前に止めて元の版への復元を試みた。`status` と復旧の結果を確認し、復元できていれば §7 の移行先版の更新ツール（`scripts/mcs_upgrade.py apply --reinstall`）で再実行する |
| `install_options_unverified` | 承認後の変更か、古い更新記録に照合の根拠がないため、再導入を開始していない。記録を削除せず、現在の状態と導入内容を担当者に確認してもらう |
| `recovery_incomplete`（復旧レポート） | 抽出処理の起動確認が未完了。中断記録を保持するため、起動失敗の原因を修復して `recover` を再実行する。同じ復旧の再試行では起動済み処理を停止し直さない |
| `update_state_corrupt` | `data/update_state.json` が読めない。内容を変えずに報告し、ユーザー判断を待つ |
| `tag_not_fetched` | `git fetch --tags origin` が失敗している。ネットワーク・タグ名を確認 |
| `validate_config_failed` / `preflight_failed: …` | 現在 / 移行先コードで設定検証が実行できない。出力を報告 |
| `standalone_runtime_missing_in_target` | 移行先が独立モード非対応。対象タグを見直す |
| `plan_failed: …` | 計画中の検査が失敗した。内容を報告し再試行の可否をユーザーと決める |
| その他（`protected_path`・`unsafe_path`・`ignored_path_tracked` 等） | 移行先タグの異常。適用せず報告 |

### 3-A. 提供元設定の保存形式が変わるときの復旧監視

この手順は、旧形式から初めて移行する施設と、提供元別の3ファイルを1つの `config.json` に集約する施設のうち、旧い `org.mcs.recovery` が入っているMacに適用します。旧い復旧監視は集約途中の状態を判定できません。更新には§2の手順で移行先から取り出した `scripts/mcs_upgrade.py` を使います。`--installed-standalone` は旧版の更新処理を使うため指定せず、代わりに移行先の更新プログラムを使う `--target-standalone` を `plan`・`apply`・`guide` に付けてください。通常の `mcs update` ではこのオプションを使いません。移行後の完了確認で保留された場合は、同じ更新を再実行する前に、移行先の `mcs_update.py recover` で更新記録を確認します。

1. 初めて移行する場合は、橋渡し版 B を通常の更新経路で適用し、HEAD・更新記録・稼働状態を読み直して B の適用を確認します。すでに3ファイルへ分離済みの場合は、更新計画で現在の版が移行元として検証済みであることを確認します。この手順ではその版を B とします。検証できない版では停止します。続く候補 C の `plan` と事前検査のエラーは解消し、無視して進めません。
2. C を適用する前に、既存の `org.mcs.recovery` の所有先と状態を確認し、実行中でないことを確かめた上で、この監視だけを計画的に停止します。対象の停止には `launchctl bootout "gui/$(id -u)/org.mcs.recovery"` を使います。失敗した場合や停止を確認できない場合は更新を始めません。
3. C は通常の `reinstall` 経路で適用し、復旧機能を `--no-recovery` で省略しません。再導入が補助4ファイルの検証済み世代を先に配置し、同じ入口のbytesを原子的に公開して監視を起動・再起動します。適用後は入口hashの一致、補助4ファイルを含む全世代の読込み、`org.mcs.recovery`の読込状態をそれぞれ確認します。入口hashの一致だけでは正常完了と扱いません。

```bash
shasum -a 256 "$REPO/deployment/recovery/mcs_recover.py" "$HOME/.mcs-recovery/mcs_recover.py"
RECOVERY_PYTHON='<記録済みrecovery_pythonの絶対パス>'
"$RECOVERY_PYTHON" -I -B "$HOME/.mcs-recovery/mcs_recover.py" --help
launchctl print "gui/$(id -u)/org.mcs.recovery"
```

`--help`も入口のtop-levelで補助4ファイルのhashを照合して読み込んだ後に実行されます。正常終了を確認してください。この確認で復旧処理は実行しません。`.prev`を保持する場合は、その入口が指す`runtime-<世代hash>`も保持します。入口だけのコピーや補助ファイルの混在で修復せず、正規の再導入経路を使います。

C の適用がコード切替前に失敗した場合は、HEAD が B で、未完了の更新記録がなく、状態を確認できる場合だけ旧い監視を再開できます。コード切替後または切替状態が不明な場合は旧い監視を再開せず、移行先版の正規 `recover` または検証済みの新しい復旧ツールで §7 の保留・復旧手順に従ってください。この順序の実機適用は未確認です。

## 4. ユーザー確認

次をまとめて提示し、適用の承認を得る（既に同じ内容で承認済みなら聞き直さない）。

- 現在版→移行先、`route`、停止の見込み時間
- `notes` の要約（版ごと。必要な追加操作を明示）
- `reinstall` の場合: install.sh の再実行と、使う opt-out。`plan` の `install_args` が配列なら
  その opt-out を使い、確認は不要（空配列は opt-out なし）。`null` のときは記録の欠落・不正・
  不一致を区別できないため、自動更新と再導入を停止し、担当者に確認を依頼する。
  旧記録と中央設定の両方に省略項目がないと確認できた場合は、ユーザーに
  `--no-llm`（自前 LLM サーバ）・`--no-brew`・`--no-plugin`・`--no-recovery` のどれを使っていたか確認する
  （`--mode` は不要。設定から自動判定）。`~/.mcs-recovery/` があるなら復旧機能は導入済みなので、
  `install_args` や回答に `--no-recovery` があっても除き、`notes` が求める場合だけ付ける
  （付けると復旧ツールが古いまま残る）。
  新しい選択は利用者が install.sh を直接実行して成功したときに記録する。standalone では
  明示指定の有無にかかわらず `--no-plugin` を記録する。通常の更新では、
  検証済みの旧記録があり中央設定に記録がなければ、run.lock を取得後、mergeの前にその値を中央設定へ写す。
  既存の更新条件を満たす場合、最新版であっても移行が必要なら `plan` は `settings_migration: required` と表示し、確認後の更新で写す。
  認証情報と保存データは現在の保存先に保持し、DBの形式変更は既存のバックアップ・移行手順で扱う。
  Python 移行や修復のために足した opt-out は記録しない。
  旧記録はそのまま保持され、有効な旧記録と異なる opt-out を保存する直接導入は停止する。
  更新が中断・失敗した場合も、同じ値を写した設定は残る場合がある。
  旧watchdogが `interrupted_pre_merge` と報告しても、設定へのコピーが行われなかった証拠にはならない。
  保存後の同期に失敗した場合は結果を確定できないため、旧記録と設定を保持して担当者に確認を依頼する。
  設定を写す前に `config.json` と同じフォルダーへ `.install-options-migration.pending` を排他的に作成して同期し、保存と照合が終わった場合に取り除く。
  この記録が残っている場合や設定・`data` のフォルダーの安全性を確認できない場合、新版の導入・更新・復旧は停止する。
  設定と記録は削除・編集しない。移行先の検証済みタグを `$TAG` に設定し、
  そのタグの更新ツールで状態を確認する。表示に設定値や秘密値は含まれない。
  ```bash
  WORK="$(mktemp -d)"
  git -C "$REPO" show "refs/tags/$TAG:scripts/mcs_upgrade.py" > "$WORK/mcs_upgrade.py" &&
    "$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" --no-fetch reconcile-settings --to "$TAG"
  ```
  状態を確認できた場合、担当者が復旧を判断し、上の最後のコマンドに
  `--confirm-human --reason '確認理由'` を加えて実行できる。理由には患者情報や秘密値を含めない。
  設定へのコピー後は同じ内容を同期し直し、確認記録を残してから保留を解除する。
  旧形式の保留記録、旧記録や保存先の不一致、release更新の保留などは自動解除できない。
  その場合や保存結果が確認できない場合は、状態を保持し担当者に確認を依頼する。
  設定・`data` のフォルダーは本人の所有で、グループと他のユーザーから書き込めない必要がある。ACL はこの確認の対象ではない。
  旧watchdogや旧updaterはこの記録を理解しないため、旧版の起動による操作を防げない。
  release-owned 経路は承認時の設定を変えないため、この記録の移行が必要または確認不能な場合は停止する。
  owner の計画時と適用開始前にも確認する。開始前に検出した場合は適用開始記録を作らない。
  中央の grant が発行された後に検出した場合は、既存の承認・記録を保持して担当者に確認を依頼する。
  開始後の保留は通常の更新・復旧で解除できない。状態ファイルを削除しない。
  対話で確認した導入設定は適用前と再導入前に照合し、変更された場合は停止する。
  手動で指定した再導入引数は記録値と別に扱う。古い更新記録に照合の根拠がない場合、
  自動再導入を停止して中断した更新の復旧手順を案内する。
  旧版は中央の記録を参照しないため、旧版へ戻すときは導入内容と旧記録を確認する。
  旧記録がなく `null` の場合、対話式の自動更新は再導入を開始しない。
  更新前から開いていた旧版の手動設定は共通ロックを使わないため、移行後に中央設定を上書きする場合がある。
  旧記録は保持され、中央の項目が失われた場合は次の更新で照合して再度写す。
- `schema_bump` の有無、`restarts`

## 5. 適用

【実行】

```bash
# route=apply
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" --no-fetch apply --to "$TAG"
# route=reinstall（install_args の各要素、または確認した opt-out を --install-arg=… で付ける）
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" --no-fetch apply --to "$TAG" --reinstall
```

上の再導入例は opt-out なしの場合。§4 で確定した opt-out があるときだけ、各要素を
`--install-arg=…` として追加する。`--no-llm` は自前 LLM サーバー・モデルの導入を省略する指定で、
既存の LLM 処理を停止する設定ではない。

`--no-fetch` は起動役の事前取得を省くだけで、適用自体はタグの照合のためネットワークを使う。
適用は移行先版の `mcs_update.apply` が行う: 更新ロック取得 → 検査 → `data/backups/preupdate-*.db` 取得 →
実行ロック → 常駐処理の停止 → `git merge --ff-only` → 新コードで（再導入 →）サービス再同期 → 再起動 →
DB を開いて移行（加法 migration）→ 事後検証。失敗時は自動で元の SHA へ戻し、schema 変更があれば DB を復元する
（復元前の損失報告に人承認が必要な場合は保留になる）。移行記録にあるHermesのコードとvenvは退避から戻せるが、brewなど再導入のその他の副作用は巻き戻らない。

【検証】終了コード 0。0 以外は §7。

## 6. 後処理と検証

SlackカードとSlackのテキスト通知のどちらも、通常の更新経路が患者情報先を保持したまま
運用ログ先を準備します。運用先が未設定または患者情報先と同一なら、非公開の`mcslog`を補完します。
明示済みの別の運用先は保持します。テキスト専用の`interactive=off`も維持します。
新規作成には、Slackアプリの接続情報・`groups:write`と`groups:read`権限・招待先の操作者IDが必要です。
権限不足・名前の競合・作成結果不明などで止まった場合は、案内された条件を確認してから
`mcs setup slack-channels`で準備を再開してください。`install.sh --no-services`で省略した準備も同じコマンドで再開できます。
不明な作成・招待を繰り返したり、運用先を患者情報先へ戻したりしません。
担当者がSlackで同名のチャンネルの有無を確かめた後は、`mcs setup slack-channels --discard-hold --request-id <記録のrequest_id> --confirm-human --reason <理由>`で記録を退避できます。記録は`data/slack-channel-setup.discarded-<request_id>.json`として残り、承認記録も作られます。作られていたチャンネルを使う場合は、そのIDを通知先に設定してから準備を再開してください。
設定の保存と更新後の接続を確認します。カードを使う構成では通常の通知workerの起動確認も必要です。

1. `restarts` に `gateway` があり Hermes モードなら、apply は再起動を依頼します。`data/gateway_restart.json` が `supervisor_restart_verified` になるまでは再起動未完了として扱い、`hermes gateway status` で稼働も確認してください。`requested`・`failed`・`unknown` は反映の証拠ではありません。Hermes の版によっては、実行中の処理を理由に再起動を拒否しません。再起動を確認できない間は、旧 worker が新しい通知形式を処理する可能性があります。処理状況を確認し、安全に再起動できる時点で管理担当者が対応してください。
2. `lineworks_adapter` があれば、[LINEWORKS.md](LINEWORKS.md) の手順で独立アダプターを再起動し `python -m lineworks_adapter check`。1.0.15では自動更新・巻戻し・復旧後の再起動を追加しましたが、この版への初回更新後も明示的な再起動と反映確認を行います。
3. `notes` にある追加操作（承認済みのもの）を実行する。`apply` 経路で `recovery_tool_changed` が true なら §2 の表のとおり install.sh を再実行する。
4. 使用中のgatewayまたは独立host・通知アダプターと抽出ワーカー3本について、更新後に起動したプロセスであることと稼働状態を確認します。古いworkerが新しい通知形式を保留している場合、保存ファイルを削除して再送させず、更新したworkerで通常の配送を再開します。
   Hermes の Slack では、カードを送るのは plugin settings の `slack_profile` の profile だけです。複数 profile の gateway で他の profile の接続が見送られた場合は、イベントログ `hermes.plugin.mcs_slack` に `worker_disabled`（`reason=profile_mismatch`）が出ます。これは正常な動作です。`channel_not_found` だけを根拠に再送しません。成否不明（`unknown`）や再送上限（`resend_exhausted`）の配送を、保存データの書換えや強制再送で解除しません。
5. 検証:

以下は公開タグの確認例です。§2-Aの未公開候補では、`git -C "$REPO" rev-parse HEAD` と `status` の `current_sha`・`state.applied[-1].sha` が候補SHAに一致し、`state.applied[-1].target_kind=local_candidate`、`state.applying=null` であることを確認します。
再planでは `--to "$TAG"` の代わりに `--candidate-sha "$CANDIDATE_SHA" --base-sha <適用後HEADの40桁SHA>` を指定します。更新前の `BASE_SHA` を再利用すると `base_sha_mismatch` で止まるため、その結果だけで更新失敗と判断せず、適用後のHEADで再計画してください。

```bash
"$PY" "$REPO/mcs/ops/mcs_update.py" status     # applied の最後が $TAG、applying が null
"$PY" "$REPO/mcs/ops/mcs_setup.py" check       # exit 0
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" --no-fetch plan --to "$TAG"   # route=noop
rm -r "$WORK"
```

## 7. 失敗時・巻戻し

施設設定の集約が更新中に止まった場合は、`"$PY" "$REPO/mcs/ops/mcs_update.py" recover` で復旧を再確認します。更新記録・移行元・ロック・処理の停止状態を確認でき、条件がそろった場合に移行を再開します。再導入が未完了の場合は、§3の `reinstall_incomplete` に従って手動で完了し、`"$PY" "$REPO/mcs/ops/mcs_update.py" reinstall-done` で完了を記録して復旧を再確認します。

自動処理と、§3-Aで配置と起動を確認した新しい独立復旧監視は、未検証の設定移行を進めず保留します。設定がpendingの場合、移行元や停止状態を確認できない場合、再導入が未完了の場合も記録と保留を維持します。markerや記録を削除して迂回しないでください。

提供元別設定を分離済みの導入先では、独立復旧監視は次の条件をすべて確認できた場合だけ復旧を進めます。

- 書込み中・初回導入中の記録がない。
- 提供元設定の確定記録が正しい形式で、その版の保存形式と内容が一致する。集約後は `config.json` の共通設定と `provider_settings` の各提供元の値を照合する。
- 現在の版が提供元別設定の読取りコードを持ち、プログラムに変更・統合途中の状態・追跡外のコードがない。

確定済みの設定が残すバックアップと作業用フォルダーは、通常の状態として扱います。条件を満たさない場合と巻戻しの継続は監視では保留し、`mcs_update.py recover`で復旧します。中断した更新がない場合は、提供元設定がどの状態でもその設定を理由に保留せず終了します。ただし、導入設定の移行結果を確認できない場合、更新記録（`update_state.json`）が壊れている場合、復元後の照合が終わっていない場合、リリース管理の記録がある場合は、提供元設定を確かめる前に保留します。監視は保留の理由を`data/recovery_report.json`に記録し、同じ保留が続く間は書き直しません。定期監視は、更新記録が30分以内に書かれている間は実行中の更新とみなし、提供元設定の状態と導入設定の移行結果による保留では理由を表示するだけで記録しません。導入先フォルダー自体を確認できない場合も記録しません。記録された保留は`mcs_setup.py check`が警告します。導入済みの復旧監視にこの判定を反映するには、§2の表に従って復旧ツール本体を再導入してください。

Pythonや接続用ライブラリの移行中に止まった場合は、**移行先版の**`mcs/ops/mcs_update.py recover`で退避した環境を戻します。通常の`mcs recover`や定期の復旧監視は、この移行を進めず停止します。
管理担当者は、記録された安全な独立Python（`recovery_python`）と更新記録の移行先SHAを確認してください。MCSのHEADがそのSHAと一致する場合は、そのPythonで`"$REPO/mcs/ops/mcs_update.py" recover`を実行します。HEADが異なる場合や対象コードがない場合は、旧版の更新ツールを代わりに起動せず、記録を保持して管理担当者が移行先コードによる復旧方法を確認します。壊れたvenvのPythonや自動選択した別Pythonは使いません。
Hermesのコードが更新開始時・移行先のどちらとも違う場合、ブランチ上や変更中の場合、移行記録が空・不正な場合も、環境や退避を動かす前に停止します。既存の`--keep-hermes-checkout`を明示した環境は、記録したcommitから変わっておらず変更もない場合に限り、ブランチ上でも保持できます。変更を破棄したり記録を削除したりして回避しないでください。
1.0.24以前の更新ツールが作った古い移行記録が残っている場合も、移行先の版を記録から確認できず停止することがあります。管理担当者が元の更新対象と退避を確認してください。

- apply が非0: `status` の `attempts[$TAG].detail` を報告する。自動巻戻し済みなら checkout は元の SHA。
  `rollback failed — escalate` の場合は追加操作をせず報告する（復旧 watchdog と人の判断に委ねる）。
  schema 変更を伴う更新で旧版の checkout へ戻した後なら、DB 復元が完了するまで抽出処理と更新 marker を止めたままにする（通知は復旧レポートで行う）。
  以後の `recover` も、復元の保留markerや未完了記録が残る間は処理を強制再開しない。独立した復旧監視のレポートに `writers_stopped` がない場合、保留だけでwriterの停止を確認済みとは扱わず、実際のプロセス状態を確認する。
  使えるバックアップと復元承認がそろい、復元後の照合も完了した場合に通常の再開に戻る。
  `restore_reconcile_schema_blocked` は、復元したDBを読込済みの新版writerで開くと再移行してしまうため、照合と再開を停止した状態です。
  DBはバックアップのschemaを保ち、復元済みmarkerと`restore_incomplete`を残します。追加の承認では解除されません。
  独立した復旧監視の`restore_reconcile_pending`も、DBのコピー後に通知照合が未完了であることを示します。更新journalが空・不在でも、更新・復旧処理の復元済みmarkerが残っていれば保留を維持します。
  旧schemaで通知を自動再照合できないため、管理担当者が適合する版・復旧Python・通知の照合方法を確認するまで、markerやjournalを削除せず収集・通知を強制再開しないでください。
  独立した復旧ツールは、復元DBと実際に再開するcleanなcheckoutのschemaが一致し、更新記録・HEAD・正規のschema定義を確認できる場合に限り、既存の `mcs recover` でwriterを再開します。
  配送の保留markerは残し、再開したrunnerが既存の通知記録照合を完了するまで通知を許可しません。版が不明・不一致、記録や定義を読めない場合、および復元の承認待ちは保留を維持します。適合するcheckoutの確認後に同じ `mcs recover` で再確認し、marker削除や追加承認で迂回しないでください。
- 適用後に問題が見つかり戻す場合（ユーザー承認後）: `"$PY" "$REPO/mcs/ops/mcs_update.py" rollback`。
  schema 変更があった更新は DB 復元を伴い、損失報告への人承認を求められることがある。
- `rollback_blocked_layout_floor` は保存済みカード形式を戻り先が扱えないことを示します。自動巻戻し・中断復旧にも同じ下限を適用し、旧コードへの切替前に拒否します。1.0.23から旧版へ戻すには、必要な損失確認と人承認を経て更新前backupを復元してください。復元holdは維持し、通常収集や通知を手動で強制再開しません。
- install.sh が失敗した場合は出力末尾の `要対処` / `対処:` を示し、ユーザーと対処してから §2 からやり直す。

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

## 更新・復旧の日本語表示

更新ランチャーと更新管理コマンドのヘルプ、対処案内、更新の影響説明は日本語です。
`mcs update --help` で操作を確認できます。コマンド名・引数・終了コードと
計画/状態のJSONは自動処理との互換性のため維持しています。JSON内の状態コードは翻訳しません。

復旧ツールの使い方だけを確認するときは、
`python3 ~/.mcs-recovery/mcs_recover.py --help` を使います。
`--help` / `-h` は復旧を実行しません。不明な引数もエラーで終了し、復旧を開始しません。
`--status` は引き続き機械処理用JSONを返します。引数なしの実行は復旧処理なので、
ヘルプの代わりに実行しないでください。配備済みの復旧コピーは通常の更新・同期後に反映されます。
OSや外部コマンドの技術的な詳細には英語が含まれることがあります。
