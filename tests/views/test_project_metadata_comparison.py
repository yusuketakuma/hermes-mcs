"""Optional comparison over real isolated snapshots, with fictional independent sources."""

from contextlib import contextmanager
from datetime import datetime
import json

import pytest

from extract_testkit import _hash, _message
from ledger import Ledger, publish_snapshot
from mcs_view import View
from project_metadata import ARTIFACT_KIND, normalize_rows
import project_metadata_view as metadata
from semantic_projection import PROJECTION_VERSION


POSTED = "2026-10-04T12:00:00+09:00"
NOW = datetime.fromisoformat(POSTED).timestamp()
SERVER_MED_NAME = "fiction-server-med"
MED = {"begin_date": "2026-10-01", "end_date": None,
       "medicine_informations": [{"id": 66, "name": SERVER_MED_NAME}]}
LAB = {"lab_test_item": {"id": 55, "name": "fiction-server-analyte",
                        "analyte_tag": "fiction-tag", "input_type": "scalar", "unit": "unit-a"}}
CHAT_MED = {"name": "fiction-chat-med", "dose": "fiction-dose", "action": "stop",
            "status": "past", "subject": "patient", "negated": False, "unverified": True}
CHAT_LAB = {"name": "fiction-chat-analyte", "value": 23, "unit": "unit-b",
            "normalized": {"measured_on": "2026-10-02", "confirmation": "quote_supported"}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("ledger.time.time", lambda: NOW)
    db = Ledger(str(tmp_path / "fictional.db"))
    for pid, kind, kid in ((1, "medical", 10), (2, "group", None), (3, "medical", None)):
        db.ensure_patient(pid)
        db.db.execute("UPDATE patients SET project_type=?,karte_id=? WHERE project_id=?",
                      (kind, kid, pid))
    db.db.commit()
    yield db
    db.close()


def _structured(store, dataset="medication_periods", *, rows=None, complete=True,
                reason=None, attempted_at=NOW, definition=None, item_id=None, pid=1,
                entity_id=10):
    if rows is None:
        rows = [MED] if dataset == "medication_periods" else [LAB] \
            if dataset == "observation_items" else [{"scalar": 23, "observation_issued_at": None}]
    scope = "project" if dataset == "care_team" else (
        "group" if dataset in ("consultations", "self_consultations") else "karte")
    payload = {"contract": "project-metadata/1", "dataset": dataset,
               "scope": scope, "entity_id": entity_id, "item_id": item_id,
               "complete": complete, "reason": reason, "http_status": None,
               "attempted_at": attempted_at, "names_retained": False,
               "rows": normalize_rows(dataset, rows) if complete else rows,
               "definition": normalize_rows("observation_items", [definition])[0]
               if definition is not None else None}
    return store.artifact_add(ARTIFACT_KIND, json.dumps(payload), project_id=pid)


def _chat(store, *, mid=1, pid=1, kind="extract_llm", content=None, meta=None,
          posted_at=POSTED):
    store.save_messages([_message(mid=mid, project_id=pid,
                                  body="BODY_CANARY entirely fictional source",
                                  posted_at=posted_at)])
    return store.artifact_add(
        kind, json.dumps(content if content is not None else
                         {"meds": [CHAT_MED], "labs": [CHAT_LAB],
                          "summary": "SUMMARY_CANARY"}),
        project_id=pid, message_id=mid,
        meta={"hash": _hash(store, mid), "engine_version": 4,
              **({"projection_version": PROJECTION_VERSION}
                 if kind in ("canonical_projection", "semantic_facts_v4") else {}), **(meta or {})})


@contextmanager
def _snapshot(store, tmp_path):
    source = store.db.execute("PRAGMA database_list").fetchone()[2]
    path = publish_snapshot(source, str(tmp_path / "snapshots"))
    assert path is not None
    view = View(path)
    try:
        yield view
    finally:
        view.close()


def _read(store, tmp_path, dataset="medication_periods", **options):
    with _snapshot(store, tmp_path) as view:
        return metadata.get_project_metadata(
            view.db, 1, dataset, now=view.meta["generated_at"], **options)


def test_snapshot_shows_independent_medication_sources_without_resolving_identity(store, tmp_path):
    structured_id = _structured(store)
    chat_id = _chat(store)
    result = _read(store, tmp_path, include_chat=True)
    assert result["state"] == "complete" and result["current_known"] is True
    assert result["rows"] == normalize_rows("medication_periods", [MED])
    assert result["structured_provenance"] == {
        "source": "mcs_structured", "dataset": "medication_periods", "entity_id": 10,
        "item_id": None, "attempt_artifact_id": structured_id, "complete_artifact_id": structured_id}
    candidate = result["chat_candidates"]["rows"][0]
    assert candidate["name"] == CHAT_MED["name"] and candidate["action"] == "stop"
    assert candidate["status"] == "past" and candidate["candidate"] is True
    assert candidate["confirmation"] == "unconfirmed" and candidate["source_unverified"] is True
    assert candidate["provenance"]["source_artifact_id"] == chat_id
    assert candidate["provenance"]["content_hash"] == _hash(store)
    assert candidate["provenance"]["generation_current"] is True
    assert result["comparison"]["relationship"] == "unconfirmed"
    assert result["comparison"]["authority"] == "not_assigned"
    serialized = json.dumps(result)
    assert "BODY_CANARY" not in serialized and "SUMMARY_CANARY" not in serialized


def test_primary_snapshot_api_and_cli_forward_explicit_comparison(store, tmp_path, capsys):
    from mcs_view import main

    _structured(store)
    _chat(store)
    with _snapshot(store, tmp_path) as view:
        result = view.read("project_metadata", project=1, dataset="medication_periods",
                           publication=True, include_chat=True)
        assert result["chat_candidates"]["rows"][0]["name"] == CHAT_MED["name"]
        assert result["comparison"]["authority"] == "not_assigned"
        snapshot = view.db.execute("PRAGMA database_list").fetchone()[2]
    assert main(["--snapshot", snapshot, "project_metadata", "--project", "1",
                 "--dataset", "medication_periods", "--publication", "--include-chat"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["chat_candidates"]["rows"][0]["name"] == CHAT_MED["name"]
    assert payload["comparison"]["relationship"] == "unconfirmed"
    assert "BODY_CANARY" not in json.dumps(payload)


def test_primary_comparison_does_not_bypass_publication(store, tmp_path, monkeypatch):
    with _snapshot(store, tmp_path) as view:
        def forbidden(*args, **kwargs):
            pytest.fail("disabled publication must not inspect candidate sources")
        monkeypatch.setattr(metadata, "get_project_metadata", forbidden)
        result = view.read("project_metadata", project=1, dataset="medication_periods",
                           include_chat=True)
        assert result["state"] == "publication_disabled"
        assert "chat_candidates" not in result
        for kind, dataset, include_chat in (
                ("project_metadata", "care_team", True),
                ("project_metadata", "consultations", True),
                ("cross_lists", "mentioned", True),
                ("project_metadata", "medication_periods", "true")):
            with pytest.raises(ValueError, match="bad_comparison_scope"):
                view.read(kind, project=1 if kind == "project_metadata" else None,
                          dataset=dataset, publication=True, include_chat=include_chat)


def test_same_label_still_does_not_associate_master_id_with_chat(store, tmp_path):
    _structured(store)
    _chat(store, content={"meds": [{**CHAT_MED, "name": SERVER_MED_NAME}]})
    result = _read(store, tmp_path, include_chat=True)
    assert result["comparison"]["matching"] == "not_performed"
    assert result["chat_candidates"]["item_association"] == "not_inferred"
    assert "id" not in result["chat_candidates"]["rows"][0]
    assert result["rows"][0]["medicine_informations"][0]["id"] == 66


@pytest.mark.parametrize("dataset", ["observation_items", "observation_values"])
def test_units_dates_and_master_item_stay_separate(store, tmp_path, dataset):
    options = {"item_id": 55} if dataset == "observation_values" else {}
    values = [{"max": 120, "min": None, "observation_issued_at": "2026-10-04T10:00:00+09:00"}]
    _structured(store, dataset, rows=values if options else [LAB],
                definition=LAB if options else None, item_id=55 if options else None)
    _chat(store)
    result = _read(store, tmp_path, dataset, include_chat=True, **options)
    definition = result["definition"] if options else result["rows"][0]
    assert definition["lab_test_item"]["id"] == 55
    assert definition["lab_test_item"]["unit"] == "unit-a"
    candidate = result["chat_candidates"]["rows"][0]
    assert candidate["kind"] == "lab" and candidate["unit"] == "unit-b"
    assert candidate["value"] == 23 and candidate["measured_on"] == "2026-10-02"
    assert candidate["confirmation"] == "unconfirmed"
    assert result["comparison"]["relationship"] == "unconfirmed"
    if options:
        assert result["rows"][0]["max"] == 120 and result["rows"][0]["min"] is None
        assert result["rows"][0]["scalar"] is None
        assert result["rows"][0]["observation_issued_at"] == "2026-10-04T10:00:00+09:00"


def test_unknown_sampling_date_is_not_filled_from_message_posting(store, tmp_path):
    _structured(store, "observation_values", item_id=55, definition=LAB)
    _chat(store, content={"labs": [{"name": "fiction-chat", "value": 0, "unit": None}]})
    result = _read(store, tmp_path, "observation_values", item_id=55, include_chat=True)
    assert result["rows"][0]["observation_issued_at"] is None
    candidate = result["chat_candidates"]["rows"][0]
    assert candidate["measured_on"] is None and candidate["unit"] is None
    assert candidate["value"] == 0
    assert candidate["provenance"]["posted_at"] == POSTED


@pytest.mark.parametrize("invalid_date", ["DATE_PRIVATE_CANARY", "2026-13-40", 123, {}])
def test_invalid_persisted_sampling_date_is_unknown_not_free_text(store, tmp_path, invalid_date):
    _structured(store, "observation_items")
    _chat(store, content={"labs": [{**CHAT_LAB, "normalized": {"measured_on": invalid_date}}]})
    result = _read(store, tmp_path, "observation_items", include_chat=True)
    assert result["chat_candidates"]["rows"][0]["measured_on"] is None
    assert "DATE_PRIVATE_CANARY" not in json.dumps(result)


@pytest.mark.parametrize("kind", ["canonical_projection", "semantic_facts_v4"])
def test_selected_canonical_generation_shadows_legacy_and_rules(store, tmp_path, kind):
    _structured(store)
    _chat(store, content={"meds": [{"name": "LEGACY_CANARY"}]})
    _chat(store, kind="extract_v1", content={"medications": [{"name": "RULE_CANARY"}]})
    chosen = _chat(store, kind=kind, content={"meds": [{"name": "fiction-canonical"}]})
    result = _read(store, tmp_path, include_chat=True)
    rows = result["chat_candidates"]["rows"]
    assert [row["name"] for row in rows] == ["fiction-canonical"]
    assert rows[0]["provenance"]["source_kind"] == kind
    assert rows[0]["provenance"]["source_artifact_id"] == chosen
    assert "CANARY" not in json.dumps(result)


def test_empty_canonical_generation_never_resurrects_old_chat_rows(store, tmp_path):
    _structured(store)
    _chat(store, content={"meds": [{"name": "OLD_CANARY"}]})
    _chat(store, kind="canonical_projection", content={"meds": []})
    result = _read(store, tmp_path, include_chat=True)
    assert result["chat_candidates"]["rows"] == []
    assert result["chat_candidates"]["state"] == "empty"
    assert result["chat_candidates"]["absence_confirmed"] is False


def test_rule_only_medications_are_still_candidates_with_own_generation(store, tmp_path):
    _structured(store)
    source_id = _chat(store, kind="extract_v1",
                      content={"medications": [{"name": "fiction-rule-med", "dose": None}]})
    result = _read(store, tmp_path, include_chat=True)
    row = result["chat_candidates"]["rows"][0]
    assert row["source_unverified"] is True and row["subject"] is None
    assert row["provenance"]["source_artifact_id"] == source_id


@pytest.mark.parametrize("problem", ["hash", "deleted", "error_meta", "error_content",
                                    "wrong_engine", "invalidated"])
def test_unusable_chat_generations_are_unknown_not_absence(store, tmp_path, problem):
    _structured(store)
    kind = "semantic_facts_v4" if problem == "wrong_engine" else (
        "canonical_projection" if problem == "invalidated" else "extract_llm")
    content = {"meds": [{"name": "UNUSABLE_CANARY"}],
               **({"_error": True} if problem == "error_content" else {})}
    meta = {"hash": "other-revision"} if problem == "hash" else (
        {"error": True} if problem == "error_meta" else (
            {"engine_version": 3} if problem == "wrong_engine" else (
                {"invalidated": True} if problem == "invalidated" else {})))
    _chat(store, kind=kind, content=content, meta=meta)
    if problem == "deleted":
        store.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
        store.db.commit()
    result = _read(store, tmp_path, include_chat=True)
    assert result["chat_candidates"]["state"] == "unknown"
    assert result["chat_candidates"]["rows"] == []
    assert result["chat_candidates"]["absence_confirmed"] is False
    assert "UNUSABLE_CANARY" not in json.dumps(result)


def test_new_same_hash_generation_changes_provenance_without_mutating_previous_snapshot(store, tmp_path):
    _structured(store)
    first = _chat(store, content={"meds": [{"name": "fiction-first"}]})
    before = _read(store, tmp_path, include_chat=True)
    second = _chat(store, content={"meds": [{"name": "fiction-second"}]})
    after = _read(store, tmp_path, include_chat=True)
    assert before["chat_candidates"]["rows"][0]["provenance"]["source_artifact_id"] == first
    assert after["chat_candidates"]["rows"][0]["provenance"]["source_artifact_id"] == second
    assert [row["name"] for row in after["chat_candidates"]["rows"]] == ["fiction-second"]


@pytest.mark.parametrize("state", ["empty", "unknown", "stale", "failed", "partial"])
def test_structured_missing_stale_and_failed_do_not_inherit_chat_authority(store, tmp_path, state):
    previous = None
    if state == "empty":
        _structured(store, rows=[])
    elif state == "stale":
        _structured(store, attempted_at=NOW - 101)
    elif state in ("failed", "partial"):
        previous = _structured(store)
        _structured(store, complete=False, rows=[{"PARTIAL_CANARY": "not published"}],
                    reason="http_error" if state == "failed" else "page_limit")
    _chat(store)
    result = _read(store, tmp_path, include_chat=True, max_age_s=100)
    assert result["state"] == ("failed" if state == "partial" else state)
    assert result["comparison"]["relationship"] == "unconfirmed"
    assert result["comparison"]["absence_confirmed"] is False
    assert result["chat_candidates"]["rows"][0]["candidate"] is True
    if previous:
        assert result["historical"] is True and result["current_known"] is False
        assert result["structured_provenance"]["complete_artifact_id"] == previous
        assert "PARTIAL_CANARY" not in json.dumps(result)


def test_mismatched_structured_item_definition_cannot_claim_a_value(store, tmp_path):
    _structured(store, "observation_values", item_id=55,
                definition={**LAB, "lab_test_item": {**LAB["lab_test_item"], "id": 56}})
    _chat(store)
    result = _read(store, tmp_path, "observation_values", item_id=55, include_chat=True)
    assert result["state"] == "unknown" and result["reason"] == "artifact_invalid"
    assert result["rows"] == [] and result["definition"] is None
    assert result["structured_provenance"]["complete_artifact_id"] is None
    assert result["chat_candidates"]["item_association"] == "not_inferred"


def test_old_message_is_historical_even_with_current_extraction_hash(store, tmp_path):
    _structured(store)
    _chat(store, posted_at="2026-10-01T12:00:00+09:00")
    result = _read(store, tmp_path, include_chat=True, max_age_s=100)
    assert result["current_known"] is True
    assert result["chat_candidates"]["rows"][0]["provenance"]["stale"] is True
    assert result["chat_candidates"]["rows"][0]["provenance"]["generation_current"] is True


@pytest.mark.parametrize("dataset,item_id,bad", [
    ("medication_periods", None, {"meds": [None, {"name": {"PRIVATE_CANARY": 1}},
                                           {"name": "fiction", "subject": "family"}]}),
    ("medication_periods", None, {"meds": {"PRIVATE_CANARY": 1}}),
    ("observation_values", 55, {"labs": [{"name": "fiction", "value": True}]}),
    ("observation_values", 55, {"labs": [{"name": "fiction", "value": 2, "unit": {}}]}),
    ("observation_values", 55, {"labs": [{"name": "fiction", "value": float("inf")}]}),
    ("observation_values", 55, {"labs": [{"name": "fiction", "value": 10 ** 400}]}),
])
def test_unknown_candidate_shapes_are_excluded_safely(store, tmp_path, dataset, item_id, bad):
    _structured(store, dataset, item_id=item_id, definition=LAB if item_id else None)
    _chat(store, content=bad)
    options = {"item_id": item_id} if item_id else {}
    result = _read(store, tmp_path, dataset, include_chat=True, **options)
    assert result["chat_candidates"]["rows"] == []
    assert result["chat_candidates"]["state"] in ("partial", "unknown")
    assert "PRIVATE_CANARY" not in json.dumps(result)


def test_other_project_messages_and_unverified_karte_mapping_do_not_associate(store, tmp_path):
    _structured(store)
    _chat(store, pid=2, mid=2, content={"meds": [{"name": "OTHER_PROJECT_CANARY"}]})
    with _snapshot(store, tmp_path) as view:
        for pid in (1, 3):
            result = metadata.get_project_metadata(view.db, pid, "medication_periods",
                                                   include_chat=True, now=NOW)
            assert result["chat_candidates"]["rows"] == []
            assert "OTHER_PROJECT_CANARY" not in json.dumps(result)
        assert result["reason"] == "association_unverified"


def test_default_off_does_not_inspect_messages_or_candidate_sources(store, tmp_path):
    _structured(store)
    _chat(store, content={"meds": [{"name": "CHAT_PRIVATE_CANARY"}]})
    with _snapshot(store, tmp_path) as view:
        queries = []
        view.db.set_trace_callback(queries.append)
        default = metadata.get_project_metadata(view.db, 1, "medication_periods", now=NOW)
        explicit = metadata.get_project_metadata(view.db, 1, "medication_periods",
                                                 include_chat=False, now=NOW)
    assert default == explicit
    assert default["chat_comparison"] == "not_compared"
    assert "chat_candidates" not in default and "structured_provenance" not in default
    assert "CHAT_PRIVATE_CANARY" not in json.dumps(default)
    assert not any("messages" in query.lower() or "extract_llm" in query.lower()
                   or "canonical_projection" in query.lower() for query in queries)


def test_care_team_privacy_and_group_scope_are_unchanged_when_off(store, tmp_path):
    member = {"id": 44, "last_name": "NAME_CANARY", "first_name": "FIRST_CANARY"}
    _structured(store, "care_team", rows=[member], entity_id=1)
    _structured(store, "consultations", pid=2, entity_id=2, rows=[{"id": 77}])
    _chat(store)
    with _snapshot(store, tmp_path) as view:
        care = metadata.get_project_metadata(view.db, 1, "care_team", now=NOW)
        group = metadata.get_project_metadata(view.db, 2, "consultations", now=NOW)
        with pytest.raises(ValueError, match="metadata view options"):
            metadata.get_project_metadata(view.db, 2, "consultations", include_chat=True, now=NOW)
    assert "NAME_CANARY" not in json.dumps(care)
    assert group["scope"] == "group" and group["patient_association"] == "unknown"
    assert group["chat_comparison"] == "not_compared"


def test_explicit_comparison_limits_never_claim_full_coverage(store, tmp_path, monkeypatch):
    _structured(store)
    for mid in range(1, 4):
        _chat(store, mid=mid, content={"meds": [{"name": f"fiction-{mid}"}]})
    monkeypatch.setattr(metadata, "MAX_ROWS", 2)
    result = _read(store, tmp_path, include_chat=True)
    assert result["chat_candidates"]["truncated"] is True
    assert result["chat_candidates"]["messages_considered"] == 2
    assert result["chat_candidates"]["state"] == "partial"
    assert result["chat_candidates"]["absence_confirmed"] is False


@pytest.mark.parametrize("value", [1, "true", None])
def test_include_chat_requires_a_literal_boolean(store, tmp_path, value):
    with _snapshot(store, tmp_path) as view:
        with pytest.raises(ValueError, match="metadata view options"):
            metadata.get_project_metadata(view.db, 1, "medication_periods", include_chat=value)
