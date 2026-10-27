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


@pytest.mark.parametrize("body", [
    "看護師が2026年10月3日の本人への訪問を中止しました。",
    "看護師が2026年10月3日に本人を訪問する予定を中止しました。",
    "看護師が2026年10月3日の本人への訪問を取り消しました。",
    "看護師が2026年10月3日の本人への訪問を取消しました。",
])
def test_explicit_future_target_cancellation_does_not_invent_cancellation_day(body):
    row, = schedule.project(1, [item(body)])["records"]
    assert row["state"] == "cancelled"
    assert row["role"] == "nurse" and row["action"] == "訪問"
    assert row["target_day"] == "2026-10-03" and row["event_day"] is None
    assert row["date_basis"] == "cancelled_intervention" and row["quote"] == body
    assert "relation" not in row


def test_ambiguous_past_cancellation_date_is_not_presented_as_an_event_date():
    body = "薬剤師が2026年9月30日に本人への服薬指導を中止しました。"
    row, = schedule.project(1, [item(body)])["records"]
    assert row["state"] == "cancelled" and row["event_day"] is None
    assert row["date_text"] == "2026年9月30日"
    assert row["date_basis"] == "cancellation_or_intervention_unclear"


@pytest.mark.parametrize("body", [
    "看護師が2026年10月3日の家族への訪問を中止しました。",
    "看護師が2026年10月3日の本人への訪問を中止していません。",
    "看護師が2026年10月3日の本人への訪問を中止ました。",
    "看護師が2026年10月3日の本人への訪問を中止する予定です。",
    "看護師が2026年10月3日（昨日）の本人への訪問を中止しました。",
])
def test_unbound_or_conditional_future_cancellation_is_not_confirmed(body):
    row, = schedule.project(1, [item(body)])["records"]
    assert row["state"] == "unknown" and row["event_day"] is None
    assert "target_day" not in row and row["quote"] is None


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


@pytest.mark.parametrize("written", ["明日", "今日", "本日", "1月1日", "12/31", "2月29日"])
@pytest.mark.parametrize("posted", [None, 0,
    datetime.fromisoformat("2026-12-31T14:59:59+00:00").timestamp(),
    datetime.fromisoformat("2026-12-31T15:00:00+00:00").timestamp()])
def test_unresolved_plan_preserves_written_date_without_deriving_event_day(written, posted):
    body = f"看護師が{written}に本人を訪問する予定です。"
    row, = schedule.project(1, [item(body, posted_at_ts=posted)])["records"]
    assert row["state"] == "planned_date_unknown"
    assert row["date_text"] == written and row["event_day"] is None
    assert row["role"] == "nurse" and row["role_text"] == "看護師" and row["action"] == "訪問"
    assert row["quote"] == body and row["span"] == [0, len(body)]
    assert "target_day" not in row and "relation" not in row


@pytest.mark.parametrize("body", [
    "看護師が明日本人を訪問する予定ではありません。",
    "看護師が明日本人を訪問するかもしれません。",
    "看護師が明日家族を訪問する予定です。",
    "看護師が翌日本人を訪問する予定です。",
    "看護師が来週本人を訪問する予定です。",
    "看護師が2月30日に本人を訪問する予定です。",
    "看護師が明日と明後日本人を訪問する予定です。",
    "看護師が明日本人を訪問する予定を中止しました。",
    "看護師が明日本人を訪問する予定ですか。",
    "「看護師が明日本人を訪問する予定です。」と聞きました。",
])
def test_unresolved_plan_does_not_confirm_ambiguous_or_other_subject(body):
    row, = schedule.project(1, [item(body)])["records"]
    assert row["state"] == "unknown" and row["event_day"] is None and row["quote"] is None


@pytest.mark.parametrize("prefix,suffix", [
    ("家族の話です。", ""), ("次の記載は誤りです。", ""),
    ("", "記載を取り消します。"), ("予定が確定した場合。", ""),
])
def test_unresolved_plan_never_promotes_a_fragment_of_multiple_sentences(prefix, suffix):
    quote = "看護師が明日本人を訪問する予定です。"
    body = prefix + quote + suffix
    row, = schedule.project(1, [item(body, quote=quote, evidence_span=[len(prefix), len(prefix) + len(quote)])])["records"]
    assert row["state"] == "unknown" and row["event_day"] is None and row["quote"] is None


