# 03 — データ契約、API、永続化、意味検証

本章と`contracts/contracts-v3.schema.json`が新v3契約の正本。既存のrelease core/trust/attestation v1はそのまま再利用する。**文書版3.0は、既存製品schema全体をv3に変える指示ではない。**
新APIは実装する論理契約であり、配備済みURLではない。CSDCの実リポジトリと既存のFleetRepository/brokerを確認済み（00/07章）。その正本へ接続し、実環境への接続は別受入までOFFとする。

## 1. 構造検証とcanonical bytes

`$defs`の名前を指定して検査する。schema rootに対して単にobjectとして検査しただけでは合格にしない。
全objectはclosed、全fieldが必須。nullableは明示したfieldだけ。JSON Schemaは構造の規約であり、下記の意味検証・署名・独立した期待値との一致を代替しない。

### ENC01

通信入力は最大16,384 bytes、JSON depth最大32、総node数最大10,000、文字列のdecoded文字数とraw byte上限の両方を検査する。重複キー（escape後の同一キーも含む）、NaN/Infinity、float、`-0`、bool-as-intを拒否。unknown schema/propertyは補正しない。
署名payloadは既存`adapters.sdc.support_contract.canonical/parse_canonical`に合わせたASCII CJSON、最大8,192 bytes。鍵順序、escaping、integer表現を同じ公開vectorで検証する。Pythonの`json.dumps`任意設定を別canonical方式として持ち込まない。
base64urlはpaddingなしの一意表現だけ。decode→encode同一性、PS256 signature 384bytes、既存3072-bit公開鍵SPKI検査を維持する。新しいcryptoライブラリや暗号アルゴリズムを収集・解析coreへ追加しない。

### ENC02

boot_idは端末installation固有saltとOS起動identityから作る擬名witnessとし、hardware serial/hostname/生OS boot識別子を送らない。
wireの時刻はUTC Unix **milliseconds**、非負safe integer（2^53−1以下）。単位を秒・nanosecondsと混ぜない。
privateなstatのdevice/inode/mtime_ns/ctime_nsは`UInt64Decimal`（先頭ゼロなしの10進文字列）とし、意味検証で0..2^64−1へ制限する。inodeは0を拒否する。nanosecondsをsafe integerに押し込むと実際の時刻が範囲外になるため、この表現を採用する。これらprivate値をwireへ送らない。

### ENC03：hashの定義

既存のrelease_id / manifest_digest / trust_digestは既存のdomain-separated関数を維持する。新v3の値は以下で固定する。

```text
H(kind, payload) = sha256(ASCII("MCS-AUTO-v3\n" + kind + "\n") + canonical(payload))
body_sha256      = sha256(送受信・保存するexact raw bytes)
preflight_sha256 = H("prepared-public", PreparedPublic)
acceptance_sha256= H("release-acceptance", ReleaseAcceptance)
asset_set_sha256 = H("asset-set", name順にsortしたAssetSpec配列)
compatibility_sha256 = H("compatibility", 検証済みCompatibility payload)
request_id(BuildRequest) = H("publication-request", {repository_id,tag,tag_oid,commit,acceptance_sha256,publisher_policy_sha256})
attempt_key = H("automatic-attempt", {scope,source_target_commit,profile_sha256,config_contract_sha256,data_contract_sha256})
```

attempt_keyにtag、publication ID、日付、policy epoch、artifactの再圧縮digest、operation IDを入れて消費済み枠を迂回させない。通常の同じtarget commitへの自動STARTは同じattempt_keyとなる。
Publicationの同一tag予約はrequest_idより強いunique制約を持つ。後でacceptanceが更新されても同じtagで新しいbytesを公開しない。

### ENC04：署名の用途

新v3は`SignedEnvelope`のpurposeを明示し、既存署名プリミティブのdomain-separated署名message規則を使う。鍵pinの用途allowlistを新v3だけに限定追加する。旧v1のpurposeをv3として解釈しない。

