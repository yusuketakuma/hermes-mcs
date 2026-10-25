"""依存lockの閉じた形式とwheelを、候補コードを実行せず検査する。"""
from __future__ import annotations

import email.parser
import base64
import csv
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile

from adapters.sdc import release_contract as release
from safe_io import atomic_write, read_regular_nofollow

LOCK_PATH = 'deployment/sdc-release-lock.json'
WHEELS_PATH = 'deployment/sdc-wheels'
SCHEMA = 'mcs.release-dependency-lock.v1'
MAX_LOCK = 1024 * 1024
MAX_WHEEL = release.POLICY_CEILINGS['max_artifact_bytes']
MAX_EXPANDED = release.POLICY_CEILINGS['max_unpacked_bytes']
MAX_ENTRIES = release.POLICY_CEILINGS['max_archive_entries']
RECEIPT_NAME = '.mcs-release-dependencies.json'
RECEIPT_SCHEMA = 'mcs.release-dependency-receipt.v1'
MAX_RECEIPT = 131072
_PACKAGE = ('name', 'version', 'wheels')


class LockError(ValueError):
    """入力値や私有pathを含めず、固定エラーだけを返す。"""
    def __init__(self, code='INVALID'):
        self.code = code
        super().__init__('RELEASE_DEPENDENCY_LOCK_' + code)


def _check(ok, code='INVALID'):
    if not ok:
        raise LockError(code)


def _fields(value, names):
    _check(type(value) is dict and set(value) == set(names))


def _ascii(value, pattern):
    return type(value) is str and re.fullmatch(pattern, value, flags=re.ASCII) is not None


def package_name(value):
    return _ascii(value, r'[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?') and '--' not in value


def _ordered(values, maximum, key, *, minimum=0):
    _check(type(values) is list and minimum <= len(values) <= maximum)
    keys = [key(value) for value in values]
    _check(keys == sorted(set(keys)))


def encoded(value):
    """検査済みplain JSON recordの固定encoding（末尾LFなし）。"""
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('ascii')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _input(value, maximum):
    """このfamily専用の有界JSON入力。既存support familyの上限は変えない。"""
    try:
        raw = value if type(value) is bytes else value.encode('ascii') if type(value) is str else encoded(value)
        _check(len(raw) <= maximum)
        def pairs(items):
            result = {}
            for key, item in items:
                _check(key not in result)
                result[key] = item
            return result
        def integer(text):
            _check(text != '-0')
            number = int(text)
            _check(abs(number) <= release.MAX_SAFE_INTEGER)
            return number
        return json.loads(raw.decode('ascii'), object_pairs_hook=pairs, parse_int=integer,
                          parse_float=lambda _: _check(False), parse_constant=lambda _: _check(False))
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise LockError() from None


def _packages(packages, maximum):
    _check(type(packages) is list and len(packages) <= maximum)
    for package in packages:
        _fields(package, _PACKAGE)
        _check(package_name(package['name']) and _ascii(package['version'], r'[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}'))
        wheels = package['wheels']
        _check(type(wheels) is list and 1 <= len(wheels) <= 8)
        for wheel in wheels:
            _fields(wheel, ('filename', 'sha256', 'size'))
            name = wheel['filename']
            _check(_ascii(name, r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,250}\.whl')
                   and len(name) <= 255 and '..' not in name)
            _check(release.hex64(wheel['sha256']) and type(wheel['size']) is int
                   and 0 < wheel['size'] <= MAX_WHEEL)
        _ordered(wheels, 8, lambda wheel: wheel['filename'], minimum=1)
    _ordered(packages, maximum, lambda package: package['name'])


def validate(value):
    """正本bytesならCJSON完全一致も必須。dictは生成器の検査入力に限る。"""
    try:
        raw = value if type(value) is bytes else value.encode('ascii') if type(value) is str else None
        value = _input(value, MAX_LOCK)
        _fields(value, ('schema_version', 'product', 'core_kind', 'profiles'))
        _check(value['schema_version'] == SCHEMA and value['product'] == 'hermes-mcs'
               and value['core_kind'] == 'stdlib-only')
        profiles = value['profiles']
        _check(type(profiles) is list and 1 <= len(profiles) <= 16)
        identities, filenames = set(), {}
        for profile in profiles:
            _fields(profile, ('profile_id', 'runtime_mode', 'platform', 'requirements_sha256',
                              'host', 'packages', 'build_packages'))
            _check(_ascii(profile['profile_id'], r'[a-z0-9][a-z0-9_-]{0,63}'))
            platform = profile['platform']
            _fields(platform, ('os', 'cpu', 'python'))
            _check(release.slug(platform['os']) and release.slug(platform['cpu'])
                   and release.int_tuple(platform['python'], 2) and platform['python'][0] == 3)
            _check(profile['runtime_mode'] in ('hermes', 'standalone')
                   and release.hex64(profile['requirements_sha256']))
            identity = (profile['runtime_mode'], encoded(platform))
            _check(identity not in identities)
            identities.add(identity)
            _packages(profile['packages'], 256)
            _packages(profile['build_packages'], 32)
            packages = {package['name']: package for package in profile['packages']}
            for package in profile['build_packages']:
                _check(package['name'] not in packages or packages[package['name']] == package)
            for package in profile['packages'] + profile['build_packages']:
                for wheel in package['wheels']:
                    identity = (wheel, package['name'], package['version'])
                    _check(wheel['filename'] not in filenames or filenames[wheel['filename']] == identity)
                    filenames[wheel['filename']] = identity
            host = profile['host']
            if profile['runtime_mode'] == 'standalone':
                _check(host is None and profile['build_packages'] == [])
            else:
                _fields(host, ('commit', 'pyproject_sha256', 'extras', 'project_name'))
                _check(_ascii(host['commit'], r'[0-9a-f]{40}') and release.hex64(host['pyproject_sha256'])
                       and host['pyproject_sha256'] == profile['requirements_sha256']
                       and host['extras'] == ['messaging'] and package_name(host['project_name'])
                       and host['project_name'] not in packages
                       and host['project_name'] not in {package['name'] for package in profile['build_packages']})
        _ordered(profiles, 16, lambda profile: profile['profile_id'], minimum=1)
        canonical = encoded(value)
        _check(len(canonical) <= MAX_LOCK and (raw is None or raw == canonical))
        return value
    except (TypeError, UnicodeError, KeyError, OverflowError, ValueError) as error:
        if isinstance(error, LockError):
            raise
        raise LockError() from None