@pytest.fixture
def source_db():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE patients(project_id INTEGER PRIMARY KEY, fetch_state TEXT,
                              fetch_reason TEXT, is_archived INTEGER);
        CREATE TABLE messages(message_id INTEGER PRIMARY KEY, project_id INTEGER,
                              body_html TEXT, body_text TEXT, body_state TEXT,
                              content_hash TEXT, posted_at_ts REAL);
        CREATE TABLE message_revisions(message_id INTEGER NOT NULL, seq INTEGER NOT NULL,
                                       PRIMARY KEY(message_id,seq));
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


def test_unresolved_plan_source_scope_and_revision_never_supply_a_calendar_day(source_db):
    original = "看護師が明日本人を訪問する予定です。"
    revised = "薬剤師が2月29日に本人を服薬指導する予定です。"
    put_source(source_db, 1, 1, original)
    put_source(source_db, 2, 2, original)
    before = schedule.read_source_schedule(source_db, 1)["records"]
    assert len(before) == 1 and before[0]["date_text"] == "明日" and before[0]["event_day"] is None
    put_source(source_db, 1, 1, revised)
    source_db.execute("UPDATE messages SET posted_at_ts=NULL WHERE message_id=1")
    after = schedule.read_source_schedule(source_db, 1)["records"]
    assert len(after) == 1 and after[0]["project_id"] == 1 and after[0]["quote"] == revised
    assert after[0]["state"] == "planned_date_unknown" and after[0]["date_text"] == "2月29日"
    assert after[0]["event_day"] is None and "target_day" not in after[0]
    assert after[0]["content_hash"] != before[0]["content_hash"]


@pytest.mark.parametrize("body", [
    "看護師です。", "訪問看護師から連絡がありました。", "介護職員からの連絡です。",
    "介護支援専門員です。", "相談員から連絡がありました。",
])
def test_profession_name_alone_is_not_an_intervention_candidate(source_db, body):
    put_source(source_db, 1, 1, body)
    result = schedule.read_source_schedule(source_db, 1)
    assert result["records"] == [] and result["coverage"]["candidates"] == 0


@pytest.mark.parametrize("role,action", [("看護師", "看護"), ("介護職員", "介護"),
                                         ("訪問看護師", "訪問")])
def test_action_outside_profession_name_remains_a_candidate(source_db, role, action):
    body = f"{role}が2026年10月3日に本人を{action}する予定です。"
    put_source(source_db, 1, 1, body)
    result = schedule.read_source_schedule(source_db, 1)
    assert result["coverage"]["candidates"] == 1
    assert result["records"][0]["state"] == "planned"
    assert result["records"][0]["action"] == action


@pytest.mark.parametrize("role,normalized", [("ケアマネ", "care_manager"), ("介護士", "care_worker"),
                                            ("ヘルパー", "care_worker")])
def test_existing_role_aliases_keep_written_role_and_source_for_explicit_visit(source_db, role, normalized):
    body = f"{role}が2026年10月3日に本人を訪問する予定です。"
    put_source(source_db, 1, 1, body)
    result = schedule.read_source_schedule(source_db, 1)
    [record] = result["records"]
    assert record["state"] == "planned" and record["role"] == normalized
    assert record["role_text"] == role and record["action"] == "訪問"
    assert record["event_day"] == "2026-10-03" and record["quote"] == body
    assert record["span"] == [0, len(body)] and record["project_id"] == 1


@pytest.mark.parametrize("role", ["ケアマネ", "介護士", "ヘルパー"])
@pytest.mark.parametrize("following", ["家族を訪問する予定です。", "本人を訪問する予定ですか。",
                                         "本人を訪問する予定ではありません。",
                                         "本人を訪問する予定を中止する予定です。"])
