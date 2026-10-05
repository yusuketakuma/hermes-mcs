# Slackの画面例ギャラリー

READMEの推奨通知先はSlack。先頭の全体像と、確認・担当、操作メニュー、
タスク入力、タスク確定、タスク一覧、患者の記録まとめの7画面を用意する。
架空の名前・投稿・数値を使った説明図で、実画面のキャプチャではない。
実データ・実投稿の匿名化例は使用しない。

PNGは日本語フォントやSVGの描画差に左右されず表示できるREADME用。
対応するSVGと生成スクリプトを編集用のソースとして保持する。
画像のボタン・メニューは操作できない。実際の配置・書体・絵文字はSlackの版や設定で変わる。

## ソースとの対応

| 画面 | 根拠 |
|---|---|
| `01-overview` | `docs/guides/USER_GUIDE.md`、`adapters/slack/delivery.py`（カード・本文・添付の配送）、`mcs/notify/notify_render.py`の`_message_post`（スレッド投稿は`↳`見出し・📋 要約・スタンプ行・本文を区切り線で並べる）、`mcs/notify/notify_cards.py`（添付の`📎`行） |
| `02-notification` | `mcs/notify/notify_render.py`の`_card_content`・`_thread_context`・`_footer`（概要・投稿ごとの要約・状態の区画）、`adapters/slack/cards.py`（区画間の区切り線、`確認する`・`担当する`・`タスク作成`のボタンと「操作を選ぶ…」、`MCSで開く`のテキストリンク）、`mcs/notify/notify_cards.py`の`_ACTIONS`、`mcs/views/message_metadata.py`（スタンプ件数と自分の投稿数） |
| `03-actions` | 同カード実装（ボタン以外の操作を選択メニューへ。表示項目は状態・設定に依存） |
| `04-task-form` | `adapters/common/text.py`の`modal_fields`、`adapters/slack/actions.py`の`_open_modal` |
| `05-task-preview` | 同`preview_text`と`_preview`（本人向け、確定する／取消） |
| `06-task-list` | `adapters/slack/actions.py`の`_task_blocks`・`_task_text`（本人向けの一覧。各タスクの直後にその状態操作） |
| `07-patient-summary` | `mcs/notify/notify_views.py`の`patient_summary_text`（患者の記録まとめ。暫定集約・取得範囲・原本確認） |

表示例では絵文字や一部の長い行を読みやすく簡略化する。
ダッシュボードや患者管理画面は描かない。Slackの`/mcs`コマンドは本文と導入ガイドで説明し、このカード操作の画面例には含めない。

## 更新

```bash
python3 scripts/development/generate_slack_gallery.py --png
python3 scripts/development/generate_slack_gallery.py --check
python3 scripts/development/readme_release.py --check
```

通常の`--png`による再生成にはInkscapeと日本語フォントが必要。
今回はmacOS標準Quick Lookで一時的に正方形へ拡張したSVGを描画し、
既存ImageMagickで上端から元の比率へ切り出した。公開SVGのサイズは変更していない。
`scripts/development/generate_slack_gallery.py`の説明用の例とレイアウトを修正し、SVGとPNGを一緒に更新する。
7画面の文字切れ・重なり・説明・本人向け表示・人承認条件を確認する。
PNGは生成時に一時ファイルのCRC・全チャンク・画像データを検査してから置き換える。
`assets.json`は対応するSVG/PNGのSHA-256記録。手で更新せず、生成器の`asset_hashes()`から更新する（`--png`でも更新される）。
CIはSVGの生成結果、PNGの完結性・サイズ・画像データ、両方のハッシュを検査する。
リリース時は`docs/development/readme-review.json`の`demos`へ確認した内容とソースを記録する。
