# リリースノートの共通ルール

CHANGELOG.mdを正本とし、GitHub Releaseのタイトル・本文を同じ内容から生成する。
対象読者は通知を利用するスタッフと、導入・保守する担当者。
各版は公開当時の仕様を記載し、現在の仕様と混同しない。

## 段落と順番

| 見出し | 記載する内容 |
|---|---|
| 新機能 | これまでできなかった操作・表示・設定を追加した変更 |
| 改善 | 既存機能の使いやすさ、処理、説明、運用を改善した変更 |
| 不具合修正 | 発生条件と、修正後の動作。セキュリティ修正もここに含める |
| 動作・設定の変更 | 既定値、互換性、収集範囲、通知、既読化、承認条件の変更 |
| 更新時の注意 | 再起動・再設定・移行・有効化・適用条件。不要なら不要と明記 |
| 技術詳細 | 内部用語、ファイル、schema、根拠、計測条件を折りたたみに記載 |

変更がない分類は表示しない。「更新時の注意」と「技術詳細」は必須。
version見出しは `## [X.Y.Z] — YYYY-MM-DD`、新しい順に並べる。
冒頭は100文字以内の太字の見出しと、その版の要約。
更新前に必要な操作やセキュリティ上の注意は、冒頭にも示す。

## 文章と見せ方

- 各項目は太字の短いタイトルを置き、次の行で利用者への影響を説明する。
- 同じ利用目的の関連変更は1項目にまとめ、説明は原則1〜3文。変更ごとの有効化・安全・更新条件は省略しない。
- 見出しと本文、項目同士の間に空行を入れる。
- 有効化の操作、既定値、対象範囲、上限、例外条件は該当項目に明記する。
- 「実装した処理」だけでなく「何ができる／使いやすくなる／直るか」を書く。
- 本文は利用者の目的ごとに関連変更をまとめ、内部ID・ファイル名・SDK・テスト・検証経緯は末尾の技術詳細へ置く。機能の既定値・有効化の条件・安全上の制限・必要な更新操作は本文にも残す。
- 同じ再起動や設定確認を項目ごとに繰り返さず、「更新時の注意」でまとめて案内する。長い引数一覧はガイドへつなぐ。
- 性能や精度の数値は根拠と測定条件がある場合だけ書く。
- AIが投稿から抽出した「完了」と、人が確認したタスク完了を区別する。
- 患者情報・秘密情報・実投稿由来の例を入れない。例は完全合成のみ。
- 未実装の計画、別versionの機能、現在の既定値を過去版の変更に混ぜない。
- READMEの変更や内部整理は改善に記載できる。運用操作を変更しない場合も明記する。

## 変更の比較と視覚資料

主要変更は「以前 → 今回 → 利用者のメリット」を対応付け、要約直後に最大3件程度の比較表を置く。軽微な版は短い文章にする。表・本文をそのまま重複転載せず、必要な設定・更新操作・安全条件は本文に残す。

重要な新機能や操作変更は、当時の比較元tag・対象tagとコミット差分に一致する図を、素材と検証手段があれば最低1点準備する。UIは同条件の実画面比較、手順はフロー、連携は情報の流れを優先する。旧画面を推測しない。説明図は実画面と区別し、未測定の効果や操作回数を載せない。図は1枚1メッセージ、本文で1〜3点、補足・開発者向けは折りたたむ。altと直下の短い説明を付け、色だけに頼らず、390px程度の幅でもラベルを確認する。

既存のSVG原稿・PNG生成方式を再利用し、Release用はPNGを基本とする。公開URLの到達とRelease実画面での表示を別々に確認する。画像公開が禁止・未承認ならファイルと配置・参照手順まで準備し、ローカルパス・未公開URLをRelease本文へ埋め込まない。画像なしでも本文で変更と必要操作が分かることを確認する。版ごとの準備・公開条件は[視覚資料の配置手順](../releases/VISUAL_PREPARATION.md)を参照する。

## 開発時の記録

実行時の挙動を変更する作業では、同じcommitに`changes/<識別子>.json`を追加する。
形式は`changes/README.md`。実装エージェントが日本語の説明を作り、生成器が集約する。
categoryは`added/changed/fixed/breaking/security`。
securityは「不具合修正」に分類し、冒頭に更新前の注意を出す。
PRのCIは実行コードの変更・削除に変更記録を要求する。
文書のみ・テストのみの作業には記録を強制しない。

## リリース準備

リリースを依頼されたエージェントは、未リリースの記録を確認して見出しと要約を作る。
version・日本時間の日付は明示的に指定する。暗黙にversionを上げない。

