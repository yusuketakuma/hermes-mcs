"""Synthetic installer config and recovery ownership boundaries."""
import errno
import os
from types import SimpleNamespace

import pytest

import lifecycle_io
import runtime_inspection
import safe_io


def test_missing_config_and_existing_regular_owner_file(tmp_path):
    path = tmp_path / "config.json"
    assert lifecycle_io.read_owner_config(path) == {}
    path.write_text('{"runtime_mode":"hermes"}')
    path.chmod(0o644)  # This reader never imposed a private-mode predicate.
    assert lifecycle_io.read_owner_config(path) == {"runtime_mode": "hermes"}


@pytest.mark.parametrize("raw,message", [(b"{", "not valid JSON"),
                                           (b"[]", "must be an object"),
                                           (b"null", "must be an object")])
def test_config_reader_retains_json_error_classification(tmp_path, raw, message):
    path = tmp_path / "config.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match=message):
        lifecycle_io.read_owner_config(path)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "foreign", "oversized"])
def test_config_reader_rejects_unsafe_source_without_widening(tmp_path, monkeypatch, kind):
    path = tmp_path / "config.json"
    if kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text("{}")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_text("{}")
    if kind == "foreign":
        original = os.fstat

        def foreign(fd):
            value = original(fd)
            return SimpleNamespace(st_mode=value.st_mode, st_uid=os.getuid() + 1,
                                   st_size=value.st_size)

        monkeypatch.setattr(lifecycle_io.os, "fstat", foreign)
    with pytest.raises(ValueError):
        lifecycle_io.read_owner_config(path, limit=1 if kind == "oversized" else 65536)


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt, SystemExit])
def test_config_reader_closes_owned_descriptor_if_transfer_fails(tmp_path, monkeypatch, failure):
    path = tmp_path / "config.json"
    path.write_text("{}")
    original_open = os.open
    opened = []
    error = failure("synthetic transfer failure")

    def acquire(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def fail(*args, **kwargs):
        raise error

    try:
        with monkeypatch.context() as patch:
            patch.setattr(lifecycle_io.os, "open", acquire)
            patch.setattr(safe_io.os, "fdopen", fail)
            with pytest.raises(ValueError if failure is OSError else failure) as caught:
                lifecycle_io.read_owner_config(path)
        if failure is OSError:
            assert str(caught.value) == "unreadable"
        else:
            assert caught.value is error
        assert len(opened) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(opened[0])
        assert closed.value.errno == errno.EBADF
    finally:
        # This finite synthetic operation acquires one descriptor only.
        for fd in opened:
            try:
                os.close(fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


def test_unselected_recovery_does_not_probe(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("no selected recovery interpreter must not trigger a probe")

    monkeypatch.setattr(runtime_inspection, "probe_python", forbidden)
    assert runtime_inspection.configured_recovery_problem({}, checkout="synthetic-checkout") is None


@pytest.mark.parametrize("path_problem", [None, "synthetic unsafe selection"])
def test_recovery_selection_path_check_precedes_probe(monkeypatch, path_problem):
    calls = []
    facts = {"python": "3.14.8", "sqlite": "3.51.3", "packages": {}}

    def validate(exe, update_repo, *, checkout):
        calls.append(("path", exe, update_repo, checkout))
        return path_problem

    def probe(exe):
        calls.append(("probe", exe))
        return facts

    def classify(value, *, recovery):
        calls.append(("facts", value, recovery))
        return "synthetic runtime problem"

    monkeypatch.setattr(runtime_inspection, "recovery_python_problem", validate)
    monkeypatch.setattr(runtime_inspection, "probe_python", probe)
    monkeypatch.setattr(runtime_inspection.mcs_runtime, "runtime_problem", classify)
    result = runtime_inspection.configured_recovery_problem(
        {"recovery_python": "synthetic-exe"}, "synthetic-update", checkout="synthetic-checkout")
    assert result == (path_problem or "synthetic runtime problem")
    assert calls == [("path", "synthetic-exe", "synthetic-update", "synthetic-checkout")] + (
        [] if path_problem else [("probe", "synthetic-exe"), ("facts", facts, True)])
