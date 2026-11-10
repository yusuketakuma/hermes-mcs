# 07 — CSDC中央実装：既存構造への接続・認可・保存・配布

基準: `yusuketakuma/cloudflare-system-defence-center@239642c2c25b4a5e01d713e45bc7f680f3a37cda`。以下の「新設」は実装する配置であり現在存在するAPIとは言わない。

## CEN01: 所有と配置

|既存正本|維持・変更する内容|担当|
|---|---|---|
|`fleet/support/broker.js`|新auto brokerへ固定pathでdispatch。R1 heartbeat、R4支援、旧release v1のroute/上限は変えない|中央|
|`fleet/releases/apply.js` / `apply-wire.js`|v1 self/individual/case契約を維持。共通端末枠の競合guardだけ新family対応を追加|中央|
|`fleet/releases/state.js` / `contracts.js` / `signatures.js`|既存root/trust/attestation/bindingを利用。既存`selectCurrent()`の全stream最大値を顧客auto選択へ使わない|中央|
|`fleet/repository.js`|tenant scoped commit、auth/nonce/監査/quota、`db.batch()`を維持。一般tenantガードを外さない|中央|
|`fleet/tenancy.js` / `access.js` / `console.js` / `console-ui.js`|現在のmembership・Access・CSRF・scopeを使い、新しい配信操作を狭く追加|中央|
|`fleet/support/{operations,restarts,configurations}.js`と実際の関連guard|旧→新、及び新→旧の枠競合を同一commitで拒否する|中央|
|`fleet/releases/auto/`（新設）|`contracts.js, broker.js, authorization.js, repository.js, rollout.js, health.js, publication.js, artifact-store.js, signatures.js, admin.js`|中央|
|`contracts/mcs-auto/v3/`（新設）|共通schema、意味検証、正負vector、API、契約manifest。MCSへの正本|契約担当のみ|
|`fleet/migrations/<次番号>_mcs_auto_*.sql`（新設）|追加table/index。番号は実HEADの採番順を使う|中央|
|`test/`|既存sandboxへ追加。support/tenant/v1回帰も含む|中央|

上表以外の大規模リファクタリング・新DB・新ORM・新queueは不要。`auto/`の各fileは責務境界であり、現行コードが既に同じ責務を持つ場合は最小の既存helperを共用する。製品PythonをCSDCへコピーしない [C01,C02]。

## CEN02: 入口の四分離

### Device

POST `/v3/mcs/releases/{select,metadata,artifact,start,status,cancel-prestart,result,health,consent-preview,consent-confirm,consent-revoke}`。
既存SUPPORT_HOSTのboundary→厳格raw JSON→`resolveDeviceScope`→`verifyDevice`→tenant scoped `FleetRepository`の順序。bodyにscopeがあってもcredentialで確定したscopeとの完全一致を要求する。Host/path/method/body/nonce/timeのHMAC bindingを保持する。

新規prepare/startには、現在のdevice enrollment、realm、資格、standing consent、中央policy、releaseの現在Trust、互換性、配信割当、未解決枠、budgetを要求する。R4のcase/local diagnostic同意はstanding consentではない。現在のprovision machineryへ専用auto profile/purposeを**明示追加**し、既存`support`同意を拡大解釈しない。auto資格未登録なら拒否する。

### Publisher

POST/PUT `/v3/mcs/publications/*`。GitHub OIDCの厳格な期待値に一致する固定publisherだけ。端末HMAC、Consoleの閲覧権限、任意URLや汎用署名要求を受け付けない。publisherはrelease登録だけを行い、顧客同意・stage昇格・device grantを作れない。

### Console operator

POST `/admin/v3/mcs/rollouts/{preview,create,activate,advance,pause,resume,cancel,read,exclude-unstarted}` と `/admin/v3/mcs/policies/{preview,approve,revoke}`。
既存ConsoleのAccess/MFA認証・現在membership・CSRFを要求。閲覧と変更は分離する。グローバル段階操作は`self`のoperator_ownerと新たに登録した`release.rollout.manage`権限の両方を必要とする。tenant_adminは自tenantの同意/停止/端末状態のみ扱い、他tenantの一覧・全体stage・割合・順位を変更できない。新しい権限は本番で自動付与しない。

### Scheduler

既存の中央scheduled入口から有界な`evaluateRollouts`を呼ぶ。R1 ingestの通信枠・heartbeat schemaを変更しない。schedulerはhealth集約と自動PAUSE、次の既承認microbatch割当だけが可能。初期policyではstage昇格不可。スケジュールが重複しても同一DB CASが一回だけ勝つ。新規常駐daemon/キューは作らない。

## CEN03: flag・申告・権限

