"""Old canonical rollup values cannot bypass the current projection gate."""
import semantic_drain
import semantic_policy
import json
import time

import pytest

import brain_export
import inference_dependency_loss
import notify_views
import rollup
import semantic_projection as projection
import semantic_v4 as v4
from semantic_testkit import _PassJev, _canonical_cfg, _ledger, _llm_v2, _message, _patient, _seeded_two


def _published(tmp_path):
    db = _ledger(tmp_path)
    patient = _patient(db)
    statements = {
        "アムロジピン": "本人は現在アムロジピン5mgを内服しています。",
        "メトホルミン": "本人は現在メトホルミン500mgを内服しています。",
    }
    patient.messages = [_message(1, body=statements["アムロジピン"])]
    patient.messages[0].replies = [_message(2, parent=1, body=statements["メトホルミン"])]
    db.save_patient(patient, notify={"source": "unread"}, semantic=True)

    def model(prompt):
        if "要約器" in prompt:
            return _llm_v2(prompt)
        # Only the target chunk may own an extracted fact; neighbouring
        # context in a prompt can mention the other drug.
        target = prompt.partition("本文:\n<<<\n")[2].partition("\n>>>")[0]
        assert target in statements.values()
        result = json.loads(_llm_v2(target))
        for fact in result.get("facts", []):
            drug = "アムロジピン" if "アムロジピン" in fact["statement"] else "メトホルミン"
            assert statements[drug] == target
            fact.update(statement=statements[drug], evidence_quote=statements[drug],
                        epistemic="reported", quantity="5mg" if drug == "アムロジピン" else "500mg")
        return json.dumps(result, ensure_ascii=False)

    out = semantic_drain.run_due(db, _canonical_cfg(), {"errors": []}, time.monotonic() + 480,
                           jev_client=_PassJev(), llm_fn=model)
    docs = [json.loads(row['content']) for row in db.artifacts('semantic_facts_v2')]
    diagnostics = [{"source": doc.get("source"), "coverage": doc.get("coverage"),
                    "open": [item for item in doc.get("obligations", [])
                             if item.get("status") in ("open", "ambiguous", "failed")]}
                   for doc in docs]
    assert out["done"] == 1, json.dumps({"out": out, "diagnostics": diagnostics}, ensure_ascii=False, sort_keys=True)
    assert {doc['source']['message_id'] for doc in docs} == {'1', '2'}
    for doc in docs:
        expected = statements['アムロジピン' if doc['source']['message_id'] == '1' else 'メトホルミン']
        fact = next(f for f in doc['facts'] if f['kind'] == 'medication_event')
        evidence = {item['evidence_id']: item['quote'] for item in doc['evidence']}
        assert doc['coverage']['status'] == 'complete' and not doc['coverage']['open_obligation_ids']
        assert fact['subject'] == 'patient:1' and fact['statement'] == expected
        assert fact['quantity'] == ('5mg' if doc['source']['message_id'] == '1' else '500mg')
        assert [evidence[eid] for eid in fact['evidence_ids']] == [expected]
    rollup.rebuild(db, 1)
    return db


def _cached(db):
    row = db.db.execute("SELECT content,meta FROM artifacts WHERE kind='patient_rollup' "
                        "AND project_id=1").fetchone()
    return json.loads(row["content"]), row["meta"]


