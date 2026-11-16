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
| **対応**（誰が何をするか） | `要オーナー対応`・`要実機作業`・`要再起動`・`要トリアージ`・`安全ゲート` | 人が必要なだけ付ける。`安全ゲート` は `ci/gates.py` などの変更で自動でも付く |
| **領域**（どこを変えるか） | `領域: 収集`・`領域: 抽出`・`領域: 患者コンテキスト`・`領域: 意味解析`・`領域: LLM`・`領域: 通知`・`領域: スタンプ`・`領域: 保存基盤`・`領域: 運用・導入`・`領域: 文書`・`領域: CI・開発` | PR は変更したファイルから自動（[`.github/labeler.yml`](../../.github/labeler.yml)）。issue は人が付ける |
| **大きさ** | `size: XS`〜`size: XL` | PR の変更行数から自動（50・200・500・1000行で区切る） |

種別は GitHub 既定の `bug`・`enhancement`・`documentation`・`dependencies` に、`refactor`（挙動を変えない整理）と `roadmap`（計画のまとめ）を足して使います。

### 版のラベルの意味

- `即時適用`: 期限切れや停止を避けるため、通常の版を待たずに出すもの。例: クラウド料金表の再確認。
- `v1.1.3`・`v1.1.4`: その版に入れると決めたもの。版のラベルと同名のマイルストーンもあり、完了率を見られます。
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
