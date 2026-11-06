"""Fully synthetic local-operator measurement snapshot reads."""
from contextlib import closing
import base64
import json
import sqlite3
import time

import pytest

import measurement_local_view
import measurement_reader
import mcs_view
import snapshots
from extract_testkit import _message
from test_measurement_sources import _save, _setup


def _publish(tmp_path, monkeypatch, *, attachment=False, correction=False):
    ledger = _setup(tmp_path, monkeypatch)
    body = "本人は2020-10-01の空腹時採血 Cr 1.0 mg/dL。"
    _save(ledger, 1, body, {"labs": [{"name": "Cr", "value": "1.0", "unit": "mg/dL",
          "condition": "空腹時", "evidence": body, "subject": "patient", "status": "current"}]},
          "2020-10-01T09:00:00+09:00")
    if attachment or correction:
        body2 = "先ほどの数値は正しくありません。" if correction else "合成の添付です。"
        _save(ledger, 2, body2, {}, "2020-10-02T09:00:00+09:00")
    if attachment:
        ledger.db.execute("INSERT INTO attachments(message_id,file_id,state) VALUES(2,'synthetic','pending')")
        ledger.db.commit()
    source = ledger.db.execute("PRAGMA database_list").fetchone()[2]
    snapshot = snapshots.publish_snapshot(source, str(tmp_path / "snapshots"))
    assert snapshot is not None
    return ledger, snapshot


def test_local_snapshot_reports_only_source_bound_observation_with_unknown_latest(tmp_path, monkeypatch):
    ledger, snapshot = _publish(tmp_path, monkeypatch, attachment=True)
    with closing(ledger):
        page = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
        assert page["outcome"] == "unknown" and page["next_cursor"]
        result = measurement_local_view.read_snapshot(snapshot, 1, cfg={}, cursor=page["next_cursor"])
        assert result["outcome"] == "observed" and result["source_complete"] is False
        assert result["relations"] == "not_assessed" and result["attachment_count"] == 1
        [item] = result["items"]
        assert item["name"] == "Cr" and item["value"] == "1.0" and item["unit"] == "mg/dL"
        assert item["condition"] == "空腹時" and item["measured_on"] == "2020-10-01"
        assert item["posted_at"] == "2020-10-01T09:00:00+09:00"
        assert "source_body" not in str(result) and "evidence" not in str(result)
        assert result["snapshot"]["generation_id"]


def test_known_correction_holds_prior_observation(tmp_path, monkeypatch):
    ledger, snapshot = _publish(tmp_path, monkeypatch, correction=True)
    with closing(ledger):
        result = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
        assert result["outcome"] == "unknown" and result["items"] == []
        assert result["reason"] == "source_relation_unconfirmed"
        older = measurement_local_view.read_snapshot(snapshot, 1, cfg={}, cursor=result["next_cursor"])
        assert older["outcome"] == "observed"
        assert older["relations"] == "not_assessed" and older["source_complete"] is False
        assert "他の投稿の訂正・最新性は未確認" in older["label"]


def test_explicit_past_measurement_remains_a_written_observation(tmp_path, monkeypatch):
    ledger = _setup(tmp_path, monkeypatch)
    body = "本人の過去の2020-10-01空腹時採血 Cr 1.0 mg/dL。"
    with closing(ledger):
        _save(ledger, 1, body, {"labs": [{"name": "Cr", "value": "1.0", "unit": "mg/dL",
              "condition": "空腹時", "evidence": body, "subject": "patient", "status": "past"}]},
              "2020-10-02T09:00:00+09:00")
        source = ledger.db.execute("PRAGMA database_list").fetchone()[2]
        snapshot = snapshots.publish_snapshot(source, str(tmp_path / "past-snapshots"))
        result = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
        assert result["outcome"] == "observed" and result["source_complete"] is False
        assert result["relations"] == "not_assessed"
        assert result["items"][0]["state"] == "history"
        assert [(item["name"], item["value"], item["measured_on"]) for item in result["items"]] == [
            ("Cr", "1.0", "2020-10-01")]


def test_no_grounded_observation_is_unknown_instead_of_an_observed_empty_result(tmp_path, monkeypatch):
    ledger = _setup(tmp_path, monkeypatch)
    with closing(ledger):
        _save(ledger, 1, "合成の連絡です。", {}, "2020-10-02T09:00:00+09:00")
        source = ledger.db.execute("PRAGMA database_list").fetchone()[2]
        snapshot = snapshots.publish_snapshot(source, str(tmp_path / "empty-snapshots"))
        result = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
        assert result["outcome"] == "unknown" and result["items"] == []
        assert result["reason"] == "observations_unavailable" and result["source_complete"] is False


