# 08 — 段階配信：固定母集団・認可・観測・昇格・停止

## ROL01: 単位と不変性

`PublicationRecord`は不変の公開物、`RolloutPolicy`は承認済みの配信条件、`Rollout`は一回の配信campaign、`CohortSnapshot`は固定対象、`Assignment`は一端末への固定割当、`StartGrant`は一回の開始許可である。互いを同一のstateにしない。

- 1 campaignは1 release_id/publication/artifact digestに固定する。同じReleaseをself/pilot/10%/50%/generalで使う。stageごとにtag/rebuild/signature coreを作り直さない。
- `(product, environment, stream_id)`に、未終結のactive rolloutは最大1。新Releaseの登録はできるが、進行中の対象を自動差替えしない。
- 10%と50%は**customer tenantの累積件数**。device数ではない。selfは分母へ入れない。pilotは顧客分母に含める。
- 実際の実行許可はdevice単位。tenant内は最大1台のsign予約/発行済みgrant/実行/unknownだけ。全campaign共通で最大10台、policyが小さければ小さい値を優先する。
- 通常auto不適格、同意未登録、移行未受入のdeviceを母集団へ入れない。未適格なprofileは別profileの成功で通さない。

## ROL02: policyの初期テンプレート

初期templateは下表。既に本番同意を得た値ではない。Console previewで同意・承認したpolicyに固定し、途中で待機時間や上限を短縮しない。

|stage|顧客tenant累積上限|必須観測|昇格|
|---|---|---|---|
|self|0|適格profileごとの自社Mac、正式assetでVERIFIED後連続60分|人承認でpilotへ|
|pilot|明示選択p件|pilot opt-in済み顧客、全profileの検証端末でVERIFIED後連続24時間|人承認でpct10へ|
|pct10|max(p, ceil(N/10))|そのstageで開始した全対象と既更新群の健全性、連続24時間|人承認でpct50へ|
|pct50|max(p, ceil(N/2))|同上、連続24時間|人承認でgeneralへ|
|general|N|残りを小batchで適用。各対象VERIFIEDと連続60分|条件確認でcompleted。未適用は別表示|

promotion modeは初期`manual`のみ実装する。自動昇格は今回の受入を満たしたことにせず、将来の別承認変更とする。事前承認templateに`start_self_on_registration=true`がある場合だけ、公開登録後にselfへの割当を自動開始できる。それが無い場合はdraftのまま人が開始する。tagだけでpilot以降が自動承認されたことにはならない。
健康reportの初期間隔300秒、最大許容受信gap900秒、client観測鮮度最大600秒。これらは準備時間120秒、grant30秒、保守窓と別の時計である。装置の業務稼働時間・sleepを実機受入で確認する。

## ROL03: 正確な母集団と順位

### Freeze

1. 対象publication、supported profile、現在のtenant/device enrollmentとstanding consent、機械的互換性が一致する候補を中央から読む。顧客名・患者情報は不要。device IDは登録済み擬名IDだけ。
2. `CohortSnapshot`用tableへ、tenantとdevice行を**同じguard付きD1 batchのINSERT…SELECT**で複写する。freeze_atをDB時刻で固定する。上限は初期1000tenant/4096device/8profile。上限超過はHOLDでありLIMIT切捨て不可。
3. Snapshot行はappend/update不可。以後の新規tenant/deviceはこのcampaignへ自動追加しない。撤回・失効は現在の開始判定で除外するが、分母Nや元行は変えない。
4. ランク計算は凍結行に対してのみ行う。中央だけが持つ`ranking_key_id`のHMAC keyを使用し、`HMAC-SHA256(key, ASCII("MCS-ROLLOUT-RANK-v3\n") + CJSON({rollout_id,tenant}))`を計算する。digest全体の昇順、同値はtenant slugのASCII順とする。
5. pilotはoperatorが明示的に選び、pilot opt-inとprofile coverageを確認する。pilot以外を上記順位で並べる。**moduloの閾値判定ではなく、必要件数分の先頭を選ぶ**。
6. scope/profile/consent-at-freeze/device集合、pilot集合、順位、policy hash、ranking key IDを含むsnapshot canonical recordのSHA256を固定する。recordをblob保存する場合もD1の件数・行digest・sealed revisionに束縛する。署名payload8KiBへ巨大一覧を入れない。全行一致のsealまでcandidateを返さない。

