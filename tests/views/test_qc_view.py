"""mcs_view `qc` — read-only view over extract_qc annotation artifacts.

Covers: summary counts (evaluated/unevaluated/verdicts/pending), the
flagged list (non-MATCH items, urgency mismatch, unevaluated), and the
per-message detail with staleness marking.
"""
import json
import time

import pytest

import semantic_qc
from extract_testkit import _ledger
from views_testkit import _message, _view
from views_testkit import _qc as _shared_qc, _seeded as _shared_seeded, _v2 as _shared_v2
from inference_version import CURRENT_VERSION, VERSION_KEY


def _mark_current(db, kind, mid=None):
    """Mark the newest shared-fixture rows current without changing the shared fixture."""
    with db.db:
        db.db.execute("UPDATE artifacts SET meta=json_set(meta, ?, ?) WHERE artifact_id IN ("
                      "SELECT MAX(artifact_id) FROM artifacts WHERE kind=? "
                      "AND (? IS NULL OR message_id=?) GROUP BY message_id)",
                      (f"$.{VERSION_KEY}", CURRENT_VERSION, kind, mid, mid))


def _seeded(tmp_path):
    db = _shared_seeded(tmp_path)
    _mark_current(db, "extract_llm")
    return db


def _v2(db, mid):
    _shared_v2(db, mid)
    _mark_current(db, "extract_llm", mid)


def _qc(db, mid, content, chash=None):
    _shared_qc(db, mid, content, chash)
    _mark_current(db, "extract_qc", mid)


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


@pytest.mark.parametrize("invalid_item", ["synthetic invalid item", None, True, 7, []])
def test_qc_view_keeps_malformed_items_unknown(tmp_path, invalid_item):
    db = _seeded(tmp_path)
    try:
        _qc(db, 1, {"qc": "done", "items": [
            {"section": "meds", "index": 0, "verdict": "MATCH"}, invalid_item]})
        before = db.db.execute(
            "SELECT content FROM artifacts WHERE kind='extract_qc'").fetchall()
        view = _view(db, tmp_path)
        try:
            out = view.read("qc", project=1)
            assert out["summary"]["verdicts"] == {"MATCH": 1, "?": 1}
            assert [row["message_id"] for row in out["items"]] == [1]
            assert out["items"][0]["flagged_items"] == []
        finally:
            view.close()
        assert db.db.execute(
            "SELECT content FROM artifacts WHERE kind='extract_qc'").fetchall() == before
    finally:
        db.close()


@pytest.mark.parametrize("invalid_items", [
    "synthetic invalid list", 7, True, {"x": "invalid"}, {}, {"a": "bad", "b": "bad"},
    {"x": {"verdict": "MATCH"}}, {"verdict": "MATCH"}, None])
def test_qc_view_keeps_invalid_item_container_unknown(tmp_path, invalid_items):
    db = _seeded(tmp_path)
    try:
        _qc(db, 1, {"qc": "done", "items": invalid_items})
        view = _view(db, tmp_path)
        try:
            out = view.read("qc", project=1)
            assert out["summary"]["verdicts"] == {"?": 1}
            assert [row["message_id"] for row in out["items"]] == [1]
            assert out["items"][0]["flagged_items"] == []
        finally:
            view.close()
    finally:
        db.close()


def test_qc_view_lists_extraction_reports_without_notes(tmp_path):
    import json
    db = _seeded(tmp_path)
    try:
        aid = db.db.execute("SELECT MAX(artifact_id) FROM artifacts WHERE "
                            "kind='extract_llm' AND message_id=2").fetchone()[0]
        db.artifact_add("extract_feedback_v1", json.dumps({
            "message_id": 2, "artifact_id": aid, "field": "meds",
            "note": "合成メモ", "actor": "discord:1"}),
            project_id=1, message_id=2, meta={})
        view = _view(db, tmp_path)
        try:
            out = view.read("qc", project=1)
        finally:
            view.close()
        assert [(r["message_id"], r["field"], r["current"])
                for r in out["extract_feedback"]] == [(2, "meds", True)]
        assert "合成メモ" not in json.dumps(out, ensure_ascii=False)
    finally:
        db.close()


