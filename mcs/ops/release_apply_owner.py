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


def _tty_planning_confirmation(installation, *, deadline):
    """Read the controlling terminal directly; stdin and caller booleans cannot approve."""
    from adapters.sdc import release_contract
    if (not release_contract.slug(installation) or type(deadline) not in (int, float)
            or not math.isfinite(deadline) or time.monotonic() >= deadline):
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
        os.write(descriptor, ('端末 ' + installation + ' の更新計画を確認します。\n'
            '中央の対象版と承認状態を取得し、現在の実装・容量・既存backupを検査します。\n'
            '1: 計画の確認を許可する\n2: 中止する（既定）\n番号: ').encode())
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
