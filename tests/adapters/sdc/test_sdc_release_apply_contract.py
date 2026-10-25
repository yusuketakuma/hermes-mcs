"""完全合成R5b入力で閉じた束縛・署名・報告遷移を検証する。"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from adapters.sdc import release_apply_contract as a
from adapters.sdc import release_contract as r
from mcs.ops import sdc_support_signatures as sig

ROOT = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts'
V = json.loads((ROOT / 'releases/apply-vectors.json').read_text())
R = json.loads((ROOT / 'releases/release-vectors.json').read_text())
P = {x['name']: x['value'] for x in R['positive']}
# Central vectors model approval/admission/report in order through +20 seconds.
NOW = V['now'] + 20000
VALIDATORS = {k: getattr(a, 'validate_apply_' + k) for k in ('intent', 'approval', 'preview', 'operation', 'grant', 'acceptance', 'admission', 'result')}
VALIDATORS['localReceipt'] = a.validate_apply_local_receipt


def chain(**changes):
    args = {k: copy.deepcopy(V[k]) for k in ('operation', 'approval', 'preview', 'acceptance', 'registration', 'expected', 'grant', 'admission')}
    args.update(local_receipt=copy.deepcopy(V['localReceipt']), now_ms=NOW)
    args.update(changes)
    return a.validate_apply_authorization(**args)


def digest(v):
    return hashlib.sha256(json.dumps(v, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()


@pytest.mark.parametrize('kind', VALIDATORS)
def test_all_records_and_missing_extra_fields(kind):
    v, validator = V[kind], VALIDATORS[kind]
    assert validator(v, now_ms=NOW) == v
    for key in v:
        changed = copy.deepcopy(v)
        del changed[key]
        with pytest.raises(ValueError):
            validator(changed, now_ms=NOW)
    with pytest.raises(ValueError) as exc:
        validator({**v, 'secret': 'SYNTHETIC_CANARY'}, now_ms=NOW)
    assert 'SYNTHETIC_CANARY' not in str(exc.value)


@pytest.mark.parametrize('kind', V['controls'])
def test_all_control_records(kind):
    v = V['controls'][kind]
    assert a.validate_apply_control(kind, v, now_ms=NOW) == v
    for k in v:
        changed = {x: y for x, y in v.items() if x != k}
        with pytest.raises(ValueError):
            a.validate_apply_control(kind, changed, now_ms=NOW)
    with pytest.raises(ValueError):
        a.validate_apply_control(kind, {**v, 'extra': True}, now_ms=NOW)


@pytest.mark.parametrize('kind', V['admin'])
def test_all_admin_records(kind):
    assert a.validate_apply_admin(kind, V['admin'][kind]) == V['admin'][kind]
    with pytest.raises(ValueError):
        a.validate_apply_admin(kind, {**V['admin'][kind], 'extra': True})


def test_hashes_match_fixed_dag_and_independent_oracle():
    assert a.hash_apply_intent(V['intent']) == digest(V['intent']) == V['operation']['intent_hash']
    for field, record, fn in (('approval_hash', 'approval', a.hash_apply_approval), ('payload_hash', 'operation', a.hash_apply_payload), ('receipt_hash', 'localReceipt', a.hash_apply_local_receipt)):
        projected = {k: v for k, v in V[record].items() if k != field}
        assert fn(V[record]) == digest(projected) == V[record][field]
    projection = dict(schema_version=a.VERSIONS['binding'], intent_hash=V['operation']['intent_hash'], approval_hash=V['approval']['approval_hash'], change_consent_id=V['preview']['change_consent_id'], claim_id=V['preview']['claim_id'], key_id=V['preview']['key_id'])
    assert a.hash_apply_change_binding(V['preview']) == a.hash_apply_change_binding(V['operation']) == digest(projection) == V['preview']['change_binding_hash']
    assert a.hash_apply_preview(V['preview']) == digest(V['preview']) == V['operation']['preview_payload_hash']
    assert a.hash_apply_state(P['State']) == V['grant']['state_sha256']
    assert a.validate_apply_policy(V['policy']) == V['policy']
    assert chain()['grant'] == V['grant']
    assert chain(grant=None, admission=None)['grant'] is None


@pytest.mark.parametrize('field', a.EXPECTED_FIELDS)
def test_independent_binding_cannot_come_from_the_envelope(field):
    missing = {k: v for k, v in V['expected'].items() if k != field}
    for expected in (missing, {**V['expected'], field: 'different'}):
        with pytest.raises(ValueError):
            chain(expected=expected)


def test_grant_time_witness_and_changed_preflight():
    args = dict(plan=P['Plan'], attestation=P['Attestation'], trust=P['Trust root version 1'], preconditions=V['preconditions'], state=P['State'], now_ms=NOW)
    assert a.validate_apply_grant_witness(**args) == {k: V['grant'][k] for k in ('preflight_sha256', 'state_sha256')}
    for field in r.PRECONDITIONS:
        if field in ('free_bytes', 'observed_at'):
            continue
        changed = {**V['preconditions'], field: None}
        with pytest.raises(ValueError):
            a.validate_apply_grant_witness(**{**args, 'preconditions': changed})
    with pytest.raises(ValueError):
        a.validate_apply_grant_witness(**{**args, 'preconditions': {**V['preconditions'], 'free_bytes': 0}})


@pytest.mark.parametrize('purpose,kind', tuple(zip(a.PURPOSES, ('preview', 'operation', 'grant'))))
def test_typed_signatures_and_legacy_cross_purpose_rejection(purpose, kind):
    raw = json.dumps(V['envelopes'][kind]).encode('ascii')
    assert sig.verify_apply_envelope(raw, purpose=purpose, pins=V['pins'], expected=V['expected'], now_ms=NOW) == V[kind]
    assert sig.validate_apply_pins(V['pins'], purpose=purpose, now_ms=NOW) == V['pins']
    for old_purpose in (*sig.PURPOSES, *sig.RESTART_PURPOSES, 'release-root', 'release-attestation'):
        with pytest.raises(ValueError):
            sig.verify_apply_envelope(raw, purpose=old_purpose, pins=V['pins'], expected=V['expected'], now_ms=NOW)
    for verifier, kwargs in ((sig.verify_envelope, {}), (sig.verify_restart_envelope, {'expected': V['expected']})):
        with pytest.raises(ValueError):
            verifier(raw, purpose=purpose, pins=V['pins'], now_ms=NOW, **kwargs)
    for changed in ({**V['envelopes'][kind], 'purpose': 'operation'}, {**V['envelopes'][kind], 'signature': 'A' * 512}):
        with pytest.raises(ValueError):
            sig.verify_apply_envelope(json.dumps(changed).encode('ascii'), purpose=purpose, pins=V['pins'], expected=V['expected'], now_ms=NOW)
    for key in a.EXPECTED_FIELDS:
        with pytest.raises(ValueError):
            sig.verify_apply_envelope(raw, purpose=purpose, pins=V['pins'], expected={**V['expected'], key: 'wrong'}, now_ms=NOW)
    with pytest.raises(ValueError):
        sig.verify_apply_envelope(raw, purpose=purpose, pins=V['pins'], expected=V['expected'], now_ms=V[kind]['expires_at'])
    assert sig.verify_apply_envelope(raw, purpose=purpose, pins=V['pins'], expected=V['expected'], now_ms=V['pins'][0]['expires_at'], allow_expired=True) == V[kind]


def test_maximum_vectors_remain_bounded_and_typed():
    for kind in ('intent', 'approval', 'acceptance', 'admission', 'operation'):
        assert VALIDATORS[kind](V['max'][kind], now_ms=NOW) == V['max'][kind]
    assert a.validate_apply_control('grant', V['max']['grant_control'], now_ms=NOW) == V['max']['grant_control']
    for purpose, kind in zip(a.PURPOSES, ('preview', 'operation', 'grant')):
        e = V['max']['envelopes'][kind]
        sig.verify_apply_envelope(json.dumps(e).encode('ascii'), purpose=purpose, pins=V['pins'], expected=V['max']['expected'], now_ms=NOW)


def test_result_binding_admission_and_sticky_known_facts():
    report = V['controls']['report']
    args = dict(operation=V['operation'], grant=V['grant'], admission=V['admission'], now_ms=NOW)
    assert a.validate_apply_outcome(report, **args) == report
    with pytest.raises(ValueError):
        a.validate_apply_outcome(report, **{**args, 'admission': {**V['admission'], 'operation_id': V['grant']['grant_id']}})
    # Terminal success is downgraded unless independently registered verified-result capability exists.
    first = a.decide_apply_report(report=report, report_hash=digest(report), stored={}, verified_result_supported=False)
    assert first['adopted'] == 'unknown' and not first['releaseSlot']
    stored = dict(latest=dict(sequence=report['sequence'], hash=digest(report), adopted='unknown'), facts=first['facts'])
    assert a.decide_apply_report(report=report, report_hash=digest(report), stored=stored, verified_result_supported=False)['disposition'] == 'duplicate'
    with pytest.raises(a.ContractError) as exc:
        a.decide_apply_report(report=report, report_hash='f' * 64, stored=stored, verified_result_supported=False)
    assert exc.value.hold
    running = {**report, 'sequence': report['sequence'] + 1, 'state': 'running', 'result': None}
    with pytest.raises(a.ContractError) as exc:
        a.decide_apply_report(report=running, report_hash=digest(running), stored=stored, verified_result_supported=False)
    assert exc.value.hold
    nofacts = {**V['result'], **dict.fromkeys(a.FACTS), 'error_code': 'PRECHECK_FAILED', 'readiness': 'not_ready'}
    failed = {**report, 'sequence': report['sequence'] + 1, 'state': 'failed', 'result': nofacts}
    a.validate_apply_control('report', failed, now_ms=NOW)
    kept = a.decide_apply_report(report=failed, report_hash=digest(failed), stored=stored, verified_result_supported=False)
    assert not kept['releaseSlot'] and kept['facts'] == first['facts']
    freed = a.decide_apply_report(report=failed, report_hash=digest(failed), stored={}, verified_result_supported=False)
    assert freed['releaseSlot'] and freed['releasedPrestart']
    with pytest.raises(a.ContractError) as exc:
        a.decide_apply_report(report=report, report_hash=digest(report), stored=dict(released_prestart=True), verified_result_supported=False)
    assert exc.value.hold


def test_time_types_bools_unknown_fields_and_synthetic_only_sources():
    for kind, fn in VALIDATORS.items():
        for clock in (None, True, -1, 2**53, 1.5):
            with pytest.raises(ValueError):
                fn(V[kind], now_ms=clock)
    for field in ('policy_epoch', 'device_epoch', 'case_epoch', 'local_consent_epoch', 'root_version', 'metadata_epoch', 'target_code_seq'):
        with pytest.raises(ValueError):
            a.validate_apply_operation({**V['operation'], field: True}, now_ms=NOW)
    source = json.loads((ROOT / 'sources.json').read_text())
    for name in ('release.schema.json', 'release-vectors.json', 'signature-vectors.json', 'apply.schema.json', 'apply-api.schema.json', 'apply-vectors.json'):
        raw = (ROOT / 'releases' / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == source['files']['releases/' + name]
        assert b'PRIVATE KEY' not in raw and b'"privateKey"' not in raw


@pytest.mark.parametrize('literal,kind,field,expected', [('1.0000000000000001', 'intent', 'root_version', 1), ('1e-999999999999', 'preconditions', 'free_bytes', 0)])
def test_unsigned_raw_json_matches_js_binary64_rounding(literal, kind, field, expected):
    raw = json.dumps(V[kind], separators=(',', ':'))
    raw = raw.replace('"' + field + '":' + str(V[kind][field]), '"' + field + '":' + literal)
    parsed = a.validate_apply_intent(raw, check_time=False) if kind == 'intent' else a.validate_apply_preconditions(raw)
    assert parsed[field] == expected and type(parsed[field]) is int


@pytest.mark.parametrize('literal', ['1e999999999999', '-1e999999999999', '-1e-999999999999', '9007199254740992.0'])
def test_unsigned_nonfinite_negative_zero_unsafe_numbers_are_fixed_rejections(literal):
    raw = json.dumps(V['intent'], separators=(',', ':')).replace('"root_version":1', '"root_version":' + literal)
    with pytest.raises(a.ContractError):
        a.validate_apply_intent(raw, check_time=False)
