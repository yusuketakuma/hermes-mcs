"""Own fresh Keychain reads and actual child retirement for one live restart attempt."""
import asyncio
import base64
from contextlib import suppress
import math
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time

from adapters.sdc.keychain import KeychainError
from sdc_restart_credential import (KEYCHAIN_CODES, MAX_FRAME, MAX_SEQUENCE, account_valid,
                                    credential_raw, decode_payload, encode_frame)

PYTHON = str(Path(sys.executable).absolute())
ENTRY = str(Path(__file__).resolve().parents[1] / 'mcs' / 'ops' / 'sdc_restart_credential.py')
POLL_S = 0.005
READ_BUDGET_S = 1.0
TERM_GRACE_S = 2.0


class CredentialCarrierError(RuntimeError):
    """Expose only fixed local failures, without pipe or credential contents."""
    def __init__(self, code='INVALID'):
        allowed = {'INVALID', 'OWNER_REQUIRED', 'BUSY', 'CLOSED', 'FAILED', 'DEADLINE',
                   'PROTOCOL', 'CHILD', 'SPAWN', 'CHECK_FAILED'}
        self.code = 'SDC_RESTART_CREDENTIAL_' + (code if code in allowed else 'INVALID')
        super().__init__(self.code)


def _spawn():
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
        child = subprocess.Popen([PYTHON, '-I', ENTRY, str(request_read), str(response_write)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True,
                                 pass_fds=(request_read, response_write), start_new_session=True,
                                 env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        return child, streams[1], streams[2]
    finally:
        for stream in streams if child is None else (streams[0], streams[3]):
            with suppress(OSError):
                stream.close()
        for fd in raw_fds:
            with suppress(OSError):
                os.close(fd)


class CredentialCarrier:
    """One loop/thread owns one fixed account and every spawn, pipe and reap.

    Each synchronous read has at most one second of the original deadline.
    check must not read credentials recursively; the owner also enforces its
    original grant window there. Failure permanently forbids further reads.
    close waits for actual cleanup without that check, even after cancellation.
    """
    def __init__(self, *, check, deadline):
        self._loop, self._thread = asyncio.get_running_loop(), threading.get_ident()
        if (not callable(check) or type(deadline) not in (int, float)
                or not math.isfinite(deadline) or deadline <= self._loop.time()):
            raise CredentialCarrierError('INVALID')
        self._check, self._deadline = check, deadline
        self._account = self._child = self._spawning = None
        self._request = self._response = None
        self._failed = self._ready = self._busy = self._lost_reap = False
        self._terminal = False
        self._sequence, self._nonce = 0, None
        self._closer = None
        self._closed = asyncio.Event()

    @property
    def busy(self):
        return (self._spawning is not None
                or self._child is not None and self._child.returncode is None
                or any(p is not None and not p.closed for p in (self._request, self._response)))

    @property
    def closed(self):
        """True only after cleanup has no pending spawn, unreaped child or open pipe."""
        return self._closed.is_set() and not self.busy

    def _owner(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not self._loop or threading.get_ident() != self._thread:
            self._failed = True
            raise CredentialCarrierError('OWNER_REQUIRED')

    def _guard(self, limit):
        self._owner()
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        if self._failed or self._closed.is_set():
            raise CredentialCarrierError('FAILED')
        if self._loop.time() >= min(limit, self._deadline):
            raise CredentialCarrierError('DEADLINE')
        try:
            self._check()
        except Exception:
            raise CredentialCarrierError('CHECK_FAILED') from None
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        if self._failed or self._loop.time() >= min(limit, self._deadline):
            raise CredentialCarrierError('DEADLINE' if not self._failed else 'FAILED')

    def _adopt_spawn(self):
        future = self._spawning
        if future is not None and future.done() and not future.cancelled():
            try:
                self._child, self._request, self._response = future.result()
            except BaseException:
                self._failed = True
                raise CredentialCarrierError('SPAWN') from None
            finally:
                self._spawning = None

    def _poll(self):
        if self._child.returncode is not None or self._lost_reap:
            return
        try:
            pid, status = os.waitpid(self._child.pid, os.WNOHANG)
        except ChildProcessError:
            self._lost_reap = True
            self._failed = True
            return
        if pid == self._child.pid:
            self._child.returncode = os.waitstatus_to_exitcode(status)
        elif pid:
            self._lost_reap = self._failed = True

    def _close_pipes(self):
        for pipe in (self._request, self._response):
            if pipe is not None and not pipe.closed:
                with suppress(OSError):
                    pipe.close()

    def _packet(self, kind):
        if self._sequence > MAX_SEQUENCE:
            raise CredentialCarrierError('PROTOCOL')
        nonce = f'{self._sequence:08x}' + secrets.token_hex(12)
        self._nonce = nonce
        value = {'kind': kind, 'sequence': self._sequence, 'nonce': nonce}
        if kind == 'prepare':
            value['account'] = self._account
        return encode_frame(value)

    def _exchange(self, packet, limit, *, terminal=False, preparing=False):
        # No outstanding/unsolicited reply may become this request's response.
        try:
            pending = os.read(self._response.fileno(), 1)
        except BlockingIOError:
            pending = None
        if pending is not None:
            raise CredentialCarrierError('PROTOCOL')
        offset, output, eof = 0, bytearray(), False
        value = None
        while True:
            self._guard(limit)
            if offset < len(packet):
                with suppress(BlockingIOError):
                    count = os.write(self._request.fileno(), packet[offset:])
                    if count <= 0:
                        raise CredentialCarrierError('PROTOCOL')
                    offset += count
            if terminal and offset == len(packet) and not self._request.closed:
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
                raise CredentialCarrierError('CHILD')
            if len(output) >= 4:
                size = int.from_bytes(output[:4], 'big')
                if not 1 <= size <= MAX_FRAME or len(output) > size + 4:
                    raise CredentialCarrierError('PROTOCOL')
                if len(output) == size + 4 and value is None:
                    value = decode_payload(bytes(output[4:]))
                    expected = 'ready' if preparing else 'value'
                    fields = {'kind', 'sequence', 'nonce'} | (set() if preparing else {'raw'})
                    if (value.get('kind') == 'error' and set(value) == {'kind', 'sequence', 'nonce', 'code'}
                            and value.get('sequence') == self._sequence and type(value.get('sequence')) is int
                            and value.get('nonce') == self._nonce and value.get('code') in KEYCHAIN_CODES):
                        raise KeychainError(value['code'])
                    if (set(value) != fields or value.get('kind') != expected
                            or type(value.get('sequence')) is not int or value['sequence'] != self._sequence
                            or value.get('nonce') != self._nonce or offset != len(packet)):
                        raise CredentialCarrierError('PROTOCOL')
            if value is not None:
                if terminal:
                    if eof and self._child.returncode == 0:
                        self._close_pipes()
                        if not self._request.closed or not self._response.closed:
                            raise CredentialCarrierError('CHILD')
                        self._guard(limit)
                        return value
                elif not eof and self._child.returncode is None:
                    try:
                        extra = os.read(self._response.fileno(), 1)
                    except BlockingIOError:
                        extra = None
                    if extra is not None:
                        raise CredentialCarrierError('PROTOCOL')
                    self._guard(limit)
                    return value
            if eof and value is None or self._child.returncode is not None and (not terminal or self._child.returncode != 0):
                raise CredentialCarrierError('CHILD')
            yield

    async def prepare(self, account):
        """Prestart a fixed child through READY; failure closes it before returning."""
        self._owner()
        try:
            if self._busy or self._account is not None or self._closed.is_set() or self._failed:
                raise CredentialCarrierError('BUSY')
            if not account_valid(account):
                raise CredentialCarrierError('INVALID')
            self._busy, self._account = True, account
            limit = min(self._deadline, self._loop.time() + READ_BUDGET_S)
            self._guard(limit)
            self._spawning = self._loop.run_in_executor(None, _spawn)
            while self._spawning is not None:
                self._guard(limit)
                self._adopt_spawn()
                if self._spawning is not None:
                    await asyncio.sleep(POLL_S)
            exchange = self._exchange(self._packet('prepare'), limit, preparing=True)
            while True:
                try:
                    next(exchange)
                except StopIteration:
                    break
                await asyncio.sleep(POLL_S)
            self._ready = True
        except BaseException as error:
            self._failed = True
            await self.close()
            if isinstance(error, (CredentialCarrierError, KeychainError)) or not isinstance(error, Exception):
                raise
            raise CredentialCarrierError() from None
        finally:
            self._busy = False

    def copy_current(self, account, *, terminal=False):
        """Copy anew; terminal success includes exit 0, real reap, EOF and closed pipes."""
        self._owner()
        try:
            if self._busy or not self._ready or self._failed or self._terminal or self._closed.is_set():
                raise CredentialCarrierError('CLOSED' if self._terminal else 'FAILED')
            if type(account) is not str or account != self._account or type(terminal) is not bool:
                raise CredentialCarrierError('INVALID')
            self._busy = True
            limit = min(self._deadline, self._loop.time() + READ_BUDGET_S)
            self._guard(limit)
            self._sequence += 1
            packet = self._packet('read_and_exit' if terminal else 'read')
            exchange = self._exchange(packet, limit, terminal=terminal)
            while True:
                try:
                    next(exchange)
                except StopIteration as done:
                    response = done.value
                    break
                time.sleep(max(0, min(POLL_S, limit - self._loop.time())))
            encoded = response['raw']
            raw = base64.b64decode(encoded, validate=True)
            if type(encoded) is not str or base64.b64encode(raw).decode('ascii') != encoded:
                raise CredentialCarrierError('PROTOCOL')
            credential_raw(raw)
            self._guard(limit)
            if terminal:
                self._terminal = True
                self._closed.set()
            return raw
        except BaseException as error:
            self._failed = True
            if isinstance(error, (CredentialCarrierError, KeychainError)) or not isinstance(error, Exception):
                raise
            raise CredentialCarrierError('PROTOCOL') from None
        finally:
            self._busy = False

    async def close(self):
        """Hold ownership through actual reap; check/deadline never interrupt cleanup."""
        self._owner()
        self._failed = True
        cancelled = False
        if self._closer is not None and self._closer is not asyncio.current_task():
            while not self._closed.is_set():
                try:
                    await self._closed.wait()
                except asyncio.CancelledError:
                    cancelled = True
        elif not self._closed.is_set():
            self._closer = asyncio.current_task()
            term_at = kill_at = None
            while not self._closed.is_set():
                with suppress(CredentialCarrierError):
                    self._adopt_spawn()
                if self._spawning is None:
                    if self._child is not None:
                        with suppress(OSError):
                            self._poll()
                        if self._child.returncode is None and not self._lost_reap:
                            now, sig = self._loop.time(), None
                            if term_at is None:
                                term_at, sig = now, signal.SIGTERM
                            elif now - term_at >= TERM_GRACE_S and (kill_at is None or now - kill_at >= TERM_GRACE_S):
                                kill_at, sig = now, signal.SIGKILL
                            if sig is not None:
                                with suppress(OSError):
                                    os.killpg(self._child.pid, sig)
                    self._close_pipes()
                    if not self.busy:
                        self._closed.set()
                        break
                try:
                    await asyncio.sleep(POLL_S)
                except asyncio.CancelledError:
                    cancelled = True
            self._closer = None
        if cancelled:
            raise asyncio.CancelledError
