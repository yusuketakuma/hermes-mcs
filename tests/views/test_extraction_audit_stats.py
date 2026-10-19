"""Extraction audit covers eligible source rows, including missing and failed outputs."""
import json
import sqlite3

import pytest

import export_schema
import extraction_audit as audit
import mcs_stats
import stats_contract
from semantic_projection import PROJECTION_VERSION
from views_testkit import SCHEMA, SNAP_TS, _msg


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def message(db, mid, *, pid=1, state="full", body="本人の薬は合成ボックスで保管しています。"):
    _msg(db, mid, pid=pid, chash=f"h{mid}", state=state, prof="薬剤師")
    db.execute("UPDATE messages SET body_text=? WHERE message_id=?", (body, mid))


def artifact(db, mid, *, kind="extract_llm", content=None, meta=None, pid=1):
    db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) VALUES(?,?,?,?,?,?)",
        (kind, pid, mid, json.dumps(content if content is not None else {}),
         json.dumps(meta if meta is not None else {"hash": f"h{mid}", "extract_version": 7}), SNAP_TS - 10))


def stats(db, **args):
    return mcs_stats.run_stats(db, SNAP_TS, {"preset": "extraction_audit", **args})["stats"]


def test_source_population_reader_priority_and_no_output_reasons(db):
    for mid in range(1, 10):
        message(db, mid, state=None if mid == 1 else "deleted" if mid == 7 else "full")
    db.execute("UPDATE messages SET body_text='' WHERE message_id=8")
    artifact(db, 1)
    artifact(db, 2)
    artifact(db, 2, kind="canonical_projection", content={"canonical_facts": []},
             meta={"hash": "h2", "projection_version": PROJECTION_VERSION})
    artifact(db, 4, meta={"hash": "old-h4"})
    artifact(db, 5, meta={"hash": "h5", "error": "synthetic"})
    artifact(db, 6, pid=99)  # a different patient's output cannot become current
    artifact(db, 9)
    artifact(db, 9, kind="extract_upgrade_v1", content={"state": "held_prior", "hash": "h9",
        "prior_artifact_id": db.execute("SELECT MAX(artifact_id) FROM artifacts").fetchone()[0], "target_version": 8})
    rows = list(audit.iter_audit_rows(db, stats_contract.resolve_scope({"limit": 1}, SNAP_TS)))
    assert len(rows) == 7
    assert [row["reader"] for row in rows] == ["reader_extract", "reader_projection", *["no_output"] * 4, "reader_extract"]
    assert [row["reason"] for row in rows[2:6]] == ["missing", "stale", "failed", "missing"]
    assert rows[-1]["held_prior"] and rows[-1]["quality"] == "attention"
    population = stats(db)["omission_proxy"]["population"]
    assert population == {"cue_posts": 7, "reader_extract": 2, "reader_projection": 1,
        "no_output": 4, "no_output_missing": 2, "no_output_stale": 1, "no_output_failed": 1}


def test_detail_cap_does_not_cut_the_population_or_class_summary(db):
    for mid in range(1, 206):
        message(db, mid)
        if mid % 2:
            artifact(db, mid, content={"patient_context": [{"category": "medication_management",
                "text": "合成ボックスで保管", "evidence": "本人の薬は合成ボックスで保管しています。"}]})
    result = stats(db, limit=1)["omission_proxy"]
    item = result["by_class"]["items"][0]
    assert (item["class"], item["cue_posts"], item["captured_posts"], item["missed_posts"]) == ("storage", 205, 103, 102)
    assert result["rows"]["truncated"] and result["rows"]["returned"] == 1
    assert not result["by_class"]["truncated"]


def test_validation_unknown_is_not_zero_and_retry_conflict_counts_are_scoped(db):
    for mid in (1, 2, 3):
        message(db, mid)
    artifact(db, 1, meta={"hash": "h1", "prefilter": True, "thin_retried": True,
        "integrity": {"validation": {"items_dropped": 2, "evidence_dropped": 1}}})
    artifact(db, 2, meta={"hash": "h2", "integrity": {"validation": {
        "items_dropped": 0, "evidence_dropped": 0}}})
    artifact(db, 3, content={"_items_dropped": True})
    result = stats(db)
    assert result["validation_drops"]["status"] == "partial"
    assert result["validation_drops"]["by_class"]["items"][0] == {"class": "storage",
        "observed_posts": 2, "unknown_posts": 1, "items_dropped": 2, "evidence_dropped": 1}
    assert result["prefilter_cue_conflict"]["by_class"]["items"] == [{"class": "storage", "posts": 1}]
    assert result["thin_residual"]["by_class"]["items"] == [{"class": "storage", "posts": 1}]
    assert list(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))[2]["quality"] == "attention"


def test_projection_category_and_verified_guard(db):
    for mid in (1, 2, 3):
        message(db, mid)
        artifact(db, mid, kind="canonical_projection", content={"canonical_facts": [{
            "kind": "adherence_administration" if mid != 3 else "symptom_state",
            "statement": "合成ボックスで保管", "validation_status": "verified" if mid != 2 else "unverified"}]},
            meta={"hash": f"h{mid}", "projection_version": PROJECTION_VERSION})
    rows = list(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))
    assert [row["classes"]["storage"] for row in rows] == [True, False, False]


@pytest.mark.parametrize("completeness", [None, [], {"requests": {"state": "hint_gaps_resolved", "remaining_hint_candidates": False}},
    {"requests": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 1}}])
