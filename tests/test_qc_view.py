"""mcs_view `qc` — read-only view over extract_qc annotation artifacts.

Covers: summary counts (evaluated/unevaluated/verdicts/pending), the
flagged list (non-MATCH items, urgency mismatch, unevaluated), and the
per-message detail with staleness marking.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "mcs"))

import extract_llm
import ledger
import mcs_adapter
import mcs_view


def _ledger(tmp_path):
    return ledger.Ledger(str(tmp_path / "ledger.db"))


def _message(mid, project_id=1):
    return mcs_adapter.Message(
        message_id=mid, project_id=project_id, parent_id=None,
        sender_id=1, sender_name="sender", sender_type="user",
        profession="", organization="",
        posted_at="2026-09-19T00:00:00+09:00",
        body_html=f"本文{mid}", body_state="full", is_unread=False,
        reply_count=0)


def _hash(db, mid):
    return db.db.execute(
        "SELECT content_hash FROM messages WHERE message_id=?",
        (mid,)).fetchone()[0]


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
            try:
                view.read("qc")
            except ValueError as e:
                assert str(e) == "project_required"
            else:
                raise AssertionError("expected project_required")
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
