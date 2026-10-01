# 日本語の変更記録

実行時の挙動を変えるPRでは、`changes/<課題番号または短い識別子>.json`を追加する。
PRを使わないエージェント作業でも、変更と同じcommitで記録する。説明は
「何を実装したか」より「利用者にとって何が変わるか」を先に書く。
schema・運用手順・著名リポジトリとの比較は`docs/RELEASE_NOTES.md`を参照。

```json
{
  "category": "fixed",
  "title": "返信が重複して届く問題を修正",
  "summary": "抽出結果が後から届いても、既存の返信投稿を更新するため同じ返信を再投稿しません。",
  "upgrade": "追加操作は不要です。",
  "details": ["配送済みの投稿IDを再利用する。"],
  "refs": ["mcs/notify/"]
}
```

これは形式例であり、実在する変更の記録ではない。実際の変更はコードで検証する。
`category`: `security` / `breaking` / `added` / `changed` / `fixed`。
`upgrade`: 必要操作・既定値・対象・適用条件を明記。「不要」も明記する。
`refs`: PR番号・commit SHA・変更されたソースパスのいずれかを必須とする。
性能向上・精度向上の数値は実測がある場合だけ書く。

`build`は記録をversion別の`archive/`へ移動する。archiveは履歴根拠として保持する。