|purpose|payload|役割|
|---|---|---|
|mcs-auto-offer-v3|Offer|端末別候補・no-candidateの証拠|
|mcs-auto-compatibility-v3|Compatibility|検証済みbase→targetの互換性|
|mcs-auto-grant-v3|StartGrant|一回の開始許可|
|mcs-auto-ack-v3|OutcomeAck|結果bytesの受領だけ|
|mcs-auto-status-v3|OperationStatus|request/受領の観測。開始権限ではない|
|mcs-publication-plan-v3|PublishPlan|限定publisherが扱う固定asset集合|
|mcs-release-acceptance-v3|ReleaseAcceptance|正式な検証受入|

rootは既存2-of-3とchain更新を維持。日常release署名keyとdevice grant keyは用途・アクセス権を分離する。公開鍵は初回bridgeで独立確認し、HTTP応答から新keyを自己登録しない。
BuildRequestのupstream_run_id/attemptは両方存在または両方null。nullは認証済みmanual/composed受入だけで許可し、workflow_runの対象SHA確認を省くためには使わない。

署名payloadが8KiBを超える場合は拒否する。上限拡大で済ませず、対応profile/asset数を減らすか、詳細証拠をcontent-addressed referenceへ分離する。大きいcompatibility catalogueはprofile別envelopeを最大4件まとめる保管物で、端末には該当envelope一件だけを返す。

## 2. 構造に加える必須意味検証

|ID|対象|必須検証|
|---|---|---|
|SEM01|全wire|ENC01、独立expected scope、canonical一致。入力自身をexpectedとして渡さない|
|SEM02|SourceRef|repo/owner ID登録値、厳密tag、各SemVer桁2^31−1以下、実tag OID/peeled SHA/main祖先|
|SEM03|ReleaseRef|release_idは認証core hash、commit/size/hash/code_seqは同coreに一致|
|SEM04|ReleaseAcceptance|6laneは重複なしexact集合。対象SHA、policy、署名actor、release checksが一致|
|SEM05|full_run|全lane executed/success、source_commit=target、jobのexact run/attempt/source。未実行は0|
|SEM06|composed|failureには解消証拠、reusedには元SHA/影響レビュー、local_equivalentには実測証拠と承認。未解消製品failureを拒否|
|SEM07|PublishPlan|request/asset集合/hash/seq/leaseを中央reservationへ照合。公開前後の同じID/bytes|
|SEM08|PublicationRecord|immutable公開証拠、全assetのID/size/digest、source、受入票の一致。suspended中はgrant禁止|
|SEM09|Compatibility|target/profile/base組を認証coreと実baselineに照合。schema/profile/config/data/control全て同じで禁止flagは全false|
|SEM10|StandingConsent|確認receiptを実TTY/人承認経路へ束縛。有効期日、enabled、scope、profile、epoch、中央方針一致|
|SEM11|MaintenanceWindow|weekdayは月曜0、start<end、同weekday重複なし。跨ぎは2record。IANA timezone実在、DST欠落時は窓なし、重複時でも同attemptは一回|
|SEM12|Offer|request_id/nonce/profileを保存要求に一致。candidate⇔selected!=nullかつELIGIBLE。none⇔selected=nullかつ理由!=ELIGIBLE|
|SEM13|Offer時刻|issued<=not_before<expires、TTL<=600秒。期限切れは新Offer照会、candidate実行許可へ昇格しない|
|SEM14|metadata|旧core/trust/attestationの期限・root/epochを維持。新しいmanifest_digestは古いplan/grantと混ぜない|
|SEM15|PreparedPublic|installed/current/target/profile/nonce/generationはprivate witness由来。患者hash/設定hashを入れない|
|SEM16|PrivatePreparedWitness|root/repo/lockの同一性、private snapshot、同じstage、backup、全retirement、容量予約。UInt64範囲を確認|
|SEM17|StartRequest|preflight hash再計算、consentと保守窓、publication/compat/manifest、attempt未消費、同operation一件|
|SEM18|StartGrant|同StartRequestの全binding一致。issued<=not_before<expires、TTL<=30秒、窓/consent/metadataの期限以内|
|SEM19|開始時刻|最新wall/monotonicとdeadlineを照合。boot変更、clock後退、1秒を超える既存許容外skewは開始禁止|
|SEM20|StartIntent/SPENT|op/grant/nonce/attemptに一意。intent→SPENT同期成功後だけ同processRAMcap。読取りだけで再発行しない|
|SEM21|PrestartAbort|ローカルintent/SPENT不在と同nonce終結receiptを確認。中央は同grant/operationの未消費予約だけCAS。timeout単独では未START認定しない|
|SEM22|RuntimeProof|exact必要service集合、source/profile/control、boot/generation、fresh birth/heartbeat/activation。同一PIDだけは不十分|
|SEM23|ServiceProof|residentはrunning＋instance/PID/birth/loaded非null、2heartbeat。timerはarmed＋設定検査、disabledはstopped。各proofは対象opへ束縛|
|SEM24|Outcome verified|readiness=ready,error=NONE,verified_at非null,installed=verified_installed=target。START<=verified<=observed|
|SEM25|Outcome unknown|readiness=unknown,verified_at=null,error!=NONE。既知installedと以前のverified_installedを保持できる|
|SEM26|Outcome cancelled|readiness=not_started,error=NO_EFFECT_CONFIRMED,verified_at=null。同生存ownerの入口未消費証明が必要|
|SEM27|OutcomeAck|request_id=Outcome.report_id、nonce=Outcome.request_nonce、report/op/scope、hash=sha256(canonical Outcome bytes)。受領は実行stateを変えない|
|SEM28|OperationStatus|元StatusRequestのID/nonce/kind/object/op/scope一致、TTL<=60秒。not_foundはobject_sha256=null。これをcapabilityにしない|
|SEM29|OwnerState|active/spent/outcomes/bindings/outbox参照整合、seq/世代高水位非後退、operation/grant/attemptの一意、verifiedは終結証拠を持つ|
|SEM30|Owner revisions|全保存でrevision増分。authority/active/baseline/generation変更でexecution_revisionも増分。ACKだけはexecution_revision不変|
|SEM31|記録容量|raw owner<=8MiB、private witness<=64KiB、最悪完了＋outbox＋同期tempの容量予約。schema配列max未満だけでは不十分|
|SEM32|outbox|rawの正確なhash、parsed OutcomeのID一致、同ID異bytes拒否。再送でreport_id/sequence/observed時刻を作り直さない|
|SEM33|再導入|owner/activation/root/外部floor不整合や古いdisk snapshotをfresh installと認定しない|
|SEM34|境界値|enum・型・max bytesの全境界、bool/int混同、ns整数精度、NFC/case衝突を合成vectorで確認|

