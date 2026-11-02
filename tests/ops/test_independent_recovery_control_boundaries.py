"""The standalone recovery copy keeps bounded controls without importing the checkout."""
import errno
import os

import pytest

from ops_testkit import _load


@pytest.mark.parametrize("kind", ["fifo", "symlink", "oversize", "invalid_utf8"])
def test_installed_repo_pointer_refuses_unsafe_leaf(tmp_path, monkeypatch, kind):
    rec = _load()
    monkeypatch.setattr(rec, "RECOVERY_DIR", str(tmp_path))
    path = tmp_path / "repo_path"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        target = tmp_path / "other"
        target.write_text("/synthetic/checkout")
        path.symlink_to(target)
    elif kind == "oversize":
        path.write_bytes(b"/" + b"x" * 65536)
    else:
        path.write_bytes(b"\xff")
    original = os.open

    def checked_open(name, flags, *args, **kwargs):
        assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(rec.os, "open", checked_open)
    assert rec._installed_repo() == rec.HOME


def test_installed_repo_pointer_keeps_parent_alias_support(tmp_path, monkeypatch):
    rec = _load()
    directory = tmp_path / "real"
    directory.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    (directory / "repo_path").write_text("/synthetic/checkout\n")
    monkeypatch.setattr(rec, "RECOVERY_DIR", str(alias))
    assert rec._installed_repo() == "/synthetic/checkout"


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink"])
def test_recovery_lock_refuses_nonregular_leaves(tmp_path, monkeypatch, kind):
    rec = _load()
    path = tmp_path / "update.lock"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        target = tmp_path / "other"
        target.write_bytes(b"synthetic untouched")
        path.symlink_to(target)
    original = os.open
    opened = []

    def checked_open(name, flags, *args, **kwargs):
        assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        fd = original(name, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(rec.os, "open", checked_open)
    assert rec._try_lock(str(path)) is None
    for fd in opened:
        with pytest.raises(OSError) as error:
            os.fstat(fd)
        assert error.value.errno == errno.EBADF


def test_detached_restart_reader_is_frozen_and_stdlib_only(tmp_path):
    rec = _load()
    namespace = {}
    definitions = rec._GATEWAY_RESTART_PROGRAM.rsplit("sys.exit(", 1)[0]
    exec(compile(definitions, "<synthetic-detached-recovery>", "exec"), namespace)
    path = tmp_path / "control.json"
    path.write_text('{"synthetic":true}')
    assert namespace["_read_local_json"](str(path)) == {"synthetic": True}
    assert namespace["_read_regular_bytes"] is not rec._read_regular_bytes
    assert not any(name in namespace for name in ("safe_io", "mcs_update", "notify_spool"))