def test_paging_does_not_exclude_patient_with_more_than_200_messages(tmp_path, monkeypatch):
    ledger = _setup(tmp_path, monkeypatch)
    with closing(ledger):
        ledger.save_messages([_message(mid=i, body="合成の連絡です。",
                              posted_at="2020-10-01T09:00:00+09:00") for i in range(1, 200)])
        for mid, value in ((200, "1.0"), (201, "1.5")):
            body = f"本人は2020-10-01の空腹時採血 Cr {value} mg/dL。"
            _save(ledger, mid, body, {"labs": [{"name": "Cr", "value": value, "unit": "mg/dL",
                  "condition": "空腹時", "evidence": body, "subject": "patient", "status": "current"}]},
                  "2020-10-02T09:00:00+09:00")
        source = ledger.db.execute("PRAGMA database_list").fetchone()[2]
        snapshot = snapshots.publish_snapshot(source, str(tmp_path / "many-snapshots"))
        newest = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
        assert newest["outcome"] == "observed" and newest["items"][0]["message_id"] == 201
        older = measurement_local_view.read_snapshot(snapshot, 1, cfg={}, cursor=newest["next_cursor"])
        assert older["outcome"] == "observed" and older["items"][0]["message_id"] == 200
        assert older["next_cursor"] and older["source_complete"] is False


def test_cursor_stays_on_one_snapshot_patient_and_source_occurrence(tmp_path, monkeypatch):
    ledger, snapshot = _publish(tmp_path, monkeypatch, attachment=True)
    with closing(ledger):
        first = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
        cursor = first["next_cursor"]
        assert cursor and first["outcome"] == "unknown"
        token = base64.urlsafe_b64decode(cursor).decode()
        assert "project_id" not in token and "message_id" not in token
        assert "合成の添付" not in token and "mg/dL" not in token
        assert measurement_local_view.read_snapshot(snapshot, 2, cfg={}, cursor=cursor)["outcome"] == "unknown"
        assert measurement_local_view.read_snapshot(snapshot, 1, cfg={}, cursor="bad token")["outcome"] == "unknown"
        with sqlite3.connect(snapshot) as db:
            db.execute("UPDATE messages SET body_html='合成の変更' WHERE message_id=2")
        assert measurement_local_view.read_snapshot(snapshot, 1, cfg={}, cursor=cursor)["outcome"] == "unknown"


def test_cursor_offset_cannot_be_changed_to_identical_other_message(tmp_path, monkeypatch):
    ledger = _setup(tmp_path, monkeypatch)
    with closing(ledger):
        ledger.save_messages([_message(mid=i, body="同一の合成本文。",
                              posted_at="2020-10-01T09:00:00+09:00") for i in range(1, 4)])
        source = ledger.db.execute("PRAGMA database_list").fetchone()[2]
        snapshot = snapshots.publish_snapshot(source, str(tmp_path / "identical-snapshots"))
        page = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
        payload = json.loads(base64.urlsafe_b64decode(page["next_cursor"]))
        payload["offset"] = 2
        altered = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        rejected = measurement_local_view.read_snapshot(snapshot, 1, cfg={}, cursor=altered)
        assert rejected["outcome"] == "unknown" and rejected["reason"] == "cursor_changed"
        assert not rejected.get("next_cursor")


@pytest.mark.parametrize("damage", ["invalid_hash_utf8", "zero_revision", "real_revision", "invalid_revision_utf8",
                                   "blob_revision"])
