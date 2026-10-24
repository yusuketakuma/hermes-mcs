"""人が起動した支援同意だけを登録し、監視・診断・操作実行から分離する。"""
import hashlib
import re
import secrets
import time
import uuid

from .connector import Connector
from .protocol import endpoint, sign_support
from .support_contract import canonical, validate_preview
from . import support_state as local
from . import provision

PREVIEW = '/v2/support/cases/preview'
ACCEPT = '/v2/support/cases/accept'
BOOTSTRAP = 'sdc.support.bootstrap.v1'
_PROFILE_SCOPE = ('tenant', 'installation', 'realm_epoch', 'device_epoch', 'device_id', 'claim_id', 'environment', 'audience')


class SupportClientError(ValueError):
    def __init__(self, code='SDC_SUPPORT_UNAVAILABLE'):
        self.code = code if code in {'SDC_SUPPORT_UNAVAILABLE', 'SDC_SUPPORT_NOT_PROVISIONED',
                                     'SDC_SUPPORT_IDENTITY_CHANGED', 'SDC_SUPPORT_RESPONSE_INVALID',
                                     'SDC_SUPPORT_EXPIRED'} else 'SDC_SUPPORT_UNAVAILABLE'
        super().__init__(self.code)


def _require(value, code='SDC_SUPPORT_RESPONSE_INVALID'):
    if not value:
        raise SupportClientError(code)


def _uuid(value):
    try:
        parsed = uuid.UUID(value) if type(value) is str else None
        return parsed is not None and parsed.version == 4 and str(parsed) == value
    except ValueError:
        return False


def _hash(value):
    return hashlib.sha256(canonical(value, max_bytes=16384).encode('ascii')).hexdigest()


def _profile_binding(snapshot, *, now_ms):
    """Bound local use by the genuine Bundle and its currently usable public pins."""
    bundle = snapshot['bundle']
    active = [pin['expires_at'] for pin in bundle['pins'] if pin['not_before'] <= now_ms < pin['expires_at']]
    _require(active and bundle['issued_at'] <= now_ms < bundle['expires_at'], 'SDC_SUPPORT_EXPIRED')
    return {'bundle_hash': snapshot['bundle_hash'], 'expires_at': min(bundle['expires_at'], *active),
            'endpoint': {k: bundle[k] for k in ('origin', 'audience', 'environment')}}


def _identity(auth):
    return {**auth['identity'], 'environment': auth['endpoint']['environment'],
            'audience': auth['endpoint']['audience'], **auth.get('scope', {})}


def _profile_budget(auth, clock, budget):
    if 'expires_at' in auth:
        if callable(getattr(budget, 'shorten', None)):
            budget.shorten(auth['expires_at'])
        _require(int(clock() * 1000) < auth['expires_at'], 'SDC_SUPPORT_EXPIRED')
    budget()


def _bootstrap(store, check_pins, now_ms, purposes):
    """Read only an already approved local provision; this is not its authenticity proof."""
    value = store.read('support-bootstrap.json')
    _require(type(value) is dict, 'SDC_SUPPORT_NOT_PROVISIONED')
    fields = {'schema_version', 'root_fingerprint', 'monitor_endpoint_hash', 'support_endpoint', 'pins'}
    _require(set(value) in (fields, fields | {'authority'})
             and value['schema_version'] == BOOTSTRAP)
    monitor = store.enrollment_binding()
    _require(value['root_fingerprint'] == monitor['root_fingerprint'] and value['monitor_endpoint_hash'] == monitor['endpoint_hash'], 'SDC_SUPPORT_IDENTITY_CHANGED')
    support = value['support_endpoint']
    _require(type(support) is dict and all(type(v) is str for v in support.values()))
    endpoint(support)
    config = store.read('endpoint.json')
    if 'authority' in value:
        current = provision.profile_snapshot(store, profile='support', now_ms=now_ms)
        _require(value['authority'] == current, 'SDC_SUPPORT_IDENTITY_CHANGED')
        bundle = current['bundle']
        _require(bundle['profile_id'] == 'support' and support == {k: bundle[k] for k in support}
                 and value['pins'] == bundle['pins'] and support['environment'] == config['environment'],
                 'SDC_SUPPORT_IDENTITY_CHANGED')
    else:
        _require(all(support[k] == config[k] for k in ('audience', 'environment')), 'SDC_SUPPORT_IDENTITY_CHANGED')
    for purpose in purposes:
        check_pins(value['pins'], purpose=purpose, now_ms=now_ms)
    # Validate encodability/size independently of the injected crypto implementation.
    _hash(value['pins'])
    return value


