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

# context照合(SEM02–03,07–11,14–17,19–23,27–34)は機能module実装時に個別vectorで固定する。
# ReleaseAcceptanceのSEM04–06はvalidate_acceptanceが担うためnegative vectorもここで固定する。
SEMANTIC_COVERED = {'Offer', 'StartGrant', 'Outcome', 'FileIdentity', 'MaintenanceWindow',
                    'RolloutPolicy', 'StandingConsent', 'ConsentPreview', 'EnrollmentProposalRequest'}
PENDING = set()


def _acceptance_policy():
    return json.loads((EXAMPLES / 'rollout-policy.json').read_text())['data']


def _bind_acceptance(data):
    """fixtureのpolicy_sha256をtest policyへ付け替え、lane semanticsを単独で試験可能にする。"""
    bound = json.loads(json.dumps(data))
    bound['policy_sha256'] = contract.hash_v3('rollout-policy', _acceptance_policy())
    return bound


def _acceptance(data):
    return contract.parse_typed(json.dumps(data).encode(), 'ReleaseAcceptance')


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
    policy = _acceptance_policy()
    for v in NEGATIVES:
        if v['definition'] == 'ReleaseAcceptance':
            bound = _bind_acceptance(v['data'])
            with pytest.raises(contract.ContractError):
                contract.validate_acceptance(_acceptance(bound),
                                             expected_source=bound['source'], policy=policy)
            continue
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
    for bad in ('0', '18446744073709551616', '00', '-1', '0\n', '1\n', ' 1', '1 ', '1\t'):
        with pytest.raises(contract.ContractError):
            parse(bad)
        with pytest.raises(contract.ContractError):
            parse(bad, wrap=True)
    # inode以外のUInt64Decimal項目(device等)も同じ型経路で末尾LFを拒否する
    with pytest.raises(contract.ContractError):
        contract.parse_typed(json.dumps(dict(fx, device='1\n')).encode(), 'FileIdentity')
    parse('1', wrap=True)
    parse('18446744073709551615', wrap=True)
    # UInt64Decimalを直接definition指定した入口(root)でも同一検査を通る（r17/r19）
    assert contract.parse_typed(b'"1"', 'UInt64Decimal') == '1'
    for bad in ('0\n', '1\n', ' 1', '1 ', '1\t', '00', '-1'):
        with pytest.raises(contract.ContractError):
            contract.parse_typed(json.dumps(bad).encode(), 'UInt64Decimal')


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


def test_rollout_policy_semantics_direct_and_nested():
    """R21: stage厳密順序/bp/health関係/時刻を直接・PolicyPreviewRequest入れ子で拒否。"""
    fx = next(f for f in FIXTURES if f['definition'] == 'RolloutPolicy')['data']

    def parse(over, wrap=False):
        policy = dict(fx, **over)
        doc = ({'schema_version': 'mcs.policy-preview-request.v3',
                'request_id': '10000000-0000-4000-8000-000000000099',
                'request_nonce': 'a' * 64, 'action': 'approve', 'policy': policy,
                'expected_policy_revision': 0} if wrap else policy)
        return contract.parse_typed(json.dumps(doc).encode(), 'PolicyPreviewRequest' if wrap else 'RolloutPolicy')

    parse({})
    parse({}, wrap=True)
    bad = [
        {'stages': list(reversed(fx['stages']))},
        {'stages': [fx['stages'][0], fx['stages'][0], *fx['stages'][2:]]},
        {'stages': fx['stages'][:4]},
        {'stages': [dict(s, cumulative_basis_points=999) if i == 2 else s for i, s in enumerate(fx['stages'])]},
        {'max_health_gap_ms': fx['health_interval_ms'] - 1, 'max_health_observation_age_ms': 200000},
        {'max_health_gap_ms': 700000, 'max_health_observation_age_ms': 800000},
        {'issued_at': fx['expires_at']},
    ]
    for over in bad:
        with pytest.raises(contract.ContractError):
            parse(over)
        with pytest.raises(contract.ContractError):
            parse(over, wrap=True)


def test_validate_acceptance_positive_fixtures():
    """SEM04–06: 公式full/composed例がexpected_source/policy束縛で受理される。"""
    policy = _acceptance_policy()
    for name in ('acceptance-full.json', 'acceptance-composed.json'):
        bound = _bind_acceptance(json.loads((EXAMPLES / name).read_text())['data'])
        contract.validate_acceptance(_acceptance(bound), expected_source=bound['source'], policy=policy)


def test_validate_acceptance_rejects_binding_and_lane_violations():
    """expected_source/policy不一致とSEM04–06の個別違反を拒否する。"""
    policy = _acceptance_policy()
    base = _bind_acceptance(json.loads((EXAMPLES / 'acceptance-composed.json').read_text())['data'])
    value = _acceptance(base)
    contract.validate_acceptance(value, expected_source=base['source'], policy=policy)

    # expected_sourceはSourceRef全体のcanonical一致を要求(中央publication.jsと同じ)
    wrong_source = dict(base['source'], commit='0' * 40)
    with pytest.raises(contract.ContractError):
        contract.validate_acceptance(value, expected_source=wrong_source, policy=policy)
    # policy_sha256はH("rollout-policy", policy)との一致を要求
    other_policy = dict(policy, policy_revision=policy['policy_revision'] + 1)
    with pytest.raises(contract.ContractError):
        contract.validate_acceptance(value, expected_source=base['source'], policy=other_policy)

    def mutated(fn):
        doc = json.loads(json.dumps(base))
        fn(doc)
        return _acceptance(doc)

    # SEM06: local_equivalentは実測successのみ(failureを免除しない)
    with pytest.raises(contract.ContractError):
        contract.validate_acceptance(
            mutated(lambda d: d['lanes'][0].update(coverage='local_equivalent', measured_result='failure',
                                                    source_commit=d['source']['commit'], reused_from_commit=None,
                                                    resolution_evidence_sha256='c' * 64)),
            expected_source=base['source'], policy=policy)
    # SEM04: executedなのに未実行理由を持つのは矛盾
    with pytest.raises(contract.ContractError):
        contract.validate_acceptance(
            mutated(lambda d: d['lanes'][1].update(unexecuted_reason='runner_down')),
            expected_source=base['source'], policy=policy)
    # SEM04: reused laneのsource_commitはreused_from_commitと一致必須
    with pytest.raises(contract.ContractError):
        contract.validate_acceptance(
            mutated(lambda d: d['lanes'][0].update(source_commit=d['source']['commit'])),
            expected_source=base['source'], policy=policy)
    # SEM05: full_runでjob_ref欠落・未executedは拒否
    full = _bind_acceptance(json.loads((EXAMPLES / 'acceptance-full.json').read_text())['data'])
    with pytest.raises(contract.ContractError):
        contract.validate_acceptance(
            _acceptance(dict(full, lanes=[dict(lane, job_ref=None) if i == 0 else lane
                              for i, lane in enumerate(full['lanes'])])),
            expected_source=full['source'], policy=policy)
