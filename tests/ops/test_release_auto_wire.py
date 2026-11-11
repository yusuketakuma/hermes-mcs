"""v3 wire deps面: signed_send/verify/fetch_artifact/release_metadataの結線検証。

verify_v3_envelope/verify_release_attestationはmonkeypatchで差し替え、
expected導出・offer照合・domain-separated hash計算・決定論的request構成を検証する。
署名・envelope検証自体はtest_sdc_release_auto_signatures側の責務。
"""
import hashlib
import io
import json
import re

import pytest

import release_auto_wire as wire_mod
from release_auto_wire import (
    ABORT_PATH, ARTIFACT_PATH, CONSENT_CONFIRM_PATH, CONSENT_REVOKE_PATH,
    METADATA_PATH, RESULT_PATH, SELECT_PATH, START_PATH, STATUS_PATH,
    Wire, WireError, _compatibility_sha256,
)
from adapters.sdc import release_contract
from adapters.sdc.support_contract import canonical

CONFIG = {'origin': 'https://sdc.example.test', 'audience': 'mcs-sdc',
          'environment': 'test'}
STATE = {'device_id': '12345678-1234-4234-8234-123456789abc',
         'key_id': '12345678-1234-4234-8234-123456789abd'}
KEY = 'a' * 64
NOW_MS = 1700000000000
SCOPE = {'tenant': 't1', 'installation': 'i1', 'device_id': STATE['device_id'],
         'realm_epoch': 'r1'}
TARGET = {'release_id': 'b' * 64, 'code_seq': 3, 'source_commit': 'c' * 40,
          'artifact_sha256': 'd' * 64, 'artifact_size': 1024}
PROFILE = {'profile_sha256': 'e' * 64, 'cohort': 'pilot'}


class FakeTransport:
    def __init__(self, responder=None, artifact_result=None):
        self.sent = []
        self.responder = responder
        self.artifact_result = artifact_result or {'sha256': 'f' * 64, 'size': 10}

    def send_json(self, path, raw, headers):
        self.sent.append((path, raw, headers))
        if self.responder is not None:
            return self.responder(path, raw, headers)
        return b'{}'

    def fetch_artifact(self, path, raw, headers, *, expected_sha256,
                       expected_size, sink):
        self.sent.append((path, raw, headers))
        sink.write(b'x' * expected_size)
        return dict(self.artifact_result)


def make_wire(transport=None, **kw):
    params = dict(transport=transport or FakeTransport(),
                  credential=lambda: (CONFIG, STATE, KEY),
                  pins=lambda: [{'kid': 'a' * 64}],
                  now_ms=lambda: NOW_MS)
    params.update(kw)
    return Wire(**params)


def req_bytes(body):
    return json.dumps(body, separators=(',', ':'), ensure_ascii=True).encode()


def test_signed_send_signs_and_posts():
    transport = FakeTransport()
    wire = make_wire(transport)
    raw = req_bytes({'schema_version': 'mcs.status-request.v3'})
    assert wire.signed_send(STATUS_PATH, raw) == b'{}'
    path, sent_raw, headers = transport.sent[0]
    assert path == STATUS_PATH and sent_raw == raw
    assert set(headers) == {'X-SDC-Device', 'X-SDC-Key-ID', 'X-SDC-Time',
                            'X-SDC-Nonce', 'X-SDC-Signature'}
    assert re.fullmatch(r'[0-9a-f]{32}', headers['X-SDC-Nonce'])


def test_signed_send_rejects_unknown_or_bad_input():
    wire = make_wire()
    raw = req_bytes({'a': 1})
    for bad in ('/v3/other', ARTIFACT_PATH, METADATA_PATH + 'x', 123):
        with pytest.raises(WireError):
            wire.signed_send(bad, raw)
    for bad_raw in (b'', 'not-bytes', None):
        with pytest.raises(WireError):
            wire.signed_send(STATUS_PATH, bad_raw)


def test_verify_requires_prior_send():
    wire = make_wire()
    with pytest.raises(WireError) as e:
        wire.verify(STATUS_PATH, b'{}')
    assert e.value.code == 'WIRE_NO_REQUEST'
    with pytest.raises(WireError):
        wire.verify(METADATA_PATH, b'{}')  # metadataは汎用verify対象外


def _capture_expected(monkeypatch, payload=None):
    captured = {}

    def fake(raw, *, purpose, pins, expected, now_ms, **kw):
        captured.update(purpose=purpose, pins=pins, expected=expected,
                        now_ms=now_ms, raw=raw)
        return payload if payload is not None else {'schema_version': 'x'}

    monkeypatch.setattr(wire_mod, 'verify_v3_envelope', fake)
    return captured


def _send_and_verify(wire, path, body, response=b'{"envelope":{}}'):
    wire.signed_send(path, req_bytes(body))
    return wire.verify(path, response)


