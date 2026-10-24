"""Own normal Keychain operations through fixed private child pipes and actual reap."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid

from .keychain import KeychainError, _CODES
from .protocol import strict_json
from . import credential_mutation as mutation

PYTHON = str(Path(sys.executable).absolute())
ENTRY = str(Path(__file__).resolve().parents[2] / 'mcs' / 'ops' / 'sdc_keychain.py')
MAX_RAW = 4096
MAX_FRAME = 8192
MAX_SEQUENCE = 2**32 - 1
READ_BUDGET_S = 5.0  # Adoption bound, not a calibrated native/OS completion guarantee.
POLL_S = 0.005
TERM_GRACE_S = 2.0
KEYCHAIN_CODES = _CODES


class KeychainReadError(KeychainError):
    """Expose fixed reader failures without account, raw values or dependency errors."""
    def __init__(self, code='INVALID'):
        allowed = {'INVALID', 'OWNER_REQUIRED', 'BUSY', 'CLOSED', 'FAILED', 'DEADLINE',
                   'PROTOCOL', 'CHILD', 'SPAWN', 'CHECK_FAILED'}
        self.code = 'SDC_KEYCHAIN_READ_' + (code if code in allowed else 'INVALID')
        ValueError.__init__(self, self.code)


def account_valid(account):
    try:
        value = uuid.UUID(account) if type(account) is str else None
        return value is not None and value.version == 4 and str(value) == account
    except ValueError:
        return False


def encode_frame(value):
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                     allow_nan=False).encode('ascii')
    if not 1 <= len(raw) <= MAX_FRAME:
        raise KeychainReadError('PROTOCOL')
    return len(raw).to_bytes(4, 'big') + raw


def decode_payload(raw):
    value = strict_json(raw)
    if type(value) is not dict or encode_frame(value)[4:] != raw:
        raise KeychainReadError('PROTOCOL')
    return value


def credential_raw(raw):
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_RAW:
        raise KeychainReadError('PROTOCOL')
    mutation.credential_value(raw)
    return raw


def read_request(value):
    if (type(value) is not dict or set(value) != {'kind', 'account', 'sequence', 'nonce'}
            or value['kind'] != 'read' or not account_valid(value['account'])
            or type(value['sequence']) is not int or not 1 <= value['sequence'] <= MAX_SEQUENCE
            or type(value['nonce']) is not str or re.fullmatch('[0-9a-f]{32}', value['nonce']) is None):
        raise KeychainReadError('PROTOCOL')
    return value


def mutation_request(value):
    fields = {'kind', 'account', 'sequence', 'nonce', 'deadline', 'intent_id', 'marker_sha256', 'raw'}
    if type(value) is not dict or set(value) != fields or value['kind'] not in ('write', 'delete'):
        raise KeychainReadError('PROTOCOL')
    read_request({key: ('read' if key == 'kind' else value[key]) for key in ('kind', 'account', 'sequence', 'nonce')})
    if (not account_valid(value['intent_id']) or type(value['deadline']) not in (int, float)
            or not math.isfinite(value['deadline']) or value['deadline'] <= 0
            or type(value['marker_sha256']) is not str or re.fullmatch('[0-9a-f]{64}', value['marker_sha256']) is None):
        raise KeychainReadError('PROTOCOL')
    if value['kind'] == 'delete':
        if value['raw'] is not None:
            raise KeychainReadError('PROTOCOL')
    else:
        try:
            raw = base64.b64decode(value['raw'], validate=True)
            if type(value['raw']) is not str or base64.b64encode(raw).decode('ascii') != value['raw']:
                raise ValueError
            credential_raw(raw)
            if mutation.credential_bytes(mutation.credential_value(raw)) != raw:
                raise ValueError
        except (ValueError, TypeError):
            raise KeychainReadError('PROTOCOL') from None
    return value


def _spawn(popen, inherited=()):
    raw_fds, streams, child = set(), [], None
    try:
        request_read, request_write = os.pipe()
        raw_fds.update((request_read, request_write))
        response_read, response_write = os.pipe()
        raw_fds.update((response_read, response_write))
        for fd, mode in ((request_read, 'rb'), (request_write, 'wb'),
                         (response_read, 'rb'), (response_write, 'wb')):
            streams.append(os.fdopen(fd, mode, buffering=0))
            raw_fds.remove(fd)
        os.set_blocking(request_write, False)
        os.set_blocking(response_read, False)
        child = popen([PYTHON, '-I', ENTRY, str(request_read), str(response_write), *map(str, inherited)],
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, close_fds=True,
                      pass_fds=(request_read, response_write, *inherited), start_new_session=True,
                      env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        return child, streams[1], streams[2]
    finally:
        for stream in streams if child is None else (streams[0], streams[3]):
            with suppress(OSError):
                stream.close()
        for fd in raw_fds:
            with suppress(OSError):
                os.close(fd)


class _CredentialProxy:
    def __init__(self, owner, account):
        self._owner, self._account = owner, account

    def read(self, *, deadline=None):
        return self._owner._read(self._account, deadline=deadline)

    def write(self, raw, *, intent_id, marker_sha256, root_fd, tick_fd, deadline):
        return self._owner._mutate(self._account, 'write', raw, intent_id=intent_id,
                                  marker_sha256=marker_sha256, root_fd=root_fd, tick_fd=tick_fd, deadline=deadline)

    def delete(self, *, intent_id, marker_sha256, root_fd, tick_fd, deadline):
        return self._owner._mutate(self._account, 'delete', None, intent_id=intent_id,
                                  marker_sha256=marker_sha256, root_fd=root_fd, tick_fd=tick_fd, deadline=deadline)


class KeychainOwner:
    """Own serial fresh reads, spawn-only work and retirement for one borrowed Store.

    Each read includes spawn, native copy, output, EOF and reap in the earlier of
    the original caller deadline and five seconds. Native code runs only in the
    child; the single worker can only create that child. Failure poisons further
    reads except a completed NOT_FOUND. close must run after state locks unwind
    and before Store.close; unreapable OS work remains held, never closed success.
    Mutations additionally retain the borrowed root and same-OFD tick lease until
    actual child retirement. Native completion is checkpointed by the fixed child.
    """
    def __init__(self, *, deadline=None, check=lambda: None, popen=None, monotonic=time.monotonic):
        if not callable(check) or not callable(monotonic) or popen is not None and not callable(popen):
            raise KeychainReadError()
        self._clock, self._check = monotonic, check
        if deadline is not None and (type(deadline) not in (int, float)
                                     or not math.isfinite(deadline) or deadline <= self._now()):
            raise KeychainReadError()
        self._deadline, self._popen = deadline, popen or subprocess.Popen
        self._thread = threading.get_ident()
        self._account = self._proxy = self._executor = self._spawning = None
        self._child = self._request = self._response = None
        self._sequence = 0
        self._failed = self._busy = self._lost_reap = self._closed = False
        self._interruption = None
        self._lease_fds = []

    @property
    def closed(self):
        return (self._closed and self._spawning is None and self._executor is None
                and self._child is None and self._request is None and self._response is None
                and not self._lost_reap and not self._lease_fds)

    def _now(self):
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise KeychainReadError()
        return value

    def _owner(self):
        if threading.get_ident() != self._thread:
            self._failed = True
            raise KeychainReadError('OWNER_REQUIRED')

    def _guard(self, limit):
        self._owner()
        if self._failed or self._closed:
            raise KeychainReadError('CLOSED' if self._closed else 'FAILED')
        if self._now() >= limit:
            raise KeychainReadError('DEADLINE')
        try:
            self._check()
        except TimeoutError:
            raise
        except Exception:
            raise KeychainReadError('CHECK_FAILED') from None
        if self._failed or self._now() >= limit:
            raise KeychainReadError('DEADLINE' if not self._failed else 'FAILED')

    def for_account(self, account):
        self._owner()
        if not account_valid(account):
            raise KeychainError('SDC_KEYCHAIN_INVALID_ACCOUNT')
        if self._closed or self._failed:
            raise KeychainReadError('CLOSED' if self._closed else 'FAILED')
        if self._account is not None and account != self._account:
            self._failed = True
            raise KeychainReadError('INVALID')
        if self._proxy is None:
            self._account, self._proxy = account, _CredentialProxy(self, account)
        return self._proxy

    def _adopt_spawn(self):
        future = self._spawning
        if future is not None and future.done():
            error = future.exception()
            if error is not None:
                self._spawning = None
                self._failed = True
                if not isinstance(error, Exception) or isinstance(error, TimeoutError):
                    raise error
                raise KeychainReadError('SPAWN') from None
            # A parent timeout while obtaining the settled result must leave the
            # Future owned so close can still adopt its child and pipe handles.
            self._child, self._request, self._response = future.result()
            self._spawning = None

    def _poll(self):
        if self._child is None or self._child.returncode is not None or self._lost_reap:
            return
        try:
            pid, status = os.waitpid(self._child.pid, os.WNOHANG)
        except ChildProcessError:
            self._lost_reap = self._failed = True
            return
        if pid == self._child.pid:
            self._child.returncode = os.waitstatus_to_exitcode(status)
        elif pid:
            self._lost_reap = self._failed = True

    def _close_pipes(self):
        for stream in (self._request, self._response):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except TimeoutError:
                    raise
                except OSError:
                    pass
        return all(stream is None or stream.closed for stream in (self._request, self._response))

    def _retired(self):
        if (self._spawning is not None or self._lost_reap
                or self._child is not None and self._child.returncode is None):
            return False
        if not self._close_pipes():
            return False
        self._child = self._request = self._response = None
        return True

    def _exchange(self, packet, nonce, limit, intent_id=None):
        offset, output, eof, value = 0, bytearray(), False, None
        while True:
            self._guard(limit)
            if offset < len(packet):
                with suppress(BlockingIOError):
                    count = os.write(self._request.fileno(), packet[offset:])
                    if count <= 0:
                        raise KeychainReadError('PROTOCOL')
                    offset += count
            if offset == len(packet) and not self._request.closed:
                self._request.close()
            if not eof:
                try:
                    chunk = os.read(self._response.fileno(), MAX_FRAME + 5 - len(output))
                except BlockingIOError:
                    chunk = None
                if chunk:
                    output.extend(chunk)
                elif chunk == b'':
                    eof = True
            self._poll()
            if self._lost_reap:
                raise KeychainReadError('CHILD')
            if len(output) >= 4:
                size = int.from_bytes(output[:4], 'big')
                if not 1 <= size <= MAX_FRAME or len(output) > size + 4:
                    raise KeychainReadError('PROTOCOL')
                if len(output) == size + 4 and value is None:
                    value = decode_payload(bytes(output[4:]))
                    common = {'kind', 'sequence', 'nonce'}
                    if (type(value.get('sequence')) is not int or value['sequence'] != self._sequence
                            or value.get('nonce') != nonce or offset != len(packet)
                            or (value.get('kind') == 'error' and (set(value) != common | {'code'}
                                                                 or value['code'] not in KEYCHAIN_CODES))
                            or (value.get('kind') == 'value' and (intent_id is not None or set(value) != common | {'raw', 'sha256'}))
                            or (value.get('kind') == 'verified' and (intent_id is None
                                or set(value) != common | {'intent_id', 'marker_sha256'}
                                or value['intent_id'] != intent_id
                                or type(value['marker_sha256']) is not str
                                or re.fullmatch('[0-9a-f]{64}', value['marker_sha256']) is None))
                            or value.get('kind') not in ('error', 'value', 'verified')):
                        raise KeychainReadError('PROTOCOL')
            if self._child.returncode is not None:
                if self._child.returncode != 0 or eof and value is None:
                    raise KeychainReadError('CHILD')
                if eof and value is not None:
                    if not self._retired():
                        raise KeychainReadError('CHILD')
                    self._guard(limit)
                    return value
            time.sleep(max(0, min(POLL_S, limit - self._now())))

    def _read(self, account, *, deadline=None):
        self._owner()
        if self._busy:
            raise KeychainReadError('BUSY')
        if account != self._account or self._sequence >= MAX_SEQUENCE:
            raise KeychainReadError('INVALID')
        self._busy = True
        try:
            limit = self._now() + READ_BUDGET_S
            if deadline is not None:
                if type(deadline) not in (int, float) or not math.isfinite(deadline):
                    raise KeychainReadError('INVALID')
                limit = min(limit, deadline)
            if self._deadline is not None:
                limit = min(limit, self._deadline)
            self._guard(limit)
            self._sequence += 1
            nonce = f'{self._sequence:08x}' + secrets.token_hex(12)
            packet = encode_frame({'kind': 'read', 'account': account, 'sequence': self._sequence, 'nonce': nonce})
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sdc-keychain-spawn')
            self._spawning = self._executor.submit(_spawn, self._popen)
            while self._spawning is not None:
                self._guard(limit)
                self._adopt_spawn()
                if self._spawning is not None:
                    time.sleep(max(0, min(POLL_S, limit - self._now())))
            value = self._exchange(packet, nonce, limit)
            if value['kind'] == 'error':
                raise KeychainError(value['code'])
            encoded = value['raw']
            raw = base64.b64decode(encoded, validate=True)
            if (type(encoded) is not str or base64.b64encode(raw).decode('ascii') != encoded
                    or value['sha256'] != hashlib.sha256(raw).hexdigest()):
                raise KeychainReadError('PROTOCOL')
            credential_raw(raw)
            self._guard(limit)
            return raw
        except BaseException as error:
            interrupted = not isinstance(error, Exception) or isinstance(error, TimeoutError)
            if interrupted and self._interruption is None:
                self._interruption = error
            if not (type(error) is KeychainError and error.code == 'SDC_KEYCHAIN_NOT_FOUND'
                    and self._spawning is None and self._child is None):
                self._failed = True
            if isinstance(error, KeychainError) or interrupted:
                raise
            raise KeychainReadError('PROTOCOL') from None
        finally:
            self._busy = False

    def _close_leases(self):
        while self._lease_fds:
            os.close(self._lease_fds[-1])
            self._lease_fds.pop()

    def _mutate(self, account, kind, raw, *, intent_id, marker_sha256, root_fd, tick_fd, deadline):
        self._owner()
        if self._busy:
            raise KeychainReadError('BUSY')
        if account != self._account or self._sequence >= MAX_SEQUENCE or self._lease_fds:
            raise KeychainReadError('INVALID')
        self._busy = True
        try:
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                raise KeychainReadError('INVALID')
            limit = min(deadline, self._now() + READ_BUDGET_S)
            if self._deadline is not None:
                limit = min(limit, self._deadline)
            self._guard(limit)
            marker = mutation.pending_marker(root_fd, tick_fd, intent_id=intent_id,
                                             marker_sha256=marker_sha256, account=account)
            if marker['mutation']['op'] != kind:
                raise KeychainReadError('PROTOCOL')
            self._sequence += 1
            nonce = f'{self._sequence:08x}' + secrets.token_hex(12)
            request = mutation_request({'kind': kind, 'account': account, 'sequence': self._sequence,
                                        'nonce': nonce, 'deadline': limit, 'intent_id': intent_id,
                                        'marker_sha256': marker_sha256,
                                        'raw': base64.b64encode(raw).decode('ascii') if raw is not None else None})
            if kind == 'write' and mutation.credential_digest(mutation.credential_value(raw)) != marker['mutation']['new_sha256']:
                raise KeychainReadError('PROTOCOL')
            packet = encode_frame(request)
            # Duplicate the held descriptors before submit: an unsettled Popen is
            # owned work too. Never open/flock the pathname to construct a lease.
            for fd in (root_fd, tick_fd):
                self._lease_fds.append(os.dup(fd))
            self._guard(limit)
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sdc-keychain-spawn')
            self._spawning = self._executor.submit(_spawn, self._popen, tuple(self._lease_fds))
            while self._spawning is not None:
                self._guard(limit)
                self._adopt_spawn()
                if self._spawning is not None:
                    time.sleep(max(0, min(POLL_S, limit - self._now())))
            value = self._exchange(packet, nonce, limit, intent_id)
            if value['kind'] == 'error':
                raise KeychainError(value['code'])
            actual = mutation.read_marker(root_fd)
            if (actual is None or mutation.marker_sha256(actual) != value['marker_sha256']
                    or actual['mutation']['phase'] != 'verified'):
                raise KeychainReadError('PROTOCOL')
            expected = {**marker, 'mutation': {**marker['mutation'], 'phase': 'verified',
                                               'verified_at': actual['mutation']['verified_at']}}
            if actual != expected:
                raise KeychainReadError('PROTOCOL')
            self._guard(limit)
            self._close_leases()
            return {'intent_id': intent_id, 'marker_sha256': value['marker_sha256']}
        except BaseException as error:
            self._failed = True
            interrupted = not isinstance(error, Exception) or isinstance(error, TimeoutError)
            if interrupted and self._interruption is None:
                self._interruption = error
            if isinstance(error, KeychainError) or interrupted:
                raise
            raise KeychainReadError('PROTOCOL') from None
        finally:
            self._busy = False

    def close(self):
        """Retire owned work; a lost reap returns unclosed for the Store's hold guard."""
        self._owner()
        if self._busy:
            raise KeychainReadError('BUSY')
        self._failed = True
        interruption = self._interruption
        term_at = kill_at = None
        while not self.closed:
            try:
                with suppress(KeychainReadError):
                    self._adopt_spawn()
                if self._spawning is None:
                    try:
                        self._poll()
                    except TimeoutError:
                        raise
                    except OSError:
                        pass
                    if self._child is not None and self._child.returncode is None and not self._lost_reap:
                        now, sig = self._now(), None
                        if term_at is None:
                            term_at, sig = now, signal.SIGTERM
                        elif now - term_at >= TERM_GRACE_S and (kill_at is None or now - kill_at >= TERM_GRACE_S):
                            kill_at, sig = now, signal.SIGKILL
                        if sig is not None:
                            try:
                                os.killpg(self._child.pid, sig)
                            except TimeoutError:
                                raise
                            except OSError:
                                pass
                    pipes_closed = self._close_pipes()
                    if (self._lost_reap and pipes_closed) or self._retired():
                        if self._executor is not None:
                            self._executor.shutdown(wait=True, cancel_futures=False)
                            self._executor = None
                        # Lost ownership is irreversible here: retain the child and
                        # failed/unknown evidence, but do not spin on an unprovable reap.
                        if not self._lost_reap:
                            self._close_leases()
                        self._closed = not self._lost_reap
                        break
                time.sleep(POLL_S)
            except BaseException as error:
                if not isinstance(error, Exception) or isinstance(error, TimeoutError):
                    if interruption is None:
                        interruption = error
                    continue
                if interruption is not None:
                    self._interruption = None
                    raise interruption from None
                raise KeychainReadError('CHILD') from None
        if interruption is not None:
            self._interruption = None
            raise interruption