@pytest.mark.parametrize('tampered', [False, True])
def test_old_projection_cache_is_safe_for_readers_and_recovers_once(tmp_path, monkeypatch, tampered):
    db = _published(tmp_path)
    try:
        cached, meta = _cached(db)
        assert any(med.get("dose") == "5mg" for med in cached["medications"])
        source_before = tuple(db.db.execute("SELECT body_text,content_hash FROM messages WHERE message_id=1").fetchone())
        v2_before = [tuple(row) for row in db.db.execute(
            "SELECT artifact_id,content FROM artifacts WHERE kind='semantic_facts_v2' AND message_id=1")]
        assert v2_before
        cached["medications"] = [{"name": "アムロジピン", "dose": "50mg"}]
        cached["profile_extension"] = {"display": "synthetic profile kept"}
        with db.db:
            db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.projection_version',3) "
                          "WHERE kind IN ('canonical_projection','semantic_facts_v4')")
            if tampered:
                tampered_rows = 0
                for row in db.db.execute("SELECT artifact_id,content FROM artifacts WHERE message_id=1 "
                                         "AND kind IN ('canonical_projection','semantic_facts_v4')").fetchall():
                    old = json.loads(row["content"])
                    for med in old.get("meds", []):
                        if med.get("dose") == "5mg":
                            med["dose"] = "50mg"
                            tampered_rows += 1
                    db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                                  (json.dumps(old), row["artifact_id"]))
                assert tampered_rows > 0
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
        unknown = rollup.current_cached_refs(db.db, 1, cached, meta)
        assert unknown["rollup_freshness"] == "unknown" and calls == []
        safe = rollup.current_cached_refs(db.db, 1, cached, meta, cfg=_canonical_cfg())
        assert calls == [1]
        assert "profile_extension" not in safe
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
        assert v4.reproject_stale(db, semantic_policy.semantic_config(_canonical_cfg())[0])["reprojected"] == 4
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind IN "
                             "('canonical_projection','semantic_facts_v4')").fetchone()[0] == before + 4
        assert tuple(db.db.execute("SELECT body_text,content_hash FROM messages WHERE message_id=1").fetchone()) == source_before
        assert [tuple(row) for row in db.db.execute(
            "SELECT artifact_id,content FROM artifacts WHERE kind='semantic_facts_v2' AND message_id=1")] == v2_before
        rollup.rebuild(db, 1)
        updated, meta = _cached(db)
        assert json.loads(meta)["projection_version"] == projection.PROJECTION_VERSION
        assert rollup.dirty_projects(db) == []
        losses = inference_dependency_loss.read(db.db, 1)['losses']
        if tampered:
            assert any(loss['origin']['message_id'] == 1 and loss['axis'] == 'use_state' for loss in losses)
            assert not any(med.get('dose') == '5mg' for med in updated.get('medications', []))
            assert any(med.get('dose') == '5mg' for med in updated.get('unverified_medications', []))
            assert any(relation['type'] == 'source_withdrawn'
                       for relation in updated['clinical_state']['relations'])
        else:
            assert not losses
            assert any(med.get("dose") == "5mg" for med in updated["medications"])
        outputs, counts = [], []
        with monkeypatch.context() as patch:
            patch.setattr(rollup, "build_rollup", lambda *_args, **_kwargs: pytest.fail("no-config cache rebuilt"))
            for _ in range(2):
                queries = []
                db.db.set_trace_callback(queries.append)
                try:
                    outputs.append(rollup.current_cached_refs(db.db, 1, updated, meta))
                finally:
                    db.db.set_trace_callback(None)
                counts.append(len(queries))
        assert outputs[0] == outputs[1]
        assert outputs[0]["rollup_freshness"] == "unknown"
        assert "medications" not in outputs[0]
        assert counts[0] == counts[1]
        assert "50mg" not in notify_views.patient_summary_text(db.db, 1, cfg=_canonical_cfg())[1]
    finally:
        db.close()


def test_legacy_only_rollup_cache_rebuilds_without_promoting_cached_values(tmp_path, monkeypatch):
    db = _seeded_two(tmp_path)
    try:
        cached = {"medications": [{"name": "legacy fixture", "dose": "unchanged"}],
                  "latest_vitals": {"at": "2026-09-22", "sbp": 128, "dbp": 70},
                  "profile_extension": {"display": "synthetic profile kept"}}
        calls = []
        original = rollup.build_rollup

        def counted(*args, **kwargs):
            calls.append(kwargs.get("cfg"))
            return original(*args, **kwargs)

        monkeypatch.setattr(rollup, "build_rollup", counted)
        before = db.db.total_changes
        result = rollup.current_cached_refs(db.db, 1, cached, {})
        assert calls == [None]
        assert "legacy fixture" not in json.dumps(result, ensure_ascii=False)
        assert "synthetic profile kept" not in json.dumps(result, ensure_ascii=False)
        assert not result.get("latest_vitals")
        assert result["measurement_coverage"]["complete"] is False
        assert db.db.total_changes == before
        assert cached["medications"][0]["dose"] == "unchanged"
    finally:
        db.close()
