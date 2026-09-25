"""Current audit coverage excludes history, stale generations and orphan results."""
import json
import time

import pytest

from ledger import Ledger
import semantic
import semantic_observe
from test_mcs_semantic import _cfg, _message


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


def test_observation_skips_invalid_usage_json(tmp_path):
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
        assert snapshot["jev_requests_today"] == 2
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
