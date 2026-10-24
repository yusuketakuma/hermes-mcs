"""Synthetic one-copy child protocol; never execute a process or native Keychain."""
import base64
import hashlib
import io
import json
import os

import pytest

from adapters.sdc.keychain import KeychainError
import sdc_keychain as entry

ACCOUNT = '00000000-0000-4000-8000-000000000001'
RAW = json.dumps({'key': 'a1' * 32}, separators=(',', ':')).encode()
CANARY = 'SYNTHETIC_CREDENTIAL_NEVER_PRINT'


def frame(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return len(raw).to_bytes(4, 'big') + raw


def request(**changes):
    return {'kind': 'read', 'account': ACCOUNT, 'sequence': 7, 'nonce': 'a' * 32, **changes}


def response(target):
    raw = target.getvalue()
    assert len(raw) >= 4 and int.from_bytes(raw[:4], 'big') == len(raw) - 4
    return json.loads(raw[4:])


class Oracle:
    def __init__(self, raw=RAW, error=None):
        self.raw, self.error, self.accounts, self.calls = raw, error, [], 0

    def __call__(self, account):
        self.accounts.append(account)
        return self

    def read(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.raw


@pytest.fixture(autouse=True)
def no_native(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Only the explicit synthetic factory may read a credential')
    monkeypatch.setattr(entry, 'KeychainCredentialStore', forbidden)


def test_exact_one_fresh_copy_preserves_raw_and_hash():
    for raw in (RAW, json.dumps({'key': 'b2' * 32}).encode()):
        oracle, target = Oracle(raw), io.BytesIO()
        assert entry._serve(io.BytesIO(frame(request())), target, factory=oracle) == 0
        assert oracle.calls == 1 and oracle.accounts == [ACCOUNT]
        assert response(target) == {'kind': 'value', 'sequence': 7, 'nonce': 'a' * 32,
                                    'raw': base64.b64encode(raw).decode(), 'sha256': hashlib.sha256(raw).hexdigest()}


@pytest.mark.parametrize('changes', [{'kind': 'write'}, {'kind': 'delete'}, {'extra': CANARY},
                                     {'sequence': True}, {'sequence': 0}, {'sequence': 2**32},
                                     {'nonce': 'X' * 32}, {'account': None}, {'account': 'not-a-uuid'},
                                     {'account': '00000000-0000-1000-8000-000000000001'}])
def test_unknown_mutation_or_bad_request_never_calls_native(changes):
    oracle, target = Oracle(), io.BytesIO()
    assert entry._serve(io.BytesIO(frame(request(**changes))), target, factory=oracle) == 1
    assert oracle.calls == 0 and oracle.accounts == [] and target.getvalue() == b''


@pytest.mark.parametrize('raw', [b'', b'\0', b'\0\0\0\0', (8193).to_bytes(4, 'big'),
                                b'\0\0\0\x04{', frame(request()) + b'X',
                                frame(request()) + frame(request(sequence=8)),
                                b'\0\0\0\x0d{"x":1,"x":2}'])
def test_complete_frame_and_eof_are_required_before_native(raw):
    oracle, target = Oracle(), io.BytesIO()
    assert entry._serve(io.BytesIO(raw), target, factory=oracle) == 1
    assert oracle.calls == 0 and oracle.accounts == [] and target.getvalue() == b''


@pytest.mark.parametrize('code', ['SDC_KEYCHAIN_NOT_FOUND', 'SDC_KEYCHAIN_LOCKED', 'SDC_KEYCHAIN_AUTH_FAILED',
                                 'SDC_KEYCHAIN_UNAVAILABLE', 'SDC_KEYCHAIN_INVALID_VALUE', 'SDC_KEYCHAIN_INVALID_ACCOUNT'])
def test_native_error_is_completed_fixed_metadata(code):
    oracle, target = Oracle(error=KeychainError(code)), io.BytesIO()
    assert entry._serve(io.BytesIO(frame(request())), target, factory=oracle) == 0
    assert oracle.calls == 1
    assert response(target) == {'kind': 'error', 'sequence': 7, 'nonce': 'a' * 32, 'code': code}


@pytest.mark.parametrize('raw', [b'', b'x' * 4097, b'[]', b'{"key":"bad"}',
                                b'{"key":"1","key":"2"}', 'not-bytes'])
def test_invalid_native_payload_never_leaks(raw):
    oracle, target = Oracle(raw), io.BytesIO()
    assert entry._serve(io.BytesIO(frame(request())), target, factory=oracle) == 0
    assert oracle.calls == 1
    assert set(response(target)) == {'kind', 'sequence', 'nonce', 'code'}
    assert response(target)['code'] in ('SDC_KEYCHAIN_UNAVAILABLE', 'SDC_KEYCHAIN_INVALID_VALUE')
    assert 'bad' not in target.getvalue().decode()


def test_arbitrary_native_error_is_redacted():
    oracle, target = Oracle(error=RuntimeError(CANARY)), io.BytesIO()
    assert entry._serve(io.BytesIO(frame(request())), target, factory=oracle) == 0
    assert response(target)['code'] == 'SDC_KEYCHAIN_UNAVAILABLE'
    assert CANARY not in target.getvalue().decode()


def test_raw_limit_and_partial_io_preserve_exact_bytes():
    class ShortSource(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 8192
            return super().read(min(size, 3))

    class ShortTarget(io.BytesIO):
        def write(self, value):
            assert len(value) <= 8196
            return super().write(value[:2])

    raw = RAW + b' ' * (4096 - len(RAW))
    oracle, target = Oracle(raw), ShortTarget()
    assert entry._serve(ShortSource(frame(request())), target, factory=oracle) == 0
    assert base64.b64decode(response(target)['raw']) == raw and oracle.calls == 1


def test_valid_entry_owns_and_closes_only_dedicated_pipe_fds(monkeypatch):
    source_read, source_write = os.pipe()
    target_read, target_write = os.pipe()
    seen = []

    def serve(source, target):
        seen.append((source.fileno(), target.fileno()))
        return 0

    monkeypatch.setattr(entry, '_serve', serve)
    try:
        assert entry.main([str(source_read), str(target_write)]) == 0
        assert seen == [(source_read, target_write)]
        for fd in (source_read, target_write):
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        os.close(source_write)
        os.close(target_read)


@pytest.mark.parametrize('args', [[], ['0', '1'], ['2', '3'], ['4', '4'], ['-3', '4'],
                                 ['003', '4'], ['account', '4'], ['3', '4', ACCOUNT]])
def test_bad_fd_arguments_never_reach_server(monkeypatch, args):
    monkeypatch.setattr(entry, '_serve', lambda *args: pytest.fail('invalid FDs must not run the server'))
    assert entry.main(args) == 1


def test_regular_file_and_reversed_pipe_fds_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, '_serve', lambda *args: pytest.fail('only dedicated pipe directions are allowed'))
    read, write = os.pipe()
    try:
        assert entry.main([str(write), str(read)]) == 1
        with (tmp_path / 'synthetic').open('w+b') as regular:
            assert entry.main([str(regular.fileno()), str(write)]) == 1
    finally:
        os.close(read)
        os.close(write)
