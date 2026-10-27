"""Synthetic projection-version migration after outpatient interpretation changes."""

import json

import semantic_policy
import semantic_projection as projection
import semantic_v4 as v4
import semantic_candidate as candidate
from inference_version import CURRENT_VERSION, VERSION_KEY
from mcs_queries import current_projection_id, current_v4_id
from semantic_testkit import _canonical_cfg, _drained_old_version, _kind_rows


def test_version_five_is_reprojected_from_the_same_audited_document(tmp_path):
    db = _drained_old_version(tmp_path)
    try:
        kinds = ("canonical_projection", v4.KIND_V4)
        db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.projection_version',5) "
                      "WHERE kind IN (?,?)", kinds)
        db.db.commit()
        old = {row["artifact_id"]: row for kind in kinds for row in _kind_rows(db, kind)}
        assert len(old) == 4 and projection.PROJECTION_VERSION == 6
        for mid in (1, 2):
            current = db.db.execute(
                f"SELECT {current_projection_id()}, {current_v4_id()} FROM messages m "
                "WHERE m.message_id=?", (mid,)).fetchone()
            assert tuple(current) == (None, None)

        scfg = semantic_policy.semantic_config(_canonical_cfg())[0]
        assert v4.reproject_stale(db, scfg, limit=1)["reprojected"] == 1
        assert v4.reproject_stale(db, scfg)["reprojected"] == 3
        for kind in kinds:
            rows = _kind_rows(db, kind)
            newer = [row for row in rows if row["artifact_id"] not in old]
            assert len(newer) == 2
            for row in newer:
                meta = json.loads(row["meta"])
                prior = old[meta["reprojected_from"]]
                old_meta = json.loads(prior["meta"])
                assert old_meta["projection_version"] == 5
                assert meta["projection_version"] == projection.PROJECTION_VERSION
                assert all(meta[key] == old_meta[key] for key in
                           ("doc_hash", "fingerprint", "policy_fingerprint", "hash"))
        total = db.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        assert v4.reproject_stale(db, scfg)["reprojected"] == 0
        assert db.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == total
    finally:
        db.close()


def test_changed_candidate_cannot_reuse_previous_approval(monkeypatch):
    cfg = _canonical_cfg()
    before, errors = semantic_policy.semantic_config(cfg)
    assert not errors and before["fact_source"] == "canonical"
    original = candidate.candidate_fingerprint(cfg)
    replacement = "0" * 64 if original != "0" * 64 else "1" * 64
    monkeypatch.setattr(candidate, "candidate_fingerprint", lambda _cfg: replacement)
    after, errors = semantic_policy.semantic_config(cfg)
    assert "config: semantic_fact_source_evidence_mismatch" in errors
    assert after["mode"] == "off" and after["fact_source"] == "legacy"


def test_newly_approved_candidate_does_not_reuse_old_policy_projection(monkeypatch):
    old, errors = semantic_policy.semantic_config(_canonical_cfg())
    assert not errors
    old_policy = semantic_policy.policy_fingerprint(old)
    original = old["candidate_fingerprint"]
    replacement = "0" * 64 if original != "0" * 64 else "1" * 64
    monkeypatch.setattr(candidate, "candidate_fingerprint", lambda _cfg: replacement)
    new, errors = semantic_policy.semantic_config(_canonical_cfg())
    assert not errors and new["fact_source"] == "canonical"
    new_policy = semantic_policy.policy_fingerprint(new)
    assert old_policy != new_policy
    old_meta = {VERSION_KEY: CURRENT_VERSION, "projection_version": 5,
                "policy_fingerprint": old_policy, "fingerprint": "synthetic-source"}
    assert projection.projection_transition(
        old_meta, off=False, enabled=True, policy=new_policy,
        source_fingerprint="synthetic-source", revivable=True) == ("expire", "policy")
