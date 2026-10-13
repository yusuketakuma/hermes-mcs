# 医薬品masterの出所と変換境界（1.0.13）

本書の旧版割当・調査時点の「未実装」「未確認」は当時の記録です。末尾の2026-10-06追記に、
1.0.16開発候補の実装と残る受入条件を区別して記録します。開発実装は公開・配備・有効化の完了ではありません。

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

## 2026-10-06追記: 1.0.16開発候補の全件容量対応

上記の2026-10-04調査・1.0.13の合成91件という歴史記録は保持する。
同じ20260930版のZIP SHAは変更せず、RAM上だけの形状/容量再確認では
19,272行・全42項目からの候補identity上限集計12,792件、出典情報を除く
compact JSON見積5,430,199 bytes、最大42別名/identityを確認した。
実master行・実名別名をrepoやfixtureへ保存していない。これは実辞書の全件変換・
私有出力・採用や臨床精度測定の完了ではなく、容量境界の確認。

開発候補の[drug_map](../../mcs/extract/drug_map.py)は20,000 entries・8MiB、
100 aliases/entryを上限にする。[converter](../../mcs/ops/import_drug_master.py)は
全件/明示部分集合、候補件数・payload bytes・上限をreport-onlyで示し、
上限超過は拒否/保留して黙って切り詰めない。知らないedition・layout・変更区分/日付、
明示codeの欠落・identityと表示の衝突は既存の拒否/除外/保留条件を維持する。

lookup/search/辞書比較の読取りAPIと共通CLIは開発実装・対象合成51件成功。
一般名処方のidentityは明示された一般名code/標準記載、製品は医薬品codeを保持する。
これらはingredient候補と区別し、YJやcode接頭辞から成分・治療上の同等性を推定しない。
別名一致があっても処方を確定せず、相互作用・禁忌チェックは行わない。
型別統計と未照合/複数候補のローカル確認一覧、サマリー等の世代連動は開発実装・
対象合成216件成功。
未照合を別名不足と決め付けず、辞書未設定・無効・確認不能を区別する。

**オーナーによる利用条件確認・status policyの採用判断・承認記録とconfig有効化は未完了。**
読取り照合、RAM集計や合成テストから承認者・利用条件確認日・receiptを作成しない。
`approved_by`等をツールが埋めて有効化条件を満たすことはない。
`--output`は従来どおり明示指定の新規0600私有辞書だけで、既存出力の上書き・
network取得・実DBの更新・設定有効化を自動で行わない。

合成回帰資産は[converter](../../tests/ops/test_import_drug_master.py)、
[大きな辞書・進捗/世代](../../tests/extract/test_drug_map_incremental.py)、
[辞書差分](../../tests/extract/test_drug_map_diff.py)、
[型別候補統計](../../tests/views/test_drug_candidates.py)。実masterの匿名化行もfixtureへ転載しない。
実装済みのCLIは `mcs drug lookup NAME [--dictionary PATH --sha256 HASH] [--limit N] [--json]`、
`mcs drug search NAME [--dictionary PATH --sha256 HASH] [--limit N] [--json]` と、
両辞書のpath/SHA対を必須にするcompareとimpact（公開snapshotの薬剤言及を新旧辞書で再照合した
読取り専用の影響件数）。limitは全て0〜200。カードの「薬剤を確認」「薬剤を検索」は、config固定かつ
DBの現行導出世代と一致する承認済み辞書だけを使う。
searchは名称・別名・識別子の正規化部分一致で参照候補を探すだけで、薬剤の照合済み候補を付与しない。
公開・配備・config有効化と実データ評価はこの開発追記の完了範囲外。

成功件数は親の同タスクの隔離結果。実master行を使ったテストや承認の代わりではない。
導出処理が辞書切替・無効化をDBへ記録した後、または進捗破損時は、サマリーやcached/snapshot参照からも旧候補注釈を落とし、
原薬剤名・言及は保持する。実masterの利用条件確認・承認・有効化の保留条件は変更しない。

## 2026-10-06追記: リリース同梱方針

オーナーの明示指示により、今後は毎回のリリースで公式サイトを確認し、新版があれば同じアップデートへ組み込む。
20260930版の公開原本ZIPと出典・版・SHA・利用条件参照を
[`resources/drug-master/`](../../resources/drug-master/README.md)へ同梱した。
上記の「repoへ保存していない」はRAM調査当時の記録であり、今回の原本配布を否定するものではない。
配布資産の原本をfixture・few-shotへ転載せず、患者データも扱わない。
`python3 scripts/development/check_drug_master_bundle.py`は原本hash・member・全行の既知形式を無通信で検証する。
承認のない変換は`terms_unconfirmed`として保留し、私有辞書を作成・有効化しない。
このrepoの専用`release-mcs`（リリースMCS）skillは、毎回の新しい公式更新照合を必須とする。

