"""Hash the complete release-owner source closure; a manifest never grants authority."""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import stat
import subprocess
import time
import unicodedata

OWNER_ID = 'mcs.updater'
OWNER_REVISION = 1
PROVIDER_PROFILE = (1, 0, 0)
SOURCE_POLICY = 'mcs-release-owner-source-v1'
SOURCE_ROOTS = ('mcs/', 'adapters/', 'mcs_standalone/', 'hermes_plugin/', 'lineworks_adapter/', 'deployment/', 'scripts/', 'resources/')
SOURCE_FILES = ('install.sh', 'pyproject.toml', 'scripts/mcs')
SOURCE_SUFFIXES = ('.py', '.sh', '.json', '.toml', '.yaml', '.yml', '.plist', '.sql', '.txt', '.lock')
MANIFEST_PATH = 'deployment/sdc-release-owner.json'
MANIFEST_VERSION = 'mcs.release-owner-source.v1'
MAX_BYTES = 1 << 20
MAX_FILES = 4096
MAX_TOTAL_BYTES = 64 << 20
REQUIRED = ('mcs/ops/release_apply_owner.py', 'mcs/ops/mcs_update.py', 'mcs/ops/release_owner_source.py',
            'mcs/ops/sdc_support_signatures.py', 'mcs/ops/owned_process.py', 'mcs/ops/release_dependency_lock.py',
            'adapters/sdc/release_state.py', 'adapters/sdc/release_apply_contract.py',
            'adapters/sdc/release_artifact.py', 'adapters/sdc/storage.py')


class SourceError(ValueError):
    def __init__(self):
        super().__init__('RELEASE_PROVIDER_SOURCE_HOLD')


def check(ok):
    if not ok:
        raise SourceError()


def budget(deadline):
    check(type(deadline) in (int, float) and math.isfinite(deadline) and time.monotonic() < deadline)
    return deadline - time.monotonic()


def source_path(path):
    return (type(path) is str and path != MANIFEST_PATH and len(path.encode('utf-8')) <= 4096
            and path == unicodedata.normalize('NFC', path)
            and not any(ord(c) < 32 or ord(c) == 127 or c in '\\:' for c in path)
            and all(p not in ('', '.', '..', '.git') for p in path.split('/'))
            and (path in SOURCE_FILES or path.startswith(SOURCE_ROOTS) and path.endswith(SOURCE_SUFFIXES)))


def canonical(value):
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    check(len(raw) <= MAX_BYTES)
    return raw


def validate_manifest(value):
    def pairs(items):
        out = {}
        for key, item in items:
            check(key not in out)
            out[key] = item
        return out
    def integer(text):
        check(text != '-0')
        return int(text)
    try:
        if type(value) in (str, bytes):
            check(len(value) <= MAX_BYTES)
            value = json.loads(value, object_pairs_hook=pairs, parse_int=integer, parse_float=lambda _: check(False), parse_constant=lambda _: check(False))
        check(type(value) is dict and set(value) == {'schema_version', 'owner_id', 'owner_revision', 'provider_profile', 'source_policy', 'files'})
        check(value['schema_version'] == MANIFEST_VERSION and value['owner_id'] == OWNER_ID
              and type(value['owner_revision']) is int and value['owner_revision'] == OWNER_REVISION
              and value['source_policy'] == SOURCE_POLICY)
        profile = value['provider_profile']
        check(type(profile) is list and len(profile) == 3 and all(type(n) is int and 0 <= n < 2**53 for n in profile))
        files = value['files']
        check(type(files) is list and 0 < len(files) <= MAX_FILES)
        names = []
        for row in files:
            check(type(row) is dict and set(row) == {'path', 'mode', 'sha256'} and source_path(row['path']))
            check(row['mode'] in ('100644', '100755') and type(row['sha256']) is str and len(row['sha256']) == 64
                  and all(c in '0123456789abcdef' for c in row['sha256']))
            names.append(row['path'])
        check(names == sorted(set(names)) and len({n.casefold() for n in names}) == len(names)
              and set((*REQUIRED, *SOURCE_FILES)) <= set(names))
        return json.loads(canonical(value))
    except SourceError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise SourceError() from None


