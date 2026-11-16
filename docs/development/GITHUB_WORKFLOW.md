# GitHub の使い方（ラベル・版・issue・PR）

この文書は、hermes-mcs の今後の更新を GitHub の issue・PR・ラベルで管理するための約束です。
ラベルの定義の正本は [`.github/labels.json`](../../.github/labels.json) です。

## まず見る場所

| 知りたいこと | 開く場所 |
|---|---|
| 全体の計画 | ラベル [`roadmap`](https://github.com/yusuketakuma/hermes-mcs/issues?q=is%3Aopen+label%3Aroadmap) の issue |
| 自分（Yusukeさん）がやること | [`要オーナー対応`](https://github.com/yusuketakuma/hermes-mcs/issues?q=is%3Aopen+label%3A%E8%A6%81%E3%82%AA%E3%83%BC%E3%83%8A%E3%83%BC%E5%AF%BE%E5%BF%9C) |
| 今すぐ出すもの | [`即時適用`](https://github.com/yusuketakuma/hermes-mcs/issues?q=is%3Aopen+label%3A%E5%8D%B3%E6%99%82%E9%81%A9%E7%94%A8) |
| 次の版に入るもの | [`v1.1.3`](https://github.com/yusuketakuma/hermes-mcs/issues?q=is%3Aopen+label%3Av1.1.3)・[`v1.1.4`](https://github.com/yusuketakuma/hermes-mcs/issues?q=is%3Aopen+label%3Av1.1.4) |
| Mac での作業が要るもの | [`要実機作業`](https://github.com/yusuketakuma/hermes-mcs/issues?q=is%3Aopen+label%3A%E8%A6%81%E5%AE%9F%E6%A9%9F%E4%BD%9C%E6%A5%AD) |
| まだ振り分けていない issue | [`要トリアージ`](https://github.com/yusuketakuma/hermes-mcs/issues?q=is%3Aopen+label%3A%E8%A6%81%E3%83%88%E3%83%AA%E3%82%A2%E3%83%BC%E3%82%B8) |

## ラベルの種類

ラベルは4つの軸に分かれます。1つの issue・PR に、軸ごとに必要なものを付けます。

| 軸 | ラベル | 付け方 |
|---|---|---|
| **版**（どの版で出すか） | `即時適用`・`v1.1.3`・`v1.1.4`・`版未定` | 人が1つだけ付ける。版が決まったら付け替える |
| **対応**（誰が何をするか） | `要オーナー対応`・`要実機作業`・`要再起動`・`要トリアージ`・`安全ゲート` | 人が必要なだけ付ける。`安全ゲート` は `ci/gates.py` などの変更で自動でも付く。自動では外さず、確認後に人が解除する |
| **領域**（どこを変えるか） | `領域: 収集`・`領域: 抽出`・`領域: 患者コンテキスト`・`領域: 意味解析`・`領域: LLM`・`領域: 通知`・`領域: スタンプ`・`領域: 保存基盤`・`領域: 運用・導入`・`領域: 文書`・`領域: CI・開発` | PR は変更したファイルから自動（[`.github/labeler.yml`](../../.github/labeler.yml)）。issue は人が付ける |
| **大きさ** | `size: XS`〜`size: XL` | PR の変更行数から自動（50・200・500・1000行で区切る） |

種別は既存の `bug`・`enhancement`・`documentation`・`dependencies` に、`refactor`（挙動を変えない整理）と `roadmap`（計画のまとめ）を足して使います。

### 版のラベルの意味

- `即時適用`: 期限切れや停止を避けるため、通常の版を待たずに出すもの。例: クラウド料金表の再確認。
- `v1.1.3`・`v1.1.4`: その版に入れると決めたもの。版のラベルと同名のマイルストーンもあり、完了率を見られます。ただしラベルとマイルストーンの関連付けは自動ではありません。版が決まった issue・PR には両方を個別に設定し、版の変更時も両方を見直します。
- `版未定`: 計画には入っているが、版はまだ決めていないもの。

ラベルを付けても merge・リリース・配備の承認にはなりません。それらは今までどおり Yusukeさんが明示した範囲だけ行います。

## 作業の流れ

1. **issue を作る**: テンプレート（不具合・機能/改善）を選ぶと `要トリアージ` が付きます。患者名・投稿本文・秘密値は書きません。
2. **振り分ける**: 版・領域・対応のラベルを付け、`要トリアージ` を外します。大きな計画は `roadmap` の issue の子 issue（sub-issue）にします。
3. **PR を出す**: テンプレートの「概要・利用者への影響・更新時の操作・検証」を埋め、本文に `Closes #番号` を書きます。版のラベルを1つ付けます。領域と size は自動で付きます。
4. **merge・リリース**: merge で issue が自動で閉じます。リリースは `release-mcs` skill と [RELEASE_NOTES.md](RELEASE_NOTES.md) の手順に従います。
5. **配備**: `要実機作業`・`要再起動` の付いた変更は、配備のあとに gateway・LINE WORKS アダプターの再起動まで済んだか確認します。

## ラベルの追加・変更

`.github/labels.json` を編集して main に入れると、[`labels-sync`](../../.github/workflows/labels-sync.yml) が GitHub のラベルの色・説明を揃え、無いマイルストーンを作ります。
定義から外したラベルとマイルストーンは自動では消しません（付いている issue を失わないため）。不要になったら GitHub の画面で消します。
次の版（例: v1.1.5）を足すときは、`labels` と `milestones` の両方に1行ずつ足します。

自動付与のワークフロー（[`labeler`](../../.github/workflows/labeler.yml)）は `pull_request_target` で動き、base 側の設定だけを読みます。PR のコードは checkout も実行もしません。

### 自動付与の境界と検証

- 領域ラベルは現在の変更ファイルと同期し、base ブランチ変更時も再評価します。
- 導入前から開いている PR を一括で分類し直す処理はありません。導入後の作成・新しい commit・再オープン・レビュー準備完了・base 変更イベントで評価します。ラベル同期 workflow を実行しても、既存 PR のラベル付与やマイルストーンへの関連付けは行いません。既存 PR は必要なときに個別確認し、分類のためだけに閉じ直したり空 commit を作ったりしません。
- [`labeler-safety.yml`](../../.github/labeler-safety.yml) の `安全ゲート` は追加のみです。手動付与したものも、対象ファイルを差分から取り除いた場合も自動削除しません。リスクを確認してから人が解除します。全ての安全上の変更をファイル名だけで判定できるわけではないため、他のファイルでも必要なら手動で付けます。
- 回帰テストは `scripts/run_tests.sh tests/meta/test_github_labels.py`。合成データとスタブだけを使い、実際のラベル・issue・PR は変更しません。
- main に入る前のローカル検証は GitHub 上の実動確認とは別です。merge 後の次の PR イベントで領域・size の付与と安全ゲートの保持を確認します。

参考（2026-11-16 確認）: [OpenClaw の labeler workflow](https://github.com/openclaw/openclaw/blob/727ef18dc214127ed3cf8d8cd06b57d5e38f9e84/.github/workflows/labeler.yml) の領域同期・base 変更時の再評価を参考にしました。組織用 GitHub App・秘密鍵・追加権限は導入しません。[actions/labeler 固定版](https://github.com/actions/labeler/blob/bf12e9b00b37c5c0ca2b87b79b2daf7891dbda13/README.md) の `sync-labels` は不一致の設定対象ラベルを削除するため、手動判断も含む安全ゲートは別設定で保持します。