`RuntimeProof.services`の観測上限は64。インストールprofileがそれ以上なら無言truncateせず未対応としてHOLDする。
source/baselineへの変更とoutbox受領は独立revisionで扱うが、これを理由にconfig/credentialの真の変更を無視してはいけない。

## 3. SDC APIの具体的な追加面

単一の登録済みHTTPS originに以下の**固定POST path**を追加する。既存monitor/support/restartのPATHSは広げず、専用v3 transportのallowlistを用いる。既存pathと衝突する場合はサーバー側実装前にbase pathを一括版管理し、任意URL入力は追加しない。

|path `/v3/mcs/releases/…`|request $defs|成功response|副作用|
|---|---|---|---|
|select|SelectionRequest|200 SignedEnvelope(Offer)|候補選択。インストール許可なし|
|metadata|MetadataRequest|200 既存attestation envelope または SignedEnvelope(Compatibility)|同releaseの正確なmetadata。kindで閉じる|
|artifact|ReadArtifactRequest|200 application/octet-stream|同digestのbytesのみ。実行許可なし|
|start|StartRequest|200 SignedEnvelope(StartGrant)|一意の中央grant予約。端末START済みとはみなさない|
|status|StatusRequest|200 SignedEnvelope(OperationStatus)|保存済みrequest/ACKの観測のみ|
|cancel-prestart|PrestartAbort|200 SignedEnvelope(OperationStatus)、kind=prestart_abort|同じ準備の取消。spent/unknown解除不可|
|result|Outcome|200 SignedEnvelope(OutcomeAck)|exact bytesの冪等受領のみ|

