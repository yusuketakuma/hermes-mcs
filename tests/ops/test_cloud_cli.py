"""Local-only cloud estimates and receipt-based global controls with synthetic data."""
import patient_context_plan as context_plan
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import sqlite3
import os
from types import SimpleNamespace
import uuid

import pytest

import anthropic_client
import cloud_cli as cli
import cloud_policy
import extract_llm
import inference_version as inference
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
    # log/compare share observe; only plan/estimate pay for the hold scan.
    assert "source_repair_evidence_holds" not in report
    holds = cli.observe(config, root=str(root), evidence_holds_wanted=True)["source_repair_evidence_holds"]
    assert (holds["unattributable_accounting_rows"], holds["old_reserved_repair_slots"],
            holds["unparseable_repair_slots"]) == (0, 0, 0)
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



@pytest.mark.parametrize("public_version", [None, "0.1", inference.CURRENT_VERSION])
def test_reextract_estimate_tracks_public_generation_even_at_the_same_internal_version(world, public_version):
    root, path, config = world
    with closing(Ledger(str(path))) as db:
        meta = {"hash": _hash(db, 1), "extract_version": extract_llm.EXTRACT_VERSION}
        if public_version is not None:
            meta[inference.VERSION_KEY] = public_version
        db.artifact_add("extract_llm", "{}", project_id=1, message_id=1, meta=meta)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = cli.observe(config, root=str(root))
    assert report["routes"]["reextract"]["targets"] == int(public_version != inference.CURRENT_VERSION)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_reextract_estimate_never_uses_legacy_facts_for_a_changed_source(world):
    root, path, config = world
    with closing(Ledger(str(path))) as db:
        db.save_messages([_message(1, body="different wholly synthetic source", project_id=1)])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert cli.observe(config, root=str(root))["routes"]["reextract"]["targets"] == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


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
    defaults = cli.diagnostics({"mcs_login_id": "synthetic"}, root=str(root))["anthropic"]
    assert defaults["mode"] == "on" and defaults["block_reason"] is None
    assert (defaults["daily_cost_limit_usd"], defaults["monthly_cost_limit_usd"]) == (25, 75)
    uncapped = cli.diagnostics({"mcs_login_id": "synthetic", "cloud_llm": {
        "daily_cost_limit_usd": None, "monthly_cost_limit_usd": None}}, root=str(root))["anthropic"]
    assert (uncapped["mode"], uncapped["status"], uncapped["block_reason"]) == ("off", "local", "cost_limit_required")
    report = cli.diagnostics({"mcs_login_id": "synthetic", "cloud_llm": {"daily_cost_limit_usd": 2}}, root=str(root))
    assert report["jev"]["mode"] == "assist" and report["jev"]["daily_request_budget"] == 10000
    assert report["anthropic"]["mode"] == "on"
    # an unreadable config reaches the CLI as {}: shown off, like the runtime
    assert cli.diagnostics({}, root=str(root))["anthropic"]["mode"] == "off"
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
        monkeypatch.setattr(cloud_policy, "api_key", lambda root=None: None)
    elif blocked == "price":
        monkeypatch.setattr(cloud_policy, "pricing_valid", lambda **kw: False)
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
    if obstacle == "busy":
        monkeypatch.setattr(writer_lock, "wait_run_lock", lambda *a, **k: None)
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


def test_control_cli_waits_boundedly_for_owner_and_rechecks_code(world, monkeypatch):
    import writer_lock
    root, _, _ = world
    acquire = writer_lock.acquire_run_lock
    monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda _: None)
    monkeypatch.setattr(writer_lock, "code_stamp", lambda: 123)
    waits = []

    def wait(path, **options):
        waits.append(options)
        return acquire(path)

    monkeypatch.setattr(writer_lock, "wait_run_lock", wait)
    assert mcs_setup.main(["cloud-llm", "stop", "--confirm-human", "--reason", "synthetic"]) == 0
    assert waits == [{"wait_s": 5.0, "expected_code_stamp": 123,
                      "marker_path": str(root / "data" / "update_in_progress.marker")}]
    fd = acquire(str(root / "data" / "run.lock"))
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