新しいflagは`MCS_AUTO_ENABLED`、`MCS_ROLLOUT_ENABLED`、`MCS_PUBLICATION_ENABLED`、`MCS_AUTO_ENROLLMENT_ENABLED`をすべて既定falseとする。既存SUPPORT_ENABLED/RELEASE_ENABLED、realm quarantine、現在policyも必要。旧`RELEASE_APPLY_ENABLED`は個別v1用であり、それをtrueにすることで顧客autoが有効になるようにしない。
OFF後も、既に発行済みoperationの`result/status/health`は下記の限定認証で受け付ける。停止後に結果が届かずUNKNOWNを解けない設計を避ける。ただしこれらは新STARTの権限を一切発行しない。
- 通常はcurrent credentialとscopeを使う。
- 正規rotation後は新credentialが同じdevice/installation/realmと元operationを証明すれば、古い結果bytesを報告できる。bodyを新credential用に改変しない。
- 撤去済み・compromiseとして失効したcredentialだけを根拠に新規報告を採用しない。既存metadata専用authで許される保持証拠を正確に対応付ける。許可を証明できない場合は人が再認証して照合し、未認証reportでslotを解放しない。
- flag OFF、中央pause、同意撤回、旧grantの現在期限切れは、当時発行したgrantに束縛された**過去結果の受領**を単独では拒否しない。現行epochと一致しない結果を新規実行許可に転用しない。

## CEN04: 共通契約と署名

03章のv3 DTOを正本とし、JS/PythonのどちらもCJSON・hash・PS256 message・safe integer・duplicate decoded key・8KiB payload/16KiB envelopeを同じvectorで検証する。JSの`JSON.parse`だけではduplicate keyを検出できないため既存`strictJSON`を維持する。
共通vectorは署名を省略した形検査だけで終わらない。C0では本packageの形/vectorを使い、実装のC1で**隔離環境内だけで作成したテスト鍵**による署名・検証・改変・別purpose拒否vectorを追加する。秘密鍵をrepo/packageへ収録しない。実鍵は別の導入受入である。
同じ配布物を一度だけ作り、metadata TTL更新は同じcoreを再署名するだけ。`release_id`や`code_seq`、asset digestを変えない。既存`release_bindings`は真の署名済み関係にのみ使い、新予約tableを同名で上書きしない [C08]。

## CEN05: 新しい永続化の単位

全tableは`mcs_auto_`または`mcs_rollout_`prefixを用いる（`sql/SCHEMA_DESIGN.md`）。同じFLEET_DB内で判定と更新を閉じる。R2とD1、signerとD1、GitHubとD1の分散transactionがあると仮定しない。

`FleetRepository.commit`の短命`request_commits`は最初のguard証拠でしかない。24時間cleanup後でも、次を保持する専用表から同ID/同bytesを判断する: 公開予約、asset manifest、stage action、device start、grant、attempt消費、outcome、prestart abort。tenant/device/realmとbody hashをunique keyへ結び付ける。元receipt削除→同じrequest実行を許してはいけない。

### TX01: 条件付きwriteのルール

1. transactionに入る前にrawを構造/意味検査し、署名やR2等の遅いI/Oを済ませる。mutable policyやtenant状態はこの段階で確定しない。
2. DBで一つのguardを作り、現在auth/membership/realm/consent/policy/rollout revision/割当/slot/quotaを再検証する。
3. `db.batch([...])`内の最初に、**その試行だけの新commit ID**の条件付き行をINSERTする。既存の同request記録を`$COMMIT`に誤利用して全writeを無条件再実行しない。
4. すべての後続writeを同guard receiptの`$COMMIT`に従わせる。guard不成立の0行はSQL例外ではない。最後に結果行を読み、存在しなければ409/保留。制約エラーなら全batch rollback [CF01]。
5. 永続dedupeを先に検出した場合は、元のraw/hashを照合してread-only replay処理へ入る。変更処理のbatchを再実行しない。
6. DB時刻は既存`unixepoch()*1000`の秒粒度を意識し、期限条件には既存の1秒安全余裕を維持する。署名TTLを伸ばすために時計を補正しない。

cached読み取り、KV、イベント到着順、GitHub concurrency、in-memory mutexは認可正本にしない。D1 read replicationを導入する場合もfinal CASは書込み側のguardで現在状態を判定する。必要な照会はprimary/一貫したsessionを使い、古いreplicaのpause状態でgrantを出さない。

## CEN06: device共通枠とtenant同時更新枠

現在の4family (`support_operations`, `support_restart_operations`, `support_config_operations`, `release_apply_operations`) の未解決枠を維持する [C03]。

