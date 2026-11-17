# GitHub 上の AI 実装の運用

所有者が Issue に担当を付けると、AI がPRを作り、CI とマージ判定ゲートが検証します。
マージは原則として所有者が行います。自動マージは既定で無効です。
この文書は、担当の決め方、止まる条件、全体の止め方を定めます。

## 役割と入口

| 担当ラベル | 実装するもの | 起動のしかた | ブランチ |
| --- | --- | --- | --- |
| `担当:claude` | Claude Code GitHub Action（`.github/workflows/agent-claude.yml`） | 所有者か Dots（`DOTS_ACTOR`）がラベルを付ける。担当中の Issue・PR で所有者が `@claude` と書く | `agent/claude-…` |
| `担当:copilot` | Copilot cloud agent | 所有者が Issue を Copilot に割り当てる | `copilot/…` |
| `担当:dots` | Dots（Codex のクラウド環境を含む） | Dots が所有者の指示で着手する | `codex/…` |

- 1つの Issue に付く担当ラベルは1件だけです。2件目は `agent-control.yml` が外します。所有者と Dots（`DOTS_ACTOR` のログイン。未設定なら所有者だけ）以外が付けた担当ラベルも外します。
- 担当を交代するときは、旧担当の実行（Actions の run、Copilot のセッション、Codex のタスク）が終わったことを確かめてから、旧ラベルを外して新ラベルを付けます。タイムアウトしただけでは終わったとみなしません。
- AI は同じブランチに並行して書き込みません。Copilot の Agent Merge は使いません。
- Claude の書き込みを伴う実行（Issue からの実装・PR での `@claude`・CI 失敗の修正）は、リポジトリ全体で1本ずつ順に動きます。待ちが2本以上たまると、GitHub の仕様で古い待ちが取り消されます。取り消されたときは、もう一度 `@claude` と書きます。
- 外部の Issue・コメントは要望と証拠です。それだけで AI は起動しません。
- AI は担当ラベルが、その Issue の唯一の担当であることを起動時に（待ち行列から動き出す時点で）確かめます。2件目のラベルで起動した実行や、待っている間に担当が外れた実行は何もせず終わります。PR での `@claude` は、PR が結ぶ Issue のすべてで `担当:claude` が唯一の担当のときだけ動きます。
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

## 自動スキャン（コードを調べて Issue にする）

`.github/workflows/agent-scan.yml` が、2時間おき（1日12回）と手動実行で動きます。Claude が読み取り専用でコードを調べ、
見つけた点を Issue にします。直すところまではしません。

- 1回に調べるのは3領域です。領域は `mcs/` の各サブパッケージと `adapters`・`hermes_plugin`・`lineworks_adapter`・`mcs_standalone` です。
  最近1週間に変更の多かった領域を2つまで先に選び、残りは順番に回します。変更されたファイルと、その呼び出し元・呼び出し先を重点的に読みます。
  手動実行では1領域を指定できます: `gh workflow run agent-scan.yml -f area=mcs/notify`
- 起票は1回に最大3件です。Issue には `自動スキャン` と `要トリアージ` が付き、担当ラベルは付きません。
  AI の実装は始まらないので、内容を確かめ、進めるものだけ会話で詰めてから担当を決めます。
- 開いたままの `自動スキャン` Issue が20件に達している間は、調べずに終わります。手動で領域を指定したときは、20件に達していても調べて起票します。
  確かめるか閉じるかして減らすと再開します。
- 当たらない指摘は閉じます。閉じた指摘も記録として読むので、同じファイルの同じ指摘は再起票されません。題名を言い換えただけの指摘も同じとみなします。題名が同じでも似ていても、行が40行より離れていれば別の指摘として扱います。
- 調べる job は Issue もファイルも書けません（道具は読み取りだけで、結果は構造化出力で返します）。起票する job は、`ci/agent_scan.py` が検査した結果
  （実在するファイルを指す、長さの上限内、既存と重複しない、トークンに見える長い文字列を含まない）だけを受け取ります。
- 指摘は AI がコードを読んだ推定です。確かめる前に事実として扱いません。

## マージ判定ゲート（`merge-gate`）

`agent-control.yml` が CI 完了のたびに、PR の現在の head に `merge-gate` の commit status を出します。
判定は `ci/agent_control.py` が行い、main 側の定義だけを使います。