```bash
python3 scripts/development/release_notes.py check
python3 scripts/development/release_notes.py build \
  --version X.Y.Z --date YYYY-MM-DD \
  --headline 'その版で何が変わるか' \
  --summary '利用者への影響と適用範囲を説明する一文。'
# README本文の5項目をソースと照合し、docs/development/readme-review.jsonを新versionへ更新する
python3 scripts/development/update_readme.py
python3 scripts/development/readme_release.py --check
python3 -m unittest discover -s tests/release -v
python3 scripts/development/release_notes.py export --version X.Y.Z \
  --output /tmp/release-notes.md --title-output /tmp/release-title.txt
```

`build`はCHANGELOGとREADMEの最新変更要約を更新し、入力記録を`changes/archive/<version>/`へ移す。
既存versionの上書き、空の変更記録、手書きUnreleased、形式不正では停止する。
生成後にソースと説明を照合する。CIは文章の事実性まで保証しない。
毎回、README本文の機能・画面例・導入・安全・導線を見直し、確認内容と根拠を
`docs/development/readme-review.json`の新しいversionに記録する。変更不要でも照合結果を残す。
見直し記録が旧版のままならCIが停止する。詳細は[README運用](README_MAINTENANCE.md)。
途中失敗した場合はCHANGELOG・README・changesのgit差分を確認し、すべてを復旧して再実行する。

## GitHubとの自動同期

`.github/workflows/release-notes.yml`が次を行う。

1. PR・main更新・リリース時に、全versionの見出し・順番・空段落・必須段落・折りたたみを検査する。
   README要約・見直し記録・参照リンクも検査し、tagではversionの一致も確認する。
2. `vX.Y.Z`タグのpushではタグ内のCHANGELOGから新しいRelease下書きを作る。公開はしない。
3. main更新、既存Releaseの公開・編集、手動の`sync_existing`実行では、mainのCHANGELOGから既存Releaseのタイトル・本文を同期する。
4. 全対象を事前確認し、内容が同じReleaseは書き換えない。途中の競合編集を検出した場合は停止する。
5. 更新後に本文一致を読み直して検証する。tag・公開日時・draft・prerelease・添付資産を更新しない。

公開済み版の文章改善を依頼された場合は、CHANGELOGの対象版を正本として編集し、関連項目を利用者の目的でまとめられる。archiveの当時の変更記録は保持し、全件の仕様・設定条件・根拠は技術詳細の折りたたみに残す。過去版やtagを付け替えず、README要約を既存生成器で更新し、Releaseは同じCHANGELOGからexport・同期する。

本文更新のAPIには、競合確認済みの既存tag名も同じ値で明示し、下書きとtagの紐付けを保持する。
API応答と再取得の両方で識別・公開状態を検査し、同期エラーはworkflowを失敗させる。

標準のGitHub Actions用GITHUB_TOKENとcontents:writeを使う。追加のLLM API・API課金は不要。
GitHubの通常のActions利用条件は適用される。repo権限やbranch保護は変更しない。
旧tagのコードにはこの生成器がないため、過去版の変更はmainからの同期を使う。
対応するCHANGELOGがない既存の安定版は、勝手に作文せずエラーとする。
プレリリースtagと自動version決定は対象外。
公開済みRelease本文を手動編集する場合も、先にCHANGELOGを修正する。
本文の手動編集だけでは、自動同期でCHANGELOGの内容へ戻る。

## 過去記録の移行

2026-10-01に、v1.0.0〜v1.0.8の9版をこの形式へ再編集した。
移行元のmainは`c0e1c0ff0a6dd6102909289c9c09454dee69567e`。
編集前のCHANGELOGとGitHub Release本文は`docs/releases/archive/`に保存。
公開当時の詳細は各版の技術詳細にも保持する。
v1.0.3の夜間20分収集は歴史上の変更として残し、後の廃止を明記した。
v1.0.5のgateway再起動不要と、他の版の再起動必須を区別している。

取り消す場合は変更commitをrevertし、元のCHANGELOGを正本に同期する。
元のReleaseタイトル・本文を厳密に戻す必要がある場合は保存したJSONを参照する。
業務DB・モデル・MCSデータの移行はない。

## 設計の参考

2026-10-01に、[VS Code](https://code.visualstudio.com/updates/v1_140)のハイライト、
[Codex](https://github.com/openai/codex/releases/tag/rust-v0.159.0)の機能分類と根拠、
[GitHub CLI](https://github.com/cli/cli/releases/tag/v2.102.0)の更新注意、
[Hermes Agent](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.24)の対象範囲と更新方法、
[Keep a Changelog日本語版](https://keepachangelog.com/ja/1.1.0/)を比較して構成した。
API仕様は[GitHub Releases公式文書](https://docs.github.com/en/rest/releases/releases#update-a-release)を参照。
