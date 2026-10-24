"""Public synthetic signatures and temporary crypto I/O; no real secrets or external services."""
import base64
import copy
import io
import json
from pathlib import Path
import subprocess

import pytest

import sdc_support_signatures as signatures

ROOT = Path(__file__).resolve().parents[2]
VECTOR = json.loads((ROOT / 'adapters/sdc/contracts/support/restart-signature-vectors.json').read_text())
OLD = json.loads((ROOT / 'adapters/sdc/contracts/support/signature-vectors.json').read_text())
CANARY = 'SYNTHETIC_PRIVATE_DO_NOT_ECHO'
PURPOSES = ('restart-operation', 'restart-execution-grant', 'restart-consent-preview')


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def verify(envelope=None, *, index=0, purpose=None, pins=None, expected=None, now=None, allow_expired=False):
    vector = VECTOR['vectors'][index]
    return signatures.verify_restart_envelope(encoded(vector['envelope'] if envelope is None else envelope),
        purpose=vector['purpose'] if purpose is None else purpose,
        pins=copy.deepcopy(VECTOR['pinned_keys']) if pins is None else pins,
        expected=copy.deepcopy(VECTOR['expected']) if expected is None else expected,
        now_ms=VECTOR['now'] if now is None else now, allow_expired=allow_expired)


@pytest.fixture
def no_crypto(monkeypatch):
    monkeypatch.setattr(signatures, '_verify_pss', lambda *_: pytest.fail('invalid input reached crypto'))


@pytest.mark.parametrize('index', [0, 1, 2])
def test_fixed_restart_ps256_vectors_with_os_openssl(index):
    result = verify(index=index)
    assert encoded(result).decode('ascii') == VECTOR['vectors'][index]['payload_canonical']
    assert result == VECTOR['vectors'][index]['payload']


def test_closed_three_purpose_sets_are_disjoint_without_widening_old_api():
    assert signatures.RESTART_PURPOSES == set(PURPOSES)
    assert signatures.PURPOSES == {'operation', 'execution-grant', 'consent-preview'}
    assert not signatures.RESTART_PURPOSES & signatures.PURPOSES


@pytest.mark.parametrize('index', [0, 1, 2])
def test_old_verifier_rejects_restart_vectors_before_crypto(no_crypto, index):
    vector = VECTOR['vectors'][index]
    with pytest.raises(signatures.SignatureError):
        signatures.verify_envelope(encoded(vector['envelope']), purpose=vector['purpose'], pins=VECTOR['pinned_keys'], now_ms=VECTOR['now'])


@pytest.mark.parametrize('index', range(len(OLD['vectors'])))
def test_old_fixed_vectors_remain_accepted_by_legacy_api_with_os_crypto(index):
    vector = OLD['vectors'][index]
    assert signatures.verify_envelope(encoded(vector['envelope']), purpose=vector['purpose'], pins=OLD['pinned_keys'], now_ms=OLD['now']) == vector['payload']


@pytest.mark.parametrize('index', range(len(OLD['vectors'])))
def test_restart_verifier_rejects_old_purpose_and_relabelled_old_record(no_crypto, index):
    old = copy.deepcopy(OLD['vectors'][index]['envelope'])
    with pytest.raises(signatures.SignatureError):
        verify(old, purpose=old['purpose'])
    old['purpose'] = 'restart-operation'
    with pytest.raises(signatures.SignatureError):
        verify(old, purpose='restart-operation')


@pytest.mark.parametrize('missing', list(VECTOR['expected']))
def test_all_fifteen_trusted_expected_fields_are_mandatory(no_crypto, missing):
    assert len(VECTOR['expected']) == 15
    expected = copy.deepcopy(VECTOR['expected'])
    del expected[missing]
    with pytest.raises(signatures.SignatureError):
        verify(expected=expected)


@pytest.mark.parametrize('expected', [None, {}, [], CANARY, 1, True])
def test_expected_is_never_inferred_from_payload(no_crypto, expected):
    with pytest.raises(signatures.SignatureError) as caught:
        signatures.verify_restart_envelope(encoded(VECTOR['vectors'][0]['envelope']), purpose='restart-operation',
            pins=VECTOR['pinned_keys'], expected=expected, now_ms=VECTOR['now'])
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize('missing', ['service_id', 'expected_runtime_generation', 'effects_profile'])
def test_expected_params_are_closed_and_complete(no_crypto, missing):
    expected = copy.deepcopy(VECTOR['expected'])
    del expected['params'][missing]
    with pytest.raises(signatures.SignatureError):
        verify(expected=expected)


