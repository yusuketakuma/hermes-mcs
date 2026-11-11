"""v3 standing consent(preview/confirm/auto-disable)を合成Store+fake wireで検証する。

実network/署名には触れない。wire.signed_send/verifyはfake — verify済みpayloadを
直接返す。owner stateは実Store+schema検証を通す。
"""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from adapters.sdc import release_auto_contract as contract
from adapters.sdc import release_auto_state as state
from adapters.sdc.storage import Store
import release_auto_consent as consent_mod

EXAMPLES = Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/mcs-auto/v3/examples'


def _example(name):
    return json.loads((EXAMPLES / name).read_text())['data']


SCOPE = _example('offer.json')['scope']
PROFILE = _example('standing-consent.json')['profile']
CONSENT_EX = _example('standing-consent.json')
CURRENT = _example('prepared-public.json')['current_release']

RECEIPT_ID = 'a1b2c3d4-5678-4abc-8def-0123456789ab'
NOW_MS = int(datetime(2026, 11, 9, 2, 0,
                      tzinfo=ZoneInfo('Asia/Tokyo')).timestamp() * 1000)


def _preview(**over):
    preview = {
        'schema_version': 'mcs.consent-preview.v3',
        'request_id': '11111111-2222-4333-8444-555555555555',
        'request_nonce': 'a' * 64,
        'preview_id': '99999999-8888-4777-8666-555555555555',
        'central_proposal_id': '77777777-6666-4555-8444-333333333333',
        'scope': dict(SCOPE),
        'profile': dict(PROFILE),
        'central_policy_id': CONSENT_EX['central_policy_id'],
        'central_policy_sha256': CONSENT_EX['central_policy_sha256'],
        'policy_epoch': 1,
        'local_consent_epoch': 1,
        'timezone': 'Asia/Tokyo',
        'windows': [{'weekday': 0, 'start_minute': 60, 'end_minute': 180}],
        'max_prepare_seconds': 120,
        'max_prepare_attempts_24h': 3,
        'max_prepare_seconds_24h': 360,
        'early_adopter': False,
        'allowed_class': 'code-only-compatible',
        'consent_expires_at': NOW_MS + 30 * 86_400_000,
        'issued_at': NOW_MS - 60_000,
        'expires_at': NOW_MS + 240_000,
        'rollout_policy_sha256': CONSENT_EX['rollout_policy_sha256'],
    }
    preview.update(over)
    return preview


def _receipt_for(request):
    consent = request['consent']
    return {
        'schema_version': 'mcs.consent-receipt.v3',
        'request_id': request['request_id'],
        'request_nonce': request['request_nonce'],
        'scope': consent['scope'],
        'consent_id': consent['consent_id'],
        'consent_sha256': contract.hash_v3('standing-consent', consent),
        'local_consent_epoch': consent['local_consent_epoch'],
        'policy_epoch': consent['policy_epoch'],
        'status': 'active',
        'accepted_at': NOW_MS,
        'issued_at': NOW_MS,
        'expires_at': NOW_MS + 300_000,
    }


class FakeWire:
    """signed_sendがrequestを記録し、verifyがprogrammed payloadを返すfake。"""

    def __init__(self, payloads=None, fail=False):
        self.sent = []
        self.payloads = list(payloads or [])
        self.fail = fail

    def signed_send(self, path, raw):
        if self.fail:
            raise RuntimeError('send failed')
        self.sent.append((path, json.loads(raw.decode())))
        return b'{}'

    def verify(self, path, response):
        return self.payloads.pop(0)


class Clock:
    def __init__(self, now_ms=NOW_MS):
        self._now = now_ms

    def wall_ms(self):
        return self._now


