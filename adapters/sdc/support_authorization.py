"""R4aの操作・個別承認・grantを照合する（署名・本人同意・実行許可は別）。"""
from __future__ import annotations

from .support_contract import (MAX_SAFE_INTEGER, hash_intent, validate_approval,
                               validate_grant, validate_operation, validate_preview)

_SCOPE = ('tenant', 'installation', 'device_id', 'environment', 'case_id')
_EPOCHS = ('realm_epoch', 'policy_epoch', 'device_epoch', 'local_consent_epoch', 'case_epoch')
_OPERATION_BINDING = (*_SCOPE, *_EPOCHS, 'audience', 'product', 'consent_revision',
                      'capability_revision', 'purpose_code')
_GRANT_BINDING = (*_SCOPE, *_EPOCHS, 'operation_id', 'payload_hash', 'approval_hash')


def _require(ok, code):
    if not ok:
        raise ValueError(code)



def _approval_for_operation(operation, approval, now_ms, code):
    approval = validate_approval(approval, now_ms=now_ms)
    _require(approval['approval_kind'] == 'individual'
             and approval['approval_id'] == operation['approval_id']
             and approval['approved_by'] == operation['approved_by']
             and approval['intent_hash'] == hash_intent(operation)
             and approval['approval_hash'] == operation['approval_hash'], code)
    _require(all(approval[key] == operation[key] for key in (*_EPOCHS, 'consent_revision')), code)
    # Central publicationApproval permits a shorter individual-approval lifetime.
    _require(approval['issued_at'] >= operation['issued_at']
             and approval['expires_at'] <= operation['expires_at'], code)
    return approval


def match_operation(operation, approval, *, consent_preview, now_ms):
    """Bind verified operation/approval to a trusted private receipt's preview.

    Never derive consent_preview from the untrusted operation itself. The caller
    must separately establish signatures, local human consent, current credential,
    scope/authority and revocation state. This pure match neither admits nor runs.
    The case-consent approver and individual-operation approver are distinct roles.
    """
    code = 'SUPPORT_OPERATION_BINDING_MISMATCH'
    try:
        _require(type(now_ms) is int and 0 <= now_ms <= MAX_SAFE_INTEGER, code)
        _require(type(consent_preview) is dict, code)
        # The five-minute display window is historical; local consent has its own expiry.
        preview = validate_preview(consent_preview, now_ms=consent_preview['not_before'])
        _require(preview['not_before'] <= now_ms < preview['consent_expires_at'], code)
        operation = validate_operation(operation, now_ms=now_ms)
        approval = _approval_for_operation(operation, approval, now_ms, code)
        _require(all(operation[key] == preview[key] for key in _OPERATION_BINDING), code)
        _require(operation['params']['diagnostic_id'] in preview['diagnostic_ids']
                 and operation['requested_by'] in preview['operator_refs'], code)
        _require(operation['expires_at'] <= preview['consent_expires_at']
                 and approval['expires_at'] <= preview['consent_expires_at'], code)
        return operation
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ValueError(code) from None


def match_grant(grant, operation, approval, *, grant_request_id, receipt_id, admission_id,
                owner_generation, now_ms):
    """Bind a verified grant to its operation, individual approval and persisted IDs.

    IDs must come from the local durable intent, never the received grant. Same live
    continuation, monotonic remaining budget, latest consent/authority and STARTED
    CAS are runner responsibilities; HMAC freshness is not an execution deadline.
    """
    code = 'SUPPORT_GRANT_BINDING_MISMATCH'
    try:
        grant = validate_grant(grant, now_ms=now_ms)
        operation = validate_operation(operation, now_ms=now_ms)
        approval = _approval_for_operation(operation, approval, now_ms, code)
        _require(all(grant[key] == operation[key] for key in _GRANT_BINDING), code)
        _require(grant['expires_at'] <= min(operation['expires_at'], approval['expires_at']), code)
        for key, expected in (('grant_request_id', grant_request_id), ('receipt_id', receipt_id),
                              ('admission_id', admission_id), ('owner_generation', owner_generation)):
            _require(type(expected) is str and grant[key] == expected, code)
        return grant
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ValueError(code) from None
