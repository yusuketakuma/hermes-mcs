"""固定診断DTOを検証し、新規イベントの鮮度と保存済みmetadata照合を分離します。"""
import hashlib
import json
import math
import re
from types import MappingProxyType

from .support_contract import canonical, MAX_SAFE_INTEGER

LIMITS = MappingProxyType({
    'max_body_bytes': 16384, 'events_per_batch': 64, 'count_max': 100000,
    'dropped_max': 1000000, 'bucket_ms': 600000, 'max_age_ms': 604800000,
    'max_future_ms': 300000, 'device_batches_day': 288, 'tenant_batches_day': 20000,
    'device_status_day': 1440, 'status_batch_ids': 16, 'case_artifacts': 8,
    'retention_ms': 604800000, 'receipt_retention_ms': 15552000000,
    'artifact_approval_ms': 1800000, 'consent_min_ms': 60000, 'consent_max_ms': 7776000000,
    'read_rows': 500, 'spool_events': 1000, 'spool_bytes': 1048576,
    'spool_age_ms': 604800000, 'max_retries': 3, 'observer_timeout_ms': 2000,
})
COMPONENTS = ('runtime', 'scheduler', 'notification', 'connectivity', 'update', 'storage', 'config', 'connector')
STAGES = ('startup', 'run', 'send', 'receive', 'apply', 'verify', 'shutdown', 'unknown')
CODES = ('TIMEOUT', 'UNAVAILABLE', 'REJECTED', 'INVALID_STATE', 'RESOURCE_LIMIT',
         'PERMISSION_DENIED', 'CONFLICT', 'INTEGRITY_FAILURE', 'CRASH', 'UNKNOWN')
SEVERITIES = ('info', 'warning', 'error', 'critical')
PLATFORM_COMPONENTS = ('ingest', 'console', 'support', 'diagnostics', 'retention')
PLATFORM_CODES = ('D1_UNAVAILABLE', 'ACCESS_UNAVAILABLE', 'KEY_UNAVAILABLE', 'SIGNING_UNAVAILABLE',
                  'LIMITS_UNAVAILABLE', 'CONFIG_UNAVAILABLE', 'UNEXPECTED')
DEFINITIONS = (
    'UUID', 'SHA256', 'Event', 'Batch', 'Receipt', 'StatusRequest', 'Status', 'Artifact',
    'ArtifactSubmit', 'ArtifactReceipt', 'ConsentGrantRequest', 'ConsentRevokeRequest',
    'DeviceReadRequest', 'EventDeleteRequest', 'ArtifactApprovalRequest', 'ArtifactReadRequest',
    'ConsentScope', 'Consent', 'PlatformFault', 'ConsentGrantResponse', 'ConsentRevokeResponse',
    'ConsentList', 'EventList', 'Deletion', 'EventDeleteResponse', 'DeletionReadRequest',
    'DeletionReadResponse', 'ArtifactApprovalResponse', 'ArtifactList', 'ErrorResponse',
    'ReceiptResponse', 'StatusResponse', 'ArtifactReceiptResponse',
)
_ERROR_CODES = (
    'INVALID_DIAGNOSTIC', 'INVALID_JSON', 'INVALID_FIELDS', 'BODY_TOO_LARGE', 'UNKNOWN_ROUTE',
    'UNAUTHENTICATED', 'DEVICE_DENIED', 'SCOPE_DENIED', 'ORIGIN_DENIED', 'CSRF_DENIED',
    'MEMBERSHIP_DENIED', 'ACCESS_DENIED', 'DIAGNOSTIC_CONSENT_REQUIRED', 'DIAGNOSTIC_APPROVAL_REQUIRED',
    'DIAGNOSTIC_CONFLICT', 'STATE_CONFLICT', 'REQUEST_LIMIT', 'LIMITS_UNAVAILABLE',
    'GLOBAL_LIMITS_UNAVAILABLE', 'KEY_UNAVAILABLE', 'CONFIG_UNAVAILABLE',
    'DIAGNOSTIC_GUARD_UNAVAILABLE', 'DIAGNOSTICS_UNAVAILABLE', 'STORAGE_UNAVAILABLE',
    'ACCESS_UNAVAILABLE', 'CSRF_UNAVAILABLE', 'UNSUPPORTED_MEDIA', 'UNSUPPORTED_ENCODING',
    'EMPTY_BODY', 'INVALID_UTF8',
)
_UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'
_EVENT = 'component stage code severity bucket_start count'
_CONTENT = ('stored', 'expired', 'deleted')
_CONSENT_REVISION = 'sdc-diagnostic-events-consent-v1'
_EXCLUDED = ('patient_data', 'secrets', 'paths', 'urls', 'exception_text', 'free_text', 'raw_logs', 'files', 'commands')
_APPROVAL_REQUEST = 'approval_id artifact_id case_id device_id case_epoch local_consent_epoch artifact_sha256 artifact_size'
_RECEIPT = 'schema_version batch_id state content_state consent_epoch event_count dropped_events partial accepted_at content_expires_at'
_ARTIFACT_RECEIPT = 'schema_version artifact_id approval_id case_id artifact_sha256 state content_state accepted_at content_expires_at'