def _complete_context(db, config):
    """Build a completed synthetic context with the original source QA evidence."""
    import patient_context_refresh as context
    import semantic_jev
    import semantic_policy
    import semantic_runtime

    with db.db:
        db.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1 "
                      "WHERE project_id=1")
    contract = context._contract({**config, "cloud_llm": {"mode": "off"}})
    contract["provider"] = extract_llm._cloud_contract()
    policy, errors = semantic_policy.semantic_config(config, key_present=True)
    assert not errors
    assessment = context._quality_assessment(policy)
    snapshot = context._snapshot(db.db, 1, config, hydrate=False, contract=contract)
    assert len(snapshot["sources"]) == 1
    source = snapshot["sources"][0]
    meta = {"source_sig": context_plan.source_signature(source), "contract": contract,
            "provider": contract["provider"], "model": cloud_policy.MODEL}
    span = [0, source["body_length"]]
    unit = context._digest([context_plan.source_key(source), meta["source_sig"], span])
    chunk = {"chunk_span": span, "part": 0, "parts": 1, "facts": []}
    chunk_key = context._digest([unit, 0])
    with db.db:
        context._replace(db, context.CHUNK, 1, chunk,
            {**meta, "unit_key": unit, "chunk_key": chunk_key}, source["message_id"])
        context._replace(db, context.SCAN, 1,
            {"complete": True, "audited": False, "facts": []}, meta, source["message_id"])

    db.db.execute("BEGIN")
    try:
        captured = context._source_quality_capture(db.db, 1, config, context_plan.source_key(source),
                                                    assessment, contract=contract)
    finally:
        db.db.rollback()
    identity = captured["identity"]
    assert not captured["facts"] and len(captured["pages"]) == 1
    page = captured["pages"][0]
    body = {"model": semantic_jev.JEV_MODEL, "state": page["state"], "questions": page["questions"]}
    reserve = semantic_runtime.patient_summary_usage_reserver(db, project_id=1, guard=lambda _stage: None,
        daily_request_budget=3000, identity=identity, purpose="source")
    reservation = reserve(body, 2)
    answers = {fid: {"choice": page["passing_choices"][fid], "confidence": 1.0} for fid in page["fact_ids"]}
    assert list(answers.values()) == [{"choice": "complete", "confidence": 1.0}]
    with db.db:
        db.db.execute("BEGIN IMMEDIATE")
        bindings = semantic_runtime.patient_source_answer_binding(db.db, 1, identity, reservation, page["fact_ids"])
        for fid, answer in answers.items():
            db.artifact_add_tx("patient_context_source_quality_v1", json.dumps(answer), project_id=1,
                meta={inference.VERSION_KEY: inference.CURRENT_VERSION, "input_key": identity["input_key"],
                      "audit_key": identity["audit_key"], "fact_id": fid, **bindings[fid]})
        assert semantic_runtime.patient_summary_resolve_request_tx(
            db, reservation, identity, answers, set(), project_id=1, purpose="source") == 1
        descriptor = semantic_runtime.patient_source_assessment_descriptor(assessment, policy)
        content = semantic_runtime.patient_source_pass_content(db.db, 1, identity, descriptor)
        assert content and content["source_fact_count"] == 0 and content["question_count"] == 1
        db.artifact_add_tx("patient_context_source_quality_pass_v1", json.dumps(content), project_id=1,
            meta={inference.VERSION_KEY: inference.CURRENT_VERSION, **{key: identity[key] for key in
                ("input_key", "candidate_key", "audit_key")}})
        context._replace(db, context.SCAN, 1,
            {"complete": True, "audited": True, "facts": [], "audited_chunks_sig": captured["chunks_sig"],
             "source_quality_audit_key": identity["audit_key"]}, meta, source["message_id"])

    projection = {"groups": {}, "relations": []}
    db.db.execute("BEGIN")
    try:
        summary = context._quality_capture(db.db, 1, config, "synthetic", [], assessment, projection,
                                           contract=contract)
    finally:
        db.db.rollback()
    summary_identity = summary["identity"]
    assert not summary["facts"] and not summary_identity["fact_ids"]
    with db.db:
        db.db.execute("BEGIN IMMEDIATE")
        summary_content = context._quality_pass_content(db.db, 1, summary_identity)
        assert summary_content == {"fact_count": 0, "zero_questions": True}
        db.artifact_add_tx(context.QUALITY_PASS, json.dumps(summary_content), project_id=1,
            meta={inference.VERSION_KEY: inference.CURRENT_VERSION, **{key: summary_identity[key] for key in
                ("input_key", "candidate_key", "audit_key")}})
        context._replace(db, context.KIND, 1, {"facts": [], "summary": [], "temporal_view": projection},
            {"source_sig": snapshot["source_sig"], "contract": contract,
             "provider": contract["provider"], "model": cloud_policy.MODEL,
             "published": context.options(config)["publication"], "summary_checked": True,
             "quality_audit_key": summary_identity["audit_key"],
             "quality_schedule_key": context._digest([config, assessment["code"]]),
             "scan_sig": context._scan_sig(db.db, 1)})
    assert not any(context._pending(db.db, 1, config, contract=contract)[2].values())



