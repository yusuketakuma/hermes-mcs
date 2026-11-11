"""v3 wire deps面: signed_send/verify/fetch_artifactの実装。

spec 03 endpoint表・SEM01/12/18/27/28のbindingを結線する。credential
(config/state/key)とv3 pin集合はprovider callableとして注入 — v3 trust
record形式が確定するまでfactory側が解決する。それまでは本module単体で
synthetic providerにより完全に検証可能。

verifyは直前にsigned_sendが送ったrequestをpath別に保持し、応答envelopeの
独立expected照合(verify_v3_envelope)へ渡す。送っていないpathへのverify、
expectedを構成できないrequest、envelope検証失敗は全て例外でfail-closed。
"""
import hashlib
import json
import secrets

from adapters.sdc import protocol, release_auto_contract as contract, release_contract
from adapters.sdc.support_contract import canonical
from sdc_support_signatures import verify_release_attestation, verify_v3_envelope

SELECT_PATH = '/v3/mcs/releases/select'
METADATA_PATH = '/v3/mcs/releases/metadata'
ARTIFACT_PATH = '/v3/mcs/releases/artifact'
START_PATH = '/v3/mcs/releases/start'
STATUS_PATH = '/v3/mcs/releases/status'
ABORT_PATH = '/v3/mcs/releases/cancel-prestart'
RESULT_PATH = '/v3/mcs/releases/result'
HEALTH_PATH = '/v3/mcs/releases/health'
CONSENT_PREVIEW_PATH = '/v3/mcs/releases/consent-preview'
CONSENT_CONFIRM_PATH = '/v3/mcs/releases/consent-confirm'
CONSENT_REVOKE_PATH = '/v3/mcs/releases/consent-revoke'

# device POST path → 応答SignedEnvelopeのpurpose。artifactは生bytesで対象外、
# metadataはrelease_metadata専用面(attestation/Compatibility両対応)で対象外。
_PATH_PURPOSE = {
    SELECT_PATH: 'mcs-auto-offer-v3',
    START_PATH: 'mcs-auto-grant-v3',
    STATUS_PATH: 'mcs-auto-status-v3',
    ABORT_PATH: 'mcs-auto-status-v3',
    RESULT_PATH: 'mcs-auto-ack-v3',
    HEALTH_PATH: 'mcs-auto-health-ack-v3',
    CONSENT_PREVIEW_PATH: 'mcs-auto-consent-preview-v3',
    CONSENT_CONFIRM_PATH: 'mcs-auto-consent-receipt-v3',
    CONSENT_REVOKE_PATH: 'mcs-auto-consent-receipt-v3',
}

# signed_send可能なdevice JSON path。metadataは応答がattestation/Compatibility
# の二形式なのでverify()の汎用経路では扱わずrelease_metadata内部で検証する。
_SENDABLE = frozenset(_PATH_PURPOSE) | {METADATA_PATH}


class WireError(RuntimeError):
    """wire deps失敗。codeは固定token。"""

    def __init__(self, code='WIRE_FAILED'):
        super().__init__(code)
        self.code = code


def _sha256_canonical(value):
    return hashlib.sha256(canonical(value).encode('ascii')).hexdigest()


