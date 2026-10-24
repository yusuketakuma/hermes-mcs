"""既存tickの残期限で診断を1通信だけ進め、未知の送信を先に照合します。"""
import math
import hashlib
import re
import secrets
import time

from . import diagnostic_state as local
from . import provision
from .connector import Connector
from .protocol import endpoint, sign_diagnostic
from .support_contract import canonical
from .transport import DiagnosticTransport, TransportError


class DiagnosticSenderError(ValueError):
    def __init__(self, code='UNAVAILABLE'):
        if code not in ('UNAVAILABLE', 'AUTHORITY_UNAVAILABLE', 'PROVIDER_UNAVAILABLE', 'RESPONSE_INVALID'):
            code = 'UNAVAILABLE'
        self.code = 'SDC_DIAGNOSTIC_' + code
        super().__init__(self.code)


def _require(ok):
    if not ok:
        raise DiagnosticSenderError('AUTHORITY_UNAVAILABLE')


def _hash(value):
    return hashlib.sha256(canonical(value, max_bytes=16384).encode('ascii')).hexdigest()


def managed_binding(store, *, now_ms):
    """Bind genuine support metadata to current local R1 identity, without keys or central consent."""
    try:
        snapshot = provision.profile_snapshot(store, profile='support', now_ms=now_ms)
        _require(type(snapshot) is dict and set(snapshot) == {
            'bundle', 'root_fingerprint', 'monitor_endpoint_hash', 'bundle_hash', 'local_receipt_id'})
        bundle = snapshot['bundle']
        _require(bundle['profile_id'] == 'support' and snapshot['bundle_hash'] == _hash(bundle))
        target = endpoint({k: bundle[k] for k in ('origin', 'audience', 'environment')})
        monitor = endpoint(store.read('endpoint.json'))
        enrollment = store.enrollment_binding()
        _require(snapshot['root_fingerprint'] == enrollment['root_fingerprint'] == store.root_fingerprint()
                 and snapshot['monitor_endpoint_hash'] == enrollment['endpoint_hash'] == _hash(monitor)
                 and target['environment'] == monitor['environment'])
        state = store.read('state.json')
        _require(type(state) is dict and state.get('state') == 'active' and state.get('enabled') is True
                 and not any(state.get(k) for k in ('disconnected', 'central_suspended', 'enrollment_hold'))
                 and 'rotation_id' not in state and all(state.get(k) == bundle[k] for k in ('claim_id', 'device_id')))
        return local.validate_binding({
            'root_fingerprint': snapshot['root_fingerprint'], 'monitor_endpoint_hash': snapshot['monitor_endpoint_hash'],
            'support_endpoint_hash': _hash(target), 'support_bundle_hash': snapshot['bundle_hash'],
            **{k: bundle[k] for k in ('root_id', 'tenant', 'installation', 'realm_epoch', 'device_epoch', 'device_id',
                                     'claim_id', 'environment', 'audience')}, 'key_id': state['key_id']})
    except TimeoutError:
        raise
    except Exception:
        raise DiagnosticSenderError('AUTHORITY_UNAVAILABLE') from None


def run_managed_once(store, *, provider, deadline, clock=time.time, monotonic=time.monotonic,
                     transport_factory=DiagnosticTransport):
    """Borrow the tick's Store/deadline and send one diagnostic action using its genuine support target.

    The caller owns tick.lock, the original deadline alarm and the Store's bounded
    credential reader. No R4a consent or pin-signature verifier is consulted here.
    Each current check reacquires the actual credential, then rechecks metadata;
    only that same RAM authentication signs the exact admitted bytes.
    """
    auth = {}

    def current():
        auth.clear()
        sender._budget(deadline)
        before = managed_binding(store, now_ms=sender._now())
        snapshot = provision.profile_snapshot(store, profile='support', now_ms=sender._now())
        bundle = snapshot['bundle']
        target = dict(endpoint({k: bundle[k] for k in ('origin', 'audience', 'environment')}))
        _require(snapshot['bundle_hash'] == before['support_bundle_hash'] and _hash(target) == before['support_endpoint_hash'])
        sender._budget(deadline)
        credential = store.read('credential')
        _require(type(credential) is dict and not any(k in credential for k in ('next_key', 'next_key_id', 'rotation_id')))
        identity = {k: before[k] for k in ('claim_id', 'device_id', 'key_id')}
        key = Connector(store, None).secret(identity, credential)
        _require(type(key) is str and re.fullmatch('[0-9a-f]{64}', key) is not None)
        sender._budget(deadline)
        _require(managed_binding(store, now_ms=sender._now()) == before)
        sender._budget(deadline)
        auth.update(binding=before, endpoint=target, identity=identity, key=key)
        return before

    def send(path, raw):
        send.refusal_status = None
        _require(path in (local.STATUS, local.REPORT) and type(raw) is bytes)
        sender._budget(deadline)
        headers = sign_diagnostic(auth['endpoint'], path, raw, auth['identity'], auth['key'],
                                  now=clock(), nonce=secrets.token_hex(16))
        transport = transport_factory(dict(auth['endpoint']))
        sender._budget(deadline)
        store.guard_credential()  # Cached auth never sends past an unfinished credential change.
        try:
            return transport.send(path, raw, headers)
        finally:
            send.refusal_status = getattr(transport, 'refusal_status', None)

    send.refusal_status = None
    sender = DiagnosticSender(store, current=current, send=send, provider=provider, clock=clock, monotonic=monotonic)
    try:
        return sender.tick(deadline=deadline)
    finally:
        auth.clear()


