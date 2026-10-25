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


APPLY_PURPOSES = frozenset({'release-apply-consent-preview', 'release-apply-operation', 'release-apply-execution-grant'})


def validate_apply_pins(pins, *, purpose, now_ms):
    """release.apply専用の公開pin集合を検査し、旧purposeとの共用を拒否する。"""
    try:
        pinned = parse_canonical(canonical(pins, max_bytes=16384), max_bytes=16384)
        return _validate_pins(pinned, purpose=purpose, now_ms=now_ms, purposes=APPLY_PURPOSES)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, UnicodeError):
        raise SignatureError() from None


def verify_apply_envelope(raw, *, purpose, pins, expected, now_ms, allow_expired=False):
    """独立した登録・承認との束縛とrelease.apply署名だけを検証する。"""
    try:
        from adapters.sdc.release_apply_contract import EXPECTED_FIELDS, validate_apply_typed_payload
        _require(type(allow_expired) is bool and type(pins) is list and type(expected) is dict)
        bindings = parse_canonical(canonical(expected), max_bytes=8192)
        pinned = parse_canonical(canonical(pins, max_bytes=16384), max_bytes=16384)
        _pin_set(pinned, APPLY_PURPOSES)
        _require(set(EXPECTED_FIELDS) <= set(bindings))

        def validate(payload):
            v = validate_apply_typed_payload(purpose, payload, now_ms=now_ms, allow_expired=allow_expired)
            _require(all(k in v and canonical(value) == canonical(v[k]) for k, value in bindings.items()))
            return v

        return _verify_envelope(raw, purpose=purpose, pins=pinned, now_ms=now_ms, purposes=APPLY_PURPOSES,
                                payload_validator=validate, cover_lifetime=True, allow_expired=allow_expired)
    except SignatureError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, UnicodeError, ImportError):
        raise SignatureError() from None


class ReleaseSignatureError(ValueError):
    """リリース署名の固定拒否コードだけを公開する。"""
    def __init__(self, code='RELEASE_SIGNATURE_INVALID'):
        self.code = code
        super().__init__(code)


def _release_require(ok, code='RELEASE_SIGNATURE_INVALID'):
    if not ok:
        raise ReleaseSignatureError(code)


def _release_public_key(spki):
    try:
        key = _spki(_b64(spki, 2731))
        outer, _ = _tlv(key, 0, 0x30)
        _, position = _tlv(outer, 0, 0x30)
        bits, _ = _tlv(outer, position, 0x03)
        public, _ = _tlv(bits, 1, 0x30)
        modulus, _ = _tlv(public, 0, 0x02)
        n = base64.urlsafe_b64encode(modulus[1:]).rstrip(b'=').decode('ascii')
        import hashlib
        kid = hashlib.sha256(canonical(dict(e='AQAB', kty='RSA', n=n)).encode('ascii')).hexdigest()
        return dict(key=key, n=n, kid=kid)
    except (ValueError, TypeError, KeyError, IndexError, binascii.Error):
        raise ReleaseSignatureError('RELEASE_SIGNATURE_KEY_DENIED') from None


def release_key_fingerprint(spki):
    """RSA3072/e65537の正規化した公開JWKからkidを計算する。"""
    return _release_public_key(spki)['kid']


def _release_snapshot(value, limit=8192):
    from adapters.sdc.release_contract import input_record
    try:
        return input_record(value, limit, plain_integers=False)
    except ValueError:
        raise ReleaseSignatureError() from None


def _release_context(expected, now_ms):
    from adapters.sdc.release_contract import EXPECTED_FIELDS, integer
    _release_require(integer(now_ms))
    _release_require(type(expected) is dict, 'RELEASE_SIGNATURE_SCOPE_DENIED')
    v = _release_snapshot(expected)
    _release_require(set(v) == set(EXPECTED_FIELDS) and all(type(v[k]) is str for k in EXPECTED_FIELDS), 'RELEASE_SIGNATURE_SCOPE_DENIED')
    return v


def _release_scope(v, expected):
    _release_require(all(v.get(k) == x for k, x in expected.items()), 'RELEASE_SIGNATURE_SCOPE_DENIED')


def _release_trust(value, now_ms, allow_expired=False):
    from adapters.sdc.release_contract import validate_release_trust
    try:
        return validate_release_trust(value, now_ms=now_ms, allow_expired=allow_expired)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ReleaseSignatureError('RELEASE_SIGNATURE_KEY_DENIED') from None


