# バックアップ・端末喪失・鍵回復の運用手順

この手順の対象は、[mcs_backup.py](../../mcs/ops/mcs_backup.py) の
`keygen` / `offsite` / `verify` / `drill` / `restore` / `status`。
ローカル CLI と合成検証の実装を説明するもので、実機の復旧保証、
外部保存の許可、定期登録、配備済み状態を意味しない。
設定・health・Hermes cron／独立hostの接続は合成検証済み。
実機のサービス設定・登録は未適用であり、設定ファイルの編集だけで定期実行が始まるとは扱わない。
予定時刻・猶予・保持数はオーナーが決め、既定値を補わない。

## 1. 保存物と保証範囲

| 保存物 | 内容と用途 | 限界 |
|---|---|---|
| ローカル日次 backup | [daily_backup](../../mcs/core/maintenance.py) が静的な `ledger-YYYYMMDD.db` を検証して保存。現行 `BACKUP_KEEP=7` | 同じ端末の平文 DB。端末喪失の対策ではない |
| 暗号化 offsite | 静的 DB を gzip、固定 `/usr/bin/openssl`、encrypt-then-MAC で `.mcsb` へ。公開前の復号検証と公開後の SHA 照合を実施 | 明示したディレクトリへ書くのみ。mount・同期先への転送完了・媒体の存続は別確認 |
| SHA receipt | `bundle`、全ファイル `sha256`、時刻、`policy_id`、最後の正常 run 時刻 | 暗号化媒体と独立に保管しなければ、媒体ごとの置換や古い正当な世代への差替えを検出できない |
| 鍵 escrow | `keygen` が人の明示操作で表示する 64 桁の小文字 hex（32 バイト） | Keychain は端末と共に失われる。外部保管者・回復手段が必要 |
| ローカル運用記録 | `backup_state.json`、`drills/*.json`、`key_escrow.json` | 鍵値は保存しない。このディレクトリだけに receipt を置くと、端末喪失時に receipt も失う |

暗号化するのは DB の内容。bundle の認証対象ヘッダーにはサイズ・hash・
件数・時刻等が含まれる。鍵保持者は正当な MAC を作れるため、MAC だけで
作成者の独立した本人確認はできない。端末・OS・ローカルアカウントの侵害、
未収集データ、失われた鍵・receipt・媒体は、この機能では回復できない。

DB の添付行と保存済み hash は復元対象だが、添付のファイル本体は含まれない
（`attachment_payloads_included:false`）。認証情報、`config.json`、`.env`、
Keychain、ブラウザ profile、配送 journal、サービス設定も bundle の収集対象外。
それぞれ別の回復手段を決める。暗号化媒体・鍵・独立 SHA receipt の
保管先、アクセス権、回復責任は三者それぞれについて確認する。

暗号化 backup の成功は、[DB 整合監査](../../mcs/core/ledger_audit.py)、
取得完全性や臨床判断の正しさの証明ではない。「記録が見つからない」を
「対応がなかった」と解釈しない。[安全境界](../../SECURITY.md)も参照。

<a id="policy"></a>
## 2. オーナーが先に決めるもの

外部保存先と保存の許可、鍵の保管者と取出し手順、SHA receipt の独立した
信頼経路、復旧点と停止許容時間、保持・削除、平文 scratch の保管と清掃、
端末・媒体の物理暗号化、OS OpenSSL の利用を承認してから実データに使う。
暗号化したデータの外部保存も外部保存の承認が必要。

CLI の `--policy` はアプリの `config.json` とは別の、明示した JSON ファイル。
`BackupPolicy.validate()` が要求する全項目は次のとおり。
下表は入力契約であり、値を代わりに決めるテンプレートではない。

