# LaunchAgent テンプレート

`local.mcs-check` / `local.mcs-deep` / `local.mcs-cmd` の3層スケジューラ。
実機 `/Users/yusuke/Library/LaunchAgents/` の構成をテンプレート化したもの。

## プレースホルダ

| プレースホルダ | 例 |
|---|---|
| `__PYTHON__` | `/Users/you/.hermes/hermes-agent/venv/bin/python` |
| `__REPO__` | このリポジトリの checkout パス（例 `/Users/you/hermes-mcs`） |
| `__DATA__` | データ dir（例 `/Users/you/.mcs/data`） |

## 導入

```bash
cd deployment/launchagents
for f in local.mcs-*.plist; do
  sed -e "s|__PYTHON__|$HOME/.hermes/hermes-agent/venv/bin/python|g" \
      -e "s|__REPO__|$(cd ../.. && pwd)|g" \
      -e "s|__DATA__|$HOME/.mcs/data|g" \
      "$f" > ~/Library/LaunchAgents/"$f"
  launchctl load ~/Library/LaunchAgents/"$f"
done
```

- `local.mcs-check` — 00/15/30/45 分の定期 tick（RunAtLoad）
- `local.mcs-deep` — 07/37 分の履歴深掘り（`--jobs-only`）
- `local.mcs-cmd` — `data/cmd/` WatchPaths 即時実行

> 注意: パス変更時は plist の ProgramArguments も更新して reload すること
> （`adapter/` → `mcs/` 移動時に実機 plist が旧パスで失敗した実績あり）。
