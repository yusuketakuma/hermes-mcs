"""Current audit coverage excludes history, stale generations and orphan results."""
import json
import time

import pytest

from ledger import Ledger
import semantic
import semantic_observe
from semantic_testkit import _cfg, _message


def _pair(db, mid, cfg, status="PASS"):
    row = db.db.execute("SELECT project_id,parent_id FROM messages WHERE message_id=?",
                        (mid,)).fetchone()
    bundle = semantic.thread_bundle(db, row["project_id"], row["parent_id"] or mid)
    scfg, errors = semantic.semantic_config(cfg)
    assert not errors
    result = {"summary": {"target_message_id": mid, "claims": [],
                          "audit_status": status},
              "findings": [], "repaired": False}
    with db.db:
        semantic._write_result(
            db, row["project_id"], mid, result, bundle["source_fingerprint"],
            {member["message_id"]: member for member in bundle["members"]},
            status, semantic.policy_fingerprint(scfg), scfg["summary_mode"])


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
    from semantic_evaluation import _percentile
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
        assert recent["llm_s_p90"] == _percentile([1.0, 3.0, 2.0, 10.0], 0.90)
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
