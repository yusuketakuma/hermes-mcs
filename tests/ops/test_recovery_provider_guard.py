"""The independent watchdog keeps unverified provider settings untouched."""
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
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


# --- settled split settings (F-R1) -----------------------------------------

CANARY = "synthetic-provider-canary"
HEAD = "a" * 40


def _private(path, raw):
    path.write_bytes(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


COMMON = {"cloud_llm": {"mode": "off", "provider_budgets": {}}, "semantic": {"mode": "off"},
          "note": "合成", "drug_map": {"synthetic": True}, "install_args": ["--synthetic"]}


def _settled(data, *, remnants=True, authority=None, common=COMMON):
    from provider_settings import common_policy_digest

    (data.parent / "config.json").write_text(json.dumps(common, ensure_ascii=False), encoding="utf-8")
    directory = data / "provider_settings"
    directory.mkdir(mode=0o700)
    hashes = {name: _private(directory / (name + ".json"), ('{"note": "%s-%s"}' % (CANARY, name)).encode())
              for name in ("anthropic", "openai", "jev")}
    value = {"version": 2, "state": "committed", "files": hashes, "common_policy_sha256": common_policy_digest(common)}
    if authority is not None:
        value = authority(value)
    _private(data / ".provider-settings-authority.json", json.dumps(value).encode())
    if remnants:
        _private(data / ".provider-settings-backup-synthetic-config.bin", CANARY.encode())
        (data / ".provider-settings-stage-synthetic").mkdir(mode=0o700)
        _private(data / ".provider-settings-stage-synthetic" / "jev", CANARY.encode())
    return directory


def _checkout(monkeypatch, watchdog, *, head=HEAD, missing=(), foreign=(), untracked=""):
    def git(args, *_a, **_k):
        if args[0] == "cat-file":
            return subprocess.CompletedProcess(args, 1 if args[2].split(":", 1)[1] in missing else 0, "", "")
        if args[0] == "ls-files":
            return subprocess.CompletedProcess(args, 0, untracked, "")
        pytest.fail("split route ran an unexpected git command: " + " ".join(args))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_head", lambda: head)
    patch_lifecycle_lookup(monkeypatch, watchdog, "_git", git)
    patch_lifecycle_lookup(monkeypatch, watchdog, "_local_changes_outside",
                           lambda _p, _t: None if foreign is None else list(foreign))


def _no_effects(monkeypatch, watchdog):
    patch_lifecycle_lookup(monkeypatch, watchdog, "_restart_drainers", lambda **_k: pytest.fail("hold restarted drainers"))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_restart_services", lambda *_a, **_k: pytest.fail("hold restarted services"))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_restore_db", lambda *_a, **_k: pytest.fail("hold restored the database"))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_notify", lambda *_a, **_k: pytest.fail("hold sent a notice"))


def test_settled_split_with_remnants_is_verified(watchdog):
    _settled(Path(watchdog.DATA))
    assert watchdog._provider_settings_state() == "split"


def test_settled_split_with_nothing_to_recover_is_silent(watchdog, monkeypatch, capsys):
    _settled(Path(watchdog.DATA))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_try_lock", lambda *_: pytest.fail("took recovery lock"))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_git", lambda *_a, **_k: pytest.fail("ran git"))
    assert watchdog._recover_legacy(if_stale=True) == 0
    assert not Path(watchdog.REPORT_PATH).exists()
    assert capsys.readouterr().out == ""


def test_pending_with_nothing_to_recover_is_not_reported(watchdog):
    data = Path(watchdog.DATA)
    _settled(data)
    _private(data / ".provider-settings-pending.json", CANARY.encode())
    assert watchdog._recover_legacy(if_stale=True) == 0
    assert not Path(watchdog.REPORT_PATH).exists()


def test_settled_split_orphan_marker_is_removed_and_hold_cleared(watchdog, monkeypatch):
    _settled(Path(watchdog.DATA))
    marker = Path(watchdog.MARKER_PATH)
    marker.write_text("synthetic marker")
    Path(watchdog.REPORT_PATH).write_text(json.dumps({
        "result": "provider_settings_unverified", "held": True, "drainers_key": "synthetic-key"}))
    _checkout(monkeypatch, watchdog)
    assert watchdog._recover_legacy(if_stale=True) == 0
    assert not marker.exists()
    report = json.loads(Path(watchdog.REPORT_PATH).read_text())
    assert report["result"] == "hold_cleared" and "held" not in report
    assert report["drainers_key"] == "synthetic-key"


