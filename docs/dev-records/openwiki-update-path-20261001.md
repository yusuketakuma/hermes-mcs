# OpenWiki 更新経路の確認（2026-10-01）

生成本文は変更せず、[共有brief](../../openwiki/INSTRUCTIONS.md)を追加した。
これは OpenWiki 0.6.1 が明示的に利用者所有・通常更新で保持する生成元であり、
生成ページ・Claims・index・provenance の直接編集ではない。
Jev の外部経路、Slack、既読化の cap 回収条件、相対リンク、今回の導入手順を
次の生成時に実ソースと照合する対象としてまとめた。

## 確認した経路

- [.github/workflows/openwiki-update.yml](../../.github/workflows/openwiki-update.yml)
  は `openwiki@0.6.1` を導入し、`openwiki code --update --print` を実行する。
  OpenAI provider とモデルを設定し、LangSmith connector 用認証と tracing 用認証を
  環境変数で渡す。生成後は `openwiki/`・agent指示・workflow の差分を PR にする。
  実行・認証・PR作成を今回行っていない。
- ローカルにも同じ 0.6.1 があり、`setup/onboarding.js` の
  `readRepositoryWikiInstructions` が `openwiki/INSTRUCTIONS.md` を読む。
  `agent/utils.js:createRunContext` がこれを生成コンテキストへ渡す。
  `agent/prompts/code.js` はこの brief を利用者所有として読むよう指示し、
  `agent/wiki-replacement.js` は init でも保存する。インストール済みコードは
  読取りだけで確認し、変更していない。
- MCP の begin/plan/page/finish はホストが執筆した本文を検査・保存して
  Claims と生成状態を管理する経路だった。既存ソースから自動的に本文を再生成する
  ローカル変換器ではないため、生成物直接編集禁止の代替として使っていない。
- `openwiki --help` は usage を表示した後、非TTYの raw mode エラーで終了した。
  更新コマンドは実行していない。今回、実モデル呼出や生成成功を検証したとはしない。

## 正確な既読化境界

前回記録の「snapshot timestamp 必須という説明は fallback_plain 例外を含まない」は、
timestamp 自体が省略されるとの誤読を招く。実装では fallback でも正の整数の
snapshot timestamp を必須とし、両方の GET に送る。省略するのは `unread=1` である。

`run_check._cap_cleared` は最古未読までの保存済み coverage を検証し、必要な場合は
最新投稿も保存済みか検証する。不足なら incomplete を維持する。完了・保存後に
unknown intent を記録して呼び出し、`mcs_adapter.mark_patient_read` は元の GET が
HTTP error で失敗し、かつ fallback が許可された場合だけ plain list を試す。
project detail に未読がないことを確認して confirmed とし、fallback 後は
`_post_ack_gap` で新しい未保管投稿を補修する。

根拠: [run_check.py](../../mcs/ingest/run_check.py) の `_cap_cleared`、
`stage_unread`、`_post_ack_gap` と
[mcs_adapter.py](../../mcs/ingest/mcs_adapter.py) の `mark_patient_read`。

## 次の再生成を実行できる条件と手順

現在の task の外部送信承認は jg のソース探索に限定される。OpenWiki CLI の
OpenAI へのソース送信、LangSmith からの取得・tracing、GitHub workflow 起動・
PR 作成はその承認に含まれない。ソース送信の対象、provider、tracing/connector の
使用範囲が明示承認された後に実行する。

1. 今回のソース変更と共有 brief を含むレビュー対象 revision を決める。
   既存の未commit差分を消したり、HEAD のみに戻して生成したりしない。
2. [開発資料](../development/DEVELOPMENT.md)に従い、実データ・認証情報を持たない別 worktree
   で実行する。読ませる対象を確認し、患者データ・`config.json`・`.env`・token・
   エクスポート・非公開ログを持ち込まない。必要な未commit変更は確認済みの
   ソース/資料だけを移し、元の checkout を保護する。
3. 認証済みの承認対象 provider を使う。認証値を引数・出力・資料へ出さない。
   workflow の既存設定を使う場合は LangSmith の取得と tracing も含むことを確認する。

```bash
# REVIEWED_REV は今回の修正と brief を含むレビュー対象 revision。
git worktree add --detach ../hermes-mcs-openwiki "$REVIEWED_REV"
cd ../hermes-mcs-openwiki
openwiki code --update --print
git diff --check
git diff --stat -- openwiki AGENTS.md CLAUDE.md .github/workflows/openwiki-update.yml
```

4. 終了コードだけで判断せず、`.last-update.json` の complete、生成ページ、Claims、
   リンク、四件の修正内容を確認する。失敗時は生成の部分完了と durable run state を
   確認し、進捗を保護してから同じ update を再開する。生成状態を手で書き換えない。
5. 通常のローカル生成も managed agent 指示を更新し得るため、Wiki以外の差分を確認する。
   正本 `AGENTS.md` が変わる場合は所定の Hermes 同期も必要となる。
   push・PR・merge・配備はそれぞれ承認範囲に従う。

## 検証と残件

共有 brief が現行 OpenWiki の読取り関数からそのまま取得できることを、ローカル
Node で確認した。文書の相対リンクの存在確認と `git diff --check` を実施した。
`ci/gates.py` は 8/8 成功、`ci/mine_gates.py --check`、
`scripts/update_readme.py --check`、`scripts/readme_release.py --check` も成功した。
生成ページ・sidecar・index・workflow・AGENTS.md はこの作業では変更していない。

再生成は未実施。共有 brief はモデルへの生成要件であり、内容の正しさを強制する
validator ではない。次の生成結果の照合が完了するまで、元の四件は未解決として扱う。