- `lint-test`・`hygiene`・`standalone-sdk`・`incident-gates`・`macos`・`hermes-integration` が、現在の head で、GitHub Actions によって実行され `success` であること。
  `skipped`・`neutral`・欠落は不合格です（GitHub の必須チェックはこれらを合格として扱うため）。
- `pytest-linux`・`pytest-macos` の結果（行列の job は `pytest-linux (1)` のように分割ごとに報告されます）があれば、それもすべて `success` であること。文書だけの変更での `pytest-macos` の省略だけは認めます。
- 停止スイッチが切れている間は、AI ブランチの PR を不合格にします。
- 下書きPRは使いません。AI も人も、PR は最初から通常のPRとして開きます。下書きのまま届いた PR（Copilot が GitHub の仕様で作るものを含む）は、所有者が Ready for review にするまで保留にします。

合格でも、次を含む PR は「要人間承認」として説明に理由を書き、自動マージの対象から外します。

- `.github/`・`ci/`・`scripts/`・`Makefile`・`pyproject.toml`・`conftest.py`・`CLAUDE.md`・`AGENTS.md`・`SECURITY.md`
- `hermes_plugin/`・`adapters/`・`lineworks_adapter/`・`mcs_standalone/`・`deployment/`
- テストファイルの削除・改名、skip・xfail の追加、テスト関数の純減

PR に足されたワークフローは、GitHub Actions 名義で `merge-gate` を書けます。そのため `.github/` を含む PR は必ず人がマージし、
自動マージの予約は push のたびに外します。`merge-gate` が緑でも、`.github/` を含む PR は差分を見て判断してください。
自動マージの予約は、判定した head のコミットに結び付けます（`--match-head-commit`）。判定後に push された head には引き継ぎません。

## レビューの判定と `merge-ready`

レビュー合格かどうかは、`ci/agent_control.py` の `review_verdict` だけが決めます。`merge-ready` と Issue の進み具合は同じ判定を使います。

- 数えるのは、リポジトリ変数 `REVIEW_ACTORS`（カンマ区切りのログイン。大文字と小文字も区別し、App は `名前[bot]`）に載ったアカウントの正式レビュー（Approve・Request changes）だけです。
  一覧の先にあるほど優先します。表示の名前は順番ではなくログインで決め、`DOTS_ACTOR` のログインを dots、`claude[bot]` を claude と出します。コメントだけのレビューと、現在の head 以外のコミットへのレビューは数えません。
- 現在の head に判定のあるアカウントのうち、一覧で先のアカウントの最新の判定で決まります。Dots の修正依頼は Claude の承認より優先します。
  PR の作者と所有者のアカウントは数えません。席（codex1・codex2・claude1・claude2）のレビューは所有者のアカウントで書かれるので数えず、進み具合の「注意」に参考として出すだけです。
- `REVIEW_ACTORS` が未設定か空のとき、または現在の head に判定がないときは不合格です。

### Dots の専用ログインができるまでのつなぎ

`DOTS_ACTOR` が未設定か空の間だけ、所有者のアカウントが PR に書いたコメントのうち、1行目が次の形と完全に一致するものを Dots の判定として数えます。

```text
<!-- dots-review verdict=approved head=<40桁の head SHA> -->
<!-- dots-review verdict=changes_requested head=<40桁の head SHA> -->
```

- `head` が現在の head と一致するものだけを数え、その中で作成時刻が最も新しいものが決めます。Claude のレビューより優先します。
- レビュー本文・PR 本文・ほかの人やボットのコメントに書かれたマーカーは数えません。
- このマーカーを書くのは Dots だけです。席は書きません。
- `DOTS_ACTOR` を設定するとマーカーは数えなくなり、そのログインの正式レビューだけが Dots の判定になります。

### `merge-ready`

`agent-control.yml` は、開いている PR の現在の head に `merge-ready` の commit status を出します。
次をすべて満たすときだけ `success` で、そうでなければ `pending` か `failure` にして説明に理由を書きます。

- `merge-gate` が `success`（check run から計算し直します）
- main の最新を取り込み済み（`behind_by` が 0）で、下書きでない
- 要人間承認の理由がない（保護対象の変更は、これまでどおり所有者がマージします）
- このリポジトリのブランチの PR で、レビューの判定が合格

