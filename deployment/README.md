# deployment/ — 実機運用資産

MCS を実機（macOS）で常駐運用するための配置資産の正本。launchd plist
テンプレート・hermes cron 用 wrapper・復旧 watchdog を置く。
`__PYTHON__` 等のプレースホルダは `mcs/ops/mcs_setup.py services` または
`install.sh` が実値に置換して配置する — 実機側の生成物を直接編集すると
repo との drift になる。

## 構成

| パス | 内容 |
|---|---|
| `launchagents/` | launchd plist テンプレート 8件。Hermes用の4件、独立host、外部health watch、LLMサーバ、復旧watchdog。ジョブ構成・プレースホルダ規則は [launchagents/README.md](launchagents/README.md) |
| `scripts/` | 定期ジョブ用wrapper 6件と、明示有効化するoffsite backup用1件。Hermesは`~/.hermes/scripts/`、独立実行は`~/.mcs/scripts/`へレンダリング |
| `recovery/` | `mcs_recover.py` — 更新中断を自律復旧する独立 watchdog ツール（install.sh が `~/.mcs-recovery/` へコピーし `org.mcs.recovery` で定期起動） |
| `cco/` | [CCO terminal/file 隔離の候補設定・承認記録・検証説明](cco/cco-terminal-isolation.md) |

同梱する公式医薬品マスター原本と版・出典・hashはcheckout内の
[`resources/drug-master/`](../resources/drug-master/README.md)に置く。gitによる導入・更新で
コードとともに配布し、実機の私有辞書・configへ自動コピー/有効化しない。

## セットアップ

入口は `./install.sh`（冪等）。スケジューリング構成と手動手順の詳細は
[launchagents/README.md](launchagents/README.md) を参照。

## CCO terminal/file 隔離の候補設定

現在の `cco/cco-terminal.candidate.yaml` は snapshot 読取専用と
`cmd-proposals` 書込み用の候補で、実行用 `cmd` は公開しない。
`cco/cco-approval-scope.json` は旧候補の設定適用記録であり、記録の候補hashは
現在のファイルと一致しない。現在候補の承認・適用・実機検証の証拠には使わない。
背景と過去の検証範囲は [CCOの隔離設定と検証記録](cco/cco-terminal-isolation.md)
を参照する。

## 責務別配置への更新

抽出workerは `mcs/extract/llm/extract_llm.py`、共通推論は `mcs/llm/` に配置する。plistとcatchupスクリプトを同時に更新し、適用時は通常の更新手順でサービスを再同期する。`mcs/ops/mcs_cli.py`・setup/update/recover の公開入口は維持する。構造変更だけで実機のplistや稼働workerは更新されない。詳細は[責務・依存・移植範囲](../docs/development/REPOSITORY_STRUCTURE.md)。
