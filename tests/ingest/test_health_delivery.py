"""Delivery observations use synthetic state and never change it."""
import json
import os
import sqlite3
import subprocess
import time
from types import SimpleNamespace

import pytest

import card_worker_status
import environment_diagnostics
import health_delivery as delivery
import health_recovery
import health_watch
import notify_parts


NOW = 1_800_000_000.0
STARTED = NOW - 6000
PID = 424242
SCOPE = {"profile": "synthetic-profile", "application_id": "A_SYNTHETIC",
         "team_id": "T_SYNTHETIC", "channel_id": "C_SYNTHETIC"}
CFG = {"runtime_mode": "hermes", "notify": {"interactive": "slack", "route_epoch": 2,
                                           "slack": SCOPE}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    path = data / "ledger.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE notification_renders(
          delivery_id TEXT PRIMARY KEY, card_id INTEGER, transport TEXT, route_epoch INTEGER,
          profile TEXT, application_id TEXT, team_id TEXT, guild_id TEXT, channel_id TEXT,
          state TEXT, created_at REAL, updated_at REAL);
        CREATE TABLE notification_render_parts(delivery_id TEXT, state TEXT, updated_at REAL);
        CREATE TABLE notification_delivery_attempts(
          delivery_id TEXT, state TEXT, created_at REAL, finished_at REAL);
    """)
    db.close()
    path.chmod(0o600)
    monkeypatch.setattr(delivery, "_worker", lambda *args: {
        "state": "started", "reason": "worker_started", "started_at": STARTED})
    return SimpleNamespace(home=tmp_path, data=data, db=path)


def _render(path, identifier, *, state="queued", created=NOW - 3600, updated=NOW - 3600,
            delivered_at=..., transport="slack", epoch=2, **changes):
    scope = {**SCOPE, **changes}
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO notification_renders(delivery_id,card_id,transport,route_epoch,"
            "profile,application_id,team_id,guild_id,channel_id,state,created_at,updated_at) "
            "VALUES(?,NULL,?,?,?,?,?,?,?,?,?,?)", (
                identifier, transport, epoch, scope.get("profile"), scope.get("application_id"),
                scope.get("team_id"), scope.get("guild_id"), scope.get("channel_id"),
                state, created, updated))
        if state == "delivered" and delivered_at is not False:
            stamp = updated if delivered_at is ... else delivered_at
            db.execute("INSERT INTO notification_delivery_attempts VALUES(?,?,?,?)",
                       (identifier, "delivered", created, stamp))


def _attempt(path, identifier, *, state="granted", created=NOW - 3601, finished=None):
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO notification_delivery_attempts VALUES(?,?,?,?)",
                   (identifier, state, created, finished))


def _observe(world, cfg=CFG):
    return delivery._observe(world.home, cfg, NOW, time.monotonic() + 20)


@pytest.mark.parametrize("change", [
    {"profile": "other"}, {"application_id": "other"}, {"team_id": "other"},
    {"channel_id": "other"}, {"epoch": 1}, {"transport": "discord"},
])
def test_other_scope_progress_never_hides_own_queued_render(world, change):
    _render(world.db, "queued")
    _render(world.db, "other", state="delivered", updated=NOW - 10, **change)
    result = _observe(world)["delivery"]
    assert result == {"state": "waiting_without_progress", "issues": ["waiting_without_progress"],
                      "queued": 1, "sending": 0,
                      "unknown": 0, "oldest_created_at": NOW - 3600, "last_progress_at": None}
    with sqlite3.connect(world.db) as db:
        db.execute("INSERT INTO notification_render_parts VALUES('queued','delivered',?)", (NOW - 5,))
    result = _observe(world)["delivery"]
    assert result["state"] == "progressing" and result["last_progress_at"] == NOW - 5


def test_current_scope_whole_render_progress_and_empty_queue(world):
    _render(world.db, "queued")
    _render(world.db, "delivered", state="delivered", updated=NOW - 1)
    assert _observe(world)["delivery"]["state"] == "progressing"
    with sqlite3.connect(world.db) as db:
        db.execute("UPDATE notification_renders SET state='delivered' WHERE delivery_id='queued'")
    assert _observe(world)["delivery"]["state"] == "idle"


def test_discord_preserves_the_existing_empty_scope_compatibility(world):
    cfg = {"notify": {"interactive": "discord", "route_epoch": 2, "discord": {
        "profile": "p", "application_id": "app", "guild_id": "g", "channel_id": "c"}}}
    _render(world.db, "discord", transport="discord", profile=None, application_id=None,
            guild_id=None, channel_id=None)
    observed = _observe(world, cfg)["delivery"]
    assert observed["queued"] == 1 and observed["state"] == "waiting_without_progress"


@pytest.mark.parametrize("state", ["sending", "unknown"])
def test_unsettled_attempt_uses_its_start_and_never_changes_the_database(world, state):
    _render(world.db, "attempt", state=state)
    first = _observe(world)["delivery"]
    assert first["state"] == "unknown" and first[state] == 1
    _attempt(world.db, "attempt", created=NOW - delivery.CARD_STALL_S)
    assert _observe(world)["delivery"]["state"] == ("pending" if state == "sending" else "unknown")
    with sqlite3.connect(world.db) as db:
        db.execute("UPDATE notification_delivery_attempts SET created_at=?", (NOW - delivery.CARD_STALL_S - 1,))
    result = _observe(world)["delivery"]
    assert result["state"] == "outcome_unconfirmed" and result[state] == 1
    with sqlite3.connect(world.db) as db:
        assert db.execute("SELECT state FROM notification_renders").fetchone()[0] == state
        assert db.execute("SELECT state FROM notification_delivery_attempts").fetchone()[0] == "granted"


@pytest.mark.parametrize("state", ["sending", "unknown"])
def test_old_queue_and_unsettled_attempt_are_independent_issues(world, state):
    _render(world.db, "queued")
    _render(world.db, "attempt", state=state)
    _attempt(world.db, "attempt")
    result = _observe(world)["delivery"]
    assert result["state"] == "waiting_without_progress"
    assert result["issues"] == ["waiting_without_progress", "outcome_unconfirmed"]
    with sqlite3.connect(world.db) as db:
        db.execute("INSERT INTO notification_render_parts VALUES('queued','delivered',?)", (NOW - 1,))
    result = _observe(world)["delivery"]
    assert result["state"] == "outcome_unconfirmed"
    assert result["issues"] == ["outcome_unconfirmed"]
    with sqlite3.connect(world.db) as db:
        db.execute("DELETE FROM notification_render_parts")
        db.execute("UPDATE notification_delivery_attempts SET created_at=?", (NOW + 1,))
    result = _observe(world)["delivery"]
    assert result["state"] == "waiting_without_progress"
    assert result["issues"] == ["waiting_without_progress"]


def test_fresh_queued_and_invalid_time_do_not_claim_progress_or_stall(world):
    _render(world.db, "queued", created=NOW - 2)
    assert _observe(world)["delivery"]["state"] == "pending"
    with sqlite3.connect(world.db) as db:
        db.execute("UPDATE notification_renders SET created_at=?", (NOW + 1,))
    result = _observe(world)["delivery"]
    assert result["state"] == "unknown" and result["queued"] is None


def test_pending_requires_valid_unsettled_attempts(world):
    _render(world.db, "queued", created=NOW - 2)
    _render(world.db, "sending", state="sending")
    assert _observe(world)["delivery"]["state"] == "unknown"
    _attempt(world.db, "sending", created=NOW - 1)
    assert _observe(world)["delivery"]["state"] == "pending"
    with sqlite3.connect(world.db) as db:
        db.execute("UPDATE notification_delivery_attempts SET created_at=?", (NOW + 1,))
    assert _observe(world)["delivery"]["state"] == "unknown"


def test_future_progress_does_not_override_real_recent_progress(world):
    _render(world.db, "queued")
    _render(world.db, "future", state="delivered", updated=NOW + 1)
    assert _observe(world)["delivery"]["state"] == "unknown"
    _render(world.db, "recent", state="delivered", updated=NOW - 5)
    result = _observe(world)["delivery"]
    assert result["state"] == "progressing" and result["last_progress_at"] == NOW - 5


@pytest.mark.parametrize("kind", ["empty", "fresh", "sending", "old"])
def test_progress_queries_only_for_old_queued(world, monkeypatch, kind):
    if kind == "fresh":
        _render(world.db, "queued", created=NOW - 1)
    elif kind == "sending":
        _render(world.db, "sending", state="sending")
        _attempt(world.db, "sending")
    elif kind == "old":
        _render(world.db, "queued")
    calls = []
    progress = delivery._progress

    def track(db, source, where, params, now):
        calls.append(source)
        return progress(db, source, where, params, now)

    monkeypatch.setattr(delivery, "_progress", track)
    result = _observe(world)["delivery"]
    assert calls == (["renders", "parts"] if kind == "old" else [])
    if kind != "old":
        assert result["last_progress_at"] is None


def test_requeued_render_keeps_creation_time_without_claiming_wait_start(world):
    _render(world.db, "requeued", state="delivered", created=NOW - 7200, updated=NOW - 7200)
    with sqlite3.connect(world.db) as db:
        db.execute("UPDATE notification_renders SET state='queued' WHERE delivery_id='requeued'")
    observed = _observe(world)["delivery"]
    assert observed["state"] == "waiting_without_progress"
    assert observed["oldest_created_at"] == NOW - 7200
    assert "wait_started_at" not in observed


def test_database_read_is_mode_ro_and_sees_committed_wal(world, monkeypatch):
    db = sqlite3.connect(world.db)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("INSERT INTO notification_renders VALUES('wal',NULL,'slack',2,?,?,?,?,?,'queued',?,?)",
               (SCOPE["profile"], SCOPE["application_id"], SCOPE["team_id"], None,
                SCOPE["channel_id"], NOW - 3600, NOW - 3600))
    db.commit()
    connect = delivery.sqlite3.connect
    opened = []

    def observe_connect(database, **kwargs):
        opened.append((database, kwargs))
        return connect(database, **kwargs)

    monkeypatch.setattr(delivery.sqlite3, "connect", observe_connect)
    try:
        assert _observe(world)["delivery"]["state"] == "waiting_without_progress"
        assert opened[0][0].endswith("?mode=ro") and "immutable" not in opened[0][0]
        assert opened[0][1]["uri"] is True
    finally:
        db.close()


def test_sqlite_busy_preserves_worker_and_restart_observation(world, monkeypatch):
    path = world.data / "gateway_restart.json"
    path.write_text(json.dumps({"status": "unknown", "at": NOW - 1,
                                "error": "gateway_restart_busy_timeout"}))
    path.chmod(0o600)

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic locked")

    monkeypatch.setattr(delivery.sqlite3, "connect", busy)
    result = _observe(world)
    assert result["delivery"]["state"] == "unknown" and result["delivery"]["queued"] is None
    assert result["worker"]["state"] == "started"
    assert result["restart_request"] == {"state": "unknown", "at": NOW - 1}


def test_expired_deadline_never_starts_sql_connection(world, monkeypatch):
    monkeypatch.setattr(delivery.sqlite3, "connect", lambda *args, **kwargs: pytest.fail("SQL started"))
    result = delivery._observe(world.home, CFG, NOW, time.monotonic() - 1)
    assert result["delivery"]["state"] == "unknown" and result["delivery"]["queued"] is None


@pytest.mark.parametrize("damage", ["missing", "public", "symlink", "update", "restore", "schema"])
def test_unverified_database_is_unknown(world, damage):
    if damage == "missing":
        world.db.unlink()
    elif damage == "public":
        world.db.chmod(0o644)
    elif damage == "symlink":
        saved = world.data / "other.db"
        world.db.rename(saved)
        world.db.symlink_to(saved)
    elif damage in ("update", "restore"):
        (world.data / ("update_in_progress.marker" if damage == "update" else "restore_pending.json")).touch()
    else:
        with sqlite3.connect(world.db) as db:
            db.execute("DROP TABLE notification_delivery_attempts")
    result = _observe(world)
    assert result["delivery"]["state"] == "unknown"
    assert result["delivery"]["queued"] is None
    assert result["worker"]["state"] == "started"


@pytest.mark.parametrize(("record", "expected"), [
    ({"status": "requested", "at": NOW - 1}, "requested"),
    ({"status": "unknown", "at": NOW - 1, "error": "gateway_restart_busy_timeout"}, "unknown"),
    ({"status": "failed", "at": NOW - 1, "error": "gateway_restart_spawn_failed"}, "failed"),
    ({"status": "requested", "at": NOW + 1}, "unverified"),
    ({"status": "unknown", "at": NOW - 1, "error": "private text"}, "unverified"),
    ({"status": "requested", "at": NOW - 1, "extra": "private text"}, "unverified"),
])
def test_restart_receipt_is_a_separate_unchanged_observation(world, record, expected):
    path = world.data / "gateway_restart.json"
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    before = path.read_bytes()
    result = _observe(world)
    assert result["restart_request"]["state"] == expected
    assert result["worker"]["state"] == "started" and path.read_bytes() == before
    assert "private text" not in json.dumps(result)


def test_verified_old_restart_is_not_current_worker_or_delivery_proof(world):
    path = world.data / "gateway_restart.json"
    record = {"status": "supervisor_restart_verified", "at": NOW - 100,
              "service": f"gui/{os.getuid()}/ai.hermes.gateway", "previous_pid": 2, "pid": 3}
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    result = _observe(world)
    assert result["restart_request"] == {"state": "supervisor_restart_verified", "at": NOW - 100}
    assert result["worker"]["started_at"] == STARTED
    assert result["delivery"]["state"] == "idle"


@pytest.mark.parametrize("damage", ["symlink", "public", "duplicate"])
def test_untrusted_restart_receipt_is_not_reported(world, damage):
    path = world.data / "gateway_restart.json"
    if damage == "symlink":
        path.symlink_to(world.data / "elsewhere")
    else:
        path.write_text('{"status":"requested","status":"failed","at":1}' if damage == "duplicate"
                        else json.dumps({"status": "requested", "at": NOW - 1}))
        path.chmod(0o644 if damage == "public" else 0o600)
    assert _observe(world)["restart_request"] == {"state": "unverified", "at": None}


def test_worker_witness_is_bound_to_live_pid_birth_and_unchanged_bytes(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    path = card_worker_status._path(data, "slack", SCOPE["profile"])
    path.parent.mkdir(mode=0o700)
    record = {"version": card_worker_status.VERSION, "reason": "worker_started",
              "pid": PID, "at": NOW - 10, "started_at": STARTED}
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    monkeypatch.setattr(health_recovery.sys, "platform", "darwin")
    monkeypatch.setattr(environment_diagnostics, "launchd_pid", lambda *a, **kw: PID)
    monkeypatch.setattr(environment_diagnostics, "process_started_at", lambda *a, **kw: STARTED)
    monkeypatch.setattr(card_worker_status.os, "kill", lambda *a: None)
    assert delivery._worker(tmp_path, CFG, NOW)["state"] == "started"
    monkeypatch.setattr(environment_diagnostics, "process_started_at", lambda *a, **kw: STARTED + 1)
    assert delivery._worker(tmp_path, CFG, NOW)["state"] == "unknown"
    monkeypatch.setattr(environment_diagnostics, "process_started_at", lambda *a, **kw: STARTED)
    read = health_recovery._read
    calls = []

    def replace_after_first_read(witness, limit):
        value = read(witness, limit)
        calls.append(1)
        if len(calls) == 1:
            path.write_text(json.dumps({**record, "at": NOW - 9}))
        return value

    monkeypatch.setattr(health_recovery, "_read", replace_after_first_read)
    assert delivery._worker(tmp_path, CFG, NOW)["state"] == "unknown"


def test_observe_sends_only_minimal_settings_to_bounded_child(world, monkeypatch):
    result = delivery._empty(NOW)
    result["worker"] = {"state": "started", "reason": "worker_started", "started_at": STARTED}
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        assert argv[1:3] == ["-I", "-B"]
        assert kwargs["timeout"] <= 3 and kwargs["cwd"] == "/"
        assert "private-canary" not in repr(argv) + repr(kwargs["env"])
        payload = json.loads(kwargs["input"])
        assert payload["cfg"] == CFG
        assert payload["deadline"] < time.monotonic() + kwargs["timeout"] - 0.4
        return subprocess.CompletedProcess(argv, 0, json.dumps(result))

    monkeypatch.setattr(delivery.subprocess, "run", run)
    cfg = {**CFG, "private": "private-canary"}
    assert delivery.observe(world.home, cfg, NOW) == result and len(seen) == 1


def test_real_child_observes_synthetic_queue_and_private_receipt(world, monkeypatch):
    _render(world.db, "queued")
    _render(world.db, "sending", state="sending")
    _attempt(world.db, "sending")
    receipt = world.data / "gateway_restart.json"
    receipt.write_text(json.dumps({"status": "requested", "at": NOW - 1}))
    receipt.chmod(0o600)
    empty_bin = world.home / "bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    for key in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(key, str(world.home))

    result = delivery.observe(world.home, CFG, NOW)
    assert result["worker"]["state"] == "unknown"
    assert result["restart_request"] == {"state": "requested", "at": NOW - 1}
    assert result["delivery"] == {"state": "waiting_without_progress",
                                  "issues": ["waiting_without_progress", "outcome_unconfirmed"],
                                  "queued": 1, "sending": 1, "unknown": 0,
                                  "oldest_created_at": NOW - 3600, "last_progress_at": None}
    assert delivery._valid_result(result, NOW)
    assert health_watch._delivery_snapshot(result, NOW) == result
    assert SCOPE["profile"] not in json.dumps(result)
    assert json.loads(receipt.read_text()) == {"status": "requested", "at": NOW - 1}


@pytest.mark.parametrize("issues", [None, ["outcome_unconfirmed", "waiting_without_progress"],
                                    ["waiting_without_progress", "waiting_without_progress"],
                                    ["unexpected"]])
def test_child_result_rejects_invalid_issue_lists(issues):
    result = delivery._empty(NOW)
    result["delivery"].update(state="waiting_without_progress", issues=issues, queued=1,
                              sending=1, unknown=0, oldest_created_at=NOW - 3600)
    assert not delivery._valid_result(result, NOW)


def test_child_result_requires_issue_and_primary_state_to_agree():
    result = delivery._empty(NOW)
    result["delivery"].update(state="waiting_without_progress",
                              issues=["waiting_without_progress", "outcome_unconfirmed"],
                              queued=1, sending=1, unknown=0, oldest_created_at=NOW - 3600)
    assert delivery._valid_result(result, NOW)
    result["delivery"]["state"] = "outcome_unconfirmed"
    assert not delivery._valid_result(result, NOW)


def test_worker_reserves_status_time_and_forwards_witness_deadline(tmp_path, monkeypatch):
    seen = []

    def observation(home, cfg, now, *, deadline):
        seen.append(deadline)
        return {"healthy": True, "reasons": [], "started_at": STARTED}

    monkeypatch.setattr(health_recovery, "_gateway_observation", observation)
    deadline = time.monotonic() + 2
    assert delivery._worker(tmp_path, CFG, NOW, deadline)["state"] == "started"
    assert seen == [deadline - delivery._STATUS_S]
    assert delivery._worker(tmp_path, CFG, NOW, time.monotonic() + 0.5)["state"] == "unknown"
    assert len(seen) == 1


def test_observation_child_does_not_create_bytecode(tmp_path, monkeypatch):
    module = tmp_path / "synthetic_observer_module.py"
    module.write_text("VALUE = 1\n")
    script = tmp_path / "synthetic_observer.py"
    script.write_text("import sys\nfrom pathlib import Path\n"
                      "sys.path.insert(0, str(Path(__file__).parent))\n"
                      "import synthetic_observer_module\n"
                      "Path(__file__).with_name('imported').write_text('ok')\nprint('{}')\n")
    monkeypatch.setattr(delivery, "__file__", str(script))
    delivery.observe(tmp_path, CFG, NOW)
    assert (tmp_path / "imported").read_text() == "ok"
    assert not (tmp_path / "__pycache__").exists()


def test_timed_out_synthetic_observer_is_reaped(tmp_path, monkeypatch):
    script = tmp_path / "synthetic_observer.py"
    script.write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(delivery, "__file__", str(script))
    monkeypatch.setattr(delivery, "OBSERVATION_S", 0.5)
    popen = subprocess.Popen
    spawned = []

    def capture(*args, **kwargs):
        process = popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(delivery.subprocess, "Popen", capture)
    assert delivery.observe(tmp_path, CFG, NOW) == delivery._empty(NOW)
    assert len(spawned) == 1 and spawned[0].poll() is not None


def test_timeout_and_invalid_child_response_return_unknown(world, monkeypatch):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(delivery.subprocess, "run", timeout)
    assert delivery.observe(world.home, CFG, NOW) == delivery._empty(NOW)
    fake = {**delivery._empty(NOW), "worker": {"state": "started", "reason": "secret", "started_at": STARTED}}
    monkeypatch.setattr(delivery.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, json.dumps(fake)))
    assert delivery.observe(world.home, CFG, NOW) == delivery._empty(NOW)
    fake = delivery._empty(NOW)
    fake["delivery"].update(state="idle", queued=1, sending=0, unknown=0, oldest_created_at=NOW - 1)
    monkeypatch.setattr(delivery.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, json.dumps(fake)))
    assert delivery.observe(world.home, CFG, NOW) == delivery._empty(NOW)


@pytest.mark.parametrize("cfg", [{}, {**CFG, "runtime_mode": "standalone"},
    {"notify": {"interactive": "slack", "slack": {"profile": "partial"}}},
    {"notify": {"interactive": "off"}}])
def test_unverified_scope_does_not_start_observation_child(world, monkeypatch, cfg):
    monkeypatch.setattr(delivery.subprocess, "run", lambda *args, **kwargs: pytest.fail("child started"))
    assert delivery.observe(world.home, cfg, NOW) == delivery._empty(NOW)

def _parts_state_schema(path):
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE notification_renders ADD COLUMN parts_state TEXT")


def test_noop_rollup_is_not_delivery_progress(world):
    """A repeated update_state rollup that changes nothing is bookkeeping:
    it must not bump updated_at into fresh delivery progress."""
    _render(world.db, "queued")
    _render(world.db, "old", state="delivered", created=NOW - 7200, updated=NOW - 7200)
    _parts_state_schema(world.db)
    with sqlite3.connect(world.db) as db:
        db.execute("INSERT INTO notification_render_parts VALUES('old','delivered',?)",
                   (NOW - 7200,))
        db.execute("UPDATE notification_renders SET parts_state='complete' "
                   "WHERE delivery_id='old'")
    assert _observe(world)["delivery"]["state"] == "waiting_without_progress"
    with sqlite3.connect(world.db) as db:
        db.row_factory = sqlite3.Row
        assert notify_parts.update_state(db, "old", NOW - 1) == "complete"
    assert _observe(world)["delivery"]["state"] == "waiting_without_progress"
    with sqlite3.connect(world.db) as db:
        assert db.execute("SELECT updated_at FROM notification_renders "
                          "WHERE delivery_id='old'").fetchone()[0] == NOW - 7200


def test_incomplete_part_settlement_without_new_delivery_is_not_progress(world):
    """Settling a pending part of an old delivered parent as not_sent/unknown/held
    updates parts_state and r.updated_at for state-tracking readers, but zero new
    deliveries succeeded — waiting_without_progress must not clear into progressing."""
    _render(world.db, "queued", created=NOW - 3601, updated=NOW - 3601)
    _render(world.db, "old", state="delivered", created=NOW - 7200,
            updated=NOW - 7200, delivered_at=NOW - 7200)
    _parts_state_schema(world.db)
    with sqlite3.connect(world.db) as db:
        db.execute("UPDATE notification_renders SET parts_state='pending' "
                   "WHERE delivery_id='old'")
        db.execute("INSERT INTO notification_render_parts VALUES('old','delivered',?)",
                   (NOW - 7200,))
        db.execute("INSERT INTO notification_render_parts VALUES('old','pending',?)",
                   (NOW - 7200,))
    before = _observe(world)["delivery"]
    assert before["state"] == "waiting_without_progress"
    assert before["last_progress_at"] == NOW - 7200

    with sqlite3.connect(world.db) as db:
        db.row_factory = sqlite3.Row
        db.execute("UPDATE notification_render_parts SET state='not_sent',updated_at=? "
                   "WHERE delivery_id='old' AND state='pending'", (NOW - 1,))
        assert notify_parts.update_state(db, "old", NOW - 1) == "incomplete"
        row = db.execute("SELECT parts_state,updated_at FROM notification_renders "
                         "WHERE delivery_id='old'").fetchone()
        assert (row["parts_state"], row["updated_at"]) == ("incomplete", NOW - 1)

    after = _observe(world)["delivery"]
    assert after["state"] == "waiting_without_progress"
    assert after["issues"] == ["waiting_without_progress"]
    assert after["last_progress_at"] == NOW - 7200

    # A render row bumped without any delivered attempt or part is not progress either.
    _render(world.db, "unbacked", state="delivered", created=NOW - 7200,
            updated=NOW - 1, delivered_at=False)
    assert _observe(world)["delivery"]["state"] == "waiting_without_progress"


@pytest.mark.parametrize("via", ["part", "parent"])
def test_actual_parent_or_part_delivery_counts_as_progress(world, via):
    """Positive control: an actual successful part send or parent send within
    the stall window proves delivery progress."""
    _render(world.db, "queued", created=NOW - 3601, updated=NOW - 3601)
    _render(world.db, "old", state="delivered", created=NOW - 7200,
            updated=NOW - 7200, delivered_at=NOW - 7200)
    assert _observe(world)["delivery"]["state"] == "waiting_without_progress"
    if via == "part":
        with sqlite3.connect(world.db) as db:
            db.execute("INSERT INTO notification_render_parts VALUES('old','delivered',?)",
                       (NOW - 1,))
    else:
        _render(world.db, "fresh-parent", state="delivered",
                created=NOW - 10, updated=NOW - 1, delivered_at=NOW - 1)
    observed = _observe(world)["delivery"]
    assert observed["state"] == "progressing"
    assert observed["last_progress_at"] == NOW - 1

