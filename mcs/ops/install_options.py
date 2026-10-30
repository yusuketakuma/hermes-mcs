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


def inspect(config_path, legacy_path, desired=None):
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


def main(argv):
    mode, config_path, legacy_path, expected, *desired = argv
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
