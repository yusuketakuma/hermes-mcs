"""Versioned update contracts with synthetic Git objects, no Git history/process."""
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_planning
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import update_services
import mcs_util
import lifecycle_io
import service_contract
import service_management
import update_discovery
import runtime_inspection
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

import pytest

import ledger
import maintenance
import notify_spool
import notify_transport
import mcs_setup
from ops_testkit import _seed_consent, in_process_post_merge
from test_mcs_upgrade import _launcher
from test_supported_schema_upgrade import (
    FIXTURES, RELEASES, _historical, _preserved, _records, _sha,
    updater as _schema_updater,
)


updater = _schema_updater
MANIFEST = json.loads((FIXTURES / "update-paths.json").read_text())
FEATURES = {r["release"]: r for r in MANIFEST["source_features"]}
ORIGINS = {r["release"]: r for r in RELEASES}
PATHS = MANIFEST["current_paths"]
ELIGIBLE = [p for p in PATHS if p["entry"] != "manual"]
MANUAL = [p for p in PATHS if p["entry"] == "manual"]


def _id(path):
    return f"{path['source']}-{path.get('target', 'current')}-{path['runtime']}"


def _blob_id(content):
    return hashlib.sha1(f"blob {len(content)}\0".encode() + content,
                        usedforsecurity=False).hexdigest()


class _Objects:
    """Git's object/working-tree boundary, explicitly not real Git validation."""
    def __init__(self, root, origin, install_changed, target, schema):
        self.root, self.target = root, target
        self.before = origin["commit"]
        self.after = hashlib.sha256(target.encode()).hexdigest()[:40]
        self.head = self.before
        self.source_tag = origin["release"]
        self.calls = []
        self.old = {"install.sh": b"# synthetic installer\n"}
        if FEATURES[self.source_tag]["updater"]:
            self.old["mcs/ops/mcs_update.py"] = b"raise RuntimeError('old updater must not run')\n"
        if FEATURES[self.source_tag]["standalone"]:
            self.old.update({"mcs_standalone/__main__.py": b"", "mcs/core/mcs_runtime.py": b""})
        self.new = {
            **self.old, "mcs/_mcs_path.py": b"",
            "mcs/mcs_setup.py": b"def validate_config(cfg): return [], []\n",
            "mcs/core/ledger.py": f"SCHEMA_VERSION = {schema}\n".encode(),
            "mcs/ops/mcs_update.py": (FIXTURES / "bootstrap-target.py").read_bytes(),
            "mcs_standalone/__main__.py": b"", "mcs/core/mcs_runtime.py": b"",
        }
        if install_changed:
            self.new["install.sh"] = b"# synthetic changed installer\n"
        self.blobs = {_blob_id(v): v for v in [*self.old.values(), *self.new.values()]}
        self.materialize(self.old)

    def materialize(self, files):
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def tree(self, ref):
        return self.old if ref == self.before or ref == "HEAD" and self.head == self.before else self.new

    def run(self, args, timeout=30, **kw):
        self.calls.append(args)
        rc, out = 0, ""
        command = args[0]
        if command == "status" or command == "fetch":
            pass
        elif command == "ls-files":
            if args[1:] == ["--stage", "-v", "-z"]:
                out = "".join(f"H 100644 {_blob_id(content)} 0\t{name}\0"
                              for name, content in sorted(self.tree("HEAD").items()))
        elif command == "describe":
            out = self.source_tag if self.head == self.before else self.target
        elif command == "rev-parse":
            out = str(self.root) if args[1] == "--show-toplevel" else (
                self.head if args[1] == "HEAD" else self.after)
        elif command == "tag":
            out = f"{self.source_tag}\n{self.target}\n"
        elif command == "merge-base":
            assert args == ["merge-base", "--is-ancestor", "HEAD", self.target]
        elif command == "cat-file":
            if args[1] == "blob":
                return subprocess.CompletedProcess(args, 0, self.blobs[args[2]], b"")
            ref, name = args[2].split(":", 1)
            rc = int(name not in self.tree(ref))
        elif command == "show":
            ref, name = args[1].split(":", 1)
            tree = self.tree(ref)
            rc = int(name not in tree)
            out = tree.get(name, b"").decode()
        elif command == "ls-tree":
            tree = self.tree(args[2])
            prefix = args[4] + "/" if len(args) > 3 else ""
            out = "".join(
                f"100644 blob {_blob_id(content)}\t{name}\0"
                for name, content in sorted(tree.items()) if name.startswith(prefix))
        elif command == "diff":
            changed = {name for name in self.old.keys() | self.new.keys()
                       if self.old.get(name) != self.new.get(name)}
            if self.head == self.after:
                changed = set()
            if "--quiet" in args:
                rc = int(any(name in changed or any(p.startswith(name + "/") for p in changed)
                             for name in args[args.index("--") + 1:]))
            else:
                out = "".join(name + "\0" for name in sorted(changed))
        elif command == "merge":
            assert args == ["merge", "--ff-only", f"refs/tags/{self.target}"]
            self.materialize(self.new)
            self.head = self.after
        elif command == "reset":
            assert args == ["reset", "--hard", self.before]
            # Simulation only: no destructive Git command is executed.
            self.materialize(self.old)
            self.head = self.before
        else:
            raise AssertionError(f"unexpected synthetic Git operation: {args}")
        return subprocess.CompletedProcess(args, rc, out, "")


