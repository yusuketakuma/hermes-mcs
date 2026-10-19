"""Owned raw descriptors close if stream handoff fails; synthetic temp files only."""
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

import mcs_backup as backup
import backup_evidence
import mcs_operations
import mcs_repair


FAULTS = [OSError, KeyboardInterrupt, SystemExit]


def _closed(fd):
    try:
        with pytest.raises(OSError) as caught:
            os.fstat(fd)
        assert caught.value.errno == errno.EBADF
    finally:
        # A red characterization must not itself retain the captured raw FD.
        try:
            os.close(fd)
        except OSError as error:
            assert error.errno == errno.EBADF


def _release_fds(descriptors):
    try:
        if descriptors:
            fd = descriptors.pop()
            try:
                os.close(fd)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
    finally:
        # Even an unexpected close error must not skip later owned FDs.
        if descriptors:
            _release_fds(descriptors)


@contextmanager
def _handoff_failure(monkeypatch, target, *, ordinal=1, predicate=None, fault=OSError):
    original = target.os.fdopen
    observed = []
    calls = []
    error = fault("synthetic stream handoff failure")

    def fail(fd, *args, **kwargs):
        if predicate is None or predicate(fd):
            calls.append(fd)
            if len(calls) == ordinal:
                observed.append(fd)
                raise error
        return original(fd, *args, **kwargs)

    monkeypatch.setattr(target.os, "fdopen", fail)
    try:
        yield observed, error
    finally:
        _release_fds(observed)


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("operation", ["record_read", "record_lock", "repair_result"])
def test_owned_record_or_result_handoff_closes_raw_fd(tmp_path, monkeypatch, operation, fault):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    records = backup_evidence.open_records(str(private))
    record = private / "backup_state.json"
    record.write_text('{"v":1}')
    record.chmod(0o600)
    before = record.read_bytes()
    target = mcs_repair if operation == "repair_result" else backup_evidence
    with _handoff_failure(monkeypatch, target, fault=fault) as (opened, error):
        with pytest.raises(fault) as caught:
            if operation == "record_read":
                records.load("backup_state.json")
            elif operation == "record_lock":
                with backup_evidence.record_lock(records):
                    pytest.fail("failed handoff acquired record lock")
            else:
                mcs_repair.save_new(private / "result.json", {"synthetic": "complete"})
        assert caught.value is error
        assert len(opened) == 1
        _closed(opened[0])
        assert record.read_bytes() == before
        assert not (private / "result.json").exists()
        assert list(private.glob(".repair-*")) == []


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("ordinal", [1, 2], ids=["staging", "claimed-source"])
def test_promotion_handoff_closes_raw_fd_and_restores_inputs(tmp_path, monkeypatch, ordinal, fault):
    pending_dir, approved_dir = tmp_path / "pending", tmp_path / "approved"
    pending_dir.mkdir(mode=0o700)
    approved_dir.mkdir(mode=0o700)
    pending, approved = pending_dir / "synthetic.json", approved_dir / "synthetic.json"
    pending.write_bytes(b"synthetic pending")
    approved.write_bytes(b"synthetic approved")
    digest = hashlib.sha256(pending.read_bytes()).hexdigest()
    with _handoff_failure(monkeypatch, mcs_operations, ordinal=ordinal, fault=fault) as (opened, error):
        with pytest.raises(fault) as caught:
            with mcs_operations._refstat_promotion(str(pending), str(approved), digest):
                pytest.fail("failed handoff published promotion")
        assert caught.value is error
        assert len(opened) == 1
        _closed(opened[0])
        assert pending.read_bytes() == b"synthetic pending"
        assert approved.read_bytes() == b"synthetic approved"
        assert sorted(p.name for p in pending_dir.iterdir()) == [pending.name]
        assert sorted(p.name for p in approved_dir.iterdir()) == [approved.name]


