# Slackの画面例ギャラリー

READMEの推奨通知先はSlack。先頭の全体像と、確認・担当、操作メニュー、
タスク入力、タスク確定、タスク一覧、患者の記録まとめ、再確認通知、初回履歴まとめの9画面を用意する。
架空の名前・投稿・数値を使った説明図で、実画面のキャプチャではない。
実データ・実投稿の匿名化例は使用しない。

PNGは日本語フォントやSVGの描画差に左右されず表示できるREADME用。
対応するSVGと生成スクリプトを編集用のソースとして保持する。
画像のボタン・メニューは操作できない。実際の配置・書体・絵文字はSlackの版や設定で変わる。

## 導入前に画面をたどる

インストール・認証情報・MCSやSlackへの接続は不要です。リンク先はすべて合成の説明画像で、次の操作を実行するものではありません。

| 見る順序 | 確認すること |
|---|---|
| [1. 元の投稿・返信・添付](01-overview.png) | 元の投稿はチャンネル、返信カードと添付は元のスレッドに届く |
| [2. 投稿ごとの確認・担当](02-notification.png) | 各カードの要約・原文・操作は、その投稿が対象 |
| [3. スタンプメニュー](03-actions.png) | メニューを開いて種類を選ぶ2段階。ほかの返信はその返信カードから操作する |
| [4. タスクを入力](04-task-form.png) → [5. 内容を確認](05-task-preview.png) | 確認画面の「確定する」までタスクを登録しない |
| [6. タスク一覧](06-task-list.png) | タスクの状態と、カードの確認済み表示を区別する |
| [7. 患者の記録まとめ](07-patient-summary.png) | 取得範囲と未取得を読み、記録がないことを対応なしと断定しない |
| [8. 原文の根拠を確認](08-urgent-notice.png) | 再確認候補は原文の人物・時点・出典とともに読む |
| [9. 初回履歴のまとめ](09-initial-history.png) | 保存済みと解析済みを区別し、解析中・保留・解析状況未取得の件数とMCSへのリンクを確認する |

### 配送保留を読む文章シナリオ