def _supplier(store, check_pins, clock, auth, *, purposes=('consent-preview',)):
    initial_identity = None

    def current():
        nonlocal initial_identity
        value = _bootstrap(store, check_pins, int(clock() * 1000), purposes)
        state = store.read('state.json')
        _require(type(state) is dict and state.get('state') == 'active' and state.get('enabled') is True
                 and not state.get('disconnected') and not state.get('central_suspended')
                 and not state.get('enrollment_hold') and 'rotation_id' not in state, 'SDC_SUPPORT_IDENTITY_CHANGED')
        _require(all(_uuid(state.get(k)) for k in ('claim_id', 'device_id', 'key_id')), 'SDC_SUPPORT_IDENTITY_CHANGED')
        credential = store.read('credential')  # Existing customer Keychain read is noninteractive.
        _require(type(credential) is dict and not any(k in credential for k in ('next_key', 'next_key_id', 'rotation_id')), 'SDC_SUPPORT_IDENTITY_CHANGED')
        key = Connector(store, None).secret(state, credential)
        _require(type(key) is str and re.fullmatch('[0-9a-f]{64}', key) is not None, 'SDC_SUPPORT_IDENTITY_CHANGED')
        identity = {k: state[k] for k in ('claim_id', 'device_id', 'key_id')}
        managed = value.get('authority')
        if managed is not None:
            _require(all(identity[k] == managed['bundle'][k] for k in ('claim_id', 'device_id')), 'SDC_SUPPORT_IDENTITY_CHANGED')
        _require(initial_identity is None or identity == initial_identity, 'SDC_SUPPORT_IDENTITY_CHANGED')
        if initial_identity is None:
            initial_identity = dict(identity)
        auth.clear()
        auth.update(endpoint=value['support_endpoint'], pins=value['pins'], identity=identity, key=key,
                    guard=store.guard_credential)
        binding = {'root_fingerprint': value['root_fingerprint'], 'monitor_endpoint_hash': value['monitor_endpoint_hash'],
                'support_endpoint_hash': _hash(value['support_endpoint']), 'pins_sha256': _hash(value['pins']),
                'secret_sha256': hashlib.sha256(bytes.fromhex(key)).hexdigest(), 'key_id': state['key_id']}
        if managed is not None:
            binding['profile'] = _profile_binding(managed, now_ms=int(clock() * 1000))
            auth.update(scope={k: managed['bundle'][k] for k in _PROFILE_SCOPE}, expires_at=binding['profile']['expires_at'])
        return binding
    return current


def _send(path, record, auth, transport_factory, clock, budget):
    _profile_budget(auth, clock, budget)
    raw = record['raw'].encode('ascii')
    headers = sign_support(auth['endpoint'], path, raw, auth['identity'], auth['key'], now=clock(), nonce=secrets.token_hex(16))
    auth['guard']()  # Cached auth never sends past an unfinished credential change.
    return transport_factory(auth['endpoint']).send(path, raw, headers)


def initialize_local_state(store, *, check_pins, clock=time.time):
    """明示provisionの許可窓内で、現在の束縛から支援の初期状態を保存する。"""
    auth = {}
    try:
        current = _supplier(store, check_pins, clock, auth)
        return local.prepare(store, binding_supplier=current, now_ms=int(clock() * 1000), initialize=True)
    finally:
        auth.clear()


