# CSDC D1の永続化設計 — 新しいtableとtransaction

これは追加migrationの設計。実行するSQL migrationを本番へ適用する承認ではない。P0で現行番号と既存型/indexを確認し、`fleet/migrations/<次番号>_mcs_auto_*.sql`へ実装する。既存table/過去migrationを破壊しない。profile/tenantの実値をfixtureで埋めない。

## SQL01: 共通の列規則

scope列: `product,trust_domain,environment,stream_id`、端末列: `tenant_id,installation_id,device_id,realm_epoch`。tenantごとの行はtenantを必ずPK/unique/whereへ含める。device_idだけのglobal照会でbody tenantを信用しない。
UTC msはINTEGER。SQLiteが64bitを扱えてもwireはJS safe integer範囲。JSONは検証済みASCII CJSONのTEXT、raw署名はexact bytes相当を保存。JSON内のauth列と正規化列が同じことをwrite前・read採用前に検査する。`CHECK`でenum/boolean非曖昧化、FK/unique/indexを付ける。任意SQL識別子をHTTPから受けない。

## SQL02: table一覧

|table|key/unique|必須の正規化列・保存物|
|---|---|---|
|mcs_auto_streams|PK(product,trust_domain,environment,stream_id)|next_code_seq、highest_reserved、revision。既存release_bindingsより低い値で初期化しない|
|mcs_auto_publications|PK publication_id; UQ(repo_id,tag); UQ(request_id); UQ(stream scope,code_seq)|tag_oid/source_commit/publisher policy+identity/acceptance hash、phase、revision/fencing、core/target digest、asset_set hash、GitHub Release ID、lease。予約の正本|
|mcs_auto_publication_records|PK publication_id; UQ(repo_id,github_release_id)|確定PublicationRecord raw/hash、登録時刻、current suspend epoch。元rawは不変、suspend別列|
|mcs_auto_objects|PK(publication_id,object_sha256)|manifest raw/hash/size、status fixed/incomplete/ready、revision、検証証拠hash、保持分類。READY後のbytes変更不可|
|mcs_auto_chunks|PK(publication_id,object_sha256,chunk_index)|chunk SHA256/size/fixed R2 key/upload確認。index0〜15、status。受領前のmanifestと同一|
|mcs_auto_policy_versions|PK(policy_id,policy_revision); UQ(policy_sha256)|RolloutPolicy raw、承認actor_ref/receipt hash、有効期日、現在headへの参照。root keyやsecretは格納しない|
|mcs_auto_enrollments|PK(tenant_id,installation_id,device_id,realm_epoch)|consent ID/hash/raw、profile、local_consent_epoch/central policy epoch、state active/revoked/pending、資格ID/hash、pilot opt-in、expiry。旧receiptは別履歴で保持|
|mcs_auto_enrollment_proposals|PK(tenant_id,proposal_id); UQ(tenant_id,request_id)|個別proposal raw/hash、actor/scope/profile/rollout policy、expected enrollment revision、expiry。端末の同意を作らない|
|mcs_auto_enrollment_receipts|PK(tenant_id,device_id,request_id)|preview/confirm/revoke exact body hash、scope、同意receipt/hash、保存response。resetで再同意しない|
|mcs_rollouts|PK rollout_id; activeなstream scopeはUQ部分index|publication_id、target、policy hash、cohort hash/id、N/p、stage/status、revision/authorization_epoch/allocation_epoch/health_revision、freeze_at、reason、completed summary|
|mcs_rollout_tenants|PK(rollout_id,tenant_id); UQ(rollout_id,rank)|pilot bool、rank、rank_sha256、snapshot-only。freeze後のUPDATEは禁止|
|mcs_rollout_devices|PK(rollout_id,tenant_id,installation_id,device_id)|scope/profile/consent/qualification-at-freeze、device順位、初期適格性hash。freeze後のUPDATE禁止|
|mcs_rollout_waves|PK(rollout_id,stage,wave_index)|対象tenant/device集合のhash、forming/active/soaking/completed/paused、revision、観測条件。前wave確定まで次を開かない|
|mcs_rollout_assignments|PK assignment_id; UQ(rollout_id,tenant_id,installation_id,device_id,profile_sha256)|target、assigned_stage/wave_index、cohort/policy、allocation_epoch、assigned_at/start_by、status、current op。元bindingは不変|
|mcs_auto_operations|PK(tenant_id,device_id,operation_id)|scope、assignment/target、exact request hash、start request ID、grant ID、phase、slot_held、execution revision、prepare nonce、signed payload/envelope、issued/expiry、known result/facts、revision|
|mcs_auto_start_requests|PK(tenant_id,device_id,request_id); UQ(tenant_id,device_id,operation_id)|exact raw/hash、元grant raw、response hash、auth/realm/receiptの保持参照。論理retryはこの保存物|
|mcs_auto_attempts|PK(tenant_id,installation_id,device_id,attempt_key)|source target/profile/data/config contracts、未START予約/消費/unknown/終結、元op。SPENT後の再利用不可|
|mcs_auto_tenant_slots|PK(tenant_id,environment)|holder operation/device/assignment、取得時刻。未解決はexpiresだけで消さない|
|mcs_auto_outcomes|PK(tenant_id,device_id,report_id); UQ(tenant_id,device_id,operation_id,report_sequence)|exact raw/hash、発行grant binding、中央受領時刻、adopted state、fresh ACK元証拠|
|mcs_auto_abort_receipts|PK(tenant_id,device_id,request_id)|元op/grant/prepare nonce、private receipt opaque ID、未START確認、same raw/hash。期限切れだけで作らない|
|mcs_auto_health_heads|PK(tenant_id,device_id,operation_id)|latest accepted health_sequence/raw hash/boot/generation/observed/received、healthy_since、status、fresh gap、最新report receipt。bounded状態|
|mcs_rollout_health_evidence|PK(rollout_id,stage,evidence_id)|昇格で使用した集約digest、coverage/観測区間、件数とhealth_revision。患者情報なし|
|mcs_rollout_previews|PK preview_id|rollout/action/actor_ref、exact payload/hash、expected revision/health_revision、expiry。承認待ちでDBlockなし|
|mcs_rollout_actions|PK action_id|actor/current auth binding、exact input hash、元preview、before/after epoch、結果receipt、固定reason、DB時刻。自動PAUSEも固定system actorで記録|

