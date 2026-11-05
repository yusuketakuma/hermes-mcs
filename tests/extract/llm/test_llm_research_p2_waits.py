"""Resident idle waits follow the earliest future due job (synthetic ledger,
fake clock, every LLM POST path fail-stubbed)."""
import extract_llm_context
import extract_llm_errors
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
import writer_lock
import json
import sqlite3

import pytest

import extract_llm
import cloud_inference
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY


def _seed_job(db, kind, next_try):
    db.db.execute(
        "INSERT INTO fetch_jobs(kind,project_id,message_id,state,next_try,"
        "created_at,updated_at) VALUES(?,1,0,'pending',?,0,0)", (kind, next_try))
    db.db.commit()


def _seed_artifact(db, kind, content=None, meta=None):
    mid = None
    if kind == "extract_llm":
        mid = db.db.execute("SELECT COALESCE(MAX(message_id),0)+1 FROM messages").fetchone()[0]
        db.save_messages([_message(mid=mid, body=f"synthetic retry {mid}")])
        meta = {**(meta or {}), VERSION_KEY: CURRENT_VERSION, "hash": _hash(db, mid),
                "extract_version": extract_llm.EXTRACT_VERSION}
    db.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at)"
        " VALUES(?,1,?,?,?,0)",
        (kind, mid, json.dumps(content) if content is not None else None,
         json.dumps(meta) if meta is not None else None))
    db.db.commit()


WALL0 = 1_000_000.0


def _loop(db, monkeypatch, flags, result=None, *, carry=None, settle=None):
    """Run the resident loop on a fake clock and return the recorded sleeps."""
    mono = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        mono[0] += s
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: mono[0])
    monkeypatch.setattr(extract_llm.time, "time", lambda: WALL0 + mono[0])
    monkeypatch.setattr(extract_llm.time, "sleep", sleep)
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(extract_llm_context, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 123))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 123))
    monkeypatch.setattr(extract_llm_runner, '_WRITER_STATE', extract_llm._WRITER_STATE)
    monkeypatch.setattr(extract_llm_source_contract, '_WRITER_STATE', extract_llm._WRITER_STATE)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(extract_llm, "Ledger", lambda *a: db)
    monkeypatch.setattr(extract_llm_resident, 'Ledger', extract_llm.Ledger)
    monkeypatch.setattr(extract_llm, "_background_semantic",
                        lambda *a, **k: {"errors": [], "done": 0})
    monkeypatch.setattr(extract_llm_resident, '_background_semantic', extract_llm._background_semantic)
    monkeypatch.setattr(extract_llm, "_background_context_refresh",
                        lambda *a, **k: {"status": "complete", "done": 0})
    monkeypatch.setattr(extract_llm_resident, '_background_context_refresh', extract_llm._background_context_refresh)
    base = result or {"done": 0, "failed": 0, "selected": 0, "left": 0}
    monkeypatch.setattr(extract_llm, "run_pending",
                        lambda *a, **k: {**base, "carry": list(carry or [])})
    monkeypatch.setattr(extract_llm_resident, 'run_pending', extract_llm.run_pending)
    monkeypatch.setattr(extract_llm, "_settle_carry", settle or (lambda *a, **k: None))
    monkeypatch.setattr(extract_llm_resident, '_settle_carry', extract_llm._settle_carry)
    monkeypatch.setattr(extract_llm_runner, '_settle_carry', extract_llm._settle_carry)
    monkeypatch.setattr(extract_llm.local_llm, "chat",
                        lambda *a, **k: pytest.fail("LLM POST during idle"))
    monkeypatch.setattr(extract_llm, "_opener_request",
                        lambda *a, **k: pytest.fail("LLM POST during idle"))
    monkeypatch.setattr(extract_llm_errors, '_opener_request', extract_llm._opener_request)
    monkeypatch.setattr(extract_llm_provider, '_opener_request', extract_llm._opener_request)
    monkeypatch.setattr(extract_llm.sys, "argv",
                        ["extract_llm", "--all", *flags, "--stop-after", "400"])
    assert extract_llm.main() == 0
    return sleeps