def connect(store, case_id, *, verifier, check_pins, transport_factory, confirm,
            clock=time.time, budget=lambda: None):
    """Register one foreground consent; caller owns a bounded whole-session deadline.

    All I/O starts after a persisted state admission. A stop may race with an already
    admitted single request; it prevents the next admission. ACK evidence alone may
    be saved after stop. Signature/current preview identity are verified before human
    consent; later runners must derive expected operation scope from that receipt.
    No initialization, poll, diagnosis, restart, discovery or service operation occurs.
    """
    _require(_uuid(case_id))
    auth = {}
    current = _supplier(store, check_pins, clock, auth)
    try:
        with store.support_session_lock():
            budget()
            snapshot = local.prepare(store, binding_supplier=current, now_ms=int(clock() * 1000))
            status = local.client_status(store, now_ms=int(clock() * 1000))
            _require(status['owner_generation'] == snapshot['owner_generation'], 'SDC_SUPPORT_IDENTITY_CHANGED')
            receipt = snapshot['receipt']
            if receipt is None or receipt['preview']['case_id'] != case_id:
                request = local.begin_preview(store, case_id, expected_owner_generation=snapshot['owner_generation'],
                                              binding_supplier=current, now_ms=int(clock() * 1000))
                response = _send(PREVIEW, request, auth, transport_factory, clock, budget)
                budget()
                _require(type(response) is dict and set(response) == {'schema_version', 'preview_id', 'preview', 'request_id'}
                         and response['schema_version'] == 'sdc.support.v1' and response['preview_id'] == request['request_id']
                         and _uuid(response['request_id']))
                payload = verifier(canonical(response['preview'], max_bytes=16384).encode('ascii'), purpose='consent-preview',
                                   pins=auth['pins'], now_ms=int(clock() * 1000))
                _profile_budget(auth, clock, budget)
                payload = validate_preview(payload, now_ms=int(clock() * 1000))
                expected = {**_identity(auth), 'preview_id': request['request_id'], 'case_id': case_id}
                _require(all(payload[k] == v for k, v in expected.items()), 'SDC_SUPPORT_IDENTITY_CHANGED')
                # Managed scope is fixed by the independent Bundle. Legacy signed
                # preview adoption remains distinct from an operation's expected scope.
                local.record_preview(store, request, payload, now_ms=int(clock() * 1000))
                if confirm(payload, dict(auth['endpoint'])) is not True:
                    return {'state': 'cancelled'}
                _profile_budget(auth, clock, budget)
                snapshot = local.confirm_consent(store, payload, expected_owner_generation=snapshot['owner_generation'],
                                                 expected_binding=request['binding'], binding_supplier=current,
                                                 now_ms=int(clock() * 1000), confirm_human=True)
            else:
                # A new foreground session must rebind the saved receipt to the current
                # monitor identity before cached ACK use or any unknown accept retry.
                expected = _identity(auth)
                _require(all(receipt['preview'][k] == v for k, v in expected.items()), 'SDC_SUPPORT_IDENTITY_CHANGED')
                _require(snapshot['enabled'], 'SDC_SUPPORT_EXPIRED')
                # Existing receipt is reused, including a previously persisted unknown accept.
                if status['client']['accept'] is not None and status['client']['accept']['ack'] is not None:
                    _require(int(clock() * 1000) < status['client']['accept']['ack']['expires_at'], 'SDC_SUPPORT_EXPIRED')
                    return {'state': 'accepted', 'expires_at': status['client']['accept']['ack']['expires_at']}
            budget()
            request = local.begin_accept(store, expected_owner_generation=snapshot['owner_generation'], binding_supplier=current,
                                         now_ms=int(clock() * 1000))
            response = _send(ACCEPT, request, auth, transport_factory, clock, budget)
            # Matching ACK is evidence even if the owner stopped while this request was in flight.
            result = local.record_accept_ack(store, request, response)
            budget()
            return {'state': 'accepted' if result['enabled'] and int(clock() * 1000) < min(
                        result['expires_at'], auth.get('expires_at', result['expires_at'])) else 'ack_recorded_inactive',
                    'expires_at': result['expires_at']}
    finally:
        auth.clear()


