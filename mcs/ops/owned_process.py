"""Own command sessions through interruption, termination and bounded reaping."""
import os
import math
import time
import signal
import subprocess
from contextlib import suppress
from contextlib import contextmanager
from contextvars import ContextVar

TERM_GRACE_S = 2  # give Git time to remove its own lock before KILL
_LIFETIME_FDS = ContextVar('mcs_owned_process_lifetime_fds', default=())


def _fd_identity(fd):
    if type(fd) is not int or fd < 0:
        raise ValueError('owned_lifetime_fd_invalid')
    info = os.fstat(fd)
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


@contextmanager
def lifetime_fds(descriptors):
    """Trusted caller's lifetime leases only, never permission to run an update."""
    lease = {'active': True, 'descriptors': tuple((fd, _fd_identity(fd)) for fd in descriptors)}
    token = _LIFETIME_FDS.set((*_LIFETIME_FDS.get(), lease))
    try:
        yield
    finally:
        lease['active'] = False
        _LIFETIME_FDS.reset(token)


@contextmanager
def _inherited_fds(extra=()):
    """Own only duplicates, so a borrowed descriptor is never closed or reused behind a child."""
    records = [record for lease in _LIFETIME_FDS.get() if lease['active'] for record in lease['descriptors']]
    records.extend((fd, _fd_identity(fd)) for fd in extra)
    copies = {}
    try:
        for fd, identity in records:
            if fd in copies:
                if _fd_identity(copies[fd]) != identity:
                    raise ValueError('owned_lifetime_fd_changed')
                continue
            copy = os.dup(fd)
            copies[fd] = copy
            if _fd_identity(copy) != identity:
                raise ValueError('owned_lifetime_fd_changed')
        yield copies
    finally:
        for copy in copies.values():
            os.close(copy)


def run_owned(argv: list[str], timeout: float, environment: dict,
               stdin=None, *, deadline=None, input_text=None, cwd=None) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output, text) for one command in its own
    session. On timeout or interruption that session is stopped by
    stop_owned and the exception re-raised. Without deadline the bound is
    timeout + TERM_GRACE_S + bounded reap. Deadline callers retain leases
    until exact child retirement, including a safety hold on lost reap."""
    if input_text is not None and (stdin is not None or type(input_text) is not str
            or len(input_text.encode('utf-8')) > 16384):
        raise ValueError('owned_input_invalid')
    def remaining():
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError('owned_deadline_invalid')
        left = deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        return min(timeout, left)
    if deadline is not None:
        remaining()
    with _inherited_fds() as inherited:
        if deadline is not None:
            remaining()
        child = subprocess.Popen(argv, stdin=subprocess.PIPE if input_text is not None else stdin, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=environment,
                                 start_new_session=True, **({'cwd': cwd} if cwd is not None else {}),
                                 **({'pass_fds': tuple(inherited.values())} if inherited else {}))
    try:
        out, err = child.communicate(timeout=timeout if deadline is None else remaining(),
            **({'input': input_text} if input_text is not None else {}))
        if deadline is not None:
            remaining()
    except BaseException:
        if deadline is not None:
            try:
                _wait_retired(child, timeout=0)
            except ChildRetirementError:
                pass
            finally:
                stop_owned(child)
        else:
            stop_owned(child)
        raise
    return subprocess.CompletedProcess(argv, child.returncode, out, err)


def stop_owned(child) -> None:
    """SIGTERM first so git cleans its lock, SIGKILL after the grace,
    then a bounded reap. Only a still-unreaped leader's group is
    signalled — its pid (= group id) cannot have been reused."""
    if child.returncode is None:
        with suppress(OSError):
            os.killpg(child.pid, signal.SIGTERM)
        with suppress(BaseException):
            child.communicate(timeout=TERM_GRACE_S)
        if child.returncode is None:
            with suppress(OSError):
                os.killpg(child.pid, signal.SIGKILL)
    with suppress(BaseException):
        child.communicate(timeout=5)
    if child.returncode is None:
        with suppress(BaseException):
            child.wait(timeout=5)
    for stream in (child.stdout, child.stderr):
        if stream is not None:
            with suppress(OSError):
                stream.close()   # an escaped holder must not leak our fds


class ChildRetirementError(ValueError):
    pass


def _wait_retired(child, *, timeout):
    """Keep the caller's leases until an exact waitpid result; lost reap never permits a signal."""
    until = time.monotonic() + timeout
    lost_reap, failed, interrupted = False, False, None
    signaled = False
    while child.returncode is None:
        try:
            if not lost_reap:
                try:
                    pid, status = os.waitpid(child.pid, os.WNOHANG)
                except InterruptedError:
                    raise
                except OSError:
                    lost_reap = failed = True
                else:
                    if pid == child.pid:
                        child.returncode = os.waitstatus_to_exitcode(status)
                    elif pid:
                        lost_reap = failed = True
            if child.returncode is None:
                if not lost_reap and not signaled and (interrupted is not None or time.monotonic() >= until):
                    signaled = failed = True
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except OSError:
                        pass
                time.sleep(0.02)
        except (KeyboardInterrupt, InterruptedError) as error:
            interrupted = error
    if interrupted is not None:
        raise interrupted
    if failed:
        raise ChildRetirementError('owned_child_retirement_hold')
