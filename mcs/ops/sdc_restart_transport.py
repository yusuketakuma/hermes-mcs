"""署名済みの再起動要求を短命子の主スレッドで一度だけ送信します。"""
import base64
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
for _path in (str(REPO), str(REPO / 'mcs')):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import _mcs_path  # noqa: E402,F401
from adapters.sdc.protocol import endpoint, strict_json, _restart_request, _support_headers  # noqa: E402
from adapters.sdc.transport import RestartTransport  # noqa: E402

MAX_INPUT = 32768
# A 16 KiB HTTP reply can grow sixfold when normalized to ASCII JSON.
MAX_OUTPUT = 6 * 16384 + 2


def encode_request(approved_endpoint, path, raw, headers):
    """Snapshot only the fixed endpoint, route, exact bytes and signed headers."""
    try:
        if (type(approved_endpoint) is not dict
                or not all(type(value) is str for value in approved_endpoint.values())):
            raise ValueError
        fixed = dict(endpoint(approved_endpoint))
        _restart_request(path, raw)
        if not _support_headers(headers):
            raise ValueError
        packet = json.dumps({'endpoint': fixed, 'path': path,
                             'raw': base64.b64encode(raw).decode('ascii'),
                             'headers': dict(headers)}, ensure_ascii=True,
                            separators=(',', ':'), allow_nan=False).encode('ascii')
        if len(packet) > MAX_INPUT:
            raise ValueError
        return packet
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ValueError('SDC_RESTART_CARRIER_REQUEST_INVALID') from None


def _read_request(stream):
    packet = stream.read(MAX_INPUT + 1)
    if len(packet) > MAX_INPUT:
        raise ValueError
    value = strict_json(packet)
    if type(value) is not dict or set(value) != {'endpoint', 'path', 'raw', 'headers'}:
        raise ValueError
    raw = base64.b64decode(value['raw'], validate=True)
    if base64.b64encode(raw).decode('ascii') != value['raw']:
        raise ValueError
    encode_request(value['endpoint'], value['path'], raw, value['headers'])
    return value['endpoint'], value['path'], raw, value['headers']


def _serve(source, target):
    try:
        config, path, raw, headers = _read_request(source)
    except Exception:
        reply = b'N\n'  # Validation failed before transport construction or dispatch.
    else:
        try:
            response = RestartTransport(config).send(path, raw, headers)
            if type(response) is not dict:
                raise ValueError
            reply = b'R\n' + json.dumps(response, ensure_ascii=True, separators=(',', ':'),
                                       allow_nan=False).encode('ascii')
            if len(reply) > MAX_OUTPUT:
                raise ValueError
        except Exception:
            reply = b'U\n'
    target.write(reply)
    target.flush()


def main():
    """Use bounded anonymous pipes; never print transport exceptions or bodies."""
    try:
        if len(sys.argv) != 1:
            return 1
        _serve(sys.stdin.buffer, sys.stdout.buffer)
        return 0
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
