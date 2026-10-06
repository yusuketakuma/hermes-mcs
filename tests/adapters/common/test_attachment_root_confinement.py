"""Workers only read sealed attachments that resolve inside <root>/attachments."""
import hashlib

import pytest

from adapters.common import paths


def _part(path, blob):
    return {"path": str(path), "sha256": hashlib.sha256(blob).hexdigest(),
            "bytes": len(blob)}


def test_inside_attachments_is_read(tmp_path):
    (tmp_path / "attachments").mkdir()
    f = tmp_path / "attachments" / "a.bin"
    f.write_bytes(b"synthetic")
    assert paths.read_verified_attachment(str(f), _part(f, b"synthetic"),
                                          str(tmp_path)) == b"synthetic"


@pytest.mark.parametrize("kind", ["outside", "symlink"])
def test_outside_or_symlink_escape_is_refused(tmp_path, kind):
    root = tmp_path / "root"
    (root / "attachments").mkdir(parents=True)
    other = tmp_path / "other.bin"
    other.write_bytes(b"synthetic")
    target = other
    if kind == "symlink":
        target = root / "attachments" / "link.bin"
        target.symlink_to(other)
    assert paths.read_verified_attachment(str(target), _part(other, b"synthetic"),
                                          str(root)) is None