| 必須フィールド | 型・受入条件 | 決める内容 |
|---|---|---|
| `destination` | 絶対パスの文字列 | 承認済み暗号化媒体の既存ディレクトリ |
| `destination_device` | 整数。現在の `stat().st_dev` と一致 | 正しい mount を人が確認して固定 |
| `destination_inode` | 整数。現在の `stat().st_ino` と一致 | ディレクトリ identity を固定 |
| `scratch_dir` | 絶対パスの文字列 | 承認済みローカル平文作業ディレクトリ |
| `policy_id` | `[a-zA-Z0-9_-]{1,64}` | 患者情報を含まない承認方針の識別子 |
| `key_custody_confirmed` | JSON の `true` | 端末外の鍵保管と回復責任を確認済みであること |
| `allow_os_openssl` | JSON の `true` | 固定 OS OpenSSL の利用を承認済みであること |
| `max_snapshots` | 1 以上の整数 | 保存数の容量上限。自動削除する保持数ではない |
| `deletion` | `"manual"` | 削除は人が範囲を承認して実施 |
| `max_rpo_seconds` | 1 以上の整数 | bundle 作成時刻と最後の正常 run 時刻の鮮度上限 |
| `max_snapshot_bytes` | 1 以上の整数 | 元 DB と復号 DB に許可する最大サイズ |

共通の `load_policy(path)` は `(BackupPolicy, scheduled_bool)` を返す。
追加の `scheduled` は boolean に限定し、省略は `false`、定期実行の許可は
JSON の `true` だけ。文字列・整数・`null` などは拒否する。`offsite --scheduled` でも
不正値は無効扱いの正常終了（`status:disabled`）にせず、`backup_policy_required` で
非ゼロ終了する。省略または `false` だけが `status:disabled` の終了 0。これは `BackupPolicy` の
必須フィールドではなく、定期登録を作る操作でもない。他の未知フィールドは受理しない。

`destination` と `scratch_dir` は既存、所有者が実行ユーザー、mode が
正確に `0700`、symlink を経由しない canonical な絶対パスであること。
両者は同じ場所・親子関係にしない。policy ファイルは所有者本人の通常ファイル、
`0600`、64 KiB 以下で、親ディレクトリも私有 `0700` が必要。
自動で mount・ディレクトリ作成・既存の権限変更は行わない。

`--state-dir` も明示した既存の私有 `0700` ディレクトリで、暗号化媒体と
同一・親子関係にしない。記録は `0600` で原子的に保存される。
別 `policy_id` の既存 `backup_state.json` を使い回すと拒否する。
読取り専用の `plan` / `preflight` は、通常の `data` 記録先配下にある
`data/snapshots/ledger-snapshot.db` を検査できる。元DBと記録を同じ
ディレクトリへ置く配置、媒体・scratchとの重複は拒否する。
媒体の identity が変わった場合も拒否するため、エラー回避だけを目的に
pin を書き換えず、対象の確認と方針の再承認を行う。

### アプリ側の接続設定（合成検証済み・実機適用前）

提供済みの `config.json.backup` は以下のキーだけを持つ object。
省略時は無効、明示する場合は boolean の `enabled` が必要。
`enabled:true` の場合、残りのフィールドも明示する（`snapshot` と `snapshot_dir` はちょうど一方）。

| キー | 提供済みの入力契約 |
|---|---|
| `enabled` | boolean。アプリ接続の opt-in。`false` はその接続を無効化する |
| `policy` | 明示した絶対パス。上記の私有 JSON policy を指す |
| `snapshot` | 明示した絶対パス。固定の静的 DB ファイル。`snapshot_dir` と排他 |
| `snapshot_dir` | 明示した絶対パス。私有の静的日次 backup ディレクトリ。`snapshot` と排他 |
| `schedule` | オーナー指定の毎日の cron 式 `M H1,H2,... * * *`。minute（M）は0〜59 の1つ、hour は0〜23 を1〜24個、重複なしのカンマ区切り（例 `15 3 * * *`、`15 3,15 * * *`） |
| `verify_interval_s` | 明示した正の整数。最終 verify 記録に許す経過秒数 |
| `drill_interval_s` | 明示した正の整数。最終 drill 記録に許す経過秒数 |

`enabled:true` では `snapshot` と `snapshot_dir` のちょうど一方を指定する
（両方・どちらも無しは拒否）。

設定検証・`_service_subs` の `BACKUP_ENABLED` / `BACKUP_POLICY` /
`BACKUP_SNAPSHOT_DIR` 供給と、health の `load_policy` + `status` 読取りが
実装されている。health は proof 不在、失敗した試行、RPO 超過、
verify / drill 記録の期限超過を正常扱いにせず、理由を分ける。
鍵・receipt・私有パスは health へ転記しない。

