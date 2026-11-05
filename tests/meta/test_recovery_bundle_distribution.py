"""Synthetic recovery bundles remain exact, independent and complete across replacement."""
import ast
import hashlib
import importlib.machinery
import importlib.util
import inspect
import json
import os
import stat
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "deployment/recovery"


def _load(path):
    name = "synthetic_recovery_bundle_" + uuid.uuid4().hex
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for path in SOURCE.glob("*.py"):
        shutil.copyfile(path, source / path.name)
    return source


def _pins(entry):
    tree = ast.parse(entry.read_text())
    declaration = next(n for n in tree.body if isinstance(n, ast.Assign)
        and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "SUPPORT_FILES")
    return ast.literal_eval(declaration.value)


def _repin(source):
    entry = source / "mcs_recover.py"
    text = entry.read_text()
    lines = text.splitlines(True)
    declaration = next(n for n in ast.parse(text).body if isinstance(n, ast.Assign)
        and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "SUPPORT_FILES")
    pins = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in _pins(entry)}
    lines[declaration.lineno - 1:declaration.end_lineno] = ["SUPPORT_FILES = " + repr(pins) + "\n"]
    entry.write_text("".join(lines))


def test_source_pins_match_every_companion_and_python39_grammar():
    pins = _pins(SOURCE / "mcs_recover.py")
    assert len(pins) == 4
    for name, expected in pins.items():
        assert hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() == expected
    for path in SOURCE.glob("*.py"):
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))


@pytest.mark.parametrize("fault", ["missing", "modified", "symlink"])
def test_incomplete_source_is_refused_before_path_initialization(tmp_path, monkeypatch, fault):
    source = _source(tmp_path)
    private = tmp_path / "must-not-be-created"
    monkeypatch.setenv("MCS_ROOT", str(private))
    helper = source / "mcs_recovery_core.py"
    raw = helper.read_bytes()
    helper.unlink()
    if fault == "modified":
        helper.write_bytes(raw + b"\n# synthetic different generation\n")
    elif fault == "symlink":
        target = source / "synthetic-other.py"
        target.write_bytes(raw)
        helper.symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        _load(source / "mcs_recover.py")
    assert not private.exists()
    assert not list(source.glob("runtime-*"))


def test_staged_generations_preserve_previous_entrypoint_and_isolate_state(tmp_path, monkeypatch):
    source = _source(tmp_path)
    monkeypatch.setenv("MCS_ROOT", str(tmp_path / "private-mcs"))
    installer = _load(source / "prepare_recovery.py")
    destination = tmp_path / "independent"
    destination.mkdir(mode=0o700)
    entry = source / "mcs_recover.py"
    old_entry = entry.read_bytes()
    old_generation = installer.prepare(entry, destination)
    assert installer.prepare(entry, destination) == old_generation
    (destination / "mcs_recover.py.prev").write_bytes(old_entry)
    helper = source / "mcs_recovery_database.py"
    helper.write_bytes(helper.read_bytes() + b"\n# synthetic new generation\n")
    _repin(source)
    new_generation = installer.prepare(entry, destination)
    assert new_generation != old_generation
    shutil.copyfile(entry, destination / "mcs_recover.py")
    old = _load(destination / "mcs_recover.py.prev")
    new = _load(destination / "mcs_recover.py")
    assert old.SUPPORT_FILES != new.SUPPORT_FILES
    assert old.mcs_recovery_core is not new.mcs_recovery_core
    old.mcs_recovery_core.DATA = "synthetic-old-only"
    assert new.mcs_recovery_core.DATA != old.mcs_recovery_core.DATA
    assert (destination / ("runtime-" + old_generation)).is_dir()
    assert (destination / ("runtime-" + new_generation)).is_dir()
    assert (destination / "mcs_recover.py.prev").read_bytes() == old_entry
    result = subprocess.run([sys.executable, "-I", str(destination / "mcs_recover.py"), "--help"],
                            capture_output=True, text=True, check=True)
    assert "--if-stale" in result.stdout


def test_partial_published_bundle_never_replaces_or_reuses_an_entrypoint(tmp_path):
    source = _source(tmp_path)
    installer = _load(source / "prepare_recovery.py")
    destination = tmp_path / "independent"
    destination.mkdir(mode=0o700)
    generation = installer.prepare(source / "mcs_recover.py", destination)
    entry = destination / "mcs_recover.py"
    shutil.copyfile(source / "mcs_recover.py", entry)
    previous = entry.read_bytes()
    (destination / ("runtime-" + generation) / "mcs_recovery_services.py").unlink()
    with pytest.raises((OSError, ValueError)):
        installer.prepare(source / "mcs_recover.py", destination)
    with pytest.raises((OSError, ValueError)):
        _load(entry)
    assert entry.read_bytes() == previous
    assert not list(destination.glob(".runtime-stage-*"))