### 計算

```python
# 概念式。N/pはfreeze済みcustomer tenant件数。
pct10_cap = max(p, (N + 9) // 10)
pct50_cap = max(p, (N + 1) // 2)
general_cap = N
# 対象集合 = pilot集合 + 非pilot順位の先頭(cap - p)件
```

N>0ではp>=1かつp<=N。複数profileのpilot coverageのためpがceil(10%)を超える場合、明示的な`pilot_minimum_exception`としてpreviewに表示し承認する。「10%を厳密に守った」と表示しない。

|N|p|pilot|pct10累積|pct50累積|general|
|---:|---:|---:|---:|---:|---:|
|1|1|1|1|1|1|
|2|1|1|1|1|2|
|10|1|1|1|5|10|
|30|1|1|3|15|30|
|30|4|4|4（例外表示）|15|30|

新規対象0件のstageは`NO_NEW_TARGETS`の記録を残し、人が次へ進められる。元のpilot観測を捨てて同じ24時間を再要求せず、skipを新device成功件数に数えない。N=0ではselfまでの受入のみ可能。顧客配信completedとは表示しない。

### 端末割当

一tenantに複数profile/複数Macがある場合、最初は各profileの明示canaryを一台ずつ、同tenant内で直列に割り当てる。そのprofileのcanaryがVERIFIED後60分の連続healthyを満たしてから、同profileの残りを進める。profileはruntime_mode/CPU/Python/依存fingerprint/必要service契約まで束縛し、OS名だけでまとめない。
`assignment_id`は一度発行し、retry/poll/再起動で作り直さない。unique `(rollout_id,tenant,installation,device_id,profile_sha256)`。同device別installation/realmへのコピーは同じ割当と認めない。
最初に割り当てた`stage`は変えず保持する。current campaign stageが進んでも過去assignment/operation/outcomeのstageを上書きしない。

### stage内のwave

stageの対象上限を一度に全て開始せず、固定順位で最大10customer tenantずつのwaveを作る。`max_wave_tenants=10`、`wave_soak_ms=3600000`が初期policy。wave内もglobal最大10device/per-tenant1deviceの制約を守る。
waveは`(rollout_id,stage,wave_index)`で固定し、Assignment/RolloutBindingへwave_indexを束縛する。前waveのgrant発行済み結果が全確定し、対象verifiedが60分の連続healthy、未START分は明示延期/除外で整理されるまで次waveを開かない。0成功waveは自動前進しない。stage昇格の24時間観測は別であり、wave60分を理由に短縮しない。
一tenantの最初のprofile canaryも60分観測してから残端末へ進む。これにより「同時に10台以下だが短時間で全台へ配った」という抜け穴を閉じる。運用上遅い場合は測定後に別承認policyを作るが、進行中の同policyを上書きしない。

## ROL04: state machine

`stage`と`status`は別の軸で保存する。

- stage: `self | pilot | pct10 | pct50 | general`
- status: `draft | active | awaiting_approval | paused | cancelling | cancelled | completed`
- assignment: `waiting | eligible | reserved | granted | verified | unknown | cancelled_no_effect | deferred_no_grant | excluded_before_grant`

正常遷移はdraft→self active→self awaiting_approval→pilot active→…→general active→completed。
`paused`はどのactive/awaiting_approvalでも可能。pause中は新reservation/grant/自動microbatch不可、結果/health/状態読取りは継続する。resumeは同stageでのみ行い、原因解消の証拠を必要とする。resumeとadvanceを同じ操作にしない。
`cancelling`は新grant停止、発行済み操作は終結を待つ。unresolvedが残る間はcancelledにせず、そのstreamへ後続rolloutを有効化しない。人が無根拠にunknownを除外するボタンは作らない。
completedは全割当の適用率100%を意味しない。明示的に未START延期/除外がある場合、UIは `completed_with_deferrals`という表示と件数を併記する（storageのstatus=completed、completion_summaryで区別）。未更新を成功として割合に入れない。

## ROL05: revisionの分離と署名binding

