"""Apply one human-confirmed command from a local CLI under the shared writer lock and keep its receipt."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path

import mcs_util

# A human-confirmed command waits for run.lock longer than one collection tick holds it
# (run_check waits up to 150 s itself): with the resident workers and the 5-second
# command ticks taking turns, a few seconds of polling almost never sees the lock free.
WAIT_SECONDS = 180.0


def wait_writer(lock_path: str, marker_path: str, expected_code: int) -> int | None:
    """Take run.lock for one human-confirmed CLI command, waiting up to WAIT_SECONDS; None while busy."""
    import sys
    from writer_lock import acquire_run_lock, wait_run_lock
    lock_fd = acquire_run_lock(lock_path)
    if lock_fd is None:
        print(f"ほかの保存処理の終了を最大{int(WAIT_SECONDS)}秒待ちます…", file=sys.stderr, flush=True)
        lock_fd = wait_run_lock(lock_path, wait_s=WAIT_SECONDS, expected_code_stamp=expected_code,
                                marker_path=marker_path)
    return lock_fd


def ledger_path() -> Path:
    return Path(mcs_util.HOME) / "data" / "ledger.db"


def apply_locked(req: dict, *, under_lock=None):
    """Return the command receipt, or None while another writer keeps ``run.lock``.

    ``under_lock`` runs after the lock and the update/restore holds are checked and
    returns extra keyword arguments for ``apply_command`` (for example a configuration
    reloaded under the lock). Callers validate ``req`` before calling, so a malformed
    request never opens a writer.
    """
    from writer_lock import code_stamp
    from ledger import Ledger
    import mcs_requests
    import notify_spool
    expected_code = code_stamp()
    path = ledger_path()
    if path.is_symlink() or not path.is_file():
        raise ValueError("ledger_unavailable")
    lock_path = str(path.parent / "run.lock")
    marker = path.parent / "update_in_progress.marker"
    lock_fd = wait_writer(lock_path, str(marker), expected_code)
    if lock_fd is None:
        return None
    try:
        if marker.exists() or code_stamp() != expected_code:
            raise ValueError("update_in_progress")
        if notify_spool.restore_writer_hold(str(path.parent)) is not None:
            raise ValueError("restore_in_progress")
        options = under_lock() if under_lock is not None else {}
        with closing(Ledger(str(path))) as ledger:
            return mcs_requests.apply_command(ledger, req, **options)
    finally:
        os.close(lock_fd)
