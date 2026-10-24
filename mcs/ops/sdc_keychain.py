"""固定の匿名pipeから受けた通常Keychain操作と完了記録を一回だけ実行します。"""
import base64
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import time

REPO = Path(__file__).resolve().parents[2]
for _path in (str(REPO), str(REPO / 'mcs')):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import _mcs_path  # noqa: E402,F401
from adapters.sdc.keychain import KeychainCredentialStore, KeychainError  # noqa: E402
from adapters.sdc.keychain_owner import (KEYCHAIN_CODES, MAX_FRAME, credential_raw,  # noqa: E402
                                          decode_payload, encode_frame, read_request, mutation_request)
from adapters.sdc import credential_mutation as mutation  # noqa: E402


def _read_exact(source, size):
    output = bytearray()
    while len(output) < size:
        part = source.read(size - len(output))
        if not part:
            raise ValueError('SDC_KEYCHAIN_READ_PROTOCOL')
        output.extend(part)
    return bytes(output)


def _write(target, value):
    packet = encode_frame(value)
    offset = 0
    while offset < len(packet):
        count = target.write(packet[offset:])
        if type(count) is not int or count <= 0:
            raise ValueError('SDC_KEYCHAIN_READ_PROTOCOL')
        offset += count
    target.flush()


def _mutate(client, request, root_fd, tick_fd, *, monotonic, clock):
    def check():
        now = monotonic()
        if type(now) not in (int, float) or not 0 <= now < request['deadline']:
            raise ValueError('SDC_KEYCHAIN_READ_DEADLINE')

    def pending():
        return mutation.pending_marker(root_fd, tick_fd, intent_id=request['intent_id'],
                                       marker_sha256=request['marker_sha256'], account=request['account'])

    def actual():
        try:
            return mutation.credential_digest(mutation.credential_value(client.read(check=check)))
        except KeychainError as error:
            if error.code != 'SDC_KEYCHAIN_NOT_FOUND':
                raise
            return None

    check()
    marker = pending()
    intent = marker['mutation']
    if intent['op'] != request['kind'] or actual() != intent['old_sha256']:
        raise ValueError('SDC_CREDENTIAL_STORE_INVALID')
    check()
    pending()
    if request['kind'] == 'write':
        raw = base64.b64decode(request['raw'], validate=True)
        if mutation.credential_digest(mutation.credential_value(raw)) != intent['new_sha256']:
            raise ValueError('SDC_CREDENTIAL_STORE_INVALID')
        client.write(raw, check=check)
    else:
        try:
            client.delete(check=check)
        except KeychainError as error:
            if error.code != 'SDC_KEYCHAIN_NOT_FOUND':
                raise
    if actual() != intent['new_sha256']:
        raise ValueError('SDC_CREDENTIAL_STORE_INVALID')
    # Each synchronous native method has returned through UI restoration and
    # CF cleanup. This checkpoint is the last operation: no native call follows.
    # A stepped-back clock must not invalidate an already completed native change.
    verified = mutation.mark_native_verified(root_fd, tick_fd, intent_id=request['intent_id'],
                                             marker_sha256=request['marker_sha256'],
                                             now_ms=max(int(clock() * 1000), intent['started_at']))
    return {'intent_id': request['intent_id'], 'marker_sha256': mutation.marker_sha256(verified)}


def _serve(source, target, *, factory=None, root_fd=None, tick_fd=None,
           monotonic=time.monotonic, clock=time.time):
    """Require one complete request and EOF before touching the fixed native reader."""
    try:
        size = int.from_bytes(_read_exact(source, 4), 'big')
        if not 1 <= size <= MAX_FRAME:
            return 1
        value = decode_payload(_read_exact(source, size))
        request = read_request(value) if value.get('kind') == 'read' else mutation_request(value)
        is_mutation = request['kind'] != 'read'
        if is_mutation != (root_fd is not None and tick_fd is not None):
            return 1
        if source.read(1) != b'':
            return 1
        response = {key: request[key] for key in ('sequence', 'nonce')}
        try:
            client = (factory or KeychainCredentialStore)(request['account'])
            if is_mutation:
                result = _mutate(client, request, root_fd, tick_fd, monotonic=monotonic, clock=clock)
                _write(target, {**response, 'kind': 'verified', **result})
                return 0
            raw = credential_raw(client.read())
        except Exception as error:
            code = error.code if isinstance(error, KeychainError) and error.code in KEYCHAIN_CODES else 'SDC_KEYCHAIN_UNAVAILABLE'
            _write(target, {**response, 'kind': 'error', 'code': code})
            return 0  # A completed error is still required to exit/reap before adoption.
        _write(target, {**response, 'kind': 'value', 'raw': base64.b64encode(raw).decode('ascii'),
                        'sha256': hashlib.sha256(raw).hexdigest()})
        return 0
    except Exception:
        return 1


def main(argv=None):
    """Own only inherited dedicated pipe FDs; normal streams never carry credentials."""
    import fcntl
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) not in (2, 4) or any(re.fullmatch('[1-9][0-9]{0,9}', value) is None for value in args):
            return 1
        descriptors = tuple(map(int, args))
        source_fd, target_fd = descriptors[:2]
        if min(descriptors) < 3 or len(set(descriptors)) != len(descriptors):
            return 1
        for fd, mode in ((source_fd, os.O_RDONLY), (target_fd, os.O_WRONLY)):
            if (not stat.S_ISFIFO(os.fstat(fd).st_mode)
                    or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != mode):
                return 1
        if len(descriptors) == 4:
            mutation._root(descriptors[2])
            mutation.tick_lock_fingerprint(descriptors[3])
        for fd in descriptors:
            os.set_inheritable(fd, False)
        try:
            with os.fdopen(source_fd, 'rb', buffering=0) as source, \
                    os.fdopen(target_fd, 'wb', buffering=0) as target:
                if len(descriptors) == 2:
                    return _serve(source, target)
                return _serve(source, target, root_fd=descriptors[2], tick_fd=descriptors[3])
        finally:
            for fd in descriptors[2:]:
                os.close(fd)
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
