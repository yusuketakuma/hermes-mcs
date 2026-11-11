"""v3 local_revoke deps面: owner state consent記録からの撤回判定を検証する。"""
import json
from pathlib import Path

import pytest

from adapters.sdc import release_auto_state as state
from adapters.sdc.storage import Store
from release_auto_revoke import LocalRevoke

EXAMPLES = Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/mcs-auto/v3/examples'


def _example(name):
    return json.loads((EXAMPLES / name).read_text())['data']


SCOPE = _example('offer.json')['scope']
PROFILE = _example('standing-consent.json')['profile']
CONSENT = _example('standing-consent.json')
CURRENT = _example('prepared-public.json')['current_release']


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
        'consent': dict(CONSENT),
        'highest_seen': dict(CURRENT), 'installed': dict(CURRENT),
        'highest_installed': dict(CURRENT), 'verified_installed': dict(CURRENT),
        'runtime_generation': 1,
        'bindings': [{'code_seq': 1, 'release_id': CURRENT['release_id']}],
        'active': None, 'spent': [], 'outcomes': [], 'outbox': [],
        'prepare_budget': [],
    }
    value.update(over)
    return value


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / 'private-auto', create=True) as value:
        yield value


def test_local_revoke_live_consent_is_false(store):
    store.write(state.OWNER_NAME, owner_value(store))
    assert LocalRevoke(store)() is False


def test_local_revoke_disabled_or_missing_consent_is_true(store):
    disabled = dict(CONSENT, enabled=False)
    store.write(state.OWNER_NAME, owner_value(store, consent=disabled))
    assert LocalRevoke(store)() is True

    store.write(state.OWNER_NAME, owner_value(store, consent=None))
    assert LocalRevoke(store)() is True


def test_local_revoke_reads_latest_state_each_call(store):
    store.write(state.OWNER_NAME, owner_value(store))
    revoke = LocalRevoke(store)
    assert revoke() is False
    store.write(state.OWNER_NAME,
                owner_value(store, consent=dict(CONSENT, enabled=False)))
    assert revoke() is True


def test_local_revoke_unreadable_state_is_true(store, tmp_path):
    # owner file未作成 → load失敗 → 撤回/不明扱い
    assert LocalRevoke(store)() is True
    # 破損JSONも同様
    path = store.root / state.OWNER_NAME
    path.write_bytes(b'not-json')
    assert LocalRevoke(store)() is True
