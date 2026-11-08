"""Only current configured worker evidence can request a shared gateway restart."""
from copy import deepcopy
import json
import os
from pathlib import Path
import plistlib

import pytest

import card_worker_status
import environment_diagnostics
import health_recovery as recovery
import health_watch
import writer_lock
from mcs_util import payload_hash

NOW = 1_000_000.0
PID = 731
BIRTH = NOW - 100


def _private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def _report(now=NOW):
    return {"status": "degraded", "watched_at": now, "health_at": now,
            "state_reasons": ["worker_start_failed"]}


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True, mode=0o700)
    cfg = {"notify": {"interactive": "slack", "slack": {
        "profile": "synthetic", "application_id": "A_SYNTHETIC",
        "team_id": "T_SYNTHETIC", "channel_id": "C_SYNTHETIC"}}}
    native = {"pid": PID, "started_at": BIRTH}
    calls = []
    monkeypatch.setattr(recovery.sys, "platform", "darwin")
    monkeypatch.setattr(environment_diagnostics, "launchd_pid", lambda target, **kw: native["pid"])
    monkeypatch.setattr(environment_diagnostics, "process_started_at", lambda pid, **kw: native["started_at"])
    monkeypatch.setattr(card_worker_status.os, "kill", lambda pid, sig: None)

    def request(data, agents, uid, *, expected_pid, request_id, update_lock_fd):
        assert writer_lock.acquire_run_lock(str(home / "data/update.lock")) is None
        assert os.fstat(update_lock_fd).st_ino == (home / "data/update.lock").stat().st_ino
        assert data == str(home / "data") and agents == str(Path.home() / "Library/LaunchAgents")
        assert uid == os.getuid() and expected_pid == native["pid"]
        state = recovery._load_state(str(home))
        pending = state["targets"][recovery.GATEWAY]["pending"]
        assert pending["result"] == "reserved" and state["attempts"] >= 1
        assert pending["request_id"] == pending["operation_id"] == request_id
        assert pending["generation"] == payload_hash(native)
        calls.append((expected_pid, request_id))
        return True

    monkeypatch.setattr(recovery, "_load_gateway_restart", lambda: request)
    world = {"home": home, "cfg": cfg, "native": native, "calls": calls, "request": request}
    _record(world)
    return world


def _record(world, *, reason="worker_start_failed", **changes):
    notify = world["cfg"]["notify"]
    transport = notify["interactive"]
    path = card_worker_status._path(world["home"] / "data", transport, notify[transport]["profile"])
    _private(path, {"version": card_worker_status.VERSION, "reason": reason,
                    "at": NOW, **world["native"], **changes})
    return path


def _run(world, *, now=NOW, cfg=None, report=None):
    return recovery.run(_report(now) if report is None else report, str(world["home"]),
                        world["cfg"] if cfg is None else cfg, now)


def _pending(world):
    return recovery._load_state(str(world["home"]))["targets"][recovery.GATEWAY]["pending"]


@pytest.mark.parametrize("transport", ["slack", "discord"])
def test_current_worker_failure_reserves_and_passes_the_shared_lock(gateway, transport):
    if transport == "discord":
        scope = gateway["cfg"]["notify"].pop("slack")
        scope["guild_id"] = scope.pop("team_id")
        gateway["cfg"]["notify"].update(interactive=transport, discord=scope)
        _record(gateway)
    out = _run(gateway)
    pending = _pending(gateway)
    assert gateway["calls"] == [(PID, pending["operation_id"])]
    assert pending["request_id"] == pending["operation_id"]
    assert pending["result"] == "requested" and pending["receipt_at"] is None
    assert out["actions"] == [{"target": recovery.GATEWAY, "action": "restart_request", **pending}]
    assert recovery._load_state(str(gateway["home"]))["history"] == []
    assert _run(gateway)["reason"] == "outcome_unresolved"
    assert len(gateway["calls"]) == 1


@pytest.mark.parametrize("mutation", ["none_mode", "unknown_mode", "mode_mismatch", "bad_notify",
                                     "bad_profile", "profile_changed", "other_transport", "not_darwin"])
