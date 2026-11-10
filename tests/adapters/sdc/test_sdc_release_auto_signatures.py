"""v3署名envelopeの検証とI1相互運用fixture。

fixtureはJS(central)側とPython(端末)側の両実装で生成したenvelopeを含み、
双方向の署名・canonical・message形式の一致を検証する。秘密鍵は含まない。
"""
import json
from pathlib import Path

import pytest

from mcs.ops.sdc_support_signatures import (
    SignatureError, validate_auto_v3_pins, verify_v3_envelope,
)

ROOT = Path(__file__).resolve().parent / 'fixtures' / 'release-auto-v3-signature-vectors.json'
FIXTURE = json.loads(ROOT.read_text())
VECTORS = FIXTURE['vectors']


def envelope_bytes(vector):
    return json.dumps(vector['envelope'], separators=(',', ':'), ensure_ascii=True).encode('ascii')


@pytest.mark.parametrize('vector', VECTORS, ids=[v['name'] for v in VECTORS])
def test_verify_v3_envelope_accepts_cross_implementation_vectors(vector):
    payload = verify_v3_envelope(
        envelope_bytes(vector), purpose=vector['purpose'], pins=vector['pins'],
        expected=vector['expected'], now_ms=vector['now_ms'])
    assert payload['schema_version']


def test_verify_v3_envelope_rejects_scope_and_signature_mutations():
    vector = VECTORS[0]
    with pytest.raises(SignatureError):
        verify_v3_envelope(envelope_bytes(vector), purpose=vector['purpose'], pins=vector['pins'],
                           expected={'request_id': '0' * 8 + '-0000-4000-8000-000000000000'},
                           now_ms=vector['now_ms'])
    other = next(v for v in VECTORS if v['purpose'] != vector['purpose'])
    with pytest.raises(SignatureError):
        verify_v3_envelope(envelope_bytes(vector), purpose=other['purpose'], pins=vector['pins'],
                           expected=vector['expected'], now_ms=vector['now_ms'])
    tampered = dict(vector['envelope'], signature=vector['envelope']['signature'][:-2] + 'AA')
    with pytest.raises(SignatureError):
        verify_v3_envelope(json.dumps(tampered).encode('ascii'), purpose=vector['purpose'],
                           pins=vector['pins'], expected=vector['expected'], now_ms=vector['now_ms'])


def test_verify_v3_envelope_rejects_pin_without_purpose_and_expired_pin():
    vector = VECTORS[0]
    wrong_purpose_pin = [dict(vector['pins'][0], purposes=['mcs-auto-grant-v3'])]
    with pytest.raises(SignatureError):
        verify_v3_envelope(envelope_bytes(vector), purpose=vector['purpose'], pins=wrong_purpose_pin,
                           expected=vector['expected'], now_ms=vector['now_ms'])
    expired = [dict(vector['pins'][0], not_before=0, expires_at=vector['now_ms'] - 1)]
    # pinの有効期間がpayload expires_atを内包しないため拒否
    with pytest.raises(SignatureError):
        verify_v3_envelope(envelope_bytes(vector), purpose=vector['purpose'], pins=expired,
                           expected=vector['expected'], now_ms=vector['now_ms'])
    # historical=Trueは期限切れpinでの過去記録検証のみ許容するが、
    # payload expires_atがpin expires_atを超える場合は引き続き拒否する。
    with pytest.raises(SignatureError):
        verify_v3_envelope(envelope_bytes(vector), purpose=vector['purpose'], pins=expired,
                           expected=vector['expected'], now_ms=vector['now_ms'], historical=True)


def test_validate_auto_v3_pins_enforces_v3_only_purposes():
    vector = VECTORS[0]
    assert validate_auto_v3_pins(vector['pins'], purpose=vector['purpose'], now_ms=vector['now_ms'])
    with pytest.raises(SignatureError):
        validate_auto_v3_pins(vector['pins'], purpose='operation', now_ms=vector['now_ms'])
    bad_kid = [dict(vector['pins'][0], kid='not-hex')]
    with pytest.raises(SignatureError):
        validate_auto_v3_pins(bad_kid, purpose=vector['purpose'], now_ms=vector['now_ms'])
    mixed = [dict(vector['pins'][0], purposes=['mcs-auto-offer-v3', 'operation'])]
    with pytest.raises(SignatureError):
        validate_auto_v3_pins(mixed, purpose=vector['purpose'], now_ms=vector['now_ms'])
