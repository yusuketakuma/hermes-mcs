"""Synthetic medication mention classes and snapshot paging contracts."""
import semantic_transport
import json
from types import SimpleNamespace

import pytest

import drug_timeline
import extract_llm
import ledger
import mcs_view
from test_drug_map import _seed


def current(store, mid, meds):
    _seed(store, mid=mid)
    message = dict(store.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())
    context = extract_llm._thread_context(store.db, message)
    meta = {"hash": message["content_hash"], "extract_version": extract_llm.EXTRACT_VERSION,
            "plan_version": extract_llm.PLAN_VERSION,
            "patient_context_version": extract_llm.PATIENT_CONTEXT_VERSION,
            "actual_model": extract_llm.MODEL,
            "context_binding": extract_llm._chunk_context(message, context)}
    store.artifact_add("extract_llm", json.dumps({"meds": meds}), project_id=1,
                       message_id=mid, model=extract_llm.MODEL, meta=meta)


@pytest.fixture
def corpus(tmp_path):
    path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(path))
    store.ensure_patient(1)
    yield store
    store.close()


def view_of(store):
    return SimpleNamespace(db=store.db, reader=store, meta={"generation_id": "synthetic-1", "generated_at": 1},
        _page=lambda *args: mcs_view.View._page(view_of(store), *args))


def test_all_mention_classes_preserved_with_dates_and_uncertainty(corpus):
    meds = [{"name": "キラナ", "subject": subject, "status": status, "negated": negated,
             "action": action, "event_time": "2026-10-16", "dose": "5mg", "form": "tablet"}
            for subject, status, negated, action in [
                ("patient", "current", False, "start"), ("family", "past", False, "stop"),
                ("unknown", "planned", True, "change"), ("other", "current", False, "none")]]
    current(corpus, 1, meds)
    report = drug_timeline.timeline(view_of(corpus), 1)
    assert [r["mention"] for r in report["items"]] == meds
    assert {r["subject_bucket"] for r in report["items"]} == {"patient", "family", "unknown", "other"}
    assert report["coverage"]["no_current_extraction"] == 0
    assert "照合中" in report["explanation"]
    assert report["coverage"]["attachments"] == "excluded_ocr_not_performed"


def test_stale_plan_rule_fallback_deleted_and_partial_not_absence(corpus):
    current(corpus, 1, [{"name": "キラナ"}])
    corpus.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.plan_version',-1) WHERE kind='extract_llm'")
    _seed(corpus, mid=2, kind="extract_v1")
    _seed(corpus, mid=3)
    corpus.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=3")
    _seed(corpus, mid=4)
    corpus.db.execute("UPDATE messages SET body_state='snippet' WHERE message_id=4")
    report = drug_timeline.timeline(view_of(corpus), 1)
    assert report["items"] == []
    assert report["coverage"]["no_current_extraction"] == 2
    assert report["coverage"]["incomplete_bodies"] == 1
    assert "未抽出" in report["explanation"] and "未取得" in report["explanation"]
    assert "なし" not in report["explanation"]


def test_paging_scope_and_generation_bound(corpus):
    for mid in (1, 2, 3):
        current(corpus, mid, [{"name": "キラナ", "subject": "patient"}])
    view = view_of(corpus)
    first = drug_timeline.timeline(view, 1, limit=1)
    second = drug_timeline.timeline(view, 1, limit=1, cursor=first["next_cursor"])
    assert first["items"][0]["message_id"] != second["items"][0]["message_id"]
    with pytest.raises(ValueError):
        drug_timeline.timeline(view, 1, name="キラナ", cursor=first["next_cursor"])


def test_frozen_context_frame_and_digest_are_required(corpus):
    import patient_state_context
    current(corpus, 1, [{"name": "キラナ", "subject": "unknown"}])
    message = dict(corpus.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    frame = patient_state_context.prepare(corpus, message, model=extract_llm.MODEL,
        contract=extract_llm._context_contract(), attempt={}, enabled=True,
        base_context=None, version=extract_llm.EXTRACT_VERSION)
    message["_frame"] = frame
    row = corpus.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='extract_llm' ORDER BY artifact_id DESC LIMIT 1").fetchone()
    meta = json.loads(row["meta"])
    meta.update(extract_llm._frame_meta(message))
    meta["context_binding"] = extract_llm._chunk_context(message, frame["text"])
    corpus.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), row[0]))
    assert drug_timeline.timeline(view_of(corpus), 1)["items"]
    corpus.db.execute("UPDATE artifacts SET content=json_set(content,'$.digest',?) WHERE artifact_id=?", ("f" * 64, frame["artifact_id"]))
    report = drug_timeline.timeline(view_of(corpus), 1)
    assert report["items"] == [] and report["coverage"]["no_current_extraction"] == 1