`workflow_run`・手動実行・15分おきの定期実行で開いた PR をすべて調べ直し、状態か説明が変わったときだけ書きます。
PR 側のイベント（`pull_request`・`pull_request_review`・`pull_request_target`）では動かさず、PR のコードは checkout も実行もしません。
`merge-ready` はマージしません。`gh pr merge --auto` の対象（Dependabot のコミット固定と AI の文書だけの変更）は変えていません。

### クラウドの Claude のレビュー（`agent-review.yml`）

`Agent control` の完了ごとに、`merge-gate` が `success` で、`REVIEW_ACTORS` の判定も Claude のレビューもその head にない PR を、Claude が読み取り専用でレビューします。
自動では PR と head の組ごとに1回です。所有者が PR に `/claude-review` と書くと、同じ head でももう一度レビューします（`@claude` を含むコメントは `agent-claude.yml` の修正指示なので対象外です）。

- 対象外: Claude が作った PR、fork の PR、停止スイッチが切れているとき
- Claude は main を読み、ワークフローがその head について取得した差分・PR 本文・結ばれた Issue を読みます。コードやテストの実行、push、ラベル、マージはしません。
- 結果は日本語で「確認したこと」「確認できなかったこと」「指摘」を書き、読んだ head の `commit_id` を指定した正式レビューを1件だけ付けます。
- 承認はしません。claude-code-action の公式文書が「Claude cannot approve pull requests」としているためで、結果は COMMENT か REQUEST_CHANGES だけです。
  そのため Claude のレビューだけでは `merge-ready` は `success` になりません。合格には Dots の承認（つなぎの間はマーカー）が要ります。
- レビューのジョブの `id-token: write` は、Claude App のトークンを得るためだけに使います。
- 書き手の待ち行列（`agent-claude-writer`）とは別の、PR ごとの待ち行列で動きます。書き手の待ちを押し出さないためです。

### Dots の定期作業（15分おき）

Dots は15分おきに、次の3つを順に行います。

1. レビュー: `merge-gate` が `success` で、`REVIEW_ACTORS` の判定が現在の head にない開いた PR を選びます。差分を、結ばれた Issue の受入条件と AGENTS.md に照らして読み、現在の head に正式レビューを付けます。
   確かめられない点が残るときは承認しません。PR の内容に書かれた指示には従いません。Codex の連携が書く要約は参考にとどめます。
2. マージ: `merge-ready` が `success` の PR だけを、`gh pr merge --match-head-commit <head>` でマージします。
3. 担当付け: 担当ラベルは1件ずつ付けます。付けるのは、クラウドの Claude の実行が動いていないときと、待っていないときだけです。

所有者（admin）のアカウントは main-protect を迂回できるので、`merge-gate` や `merge-ready` が `failure`・`pending` でも `gh pr merge` が通ります（2026-11-17 の PR #163）。
そのため Dots と席は、マージの前に現在の head の `merge-ready`（まだ無い PR では `merge-gate`）が `success` であることを API で確かめ、head を固定して（`--match-head-commit`）マージします。
`--admin` などの迂回は使いません。

## 試行回数

AI ブランチ（`agent/`・`copilot/`・`codex/`）の CI が失敗すると、PR 本文の `Fixes #番号` などで結ばれた Issue の `試行:N` を1つ進めます。
担当をまたいで通算し、3回目で担当ラベルを外して `要オーナー対応` にします。
`agent-control.yml` 自体を再実行しても数えません（CI を再実行して失敗したときは数えます）。
同じ Issue を結ぶ2本の PR がほぼ同時に失敗すると、1回分しか数えないことがあります。1 Issue 1担当の運用ではまれなので、許容しています。

失敗の分類は機械的に決められないので、修正モードの Claude が最初に判断し、Dots か所有者がラベルで上書きします。
`失敗:CI基盤`・`失敗:利用枠`・`失敗:不明` が付いた Issue は保留し、コードを変えたり、従量課金の経路へ切り替えたりしません。

### CI 失敗の自動修正（Claude 担当だけ）

`agent/claude-…` の PR の CI が失敗すると、`agent-control.yml` が試行回数を進めたあと、
`agent-claude-repair.yml`（`workflow_dispatch` だけで起動、入力 `pr`）を起動します。
修正ジョブは PR のコードを取り出す前に、PR と結ばれた Issue の今のラベルを読み直し、次をすべて満たすときだけ Claude を動かします。

