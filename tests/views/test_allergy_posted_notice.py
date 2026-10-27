"""Synthetic posted-time allergy excerpts on a borrowed patient snapshot."""
from dataclasses import replace
import sqlite3

import pytest

import notify_views
import patient_context_refresh as refresh
import patient_context_view as views
from patient_context import extract_context
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    ledger = _db(tmp_path, ())
    facts = []
    coverage = {"unreflected": 0, "unscanned": 0, "incomplete": 0, "attachments": {"count": 0},
                "karte_records": {"stored": 0}}
    inference = {"publication_eligible": True, "complete": True}
    monkeypatch.setattr(refresh, "context_view_read", lambda *a, **k: ({
        "facts": facts, "summary": [], "coverage": coverage, "as_of": 1_790_000_000}, inference))
    yield ledger, facts, coverage, inference
    ledger.close()


def _seed(ctx, mid, body):
    ledger, facts, _, _ = ctx
    ledger.save_messages([replace(_message(mid, body=body), posted_at="2026-10-03T09:00:00+09:00")], project_id=1)
    source = {**dict(ledger.db.execute("SELECT * FROM messages WHERE project_id=1 AND message_id=?", (mid,)).fetchone()),
              "source": "chat"}
    created = refresh._facts(source, extract_context(body), night_id="2026-10-03", discovery="new_report")
    assert created
    for fact in created:
        fact["view_bucket"] = "unknown"
    facts.extend(created)
    return created[0]


def _records(ctx):
    ledger = ctx[0]
    if not ledger.db.in_transaction:
        ledger.db.execute("BEGIN")
    return views.allergy_posted_records(ledger.db, views.evidence(ledger.db, 1, cfg={}))


def test_posted_allergy_retains_unknown_source_and_date_in_private_view(ctx):
    body = "アレルギー: 本人はペニシリンのアレルギーがある。"
    fact = _seed(ctx, 1, body)
    ledger = ctx[0]
    before = ledger.db.total_changes
    assert _records(ctx) == [fact]
    display = notify_views.patient_context_list(ledger.db, 1, "background", cfg={}, since=0, now=1_790_000_000)
    wording = "本人はペニシリンのアレルギーがある。"
    assert any("投稿時点の記載" in row["group"] and "時点・関係未確認" in row["group"]
               and wording in row["text"] and "10/03" in row["text"] for row in display["items"])
    assert any("時点・関係未確認" in row["group"] and wording in row["text"] for row in display["items"])
    assert ledger.db.total_changes == before


def test_posted_notice_requires_the_callers_snapshot_transaction(ctx):
    _seed(ctx, 1, "アレルギー: 本人はペニシリンのアレルギーがある。")
    db = ctx[0].db
    ev = views.evidence(db, 1, cfg={})
    assert not db.in_transaction
    assert views.allergy_posted_records(db, ev) == []
    assert _records(ctx)
    assert db.in_transaction


@pytest.mark.parametrize("body", [
    "アレルギー: 家族はペニシリンのアレルギーがある。",
    "アレルギー: 本人は以前ペニシリンのアレルギーがあった。",
    "アレルギー: 本人はペニシリンのアレルギーがない。",
    "アレルギー: 本人はペニシリンのアレルギーがあるかもしれない。",
    "アレルギー: 本人は薬剤のアレルギーがある。",
    "アレルギー: 本人はペニシリンのアレルギーがあると医師に報告予定。",
    "アレルギー: 家族の話。本人はペニシリンのアレルギーがある。",
])
def test_other_subject_or_unsafe_context_is_not_a_posted_notice(ctx, body):
    _seed(ctx, 1, body)
    assert _records(ctx) == []


@pytest.mark.parametrize("other", [
    "アレルギー: 本人はペニシリンのアレルギーがありません。",
    "アレルギー: 本人の薬剤アレルギーは未確認です。",
    "アレルギー: 本人の薬剤アレルギーの記載を訂正しました。",
    "アレルギー: 本人は別の薬剤のアレルギーがある。",
])
def test_other_patient_allergy_assertions_leave_posted_notice_unconfirmed(ctx, other):
    _seed(ctx, 1, "アレルギー: 本人はペニシリンのアレルギーがある。")
    _seed(ctx, 2, other)
    assert _records(ctx) == []


