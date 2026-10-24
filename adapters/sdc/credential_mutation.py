"""Validate one private credential mutation and checkpoint its native completion."""
import hashlib
import json
import os
import re
import secrets
import stat
import uuid

from .keychain import KeychainError
from .protocol import strict_json

NAME = 'credential-store.json'
SCHEMA = 'sdc.credential-store.v3'
MAX_CREDENTIAL = 4096
# Full replacement context and reconcile receipt, two 128-char maximally escaped reasons, all safe integers.
MAX_MARKER = 2165
MAX_INTEGER = 2**53 - 1
_BASE = {'schema_version', 'mode', 'account', 'root_fingerprint', 'endpoint_hash'}
_IDS = {'claim_id', 'device_id', 'key_id', 'rotation_id'}
PURPOSES = frozenset({'initial-key', 'claim-key-id', 'reenroll-key', 'rotation-candidate',
                      'rotation-replacement', 'rotation-key-id', 'rotation-finalize', 'delete'})


def _invalid():
    raise ValueError('SDC_CREDENTIAL_STORE_INVALID')


def _uuid(value):
    try:
        identifier = uuid.UUID(value) if type(value) is str else None
        return identifier is not None and identifier.version == 4 and str(identifier) == value
    except ValueError:
        return False


def _hash(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _integer(value):
    return type(value) is int and 0 <= value <= MAX_INTEGER


def _bytes(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('ascii')


def credential_value(raw):
    """Return the existing credential shape; hashes include every credential field."""
    try:
        if type(raw) is not bytes or not 1 <= len(raw) <= MAX_CREDENTIAL:
            raise ValueError
        value = strict_json(raw)
        fields = {'key', 'key_id', 'next_key', 'next_key_id', 'completed_rotation_id'}
        if type(value) is not dict or 'key' not in value or not set(value) <= fields:
            raise ValueError
        if any(name in value and not _hash(value[name]) for name in ('key', 'next_key')):
            raise ValueError
        if any(name in value and not _uuid(value[name])
               for name in ('key_id', 'next_key_id', 'completed_rotation_id')):
            raise ValueError
        return value
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise KeychainError('SDC_KEYCHAIN_INVALID_VALUE') from None


def credential_bytes(value):
    try:
        raw = _bytes(value)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise KeychainError('SDC_KEYCHAIN_INVALID_VALUE') from None
    credential_value(raw)
    return raw


def credential_digest(value):
    return hashlib.sha256(credential_bytes(value)).hexdigest()


def _context(value, purpose):
    if type(value) is not dict or set(value) != {'before', 'after', 'sequence', 'sending_sha256', 'stop_before_sha256'}:
        _invalid()
    before, after = value['before'], value['after']
    for item in (before, after):
        if (type(item) is not dict or set(item) != _IDS
                or any(v is not None and not _uuid(v) for v in item.values())):
            _invalid()
    if (not _integer(value['sequence']) or not _hash(value['stop_before_sha256'])
            or value['sending_sha256'] is not None and not _hash(value['sending_sha256'])):
        _invalid()
    identity = ('claim_id', 'device_id', 'key_id')
    same_identity = all(before[k] == after[k] for k in identity)
    active = all(before[k] is not None for k in identity)
    valid = {
        'initial-key': all(v is None for v in before.values()) and after['claim_id'] is not None
                       and all(after[k] is None for k in ('device_id', 'key_id', 'rotation_id')),
        'claim-key-id': before['claim_id'] is not None and before['claim_id'] == after['claim_id']
                        and after['device_id'] is not None and after['key_id'] is not None
                        and before['rotation_id'] is None and after['rotation_id'] is None
                        and all(before[k] is None or before[k] == after[k] for k in ('device_id', 'key_id')),
        'reenroll-key': after['claim_id'] is not None and after['claim_id'] != before['claim_id']
                        and all(after[k] is None for k in ('device_id', 'key_id', 'rotation_id')),
        'rotation-candidate': active and same_identity and before['rotation_id'] is None and after['rotation_id'] is not None,
        'rotation-replacement': active and same_identity and before['rotation_id'] is not None
                                and after['rotation_id'] is not None and before['rotation_id'] != after['rotation_id'],
        'rotation-key-id': active and before == after and before['rotation_id'] is not None,
        'rotation-finalize': active and same_identity and before['rotation_id'] is not None and after['rotation_id'] is None,
        'delete': before == after,
    }
    if not valid[purpose]:
        _invalid()


def validate_marker(value):
    """Validate local structure only; caller owns current root, consent and lock authority."""
    if type(value) is not dict:
        _invalid()
    version = value.get('schema_version')
    fields = _BASE if version == 'sdc.credential-store.v2' else _BASE | {'tick_lock_fingerprint', 'mutation'}
    if version == SCHEMA and 'reconcile' in value:
        fields = fields | {'reconcile'}  # Optional: absent in every marker written before a reconcile.
    if (version not in ('sdc.credential-store.v2', SCHEMA) or set(value) != fields
            or value['mode'] != 'keychain' or not _uuid(value['account'])
            or not _hash(value['root_fingerprint']) or not _hash(value['endpoint_hash'])):
        _invalid()
    if version == SCHEMA:
        if not _hash(value['tick_lock_fingerprint']):
            _invalid()
        mutation = value['mutation']
        if mutation is not None:
            fields = {'id', 'op', 'purpose', 'phase', 'old_sha256', 'new_sha256', 'started_at',
                      'context', 'approval', 'verified_at', 'completed_at'}
            if type(mutation) is not dict or set(mutation) != fields:
                _invalid()
            purpose, phase = mutation['purpose'], mutation['phase']
            if (not _uuid(mutation['id']) or type(purpose) is not str or purpose not in PURPOSES
                    or type(phase) is not str or phase not in ('pending', 'verified', 'complete')
                    or mutation['op'] != ('delete' if purpose == 'delete' else 'write')
                    or not _integer(mutation['started_at'])):
                _invalid()
            old, new = mutation['old_sha256'], mutation['new_sha256']
            if (old is not None and not _hash(old) or purpose == 'initial-key' and old is not None
                    or purpose not in ('initial-key', 'delete') and old is None
                    or purpose == 'delete' and new is not None or purpose != 'delete' and not _hash(new)):
                _invalid()
            _context(mutation['context'], purpose)
            approval = mutation['approval']
            if (type(approval) is not dict or set(approval) != {'receipt_id', 'confirmed_at', 'reason'}
                    or not _uuid(approval['receipt_id']) or not _integer(approval['confirmed_at'])
                    or approval['confirmed_at'] > mutation['started_at']
                    or type(approval['reason']) is not str
                    or re.fullmatch('[ -~]{1,128}', approval['reason']) is None):
                _invalid()
            verified, complete = mutation['verified_at'], mutation['completed_at']
            if phase == 'pending':
                if verified is not None or complete is not None:
                    _invalid()
            elif not _integer(verified) or verified < mutation['started_at']:
                _invalid()
            elif phase == 'verified':
                if complete is not None:
                    _invalid()
            elif not _integer(complete) or complete < verified:
                _invalid()
        if 'reconcile' in value:
            # The latest explicit reconcile approval of this same verified/complete intent only.
            receipt = value['reconcile']
            if (mutation is None or mutation['phase'] == 'pending' or type(receipt) is not dict
                    or set(receipt) != {'intent_id', 'receipt_id', 'confirmed_at', 'reason'}
                    or receipt['intent_id'] != mutation['id'] or not _uuid(receipt['receipt_id'])
                    or not _integer(receipt['confirmed_at']) or type(receipt['reason']) is not str
                    or re.fullmatch('[ -~]{1,128}', receipt['reason']) is None):
                _invalid()
    try:
        if len(_bytes(value)) > MAX_MARKER:
            _invalid()
    except (TypeError, OverflowError, RecursionError):
        _invalid()
    return value


def marker_sha256(value):
    return hashlib.sha256(_bytes(validate_marker(value))).hexdigest()


def _root(fd):
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        _invalid()


def _private(fd, maximum):
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600
            or st.st_nlink != 1 or st.st_size > maximum):
        _invalid()
    return st


def tick_lock_fingerprint(fd):
    st = _private(fd, 0)
    return hashlib.sha256(_bytes({'device': st.st_dev, 'inode': st.st_ino, 'uid': st.st_uid})).hexdigest()


def read_marker(root_fd):
    _root(root_fd)
    try:
        fd = os.open(NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    except FileNotFoundError:
        return None
    try:
        _private(fd, MAX_MARKER)
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_MARKER + 1)
        if not 1 <= len(raw) <= MAX_MARKER:
            _invalid()
        try:
            return validate_marker(strict_json(raw))
        except (ValueError, TypeError, RecursionError):
            _invalid()
    finally:
        os.close(fd)


def pending_marker(root_fd, tick_fd, *, intent_id, marker_sha256, account):
    """Check the fixed pending record and named lease target without taking another lock."""
    marker = read_marker(root_fd)
    if (marker is None or marker['schema_version'] != SCHEMA or marker['account'] != account
            or marker['mutation'] is None or marker['mutation']['id'] != intent_id
            or marker['mutation']['phase'] != 'pending'
            or hashlib.sha256(_bytes(marker)).hexdigest() != marker_sha256
            or marker['tick_lock_fingerprint'] != tick_lock_fingerprint(tick_fd)):
        _invalid()
    fd = os.open('tick.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    try:
        if tick_lock_fingerprint(fd) != marker['tick_lock_fingerprint']:
            _invalid()
    finally:
        os.close(fd)
    return marker


def write_marker(root_fd, value, *, expected_sha256):
    """CAS only the fixed marker; callers retain ownership and poison uncertain writes."""
    raw = _bytes(validate_marker(value))
    _root(root_fd)
    if expected_sha256 is None:
        fd = os.open(NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
        try:
            _private(fd, 0)
        finally:
            os.close(fd)
        os.fsync(root_fd)  # Failed reservation remains an invalid guard, never fresh.
    elif not _hash(expected_sha256) or marker_sha256(read_marker(root_fd)) != expected_sha256:
        _invalid()
    temp = '.sdc-' + secrets.token_hex(16)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if expected_sha256 is not None and marker_sha256(read_marker(root_fd)) != expected_sha256:
            _invalid()
        os.replace(temp, NAME, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
        actual = read_marker(root_fd)
        if actual != value:
            _invalid()
        return actual
    finally:
        try:
            os.unlink(temp, dir_fd=root_fd)
        except FileNotFoundError:
            pass


def mark_native_verified(root_fd, tick_fd, *, intent_id, marker_sha256, now_ms):
    """Checkpoint only after all native calls and cleanup; never run native afterward."""
    marker = read_marker(root_fd)
    if marker is None:
        _invalid()
    marker = pending_marker(root_fd, tick_fd, intent_id=intent_id,
                            marker_sha256=marker_sha256, account=marker['account'])
    value = {**marker, 'mutation': {**marker['mutation'], 'phase': 'verified', 'verified_at': now_ms}}
    return write_marker(root_fd, value, expected_sha256=marker_sha256)
