"""A refused or rolled-back approval durably restores its filesystem inputs."""
import hashlib

import pytest

import mcs_operations


@pytest.mark.parametrize("rejected_before_publish", [False, True])
def test_refstat_rollback_syncs_restored_directories(tmp_path, monkeypatch, rejected_before_publish):
    pending_dir = tmp_path / "pending"
    approved_dir = tmp_path / "approved"
    pending_dir.mkdir()
    approved_dir.mkdir()
    pending = pending_dir / "synthetic.json"
    approved = approved_dir / "synthetic.json"
    pending.write_bytes(b"synthetic new input")
    approved.write_bytes(b"synthetic previous approval")
    digest = hashlib.sha256(pending.read_bytes()).hexdigest()
    original = mcs_operations.fsync_dir
    synced = []

    def sync(path, **kwargs):
        synced.append(str(path))
        return original(path, **kwargs)

    monkeypatch.setattr(mcs_operations, "fsync_dir", sync)
    error = ValueError if rejected_before_publish else RuntimeError
    with pytest.raises(error):
        with mcs_operations._refstat_promotion(
                str(pending), str(approved), "0" * 64 if rejected_before_publish else digest):
            raise RuntimeError("synthetic receipt commit failure")
    assert pending.read_bytes() == b"synthetic new input"
    assert approved.read_bytes() == b"synthetic previous approval"
    expected = [str(pending_dir)] if rejected_before_publish else [
        str(approved_dir), str(approved_dir), str(pending_dir)]
    assert synced == expected


@pytest.mark.parametrize("newer_pending", [False, True])
def test_rollback_sync_failure_preserves_both_inputs(tmp_path, monkeypatch, newer_pending):
    pending_dir = tmp_path / "pending"
    approved_dir = tmp_path / "approved"
    pending_dir.mkdir()
    approved_dir.mkdir()
    pending = pending_dir / "synthetic.json"
    approved = approved_dir / "synthetic.json"
    pending.write_bytes(b"synthetic input")
    approved.write_bytes(b"synthetic previous approval")
    digest = hashlib.sha256(pending.read_bytes()).hexdigest()
    original = mcs_operations.fsync_dir
    approved_syncs = 0

    def sync(path, **kwargs):
        nonlocal approved_syncs
        if str(path) == str(approved_dir):
            approved_syncs += 1
            if approved_syncs == 2:
                raise OSError("synthetic rollback directory sync failure")
        return original(path, **kwargs)

    monkeypatch.setattr(mcs_operations, "fsync_dir", sync)
    with pytest.raises(OSError, match="synthetic rollback"):
        with mcs_operations._refstat_promotion(str(pending), str(approved), digest):
            if newer_pending:
                pending.write_bytes(b"synthetic newer capture")
            raise RuntimeError("synthetic receipt commit failure")
    assert approved.read_bytes() == b"synthetic previous approval"
    if newer_pending:
        assert pending.read_bytes() == b"synthetic newer capture"
        recovered = list(pending_dir.glob(".refstat-*.pending"))
        assert len(recovered) == 1
        assert recovered[0].read_bytes() == b"synthetic input"
    else:
        assert pending.read_bytes() == b"synthetic input"