端末認証は既存device/key/time/nonce/signatureのHMAC contractを専用pathへ拡張し、body/path/method bindingを維持する。ヘッダーnonceはHTTP送信ごと、payloadのrequest_nonceは論理requestのretryで固定。サーバーはcredentialからtenant/device/realmを解決し、body自己申告だけで権限を選ばない。
metadata応答の既存envelopeはrequest nonceを含まなくてもよいが、受信側が独立して固定したrelease_id/manifest_digest/profileと完全一致させる。期限更新でmanifestが変わった場合はselectから新たに計画し直し、PREPARED中に勝手に切り替えない。

errorは`{schema_version:"mcs.release-error.v3",code:<固定enum>,retry_after_ms:<0..86400000>}`のclosed object、最大1KiB。固定コードは末節参照。401/403/409/410/413/422/429/503を用途で区別する。HTML、302、曖昧な202、長すぎる本文、構文不正は成功にしない。
`result`のACK TTLは最大300秒。遅れたACKはsame raw Outcomeを再送してfreshなACKを取得できる。実行stateへ影響させない。
`status`のnot_foundは「サーバーにその記録がない」であり「端末では何も実行していない」の証拠ではない。

### 3.1 artifact streaming

リクエストは16KiB以内の認証JSON。応答はContent-Type=application/octet-stream、Content-Encoding未設定またはidentity、予想Content-Lengthとサイズ一致を検査する。長さが無ければ累積bytesで同じ上限を強制する。hashはstream中に計算する。通常TLS検証、proxyなし、redirectなし、総deadline300秒、chunk64KiB。
中央のGitHub取得だけは登録済みrepo/release/assetからの200/302を扱う。LocationのHTTPS host allowlist、port/userinfo禁止、最大3redirect、private/link-local/loopback解決先拒否、host変更時Authorizationを転送しない。一般URL fetch/proxyにしない。設定したGitHub download hostが変わったら保守で検証し、ワイルドカード全host許可に拡張しない。
初期実装はRange/部分再開なし。途中ファイルは非採用として管理し、次回再検証済みの同bytesを最初から取得する。

## 3.2 Publisher専用の中央API

端末credentialでは利用不可。01のOIDC条件を満たすpublisherだけが使用する。device grant signer/root管理には到達させない。

|path|method/input|response|契約|
|---|---|---|---|
|`/v3/mcs/publications/reserve`|POST BuildRequest|PublicationReservation|同tag/sourceの一意予約とcode_seq。ここではasset未確定でもよい|
|`/v3/mcs/publications/object-manifests`|POST ObjectManifestRequest|ObjectReceipt|全体/固定16MiB chunk集合を先に固定|
|`/v3/mcs/publications/objects/{sha256}/chunks/{index}`|PUT exact chunk、publication/fencing token|ObjectReceipt|1request最大16MiB、既存同bytesだけ冪等、異bytesは拒否|
|`/v3/mcs/publications/objects/verify`|POST ObjectVerifyRequest|ObjectReceipt|全体stream hash再計算後、READYへCAS|
|`/v3/mcs/publications/finalize`|POST PublicationFinalizeRequest|SignedEnvelope(PublishPlan)|既存core validatorとreservation seq/source、target lock/profile、acceptanceを検証して限定release署名とmetadata asset生成|
|`/v3/mcs/publications/register`|POST PublicationRegisterRequest|PublicationRecord|公開済みのexact GitHub ID/assetとtrusted verifier証拠を照合して登録|

reserve/finalizeのmetadataは128KiB以下、payload内のrelease coreは8KiB以下。これはサーバーのpublisher専用面の上限であり、既存device transportを広げない。
finalizeのcandidate_assetsはbundleと説明画像だけ。reservedな4metadata名（core/initial attestation/compatibility/acceptance）は中央が認証済み材料から生成して固定する。任意payloadへの汎用sign APIを作らない。
packagerはcode_seqが必要なcoreを先に確定しない。bundle bytesとcore材料を準備→reserve採番→core形成/検査→finalize署名→全asset集合固定と進む。tag/source treeは一切変更しない。

registerで`release_verified=true`というcallerのbooleanだけを信じない。専用verify-register jobの実OIDC、承認verifier closure/toolchain、当該run/attempt、保存済みGitHub原証拠hashを束縛する。中央もGitHub APIからRelease ID/tag/immutable/asset集合を再照合する。
中央runtimeがGitHub CLIを実行できるとは仮定しない。公式CLIの暗号検証は固定したverifier jobが行い、その結果を上の認証済み契約で受領する。中央で独立検証する実装が存在するなら追加できるが、未実装の検証を実施済みと表示しない。
同OIDC/jtiを別publicationの書込みへ流用させない。retryは同request/hashだけ。これらのendpointは07章のCSDC固定routerへ対応付ける。本番の承認済みoriginを確認するまで実ネットワークを呼び出さない。

