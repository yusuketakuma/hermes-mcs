"""中断した導入設定の移行を確認し、明示操作で再開する。"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _mcs_path  # noqa: E402,F401
import config_edit  # noqa: E402
import install_options  # noqa: E402
import mcs_util  # noqa: E402
import release_apply_owner  # noqa: E402
import safe_io  # noqa: E402
import settings_migration_receipt as receipt  # noqa: E402
import update_journal  # noqa: E402


def _classification(config, legacy, evidence):
    raw, base = config_edit._snapshot(config)
    config_edit._parse(raw)
    central, _ = install_options._central(config)
    old, digest = install_options._legacy(legacy)
    if old is None or digest != evidence['legacy_sha256']:
        raise ValueError('legacy_changed')
    observed = install_options.inspect(config, legacy)
    if central is None and observed == evidence['base_sha256']:
        return 'not_published', raw, base
    if central is not None and central == old:
        return 'published', raw, base
    raise ValueError('settings_changed')


def _audit_path(root, evidence):
    return Path(root) / ('.install-options-migration.reconcile.' + evidence['operation'])


def _verify_lock(fd, path, data_identity):
    held, current, parent = os.fstat(fd), os.stat(path, follow_symlinks=False), os.stat(path.parent)
    if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid()
            or stat.S_IMODE(held.st_mode) != 0o600 or held.st_nlink != 1 or held.st_size != 0
            or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)
            or [parent.st_dev, parent.st_ino] != data_identity):
        raise ValueError('migration_lock_unverified')


def _acquire_lock(data, name, evidence):
    directory, identity = receipt._open_directory(data)
    try:
        if list(identity) != evidence['data']:
            raise ValueError('migration_data_changed')
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=directory)
        try:
            _verify_lock(fd, data / name, evidence['data'])
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return None
            _verify_lock(fd, data / name, evidence['data'])
            return fd
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(directory)


def _audit(root, evidence, state, reason):
    path = _audit_path(root, evidence)
    raw = (json.dumps({'v': 1, 'operation': evidence['operation'], 'state': state,
                       'base_sha256': evidence['base_sha256'],
                       'legacy_sha256': evidence['legacy_sha256'], 'reason': reason},
                      sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    try:
        safe_io.publish_new_bytes(path, raw)
    except FileExistsError:
        existing = safe_io.read_private_bytes(path, 4096)
        info = os.stat(path, follow_symlinks=False)
        if (existing != raw or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()):
            raise ValueError('reconcile_receipt_conflict') from None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            held = os.fstat(fd)
            if (held.st_dev, held.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError('reconcile_receipt_changed')
            os.fsync(fd)
        finally:
            os.close(fd)
        safe_io.fsync_dir(root)


def _check_update_journal(data):
    if any(os.path.lexists(data / name) for name in (
            'restore_pending.json', 'restore.json', 'backup_restore_approval.json',
            'backup_restore_resume.json', 'backup_restore_blocked.json')):
        raise ValueError('restore_hold')
    state = update_journal.load_state(data / 'update_state.json')
    if state.get('_corrupt') or state.get('applying') or state.get('restore_consent'):
        raise ValueError('update_journal_unverified')
    stages = state.get('stages', [])
    premerge = {'local_checks', 'candidate_verify', 'remote_verify', 'fetch',
                'tag_checks', 'backup', 'lock', 'settings_migration'}
    if stages and (stages[-1].get('stage') not in ('lock', 'settings_migration')
                   or any(row.get('stage') not in premerge for row in stages)):
        raise ValueError('update_journal_unverified')
    rows = [state, *stages, *state.get('applied', [])]
    if any('release_ref' in row for row in rows):
        raise ValueError('release_journal_hold')


def _check_release_control(root):
    directory = os.open(release_apply_owner._control_root(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in ('release-owner.json', 'release-owner-activation.json', 'release-bootstrap.json',
                     'release-planning.json', 'release-plan.json', 'release-change.json', 'release-successor.json'):
            try:
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError('release_control_hold')
    finally:
        os.close(directory)


def reconcile(root, reason, *, confirm_human=False):
    """Keep evidence until a same-value configuration has been synchronized."""
    if (confirm_human is not True or type(reason) is not str or not reason.strip()
            or reason != reason.strip() or len(reason.encode('utf-8')) > 512
            or any(ord(c) < 32 or ord(c) == 127 for c in reason)):
        raise ValueError('human_confirmation_required')
    root = Path(root).absolute()
    data, config = root / 'data', root / 'config.json'
    legacy = data / 'install_options.json'
    evidence, marker_identity = receipt.read_evidence(data)
    _classification(config, legacy, evidence)
    with release_apply_owner._legacy_scope(str(root)):
        _check_release_control(root)
        return _reconcile_locked(root, data, config, legacy, evidence, marker_identity, reason)


def _reconcile_locked(root, data, config, legacy, evidence, marker_identity, reason):
    update_fd = _acquire_lock(data, 'update.lock', evidence)
    if update_fd is None:
        raise ValueError('update_busy')
    run_fd = None
    try:
        _verify_lock(update_fd, data / 'update.lock', evidence['data'])
        _check_update_journal(data)
        run_fd = _acquire_lock(data, 'run.lock', evidence)
        if run_fd is None:
            raise ValueError('run_busy')
        _verify_lock(run_fd, data / 'run.lock', evidence['data'])
        with config_edit._edit_lock(config):
            if receipt.read_evidence(data) != (evidence, marker_identity):
                raise ValueError('migration_evidence_changed')
            state, raw, base = _classification(config, legacy, evidence)
            current = os.stat(config, follow_symlinks=False)
            if (current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) != 0o600
                    or current.st_nlink != 1 or config_edit._identity(current) != base[0]):
                raise ValueError('config_unverified')
            _audit(root, evidence, state, reason)
            if state == 'published':
                def write_checked(stream):
                    stream.write(raw)
                    if (config_edit._snapshot(config)[1] != base
                            or receipt.read_evidence(data) != (evidence, marker_identity)
                            or _classification(config, legacy, evidence)[0] != state):
                        raise ValueError('settings_changed')
                safe_io.atomic_write(str(config), write_checked, 0o600, binary=True, makedirs=False)
                if config_edit._snapshot(config)[0] != raw:
                    raise ValueError('config_unverified')
            if (receipt.read_evidence(data) != (evidence, marker_identity)
                    or _classification(config, legacy, evidence)[0] != state):
                raise ValueError('settings_changed')
            _verify_lock(update_fd, data / 'update.lock', evidence['data'])
            _verify_lock(run_fd, data / 'run.lock', evidence['data'])
            directory, root_identity = receipt._open_directory(root)
            try:
                current = os.stat(receipt.NAME, dir_fd=directory, follow_symlinks=False)
                marker_now = (current.st_dev, current.st_ino, current.st_mode, current.st_uid,
                              current.st_nlink, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
                if marker_now != marker_identity or list(root_identity) != evidence['root']:
                    raise ValueError('migration_evidence_changed')
                os.unlink(receipt.NAME, dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(directory)
            return state
    finally:
        if run_fd is not None:
            os.close(run_fd)
        os.close(update_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description='導入設定の移行保留を確認します。')
    parser.add_argument('--root', default=mcs_util.HOME)
    parser.add_argument('--confirm-human', action='store_true')
    parser.add_argument('--reason', default='', help='確認理由。患者情報や認証情報を含めないでください。')
    args = parser.parse_args(argv)
    try:
        if not args.confirm_human:
            evidence, _ = receipt.read_evidence(Path(args.root) / 'data')
            state, _, _ = _classification(Path(args.root) / 'config.json',
                                          Path(args.root) / 'data/install_options.json', evidence)
            print('導入設定の移行保留: ' + ('設定へのコピー前' if state == 'not_published'
                  else '設定の値は一致していますが、保存後の同期結果は不明です'))
            print('記録と設定を保持し、担当者が --confirm-human --reason 理由 を指定できます。'
                  'ほかの保留や競合を確認できない場合は停止します。')
            return 0
        reason = args.reason.strip()
        if not reason or len(reason.encode('utf-8')) > 512 or any(ord(c) < 32 for c in reason):
            raise ValueError('reason_invalid')
        reconcile(args.root, reason, confirm_human=True)
        print('導入設定の保留を解除しました。更新または導入をもう一度実行してください。')
        return 0
    except (KeyboardInterrupt, EOFError):
        print('\n移行保留の復旧を中断しました。設定と記録の保存状態を担当者に確認してください。', file=sys.stderr)
        return 130
    except (OSError, ValueError, TypeError, RecursionError):
        print('移行保留を安全に確認できません。設定と記録の保存状態を担当者に確認してください。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
