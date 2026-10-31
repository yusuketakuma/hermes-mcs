"""The independent watchdog keeps unverified provider settings untouched."""
import fcntl
import importlib.util
import os
from pathlib import Path

import pytest


@pytest.fixture
def watchdog(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "deployment" / "recovery" / "mcs_recover.py"
    spec = importlib.util.spec_from_file_location("synthetic_provider_recovery", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tmp_path.chmod(0o700)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    for name, value in {"DATA": data, "STATE_PATH": data / "update_state.json",
                        "UPDATE_LOCK": data / "update.lock", "RUN_LOCK": data / "run.lock",
                        "MARKER_PATH": data / "update_in_progress.marker"}.items():
        monkeypatch.setattr(module, name, str(value))
    return module


@pytest.mark.parametrize("name,kind", [
    (".provider-settings-pending.json", "file"),
    (".provider-settings-pending.json", "symlink"),
    (".provider-settings-authority.json", "file"),
    ("provider_settings", "directory"),
    ("provider_settings", "symlink"),
    (".provider-settings-backup-synthetic-config.bin", "file"),
    (".provider-settings-stage-synthetic", "directory"),
    (".provider-settings-stage-synthetic", "symlink"),
])
@pytest.mark.parametrize("with_journal", [False, True])
def test_unverified_provider_state_blocks_recovery_before_locks(watchdog, monkeypatch, capsys, name, kind, with_journal):
    data = Path(watchdog.DATA)
    marker = data / "update_in_progress.marker"
    marker.write_text("synthetic marker")
    journal = data / "update_state.json"
    if with_journal:
        journal.write_text('{"v":1,"applying":null,"stages":[],"applied":[]}')
    suspect = data / name
    if kind == "symlink":
        suspect.symlink_to(data / "synthetic-missing")
    elif kind == "directory":
        suspect.mkdir()
    else:
        suspect.write_text("synthetic private canary")
    original = (marker.read_bytes(), journal.read_bytes() if with_journal else None)
    monkeypatch.setattr(watchdog, "_try_lock", lambda *_: pytest.fail("unverified state took recovery lock"))
    monkeypatch.setattr(watchdog, "_git", lambda *_a, **_k: pytest.fail("unverified state ran git"))
    monkeypatch.setattr(watchdog, "_restart_drainers", lambda **_k: pytest.fail("unverified state restarted"))
    assert watchdog._recover_legacy() == 2
    assert (marker.read_bytes(), journal.read_bytes() if with_journal else None) == original
    assert os.path.lexists(suspect)
    captured = capsys.readouterr()
    assert "synthetic private canary" not in captured.out + captured.err


def test_pending_appearing_after_initial_check_keeps_marker_and_journal(watchdog, monkeypatch):
    data = Path(watchdog.DATA)
    marker = data / "update_in_progress.marker"
    marker.write_text("synthetic marker")
    original = watchdog._lock_provider_editor
    def activate():
        fd = original()
        (data / ".provider-settings-pending.json").write_text("synthetic pending")
        return fd
    monkeypatch.setattr(watchdog, "_lock_provider_editor", activate)
    monkeypatch.setattr(watchdog, "_git", lambda *_a, **_k: pytest.fail("pending ran git"))
    monkeypatch.setattr(watchdog, "_restart_drainers", lambda **_k: pytest.fail("pending restarted"))
    assert watchdog._recover_legacy() == 2
    assert marker.read_text() == "synthetic marker"
    assert (data / ".provider-settings-pending.json").read_text() == "synthetic pending"
    assert not Path(watchdog.STATE_PATH).exists()


def test_unreadable_provider_marker_state_is_not_legacy(watchdog, monkeypatch):
    marker = Path(watchdog.MARKER_PATH)
    marker.write_text("synthetic marker")
    original = watchdog.os.stat
    def denied(path, *args, **kwargs):
        if path == ".provider-settings-pending.json":
            raise PermissionError("synthetic private canary")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(watchdog.os, "stat", denied)
    monkeypatch.setattr(watchdog, "_try_lock", lambda *_: pytest.fail("unverified state took recovery lock"))
    assert watchdog._recover_legacy() == 2
    assert marker.read_text() == "synthetic marker"


def test_unreadable_remnant_scan_keeps_marker(watchdog, monkeypatch):
    marker = Path(watchdog.MARKER_PATH)
    marker.write_text("synthetic marker")
    def denied(_fd):
        raise PermissionError("synthetic private canary")
    monkeypatch.setattr(watchdog.os, "scandir", denied)
    monkeypatch.setattr(watchdog, "_try_lock", lambda *_: pytest.fail("unverified state took recovery lock"))
    assert watchdog._recover_legacy() == 2
    assert marker.read_text() == "synthetic marker"


def test_busy_editor_keeps_legacy_orphan_marker(watchdog):
    root = Path(watchdog.DATA).parent
    marker = Path(watchdog.MARKER_PATH)
    marker.write_text("synthetic marker")
    fd = os.open(root / "config.json.edit.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert watchdog._recover_legacy() == 2
        assert marker.read_text() == "synthetic marker"
    finally:
        os.close(fd)


@pytest.mark.parametrize("damage", ["symlink", "nonempty", "loose"])
def test_untrusted_editor_lock_keeps_marker(watchdog, damage):
    root = Path(watchdog.DATA).parent
    marker = Path(watchdog.MARKER_PATH)
    marker.write_text("synthetic marker")
    lock = root / "config.json.edit.lock"
    if damage == "symlink":
        lock.symlink_to(root / "synthetic-target")
    else:
        lock.write_text("x" if damage == "nonempty" else "")
        lock.chmod(0o666 if damage == "loose" else 0o600)
    assert watchdog._recover_legacy() == 2
    assert marker.read_text() == "synthetic marker" and os.path.lexists(lock)


def test_legacy_orphan_cleanup_holds_editor_lock(watchdog, monkeypatch):
    root = Path(watchdog.DATA).parent
    marker = Path(watchdog.MARKER_PATH)
    marker.write_text("synthetic marker")
    original = watchdog._remove_marker
    def checked():
        fd = os.open(root / "config.json.edit.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        original()
    monkeypatch.setattr(watchdog, "_remove_marker", checked)
    assert watchdog._recover_legacy() == 0
    assert not marker.exists()


def _linked_root(watchdog, tmp_path, monkeypatch):
    from contextlib import nullcontext

    target = tmp_path / "synthetic-target"
    target.mkdir(mode=0o700)
    data = target / "data"
    data.mkdir(mode=0o700)
    link = tmp_path / "synthetic-root"
    link.symlink_to(target, target_is_directory=True)
    logical_data = link / "data"
    for name, value in {"DATA": logical_data, "STATE_PATH": logical_data / "update_state.json",
                        "UPDATE_LOCK": logical_data / "update.lock", "RUN_LOCK": logical_data / "run.lock",
                        "MARKER_PATH": logical_data / "update_in_progress.marker"}.items():
        monkeypatch.setattr(watchdog, name, str(value))
    monkeypatch.setattr(watchdog, "_release_scope", lambda _path: nullcontext())
    return target, link, data


def test_private_link_legacy_orphan_recovery(watchdog, tmp_path, monkeypatch):
    _, _, data = _linked_root(watchdog, tmp_path, monkeypatch)
    marker = data / "update_in_progress.marker"
    marker.write_text("synthetic marker")
    assert watchdog.recover() == 0
    assert not marker.exists()


def test_private_link_split_still_holds(watchdog, tmp_path, monkeypatch):
    _, _, data = _linked_root(watchdog, tmp_path, monkeypatch)
    marker = data / "update_in_progress.marker"
    marker.write_text("synthetic marker")
    (data / ".provider-settings-authority.json").write_text("synthetic authority")
    monkeypatch.setattr(watchdog, "_try_lock", lambda *_: pytest.fail("split state took recovery lock"))
    assert watchdog.recover() == 2
    assert marker.read_text() == "synthetic marker"


@pytest.mark.parametrize("when", ["gate", "editor"])
def test_private_link_replaced_with_same_target_is_held(watchdog, tmp_path, monkeypatch, when):
    from contextlib import contextmanager

    target, link, data = _linked_root(watchdog, tmp_path, monkeypatch)
    marker = data / "update_in_progress.marker"
    marker.write_text("synthetic marker")
    journal = data / "update_state.json"
    journal.write_text('{"v":1,"applying":null,"stages":[],"applied":[]}')
    old_journal = journal.read_bytes()

    def replace_link():
        link.rename(link.with_name("synthetic-old-link"))
        link.symlink_to(target, target_is_directory=True)

    if when == "gate":
        @contextmanager
        def changed_gate(_path):
            replace_link()
            yield
        monkeypatch.setattr(watchdog, "_release_scope", changed_gate)
    else:
        original = watchdog._lock_provider_editor
        def changed_editor():
            fd = original()
            replace_link()
            return fd
        monkeypatch.setattr(watchdog, "_lock_provider_editor", changed_editor)
    monkeypatch.setattr(watchdog, "_git", lambda *_a, **_k: pytest.fail("changed root ran git"))
    monkeypatch.setattr(watchdog, "_restart_drainers", lambda **_k: pytest.fail("changed root restarted"))
    assert watchdog.recover() == 2
    assert marker.read_text() == "synthetic marker"
    assert journal.read_bytes() == old_journal


def test_link_replacement_releases_previously_acquired_update_lock(watchdog, tmp_path, monkeypatch):
    target, link, data = _linked_root(watchdog, tmp_path, monkeypatch)
    marker = data / "update_in_progress.marker"
    marker.write_text("synthetic marker")
    original = watchdog._try_lock

    def change_after_update_lock(path):
        fd = original(path)
        if path == watchdog.UPDATE_LOCK and fd is not None:
            link.rename(link.with_name("synthetic-old-link"))
            link.symlink_to(target, target_is_directory=True)
        return fd

    monkeypatch.setattr(watchdog, "_try_lock", change_after_update_lock)
    assert watchdog.recover() == 2
    assert marker.read_text() == "synthetic marker"
    fd = os.open(data / "update.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