`RolloutBinding`をOffer.selected、StartRequest、StartGrant、Outcome、HealthObservationに必須追加する。

```text
rollout_id / assignment_id / policy_sha256 / cohort_sha256
allocation_epoch / authorization_epoch / stage / wave_index
```

- `allocation_epoch`はsnapshot/割当の世代。既存assignmentは固定。
- `authorization_epoch`はstage切替・pause/resume・安全policy変更で単調増加。新しいOffer/Startは現在値と一致する。既存の`rollout_epoch`fieldは同値を要求する。
- `revision`は管理mutationのCAS用。`health_revision`はhealth/結果集約のCAS用。正常healthの到着だけでauthorization_epochを進めてgrantを無効化しない。
- `policy_sha256`は署名済みの固定policy。自動更新を通すためにmutable設定を同じhashの別意味にしない。
- StartGrantはStartRequestとexact binding一致。private prepared evidenceにも同じbindingを保存する。
- 結果受領は**発行時のgrantに保存したbinding**へ一致させる。現在のauthorization_epoch/stageへ合わせ直さない。これによりpauseや昇格後に遅れて到着した正規の結果を拒否しない。

Offer.noneではselected=null。未許可のhigher releaseをmetadata maximumとして端末へ観測させてhighest_seenを進めない。stage適格性を選択してから製品の署名metadataを返す。`fleet/releases/state.js::selectCurrent`はv1の用途を保ち、autoはexact release_idに束縛してattestationを取得する [C08]。auto有効後に旧background metadata pollが全stream最大値をv3 ownerへobserveする入口も禁止する。v1 statusの読取りは許せるが、割当外releaseをv3高水位へ採用する副作用はP0で全callerを塞ぐ。

## ROL06: healthの意味と昇格条件

旧R1 heartbeatは運用監視として残す。段階配信の判定は新しい`health`専用DTOを使い、患者件数・本文・任意log・LLM出力は送らない。

HealthObservationはdevice認証に加え、rollout binding、operation/grant/target、runtime generation、boot擬名、process setのopaque proof ID、health_sequence、観測時刻、statusと固定reasonを持つ。中央は発行済みgrantと受領済みverified outcomeへ照合する。runtimeが旧版、対象opが違う、未verifiedならhealthyとして蓄積しない。

**連続健康時間は、Release公開時刻でもclientの自己申告uptimeでもなく、中央が受領したfreshな観測の連続区間**で計算する。
- 初回: verifiedの受領後、fresh healthyが初めて受領されたDB時刻を`healthy_since`にする。
- 継続: 同target/op/generation/boot、sequenceが増加、観測鮮度<=600秒、受信gap<=900秒なら継続。
- replay: 同ID/同bytesはACKだけ。last_fresh_received_at/healthy_sinceを前進させない。同ID異bytesはintegrity hold。
- boot/generation変更、gap超過、degraded/unknownで連続区間をリセット。通常再起動でbootが変わった場合は、端末のtrusted loaderが同じverified targetの新しい起動proofを作り、そのprofileの世代規則で受け入れられる場合だけ新しい区間を始める。START時のboot bindingを再利用して新たなcode effectを許可してはならない。再起動が認められた正常起動でも、古い区間を新bootへ接続しない。
- 時刻が未来許容を超える/後退/不明ならfreshにしない。受信時刻にもDB秒粒度を考慮し、許容値を勝手に広げない。

stageを`awaiting_approval`にできる必要条件:
1. 必須profileごとのself/pilot coverageがある。
2. stageの新規対象と発行済み操作が全て確定。`signing_reserved/granted/unknown`がない。
3. 少なくとも一件の新規verified成功（新規対象0stageは明示skip）を持つ。0台onlineで待つだけでは進めない。
4. 対象の全verified端末が必要な連続時間を満たし、既更新群にも現在のhealth unknown/criticalがない。
5. actorが確認するsnapshotに、N/対象/verified/未START延期/失敗/unknownの分母と件数が明記される。
6. noHoldと現在のpublication/trust/authorityが成立する。停止されたreleaseを「待機時間経過」で再開しない。

