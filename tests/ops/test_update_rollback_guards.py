"""Updater rollback/restart guards (synthetic repo, temp DB, stubbed
launchctl and network)."""
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
    before = _git(repo, "rev-parse", "v1.0.0").stdout.strip()
    after = _head(repo)
    state = updater._default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": after, "prev_sha": before,
                         "schema_bump": True,
                         "backup_path": str(tmp_path / "pruned.db")}]
    updater.save_state(state)
    notices = []
    monkeypatch.setattr(updater, "load_config", lambda: {})
    monkeypatch.setattr(updater, "quiesce", lambda: pytest.fail("no quiesce"))
    monkeypatch.setattr(updater, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    assert updater.rollback("rb-1") == 1
    assert _head(repo) == after
    st = updater.load_state()
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
    monkeypatch.setattr(mcs_update.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("no launchctl in standalone"))
    cfg = {"runtime_mode": "standalone", "notify": {"interactive": "lineworks"}}
    mcs_update.restart_services(cfg, False)
    assert requested == [cfg]


def test_release_notes_fetch_refuses_redirects(monkeypatch):
    import mcs_util
    seen = []

    class Opener:
        def open(self, req, timeout):
            raise OSError("synthetic: no network")

    monkeypatch.setattr(mcs_update, "_git_out",
                        lambda argv, **k: "https://github.com/o/r.git")
    monkeypatch.setattr(mcs_util, "no_proxy_opener",
                        lambda *h: seen.append(h) or Opener())
    assert mcs_update.fetch_notes("v1.0.0") is None
    assert seen == [(mcs_util.NoRedirect,)]
