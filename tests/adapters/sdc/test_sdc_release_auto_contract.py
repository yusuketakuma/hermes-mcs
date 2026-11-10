"""v3 contract vectors: CSDC側JS実装と同一fixtureでparse/canonical/hashのparityを確認する。"""
import hashlib
import json
from pathlib import Path

import pytest

from adapters.sdc import release_auto_contract as contract
from adapters.sdc.support_contract import canonical

EXAMPLES = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/mcs-auto/v3/examples'
FIXTURES = [json.loads(p.read_text()) for p in sorted(EXAMPLES.glob('*.json')) if p.name != 'negative-vectors.json']
NEGATIVES = json.loads((EXAMPLES / 'negative-vectors.json').read_text())['vectors']

# context照合(SEM02–11,14–17,19–23,27–34)は機能module実装時に個別vectorで固定する。
# ReleaseAcceptanceの4件はSEM04–06の署名・実測・承認証拠との照合が必要で、publication側で固定する。
SEMANTIC_COVERED = {'Offer', 'StartGrant', 'Outcome', 'FileIdentity', 'MaintenanceWindow',
                    'StandingConsent', 'ConsentPreview', 'EnrollmentProposalRequest'}
PENDING = {'duplicate_lane', 'full_run_wrong_sha', 'reused_without_review', 'unresolved_failure'}


@pytest.mark.parametrize('fx', FIXTURES, ids=[f['definition'] for f in FIXTURES])
def test_positive_fixtures_validate_and_canonical_roundtrip(fx):
    raw = json.dumps(fx['data']).encode('utf-8')
    parsed = contract.parse_typed(raw, fx['definition'])
    assert contract.canonical_typed(parsed, fx['definition']) == canonical(fx['data'], max_bytes=contract.AUTO_INPUT_BYTES).encode('ascii')
    # canonical bytesを再parseしても同じ値に戻る
    again = contract.parse_typed(contract.canonical_typed(parsed, fx['definition']), fx['definition'])
    assert contract.canonical_typed(again, fx['definition']) == contract.canonical_typed(parsed, fx['definition'])


def test_negative_vectors_reject_or_stay_pending():
    pending = set()
    for v in NEGATIVES:
        covered = v['reject_layer'] == 'structure' or v['definition'] in SEMANTIC_COVERED
        if not covered:
            pending.add(v['id'])
            continue
        with pytest.raises(contract.ContractError):
            contract.parse_typed(json.dumps(v['data']).encode('utf-8'), v['definition'])
    assert pending == PENDING


def test_enc01_input_guards():
    offer = next(f for f in FIXTURES if f['definition'] == 'Offer')
    raw = json.dumps(offer['data']).encode()
    with pytest.raises(contract.ContractError):
        contract.parse_typed(b' ' * (contract.AUTO_INPUT_BYTES + 1) + raw, 'Offer')
    with pytest.raises(contract.ContractError):
        contract.parse_typed(raw, 'NoSuchDef')
    with pytest.raises(contract.ContractError):
        contract.parse_typed(b'{"schema_version":"mcs.offer.v3","schema_version":"mcs.offer.v3"}', 'Offer')
    # 整数値float(1e3→1000, 0.0→0)はJS側と同じく受理、非整数/-0/unsafeは拒否
    assert contract.parse_typed(b'{"weekday":1e0,"start_minute":0,"end_minute":60}', 'MaintenanceWindow')['weekday'] == 1
    with pytest.raises(contract.ContractError):
        contract.parse_typed(b'{"weekday":1.5,"start_minute":0,"end_minute":60}', 'MaintenanceWindow')
    with pytest.raises(contract.ContractError):
        contract.parse_typed(b'{"weekday":-0,"start_minute":0,"end_minute":60}', 'MaintenanceWindow')
    with pytest.raises(contract.ContractError):
        contract.parse_typed(b'{"weekday":9007199254740992,"start_minute":0,"end_minute":60}', 'MaintenanceWindow')
    with pytest.raises(contract.ContractError):
        contract.parse_typed(b'{"weekday":1,"weekday":2,"start_minute":0,"end_minute":60}', 'MaintenanceWindow')
    # escape同値の重複keyも拒否
    with pytest.raises(contract.ContractError):
        contract.parse_typed('{"weekday":1,"\\u0077eekday":2,"start_minute":0,"end_minute":60}'.encode(), 'MaintenanceWindow')
    with pytest.raises(contract.ContractError):
        contract.parse_typed(b'{"weekday":NaN,"start_minute":0,"end_minute":60}', 'MaintenanceWindow')
    deep = b'1'
    for _ in range(contract.AUTO_DEPTH + 2):
        deep = b'[' + deep + b']'
    with pytest.raises(contract.ContractError):
        contract.parse_typed(deep, 'Offer')


def test_file_identity_inode_boundaries_and_nested_semantics():
    """R10: inode=0拒否・UInt64境界・PrivateFileWitness入れ子でのsemantic適用。"""
    fx = next(f for f in FIXTURES if f['definition'] == 'FileIdentity')['data']

    def parse(inode, wrap=False):
        identity = dict(fx, inode=inode)
        doc = ({'logical_id': 'release-owner.json', 'identity': identity, 'sha256': 'a' * 64}
               if wrap else identity)
        return contract.parse_typed(json.dumps(doc).encode(), 'PrivateFileWitness' if wrap else 'FileIdentity')

    parse('1')
    parse('18446744073709551615')  # 2^64-1
    parse('18446744073709551614')  # positive fixture値
    for bad in ('0', '18446744073709551616', '00', '-1'):
        with pytest.raises(contract.ContractError):
            parse(bad)
        with pytest.raises(contract.ContractError):
            parse(bad, wrap=True)
    parse('1', wrap=True)
    parse('18446744073709551615', wrap=True)


def test_hash_v3_domain_separated():
    value = {'b': [1, 'x'], 'a': None}
    expected = hashlib.sha256(('MCS-AUTO-v3\nprobe\n' + canonical(value, max_bytes=contract.AUTO_INPUT_BYTES)).encode('ascii')).hexdigest()
    assert contract.hash_v3('probe', value) == expected
    with pytest.raises(contract.ContractError):
        contract.hash_v3('Bad Kind', value)


def test_contract_error_carries_code():
    with pytest.raises(contract.ContractError) as ei:
        contract.parse_typed(b'{}', 'Offer')
    assert ei.value.code == 'CONTRACT_INVALID'