def status(store, *, now_ms):
    """Project local records only, without Keychain, network or current central authorization."""
    value = local.client_status(store, now_ms=now_ms)
    receipt, accept = value['receipt'], value['client']['accept']
    active = value['runner']['active']
    operation = None
    if active is not None:
        report = json_body(active['report']) if active['report'] is not None else None
        operation = {'operation_id': active['operation']['operation_id'], 'phase': active['phase'],
                     'report_id': report['report_id'] if report else None, 'report_state': report['state'] if report else None}
    return {'enabled': bool(value['enabled'] and receipt and receipt['accepted_at'] <= now_ms < min(
                receipt['preview']['consent_expires_at'], local._profile_expiry(receipt['binding']))),
            'case_id': receipt['preview']['case_id'] if receipt else None,
            'consent_expires_at': receipt['preview']['consent_expires_at'] if receipt else None,
            'accept': 'accepted' if accept and accept['ack'] else 'unknown' if accept else 'not_sent',
            'accept_expires_at': accept['ack']['expires_at'] if accept and accept['ack'] else None,
            'operation': operation, 'operation_receipts': len(value['runner']['terminal'])}


def stop(store, *, now_ms):
    """Disable admissions first, then independently observe foreground session ownership."""
    local.disable(store, now_ms=now_ms)
    try:
        with store.support_session_lock():
            return {'state': 'stopped', 'foreground_ended': True}
    except BlockingIOError:
        return {'state': 'stopped', 'foreground_ended': False}


# Grant lifetime is a duration measured from BEFORE its ID and durable intent.
# Server issued_at floors its captured request time; reserve the whole 1000ms.
GRANT_FLOOR_RESERVE_MS = 1000
# This is a conservative fail-closed local jump detector, not measured clock
# accuracy or a proof of synchronization to the server's clock.
WALL_MONOTONIC_DIVERGENCE_MS = 100


def _receipt_identity(receipt, auth):
    expected = _identity(auth)
    _require(receipt is not None and all(receipt['preview'][k] == v for k, v in expected.items()), 'SDC_SUPPORT_IDENTITY_CHANGED')


def _clock_sample(clock, monotonic):
    # Wall is observed somewhere inside this monotonic interval. A preempted
    # process has uncertainty, not evidence that the wall clock jumped.
    before, wall, after = monotonic(), clock() * 1000, monotonic()
    _require(after >= before and 0 <= wall <= 2**53 - 1, 'SDC_SUPPORT_EXPIRED')
    return before, wall, after


def _grant_clock(clock, monotonic, anchor, grant=None):
    before, wall_now, after = _clock_sample(clock, monotonic)
    mono_start, wall_start, mono_end = anchor
    low, elapsed = (before - mono_end) * 1000, (after - mono_start) * 1000
    wall_elapsed = wall_now - wall_start
    _require(before >= mono_end and wall_elapsed >= 0
             and low - WALL_MONOTONIC_DIVERGENCE_MS <= wall_elapsed <= elapsed + WALL_MONOTONIC_DIVERGENCE_MS,
             'SDC_SUPPORT_EXPIRED')
    if grant is not None:
        # The earliest possible start and latest possible current sample make
        # preemption consume the budget, never extend it.
        _require(grant['not_before'] <= wall_now < grant['expires_at']
                 and elapsed < grant['expires_at'] - grant['issued_at'] - GRANT_FLOOR_RESERVE_MS, 'SDC_SUPPORT_EXPIRED')