def _expected(path, purpose, request):
    """送信requestから応答payloadへの独立binding集合を構成する(SEM表)。"""
    req = request
    if purpose == 'mcs-auto-offer-v3':  # Offer: SEM12 request_id/nonce/profile
        expected = {'request_id': req.get('request_id'),
                    'request_nonce': req.get('request_nonce'),
                    'scope': req.get('scope')}
        profile = req.get('profile')
        if isinstance(profile, dict) and 'profile_sha256' in profile:
            expected['profile_sha256'] = profile['profile_sha256']
        return expected
    if purpose == 'mcs-auto-grant-v3':  # StartGrant: SEM18 StartRequest全binding
        return {'start_request_id': req.get('request_id'),
                'request_nonce': req.get('request_nonce'),
                'operation_id': req.get('operation_id'),
                'scope': req.get('scope'),
                'consent_id': req.get('consent_id'),
                'local_consent_epoch': req.get('local_consent_epoch'),
                'policy_epoch': req.get('policy_epoch'),
                'rollout_epoch': req.get('rollout_epoch'),
                'publication_id': req.get('publication_id'),
                'manifest_digest': req.get('manifest_digest'),
                'compatibility_sha256': req.get('compatibility_sha256'),
                'preflight_sha256': req.get('preflight_sha256'),
                'attempt_key': req.get('attempt_key'),
                'window': req.get('window'),
                'rollout': req.get('rollout')}
    if purpose == 'mcs-auto-ack-v3':  # OutcomeAck: SEM27 report_id/nonce/hash
        return {'request_id': req.get('report_id'),
                'request_nonce': req.get('request_nonce'),
                'scope': req.get('scope'),
                'operation_id': req.get('operation_id'),
                'report_id': req.get('report_id'),
                'outcome_sha256': _sha256_canonical(req)}
    if purpose == 'mcs-auto-status-v3':  # OperationStatus: SEM28 ID/nonce/kind/object
        expected = {'request_id': req.get('request_id'),
                    'request_nonce': req.get('request_nonce'),
                    'scope': req.get('scope'),
                    'operation_id': req.get('operation_id')}
        if path == ABORT_PATH:
            expected['kind'] = 'prestart_abort'
        else:
            expected['kind'] = req.get('kind')
            expected['object_id'] = req.get('object_id')
        return expected
    if purpose == 'mcs-auto-health-ack-v3':  # HealthAck: report_id/observation hash
        return {'request_id': req.get('report_id'),
                'request_nonce': req.get('request_nonce'),
                'scope': req.get('scope'),
                'report_id': req.get('report_id'),
                'observation_sha256': _sha256_canonical(req)}
    if purpose == 'mcs-auto-consent-preview-v3':  # ConsentPreview
        return {'request_id': req.get('request_id'),
                'request_nonce': req.get('request_nonce'),
                'scope': req.get('scope'),
                'central_proposal_id': req.get('central_proposal_id')}
    if purpose == 'mcs-auto-consent-receipt-v3':  # ConsentReceipt
        if path == CONSENT_REVOKE_PATH:
            # ConsentRevokeRequestはscope/consent_id/new epochを直接持つ。
            return {'request_id': req.get('request_id'),
                    'request_nonce': req.get('request_nonce'),
                    'scope': req.get('scope'),
                    'consent_id': req.get('consent_id'),
                    'local_consent_epoch': req.get('new_local_consent_epoch')}
        # ConsentConfirmRequestはscopeを持たず、consent内側が正本。
        consent = req.get('consent')
        if not isinstance(consent, dict):
            raise WireError('WIRE_CONSENT')
        expected = {'request_id': req.get('request_id'),
                    'request_nonce': req.get('request_nonce'),
                    'scope': consent.get('scope'),
                    'consent_id': consent.get('consent_id')}
        for key in ('local_consent_epoch', 'policy_epoch'):
            if key in consent:
                expected[key] = consent[key]
        expected['consent_sha256'] = contract.hash_v3('standing-consent', consent)
        return expected
    raise WireError('WIRE_PURPOSE')


def _det_uuid(seed):
    """sha256 seedから決定論的なv4形uuidを作る(owner._det_uuidと同形)。"""
    h = hashlib.sha256(seed.encode()).digest()
    b = bytearray(h[:16])
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    s = b.hex()
    return f'{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}'


def _metadata_request(scope, release_id, profile_sha256, kind):
    """kind別の論理requestを決定論的に構成する(retryで同一bytes)。"""
    seed = hashlib.sha256(
        ('mcs-auto-v3|metadata|' + kind + '|' + release_id + '|' + profile_sha256
         + '|' + canonical(scope)).encode()).hexdigest()
    request_id = _det_uuid('id|' + seed)
    request_nonce = hashlib.sha256(('nonce|' + seed).encode()).hexdigest()
    body = {'schema_version': 'mcs.metadata-request.v3',
            'request_id': request_id, 'request_nonce': request_nonce,
            'scope': scope, 'release_id': release_id,
            'profile_sha256': profile_sha256, 'kind': kind}
    return json.dumps(body, ensure_ascii=True, separators=(',', ':')).encode()


def _compatibility_sha256(payload):
    """ENC03: compatibility_sha256 = H("compatibility", verified payload)。"""
    return hashlib.sha256(
        b'MCS-AUTO-v3\ncompatibility\n' + canonical(payload).encode('ascii')
    ).hexdigest()


