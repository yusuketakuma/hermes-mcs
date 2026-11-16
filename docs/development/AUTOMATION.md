# GitHub 上の AI 実装の運用

所有者が Issue に担当を付けると、AI が下書きPRを作り、CI とマージ判定ゲートが検証します。
マージは原則として所有者が行います。自動マージは既定で無効です。
この文書は、担当の決め方、止まる条件、全体の止め方を定めます。

## 役割と入口

| 担当ラベル | 実装するもの | 起動のしかた | ブランチ |
| --- | --- | --- | --- |
| `担当:claude` | Claude Code GitHub Action（`.github/workflows/agent-claude.yml`） | 所有者がラベルを付ける。担当中の Issue・PR で所有者が `@claude` と書く | `agent/claude-…` |
| `担当:copilot` | Copilot cloud agent | 所有者が Issue を Copilot に割り当てる | `copilot/…` |
| `担当:dots` | Dots（Codex のクラウド環境を含む） | Dots が所有者の指示で着手する | `codex/…` |

- 1つの Issue に付く担当ラベルは1件だけです。2件目は `agent-control.yml` が外します。所有者以外が付けた担当ラベルも外します。
- 担当を交代するときは、旧担当の実行（Actions の run、Copilot のセッション、Codex のタスク）が終わったことを確かめてから、旧ラベルを外して新ラベルを付けます。タイムアウトしただけでは終わったとみなしません。
- AI は同じブランチに並行して書き込みません。Copilot の Agent Merge は使いません。
- 外部の Issue・コメントは要望と証拠です。それだけで AI は起動しません。
- AI は担当ラベルが、その Issue の唯一の担当であることを起動時に確かめます。2件目のラベルで起動した実行は何もせず終わります。
- 担当ブランチの判定は、このリポジトリに push されたブランチだけを対象にします。fork のブランチは名前が同じでも AI の作業として扱いません。

## 相談モード（実装の前に Issue を詰める）

所有者が Issue にコメントすると、AI が読み取り専用でコードを調べ、Issue へのコメントで答えます（`.github/workflows/agent-consult.yml`）。
push・ブランチ・PR・ラベルの変更はしません。使うトークンは `contents: read` で、書けるのは Issue のコメントだけです。

| 書き方 | 相手 | 条件 |
| --- | --- | --- |
| `@claude …` | Claude（`CLAUDE_CODE_OAUTH_TOKEN`、Claude の利用枠） | その Issue に `担当:claude` が無いとき。付いていれば実装の指示になります |
| `/codex …` | Codex（`openai/codex-action`、`OPENAI_API_KEY` の従量課金） | いつでも。Codex はネットワークなし・読み取り専用の sandbox で動きます |

内容が固まったら、担当ラベルを付けて実装に進めます。Codex の呼び出しに `@codex` を使わないのは、Codex の GitHub 連携（レビュー）と衝突させないためです。
相談のやり取りも公開されます。実データ・実ログは書きません。

## マージ判定ゲート（`merge-gate`）

`agent-control.yml` が CI 完了のたびに、PR の現在の head に `merge-gate` の commit status を出します。
判定は `ci/agent_control.py` が行い、main 側の定義だけを使います。

- `lint-test`・`hygiene`・`standalone-sdk`・`incident-gates`・`macos`・`hermes-integration` が、現在の head で、GitHub Actions によって実行され `success` であること。
  `skipped`・`neutral`・欠落は不合格です（GitHub の必須チェックはこれらを合格として扱うため）。
- `pytest-linux`・`pytest-macos` があれば、それも `success` であること。文書だけの変更での `pytest-macos` の省略だけは認めます。
- 停止スイッチが切れている間は、AI ブランチの PR を不合格にします。
- 下書きの PR は保留にします。

合格でも、次を含む PR は「要人間承認」として説明に理由を書き、自動マージの対象から外します。

- `.github/`・`ci/`・`scripts/`・`Makefile`・`pyproject.toml`・`conftest.py`・`CLAUDE.md`・`AGENTS.md`・`SECURITY.md`
- `hermes_plugin/`・`adapters/`・`lineworks_adapter/`・`mcs_standalone/`・`deployment/`
- テストファイルの削除・改名、skip・xfail の追加、テスト関数の純減

PR に足されたワークフローは、GitHub Actions 名義で `merge-gate` を書けます。そのため `.github/` を含む PR は必ず人がマージし、
自動マージの予約は push のたびに外します。`merge-gate` が緑でも、`.github/` を含む PR は差分を見て判断してください。
自動マージの予約は、判定した head のコミットに結び付けます（`--match-head-commit`）。判定後に push された head には引き継ぎません。

## 試行回数

AI ブランチ（`agent/`・`copilot/`・`codex/`）の CI が失敗すると、PR 本文の `Fixes #番号` などで結ばれた Issue の `試行:N` を1つ進めます。
担当をまたいで通算し、3回目で担当ラベルを外して `要オーナー対応` にします。
`agent-control.yml` 自体を再実行しても数えません（CI を再実行して失敗したときは数えます）。
同じ Issue を結ぶ2本の PR がほぼ同時に失敗すると、1回分しか数えないことがあります。1 Issue 1担当の運用ではまれなので、許容しています。

