# Slackの画面例ギャラリー

READMEの推奨通知先はSlack。先頭の全体像と、確認・担当、操作メニュー、
タスク入力、タスク確定、タスク一覧、患者の記録まとめ、再確認通知の画面と、旧版で届いた初回履歴まとめの画面を掲載する。
架空の名前・投稿・数値を使った説明図で、実画面のキャプチャではない。
実データ・実投稿の匿名化例は使用しない。

PNGは日本語フォントやSVGの描画差に左右されず表示できるREADME用。
対応するSVGと生成スクリプトを編集用のソースとして保持する。
カード見出しの日時・投稿数と同じ欄に、投稿ごとの短い処理状態と、カードを初めて作成した時点の文脈の進捗を表示する。文脈の進捗は`👤 73%`のように示し、AnthropicやOpenAIのアイコンは表示しない。ローカル・Jevのアイコンは、その投稿で実行を確認できた場合に表示する。

01は元投稿の処理が完了し、問題がないため処理状況の欄を出さない例。後から作成した返信カードは投稿の処理中で「◐ 未完了」だけを表示する。02は元投稿が「◐ 未完了」、返信が「? 状況未取得」で文脈5%、03は「! 要確認」で文脈・サービスの欄をすべて表示する例。数値と時系列はすべて架空で、実際の処理結果ではない。投稿の処理が未完了・不明の例では部分的な抽出結果を載せず、要約の待ち状態を示す。

文脈の割合はカード作成時点の記録で、後の進捗だけを理由に既存カードを更新しない。後から作る返信カードには、その返信カードの作成時点の割合を表示する。新しい入力が加わると対象範囲と処理段階が変わり、割合が下がることがある。01・02の返信は、新しい入力のローカル処理と品質確認がまだ揃わず5%の例で、先に作成した元投稿カードの73%は変えない。投稿の処理完了や文脈の進捗は、医療上の情報や対応がすべて揃ったことを意味しない。

◐は投稿の処理が未完了、!は要確認、?は状況未確認を表し、送信前の待機と実行中を推測で区別しない。✓は実行を確認済み、●は複数投稿のうち使用を確認できたことを表す。投稿の処理状態・サービスの実行状態・文脈の進捗は区別する。カード最上部のアイコンは、カード形式6以降のカードでは状態に合わせて💬（未確認・担当なし）・👤（担当中）・✅（今の内容が確認済み）になる。確認は確認ボタンの記録で、作業の完了ではない。01は💬、02の元投稿は担当・確認済みの✅の例。