def _reconcile(store, status, auth, current, transport_factory, clock, budget):
    from .support_control import STATUS, validate_response
    active = status['runner']['active']
    request = local.runner_status(store, expected_owner_generation=status['owner_generation'], binding_supplier=current,
                                  now_ms=int(clock() * 1000))
    response = _send(STATUS, request, auth, transport_factory, clock, budget)
    response = validate_response(STATUS, response, request=json_body(request))
    if active['phase'] == 'ADMITTING':
        if (active['grant'] is None and active['report'] is None and response['report'] is None
                and response['operation']['state'] in ('canceled', 'expired') and response['operation']['state_revision'] == 0):
            local.runner_pregrant_terminal(store, request, response, expected_owner_generation=status['owner_generation'],
                                           binding_supplier=current, now_ms=int(clock() * 1000))
            return {'state': 'pregrant_terminal', 'terminal_state': response['operation']['state']}
        return {'state': 'orphan_hold'}
    report = active['report']
    if response['report'] is not None and report is not None:
        body = json_body(report)
        _require(response['report']['body_hash'] == report['body_hash'] and response['report']['state'] == body['state']
                 and response['report']['state_revision'] == body['state_revision'])
        local.runner_ack(store, report, state_code=body['state'], ack_request_id=response['request_id'], ack_source='status')
        return {'state': 'report_reconciled', 'report_state': body['state']}
    # report:null/primary operation state are not proof of nonacceptance or a
    # permission to rerun; keep every unresolved receipt and raw report intact.
    return {'state': 'result_unknown'}


def json_body(record):
    from .support_contract import parse_canonical
    return parse_canonical(record['raw'], max_bytes=16384)


def run_once(store, *, verifier, check_pins, transport_factory, provider,
             clock=time.time, monotonic=time.monotonic, budget=lambda: None):
    """Own one foreground session and perform exactly one bounded support pass."""
    with store.support_session_lock():
        return _run_pass(store, verifier=verifier, check_pins=check_pins, transport_factory=transport_factory,
                         provider=provider, clock=clock, monotonic=monotonic, budget=budget)


