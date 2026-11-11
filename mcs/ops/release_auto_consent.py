"""v3 standing consentの取得・確認・撤回。

- request_consent_preview: ConsentPreviewRequestを送り、署名検証済みの
  ConsentPreview payloadを返す。preview内容は中央proposalの写しで、
  端末側で窓・予算・policyを改変しない。
- confirm_standing_policy: 検証済みpreviewからStandingConsentを構成し
  ConsentConfirmRequestを送る。ConsentReceipt(active)を検証後、owner stateの
  consentへ耐久保存する。consentの全policy項目はpreviewと完全一致させ、
  人確認の実施は呼出側(表示→人の確認→私有receipt)の責務とする。
- disable_auto: trusted CLI `auto-disable`相当のlocal撤回。consentを
  enabled=false・local_consent_epoch増分で先に耐久保存し、その後中央の
  consent-revokeを送る。通信失敗してもlocal撤回は戻さない(02 §revocation)。

request_id/request_nonce/各種IDは論理内容から決定論的に導出し、retry時に
中央のrequest hash冪等(replay)へ乗せる。confirmed_atはpreview.issued_atを
使う — preview有効期(<=5分)内の確定的な時刻で、retryでconsent bytesが
変わりCONFIRM_CONFLICTになるのを防ぐ。人確認時刻の真正性は
confirmation_receipt_idが私有証拠へ束縛する。
"""
import json
import uuid

from adapters.sdc import release_auto_contract as contract
from adapters.sdc import release_auto_state as auto_state
from release_auto_owner import _det_uuid, _mutate, _request_ids
from release_auto_wire import (CONSENT_CONFIRM_PATH, CONSENT_PREVIEW_PATH,
                               CONSENT_REVOKE_PATH)

PREVIEW_REQUEST = 'mcs.consent-preview-request.v3'
CONFIRM_REQUEST = 'mcs.consent-confirm-request.v3'
REVOKE_REQUEST = 'mcs.consent-revoke-request.v3'
STANDING_CONSENT = 'mcs.standing-consent.v3'

# 人確認の私有証拠(spec 03 SEM10: confirmation receiptは実機人確認の私有証拠と
# 対応)。preview bytesごとにreceipt_idを耐久化し、retryで同一consent bytesを
# 再構成する。owner state正本ではなく独立sidecar — consent schemaの項目ではない。
CONFIRMATION_NAME = 'release-consent-confirmation.json'
_CONFIRMATION_SCHEMA = 'mcs.consent-confirmation-local.v1'
_CONFIRMATION_MAX = 16


class ConsentError(RuntimeError):
    """consent経路の拒否。codeは固定token。"""

    def __init__(self, code='CONSENT_HOLD'):
        super().__init__(code)
        self.code = code


def _require(ok, code='CONSENT_HOLD'):
    if not ok:
        raise ConsentError(code)


def _send(wire, path, request, definition):
    raw = contract.canonical_typed(request, definition)
    response = wire.signed_send(path, raw)
    return wire.verify(path, response)


def _active_receipt(receipt):
    _require(isinstance(receipt, dict), 'CONSENT_RECEIPT')
    _require(receipt.get('status') == 'active', 'CONSENT_NOT_ACTIVE')
    return receipt


def _confirmation_load(store):
    raw = store.read(CONFIRMATION_NAME)
    if raw is None:
        return {'schema_version': _CONFIRMATION_SCHEMA, 'entries': {}}
    entries = raw.get('entries') if isinstance(raw, dict) else None
    if (not isinstance(raw, dict)
            or raw.get('schema_version') != _CONFIRMATION_SCHEMA
            or not isinstance(entries, dict)):
        raise ConsentError('CONSENT_EVIDENCE')
    for key, entry in entries.items():
        if (not isinstance(key, str) or len(key) != 64
                or not isinstance(entry, dict)
                or not isinstance(entry.get('receipt_id'), str)
                or type(entry.get('confirmed_at')) is not int
                or ('reason' in entry and not isinstance(entry['reason'], str))):
            raise ConsentError('CONSENT_EVIDENCE')
    return raw