@pytest.mark.parametrize('field', list(VECTOR['expected']))
def test_independent_expected_mismatch_is_rejected(no_crypto, field):
    expected = copy.deepcopy(VECTOR['expected'])
    if field == 'params':
        expected[field]['expected_runtime_generation'] = '55555555555545559555555555555555'
    elif type(expected[field]) is int:
        expected[field] += 1
    else:
        expected[field] = 'different'
    with pytest.raises(signatures.SignatureError):
        verify(expected=expected)


@pytest.mark.parametrize('changes', [{'policy_epoch': True}, {'extra': CANARY}, {'service_revision': 1.0},
                                     {'params': {'service_id': 'mcs.standalone', 'expected_runtime_generation': '0' * 32,
                                                 'effects_profile': 'mcs.standalone.restart.v1', 'extra': CANARY}}])
def test_exact_cjson_binding_distinguishes_bool_int_float_and_unknown_fields(no_crypto, changes):
    with pytest.raises(signatures.SignatureError) as caught:
        verify(expected={**copy.deepcopy(VECTOR['expected']), **changes})
    assert CANARY not in str(caught.value)


def test_extra_expected_existing_hashes_are_checked_and_detached(monkeypatch):
    monkeypatch.setattr(signatures, '_verify_pss', lambda *_: None)
    expected = copy.deepcopy(VECTOR['expected'])
    operation = VECTOR['vectors'][0]['payload']
    expected.update({k: operation[k] for k in ('schema_version', 'preview_payload_hash', 'local_change_receipt_hash')})
    assert verify(expected=expected) == operation
    for key in ('preview_payload_hash', 'local_change_receipt_hash'):
        with pytest.raises(signatures.SignatureError):
            verify(expected={**expected, key: 'f' * 64})


@pytest.mark.parametrize('uses', [['operation'], ['execution-grant'], ['consent-preview'], ['restart-operation', 'operation'],
                                 ['restart-operation', 'restart-operation'], ['restart-operation', 'release'], []])
def test_restart_pins_cannot_mix_old_unknown_or_duplicate_purposes(no_crypto, uses):
    pins = copy.deepcopy(VECTOR['pinned_keys'])
    pins[0]['purposes'] = uses
    with pytest.raises(signatures.SignatureError):
        verify(pins=pins)
    with pytest.raises(signatures.SignatureError):
        signatures.validate_restart_pins(pins, purpose='restart-operation', now_ms=VECTOR['now'])


@pytest.mark.parametrize('change', ['duplicate', 'mixed', 'tuple', 'unknown', 'extra', 'empty'])
def test_pin_set_is_explicit_bounded_and_closed(no_crypto, change):
    pins = copy.deepcopy(VECTOR['pinned_keys'])
    if change == 'duplicate':
        pins += copy.deepcopy(pins)
    elif change == 'mixed':
        pins += copy.deepcopy(OLD['pinned_keys'])
    elif change == 'tuple':
        pins = tuple(pins)
    elif change == 'unknown':
        pins[0]['kid'] = 'different'
    elif change == 'extra':
        pins[0]['url'] = CANARY
    else:
        pins = []
    with pytest.raises(signatures.SignatureError):
        verify(pins=pins)


@pytest.mark.parametrize('change', ['algorithm', 'exponent', 'modulus', 'trailing', 'encoding'])
def test_restart_der_reuses_rsa3072_e65537_algorithm_and_minimal_encoding(no_crypto, change):
    pins = copy.deepcopy(VECTOR['pinned_keys'])
    raw = bytearray(base64.urlsafe_b64decode(pins[0]['spki'] + '=' * (-len(pins[0]['spki']) % 4)))
    if change == 'algorithm':
        marker = bytes.fromhex('06092a864886f70d0101010500')
        start = raw.index(marker)
        raw[start + 10] = 10  # Restricted PSS OID must not replace rsaEncryption.
    elif change == 'exponent':
        raw[-1] = 3
    elif change == 'modulus':
        marker = bytes.fromhex('0282018100')
        raw[raw.index(marker) + len(marker)] &= 127
    elif change == 'trailing':
        raw.append(0)
    else:
        pins[0]['spki'] += '='
    if change != 'encoding':
        pins[0]['spki'] = b64(raw)
    with pytest.raises(signatures.SignatureError):
        verify(pins=pins)


