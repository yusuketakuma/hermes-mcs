"""Synthetic manifest replay follows the same latest row as progress reads."""
import json
import sqlite3

import pytest

import semantic_assembly as assembly
import semantic_extraction as extraction
import semantic_policy
import semantic_store
import semantic_transport
from semantic_testkit import NO_FACTS, _canonical_cfg, _ledger, _message, _patient
from test_semantic_facts_v2 import _FakeLedger


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    import extraction_refresh

    store = _ledger(tmp_path)
    _patient(store)
    _patient(store, 2)
    store.save_messages([_message(1), _message(2)], project_id=1)
    store.save_messages([_message(3, pid=2)], project_id=2)
    monkeypatch.setattr(extraction_refresh, "refresh_extraction_cards", lambda *a: None)
    yield store
    store.close()


def _rows(ledger, *, project_id=1, message_id=1):
    return list(ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind=? AND project_id=? AND message_id=? ORDER BY artifact_id",
        (extraction.KIND_MANIFEST, project_id, message_id)))


def _persist(ledger, fp="synthetic-fp", model="model-a", *, project_id=1, message_id=1):
    body = "架空の連絡です。"
    manifest = assembly.build_manifest(body, fp)
    extraction._persist_manifest(ledger, project_id, message_id, model, fp, manifest, len(body))


def test_real_resume_model_aba_reselects_latest_manifest_and_preserves_prefix(ledger, monkeypatch):
    body = "宇宙の架空説明。" * 400
    ledger.save_messages([_message(1, body=body)], project_id=1)
    model = ["model-a"]
    monkeypatch.setattr(semantic_transport, "llm_model", lambda cfg=None: model[0])
    fingerprints = []
    results = []
    calls = []
    for index, value in enumerate(("model-a", "model-b", "model-a")):
        model[0] = value
        bundle = semantic_store.thread_bundle(ledger.db, 1, 1, local_model=value)
        fingerprints.append(bundle["source_fingerprint"])
        current_calls = []
        calls.append(current_calls)

        def interrupted(prompt):
            current_calls.append(prompt)
            if index == 2 or len(current_calls) > 1:
                raise RuntimeError("synthetic interruption")
            return json.dumps({"facts": [], "category_presence": NO_FACTS})

        results.append(extraction.extract_facts_v2(
            interrupted, bundle["members"][0], ledger=ledger,
            source_fingerprint=bundle["source_fingerprint"]))
    assert fingerprints[0] != fingerprints[1] and fingerprints[0] == fingerprints[2]
    assert all(not result["extraction_complete"] for result in results)
    assert results[2]["reused_chunks"] == [0] and len(calls[2]) == 1
    rows = _rows(ledger)
    assert len(rows) == 3 and [r["model"] for r in rows] == ["model-a", "model-b", "model-a"]
    assert json.loads(rows[-1]["meta"])["source_fingerprint"] == fingerprints[0]
    cfg, errors = semantic_policy.semantic_config(_canonical_cfg())
    assert not errors
    message = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    progress = extraction.progress_for_message(ledger.db, message, model="model-a",
        policy_fingerprint=semantic_policy.policy_fingerprint(cfg))
    assert progress["state"] == "processing" and progress["completed"] == 1


def test_only_latest_exact_model_metadata_and_content_are_reused(ledger, monkeypatch):
    import extraction_refresh

    renders = []
    monkeypatch.setattr(extraction_refresh, "refresh_extraction_cards", lambda *args: renders.append(args[1:]))
    _persist(ledger)
    first = _rows(ledger)[0]["artifact_id"]
    _persist(ledger)
    assert [r["artifact_id"] for r in _rows(ledger)] == [first]
    assert len(renders) == 1
    _persist(ledger, model="model-b")  # same source fingerprint and manifest
    _persist(ledger, model="model-a")
    assert [r["model"] for r in _rows(ledger)] == ["model-a", "model-b", "model-a"]
    assert len(renders) == 3
    ledger.db.execute("UPDATE artifacts SET meta='{}' WHERE artifact_id=?", (_rows(ledger)[-1]["artifact_id"],))
    ledger.db.commit()
    _persist(ledger)
    assert len(_rows(ledger)) == 4


@pytest.mark.parametrize("column,value", [("content", "not-json"), ("meta", "not-json"),
                                           ("content", '{}'), ("meta", '{}')])
def test_bad_latest_row_does_not_revive_old_matching_row(ledger, column, value):
    _persist(ledger)
    _persist(ledger, fp="other-fp")
    last = _rows(ledger)[-1]["artifact_id"]
    ledger.db.execute(f"UPDATE artifacts SET {column}=? WHERE artifact_id=?", (value, last))
    ledger.db.commit()
    _persist(ledger)
    assert len(_rows(ledger)) == 3
    assert json.loads(_rows(ledger)[-1]["meta"])["source_fingerprint"] == "synthetic-fp"


def test_other_project_or_message_does_not_supersede_source_manifest(ledger):
    _persist(ledger)
    _persist(ledger, fp="other-fp", project_id=2, message_id=3)
    _persist(ledger, fp="other-fp", message_id=2)
    _persist(ledger)
    assert len(_rows(ledger)) == 1