def digest(value):
    return hashlib.sha256(canonical(validate_manifest(value))).hexdigest()


def declarations(raw, manifest):
    """Read literal revision/profile declarations without executing target source."""
    expected = {'OWNER_ID': OWNER_ID, 'OWNER_REVISION': OWNER_REVISION, 'SOURCE_POLICY': SOURCE_POLICY,
                'SOURCE_ROOTS': SOURCE_ROOTS, 'SOURCE_FILES': SOURCE_FILES, 'SOURCE_SUFFIXES': SOURCE_SUFFIXES,
                'MANIFEST_PATH': MANIFEST_PATH, 'MANIFEST_VERSION': MANIFEST_VERSION,
                'PROVIDER_PROFILE': tuple(manifest['provider_profile']), 'REQUIRED': REQUIRED}
    found, allowed = {}, set()
    try:
        tree = ast.parse(raw)
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in expected:
                    check(target.id not in found)
                    found[target.id] = ast.literal_eval(node.value)
                    allowed.add(target)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in expected and isinstance(node.ctx, (ast.Store, ast.Del)):
                check(node in allowed)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                check(node.name not in expected)
            if isinstance(node, ast.alias):
                check((node.asname or node.name.split('.')[0]) not in expected)
        check(set(found) == set(expected))
        for key, value in expected.items():
            actual = found[key]
            check(type(actual) is type(value))
            if type(value) is tuple:
                check(len(actual) == len(value) and all(type(a) is type(b) and a == b for a, b in zip(actual, value)))
            else:
                check(actual == value)
    except SourceError:
        raise
    except (SyntaxError, ValueError, TypeError, RecursionError):
        raise SourceError() from None


def _read(root, path, deadline):
    budget(deadline)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    stream = None
    try:
        parts = path.split('/')
        check(all(p not in ('', '.', '..') for p in parts))
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        stream = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(stream)
        check(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and before.st_nlink == 1 and before.st_size <= MAX_BYTES)
        raw = bytearray()
        while True:
            budget(deadline)
            part = os.read(stream, 65536)
            if not part:
                break
            raw.extend(part)
            check(len(raw) <= MAX_BYTES)
        after = os.fstat(stream)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_mode', 'st_uid', 'st_nlink')
        check(tuple(getattr(before, k) for k in fields) == tuple(getattr(after, k) for k in fields))
        return bytes(raw), '100755' if before.st_mode & stat.S_IXUSR else '100644'
    finally:
        if stream is not None:
            os.close(stream)
        os.close(directory)


def _git(repository, args, deadline):
    import owned_process
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(repository), 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_NO_LAZY_FETCH': '1', 'GIT_OPTIONAL_LOCKS': '0'}
    result = owned_process.run_owned(['/usr/bin/git', '-c', 'core.fsmonitor=false', '-c', 'protocol.allow=never',
        '-c', 'credential.helper=', '-c', 'core.hooksPath=/dev/null', '-C', str(repository), *args], min(30, budget(deadline)), env, deadline=deadline)
    budget(deadline)
    check(result.returncode == 0)
    return result.stdout


def build_manifest(repository, *, deadline):
    """Build review material from staged paths and actual bytes, never accept that material."""
    try:
        entries, total = [], 0
        for row in _git(repository, ['ls-files', '--stage', '-z'], deadline).split('\0'):
            if not row:
                continue
            meta, path = row.split('\t', 1)
            if not source_path(path):
                continue
            mode, _, stage = meta.split()
            check(stage == '0' and mode in ('100644', '100755'))
            raw, actual_mode = _read(repository, path, deadline)
            check(actual_mode == mode)
            total += len(raw)
            check(total <= MAX_TOTAL_BYTES and len(entries) < MAX_FILES)
            entries.append(dict(path=path, mode=mode, sha256=hashlib.sha256(raw).hexdigest()))
        value = dict(schema_version=MANIFEST_VERSION, owner_id=OWNER_ID, owner_revision=OWNER_REVISION,
            provider_profile=list(PROVIDER_PROFILE), source_policy=SOURCE_POLICY, files=sorted(entries, key=lambda r: r['path']))
        return validate_manifest(value)
    except SourceError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, subprocess.SubprocessError):
        raise SourceError() from None