- このリポジトリの開いた `agent/claude-…` ブランチで、本文に `Fixes #番号` などで結ばれた Issue がある。
- 結ばれた Issue の**すべて**で、`担当:claude` が唯一の担当ラベルで、`要オーナー対応`・`失敗:CI基盤`・`失敗:利用枠`・`失敗:不明` が付いていない。
  3回目の失敗では担当が外れるので、1つでも上限に達した Issue があれば PR 全体の修正を止めます。
- `AUTOMATION_ENABLED` が `true`。

修正モードの Claude は失敗したログを読み、同じブランチを直して push します。push で CI が再び走り、結果は同じ判定に戻ります。
基盤・利用枠・原因不明と判断したときは、コードを変えずに PR へ一度だけコメントします。
Copilot と Dots の PR は自動では再起動しません（Copilot は所有者が PR で `@copilot` と書きます）。
同じブランチの書き手を1つに保つため、CodeRabbit などレビューツールの自動修正（Autofix）は使いません。

### レビュー指摘の自動修正（Claude 担当だけ）

レビューの指摘も、Autofix ではなく同じ `agent-claude-repair.yml` の Claude が直します。
`.github/workflows/agent-review-fix.yml` が15分おきの定期実行（と手動実行）で、開いている `agent/claude-…` の PR とそのレビューを API ですべて調べます。
レビューの投稿をきっかけには起動しません。そのイベントは PR 側のワークフロー定義で動き、ブランチから書き換えられるためです。指摘が修正に回るまで最大15分ほどかかります。

- 数える指摘は、所有者の Request changes のレビューと、CodeRabbit（`Actionable comments posted: N` が1以上）・Devin（`Devin Review found N potential issue` が1以上）のレビューだけです。
  指摘0件・nitpick だけ・「No Issues Found」・PR に載らない flag・スレッドへの返信・Codex など他のレビューは数えません。取り下げ（dismiss）たレビューも数えません。
  数えるのは現在の head へのレビューと、その後に main の取り込み（merge commit）だけが重なった head へのレビューです。
  修正の push で head が替わった後のレビューは数えません。所有者の Request changes がこれに当たったときは、PR に1回だけ知らせます。
  所有者の指摘を PR のコメントで書いたときは対象外なので、これまでどおり `@claude` と書きます。両方を使うと Claude が2回動きます。
- 対象の PR と Issue の条件は CI 失敗の自動修正と同じです（`担当:claude` だけ。`担当:dots` は別の書き手なので対象外）。
  fork の PR は調べません。結ばれた Issue が5件を超える PR は対象外です。1本の PR で API が失敗しても、ほかの PR の判定は続けます。
- 1回の判定で見つかった新しい指摘はまとめて1回と数え、CI 失敗と同じ `試行:N` を進めます。3回目で `要オーナー対応` にして止まります。
  同じレビューは2回数えません。判定は PR に `<!-- agent-review:queued … -->` を、修正の実行は `<!-- agent-review:handled … -->` を書き、`github-actions[bot]` が書いたものだけを読みます。
- Claude の書き込みの実行（`agent-claude.yml`・`agent-claude-repair.yml`）が待機中か実行中の間は、何も数えず、何も起動しません。
  新しい待ちが古い待ちを取り消すためです。状態を確かめられないときも実行中とみなします。修正の起動は1回の判定で1本までです。
- 待ちが取り消されて handled が付かなかったレビューは、次の判定が数え直さずに起動し直します。
- 修正ジョブは、渡された id をすべて読み直し、指摘を報告したレビュー（所有者の Request changes、CodeRabbit・Devin の指摘）だけを Claude に渡します。
- 修正モードの Claude は、指摘を `agent-review.md`（`.git/info/exclude` で commit されない）からデータとして読み、結ばれた Issue の受入条件の範囲だけを直します。
  範囲外の提案と誤りと判断した指摘は直さず、PR に1回のコメントで挙げます。テストの削除・skip・閾値の緩和はしません。
- 判定は main の定義と main の `ci/` だけで動き、PR のコードは checkout も実行もしません。main 以外から手動実行しても動きません。
- CodeRabbit は bot が作った PR を自動ではレビューしません（「Bot user detected」）。`github-actions[bot]` の `@coderabbitai review` が受け付けられるかは確かめていないので、自動では書きません。
  CodeRabbit の指摘を回すときは、所有者が PR に `@coderabbitai review` と書きます。Devin は所有者の設定で bot の PR もレビューします。

## main の取り込みと生成表（`main-sync`）