class Wire:
    """deps契約のsigned_send/verify/fetch_artifact/release_metadata面。

    transport: ReleaseAutoTransport互換 (send_json/fetch_artifact)。
    credential: callable() -> (config, state, key) — 署名材料の都度解決。
    pins: callable() -> list — 信頼済みv3 pin集合の都度解決。
    now_ms: callable() -> int。
    release_trust/release_scope: 既存release attestation検証用のprovider。
        未設定ならrelease_metadataはWIRE_UNAVAILABLEでfail-closed。
    """

    def __init__(self, *, transport, credential, pins, now_ms,
                 release_trust=None, release_scope=None):
        self._transport = transport
        self._credential = credential
        self._pins = pins
        self._now_ms = now_ms
        self._release_trust = release_trust
        self._release_scope = release_scope
        self._pending = {}
        self._offer = None

    def _sign(self, path, raw):
        try:
            config, state, key = self._credential()
            return protocol.sign_release_auto(
                config, path, raw, state, key,
                now=self._now_ms() // 1000, nonce=secrets.token_hex(16))
        except WireError:
            raise
        except Exception as e:
            raise WireError('WIRE_CREDENTIAL') from e

    def signed_send(self, path, raw_bytes):
        if path not in _SENDABLE or type(raw_bytes) is not bytes or not raw_bytes:
            raise WireError('WIRE_PATH')
        try:
            request = json.loads(raw_bytes.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            request = None
        self._pending[path] = {'request': request, 'raw': raw_bytes}
        headers = self._sign(path, raw_bytes)
        try:
            return self._transport.send_json(path, raw_bytes, headers)
        except Exception as e:
            raise WireError('WIRE_SEND') from e

    def verify(self, path, response_bytes):
        purpose = _PATH_PURPOSE.get(path)
        if purpose is None or type(response_bytes) is not bytes:
            raise WireError('WIRE_PATH')
        pending = self._pending.get(path)
        if pending is None or not isinstance(pending['request'], dict):
            raise WireError('WIRE_NO_REQUEST')
        try:
            pins = self._pins()
            expected = _expected(path, purpose, pending['request'])
            payload = verify_v3_envelope(
                response_bytes, purpose=purpose, pins=pins,
                expected=expected, now_ms=self._now_ms())
        except WireError:
            raise
        except Exception as e:
            raise WireError('WIRE_VERIFY') from e
        if path == SELECT_PATH:
            self._offer = payload
        return payload

    def fetch_artifact(self, request_raw, *, expected_sha256, expected_size, sink):
        if type(request_raw) is not bytes or not request_raw:
            raise WireError('WIRE_REQUEST')
        headers = self._sign(ARTIFACT_PATH, request_raw)
        try:
            return self._transport.fetch_artifact(
                ARTIFACT_PATH, request_raw, headers,
                expected_sha256=expected_sha256,
                expected_size=expected_size, sink=sink)
        except Exception as e:
            raise WireError('WIRE_FETCH') from e

    def _metadata_send(self, kind, scope, release_id, profile_sha256):
        """metadata要求を送り(raw, parsed)を返す。署名・path結合はsigned_send経路。"""
        raw = _metadata_request(scope, release_id, profile_sha256, kind)
        response = self.signed_send(METADATA_PATH, raw)
        try:
            value = json.loads(response.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise WireError('WIRE_METADATA') from None
        if not isinstance(value, dict):
            raise WireError('WIRE_METADATA')
        return response, value

    def release_metadata(self, target, profile):
        """検証済み公開metadataを返す。offer.selectedの署名済みclaimを
        attestation/compatibility両endpointの検証済み値と相互照合する(SEM14)。

        戻り値: {'publication_id','manifest_digest','compatibility_sha256',
                 'rollout_epoch'}。offer未検証・selected不一致・期限切れ・
        provider未設定は全てfail-closed。
        """
        if self._release_trust is None or self._release_scope is None:
            raise WireError('WIRE_UNAVAILABLE')
        offer = self._offer
        if not isinstance(offer, dict) or offer.get('status') != 'candidate':
            raise WireError('WIRE_METADATA')
        if type(offer.get('expires_at')) is not int or offer['expires_at'] <= self._now_ms():
            raise WireError('WIRE_METADATA')
        selected = offer.get('selected')
        profile_sha256 = profile.get('profile_sha256') if isinstance(profile, dict) else None
        if (not isinstance(selected, dict)
                or selected.get('release') != target
                or not isinstance(profile_sha256, str)
                or offer.get('profile_sha256') != profile_sha256):
            raise WireError('WIRE_METADATA')
        scope = offer['scope']
        release_id = target.get('release_id') if isinstance(target, dict) else None
        if type(release_id) is not str:
            raise WireError('WIRE_METADATA')

        # kind=attestation: 既存release attestation envelopeを検証し
        # manifest_digestを独立算出する。
        _, attestation = self._metadata_send(
            'attestation', scope, release_id, profile_sha256)
        try:
            trust = self._release_trust()
            expected = self._release_scope()
            payload = verify_release_attestation(
                attestation, trust=trust, expected=expected,
                now_ms=self._now_ms())
        except WireError:
            raise
        except Exception as e:
            raise WireError('WIRE_METADATA') from e
        manifest_digest = release_contract.hash_release_manifest(payload)
        if payload.get('release_id') != release_id:
            raise WireError('WIRE_METADATA')

        # kind=compatibility: v3 SignedEnvelope(Compatibility)をraw bytesで検証し
        # compatibility_sha256をENC03のdomain-separated hashで算出する。
        compat_raw, _ = self._metadata_send(
            'compatibility', scope, release_id, profile_sha256)
        try:
            pins = self._pins()
            payload = verify_v3_envelope(
                compat_raw, purpose='mcs-auto-compatibility-v3', pins=pins,
                expected={'scope': scope, 'target': target, 'profile': profile},
                now_ms=self._now_ms())
        except WireError:
            raise
        except Exception as e:
            raise WireError('WIRE_METADATA') from e
        compat_sha = _compatibility_sha256(payload)

        # offer.selectedの署名済みclaimと検証済み値を相互照合する。
        rollout = selected.get('rollout')
        if (selected.get('manifest_digest') != manifest_digest
                or selected.get('compatibility_sha256') != compat_sha
                or not isinstance(rollout, dict)
                or type(rollout.get('authorization_epoch')) is not int
                or offer.get('rollout_epoch') != rollout['authorization_epoch']
                or type(selected.get('publication_id')) is not str):
            raise WireError('WIRE_METADATA')
        return {'publication_id': selected['publication_id'],
                'manifest_digest': manifest_digest,
                'compatibility_sha256': compat_sha,
                'rollout_epoch': offer['rollout_epoch']}