def test_canonical_original_document_and_generation_change(tmp_path):
    from semantic_testkit import _seeded_two, _drain, _PassJev, _canonical_cfg
    store = _seeded_two(tmp_path)
    try:
        assert _drain(store, jev=_PassJev())["done"] == 1
        cfg = _canonical_cfg()
        report = drug_timeline.timeline(view_of(store), 1, cfg=cfg)
        assert len(report["items"]) == 4
        assert all("workflow_status" in item["mention"] and "event_time" in item["mention"]
                   for item in report["items"])
        cfg["local_llm"] = {"model": "changed-synthetic-model"}
        report = drug_timeline.timeline(view_of(store), 1, cfg=cfg)
        assert report["items"] == [] and report["coverage"]["no_current_extraction"] == 2
    finally:
        store.close()


def test_changed_dictionary_pin_hides_old_candidates(corpus, tmp_path):
    import drug_map
    from test_drug_map import _dictionary
    current(corpus, 1, [{"name": "キラナ", "subject": "patient"}])
    drug_map.derive(corpus, _dictionary(tmp_path))
    assert drug_timeline.timeline(view_of(corpus), 1)["items"][0]["candidate"]
    report = drug_timeline.timeline(view_of(corpus), 1,
        cfg={"drug_map": {"path": str(tmp_path / "new.json"), "sha256": "f" * 64}})
    assert report["items"][0]["candidate"] is None
    assert report["coverage"]["dictionary"] == "pending" and "照合中" in report["explanation"]


def canonical_source(store, monkeypatch, *, name="キラナ錠5mg", grounded=True, extra_names=()):
    statement = name + "を開始"
    current(store, 1, [])
    row = store.db.execute("SELECT artifact_id FROM artifacts ORDER BY artifact_id DESC LIMIT 1").fetchone()
    store.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                     (json.dumps({"fingerprint": "synthetic", "policy_fingerprint": "synthetic"}), row[0]))
    quote = statement + "、" + "、".join(extra_names)
    fact = {"kind": "medication_event", "statement": statement, "subject": "patient:1",
            "action": "start", "polarity": "affirmed", "epistemic": "reported",
            "workflow_status": "performed", "event_time": "2026-10-01", "quantity": "5mg",
            "evidence_ids": ["e1"], "patient_context": {"details": [
                {"key": "drug_name", "value": value, "evidence": quote}
                for value in ([name, *extra_names] if grounded else [])]}}
    doc = {"facts": [fact], "evidence": [{"evidence_id": "e1", "quote": quote}]}
    monkeypatch.setattr("drug_map._source", lambda *a: (row[0], "semantic_facts_v4", "{}", "synthetic", 1))
    monkeypatch.setattr("semantic_v4.reproject_doc", lambda *a: (doc, None))
    monkeypatch.setattr("semantic_store.thread_bundle", lambda *a, **k: {"source_fingerprint": "synthetic"})
    monkeypatch.setattr("semantic_policy.semantic_config", lambda *a: ({}, []))
    monkeypatch.setattr("semantic_policy.policy_fingerprint", lambda *a: "synthetic")
    monkeypatch.setattr(semantic_transport, 'llm_model', lambda *a: "synthetic")
    return doc


def test_canonical_grounded_drug_name_keeps_strength_and_search(corpus, monkeypatch):
    doc = canonical_source(corpus, monkeypatch)
    report = drug_timeline.timeline(view_of(corpus), 1, name="キラナ錠5mg")
    mention = report["items"][0]["mention"]
    assert mention["name"] == "キラナ錠5mg" and mention["name_state"] == "grounded"
    assert mention["statement"] == doc["facts"][0]["statement"]
    assert mention["quantity"] == "5mg"
    assert mention["evidence_quotes"] == [doc["evidence"][0]["quote"]]
    assert not drug_timeline.timeline(view_of(corpus), 1, name="5mg")["items"]


@pytest.mark.parametrize("case", ["missing", "foreign_evidence", "ambiguous"])
def test_canonical_unestablished_names_keep_evidence_without_guessing(corpus, monkeypatch, case):
    doc = canonical_source(corpus, monkeypatch, grounded=case != "missing",
                           extra_names=("キラーナ錠10mg",) if case == "ambiguous" else ())
    if case == "foreign_evidence":
        doc["facts"][0]["evidence_ids"] = ["different-fact"]
    report = drug_timeline.timeline(view_of(corpus), 1)
    item = report["items"][0]
    assert item["mention"]["name"] == "薬名不明"
    assert item["mention"]["name_state"] == ("ambiguous" if case == "ambiguous" else "unknown")
    assert item["mention"]["statement"] == "キラナ錠5mgを開始"
    assert item["candidate"] is None and item["matching"] == "pending"
    assert not drug_timeline.timeline(view_of(corpus), 1, name="5mg")["items"]
