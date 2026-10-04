# 医薬品masterの出所と変換境界（1.0.13）

オーナー指定の出所は[診療報酬情報提供サービスのdownload menu](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/)。
旧年度の`R07_y.zip`ではなく、現在適用される
[`downloadMenu/yFile`](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/yFile)
を使う。自作別名表や別ページの薬価品目リストへ自動で置き換えない。

## 2026-10-04の読取り確認

公開ZIPをメモリ上だけで取得・展開し、次を確認した。実DB・設定・辞書ファイルは
更新していない。実masterの行をrepoや合成fixtureへコピーしていない。

| 項目 | 確認値 |
|---|---|
| member / edition | `y_20260930.csv` / `20260930` |
| ZIP bytes | 1,157,440 |
| ZIP SHA-256 | `820f173981b1601e718ef110ffba55fbea1c04d07abb6a4297b4281db0eecccf` |
| CSV bytes / encoding | 6,079,806 / cp932 |
| records / layout | 19,272 / 全行42項目、種別Y |
| 変更区分 | 0が18,250件、3が1,022件 |
| 一般名code・標準記載あり | 各7,737件 |
| 廃止日 | 全行`99999999` |

これは取得時のpin候補で、次回の公開内容に再利用してよい保証ではない。
実全件のconverter実行・照合・私有辞書への出力・有効化は未実施。

## 一次仕様と意味

[R08rec3](https://shinryohoshu.mhlw.go.jp/shinryohoshu/file/spec/R08rec3.pdf)
222–223ページは42項目、CSV、医薬品code9桁・薬価code12桁・一般名code12桁を定義する。
[R08rec1](https://shinryohoshu.mhlw.go.jp/shinryohoshu/file/spec/R08rec1.pdf)
16ページの医薬品節では、変更区分は0=同じ、1=抹消、3=新規、5=変更、9=廃止。
他masterの復活区分2を医薬品へ流用しない。
18ページは廃止品以外の廃止日を`99999999`、経過措置等の期限なしを0と定義する。
19ページの一般名codeと標準記載は一般名処方master由来で、該当なしは省略される。

pinのstatus policyはこの資料に基づき、候補の変更区分0/3/5と日付番兵を
明示する。抹消・廃止や未確認値を有効な処方へ変換しない。
候補identityは明示された一般名処方code/記載、なければ製品codeとし、
成分・YJ・治療上の同等性をcodeの接頭辞から推定しない。
最大長の省略可能なかな、衝突・用量/剤形の不足は不明/曖昧のまま保持する。

## 利用条件と操作

[厚労省の共通利用規約](https://www.mhlw.go.jp/chosakuken/index.html)は、権利表記等の
例外がない場合PDL1.0に準拠し、出典と加工表示を要求する。特別な利用条件や
第三者権利の例外まで無条件に解消したとは扱わない。私有pinには出典・edition・
hash・layout・利用条件と承認/根拠を記録し、承認や人手receiptをツールで偽造しない。

`python3 mcs/ops/import_drug_master.py --source MASTER --pin PIN`はofflineの
report-onlyで、`--output DEST`を指定したときだけ新規0600辞書を作る。
既存出力の上書き・network・実DB更新・設定有効化は行わない。
このconverterの合成91件成功は、実masterの全件変換や臨床精度の証拠ではない。
