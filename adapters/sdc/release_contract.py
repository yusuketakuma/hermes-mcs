"""署名済みリリースの閉じた契約・hash・純粋な計画判定を検査する。"""
from __future__ import annotations

import base64
import hashlib
import json
import re

from adapters.sdc.support_contract import canonical, parse_canonical

EXPECTED_FIELDS = ('product', 'trust_domain', 'environment', 'stream_id')
MODES = ('select_below_seen', 'code_rollback')
CANARY_STAGES = ('self_test', 'consented_single', 'customer_batch')
VERSIONS = {key: 'sdc.release.' + name + '.v1' for key, name in (
    ('core', 'core'), ('attestation', 'attestation'), ('trust', 'trust'),
    ('recovery', 'recovery-checkpoint'), ('state', 'state'), ('plan', 'plan'))}
PREFIXES = {key: 'SDC-RELEASE-' + name + '-v1\n' for key, name in (
    ('trust', 'TRUST'), ('core', 'CORE'), ('manifest', 'MANIFEST'), ('plan', 'PLAN'),
    ('precondition', 'PRECONDITION'))}
POLICY_CEILINGS = dict(max_attestation_ttl_ms=86400000, plan_ttl_ms=1800000,
    preflight_ttl_ms=300000, max_artifact_bytes=268435456, max_unpacked_bytes=1073741824,
    max_archive_entries=50000, download_timeout_ms=300000, max_clock_skew_ms=1000)
MAX_SAFE_INTEGER = 2**53 - 1
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'
STATE_POINTS = ('highest_seen', 'installed', 'highest_installed')
PRECONDITIONS = ('current_commit', 'hand_edited', 'installed_lock_sha256', 'free_bytes',
    'platform', 'updater', 'installed_schema', 'backup_verified', 'backup_sha256', 'observed_at')


class ContractError(ValueError):
    """固定コードだけを公開する契約の拒否。"""
    def __init__(self, code='RELEASE_INVALID_CONTRACT'):
        self.code = code
        super().__init__(code)


def check(ok, code='RELEASE_INVALID_CONTRACT'):
    if not ok:
        raise ContractError(code)


def match(v, pattern):
    return type(v) is str and re.fullmatch(pattern, v) is not None


def hex64(v):
    return match(v, r'[0-9a-f]{64}')


def integer(v):
    return type(v) is int and 0 <= v <= MAX_SAFE_INTEGER


def positive(v):
    return integer(v) and v > 0


def fields(v, names):
    check(type(v) is dict and set(v) == set(names))


def token(v):
    return match(v, r'[A-Za-z0-9][A-Za-z0-9._:-]{0,63}')


def slug(v):
    return match(v, r'[a-z0-9][a-z0-9_-]{0,63}')


def realm(v):
    return match(v, r'[A-Za-z0-9][A-Za-z0-9:_-]{0,127}')


def uuid(v):
    return match(v, UUID)


def int_tuple(v, size):
    return type(v) is list and len(v) == size and all(integer(x) for x in v)


def input_record(value, limit=8192, *, plain_integers=True):
    try:
        if type(value) in (bytes, str):
            raw = value.decode('utf-8') if type(value) is bytes else value
            check(len(raw.encode('utf-8')) <= limit)

            def pairs(items):
                result = {}
                for k, v in items:
                    check(k not in result)
                    result[k] = v
                return result

            def number(text):
                check(text != '-0')
                return int(text)

            def fraction(text):
                check(not plain_integers)
                # Match JS binary64 rounding in unsigned wire JSON; signed payloads require exact CJSON.
                n = float(text)
                check(n.is_integer() and abs(n) <= MAX_SAFE_INTEGER and not (n == 0 and text.startswith('-')))
                return int(n)

            value = json.loads(raw, object_pairs_hook=pairs, parse_int=number,
                               parse_float=fraction, parse_constant=lambda _: check(False))
        return parse_canonical(canonical(value, max_bytes=limit), max_bytes=limit)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, UnicodeError):
        raise ContractError() from None


