# GitHub 上の AI 実装の運用

所有者が Issue に担当を付けると、AI がPRを作り、CI とマージ判定ゲートが検証します。
自動マージは明示的に有効化したときだけ動き、現在の head に対する人の承認を必須にします。
停止スイッチの現在値は未確認です。この変更だけでは有効になりません。
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

既存のゲートは、ワークフロー・実行環境・テスト削除などの変更を「要人間承認」として説明に残します。
新しい自動マージ controller は、このパスによる区分を対象の選別には使いません。すべての対象 PR に同じ人承認・CI・保護条件を課します。
PR に足されたワークフローも Actions 名義の status を書けるため、承認者は `.github/` やゲート自体の差分も確認してください。
`ci/agent_control.py` の check evidence 判定は変更しません。既存の docs mode では SDK などが省略されても、
このゲートは実際の成功を要求するため通らない場合があります。文書だけの PR に例外を足したり、full CI を強制したりはしません。

## 試行回数

AI ブランチ（`agent/`・`copilot/`・`codex/`）の CI が失敗すると、PR 本文の `Fixes #番号` などで結ばれた Issue の `試行:N` を1つ進めます。
担当をまたいで通算し、3回目で担当ラベルを外して `要オーナー対応` にします。
`agent-control.yml` 自体を再実行しても数えません（CI を再実行して失敗したときは数えます）。
同じ Issue を結ぶ2本の PR がほぼ同時に失敗すると、1回分しか数えないことがあります。1 Issue 1担当の運用ではまれなので、許容しています。

Issue の失敗ラベルの分類は、所有者が確認します。後述の runner 基盤障害だけの再試行とは別の運用です。
`失敗:CI基盤`・`失敗:利用枠`・`失敗:不明` が付いた Issue は保留し、コードを変えたり、従量課金の経路へ切り替えたりしません。

### 所有者が依頼する修正（Claude 担当だけ）

CI 失敗をきっかけに `agent-control.yml` が Claude のコード修正を自動起動する経路は廃止しました。
所有者が修正を依頼するときだけ、PR で `@claude` と指示するか、
`agent-claude-repair.yml` を既定ブランチから手動で `workflow_dispatch` します（入力 `pr`）。
修正ジョブは PR のコードを取り出す前に、PR と結ばれた Issue の今のラベルを読み直し、次をすべて満たすときだけ Claude を動かします。

- このリポジトリの開いた `agent/claude-…` ブランチで、本文に `Fixes #番号` などで結ばれた Issue がある。
- 結ばれた Issue の**すべて**で、`担当:claude` が唯一の担当ラベルで、`要オーナー対応`・`失敗:CI基盤`・`失敗:利用枠`・`失敗:不明` が付いていない。
  3回目の失敗では担当が外れるので、1つでも上限に達した Issue があれば PR 全体の修正を止めます。
- `AUTOMATION_ENABLED` が `true`。

修正モードの Claude は失敗したログを読み、同じブランチを直して push します。push で CI が再び走り、結果は同じ判定に戻ります。
基盤・利用枠・原因不明と判断したときは、コードを変えずに PR へ一度だけコメントします。
Copilot と Dots の PR は自動では再起動しません（Copilot は所有者が PR で `@copilot` と書きます）。
同じブランチの書き手を1つに保つため、CodeRabbit などレビューツールの自動修正（Autofix）は使いません。

## main の取り込みと生成表（`main-sync`）

PR どうしの衝突を減らすため、`.github/workflows/main-sync.yml` が main への push または手動 dispatch で2つの処理をします。
controller が `GITHUB_TOKEN` で直接 merge したときは push イベントの抑制に備え、merge の確認後に main-sync を明示的に dispatch します。

- **生成表の更新（`dev-doc`）**: `docs/development/DEVELOPMENT.md` の生成表（`GENERATED:modules` など）は PR に含めません。
  ほぼすべての PR がこの表を変えるため、含めると1本マージされるたびに残りの PR がすべて衝突するからです。
  main に入ったあと、このジョブが `update_readme.py --dev-doc` で作り直して main に commit します。
  PR 側の `hygiene` は、生成器が動くことと、PR が生成表を main（merge base）のまま残していることだけを確かめます。
  手書き部分の変更は PR に含めてかまいません。`README.md` の `GENERATED:release` はこれまでどおり PR で作り直します。
