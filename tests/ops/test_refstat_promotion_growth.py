"""A pending file growing through an older open descriptor cannot prolong approval."""
import hashlib
import pytest

import mcs_operations


def test_promotion_bounds_claimed_source_and_rolls_back_when_it_grows(tmp_path, monkeypatch):
    pending_dir, approved_dir = tmp_path / "pending", tmp_path / "approved"
    pending_dir.mkdir(mode=0o700)
    approved_dir.mkdir(mode=0o700)
    pending, approved = pending_dir / "synthetic.json", approved_dir / "synthetic.json"
    original = b'{"synthetic":1}'
    pending.write_bytes(original)
    approved.write_bytes(b'{"synthetic":"prior"}')
    opened = mcs_operations.os.fdopen
    reads = []

    class Growing:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, length):
            reads.append(length)
            if len(reads) > 3:
                pytest.fail("claimed file growth exceeded the bounded source budget")
            chunk = self.stream.read(length)
            return chunk or b"x" * length  # Fake writes continue through the older fd.

    def fdopen(fd, mode, *args, **kwargs):
        stream = opened(fd, mode, *args, **kwargs)
        return Growing(stream) if mode == "rb" else stream

    monkeypatch.setattr(mcs_operations.os, "fdopen", fdopen)
    with pytest.raises(ValueError, match="refstat_hash_mismatch"):
        with mcs_operations._refstat_promotion(
                str(pending), str(approved), hashlib.sha256(original).hexdigest()):
            pytest.fail("growing bytes were published")
    assert reads == [len(original) + 1, 1]
    assert pending.read_bytes() == original
    assert approved.read_bytes() == b'{"synthetic":"prior"}'
    assert sorted(p.name for p in pending_dir.iterdir()) == [pending.name]
    assert sorted(p.name for p in approved_dir.iterdir()) == [approved.name]
