"""Apply one human-confirmed command from a local CLI under the shared writer lock and keep its receipt."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path

import mcs_util

WAIT_SECONDS = 5.0


def ledger_path() -> Path:
    return Path(mcs_util.HOME) / "data" / "ledger.db"


def apply_locked(req: dict, *, under_lock=None):
    """Return the command receipt, or None while another writer keeps ``run.lock``.

    ``under_lock`` runs after the lock and the update/restore holds are checked and
    returns extra keyword arguments for ``apply_command`` (for example a configuration
    reloaded under the lock). Callers validate ``req`` before calling, so a malformed
    request never opens a writer.
    """
    from writer_lock import acquire_run_lock, code_stamp, wait_run_lock
    from ledger import Ledger
    import mcs_requests
    import notify_spool
    expected_code = code_stamp()
    path = ledger_path()
    if path.is_symlink() or not path.is_file():
        raise ValueError("ledger_unavailable")
    lock_path = str(path.parent / "run.lock")
    marker = path.parent / "update_in_progress.marker"
    lock_fd = acquire_run_lock(lock_path)
    if lock_fd is None:
        lock_fd = wait_run_lock(lock_path, wait_s=WAIT_SECONDS, expected_code_stamp=expected_code,
                                marker_path=str(marker))
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
