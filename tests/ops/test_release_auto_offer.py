"""v3 offer_eligible deps面: offer.selected適格判定の検証。"""
import json
from pathlib import Path

from release_auto_offer import OfferEligible

EXAMPLES = Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/mcs-auto/v3/examples'


def _example(name):
    return json.loads((EXAMPLES / name).read_text())['data']


OFFER = _example('offer.json')
PROFILE = _example('standing-consent.json')['profile']
OWNER = {'profile': dict(PROFILE)}


def test_offer_eligible_accepts_valid_offer():
    assert OfferEligible()(OFFER, OWNER) is True


def test_offer_eligible_rejects_profile_mismatch():
    offer = json.loads(json.dumps(OFFER))
    offer['profile_sha256'] = '0' * 64
    assert OfferEligible()(offer, OWNER) is False

    owner = {'profile': dict(PROFILE, profile_sha256='0' * 64)}
    assert OfferEligible()(OFFER, owner) is False


def test_offer_eligible_rejects_epoch_and_stage_mismatch():
    offer = json.loads(json.dumps(OFFER))
    offer['rollout_epoch'] = 99
    assert OfferEligible()(offer, OWNER) is False

    offer = json.loads(json.dumps(OFFER))
    offer['selected']['rollout']['stage'] = 'bogus'
    assert OfferEligible()(offer, OWNER) is False


def test_offer_eligible_rejects_malformed_selected():
    for key in ('manifest_digest', 'compatibility_sha256',
                'publication_proof_sha256', 'acceptance_sha256'):
        offer = json.loads(json.dumps(OFFER))
        offer['selected'][key] = 'not-hex'
        assert OfferEligible()(offer, OWNER) is False, key

    offer = json.loads(json.dumps(OFFER))
    offer['selected']['publication_id'] = 'not-a-uuid'
    assert OfferEligible()(offer, OWNER) is False

    offer = json.loads(json.dumps(OFFER))
    offer['selected']['rollout'] = None
    assert OfferEligible()(offer, OWNER) is False


def test_offer_eligible_fail_closed_on_garbage():
    eligible = OfferEligible()
    for offer, owner in ((None, OWNER), (OFFER, None), ('x', OWNER),
                         ({}, {}), (OFFER, {'profile': 'x'})):
        assert eligible(offer, owner) is False
