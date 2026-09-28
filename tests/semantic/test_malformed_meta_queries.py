"""Tick, drain and stats queries skip artifacts with malformed meta."""
import json
import time

import pytest

import semantic
import semantic_drain
import semantic_v4 as v4
from semantic_policy import semantic_config
from semantic_store import invalidate_projections

MALFORMED = ["{not json", "[1,2]", '"s"', "5", None]


def _add_malformed(db, kind, mids=(1, 2)):
    ids = []
    for mid in mids:
        for raw in MALFORMED:
            aid = db.artifact_add(kind, "{}", project_id=1, message_id=mid)
            db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                          (raw, aid))
            ids.append(aid)
    db.db.commit()
    return ids


def _metas(db, ids):
    return [db.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?",
                          (i,)).fetchone()[0] for i in ids]


def test_reproject_and_run_due_skip_malformed_meta(tmp_path):
    from semantic_testkit import _canonical_cfg, _llm_v2
    from semantic_testkit import _drained_old_version
    from semantic_testkit import _PassJev
    db = _drained_old_version(tmp_path)
    try:
        scfg = semantic_config(_canonical_cfg())[0]
        bad = [i for kind in ("canonical_projection", v4.KIND_V4)
               for i in _add_malformed(db, kind)]
        before = _metas(db, bad)
        assert v4.reproject_stale(db, scfg) == {
            "reprojected": 4, "skipped": 0, "skip_reasons": {}}
        assert v4.reproject_stale(db, scfg)["reprojected"] == 0
        for _ in range(2):
            result = {"errors": []}
            semantic.run_due(db, _canonical_cfg(), result,
                             time.monotonic() + 300, jev_client=_PassJev(),
                             llm_fn=_llm_v2)
            assert result["errors"] == []
        assert _metas(db, bad) == before
    finally:
        db.close()


@pytest.mark.parametrize("mode", ["enforce", "off"])
def test_invalidate_projections_skips_malformed_meta(tmp_path, mode):
    from semantic_testkit import _canonical_cfg
    from semantic_testkit import _drained_old_version
    db = _drained_old_version(tmp_path)
    try:
        scfg = dict(semantic_config(_canonical_cfg())[0], mode=mode)
        bad = [i for kind in ("canonical_projection", v4.KIND_V4)
               for i in _add_malformed(db, kind)]
        before = _metas(db, bad)
        assert invalidate_projections(db, scfg) == (4 if mode == "off" else 0)
        assert _metas(db, bad) == before
    finally:
        db.close()


def test_qc_seed_skips_malformed_meta(tmp_path):
    from extract_testkit import _hash, _ledger, _message
    from extract_testkit import _qc_job, _v2_artifact
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message()])
        for kind in ("extract_llm", "extract_qc", "signal_v1"):
            _add_malformed(db, kind, mids=(1,))
        source_id = _v2_artifact(db, 1, _hash(db))
        assert semantic_drain._qc_seed(db, time.time()) == 1
        assert semantic_drain._qc_seed(db, time.time()) == 0
        payload = json.loads(_qc_job(db)["payload"])
        assert payload["source_artifact_id"] == source_id
        assert payload["hash"] == _hash(db)
    finally:
        db.close()


def test_canonical_readiness_counts_malformed_audits_as_unknown(tmp_path):
    from semantic_testkit import _cfg, _seeded
    db = _seeded(tmp_path)
    try:
        db.artifact_add("semantic_facts_audit", "{}", project_id=1,
                        message_id=1, meta={"audit_status": "PASS"})
        _add_malformed(db, "semantic_facts_audit", mids=(1,))
        r = semantic._canonical_readiness(
            db, _cfg("shadow", fact_source="shadow"))
        assert r["fact_audits"] == {"PASS": 1, "unknown": len(MALFORMED)}
    finally:
        db.close()
