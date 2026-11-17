"""Drain-level local model change: local results are redone, cloud-produced ones are kept (synthetic).

The model is changed through the module seam (semantic_transport.LLM_MODEL),
which is what llm_model() returns for a changed config.json local_llm.model.
That equivalence holds for fact_source=legacy only; for shadow/canonical a
real config change also moves policy_fingerprint (candidate_fingerprint),
see test_local_model_change_semantic.test_shadow_policy_ignores_a_configured_local_model_change.
"""
import json
import time

import pytest

import cloud_policy
import semantic_drain
import semantic_extraction
import semantic_job_processor
import semantic_policy
import semantic_publication
import semantic_send_gate
import semantic_store
import semantic_transport
from semantic_testkit import _PassJev, _canonical_cfg, _kind_rows, _llm_v2, _seeded_two

_PRODUCED = (semantic_policy.KIND_SUMMARY, semantic_policy.KIND_CANDIDATE, semantic_policy.KIND_FACTS_V2,
             semantic_policy.KIND_FACT_PROJ, semantic_publication.KIND_V4)
_RESERVATION = "synthetic-reservation"


def _drain(db, calls, jev, *, rounds=4, requeue=False, provider="local"):
    if requeue:
        # the existing replay path re-runs the done job (no new seed contract here)
        db.semantic_seed(1, [1, 2], {"source": "history_import"})

    def llm(prompt):
        calls.append(prompt)
        semantic_transport._record_inference(provider)
        return _llm_v2(prompt)
    for _ in range(rounds):
        semantic_drain.run_due(db, _canonical_cfg(), {"errors": []}, time.monotonic() + 300,
                               jev_client=jev, llm_fn=llm)


def _current_summaries(db):
    scfg = semantic_policy.runtime_config(_canonical_cfg())[0]
    policy = semantic_policy.policy_fingerprint(scfg)
    out = {}
    for mid in (1, 2):
        bundle = semantic_store.thread_bundle(db.db, 1, 1, [mid])
        row = semantic_send_gate.current_summary(db.db, 1, mid, bundle, policy, fact_source="canonical")
        out[mid] = row["artifact_id"] if row else None
    return out


@pytest.fixture
def drained(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-a")
    db = _seeded_two(tmp_path)
    calls = []
    _drain(db, calls, _PassJev())
    assert calls and all(_current_summaries(db).values())
    yield db, monkeypatch
    db.close()


class _Receipts:
    """Stubbed settled cloud reservations: one per member, verifiable while ``valid``."""

    def __init__(self, db, monkeypatch):
        self.valid = True
        self.ids = {db.artifact_add(cloud_policy.KINDS[0], "{}", project_id=1, message_id=mid,
                                    model=cloud_policy.PROVIDERS["anthropic"],
                                    meta={"reservation_id": _RESERVATION}): mid for mid in (1, 2)}
        monkeypatch.setattr(cloud_policy, "reservation_evidence", self.evidence)
        monkeypatch.setattr(cloud_policy, "reservation_evidence_batch",
                            lambda db_, ids: {aid: self.evidence(db_, aid) for aid in ids})

    def evidence(self, _db, artifact_id):
        if not self.valid or artifact_id not in self.ids:
            raise ValueError("synthetic_receipt_missing")
        return {"reservation": {"reservation_id": _RESERVATION, "model": cloud_policy.PROVIDERS["anthropic"],
                                "route": "semantic_backlog", "project_id": 1,
                                "message_id": self.ids[artifact_id]}}


@pytest.fixture
def cloud_drained(tmp_path, monkeypatch):
    """Every result written by the production writers under a stubbed cloud producer."""
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-a")
    monkeypatch.setattr(semantic_transport, "inference_provider", lambda: "anthropic")
    monkeypatch.setattr(semantic_transport, "inference_model", lambda: cloud_policy.PROVIDERS["anthropic"])
    monkeypatch.setattr(semantic_transport, "inference_requests", lambda: [_RESERVATION])
    db = _seeded_two(tmp_path)
    receipts = _Receipts(db, monkeypatch)
    calls = []
    _drain(db, calls, _PassJev(), provider="anthropic")
    assert calls and all(_current_summaries(db).values())
    for kind in _PRODUCED:
        for row in _kind_rows(db, kind):
            proof = json.loads(row["meta"])["provider_provenance"]
            assert proof["providers"] == ["anthropic"] and proof["complete"] is True, kind
    yield db, monkeypatch, receipts
    db.close()


def test_local_semantic_results_are_redone_for_a_new_local_model(drained):
    db, monkeypatch = drained
    before = _current_summaries(db)
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-b")
    assert _current_summaries(db) == {1: None, 2: None}
    calls = []
    _drain(db, calls, _PassJev(), requeue=True)
    after = _current_summaries(db)
    assert calls and all(after.values()) and set(after.values()).isdisjoint(before.values())
    rows = {row["artifact_id"]: row["model"] for row in _kind_rows(db, semantic_policy.KIND_SUMMARY)}
    assert {rows[artifact_id] for artifact_id in after.values()} == {"synthetic-local-b"}
    # back to the first model: the latest (model B) rows decide, never an older A row
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-a")
    assert _current_summaries(db) == {1: None, 2: None}


def test_cloud_produced_semantic_results_survive_a_local_model_change(cloud_drained):
    db, monkeypatch, _ = cloud_drained
    before = _current_summaries(db)
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-b")
    assert _current_summaries(db) == before
    calls = []
    jev = _PassJev()
    _drain(db, calls, jev, requeue=True, provider="anthropic")
    assert calls == [] and jev.requests_made == 0 and _current_summaries(db) == before
    assert db.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic'").fetchone()[0] == "done"


def _member(db, mid):
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [mid])
    return next(m for m in bundle["members"] if m["message_id"] == mid), bundle["source_fingerprint"]


