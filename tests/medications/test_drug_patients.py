"""Synthetic cross-patient medication mention search contracts."""
import json
from types import SimpleNamespace

import pytest

import drug_map
import drug_patients
import extract_llm
import ledger
import mcs_view
from extract_testkit import _message
from inference_dependency_loss import source_binding
from inference_version import CURRENT_VERSION, VERSION_KEY
from medications_testkit import _dictionary


def current(store, pid, mid, names, *, posted="2026-11-01T09:00:00+09:00"):
    body = "".join(f"本人は{name}を開始し服用しています。" for name in names) or "完全合成の記録です。"
    store.save_messages([_message(mid=mid, project_id=pid, body=body, posted_at=posted)])
    message = dict(store.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())
    message["_source_binding"] = source_binding(store.db, pid, mid)
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
    meds = [{"name": name, "subject": "patient", "action": "start", "status": "current"} for name in names]
    store.artifact_add("extract_llm", json.dumps({"meds": meds}), project_id=pid,
                       message_id=mid, model=extract_llm.MODEL, meta=meta)


@pytest.fixture
def corpus(tmp_path):
    store = ledger.Ledger(str(tmp_path / "ledger.db"))
    for pid in (1, 2, 3):
        store.ensure_patient(pid)
    yield store
    store.close()


def view_of(store):
    return SimpleNamespace(db=store.db, reader=store, meta={"generation_id": "synthetic-1", "generated_at": 1},
                           _page=lambda *args: mcs_view.View._page(view_of(store), *args))


def test_lists_only_patients_with_a_mention_newest_first(corpus):
    current(corpus, 1, 11, ["キラナ"], posted="2026-11-01T09:00:00+09:00")
    current(corpus, 2, 21, ["キラナ"], posted="2026-11-03T09:00:00+09:00")
    current(corpus, 2, 22, ["キラナ", "別薬"], posted="2026-11-02T09:00:00+09:00")
    current(corpus, 3, 31, ["別薬"])
    report = drug_patients.find(view_of(corpus), name="キラナ")
    assert [item["project_id"] for item in report["items"]] == [2, 1]
    second = report["items"][0]
    assert second["mentions"] == 2 and second["posts"] == 2
    assert second["latest"]["message_id"] == 21 and second["latest"]["change"] == "開始の記載"
    assert all(item["current_use_confirmed"] is False for item in report["items"])
    assert report["candidate_only"] and report["read_only"]
    assert "服用中の確定ではありません" in report["explanation"]


def test_unextracted_posts_are_reported_not_treated_as_absence(corpus):
    current(corpus, 1, 11, ["キラナ"])
    corpus.save_messages([_message(mid=12, project_id=3, body="合成の未抽出投稿です。")])
    report = drug_patients.find(view_of(corpus), name="キラナ")
    assert report["total"] == 1
    assert report["coverage"]["no_current_extraction"] == 1
    assert report["coverage"]["patients_with_unextracted_posts"] == 1
    assert "未抽出1投稿" in report["explanation"]


def test_archived_patients_are_skipped_unless_requested(corpus):
    current(corpus, 1, 11, ["キラナ"])
    corpus.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    assert drug_patients.find(view_of(corpus), name="キラナ")["total"] == 0
    report = drug_patients.find(view_of(corpus), name="キラナ", include_archived=True)
    assert report["total"] == 1 and report["items"][0]["archived"] is True
    assert report["coverage"]["archived_skipped"] == 0


def test_identities_match_stored_candidates_of_other_names(corpus, tmp_path):
    current(corpus, 1, 11, ["キラーナ"])
    current(corpus, 2, 21, ["別薬"])
    drug_map.derive(corpus, _dictionary(tmp_path))
    report = drug_patients.find(view_of(corpus), identities={"fiction-i1"})
    assert [item["project_id"] for item in report["items"]] == [1]
    assert report["query"]["name_matching"] == "exact_or_identity"


def test_limit_truncates_but_keeps_total(corpus):
    for pid in (1, 2, 3):
        current(corpus, pid, pid * 10, ["キラナ"])
    report = drug_patients.find(view_of(corpus), name="キラナ", limit=2)
    assert report["returned"] == 2 and report["total"] == 3 and report["truncated"]


@pytest.mark.parametrize("kwargs", [{}, {"name": "a", "identities": {"x"}}, {"name": ""},
                                    {"identities": [""]}, {"name": "a", "limit": 0}])
def test_arguments_are_strict(corpus, kwargs):
    with pytest.raises(ValueError):
        drug_patients.find(view_of(corpus), **kwargs)


def test_master_impact_and_formulary_map_to_dictionary_identities(tmp_path):
    from medications_testkit import _source_document
    active = _dictionary(tmp_path, _source_document())
    report = {"schema": "mcs-drug-master-impact/1", "items": [{"medicine": "900000002"}]}
    assert drug_patients.master_identities(active, report) == {"mhlw:general:GEN000000001"}
    assert drug_patients.master_identities(None, report) == set()
    with pytest.raises(ValueError):
        drug_patients.master_identities(active, {"items": []})
    watch = {"inserts": [{"name": "架空製品錠5mg", "last": "changed", "aliases": []},
                         {"name": "架空ブランド", "last": "unchanged"}]}
    codes, names = drug_patients.formulary_identities(active, watch)
    assert names == ["架空製品錠5mg"] and codes == {"mhlw:medicine:900000001"}
