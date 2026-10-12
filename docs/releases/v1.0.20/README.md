# 1.0.20 独立実行の外部監視

独立hostの外から保存済み状態を読み、停止・未回復・停滞を判定して設定した通知先へ警報する説明図。実画面ではない。

根拠: [service](../../../mcs_standalone/service.py)、[runtime](../../../mcs_standalone/runtime.py)、[health_watch](../../../mcs/ingest/health_watch.py)、[変更記録](../../../changes/archive/1.0.20/stab-standalone-health-watch.json)。

SVGを正本とし、macOSで `qlmanage -t -s 800 -o /tmp/visuals docs/releases/v1.0.20/standalone-health.svg` を実行後、生成したPNGをstandalone-health.pngへ配置する。画像全体と文字・下端注記を確認し、assets.jsonのhashを更新する。Release本文は公開済みの固定commitのPNGを参照し、公開URL到達と実表示を別々に確認する。
