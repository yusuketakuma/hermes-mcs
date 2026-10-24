"""Pure synthetic child mutations: native completion precedes its fixed checkpoint."""
from copy import deepcopy
import base64
import io
import json
import os

import pytest

from adapters.sdc import credential_mutation as cm
from adapters.sdc.keychain import KeychainCredentialStore
import sdc_keychain as entry
from test_sdc_credential_mutation import ACCOUNT, INTENT, canonical, digest
from test_sdc_credential_mutation import private_marker as private_marker


def request(value, **changes):
    return {'kind': value['mutation']['op'], 'account': ACCOUNT, 'sequence': 8, 'nonce': 'a' * 32,
            'deadline': 105.0, 'intent_id': INTENT, 'marker_sha256': digest(value),
            'raw': base64.b64encode(canonical({'key': '02' * 32})).decode()
                   if value['mutation']['op'] == 'write' else None, **changes}


def frame(value):
    raw = canonical(value)
    return len(raw).to_bytes(4, 'big') + raw


class Native:
    """Real credential adapter over fake native return values, with no C loading."""
    def __init__(self, events, raw=None):
        self.events, self.raw = events, raw
        self.fail = None
        self.on_stage = lambda name: None
        self.corrupt_readback = False

    def _stage(self, name, account):
        assert account == ACCOUNT
        self.events.append(name)
        self.on_stage(name)
        if self.fail == name:
            raise RuntimeError('synthetic native or UI restoration failure')

    def copy(self, account):
        self._stage('copy', account)
        if self.raw is None:
            return -25300, None
        return 0, self.raw if not self.corrupt_readback else canonical({'key': 'ff' * 32})

    def add(self, account, raw):
        self._stage('add', account)
        if self.raw is not None:
            return -25299
        self.raw = raw
        return 0

    def update(self, account, raw):
        self._stage('update', account)
        self.raw = raw
        return 0

    def delete(self, account):
        self._stage('delete', account)
        status = -25300 if self.raw is None else 0
        self.raw = None
        return status


def serve(private_marker, native, value=None, *, changes=None, monotonic=lambda: 100.0, target=None, clock=lambda: 1.1):
    _, root_fd, tick_fd, original = private_marker
    value = original if value is None else value
    target = io.BytesIO() if target is None else target
    result = entry._serve(io.BytesIO(frame(request(value, **(changes or {})))), target,
                          factory=lambda account: KeychainCredentialStore(account, native=native),
                          root_fd=root_fd, tick_fd=tick_fd, monotonic=monotonic, clock=clock)
    return result, target


def save(private_marker, value):
    root = private_marker[0]
    (root / cm.NAME).write_bytes(canonical(value))


@pytest.fixture(autouse=True)
def no_real_native(monkeypatch):
    monkeypatch.setattr(entry, 'KeychainCredentialStore', lambda *a, **k: pytest.fail('explicit fake native only'))


@pytest.mark.parametrize('duplicate', [False, True])
def test_add_or_update_then_actual_readback_precedes_checkpoint(private_marker, monkeypatch, duplicate):
    events = []
    native = Native(events, canonical({'key': '01' * 32}) if duplicate else None)
    value = deepcopy(private_marker[3])
    if duplicate:
        value['mutation'].update(purpose='claim-key-id', old_sha256=digest({'key': '01' * 32}))
        value['mutation']['context']['after'].update(device_id=ACCOUNT, key_id=INTENT)
        value['mutation']['context']['before'] = {**value['mutation']['context']['after'], 'device_id': None, 'key_id': None}
        save(private_marker, value)
    original = cm.mark_native_verified

    def checkpoint(*args, **kwargs):
        events.append('verified')
        result = original(*args, **kwargs)
        native.on_stage = lambda name: pytest.fail('No native call is allowed after verified')
        return result

    monkeypatch.setattr(cm, 'mark_native_verified', checkpoint)
    result, target = serve(private_marker, native, value)
    assert result == 0
    assert events == (['copy', 'add', 'update', 'copy', 'verified'] if duplicate else ['copy', 'add', 'copy', 'verified'])
    actual = cm.read_marker(private_marker[1])
    response = json.loads(target.getvalue()[4:])
    assert response == {'kind': 'verified', 'sequence': 8, 'nonce': 'a' * 32,
                        'intent_id': INTENT, 'marker_sha256': digest(actual)}
    assert actual == {**value, 'mutation': {**value['mutation'], 'phase': 'verified', 'verified_at': 1100}}
    # The 4-byte length prefix is binary; only the JSON body must carry no secret.
    assert 'key' not in target.getvalue()[4:].decode() and native.raw == canonical({'key': '02' * 32})


def test_stepped_back_clock_still_checkpoints_a_completed_native_change(private_marker):
    native = Native([], None)
    started = private_marker[3]['mutation']['started_at']
    result, _ = serve(private_marker, native, clock=lambda: (started - 500) / 1000)
    assert result == 0
    assert cm.read_marker(private_marker[1])['mutation'] == {**private_marker[3]['mutation'], 'phase': 'verified',
                                                             'verified_at': started}