def test_invalid_source_bytes_or_revision_leave_an_unknown_page_with_continuation(tmp_path, monkeypatch, damage):
    ledger, snapshot = _publish(tmp_path, monkeypatch, attachment=True)
    with closing(ledger):
        with sqlite3.connect(snapshot) as db:
            if damage == "invalid_hash_utf8":
                db.execute("UPDATE messages SET content_hash=CAST(x'80' AS TEXT) WHERE message_id=2")
            elif damage == "invalid_revision_utf8":
                db.execute("INSERT INTO message_revisions(message_id,seq,content_hash,body_state,observed_at) "
                           "VALUES(2,CAST(x'80' AS TEXT),'synthetic','full',1)")
            else:
                seq = {"zero_revision": 0, "real_revision": 1.5,
                       "blob_revision": sqlite3.Binary(b"\x80")}[damage]
                db.execute("INSERT INTO message_revisions(message_id,seq,content_hash,body_state,observed_at) "
                           "VALUES(2,?,'synthetic','full',1)", (seq,))
        page = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
        assert page["outcome"] == "unknown" and page["reason"] == "source_unverified"
        assert page["items"] == [] and page["next_cursor"]
        older = measurement_local_view.read_snapshot(snapshot, 1, cfg={}, cursor=page["next_cursor"])
        assert older["outcome"] in ("unknown", "observed") and not older.get("next_cursor")


@pytest.mark.parametrize("damage", ["revision_schema", "revision_oversize"])
def test_unverifiable_revision_boundary_cannot_be_skipped(tmp_path, monkeypatch, damage):
    ledger, snapshot = _publish(tmp_path, monkeypatch, attachment=True)
    with closing(ledger):
        with sqlite3.connect(snapshot) as db:
            if damage == "revision_schema":
                db.execute("DROP TABLE message_revisions")
            else:
                db.execute("INSERT INTO message_revisions(message_id,seq,content_hash,body_state,observed_at) "
                           "VALUES(2,?,'synthetic','full',1)", ("x" * 65,))
        page = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
        assert page["outcome"] == "unknown" and page["items"] == []
        assert not page.get("next_cursor")


@pytest.mark.parametrize("problem", ["snippet", "deleted", "oversize", "unsupported"])
def test_unknown_post_gives_explicit_reason_and_safe_continuation(tmp_path, monkeypatch, problem):
    ledger = _setup(tmp_path, monkeypatch)
    with closing(ledger):
        ledger.save_messages([_message(mid=1, body="別の合成投稿"), _message(mid=2, body="合成の投稿")])
        if problem in ("snippet", "deleted"):
            ledger.db.execute("UPDATE messages SET body_state=? WHERE message_id=2", (problem,))
        elif problem == "oversize":
            ledger.db.execute("UPDATE messages SET body_text=? WHERE message_id=2", ("x" * 20001,))
        else:
            ledger.artifact_add("extract_llm_chunk", "{}", project_id=1, message_id=2)
        ledger.db.commit()
        source = ledger.db.execute("PRAGMA database_list").fetchone()[2]
        snapshot = snapshots.publish_snapshot(source, str(tmp_path / "unknown-snapshots"))
        first = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
        assert first["outcome"] == "unknown" and first["items"] == [] and first["reason"]
        assert first["next_cursor"]
        second = measurement_local_view.read_snapshot(snapshot, 1, cfg={}, cursor=first["next_cursor"])
        assert second["outcome"] == "unknown" and second["items"] == []
        assert second["reason"] == "observations_unavailable" and second["next_cursor"] is None


def test_archived_or_forbidden_patient_cannot_get_a_cursor(tmp_path, monkeypatch):
    ledger, snapshot = _publish(tmp_path, monkeypatch, attachment=True)
    with closing(ledger):
        for sql in ("UPDATE patients SET is_archived=1 WHERE project_id=1",
                    "UPDATE patients SET is_archived=0,fetch_state='forbidden' WHERE project_id=1"):
            with sqlite3.connect(snapshot) as db:
                db.execute(sql)
            result = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
            assert result["outcome"] == "unknown" and result["items"] == []
            assert not result.get("next_cursor")


def test_empty_pages_offer_continuation_until_the_tail(tmp_path, monkeypatch):
    ledger = _setup(tmp_path, monkeypatch)
    with closing(ledger):
        ledger.save_messages([_message(mid=i, body="合成の連絡です。",
                              posted_at="2020-10-01T09:00:00+09:00") for i in range(1, 4)])
        source = ledger.db.execute("PRAGMA database_list").fetchone()[2]
        snapshot = snapshots.publish_snapshot(source, str(tmp_path / "tail-snapshots"))
        first = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
        second = measurement_local_view.read_snapshot(snapshot, 1, cfg={}, cursor=first["next_cursor"])
        last = measurement_local_view.read_snapshot(snapshot, 1, cfg={}, cursor=second["next_cursor"])
        assert all(page["outcome"] == "unknown" and page["items"] == [] for page in (first, second, last))
        assert first["next_cursor"] and second["next_cursor"] and last["next_cursor"] is None