@pytest.mark.parametrize("fault", FAULTS)
def test_key_fd_handoff_closes_only_owned_duplicate(tmp_path, monkeypatch, capsys, fault):
    private = tmp_path / "records"
    private.mkdir(mode=0o700)
    key = tmp_path / "synthetic-key"
    key.write_bytes(bytes(range(32)))
    original = os.open(key, os.O_RDONLY)
    info = os.fstat(original)
    monkeypatch.setattr(backup_evidence, "load_policy", lambda *a, **k: (
        SimpleNamespace(validate=lambda: None, destination=str(tmp_path / "medium")), False))
    monkeypatch.setattr(backup_evidence, "read_state", lambda *a: {})
    monkeypatch.setattr(backup, "offsite", lambda *a: pytest.fail("failed key handoff reached crypto"))
    with _handoff_failure(
        monkeypatch, backup, fault=fault,
        predicate=lambda fd: (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (info.st_dev, info.st_ino)) as (opened, error):
        args = ["offsite", "--policy", "synthetic-policy", "--state-dir", str(private),
                "--key-fd", str(original), "--snapshot", str(tmp_path / "synthetic.db")]
        try:
            if fault is OSError:
                assert backup.main(args) == 1
                assert json.loads(capsys.readouterr().out) == {"error": "backup_io_or_policy_failed"}
            else:
                with pytest.raises(fault) as caught:
                    backup.main(args)
                assert caught.value is error
            assert len(opened) == 1 and opened[0] != original
            _closed(opened[0])
            assert os.fstat(original).st_ino == info.st_ino
            assert os.read(original, 33) == bytes(range(32))
        finally:
            os.close(original)


def test_successful_key_fd_handoff_preserves_borrowed_descriptor(tmp_path, monkeypatch, capsys):
    private = tmp_path / "records"
    private.mkdir(mode=0o700)
    key = tmp_path / "synthetic-key"
    key.write_bytes(bytes(range(32)))
    original = os.open(key, os.O_RDONLY)
    monkeypatch.setattr(backup_evidence, "load_policy", lambda *a, **k: (
        SimpleNamespace(validate=lambda: None, destination=str(tmp_path / "medium")), False))
    monkeypatch.setattr(backup_evidence, "read_state", lambda *a: {})
    monkeypatch.setattr(backup_evidence, "record_success", lambda *a, **k: None)
    observed = []
    duplicates = []
    duplicate = backup.os.dup

    def dup(fd):
        owned = duplicate(fd)
        duplicates.append(owned)
        return owned

    monkeypatch.setattr(backup.os, "dup", dup)

    def offsite(snapshot, policy, read_key):
        observed.append(read_key())
        return {"synthetic": "complete"}

    monkeypatch.setattr(backup, "offsite", offsite)
    try:
        assert backup.main(["offsite", "--policy", "synthetic-policy", "--state-dir", str(private),
                            "--key-fd", str(original), "--snapshot", "synthetic.db"]) == 0
        assert observed == [bytes(range(32))]
        assert json.loads(capsys.readouterr().out) == {"synthetic": "complete"}
        assert os.fstat(original).st_ino == key.stat().st_ino
        assert len(duplicates) == 1 and duplicates[0] != original
        _closed(duplicates[0])
    finally:
        try:
            _release_fds(duplicates)
        finally:
            os.close(original)


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("cut", [
    "policy_read", "inspection_hash", "copy_source", "copy_target", "encrypted_publish",
    "atomic_text", "atomic_binary", "publish_new", "regular_read", "recovery_tool", "service_manifest",
])
def test_shared_owned_stream_failure_closes_at_actual_entry(tmp_path, monkeypatch, fault, cut):
    import safe_io
    import service_management

    source = tmp_path / "source"
    source.write_bytes(b"synthetic source")
    source.chmod(0o600)
    target = tmp_path / "new"
    medium, scratch, agents, recovery = [tmp_path / name for name in ("medium", "scratch", "agents", "recovery")]
    for folder in (medium, scratch, agents, recovery):
        folder.mkdir(mode=0o700)
    (recovery / "mcs_recover.py").write_bytes(b"synthetic recovery")
    (recovery / "mcs_recover.py").chmod(0o700)
    manifest = tmp_path / "service-manifest.json"
    manifest.write_text("{}")
    manifest.chmod(0o600)
    paths = SimpleNamespace(recovery_dir=str(recovery), agents_dir=str(agents), uid=os.getuid(),
                            checkout=str(tmp_path), manifest_path=str(manifest), hermes_scripts_dir=str(tmp_path))
    monkeypatch.setattr(service_management.mcs_util, "hermes_exe", lambda cfg: "/synthetic/hermes")
    monkeypatch.setattr(service_management.runtime_inspection, "executable_available", lambda exe: False)
    monkeypatch.setattr(service_management.lifecycle_io, "run_bounded", lambda *a, **k: pytest.fail("synthetic case attempted service operation"))
    policy = SimpleNamespace(validate=lambda: None, max_snapshot_bytes=4096, max_snapshots=2,
                             destination=str(medium), scratch_dir=str(scratch), policy_id="synthetic")
    monkeypatch.setattr(backup_evidence, "inventory_snapshot", lambda path: {"last_successful_run": 100})
    monkeypatch.setattr(backup_evidence, "check_fresh", lambda *a: None)
    monkeypatch.setattr(backup, "_openssl", lambda source, dest, key: dest.write_bytes(b"synthetic ciphertext"))
    monkeypatch.setattr(backup, "_mac_key", lambda *a: b"s" * 32)
    monkeypatch.setattr(backup, "_decode", lambda *a: pytest.fail("handoff failure reached decryption"))
    ordinal = 2 if cut == "copy_target" else 3 if cut == "encrypted_publish" else 1
    with _handoff_failure(monkeypatch, safe_io, ordinal=ordinal, fault=fault) as (opened, error):
        def invoke():
            if cut == "policy_read":
                backup_evidence.load_policy(source)
            elif cut == "inspection_hash":
                backup_evidence._inspection_hash(source, 4096)
            elif cut in ("copy_source", "copy_target"):
                backup._copy_private(source, target, 4096)
            elif cut == "encrypted_publish":
                backup.offsite(str(source), policy, lambda: bytes(range(32)))
            elif cut == "atomic_text":
                safe_io.atomic_write(str(target), lambda stream: stream.write("synthetic"))
            elif cut == "atomic_binary":
                safe_io.atomic_write(str(target), lambda stream: stream.write(b"synthetic"), binary=True)
            elif cut == "publish_new":
                safe_io.publish_new_bytes(target, b"synthetic")
            elif cut == "regular_read":
                safe_io.read_regular_nofollow(source, 4096, ValueError)
            elif cut == "recovery_tool":
                return service_management.recovery_owned(paths=paths)
            else:
                return service_management.retire_to_snapshot({}, cfg={}, paths=paths)
        if cut in ("recovery_tool", "service_manifest") and fault is OSError:
            assert invoke() == (False if cut == "recovery_tool" else ["cron_list_unverifiable"])
        else:
            with pytest.raises(fault) as caught:
                invoke()
            assert caught.value is error
        assert len(opened) == 1
        _closed(opened[0])
        assert source.read_bytes() == b"synthetic source"
        assert manifest.read_text() == "{}"
        assert list(medium.iterdir()) == [] and list(scratch.iterdir()) == []
        if cut == "copy_target":
            # Existing caller cleanup owns partial exclusive target removal.
            assert target.read_bytes() == b""
        else:
            assert not target.exists()
        assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
            ["source", "medium", "scratch", "agents", "recovery", "service-manifest.json"]
            + (["new"] if cut == "copy_target" else []))