def _release_unpack(value, purpose):
    try:
        from adapters.sdc.release_contract import hex64
        envelope = _release_snapshot(value, 16384)
        _release_require(type(envelope) is dict and set(envelope) == {'alg', 'kid', 'purpose', 'payload', 'signature'})
        _release_require(envelope['alg'] == 'PS256' and hex64(envelope['kid']) and envelope['purpose'] == purpose)
        raw = _b64(envelope['payload'], 10923)
        payload = parse_canonical(raw, max_bytes=8192)
        _release_require(type(payload) is dict)
        signature = _b64(envelope['signature'], 512)
        _release_require(len(signature) == 384)
        return dict(envelope=envelope, payload=payload, raw=raw, signature=signature)
    except ReleaseSignatureError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, UnicodeError):
        raise ReleaseSignatureError() from None


def _release_proof(value):
    if type(value) in (bytes, str):
        try:
            text = value.decode('utf-8') if type(value) is bytes else value
            _release_require(len(text.encode('utf-8')) <= 98304 and not text.startswith('\ufeff'))
        except UnicodeError:
            raise ReleaseSignatureError() from None

        def pairs(items):
            result = {}
            for k, v in items:
                _release_require(k not in result)
                result[k] = v
            return result

        try:
            value = json.loads(text, object_pairs_hook=pairs)
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise ReleaseSignatureError() from None
    _release_require(type(value) is list and 2 <= len(value) <= 6)
    entries = [_release_unpack(e, 'release-root') for e in value]
    _release_require(len({e['envelope']['kid'] for e in entries}) == len(entries))
    _release_require(all(e['envelope']['payload'] == entries[0]['envelope']['payload'] for e in entries))
    _release_require(1 + sum(len(canonical(e['envelope'], max_bytes=16384)) + 1 for e in entries) <= 98304)
    return entries


def _release_keyring(trust):
    roots, releases, moduli = {}, {}, set()
    for records, dest in ((trust['root_keys'], roots), (trust['release_keys'], releases)):
        for record in records:
            key = _release_public_key(record['spki'])
            _release_require(key['kid'] == record['kid'] and key['kid'] not in roots and key['kid'] not in releases and key['n'] not in moduli, 'RELEASE_SIGNATURE_KEY_DENIED')
            moduli.add(key['n'])
            dest[record['kid']] = dict(**key, record=record)
    return roots, releases


def _release_valid(entry, key):
    e = entry['envelope']
    message = b'SDC-SIGNED-v1\n' + e['purpose'].encode('ascii') + b'\n' + e['kid'].encode('ascii') + b'\n' + entry['raw']
    try:
        _verify_pss(key['key'], entry['signature'], message)
        return True
    except SignatureError as exc:
        if exc.code == 'SDC_SUPPORT_CRYPTO_UNAVAILABLE':
            raise
        return False


def _release_votes(entries, allowed, retired=()):
    votes = set()
    for e in entries:
        kid = e['envelope']['kid']
        key = allowed.get(kid)
        _release_require(key is not None or kid in retired, 'RELEASE_SIGNATURE_KEY_DENIED')
        if key is not None and _release_valid(e, key):
            votes.add(kid)
    return votes


def _release_monotonic(old, new):
    code = 'RELEASE_TRUST_CONFLICT'
    roots, releases = {k['kid'] for k in new['root_keys']}, {k['kid'] for k in new['release_keys']}
    _release_require(set(old['retired_root_kids']) <= set(new['retired_root_kids']) and set(old['revoked_release_kids']) <= set(new['revoked_release_kids']), code)
    _release_require(all(k['kid'] in roots or k['kid'] in new['retired_root_kids'] for k in old['root_keys']), code)
    _release_require(all(k['kid'] in releases or k['kid'] in new['revoked_release_kids'] for k in old['release_keys']), code)
    old_roots = {k['kid'] for k in old['root_keys']} | set(old['retired_root_kids'])
    old_releases = {k['kid'] for k in old['release_keys']} | set(old['revoked_release_kids'])
    _release_require(not roots & old_releases and not releases & old_roots, code)
    r = new['recovery']
    if r is None:
        _release_require(new['metadata_epoch'] == old['metadata_epoch'], code)
    else:
        _release_require(new['metadata_epoch'] == old['metadata_epoch'] + 1 and r['previous_epoch'] == old['metadata_epoch'] and r['new_epoch'] == new['metadata_epoch'], code)
        _release_require(all(k in {x['kid'] for x in old['release_keys']} and k in new['revoked_release_kids'] and k not in releases for k in r['affected_release_kids']), code)