@pytest.mark.parametrize("change", ["rule_version", "extraction", "karte"])
def test_current_context_targets_follow_runtime_signatures_without_message_timestamp_change(world, monkeypatch, change):
    root, path, config = world
    with closing(Ledger(str(path))) as db:
        _complete_context(db, config)
        timestamps = tuple(db.db.execute("SELECT first_seen,updated_seen,posted_at_ts FROM messages WHERE message_id=1").fetchone())
    assert cli.observe(config, root=str(root))["routes"]["patient_context"]["targets"] == 0
    if change == "rule_version":
        # a code release alone keeps cloud contexts current; a rule version rebuilds them
        monkeypatch.setattr(extract_llm, "_upgrade_contract", lambda: "f" * 64)
        assert cli.observe(config, root=str(root))["routes"]["patient_context"]["targets"] == 0
        monkeypatch.setattr(extract_llm, "EXTRACT_VERSION", extract_llm.EXTRACT_VERSION + 1)
    else:
        with closing(Ledger(str(path))) as db:
            if change == "extraction":
                db.artifact_add("extract_v1", "{}", project_id=1, message_id=1,
                    meta={inference.VERSION_KEY: inference.CURRENT_VERSION, "hash": _hash(db, 1),
                          "rule_version": extract_llm.extract.RULE_VERSION})
            else:
                db.db.execute("UPDATE patients SET karte_id=10 WHERE project_id=1")
                db.db.commit()
                db.artifact_add("karte_summary", '{"comment":"完全合成の追加背景。"}',
                                project_id=1, meta={"karte_id": 10})
            assert tuple(db.db.execute("SELECT first_seen,updated_seen,posted_at_ts FROM messages WHERE message_id=1").fetchone()) == timestamps
    report = cli.observe(config, root=str(root))
    assert report["routes"]["patient_context"]["targets"] == 1
    assert report["patient_context_basis"] == "runtime_pending_source_contract"
    assert report["recurring_basis"] == "stored_message_timestamp_proxy"


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_current_context_planning_never_reads_an_implicit_installation_key(world, monkeypatch, provider):
    root, _, config = world
    (root / ".env").write_text(("MCS_OPENAI_API_KEY" if provider == "openai" else "MCS_ANTHROPIC_API_KEY") + "=synthetic\n")
    (root / ".env").chmod(0o600)
    original = cloud_policy.provider_key
    def key(name, path=None):
        assert path == str(root), "planning must not inspect a different installation"
        return original(name, path)
    monkeypatch.setattr(cloud_policy, "provider_key", key)
    report = cli.observe(config, root=str(root))
    assert report["routes"]["patient_context"]["targets"] == 1 and report["provider"] == provider


