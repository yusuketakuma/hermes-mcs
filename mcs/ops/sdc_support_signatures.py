"""固定公開鍵で遠隔支援の署名だけを検証する（操作の認可・実行・有効化は行わない）。"""
from __future__ import annotations

import base64
import binascii
from contextlib import ExitStack, contextmanager, suppress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

import owned_process

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters.sdc.protocol import strict_json  # noqa: E402
from adapters.sdc.support_contract import canonical, parse_canonical  # noqa: E402

OPENSSL = '/usr/bin/openssl'
VERIFY_TIMEOUT = 5
MAX_SAFE_INTEGER = 2**53 - 1
PURPOSES = frozenset({'operation', 'execution-grant', 'consent-preview'})
RESTART_PURPOSES = frozenset({'restart-operation', 'restart-execution-grant', 'restart-consent-preview'})
ENVIRONMENT = {'LC_ALL': 'C', 'OPENSSL_CONF': '/dev/null'}


class SignatureError(ValueError):
    """Expose only fixed verification failure codes."""
    def __init__(self, code='SDC_SUPPORT_SIGNATURE_INVALID'):
        self.code = code if code in ('SDC_SUPPORT_SIGNATURE_INVALID', 'SDC_SUPPORT_CRYPTO_UNAVAILABLE') else 'SDC_SUPPORT_SIGNATURE_INVALID'
        super().__init__(self.code)


def _require(condition):
    if not condition:
        raise SignatureError()


def _b64(value, maximum):
    _require(type(value) is str and 0 < len(value) <= maximum and re.fullmatch(r'[A-Za-z0-9_-]+', value))
    raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    _require(base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii') == value)
    return raw


def _tlv(raw, offset, tag):
    """Read one minimal definite-length DER field, with no generic ASN.1 features."""
    _require(offset + 2 <= len(raw) and raw[offset] == tag)
    size, offset = raw[offset + 1], offset + 2
    if size & 128:
        count = size & 127
        _require(1 <= count <= 2 and offset + count <= len(raw) and raw[offset] != 0)
        size = int.from_bytes(raw[offset:offset + count], 'big')
        _require(size >= 128 and (count == 1 or size >= 256))
        offset += count
    end = offset + size
    _require(end <= len(raw))
    return raw[offset:end], end


def _spki(raw):
    _require(0 < len(raw) <= 1024)
    outer, end = _tlv(raw, 0, 0x30)
    _require(end == len(raw))
    algorithm, position = _tlv(outer, 0, 0x30)
    # rsaEncryption OID with explicit NULL; neither EC nor restricted-PSS keys.
    _require(algorithm == bytes.fromhex('06092a864886f70d0101010500'))
    bits, end = _tlv(outer, position, 0x03)
    _require(end == len(outer) and bits[:1] == b'\0')
    public, end = _tlv(bits, 1, 0x30)
    _require(end == len(bits))
    modulus, position = _tlv(public, 0, 0x02)
    exponent, end = _tlv(public, position, 0x02)
    _require(end == len(public) and len(modulus) == 385 and modulus[0] == 0
             and modulus[1] & 128 and modulus[-1] & 1 and exponent == b'\x01\x00\x01')
    return raw


def _pin(pins, kid, purpose, now_ms, *, purposes=PURPOSES, record_window=None, allow_expired=False):
    _require(type(pins) in (list, tuple) and 1 <= len(pins) <= 16)
    selected, seen = None, set()
    for pin in pins:
        _require(type(pin) is dict and set(pin) == {'kid', 'purposes', 'spki', 'not_before', 'expires_at'})
        name = pin['kid']
        _require(type(name) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) and name not in seen)
        seen.add(name)
        uses = pin['purposes']
        _require(type(uses) in (list, tuple) and 1 <= len(uses) <= len(purposes) and all(type(use) is str and use in purposes for use in uses) and len(set(uses)) == len(uses))
        start, end = pin['not_before'], pin['expires_at']
        _require(type(start) is int and type(end) is int and 0 <= start < end <= MAX_SAFE_INTEGER)
        if name == kid:
            _require(purpose in uses)
            if record_window is not None:
                _require(start <= record_window['issued_at'] and record_window['expires_at'] <= end)
            _require(allow_expired or start <= now_ms < end)
            selected = _spki(_b64(pin['spki'], 2048))
    _require(selected is not None)
    return selected


def _pin_set(pins, purposes):
    _require(type(pins) is list and 1 <= len(pins) <= 16)
    # Validate every DER key, including currently inactive pins.
    for pin in pins:
        _require(type(pin) is dict and type(pin.get('purposes')) is list and bool(pin['purposes']))
        _pin(pins, pin.get('kid'), pin['purposes'][0], pin.get('not_before'), purposes=purposes)