def ensure_confirmation_receipt(store, preview, now_ms, *, reason=None):
    """preview bytesへ束縛した人確認receipt_idを私有証拠へ耐久化して返す。

    previewは呼出側でwire.verify済みのpayload。schema検証後、
    preview_sha256をkeyにentriesへ{receipt_id, confirmed_at, preview_id,
    reason?}を記録する。同一previewの再呼出しは最初のreceipt_idを返す — これにより
    confirm retryでconsent bytesが一致しCONFIRM_CONFLICTを避けられる。
    entriesはconfirmed_at昇順で_CONFIRMATION_MAX件まで保持する
    (preview TTL<=5分のため古い証拠の実害はない)。
    """
    _require(isinstance(preview, dict), 'CONSENT_PREVIEW')
    preview = contract.parse_typed(
        contract.canonical_typed(preview, 'ConsentPreview'), 'ConsentPreview',
        max_bytes=contract.AUTO_INPUT_BYTES)
    _require(type(now_ms) is int and now_ms > 0, 'CONSENT_CLOCK')
    preview_sha256 = contract.hash_v3('consent-preview', preview)
    with store.release_owner_lock():
        state = _confirmation_load(store)
        entries = state['entries']
        existing = entries.get(preview_sha256)
        if isinstance(existing, dict):
            return existing['receipt_id']
        receipt_id = str(uuid.uuid4())
        entry = {'receipt_id': receipt_id, 'confirmed_at': now_ms,
                 'preview_id': preview.get('preview_id')}
        if isinstance(reason, str):
            entry['reason'] = reason
        entries[preview_sha256] = entry
        if len(entries) > _CONFIRMATION_MAX:
            for key, _ in sorted(
                    entries.items(),
                    key=lambda kv: kv[1]['confirmed_at']
            )[:len(entries) - _CONFIRMATION_MAX]:
                del entries[key]
        store.write(CONFIRMATION_NAME, state)
        return receipt_id


def request_consent_preview(wire, *, scope, profile_sha256, central_proposal_id):
    """ConsentPreviewRequestを送り検証済みConsentPreview payloadを返す。

    scope/profile_sha256/central_proposal_idは呼出側がdeviceの登録値と
    admin proposalから供給する。wire.verifyが署名・purpose・request bindingを
    検証済みのpayloadだけを返す。
    """
    scope_json = json.dumps(scope, ensure_ascii=True, sort_keys=True,
                            separators=(',', ':'))
    request_id, request_nonce = _request_ids(
        'consent-preview', scope_json, profile_sha256, central_proposal_id)
    request = {'schema_version': PREVIEW_REQUEST,
               'request_id': request_id, 'request_nonce': request_nonce,
               'scope': scope, 'profile_sha256': profile_sha256,
               'central_proposal_id': central_proposal_id}
    return _send(wire, CONSENT_PREVIEW_PATH, request, 'ConsentPreviewRequest')


def _consent_from_preview(preview, confirmation_receipt_id):
    """検証済みConsentPreviewからStandingConsentを構成する。

    policy/window/profile/epoch/早期採用者/両policy hashはpreviewと完全一致
    (SEM10・中央CONSENT_MISMATCHと同じ規則)。confirmation_receipt_idは
    実機人確認の私有証拠へ対応するIDを呼出側が渡す(spec 03:
    confirmation receiptは実機人確認の私有証拠と対応)。consent_idは
    preview_idから決定論的に導出しretryで同一consent bytesを再構成する。
    expires_atはproposal承認のconsent_expires_atをそのまま上限として使う。
    """
    preview_id = preview.get('preview_id')
    _require(isinstance(preview_id, str), 'CONSENT_PREVIEW')
    consent = {
        'schema_version': STANDING_CONSENT,
        'consent_id': _det_uuid('consent|' + preview_id),
        'scope': preview['scope'],
        'profile': preview['profile'],
        'allowed_class': preview['allowed_class'],
        'timezone': preview['timezone'],
        'windows': preview['windows'],
        'max_prepare_seconds': preview['max_prepare_seconds'],
        'max_prepare_count_24h': preview['max_prepare_attempts_24h'],
        'max_prepare_seconds_24h': preview['max_prepare_seconds_24h'],
        'local_consent_epoch': preview['local_consent_epoch'],
        'policy_epoch': preview['policy_epoch'],
        'central_policy_id': preview['central_policy_id'],
        'central_policy_sha256': preview['central_policy_sha256'],
        'confirmation_receipt_id': confirmation_receipt_id,
        'confirmed_at': preview['issued_at'],
        'expires_at': preview['consent_expires_at'],
        'enabled': True,
        'early_adopter': preview['early_adopter'],
        'rollout_policy_sha256': preview['rollout_policy_sha256'],
    }
    return contract.parse_typed(
        contract.canonical_typed(consent, 'StandingConsent'), 'StandingConsent',
        max_bytes=contract.AUTO_INPUT_BYTES)


