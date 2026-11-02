"""Synthetic medication mention classes and snapshot paging contracts."""
import semantic_transport
import json
from types import SimpleNamespace

import pytest

import drug_timeline
import extract_llm
import ledger
import mcs_view
from inference_dependency_loss import source_binding
from inference_version import CURRENT_VERSION, VERSION_KEY
from medications_testkit import _seed


def current(store, mid, meds, *, posted="2026-09-19T00:00:00+09:00"):
    _seed(store, mid=mid, posted=posted)
    message = dict(store.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())
    message["_source_binding"] = source_binding(store.db, 1, mid)
    context = extract_llm._thread_context(store.db, message)
    meta = {"hash": message["content_hash"], VERSION_KEY: CURRENT_VERSION,
            "source_binding": message["_source_binding"],
            "extract_version": extract_llm.EXTRACT_VERSION,
            "plan_version": extract_llm.PLAN_VERSION,
            "patient_context_version": extract_llm.PATIENT_CONTEXT_VERSION,
            "actual_model": extract_llm.MODEL,
            "context_binding": extract_llm._chunk_context(message, context),
            "integrity": {"completeness_chunks": 1, "completeness": {
                "0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}}
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
    assert report["coverage"]["withdrawn_posts"] == 1
    assert "未抽出" in report["explanation"] and "未取得" in report["explanation"]
    assert "削除された投稿あり" in report["explanation"]
    assert "なし" not in report["explanation"]


@pytest.mark.parametrize("integrity,complete", [
    ({}, False), (None, False), ([], False),
    ({"completeness": {}}, False), ({"completeness": None}, False),
    ({"completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}, False),
    ({"completeness_chunks": 1, "completeness": {
        "0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}, True),
    ({"completeness_chunks": 1, "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": False}}}, False),
    ({"completeness_chunks": 1, "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0.0}}}, False),
    ({"completeness_chunks": 1, "completeness": {"0": {"state": "hint_gaps_resolved"}}}, False),
    ({"completeness_chunks": 1, "completeness": {"0": {"state": "unresolved", "remaining_hint_candidates": 0}}}, False),
])
def test_completeness_keeps_unresolved_mentions_out_of_timeline(corpus, integrity, complete):
    current(corpus, 1, [{"name": "キラナ", "subject": "patient"}])
    corpus.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.integrity',json(?)) WHERE kind='extract_llm'",
                      (json.dumps(integrity),))
    report = drug_timeline.timeline(view_of(corpus), 1)
    assert bool(report["items"]) is complete
    assert report["coverage"]["no_current_extraction"] == (0 if complete else 1)


def test_paging_scope_and_generation_bound(corpus):
    for mid in (1, 2, 3):
        current(corpus, mid, [{"name": "キラナ", "subject": "patient"}])
    view = view_of(corpus)
    first = drug_timeline.timeline(view, 1, limit=1)
    second = drug_timeline.timeline(view, 1, limit=1, cursor=first["next_cursor"])
    assert first["items"][0]["message_id"] != second["items"][0]["message_id"]
    with pytest.raises(ValueError):
        drug_timeline.timeline(view, 1, name="キラナ", cursor=first["next_cursor"])


def test_withdrawn_coverage_is_patient_period_wide_and_does_not_change_page(corpus):
    from extract_testkit import _message

    for mid in (1, 2):
        current(corpus, mid, [{"name": "キラナ", "subject": "patient"}])
    _seed(corpus, mid=7)
    corpus.db.execute("UPDATE messages SET body_state='snippet' WHERE message_id=7")
    ts = corpus.db.execute("SELECT posted_at_ts FROM messages WHERE message_id=1").fetchone()[0]
    view = view_of(corpus)
    kwargs = {"name": "キラナ", "since": ts - 5, "until": ts + 5, "limit": 1}
    before = drug_timeline.timeline(view, 1, **kwargs)
    before_next = drug_timeline.timeline(view, 1, cursor=before["next_cursor"], **kwargs)

    for mid, value in ((3, ts), (4, ts - 100), (5, None)):
        _seed(corpus, mid=mid, names=("合成非表示の薬",))
        corpus.db.execute("UPDATE messages SET body_state='deleted', posted_at_ts=? WHERE message_id=?",
                          (value, mid))
    corpus.ensure_patient(2)
    corpus.save_messages([_message(mid=6, project_id=2, body="別患者の合成本文")])
    corpus.db.execute("UPDATE messages SET body_state='deleted', posted_at_ts=? WHERE message_id=6", (ts,))
    corpus.save_messages([_message(mid=8, project_id=2, body="別患者の時刻不明")])
    corpus.db.execute("UPDATE messages SET body_state='deleted', posted_at_ts=NULL WHERE message_id=8")
    corpus.db.commit()
    first = drug_timeline.timeline(view, 1, **kwargs)
    second = drug_timeline.timeline(view, 1, cursor=first["next_cursor"], **kwargs)
    assert first["items"] == before["items"] and second["items"] == before_next["items"]
    assert first["next_cursor"] == before["next_cursor"]
    assert first["coverage"]["stored_posts"] == 3
    assert first["coverage"]["incomplete_bodies"] == 1
    assert first["coverage"]["withdrawn_posts"] == second["coverage"]["withdrawn_posts"] == 1
    assert first["coverage"]["withdrawn_time_unknown"] == second["coverage"]["withdrawn_time_unknown"] == 1
    unbounded = drug_timeline.timeline(view, 1)
    assert unbounded["coverage"]["withdrawn_posts"] == 3
    assert unbounded["coverage"]["withdrawn_time_unknown"] == 1
    assert "削除された投稿あり" in first["explanation"]
    assert "削除された投稿の時刻不明" in first["explanation"]
    assert "合成非表示の薬" not in json.dumps(first, ensure_ascii=False)
    assert first["candidate_only"] is True
    assert all(item["change"]["current_use_confirmed"] is False for item in first["items"])


def test_only_unknown_time_deleted_still_warns_with_period_filter(corpus):
    _seed(corpus, mid=1, names=("合成非表示の薬",))
    corpus.db.execute("UPDATE messages SET body_state='deleted', posted_at_ts=NULL WHERE message_id=1")
    report = drug_timeline.timeline(view_of(corpus), 1, since=1, until=2)
    assert report["coverage"]["withdrawn_posts"] == 0
    assert report["coverage"]["withdrawn_time_unknown"] == 1
    assert "削除された投稿の時刻不明" in report["explanation"]
    assert "合成非表示の薬" not in json.dumps(report, ensure_ascii=False)


def test_frozen_context_frame_and_digest_are_required(corpus):
    import extraction_context
    current(corpus, 1, [{"name": "キラナ", "subject": "unknown"}])
    message = dict(corpus.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    message["_source_binding"] = source_binding(corpus.db, 1, 1)
    frame = extraction_context.prepare(corpus, message, model=extract_llm.MODEL,
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


@pytest.mark.parametrize("binding", [None, "f" * 64])
def test_timeline_requires_the_prepared_source_binding(corpus, binding):
    current(corpus, 1, [{"name": "キラナ", "subject": "patient"}])
    assert drug_timeline.timeline(view_of(corpus), 1)["items"]
    aid = corpus.db.execute("SELECT MAX(artifact_id) FROM artifacts WHERE kind='extract_llm'").fetchone()[0]
    corpus.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.source_binding',?) WHERE artifact_id=?",
                      (binding, aid))
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
    from medications_testkit import _dictionary
    current(corpus, 1, [{"name": "キラナ", "subject": "patient"}])
    drug_map.derive(corpus, _dictionary(tmp_path))
    assert drug_timeline.timeline(view_of(corpus), 1)["items"][0]["candidate"]
    report = drug_timeline.timeline(view_of(corpus), 1,
        cfg={"drug_map": {"path": str(tmp_path / "new.json"), "sha256": "f" * 64}})
    assert report["items"][0]["candidate"] is None
    assert report["coverage"]["dictionary"] == "pending" and "照合中" in report["explanation"]


def test_timeline_reuses_dictionary_progress_in_its_borrowed_snapshot(corpus, tmp_path, monkeypatch):
    import drug_map
    from medications_testkit import _dictionary
    for mid in (1, 2, 3):
        current(corpus, mid, [{"name": "キラナ", "subject": "patient"}])
    drug_map.derive(corpus, _dictionary(tmp_path))
    read_progress, reads = drug_map.read_annotation_progress, []
    def counted(db):
        reads.append(db)
        return read_progress(db)
    monkeypatch.setattr(drug_map, "read_annotation_progress", counted)
    report = drug_timeline.timeline(view_of(corpus), 1)
    assert reads == [corpus.db]
    assert {item["message_id"] for item in report["items"]} == {1, 2, 3}
    assert all(item["candidate"]["status"] == "resolved" for item in report["items"])
    assert report["coverage"]["matching_pending_on_page"] == 0


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
    monkeypatch.setattr("drug_map._source", lambda *a, **k: (row[0], "semantic_facts_v4", "{}", "synthetic", 1))
    monkeypatch.setattr("semantic_publication.reproject_doc", lambda *a: (doc, None))
    monkeypatch.setattr("semantic_store.thread_bundle", lambda *a, **k: {"source_fingerprint": "synthetic"})
    monkeypatch.setattr("semantic_policy.semantic_config", lambda *a, **k: ({}, []))
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
