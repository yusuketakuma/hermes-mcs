"""Direct contracts of semantic_metrics: audit history counting tolerates
malformed rows, and current_quality classifies every scoped message.
Synthetic ledger only."""
import semantic_policy
import json
import sqlite3
from types import SimpleNamespace

from ledger import Ledger
import pytest
import semantic_metrics
from semantic_policy import KIND_AUDIT, KIND_SUMMARY
from semantic_testkit import _cfg, _message, _audit_pair as _pair


@pytest.fixture
def db(tmp_path):
    led = Ledger(str(tmp_path / "ledger.db"))
    try:
        yield led
    finally:
        led.close()


def _raw(value):
    return value if isinstance(value, str) or value is None else json.dumps(value)


def _artifact(db, kind, content, meta, mid=1):
    """Insert one artifact row verbatim (malformed JSON included)."""
    if not db.db.execute("SELECT 1 FROM messages WHERE message_id=?",
                         (mid,)).fetchone():
        db.save_messages([_message(mid)])
    with db.db:
        db.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,meta,"
            "created_at) VALUES(?,?,?,?,?,0)",
            (kind, 1, mid, _raw(content), _raw(meta)))


# --- audit_history ------------------------------------------------------

def test_audit_history_is_empty_without_audits(db):
    _artifact(db, KIND_SUMMARY, {"findings": [{"code": "x"}]},
              {"audit_status": "PASS"})
    assert semantic_metrics.audit_history(db) == {
        "audit_statuses": {}, "repaired_audits": {}, "finding_codes": {}}


def test_audit_history_counts_statuses_repairs_and_finding_codes(db):
    _artifact(db, KIND_AUDIT, {"findings": [{"code": "dose"}, {"code": "dose"}]},
              {"audit_status": "PASS", "repair_count": 1})
    _artifact(db, KIND_AUDIT, {"findings": [{"code": "date"}]},
              {"audit_status": "PASS"})
    _artifact(db, KIND_AUDIT, {"findings": []},
              {"audit_status": "NEEDS_REVIEW", "repair_count": 1})
    _artifact(db, KIND_AUDIT, {}, {"audit_status": "NEEDS_REVIEW",
                                   "repair_count": 2})
    assert semantic_metrics.audit_history(db) == {
        "audit_statuses": {"PASS": 2, "NEEDS_REVIEW": 2},
        "repaired_audits": {"PASS": 1, "NEEDS_REVIEW": 1},
        "finding_codes": {"dose": 2, "date": 1}}


def test_audit_history_tolerates_malformed_rows(db):
    _artifact(db, KIND_AUDIT, "not json", None)
    _artifact(db, KIND_AUDIT, [1, 2], {"audit_status": ""})
    _artifact(db, KIND_AUDIT, {"findings": "dose"}, {"audit_status": 3,
                                                     "repair_count": 1})
    _artifact(db, KIND_AUDIT,
              {"findings": ["dose", {"code": ""}, {"code": 7}, {}, {"code": "ok"}]},
              "[]")
    assert semantic_metrics.audit_history(db) == {
        "audit_statuses": {"unparsed": 4},
        "repaired_audits": {"?": 1},
        "finding_codes": {"ok": 1}}


# --- current_quality ----------------------------------------------------

def test_current_quality_needs_supplied_valid_config(db):
    assert semantic_metrics.current_quality(db, None) == {
        "available": False, "reason": "config_not_supplied"}
    for cfg in ({"semantic": {"mode": "loud"}},
                {"semantic": {"unknown_field": True}}):
        assert semantic_metrics.current_quality(db, cfg) == {
            "available": False, "reason": "config_invalid"}


@pytest.mark.parametrize("cfg", [_cfg("off"), _cfg(summary_mode="off")],
                         ids=["mode_off", "summary_off"])
def test_disabled_summary_reports_no_backlog(db, cfg):
    db.save_messages([_message(1)])
    out = semantic_metrics.current_quality(db, cfg)
    assert out["available"] is False and out["reason"] == "summary_disabled"
    assert out["denominator"] == 1
    assert out["complete"] is None and out["incomplete"] is None
    assert out["completion_rate"] is None and out["pass_rate"] is None


