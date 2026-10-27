"""Fully synthetic bounded measurement reader and borrowed snapshot checks."""
from contextlib import closing
import json
import sqlite3

import pytest

import measurement_reader
import notify_views
from extract_testkit import _message
from test_measurement_sources import _setup, _save


def _seed(ledger):
    body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
    _save(ledger, 1, body, {"labs": [{"name": "Cr", "value": "1.0", "unit": "mg/dL",
          "condition": "空腹時", "evidence": body, "subject": "patient", "status": "current"}]},
          "2026-10-01T09:00:00+09:00")


def test_live_pages_skip_unverified_without_losing_older_source(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        ledger.save_messages([_message(mid=2, body="合成の未解析投稿")])
        ledger.db.execute("BEGIN")
        first = measurement_reader.read_live_page(ledger.db, 1, cfg={}, now=1800000000)
        assert first["items"] == [] and first["next_cursor"] is not None
        second = measurement_reader.read_live_page(ledger.db, 1, cfg={}, now=1800000000,
                                                    cursor=first["next_cursor"])
        assert [item["value"] for item in second["items"]] == ["1.0"]
        assert second["relations"] == "not_assessed" and second["source_complete"] is False
        assert ledger.db.in_transaction
        ledger.db.rollback()


def test_live_cursor_rejects_changed_boundary_and_patient_scope(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        ledger.save_messages([_message(mid=2, body="合成の未解析投稿")])
        ledger.db.execute("BEGIN")
        first = measurement_reader.read_live_page(ledger.db, 1, cfg={}, now=1800000000)
        ledger.db.execute("UPDATE messages SET body_text='訂正された合成本文' WHERE message_id=2")
        with pytest.raises(ValueError, match="measurement_cursor_changed"):
            measurement_reader.read_live_page(ledger.db, 1, cfg={}, now=1800000000,
                                              cursor=first["next_cursor"])
        ledger.db.execute("UPDATE patients SET fetch_state='forbidden' WHERE project_id=1")
        refused = measurement_reader.read_live_page(ledger.db, 1, cfg={}, now=1800000000)
        assert refused["items"] == [] and refused["next_cursor"] is None
        view = notify_views._measurement_list(ledger.db, 1, {"project_id": 1, "items": [
            {"project_id": 1, "name": "合成検査", "value": "秘密にする合成値"}]})
        assert "秘密にする合成値" not in json.dumps(view, ensure_ascii=False)
        assert view["title"] == "測定値の原文記録"
        ledger.db.rollback()


def test_single_source_preflight_counts_only_revisions_dependency_reader_materializes(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        ledger.save_messages([_message(mid=i, body="別の合成投稿") for i in range(2, 7)])
        monkeypatch.setattr(measurement_reader, "_MAX_ROWS", 4)
        result = measurement_reader.read_measurements(ledger.db, 1, cfg={}, message_id=1)
        assert result["coverage"]["reader_state"] == "bounded"
        assert [item["value"] for item in result["records"]] == ["1.0"]
        # Old revisions really are read by the dependency barrier and remain bounded.
        for seq in range(2, 7):
            ledger.db.execute("INSERT INTO message_revisions "
                              "(message_id,seq,content_hash,body_state,observed_at) VALUES (?,?,?,?,?)",
                              (2, seq, "a" * 64, "full", 1.0))
        limited = measurement_reader.read_measurements(ledger.db, 1, cfg={}, message_id=1)
        assert limited["coverage"]["reader_state"] == "resource_limit"
        assert limited["records"] == []
        ledger.db.rollback()


def test_bounded_read_preserves_observation_and_caller_rollback(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        ledger.db.execute("UPDATE patients SET fetch_reason='synthetic' WHERE project_id=1")
        before = ledger.db.total_changes
        result = measurement_reader.read_measurements(ledger.db, 1, cfg={})
        assert ledger.db.in_transaction and ledger.db.total_changes == before
        assert result["coverage"]["reader_state"] == "bounded"
        assert result["coverage"]["source_complete"] is False
        [item] = result["records"]
        assert item["state"] == "observed" and item["value"] == "1.0"
        assert "source_body" not in item and "evidence" not in item
        ledger.db.rollback()
        assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None


@pytest.mark.parametrize("field,value", [("body_html", "異なる合成原文"), ("body_text", "")])
def test_hash_html_text_mismatch_holds_entire_result(tmp_path, monkeypatch, field, value):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        ledger.db.execute(f"UPDATE messages SET {field}=? WHERE message_id=1", (value,))
        result = measurement_reader.read_measurements(ledger.db, 1, cfg={})
        assert result["records"] == [] and result["coverage"]["reader_state"] == "source_unverified"
        assert ledger.db.in_transaction
        ledger.db.rollback()


def test_more_than_200_messages_does_not_return_partial_candidates(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        ledger.save_messages([_message(mid=i, body="合成本文") for i in range(2, 202)])
        result = measurement_reader.read_measurements(ledger.db, 1, cfg={})
        assert result["records"] == [] and result["coverage"]["reader_state"] == "resource_limit"
        assert result["coverage"]["source_complete"] is False


def test_exactly_200_messages_are_bounded_but_unprocessed_sources_stay_unknown(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        ledger.save_messages([_message(mid=i, body="合成本文") for i in range(2, 201)])
        result = measurement_reader.read_measurements(ledger.db, 1, cfg={})
        assert result["coverage"]["reader_state"] == "bounded"
        assert result["coverage"]["source_complete"] is False
        assert result["coverage"]["unprocessed"] == 199
        assert len(result["records"]) == 1


@pytest.mark.parametrize("kind", ["canonical_projection", "semantic_facts_v4", "semantic_facts_v2",
                                  "extract_llm_chunk", "extract_feedback_v1"])
def test_unbounded_dependency_is_held_without_fallback(tmp_path, monkeypatch, kind):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        ledger.artifact_add(kind, json.dumps({}), project_id=1, message_id=1)
        result = measurement_reader.read_measurements(ledger.db, 1, cfg={})
        assert result["records"] == [] and result["coverage"]["reader_state"] == "unsupported_dependency"


@pytest.mark.parametrize("field,value", [("body_html", "x" * 80001), ("body_html", "x\0y"),
                                         ("body_text", "x" * 20001)])
def test_oversize_or_nul_is_held_before_source_materialization(tmp_path, monkeypatch, field, value):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        ledger.db.execute(f"UPDATE messages SET {field}=? WHERE message_id=1", (value,))
        result = measurement_reader.read_measurements(ledger.db, 1, cfg={})
        assert result["records"] == [] and result["coverage"]["reader_state"] == "resource_limit"
        ledger.db.rollback()


def test_other_patient_does_not_affect_target_and_absent_config_is_unknown(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        ledger.ensure_patient(2)
        ledger.save_messages([_message(mid=2, project_id=2, body="別患者の合成本文")])
        ledger.artifact_add("extract_llm_chunk", "{}", project_id=2, message_id=2)
        assert measurement_reader.read_measurements(ledger.db, 1, cfg={})["coverage"]["reader_state"] == "bounded"
        assert measurement_reader.read_measurements(ledger.db, 1)["coverage"]["reader_state"] == "configuration_unavailable"
        assert measurement_reader.read_measurements(ledger.db, 1, cfg={"semantic": {"fact_source": "canonical"}})[
            "coverage"]["reader_state"] == "canonical_unavailable"


@pytest.mark.parametrize("semantic", [None, "legacy", [], {"fact_source": "typo"},
                                      {"mode": "typo"}, {"unrecognized": True}])
def test_invalid_semantic_configuration_never_falls_back_to_legacy(tmp_path, monkeypatch, semantic):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        result = measurement_reader.read_measurements(ledger.db, 1, cfg={"semantic": semantic})
        assert result["records"] == []
        assert result["coverage"]["reader_state"] == "configuration_unavailable"


@pytest.mark.parametrize(("table", "field"), [("patients", "fetch_reason"), ("messages", "body_text")])
def test_invalid_utf8_is_unknown_and_does_not_commit(tmp_path, monkeypatch, table, field):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        ledger.db.execute(f"UPDATE {table} SET {field}=CAST(x'80' AS TEXT) WHERE project_id=1")
        result = measurement_reader.read_measurements(ledger.db, 1, cfg={})
        assert result["records"] == [] and result["coverage"]["reader_state"] == "source_unverified"
        assert ledger.db.in_transaction
        ledger.db.rollback()


def test_other_writer_cannot_mix_preflight_and_source_snapshot(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        other = sqlite3.connect(ledger.db.execute("PRAGMA database_list").fetchone()[2])
        original, changed = measurement_reader._preflight, []

        def mutate_after_patient(db, table, *args, **kwargs):
            result = original(db, table, *args, **kwargs)
            if table == "patients" and not changed:
                other.execute("UPDATE messages SET body_html='合成の訂正文' WHERE message_id=1")
                other.commit()
                changed.append(True)
            return result

        monkeypatch.setattr(measurement_reader, "_preflight", mutate_after_patient)
        try:
            first = measurement_reader.read_measurements(ledger.db, 1, cfg={})
            assert changed and first["coverage"]["reader_state"] == "bounded"
            assert first["records"] and not ledger.db.in_transaction
            second = measurement_reader.read_measurements(ledger.db, 1, cfg={})
            assert second["records"] == [] and second["coverage"]["reader_state"] == "source_unverified"
        finally:
            other.close()


def test_oversized_artifact_and_byte_budget_hold_all_candidates(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        ledger.artifact_add("synthetic_history", "x" * 2000, project_id=1, message_id=1)
        monkeypatch.setattr(measurement_reader, "_MAX_BYTES", 1000)
        result = measurement_reader.read_measurements(ledger.db, 1, cfg={})
        assert result["records"] == [] and result["coverage"]["reader_state"] == "resource_limit"


def test_page_preflight_bounds_dependency_proofs_from_other_messages(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        ledger.save_messages([_message(mid=2, body="別の合成投稿")])
        ledger.artifact_add("inference_dependency_loss", "x" * 1000001, project_id=1, message_id=2)
        monkeypatch.setattr(measurement_reader.measurement_sources, "collect_measurements",
                            lambda *a, **k: pytest.fail("unbounded dependency materialized"))
        result = measurement_reader.read_measurements(ledger.db, 1, cfg={}, message_id=1)
        assert result["records"] == [] and result["coverage"]["reader_state"] == "resource_limit"


def test_page_preflight_bounds_thread_context_before_materialization(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        ledger.save_messages([_message(mid=1, body="x" * 20001),
                              _message(mid=2, body="合成の返信", parent_id=1)])
        monkeypatch.setattr(measurement_reader.measurement_sources, "collect_measurements",
                            lambda *a, **k: pytest.fail("oversize context materialized"))
        result = measurement_reader.read_measurements(ledger.db, 1, cfg={}, message_id=2)
        assert result["records"] == [] and result["coverage"]["reader_state"] == "resource_limit"


def test_related_artifact_count_is_bounded_before_progress(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        for _ in range(3):
            ledger.artifact_add("synthetic_history", "{}", project_id=1, message_id=1)
        monkeypatch.setattr(measurement_reader, "_MAX_ROWS", 2)
        result = measurement_reader.read_measurements(ledger.db, 1, cfg={})
        assert result["records"] == [] and result["coverage"]["reader_state"] == "resource_limit"


def test_interrupted_preflight_propagates_without_committing_caller_transaction(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        _seed(ledger)
        ledger.db.execute("UPDATE patients SET fetch_reason='synthetic' WHERE project_id=1")
        error = sqlite3.OperationalError("synthetic interrupt")
        error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
        monkeypatch.setattr(measurement_reader, "_preflight", lambda *a, **k: (_ for _ in ()).throw(error))
        with pytest.raises(sqlite3.OperationalError) as caught:
            measurement_reader.read_measurements(ledger.db, 1, cfg={})
        assert caught.value is error and ledger.db.in_transaction
        ledger.db.rollback()