def test_family_correction_is_not_discarded_as_an_independent_family_report(ctx):
    _seed(ctx, 1, "アレルギー: 本人はペニシリンのアレルギーがある。")
    _seed(ctx, 2, "アレルギー: 訂正、ペニシリンアレルギーがあるのは本人ではなく母です。")
    assert _records(ctx) == []


def test_independent_family_allergy_does_not_block_the_patient_record(ctx):
    fact = _seed(ctx, 1, "アレルギー: 本人はペニシリンのアレルギーがある。")
    _seed(ctx, 2, "アレルギー: 家族は牛乳アレルギーがあります。")
    assert _records(ctx) == [fact]


def test_current_evidence_correction_absent_from_nightly_facts_blocks_notice(ctx):
    _seed(ctx, 1, "アレルギー: 本人はペニシリンのアレルギーがある。")
    body = "アレルギー: 訂正、ペニシリンアレルギーがあるのは本人ではなく母です。"
    _seed(ctx, 2, body)
    ctx[1].pop()
    item = {"category": "allergies", "text": body, "evidence": body, "subject": "family"}
    _artifact(ctx[0], "extract_llm", 2, {"patient_context": [item]}, {"hash": _hash_of(ctx[0], 2)})
    db = ctx[0].db
    db.execute("BEGIN")
    ev = views.evidence(db, 1, cfg={})
    assert any(item.get("message_id") == 2 and item.get("quote") == body for item in ev["items"])
    assert views.allergy_posted_records(db, ev) == []


@pytest.mark.parametrize("body", [
    "生活環境: アレルギーの記録を訂正しました。",
    "生活環境: 前回の記録を訂正しました。",
])
def test_correction_from_another_category_may_refer_to_the_allergy(ctx, body):
    _seed(ctx, 1, "アレルギー: 本人はペニシリンのアレルギーがある。")
    _seed(ctx, 2, body)
    assert _records(ctx) == []


def test_a_clearly_unrelated_correction_does_not_hide_the_source(ctx):
    fact = _seed(ctx, 1, "アレルギー: 本人はペニシリンのアレルギーがある。")
    _seed(ctx, 2, "生活環境: 本人の体重の記録を訂正しました。")
    assert _records(ctx) == [fact]


@pytest.mark.parametrize("body", [
    "先ほどの件は間違いでした。", "アレルギーの記録を訂正しました。",
    "先ほどの件は違います。", "先ほどの件は違いました。",
    "前回の投稿は取り消します。", "その件はなしで。", "さっきの話は誤解でした。",
])
def test_unclassified_source_correction_blocks_the_posted_notice(ctx, body):
    _seed(ctx, 1, "アレルギー: 本人はペニシリンのアレルギーがある。")
    ctx[0].save_messages([_message(2, body=body)], project_id=1)
    assert _records(ctx) == []


@pytest.mark.parametrize("correction", [
    "本人の合成薬のアレルギーの記載は正しくありません。",
    "先ほどの記載は正しくありません。",
    "その記載は誤りです。",
])
def test_later_explicit_or_ambiguous_correction_does_not_promote_allergy(ctx, correction):
    original = "アレルギー: 本人は合成薬のアレルギーがある。"
    _seed(ctx, 1, original)
    later = replace(_message(2, body=correction), posted_at="2026-10-03T09:10:00+09:00")
    ctx[0].save_messages([later], project_id=1)
    assert _records(ctx) == []  # An ambiguous reference is held, not paired as a proven correction.
    rows = ctx[0].db.execute("SELECT message_id,body_text,body_state FROM messages "
                             "WHERE project_id=1 ORDER BY message_id").fetchall()
    assert [(row["message_id"], row["body_text"], row["body_state"]) for row in rows] == [
        (1, original, "full"), (2, correction, "full")]


@pytest.mark.parametrize("other", [
    "本日の体重の記載は正しくありません。",
    "先ほど測った体重の値は正しくありません。",
    "食事の量は正しくありません。",
    "その記載は誤りではありません。",
])
def test_later_unrelated_inaccuracy_does_not_hide_allergy(ctx, other):
    fact = _seed(ctx, 1, "アレルギー: 本人は合成薬のアレルギーがある。")
    later = replace(_message(2, body=other), posted_at="2026-10-03T09:10:00+09:00")
    ctx[0].save_messages([later], project_id=1)
    assert _records(ctx) == [fact]