@pytest.mark.parametrize('index', [0, 1, 2])
def test_pin_covers_whole_signed_lifetime_not_only_current_instant(no_crypto, index):
    payload = VECTOR['vectors'][index]['payload']
    for changes in ({'not_before': payload['issued_at'] + 1}, {'expires_at': payload['expires_at'] - 1}):
        pins = copy.deepcopy(VECTOR['pinned_keys'])
        pins[0].update(changes)
        with pytest.raises(signatures.SignatureError):
            verify(index=index, pins=pins)


def test_explicit_historical_mode_allows_record_and_pin_expiry_but_never_future_or_current_io():
    now = VECTOR['pinned_keys'][0]['expires_at'] + 1
    with pytest.raises(signatures.SignatureError):
        verify(now=now)
    assert verify(now=now, allow_expired=True) == VECTOR['vectors'][0]['payload']
    with pytest.raises(signatures.SignatureError):
        signatures.validate_restart_pins(VECTOR['pinned_keys'], purpose='restart-operation', now_ms=now)
    with pytest.raises(signatures.SignatureError):
        verify(now=VECTOR['vectors'][0]['payload']['not_before'] - 1, allow_expired=True)


@pytest.mark.parametrize('value', [None, 1, 'true', [], {}])
def test_history_exception_requires_strict_explicit_bool(no_crypto, value):
    with pytest.raises(signatures.SignatureError):
        verify(allow_expired=value)


@pytest.mark.parametrize('index', [0, 1, 2])
def test_purpose_relabel_never_substitutes_restart_record(no_crypto, index):
    original = VECTOR['vectors'][index]
    for purpose in set(PURPOSES) - {original['purpose']}:
        with pytest.raises(signatures.SignatureError):
            verify(index=index, purpose=purpose)
        envelope = {**original['envelope'], 'purpose': purpose}
        with pytest.raises(signatures.SignatureError):
            verify(envelope, purpose=purpose)


@pytest.mark.parametrize('change', ['algorithm', 'extra', 'payload_padding', 'signature_length', 'payload_noncanonical',
                                  'duplicate_payload', 'bad_hash', 'unsafe_integer', 'non_ascii', 'oversized'])
def test_raw_payload_and_envelope_reject_before_crypto(no_crypto, change):
    envelope = copy.deepcopy(VECTOR['vectors'][0]['envelope'])
    if change == 'algorithm':
        envelope['alg'] = 'RS256'
    elif change == 'extra':
        envelope['key_url'] = CANARY
    elif change == 'payload_padding':
        envelope['payload'] += '='
    elif change == 'signature_length':
        envelope['signature'] = b64(b'a' * 383)
    elif change == 'payload_noncanonical':
        envelope['payload'] = b64(VECTOR['vectors'][0]['payload_canonical'].encode() + b'\n')
    elif change == 'duplicate_payload':
        raw = VECTOR['vectors'][0]['payload_canonical'].replace('{', '{"type":"service.restart",', 1)
        envelope['payload'] = b64(raw.encode())
    elif change == 'bad_hash':
        envelope['payload'] = b64(encoded({**VECTOR['vectors'][0]['payload'], 'payload_hash': '0' * 64}))
    elif change == 'unsafe_integer':
        envelope['payload'] = b64(encoded({**VECTOR['vectors'][0]['payload'], 'policy_epoch': 2**53}))
    elif change == 'non_ascii':
        envelope['payload'] = b64(b'\xff')
    else:
        envelope['payload'] = 'a' * 12000
    with pytest.raises(signatures.SignatureError):
        verify(envelope)