**offlineの区別**: grant前の未接続は「未実行」であり失敗や成功にしない。assignment期限（初期7日）が過ぎても自動成功化しない。operatorは理由付き`exclude-unstarted`で当該stageから延期できるが、grant/sign reservation/START intentの疑いがあるdeviceは対象外。pilot必須coverageをこの操作で欠落させて昇格しない。
発行済みgrantの結果が所定の完了期限まで不明ならunknownとして自動PAUSEする。timeoutでslotを解放しない。

## ROL07: 自動停止

同release内で以下を受領・検出したら新reservation/grantを停止し、authorization_epochを増分する。
- outcome unknown、apply/runtime/storage integrity failure、異なるtarget/署名binding矛盾。
- started/grantedの結果不明、期待していたverified端末のhealth gap/unknownが許容を超える。
- 必須profileの安全条件崩壊、public artifactのhash不一致、operator pause。

単発のSTART前ネットワーク障害や保守窓待ちはdevice延期であり、それだけで全campaignを停止しない。`healthy`かどうかの推定はしない。外部LLMの一時障害はコード稼働受入と分離し、更新由来のcriticalとして自動断定しない。
停止は後続配信を止める操作であり、正常版の業務停止、既に始まった結果保存の妨害、Release削除、無承認rollbackではない。

## ROL08: operator mutationの二段階確認

1. `preview`はread-only。対象rollout、action、現在stage/status/revision/authorization_epoch/health_revision、policy/cohort hash、次の件数、阻害理由、必要な観測条件を返す。
2. `activate/advance/resume/pause/cancel/exclude-unstarted`はaction ID、preview hash、期待revisionとhealth_revision、対象stage、固定reason_codeを要求する。自由な患者情報入りreasonをwireへ流さない。
3. action ID+same body hashは同じ保存receiptを返す。別bytesは409。
4. **advanceは直前stageだけ**。self→generalの飛び越し、pausedのままadvance、既存unknownをskipするactionを拒否。
5. membership/CSRF/tokenexpiry/now、現rowrevision、health_revision、正確なeligible判定を**同じbatchのguard**で再チェック。preview後のlate errorが先にcommitすれば昇格は負ける。昇格が先にcommitした直後にerrorが来れば即PAUSEし、新grantは以後停止。すでに発行済みの有限grantの扱いはCEN07どおり。
6. 正常health到着でhealth_revisionだけ進んだ場合も、古いpreviewを無条件採用しない。再previewできる案内を返し、人確認待ち中にlockを保持しない。

createはtyped確認でdraftを作るだけで端末を開始しない。draftのactivate_selfにはpreviewを要求する（事前承認template自動selfは独立policy内の狭い例外）。pauseは迅速に行うためpreview不要の専用typed actionを許すが、権限/CSRF/期待rollout IDと明示理由、idempotencyは必須。危険なstage拡大はpreview必須。resumeはunknown消去ではない。

## ROL09: GitHub操作との関係

初期の運用入口はCSDC Consoleだけで完成可能にする。GitHub Environmentsは承認補助であり、tenant割合・Mac状態を管理しない。private CSDCでrequired reviewersの利用可否を確認せず必須にしない [GH17]。
オプション`promote-rollout.yml`は、**Consoleで既に承認され保存されたaction receipt ID/hash**を指定し、固定OIDCでその操作を配信系へ伝達するだけ。任意stage/割合をworkflow入力で上書きしない。現在epoch/対象hash/期限不一致は拒否し、GitHubのgreen表示で中央CASを回避しない。
GH workflowの追加はCSDC側のfile所有者が行い、GitHub environment等の実設定は別承認。新規PAT・有料プラン要求を段階配信の必須条件にしない。

## ROL10: capacity・cohort変更・新しい版

freeze後に増えたtenant/deviceは次campaignへ。緊急追加を同じcohort hashで混ぜない。非互換profileを通常更新へ格上げしない。更新済み端末を分母から消して成功率を上げない。
新しいreleaseが公開されても、旧operationのtargetは不変。旧campaignに未解決操作があれば、新releaseでその記録を隠さない。自動同意の外にある緊急保守の明示承認を別に扱う。
複数tenantの独立運用を必要以上に止めないため、new grantは割当済みかつ準備済みのdeviceへだけ作る。offline deviceのために予め30秒grantやtenant slotを消費しない。

END-STAGED-ROLLOUT-V3.0
