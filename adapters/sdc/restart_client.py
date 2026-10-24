"""本人が起動した再起動準備と変更同意を、固定登録・実owner観測・acceptへ結ぶ。"""
import hashlib
import secrets
import time

from . import restart_contract as contract
from . import restart_control as control
from . import restart_planning as planning
from . import restart_state as change
from . import support_client as support
from . import provision as managed_provision
from .protocol import endpoint, sign_restart
from .support_contract import canonical
from .transport import TransportError


class RestartClientError(ValueError):
    def __init__(self, code='UNAVAILABLE'):
        allowed = {'UNAVAILABLE', 'NOT_PROVISIONED', 'EXPIRED', 'READ_HELD', 'CONFLICT'}
        super().__init__('SDC_RESTART_' + (code if code in allowed else 'UNAVAILABLE'))


def _require(ok, code='CONFLICT'):
    if not ok:
        raise RestartClientError(code)


def _now(clock):
    value = clock() * 1000
    _require(type(value) in (int, float) and 0 <= value <= 2**53 - 1, 'EXPIRED')
    return int(value)


def _supplier(store, provision, check_pins, check_restart_pins, clock, auth):
    """Read a separately authenticated restart provision; preserve the original R4a pins."""
    _require(callable(provision), 'NOT_PROVISIONED')
    parent_auth = {}
    parent_current = support._supplier(store, check_pins, clock, parent_auth, purposes=('consent-preview',))

    def current():
        try:
            value = provision()
            _require(type(value) is dict)
            managed = set(value) == {'bundle', 'root_fingerprint', 'monitor_endpoint_hash', 'bundle_hash', 'local_receipt_id'}
            if managed:
                _require(value == managed_provision.profile_snapshot(store, profile='restart', now_ms=_now(clock)))
                bundle = value['bundle']
                target = dict(endpoint({k: bundle[k] for k in ('origin', 'audience', 'environment')}))
                pins = bundle['pins']
            else:
                _require(set(value) == {'root_fingerprint', 'monitor_endpoint_hash', 'support_endpoint', 'pins'})
                target, pins = dict(endpoint(value['support_endpoint'])), value['pins']
            monitor = store.enrollment_binding()
            _require(value['root_fingerprint'] == monitor['root_fingerprint'] and value['monitor_endpoint_hash'] == monitor['endpoint_hash'])
            check_restart_pins(pins, purpose='restart-consent-preview', now_ms=_now(clock))
            parent_binding = parent_current()
            _require(managed == ('profile' in parent_binding))
            if managed:
                _require(bundle['profile_id'] == 'restart' and all(bundle[k] == parent_auth['scope'][k]
                    for k in support._PROFILE_SCOPE if k != 'audience'))
            else:
                _require(all(target[k] == parent_auth['endpoint'][k] for k in ('audience', 'environment')))
            binding = {**parent_binding, 'support_endpoint_hash': support._hash(target), 'pins_sha256': support._hash(pins),
                       **parent_auth['identity']}
            auth.clear()
            auth.update(endpoint=target, pins=planning._copy(pins), identity=parent_auth['identity'], key=parent_auth['key'],
                        guard=store.guard_credential)
            if managed:
                binding['profile'] = support._profile_binding(value, now_ms=_now(clock))
                auth['expires_at'] = min(parent_auth['expires_at'], binding['profile']['expires_at'])
            return {'parent_binding': parent_binding, 'binding': change._binding(binding)}
        except TimeoutError:
            raise
        except Exception:
            raise RestartClientError() from None
        finally:
            parent_auth.clear()
    return current


def _send(path, record, auth, transport_factory, clock, budget):
    support._profile_budget(auth, clock, budget)
    raw = record['raw'].encode('ascii')
    _require(hashlib.sha256(raw).hexdigest() == record['body_hash'])
    control.validate_request(path, raw, now_ms=_now(clock), allow_historical=path == control.ACCEPT)
    headers = sign_restart(auth['endpoint'], path, raw, auth['identity'], auth['key'], now=clock(), nonce=secrets.token_hex(16))
    auth['guard']()  # Cached auth never sends past an unfinished credential change.
    return transport_factory(auth['endpoint']).send(path, raw, headers)