def test_empty_store_has_no_rates(db):
    out = semantic_metrics.current_quality(db, _cfg())
    assert out["available"] is True
    assert (out["total_messages"], out["denominator"], out["complete"],
            out["incomplete"]) == (0, 0, 0, 0)
    assert out["completion_rate"] is None and out["pass_rate"] is None
    assert out["scope"] == "stored_messages_in_configured_projects"
    assert out["completion_basis"] == "terminal_summary_audit_pair"
    scfg, errors = semantic_policy.semantic_config(_cfg())
    assert not errors
    assert out["policy_fingerprint"] == semantic_policy.policy_fingerprint(scfg)
    assert (out["mode"], out["summary_mode"]) == ("shadow", "shadow")


def test_incomplete_reasons_are_classified(db):
    cfg = _cfg()
    # 1: root with a current pair; 2: root without results;
    # 3: reply whose root row is not stored (bundle unbuildable)
    db.save_messages([_message(1), _message(2), _message(3, parent=50)])
    _pair(db, 1, cfg, "NEEDS_REVIEW")
    out = semantic_metrics.current_quality(db, cfg)
    assert (out["denominator"], out["complete"], out["incomplete"]) == (3, 1, 2)
    assert out["incomplete_reasons"] == {"current_pair_missing": 1,
                                         "thread_missing": 1}
    assert out["audit_statuses"] == {"NEEDS_REVIEW": 1}
    # NEEDS_REVIEW completes an audit but is not a PASS
    assert out["completion_rate"] == 1 / 3 and out["pass_rate"] == 0


def test_terminal_status_is_required_for_completion(db):
    cfg = _cfg()
    db.save_messages([_message(1), _message(2)])
    _pair(db, 1, cfg, "PASS")
    _pair(db, 2, cfg, "PENDING")
    out = semantic_metrics.current_quality(db, cfg)
    assert out["complete"] == 1 and out["audit_statuses"] == {"PASS": 1}
    assert out["incomplete_reasons"] == {"current_pair_incomplete": 1}
    assert out["completion_rate"] == out["pass_rate"] == 1 / 2


def test_project_scope_excludes_other_projects(db):
    cfg = _cfg(project_ids=[2])
    db.save_messages([_message(1), _message(2, pid=2), _message(3, pid=2)])
    _pair(db, 2, cfg)
    out = semantic_metrics.current_quality(db, cfg)
    assert (out["total_messages"], out["scoped_messages"],
            out["excluded_messages"]) == (3, 2, 1)
    assert out["complete"] == 1
    assert out["incomplete_reasons"] == {"current_pair_missing": 1}


def test_current_quality_is_read_only(db):
    cfg = _cfg()
    db.save_messages([_message(1), _message(2, parent=1)])
    _pair(db, 1, cfg)
    changes = db.db.total_changes
    out = semantic_metrics.current_quality(db, cfg)
    assert out["complete"] == 1 and out["incomplete"] == 1
    assert db.db.total_changes == changes


def test_audit_findings_loop_can_be_cancelled_without_returning_partial_counts(db):
    _artifact(db, KIND_AUDIT, {"findings": [{"code": "dose"}] * 1000},
              {"audit_status": "PASS"})
    calls = 0

    def deadline():
        nonlocal calls
        calls += 1
        if calls == 20:
            raise TimeoutError("synthetic deadline")

    with pytest.raises(TimeoutError, match="synthetic deadline"):
        semantic_metrics.audit_history(db, checkpoint=deadline)
    assert calls == 20


def test_current_quality_counts_same_members_during_concurrent_arrival(db, tmp_path):
    db.db.execute("PRAGMA journal_mode=WAL")
    db.save_messages([_message(1, parent=99)])
    reader = sqlite3.connect(f"file:{tmp_path / 'ledger.db'}?mode=ro", uri=True)
    reader.row_factory = sqlite3.Row

    class ArrivalBetweenQueries:
        inserted = False

        def execute(self, sql, *args):
            cursor = reader.execute(sql, *args)
            if "FROM messages" in sql and not self.inserted:
                self.inserted = True
                db.save_messages([_message(2, parent=99)])
            return cursor

    try:
        out = semantic_metrics.current_quality(
            SimpleNamespace(db=ArrivalBetweenQueries()), _cfg())
        assert out["denominator"] == out["scoped_messages"] == out["total_messages"] == 1
        assert out["incomplete"] == sum(out["incomplete_reasons"].values()) == 1
        assert db.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    finally:
        reader.close()