def owner_value(store, **over):
    value = {
        'schema_version': 'mcs.release-owner.v3',
        'scope': dict(SCOPE),
        'root_fingerprint': store.root_fingerprint(),
        'repository_fingerprint': 'd' * 64, 'activation_sha256': 'e' * 64,
        'control_closure_sha256': PROFILE['control_closure_sha256'],
        'revision': 1, 'execution_revision': 1,
        'trust_json': '{}', 'legacy_evidence': [], 'operation_evidence': [],
        'lock_fingerprints': {k: '0' * 64 for k in 'GUQR CAMO'.replace(' ', '')},
        'installed_source_manifest_sha256': '1' * 64,
        'verified_source_manifest_sha256': '2' * 64,
        'profile': dict(PROFILE),
        'consent': None,
        'highest_seen': dict(CURRENT),
        'installed': dict(CURRENT),
        'highest_installed': dict(CURRENT),
        'verified_installed': dict(CURRENT),
        'runtime_generation': 1,
        'bindings': [{'code_seq': CURRENT['code_seq'],
                      'release_id': CURRENT['release_id']}],
        'active': None, 'spent': [], 'outcomes': [], 'outbox': [], 'prepare_budget': [],
    }
    value.update(over)
    return value


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / 'private-auto', create=True) as value:
        yield value


def seed(store, **over):
    store.write(state.OWNER_NAME, owner_value(store, **over))


def test_request_consent_preview_sends_and_returns(store):
    preview = _preview()
    wire = FakeWire(payloads=[preview])
    result = consent_mod.request_consent_preview(
        wire, scope=SCOPE, profile_sha256=PROFILE['profile_sha256'],
        central_proposal_id=preview['central_proposal_id'])
    assert result == preview
    path, request = wire.sent[0]
    assert path == '/v3/mcs/releases/consent-preview'
    assert request['schema_version'] == 'mcs.consent-preview-request.v3'
    assert request['scope'] == SCOPE
    assert request['central_proposal_id'] == preview['central_proposal_id']
    assert request['request_id'] and request['request_nonce']


def test_confirm_standing_policy_saves_consent(store):
    seed(store)
    preview = _preview()
    class EchoWire(FakeWire):
        def verify(self, path, response):
            request = self.sent[-1][1]
            return _receipt_for(request)

    wire = EchoWire()
    consent = consent_mod.confirm_standing_policy(
        store, wire, preview=preview, clock=Clock(),
        confirmation_receipt_id=RECEIPT_ID)
    owner = state.load(store)
    assert owner['consent'] == consent
    assert consent['schema_version'] == 'mcs.standing-consent.v3'
    # preview→consentの写像: policy項目は完全一致、id系はpreview_id由来で決定論的
    assert consent['scope'] == preview['scope']
    assert consent['profile'] == preview['profile']
    assert consent['max_prepare_count_24h'] == preview['max_prepare_attempts_24h']
    assert consent['local_consent_epoch'] == preview['local_consent_epoch']
    assert consent['central_policy_sha256'] == preview['central_policy_sha256']
    assert consent['expires_at'] == preview['consent_expires_at']
    assert consent['enabled'] is True
    assert consent['confirmation_receipt_id'] == RECEIPT_ID
    path, request = wire.sent[0]
    assert path == '/v3/mcs/releases/consent-confirm'
    assert request['preview_id'] == preview['preview_id']
    assert request['preview_sha256']
    assert request['consent'] == consent


def test_confirm_rejects_expired_preview(store):
    seed(store)
    preview = _preview(expires_at=NOW_MS - 1)
    wire = FakeWire()
    with pytest.raises(consent_mod.ConsentError):
        consent_mod.confirm_standing_policy(
            store, wire, preview=preview, clock=Clock(),
            confirmation_receipt_id=RECEIPT_ID)
    assert state.load(store)['consent'] is None
    assert wire.sent == []


def test_confirm_rejects_revoked_receipt(store):
    seed(store)
    preview = _preview()

    class RevokeWire(FakeWire):
        def verify(self, path, response):
            receipt = _receipt_for(self.sent[-1][1])
            receipt['status'] = 'revoked'
            return receipt

    with pytest.raises(consent_mod.ConsentError):
        consent_mod.confirm_standing_policy(
            store, RevokeWire(), preview=preview, clock=Clock(),
            confirmation_receipt_id=RECEIPT_ID)
    assert state.load(store)['consent'] is None


