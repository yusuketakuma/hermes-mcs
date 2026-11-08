"""Cooperative restart reservations survive crashes without retrying unknown effects."""
from copy import deepcopy
import json
import os

import pytest

import health_recovery as recovery
import health_watch
import mcs_config
import notify_spool
import safe_io
import update_journal
import writer_lock
from mcs_standalone import service

NOW = 1_000_000.0
GENERATION = "a" * 32
REQUEST = "b" * 32
REAL_REQUEST = service.request_restart
REAL_STATUS = service.status


def _private(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def _report(now=NOW, *, status="degraded", reasons=None, mode="standalone"):
    return {"status": status, "watched_at": now, "runtime_mode": mode,
            "state_reasons": ["standalone_child_failing"] if reasons is None else reasons}


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True, mode=0o700)
    live = {"generation": GENERATION, "pid": 731, "updated_at": NOW, "children": {},
            "restarting": False, "update_in_progress": False,
            "restart_reservation": None, "update_marker": None,
            "started_at": NOW - 100, "connector": {"state": "running"},
            "exits": {"cmd": {"failures": health_watch.STANDALONE_CHILD_FAILURES}}}
    calls = []
    monkeypatch.setattr(service, "status", lambda root, **kw: dict(live))
    monkeypatch.setattr(health_watch, "_pid_alive", lambda pid: pid == 731)

    def restart(root, *, expected_generation=None):
        # Both updater exclusion and durable reservation precede the only effect.
        assert writer_lock.acquire_run_lock(str(home / "data/update.lock")) is None
        state = recovery._load_state(root)
        pending = state["targets"][recovery.TARGET]["pending"]
        assert pending["result"] == "reserved" and pending["request_id"] is None
        assert pending["generation"] == expected_generation == live["generation"]
        assert state["attempts"] >= 1
        calls.append((root, expected_generation))
        return REQUEST

    monkeypatch.setattr(service, "request_restart", restart)
    return home, live, calls


def _run(world, *, now=NOW, cfg=None, report=None):
    home, _, _ = world
    return recovery.run(_report(now) if report is None else report, str(home),
                        {"runtime_mode": "standalone"} if cfg is None else cfg, now)


def _pending(home):
    return recovery._load_state(str(home))["targets"][recovery.TARGET]["pending"]


def test_request_requires_durable_reservation_and_shared_update_lock(world):
    home, _, calls = world
    result = _run(world)
    assert calls == [(str(home), GENERATION)]
    action = result["actions"][0]
    assert action["action"] == "restart_request" and action["result"] == "requested"
    assert action["request_id"] == REQUEST and len(action["operation_id"]) == 32
    assert _pending(home) == {key: action[key] for key in recovery._PENDING_KEYS}
    assert recovery._load_state(str(home))["history"] == []
    assert (home / recovery.STATE_REL).stat().st_mode & 0o777 == 0o600
    fd = writer_lock.acquire_run_lock(str(home / "data/update.lock"))
    assert fd is not None
    os.close(fd)


@pytest.mark.parametrize("section", [None, "yes", {"enabled": 1}, {"max_attempts": True},
                                     {"max_attempts": 11}, {"window_s": 3599}, {"extra": True}])
def test_invalid_configuration_uses_shared_validator_and_never_acts(world, section):
    assert mcs_config._auto_recovery(section) is not None
    out = _run(world, cfg={"runtime_mode": "standalone", "auto_recovery": section})
    assert out["reason"] == "config_invalid" and world[2] == []
    assert not (world[0] / recovery.STATE_REL).exists()


@pytest.mark.parametrize("cfg, report", [
    ({}, _report()),
    ({"runtime_mode": "hermes"}, _report()),
    ({"runtime_mode": "standalone"}, _report(mode="hermes")),
    ({"runtime_mode": "standalone", "auto_recovery": {"enabled": False}}, _report()),
    ({"runtime_mode": "standalone"}, _report(NOW - 1)),
])
def test_disabled_or_unverified_runtime_never_acts(world, cfg, report):
    assert _run(world, cfg=cfg, report=report)["actions"] == []
    assert world[2] == [] and not (world[0] / recovery.STATE_REL).exists()


def test_manual_stop_and_old_install_progress_do_not_authorize_bootstrap(world, monkeypatch):
    home, _, calls = world
    _private(home / "data/install_progress.json", {
        "status": "running", "stage": "services", "updated_at": NOW - 4000, "run_id": "731-1"})
    monkeypatch.setattr(service, "status", lambda *a, **kw: (_ for _ in ()).throw(
        ValueError("standalone_status_unavailable")))
    assert _run(world)["reason"] == "host_unavailable"
    assert calls == [] and not (home / recovery.STATE_REL).exists()


