"""Fully synthetic intervention assertions and source-boundary checks."""
from datetime import datetime
import hashlib
import sqlite3

import pytest

import intervention_schedule as schedule
from mcs_util import html_to_text


POSTED = datetime.fromisoformat("2026-10-01T09:00:00+09:00").timestamp()


def item(body, **changes):
    row = {"project_id": 1, "message_id": 7, "content_hash": "a" * 64,
           "category": "followup", "subject": "patient", "source": "chat",
           "body_state": "full", "posted_at_ts": POSTED, "source_body": body,
           "quote": body, "unverified": False}
    return {**row, **changes}


def test_named_actor_and_patient_with_explicit_future_date_are_source_bound():
    body = "看護師が2026年10月3日に本人を訪問する予定です。"
    result = schedule.project(1, [item(body)], coverage={"source_complete": False})
    record, = result["records"]
    assert record == {"project_id": 1, "message_id": 7, "content_hash": "a" * 64,
                      "span": [0, len(body)], "quote": body, "role": "nurse",
                      "role_text": "看護師", "action": "訪問", "date_text": "2026年10月3日",
                      "event_day": "2026-10-03", "state": "planned"}
    assert result["coverage"]["upstream_source_complete"] is False
    assert result["coverage"]["source_validation"] == "caller_required"
    assert "source_body" not in str(result)


@pytest.mark.parametrize("body", [
    "2026年10月3日に本人を訪問する予定です。",  # sender is not actor
    "本人は2026年10月3日に看護師に訪問を依頼する予定です。",
    "看護師と薬剤師が2026年10月3日に本人を訪問する予定です。",
    "看護師が来週本人を訪問する予定です。",
    "看護師が2026年10月3日に家族を訪問する予定です。",
    "看護師が2026年10月3日に本人を訪問する予定ではありません。",
    "看護師が2026年10月3日に本人を訪問するかもしれません。",
    "看護師が2026年10月3日に本人を訪問する予定です。薬剤師が確認します。",
    "看護師が2026年10月3日に本人を訪問する予定を中止する予定です。",
    "看護師が2026年10月3日に本人を訪問しました。",  # future event is not completed
    "看護師は2026年10月3日に本人に連絡し、施設担当者が訪問する予定です。",
    "看護師が2026年10月3日に本人に連絡し、訪問予定です。",
    "看護師が2026年10月3日に本人への訪問を中止しました。",  # future cancellation
    "看護師が2026年10月3日（昨日）に本人を訪問する予定です。",
    "看護師が2026年10月3日に本人を訪問する予定ですか。",
])
def test_ambiguous_scope_or_time_never_becomes_a_verified_intervention(body):
    row, = schedule.project(1, [item(body, profession="看護師")])["records"]
    assert row["state"] == "unknown"
    assert row["role"] is None and row["event_day"] is None


@pytest.mark.parametrize("body, expected", [
    ("看護師が2026年9月30日に本人を訪問しました。", "completed"),
    ("薬剤師が2026年9月30日に本人への服薬指導を中止しました。", "cancelled"),
    ("訪問看護師が2026年10月3日に本人を訪問する予定です。", "planned"),
])
def test_planned_completed_and_cancelled_records_are_distinct(body, expected):
    row, = schedule.project(1, [item(body)])["records"]
    assert row["state"] == expected
    assert row["span"] == [0, len(body)]


def test_foreign_deleted_and_unbound_sources_never_expose_original_text():
    body = "看護師が2026年10月3日に本人を訪問する予定です。"
    result = schedule.project(1, [item(body, project_id=2), item(body, body_state="deleted"),
                                  item(body, source_body="訂正後の別の合成本文"),
                                  item(body, evidence_span=[1, len(body)]),
                                  item(body, content_hash="bad"), item(body, source_body=body + body)])
    assert result["coverage"]["foreign"] == 1
    assert result["coverage"]["deleted"] == 1
    assert result["coverage"]["unbound"] == 4
    assert all(row["quote"] is None and row["state"] == "unknown" for row in result["records"])
    assert body not in str(result)


def test_multiple_candidates_are_not_linked_or_falsely_completed():
    plan = "看護師が2026年10月3日に本人を訪問する予定です。"
    cancel = "看護師が2026年9月30日に本人への訪問を中止しました。"
    rows = schedule.project(1, [item(plan), item(cancel, message_id=8, content_hash="b" * 64)])["records"]
    assert [row["state"] for row in rows] == ["planned", "cancelled"]
    assert [row["message_id"] for row in rows] == [7, 8]
    assert all("relation" not in row for row in rows)