## 4. 中央の永続化（既存DBへ追加する論理table）

製品側はstdlib制約を維持する。以下は責務の概要。正確な全table/unique/guardは`sql/SCHEMA_DESIGN.md`と07章。既存FLEET_DBのD1 batchで実現し、別の遠隔操作基盤を作らない。

|table|主キー/unique|内容|
|---|---|---|
|mcs_auto_publications|request_id PK; unique(repo_id,tag)|tag OID、SHA、受入票、code_seq、phase、revision、fencing token|
|mcs_auto_objects|sha256 PK|write-once bytes、size、content type。公開アセットと原証拠を分類|
|既存release_bindings|既存product/trust_domain/environment/stream/code_seq|署名済みbindingを再利用。新しい予約sequenceはmcs_auto_streamsで原子的に管理|
|mcs_auto_publication_records|publication_id PK; unique(repo_id,github_release_id)|PublicationRecordと不変asset集合|
|mcs_auto_enrollments|scope PK|同意id/epoch、policy、profile、device realm、停止、verified floor|
|mcs_rollout_assignments|assignment_id PK、同rollout/tenant/installation/device/profile一意|固定cohort/target/期限、stage、観測状態|
|mcs_auto_start_requests|unique(device_id,request_id); unique(device_id,operation_id)|request hash、exact grant bytes、expires、state|
|mcs_auto_attempts|unique(device_id,attempt_key)|reserved/consumed/unknown/cancelled_no_effect/verified、元operation|
|mcs_auto_outcomes|unique(device_id,report_id); unique(device_id,operation_id,report_sequence)|exact raw/hash、受領時刻、正規の状態遷移|
|mcs_rollout_actions|audit_id PK|同意・停止・復旧の認証済み変更。患者情報なし|

### TX-PUBLISH

予約採番はtransaction内でstream seqを増分し、同じtagへ同じcode_seqを割当てる。phase updateは`WHERE revision=expected AND fencing_token=current`でCAS。ゼロ行更新は競合であり成功ではない。
古いpublisherがGitHub APIを既に送った可能性はfencingだけで消せない。そのため各writeは同じ固定Release/asset集合だけへ収束させ、tag再読取り・exact ID・digest検査を必須にする。新lease取得時も公開結果を照会し、旧run不明を理由に異なるbytesを採用しない。

### TX-START

1. 認証device/epoch、同意、window、publication、rollout、profile、現在のbaseline/floorを照合。
2. request IDが既存ならbody hash一致で**同じ保存grant bytes**を返す。不一致は409。
3. 同operation別request、同attempt既消費/unknown、他active reservation、local/central holdがあれば拒否。
4. preflight age<=5分、同generation/boot、manifest TTLを確認。TTL30秒以下のgrantを発行してexact bytesを一意予約としてcommitする。
5. commit後の応答喪失は再生成しない。clientは同request再照会。期限切れはstatus/明示prestart abortの契約へ。

中央は署名鍵を渡さず、認証済み内容だけをsignする。CEN07のsigning_reserved→transaction外sign→現在状態再検証CAS→保存bytes返却を必須にする。phase、同端末/tenant/global枠、response未返却を曖昧にしない。

### TX-ABORT

deviceがローカル未STARTを証明した同じop/prepare_nonceのabortを保持する。同request/hashは冪等。中央に結果unknown/consumed/verifiedがある場合は取消不可。未START予約を終結した後だけ次prepareを許可する。攻撃者による全disk rollbackをclient申告だけで消さず、realm/高水位不一致は再導入保留。

### TX-RESULT