def test_verify_offer_expected_binding(monkeypatch):
    captured = _capture_expected(monkeypatch)
    wire = make_wire(FakeTransport(lambda p, r, h: b'{"envelope":{}}'))
    body = {'request_id': 'r1', 'request_nonce': 'n1', 'scope': SCOPE,
            'profile': PROFILE}
    _send_and_verify(wire, SELECT_PATH, body)
    assert captured['purpose'] == 'mcs-auto-offer-v3'
    assert captured['expected'] == {'request_id': 'r1', 'request_nonce': 'n1',
                                    'scope': SCOPE, 'profile_sha256': 'e' * 64}


def test_verify_grant_expected_binding(monkeypatch):
    captured = _capture_expected(monkeypatch)
    wire = make_wire(FakeTransport(lambda p, r, h: b'{"envelope":{}}'))
    body = {'request_id': 'req-1', 'request_nonce': 'n', 'operation_id': 'op',
            'scope': SCOPE, 'consent_id': 'c', 'local_consent_epoch': 1,
            'policy_epoch': 2, 'rollout_epoch': 3, 'publication_id': 'pub',
            'manifest_digest': 'm' * 64, 'compatibility_sha256': 'k' * 64,
            'preflight_sha256': 'p' * 64, 'attempt_key': 'a',
            'window': {'starts_at': 1}, 'rollout': {'stage': 'pilot'}}
    _send_and_verify(wire, START_PATH, body)
    expected = captured['expected']
    assert expected['start_request_id'] == 'req-1'
    assert expected['window'] == {'starts_at': 1}
    assert expected['rollout'] == {'stage': 'pilot'}
    assert 'request_id' not in expected


def test_verify_ack_binds_canonical_outcome_hash(monkeypatch):
    captured = _capture_expected(monkeypatch)
    wire = make_wire(FakeTransport(lambda p, r, h: b'{"envelope":{}}'))
    outcome = {'report_id': 'rep-1', 'request_nonce': 'n', 'scope': SCOPE,
               'operation_id': 'op', 'state': 'verified'}
    _send_and_verify(wire, RESULT_PATH, outcome)
    expected = captured['expected']
    assert expected['request_id'] == 'rep-1' and expected['report_id'] == 'rep-1'
    assert expected['outcome_sha256'] == hashlib.sha256(
        canonical(outcome).encode('ascii')).hexdigest()


def test_verify_status_and_abort_bindings(monkeypatch):
    captured = _capture_expected(monkeypatch)
    wire = make_wire(FakeTransport(lambda p, r, h: b'{"envelope":{}}'))
    status_req = {'request_id': 's1', 'request_nonce': 'n', 'scope': SCOPE,
                  'operation_id': 'op', 'kind': 'operation', 'object_id': 'op'}
    _send_and_verify(wire, STATUS_PATH, status_req)
    assert captured['expected']['kind'] == 'operation'
    assert captured['expected']['object_id'] == 'op'

    abort_req = {'request_id': 'a1', 'request_nonce': 'n2', 'scope': SCOPE,
                 'operation_id': 'op', 'start_request_id': 'sr',
                 'grant_id': 'g', 'prepare_nonce': 'p',
                 'execution_revision': 1, 'local_abort_receipt_id': 'ar',
                 'reason': 'x', 'observed_at': 1}
    _send_and_verify(wire, ABORT_PATH, abort_req)
    assert captured['expected']['kind'] == 'prestart_abort'
    assert 'object_id' not in captured['expected']


def test_verify_consent_confirm_binds_inner_consent(monkeypatch):
    captured = _capture_expected(monkeypatch)
    wire = make_wire(FakeTransport(lambda p, r, h: b'{"envelope":{}}'))
    consent = {'consent_id': 'cid', 'scope': SCOPE, 'local_consent_epoch': 4,
               'policy_epoch': 5, 'enabled': True}
    body = {'request_id': 'cr1', 'request_nonce': 'n', 'preview_id': 'pv',
            'preview_sha256': 'h' * 64, 'consent': consent}
    _send_and_verify(wire, CONSENT_CONFIRM_PATH, body)
    expected = captured['expected']
    assert expected['scope'] == SCOPE and expected['consent_id'] == 'cid'
    assert expected['local_consent_epoch'] == 4
    assert expected['consent_sha256'] == hashlib.sha256(
        b'MCS-AUTO-v3\nstanding-consent\n' + canonical(consent).encode('ascii')
    ).hexdigest()
    assert 'scope' not in body  # request直下にscopeが無いことを確認済み


