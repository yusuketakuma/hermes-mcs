"""Synthetic acceptance of the receipted patient-context reset: erase, carry spending over, rebuild."""
import copy
from datetime import datetime
from decimal import Decimal
import json
import os
from types import SimpleNamespace

import pytest

import cloud_policy
import patient_context_quota
import patient_context_refresh_readiness as readiness
import patient_context_refresh_state as state
import patient_context_reset as reset
import semantic_policy
import semantic_store
from test_context_cloud_update_identity import cloud, finish, real_cloud, runtime  # noqa: F401  (fixtures)

BACKUP = {"name": "patient-context-reset-synthetic.db", "bytes": 7, "sha256": "a" * 64}


def _disabled(cfg):
    out = copy.deepcopy(cfg)
    out.setdefault("extract", {}).setdefault("patient_context", {})["enabled"] = False
    return out


def _request(preview_hash, **kw):
    return {"version": 1, "cmd": reset.COMMAND, "command_id": "00000000-0000-4000-8000-000000000001",
            "actor": "synthetic-human", "human_confirmed": True, "project_id": None,
            "reason": "synthetic rebuild", "preview_hash": preview_hash, "backup": BACKUP, **kw}


def _apply(ledger, cfg, req, now, tmp_path):
    with ledger.db:
        ledger.db.execute("BEGIN IMMEDIATE")
        return reset.apply_tx(ledger.db, req, now, cfg=cfg, backup_dir=tmp_path)


def _copy_to_other_route(ledger):
    """One settled reextract pair, cloned from a real patient-context pair, that the reset must keep."""
    reserve = ledger.artifacts("cloud_usage_reserve")[0]
    rid = json.loads(reserve["meta"])["reservation_id"]
    result = next(row for row in ledger.artifacts("cloud_usage_result")
                  if json.loads(row["meta"])["reservation_id"] == rid)
    with ledger.db:
        for row in (reserve, result):
            meta = {**json.loads(row["meta"]), "route": "reextract", "reservation_id": "f" * 32}
            ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                              "VALUES(?,?,?,?,?,?,?)", (row["kind"], row["project_id"], row["message_id"],
                                                         row["content"], row["model"], json.dumps(meta),
                                                         row["created_at"]))


def _usage(ledger):
    return {period: cloud_policy.usage_report(SimpleNamespace(db=ledger.db), period=period)
            for period in ("day", "month")}


@pytest.fixture
def built(runtime, real_cloud, tmp_path):  # noqa: F811
    ledger, clock, _ = runtime
    cfg, posts = real_cloud
    finish(runtime, cfg)
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg) is not None
    backup = tmp_path / BACKUP["name"]
    backup.write_bytes(b"backup!")
    os.utime(backup, (clock.wall, clock.wall))   # taken just now, on the synthetic clock
    return ledger, clock, cfg, posts


def test_reset_refuses_until_generation_is_disabled_and_cloud_is_paused(built, tmp_path):
    ledger, clock, cfg, _ = built
    assert reset.preview(ledger.db, cfg=cfg, now=clock.wall)["refusal"] == "patient_context_enabled"
    assert reset.preview(ledger.db, cfg=_disabled(cfg), now=clock.wall)["refusal"] == "cloud_not_paused"
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=_disabled(cfg), now=clock.wall)
    assert found["refusal"] is None and found["reservations"] > 0
    assert _apply(ledger, _disabled(cfg), _request("b" * 64), clock.wall, tmp_path)[0] == "preview_changed"
    missing = _request(found["preview_hash"], backup={**BACKUP, "name": "absent.db"})
    assert _apply(ledger, _disabled(cfg), missing, clock.wall, tmp_path)[0] == "backup_missing"
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg) is not None, "a refused reset changes nothing"


def test_reset_erases_results_and_accounting_but_keeps_spending(built, tmp_path):
    ledger, clock, cfg, _ = built
    off = _disabled(cfg)
    _copy_to_other_route(ledger)
    cloud_policy.stop(ledger)
    before = _usage(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    error, extra = _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)
    assert error is None and extra["erased"]["accounting_rows"] >= 2 and extra["carryover"]
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg) is None, "published contexts are hidden at once"
    for kind in reset.ERASED_KINDS:
        assert not ledger.artifacts(kind), kind
    routes = {json.loads(row["meta"])["route"] for row in ledger.artifacts("cloud_usage_reserve")}
    assert routes == {"reextract"}
    assert _usage(ledger) == before, "every limit keeps counting what was already spent"


def test_context_rebuilds_after_reset(built, tmp_path):
    ledger, clock, cfg, posts = built
    sent = len(posts)
    cloud_policy.stop(ledger)
    off = _disabled(cfg)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] is None
    posts.clear()   # the synthetic wire counts reservations against its own sends since the reset
    cloud_policy.resume(ledger)
    finish(clock_runtime(ledger, clock), cfg)
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg) is not None
    assert len(posts) == sent, "the rebuild sends the same work again"


def clock_runtime(ledger, clock):
    return ledger, clock, []


def _reserves(ledger):
    return [row["artifact_id"] for row in ledger.artifacts("cloud_usage_reserve")]