同report ID+同bytesはACKを再返却。異bytesは409。operation/grant/attempt/targetを中央保存の発行記録に照合。
状態はunknown→verifiedへの**観測的な後続report**のみ許容し、verified→unknown/別targetやsequence後退は拒否。後続reportは新report ID/次sequenceを取り、旧rawを上書きしない。
中央の成功受領はdeviceローカルの終結を作らない。deviceの次grantは自身のverified baselineと中央受領との同期確認を必要とするが、前のACKをdeviceが受け取っていないだけなら同body再送で解消できる。network断時に現在アプリを停止させない。

## 5. ローカル永続状態と多ファイル境界

正本`release-owner.json`に`OwnerState`、同じ既存activation fenceを維持する。state保存はO下のload→意味検証→whole-state CAS→新tempへwrite→fsync→atomic replace→directory fsync。
statのidentityはnofollow/private owner/mode/nlinkの既存検査を維持。正本のinodeはatomic replaceで変わるため、永続identityとして固定するのは**lock inode/root**であり、正本inodeそのものを次保存まで不変と要求しない。

|record|保存・読取り条件|削除/再利用|
|---|---|---|
|release-owner.json|唯一のv3正本、最大8MiB|旧SPENT/outcomeは消さない|
|release-owner-activation.json|legacy拒否の永続fence|削除して旧入口を開かない|
|release-start-intent.json|START前、exact op/grant/hash|完了証拠をownerへ保存後、旧intent bytesを証拠保管したうえで次op用へ原子的置換可。消失だけで未STARTとしない|
|release-auto-private.json|private witness、64KiB以内、op/nonce binding|確定済みopのcleanup方針は自分のstage等に限定。元証拠のhash/referenceはownerへ残す|
|release-runtime-proof.json|op/generation/boot/sourceの限定proof|同世代のbounded snapshot。履歴は終結digestをownerで保持|
|既存update_state.json|既存journalへのv3 operation参照|勝手に全体初期化しない。ownerとの不一致はHOLD|
|既存v1のbootstrap/planning/change/successor等|移行前全件棚卸し|unknown/pendingを破棄・成功化しない|

上の新filenameは`Store.NAMES/LIMITS`へ明示追加し、任意filenameを許すAPIを作らない。private witnessに存在しないファイルはmissingを明示する別source一覧へ記録し、架空のinodeで埋めない。

**改訂補足**: 実装時に必要な証拠が最大64KiBへ収まらないprofileは未適格とする。上限超過をsilent truncationしない。通常activeの全証拠とgrant/manifest/consentのrawが結果確認時に解決できることをcapacity予約に含める。
大きい旧owner/source manifestは`StoredEvidence`でprivate object_key（SHA-256名）へ参照する。保存先はcontrol root直下の固定`evidence/` directoryだけ、最大8MiB/file、名前は64桁hash、no symlink・single-link・0600。新規publishはO_EXCLまたは同bytes照合、overwrite禁止。v1 rawを16KiBに切り詰めない。operation_evidenceはmanifest/compatibility/consent等の署名rawを保ち、参照不明はHOLD。object_key=raw SHA256を要求する。
OwnerState.lock_fingerprintsはG/U/Q/R/C/A/M/Oの導入済みinode等を束縛し、全開始・復旧で再照合する。startup_nonceはActiveOperationで起動前に保存し、新process自己申告から期待値を作らない。installed/verifiedの全source manifest digestも別々に更新する。

ownerとjournal/intentは同時commitできない。ownerは権限消費・終結の正本、intentは曖昧な未実行扱いを防ぐfence、journalはeffect位置の補助とする。crash時に整合する正本へ観測だけで収束できなければUNKNOWNを保持する。

## 6. エラーと再試行の分類