def _sorted(v, maximum, minimum=0, key=lambda x: x):
    check(type(v) is list and minimum <= len(v) <= maximum)
    try:
        check(all(key(a) < key(b) for a, b in zip(v, v[1:])))
    except (TypeError, KeyError, AttributeError):
        raise ContractError() from None


def _scope(v):
    check(v['product'] == 'hermes-mcs' and token(v['trust_domain'])
          and match(v['environment'], r'[a-z0-9_-]{1,32}') and token(v['stream_id']))


def _same_scope(a, b):
    return all(a[k] == b[k] for k in EXPECTED_FIELDS)


def _times(v, maximum):
    check(all(integer(v[k]) for k in ('issued_at', 'not_before', 'expires_at')))
    check(v['issued_at'] <= v['not_before'] < v['expires_at']
          and v['expires_at'] - v['issued_at'] <= maximum)


def _point(v):
    fields(v, ('code_seq', 'release_id'))
    check(positive(v['code_seq']) and hex64(v['release_id']))


def _point_conflict(p, seq, release_id):
    check(p is None or p['code_seq'] != seq or p['release_id'] == release_id, 'RELEASE_CONFLICT')


def _identity(kind, v):
    return hashlib.sha256((PREFIXES[kind] + canonical(v)).encode('ascii')).hexdigest()


def hash_release_trust(v):
    return _identity('trust', v)


def hash_release_core(v):
    return _identity('core', v)


def hash_release_manifest(v):
    return _identity('manifest', v)


def hash_release_plan(v):
    return _identity('plan', v)


def hash_release_preconditions(v):
    return _identity('precondition', v)