新設`mcs_auto_operations`は`slot_held=1`を持つ。既存familyのproposal/admit/grantの**各mutation guard**へ新tableの不在を追加する。新autoのsign reservation/grant guardにも既存4familyの不在を追加する。異なるfamilyが並行readした後でも、書込みbatchが直列に現在を確認して一方しか開始しない。
共通helper（提案`fleet/support/device-slot.js`）へpredicateをまとめる場合も、caseごとの20件制限や独立integrity hold等を落とさない。autoはfake caseを作らず、独自の予算を持つ。helperがcaseを必須にする場合は責任を分ける。

さらに`mcs_auto_tenant_slots`のunique `(tenant,environment)`で、一tenantの複数Macへ並行STARTを出さない。全campaign共通のactive reservation最大10台も同batchで検査する。publicなRolloutPolicy値が小さければ小さい値を使う。global/per-tenant slotを占める状態は `signing_reserved/grant_issued/running/unknown`。
slotはTTLだけでは解放しない。grant期限切れは既にSTARTした可能性を消さない。解放は検証済みverified outcome、明示的prestart abort、同生存ownerのcancelled_no_effect証拠、又は別承認の復旧だけ。これらでもattemptのSPENTは消さない。

## CEN07: grant生成の二段階CAS

`authorizeStart`は次の順序で行う。

**A. reserve**: current standing enrollment、publication/Trust、割当、rollout authorization_epoch、profile/base、prepare witness age、時間帯、同operation/attempt、共通枠、tenant枠、全体枠を一つのguardで確認。`signing_reserved`行にexact request hash・grant ID・署名対象payload・期限・署名用途・現在の実行revisionを固定する。この時点では端末へgrantを返さない。

**B. sign**: transaction外で固定payloadを専用grant signerへ渡す。同payloadでもPS256署名bytesは変わり得るため、retryで別署名を公開しない。未返却candidateを複数作った場合はCAS勝者の一つだけを保存・公開し、後続は保存bytesを使用する。外部署名I/Oの曖昧さでpayload/grant IDを再採番しない。

**C. commit**: 原reserveと現在の同意・realm・割当・停止epoch・期限を再確認し、exact signed envelopeを保存して`grant_issued`へ。pause/撤回/CAS負けなら**一切返さない**。署名済みbytesがあることを許可済みとして扱わない。期限不足なら、既発行でないことを耐久証拠で確定して未START予約を閉じる。すでに`grant_issued`ならtimeoutを理由に閉じない。

**D. send/replay**: 正常commit後だけ保存bytesを返す。HTTP応答喪失は同request/hashで同bytesを再取得。再送時に期限を更新しない。別requestで同operationを作り直さない。

PauseとCのcommitの勝者が開始許可の境界。Cが先なら最大30秒の既発行grantは端末へ届いている可能性がある。pause後の新grantは出さないが、オフライン端末へ即時取消を保証しない。端末がlocal撤回/より新しいpause epochをeffect前に観測した場合は開始を拒否する。開始済みの整合性確定は妨げない。

## CEN08: standing consentの登録と撤回

R4の支援caseから権限を流用せず、新family `mcs-auto-consent-v3`の二者確認を持つ。

1. Consoleの当該tenant管理者が、登録済みdevice/profileに対するpolicy案をpreviewする。送信項目、許可クラス、保守窓、期限、停止手順、pilot opt-inを表示する。clientの自己申告だけでtenant登録を作らない。
2. 現在のscope・policy digest・nonce・confirmation ID・5分以下の期限を束縛した署名previewを端末へ返す。
3. MCSの人承認UI/TTYで同内容を確認し、private receiptと`StandingConsent`を保存する。無人flagで人確認を代用しない。
4. 端末から`consent-confirm`を送り、中央はpreview・現在のtenant権限/policy・scope・同意hashをCASしてenrollmentをactiveへ。中央ACKが未知ならlocalは「登録未確定」でautoOFF。
5. local撤回は先にepochを進めて無人STARTを禁止し、中央への報告失敗でも有効。中央撤回は今後のgrantを拒否する。既存grantの有限残存時間と既に始まった結果報告は別扱い。

pilot opt-inは通常auto同意に暗黙追加しない。preview/confirm/revoke用の閉schemaは共通contractに含む。完全自動の再同意・欠損時再作成は禁止する。

Enrollmentの中央提案作成はPOST `/admin/v3/mcs/enrollments/propose`、`EnrollmentProposalRequest→EnrollmentProposalReceipt`で固定する。当該tenant_admin又は明示的に担当するoperatorだけが作成でき、現在scope/profile/資格・承認済みRolloutPolicyと制限値を照合する。expected_enrollment_revisionをCASし、元の端末同意を自動置換しない。
proposalのcanonical hashを`H("enrollment-proposal", EnrollmentProposalRequest)`で固定する。ConsentPreviewのcentral_policy_id=proposal_id、central_policy_sha256=proposal_sha256、rollout_policy_sha256=承認済みRolloutPolicy hashとする。端末が後者だけを見て個別保守窓やpilot同意を省かない。proposalの確認有効期は5分以下、consent自体の有効期は指定値かつRolloutPolicy expiry以下。正規preview発行前に切れた場合は新しい人提案からやり直す。