`mcs_auto_operations`へ `(tenant_id,device_id) WHERE slot_held=1`の部分unique index、`mcs_auto_tenant_slots`の一意性、全active operation数のguardを設ける。既存familyは新tableを含む同一commitの不在検査を必須にする。各旧family自身のunique/未解決制限も維持する。

PKにrealmを含まない既存規約のtableでは、全書込みとread採用のguardに元realm一致を要求し、再登録時に旧realmのrowを消さない。必要ならcomposite keyへrealmを追加するが、単に新realmで旧未解決枠を回避しない。元realm未解決は外部floor再導入審査へ。

## SQL03: critical transactionの具体形

### start reservation

```text
input = trusted parsed StartRequest + independently authenticated device
prior = durable request lookup by (tenant,device,request_id)
if prior:
    require exact body hash/scope/operation; return stored observation/replay path
candidate = current policy/trust/enrollment/cohort/assignment/private-public witness
batch:
    INSERT new request_commits nonce WHERE all current auth + candidate CAS
        + no old-family unresolved + no auto operation active
        + tenant slot free + global active < effective cap
        + exact attempt allowed + current authorization_epoch
    INSERT mcs_auto_operations signing_reserved WHERE $COMMIT
    INSERT mcs_auto_start_requests WHERE $COMMIT
    INSERT/guarded update mcs_auto_attempts reservation WHERE $COMMIT
    INSERT mcs_auto_tenant_slots WHERE $COMMIT
    quota/nonce/audit writes WHERE $COMMIT
    SELECT commit result
if no commit row: conflict, not success
```

署名はこのbatch外。発行commitでは同reservationのrevision/payload/hash、現在auth/pause/epoch/slot/期限を再検証し、signed bytesを保存した後だけHTTPへ返す。

### result adoption

```text
lookup issuing operation/grant, not current campaign stage
verify exact binding, historical signing proof + authenticated current reporter
dedupe report ID and sequence; immutable known facts cannot regress
batch:
    conditional commit (same op revision/current limited report auth/no conflicting receipt)
    INSERT outcome exact raw/hash WHERE $COMMIT
    UPDATE operation's reported facts/revision WHERE $COMMIT
    if verified or valid cancelled_no_effect:
        release exactly this tenant/device auto slot WHERE $COMMIT
        keep attempt SPENT, keep all unknown reports as history
    if unknown: retain slot; PAUSE campaign (epoch increment) WHERE $COMMIT
    increase health_revision / reset incompatible health interval WHERE $COMMIT
    SELECT receipt
```

中央がverifiedを採用しても端末local stateを自動で書き直さない。ACK未受領と実行失敗を区別する。healthのdegraded/gapによるPAUSEはknown operation resultをunknownへ書換えず、campaign safety状態で管理する。

### stage advance

```text
preview evidence digest and immutable action request are fixed
batch current guard:
    current operator/Access/CSRF/membership + active authority
    exact rollout revision + health_revision + authorization_epoch + current stage
    exact sealed cohort/policy/publication
    no unresolved issued operations, exact required coverage and current health/freshness
writes:
    durable action receipt + stage/authorization_epoch increment
    new assignments for permitted next cohort ONLY (or bounded allocation intent)
    explicit per-stage health acceptance evidence
```

大量assignmentはstage変更と一括で無制限INSERTしない。固定cohortの範囲を新stage許可として耐久化し、有界allocatorがmicrobatchで既承認集合だけを割り当てる。allocator停止・競合でもuniqueで重複しない。grantは割当と現在stage許可の両方を再確認する。

## SQL04: snapshot sealと保管

freezeは同じtransactionのINSERT…SELECTでcustomer/deviceの一時snapshotを固定する。rank/hash計算がtransaction外でも元snapshot行は不変で、sealed=trueになるまで配信不可。
cohort digestは`CohortSnapshot`のCJSONに対するHであり、stage・health等の可変状態を含めない。count/row hash/manifest hash/expected build revisionのCASでsealする。crash後は同じsnapshotから再計算する。現在のdevices tableを再readして追加/削除した別snapshotを同じIDとしてsealしない。

## SQL05: cleanupと長期容量

`request_commits`の既存cleanupへ新しいauthority/receiptを巻き込まない。不可逆なSPENTやbindingの自動削除は対象外。health_headsは最新値のCASでboundedに保ち、過去sequenceをfreshとして再採用しない。昇格証拠の参照/hashは保持する。
R2 stagingのorphanは中央が所有・未参照・未公開・未grant参照を証明した場合だけ別承認済みcleanup方針で処理する。未知の保存状態を消して再送しない。
大きな履歴のarchive/compactionは別保守。容量逼迫時は新しいSTART/公開予約を止めるが既知の結果受領の最小容量を先に予約する。

END-SQL-DESIGN-V3.0