def select(value, runtime_mode, platform):
    lock = validate(value)
    matches = [profile for profile in lock['profiles']
               if profile['runtime_mode'] == runtime_mode and profile['platform'] == platform]
    _check(len(matches) == 1, 'PROFILE_UNSUPPORTED')
    return matches[0]


def read_lock(repo):
    path = Path(repo) / LOCK_PATH
    try:
        return validate(read_regular_nofollow(path, MAX_LOCK, LockError))
    except FileNotFoundError:
        raise LockError('MISSING') from None


def hermes_pin(install_bytes):
    """shellは実行せず、一意な固定literalだけを採用する。"""
    try:
        lines = install_bytes.decode('utf-8').splitlines()
        assignments = [line for line in lines if re.match(r'^\s*HERMES_PIN\s*=', line)]
        _check(len(assignments) == 1)
        match = re.fullmatch(r'HERMES_PIN="([0-9a-f]{40})"', assignments[0])
        _check(match is not None)
        return match[1]
    except UnicodeError:
        raise LockError() from None


def wheel_metadata(raw, *, expected=None):
    """ZIPを展開せず有界に全entry/CRCを調べ、METADATAの固定名・版を読む。"""
    _check(type(raw) is bytes and 0 < len(raw) <= MAX_WHEEL)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            _check(0 < len(entries) <= MAX_ENTRIES)
            names, prefixes, expanded, metadata = set(), {}, 0, []
            for entry in entries:
                name = entry.filename
                parts = name.rstrip('/').split('/')
                _check(len(name) <= 4096 and len(parts) <= 64 and all(len(part) <= 255 for part in parts)
                       and name.isascii() and all(32 <= ord(char) < 127 for char in name)
                       and ':' not in name and all(part not in ('', '.', '..') for part in parts)
                       and '\\' not in name and not name.startswith('/') and '\x00' not in name
                       and name.rstrip('/').casefold() not in names and not entry.flag_bits & 1)
                names.add(name.rstrip('/').casefold())
                for index in range(1, len(parts) + 1):
                    prefix = '/'.join(parts[:index])
                    is_directory = index < len(parts) or entry.is_dir()
                    previous = prefixes.get(prefix.casefold())
                    _check(previous is None or previous == (prefix, True) and is_directory, 'WHEEL_INVALID')
                    prefixes[prefix.casefold()] = (prefix, is_directory)
                mode = entry.external_attr >> 16
                _check(stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR))
                expanded += entry.file_size
                _check(expanded <= MAX_EXPANDED and entry.file_size <= max(1, entry.compress_size) * 1000)
                if name.endswith('.dist-info/METADATA'):
                    _check(len(parts) == 2 and entry.file_size <= MAX_LOCK)
                    metadata.append(entry)
                # Read in blocks: validate CRC without retaining the expanded package.
                with archive.open(entry) as stream:
                    count = 0
                    while chunk := stream.read(65536):
                        count += len(chunk)
                        _check(count <= entry.file_size)
                    _check(count == entry.file_size)
            _check(len(metadata) == 1)
            message = email.parser.BytesParser().parsebytes(archive.read(metadata[0]))
            _check(len(message.get_all('Name', [])) == len(message.get_all('Version', [])) == 1)
            name = re.sub(r'[-_.]+', '-', message['Name']).lower()
            version = message['Version']
            _check(package_name(name) and _ascii(version, r'[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}'))
            directory = metadata[0].filename.rsplit('/', 1)[0]
            _check(directory == name.replace('-', '_') + '-' + version + '.dist-info', 'WHEEL_INVALID')
            for requirement in message.get_all('Requires-Dist', []):
                _check(not any(char in requirement for char in ('@', '/', '\\', '\r', '\x00')), 'WHEEL_INVALID')
            _check(expected is None or (name, version) == (expected['name'], expected['version']))
            return {'name': name, 'version': version, 'dist_info': directory,
                    'extras': message.get_all('Provides-Extra', [])}
    except (zipfile.BadZipFile, RuntimeError, EOFError, UnicodeError, OSError, NotImplementedError):
        raise LockError('WHEEL_INVALID') from None


def verify_wheels(profile, wheelhouse):
    """許可wheelだけを読取り、size/hashとMETADATAを突合せる。"""
    result = {}
    for package in profile['packages'] + profile['build_packages']:
        for wheel in package['wheels']:
            if wheel['filename'] in result:
                continue
            raw = read_regular_nofollow(Path(wheelhouse) / wheel['filename'], MAX_WHEEL, LockError)
            _check(len(raw) == wheel['size'] and hashlib.sha256(raw).hexdigest() == wheel['sha256'], 'WHEEL_INVALID')
            wheel_metadata(raw, expected=package)
            result[wheel['filename']] = raw
    return result


def _git_read(repo, args, *, deadline=None):
    return _run(['/usr/bin/git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
                 '-c', 'core.untrackedCache=false', '-c', 'core.attributesFile=/dev/null',
                 '-c', 'protocol.allow=never', '-C', str(repo), *args],
                cwd=repo, deadline=deadline if deadline is not None else time.monotonic() + 10)


