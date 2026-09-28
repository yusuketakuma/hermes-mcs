# deployment/ — 実機運用資産

MCS を実機（macOS）で常駐運用するための配置資産の正本。launchd plist
テンプレート・hermes cron 用 wrapper・復旧 watchdog を置く。
`__PYTHON__` 等のプレースホルダは `mcs/ops/mcs_setup.py services` または
`install.sh` が実値に置換して配置する — 実機側の生成物を直接編集すると
repo との drift になる。

## 構成

| パス | 内容 |
|---|---|
| `launchagents/` | launchd plist テンプレート 6件。ジョブ構成・プレースホルダ規則・drainer 設計は [launchagents/README.md](launchagents/README.md) |
| `scripts/` | hermes cron 用 wrapper スクリプト 6件（`~/.hermes/scripts/` へレンダリング） |
| `recovery/` | `mcs_recover.py` — 更新中断を自律復旧する独立 watchdog ツール（install.sh が `~/.mcs-recovery/` へコピーし `org.mcs.recovery` で定期起動） |
| `cco-terminal.candidate.yaml` | CCO terminal/file 隔離の候補設定（terminal 節のみの差分） |
| `cco-approval-scope.json` | 上記候補の適用範囲・検証・適用前後ハッシュの記録 |

## セットアップ

入口は `./install.sh`（冪等）。スケジューリング構成と手動手順の詳細は
[launchagents/README.md](launchagents/README.md) を参照。

## CCO terminal/file 隔離の候補設定

現在の `cco-terminal.candidate.yaml` は snapshot 読取専用と
`cmd-proposals` 書込み用の候補で、実行用 `cmd` は公開しない。
`cco-approval-scope.json` は旧候補の設定適用記録であり、記録の候補hashは
現在のファイルと一致しない。現在候補の承認・適用・実機検証の証拠には使わない。
背景と過去の検証範囲は [cco-terminal-isolation.md](cco-terminal-isolation.md)
を参照する。
