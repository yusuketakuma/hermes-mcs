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

### 初回更新で追加した防御と確認範囲

原本の更新日と、各行が候補になる日付は一致するとは限らない。変換は従来どおり、指定した
`as_of`における候補を作る。作成済み辞書の内容が日付だけで自動更新されることはない。
新しい辞書はSHAで保護される`source`に`as_of`と`next_boundary_on`を記録する。
後者は候補集合が次に変わり得る日で、今後の境界がなければ`null`。
変換reportの`future_starts`と`future_ends`は日付別の**原本行数**であり、辞書identity数や処方数ではない。

開始側の2列は遅い方、終了側の2列は早い方を使う。選択外・未知の状態/日付・名称欠落・
終了済み・開始が最終使用日より後の行を見直し日の集計へ混ぜない。終了日はR08rec1の医薬品節に従い当日を含め、翌日から除外する。従来の当日除外を修正し、見直し日も翌日を記録する。その他の候補除外方針を維持し、
薬剤の臨床的な適否・成分・治療上の同等性は推定しない。
同じ原本からの再作成でも、基準日や内容が変われば辞書SHAが変わり、既存の世代切替で照合し直す。

| 想定した問題 | 防御・確認 | 状態・限界 |
|---|---|---|
| 取得元の変更、redirect/proxy、途中切断、容量超過 | 固定公式URL・期限・32MiB上限で取得し、取得完了後のSHAを照合 | 今回の公開原本は独立取得で一致。将来の配布元の真正性を永続保証しない |
| 版・manifest・member・CSV件数の不一致 | 固定版と厳密manifest、ZIP/CSVのSHA・bytes・行数を照合 | 不一致は辞書を作らず拒否 |
| 破損圧縮、CRC異常、複数member、zip bomb、symlink | 1 member・方式/展開容量を制限し、展開エラーを固定理由へ正規化 | 破損DEFLATEでもtracebackを出さず拒否する回帰を追加 |
| 文字コード、列数、長さ、型、重複code、未知の値 | cp932 strict・42列・幅/型・identity検査、未知の状態/日付は除外/保留 | 黙った切詰めや未知の状態の自動採用なし |
| codeは同じで表示が矛盾、一般名なし、似た名称の誤統合 | identityの表示矛盾は拒否、一般名なしは製品identity、衝突は曖昧のまま | 成分や同等性を推測しない既存回帰を維持 |
| 部分選択の漏れ、上限超過 | 指定codeの存在を全件確認、entry/alias/bytes上限で保留 | 欠落codeや超過を黙って省かない |
| 更新当日の辞書に未来の開始行が入らない | 作成基準日・次の見直し日・開始/終了予定の行数を記録 | 自動採用せず、その日以降の再作成判断に使う |
| 最終日を1日早く除外する・終了後も旧辞書を使い続ける | 最終日を含む合成回帰を追加。`mcs drug status`で同梱原本との違いと見直し日を確認 | 自動で辞書を承認・切替・失効させない |
| 基準日を持たない旧辞書、時計の巻戻り、未来基準日 | 旧辞書の読取り互換を維持し、状態表示で基準日未確認/未来を区別 | OS時計そのものの正しさはこの処理では保証しない |
| 誤ったpath/SHAで設定を切替 | 明示`init --set drug_map=...`時だけ、書込み前に辞書を検証 | 未指定のinitに辞書読取りを追加しない。検証後のファイル障害はruntimeが再検証 |
| 読取り途中のI/O障害で旧候補が残る | 取得済みFDのwrap/stat/read失敗を既存の無効辞書経路へ渡す | 合成障害で旧候補抑制・原薬剤名保持・次回復旧を確認 |
| 変更時刻と容量が同じファイルへの差替え | 辞書をSHAで確認し、世代はdict_idとSHAの組で照合 | mtime/sizeだけを信頼しない |
| 部分導出・期限中断・再起動、A→B→Aの戻し | bounded導出と世代進捗で再開し、未処理の旧世代候補を隠す | 合成の部分切替と旧辞書への復帰で確認 |
| 古いcache/rollup/snapshotからの旧候補表示 | 現行世代・原文世代の照合と再構築を確認 | 配布済みsnapshotは生成時点の写し。更新までの遅れを世代・時刻で区別 |
| ディスク満杯、権限拒否、保存途中の通常例外 | private staging→fsync→検証→新規link。旧辞書を上書きしない | fsync/link失敗と再試行を合成検証。失敗時のstagingを回収 |
| 同名出力、再実行、切替直前の中断 | 既存ファイルは再検証し、同じ内容なら再利用。設定は別操作 | 新規linkは上書きしない。既存辞書・設定を保持 |
| 強制終了・停電で残るstagingや名前の未永続化 | 旧辞書を保持し、再起動後にstatusとファイル検証から再開 | 実機停電は未検証。残骸の扱いは下記 |
| 承認の引継ぎ・外部出力・患者情報混入 | 承認/利用条件は辞書ごとの運用者入力。原本資産と合成fixtureを分離 | 同梱・比較・再作成で承認や本番有効化を自動継承しない |

入力・保存の回帰は[converter](../../tests/ops/test_import_drug_master.py)と
[bundle](../../tests/meta/test_drug_master_bundle.py)、世代・障害復旧は
[loader](../../tests/extract/test_drug_map.py)、[pipeline](../../tests/ingest/test_drug_map_pipeline.py)、
[incremental](../../tests/extract/test_drug_map_incremental.py)に置く。
本表は具体的な失敗経路を追った有限の確認であり、実機ディスク性能・停電・臨床精度や全環境の無障害を保証しない。

### 更新・戻し・中断後の操作

1. `mcs drug status`で同梱版と現在の辞書を確認し、現在の私有設定のpath/SHAと辞書ファイルを保持する。
2. 必要な利用条件・状態定義を確認して`install-bundled`で新規作成する。表示された見直し日以降は再作成を検討する。
3. `compare`と`impact`で新旧の差分・影響を確認する。比較結果だけで処方や成分を確定しない。
4. 使う辞書を明示設定し、定期処理で新しい世代への照合が進むことを確認する。旧辞書は付随作業で削除しない。
5. 戻す場合は、保持した旧path/SHAの組を再設定する。旧20260930用pinを新版converterへ渡す必要はない。
   既に作成済みの旧私有辞書をそのまま読み込める。稼働中処理・snapshotには反映までの時間差がある。

通常の保存失敗では一時ファイルを回収するが、SIGKILL・停電では`data/drug-map/.drug-master-*`が残り得る。
再起動後に作成処理が動いていないこと、所有者・通常ファイル・一時名であることを確認してから、
不要な一時ファイルだけを運用者が除去する。設定中の辞書・旧辞書や不明なファイルをまとめて削除しない。
新しいファイルが失われた場合は既存の設定を確かめ、同じ検証付き作成を再実行する。
既に送信した通知や出力済みコピーの遡及撤回は行わない。
