# スケジューリング構成

収集ジョブは **hermes cron（標準スケジューラ）+ launchd 1件** のハイブリッド。

| ジョブ | スケジュール | 実行系 |
|---|---|---|
| 未読チェック `run_check.py --json --download-files` | `*/15 * * * *` | hermes cron |
| durable-job drain `run_check.py --json --jobs-only` | `7,37 * * * *` | hermes cron |
| コマンド取込 `run_check.py --json --download-files` | `data/cmd/` WatchPaths（イベント駆動） | launchd |

## hermes cron 側（推奨デフォルト）

wrapper スクリプトを `$HERMES_HOME/scripts/` に置き、`--no-agent`
モードで登録する（stdout 空=成功時沈黙、alert 行のみ通知）:

```bash
cat > ~/.hermes/scripts/mcs_check.sh <<'SH'
#!/bin/bash
set -u
PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
LOG="$HOME/.mcs/data/run.log"
PY=$HOME/.hermes/hermes-agent/venv/bin/python
"$PY" "$HOME/.mcs/mcs/run_check.py" --json --download-files >>"$LOG" 2>&1
rc=$?
# exit 2 = session expired — MCS notifier 側が notify_system_target で
# 重複排除済みアラートを出すためここでは黙る（15分毎スパム防止）
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
  printf 'mcs check: run_check exited %d — see %s\n' "$rc" "$LOG"
fi
exit "$rc"
SH
chmod +x ~/.hermes/scripts/mcs_check.sh
hermes cron create "*/15 * * * *" --name "MCS unread check" \
  --script mcs_check.sh --no-agent --deliver local
# mcs_deep.sh も同形（--jobs-only）で "7,37 * * * *" に登録
```

## launchd 側（コマンド取込のみ）

`local.mcs-cmd` は `data/cmd/` へのファイル出現で即時 drain する
イベント駆動ジョブ。hermes cron は時間ベースのみなので launchd 維持。

```bash
cd deployment/launchagents
sed -e "s|__PYTHON__|$HOME/.hermes/hermes-agent/venv/bin/python|g" \
    -e "s|__REPO__|$(cd ../.. && pwd)|g" \
    -e "s|__DATA__|$HOME/.mcs/data|g" \
    local.mcs-cmd.plist > ~/Library/LaunchAgents/local.mcs-cmd.plist
launchctl load ~/Library/LaunchAgents/local.mcs-cmd.plist
```

| プレースホルダ | 例 |
|---|---|
| `__PYTHON__` | `/Users/you/.hermes/hermes-agent/venv/bin/python` |
| `__REPO__` | このリポジトリの checkout パス（例 `/Users/you/hermes-mcs`） |
| `__DATA__` | データ dir（例 `/Users/you/.mcs/data`） |

> 注意: パス変更時は plist の ProgramArguments と cron wrapper の双方を
> 更新すること（`adapter/` → `mcs/` 移動時に実機 plist が旧パスで失敗した実績あり）。
> launchd 直管理だった `local.mcs-check`/`local.mcs-deep` は 2026-09 に
> hermes cron へ移行済み（実行履歴・incident が `hermes cron runs` に残る）。