def test_verify_consent_revoke_binds_new_epoch(monkeypatch):
    captured = _capture_expected(monkeypatch)
    wire = make_wire(FakeTransport(lambda p, r, h: b'{"envelope":{}}'))
    body = {'request_id': 'rv1', 'request_nonce': 'n', 'scope': SCOPE,
            'consent_id': 'cid', 'previous_local_consent_epoch': 4,
            'new_local_consent_epoch': 5,
            'local_revocation_receipt_id': 'lr', 'observed_at': 1}
    _send_and_verify(wire, CONSENT_REVOKE_PATH, body)
    expected = captured['expected']
    assert expected['local_consent_epoch'] == 5
    assert 'consent_sha256' not in expected


def test_verify_retains_offer_and_wraps_failures(monkeypatch):
    payload = {'status': 'candidate', 'selected': {'release': TARGET}}
    _capture_expected(monkeypatch, payload=payload)
    wire = make_wire(FakeTransport(lambda p, r, h: b'{"envelope":{}}'))
    _send_and_verify(wire, SELECT_PATH,
                     {'request_id': 'r', 'request_nonce': 'n',
                      'scope': SCOPE, 'profile': PROFILE})
    assert wire._offer == payload

    def boom(*a, **k):
        raise ValueError('bad envelope')

    monkeypatch.setattr(wire_mod, 'verify_v3_envelope', boom)
    with pytest.raises(WireError) as e:
        wire.verify(SELECT_PATH, b'{"envelope":{}}')
    assert e.value.code == 'WIRE_VERIFY'


def test_signed_send_preserves_transport_error_detail():
    """ReleaseAutoErrorのstatus/code/retry_after_msをWireErrorへ退避し、
    retry分類に必要な中央拒否詳細を失わない。"""
    from adapters.sdc.release_auto_transport import ReleaseAutoError

    def responder(path, raw, headers):
        raise ReleaseAutoError(409, 'OFFER_EXPIRED', 5000)

    wire = make_wire(FakeTransport(responder))
    with pytest.raises(WireError) as e:
        wire.signed_send(STATUS_PATH, req_bytes({'a': 1}))
    err = e.value
    assert err.code == 'WIRE_SEND'
    assert err.status == 409
    assert err.remote_code == 'OFFER_EXPIRED'
    assert err.retry_after_ms == 5000
    assert isinstance(err.__cause__, ReleaseAutoError)


def test_signed_send_plain_failure_has_no_remote_detail():
    def responder(path, raw, headers):
        raise RuntimeError('net')

    wire = make_wire(FakeTransport(responder))
    with pytest.raises(WireError) as e:
        wire.signed_send(STATUS_PATH, req_bytes({'a': 1}))
    assert e.value.code == 'WIRE_SEND'
    assert e.value.status is None
    assert e.value.remote_code is None
    assert e.value.retry_after_ms is None


def test_fetch_artifact_preserves_transport_error_detail():
    from adapters.sdc.release_auto_transport import ReleaseAutoError

    transport = FakeTransport()

    def failing(*a, **k):
        raise ReleaseAutoError(0, 'NETWORK_UNAVAILABLE')

    transport.fetch_artifact = failing
    wire = make_wire(transport)
    with pytest.raises(WireError) as e:
        wire.fetch_artifact(req_bytes({'release_id': 'r'}),
                            expected_sha256='f' * 64, expected_size=10,
                            sink=io.BytesIO())
    err = e.value
    assert err.code == 'WIRE_FETCH'
    assert err.status == 0
    assert err.remote_code == 'NETWORK_UNAVAILABLE'
    assert err.retry_after_ms == 0


def test_fetch_artifact_delegates_with_bounds():
    transport = FakeTransport(artifact_result={'sha256': 'f' * 64, 'size': 10})
    wire = make_wire(transport)
    sink = io.BytesIO()
    raw = req_bytes({'release_id': 'r'})
    result = wire.fetch_artifact(raw, expected_sha256='f' * 64, expected_size=10,
                                 sink=sink)
    assert result == {'sha256': 'f' * 64, 'size': 10}
    path, sent_raw, _headers = transport.sent[0]
    assert path == ARTIFACT_PATH and sent_raw == raw

    def bad_fetch(*a, **k):
        raise RuntimeError('net')

    transport.fetch_artifact = bad_fetch
    with pytest.raises(WireError) as e:
        wire.fetch_artifact(raw, expected_sha256='f' * 64, expected_size=10,
                            sink=sink)
    assert e.value.code == 'WIRE_FETCH'


def _offer_payload(**over):
    selected = {'release': TARGET, 'manifest_digest': 'm' * 64,
                'compatibility_sha256': 'c' * 64,
                'publication_id': '12345678-1234-4234-8234-123456789abc',
                'publication_proof_sha256': 'p' * 64,
                'acceptance_sha256': 'a' * 64,
                'rollout': {'authorization_epoch': 7}}
    offer = {'status': 'candidate', 'scope': SCOPE,
             'profile_sha256': 'e' * 64, 'rollout_epoch': 7,
             'expires_at': NOW_MS + 60000, 'selected': selected}
    offer.update(over)
    return offer