def initialize_local_state(store):
    """明示provisionの同じ許可窓内で、変更同意と準備の初期状態を保存する。"""
    change.initialize(store)
    planning.initialize(store)


def _guard(clock, monotonic, budget, limit_deadline):
    end = monotonic() + 300
    expiry = _now(clock) + 300000
    if callable(getattr(budget, 'expires_at', None)):
        expiry = min(expiry, budget.expires_at())

    def check(wait_seconds=0):
        budget()
        _require(monotonic() + wait_seconds < end and (expiry is None or _now(clock) + wait_seconds * 1000 < expiry), 'EXPIRED')

    def shorten(value):
        nonlocal expiry
        expiry = value if expiry is None else min(expiry, value)
        if limit_deadline is not None:
            limit_deadline(expiry)
        check()
    check.expires_at = lambda: expiry
    return check, shorten


def _retry_locked(action, guard, pause):
    """Retry local lock acquisition only, after the operation has released its locks."""
    while True:
        guard()
        try:
            return action()
        except BlockingIOError:
            guard(0.05)
            pause(0.05)


def _snapshot(store, current, clock, guard, shorten, pause):
    frame = _retry_locked(lambda: planning.snapshot(store, binding_supplier=current, now_ms=_now(clock)), guard, pause)
    shorten(min(frame['state']['plan']['expires_at'] if frame['state']['plan'] else frame['parent']['expires_at'],
                planning.support._profile_expiry(frame['binding'])))
    return frame


def _wait(store, phase, current, clock, guard, shorten, pause, *, fresh=False):
    while True:
        frame = _retry_locked(lambda: planning.local_snapshot(store, now_ms=_now(clock)), guard, pause)
        shorten(frame['state']['plan']['expires_at'])
        sample = frame['state']['plan']['samples'][phase]
        _require(sample is not None)
        _require(_now(clock) < sample['expires_at'], 'EXPIRED')
        if sample['preflight'] is not None:
            if fresh:
                planning._preflight(frame['state']['plan'], sample['preflight'], _now(clock))
            return _snapshot(store, current, clock, guard, shorten, pause)
        pause(0.1)


def _read_registration(store, current, auth, transport_factory, clock, guard, pause):
    for _ in range(4):
        guard()
        request = planning.read_attempt(store, binding_supplier=current, now_ms=_now(clock))
        try:
            response = _send(control.REGISTRATION_READ, request, auth, transport_factory, clock, guard)
        except TransportError as error:
            if type(error.status) is not int or not 500 <= error.status <= 599 or error.code == 'SDC_REDIRECT_DENIED':
                _retry_locked(lambda: planning.block_read(store, request), guard, pause)
                raise
            if error.retry_after:
                # The caller's alarm and the same monotonic/consent budget cover this wait.
                guard(error.retry_after)
                pause(error.retry_after)
                guard()
            continue
        guard()
        try:
            checked = control.validate_response(control.REGISTRATION_READ, response,
                request=planning._body(control.REGISTRATION_READ, request), now_ms=_now(clock))
        except ValueError:
            reg = response.get('registration') if type(response) is dict else None
            original = planning._body(control.REGISTRATION_READ, request)
            wrong_id = type(response) is dict and any(k in response and response[k] != original[k] for k in ('case_id', 'local_receipt_id'))
            terminal = wrong_id or (type(reg) is dict and (reg.get('state') == 'revoked'
                or type(reg.get('expires_at')) is int and reg['expires_at'] <= _now(clock)
                or type(reg.get('not_before')) is int and reg['not_before'] > _now(clock)))
            if terminal:
                _retry_locked(lambda: planning.block_read(store, request), guard, pause)
                raise RestartClientError('READ_HELD') from None
            continue
        try:
            _retry_locked(lambda: planning.record_registration(store, request, checked, binding_supplier=current, now_ms=_now(clock)), guard, pause)
        except ValueError:
            # No authorization, scope or persistence error is a network retry.
            _retry_locked(lambda: planning.block_read(store, request), guard, pause)
            raise
        return
    raise RestartClientError('READ_HELD')