@pytest.mark.parametrize("field,value", [
    ("restarting", True), ("update_in_progress", True), ("restart_reservation", "active"),
    ("restart_reservation", "uncertain"), ("update_marker", {}), ("generation", "bad"),
    ("restarting", None),
])
def test_draining_updating_or_unknown_host_never_acts(world, field, value):
    world[1][field] = value
    assert _run(world)["reason"] == "host_busy" and world[2] == []


@pytest.mark.parametrize("field", ["restart_reservation", "update_marker", "update_in_progress", "exits"])
def test_missing_host_fences_never_act(world, field):
    del world[1][field]
    assert _run(world)["reason"] == "host_busy" and world[2] == []


@pytest.mark.parametrize("hold", ["marker", "applying", "stages", "consent", "corrupt", "restore"])
def test_update_and_restore_holds_precede_reservation(world, hold):
    home, _, calls = world
    state = update_journal.default_state()
    if hold == "marker":
        _private(home / "data" / writer_lock.UPDATE_MARKER_NAME, {})
    elif hold == "restore":
        _private(home / "data" / notify_spool.RESTORE_MARKER, {"phase": "awaiting_consent"})
    else:
        state.update({"applying": {}} if hold == "applying" else
                     {"stages": [{"stage": "merged"}]} if hold == "stages" else
                     {"restore_consent": {}} if hold == "consent" else {"_corrupt": True})
        _private(home / "data/update_state.json", state)
    assert _run(world)["reason"] == "update_or_restore_held"
    assert calls == [] and not (home / recovery.STATE_REL).exists()


def test_busy_update_lock_is_not_bypassed(world):
    home, _, calls = world
    fd = writer_lock.acquire_run_lock(str(home / "data/update.lock"))
    try:
        assert _run(world)["reason"] == "update_busy"
        assert calls == [] and not (home / recovery.STATE_REL).exists()
    finally:
        os.close(fd)


@pytest.mark.parametrize("raw", [
    "{", '{"v":1,"v":1}', '{"window_start":NaN}', '{"window_start":1e999}',
    "[]", '"synthetic_secret"', "x" * (recovery.MAX_STATE_BYTES + 1),
])
def test_corrupt_or_unbounded_state_is_never_reset(world, raw):
    path = world[0] / recovery.STATE_REL
    path.write_text(raw)
    path.chmod(0o600)
    out = _run(world)
    assert out["reason"] == "state_unverified" and world[2] == []
    assert path.read_text() == raw and "synthetic_secret" not in json.dumps(out)


def test_duplicate_key_cannot_hide_a_valid_looking_empty_history(world):
    state = recovery._load_state(str(world[0]))
    raw = '{"attempts":1,' + json.dumps(state)[1:]
    assert recovery._valid_state(json.loads(raw))  # Permissive parsing would admit a new request.
    path = world[0] / recovery.STATE_REL
    path.write_text(raw)
    path.chmod(0o600)
    assert _run(world)["reason"] == "state_unverified" and world[2] == []
    assert path.read_text() == raw


@pytest.mark.parametrize("damage", ["bool_count", "unknown_target", "extra", "pending_shape", "old_format"])
def test_invalid_state_shape_is_preserved(world, damage):
    state = recovery._load_state(str(world[0]))
    if damage == "bool_count":
        state["attempts"] = True
    elif damage == "unknown_target":
        state["targets"]["synthetic_other"] = {"pending": None}
    elif damage == "extra":
        state["extra"] = "synthetic_secret"
    elif damage == "pending_shape":
        state["targets"][recovery.TARGET] = {"pending": {}}
    else:
        state = {"version": 1, "targets": {}, "history": []}
    path = world[0] / recovery.STATE_REL
    _private(path, state)
    before = path.read_bytes()
    assert _run(world)["reason"] == "state_unverified" and world[2] == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["symlink", "dangling", "fifo", "public"])
def test_untrusted_state_file_is_not_followed_or_replaced(world, kind):
    path = world[0] / recovery.STATE_REL
    destination = world[0] / "synthetic-target"
    destination.write_text("synthetic_secret")
    if kind in ("symlink", "dangling"):
        path.symlink_to(destination if kind == "symlink" else world[0] / "absent")
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        _private(path, recovery._load_state(str(world[0])))
        path.chmod(0o644)
    assert _run(world)["reason"] == "state_unverified" and world[2] == []
    assert destination.read_text() == "synthetic_secret"
    if kind in ("symlink", "dangling"):
        assert path.is_symlink()