def test_signals_view_counts_dismissal_reasons(tmp_path):
    import json
    db = _seeded(tmp_path)
    try:
        for code in ("duplicate", "duplicate", None):
            db.artifact_add("signal_v1", json.dumps(
                {"type": "med_change_no_followup", "state": "dismissed",
                 **({"dismiss_reason_code": code} if code else {})}),
                project_id=1, meta={"key": "k"})
        view = _view(db, tmp_path)
        try:
            out = view.signals({})
        finally:
            view.close()
        assert out["dismissals"] == {
            "med_change_no_followup": {"duplicate": 2, "unclassified": 1}}
    finally:
        db.close()


def test_signals_view_counts_a_never_emitted_type_as_unknown(tmp_path):
    import json
    db = _seeded(tmp_path)
    try:
        db.artifact_add("signal_v1", json.dumps({"type": "med_followup", "state": "dismissed"}),
                        project_id=1, meta={"key": "k"})
        view = _view(db, tmp_path)
        try:
            assert view.signals({})["dismissals"] == {"unknown": {"unclassified": 1}}
        finally:
            view.close()
    finally:
        db.close()


@pytest.mark.parametrize("reason", [{"raw": "synthetic note"}, ["duplicate"], "synthetic note"])
def test_signals_view_holds_unknown_dismissal_reason_codes(tmp_path, reason):
    import json
    db = _seeded(tmp_path)
    try:
        db.artifact_add("signal_v1", json.dumps({
            "type": "med_change_no_followup", "state": "dismissed",
            "dismiss_reason_code": reason}), project_id=1, meta={"key": "k"})
        view = _view(db, tmp_path)
        try:
            assert view.signals({})["dismissals"] == {
                "med_change_no_followup": {"unclassified": 1}}
        finally:
            view.close()
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


def test_qc_questions_reserve_each_section():
    """Every section gets an audit opportunity despite duplicate events."""
    ex = {"events": ["visit"] * 8, "vitals": {"hr": 90, "sbp": 120},
          "meds": [{"name": f"薬{i}"} for i in range(10)],
          "symptoms": [{"text": f"症状{i}"} for i in range(10)],
          "labs": [{"name": "採血"}]}
    _, layout, _ = semantic_qc._qc_questions(ex)
    sections = [s for _, s, _ in layout if s != "urgency"]
    assert sections[:5] == ["vitals", "meds", "symptoms", "labs", "events"]
    assert sections.count("events") == 1
    assert len(sections) == semantic_qc.QC_MAX_ITEMS
    assert sections.count("meds") == 6
    assert sections.count("symptoms") == 6


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
        view.meta["generated_at"] = now  # the view scopes at its snapshot generation time
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
        view.meta["generated_at"] = now  # the view scopes at its snapshot generation time
        try:
            summary = view.read("qc", project=1)["summary"]
            assert summary["pending"] == 0
            assert summary["eligible"] == 2 and summary["out_of_scope"] == 3
            assert summary["evaluated"] == 3
        finally:
            view.close()
    finally:
        db.close()


def _mark_fixed(db, mid, qc_fix, providers=None):
    """Synthetic settle marker (and provider record) on the audited extraction."""
    with db.db:
        db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.qc_fix',json(?)) WHERE artifact_id=("
                      "SELECT MAX(artifact_id) FROM artifacts WHERE kind='extract_llm' AND message_id=?)",
                      (json.dumps(qc_fix), mid))
        if providers is not None:
            db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.provider_provenance',json(?)) "
                          "WHERE artifact_id=(SELECT MAX(artifact_id) FROM artifacts "
                          "WHERE kind='extract_llm' AND message_id=?)",
                          (json.dumps({"providers": providers}), mid))


