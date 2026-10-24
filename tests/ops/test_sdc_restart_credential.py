"""Pure synthetic child-entry protocol checks; no Keychain or subprocess execution."""
import base64
import io
import json
import os

import pytest

from adapters.sdc.keychain import KeychainError
import sdc_restart_credential as entry

ACCOUNT = '00000000-0000-4000-8000-000000000001'


@pytest.fixture(autouse=True)
def no_native_keychain(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('child tests must use their synthetic credential reader')
    monkeypatch.setattr(entry, 'KeychainCredentialStore', forbidden)


def frame(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
    return len(raw).to_bytes(4, 'big') + raw


def request(sequence, *, kind=None):
    value = {'kind': kind or ('prepare' if sequence == 0 else 'read'),
             'sequence': sequence, 'nonce': f'{sequence + 1:032x}'}
    if sequence == 0:
        value['account'] = ACCOUNT
    return value


def responses(target):
    raw, result = target.getvalue(), []
    while raw:
        assert len(raw) >= 4
        size = int.from_bytes(raw[:4], 'big')
        assert 1 <= size <= 8192 and len(raw) >= size + 4
        result.append(json.loads(raw[4:size + 4]))
        raw = raw[size + 4:]
    return result


class NativeOracle:
    def __init__(self):
        self.accounts, self.calls = [], []
        self.error = None
        self.raw = None

    def __call__(self, account):
        self.accounts.append(account)
        return self

    def read(self):
        self.calls.append(len(self.calls) + 1)
        if self.error is not None:
            raise self.error
        return self.raw if self.raw is not None else json.dumps({'key': f'{len(self.calls):064x}'}).encode()


def test_prepare_has_no_native_read_and_each_sequence_copies_again():
    oracle, target = NativeOracle(), io.BytesIO()
    source = io.BytesIO(frame(request(0)))
    assert entry._serve(source, target, factory=oracle) == 0
    assert oracle.calls == [] and oracle.accounts == [ACCOUNT]
    assert responses(target) == [{'kind': 'ready', 'sequence': 0, 'nonce': request(0)['nonce']}]

    oracle, target = NativeOracle(), io.BytesIO()
    source = io.BytesIO(b''.join(frame(request(n, kind='read_and_exit' if n == 3 else None)) for n in range(4)))
    assert entry._serve(source, target, factory=oracle) == 0
    assert oracle.calls == [1, 2, 3] and oracle.accounts == [ACCOUNT]
    output = responses(target)
    assert [v['sequence'] for v in output] == [0, 1, 2, 3]
    assert [v['nonce'] for v in output] == [request(n)['nonce'] for n in range(4)]
    assert all(set(v) == {'kind', 'sequence', 'nonce', 'raw'} for v in output[1:])
    assert [json.loads(base64.b64decode(v['raw'])) for v in output[1:]] == [
        {'key': f'{n:064x}'} for n in range(1, 4)]


@pytest.mark.parametrize('change', ['extra', 'account', 'sequence', 'bool-sequence',
                                    'reused-nonce', 'bad-nonce', 'kind', 'missing'])
def test_invalid_request_is_rejected_before_native_copy(change):
    oracle, target = NativeOracle(), io.BytesIO()
    value = request(1)
    if change == 'extra':
        value['extra'] = 'synthetic-canary'
    elif change == 'account':
        value['account'] = ACCOUNT
    elif change == 'sequence':
        value['sequence'] = 2
    elif change == 'bool-sequence':
        value['sequence'] = True
    elif change == 'reused-nonce':
        value['nonce'] = request(0)['nonce']
    elif change == 'bad-nonce':
        value['nonce'] = 'X' * 32
    elif change == 'kind':
        value['kind'] = 'update'
    elif change == 'missing':
        del value['nonce']
    assert entry._serve(io.BytesIO(frame(request(0)) + frame(value)), target, factory=oracle) == 1
    assert oracle.calls == [] and len(responses(target)) == 1


@pytest.mark.parametrize('raw', [b'', b'\x00', b'\x00\x00\x00\x00',
                                (8193).to_bytes(4, 'big'), b'\x00\x00\x00\x04{',
                                b'\x00\x00\x00\x0d{"x":1,"x":2}',
                                b'\x00\x00\x00\x05{} {}'])
def test_invalid_prepare_frame_has_no_factory_or_native_call(raw):
    oracle, target = NativeOracle(), io.BytesIO()
    assert entry._serve(io.BytesIO(raw), target, factory=oracle) == 1
    assert oracle.accounts == [] and oracle.calls == [] and target.getvalue() == b''


@pytest.mark.parametrize('account', [None, '', 'not-a-uuid', '00000000-0000-1000-8000-000000000001'])
def test_prepare_requires_one_canonical_uuid4_account(account):
    oracle, target = NativeOracle(), io.BytesIO()
    value = {**request(0), 'account': account}
    assert entry._serve(io.BytesIO(frame(value)), target, factory=oracle) == 1
    assert oracle.accounts == [] and target.getvalue() == b''


def test_terminal_requires_request_eof_before_native_copy():
    oracle, target = NativeOracle(), io.BytesIO()
    source = io.BytesIO(frame(request(0)) + frame(request(1, kind='read_and_exit')) + b'X')
    assert entry._serve(source, target, factory=oracle) == 1
    assert oracle.calls == [] and len(responses(target)) == 1


@pytest.mark.parametrize('code', ['SDC_KEYCHAIN_NOT_FOUND', 'SDC_KEYCHAIN_LOCKED',
                                 'SDC_KEYCHAIN_AUTH_FAILED', 'SDC_KEYCHAIN_UNAVAILABLE',
                                 'SDC_KEYCHAIN_INVALID_VALUE', 'SDC_KEYCHAIN_INVALID_ACCOUNT'])
def test_native_errors_are_fixed_and_end_the_attempt(code):
    oracle, target = NativeOracle(), io.BytesIO()
    oracle.error = KeychainError(code)
    source = io.BytesIO(frame(request(0)) + frame(request(1)) + frame(request(2)))
    assert entry._serve(source, target, factory=oracle) == 1
    assert oracle.calls == [1]
    assert responses(target)[1] == {'kind': 'error', 'sequence': 1,
                                    'nonce': request(1)['nonce'], 'code': code}


@pytest.mark.parametrize('raw', [b'', b'x' * 4097, b'{"key":"synthetic-secret-canary"}',
                                b'{"key":"1","key":"2"}', b'[]', 'not-bytes'])
def test_invalid_native_value_never_leaks_or_allows_another_read(raw):
    oracle, target = NativeOracle(), io.BytesIO()
    oracle.raw = raw
    source = io.BytesIO(frame(request(0)) + frame(request(1)) + frame(request(2)))
    assert entry._serve(source, target, factory=oracle) == 1
    assert oracle.calls == [1]
    assert 'synthetic-secret-canary' not in target.getvalue().decode()
    assert set(responses(target)[1]) == {'kind', 'sequence', 'nonce', 'code'}


def test_unexpected_native_exception_is_redacted():
    oracle, target = NativeOracle(), io.BytesIO()
    oracle.error = RuntimeError('synthetic-secret-canary')
    assert entry._serve(io.BytesIO(frame(request(0)) + frame(request(1))), target, factory=oracle) == 1
    assert responses(target)[1]['code'] == 'SDC_KEYCHAIN_UNAVAILABLE'
    assert 'synthetic-secret-canary' not in target.getvalue().decode()


def test_exact_raw_limit_survives_the_distinct_bounded_frame():
    oracle, target = NativeOracle(), io.BytesIO()
    raw = json.dumps({'key': '1' * 64}).encode()
    oracle.raw = raw + b' ' * (4096 - len(raw))
    source = io.BytesIO(frame(request(0)) + frame(request(1, kind='read_and_exit')))
    assert entry._serve(source, target, factory=oracle) == 0
    assert base64.b64decode(responses(target)[1]['raw']) == oracle.raw
    assert len(target.getvalue()) < 8192 and oracle.calls == [1]


def test_partial_reads_and_writes_are_complete_and_bounded():
    class ShortSource(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 8192
            return super().read(min(3, size))

    class ShortTarget(io.BytesIO):
        def write(self, raw):
            assert len(raw) <= 8196
            return super().write(raw[:2])

    oracle, target = NativeOracle(), ShortTarget()
    source = ShortSource(frame(request(0)) + frame(request(1, kind='read_and_exit')))
    assert entry._serve(source, target, factory=oracle) == 0
    assert oracle.calls == [1] and len(responses(target)) == 2


def test_valid_fd_entry_owns_only_dedicated_pipes_and_closes_them(monkeypatch):
    request_read, request_write = os.pipe()
    response_read, response_write = os.pipe()
    seen = []

    def serve(source, target):
        seen.append((source.fileno(), target.fileno()))
        return 0

    monkeypatch.setattr(entry, '_serve', serve)
    try:
        assert entry.main([str(request_read), str(response_write)]) == 0
        assert seen == [(request_read, response_write)]
        for fd in (request_read, response_write):
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        os.close(request_write)
        os.close(response_read)


@pytest.mark.parametrize('args', [[], ['0', '1'], ['2', '3'], ['4', '4'], ['-3', '4'],
                                 ['003', '4'], ['account', '4'], ['3', '4', ACCOUNT]])
def test_bad_fd_arguments_never_invoke_server(monkeypatch, args):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid inherited FDs must not reach the reader')
    monkeypatch.setattr(entry, '_serve', forbidden)
    assert entry.main(args) == 1


def test_wrong_pipe_directions_are_rejected(monkeypatch):
    request_read, request_write = os.pipe()
    response_read, response_write = os.pipe()
    monkeypatch.setattr(entry, '_serve', lambda *args: pytest.fail('wrong directions'))
    try:
        assert entry.main([str(request_write), str(response_read)]) == 1
    finally:
        for fd in (request_read, request_write, response_read, response_write):
            os.close(fd)
