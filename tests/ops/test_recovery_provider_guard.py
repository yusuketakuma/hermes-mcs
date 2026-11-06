"""The independent watchdog keeps unverified provider settings untouched."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path

import pytest

from ops_testkit import patch_lifecycle_lookup


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
                        "MARKER_PATH": data / "update_in_progress.marker",
                        "REPORT_PATH": data / "recovery_report.json",
                        "REPO": tmp_path / "synthetic-repo"}.items():
        patch_lifecycle_lookup(monkeypatch, module, name, str(value))
    return module


@pytest.mark.parametrize("flag,head,target,prev", [
    (True, "target", "target", "previous"),
    (True, "", "target", "previous"),
    (True, "other", "target", "previous"),
    (True, "previous", None, "previous"),
    (False, "previous", "target", "previous"),
    (None, "previous", "target", "previous"),
    (1, "previous", "target", "previous"),
    ("true", "previous", "target", "previous"),
])
@pytest.mark.parametrize("existing_marker", [True, False])
def test_first_split_forward_recovery_holds(watchdog, monkeypatch, flag, head, target, prev, existing_marker):
    marker = Path(watchdog.MARKER_PATH)
    if existing_marker:
        marker.write_text("synthetic marker")
    journal = Path(watchdog.STATE_PATH)
    journal.write_text(json.dumps({"v": 1, "applying": {
        "sha": target, "prev_sha": prev, "provider_settings_migration_required": flag,
    }, "stages": [], "applied": []}))
    before = journal.read_bytes()
    patch_lifecycle_lookup(monkeypatch, watchdog, "_head", lambda: head)
    patch_lifecycle_lookup(monkeypatch, watchdog, "_clean_stale_git_locks", lambda: pytest.fail("hold changed git state"))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_restart_drainers", lambda **_k: pytest.fail("hold restarted drainers"))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_restart_services", lambda *_a, **_k: pytest.fail("hold restarted services"))
    assert watchdog._recover_legacy(if_stale=True) == 1
    assert journal.read_bytes() == before
    assert marker.is_file()
    if existing_marker:
        assert marker.read_text() == "synthetic marker"
    report = json.loads(Path(watchdog.REPORT_PATH).read_text())
    assert report["result"] == "provider_migration_pending"
    assert report["writers_stopped"] is False


def test_first_split_hold_with_unverifiable_marker_reports_no_stop_proof(watchdog, monkeypatch):
    journal = Path(watchdog.STATE_PATH)
    journal.write_text(json.dumps({"v": 1, "applying": {
        "sha": "target", "prev_sha": "previous", "provider_settings_migration_required": True,
    }, "stages": [], "applied": []}))
    marker = Path(watchdog.MARKER_PATH)
    marker.symlink_to(Path(watchdog.DATA) / "missing")
    before = journal.read_bytes()
    patch_lifecycle_lookup(monkeypatch, watchdog, "_head", lambda: "target")
    assert watchdog._recover_legacy() == 1
    assert marker.is_symlink() and journal.read_bytes() == before
    report = json.loads(Path(watchdog.REPORT_PATH).read_text())
    assert report["result"] == "provider_migration_hold_unverified"
    assert report["writers_stopped"] is False


def test_first_split_head_changed_before_classification_is_held(watchdog, monkeypatch):
    journal = Path(watchdog.STATE_PATH)
    journal.write_text(json.dumps({"v": 1, "applying": {
        "sha": "target", "prev_sha": "previous", "provider_settings_migration_required": True,
    }, "stages": [], "applied": []}))
    before = journal.read_bytes()
    heads = iter(("previous", "target"))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_head", lambda: next(heads))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_clean_stale_git_locks", lambda: [])
    patch_lifecycle_lookup(monkeypatch, watchdog, "_clean", lambda: pytest.fail("changed HEAD passed hold"))
    assert watchdog._recover_legacy() == 1
    assert journal.read_bytes() == before
    assert Path(watchdog.MARKER_PATH).is_file()


@pytest.mark.parametrize("kind", ["old_journal", "pre_merge", "rollback"])
def test_first_split_guard_preserves_existing_recovery_routes(watchdog, monkeypatch, kind):
    applying = {"sha": "target", "prev_sha": "previous"}
    if kind != "old_journal":
        applying["provider_settings_migration_required"] = True
    if kind == "rollback":
        applying.update(rollback=True, backup_path="synthetic-backup", schema_bump=False)
    Path(watchdog.STATE_PATH).write_text(json.dumps({
        "v": 1, "applying": applying, "stages": [], "applied": [],
    }))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_head", lambda: "previous" if kind == "pre_merge" else "target")
    class ExistingRouteReached(Exception):
        pass
    def reached():
        raise ExistingRouteReached
    patch_lifecycle_lookup(monkeypatch, watchdog, "_clean_stale_git_locks", reached)
    with pytest.raises(ExistingRouteReached):
        watchdog._recover_legacy()


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
    patch_lifecycle_lookup(monkeypatch, watchdog, "_try_lock", lambda *_: pytest.fail("unverified state took recovery lock"))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_git", lambda *_a, **_k: pytest.fail("unverified state ran git"))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_restart_drainers", lambda **_k: pytest.fail("unverified state restarted"))
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
    patch_lifecycle_lookup(monkeypatch, watchdog, "_lock_provider_editor", activate)
    patch_lifecycle_lookup(monkeypatch, watchdog, "_git", lambda *_a, **_k: pytest.fail("pending ran git"))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_restart_drainers", lambda **_k: pytest.fail("pending restarted"))
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
    patch_lifecycle_lookup(monkeypatch, watchdog, "_try_lock", lambda *_: pytest.fail("unverified state took recovery lock"))
    assert watchdog._recover_legacy() == 2
    assert marker.read_text() == "synthetic marker"


def test_unreadable_remnant_scan_keeps_marker(watchdog, monkeypatch):
    marker = Path(watchdog.MARKER_PATH)
    marker.write_text("synthetic marker")
    def denied(_fd):
        raise PermissionError("synthetic private canary")
    monkeypatch.setattr(watchdog.os, "scandir", denied)
    patch_lifecycle_lookup(monkeypatch, watchdog, "_try_lock", lambda *_: pytest.fail("unverified state took recovery lock"))
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
    patch_lifecycle_lookup(monkeypatch, watchdog, "_remove_marker", checked)
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
        patch_lifecycle_lookup(monkeypatch, watchdog, name, str(value))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_release_scope", lambda _path: nullcontext())
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
    patch_lifecycle_lookup(monkeypatch, watchdog, "_try_lock", lambda *_: pytest.fail("split state took recovery lock"))
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
        patch_lifecycle_lookup(monkeypatch, watchdog, "_release_scope", changed_gate)
    else:
        original = watchdog._lock_provider_editor
        def changed_editor():
            fd = original()
            replace_link()
            return fd
        patch_lifecycle_lookup(monkeypatch, watchdog, "_lock_provider_editor", changed_editor)
    patch_lifecycle_lookup(monkeypatch, watchdog, "_git", lambda *_a, **_k: pytest.fail("changed root ran git"))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_restart_drainers", lambda **_k: pytest.fail("changed root restarted"))
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

    patch_lifecycle_lookup(monkeypatch, watchdog, "_try_lock", change_after_update_lock)
    assert watchdog.recover() == 2
    assert marker.read_text() == "synthetic marker"
    fd = os.open(data / "update.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
