# 1.0.13 NASバックアップ方針案

2026-10-04。ユーザーの「NAS案を作る」に基づくレビュー用の案。
**実行用policyではなく、実データへの書込み・鍵作成・mount・サービス適用は未承認／未実施。**
実装・コマンドの正本は[バックアップ手順](../guides/BACKUP.md)。

同日、ユーザーは最大30bundle・手動削除・週1回verify・90日ごとの回復鍵訓練を
運用方針として採用し、続いてRPOは24時間を選択した。実行時刻・保存先・
容量・鍵/receiptの保管担当は未確定。実行用policyの作成・実データ操作は別工程。
回復鍵の媒体は、NAS/端末とは別の封印した紙を採用した。実保管場所・担当と
receipt保管は未指定で、鍵生成・表示・転記・保管確認はまだ実施していない。

## 対象と配置

Mac miniのMCS原本DBを、検証済みの日次snapshotから暗号化してNASへ保存する。
NASへ置くのは暗号化bundleで、平文DB・鍵・認証情報を置かない。
添付ファイル本体、Keychain、config、配送journal、サービス設定は別途回復対象となる。

| 保存物 | 配置案 | 確定条件 |
|---|---|---|
| 暗号化bundle | Mac miniにmountしたNASの専用ディレクトリ | 実mountパス、保存許可、容量、0700・同一UID、device/inodeを確認 |
| 運用鍵 | Mac miniの専用`mcs-backup` Keychain service | 作成・保管責任と端末外の回復経路を確定 |
| 回復鍵 | NASともMac miniとも別の保管場所 | 保管担当、取出し手順、鍵を使った復元訓練を確認 |
| 信頼するSHA receipt | bundleと別の保管場所 | NASごとの置換・巻戻しを判別でき、端末喪失後にも取得可能 |
| 平文scratch | Mac miniの私有ディレクトリ | 端末の物理暗号化、必要容量、清掃責任を確認 |

NASのRAIDやsnapshotは補助であり、鍵回復・独立receipt・復元訓練の代わりにはしない。
NASの管理snapshotや複製に古い暗号文が残る条件も保持・削除方針へ含める。

## 数値のレビュー案

保持・verify・訓練と24時間RPOは採用済み。それ以外は未採用の提案であり、
実測や配備済み設定ではない。実行用policyへ自動反映しない。

| 項目 | 提案 | 採用前の確認 |
|---|---|---|
| offsite | 日次snapshot完成後に毎日1回 | 実行時刻・timezone・NAS接続時間を指定 |
| RPO | 採用済み: 24時間以内 | 新しい静的snapshotの用意と転送時刻を組み合わせ、保存済み復旧点の最大年齢を実測して受入 |
| 保持 | 採用済み: 最大30bundle、削除は手動 | 30日保証ではない。追加の手動実行も1bundleとして容量を消費 |
| verify | 採用済み: offsite時の検証に加え、独立確認を週1回 | 検証担当と鍵・receiptの取得経路は未指定 |
| escrow-key drill | 採用済み: 初回、その後90日以内ごと | 端末外の鍵を手入力し、新規私有先で復元。実機同意は別途必要 |
| snapshot容量上限 | 実DBサイズの確認後に設定 | `max_snapshot_bytes`を推測で決めず、保存可能数とscratch容量を照合 |

`verify_interval_s`は7日、`drill_interval_s`は90日を採用。
これらはhealthの期限であり、別のverify／drill jobを自動登録する設定ではない。
日次snapshot作成とoffsiteの時刻差だけでは、復旧点の最大年齢は24時間を超えうる。
採用済みの24時間RPO向けに、実装は明示した静的`backup.snapshot`と
単一minute・複数hourの`backup.schedule`を扱える。実パス・snapshot作成先行・
実行周期は未確定で、旧日次設定を自動切替せず、達成済みとは扱わない。
復旧所要時間は未実測で、RTOの達成を約束しない。

## 実行用policyに必要な未確定値

紙は既存`keygen`の明示human escrowで表示する32byte鍵の64文字hexを、
本人がローカルで転記・封印する媒体とする。代理人ツール・チャット・repo・
NASへ鍵値を送らない。保管後のcustody確認と、紙からの90日訓練は別のreceiptで
記録し、媒体方針の採用だけで`custody_confirmed`やdrill成功へ変えない。

| 値 | 現在の状態 |
|---|---|
| `destination` | NASの専用ディレクトリの絶対パス未指定 |
| `destination_device` / `destination_inode` | mount後に確認。架空の値や既定値は使わない |
| `scratch_dir` | ローカル私有先未指定。source・NAS先と分離 |
| `policy_id` | 非秘密・非患者識別子の管理名未指定 |
| `key_custody_confirmed` | 端末外の回復鍵保管を確認するまでtrueにしない |
| `allow_os_openssl` | 固定OS OpenSSLの利用方針未確定 |
| `max_snapshots` / `deletion` | 30bundle／manualを採用済み。実容量と削除時の個別確認は別途必要 |
| `max_rpo_seconds` | 86400秒を採用済み。snapshot/転送周期による達成は未検証 |
| `max_snapshot_bytes` | 実サイズ・容量未確認 |
| `scheduled` | 実機確認と方針採用まではfalse |

アプリ側の`backup.enabled`も採用・実機確認まではfalseとする。
`policy`・`snapshot_dir`・毎日の`schedule`・verify/drill間隔は、採用後に明示する。
この文書を`--policy`へ渡したり、repoに実用の鍵・秘密設定を保存したりしない。

## 接続切れと復元時の扱い

- NAS未mount、identityの変化、私有権限や容量の不足は止める。
  mount先が消えた際にローカルの同名ディレクトリへ代替保存しない。
- 再mountでdevice/inodeが変わった場合はオーナーが媒体を確認してpolicyを更新する。
  自動でpinを付け替えない。
- 保持上限に達したら新規保存を止め、失敗・RPO超過をhealthで示す。
  既存bundleの自動削除で成功に見せない。
- 復元は新規私有先だけに行う。DB・bundle・配置先を束縛した人のreason/receiptで
  収集再開を判断し、配送の保留は維持する。既存の更新/rollback承認を流用しない。

## 採用前に揃える証拠

1. mountパス・私有権限・媒体identity・容量と保存許可。
2. 鍵とreceiptの別保管担当・取得手順。鍵値はチャットやrepoへ送らない。
3. 採用済みの保持・verify/訓練間隔・24時間RPOを反映し、fresh snapshotと転送の周期・実行時刻、平文scratchの管理方針を確定。
4. 鍵・書込みなしの`plan`による整合確認。unknownや処理上限到達は成功にしない。
5. 別途明示された範囲での実バックアップ・検証・escrow-key drillと所要時間の記録。

この案の作成は1〜5の実施や承認を意味しない。