def confirm_standing_policy(store, wire, *, preview, clock,
                            confirmation_receipt_id):
    """previewからStandingConsentを構成し中央confirm→owner stateへ保存する。

    previewはrequest_consent_preview等でwire.verify済みのpayloadを渡す。
    confirmation_receipt_idは実機人確認の私有証拠へ対応するUUIDv4を呼出側
    (人間向けconfirm CLI)が渡す — retryでは同一IDを再利用しないとconsent
    bytesが変わりCONFIRM_CONFLICTになる。期限切れpreview・receipt非active・
    wire失敗は全てConsentErrorで止め、owner stateは変更しない(confirm応答を
    確認してからだけ保存する)。戻り値は保存したStandingConsent dict。
    """
    _require(wire is not None, 'CONSENT_WIRE')
    _require(isinstance(preview, dict), 'CONSENT_PREVIEW')
    preview = contract.parse_typed(
        contract.canonical_typed(preview, 'ConsentPreview'), 'ConsentPreview',
        max_bytes=contract.AUTO_INPUT_BYTES)
    now_ms = clock.wall_ms()
    _require(type(now_ms) is int, 'CONSENT_CLOCK')
    _require(type(preview.get('expires_at')) is int
             and preview['expires_at'] > now_ms, 'CONSENT_PREVIEW_EXPIRED')
    consent = _consent_from_preview(preview, confirmation_receipt_id)
    _require(type(consent['expires_at']) is int
             and consent['expires_at'] > now_ms, 'CONSENT_EXPIRED')
    preview_sha256 = contract.hash_v3('consent-preview', preview)
    request_id, request_nonce = _request_ids(
        'consent-confirm', preview['preview_id'], preview_sha256)
    request = {'schema_version': CONFIRM_REQUEST,
               'request_id': request_id, 'request_nonce': request_nonce,
               'preview_id': preview['preview_id'],
               'preview_sha256': preview_sha256, 'consent': consent}
    receipt = _active_receipt(_send(
        wire, CONSENT_CONFIRM_PATH, request, 'ConsentConfirmRequest'))
    _require(receipt.get('consent_sha256') == contract.hash_v3(
        'standing-consent', consent), 'CONSENT_RECEIPT')

    def apply(new):
        new['consent'] = consent

    saved = _mutate(store, apply)
    return saved['consent']


def _revoke_request(consent, new_epoch):
    request_id, request_nonce = _request_ids(
        'consent-revoke', consent['consent_id'], new_epoch)
    return {'schema_version': REVOKE_REQUEST,
            'request_id': request_id, 'request_nonce': request_nonce,
            'scope': consent['scope'], 'consent_id': consent['consent_id'],
            'previous_local_consent_epoch': new_epoch - 1,
            'new_local_consent_epoch': new_epoch,
            'local_revocation_receipt_id': _det_uuid(
                'consent-revocation|' + consent['consent_id'] + '|' + str(new_epoch)),
            'observed_at': consent['confirmed_at']}


def disable_auto(store, wire=None):
    """auto-disable相当のlocal撤回。戻り値は結果dict。

    consentが無い/既にdisabledならlocal書換えはせず、中央revokeだけを
    冪等requestで再試行する(wire指定時)。local撤回が先・中央通知は後で、
    通信失敗してもlocal状態は戻さない。観測時刻はconsent.confirmed_atを
    使いretryでrequest bytesを固定する(常に過去時刻で<=nowを満たす)。
    """
    owner = auto_state.load(store)
    consent = owner.get('consent')
    if not isinstance(consent, dict):
        return {'action': 'noop', 'reason': 'no_consent'}
    _require(type(consent.get('local_consent_epoch')) is int,
             'CONSENT_EPOCH')
    if consent.get('enabled') is True:
        new_epoch = consent['local_consent_epoch'] + 1
        revoked = dict(consent)
        revoked['enabled'] = False
        revoked['local_consent_epoch'] = new_epoch
        contract.parse_typed(
            contract.canonical_typed(revoked, 'StandingConsent'),
            'StandingConsent', max_bytes=contract.AUTO_INPUT_BYTES)

        def apply(new):
            new['consent'] = revoked

        _mutate(store, apply)
        consent = revoked
    else:
        new_epoch = consent['local_consent_epoch']
    result = {'action': 'disabled', 'local_consent_epoch': new_epoch,
              'central_notified': False}
    if wire is not None:
        request = _revoke_request(consent, new_epoch)
        receipt = wire.verify(CONSENT_REVOKE_PATH, wire.signed_send(
            CONSENT_REVOKE_PATH,
            contract.canonical_typed(request, 'ConsentRevokeRequest')))
        _require(isinstance(receipt, dict)
                 and receipt.get('status') == 'revoked'
                 and receipt.get('local_consent_epoch') == new_epoch,
                 'CONSENT_RECEIPT')
        result['central_notified'] = True
    return result