これは操作画面や実際の配送結果ではなく、保留の意味を知るための文章です。
親カードのスレッドへの配送が未確認なら、保存済みの返信は保留されます。時間がたってもチャンネルへ独立投稿しません。「返信N件を保存済み」は配送完了件数ではありません。
配送結果が不明な場合は自動再送せず、管理者が通知先の投稿と配送記録を確認します。保存済み・送信済み・結果不明を分け、保留を解除する操作をこのデモから行うことはできません。
詳しくは[元投稿・返信とカードの関係](../../guides/USER_GUIDE.md#元投稿返信とカードの関係)と、続く本文・添付の配送説明を参照してください。[導入手順](../../guides/EASY_SETUP.md)へ進む前にここまでを確認できます。

## ソースとの対応

| 画面 | 根拠 |
|---|---|
| `01-overview` | `mcs/notify/notify_cards.py`の`_split_reply_targets`・`_bind_reply_thread`と`notify_render.reply_source`（元の投稿のカードと、スレッド内の返信カード）、`docs/guides/USER_GUIDE.md`、`adapters/slack/delivery.py`（カード・添付の配送）、`mcs/notify/notify_parts.py`の`face_posts`（投稿ごとの要約と原文をカード本文へ。スタンプは下部の共通エリアに集約。添付の`📎`行と要約を含まない見出し）、`adapters/slack/presentation.py`の`render_parts`・`_folded_original`（スタンプ行は小さな補足表示、原文は`原文（N字）`の折りたたみブロック）、`present_slack.face_block_count`（収まらない投稿はスレッド本文へ） |
| `02-notification` | `mcs/notify/notify_render.py`の`card_content`・`_thread_context`・`_footer`（概要・投稿ごとの要約・状態の区画）、`adapters/slack/cards.py`（区画間の区切り線、確認・担当・タスク作成も含めた「操作を選ぶ…」、`MCSで開く`のテキストリンク）、`mcs/notify/notify_action_tokens.py`の`ACTIONS`、`mcs/stamps/stamp_format.py`（スタンプの件数・表示）、`mcs/views/message_metadata.py`（自分の投稿数） |
| `03-actions` | `notify_cards._stamp_target`・`_action_rows`・`adapters/slack/cards.py`（観測済みスタンプのエリアに送信用`static_select`を配置。メニュー展開→最新投稿への種類選択の2段階。runnerのラベルで絵文字と名前を表示し、自分の種類は`✓（取消）`）。1投稿を示す新形式では5種類だけを表示する。「🗂 別の投稿」は複数投稿をまとめた従来形式に保持する。通常操作は確認・担当・タスク作成を含め元の順で選択メニューへ。患者の検索は常に含み、全体の新着集計・自分のタスク・未確認一覧は集計カードだけ。その他の表示項目は状態・設定に依存 |
| `04-task-form` | `adapters/common/forms.py`の`modal_fields`、`adapters/slack/actions.py`の`_open_modal` |
| `05-task-preview` | `adapters/common/text.py`の`preview_text`、`adapters/slack/actions.py`の`_preview`（本人向け、確定する／取消） |
| `06-task-list` | `adapters/slack/actions.py`の`_task_blocks`・`_task_text`（本人向けの一覧。各タスクの直後にその状態操作） |
| `07-patient-summary` | `mcs/notify/notify_views.py`の`patient_summary_text`（患者の記録まとめ。暫定集約・取得範囲・原本確認・背景／療養情報の本人向け抜粋）、`mcs/views/extraction_progress.py`（現在のsource/model/prompt/layoutへ束縛した処理中・完了・要確認と完了区間数。最新20投稿まで、履歴や臨床情報の完全性とは区別）、`notify_cards._context_navigation`（最初の5節・各節から8節への循環。終日の患者コンテキスト更新が有効な場合） |
| `08-urgent-notice` | `mcs/notify/alert_view.py`の`render_parts`を完全合成のメモリDBで実行（患者本人／対象未確認、同投稿の一意な原文引用、投稿日時と緊急度の判定時刻、原本確認、未対応を断定しない記録表示）。`notify_urgent.py`の配送前検証と既存通知スレッドへの追加配送は別の安全条件。 |
| `09-initial-history` | `mcs/notify/notify_history.py`の`content`・`actions`を完全合成のメモリDBと件数で実行。履歴の保存は完了し、解析中・保留・解析状況未取得が残る例。本文・添付・スタンプの送信は描かず、現行抽出で緊急度が高い投稿と添付は件数とMCSへの案内を表示する。[新しく認識した患者の履歴](../../guides/USER_GUIDE.md#新しく認識した患者の履歴)に対応する。 |

表示例では絵文字や一部の長い行を読みやすく簡略化する。
`03-actions`だけは操作者1人とMCSアカウントを設定し、スタンプ送信が有効な例。01・02・04〜08はスタンプ送信を設定していない例で、スタンプの読取り表示は維持する。09の初回履歴まとめにはスタンプを表示しない。
スタンプの並びは`👀見ました`・`🙆承知`・`🙏感謝`・`👍いいね`・`✅完了`。この画面では自分の`🙆承知`に`✓`を付けている。送信用メニューと展開した選択肢を並べた説明図で、実端末の配置はSlackに従う。変更後の実機での展開・連続操作・選択状態は未確認。[適用前の確認事項](../../specs/stamp-management.md#画面から受付まで)を参照する。
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
2026-10-23の返信カード（layout 4、既定で有効）では、`01-overview`を元の投稿のカードとスレッド内の返信カード・添付に、`02-notification`を元の投稿のカードと返信カードの2枚に描き直し、同じQuick Look＋ImageMagickの手順でPNGを再生成した。ほかの6画面は変更していない。
小型表示の候補では`01-overview`・`02-notification`・`03-actions`の通常操作をメニューへ集約し、`03-actions`に3組の小型操作欄を描いた。生成器からSVGを更新し、同じQuick Look＋ImageMagickの手順でPNGを再生成する。
2026-10-21の識別性修正では、実モバイルで親指に固定された小型部品を標準の名前付きボタンへ置き換え、`03-actions`だけを再生成した。6個目の「🗂 別の投稿」は対象選択であり、スタンプは5種類。
絵文字の描画は環境によって異なり、実アプリの表示とは区別する。
`scripts/development/generate_slack_gallery.py`の説明用の例とレイアウトを修正し、SVGとPNGを一緒に更新する。
9画面の文字切れ・重なり・説明・本人向け表示・人承認条件を確認する。
PNGは生成時に一時ファイルのCRC・全チャンク・画像データを検査してから置き換える。
`assets.json`は対応するSVG/PNGのSHA-256記録。手で更新せず、生成器の`asset_hashes()`から更新する（`--png`でも更新される）。
CIはSVGの生成結果、PNGの完結性・サイズ・画像データ、両方のハッシュを検査する。
リリース時は`docs/development/readme-review.json`の`demos`へ確認した内容とソースを記録する。

2026-10-21の集約では、カード下部へ観測スタンプを集め、送信はメニュー展開→種類選択へ変更。`01-overview`・`02-notification`・`03-actions`を同じ生成手順で更新した。

同日の表示調整では、重複するスタンプの合計と送信メニューの見出しを省略し、投稿別の件数・押下者・観測時点とメニューを維持。`01-overview`・`02-notification`・`03-actions`を再生成した。

2026-10-23の返信カードhotfixでは、親の返信件数を「保存済み」と表示し、単投稿のスタンプメニューから「別の投稿」を除いた。01〜03のSVG/PNGを既存のQuick Look＋ImageMagick手順で再生成し、保存件数を配送完了と誤認させない説明へ合わせた。

初回履歴まとめの`09-initial-history`を追加した。保存と解析の状態を別に示し、解析中・保留・解析状況未取得が残る間は解析完了と表示しない。PNGは既存のQuick Look＋ImageMagickの手順でこの1枚だけ描画し、01〜08のSVG・PNGは変更していない。