def test_reset_keeps_a_period_budget_that_starts_mid_day(built, tmp_path):
    """A period start between two reservations of one day keeps its count: the carry-over is
    split at that start, so neither the earlier spending joins it nor the later leaves it."""
    ledger, clock, cfg, _ = built
    first = min(_reserves(ledger))
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (clock.wall - 3 * 3600, first))
    start = datetime.fromtimestamp(clock.wall - 3600, cloud_policy.JST).isoformat()
    off = _disabled(cfg)
    off["cloud_llm"] = {**off["cloud_llm"], "window_start": start, "window_budget_usd": 50}
    cloud_policy.stop(ledger)
    usage = SimpleNamespace(db=ledger.db)
    window = cloud_policy.window_spend(usage, off)
    whole_day = copy.deepcopy(off)
    whole_day["cloud_llm"]["window_start"] = datetime.fromtimestamp(clock.wall - 4 * 3600, cloud_policy.JST).isoformat()
    assert 0 < window < cloud_policy.window_spend(usage, whole_day), "the earlier reservation is outside the period"
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert sum(entry["requests"] for entry in found["carryover"]) == found["reservations"]
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] is None
    assert cloud_policy.window_spend(usage, off) == window, "budget_block_reason compares this same total"


def test_a_changed_spending_total_rolls_the_whole_reset_back(built, tmp_path, monkeypatch):
    ledger, clock, cfg, _ = built
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    reserves = _reserves(ledger)
    real, seen = reset._spend, []

    def drifting(db, cfg):
        seen.append(real(db, cfg))
        return seen[-1] if len(seen) == 1 else {**seen[-1], "day": None}

    monkeypatch.setattr(reset, "_spend", drifting)
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path) == ("spend_changed", {})
    assert _reserves(ledger) == reserves and not ledger.artifacts(cloud_policy.CARRYOVER_KIND)
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg) is not None


def _quality_usage(ledger, created_at, requests):
    meta = {"operation": "patient_summary_quality", "jev_requests": requests, "quality_result": "pass"}
    with ledger.db:
        ledger.db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
                          (semantic_policy.KIND_USAGE, "", "", json.dumps(meta), created_at))


def test_todays_quality_jev_requests_keep_counting_after_the_reset(built, tmp_path):
    ledger, clock, cfg, _ = built
    existing = len(reset._quality_usage(ledger.db))
    _quality_usage(ledger, clock.wall - 1200, 2)
    _quality_usage(ledger, clock.wall - 2 * 86400, 5)
    before = semantic_store.jev_usage_today(ledger)
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert found["quality_usage_rows"] == existing + 2
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] is None
    vars(ledger).pop("_jev_usage_cache", None)
    assert semantic_store.jev_usage_today(ledger) == before
    operations = [json.loads(row["meta"]).get("operation") for row in ledger.artifacts(semantic_policy.KIND_USAGE)]
    assert operations.count(reset.CARRIED_JEV_OPERATION) >= 1
    assert not set(operations) & set(reset.QUALITY_OPERATIONS)


def _claim(ledger, now):
    with ledger.db:
        ledger.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,next_try,created_at,updated_at) "
                          "VALUES(?,?,?,?,?,?)", (state.QUALITY_CLAIM, 1, 0, now + 60, now, now))


def _generating(ledger, now):
    with ledger.db:
        ledger.db.execute("INSERT INTO artifacts(kind,project_id,content,model,meta,created_at) VALUES(?,?,?,?,?,?)",
                          (patient_context_quota.KIND, 1, json.dumps({"state": "started", "lease_until": now + 60}),
                           "", "{}", now))


def _sending(ledger, now):
    reserve = ledger.artifacts("cloud_usage_reserve")[0]
    meta = {**json.loads(reserve["meta"]), "reservation_id": "e" * 32}
    with ledger.db:
        ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                          "VALUES(?,?,?,?,?,?,?)", (reserve["kind"], reserve["project_id"], reserve["message_id"],
                                                    reserve["content"], reserve["model"], json.dumps(meta), now))


@pytest.mark.parametrize("busy, refusal", [(_claim, "quality_claim_active"), (_generating, "generation_active"),
                                           (_sending, "cloud_in_flight")])
def test_reset_refuses_while_work_is_running(built, tmp_path, busy, refusal):
    ledger, clock, cfg, _ = built
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    busy(ledger, clock.wall)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert found["refusal"] == refusal
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] == refusal


@pytest.mark.parametrize("age, error", [(reset.BACKUP_MAX_AGE_S + 1, "backup_missing"),
                                        (-reset.BACKUP_CLOCK_SKEW_S - 1, "backup_missing"),
                                        (reset.BACKUP_MAX_AGE_S - 60, None)])
def test_backup_must_be_this_resets_recent_copy(built, tmp_path, age, error):
    ledger, clock, cfg, _ = built
    assert reset.validate_operation(_request("c" * 64, backup={**BACKUP, "name": "other.db"}),
                                    set(_request("c" * 64)) - {"reason", "preview_hash", "backup"}) == "bad_backup"
    os.utime(tmp_path / BACKUP["name"], (clock.wall - age, clock.wall - age))
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] == error