def test_loaded_source_and_frozen_child_survive_disk_replacement(tmp_path, monkeypatch):
    source = _source(tmp_path)
    monkeypatch.setenv("MCS_ROOT", str(tmp_path / "private-mcs"))
    recovery = _load(source / "mcs_recover.py")
    function = recovery.mcs_recovery_gateway._gateway_restart_run
    original = inspect.getsource(function)
    (source / "mcs_recovery_gateway.py").write_text("# synthetic replacement, never imported\n")
    assert inspect.getsource(function) == original
    assert recovery.mcs_recovery_gateway.__loader__.get_source(function.__module__).endswith("\n")
    code = recovery._GATEWAY_RESTART_PROGRAM.rsplit("sys.exit(", 1)[0]
    data = tmp_path / "synthetic-child"
    code += "\n_gateway_restart_report(" + repr(str(data)) + ", 'synthetic-closure-loaded')\n"
    subprocess.run([sys.executable, "-I", "-c", code], check=True, capture_output=True, text=True)
    assert json.loads((data / "gateway_restart.json").read_text())["status"] == "synthetic-closure-loaded"
    assert not (tmp_path / "private-mcs").exists()


def test_bundle_directory_flush_failure_keeps_old_entrypoint(tmp_path, monkeypatch):
    source = _source(tmp_path)
    installer = _load(source / "prepare_recovery.py")
    destination = tmp_path / "independent"
    destination.mkdir(mode=0o700)
    entry = destination / "mcs_recover.py"
    entry.write_bytes(b"# synthetic previous recovery\n")
    previous = entry.read_bytes()
    real_fsync = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic directory flush failed")
        return real_fsync(fd)
    monkeypatch.setattr(installer.os, "fsync", fail_directory)
    with pytest.raises(OSError, match="synthetic directory flush failed"):
        installer.prepare(source / "mcs_recover.py", destination)
    assert entry.read_bytes() == previous
    assert not list(destination.glob("runtime-*"))
    assert not list(destination.glob(".runtime-stage-*"))


def test_entry_replacement_after_preparation_never_changes_publication(tmp_path, monkeypatch):
    source = _source(tmp_path)
    monkeypatch.setenv("MCS_ROOT", str(tmp_path / "private-mcs"))
    publisher = _load(source / "prepare_recovery.py")
    destination = tmp_path / "independent"
    destination.mkdir(mode=0o700)
    target, previous = destination / "mcs_recover.py", destination / "mcs_recover.py.prev"
    target.write_bytes(b"# synthetic old current\n")
    previous.write_bytes(b"# synthetic older previous\n")
    entry = source / "mcs_recover.py"
    captured = entry.read_bytes()
    original_prepare = publisher._prepare_bundle
    def swap_after_prepare(path, folder, raw):
        result = original_prepare(path, folder, raw)
        entry.write_bytes(b"raise RuntimeError('synthetic unprepared entry')\n")
        return result
    monkeypatch.setattr(publisher, "_prepare_bundle", swap_after_prepare)
    assert publisher.install(entry, destination) is True
    assert target.read_bytes() == captured
    assert previous.read_bytes() == b"# synthetic old current\n"
    _load(target)
    monkeypatch.setattr(publisher, "_prepare_bundle", original_prepare)
    entry.write_bytes(captured)
    assert publisher.install(entry, destination) is False
    assert previous.read_bytes() == b"# synthetic old current\n"
    entry.write_bytes(captured + b"\n# synthetic next entry\n")
    saved_target, saved_previous = target.read_bytes(), previous.read_bytes()
    original_sync = publisher._sync_directory
    calls = []
    def fail_before_publication(path):
        calls.append(path)
        if len(calls) == 3:
            raise OSError("synthetic publish directory flush failed")
        return original_sync(path)
    monkeypatch.setattr(publisher, "_sync_directory", fail_before_publication)
    with pytest.raises(OSError, match="synthetic publish directory flush failed"):
        publisher.install(entry, destination)
    assert target.read_bytes() == saved_target and previous.read_bytes() == saved_previous
    assert not list(destination.glob(".entry-stage-*"))
