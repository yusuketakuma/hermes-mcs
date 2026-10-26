"""Local-only cloud estimates and receipt-based global controls with synthetic data."""
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from types import SimpleNamespace
import uuid

import pytest

import anthropic_client
import cloud_cli as cli
import cloud_policy
import extract_llm
import mcs_operations
import mcs_requests
import mcs_setup
import mcs_util
from ledger import Ledger
from extract_testkit import _message, _hash


@pytest.fixture
def world(tmp_path, monkeypatch):
    now = datetime(2026, 10, 26, 12, tzinfo=cloud_policy.JST).timestamp()
    monkeypatch.setattr(cli.time, "time", lambda: now)
    for name in ("MCS_SETUP_PASSWORD", "TYPESAFE_API_KEY", "MCS_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    config = {"mcs_login_id": "synthetic", "notify_target": "slack:#synthetic",
              "cloud_llm": {"mode": "on", "project_ids": [1]}}
    (tmp_path / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr(mcs_util, "load_config", lambda *a, **kw: config)
    monkeypatch.setattr(anthropic_client.AnthropicClient, "complete", lambda *a, **k: pytest.fail("no API"))
    path = tmp_path / "data" / "ledger.db"
    path.parent.mkdir()
    with closing(Ledger(str(path))) as db:
        db.db.execute("INSERT INTO patients(project_id,patient_name,is_archived) VALUES(1,'synthetic-private-patient',0)")
        db.db.execute("INSERT INTO patients(project_id,patient_name,is_archived) VALUES(2,'synthetic-excluded-patient',0)")
        db.db.commit()
        for mid, pid in ((1, 1), (2, 2)):
            db.save_messages([_message(mid, body="synthetic private body", project_id=pid,
                                       posted_at="2026-10-26T10:00:00+09:00")])
            db.artifact_add("extract_llm", "{}", project_id=pid, message_id=mid,
                meta={"hash": _hash(db, mid), "extract_version": extract_llm.EXTRACT_VERSION - 1})
    return tmp_path, path, config


def _key(root):
    path = root / ".env"
    path.write_text("MCS_ANTHROPIC_API_KEY=synthetic-secret-key\nTYPESAFE_API_KEY=synthetic-jev-key\n")
    path.chmod(0o600)
    return path


def _req(action, **kw):
    return {"version": 1, "cmd": "ops.cloud_" + action, "command_id": str(uuid.uuid4()),
            "actor": "synthetic-human", "human_confirmed": True, "project_id": None,
            "reason": "synthetic maintenance", **kw}


def test_plan_is_readonly_keyless_and_outputs_only_estimates(world):
    root, path, config = world
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = cli.observe(config, root=str(root))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert report["send_decision"] == "no_api_key"
    assert report["routes"]["reextract"]["targets"] == 1
    assert report["routes"]["patient_context"]["targets"] == 1
    assert report["accounting"]["daily_requests"] == 0
    assert report["input_estimate_basis"] == "character_proxy"
    assert report["reservation_input_upper_bound_tokens"] == 1048576
    raw = json.dumps(report)
    assert "synthetic private body" not in raw and "synthetic-private-patient" not in raw
    assert "message_id" not in raw and "project_id" not in raw and str(root) not in raw


def test_estimate_has_daily_monthly_oneoff_and_optional_fx(world):
    root, _, config = world
    config["cloud_llm"]["usd_jpy"] = 150
    report = cli.observe(config, root=str(root), days=14)
    daily = report["patient_context_recurring_estimate"]["daily"]
    monthly = report["patient_context_recurring_estimate"]["monthly_30_days"]
    assert monthly["requests_estimate"] >= daily["requests_estimate"]
    assert Decimal(daily["estimated_cost_low_jpy"]) == Decimal(daily["estimated_cost_low_usd"]) * 150
    assert report["reextract_backlog_once"]["targets"] == 1
    assert report["new_posts_daily_estimate"] > 0


def test_log_filter_never_displays_ids_or_untrusted_metadata(world):
    root, path, config = world
    with closing(Ledger(str(path))) as db:
        for pid in (1, 2):
            db.artifact_add("cloud_usage_dryrun", "synthetic hidden content", project_id=pid,
                meta={"route": "reextract", "requests": pid, "model": "synthetic hidden model",
                      "project_id": pid, "owner": "synthetic hidden owner", "error_kind": {"body": "hidden"}})
    report = cli.observe(config, root=str(root), project=1)
    assert len(report["log"]) == 1
    assert report["log"][0]["requests"] == 1
    assert "synthetic hidden" not in json.dumps(report)
    assert "owner" not in json.dumps(report) and "project_id" not in json.dumps(report)


def test_doctor_resolves_auto_without_exposing_keys(world):
    root, _, _ = world
    _key(root)
    report = cli.diagnostics({}, root=str(root))
    assert report["jev"]["mode"] == "assist" and report["jev"]["daily_request_budget"] == 500
    assert report["anthropic"]["mode"] == "on"
    assert report["jev"]["scope"] == "all" and report["anthropic"]["scope"] == "all"
    assert "synthetic-secret-key" not in json.dumps(report)
    assert str(root) not in json.dumps(report)


@pytest.mark.parametrize("mutation,error", [({"human_confirmed": False},"human_confirmation_required"),
                                           ({"reason": ""},"bad_reason"),
                                           ({"project_id": 1},"bad_project_id")])
def test_controls_keep_existing_human_boundary(world, mutation, error):
    _, path, _ = world
    with closing(Ledger(str(path))) as db:
        receipt = mcs_requests.apply_command(db, _req("stop", **mutation))
        assert receipt["outcome"] == "rejected" and receipt["error"] == error
        assert not db.artifacts("cloud_control")
        assert db.db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 1
    assert mcs_requests.validate({**_req("stop"), "cmd": "ops.pause", "feature": "semantic"}) == "bad_project_id"


def test_stop_resume_and_idempotent_receipt_share_one_transaction(world):
    root, path, _ = world
    env = _key(root)
    with closing(Ledger(str(path))) as db:
        stop = mcs_requests.apply_command(db, _req("stop"))
        assert stop["outcome"] == "applied"
        assert cloud_policy.global_block_reason(db) == "paused"
        req = _req("resume")
        resume = mcs_requests.apply_command(db, req)
        assert resume["outcome"] == "applied" and cloud_policy.global_block_reason(db) is None
        count = db.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        env.unlink()
        assert mcs_requests.apply_command(db, req) == resume
        assert db.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == count
        rejected = mcs_requests.apply_command(db, _req("resume"))
        assert rejected["error"] == "no_api_key"


@pytest.mark.parametrize("blocked", ["key", "price", "config"])
def test_resume_rechecks_requirements_inside_receipt_writer(world, monkeypatch, blocked):
    root, path, config = world
    _key(root)
    if blocked == "key":
        monkeypatch.setattr(cloud_policy, "api_key", lambda: None)
    elif blocked == "price":
        monkeypatch.setattr(cloud_policy, "pricing_valid", lambda: False)
    else:
        config["cloud_llm"]["model"] = "invalid"
        (root / "config.json").write_text(json.dumps(config))
    with closing(Ledger(str(path))) as db:
        receipt = mcs_requests.apply_command(db, _req("resume"))
        assert receipt["outcome"] == "rejected"
        assert not db.artifacts("cloud_control") and not db.artifacts("cloud_circuit")


def test_control_and_receipt_rollback_together(world, monkeypatch):
    _, path, _ = world
    original = mcs_operations.apply_tx

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic receipt failure")

    monkeypatch.setattr(mcs_operations, "apply_tx", fail_after_write)
    with closing(Ledger(str(path))) as db:
        with pytest.raises(RuntimeError):
            mcs_requests.apply_command(db, _req("stop"))
        assert not db.artifacts("cloud_control")
        assert db.db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 0


def test_cli_commands_use_nested_parser_and_never_touch_network(world, capsys):
    root, _, _ = world
    assert mcs_setup.main(["cloud-llm", "plan"]) == 0
    assert "no_api_key" in capsys.readouterr().out
    assert mcs_setup.main(["cloud-llm", "stop", "--reason", "synthetic"]) == 1
    _key(root)
    assert mcs_setup.main(["cloud-llm", "stop", "--confirm-human", "--reason", "synthetic"]) == 0
    assert '"outcome": "applied"' in capsys.readouterr().out


@pytest.mark.parametrize("obstacle", ["busy", "updating", "code_changed"])
def test_control_cli_refuses_ledger_open_without_current_exclusive_owner(world, monkeypatch, obstacle):
    import ledger
    import writer_lock
    root, _, _ = world
    lock_path = str(root / "data" / "run.lock")
    fd = writer_lock.acquire_run_lock(lock_path) if obstacle == "busy" else None
    if obstacle == "updating":
        (root / "data" / "update_in_progress.marker").write_text("synthetic")
    if obstacle == "code_changed":
        values = iter([1, 2])
        monkeypatch.setattr(writer_lock, "code_stamp", lambda: next(values))
    monkeypatch.setattr(ledger, "Ledger", lambda *a, **k: pytest.fail("rejected control must not open writer"))
    try:
        assert mcs_setup.main(["cloud-llm", "stop", "--confirm-human", "--reason", "synthetic"]) != 0
    finally:
        if fd is not None:
            os.close(fd)
    fd = writer_lock.acquire_run_lock(lock_path)
    assert fd is not None
    os.close(fd)


@pytest.mark.parametrize("fail", [False, True])
def test_control_cli_holds_lock_for_receipt_and_releases_after_exception(world, monkeypatch, fail):
    import writer_lock
    root, _, _ = world
    lock_path = str(root / "data" / "run.lock")
    original = mcs_requests.apply_command

    def apply(ledger, req):
        assert writer_lock.acquire_run_lock(lock_path) is None
        if fail:
            raise ValueError("synthetic control failure")
        return original(ledger, req)

    monkeypatch.setattr(mcs_requests, "apply_command", apply)
    result = mcs_setup.main(["cloud-llm", "stop", "--confirm-human", "--reason", "synthetic"])
    assert (result == 0) is not fail
    fd = writer_lock.acquire_run_lock(lock_path)
    assert fd is not None
    os.close(fd)


def test_init_secret_storage_uses_dotenv_without_printing_secret_or_path(world, monkeypatch, capsys):
    root, _, _ = world
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(root / ".env"))
    monkeypatch.setenv("MCS_ANTHROPIC_API_KEY", "synthetic-incoming-key")
    monkeypatch.delenv("MCS_SETUP_PASSWORD", raising=False)
    assert mcs_setup._init_store_secrets(SimpleNamespace(yes=True), "synthetic") is None
    assert cloud_policy.api_key(str(root)) == "synthetic-incoming-key"
    output = capsys.readouterr().out
    assert "synthetic-incoming-key" not in output and str(root) not in output
    assert cli.pending_key_presence(str(root))["anthropic"]


def test_reader_has_row_and_time_budgets(world, monkeypatch):
    root, _, config = world
    monkeypatch.setattr(cli, "READ_ROWS", 1)
    with pytest.raises(ValueError, match="observation_budget_exceeded"):
        cli.observe(config, root=str(root))


@pytest.mark.parametrize("content", ["{broken", "{}", "[]", '{"mcs_login_id":"synthetic"}',
                                      '{"mcs_login_id":"synthetic","notify_target":"slack:#synthetic","cloud_llm":{"mode":"wrong"}}'])
def test_resume_refuses_unreadable_or_schema_invalid_config_at_mutation_owner(world, content):
    root, path, _ = world
    _key(root)
    (root / "config.json").write_text(content)
    with closing(Ledger(str(path))) as db:
        receipt = mcs_requests.apply_command(db, _req("resume"))
        assert receipt["outcome"] == "rejected" and receipt["error"] == "cloud_config_invalid"
        assert not db.artifacts("cloud_control") and not db.artifacts("cloud_circuit")
        assert db.db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 1