def _validate_pins(pins, *, purpose, now_ms, purposes):
    try:
        _require(type(purpose) is str and purpose in purposes)
        _require(type(now_ms) is int and 0 <= now_ms <= MAX_SAFE_INTEGER)
        _pin_set(pins, purposes)
        _require(any(purpose in pin['purposes'] and pin['not_before'] <= now_ms < pin['expires_at'] for pin in pins))
        return strict_json(json.dumps(pins, ensure_ascii=True).encode('ascii'))
    except SignatureError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, UnicodeError, binascii.Error):
        raise SignatureError() from None


def validate_pins(pins, *, purpose, now_ms):
    """Validate provisioned R4a public pins before HTTP; never discover or adopt keys."""
    return _validate_pins(pins, purpose=purpose, now_ms=now_ms, purposes=PURPOSES)


def validate_restart_pins(pins, *, purpose, now_ms):
    """Validate current restart-only public pins; no historical exception before I/O."""
    try:
        pinned = parse_canonical(canonical(pins, max_bytes=16384), max_bytes=16384)
        return _validate_pins(pinned, purpose=purpose, now_ms=now_ms, purposes=RESTART_PURPOSES)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, UnicodeError, ImportError):
        raise SignatureError() from None


def _private_file(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)


@contextmanager
def _defer_crypto_interrupts():
    if threading.current_thread() is not threading.main_thread():
        yield {}
        return
    previous, pending, errors, delivered = {}, {}, {}, set()

    def remember(signum, frame):
        pending.setdefault(signum, frame)

    def deliver():
        # Preserve the parent deadline closure even when SIGINT also arrived.
        for signum in (signal.SIGALRM, signal.SIGINT):
            if signum in pending and signum not in delivered:
                delivered.add(signum)
                try:
                    previous[signum](signum, pending[signum])
                except BaseException as exc:
                    errors[signum] = exc

    def restore_handler(signum, handler):
        # shortcut: raising handlers can interrupt restoration; a process-wide signal policy is needed for stronger guarantees.
        for _ in range(3):
            try:
                signal.signal(signum, handler)
                return
            except BaseException as exc:
                errors.setdefault(0, exc)
        raise SignatureError('SDC_SUPPORT_CRYPTO_UNAVAILABLE')

    try:
        with ExitStack() as restore:
            for signum in (signal.SIGINT, signal.SIGALRM):
                handler = signal.getsignal(signum)
                if handler is None:
                    raise SignatureError('SDC_SUPPORT_CRYPTO_UNAVAILABLE')
                if callable(handler):
                    previous[signum] = handler
                    restore.callback(restore_handler, signum, handler)
                    signal.signal(signum, remember)
            try:
                yield pending
            finally:
                # Re-deliver only after child, pipes and temporary files are gone.
                deliver()
    finally:
        deliver()
        for signum in (signal.SIGALRM, 0, signal.SIGINT):
            if signum in errors:
                raise errors[signum]
        if pending:
            raise SignatureError('SDC_SUPPORT_CRYPTO_UNAVAILABLE')


def _verify_pss(key, signature, message):
    try:
        with _defer_crypto_interrupts() as pending, tempfile.TemporaryDirectory(prefix='mcs-sdc-verify-') as directory:
            key_path, sig_path = Path(directory) / 'public.pem', Path(directory) / 'signature.bin'
            encoded = base64.b64encode(key)
            pem = b'-----BEGIN PUBLIC KEY-----\n' + b'\n'.join(encoded[i:i + 64] for i in range(0, len(encoded), 64)) + b'\n-----END PUBLIC KEY-----\n'
            _private_file(key_path, pem)
            _private_file(sig_path, signature)
            argv = [OPENSSL, 'dgst', '-sha256', '-verify', str(key_path), '-signature', str(sig_path),
                    '-sigopt', 'rsa_padding_mode:pss', '-sigopt', 'rsa_pss_saltlen:32', '-sigopt', 'rsa_mgf1_md:sha256']
            if pending:
                raise SignatureError('SDC_SUPPORT_CRYPTO_UNAVAILABLE')
            child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, env=dict(ENVIRONMENT), start_new_session=True)
            completed = False
            try:
                end = time.monotonic() + VERIFY_TIMEOUT
                first = True
                while True:
                    remaining = end - time.monotonic()
                    if pending or remaining <= 0:
                        raise SignatureError('SDC_SUPPORT_CRYPTO_UNAVAILABLE')
                    try:
                        initial_input = {'input': message} if first else {}
                        first = False
                        child.communicate(timeout=min(0.05, remaining), **initial_input)
                    except subprocess.TimeoutExpired:
                        continue
                    if pending or time.monotonic() >= end:
                        raise SignatureError('SDC_SUPPORT_CRYPTO_UNAVAILABLE')
                    completed = True
                    break
            finally:
                try:
                    if not completed or child.returncode is None:
                        with suppress(BaseException):
                            owned_process.stop_owned(child)
                finally:
                    # A bounded stop attempt is not proof that our child/pipe is gone.
                    attempts = 0
                    while child.returncode is None or not child.stdin.closed:
                        with suppress(BaseException):
                            child.stdin.close()
                        with suppress(BaseException):
                            child.wait(timeout=0.1)
                        if child.returncode is None or not child.stdin.closed:
                            attempts += 1
                            if child.returncode is None and attempts % 20 == 0:
                                with suppress(BaseException):
                                    owned_process.stop_owned(child)
                            with suppress(BaseException):
                                time.sleep(0.05)
            _require(child.returncode == 0)
    except KeyboardInterrupt:
        raise KeyboardInterrupt() from None
    except SignatureError:
        raise
    except Exception:
        raise SignatureError('SDC_SUPPORT_CRYPTO_UNAVAILABLE') from None


