"""Consume one live restart grant before latching the existing Runtime drain."""
import asyncio
import functools
import hashlib
import math
import secrets
import time

from adapters.sdc import restart_contract as contract
from adapters.sdc import restart_control as control
from adapters.sdc import restart_execution as execution
from adapters.sdc import restart_state as consent
from adapters.sdc import provision, support_client
from adapters.sdc.protocol import endpoint, sign_restart
from adapters.sdc.support_contract import canonical, parse_canonical
from sdc_support_signatures import validate_restart_pins, verify_restart_envelope
from .restart_credential import CredentialCarrier


class RestartOwnerError(ValueError):
    """Expose fixed execution failures without authority or response contents."""
    def __init__(self, code='INVALID'):
        allowed = {'INVALID', 'EXPIRED', 'STOPPED', 'AUTHORITY_CHANGED', 'BUSY'}
        self.code = 'SDC_RESTART_EXECUTION_' + (code if code in allowed else 'INVALID')
        super().__init__(self.code)


def _require(value, code='INVALID'):
    if not value:
        raise RestartOwnerError(code)


def _copy(value, maximum=16384):
    return parse_canonical(canonical(value, max_bytes=maximum), max_bytes=maximum)


def _hash(value):
    return hashlib.sha256(canonical(value, max_bytes=16384).encode('ascii')).hexdigest()


