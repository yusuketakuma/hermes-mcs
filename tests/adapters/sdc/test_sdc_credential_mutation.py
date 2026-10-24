"""Independent synthetic contracts for the one private native-completion marker."""
from copy import deepcopy
import hashlib
import json
import os

import pytest

from adapters.sdc import credential_mutation as cm
from adapters.sdc.keychain import KeychainError

ACCOUNT = '00000000-0000-4000-8000-000000000001'
CLAIM = '00000000-0000-4000-8000-000000000002'
DEVICE = '00000000-0000-4000-8000-000000000003'
KEY_ID = '00000000-0000-4000-8000-000000000004'
INTENT = '00000000-0000-4000-8000-000000000005'
ROTATION = '00000000-0000-4000-8000-000000000006'
OTHER = '00000000-0000-4000-8000-000000000007'


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def marker_value(*, purpose='initial-key', phase='pending', fingerprint='d' * 64):
    identity = {'claim_id': CLAIM, 'device_id': DEVICE, 'key_id': KEY_ID, 'rotation_id': None}
    before, after = dict(identity), dict(identity)
    if purpose == 'initial-key':
        before = dict.fromkeys(identity)
        after = {**before, 'claim_id': CLAIM}
    elif purpose == 'claim-key-id':
        before = {**identity, 'device_id': None, 'key_id': None}
    elif purpose == 'reenroll-key':
        after = {**dict.fromkeys(identity), 'claim_id': OTHER}
    elif purpose == 'rotation-candidate':
        after['rotation_id'] = ROTATION
    elif purpose == 'rotation-replacement':
        before['rotation_id'], after['rotation_id'] = ROTATION, OTHER
    elif purpose == 'rotation-key-id':
        before['rotation_id'] = after['rotation_id'] = ROTATION
    elif purpose == 'rotation-finalize':
        before['rotation_id'] = ROTATION
    return {'schema_version': 'sdc.credential-store.v3', 'mode': 'keychain', 'account': ACCOUNT,
            'root_fingerprint': 'a' * 64, 'endpoint_hash': 'b' * 64, 'tick_lock_fingerprint': fingerprint,
            'mutation': {'id': INTENT, 'op': 'delete' if purpose == 'delete' else 'write',
                         'purpose': purpose, 'phase': phase,
                         'old_sha256': None if purpose == 'initial-key' else digest({'key': '01' * 32}),
                         'new_sha256': None if purpose == 'delete' else digest({'key': '02' * 32}),
                         'started_at': 1000, 'context': {'before': before, 'after': after, 'sequence': 0,
                         'sending_sha256': None, 'stop_before_sha256': 'c' * 64},
                         'approval': {'receipt_id': OTHER, 'confirmed_at': 900, 'reason': 'synthetic approval'},
                         'verified_at': None if phase == 'pending' else 1100,
                         'completed_at': 1200 if phase == 'complete' else None}}