class DiagnosticContractError(ValueError):
    """Only a fixed diagnostic error code is exposed."""
    def __init__(self, code='INVALID_CONTRACT'):
        if code not in ('INVALID_CONTRACT', 'INVALID_JSON', 'BODY_TOO_LARGE', 'STALE', 'BINDING_CONFLICT'):
            code = 'INVALID_CONTRACT'
        self.code = 'SDC_DIAGNOSTIC_' + code
        super().__init__(self.code)


def _check(ok, code='INVALID_CONTRACT'):
    if not ok:
        raise DiagnosticContractError(code)


def _int(value, minimum=0, maximum=MAX_SAFE_INTEGER):
    _check(type(value) is int and minimum <= value <= maximum)


def _fields(value, names):
    _check(type(value) is dict and set(value) == set(names.split()))


def _uuid(value):
    _check(type(value) is str and re.fullmatch(_UUID, value) is not None)


def _hex(value):
    _check(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None)


def _ids(value, names):
    for name in names.split():
        _uuid(value[name])


def _times(value, names, *, nullable=False):
    for name in names.split():
        if not nullable or value[name] is not None:
            _int(value[name])


def _version(value, suffix):
    _check(value['schema_version'] == 'sdc.diagnostics.' + suffix + '.v1')


def _list(value, maximum, minimum=0):
    _check(type(value) is list and minimum <= len(value) <= maximum)


def _snapshot(value):
    active, nodes = set(), 0

    def copy(item, depth=0):
        nonlocal nodes
        nodes += 1
        _check(depth <= 8 and nodes <= LIMITS['max_body_bytes'])
        kind = type(item)
        if kind in (str, int, bool) or item is None:
            canonical(item, max_bytes=LIMITS['max_body_bytes'])
            return item
        _check(kind in (list, dict) and id(item) not in active)
        _check(len(item) <= (1024 if kind is list else 64))
        active.add(id(item))
        try:
            if kind is list:
                return [copy(v, depth + 1) for v in item]
            _check(all(type(k) is str for k in item))
            return {k: copy(v, depth + 1) for k, v in item.items()}
        finally:
            active.remove(id(item))

    return copy(value)


def _parse(raw):
    _check(type(raw) in (bytes, str), 'INVALID_JSON')
    _check(0 < len(raw) <= LIMITS['max_body_bytes'], 'BODY_TOO_LARGE')
    encoded = raw.encode('utf-8') if type(raw) is str else raw
    _check(len(encoded) <= LIMITS['max_body_bytes'], 'BODY_TOO_LARGE')
    _check(not encoded.startswith(b'\xef\xbb\xbf'), 'INVALID_JSON')

    def pairs(items):
        result = {}
        for key, value in items:
            _check(key not in result, 'INVALID_JSON')
            result[key] = value
        return result

    def number(text):
        # JS Number.isSafeInteger semantics, including integer-valued exponents.
        # Conversion to bounded float precedes int, even for extreme tokens.
        value = float(text)
        _check(math.isfinite(value) and value.is_integer() and abs(value) <= MAX_SAFE_INTEGER
               and not (value == 0 and math.copysign(1, value) < 0), 'INVALID_JSON')
        return int(value)

    def constant(_):
        raise DiagnosticContractError('INVALID_JSON')

    return json.loads(encoded.decode('utf-8', errors='strict'), object_pairs_hook=pairs,
                      parse_int=number, parse_float=number, parse_constant=constant)


