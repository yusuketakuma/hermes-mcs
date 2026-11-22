# v1.2.1 リリース資料

v1.2.1候補の変更記録17件を `changes/archive/1.2.1/` へ集約し、CHANGELOG・README・説明図を現在の実装へ合わせた資料です。

v1.2.1は、公開に失敗したスタンプ結果があとの通知を止めたり、過去投稿の編集が新着の通知まで抑制したりする不具合を直したバグフィックス版です。復元後照合の記録、統計の集計境界、独立実行の時計確認、OCR・CSDCの不具合も修正し、ゲートウェイ再起動中は未開始の定期処理が早めに譲るようになりました。

## 収録内容

- [release-notes-1.2.1.md](release-notes-1.2.1.md)・[release-title-1.2.1.txt](release-title-1.2.1.txt): CHANGELOGの1.2.1節からexportした公開本文とタイトル。
- [changelog-1.2.1.md](changelog-1.2.1.md): 全変更項目を写した版別詳細ファイル（1MB未満を生成時に検査）。
- [change-map.json](change-map.json): archive 17件全件のタイトル・保存先・SHA256対応表。
- notification-flow-v121（SVG・PNG）: 合成の説明図1点。実画面・測定結果ではありません。来歴・hash・描画確認は [visual-source.json](visual-source.json)。
- [ACCEPTANCE_1.2.1.md](ACCEPTANCE_1.2.1.md): 受入準備票。検証済み項目と未実行項目を分けて記録しています。

今回の文書・図の集約だけで、候補全体の合格・本番適用・公開を完了とは扱いません。公開時は PNG を `releases/download/v1.2.1/` の添付として配置し、到達と表示を確認します。
