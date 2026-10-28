"""既存updater内でreleaseの子processと一回実行境界を所有する。"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import secrets
import select
import stat
import subprocess
import sys
import time
import threading
import uuid

CHILD_FDS_ENV = 'MCS_RELEASE_CHILD_FDS'



class ReleaseApplyError(ValueError):
    def __init__(self, code='HOLD'):
        self.code = 'RELEASE_APPLY_' + code
        super().__init__(self.code)


class _EffectCapability:
    __slots__ = ()


def _require_settled_install_options(runtime_home, repository, deadline):
    """Hold before owner approval can be spent when migration is pending or unknown."""
    import mcs_update
    expected = os.path.abspath(os.path.expanduser(str(runtime_home)))
    if (os.path.abspath(mcs_update.HOME) != expected
            or os.path.abspath(mcs_update.mcs_util.CONF_PATH) != os.path.join(expected, 'config.json')
            or os.path.abspath(mcs_update.DATA) != os.path.join(expected, 'data')
            or Path(mcs_update.__file__).resolve() != Path(repository).resolve() / 'mcs/ops/mcs_update.py'):
        raise ReleaseApplyError('SETTINGS_MIGRATION_UNVERIFIED')
    result = mcs_update._release_migration_probe(deadline)
    if result == 'required':
        raise ReleaseApplyError('SETTINGS_MIGRATION_REQUIRED')
    if result != 'none':
        raise ReleaseApplyError('SETTINGS_MIGRATION_UNVERIFIED')


def _tty_planning_confirmation(installation, *, deadline):
    """Read the controlling terminal directly; stdin and caller booleans cannot approve."""
    from adapters.sdc import release_contract
    if not release_contract.slug(installation):
        raise ReleaseApplyError('CONSENT_HOLD')
    return _tty_confirmation('端末 ' + installation + ' の更新計画を確認します。\n'
        '中央の対象版と承認状態を取得し、現在の実装・容量・既存backupを検査します。\n'
        '1: 計画の確認を許可する\n2: 中止する（既定）\n番号: ', deadline=deadline)


def _tty_confirmation(prompt, *, deadline):
    if (type(deadline) not in (int, float) or not math.isfinite(deadline) or time.monotonic() >= deadline):
        raise ReleaseApplyError('CONSENT_HOLD')
    descriptor = None
    try:
        descriptor = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        if not os.isatty(descriptor) or os.tcgetpgrp(descriptor) != os.getpgrp():
            raise ReleaseApplyError('CONSENT_HOLD')
        def line():
            data = bytearray()
            while len(data) <= 128:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([descriptor], [], [], left)[0]:
                    raise ReleaseApplyError('CONSENT_HOLD')
                part = os.read(descriptor, 1)
                if not part:
                    return None
                if part == b'\n':
                    return bytes(data)
                data.extend(part)
            raise ReleaseApplyError('CONSENT_HOLD')
        os.write(descriptor, prompt.encode())
        if line() != b'1':
            return None
        os.write(descriptor, '秘密や個人情報を含まない半角の理由（1〜128文字）: '.encode())
        reason = line()
        if (not reason or not reason.strip() or any(c < 32 or c > 126 for c in reason)
                or time.monotonic() >= deadline):
            raise ReleaseApplyError('CONSENT_HOLD')
        return hashlib.sha256(reason).hexdigest()
    except (OSError, ValueError):
        raise ReleaseApplyError('CONSENT_HOLD') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _planning_snapshot(control, monitor, *, runtime_home, repository, deadline):
    from adapters.sdc import release_apply_contract, release_state
    current = _load_release_currentness(control, monitor, runtime_home=runtime_home,
        now_ms=int(time.time() * 1000), deadline=deadline)
    with control.release_owner_lock():
        owner = release_state._read(control)
        repository_fingerprint = _repository_fingerprint(repository)
        if owner['state']['unresolved'] is not None or owner['repo_fingerprint'] != repository_fingerprint:
            raise ReleaseApplyError('CONSENT_HOLD')
        return dict(binding=current.binding, root_fingerprint=control.root_fingerprint(),
            repo_fingerprint=repository_fingerprint, owner_state_revision=owner['revision'],
            state_sha256=release_apply_contract.hash_apply_state(owner['state']),
            installation=current.scope['installation'])


def _validate_planning_consent(value):
    from adapters.sdc import release_contract as contract
    try:
        value = contract.input_record(value, 16384)
        contract.fields(value, ('schema_version', 'binding', 'root_fingerprint', 'repo_fingerprint',
            'owner_state_revision', 'state_sha256', 'installation', 'case_id', 'planning_consent_id',
            'confirmed_at', 'expires_at', 'reason_sha256'))
        contract.fields(value['binding'], ('bootstrap_sha256', 'monitor_binding', 'support_snapshot_sha256',
            'monitor_snapshot_sha256', 'credential_sha256', 'owner_evidence_sha256', 'device_epoch'))
        bound = value['binding']['monitor_binding']
        contract.fields(bound, ('root_fingerprint', 'endpoint_hash', 'claim_id', 'device_id', 'key_id',
            'tenant', 'installation', 'environment', 'realm_epoch'))
        if (value['schema_version'] != 'mcs.release-planning-consent.v1'
                or not all(contract.hex64(value[k]) for k in ('root_fingerprint', 'repo_fingerprint', 'state_sha256', 'reason_sha256'))
                or not contract.positive(value['owner_state_revision']) or not contract.slug(value['installation'])
                or not all(contract.uuid(value[k]) for k in ('case_id', 'planning_consent_id'))
                or not all(contract.integer(value[k]) for k in ('confirmed_at', 'expires_at'))
                or value['expires_at'] - value['confirmed_at'] != 300000
                or not all(contract.hex64(value['binding'][k]) for k in ('bootstrap_sha256',
                    'support_snapshot_sha256', 'monitor_snapshot_sha256', 'credential_sha256', 'owner_evidence_sha256'))
                or not contract.positive(value['binding']['device_epoch'])
                or not all(contract.hex64(bound[k]) for k in ('root_fingerprint', 'endpoint_hash'))
                or not all(contract.uuid(bound[k]) for k in ('claim_id', 'device_id', 'key_id'))
                or not all(contract.slug(bound[k]) for k in ('tenant', 'installation', 'environment'))
                or not contract.realm(bound['realm_epoch']) or bound['installation'] != value['installation']):
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError):
        raise ReleaseApplyError('CONSENT_HOLD') from None


def _issue_planning_consent(control, monitor, *, runtime_home, repository, case_id, deadline):
    """Issue a local planning receipt before any operation or change consent exists."""
    from adapters.sdc import release_contract, release_apply_contract, release_state
    if not release_contract.uuid(case_id) or control._release_entry_fd is not None:
        raise ReleaseApplyError('CONSENT_HOLD')
    with control.release_authority_lock(), monitor.lock():
        before = _planning_snapshot(control, monitor, runtime_home=runtime_home,
            repository=repository, deadline=deadline)
    reason = _tty_planning_confirmation(before['installation'], deadline=deadline)
    if reason is None:
        return None
    with control.release_authority_lock(), monitor.lock():
        after = _planning_snapshot(control, monitor, runtime_home=runtime_home,
            repository=repository, deadline=deadline)
        if before != after:
            raise ReleaseApplyError('CONSENT_HOLD')
        now = int(time.time() * 1000)
        record = _validate_planning_consent(dict(schema_version='mcs.release-planning-consent.v1', **after,
            case_id=case_id, planning_consent_id=str(uuid.uuid4()), confirmed_at=now,
            expires_at=now + 300000, reason_sha256=reason))
        with control.release_owner_lock():
            owner = release_state._read(control)
            if (owner['revision'] != after['owner_state_revision']
                    or release_apply_contract.hash_apply_state(owner['state']) != after['state_sha256']):
                raise ReleaseApplyError('CONSENT_HOLD')
            previous = control.read('release-planning.json')
            if previous is not None:
                previous = _validate_planning_consent(previous)
                if (previous['root_fingerprint'] != record['root_fingerprint']
                        or previous['repo_fingerprint'] != record['repo_fingerprint'] or previous['expires_at'] > now):
                    raise ReleaseApplyError('CONSENT_HOLD')
            try:
                if time.monotonic() >= deadline:
                    raise ReleaseApplyError('CONSENT_HOLD')
                control.write('release-planning.json', record)
                if control.read('release-planning.json') != record or time.monotonic() >= deadline:
                    raise ReleaseApplyError('CONSENT_HOLD')
            except BaseException:
                control._release_owner_uncertain = True
                raise
        return record


@dataclass(frozen=True, eq=False, repr=False)
class _StartedEffect:
    owner: object
    process_id: int
    store: object
    locks: tuple
    lock_identities: tuple
    runtime_home: str
    repository: str
    reference: dict
    spent: dict
    published_revision: int
    journal: dict
    configuration: dict
    start_deadline: float
    prequiesce_deadline: float
    monitor: object = None
    change_sha256: str | None = None
    start_not_before: int | None = None
    start_expires_at: int | None = None


# Constructors never publish authority. Only the verified live factory may populate these.
_EFFECT_MUTEX = threading.Lock()
_EFFECT_PROCESS_ID = os.getpid()
_EFFECT_CAPS = {}
_EFFECT_PARENTS = {}
_EFFECT_CLAIMED = set()
_EFFECT_RUNNING = set()
_EFFECT_CHILDREN = set()
_EFFECT_ARTIFACTS = {}
_EFFECT_IMPORTED = set()
_EFFECT_COMPLETED = {}
_LIVE_CONTINUATIONS = {}


@dataclass(frozen=True, repr=False)
class _ReleaseCurrentness:
    bootstrap: dict
    binding: dict
    scope: dict
    identity: dict
    secret: str


def _load_release_currentness(control, monitor, *, runtime_home, now_ms, deadline):
    """Read dedicated public bootstrap and actual identity; never issue consent or a capability."""
    from adapters.sdc import provision, release_contract, release_state
    from adapters.sdc.connector import Connector
    from adapters.sdc.protocol import endpoint
    from sdc_support_signatures import APPLY_PURPOSES, validate_apply_pins, validate_registration_pins
    try:
        if (control._release_authority_fd is None
                or control._release_authority_thread != threading.get_ident()
                or monitor._tick_fd is None or monitor._tick_thread != threading.get_ident()
                or control.root != _control_root(runtime_home)
                or not release_contract.integer(now_ms)
                or type(deadline) not in (int, float) or not math.isfinite(deadline)
                or time.monotonic() + 5 > deadline
                or getattr(control, '_release_owner_uncertain', False)):
            raise ReleaseApplyError('CURRENTNESS_HOLD')
        # The two independently bounded pin sets exceed the single-wire 16 KiB helper limit.
        raw_bootstrap = json.dumps(control.read('release-bootstrap.json'), ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')
        if len(raw_bootstrap) > 2 * 16384 + 4096:
            raise ReleaseApplyError('CURRENTNESS_HOLD')
        value = json.loads(raw_bootstrap)
        release_contract.fields(value, ('schema_version', 'control_root_fingerprint', 'monitor_binding',
            'support_snapshot_sha256', 'release_endpoint', 'operation_pins', 'registration_pins', 'initial_trust_sha256'))
        bound = value['monitor_binding']
        identity_fields = ('claim_id', 'device_id', 'key_id')
        profile_fields = ('tenant', 'installation', 'environment', 'realm_epoch')
        release_contract.fields(bound, ('root_fingerprint', 'endpoint_hash', *identity_fields, *profile_fields))
        if (value['schema_version'] != 'mcs.release-bootstrap.v1'
                or value['control_root_fingerprint'] != control.root_fingerprint()
                or not all(release_contract.hex64(v) for v in (
                    value['support_snapshot_sha256'], value['initial_trust_sha256'],
                    bound['root_fingerprint'], bound['endpoint_hash']))
                or not all(release_contract.uuid(bound[k]) for k in identity_fields)):
            raise ReleaseApplyError('CURRENTNESS_HOLD')
        enrollment = monitor.enrollment_binding()
        current_monitor = provision.profile_snapshot(monitor, profile='monitor', now_ms=now_ms)
        support = provision.profile_snapshot(monitor, profile='support', now_ms=now_ms)
        if (any(bound[k] != enrollment[k] for k in ('root_fingerprint', 'endpoint_hash'))
                or monitor._fingerprint(support) != value['support_snapshot_sha256']
                or any(bound[k] != current_monitor['bundle'][k] for k in profile_fields)
                or current_monitor['root_fingerprint'] != bound['root_fingerprint']
                or support['root_fingerprint'] != bound['root_fingerprint']):
            raise ReleaseApplyError('CURRENTNESS_HOLD')
        state = monitor.read('state.json')
        if (type(state) is not dict or state.get('state') != 'active' or state.get('enabled') is not True
                or any(state.get(k) for k in ('disconnected', 'central_suspended', 'enrollment_hold'))
                or 'rotation_id' in state or any(state.get(k) != bound[k] for k in identity_fields)
                or any(support['bundle'][k] != bound[k] for k in ('claim_id', 'device_id'))):
            raise ReleaseApplyError('CURRENTNESS_HOLD')
        monitor.guard_credential()
        credential = monitor.read('credential')
        if (type(credential) is not dict
                or any(k in credential for k in ('next_key', 'next_key_id', 'rotation_id'))
                or credential.get('key_id') != bound['key_id']):
            raise ReleaseApplyError('CURRENTNESS_HOLD')
        secret = Connector(monitor, None).secret(state, credential)
        if not release_contract.hex64(secret):
            raise ReleaseApplyError('CURRENTNESS_HOLD')
        release_endpoint = endpoint(value['release_endpoint'])
        if release_endpoint['environment'] != bound['environment']:
            raise ReleaseApplyError('CURRENTNESS_HOLD')
        for purpose in APPLY_PURPOSES:
            validate_apply_pins(value['operation_pins'], purpose=purpose, now_ms=now_ms)
        validate_registration_pins(value['registration_pins'], now_ms=now_ms)
        # Actual credential I/O finishes before the short control-store lock.
        with control.release_owner_lock():
            owner = release_state._read(control)
            activation = control.read(release_state.ACTIVATION)
            if (activation['initial_trust_sha256'] != value['initial_trust_sha256']
                    or any(owner['state'][k] != bound[k] for k in (*profile_fields, 'device_id'))):
                raise ReleaseApplyError('CURRENTNESS_HOLD')
        monitor.guard_credential()
        if (control.read('release-bootstrap.json') != value
                or monitor.enrollment_binding() != enrollment
                or provision.profile_snapshot(monitor, profile='monitor', now_ms=now_ms) != current_monitor
                or provision.profile_snapshot(monitor, profile='support', now_ms=now_ms) != support
                or monitor.read('state.json') != state or time.monotonic() >= deadline):
            raise ReleaseApplyError('CURRENTNESS_HOLD')
        binding = {'bootstrap_sha256': control._fingerprint(value),
            'monitor_binding': bound, 'support_snapshot_sha256': value['support_snapshot_sha256'],
            'monitor_snapshot_sha256': monitor._fingerprint(current_monitor),
            'credential_sha256': monitor._fingerprint(credential),
            'owner_evidence_sha256': owner['owner_evidence_sha256'],
            'device_epoch': support['bundle']['device_epoch']}
        scope = {k: owner['state'][k] for k in (*release_contract.EXPECTED_FIELDS,
                                              'tenant', 'installation', 'device_id', 'realm_epoch')}
        scope.update(audience=release_endpoint['audience'], claim_id=bound['claim_id'], key_id=bound['key_id'])
        return _ReleaseCurrentness(value, binding, scope, {k: bound[k] for k in identity_fields}, secret)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ReleaseApplyError('CURRENTNESS_HOLD') from None



@dataclass(frozen=True, repr=False)
class _PreparedRegistrationRead:
    endpoint: str
    path: str
    body: bytes
    headers: tuple
    expected: bytes
    pins: bytes
    owner_evidence_sha256: str
    device_evidence_sha256: str
    deadline: float


def _prepare_registration_read(current, *, expected_registration_id, now_ms, deadline):
    """The owner creates the challenge and immutable wire bytes before untrusted transport."""
    from adapters.sdc import protocol, release_contract
    from adapters.sdc import release_registration_contract as registration
    from adapters.sdc.support_contract import canonical
    try:
        if (type(current) is not _ReleaseCurrentness or not release_contract.integer(now_ms)
                or type(deadline) not in (int, float) or not math.isfinite(deadline)
                or time.monotonic() >= deadline):
            raise ValueError
        import uuid
        body = dict(schema_version=registration.REQUEST_VERSION, request_id=str(uuid.uuid4()),
                    expected_registration_id=expected_registration_id)
        registration.validate_request(body)
        encoded = canonical(body).encode('ascii')
        endpoint = current.bootstrap['release_endpoint']
        expected = {k: current.scope[k] for k in registration.SCOPE if k not in ('policy_epoch', 'device_epoch')}
        # This release capability is restricted to the self tenant's fixed policy epoch.
        if expected['tenant'] != 'self':
            raise ValueError
        expected.update(policy_epoch=1, device_epoch=current.binding['device_epoch'],
                        request_id=body['request_id'], expected_registration_id=expected_registration_id)
        owner_hash = current.binding['owner_evidence_sha256']
        if not release_contract.hex64(owner_hash) or owner_hash == '0' * 64:
            raise ValueError
        device_hash = hashlib.sha256(canonical(dict(
            schema_version='mcs.release.registration-evidence.v1',
            bootstrap_sha256=current.binding['bootstrap_sha256'],
            monitor_binding=current.binding['monitor_binding'],
            support_snapshot_sha256=current.binding['support_snapshot_sha256'],
            monitor_snapshot_sha256=current.binding['monitor_snapshot_sha256'],
            owner_evidence_sha256=owner_hash, device_epoch=current.binding['device_epoch'])).encode('ascii')).hexdigest()
        headers = protocol.sign_release_registration(endpoint, registration.PATH, encoded,
            current.identity, current.secret, now=now_ms / 1000, nonce=secrets.token_hex(16))
        return _PreparedRegistrationRead(endpoint['origin'], registration.PATH, encoded,
            tuple(sorted(headers.items())), canonical(expected).encode('ascii'),
            canonical(current.bootstrap['registration_pins'], max_bytes=16384).encode('ascii'),
            owner_hash, device_hash, float(deadline))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise ReleaseApplyError('REGISTRATION_HOLD') from None


def _verify_registration_read(prepared, raw, *, now_ms):
    """Only verified current registered bytes with independently accepted evidence may pass."""
    from adapters.sdc import release_registration_contract as registration
    from adapters.sdc.support_contract import canonical
    from sdc_support_signatures import verify_registration_envelope
    try:
        if (type(prepared) is not _PreparedRegistrationRead or type(raw) is not bytes
                or time.monotonic() >= prepared.deadline):
            raise ValueError
        response = registration.validate_response(raw)
        value = verify_registration_envelope(canonical(response['proof']).encode('ascii'),
            pins=json.loads(prepared.pins), expected=json.loads(prepared.expected), now_ms=now_ms)
        if (value['status'] != 'registered'
                or value['owner']['owner_evidence_sha256'] != prepared.owner_evidence_sha256
                or value['registration']['device_evidence_sha256'] != prepared.device_evidence_sha256
                or time.monotonic() >= prepared.deadline):
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise ReleaseApplyError('REGISTRATION_HOLD') from None


@dataclass(frozen=True, repr=False)
class _PreparedApplyControl:
    endpoint: str
    path: str
    body: bytes
    headers: tuple
    binding: bytes
    deadline: float


def _prepare_apply_control(current, kind, body, *, now_ms, deadline):
    """Freeze a device control and fresh HMAC; it never grants execution authority."""
    from adapters.sdc import protocol, release_apply_contract, release_contract
    from adapters.sdc.support_contract import canonical
    try:
        if (type(current) is not _ReleaseCurrentness or type(kind) is not str
                or kind not in release_apply_contract.CONTROLS or not release_contract.integer(now_ms)
                or type(deadline) not in (int, float) or not math.isfinite(deadline)
                or time.monotonic() >= deadline):
            raise ValueError
        path = next(path for path, value in protocol.RELEASE_APPLY_PATHS.items() if value == kind)
        value = protocol._release_apply_body(kind, body, now_ms=now_ms)
        if kind in ('plan_report', 'grant'):
            for key in (*release_contract.EXPECTED_FIELDS, 'tenant', 'installation', 'device_id', 'realm_epoch'):
                if value['state'][key] != current.scope[key]:
                    raise ValueError
        encoded = canonical(value, max_bytes=16384).encode('ascii')
        endpoint = protocol.endpoint(current.bootstrap['release_endpoint'])
        if endpoint['environment'] != current.scope['environment']:
            raise ValueError
        headers = protocol.sign_release_apply(endpoint, path, encoded, current.identity, current.secret,
            now=now_ms / 1000, nonce=secrets.token_hex(16))
        if time.monotonic() >= deadline:
            raise ValueError
        return _PreparedApplyControl(endpoint['origin'], path, encoded, tuple(sorted(headers.items())),
            canonical(current.binding).encode('ascii'), float(deadline))
    except (ValueError, TypeError, KeyError, AttributeError, StopIteration, OverflowError, RecursionError):
        raise ReleaseApplyError('CONTROL_HOLD') from None


def _read_frozen_plan(control):
    """Read a fixed local report as data only; it cannot restore a grant or a live owner."""
    from adapters.sdc import release_apply_contract as apply, release_contract as release
    from adapters.sdc.support_contract import canonical, parse_canonical
    try:
        value = control.read('release-plan.json')
        if value is None:
            return None
        release.fields(value, ('schema_version', 'root_fingerprint', 'repo_fingerprint', 'binding',
            'planning_consent_id', 'raw', 'body_sha256', 'attestation', 'target_source_sha256', 'record_sha256'))
        if (value['schema_version'] != 'mcs.release-plan-report.v1'
                or not all(release.hex64(value[k]) for k in ('root_fingerprint', 'repo_fingerprint', 'body_sha256', 'target_source_sha256', 'record_sha256'))
                or value['root_fingerprint'] != control.root_fingerprint()
                or not release.uuid(value['planning_consent_id']) or type(value['binding']) is not dict
                or type(value['raw']) is not str):
            raise ValueError
        record_body = {key: item for key, item in value.items() if key != 'record_sha256'}
        if control._fingerprint(record_body) != value['record_sha256']:
            raise ValueError
        raw = value['raw'].encode('ascii')
        body = parse_canonical(raw, max_bytes=16384)
        apply.validate_apply_control('plan_report', body, now_ms=body['plan']['issued_at'])
        if (canonical(body, max_bytes=16384).encode('ascii') != raw or hashlib.sha256(raw).hexdigest() != value['body_sha256']
                or body['local_planning_consent_id'] != value['planning_consent_id']):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        raise ReleaseApplyError('PLAN_HOLD') from None


def _prepare_native_plan(control, monitor, *, runtime_home, repository, case_id,
                         attestation_envelope, artifact_source, deadline):
    """Freeze a native plan before sending; incoming operations cannot supply its report or receipt."""
    from adapters.sdc import release_contract as release, release_apply_contract as apply, release_state
    import mcs_update
    import release_owner_source
    try:
        if not release.uuid(case_id) or control._release_entry_fd is not None:
            raise ValueError
        mcs_update._bounded_timeout(None, deadline)
        with control.release_authority_lock(), monitor.lock():
            now = int(time.time() * 1000)
            current = _load_release_currentness(control, monitor, runtime_home=runtime_home, now_ms=now, deadline=deadline)
            consent = _validate_planning_consent(control.read('release-planning.json'))
            if (consent['case_id'] != case_id or consent['binding'] != current.binding
                    or consent['root_fingerprint'] != control.root_fingerprint()
                    or consent['repo_fingerprint'] != _repository_fingerprint(repository)
                    or not consent['confirmed_at'] <= now < consent['expires_at']):
                raise ValueError
            prior = _read_frozen_plan(control)
            if prior is not None:
                previous_body = json.loads(prior['raw'])
                if (previous_body['plan']['expires_at'] > now
                        or prior['planning_consent_id'] == consent['planning_consent_id']):
                    raise ValueError
        # All crypto, source/SDK observations and artifact workers run without A/T/S held.
        envelope = release.input_record(attestation_envelope, 16384)
        attestation, _ = release_state._observe_attestation(control, envelope, now_ms=int(time.time() * 1000))
        with control.release_owner_lock():
            owner = release_state._read(control)
        provider = _initial_provider_source(control, repository, deadline=deadline)
        if list(provider['provider_profile']) < attestation['core']['minimum_updater']:
            raise ValueError
        with _inspect_artifact(artifact_source, core=attestation['core'], policy=owner['trust']['policy'],
                profile_id=owner['baseline']['profile_id'], profile_sha256=owner['baseline']['profile_sha256'], deadline=deadline) as artifact:
            target_source = release_owner_source.verify_target(artifact['tree_files'], deadline=deadline)
        sampled = _native_preflight(control, runtime_home=runtime_home, repository=repository, deadline=deadline)
        now = int(time.time() * 1000)
        expires = min(now + owner['trust']['policy']['plan_ttl_ms'], attestation['expires_at'],
                      owner['trust']['expires_at'], consent['expires_at'])
        state = owner['state']
        plan = dict(schema_version=release.VERSIONS['plan'], plan_id=str(uuid.uuid4()),
            **{key: state[key] for key in (*release.EXPECTED_FIELDS, 'tenant', 'installation', 'device_id',
                'realm_epoch', 'trust_digest', 'root_version', 'metadata_epoch', *release.STATE_POINTS)},
            release_id=attestation['release_id'], manifest_digest=release.hash_release_manifest(attestation),
            target_code_seq=attestation['core']['code_seq'], preconditions=sampled['preconditions'],
            precondition_sha256=release.hash_release_preconditions(sampled['preconditions']),
            window={'starts_at': now, 'ends_at': expires}, canary_stage='self_test', modes=[], issued_at=now, expires_at=expires)
        plan = release.validate_release_plan(plan, trust=owner['trust'], attestation=attestation, state=state, now_ms=now)
        if release.release_plan_blockers(plan, attestation, owner['trust'], state, now_ms=now):
            raise ValueError
        body = dict(schema_version=apply.VERSIONS['control'], report_id=str(uuid.uuid4()), case_id=case_id,
            local_planning_consent_id=consent['planning_consent_id'], state=state, plan=plan)
        with control.release_authority_lock(), monitor.lock():
            now = int(time.time() * 1000)
            fresh = _load_release_currentness(control, monitor, runtime_home=runtime_home, now_ms=now, deadline=deadline)
            if (fresh.binding != current.binding or control.read('release-planning.json') != consent
                    or now >= expires or mcs_update.mcs_util.load_config() != sampled['configuration']):
                raise ValueError
            prepared = _prepare_apply_control(fresh, 'plan_report', body, now_ms=now, deadline=deadline)
            record = dict(schema_version='mcs.release-plan-report.v1', root_fingerprint=control.root_fingerprint(),
                repo_fingerprint=consent['repo_fingerprint'], binding=fresh.binding,
                planning_consent_id=consent['planning_consent_id'], raw=prepared.body.decode('ascii'),
                body_sha256=hashlib.sha256(prepared.body).hexdigest(), attestation=envelope,
                target_source_sha256=target_source['sha256'])
            record['record_sha256'] = control._fingerprint(record)
            with control.release_owner_lock():
                if release_state._read(control) != owner or _read_frozen_plan(control) != prior:
                    raise ValueError
                try:
                    control.write('release-plan.json', record)
                    if _read_frozen_plan(control) != record or time.monotonic() >= deadline:
                        raise ReleaseApplyError('PLAN_HOLD')
                except BaseException:
                    control._release_owner_uncertain = True
                    raise
            return record, prepared
    except ReleaseApplyError as exc:
        if exc.code in ('RELEASE_APPLY_SETTINGS_MIGRATION_REQUIRED', 'RELEASE_APPLY_SETTINGS_MIGRATION_UNVERIFIED'):
            raise
        raise ReleaseApplyError('PLAN_HOLD') from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, mcs_update.UpdateError):
        raise ReleaseApplyError('PLAN_HOLD') from None


def _capture_release_case(control, monitor, *, runtime_home, repository, deadline):
    with control.release_authority_lock():
        return _case_under_authority(control, monitor, runtime_home=runtime_home, repository=repository, deadline=deadline)


def _case_under_authority(control, monitor, *, runtime_home, repository, deadline, started=None):
    """Read independent R4 case epochs while the caller owns the actual same-thread authority lock."""
    from adapters.sdc import provision, release_apply_contract as apply, release_contract as release
    from adapters.sdc import release_state, support_client, support_state
    captured = {}
    try:
        if control._release_authority_fd is None or control._release_authority_thread != threading.get_ident():
            raise ValueError
        now = int(time.time() * 1000)
        def binding_supplier():
            current = _load_release_currentness(control, monitor, runtime_home=runtime_home, now_ms=now, deadline=deadline)
            profile = provision.profile_snapshot(monitor, profile='support', now_ms=now)
            bundle = profile['bundle']
            endpoint = {key: bundle[key] for key in ('origin', 'audience', 'environment')}
            captured['current'] = current
            return dict(root_fingerprint=current.binding['monitor_binding']['root_fingerprint'],
                monitor_endpoint_hash=current.binding['monitor_binding']['endpoint_hash'],
                support_endpoint_hash=monitor._fingerprint(endpoint), pins_sha256=support_client._hash(bundle['pins']),
                secret_sha256=hashlib.sha256(bytes.fromhex(current.secret)).hexdigest(), key_id=current.identity['key_id'],
                profile=support_client._profile_binding(profile, now_ms=now))
        # R4 owns support-state -> tick order; never acquire support-state under an already-held tick.
        support = support_state.prepare(monitor, binding_supplier=binding_supplier, now_ms=now)
        if not support['enabled'] or support['receipt'] is None:
            raise ValueError
        current = captured['current']
        record = _read_frozen_plan(control)
        if (record is None or record['binding'] != current.binding
                or record['repo_fingerprint'] != _repository_fingerprint(repository)):
            raise ValueError
        body = json.loads(record['raw'])
        plan, consent = body['plan'], support['receipt']['preview']
        if (now >= plan['expires_at'] or consent['case_id'] != body['case_id']
                or consent['key_id'] != current.identity['key_id']
                or consent['device_epoch'] != current.binding['device_epoch']):
            raise ValueError
        for key in ('tenant', 'installation', 'device_id', 'environment', 'realm_epoch'):
            if consent[key] != current.scope[key]:
                raise ValueError
        with control.release_owner_lock():
            owner = release_state._read(control)
            expected_state = body['state']
            if started is not None:
                expected_state = {**expected_state, 'unresolved': dict(operation_id=started['operation']['operation_id'],
                    plan_sha256=started['operation']['plan_hash'], phase='unknown')}
                if not owner['spent'] or owner['spent'][-1] != started:
                    raise ValueError
            if owner['state'] != expected_state:
                raise ValueError
        expected = dict(current.scope)
        expected.update({key: consent[key] for key in ('case_id', 'policy_epoch', 'device_epoch', 'case_epoch', 'local_consent_epoch')})
        expected.update({key: plan[key] for key in ('trust_digest', 'root_version', 'metadata_epoch', 'release_id',
            'manifest_digest', 'target_code_seq', 'plan_id')}, plan_hash=release.hash_release_plan(plan),
            capability_revision=apply.CAPABILITY_REVISION, consent_revision=apply.CONSENT_REVISION)
        if not set(apply.EXPECTED_FIELDS) <= set(expected) or time.monotonic() >= deadline:
            raise ValueError
        return {'current': current, 'record': record, 'expected': expected,
            'support_sha256': monitor._fingerprint(support), 'purpose_code': consent['purpose_code'],
            'expires_at': min(plan['expires_at'], consent['consent_expires_at'])}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ReleaseApplyError('CASE_HOLD') from None


def _read_change(control):
    from adapters.sdc import release_contract as release
    try:
        value = control.read('release-change.json')
        if value is None:
            return None
        release.fields(value, ('schema_version', 'root_fingerprint', 'repo_fingerprint', 'binding',
            'plan_record_sha256', 'support_sha256', 'expected', 'operation_id', 'preview_raw', 'preview',
            'local_receipt', 'accept_raw', 'acceptance', 'reason_sha256', 'revoked', 'record_sha256'))
        if (value['schema_version'] != 'mcs.release-change.v1'
                or value['root_fingerprint'] != control.root_fingerprint()
                or not all(release.hex64(value[key]) for key in ('root_fingerprint', 'repo_fingerprint',
                    'plan_record_sha256', 'support_sha256', 'record_sha256'))
                or not release.uuid(value['operation_id']) or type(value['revoked']) is not bool
                or control._fingerprint({key: item for key, item in value.items() if key != 'record_sha256'}) != value['record_sha256']):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ReleaseApplyError('CHANGE_HOLD') from None


def _save_change(control, value):
    if control._release_authority_fd is None or control._release_authority_thread != threading.get_ident():
        raise ReleaseApplyError('CHANGE_HOLD')
    value = json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))
    value.pop('record_sha256', None)
    value['record_sha256'] = control._fingerprint(value)
    try:
        control.write('release-change.json', value)
        if _read_change(control) != value:
            raise ReleaseApplyError('CHANGE_HOLD')
    except BaseException:
        control._release_owner_uncertain = True
        raise
    return value


def _begin_native_change(control, monitor, *, runtime_home, repository, operation_id, deadline):
    """Fix the preview request and its independent case/plan expectations before HTTP."""
    from adapters.sdc import release_contract as release, release_apply_contract as apply
    try:
        if not release.uuid(operation_id):
            raise ValueError
        with control.release_authority_lock():
            context = _case_under_authority(control, monitor, runtime_home=runtime_home, repository=repository, deadline=deadline)
            previous = _read_change(control)
            if previous is not None and (not previous['revoked'] or previous['operation_id'] == operation_id):
                raise ValueError
            now = int(time.time() * 1000)
            request = dict(schema_version=apply.VERSIONS['control'], preview_id=str(uuid.uuid4()), operation_id=operation_id)
            prepared = _prepare_apply_control(context['current'], 'preview', request, now_ms=now, deadline=deadline)
            expected = {**context['expected'], 'operation_id': operation_id, 'preview_id': request['preview_id']}
            record = dict(schema_version='mcs.release-change.v1', root_fingerprint=control.root_fingerprint(),
                repo_fingerprint=context['record']['repo_fingerprint'], binding=context['current'].binding,
                plan_record_sha256=context['record']['record_sha256'], support_sha256=context['support_sha256'],
                expected=expected, operation_id=operation_id, preview_raw=prepared.body.decode('ascii'),
                preview=None, local_receipt=None, accept_raw=None, acceptance=None, reason_sha256=None, revoked=False)
            record = _save_change(control, record)
            if time.monotonic() >= deadline:
                raise ValueError
            return record, prepared
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ReleaseApplyError('CHANGE_HOLD') from None


def _current_change(control, monitor, *, runtime_home, repository, operation_id, deadline, started=None):
    context = _case_under_authority(control, monitor, runtime_home=runtime_home, repository=repository, deadline=deadline, started=started)
    record = _read_change(control)
    if (record is None or record['revoked'] or record['operation_id'] != operation_id
            or record['binding'] != context['current'].binding
            or record['plan_record_sha256'] != context['record']['record_sha256']
            or record['repo_fingerprint'] != context['record']['repo_fingerprint']
            or record['support_sha256'] != context['support_sha256']
            or {key: record['expected'][key] for key in context['expected']} != context['expected']):
        raise ReleaseApplyError('CHANGE_HOLD')
    return context, record


def _apply_response(raw, kind, names):
    from adapters.sdc import release_contract
    from adapters.sdc.protocol import strict_json
    if type(raw) is not bytes or not 0 < len(raw) <= 16384 or not raw.isascii():
        raise ReleaseApplyError('CHANGE_HOLD')
    value = strict_json(raw)
    release_contract.fields(value, ('schema_version', *names))
    if value['schema_version'] != 'sdc.release.apply-' + kind + '-response.v1':
        raise ReleaseApplyError('CHANGE_HOLD')
    return value


def _consent_native_change(control, monitor, *, runtime_home, repository, operation_id, preview_response, deadline):
    """Verify independent approval, ask the real TTY, then freeze the receipt before accept HTTP."""
    from adapters.sdc import release_apply_contract as apply
    from adapters.sdc.support_contract import canonical
    from sdc_support_signatures import verify_apply_envelope
    try:
        with control.release_authority_lock():
            context, before = _current_change(control, monitor, runtime_home=runtime_home, repository=repository,
                operation_id=operation_id, deadline=deadline)
            if before['preview'] is not None or before['local_receipt'] is not None:
                raise ValueError
        now = int(time.time() * 1000)
        response = _apply_response(preview_response, 'preview', ('intent', 'approval', 'preview'))
        preview = verify_apply_envelope(canonical(response['preview'], max_bytes=16384).encode('ascii'),
            purpose=apply.PURPOSES[0], pins=context['current'].bootstrap['operation_pins'],
            expected=before['expected'], now_ms=now)
        plan = json.loads(context['record']['raw'])
        if (not apply.same(response['intent'], preview['intent']) or not apply.same(response['approval'], preview['approval'])
                or preview['intent']['plan_report_id'] != plan['report_id']
                or preview['intent']['plan_report_hash'] != context['record']['body_sha256']
                or preview['intent']['purpose_code'] != context['purpose_code']):
            raise ValueError
        window = preview['intent']['window']
        expires = min(preview['expires_at'], context['expires_at'], window['ends_at'])
        if now >= expires or time.monotonic() >= deadline:
            raise ValueError
        prompt = ('端末 ' + preview['installation'] + ' の個別更新を確認します。\n'
            '対象版: ' + preview['release_id'] + '（順序 ' + str(preview['target_code_seq']) + '）\n'
            '実施窓（Unixミリ秒）: ' + str(window['starts_at']) + '〜' + str(window['ends_at']) + '\n'
            '署名済み版の取込み、既存backup、処理の一時停止、更新と再起動を実行します。\n'
            '中央の個別承認を検証済みです。端末での変更同意を記録します。\n'
            '1: この個別更新に同意する\n2: 中止する（既定）\n番号: ')
        reason = _tty_confirmation(prompt, deadline=min(deadline, time.monotonic() + (expires - now) / 1000))
        if reason is None:
            _cancel_native_change(control, runtime_home=runtime_home, operation_id=operation_id)
            return None
        with control.release_authority_lock():
            after_context, after = _current_change(control, monitor, runtime_home=runtime_home, repository=repository,
                operation_id=operation_id, deadline=deadline)
            now = int(time.time() * 1000)
            if after != before or now >= expires or time.monotonic() >= deadline:
                raise ValueError
            local = dict(schema_version=apply.VERSIONS['receipt'], **{key: preview[key] for key in apply.COMMON},
                **{key: preview[key] for key in ('operation_id', 'intent_hash', 'change_consent_id', 'change_binding_hash', 'preview_id')},
                approval_hash=preview['approval']['approval_hash'], preview_payload_hash=apply.hash_apply_preview(preview),
                local_change_receipt_id=str(uuid.uuid4()), change_accept_id=str(uuid.uuid4()),
                confirmed_at=now, expires_at=min(preview['intent']['expires_at'], preview['approval']['expires_at'],
                    after_context['expires_at']), receipt_hash=apply.ZERO)
            local['receipt_hash'] = apply.hash_apply_local_receipt(local)
            apply.validate_apply_local_receipt(local, now_ms=now)
            accept = dict(schema_version=apply.VERSIONS['control'], accept_id=local['change_accept_id'],
                **{key: local[key] for key in ('preview_id', 'change_consent_id', 'change_binding_hash',
                    'local_change_receipt_id', 'preview_payload_hash')}, local_change_receipt_hash=local['receipt_hash'])
            prepared = _prepare_apply_control(after_context['current'], 'accept', dict(control=accept, receipt=local),
                now_ms=now, deadline=deadline)
            after.update(preview=response, local_receipt=local, reason_sha256=reason, accept_raw=prepared.body.decode('ascii'))
            saved = _save_change(control, after)
            if time.monotonic() >= deadline:
                raise ValueError
            return saved, prepared
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ReleaseApplyError('CHANGE_HOLD') from None


def _accept_native_change(control, monitor, *, runtime_home, repository, operation_id, acceptance_response, deadline):
    from adapters.sdc import release_apply_contract as apply
    from adapters.sdc.support_contract import canonical
    from sdc_support_signatures import verify_apply_envelope
    try:
        response = _apply_response(acceptance_response, 'accept', ('acceptance',))
        with control.release_authority_lock():
            context, before = _current_change(control, monitor, runtime_home=runtime_home, repository=repository,
                operation_id=operation_id, deadline=deadline)
            if before['acceptance'] is not None or before['preview'] is None:
                raise ValueError
        payload = verify_apply_envelope(canonical(before['preview']['preview'], max_bytes=16384).encode('ascii'),
            purpose=apply.PURPOSES[0], pins=context['current'].bootstrap['operation_pins'],
            expected=before['expected'], now_ms=int(time.time() * 1000), allow_expired=True)
        with control.release_authority_lock():
            _, record = _current_change(control, monitor, runtime_home=runtime_home, repository=repository,
                operation_id=operation_id, deadline=deadline)
            now = int(time.time() * 1000)
            ack = apply.validate_apply_acceptance(response['acceptance'], now_ms=now)
            local = apply.validate_apply_local_receipt(record['local_receipt'], now_ms=now)
            if (record != before or ack['accept_id'] != local['change_accept_id']
                    or not ack['local_change_receipt_hash'] == local['receipt_hash'] == apply.hash_apply_local_receipt(local)
                    or any(ack[key] != local[key] for key in ('preview_id', 'operation_id', 'change_consent_id',
                        'change_binding_hash', 'local_change_receipt_id', 'preview_payload_hash'))
                    or not local['confirmed_at'] <= ack['accepted_at'] < payload['expires_at']
                    or ack['expires_at'] > local['expires_at'] or time.monotonic() >= deadline):
                raise ValueError
            record['acceptance'] = ack
            saved = _save_change(control, record)
            if time.monotonic() >= deadline:
                raise ValueError
            return saved
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ReleaseApplyError('CHANGE_HOLD') from None


def _cancel_native_change(control, *, runtime_home, operation_id):
    """Revoke local permission with A alone; never clear STARTED, J or SPENT."""
    from adapters.sdc import release_contract
    if control.root != _control_root(runtime_home) or not release_contract.uuid(operation_id):
        raise ReleaseApplyError('CHANGE_HOLD')
    with control.release_authority_lock():
        record = _read_change(control)
        if record is None or record['operation_id'] != operation_id:
            raise ReleaseApplyError('CHANGE_HOLD')
        if not record['revoked']:
            record['revoked'] = True
            record = _save_change(control, record)
        return record


def _run_native_release(control, monitor, *, runtime_home, repository, operation_id,
                        expected_registration_id, artifact_source, carrier, deadline):
    """Own fresh admission, one grant consumption and the borrowed pipeline in this process."""
    from contextlib import ExitStack
    import mcs_update
    import owned_process
    import release_owner_source
    import update_journal
    from adapters.sdc import release_apply_contract as apply, release_contract as release, release_state
    from adapters.sdc.support_contract import canonical
    from sdc_support_signatures import verify_apply_envelope
    owner, capability, entry, spent = object(), None, None, None
    queued = False
    def persist_started():
        if spent is not None and not queued and not getattr(control, '_release_owner_uncertain', False):
            _queue_native_unknown(control, operation=operation, grant=grant, admission=admission, spent=spent, installed=None)
    try:
        if (not release.uuid(operation_id) or os.getpid() != _EFFECT_PROCESS_ID
                or control.root != _control_root(runtime_home)):
            raise ValueError
        mcs_update._bounded_timeout(None, deadline)
        with control.release_authority_lock():
            context, change = _current_change(control, monitor, runtime_home=runtime_home,
                repository=repository, operation_id=operation_id, deadline=deadline)
            if change['acceptance'] is None or change['local_receipt'] is None:
                raise ValueError
            current = context['current']
            now = int(time.time() * 1000)
            poll_request = _prepare_apply_control(current, 'poll', dict(schema_version=apply.VERSIONS['control'],
                poll_id=str(uuid.uuid4()), case_id=context['expected']['case_id']), now_ms=now, deadline=deadline)
        _initial_provider_source(control, repository, deadline=deadline)
        _require_settled_install_options(runtime_home, repository, deadline)
        # The carrier supplies untrusted bytes only; no callback supplies observations or authority.
        polled = _apply_response(carrier(poll_request), 'poll', ('context',))['context']
        release.fields(polled, ('intent', 'approval', 'acceptance', 'operation', 'admission'))
        if polled['admission'] is not None or not apply.same(polled['acceptance'], change['acceptance']):
            raise ValueError
        now = int(time.time() * 1000)
        operation = verify_apply_envelope(canonical(polled['operation'], max_bytes=16384).encode('ascii'),
            purpose=apply.PURPOSES[1], pins=current.bootstrap['operation_pins'], expected=change['expected'], now_ms=now)
        preview = verify_apply_envelope(canonical(change['preview']['preview'], max_bytes=16384).encode('ascii'),
            purpose=apply.PURPOSES[0], pins=current.bootstrap['operation_pins'], expected=change['expected'],
            now_ms=now, allow_expired=True)
        authorization = dict(operation=operation, approval=polled['approval'], preview=preview,
            local_receipt=change['local_receipt'], acceptance=change['acceptance'],
            expected=change['expected'])
        frozen = context['record']
        plan = json.loads(frozen['raw'])['plan']
        if (not apply.same(polled['intent'], preview['intent']) or not apply.same(polled['approval'], preview['approval'])
                or preview['intent']['plan_report_id'] != json.loads(frozen['raw'])['report_id']
                or preview['intent']['plan_report_hash'] != frozen['body_sha256']):
            raise ValueError
        attestation, _ = release_state._observe_attestation(control, frozen['attestation'], now_ms=now)
        with control.release_owner_lock():
            baseline = release_state._read(control)
        with _inspect_artifact(artifact_source, core=attestation['core'], policy=baseline['trust']['policy'],
                profile_id=baseline['baseline']['profile_id'], profile_sha256=baseline['baseline']['profile_sha256'], deadline=deadline) as artifact, ExitStack() as lifetime:
            target_source = release_owner_source.verify_target(artifact['tree_files'], deadline=deadline)
            if target_source['sha256'] != frozen['target_source_sha256']:
                raise ValueError
            bundle_fd = os.open(artifact['bundle'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            lifetime.callback(os.close, bundle_fd)
            gate_fd = lifetime.enter_context(control.release_entry_lock())
            update_fd = mcs_update.acquire_update_lock()
            if update_fd is None:
                raise ValueError
            lifetime.callback(os.close, update_fd)
            journal_path = Path(runtime_home) / 'data/update_state.json'
            journal = update_journal.load_state(journal_path)
            stages = journal.get('stages', [])
            if (journal.get('_corrupt') or journal.get('applying')
                    or (stages and stages[-1].get('stage') != 'done')
                    or 'release_ref' in journal or any('release_ref' in row for row in journal.get('applied', []))):
                raise ValueError
            while True:
                mcs_update._bounded_timeout(None, deadline)
                run_fd = mcs_update.acquire_run_lock(mcs_update.RUN_LOCK)
                if run_fd is not None:
                    break
                time.sleep(min(.05, max(0, deadline - time.monotonic())))
            lifetime.callback(os.close, run_fd)
            bounded = min(deadline, time.monotonic() + 120)
            locks = (gate_fd, update_fd, run_fd)
            for fd, path in zip(locks, (control.root / 'release-entry.lock', mcs_update.UPDATE_LOCK, mcs_update.RUN_LOCK)):
                _verify_inherited_lock(fd, path)
            lifetime.enter_context(owned_process.lifetime_fds(locks))
            lifetime.callback(persist_started)
            preflight = _native_preflight(control, runtime_home=runtime_home, repository=repository, deadline=bounded)
            if mcs_update.precheck_local(preflight['configuration'], deadline=bounded):
                raise ValueError
            with control.release_authority_lock():
                fresh, same_change = _current_change(control, monitor, runtime_home=runtime_home,
                    repository=repository, operation_id=operation_id, deadline=bounded)
                if same_change != change:
                    raise ValueError
                registration_request = _prepare_registration_read(fresh['current'],
                    expected_registration_id=expected_registration_id, now_ms=int(time.time() * 1000), deadline=bounded)
            registration = _verify_registration_read(registration_request, carrier(registration_request), now_ms=int(time.time() * 1000))
            authorization['registration'] = registration['registration']
            apply.validate_apply_authorization(**authorization, now_ms=int(time.time() * 1000))
            with control.release_authority_lock():
                fresh, same_change = _current_change(control, monitor, runtime_home=runtime_home,
                    repository=repository, operation_id=operation_id, deadline=bounded)
                with control.release_owner_lock():
                    witness = release_state._read(control)
                if same_change != change or witness != baseline:
                    raise ValueError
                witness_hashes = apply.validate_apply_grant_witness(plan=plan, attestation=attestation,
                    trust=witness['trust'], preconditions=preflight['preconditions'], state=witness['state'], now_ms=int(time.time() * 1000))
                admission_id, receipt_id, generation = str(uuid.uuid4()), str(uuid.uuid4()), secrets.token_hex(16)
                admission_body = dict(schema_version=apply.VERSIONS['control'], admission_id=admission_id,
                    operation_id=operation_id, payload_hash=operation['payload_hash'], receipt_id=receipt_id, owner_generation=generation)
                admission_request = _prepare_apply_control(fresh['current'], 'admit', admission_body,
                    now_ms=int(time.time() * 1000), deadline=bounded)
            admission = apply.validate_apply_admission(_apply_response(carrier(admission_request), 'admit', ('admission',))['admission'], now_ms=int(time.time() * 1000))
            if any(admission[key] != admission_body[key] for key in apply.ADMISSION if key != 'admitted_at'):
                raise ValueError
            with control.release_authority_lock():
                fresh, same_change = _current_change(control, monitor, runtime_home=runtime_home,
                    repository=repository, operation_id=operation_id, deadline=bounded)
                if same_change != change:
                    raise ValueError
                grant_body = dict(schema_version=apply.VERSIONS['control'], grant_request_id=str(uuid.uuid4()),
                    operation_id=operation_id, payload_hash=operation['payload_hash'], receipt_id=receipt_id,
                    admission_id=admission_id, preconditions=preflight['preconditions'], state=witness['state'])
                grant_request = _prepare_apply_control(fresh['current'], 'grant', grant_body, now_ms=int(time.time() * 1000), deadline=bounded)
            grant_response = _apply_response(carrier(grant_request), 'grant', ('admission', 'grant'))
            grant_raw = canonical(grant_response['grant'], max_bytes=16384).encode('ascii')
            grant_expected = {**change['expected'], **{key: admission[key] for key in ('receipt_id', 'admission_id', 'owner_generation')},
                'grant_request_id': grant_body['grant_request_id'], **witness_hashes}
            grant = verify_apply_envelope(grant_raw, purpose=apply.PURPOSES[2], pins=current.bootstrap['operation_pins'],
                expected=grant_expected, now_ms=int(time.time() * 1000))
            if not apply.same(grant_response['admission'], admission):
                raise ValueError
            apply.validate_apply_authorization(**authorization, grant=grant, admission=admission, now_ms=int(time.time() * 1000))
            continuation = object()
            with _EFFECT_MUTEX:
                if os.getpid() != _EFFECT_PROCESS_ID or owner in _LIVE_CONTINUATIONS:
                    raise ValueError
                _LIVE_CONTINUATIONS[owner] = continuation
            # Re-observe actual facts without changing the exact granted witness hashes.
            rechecked = _native_preflight(control, runtime_home=runtime_home, repository=repository, deadline=bounded)
            apply.validate_apply_grant_witness(plan=plan, attestation=attestation, trust=witness['trust'],
                preconditions=rechecked['preconditions'], state=witness['state'], now_ms=int(time.time() * 1000))
            if (rechecked['configuration'] != preflight['configuration'] or rechecked['observed'] != preflight['observed']
                    or any(rechecked['preconditions'][key] != preflight['preconditions'][key] for key in release.PRECONDITIONS if key not in ('observed_at', 'free_bytes'))
                    or rechecked['preconditions']['free_bytes'] < attestation['core']['artifact']['size'] + witness['trust']['policy']['max_unpacked_bytes']
                    or update_journal.load_state(journal_path) != journal):
                raise ValueError
            with control.release_authority_lock():
                fresh, same_change = _current_change(control, monitor, runtime_home=runtime_home,
                    repository=repository, operation_id=operation_id, deadline=bounded)
                now = int(time.time() * 1000)
                apply.validate_apply_authorization(**authorization, grant=grant, admission=admission, now_ms=now)
                if (same_change != change or now >= registration['expires_at']
                        or not operation['window']['starts_at'] <= now < operation['window']['ends_at']
                        or not grant['not_before'] <= now < grant['expires_at'] or time.monotonic() >= bounded):
                    raise ValueError
                _initial_provider_source(control, repository, deadline=bounded)
                _require_settled_install_options(runtime_home, repository, bounded)
                # Consume the same-process RAM continuation before the durable boundary.
                with _EFFECT_MUTEX:
                    if os.getpid() != _EFFECT_PROCESS_ID or _LIVE_CONTINUATIONS.pop(owner, None) is not continuation:
                        raise ValueError
                start_receipt_id = str(uuid.uuid4())
                start_not_before = max(grant['not_before'], operation['window']['starts_at'], registration['not_before'])
                start_expires_at = min(grant['expires_at'], operation['window']['ends_at'], registration['expires_at'], context['expires_at'])
                start_deadline = min(bounded, time.monotonic() + (start_expires_at - now) / 1000)
                spent = release_state._consume_and_start(control, operation=operation, grant=grant, admission=admission,
                    grant_envelope_sha256=hashlib.sha256(grant_raw).hexdigest(), witness_state=witness['state'],
                    preconditions=preflight['preconditions'], start_receipt_id=start_receipt_id, started_at=now)
                _, same_change = _current_change(control, monitor, runtime_home=runtime_home,
                    repository=repository, operation_id=operation_id, deadline=bounded, started=spent)
                if same_change != change or not start_not_before <= int(time.time() * 1000) < start_expires_at or time.monotonic() >= start_deadline:
                    raise ValueError
                with control.release_owner_lock():
                    published = release_state._read(control)
            import mcs_runtime
            venv, host = mcs_update._runtime_venv(preflight['configuration'])
            actual = _observe_installed(repository, python=mcs_runtime.python_executable(preflight['configuration'], root=runtime_home),
                venv=venv, runtime_mode=mcs_runtime.mode(preflight['configuration']), platform=preflight['preconditions']['platform'],
                ledger_path=mcs_update.LEDGER, host_source=host, deadline=bounded)
            if actual != preflight['observed'] or mcs_update.mcs_util.load_config() != preflight['configuration']:
                raise ValueError
            with control.release_authority_lock():
                _, same_change = _current_change(control, monitor, runtime_home=runtime_home,
                    repository=repository, operation_id=operation_id, deadline=bounded, started=spent)
                if same_change != change or time.monotonic() >= start_deadline or not start_not_before <= int(time.time() * 1000) < start_expires_at:
                    raise ValueError
                with control.release_owner_lock():
                    published = release_state._read(control)
                core = attestation['core']
                reference = dict(schema_version='mcs.release-reference.v1', root_fingerprint=control.root_fingerprint(),
                    repo_fingerprint=published['repo_fingerprint'], operation_id=operation_id, grant_id=grant['grant_id'],
                    plan_sha256=operation['plan_hash'], release_id=operation['release_id'], target_code_seq=operation['target_code_seq'],
                    target_commit=core['commit'], artifact_sha256=core['artifact']['sha256'], lock_sha256=core['lock_sha256'],
                    target_schema=core['schema']['target'], start_receipt_id=start_receipt_id, started_at=now)
                journal = {**journal, 'release_ref': reference, 'stages': []}
                update_journal.save_state(journal_path, journal)
                if update_journal.load_state(journal_path) != journal:
                    raise ValueError
                entry = _StartedEffect(owner=owner, process_id=os.getpid(), store=control, locks=locks,
                    lock_identities=tuple(_effect_fd_identity(fd) for fd in locks), runtime_home=str(runtime_home),
                    repository=str(repository), reference=reference, spent=spent, published_revision=published['revision'],
                    journal=journal, configuration=preflight['configuration'], start_deadline=start_deadline,
                    prequiesce_deadline=bounded, monitor=monitor, change_sha256=change['record_sha256'],
                    start_not_before=start_not_before, start_expires_at=start_expires_at)
                frame = _StartedArtifact(bundle_fd, _effect_fd_identity(bundle_fd), canonical(attestation).encode('ascii'),
                    release_owner_source.canonical(target_source['manifest']), target_source['sha256'])
                capability = _EffectCapability()
                with _EFFECT_MUTEX:
                    _EFFECT_PARENTS[owner], _EFFECT_CAPS[capability], _EFFECT_ARTIFACTS[owner] = entry, entry, frame
            try:
                result = mcs_update._apply_pipeline(None, core['commit'], None,
                    preflight['preconditions']['current_commit'], False, (), _release_cap=capability)
            finally:
                # Every effect/child retires inside the borrowed core before it returns or raises.
                with _EFFECT_MUTEX:
                    completed = _EFFECT_COMPLETED.get(entry)
                installed = None
                if completed is not None:
                    try:
                        import mcs_runtime
                        venv, host = mcs_update._runtime_venv(preflight['configuration'])
                        observed = _observe_installed(repository, python=mcs_runtime.python_executable(preflight['configuration'], root=runtime_home),
                            venv=venv, runtime_mode=mcs_runtime.mode(preflight['configuration']), host_source=host,
                            platform=preflight['preconditions']['platform'], ledger_path=mcs_update.LEDGER,
                            deadline=time.monotonic() + 30)
                        installed = release_state._observe_installed(control, frozen['attestation'],
                            observed={key: observed[key] for key in ('commit', 'lock_sha256', 'schema', 'profile_id', 'profile_sha256', 'runtime_fingerprint')},
                            now_ms=int(time.time() * 1000))
                        _save_pending_successor(control, entry=entry, frame=frame, completed=completed,
                            current_source_sha256=preflight['source_sha256'], observed=observed,
                            attestation_envelope=frozen['attestation'])
                    except (OSError, ValueError, mcs_update.UpdateError):
                        pass
                _queue_native_unknown(control, operation=operation, grant=grant, admission=admission,
                    spent=spent, installed=installed)
                queued = True
            return dict(started=True, readiness='unknown', installed=installed, pipeline_returncode=result)
    except ReleaseApplyError as exc:
        if exc.code in ('RELEASE_APPLY_SETTINGS_MIGRATION_REQUIRED', 'RELEASE_APPLY_SETTINGS_MIGRATION_UNVERIFIED'):
            raise
        raise ReleaseApplyError('RUN_HOLD') from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, mcs_update.UpdateError):
        raise ReleaseApplyError('RUN_HOLD') from None
    finally:
        with _EFFECT_MUTEX:
            if capability is not None:
                _EFFECT_CAPS.pop(capability, None)
            _EFFECT_PARENTS.pop(owner, None)
            _EFFECT_ARTIFACTS.pop(owner, None)
            _LIVE_CONTINUATIONS.pop(owner, None)
            if entry is not None:
                _EFFECT_CLAIMED.discard(entry)
                _EFFECT_RUNNING.discard(entry)
                _EFFECT_CHILDREN.discard(entry)
                _EFFECT_IMPORTED.discard(entry)
                _EFFECT_COMPLETED.pop(entry, None)


def _save_pending_successor(control, *, entry, frame, completed, current_source_sha256, observed, attestation_envelope):
    """Retain authenticated successor evidence; unknown/J never promotes it to an active owner."""
    import release_owner_source
    from adapters.sdc import release_state
    with _EFFECT_MUTEX:
        if (_EFFECT_PARENTS.get(entry.owner) is not entry or _EFFECT_COMPLETED.get(entry) != completed
                or entry.process_id != os.getpid() or completed['reference'] != entry.reference):
            raise ReleaseApplyError('SOURCE_HOLD')
    target = release_owner_source.verify_checkout(entry.repository, frame.source_manifest,
        deadline=time.monotonic() + 30, current=False)
    if target['sha256'] != frame.source_sha256:
        raise ReleaseApplyError('SOURCE_HOLD')
    with control.release_owner_lock():
        value = release_state._read(control)
        if (not value['spent'] or value['spent'][-1] != entry.spent or value['state']['unresolved'] is None
                or value['baseline']['commit'] != entry.reference['target_commit']
                or any(value['baseline'][key] != observed[key] for key in ('commit', 'lock_sha256', 'schema',
                    'profile_id', 'profile_sha256', 'runtime_fingerprint'))):
            raise ReleaseApplyError('SOURCE_HOLD')
        proof = dict(schema_version='mcs.release-owner-successor.v1', phase='pending',
            root_fingerprint=value['root_fingerprint'], repo_fingerprint=value['repo_fingerprint'],
            initial_source_sha256=value['owner_evidence_sha256'], parent_source_sha256=current_source_sha256,
            target_source_sha256=target['sha256'], target_manifest=target['manifest'],
            attestation=attestation_envelope, reference=entry.reference, completion=completed,
            observed=observed, state=value['state'])
        proof['record_sha256'] = control._fingerprint(proof)
        try:
            control.write('release-successor.json', proof)
            if control.read('release-successor.json') != proof:
                raise ReleaseApplyError('SOURCE_HOLD')
        except BaseException:
            control._release_owner_uncertain = True
            raise
    return proof


def _queue_native_unknown(control, *, operation, grant, admission, spent, installed):
    from adapters.sdc import release_apply_contract as apply, release_state
    now = int(time.time() * 1000)
    result = dict(schema_version=apply.VERSIONS['result'], operation_id=operation['operation_id'],
        payload_hash=operation['payload_hash'], receipt_id=grant['receipt_id'], grant_id=grant['grant_id'],
        release_id=operation['release_id'], manifest_digest=operation['manifest_digest'],
        target_code_seq=operation['target_code_seq'], generated_at=now, start_receipt_id=spent['start_receipt_id'],
        started_at=spent['started_at'], installed_release_id=installed['release_id'] if installed else None,
        installed_code_seq=installed['code_seq'] if installed else None, verified_at=None, readiness='unknown', error_code='RESULT_UNKNOWN')
    report = dict(schema_version=apply.VERSIONS['control'], report_id=str(uuid.uuid4()),
        operation_id=operation['operation_id'], payload_hash=operation['payload_hash'], receipt_id=grant['receipt_id'],
        grant_id=grant['grant_id'], sequence=1, state='unknown', observed_at=now, result=result)
    apply.validate_apply_outcome(report, operation=operation, grant=grant, admission=admission, now_ms=now)
    return release_state._queue_report(control, report, now_ms=now)


@dataclass(frozen=True, repr=False)
class _StartedArtifact:
    descriptor: int
    descriptor_identity: tuple
    attestation: bytes
    source_manifest: bytes
    source_sha256: str


def _effect_artifact(entry):
    from adapters.sdc import release_contract
    import release_owner_source
    if type(entry) is not _StartedEffect:
        raise ReleaseApplyError('ARTIFACT_HOLD')
    with _EFFECT_MUTEX:
        if (_EFFECT_PARENTS.get(entry.owner) is not entry or entry not in _EFFECT_RUNNING
                or entry.process_id != os.getpid()):
            raise ReleaseApplyError('ARTIFACT_HOLD')
        frame = _EFFECT_ARTIFACTS.get(entry.owner)
    try:
        if (type(frame) is not _StartedArtifact or type(frame.attestation) is not bytes
                or type(frame.source_manifest) is not bytes
                or _effect_fd_identity(frame.descriptor) != frame.descriptor_identity):
            raise ValueError
        attestation = release_contract.input_record(frame.attestation)
        core, reference = attestation['core'], entry.reference
        if (release_contract.hash_release_manifest(attestation) != entry.spent['operation']['manifest_digest']
                or attestation['release_id'] != reference['release_id']
                or any(core[key] != reference[ref] for key, ref in (
                    ('commit', 'target_commit'), ('code_seq', 'target_code_seq'), ('lock_sha256', 'lock_sha256')))
                or core['artifact']['sha256'] != reference['artifact_sha256']
                or core['schema']['target'] != reference['target_schema']
                or release_owner_source.digest(frame.source_manifest) != frame.source_sha256):
            raise ValueError
        return frame, attestation
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ReleaseApplyError('ARTIFACT_HOLD') from None


def _import_started_artifact(entry):
    """Import fixed authenticated bundle bytes only after this live entry published STARTED."""
    import mcs_update
    import owned_process
    frame, attestation = _effect_artifact(entry)
    with _EFFECT_MUTEX:
        if entry in _EFFECT_IMPORTED:
            raise ReleaseApplyError('ARTIFACT_HOLD')
        _EFFECT_IMPORTED.add(entry)
    core, deadline = attestation['core'], entry.prequiesce_deadline
    mcs_update._bounded_timeout(None, deadline)
    before = os.fstat(frame.descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size != core['artifact']['size']:
        raise ReleaseApplyError('ARTIFACT_HOLD')
    digest, offset = hashlib.sha256(), 0
    while offset < before.st_size:
        mcs_update._bounded_timeout(None, deadline)
        part = os.pread(frame.descriptor, min(65536, before.st_size - offset), offset)
        if not part:
            raise ReleaseApplyError('ARTIFACT_HOLD')
        digest.update(part)
        offset += len(part)
    after = os.fstat(frame.descriptor)
    if (digest.hexdigest() != core['artifact']['sha256'] or before.st_mtime_ns != after.st_mtime_ns
            or before.st_size != after.st_size or _effect_fd_identity(frame.descriptor) != frame.descriptor_identity):
        raise ReleaseApplyError('ARTIFACT_HOLD')
    with os.fdopen(os.dup(frame.descriptor), 'rb') as bundle, owned_process.lifetime_fds(entry.locks):
        bundle.seek(0)
        result = owned_process.run_owned(['/usr/bin/git', '-C', entry.repository, 'bundle', 'unbundle', '-'],
            30, mcs_update._release_git_environment(), stdin=bundle, deadline=deadline)
    if (result.returncode != 0
            or mcs_update._git_out(['cat-file', '-t', core['commit']], deadline=deadline).strip() != 'commit'
            or mcs_update._git_out(['rev-parse', '--verify', core['commit'] + '^{commit}'], deadline=deadline).strip() != core['commit']):
        raise ReleaseApplyError('ARTIFACT_HOLD')


def _verify_merged_source(entry):
    """The running parent keeps its own closure; verify the target before starting its child."""
    import release_owner_source
    frame, _ = _effect_artifact(entry)
    with _EFFECT_MUTEX:
        if entry not in _EFFECT_IMPORTED:
            raise ReleaseApplyError('ARTIFACT_HOLD')
    result = release_owner_source.verify_checkout(entry.repository, frame.source_manifest,
        deadline=time.monotonic() + 30, current=False)
    if result['sha256'] != frame.source_sha256:
        raise ReleaseApplyError('ARTIFACT_HOLD')

def _effect_fd_identity(fd):
    if type(fd) is not int or fd < 0:
        raise ReleaseApplyError('EFFECT_HOLD')
    info = os.fstat(fd)
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode


def _claim_effect_entry(capability, *, arguments, runtime_home, repository, journal_path):
    """同一processの登録済みRAM capabilityだけを一回消費する。保存値からは発行しない。"""
    if type(capability) is not _EffectCapability or os.getpid() != _EFFECT_PROCESS_ID:
        raise ReleaseApplyError('EFFECT_HOLD')
    import update_journal
    from adapters.sdc import release_state
    with _EFFECT_MUTEX:
        entry = _EFFECT_CAPS.pop(capability, None)
        if (type(entry) is not _StartedEffect or entry.process_id != os.getpid()
                or _EFFECT_PARENTS.get(entry.owner) is not entry or entry in _EFFECT_CLAIMED):
            raise ReleaseApplyError('EFFECT_HOLD')
        _EFFECT_CLAIMED.add(entry)
    try:
        ref = entry.reference
        expected_arguments = (None, ref['target_commit'], None, entry.spent['preconditions']['current_commit'],
                              False, (), None, False, None)
        if (type(arguments) is not tuple or len(arguments) != len(expected_arguments)
                or any(type(a) is not type(b) or a != b for a, b in zip(arguments, expected_arguments))
                or os.path.realpath(runtime_home) != os.path.realpath(entry.runtime_home)
                or os.path.realpath(repository) != os.path.realpath(entry.repository)
                or os.path.realpath(journal_path) != os.path.realpath(Path(entry.runtime_home) / 'data/update_state.json')
                or entry.store.root != _control_root(runtime_home)
                or not update_journal.valid_release_ref(ref)
                or ref['repo_fingerprint'] != _repository_fingerprint(repository)
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       for value in (entry.start_deadline, entry.prequiesce_deadline))
                or time.monotonic() >= min(entry.start_deadline, entry.prequiesce_deadline)
                or len(entry.locks) != 3 or len(entry.lock_identities) != 3
                or entry.store._release_entry_fd != entry.locks[0]
                or entry.store._release_entry_shared is not False):
            raise ReleaseApplyError('EFFECT_HOLD')
        paths = (entry.store.root / 'release-entry.lock', Path(runtime_home) / 'data/update.lock',
                 Path(runtime_home) / 'data/run.lock')
        for fd, expected, path in zip(entry.locks, entry.lock_identities, paths):
            current = path.stat(follow_symlinks=False)
            if (_effect_fd_identity(fd) != expected
                    or (current.st_dev, current.st_ino, current.st_uid, current.st_mode) != expected):
                raise ReleaseApplyError('EFFECT_HOLD')
        if update_journal.load_state(journal_path) != entry.journal or entry.journal.get('applying') or entry.journal.get('stages'):
            raise ReleaseApplyError('EFFECT_HOLD')
        with entry.store.release_owner_lock():
            saved = release_state._read(entry.store)
            expected_state = {**entry.spent['witness_state'], 'unresolved': {
                'operation_id': entry.spent['operation']['operation_id'],
                'plan_sha256': entry.spent['operation']['plan_hash'], 'phase': 'unknown'}}
            if (saved['revision'] != entry.published_revision or saved['state'] != expected_state
                    or not saved['spent'] or saved['spent'][-1] != entry.spent
                    or saved['root_fingerprint'] != ref['root_fingerprint']
                    or saved['repo_fingerprint'] != ref['repo_fingerprint']
                    or any(ref[key] != value for key, value in {
                        'operation_id': entry.spent['operation']['operation_id'],
                        'grant_id': entry.spent['grant']['grant_id'],
                        'plan_sha256': entry.spent['operation']['plan_hash'],
                        'release_id': entry.spent['operation']['release_id'],
                        'target_code_seq': entry.spent['operation']['target_code_seq'],
                        'start_receipt_id': entry.spent['start_receipt_id'],
                        'started_at': entry.spent['started_at']}.items())):
                raise ReleaseApplyError('EFFECT_HOLD')
        with entry.store.release_authority_lock():
            if entry.change_sha256 is not None:
                _, change = _current_change(entry.store, entry.monitor, runtime_home=runtime_home,
                    repository=repository, operation_id=ref['operation_id'], deadline=entry.prequiesce_deadline,
                    started=entry.spent)
                if (change['record_sha256'] != entry.change_sha256
                        or type(entry.start_not_before) is not int or type(entry.start_expires_at) is not int
                        or not entry.start_not_before <= int(time.time() * 1000) < entry.start_expires_at):
                    raise ReleaseApplyError('EFFECT_HOLD')
            with _EFFECT_MUTEX:
                if (_EFFECT_PARENTS.get(entry.owner) is not entry or entry in _EFFECT_RUNNING
                        or time.monotonic() >= min(entry.start_deadline, entry.prequiesce_deadline)):
                    raise ReleaseApplyError('EFFECT_HOLD')
                _EFFECT_RUNNING.add(entry)
        return entry
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ReleaseApplyError('EFFECT_HOLD') from None


def _finish_effect_entry(entry):
    if os.getpid() != _EFFECT_PROCESS_ID:
        return
    with _EFFECT_MUTEX:
        _EFFECT_RUNNING.discard(entry)



def _wait_effect_child(child, *, timeout):
    import owned_process
    try:
        return owned_process._wait_retired(child, timeout=timeout)
    except owned_process.ChildRetirementError:
        raise ReleaseApplyError('CHILD_HOLD') from None


def _run_effect_post_merge(entry, *, environment, timeout):
    """Issue one RAM-only native ticket and retain G/U/R through actual child retirement."""
    import owned_process
    import update_journal
    from adapters.sdc import release_state
    if (type(entry) is not _StartedEffect or entry.process_id != os.getpid()
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
            or type(environment) is not dict):
        raise ReleaseApplyError('CHILD_HOLD')
    with _EFFECT_MUTEX:
        if (_EFFECT_PARENTS.get(entry.owner) is not entry or entry not in _EFFECT_RUNNING
                or entry in _EFFECT_CHILDREN):
            raise ReleaseApplyError('CHILD_HOLD')
        _EFFECT_CHILDREN.add(entry)
    reference = entry.reference
    if (len(entry.locks) != 3 or len(entry.lock_identities) != 3
            or entry.store._release_entry_thread != threading.get_ident()
            or entry.store._release_entry_fd != entry.locks[0] or entry.store._release_entry_shared is not False
            or any(_effect_fd_identity(fd) != identity for fd, identity in zip(entry.locks, entry.lock_identities))):
        raise ReleaseApplyError('CHILD_HOLD')
    data = Path(entry.runtime_home) / 'data'
    for fd, path in zip(entry.locks, (entry.store.root / 'release-entry.lock', data / 'update.lock', data / 'run.lock')):
        _verify_inherited_lock(fd, path)
    git_environment = {'PATH': '/usr/bin:/bin', 'HOME': entry.runtime_home, 'LC_ALL': 'C',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_CONFIG_GLOBAL': '/dev/null',
        'GIT_TERMINAL_PROMPT': '0', 'GIT_ATTR_NOSYSTEM': '1', 'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_OPTIONAL_LOCKS': '0'}
    git = ['/usr/bin/git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
           '-c', 'credential.helper=', '-C', entry.repository]
    with owned_process.lifetime_fds(entry.locks):
        head = owned_process.run_owned(git + ['rev-parse', '--verify', 'HEAD'], 10, git_environment)
        clean = owned_process.run_owned(git + ['status', '--porcelain=v1', '--untracked-files=all'], 10, git_environment)
    if head.returncode != 0 or head.stdout.strip() != reference['target_commit'] or clean.returncode != 0 or clean.stdout:
        raise ReleaseApplyError('CHILD_HOLD')
    path = data / 'update_state.json'
    state = update_journal.load_state(path)
    applying = state.get('applying') or {}
    if (state.get('_corrupt') or state.get('release_ref') != reference
            or applying.get('release_ref') != reference or applying.get('sha') != reference['target_commit']
            or applying.get('reinstall') or applying.get('runtime_migration') or 'release_child' in applying
            or reference['repo_fingerprint'] != _repository_fingerprint(entry.repository)):
        raise ReleaseApplyError('CHILD_HOLD')
    with entry.store.release_owner_lock():
        owner = release_state._read(entry.store)
        unresolved = owner['state']['unresolved']
        if (not owner['spent'] or owner['spent'][-1] != entry.spent or unresolved is None
                or unresolved['operation_id'] != reference['operation_id']
                or unresolved['plan_sha256'] != reference['plan_sha256']):
            raise ReleaseApplyError('CHILD_HOLD')
    raw, receipt = _child_ticket(reference)
    applying['release_child'] = receipt
    update_journal.save_state(path, state)
    reader, writer = os.pipe()
    child = None
    try:
        if os.write(writer, raw) != len(raw):
            raise ReleaseApplyError('CHILD_HOLD')
        os.close(writer)
        writer = None
        env = dict(environment)
        env['_MCS_UPDATE_PM'] = reference['target_commit']
        with owned_process._inherited_fds((reader, *entry.locks)) as inherited:
            env[CHILD_FDS_ENV] = ':'.join(str(inherited[fd]) for fd in (reader, *entry.locks))
            env['MCS_UPDATE_RUN_LOCK_FD'] = str(inherited[entry.locks[2]])
            child = subprocess.Popen([sys.executable, str(Path(entry.repository) / 'mcs/ops/mcs_update.py'), '--post-merge'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, pass_fds=tuple(inherited.values()), close_fds=True, start_new_session=True)
        _wait_effect_child(child, timeout=timeout)
        current = update_journal.load_state(path)
        applied = (current.get('applied') or [{}])[-1]
        consumed = {**receipt, 'phase': 'consumed'}
        if (current.get('_corrupt') or current.get('release_ref') != reference or current.get('applying') is not None
                or 'done' not in [row.get('stage') for row in current.get('stages', [])]
                or applied.get('sha') != reference['target_commit'] or applied.get('release_ref') != reference
                or applied.get('release_child') != consumed):
            raise ReleaseApplyError('CHILD_HOLD')
        with _EFFECT_MUTEX:
            _EFFECT_COMPLETED[entry] = {'reference': dict(reference), 'receipt': consumed,
                'child_returncode': child.returncode, 'process_id': os.getpid()}
    finally:
        # Interruption of Popen's caller must not release the borrowed leases beside a live child.
        if child is not None and child.returncode is None:
            _wait_effect_child(child, timeout=0)
        for fd in (reader, writer):
            if fd is not None:
                os.close(fd)

def _child_ticket(reference):
    """Only bytes for the native continuation; the live owner must issue them once."""
    from update_journal import release_ref_digest
    binding = release_ref_digest(reference)
    raw = json.dumps({'schema_version': 'mcs.release-child-ticket.v1',
                      'purpose': 'post-merge.v1', 'release_ref_sha256': binding,
                      'nonce': secrets.token_hex(32)}, sort_keys=True,
                     separators=(',', ':')).encode('ascii')
    receipt = {'schema_version': 'mcs.release-child.v1', 'purpose': 'post-merge.v1',
               'release_ref_sha256': binding, 'ticket_sha256': hashlib.sha256(raw).hexdigest(),
               'phase': 'issued'}
    return raw, receipt


def _read_child_ticket(descriptor, receipt, reference, *, deadline):
    """A receipt match is necessary, never sufficient: inherited locks are checked separately."""
    from update_journal import valid_release_child
    if (not valid_release_child(receipt, reference) or receipt['phase'] != 'issued'
            or not stat.S_ISFIFO(os.fstat(descriptor).st_mode)):
        raise ReleaseApplyError('CHILD_HOLD')
    raw = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
            raise ReleaseApplyError('CHILD_HOLD')
        part = os.read(descriptor, 1025 - len(raw))
        if not part:
            break
        raw.extend(part)
        if len(raw) > 1024:
            raise ReleaseApplyError('CHILD_HOLD')
    if hashlib.sha256(raw).hexdigest() != receipt['ticket_sha256']:
        raise ReleaseApplyError('CHILD_HOLD')
    try:
        value = json.loads(bytes(raw))
        canonical = json.dumps(value, sort_keys=True, separators=(',', ':'),
                               ensure_ascii=True).encode('ascii')
        valid = (type(value) is dict and set(value) == {
            'schema_version', 'purpose', 'release_ref_sha256', 'nonce'}
            and value['schema_version'] == 'mcs.release-child-ticket.v1'
            and value['purpose'] == receipt['purpose']
            and value['release_ref_sha256'] == receipt['release_ref_sha256']
            and type(value['nonce']) is str and len(value['nonce']) == 64
            and all(c in '0123456789abcdef' for c in value['nonce']))
    except (ValueError, UnicodeError, RecursionError):
        raise ReleaseApplyError('CHILD_HOLD') from None
    if not valid or canonical != raw:
        raise ReleaseApplyError('CHILD_HOLD')


def _verify_inherited_lock(descriptor, path):
    """Prove a held exclusive OFD without unlocking or downgrading the inherited lock."""
    probe = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        held, actual = os.fstat(descriptor), os.fstat(probe)
        if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid()
                or held.st_nlink != 1 or stat.S_IMODE(held.st_mode) != 0o600
                or (held.st_dev, held.st_ino) != (actual.st_dev, actual.st_ino)):
            raise ReleaseApplyError('CHILD_HOLD')
        try:
            fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise ReleaseApplyError('CHILD_HOLD')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReleaseApplyError('CHILD_HOLD') from None
        current = os.stat(path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise ReleaseApplyError('CHILD_HOLD')
    finally:
        os.close(probe)


def _repository_fingerprint(repository):
    canonical = os.path.realpath(repository)
    descriptor = os.open(canonical, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        current = os.stat(canonical, follow_symlinks=False)
        if info.st_uid != os.getuid() or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ReleaseApplyError('REPOSITORY_CHANGED')
        raw = json.dumps({'path': canonical, 'uid': info.st_uid,
                          'device': info.st_dev, 'inode': info.st_ino},
                         sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()
    finally:
        os.close(descriptor)


def _check_git_metadata(repository, *, deadline):
    """Reject local attribute/graft overrides before any working-tree Git command."""
    import mcs_update
    import owned_process
    try:
        for name in ('info/attributes', 'info/grafts'):
            result = owned_process.run_owned(['/usr/bin/git', '-C', str(repository), 'rev-parse', '--git-path', name],
                mcs_update._bounded_timeout(10, deadline), mcs_update._release_git_environment(), deadline=deadline)
            path = result.stdout.strip()
            if result.returncode != 0 or not path or os.path.lexists(Path(repository) / path):
                raise ReleaseApplyError('GIT_METADATA_HOLD')
    except (OSError, subprocess.SubprocessError):
        raise ReleaseApplyError('GIT_METADATA_HOLD') from None


def _observe_installed(repository, *, python, venv, runtime_mode, platform, ledger_path,
                       deadline, host_source=None):
    """現在の導入物だけを読む。計画・署名認可・基線認定を生成しない。"""
    import platform as host_platform
    import math
    import sys
    import release_dependency_lock as dependencies
    import restore_evidence

    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ReleaseApplyError('OBSERVATION_TIMEOUT')

    def remaining(minimum=0):
        if deadline - time.monotonic() <= minimum:
            raise ReleaseApplyError('OBSERVATION_TIMEOUT')

    def identity(info):
        return tuple(getattr(info, field) for field in (
            'st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_size', 'st_mtime_ns'))

    def schema():
        # The existing read-only SQLite reader has a five-second busy timeout.
        remaining(5)
        sidecars = [Path(str(ledger_path) + suffix) for suffix in ('-wal', '-shm')]
        present = []
        for path in sidecars:
            try:
                info = path.lstat()
            except FileNotFoundError:
                present.append(False)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ReleaseApplyError('SCHEMA_UNOBSERVED')
            present.append(True)
        if present[0] and not present[1]:
            raise ReleaseApplyError('SCHEMA_UNOBSERVED')  # Do not create a missing WAL index.
        value = restore_evidence.database_version(str(ledger_path))
        if type(value) is not int or value < 1:
            raise ReleaseApplyError('SCHEMA_UNOBSERVED')
        return value

    ledger_fd = venv_fd = None
    try:
        remaining()
        actual_os = {'darwin': 'macos', 'linux': 'linux'}.get(sys.platform)
        actual_cpu = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'x86_64'}.get(host_platform.machine().lower())
        if (type(platform) is not dict or set(platform) != {'os', 'cpu', 'python'}
                or type(platform['python']) is not list or actual_os is None or actual_cpu is None
                or (platform.get('os'), platform.get('cpu')) != (actual_os, actual_cpu)):
            raise ReleaseApplyError('PLATFORM_MISMATCH')
        platform = {**platform, 'python': list(platform['python'])}
        repository_id = _repository_fingerprint(repository)
        _check_git_metadata(repository, deadline=deadline)
        head, clean = dependencies._git_observation(repository, deadline=deadline)
        venv_fd = os.open(venv, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fcntl.flock(venv_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        ledger_fd = os.open(ledger_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        ledger_before = os.fstat(ledger_fd)
        if not stat.S_ISREG(ledger_before.st_mode) or ledger_before.st_uid != os.getuid() or ledger_before.st_nlink != 1:
            raise ReleaseApplyError('SCHEMA_UNOBSERVED')
        installed_schema = schema()
        observed = dependencies.observe_locked(repository, python=python, venv=venv,
            runtime_mode=runtime_mode, platform=platform, host_source=host_source, deadline=deadline)
        final_head, final_clean = dependencies._git_observation(repository, deadline=deadline)
        if (repository_id != _repository_fingerprint(repository) or (head, clean) != (final_head, final_clean)
                or observed['lock_sha256'] != dependencies.digest(dependencies.read_lock(repository))
                or installed_schema != schema()
                or identity(ledger_before) != identity(os.fstat(ledger_fd))
                or identity(ledger_before) != identity(os.stat(ledger_path, follow_symlinks=False))):
            raise ReleaseApplyError('OBSERVATION_CHANGED')
        remaining()
        return {'repo_fingerprint': repository_id, 'commit': head, 'hand_edited': not clean,
                'lock_sha256': observed['lock_sha256'], 'schema': installed_schema,
                'runtime_mode': runtime_mode, 'platform': dict(platform),
                'runtime_fingerprint': observed['runtime_fingerprint'],
                'profile_id': observed['profile_id'], 'profile_sha256': observed['profile_sha256']}
    except ReleaseApplyError:
        raise
    except subprocess.TimeoutExpired:
        raise ReleaseApplyError('OBSERVATION_TIMEOUT') from None
    except dependencies.LockError:
        raise ReleaseApplyError('DEPENDENCIES_UNOBSERVED') from None
    except (OSError, ValueError, TypeError, KeyError):
        raise ReleaseApplyError('OBSERVATION_UNAVAILABLE') from None
    finally:
        if ledger_fd is not None:
            os.close(ledger_fd)
        if venv_fd is not None:
            os.close(venv_fd)


def _initial_provider_source(control, repository, *, deadline):
    """Match actual initial source bytes to an independently accepted root, never self-initialize."""
    import release_owner_source as source
    from adapters.sdc import release_state
    try:
        if (Path(__file__).resolve() != Path(repository).resolve() / 'mcs/ops/release_apply_owner.py'
                or Path(source.__file__).resolve() != Path(repository).resolve() / 'mcs/ops/release_owner_source.py'):
            raise ReleaseApplyError('PROVIDER_HOLD')
        with control.release_owner_lock():
            saved = release_state._read(control)
            if (saved['state']['unresolved'] is not None
                    or saved['repo_fingerprint'] != _repository_fingerprint(repository)):
                raise ReleaseApplyError('PROVIDER_HOLD')
        _check_git_metadata(repository, deadline=deadline)
        raw, _ = source._read(repository, source.MANIFEST_PATH, deadline)
        result = source.verify_checkout(repository, raw, deadline=deadline, current=True)
        if result['sha256'] != saved['owner_evidence_sha256']:
            raise ReleaseApplyError('PROVIDER_HOLD')
        with control.release_owner_lock():
            if release_state._read(control) != saved:
                raise ReleaseApplyError('PROVIDER_HOLD')
        return result
    except (OSError, ValueError, TypeError, KeyError):
        raise ReleaseApplyError('PROVIDER_HOLD') from None


def _observe_existing_backup(directory, *, deadline):
    """Verify and hash an existing static backup without creating or repairing one."""
    import maintenance
    import mcs_update
    directory_fd = descriptor = None
    try:
        mcs_update._bounded_timeout(None, deadline)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ReleaseApplyError('BACKUP_UNVERIFIED')
        latest = None
        with os.scandir(directory_fd) as entries:
            for count, entry in enumerate(entries):
                mcs_update._bounded_timeout(None, deadline)
                if count >= maintenance.LOCAL_STATUS_SCAN_MAX:
                    raise ReleaseApplyError('BACKUP_UNVERIFIED')
                if not (entry.name.startswith('ledger-') and entry.name.endswith('.db')):
                    continue
                current = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode):
                    raise ReleaseApplyError('BACKUP_UNVERIFIED')
                if latest is None or (current.st_mtime_ns, entry.name) > (latest[1].st_mtime_ns, latest[0]):
                    latest = (entry.name, current)
        if latest is None:
            raise ReleaseApplyError('BACKUP_UNVERIFIED')
        name, selected = latest
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        before = os.fstat(descriptor)
        def identity(value):
            return tuple(getattr(value, k) for k in ('st_dev', 'st_ino', 'st_uid', 'st_nlink', 'st_mode', 'st_size', 'st_mtime_ns'))
        if (identity(before) != identity(selected) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600):
            raise ReleaseApplyError('BACKUP_UNVERIFIED')
        for suffix in ('-wal', '-shm', '-journal'):
            try:
                os.stat(name + suffix, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ReleaseApplyError('BACKUP_UNVERIFIED')
        path = str(Path(directory) / name)
        if not maintenance._valid(path, deadline):
            raise ReleaseApplyError('BACKUP_UNVERIFIED')
        digest = hashlib.sha256()
        while part := os.read(descriptor, 65536):
            mcs_update._bounded_timeout(None, deadline)
            digest.update(part)
        if (identity(before) != identity(os.fstat(descriptor))
                or identity(before) != identity(os.stat(name, dir_fd=directory_fd, follow_symlinks=False))
                or identity(info) != identity(os.stat(directory, follow_symlinks=False))):
            raise ReleaseApplyError('BACKUP_UNVERIFIED')
        mcs_update._bounded_timeout(None, deadline)
        return {'backup_verified': True, 'backup_sha256': digest.hexdigest()}
    except (OSError, ValueError, TypeError, KeyError, mcs_update.UpdateError):
        raise ReleaseApplyError('BACKUP_UNVERIFIED') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            os.close(directory_fd)


def _native_preflight(control, *, runtime_home, repository, deadline):
    """Build preconditions from actual current code, SDK, schema, space and existing backup."""
    import platform as host_platform
    import maintenance
    import mcs_update
    import mcs_runtime
    import runtime_inspection
    from adapters.sdc import release_apply_contract, release_state
    if (os.path.realpath(runtime_home) != os.path.realpath(mcs_update.HOME)
            or os.path.realpath(repository) != os.path.realpath(mcs_update.REPO)
            or Path(mcs_update.__file__).resolve() != Path(repository).resolve() / 'mcs/ops/mcs_update.py'
            or control.root != _control_root(runtime_home)
            or os.path.realpath(maintenance.BACKUP_DIR) != os.path.realpath(Path(runtime_home) / 'data/backups')):
        raise ReleaseApplyError('PREFLIGHT_HOLD')
    observed_at = int(time.time() * 1000)
    provider = _initial_provider_source(control, repository, deadline=deadline)
    _require_settled_install_options(runtime_home, repository, deadline)
    configuration = mcs_update.mcs_util.load_config()
    python = mcs_runtime.python_executable(configuration, root=runtime_home)
    facts = runtime_inspection.probe_python(python, deadline=deadline)
    if facts is None:
        raise ReleaseApplyError('PREFLIGHT_HOLD')
    try:
        platform = {'os': {'darwin': 'macos', 'linux': 'linux'}[sys.platform],
            'cpu': {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'x86_64'}[host_platform.machine().lower()],
            'python': [int(n) for n in facts['python'].split('.')[:2]]}
        venv, host = mcs_update._runtime_venv(configuration)
        observed = _observe_installed(repository, python=python, venv=venv, runtime_mode=mcs_runtime.mode(configuration),
            platform=platform, ledger_path=mcs_update.LEDGER, deadline=deadline, host_source=host)
        backup = _observe_existing_backup(maintenance.BACKUP_DIR, deadline=deadline)
        filesystem = os.statvfs(runtime_home)
        preconditions = dict(current_commit=observed['commit'], hand_edited=observed['hand_edited'],
            installed_lock_sha256=observed['lock_sha256'], free_bytes=filesystem.f_bavail * filesystem.f_frsize,
            platform=observed['platform'], updater=list(provider['provider_profile']), installed_schema=observed['schema'],
            **backup, observed_at=observed_at)
        with control.release_owner_lock():
            owner = release_state._read(control)
            baseline = owner['baseline']
            if (owner['state']['unresolved'] is not None or owner['repo_fingerprint'] != observed['repo_fingerprint']
                    or any(baseline[k] != observed[key] for k, key in (
                        ('commit', 'commit'), ('lock_sha256', 'lock_sha256'), ('schema', 'schema'),
                        ('profile_id', 'profile_id'), ('profile_sha256', 'profile_sha256'), ('runtime_fingerprint', 'runtime_fingerprint')))):
                raise ReleaseApplyError('PREFLIGHT_HOLD')
        if mcs_update.mcs_util.load_config() != configuration:
            raise ReleaseApplyError('PREFLIGHT_HOLD')
        mcs_update._bounded_timeout(None, deadline)
        return {'preconditions': release_apply_contract.validate_apply_preconditions(preconditions),
            'observed': observed, 'configuration': configuration, 'source_sha256': provider['sha256']}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, mcs_update.UpdateError):
        raise ReleaseApplyError('PREFLIGHT_HOLD') from None


def _adopt_post_merge(runtime_home, repository, *, ticket_fd, locks, deadline):
    """Consume only an already-issued native continuation; never mint a release owner."""
    import update_journal
    from adapters.sdc import release_state
    from adapters.sdc.storage import Store
    if (type(locks) is not tuple or len(locks) != 3
            or any(type(fd) is not int or fd < 0 for fd in (*locks, ticket_fd))
            or len(set((*locks, ticket_fd))) != 4):
        raise ReleaseApplyError('CHILD_HOLD')
    root = _control_root(runtime_home)
    data = Path(runtime_home) / 'data'
    paths = (root / 'release-entry.lock', data / 'update.lock', data / 'run.lock')

    def verify_locks():
        for descriptor, path in zip(locks, paths):
            _verify_inherited_lock(descriptor, path)

    verify_locks()
    # No shared G acquisition here: the native child inherits its parent's exclusive OFD.
    with Store(root) as store, store.release_owner_lock():
        owner = release_state._read(store)
        state = update_journal.load_state(data / 'update_state.json')
        applying = state.get('applying') or {}
        reference, receipt = applying.get('release_ref'), applying.get('release_child')
        if (state.get('_corrupt') or not update_journal.valid_release_ref(reference)
                or state.get('release_ref') != reference or not owner['spent']
                or applying.get('sha') != reference['target_commit']
                or applying.get('reinstall') or applying.get('runtime_migration')
                or reference['repo_fingerprint'] != _repository_fingerprint(repository)):
            raise ReleaseApplyError('CHILD_HOLD')
        spent = owner['spent'][-1]
        expected = {'root_fingerprint': owner['root_fingerprint'],
                    'repo_fingerprint': owner['repo_fingerprint'],
                    'operation_id': spent['operation']['operation_id'],
                    'grant_id': spent['grant']['grant_id'], 'plan_sha256': spent['operation']['plan_hash'],
                    'release_id': spent['operation']['release_id'],
                    'target_code_seq': spent['operation']['target_code_seq'],
                    'start_receipt_id': spent['start_receipt_id'], 'started_at': spent['started_at']}
        gate = os.fstat(locks[0])
        gate_fingerprint = store._fingerprint({'uid': gate.st_uid, 'device': gate.st_dev, 'inode': gate.st_ino})
        unresolved = owner['state']['unresolved']
        if (any(reference[key] != value for key, value in expected.items())
                or owner['entry_lock_fingerprint'] != gate_fingerprint
                or unresolved is None or unresolved['operation_id'] != reference['operation_id']
                or unresolved['plan_sha256'] != reference['plan_sha256']):
            raise ReleaseApplyError('CHILD_HOLD')
        _read_child_ticket(ticket_fd, receipt, reference, deadline=min(deadline, time.monotonic() + 2))
        verify_locks()
        if release_state._read(store) != owner:
            raise ReleaseApplyError('CHILD_HOLD')
        applying['release_child'] = {**receipt, 'phase': 'consumed'}
        # A failed/uncertain publish is terminal for this child; never retry the body or legacy route.
        update_journal.save_state(data / 'update_state.json', state)
        verify_locks()
        if release_state._read(store) != owner:
            raise ReleaseApplyError('CHILD_HOLD')
        if time.monotonic() >= deadline:
            raise ReleaseApplyError('CHILD_HOLD')
        return state


def _control_root(runtime_home):
    """Copied into the independent 3.9/3.10 closures; common vectors pin every byte."""
    canonical = os.path.realpath(os.path.abspath(os.path.expanduser(runtime_home)))
    namespace = hashlib.sha256(os.fsencode(canonical)).hexdigest()
    return Path(os.path.realpath(os.path.expanduser('~'))) / '.mcs-release-control' / namespace


@contextmanager
def _control_store(runtime_home):
    from adapters.sdc.storage import Store
    root = _control_root(runtime_home)
    # Store validates owner/private mode/nofollow and only creates the final directory.
    with Store(root.parent, create=True):
        pass
    with Store(root, create=True) as store:
        yield store


def _open_release_gate(runtime_home):
    root = _control_root(runtime_home)
    directory, held = os.open('/', os.O_RDONLY | os.O_DIRECTORY), None
    try:
        for index, part in enumerate(root.parts[1:]):
            private = index >= len(root.parts) - 3
            if private:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory)
                    os.fsync(directory)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError('release_entry_hold')
        held = os.open('release-entry.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=directory)
        info = os.fstat(held)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_size != 0 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('release_entry_hold')
        fcntl.flock(held, fcntl.LOCK_SH | fcntl.LOCK_NB)
        for name in ('release-owner.json', 'release-owner-activation.json'):
            try:
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError('release_entry_hold')
        result, held = held, None
        return result
    finally:
        if held is not None:
            os.close(held)
        os.close(directory)



@contextmanager
def _legacy_scope(runtime_home):
    """Stdlib-only so a target-tag bootstrap never imports adapter or SDK code."""
    fd = _open_release_gate(runtime_home)
    try:
        yield fd
    finally:
        os.close(fd)


def _git_validation(directory, pack_path, size, deadline):
    from adapters.sdc.release_artifact import _check, _time
    git_dir, empty = directory / 'inspect.git', directory / 'empty'
    empty.mkdir()
    environment = {'PATH': '/usr/bin:/bin', 'HOME': str(directory), 'LC_ALL': 'C',
                   'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': '/dev/null',
                   'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
                   'GIT_ATTR_NOSYSTEM': '1', 'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_OPTIONAL_LOCKS': '0'}
    common = ['/usr/bin/git', '-c', 'protocol.allow=never', '-c', f'core.hooksPath={empty}',
              '-c', 'core.attributesFile=/dev/null', '-c', 'credential.helper=',
              '-c', 'pack.threads=1', '-c', 'core.commitGraph=false']
    for command in (['init', '--bare', '--object-format=sha1', f'--template={empty}', str(git_dir)],
                    [f'--git-dir={git_dir}', 'index-pack', '--strict', '--threads=1', '--index-version=2',
                     '--no-rev-index', f'--max-input-size={size}', str(pack_path)]):
        _time(deadline)
        child = subprocess.Popen(common + command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env=environment, close_fds=True, start_new_session=True)
        try:
            _check(child.wait(timeout=max(0, deadline - time.monotonic())) == 0, 'GIT_VALIDATION_FAILED')
        except BaseException:
            if child.returncode is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # Keep staging owned until the direct child really retires, even on interruption.
                while child.returncode is None:
                    try:
                        child.wait()
                    except (KeyboardInterrupt, InterruptedError):
                        pass
            raise


@contextmanager
def _inspect_artifact(source, **parameters):
    """Adapterの純粋pack/tree検査と、trusted ownerのGit意味検証を一つに束ねる。"""
    from adapters.sdc import release_artifact as artifact
    from adapters.sdc.release_artifact import _time
    with artifact._inspect_verified(source, **parameters) as result:
        # Native lock validation stays out of the independently importable pure adapter.
        from release_dependency_lock import LockError, digest, hermes_pin, validate
        try:
            lock = validate(result['lock_raw'])
            profiles = [profile for profile in lock['profiles'] if profile['profile_id'] == parameters['profile_id']]
            artifact._check(len(profiles) == 1 and digest(profiles[0]) == parameters['profile_sha256'],
                            'PROFILE_CHANGED')
            profile = profiles[0]
            name = ('deployment/requirements-standalone.txt' if profile['runtime_mode'] == 'standalone'
                    else 'install.sh')
            declared = result['profile_sources'][name]
            artifact._check(declared is not None and declared['size'] <= 1024 * 1024, 'PROFILE_SOURCE_MISSING')
            raw = declared['path'].read_bytes()
            valid = (hashlib.sha256(raw).hexdigest() == profile['requirements_sha256']
                     if profile['runtime_mode'] == 'standalone' else hermes_pin(raw) == profile['host']['commit'])
            artifact._check(valid, 'PROFILE_SOURCE_CHANGED')
        except (LockError, OSError):
            raise artifact.ArtifactError('INSPECTION_FAILED') from None
        try:
            _git_validation(result['bundle'].parent, result['pack'], result['pack_size'], parameters['deadline'])
        except (OSError, subprocess.SubprocessError):
            raise artifact.ArtifactError('GIT_VALIDATION_FAILED') from None
        _time(parameters['deadline'])
        result.pop('profile_sources')
        yield result