_NO_MATCH = {"qc": "done", "items": [{"section": "symptoms", "index": 0, "item": {"text": "C"},
                                      "verdict": "NO_MATCH", "noul": 0.1}]}


def test_qc_view_marks_confirmation_candidates_after_both_automatic_attempts(tmp_path):
    db = _seeded(tmp_path)
    try:
        _mark_fixed(db, 1, {"qc": 1, "applied": True, "attempt": 2})          # local + cloud, still flagged
        _mark_fixed(db, 2, {"qc": 1, "applied": True})                        # local only so far
        _mark_fixed(db, 3, {"qc": 1, "applied": False}, providers=["anthropic"])  # kept a cloud result
        _mark_fixed(db, 4, {"qc": 1, "applied": False, "attempt": 2})         # only undetermined now
        for mid in (1, 2):
            _qc(db, mid, _NO_MATCH)
        _qc(db, 3, {"qc": "done", "items": [], "urgency": {"extracted": "routine", "jev": "high"}})
        _qc(db, 4, {"qc": "done", "items": [{"section": "meds", "index": 0, "item": {"name": "A"},
                                             "verdict": "UNDETERMINED"}]})
        db.save_messages([_message(5), _message(6)])
        for mid in (5, 6):
            _v2(db, mid)
            _qc(db, mid, _NO_MATCH)
        _mark_fixed(db, 5, {"qc": 1, "applied": True})       # local fix, then a plain cloud send of this source
        with db.db:
            db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at)"
                          " VALUES('cloud_usage_reserve',1,5,'{}','m',?,?)",
                          (json.dumps({"route": "reextract"}), time.time() + 1))
        view = _view(db, tmp_path)
        try:
            out = view.read("qc", project=1)
            only = view.read("qc", project=1, needs_confirmation=True)
            with pytest.raises(ValueError, match="bad_needs_confirmation"):
                view.read("status", needs_confirmation=True)
            with pytest.raises(ValueError, match="bad_needs_confirmation"):
                view.read("qc", project=1, message_id=1, needs_confirmation=True)
        finally:
            view.close()
        rows = {i["message_id"]: (i["qc_attempts"], i["needs_confirmation"], i["followup_state"])
                for i in out["items"]}
        assert rows == {1: (2, True, "needs_confirmation"), 2: (1, False, "cloud_reextract_pending"),
                        3: (1, True, "needs_confirmation"), 4: (2, False, None),
                        5: (1, True, "needs_confirmation"), 6: (0, False, "local_reextract_pending")}
        assert [i["message_id"] for i in only["items"]] == [5, 3, 1]
        assert out["summary"]["needs_confirmation"] == only["summary"]["needs_confirmation"] == 3
        assert out["summary"]["followup"] == {"local_reextract_pending": 1, "cloud_reextract_pending": 1,
                                              "needs_confirmation": 3}
        assert out["legend"]["followup_state"]["cloud_reextract_pending"].startswith("クラウド再抽出待ち")
        assert out["legend"]["needs_confirmation"] == (
            "確認候補: 自動の再抽出（ローカル1回・クラウド1回）の後も、本文に裏付けが見つからない項目"
            "または緊急度の相違が残る抽出。誤りと確定したものではない")
    finally:
        db.close()


def test_qc_view_needs_confirmation_flag_reaches_the_cli(tmp_path, capsys):
    import ledger
    import mcs_view
    db = _seeded(tmp_path)
    try:
        _mark_fixed(db, 1, {"qc": 1, "applied": True, "attempt": 2})
        _qc(db, 1, _NO_MATCH)
        _qc(db, 2, _NO_MATCH)
        snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snap"))
    finally:
        db.close()
    assert mcs_view.main(["--snapshot", snap, "qc", "--project", "1", "--needs-confirmation"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [i["message_id"] for i in out["items"]] == [1] and out["items"][0]["needs_confirmation"] is True