|code|表示/分類|自動再試行|
|---|---|---|
|NO_CANDIDATE|候補なし（有効なsigned noneのみ）|通常poll|
|NETWORK_UNAVAILABLE / RATE_LIMITED|取得または認可保留|START前の同requestのみ、backoff/Retry-After|
|SIGNATURE_INVALID / SCOPE_MISMATCH / CONTRACT_INVALID|検証失敗|同候補の無限retry不可。状態変化/人確認まで保留|
|PUBLICATION_UNVERIFIED / PUBLISH_PERMISSION_HOLD|公開証拠/権限不足|安全なread再照合。createを反復しない|
|COMPATIBILITY_HOLD / PROFILE_UNQUALIFIED|保守更新が必要|通常auto不可|
|CONSENT_REQUIRED / CONSENT_REVOKED / OUTSIDE_WINDOW|端末の許可条件不成立|同意/時刻の正規変化後だけ|
|PREPARE_BUSY / PREPARE_BUDGET_EXCEEDED|業務優先の延期|準備予算内だけ|
|WITNESS_CHANGED / START_EXPIRED|開始しない|未STARTの正式終結後、新prepare|
|ATTEMPT_SPENT / START_OUTCOME_UNKNOWN|一回性保留|旧grant/別IDで回避しない|
|CAPACITY_HOLD / STORAGE_UNKNOWN|容量/保存の不確実性|START前のみ是正後再計画。後は観測/人対応|
|APPLIED_UNVERIFIED / RUNTIME_HOLD|新版稼働未確認|観測的確定のみ。無承認restartなし|
|LEGACY_STATE_HOLD / RECOVERY_GENERATION_HOLD|移行/復旧器未適格|bridgeの別承認|

NO_CANDIDATEはsigned Offerのstatus説明であり、未署名errorを根拠に正常な「候補なし」へ移さない。それ以外のAPI error codeはこの表の固定値に限定し、生exception、絶対path、hostname、原log、backup digest等を送らない。詳細なローカル原因はprivate診断へ格納し、外部用DTOはallowlistから新たに構築する。

## 7. 段階配信と同意の追加契約

以下を既存v1や旧auto提案v2に混ぜず、v3の必須fieldとして定義する。

|定義|追加の意味検証|
|---|---|
|RolloutPolicy|stageはself,pilot,pct10,pct50,generalの順でexact5件。bpは0,0,1000,5000,10000。promotion_mode=manual。health gap>=interval、鮮度<=gap、issued<expires|
|CohortSnapshot|customer tenantにselfを含めない。tenant/rank/deviceの重複なし、rankは1..N、各deviceはscope/qualified profile/standing consentに対応。上限超過をtruncateしない。中央私有record最大8MiB、署名はdigestだけ|
|Assignment|snapshotの実device/tenant/profileに属し、assigned_stage不変、assigned_at<start_by。未許可tenantへ作らない|
|RolloutBinding|selected→prepared→StartRequest→StartGrantは完全一致。rollout_epoch=authorization_epoch。Outcome/Healthは発行時のbindingに一致し、現在stageへ書換えない|
|RolloutPreview|read-only。stage/action/件数/阻害理由とrevision/health_revisionを同snapshotへ束縛。TTL<=300秒|
|RolloutActionRequest|pauseだけpreviewを省略可。advance/activate/resume/excludeはpreview必須、target_stage/assignment集合を独立保存previewへ照合。skip/除外でunknown解消不可|
|HealthObservation|healthy iff reason=NONE。healthyは元verified outcome＋same target/runtime/boot evidenceに束縛。sequence単調、同ID異bytes拒否、replayでfresh時間を延長しない|
|HealthAck|report/request ID、nonce、scope、exact observation hashを照合。TTL<=300秒。ACKは新実行権限ではない|
|ConsentPreview|現在の中央proposal、scope/profile/policy、現在epoch、保守窓、期限、pilot opt-inを固定。TTL<=300秒。人確認後のreceiptを既存監視同意から作らない|
|ConsentConfirmRequest|StandingConsentのpolicy/window/profile/early_adopter/epochがpreviewに完全一致。confirmation receiptは実機人確認の私有証拠と対応|
|ConsentRevokeRequest|new_local_consent_epoch>previous、scope/ID一致。通信失敗してもlocal撤回を戻さない|
|ConsentReceipt|request ID/nonce/scope/consent hash一致、TTL<=300秒。active受領までauto不適格。revokedをactiveへ再解釈しない|
|ObjectManifestRequest|indexは0から連続、最後以外16MiB、全sizeの和一致、total<=256MiB、chunk<=16、固定publication/fencing。hashをheader自己申告だけで検証済みにしない|
|ObjectVerifyRequest/ObjectReceipt|manifest固定、R2 exact bytes→全体hash→D1 READY CAS。分散transactionを仮定しない|

### 7.1 新しい署名用途