## CEN09: artifact storage — 大容量一括PUTをしない

Workerのrequest body上限はCloudflare account planに依存し、Free/Proは100MB。Workerは128MB memoryの制約を持つ。旧案の256MiB一括requestや`arrayBuffer()`全量hashを採用しない [CF02,CF03]。

採用方式は **固定16MiBのcontent-addressed chunk**。最大256MiBのbundleは最大16chunk。最後だけ短くてよい。logical artifactは元の完全bundle一つであり、GitHub asset/digestも同一bundleを指す。chunkは中央の輸送・保管方式であって差分bundleではない。

### API

- POST `/v3/mcs/publications/object-manifests` (`ObjectManifestRequest`)：publication/fencing token/全体hash/size/順序付きchunk hash/sizeを先に固定する。
- PUT `/v3/mcs/publications/objects/{sha256}/chunks/{index}`：publisher OIDCとpublication/fencing token。headerは`X-MCS-Request-ID`（UUID）、`X-MCS-Publication-ID`、`X-MCS-Fencing-Token`、`X-MCS-Object-Manifest-SHA256`に固定し、path/index/期待chunk hash/sizeを保存manifestへ照合する。ObjectReceipt.request_idはこのrequest ID。同IDの再送は同tuple/body hashだけ。bodyは期待したchunkだけ。indexは0〜15、`Content-Encoding`はidentity、正確な上限・累積byte数・SHA256を確認する。
- POST `/v3/mcs/publications/objects/verify` (`ObjectVerifyRequest`)：保存済み全chunkをindex順にbounded readし、全体hash/sizeを再計算。成功したexact manifestをREADYへCASする。

通常assetが16MiB以下でも同じ1chunkの経路を使う。hashは`crypto.DigestStream('SHA-256')`又は同等に検証された非保持stream adapterを用いる。テストではNode標準`createHash`を注入する。`tee()`片側の無限先行bufferを作らず、read→size計測→digestへのbackpressure付きwrite→R2へのwriteを連動させる [CF03]。
R2 `put`のchecksum指定とcreate-only条件も利用する。既存同keyはsize/checksumを照合し、一致なら冪等、不一致はHOLD。ETagをSHA256として信用しない [CF04]。

R2 object keyは固定prefix + publication ID + object hash + chunk index/hashのみ。tenant入力・path・URLをobject keyへ直結しない。code artifact bucket/prefixは患者/支援添付と分離し、公開bucketにしない。新資源/費用は別承認。
D1 manifestがREADYになる前は配信不可。R2書込み後D1commit失敗はorphanとして残り得るが、存在だけで採用しない。cleanupは未参照・自分のstaging・保持猶予が証明できるものだけを別の所有済み処理で扱い、証拠や顧客ファイルを削除しない。

端末の`artifact`応答では、READY manifestのchunkを順に読み、一つのstreamとして返す。端末は全体size/hashを独立検証する。途中失敗は不完全downloadとして捨て、署名不正を「最新版なし」に変えない。中央も後からchunkが欠けていれば200成功扱いせずstreamをerrorで閉じる。
16chunk、認証・DB・R2処理数・hash CPU・総300秒・private領域の容量を予算化する。既存プランで通ると測定せず主張しない。上限不適合なら配信のみHOLDし、有料プランへ勝手に変更しない。

## CEN10: 安定性と保持

- control responseは最大16KiB。中央の大きいcohortはsigned digest/referenceで扱い、端末へ顧客一覧を送らない。
- schedulerは一tick最大100campaign/100候補mutationなど明示上限を持ち、cursorを耐久保存する。silent truncateで完了としない。実際の上限は既存予算との厳しい方。
- 同意・公開証拠・SPENT/attempt・結果・action receiptは通常cleanupから除外する。健康観測はdeviceごとの最新限定windowを持ち、stage昇格に使用したevidence digest/範囲/件数を固定する。容量不足で新grantを保留し、履歴消去で回避しない。
- UIのreadは変更を起こさず、再読込でstageを進めない。再送・異なるtabの同時承認は同action ID/CASで一回。
- CSDC自体の新版配備順は中央schema追加OFF→backendOFF→MCS bridge/資格→自社受入→限定enable。CSDCの古いWorkerが新tableを知らず共通枠を迂回しないよう、同一deviceを扱う全mutation経路の更新完了を有効化gateにする。

END-CSDC-BACKEND-V3.0