health は `run_check.HOME/data` の記録、描画 wrapper は `DATA` の記録を使う。
手動 CLI で別の `--state-dir` を選んでも、health の読取り先が自動で変わるわけではない。
`last_drill_at` や health の正常判定だけでは、外部 escrow 鍵を人が取り出した
証明にはならない。実施方法も非機密の証拠として確認する。
間隔の既定値、grace、登録済み job の存在はこの設定契約から推定しない。
`verify_interval_s` / `drill_interval_s` は証拠の鮮度条件であり、
それだけで別の verify / drill job を登録するものではない。

## 3. CLI の準備と鍵の初回 escrow

以下の shell 変数は、担当者が確認した値を指定する。
`PY` は動作確認済み Python 3.10 以上、`REPO` は対象コードの絶対パス、
`POLICY` は承認済み policy、`STATE_DIR` はローカル記録先、
`SNAPSHOT_DIR` は私有の日次 backup ディレクトリ。
ここでは保存先、日付、閾値、予定時刻の具体値を仮定しない。
新規の私有ディレクトリを準備する場合は本人が `umask 077` と mode `0700` を
用い、既存ディレクトリを黙って chmod して流用しない。

初回だけ、オーナー本人が非共有の対話端末で実行する。
`ESCROW_REASON` は非機密の操作理由。鍵値を agent・チャット・画面例・
共有ログへ渡さず、端末の画面記録と出力保存先も本人が管理する。

```bash
"$PY" "$REPO/mcs/ops/mcs_backup.py" keygen \
  --state-dir "$STATE_DIR" \
  --show-escrow --confirm-human --reason "$ESCROW_REASON"
```

成功 JSON の `escrow_key_hex` だけが鍵。専用 service `mcs-backup` への保存と
read-back を行い、新規 account を使う。既存 service の鍵や
`key_escrow.json` があれば置換しない。通常の各コマンドは鍵を表示しない。

