"""Acknowledge exact exhausted OCR alerts without adopting text or clearing source gaps."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: E402,F401

import attachment_ocr_ack as ack  # noqa: E402
from mcs_requests import canonical, payload_hash, valid_uuid  # noqa: E402
from mcs_util import strict_json, valid_hash, valid_text  # noqa: E402
from notify_cards import mark_snapshot_dirty  # noqa: E402
from safe_io import atomic_write, fsync_dir, read_regular_nofollow  # noqa: E402
from writer_lock import acquire_run_lock, code_stamp, wait_run_lock  # noqa: E402


def _validate(request):
    if (type(request) is not dict or set(request) != {
            'command_id', 'actor', 'reason', 'human_confirmed', 'plan'}
            or not valid_uuid(request.get('command_id'))
            or request.get('human_confirmed') is not True
            or not valid_text(request.get('actor'), 120)
            or not valid_text(request.get('reason'), 2000)):
        raise ValueError('ocr_ack_confirmation_required')
    plan = request['plan']
    if (type(plan) is not dict or set(plan) != {'version', 'action', 'database_identity', 'targets'}
            or type(plan.get('version')) is not int or plan['version'] != 1
            or plan.get('action') != ack.ACTION or not valid_hash(plan.get('database_identity'))
            or type(plan.get('targets')) is not list or not 1 <= len(plan['targets']) <= ack.MAX_TARGETS
            or any(type(item) is not dict or set(item) != {'artifact_id', 'proof'}
                   or type(item.get('artifact_id')) is not int or item['artifact_id'] <= 0
                   or not valid_hash(item.get('proof')) for item in plan['targets'])):
        raise ValueError('ocr_ack_plan_invalid')
    ids = [item['artifact_id'] for item in plan['targets']]
    if ids != sorted(set(ids)):
        raise ValueError('ocr_ack_plan_invalid')
    return ids


def _publish(archive, receipt):
    path = archive / 'receipt.json'
    if os.path.lexists(path):
        prior = strict_json(read_regular_nofollow(str(path), 65536, ValueError))
        if prior != receipt:
            raise ValueError('ocr_ack_receipt_conflict')
    else:
        atomic_write(str(path), lambda f: json.dump(receipt, f, ensure_ascii=False, indent=2), mode=0o600)
    fsync_dir(str(archive))


def _receipt(db, command_id, digest):
    row = db.execute('SELECT payload_hash,receipt_json FROM command_receipts WHERE command_id=?',
                     (command_id,)).fetchone()
    if row is None:
        return None
    if row[0] != digest:
        raise ValueError('command_id_conflict')
    try:
        receipt = strict_json(row[1])
    except (ValueError, TypeError, RecursionError):
        raise ValueError('ocr_ack_receipt_unverified') from None
    if (type(receipt) is not dict or receipt.get('kind') != ack.COMMAND
            or receipt.get('outcome') != 'applied' or receipt.get('payload_hash') != digest
            or receipt.get('command_id') != command_id):
        raise ValueError('ocr_ack_receipt_unverified')
    return receipt


def apply(root, request, *, wait_seconds=0):
    """Own locks, backup and one atomic acknowledgement/receipt transaction."""
    ids = _validate(request)
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 600:
        raise ValueError('bad_wait_seconds')
    data = Path(root) / 'data'
    archive = data / 'ocr_alert_acknowledgements' / request['command_id']
    if (data.is_symlink() or (data / 'ledger.db').is_symlink()
            or archive.parent.is_symlink() or archive.is_symlink()):
        raise ValueError('unsafe_data_path')
    digest = payload_hash(request)
    locks = []
    try:
        for name in ('update.lock', 'run.lock'):
            stamp = code_stamp() if name == 'run.lock' and wait_seconds else None
            fd = acquire_run_lock(str(data / name))
            if fd is None and stamp is not None:
                fd = wait_run_lock(str(data / name), wait_s=wait_seconds,
                    expected_code_stamp=stamp, marker_path=str(data / 'update_in_progress.marker'))
            if fd is None:
                raise ValueError('operation_lock_held')
            locks.append(fd)
        if any(os.path.lexists(data / name) for name in ('update_in_progress.marker', 'restore_pending.json')):
            raise ValueError('lifecycle_operation_pending')
        with closing(sqlite3.connect((data / 'ledger.db').resolve().as_uri() + '?mode=rw',
                                     uri=True, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            receipt = _receipt(db, request['command_id'], digest)
            replayed = receipt is not None
            if receipt is None:
                # A receipt file with no DB receipt can be a pre-ack restore.
                if os.path.lexists(archive / 'receipt.json'):
                    raise ValueError('ocr_ack_restore_conflict')
                db.execute('BEGIN')
                try:
                    if ack.build_plan(db, ids) != request['plan']:
                        raise ValueError('stale_plan')
                finally:
                    db.rollback()
                archive.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd, name = tempfile.mkstemp(prefix='before-', suffix='.db', dir=archive)
                os.close(fd)
                with closing(sqlite3.connect(name)) as backup:
                    db.backup(backup)
                    if backup.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('backup_verification_failed')
                with open(name, 'rb') as stream:
                    os.fsync(stream.fileno())
                fsync_dir(str(archive))
                db.execute('BEGIN IMMEDIATE')
                try:
                    if _receipt(db, request['command_id'], digest) is not None:
                        raise ValueError('ocr_ack_state_changed')
                    if ack.build_plan(db, ids) != request['plan']:
                        raise ValueError('stale_plan')
                    now = time.time()
                    acknowledgement_ids = []
                    for item in request['plan']['targets']:
                        source = db.execute('SELECT project_id,message_id FROM artifacts WHERE artifact_id=?',
                                            (item['artifact_id'],)).fetchone()
                        content = {'schema': ack.SCHEMA, 'target': item,
                                   'command_id': request['command_id'], 'request_hash': digest}
                        cursor = db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) '
                            'VALUES(?,?,?,?,?,?)', (ack.KIND, source[0], source[1], canonical(content).decode(),
                            canonical({'failure_artifact_id': item['artifact_id']}).decode(), now))
                        acknowledgement_ids.append(cursor.lastrowid)
                    receipt = {'kind': ack.COMMAND, 'command_id': request['command_id'],
                        'payload_hash': digest, 'outcome': 'applied', 'actor': request['actor'],
                        'reason': request['reason'], 'human_confirmed': True, 'processed_at': now,
                        'targets': request['plan']['targets'], 'acknowledgement_ids': acknowledgement_ids,
                        'backup': str(Path(name).relative_to(data)),
                        'ocr_outcome': 'failed', 'source_coverage': 'unrecovered',
                        'clinical_review': 'not_proven', 'original_file_availability': 'not_rechecked'}
                    db.execute('INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)',
                        (request['command_id'], digest, None, None, 'applied', canonical(receipt).decode(), now))
                    mark_snapshot_dirty(db)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            # Everything after DB commit is a receipt copy, never another ack.
            try:
                archive.mkdir(mode=0o700, parents=True, exist_ok=True)
                _publish(archive, receipt)
                state = 'saved'
            except (OSError, ValueError, RecursionError):
                state = 'pending'
            return {**receipt, 'receipt_state': state, 'replayed': replayed}
    finally:
        for fd in reversed(locks):
            os.close(fd)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        super().error('ocr_ack_arguments_invalid')


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    plan = sub.add_parser('plan')
    plan.add_argument('--artifact-id', type=int, action='append', required=True)
    apply_parser = sub.add_parser('apply')
    apply_parser.add_argument('--plan', type=Path, required=True)
    apply_parser.add_argument('--command-id', required=True)
    apply_parser.add_argument('--actor', required=True)
    apply_parser.add_argument('--reason', required=True)
    apply_parser.add_argument('--confirm-human', action='store_true')
    apply_parser.add_argument('--wait-seconds', type=int, default=0)
    args = parser.parse_args(argv)
    from mcs_runtime import sqlite_wal_safe
    try:
        if not sqlite_wal_safe(sqlite3.sqlite_version_info):
            raise ValueError('sqlite_runtime_unsafe')
        if args.action == 'plan':
            with closing(sqlite3.connect((args.root / 'data/ledger.db').resolve().as_uri() + '?mode=ro',
                                         uri=True, timeout=1)) as db:
                db.row_factory = sqlite3.Row
                db.execute('BEGIN')
                result = ack.build_plan(db, args.artifact_id)
        else:
            plan = strict_json(read_regular_nofollow(str(args.plan), 65536, ValueError))
            receipt = apply(args.root, {'command_id': args.command_id, 'actor': args.actor,
                'reason': args.reason, 'human_confirmed': args.confirm_human, 'plan': plan},
                wait_seconds=args.wait_seconds)
            result = {key: receipt[key] for key in ('outcome', 'command_id', 'receipt_state', 'replayed',
                                                    'ocr_outcome', 'source_coverage')}
            result['acknowledged'] = len(receipt['targets'])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ValueError as error:
        allowed = {'command_id_conflict', 'bad_wait_seconds', 'unsafe_data_path', 'stale_plan',
            'operation_lock_held', 'lifecycle_operation_pending', 'backup_verification_failed',
            'sqlite_runtime_unsafe', 'ocr_ack_confirmation_required', 'ocr_ack_plan_invalid',
            'ocr_ack_target_invalid', 'ocr_ack_target_changed', 'ocr_ack_source_unverified',
            'ocr_ack_database_unverified', 'ocr_ack_snapshot_required', 'ocr_ack_targets_invalid',
            'ocr_ack_already_acknowledged', 'ocr_ack_receipt_unverified', 'ocr_ack_restore_conflict',
            'ocr_ack_state_changed'}
        code = error.args[0] if len(error.args) == 1 and type(error.args[0]) is str else None
        print(json.dumps({'outcome': 'rejected', 'error': code if code in allowed else 'ocr_ack_invalid'}))
        return 2
    except (OSError, sqlite3.Error, RecursionError):
        # A commit may already exist: the same UUID must be inspected/replayed.
        print(json.dumps({'outcome': 'unknown', 'error': 'ocr_ack_operation_unverified'}))
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
