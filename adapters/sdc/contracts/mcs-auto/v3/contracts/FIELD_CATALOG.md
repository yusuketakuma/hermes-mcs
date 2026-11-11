# 共通field一覧 v3.0

構造の自動一覧。03/07/08章の意味・権限検証も必須。

## Sha256

```json
{
  "type": "string",
  "minLength": 1,
  "maxLength": 64,
  "pattern": "^[0-9a-f]{64}$"
}
```

## Commit

```json
{
  "type": "string",
  "minLength": 1,
  "maxLength": 40,
  "pattern": "^[0-9a-f]{40}$"
}
```

## Uuid

```json
{
  "type": "string",
  "minLength": 1,
  "maxLength": 36,
  "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
}
```

## Uint

```json
{
  "type": "integer",
  "minimum": 0,
  "maximum": 9007199254740991
}
```

## Positive

```json
{
  "type": "integer",
  "minimum": 1,
  "maximum": 9007199254740991
}
```

## Slug

```json
{
  "type": "string",
  "minLength": 1,
  "maxLength": 64,
  "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$"
}
```

## Token

```json
{
  "type": "string",
  "minLength": 1,
  "maxLength": 100,
  "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$"
}
```

## Tag

```json
{
  "type": "string",
  "minLength": 1,
  "maxLength": 64,
  "pattern": "^v(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$"
}
```

## RawJson

```json
{
  "type": "string",
  "minLength": 1,
  "maxLength": 16384
}
```

## UInt64Decimal

```json
{
  "type": "string",
  "minLength": 1,
  "maxLength": 20,
  "pattern": "^(0|[1-9][0-9]{0,19})$"
}
```

## TimeWindow

|field|構造|
|---|---|
|`starts_at`|`{"$ref":"#/$defs/Uint"}`|
|`ends_at`|`{"$ref":"#/$defs/Uint"}`|

## Scope

|field|構造|
|---|---|
|`product`|`{"const":"hermes-mcs"}`|
|`tenant`|`{"$ref":"#/$defs/Slug"}`|
|`installation`|`{"$ref":"#/$defs/Slug"}`|
|`device_id`|`{"$ref":"#/$defs/Uuid"}`|
|`realm_epoch`|`{"$ref":"#/$defs/Token"}`|
|`environment`|`{"$ref":"#/$defs/Slug"}`|
|`stream_id`|`{"$ref":"#/$defs/Token"}`|

## ProductScope

|field|構造|
|---|---|
|`product`|`{"const":"hermes-mcs"}`|
|`environment`|`{"$ref":"#/$defs/Slug"}`|
|`trust_domain`|`{"$ref":"#/$defs/Token"}`|
|`stream_id`|`{"$ref":"#/$defs/Token"}`|

## Profile

|field|構造|
|---|---|
|`profile_id`|`{"$ref":"#/$defs/Slug"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`runtime_mode`|`{"type":"string","enum":["hermes","standalone"]}`|
|`os`|`{"const":"macos"}`|
|`cpu`|`{"type":"string","enum":["arm64","x86_64"]}`|
|`python`|`{"type":"array","prefixItems":[{"const":3},{"type":"integer","minimum":9,"maximum":99}],"items":false,"minItems":2,"maxItems":2}`|
|`control_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|

## ReleaseRef

|field|構造|
|---|---|
|`release_id`|`{"$ref":"#/$defs/Sha256"}`|
|`code_seq`|`{"$ref":"#/$defs/Positive"}`|
|`source_commit`|`{"$ref":"#/$defs/Commit"}`|
|`artifact_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`artifact_size`|`{"type":"integer","minimum":1,"maximum":268435456}`|

## SourceRef

|field|構造|
|---|---|
|`repository_id`|`{"$ref":"#/$defs/Positive"}`|
|`repository_owner_id`|`{"$ref":"#/$defs/Positive"}`|
|`tag`|`{"$ref":"#/$defs/Tag"}`|
|`tag_oid`|`{"$ref":"#/$defs/Commit"}`|
|`commit`|`{"$ref":"#/$defs/Commit"}`|
|`main_snapshot_sha`|`{"$ref":"#/$defs/Commit"}`|

## JobRef

|field|構造|
|---|---|
|`workflow_id`|`{"$ref":"#/$defs/Positive"}`|
|`run_id`|`{"$ref":"#/$defs/Positive"}`|
|`run_attempt`|`{"$ref":"#/$defs/Positive"}`|
|`job_id`|`{"$ref":"#/$defs/Positive"}`|
|`artifact_id`|`{"anyOf":[{"$ref":"#/$defs/Positive"},{"type":"null"}]}`|

## LaneEvidence

|field|構造|
|---|---|
|`lane`|`{"type":"string","enum":["lint-test","hygiene","standalone-sdk","incident-gates","macos","hermes-integration"]}`|
|`source_commit`|`{"$ref":"#/$defs/Commit"}`|
|`environment_digest`|`{"$ref":"#/$defs/Sha256"}`|
|`suite_digest`|`{"$ref":"#/$defs/Sha256"}`|
|`evidence_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`measured_result`|`{"type":"string","enum":["success","failure","infra_unavailable"]}`|
|`coverage`|`{"type":"string","enum":["executed","reused","local_equivalent"]}`|
|`reused_from_commit`|`{"anyOf":[{"$ref":"#/$defs/Commit"},{"type":"null"}]}`|
|`impact_review_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`resolution_evidence_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`job_ref`|`{"anyOf":[{"$ref":"#/$defs/JobRef"},{"type":"null"}]}`|
|`unexecuted_reason`|`{"anyOf":[{"$ref":"#/$defs/Token"},{"type":"null"}]}`|

## ReleaseAcceptance

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.release-acceptance.v3"}`|
|`acceptance_id`|`{"$ref":"#/$defs/Uuid"}`|
|`source`|`{"$ref":"#/$defs/SourceRef"}`|
|`mode`|`{"type":"string","enum":["full_run","composed"]}`|
|`policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`lanes`|`{"type":"array","items":{"$ref":"#/$defs/LaneEvidence"},"minItems":6,"maxItems":6}`|
|`release_checks_digest`|`{"$ref":"#/$defs/Sha256"}`|
|`unresolved_product_failures`|`{"type":"array","items":{"$ref":"#/$defs/Token"},"minItems":0,"maxItems":0}`|
|`accepted`|`{"const":true}`|
|`accepted_at`|`{"$ref":"#/$defs/Uint"}`|
|`approved_actor`|`{"$ref":"#/$defs/Token"}`|

## BuildRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.build-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Sha256"}`|
|`source`|`{"$ref":"#/$defs/SourceRef"}`|
|`acceptance_id`|`{"$ref":"#/$defs/Uuid"}`|
|`acceptance_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`upstream_run_id`|`{"anyOf":[{"$ref":"#/$defs/Positive"},{"type":"null"}]}`|
|`upstream_run_attempt`|`{"anyOf":[{"$ref":"#/$defs/Positive"},{"type":"null"}]}`|
|`publisher_source_commit`|`{"$ref":"#/$defs/Commit"}`|
|`publisher_policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`created_at`|`{"$ref":"#/$defs/Uint"}`|

