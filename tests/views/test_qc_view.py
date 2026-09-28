"""mcs_view `qc` — read-only view over extract_qc annotation artifacts.

Covers: summary counts (evaluated/unevaluated/verdicts/pending), the
flagged list (non-MATCH items, urgency mismatch, unevaluated), and the
per-message detail with staleness marking.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

import pytest

import extract_llm
import ledger
import mcs_adapter
import mcs_view
import semantic_qc
from extract_testkit import _hash, _ledger


def _message(mid, project_id=1):
    return mcs_adapter.Message(
        message_id=mid, project_id=project_id, parent_id=None,
        sender_id=1, sender_name="sender", sender_type="user",
        profession="", organization="",
        posted_at=datetime.now(timezone.utc).isoformat(),
        body_html=f"本文{mid}", body_state="full", is_unread=False,
        reply_count=0)


def _v2(db, mid):
    db.artifact_add(
        "extract_llm", json.dumps({"meds": [], "urgency": "routine"}),
        project_id=1, message_id=mid,
        meta={"hash": _hash(db, mid),
              "extract_version": extract_llm.EXTRACT_VERSION})


def _qc(db, mid, content, chash=None):
    source_id = db.db.execute(
        "SELECT MAX(artifact_id) FROM artifacts WHERE kind='extract_llm' "
        "AND message_id=?", (mid,)).fetchone()[0]
    db.artifact_add(
        "extract_qc", json.dumps(content), project_id=1, message_id=mid,
        model="jev", meta={"hash": chash or _hash(db, mid),
                           "extract_version": extract_llm.EXTRACT_VERSION,
                           "source_artifact_id": source_id,
                           "qc": content.get("qc")})


def _view(db, tmp_path):
    snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"),
                                   str(tmp_path / "snap"))
    return mcs_view.View(snap)


def _seeded(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message(i) for i in (1, 2, 3, 4)])
    for i in (1, 2, 3, 4):
        _v2(db, i)
    return db


def test_summary_and_flagged_list(tmp_path):
    db = _seeded(tmp_path)
    try:
        _qc(db, 1, {"qc": "done", "items": [
            {"section": "meds", "index": 0, "item": {"name": "A"},
             "verdict": "MATCH", "noul": 0.9}]})
        _qc(db, 2, {"qc": "done", "items": [
            {"section": "meds", "index": 0, "item": {"name": "B"},
             "verdict": "MATCH", "noul": 0.9},
            {"section": "symptoms", "index": 0, "item": {"text": "C"},
             "verdict": "NO_MATCH", "noul": 0.1}],
            "urgency": {"extracted": "routine", "jev": "high",
                        "confidence": 0.8}})
        _qc(db, 3, {"qc": "unevaluated", "reason": "invalid_argument"})
        view = _view(db, tmp_path)
        try:
            out = view.read("qc", project=1)
        finally:
            view.close()
        s = out["summary"]
        assert s["total"] == 3 and s["evaluated"] == 2
        assert s["unevaluated"] == 1
        assert s["verdicts"] == {"MATCH": 2, "NO_MATCH": 1}
        assert s["urgency_mismatch"] == 1
        assert s["pending"] == 1          # message 4: v2 done, no QC row
        flagged = {i["message_id"]: i for i in out["items"]}
        assert set(flagged) == {2, 3}     # clean message 1 not listed
        assert flagged[2]["qc_state"] == "done"
        assert len(flagged[2]["flagged_items"]) == 1
        assert flagged[2]["flagged_items"][0]["verdict"] == "NO_MATCH"
        assert flagged[2]["urgency_mismatch"]["jev"] == "high"
        assert flagged[3]["unevaluated_reason"] == "invalid_argument"
    finally:
        db.close()


def test_per_message_detail_marks_stale(tmp_path):
    db = _seeded(tmp_path)
    try:
        _qc(db, 1, {"qc": "done", "items": []}, chash="old-hash")
        _qc(db, 1, {"qc": "done", "items": [
            {"section": "meds", "index": 0, "item": {"name": "A"},
             "verdict": "MATCH", "noul": 0.95}]})
        view = _view(db, tmp_path)
        try:
            out = view.read("qc", project=1, message_id=1)
        finally:
            view.close()
        assert len(out["qc"]) == 2
        by_age = {r["content"]["items"] != []: r for r in out["qc"]}
        assert by_age[True]["current"] is True
        assert by_age[False]["current"] is False
    finally:
        db.close()


def test_stale_qc_excluded_from_summary(tmp_path):
    db = _seeded(tmp_path)
    try:
        _qc(db, 1, {"qc": "done", "items": [
            {"section": "meds", "index": 0, "item": {"name": "A"},
             "verdict": "NO_MATCH", "noul": 0.1}]}, chash="old-hash")
        view = _view(db, tmp_path)
        try:
            out = view.read("qc", project=1)
        finally:
            view.close()
        assert out["summary"]["total"] == 0
        assert out["summary"]["pending"] == 4
        assert out["items"] == []
    finally:
        db.close()


def test_qc_requires_project(tmp_path):
    db = _seeded(tmp_path)
    try:
        view = _view(db, tmp_path)
        try:
            with pytest.raises(ValueError, match="project_required"):
                view.read("qc")
        finally:
            view.close()
    finally:
        db.close()


def test_qc_view_binds_exact_extraction_and_discloses_unchecked_fields(tmp_path):
    db = _seeded(tmp_path)
    try:
        _qc(db, 1, {"qc": "done", "items": [], "coverage": {
            "checked": 0, "total": 1, "unchecked": 1,
            "by_field": {"summary": {"checked": 0, "total": 1, "unchecked": 1}}}})
        view = _view(db, tmp_path)
        try:
            out = view.read("qc", project=1)
            assert out["summary"]["unchecked_items"] == 1
            assert out["items"][0]["coverage"]["by_field"]["summary"]["unchecked"] == 1
        finally:
            view.close()
        _v2(db, 1)  # same body/version, new result identity
        view = _view(db, tmp_path)
        try:
            assert view.read("qc", project=1)["summary"]["pending"] == 4
            detail = view.read("qc", project=1, message_id=1)
            assert not any(r["current"] for r in detail["qc"])
        finally:
            view.close()
    finally:
        db.close()


def test_qc_questions_audit_events_and_vitals_first():
    """The shared item budget goes to the highest-NO_MATCH sections
    first — production measured events/vitals as the dominant
    unsupported-output classes, so they audit ahead of meds/symptoms/
    labs even when those fill the rest of the extraction."""
    ex = {"events": ["visit"] * 8, "vitals": {"hr": 90, "sbp": 120},
          "meds": [{"name": f"薬{i}"} for i in range(10)],
          "symptoms": [{"text": f"症状{i}"} for i in range(10)],
          "labs": [{"name": "採血"}]}
    _, layout, _ = semantic_qc._qc_questions(ex)
    sections = [s for _, s, _ in layout if s != "urgency"]
    assert sections[:10] == ["events"] * 8 + ["vitals", "vitals"]
    assert len(sections) == semantic_qc.QC_MAX_ITEMS
    assert sections.count("meds") == semantic_qc.QC_MAX_ITEMS - 10


def test_qc_view_separates_age_scope_from_unfinished_work(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(semantic_qc.time, "time", lambda: now)
    cutoff = now - semantic_qc.QC_REALTIME_MAX_AGE_S
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(i) for i in (1, 2, 3, 4, 5)])
        for mid, posted in ((1, now - 30 * 86400), (2, now - 61 * 86400), (3, None),
                            (4, cutoff), (5, cutoff - 1)):
            db.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?",
                          (posted, mid))
            _v2(db, mid)
        db.db.commit()
        _qc(db, 5, {"qc": "done", "items": []})
        assert semantic_qc._qc_seed(db, now) == 2
        assert {r[0] for r in db.db.execute(
            "SELECT message_id FROM fetch_jobs WHERE kind='extract_qc'")} == {1, 4}
        view = _view(db, tmp_path)
        try:
            summary = view.read("qc", project=1)["summary"]
            assert summary["pending"] == 2
            assert summary["eligible"] == 2
            assert summary["out_of_scope"] == 3
            assert summary["evaluated"] == 1  # historical annotation remains visible
        finally:
            view.close()
        for mid in (1, 4):
            _qc(db, mid, {"qc": "done", "items": []})
        assert semantic_qc._qc_seed(db, now) == 0
        view = _view(db, tmp_path)
        try:
            summary = view.read("qc", project=1)["summary"]
            assert summary["pending"] == 0
            assert summary["eligible"] == 2 and summary["out_of_scope"] == 3
            assert summary["evaluated"] == 3
        finally:
            view.close()
    finally:
        db.close()