class _Owner:
    def __init__(self, runtime, store, supplier, deadline, verifier, clock, monotonic):
        self.runtime, self.store, self.supplier = runtime, store, supplier
        self.deadline, self.verifier, self.clock, self.monotonic = deadline, verifier, clock, monotonic
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        self.closed = asyncio.Event()
        self.stopping = False
        self.handle = None
        self.alive = None
        self.expected = self.binding = self.anchor = None
        self.auth = None
        self.credential = None
        self.final_grant = None

    def cancel(self):
        self.stopping = True
        if self.task is not asyncio.current_task():
            self.task.cancel()

    async def wait_closed(self):
        cancelled = False
        while not self.closed.is_set():
            try:
                await self.closed.wait()
            except asyncio.CancelledError:
                cancelled = True
                self.cancel()
        if cancelled:
            raise asyncio.CancelledError

    def now(self):
        value = self.clock() * 1000
        _require(type(value) in (int, float) and math.isfinite(value)
                 and 0 <= value <= 2**53 - 1, 'EXPIRED')
        return int(value)

    def check(self):
        if self.task.cancelling():
            raise asyncio.CancelledError
        _require(not self.stopping, 'STOPPED')
        _require(self.loop.time() < self.deadline, 'EXPIRED')
        self.runtime._restart_execution_check(self, self.handle)
        if self.binding is not None and 'profile' in self.binding:
            _require(self.now() < self.binding['profile']['expires_at'], 'EXPIRED')
        if self.final_grant is not None:
            self._grant_time(self.final_grant)

    async def prepare_credential(self):
        self.check()
        marker = self.store.read('credential-store.json')
        if marker is not None:
            self.credential = CredentialCarrier(check=self.check, deadline=self.deadline)
            await self.credential.prepare(marker['account'])
        self.check()

    async def close_credential(self):
        if self.credential is None:
            return

        async def retire_credential():
            while True:
                try:
                    await self.credential.close()
                except asyncio.CancelledError:
                    self.stopping = True
                except Exception:
                    self.stopping = True
                if self.credential.closed:
                    return
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    self.stopping = True

        closing = self.loop.create_task(retire_credential())
        cancelled = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                cancelled, self.stopping = True, True
        closing.result()
        if cancelled:
            raise asyncio.CancelledError

    def current(self, *, terminal_read=False):
        self.check()
        reading, credential = True, None
        reads = 0

        def read_credential():
            nonlocal reads, credential
            _require(reading and reads == 0, 'AUTHORITY_CHANGED')
            self.check()
            reads += 1
            native = (functools.partial(self.credential.copy_current, terminal=terminal_read)
                      if self.credential is not None else None)
            credential = _copy(self.store.read_current_credential(copy_current=native))
            self.check()
            return _copy(credential)

        try:
            value = self.supplier(read_credential=read_credential)
        finally:
            reading = False
        _require(reads == 1 and type(credential) is dict
                 and not any(k in credential for k in ('next_key', 'next_key_id', 'rotation_id')),
                 'AUTHORITY_CHANGED')
        _require(type(value) is dict and set(value) == {'expected', 'registration', 'endpoint', 'pins', 'identity',
                                'key', 'binding', 'allowed'} and value['allowed'] is True,
                 'AUTHORITY_CHANGED')
        # Authority joins several bounded records; it is not a larger wire DTO.
        value = {key: _copy(item) for key, item in value.items()}
        expected, identity, key = value['expected'], value['identity'], value['key']
        _require(type(expected) is dict and set(expected) == set(contract.EXPECTED_FIELDS)
                 and type(identity) is dict and set(identity) == {'claim_id', 'device_id', 'key_id'}
                 and type(key) is str and len(key) == 64, 'AUTHORITY_CHANGED')
        _require(key == credential.get('key') and credential.get('key_id') == identity['key_id'],
                 'AUTHORITY_CHANGED')
        endpoint(value['endpoint'])
        _require(all(identity[k] == expected[k] for k in identity)
                 and all(value['endpoint'][k] == expected[k] for k in ('audience', 'environment')),
                 'AUTHORITY_CHANGED')
        now = self.now()
        for purpose in ('restart-operation', 'restart-execution-grant'):
            validate_restart_pins(value['pins'], purpose=purpose, now_ms=now)
        contract.validate_restart_record('registration', value['registration'], now_ms=now)
        binding = consent._binding(value['binding'])
        checked_binding = {**identity, 'root_fingerprint': self.store.root_fingerprint(),
                           'monitor_endpoint_hash': binding['monitor_endpoint_hash'],
                           'support_endpoint_hash': _hash(value['endpoint']),
                           'pins_sha256': _hash(value['pins']),
                           'secret_sha256': hashlib.sha256(bytes.fromhex(key)).hexdigest()}
        monitor = provision.monitor_endpoint(self.store)
        if monitor is None:
            _require('profile' not in binding, 'AUTHORITY_CHANGED')
        else:
            # Both reads are keyless and do not reacquire tick.lock. The saved
            # profile, not the supplier's binding, supplies this comparison.
            snapshot = provision.profile_snapshot(self.store, profile='restart', now_ms=now)
            bundle = snapshot['bundle']
            _require(bundle['profile_id'] == 'restart'
                     and all(bundle[k] == expected[k] for k in support_client._PROFILE_SCOPE)
                     and value['endpoint'] == {k: bundle[k] for k in ('origin', 'audience', 'environment')}
                     and value['pins'] == bundle['pins']
                     and snapshot['root_fingerprint'] == checked_binding['root_fingerprint']
                     and snapshot['monitor_endpoint_hash'] == _hash(monitor), 'AUTHORITY_CHANGED')
            checked_binding['monitor_endpoint_hash'] = snapshot['monitor_endpoint_hash']
            checked_binding['profile'] = support_client._profile_binding(snapshot, now_ms=now)
        _require(binding == checked_binding, 'AUTHORITY_CHANGED')
        _require(self.expected is None or self.expected == expected, 'AUTHORITY_CHANGED')
        _require(self.binding is None or self.binding == binding, 'AUTHORITY_CHANGED')
        if 'profile' in checked_binding:
            _require(now <= self.now() < checked_binding['profile']['expires_at'], 'EXPIRED')
        self.expected, self.binding, self.auth = expected, binding, value
        self.check()
        return binding

    def preflight(self):
        self.check()
        return {'schema_version': 'sdc.support.restart-preflight.v1', 'product': 'hermes-mcs',
                **{k: self.expected[k] for k in ('tenant', 'installation', 'device_id', 'environment', 'case_id')},
                'service_id': 'mcs.standalone', 'runtime_generation': self.runtime.generation,
                'started_at': int(self.runtime.started_at * 1000), 'observed_at': self.now(),
                'owner_alive': True, 'update_in_progress': False,
                'command_intake_in_progress': False, 'restarting': False}

    def sample(self):
        before, wall, after = self.monotonic(), self.clock() * 1000, self.monotonic()
        _require(all(type(v) in (int, float) and math.isfinite(v) for v in (before, wall, after))
                 and after >= before and 0 <= wall <= 2**53 - 1, 'EXPIRED')
        return before, wall, after

    def grant_clock(self, grant=None):
        self.check()
        return self._grant_time(grant)

    def _grant_time(self, grant=None):
        before, wall, after = self.sample()
        start, origin, end = self.anchor
        low, elapsed, wall_elapsed = (before - end) * 1000, (after - start) * 1000, wall - origin
        _require(before >= end and wall_elapsed >= 0 and low - 100 <= wall_elapsed <= elapsed + 100,
                 'EXPIRED')
        if self.binding is not None and 'profile' in self.binding:
            _require(wall < self.binding['profile']['expires_at'], 'EXPIRED')
        if grant is not None:
            _require(grant['not_before'] <= wall < grant['expires_at']
                     and elapsed < grant['expires_at'] - grant['issued_at'] - 1000, 'EXPIRED')
        return int(wall)

    async def verify(self, envelope, purpose):
        self.current()
        call = functools.partial(self.verifier, canonical(envelope, max_bytes=16384).encode('ascii'),
                                 purpose=purpose, pins=self.auth['pins'], expected=self.expected,
                                 now_ms=self.now())
        # Never cancel this Future: its synchronous verifier owns OpenSSL through real reap.
        future = self.loop.run_in_executor(None, call)
        cancelled = False
        try:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    cancelled = True
                    self.stopping = True
            result = future.result()
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        if cancelled:
            raise asyncio.CancelledError
        self.current()
        return contract.validate_typed_payload(purpose, result, now_ms=self.now())

    async def send(self, path, record):
        self.current()
        if self.anchor is not None:
            self.grant_clock()
        raw = record['raw'].encode('ascii')
        _require(hashlib.sha256(raw).hexdigest() == record['body_hash'])
        request = control.validate_request(path, raw, now_ms=self.now())
        headers = sign_restart(self.auth['endpoint'], path, raw, self.auth['identity'], self.auth['key'],
                               now=self.clock(), nonce=secrets.token_hex(16))
        response = await self.runtime.restart_exchange(self.handle, self.auth['endpoint'], path, raw,
                                                      headers, deadline=self.deadline)
        self.current()
        return control.validate_response(path, response, request=request, now_ms=self.now())

    async def run(self, operation_envelope, approval, acceptance):
        self.current()
        snapshot = consent.prepare(self.store, binding_supplier=self.current, now_ms=self.now())
        _require(snapshot['enabled'] and snapshot['accept']['ack'] is not None)
        consent.require_primary_acceptance(self.store, acceptance, now_ms=self.now())
        operation = await self.verify(operation_envelope, 'restart-operation')
        joined = dict(operation=operation, approval=approval, preview=snapshot['preview'],
                      local_receipt=snapshot['receipt'], acceptance=acceptance, expected=self.expected)

        def authorize(grant=None, admission=None, *, terminal_read=False):
            self.current(terminal_read=terminal_read)
            contract.validate_restart_authorization(**joined, registration=self.auth['registration'],
                preflight=self.preflight(), now_ms=self.now(), grant=grant, admission=admission)

        authorize()
        self.alive, request = execution.begin_admit(self.store, operation=operation, approval=approval,
            expected=self.expected, expected_owner_generation=snapshot['owner_generation'],
            binding_supplier=self.current, registration=self.auth['registration'],
            preflight=self.preflight(), now_ms=self.now())
        admission_body = parse_canonical(request['raw'], max_bytes=16384)
        admission = {k: admission_body[k] for k in ('receipt_id', 'admission_id', 'owner_generation')}
        response = await self.send(control.ADMIT, request)
        execution.record_admit_ack(self.store, self.alive, request, response, now_ms=self.now())
        authorize()
        # Saving/generating the first grant request consumes this original anchor too.
        self.anchor = self.sample()
        request = execution.begin_grant(self.store, self.alive, binding_supplier=self.current,
            registration=self.auth['registration'], preflight=self.preflight(), now_ms=self.now())
        response = await self.send(control.GRANT, request)
        grant = await self.verify(response['grant'], 'restart-execution-grant')
        authorize(grant, admission)
        # Every native-read poll consumes the same original grant anchor.
        self.final_grant = grant

        def final_check(*, terminal_read=False):
            authorize(grant, admission, terminal_read=terminal_read)
            return self.grant_clock(grant)

        def start(start_receipt_id):
            now = final_check(terminal_read=True)
            return self.runtime._restart_support_start(self, self.handle,
                operation_id=operation['operation_id'], receipt_id=admission['receipt_id'],
                start_receipt_id=start_receipt_id, started_at=now)

        final_check()
        return execution.spend_and_start(self.store, self.alive, grant=grant, binding_supplier=self.current,
            registration=self.auth['registration'], preflight=self.preflight(), now_ms=self.now(),
            final_check=final_check, start=start)