def test_estimate_separates_chunk_repair_and_shadow_without_fabricating_output_usage(world):
    root, path, config = world
    config["cloud_llm"]["mode"] = "shadow"
    with closing(Ledger(str(path))) as db:
        db.save_messages([_message(1, body="合成文。" * 3000, project_id=1,
                                   posted_at="2026-10-26T10:00:00+09:00")])
        db.artifact_add("extract_llm", "{}", project_id=1, message_id=1,
            meta={"hash": _hash(db, 1), "extract_version": extract_llm.EXTRACT_VERSION - 1})
    report = cli.observe(config, root=str(root))
    parts = report["estimate_components"]
    assert parts["current_backlog"]["reextract"]["basic_requests_estimate"] == 1
    assert parts["chunk_repair_retry_additions"]["chunk_requests_current"] > 0
    assert parts["chunk_repair_retry_additions"]["repair_requests_estimate"] is None
    assert parts["chunk_repair_retry_additions"]["retry_requests_estimate"] is None
    assert parts["shadow_additions"]["enabled"]
    assert parts["shadow_additions"]["cloud_work_current"] == report["routes"]
    assert report["usage_calibration"]["status"] == "unmeasured"
    assert report["routes"]["reextract"]["estimated_output_tokens"] is None
    assert report["routes"]["reextract"]["output_token_request_ceiling"] > 0
    assert report["cost_basis"] == "reference_range_not_budget_reservation_or_invoice"


def test_usage_calibration_reports_its_measured_denominator_and_output_mean(world):
    root, path, config = world
    with closing(Ledger(str(path))) as db:
        body = {"model": cloud_policy.MODEL, "system": "s" * 12,
                "messages": [{"role": "user", "content": "c" * 18}], "max_tokens": 1600}
        reserve = cloud_policy.usage_reserver(db, config, route="reextract", project_id=1,
            unit_key="synthetic", message_id=1)(body, 1)
        cloud_policy.record_result(db, reserve, {"model": cloud_policy.MODEL,
            "request_fp": reserve["request_fp"], "usage": {"input_tokens": 60, "output_tokens": 10},
            "http_class": "http_200", "request_id": "req_synthetic", "error_kind": None})
    report = cli.observe(config, root=str(root))
    calibration = report["usage_calibration"]
    assert calibration["status"] == "measured"
    assert calibration["settled_requests_with_characters"] == 1
    assert calibration["input_chars"] == 30 and calibration["input_tokens"] == 60
    assert Decimal(calibration["tokens_per_character"]) == 2
    assert Decimal(calibration["output_tokens_per_request"]) == 10
    for item in report["routes"].values():
        assert item["estimated_input_tokens"] == 2 * item["input_chars"]
        assert item["estimated_output_tokens"] == 10 * item["requests_estimate"]


def test_shadow_rates_keep_failure_and_legacy_unknowns_out_of_measured_denominators(world):
    root, path, config = world
    with closing(Ledger(str(path))) as db:
        for values in ({"version": 2, "units": 1, "local_facts": 3, "cloud_facts": 2,
            "matched": 1, "validated": 2, "validated_units": 1, "rejected": 4,
            "rejected_units": 1, "rejection_observations": 1, "held": 0, "held_observations": 1,
            "repairs": 2, "repaired_units": 1, "repair_observations": 1},
            {"version": 2, "units": 1, "local_facts": 3, "cloud_facts": None,
             "matched": None, "validated": None, "validated_units": 0, "rejected": None,
             "rejected_units": None, "rejection_observations": 0, "held": None,
             "held_observations": 0, "repairs": None, "repaired_units": None, "repair_observations": 0},
            {"local_facts": 2, "cloud_facts": 2, "matched": 2, "held": 0, "repairs": 0}):
            db.artifact_add("cloud_compare_v1", "{}", meta={"route": "reextract", **values})
    report = cli.observe(config, root=str(root))["shadow_comparison"]
    assert report["units"] == 2 and report["unvalidated_units"] == 1
    assert report["legacy_records_without_unit_denominators"] == 1
    assert report["rate_denominators"] == {"rejection": 1, "held": 1, "repair": 1}
    assert report["rejection_rate"] == report["repair_rate"] == 1
    assert report["held_rate"] == 0
    assert report["local_recall"] == 3 / 5
    unknown = cli._comparison_report([])
    assert unknown["rejected"] is unknown["rejection_rate"] is unknown["repair_rate"] is None


