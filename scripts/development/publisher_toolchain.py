#!/usr/bin/env python3
"""publisher toolchainの実測lock生成・照合 (M9/W12)。

Git・gh・Pythonの実バージョンと配布digestを`publisher-toolchain.lock`へ
記録する。値は全て実測 — 根拠のないSHAやversionを捏造しない
(01_GITHUB_PUBLICATION §3.3)。ghに`release verify`/`release verify-asset`
の能力がなければfail closedでlockを作らない。
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time

LOCK_SCHEMA = 'mcs.publisher-toolchain.v1'


def _fail(code, detail=None):
    out = {'ok': False, 'code': code}
    if detail:
        out['detail'] = detail
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 2


def _run(argv, timeout=15):
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc


def _version(path):
    proc = _run([path, '--version'])
    if proc is None or proc.returncode != 0:
        return None
    line = proc.stdout.decode('utf-8', 'replace').splitlines()
    return line[0].strip() if line else None


def _sha256(path):
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _probe_tool(name, path):
    version = _version(path)
    digest = _sha256(path)
    if version is None or digest is None:
        raise ValueError(f'{name}:version-or-digest-unavailable')
    return {'path': path, 'version': version, 'sha256': digest}


def _gh_capabilities(gh_path):
    """gh release verify/verify-assetの存在を--help終了コードで確認する。"""
    caps = {}
    for name, sub in (('release_verify', 'verify'),
                      ('release_verify_asset', 'verify-asset')):
        proc = _run([gh_path, 'release', sub, '--help'])
        caps[name] = bool(proc is not None and proc.returncode == 0)
    return caps


def measure():
    """git/gh/pythonを実測する。欠落・能力不足はValueErrorでfail closed。"""
    tools = {}
    for name in ('git', 'gh'):
        path = shutil.which(name)
        if path is None:
            raise ValueError(f'{name}:not-found')
        tools[name] = _probe_tool(name, path)
    python = sys.executable
    tools['python'] = _probe_tool('python', python)
    caps = _gh_capabilities(tools['gh']['path'])
    if not all(caps.values()):
        raise ValueError('gh:verify-capability-missing')
    return {'schema_version': LOCK_SCHEMA, 'measured_at': int(time.time()),
            'tools': tools, 'gh_capabilities': caps}


def _comparable(lock):
    return {k: lock[k] for k in ('schema_version', 'tools',
                                 'gh_capabilities')}


def cmd_measure(args):
    try:
        lock = measure()
    except ValueError as e:
        return _fail('TOOLCHAIN_UNAVAILABLE', str(e))
    raw = json.dumps(lock, ensure_ascii=False, indent=2,
                     sort_keys=True) + '\n'
    if args.output == '-':
        sys.stdout.write(raw)
    else:
        try:
            with open(args.output, 'x', encoding='utf-8') as f:
                f.write(raw)
        except FileExistsError:
            return _fail('LOCK_EXISTS', args.output)
        except OSError:
            return _fail('WRITE_FAILED', args.output)
    print(json.dumps({'ok': True, 'output': args.output,
                      'tools': sorted(lock['tools'])}, sort_keys=True))
    return 0


def cmd_check(args):
    try:
        with open(args.lock, encoding='utf-8') as f:
            recorded = json.load(f)
    except (OSError, ValueError):
        return _fail('LOCK_UNREADABLE', args.lock)
    if not isinstance(recorded, dict) \
            or recorded.get('schema_version') != LOCK_SCHEMA:
        return _fail('LOCK_SCHEMA', args.lock)
    try:
        current = measure()
    except ValueError as e:
        return _fail('TOOLCHAIN_UNAVAILABLE', str(e))
    diffs = [k for k in ('tools', 'gh_capabilities')
             if _comparable(current)[k] != _comparable(recorded).get(k)]
    if diffs:
        return _fail('TOOLCHAIN_MISMATCH', ','.join(sorted(diffs)))
    print(json.dumps({'ok': True, 'lock': args.lock}, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='publisher-toolchain')
    sub = parser.add_subparsers(dest='command', required=True)
    m = sub.add_parser('measure', help='toolchainを実測してlockを書く')
    m.add_argument('--output', required=True,
                   help='lockの出力path("-"でstdout)')
    m.set_defaults(fn=cmd_measure)
    c = sub.add_parser('check', help='現在のtoolchainがlockと一致するか照合')
    c.add_argument('--lock', required=True)
    c.set_defaults(fn=cmd_check)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == '__main__':
    sys.exit(main())