@pytest.mark.parametrize("kind", ["semantic", "extract_qc"])
def test_semantic_job_due_soon_shortens_the_empty_pass(tmp_path, monkeypatch, kind):
    db = _ledger(tmp_path)
    _seed_job(db, kind, WALL0 + 40)
    sleeps = _loop(db, monkeypatch, ["--lane", "2", "--semantic"])
    assert sleeps[0] == 40 and sleeps[1] == 120   # past due and unserved: fixed poll, no spin


def test_semantic_job_is_ignored_without_the_semantic_flag(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    _seed_job(db, "semantic", WALL0 + 40)
    assert _loop(db, monkeypatch, ["--lane", "2"])[:2] == [120, 120]


def test_claim_lease_rows_never_shorten(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    _seed_job(db, "extract_claim", WALL0 + 40)
    assert _loop(db, monkeypatch, ["--lane", "2", "--semantic"])[0] == 120


@pytest.mark.parametrize("kind", ["PROGRESS", "SCAN"])
def test_patient_context_retry_shortens_lane3_only(tmp_path, monkeypatch, kind):
    import patient_context_refresh
    db = _ledger(tmp_path)
    _seed_artifact(db, getattr(patient_context_refresh, kind), {"next_try": WALL0 + 40, "attempts": 1})
    assert _loop(db, monkeypatch, ["--lane", "3"])[0] == 40
    assert _loop(db, monkeypatch, ["--lane", "2"])[0] == 120


def test_capped_or_malformed_context_rows_do_not_wake(tmp_path, monkeypatch):
    import patient_context_refresh
    db = _ledger(tmp_path)
    _seed_artifact(db, patient_context_refresh.SCAN, {"next_try": WALL0 + 40, "attempts": 5})
    _seed_artifact(db, patient_context_refresh.SCAN, {"next_try": "soon", "attempts": 0})
    assert _loop(db, monkeypatch, ["--lane", "3"])[0] == 120


def test_stale_past_due_error_row_keeps_the_poll(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    _seed_artifact(db, "extract_llm", meta={"error": 1, "attempts": 1, "next_try": WALL0 - 10})
    assert _loop(db, monkeypatch, ["--lane", "2"])[:2] == [120, 120]


def test_future_error_retry_shortens_the_empty_pass(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    meta = {"error": 1, "attempts": 1, "next_try": WALL0 + 40}
    _seed_artifact(db, "extract_llm", meta=meta)
    assert _loop(db, monkeypatch, ["--lane", "2"])[0] == 40
    rows = db.db.execute("SELECT kind, meta FROM artifacts").fetchall()
    expected = {**meta, VERSION_KEY: CURRENT_VERSION, "hash": _hash(db, 1),
                "extract_version": extract_llm.EXTRACT_VERSION}
    assert [(r[0], json.loads(r[1])) for r in rows] == [("extract_llm", expected)]


def test_floor_is_five_seconds(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    _seed_job(db, "semantic", WALL0 + 2)
    assert _loop(db, monkeypatch, ["--lane", "2", "--semantic"])[0] == 5


def test_failure_pass_waits_for_the_nearer_due_job(tmp_path, monkeypatch):
    failed = {"done": 0, "failed": 2, "selected": 2, "left": 2}
    db = _ledger(tmp_path)
    _seed_artifact(db, "extract_llm", meta={"error": 1, "attempts": 1, "next_try": WALL0 + 200})
    _seed_job(db, "semantic", WALL0 + 40)
    assert _loop(db, monkeypatch, ["--lane", "2", "--semantic"], failed)[0] == 40
    (tmp_path / "b").mkdir()
    db = _ledger(tmp_path / "b")
    _seed_artifact(db, "extract_llm", meta={"error": 1, "attempts": 1, "next_try": WALL0 + 200})
    assert _loop(db, monkeypatch, ["--lane", "2", "--semantic"], failed)[0] == 200
    (tmp_path / "c").mkdir()
    db = _ledger(tmp_path / "c")
    _seed_artifact(db, "extract_llm", meta={"error": 1, "attempts": 1, "next_try": WALL0 + 900})
    assert _loop(db, monkeypatch, ["--lane", "2", "--semantic"], failed)[0] == 300


def test_failure_pass_without_retriable_row_uses_due_jobs(tmp_path, monkeypatch):
    import patient_context_refresh
    db = _ledger(tmp_path)
    _seed_artifact(db, patient_context_refresh.SCAN, {"next_try": WALL0 + 40, "attempts": 1})
    failed = {"done": 0, "failed": 2, "selected": 2, "left": 2}
    assert _loop(db, monkeypatch, ["--lane", "3"], failed)[0] == 40


def test_carried_result_waits_only_the_floor(tmp_path, monkeypatch, capsys):
    db = _ledger(tmp_path)
    carry = [("row", WALL0 + 900, lambda: None)]
    sleeps = _loop(db, monkeypatch, ["--lane", "2"], carry=carry)
    assert sleeps[0] == 5
    line = next(json.loads(ln) for ln in capsys.readouterr().out.splitlines() if "idle_s" in ln)
    assert line["carried"] == 1


def test_gated_pauses_are_unchanged_with_a_runnable_job(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    _seed_job(db, "semantic", WALL0 + 40)
    flags = ["--lane", "2", "--semantic"]
    gated = {"done": 0, "failed": 0, "selected": 3, "left": 3}
    assert _loop(db, monkeypatch, flags, {**gated, "circuit_open_s": 600})[:2] == [120, 120]
    assert _loop(db, monkeypatch, flags, {**gated, "circuit_open_s": 40})[:2] == [40, 40]
    assert _loop(db, monkeypatch, flags, {**gated, "disk_low": True})[:2] == [120, 120]
    assert _loop(db, monkeypatch, flags, {**gated, "deferred": 1})[:2] == [30, 30]


def test_idle_line_is_numeric_only(tmp_path, monkeypatch, capsys):
    db = _ledger(tmp_path)
    _seed_job(db, "semantic", WALL0 + 40)
    _loop(db, monkeypatch, ["--lane", "2", "--semantic"])
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if "idle_s" in ln]
    assert lines
    for line in lines:
        assert set(line) == {"idle_s", "due_in_s", "carried", "ts"}
        assert all(v is None or type(v) in (int, float) for v in line.values())


def test_next_due_in_survives_a_locked_database():
    class DB:
        def execute(self, *a):
            raise sqlite3.OperationalError("database is locked")
    led = type("L", (), {"db": DB()})()
    assert extract_llm._next_due_in(led, WALL0, semantic=True, context=True) is None


def test_next_due_in_excludes_past_and_picks_earliest(tmp_path):
    import patient_context_refresh
    db = _ledger(tmp_path)
    _seed_artifact(db, "extract_llm", meta={"error": 1, "attempts": 1, "next_try": WALL0 - 5})
    _seed_job(db, "semantic", WALL0 + 70)
    _seed_artifact(db, patient_context_refresh.PROGRESS, {"next_try": WALL0 + 30, "attempts": 0})
    assert extract_llm._next_due_in(db, WALL0, semantic=True, context=True) == 30
    assert extract_llm._next_due_in(db, WALL0, semantic=True, context=False) == 70
    assert extract_llm._next_due_in(db, WALL0 + 100, semantic=True, context=True) is None


def test_stale_past_due_row_does_not_hide_a_later_retry(tmp_path, monkeypatch):
    # unconditional MIN(next_try) returned the stale row, the caller dropped
    # it as past, and the worker fell back to the fixed 120 s poll
    db = _ledger(tmp_path)
    _seed_artifact(db, "extract_llm", meta={"error": 1, "attempts": 1, "next_try": WALL0 - 5})
    _seed_artifact(db, "extract_llm", meta={"error": 1, "attempts": 1, "next_try": WALL0 + 40})
    assert extract_llm._next_due_in(db, WALL0, semantic=False, context=False) == 40
    assert extract_llm._next_retry(db) == WALL0 - 5   # default keeps the old contract
    assert _loop(db, monkeypatch, ["--lane", "2"])[0] == 40
