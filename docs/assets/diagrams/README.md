# 図の原本（diagram-design）

`docs/assets/*.svg` は、すべてこのディレクトリのHTMLが原本です。
diagram-design skill（`~/.agents/skills/diagram-design`）で作成し、配色はリポジトリ直下の
`.diagram-design`（`profile: hermes-mcs`、READMEの画面例と同じ配色）に従います。

| 原本 | 掲載SVG | 掲載先 |
|---|---|---|
| `flow-overview.html` | `docs/assets/flow-overview.svg` | README「仕組みと情報の行き先」 |
| `notification-flow.html` | `docs/assets/notification-flow.svg` | README「画面イメージ — Slack」 |
| `runtime-topology.html` | `docs/assets/runtime-topology.svg` | 利用者ガイド「実際に稼働している構成」 |
| `flow-pipeline.html` | `docs/assets/flow-pipeline.svg` | 利用者ガイド「パイプライン」 |
| `flow-journey.html` | `docs/assets/flow-journey.svg` | 利用者ガイド「症例経過」 |
| `flow-network.html` | `docs/assets/flow-network.svg` | 利用者ガイド「多職種連携」 |
| `flow-timeline.html` | `docs/assets/flow-timeline.svg` | 利用者ガイド「症例タイムライン」 |
| `flow-signals.html` | `docs/assets/flow-signals.svg` | 利用者ガイド「アラートの流れ」 |
| `analytics-overview.html` | `docs/assets/analytics-overview.svg` | 利用者ガイド「出力イメージ」 |

更新手順:

1. HTMLを編集し、`python3 ~/.agents/skills/diagram-design/scripts/self_check.py <file>` で確認する。
2. `python3 ~/.agents/skills/diagram-design/scripts/export_svg.py <file> docs/assets/<name>.svg` で書き出す。
3. 書き出したSVGから Google Fonts の `@import` を削除し、`<svg>` に `width`/`height` を付ける
   （ローカルの日本語フォントだけを使い、README表示時に外部へ取得しない）。

掲載する図は完全な架空・一般的な構成だけを描き、患者情報や実投稿を含めない。