- **PR への main の取り込み（`update-prs`）**: main より遅れている、このリポジトリのブランチの PR へ、GitHub の Update branch で main を merge します。
  rebase や force-push はしません。GITHUB_TOKEN で作った commit では PR の CI が起動しないため、続けて CI と、`release-notes.yml` の読み取りだけの PR 検査（`action=pr_check`、変更記録の検査を含む）を `workflow_dispatch` で起動し、
  CI の最後のジョブ（`merge-gate-request`）が `agent-control.yml` を `workflow_dispatch` で起動して、`merge-gate` を出します。
  GITHUB_TOKEN で起動した実行は、完了しても `workflow_run` を起こさないためです。人が起動した CI は、これまでどおり `workflow_run` で判定します。
  この依頼は PR 番号・head SHA・CI run ID を渡します。controller は本物の `ci.yml` の run か、head が一致するかを API で確かめ、
  terminal job を含む完了を最大60秒のポーリングで待ちます。新しい run の完了を確認する前には merge・再試行しません。
  bot が再試行した PR CI も同じ terminal hook を使い、合成 merge commit ではなく PR の head SHA を渡します。
  dispatch によるゲート判定は Issue の試行回数を加算せず、Claude の自動修正も起動しません。
  既定ブランチ（`refs/heads/<既定ブランチ>`）以外から起動された判定は動きません。
  Actions event policy で `agent-control.yml` の起動イベントを絞っている場合は、`workflow_dispatch` も許可します。許可が無いと、この判定は起動時に失敗します。
  起動に失敗した head は、次の実行で（このジョブの merge commit に CI が無いことを確かめて）起動し直します。引き継いだ自動マージ予約は取り込みの前に外し、付け直しません。
  衝突、または `.github/workflows/` の変更（GITHUB_TOKEN では書けません）で取り込めない PR には、手で取り込む手順を1件だけコメントし、以降はそのコメントを書き換えます。
  fork の PR は対象外です。

生成表の commit は、environment `main-docs` に置いた deploy key で push します。environment は main だけに使わせるので、
PR が足したワークフローからは鍵を読めません。main-protect はこの deploy key だけを迂回させます。
鍵が未設定の間、`dev-doc` は何もせず成功し、生成表は更新されません（`update-prs` は動きます）。

止めるときは `gh workflow disable main-sync.yml --repo yusuketakuma/hermes-mcs` を実行します。

## 停止スイッチ

リポジトリ変数 `AUTOMATION_ENABLED` が `true` のときだけ AI が起動し、AI ブランチの PR がゲートを通ります。
変数が無い・`true` 以外なら停止です。controller の CI 再試行も `AUTOMATION_ENABLED` が `true` のときだけ動きます。
直接 merge するには、さらに `AUTO_MERGE_ENABLED` が `true` であることが必要です。後者だけを切っても CI 再試行は止まりません。
停止中も、引き継いだ自動マージ予約の撤回と、古い対象 CI の取り消しは行います。

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

停止中に遅れて届いた完了でも controller は merge も CI 再試行もしません。再開は必要な変数を `true` に戻します。

## 自動マージ（既定で無効）

対象は、同一リポジトリのブランチから既定ブランチへ向けた、開いている非 draft PR のすべてです。
AI・人・Dependabot の別、ブランチ名、変更ファイルで対象を分けません。パス例外も承認の免除もありません。
fork、別の base、閉じた PR、draft は対象外です。

- 最新の決定的な formal review を承認者ごとに確認します。現在の head と完全一致する `APPROVED` が必要です。
  承認者は PR 作成者ではない GitHub `User` で、そのリポジトリの `write`・`maintain`・`admin` のいずれかを持つ人に限ります。
  bot、自分の PR への承認、read-only ユーザー、所有者のコメントは承認として扱いません。
- formal review の代わりに、オーナー（リポジトリ所有者か admin）が付けた `オーナー承認` ラベルも人の承認として扱います。作成者本人の PR にも使えます。
  ラベルを付けた時刻が現在の head の最初の CI 実行より後の場合だけ有効です。push すると無効になり、`pull_request_target` の synchronize で外します。
  未解決の `CHANGES_REQUESTED` はラベルがあっても止めます。ラベルの追加・削除では CI を起動しません（`ci.yml` は label イベントで動きません）。
  未解決の `CHANGES_REQUESTED` があれば止めます。古い・dismissed・不明・取得失敗の review を有効な承認として流用しません。
- 現在の head の最新 CI run が完了して成功し、既存の6ジョブの実際の成功と `merge-gate` の成功が必要です。
  GitHub の `mergeable_state` も `clean` でなければ merge しません。branch protection を迂回したり、必須チェックを変更したりしません。
- merge 直前に PR の head・review・check evidence を読み直し、`GITHUB_TOKEN` だけで REST merge を要求します。
  head SHA を楽観ロックに指定し、`merge_method=merge` の merge commit でグループの履歴を保持します。squash・rebase・admin bypass・PAT は使いません。
  将来の成功を待つ `--auto` 予約は作りません。残っていた予約は、既定ブランチの controller が新しい push を含む再評価時、どの `merge-gate` を発行するよりも前に全対象から撤回します。fork・下書きの残存予約も含め、撤回を確認できなければその wake ではゲートを発行しません。