def test_unknown_local_grounding_is_not_an_empty_measured_comparison(world):
    root, path, config = world
    with closing(Ledger(str(path))) as db:
        extract_llm._cloud_compare(db, route="patient_context", project_id=1,
                                  local_facts=None, cloud_facts={"synthetic"})
        extract_llm._cloud_compare(db, route="patient_context", project_id=1,
                                  local_facts={"synthetic"}, cloud_facts=None)
    report = cli.observe(config, root=str(root))["shadow_comparison"]
    assert report["units"] == 2 and report["validated_units"] == report["unvalidated_units"] == 1
    assert report["local_facts"] == report["cloud_facts"] == 1
    assert report["matched"] is report["local_recall"] is report["cloud_only"] is None
    assert report["local_recall_fact_denominator"] == 0
    assert report["metric_records_observed"]["matched"] == 0


def test_disposition_receipt_is_auditable_without_double_count_or_identity_leak(world):
    root, path, config = world
    with closing(Ledger(str(path))) as db:
        db.artifact_add("cloud_usage_disposition", "{}", project_id=1, message_id=1,
            meta={"route": "reextract", "disposition": "discarded_scope", "reservation_id": "private-id",
                  "request_id": "private-request", "unit_key": "private-unit"})
    report = cli.observe(config, root=str(root), project=1)
    receipt = next(row for row in report["log"] if row["kind"] == "cloud_usage_disposition")
    assert receipt["disposition"] == "discarded_scope"
    assert report["accounting"]["daily_requests"] == 0
    assert Decimal(report["accounting"]["monthly_cost_usd"]) == 0
    assert "private-" not in json.dumps(receipt)



def _real_interrupt(monkeypatch):
    original = cli.policy.evidence_hold_counts

    def interrupted(db, *, now):
        db.set_progress_handler(lambda: 1, 1)   # SQLite raises SQLITE_INTERRUPT inside the reader TX
        return original(db, now=now)
    monkeypatch.setattr(cli.policy, "evidence_hold_counts", interrupted)


def test_interrupted_hold_scan_is_unknown_and_keeps_the_estimate(world, monkeypatch):
    root, _path, config = world
    _real_interrupt(monkeypatch)
    report = cli.observe(config, root=str(root), evidence_holds_wanted=True)
    assert report["source_repair_evidence_holds"] == {"unknown": "deadline"}
    assert report["routes"]["reextract"]["targets"] == 1


def test_oversized_slot_is_unknown_and_keeps_the_estimate(world):
    root, path, config = world
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES(?,?,?,?,?)",
                   ("patient_context_source_quality_repair_candidate_v1", 1, "x" * (3 * 1024 * 1024), "{}", 1.0))
    report = cli.observe(config, root=str(root), evidence_holds_wanted=True)
    # The reader's 2 MiB value limit stops the scan; the estimate is still printed.
    assert report["source_repair_evidence_holds"] == {"unknown": "value_too_large"}
    assert report["routes"]["reextract"]["targets"] == 1


def test_period_spending_is_unknown_once_usage_inside_it_was_pruned(world):
    root, path, config = world
    config["cloud_llm"].update(window_start="2026-10-26T00:00:00+09:00", window_budget_usd=80)
    assert cli.observe(config, root=str(root))["period_budget"]["spent_or_reserved_usd"] == "0"
    with closing(Ledger(str(path))) as db:
        db.db.execute("INSERT INTO artifacts(kind,content,created_at) VALUES(?,?,?)",
                      (cloud_policy.PRUNE_MARK_KIND, repr(cli.time.time() - 3600), cli.time.time()))
        db.db.commit()
    # a pruned range would make the sum look smaller; it is shown as unknown instead
    assert cli.observe(config, root=str(root))["period_budget"]["spent_or_reserved_usd"] is None


def test_an_expired_period_older_than_the_usage_history_is_unknown_even_without_a_mark(world):
    root, path, config = world
    config["cloud_llm"].update(window_start="2026-08-01T00:00:00+09:00", window_budget_usd=80,
                               until="2026-10-01T00:00:00+09:00")
    assert cli.observe(config, root=str(root))["period_budget"]["spent_or_reserved_usd"] is None
