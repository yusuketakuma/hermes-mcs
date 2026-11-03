"""Synthetic freshness checks share publication fencing and expose bounded timings."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import os
import threading
import time

import pytest

import maintenance
import snapshots
from extract_testkit import _ledger, _message


def test_refresh_fences_decision_and_marker_to_avoid_a_second_copy(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(1, body="合成の経過記録")])
    db.close()
    path = str(tmp_path / "ledger.db")
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    signature_started, resume_signature = threading.Event(), threading.Event()
    original_signature = maintenance._content_signature
    original_copy = snapshots._prepare_in_child
    signatures, copies = [], []

    def signature(*args, **kwargs):
        signatures.append(True)
        signature_started.set()
        assert resume_signature.wait(10)
        return original_signature(*args, **kwargs)

    def copy(*args, **kwargs):
        copies.append(True)
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(maintenance, "_content_signature", signature)
    monkeypatch.setattr(snapshots, "_prepare_in_child", copy)
    def winner():
        result = maintenance.refresh_snapshot(path, deadline=time.monotonic() + 20)
        return result, snapshots.last_failure(), snapshots.last_timing()

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(winner)
        assert signature_started.wait(10)
        try:
            # No second caller may take a stale decision while the first owns the fence.
            assert maintenance.refresh_snapshot(path, deadline=time.monotonic() + 20) is None
            assert snapshots.last_failure()["reason"] == "snapshot_lock_busy"
            assert snapshots.publish_snapshot(path, maintenance.SNAPSHOT_DIR) is None
            assert snapshots.last_failure()["reason"] == "snapshot_lock_busy"
            assert signatures == [True]
        finally:
            resume_signature.set()
        result, failure, timing = first.result(timeout=20)
        assert result == "published" and failure == {}
        assert set(timing) == {"copy_s", "verify_s", "budget_s", "signature_s", "marker_s",
                               "publication_s", "total_s", "marker_state"}
        assert timing["marker_state"] == "saved"
        assert snapshots.last_failure()["reason"] == "snapshot_lock_busy"
        assert snapshots.last_timing() == {}
    assert maintenance.refresh_snapshot(path, deadline=time.monotonic() + 20) == "unchanged"
    assert copies == [True] and len(signatures) == 2


def test_refresh_reports_signature_marker_publication_and_total_timings(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(1, body="完全合成の記録")])
    db.close()
    path = str(tmp_path / "ledger.db")
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    assert maintenance.refresh_snapshot(path, deadline=time.monotonic() + 20) == "published"
    timing = snapshots.last_timing()
    assert set(timing) == {"copy_s", "verify_s", "budget_s", "signature_s", "marker_s",
                           "publication_s", "total_s", "marker_state"}
    assert timing["marker_state"] == "saved"
    assert all(type(value) is float and value >= 0 for key, value in timing.items()
               if key != "marker_state")
    for name in ("copy_s", "verify_s", "signature_s", "marker_s", "publication_s"):
        assert timing[name] <= timing["total_s"] + 0.002
    assert snapshots.last_failure() == {}
    assert maintenance.refresh_snapshot(path) == "unchanged"
    assert snapshots.last_timing() == {}


@pytest.mark.parametrize("damage", ["wrong_fd", "wrong_destination", "unlocked", "foreign_owner",
                                    "closed_fd", "mode", "hardlink", "invalid_type"])
def test_borrowed_publication_fence_cannot_bypass_lock_validation(tmp_path, monkeypatch, damage):
    destination = str(tmp_path / "snapshots")
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", destination)
    monkeypatch.setattr(snapshots, "_prepare_in_child", lambda *a, **kw: pytest.fail("unverified fence copied"))
    snapshots._reset_status()
    with snapshots._publication_lock(destination) as owned:
        assert owned is not None
        borrowed = owned
        extra = None
        if damage in ("wrong_fd", "closed_fd"):
            extra = os.open(tmp_path / "unrelated", os.O_WRONLY | os.O_CREAT, 0o600)
            borrowed = extra
            if damage == "closed_fd":
                os.close(extra)
                extra = None
        elif damage == "wrong_destination":
            monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(tmp_path / "other"))
        elif damage in ("unlocked", "foreign_owner"):
            fcntl.flock(owned, fcntl.LOCK_UN)
            if damage == "foreign_owner":
                extra = os.open(os.path.join(destination, ".snapshot.lock"), os.O_WRONLY)
                fcntl.flock(extra, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif damage == "mode":
            os.fchmod(owned, 0o640)
        elif damage == "hardlink":
            os.link(os.path.join(destination, ".snapshot.lock"), tmp_path / "extra-link")
        elif damage == "invalid_type":
            borrowed = False
        try:
            snapshots._record_timing(copy_s=99.0)
            snapshots._fail("previous_failure", exit_code=4)
            assert maintenance.publish_snapshot(str(tmp_path / "absent.db"), _lock_fd=borrowed) is False
            assert snapshots.last_failure() == {"reason": "snapshot_lock_unverified"}
            assert snapshots.last_timing() == {}
        finally:
            if extra is not None:
                os.close(extra)


def test_marker_failure_keeps_verified_publication_but_reports_unavailable_marker(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(1, body="合成記録")])
    db.close()
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(tmp_path / "snapshots"))

    def unavailable(*args, **kwargs):
        raise OSError("synthetic marker failure")

    monkeypatch.setattr(maintenance, "atomic_write", unavailable)
    assert maintenance.refresh_snapshot(str(tmp_path / "ledger.db")) == "published"
    assert (tmp_path / "snapshots" / "ledger-snapshot.db").is_file()
    timing = snapshots.last_timing()
    assert timing["marker_state"] == "unavailable" and timing["marker_s"] >= 0
    assert snapshots.last_failure() == {}
