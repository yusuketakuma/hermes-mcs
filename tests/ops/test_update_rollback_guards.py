"""Updater rollback/restart guards (synthetic repo, temp DB, stubbed
launchctl and network)."""
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
import sqlite3

import pytest

import mcs_update
from ops_testkit import _git, _make_repo
from test_mcs_update import updater  # noqa: F401


def _head(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_rollback_with_unusable_schema_backup_never_resets(
        updater, tmp_path, monkeypatch):  # noqa: F811
    """maintenance keeps only the newest schema-bump backup: a second
    rollback step must stop before the reset, not leave old code on the
    newer schema."""
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    before = _git(repo, "rev-parse", "v1.0.0").stdout.strip()
    after = _head(repo)
    state = updater.journal_store.default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": after, "prev_sha": before,
                         "schema_bump": True,
                         "backup_path": str(tmp_path / "pruned.db")}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    notices = []
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(updater, "quiesce", lambda: pytest.fail("no quiesce"))
    monkeypatch.setattr(update_apply, 'quiesce', updater.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', updater.quiesce)
    monkeypatch.setattr(updater, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', updater._enqueue_notice)
    assert updater.rollback("rb-1") == 1
    assert _head(repo) == after
    st = updater.journal_store.load_state(updater.STATE_PATH)
    assert st["applying"] is None and st["stages"] == []
    assert st["applied"][-1]["sha"] == after
    assert st["executed"]["rb-1"]["result"] == "rollback_failed"
    assert "backup_invalid" in st["executed"]["rb-1"]["detail"]
    assert any("backup_invalid" in n for n in notices)


def _notify_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE notify_outbox(event_id INTEGER PRIMARY KEY, state TEXT,
        next_try REAL, updated_at REAL);
      CREATE TABLE notification_renders(delivery_id TEXT PRIMARY KEY,
        card_id INTEGER, op TEXT, state TEXT, intent_event_id INTEGER,
        updated_at REAL);
      CREATE TABLE notification_delivery_attempts(attempt_id TEXT,
        delivery_id TEXT, state TEXT);
      CREATE TABLE notification_intent_cards(event_id INTEGER, card_id INTEGER,
        state TEXT, delivery_id TEXT, required_render_rev INTEGER);
      INSERT INTO notify_outbox VALUES(1,'pending',9e9,0),(2,'pending',9e9,0);
      INSERT INTO notification_renders VALUES
        ('unsent',NULL,'notice','queued',1,0),
        ('attempted',NULL,'notice','queued',2,0),
        ('card',7,'create','queued',NULL,0),
        ('card-sent',8,'update','queued',NULL,0),
        ('done',NULL,'notice','delivered',1,0);
      INSERT INTO notification_delivery_attempts VALUES('a','attempted','unknown'),
        ('b','card-sent','granted');
      INSERT INTO notification_intent_cards VALUES(3,7,'pending','card',4),
        (4,8,'pending','card-sent',2);
    """)
    con.commit()
    con.close()


def test_rollback_cancels_every_unsent_queued_render(updater):  # noqa: F811
    """A queued card render carries the newer spec keys (post_actions,
    accent) too: the restored worker would refuse it forever while the
    restored runner counts it as live, so it is cancelled and its
    intents unbound for the restored sweep to re-issue."""
    _notify_db(updater.LEDGER)
    assert updater._cancel_unsent_notices() == 2
    con = sqlite3.connect(updater.LEDGER)
    states = dict(con.execute(
        "SELECT delivery_id,state FROM notification_renders"))
    due = dict(con.execute("SELECT event_id,next_try FROM notify_outbox"))
    intents = dict(con.execute(
        "SELECT event_id,delivery_id FROM notification_intent_cards"))
    con.close()
    assert states == {"unsent": "cancelled", "attempted": "queued",
                      "card": "cancelled", "card-sent": "queued",
                      "done": "delivered"}
    assert due[1] < 9e9 and due[2] == 9e9    # re-dispatched by restored code
    assert intents == {3: None, 4: "card-sent"}   # the granted one keeps its send


def test_cancel_unsent_notices_never_creates_a_missing_ledger(updater, tmp_path):  # noqa: F811
    assert updater._cancel_unsent_notices() == 0
    assert not (tmp_path / "ledger.db").exists()


@pytest.mark.parametrize("cfg,expected", [
    ({"notify": {"interactive": "lineworks"}},
     [["kickstart", "-k", "/ai.mcs.lineworks"], ["kickstart", "-k", "/ai.hermes.gateway"]]),
    ({"notify": {"interactive": "slack"}}, [["kickstart", "-k", "/ai.hermes.gateway"]]),
])
def test_restart_services_refreshes_the_lineworks_adapter(monkeypatch, cfg, expected):
    started = []
    monkeypatch.setattr(mcs_update.subprocess, "Popen",
                        lambda argv, **k: started.append(argv))
    mcs_update.restart_services(cfg, plugin_changed=True)
    got = [[a[1], a[2], "/" + a[3].rsplit("/", 1)[1]]
           if a[0] == "launchctl" else ["kickstart", "-k", "/ai.hermes.gateway"]
           for a in started]
    assert sorted(got) == sorted(expected)
    gateway = [a for a in started if a[0] != "launchctl"]
    assert len(gateway) == 1 and gateway[0][1:3] == ["-I", "-c"]
    assert gateway[0][3] == mcs_update._request_gateway_restart.__globals__["_GATEWAY_RESTART_PROGRAM"]


def test_restart_services_without_plugin_change_still_refreshes_lineworks(monkeypatch):
    started = []
    monkeypatch.setattr(mcs_update.subprocess, "Popen",
                        lambda argv, **k: started.append(argv))
    mcs_update.restart_services({"notify": {"interactive": "lineworks"}}, False)
    assert [a[-1].rsplit("/", 1)[1] for a in started] == ["ai.mcs.lineworks"]


def test_standalone_restart_leaves_lineworks_to_the_host(monkeypatch):
    requested = []
    monkeypatch.setattr(mcs_update, "restart_gateway", requested.append)
    monkeypatch.setattr(update_services, 'restart_gateway', mcs_update.restart_gateway)
    monkeypatch.setattr(mcs_update.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("no launchctl in standalone"))
    cfg = {"runtime_mode": "standalone", "notify": {"interactive": "lineworks"}}
    mcs_update.restart_services(cfg, False)
    assert requested == [cfg]


def test_known_lineworks_kickstart_failure_is_announced(monkeypatch):
    notices = []
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)

    def missing(argv, **k):
        raise OSError("synthetic launchctl missing")
    monkeypatch.setattr(mcs_update.subprocess, "Popen", missing)
    assert mcs_update.restart_lineworks({"notify": {"interactive": "lineworks"}}) is None
    assert len(notices) == 1 and "LINE WORKS" in notices[0]
    assert "synthetic" not in notices[0]             # fixed text, no OS message


def test_issued_lineworks_kickstart_raises_no_notice(monkeypatch):
    notices, started = [], []
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(mcs_update.subprocess, "Popen",
                        lambda argv, **k: started.append((argv, k)))
    mcs_update.restart_lineworks({"notify": {"interactive": "lineworks"}})
    assert notices == [] and len(started) == 1
    assert started[0][1]["start_new_session"] is True   # never killed with us


@pytest.mark.parametrize("cfg", [
    {}, {"notify": "lineworks"}, {"notify": {"interactive": "discord"}},
    {"runtime_mode": "standalone", "notify": {"interactive": "lineworks"}},
    {"runtime_mode": "synthetic-invalid", "notify": {"interactive": "lineworks"}},
])
def test_lineworks_refresh_is_a_quiet_noop_outside_its_config(monkeypatch, cfg):
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: pytest.fail("no notice"))
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(mcs_update.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("no launchctl"))
    assert mcs_update.restart_lineworks(cfg) is None


def test_release_notes_fetch_refuses_redirects(monkeypatch):
    import mcs_util
    seen = []

    class Opener:
        def open(self, req, timeout):
            raise OSError("synthetic: no network")

    monkeypatch.setattr(mcs_update, "_git_out",
                        lambda argv, **k: "https://github.com/o/r.git")
    monkeypatch.setattr(update_apply, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_planning, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_preflight, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_recovery, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_repository, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_restore, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(mcs_util, "no_proxy_opener",
                        lambda *h: seen.append(h) or Opener())
    assert mcs_update.fetch_notes("v1.0.0") is None
    assert seen == [(mcs_util.NoRedirect,)]


_SPLIT_REQUIRED = (
    "mcs/core/mcs_util.py", "mcs/core/provider_settings.py", "mcs/core/mcs_config.py",
    "mcs/core/safe_io.py", "mcs/llm/cloud_policy.py", "mcs/semantic/semantic_jev.py",
)


def _source_repo(tmp_path):
    repo, _ = _make_repo(tmp_path)
    for name in _SPLIT_REQUIRED + (
            "mcs/ingest/consumer.py", "mcs_standalone/config.py", "adapters/slack/config.py",
            "hermes_plugin/entry.py", "lineworks_adapter/__main__.py",
            "deployment/recovery/mcs_recover.py", "scripts/mcs_upgrade.py", "install.sh"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic source\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "synthetic split readers")
    original = _head(repo)
    (repo / "notes.txt").write_text("synthetic docs only")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-qm", "synthetic docs")
    return repo, original


def test_split_target_requires_matching_committed_real_source(updater, tmp_path, monkeypatch):  # noqa: F811
    repo, compatible = _source_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    assert updater._split_target_source_matches(compatible)
    path = repo / "mcs/ingest/consumer.py"
    original = path.read_text()
    path.write_text("# synthetic uncommitted reader\n")
    assert not updater._split_target_source_matches(compatible)
    path.write_text(original)
    (repo / "mcs/extra.py").write_text("# synthetic shadow\n")
    assert not updater._split_target_source_matches(compatible)
    (repo / "mcs/extra.py").unlink()
    path.write_text("# synthetic changed reader\n")
    _git(repo, "add", str(path))
    _git(repo, "commit", "-qm", "synthetic changed reader")
    assert not updater._split_target_source_matches(compatible)
    assert not updater._split_target_source_matches("f" * 39)


def test_split_target_rejects_staged_or_hidden_worktree_change(updater, tmp_path, monkeypatch):  # noqa: F811
    repo, compatible = _source_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    path = repo / "mcs/core/mcs_util.py"
    path.write_text("# synthetic changed loader\n")
    _git(repo, "update-index", "--assume-unchanged", "mcs/core/mcs_util.py")
    assert not updater._split_target_source_matches(compatible)


def test_provider_preflight_refuses_pending_missing_evidence_and_snapshot_change(monkeypatch, tmp_path):
    import provider_settings
    monkeypatch.setattr(mcs_update, "HOME", str(tmp_path))
    monkeypatch.setattr(update_apply, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_cli, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_execution, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_lifecycle_context, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_preflight, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_recovery, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_runtime_migration, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(provider_settings, "probe", lambda root: {"state": "pending"})
    with pytest.raises(mcs_update.UpdateError, match="settings_unverified"):
        mcs_update._provider_rollback_preflight("a" * 40)
    monkeypatch.setattr(provider_settings, "probe", lambda root: {"state": "requires_checked_read"})
    monkeypatch.setattr(provider_settings, "read", lambda *a, **k: {"mode": "split", "snapshot": {"v": 1}})
    monkeypatch.setattr(mcs_update, "_split_target_source_matches", lambda target, **kwargs: False)
    monkeypatch.setattr(update_preflight, '_split_target_source_matches', mcs_update._split_target_source_matches)
    monkeypatch.setattr(update_runtime_migration, '_split_target_source_matches', mcs_update._split_target_source_matches)
    with pytest.raises(mcs_update.UpdateError, match="target_unverified"):
        mcs_update._provider_rollback_preflight("a" * 40)
    states = iter([{"mode": "split", "snapshot": {"v": 1}}, {"mode": "split", "snapshot": {"v": 2}}])
    monkeypatch.setattr(provider_settings, "read", lambda *a, **k: next(states))
    monkeypatch.setattr(mcs_update, "_split_target_source_matches", lambda target, **kwargs: True)
    monkeypatch.setattr(update_preflight, '_split_target_source_matches', mcs_update._split_target_source_matches)
    monkeypatch.setattr(update_runtime_migration, '_split_target_source_matches', mcs_update._split_target_source_matches)
    with pytest.raises(mcs_update.UpdateError, match="settings_changed"):
        mcs_update._provider_rollback_preflight("a" * 40)


@pytest.mark.parametrize("state", ["root_missing", "uninitialized_or_legacy_unverified", "legacy_unverified"])
def test_provider_preflight_preserves_legacy_behavior(monkeypatch, tmp_path, state):
    import provider_settings
    monkeypatch.setattr(mcs_update, "HOME", str(tmp_path))
    monkeypatch.setattr(update_apply, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_cli, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_execution, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_lifecycle_context, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_preflight, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_recovery, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_runtime_migration, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(provider_settings, "probe", lambda root: {"state": state})
    monkeypatch.setattr(mcs_update, "_split_target_source_matches", lambda target, **kwargs: pytest.fail("legacy target check"))
    monkeypatch.setattr(update_preflight, '_split_target_source_matches', mcs_update._split_target_source_matches)
    monkeypatch.setattr(update_runtime_migration, '_split_target_source_matches', mcs_update._split_target_source_matches)
    assert mcs_update._provider_rollback_preflight("synthetic-legacy-target") is None


def test_rollback_provider_hold_precedes_quiesce_and_tree_change(updater, tmp_path, monkeypatch):  # noqa: F811
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    before, after = _git(repo, "rev-parse", "v1.0.0").stdout.strip(), _head(repo)
    state = updater.journal_store.default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": after, "prev_sha": before, "schema_bump": False}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    def deny(_target):
        raise updater.UpdateError("provider_rollback_target_unverified")
    monkeypatch.setattr(updater, "_provider_rollback_preflight", deny)
    monkeypatch.setattr(update_apply, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(update_recovery, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(update_restore, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(updater, "quiesce", lambda: pytest.fail("no quiesce"))
    monkeypatch.setattr(update_apply, 'quiesce', updater.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', updater.quiesce)
    monkeypatch.setattr(updater, "restart_agents", lambda *a, **k: pytest.fail("no restart"))
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(updater, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', updater._enqueue_notice)
    assert updater.rollback("synthetic-rollback") == 1
    assert _head(repo) == after
    current = updater.journal_store.load_state(updater.STATE_PATH)
    assert current["applying"] is None and current["applied"] == state["applied"]


def test_recovery_provider_hold_never_resets_or_restarts(updater, tmp_path, monkeypatch):  # noqa: F811
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    head = _head(repo)
    state = updater.journal_store.default_state()
    state["applying"] = {"sha": head, "prev_sha": "a" * 40, "tag": "synthetic", "rollback": True}
    state["stages"] = [{"stage": "merge"}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    def deny(_target):
        raise updater.UpdateError("provider_rollback_target_unverified")
    monkeypatch.setattr(updater, "_provider_rollback_preflight", deny)
    monkeypatch.setattr(update_apply, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(update_recovery, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(update_restore, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(updater, "restart_agents", lambda *a, **k: pytest.fail("no restart"))
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(updater, "_git_out", lambda *a, **k: pytest.fail("no reset"))
    monkeypatch.setattr(update_apply, '_git_out', updater._git_out)
    monkeypatch.setattr(update_planning, '_git_out', updater._git_out)
    monkeypatch.setattr(update_preflight, '_git_out', updater._git_out)
    monkeypatch.setattr(update_recovery, '_git_out', updater._git_out)
    monkeypatch.setattr(update_repository, '_git_out', updater._git_out)
    monkeypatch.setattr(update_restore, '_git_out', updater._git_out)
    assert updater.recover_interrupted() == 1
    assert _head(repo) == head
    saved = updater.journal_store.load_state(updater.STATE_PATH)
    assert saved["applying"] == state["applying"] and saved["stages"] == state["stages"]


def test_provider_preflight_allows_only_matching_target_and_stable_snapshot(updater, tmp_path, monkeypatch):  # noqa: F811
    import provider_settings
    repo, compatible = _source_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    monkeypatch.setattr(provider_settings, "probe", lambda root: {"state": "requires_checked_read"})
    reads = []
    def read(*args, **kwargs):
        reads.append(True)
        return {"mode": "split", "snapshot": {"synthetic": 1}}
    monkeypatch.setattr(provider_settings, "read", read)
    assert updater._provider_rollback_preflight(compatible) is True
    assert reads == [True, True]
    (repo / "mcs/ingest/consumer.py").write_text("# synthetic changed consumer\n")
    with pytest.raises(updater.UpdateError, match="target_unverified"):
        updater._provider_rollback_preflight(compatible)


def test_rollback_pending_after_quiesce_keeps_marker_and_journal(updater, tmp_path, monkeypatch):  # noqa: F811
    import hashlib
    import json
    import provider_settings

    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    before, after = _git(repo, "rev-parse", "v1.0.0").stdout.strip(), _head(repo)
    state = updater.journal_store.default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": after, "prev_sha": before, "schema_bump": False}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    root = tmp_path / "runtime"
    root.chmod(0o700)
    data = root / "data"
    data.mkdir(mode=0o700)
    directory = data / "provider_settings"
    directory.mkdir(mode=0o700)

    def private_json(path, value):
        raw = (json.dumps(value, sort_keys=True) + "\n").encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        return hashlib.sha256(raw).hexdigest()

    common = {"cloud_llm": {"mode": "off", "project_ids": []}, "semantic": {"mode": "off"}}
    private_json(root / "config.json", common)
    hashes = {name: private_json(directory / (name + ".json"), {}) for name in provider_settings.PROVIDERS}
    private_json(data / ".provider-settings-authority.json", {
        "version": 2, "state": "committed", "files": hashes,
        "common_policy_sha256": provider_settings.common_policy_digest(common)})
    pending = data / ".provider-settings-pending.json"
    marker = tmp_path / "data" / "marker"

    def quiesce_then_pending():
        marker.write_text("synthetic hold")
        private_json(pending, {"synthetic": True})

    monkeypatch.setattr(updater, "_split_target_source_matches", lambda target, **kwargs: True)
    monkeypatch.setattr(update_preflight, '_split_target_source_matches', updater._split_target_source_matches)
    monkeypatch.setattr(update_runtime_migration, '_split_target_source_matches', updater._split_target_source_matches)
    monkeypatch.setattr(updater, "_check_runtime_restore", lambda entry: None)
    monkeypatch.setattr(update_apply, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(update_recovery, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(update_runtime_migration, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(updater, "_local_changes_outside", lambda *a: [])
    monkeypatch.setattr(update_apply, '_local_changes_outside', updater._local_changes_outside)
    monkeypatch.setattr(update_recovery, '_local_changes_outside', updater._local_changes_outside)
    monkeypatch.setattr(update_restore, '_local_changes_outside', updater._local_changes_outside)
    monkeypatch.setattr(updater, "quiesce", quiesce_then_pending)
    monkeypatch.setattr(update_apply, 'quiesce', updater.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', updater.quiesce)
    monkeypatch.setattr(updater, "restart_agents", lambda *a, **k: pytest.fail("no restart"))
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(updater, "restart_services", lambda *a, **k: pytest.fail("no service restart"))
    monkeypatch.setattr(update_apply, 'restart_services', updater.restart_services)
    monkeypatch.setattr(update_recovery, 'restart_services', updater.restart_services)
    monkeypatch.setattr(update_services, 'restart_services', updater.restart_services)
    monkeypatch.setattr(updater, "_restore_runtime", lambda *a, **k: pytest.fail("no runtime change"))
    monkeypatch.setattr(update_recovery, '_restore_runtime', updater._restore_runtime)
    monkeypatch.setattr(update_restore, '_restore_runtime', updater._restore_runtime)
    monkeypatch.setattr(updater, "_enqueue_notice", lambda *a, **k: pytest.fail("no enqueue"))
    monkeypatch.setattr(update_apply, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', updater._enqueue_notice)
    assert updater.rollback("synthetic-pending") == 2
    assert marker.read_text() == "synthetic hold" and pending.is_file()
    assert _head(repo) == after
    saved = updater.journal_store.load_state(updater.STATE_PATH)
    assert saved["applying"]["rollback"] is True
    assert saved["applying"]["sha"] == before
    assert saved["applied"] == state["applied"]
    assert "synthetic-pending" not in saved.get("executed", {})


def test_migration_undo_pending_after_quiesce_keeps_hold(updater, tmp_path, monkeypatch):  # noqa: F811
    import provider_settings

    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    before, after = _git(repo, "rev-parse", "v1.0.0").stdout.strip(), _head(repo)
    applying = {"tag": "synthetic", "sha": before, "prev_sha": after,
                "rollback": True, "schema_bump": False}
    state = updater.journal_store.default_state()
    state["applying"] = applying
    updater.journal_store.save_state(updater.STATE_PATH, state)
    marker = tmp_path / "data" / "marker"
    monkeypatch.setattr(provider_settings, "probe", lambda root: {"state": "pending"})
    monkeypatch.setattr(updater, "quiesce", lambda: marker.write_text("synthetic hold"))
    monkeypatch.setattr(update_apply, 'quiesce', updater.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', updater.quiesce)
    monkeypatch.setattr(updater, "_restore_runtime", lambda *a, **k: pytest.fail("no runtime change"))
    monkeypatch.setattr(update_recovery, '_restore_runtime', updater._restore_runtime)
    monkeypatch.setattr(update_restore, '_restore_runtime', updater._restore_runtime)
    def forbidden(*args, **kwargs):
        pytest.fail("no restart or escalation")
    assert updater._undo_interrupted_migration(state, applying, forbidden, forbidden) == 1
    assert marker.read_text() == "synthetic hold" and _head(repo) == after
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"] == applying


def test_split_target_rejects_sourceless_bytecode_without_rejecting_pycache(
        updater, tmp_path, monkeypatch):  # noqa: F811
    repo, compatible = _source_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    cache = repo / "mcs" / "__pycache__"
    cache.mkdir()
    (cache / "synthetic.cpython-314.pyc").write_bytes(b"synthetic")
    assert updater._split_target_source_matches(compatible)
    shadow = repo / "mcs" / "semantic" / "synthetic.pyc"
    shadow.write_bytes(b"synthetic")
    assert not updater._split_target_source_matches(compatible)


def test_recovery_empty_journal_pending_keeps_marker(updater, tmp_path, monkeypatch):  # noqa: F811
    import provider_settings
    state = updater.journal_store.default_state()
    updater.journal_store.save_state(updater.STATE_PATH, state)
    marker = tmp_path / "data" / "marker"
    marker.write_text("synthetic hold")
    monkeypatch.setattr(provider_settings, "probe", lambda root: {"state": "pending"})
    monkeypatch.setattr(updater, "_remove_marker", lambda: pytest.fail("keep marker"))
    monkeypatch.setattr(update_apply, '_remove_marker', updater._remove_marker)
    monkeypatch.setattr(update_recovery, '_remove_marker', updater._remove_marker)
    monkeypatch.setattr(update_services, '_remove_marker', updater._remove_marker)
    monkeypatch.setattr(updater, "restart_agents", lambda *a, **k: pytest.fail("no restart"))
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
    assert updater.recover_interrupted() == 1
    assert marker.read_text() == "synthetic hold"
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"] is None


def test_recovery_stage_only_uses_current_split_target(updater, tmp_path, monkeypatch):  # noqa: F811
    import provider_settings
    repo, _ = _source_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    state = updater.journal_store.default_state()
    state["stages"] = [{"stage": "fetch"}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    monkeypatch.setattr(provider_settings, "probe", lambda root: {"state": "requires_checked_read"})
    monkeypatch.setattr(provider_settings, "read", lambda *a, **k: {"mode": "split", "snapshot": {"v": 1}})
    monkeypatch.setattr(updater, "_migrate_log_tail", lambda: None)
    monkeypatch.setattr(update_execution, '_migrate_log_tail', updater._migrate_log_tail)
    monkeypatch.setattr(update_recovery, '_migrate_log_tail', updater._migrate_log_tail)
    monkeypatch.setattr(updater, "_check_runtime_restore", lambda entry: None)
    monkeypatch.setattr(update_apply, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(update_recovery, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(update_runtime_migration, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(updater, "_clean_stale_git_locks", lambda: [])
    monkeypatch.setattr(update_recovery, '_clean_stale_git_locks', updater._clean_stale_git_locks)
    finished = []
    monkeypatch.setattr(updater, "_finish_recovery",
                        lambda state, result, removed: finished.append(result) or 0)
    monkeypatch.setattr(update_recovery, '_finish_recovery', updater._finish_recovery)
    assert updater.recover_interrupted() == 0
    assert finished == ["interrupted_pre_merge"]


def test_split_forward_recovery_uses_target_without_requiring_previous_source(updater, tmp_path, monkeypatch):  # noqa: F811
    import hashlib
    import json
    import provider_settings

    repo, _ = _source_repo(tmp_path)
    previous = _head(repo)
    (repo / "mcs/ingest/consumer.py").write_text("# synthetic updated split consumer\n")
    _git(repo, "add", "mcs/ingest/consumer.py")
    _git(repo, "commit", "-qm", "synthetic updated source")
    target = _head(repo)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    assert not updater._split_target_source_matches(previous)
    assert updater._split_target_source_matches(target)

    root = tmp_path / "runtime"
    root.chmod(0o700)
    data = root / "data"
    data.mkdir(mode=0o700)
    directory = data / "provider_settings"
    directory.mkdir(mode=0o700)

    def private_json(path, value):
        raw = (json.dumps(value, sort_keys=True) + "\n").encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        return hashlib.sha256(raw).hexdigest()

    common = {"cloud_llm": {"mode": "off", "project_ids": []}, "semantic": {"mode": "off"}}
    private_json(root / "config.json", common)
    hashes = {name: private_json(directory / (name + ".json"), {}) for name in provider_settings.PROVIDERS}
    authority = data / ".provider-settings-authority.json"
    private_json(authority, {"version": 2, "state": "committed", "files": hashes,
                             "common_policy_sha256": provider_settings.common_policy_digest(common)})
    authority_before = authority.read_bytes()
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "synthetic", "sha": target, "prev_sha": previous, "rollback": False}
    state["stages"] = [{"stage": "post_merge"}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    monkeypatch.setattr(updater, "_migrate_log_tail", lambda: None)
    monkeypatch.setattr(update_execution, '_migrate_log_tail', updater._migrate_log_tail)
    monkeypatch.setattr(update_recovery, '_migrate_log_tail', updater._migrate_log_tail)
    monkeypatch.setattr(updater, "_check_runtime_restore", lambda entry: None)
    monkeypatch.setattr(update_apply, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(update_recovery, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(update_runtime_migration, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(updater, "_clean_stale_git_locks", lambda: [])
    monkeypatch.setattr(update_recovery, '_clean_stale_git_locks', updater._clean_stale_git_locks)
    monkeypatch.setattr(updater, "_services_reconcile", lambda: None)
    monkeypatch.setattr(update_recovery, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(update_restore, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(update_runtime_migration, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(updater, "restart_agents", lambda *a, **k: [])
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(updater, "restart_services", lambda *a, **k: None)
    monkeypatch.setattr(update_apply, 'restart_services', updater.restart_services)
    monkeypatch.setattr(update_recovery, 'restart_services', updater.restart_services)
    monkeypatch.setattr(update_services, 'restart_services', updater.restart_services)
    monkeypatch.setattr(updater, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(mcs_util, "load_config", lambda: common)
    postchecks = []
    monkeypatch.setattr(updater, "_postcheck", lambda state, sha: postchecks.append(sha) or [])
    monkeypatch.setattr(update_recovery, '_postcheck', updater._postcheck)
    monkeypatch.setattr(update_runtime_migration, '_postcheck', updater._postcheck)
    assert updater.recover_interrupted() == 0
    assert postchecks == [target] and _head(repo) == target
    saved = updater.journal_store.load_state(updater.STATE_PATH)
    assert saved["applying"] is None and saved["stages"] == []
    assert saved["applied"][-1]["sha"] == target
    assert authority.read_bytes() == authority_before


@pytest.mark.parametrize("name", [
    "mcs_standalone/config.py", "adapters/slack/config.py", "hermes_plugin/entry.py",
    "lineworks_adapter/__main__.py", "deployment/recovery/mcs_recover.py",
    "scripts/mcs_upgrade.py", "install.sh",
])
def test_split_target_rejects_changed_runtime_consumer(updater, tmp_path, monkeypatch, name):  # noqa: F811
    repo, compatible = _source_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    path = repo / name
    path.write_text("# synthetic changed runtime consumer\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", "synthetic runtime change")
    assert not updater._split_target_source_matches(compatible)


def test_rollback_error_with_new_pending_keeps_hold(updater, tmp_path, monkeypatch):  # noqa: F811
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    before, after = _git(repo, "rev-parse", "v1.0.0").stdout.strip(), _head(repo)
    state = updater.journal_store.default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": after, "prev_sha": before, "schema_bump": False}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    marker = tmp_path / "data" / "marker"
    pending = {"value": False}

    def preflight(target):
        if pending["value"]:
            raise updater.ProviderRollbackUnverified("provider_rollback_settings_unverified")

    def failed_tree(*args, **kwargs):
        pending["value"] = True
        raise RuntimeError("synthetic interrupted load")

    monkeypatch.setattr(updater, "_provider_rollback_preflight", preflight)
    monkeypatch.setattr(update_apply, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(update_recovery, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(update_restore, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(updater, "_check_runtime_restore", lambda entry: None)
    monkeypatch.setattr(update_apply, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(update_recovery, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(update_runtime_migration, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(updater, "_local_changes_outside", lambda *a: [])
    monkeypatch.setattr(update_apply, '_local_changes_outside', updater._local_changes_outside)
    monkeypatch.setattr(update_recovery, '_local_changes_outside', updater._local_changes_outside)
    monkeypatch.setattr(update_restore, '_local_changes_outside', updater._local_changes_outside)
    monkeypatch.setattr(updater, "quiesce", lambda: marker.write_text("synthetic hold"))
    monkeypatch.setattr(update_apply, 'quiesce', updater.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', updater.quiesce)
    monkeypatch.setattr(updater, "_rollback_tree", failed_tree)
    monkeypatch.setattr(update_apply, '_rollback_tree', updater._rollback_tree)
    monkeypatch.setattr(update_recovery, '_rollback_tree', updater._rollback_tree)
    monkeypatch.setattr(updater, "restart_agents", lambda *a, **k: pytest.fail("no restart"))
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(updater, "_enqueue_notice", lambda *a, **k: pytest.fail("no enqueue"))
    monkeypatch.setattr(update_apply, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', updater._enqueue_notice)
    assert updater.rollback("synthetic-pending-error") == 2
    assert marker.read_text() == "synthetic hold" and _head(repo) == after
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]["rollback"] is True


def test_recovery_finish_rechecks_pending_before_marker_and_restart(updater, tmp_path, monkeypatch):  # noqa: F811
    import provider_settings
    marker = tmp_path / "data" / "marker"
    marker.write_text("synthetic hold")
    monkeypatch.setattr(provider_settings, "probe", lambda root: {"state": "pending"})
    monkeypatch.setattr(updater, "_remove_marker", lambda: pytest.fail("keep marker"))
    monkeypatch.setattr(update_apply, '_remove_marker', updater._remove_marker)
    monkeypatch.setattr(update_recovery, '_remove_marker', updater._remove_marker)
    monkeypatch.setattr(update_services, '_remove_marker', updater._remove_marker)
    monkeypatch.setattr(updater, "restart_agents", lambda *a, **k: pytest.fail("no restart"))
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
    assert updater._finish_recovery(updater.journal_store.default_state(), "synthetic", []) == 1
    assert marker.read_text() == "synthetic hold"
