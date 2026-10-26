"""Synthetic source/time/coverage boundaries for medication comparison and changes."""
import json

import pytest

import drug_map
import drug_reconciliation
import drug_timeline
import test_drug_timeline as timeline_kit
from test_drug_timeline import current, view_of
from medications_testkit import _dictionary, _source_document
from project_metadata import ARTIFACT_KIND, normalize_rows


corpus = timeline_kit.corpus
NOW = 1790000000


def structured(store, rows, *, complete=True, at=NOW):
    store.db.execute("UPDATE patients SET project_type='medical',karte_id=10 WHERE project_id=1")
    payload = {"contract": "project-metadata/1", "dataset": "medication_periods",
               "scope": "karte", "entity_id": 10, "item_id": None,
               "complete": complete, "reason": None if complete else "fetch_error",
               "http_status": None, "attempted_at": at, "names_retained": False,
               "rows": normalize_rows("medication_periods", rows), "definition": None}
    store.artifact_add(ARTIFACT_KIND, json.dumps(payload), project_id=1)


def period(name):
    return {"begin_date": "2026-01-01", "end_date": "2026-01-31",
            "medicine_informations": [{"id": 66, "name": name}]}


def med(name="キラナ錠5mg", **values):
    return {"name": name, "subject": "patient", "status": "current",
            "action": "start", "negated": False, **values}


@pytest.mark.parametrize("action", ["start", "stop", "increase", "decrease", "change"])
def test_change_actions_never_establish_treatment(corpus, action):
    current(corpus, 1, [med(action=action), med(action=action, status="planned"),
                        med(action=action, status="past", subject="family")])
    result = drug_timeline.timeline(view_of(corpus), 1)
    rows = [item["change"] for item in result["items"]]
    assert [r["action"] for r in rows] == [action] * 3
    assert rows[0]["implementation"] == "not_established"
    assert rows[1]["implementation"] == "planned"
    assert rows[2]["subject"] == "family" and rows[2]["time_scope"] == "past"
    assert all(r["prescription_authority"] is False and r["current_use_confirmed"] is False for r in rows)


def test_canonical_performed_and_negation_are_distinct():
    base = {"action": "stop", "workflow_status": "performed", "polarity": "affirmed",
            "event_time": "2026-01-03", "epistemic": "reported"}
    assert drug_timeline.change_details(base, "patient", None)["implementation"] == "performed_report"
    assert drug_timeline.change_details({**base, "polarity": "negated"}, "patient", None)["implementation"] == "not_established"
    assert drug_timeline.change_details(base, "patient", None)["time_scope"] == "unknown"


def test_alias_search_and_code_groups_use_current_pin(corpus, tmp_path):
    current(corpus, 1, [med("キラナ"), med("別キラナ", status="planned"), med("共通架空")])
    document = _source_document()
    document["entries"][1]["aliases"] = ["キラナ", "別キラナ", "キラーナ"]
    dictionary = _dictionary(tmp_path, document)
    drug_map.derive(corpus, dictionary)
    cfg = {"drug_map": {"path": str(tmp_path / "fictional-drug-map.json"), "sha256": dictionary.sha256}}
    result = drug_timeline.timeline(view_of(corpus), 1, name="キラーナ", cfg=cfg)
    assert len(result["items"]) == 2
    assert len(result["drug_groups"]) == 1
    assert result["drug_groups"][0]["item_indices"] == [0, 1]
    cfg["drug_map"]["sha256"] = "f" * 64
    assert drug_timeline.timeline(view_of(corpus), 1, name="キラーナ", cfg=cfg)["items"] == []


def test_strength_difference_keeps_structured_dates_and_dose_separate(corpus, monkeypatch):
    monkeypatch.setattr("project_metadata_view.time.time", lambda: NOW)
    current(corpus, 1, [med(dose="10mg")])
    structured(corpus, [period("キラナ錠10mg")])
    result = drug_reconciliation.reconcile(view_of(corpus), 1)
    item = result["items"][0]
    assert item["status"] == "review"
    comparison = item["comparisons"][0]
    assert "strength_difference" in comparison["comparison"]["reasons"]
    assert comparison["structured"]["begin_date"] == "2026-01-01"
    assert comparison["structured"]["end_date"] == "2026-01-31"
    assert comparison["structured"]["information_acquired_at"] == NOW
    assert "unit" in comparison["comparison"]["missing"]
    assert result["structured"]["current_known"] is True
    assert result["clinical_equivalence_confirmed"] is False