def _event(value, now, *, row=False):
    _fields(value, _EVENT + (' batch_id' if row else ''))
    if row:
        _uuid(value['batch_id'])
    for name, choices in (('component', COMPONENTS), ('stage', STAGES),
                          ('code', CODES), ('severity', SEVERITIES)):
        _check(value[name] in choices)
    _int(value['count'], 1, LIMITS['count_max'])
    _int(value['bucket_start'])
    _check(value['bucket_start'] % LIMITS['bucket_ms'] == 0)
    if now is not None:
        _check(now - LIMITS['max_age_ms'] <= value['bucket_start'] <= now + LIMITS['max_future_ms'], 'STALE')


def _events(values, now):
    _list(values, LIMITS['events_per_batch'], 1)
    seen = set()
    for value in values:
        _event(value, now)
        key = tuple(value[k] for k in _EVENT.split()[:-1])
        _check(key not in seen)
        seen.add(key)


def _tail(value):
    _int(value['dropped_events'], 0, LIMITS['dropped_max'])
    _check(type(value['partial']) is bool)


def _content(value, *, artifact=False):
    _check(value['content_state'] in _CONTENT)
    _times(value, 'accepted_at content_expires_at')
    elapsed = value['content_expires_at'] - value['accepted_at']
    _check(0 <= elapsed <= LIMITS['retention_ms'] if artifact else elapsed == LIMITS['retention_ms'])


def _batch_metadata(value):
    _uuid(value['batch_id'])
    _int(value['consent_epoch'], 1)
    _int(value['event_count'], 1, LIMITS['events_per_batch'])
    _tail(value)
    _content(value)


def _scope(value):
    _fields(value, 'consent_revision target fields components content_retention_days receipt_retention_days max_validity_days excluded')
    _check(value['consent_revision'] == _CONSENT_REVISION and value['target'] == 'device'
           and value['fields'] == (_EVENT + ' dropped_events partial').split()
           and value['components'] == list(COMPONENTS) and value['excluded'] == list(_EXCLUDED))
    for key, expected in (('content_retention_days', 7), ('receipt_retention_days', 180), ('max_validity_days', 90)):
        _int(value[key], expected, expected)


def _consent(value):
    _fields(value, 'consent_id device_id consent_epoch consent_revision granted_by not_before expires_at revoked_at active')
    _ids(value, 'consent_id device_id')
    _int(value['consent_epoch'], 1)
    _check(value['consent_revision'] == _CONSENT_REVISION)
    _hex(value['granted_by'])
    _times(value, 'not_before expires_at')
    _times(value, 'revoked_at', nullable=True)
    _check(0 < value['expires_at'] - value['not_before'] <= LIMITS['consent_max_ms']
           and type(value['active']) is bool and (not value['active'] or value['revoked_at'] is None))
    # active is an observation, not current authority; do not refresh it from now.


def _approval_fields(value):
    _ids(value, 'approval_id artifact_id case_id device_id')
    for key in ('case_epoch', 'local_consent_epoch'):
        _int(value[key], 1)
    _hex(value['artifact_sha256'])
    _int(value['artifact_size'], 2, LIMITS['max_body_bytes'])


def _artifact_join(container, artifact):
    _check(container['case_id'] == artifact['case_id'], 'BINDING_CONFLICT')
    digest = hashlib.sha256(canonical(artifact, max_bytes=LIMITS['max_body_bytes']).encode('ascii')).hexdigest()
    _check(container['artifact_sha256'] == digest, 'BINDING_CONFLICT')