def validate_release_trust(value, *, now_ms=None, allow_expired=False):
    """Trustの形・時刻を検査し、鍵や署名の真正性は判定しない。"""
    check(type(allow_expired) is bool)
    v = input_record(value)
    fields(v, ('schema_version', *EXPECTED_FIELDS, 'root_version', 'previous_trust_digest',
        'root_keys', 'root_threshold', 'release_keys', 'revoked_release_kids', 'retired_root_kids',
        'metadata_epoch', 'issued_at', 'not_before', 'expires_at', 'policy', 'recovery'))
    check(v['schema_version'] == VERSIONS['trust'])
    _scope(v)
    check(positive(v['root_version']) and (v['previous_trust_digest'] is None if v['root_version'] == 1 else hex64(v['previous_trust_digest']))
          and type(v['root_threshold']) is int and v['root_threshold'] == 2 and positive(v['metadata_epoch']))
    _sorted(v['root_keys'], 3, 3, lambda k: k.get('kid'))
    _sorted(v['release_keys'], 3, 1, lambda k: k.get('kid'))
    for role in ('root_keys', 'release_keys'):
        for k in v[role]:
            fields(k, ('kid', 'spki') if role == 'root_keys' else ('kid', 'spki', 'not_before', 'expires_at'))
            check(hex64(k['kid']) and match(k['spki'], r'[A-Za-z0-9_-]+'))
            try:
                raw = base64.b64decode(k['spki'] + '=' * (-len(k['spki']) % 4), altchars=b'-_', validate=True)
                check(0 < len(raw) <= 2048 and base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii') == k['spki'])
            except ValueError:
                raise ContractError() from None
            if role == 'release_keys':
                check(integer(k['not_before']) and integer(k['expires_at']) and k['not_before'] < k['expires_at'])
    for name in ('revoked_release_kids', 'retired_root_kids'):
        _sorted(v[name], 16)
        check(all(hex64(k) for k in v[name]))
    active = v['root_keys'] + v['release_keys']
    kids = [k['kid'] for k in active] + v['revoked_release_kids'] + v['retired_root_kids']
    check(len(set(kids)) == len(kids) and len({k['spki'] for k in active}) == len(active))
    _times(v, 365 * 86400000)
    p = v['policy']
    fields(p, ('download_origin', *POLICY_CEILINGS))
    check(match(p['download_origin'], r'https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+') and len(p['download_origin']) <= 261)
    for k, maximum in POLICY_CEILINGS.items():
        check((integer(p[k]) if k == 'max_clock_skew_ms' else positive(p[k])) and p[k] <= maximum)
    r = v['recovery']
    if r is not None:
        fields(r, ('schema_version', 'previous_epoch', 'new_epoch', 'release_id', 'manifest_digest', 'reason', 'affected_release_kids', 'preserve_bindings', 'preserve_successful_frontier'))
        check(r['schema_version'] == VERSIONS['recovery'] and positive(r['previous_epoch']) and integer(r['new_epoch'])
              and r['new_epoch'] == r['previous_epoch'] + 1 == v['metadata_epoch'] and hex64(r['release_id']) and hex64(r['manifest_digest']))
        check(r['reason'] in ('metadata_forward_attack', 'release_key_compromise') and r['preserve_bindings'] is True and r['preserve_successful_frontier'] is True and v['root_version'] > 1)
        _sorted(r['affected_release_kids'], 3, 1)
        check(all(hex64(k) and k in v['revoked_release_kids'] and k not in [x['kid'] for x in v['release_keys']] for k in r['affected_release_kids']))
    if now_ms is not None:
        check(integer(now_ms) and v['not_before'] <= now_ms + p['max_clock_skew_ms'] and (allow_expired or now_ms < v['expires_at']))
    return v


def validate_release_attestation(value, *, trust, now_ms):
    """Coreのidentity・scope・epoch・期限を照合し、署名の検証は行わない。"""
    check(integer(now_ms))
    t, v = validate_release_trust(trust, now_ms=now_ms), input_record(value)
    p = t['policy']
    fields(v, ('schema_version', *EXPECTED_FIELDS, 'metadata_epoch', 'core', 'release_id', 'issued_at', 'not_before', 'expires_at'))
    check(v['schema_version'] == VERSIONS['attestation'] and _same_scope(v, t) and type(v['metadata_epoch']) is int and v['metadata_epoch'] == t['metadata_epoch'] and hex64(v['release_id']))
    _times(v, p['max_attestation_ttl_ms'])
    c = v['core']
    fields(c, ('schema_version', *EXPECTED_FIELDS, 'code_seq', 'commit', 'artifact', 'lock_sha256', 'platforms', 'minimum_updater', 'schema', 'apply', 'recovery'))
    check(c['schema_version'] == VERSIONS['core'] and _same_scope(c, v) and positive(c['code_seq']) and match(c['commit'], r'[0-9a-f]{40}') and hex64(c['lock_sha256']) and int_tuple(c['minimum_updater'], 3))
    a = c['artifact']
    fields(a, ('format', 'object_format', 'sha256', 'size'))
    check(a['format'] == 'git-bundle-v2' and a['object_format'] == 'sha1' and hex64(a['sha256']) and positive(a['size']) and a['size'] <= p['max_artifact_bytes'])
    _sorted(c['platforms'], 8, 1, canonical)
    for x in c['platforms']:
        fields(x, ('os', 'cpu', 'python'))
        check(slug(x['os']) and slug(x['cpu']) and int_tuple(x['python'], 2) and x['python'][0] == 3)
    s = c['schema']
    fields(s, ('accepts_installed', 'target', 'migration', 'rollback_requires_db_restore'))
    _sorted(s['accepts_installed'], 16, 1)
    check(all(positive(x) for x in s['accepts_installed']) and positive(s['target']) and s['migration'] in ('none', 'forward_only') and type(s['rollback_requires_db_restore']) is bool)
    check(s['migration'] == 'forward_only' or s['target'] in s['accepts_installed'])
    fields(c['apply'], ('quiesce_required', 'restart_required'))
    check(all(type(x) is bool for x in c['apply'].values()))
    fields(c['recovery'], ('code_rollback', 'db_recovery'))
    check(c['recovery']['code_rollback'] in ('previous_release', 'not_supported') and c['recovery']['db_recovery'] == 'separate_approval')
    check(v['release_id'] == hash_release_core(c) and v['not_before'] <= now_ms + p['max_clock_skew_ms'] and now_ms < v['expires_at'])
    return v


def validate_release_state(value):
    """T/R/H/I/F/Jの閉じた形と、同一sequenceのidentity不変性を検査する。"""
    v = input_record(value)
    fields(v, ('schema_version', *EXPECTED_FIELDS, 'tenant', 'installation', 'device_id', 'realm_epoch', 'trust_digest', 'root_version', 'metadata_epoch', *STATE_POINTS, 'unresolved'))
    check(v['schema_version'] == VERSIONS['state'])
    _scope(v)
    check(slug(v['tenant']) and slug(v['installation']) and uuid(v['device_id']) and realm(v['realm_epoch']) and hex64(v['trust_digest']) and positive(v['root_version']) and positive(v['metadata_epoch']))
    for k in STATE_POINTS:
        if v[k] is not None:
            _point(v[k])
    if v['unresolved'] is not None:
        j = v['unresolved']
        fields(j, ('operation_id', 'plan_sha256', 'phase'))
        check(uuid(j['operation_id']) and hex64(j['plan_sha256']) and j['phase'] in ('started', 'unknown'))
    if v['installed'] is not None:
        check(v['highest_installed'] is not None and v['highest_installed']['code_seq'] >= v['installed']['code_seq'])
    for a in STATE_POINTS:
        for b in STATE_POINTS:
            if a < b and v[a] is not None and v[b] is not None:
                _point_conflict(v[a], v[b]['code_seq'], v[b]['release_id'])
    return v


def observe_release(state, attestation, *, trust, existing_binding, now_ms):
    """認証後の観測を純粋に還元し、保存すべきbindingとHを返す。"""
    s = validate_release_state(state)
    t = validate_release_trust(trust, now_ms=now_ms)
    a = validate_release_attestation(attestation, trust=t, now_ms=now_ms)
    binding = None if existing_binding is None else input_record(existing_binding)
    check(_same_scope(s, a) and s['trust_digest'] == hash_release_trust(t) and s['root_version'] == t['root_version'] and s['metadata_epoch'] == t['metadata_epoch'])
    seq = a['core']['code_seq']
    if binding is not None:
        fields(binding, ('stream_id', 'code_seq', 'release_id'))
        check(binding['stream_id'] == a['stream_id'] and type(binding['code_seq']) is int and binding['code_seq'] == seq and hex64(binding['release_id']))
        check(binding['release_id'] == a['release_id'], 'RELEASE_CONFLICT')
    for k in STATE_POINTS:
        _point_conflict(s[k], seq, a['release_id'])
    raised = s['highest_seen'] is None or seq > s['highest_seen']['code_seq']
    if raised:
        s['highest_seen'] = dict(code_seq=seq, release_id=a['release_id'])
    return dict(state=s, binding=dict(stream_id=a['stream_id'], code_seq=seq, release_id=a['release_id']),
                binding_new=binding is None, highest_seen_raised=raised, manifest_digest=hash_release_manifest(a))


def derive_release_modes(target_seq, state):
    check(positive(target_seq))
    return [mode for mode, k in zip(MODES, ('highest_seen', 'highest_installed')) if state[k] is not None and target_seq < state[k]['code_seq']]


def validate_release_plan(value, *, trust, attestation, state, now_ms):
    """読取り専用計画の全束縛を照合し、更新の認可は行わない。"""
    check(integer(now_ms))
    t, s, v = validate_release_trust(trust, now_ms=now_ms), validate_release_state(state), input_record(value)
    a = validate_release_attestation(attestation, trust=t, now_ms=now_ms)
    p = t['policy']
    fields(v, ('schema_version', 'plan_id', *EXPECTED_FIELDS, 'tenant', 'installation', 'device_id', 'realm_epoch', 'trust_digest', 'root_version', 'metadata_epoch', *STATE_POINTS, 'release_id', 'manifest_digest', 'target_code_seq', 'preconditions', 'precondition_sha256', 'window', 'canary_stage', 'modes', 'issued_at', 'expires_at'))
    check(v['schema_version'] == VERSIONS['plan'] and uuid(v['plan_id']) and _same_scope(v, a) and _same_scope(s, a))
    for k in ('tenant', 'installation', 'device_id', 'realm_epoch', 'trust_digest', 'root_version', 'metadata_epoch', *STATE_POINTS):
        check(canonical(v[k]) == canonical(s[k]))
    check(v['trust_digest'] == hash_release_trust(t) and v['root_version'] == t['root_version'] and v['metadata_epoch'] == a['metadata_epoch'])
    check(v['release_id'] == a['release_id'] and v['manifest_digest'] == hash_release_manifest(a) and type(v['target_code_seq']) is int and v['target_code_seq'] == a['core']['code_seq'])
    for k in STATE_POINTS:
        _point_conflict(s[k], v['target_code_seq'], v['release_id'])
    c = v['preconditions']
    fields(c, PRECONDITIONS)
    check(match(c['current_commit'], r'[0-9a-f]{40}') and type(c['hand_edited']) is bool and hex64(c['installed_lock_sha256']) and integer(c['free_bytes']) and int_tuple(c['updater'], 3) and positive(c['installed_schema']) and type(c['backup_verified']) is bool and hex64(c['backup_sha256']) and integer(c['observed_at']))
    fields(c['platform'], ('os', 'cpu', 'python'))
    check(slug(c['platform']['os']) and slug(c['platform']['cpu']) and int_tuple(c['platform']['python'], 2))
    check(v['precondition_sha256'] == hash_release_preconditions(c))
    fields(v['window'], ('starts_at', 'ends_at'))
    check(integer(v['window']['starts_at']) and integer(v['window']['ends_at']) and v['window']['starts_at'] < v['window']['ends_at'])
    check(v['canary_stage'] in CANARY_STAGES and canonical(v['modes']) == canonical(derive_release_modes(v['target_code_seq'], s)))
    check(integer(v['issued_at']) and integer(v['expires_at']) and v['issued_at'] < v['expires_at'] and v['expires_at'] - v['issued_at'] <= p['plan_ttl_ms'] and c['observed_at'] <= v['issued_at'] and v['issued_at'] - c['observed_at'] <= p['preflight_ttl_ms'])
    check(v['expires_at'] <= a['expires_at'] and v['expires_at'] <= t['expires_at'] and v['issued_at'] <= now_ms + p['max_clock_skew_ms'])
    return v


def release_plan_blockers(plan, attestation, trust, state, *, now_ms):
    """検証済み計画の開始阻害だけを順に返し、実行権限は作らない。"""
    check(integer(now_ms))
    c, x, p = attestation['core'], plan['preconditions'], trust['policy']
    conditions = (
        ('BASELINE_MISSING', state['installed'] is None or state['highest_installed'] is None),
        ('TARGET_NOT_OBSERVED', state['highest_seen'] is None or state['highest_seen']['code_seq'] < plan['target_code_seq']),
        ('ALREADY_INSTALLED', state['installed'] is not None and state['installed']['release_id'] == plan['release_id']),
        ('OUTSIDE_WINDOW', not plan['window']['starts_at'] <= now_ms < plan['window']['ends_at']),
        ('PLAN_EXPIRED', now_ms >= plan['expires_at']), ('PREFLIGHT_STALE', now_ms - x['observed_at'] > p['preflight_ttl_ms']),
        ('HAND_EDITED', x['hand_edited']), ('BACKUP_UNVERIFIED', not x['backup_verified']),
        ('INSUFFICIENT_SPACE', x['free_bytes'] < c['artifact']['size'] + p['max_unpacked_bytes']),
        ('UNSUPPORTED_PLATFORM', not any(canonical(y) == canonical(x['platform']) for y in c['platforms'])),
        ('UPDATER_TOO_OLD', x['updater'] < c['minimum_updater']),
        ('SCHEMA_INCOMPATIBLE', x['installed_schema'] not in c['schema']['accepts_installed']),
        ('UNRESOLVED_OPERATION', state['unresolved'] is not None), ('EXCEPTION_APPROVAL_REQUIRED', bool(plan['modes'])))
    return [name for name, blocked in conditions if blocked]