def test_role_alias_does_not_promote_foreign_or_uncertain_visit(role, following):
    body = f"{role}が2026年10月3日に{following}"
    [record] = schedule.project(1, [item(body)])["records"]
    assert record["state"] == "unknown" and record["role"] is None
    assert record["event_day"] is None and record["quote"] is None


@pytest.mark.parametrize("role,normalized", [("ケアマネ", "care_manager"), ("介護士", "care_worker"),
                                            ("ヘルパー", "care_worker")])
def test_role_alias_preserves_cancellation_and_unresolved_date(role, normalized):
    cancelled = f"{role}が2026年10月3日の本人への訪問を中止しました。"
    unresolved = f"{role}が明日本人を訪問する予定です。"
    cancel, plan = schedule.project(1, [item(cancelled), item(unresolved, message_id=8)])["records"]
    assert cancel["state"] == "cancelled" and cancel["role"] == normalized
    assert cancel["role_text"] == role and cancel["target_day"] == "2026-10-03"
    assert cancel["event_day"] is None and cancel["quote"] == cancelled
    assert plan["state"] == "planned_date_unknown" and plan["role"] == normalized
    assert plan["date_text"] == "明日" and plan["event_day"] is None
    assert plan["quote"] == unresolved and "relation" not in cancel and "relation" not in plan


@pytest.mark.parametrize("body", [
    "ケアマネです。", "介護士です。", "ヘルパーです。", "訪問介護士です。", "訪問ヘルパーです。",
])
def test_role_alias_alone_does_not_supply_an_action(source_db, body):
    put_source(source_db, 1, 1, body)
    result = schedule.read_source_schedule(source_db, 1)
    assert result["records"] == [] and result["coverage"]["candidates"] == 0


@pytest.mark.parametrize("role", ["介護士長", "ケアマネージャー", "訪問ヘルパー"])
def test_unapproved_or_compound_role_is_not_promoted_as_an_alias(role):
    body = f"{role}が2026年10月3日に本人を訪問する予定です。"
    [record] = schedule.project(1, [item(body)])["records"]
    assert record["state"] == "unknown" and record["role"] is None
    assert record["event_day"] is None and record["quote"] is None


def test_future_target_cancellation_and_plan_remain_separate_source_records(source_db):
    plan = "看護師が2026年10月3日に本人を訪問する予定です。"
    cancel = "看護師が2026年10月3日の本人への訪問を中止しました。"
    put_source(source_db, 1, 1, plan)
    put_source(source_db, 2, 1, cancel)
    records = schedule.read_source_schedule(source_db, 1)["records"]
    assert [row["state"] for row in records] == ["cancelled", "planned"]
    assert records[0]["target_day"] == "2026-10-03" and records[0]["event_day"] is None
    assert all("relation" not in row for row in records)


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


@pytest.mark.parametrize('nul_field', ['body', 'html', 'both'])
def test_embedded_nul_rejects_source_before_sql_returns_large_text(source_db, nul_field):
    db = source_db
    valid = '看護師が2026年10月3日に本人を訪問する予定です。'
    put_source(db, 1, 1, valid)
    put_source(db, 2, 2, valid)
    huge = 'x\0' + 'A' * 100_000
    put_source(db, 3, 1, huge if nul_field in ('html', 'both') else 'x')
    if nul_field == 'body':
        db.execute('UPDATE messages SET body_text=? WHERE message_id=3', (huge,))
    elif nul_field == 'html':
        db.execute("UPDATE messages SET body_text='x' WHERE message_id=3")
    seen = []

    def inspect(cursor, row):
        if cursor.description and any(column[0] == 'body_nul' for column in cursor.description):
            if row[3] == 3:
                seen.append((row[-2], row[-1]))
        return row

    db.row_factory = inspect
    try:
        result = schedule.read_source_schedule(db, 1)
    finally:
        db.row_factory = None
    assert seen == [(None, None)]
    assert result['coverage']['invalid_source'] == 1
    assert result['coverage']['messages_examined'] == 2
    assert [record['message_id'] for record in result['records']] == [1]
    assert result['records'][0]['state'] == 'planned'


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
            CREATE TABLE message_revisions(message_id INTEGER NOT NULL, seq INTEGER NOT NULL,
                                           PRIMARY KEY(message_id,seq));
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


