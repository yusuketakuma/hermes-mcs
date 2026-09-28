"""mcs_update — updater state, tag detection, gates, receipt queue.

Fully synthetic: temp git repos + temp sqlite + stubbed subprocess/
network. No real MCS, Discord, Keychain, or external repo access.
"""
import json
import os
import sqlite3
from contextlib import suppress
import subprocess
import time
from pathlib import Path

import pytest

import mcs_update


# ---------------------------------------------------------------- helpers

def _git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo, *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
    return r


def _make_repo(tmp_path):
    """A repo with v1.0.0 (lightweight) and v1.1.0 (annotated) tags."""
    bare = tmp_path / "remote.git"
    work = tmp_path / "remote-work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "f.txt").write_text("one")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "c1")
    _git(work, "tag", "v1.0.0")            # lightweight — no peel line
    (work / "f.txt").write_text("two")
    _git(work, "commit", "-qam", "c2")
    _git(work, "tag", "-a", "v1.1.0", "-m", "release")   # annotated
    _git(work, "init", "-q", "--bare", str(bare))
    _git(work, "push", "-q", str(bare), "main",
         "v1.0.0", "v1.1.0")

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(bare), str(repo)],
                   check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    return repo, bare


@pytest.fixture
def updater(tmp_path, monkeypatch):
    """mcs_update pointed at temp dirs — REPORT_PATH included so a
    recovery path can never scribble on the real runtime."""
    monkeypatch.setattr(mcs_update, "REPO", str(tmp_path / "repo"))
    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(mcs_update, "STATE_PATH",
                        str(tmp_path / "data" / "update_state.json"))
    monkeypatch.setattr(mcs_update, "UPDATE_LOCK",
                        str(tmp_path / "data" / "update.lock"))
    monkeypatch.setattr(mcs_update, "RUN_LOCK",
                        str(tmp_path / "data" / "run.lock"))
    monkeypatch.setattr(mcs_update, "MARKER_PATH",
                        str(tmp_path / "data" / "marker"))
    monkeypatch.setattr(mcs_update, "REPORT_PATH",
                        str(tmp_path / "data" / "recovery_report.json"))
    monkeypatch.setattr(mcs_update, "RESTORE_REPORT_PATH",
                        str(tmp_path / "data" / "restore_report.json"))
    monkeypatch.setattr(mcs_update, "LEDGER", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(mcs_update, "BACKUP_DIR",
                        str(tmp_path / "data" / "backups"))
    monkeypatch.setattr(mcs_update, "MANIFEST_PATH",
                        str(tmp_path / "data" / "service_manifest.json"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    return mcs_update


# ---------------------------------------------------------------- state

def test_state_roundtrip_and_corrupt(updater, tmp_path):
    state = updater._default_state()
    state["stages"].append({"stage": "local_checks", "at": 1.0})
    updater.save_state(state)
    loaded = updater.load_state()
    assert loaded["stages"][0]["stage"] == "local_checks"
    assert loaded["v"] == 1
    (tmp_path / "data" / "update_state.json").write_text("{not json")
    assert updater.load_state().get("_corrupt") is True


@pytest.mark.parametrize("field,value", [
    ("v", True), ("stages", {}), ("stages", [None]),
    ("stages", [{"stage": "merge", "at": float("nan")}]),
    ("applying", []), ("applying", {"at": float("inf")}),
    ("applied", ["invalid"]), ("executed", []), ("attempts", []),
])
def test_corrupt_journal_shapes_are_rejected_by_both_readers(
        updater, tmp_path, monkeypatch, field, value):
    from test_mcs_recover import _load
    recovery = _load()
    monkeypatch.setattr(recovery, "STATE_PATH", updater.STATE_PATH)
    state = updater._default_state()
    state[field] = value
    updater.save_state(state)
    assert updater.load_state() == {"_corrupt": True}
    assert recovery._load_state() == {"_corrupt": True}


def test_restore_does_not_replace_unreadable_consent_marker(updater, tmp_path):
    marker = tmp_path / "data" / "restore_pending.json"
    marker.write_text("{broken")
    with pytest.raises(updater.UpdateError, match="restore_marker_unreadable"):
        updater._restore_db(str(tmp_path / "backup.db"))
    assert marker.read_text() == "{broken"


def test_update_lock_nonblocking(updater):
    fd = updater.acquire_update_lock()
    assert fd is not None
    assert updater.acquire_update_lock() is None
    os.close(fd)
    assert updater.acquire_update_lock() is not None


@pytest.mark.parametrize("interrupted", [False, True])
def test_rollback_keeps_manifest_and_restarts_changed_plugin(
        updater, tmp_path, monkeypatch, interrupted):
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    before = _git(repo, "rev-parse", "v1.0.0").stdout.strip()
    after = _git(repo, "rev-parse", "v1.1.0^{commit}").stdout.strip()
    desired = {"scripts": [], "agents": [{"label": "local.mcs-cmd"}], "cron": []}
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": after,
                         "prev_sha": before, "plugin_changed": True,
                         "manifest_snapshot": desired, "command_id": "apply-id"}
    monkeypatch.setattr(updater, "_services_reconcile", lambda: None)
    monkeypatch.setattr(updater, "restart_agents", lambda: [])
    monkeypatch.setattr(updater, "_postcheck", lambda *a: [])
    monkeypatch.setattr(updater, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(updater, "load_config", lambda: {})
    assert updater._post_merge(state) == 0
    state = updater.load_state()
    assert state["applied"][-1]["manifest_snapshot"] == desired
    state["stages"] = []
    restarts, memberships = [], []

    def restart(cfg):
        # The decision survives popping the entry, and durable bookkeeping
        # must precede a restart that could terminate this caller.
        persisted = updater.load_state()
        assert persisted["applied"] == []
        assert persisted["executed"]["rollback-id"]["result"] == "rolled_back"
        restarts.append(True)

    monkeypatch.setattr(updater, "restart_gateway", restart)
    monkeypatch.setattr(updater, "quiesce", lambda: [])
    monkeypatch.setattr(updater, "_reconcile_membership",
                        lambda manifest: memberships.append(manifest) or [])
    if interrupted:
        state["applying"] = {"tag": "rollback:v1.1.0", "sha": before,
                             "prev_sha": after, "rollback": True,
                             "plugin_changed": True, "manifest_snapshot": desired,
                             "command_id": "rollback-id", "at": time.time()}
        state["stages"] = [{"stage": "rollback", "at": time.time()}]
        _git(repo, "reset", "--hard", before)
    updater.save_state(state)
    assert (updater.recover_interrupted() if interrupted
            else updater.rollback("rollback-id")) == 0
    assert updater.load_state()["applied"] == []
    assert restarts == [True]
    assert memberships == [desired]


# ------------------------------------------------------------ tag parsing

def test_remote_tag_sha_annotated_and_lightweight(updater, tmp_path):
    repo, _ = _make_repo(tmp_path)
    monkeypatch_repo = str(repo)
    import mcs_update as mu
    mu.REPO = monkeypatch_repo
    lite = mu.remote_tag_sha("v1.0.0")
    ann = mu.remote_tag_sha("v1.1.0")
    commit_lite = _git(repo, "rev-list", "-n1", "v1.0.0").stdout.strip()
    commit_ann = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    assert lite == commit_lite           # lightweight: direct = commit
    assert ann == commit_ann             # annotated: peeled = commit
    assert ann != _git(repo, "rev-parse", "v1.1.0").stdout.strip()


def test_detect_latest_semver_max(updater, tmp_path):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    tag, sha = mcs_update.detect_latest()
    assert tag == "v1.1.0"
    assert len(sha) == 40


def test_detect_latest_prerelease_excluded(updater, monkeypatch):
    def fake_git(args, timeout=30):
        class R:
            returncode = 0
            stdout = (("a" * 40) + "\trefs/tags/v1.2.0\n"
                      + ("b" * 40) + "\trefs/tags/v1.3.0-rc1\n"
                      + ("c" * 40) + "\trefs/tags/v1.3.0-rc1^{}\n")
            stderr = ""
        return R()
    monkeypatch.setattr(mcs_update, "_git", fake_git)
    tag, sha = mcs_update.detect_latest()
    assert tag == "v1.2.0"
    tag, sha = mcs_update.detect_latest(include_prerelease=True)
    # opt-in picks the prerelease — and peels it to the commit sha
    assert tag == "v1.3.0-rc1"
    assert sha == "c" * 40


def test_prerelease_numeric_components_follow_semver_order():
    names = ["v1.2.0-rc.2", "v1.2.0-rc.10", "v1.2.0"]
    assert sorted(reversed(names), key=mcs_update._ver_key) == names
    assert mcs_update._ver_key(["invalid"]) is None


def test_malformed_approval_cannot_break_queue_or_veto_valid_apply(updater, tmp_path):
    con = _receipts_db(updater.LEDGER)
    receipts = [
        ("valid", {"cmd": "ops.update_apply", "scheduled": True,
                   "tag": "v1.2.0", "target_sha": "a" * 40}),
        ("bad", {"cmd": "ops.update_apply", "scheduled": True,
                 "tag": ["invalid"], "target_sha": "a" * 40}),
        ("unscheduled", {"cmd": "ops.update_rollback", "scheduled": False}),
    ]
    for timestamp, (cid, receipt) in enumerate(receipts):
        con.execute("INSERT INTO command_receipts VALUES(?,?,NULL,NULL,'applied',?,?)",
                    (cid, "h" * 64, json.dumps(receipt), timestamp))
    con.commit()
    con.close()
    candidates, consumed = updater.scan_pending_approvals(updater._default_state())
    assert [candidate["command_id"] for candidate in candidates] == ["valid"]
    assert consumed == []


def test_defuse_mentions():
    out = mcs_update.defuse_mentions(
        "ping @everyone <@123> <@&456> <#789> @here")
    assert "@everyone" not in out and "@here" not in out
    assert "<@123>" not in out and "<#789>" not in out
    assert "＠everyone" in out


def test_impact_summary(updater, tmp_path):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    cur = _git(repo, "rev-parse", "HEAD~0").stdout.strip()
    # tag touches only f.txt — no impact flags
    assert mcs_update.impact_summary(cur, "v1.1.0") == []


# ------------------------------------------------------- protected paths

def test_precheck_tag_rejects_protected_and_symlink(updater, tmp_path,
                                                    monkeypatch):
    repo, _ = _make_repo(tmp_path)
    work = tmp_path / "remote-work"
    (work / "data").mkdir(exist_ok=True)
    (work / "data" / "evil").write_text("x")
    (work / "link.sh").unlink(missing_ok=True)
    os.symlink("f.txt", work / "link.sh")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "evil")
    _git(work, "tag", "v9.9.9")
    _git(work, "push", "-q", str(tmp_path / "remote.git"),
         "main", "v9.9.9")
    mcs_update.REPO = str(repo)
    _git(repo, "fetch", "-q", "--tags")
    errors = mcs_update.precheck_tag("v9.9.9")
    assert any("protected_path" in e for e in errors)
    assert any("bad_entry_type" in e for e in errors)


@pytest.mark.parametrize("name", ["user-added.txt", "新規ファイル.txt", "sp ace.txt"])
def test_rollback_preserves_untracked_update_name(updater, tmp_path, monkeypatch, name):
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    previous = _git(repo, "rev-parse", "HEAD").stdout.strip()
    path = repo / name
    path.write_text("release content")
    _git(repo, "add", path.name)
    _git(repo, "commit", "-qm", "release adds path")
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "reset", "--hard", previous)
    path.write_text("user content after interrupted update")
    monkeypatch.setattr(updater, "_reconcile_membership", lambda manifest: [])
    monkeypatch.setattr(updater, "_services_reconcile", lambda: None)

    updater._rollback_tree({"sha": target, "prev_sha": previous})

    assert path.read_text() == "user content after interrupted update"


def test_stale_git_lock_cleanup(updater, tmp_path):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    # a FRESH lock may belong to a live git process — kept (H3)
    assert updater._clean_stale_git_locks() == []
    assert lock.exists()
    old = time.time() - mcs_update.GIT_LOCK_MIN_AGE_S - 60
    os.utime(lock, (old, old))
    assert updater._clean_stale_git_locks() == [str(lock)]
    assert not lock.exists()


# ------------------------------------------------------------ receipts

def _receipts_db(path):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS command_receipts(
      command_id TEXT PRIMARY KEY, payload_hash TEXT, project_id INTEGER,
      request_id INTEGER,
      outcome TEXT CHECK(outcome IN ('applied','rejected')),
      receipt_json TEXT, processed_at REAL)""")
    return con


def _seed_consent(ledger_path, backup_path, cid="cid-consent",
                  report=None):
    """Drop an ops.restore_approve receipt bound to the CURRENT loss
    report into the live DB — the same row the human-approval path
    commits. Returns the report it was bound to."""
    if report is None:
        report = mcs_update._restore_loss_report(backup_path)
    con = _receipts_db(ledger_path)
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


def _rec(cid, cmd, at, **kw):
    return json.dumps({"cmd": cmd, "scheduled": True,
                       "command_id": cid, **kw})


def test_scan_pending_newest_tag_and_veto(updater, tmp_path):
    db = tmp_path / "ledger.db"
    con = _receipts_db(db)
    sha = "a" * 40
    rows = [
        ("cid-old", _rec("cid-old", "ops.update_apply", 1,
                         tag="v1.0.0", target_sha=sha, base_sha=sha), 1),
        ("cid-new", _rec("cid-new", "ops.update_apply", 2,
                         tag="v1.2.0", target_sha=sha, base_sha=sha), 2),
        ("cid-rb", _rec("cid-rb", "ops.update_rollback", 3), 3),
    ]
    con.executemany(
        "INSERT INTO command_receipts VALUES(?,?,NULL,NULL,'applied',?,?)",
        [(c, "h" * 64, j, t) for c, j, t in rows])
    con.commit()
    con.close()
    updater.LEDGER = str(db)
    cand, consumed = updater.scan_pending_approvals(
        updater._default_state())
    # the rollback vetoed both applies; only it remains as a candidate
    assert [c["command_id"] for c in cand] == ["cid-rb"]
    assert cand[0]["rollback"] is True
    assert sorted(dict(consumed).keys()) == ["cid-new", "cid-old"]
    assert set(dict(consumed).values()) == {"vetoed"}


def test_scan_pending_supersede(updater, tmp_path):
    db = tmp_path / "ledger.db"
    con = _receipts_db(db)
    sha = "a" * 40
    for cid, tag, at in (("c1", "v1.0.0", 1), ("c2", "v1.2.0", 2),
                         ("c3", "v1.10.0", 3)):
        con.execute(
            "INSERT INTO command_receipts VALUES(?,?,NULL,NULL,"
            "'applied',?,?)",
            (cid, "h" * 64, _rec(cid, "ops.update_apply", at, tag=tag,
                                 target_sha=sha), at))
    con.commit()
    con.close()
    updater.LEDGER = str(db)
    cand, consumed = updater.scan_pending_approvals(
        updater._default_state())
    # numeric semver: v1.10.0 > v1.2.0 (string compare would pick v1.2.0)
    assert [c["tag"] for c in cand] == ["v1.10.0"]
    assert sorted(dict(consumed).values()) == ["superseded"] * 2


def test_scan_skips_executed(updater, tmp_path):
    db = tmp_path / "ledger.db"
    con = _receipts_db(db)
    sha = "a" * 40
    con.execute(
        "INSERT INTO command_receipts VALUES(?,?,NULL,NULL,'applied',?,?)",
        ("c1", "h" * 64, _rec("c1", "ops.update_apply", 1, tag="v1.0.0",
                              target_sha=sha), 1))
    con.commit()
    con.close()
    updater.LEDGER = str(db)
    state = updater._default_state()
    state["executed"] = {"c1": {"result": "applied", "at": 1}}
    cand, consumed = updater.scan_pending_approvals(state)
    assert cand == [] and consumed == []


def test_scan_missing_ledger(updater, tmp_path):
    updater.LEDGER = str(tmp_path / "nonexistent.db")
    cand, consumed = updater.scan_pending_approvals(
        updater._default_state())
    assert cand == [] and consumed == []


def test_scan_same_tag_newest_receipt_wins(updater, tmp_path):
    """Two approvals of the same tag: the NEWER receipt wins — it
    carries the freshest sha pin; the older is superseded (B)."""
    db = tmp_path / "ledger.db"
    con = _receipts_db(db)
    sha_old, sha_new = "a" * 40, "b" * 40
    for cid, sha, at in (("c-old", sha_old, 1), ("c-new", sha_new, 2)):
        con.execute(
            "INSERT INTO command_receipts VALUES(?,?,NULL,NULL,"
            "'applied',?,?)",
            (cid, "h" * 64, _rec(cid, "ops.update_apply", at,
                                 tag="v1.2.0", target_sha=sha), at))
    con.commit()
    con.close()
    updater.LEDGER = str(db)
    cand, consumed = updater.scan_pending_approvals(
        updater._default_state())
    assert [c["command_id"] for c in cand] == ["c-new"]
    assert cand[0]["target_sha"] == sha_new
    assert dict(consumed) == {"c-old": "superseded"}


# ------------------------------------------------------------- apply path

def _apply_env(updater, monkeypatch, *, precheck_errors=(),
               cfg=None, sha=None):
    """Stub every apply() external boundary; the temp repo + real
    tags remain real (fetch/rev-parse/merge hit the file remote)."""
    repo, _ = _make_repo(Path(mcs_update.DATA).parent)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "precheck_local", lambda c: [])
    monkeypatch.setattr(mcs_update, "precheck_tag",
                        lambda t: list(precheck_errors))
    monkeypatch.setattr(mcs_update, "remote_tag_sha",
                        lambda t: sha)
    monkeypatch.setattr(mcs_update, "load_config",
                        lambda: cfg or {})
    monkeypatch.setattr(mcs_update, "_baseline_check", lambda c: [])
    monkeypatch.setattr(mcs_update, "quiesce", lambda: [])
    monkeypatch.setattr(mcs_update, "restart_agents", lambda: [])
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(mcs_update, "_postcheck", lambda s, e: [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)
    import maintenance
    monkeypatch.setattr(maintenance, "preupdate_backup",
                        lambda db: str(db) + ".bak")
    return repo


def test_bail_consumes_failed_receipt(updater, tmp_path, monkeypatch):
    """A failed apply attempt MUST consume the receipt — otherwise the
    next daily check retries it forever (A)."""
    repo = _apply_env(updater, monkeypatch, precheck_errors=["boom"])
    (tmp_path / "remote-work" / "v2.txt").write_text("x")
    _git(tmp_path / "remote-work", "add", ".")
    _git(tmp_path / "remote-work", "commit", "-qm", "c")
    _git(tmp_path / "remote-work", "tag", "v9.9.9")
    _git(tmp_path / "remote-work", "push", "-q",
         str(tmp_path / "remote.git"), "main", "v9.9.9")
    sha = _git(repo, "rev-list", "-n1", "v9.9.9",
               check=False).stdout.strip()
    if not sha:
        _git(repo, "fetch", "-q", "--tags")
        sha = _git(repo, "rev-list", "-n1", "v9.9.9").stdout.strip()
    monkeypatch.setattr(mcs_update, "remote_tag_sha", lambda t: sha)
    rc = updater.apply("v9.9.9", sha, "cid-fail")
    assert rc == 1
    after = updater.load_state()
    assert after["executed"]["cid-fail"]["result"] == "failed"
    assert after["attempts"]["v9.9.9"]["result"] == "failed"


def test_apply_transient_lock_failure_keeps_receipt(updater, tmp_path,
                                                    monkeypatch):
    """run_lock_timeout never consumes the receipt — nothing was
    attempted; a later check may legitimately retry."""
    repo = _apply_env(updater, monkeypatch)
    sha = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    monkeypatch.setattr(mcs_update, "remote_tag_sha", lambda t: sha)
    monkeypatch.setattr(mcs_update, "_acquire_run_lock_wait",
                        lambda **kw: None)
    rc = updater.apply("v1.1.0", sha, "cid-wait")
    assert rc == 2
    after = updater.load_state()
    assert "cid-wait" not in after.get("executed", {})
    assert after["stages"] == []       # journal cleaned, not wedged


def test_apply_update_lock_busy_returns_cleanly(updater, tmp_path,
                                               monkeypatch):
    """A live updater holds update.lock — a second apply must exit
    without touching state rather than queue another attempt."""
    _apply_env(updater, monkeypatch)
    fd = updater.acquire_update_lock()
    try:
        assert updater.apply("v1.1.0", "a" * 40, "cid-2") == 0
    finally:
        os.close(fd)


def test_schema_bump_never_auto_applies(updater, tmp_path, monkeypatch):
    """mode=auto + a schema-bumping tag => refused; only a human
    receipt may proceed past this gate (R2)."""
    repo = _apply_env(updater, monkeypatch,
                      precheck_errors=["schema_bump:7->8"],
                      cfg={"update": {"mode": "auto"}})
    sha = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    monkeypatch.setattr(mcs_update, "remote_tag_sha", lambda t: sha)
    rc = updater.apply("v1.1.0", sha, None)   # auto path: no receipt
    assert rc == 1
    after = updater.load_state()
    assert after["attempts"]["v1.1.0"]["result"] == "failed"
    assert "schema_bump_auto_blocked" in \
        after["attempts"]["v1.1.0"]["detail"]


# ----------------------------------------------------------------- config

def test_update_config_validation():
    import mcs_setup
    errs, _ = mcs_setup.validate_config(
        {"mcs_login_id": "u", "notify_target": "x",
         "update": {"mode": "notify", "auto_delay_h": 24}})
    assert not any("update" in e for e in errs)
    errs, _ = mcs_setup.validate_config(
        {"mcs_login_id": "u", "notify_target": "x",
         "update": {"mode": "bogus"}})
    assert any("update.mode" in e for e in errs)
    errs, _ = mcs_setup.validate_config(
        {"mcs_login_id": "u", "notify_target": "x",
         "update": {"auto_delay_h": -1}})
    assert any("update.auto_delay_h" in e for e in errs)


# ---------------------------------------------------------------- recover

def test_recover_interrupted_pre_merge(updater, tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = updater._default_state()
    state["applying"] = {"tag": "v9.9.9",
                         "sha": "f" * 40,        # != HEAD → pre-merge
                         "prev_sha": head,
                         "at": time.time() - 4000}
    state["stages"] = [{"stage": "quiesce", "at": time.time() - 3000}]
    updater.save_state(state)
    rc = updater.recover_interrupted()
    assert rc == 0
    after = updater.load_state()
    assert after["applying"] is None and after["stages"] == []


def test_recover_merge_head_abort(updater, tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / ".git" / "MERGE_HEAD").write_text(prev + "\n")
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": prev, "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    updater.save_state(state)
    rc = updater.recover_interrupted()
    assert rc == 0
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == prev


def test_recover_escalates_unclassifiable(updater, tmp_path,
                                          monkeypatch):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    state = updater._default_state()
    # HEAD moved somewhere unknown mid-merge
    state["applying"] = {"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": "0" * 40, "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    updater.save_state(state)
    rc = updater.recover_interrupted()
    assert rc == 1
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "escalate"
    # destructive actions must not have run
    assert _git(repo, "rev-parse", "HEAD").returncode == 0


def test_recover_pre_applying_remnant_is_cleaned(updater, tmp_path,
                                                 monkeypatch):
    """Stages without `applying` used to wedge every future update —
    a crash before the 'applying' journal wrote nothing to the tree,
    so recovery must simply clean up (F10)."""
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    state = updater._default_state()
    state["stages"] = [{"stage": "local_checks", "at": time.time() - 4000},
                       {"stage": "fetch", "at": time.time() - 3900}]
    updater.save_state(state)
    assert updater.recover_interrupted() == 0
    after = updater.load_state()
    assert after["stages"] == [] and after["applying"] is None
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "interrupted_pre_merge"


def test_recover_completes_done_bookkeeping(updater, tmp_path,
                                            monkeypatch):
    """Child wrote 'done' then died before the parent consumed the
    receipt — recovery completes the bookkeeping (H5)."""
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    state = updater._default_state()
    state["stages"] = [{"stage": "done", "at": time.time() - 100}]
    state["applied"] = [{"tag": "v1.1.0", "sha": "b" * 40,
                         "prev_sha": "a" * 40, "command_id": "cid-9",
                         "at": time.time()}]
    updater.save_state(state)
    assert updater.recover_interrupted() == 0
    after = updater.load_state()
    assert after["stages"] == []
    assert after["executed"]["cid-9"]["result"] == "applied"
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "resumed_done"


def test_recover_mixed_tree_after_merge_stage_crash(updater, tmp_path,
                                                    monkeypatch):
    """'merge' is journaled BEFORE the merge — a crash during checkout
    leaves HEAD==prev with a dirty tree and no MERGE_HEAD. Stage-label
    gating could never reach this path; measurement must (F1)."""
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    target = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    # simulate a partially-written checkout: tracked file has target
    # content, update-introduced file exists untracked
    (repo / "f.txt").write_text("partial-checkout-state")
    (repo / "introduced.txt").write_text("new")
    work = tmp_path / "remote-work"
    (work / "introduced.txt").write_text("new")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "c3")
    _git(work, "tag", "v1.2.0")
    _git(work, "push", "-q", str(tmp_path / "remote.git"),
         "main", "v1.2.0")
    _git(repo, "fetch", "-q", "--tags")
    target = _git(repo, "rev-list", "-n1", "v1.2.0").stdout.strip()
    state = updater._default_state()
    state["applying"] = {"tag": "v1.2.0", "sha": target,
                         "prev_sha": prev, "command_id": "cid-mix",
                         "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    updater.save_state(state)
    assert updater.recover_interrupted() == 0
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == prev
    assert _git(repo, "status", "--porcelain", "-uno").stdout.strip() == ""
    assert (repo / "introduced.txt").read_text() == "new"
    after = updater.load_state()
    # the failed apply's receipt is consumed — no crash/apply loop
    assert after["executed"]["cid-mix"]["result"] == \
        "interrupted_recovered"


def test_recover_defers_when_run_lock_busy(updater, tmp_path,
                                           monkeypatch):
    """A busy run.lock means a live tick — recovery must defer and
    touch NOTHING (H1): no git lock removal, no state mutation."""
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    removed = []
    monkeypatch.setattr(mcs_update, "_clean_stale_git_locks",
                        lambda: removed.append(1) or [])
    monkeypatch.setattr(mcs_update, "_acquire_run_lock_wait",
                        lambda **kw: None)      # busy — defer
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": "0" * 40, "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    updater.save_state(state)
    assert updater.recover_interrupted() == 0
    assert removed == []
    # journal untouched — still waiting for a quiet moment
    assert updater.load_state()["applying"]["tag"] == "v1.1.0"


def test_recover_removes_orphan_marker(updater, tmp_path, monkeypatch):
    """Clean journal + leftover marker = dead writer — the marker must
    go or helper launchers stay suppressed forever (H9)."""
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "_acquire_run_lock_wait",
                        lambda **kw: os.open(mcs_update.RUN_LOCK,
                                             os.O_WRONLY | os.O_CREAT))
    Path(mcs_update.MARKER_PATH).write_text(str(time.time()))
    assert updater.recover_interrupted() == 0
    assert not Path(mcs_update.MARKER_PATH).exists()


def test_restore_db_removes_wal_sidecars(updater, tmp_path, monkeypatch):
    """Restoring over a WAL-mode DB must delete -wal/-shm first — a
    stale WAL replayed against the restored file corrupts it (F9)."""
    import ledger as _ledger  # noqa: F401 — real module, tmp DBs only
    live = str(tmp_path / "ledger.db")
    back = str(tmp_path / "backup.db")
    mcs_update.LEDGER = live
    for path, ver, tag in ((live, 7, "live"), (back, 6, "back")):
        con = sqlite3.connect(path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(f"PRAGMA user_version={ver}")
        con.execute("CREATE TABLE t(x)")
        con.execute("INSERT INTO t VALUES(?)", (tag,))
        con.commit()
        con.close()
    (Path(live).parent / "ledger.db-wal").write_bytes(b"stalewal")
    (Path(live).parent / "ledger.db-shm").write_bytes(b"staleshm")
    # valid_mcs_db is the live-schema gate — stub it to accept both
    monkeypatch.setattr(_ledger, "valid_mcs_db", lambda p: True)
    _seed_consent(live, back)
    mcs_update._restore_db(back)
    assert not Path(live + "-wal").exists()
    assert not Path(live + "-shm").exists()
    # _restore_db opens a Ledger for reconcile, which migrates the
    # restored file to SCHEMA_VERSION — the proof the swap happened is
    # the backup's own row, not user_version
    con = sqlite3.connect("file:" + live + "?mode=ro", uri=True)
    assert con.execute("SELECT x FROM t").fetchone()[0] == "back"
    con.close()


def test_restore_db_skips_same_version(updater, tmp_path, monkeypatch):
    """No restore when live already matches the backup schema — a
    failed apply may never have migrated the DB."""
    import ledger as _ledger
    live = str(tmp_path / "ledger.db")
    back = str(tmp_path / "backup.db")
    mcs_update.LEDGER = live
    for path in (live, back):
        con = sqlite3.connect(path)
        con.execute("PRAGMA user_version=7")
        con.execute("CREATE TABLE sentinel(x)")
        con.commit()
        con.execute("INSERT INTO sentinel VALUES(?)", (path,))
        con.commit()
        con.close()
    monkeypatch.setattr(_ledger, "valid_mcs_db", lambda p: True)
    mcs_update._restore_db(back)
    con = sqlite3.connect("file:" + live + "?mode=ro", uri=True)
    row = con.execute("SELECT x FROM sentinel").fetchone()[0]
    con.close()
    assert row == live               # untouched — no copy happened


def test_restore_db_io_error_is_update_error(updater, tmp_path,
                                             monkeypatch):
    """A raw OSError out of _restore_db would slip past every caller's
    `except UpdateError` (apply bail, recover escalate, rollback
    rb_error) — it must be converted so drainers are never left
    quiesced by an unhandled raise."""
    import ledger as _ledger
    live = str(tmp_path / "ledger.db")
    back = str(tmp_path / "backup.db")
    mcs_update.LEDGER = live
    for path, ver in ((live, 8), (back, 7)):
        con = sqlite3.connect(path)
        con.execute(f"PRAGMA user_version={ver}")
        con.execute("CREATE TABLE t(x)")
        con.commit()
        con.close()
    monkeypatch.setattr(_ledger, "valid_mcs_db", lambda p: True)
    _seed_consent(live, back)
    def fail_copy(*args):
        raise OSError("synthetic copy failure")
    monkeypatch.setattr(mcs_update.shutil, "copyfileobj", fail_copy)
    with pytest.raises(mcs_update.UpdateError):
        mcs_update._restore_db(back)
    con = sqlite3.connect("file:" + live + "?mode=ro", uri=True)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 8
    con.close()


def test_rollback_restarts_agents_on_unexpected_error(
        updater, tmp_path, monkeypatch):
    """_rollback_tree raising ANY exception (not just UpdateError) must
    still reach restart_agents — quiesced drainers can never be left
    down by an unhandled raise (H4)."""
    repo, _ = _make_repo(tmp_path)           # fixture REPO == this
    monkeypatch.setattr(mcs_update, "_acquire_run_lock_wait",
                        lambda **kw: os.open(mcs_update.RUN_LOCK,
                                             os.O_WRONLY | os.O_CREAT))
    monkeypatch.setattr(mcs_update, "quiesce", lambda: None)
    restarted = []
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda: restarted.append(1) or [])
    monkeypatch.setattr(
        mcs_update, "_rollback_tree",
        lambda e: (_ for _ in ()).throw(OSError("disk gone")))
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    monkeypatch.setattr(mcs_update, "_gateway_restart_if_needed",
                        lambda s: None)
    state = updater._default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": _git(repo, "rev-parse", "HEAD")
                         .stdout.strip(), "at": time.time()}]
    updater.save_state(state)
    rc = updater.rollback("cid-rb")
    assert rc == 1
    assert restarted == [1]                  # drainers brought back up
    after = updater.load_state()
    assert after["executed"]["cid-rb"]["result"] == "rollback_failed"


# ------------------------------------------------- restore consent gate
# R2/T13: a schema-bump rollback that would replace the live DB must
# wait for a NEW human approval bound to the exact backup bytes +
# schema + loss report. An earlier update/rollback approval never
# substitutes; every writer/sender stays frozen while it waits.

def _mk_schema(path, version, messages=0):
    """A real Ledger-created DB pinned to `version` — passes the real
    valid_mcs_db gate, so consent tests exercise the true restore path."""
    import ledger as _ledger
    lg = _ledger.Ledger(str(path))
    lg.db.execute(f"PRAGMA user_version={version}")
    for i in range(messages):
        lg.db.execute(
            "INSERT INTO messages(message_id,project_id,posted_at,"
            "posted_at_ts,body_html,body_state,content_hash,first_seen)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (i + 1, 1, "2026-01-01", 100 + i, "<b>x</b>", "full",
             f"h{i}", 1))
    lg.db.commit()
    lg.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    lg.db.close()
    for side in (str(path) + "-wal", str(path) + "-shm"):
        with suppress(OSError):
            os.unlink(side)


def _schema_bump_world(updater, tmp_path, monkeypatch):
    """applied v1.1.0 entry flagged schema_bump with a real v7 backup;
    live DB sits one schema ahead at v8. Every external boundary is
    stubbed; git reset and the DB files stay real."""
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    before = _git(repo, "rev-parse", "v1.0.0").stdout.strip()
    after = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    live = str(tmp_path / "data" / "ledger.db")   # under DATA — the
    # marker/data_root paths must coincide like production
    monkeypatch.setattr(mcs_update, "LEDGER", live)
    back = tmp_path / "data" / "backups" / "preupdate.db"
    back.parent.mkdir(parents=True, exist_ok=True)
    _mk_schema(back, 7, messages=2)
    _mk_schema(live, 8, messages=5)          # 3 rows since the backup
    # keep quiesce's real marker write — only launchd stops are stubbed
    monkeypatch.setattr(mcs_update, "quiesce",
                        lambda: (mcs_update._write_marker(), [])[1])
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(mcs_update, "_reconcile_membership", lambda m: [])
    monkeypatch.setattr(mcs_update, "_postcheck", lambda s, e: [])
    monkeypatch.setattr(mcs_update, "load_config", lambda: {})
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)
    restarts = []
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda: restarts.append(1) or [])
    state = updater._default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": after,
                         "prev_sha": before, "schema_bump": True,
                         "backup_path": str(back), "at": time.time()}]
    updater.save_state(state)
    return repo, live, str(back), before, after, restarts


def _live_version(path):
    con = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def test_rollback_schema_bump_holds_for_consent(updater, tmp_path,
                                                monkeypatch):
    """No bound receipt => the rollback stops before the DB replace:
    tree already reset, drainers down, BOTH markers up, loss report
    durable, live DB byte-identical (T13)."""
    repo, live, back, before, _after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    live_bytes = Path(live).read_bytes()
    rc = updater.rollback("cid-rb")
    assert rc == 2
    assert Path(live).read_bytes() == live_bytes   # no unauthorized swap
    assert _live_version(live) == 8
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert restarts == []                  # drainers stay quiesced
    assert Path(mcs_update.MARKER_PATH).exists()
    marker = json.loads(
        (tmp_path / "data" / "restore_pending.json").read_text())
    assert marker["phase"] == "awaiting_consent"
    report = json.loads(
        (tmp_path / "data" / "restore_report.json").read_text())
    assert marker["report_id"] == report["report_id"]
    assert report["backup_schema"] == 7
    assert report["stored_since_backup"]["messages"] == 3
    assert report["intervening_messages"] == 3
    assert report["backup_sha256"] == mcs_update._file_sha256(back)
    state = updater.load_state()
    assert state["applying"]["rollback"] is True
    assert state["restore_consent"]["report_id"] == report["report_id"]
    # the hold must NOT consume the rollback receipt — it stays pending
    # until the restore actually finishes
    assert "cid-rb" not in state.get("executed", {})


def test_rollback_consent_converges_via_recover(updater, tmp_path,
                                                monkeypatch):
    """After the bound ops.restore_approve receipt lands, recovery
    re-enters _restore_db: swap happens, reconcile clears the marker,
    drainers restart, the rollback completes (T13 happy path)."""
    repo, live, back, before, _after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    assert updater.rollback("cid-rb") == 2
    _seed_consent(live, back)
    assert updater.recover_interrupted() == 0
    assert _live_version(live) >= 7        # backup bytes restored
    con = sqlite3.connect("file:" + live + "?mode=ro", uri=True)
    assert con.execute(
        "SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    con.close()
    # reconcile consumed the marker — no stale hold pinning senders
    import notify_cards
    assert notify_cards.restore_pending(str(tmp_path / "data")) is None
    state = updater.load_state()
    assert state["applying"] is None
    assert "restore_consent" not in state
    assert state["executed"]["cid-rb"]["result"] == "rolled_back"
    assert restarts == [1]                 # drainers came back up


def test_rollback_wrong_report_consent_stays_held(updater, tmp_path,
                                                  monkeypatch):
    """A receipt bound to a DIFFERENT loss report never unlocks the
    swap — approval is bound to exact bytes+schema+loss (R2)."""
    _repo, live, back, _before, _after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    assert updater.rollback("cid-rb") == 2
    _seed_consent(live, back,
                  report={"report_id": "f" * 64,
                          "backup_sha256": "e" * 64,
                          "backup_schema": 7})
    assert updater.recover_interrupted() == 0
    assert _live_version(live) == 8        # still untouched
    import notify_cards
    marker = notify_cards.restore_awaiting_consent(
        str(tmp_path / "data"))
    assert marker is not None              # hold stands
    assert restarts == []


def test_rollback_update_receipt_cannot_satisfy_consent(
        updater, tmp_path, monkeypatch):
    """The earlier ops.update_apply approval that authorized the update
    must NOT count as restore consent — a distinct op name is required
    (the human must have seen the loss report first)."""
    _repo, live, back, _before, _after, _restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    assert updater.rollback("cid-rb") == 2
    # seed an applied ops.update_apply receipt — same payload shape the
    # earlier approval produced, minus the restore binding
    report = mcs_update._restore_loss_report(back)
    con = _receipts_db(live)
    con.execute(
        "INSERT OR REPLACE INTO command_receipts VALUES(?,?,NULL,NULL,"
        "'applied',?,?)",
        ("cid-apply-old", "h" * 64, json.dumps({
            "cmd": "ops.update_apply", "scheduled": True,
            "command_id": "cid-apply-old", "tag": "v1.1.0",
            "report_id": report["report_id"],          # even forged
            "backup_sha256": report["backup_sha256"],
            "backup_schema": report["backup_schema"]}),
         time.time()))
    con.commit()
    con.close()
    assert updater.recover_interrupted() == 0
    assert _live_version(live) == 8        # never swapped
    import notify_cards
    assert notify_cards.restore_awaiting_consent(
        str(tmp_path / "data")) is not None


def test_recover_escalates_orphaned_restore_consent(updater, tmp_path,
                                                    monkeypatch):
    """restore_consent with a non-rollback applying record is journal
    corruption — escalate fail-closed rather than classify away the
    hold (a 'finish' would wedge every send grant forever)."""
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "restart_agents", lambda: [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": head, "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    state["restore_consent"] = {"report_id": "a" * 64}
    updater.save_state(state)
    rc = updater.recover_interrupted()
    assert rc == 1
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "escalate"
    assert "restore_consent" in report["detail"]
    # journal preserved for a human — nothing classified away
    after = updater.load_state()
    assert after["applying"] is not None


def test_loss_report_binds_live_state(updater, tmp_path, monkeypatch):
    """report_id is deterministic over (backup bytes, backup schema,
    live-vs-backup deltas) — any write slipping past the writer hold
    changes the id and invalidates every stale consent (T13)."""
    _repo, live, back, _b, _a, _r = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    r1 = mcs_update._restore_loss_report(back)
    r2 = mcs_update._restore_loss_report(back)
    assert r1["report_id"] == r2["report_id"]     # deterministic
    # a write that slips in invalidates the bound report
    con = sqlite3.connect(live)
    con.execute(
        "INSERT INTO messages(message_id,project_id,posted_at,"
        "posted_at_ts,body_html,body_state,content_hash,first_seen)"
        " VALUES(99,1,'2026-01-02',999,'<b>y</b>','full','h9',1)")
    con.commit()
    con.close()
    r3 = mcs_update._restore_loss_report(back)
    assert r3["report_id"] != r1["report_id"]
    assert r3["intervening_messages"] == 4


def test_restore_consent_expires_when_existing_message_changes(
        updater, tmp_path, monkeypatch):
    _repo, live, back, _before, _after, _restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    approved = _seed_consent(live, back)
    with sqlite3.connect(live) as con:
        con.execute("UPDATE messages SET body_html='changed synthetic text'")
    current = updater._restore_loss_report(back)
    assert current["stored_since_backup"] == approved["stored_since_backup"]
    assert current["report_id"] != approved["report_id"]
    assert updater._restore_consent(current) is None


@pytest.mark.parametrize("independent", [False, True])
def test_failed_restore_copy_preserves_live_wal(updater, tmp_path, monkeypatch, independent):
    from test_mcs_recover import _load
    _repo, live, back, _before, _after, _restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    recovery = _load() if independent else updater
    monkeypatch.setattr(recovery, "LEDGER", live)
    monkeypatch.setattr(recovery, "DATA", updater.DATA)
    writer = sqlite3.connect(live)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE messages SET body_html='durable synthetic update'")
        writer.commit()
        _seed_consent(live, back)
        wal = Path(live + "-wal")
        before = wal.read_bytes()

        def fail_copy(*args):
            raise OSError("synthetic copy failure")

        monkeypatch.setattr(recovery.shutil, "copyfileobj", fail_copy)
        if independent:
            assert recovery._restore_db(back).startswith("restore_failed:")
        else:
            with pytest.raises(updater.UpdateError, match="restore_failed"):
                recovery._restore_db(back)
        assert wal.read_bytes() == before
        assert writer.execute("SELECT body_html FROM messages LIMIT 1").fetchone()[0] == (
            "durable synthetic update")
    finally:
        writer.close()


def test_apply_bail_consent_hold_keeps_writers_stopped(updater, tmp_path,
                                                       monkeypatch):
    """A post-merge failure on a schema-bump release that lands in the
    restore-consent hold keeps the same invariant as rollback(): the
    drainers the NEW-code child restarted are quiesced again and the
    update marker stays up until consent."""
    import maintenance
    repo, _ = _make_repo(tmp_path)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    sha = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    live = str(tmp_path / "data" / "ledger.db")
    monkeypatch.setattr(mcs_update, "LEDGER", live)
    back = tmp_path / "data" / "backups" / "pre.db"
    back.parent.mkdir(parents=True, exist_ok=True)
    _mk_schema(back, 7, messages=2)
    _mk_schema(live, 8, messages=5)
    monkeypatch.setattr(mcs_update, "load_config",
                        lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(mcs_update, "precheck_local", lambda c: [])
    monkeypatch.setattr(mcs_update, "remote_tag_sha", lambda t: sha)
    monkeypatch.setattr(mcs_update, "precheck_tag",
                        lambda t: ["schema_bump:7->8"])
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda p: str(back))
    monkeypatch.setattr(mcs_update, "_baseline_check", lambda c: [])
    events = []

    def quiesce():
        events.append("quiesce")
        mcs_update._write_marker()
        return []

    def restart():
        events.append("restart")
        mcs_update._remove_marker()   # real restart_agents drops it
        return []

    monkeypatch.setattr(mcs_update, "quiesce", quiesce)
    monkeypatch.setattr(mcs_update, "restart_agents", restart)
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(mcs_update, "_reconcile_membership", lambda m: [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)

    def post_merge(_state):
        st = mcs_update.load_state()
        mcs_update.journal(st, "services")
        restart()
        mcs_update.journal(st, "restart")
        raise mcs_update.UpdateError(
            "post_merge_failed: postcheck_failed: new_env_error: x")

    monkeypatch.setattr(mcs_update, "_run_post_merge", post_merge)
    assert mcs_update.apply("v1.1.0", sha, "cid-apply") == 2
    st = mcs_update.load_state()
    assert st.get("restore_consent")
    assert events[-1] == "quiesce" and "restart" in events
    assert Path(mcs_update.MARKER_PATH).exists()


def test_manual_rollback_defers_to_recovery_of_interrupted_apply(
        updater, monkeypatch):
    """An interrupted apply journal belongs to recovery — a manual
    rollback of the last APPLIED entry must not overwrite it."""
    state = {"v": 1, "applied": [{"tag": "v1.0.5", "sha": "a" * 40,
                                  "prev_sha": "b" * 40}],
             "applying": {"tag": "v1.0.6", "sha": "c" * 40,
                          "prev_sha": "a" * 40, "at": time.time()},
             "stages": [{"stage": "merge", "at": time.time()}],
             "attempts": {}, "executed": {}}
    mcs_update.save_state(state)
    calls = []
    monkeypatch.setattr(mcs_update, "recover_interrupted",
                        lambda *a, **k: calls.append(1) or 0)
    monkeypatch.setattr(mcs_update, "quiesce",
                        lambda: pytest.fail("rollback must not quiesce"))
    assert mcs_update.rollback() == 0
    assert calls == [1]
    assert mcs_update.load_state()["applying"]["tag"] == "v1.0.6"