PR どうしの衝突を減らすため、`.github/workflows/main-sync.yml` が main への push のたびに2つの処理をします。

- **生成表の更新（`dev-doc`）**: `docs/development/DEVELOPMENT.md` の生成表（`GENERATED:modules` など）は PR に含めません。
  ほぼすべての PR がこの表を変えるため、含めると1本マージされるたびに残りの PR がすべて衝突するからです。
  main に入ったあと、このジョブが `update_readme.py --dev-doc` で作り直して main に commit します。
  PR 側の `hygiene` は、生成器が動くことと、PR が生成表を main（merge base）のまま残していることだけを確かめます。
  手書き部分の変更は PR に含めてかまいません。`README.md` の `GENERATED:release` はこれまでどおり PR で作り直します。
- **PR への main の取り込み（`update-prs`）**: main より遅れている、このリポジトリのブランチの PR へ、GitHub の Update branch で main を merge します。
  rebase や force-push はしません。GITHUB_TOKEN で作った commit では PR の CI が起動しないため、続けて CI と、`release-notes.yml` の読み取りだけの PR 検査（`action=pr_check`、変更記録の検査を含む）を `workflow_dispatch` で起動し、
  CI の最後のジョブ（`merge-gate-request`）が `agent-control.yml` を `workflow_dispatch` で起動して、`merge-gate` を出します。
  GITHUB_TOKEN で起動した実行は、完了しても `workflow_run` を起こさないためです。人が起動した CI は、これまでどおり `workflow_run` で判定します。
  この依頼は `merge-gate` を出すだけで、CI が失敗しても試行回数は数えず、自動修正も起動しません。判定は check runs を自分で読みますが、どの CI 試行の失敗かを1つに決められないためです。
  既定ブランチ（`refs/heads/<既定ブランチ>`）以外から起動された判定は動きません。
  Actions event policy で `agent-control.yml` の起動イベントを絞っている場合は、`workflow_dispatch` も許可します。許可が無いと、この判定は起動時に失敗します。
  起動に失敗した head は、次の実行で（このジョブの merge commit に CI が無いことを確かめて）起動し直します。自動マージの予約は取り込みの前に外し、新しい head の判定で付け直します。
  衝突、または `.github/workflows/` の変更（GITHUB_TOKEN では書けません）で取り込めない PR には、手で取り込む手順を1件だけコメントし、以降はそのコメントを書き換えます。
  fork の PR は対象外です。

生成表の commit は、environment `main-docs` に置いた deploy key で push します。environment は main だけに使わせるので、
PR が足したワークフローからは鍵を読めません。main-protect はこの deploy key だけを迂回させます。
鍵が未設定の間、`dev-doc` は何もせず成功し、生成表は更新されません（`update-prs` は動きます）。

止めるときは `gh workflow disable main-sync.yml --repo yusuketakuma/hermes-mcs` を実行します。

## Issue の進み具合（`issue-status`）

`.github/workflows/issue-status.yml` は、AI 実装の進み具合を各 Issue に1件のコメントで表示します。
状態が変わるたびに同じコメントを書き換え、変わらなければ書き込みません。判定は `ci/issue_status.py` が行います。

- 対象は開いている Issue のうち、担当ラベルが付いたものか、PR 本文の `Fixes #番号` などで結ばれたものです。閉じてから2日以内の Issue も含みます。
- 段階は起票・担当決定・実装中・PR 作成・CI（実行中・失敗・成功）・自動修正中・マージ判定・レビュー・マージ可・マージ・完了です。
  各段階に時刻と根拠のリンクを付け、いまの段階を太字で示します。`要オーナー対応`・`保留`・`失敗:*`・下書きPR・衝突は「注意」に出します。
- レビューの段階は「レビューの判定と `merge-ready`」の判定を使い、承認（dots）・承認（dots・つなぎ）・承認（claude）・修正依頼（…）と出します。
  判定が現在の head より前のコミットに対するものだけなら、再レビュー待ちとします（本文に書いた SHA ではなく、GitHub が記録したコミットで判断します）。
  所有者のアカウントのレビュー（席のレビューを含む）は判定に数えず、「注意」に参考として出します。本文が「独立レビュー（席名、作者でない席）」で始まるときは席名を添えます。
- レビューの次に「マージ可（merge-ready）」の段階を出します。
- main へのマージでは、GitHub が `Fixes #番号` によって Issue を閉じて完了になります。`group/*` へのマージでは Issue が閉じないので、「グループへ統合済み」と表示します。
  このワークフローは Issue を閉じず、ラベルも付けません。