def verify_release_bootstrap(value, *, expected_trust_hash, expected, now_ms):
    """独立配布されたdigestと2つのroot票でTrustを検証し、採用は行わない。"""
    from adapters.sdc.release_contract import hash_release_trust, hex64
    bindings = _release_context(expected, now_ms)
    entries = _release_proof(value)
    trust = _release_trust(entries[0]['payload'], now_ms)
    _release_require(hex64(expected_trust_hash), 'RELEASE_SIGNATURE_SCOPE_DENIED')
    _release_scope(trust, bindings)
    _release_require(hash_release_trust(trust) == expected_trust_hash, 'RELEASE_SIGNATURE_SCOPE_DENIED')
    roots, _ = _release_keyring(trust)
    _release_require(len(_release_votes(entries, roots, trust['retired_root_kids'])) >= 2, 'RELEASE_SIGNATURE_THRESHOLD_DENIED')
    return trust


def verify_release_root_chain(values, *, trusted, expected, now_ms):
    """最大32遷移を旧・新root閾値で検証し、最終Trustの有効期限を要求する。"""
    from adapters.sdc.release_contract import hash_release_trust, integer
    bindings = _release_context(expected, now_ms)
    current = _release_trust(_release_snapshot(trusted), now_ms, True)
    _release_scope(current, bindings)
    _release_require(type(values) is list and 1 <= len(values) <= 32)
    chain = [_release_proof(v) for v in values]
    roots, _ = _release_keyring(current)
    for i, entries in enumerate(chain):
        candidate = _release_trust(entries[0]['payload'], now_ms, i < len(chain) - 1)
        _release_scope(candidate, bindings)
        if candidate['root_version'] == current['root_version']:
            _release_require(hash_release_trust(candidate) == hash_release_trust(current), 'RELEASE_TRUST_CONFLICT')
            _release_require(len(_release_votes(entries, roots, current['retired_root_kids'])) >= 2, 'RELEASE_SIGNATURE_THRESHOLD_DENIED')
            continue
        _release_require(integer(current['root_version'] + 1) and candidate['root_version'] == current['root_version'] + 1 and candidate['previous_trust_digest'] == hash_release_trust(current) and candidate['issued_at'] >= current['issued_at'], 'RELEASE_TRUST_CONFLICT')
        _release_monotonic(current, candidate)
        next_roots, _ = _release_keyring(candidate)
        for kid, key in next_roots.items():
            _release_require(kid not in roots or roots[kid]['n'] == key['n'], 'RELEASE_SIGNATURE_KEY_DENIED')
        counted = _release_votes(entries, {**roots, **next_roots})
        _release_require(len(set(roots) & counted) >= 2 and len(set(next_roots) & counted) >= 2, 'RELEASE_SIGNATURE_THRESHOLD_DENIED')
        current, roots = candidate, next_roots
    return _release_trust(current, now_ms)


def verify_release_root(value, *, trusted, expected, now_ms):
    """1遷移のroot署名を検証し、永続的な採用は行わない。"""
    return verify_release_root_chain([value], trusted=trusted, expected=expected, now_ms=now_ms)


def verify_release_attestation(value, *, trust, expected, now_ms):
    """現在のTrustと独立scopeに束縛したrelease署名を検証する。"""
    from adapters.sdc.release_contract import validate_release_attestation
    bindings = _release_context(expected, now_ms)
    authority = _release_trust(_release_snapshot(trust), now_ms)
    entry = _release_unpack(value, 'release-attestation')
    _release_scope(authority, bindings)
    _release_scope(entry['payload'], bindings)
    try:
        payload = validate_release_attestation(entry['payload'], trust=authority, now_ms=now_ms)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ReleaseSignatureError() from None
    _, releases = _release_keyring(authority)
    key = releases.get(entry['envelope']['kid'])
    _release_require(key is not None and entry['envelope']['kid'] not in authority['revoked_release_kids'], 'RELEASE_SIGNATURE_KEY_DENIED')
    record = key['record']
    _release_require(record['not_before'] <= payload['issued_at'] and payload['expires_at'] <= record['expires_at'] and record['not_before'] <= now_ms < record['expires_at'], 'RELEASE_SIGNATURE_KEY_DENIED')
    _release_require(_release_valid(entry, key))
    return payload
