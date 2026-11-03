"""Current audit coverage excludes history, stale generations and orphan results."""
import json
import sqlite3
import time

import pytest

from inference_version import CURRENT_VERSION, VERSION_KEY
from ledger import Ledger
import semantic
import semantic_observe
from semantic_testkit import _cfg, _message, _audit_pair as _pair


@pytest.mark.parametrize("batch,counted", [(1, True), (2, False), (True, False),
                                           (1.0, False), (None, False), (0, False), ("1", False)])
def test_extract_metrics_do_not_count_shared_batch_measurements_per_artifact(batch, counted):
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE artifacts (artifact_id INTEGER, kind TEXT, meta TEXT)")
        integrity = {"calls": 1, "batch": batch, "timings": {
            "prompt_ms": 2.0, "predicted_ms": 3.0}, "usage": {"total_tokens": 10}}
        db.executemany("INSERT INTO artifacts VALUES (?, 'extract_llm', ?)",
                       [(i, json.dumps({"integrity": integrity})) for i in range(2)])
        observed = semantic_observe._extract_recent(db)
        assert observed["artifacts"] == 2
        expected = {"calls": 2, "prompt_ms": 4.0, "predicted_ms": 6.0, "tokens": 20}
        assert {key: observed[key] for key in expected} == (
            expected if counted else dict.fromkeys(expected))


def test_observation_and_status_share_current_denominator_not_history(tmp_path):
    db_path = str(tmp_path / "ledger.db")
    db = Ledger(db_path)
    cfg = _cfg(project_ids=[1])
    try:
        db.save_messages([_message(1), _message(2), _message(3),
                          _message(4, pid=2), _message(5), _message(99)])
        _pair(db, 1, cfg, "NEEDS_REVIEW")
        _pair(db, 1, cfg)
        _pair(db, 2, cfg, "PENDING")
        _pair(db, 3, cfg, "NEEDS_REVIEW")
        _pair(db, 5, cfg, "NEEDS_REVIEW")
        _pair(db, 99, cfg)
        with db.db:
            db.db.execute("DELETE FROM artifacts WHERE kind='semantic_summary' "
                          "AND message_id=3")
            db.db.execute("DELETE FROM messages WHERE message_id=99")
        changes = db.db.total_changes
        status = semantic.status_report(db, cfg)
        snapshot = semantic_observe.observe(db_path, cfg)
        assert status["current_quality"] == snapshot["current_quality"]
        assert status["history"] == snapshot["history"]
        assert status["audit_statuses_scope"] == snapshot["audit_statuses_scope"] == "history"
        assert sum(status["audit_statuses"].values()) == 6
        quality = status["current_quality"]
        assert quality["total_messages"] == 5 and quality["excluded_messages"] == 1
        assert (quality["denominator"], quality["complete"], quality["incomplete"]) == (4, 2, 2)
        assert quality["audit_statuses"] == {"PASS": 1, "NEEDS_REVIEW": 1}
        assert quality["completion_rate"] == 1 / 2 and quality["pass_rate"] == 1 / 4
        assert db.db.total_changes == changes
    finally:
        db.close()


@pytest.mark.parametrize("change", ["source", "policy", "publication",
                                    "revision", "summary_status"])
def test_changed_generation_or_inconsistent_pair_is_not_complete(tmp_path, change):
    db = Ledger(str(tmp_path / "ledger.db"))
    cfg = _cfg()
    try:
        db.save_messages([_message(1)])
        _pair(db, 1, cfg)
        assert semantic.status_report(db, cfg)["current_quality"]["complete"] == 1
        if change == "source":
            db.save_messages([_message(1, body="合成記録を訂正しました。")])
        elif change == "policy":
            cfg["semantic"]["calibration_version"] = "synthetic-revised"
        elif change == "publication":
            cfg["semantic"]["summary_mode"] = "assist"
        else:
            row = db.db.execute("SELECT artifact_id,meta,content FROM artifacts "
                                "WHERE kind='semantic_summary'").fetchone()
            if change == "revision":
                updated = json.loads(row["meta"])
                updated["target_revision"] = "obsolete"
                column = "meta"
            else:
                updated = json.loads(row["content"])
                updated["audit_status"] = "PENDING"
                column = "content"
            with db.db:
                db.db.execute(f"UPDATE artifacts SET {column}=? WHERE artifact_id=?",
                              (json.dumps(updated), row["artifact_id"]))
        report = semantic.status_report(db, cfg)
        assert report["audit_statuses"] == {"PASS": 1}
        assert report["current_quality"]["complete"] == 0
        assert report["current_quality"]["incomplete"] == 1
    finally:
        db.close()