`pull_request_target` の PR 受付、CI 完了、review の提出・編集・取り消しで再評価します。review は
`pr-review-signal.yml` の読み取り専用・checkout なしの no-op を完了させ、既定ブランチの `agent-control.yml` に `workflow_run` で伝えます。
controller は常に既定ブランチのコードを使い、PR コードを実行しません。自動マージ判定に cron・日次巡回・digest はありません。
1つの非キャンセル concurrency group に直列化し、review signal を含む各 wake で既定ブランチ向けの全 open PR の現在 head の `merge-gate` を判定してから merge を評価します。ゲートの判定には fork・下書きも含めますが、自動 merge の対象にはしません。
同じ head SHA は1件にまとめ、下書きと ready PR が共有している場合は ready PR 群を優先します。その群の各 PR を判定し、success・pending・failure の順で最も有利な結果を発行します。個別の取得エラーは他の候補を妨げず、ジョブ結果には残します。
作成者を問わず最新の `merge-gate` を読み、それが `github-actions[bot]` によるもので state・description が同じ場合だけ再発行を省きます。別の作成者の新しい status があれば再発行し、変化のない通常時は commit status の件数上限を浪費しません。
最後の1本だけの出力には依存しません。全 PR の走査は判定だけを行い、Issue の試行回数は、失敗した CI run の `pull_requests` に記録された PR の現在 head が一致するときだけ、下書きも含め別に加算します。
PR ごとの取得・ゲート発行に失敗しても残りの PR と controller の再評価を続け、最後に失敗した判定があったことをジョブ結果へ反映します。

承認の追加・変更では CI を dispatch も再実行もしません。CI と人の承認は並行して待ちます。
新しい head では古い承認と成功を使わず、新しい head の CI と新しい人承認が必要です。
PR CI は既存の concurrency で旧 run を取り消します。main-sync の branch-dispatch CI も、
PR 番号・branch 種別を含む run title と SHA で絞り、現在の head を再確認して旧 run だけを取り消します。
この title がない旧形式の dispatch run は、tag・手動実行との安全な識別ができないため取り消しません。tag・schedule・別 PR・現在の SHA は対象外です。
取り消しが失敗した場合は run を再取得し、すでに完了していれば処理を続けます。まだ実行中か確認できない場合も警告だけを出し、新 head の CI 起動や判定は止めません。

### CI 基盤障害の再試行

自動再試行を許可するのは、API で確かめた本物の最新 CI 完了イベントだけです。review・PR 受付・一般の手動再評価からは再試行しません。
現在の head の `ci.yml` が `run_attempt=1` の完了済み failure で、失敗した全ジョブを調べ、
runner の `Set up job` で発生した基盤障害を、狭い許可リストの Actions annotation で積極的に確認できる場合に限ります。
一般的な timeout・network の文字列やログの部分一致では判断しません。基盤障害の pytest slice に依存する
`lint-test`・`macos` の失敗だけは、明示した依存関係で区別します。独立したテスト失敗や原因不明の失敗が1件でもあれば再試行しません。

run ID/head に対応する bot 所有の commit status `ci-infra-retry/<run-id>` に要求を先に記録し、
head と最新 run を再確認して `rerun-failed-jobs` を1回だけ要求します。失敗ジョブとその依存先だけが対象で、成功した無関係の CI は回し直しません。
成功した再試行は同じ head の人承認を維持します。通常の待機、承認受付、再試行の記録だけでは PR コメントを出しません。
テスト失敗・確認できない障害・再試行後の失敗では merge を止め、実際の run/job ログへのリンク付き通知を重複しないように更新します。
保護による block、conflict、未解決の変更要求も対応が必要な通知の対象です。

有効化前に完全合成 fixture の focused meta tests で、古い承認・head 変更・チェック欠落・保護 pending/blocked・複数 PR・
重複イベント・基盤障害とテスト失敗の混在・2回目の失敗・承認時に CI 操作がないことを確認します。実データ・実機は使いません。
現在の変数値は未確認です。有効化する場合は所有者が保護設定を確認し、`AUTOMATION_ENABLED` と `AUTO_MERGE_ENABLED` を両方 `true` にします。

この実装は `origin/main` から作成し、PR #266 の上には積んでいません。#265 の review 自動修正、#266 の merge readiness とは
`agent-control.yml`・`ci/agent_control.py`・この文書・meta tests が重なるため、取り込む場合は経路を二重化せず調整します。

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
- 読み取り確認時の `main-protect` は、すでに `merge-gate` と6つの CI gate を必須にしています。重ねて追加せず、有効化前に現在の設定を確認します。
- 新 controller は直接 merge するため、Settings → General の Allow auto-merge を有効にする必要はありません。保護設定や bypass 権限はこの変更で操作しません。

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