@pytest.mark.parametrize("published", [False, True])
def test_reservation_save_failure_never_requests(world, monkeypatch, published):
    original = recovery._save_state

    def fail(home, state):
        if published:
            original(home, state)
        raise OSError("synthetic_secret")

    monkeypatch.setattr(recovery, "_save_state", fail)
    assert _run(world)["reason"] == "state_persist_failed" and world[2] == []
    if published:
        monkeypatch.setattr(recovery, "_save_state", original)
        assert _run(world, now=NOW + 2 * recovery.DEFAULT_WINDOW_S)["reason"] == "outcome_unresolved"
        assert _pending(world[0])["result"] == "reserved" and world[2] == []


@pytest.mark.parametrize("failure", ["crash", "exception", "bad_receipt", "result_save", "result_saved_then_error"])
def test_after_reservation_unknown_never_retries_across_windows(world, monkeypatch, failure):
    home, _, calls = world
    request = service.request_restart
    save = recovery._save_state

    def interrupted(root, **kwargs):
        request(root, **kwargs)
        if failure == "crash":
            raise KeyboardInterrupt
        if failure == "exception":
            raise OSError("synthetic_secret")
        return "invalid_synthetic_receipt" if failure == "bad_receipt" else REQUEST

    def persist(root, state):
        if state["targets"][recovery.TARGET]["pending"]["result"] != "reserved":
            if failure == "result_saved_then_error":
                save(root, state)
            raise OSError("synthetic_secret")
        save(root, state)

    monkeypatch.setattr(service, "request_restart", interrupted)
    if failure in ("result_save", "result_saved_then_error"):
        monkeypatch.setattr(recovery, "_save_state", persist)
    if failure == "crash":
        with pytest.raises(KeyboardInterrupt):
            _run(world)
    else:
        out = _run(world)
        assert "synthetic_secret" not in json.dumps(out)
    assert len(calls) == 1
    before = (home / recovery.STATE_REL).read_bytes()
    assert _run(world, now=NOW + 3 * recovery.DEFAULT_WINDOW_S)["reason"] == "outcome_unresolved"
    assert len(calls) == 1 and (home / recovery.STATE_REL).read_bytes() == before
    assert recovery._load_state(str(home))["attempts"] == 1


@pytest.mark.parametrize("unrelated", [["attachment_ocr_pending"], None])
def test_only_new_healthy_generation_resolves_observation(world, unrelated):
    home, live, calls = world
    _run(world)
    live["exits"] = {}
    assert _run(world, report=_report(status="ok", reasons=[]))["reason"] == "outcome_unresolved"
    live["generation"] = "c" * 32
    live["exits"] = {"cmd": {"failures": health_watch.STANDALONE_CHILD_FAILURES}}
    assert _run(world)["reason"] == "outcome_unresolved"
    live["exits"] = {}
    out = _run(world, report={**_report(status="degraded"), "state_reasons": unrelated})
    assert out["reason"] == "healthy_observed" and len(calls) == 1
    state = recovery._load_state(str(home))
    assert state["targets"][recovery.TARGET]["pending"] is None and state["attempts"] == 1
    observed = state["history"][-1]
    assert observed["observation"] == "healthy_observed"
    assert observed["generation"] == GENERATION and observed["observed_generation"] == "c" * 32
    assert observed["result"] == "requested" and observed["request_id"] == REQUEST


def test_overall_ok_does_not_complete_missing_host_evidence(world):
    home, live, calls = world
    _run(world)
    live["generation"] = "c" * 32
    del live["exits"]
    before = (home / recovery.STATE_REL).read_bytes()
    assert _run(world, report=_report(status="ok", reasons=[]))["reason"] == "host_busy"
    assert len(calls) == 1 and (home / recovery.STATE_REL).read_bytes() == before


def test_daily_budget_is_shared_and_runtime_changes_preserve_unknown(world):
    home, live, calls = world
    # The known gateway target shares the top-level budget, even before its caller is enabled.
    state = recovery._load_state(str(home))
    state.update(window_start=NOW, attempts=2, targets={"ai.hermes.gateway": {"pending": None}})
    recovery._save_state(str(home), state)
    _run(world)
    before = (home / recovery.STATE_REL).read_bytes()
    assert _run(world, cfg={"runtime_mode": "hermes"})["reason"] == "runtime_unsupported"
    assert (home / recovery.STATE_REL).read_bytes() == before
    live["generation"] = "c" * 32
    live["exits"] = {}
    assert _run(world, report=_report(status="ok", reasons=[]))["reason"] == "healthy_observed"
    live["exits"] = {"cmd": {"failures": health_watch.STANDALONE_CHILD_FAILURES}}
    assert _run(world)["reason"] == "budget_exhausted" and len(calls) == 1
    assert recovery._load_state(str(home))["attempts"] == 3
    live["updated_at"] = NOW + recovery.DEFAULT_WINDOW_S
    assert _run(world, now=NOW + recovery.DEFAULT_WINDOW_S)["actions"] and len(calls) == 2