def test_confirm_rejects_receipt_hash_mismatch(store):
    seed(store)
    preview = _preview()

    class BadWire(FakeWire):
        def verify(self, path, response):
            receipt = _receipt_for(self.sent[-1][1])
            receipt['consent_sha256'] = '0' * 64
            return receipt

    with pytest.raises(consent_mod.ConsentError):
        consent_mod.confirm_standing_policy(
            store, BadWire(), preview=preview, clock=Clock(),
            confirmation_receipt_id=RECEIPT_ID)
    assert state.load(store)['consent'] is None


def test_disable_auto_local_only(store):
    consent = dict(CONSENT_EX)
    consent['confirmed_at'] = NOW_MS - 1000
    consent['expires_at'] = NOW_MS + 30 * 86_400_000
    seed(store, consent=consent)
    before = state.load(store)
    result = consent_mod.disable_auto(store)
    assert result['action'] == 'disabled'
    assert result['local_consent_epoch'] == consent['local_consent_epoch'] + 1
    assert result['central_notified'] is False
    owner = state.load(store)
    saved = owner['consent']
    assert saved['enabled'] is False
    assert saved['local_consent_epoch'] == consent['local_consent_epoch'] + 1
    # safety項目変更なのでexecution_revisionが進む
    assert owner['execution_revision'] == before['execution_revision'] + 1


def test_disable_auto_notifies_central_and_is_idempotent(store):
    consent = dict(CONSENT_EX)
    consent['confirmed_at'] = NOW_MS - 1000
    consent['expires_at'] = NOW_MS + 30 * 86_400_000
    seed(store, consent=consent)

    class RevokeWire(FakeWire):
        def verify(self, path, response):
            request = self.sent[-1][1]
            return {
                'schema_version': 'mcs.consent-receipt.v3',
                'request_id': request['request_id'],
                'request_nonce': request['request_nonce'],
                'scope': request['scope'],
                'consent_id': request['consent_id'],
                'consent_sha256': '1' * 64,
                'local_consent_epoch': request['new_local_consent_epoch'],
                'policy_epoch': consent['policy_epoch'],
                'status': 'revoked',
                'accepted_at': NOW_MS, 'issued_at': NOW_MS,
                'expires_at': NOW_MS + 300_000,
            }

    wire = RevokeWire()
    result = consent_mod.disable_auto(store, wire)
    assert result['central_notified'] is True
    path, request = wire.sent[0]
    assert path == '/v3/mcs/releases/consent-revoke'
    assert request['new_local_consent_epoch'] == consent['local_consent_epoch'] + 1
    assert request['previous_local_consent_epoch'] == consent['local_consent_epoch']

    # 2回目: localは既にdisabledなので書換えず、同じ冪等requestで中央へ再送
    wire2 = RevokeWire()
    result2 = consent_mod.disable_auto(store, wire2)
    assert result2['action'] == 'disabled'
    assert result2['local_consent_epoch'] == result['local_consent_epoch']
    _, request2 = wire2.sent[0]
    assert request2 == request


def test_disable_auto_no_consent_is_noop(store):
    seed(store, consent=None)
    result = consent_mod.disable_auto(store)
    assert result == {'action': 'noop', 'reason': 'no_consent'}


def test_disable_auto_wire_failure_keeps_local_revoked(store):
    consent = dict(CONSENT_EX)
    consent['confirmed_at'] = NOW_MS - 1000
    consent['expires_at'] = NOW_MS + 30 * 86_400_000
    seed(store, consent=consent)
    with pytest.raises(RuntimeError):
        consent_mod.disable_auto(store, FakeWire(fail=True))
    owner = state.load(store)
    assert owner['consent']['enabled'] is False
    assert owner['consent']['local_consent_epoch'] == consent['local_consent_epoch'] + 1
