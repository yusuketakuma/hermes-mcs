"""Versioned update contracts with synthetic Git objects, no Git history/process."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

import ledger
import maintenance
import mcs_setup
from ops_testkit import _seed_consent
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
        self.blobs = {hashlib.sha256(v).hexdigest()[:40]: v
                      for v in [*self.old.values(), *self.new.values()]}
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
        if command == "status" or command == "ls-files" or command == "fetch":
            pass
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
                f"100644 blob {hashlib.sha256(content).hexdigest()[:40]}\t{name}\0"
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
    objects = _Objects(root, origin, install,
                       path.get("target", MANIFEST["target"]["version"]),
                       ledger.SCHEMA_VERSION if schema is None else schema)
    monkeypatch.setattr(updater, "HOME", str(root))
    monkeypatch.setattr(updater, "load_config", lambda: cfg)
    monkeypatch.setattr(updater, "_git", objects.run)
    monkeypatch.setattr(updater, "remote_tag_sha", lambda tag: objects.after)
    monkeypatch.setattr(maintenance, "BACKUP_DIR", updater.BACKUP_DIR)
    monkeypatch.setattr(mcs_setup, "_hermes_exe", lambda *a: "synthetic-hermes")
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda *a: True)
    monkeypatch.setattr(mcs_setup, "_standalone_py_problem", lambda cfg: None)
    monkeypatch.setattr(updater, "_standalone_drainer_problems", lambda: [])
    monkeypatch.setattr(updater, "_baseline_check", lambda cfg: [])
    calls = []
    monkeypatch.setattr(updater, "quiesce", lambda: updater._write_marker() or [])
    monkeypatch.setattr(updater, "restart_agents", lambda bounce=True: [])
    monkeypatch.setattr(updater, "restart_gateway", lambda cfg: None)
    monkeypatch.setattr(updater, "_services_reconcile", lambda: calls.append("services"))
    monkeypatch.setattr(updater, "_run_post_merge", lambda sha: updater._post_merge(updater.load_state()))
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
    assert set(FEATURES) == set(ORIGINS)
    assert {(p["source"], p["runtime"]) for p in PATHS} == {
        (release, mode) for release, flags in FEATURES.items()
        for mode in (["hermes", "standalone"] if flags["standalone"] else ["hermes"])}
    assert all(len(o["commit"]) == 40 for o in RELEASES)


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
    assert updater.apply(objects.target, objects.after, None, reinstall=install,
                         install_args=("--no-llm",) if install else ()) == 0
    applied = updater.load_state()["applied"][-1]
    assert applied["sha"] == objects.after and applied["prev_sha"] == objects.before
    bump = ORIGINS[path["source"]]["schema_version"] < ledger.SCHEMA_VERSION
    assert applied["schema_bump"] is bump
    assert applied["reinstall_done"] is install
    backup = Path(applied["backup_path"])
    assert _records(backup) == original
    _preserved(Path(updater.LEDGER), original)
    assert updater.apply(objects.target, objects.after, None, reinstall=install) == 0
    assert len(updater.load_state()["applied"]) == 1
    assert calls == ([["/bin/sh", str(objects.root / "install.sh"), "--no-llm", "--no-services"],
                      "services"] if install else ["services"])
    with closing(ledger.Ledger(updater.LEDGER)) as db:
        with db.db:
            db.db.execute("INSERT INTO messages(message_id,project_id,body_html,body_text,"
                          "body_state,content_hash) VALUES(203,101,'synthetic later',"
                          "'synthetic later','full',?)",
                          (hashlib.sha256(b"synthetic later").hexdigest(),))
    after_insert = _records(Path(updater.LEDGER))
    assert updater.rollback("synthetic-path-rollback") == (2 if bump else 0)
    if bump:
        report = json.loads(Path(updater.RESTORE_REPORT_PATH).read_text())
        assert report["stored_since_backup"]["messages"] == 1
        _seed_consent(updater.LEDGER, str(backup), report=report)
        assert updater.recover_interrupted() == 0
    else:
        assert not Path(updater.RESTORE_REPORT_PATH).exists()
    assert objects.head == objects.before
    assert updater.load_state()["executed"]["synthetic-path-rollback"]["result"] == "rolled_back"
    assert config.read_bytes() == config_bytes
    durable = {table: rows for table, rows in (original if bump else after_insert).items()
               if table != "notify_outbox" and not table.startswith("notification_")}
    _preserved(Path(updater.LEDGER), durable)
    assert _records(backup) == original


@pytest.mark.parametrize("path", ELIGIBLE, ids=_id)
def test_failed_reinstall_rolls_back_before_schema_migration(
        tmp_path, monkeypatch, updater, path):
    objects, source, config, calls, installer = _world(tmp_path, monkeypatch, updater, path, install=True)
    before = (_sha(source), _records(Path(updater.LEDGER)), config.read_bytes())
    installer.returncode = 1
    assert updater.apply(objects.target, objects.after, None, reinstall=True) == 1
    assert calls == [["/bin/sh", str(objects.root / "install.sh"), "--no-services"], "services"]
    assert objects.head == objects.before
    assert before == (_sha(source), _records(Path(updater.LEDGER)), config.read_bytes())
    assert updater.load_state()["applying"] is None


@pytest.mark.parametrize("path", MANUAL, ids=_id)
def test_legacy_manual_reinstall_migration_and_restore(
        tmp_path, monkeypatch, updater, path):
    objects, source, config, calls, _ = _world(tmp_path, monkeypatch, updater, path, install=True)
    assert "legacy_source_manual" in updater.plan(objects.target)["blockers"]
    expected, config_bytes = _records(source), config.read_bytes()
    backup = maintenance.preupdate_backup(updater.LEDGER)
    # The documented operator route has no automatic apply/rollback promise.
    objects.run(["merge", "--ff-only", f"refs/tags/{objects.target}"])
    applying = {"reinstall": True, "install_args": ["--no-llm"]}
    updater._reinstall(applying)
    assert applying["reinstall_done"] is True
    assert updater._postcheck({"baseline_check": []}, objects.after) == []
    _preserved(Path(updater.LEDGER), expected)
    assert calls == [["/bin/sh", str(objects.root / "install.sh"), "--no-llm", "--no-services"]]
    objects.run(["reset", "--hard", objects.before])
    with pytest.raises(updater.RestoreConsentPending) as held:
        updater._restore_db(backup)
    _seed_consent(updater.LEDGER, backup, report=held.value.report)
    updater._restore_db(backup)
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
    assert launcher.main(["--repo", str(objects.root), "--no-fetch", command,
                          "--to", objects.target, *options]) == 0
    seen = json.loads(capfd.readouterr().out)
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
    cfg = updater.load_config()
    old, desired = "/synthetic/system/python3", str(tmp_path / "recovery-runtime/bin/python3")
    recovery = tmp_path / "mcs-recovery"
    recovery.mkdir(mode=0o700)
    (recovery / "mcs_recover.py").write_text("# synthetic v1.0.12 recovery tool\n")
    (recovery / "mcs_recover.py").chmod(0o700)
    (recovery / "repo_path").write_text(str(Path(mcs_setup.REPO_ROOT).resolve()) + "\n")
    (recovery / "repo_path").chmod(0o600)
    plist = Path(mcs_setup.AGENTS_DIR, mcs_setup.RECOVERY_LABEL + ".plist")
    plist.write_bytes(plistlib.dumps({
        "Label": mcs_setup.RECOVERY_LABEL,
        "ProgramArguments": [old, str(recovery / "mcs_recover.py"), "--if-stale"]}))
    plist.chmod(0o600)
    monkeypatch.setattr(mcs_setup, "RECOVERY_DIR", str(recovery))
    monkeypatch.setattr(mcs_setup, "_runtime_probe", lambda exe:
                        _facts("3.9.6", "3.51.0") if exe == old else _facts())
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup, "_run", lambda argv, **kw: SimpleNamespace(
        returncode=113 if argv[:2] == ["launchctl", "print"] else 0, stdout=""))
    notes = []

    def services():
        # what _run_post_merge hands the NEW-code child; the parent apply
        # still holds update.lock and run.lock
        monkeypatch.setenv(updater._UPDATE_PM_ENV, updater.load_state()["applying"]["sha"])
        assert mcs_setup.runtime_gate_errors(cfg, repair_recovery=True) == []
        if mcs_setup._sync_recovery(cfg, notes.append, False):
            raise updater.UpdateError("services_failed: " + notes[-1])

    monkeypatch.setattr(updater, "_services_reconcile", services)

    def merged():
        return [c for c in objects.calls if c[0] == "merge"]

    # The unchanged default watchdog is a pre-merge blocker naming the step.
    result = updater.plan(objects.target)
    assert result["route"] == "blocked"
    assert "recovery_runtime: runtime SQLite WAL-reset fix missing" in result["blockers"]
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
    assert updater.load_state()["applied"][-1]["sha"] == objects.after
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][0] == desired
    assert mcs_setup.runtime_gate_errors(cfg) == []