def test_settled_split_pre_merge_reaches_existing_route(watchdog, monkeypatch):
    _settled(Path(watchdog.DATA))
    Path(watchdog.STATE_PATH).write_text(json.dumps({
        "v": 1, "applying": {"sha": "b" * 40, "prev_sha": HEAD}, "stages": [], "applied": []}))
    _checkout(monkeypatch, watchdog)

    class ExistingRouteReached(Exception):
        pass

    def reached():
        raise ExistingRouteReached
    patch_lifecycle_lookup(monkeypatch, watchdog, "_clean_stale_git_locks", reached)
    with pytest.raises(ExistingRouteReached):
        watchdog._recover_legacy()


def _damage(name, data):
    in_flight = {"pending": ".provider-settings-pending.json",
                 "bootstrap_fresh": ".provider-bootstrap-fresh.json",
                 "bootstrap_complete": ".provider-bootstrap-complete.json"}
    authority = {"state": lambda v: dict(v, state="pending"),
                 "version_bool": lambda v: dict(v, version=True),
                 "extra_key": lambda v: dict(v, note=CANARY),
                 "configuration_state": lambda v: dict(v, configuration_state="other")}
    if name == "remnant_only":
        _private(data / ".provider-settings-backup-synthetic-config.bin", CANARY.encode())
        return
    directory = _settled(data, authority=authority.get(name))
    if name == "hash":
        _private(directory / "jev.json", b'{"note": "changed"}')
    elif name == "missing_file":
        (directory / "openai.json").unlink()
    elif name == "loose_authority":
        (data / ".provider-settings-authority.json").chmod(0o644)
    elif name == "loose_directory":
        directory.chmod(0o755)
    elif name == "hard_link":
        os.link(directory / "anthropic.json", data / "synthetic-link")
    elif name == "symlink_file":
        (directory / "jev.json").rename(data / "synthetic-jev")
        (directory / "jev.json").symlink_to(data / "synthetic-jev")
    elif name == "authority_missing":
        (data / ".provider-settings-authority.json").unlink()
    elif name in in_flight:
        _private(data / in_flight[name], CANARY.encode())
    elif name == "config_edited":
        (data.parent / "config.json").write_text(json.dumps(dict(COMMON, note=CANARY)))
    elif name == "config_missing":
        (data.parent / "config.json").unlink()
    elif name.startswith("config_ambiguous"):
        overlap = {"config_ambiguous_model": {"cloud_llm": {"mode": "off", "model": CANARY}},
                   "config_ambiguous_budget": {"cloud_llm": {"mode": "off", "provider_budgets": {"openai": {}}}},
                   "config_ambiguous_jev": {"semantic": {"mode": "off", "until": CANARY}}}[name]
        (data / ".provider-settings-authority.json").unlink()
        shutil.rmtree(directory)
        _settled(data, remnants=False, common=dict(COMMON, **overlap))