def test_corrupt_or_unresolved_completeness_is_attention(db, completeness):
    message(db, 1)
    artifact(db, 1, meta={"hash": "h1", "integrity": {"completeness": completeness}})
    assert next(audit.iter_audit_rows(db, stats_contract.resolve_scope({}, SNAP_TS)))["quality"] == "attention"


def test_aggregate_export_and_read_only_single_source_scan(db, monkeypatch):
    message(db, 1, body="本人の薬は合成ボックスで保管しています。合成秘密ラベル。")
    artifact(db, 1, kind="extract_feedback_v1", content={"field": "patient_context", "note": "合成秘密ラベル"})
    artifact(db, 1, kind="extract_feedback_v1", content={"field": "合成秘密ラベル"})
    original = audit.iter_audit_rows
    calls = []
    def counted(*args):
        calls.append(1)
        yield from original(*args)
    monkeypatch.setattr(audit, "iter_audit_rows", counted)
    db.execute("PRAGMA query_only=ON")
    result = stats(db)
    assert len(calls) == 1
    for name, value in result.items():
        record = {"type": "stat", "contract": "mcs-read-model/1", "snapshot_generation_id": "g",
                  "preset": "extraction_audit", "name": name, "value": value}
        export_schema.validate_record(record)
    text = json.dumps(result, ensure_ascii=False)
    assert text.isascii() and "合成秘密ラベル" not in text
    assert result["feedback_by_field"]["invalid_reports"] == 1
    assert next(r for r in result["feedback_by_field"]["rows"]["items"] if r["field"] == "patient_context")["reports"] == 1
    assert result["attachment_share"]["status"] == "unavailable"


def test_empty_population_and_scope(db):
    assert stats(db)["omission_proxy"]["population"]["cue_posts"] == 0
    message(db, 1, pid=1)
    message(db, 2, pid=2)
    result = stats(db, project=2)["omission_proxy"]
    assert result["population"]["cue_posts"] == 1
    assert result["scope"]["project_id"] == 2


def test_attachment_population_includes_cue_free_posts_and_excludes_withdrawn(db):
    db.execute("CREATE TABLE attachments(message_id INTEGER,state TEXT)")
    message(db, 1)
    message(db, 2, body="合成の挨拶のみです。")
    message(db, 3, pid=2)
    db.executemany("INSERT INTO attachments VALUES (?,?)", [(1, "pending"), (1, "downloaded"),
        (2, "withdrawn"), (3, "failed")])
    result = stats(db, project=1)["attachment_share"]
    assert result["status"] == "ok" and result["population_posts"] == 2
    assert result["rows"]["items"] == [{"reader": "no_output", "length": "len_s", "posts": 2, "with_attachments": 1}]


def test_audit_failure_is_reported_once_without_repeating_the_full_scan(db, monkeypatch):
    calls = []
    def failed(*args):
        calls.append(1)
        raise ValueError("SYNTHETIC_PRIVATE_TEXT")
    monkeypatch.setattr(audit, "collect", failed)
    result = stats(db)
    assert len(calls) == 1
    assert all(value["status"] == "unavailable" for value in result.values())
    assert "SYNTHETIC_PRIVATE_TEXT" not in json.dumps(result)


def test_rule_display_context_cannot_hide_a_raw_llm_omission_or_change_its_digest(db):
    message(db, 1)
    artifact(db, 1, content={"summary": "合成の要約"})
    scope = stats_contract.resolve_scope({}, SNAP_TS)
    before = next(audit.iter_audit_rows(db, scope))
    artifact(db, 1, kind="extract_v1", content={"patient_context": [{
        "category": "medication_management", "text": "合成ボックスで保管しています",
        "evidence": "本人の薬は合成ボックスで保管しています。", "subject": "patient"}]})
    after = next(audit.iter_audit_rows(db, scope))
    assert before["classes"] == after["classes"] == {"storage": False}
    assert before["output_digest"] == after["output_digest"]
    assert "patient_context" not in after["_document"]


def test_cohort_definition_ignores_unrelated_context_or_stat_source_changes(tmp_path, monkeypatch):
    import patient_context
    before = audit.definition_fingerprint()
    changed = tmp_path / "unrelated.py"
    changed.write_text("# unrelated attribute or statistic change\n")
    monkeypatch.setattr(patient_context, "__file__", str(changed))
    monkeypatch.setattr(mcs_stats, "__file__", str(changed))
    assert audit.definition_fingerprint() == before


def test_cohort_definition_binds_the_actual_scope_implementation(monkeypatch):
    before = audit.definition_fingerprint()
    def different_scope(scope, col="m.posted_at_ts"):
        return " AND 0", []
    monkeypatch.setattr(stats_contract, "message_scope_sql", different_scope)
    assert audit.definition_fingerprint() != before


def test_cohort_definition_binds_semantic_category_and_projection_version(monkeypatch):
    import semantic_projection
    before = audit.definition_fingerprint()
    monkeypatch.setattr(audit, "KIND_CATEGORY", {**audit.KIND_CATEGORY, "adherence_administration": "symptom_state"})
    assert audit.definition_fingerprint() != before
    monkeypatch.undo()
    monkeypatch.setattr(semantic_projection, "PROJECTION_VERSION", semantic_projection.PROJECTION_VERSION + 1)
    assert audit.definition_fingerprint() != before


def test_cohort_definition_binds_the_canonical_scope_limit(monkeypatch):
    before = audit.definition_fingerprint()
    monkeypatch.setattr(stats_contract, "DETAIL_LIMIT", stats_contract.DETAIL_LIMIT + 1)
    assert audit.definition_fingerprint() != before
