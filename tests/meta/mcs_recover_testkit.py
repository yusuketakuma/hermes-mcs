"""Synthetic mcs recover testkit contracts; shared helpers preserve isolated runtime state."""
import json
import os
import subprocess
import time
from pathlib import Path
import pytest
from ops_testkit import _git, _load


_MIGRATION = {"from": "3.14.7", "to": "3.14.8"}


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("one")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "c1")
    _git(repo, "tag", "v1.0.0")
    return repo


@pytest.fixture
def rec(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.mcs_recovery_services, "_notify", lambda *args: None)
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    original_run = subprocess.run

    def local_run(args, **kwargs):
        if args[0] == "launchctl":
            return subprocess.CompletedProcess(args, 0, "", "")
        return original_run(args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", local_run)
    monkeypatch.setattr(mod.mcs_recovery_core, "REPO", str(tmp_path / "repo"))
    monkeypatch.setattr(mod.mcs_recovery_core, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(mod.mcs_recovery_core, "STATE_PATH",
                        str(tmp_path / "data" / "update_state.json"))
    monkeypatch.setattr(mod.mcs_recovery_core, "UPDATE_LOCK",
                        str(tmp_path / "data" / "update.lock"))
    monkeypatch.setattr(mod.mcs_recovery_core, "RUN_LOCK",
                        str(tmp_path / "data" / "run.lock"))
    monkeypatch.setattr(mod.mcs_recovery_core, "MARKER_PATH",
                        str(tmp_path / "data" / "marker"))
    monkeypatch.setattr(mod.mcs_recovery_core, "REPORT_PATH",
                        str(tmp_path / "data" / "recovery_report.json"))
    monkeypatch.setattr(mod.mcs_recovery_core, "MANIFEST_PATH",
                        str(tmp_path / "data" / "service_manifest.json"))
    monkeypatch.setattr(mod.mcs_recovery_core, "SCRIPTS_DIR", str(tmp_path / "hermes-scripts"))
    monkeypatch.setattr(mod.mcs_recovery_core, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(mod.mcs_recovery_core, "RESIDENT_LABELS", ())
    os.makedirs(tmp_path / "data", exist_ok=True)
    return mod


@pytest.fixture
def gateway_restarts(rec, monkeypatch):
    calls = []
    original_popen = subprocess.Popen

    def local_popen(args, **kwargs):
        if len(args) > 2 and args[1:4] == ["-I", "-c", rec.mcs_recovery_gateway._GATEWAY_RESTART_PROGRAM]:
            calls.append(args)
            return None
        return original_popen(args, **kwargs)

    monkeypatch.setattr(rec.subprocess, "Popen", local_popen)
    return calls


def _applying(prev, tag="v1.1.0", sha="t" * 40, stage="quiesce",
              ago=4000):
    return {"v": 1,
            "applying": {"tag": tag, "sha": sha, "prev_sha": prev,
                         "at": time.time() - ago},
            "stages": [{"stage": stage, "at": time.time() - ago + 100}],
            "applied": [], "attempts": {}, "executed": {}}


# ---------------------------------------------------- _restore_db contract
# Rollback recovery must FAIL CLOSED: an unreadable backup/live DB or a
# failed copy can never be a silent "nothing to do" — that would report
# success while old code runs against a newer schema (F-restore).

def _mk_db(path, version):
    import sqlite3
    con = sqlite3.connect(str(path))
    con.execute(f"PRAGMA user_version={version}")
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()


def _consent(rec, live, back, cid="cid-consent", report=None):
    """Commit a synthetic ops.restore_approve receipt bound to the
    current loss report — the same row the human-approval path writes."""
    import sqlite3
    if report is None:
        report = rec.mcs_recovery_database._loss_report(str(back))
    con = sqlite3.connect(str(live))
    con.execute("""CREATE TABLE IF NOT EXISTS command_receipts(
      command_id TEXT PRIMARY KEY, payload_hash TEXT, project_id INTEGER,
      request_id INTEGER,
      outcome TEXT CHECK(outcome IN ('applied','rejected')),
      receipt_json TEXT, processed_at REAL)""")
    con.execute(
        "INSERT OR REPLACE INTO command_receipts VALUES(?,?,NULL,NULL,"
        "'applied',?,?)",
        (cid, "h" * 64, json.dumps({
            "cmd": "ops.restore_approve", "scheduled": True,
            "command_id": cid, "report_id": report["report_id"],
            "backup_sha256": report["backup_sha256"],
            "backup_schema": report["backup_schema"]}), time.time()))
    con.commit()
    con.close()
    return report


def _rollback_journal(rec, backup):
    """The production precondition of a DB replace: a rollback-shaped
    applying journal (recover only replaces from that journal)."""
    state = rec.mcs_recovery_core._load_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "b" * 40, "prev_sha": "a" * 40,
                         "rollback": True, "backup_path": str(backup), "at": time.time()}
    rec.mcs_recovery_core._save_state(state)


def _journal_kept(rec):
    applying = rec.mcs_recovery_core._load_state()["applying"]
    assert applying["rollback"] is True and "restore_staging" not in applying


def _rollback_state(rec, tmp_path, backup_path):
    """Journal state for the post-merge rollback-recovery path:
    HEAD == applying.sha (tree already converged), rollback flagged."""
    repo = _make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = _applying("0" * 40, sha=head, stage="applying")
    state["applying"].update(
        {"rollback": True, "command_id": "cid-rb",
         "backup_path": backup_path})
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    return state


def _backup_membership(rec, tmp_path, monkeypatch, *, enabled=False, template=False,
                       proof="manifest", listed="mcs_offsite.sh", returncode=0):
    root = Path(rec.mcs_recovery_core.DATA).parent
    root.joinpath("config.json").write_text(json.dumps(
        {"runtime_mode": "hermes", "backup": {"enabled": enabled}}))
    if template:
        wrapper = Path(rec.mcs_recovery_core.REPO, "deployment/scripts/mcs_offsite.sh")
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text("# synthetic template: never executed")
    evidence = {"cron": [{"id": "abcdef", "script": "mcs_offsite.sh"}]}
    if proof == "manifest":
        Path(rec.mcs_recovery_core.MANIFEST_PATH).write_text(json.dumps(evidence))
    hermes = tmp_path / "synthetic-hermes"
    hermes.write_text("# synthetic placeholder: never executed")
    monkeypatch.setattr(rec.shutil, "which", lambda name: str(hermes))
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        if argv == [str(hermes), "cron", "list", "--all"]:
            return subprocess.CompletedProcess(
                argv, returncode,
                f"  abcdef [disabled]\n    Script:  {listed}\n"
                "  123abc [active]\n    Script:  /shared/mcs_custom.sh\n"
                "  456def [active]\n    Script:  mcs_health.sh\n", "")
        assert argv == [str(hermes), "cron", "remove", "abcdef"]
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(rec.subprocess, "run", run)
    return hermes, calls, evidence


def _standalone_recovery(rec):
    Path(rec.mcs_recovery_core.DATA).parent.joinpath("config.json").write_text(json.dumps({"runtime_mode": "standalone"}))
    value = {"pid": os.getpid(), "generation": "b" * 32, "updated_at": time.time(),
             "children": {name: {"pid": os.getpid(), "kind": "background"}
                          for name in ("extract-0", "extract-2", "extract-night")}}
    path = Path(rec.mcs_recovery_core.DATA, "standalone-status.json")
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return value


class _EnsureLaunchd:
    """launchctl stub with per-label loaded/running state; `hung`
    labels time out on every verb."""

    def __init__(self, loaded=(), running=(), hung=()):
        self.loaded, self.running = set(loaded), set(running)
        self.hung = set(hung)
        self.calls = []

    def __call__(self, argv, *args, **kwargs):
        verb, target = argv[1], argv[-1]
        label = os.path.basename(target)[:-6] \
            if verb == "bootstrap" else target.rsplit("/", 1)[-1]
        self.calls.append((verb, label))
        if label in self.hung:
            raise subprocess.TimeoutExpired(argv, 30)
        rc, out = 0, ""
        if verb == "bootout":
            self.loaded.discard(label)
            self.running.discard(label)
        elif verb in ("bootstrap", "kickstart"):
            if verb == "bootstrap":
                self.loaded.add(label)
            if label in self.loaded:
                self.running.add(label)
        elif verb == "print":
            rc = 0 if label in self.loaded else 113
            out = "\tpid = 4242\n" if label in self.running else ""
        return subprocess.CompletedProcess(argv, rc, out, "")


# ------ every escalation inside a consent hold keeps the freeze

def _held_world(rec, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    _rollback_state(rec, tmp_path, str(back))
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    monkeypatch.setattr(rec.mcs_recovery_services, "_reconcile_membership", lambda s: [])
    restarts, notices = [], []
    monkeypatch.setattr(rec.mcs_recovery_services, "_restart_drainers",
                        lambda **k: restarts.append(k) or [])
    monkeypatch.setattr(rec.mcs_recovery_services, "_notify", notices.append)
    Path(rec.mcs_recovery_core.MARKER_PATH).write_text("1")
    assert rec.recover() == 0                       # held
    return live, back, restarts, notices


def _assert_frozen(rec, tmp_path, live, live_before, restarts):
    assert restarts == []
    assert Path(rec.mcs_recovery_core.MARKER_PATH).exists()
    marker = tmp_path / "data" / "restore_pending.json"
    assert json.loads(marker.read_text())["phase"] == "awaiting_consent"
    state = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())
    assert state["applying"]["rollback"] and state["restore_consent"]
    assert "cid-rb" not in state.get("executed", {})
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
    assert report["result"] == "restore_consent_blocked"
    assert live.read_bytes() == live_before


def _commit_recovery_schema(rec, tmp_path, version):
    repo = tmp_path / "repo"
    canonical = repo / "mcs" / "core" / "ledger.py"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("SCHEMA_VERSION = " + str(version) + "\nraise AssertionError('restored code must not be imported')\n")
    _git(repo, "add", "mcs/core/ledger.py")
    _git(repo, "commit", "-qm", "synthetic canonical schema")
    state = rec.mcs_recovery_core._load_state()
    state["applying"]["sha"] = _git(repo, "rev-parse", "HEAD").stdout.strip()
    rec.mcs_recovery_core._save_state(state)
    return canonical