def test_duplicate_outer_keys_and_raw_bounds_reject(no_crypto):
    raw = encoded(VECTOR['vectors'][0]['envelope'])
    for invalid in (raw.replace(b'{', b'{"alg":"PS256",', 1), b' ' * 16385, b'\xff', raw.decode()):
        with pytest.raises(signatures.SignatureError):
            signatures.verify_restart_envelope(invalid, purpose='restart-operation', pins=VECTOR['pinned_keys'],
                                                expected=VECTOR['expected'], now_ms=VECTOR['now'])


def test_modified_signature_is_rejected_with_os_crypto():
    envelope = copy.deepcopy(VECTOR['vectors'][0]['envelope'])
    signature = bytearray(base64.urlsafe_b64decode(envelope['signature']))
    signature[0] ^= 1
    envelope['signature'] = b64(signature)
    with pytest.raises(signatures.SignatureError):
        verify(envelope)


def test_restart_current_pin_preflight_never_starts_crypto_and_returns_detached(no_crypto):
    pins = copy.deepcopy(VECTOR['pinned_keys'])
    for purpose in PURPOSES:
        result = signatures.validate_restart_pins(pins, purpose=purpose, now_ms=VECTOR['now'])
        assert result == pins and result is not pins and result[0] is not pins[0]
    with pytest.raises(signatures.SignatureError):
        signatures.validate_pins(pins, purpose='restart-operation', now_ms=VECTOR['now'])


def test_restart_uses_shared_fixed_pss_process_and_exact_decoded_domain_message(monkeypatch):
    paths, messages = [], []
    monkeypatch.setenv('SECRET_CANARY', CANARY)
    class Child:
        returncode = 0
        stdin = io.BytesIO()
        def communicate(self, *, input, timeout):
            messages.append(input)
            assert 0 < timeout <= 0.05
            self.stdin.close()
    def popen(argv, **kwargs):
        assert argv[:3] == ['/usr/bin/openssl', 'dgst', '-sha256']
        assert argv[-6:] == ['-sigopt', 'rsa_padding_mode:pss', '-sigopt', 'rsa_pss_saltlen:32', '-sigopt', 'rsa_mgf1_md:sha256']
        assert kwargs == {'stdin': subprocess.PIPE, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
                          'env': {'LC_ALL': 'C', 'OPENSSL_CONF': '/dev/null'}, 'start_new_session': True}
        assert CANARY not in str(argv) + str(kwargs)
        for flag in ('-verify', '-signature'):
            path = Path(argv[argv.index(flag) + 1])
            assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
            paths.append(path)
        return Child()
    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    assert verify() == VECTOR['vectors'][0]['payload']
    assert messages == [VECTOR['vectors'][0]['signed_ascii'].encode('ascii')]
    assert not messages[0].endswith(b'\n')
    assert all(not path.exists() and not path.parent.exists() for path in paths)


@pytest.mark.parametrize('failure', ['timeout', 'cancel', 'spawn', 'unknown', 'nonzero'])
def test_restart_shared_process_cleanup_and_fixed_errors(monkeypatch, failure, capsys):
    paths, stopped = [], []
    clock = [100.0]
    if failure == 'timeout':
        monkeypatch.setattr(signatures.time, 'monotonic', lambda: clock[0])
    class Child:
        returncode = 1 if failure == 'nonzero' else None
        stdin = io.BytesIO()
        def communicate(self, **kwargs):
            if failure == 'timeout':
                clock[0] += signatures.VERIFY_TIMEOUT
                raise subprocess.TimeoutExpired(CANARY, 5, output=CANARY, stderr=CANARY)
            if failure == 'cancel':
                raise KeyboardInterrupt(CANARY)
            if failure == 'unknown':
                raise RuntimeError(CANARY)
            self.stdin.close()
    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index('-verify') + 1]).parent)
        if failure == 'spawn':
            raise OSError(CANARY)
        return Child()
    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    def stop(child):
        stopped.append(child)
        child.returncode = -9
        child.stdin.close()
    monkeypatch.setattr(signatures.owned_process, 'stop_owned', stop)
    with pytest.raises(KeyboardInterrupt if failure == 'cancel' else signatures.SignatureError) as caught:
        verify()
    assert CANARY not in str(caught.value)
    assert bool(stopped) == (failure in ('timeout', 'cancel', 'unknown'))
    assert all(not path.exists() for path in paths)
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err
