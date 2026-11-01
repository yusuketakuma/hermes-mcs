"""Synthetic prefix imports retain originals through crashes, old FDs and source drift."""
import errno
import json
import os

import pytest

import run_log


@pytest.fixture
def data(tmp_path):
    path = tmp_path / "data"
    path.mkdir(mode=0o700)
    return path


def legacy(data, name="run.log", raw=b"synthetic old\n"):
    path = data / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def test_import_is_ordered_idempotent_and_originals_are_kept(data):
    rotated = legacy(data, "run.log.1", b"older\n")
    active = legacy(data, "run.log", b"later\n")
    result = run_log.migrate(data, ["run.log.1", "run.log"])
    assert result["originals_retained"]
    assert (data / run_log.LOG).read_bytes() == b"older\nlater\n"
    assert rotated.read_bytes() == b"older\n" and active.read_bytes() == b"later\n"
    assert run_log.migrate(data, ["run.log.1", "run.log"])["imported"] == []
    backups = [path for path in (data / run_log.BACKUPS).iterdir() if path.name != run_log.STATE]
    assert sorted(path.read_bytes() for path in backups) == sorted([b"older\n", b"later\n"])
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in backups)


def test_old_open_fd_tail_is_imported_once_without_truncation(data):
    source = legacy(data)
    fd = os.open(source, os.O_WRONLY | os.O_APPEND)
    try:
        run_log.migrate(data, ["run.log"])
        run_log.append(data, b"new writer\n")
        os.write(fd, b"old fd tail\n")
        run_log.migrate(data, ["run.log"])
        run_log.migrate(data, ["run.log"])
    finally:
        os.close(fd)
    assert source.read_bytes() == b"synthetic old\nold fd tail\n"
    assert (data / run_log.LOG).read_bytes() == b"synthetic old\nnew writer\nold fd tail\n"


@pytest.mark.parametrize("drift", ["inode", "truncate", "rewrite", "missing"])
def test_source_drift_holds_without_losing_prior_import(data, drift):
    source = legacy(data)
    run_log.migrate(data, ["run.log"])
    previous = (data / run_log.LOG).read_bytes()
    if drift == "inode":
        source.rename(data / "retained-original")
        legacy(data, raw=b"different inode\n")
    elif drift == "truncate":
        source.write_bytes(b"short")
    elif drift == "rewrite":
        source.write_bytes(b"x" * source.stat().st_size)
    else:
        source.rename(data / "retained-original")
    with pytest.raises(run_log.RunLogError, match="legacy_(changed|missing)"):
        run_log.migrate(data, ["run.log"])
    assert (data / run_log.LOG).read_bytes() == previous


@pytest.mark.parametrize("phase", ["before_append", "partial_append", "after_append"])
def test_import_crash_recovery_has_no_duplicate_or_missing_bytes(data, monkeypatch, phase):
    source = legacy(data, raw=b"one\ntwo\n")
    save, write = run_log._save, run_log.os.write
    prepared, target_writes = False, 0

    def crash_save(directory, backup, state):
        nonlocal prepared
        result = save(directory, backup, state)
        if state["pending"] is not None:
            prepared = True
            if phase == "before_append":
                raise KeyboardInterrupt
        elif phase == "after_append":
            # Failure before publishing the committed state keeps the durable prepared entry.
            return result
        return result

    def crash_write(fd, raw):
        nonlocal target_writes
        if prepared and os.fstat(fd).st_ino == (data / run_log.LOG).stat().st_ino:
            if phase == "partial_append":
                if target_writes:
                    raise KeyboardInterrupt
                target_writes += 1
                return write(fd, raw[:3])
        return write(fd, raw)

    def before_commit(directory, backup, state):
        if prepared and state["pending"] is None and phase == "after_append":
            raise KeyboardInterrupt
        return crash_save(directory, backup, state)

    with monkeypatch.context() as patch:
        patch.setattr(run_log, "_save", before_commit)
        patch.setattr(run_log.os, "write", crash_write)
        with pytest.raises(KeyboardInterrupt):
            run_log.migrate(data, ["run.log"])
    run_log.append(data, b"next\n")
    assert run_log.migrate(data, ["run.log"])["imported"] == []
    assert (data / run_log.LOG).read_bytes() == b"one\ntwo\nnext\n"
    assert source.read_bytes() == b"one\ntwo\n"