def _run_pass(store, *, verifier, check_pins, transport_factory, provider,
             clock=time.time, monotonic=time.monotonic, budget=lambda: None, expected_consent_owner=None):
    """Poll once and run one fixed diagnosis only with consent, approval and a live grant.

    Provider(diagnostic_id, *, generated_at_ms) returns a closed diagnostic DTO.
    It must be a fixed locally injected collector: no shell/file/URL/LLM/restart or
    patient data is accepted from the operation. STARTED is permanent execution
    evidence: a later process only reconciles metadata and never executes it again.
    Caller owns the whole foreground deadline, including prompt/HTTP/crypto/provider.
    """
    from .support_control import POLL, ADMIT, GRANT, REPORT, validate_request, validate_response
    from .support_authorization import match_operation, match_grant
    _require(callable(provider), 'SDC_SUPPORT_NOT_PROVISIONED')
    auth = {}
    current = _supplier(store, check_pins, clock, auth, purposes=('operation', 'execution-grant'))
    try:
        budget()
        snapshot = local.prepare(store, binding_supplier=current, now_ms=int(clock() * 1000))
        _require(expected_consent_owner is None or snapshot['owner_generation'] == expected_consent_owner, 'SDC_SUPPORT_IDENTITY_CHANGED')
        saved = local.client_status(store, now_ms=int(clock() * 1000))
        _require(snapshot['owner_generation'] == saved['owner_generation'] and saved['enabled'], 'SDC_SUPPORT_IDENTITY_CHANGED')
        _receipt_identity(saved['receipt'], auth)
        if saved['runner']['active'] is not None:
            return _reconcile(store, saved, auth, current, transport_factory, clock, budget)
        _require(snapshot['enabled'], 'SDC_SUPPORT_EXPIRED')
        poll = local.runner_poll(store, expected_owner_generation=snapshot['owner_generation'], binding_supplier=current,
                                 now_ms=int(clock() * 1000))
        response = _send(POLL, poll, auth, transport_factory, clock, budget)
        budget()
        response = validate_response(POLL, response, request=json_body(poll))
        if response['operation'] is None:
            local.runner_empty(store, expected_owner_generation=snapshot['owner_generation'], binding_supplier=current,
                               now_ms=int(clock() * 1000))
            return {'state': 'empty'}
        operation = verifier(canonical(response['operation'], max_bytes=16384).encode('ascii'), purpose='operation',
                             pins=auth['pins'], now_ms=int(clock() * 1000))
        budget()
        operation = match_operation(operation, response['approval'], consent_preview=saved['receipt']['preview'], now_ms=int(clock() * 1000))
        alive = uuid.uuid4().hex  # Live continuation only, separate from local consent CAS.
        active = local.runner_admit(store, operation, response['approval'], alive_owner=alive,
                                    expected_owner_generation=snapshot['owner_generation'], binding_supplier=current, now_ms=int(clock() * 1000))
        admission = _send(ADMIT, active['admit'], auth, transport_factory, clock, budget)
        budget()
        validate_response(ADMIT, admission, request=json_body(active['admit']))
        # This baseline is captured BEFORE first grant UUID creation and fsync.
        # It is never reset after HTTP, verification, same-ID reads or waits.
        anchor = _clock_sample(clock, monotonic)
        _grant_clock(clock, monotonic, anchor)
        active = local.runner_grant(store, alive_owner=alive, expected_owner_generation=snapshot['owner_generation'],
                                    binding_supplier=current, now_ms=int(clock() * 1000))
        _grant_clock(clock, monotonic, anchor)
        response = _send(GRANT, active['grant'], auth, transport_factory, clock, budget)
        budget()
        response = validate_response(GRANT, response, request=json_body(active['grant']))
        if response['grant'] is None:
            return {'state': 'grant_hold'}
        _require(response['state'] == 'grant_consumed')
        grant = verifier(canonical(response['grant'], max_bytes=16384).encode('ascii'), purpose='execution-grant',
                         pins=auth['pins'], now_ms=int(clock() * 1000))
        budget()
        admit, grant_request = json_body(active['admit']), json_body(active['grant'])
        grant = match_grant(grant, operation, active['approval'],
                            grant_request_id=grant_request['grant_request_id'], receipt_id=admit['receipt_id'],
                            admission_id=admit['admission_id'], owner_generation=alive, now_ms=int(clock() * 1000))
        # Recheck immutable approval and all current wall windows before STARTED.
        match_operation(operation, active['approval'], consent_preview=saved['receipt']['preview'], now_ms=int(clock() * 1000))
        def execution_deadline():
            _profile_budget(auth, clock, budget)
            _grant_clock(clock, monotonic, anchor, grant)
        local.runner_started(store, alive_owner=alive, expected_owner_generation=snapshot['owner_generation'],
                             binding_supplier=current, now_ms=int(clock() * 1000), check_deadline=execution_deadline)
        result, pending_error = None, None
        generated_at = int(clock() * 1000)
        try:
            local.runner_execution(store, alive_owner=alive, expected_owner_generation=snapshot['owner_generation'],
                                   binding_supplier=current, now_ms=int(clock() * 1000), check_deadline=execution_deadline)
            execution_deadline()
            result = provider(operation['params']['diagnostic_id'], generated_at_ms=generated_at)
            execution_deadline()
            fresh = local.prepare(store, binding_supplier=current, now_ms=int(clock() * 1000))
            _require(fresh['owner_generation'] == snapshot['owner_generation'] and fresh['enabled'], 'SDC_SUPPORT_IDENTITY_CHANGED')
            execution_deadline()
            _require(type(result) is dict and result.get('diagnostic_id') == operation['params']['diagnostic_id']
                     and result.get('generated_at') == generated_at)
        except BaseException as error:
            result, pending_error = None, error
        try:
            report = {'schema_version': 'sdc.support.v1', 'report_id': str(uuid.uuid4()), 'operation_id': operation['operation_id'],
                      'payload_hash': operation['payload_hash'], 'receipt_id': admit['receipt_id'], 'state_revision': 1,
                      'state': 'succeeded' if result is not None else 'unknown', 'observed_at': generated_at if result is not None else int(clock() * 1000),
                      'result': result}
            try:
                report = validate_request(REPORT, report)
            except ValueError:
                # Malformed provider output is never serialized to durable state or wire.
                report.update(state='unknown', result=None, observed_at=int(clock() * 1000))
                report = validate_request(REPORT, report)
                if not isinstance(pending_error, TimeoutError):
                    pending_error = SupportClientError()
            active = local.runner_result(store, report, alive_owner=alive)
        except BaseException:
            if isinstance(pending_error, TimeoutError):
                raise pending_error from None
            raise
        if pending_error is not None and (isinstance(pending_error, TimeoutError) or not isinstance(pending_error, Exception)):
            raise pending_error
        budget()
        report_request = local.runner_report(store, alive_owner=alive, expected_owner_generation=snapshot['owner_generation'],
                                             binding_supplier=current, now_ms=int(clock() * 1000))
        try:
            ack = _send(REPORT, report_request, auth, transport_factory, clock, budget)
            ack = validate_response(REPORT, ack, request=json_body(report_request))
        except TimeoutError:
            raise
        except Exception:
            budget()
            saved = local.client_status(store, now_ms=int(clock() * 1000))
            _require(saved['owner_generation'] == snapshot['owner_generation'], 'SDC_SUPPORT_IDENTITY_CHANGED')
            return _reconcile(store, saved, auth, current, transport_factory, clock, budget)
        local.runner_ack(store, report_request, state_code=ack['state'], ack_request_id=ack['request_id'], ack_source='report')
        budget()
        return {'state': 'report_accepted', 'report_state': report['state']}
    finally:
        auth.clear()


