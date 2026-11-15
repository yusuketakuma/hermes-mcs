"""既存updater内でreleaseの子processと一回実行境界を所有する。"""
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import threading

from release_apply_checks import (
    ReleaseApplyError as ReleaseApplyError,
    _require_settled_install_options as _require_settled_install_options,
    _child_ticket as _child_ticket,
    _read_child_ticket as _read_child_ticket,
    _verify_inherited_lock as _verify_inherited_lock,
    _repository_fingerprint as _repository_fingerprint,
    _check_git_metadata as _check_git_metadata,
    _observe_installed as _observe_installed,
    _initial_provider_source as _initial_provider_source,
    _observe_existing_backup as _observe_existing_backup,
    _native_preflight as _native_preflight,
    _adopt_post_merge as _adopt_post_merge,
    _control_root as _control_root,
    _control_store as _control_store,
    _open_release_gate as _open_release_gate,
    _legacy_scope as _legacy_scope,
    _git_validation as _git_validation,
    _inspect_artifact as _inspect_artifact,
)

from contextlib import contextmanager as contextmanager
import fcntl as fcntl
import signal as signal

CHILD_FDS_ENV = 'MCS_RELEASE_CHILD_FDS'


class _EffectCapability:
    __slots__ = ()


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


@dataclass(frozen=True, repr=False)
class _PreparedApplyControl:
    endpoint: str
    path: str
    body: bytes
    headers: tuple
    binding: bytes
    deadline: float


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