def test_project_rejects_invalid_patient_scope():
    with pytest.raises(ValueError, match="invalid_intervention_input"):
        schedule.project(True, [])


def test_missing_source_scope_is_not_assumed_and_arbitrary_coverage_is_not_returned():
    body = "看護師が2026年10月3日に本人を訪問する予定です。"
    source = item(body)
    del source["project_id"]
    marker = "secret-synthetic-coverage-marker"
    result = schedule.project(1, [source], coverage={"source_complete": True, "untrusted": marker})
    assert result["records"] == [] and result["coverage"]["foreign"] == 1
    assert result["coverage"]["upstream_source_complete"] is True
    assert marker not in str(result)


def test_missing_unverified_flag_never_becomes_a_verified_intervention():
    body = "看護師が2026年10月3日に本人を訪問する予定です。"
    source = item(body)
    del source["unverified"]
    assert schedule.project(1, [source])["records"][0]["state"] == "unknown"


@pytest.fixture
def source_db():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE patients(project_id INTEGER PRIMARY KEY, fetch_state TEXT,
                              fetch_reason TEXT, is_archived INTEGER);
        CREATE TABLE messages(message_id INTEGER PRIMARY KEY, project_id INTEGER,
                              body_html TEXT, body_text TEXT, body_state TEXT,
                              content_hash TEXT, posted_at_ts REAL);
        INSERT INTO patients VALUES(1, 'complete', NULL, 0);
        INSERT INTO patients VALUES(2, 'complete', NULL, 0);
    """)
    yield db
    db.close()


def put_source(db, mid, project_id, html, *, state="full", digest=None):
    digest = digest if digest is not None else hashlib.sha256(html.encode()).hexdigest()
    db.execute("INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?)",
               (mid, project_id, html, html_to_text(html), state, digest, POSTED))
    db.commit()


def test_source_reader_uses_patient_scoped_verified_single_sentences(source_db):
    db = source_db
    planned = "看護師が2026年10月3日に本人を訪問する予定です。"
    unknown = "家族によると看護師が2026年10月3日に本人を訪問する予定です。"
    put_source(db, 1, 1, planned + unknown)
    put_source(db, 2, 2, planned)
    put_source(db, 3, 1, planned)
    result = schedule.read_source_schedule(db, 1)
    assert [r["state"] for r in result["records"]] == ["planned", "unknown", "unknown"]
    assert all(r["project_id"] == 1 and r["message_id"] in (1, 3) for r in result["records"])
    assert result["records"][0]["span"] == [0, len(planned)]
    assert result["coverage"]["source_complete"] is None
    assert result["coverage"]["extraction"] == "not_assessed"
    assert result["coverage"]["fetch_state"] == "complete"


@pytest.mark.parametrize("body", [
    "次の記載は誤りです。看護師が2026年10月3日に本人を訪問する予定です。",
    "看護師が2026年10月3日に本人を訪問する予定です。という記載は取り消されました。",
    "家族の話です。看護師が2026年10月3日に本人を訪問する予定です。",
    "看護師が2026年10月3日に本人を訪問する予定です。本人以外への予定でした。",
    "予定が確定した場合。看護師が2026年10月3日に本人を訪問する予定です。",
    "「看護師が2026年10月3日に本人を訪問する予定です。」と聞きました。",
    "前回の記載を訂正します。看護師が2026年10月3日に本人を訪問する予定です。",
])
def test_neighbouring_or_quoted_context_cannot_confirm_an_intervention(source_db, body):
    put_source(source_db, 1, 1, body)
    rows = schedule.read_source_schedule(source_db, 1)["records"]
    assert len(rows) == 1 and rows[0]["state"] == "unknown"
    assert rows[0]["quote"] is None
    assert rows[0]["role"] is None and rows[0]["event_day"] is None


def test_source_reader_distinguishes_missing_and_changed_sources(source_db):
    db = source_db
    planned = "看護師が2026年10月3日に本人を訪問する予定です。"
    put_source(db, 1, 1, planned, state="deleted")
    put_source(db, 2, 1, planned, state="snippet")
    put_source(db, 3, 1, planned, digest="0" * 64)
    put_source(db, 4, 1, planned)
    db.execute("UPDATE messages SET body_text=? WHERE message_id=4", (planned + "改変",))
    db.execute("UPDATE patients SET fetch_state='incomplete' WHERE project_id=1")
    result = schedule.read_source_schedule(db, 1)
    assert not result["records"]
    assert result["coverage"]["deleted"] == 1
    assert result["coverage"]["incomplete"] == 1
    assert result["coverage"]["invalid_source"] == 2
    assert result["coverage"]["fetch_state"] == "incomplete"
    put_source(db, 4, 1, "看護師が2026年9月30日に本人を訪問しました。")
    updated = schedule.read_source_schedule(db, 1)
    assert [r["state"] for r in updated["records"]] == ["completed"]
    assert all(r["message_id"] != 1 for r in updated["records"])


def test_source_reader_reports_both_scan_and_candidate_bounds(source_db):
    db = source_db
    planned = "看護師が2026年10月3日に本人を訪問する予定です。"
    put_source(db, 1, 1, planned)
    put_source(db, 2, 1, planned + planned)
    result = schedule.read_source_schedule(db, 1, limit=1, max_records=1)
    assert result["coverage"]["truncated"] is True
    assert result["coverage"]["records_truncated"] is True
    assert len(result["records"]) == 1
    put_source(db, 3, 1, "A" * (schedule._MAX_BODY_CHARS + 1))
    assert schedule.read_source_schedule(db, 1)["coverage"]["oversize"] == 1


def test_source_reader_does_not_link_cancellation_to_separate_plan(source_db):
    db = source_db
    put_source(db, 1, 1, "看護師が2026年10月3日に本人を訪問する予定です。")
    put_source(db, 2, 1, "看護師が2026年9月30日に本人への訪問を中止しました。")
    rows = schedule.read_source_schedule(db, 1)["records"]
    assert [row["state"] for row in rows] == ["cancelled", "planned"]
    assert all("relation" not in row for row in rows)


def test_source_reader_does_not_claim_empty_or_inaccessible_scope_complete(source_db):
    db = source_db
    db.execute("UPDATE patients SET fetch_state='pending' WHERE project_id=1")
    result = schedule.read_source_schedule(db, 1)
    assert not result["records"] and result["coverage"]["fetch_state"] == "pending"
    assert result["coverage"]["source_complete"] is None
    db.execute("UPDATE patients SET fetch_reason='access_revoked' WHERE project_id=1")
    assert schedule.read_source_schedule(db, 1)["coverage"]["state"] == "scope_unavailable"
    assert schedule.read_source_schedule(db, 999)["coverage"]["state"] == "scope_unknown"


def test_source_reader_old_or_missing_schema_stays_unknown():
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE patients(project_id INTEGER PRIMARY KEY)")
        result = schedule.read_source_schedule(db, 1)
        assert result["coverage"]["state"] == "schema_unknown" and not result["records"]
    finally:
        db.close()


def test_source_reader_uses_caller_snapshot_during_concurrent_revision(tmp_path):
    path = tmp_path / "synthetic-snapshot.db"
    writer = sqlite3.connect(path)
    reader = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.executescript("""
            CREATE TABLE patients(project_id INTEGER PRIMARY KEY, fetch_state TEXT,
                                  fetch_reason TEXT, is_archived INTEGER);
            CREATE TABLE messages(message_id INTEGER PRIMARY KEY, project_id INTEGER,
                                  body_html TEXT, body_text TEXT, body_state TEXT,
                                  content_hash TEXT, posted_at_ts REAL);
            INSERT INTO patients VALUES(1, 'complete', NULL, 0);
        """)
        planned = "看護師が2026年10月3日に本人を訪問する予定です。"
        put_source(writer, 1, 1, planned)
        reader.execute("BEGIN")
        before = schedule.read_source_schedule(reader, 1)
        put_source(writer, 1, 1, "看護師が2026年9月30日に本人を訪問しました。")
        held = schedule.read_source_schedule(reader, 1)
        assert held == before and held["records"][0]["state"] == "planned"
        reader.execute("COMMIT")
        after = schedule.read_source_schedule(reader, 1)
        assert after["records"][0]["state"] == "completed"
        assert after["records"][0]["content_hash"] != before["records"][0]["content_hash"]
    finally:
        reader.close()
        writer.close()