def _definition(name, value, now):
    _check(type(name) is str and name in DEFINITIONS)
    if name == 'UUID':
        _uuid(value)
    elif name == 'SHA256':
        _hex(value)
    elif name == 'Event':
        _event(value, now)
    elif name in ('Batch', 'Artifact'):
        _fields(value, 'schema_version ' + ('batch_id consent_epoch' if name == 'Batch' else 'case_id')
                + ' events dropped_events partial')
        _version(value, 'events' if name == 'Batch' else 'case-artifact')
        _uuid(value['batch_id' if name == 'Batch' else 'case_id'])
        if name == 'Batch':
            _int(value['consent_epoch'], 1)
        _events(value['events'], now)
        _tail(value)
    elif name == 'ArtifactSubmit':
        _fields(value, 'schema_version artifact_id approval_id case_id case_epoch local_consent_epoch artifact_sha256 artifact')
        _version(value, 'case-artifact-submit')
        _ids(value, 'artifact_id approval_id case_id')
        _int(value['case_epoch'], 1)
        _int(value['local_consent_epoch'], 1)
        _hex(value['artifact_sha256'])
        _definition('Artifact', value['artifact'], now)
        _artifact_join(value, value['artifact'])
    elif name in ('Receipt', 'ArtifactReceipt'):
        artifact = name == 'ArtifactReceipt'
        _fields(value, _ARTIFACT_RECEIPT if artifact else _RECEIPT)
        _version(value, 'case-artifact-receipt' if artifact else 'receipt')
        _check(value['state'] == 'accepted')
        if artifact:
            _ids(value, 'artifact_id approval_id case_id')
            _hex(value['artifact_sha256'])
            _content(value, artifact=True)
        else:
            _batch_metadata(value)
    elif name in ('ReceiptResponse', 'StatusResponse', 'ArtifactReceiptResponse'):
        _check(type(value) is dict and 'request_id' in value)
        _uuid(value['request_id'])
        _definition(name.removesuffix('Response'), {k: v for k, v in value.items() if k != 'request_id'}, now)
    elif name == 'StatusRequest':
        _fields(value, 'schema_version batch_ids')
        _version(value, 'status-request')
        _list(value['batch_ids'], LIMITS['status_batch_ids'])
        for identifier in value['batch_ids']:
            _uuid(identifier)
        _check(len(set(value['batch_ids'])) == len(value['batch_ids']))
    elif name == 'Status':
        _fields(value, 'schema_version consent batches')
        _version(value, 'status')
        consent = value['consent']
        _fields(consent, 'state consent_epoch expires_at')
        if consent['state'] == 'active':
            _int(consent['consent_epoch'], 1)
            _int(consent['expires_at'])
        else:
            _check(consent['state'] == 'inactive' and consent['consent_epoch'] is None and consent['expires_at'] is None)
        _list(value['batches'], LIMITS['status_batch_ids'])
        for batch in value['batches']:
            _fields(batch, 'batch_id state content_state')
            _uuid(batch['batch_id'])
            _check(batch['state'] == 'accepted' and batch['content_state'] in _CONTENT
                   or batch['state'] == 'unknown' and batch['content_state'] is None)
    elif name in ('ConsentGrantRequest', 'ConsentRevokeRequest', 'DeviceReadRequest',
                   'EventDeleteRequest', 'ArtifactReadRequest', 'DeletionReadRequest'):
        names = {'ConsentGrantRequest': 'consent_id device_id expires_at',
                 'ConsentRevokeRequest': 'revocation_id consent_id', 'DeviceReadRequest': 'device_id',
                 'EventDeleteRequest': 'deletion_id device_id', 'ArtifactReadRequest': 'case_id device_id',
                 'DeletionReadRequest': 'deletion_id'}[name]
        _fields(value, names)
        _ids(value, names.replace(' expires_at', ''))
        if name == 'ConsentGrantRequest':
            _int(value['expires_at'])
            if now is not None:
                _check(now + LIMITS['consent_min_ms'] <= value['expires_at'] <= now + LIMITS['consent_max_ms'], 'STALE')
    elif name == 'ArtifactApprovalRequest':
        _fields(value, _APPROVAL_REQUEST)
        _approval_fields(value)
    elif name == 'ConsentScope':
        _scope(value)
    elif name == 'Consent':
        _consent(value)
    elif name in ('ConsentGrantResponse', 'ConsentRevokeResponse'):
        grant = name == 'ConsentGrantResponse'
        _fields(value, 'code consent' + (' scope' if grant else ''))
        _check(value['code'] == ('diagnostic_consent_granted' if grant else 'diagnostic_consent_revoked'))
        _consent(value['consent'])
        if grant:
            _scope(value['scope'])
    elif name == 'ConsentList':
        _fields(value, 'code device_id scope consents')
        _check(value['code'] == 'diagnostic_consents')
        _uuid(value['device_id'])
        _scope(value['scope'])
        _list(value['consents'], 20)
        for consent in value['consents']:
            _consent(consent)
            _check(consent['device_id'] == value['device_id'], 'BINDING_CONFLICT')
    elif name == 'EventList':
        _fields(value, 'code device_id events batches')
        _check(value['code'] == 'diagnostic_events')
        _uuid(value['device_id'])
        _list(value['events'], LIMITS['read_rows'])
        for event in value['events']:
            _event(event, None, row=True)
        _list(value['batches'], 100)
        for batch in value['batches']:
            _fields(batch, 'batch_id consent_epoch event_count dropped_events partial content_state accepted_at content_expires_at')
            _batch_metadata(batch)
        # The two capped queries are independent; do not invent a complete join.
    elif name == 'Deletion':
        _fields(value, 'deletion_id device_id deleted_by deleted_at expires_at')
        _ids(value, 'deletion_id device_id')
        _hex(value['deleted_by'])
        _times(value, 'deleted_at expires_at')
        _check(value['expires_at'] - value['deleted_at'] == LIMITS['receipt_retention_ms'])
    elif name in ('EventDeleteResponse', 'DeletionReadResponse'):
        _fields(value, 'code deletion')
        _check(value['code'] == ('diagnostic_events_deleted' if name == 'EventDeleteResponse' else 'diagnostic_deletion'))
        _definition('Deletion', value['deletion'], None)
    elif name == 'ArtifactApprovalResponse':
        _fields(value, 'code approval')
        _check(value['code'] == 'diagnostic_artifact_approved')
        approval = value['approval']
        _fields(approval, _APPROVAL_REQUEST + ' artifact_kind approved_by created_at expires_at consumed_at revoked_at')
        _approval_fields(approval)
        _check(approval['artifact_kind'] == 'sdc.diagnostics.case-artifact.v1')
        _hex(approval['approved_by'])
        _times(approval, 'created_at expires_at')
        _times(approval, 'consumed_at revoked_at', nullable=True)
        _check(0 < approval['expires_at'] - approval['created_at'] <= LIMITS['artifact_approval_ms'])
    elif name == 'ArtifactList':
        _fields(value, 'code case_id device_id artifacts')
        _check(value['code'] == 'diagnostic_case_artifacts')
        _ids(value, 'case_id device_id')
        _list(value['artifacts'], LIMITS['case_artifacts'])
        for receipt in value['artifacts']:
            _check(type(receipt) is dict and 'artifact' in receipt)
            _definition('ArtifactReceipt', {k: v for k, v in receipt.items() if k != 'artifact'}, None)
            _check(receipt['case_id'] == value['case_id'], 'BINDING_CONFLICT')
            if receipt['content_state'] == 'stored':
                _definition('Artifact', receipt['artifact'], None)
                _artifact_join(receipt, receipt['artifact'])
            else:
                _check(receipt['artifact'] is None)
    elif name == 'PlatformFault':
        _fields(value, 'schema_version component code bucket_start')
        _version(value, 'platform-fault')
        _check(value['component'] in PLATFORM_COMPONENTS and value['code'] in PLATFORM_CODES)
        _int(value['bucket_start'])
        _check(value['bucket_start'] % LIMITS['bucket_ms'] == 0)
    elif name == 'ErrorResponse':
        _fields(value, 'code request_id')
        _check(value['code'] in _ERROR_CODES)
        _uuid(value['request_id'])


