"""導入時の省略項目を旧記録と照合し、設定に保存する。"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _mcs_path  # noqa: E402,F401
import settings_migration_receipt  # noqa: E402
from install_options_contract import valid  # noqa: E402

KEY = 'install_args'


def _unique_pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('duplicate_key')
        value[key] = item
    return value


def _reject_constant(_value):
    raise ValueError('invalid_number')


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('invalid_number')
    return result


def _parse_json(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_pairs,
                      parse_constant=_reject_constant, parse_float=_finite_float)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _read(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if os.path.lexists(path):
            raise ValueError('unreadable') from None
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('unreadable')
        raw = stream.read(limit + 1)
        if (len(raw) > limit or _identity(os.fstat(stream.fileno())) != _identity(info)
                or _identity(os.lstat(path)) != _identity(info)):
            raise ValueError('changed')
    return raw


def _legacy(path):
    raw = _read(path, 65536)
    if raw is None:
        return None, None
    obj = _parse_json(raw)
    if (not isinstance(obj, dict) or not valid(obj.get(KEY))
            or ('v' in obj and (type(obj['v']) is not int or obj['v'] != 1))):
        raise ValueError('legacy_invalid')
    return sorted(set(obj[KEY])), hashlib.sha256(raw).hexdigest()


def _central(path):
    # The installer can use macOS system Python 3.9 before its venv exists.
    raw = _read(path, 1024 * 1024)
    if raw is None:
        return None, None
    cfg = _parse_json(raw)
    if not isinstance(cfg, dict):
        raise ValueError('config_unreadable')
    value = cfg.get(KEY)
    if KEY in cfg and not valid(value):
        raise ValueError('config_invalid')
    return (sorted(set(value)) if KEY in cfg else None), None


def _provider_preflight(config_path, legacy_path):
    root = Path(os.path.abspath(config_path)).parent
    if (Path(os.path.abspath(config_path)) != root / 'config.json'
            or Path(os.path.abspath(legacy_path)) != root / 'data' / 'install_options.json'):
        return
    import provider_settings

    state = provider_settings.probe(root)['state']
    if state == 'pending':
        raise ValueError('provider_settings_pending')
    if state == 'requires_checked_read':
        import cloud_policy
        import semantic_jev

        owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
        checked = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
        if checked['mode'] != 'split':
            raise ValueError('provider_settings_unverified')


def inspect(config_path, legacy_path, desired=None):
    _provider_preflight(config_path, legacy_path)
    central, _ = _central(config_path)
    legacy, digest = _legacy(legacy_path)
    if central is not None and legacy is not None and central != legacy:
        raise ValueError('record_conflict')
    if desired is not None:
        if not valid(desired):
            raise ValueError('options_invalid')
        desired = sorted(set(desired))
        if legacy is not None and desired != legacy:
            raise ValueError('legacy_rollback_conflict')
    if central is None and legacy is None:
        return 'empty'
    state = json.dumps([central, legacy, digest], separators=(',', ':')).encode()
    return hashlib.sha256(state).hexdigest()


def selection(config_path, legacy_path):
    central, _ = _central(config_path)
    legacy, _ = _legacy(legacy_path)
    if central is not None and legacy is not None and central != legacy:
        raise ValueError('record_conflict')
    return central if central is not None else legacy


def recorded(config_path, legacy_path):
    try:
        return selection(config_path, legacy_path)
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def save(config_path, legacy_path, desired, expected, *, check_pending=False):
    import config_edit
    import mcs_util
    import safe_io
    publication_started = False
    try:
        with config_edit._edit_lock(config_path):
            if check_pending and settings_migration_receipt.pending(Path(legacy_path).parent):
                raise config_edit.ConfigConflict('migration_pending')
            if inspect(config_path, legacy_path, desired) != expected:
                raise config_edit.ConfigConflict('options_changed')
            if desired is None:
                central, _ = _central(config_path)
                legacy, _ = _legacy(legacy_path)
                if central is not None or legacy is None:
                    return 0
                desired = legacy
            raw, base = config_edit._snapshot(config_path)
            cfg = config_edit._parse(raw)
            if os.lstat(config_path).st_uid != os.getuid():
                raise ValueError('config_owner')
            if cfg.get(KEY) == sorted(set(desired)):
                return 0
            cfg[KEY] = sorted(set(desired))
            body = json.dumps(cfg, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
            if len(body.encode('utf-8')) > mcs_util.CONFIG_MAX_BYTES:
                raise ValueError('config_too_large')
            def write_checked(stream):
                stream.write(body)
                if (config_edit._snapshot(config_path)[1] != base
                        or (check_pending and settings_migration_receipt.pending(Path(legacy_path).parent))
                        or inspect(config_path, legacy_path, desired) != expected):
                    raise config_edit.ConfigConflict('options_changed')
            publication_started = True
            safe_io.atomic_write(config_path, write_checked, 0o600,
                                 tmp_prefix='config.json.stage-', makedirs=False)
    except config_edit.ConfigConflict:
        print('導入の省略項目が変更されたか、別の設定保存が進行中です。現在の状態を確認してください。', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if publication_started:
            print('導入の省略項目の保存結果を確認できません。現在の設定を確認してください。', file=sys.stderr)
        else:
            print('導入の省略項目は保存していません。現在の状態を確認してください。', file=sys.stderr)
        return 130
    except (OSError, ValueError, TypeError, RecursionError):
        if publication_started:
            print('導入の省略項目の保存結果を確認できません。現在の設定を確認してください。', file=sys.stderr)
        else:
            print('導入の省略項目を確認できないため保存していません。旧記録と設定を確認してください。', file=sys.stderr)
        return 1
    return 0


def _fresh_cron_absent():
    """Unknown or matching Hermes schedules never prove a fresh install."""
    import service_management
    import scheduled_jobs

    mode = os.environ.get('MCS_FRESH_RUNTIME_MODE', 'hermes')
    home_name = os.environ.get('MCS_FRESH_HERMES_HOME', '')
    executable = os.environ.get('MCS_FRESH_HERMES_EXE', '')
    if mode not in ('hermes', 'standalone') or not os.path.isabs(home_name):
        return False
    home = Path(home_name)
    profiles = home / 'profiles'
    if os.path.lexists(profiles):
        if profiles.is_symlink() or not profiles.is_dir() or any(os.scandir(profiles)):
            return False  # Existing contract inventories only the default profile.
    candidates = [home / 'hermes-agent' / 'venv' / 'bin' / 'hermes', Path.home() / '.local' / 'bin' / 'hermes']
    if executable and os.path.isabs(executable) and mode == 'hermes':
        candidates.insert(0, Path(executable))
    present = list(dict.fromkeys(path for path in candidates if os.path.lexists(path)))
    if not present:
        return mode == 'standalone' and not os.path.lexists(home)
    scripts = service_management._known_cron_scripts()
    names = {name for name, _, _ in scheduled_jobs.CRON_JOBS} | {'MCS offsite backup'}
    for path in present:
        entries = service_management.inspect_cron(str(path))
        if entries is None:
            return False
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get('script'), str)
                    or not isinstance(entry.get('name'), str)):
                return False
            if Path(entry['script']).name in scripts or entry['name'] in names:
                return False
    return True


def _fresh_workers_absent():
    """Require a successful, bounded inventory of known owned jobs and workers."""
    import subprocess

    try:
        result = subprocess.run(['/bin/ps', '-axo', 'pid=,args='], capture_output=True, text=True,
                                timeout=10, check=False)
        if result.returncode != 0:
            return False
        markers = ('mcs_standalone', 'lineworks_adapter', 'extract_llm.py', 'mcs_adapter.py',
                   'mcs_recover.py', 'mcs_update.py', '/mcs/ingest/', '/mcs/semantic/',
                   '/mcs/notify/', '/mcs/extract/', '/mcs/patients/', '/mcs/stamps/', '/mcs/ops/',
                   'mcs_check.sh', 'mcs_health.sh', 'mcs_deep.sh', 'mcs_llm_catchup.sh',
                   'mcs_update.sh', 'mcs_offsite.sh', 'mcs-cmd', 'mcs-int', 'mcs-health')
        for line in result.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2 or not fields[0].isdigit():
                return False
            if int(fields[0]) != os.getpid() and any(marker in fields[1] for marker in markers):
                return False
        home = Path.home()
        if sys.platform == 'darwin':
            agents = home / 'Library' / 'LaunchAgents'
            if agents.exists() or agents.is_symlink():
                if agents.is_symlink() or not agents.is_dir():
                    return False
                for entry in os.scandir(agents):
                    if entry.name.startswith(('ai.mcs.', 'local.mcs-', 'org.mcs.recovery')):
                        return False
            result = subprocess.run(['/bin/launchctl', 'list'], capture_output=True, text=True,
                                    timeout=10, check=False)
            if result.returncode != 0:
                return False
            lines = result.stdout.splitlines()
            if not lines or lines[0].split() != ['PID', 'Status', 'Label']:
                return False
            for line in lines[1:]:
                fields = line.split()
                if len(fields) != 3:
                    return False
                if fields[2].startswith(('ai.mcs.', 'local.mcs-', 'org.mcs.recovery')):
                    return False
        elif sys.platform.startswith('linux'):
            units = home / '.config' / 'systemd' / 'user'
            if units.exists() or units.is_symlink():
                if units.is_symlink() or not units.is_dir():
                    return False
                for entry in os.scandir(units):
                    if entry.name.startswith(('mcs-', 'mcs.')):
                        return False
            result = subprocess.run(['/usr/bin/systemctl', '--user', 'list-units', '--all', '--plain',
                                     '--no-legend'], capture_output=True, text=True, timeout=10, check=False)
            if result.returncode != 0:
                return False
            for line in result.stdout.splitlines():
                fields = line.split()
                if not fields:
                    continue
                if fields[0] == '●':
                    fields = fields[1:]
                if not fields or fields[0].startswith(('mcs-', 'mcs.')):
                    return False
        else:
            return False
        return True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _fresh_update_lock(root, lock_fd):
    import fcntl

    if type(lock_fd) is not int or lock_fd < 3:
        raise ValueError('fresh_lock_invalid')
    path = root / 'data' / 'update.lock'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        held, actual = os.fstat(lock_fd), os.fstat(fd)
        if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid() or held.st_nlink != 1
                or stat.S_IMODE(held.st_mode) != 0o600 or held.st_size != 0
                or (held.st_dev, held.st_ino) != (actual.st_dev, actual.st_ino)):
            raise ValueError('fresh_lock_invalid')
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise ValueError('fresh_lock_unheld')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = os.stat(path, follow_symlinks=False)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError('fresh_lock_changed')
    finally:
        os.close(fd)


_FRESH_MARKER = '.provider-bootstrap-fresh.json'
_FRESH_COMPLETE = '.provider-bootstrap-complete.json'
_FRESH_FORBIDDEN = ('.env', 'chrome-profile', 'token_cache.json', 'backups', 'exports', 'mcs-exports',
                    '.install-options-migration.pending')


def _fresh_anchor(path, *, private=False):
    info = os.stat(path, follow_symlinks=False)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022 or private and stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError('fresh_directory_unverified')
    return [info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)]


def _fresh_root_clean(root):
    for entry in os.scandir(root):
        if entry.name.startswith(('.env.', '.env-')) and entry.name not in ('.env.example', '.env.template'):
            raise ValueError('fresh_prior_state_present')
    for name in _FRESH_FORBIDDEN:
        try:
            os.stat(root / name, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise ValueError('fresh_prior_state_present')


def _fresh_marker_read(root, name=_FRESH_MARKER):
    import safe_io
    if name not in (_FRESH_MARKER, _FRESH_COMPLETE):
        raise ValueError('fresh_marker_invalid')
    path = root / 'data' / name
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        with safe_io.fdopen_owned(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > 4096):
                raise ValueError('fresh_marker_invalid')
            raw = stream.read(4097)
            after = os.fstat(stream.fileno())
            named = os.stat(path, follow_symlinks=False)
            if len(raw) > 4096 or _identity(before) != _identity(after) or _identity(after) != _identity(named):
                raise ValueError('fresh_marker_changed')
    except BaseException:
        raise
    value = _parse_json(raw)
    if (not isinstance(value, dict) or set(value) != {'version', 'root', 'data', 'config'}
            or type(value['version']) is not int or value['version'] != 1 or value['config'] is not None
            or value['root'] != _fresh_anchor(root)
            or value['data'] != _fresh_anchor(root / 'data', private=True)):
        raise ValueError('fresh_marker_invalid')
    return value, raw, _identity(named)


def fresh_marker_init(root, data_token, update_fd):
    import safe_io
    root = Path(os.path.abspath(root))
    _fresh_root_clean(root)
    _fresh_update_lock(root, int(update_fd))
    if os.path.lexists(root / 'config.json') or set(os.listdir(root / 'data')) != {'update.lock'}:
        raise ValueError('fresh_initial_state_invalid')
    data_info = os.stat(root / 'data', follow_symlinks=False)
    expected = f'{data_info.st_dev} {data_info.st_ino} {data_info.st_uid} {stat.S_IMODE(data_info.st_mode):o} {data_info.st_nlink}'
    # mkdir proof was captured before update.lock was created; ignore the
    # directory link count, which is not an identity of the directory.
    if data_token.split()[:4] != expected.split()[:4]:
        raise ValueError('fresh_data_changed')
    marker = {'version': 1, 'root': _fresh_anchor(root), 'data': _fresh_anchor(root / 'data', private=True), 'config': None}
    path = root / 'data' / _FRESH_MARKER
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with safe_io.fdopen_owned(fd, 'wb') as stream:
        stream.write((json.dumps(marker, sort_keys=True) + '\n').encode())
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(root / 'data', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return marker


def _fresh_inventory(root, data_token, lock_fd):
    values = data_token.split()
    if len(values) != 5 or any(not value.isdigit() for value in values):
        raise ValueError('fresh_data_invalid')
    device, inode, uid, mode, _ = (int(value) for value in values)
    directory = os.open(root / 'data', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(directory)
        current = os.stat(root / 'data', follow_symlinks=False)
        def anchor(info):
            return (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))
        if (anchor(before) != (device, inode, uid, int(str(mode), 8))
                or uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o700
                or anchor(before) != anchor(current) or set(os.listdir(directory)) != {'update.lock', _FRESH_MARKER}):
            raise ValueError('fresh_data_unverified')
        _fresh_update_lock(root, lock_fd)
        _fresh_marker_read(root)
        _fresh_root_clean(root)
        for name in ('.install-options-migration.pending',):
            try:
                os.stat(root / name, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError('fresh_migration_present')
        if not _fresh_workers_absent() or not _fresh_cron_absent():
            raise ValueError('fresh_workers_unverified')
        after = os.fstat(directory)
        named = os.stat(root / 'data', follow_symlinks=False)
        if (anchor(after) != anchor(before) or anchor(named) != anchor(before)
                or _identity(after) != _identity(before) or _identity(named) != _identity(after)
                or set(os.listdir(directory)) != {'update.lock', _FRESH_MARKER}):
            raise ValueError('fresh_data_changed')
        _fresh_update_lock(root, lock_fd)
    finally:
        os.close(directory)


def bootstrap_fresh(config_path, legacy_path, proof, data_token, update_fd):
    """Bootstrap only the installer-created config and its new, isolated data root."""
    import config_edit
    import cloud_policy
    import provider_settings
    import semantic_jev

    root = Path(os.path.abspath(config_path)).parent
    if (Path(os.path.abspath(config_path)) != root / 'config.json'
            or Path(os.path.abspath(legacy_path)) != root / 'data' / 'install_options.json'):
        raise ValueError('fresh_path_invalid')
    try:
        expected = _parse_json(proof.encode('utf-8'))
        if (not isinstance(expected, dict) or set(expected) != {'identity', 'sha256'}
                or not isinstance(expected['identity'], list) or len(expected['identity']) != 8
                or any(type(value) is not int for value in expected['identity'])
                or not isinstance(expected['sha256'], str) or len(expected['sha256']) != 64):
            raise ValueError('fresh_config_invalid')
        fd = int(update_fd)
        _fresh_inventory(root, data_token, fd)
        with config_edit._edit_lock(root / 'config.json') as edit_fd:
            owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
            before = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
            snap = before['snapshot']['files']['config']
            if (before['mode'] != 'legacy_unverified' or not isinstance(snap, dict)
                    or list(snap['identity']) != expected['identity'] or snap['sha256'] != expected['sha256']):
                raise ValueError('fresh_config_changed')
            _fresh_inventory(root, data_token, fd)
            provider_settings.bootstrap_locked(root, before['snapshot'], before['common'],
                                               model_owners=owners, lock_fd=edit_fd,
                                               jev_model=semantic_jev.JEV_MODEL)
            checked = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
            if checked['mode'] != 'split':
                raise ValueError('fresh_readback_unverified')
            marker, raw, identity = _fresh_marker_read(root)
            if marker['config'] is not None:
                raise ValueError('fresh_marker_invalid')
            _, again, current = _fresh_marker_read(root)
            if (again, current) != (raw, identity):
                raise ValueError('fresh_marker_changed')
            # Keep either the pending name or a verified completion name at
            # every crash boundary. Never replace an existing completion file.
            directory = os.open(root / 'data', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.link(_FRESH_MARKER, _FRESH_COMPLETE, src_dir_fd=directory, dst_dir_fd=directory,
                        follow_symlinks=False)
                os.fsync(directory)
                os.unlink(_FRESH_MARKER, dir_fd=directory)
                os.fsync(directory)
                _fresh_marker_read(root, _FRESH_COMPLETE)
                os.unlink(_FRESH_COMPLETE, dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(directory)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, ImportError) as error:
        raise ValueError('fresh_bootstrap_unverified') from error
    return 0


def fresh_complete(root, update_fd):
    """Resolve only a durable completion witness after strict split readback."""
    import cloud_policy
    import provider_settings
    import semantic_jev
    root = Path(os.path.abspath(root))
    _fresh_update_lock(root, int(update_fd))
    if os.path.lexists(root / 'data' / _FRESH_MARKER):
        raise ValueError('fresh_pending')
    _, raw, identity = _fresh_marker_read(root, _FRESH_COMPLETE)
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    first = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
    if first['mode'] != 'split':
        raise ValueError('fresh_readback_unverified')
    second = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
    _, now_raw, now_identity = _fresh_marker_read(root, _FRESH_COMPLETE)
    if second['mode'] != 'split' or first['snapshot'] != second['snapshot'] or (raw, identity) != (now_raw, now_identity):
        raise ValueError('fresh_readback_changed')
    directory = os.open(root / 'data', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.unlink(_FRESH_COMPLETE, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


def require_legacy_runtime_edit(config_path, legacy_path):
    """Reject raw runtime edits unless the checked installation is still legacy."""
    import provider_settings
    root = Path(os.path.abspath(config_path)).parent
    if (Path(os.path.abspath(config_path)) != root / 'config.json'
            or Path(os.path.abspath(legacy_path)) != root / 'data' / 'install_options.json'
            or settings_migration_receipt.pending(root / 'data')):
        raise ValueError('runtime_edit_unverified')
    state = provider_settings.probe(root)
    if state['state'] != 'legacy_unverified' or not state['config_present']:
        raise ValueError('runtime_edit_unavailable')
    if provider_settings.read(root)['mode'] != 'legacy_unverified':
        raise ValueError('runtime_edit_unavailable')


def require_uninitialized_legacy(config_path, legacy_path, update_fd, *, allow_config=False):
    """Require an existing data directory with no provider migration evidence."""
    import provider_settings

    root = Path(os.path.abspath(config_path)).parent
    if (Path(os.path.abspath(config_path)) != root / 'config.json'
            or Path(os.path.abspath(legacy_path)) != root / 'data' / 'install_options.json'
            or settings_migration_receipt.pending(root / 'data')):
        raise ValueError('legacy_install_unverified')
    _fresh_update_lock(root, int(update_fd))
    state = provider_settings.probe(root)
    permitted = ('uninitialized_or_legacy_unverified', 'legacy_unverified') if allow_config else (
        'uninitialized_or_legacy_unverified',)
    if state['state'] not in permitted or (state['config_present'] and not allow_config):
        raise ValueError('legacy_install_unverified')
    _fresh_update_lock(root, int(update_fd))


def _runtime_snapshot_token(snapshot):
    evidence = {'files': snapshot['files'], 'authority': snapshot['authority'],
                'pending': snapshot['pending'], 'root': snapshot['root'][:4],
                'root_link': snapshot.get('root_link'), 'data': snapshot['data'][:4],
                'provider_settings': snapshot['provider_settings'][:4]}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _runtime_base(snapshot):
    info = snapshot['files']['config']
    identity = info['identity']
    return ':'.join(str(identity[index]) for index in (0, 1, 2, 5, 6, 7)) + ':' + info['sha256']


def _bootstrap_runtime_read(config_path, legacy_path):
    import cloud_policy
    import provider_settings
    import semantic_jev

    root = Path(os.path.abspath(config_path)).parent
    if (Path(os.path.abspath(config_path)) != root / 'config.json'
            or Path(os.path.abspath(legacy_path)) != root / 'data' / 'install_options.json'
            or settings_migration_receipt.pending(root / 'data')):
        raise ValueError('runtime_edit_unverified')
    state = provider_settings.probe(root)
    if state['state'] == 'legacy_unverified':
        return root, None, None
    if state['state'] != 'requires_checked_read':
        raise ValueError('runtime_edit_unverified')
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    checked = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
    if checked['mode'] != 'split' or settings_migration_receipt.pending(root / 'data'):
        raise ValueError('runtime_edit_unverified')
    if checked.get('configuration_state') != 'bootstrap':
        return root, None, None
    for name in (_FRESH_MARKER, _FRESH_COMPLETE):
        if os.path.lexists(root / 'data' / name):
            raise ValueError('runtime_edit_unverified')
    if any(checked['providers'][name] != {} for name in provider_settings.PROVIDERS):
        raise ValueError('runtime_edit_unverified')
    return root, owners, checked


def bootstrap_runtime_inspect(config_path, legacy_path, config_base):
    _, _, checked = _bootstrap_runtime_read(config_path, legacy_path)
    if checked is None:
        return 'none'
    if _runtime_base(checked['snapshot']) != config_base:
        raise ValueError('runtime_snapshot_changed')
    return 'bootstrap:' + _runtime_snapshot_token(checked['snapshot'])


def bootstrap_runtime_apply(config_path, legacy_path, expected, mode_flag, requested_mode, recovery):
    import config_edit
    import provider_settings
    import semantic_jev

    if (not isinstance(expected, str) or not expected.startswith('bootstrap:')
            or mode_flag not in ('0', '1') or requested_mode not in ('hermes', 'standalone')
            or not (mode_flag == '1' or recovery)):
        raise ValueError('runtime_edit_unverified')
    root, _, first = _bootstrap_runtime_read(config_path, legacy_path)
    if first is None or 'bootstrap:' + _runtime_snapshot_token(first['snapshot']) != expected:
        raise ValueError('runtime_snapshot_changed')
    changes = {}
    if mode_flag == '1':
        changes['runtime_mode'] = requested_mode
    if recovery:
        changes['recovery_python'] = recovery
    with config_edit._edit_lock(root / 'config.json') as lock_fd:
        _, owners, current = _bootstrap_runtime_read(config_path, legacy_path)
        if current is None or 'bootstrap:' + _runtime_snapshot_token(current['snapshot']) != expected:
            raise ValueError('runtime_snapshot_changed')
        final = provider_settings.bootstrap_runtime_locked(
            root, current['snapshot'], changes, model_owners=owners, lock_fd=lock_fd,
            jev_model=semantic_jev.JEV_MODEL)
    if final.get('configuration_state') != 'bootstrap':
        raise ValueError('runtime_edit_unverified')
    return _runtime_base(final['snapshot']) + ' split:' + _runtime_snapshot_token(final['snapshot'])


def runtime_edit_decision(config_path, legacy_path, config_base, mode_flag, requested_mode, recovery):
    """Allow a split installer's explicit selections only when raw values match."""
    import cloud_policy
    import provider_settings
    import semantic_jev

    root = Path(os.path.abspath(config_path)).parent
    if (Path(os.path.abspath(config_path)) != root / 'config.json'
            or Path(os.path.abspath(legacy_path)) != root / 'data' / 'install_options.json'
            or mode_flag not in ('0', '1') or requested_mode not in ('hermes', 'standalone')
            or settings_migration_receipt.pending(root / 'data')):
        raise ValueError('runtime_edit_unverified')
    state = provider_settings.probe(root)
    if state['state'] == 'legacy_unverified' and state['config_present']:
        require_legacy_runtime_edit(config_path, legacy_path)
        return 'legacy'
    if state['state'] != 'requires_checked_read':
        raise ValueError('runtime_edit_unverified')
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    checked = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
    if checked['mode'] != 'split' or settings_migration_receipt.pending(root / 'data'):
        raise ValueError('runtime_edit_unverified')
    common, snapshot = checked['common'], checked['snapshot']
    actual_base = _runtime_base(snapshot)
    if (actual_base != config_base
            or mode_flag == '1' and common.get('runtime_mode') != requested_mode
            or recovery and common.get('recovery_python') != recovery):
        raise ValueError('runtime_edit_unavailable')
    # Installer software preparation can add unrelated files under root/data.
    # Bind their anchors plus every settings file and the pending witness.
    return 'split:' + _runtime_snapshot_token(snapshot)


