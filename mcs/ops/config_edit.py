"""Validated, atomic edits of selected config.json keys from the command line."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

import safe_io
import mcs_util


class ConfigConflict(ValueError):
    """Another configuration writer owns or changed this edit's base."""


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _snapshot():
    path = Path(mcs_util.CONF_PATH)
    raw, info = safe_io.read_regular_nofollow(path, mcs_util.CONFIG_MAX_BYTES,
                                            lambda: ValueError("config_unreadable"), with_stat=True)
    if len(raw) > mcs_util.CONFIG_MAX_BYTES:
        raise ValueError("config_unreadable")
    current = path.lstat()
    if not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(info):
        raise ConfigConflict("config_changed")
    return raw, (_identity(info), hashlib.sha256(raw).digest())


def _parse(raw):
    cfg = mcs_util.parse_config(raw)
    if not isinstance(cfg, dict):
        raise ValueError("config_unreadable")
    return cfg


def read_strict() -> dict:
    """Read one bounded regular no-follow descriptor, refusing an unstable pathname."""
    return _parse(_snapshot()[0])


@contextmanager
def _edit_lock():
    # Shared by the two new CLI editors, never across network or human input.
    # Older writers do not use this lock; the base snapshot below detects
    # their observed changes. It cannot exclude an external rename after
    # the last comparison and before atomic_write's final os.replace.
    path = str(mcs_util.CONF_PATH) + ".edit.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size):
            raise ValueError("config_lock_invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigConflict("config_busy") from None
        if _identity(os.stat(path, follow_symlinks=False)) != _identity(info):
            raise ConfigConflict("config_lock_changed")
        yield
    finally:
        os.close(fd)


def apply(changes: dict) -> tuple[dict | None, list[str]]:
    """Validate the whole edited config and atomically save against its locked base.

    Every known schema error blocks publication, including errors outside
    the edited section.
    """
    from mcs_config import validate_config
    with _edit_lock():
        raw, base = _snapshot()
        cfg = _parse(raw)
        for dotted, value in changes.items():
            section, key = dotted.split(".", 1)
            block = cfg.get(section, {})
            if not isinstance(block, dict):
                raise ValueError("config_section_invalid")
            block = dict(block)
            if value is None:
                block.pop(key, None)
            else:
                block[key] = value
            cfg[section] = block
        errors, _ = validate_config(cfg)
        if errors:  # unknown keys remain warnings and are preserved verbatim
            return None, errors
        body = json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if len(body.encode("utf-8")) > mcs_util.CONFIG_MAX_BYTES:
            raise ValueError("config_unreadable")

        def write_checked(stream):
            stream.write(body)
            if _snapshot()[1] != base:
                raise ConfigConflict("config_changed")
        safe_io.atomic_write(mcs_util.CONF_PATH, write_checked, 0o600,
                             tmp_prefix=".svc.", makedirs=False)
        return cfg, []


def _on_off(text):
    value = {"on": True, "true": True, "off": False, "false": False}.get(text.strip().lower())
    if value is None:
        raise ValueError
    return value


def settings_command(args) -> int:
    """Show or set extract.prefilter / local_llm.disk_guard_mb / local_llm.admission."""
    try:
        changes = {}
        if args.prefilter is not None:
            changes["extract.prefilter"] = _on_off(args.prefilter)
        if args.admission is not None:
            changes["local_llm.admission"] = _on_off(args.admission)
        if args.disk_guard_mb is not None:
            guard = float(args.disk_guard_mb)
            if not guard >= 0 or guard == float("inf"):
                raise ValueError
            changes["local_llm.disk_guard_mb"] = int(guard) if guard.is_integer() else guard
    except ValueError:
        print("設定: --prefilter・--admission は on/off、--disk-guard-mb は0以上の数で指定してください")
        return 1
    try:
        if changes:
            _, errors = apply(changes)
            if errors:
                print("設定に誤りがあるため保存していません。mcs_setup.py check で確認してください")
                return 1
        else:
            from mcs_config import validate_config
            if validate_config(read_strict())[0]:
                print("設定に誤りがあるため表示していません。mcs_setup.py check で確認してください")
                return 1
    except ConfigConflict:
        print("設定: ほかの設定保存と競合したため変更していません。設定を再確認して再試行してください")
        return 1
    except FileNotFoundError:
        print("設定: 設定ファイルがないため変更していません（先に mcs_setup.py init で設定してください）")
        return 1
    except (OSError, ValueError, RecursionError):
        print("設定: 設定ファイルを読み取れないため変更していません。mcs_setup.py check で確認してください")
        return 1
    import llm_availability
    import local_llm
    import extract_llm
    print(json.dumps({"prefilter": extract_llm._prefilter_enabled(),
                      "disk_guard_mb": llm_availability.disk_floor_mb(),
                      "admission": local_llm.admission_enabled(),
                      "saved": bool(changes)}, ensure_ascii=False))
    return 0


def add_parsers(sub) -> None:
    p = sub.add_parser("settings", help="抽出の事前判定・ディスク下限・LLM受付制御を表示・設定します")
    p.add_argument("--prefilter", help="on/off: 挨拶だけの投稿などをLLMに送らない事前判定")
    p.add_argument("--disk-guard-mb", dest="disk_guard_mb", help="空きがこのMB未満ならローカルLLMを止める（0で無効）")
    p.add_argument("--admission", help="on/off: ローカルLLMの共有受付制御（既定off）")
    p.set_defaults(fn=settings_command)