def test_subject_plan_past_and_missing_metadata_are_not_current_prescription(corpus):
    current(corpus, 1, [med(subject="family"), med(subject="unknown"),
                        med(status="planned"), med(status="past")])
    result = drug_reconciliation.reconcile(view_of(corpus), 1)
    assert [i["status"] for i in result["items"]] == ["not_comparable", "not_comparable", "unknown", "unknown"]
    assert "planned_mention" in result["items"][2]["context"]
    assert "historical_mention" in result["items"][3]["context"]
    assert result["structured"]["state"] == "unknown"
    assert result["absence_confirmed"] is False


def test_empty_structured_is_only_snapshot_name_absence(corpus, monkeypatch):
    monkeypatch.setattr("project_metadata_view.time.time", lambda: NOW)
    current(corpus, 1, [med()])
    structured(corpus, [])
    result = drug_reconciliation.reconcile(view_of(corpus), 1)
    assert result["structured"]["state"] == "empty"
    assert result["items"][0]["reason"] == "name_not_observed_in_structured_snapshot"
    assert result["items"][0]["absence_confirmed"] is False


def test_stale_structured_and_unextracted_posts_remain_visible(corpus, monkeypatch):
    monkeypatch.setattr("project_metadata_view.time.time", lambda: NOW + 90000)
    current(corpus, 1, [med()])
    current(corpus, 2, [med()])
    corpus.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.plan_version',-1) WHERE message_id=2")
    structured(corpus, [period("キラナ錠5mg")])
    result = drug_reconciliation.reconcile(view_of(corpus), 1)
    assert result["structured"]["state"] == "stale"
    assert "structured_not_current" in result["items"][0]["context"]
    assert result["coverage"]["no_current_extraction"] == 1
    assert result["items"][0]["status"] == "unknown"  # independently missing unit


def test_missing_structured_name_does_not_claim_name_absence(corpus, monkeypatch):
    monkeypatch.setattr("project_metadata_view.time.time", lambda: NOW)
    current(corpus, 1, [med()])
    structured(corpus, [period(None)])
    report = drug_reconciliation.reconcile(view_of(corpus), 1)
    assert report["items"][0]["reason"] == "structured_names_missing"
    assert report["items"][0]["status"] == "unknown"
    assert report["structured"]["missing_names"] == 1


def test_reconciliation_preserves_page_cursor_and_is_read_only(corpus, monkeypatch):
    monkeypatch.setattr("project_metadata_view.time.time", lambda: NOW)
    for mid in (1, 2):
        current(corpus, mid, [med()])
    structured(corpus, [period("キラナ錠5mg")])
    corpus.db.commit()
    before = corpus.db.total_changes
    view = view_of(corpus)
    first = drug_reconciliation.reconcile(view, 1, limit=1)
    second = drug_reconciliation.reconcile(view, 1, limit=1, cursor=first["next_cursor"])
    assert first["items"][0]["message_id"] != second["items"][0]["message_id"]
    assert corpus.db.total_changes == before
    assert first["comparison_scope"] == "stored_post_page"


def test_reconciliation_reports_deleted_posts_without_disclosing_their_contents(corpus):
    current(corpus, 1, [med()])
    timeline_kit._seed(corpus, mid=2, names=("合成非表示の薬",))
    corpus.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=2")
    corpus.db.commit()
    view = view_of(corpus)
    before = corpus.db.total_changes
    timeline = drug_timeline.timeline(view, 1)
    report = drug_reconciliation.reconcile(view, 1)
    assert report["coverage"] == timeline["coverage"]
    assert report["coverage"]["withdrawn_posts"] == 1
    assert "削除された投稿あり" in report["explanation"]
    assert "合成非表示の薬" not in json.dumps(report, ensure_ascii=False)
    assert report["candidate_only"] is True and report["current_use_confirmed"] is False
    assert report["absence_confirmed"] is False
    assert corpus.db.total_changes == before