def fresh_marker_sync(root, update_fd):
    root = Path(os.path.abspath(root))
    _fresh_update_lock(root, int(update_fd))
    _fresh_root_clean(root)
    _, raw, identity = _fresh_marker_read(root)
    fd = os.open(root / 'data' / _FRESH_MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if _identity(os.fstat(fd)) != identity:
            raise ValueError('fresh_marker_changed')
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(root / 'data', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    _, after, after_identity = _fresh_marker_read(root)
    if (after, after_identity) != (raw, identity):
        raise ValueError('fresh_marker_changed')


def main(argv):
    mode, config_path, legacy_path, expected, *desired = argv
    if mode == 'fresh-complete':
        try:
            if len(desired) != 1:
                return 1
            fresh_complete(Path(os.path.abspath(config_path)).parent, desired[0])
        except (OSError, ValueError, TypeError, KeyError, RecursionError, ImportError):
            print('初回導入の完了記録を確認できません。設定と記録を保持します。', file=sys.stderr)
            return 1
        return 0
    if mode == 'require-uninitialized-legacy':
        try:
            if len(desired) != 1:
                return 1
            require_uninitialized_legacy(config_path, legacy_path, desired[0])
        except (OSError, ValueError, TypeError, KeyError, RecursionError, ImportError):
            print('既存データの設定状態を確認できません。記録を保持してください。', file=sys.stderr)
            return 1
        return 0
    if mode in ('bootstrap-runtime-inspect', 'bootstrap-runtime-apply'):
        try:
            if mode == 'bootstrap-runtime-inspect':
                if desired:
                    return 1
                result = bootstrap_runtime_inspect(config_path, legacy_path, expected)
            else:
                if len(desired) != 3:
                    return 1
                result = bootstrap_runtime_apply(config_path, legacy_path, expected, *desired)
            print(result)
        except (OSError, ValueError, TypeError, KeyError, RecursionError, ImportError):
            print('初回設定の接続方法を確認できません。設定と記録を保持してください。', file=sys.stderr)
            return 1
        return 0
    if mode in ('runtime-edit-decision', 'runtime-noop-verify'):
        try:
            if len(desired) != 4:
                return 1
            config_base, mode_flag, requested_mode, recovery = desired
            result = runtime_edit_decision(config_path, legacy_path, config_base, mode_flag, requested_mode, recovery)
            if mode == 'runtime-noop-verify':
                if not result.startswith('split:') or result != expected:
                    raise ValueError('runtime_snapshot_changed')
            else:
                print(result)
        except (OSError, ValueError, TypeError, KeyError, RecursionError, ImportError):
            print('提供元設定または接続方法を確認できません。設定を保持してください。', file=sys.stderr)
            return 1
        return 0
    if mode == 'require-legacy-runtime':
        try:
            require_legacy_runtime_edit(config_path, legacy_path)
        except (OSError, ValueError, TypeError, KeyError, RecursionError, ImportError):
            print('提供元設定のある環境では接続方法をここで変更できません。設定を保持して更新・復旧手順を確認してください。', file=sys.stderr)
            return 1
        return 0
    if mode == 'fresh-sync':
        try:
            if len(desired) != 1:
                return 1
            fresh_marker_sync(Path(os.path.abspath(config_path)).parent, desired[0])
        except (OSError, ValueError, TypeError, KeyError, RecursionError, ImportError):
            print('初回導入の記録を確認できません。設定と記録を保持します。', file=sys.stderr)
            return 1
        return 0
    if mode == 'fresh-init':
        try:
            if len(desired) != 1:
                return 1
            root = Path(os.path.abspath(config_path)).parent
            fresh_marker_init(root, expected, desired[0])
        except (OSError, ValueError, TypeError, KeyError, RecursionError, ImportError):
            print('初回導入の記録を確認できません。設定と記録を保持します。', file=sys.stderr)
            return 1
        return 0
    if mode == 'bootstrap-fresh':
        if len(desired) != 2:
            return 1
        try:
            return bootstrap_fresh(config_path, legacy_path, expected, desired[0], desired[1])
        except (OSError, ValueError, TypeError, RecursionError, ImportError):
            print('初回導入の証拠を確認できません。設定と保留記録を保持します。', file=sys.stderr)
            return 1
    if mode == 'provider-check':
        try:
            _provider_preflight(config_path, legacy_path)
        except (OSError, ValueError, TypeError, RecursionError, ImportError):
            print('提供元設定の状態を確認できません。設定を保持して中止します。', file=sys.stderr)
            return 1
        return 0
    if settings_migration_receipt.pending(Path(legacy_path).parent):
        print('導入設定の移行結果を確認できません。保留記録と設定を保持し、担当者に確認してください。', file=sys.stderr)
        return 1
    if mode in ('inspect', 'inspect-update'):
        try:
            token = inspect(config_path, legacy_path, desired if mode == 'inspect' else None)
            if mode == 'inspect-update':
                central, _ = _central(config_path)
                legacy, _ = _legacy(legacy_path)
                token = ('migrate:' if central is None and legacy is not None else 'skip:') + token
            print(token)
        except (OSError, ValueError, TypeError, RecursionError):
            print('導入の省略項目と旧記録の整合を確認できません。旧記録を保持して中止します。', file=sys.stderr)
            return 1
        return 0
    if mode == 'save':
        return save(config_path, legacy_path, desired, expected, check_pending=True)
    if mode == 'migrate':
        return save(config_path, legacy_path, None, expected, check_pending=True)
    if mode == 'verify':
        try:
            if inspect(config_path, legacy_path) == expected:
                return 0
        except (OSError, ValueError, TypeError, RecursionError):
            pass
        print('導入設定が変更されたか確認できません。現在の状態を確認してください。', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