def _verify_envelope(raw, *, purpose, pins, now_ms, purposes, payload_validator=None,
                     cover_lifetime=False, allow_expired=False):
    try:
        _require(type(raw) is bytes and 0 < len(raw) <= 16384 and raw.isascii())
        _require(type(purpose) is str and purpose in purposes)
        _require(type(now_ms) is int and 0 <= now_ms <= MAX_SAFE_INTEGER)
        envelope = strict_json(raw)
        _require(type(envelope) is dict and set(envelope) == {'alg', 'kid', 'purpose', 'payload', 'signature'})
        _require(envelope['alg'] == 'PS256' and envelope['purpose'] == purpose)
        kid = envelope['kid']
        _require(type(kid) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', kid))
        payload_raw = _b64(envelope['payload'], 10923)
        payload = parse_canonical(payload_raw, max_bytes=8192)
        _require(type(payload) is dict)
        signature = _b64(envelope['signature'], 512)
        _require(len(signature) == 384)
        if payload_validator is not None:
            payload = payload_validator(payload)
            _require(canonical(payload, max_bytes=8192).encode('ascii') == payload_raw)
        key = _pin(pins, kid, purpose, now_ms, purposes=purposes,
                   record_window=payload if cover_lifetime else None, allow_expired=allow_expired)
        message = b'SDC-SIGNED-v1\n' + purpose.encode('ascii') + b'\n' + kid.encode('ascii') + b'\n' + payload_raw
        _verify_pss(key, signature, message)
        return payload
    except SignatureError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, UnicodeError, binascii.Error):
        raise SignatureError() from None



def verify_envelope(raw, *, purpose, pins, now_ms):
    """Verify R4a signature/encoding only; callers separately authorize payload semantics.

    Pins are trusted caller input, never adopted from an envelope or fetched here.
    Restart purposes are deliberately excluded from this legacy API.
    """
    return _verify_envelope(raw, purpose=purpose, pins=pins, now_ms=now_ms, purposes=PURPOSES)


def verify_restart_envelope(raw, *, purpose, pins, expected, now_ms, allow_expired=False):
    """Verify typed restart evidence and independent expected bindings, not execution authority.

    All 15 expected fields must come from trusted retained local registration and
    approval, never from this payload. Additional expected fields must exist and
    match exact CJSON. No key/origin discovery or restart connection occurs here.
    allow_expired=True is explicit historical evidence verification only: both the
    record and its pin may now be expired, but the record's entire signed lifetime
    must fit the pin and its not_before may never be in the future. This grants no
    current authority or permission to start/repeat an operation. Before any new
    I/O callers must use current validate_restart_pins and separate authorization.
    """
    try:
        from adapters.sdc.restart_contract import EXPECTED_FIELDS, validate_typed_payload
        _require(type(allow_expired) is bool and type(pins) is list and type(expected) is dict)
        bindings = parse_canonical(canonical(expected, max_bytes=8192), max_bytes=8192)
        pinned = parse_canonical(canonical(pins, max_bytes=16384), max_bytes=16384)
        _pin_set(pinned, RESTART_PURPOSES)
        _require(set(EXPECTED_FIELDS) <= set(bindings))
        _require(type(bindings['params']) is dict and set(bindings['params']) == {
            'service_id', 'expected_runtime_generation', 'effects_profile'})

        def validate(payload):
            fixed = validate_typed_payload(purpose, payload, now_ms=now_ms, allow_expired=allow_expired)
            _require(all(key in fixed and canonical(value, max_bytes=8192) == canonical(fixed[key], max_bytes=8192)
                         for key, value in bindings.items()))
            return fixed

        return _verify_envelope(raw, purpose=purpose, pins=pinned, now_ms=now_ms, purposes=RESTART_PURPOSES,
                                payload_validator=validate, cover_lifetime=True, allow_expired=allow_expired)
    except SignatureError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, UnicodeError, ImportError):
        raise SignatureError() from None
