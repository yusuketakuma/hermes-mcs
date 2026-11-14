# CLAUDE.md — hermes-mcs

@AGENTS.md

## Claude Code harness notes

- Bashツールの `grep` は ugrep ラッパーで `.gitignore` 対象を飛ばす。呼出元の網羅確認は正本の `git grep` の規則に従う。
- auto mode が共有資源への merge・fast-forward を拒否したら、回避せずユーザーの明示承認を得る。
- サブエージェントにテストを並行して走らせない。
- 圧縮時は worktree・ブランチ・最新SHA・変更ファイル・実行テストと結果・未実施検証を残す。