def plan(store, case_id, *, restart_provision, check_pins, check_restart_pins, transport_factory, confirm,
         clock=time.time, monotonic=time.monotonic, pause=time.sleep, budget=lambda: None, limit_deadline=None):
    """Obtain separate read/sample and inspection consent; never initialize or execute."""
    _require(callable(restart_provision), 'NOT_PROVISIONED')
    _require(support._uuid(case_id))
    auth = {}
    current = _supplier(store, restart_provision, check_pins, check_restart_pins, clock, auth)
    guard, shorten = _guard(clock, monotonic, budget, limit_deadline)
    try:
        with store.support_session_lock():
            frame = _snapshot(store, current, clock, guard, shorten, pause)
            _require(frame['parent']['expected']['case_id'] == case_id)
            if frame['state']['plan'] is None:
                if confirm('read', frame['parent'], dict(auth['endpoint'])) is not True:
                    return {'state': 'cancelled'}
                guard()
                planning.begin(store, frame, binding_supplier=current, now_ms=_now(clock), confirm_human=True)
            frame = _snapshot(store, current, clock, guard, shorten, pause)
            p = frame['state']['plan']
            if p['inspection_ack'] is not None:
                return {'state': 'planned'}
            if p['inspection'] is not None:
                return {'state': 'inspection_unknown'}
            if p['registration'] is None:
                _read_registration(store, current, auth, transport_factory, clock, guard, pause)
            frame = _snapshot(store, current, clock, guard, shorten, pause)
            if frame['state']['plan']['samples']['sample'] is None:
                planning.request_sample(store, 'sample', binding_supplier=current, now_ms=_now(clock), expires_at=guard.expires_at())
            frame = _wait(store, 'sample', current, clock, guard, shorten, pause)
            p = frame['state']['plan']
            if p['inspection_consent'] is None:
                if confirm('inspection', {'registration': p['registration']['value'], 'preflight': p['samples']['sample']['preflight'],
                                          'expires_at': p['expires_at']}, dict(auth['endpoint'])) is not True:
                    planning.disable(store)
                    return {'state': 'cancelled'}
                guard()
                planning.confirm_inspection(store, binding_supplier=current, now_ms=_now(clock), confirm_human=True)
            frame = _snapshot(store, current, clock, guard, shorten, pause)
            p = frame['state']['plan']
            if _now(clock) - p['samples']['sample']['preflight']['observed_at'] > 15000:
                if p['samples']['refresh'] is None:
                    planning.request_sample(store, 'refresh', binding_supplier=current, now_ms=_now(clock), expires_at=guard.expires_at())
                _wait(store, 'refresh', current, clock, guard, shorten, pause, fresh=True)
            request = planning.begin_inspection(store, binding_supplier=current, now_ms=_now(clock))
            response = _send(control.INSPECTION, request, auth, transport_factory, clock, guard)
            _retry_locked(lambda: planning.record_inspection(store, request, response, binding_supplier=current, now_ms=_now(clock)), guard, pause)
            _snapshot(store, current, clock, guard, shorten, pause)
            return {'state': 'planned'}
    except BaseException as error:
        if not isinstance(error, Exception) or str(error) == 'SDC_RESTART_EXPIRED':
            try:
                planning.disable(store)
            except Exception:
                pass  # Preserve the original cancellation; no success is returned.
        raise
    finally:
        auth.clear()