def test_partial_import_refuses_foreign_destination_tail(data, monkeypatch):
    legacy(data)
    save = run_log._save

    def crash(directory, backup, state):
        save(directory, backup, state)
        if state["pending"] is not None:
            raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(run_log, "_save", crash)
        with pytest.raises(KeyboardInterrupt):
            run_log.migrate(data, ["run.log"])
    (data / run_log.LOG).write_bytes(b"uncooperative destination write\n")
    with pytest.raises(run_log.RunLogError, match="pending_changed"):
        run_log.append(data, b"next\n")
    assert (data / run_log.LOG).read_bytes() == b"uncooperative destination write\n"


def test_capacity_failure_before_preparation_keeps_original_and_destination(data, monkeypatch):
    source = legacy(data)
    run_log.append(data, b"existing\n")
    state = (data / run_log.STATE).read_bytes()
    write = run_log.os.write

    def fail(fd, raw):
        if raw == b"synthetic old\n":
            raise OSError(errno.ENOSPC, "synthetic capacity")
        return write(fd, raw)

    with monkeypatch.context() as patch:
        patch.setattr(run_log.os, "write", fail)
        with pytest.raises(OSError):
            run_log.migrate(data, ["run.log"])
    assert source.read_bytes() == b"synthetic old\n"
    assert (data / run_log.LOG).read_bytes() == b"existing\n"
    assert (data / run_log.STATE).read_bytes() == state
    run_log.migrate(data, ["run.log"])
    assert (data / run_log.LOG).read_bytes() == b"existing\nsynthetic old\n"


def test_missing_transaction_file_is_not_misreported_as_an_absent_legacy_source(data, monkeypatch):
    source = legacy(data)

    def fail(*_):
        raise FileNotFoundError("synthetic staging disappearance")

    monkeypatch.setattr(run_log, "_save", fail)
    with pytest.raises(FileNotFoundError):
        run_log.migrate(data, ["run.log"])
    assert source.read_bytes() == b"synthetic old\n"


@pytest.mark.parametrize("name", ["../outside.log", "mcs.log", "unknown.log", "../../.hermes/logs/llamacpp.log"])
def test_import_only_allows_known_mcs_owned_sources(data, name):
    with pytest.raises(ValueError, match="name_invalid"):
        run_log.migrate(data, [name])
    assert list(data.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "writable"])
def test_untrusted_legacy_source_is_not_consumed(data, tmp_path, kind):
    target = legacy(data, "protected.bin")
    source = data / "run.log"
    if kind == "symlink":
        source.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, source)
    elif kind == "fifo":
        os.mkfifo(source, 0o600)
    else:
        source = legacy(data)
        source.chmod(0o666)
    with pytest.raises((OSError, run_log.RunLogError)):
        run_log.migrate(data, ["run.log"])
    assert target.read_bytes() == b"synthetic old\n"
    assert not (data / run_log.LOG).exists()
    if (data / run_log.STATE).exists():
        assert json.loads((data / run_log.STATE).read_text())["sources"] == {}


def test_readable_legacy_permissions_are_narrowed_without_changing_bytes(data):
    source = legacy(data)
    source.chmod(0o644)
    run_log.migrate(data, ["run.log"])
    assert source.stat().st_mode & 0o777 == 0o600
    assert source.read_bytes() == (data / run_log.LOG).read_bytes() == b"synthetic old\n"


def test_only_mcs_home_llama_log_is_imported_not_shared_hermes(data):
    logs = data.parent / "logs"
    logs.mkdir(mode=0o700)
    source = logs / "llamacpp.log"
    source.write_bytes(b"MCS-owned synthetic llama output\n")
    source.chmod(0o600)
    shared = data.parent / ".hermes" / "logs"
    shared.mkdir(parents=True)
    (shared / "llamacpp.log").write_bytes(b"shared log must stay untouched")
    run_log.migrate(data)
    assert (data / run_log.LOG).read_bytes() == source.read_bytes()
    assert (shared / "llamacpp.log").read_bytes() == b"shared log must stay untouched"
