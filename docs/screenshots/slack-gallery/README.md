# Slackの画面例ギャラリー

READMEの推奨通知先はSlack。先頭の全体像と、確認・担当、操作メニュー、
タスク入力、タスク確定、タスク一覧、患者の記録まとめ、再確認通知の8画面を用意する。
架空の名前・投稿・数値を使った説明図で、実画面のキャプチャではない。
実データ・実投稿の匿名化例は使用しない。

PNGは日本語フォントやSVGの描画差に左右されず表示できるREADME用。
対応するSVGと生成スクリプトを編集用のソースとして保持する。
画像のボタン・メニューは操作できない。実際の配置・書体・絵文字はSlackの版や設定で変わる。

## ソースとの対応

| 画面 | 根拠 |
|---|---|
| `01-overview` | `docs/guides/USER_GUIDE.md`、`adapters/slack/delivery.py`（カード・添付の配送）、`mcs/notify/notify_parts.py`の`face_posts`（投稿ごとの要約の下にスタンプ行と原文をカード本文へ。返信にも同じ順。添付の`📎`行と要約を含まない見出し）、`adapters/slack/cards.py`の`render_parts`・`_folded_original`（スタンプ行は小さな補足表示、原文は`原文（N字）`の折りたたみブロック）、`present_slack.face_block_count`（収まらない投稿はスレッド本文へ） |
| `02-notification` | `mcs/notify/notify_render.py`の`card_content`・`_thread_context`・`_footer`（概要・投稿ごとの要約・状態の区画）、`adapters/slack/cards.py`（区画間の区切り線、`確認する`・`担当する`・`タスク作成`のボタンと「操作を選ぶ…」、`MCSで開く`のテキストリンク）、`mcs/notify/notify_action_tokens.py`の`ACTIONS`、`mcs/stamps/stamp_format.py`（スタンプの件数・表示）、`mcs/views/message_metadata.py`（自分の投稿数） |
| `03-actions` | `notify_cards._stamp_target`・`_action_rows`・`adapters/slack/cards.py`（最新の表示投稿に5種類のスタンプを1回押しで依頼。`adapters/common/text.py`の`stamp_icon`で絵文字だけを表示し、自分のスタンプを強調する。もう一度押すと取消）。ほかの操作はボタン行の順に選択メニューへ。未完了タスクがあれば`タスク一覧`が先頭。患者の検索は常に含み、全体の新着集計・自分のタスク・未確認一覧は集計カードだけ。別の投稿を選ぶ`MCSスタンプ`フォームも`誤りを報告`の後に残る。その他の表示項目は状態・設定に依存 |
| `04-task-form` | `adapters/common/text.py`の`modal_fields`、`adapters/slack/actions.py`の`_open_modal` |
| `05-task-preview` | 同`preview_text`と`_preview`（本人向け、確定する／取消） |
| `06-task-list` | `adapters/slack/actions.py`の`_task_blocks`・`_task_text`（本人向けの一覧。各タスクの直後にその状態操作） |
| `07-patient-summary` | `mcs/notify/notify_views.py`の`patient_summary_text`（患者の記録まとめ。暫定集約・取得範囲・原本確認・背景／療養情報の本人向け抜粋）、`mcs/views/extraction_progress.py`（現在のsource/model/prompt/layoutへ束縛した処理中・完了・要確認と完了区間数。最新20投稿まで、履歴や臨床情報の完全性とは区別）、`notify_cards._context_navigation`（最初の5節・各節から8節への循環。夜間機能が有効な場合） |
| `08-urgent-notice` | `mcs/notify/alert_view.py`の`render_parts`を完全合成のメモリDBで実行（患者本人／対象未確認、同投稿の一意な原文引用、投稿日時と緊急度の判定時刻、原本確認、未対応を断定しない記録表示）。`notify_urgent.py`の配送前検証と既存通知スレッドへの追加配送は別の安全条件。 |

表示例では絵文字や一部の長い行を読みやすく簡略化する。
`03-actions`だけは操作者1人とMCSアカウントを設定し、スタンプ送信が有効な例。ほかの7画面はスタンプ送信を設定していない例で、スタンプの読取り表示は維持する。
絵文字の意味は左から`👀見ました`・`🙆承知`・`🙏感謝`・`👍いいね`・`✅完了`。この画面では自分の`🙆承知`を強調している。
両方の本人情報を設定すると送信は既定で有効になり、`stamps.enabled=false`で無効にできる。画像の生成器は設定を読み込まず、この違いを説明用の例として描く。
ダッシュボードや患者管理画面は描かない。Slackの`/mcs`コマンドは本文と導入ガイドで説明し、このカード操作の画面例には含めない。

## 更新

```bash
python3 scripts/development/generate_slack_gallery.py --png
python3 scripts/development/generate_slack_gallery.py --check
python3 scripts/development/readme_release.py --check
```

通常の`--png`による再生成にはInkscapeと日本語フォントが必要。
従来画像はmacOS標準Quick LookとImageMagickで描画した。2026-10-08の01〜03・07の項目順と患者まとめの修正は、
既存のlibrsvg/Cairoとインストール済み日本語フォントを隔離環境で使って再描画した。
2026-10-17の`01-overview`は要約・スタンプ・本文を並べるため高さを1200pxへ広げ、
macOS標準Quick LookとImageMagickで1650×1800pxのPNGを描画した。
2026-10-17のボタン再編では`03-actions`と`07-patient-summary`を更新した。
2026-10-18のカード配置変更（要約・スタンプ・原文の折りたたみをカードへ、添付・後から届いた返信の新着案内・収まらない本文をスレッドへ）では`01-overview`・`02-notification`・`03-actions`を更新し、`02-notification`の高さを1060pxへ広げ、同じQuick Look＋ImageMagickの手順で描画した。
Inkscapeのない環境では一時コピーのSVG viewportだけを正方形へ広げてQuick Lookで描画し、
元の1650px幅・縦横比の範囲をImageMagickで切り出した。SVG本体の配置は変えず、
`verify_png()`と`asset_hashes()`でPNGの完結性・寸法・SVG/PNGのハッシュを検査・同期した。
2026-10-20の`03-actions`はMCSスタンプを有効にした場合のメニューを追加し、同じQuick Look＋ImageMagickの手順で描画した。ほかの7画面は変更していない。
同日の1回押しへの変更では、`03-actions`にスタンプ列と本人状態の強調表示を追加し、投稿を選ぶフォームもメニューに残した。ほかの7画面は変更していない。
絵文字の描画は環境によって異なり、実アプリの表示とは区別する。
`scripts/development/generate_slack_gallery.py`の説明用の例とレイアウトを修正し、SVGとPNGを一緒に更新する。
8画面の文字切れ・重なり・説明・本人向け表示・人承認条件を確認する。
PNGは生成時に一時ファイルのCRC・全チャンク・画像データを検査してから置き換える。
`assets.json`は対応するSVG/PNGのSHA-256記録。手で更新せず、生成器の`asset_hashes()`から更新する（`--png`でも更新される）。
CIはSVGの生成結果、PNGの完結性・サイズ・画像データ、両方のハッシュを検査する。
リリース時は`docs/development/readme-review.json`の`demos`へ確認した内容とソースを記録する。
