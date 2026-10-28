"""対話で外部解析のAPIキーを本人専用の環境ファイルへ保存します。"""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
import getpass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import warnings

import mcs_util
import safe_io

KEYS = (("TYPESAFE_API_KEY", "Jev"), ("MCS_ANTHROPIC_API_KEY", "Anthropic"))


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _snapshot(path):
    try:
        raw, info = safe_io.read_regular_nofollow(
            path, mcs_util.DOTENV_MAX_BYTES, lambda: ValueError('env_unreadable'), with_stat=True)
    except FileNotFoundError:
        if os.path.lexists(path):
            raise ValueError('env_unreadable') from None
        return b'', None
    current = os.lstat(path)
    if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1 or len(raw) > mcs_util.DOTENV_MAX_BYTES
            or _identity(current) != _identity(info)):
        raise ValueError('env_unreadable')
    raw.decode('utf-8')
    return raw, (_identity(info), hashlib.sha256(raw).digest())


def _merge(raw, updates):
    lines = raw.decode('utf-8').splitlines(keepends=True)
    kept = [line for line in lines
            if line.split('=', 1)[0].removeprefix('export ') not in updates]
    if kept and not kept[-1].endswith(('\n', '\r')):
        kept[-1] += '\n'
    for key, value in updates.items():
        encoded = json.dumps(value, ensure_ascii=False) if any(c in value for c in "\"'\\") else value
        kept.append(f'{key}={encoded}\n')
    body = ''.join(kept).encode('utf-8')
    if len(body) > mcs_util.DOTENV_MAX_BYTES:
        raise ValueError('env_too_large')
    return body


class EnvLockError(ValueError):
    pass


@contextmanager
def _env_edit_lock(path):
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise EnvLockError('env_parent_untrusted')
    fd = os.open(str(path) + '.edit.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size
                or _identity(os.stat(str(path) + '.edit.lock', follow_symlinks=False)) != _identity(info)):
            raise EnvLockError('env_lock_invalid')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if _identity(os.stat(str(path) + '.edit.lock', follow_symlinks=False)) != _identity(info):
            raise EnvLockError('env_lock_changed')
        yield
    finally:
        os.close(fd)


def _save(path, updates, expected):
    with _env_edit_lock(path):
        raw, base = _snapshot(path)
        if base != expected:
            raise ValueError('env_changed')
        body = _merge(raw, updates)
        def write_checked(stream):
            stream.write(body)
            if _snapshot(path)[1] != base:
                raise ValueError('env_changed')
        safe_io.atomic_write(str(path), write_checked, 0o600, binary=True,
                             tmp_prefix='.env.stage-', makedirs=False)


def _choose(prompt):
    while True:
        answer = input(prompt).strip()
        if answer in ('', '1', '2', '0'):
            return answer or '2'
        print('1、2、0 のいずれかを入力してください。')


def is_terminal():
    return sys.stdin.isatty() and sys.stdout.isatty()


def run(root=None):
    """非表示入力でAPIキーを選択し、既存の認証情報を保護して保存します。"""
    if not is_terminal():
        print('APIキーの設定は本人の端末で mcs setup api-keys を実行してください。')
        return 2
    root = Path(mcs_util.HOME if root is None else root)
    path = root / '.env'
    saving = False
    try:
        _, base = _snapshot(path)
        print('外部解析のAPIキーを設定します。キーは表示されず、このMCSの本人専用 .env に保存します。')
        print('有効な場合、JevやAnthropicへ投稿や患者の文脈が送られ、費用が発生することがあります。対象範囲と上限は設定で確認してください。')
        updates = {}
        for key, label in KEYS:
            problem = mcs_util.private_env_problem(key, root)
            status = '登録済み' if problem is None else '未登録' if problem in ('env_file_missing', 'key_missing') else '確認が必要'
            print(f'{label}: {status}\n  1) 入力・変更する\n  2) 現在のまま（推奨）\n  0) 中止する')
            choice = _choose('選択 [2]: ')
            if choice == '0':
                print('APIキーの変更は保存していません。')
                return 0
            if choice == '1':
                while True:
                    with warnings.catch_warnings():
                        warnings.simplefilter('error', getpass.GetPassWarning)
                        value = getpass.getpass(f'{label}のAPIキー（非表示、Ctrl+Cで中止）: ')
                    if value and not any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value):
                        updates[key] = value
                        break
                    print('空欄や空白・制御文字は使えません。もう一度入力してください。')
        if not updates:
            print('APIキーは変更していません。')
            return 0
        print('選んだAPIキーを保存します。既存のその他の認証情報は保持します。')
        if _choose('1) 保存する  2) 保存しない（推奨）  0) 中止 [2]: ') != '1':
            print('APIキーの変更は保存していません。')
            return 0
        saving = True
        _save(path, updates, base)
    except (KeyboardInterrupt, EOFError):
        print('\nAPIキーの保存結果を確認できません。既存の認証情報の状態を確認してください。'
              if saving else '\nAPIキーの変更は保存していません。')
        return 130
    except (OSError, ValueError, UnicodeError, getpass.GetPassWarning):
        print('APIキーの保存結果を確認できません。本人専用の保存先と現在の設定を確認してください。')
        return 1
    print('選んだAPIキーを保存しました。サービスの利用条件と設定を確認してください。')
    return 0
