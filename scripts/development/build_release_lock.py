#!/usr/bin/env python3
"""公開specとoffline wheelから依存lockを生成する（導入・解決はしない）。"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'mcs'))
import _mcs_path  # noqa: E402,F401 — shared flat module roots, without importing SDKs.

from safe_io import atomic_write, read_regular_nofollow
from release_dependency_lock import MAX_LOCK, LockError, build_lock, encoded


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--host-source')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        spec = read_regular_nofollow(args.spec, MAX_LOCK, LockError)
        lock = build_lock(spec, args.repo, args.host_source)
        atomic_write(args.output, lambda stream: stream.write(encoded(lock)), mode=0o600,
                     binary=True, makedirs=False)
    except (OSError, ValueError, TimeoutError):
        print('依存lockを生成できません。公開spec・固定source・wheelを確認してください。', file=sys.stderr)
        return 1
    print('依存lockを生成しました。依存解決・導入・真正な基線の確認は別途必要です。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
