"""Canonical-fact coverage in the patient rollup (T5).

The rollup prefers canonical_projection over extract_llm via
current_fact_pred; every verified fact must remain enumerable with
fact_id/evidence — newest generation wins per fact_id.
"""
import json

import pytest

from extract_testkit import _ledger, _message, _hash
import rollup


@pytest.fixture
def db(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.ensure_patient(1)
    yield ledger
    ledger.close()


def _fact(fid, kind, statement, quote="引用"):
    return {"fact_id": fid, "kind": kind, "statement": statement,
            "subject": "patient:1", "evidence_quote": quote,
            "evidence_ids": ["ev"], "importance": "T1",
            "validation_status": "verified"}


def _add(db, mid, kind, content, posted):
    db.save_messages([_message(mid=mid, body="合成本文",
                               posted_at=posted)])
    db.artifact_add(kind, json.dumps(content), project_id=1,
                    message_id=mid, meta={"hash": _hash(db, mid)})


def test_rollup_collects_canonical_facts_newest_first(db):
    _add(db, 1, "canonical_projection",
         {"canonical_facts": [
             _fact("f_old", "allergy_intolerance", "旧アレルギー"),
             _fact("f_same", "vital_lab", "BP 110/70")]},
         "2026-09-18T00:00:00+09:00")
    _add(db, 2, "canonical_projection",
         {"canonical_facts": [
             _fact("f_same", "vital_lab", "BP 130/85", quote="130/85"),
             _fact("f_new", "adverse_drug_event", "嘔気", quote="嘔気")]},
         "2026-09-19T00:00:00+09:00")
    out = rollup.build_rollup(db, 1)
    facts = out["canonical_facts"]
    by_id = {f["fact_id"]: f for f in facts}
    assert set(by_id) == {"f_old", "f_same", "f_new"}
    # newest generation wins per fact_id
    assert by_id["f_same"]["statement"] == "BP 130/85"
    assert by_id["f_same"]["last"].startswith("2026-09-19")
    assert by_id["f_old"]["last"].startswith("2026-09-18")
    # evidence + kind ride along for the readers
    assert by_id["f_new"]["kind"] == "adverse_drug_event"
    assert by_id["f_new"]["evidence"] == "嘔気"


def test_rollup_canonical_shadows_and_stale_falls_back(db):
    """current_fact_pred: a hash-current projection owns the fact
    source; once the revision changes it drops out and the legacy row
    is read instead."""
    _add(db, 1, "extract_llm", {"meds": [{"name": "旧薬"}]},
         "2026-09-18T00:00:00+09:00")
    _add(db, 2, "canonical_projection",
         {"meds": [{"name": "新薬", "action": "stop"}],
          "canonical_facts": [_fact("f1", "preference", "午前希望")]},
         "2026-09-19T00:00:00+09:00")
    out = rollup.build_rollup(db, 1)
    assert [f["fact_id"] for f in out["canonical_facts"]] == ["f1"]

    # body revision -> every stored artifact hash goes stale
    db.db.execute("UPDATE messages SET body_text='改訂本文',"
                  " content_hash='newhash' WHERE message_id=2")
    db.db.commit()
    out = rollup.build_rollup(db, 1)
    assert out.get("canonical_facts", []) == []


def test_rollup_skips_unlisted_canonical_entries(db):
    """Malformed canonical_facts entries never reach the read model."""
    _add(db, 1, "canonical_projection",
         {"canonical_facts": [
             {"statement": "fact_idなし"},          # no fact_id
             "not-a-dict",
             _fact("f1", "vital_lab", "BP 120/80")]},
         "2026-09-18T00:00:00+09:00")
    out = rollup.build_rollup(db, 1)
    assert [f["fact_id"] for f in out["canonical_facts"]] == ["f1"]


@pytest.mark.parametrize("invalid", ["foreign_patient", "deleted"])
def test_rule_rollup_requires_current_patient_source(db, invalid):
    _add(db, 1, "extract_v1", {"vitals": {"temp": 38.1}},
         "2026-09-18T00:00:00+09:00")
    if invalid == "foreign_patient":
        db.db.execute("UPDATE artifacts SET project_id=2")
    else:
        db.db.execute("UPDATE messages SET body_state='deleted'")
    assert "latest_vitals" not in rollup.build_rollup(db, 1)


def test_malformed_lab_container_keeps_other_facts(db):
    _add(db, 1, "extract_llm", {"labs": 7, "summary": "合成要約"},
         "2026-09-18T00:00:00+09:00")
    assert rollup.build_rollup(db, 1)["summary"]["text"] == "合成要約"


@pytest.mark.parametrize(("field", "value"), [
    ("generated_at", "broken"), ("generated_at", float("inf")),
    ("next_med_period_check", "broken"),
])
def test_invalid_rollup_schedule_is_rebuilt(db, field, value):
    _add(db, 1, "extract_v1", {}, "2026-09-18T00:00:00+09:00")
    rollup.rebuild(db, 1)
    row = db.db.execute("SELECT meta FROM artifacts WHERE kind=?",
                        (rollup.KIND,)).fetchone()
    meta = json.loads(row[0])
    meta[field] = value
    db.db.execute("UPDATE artifacts SET meta=? WHERE kind=?",
                  (json.dumps(meta), rollup.KIND))
    assert rollup.dirty_projects(db) == [1]