@pytest.mark.parametrize('present', [False, True])
def test_delete_requires_actual_not_found_after_return(private_marker, present):
    value = deepcopy(private_marker[3])
    value['mutation'].update(op='delete', purpose='delete', new_sha256=None,
                             old_sha256=digest({'key': '01' * 32}) if present else None)
    value['mutation']['context']['after'] = dict(value['mutation']['context']['before'])
    save(private_marker, value)
    events, native = [], None
    native = Native(events, canonical({'key': '01' * 32}) if present else None)
    result, target = serve(private_marker, native, value)
    assert result == 0 and events == ['copy', 'delete', 'copy']
    assert json.loads(target.getvalue()[4:])['kind'] == 'verified'
    assert cm.read_marker(private_marker[1])['mutation']['phase'] == 'verified'


@pytest.mark.parametrize('fault', ['old-value', 'wrong-newhash', 'add', 'readback', 'restore', 'expired', 'between-add-update'])
def test_missing_native_completion_never_creates_verified(private_marker, fault):
    events = []
    native = Native(events, canonical({'key': '01' * 32}) if fault in ('old-value', 'between-add-update') else None)
    value = deepcopy(private_marker[3])
    now = [100.0]
    changes = {}
    if fault == 'wrong-newhash':
        changes['raw'] = base64.b64encode(canonical({'key': 'ff' * 32})).decode()
    elif fault == 'expired':
        now[0] = 105.0
    elif fault == 'add':
        native.fail = 'add'
    elif fault == 'readback':
        native.on_stage = lambda name: setattr(native, 'corrupt_readback', name == 'add' or native.corrupt_readback)
    elif fault == 'restore':
        def restored(name):
            if name == 'add':
                native.raw = canonical({'key': '02' * 32})
                raise RuntimeError('synthetic UI restore failed after native write')
        native.on_stage = restored
    elif fault == 'between-add-update':
        value['mutation'].update(purpose='claim-key-id', old_sha256=digest({'key': '01' * 32}))
        value['mutation']['context']['after'].update(device_id=ACCOUNT, key_id=INTENT)
        value['mutation']['context']['before'] = {**value['mutation']['context']['after'], 'device_id': None, 'key_id': None}
        native.on_stage = lambda name: now.__setitem__(0, 106.0) if name == 'add' else None
        save(private_marker, value)
    result, target = serve(private_marker, native, value, changes=changes, monotonic=lambda: now[0])
    assert result == 0 and json.loads(target.getvalue()[4:])['kind'] == 'error'
    assert cm.read_marker(private_marker[1]) == value
    if fault in ('old-value', 'wrong-newhash', 'expired'):
        assert 'add' not in events and 'update' not in events
    if fault == 'between-add-update':
        assert events == ['copy', 'add']


def test_checkpoint_survives_reply_loss_without_native_retry(private_marker):
    class BrokenOutput(io.BytesIO):
        def write(self, raw):
            raise OSError('synthetic reply loss')
    events = []
    native = Native(events)
    result, _ = serve(private_marker, native, target=BrokenOutput())
    assert result == 1 and events == ['copy', 'add', 'copy']
    actual = cm.read_marker(private_marker[1])
    assert actual['mutation']['phase'] == 'verified'
    # Even an identical old request cannot repeat its side effect after proof.
    serve(private_marker, native)
    assert events == ['copy', 'add', 'copy']


@pytest.mark.parametrize('change', [{'deadline': True}, {'deadline': float('inf')}, {'raw': None},
                                   {'intent_id': ACCOUNT}, {'marker_sha256': 'f' * 64},
                                   {'extra': 'synthetic'}, {'account': INTENT}])
def test_bad_mutation_frame_or_cas_has_no_native_calls(private_marker, change):
    events = []
    native = Native(events)
    if change.get('deadline') == float('inf'):
        with pytest.raises(ValueError):
            serve(private_marker, native, changes=change)
    else:
        serve(private_marker, native, changes=change)
    assert events == [] and cm.read_marker(private_marker[1]) == private_marker[3]


def test_mutation_entry_owns_root_and_lease_only_until_server_returns(private_marker, monkeypatch):
    _, root_fd, tick_fd, _ = private_marker
    source_read, source_write = os.pipe()
    target_read, target_write = os.pipe()
    root_copy, tick_copy = os.dup(root_fd), os.dup(tick_fd)
    seen = []

    def server(source, target, *, root_fd, tick_fd):
        seen.append((root_fd, tick_fd))
        assert all(not os.get_inheritable(fd) for fd in (source.fileno(), target.fileno(), root_fd, tick_fd))
        return 0

    monkeypatch.setattr(entry, '_serve', server)
    try:
        assert entry.main(list(map(str, (source_read, target_write, root_copy, tick_copy)))) == 0
        assert seen == [(root_copy, tick_copy)]
        for fd in (source_read, target_write, root_copy, tick_copy):
            with pytest.raises(OSError):
                os.fstat(fd)
        assert os.fstat(root_fd) and os.fstat(tick_fd)
    finally:
        os.close(source_write)
        os.close(target_read)


def test_adapter_deadline_between_add_and_update_preserves_timeout_instance():
    events = []
    native = Native(events, canonical({'key': '01' * 32}))
    client = KeychainCredentialStore(ACCOUNT, native=native)
    error = TimeoutError('synthetic parent deadline')

    def check():
        if events:
            raise error

    with pytest.raises(TimeoutError) as caught:
        client.write(canonical({'key': '02' * 32}), check=check)
    assert caught.value is error and events == ['add']
