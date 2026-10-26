"""Public central vectors only; no enrollment, credential store or network is touched."""
import copy
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from adapters.sdc import protocol
from adapters.sdc import release_registration_contract as contract
from adapters.sdc.support_contract import canonical
from mcs.ops.sdc_support_signatures import (
    SignatureError, validate_registration_pins, verify_registration_envelope, verify_apply_envelope,
)

ROOT = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/releases'
GOLD = json.loads((ROOT / 'registration-vectors.json').read_text())
NOW = GOLD['now']


def raw(v):
    return canonical(v).encode('ascii')


@pytest.mark.parametrize('name', list(GOLD['proofs']))
def test_central_public_proofs_verify_without_changing_their_status(name):
    expected = {k: GOLD['proofs'][name][k] for k in contract.EXPECTED}
    assert verify_registration_envelope(raw(GOLD['envelopes'][name]), pins=GOLD['pins'], expected=expected, now_ms=NOW) == GOLD['proofs'][name]


@pytest.mark.parametrize('field', contract.EXPECTED)
def test_signed_proof_requires_independent_exact_request_and_device_bindings(field):
    expected = copy.deepcopy(GOLD['expected'])
    expected[field] = 'other' if expected[field] is not None else '00000009-0000-4000-8000-000000000001'
    with pytest.raises(SignatureError):
        verify_registration_envelope(raw(GOLD['envelopes']['registered']), pins=GOLD['pins'], expected=expected, now_ms=NOW)
    del expected[field]
    with pytest.raises(SignatureError):
        verify_registration_envelope(raw(GOLD['envelopes']['registered']), pins=GOLD['pins'], expected=expected, now_ms=NOW)


def test_proof_has_no_execution_purpose_and_cannot_refresh_expired_bytes():
    for now in (NOW + 30000, True, -1, 2**53):
        with pytest.raises(SignatureError):
            verify_registration_envelope(raw(GOLD['envelopes']['registered']), pins=GOLD['pins'], expected=GOLD['expected'], now_ms=now)
    with pytest.raises(SignatureError):
        verify_apply_envelope(raw(GOLD['envelopes']['registered']), purpose=contract.PURPOSE, pins=GOLD['pins'], expected=GOLD['expected'], now_ms=NOW)
    pins = copy.deepcopy(GOLD['pins'])
    pins[0]['purposes'].append('release-apply-operation')
    with pytest.raises(SignatureError):
        validate_registration_pins(pins, now_ms=NOW)
    envelope = copy.deepcopy(GOLD['envelopes']['registered'])
    envelope['signature'] = envelope['signature'][:-4] + 'AAAA'
    with pytest.raises(SignatureError):
        verify_registration_envelope(raw(envelope), pins=GOLD['pins'], expected=GOLD['expected'], now_ms=NOW)


@pytest.mark.parametrize('change', [lambda v: v.update(expires_at=NOW + 30001), lambda v: v.update(approved=True),
    lambda v: v['owner'].update(owner_evidence_sha256='0' * 64), lambda v: v['registration'].update(device_evidence_sha256='0' * 64),
    lambda v: v['registration'].update(key_id='00000009-0000-4000-8000-000000000001')])
def test_invalid_semantics_are_rejected_before_signature_authority(change):
    value = copy.deepcopy(GOLD['proofs']['registered'])
    change(value)
    with pytest.raises(ValueError):
        contract.validate_proof(value, now_ms=NOW)


def test_fixed_read_route_uses_actual_device_hmac_and_never_accepts_dynamic_paths():
    config = {'origin': 'https://synthetic.invalid', 'audience': GOLD['expected']['audience'], 'environment': 'synthetic'}
    state = {k: GOLD['expected'][k] for k in ('device_id', 'key_id')}
    body, secret, nonce = raw(GOLD['request']), 'a1' * 32, 'b2' * 16
    headers = protocol.sign_release_registration(config, contract.PATH, body, state, secret, now=NOW / 1000, nonce=nonce)
    message = '\n'.join(('SDC-HMAC-v1', config['audience'], 'POST', contract.PATH, state['device_id'], state['key_id'], str(NOW // 1000), nonce, hashlib.sha256(body).hexdigest()))
    assert headers['X-SDC-Signature'] == hmac.new(bytes.fromhex(secret), message.encode(), hashlib.sha256).hexdigest()
    for path in (contract.PATH + '?x=1', contract.PATH + '/', '/v2/releases/apply/operations/grant'):
        with pytest.raises(ValueError):
            protocol.sign_release_registration(config, path, body, state, secret, now=NOW / 1000, nonce=nonce)
    with pytest.raises(ValueError):
        protocol.sign_release_registration(config, contract.PATH, raw({**GOLD['request'], 'approved': True}), state, secret, now=NOW / 1000, nonce=nonce)