01は元投稿と返信のそれぞれに、架空の画像とPDFをまとめて表示した例です。ファイルのプレビューと操作欄との厳密な順序は実Slackで未確認で、画像内の並びを実UIの保証には使いません。送信する全集合と、非公開の準備・表示完了・結果不明の区別は[Slackの原文と添付の配送](../../specs/semantic-evaluation.md#slackの原文と添付の配送)に従います。

画像のボタン・メニューは操作できない。実際の配置・書体・絵文字はSlackの版や設定で変わる。

## 導入前に画面をたどる

インストール・認証情報・MCSやSlackへの接続は不要です。リンク先はすべて合成の説明画像で、次の操作を実行するものではありません。

| 見る順序 | 確認すること |
|---|---|
| [1. 元の投稿・返信・添付](01-overview.png) | 元の投稿はチャンネル、返信カードは元のスレッドへ。添付は各投稿のカードにまとめる |
| [2. 投稿ごとの確認・担当](02-notification.png) | 各カードの要約・原文・操作は、その投稿が対象 |
| [3. スタンプメニュー](03-actions.png) | メニューを開いて種類を選ぶ2段階。ほかの返信はその返信カードから操作する |
| [4. タスクを入力](04-task-form.png) → [5. 内容を確認](05-task-preview.png) | 確認画面の「確定する」までタスクを登録しない |
| [6. タスク一覧](06-task-list.png) | タスクの状態と、カードの確認済み表示を区別する |
| [7. 患者の記録まとめ](07-patient-summary.png) | 取得範囲と未取得を読み、記録がないことを対応なしと断定しない |
| [8. 原文の根拠を確認](08-urgent-notice.png) | 再確認候補は原文の人物・時点・出典とともに読む |
| [9. 旧版で届いた初回履歴のまとめ](09-initial-history.png) | 更新前に配信されたカードの例。現在は初回履歴の通知を送らない |

### 配送保留を読む文章シナリオ

これは操作画面や実際の配送結果ではなく、保留の意味を知るための文章です。
親カードのスレッドへの配送が未確認なら、保存済みの返信は保留されます。時間がたってもチャンネルへ独立投稿しません。「返信N件を保存済み」は配送完了件数ではありません。
配送結果が不明な場合は自動再送せず、管理者が通知先の投稿と配送記録を確認します。保存済み・送信済み・結果不明を分け、保留を解除する操作をこのデモから行うことはできません。
詳しくは[元投稿・返信とカードの関係](../../guides/USER_GUIDE.md#元投稿返信とカードの関係)と、続く本文・添付の配送説明を参照してください。[導入手順](../../guides/EASY_SETUP.md)へ進む前にここまでを確認できます。

## ソースとの対応

| 画面 | 根拠 |
|---|---|
| `01-overview` | `mcs/notify/notify_cards.py`の`_split_reply_targets`・`_bind_reply_thread`と`notify_render.reply_source`（元の投稿のカードと、スレッド内の返信カード）、`docs/guides/USER_GUIDE.md`、`adapters/slack/delivery.py`の`_attachment_part`・`_file_group`（非公開の準備と、同じカードへのファイル群表示）、`mcs/notify/notify_parts.py`の`face_posts`（投稿ごとの要約と原文をカード本文へ。スタンプは下部の共通エリアに集約。見出しの添付件数と、要約を含まない投稿見出し）、`adapters/slack/presentation.py`の`render_parts`・`_folded_original`（スタンプ行は小さな補足表示、原文は`原文（N字）`の折りたたみブロック）、`present_slack.face_block_count`（収まらない投稿はスレッド本文へ）、`mcs/notify/processing_route.py`の`context_progress_text`・`compact_header`と`notify_card_renders._frozen_context_progress`（投稿処理とは別に、初回カード作成時の文脈の進捗を保持） |
| `02-notification` | `mcs/notify/notify_render.py`の`card_content`・`_thread_context`・`_footer`（概要・投稿ごとの要約・状態の区画）、`adapters/slack/cards.py`（区画間の区切り線、`確認する`・`担当する`のボタンと、タスク作成などの「操作を選ぶ…」、`MCSで開く`のテキストリンク）、`notify_render.STATE_ICONS`（見出しのアイコン）、`structured_view.structured_lines`の`request_first`（`📌 依頼`を要約の先頭に）、`mcs/notify/notify_action_tokens.py`の`ACTIONS`、`mcs/stamps/stamp_format.py`（スタンプの件数・表示）、`mcs/views/message_metadata.py`（自分の投稿数） |
| `03-actions` | `notify_cards._stamp_target`・`_action_rows`・`adapters/slack/cards.py`（観測済みスタンプのエリアに送信用`static_select`を配置。メニュー展開→最新投稿への種類選択の2段階。runnerのラベルで絵文字と名前を表示し、自分の種類は`✓（取消）`）。1投稿を示す新形式では5種類だけを表示する。「🗂 別の投稿」は複数投稿をまとめた従来形式に保持する。`確認する`・`担当する`はボタン、タスク作成などの通常操作は元の順で選択メニューへ。患者の検索は常に含み、全体の新着集計・自分のタスク・未確認一覧は集計カードだけ。その他の表示項目は状態・設定に依存 |
| `04-task-form` | `adapters/common/forms.py`の`modal_fields`、`adapters/slack/actions.py`の`_open_modal` |
| `05-task-preview` | `adapters/common/text.py`の`preview_text`、`adapters/slack/actions.py`の`_preview`（本人向け、確定する／取消） |
| `06-task-list` | `adapters/slack/actions.py`の`_task_blocks`・`_task_text`（本人向けの一覧。各タスクの直後にその状態操作） |
| `07-patient-summary` | `mcs/notify/notify_views.py`の`patient_summary_text`（本人向けの未完了タスク・背景抜粋・取得範囲）、`notify_actions._context_navigation`（患者情報から最初の5節へ進む操作）、`mcs/views/patient_search.py`・`notify_views.patient_search_view`・`notify_actions._patient_search`（同じ患者の別スレッド・添付OCRを検索し、原資料・続きを検索・条件変更へ進む本人向け表示） |
| `08-urgent-notice` | `mcs/notify/alert_view.py`の`render_parts`を完全合成のメモリDBで実行（患者本人／対象未確認、同投稿の一意な原文引用、投稿日時と緊急度の判定時刻、原本確認、未対応を断定しない記録表示）。`notify_urgent.py`の配送前検証と既存通知スレッドへの追加配送は別の安全条件。 |
| `09-initial-history` | `mcs/notify/notify_history.py`の`content`・`actions`を完全合成のメモリDBと件数で実行。旧版のカードに、履歴の保存完了と、解析中・保留・解析状況未取得の件数が表示された例。本文・添付・スタンプの送信は描かず、現行抽出で緊急度が高い投稿と添付は件数とMCSへの案内を表示する。[新しく認識した患者の履歴](../../guides/USER_GUIDE.md#新しく認識した患者の履歴)に対応する。 |

表示例では絵文字や一部の長い行を読みやすく簡略化する。
`03-actions`だけは操作者1人とMCSアカウントを設定し、スタンプ送信が有効な例。01・02・04〜08はスタンプ送信を設定していない例で、スタンプの読取り表示は維持する。09の初回履歴まとめにはスタンプを表示しない。
スタンプの並びは`👀見ました`・`🙆承知`・`🙏感謝`・`👍いいね`・`✅完了`。この画面では自分の`🙆承知`に`✓`を付けている。送信用メニューと展開した選択肢を並べた説明図で、実端末の配置はSlackに従う。[適用前の確認事項](../../specs/stamp-management.md#画面から受付まで)を参照する。
両方の本人情報を設定すると送信は既定で有効になり、`stamps.enabled=false`で無効にできる。画像の生成器は設定を読み込まず、この違いを説明用の例として描く。
ダッシュボードや患者管理画面は描かない。Slackの`/mcs`コマンドは本文と導入ガイドで説明し、このカード操作の画面例には含めない。

## 更新

```bash
python3 scripts/development/generate_slack_gallery.py --png
python3 scripts/development/generate_slack_gallery.py --check
python3 scripts/development/readme_release.py --check
```

通常の`--png`による再生成にはInkscapeと日本語フォントが必要。macOSでは `--png --renderer sips` で標準のsipsを明示して使える。どちらも同じPNG検査を通す。
SVGとPNGは一緒に更新します。`--check` は生成元との一致と、PNGの寸法・CRC・画像データ、`assets.json` に記録したハッシュを検査します。画像を開き、文字切れ、画面上の操作、本人向け表示、人による確認が必要な箇所も確認してください。
