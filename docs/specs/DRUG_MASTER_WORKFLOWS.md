# 薬剤マスターの照会・変更経緯・薬歴照合

薬剤名の照会、規格表記の差、公式マスターの版差分、投稿の変更経緯、構造化薬歴との照合を、ローカルの確認候補として扱う。処方の正本、臨床的同等性、現在服用中の確定には使わない。既定の収集・通知設定は変更しない。

## データと実行境界

- 公式マスターは公開資産。私有辞書への変換・利用条件確認・人承認・有効化は別操作で、照会や版比較から自動適用しない。
- 患者別の操作は公開済みのローカルsnapshotを読み取る。「公開」はローカルreader向けの発行を意味し、外部公開ではない。稼働中の原本DB、MCS API、LLMには接続しない。
- timeline/reconcileの結果は患者別の情報を含み得る私有出力。既存の外部exportへ追加せず、リポジトリやCIへ実データを保存しない。今回のテストはすべて合成データ。
- 辞書のSHA256とresolver版を照合する。投稿の抽出は現行世代・モデル・contextの整合が必要で、古い抽出やルールだけの結果へ自動的に戻して欠測を隠さない。

実装入口は [mcs_drug.py](../../mcs/ops/mcs_drug.py)。以下の名称・project ID・パスは説明用の架空例であり、入力済みデータの同梱を意味しない。

## 1. 同じ公式コードに属する名称を調べる

```sh
python3 mcs/ops/mcs_drug.py related '架空製品錠5mg' --json
```

省略時は設定済みの私有辞書を使用する。別の辞書は `--dictionary /private/path/dictionary.json --sha256 HASH` で明示する。`--limit` は表示件数上限。`DrugMap.related_names(name, limit=200)` はコード候補、別名、商品別の名前・単位・剤形区分、総件数、切詰めの有無を返す。

一般名処方コードが同じ商品の関連を扱い、成分名が似ているという理由で別コードを統合しない。`official_identity` は候補が公式の一般名/product identityであることを示す。旧辞書のingredient/classにも参考別名は返るが、そのフラグはfalse。複数候補は曖昧なまま保持する。商品の規格・剤形は名前ごとに保持し、代替調剤可能という判定へ変換しない。

`products[].form_code` は公式マスターの内用・注射・外用などの区分であり、錠・散・液などの製剤表記ではない。一般名処方コードや商品metadataがない旧辞書は、その情報がないまま読み込める。

## 2. 薬名に書かれた規格・剤形・単位を比較する

```sh
python3 mcs/ops/mcs_drug.py check-surface '架空製品錠5mg' \
  --reference '架空製品錠10mg' --unit 錠 --reference-unit 錠 --json
```

[drug_map.compare_surfaces](../../mcs/extract/drug_map.py) は両側の薬名から読める規格・剤形と、明示された単位だけを比較する。`strength_difference/form_difference/unit_difference` を確認理由とし、未記載は `missing` に残す。差があれば `review`、必要な項目がなければ `unknown`、確認できた表記に差がなければ `no_difference_observed`。いずれも `equivalence_confirmed=false`。

単位換算や臨床的等価の推定はしない。服用量・一日量の `dose` を製剤規格へ読み替えない。公式マスターの `form_code` と、名称から取り出した錠・液などの `form` を混同しない。

## 3. 公式マスターの版差分と採用辞書への影響

```sh
python3 mcs/ops/mcs_drug.py master-impact \
  --before /private/master/old.zip --before-pin /private/master/old-pin.json \
  --after /private/master/new.zip --after-pin /private/master/new-pin.json --json
```

[drug_master_compare.compare](../../mcs/views/drug_master_compare.py) が、版・仕様・原本SHA256を固定したCSV/ZIPを全薬剤コードで比較する。pinの検証は [import_drug_master.catalog](../../mcs/ops/import_drug_master.py) が行う。既存 `compare` は変換済み私有辞書同士、`impact` はその辞書変更が投稿の候補へ与える影響であり、公式原本を比べる `master-impact` と入力が異なる。

追加、名称変更、廃止確認、経過措置期限、単位・剤形区分・一般名コード変更、新版に見当たらないコードを分ける。新版から消えた行だけで廃止を断定しない。採用辞書に保持された出典コードで影響を照合し、旧辞書に出典metadataがなければ採用影響はunknown/partialとして残す。名称差分を表示しても辞書承認、pin変更、薬剤候補の再生成を自動実行しない。