@pytest.mark.parametrize("name,expected", [
    ("hash", "provider_settings_unverified"), ("missing_file", "provider_settings_unverified"),
    ("state", "provider_settings_unverified"), ("version_bool", "provider_settings_unverified"),
    ("extra_key", "provider_settings_unverified"), ("configuration_state", "provider_settings_unverified"),
    ("loose_authority", "provider_settings_unverified"), ("loose_directory", "provider_settings_unverified"),
    ("hard_link", "provider_settings_unverified"), ("symlink_file", "provider_settings_unverified"),
    ("authority_missing", "provider_settings_unverified"), ("remnant_only", "provider_settings_unverified"),
    ("pending", "provider_settings_pending"), ("bootstrap_fresh", "provider_settings_pending"),
    ("bootstrap_complete", "provider_settings_pending"),
    ("config_edited", "provider_settings_unverified"), ("config_missing", "provider_settings_unverified"),
    ("config_ambiguous_model", "provider_settings_unverified"),
    ("config_ambiguous_budget", "provider_settings_unverified"),
    ("config_ambiguous_jev", "provider_settings_unverified"),
])
def test_unsettled_split_holds_before_locks_and_reports_once(watchdog, monkeypatch, capsys, name, expected):
    data = Path(watchdog.DATA)
    _damage(name, data)
    marker = Path(watchdog.MARKER_PATH)
    marker.write_text("synthetic marker")
    patch_lifecycle_lookup(monkeypatch, watchdog, "_try_lock", lambda *_: pytest.fail("unsettled state took recovery lock"))
    patch_lifecycle_lookup(monkeypatch, watchdog, "_git", lambda *_a, **_k: pytest.fail("unsettled state ran git"))
    _no_effects(monkeypatch, watchdog)
    assert watchdog._recover_legacy(if_stale=True) == 2
    report_path = Path(watchdog.REPORT_PATH)
    report = json.loads(report_path.read_text())
    assert report["result"] == expected and report["held"] is True and report["writers_stopped"] is False
    first = (report_path.stat().st_ino, report_path.read_bytes())
    assert watchdog._recover_legacy(if_stale=True) == 2
    assert (report_path.stat().st_ino, report_path.read_bytes()) == first
    assert marker.read_text() == "synthetic marker"
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err + report_path.read_text()
    assert str(data) not in report_path.read_text()


def test_split_namespace_change_during_check_holds(watchdog, monkeypatch):
    data = Path(watchdog.DATA)
    _settled(data)
    original = watchdog._provider_snapshot
    calls = []

    def changing(*fds):
        result = original(*fds)
        if not calls:
            _private(data / ".provider-settings-backup-synthetic-new.bin", CANARY.encode())
        calls.append(result)
        return result
    monkeypatch.setattr(watchdog, "_provider_snapshot", changing)
    assert watchdog._provider_settings_state() == "provider_settings_unverified"


def test_unrelated_data_churn_keeps_split_verified(watchdog, monkeypatch):
    data = Path(watchdog.DATA)
    _settled(data)
    original = watchdog._provider_snapshot

    def churn(*fds):
        result = original(*fds)
        (data / "synthetic-runtime-file").write_text("x")
        return result
    monkeypatch.setattr(watchdog, "_provider_snapshot", churn)
    assert watchdog._provider_settings_state() == "split"


@pytest.mark.parametrize("journal", [True, False])
@pytest.mark.parametrize("case", ["no_reader", "unknown_head", "merge_head", "local_change",
                                  "unverifiable_tree", "untracked_code"])
def test_split_checkout_that_is_not_intact_holds(watchdog, monkeypatch, case, journal):
    _settled(Path(watchdog.DATA))
    marker = Path(watchdog.MARKER_PATH)
    marker.write_text("synthetic marker")
    journal_path = Path(watchdog.STATE_PATH)
    if journal:
        journal_path.write_text(json.dumps({
            "v": 1, "applying": {"sha": HEAD, "prev_sha": "b" * 40}, "stages": [], "applied": []}))
    before = journal_path.read_bytes() if journal else None
    foreign = {"unverifiable_tree": None, "local_change": ["mcs/core/mcs_util.py"]}.get(case, [])
    _checkout(monkeypatch, watchdog, head="" if case == "unknown_head" else HEAD,
              missing=("mcs/core/provider_settings.py",) if case == "no_reader" else (),
              foreign=foreign,
              untracked=("mcs/core/provider_settings_shadow.py\0" if case == "untracked_code"
                         else "mcs/core/__pycache__/synthetic.cpython-314.pyc\0"))
    if case == "merge_head":
        git_dir = Path(watchdog.REPO) / ".git"
        git_dir.mkdir(parents=True)
        (git_dir / "MERGE_HEAD").write_text(HEAD)
    patch_lifecycle_lookup(monkeypatch, watchdog, "_clean_stale_git_locks", lambda: pytest.fail("hold cleaned git locks"))
    _no_effects(monkeypatch, watchdog)
    assert watchdog._recover_legacy() == 1
    assert marker.read_text() == "synthetic marker"
    assert (journal_path.read_bytes() if journal else None) == before
    report = json.loads(Path(watchdog.REPORT_PATH).read_text())
    assert report["result"] == "provider_checkout_hold" and report["held"] is True


