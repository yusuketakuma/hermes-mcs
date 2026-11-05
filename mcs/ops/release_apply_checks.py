"""Verify release tickets, inherited locks and installed source evidence before native apply."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import secrets
import select
import stat
import subprocess
import sys
import time

class ReleaseApplyError(ValueError):
    def __init__(self, code='HOLD'):
        self.code = 'RELEASE_APPLY_' + code
        super().__init__(self.code)


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
    import math  # noqa: F401 - keep the moved function body unchanged
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
    import release_apply_owner as entry
    from adapters.sdc import release_state
    try:
        if (Path(__file__).resolve() != Path(repository).resolve() / 'mcs/ops/release_apply_checks.py'
                or Path(entry.__file__).resolve() != Path(repository).resolve() / 'mcs/ops/release_apply_owner.py'
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