def _reuse(db, mid):
    member, fingerprint = _member(db, mid)

    def no_call(_prompt):
        raise AssertionError("a reusable chunk must not be re-extracted")
    return semantic_extraction.extract_facts_v2(no_call, member, ledger=db, source_fingerprint=fingerprint,
                                                project_id=1)


def test_verified_cloud_chunks_are_reused_under_another_local_model(cloud_drained):
    db, monkeypatch, _ = cloud_drained
    first = _reuse(db, 1)
    assert first["reused_chunks"] and first["producer_evidence"] == {"providers": ["anthropic"], "complete": True}
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-b")
    with semantic_transport.local_model_scope():
        changed = _reuse(db, 1)
        assert changed["reused_chunks"] == first["reused_chunks"] and changed["extraction_complete"]
        assert changed["producer_evidence"] == first["producer_evidence"]
        # a proven cloud prefix never joins a local fallback, whichever local model is set
        assert not semantic_job_processor.local_fallback_allowed(db.db, 1, 1, _member(db, 1)[1])


@pytest.mark.parametrize("local_model", ["synthetic-local-a", "synthetic-local-b"])
def test_unproven_cloud_chunk_is_judged_as_local(cloud_drained, local_model):
    db, monkeypatch, receipts = cloud_drained
    receipts.valid = False
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", local_model)
    with semantic_transport.local_model_scope():
        result = _reuse(db, 1)
        assert not result["reused_chunks"] and not result["extraction_complete"]
        fingerprint = _member(db, 1)[1]
        # same local model: an unproven chunk of this model blocks the fallback;
        # another model: no side reuses it, so the fallback may proceed
        assert semantic_job_processor.local_fallback_allowed(db.db, 1, 1, fingerprint) is (
            local_model != "synthetic-local-a")


def test_local_chunks_allow_the_fallback_under_any_local_model(drained):
    db, monkeypatch = drained
    fingerprint = _member(db, 1)[1]
    assert semantic_job_processor.local_fallback_allowed(db.db, 1, 1, fingerprint)
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-b")
    assert semantic_job_processor.local_fallback_allowed(db.db, 1, 1, fingerprint)


def _progress(db, mid, model):
    row = dict(db.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())
    scfg = semantic_policy.runtime_config(_canonical_cfg())[0]
    return semantic_extraction.progress_for_message(
        db.db, row, model=model, policy_fingerprint=semantic_policy.policy_fingerprint(scfg))


def test_progress_shows_a_pending_local_redo_as_processing(drained):
    db, _ = drained
    assert _progress(db, 1, "synthetic-local-a")["state"] == "complete"
    # the local publication awaits its redo: chunks are redone, not an attention state
    redo = _progress(db, 1, "synthetic-local-b")
    assert redo["state"] == "processing" and redo["completed"] == 0


def test_progress_keeps_a_cloud_publication_complete(cloud_drained):
    db, _, _ = cloud_drained
    assert _progress(db, 1, "synthetic-local-b")["state"] == "complete"


def test_readers_with_a_supplied_config_never_open_host_configuration(drained):
    import patient_context_progress
    import patient_context_refresh_readiness as readiness
    db, monkeypatch = drained
    cfg = _canonical_cfg()
    scfg = semantic_policy.runtime_config(cfg)[0]
    rows = db.db.execute("SELECT m.*,p.project_id AS patient_id,p.fetch_reason FROM messages m "
                         "JOIN patients p ON p.project_id=m.project_id ORDER BY m.message_id").fetchall()

    def ready():
        return ([readiness._message_ready(db, row, cfg, scfg, deadline=time.monotonic() + 600,
                                          attachment_mode="off") for row in rows],
                patient_context_progress._local_sources_ready(db.db, 1, cfg))
    expected = ready()
    with semantic_transport.config_model_scope(cfg):
        assert [semantic_store.reprocess_target_state(db, row, scfg, cfg=cfg) for row in rows] == [
            "current", "current"]

    def host_read(*_args, **_kwargs):
        raise AssertionError("a reader with a supplied config opened host configuration")
    monkeypatch.setattr(semantic_transport, "load_config", host_read)
    assert ready() == expected
    # the supplied config decides: its local model, not the host's, judges local rows
    other = {**cfg, "local_llm": {"model": "synthetic-local-b"}}
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", semantic_transport._LLM_MPIN)
    with semantic_transport.config_model_scope(other):
        assert not {semantic_store.reprocess_target_state(db, row, scfg, cfg=other)
                    for row in rows} & {"current", "current_skipped"}