## 2026-10-11追記: 同梱原本からの辞書作成（1.0.18）

オーナーの明示指示「マスターとしてシステムで活用できるように」により、同梱原本から私有辞書へ到達する
経路を[`mcs drug install-bundled`](../../mcs/ops/mcs_drug.py)として追加した。上記の境界は変更しない。

- 版・layout・ZIP SHA・memberは同梱manifestから、`as_of`は実行日から機械的に埋める。
  `terms_checked_on`・`status_policy.confirmed_by`は運用者の必須入力、`approved_by`は任意入力で、
  manifestの`reference_checked_on`や同梱物から作らない。`terms_record`は省略時に利用条件URLを記録する。
- status policyはR08rec1の医薬品節に基づく変更区分0/3/5と日付番兵0/99999999を、
  `--confirmed-by`の確認の下でだけ適用する。抹消1・廃止9、収載日・変更日が実行日より後の行は候補にしない。
- 変換は既存の`convert()`で全行を検証し、結果のCSV SHAと行数がmanifestと一致しない場合は作成しない。
  出力は既存の`write_private()`で`data/drug-map/`へ新規0600作成し、同じ内容が既にあれば上書きせずそれを示す。
- 設定（`drug_map`）・DB・通信・稼働サービスは変更しない。有効化は運用者が既存の
  `mcs_setup.py init --set 'drug_map=...'`で行い、その後の定期処理で既存の導出が新しい辞書世代へ照合し直す。
- `mcs drug status`は同梱版と設定中の辞書（dict_idに含まれる原本SHA）を比べ、未設定・不一致・
  未承認（参考のみ）・承認済みを読取り専用で示す。更新で新しい公式版が同梱された場合の再作成判断に使う。

合成回帰は[同梱経路](../../tests/ops/test_mcs_drug_bundled.py)。同梱された公開原本そのものも
実行時converterで全行変換できることを一時HOMEで確認し、行やnameをテストへ転記しない。

## 2026-10-13追記: 20261013版への更新（1.0.21）

1.0.21準備の公式照合で、医薬品マスターが20261013版（19,286件）に更新されていた。
固定URLの`yFile`をno-proxy/no-redirect・32MiB上限で取得し、root・Claudeの2回の独立取得でバイト列が一致した。

| 項目 | 確認値 |
|---|---|
| member / edition | `y_20261013.csv` / `20261013` |
| ZIP bytes / SHA-256 | 1,158,682 / `c3efc8aa772b9bf54cfa992e8df3bc82480f86f35bbeb52522d0997144140fb5` |
| CSV bytes / SHA-256 | 6,083,884 / `6fa8f08ca4341ac530e5f4081d3fd3bbebbfcdd278c0d0ec0f172a0c1af018d1` |
| records / layout | 19,286 / 全行42項目、種別Y（既存の`R08rec3-medicine-42`検査を全行通過） |
| 変更区分 | 0が19,257件、3が14件、5が15件 |

- R08rec3は前回確認とSHAが一致した。R08rec1は改版（表紙 令和8年10月13日）されたが、医薬品節の変更区分
  （0=同じ・1=抹消・3=新規・5=変更・9=廃止）、廃止日の`99999999`、経過措置期限なしの0、一般名code・標準記載の省略規定は
  変わっていない。利用条件ページは公共データ利用規約（第1.0版）準拠・出典・加工表示の記載を保っている。
- converterの`EDITION`を20261013へ更新した。layout・変更区分・日付番兵の検査は変えていない。pinの`as_of`は版の日付以降、
  memberは`y_20261013.csv`だけを受け付ける。旧版のpinはedition不一致（`pin_schema_layout_or_edition`）で拒否される。
- 既存status policy（変更区分0/3/5）で全行をRAM内で変換すると、12,195 entries・compact JSON 5,140,954 bytesで、
  20,000 entries・8MiBの上限内だった（`as_of` 2026-10-13時点）。
- 旧20260930版の原本・manifestは`resources/drug-master/20260930/`に残し、削除しない。
- 私有辞書の作成・承認・config有効化は、引き続き運用者の操作で、本更新の範囲外。