@pytest.mark.parametrize("body", ["本日は晴れでした。", "本人は食事を食べていません。", "歩行の補助はなしでした。"])
def test_unrelated_unclassified_source_does_not_block_the_notice(ctx, body):
    fact = _seed(ctx, 1, "アレルギー: 本人はペニシリンのアレルギーがある。")
    ctx[0].save_messages([_message(2, body=body)], project_id=1)
    assert _records(ctx) == [fact]


def test_unclassified_other_allergy_source_blocks_the_notice(ctx):
    _seed(ctx, 1, "アレルギー: 本人はペニシリンのアレルギーがある。")
    ctx[0].save_messages([_message(2, body="アレルギーはないそうです。")], project_id=1)
    assert _records(ctx) == []


def test_source_scan_limit_and_oversized_original_fail_closed(ctx):
    _seed(ctx, 1, "アレルギー: 本人はペニシリンのアレルギーがある。")
    ctx[0].save_messages([_message(2, body="合成" * 1501)], project_id=1)
    assert _records(ctx) == []


@pytest.mark.parametrize('position', ['target', 'other'])
@pytest.mark.parametrize('field', ['body_text', 'body_html'])
@pytest.mark.parametrize('embedded_nul', [False, True])
def test_oversized_or_nul_original_never_crosses_sql_into_python(ctx, position, field, embedded_nul):
    _seed(ctx, 1, 'アレルギー: 本人は合成薬のアレルギーがある。')
    ledger = ctx[0]
    if position == 'other':
        ledger.save_messages([_message(2, body='通常の合成連絡です。')], project_id=1)
    ev = views.evidence(ledger.db, 1, cfg={})
    mid = 1 if position == 'target' else 2
    huge = ('x\0' if embedded_nul else '') + 'A' * (12001 if field == 'body_html' else 3001)
    ledger.db.execute(f'UPDATE messages SET {field}=? WHERE project_id=1 AND message_id=?', (huge, mid))
    returned = []

    def inspect(cursor, values):
        names = tuple(col[0] for col in cursor.description or ())
        if 'body_text' in names and 'body_html' in names and 'message_id' in names:
            if values[names.index('message_id')] == mid:
                returned.append(values[names.index(field)])
        return sqlite3.Row(cursor, values)

    ledger.db.row_factory = inspect
    try:
        assert views.allergy_posted_records(ledger.db, ev) == []
        assert ledger.db.in_transaction
    finally:
        ledger.db.row_factory = sqlite3.Row
        ledger.db.rollback()
    assert returned == ([] if position == 'target' else [None])


def test_foreign_oversized_source_does_not_hide_valid_patient_record(ctx):
    fact = _seed(ctx, 1, 'アレルギー: 本人は合成薬のアレルギーがある。')
    ledger = ctx[0]
    ledger.ensure_patient(2)
    ledger.save_messages([_message(2, pid=2, body='別患者の合成連絡です。')], project_id=2)
    ev = views.evidence(ledger.db, 1, cfg={})
    huge = 'x\0' + 'A' * 100_000
    ledger.db.execute('UPDATE messages SET body_text=?,body_html=? WHERE project_id=2 AND message_id=2', (huge, huge))
    assert views.allergy_posted_records(ledger.db, ev) == [fact]
    assert ledger.db.in_transaction
    ledger.db.rollback()