class DiagnosticSender:
    """Borrow a locked Store; injected owners alone acquire credentials and send HTTP.

    current() returns independently checked target binding, never a secret.
    send(path, raw) signs these exact bytes and owns bounded transport cleanup.
    Its optional refusal_status preserves an observed 4xx across cancellation.
    provider(previous_marks=..., since_ms=..., now_ms=...) is a fixed, bounded
    passive projection, called only after a current STATUS in this invocation,
    with no pending batch; historical IDs in that STATUS confer no authority.
    No persisted observation can restore the private sampling token.
    """
    def __init__(self, store, *, current, send, provider, clock=time.time, monotonic=time.monotonic):
        self.store, self.current, self.send, self.provider = store, current, send, provider
        self.clock, self.monotonic = clock, monotonic
        self._busy = False

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= (2**53 - 1) / 1000:
            raise DiagnosticSenderError()
        milliseconds = int(now * 1000)
        if milliseconds > 2**53 - 1:
            raise DiagnosticSenderError()
        return milliseconds

    def _budget(self, deadline):
        now = self.monotonic()
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise DiagnosticSenderError()
        if type(now) not in (int, float) or not math.isfinite(now) or now >= deadline:
            raise TimeoutError('SDC_DIAGNOSTIC_DEADLINE')

    def _current(self, deadline):
        self._budget(deadline)
        try:
            value = local.validate_binding(self.current())
        except TimeoutError:
            raise
        except Exception:
            raise DiagnosticSenderError('AUTHORITY_UNAVAILABLE') from None
        self._budget(deadline)
        return value

    def _failure(self, request, error, *, dispatched):
        observed = getattr(self.send, 'refusal_status', None) if dispatched else None
        reported = error.status if isinstance(error, TransportError) else None
        refusals = [v for v in (observed, reported) if type(v) is int and 400 <= v < 500]
        # Any actual terminal refusal wins over a separately observed rate limit.
        status = next((v for v in refusals if v != 429), None)
        if status is None:
            status = 429 if refusals else reported if type(reported) is int else None
        retry = error.retry_after if isinstance(error, TransportError) else 0
        retry = retry if type(retry) is int and 0 <= retry <= 86400 else 0
        local.record_failure(self.store, request, now_ms=self._now(), status=status, retry_after=retry)

    def tick(self, *, deadline):
        """Advance one durable action with at most one diagnostic HTTP; never renew a deadline."""
        if self._busy:
            raise DiagnosticSenderError()
        self._busy = True
        try:
            return self._tick(deadline)
        finally:
            self._busy = False

    def _tick(self, deadline):
        self._budget(deadline)
        before = local.status(self.store, now_ms=self._now())
        if not before['enabled']:
            return before
        current = self._current(deadline)
        request = local.admit(self.store, current=current, now_ms=self._now())
        if request is None:
            return local.status(self.store, now_ms=self._now())
        dispatched = False
        try:
            current = self._current(deadline)
            if not local.check_dispatch(self.store, request, current=current, now_ms=self._now()):
                return local.status(self.store, now_ms=self._now())
            self._budget(deadline)
            dispatched = True
            response = self.send(request['path'], request['raw'])
        except BaseException as error:
            try:
                self._failure(request, error, dispatched=dispatched)
            except BaseException:
                self.store._diagnostic_uncertain = True
                if isinstance(error, TimeoutError) or not isinstance(error, Exception):
                    raise error from None
                raise
            if isinstance(error, TransportError):
                return local.status(self.store, now_ms=self._now())
            if isinstance(error, (TimeoutError, KeyboardInterrupt, SystemExit)) or not isinstance(error, Exception):
                raise
            raise DiagnosticSenderError() from None
        # A received ACK remains evidence even if the deadline/current supplier fails.
        # No collection token is issued without the post-response current check.
        try:
            current = self._current(deadline)
        except BaseException as error:
            try:
                local.record_response(self.store, request, response, current=None, now_ms=self._now())
            except BaseException:
                self.store._diagnostic_uncertain = True
                raise error from None
            raise
        try:
            permit = local.record_response(self.store, request, response, current=current, now_ms=self._now())
        except TimeoutError:
            raise
        except (ValueError, TypeError, KeyError, OSError):
            raise DiagnosticSenderError('RESPONSE_INVALID') from None
        if permit is not None:
            self._budget(deadline)
            # Recheck current authority after saving the STATUS and before touching sources.
            current = self._current(deadline)
            context = local.sample_context(self.store, permit, current=current, now_ms=self._now())
            try:
                sample = self.provider(**context)
            except TimeoutError:
                raise
            except Exception:
                raise DiagnosticSenderError('PROVIDER_UNAVAILABLE') from None
            current = self._current(deadline)
            local.collect(self.store, permit, sample, current=current, now_ms=self._now())
        self._budget(deadline)
        return local.status(self.store, now_ms=self._now())