def _validate(definition, value, now, *, raw=False):
    try:
        result = _snapshot(_parse(value) if raw else value)
        _definition(definition, result, now)
        return result
    except DiagnosticContractError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise DiagnosticContractError('INVALID_JSON' if raw else 'INVALID_CONTRACT') from None


def validate_diagnostic(definition, value, *, now_ms):
    """Validate a detached DTO, applying freshness to new events/consent requests.

    Receipts, status, consent and list responses remain observations, including
    expired/revoked metadata. No signature, authenticated scope, authority, CAS,
    customer contract expiry, delivery or permission is proved by this function.
    Value DTOs use schema collection limits; parse_* additionally limits the
    original body to 16KiB. A maximum admin list can exceed that transport cap.
    """
    _int(now_ms)
    return _validate(definition, value, now_ms)


def parse_diagnostic(definition, raw, *, now_ms):
    """Validate bounded strict UTF-8 JSON; the caller retains original replay bytes."""
    _int(now_ms)
    return _validate(definition, raw, now_ms, raw=True)


def validate_diagnostic_shape(definition, value):
    """Check stored-ID metadata shape/joins only; never authorize a new send.

    The caller must first match authenticated scope, stored ID and identical raw
    bytes, and separately check current authority. No IDs or time are refreshed.
    """
    return _validate(definition, value, None)


def parse_diagnostic_shape(definition, raw):
    """Parse a stored-ID comparison candidate without extending its freshness."""
    return _validate(definition, raw, None, raw=True)