## AssetSpec

|field|構造|
|---|---|
|`name`|`{"type":"string","minLength":1,"maxLength":128,"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`size`|`{"type":"integer","minimum":1,"maximum":268435456}`|
|`media_type`|`{"type":"string","enum":["application/octet-stream","application/json","image/png","image/svg+xml","text/markdown"]}`|

## PublishPlan

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.publish-plan.v3"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request`|`{"$ref":"#/$defs/BuildRequest"}`|
|`release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`profile_sha256s`|`{"type":"array","items":{"$ref":"#/$defs/Sha256"},"minItems":1,"maxItems":4}`|
|`assets`|`{"type":"array","items":{"$ref":"#/$defs/AssetSpec"},"minItems":5,"maxItems":32}`|
|`asset_set_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`github_release_id`|`{"anyOf":[{"$ref":"#/$defs/Positive"},{"type":"null"}]}`|
|`revision`|`{"$ref":"#/$defs/Positive"}`|
|`fencing_token`|`{"$ref":"#/$defs/Positive"}`|
|`phase`|`{"type":"string","enum":["reserved","packaged","draft_created","assets_complete","published_unverified","registered","conflict"]}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## PublishedAsset

|field|構造|
|---|---|
|`asset_id`|`{"$ref":"#/$defs/Positive"}`|
|`name`|`{"type":"string","minLength":1,"maxLength":128,"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"}`|
|`size`|`{"$ref":"#/$defs/Positive"}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|

## PublicationRecord

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.publication-record.v3"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`source`|`{"$ref":"#/$defs/SourceRef"}`|
|`release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`github_release_id`|`{"$ref":"#/$defs/Positive"}`|
|`assets`|`{"type":"array","items":{"$ref":"#/$defs/PublishedAsset"},"minItems":5,"maxItems":32}`|
|`github_release_proof_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`acceptance_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`asset_set_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`immutable`|`{"const":true}`|
|`registered_at`|`{"$ref":"#/$defs/Uint"}`|
|`last_github_observed_at`|`{"$ref":"#/$defs/Uint"}`|
|`suspended`|`{"type":"boolean"}`|

## CompatibilityBase

|field|構造|
|---|---|
|`base_release_id`|`{"$ref":"#/$defs/Sha256"}`|
|`base_commit`|`{"$ref":"#/$defs/Commit"}`|
|`base_schema`|`{"$ref":"#/$defs/Positive"}`|
|`base_profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`config_contract_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`data_contract_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`control_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|

## Compatibility

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.compatibility.v3"}`|
|`scope`|`{"$ref":"#/$defs/ProductScope"}`|
|`target`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`profile`|`{"$ref":"#/$defs/Profile"}`|
|`bases`|`{"type":"array","items":{"$ref":"#/$defs/CompatibilityBase"},"minItems":1,"maxItems":4}`|
|`target_schema`|`{"$ref":"#/$defs/Positive"}`|
|`allowed_class`|`{"const":"code-only-compatible"}`|
|`config_contract_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`data_contract_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`classification_evidence_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`db_migration`|`{"const":false}`|
|`runtime_change`|`{"const":false}`|
|`configuration_change`|`{"const":false}`|
|`authorization_expansion`|`{"const":false}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|

## MaintenanceWindow

|field|構造|
|---|---|
|`weekday`|`{"type":"integer","minimum":0,"maximum":6}`|
|`start_minute`|`{"type":"integer","minimum":0,"maximum":1439}`|
|`end_minute`|`{"type":"integer","minimum":1,"maximum":1440}`|

## StandingConsent

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.standing-consent.v3"}`|
|`consent_id`|`{"$ref":"#/$defs/Uuid"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`profile`|`{"$ref":"#/$defs/Profile"}`|
|`allowed_class`|`{"const":"code-only-compatible"}`|
|`timezone`|`{"type":"string","minLength":1,"maxLength":64,"pattern":"^[A-Za-z0-9_+/-]+$"}`|
|`windows`|`{"type":"array","items":{"$ref":"#/$defs/MaintenanceWindow"},"minItems":1,"maxItems":32}`|
|`max_prepare_seconds`|`{"type":"integer","minimum":1,"maximum":120}`|
|`max_prepare_count_24h`|`{"type":"integer","minimum":1,"maximum":3}`|
|`max_prepare_seconds_24h`|`{"type":"integer","minimum":1,"maximum":360}`|
|`local_consent_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`policy_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`central_policy_id`|`{"$ref":"#/$defs/Uuid"}`|
|`central_policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`confirmation_receipt_id`|`{"$ref":"#/$defs/Uuid"}`|
|`confirmed_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|
|`enabled`|`{"type":"boolean"}`|
|`early_adopter`|`{"type":"boolean"}`|
|`rollout_policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|

## SelectionRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.selection-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`profile`|`{"$ref":"#/$defs/Profile"}`|
|`current_release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`verified_release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`consent_id`|`{"$ref":"#/$defs/Uuid"}`|
|`local_consent_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`policy_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`runtime_generation`|`{"$ref":"#/$defs/Positive"}`|

## SelectedRelease

|field|構造|
|---|---|
|`release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`manifest_digest`|`{"$ref":"#/$defs/Sha256"}`|
|`compatibility_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`publication_proof_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`acceptance_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`rollout`|`{"$ref":"#/$defs/RolloutBinding"}`|

## Offer

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.offer.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`rollout_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`status`|`{"type":"string","enum":["candidate","none"]}`|
|`reason`|`{"type":"string","enum":["ELIGIBLE","NO_NEW_RELEASE","COHORT_WAIT","MAINTENANCE_REQUIRED","POLICY_HOLD","PROFILE_UNQUALIFIED"]}`|
|`selected`|`{"anyOf":[{"$ref":"#/$defs/SelectedRelease"},{"type":"null"}]}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`not_before`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## MetadataRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.metadata-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`release_id`|`{"$ref":"#/$defs/Sha256"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`kind`|`{"type":"string","enum":["attestation","compatibility"]}`|

## ReadArtifactRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.read-artifact-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`release_id`|`{"$ref":"#/$defs/Sha256"}`|
|`artifact_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`artifact_size`|`{"type":"integer","minimum":1,"maximum":268435456}`|

## PreparedPublic

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.prepared-public.v3"}`|
|`local_preflight_id`|`{"$ref":"#/$defs/Uuid"}`|
|`local_backup_evidence_id`|`{"$ref":"#/$defs/Uuid"}`|
|`current_release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`target_release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`control_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`config_contract_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`data_contract_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`installed_schema`|`{"$ref":"#/$defs/Positive"}`|
|`prepare_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`execution_revision`|`{"$ref":"#/$defs/Positive"}`|
|`runtime_generation`|`{"$ref":"#/$defs/Positive"}`|
|`boot_id`|`{"$ref":"#/$defs/Sha256"}`|
|`resources_verified`|`{"const":true}`|
|`observed_at`|`{"$ref":"#/$defs/Uint"}`|
|`rollout`|`{"$ref":"#/$defs/RolloutBinding"}`|

## StartRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.start-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`consent_id`|`{"$ref":"#/$defs/Uuid"}`|
|`local_consent_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`policy_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`rollout_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`manifest_digest`|`{"$ref":"#/$defs/Sha256"}`|
|`compatibility_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`preflight`|`{"$ref":"#/$defs/PreparedPublic"}`|
|`preflight_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`attempt_key`|`{"$ref":"#/$defs/Sha256"}`|
|`window`|`{"$ref":"#/$defs/TimeWindow"}`|
|`rollout`|`{"$ref":"#/$defs/RolloutBinding"}`|

## StartGrant

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.start-grant.v3"}`|
|`grant_id`|`{"$ref":"#/$defs/Uuid"}`|
|`start_request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`target`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`consent_id`|`{"$ref":"#/$defs/Uuid"}`|
|`local_consent_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`policy_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`rollout_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`manifest_digest`|`{"$ref":"#/$defs/Sha256"}`|
|`compatibility_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`control_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`prepare_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`execution_revision`|`{"$ref":"#/$defs/Positive"}`|
|`runtime_generation`|`{"$ref":"#/$defs/Positive"}`|
|`boot_id`|`{"$ref":"#/$defs/Sha256"}`|
|`preflight_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`attempt_key`|`{"$ref":"#/$defs/Sha256"}`|
|`window`|`{"$ref":"#/$defs/TimeWindow"}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`not_before`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|
|`rollout`|`{"$ref":"#/$defs/RolloutBinding"}`|

## PrestartAbort

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.prestart-abort.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`start_request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`grant_id`|`{"anyOf":[{"$ref":"#/$defs/Uuid"},{"type":"null"}]}`|
|`prepare_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`execution_revision`|`{"$ref":"#/$defs/Positive"}`|
|`local_abort_receipt_id`|`{"$ref":"#/$defs/Uuid"}`|
|`reason`|`{"type":"string","enum":["EXPIRED_BEFORE_START","REVOKED_BEFORE_START","PREPARE_DEFERRED"]}`|
|`observed_at`|`{"$ref":"#/$defs/Uint"}`|

## Outcome

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.outcome.v3"}`|
|`report_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`report_sequence`|`{"$ref":"#/$defs/Positive"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`grant_id`|`{"$ref":"#/$defs/Uuid"}`|
|`attempt_key`|`{"$ref":"#/$defs/Sha256"}`|
|`target`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`installed`|`{"anyOf":[{"$ref":"#/$defs/ReleaseRef"},{"type":"null"}]}`|
|`verified_installed`|`{"anyOf":[{"$ref":"#/$defs/ReleaseRef"},{"type":"null"}]}`|
|`state`|`{"type":"string","enum":["verified","unknown","cancelled_no_effect"]}`|
|`readiness`|`{"type":"string","enum":["ready","unknown","not_started"]}`|
|`runtime_generation`|`{"$ref":"#/$defs/Positive"}`|
|`started_at`|`{"$ref":"#/$defs/Uint"}`|
|`observed_at`|`{"$ref":"#/$defs/Uint"}`|
|`verified_at`|`{"anyOf":[{"$ref":"#/$defs/Uint"},{"type":"null"}]}`|
|`local_proof_id`|`{"$ref":"#/$defs/Uuid"}`|
|`error_code`|`{"type":"string","enum":["NONE","RESULT_UNKNOWN","NO_EFFECT_CONFIRMED","SOURCE_HOLD","RUNTIME_HOLD","PERSISTENCE_UNKNOWN"]}`|
|`rollout`|`{"$ref":"#/$defs/RolloutBinding"}`|

## OutcomeAck

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.outcome-ack.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`report_id`|`{"$ref":"#/$defs/Uuid"}`|
|`outcome_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`state`|`{"const":"received"}`|
|`received_at`|`{"$ref":"#/$defs/Uint"}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## StatusRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.status-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`kind`|`{"type":"string","enum":["start","prestart_abort","result"]}`|
|`object_id`|`{"$ref":"#/$defs/Uuid"}`|

## FileIdentity

|field|構造|
|---|---|
|`device`|`{"$ref":"#/$defs/UInt64Decimal"}`|
|`inode`|`{"$ref":"#/$defs/UInt64Decimal"}`|
|`uid`|`{"$ref":"#/$defs/Uint"}`|
|`mode`|`{"$ref":"#/$defs/Uint"}`|
|`nlink`|`{"const":1}`|
|`size`|`{"$ref":"#/$defs/Uint"}`|
|`mtime_ns`|`{"$ref":"#/$defs/UInt64Decimal"}`|
|`ctime_ns`|`{"$ref":"#/$defs/UInt64Decimal"}`|

## OperationStatus

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.operation-status.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`kind`|`{"type":"string","enum":["start","prestart_abort","result"]}`|
|`object_id`|`{"$ref":"#/$defs/Uuid"}`|
|`object_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`state`|`{"type":"string","enum":["not_found","pending","granted","cancelled_prestart","received"]}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## PrivateFileWitness

|field|構造|
|---|---|
|`logical_id`|`{"$ref":"#/$defs/Token"}`|
|`identity`|`{"$ref":"#/$defs/FileIdentity"}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|

## RetiredProcess

|field|構造|
|---|---|
|`service_id`|`{"$ref":"#/$defs/Token"}`|
|`process_instance_id`|`{"$ref":"#/$defs/Uuid"}`|
|`generation`|`{"$ref":"#/$defs/Positive"}`|
|`boot_id`|`{"$ref":"#/$defs/Sha256"}`|
|`birth_witness`|`{"$ref":"#/$defs/Sha256"}`|
|`retired`|`{"const":true}`|

## PrivatePreparedWitness

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.private-prepared-witness.v3"}`|
|`public`|`{"$ref":"#/$defs/PreparedPublic"}`|
|`root_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`repository_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`lock_identities`|`{"type":"array","items":{"$ref":"#/$defs/PrivateFileWitness"},"minItems":8,"maxItems":8}`|
|`configuration_files`|`{"type":"array","items":{"$ref":"#/$defs/PrivateFileWitness"},"minItems":1,"maxItems":64}`|
|`source_tree_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`runtime_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`ledger_device`|`{"$ref":"#/$defs/UInt64Decimal"}`|
|`ledger_inode`|`{"$ref":"#/$defs/UInt64Decimal"}`|
|`stage_identity`|`{"$ref":"#/$defs/FileIdentity"}`|
|`stage_path`|`{"type":"string","minLength":1,"maxLength":4096}`|
|`backup_path`|`{"type":"string","minLength":1,"maxLength":4096}`|
|`backup_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`retired_processes`|`{"type":"array","items":{"$ref":"#/$defs/RetiredProcess"},"minItems":0,"maxItems":64}`|
|`required_free_bytes`|`{"$ref":"#/$defs/Uint"}`|
|`reserved_owner_bytes`|`{"type":"integer","minimum":1,"maximum":8388608}`|
|`prepared_until`|`{"$ref":"#/$defs/Uint"}`|

## StartIntent

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.start-intent.v3"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`grant_id`|`{"$ref":"#/$defs/Uuid"}`|
|`grant_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`attempt_key`|`{"$ref":"#/$defs/Sha256"}`|
|`prepare_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`root_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`source_before`|`{"$ref":"#/$defs/Commit"}`|
|`source_target`|`{"$ref":"#/$defs/Commit"}`|
|`created_at`|`{"$ref":"#/$defs/Uint"}`|
|`phase`|`{"type":"string","enum":["open","retired"]}`|
|`terminal_outcome_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|

## ServiceProof

|field|構造|
|---|---|
|`service_id`|`{"$ref":"#/$defs/Token"}`|
|`desired_kind`|`{"type":"string","enum":["resident","timer","disabled"]}`|
|`proof_id`|`{"$ref":"#/$defs/Uuid"}`|
|`process_instance_id`|`{"anyOf":[{"$ref":"#/$defs/Uuid"},{"type":"null"}]}`|
|`pid`|`{"anyOf":[{"$ref":"#/$defs/Positive"},{"type":"null"}]}`|
|`birth_witness`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`loaded_source_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`state`|`{"type":"string","enum":["running","armed","stopped"]}`|
|`observed_at`|`{"$ref":"#/$defs/Uint"}`|
|`previous_heartbeat_at`|`{"anyOf":[{"$ref":"#/$defs/Uint"},{"type":"null"}]}`|

## RuntimeProof

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.runtime-proof.v3"}`|
|`proof_id`|`{"$ref":"#/$defs/Uuid"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`control_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`runtime_generation`|`{"$ref":"#/$defs/Positive"}`|
|`boot_id`|`{"$ref":"#/$defs/Sha256"}`|
|`startup_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`services`|`{"type":"array","items":{"$ref":"#/$defs/ServiceProof"},"minItems":1,"maxItems":64}`|
|`activation_receipt_id`|`{"$ref":"#/$defs/Uuid"}`|
|`observed_at`|`{"$ref":"#/$defs/Uint"}`|

## OutboxEntry

|field|構造|
|---|---|
|`report_id`|`{"$ref":"#/$defs/Uuid"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`raw`|`{"$ref":"#/$defs/RawJson"}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`acknowledged`|`{"type":"boolean"}`|

## Binding

|field|構造|
|---|---|
|`code_seq`|`{"$ref":"#/$defs/Positive"}`|
|`release_id`|`{"$ref":"#/$defs/Sha256"}`|

## BudgetEntry

|field|構造|
|---|---|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`started_at`|`{"$ref":"#/$defs/Uint"}`|
|`ended_at`|`{"anyOf":[{"$ref":"#/$defs/Uint"},{"type":"null"}]}`|
|`reserved_ms`|`{"type":"integer","minimum":1,"maximum":120000}`|

## StoredEvidence

|field|構造|
|---|---|
|`logical_id`|`{"$ref":"#/$defs/Token"}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`size`|`{"type":"integer","minimum":1,"maximum":8388608}`|
|`object_key`|`{"$ref":"#/$defs/Sha256"}`|

## LockFingerprints

|field|構造|
|---|---|
|`G`|`{"$ref":"#/$defs/Sha256"}`|
|`U`|`{"$ref":"#/$defs/Sha256"}`|
|`Q`|`{"$ref":"#/$defs/Sha256"}`|
|`R`|`{"$ref":"#/$defs/Sha256"}`|
|`C`|`{"$ref":"#/$defs/Sha256"}`|
|`A`|`{"$ref":"#/$defs/Sha256"}`|
|`M`|`{"$ref":"#/$defs/Sha256"}`|
|`O`|`{"$ref":"#/$defs/Sha256"}`|

## SavedEvidence

|field|構造|
|---|---|
|`logical_id`|`{"$ref":"#/$defs/Token"}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`raw`|`{"$ref":"#/$defs/RawJson"}`|

## SpentRecord

|field|構造|
|---|---|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`grant_id`|`{"$ref":"#/$defs/Uuid"}`|
|`attempt_key`|`{"$ref":"#/$defs/Sha256"}`|
|`start_receipt_id`|`{"$ref":"#/$defs/Uuid"}`|
|`started_at`|`{"$ref":"#/$defs/Uint"}`|
|`grant_raw`|`{"$ref":"#/$defs/RawJson"}`|
|`grant_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`public_preflight`|`{"$ref":"#/$defs/PreparedPublic"}`|
|`manifest_evidence_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`consent_evidence_sha256`|`{"$ref":"#/$defs/Sha256"}`|

## ActiveOperation

|field|構造|
|---|---|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`phase`|`{"type":"string","enum":["STAGED","PREPARING","PREPARED","START_INTENT","STARTED","APPLIED","CANDIDATE_READY","ACTIVATING","UNKNOWN"]}`|
|`target`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`attempt_key`|`{"$ref":"#/$defs/Sha256"}`|
|`prepare_nonce`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`prepare_deadline`|`{"anyOf":[{"$ref":"#/$defs/Uint"},{"type":"null"}]}`|
|`private_witness`|`{"anyOf":[{"$ref":"#/$defs/PrivatePreparedWitness"},{"type":"null"}]}`|
|`grant_raw`|`{"anyOf":[{"$ref":"#/$defs/RawJson"},{"type":"null"}]}`|
|`start_receipt_id`|`{"anyOf":[{"$ref":"#/$defs/Uuid"},{"type":"null"}]}`|
|`candidate_generation`|`{"anyOf":[{"$ref":"#/$defs/Positive"},{"type":"null"}]}`|
|`startup_nonce`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`activation_receipt_id`|`{"anyOf":[{"$ref":"#/$defs/Uuid"},{"type":"null"}]}`|
|`runtime_proof`|`{"anyOf":[{"$ref":"#/$defs/RuntimeProof"},{"type":"null"}]}`|
|`reserved_owner_bytes`|`{"type":"integer","minimum":1,"maximum":8388608}`|
|`error_code`|`{"anyOf":[{"$ref":"#/$defs/Token"},{"type":"null"}]}`|
|`rollout`|`{"$ref":"#/$defs/RolloutBinding"}`|

## TerminalRecord

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.terminal-record.v3"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`state`|`{"type":"string","enum":["VERIFIED","DEFERRED","CANCELLED_NO_EFFECT"]}`|
|`attempt_key`|`{"$ref":"#/$defs/Sha256"}`|
|`outcome_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`proof_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`terminal_at`|`{"$ref":"#/$defs/Uint"}`|

## OwnerState

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.release-owner.v3"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`root_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`repository_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`activation_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`control_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`revision`|`{"$ref":"#/$defs/Positive"}`|
|`execution_revision`|`{"$ref":"#/$defs/Positive"}`|
|`trust_json`|`{"type":"string","minLength":1,"maxLength":8192}`|
|`legacy_evidence`|`{"type":"array","items":{"$ref":"#/$defs/StoredEvidence"},"minItems":0,"maxItems":128}`|
|`operation_evidence`|`{"type":"array","items":{"$ref":"#/$defs/StoredEvidence"},"minItems":0,"maxItems":10000}`|
|`lock_fingerprints`|`{"$ref":"#/$defs/LockFingerprints"}`|
|`installed_source_manifest_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`verified_source_manifest_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`profile`|`{"$ref":"#/$defs/Profile"}`|
|`consent`|`{"anyOf":[{"$ref":"#/$defs/StandingConsent"},{"type":"null"}]}`|
|`highest_seen`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`installed`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`highest_installed`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`verified_installed`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`runtime_generation`|`{"$ref":"#/$defs/Positive"}`|
|`bindings`|`{"type":"array","items":{"$ref":"#/$defs/Binding"},"minItems":1,"maxItems":100000}`|
|`active`|`{"anyOf":[{"$ref":"#/$defs/ActiveOperation"},{"type":"null"}]}`|
|`spent`|`{"type":"array","items":{"$ref":"#/$defs/SpentRecord"},"minItems":0,"maxItems":10000}`|
|`outcomes`|`{"type":"array","items":{"$ref":"#/$defs/TerminalRecord"},"minItems":0,"maxItems":10000}`|
|`outbox`|`{"type":"array","items":{"$ref":"#/$defs/OutboxEntry"},"minItems":0,"maxItems":10000}`|
|`prepare_budget`|`{"type":"array","items":{"$ref":"#/$defs/BudgetEntry"},"minItems":0,"maxItems":128}`|

## SignedEnvelope

|field|構造|
|---|---|
|`alg`|`{"const":"PS256"}`|
|`kid`|`{"$ref":"#/$defs/Sha256"}`|
|`purpose`|`{"type":"string","enum":["mcs-auto-offer-v3","mcs-auto-compatibility-v3","mcs-auto-grant-v3","mcs-auto-ack-v3","mcs-auto-status-v3","mcs-publication-plan-v3","mcs-release-acceptance-v3","mcs-auto-health-ack-v3","mcs-auto-consent-preview-v3","mcs-auto-consent-receipt-v3","mcs-rollout-policy-v3"]}`|
|`payload`|`{"type":"string","minLength":1,"maxLength":10923,"pattern":"^[A-Za-z0-9_-]+$"}`|
|`signature`|`{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z0-9_-]+$"}`|

## CompatibilityCatalog

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.compatibility-catalog.v3"}`|
|`entries`|`{"type":"array","items":{"type":"object","additionalProperties":false,"properties":{"profile_sha256":{"$ref":"#/$defs/Sha256"},"envelope":{"$ref":"#/$defs/SignedEnvelope"}},"required":["profile_sha256","envelope"]},"minItems":1,"maxItems":4}`|

## ControlFile

|field|構造|
|---|---|
|`path`|`{"type":"string","minLength":1,"maxLength":4096}`|
|`mode`|`{"type":"string","enum":["100644","100755"]}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|

## ControlManifest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.release-control-manifest.v3"}`|
|`owner_id`|`{"const":"mcs.updater"}`|
|`owner_revision`|`{"const":2}`|
|`entrypoints`|`{"type":"array","items":{"type":"string","minLength":1,"maxLength":4096},"minItems":1,"maxItems":16}`|
|`files`|`{"type":"array","items":{"$ref":"#/$defs/ControlFile"},"minItems":1,"maxItems":512}`|
|`dependency_policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`recovery_generation_sha256`|`{"$ref":"#/$defs/Sha256"}`|

## MigrationFile

|field|構造|
|---|---|
|`logical_id`|`{"$ref":"#/$defs/Token"}`|
|`before_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`size`|`{"$ref":"#/$defs/Uint"}`|
|`disposition`|`{"type":"string","enum":["preserve","archive_exact","translate_verified","must_be_absent"]}`|

## MigrationIntent

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.release-owner-migration.v3"}`|
|`migration_id`|`{"$ref":"#/$defs/Uuid"}`|
|`root_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`repository_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`source_schema`|`{"type":"string","enum":["none","mcs.release-owner.v1"]}`|
|`target_schema`|`{"const":"mcs.release-owner.v3"}`|
|`bridge_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`control_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`recovery_generation_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`external_floor_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`files`|`{"type":"array","items":{"$ref":"#/$defs/MigrationFile"},"minItems":1,"maxItems":128}`|
|`human_receipt_id`|`{"$ref":"#/$defs/Uuid"}`|
|`created_at`|`{"$ref":"#/$defs/Uint"}`|
|`phase`|`{"type":"string","enum":["prepared","old_archived","owner_replaced","verified"]}`|

## ApiError

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.release-error.v3"}`|
|`code`|`{"type":"string","enum":["NETWORK_UNAVAILABLE","RATE_LIMITED","SIGNATURE_INVALID","SCOPE_MISMATCH","CONTRACT_INVALID","PUBLICATION_UNVERIFIED","PUBLISH_PERMISSION_HOLD","COMPATIBILITY_HOLD","PROFILE_UNQUALIFIED","CONSENT_REQUIRED","CONSENT_REVOKED","OUTSIDE_WINDOW","PREPARE_BUSY","PREPARE_BUDGET_EXCEEDED","WITNESS_CHANGED","START_EXPIRED","ATTEMPT_SPENT","START_OUTCOME_UNKNOWN","CAPACITY_HOLD","STORAGE_UNKNOWN","APPLIED_UNVERIFIED","RUNTIME_HOLD","LEGACY_STATE_HOLD","RECOVERY_GENERATION_HOLD","ROLLOUT_PAUSED","ROLLOUT_CONFLICT","COHORT_UNSEALED","SHARED_DEVICE_BUSY","TENANT_UPDATE_BUSY","PROFILE_COVERAGE_HOLD","HEALTH_HOLD","OBJECT_INCOMPLETE","RESOURCE_LIMIT_HOLD"]}`|
|`retry_after_ms`|`{"type":"integer","minimum":0,"maximum":86400000}`|

## PublicationReservation

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.publication-reservation.v3"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_id`|`{"$ref":"#/$defs/Sha256"}`|
|`code_seq`|`{"$ref":"#/$defs/Positive"}`|
|`revision`|`{"$ref":"#/$defs/Positive"}`|
|`fencing_token`|`{"$ref":"#/$defs/Positive"}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## PublicationFinalizeRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.publication-finalize-request.v3"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`expected_revision`|`{"$ref":"#/$defs/Positive"}`|
|`fencing_token`|`{"$ref":"#/$defs/Positive"}`|
|`release_core_json`|`{"type":"string","minLength":1,"maxLength":8192}`|
|`compatibility_payloads`|`{"type":"array","items":{"$ref":"#/$defs/Compatibility"},"minItems":1,"maxItems":4}`|
|`candidate_assets`|`{"type":"array","items":{"$ref":"#/$defs/AssetSpec"},"minItems":1,"maxItems":28}`|

## AssetVerification

|field|構造|
|---|---|
|`asset_id`|`{"$ref":"#/$defs/Positive"}`|
|`name`|`{"type":"string","minLength":1,"maxLength":128}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`size`|`{"$ref":"#/$defs/Positive"}`|
|`verified`|`{"const":true}`|

## GithubVerification

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.github-verification.v3"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`github_release_id`|`{"$ref":"#/$defs/Positive"}`|
|`source`|`{"$ref":"#/$defs/SourceRef"}`|
|`publisher_run_id`|`{"$ref":"#/$defs/Positive"}`|
|`publisher_run_attempt`|`{"$ref":"#/$defs/Positive"}`|
|`verifier_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`toolchain_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`release_attestation_evidence_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`release_verified`|`{"const":true}`|
|`immutable`|`{"const":true}`|
|`assets`|`{"type":"array","items":{"$ref":"#/$defs/AssetVerification"},"minItems":5,"maxItems":32}`|
|`observed_at`|`{"$ref":"#/$defs/Uint"}`|

## PublicationRegisterRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.publication-register-request.v3"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`expected_revision`|`{"$ref":"#/$defs/Positive"}`|
|`fencing_token`|`{"$ref":"#/$defs/Positive"}`|
|`verification`|`{"$ref":"#/$defs/GithubVerification"}`|

## RolloutStage

```json
{
  "type": "string",
  "enum": [
    "self",
    "pilot",
    "pct10",
    "pct50",
    "general"
  ]
}
```

## RolloutStatus

```json
{
  "type": "string",
  "enum": [
    "draft",
    "active",
    "awaiting_approval",
    "paused",
    "cancelling",
    "cancelled",
    "completed"
  ]
}
```

## RolloutBinding

|field|構造|
|---|---|
|`rollout_id`|`{"$ref":"#/$defs/Uuid"}`|
|`assignment_id`|`{"$ref":"#/$defs/Uuid"}`|
|`policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`cohort_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`allocation_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`authorization_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`stage`|`{"$ref":"#/$defs/RolloutStage"}`|
|`wave_index`|`{"$ref":"#/$defs/Positive"}`|

## StageRule

|field|構造|
|---|---|
|`stage`|`{"$ref":"#/$defs/RolloutStage"}`|
|`cumulative_basis_points`|`{"type":"integer","minimum":0,"maximum":10000}`|
|`soak_ms`|`{"type":"integer","minimum":1,"maximum":604800000}`|
|`min_new_verified`|`{"type":"integer","minimum":0,"maximum":4096}`|
|`requires_manual_advance`|`{"type":"boolean"}`|

## RolloutPolicy

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.rollout-policy.v3"}`|
|`policy_id`|`{"$ref":"#/$defs/Uuid"}`|
|`policy_revision`|`{"$ref":"#/$defs/Positive"}`|
|`product_scope`|`{"$ref":"#/$defs/ProductScope"}`|
|`promotion_mode`|`{"const":"manual"}`|
|`start_self_on_registration`|`{"type":"boolean"}`|
|`stages`|`{"type":"array","items":{"$ref":"#/$defs/StageRule"},"minItems":5,"maxItems":5}`|
|`max_parallel_devices`|`{"type":"integer","minimum":1,"maximum":10}`|
|`max_parallel_per_tenant`|`{"const":1}`|
|`health_interval_ms`|`{"type":"integer","minimum":60000,"maximum":900000}`|
|`max_health_gap_ms`|`{"type":"integer","minimum":60000,"maximum":3600000}`|
|`max_health_observation_age_ms`|`{"type":"integer","minimum":1000,"maximum":900000}`|
|`assignment_wait_ms`|`{"type":"integer","minimum":60000,"maximum":2592000000}`|
|`max_tenants`|`{"type":"integer","minimum":1,"maximum":1000}`|
|`max_devices`|`{"type":"integer","minimum":1,"maximum":4096}`|
|`max_profiles`|`{"type":"integer","minimum":1,"maximum":8}`|
|`rank_key_id`|`{"$ref":"#/$defs/Token"}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|
|`max_wave_tenants`|`{"type":"integer","minimum":1,"maximum":10}`|
|`wave_soak_ms`|`{"type":"integer","minimum":3600000,"maximum":604800000}`|
|`tenant_canary_soak_ms`|`{"type":"integer","minimum":3600000,"maximum":604800000}`|

## CohortTenant

|field|構造|
|---|---|
|`tenant`|`{"$ref":"#/$defs/Slug"}`|
|`rank`|`{"$ref":"#/$defs/Positive"}`|
|`pilot`|`{"type":"boolean"}`|
|`rank_sha256`|`{"$ref":"#/$defs/Sha256"}`|

## CohortDevice

|field|構造|
|---|---|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`consent_id`|`{"$ref":"#/$defs/Uuid"}`|
|`consent_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`qualification_id`|`{"$ref":"#/$defs/Uuid"}`|
|`qualification_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`device_rank`|`{"$ref":"#/$defs/Positive"}`|
|`early_adopter`|`{"type":"boolean"}`|

## CohortSnapshot

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.cohort-snapshot.v3"}`|
|`cohort_id`|`{"$ref":"#/$defs/Uuid"}`|
|`rollout_id`|`{"$ref":"#/$defs/Uuid"}`|
|`product_scope`|`{"$ref":"#/$defs/ProductScope"}`|
|`policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`target`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`allocation_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`frozen_at`|`{"$ref":"#/$defs/Uint"}`|
|`ranking_key_id`|`{"$ref":"#/$defs/Token"}`|
|`profiles`|`{"type":"array","items":{"$ref":"#/$defs/Sha256"},"minItems":1,"maxItems":8}`|
|`customer_tenants`|`{"type":"array","items":{"$ref":"#/$defs/CohortTenant"},"minItems":0,"maxItems":1000}`|
|`devices`|`{"type":"array","items":{"$ref":"#/$defs/CohortDevice"},"minItems":1,"maxItems":4096}`|

## Assignment

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.rollout-assignment.v3"}`|
|`assignment_id`|`{"$ref":"#/$defs/Uuid"}`|
|`rollout_id`|`{"$ref":"#/$defs/Uuid"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`target`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`cohort_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`allocation_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`assigned_stage`|`{"$ref":"#/$defs/RolloutStage"}`|
|`assigned_at`|`{"$ref":"#/$defs/Uint"}`|
|`start_by`|`{"$ref":"#/$defs/Uint"}`|
|`wave_index`|`{"$ref":"#/$defs/Positive"}`|

## RolloutCounters

|field|構造|
|---|---|
|`customer_total`|`{"type":"integer","minimum":0,"maximum":4096}`|
|`customer_allowed`|`{"type":"integer","minimum":0,"maximum":4096}`|
|`device_assigned`|`{"type":"integer","minimum":0,"maximum":4096}`|
|`reserved`|`{"type":"integer","minimum":0,"maximum":4096}`|
|`granted`|`{"type":"integer","minimum":0,"maximum":4096}`|
|`verified`|`{"type":"integer","minimum":0,"maximum":4096}`|
|`healthy`|`{"type":"integer","minimum":0,"maximum":4096}`|
|`unknown`|`{"type":"integer","minimum":0,"maximum":4096}`|
|`deferred_no_grant`|`{"type":"integer","minimum":0,"maximum":4096}`|
|`excluded_before_grant`|`{"type":"integer","minimum":0,"maximum":4096}`|

## RolloutCreateRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.rollout-create-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`rollout_id`|`{"$ref":"#/$defs/Uuid"}`|
|`product_scope`|`{"$ref":"#/$defs/ProductScope"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`policy_id`|`{"$ref":"#/$defs/Uuid"}`|
|`policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`expected_policy_revision`|`{"$ref":"#/$defs/Positive"}`|
|`pilot_tenants`|`{"type":"array","items":{"$ref":"#/$defs/Slug"},"minItems":0,"maxItems":1000}`|

## RolloutPreviewRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.rollout-preview-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`rollout_id`|`{"$ref":"#/$defs/Uuid"}`|
|`action`|`{"type":"string","enum":["activate_self","advance","pause","resume","cancel","exclude_unstarted"]}`|
|`target_stage`|`{"anyOf":[{"$ref":"#/$defs/RolloutStage"},{"type":"null"}]}`|
|`assignment_ids`|`{"type":"array","items":{"$ref":"#/$defs/Uuid"},"minItems":0,"maxItems":128}`|

## RolloutPreview

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.rollout-preview.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`preview_id`|`{"$ref":"#/$defs/Uuid"}`|
|`rollout_id`|`{"$ref":"#/$defs/Uuid"}`|
|`action`|`{"type":"string","enum":["activate_self","advance","pause","resume","cancel","exclude_unstarted"]}`|
|`expected_revision`|`{"$ref":"#/$defs/Positive"}`|
|`health_revision`|`{"$ref":"#/$defs/Uint"}`|
|`authorization_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`current_stage`|`{"$ref":"#/$defs/RolloutStage"}`|
|`current_status`|`{"$ref":"#/$defs/RolloutStatus"}`|
|`target_stage`|`{"anyOf":[{"$ref":"#/$defs/RolloutStage"},{"type":"null"}]}`|
|`policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`cohort_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`counters`|`{"$ref":"#/$defs/RolloutCounters"}`|
|`assignment_ids`|`{"type":"array","items":{"$ref":"#/$defs/Uuid"},"minItems":0,"maxItems":128}`|
|`evaluation_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`blockers`|`{"type":"array","items":{"type":"string","enum":["UNSEALED_COHORT","PROFILE_COVERAGE","OUTCOME_UNKNOWN","HEALTH_GAP","SOAK_INCOMPLETE","ACTIVE_GRANTS","NO_NEW_SUCCESS","PUBLICATION_HOLD","CONSENT_HOLD","INVALID_TRANSITION"]},"minItems":0,"maxItems":16}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## RolloutActionRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.rollout-action-request.v3"}`|
|`action_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`rollout_id`|`{"$ref":"#/$defs/Uuid"}`|
|`action`|`{"type":"string","enum":["activate_self","advance","pause","resume","cancel","exclude_unstarted"]}`|
|`expected_revision`|`{"$ref":"#/$defs/Positive"}`|
|`expected_health_revision`|`{"$ref":"#/$defs/Uint"}`|
|`expected_authorization_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`preview_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`target_stage`|`{"anyOf":[{"$ref":"#/$defs/RolloutStage"},{"type":"null"}]}`|
|`assignment_ids`|`{"type":"array","items":{"$ref":"#/$defs/Uuid"},"minItems":0,"maxItems":128}`|
|`reason_code`|`{"type":"string","enum":["STAGE_ACCEPTED","OPERATOR_PAUSE","CAUSE_RESOLVED","ROLLOUT_CANCELLED","OFFLINE_BEFORE_GRANT","NO_NEW_TARGETS"]}`|

## RolloutAdminReceipt

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.rollout-admin-receipt.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`rollout_id`|`{"$ref":"#/$defs/Uuid"}`|
|`action_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`revision`|`{"$ref":"#/$defs/Positive"}`|
|`health_revision`|`{"$ref":"#/$defs/Uint"}`|
|`authorization_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`stage`|`{"$ref":"#/$defs/RolloutStage"}`|
|`status`|`{"$ref":"#/$defs/RolloutStatus"}`|
|`cohort_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`counters`|`{"$ref":"#/$defs/RolloutCounters"}`|
|`accepted_at`|`{"$ref":"#/$defs/Uint"}`|

## HealthObservation

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.health-observation.v3"}`|
|`report_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`health_sequence`|`{"$ref":"#/$defs/Positive"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`rollout`|`{"$ref":"#/$defs/RolloutBinding"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`grant_id`|`{"$ref":"#/$defs/Uuid"}`|
|`target`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`runtime_generation`|`{"$ref":"#/$defs/Positive"}`|
|`boot_id`|`{"$ref":"#/$defs/Sha256"}`|
|`local_runtime_proof_id`|`{"$ref":"#/$defs/Uuid"}`|
|`status`|`{"type":"string","enum":["healthy","degraded","unknown"]}`|
|`reason_code`|`{"type":"string","enum":["NONE","SERVICE_NOT_READY","SOURCE_CHANGED","GENERATION_CHANGED","LOCAL_OBSERVATION_UNAVAILABLE","DEPENDENCY_UNVERIFIED"]}`|
|`observed_at`|`{"$ref":"#/$defs/Uint"}`|

## HealthAck

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.health-ack.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`report_id`|`{"$ref":"#/$defs/Uuid"}`|
|`observation_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`received_at`|`{"$ref":"#/$defs/Uint"}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## ConsentPreviewRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.consent-preview-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`central_proposal_id`|`{"$ref":"#/$defs/Uuid"}`|

## ConsentPreview

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.consent-preview.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`preview_id`|`{"$ref":"#/$defs/Uuid"}`|
|`central_proposal_id`|`{"$ref":"#/$defs/Uuid"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`profile`|`{"$ref":"#/$defs/Profile"}`|
|`central_policy_id`|`{"$ref":"#/$defs/Uuid"}`|
|`central_policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`policy_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`local_consent_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`timezone`|`{"type":"string","minLength":1,"maxLength":64,"pattern":"^[A-Za-z0-9_+/-]+$"}`|
|`windows`|`{"type":"array","items":{"$ref":"#/$defs/MaintenanceWindow"},"minItems":1,"maxItems":32}`|
|`max_prepare_seconds`|`{"type":"integer","minimum":1,"maximum":120}`|
|`max_prepare_attempts_24h`|`{"type":"integer","minimum":1,"maximum":3}`|
|`max_prepare_seconds_24h`|`{"type":"integer","minimum":1,"maximum":360}`|
|`early_adopter`|`{"type":"boolean"}`|
|`allowed_class`|`{"const":"code-only-compatible"}`|
|`consent_expires_at`|`{"$ref":"#/$defs/Uint"}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|
|`rollout_policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|

## ConsentConfirmRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.consent-confirm-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`preview_id`|`{"$ref":"#/$defs/Uuid"}`|
|`preview_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`consent`|`{"$ref":"#/$defs/StandingConsent"}`|

## ConsentRevokeRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.consent-revoke-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`consent_id`|`{"$ref":"#/$defs/Uuid"}`|
|`previous_local_consent_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`new_local_consent_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`local_revocation_receipt_id`|`{"$ref":"#/$defs/Uuid"}`|
|`observed_at`|`{"$ref":"#/$defs/Uint"}`|

## ConsentReceipt

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.consent-receipt.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`consent_id`|`{"$ref":"#/$defs/Uuid"}`|
|`consent_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`local_consent_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`policy_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`status`|`{"type":"string","enum":["active","revoked"]}`|
|`accepted_at`|`{"$ref":"#/$defs/Uint"}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## ArtifactChunk

|field|構造|
|---|---|
|`index`|`{"type":"integer","minimum":0,"maximum":15}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`size`|`{"type":"integer","minimum":1,"maximum":16777216}`|

## ObjectManifestRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.object-manifest-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`expected_revision`|`{"$ref":"#/$defs/Positive"}`|
|`fencing_token`|`{"$ref":"#/$defs/Positive"}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`size`|`{"type":"integer","minimum":1,"maximum":268435456}`|
|`content_type`|`{"type":"string","enum":["application/octet-stream","application/json","image/png","text/markdown"]}`|
|`chunks`|`{"type":"array","items":{"$ref":"#/$defs/ArtifactChunk"},"minItems":1,"maxItems":16}`|

## ObjectVerifyRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.object-verify-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`expected_revision`|`{"$ref":"#/$defs/Positive"}`|
|`fencing_token`|`{"$ref":"#/$defs/Positive"}`|
|`manifest_sha256`|`{"$ref":"#/$defs/Sha256"}`|

## ObjectReceipt

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.object-receipt.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`manifest_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`size`|`{"type":"integer","minimum":1,"maximum":268435456}`|
|`state`|`{"type":"string","enum":["manifest_fixed","chunks_incomplete","ready"]}`|
|`accepted_at`|`{"$ref":"#/$defs/Uint"}`|

## PolicyPreviewRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.policy-preview-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`action`|`{"type":"string","enum":["approve","revoke"]}`|
|`policy`|`{"$ref":"#/$defs/RolloutPolicy"}`|
|`expected_policy_revision`|`{"$ref":"#/$defs/Uint"}`|

## PolicyPreview

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.policy-preview.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`preview_id`|`{"$ref":"#/$defs/Uuid"}`|
|`action`|`{"type":"string","enum":["approve","revoke"]}`|
|`policy`|`{"$ref":"#/$defs/RolloutPolicy"}`|
|`policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`expected_policy_revision`|`{"$ref":"#/$defs/Uint"}`|
|`actor_ref`|`{"$ref":"#/$defs/Sha256"}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## PolicyActionRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.policy-action-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`preview_id`|`{"$ref":"#/$defs/Uuid"}`|
|`preview_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`action`|`{"type":"string","enum":["approve","revoke"]}`|
|`policy_id`|`{"$ref":"#/$defs/Uuid"}`|
|`policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`expected_policy_revision`|`{"$ref":"#/$defs/Uint"}`|
|`reason_code`|`{"type":"string","enum":["POLICY_APPROVED","POLICY_REVOKED"]}`|

## PolicyReceipt

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.policy-receipt.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`policy_id`|`{"$ref":"#/$defs/Uuid"}`|
|`policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`policy_revision`|`{"$ref":"#/$defs/Positive"}`|
|`status`|`{"type":"string","enum":["active","revoked"]}`|
|`accepted_at`|`{"$ref":"#/$defs/Uint"}`|

## RolloutReadRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.rollout-read-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`rollout_id`|`{"anyOf":[{"$ref":"#/$defs/Uuid"},{"type":"null"}]}`|
|`cursor`|`{"anyOf":[{"type":"string","minLength":1,"maxLength":256},{"type":"null"}]}`|
|`limit`|`{"type":"integer","minimum":1,"maximum":100}`|

## RolloutView

|field|構造|
|---|---|
|`rollout_id`|`{"$ref":"#/$defs/Uuid"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`stage`|`{"$ref":"#/$defs/RolloutStage"}`|
|`status`|`{"$ref":"#/$defs/RolloutStatus"}`|
|`revision`|`{"$ref":"#/$defs/Positive"}`|
|`health_revision`|`{"$ref":"#/$defs/Uint"}`|
|`authorization_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`cohort_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`counters`|`{"$ref":"#/$defs/RolloutCounters"}`|
|`pause_reason`|`{"anyOf":[{"type":"string","enum":["OPERATOR_PAUSE","RESULT_UNKNOWN","HEALTH_GAP","INTEGRITY_HOLD","PROFILE_COVERAGE"]},{"type":"null"}]}`|

## RolloutReadResponse

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.rollout-read-response.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`items`|`{"type":"array","items":{"$ref":"#/$defs/RolloutView"},"minItems":0,"maxItems":100}`|
|`next_cursor`|`{"anyOf":[{"type":"string","minLength":1,"maxLength":256},{"type":"null"}]}`|

## EnrollmentProposalRequest

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.enrollment-proposal-request.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`proposal_id`|`{"$ref":"#/$defs/Uuid"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`rollout_policy_id`|`{"$ref":"#/$defs/Uuid"}`|
|`rollout_policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`expected_enrollment_revision`|`{"$ref":"#/$defs/Uint"}`|
|`timezone`|`{"type":"string","minLength":1,"maxLength":64,"pattern":"^[A-Za-z0-9_+/-]+$"}`|
|`windows`|`{"type":"array","items":{"$ref":"#/$defs/MaintenanceWindow"},"minItems":1,"maxItems":32}`|
|`max_prepare_seconds`|`{"type":"integer","minimum":1,"maximum":120}`|
|`max_prepare_attempts_24h`|`{"type":"integer","minimum":1,"maximum":3}`|
|`max_prepare_seconds_24h`|`{"type":"integer","minimum":1,"maximum":360}`|
|`early_adopter`|`{"type":"boolean"}`|
|`consent_expires_at`|`{"$ref":"#/$defs/Uint"}`|

## EnrollmentProposalReceipt

|field|構造|
|---|---|
|`schema_version`|`{"const":"mcs.enrollment-proposal-receipt.v3"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`proposal_id`|`{"$ref":"#/$defs/Uuid"}`|
|`proposal_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`enrollment_revision`|`{"$ref":"#/$defs/Positive"}`|
|`accepted_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

END-FIELD-CATALOG-V3.0
