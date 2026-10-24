"""固定の私有pipeで同じ再起動試行のKeychain実読取りだけを行います。"""
import base64
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

REPO = Path(__file__).resolve().parents[2]
for _path in (str(REPO), str(REPO / 'mcs')):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import _mcs_path  # noqa: E402,F401
from adapters.sdc.keychain import KeychainCredentialStore, KeychainError  # noqa: E402
from adapters.sdc.protocol import strict_json  # noqa: E402
from adapters.sdc.storage import Store  # noqa: E402

MAX_RAW = 4096
MAX_FRAME = 8192
MAX_SEQUENCE = 2**32 - 1
KEYCHAIN_CODES = frozenset({'SDC_KEYCHAIN_NOT_FOUND', 'SDC_KEYCHAIN_LOCKED',
                          'SDC_KEYCHAIN_AUTH_FAILED', 'SDC_KEYCHAIN_UNAVAILABLE',
                          'SDC_KEYCHAIN_INVALID_VALUE', 'SDC_KEYCHAIN_INVALID_ACCOUNT'})


def account_valid(account):
    try:
        value = uuid.UUID(account) if type(account) is str else None
        return value is not None and value.version == 4 and str(value) == account
    except ValueError:
        return False


def encode_frame(value):
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                     allow_nan=False).encode('ascii')
    if not 1 <= len(raw) <= MAX_FRAME:
        raise ValueError('SDC_RESTART_CREDENTIAL_PROTOCOL')
    return len(raw).to_bytes(4, 'big') + raw


def decode_payload(raw):
    value = strict_json(raw)
    if type(value) is not dict or encode_frame(value)[4:] != raw:
        raise ValueError('SDC_RESTART_CREDENTIAL_PROTOCOL')
    return value


def credential_raw(raw):
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_RAW:
        raise ValueError('SDC_RESTART_CREDENTIAL_PROTOCOL')
    Store._credential_value(raw)
    return raw


def _read_exact(source, size):
    out = bytearray()
    while len(out) < size:
        part = source.read(size - len(out))
        if not part:
            raise ValueError('SDC_RESTART_CREDENTIAL_PROTOCOL')
        out.extend(part)
    return bytes(out)


def _read_frame(source):
    first = source.read(1)
    if first == b'':
        return None
    header = first + _read_exact(source, 3)
    size = int.from_bytes(header, 'big')
    if not 1 <= size <= MAX_FRAME:
        raise ValueError('SDC_RESTART_CREDENTIAL_PROTOCOL')
    return decode_payload(_read_exact(source, size))


def _write_frame(target, value):
    packet = encode_frame(value)
    offset = 0
    while offset < len(packet):
        count = target.write(packet[offset:])
        if type(count) is not int or count <= 0:
            raise ValueError('SDC_RESTART_CREDENTIAL_PROTOCOL')
        offset += count
    target.flush()


def _request(value, sequence, previous_nonce):
    fields = {'kind', 'sequence', 'nonce'} | ({'account'} if sequence == 0 else set())
    if (type(value) is not dict or set(value) != fields
            or type(value['sequence']) is not int or value['sequence'] != sequence
            or not 0 <= sequence <= MAX_SEQUENCE
            or type(value['nonce']) is not str or re.fullmatch('[0-9a-f]{32}', value['nonce']) is None
            or value['nonce'] == previous_nonce
            or value['kind'] not in (('prepare',) if sequence == 0 else ('read', 'read_and_exit'))
            or sequence == 0 and not account_valid(value['account'])):
        raise ValueError('SDC_RESTART_CREDENTIAL_PROTOCOL')
    return value


def _serve(source, target, *, factory=None):
    """READY never reads a credential; every subsequent accepted request copies afresh."""
    try:
        request = _request(_read_frame(source), 0, None)
        reader = (factory or KeychainCredentialStore)(request['account'])
        nonce = request['nonce']
        _write_frame(target, {'kind': 'ready', 'sequence': 0, 'nonce': nonce})
        sequence = 1
        while True:
            value = _read_frame(source)
            if value is None:
                return 0
            request = _request(value, sequence, nonce)
            nonce = request['nonce']
            terminal = request['kind'] == 'read_and_exit'
            if terminal and source.read(1) != b'':
                return 1
            response = {'sequence': sequence, 'nonce': nonce}
            try:
                raw = credential_raw(reader.read())
            except Exception as error:
                code = error.code if isinstance(error, KeychainError) else 'SDC_KEYCHAIN_UNAVAILABLE'
                _write_frame(target, {**response, 'kind': 'error', 'code': code})
                return 1
            _write_frame(target, {**response, 'kind': 'value',
                                  'raw': base64.b64encode(raw).decode('ascii')})
            if terminal:
                return 0
            sequence += 1
    except Exception:
        return 1


def main(argv=None):
    """Use only inherited dedicated pipe FDs; standard streams carry no credentials."""
    import fcntl
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) != 2 or any(re.fullmatch('[1-9][0-9]{0,9}', arg) is None for arg in args):
            return 1
        source_fd, target_fd = map(int, args)
        if source_fd < 3 or target_fd < 3 or source_fd == target_fd:
            return 1
        for fd, mode in ((source_fd, os.O_RDONLY), (target_fd, os.O_WRONLY)):
            if (not stat.S_ISFIFO(os.fstat(fd).st_mode)
                    or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != mode):
                return 1
        with os.fdopen(source_fd, 'rb', buffering=0) as source, \
                os.fdopen(target_fd, 'wb', buffering=0) as target:
            return _serve(source, target)
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