@pytest.fixture
def private_marker(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    tick_fd = os.open('tick.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root_fd)
    try:
        # Fingerprint oracle is specified independently from the production helper.
        st = os.fstat(tick_fd)
        value = marker_value(fingerprint=digest({'device': st.st_dev, 'inode': st.st_ino, 'uid': st.st_uid}))
        fd = os.open('credential-store.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root_fd)
        with os.fdopen(fd, 'wb') as target:
            target.write(canonical(value))
        yield root, root_fd, tick_fd, value
    finally:
        os.close(tick_fd)
        os.close(root_fd)


@pytest.mark.parametrize('purpose', sorted(cm.PURPOSES))
@pytest.mark.parametrize('phase', ['pending', 'verified', 'complete'])
def test_closed_purpose_and_phase_shapes_do_not_mutate_input(purpose, phase):
    value = marker_value(purpose=purpose, phase=phase)
    before = deepcopy(value)
    assert cm.validate_marker(value) == before
    assert cm.marker_sha256(value) == digest(before)
    assert value == before


@pytest.mark.parametrize('path,value', [
    (('extra',), 'synthetic secret'), (('schema_version',), 'sdc.credential-store.v4'),
    (('account',), '0000000A-0000-4000-8000-000000000002'), (('mode',), 'file'), (('tick_lock_fingerprint',), 'F' * 64),
    (('mutation', 'id'), '00000000-0000-1000-8000-000000000001'),
    (('mutation', 'op'), 'delete'), (('mutation', 'purpose'), 'arbitrary'),
    (('mutation', 'phase'), 'unknown'), (('mutation', 'old_sha256'), 'a' * 64),
    (('mutation', 'new_sha256'), None), (('mutation', 'started_at'), True),
    (('mutation', 'started_at'), 2**53), (('mutation', 'verified_at'), 1100),
    (('mutation', 'context', 'after', 'claim_id'), None),
    (('mutation', 'context', 'after', 'extra'), None),
    (('mutation', 'context', 'sequence'), -1), (('mutation', 'context', 'sequence'), 1.0),
    (('mutation', 'context', 'sending_sha256'), False),
    (('mutation', 'approval', 'confirmed_at'), 1001),
    (('mutation', 'approval', 'reason'), 'bad\nreason'),
    (('mutation', 'approval', 'reason'), 'x' * 129),
])
def test_invalid_closed_fields_are_rejected_with_fixed_code(path, value):
    marker = marker_value()
    target = marker
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match='^SDC_CREDENTIAL_STORE_INVALID$'):
        cm.validate_marker(marker)


def test_all_fields_and_maximum_json_escaping_fit_derived_marker_bound():
    value = marker_value(purpose='rotation-replacement', phase='complete')
    item = value['mutation']
    item['started_at'] = item['verified_at'] = item['completed_at'] = 9007199254740991
    item['approval'].update(confirmed_at=9007199254740991, reason='"' * 128)
    item['context'].update(sequence=9007199254740991, sending_sha256='f' * 64)
    # All IDs/hashes have fixed widths; replacement has the longest purpose and
    # fills both four-ID frames, both hashes and all three integer timestamps.
    expected = canonical(value)
    assert len(expected) == 1748
    assert cm.validate_marker(value) == value
    # The optional latest reconcile receipt is the only other field; with it the bound is exact.
    value['reconcile'] = {'intent_id': INTENT, 'receipt_id': OTHER, 'confirmed_at': 9007199254740991,
                          'reason': '"' * 128}
    assert len(canonical(value)) == cm.MAX_MARKER == 2165
    assert cm.validate_marker(value) == value


@pytest.mark.parametrize('change', ['pending', 'other-intent', 'extra-field', 'bad-reason', 'v2'])
def test_reconcile_receipt_is_closed_and_bound_to_the_same_unpending_intent(change):
    value = marker_value(phase='verified')
    value['reconcile'] = {'intent_id': INTENT, 'receipt_id': OTHER, 'confirmed_at': 1300, 'reason': 'synthetic'}
    assert cm.validate_marker(value) == value
    if change == 'pending':
        value = {**marker_value(), 'reconcile': value['reconcile']}
    elif change == 'other-intent':
        value['reconcile']['intent_id'] = OTHER
    elif change == 'extra-field':
        value['reconcile']['extra'] = None
    elif change == 'bad-reason':
        value['reconcile']['reason'] = 'line\nbreak'
    else:
        value = {key: item for key, item in value.items() if key not in ('mutation', 'tick_lock_fingerprint')}
        value['schema_version'] = 'sdc.credential-store.v2'
    with pytest.raises(ValueError, match='^SDC_CREDENTIAL_STORE_INVALID$'):
        cm.validate_marker(value)


def test_v2_compatibility_does_not_invent_mutation_or_tick_binding():
    value = {key: item for key, item in marker_value().items() if key not in ('mutation', 'tick_lock_fingerprint')}
    value['schema_version'] = 'sdc.credential-store.v2'
    assert cm.validate_marker(value) == value and 'mutation' not in value


@pytest.mark.parametrize('raw', [b'[]', b'{"key":"bad"}', b'{"key":"1","key":"2"}',
                                b'\xef\xbb\xbf{}', b' ' * 4097, b'[' * 1000 + b']' * 1000])
def test_credential_bad_raw_has_fixed_error(raw):
    with pytest.raises(KeychainError, match='^SDC_KEYCHAIN_INVALID_VALUE$'):
        cm.credential_value(raw)


def test_credential_whole_value_hash_and_canonical_order():
    value = {'key_id': KEY_ID, 'key': '01' * 32}
    assert cm.credential_bytes(value) == canonical(value)
    assert cm.credential_value(b' ' + canonical(value) + b' ') == value
    assert cm.credential_digest(value) == digest(value)
    assert cm.credential_digest(value) != cm.credential_digest({'key': value['key']})


def test_checkpoint_changes_only_native_phase_after_exact_cas(private_marker):
    _, root_fd, tick_fd, value = private_marker
    assert cm.pending_marker(root_fd, tick_fd, intent_id=INTENT, marker_sha256=digest(value), account=ACCOUNT) == value
    actual = cm.mark_native_verified(root_fd, tick_fd, intent_id=INTENT, marker_sha256=digest(value), now_ms=1100)
    expected = {**value, 'mutation': {**value['mutation'], 'phase': 'verified', 'verified_at': 1100}}
    assert actual == expected == cm.read_marker(root_fd)
    assert os.fstat(root_fd) and os.fstat(tick_fd)
    with pytest.raises(ValueError, match='SDC_CREDENTIAL_STORE_INVALID'):
        cm.mark_native_verified(root_fd, tick_fd, intent_id=INTENT, marker_sha256=digest(value), now_ms=1200)


@pytest.mark.parametrize('changed', ['id', 'digest', 'account', 'tick-replaced', 'tick-missing'])
def test_pending_requires_exact_intent_and_existing_lock_inode(private_marker, changed):
    root, root_fd, tick_fd, value = private_marker
    if changed.startswith('tick-'):
        (root / 'tick.lock').unlink()
        if changed == 'tick-replaced':
            replacement = os.open(root / 'tick.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(replacement)
    with pytest.raises((ValueError, FileNotFoundError)):
        cm.pending_marker(root_fd, tick_fd, intent_id=OTHER if changed == 'id' else INTENT,
                          marker_sha256='0' * 64 if changed == 'digest' else digest(value),
                          account=OTHER if changed == 'account' else ACCOUNT)
    assert cm.read_marker(root_fd) == value


@pytest.mark.parametrize('site', ['file-fsync', 'replace', 'directory-fsync', 'readback'])
def test_checkpoint_failure_never_rolls_back_visible_native_proof(private_marker, monkeypatch, site):
    _, root_fd, tick_fd, value = private_marker
    error = TimeoutError('synthetic deadline')
    original_fsync, original_replace, original_read = cm.os.fsync, cm.os.replace, cm.read_marker
    reads = []

    def fsync(fd):
        if site == ('directory-fsync' if fd == root_fd else 'file-fsync'):
            raise error
        return original_fsync(fd)

    def replace(*args, **kwargs):
        if site == 'replace':
            raise error
        return original_replace(*args, **kwargs)

    def read(fd):
        actual = original_read(fd)
        reads.append(actual)
        if site == 'readback' and actual['mutation']['phase'] == 'verified':
            raise error
        return actual

    monkeypatch.setattr(cm.os, 'fsync', fsync)
    monkeypatch.setattr(cm.os, 'replace', replace)
    monkeypatch.setattr(cm, 'read_marker', read)
    with pytest.raises(TimeoutError) as caught:
        cm.mark_native_verified(root_fd, tick_fd, intent_id=INTENT, marker_sha256=digest(value), now_ms=1100)
    assert caught.value is error
    actual = original_read(root_fd)
    assert actual['mutation']['id'] == INTENT
    assert actual['mutation']['phase'] == ('verified' if site in ('directory-fsync', 'readback') else 'pending')


def test_failed_initial_guard_is_not_fresh_or_reinitialized(private_marker, monkeypatch):
    root, root_fd, _, value = private_marker
    (root / 'credential-store.json').unlink()
    error = OSError('synthetic fsync')
    monkeypatch.setattr(cm.os, 'fsync', lambda fd: (_ for _ in ()).throw(error))
    with pytest.raises(OSError):
        cm.write_marker(root_fd, value, expected_sha256=None)
    assert (root / 'credential-store.json').read_bytes() == b''
    with pytest.raises(ValueError, match='SDC_CREDENTIAL_STORE_INVALID'):
        cm.read_marker(root_fd)
    with pytest.raises(FileExistsError):
        cm.write_marker(root_fd, value, expected_sha256=None)