def consent(store, operation_id, *, restart_provision, verifier, check_pins, check_restart_pins, transport_factory, confirm,
            clock=time.time, monotonic=time.monotonic, pause=time.sleep, budget=lambda: None, limit_deadline=None):
    """Verify the fixed preview against independent expected15, prompt, witness G, then accept."""
    _require(callable(restart_provision), 'NOT_PROVISIONED')
    _require(support._uuid(operation_id))
    auth = {}
    current = _supplier(store, restart_provision, check_pins, check_restart_pins, clock, auth)
    guard, shorten = _guard(clock, monotonic, budget, limit_deadline)
    try:
        with store.support_session_lock():
            frame = _snapshot(store, current, clock, guard, shorten, pause)
            p = frame['state']['plan']
            _require(p is not None and p['inspection_ack'] is not None)
            if p['verified_preview'] is None:
                if p['preview'] is not None:
                    return {'state': 'preview_unknown'}
                request = planning.begin_preview(store, operation_id, binding_supplier=current, now_ms=_now(clock))
                response = _send(control.PREVIEW, request, auth, transport_factory, clock, guard)
                response = control.validate_response(control.PREVIEW, response, request=planning._body(control.PREVIEW, request), now_ms=_now(clock))
                frame = _snapshot(store, current, clock, guard, shorten, pause)
                verified = verifier(canonical(response['preview'], max_bytes=16384).encode('ascii'), purpose='restart-consent-preview',
                                    pins=auth['pins'], expected=planning._expected(frame['state']['plan']), now_ms=_now(clock))
                guard()
                _retry_locked(lambda: planning.record_preview(store, request, verified, response['registration'],
                    binding_supplier=current, now_ms=_now(clock)), guard, pause)
                frame = _snapshot(store, current, clock, guard, shorten, pause)
                p = frame['state']['plan']
            _require(p['verified_preview']['operation_id'] == operation_id)
            if frame['restart']['receipt'] is None:
                preview = p['verified_preview']
                contract.validate_typed_payload('restart-consent-preview', preview, now_ms=_now(clock))
                shorten(preview['expires_at'])
                if confirm('change', preview, dict(auth['endpoint'])) is not True:
                    planning.disable(store)
                    return {'state': 'cancelled'}
                guard()
                if p['samples']['confirm'] is None:
                    planning.request_sample(store, 'confirm', binding_supplier=current, now_ms=_now(clock), expires_at=guard.expires_at())
                frame = _wait(store, 'confirm', current, clock, guard, shorten, pause, fresh=True)

                def final_binding():
                    guard()
                    latest = planning._access(store, current, _now(clock))
                    plan = latest['state']['plan']
                    _require(plan['verified_preview'] == preview)
                    contract.validate_typed_payload('restart-consent-preview', preview, now_ms=_now(clock))
                    planning._preflight(plan, plan['samples']['confirm']['preflight'], _now(clock))
                    return latest['binding']

                with store.support_state_lock():
                    change.confirm_consent(store, preview, expected=planning._expected(frame['state']['plan']),
                        expected_owner_generation=frame['restart']['owner_generation'], expected_binding=frame['binding'],
                        binding_supplier=final_binding, now_ms=_now(clock), confirm_human=True)
                frame = _snapshot(store, current, clock, guard, shorten, pause)
            if frame['restart']['accept']['ack'] is not None:
                return {'state': 'accepted'}

            def accept_binding():
                guard()
                return planning._access(store, current, _now(clock))['binding']

            with store.support_state_lock():
                request = change.begin_accept(store, expected_owner_generation=frame['restart']['owner_generation'],
                    binding_supplier=accept_binding, now_ms=_now(clock))
            response = _send(control.ACCEPT, request, auth, transport_factory, clock, guard)
            _retry_locked(lambda: change.record_accept_ack(store, request, response, now_ms=_now(clock)), guard, pause)
            try:
                _snapshot(store, current, clock, guard, shorten, pause)
            except ValueError:
                return {'state': 'ack_recorded_inactive'}
            return {'state': 'accepted'}
    except BaseException as error:
        if not isinstance(error, Exception) or str(error) == 'SDC_RESTART_EXPIRED':
            try:
                planning.disable(store)
            except Exception:
                pass
        raise
    finally:
        auth.clear()


def status(store, *, now_ms):
    result = {}
    for label, name, read in (('planning', planning.NAME, lambda: planning.status(store)),
                              ('change', change.NAME, lambda: change.status(store, now_ms=now_ms))):
        if store.read(name) is None:
            result[label] = {'state': 'missing'}
        else:
            value = read()
            result[label] = value if label == 'planning' else {'disabled': value['disabled'], 'enabled': value['enabled'],
                'accepted': bool(value['accept'] and value['accept']['ack']), 'uncertain': value['uncertain']}
    return result


def stop_children(store):
    """Call only after parent disable; missing child state stays missing and cannot admit."""
    failed = False
    first_timeout = None
    for name, disable in ((planning.NAME, planning.disable), (change.NAME, change.disable)):
        try:
            if store.read(name) is not None:
                disable(store)
        except TimeoutError as error:
            if first_timeout is None:
                first_timeout = error
        except Exception:
            failed = True
        except BaseException:
            if first_timeout is None:
                raise
            # A second cancellation cannot replace the first deadline failure.
    if first_timeout is not None:
        raise first_timeout
    _require(not failed)
