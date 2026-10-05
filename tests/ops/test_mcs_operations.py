"""Synthetic contracts for the CCO-only durable operation commands."""

import json
import time
import uuid

import pytest

from ledger import Ledger
import job_ops
import mcs_operations
import mcs_requests


def _command(cmd, project_id=1, **fields):
    return {
        "version": 1,
        "cmd": cmd,
        "command_id": str(uuid.uuid4()),
        "actor": "synthetic-cco",
        "human_confirmed": True,
        "project_id": project_id,
        **fields,
    }


def _job(db, job_id):
    return db.db.execute(
        "SELECT * FROM fetch_jobs WHERE job_id=?", (job_id,)).fetchone()


def test_ops_retry_preserves_attempts_and_receipt_is_idempotent(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.ensure_patient(1)
    payload = {"generation": "g1", "source_generation": "s1",
               "targets": [42]}
    job_id = db.job_add("semantic", 1, 42, payload=payload)
    db.db.execute(
        "UPDATE fetch_jobs SET state='failed',attempts=2,next_try=? "
        "WHERE job_id=?", (time.time() + 9999, job_id))
    db.db.commit()
    req = _command(
        "ops.retry", job_id=job_id,
        expected_payload_hash=mcs_requests.payload_hash(payload),
    )

    first = mcs_requests.apply_command(db, req)
    second = mcs_requests.apply_command(db, req)
    row = _job(db, job_id)
    assert first["outcome"] == second["outcome"] == "applied"
    assert first == second
    assert row["state"] == "pending" and row["attempts"] == 2
    assert row["next_try"] == 0
    assert json.loads(row["payload"])["retry_command_id"] == req["command_id"]
    db.close()


def test_ops_scan_deepens_existing_history_without_resetting_cursor(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.ensure_patient(1)
    now = time.time()
    old_payload = {"since": int(now - 10 * 86400), "page": 4,
                   "pages": 2, "trickle": True}
    job_id = db.job_add("history", 1, payload=old_payload)
    req = _command("ops.scan", days=30, pages=5)

    receipt = mcs_requests.apply_command(db, req)
    row = _job(db, job_id)
    payload = json.loads(row["payload"])
    assert receipt["outcome"] == "applied"
    assert receipt["job_id"] == job_id
    assert row["state"] == "pending"
    assert payload["page"] == 4
    assert payload["pages"] == 5
    assert payload["since"] < old_payload["since"]
    assert payload["trickle"] is False
    db.close()


def test_pause_resume_records_control_and_invalidates_pending_tokens(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    payload = {"generation": "g1", "source_generation": "s1",
               "targets": [7]}
    job_id = db.job_add("semantic", 1, 7, payload=payload, next_try=9999)
    corrupt_id = db.job_add("semantic", 1, 8,
                            payload={"generation": "bad"}, next_try=9999)
    db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                  ("{corrupt", corrupt_id))
    db.db.execute(
        "UPDATE fetch_jobs SET attempts=3 WHERE job_id=?", (job_id,))
    db.db.commit()
    db.ensure_patient(1)
    corrupt_payload = _job(db, corrupt_id)["payload"]

    paused_receipt = mcs_requests.apply_command(
        db, _command("ops.pause", feature="semantic"))
    paused_row = _job(db, job_id)
    paused_payload = json.loads(paused_row["payload"])
    control = db.db.execute(
        "SELECT content,meta FROM artifacts WHERE kind='semantic_control' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    assert paused_receipt["outcome"] == "applied"
    assert paused_receipt["skipped_jobs"] == 1
    assert mcs_operations.paused(db, 1)
    assert control["content"] == "paused"
    assert json.loads(control["meta"])["actor"] == "synthetic-cco"
    assert paused_payload["control_generation"] == \
        paused_receipt["control_artifact_id"]
    assert paused_row["attempts"] == 3 and paused_row["next_try"] == 9999
    assert _job(db, corrupt_id)["payload"] == corrupt_payload

    resumed_receipt = mcs_requests.apply_command(
        db, _command("ops.resume", feature="semantic"))
    resumed_row = _job(db, job_id)
    resumed_payload = json.loads(resumed_row["payload"])
    assert resumed_receipt["outcome"] == "applied"
    assert resumed_receipt["skipped_jobs"] == 1
    assert not mcs_operations.paused(db, 1)
    assert resumed_payload["control_generation"] == \
        resumed_receipt["control_artifact_id"]
    assert resumed_payload["control_generation"] != \
        paused_payload["control_generation"]
    assert resumed_row["attempts"] == 3 and resumed_row["next_try"] == 0
    assert _job(db, corrupt_id)["payload"] == corrupt_payload
    assert db.db.execute(
        "SELECT COUNT(*) FROM fetch_jobs WHERE kind='semantic' "
        "AND project_id=1").fetchone()[0] == 2
    db.close()


def test_drain_commands_routes_ops_through_receipt_path(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.ensure_patient(1)
    cmd_dir = tmp_path / "cmd"
    cmd_dir.mkdir()
    req = _command("ops.pause", feature="semantic")
    mcs_requests.enqueue(req, str(cmd_dir))

    result = {"errors": []}
    job_ops.drain_commands(db, result, str(cmd_dir))
    receipt = db.db.execute(
        "SELECT outcome FROM command_receipts WHERE command_id=?",
        (req["command_id"],)).fetchone()
    assert result["command_commands"] == 1
    assert result["ops_commands"] == 1
    assert receipt["outcome"] == "applied"
    assert not list(cmd_dir.glob("*.json"))
    db.close()


# ---------------------------------------------------------- update ops

def _update_command(cmd, **fields):
    """Projectless lifecycle op — project_id stays None."""
    req = _command(cmd, project_id=None, reason="go")
    req.update(fields)
    return req


def test_update_apply_rejects_when_updater_not_deployed(
        tmp_path, monkeypatch):
    """A receipt claiming 'scheduled' while nothing can launch the
    updater is a lie — reject before commit (C)."""
    import mcs_update
    import mcs_util
    monkeypatch.setattr(mcs_update, "WRAPPER",
                        str(tmp_path / "no-such-wrapper.sh"))
    monkeypatch.setattr(mcs_util, "load_config",
                        lambda: {"update": {"mode": "notify"}})
    db = Ledger(str(tmp_path / "ledger.db"))
    req = _update_command("ops.update_apply", tag="v1.2.3",
                          target_sha="a" * 40)
    receipt = mcs_requests.apply_command(db, req)
    assert receipt["outcome"] == "rejected"
    assert receipt["error"] == "updater_not_deployed"
    db.close()


def test_update_apply_rejects_when_mode_off(tmp_path, monkeypatch):
    """mode=off must reject at intake — otherwise the receipt sits
    dormant while the user believes it was scheduled (Q17)."""
    import mcs_update
    import mcs_util
    wrapper = tmp_path / "mcs_update.sh"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(mcs_update, "WRAPPER", str(wrapper))
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    db = Ledger(str(tmp_path / "ledger.db"))
    req = _update_command("ops.update_apply", tag="v1.2.3",
                          target_sha="a" * 40)
    receipt = mcs_requests.apply_command(db, req)
    assert receipt["outcome"] == "rejected"
    assert receipt["error"] == "update_disabled"
    db.close()


def test_update_apply_rejects_project_id_and_extra_fields(
        tmp_path, monkeypatch):
    """Lifecycle ops are projectless — a smuggled project_id or an
    unused sha pin on rollback must be rejected, never silently
    ignored (O/S2)."""
    db = Ledger(str(tmp_path / "ledger.db"))
    bad = _update_command("ops.update_apply", tag="v1.2.3",
                          project_id=9)
    assert mcs_requests.apply_command(db, bad)["error"] \
        == "bad_project_id"
    bad2 = _update_command("ops.update_rollback", target_sha="a" * 40)
    receipt = mcs_requests.apply_command(db, bad2)
    assert receipt["outcome"] == "rejected"
    assert receipt["error"] == "unknown_field"
    db.close()


def test_update_apply_schedules_and_pins_sha(tmp_path, monkeypatch):
    """Happy path: deployed wrapper + mode on => scheduled receipt
    with tag/sha pins recorded."""
    import mcs_update
    import mcs_util
    wrapper = tmp_path / "mcs_update.sh"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(mcs_update, "WRAPPER", str(wrapper))
    monkeypatch.setattr(mcs_util, "load_config",
                        lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(mcs_update, "remote_tag_sha",
                        lambda t: "b" * 40)
    monkeypatch.setattr(mcs_update, "current_version",
                        lambda: ("v1.0.0", "c" * 40))
    db = Ledger(str(tmp_path / "ledger.db"))
    req = _update_command("ops.update_apply", tag="v1.2.3")
    receipt = mcs_requests.apply_command(db, req)
    assert receipt["outcome"] == "applied"
    assert receipt["scheduled"] is True
    assert receipt["cmd"] == "ops.update_apply"
    assert receipt["target_sha"] == "b" * 40   # pinned at approval
    assert receipt["base_sha"] == "c" * 40
    assert receipt["reason"] == "go"
    db.close()


def _update_env(tmp_path, monkeypatch):
    """Deployed wrapper + mode on; returns the mcs_update module for
    further stubbing."""
    import mcs_update
    import mcs_util
    wrapper = tmp_path / "mcs_update.sh"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(mcs_update, "WRAPPER", str(wrapper))
    monkeypatch.setattr(mcs_util, "load_config",
                        lambda: {"update": {"mode": "notify"}})
    return mcs_update


def test_update_apply_resolves_sha_outside_tx(tmp_path, monkeypatch):
    """remote_tag_sha (ls-remote = network) must run BEFORE the write
    transaction via prepare_update_pins — an ls-remote inside BEGIN
    IMMEDIATE would hold the DB writer lock for the network
    round-trip (F-tx)."""
    mcs_update = _update_env(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(mcs_update, "remote_tag_sha",
                        lambda t: calls.append(t) or "b" * 40)
    monkeypatch.setattr(mcs_update, "current_version",
                        lambda: ("v1.0.0", "c" * 40))
    db = Ledger(str(tmp_path / "ledger.db"))
    req = _update_command("ops.update_apply", tag="v1.2.3")
    receipt = mcs_requests.apply_command(db, req)
    assert receipt["outcome"] == "applied"
    assert receipt["target_sha"] == "b" * 40
    assert receipt["base_sha"] == "c" * 40
    assert calls == ["v1.2.3"]               # resolved once, pre-tx
    assert mcs_requests.apply_command(db, req) == receipt
    assert calls == ["v1.2.3"]               # replay never resolves the tag again
    db.close()


def test_unconfirmed_update_never_resolves_remote_pins(tmp_path, monkeypatch):
    mcs_update = _update_env(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(mcs_update, "remote_tag_sha", lambda tag: calls.append(tag) or "b" * 40)
    monkeypatch.setattr(mcs_update, "current_version", lambda: ("v1.0.0", "c" * 40))
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        receipt = mcs_requests.apply_command(db, _update_command(
            "ops.update_apply", tag="v1.2.3", human_confirmed=False))
        assert receipt["error"] == "human_confirmation_required"
        assert calls == []
    finally:
        db.close()


def test_signal_dismiss_rejects_corrupt_evidence_with_receipt(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.artifact_add("signal_v1", json.dumps({"state": "open", "evidence": []}),
                        project_id=1, meta={"key": "synthetic-signal"})
        receipt = mcs_requests.apply_command(db, _command(
            "ops.signal_dismiss", signal_key="synthetic-signal", reason="reviewed"))
        assert receipt["outcome"] == "rejected"
        assert receipt["error"] == "signal_corrupt"
        assert len(db.artifacts("signal_v1", project_id=1)) == 1
    finally:
        db.close()


@pytest.mark.parametrize("raw", ["{broken", "[" * 1500 + "0" + "]" * 1500,
                                 "[]", "null"], ids=["malformed", "deep", "array", "null"])
@pytest.mark.parametrize("pin", ["old", "latest", "absent"])
def test_signal_dismiss_never_falls_back_past_corrupt_latest(tmp_path, raw, pin):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        key = "synthetic-operation-signal"
        old = db.artifact_add(
            "signal_v1", json.dumps({"state": "open", "evidence": {}}),
            project_id=1, meta={"key": key})
        latest = db.artifact_add("signal_v1", raw, project_id=1, meta={"key": key})
        fields = {"signal_key": key, "reason": "synthetic human review"}
        if pin != "absent":
            fields["expected_signal_artifact_id"] = old if pin == "old" else latest
        command = _command("ops.signal_dismiss", **fields)
        receipt = mcs_requests.apply_command(db, command)
        assert receipt["outcome"] == "rejected"
        assert receipt["error"] == ("signal_changed" if pin == "old" else "signal_corrupt")
        assert mcs_requests.apply_command(db, command) == receipt
        assert len(db.artifacts("signal_v1", project_id=1)) == 2
        stored = db.db.execute(
            "SELECT receipt_json FROM command_receipts WHERE command_id=?",
            (command["command_id"],)).fetchone()
        assert json.loads(stored[0]) == receipt
    finally:
        db.close()


def test_signal_dismiss_corrupt_foreign_latest_checks_scope_before_pin(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        key = "synthetic-operation-signal"
        old = db.artifact_add(
            "signal_v1", json.dumps({"state": "open", "evidence": {}}),
            project_id=1, meta={"key": key})
        db.artifact_add("signal_v1", "{broken", project_id=2, meta={"key": key})
        receipt = mcs_requests.apply_command(db, _command(
            "ops.signal_dismiss", signal_key=key, reason="synthetic human review",
            expected_signal_artifact_id=old))
        assert receipt["outcome"] == "rejected"
        assert receipt["error"] == "project_mismatch"
        assert "current_artifact_id" not in receipt
        assert len(db.artifacts("signal_v1")) == 2
    finally:
        db.close()


def test_update_apply_unresolvable_tag_rejected(tmp_path, monkeypatch):
    """A tag that fails to resolve pre-tx rejects the receipt — the
    in-tx path never performs its own network lookup as a fallback."""
    mcs_update = _update_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mcs_update, "remote_tag_sha",
        lambda t: (_ for _ in ()).throw(mcs_update.UpdateError("x")))
    db = Ledger(str(tmp_path / "ledger.db"))
    req = _update_command("ops.update_apply", tag="v1.2.3")
    receipt = mcs_requests.apply_command(db, req)
    assert receipt["outcome"] == "rejected"
    assert receipt["error"] == "update_tag_unresolvable"
    db.close()


def test_update_apply_unresolvable_base_rejected(tmp_path, monkeypatch):
    """A HEAD that fails to resolve pre-tx rejects the receipt — never a
    committed approval with base_sha null, and no in-tx git retry."""
    mcs_update = _update_env(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(mcs_update, "remote_tag_sha", lambda t: "b" * 40)

    def boom():
        calls.append(1)
        raise mcs_update.UpdateError("x")
    monkeypatch.setattr(mcs_update, "current_version", boom)
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        receipt = mcs_requests.apply_command(
            db, _update_command("ops.update_apply", tag="v1.2.3"))
        assert receipt["outcome"] == "rejected"
        assert receipt["error"] == "update_base_unresolvable"
        assert "base_sha" not in receipt
        assert calls == [1]                  # pre-tx only
    finally:
        db.close()


def test_update_apply_pinned_sha_skips_remote(tmp_path, monkeypatch):
    """A pre-pinned target_sha must not trigger any remote lookup —
    neither pre-tx nor in-tx."""
    mcs_update = _update_env(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(mcs_update, "remote_tag_sha",
                        lambda t: calls.append(t) or None)
    monkeypatch.setattr(mcs_update, "current_version",
                        lambda: ("v1.0.0", "c" * 40))
    db = Ledger(str(tmp_path / "ledger.db"))
    req = _update_command("ops.update_apply", tag="v1.2.3",
                          target_sha="d" * 40)
    receipt = mcs_requests.apply_command(db, req)
    assert receipt["outcome"] == "applied"
    assert receipt["target_sha"] == "d" * 40
    assert calls == []
    db.close()


# -------------------------------------------------------- restore consent

def test_restore_approve_commits_bound_receipt(tmp_path):
    """Per-restore consent: the committed receipt carries the exact
    report/backup binding the consent gate compares (T13)."""
    db = Ledger(str(tmp_path / "ledger.db"))
    req = _update_command(
        "ops.restore_approve",
        report_id="a" * 64, backup_sha256="b" * 64,
        backup_schema=7, reason="loss reviewed")
    receipt = mcs_requests.apply_command(db, req)
    assert receipt["outcome"] == "applied"
    assert receipt["scheduled"] is True
    assert receipt["cmd"] == "ops.restore_approve"
    assert receipt["report_id"] == "a" * 64
    assert receipt["backup_sha256"] == "b" * 64
    assert receipt["backup_schema"] == 7
    assert receipt["reason"] == "loss reviewed"
    # idempotent replay — the same command_id re-reads the receipt
    again = mcs_requests.apply_command(db, req)
    assert again == receipt
    db.close()


def test_restore_approve_rejects_bad_binding(tmp_path):
    """The binding fields are the gate — malformed/absent ones reject
    before any receipt commits (T13)."""
    db = Ledger(str(tmp_path / "ledger.db"))
    base = _update_command(
        "ops.restore_approve",
        report_id="a" * 64, backup_sha256="b" * 64,
        backup_schema=7, reason="r")
    for field, value in (
            ("report_id", "short"),
            ("backup_sha256", None),
            ("backup_schema", "7"),
            ("backup_schema", -1)):
        bad = dict(base, command_id=str(uuid.uuid4()))
        bad[field] = value
        assert mcs_requests.apply_command(db, bad)["outcome"] == "rejected"
    # no report binding at all
    bad = dict(base, command_id=str(uuid.uuid4()))
    del bad["report_id"]
    assert mcs_requests.apply_command(db, bad)["error"] == "bad_report_id"
    # project scope forbidden — lifecycle ops are projectless
    bad = dict(base, command_id=str(uuid.uuid4()), project_id=9)
    assert mcs_requests.apply_command(db, bad)["error"] == "bad_project_id"
    # unknown fields never silently ignored
    bad = dict(base, command_id=str(uuid.uuid4()), scope="ev")
    assert mcs_requests.apply_command(db, bad)["error"] == "unknown_field"
    # human_confirmed is the shared envelope gate
    bad = dict(base, command_id=str(uuid.uuid4()), human_confirmed=False)
    assert mcs_requests.apply_command(db, bad)["error"] == \
        "human_confirmation_required"
    db.close()


def test_restore_approve_drains_during_consent_hold(tmp_path, monkeypatch):
    """While the awaiting_consent marker stands, drain_commands must
    still accept ops.restore_approve (it is the only way out) and spawn
    the updater after the commit — while every other command stays
    queued (T13)."""
    import notify_cards
    data = tmp_path / "data"
    data.mkdir()
    db = Ledger(str(data / "ledger.db"))
    db.ensure_patient(1)
    cmd_dir = data / "cmd"
    cmd_dir.mkdir()
    notify_cards.mark_restored(
        str(tmp_path / "data"), backup_path="/b.db",
        phase="awaiting_consent")
    mcs_requests.enqueue(_update_command(
        "ops.restore_approve",
        report_id="a" * 64, backup_sha256="b" * 64,
        backup_schema=7, reason="ok"), str(cmd_dir))
    mcs_requests.enqueue(_command("ops.pause", feature="semantic"),
                         str(cmd_dir))
    legacy = cmd_dir / "legacy-import.json"
    legacy.write_text(json.dumps({"cmd": "import", "project_id": 99}))
    import mcs_update
    spawned = []
    monkeypatch.setattr(mcs_update, "spawn_detached",
                        lambda: spawned.append(1))
    result = {"errors": []}
    job_ops.drain_commands(db, result, str(cmd_dir))
    assert result["command_commands"] == 1       # only the consent op
    assert spawned == [1]                        # updater re-launched
    left = [p.name for p in cmd_dir.glob("*.json")]
    assert len(left) == 2                        # pause and import stayed queued
    assert legacy.exists()
    assert db.db.execute("SELECT 1 FROM patients WHERE project_id=99").fetchone() is None
    db.close()
