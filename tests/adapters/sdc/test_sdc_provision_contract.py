"""Exact synthetic provision oracles; no central repository, private keys or live I/O."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from adapters.sdc import provision_contract as contract
from adapters.sdc.support_contract import canonical

ROOT = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/provision'
V = json.loads((ROOT / 'provision-vectors.json').read_text())
SCHEMA = json.loads((ROOT / 'provision.schema.json').read_text())
NOW = V['now']
OTHER = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
CANARY = 'SYNTHETIC_PRIVATE_NEVER_IN_ERRORS'
SAFE = 9007199254740991
MAX_GENERATION = 2147483647
LOCAL_RUNTIME_REJECTIONS = {
    'Same kid across pins', 'Invalid public SPKI binary',
    'Expired public pin', 'Bundle import order',
}
EXTERNAL_RUNTIME_CHECKS = {'Bundle hash policy mismatch', 'ACK before issuance'}


def example(definition):
    return copy.deepcopy(next(v['value'] for v in V['positive'] if v['definition'] == definition))


def bundle(profile='monitor'):
    return copy.deepcopy(next(v['value'] for v in V['positive']
                              if v['definition'] == 'Bundle' and v['value']['profile_id'] == profile))


def raw(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('ascii')


def rejected(definition, value, *, parse=False, now_ms=NOW):
    validate = contract.parse_provision if parse else contract.validate_provision
    with pytest.raises(contract.ProvisionError, match='^SDC_PROVISION_INVALID$') as caught:
        validate(definition, value, now_ms=now_ms)
    assert CANARY not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.fixture(autouse=True)
def no_native_crypto_or_process(monkeypatch):
    import sdc_support_signatures as signatures

    def forbidden(*args, **kwargs):
        raise AssertionError('provision contract must not perform native verification or spawn')

    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(os, 'system', forbidden)
    monkeypatch.setattr(signatures, 'verify_envelope', forbidden)
    monkeypatch.setattr(signatures, 'verify_restart_envelope', forbidden)


def test_exact_local_oracle_bytes_and_all_vector_groups_are_accounted_for():
    expected = {
        'provision.schema.json': 'b2bea5be19d01a3fb3021408145e39aa9f8a4cd2d76fd7acc0d54c404f2b3649',
        'provision-vectors.json': 'd6c49ea2dc62d5780f87b8f26f3cb9106f7b36ca36fa66f88b84bcd396fd81aa',
    }
    for name, digest in expected.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
    assert [len(V[group]) for group in ('positive', 'negative', 'runtime_only')] == [30, 104, 6]
    assert {v['name'] for v in V['runtime_only']} == LOCAL_RUNTIME_REJECTIONS | EXTERNAL_RUNTIME_CHECKS
    assert LOCAL_RUNTIME_REJECTIONS.isdisjoint(EXTERNAL_RUNTIME_CHECKS)


@pytest.mark.parametrize('vector', V['positive'], ids=lambda v: v['name'])
def test_all_fixed_positive_vectors_are_detached_and_noncanonical_json_is_allowed(vector):
    before = copy.deepcopy(vector['value'])
    actual = contract.validate_provision(vector['definition'], vector['value'], now_ms=NOW)
    assert actual == before and actual is not vector['value']
    assert vector['value'] == before
    text = json.dumps(vector['value'], ensure_ascii=True, indent=1)
    assert contract.parse_provision(vector['definition'], text, now_ms=NOW) == before
    assert contract.parse_provision(vector['definition'], text.encode(), now_ms=NOW) == before
    assert SCHEMA['$defs'][vector['definition']]['additionalProperties'] is False


@pytest.mark.parametrize('vector', V['negative'], ids=lambda v: v['name'])
def test_all_fixed_negative_vectors_reject_without_mutation(vector):
    before = copy.deepcopy(vector['value'])
    rejected(vector['definition'], vector['value'])
    rejected(vector['definition'], raw(vector['value']), parse=True)
    assert vector['value'] == before


@pytest.mark.parametrize('vector', V['runtime_only'], ids=lambda v: v['name'])
def test_shape_valid_runtime_vectors_distinguish_local_checks_from_external_authority(vector):
    assert vector['required_runtime_check']
    if vector['name'] in LOCAL_RUNTIME_REJECTIONS:
        rejected(vector['definition'], vector['value'])
        rejected(vector['definition'], raw(vector['value']), parse=True)
    else:
        # Passing here proves no configured policy/issuance lookup happened. It
        # does not establish authenticity, a matching policy, or ACK eligibility.
        actual = contract.validate_provision(vector['definition'], vector['value'], now_ms=NOW)
        assert actual == vector['value']
        if vector['name'] == 'Bundle hash policy mismatch':
            assert actual['policy_hash'] != V['canonical']['policy_hash']
        else:
            assert actual['applied_at'] < bundle()['issued_at']


def definition_examples():
    values = {v['definition']: copy.deepcopy(v['value']) for v in V['positive']}
    values.update({
        'UUID': OTHER, 'Hash': 'a' * 64, 'Identifier': 'synthetic', 'Environment': 'synthetic',
        'RealmEpoch': 'synthetic:realm', 'Audience': 'sdc:synthetic', 'Origin': 'https://a.invalid:443',
        'IssuerRef': 'synthetic', 'ChannelRef': 'authenticated-console-manual-v1', 'ProfileID': 'monitor',
        'Generation': 1, 'ExpectedGeneration': 0, 'Epoch': 0, 'Timestamp': NOW,
        'CSRF': '1792663200.' + 'a' * 64,
    })
    for profile in values['Policy']['profiles']:
        name = profile['profile_id'].title()
        values[name + 'Profile'] = copy.deepcopy(profile)
        if profile['pins']:
            values[name + 'Pins'] = copy.deepcopy(profile['pins'])
            values[name + 'Pin'] = copy.deepcopy(profile['pins'][0])
    return values


DEFINITION_EXAMPLES = definition_examples()


def test_every_fixed_schema_definition_has_a_direct_success_oracle():
    assert set(DEFINITION_EXAMPLES) == set(SCHEMA['$defs'])
    assert len(DEFINITION_EXAMPLES) == 45


@pytest.mark.parametrize('definition,value', DEFINITION_EXAMPLES.items())
def test_direct_definitions_require_exact_plain_types_and_closed_object_fields(definition, value):
    assert contract.validate_provision(definition, value, now_ms=NOW) == value
    rejected(definition, None)
    if type(value) is dict:
        schema = SCHEMA['$defs'][definition]
        assert set(value) == set(schema['properties']) == set(schema['required'])
        for field in value:
            changed = copy.deepcopy(value)
            del changed[field]
            rejected(definition, changed)
        rejected(definition, {**value, 'private_key': CANARY})
        rejected(definition, list(value.items()))
    if type(value) is list:
        rejected(definition, tuple(value))


@pytest.mark.parametrize('profile', ['monitor', 'support', 'restart'])
def test_canonical_bundle_bytes_and_hashes_match_independent_fixed_oracles(profile):
    value = bundle(profile)
    oracle = V['canonical']['bundles'][profile]
    actual = contract.validate_provision('Bundle', value, now_ms=NOW)
    assert canonical(actual, max_bytes=16384) == oracle['canonical']
    assert hashlib.sha256(oracle['canonical'].encode('ascii')).hexdigest() == oracle['sha256']
    assert actual['policy_hash'] == V['canonical']['policy_hash']
    assert contract.parse_provision('Bundle', raw(dict(reversed(list(value.items())))), now_ms=NOW) == actual
    actual['pins'].append({'private_key': CANARY})
    assert value == bundle(profile)


def test_canonical_policy_hash_covers_all_profiles_and_is_not_a_trust_anchor():
    value = example('Policy')
    assert canonical(value, max_bytes=16384) == V['canonical']['policy']
    assert hashlib.sha256(V['canonical']['policy'].encode('ascii')).hexdigest() == V['canonical']['policy_hash']
    value['issuer_ref'] = 'different-issuer'
    assert contract.validate_provision('Policy', value, now_ms=NOW) == value
    assert hashlib.sha256(canonical(value).encode()).hexdigest() != V['canonical']['policy_hash']


@pytest.mark.parametrize('definition', ['IssueResult', 'IssueResponse', 'ReadResult', 'ReadResponse'])
def test_contained_bundle_hash_must_match_exact_cjson(definition):
    value = example(definition)
    value['bundle']['tenant'] = 'different'
    rejected(definition, value)
    digest = hashlib.sha256(canonical(value['bundle'], max_bytes=16384).encode()).hexdigest()
    if definition.startswith('Issue'):
        value['bundle_hash'] = digest
    else:
        value['status']['bundle_hash'] = digest
    assert contract.validate_provision(definition, value, now_ms=NOW) == value
    # Rehashing changed data demonstrates internal consistency, never trust.


@pytest.mark.parametrize('profile', ['monitor', 'support', 'restart'])
@pytest.mark.parametrize('field', ['device_id', 'claim_id', 'device_epoch'])
def test_profile_device_null_rules_hold_for_every_generation(profile, field):
    value = bundle(profile)
    value['generation'] = 7
    value['previous_provision_id'] = OTHER
    value[field] = (1 if field == 'device_epoch' else OTHER) if profile == 'monitor' else None
    rejected('Bundle', value)


def test_previous_generation_is_a_caller_head_comparison_not_an_invented_schema_rule():
    value = bundle()
    value.update(generation=7, previous_provision_id=None)
    assert contract.validate_provision('Bundle', value, now_ms=NOW) == value
    value.update(generation=1, previous_provision_id=OTHER)
    assert contract.validate_provision('Bundle', value, now_ms=NOW) == value


@pytest.mark.parametrize('definition', ['IssueRequest', 'IssueAdminRequest'])
def test_issue_generation_and_previous_id_pairing(definition):
    value = example(definition)
    for generation, previous in ((0, None), (1, OTHER), (MAX_GENERATION - 1, OTHER)):
        value.update(expected_generation=generation, expected_provision_id=previous)
        assert contract.validate_provision(definition, value, now_ms=NOW) == value
    for generation, previous in ((0, OTHER), (1, None), (MAX_GENERATION, OTHER)):
        value.update(expected_generation=generation, expected_provision_id=previous)
        rejected(definition, value)


@pytest.mark.parametrize('definition,lower,upper', [
    ('Generation', 1, MAX_GENERATION), ('ExpectedGeneration', 0, MAX_GENERATION - 1),
    ('Timestamp', 0, SAFE), ('Epoch', 0, SAFE),
])
def test_integer_boundaries_are_safe_and_bool_is_never_an_integer(definition, lower, upper):
    for valid in (lower, upper):
        assert contract.validate_provision(definition, valid, now_ms=NOW) == valid
    for invalid in (lower - 1, upper + 1, True, False, 1.0, '1', None, [], {}):
        rejected(definition, invalid)


@pytest.mark.parametrize('invalid', [None, True, False, -1, SAFE + 1, 1.0, '0'])
def test_caller_clock_is_a_required_safe_integer(invalid):
    rejected('Timestamp', NOW, now_ms=invalid)
    rejected('Timestamp', b'0', now_ms=invalid, parse=True)


@pytest.mark.parametrize('definition', [None, True, [], {}, '', 'Bundle ', 'StatusAdminRequest',
                                       'AckAdminRequest', 'UnknownResult', '#/$defs/Bundle', 'PrivateKey'])
def test_unknown_definition_never_selects_a_fallback(definition):
    rejected(definition, bundle())


@pytest.mark.parametrize('value', ['https://a.invalid', 'https://a.invalid:443', 'https://a-b.invalid',
                                  'https://' + 'a' * 63 + '.invalid'])
def test_fixed_dns_origin_accepts_central_explicit_default_port_without_normalizing(value):
    assert contract.validate_provision('Origin', value, now_ms=NOW) == value


@pytest.mark.parametrize('value', ['https://127.0.0.1', 'https://127.1:443', 'https://2130706433',
                                  'https://0x7f.1', 'https://0177.0.0.1', 'https://0x',
                                  'https://a.0xg'])
def test_numeric_origin_grammar_preserves_central_url_meaning_without_trusting_the_host(value):
    # These are parser oracles, not approved origins and never network targets.
    assert contract.validate_provision('Origin', value, now_ms=NOW) == value


@pytest.mark.parametrize('zeroes', [70, 251])
def test_central_url_oracle_checks_numeric_hostname_after_ipv4_normalization(zeroes):
    # Independent central/Node oracle: this original origin becomes 0.0.0.1
    # before central hostname-label checks, so its original label may exceed 63.
    value = 'https://' + '0' * zeroes + '1'
    assert contract.validate_provision('Origin', value, now_ms=NOW) == value
    assert contract.parse_provision('Origin', raw(value), now_ms=NOW) == value


@pytest.mark.parametrize('value', ['https://single', 'https://a.invalid/', 'https://a.invalid?',
                                  'https://a.invalid#', 'https://a.invalid:444', 'https://a.invalid:0443',
                                  'https://user@a.invalid', 'https://a..invalid', 'https://-a.invalid',
                                  'https://a-.invalid', 'https://A.invalid', 'http://a.invalid',
                                  'https://a.invalid\n', 'https://[::1]', 'https://a.invalid.',
                                  'https://256.256.256.256', 'https://example.1', 'https://09',
                                  'https://1.2.3.4.5', 'https://4294967296', 'https://1.16777216',
                                  'https://.1', 'https://1..1', 'https://' + '0' * 252 + '1',
                                  'https://' + 'a' * 64 + '.invalid',
                                  'https://' + '.'.join(['a' * 63] * 4)])
def test_origin_semantics_reject_nonfixed_or_noncentral_hosts(value):
    rejected('Origin', value)


@pytest.mark.parametrize('value', ['https://1.2.3.4.', 'https://1.'])
def test_origin_intentionally_rejects_trailing_dot_numeric_hosts_accepted_by_central(value):
    # Product import keeps a stricter fixed-host boundary; never rewrite signed origin bytes.
    rejected('Origin', value)


@pytest.mark.parametrize('definition,value', [('UUID', OTHER.upper()), ('UUID', OTHER + '\n'),
                                            ('UUID', OTHER.replace('-4', '-1', 1)), ('Hash', 'A' * 64),
                                            ('Identifier', '/tmp'), ('Identifier', 'a' * 65),
                                            ('Environment', 'a' * 33), ('RealmEpoch', ':realm'),
                                            ('Audience', 'a' * 101), ('CSRF', '1792663200.' + 'A' * 64),
                                            ('ProfileID', ['support']), ('ChannelRef', 'other')])
def test_closed_strings_and_identifiers_do_not_use_js_property_coercion(definition, value):
    rejected(definition, value)


@pytest.mark.parametrize('profile', ['support', 'restart'])
def test_all_pins_must_be_current_unique_and_cover_only_their_profile(profile):
    definition = profile.title() + 'Pins'
    original = bundle(profile)['pins']
    for count in (1, 8):
        pins = [{**copy.deepcopy(original[0]), 'kid': 'synthetic-' + str(i)} for i in range(count)]
        assert contract.validate_provision(definition, pins, now_ms=NOW) == pins
    rejected(definition, original * 9)
    rejected(definition, [])
    extra = copy.deepcopy(original[0])
    extra['kid'] = 'synthetic-other'
    for start, end in ((NOW + 1, NOW + 2), (NOW - 1, NOW), (NOW, NOW), (NOW + 2, NOW + 1)):
        extra.update(not_before=start, expires_at=end)
        rejected(definition, [*original, extra])
    changed = copy.deepcopy(original)
    changed[0]['purposes'] = changed[0]['purposes'][:-1]
    rejected(definition, changed)
    other = 'restart' if profile == 'support' else 'support'
    rejected(definition, bundle(other)['pins'])
    changed = copy.deepcopy(original)
    changed[0]['purposes'] *= 2
    rejected(definition, changed)


def test_purpose_coverage_can_be_split_across_distinct_current_pins():
    original = bundle('support')['pins'][0]
    pins = [{**copy.deepcopy(original), 'kid': 'synthetic-' + str(i), 'purposes': [purpose]}
            for i, purpose in enumerate(original['purposes'])]
    assert contract.validate_provision('SupportPins', pins, now_ms=NOW) == pins


@pytest.mark.parametrize('mutation', ['trailing', 'truncated', 'algorithm', 'exponent',
                                     'short-modulus', 'even-modulus', 'nonminimal-length'])
def test_public_spki_is_strict_der_rsa3072_e65537_without_native_crypto(mutation):
    pin = bundle('support')['pins'][0]
    decoded = bytearray(base64.urlsafe_b64decode(pin['spki'] + '=' * (-len(pin['spki']) % 4)))
    if mutation == 'trailing':
        decoded += b'\x00'
    elif mutation == 'truncated':
        del decoded[-1]
    elif mutation == 'algorithm':
        position = decoded.index(bytes.fromhex('06092a864886f70d0101010500'))
        decoded[position + 10] = 2
    elif mutation == 'exponent':
        decoded[-1] = 3
    elif mutation == 'short-modulus':
        position = decoded.index(bytes.fromhex('0282018100'))
        decoded[position + 5] &= 0x7f
    elif mutation == 'even-modulus':
        position = decoded.index(bytes.fromhex('0203010001'))
        decoded[position - 1] &= 0xfe
    else:
        assert decoded[:2] == bytes.fromhex('3082')
        decoded = b'\x30\x83\x00' + decoded[2:]
    pin['spki'] = base64.urlsafe_b64encode(decoded).rstrip(b'=').decode('ascii')
    rejected('SupportPin', pin)


def test_public_spki_base64url_requires_exact_unpadded_reencoding():
    pin = bundle('restart')['pins'][0]
    original = pin['spki']
    for value in (original + '=', 'AQAB', 'A' * 2732, '', CANARY + '\n'):
        rejected('RestartPin', {**pin, 'spki': value})
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
    changed = original[:-1] + alphabet[alphabet.index(original[-1]) | 1]
    assert changed != original
    padding = '=' * (-len(original) % 4)
    assert base64.urlsafe_b64decode(changed + padding) == base64.urlsafe_b64decode(original + padding)
    rejected('RestartPin', {**pin, 'spki': changed})


@pytest.mark.parametrize('changes', [
    {'import_expires_at': NOW}, {'import_expires_at': NOW - 1},
    {'import_expires_at': NOW + 1800001}, {'expires_at': NOW},
    {'expires_at': NOW + 30 * 86400000 + 1},
    {'import_expires_at': NOW + 1000, 'expires_at': NOW + 999},
])
def test_bundle_local_time_order_and_caps_are_enforced(changes):
    value = bundle()
    value.update(changes)
    rejected('Bundle', value)


def test_import_window_is_separate_from_common_bundle_and_historical_metadata():
    value = bundle()
    for now in (value['issued_at'], value['import_expires_at'] - 1):
        assert contract.validate_import_bundle(value, now_ms=now) == value
    for now in (value['issued_at'] - 1, value['import_expires_at'], value['expires_at']):
        assert contract.validate_provision('Bundle', value, now_ms=now) == value
        with pytest.raises(contract.ProvisionError, match='^SDC_PROVISION_INVALID$'):
            contract.validate_import_bundle(value, now_ms=now)
    response = example('ReadResponse')
    after_import = response['bundle']['import_expires_at']
    assert contract.validate_provision('ReadResponse', response, now_ms=after_import) == response
    for definition in ('Status', 'Receipt', 'StatusResponse', 'AckResponse'):
        saved = example(definition)
        assert contract.validate_provision(definition, saved, now_ms=SAFE) == saved


def test_bundle_authority_cannot_outlive_the_shortest_selected_pin():
    value = bundle('support')
    value['pins'].append({**copy.deepcopy(value['pins'][0]), 'kid': 'synthetic-short',
                          'expires_at': value['expires_at'] - 1})
    rejected('Bundle', value)
    value['expires_at'] -= 1
    assert contract.validate_import_bundle(value, now_ms=NOW) == value
    value['pins'][0]['not_before'] = NOW + 1
    rejected('Bundle', value, now_ms=NOW + 2)


@pytest.mark.parametrize('definition', ['ReadResult', 'ReadResponse'])
def test_noncurrent_read_hides_bundle_without_promoting_a_saved_receipt(definition):
    value = example(definition)
    for state in ('superseded', 'revoked', 'expired'):
        value['status']['state'] = state
        rejected(definition, value)
        saved = {**value, 'bundle': None}
        assert contract.validate_provision(definition, saved, now_ms=SAFE) == saved


@pytest.mark.parametrize('wire,expected', [(b'0', 0), (b'1.0', 1), (b'1e0', 1), (b'1E+3', 1000),
                                         (b'0.0', 0), (b'9007199254740991', SAFE),
                                         (b'9007199254740991.0', SAFE),
                                         (b'9007199254740991.1', SAFE)])
def test_json_integral_numbers_match_central_binary64_cjson_meaning(wire, expected):
    value = contract.parse_provision('Timestamp', wire, now_ms=NOW)
    assert type(value) is int and value == expected
    assert canonical(value) == str(expected)


@pytest.mark.parametrize('wire', [b'-0', b'-0.0', b'-0e0', b'-0E+10'])
def test_provision_control_oracle_normalizes_negative_zero_and_preserves_field_minima(wire):
    # Independent strictJSON -> validateProvisionControl('issue') oracle accepts
    # expected_generation:-0. Diagnostics has a different negative-zero rule.
    for definition in ('Timestamp', 'Epoch', 'ExpectedGeneration'):
        actual = contract.parse_provision(definition, wire, now_ms=NOW)
        assert type(actual) is int and actual == 0
    value = example('IssueRequest')
    assert value['expected_generation'] == 0 and value['expected_provision_id'] is None
    source = raw(value).replace(b'"expected_generation":0', b'"expected_generation":' + wire)
    assert source != raw(value)
    assert contract.parse_provision('IssueRequest', source, now_ms=NOW) == value
    rejected('Generation', wire, parse=True)
    revoke = example('RevokeRequest')
    expected = b'"expected_generation":' + str(revoke['expected_generation']).encode('ascii')
    source = raw(revoke).replace(expected, b'"expected_generation":' + wire)
    assert source != raw(revoke)
    rejected('RevokeRequest', source, parse=True)


@pytest.mark.parametrize('wire', [b'1.5', b'1e309', b'-1e309',
                                b'NaN', b'Infinity', b'-Infinity', b'9007199254740992',
                                b'9007199254740992.0', b'1e99999', b'true', b'null'])
def test_raw_numbers_reject_nonfinite_fraction_bool_and_unsafe(wire):
    rejected('Timestamp', wire, parse=True)


def test_strict_json_duplicate_bom_depth_unicode_and_size_limits_hide_raw_values():
    source = raw(example('ReadRequest'))
    for invalid in (b'\xef\xbb\xbf' + source, source + b' trailing', source + b'\x00',
                    source.replace(b'{', b'{"root_id":"' + OTHER.encode() + b'",', 1),
                    source.replace(b'{', b'{"\\u0072oot_id":"' + OTHER.encode() + b'",', 1),
                    b'{"x":1,"x":2}', b'{"x":1,"\\u0078":2}', b'{"x":{"y":1,"y":2}}',
                    b'[' * 1000 + b'0' + b']' * 1000, b'[' * 9 + b'0' + b']' * 9,
                    b'\xff', b'"\\ud800"', b'"\\u00e9"', b'"line\\nfeed"',
                    b'{"private_key":"' + CANARY.encode() + b'"}', b' ' * 16385):
        rejected('ReadRequest', invalid, parse=True)
    for invalid in (None, {}, [], bytearray(source), '\ud800', 'é' * 8193):
        rejected('ReadRequest', invalid, parse=True)
    assert contract.parse_provision('Timestamp', b' ' * 16383 + b'0', now_ms=NOW) == 0
    rejected('Timestamp', b' ' * 16384 + b'0', parse=True)
    # UTF-8 is required; escaped ASCII is allowed and normalized for CJSON.
    encoded = raw(bundle()).replace(b'"self"', b'"\\u0073elf"')
    assert contract.parse_provision('Bundle', encoded, now_ms=NOW) == bundle()
    nested = raw(example('IssueResponse')).replace(b'"tenant":', b'"tenant":"self","tenant":')
    rejected('IssueResponse', nested, parse=True)


def test_cycles_subclasses_nonjson_values_and_dependency_errors_are_fixed(monkeypatch):
    class ForeignDict(dict):
        pass

    class ForeignInt(int):
        pass

    cyclic = bundle()
    cyclic['private_key'] = cyclic
    for invalid in (cyclic, ForeignDict(bundle()), {**bundle(), 'private_key': object()},
                    {**bundle(), 'generation': ForeignInt(1)}, {**bundle(), CANARY: CANARY}):
        rejected('Bundle', invalid)
    import sdc_support_signatures as signatures

    def dependency_failure(*args, **kwargs):
        raise ValueError(CANARY)

    monkeypatch.setattr(signatures, 'validate_pins', dependency_failure)
    rejected('SupportPin', bundle('support')['pins'][0])