def test_source_revision_aba_creates_distinct_bound_manifests(ledger, monkeypatch):
    monkeypatch.setattr(semantic_transport, "llm_model", lambda cfg=None: "model-a")
    fingerprints = []
    for body in ("完全合成の元の連絡。", "完全合成の訂正後の連絡。", "完全合成の元の連絡。"):
        ledger.save_messages([_message(1, body=body)], project_id=1)
        bundle = semantic_store.thread_bundle(ledger.db, 1, 1, local_model="model-a")
        fp = bundle["source_fingerprint"]
        fingerprints.append(fp)
        text = bundle["members"][0]["body_original"]
        manifest = assembly.build_manifest(text, fp)
        extraction._persist_manifest(ledger, 1, 1, "model-a", fp, manifest, len(text))
    assert len(set(fingerprints)) == 3
    assert [json.loads(row["meta"])["source_fingerprint"] for row in _rows(ledger)] == fingerprints


def test_failed_new_manifest_write_rolls_back_without_hiding_previous(ledger, monkeypatch):
    _persist(ledger)
    before = [tuple(row) for row in _rows(ledger)]
    original = ledger.artifact_add_tx

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic write failure")

    monkeypatch.setattr(ledger, "artifact_add_tx", fail_after_insert)
    with pytest.raises(RuntimeError, match="synthetic write failure"):
        _persist(ledger, fp="other-fp")
    assert [tuple(row) for row in _rows(ledger)] == before


def _observer(ledger):
    path = ledger.db.execute("PRAGMA database_list").fetchone()[2]
    return sqlite3.connect(path)


def test_noop_keeps_caller_transaction_and_its_uncommitted_changes(ledger, monkeypatch):
    import extraction_refresh

    _persist(ledger)
    observer = _observer(ledger)
    renders = []
    monkeypatch.setattr(extraction_refresh, "refresh_extraction_cards", lambda *a: renders.append(a))
    try:
        ledger.db.execute("UPDATE patients SET disease='synthetic-uncommitted' WHERE project_id=1")
        _persist(ledger)
        assert ledger.db.in_transaction and not renders
        assert observer.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0] == ""
        ledger.db.rollback()
        assert ledger.db.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0] == ""
        assert len(_rows(ledger)) == 1
    finally:
        observer.close()


def test_new_manifest_inside_caller_transaction_is_not_committed_or_rendered(ledger, monkeypatch):
    import extraction_refresh

    _persist(ledger)
    observer = _observer(ledger)
    renders = []
    monkeypatch.setattr(extraction_refresh, "refresh_extraction_cards", lambda *a: renders.append(a))
    try:
        ledger.db.execute("UPDATE patients SET disease='synthetic-uncommitted' WHERE project_id=1")
        _persist(ledger, fp="other-fp")
        assert ledger.db.in_transaction and len(_rows(ledger)) == 2 and not renders
        count = observer.execute("SELECT COUNT(*) FROM artifacts WHERE kind=? AND project_id=1 AND message_id=1",
                                 (extraction.KIND_MANIFEST,)).fetchone()[0]
        assert count == 1
        ledger.db.rollback()
        assert len(_rows(ledger)) == 1
        assert ledger.db.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0] == ""
    finally:
        observer.close()


def test_failure_inside_caller_transaction_leaves_rollback_to_caller(ledger, monkeypatch):
    import extraction_refresh

    _persist(ledger)
    renders = []
    monkeypatch.setattr(extraction_refresh, "refresh_extraction_cards", lambda *a: renders.append(a))
    original = ledger.artifact_add_tx

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic borrowed write failure")

    monkeypatch.setattr(ledger, "artifact_add_tx", fail_after_insert)
    ledger.db.execute("UPDATE patients SET disease='synthetic-uncommitted' WHERE project_id=1")
    with pytest.raises(RuntimeError, match="synthetic borrowed write failure"):
        _persist(ledger, fp="other-fp")
    assert ledger.db.in_transaction and len(_rows(ledger)) == 2 and not renders
    ledger.db.rollback()
    assert len(_rows(ledger)) == 1
    assert ledger.db.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0] == ""


def test_borrowed_commit_makes_manifest_visible_without_precommit_render(ledger, monkeypatch):
    import extraction_refresh

    _persist(ledger)
    renders = []
    monkeypatch.setattr(extraction_refresh, "refresh_extraction_cards", lambda *a: renders.append(a))
    ledger.db.execute("UPDATE patients SET disease='synthetic-change' WHERE project_id=1")
    _persist(ledger, fp="other-fp")
    assert ledger.db.in_transaction and not renders
    ledger.db.commit()
    assert len(_rows(ledger)) == 2 and not renders


def test_owned_transaction_renders_only_after_commit(ledger, monkeypatch):
    import extraction_refresh

    observed = []
    monkeypatch.setattr(extraction_refresh, "refresh_extraction_cards",
                        lambda *args: observed.append(ledger.db.in_transaction))
    _persist(ledger)
    assert observed == [False]


def test_fake_ledger_keeps_existing_transaction_behavior(request, monkeypatch):
    import extraction_refresh
    monkeypatch.setattr(extraction_refresh, "refresh_extraction_cards", lambda *a: None)
    ledger = _FakeLedger(request)
    def uncommitted_add(kind, content, project_id=None, message_id=None, model=None, meta=None):
        ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,model,content,meta) "
                          "VALUES(?,?,?,?,?,?)", (kind, project_id, message_id, model,
                                                  content, json.dumps(meta or {})))
    monkeypatch.setattr(ledger, "artifact_add", uncommitted_add)
    _persist(ledger, message_id="m1")
    assert ledger.db.in_transaction
    _persist(ledger, message_id="m1")
    assert len(ledger.rows) == 1 and ledger.db.in_transaction