def test_missing_patient_and_config_return_unknown(tmp_path, monkeypatch):
    ledger, snapshot = _publish(tmp_path, monkeypatch)
    with closing(ledger):
        assert measurement_local_view.read_snapshot(snapshot, 2, cfg={})["outcome"] == "unknown"
        result = measurement_local_view.read_snapshot(snapshot, 1)
        assert result["outcome"] == "unknown" and result["items"] == []


def test_deadline_and_sqlite_busy_return_unknown_without_source_text(tmp_path, monkeypatch):
    ledger, snapshot = _publish(tmp_path, monkeypatch)
    with closing(ledger):
        assert measurement_local_view.read_snapshot(snapshot, 1, cfg={}, timeout_s=1e-12)["outcome"] == "unknown"

        def busy(*_args, **_kwargs):
            raise sqlite3.OperationalError("synthetic-private-canary")

        monkeypatch.setattr(measurement_local_view.sqlite3, "connect", busy)
        result = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
        assert result["outcome"] == "unknown" and result["items"] == []
        assert "synthetic-private-canary" not in str(result)


def test_future_snapshot_is_not_used_as_a_time_proof(tmp_path, monkeypatch):
    ledger, snapshot = _publish(tmp_path, monkeypatch)
    with closing(ledger):
        with sqlite3.connect(snapshot) as db:
            db.execute("UPDATE snapshot_meta SET generated_at=? WHERE singleton=1", (time.time() + 86400,))
        result = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
        assert result["outcome"] == "unknown" and result["items"] == []
        assert result["reason"] == "snapshot_unavailable"


def test_sql_progress_interrupt_returns_unknown(tmp_path, monkeypatch):
    ledger, snapshot = _publish(tmp_path, monkeypatch)
    with closing(ledger):
        original = measurement_reader._preflight

        def costly(db, *args, **kwargs):
            db.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) "
                       "SELECT SUM(x) FROM n").fetchone()
            return original(db, *args, **kwargs)

        clock = iter(i * 0.001 for i in range(100000))
        monkeypatch.setattr(measurement_local_view.time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(measurement_reader, "_preflight", costly)
        result = measurement_local_view.read_snapshot(snapshot, 1, cfg={}, timeout_s=0.05)
        assert result["outcome"] == "unknown" and result["items"] == []


def test_cli_uses_local_entry_without_altering_existing_view_handler(tmp_path, monkeypatch, capsys):
    ledger, snapshot = _publish(tmp_path, monkeypatch)
    with closing(ledger):
        called = []
        ledger.db.set_progress_handler(lambda: called.append(True) or 0, 1)
        monkeypatch.setattr("mcs_util.load_config_checked", lambda: {})
        try:
            assert mcs_view.main(["--snapshot", snapshot, "local_measurements", "--project", "1"]) == 0
            output = capsys.readouterr().out
            assert "原文に記載された測定値" in output
            ledger.db.execute("SELECT 1").fetchone()
            assert called
        finally:
            ledger.db.set_progress_handler(None, 0)


@pytest.mark.parametrize('version', [12, 13])
def test_published_schema12_is_readable_but_future_schema_stays_closed(tmp_path, monkeypatch, version):
    ledger, snapshot = _publish(tmp_path, monkeypatch)
    with closing(ledger):
        with closing(sqlite3.connect(snapshot)) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 12
            db.execute(f'PRAGMA user_version={version}')
            db.commit()
        if version == 13:
            monkeypatch.setattr(measurement_reader, 'patient_scope_valid',
                lambda *_args: pytest.fail('unknown schema reached clinical reader'))
            assert measurement_local_view.read_snapshot(snapshot, 1, cfg={}) == {
                'outcome': 'unknown', 'reason': 'snapshot_unavailable',
                'items': [], 'source_complete': False}
            with pytest.raises(ValueError, match='snapshot_upgrade_required'):
                mcs_view.View(snapshot)
        else:
            result = measurement_local_view.read_snapshot(snapshot, 1, cfg={})
            assert result['outcome'] == 'observed'
            assert result['items'][0]['name'] == 'Cr'
            with closing(mcs_view.View(snapshot)) as view:
                assert view.meta['generation_id'] == result['snapshot']['generation_id']
                with pytest.raises(sqlite3.OperationalError, match='readonly'):
                    view.db.execute('DELETE FROM snapshot_meta')