`key_escrow.json` の `escrow_displayed:true` は表示操作の記録であり、
端末外に保管できた証明ではない。`custody_confirmed` はこの操作では
`false` のまま。オーナーは外部保管と [escrow-key drill](#escrow-drill) を
確認してから、policy の `key_custody_confirmed` を承認する。
失敗・中断で `status:pending` が残った場合、鍵が保存されていないとは
断定できない。記録や Keychain を削除して再実行せず、本人が保管状態を照合する。

鍵の import・置換・rotation・既存鍵の再表示を行う CLI はない。
端末喪失時に `keygen` で新しい鍵を作っても、過去 bundle の復号鍵にはならない。

## 4. 手動 offsite、receipt の退避、status

稼働中の原本 `ledger.db` ではなく、検証済みの静的な日次 backup を指定する。
WAL / SHM / journal の sidecar があるファイル、symlink、容量超過、
鮮度不明・超過の復旧点は拒否する。

```bash
"$PY" "$REPO/mcs/ops/mcs_backup.py" offsite \
  --policy "$POLICY" --state-dir "$STATE_DIR" --keychain \
  --snapshot-dir "$SNAPSHOT_DIR"
```

`--snapshot-dir` は日付名の降順で valid な静的日次 DB を選ぶ。
特定世代を選ぶ場合はこれを `--snapshot "$STATIC_SNAPSHOT"` に替える。
両方は指定しない。保存上限に達すると失敗し、旧世代を自動で消さない。

成功の receipt は stdout と `backup_state.json` に保存される。
媒体側の `.mcsb` と照合できる SHA と世代情報を、承認済みの独立した保管先へ
退避し、その保管が読めることを本人が確認する。鍵と receipt を暗号化媒体だけに
同居させない。転送・同期を使う場合、その権限と移送完了確認は別工程。
receipt の hash は検証時に媒体上のファイルから作り直して代用しない。

```bash
"$PY" "$REPO/mcs/ops/mcs_backup.py" status \
  --policy "$POLICY" --state-dir "$STATE_DIR"
```

`status` は鍵・SQLite・サービスへアクセスせず記録を読むが、policy の
私有ディレクトリと媒体 pin は検査する。`within_rpo:null` は復旧点不明、
`false` は許可された鮮度範囲外。成功した時刻だけでなく
`last_attempt_failed` も確認する。これは直近の操作の失敗に加え、直近の offsite
試行の失敗（`last_offsite_attempt_at` の試行）も示し、後続の verify / drill の成功では
解除されない。次の offsite 成功で解除される。失敗は過去の成功 receipt を取り消さないが、
新しい復旧点を作れたことにもならない。媒体・policy・記録が読めない場合は
正常な status を装わず非ゼロ終了する。

## 5. 自動 verify と人手 drill を分ける

自動 verify の対象は、担当者が事前に固定した `BUNDLE` と、
暗号化媒体とは別の信頼経路で得た `TRUSTED_SHA256`。鍵は Keychain のものを使う。

```bash
"$PY" "$REPO/mcs/ops/mcs_backup.py" verify \
  --policy "$POLICY" --state-dir "$STATE_DIR" --keychain \
  --bundle "$BUNDLE" --sha256 "$TRUSTED_SHA256"
```

全ファイル SHA と HMAC を復号前に検査し、復号・展開後に平文 SHA、
`valid_mcs_db`、`integrity_check`、`foreign_key_check`、schema と inventory の
一致を確認する。Keychain の鍵で成功しても、外部 escrow から回復できる証明にはならない。
通常 verify の一時平文は終了時に掃除するが、クラッシュ・電源断後の残留と
安全消去は別の owner policy が必要。

<a id="escrow-drill"></a>
### 人の手入力 escrow-key drill

実施間隔はオーナーが決める。Keychain を使わず、外部保管した鍵を本人が
取り出して検証する。`--key-fd` が読むのは**改行なしの 32 バイトの binary**。
64 桁の hex 文字列、JSON、任意のパスフレーズをそのまま渡すと拒否する。

以下は Bash の対話端末で、hex を非表示で入力し、復号した 32 バイトを
そのコマンドの FD 3 だけへ渡す補助関数。鍵を argv・shell 変数・環境変数・
平文ファイルへ置かない。端末で非表示入力できなければ失敗させる。
関数の出力や FD をログ収集対象にしない。

```bash
backup_with_escrow() {
  "$PY" "$REPO/mcs/ops/mcs_backup.py" "$@" --key-fd 3 \
    3< <("$PY" -c '
import getpass, re, sys, warnings
warnings.simplefilter("error", getpass.GetPassWarning)
if not sys.stdin.isatty():
    raise SystemExit("private interactive terminal required")
value = getpass.getpass("Escrow key (64 lowercase hex): ")
if re.fullmatch(r"[0-9a-f]{64}", value) is None:
    raise SystemExit("expected exactly 64 lowercase hex characters")
sys.stdout.buffer.write(bytes.fromhex(value))
')
}

backup_with_escrow drill \
  --policy "$POLICY" --state-dir "$STATE_DIR" \
  --bundle "$BUNDLE" --sha256 "$TRUSTED_SHA256" \
  --destination "$NEW_DRILL_DIR"
```

`NEW_DRILL_DIR` はまだ存在せず、policy の `scratch_dir` の直下に置く。
結果は `ledger.db`、`drill.json`、同意待ちマーカーを保持し、
`STATE_DIR/drills/*.json` と `backup_state.json` にも記録する。
訓練用 DB を原本や稼働対象へ流用しない。

## 6. 訓練の証拠・保持・平文 cleanup

成功 inventory には schema、テーブル件数、状態別件数、取得・返信の集計、
最後の正常 run 時刻が入る。schema・hash・件数の一致と所要時間を記録し、
`requests` と `command_receipts` の件数は担当者が必ず確認する。
原本側の未取得・未保存情報まで復元できたとは記録しない。

共有する手書き証拠は件数・hash・時刻・所要時間だけにする。患者名、本文、
添付内容、鍵、認証情報、PHI を含むパス・自由記述・実投稿の匿名化例を
repo や共有ログへ入れない。運用 JSON は私有ローカル記録のまま保管する。

暗号化世代の自動 prune はない。削除前に対象世代、残す復旧点、独立 receipt、
配送 journal の保持との整合を確認し、人が範囲を承認して実施する。
日次 DB の自動 7 世代保持を、offsite の保持承認に流用しない。

scratch 内の一時作業ディレクトリと保持された訓練 DB は平文である。
クラッシュ残留、訓練コピー、移送中の平文を対象別に確認し、オーナー指定の
期限・保管先・削除方式で cleanup する。一律の `rm -rf` や無断の自動清掃は行わない。
unlink は安全消去の証明ではなく、ファイル暗号化・媒体暗号化と保管管理が必要。

<a id="lost-terminal"></a>
## 7. 端末喪失後の鍵回復と held restore

1. オーナーと影響範囲を確認し、旧端末の扱い・認証情報失効を別の承認手順で決める。
   暗号化媒体、対応する独立 SHA receipt、外部 escrow 鍵、承認方針の記録を揃える。
   いずれかが欠ければ暗号化 bundle があってもこの経路は進めない。
2. 新端末に検証対象のコードと Python、OS OpenSSL を準備する。
   [導入手順](SETUP_AGENT.md)は準備の参考だが、収集・通知・adapter・scheduler・
   復旧 watchdog の自動起動まで一括で実行しない。復元・稼働への採用は別承認。
   config・資格情報・journal・添付実体の回復計画も別途確認する。
3. 本人が新端末上の媒体を識別し、既存の私有 `destination` と新しいローカル
   scratch・記録先を用意する。device/inode は新端末で再確認・承認する。
   元 bundle と同じ `policy_id` を用い、旧端末の pin・ローカルパスを無検証で
   再使用しない。鍵は `keygen` で作り直さず、外部 escrow を使う。
4. 上の `backup_with_escrow` 関数で、まず `drill` を行う。
   鍵・receipt・DB の検証が成功し、schema・件数・hash・正常 run 時刻を
   人が確認できてから、次の新規配置へ進む。
5. `NEW_DATA_DIR` は未作成で、親だけが既存の本人所有 `0700` の canonical
   絶対パスであることを確認する。暗号化媒体の下に置かない。
   稼働中や既存の `data/ledger.db` があるディレクトリは指定しない。

```bash
backup_with_escrow restore \
  --policy "$POLICY" --state-dir "$STATE_DIR" \
  --bundle "$BUNDLE" --sha256 "$TRUSTED_SHA256" \
  --destination "$NEW_DATA_DIR"
```

6. 成功時は `restore.json` と `awaiting_consent` のマーカーを DB 配置より先に
   永続化し、新規ディレクトリにだけ `ledger.db` を配置する。
   `restore_pending:true`、plain SHA・inventory の一致、私有権限を確認する。
   配置競合が起きても既存 ledger を置換せず、競合ファイルと hold を保持する。
   サービス起動・配送照合・通知・原本へのコピーはこの CLI は行わない。
7. [同意・稼働再開の境界](#consent)で停止する。成功した復号・配置を
   通知再開や原本上書きの許可として扱わない。

`max_rpo_seconds` は offsite 作成だけでなく verify / drill / restore にも適用される。
端末喪失から時間が経った世代や最後の正常 run が古い世代は、
`backup_rpo_exceeded_or_unknown` で拒否され得る。自動 bypass はない。
保存する世代と通常 RPO を決める際にこの制約を確認し、例外復旧が必要なら
オーナーが方針と鮮度・配送リスクを再評価する。値を勝手に増やして通さない。

<a id="consent"></a>
## 8. 同意・稼働再開の境界

新端末 restore は**配置後も同意待ち**で終わる。
[notify_reconcile](../../mcs/notify/notify_reconcile.py) は
`awaiting_consent` を解除せず、照合を skip する。
run_check の同意待ち経路も通常の収集・derive・送信を凍結する。
マーカーを手で消したり、phase を書き換えたりして迂回しない。
`--no-notify` だけでは outbox への登録防止や復元承認にはならない。

現行の [ops.restore_approve](../../mcs/ops/mcs_operations.py) と
[updater の承認照合](../../mcs/ops/mcs_update.py) は、更新・rollback の
損失報告 `report_id`、`backup_sha256`、`backup_schema` に束縛された receipt。
人の確認・actor・reason と receipt commit が必要で、過去の更新承認で代用しない。
backup CLI の `restore.json` はその損失報告ではなく、offsite 配置後の hold を
解除する汎用 CLI もこのモジュールにはない。

専用の[mcs_restore.py](../../mcs/ops/mcs_restore.py)で、復元DB・bundle・報告・
マーカー・配置先identityを束縛した`plan → approve → resume`を行う。
人のactor・reason・鍵保管の参照は明示入力で、既定値はない。
DBの置換・改変・承認との不一致・公開中断は保留を維持する。
実データでの実行は別途確定した人の同意が必要で、サービスは開始しない。

```bash
python3 mcs/ops/mcs_restore.py plan \
  --destination "$DEST" --source-sha256 "$BUNDLE_SHA"

python3 mcs/ops/mcs_restore.py approve \
  --destination "$DEST" --source-sha256 "$BUNDLE_SHA" \
  --plan-sha256 "$PLAN_SHA" --confirm-human \
  --actor "$ACTOR" --reason "$REASON" \
  --custody-ref "$CUSTODY_REF" --delivery-policy hold_all

python3 mcs/ops/mcs_restore.py resume \
  --destination "$DEST" --receipt-sha256 "$RECEIPT_SHA" \
  --confirm-human --actor "$ACTOR" --reason "$REASON"
```

`PLAN_SHA`と`RECEIPT_SHA`は対応する直前の結果から取得する。actorとreasonは
receiptと一致させる。別の配置先や古い承認の値を流用しない。
採用後の通常のDB更新は許容するが、DB identityと元の復元証拠を継続検証する。

この経路が再開するのは**収集のみ**で、`hold_all`による配送・照合の保留は残る。
添付実体と配送journalの欠落、失われた範囲、初回tickの再通知範囲は別に確認し、
配送を再開する許可にはしない。
既存の `notify_max_age_h` は正の有限値を要求するが、ここで時間数を推定しない。
配送済みか不明なものを未配送扱いで再送しない。

古い offsite 世代からの復元では、圧縮済み journal や失われた配送証拠を
復元 DB が補えるとは限らない。DB の件数・暗号の成功で配送履歴の完全性を
主張せず、必要な通知を人が照合する。[ライフサイクル仕様](../specs/lifecycle-spec.md)参照。

## 9. 定期登録の実装と実機での確認

定期 [wrapper](../../deployment/scripts/mcs_offsite.sh) は既定無効。
現行 wrapper は描画時の明示 opt-in（または `--enable`）と、私有 policy の
`scheduled:true` の両方を要求し、更新中の marker があれば起動しない。
設定からの描画値供給と health の記録接続は上記の実装を確認済みだが、
`services`は明示した毎日の時刻でHermes cronへ追加し、独立runtimeは同じjob定義を
単一host内で所有する。既定の6jobは維持し、backup無効化時は所有済みの追加jobだけを除く。
共有jobの維持・追加・無効化と独立hostの所有を合成検証した。
実機の登録・反映、新端末のサービス開始・hold解除は別工程。
描画済み wrapper が既にあり、一回の offsite 実行を明示承認した場合だけ、
次の明示呼出しを使える。`RENDERED_WRAPPER` は private な配備候補の絶対パスで、
未置換の source template をそのまま実行しない。この操作は定期登録を作らない。

```bash
/bin/bash "$RENDERED_WRAPPER" --enable \
  --policy "$POLICY" --snapshot-dir "$SNAPSHOT_DIR"
```

登録が完了したとの親側の確認と、実機の承認・検証が揃うまでは、
定期 job・外部媒体への退避・警報が継続して動いているとは主張しない。

非ゼロ終了時は秘密値や本文を含めずエラートークンを報告する。
鍵・MAC・receipt 不一致なら同じ媒体から hash を取り直して通さない。
媒体 pin 変更・RPO 超過・保存上限・私有権限の不足はオーナー判断に戻す。
途中の keygen、既存 destination、競合 ledger を無断で削除して再実行しない。
原本や端末外の保存物を変える前に、対象・復旧手段・承認範囲を確定する。