def test_release_metadata_requires_providers_and_offer():
    wire = make_wire()
    with pytest.raises(WireError) as e:
        wire.release_metadata(TARGET, PROFILE)
    assert e.value.code == 'WIRE_UNAVAILABLE'

    wire = make_wire(release_trust=lambda: {}, release_scope=lambda: {})
    with pytest.raises(WireError) as e:
        wire.release_metadata(TARGET, PROFILE)
    assert e.value.code == 'WIRE_METADATA'


def test_release_metadata_full_flow(monkeypatch):
    attestation = {'release_id': TARGET['release_id'], 'core': {}}
    compat_payload = {'scope': SCOPE, 'target': TARGET, 'profile': PROFILE}
    calls = []

    def responder(path, raw, headers):
        if path != METADATA_PATH:
            return b'{"envelope":{}}'
        kind = json.loads(raw)['kind']
        calls.append(kind)
        if kind == 'attestation':
            return json.dumps({'envelope': {}, 'payload': attestation}).encode()
        return json.dumps({'envelope': {}}).encode()

    transport = FakeTransport(responder)
    wire = make_wire(transport, release_trust=lambda: {'t': 1},
                     release_scope=lambda: {'s': 1})
    monkeypatch.setattr(wire_mod, 'verify_release_attestation',
                        lambda *a, **k: attestation)
    compat_sha = _compatibility_sha256(compat_payload)
    manifest = release_contract.hash_release_manifest(attestation)
    state = {'offer': None}

    def fake_v3(raw, *, purpose, **kw):
        if purpose == 'mcs-auto-offer-v3':
            return state['offer']
        assert purpose == 'mcs-auto-compatibility-v3'
        return compat_payload

    monkeypatch.setattr(wire_mod, 'verify_v3_envelope', fake_v3)

    def seed_offer(offer):
        state['offer'] = offer
        wire.signed_send(SELECT_PATH, req_bytes(
            {'request_id': 'r', 'request_nonce': 'n', 'scope': SCOPE,
             'profile': PROFILE}))
        wire.verify(SELECT_PATH, b'{"envelope":{}}')

    # offer未検証
    with pytest.raises(WireError):
        wire.release_metadata(TARGET, PROFILE)
    assert calls == []

    # selected.release不一致 → 送信前に拒否
    seed_offer(_offer_payload())
    other = dict(TARGET, release_id='0' * 64)
    with pytest.raises(WireError):
        wire.release_metadata(other, PROFILE)
    assert calls == []

    # 期限切れoffer
    seed_offer(_offer_payload(expires_at=NOW_MS - 1))
    with pytest.raises(WireError):
        wire.release_metadata(TARGET, PROFILE)
    assert calls == []

    # 正常系: attestation+compatibility両検証で4fieldを返す
    offer = _offer_payload()
    offer['selected']['manifest_digest'] = manifest
    offer['selected']['compatibility_sha256'] = compat_sha
    seed_offer(offer)
    result = wire.release_metadata(TARGET, PROFILE)
    assert result == {'publication_id': offer['selected']['publication_id'],
                      'manifest_digest': manifest,
                      'compatibility_sha256': compat_sha,
                      'rollout_epoch': 7}
    assert calls == ['attestation', 'compatibility']

    # metadata requestはkind別に決定論的(同一bytes)
    wire.release_metadata(TARGET, PROFILE)
    att_raws = [r for p, r, h in transport.sent
                if p == METADATA_PATH
                and json.loads(r)['kind'] == 'attestation']
    assert len(att_raws) == 2 and att_raws[0] == att_raws[1]

    # selectedのclaimと検証値が不一致なら拒否
    bad = _offer_payload()
    bad['selected']['compatibility_sha256'] = compat_sha
    bad['selected']['manifest_digest'] = '0' * 64
    seed_offer(bad)
    with pytest.raises(WireError):
        wire.release_metadata(TARGET, PROFILE)


def test_metadata_request_is_deterministic_and_schema_bound():
    raw1 = wire_mod._metadata_request(SCOPE, TARGET['release_id'],
                                      PROFILE['profile_sha256'], 'attestation')
    raw2 = wire_mod._metadata_request(SCOPE, TARGET['release_id'],
                                      PROFILE['profile_sha256'], 'attestation')
    assert raw1 == raw2
    body = json.loads(raw1)
    assert body['schema_version'] == 'mcs.metadata-request.v3'
    assert body['kind'] == 'attestation'
    assert body['release_id'] == TARGET['release_id']
    assert len(body['request_nonce']) == 64