def verify_checkout(repository, manifest, *, deadline, current=False):
    """Check bytes and declarations; qualification additionally needs independent root or Core proof."""
    try:
        value = validate_manifest(manifest)
        check(type(current) is bool)
        actual = build_manifest(repository, deadline=deadline)
        actual['provider_profile'] = value['provider_profile']
        check(canonical(actual) == canonical(value))
        raw, _ = _read(repository, 'mcs/ops/release_owner_source.py', deadline)
        declarations(raw, value)
        if current:
            check(tuple(value['provider_profile']) == PROVIDER_PROFILE)
        check(not _git(repository, ['status', '--porcelain', '--untracked-files=no'], deadline).strip())
        check(not any(source_path(p) for p in _git(repository, ['ls-files', '--others', '--exclude-standard', '-z'], deadline).split('\0') if p))
        return dict(manifest=value, sha256=digest(value), provider_profile=tuple(value['provider_profile']))
    except SourceError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, subprocess.SubprocessError):
        raise SourceError() from None


def verify_target(files, *, deadline):
    """Check only an already-authenticated artifact tree, without running candidate code."""
    try:
        check(type(files) is dict and MANIFEST_PATH in files)
        def read(row):
            check(type(row) is dict and type(row['size']) is int and 0 <= row['size'] <= MAX_BYTES)
            path = row['path']
            raw, _ = _read(path.parent, path.name, deadline)
            check(len(raw) == row['size'])
            return raw
        value = validate_manifest(read(files[MANIFEST_PATH]))
        names = sorted(path for path in files if source_path(path))
        check(names == [row['path'] for row in value['files']])
        total, provider_raw = 0, None
        for expected in value['files']:
            budget(deadline)
            row = files[expected['path']]
            raw = read(row)
            total += len(raw)
            check(total <= MAX_TOTAL_BYTES and row['mode'] == expected['mode']
                  and hashlib.sha256(raw).hexdigest() == expected['sha256'])
            if expected['path'].endswith('.py'):
                ast.parse(raw, filename=expected['path'])
                budget(deadline)
            if expected['path'] == 'mcs/ops/release_owner_source.py':
                provider_raw = raw
        declarations(provider_raw, value)
        return dict(manifest=value, sha256=digest(value), provider_profile=tuple(value['provider_profile']))
    except SourceError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, SyntaxError, RecursionError):
        raise SourceError() from None


def main(arguments=None):
    """Development metadata generation only; no installation, acceptance or key operation."""
    import argparse
    from pathlib import Path
    import sys
    parser = argparse.ArgumentParser(description='Build or check release-owner source review material.')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--write', action='store_true')
    action.add_argument('--check', action='store_true')
    options = parser.parse_args(arguments)
    repository = Path(__file__).resolve().parents[2]
    deadline = time.monotonic() + 120
    try:
        if options.write:
            value = build_manifest(repository, deadline=deadline)
            raw, _ = _read(repository, 'mcs/ops/release_owner_source.py', deadline)
            declarations(raw, value)
            sys.path.insert(0, str(repository / 'mcs'))
            import _mcs_path  # noqa: F401 - native flat import roots
            from safe_io import atomic_write
            atomic_write(str(repository / MANIFEST_PATH), lambda stream: stream.write(canonical(value).decode('ascii') + '\n'), mode=0o644)
        else:
            raw, _ = _read(repository, MANIFEST_PATH, deadline)
            value = validate_manifest(raw)
            verify_checkout(repository, value, deadline=deadline)
        print(digest(value))
        return 0
    except (SourceError, OSError):
        print('RELEASE_PROVIDER_SOURCE_HOLD')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