@pytest.mark.parametrize("fault", FAULTS)
def test_owned_stream_primitive_propagates_identity_and_closes_fd(tmp_path, monkeypatch, fault):
    import safe_io
    source = tmp_path / "synthetic"
    source.write_bytes(b"synthetic")
    fd = os.open(source, os.O_RDONLY)
    # Primitive coverage uses a failure before the normal library accepts ownership.
    with _handoff_failure(monkeypatch, safe_io, fault=fault) as (opened, error):
        try:
            with pytest.raises(fault) as caught:
                safe_io.fdopen_owned(fd, "rb")
            assert caught.value is error
            assert opened == [fd]
            _closed(fd)
        finally:
            # Also covers an unexpected earlier oracle failure before capture.
            try:
                os.close(fd)
            except OSError as closing_error:
                assert closing_error.errno == errno.EBADF


def test_owned_stream_primitive_forwards_text_options_and_stream_closes_once(tmp_path, monkeypatch):
    import safe_io
    destination = tmp_path / "synthetic.txt"
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    original = safe_io.os.fdopen
    observed = []

    def wrap(raw_fd, mode, **kwargs):
        observed.append((raw_fd, mode, kwargs))
        return original(raw_fd, mode, **kwargs)

    monkeypatch.setattr(safe_io.os, "fdopen", wrap)
    stream = None
    try:
        stream = safe_io.fdopen_owned(fd, "w", encoding="utf-8", newline="\n")
        assert not stream.closed
        assert os.fstat(fd).st_ino == destination.stat().st_ino
        stream.write("synthetic\n")
        stream.close()
        assert stream.closed
        _closed(fd)
        assert observed == [(fd, "w", {"closefd": True, "encoding": "utf-8", "newline": "\n"})]
        assert destination.read_bytes() == b"synthetic\n"
    finally:
        try:
            if stream is not None:
                stream.close()
        finally:
            try:
                os.close(fd)
            except OSError as error:
                assert error.errno == errno.EBADF


@pytest.mark.parametrize("fault", FAULTS)
def test_owned_stream_preserves_error_after_library_closes_fd(tmp_path, monkeypatch, fault):
    import safe_io
    source = tmp_path / "synthetic"
    source.write_bytes(b"synthetic")
    fd = os.open(source, os.O_RDONLY)
    error = fault("synthetic post-close construction failure")
    try:
        def close_then_fail(raw_fd, mode, **kwargs):
            assert raw_fd == fd
            os.close(raw_fd)
            raise error

        monkeypatch.setattr(safe_io.os, "fdopen", close_then_fail)
        with pytest.raises(fault) as caught:
            safe_io.fdopen_owned(fd, "r", encoding="utf-8")
        assert caught.value is error
        _closed(fd)
    finally:
        try:
            os.close(fd)
        except OSError as closing_error:
            assert closing_error.errno == errno.EBADF


def test_owned_stream_real_stdlib_invalid_text_encoding_closes_fd(tmp_path):
    import safe_io
    destination = tmp_path / "synthetic.txt"
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with pytest.raises(LookupError):
            safe_io.fdopen_owned(fd, "w", encoding="mcs-synthetic-missing-codec")
        _closed(fd)
        assert destination.read_bytes() == b""
    finally:
        try:
            os.close(fd)
        except OSError as closing_error:
            assert closing_error.errno == errno.EBADF