async def execute(runtime, store, *, operation_envelope, approval, acceptance,
                  authority_supplier, deadline, verifier=None, clock=time.time, monotonic=time.monotonic):
    """Borrow one Store through SPENT/latch; saved state never creates a live continuation.

    The trusted supplier must call its read_credential callback once, synchronously,
    without HTTP or prompts, to join current expected15/registration and authority;
    initial registration discovery and automatic invocation remain unconnected.
    """
    runtime._reservation_owner()
    _require(runtime._restart_execution_owner is None, 'BUSY')
    _require(type(deadline) in (int, float) and math.isfinite(deadline)
             and asyncio.get_running_loop().time() < deadline, 'EXPIRED')
    owner = _Owner(runtime, store, authority_supplier, deadline,
                   verifier or verify_restart_envelope, clock, monotonic)
    try:
        operation_envelope, approval, acceptance = (_copy(operation_envelope), _copy(approval), _copy(acceptance))
        owner.handle = runtime.reserve_restart()
        runtime._restart_execution_owner = owner
        await owner.prepare_credential()
        return await owner.run(operation_envelope, approval, acceptance)
    except asyncio.CancelledError:
        raise
    except RestartOwnerError:
        raise
    except Exception:
        raise RestartOwnerError() from None
    finally:
        try:
            await owner.close_credential()
        finally:
            try:
                if owner.alive is not None:
                    execution.retire(store, owner.alive)
            finally:
                if runtime._restart_execution_owner is owner:
                    runtime._restart_execution_owner = None
                try:
                    if owner.handle is not None and runtime._support_restart is None:
                        runtime.release_restart(owner.handle)
                finally:
                    owner.auth = owner.expected = owner.binding = owner.final_grant = None
                    owner.closed.set()