@pytest.mark.parametrize("body", [
    "看護師が2026年10月3日に本人を訪問する予定です。",
    "看護師が2026年9月30日に本人を訪問しました。",
    "看護師が2026年10月3日の本人への訪問を中止しました。",
    "看護師が明日本人を訪問する予定です。",
    "看護師が2026年10月3日に家族を訪問する予定です。",
    "次の記載は誤りです。看護師が2026年10月3日に本人を訪問する予定です。",
    "看護師が2026年10月3日に本人を訪問する予定です。という記載を取り消しました。",
    "介護士です。",
])
def test_compact_source_uses_same_classification_and_spans_as_reader(source_db, body):
    put_source(source_db, 1, 1, body)
    digest = hashlib.sha256(body.encode()).hexdigest()
    compact = schedule.compact_source_record(1, 1, digest, body, POSTED)
    observed = schedule.read_source_schedule(source_db, 1)
    fields = ("span", "role", "action", "event_day", "target_day", "date_basis", "state")
    expected = [{key: row[key] for key in fields if key in row} for row in observed["records"]]
    assert compact["records"] == expected
    assert compact["coverage"]["candidates"] == observed["coverage"]["candidates"]
    assert compact["coverage"]["unknown"] == observed["coverage"]["unknown"]
    assert compact["coverage"]["source_complete"] is None
    assert compact["coverage"]["source_validation"] == "caller_required"
    assert body not in str(compact)
    assert all("quote" not in row and "role_text" not in row and "date_text" not in row
               for row in compact["records"])


def test_compact_source_marks_record_limit_and_does_not_drop_uncertainty(source_db):
    sentence = "看護師が2026年10月3日に本人を訪問する予定です。"
    body = sentence * 2
    put_source(source_db, 1, 1, body)
    result = schedule.compact_source_record(1, 1, hashlib.sha256(body.encode()).hexdigest(),
                                            body, POSTED, max_records=1)
    reader = schedule.read_source_schedule(source_db, 1, max_records=1)
    assert result["coverage"]["records_truncated"] is True
    assert result["coverage"]["unknown"] == 1
    assert result["records"][0]["state"] == "unknown"
    assert result["records"][0]["span"] == reader["records"][0]["span"]
    assert reader["coverage"]["records_truncated"] is True


@pytest.mark.parametrize("body,state,expected", [
    ("本文", "deleted", "deleted"), ("本文", "snippet", "incomplete"),
    (None, "full", "invalid_source"), ("x\0y", "full", "invalid_source"),
    ("x" * (schedule._MAX_BODY_CHARS + 1), "full", "oversize"),
])
def test_compact_source_reports_missing_or_rejected_input_without_content(body, state, expected):
    result = schedule.compact_source_record(1, 1, "a" * 64, body, POSTED, body_state=state)
    assert result["coverage"]["state"] == expected
    assert result["coverage"]["source_complete"] is None
    assert result["records"] == []


def test_profession_names_do_not_hide_an_independent_action():
    roles = "訪問看護師" * 1000
    assert not schedule._has_independent_action(roles)
    assert schedule._has_independent_action(roles + "が本人を訪問")
    assert schedule._has_independent_action("訪問後に" + roles)


def test_profession_action_scan_preserves_original_overlap_rules():
    import random

    randomizer = random.Random(4521)
    tokens = ["訪問", "訪問看護師", "看護師", "介護士", "介護", "医師", "本人", "予定", "。"]
    for _ in range(500):
        body = "".join(randomizer.choice(tokens) for _ in range(randomizer.randrange(1, 25)))
        intervals = [(m.start() - 2 if body[max(0, m.start() - 2):m.start()] == "訪問"
                      else m.start(), m.end()) for m in schedule._ROLE_MENTION.finditer(body)]
        expected = bool(intervals and any(
            not any(start < action.end() and action.start() < end for start, end in intervals)
            for action in schedule._ACTION_MENTION.finditer(body)))
        assert schedule._has_independent_action(body) is expected


