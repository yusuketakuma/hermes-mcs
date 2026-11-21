# v1.2.0 リリース資料

v1.2.0候補の変更記録440件を `changes/archive/1.2.0/` へ集約し、CHANGELOG・README・説明図を現在の実装へ合わせた資料です。

v1.2.0は、通知カードの確認・担当をボタンにし、処理状況は問題があるときだけ表示する版です。クラウドAIは用途ごとのモデル（再抽出・意味解析は Haiku 5.5 medium、品質修復・患者コンテキストは Sonnet 5.5 high）で送り、新規導入では施設の責任者が確認を記録したサービスにだけ送信します。抽出・添付OCR・患者コンテキスト・更新と復旧の不具合も多数修正しています。

## 収録内容

- [release-notes-1.2.0.md](release-notes-1.2.0.md)・[release-title-1.2.0.txt](release-title-1.2.0.txt): CHANGELOGの1.2.0節からexportした公開本文とタイトル。
- [changelog-1.2.0.md](changelog-1.2.0.md): 公開本文の文字数上限対策として、全変更項目を写した版別詳細ファイル（1MB未満を生成時に検査）。
- [change-map.json](change-map.json): archive 440件全件のタイトル・保存先・SHA256対応表。
- notification-card-v120（SVG・PNG）・cloud-ai-v120（SVG・PNG）: 合成の説明図2点。実画面・測定結果ではありません。来歴・hash・描画確認は [visual-source.json](visual-source.json)。
- [ACCEPTANCE_1.2.0.md](ACCEPTANCE_1.2.0.md): 受入準備票。検証済み項目と未実行項目を分けて記録しています。

今回の文書・図の集約だけで、候補全体の合格・本番適用・公開を完了とは扱いません。公開時は PNG を `releases/download/v1.2.0/` の添付として配置し、到達と表示を確認します。
