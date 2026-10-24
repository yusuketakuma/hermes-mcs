"""同じ生存ownerだけの再起動入場・一度の消費・開始証拠を私有領域に保存する。"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import uuid

from . import restart_contract as contract
from . import restart_control as control
from . import restart_state as consent
from .storage import RESTART_EXECUTION_MAX_BYTES
from .support_contract import canonical

NAME = 'restart-execution.json'
SCHEMA = 'sdc.support.restart-execution-state.v1'


class RestartExecutionError(ValueError):
    """Expose fixed errors without the request, credentials or arbitrary callback text."""
    def __init__(self, code='INVALID'):
        allowed = {'INVALID', 'MISSING', 'CONFLICT', 'OWNER', 'UNCERTAIN', 'EXPIRED'}
        super().__init__('SUPPORT_RESTART_EXECUTION_' + (code if code in allowed else 'INVALID'))


def _check(ok, code='INVALID'):
    if not ok:
        raise RestartExecutionError(code)


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False))


def _hash(value):
    return hashlib.sha256(canonical(value, max_bytes=16384).encode('ascii')).hexdigest()


def _uuid(value):
    try:
        identifier = uuid.UUID(value) if type(value) is str else None
        return identifier is not None and identifier.version == 4 and str(identifier) == value
    except ValueError:
        return False


def _wire(path, body, now_ms):
    body = control.validate_request(path, body, now_ms=now_ms)
    raw = canonical(body, max_bytes=16384)
    return {'raw': raw, 'body_hash': hashlib.sha256(raw.encode('ascii')).hexdigest(), 'created_at': now_ms}


def _body(path, wire):
    _check(type(wire) is dict and set(wire) == {'raw', 'body_hash', 'created_at'} and consent._integer(wire['created_at']))
    body = control.validate_request(path, wire['raw'], now_ms=wire['created_at'])
    _check(wire['body_hash'] == _hash(body))
    return body


def _request(wire):
    return {k: wire[k] for k in ('raw', 'body_hash')}


def _validated(state):
    try:
        fields = {'schema_version', 'root_fingerprint', 'consent_owner', 'binding', 'operation', 'approval',
                  'verified_at', 'acceptance_hash', 'phase', 'admit', 'admit_ack', 'grant_request',
                  'start_receipt_id', 'spent', 'started_at', 'status_request', 'report', 'report_admitted', 'report_ack'}
        _check(type(state) is dict and set(state) == fields and state['schema_version'] == SCHEMA)
        _check(type(state['report_admitted']) is bool and consent._hex(state['root_fingerprint']) and consent._hex(state['consent_owner'], 32)
               and consent._hex(state['acceptance_hash']) and consent._integer(state['verified_at'])
               and state['phase'] in ('ADMITTING', 'ADMITTED', 'GRANT_REQUESTED', 'SPENT') and _uuid(state['start_receipt_id']))
        binding = consent._binding(state['binding'])
        _check(binding['root_fingerprint'] == state['root_fingerprint'])
        op = contract.validate_typed_payload('restart-operation', state['operation'], now_ms=state['verified_at'])
        approval = contract.validate_restart_approval(state['approval'], now_ms=state['verified_at'])
        _check(approval['approval_hash'] == contract.hash_restart_approval(approval))
        _check(all(canonical(op[k]) == canonical(approval[k]) for k in (*contract.EXPECTED_FIELDS, 'product',
               'capability_revision', 'consent_revision', 'approval_id', 'approval_hash', 'intent_hash', 'approved_by')))
        _check(all(binding[k] == op[k] for k in ('claim_id', 'device_id', 'key_id')))
        admission = _body(control.ADMIT, state['admit'])
        _check(admission['operation_id'] == op['operation_id'] and admission['payload_hash'] == op['payload_hash'])
        if state['admit_ack'] is not None:
            control.validate_response(control.ADMIT, state['admit_ack'], request=admission, now_ms=state['admit']['created_at'])
        _check((state['phase'] == 'ADMITTING') == (state['admit_ack'] is None))
        if state['grant_request'] is not None:
            request = _body(control.GRANT, state['grant_request'])
            _check(all(request[k] == admission[k] for k in ('operation_id', 'payload_hash', 'receipt_id', 'admission_id')))
        _check((state['phase'] in ('GRANT_REQUESTED', 'SPENT')) == (state['grant_request'] is not None))
        spent = state['spent']
        _check((state['phase'] == 'SPENT') == (spent is not None))
        if spent is not None:
            _check(type(spent) is dict and set(spent) == {'grant_id', 'grant_hash', 'spent_at'}
                   and _uuid(spent['grant_id']) and consent._hex(spent['grant_hash']) and consent._integer(spent['spent_at']))
        _check(state['started_at'] is None or (spent is not None and consent._integer(state['started_at'])
                                              and spent['spent_at'] <= state['started_at']))
        status = _body(control.STATUS, state['status_request'])
        _check(all(status[k] == admission[k] for k in ('operation_id', 'payload_hash', 'receipt_id')))
        if state['report'] is not None:
            report = _body(control.REPORT, state['report'])
            _check(spent is not None and all(report[k] == status[k] for k in ('operation_id', 'payload_hash', 'receipt_id', 'report_id'))
                   and report['state'] == 'unknown' and report['state_revision'] == 1 and report['result'] is None
                   and report['observed_at'] == state['started_at'])
        _check(not state['report_admitted'] or state['report'] is not None)
        ack = state['report_ack']
        if ack is not None:
            _check(state['report_admitted'] and state['report'] is not None and type(ack) is dict
                   and set(ack) == {'source', 'request_id', 'body_hash', 'state', 'state_revision'}
                   and ack['source'] in ('report', 'status') and _uuid(ack['request_id']))
            report = _body(control.REPORT, state['report'])
            _check(ack['body_hash'] == state['report']['body_hash'] and ack['state'] == report['state']
                   and ack['state_revision'] == report['state_revision'] and type(ack['state_revision']) is int)
        _check(len(json.dumps(state, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')) <= RESTART_EXECUTION_MAX_BYTES)
        return _copy(state)
    except RestartExecutionError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise RestartExecutionError() from None


def _read(store):
    try:
        root = store.root_fingerprint()
        state = store.read(NAME)
        _check(state is not None, 'MISSING')
        state = _validated(state)
        _check(state['root_fingerprint'] == root, 'CONFLICT')
        return state
    except RestartExecutionError:
        raise
    except (OSError, ValueError, TypeError):
        raise RestartExecutionError() from None


class _Alive:
    """Opaque RAM-only capability; no loader, pickle or string can create a continuation."""
    __slots__ = ('store', 'fd', 'root', 'thread', 'owner', 'phase', 'record_hash')

    def __init__(self, store, root, owner):
        self.store, self.fd, self.root = store, store.fd, root
        self.thread, self.owner = threading.get_ident(), owner
        self.phase, self.record_hash = 'ADMITTING', None

    def __reduce__(self):
        raise TypeError('SUPPORT_RESTART_EXECUTION_OWNER')


def _certain(store):
    _check(not getattr(store, '_restart_execution_uncertain', False), 'UNCERTAIN')
    consent._certain(store)


def _owner(store, alive, phase):
    _check(type(alive) is _Alive and alive.store is store and alive.fd == store.fd
           and alive.thread == threading.get_ident() and alive.root == store.root_fingerprint()
           and alive.phase == phase, 'OWNER')
    _certain(store)
    state = _read(store)
    _check(state['phase'] == phase and _hash_record(state) == alive.record_hash
           and _body(control.ADMIT, state['admit'])['owner_generation'] == alive.owner, 'OWNER')
    return state


def _hash_record(state):
    return hashlib.sha256(json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()


def _write(store, state, alive=None):
    _certain(store)
    state = _validated(state)
    try:
        _check(store.root_fingerprint() == state['root_fingerprint'], 'CONFLICT')
        store.write(NAME, state)
        _check(store.root_fingerprint() == state['root_fingerprint'], 'CONFLICT')
    except BaseException as error:
        store._restart_execution_uncertain = True
        if alive is not None:
            alive.phase = 'UNCERTAIN'
        if not isinstance(error, Exception):
            raise
        raise RestartExecutionError('UNCERTAIN') from None
    if alive is not None:
        alive.record_hash = _hash_record(state)


def _consent(store, state, supplier, now_ms, *, historical=False):
    # Caller owns restart-state.lock then tick.lock; do not reenter public helpers.
    _certain(store)
    current = consent._read(store)
    _check(not current['disabled'] and current['receipt'] is not None and current['accept']['ack'] is not None, 'CONFLICT')
    binding = consent._current(supplier)
    _check(binding == current['binding'], 'CONFLICT')
    receipt, ack = current['receipt'], current['accept']['ack']
    _check(consent._integer(now_ms) and receipt['confirmed_at'] <= now_ms, 'EXPIRED')
    if not historical:
        _check(now_ms < min(receipt['expires_at'], ack['expires_at']), 'EXPIRED')
    if state is not None:
        _check(current['owner_generation'] == state['consent_owner'] and binding == state['binding']
               and _hash(ack) == state['acceptance_hash'], 'CONFLICT')
    return current


def _authorize(current, state, registration, preflight, now_ms, grant=None):
    admission = _body(control.ADMIT, state['admit'])
    return contract.validate_restart_authorization(operation=state['operation'], approval=state['approval'],
        preview=current['preview'], local_receipt=current['receipt'], acceptance=current['accept']['ack'],
        registration=registration, preflight=preflight, expected=current['expected'], now_ms=now_ms,
        grant=grant, admission={k: admission[k] for k in ('receipt_id', 'admission_id', 'owner_generation')})


def begin_admit(store, *, operation, approval, expected, expected_owner_generation, binding_supplier,
                registration, preflight, now_ms):
    """Create one durable admission and RAM owner; caller verifies signatures/current authority.

    Existing/corrupt evidence and a missing record already claimed by this Store
    are never resumed. External deletion/rollback cannot be detected after reopen.
    The borrowed Store must remain open until the caller and its owned I/O finish.
    """
    with store.restart_state_lock(), store.lock():
        _check(not getattr(store, '_restart_execution_claimed', False), 'CONFLICT')
        current = _consent(store, None, binding_supplier, now_ms)
        _check(current['owner_generation'] == expected_owner_generation and canonical(expected) == canonical(current['expected']), 'CONFLICT')
        checked = contract.validate_restart_authorization(operation=operation, approval=approval,
            preview=current['preview'], local_receipt=current['receipt'], acceptance=current['accept']['ack'],
            registration=registration, preflight=preflight, expected=current['expected'], now_ms=now_ms)
        operation, approval = checked['operation'], checked['approval']
        owner = secrets.token_hex(16)
        ids = {k: str(uuid.uuid4()) for k in ('receipt_id', 'admission_id', 'report_id', 'status_id')}
        base = {'schema_version': 'sdc.support.restart-control.v1', 'operation_id': operation['operation_id'],
                'payload_hash': operation['payload_hash'], 'receipt_id': ids['receipt_id']}
        state = {'schema_version': SCHEMA, 'root_fingerprint': current['root_fingerprint'],
                 'consent_owner': current['owner_generation'], 'binding': current['binding'], 'operation': operation,
                 'approval': approval, 'verified_at': now_ms, 'acceptance_hash': _hash(current['accept']['ack']),
                 'phase': 'ADMITTING', 'admit': _wire(control.ADMIT, {**base, 'admission_id': ids['admission_id'], 'owner_generation': owner}, now_ms),
                 'admit_ack': None, 'grant_request': None, 'start_receipt_id': str(uuid.uuid4()), 'spent': None, 'started_at': None,
                 'status_request': _wire(control.STATUS, {**base, 'status_id': ids['status_id'], 'report_id': ids['report_id']}, now_ms),
                 'report': None, 'report_admitted': False, 'report_ack': None}
        alive = _Alive(store, current['root_fingerprint'], owner)
        # O_EXCL leaves a hold record even if the first atomic write fails before replace.
        store._restart_execution_claimed = True
        try:
            fd = store._open(NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(store.fd)
            _write(store, state, alive)
        except FileExistsError:
            raise RestartExecutionError('CONFLICT') from None
        except BaseException as error:
            store._restart_execution_uncertain = True
            alive.phase = 'UNCERTAIN'
            if not isinstance(error, Exception):
                raise
            raise RestartExecutionError('UNCERTAIN') from None
        return alive, _request(state['admit'])


def record_admit_ack(store, alive, request, response, now_ms):
    """Keep an exact admitted ACK only in the originating continuation, without executing."""
    with store.restart_state_lock():
        state = _owner(store, alive, 'ADMITTING')
        _check(_request(state['admit']) == request, 'CONFLICT')
        response = control.validate_response(control.ADMIT, response, request=_body(control.ADMIT, state['admit']), now_ms=now_ms)
        state.update(admit_ack=response, phase='ADMITTED')
        _write(store, state, alive)
        alive.phase = 'ADMITTED'


def begin_grant(store, alive, *, binding_supplier, registration, preflight, now_ms):
    """Persist the original grant request; caller must capture its clock anchor before this call."""
    with store.restart_state_lock(), store.lock():
        state = _owner(store, alive, 'ADMITTED')
        current = _consent(store, state, binding_supplier, now_ms)
        _authorize(current, state, registration, preflight, now_ms)
        admission = _body(control.ADMIT, state['admit'])
        body = {k: admission[k] for k in ('schema_version', 'operation_id', 'payload_hash', 'receipt_id', 'admission_id')}
        body.update(grant_request_id=str(uuid.uuid4()), preflight=preflight)
        state.update(phase='GRANT_REQUESTED', grant_request=_wire(control.GRANT, body, now_ms))
        _write(store, state, alive)
        alive.phase = 'GRANT_REQUESTED'
        return _request(state['grant_request'])


def spend_and_start(store, alive, *, grant, binding_supplier, registration, preflight, now_ms, final_check, start):
    """Spend once, fsync, recheck synchronously, then invoke start(start_receipt_id).

    grant is RAM input only. final_check returns a fresh integer wall time after
    checking original monotonic budget, current authority and the actual Runtime.
    start repeats owner/clock checks and returns the observed integer start time.
    Both callbacks are bounded synchronous code: no await, HTTP or human prompt.
    SPENT/unknown never proves drain, process exit, successor or readiness.
    """
    _check(callable(final_check) and callable(start))
    with store.restart_state_lock(), store.lock():
        state = _owner(store, alive, 'GRANT_REQUESTED')
        alive.phase = 'SPENT'  # Validation failure cannot reopen the one final attempt either.
        current = _consent(store, state, binding_supplier, now_ms)
        checked = _authorize(current, state, registration, preflight, now_ms, grant)
        grant = checked['grant']
        request = _body(control.GRANT, state['grant_request'])
        _check(grant['grant_request_id'] == request['grant_request_id'], 'CONFLICT')
        state.update(phase='SPENT', spent={'grant_id': grant['grant_id'], 'grant_hash': _hash(grant), 'spent_at': now_ms})
        _write(store, state, alive)
        current = _consent(store, state, binding_supplier, now_ms)
        try:
            fresh_now = final_check()
        except Exception:
            raise RestartExecutionError('CONFLICT') from None
        _check(consent._integer(fresh_now) and fresh_now >= now_ms, 'EXPIRED')
        _authorize(current, state, registration, preflight, fresh_now, grant)
        try:
            started_at = start(state['start_receipt_id'])
        except Exception:
            raise RestartExecutionError('CONFLICT') from None
        _check(consent._integer(started_at) and fresh_now <= started_at
               and grant['not_before'] <= started_at < grant['expires_at'], 'EXPIRED')
        state['started_at'] = started_at
        status_body = _body(control.STATUS, state['status_request'])
        report = {k: status_body[k] for k in ('schema_version', 'report_id', 'operation_id', 'payload_hash', 'receipt_id')}
        # This continuation has emitted no report; central revision starts at zero.
        report.update(state_revision=1, state='unknown', observed_at=started_at, result=None)
        state['report'] = _wire(control.REPORT, report, started_at)
        _write(store, state, alive)
        return _projection(store, state)


def retire(store, alive):
    """Revoke a borrowed RAM capability after owned I/O settles; do not rewrite durable evidence."""
    _check(type(alive) is _Alive and alive.store is store and alive.thread == threading.get_ident(), 'OWNER')
    alive.phase = 'CLOSED'


def _projection(store, state):
    admit = _body(control.ADMIT, state['admit'])
    status = _body(control.STATUS, state['status_request'])
    return {'state': 'unknown', 'phase': state['phase'],
            **{k: admit[k] for k in ('operation_id', 'payload_hash', 'receipt_id', 'admission_id')},
            'grant_id': state['spent']['grant_id'] if state['spent'] else None,
            'start_receipt_id': state['start_receipt_id'], 'started_at': state['started_at'], 'report_id': status['report_id'],
            'report_prepared': state['report'] is not None, 'report_accepted': state['report_ack'] is not None,
            'uncertain': bool(getattr(store, '_restart_execution_uncertain', False))}


def status(store):
    """Read local metadata only; no saved phase or owner string restores execution authority."""
    with store.restart_state_lock():
        return _projection(store, _read(store))


def metadata_request(store, path, *, binding_supplier, now_ms):
    """Admit only fixed REPORT/STATUS bytes with current local binding; caller owns central authorization."""
    _check(path in (control.REPORT, control.STATUS), 'CONFLICT')
    with store.restart_state_lock(), store.lock():
        _certain(store)
        state = _read(store)
        _consent(store, state, binding_supplier, now_ms, historical=True)
        wire = state['report'] if path == control.REPORT else state['status_request']
        _check(wire is not None and (path != control.REPORT or not state['report_admitted']), 'CONFLICT')
        control.validate_request(path, wire['raw'], now_ms=now_ms)
        if path == control.REPORT:
            state['report_admitted'] = True
        _write(store, state)
        return _request(wire)


def record_metadata_ack(store, path, request, response, *, now_ms):
    """Record only exact report acceptance evidence; null status remains unknown."""
    _check(path in (control.REPORT, control.STATUS), 'CONFLICT')
    with store.restart_state_lock():
        _certain(store)
        state = _read(store)
        wire = state['report'] if path == control.REPORT else state['status_request']
        _check(wire is not None and request == _request(wire)
               and (path != control.REPORT or state['report_admitted']), 'CONFLICT')
        response = control.validate_response(path, response, request=_body(path, wire), now_ms=now_ms)
        evidence = response if path == control.REPORT else response['report']
        if evidence is not None and state['report'] is not None:
            _check(state['report_admitted'], 'CONFLICT')
            report = _body(control.REPORT, state['report'])
            _check(evidence['state'] == report['state'] and evidence['state_revision'] == report['state_revision']
                   and (path == control.REPORT or evidence['body_hash'] == state['report']['body_hash']), 'CONFLICT')
            if state['report_ack'] is None:
                state['report_ack'] = {'source': 'report' if path == control.REPORT else 'status',
                    'request_id': response['request_id'], 'body_hash': state['report']['body_hash'],
                    'state': report['state'], 'state_revision': report['state_revision']}
                _write(store, state)
        return _projection(store, state)