def test_unverified_runtime_or_configured_profile_cannot_restart(gateway, monkeypatch, mutation):
    cfg, report = deepcopy(gateway["cfg"]), _report()
    if mutation == "none_mode":
        cfg["runtime_mode"] = None
    elif mutation == "unknown_mode":
        cfg["runtime_mode"] = "other"
    elif mutation == "mode_mismatch":
        report["runtime_mode"] = "standalone"
    elif mutation == "bad_notify":
        cfg["notify"] = None
    elif mutation == "bad_profile":
        cfg["notify"]["slack"]["profile"] = 1
    elif mutation == "profile_changed":
        cfg["notify"]["slack"]["profile"] = "different"
    elif mutation == "other_transport":
        cfg["notify"]["interactive"] = "off"
    else:
        monkeypatch.setattr(recovery.sys, "platform", "linux")
    assert _run(gateway, cfg=cfg, report=report)["actions"] == []
    assert gateway["calls"] == [] and not (gateway["home"] / recovery.STATE_REL).exists()


@pytest.mark.parametrize("changes", [{"version": "old"}, {"pid": PID + 1}, {"pid": True},
    {"started_at": BIRTH + 1}, {"started_at": float("nan")}, {"at": NOW + 1},
    {"reason": "profile_mismatch"}, {"reason": "worker_restart_required"}, {"extra": "secret"}])
def test_old_or_mismatched_worker_record_is_not_current_failure(gateway, changes):
    _record(gateway, **changes)
    out = _run(gateway)
    assert out["reason"] == "gateway_unavailable" and gateway["calls"] == []
    assert "secret" not in json.dumps(out)


@pytest.mark.parametrize("pids,birth", [([None, None], BIRTH), ([PID, PID + 1], BIRTH),
    ([True, None], BIRTH), ([PID + 1, PID + 1], BIRTH), ([PID, None], BIRTH + 1)])
def test_absent_ambiguous_or_reused_native_process_never_acts(gateway, monkeypatch, pids, birth):
    results = iter(pids)
    monkeypatch.setattr(environment_diagnostics, "launchd_pid", lambda *a, **kw: next(results))
    monkeypatch.setattr(environment_diagnostics, "process_started_at", lambda *a, **kw: birth)
    assert _run(gateway)["reason"] == "gateway_unavailable" and gateway["calls"] == []


@pytest.mark.parametrize("damage", ["duplicate", "oversize", "symlink", "public", "status_race"])
def test_worker_witness_requires_strict_private_stable_bytes(gateway, monkeypatch, damage):
    path = _record(gateway)
    if damage == "duplicate":
        path.write_text('{"pid":1,' + path.read_text()[1:])
    elif damage == "oversize":
        path.write_text(" " * 2049)
    elif damage == "symlink":
        target = path.with_suffix(".saved")
        path.rename(target)
        path.symlink_to(target)
    elif damage == "public":
        path.chmod(0o644)
    else:
        status = card_worker_status.status

        def race(*args):
            value = status(*args)
            _record(gateway, pid=PID + 1)
            return value

        monkeypatch.setattr(card_worker_status, "status", race)
    assert _run(gateway)["reason"] == "gateway_unavailable" and gateway["calls"] == []


@pytest.mark.parametrize("changes", [{"status": "stale"}, {"status": "ok"},
    {"health_at": NOW - 1801}, {"health_at": NOW + 1}, {"health_at": None},
    {"state_reasons": ["worker_restart_required"]}, {"state_reasons": ["attachment_ocr_pending"]},
    {"state_reasons": ["notification_unknown"]}, {"watched_at": NOW - 1}])
def test_only_fresh_failure_report_is_eligible(gateway, changes):
    assert _run(gateway, report={**_report(), **changes})["actions"] == []
    assert gateway["calls"] == [] and not (gateway["home"] / recovery.STATE_REL).exists()


@pytest.mark.parametrize("receipt", [
    {"status": "requested", "at": 1}, {"status": "unknown", "at": 1},
    {"status": "failed", "at": NOW, "error": "synthetic_secret"},
    {"status": "failed", "at": NOW + 1, "error": "gateway_restart_spawn_failed"},
    {"status": "failed", "at": NOW, "error": "gateway_restart_spawn_failed", "unknown": 1},
    {"status": "supervisor_restart_verified", "at": NOW},
    {"status": "failed", "at": NOW, "error": "gateway_restart_spawn_failed", "request_id": True},
])
def test_existing_manual_unresolved_or_unverified_receipt_is_never_overwritten(gateway, receipt):
    path = gateway["home"] / "data/gateway_restart.json"
    _private(path, receipt)
    before = path.read_bytes()
    out = _run(gateway)
    assert out["reason"] == "gateway_request_unresolved" and gateway["calls"] == []
    assert path.read_bytes() == before and not (gateway["home"] / recovery.STATE_REL).exists()
    assert "synthetic_secret" not in json.dumps(out)


@pytest.mark.parametrize("raw", ["{", '{"status":"requested","status":"failed"}',
                                   '{"status":"failed","at":NaN}', "x" * 4097])
