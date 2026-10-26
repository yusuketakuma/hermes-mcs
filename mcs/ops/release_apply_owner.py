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
import time
import threading

CHILD_FDS_ENV = 'MCS_RELEASE_CHILD_FDS'



class ReleaseApplyError(ValueError):
    def __init__(self, code='HOLD'):
        self.code = 'RELEASE_APPLY_' + code
        super().__init__(self.code)


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


# Constructors never publish authority. Only the verified live factory may populate these.
_EFFECT_MUTEX = threading.Lock()
_EFFECT_PROCESS_ID = os.getpid()
_EFFECT_CAPS = {}
_EFFECT_PARENTS = {}
_EFFECT_CLAIMED = set()
_EFFECT_RUNNING = set()


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
