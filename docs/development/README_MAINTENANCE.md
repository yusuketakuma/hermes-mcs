# READMEの構成とリリース時の更新

READMEは、MCS通知を使うスタッフ・導入を判断する責任者・運用担当の入口とする。
既存の画面例・機能・データの行き先・導入・安全・ドキュメント・ライセンスを維持し、
要点から詳細へ進める構成にする。未実装機能や根拠のない性能・医療効果を訴求しない。

## 見せ方のルール

- 冒頭は「誰のどんな作業に役立つか」と、目的別の短い導線を置く。
- 機能は利用者の目的と対応させる。内部モジュールの一覧はDEVELOPMENTに置く。
- GitHubで使える`details`・`summary`で画面例・目的別説明・FAQを開閉する。
- 通知先の推奨と先頭画面はSlack。DiscordとLINE WORKSの画面例は、それぞれ折りたたみに置く。
- Slackの7画面は実装に基づく完全合成の説明図を使い、実画面のキャプチャと区別する。
  画像内のボタンは説明用。SVGとPNGは生成スクリプトで一緒に更新する。
  詳細は[画面画像の更新手順](../screenshots/slack-gallery/README.md)。
- LINE WORKSは共有トークルームへの連続投稿と、本人との1:1入力・確定を実装に基づく完全合成図で示す。
  SVGとPNGを一緒に更新し、[LINE WORKS画像の更新手順](../screenshots/lineworks-gallery/README.md)で検証する。
- データの外部送信・人承認・暫定集約の境界は、折りたたみだけに隠さない。
- 導入コマンドを短くし、設定・復旧・技術資料へは目的別にリンクする。
- ASCIIの固定アンカーで主要な導線を維持する。JavaScript、フォーム、実患者のデモを埋め込まない。
- READMEはmainの機能を説明する。公開版の仕様と更新手順は該当版のCHANGELOGを参照する。

## 自動生成する部分

`<!-- BEGIN GENERATED:release -->`から`<!-- END GENERATED:release -->`の間は、
最新のCHANGELOGからversion・日付・見出し・要約・各分類の先頭1件・すべての更新時の注意を生成する。
技術詳細とその他の変更はCHANGELOGへリンクする。生成部分を手で編集しない。

```bash
python3 scripts/development/readme_release.py          # READMEの最新変更を同期
python3 scripts/development/readme_release.py --check  # 同期・見直し記録・リンクを検査
python3 scripts/development/update_readme.py           # README要約 + DEVELOPMENTの生成表
```

`release_notes.py build`は新しいCHANGELOGとREADMEの要約を同じ作業で更新する。
READMEのマーカーが不正なら、CHANGELOG・README・変更記録を書き換える前に停止する。
複数ファイルの書き込みや記録の移動の途中で失敗した場合は、すべてのgit差分を確認して復旧する。

## 毎回リリース時に見直す部分

自動要約だけで、本文全体の機能説明が更新されたことにはしない。
リリースを準備するエージェントは、新しい変更記録とソースを確認し、次の5項目を見直す。
変更がなければ、何を照合して変更不要と判断したかを記録する。

| `sections`のキー | 確認する対象 |
|---|---|
| `features` | 機能一覧・目的別説明・既定値・取得範囲・通知・アラート・人承認 |
| `demos` | Slack/Discord/LINE WORKSの表示例・ボタン・スレッド/連続投稿・本人向け回答・表示条件・合成データであること |
| `quickstart` | 必要環境・preflight・導入・成功の目安・診断・更新時の操作 |
| `safety` | ローカル/外部送信・許可設定・既読化・承認・未取得/未解析/復旧の限界 |
| `docs` | ページ内導線・相対リンク・移動/廃止したガイド・参照資料 |

見直し結果は`docs/development/readme-review.json`へ記録する。
`version`を新しい版にし、5項目それぞれの`notes`に確認内容、`sources`に照合したリポジトリ内のファイルを記載する。
内容を確認せずversionだけを上げたり、前版の確認文を機械的にコピーしたりしない。
`demos`ではSlackを先頭にし、表示例の根拠・本人向け回答・人承認条件を再確認する。
Slackの画面を変更した場合はSVG・PNGの両方を更新し、`generate_slack_gallery.py --check`で
7組の画像とハッシュ記録が一致することを検査する。LINE WORKSの画像も対応する更新手順に従い、
SVG・PNGと現行実装の表示・人承認条件を照合する。

PR・main・リリースのCIは、生成部分の一致、最新CHANGELOG版との一致、5項目の記録、
ソースの存在、READMEのファイルリンク・主要アンカーを検査する。
タグからRelease下書きを作るときはtag・CHANGELOG・READMEの版も照合する。
見直し記録の版が古ければ失敗する。CIは文章の事実性や医療的妥当性を自動保証しない。

## 参考READMEと採用した考え方

2026-10-01に一次資料を確認し、日本語の利用者向けREADMEへ次の考え方を取り入れた。
文章・画面・ロゴは転載していない。

| 参考 | 読みやすさにつながる構成 | 本リポジトリへの適用 |
|---|---|---|
| [Ollama](https://github.com/ollama/ollama/blob/main/README.md) | 短い価値説明、すぐ試せる導入、詳細ドキュメントへの導線 | 冒頭の要点、短い導入コマンド、目的別ガイド |
| [Supabase](https://github.com/supabase/supabase/blob/master/README.md) | 機能一覧、画面例、ドキュメントと仕組みの分離 | 目的と機能の対応表、通知先別の画面例、利用・運用・開発の導線 |
| [GitHubの折りたたみ](https://docs.github.com/ja/get-started/writing-on-github/working-with-advanced-formatting/organizing-information-with-collapsed-sections) | 読み手が詳細を選んで開く | 画面例・使い方・FAQ・リリース詳細の開閉 |