def _git_observation(repo, *, deadline=None):
    # Reject executable filters and nested repositories before asking status to inspect files.
    keys = _git_read(repo, ['config', '--local', '--includes', '--null', '--name-only', '--list'], deadline=deadline)
    _check(not any(key.lower().startswith(b'filter.') for key in keys.split(b'\0')), 'REPOSITORY_UNSUPPORTED')
    stages = _git_read(repo, ['ls-files', '--stage', '-z'], deadline=deadline)
    _check(not any(row.startswith(b'160000 ') for row in stages.split(b'\0')), 'REPOSITORY_UNSUPPORTED')
    flags = _git_read(repo, ['ls-files', '-v', '-z'], deadline=deadline)
    _check(all(row.startswith(b'H ') for row in flags.split(b'\0') if row), 'REPOSITORY_UNSUPPORTED')
    head = _git_read(repo, ['rev-parse', '--verify', 'HEAD'], deadline=deadline).strip().decode('ascii')
    _check(_ascii(head, r'[0-9a-f]{40}'), 'REPOSITORY_UNSUPPORTED')
    clean = not _git_read(repo, ['status', '--porcelain=v1', '-z', '--untracked-files=all',
                                '--ignore-submodules=all'], deadline=deadline)
    return head, clean


def source_requirements(profile, repo, host_source=None, *, deadline=None):
    """解決器へ渡す固定sourceとoverride。source/buildコードは実行しない。"""
    if profile['runtime_mode'] == 'standalone':
        raw = read_regular_nofollow(Path(repo) / 'deployment/requirements-standalone.txt', MAX_LOCK, LockError)
        _check(hashlib.sha256(raw).hexdigest() == profile['requirements_sha256'])
        expected = {package['name']: package['version'] for package in profile['packages']}
        for line in raw.decode('ascii').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9.!+_-]+)', line)
            _check(match is not None)
            name = re.sub('[-_.]+', '-', match[1]).lower()
            _check(expected.get(name) == match[2])
        return raw.decode('ascii'), '', ''
    _check(host_source is not None, 'HOST_REQUIRED')
    host = profile['host']
    install = read_regular_nofollow(Path(repo) / 'install.sh', MAX_LOCK, LockError)
    _check(hermes_pin(install) == host['commit'], 'HOST_MISMATCH')
    head, clean = _git_observation(host_source, deadline=deadline)
    _check(head == host['commit'] and clean, 'HOST_MISMATCH')
    raw = read_regular_nofollow(Path(host_source) / 'pyproject.toml', MAX_LOCK, LockError)
    _check(hashlib.sha256(raw).hexdigest() == host['pyproject_sha256'], 'HOST_MISMATCH')
    _check(_git_read(host_source, ['show', 'HEAD:pyproject.toml'], deadline=deadline) == raw, 'HOST_MISMATCH')
    import tomllib
    try:
        project = tomllib.loads(raw.decode('utf-8'))
        declared = project['project']
        _check(re.sub('[-_.]+', '-', declared['name']).lower() == host['project_name'])
        uv = project.get('tool', {}).get('uv', {})
        # Sources/indexes/workspaces would add a second authority for artifact selection.
        _check(not any(key in uv for key in ('sources', 'index', 'workspace', 'constraint-dependencies', 'exclude-dependencies')),
               'HOST_UNSUPPORTED')
        dependencies = declared['dependencies'] + declared['optional-dependencies']['messaging']
        overrides = uv.get('override-dependencies', [])
        build = project['build-system']
        _check(build['build-backend'] == 'setuptools.build_meta' and set(build) == {'requires', 'build-backend'}, 'HOST_UNSUPPORTED')
        def lines(items):
            _check(type(items) is list and len(items) <= 1024)
            _check(all(type(item) is str and item.isascii() and 0 < len(item) <= 1024
                       and not any(char in item for char in ('@', '/', '\\', '\n', '\r', '\x00'))
                       and re.match(r'^[A-Za-z0-9]', item) for item in items), 'HOST_UNSUPPORTED')
            return ''.join(item + '\n' for item in items)
        _python_requirement(declared.get('requires-python'), profile['platform']['python'])
        environments = uv.get('environments', [])
        _check(type(environments) is list and len(environments) <= 16, 'HOST_UNSUPPORTED')
        if environments:
            _check(any(_environment(expression, profile['platform']['python']) for expression in environments), 'HOST_UNSUPPORTED')
        return lines(dependencies), lines(overrides), lines(build['requires'])
    except (KeyError, TypeError, UnicodeError, tomllib.TOMLDecodeError):
        raise LockError('HOST_UNSUPPORTED') from None


def _compare(left, operator, right):
    return {'==': left == right, '!=': left != right, '>=': left >= right,
            '<=': left <= right, '>': left > right, '<': left < right}[operator]


def _python_requirement(value, python):
    _check(type(value) is str, 'HOST_UNSUPPORTED')
    for term in value.split(','):
        match = re.fullmatch(r'\s*(==|!=|>=|<=|>|<)\s*(\d+)\.(\d+)\s*', term)
        _check(match is not None, 'HOST_UNSUPPORTED')
        _check(_compare(tuple(python), match[1], (int(match[2]), int(match[3]))), 'PROFILE_UNSUPPORTED')


def _environment(value, python):
    # Unsupported marker expressions stop; never silently ignore a host environment guard.
    _check(type(value) is str, 'HOST_UNSUPPORTED')
    match = re.fullmatch(r'''python_version\s*(==|!=|>=|<=|>|<)\s*['"](\d+)\.(\d+)['"]''', value)
    _check(match is not None, 'HOST_UNSUPPORTED')
    return _compare(tuple(python), match[1], (int(match[2]), int(match[3])))