def test_empty_target_map_does_not_reset_recorded_shared_budget(world):
    home, _, calls = world
    state = recovery._load_state(str(home))
    state.update(window_start=NOW, attempts=3)
    assert state["targets"] == {} and recovery._valid_state(state)
    recovery._save_state(str(home), state)
    before = (home / recovery.STATE_REL).read_bytes()
    assert _run(world)["reason"] == "budget_exhausted" and calls == []
    assert (home / recovery.STATE_REL).read_bytes() == before


def test_other_target_pending_survives_window_rollover(world):
    home, _, calls = world
    _run(world)
    state = recovery._load_state(str(home))
    state["targets"]["ai.hermes.gateway"] = state["targets"].pop(recovery.TARGET)
    recovery._save_state(str(home), state)
    before = (home / recovery.STATE_REL).read_bytes()
    assert _run(world, now=NOW + 2 * recovery.DEFAULT_WINDOW_S)["reason"] == "outcome_unresolved"
    assert len(calls) == 1 and (home / recovery.STATE_REL).read_bytes() == before


def test_existing_restart_request_is_not_overwritten_or_charged(world):
    home, _, calls = world
    path = home / "data" / service.RESTART_FILE
    _private(path, {"generation": GENERATION, "request_id": "d" * 32, "requested_at": NOW})
    before = path.read_bytes()
    assert _run(world)["reason"] == "restart_request_present" and calls == []
    assert path.read_bytes() == before and not (home / recovery.STATE_REL).exists()


def test_generation_race_rejects_request_before_publication(world, monkeypatch):
    home, live, calls = world
    monkeypatch.setattr(service, "request_restart", REAL_REQUEST)
    observations = iter([dict(live), {**live, "generation": "c" * 32}])
    monkeypatch.setattr(service, "status", lambda *a, **kw: next(observations))
    assert _run(world)["actions"][0]["result"] == "unknown"
    assert not (home / "data" / service.RESTART_FILE).exists() and calls == []
    assert _pending(home)["result"] == "unknown"


def test_manual_request_wins_publication_race_without_overwrite(world, monkeypatch):
    home, _, calls = world
    monkeypatch.setattr(service, "request_restart", REAL_REQUEST)
    publish = safe_io.publish_new_bytes
    request_path = home / "data" / service.RESTART_FILE
    manual = {"generation": GENERATION, "request_id": "d" * 32, "requested_at": NOW}

    def race(path, raw):
        _private(request_path, manual)
        publish(path, raw)

    monkeypatch.setattr(safe_io, "publish_new_bytes", race)
    assert _run(world)["actions"][0]["result"] == "unknown" and calls == []
    assert json.loads(request_path.read_text()) == manual
    assert _run(world)["reason"] == "outcome_unresolved"


def test_manual_restart_api_keeps_existing_replace_semantics(world):
    home, _, _ = world
    path = home / "data" / service.RESTART_FILE
    _private(path, {"old": "synthetic"})
    request_id = REAL_REQUEST(str(home))
    saved = json.loads(path.read_text())
    assert saved["request_id"] == request_id and saved["generation"] == GENERATION
    assert "old" not in saved


@pytest.mark.parametrize("damage", ["stale", "dead", "malformed"])
def test_native_status_contract_blocks_unknown_hosts(world, monkeypatch, damage):
    home, live, calls = world
    value = dict(live)
    if damage == "stale":
        value["updated_at"] = NOW - service.STATUS_TTL - 1
    elif damage == "malformed":
        value["generation"] = "bad"
    _private(home / "data" / service.STATUS_FILE, value)
    monkeypatch.setattr(service, "status", REAL_STATUS)
    monkeypatch.setattr(service, "pid_exists", lambda pid: damage != "dead")
    assert _run(world)["reason"] == "host_unavailable" and calls == []


@pytest.mark.parametrize("argv, acts", [([], False), (["--send"], True),
                                        (["--send", "--now", str(NOW)], False)])
def test_real_watch_main_requires_live_explicit_send(world, monkeypatch, argv, acts):
    home, _, calls = world
    report = _report()
    monkeypatch.setattr(health_watch, "_load_config", lambda _: {"runtime_mode": "standalone"})
    monkeypatch.setattr(health_watch, "evaluate", lambda **kw: deepcopy(report))
    monkeypatch.setattr(health_watch, "_alert_lines", lambda report: [])
    assert health_watch.main(["--home", str(home), *argv]) == 0
    assert bool(calls) is acts and (home / recovery.STATE_REL).exists() is acts
