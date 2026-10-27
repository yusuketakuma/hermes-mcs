"""Validate the additive current-registration proof; it never authorizes execution."""
from . import release_contract as release
from . import release_apply_contract as apply

PURPOSE = 'release-apply-registration-current'
REQUEST_VERSION = 'sdc.release.registration-request.v1'
PROOF_VERSION = 'sdc.release.registration-current.v1'
RESPONSE_VERSION = 'sdc.release.registration-response.v1'
PATH = '/v2/releases/apply/registration/current'
SCOPE = ('product', 'audience', 'tenant', 'installation', 'device_id', 'environment', 'claim_id', 'key_id', 'realm_epoch', 'policy_epoch', 'device_epoch')
EXPECTED = (*SCOPE, 'request_id', 'expected_registration_id')
TTL_MS = 30000


def check(ok):
    if not ok:
        raise ValueError('RELEASE_REGISTRATION_INVALID')


def validate_request(value):
    v = release.input_record(value, 8192, plain_integers=False)
    check(set(v) == {'schema_version', 'request_id', 'expected_registration_id'})
    check(v['schema_version'] == REQUEST_VERSION and release.uuid(v['request_id'])
          and (v['expected_registration_id'] is None or release.uuid(v['expected_registration_id'])))
    return v


def validate_proof(value, *, now_ms):
    v = release.input_record(value, 8192, plain_integers=False)
    check(set(v) == {'schema_version', *EXPECTED, 'authority_revision', 'authority_sha256', 'status', 'owner', 'registration', 'issued_at', 'not_before', 'expires_at'})
    check(v['schema_version'] == PROOF_VERSION and v['product'] == 'hermes-mcs' and v['tenant'] == 'self'
          and release.slug(v['installation']) and release.match(v['audience'], r'[A-Za-z0-9:_-]{1,100}')
          and release.match(v['environment'], r'[a-z0-9_-]{1,32}') and release.realm(v['realm_epoch']))
    check(all(release.uuid(v[k]) for k in ('device_id', 'claim_id', 'key_id', 'request_id'))
          and (v['expected_registration_id'] is None or release.uuid(v['expected_registration_id'])))
    check(all(release.positive(v[k]) for k in ('policy_epoch', 'device_epoch', 'authority_revision')) and release.hex64(v['authority_sha256']))
    check(all(release.integer(v[k]) for k in ('issued_at', 'not_before', 'expires_at')) and release.integer(now_ms)
          and v['issued_at'] == v['not_before'] < v['expires_at'] and v['expires_at'] - v['issued_at'] <= TTL_MS
          and v['not_before'] <= now_ms + 1000 and now_ms < v['expires_at'])
    check(v['status'] in ('registered', 'not_registered', 'revoked', 'expired', 'changed'))
    if v['status'] in ('not_registered', 'revoked'):
        check(v['owner'] is None and v['registration'] is None
              and (v['status'] == 'not_registered') == (v['expected_registration_id'] is None))
    else:
        apply.validate_apply_policy(dict(schema_version=apply.VERSIONS['policy'], policy_revision=v['authority_revision'], owners=[v['owner']], device_registrations=[v['registration']]))
        check(v['owner']['owner_evidence_sha256'] != apply.ZERO and v['registration']['device_evidence_sha256'] != apply.ZERO)
        check(all(v['registration'][k] == v[k] for k in SCOPE if k != 'audience'))
        current = v['registration']['not_before'] <= v['issued_at'] < v['registration']['expires_at']
        check(not current if v['status'] == 'expired' else current and v['expires_at'] <= v['registration']['expires_at'])
        if v['status'] == 'registered':
            check(v['expected_registration_id'] is None or v['expected_registration_id'] == v['registration']['registration_id'])
        if v['status'] == 'changed':
            check(v['expected_registration_id'] is not None and v['expected_registration_id'] != v['registration']['registration_id'])
    return v


def validate_response(value):
    v = release.input_record(value, 16384, plain_integers=False)
    check(set(v) == {'schema_version', 'proof', 'request_id'} and v['schema_version'] == RESPONSE_VERSION and release.uuid(v['request_id']))
    check(type(v['proof']) is dict)
    return v
