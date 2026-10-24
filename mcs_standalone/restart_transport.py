"""Keep one restart HTTP child's ownership until actual reap without blocking the owner loop."""
import asyncio
from contextlib import suppress
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from adapters.sdc.protocol import strict_json
from sdc_restart_transport import MAX_OUTPUT, encode_request

PYTHON = str(Path(sys.executable).absolute())
ENTRY = str(Path(__file__).resolve().parents[1] / 'mcs' / 'ops' / 'sdc_restart_transport.py')
POLL_S = 0.01
TERM_GRACE_S = 2.0
MAX_SEND_S = 10.0


class RestartCarrierError(RuntimeError):
    """Expose a fixed local code and observed send stage, never response/error text."""
    def __init__(self, code, outcome='not_sent'):
        super().__init__(code)
        self.code, self.outcome, self.status = code, outcome, 503


def _spawn():
    return subprocess.Popen([PYTHON, '-I', ENTRY], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            bufsize=0, close_fds=True, start_new_session=True,
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})


class RestartCarrier:
    """One Runtime owns this object, its reservation and its original send continuation.

    Construct on that Runtime's loop/thread. Retain it until busy is false; stop
    calls cancel(), then waits for wait_closed() before releasing the reservation.
    send cancellation also waits for real reap, even under repeated cancellation.
    A stuck spawn/reap keeps busy true indefinitely, never implies success. Only
    send returns a response, and only to its original continuation. The caller
    must reject stale owner/consent/deadline state, validate the response and must
    never retry an unknown send. No grant, result or request is persisted here.
    """
    def __init__(self):
        self._loop = asyncio.get_running_loop()
        self._thread = threading.get_ident()
        self._owner = None
        self._stopping = False
        self._closed = asyncio.Event()
        self._closed.set()
        self.outcome = 'not_sent'

    def _check_owner(self):
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if threading.get_ident() != self._thread or current is not self._loop:
            raise RestartCarrierError('SDC_RESTART_CARRIER_OWNER_REQUIRED')

    @property
    def busy(self):
        return self._owner is not None

    def cancel(self):
        self._check_owner()
        if self.busy:
            self._stopping = True

    async def wait_closed(self):
        """Wait for ownership retirement; this never retrieves a response."""
        self._check_owner()
        cancelled = False
        while self.busy:
            try:
                await self._closed.wait()
            except asyncio.CancelledError:
                cancelled = True
                self.cancel()
        if cancelled:
            raise asyncio.CancelledError

    async def send(self, approved_endpoint, path, raw, headers, *, deadline):
        """Send once within finite absolute loop.time budget, capped at ten seconds."""
        self._check_owner()
        if self.busy:
            raise RestartCarrierError('SDC_RESTART_CARRIER_BUSY')
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise RestartCarrierError('SDC_RESTART_CARRIER_DEADLINE_INVALID')
        try:
            packet = encode_request(approved_endpoint, path, raw, headers)
        except ValueError:
            raise RestartCarrierError('SDC_RESTART_CARRIER_REQUEST_INVALID') from None
        if deadline <= self._loop.time():
            raise RestartCarrierError('SDC_RESTART_CARRIER_DEADLINE')
        deadline = min(deadline, self._loop.time() + MAX_SEND_S)
        self._stopping, self.outcome = False, 'not_sent'
        self._closed.clear()
        coroutine = self._run(packet, deadline)
        try:
            task = asyncio.create_task(coroutine)
        except BaseException as exc:
            coroutine.close()
            self._closed.set()
            if isinstance(exc, Exception):
                raise RestartCarrierError('SDC_RESTART_CARRIER_UNKNOWN') from None
            raise
        self._owner = task
        cancelled = False
        try:
            while True:
                try:
                    response, code = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.cancelled():
                        raise
                    cancelled = True
                    self.cancel()
            if cancelled:
                raise asyncio.CancelledError
            if self._stopping:
                raise RestartCarrierError('SDC_RESTART_CARRIER_CANCELLED', self.outcome)
            if self._loop.time() >= deadline:
                raise RestartCarrierError('SDC_RESTART_CARRIER_DEADLINE', self.outcome)
            if code:
                raise RestartCarrierError(code, self.outcome)
            return response
        finally:
            # _run completes after reap/pipe close, or is cancelled before starting.
            if task.done():
                self._owner = None
                self._closed.set()

    async def _run(self, packet, deadline):
        child = None
        try:
            # Keep the executor future outside cancel-all task shutdown as well.
            spawning = self._loop.run_in_executor(None, _spawn)
            while True:
                try:
                    child = await asyncio.shield(spawning)
                    break
                except asyncio.CancelledError:
                    self._stopping = True
            for stream in (child.stdin, child.stdout):
                os.set_blocking(stream.fileno(), False)
            output, offset, eof = bytearray(), 0, False
            while True:
                if self._stopping:
                    return None, 'SDC_RESTART_CARRIER_CANCELLED'
                if self._loop.time() >= deadline:
                    return None, 'SDC_RESTART_CARRIER_DEADLINE'
                if not child.stdin.closed:
                    self.outcome = 'unknown'
                    with suppress(BlockingIOError):
                        offset += os.write(child.stdin.fileno(), packet[offset:])
                    if offset == len(packet):
                        child.stdin.close()
                if not eof:
                    try:
                        chunk = os.read(child.stdout.fileno(), MAX_OUTPUT + 1 - len(output))
                    except BlockingIOError:
                        chunk = None
                    if chunk:
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT:
                            return None, 'SDC_RESTART_CARRIER_RESPONSE_INVALID'
                    elif chunk == b'':
                        eof = True
                if child.poll() is not None and eof:
                    if child.returncode != 0:
                        return None, 'SDC_RESTART_CARRIER_UNKNOWN'
                    if output == b'N\n':
                        self.outcome = 'not_sent'
                        return None, 'SDC_RESTART_CARRIER_REQUEST_INVALID'
                    if output[:2] != b'R\n':
                        return None, 'SDC_RESTART_CARRIER_UNKNOWN'
                    response = strict_json(bytes(output[2:]))
                    if type(response) is not dict:
                        return None, 'SDC_RESTART_CARRIER_RESPONSE_INVALID'
                    self.outcome = 'response'
                    return response, None
                await asyncio.sleep(POLL_S)
        except asyncio.CancelledError:
            self._stopping = True
            return None, 'SDC_RESTART_CARRIER_CANCELLED'
        except Exception:
            return None, 'SDC_RESTART_CARRIER_UNKNOWN'
        finally:
            if child is not None:
                await self._retire(child)

    async def _retire(self, child):
        term_at, killed = None, False
        while True:
            # Popen is reaped only here/on this loop, never by an asyncio watcher.
            with suppress(OSError):
                child.poll()
            if child.returncode is None:
                now = self._loop.time()
                sig = None
                if term_at is None:
                    term_at, sig = now, signal.SIGTERM
                elif not killed and now - term_at >= TERM_GRACE_S:
                    killed, sig = True, signal.SIGKILL
                if sig is not None:
                    with suppress(OSError):
                        os.killpg(child.pid, sig)
            for stream in (child.stdin, child.stdout):
                if not stream.closed:
                    with suppress(OSError):
                        stream.close()
            if child.returncode is not None and child.stdin.closed and child.stdout.closed:
                return
            try:
                await asyncio.sleep(POLL_S)
            except asyncio.CancelledError:
                self._stopping = True
