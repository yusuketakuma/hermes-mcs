"""Repair hashes only static snapshots, with the same sidecar rule as backup.
Synthetic temp files only."""
import pytest

import mcs_repair


@pytest.mark.parametrize("make_sidecar", [
    lambda side, tmp: side.write_bytes(b"synthetic"),           # regular sidecar file
    lambda side, tmp: side.symlink_to(tmp / "missing"),          # dangling link
], ids=["regular_file", "dangling_link"])
@pytest.mark.parametrize("suffix", ["-shm", "-wal"])
def test_hash_file_refuses_any_sidecar_entry(tmp_path, make_sidecar, suffix):
    snapshot = tmp_path / "snapshot.db"
    snapshot.write_bytes(b"synthetic")
    make_sidecar(tmp_path / f"snapshot.db{suffix}", tmp_path)
    with pytest.raises(ValueError, match="static_snapshot_required"):
        mcs_repair._hash_file(snapshot)


def test_hash_file_accepts_a_static_file(tmp_path):
    snapshot = tmp_path / "snapshot.db"
    snapshot.write_bytes(b"synthetic")
    assert len(mcs_repair._hash_file(snapshot)) == 64