def test_split_rollback_is_left_to_target_updater(watchdog, monkeypatch):
    _settled(Path(watchdog.DATA))
    journal = Path(watchdog.STATE_PATH)
    journal.write_text(json.dumps({"v": 1, "applying": {
        "sha": HEAD, "prev_sha": "b" * 40, "rollback": True, "backup_path": "synthetic-backup",
        "schema_bump": True}, "stages": [], "applied": []}))
    before = journal.read_bytes()
    _checkout(monkeypatch, watchdog)
    patch_lifecycle_lookup(monkeypatch, watchdog, "_clean_stale_git_locks", lambda: pytest.fail("hold cleaned git locks"))
    _no_effects(monkeypatch, watchdog)
    assert watchdog._recover_legacy() == 1
    assert journal.read_bytes() == before
    assert Path(watchdog.MARKER_PATH).is_file()
    assert json.loads(Path(watchdog.REPORT_PATH).read_text())["result"] == "provider_rollback_hold"


def test_watchdog_accepts_split_written_by_provider_settings(watchdog, monkeypatch):
    import mcs_config
    import provider_settings as settings

    root = Path(watchdog.DATA).parent
    owners = {"synthetic-anthropic": "anthropic", "synthetic-openai": "openai"}
    _private(root / "config.json", json.dumps(
        {"cloud_llm": {"mode": "off", "project_ids": []}, "semantic": {"mode": "off"}}).encode())
    monkeypatch.setattr(mcs_config, "validate_config", lambda _: ([], []))
    current = settings.read(root, model_owners=owners)
    for _ in range(2):
        written = settings.commit(root, current["snapshot"], current["common"],
                                  {name: {} for name in settings.PROVIDERS},
                                  selected_provider="openai", model_owners=owners)
        current = settings.read(root, model_owners=owners)
    assert written["mode"] == "split"
    assert any(p.name.startswith(".provider-settings-backup-") for p in Path(watchdog.DATA).iterdir())
    assert watchdog._provider_settings_state() == "split"


def test_doctor_reads_only_the_hold_token(tmp_path):
    import service_management

    (tmp_path / "data").mkdir()
    report = tmp_path / "data" / "recovery_report.json"
    assert service_management.recovery_hold(str(tmp_path)) is None
    report.write_text(json.dumps({"result": "provider_checkout_hold", "held": True, "detail": CANARY}))
    assert service_management.recovery_hold(str(tmp_path)) == "provider_checkout_hold"
    report.write_text(json.dumps({"result": "resumed", "detail": CANARY}))
    assert service_management.recovery_hold(str(tmp_path)) is None
    report.write_text(json.dumps({"result": CANARY + " /etc", "held": True}))
    assert service_management.recovery_hold(str(tmp_path)) is None


def test_independent_roots_do_not_unsettle_split(watchdog):
    data = Path(watchdog.DATA)
    _settled(data)
    (data.parent / "config.json").write_text(json.dumps(dict(COMMON, drug_map={"edited": 1}, sdc={"x": 1})))
    assert watchdog._provider_settings_state() == "split"


@pytest.mark.parametrize("common", [
    {}, COMMON, {"b": [1, 2.5, None, "é"], "a": {"z": True, "y": "\u65e5"}, "sdc": {"x": 1}},
])
def test_common_policy_digest_matches_provider_settings(watchdog, common):
    from provider_settings import common_policy_digest

    assert watchdog._common_policy_digest(common) == common_policy_digest(common)


def test_split_checkout_hold_does_not_create_marker(watchdog, monkeypatch):
    # An apply that died before quiesce left no marker; creating one would stop collection.
    _settled(Path(watchdog.DATA))
    Path(watchdog.STATE_PATH).write_text(json.dumps({
        "v": 1, "applying": {"sha": HEAD, "prev_sha": "b" * 40}, "stages": [], "applied": []}))
    _checkout(monkeypatch, watchdog, foreign=["mcs/core/mcs_util.py"])
    patch_lifecycle_lookup(monkeypatch, watchdog, "_clean_stale_git_locks", lambda: pytest.fail("hold cleaned locks"))
    _no_effects(monkeypatch, watchdog)
    assert watchdog._recover_legacy() == 1
    assert not os.path.lexists(watchdog.MARKER_PATH)
    report = json.loads(Path(watchdog.REPORT_PATH).read_text())
    assert report["result"] == "provider_checkout_hold" and report["held"] is True