失敗の分類は機械的に決められないので、Dots か所有者がラベルで付けます。再試行してよいのは `失敗:コード` だけです。
`失敗:CI基盤`・`失敗:利用枠`・`失敗:不明` は保留し、コードを変えたり、従量課金の経路へ切り替えたりしません。

### CI 失敗の自動修正（Claude 担当だけ）

`agent/claude-…` の PR の CI が失敗し、結ばれた Issue が次をすべて満たすと、`agent-control.yml` が
`agent-claude-repair.yml`（`workflow_dispatch` だけで起動、入力 `pr`）を起動します。

- `担当:claude` が付いている。`要オーナー対応`・`失敗:CI基盤`・`失敗:利用枠`・`失敗:不明` が付いていない。
- `試行:N` が上限に達していない（3回目の失敗では起動せず、担当を外します）。
- `AUTOMATION_ENABLED` が `true`。

修正モードの Claude は失敗したログを読み、同じブランチを直して push します。push で CI が再び走り、結果は同じ判定に戻ります。
基盤・利用枠・原因不明と判断したときは、コードを変えずに PR へ一度だけコメントします。
Copilot と Dots の PR は自動では再起動しません（Copilot は所有者が PR で `@copilot` と書きます）。
同じブランチの書き手を1つに保つため、CodeRabbit などレビューツールの自動修正（Autofix）は使いません。

## 停止スイッチ

リポジトリ変数 `AUTOMATION_ENABLED` が `true` のときだけ AI が起動し、AI ブランチの PR がゲートを通ります。
変数が無い・`true` 以外なら停止です。自動マージは、さらに `AUTO_MERGE_ENABLED` が `true` のときだけ動きます。

全体を止める手順です。上から順に行います。

```bash
R=yusuketakuma/hermes-mcs
gh variable set AUTOMATION_ENABLED --body false --repo $R
gh variable set AUTO_MERGE_ENABLED --body false --repo $R
gh pr list --repo $R --search "is:open auto-merge:enabled" --json number --jq '.[].number' |
  xargs -r -n1 gh pr merge --disable-auto --repo $R
```

- Copilot: 担当中の Issue の割当を外し、実行中のセッションを止めます。
- Claude: 必要なら Claude GitHub App を一時停止します（Settings → GitHub Apps → Configure → Suspend）。
- Dots: Dots を一時停止したうえで、Scheduled の定期タスクと、委任した Codex のタスクを個別に取り消します。Dots の一時停止だけでは止まりません。

停止中に遅れて届いた push はゲートを通らないので、マージされません。再開は変数を `true` に戻します。

## 自動マージ（既定で無効）

`AUTO_MERGE_ENABLED` が `true` のとき、ゲートが合格で、要人間承認の理由がなく、main の最新を取り込み済みの PR のうち、
次の2種類だけに自動マージを予約します。

- AI ブランチの、`docs/` 配下（`docs/specs/` を除く）の Markdown・画像だけの変更
- Dependabot による、ワークフローの `uses:` のコミット固定だけを差し替える変更

有効にする前に、次の8項目を完全合成の Issue・PR で確かめます。実データ・実機は使いません。

| 試験 | 合格条件 |
| --- | --- |
| 同じイベントの二重受信 | PR が1本のまま |
| 担当がタイムアウトした直後に復帰 | 同時書き込みが起きない |
| 必須テストのスキップ・削除 | ゲートが不合格または要人間承認 |
| 古いコミットの成功後に追加 push | 古い成功を流用しない |
| 外部 Issue に不正な操作指示 | AI が起動しない |
| 利用枠・上限への到達 | 保留になり、有料経路へ切り替わらない |
| 個別には成功するが組合せで失敗する PR | main の最新を取り込むまで予約されない |
| 停止後に遅れて届いた完了 | マージされない |

「マージ済み」は「提供済み」ではありません。実機への反映が要る変更は、マージ後も `要実機作業`・`要再起動` の Issue を開いたままにし、
リリースと配備はこれまでどおり所有者が行います。

## 所有者が行う設定

このリポジトリの Claude のセッションからは GitHub の設定を変えられません。設定は所有者が行います。

```bash
R=yusuketakuma/hermes-mcs
# 停止スイッチ（作るだけ。false のまま）
gh variable set AUTOMATION_ENABLED --body false --repo $R
gh variable set AUTO_MERGE_ENABLED --body false --repo $R
# Claude Code Action の認証（手元で claude setup-token を実行して得たトークン）
gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo $R
# Codex の相談モード（OpenAI API の従量課金。使わないなら設定しない）
gh secret set OPENAI_API_KEY --repo $R
```

- Claude GitHub App をこのリポジトリにインストールします（https://github.com/apps/claude）。
- `merge-gate` を main-protect の必須チェックに足すのは、合成の試験 PR でゲートの不合格を確かめてからにします。
- 自動マージを使うときは、Settings → General の Allow auto-merge をオンにします。

## データの扱い

公開リポジトリなので、Issue・PR・Actions のログは誰でも読めます。患者データ・実投稿（匿名化例も含む）・実データのログ・秘密値は、
Issue にも AI への指示にも書きません。稼働中の Mac（`~/.mcs`）は、どの AI にも接続しません。
