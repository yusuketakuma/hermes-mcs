"""公開された完全合成R5a vectorと独立hashで契約・署名を検証する。"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from adapters.sdc import release_contract as r
from mcs.ops import sdc_support_signatures as sig

ROOT = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/releases'
V = json.loads((ROOT / 'release-vectors.json').read_text())
G = json.loads((ROOT / 'signature-vectors.json').read_text())
NOW = V['now']
P = {x['name']: x['value'] for x in V['positive']}


def digest(prefix, value):
    return hashlib.sha256((prefix + json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)).encode('ascii')).hexdigest()


def validate(row):
    value = row['value']
    if row['definition'] == 'Trust':
        return r.validate_release_trust(value, now_ms=None if row.get('validate_without_now') else NOW)
    if row['definition'] == 'Attestation':
        return r.validate_release_attestation(value, trust=P['Trust root version 1'], now_ms=NOW)
    if row['definition'] == 'State':
        return r.validate_release_state(value)
    return r.validate_release_plan(value, trust=P['Trust root version 1'], attestation=P['Attestation'], state=row.get('state', P['State']), now_ms=NOW)


@pytest.mark.parametrize('row', V['positive'], ids=lambda x: x['name'])
def test_positive_contracts(row):
    actual = validate(row)
    assert actual == row['value'] and actual is not row['value']


@pytest.mark.parametrize('row', V['negative'], ids=lambda x: x['name'])
def test_negative_contracts(row):
    with pytest.raises(ValueError):
        validate(row)


def test_identity_domains_and_closed_input():
    pairs = ((r.hash_release_trust, 'TRUST'), (r.hash_release_core, 'CORE'), (r.hash_release_manifest, 'MANIFEST'), (r.hash_release_plan, 'PLAN'), (r.hash_release_preconditions, 'PRECONDITION'))
    for fn, name in pairs:
        assert fn(P['Attestation']['core']) == digest('SDC-RELEASE-' + name + '-v1\n', P['Attestation']['core'])
    assert r.hash_release_core(P['Attestation']['core']) == P['Attestation']['release_id']
    assert r.hash_release_plan(P['Plan']) == 'c78e9580781424cf1c1d7b893cc59f0287c0a92e5b20ae54dfe6c31bd9c017ae'
    raw = json.dumps(P['Trust root version 1'])
    for bad in ('\ufeff' + raw, raw.replace('"root_version": 1', '"root_version": 1.0'), raw.replace('"root_version": 1', '"root_version": true'), raw.replace('"root_version": 1', '"root_version": 1, "root_version": 1'), ' ' * 8193, b'\xff', '{"a":-0}', '{"a":NaN}'):
        with pytest.raises(ValueError):
            r.validate_release_trust(bad, now_ms=NOW)
    for row in V['positive']:
        changed = {**row['value'], 'secret': 'SYNTHETIC_CANARY'}
        with pytest.raises(ValueError) as exc:
            validate({**row, 'value': changed})
        assert 'SYNTHETIC_CANARY' not in str(exc.value)


@pytest.mark.parametrize('row', V['observations'], ids=lambda x: x['name'])
def test_observation(row):
    state = {**P['State'], **row.get('state_override', {})}
    args = dict(trust=P['Trust root version 1'], now_ms=NOW)
    if row['existing_binding'] != 'absent':
        args['existing_binding'] = row['existing_binding']
    if row['expect']['status'] != 'ok':
        with pytest.raises((ValueError, TypeError)):
            r.observe_release(state, P['Attestation'], **args)
    else:
        result = r.observe_release(state, P['Attestation'], **args)
        assert result['state']['highest_seen'] == row['expect']['highest_seen']
        assert result['binding_new'] == row['expect']['binding_new']
        assert result['state']['highest_installed'] == state['highest_installed']


def test_modes_and_all_blockers_hold_without_side_effects():
    state, plan, trust, attestation = (copy.deepcopy(P[n]) for n in ('State', 'Plan', 'Trust root version 1', 'Attestation'))
    assert r.release_plan_blockers(plan, attestation, trust, state, now_ms=NOW) == []
    for seen, frontier, expected in ((42, 41, []), (41, 50, ['code_rollback']), (50, 41, ['select_below_seen']), (50, 50, list(r.MODES))):
        state['highest_seen']['code_seq'], state['highest_installed']['code_seq'] = seen, frontier
        assert r.derive_release_modes(42, state) == expected
    state['installed'] = state['highest_installed'] = state['highest_seen'] = None
    state['unresolved'] = {'phase': 'unknown'}
    plan['preconditions'].update(hand_edited=True, backup_verified=False, free_bytes=0, updater=[0, 0, 0], installed_schema=999, platform=dict(os='other', cpu='other', python=[3, 0]), observed_at=0)
    plan['window']['ends_at'] = NOW
    plan['expires_at'] = NOW
    plan['modes'] = ['select_below_seen']
    blockers = r.release_plan_blockers(plan, attestation, trust, state, now_ms=NOW)
    assert blockers == ['BASELINE_MISSING', 'TARGET_NOT_OBSERVED', 'OUTSIDE_WINDOW', 'PLAN_EXPIRED', 'PREFLIGHT_STALE', 'HAND_EDITED', 'BACKUP_UNVERIFIED', 'INSUFFICIENT_SPACE', 'UNSUPPORTED_PLATFORM', 'UPDATER_TOO_OLD', 'SCHEMA_INCOMPATIBLE', 'UNRESOLVED_OPERATION', 'EXCEPTION_APPROVAL_REQUIRED']


def invoke(row):
    name = row.get('trusted', row.get('expected_trust', 'bootstrap'))
    expected = G['maximum_scope'] if name.startswith('maximum-') else G['scope']
    now = G['maximum_now'] if name.startswith('maximum-') else G['now']
    args = dict(expected=expected, now_ms=now)
    if row['api'] == 'bootstrap':
        trust = G['trusts'][row.get('expected_trust', 'bootstrap')]
        return sig.verify_release_bootstrap(G['proofs'][row['proof']], expected_trust_hash=row.get('expected_digest', digest('SDC-RELEASE-TRUST-v1\n', trust)), **args)
    args['trusted'] = G['trusts'][row['trusted']]
    if row['api'] == 'root':
        return sig.verify_release_root(G['proofs'][row['proof']], **args)
    if row['api'] == 'chain':
        return sig.verify_release_root_chain([G['proofs'][n] for n in row['proofs']], **args)
    return sig.verify_release_attestation(G['attestations'][row['attestation']]['envelope'], trust=args.pop('trusted'), **args)


@pytest.mark.parametrize('row', G['positive'], ids=lambda x: x['name'])
def test_positive_root_proofs(row):
    assert invoke(row) == G['trusts'][row['expected_trust']]


@pytest.mark.parametrize('row', G['negative'], ids=lambda x: x['name'])
def test_negative_root_and_attestation_proofs(row):
    with pytest.raises(sig.ReleaseSignatureError) as exc:
        invoke(row)
    assert exc.value.code == row['expected_error']


def test_fingerprints_and_current_attestations():
    for k in G['keys']:
        if k['name'].startswith('invalid-'):
            with pytest.raises(sig.ReleaseSignatureError):
                sig.release_key_fingerprint(k['spki'])
        else:
            assert sig.release_key_fingerprint(k['spki']) == k['kid'] == digest('', k['public_jwk'])
    for name in ('current', 'refreshed'):
        value = G['attestations'][name]
        assert sig.verify_release_attestation(value['envelope'], trust=G['trusts']['rotated'], expected=G['scope'], now_ms=NOW) == value['value']
    for field in r.EXPECTED_FIELDS:
        with pytest.raises(sig.ReleaseSignatureError):
            sig.verify_release_bootstrap(G['proofs']['bootstrap'], expected_trust_hash=r.hash_release_trust(G['trusts']['bootstrap']), expected={**G['scope'], field: 'wrong'}, now_ms=NOW)


def test_proof_bounds_and_unavailable_crypto_never_count_as_a_vote(monkeypatch):
    args = dict(expected_trust_hash=r.hash_release_trust(G['trusts']['bootstrap']), expected=G['scope'], now_ms=NOW)
    for v in ([], G['proofs']['bootstrap'] * 4, ' ' * 98305):
        with pytest.raises(ValueError):
            sig.verify_release_bootstrap(v, **args)
    def unavailable(*args):
        raise sig.SignatureError('SDC_SUPPORT_CRYPTO_UNAVAILABLE')
    monkeypatch.setattr(sig, '_verify_pss', unavailable)
    with pytest.raises(sig.SignatureError, match='CRYPTO_UNAVAILABLE'):
        sig.verify_release_bootstrap(G['proofs']['bootstrap'], **args)


@pytest.mark.parametrize('row', V['modes'], ids=lambda x: x['name'])
def test_all_fixed_mode_vectors(row):
    state = {**P['State'], **row.get('state_override', {})}
    assert r.derive_release_modes(row['target'], state) == row['expect']


def test_proof_bytes_are_bounded_utf8_and_no_bom():
    args = dict(expected_trust_hash=r.hash_release_trust(G['trusts']['bootstrap']), expected=G['scope'], now_ms=NOW)
    raw = json.dumps(G['proofs']['bootstrap']).encode('utf-8')
    assert sig.verify_release_bootstrap(raw, **args) == G['trusts']['bootstrap']
    for bad in (b'\xef\xbb\xbf' + raw, b'\xff', '\U0001f600' * 30000):
        with pytest.raises(sig.ReleaseSignatureError):
            sig.verify_release_bootstrap(bad, **args)


@pytest.mark.parametrize('api', ['root', 'attestation'])
def test_nonfinite_trust_number_is_a_fixed_signature_error(api):
    raw = json.dumps(G['trusts']['bootstrap'], separators=(',', ':')).replace('"root_version":1', '"root_version":1e999999999999')
    with pytest.raises(sig.ReleaseSignatureError):
        if api == 'root':
            sig.verify_release_root(G['proofs']['rotated'], trusted=raw, expected=G['scope'], now_ms=NOW)
        else:
            sig.verify_release_attestation(G['attestations']['current']['envelope'], trust=raw, expected=G['scope'], now_ms=NOW)


@pytest.mark.parametrize('literal', ['1.0', '1e0', '1.0000000000000001'])
def test_r5a_raw_fraction_remains_rejected(literal):
    raw = json.dumps(G['trusts']['bootstrap'], separators=(',', ':')).replace('"root_version":1', '"root_version":' + literal)
    with pytest.raises(r.ContractError):
        r.validate_release_trust(raw, now_ms=NOW)