class _WatchExpired(BaseException):
    pass


def watch(store, *, verifier, check_pins, transport_factory, provider, interval=60,
          clock=time.time, monotonic=time.monotonic, wait=time.sleep, budget=lambda: None,
          limit_deadline=lambda expires_at_ms: None):
    """Own one foreground session for <=300s; repeat only after definite pass completion.

    interval is a fixed idle polling period, never a completion SLA. The caller's
    limit_deadline callback may only shorten its parent alarm to the original
    accepted-consent expiry. No owner/receipt switch, new scheduler or thread occurs.
    """
    _require(type(interval) is int and interval in (30, 60))
    _require(callable(provider), 'SDC_SUPPORT_NOT_PROVISIONED')
    started = monotonic()
    passes, last = 0, None
    with store.support_session_lock():
        budget()
        saved = local.client_status(store, now_ms=int(clock() * 1000))
        receipt, accept = saved['receipt'], saved['client']['accept']
        _require(saved['enabled'] and receipt is not None and accept is not None and accept['ack'] is not None)
        owner = saved['owner_generation']
        expiry = min(receipt['preview']['consent_expires_at'], accept['ack']['expires_at'])
        # Never adopt a later owner or renewed consent while this watch is alive.
        deadline = min(started + 300, monotonic() + max(0, expiry / 1000 - clock()))
        limit_deadline(expiry)

        def watch_budget():
            budget()
            now = monotonic()
            _require(now >= started, 'SDC_SUPPORT_EXPIRED')
            if now >= deadline or int(clock() * 1000) >= expiry:
                raise _WatchExpired()

        def stopped():
            current = local.client_status(store, now_ms=int(clock() * 1000))
            return (not current['enabled'] or current['owner_generation'] != owner
                    or current['receipt'] != receipt)

        try:
            while True:
                watch_budget()
                if stopped():
                    return {'state': 'watch_finished', 'reason': 'stopped_or_owner_changed', 'passes': passes}
                outcome = _run_pass(store, verifier=verifier, check_pins=check_pins, transport_factory=transport_factory,
                                    provider=provider, clock=clock, monotonic=monotonic, budget=watch_budget, expected_consent_owner=owner)
                passes += 1
                last = outcome['state']
                if last not in ('empty', 'report_accepted', 'report_reconciled', 'pregrant_terminal'):
                    return {**outcome, 'passes': passes}
                # Completion starts a new idle period. Delayed work never catches up.
                due = monotonic() + interval
                while True:
                    watch_budget()
                    if stopped():
                        return {'state': 'watch_finished', 'reason': 'stopped_or_owner_changed', 'passes': passes}
                    remaining = due - monotonic()
                    if remaining <= 0:
                        break
                    wait(min(1, remaining, max(0, deadline - monotonic())))
        except _WatchExpired:
            return {'state': 'watch_finished', 'reason': 'deadline', 'passes': passes, 'last': last}