def test_corrupt_native_receipt_holds_without_reset(gateway, raw):
    path = gateway["home"] / "data/gateway_restart.json"
    path.write_text(raw)
    path.chmod(0o600)
    assert _run(gateway)["reason"] == "gateway_request_unresolved"
    assert path.read_text() == raw and gateway["calls"] == []


@pytest.mark.parametrize("damage", ["symlink", "dangling", "public"])
def test_untrusted_native_receipt_path_cannot_be_treated_as_absent(gateway, damage):
    path = gateway["home"] / "data/gateway_restart.json"
    if damage == "public":
        _private(path, {"status": "failed", "at": NOW, "error": "gateway_restart_spawn_failed"})
        path.chmod(0o644)
    else:
        target = path.with_suffix(".saved")
        if damage == "symlink":
            _private(target, {"status": "requested", "at": 1})
        path.symlink_to(target)
    assert _run(gateway)["reason"] == "gateway_request_unresolved" and gateway["calls"] == []
    assert not (gateway["home"] / recovery.STATE_REL).exists()
    if damage != "public":
        assert path.is_symlink()


@pytest.mark.parametrize("status", ["failed", "supervisor_restart_verified"])
def test_verified_terminal_native_receipt_allows_a_new_reservation(gateway, status):
    receipt = {"status": status, "at": NOW - 1, "request_id": "c" * 32}
    receipt.update({"error": "gateway_restart_spawn_failed"} if status == "failed" else {
        "previous_pid": PID - 2, "pid": PID - 1, "service": f"gui/{os.getuid()}/{recovery.GATEWAY}"})
    _private(gateway["home"] / "data/gateway_restart.json", receipt)
    assert _run(gateway)["actions"] and len(gateway["calls"]) == 1


@pytest.mark.parametrize("failure", ["crash", "effect_error", "record_error"])
def test_gateway_reservation_never_replays_uncertain_effect_across_windows(gateway, monkeypatch, failure):
    request, save = gateway["request"], recovery._save_state

    def effect(*args, **kwargs):
        request(*args, **kwargs)
        if failure == "crash":
            raise KeyboardInterrupt
        if failure == "effect_error":
            raise OSError("synthetic_secret")
        return True

    def persist(home, state):
        if state["targets"][recovery.GATEWAY]["pending"]["result"] != "reserved":
            raise OSError("synthetic_secret")
        save(home, state)

    monkeypatch.setattr(recovery, "_load_gateway_restart", lambda: effect)
    if failure == "record_error":
        monkeypatch.setattr(recovery, "_save_state", persist)
    if failure == "crash":
        with pytest.raises(KeyboardInterrupt):
            _run(gateway)
    else:
        assert "synthetic_secret" not in json.dumps(_run(gateway))
    before = (gateway["home"] / recovery.STATE_REL).read_bytes()
    assert _run(gateway, now=NOW + 3 * recovery.DEFAULT_WINDOW_S)["reason"] == "outcome_unresolved"
    assert len(gateway["calls"]) == 1 and (gateway["home"] / recovery.STATE_REL).read_bytes() == before


def test_reservation_save_failure_does_not_call_gateway_helper(gateway, monkeypatch):
    def fail(*args):
        raise OSError("synthetic_secret")

    monkeypatch.setattr(recovery, "_save_state", fail)
    assert _run(gateway)["reason"] == "state_persist_failed" and gateway["calls"] == []


def test_gateway_uses_same_global_budget_and_update_lock(gateway):
    state = recovery._load_state(str(gateway["home"]))
    state.update(window_start=NOW, attempts=3, targets={recovery.TARGET: {"pending": None}})
    recovery._save_state(str(gateway["home"]), state)
    assert _run(gateway)["reason"] == "budget_exhausted" and gateway["calls"] == []
    lock = writer_lock.acquire_run_lock(str(gateway["home"] / "data/update.lock"))
    try:
        assert _run(gateway)["reason"] == "update_busy" and gateway["calls"] == []
    finally:
        os.close(lock)


@pytest.mark.parametrize("hold", ["update", "restore"])
def test_gateway_does_not_bypass_update_or_restore_fences(gateway, hold):
    data = gateway["home"] / "data"
    _private(data / (writer_lock.UPDATE_MARKER_NAME if hold == "update" else "restore_pending.json"),
             {"phase": "awaiting_consent"})
    assert _run(gateway)["reason"] == "update_or_restore_held" and gateway["calls"] == []
    assert not (gateway["home"] / recovery.STATE_REL).exists()