@pytest.mark.parametrize("project_id,message_id,digest,limit", [
    (True, 1, "a" * 64, 1), (1, False, "a" * 64, 1), (1, 1, "bad", 1),
    (1, 1, "a" * 64, 0), (1, 1, "a" * 64, 1001),
])
def test_compact_source_rejects_invalid_scope_or_limits(project_id, message_id, digest, limit):
    with pytest.raises(ValueError, match="invalid_intervention_input"):
        schedule.compact_source_record(project_id, message_id, digest, "本文", POSTED, max_records=limit)


def _source_schedule_test_page(db, *args, **kwargs):
    if not db.in_transaction:
        db.execute("BEGIN")
    return schedule.read_source_schedule_page(db, *args, **kwargs)


def test_source_schedule_pages_require_the_callers_snapshot(source_db):
    assert not source_db.in_transaction
    with pytest.raises(ValueError, match='intervention_snapshot_required'):
        schedule.read_source_schedule_page(source_db, 1)


def test_source_schedule_pages_cover_over_200_posts_with_scope_and_gaps(source_db):
    body = "看護師が2026年10月3日に本人を訪問する予定です。"
    for mid in range(1, 206):
        put_source(source_db, mid, 1, body)
    put_source(source_db, 206, 2, body)
    put_source(source_db, 40, 1, body, state="snippet")
    put_source(source_db, 41, 1, body, state="deleted")
    put_source(source_db, 42, 1, body, digest="0" * 64)
    source_db.execute("UPDATE patients SET fetch_state='incomplete' WHERE project_id=1")
    cursor, records, coverage_rows = None, [], []
    for _ in range(100):
        page = _source_schedule_test_page(source_db, 1, cursor=cursor, limit=17, max_records=9)
        records.extend(page['records'])
        coverage_rows.append(page['coverage'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    else:
        pytest.fail('pagination did not reach the end')
    ids = [record['message_id'] for record in records]
    assert ids == [mid for mid in range(205, 0, -1) if mid not in (40, 41, 42)]
    assert all(record['project_id'] == 1 and record['quote'] == body for record in records)
    assert sum(row['messages_examined'] for row in coverage_rows) == 205
    assert sum(row['incomplete'] for row in coverage_rows) == 1
    assert sum(row['deleted'] for row in coverage_rows) == 1
    assert sum(row['invalid_source'] for row in coverage_rows) == 1
    assert coverage_rows[-1]['tail_reached'] is True
    assert all(row['source_complete'] is None and row['extraction'] == 'not_assessed'
               and row['fetch_state'] == 'incomplete' for row in coverage_rows)


def test_source_schedule_pages_resume_inside_one_message_without_loss(source_db):
    sentence = "看護師が2026年10月3日に本人を訪問する予定です。"
    body = sentence * 7
    put_source(source_db, 1, 1, body)
    put_source(source_db, 2, 1, body)
    cursor, records = None, []
    for _ in range(20):
        page = _source_schedule_test_page(source_db, 1, cursor=cursor, limit=1, max_records=2)
        records.extend(page['records'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    else:
        pytest.fail('pagination did not reach the end')
    assert [(r['message_id'], r['span']) for r in records] == [
        (mid, [index * len(sentence), (index + 1) * len(sentence)])
        for mid in (2, 1) for index in range(7)]
    assert all(record['state'] == 'unknown' and record['quote'] is None for record in records)
    assert page['coverage']['tail_reached'] is True and page['coverage']['source_complete'] is None


@pytest.mark.parametrize('change', ['body', 'posted', 'deleted', 'other_project'])
def test_source_schedule_partial_cursor_rejects_changed_source(source_db, change):
    body = "看護師が2026年10月3日に本人を訪問する予定です。" * 3
    put_source(source_db, 2, 1, body)
    first = _source_schedule_test_page(source_db, 1, limit=5, max_records=1)
    assert first['next_cursor']['source_fp'] is not None
    if change == 'body':
        put_source(source_db, 2, 1, body + "改訂。")
    elif change == 'posted':
        source_db.execute('UPDATE messages SET posted_at_ts=posted_at_ts+86400 WHERE message_id=2')
    elif change == 'deleted':
        source_db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=2")
    else:
        source_db.execute('UPDATE messages SET project_id=2 WHERE message_id=2')
    with pytest.raises(ValueError, match='intervention_cursor_source_changed'):
        _source_schedule_test_page(source_db, 1, cursor=first['next_cursor'])


def test_source_schedule_cursor_cannot_cross_patient_or_skip_into_sentence(source_db):
    body = "看護師が2026年10月3日に本人を訪問する予定です。" * 3
    put_source(source_db, 2, 1, body)
    first = _source_schedule_test_page(source_db, 1, max_records=1)
    cursor = first['next_cursor']
    with pytest.raises(ValueError, match='invalid_intervention_cursor'):
        _source_schedule_test_page(source_db, 2, cursor=cursor)
    with pytest.raises(ValueError, match='invalid_intervention_cursor'):
        _source_schedule_test_page(source_db, 1, cursor={**cursor, 'offset': 2})
    with pytest.raises(ValueError, match='invalid_intervention_cursor'):
        _source_schedule_test_page(source_db, 1, cursor={**cursor, 'offset': True})


@pytest.mark.parametrize('state,reason,archived', [
    ('forbidden', None, 0), ('complete', 'access_revoked', 0), ('complete', None, 1),
])
def test_source_schedule_pages_fail_closed_for_inaccessible_patient(source_db, state, reason, archived):
    put_source(source_db, 1, 1, "看護師が2026年10月3日に本人を訪問する予定です。")
    source_db.execute('UPDATE patients SET fetch_state=?,fetch_reason=?,is_archived=? WHERE project_id=1',
                      (state, reason, archived))
    result = _source_schedule_test_page(source_db, 1)
    assert result['records'] == [] and result['next_cursor'] is None
    assert result['coverage']['state'] == 'scope_unavailable'
    assert result['coverage']['tail_reached'] is None


def test_source_schedule_pages_empty_tail_is_not_history_or_analysis_completion(source_db):
    source_db.execute("UPDATE patients SET fetch_state='complete' WHERE project_id=1")
    page = _source_schedule_test_page(source_db, 1)
    assert page['records'] == [] and page['next_cursor'] is None
    assert page['coverage']['tail_reached'] is True
    assert page['coverage']['source_complete'] is None
    assert page['coverage']['extraction'] == 'not_assessed'
    assert page['coverage']['relations'] == 'not_assessed'


def test_source_schedule_pages_match_existing_reader_on_a_single_page(source_db):
    planned = "看護師が2026年10月3日に本人を訪問する予定です。"
    ambiguous = "家族の話です。看護師が2026年10月3日に本人を訪問する予定です。"
    put_source(source_db, 3, 1, planned)
    put_source(source_db, 2, 1, ambiguous)
    put_source(source_db, 1, 1, planned, state='deleted')
    old = schedule.read_source_schedule(source_db, 1)
    page = _source_schedule_test_page(source_db, 1)
    assert page['records'] == old['records']
    for key in ('fetch_state', 'messages_examined', 'incomplete', 'deleted', 'invalid_source',
                'oversize', 'candidates', 'unknown', 'source_complete'):
        assert page['coverage'][key] == old['coverage'][key]
    assert page['coverage']['tail_reached'] is True


def test_source_schedule_paging_borrows_one_snapshot_across_revision(tmp_path):
    path = tmp_path / 'synthetic-paged-snapshot.db'
    writer = sqlite3.connect(path)
    reader = sqlite3.connect(path)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.executescript('''
            CREATE TABLE patients(project_id INTEGER PRIMARY KEY, fetch_state TEXT,
                                  fetch_reason TEXT, is_archived INTEGER);
            CREATE TABLE messages(message_id INTEGER PRIMARY KEY, project_id INTEGER,
                                  body_html TEXT, body_text TEXT, body_state TEXT,
                                  content_hash TEXT, posted_at_ts REAL);
            CREATE TABLE message_revisions(message_id INTEGER NOT NULL, seq INTEGER NOT NULL,
                                           PRIMARY KEY(message_id,seq));
            INSERT INTO patients VALUES(1, 'complete', NULL, 0);
        ''')
        original = '看護師が2026年10月3日に本人を訪問する予定です。' * 3
        put_source(writer, 1, 1, original)
        reader.execute('BEGIN')
        first = schedule.read_source_schedule_page(reader, 1, max_records=1)
        assert reader.in_transaction and first['next_cursor']['source_fp'] is not None
        put_source(writer, 1, 1, '看護師が2026年9月30日に本人を訪問しました。')
        second = schedule.read_source_schedule_page(reader, 1, cursor=first['next_cursor'], max_records=1)
        assert reader.in_transaction
        assert second['records'][0]['span'][0] == len(original) // 3
        reader.execute('COMMIT')
        reader.execute('BEGIN')
        with pytest.raises(ValueError, match='intervention_cursor_source_changed'):
            schedule.read_source_schedule_page(reader, 1, cursor=first['next_cursor'], max_records=1)
    finally:
        reader.close()
        writer.close()


def test_source_schedule_live_pages_explicitly_do_not_claim_one_snapshot(source_db):
    body = '看護師が2026年10月3日に本人を訪問する予定です。'
    put_source(source_db, 2, 1, body)
    put_source(source_db, 1, 1, body)
    first = _source_schedule_test_page(source_db, 1, consistency='page', limit=1)
    assert first['records'][0]['message_id'] == 2
    assert first['coverage']['snapshot_consistency'] == 'page_only_changes_unverified'
    assert first['next_cursor']['consistency'] == 'page'
    source_db.commit()
    revised = '薬剤師が2026年10月3日に本人を訪問する予定です。'
    put_source(source_db, 1, 1, revised)
    second = _source_schedule_test_page(source_db, 1, cursor=first['next_cursor'],
                                        consistency='page', limit=1)
    assert second['records'][0]['quote'] == revised
    assert second['coverage']['tail_reached'] is True
    assert second['coverage']['snapshot_consistency'] == 'page_only_changes_unverified'
    assert second['coverage']['source_complete'] is None
    with pytest.raises(ValueError, match='invalid_intervention_cursor'):
        _source_schedule_test_page(source_db, 1, cursor=first['next_cursor'], consistency='snapshot')


def test_source_schedule_live_partial_cursor_rejects_changed_source(source_db):
    body = '看護師が2026年10月3日に本人を訪問する予定です。' * 3
    put_source(source_db, 1, 1, body)
    first = _source_schedule_test_page(source_db, 1, consistency='page', max_records=1)
    source_db.commit()
    put_source(source_db, 1, 1, body + '訂正。')
    with pytest.raises(ValueError, match='intervention_cursor_source_changed'):
        _source_schedule_test_page(source_db, 1, consistency='page', cursor=first['next_cursor'])


def test_source_schedule_snapshot_mode_still_requires_caller_binding(source_db):
    put_source(source_db, 1, 1, '看護師が2026年10月3日に本人を訪問する予定です。')
    page = _source_schedule_test_page(source_db, 1, consistency='snapshot')
    assert page['coverage']['snapshot_consistency'] == 'caller_same_snapshot_required'
    assert page['coverage']['source_complete'] is None
    with pytest.raises(ValueError, match='invalid_intervention_input'):
        _source_schedule_test_page(source_db, 1, consistency='unrecognized')


@pytest.mark.parametrize('middle_state', ['full', 'deleted'])
def test_source_schedule_partial_cursor_rejects_aba_occurrence(source_db, middle_state):
    body = '看護師が2026年10月3日に本人を訪問する予定です。' * 3
    put_source(source_db, 1, 1, body)
    first = _source_schedule_test_page(source_db, 1, consistency='page', max_records=1)
    source_db.commit()
    middle = '薬剤師が2026年10月3日に本人を訪問する予定です。' if middle_state == 'full' else ''
    put_source(source_db, 1, 1, middle, state=middle_state)
    put_source(source_db, 1, 1, body)
    source_db.execute('INSERT INTO message_revisions VALUES(1,1)')
    source_db.execute('INSERT INTO message_revisions VALUES(1,2)')
    source_db.execute('INSERT INTO message_revisions VALUES(1,3)')
    with pytest.raises(ValueError, match='intervention_cursor_source_changed'):
        _source_schedule_test_page(source_db, 1, consistency='page', cursor=first['next_cursor'])


@pytest.mark.parametrize('problem', ['missing_table', 'invalid_seq'])
def test_source_schedule_partial_cursor_rejects_unknown_occurrence(source_db, problem):
    body = '看護師が2026年10月3日に本人を訪問する予定です。' * 3
    put_source(source_db, 1, 1, body)
    if problem == 'missing_table':
        source_db.execute('DROP TABLE message_revisions')
    else:
        source_db.execute("INSERT INTO message_revisions VALUES(1,'not-an-integer')")
    with pytest.raises(ValueError, match='intervention_cursor_revision_unavailable'):
        _source_schedule_test_page(source_db, 1, max_records=1)


def test_source_schedule_page_fetches_only_needed_rows_and_one_lookahead(source_db):
    body = '看護師が2026年10月3日に本人を訪問する予定です。'
    for mid in range(1, 206):
        put_source(source_db, mid, 1, body)
    fetched = []
    original_factory = source_db.row_factory
    def observe(cursor, row):
        if cursor.description and [column[0] for column in cursor.description[:2]] == ['message_id', 'body_state']:
            fetched.append(row[0])
        return row
    source_db.row_factory = observe
    try:
        page = _source_schedule_test_page(source_db, 1, limit=200, max_records=1)
    finally:
        source_db.row_factory = original_factory
    assert [record['message_id'] for record in page['records']] == [205]
    assert fetched == [205, 204]
    assert page['coverage']['messages_examined'] == 1
    assert page['next_cursor']['message_id'] == 205


@pytest.mark.parametrize('middle_state', ['full', 'deleted'])
def test_partial_cursor_rejects_aba_with_real_ledger_writer(tmp_path, middle_state):
    from extract_testkit import _ledger, _message

    ledger = _ledger(tmp_path)
    try:
        ledger.ensure_patient(1)
        body = '看護師が2026年10月3日に本人を訪問する予定です。' * 3
        original = _message(mid=1, body=body)
        ledger.save_messages([original], project_id=1)
        first_source = ledger.db.execute(
            'SELECT content_hash,body_html,body_text,body_state,posted_at_ts FROM messages WHERE message_id=1'
        ).fetchone()
        before = tuple(first_source)
        before_seq = ledger.db.execute(
            'SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=1').fetchone()[0]
        ledger.db.execute('BEGIN')
        first = schedule.read_source_schedule_page(ledger.db, 1, consistency='page', max_records=1)
        assert first['next_cursor']['source_fp'] is not None
        ledger.db.commit()
        middle = (_message(mid=1, body='薬剤師が2026年10月3日に本人を訪問する予定です。')
                  if middle_state == 'full' else _message(mid=1, body='', state='deleted'))
        ledger.save_messages([middle], project_id=1)
        ledger.save_messages([original], project_id=1)
        after = tuple(ledger.db.execute(
            'SELECT content_hash,body_html,body_text,body_state,posted_at_ts FROM messages WHERE message_id=1'
        ).fetchone())
        after_seq = ledger.db.execute(
            'SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=1').fetchone()[0]
        assert after == before and after_seq >= before_seq + 2
        ledger.db.execute('BEGIN')
        with pytest.raises(ValueError, match='intervention_cursor_source_changed'):
            schedule.read_source_schedule_page(ledger.db, 1, consistency='page', cursor=first['next_cursor'])
        assert ledger.db.in_transaction  # the reader does not finish the borrowed snapshot
        ledger.db.rollback()
    finally:
        ledger.close()
