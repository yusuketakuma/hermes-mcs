"""Old canonical rollup values cannot bypass the current projection gate."""
import json
import time

import pytest

import brain_export
import notify_views
import rollup
import semantic
import semantic_projection as projection
import semantic_v4 as v4
from semantic_testkit import _PassJev, _canonical_cfg, _llm_v2, _seeded_two


def _published(tmp_path):
    db = _seeded_two(tmp_path)

    def model(prompt):
        result = json.loads(_llm_v2(prompt))
        for fact in result.get("facts", []):
            fact.update(epistemic="reported", quantity="5mg" if "アムロジピン" in fact["statement"] else "500mg")
        return json.dumps(result, ensure_ascii=False)

    out = semantic.run_due(db, _canonical_cfg(), {"errors": []}, time.monotonic() + 480,
                           jev_client=_PassJev(), llm_fn=model)
    assert out["done"] == 1
    rollup.rebuild(db, 1)
    return db


def _cached(db):
    row = db.db.execute("SELECT content,meta FROM artifacts WHERE kind='patient_rollup' "
                        "AND project_id=1").fetchone()
    return json.loads(row["content"]), row["meta"]


def test_old_projection_cache_is_safe_for_readers_and_recovers_once(tmp_path, monkeypatch):
    db = _published(tmp_path)
    try:
        cached, meta = _cached(db)
        assert any(med.get("dose") == "5mg" for med in cached["medications"])
        cached["medications"] = [{"name": "アムロジピン", "dose": "50mg"}]
        cached["profile_extension"] = {"display": "synthetic profile kept"}
        with db.db:
            db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.projection_version',3) "
                          "WHERE kind IN ('canonical_projection','semantic_facts_v4')")
            for row in db.db.execute("SELECT artifact_id,content FROM artifacts WHERE message_id=1 "
                                     "AND kind IN ('canonical_projection','semantic_facts_v4')").fetchall():
                old = json.loads(row["content"])
                for med in old.get("meds", []):
                    if med.get("dose") == "5mg":
                        med["dose"] = "50mg"
                db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                              (json.dumps(old), row["artifact_id"]))
            db.db.execute("UPDATE artifacts SET content=?,meta=json_remove(meta,'$.projection_version') "
                          "WHERE kind='patient_rollup'", (json.dumps(cached),))
        cached, meta = _cached(db)
        assert 1 in rollup.dirty_projects(db)
        changes = db.db.total_changes
        original = rollup.build_rollup
        calls = []

        def counted(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(rollup, "build_rollup", counted)
        safe = rollup.current_cached_refs(db.db, 1, cached, meta)
        assert calls == [1]
        assert safe["profile_extension"] == cached["profile_extension"]
        assert safe["msg_count"] == cached["msg_count"]
        assert not any(med.get("dose") == "50mg" for med in safe.get("medications", []))
        assert db.db.total_changes == changes
        assert "50mg" not in notify_views.patient_summary_text(db.db, 1, cfg=_canonical_cfg())[1]
        # Same filter and renderer used by brain_export.run, without external output.
        exported = brain_export._patient_md(1, "架空太郎", {},
                                            rollup.current_cached_refs(db.db, 1, cached, meta))
        assert "50mg" not in exported
        before = db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind IN "
                               "('canonical_projection','semantic_facts_v4')").fetchone()[0]
        assert v4.reproject_stale(db, semantic.semantic_config(_canonical_cfg())[0])["reprojected"] == 4
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind IN "
                             "('canonical_projection','semantic_facts_v4')").fetchone()[0] == before + 4
        rollup.rebuild(db, 1)
        updated, meta = _cached(db)
        assert json.loads(meta)["projection_version"] == projection.PROJECTION_VERSION
        assert rollup.dirty_projects(db) == []
        assert any(med.get("dose") == "5mg" for med in updated["medications"])
        monkeypatch.setattr(rollup, "build_rollup", lambda *_args, **_kwargs: pytest.fail("fresh cache rebuilt"))
        outputs, counts = [], []
        for _ in range(2):
            queries = []
            db.db.set_trace_callback(queries.append)
            try:
                outputs.append(rollup.current_cached_refs(db.db, 1, updated, meta))
            finally:
                db.db.set_trace_callback(None)
            counts.append(len(queries))
        assert outputs[0] == outputs[1] == updated
        assert counts[0] == counts[1]
        assert "50mg" not in notify_views.patient_summary_text(db.db, 1, cfg=_canonical_cfg())[1]
    finally:
        db.close()


def test_legacy_only_rollup_cache_is_unchanged(tmp_path, monkeypatch):
    db = _seeded_two(tmp_path)
    try:
        cached = {"medications": [{"name": "legacy fixture", "dose": "unchanged"}],
                  "latest_vitals": {"at": "2026-09-22", "sbp": 128, "dbp": 70},
                  "profile_extension": {"display": "synthetic profile kept"}}
        monkeypatch.setattr(rollup, "build_rollup", lambda *_args, **_kwargs: pytest.fail("legacy cache rebuilt"))
        assert rollup.current_cached_refs(db.db, 1, cached, {}) == cached
    finally:
        db.close()