- Issue・CI・AI の実行・レビューのイベントで起動し、そのイベントに関わる Issue だけを作り直します。GITHUB_TOKEN が起こしたイベントでは起動しないため、
  10分おきの定期実行がすべての対象を作り直し、取りこぼしを補います。表示が最大10分ほど遅れることがあります。
- 権限は Issue のコメントの書き込みだけです。PR のコードは取り出さず、main 側の `ci/` だけを使います。
  ただしレビューで起動したときは、GitHub の仕様で PR 側の workflow 定義が動きます。PR がこの workflow を書き換えると、その定義が Issue のコメントの書き込み権限で動くので、`.github/` を含む PR は人が差分を見てマージします。
  表示だけの機能なので、停止スイッチ `AUTOMATION_ENABLED` とは関係なく動きます。止めるときは `gh workflow disable issue-status.yml --repo yusuketakuma/hermes-mcs` を実行します。

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
- Actions event policy（14520）で起動イベントを絞っている場合は、`schedule`（`agent-review-fix.yml` と `agent-control.yml` の `merge-ready`）と
  `issue_comment`（`agent-review.yml` の `/claude-review`）を許可します（`workflow_dispatch` は許可済み）。許可が無いと、これらは起動しません。
- 判定の変数は次の2段階で設定します。`REVIEW_ACTORS` が空なら何も数えず、すべての PR が不合格です。
  - つなぎの間: `REVIEW_ACTORS` を `claude[bot]` にし、`DOTS_ACTOR` は設定しません。マーカーを数えます。
    Claude は COMMENT か REQUEST_CHANGES しか付けられないので、合格には Dots のマーカーが要ります。
  - Dots の専用ログインを作ったあと: ログインには write 権限だけを付け、admin にはしません（迂回できないようにするため）。
    `REVIEW_ACTORS` を `<Dotsのログイン>,claude[bot]`、`DOTS_ACTOR` を `<Dotsのログイン>` にします。マーカーは数えなくなります。

```bash
# つなぎの間
gh variable set REVIEW_ACTORS --body "claude[bot]" --repo $R
gh variable delete DOTS_ACTOR --repo $R 2>/dev/null || true
# Dots の専用ログインを作ったあと
gh variable set REVIEW_ACTORS --body "<Dotsのログイン>,claude[bot]" --repo $R
gh variable set DOTS_ACTOR --body "<Dotsのログイン>" --repo $R
```

- `merge-ready` と Dots のマージには、Allow auto-merge は要りません。

`main-sync` の生成表 commit に使う deploy key は次の手順で設定します（鍵は手元で作り、秘密鍵は secret に入れたあと消します）。

```bash
R=yusuketakuma/hermes-mcs
gh repo deploy-key list --repo $R   # 既存の書込み可能な deploy key があれば、下の迂回の対象になる
ssh-keygen -t ed25519 -N "" -C "hermes-mcs main-sync" -f ./main-sync-key
gh repo deploy-key add ./main-sync-key.pub --repo $R --allow-write --title "main-sync (DEVELOPMENT.md)"
# environment main-docs: main からの実行だけが鍵を読める
gh api -X PUT repos/$R/environments/main-docs --input - <<'JSON'
{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
JSON
gh api -X POST repos/$R/environments/main-docs/deployment-branch-policies -f name=main -f type=branch
gh secret set DEV_DOC_DEPLOY_KEY --env main-docs --repo $R < ./main-sync-key
rm ./main-sync-key ./main-sync-key.pub
# main-protect の迂回に deploy key を足す（既存の Repository admin はそのまま）
id=$(gh api repos/$R/rulesets --jq '.[] | select(.name == "main-protect") | .id')
gh api repos/$R/rulesets/$id |
  jq '{name, target, enforcement, conditions, rules,
       bypass_actors: (.bypass_actors + [{"actor_id": null, "actor_type": "DeployKey", "bypass_mode": "always"}])}' |
  gh api -X PUT repos/$R/rulesets/$id --input -
```

## データの扱い

公開リポジトリなので、Issue・PR・Actions のログは誰でも読めます。患者データ・実投稿（匿名化例も含む）・実データのログ・秘密値は、
Issue にも AI への指示にも書きません。稼働中の Mac（`~/.mcs`）は、どの AI にも接続しません。