`mcs-auto-health-ack-v3→HealthAck`、`mcs-auto-consent-preview-v3→ConsentPreview`、`mcs-auto-consent-receipt-v3→ConsentReceipt`、`mcs-rollout-policy-v3→RolloutPolicy`を専用pin用途へ登録する。既存v1のacceptanceに流用しない。
`H("rollout-policy", RolloutPolicy)`, `H("cohort-snapshot", CohortSnapshot)`, `H("rollout-preview", RolloutPreview)`, `H("rollout-action", RolloutActionRequest)`, `H("object-manifest", ObjectManifestRequest)`を新hashとして用いる。cohort canonical化はfreezeされたrecordだけ。health/outcomeの再送hashはdomain hashではなくexact raw SHA256。

### 7.2 全API追加

|固定path suffix|request|success payload|
|---|---|---|
|`/v3/mcs/releases/health`|HealthObservation|SignedEnvelope(HealthAck)|
|`/v3/mcs/releases/consent-preview`|ConsentPreviewRequest|SignedEnvelope(ConsentPreview)|
|`/v3/mcs/releases/consent-confirm`|ConsentConfirmRequest|SignedEnvelope(ConsentReceipt)|
|`/v3/mcs/releases/consent-revoke`|ConsentRevokeRequest|SignedEnvelope(ConsentReceipt)|
|`/admin/v3/mcs/rollouts/create`|RolloutCreateRequest|RolloutAdminReceipt（draftのみ）|
|`/admin/v3/mcs/rollouts/preview`|RolloutPreviewRequest|RolloutPreview|
|`/admin/v3/mcs/rollouts/activate`|RolloutActionRequest(action=activate_self)|RolloutAdminReceipt|
|`/admin/v3/mcs/rollouts/advance,pause,resume,cancel,exclude-unstarted`|RolloutActionRequest（pathとaction一致）|RolloutAdminReceipt|

admin readは既存Consoleのread認可でtenant別の固定viewを返す。action mutation DTOをreadに使わない。大きい一覧はopaque cursor、page<=100、返却byte上限内でページングし、対象tenantをcursorから推測で選ばない。policy proposal/approvalは既存の信頼済み設定承認の意味を保ち、RolloutPolicyのid/hashを表示し承認receiptへ固定する。一般端末APIでpolicy/root/鍵を作らない。

通常制御16KiB/署名payload8KiBを維持する。中央内部snapshot（8MiB）とpublisher metadata（128KiB）は別の入口/adapterであり既存device上限を広げない。

### 7.3 管理read/policy契約

`contracts/api-contract.json`をroute一覧の機械正本とする。`RolloutReadRequest/Response`は認証済みoperatorの範囲内の固定viewだけ返し、readでstage mutationを起こさない。tenant専用画面は当該tenantに絞ったviewを使い、global routeをtenant_adminへ開かない。
`PolicyPreviewRequest→PolicyPreview→PolicyActionRequest→PolicyReceipt`をpolicy approve/revokeへ使う。現在のoperator actor_ref、preview hash、expected policy revision、対象policy canonical hash、期限・CSRFをcommit時に再照合する。初回expected revisionは0、既存headには正確なrevisionを要求する。revokeは保持済みpolicyをactiveへ戻さず、新規grantを拒否する。既存campaignのimmutable policy bytesは改変しない。初期auto拡大policyへの署名は承認receipt正常commit後のみ。
Draft createはまだ端末実行を認可せず、cohort seal前のreceiptではcohort_sha256=nullを許す。署名を必要とするpolicy/grant/device-responseと、現在のAccess認証上で見るConsole previewは役割が違う。

### 7.4 tenant別の初回提案

`/admin/v3/mcs/enrollments/propose`はEnrollmentProposalRequest/Receiptを使う。tenantの管理権限、expected revision、登録profile、承認済みRolloutPolicyを検証し、proposal ID/hashをConsentPreviewRequest.central_proposal_idに対応させる。ConsentPreviewではcentral_policy_id/hashが個別proposal、rollout_policy_sha256がglobal templateを表す。StandingConsentの両hashを同時に照合する。変更した保守窓やearly_adopterを古いpreviewで確認してはいけない。旧standing receiptがある場合も中央提案だけでlocal同意を上書きしない。

END-CONTRACTS-V3.0