def build_lock(specification, repo, host_source=None):
    """公開specとwheel bytesからidentityを生成。依存解決/導入の証明ではない。"""
    value = _input(specification, MAX_LOCK)
    try:
        for profile in value['profiles']:
            if profile['runtime_mode'] == 'standalone':
                source = read_regular_nofollow(Path(repo) / 'deployment/requirements-standalone.txt', MAX_LOCK, LockError)
            else:
                _check(host_source is not None, 'HOST_REQUIRED')
                source = read_regular_nofollow(Path(host_source) / 'pyproject.toml', MAX_LOCK, LockError)
                profile['host']['pyproject_sha256'] = hashlib.sha256(source).hexdigest()
            profile['requirements_sha256'] = hashlib.sha256(source).hexdigest()
            for package in profile['packages'] + profile['build_packages']:
                for wheel in package['wheels']:
                    _check(set(wheel) <= {'filename', 'sha256', 'size'} and 'filename' in wheel)
                    name = wheel['filename']
                    _check(_ascii(name, r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,250}\.whl') and '..' not in name)
                    raw = read_regular_nofollow(Path(repo) / WHEELS_PATH / name, MAX_WHEEL, LockError)
                    wheel_metadata(raw, expected=package)
                    wheel.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        value = validate(value)
        for profile in value['profiles']:
            source_requirements(profile, repo, host_source)
        return value
    except (KeyError, TypeError, UnicodeError):
        raise LockError() from None


def _run(argv, *, cwd, deadline, input_bytes=None):
    """Bound one owned process group; no manager output reaches logs or caller errors."""
    remaining = deadline - time.monotonic()
    _check(remaining > 0, 'TIMEOUT')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('UV_', 'PIP_', 'PYTHON', 'VIRTUAL_ENV', 'CONDA_', 'GIT_'))}
    env.update(UV_OFFLINE='1', UV_NO_CONFIG='1', UV_PYTHON_DOWNLOADS='never',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_REPLACE_OBJECTS='1', GIT_TERMINAL_PROMPT='0',
               GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1', GIT_ATTR_NOSYSTEM='1')
    with tempfile.TemporaryFile() as output:
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            child.communicate(input_bytes, timeout=remaining)
            output.seek(0)
            raw = output.read(MAX_LOCK + 1)
            _check(child.returncode == 0 and len(raw) <= MAX_LOCK, 'MANAGER_FAILED')
            return raw
        finally:
            if child.returncode is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate(timeout=5)


_METADATA_PROBE = '''import importlib.metadata as m,json,pathlib,sys,sysconfig
args=json.load(sys.stdin)
venv=pathlib.Path(args['venv'])
site=venv/'lib'/('python%d.%d'%sys.version_info[:2])/'site-packages'
rows=[]
for d in m.distributions(path=[str(site)]):
 rows.append({'name':d.metadata['Name'],'version':d.version,'direct':d.read_text('direct_url.json')})
print(json.dumps({'python':list(sys.version_info[:2]),'executable':sys.executable,'site':str(site),'packages':rows}))
'''


def _runtime(python, venv, *, deadline, cwd):
    _check(Path(python).absolute().parent.parent == Path(venv).absolute(), 'RUNTIME_MISMATCH')
    venv = Path(venv).absolute()
    before = os.stat(venv, follow_symlinks=False)
    _check(stat.S_ISDIR(before.st_mode) and before.st_uid == os.getuid(), 'RUNTIME_MISMATCH')
    executable = Path(python).resolve(strict=True)
    executable_stat = executable.stat()
    _check(stat.S_ISREG(executable_stat.st_mode), 'RUNTIME_MISMATCH')
    raw = _run([str(python), '-I', '-S', '-B', '-c', _METADATA_PROBE], cwd=cwd, deadline=deadline,
               input_bytes=json.dumps({'venv': str(venv)}).encode())
    try:
        observed = json.loads(raw)
        _check(Path(observed['executable']).resolve() == executable, 'RUNTIME_MISMATCH')
        after = os.stat(venv, follow_symlinks=False)
        _check((before.st_dev, before.st_ino, before.st_uid) == (after.st_dev, after.st_ino, after.st_uid), 'RUNTIME_MISMATCH')
        final_executable = Path(python).resolve(strict=True)
        final_stat = final_executable.stat()
        _check(final_executable == executable and all(getattr(executable_stat, field) == getattr(final_stat, field)
               for field in ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_size', 'st_mtime_ns')), 'RUNTIME_MISMATCH')
        identity = {'venv': str(venv), 'uid': before.st_uid, 'device': before.st_dev,
                    'inode': before.st_ino, 'executable': str(executable),
                    'executable_device': executable_stat.st_dev, 'executable_inode': executable_stat.st_ino,
                    'python': observed['python']}
        return digest(identity), observed
    except (KeyError, TypeError, ValueError):
        raise LockError('RUNTIME_MISMATCH') from None


def _compile(uv, source, overrides, packages, workspace, python, *, deadline, label):
    """Read uv's pylock machine record; free-form diagnostics never prove a plan."""
    import tomllib
    if not packages:
        _check(not source.strip(), 'RESOLUTION_MISMATCH')
        return {}
    source_path = workspace / (label + '.in')
    source_path.write_text(source, encoding='ascii')
    override_path = workspace / (label + '-overrides.txt')
    override_path.write_text(overrides, encoding='ascii')
    output_path = workspace / ('pylock.' + label + '.toml')
    argv = [uv, 'pip', 'compile', '--offline', '--no-index', '--no-config', '--no-cache',
            '--no-build', '--no-python-downloads', '--python', str(_isolated_python(python, workspace)), '--find-links', str(workspace / 'wheels'),
            '--format', 'pylock.toml', '--no-header', '--output-file', str(output_path),
            '--overrides', str(override_path), str(source_path)]
    _run(argv, cwd=workspace, deadline=deadline)
    try:
        plan = tomllib.loads(read_regular_nofollow(output_path, MAX_LOCK, LockError).decode('utf-8'))
        _check(set(plan) <= {'lock-version', 'created-by', 'requires-python', 'packages'} and plan['lock-version'] == '1.0')
        selected = {}
        expected = {package['name']: package for package in packages}
        for package in plan.get('packages', []):
            _check(type(package) is dict and set(package) in (
                {'name', 'version', 'wheels'}, {'name', 'version', 'archive'}))
            name = package['name']
            _check(name in expected and name not in selected and package['version'] == expected[name]['version'])
            allowed = {wheel['filename']: wheel for wheel in expected[name]['wheels']}
            choices = []
            candidates = package['wheels'] if 'wheels' in package else [package['archive']]
            for candidate in candidates:
                _fields(candidate, ('url', 'hashes') if 'wheels' in package else ('path', 'hashes'))
                # Only URLs we generated from our verified private snapshot are admissible.
                matches = []
                for filename, wheel in allowed.items():
                    path = workspace / 'wheels' / filename
                    key, location = ('url', path.as_uri()) if 'wheels' in package else ('path', str(path))
                    if candidate[key] == location:
                        matches.append(wheel)
                _check(len(matches) == 1 and candidate['hashes'] == {'sha256': matches[0]['sha256']})
                choices.append(matches[0])
            _check(choices)
            selected[name] = sorted(choices, key=lambda wheel: wheel['filename'])[0]
        _check(set(selected) == set(expected), 'RESOLUTION_MISMATCH')
        return selected
    except (KeyError, TypeError, UnicodeError, tomllib.TOMLDecodeError):
        raise LockError('RESOLUTION_MISMATCH') from None


def _isolated_python(python, workspace):
    """uvのinterpreter probeもsite/.pthを実行させない（製品runtimeは3.14）。"""
    path = workspace / 'isolated-python'
    driver = workspace / 'isolated-driver.py'
    code = '''import pathlib,runpy,sys
site=sys.argv[1]
args=sys.argv[2:]
sys.path.append(site)
while args and args[0] in ('-I','-B','-s','-E','-S'):
 args=args[1:]
if not args:
 raise SystemExit(2)
if args[0]=='-c' and len(args)>=2:
 sys.argv=['-c',*args[2:]]
 exec(compile(args[1],'<isolated-manager>','exec'),{'__name__':'__main__'})
elif args[0]=='-m' and len(args)>=2:
 sys.argv=args[1:]
 runpy.run_module(args[1],run_name='__main__',alter_sys=True)
elif not args[0].startswith('-'):
 sys.argv=args
 runpy.run_path(args[0],run_name='__main__')
else:
 raise SystemExit(2)
'''.encode('ascii')
    site = Path(python).absolute().parent.parent / 'lib/python3.14/site-packages'
    raw = ('#!/bin/sh\nexec ' + shlex.quote(str(Path(python).absolute())) + ' -I -S '
           + shlex.quote(str(driver)) + ' ' + shlex.quote(str(site)) + ' "$@"\n').encode('utf-8')
    try:
        driver_fd = os.open(driver, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        _check(read_regular_nofollow(driver, 8192, LockError) == code, 'RUNTIME_MISMATCH')
    else:
        with os.fdopen(driver_fd, 'wb') as stream:
            stream.write(code)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
    except FileExistsError:
        _check(read_regular_nofollow(path, 8192, LockError) == raw, 'RUNTIME_MISMATCH')
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
    return path


def _receipt(value):
    try:
        value = _input(value, MAX_RECEIPT)
        _fields(value, ('schema_version', 'attempt_id', 'phase', 'runtime_fingerprint', 'profile_id',
                        'profile_sha256', 'lock_sha256', 'host_commit', 'packages', 'build_packages',
                        'issued_at', 'completed_at'))
        _check(value['schema_version'] == RECEIPT_SCHEMA and release.uuid(value['attempt_id'])
               and value['phase'] in ('pending', 'completed')
               and _ascii(value['profile_id'], r'[a-z0-9][a-z0-9_-]{0,63}'))
        _check(all(release.hex64(value[key]) for key in ('runtime_fingerprint', 'profile_sha256', 'lock_sha256')))
        _check(value['host_commit'] is None or _ascii(value['host_commit'], r'[0-9a-f]{40}'))
        for key, maximum in (('packages', 256), ('build_packages', 32)):
            for package in value[key]:
                _fields(package, ('name', 'version', 'wheel_sha256'))
                _check(package_name(package['name']) and _ascii(package['version'], r'[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}')
                       and release.hex64(package['wheel_sha256']))
            _ordered(value[key], maximum, lambda package: package['name'])
        _check(release.integer(value['issued_at']))
        if value['phase'] == 'pending':
            _check(value['completed_at'] is None)
        else:
            _check(release.integer(value['completed_at']) and value['completed_at'] >= value['issued_at'])
        _check(len(encoded(value)) <= MAX_RECEIPT)
        return value
    except (TypeError, KeyError, ValueError) as error:
        if isinstance(error, LockError):
            raise
        raise LockError('RECEIPT_INVALID') from None


def _read_receipt(venv):
    path = Path(venv) / RECEIPT_NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        _check(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
               and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
               and info.st_size <= MAX_RECEIPT, 'RECEIPT_INVALID')
        raw = stream.read(MAX_RECEIPT + 1)
    return _receipt(raw)


def _write_receipt(venv, value):
    value = _receipt(value)
    _read_receipt(venv)  # Never replace a corrupt, linked or foreign receipt.
    atomic_write(str(Path(venv) / RECEIPT_NAME), lambda stream: stream.write(encoded(value)),
                 mode=0o600, binary=True, makedirs=False)


def _metadata_map(observed):
    _check(type(observed.get('packages')) is list, 'METADATA_MISMATCH')
    actual = {}
    for package in observed['packages']:
        _fields(package, ('name', 'version', 'direct'))
        _check(type(package['name']) is str and type(package['version']) is str, 'METADATA_MISMATCH')
        name = re.sub('[-_.]+', '-', package['name']).lower()
        _check(package_name(name) and name not in actual, 'METADATA_MISMATCH')
        actual[name] = package
    return actual


def _reject_extraneous(observed, profile):
    allowed = {package['name'] for package in profile['packages'] + profile['build_packages']}
    if profile['host'] is not None:
        allowed.add(profile['host']['project_name'])
    _check(set(_metadata_map(observed)) <= allowed, 'EXTRANEOUS_DISTRIBUTION')


def _observed_packages(observed, profile, host_source):
    expected = {package['name']: package['version']
                for package in profile['packages'] + profile['build_packages']}
    host = profile['host']
    actual = _metadata_map(observed)
    for name, package in actual.items():
        if host is not None and name == host['project_name']:
            try:
                direct = json.loads(package['direct'])
                _check(direct == {'url': Path(host_source).resolve().as_uri(),
                                   'dir_info': {'editable': True}}, 'HOST_MISMATCH')
            except (TypeError, ValueError):
                raise LockError('HOST_MISMATCH') from None
        else:
            _check(expected.get(name) == package['version'], 'METADATA_MISMATCH')
    _check(set(actual) == set(expected) | ({host['project_name']} if host is not None else set()), 'METADATA_MISMATCH')


_EDITABLE_HOOK = '''import importlib,json,pathlib,sys
args=json.load(sys.stdin)
sys.path.append(args['site'])
backend=importlib.import_module('setuptools.build_meta')
if args['action']=='requires':
 value=backend.get_requires_for_build_editable(config_settings=None)
elif args['action']=='build':
 value=backend.build_editable(args['output'],config_settings=None,metadata_directory=None)
else:
 raise SystemExit(2)
pathlib.Path(args['result']).write_text(json.dumps(value),encoding='utf-8')
'''


def _hash_requirements(chosen, workspace, path):
    path.write_text(''.join(name + ' @ ' + (workspace / 'wheels' / wheel['filename']).as_uri()
                            + ' --hash=sha256:' + wheel['sha256'] + '\n'
                            for name, wheel in sorted(chosen.items())), encoding='ascii')


def _install_wheels(uv, python, requirements, manager_workspace, *, deadline):
    _run([uv, 'pip', 'install', '--python', str(_isolated_python(python, manager_workspace)),
          '--offline', '--no-index', '--no-config', '--no-cache', '--no-python-downloads',
          '--no-deps', '--no-build', '--require-hashes', '--reinstall', '-r', str(requirements)],
         cwd=manager_workspace, deadline=deadline)


def _build_editable(uv, python, profile, source, overrides, build_source, chosen_build,
                    workspace, host_source, *, deadline):
    """固定backendだけをbuild専用venvで実行。runtimeのplugin/.pthは見せない。"""
    import tomllib
    build_venv = workspace / 'build-venv'
    _run([str(python), '-I', '-S', '-m', 'venv', '--without-pip', str(build_venv)],
         cwd=workspace, deadline=deadline)
    config = read_regular_nofollow(build_venv / 'pyvenv.cfg', 8192, LockError)
    _check(b'include-system-site-packages = false' in config, 'RUNTIME_MISMATCH')
    build_python = build_venv / 'bin/python'
    manager = workspace / 'build-manager'
    manager.mkdir(mode=0o700)
    build_requirements = workspace / 'build-install.txt'
    _hash_requirements(chosen_build, workspace, build_requirements)
    if chosen_build:
        _install_wheels(uv, build_python, build_requirements, manager, deadline=deadline)
    output = workspace / 'editable-output'
    output.mkdir(mode=0o700)
    def hook(action):
        result = workspace / ('editable-' + action + '.json')
        args = {'site': str(build_venv / 'lib/python3.14/site-packages'), 'action': action,
                'output': str(output), 'result': str(result)}
        _run([str(build_python), '-I', '-S', '-c', _EDITABLE_HOOK], cwd=host_source,
             deadline=deadline, input_bytes=json.dumps(args).encode())
        return _input(read_regular_nofollow(result, 16384, LockError), 16384)
    requires = hook('requires')
    _check(type(requires) is list and len(requires) <= 32, 'BUILD_UNSUPPORTED')
    _check(all(type(item) is str and item.isascii() and 0 < len(item) <= 1024
               and re.match(r'^[A-Za-z0-9]', item)
               and not any(char in item for char in ('@', '/', '\\', '\n', '\r', '\x00'))
               for item in requires), 'BUILD_UNSUPPORTED')
    if requires:
        dynamic = _compile(uv, build_source + ''.join(item + '\n' for item in requires), '',
                           profile['build_packages'], workspace, python, deadline=deadline, label='dynamic-build')
        _check(dynamic == chosen_build, 'RESOLUTION_MISMATCH')
    filename = hook('build')
    _check(_ascii(filename, r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,250}\.whl') and '..' not in filename,
           'WHEEL_INVALID')
    raw = read_regular_nofollow(output / filename, MAX_WHEEL, LockError)
    metadata = wheel_metadata(raw)
    project = tomllib.loads(read_regular_nofollow(Path(host_source) / 'pyproject.toml', MAX_LOCK, LockError).decode('utf-8'))['project']
    _check(metadata['name'] == profile['host']['project_name']
           and metadata['version'] == project.get('version')
           and 'messaging' in metadata['extras'], 'HOST_MISMATCH')
    host_wheel = {'filename': filename, 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}
    _check(not (workspace / 'wheels' / filename).exists(), 'WHEEL_INVALID')
    (workspace / 'wheels' / filename).write_bytes(raw)
    generated = {'name': metadata['name'], 'version': metadata['version'], 'wheels': [host_wheel]}
    # Validate generated runtime metadata/extras through the same offline resolver, not a PEP508 clone.
    host_input = metadata['name'] + '[messaging] @ ' + (workspace / 'wheels' / filename).as_uri() + '\n'
    _compile(uv, source + host_input, overrides, profile['packages'] + [generated], workspace,
             python, deadline=deadline, label='generated-host')
    return generated, metadata['dist_info']


def _host_provenance(venv, dist_info, host_source):
    """実際のeditable wheel導入後、pending中にprovenance/RECORDを一致させる。"""
    _check(_ascii(dist_info, r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,250}\.dist-info') and '..' not in dist_info)
    directory = Path(venv) / 'lib/python3.14/site-packages' / dist_info
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        _check(before.st_uid == os.getuid())
        for name in ('direct_url.json', 'RECORD'):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            _check(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1)
        record = read_regular_nofollow(directory / 'RECORD', MAX_LOCK, LockError).decode('utf-8')
        rows = list(csv.reader(io.StringIO(record)))
        _check(all(len(row) == 3 for row in rows))
        direct_name, record_name = dist_info + '/direct_url.json', dist_info + '/RECORD'
        rows = [row for row in rows if row[0] not in (direct_name, record_name)]
        direct = encoded({'url': Path(host_source).resolve().as_uri(), 'dir_info': {'editable': True}})
        token = base64.urlsafe_b64encode(hashlib.sha256(direct).digest()).rstrip(b'=').decode('ascii')
        rows += [[direct_name, 'sha256=' + token, str(len(direct))], [record_name, '', '']]
        output = io.StringIO()
        csv.writer(output, lineterminator='\n').writerows(rows)
        raw = output.getvalue().encode('utf-8')
        _check(len(raw) <= MAX_LOCK)
        atomic_write(str(directory / 'direct_url.json'), lambda stream: stream.write(direct),
                     mode=0o644, binary=True, makedirs=False)
        atomic_write(str(directory / 'RECORD'), lambda stream: stream.write(raw),
                     mode=0o644, binary=True, makedirs=False)
        after = os.stat(directory, follow_symlinks=False)
        _check((before.st_dev, before.st_ino, before.st_uid) == (after.st_dev, after.st_ino, after.st_uid))
    finally:
        os.close(fd)


def install_locked(repo, *, python, venv, runtime_mode, platform, host_source=None, timeout=300):
    """明示install操作だけから呼ぶ。pending保存後にのみhash固定導入へ進む。"""
    _check(type(timeout) in (int, float) and 0 < timeout <= 900, 'TIMEOUT')
    deadline = time.monotonic() + timeout
    lock = read_lock(repo)
    profile = select(lock, runtime_mode, platform)
    uv = shutil.which('uv')
    _check(uv is not None, 'MANAGER_UNAVAILABLE')
    # The directory descriptor is also the cooperative installer lock; it creates no extra state file.
    fd = os.open(venv, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _read_receipt(venv)
        fingerprint, before = _runtime(python, venv, deadline=deadline, cwd=repo)
        _check(before['python'] == platform['python'] == [3, 14], 'RUNTIME_MISMATCH')
        _reject_extraneous(before, profile)
        source, overrides, build_source = source_requirements(profile, repo, host_source)
        wheels = verify_wheels(profile, Path(repo) / WHEELS_PATH)
        with tempfile.TemporaryDirectory(prefix='mcs-locked-install-') as temporary:
            workspace = Path(temporary).resolve()
            (workspace / 'wheels').mkdir(mode=0o700)
            for filename, raw in wheels.items():
                (workspace / 'wheels' / filename).write_bytes(raw)
            runtime = _compile(uv, source, overrides, profile['packages'], workspace, python,
                               deadline=deadline, label='runtime')
            build = _compile(uv, build_source, '', profile['build_packages'], workspace, python,
                             deadline=deadline, label='build') if build_source else {}
            for name in set(runtime) & set(build):
                _check(runtime[name] == build[name], 'RESOLUTION_MISMATCH')
            chosen = {**runtime, **build}
            def rows(packages):
                return [{'name': package['name'], 'version': package['version'],
                         'wheel_sha256': chosen[package['name']]['sha256']} for package in packages]
            receipt = {'schema_version': RECEIPT_SCHEMA, 'attempt_id': str(uuid.uuid4()),
                       'phase': 'pending', 'runtime_fingerprint': fingerprint,
                       'profile_id': profile['profile_id'], 'profile_sha256': digest(profile),
                       'lock_sha256': digest(lock), 'host_commit': profile['host']['commit'] if profile['host'] else None,
                       'packages': rows(profile['packages']), 'build_packages': rows(profile['build_packages']),
                       'issued_at': int(time.time() * 1000), 'completed_at': None}
            requirements = workspace / 'install.txt'
            _hash_requirements(chosen, workspace, requirements)
            _check(read_lock(repo) == lock and source_requirements(profile, repo, host_source) == (source, overrides, build_source), 'CONFLICT')
            current_fingerprint, _ = _runtime(python, venv, deadline=deadline, cwd=repo)
            _check(current_fingerprint == fingerprint, 'RUNTIME_MISMATCH')
            _write_receipt(venv, receipt)
            generated = dist_info = None
            if profile['host'] is not None:
                source_requirements(profile, repo, host_source)
                generated, dist_info = _build_editable(uv, python, profile, source, overrides, build_source,
                                                      build, workspace, host_source, deadline=deadline)
                _check(read_lock(repo) == lock and source_requirements(profile, repo, host_source) == (source, overrides, build_source)
                       and _read_receipt(venv) == receipt, 'CONFLICT')
                current_fingerprint, _ = _runtime(python, venv, deadline=deadline, cwd=repo)
                _check(current_fingerprint == fingerprint, 'RUNTIME_MISMATCH')
            if chosen:
                _install_wheels(uv, python, requirements, workspace, deadline=deadline)
            if generated is not None:
                source_requirements(profile, repo, host_source)
                host_requirements = workspace / 'host-install.txt'
                _hash_requirements({generated['name']: generated['wheels'][0]}, workspace, host_requirements)
                _install_wheels(uv, python, host_requirements, workspace, deadline=deadline)
                _host_provenance(venv, dist_info, host_source)
            observed_fingerprint, after = _runtime(python, venv, deadline=deadline, cwd=repo)
            _check(observed_fingerprint == fingerprint, 'RUNTIME_MISMATCH')
            _observed_packages(after, profile, host_source)
            _check(read_lock(repo) == lock and source_requirements(profile, repo, host_source) == (source, overrides, build_source), 'CONFLICT')
            _check(_read_receipt(venv) == receipt, 'CONFLICT')
            receipt.update(phase='completed', completed_at=int(time.time() * 1000))
            _write_receipt(venv, receipt)
            return receipt
    finally:
        os.close(fd)


def observe_locked(repo, *, python, venv, runtime_mode, platform, host_source=None, timeout=30, deadline=None):
    """既存completed記録と現在metadataを読むだけ。lock全体差はprofile同一なら許す。"""
    deadline = min(deadline, time.monotonic() + timeout) if deadline is not None else time.monotonic() + timeout
    _check(deadline > time.monotonic(), 'TIMEOUT')
    lock = read_lock(repo)
    profile = select(lock, runtime_mode, platform)
    receipt = _read_receipt(venv)
    _check(receipt is not None and receipt['phase'] == 'completed', 'UNKNOWN_BASELINE')
    _check(receipt['profile_id'] == profile['profile_id'] and receipt['profile_sha256'] == digest(profile)
           and receipt['host_commit'] == (profile['host']['commit'] if profile['host'] else None), 'UNKNOWN_BASELINE')
    fingerprint, observed = _runtime(python, venv, deadline=deadline, cwd=repo)
    _check(fingerprint == receipt['runtime_fingerprint'] and observed['python'] == platform['python'], 'RUNTIME_MISMATCH')
    source_requirements(profile, repo, host_source, deadline=deadline)
    _observed_packages(observed, profile, host_source)
    for field in ('packages', 'build_packages'):
        _check(len(receipt[field]) == len(profile[field]), 'RECEIPT_INVALID')
        for recorded, expected in zip(receipt[field], profile[field]):
            _check(recorded['name'] == expected['name'] and recorded['version'] == expected['version']
                   and recorded['wheel_sha256'] in {wheel['sha256'] for wheel in expected['wheels']}, 'RECEIPT_INVALID')
    _check(_read_receipt(venv) == receipt and read_lock(repo) == lock, 'CONFLICT')
    _check(deadline > time.monotonic(), 'TIMEOUT')
    return {'lock_sha256': digest(lock), 'profile_sha256': digest(profile), 'profile_id': profile['profile_id'],
            'runtime_fingerprint': fingerprint}


def main(argv=None):
    """install.sh専用の固定入口。秘密や私有pathを表示しない。"""
    import argparse
    import platform as host_platform
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--python', required=True)
    parser.add_argument('--venv', required=True)
    parser.add_argument('--mode', choices=('hermes', 'standalone'), required=True)
    parser.add_argument('--host-source')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args(argv)
    platform = {'os': {'Darwin': 'macos', 'Linux': 'linux'}.get(host_platform.system(), 'unsupported'),
                'cpu': {'aarch64': 'arm64', 'AMD64': 'x86_64'}.get(host_platform.machine(), host_platform.machine()),
                'python': list(sys.version_info[:2])}
    try:
        if args.check_only:
            lock = read_lock(args.repo)
            profile = select(lock, args.mode, platform)
            verify_wheels(profile, Path(args.repo) / WHEELS_PATH)
            if profile['host'] is not None:
                install = read_regular_nofollow(Path(args.repo) / 'install.sh', MAX_LOCK, LockError)
                _check(hermes_pin(install) == profile['host']['commit'], 'HOST_MISMATCH')
            if profile['host'] is None or args.host_source and Path(args.host_source).exists():
                source_requirements(profile, args.repo, args.host_source)
            _check(shutil.which('uv') is not None, 'MANAGER_UNAVAILABLE')
            return 0
        install_locked(args.repo, python=args.python, venv=args.venv, runtime_mode=args.mode,
                       platform=platform, host_source=args.host_source)
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as error:
        if isinstance(error, LockError) and error.code == 'EXTRANEOUS_DISTRIBUTION':
            print('宣言外のpackageがあるため変更前に停止しました。現在の環境を残したまま、新しいPython環境で再導入してください。', file=sys.stderr)
        else:
            print('固定した依存環境の導入を確認できません。設定と未完了記録を保持して停止しました。', file=sys.stderr)
        return 1
    print('固定した依存の導入と現在の版を確認しました。遠隔更新の有効化・基線認定は別途必要です。')
    return 0