def _world(tmp_path, monkeypatch, updater, path, *, install=False, schema=None):
    origin = ORIGINS[path["source"]]
    source = _historical(tmp_path / "source", origin)
    shutil.copy2(source, updater.LEDGER)
    shutil.copytree(source.parent / "attachments", Path(updater.DATA) / "attachments")
    cfg = {"runtime_mode": path["runtime"], "mcs_login_id": "synthetic",
           "notify_target": "local", "update": {"mode": "notify"}}
    root = Path(updater.REPO)
    config = root / "config.json"
    config.write_text(json.dumps(cfg))
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(config))
    objects = _Objects(root, origin, install,
                       path.get("target", MANIFEST["target"]["version"]),
                       ledger.SCHEMA_VERSION if schema is None else schema)
    monkeypatch.setattr(updater, "HOME", str(root))
    monkeypatch.setattr(update_apply, 'HOME', updater.HOME)
    monkeypatch.setattr(update_cli, 'HOME', updater.HOME)
    monkeypatch.setattr(update_execution, 'HOME', updater.HOME)
    monkeypatch.setattr(update_lifecycle_context, 'HOME', updater.HOME)
    monkeypatch.setattr(update_preflight, 'HOME', updater.HOME)
    monkeypatch.setattr(update_recovery, 'HOME', updater.HOME)
    monkeypatch.setattr(update_runtime_migration, 'HOME', updater.HOME)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: cfg)
    monkeypatch.setattr(updater, "_git", objects.run)
    monkeypatch.setattr(update_apply, '_git', updater._git)
    monkeypatch.setattr(update_cli, '_git', updater._git)
    monkeypatch.setattr(update_planning, '_git', updater._git)
    monkeypatch.setattr(update_preflight, '_git', updater._git)
    monkeypatch.setattr(update_recovery, '_git', updater._git)
    monkeypatch.setattr(update_repository, '_git', updater._git)
    monkeypatch.setattr(update_restore, '_git', updater._git)
    monkeypatch.setattr(update_runtime_migration, '_git', updater._git)
    monkeypatch.setattr(update_discovery, "git", objects.run)
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda tag, **_lif05_paths: objects.after)
    monkeypatch.setattr(maintenance, "BACKUP_DIR", updater.BACKUP_DIR)
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda *a: "synthetic-hermes")
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda *a: True)
    monkeypatch.setattr(runtime_inspection, "python_problem", lambda exe: None)
    monkeypatch.setattr(updater, "_standalone_drainer_problems", lambda: [])
    monkeypatch.setattr(update_execution, '_standalone_drainer_problems', updater._standalone_drainer_problems)
    monkeypatch.setattr(update_services, '_standalone_drainer_problems', updater._standalone_drainer_problems)
    monkeypatch.setattr(updater, "_baseline_check", lambda cfg: [])
    monkeypatch.setattr(update_apply, '_baseline_check', updater._baseline_check)
    calls = []
    monkeypatch.setattr(updater, "quiesce", lambda: updater._write_marker() or [])
    monkeypatch.setattr(update_apply, 'quiesce', updater.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', updater.quiesce)
    monkeypatch.setattr(updater, "restart_agents", lambda bounce=True: [])
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(updater, "restart_gateway", lambda cfg: None)
    monkeypatch.setattr(update_services, 'restart_gateway', updater.restart_gateway)
    monkeypatch.setattr(updater, "_services_reconcile", lambda: calls.append("services"))
    monkeypatch.setattr(update_recovery, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(update_restore, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(update_runtime_migration, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(updater, "_run_post_merge", in_process_post_merge(updater, monkeypatch))
    monkeypatch.setattr(update_apply, '_run_post_merge', updater._run_post_merge)
    monkeypatch.delenv("MCS_UPDATE_REPO", raising=False)
    real_run = subprocess.run

    def run(argv, *args, **kw):
        if argv[0] == "git":
            if "check-ignore" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return objects.run(argv[3:])
        return real_run(argv, *args, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    real_popen = subprocess.Popen

    class Installer:
        returncode = 0
        pid = 0
        def __init__(self, argv, **kw):
            assert kw["start_new_session"] and kw["stdin"] == subprocess.DEVNULL
            calls.append(argv)
        def communicate(self, timeout):
            return "synthetic installer", None

    monkeypatch.setattr(subprocess, "Popen", lambda argv, *a, **kw:
                        Installer(argv, **kw) if argv[0] == "/bin/sh" else real_popen(argv, *a, **kw))
    return objects, source, config, calls, Installer


def test_manifest_preserves_exact_historical_26_and_supported_sources():
    historical = {(p["source"], p["target"], p["runtime"])
                  for p in MANIFEST["historical_paths"]}
    expected = {(f"v1.0.{n}", f"v1.0.{t}", "hermes")
                for n in range(10) for t in (11, 12)}
    expected |= {("v1.0.10", f"v1.0.{t}", mode)
                 for t in (11, 12) for mode in ("hermes", "standalone")}
    expected |= {("v1.0.11", "v1.0.12", mode) for mode in ("hermes", "standalone")}
    assert historical == expected and len(historical) == 26
    assert MANIFEST["target"]["version"] == "v1.1.1"
    assert RELEASES[-1]["release"] == "v1.1.0"
    assert RELEASES[-1]["schema_version"] == 10
    assert RELEASES[-1]["fixture"] == "shape-14.sql"
    expected_sources = {f"v1.0.{n}" for n in range(30)} | {"v1.1.0"}
    assert {origin["release"] for origin in RELEASES} == expected_sources
    assert set(FEATURES) == set(ORIGINS) == {origin["release"] for origin in RELEASES}
    assert len(FEATURES) == len(MANIFEST["source_features"]) == len(RELEASES)
    expected_paths = {
        (release, mode) for release, flags in FEATURES.items()
        for mode in (["hermes", "standalone"] if flags["standalone"] else ["hermes"])}
    assert {(p["source"], p["runtime"]) for p in PATHS} == expected_paths
    assert len(PATHS) == len(expected_paths)
    assert all(len(o["commit"]) == 40 for o in RELEASES)


def test_synthetic_git_provenance_accepts_endpoints_and_rejects_local_edits(
        tmp_path, monkeypatch, updater):
    objects, _, _, _, _ = _world(tmp_path, monkeypatch, updater, ELIGIBLE[0], install=True)
    assert updater._local_changes_outside(objects.before, objects.after) == []
    objects.run(["merge", "--ff-only", f"refs/tags/{objects.target}"])
    assert updater._local_changes_outside(objects.before, objects.after) == []
    (objects.root / "install.sh").write_bytes(b"# synthetic local change\n")
    assert updater._local_changes_outside(objects.before, objects.after) == ["install.sh"]


@pytest.mark.parametrize("path", MANIFEST["historical_paths"], ids=_id)
def test_historical_routes_classified_without_claiming_old_binary_execution(
        tmp_path, monkeypatch, updater, path):
    target = ORIGINS[path["target"]]
    objects, source, config, _, _ = _world(
        tmp_path, monkeypatch, updater, path,
        install=path["install_changed"], schema=target["schema_version"])
    before = (_sha(source), _sha(Path(updater.LEDGER)), config.read_bytes())
    result = updater.plan(objects.target)
    blocker = ("legacy_source_manual" if not FEATURES[path["source"]]["updater"] else
               "standalone_external_apply" if path["runtime"] == "standalone" else None)
    assert result["route"] == ("blocked" if blocker else "reinstall" if path["install_changed"] else "apply")
    assert result["blockers"] == ([blocker] if blocker else [])
    assert result["current"]["tag"] == path["source"]
    assert before == (_sha(source), _sha(Path(updater.LEDGER)), config.read_bytes())
    assert not Path(updater.STATE_PATH).exists()


@pytest.mark.parametrize("install", [False, True])
@pytest.mark.parametrize("path", PATHS, ids=_id)
def test_current_plan_is_readonly_and_names_manual_or_host_prerequisites(
        tmp_path, monkeypatch, updater, path, install):
    objects, source, config, _, _ = _world(tmp_path, monkeypatch, updater, path, install=install)
    before = (_sha(source), _sha(Path(updater.LEDGER)), config.read_bytes())
    result = updater.plan(objects.target)
    blocker = path["external_plan"]
    assert result["route"] == ("blocked" if blocker else "reinstall" if install else "apply")
    assert result["blockers"] == ([blocker] if blocker else [])
    source_schema = ORIGINS[path["source"]]["schema_version"]
    assert result["schema_bump"] == (
        f"schema_bump:{source_schema}->{ledger.SCHEMA_VERSION}"
        if source_schema < ledger.SCHEMA_VERSION else None)
    assert result["reinstall"] == (["install_sh_changed"] if install else [])
    assert before == (_sha(source), _sha(Path(updater.LEDGER)), config.read_bytes())
    assert not Path(updater.STATE_PATH).exists()


@pytest.mark.parametrize("install", [False, True])
@pytest.mark.parametrize("path", ELIGIBLE, ids=_id)
def test_apply_reapply_and_bound_rollback_preserve_released_records(
        tmp_path, monkeypatch, updater, path, install):
    objects, source, config, calls, _ = _world(tmp_path, monkeypatch, updater, path, install=install)
    original, config_bytes = _records(source), config.read_bytes()
    source_hash = _sha(source)
    restarts = []
    monkeypatch.setattr(updater, "restart_agents", lambda bounce=True: restarts.append(("agents", bounce)) or [])
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(updater, "restart_gateway", lambda cfg: restarts.append(("gateway", None)))
    monkeypatch.setattr(update_services, 'restart_gateway', updater.restart_gateway)
    assert updater.apply(objects.target, objects.after, None, reinstall=install,
                         install_args=("--no-llm",) if install else ()) == 0
    applied = updater.journal_store.load_state(updater.STATE_PATH)["applied"][-1]
    assert applied["sha"] == objects.after and applied["prev_sha"] == objects.before
    bump = ORIGINS[path["source"]]["schema_version"] < ledger.SCHEMA_VERSION
    assert applied["schema_bump"] is bump
    assert applied["reinstall_done"] is install
    backup = Path(applied["backup_path"])
    assert _records(backup) == original
    _preserved(Path(updater.LEDGER), original)
    assert updater.apply(objects.target, objects.after, None, reinstall=install) == 0
    assert len(updater.journal_store.load_state(updater.STATE_PATH)["applied"]) == 1
    assert calls == ([["/bin/sh", str(objects.root / "install.sh"), "--no-llm", "--no-services"],
                      "services"] if install else ["services"])
    with closing(ledger.Ledger(updater.LEDGER)) as db:
        with db.db:
            db.db.execute("INSERT INTO messages(message_id,project_id,body_html,body_text,"
                          "body_state,content_hash) VALUES(203,101,'synthetic later',"
                          "'synthetic later','full',?)",
                          (hashlib.sha256(b"synthetic later").hexdigest(),))
    after_insert = _records(Path(updater.LEDGER))
    backup_hash = _sha(backup)
    restarts.clear()  # The following observations concern rollback, not completed apply.
    assert updater.rollback("synthetic-path-rollback") == (2 if bump else 0)
    if bump:
        report = json.loads(Path(updater.RESTORE_REPORT_PATH).read_text())
        assert report["stored_since_backup"]["messages"] == 1
        assert restarts == [] and Path(updater.MARKER_PATH).exists()
        live = Path(updater.LEDGER)
        with closing(sqlite3.connect(live.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            denied = notify_transport.apply_transport_begin(con, {"command_id": "synthetic-path-before-consent"}, {})
            assert denied["granted"] is False and denied["error"] == "denied_restore_pending"
        _seed_consent(updater.LEDGER, str(backup), report=report)
        # The loaded current writer must not migrate the restored historical DB.
        assert updater.recover_interrupted() == 1
        marker = notify_spool.restore_pending(str(live.parent))
        assert marker is not None and marker["phase"] == "restored"
        assert marker["report_id"] == report["report_id"] and marker["backup_path"] == str(backup)
        held = updater.journal_store.load_state(updater.STATE_PATH)
        assert held["applying"]["rollback"] is True and held["applying"]["restore_incomplete"] is True
        assert held["restore_consent"]["report_id"] == report["report_id"]
        assert "synthetic-path-rollback" not in held["executed"]
        assert restarts == [] and Path(updater.MARKER_PATH).exists()
        assert _sha(live) == backup_hash and _sha(backup) == backup_hash and _sha(source) == source_hash
        _preserved(live, original)  # Even disputed notification rows stay byte-preserved while held.
        with closing(sqlite3.connect(live.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            assert con.execute("PRAGMA user_version").fetchone()[0] == ORIGINS[path["source"]]["schema_version"]
            denied = notify_transport.apply_transport_begin(con, {"command_id": "synthetic-path-after-held-copy"}, {})
            assert denied["granted"] is False and denied["error"] == "denied_restore_pending"
        assert all(not Path(str(live) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
        stable = (_sha(live), Path(updater.STATE_PATH).read_bytes(),
                  Path(updater.RESTORE_REPORT_PATH).read_bytes(), marker)
        assert updater.recover_interrupted() == 1
        assert (_sha(live), Path(updater.STATE_PATH).read_bytes(),
                Path(updater.RESTORE_REPORT_PATH).read_bytes(), notify_spool.restore_pending(str(live.parent))) == stable
        assert restarts == [] and Path(updater.MARKER_PATH).exists()
    else:
        assert not Path(updater.RESTORE_REPORT_PATH).exists()
    assert objects.head == objects.before
    if not bump:
        # Existing supported-schema no-copy rollback keeps its success oracle.
        assert updater.journal_store.load_state(updater.STATE_PATH)["executed"]["synthetic-path-rollback"]["result"] == "rolled_back"
    assert config.read_bytes() == config_bytes
    durable = {table: rows for table, rows in (original if bump else after_insert).items()
               if table != "notify_outbox" and not table.startswith("notification_")}
    _preserved(Path(updater.LEDGER), durable)
    assert _records(backup) == original


@pytest.mark.parametrize("choice", [None, False, True], ids=["unset", "off", "on"])
def test_update_and_rollback_preserve_settings_and_follow_release_defaults(
        tmp_path, monkeypatch, updater, choice):
    path = next(p for p in ELIGIBLE
                if p["source"] == RELEASES[-1]["release"] and p["runtime"] == "hermes")
    objects, _, config, _, _ = _world(tmp_path, monkeypatch, updater, path)
    # Isolate settings from schema rollback: this synthetic live DB already
    # uses the target schema, so rollback does not restore an older backup.
    with closing(ledger.Ledger(updater.LEDGER)):
        pass

    cfg = {"runtime_mode": "hermes", "mcs_login_id": "synthetic",
           "notify_target": "local", "update": {"mode": "notify"},
           "optional_features": {"future_feature": {"preserve": [1, 2]}},
           "future_extension": {"preserve": "synthetic"}}
    if choice is not None:
        cfg["optional_features"]["csdc"] = choice
    config.write_text(json.dumps(cfg, indent=2) + "\n")
    config_bytes = config.read_bytes()
    monkeypatch.setattr(mcs_util, "load_config", lambda: json.loads(config.read_bytes()))

    # Both synthetic trees contain the actual feature implementation. Only
    # the new tree's default differs, as a future release might change it.
    relative = "mcs/core/mcs_config.py"
    source = (Path(__file__).resolve().parents[2] / relative).read_bytes()
    old_default = b'OPTIONAL_FEATURE_DEFAULTS = {"csdc": False}'
    assert source.count(old_default) == 1
    target = source.replace(old_default, b'OPTIONAL_FEATURE_DEFAULTS = {"csdc": True}')
    objects.old[relative] = source
    objects.new[relative] = target
    objects.blobs.update({_blob_id(source): source, _blob_id(target): target})
    objects.materialize(objects.old)
    guarded_operations = []
    real_git = updater._git

    def guarded_git(args, *extra, **kwargs):
        if args[0] in ("merge", "reset"):
            assert Path(updater.MARKER_PATH).exists()
            assert updater.acquire_update_lock() is None
            assert updater.acquire_run_lock(updater.RUN_LOCK) is None
            guarded_operations.append(args[0])
        return real_git(args, *extra, **kwargs)

    monkeypatch.setattr(updater, "_git", guarded_git)
    monkeypatch.setattr(update_apply, '_git', updater._git)
    monkeypatch.setattr(update_cli, '_git', updater._git)
    monkeypatch.setattr(update_planning, '_git', updater._git)
    monkeypatch.setattr(update_preflight, '_git', updater._git)
    monkeypatch.setattr(update_recovery, '_git', updater._git)
    monkeypatch.setattr(update_repository, '_git', updater._git)
    monkeypatch.setattr(update_restore, '_git', updater._git)
    monkeypatch.setattr(update_runtime_migration, '_git', updater._git)

    def assert_settings(code, expected):
        current = objects.root / relative
        assert current.read_bytes() == code
        assert config.read_bytes() == config_bytes
        stored = json.loads(config.read_bytes())
        assert stored == cfg
        namespace = {"__name__": "synthetic_release_config"}
        exec(compile(current.read_bytes(), str(current), "exec"), namespace)
        assert namespace["feature_enabled"](stored, "csdc") is expected

    old_value = False if choice is None else choice
    new_value = True if choice is None else choice
    assert_settings(source, old_value)
    result = updater.plan(objects.target)
    assert result["route"] == "apply" and not result["blockers"]
    assert result["schema_bump"] is None
    assert_settings(source, old_value)

    assert updater.apply(objects.target, objects.after, None) == 0
    applied = updater.journal_store.load_state(updater.STATE_PATH)["applied"]
    assert len(applied) == 1 and applied[0]["schema_bump"] is False
    assert_settings(target, new_value)
    assert updater.apply(objects.target, objects.after, None) == 0
    assert len(updater.journal_store.load_state(updater.STATE_PATH)["applied"]) == 1
    assert_settings(target, new_value)

    assert updater.rollback("synthetic-settings-rollback") == 0
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state["executed"]["synthetic-settings-rollback"]["result"] == "rolled_back"
    assert objects.head == objects.before
    assert_settings(source, old_value)
    assert guarded_operations == ["merge", "reset"]


@pytest.mark.parametrize("path", ELIGIBLE, ids=_id)
def test_failed_reinstall_rolls_back_before_schema_migration(
        tmp_path, monkeypatch, updater, path):
    # Isolate reinstall rollback from the separately tested provider migration hold.
    monkeypatch.setattr(updater, "_provider_migration_needed", lambda: False)
    monkeypatch.setattr(update_apply, '_provider_migration_needed', updater._provider_migration_needed)
    monkeypatch.setattr(update_recovery, '_provider_migration_needed', updater._provider_migration_needed)
    objects, source, config, calls, installer = _world(tmp_path, monkeypatch, updater, path, install=True)
    before = (_sha(source), _records(Path(updater.LEDGER)), config.read_bytes())
    installer.returncode = 1
    assert updater.apply(objects.target, objects.after, None, reinstall=True) == 1
    assert calls == [["/bin/sh", str(objects.root / "install.sh"), "--no-services"], "services"]
    assert objects.head == objects.before
    assert before == (_sha(source), _records(Path(updater.LEDGER)), config.read_bytes())
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"] is None


@pytest.mark.parametrize("path", MANUAL, ids=_id)
def test_legacy_manual_reinstall_migration_and_restore(
        tmp_path, monkeypatch, updater, path):
    objects, source, config, calls, _ = _world(tmp_path, monkeypatch, updater, path, install=True)
    assert "legacy_source_manual" in updater.plan(objects.target)["blockers"]
    expected, config_bytes = _records(source), config.read_bytes()
    source_hash = _sha(source)
    restarts = []
    monkeypatch.setattr(updater, "restart_agents", lambda bounce=True: restarts.append(("agents", bounce)) or [])
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(updater, "restart_gateway", lambda cfg: restarts.append(("gateway", None)))
    monkeypatch.setattr(update_services, 'restart_gateway', updater.restart_gateway)
    backup = maintenance.preupdate_backup(updater.LEDGER)
    backup_hash = _sha(Path(backup))
    # The documented operator route has no automatic apply/rollback promise.
    objects.run(["merge", "--ff-only", f"refs/tags/{objects.target}"])
    applying = {"reinstall": True, "install_args": ["--no-llm"],
                "install_options_base": updater._install_options_snapshot()[0]}
    update_fd = updater.acquire_update_lock()
    assert update_fd is not None
    try:
        monkeypatch.setenv(updater.UPDATE_LOCK_FD_ENV, str(update_fd))
        updater._reinstall(applying)
    finally:
        os.close(update_fd)
    assert applying["reinstall_done"] is True
    assert updater._postcheck({"baseline_check": []}, objects.after) == []
    _preserved(Path(updater.LEDGER), expected)
    assert calls == [["/bin/sh", str(objects.root / "install.sh"), "--no-llm", "--no-services"]]
    objects.run(["reset", "--hard", objects.before])
    with pytest.raises(updater.RestoreConsentPending) as held:
        updater._restore_db(backup)
    _seed_consent(updater.LEDGER, backup, report=held.value.report)
    state = updater.journal_store.load_state(updater.STATE_PATH)       # the rollback journal a real replace runs under
    state["applying"] = {"tag": objects.target, "sha": objects.after,
                         "prev_sha": objects.before, "rollback": True,
                         "backup_path": backup, "at": time.time()}
    updater.journal_store.save_state(updater.STATE_PATH, state)
    # This manual journal simulates the existing copy boundary, not an old writer binary.
    assert ORIGINS[path["source"]]["schema_version"] < ledger.SCHEMA_VERSION
    with pytest.raises(updater.UpdateError, match="^restore_reconcile_schema_blocked$"):
        updater._restore_db(backup)
    applying = updater.journal_store.load_state(updater.STATE_PATH)["applying"]
    assert applying["restore_incomplete"] is True and restarts == []
    live = Path(updater.LEDGER)
    marker = notify_spool.restore_pending(str(live.parent))
    assert marker is not None and marker["phase"] == "restored"
    assert marker["report_id"] == held.value.report["report_id"] and marker["backup_path"] == backup
    assert _sha(live) == backup_hash and _sha(Path(backup)) == backup_hash and _sha(source) == source_hash
    _preserved(live, expected)
    with closing(sqlite3.connect(live.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        assert con.execute("PRAGMA user_version").fetchone()[0] == ORIGINS[path["source"]]["schema_version"]
        denied = notify_transport.apply_transport_begin(con, {"command_id": "synthetic-manual-held-copy"}, {})
        assert denied["granted"] is False and denied["error"] == "denied_restore_pending"
    assert all(not Path(str(live) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
    stable = (_sha(live), Path(updater.STATE_PATH).read_bytes(),
              Path(updater.RESTORE_REPORT_PATH).read_bytes(), marker)
    with pytest.raises(updater.UpdateError, match="^restore_reconcile_schema_blocked$"):
        updater._restore_db(backup)
    assert (_sha(live), Path(updater.STATE_PATH).read_bytes(),
            Path(updater.RESTORE_REPORT_PATH).read_bytes(), notify_spool.restore_pending(str(live.parent))) == stable
    assert restarts == []
    assert applying["backup_path"] == backup and "restore_staging" not in applying
    durable = {table: rows for table, rows in expected.items()
               if table != "notify_outbox" and not table.startswith("notification_")}
    _preserved(Path(updater.LEDGER), durable)
    assert config.read_bytes() == config_bytes


@pytest.mark.parametrize("path", PATHS, ids=_id)
@pytest.mark.parametrize("command", ["plan", "apply"])
def test_bootstrap_executes_only_target_and_cleans_private_tree(
        tmp_path, monkeypatch, updater, path, command, capfd):
    objects, source, config, _, _ = _world(tmp_path, monkeypatch, updater, path)
    launcher = _launcher()
    def git(repo, *args, binary=False):
        result = objects.run(list(args))
        assert result.returncode == 0
        return result.stdout
    monkeypatch.setattr(launcher, "_git", git)
    before = (_sha(source), _sha(Path(updater.LEDGER)), config.read_bytes())
    options = ["--reinstall", "--install-arg=--no-llm"] if command == "apply" else []
    # This synthetic target logs its invocation but never changes HEAD. apply must
    # report not-applied (2), while a read-only plan still succeeds.
    assert launcher.main(["--repo", str(objects.root), "--no-fetch", command,
                          "--to", objects.target, *options]) == (2 if command == "apply" else 0)
    captured = capfd.readouterr()
    assert ("適用されていません" in captured.err) == (command == "apply")
    seen = json.loads(captured.out)
    assert seen["entry"] == "synthetic-target"
    assert seen["argv"] == [command, "--tag", objects.target, *options]
    assert seen["repo"] == seen["cwd"] == str(objects.root)
    assert not Path(seen["file"]).exists()
    assert before == (_sha(source), _sha(Path(updater.LEDGER)), config.read_bytes())
    assert objects.head == objects.before


@pytest.mark.parametrize("path", [p for p in PATHS if p["runtime"] == "standalone"], ids=_id)
def test_standalone_external_apply_never_enters_host_pipeline(
        tmp_path, monkeypatch, updater, path):
    objects, _, _, _, _ = _world(tmp_path, monkeypatch, updater, path)
    monkeypatch.setenv("MCS_UPDATE_REPO", str(objects.root))
    assert updater.apply(objects.target, objects.after, None) == 2
    assert not Path(updater.STATE_PATH).exists()


@pytest.mark.parametrize("install", [False, True])
def test_v1012_plist_with_unsafe_python_blocks_before_merge_and_owner_repair_applies(
        tmp_path, monkeypatch, updater, install):
    """v1.0.12 rendered org.mcs.recovery with /usr/bin/python3 (synthetic
    stand-in here: Python 3.9.6 / SQLite 3.51.0, no WAL-reset fix)."""
    import plistlib
    import sys
    from types import SimpleNamespace
    from test_runtime_compatibility import _facts
    path = next(p for p in ELIGIBLE if p["source"] == "v1.0.12" and p["runtime"] == "hermes")
    objects, _, _, _, _ = _world(tmp_path, monkeypatch, updater, path, install=install)
    cfg = mcs_util.load_config()
    old, desired = "/synthetic/system/python3", str(tmp_path / "recovery-runtime/bin/python3")
    recovery = tmp_path / "mcs-recovery"
    recovery.mkdir(mode=0o700)
    (recovery / "mcs_recover.py").write_text("# synthetic v1.0.12 recovery tool\n")
    (recovery / "mcs_recover.py").chmod(0o700)
    (recovery / "repo_path").write_text(str(Path(mcs_setup.REPO_ROOT).resolve()) + "\n")
    (recovery / "repo_path").chmod(0o600)
    plist = Path(mcs_setup.AGENTS_DIR, service_contract.RECOVERY_LABEL + ".plist")
    plist.write_bytes(plistlib.dumps({
        "Label": service_contract.RECOVERY_LABEL,
        "ProgramArguments": [old, str(recovery / "mcs_recover.py"), "--if-stale"]}))
    plist.chmod(0o600)
    monkeypatch.setattr(mcs_setup, "RECOVERY_DIR", str(recovery))
    # Synthetic Git tree and loaded service source are separate in this fixture.
    from dataclasses import replace
    service_paths = replace(updater._service_paths(), recovery_dir=str(recovery),
                            checkout=str(mcs_setup.REPO_ROOT))
    monkeypatch.setattr(updater, "_service_paths", lambda: service_paths)
    monkeypatch.setattr(update_execution, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_lifecycle_context, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_planning, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_preflight, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_recovery, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_restore, '_service_paths', updater._service_paths)
    assert service_management.recovery_owned(paths=updater._service_paths())
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda exe:
                        _facts("3.9.6", "3.51.0") if exe == old else _facts())
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(lifecycle_io, 'run_bounded', lambda argv, **kw: SimpleNamespace(
        returncode=113 if argv[:2] == ["launchctl", "print"] else 0, stdout=""))
    notes = []

    def services():
        # what _run_post_merge hands the NEW-code child; the parent apply
        # still holds update.lock and run.lock
        monkeypatch.setenv(updater._UPDATE_PM_ENV, updater.journal_store.load_state(updater.STATE_PATH)["applying"]["sha"])
        assert service_management.runtime_gate_errors(cfg, repair_recovery=True, paths=mcs_setup._service_paths()) == []
        if service_management.sync_recovery(cfg, notes.append, False, paths=mcs_setup._service_paths()):
            raise updater.UpdateError("services_failed: " + notes[-1])

    monkeypatch.setattr(updater, "_services_reconcile", services)
    monkeypatch.setattr(update_recovery, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(update_restore, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(update_runtime_migration, '_services_reconcile', updater._services_reconcile)

    def merged():
        return [c for c in objects.calls if c[0] == "merge"]

    # The unchanged default watchdog is a pre-merge blocker naming the step.
    result = updater.plan(objects.target)
    assert result["route"] == "blocked"
    assert "recovery_runtime: 実行環境のPythonの版が対応範囲外です" in result["blockers"]
    assert any("recovery_python" in note for note in result["notes"])
    assert updater.apply(objects.target, objects.after, None, reinstall=install) == 1
    assert not merged() and objects.head == objects.before
    assert not Path(updater.STATE_PATH).exists()

    # Remedy reachable from v1.0.12: the owner sets recovery_python by hand.
    cfg["recovery_python"] = desired
    result = updater.plan(objects.target)
    assert result["route"] == ("reinstall" if install else "apply")
    assert any("--no-recovery" in note for note in result["notes"]) is install
    if install:
        # install.sh stops on the pending selection drift — refuse pre-merge
        assert updater.apply(objects.target, objects.after, None, reinstall=True) == 1
        assert not merged() and not Path(updater.STATE_PATH).exists()
    assert updater.apply(objects.target, objects.after, None, reinstall=install,
                         install_args=("--no-recovery",) if install else ()) == 0
    assert len(merged()) == 1 and objects.head == objects.after
    assert updater.journal_store.load_state(updater.STATE_PATH)["applied"][-1]["sha"] == objects.after
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][0] == desired
    assert service_management.runtime_gate_errors(cfg, paths=mcs_setup._service_paths()) == []