def test_results_whose_reservation_is_gone_are_erased_too(built, tmp_path):
    ledger, clock, cfg, _ = built
    result = ledger.artifacts("cloud_usage_result")[0]
    meta = {**json.loads(result["meta"]), "reservation_id": "d" * 32}
    with ledger.db:
        ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                          "VALUES(?,?,?,?,?,?,?)", (result["kind"], result["project_id"], result["message_id"],
                                                    result["content"], result["model"], json.dumps(meta),
                                                    clock.wall - 70 * 86400))
    off = _disabled(cfg)
    cloud_policy.stop(ledger)
    found = reset.preview(ledger.db, cfg=off, now=clock.wall)
    assert found["refusal"] is None
    assert _apply(ledger, off, _request(found["preview_hash"]), clock.wall, tmp_path)[0] is None
    assert not ledger.artifacts("cloud_usage_result")


def test_amounts_compare_as_numbers():
    assert reset._amounts({"a_usd": "1.20", "providers": {"x": {"cost_usd": "0"}}, "n": 2}) == \
        {"a_usd": Decimal("1.2"), "providers": {"x": {"cost_usd": Decimal("0.0")}}, "n": 2}


@pytest.fixture
def cli(built, tmp_path, monkeypatch):
    """The cloud-llm CLI over a copy of the built ledger in a synthetic home."""
    import argparse
    import sqlite3
    import cloud_cli
    import maintenance
    import mcs_util
    ledger = built[0]
    root = tmp_path / "synthetic-root"
    (root / "data").mkdir(parents=True)
    with sqlite3.connect(str(root / "data" / "ledger.db")) as target:
        ledger.db.backup(target)
    monkeypatch.setattr(mcs_util, "HOME", str(root))
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(root / "data" / "backups"))
    monkeypatch.setattr(cloud_cli, "_context_reset_preview", lambda _cfg: {"refusal": None, "preview_hash": "d" * 64})
    parser = argparse.ArgumentParser()
    cloud_cli.add_parsers(parser.add_subparsers(dest="command"))

    def run(*extra):
        args = parser.parse_args(["cloud-llm", "context-reset", "--preview-hash", "d" * 64, "--confirm-human",
                                  "--reason", "synthetic rebuild", *extra])
        return args.fn(args)
    return run, root


def _backups(root):
    folder = root / "data" / "backups"
    return sorted(path.name for path in folder.iterdir()) if folder.exists() else []


@pytest.mark.parametrize("stored, rc, shown", [
    ({"outcome": "applied", "backup": {"name": "earlier"}, "erased": {}}, 0, {"backup": {"name": "earlier"}}),
    ({"outcome": "applied", "control": "paused"}, 1, {"error": "command_id_conflict"}),
])
def test_cli_replays_a_known_command_id_without_a_new_backup(cli, capsys, stored, rc, shown):
    import sqlite3
    run, root = cli
    command_id = "00000000-0000-4000-8000-000000000002"
    with sqlite3.connect(str(root / "data" / "ledger.db")) as db:
        db.execute("INSERT INTO command_receipts(command_id,payload_hash,outcome,receipt_json,processed_at) "
                   "VALUES(?,?,?,?,?)", (command_id, "f" * 64, "applied",
                                         json.dumps({"command_id": command_id, **stored}), 1.0))
    assert run("--command-id", command_id) == rc
    printed = json.loads(capsys.readouterr().out)
    assert {key: printed[key] for key in shown} == shown
    assert _backups(root) == []


def test_cli_removes_the_backup_of_a_rejected_reset(cli, monkeypatch, capsys):
    import mcs_requests
    run, root = cli
    monkeypatch.setattr(mcs_requests, "apply_command", lambda _ledger, req: {
        "command_id": req["command_id"], "outcome": "rejected", "error": "preview_changed"})
    assert run() == 1
    assert json.loads(capsys.readouterr().out)["error"] == "preview_changed"
    assert _backups(root) == []


def test_cli_keeps_the_backup_of_an_applied_reset(cli, monkeypatch, capsys):
    import mcs_requests
    run, root = cli
    monkeypatch.setattr(mcs_requests, "apply_command", lambda _ledger, req: {
        "command_id": req["command_id"], "outcome": "applied", "backup": req["backup"], "erased": {}})
    assert run() == 0
    printed = json.loads(capsys.readouterr().out)
    assert _backups(root) == [printed["backup"]["name"]]


def test_cli_reports_a_reader_limit_apart_from_refusals(built, monkeypatch, tmp_path):
    import sqlite3
    import cloud_cli
    import mcs_util
    ledger = built[0]
    (tmp_path / "home" / "data").mkdir(parents=True)
    with sqlite3.connect(str(tmp_path / "home" / "data" / "ledger.db")) as target:
        ledger.db.backup(target)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cloud_cli, "RESET_READ_ROWS", 3)
    found = cloud_cli._context_reset_preview(_disabled(built[2]))
    assert found == {"refusal": "read_incomplete", "stop_reason": "reset_budget_exceeded", "preview_hash": None}