@pytest.mark.parametrize('code', [None, sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_allergy_source_scan_propagates_interruption_without_committing(ctx, code):
    _seed(ctx, 1, 'アレルギー: 本人は合成薬のアレルギーがある。')
    db = ctx[0].db
    ev = views.evidence(db, 1, cfg={})
    db.execute("UPDATE patients SET disease='合成変更' WHERE project_id=1")
    error = sqlite3.OperationalError('synthetic scan interruption')
    if code is not None:
        error.sqlite_errorcode = code
    calls = []

    class FaultDB:
        @property
        def in_transaction(self):
            return db.in_transaction

        def execute(self, sql, params=()):
            if 'FROM messages WHERE project_id=? LIMIT 201' in sql:
                calls.append(1)
                raise error
            return db.execute(sql, params)

    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            views.allergy_posted_records(FaultDB(), ev)
        assert caught.value is error and calls == [1]
        assert db.in_transaction
        assert db.execute('SELECT disease FROM patients WHERE project_id=1').fetchone()[0] == '合成変更'
    finally:
        db.rollback()
    assert db.execute('SELECT disease FROM patients WHERE project_id=1').fetchone()[0] != '合成変更'


def test_allergy_source_scan_known_schema_error_stays_unconfirmed(ctx):
    _seed(ctx, 1, 'アレルギー: 本人は合成薬のアレルギーがある。')
    db = ctx[0].db
    ev = views.evidence(db, 1, cfg={})
    db.execute('BEGIN')
    error = sqlite3.OperationalError('synthetic missing schema')
    error.sqlite_errorcode = sqlite3.SQLITE_ERROR
    calls = []

    class FaultDB:
        @property
        def in_transaction(self):
            return db.in_transaction

        def execute(self, sql, params=()):
            if 'FROM messages WHERE project_id=? LIMIT 201' in sql:
                calls.append(1)
                raise error
            return db.execute(sql, params)

    try:
        assert views.allergy_posted_records(FaultDB(), ev) == []
        assert calls == [1] and db.in_transaction
    finally:
        db.rollback()


def test_allergy_source_missing_column_stays_unconfirmed_before_source_read(ctx):
    _seed(ctx, 1, 'アレルギー: 本人は合成薬のアレルギーがある。')
    db = ctx[0].db
    ev = views.evidence(db, 1, cfg={})
    db.execute('BEGIN')
    source_reads = []

    class OldSchemaDB:
        @property
        def in_transaction(self):
            return db.in_transaction

        def execute(self, sql, params=()):
            if sql == 'PRAGMA table_info(messages)':
                return [row for row in db.execute(sql, params) if row[1] != 'body_html']
            source_reads.append(sql)
            return db.execute(sql, params)

    try:
        assert views.allergy_posted_records(OldSchemaDB(), ev) == []
        assert not source_reads and db.in_transaction
    finally:
        db.rollback()


def test_source_count_above_limit_fails_closed(ctx):
    _seed(ctx, 1, "アレルギー: 本人はペニシリンのアレルギーがある。")
    ctx[0].save_messages([_message(mid, body="合成") for mid in range(2, 202)], project_id=1)
    assert _records(ctx) == []


def test_out_of_range_post_time_is_not_displayed_or_formatted(ctx):
    fact = _seed(ctx, 1, "アレルギー: 本人はペニシリンのアレルギーがある。")
    ctx[0].db.execute("UPDATE messages SET posted_at_ts=? WHERE project_id=1 AND message_id=1", (1e300,))
    fact["posted_at_ts"] = 1e300
    assert _records(ctx) == []
    display = notify_views.patient_context_list(ctx[0].db, 1, "background", cfg={}, since=0, now=1_790_000_000)
    assert all("投稿時点の記載" not in row["group"] for row in display["items"])


def test_missing_coverage_or_changed_source_cannot_prove_a_notice(ctx):
    _seed(ctx, 1, "アレルギー: 本人はペニシリンのアレルギーがある。")
    ledger, _, coverage, inference = ctx
    for key in ("unreflected", "unscanned", "incomplete"):
        coverage[key] = 1
        assert _records(ctx) == []
        coverage[key] = 0
    inference["publication_eligible"] = False
    assert _records(ctx) == []
    inference["publication_eligible"] = True
    coverage["attachments"]["count"] = 1
    assert _records(ctx) == []
    coverage["attachments"]["count"] = 0
    inference["complete"] = False
    assert _records(ctx) == []
    inference["complete"] = True
    coverage["karte_records"]["stored"] = 1
    assert _records(ctx) == []
    coverage["karte_records"]["stored"] = 0
    ledger.db.execute("UPDATE messages SET body_html=? WHERE project_id=1 AND message_id=1", ("<p>changed</p>",))
    assert _records(ctx) == []