変換元の公式仕様に対応したフィールド位置・許容値はimporterを正本とする。廃止日・経過措置期限は別フィールドであり、名称変更を期限切れと読み替えない。

## 4. 薬剤の変更経緯

```sh
python3 mcs/ops/mcs_drug.py timeline --project 1 --name '架空製品錠5mg' --limit 50 --json
```

[drug_timeline.timeline](../../mcs/views/drug_timeline.py) は保存済み投稿のページを返す。`--since/--until` は投稿日時のUnix秒、`--cursor` は同じsnapshot・検索条件の次ページ、`--snapshot` は明示するローカルsnapshot。薬剤のイベント日時を投稿日時から補作しない。

各itemに原抽出の `mention`、投稿・artifact参照、`change` を持たせる。

| 欄 | 意味 |
|---|---|
| action | 開始start・中止stop・増量increase・減量decrease・変更changeなど、抽出に保存された動作 |
| subject | 本人patient、家族family、他者other、不明unknown |
| time_scope / event_time | 保存済みの過去・現在・予定の区別とイベント時点。なければunknown/null |
| implementation | planned、performed_report、not_established。polarity=affirmed、workflow_status=performed/done、epistemic=asserted/reportedがすべて揃った場合だけ実施の記載。currentだけでは実施を確定しない |
| polarity / workflow_status / epistemic | 否定・不確実・作業状態を独立して保持 |
| identity_candidates | 現行辞書の薬剤コード候補。本人の処方であるとの承認ではない |

`drug_groups` はページ内の単一公式コード候補ごとにitemのindexを束ねる。複数候補、旧ingredient/class、未照合は別言及として残す。グループ内でも予定・過去・他者を削除せず、単一の「現在の処方」へ集約しない。

`--name` は表記一致に加え、現在のsnapshotと承認済み設定辞書の世代が一致するときだけ、単一の公式一般名/productコード候補で関連名まで検索する。曖昧候補や旧成分名から検索を広げない。辞書・抽出の不一致は照合中・未抽出として残る。

## 5. 構造化薬歴と投稿の照合

```sh
python3 mcs/ops/mcs_drug.py reconcile --project 1 --limit 50 --json
```

[drug_reconciliation.reconcile](../../mcs/views/drug_reconciliation.py) はtimelineと同じページ条件で投稿を読み、[get_project_metadata](../../mcs/views/project_metadata_view.py) が返す取得済み `medication_periods` と照合する。nameフィルタはtimelineだけ。追加取得は行わない。

構造化薬歴は薬名、薬歴内ID、begin_date/end_dateを持つ。薬歴内IDを公式薬剤コードとして扱わない。取得時点 `last_complete_at`、最新試行 `attempted_at`、state/current_known/historicalをそのまま示す。すべての保存済み期間を比較するため、過去の期間の一致を現処方の一致としない。

本人の言及について、表記完全一致・同じ文字上の薬名base・単一公式コード候補で比較先を探し、項目2の規格/剤形比較を再利用する。base一致は規格差を見つけるための候補選定であり、薬剤同一性の証明ではない。独立した規格・単位欄は取得schemaにないため、薬名にない値は不明のまま残す。

- 不一致は `review` と比較対象・理由を返す。薬名が保存薬歴で見当たらなくても `absence_confirmed=false` で、未処方・服薬なしとはしない。
- 家族・他者・主体不明は `not_comparable`。予定・過去・否定や不明の極性はcontextに残し、実施済みの服薬と比較したように見せない。
- 薬歴未取得はunknown、古いcompleteはstale、失敗後の過去データはhistoricalのまま。薬名が欠けた薬歴がある場合、比較先なしを不一致へ断定しない。
- coverageには未取得本文、未抽出、取得失敗、返信取得漏れ、履歴範囲、snapshot世代を保持する。比較は投稿の現在ページに限り、薬歴側にしかない薬を全投稿に存在しないと断定しない。

## 検証と未検証事項

[薬剤経緯テスト](../../tests/views/test_drug_timeline.py) と [薬歴照合テスト](../../tests/views/test_drug_reconciliation.py) は、予定/実施記載・本人/家族・過去/現在、世代差、コード候補検索、旧辞書の境界、規格差、未知の単位、薬歴日時、取得漏れ、ページング、読取り専用、CLIからの合成snapshot照合を検査する。

合成テストは実患者の抽出精度、薬歴取得の網羅性、臨床判断の妥当性を証明しない。今回、実API・原本DB・実LLM・本番辞書の承認や切替・外部exportは実行していない。