def test_reconciliation_warns_for_deleted_unknown_time_with_period(corpus):
    timeline_kit._seed(corpus, mid=1, names=("合成非表示の薬",))
    corpus.db.execute("UPDATE messages SET body_state='deleted', posted_at_ts=NULL WHERE message_id=1")
    report = drug_reconciliation.reconcile(view_of(corpus), 1, since=1, until=2)
    assert report["coverage"]["withdrawn_posts"] == 0
    assert report["coverage"]["withdrawn_time_unknown"] == 1
    assert "削除された投稿の時刻不明" in report["explanation"]
    assert "合成非表示の薬" not in json.dumps(report, ensure_ascii=False)


def test_legacy_ingredient_alias_does_not_expand_official_identity(corpus, tmp_path):
    current(corpus, 1, [med("キラナ"), med("別キラナ")])
    dictionary = _dictionary(tmp_path)
    drug_map.derive(corpus, dictionary)
    cfg = {"drug_map": {"path": str(tmp_path / "fictional-drug-map.json"), "sha256": dictionary.sha256}}
    result = drug_timeline.timeline(view_of(corpus), 1, name="キラナ", cfg=cfg)
    assert len(result["items"]) == 1
    assert result["coverage"]["name_matching"] == "exact_only"
    assert result["drug_groups"][0]["identity"] is None


def test_cli_reconcile_reads_only_published_synthetic_snapshot(corpus, tmp_path, monkeypatch, capsys):
    import ledger
    import mcs_drug
    monkeypatch.setattr("project_metadata_view.time.time", lambda: NOW)
    current(corpus, 1, [med()])
    structured(corpus, [period("キラナ錠10mg")])
    corpus.db.commit()
    snapshot = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snapshots"))
    monkeypatch.setattr(mcs_drug, "load_config", lambda: {})
    assert mcs_drug.main(["reconcile", "--project", "1", "--snapshot", snapshot, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "drug_reconciliation"
    assert result["items"][0]["reason"] == "surface_difference"
    assert result["read_only"] is True


def test_failed_refresh_keeps_historical_information_time(corpus, monkeypatch):
    monkeypatch.setattr("project_metadata_view.time.time", lambda: NOW + 10)
    current(corpus, 1, [med()])
    structured(corpus, [period("キラナ錠10mg")])
    structured(corpus, [], complete=False, at=NOW + 5)
    result = drug_reconciliation.reconcile(view_of(corpus), 1)
    assert result["structured"]["state"] == "failed"
    assert result["structured"]["last_complete_at"] == NOW
    assert result["structured"]["attempted_at"] == NOW + 5
    assert result["structured"]["historical"] is True
    assert "structured_not_current" in result["items"][0]["context"]
    assert result["items"][0]["comparisons"][0]["structured"]["information_acquired_at"] == NOW


@pytest.mark.parametrize('epistemic', ['unknown', 'speculated', None, 'asserted', 'reported'])
def test_change_completion_preserves_epistemic(epistemic):
    result = drug_timeline.change_details({'action': 'stop', 'workflow_status': 'performed',
        'polarity': 'affirmed', 'epistemic': epistemic}, 'patient', None)
    assert result['epistemic'] == epistemic
    assert result['implementation'] == ('performed_report' if epistemic in ('asserted', 'reported')
                                        else 'not_established')


@pytest.mark.parametrize("grounded", [True, False])
def test_canonical_reconciliation_uses_grounded_name_or_retains_unknown(corpus, monkeypatch, grounded):
    timeline_kit.canonical_source(corpus, monkeypatch, grounded=grounded)
    monkeypatch.setattr("project_metadata_view.time.time", lambda: NOW)
    structured(corpus, [period("キラナ錠10mg")])
    result = drug_reconciliation.reconcile(view_of(corpus), 1)
    item = result["items"][0]
    if grounded:
        assert item["reason"] == "surface_difference"
        assert "strength_difference" in item["comparisons"][0]["comparison"]["reasons"]
    else:
        assert item["status"] == "unknown"
        assert item["reason"] == "mention_name_not_established"
        assert not item["comparisons"]
    assert item["absence_confirmed"] is False
