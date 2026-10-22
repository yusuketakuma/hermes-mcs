@AGENTS.md

# Claude Code 固有

上の `AGENTS.md` が共通の正本。ここには Claude Code だけに関わる差分を書く。

- Bash ツールの `grep` は ugrep のラッパーで、`.gitignore` 対象（`data/` など）を飛ばす。改名・削除の呼出元を網羅するときは `git grep` を使う。
- auto mode は `release/*`・`main` への merge・fast-forward を共有資源の変更として拒否することがある。拒否されたら回避せず、ユーザーの明示承認を得てから実行する。
- サブエージェントにテストを並行して走らせない（`AGENTS.md` の全体 suite の制約はサブエージェントにも及ぶ）。
- 圧縮時は、作業中の worktree とブランチ・最新 SHA・変更ファイル・実行したテストと結果・未実施の検証を残す。
