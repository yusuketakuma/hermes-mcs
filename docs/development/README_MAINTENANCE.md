# READMEの構成とリリース時の更新

READMEは、MCS通知を使うスタッフ・導入を判断する責任者・運用担当の入口とする。
既存の画面例・機能・データの行き先・導入・安全・ドキュメント・ライセンスを維持し、
要点から詳細へ進める構成にする。未実装機能や根拠のない性能・医療効果を訴求しない。

## 見せ方のルール

- 冒頭は「誰のどんな作業に役立つか」と、目的別の短い導線を置く。
- 機能は利用者の目的と対応させる。内部モジュールの一覧はDEVELOPMENTに置く。
- GitHubで使える`details`・`summary`で画面例・目的別説明・FAQを開閉する。
- 通知先の推奨と先頭画面はSlack。DiscordとLINE WORKSの画面例は、それぞれ折りたたみに置く。
- Slackの9画面は実装に基づく完全合成の説明図を使い、実画面のキャプチャと区別する。
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
最新のCHANGELOGから対象版を読み、CHANGELOGとGitHub Releaseへのリンクを生成する。変更点・比較表・更新時の注意・リリースの説明図はCHANGELOGとRelease本文に掲載する。生成部分は手で編集しない。

```bash
python3 scripts/development/readme_release.py          # READMEの対象版とリンクを同期
python3 scripts/development/readme_release.py --check  # 同期・見直し記録・リンクを検査
python3 scripts/development/update_readme.py           # READMEの対象版・リンク（DEVELOPMENTの生成表は main-sync が main で更新）
```

`release_notes.py build`は新しいCHANGELOGとREADMEの対象版・リンクを同じ作業で更新する。
READMEのマーカーが不正なら、CHANGELOG・README・変更記録を書き換える前に停止する。
複数ファイルの書き込みや記録の移動の途中で失敗した場合は、すべてのgit差分を確認して復旧する。

## 同じ未公開版へ追補するとき

同じ未公開版への追補が依頼された場合も、`release_notes.py build`は既存versionを上書きしない。
既存の`render`処理と同じ形式で追加分を統合し、公開済みの旧版や保存済みの技術詳細・適用条件・図を消さない。
変更記録は元のJSONを保って同版のarchiveへ移し、対応表の全件数・hash・本文との対応を照合する。
その後、正本の見直し記録とCHANGELOGからREADMEの対象版・リンクを生成し、既存のcheckとexportで版・リンク・生成範囲を確認する。

CIはrunと対象SHAごとに記録し、失敗・skip・runner未割当のcancelledを成功へ読み替えない。
修正後のローカル検証と既存の成功結果を併用する場合も、元の失敗ノードと修正後の根拠を対応付ける。
重複を含む関連検証の件数を、元CIの失敗数から単純に差し引かない。ローカル相当ジョブもhosted CIの成功とは区別する。
本番適用、運用受入、公開はそれぞれreceiptで確認し、準備や合成検証の成功をその代わりにしない。
私有auditの患者情報・実投稿・秘密値を、READMEやRelease本文へ転載しない。

選択ジョブだけをローカル実行した場合は、選択ジョブのexitとrunner aggregateを分けて記録する。
未選択のジョブがあるためaggregateが非0でも、選択したジョブが失敗したとは限らない。
実行記録で理由を確認し、未選択を未実行のまま示す。hosted CIの結果へ読み替えない。
本番適用後の1周期の保存件数や所要時間は、単位と観測範囲を示す。前後比較がなければ速度改善の根拠にはしない。
通常再起動による実機PID置換と、処理中拒否の自動待機を確認した合成検証も区別する。checkout外の復旧本体がある場合は、更新ガイドの同期条件を省略しない。
保存成功が未観測の経路や残る拒否・警告は、成功した経路と区別して受入へ残す。

## 毎回リリース時に見直す部分

対象版とリンクの同期後も、README本文を実装と照合する。
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
9組の画像とハッシュ記録が一致することを検査する。LINE WORKSの画像も対応する更新手順に従い、
SVG・PNGと現行実装の表示・人承認条件を照合する。

PR・main・リリースのCIは、生成部分の一致、最新CHANGELOG版との一致、5項目の記録、
ソースの存在、READMEのファイルリンク・主要アンカーを検査する。
タグからRelease下書きを作るときはtag・CHANGELOG・READMEの版も照合する。
見直し記録の版が古ければ失敗する。CIは文章の事実性や医療的妥当性を自動保証しない。

候補版の先行レビューは`readme-review-<version>.json`へ同じschemaで記録し、
`notes`に候補であること、照合済みの範囲と残る受入条件を記載する。
候補の記録は公開・全件受入の証明ではない。`readme-review.json`は最新CHANGELOG版と
一致させ、公開版の`GENERATED:release`を手編集したり、CIを弱めたりしない。
release build時に候補記録を最終照合して正本へ反映し、
CHANGELOGとREADMEの対象版・リンクの生成・最終一致検査を同じ版で完了する。