def test_missing_or_disabled_config_is_not_reported_as_current_quality(tmp_path,
                                                                       monkeypatch):
    db_path = str(tmp_path / "ledger.db")
    db = Ledger(db_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("snapshot functions must not load host configuration")

    monkeypatch.setattr(semantic, "load_config", forbidden)
    try:
        db.save_messages([_message(1)])
        assert semantic.status_report(db)["current_quality"] == {
            "available": False, "reason": "config_not_supplied"}
        assert semantic_observe.observe(db_path)["current_quality"] == {
            "available": False, "reason": "config_not_supplied"}
        quality = semantic.status_report(db, _cfg("off"))["current_quality"]
        assert quality["available"] is False and quality["reason"] == "summary_disabled"
        assert quality["complete"] is None and quality["incomplete"] is None
        assert quality["completion_rate"] is None
        # U07-F06: an enabled supplied config is used as-is — bundling
        # the threads must not fall back to the host config file
        quality = semantic.status_report(db, _cfg())["current_quality"]
        assert quality["available"] is True
        assert quality["denominator"] == 1
        quality = semantic_observe.observe(db_path, _cfg())["current_quality"]
        assert quality["available"] is True
    finally:
        db.close()


def test_observation_excludes_partial_bodies_from_extract_backlog(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.save_messages([_message(1, body="完全な合成本文"),
                          _message(2, body="部分的な合成本文")])
        with db.db:
            db.db.execute("UPDATE messages SET body_state='snippet' "
                          "WHERE message_id=2")

        snapshot = semantic_observe.observe(str(tmp_path / "ledger.db"))
        assert snapshot["extract_llm_left"] == 1
    finally:
        db.close()


def test_observation_extract_backlog_excludes_v4_current_messages(tmp_path):
    """A message with only a current v4 row is not pending for run_pending,
    so the observed backlog and its age must drain to empty too."""
    from semantic_projection import PROJECTION_VERSION
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.save_messages([_message(1, body="合成のv4済み本文")])
        row = db.db.execute("SELECT project_id,content_hash FROM messages "
                            "WHERE message_id=1").fetchone()
        with db.db:
            db.db.execute(
                "INSERT INTO artifacts(kind,project_id,message_id,content,"
                "meta,created_at) VALUES('semantic_facts_v4',?,1,'{}',?,?)",
                (row["project_id"], json.dumps({
                    VERSION_KEY: CURRENT_VERSION, "hash": row["content_hash"], "engine_version": 4,
                    "extract_version": 4,
                    "projection_version": PROJECTION_VERSION}), time.time()))

        snapshot = semantic_observe.observe(str(tmp_path / "ledger.db"))
        assert snapshot["extract_llm_left"] == 0
        assert snapshot["queue_ages_s"]["extract_llm"] is None
    finally:
        db.close()


def test_observation_reports_invalid_usage_as_unknown(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        now = time.time()
        with db.db:
            db.db.execute(
                "INSERT INTO artifacts(kind,content,meta,created_at) "
                "VALUES('semantic_usage','{}','{broken',?)", (now,))
            db.db.execute(
                "INSERT INTO artifacts(kind,content,meta,created_at) "
                "VALUES('semantic_usage','{}',?,?)",
                (json.dumps({"jev_requests": 2}), now))

        snapshot = semantic_observe.observe(str(tmp_path / "ledger.db"))
        assert snapshot["jev_requests_today"] is None
        assert snapshot["jev_usage_error"] == "semantic_usage_invalid"
    finally:
        db.close()


def test_observation_skips_invalid_job_json(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        with db.db:
            db.db.execute(
                "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state) "
                "VALUES('semantic',1,1,'{broken','pending')")

        assert semantic_observe.observe(str(tmp_path / "ledger.db"))[
            "eligible_pending"] == 0
    finally:
        db.close()


def test_recent_drain_window_and_llm_p90(tmp_path):
    from stats_math import percentile
    db_path = str(tmp_path / "ledger.db")
    db = Ledger(db_path)
    try:
        def run(job_llm_s, age_days=0):
            metrics = [{"llm_s": v} for v in job_llm_s] if job_llm_s is not None else None
            rid = db.artifact_add("semantic_drain_run", json.dumps(
                {"v": 1, "done": len(job_llm_s or []), "job_metrics": metrics}))
            with db.db:
                db.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?",
                              (time.time() - age_days * 86400, rid))
        run([1.0, 3.0])
        run([2.0, 10.0], age_days=13)
        run([500.0], age_days=15)  # outside the 14-day window

        recent = semantic_observe.observe(db_path, days=14)["recent_drain"]
        assert recent["runs"] == 2 and recent["done"] == 4
        assert recent["llm_s"] == 16.0
        # pinned to the module's existing interpolating percentile helper
        # (linear interpolation: 3 + (10 - 3) * 0.7 = 7.9; ceil-rank would give 10)
        assert recent["llm_s_p90"] == percentile([1.0, 3.0, 2.0, 10.0], 0.90)
        assert abs(recent["llm_s_p90"] - 7.9) < 1e-9
        assert recent["jev_s"] is None and recent["post_s"] is None
        assert semantic_observe.observe(db_path, days=30)["recent_drain"]["runs"] == 3
        assert semantic_observe.observe(db_path, days=30)["recent_drain"]["llm_s"] == 516.0

        with db.db:
            db.db.execute("DELETE FROM artifacts")
        run(None)
        recent = semantic_observe.observe(db_path)["recent_drain"]
        assert recent["runs"] == 1 and recent["llm_s_p90"] is None
        assert recent["llm_s"] is None

        # mixed window: one run with metrics, one without, one with a
        # non-numeric llm_s -> both gate inputs unknown, never a subset p90
        run([1.0, 3.0])
        recent = semantic_observe.observe(db_path)["recent_drain"]
        assert recent["runs"] == 2 and recent["llm_s"] is None
        assert recent["llm_s_p90"] is None
        with db.db:
            db.db.execute("DELETE FROM artifacts")
        run([1.0, 3.0])
        db.artifact_add("semantic_drain_run", json.dumps(
            {"v": 1, "done": 1, "job_metrics": [{"llm_s": "9"}]}))
        recent = semantic_observe.observe(db_path)["recent_drain"]
        assert recent["runs"] == 2 and recent["llm_s"] is None
        assert recent["llm_s_p90"] is None
    finally:
        db.close()


@pytest.mark.parametrize("argv", [["--days"], ["--days", "2w"], ["--days", "0"]])
def test_main_rejects_bad_days_with_usage(monkeypatch, capsys, argv):
    """A bad --days is a usage error (exit 2), never a traceback or a
    silently empty window that reads as 'no drains'."""
    monkeypatch.setattr(semantic_observe.sys, "argv", ["semantic_observe.py"] + argv)
    assert semantic_observe.main() == 2
    assert "usage" in capsys.readouterr().err


def test_observer_components_share_a_read_view_during_writes(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE witness (value INTEGER)")
    writer.execute("INSERT INTO witness VALUES (1)")
    writer.commit()
    readers = []

    def components(c, cfg, days, *, now, checkpoint, payload_checkpoint, **_):
        readers.append(c)
        assert c.in_transaction
        before = c.execute("SELECT COUNT(*) FROM witness").fetchone()[0]
        writer.execute("INSERT INTO witness VALUES (2)")
        writer.commit()
        after = c.execute("SELECT COUNT(*) FROM witness").fetchone()[0]
        with pytest.raises(sqlite3.OperationalError):
            c.execute("DELETE FROM witness")
        return {"before": before, "after": after}

    monkeypatch.setattr(semantic_observe, "_observe", components)
    try:
        out = semantic_observe.observe(str(path))
        assert out["before"] == out["after"] == 1
        assert writer.execute("SELECT COUNT(*) FROM witness").fetchone()[0] == 2
        assert out["observation"]["consistency"] == "single_read_transaction"
        with pytest.raises(sqlite3.ProgrammingError):
            readers[0].execute("SELECT 1")
    finally:
        writer.close()


def test_observer_interrupts_expensive_sql_and_closes_connection(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    sqlite3.connect(path).close()
    ticks = iter(range(10000))
    monkeypatch.setattr(semantic_observe.time, "monotonic", lambda: next(ticks))
    readers = []

    def expensive(c, cfg, days, *, now, checkpoint, payload_checkpoint, **_):
        readers.append(c)
        c.execute("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL "
                  "SELECT x+1 FROM n WHERE x<100000000) SELECT SUM(x) FROM n").fetchone()
        pytest.fail("query was not interrupted")

    monkeypatch.setattr(semantic_observe, "_observe", expensive)
    out = semantic_observe.observe(str(path), budget_s=3)
    assert out["status"] == "unavailable"
    assert out["reason"] == "observation_deadline_exceeded"
    assert "jobs" not in out
    with pytest.raises(sqlite3.ProgrammingError):
        readers[0].execute("SELECT 1")


def test_missing_observation_database_is_not_created_or_exposed(tmp_path):
    path = tmp_path / "synthetic-private-location.db"
    out = semantic_observe.observe(str(path))
    assert out["status"] == "unavailable"
    assert not path.exists()
    assert str(tmp_path) not in json.dumps(out)


@pytest.mark.parametrize("failure", ["binding", "interface"])
def test_sqlite_error_without_errorcode_stays_unavailable_and_closes_reader(
        tmp_path, monkeypatch, failure):
    path = tmp_path / "synthetic.db"
    sqlite3.connect(path).close()
    readers = []

    def invalid(c, cfg, days, *, now, checkpoint, payload_checkpoint, **_):
        readers.append(c)
        if failure == "binding":
            c.execute("SELECT ?", ({"synthetic": "unsupported binding"},))
        raise sqlite3.InterfaceError("synthetic-private-error")

    monkeypatch.setattr(semantic_observe, "_observe", invalid)
    out = semantic_observe.observe(str(path))
    assert out["status"] == "unavailable"
    assert out["reason"] == "observation_database_unavailable"
    assert "jobs" not in out
    assert "private" not in json.dumps(out)
    with pytest.raises(sqlite3.ProgrammingError):
        readers[0].execute("SELECT 1")


def test_deadline_interrupts_python_metric_loop_before_all_items(tmp_path, monkeypatch):
    path = str(tmp_path / "synthetic.db")
    db = Ledger(path)
    try:
        db.artifact_add("semantic_drain_run", json.dumps({
            "done": 2000, "job_metrics": [{"llm_s": 1, "usage": {
                "input_tokens": 1, "output_tokens": 1,
                "unreported_requests": 0}}] * 2000}))
    finally:
        db.close()
    clock = [0]
    calls = [0]
    original = semantic_observe._nonnegative

    def advance_clock(value, **kw):
        calls[0] += 1
        if calls[0] == 50:
            clock[0] = 2
        return original(value, **kw)

    monkeypatch.setattr(semantic_observe.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(semantic_observe, "_nonnegative", advance_clock)
    out = semantic_observe.observe(path, budget_s=1)
    assert out["reason"] == "observation_deadline_exceeded"
    assert "recent_drain" not in out
    assert 50 <= calls[0] < 100


@pytest.mark.parametrize("size", [semantic_observe.OBSERVE_MAX_CELL + 1,
                                 semantic_observe.OBSERVE_MAX_CELL * 3])
def test_large_stored_json_is_rejected_before_python_parsing(tmp_path, size):
    path = str(tmp_path / "synthetic.db")
    db = Ledger(path)
    try:
        db.artifact_add("semantic_drain_run", json.dumps({"padding": "x" * size}))
    finally:
        db.close()
    out = semantic_observe.observe(path)
    assert out["status"] == "unavailable"
    assert out["reason"] == "observation_size_exceeded"
    assert "padding" not in json.dumps(out)


def test_observation_item_limit_never_returns_a_partial_total(tmp_path, monkeypatch):
    path = str(tmp_path / "synthetic.db")
    db = Ledger(path)
    try:
        db.artifact_add("semantic_drain_run", json.dumps({
            "done": 2000, "job_metrics": [{"llm_s": 1}] * 2000}))
    finally:
        db.close()
    monkeypatch.setattr(semantic_observe, "OBSERVE_MAX_ITEMS", 50)
    out = semantic_observe.observe(path)
    assert out["reason"] == "observation_size_exceeded"
    assert "recent_drain" not in out


@pytest.mark.parametrize("argv", [["--json"], []])
def test_observation_failure_is_not_a_successful_empty_report(monkeypatch, capsys, argv):
    monkeypatch.setattr(semantic_observe.sys, "argv", ["semantic_observe.py", *argv])
    monkeypatch.setattr(semantic_observe, "observe", lambda **kw: {
        "status": "unavailable", "reason": "observation_database_unavailable"})
    assert semantic_observe.main() == 2
    assert "unavailable" in capsys.readouterr().out


@pytest.mark.parametrize("limit", ["deadline", "size"])
def test_quality_over_budget_keeps_other_components(tmp_path, monkeypatch, limit):
    path = str(tmp_path / "synthetic.db")
    db = Ledger(path)
    try:
        db.save_messages([_message(1), _message(2), _message(3)])
    finally:
        db.close()
    import semantic_metrics
    clock = [0]
    real = semantic_metrics.thread_bundle

    def exhausting(*a, **kw):
        if limit == "deadline":
            clock[0] = 100
        else:
            monkeypatch.setattr(semantic_observe, "OBSERVE_MAX_ITEMS", 0)
        return real(*a, **kw)

    monkeypatch.setattr(semantic_observe.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(semantic_metrics, "thread_bundle", exhausting)
    out = semantic_observe.observe(path, cfg=_cfg())
    reason = f"observation_{'deadline' if limit == 'deadline' else 'size'}_exceeded"
    assert out["current_quality"] == {"available": False, "reason": reason}
    assert out["observation"]["status"] == "partial"
    assert out["observation"]["unavailable_components"] == ["current_quality"]
    assert "status" not in out and "jobs" in out and "pipeline_progress" in out


@pytest.mark.parametrize("argv", [["--json"], []])
def test_partial_observation_exits_two_after_printing_parts(tmp_path, monkeypatch,
                                                           capsys, argv):
    path = str(tmp_path / "synthetic.db")
    Ledger(path).close()
    real = semantic_observe.observe
    monkeypatch.setattr(semantic_observe, "load_config", _cfg, raising=False)
    monkeypatch.setattr(semantic_observe.sys, "argv", ["semantic_observe.py", *argv])

    def partial(**kw):
        out = real(path, cfg=_cfg(), days=kw["days"])
        out["current_quality"] = {"available": False,
                                  "reason": "observation_size_exceeded"}
        out["observation"].update(status="partial",
                                  unavailable_components=["current_quality"])
        return out

    monkeypatch.setattr(semantic_observe, "observe", partial)
    assert semantic_observe.main() == 2
    printed = capsys.readouterr().out
    assert "observation_size_exceeded" in printed
    assert "jobs" in printed


def test_quality_finishing_past_the_deadline_is_not_complete(tmp_path, monkeypatch):
    path = str(tmp_path / "synthetic.db")
    db = Ledger(path)
    try:
        db.save_messages([_message(1)])
    finally:
        db.close()
    import semantic_metrics
    clock = [0]
    real = semantic_metrics.current_artifact
    looked_up = []

    def last_lookup(*a, **kw):
        result = real(*a, **kw)
        if a[1] == semantic_metrics.KIND_SUMMARY:
            clock[0] = 100     # the final summary lookup ends after the deadline
            looked_up.append(a[1])
        return result

    monkeypatch.setattr(semantic_observe.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(semantic_metrics, "current_artifact", last_lookup)
    out = semantic_observe.observe(path, cfg=_cfg())
    assert looked_up
    assert out["observation"]["status"] == "partial"
    assert out["current_quality"] == {"available": False,
                                      "reason": "observation_deadline_exceeded"}