@pytest.mark.parametrize("damage", ["other_id", "missing_pid", "wrong_pid", "old_at", "future_at"])
def test_receipt_without_this_request_and_process_proof_is_ignored(gateway, damage):
    _run(gateway)
    pending = _pending(gateway)
    receipt = {"request_id": pending["operation_id"], "status": "unknown",
               "previous_pid": PID, "at": NOW + 1}
    if damage == "other_id":
        receipt["request_id"] = "f" * 32
    elif damage == "missing_pid":
        del receipt["previous_pid"]
    elif damage == "wrong_pid":
        receipt["previous_pid"] += 1
    elif damage == "old_at":
        receipt["at"] = NOW - 1
    else:
        receipt["at"] = NOW + 3
    _private(gateway["home"] / "data/gateway_restart.json", receipt)
    before = (gateway["home"] / recovery.STATE_REL).read_bytes()
    assert _run(gateway, now=NOW + 2)["reason"] == "outcome_unresolved"
    assert (gateway["home"] / recovery.STATE_REL).read_bytes() == before
    assert len(gateway["calls"]) == 1


def test_replaced_process_is_not_healthy_until_current_worker_started(gateway):
    _run(gateway)
    pending = _pending(gateway)
    _private(gateway["home"] / "data/gateway_restart.json", {
        "request_id": pending["operation_id"], "status": "supervisor_restart_verified",
        "previous_pid": PID, "pid": PID + 1, "at": NOW + 1})
    assert _run(gateway, now=NOW + 2)["reason"] == "outcome_unresolved"
    assert _pending(gateway)["result"] == "process_replaced"
    _record(gateway, reason="worker_started")
    assert _run(gateway, now=NOW + 2)["reason"] == "outcome_unresolved"  # Same generation.
    gateway["native"].update(pid=PID + 1, started_at=NOW + 1)
    _record(gateway, reason="worker_started", at=NOW + 2)
    report = {**_report(NOW + 2), "state_reasons": ["attachment_ocr_pending", "notification_unknown"]}
    assert _run(gateway, now=NOW + 2, report=report)["reason"] == "healthy_observed"
    state = recovery._load_state(str(gateway["home"]))
    assert state["targets"][recovery.GATEWAY]["pending"] is None and state["attempts"] == 1
    assert state["history"][-1]["observation"] == "healthy_observed" and len(gateway["calls"]) == 1


@pytest.mark.parametrize("capability", ["current", "old", "broken"])
def test_gateway_loader_requires_fixed_checkout_and_auto_contract(gateway, monkeypatch, capability):
    def load(path):
        assert Path(path) == Path(recovery.__file__).resolve().parents[2] / "deployment/recovery/mcs_recover.py"
        if capability == "broken":
            raise ValueError("synthetic_secret")
        return {"request_gateway_restart": gateway["request"] if capability == "current"
                else lambda data, agents, uid: True}

    # Fixture only replaces the loader; restore the real function for this contract check.
    monkeypatch.setattr(recovery.runpy, "run_path", load)
    request = REAL_LOADER()
    assert (request is gateway["request"]) is (capability == "current")
    monkeypatch.setattr(recovery, "_load_gateway_restart", lambda: request)
    out = _run(gateway)
    assert bool(out["actions"]) is (capability == "current")
    if capability != "current":
        assert out["reason"] == "gateway_helper_unavailable"
        assert not (gateway["home"] / recovery.STATE_REL).exists()


REAL_LOADER = recovery._load_gateway_restart


@pytest.mark.parametrize("argv, acts", [([], False), (["--send"], True),
                                        (["--send", "--now", str(NOW)], False)])
def test_real_watch_entry_runs_gateway_recovery_only_with_live_send(gateway, monkeypatch, argv, acts):
    monkeypatch.setattr(health_watch, "_load_config", lambda _: gateway["cfg"])
    monkeypatch.setattr(health_watch, "evaluate", lambda **kw: _report())
    monkeypatch.setattr(health_watch, "_alert_lines", lambda report: [])
    assert health_watch.main(["--home", str(gateway["home"]), *argv]) == 0
    assert bool(gateway["calls"]) is acts


def test_hermes_launchers_opt_in_to_live_recovery():
    root = Path(__file__).resolve().parents[2]
    plist = plistlib.loads((root / "deployment/launchagents/local.mcs-health.plist").read_bytes())
    assert plist["ProgramArguments"][-2:] == ["__REPO__/mcs/ingest/health_watch.py", "--send"]
    assert '"$PY" __REPO__/mcs/ingest/health_watch.py --send' in (
        root / "deployment/scripts/mcs_health.sh").read_text()
