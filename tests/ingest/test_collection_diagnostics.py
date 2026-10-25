"""Collection observations preserve privacy, read ownership, limits and alert semantics."""
import copy
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

import collection_diagnostics as diagnostics
import health_report
import health_watch


@pytest.fixture
def db():
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript('CREATE TABLE patients(project_id INTEGER, fetch_state TEXT, fetch_reason TEXT, is_archived INTEGER);'
                         'CREATE TABLE messages(message_id INTEGER,project_id INTEGER,parent_id INTEGER,body_state TEXT,first_seen REAL);')
        yield db


def insert(db, pid=1, state='incomplete', reason='parent_body_incomplete', archived=0,
           mid=1, parent=None, body='unknown', first=100):
    db.execute('INSERT INTO patients VALUES(?,?,?,?)', (pid, state, reason, archived))
    db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (mid, pid, parent, body, first))
    db.commit()


def observe(db, errors=None, now=3700):
    return health_report.collection_diagnostics_health(db, [] if errors is None else errors, now)


def test_counts_use_active_incomplete_patients_and_original_observation(db):
    insert(db)
    insert(db, pid=2, mid=2, reason='thread_incomplete', body='full')
    insert(db, pid=3, mid=3, state='complete')
    insert(db, pid=4, mid=4, archived=1)
    insert(db, pid=5, mid=5, parent=10, body='snippet')
    block = observe(db, ['project 1: parent_body_incomplete', 'attachment 4: fs_OSError'])
    assert block['incomplete'] == {'state': 'known', 'projects': 3,
        'reasons': {**dict.fromkeys(diagnostics.FETCH_LABELS, 0), 'parent_body_incomplete': 2, 'thread_incomplete': 1},
        'parent_bodies': 1, 'oldest_observed_age_s': 3600, 'age_unknown': 0}
    assert block['errors']['reasons']['incomplete'] == block['errors']['reasons']['storage'] == 1
    assert diagnostics.validated(block, fine=True) == block
    assert '最長1.0時間' in diagnostics.detail_lines(block, 'incomplete')[1]
    db.execute('UPDATE patients SET fetch_state="complete"')
    db.commit()
    assert observe(db)['incomplete']['parent_bodies'] == 0


@pytest.mark.parametrize('first', [None, -1, 4000, 'synthetic-secret'])
def test_unknown_or_future_time_is_not_zero_or_fabricated_age(db, first):
    insert(db, first=first)
    block = observe(db)
    assert block['incomplete']['oldest_observed_age_s'] is None
    assert block['incomplete']['age_unknown'] == 1
    assert '経過時間不明' in diagnostics.detail_lines(block, 'incomplete')[1]
    assert 'synthetic-secret' not in json.dumps(block)


def test_incomplete_scan_does_not_visit_complete_patients_history(db):
    db.executescript('DROP TABLE patients;'
                     'CREATE TABLE patients(project_id INTEGER PRIMARY KEY, fetch_state TEXT, fetch_reason TEXT, is_archived INTEGER);'
                     'CREATE INDEX idx_messages_project_time ON messages(project_id);'
                     'CREATE INDEX idx_messages_parent ON messages(parent_id);')
    insert(db, pid=1, state='complete', body='full')
    db.executemany('INSERT INTO messages VALUES(?,1,NULL,"full",100)',
                   ((mid,) for mid in range(2, 10002)))
    insert(db, pid=2, mid=10002)
    steps = 0

    def stop_unrelated_scan():
        nonlocal steps
        steps += 100
        return int(steps > 2000)

    db.set_progress_handler(stop_unrelated_scan, 100)
    try:
        counts = diagnostics.incomplete_counts(db, 3700, lambda: None)
    finally:
        db.set_progress_handler(None, 0)
    assert counts['projects'] == counts['parent_bodies'] == 1
    assert counts['oldest_observed_age_s'] == 3600


def test_unknown_reason_and_mixed_ages_do_not_disclose_raw_data(db):
    insert(db, reason='SYNTHETIC-PRIVATE-NAME', first=None)
    insert(db, pid=2, mid=2, first=100)
    block = observe(db, ['project 3: http_error(status=429): secret-url',
                         'thread 4: schema_error: SYNTHETIC-PRIVATE-NAME',
                         'cmd_invalid: private-token', 'SYNTHETIC-PRIVATE-NAME'])
    assert block['errors']['reasons'] == {**dict.fromkeys(diagnostics.ERROR_LABELS, 0),
        'rate_limit': 1, 'schema': 1, 'command': 1, 'other': 1}
    assert block['incomplete']['reasons']['other'] == 1
    assert block['incomplete']['oldest_observed_age_s'] == 3600
    assert block['incomplete']['age_unknown'] == 1
    public = json.dumps(block) + ''.join(diagnostics.detail_lines(block, 'errors'))
    assert all(secret not in public for secret in ('secret-url', 'private-token', 'SYNTHETIC-PRIVATE-NAME'))


@pytest.mark.parametrize('error,reason', [
    ('session_expired: synthetic', 'authentication'), ('deadline_exceeded', 'deadline'),
    ('probe 42: http_error(status=503): private', 'network'),
    ('project 42: db_write_failed', 'storage'), ('update_spawn_failed', 'command'),
])
def test_fixed_producer_error_classes(error, reason):
    counts = diagnostics.error_counts([error])
    assert counts['reasons'][reason] == counts['total'] == 1


def test_error_work_is_bounded_and_unknown_is_distinct_from_zero(db):
    errors = ['deadline_exceeded'] * (diagnostics.MAX_ERRORS + 10)
    block = observe(db, errors)
    assert block['errors']['state'] == 'partial'
    assert block['errors']['total'] == diagnostics.MAX_ERRORS + 10
    assert sum(block['errors']['reasons'].values()) == diagnostics.MAX_ERRORS
    assert diagnostics.validated(block, fine=True) == block
    assert diagnostics.error_counts(None)['total'] is None
    assert observe(db)['incomplete']['projects'] == 0


def test_row_bound_and_sql_errors_are_unknown_not_empty(db, monkeypatch):
    insert(db)
    insert(db, pid=2, mid=2)
    monkeypatch.setattr(diagnostics, 'MAX_ROWS', 1)
    assert observe(db)['incomplete'] == diagnostics.unknown_incomplete()
    assert db.execute('SELECT count(*) FROM messages').fetchone()[0] == 2
    db.execute('DROP TABLE messages')
    db.commit()
    assert observe(db)['incomplete'] == diagnostics.unknown_incomplete()


def test_borrowed_reader_keeps_transaction_wait_and_data(db):
    insert(db)
    db.execute('PRAGMA busy_timeout=4321')
    db.execute('INSERT INTO patients VALUES(9,"complete",NULL,0)')
    assert db.in_transaction
    observe(db)
    assert db.in_transaction
    assert db.execute('PRAGMA busy_timeout').fetchone()[0] == 4321
    db.rollback()
    assert db.execute('SELECT COUNT(*) FROM patients').fetchone()[0] == 1


def test_expired_observation_does_not_escape_budget(db, monkeypatch):
    insert(db)
    clock = iter([0, 0, 1])
    monkeypatch.setattr(health_report, 'time', SimpleNamespace(monotonic=lambda: next(clock, 1)))
    block = observe(db)
    assert block['incomplete'] == diagnostics.unknown_incomplete()
    assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1


@pytest.mark.parametrize('change', ['bool_count', 'negative', 'private_reason', 'future_age', 'wrong_generation'])
def test_public_observer_rejects_malformed_counts(db, change):
    insert(db)
    block = copy.deepcopy(observe(db))
    if change == 'bool_count':
        block['incomplete']['projects'] = True
    elif change == 'negative':
        block['errors']['total'] = -1
    elif change == 'private_reason':
        block['errors']['reasons'] = {'SYNTHETIC-SECRET': 1}
    elif change == 'future_age':
        block['incomplete']['oldest_observed_age_s'] = -1
    else:
        block['observed_at'] = 3699
    assert diagnostics.validated(block, 3700) is None


def test_monitor_uses_counts_but_keeps_stale_reasons_unknown(tmp_path, db):
    insert(db)
    block = observe(db, ['deadline_exceeded'])
    block['SYNTHETIC-SECRET'] = 'SYNTHETIC-SECRET'
    path = tmp_path / 'health.json'
    health = {'at': 3700, 'overall': 'degraded', 'state_reasons': ['stage_errors', 'collection_incomplete'],
              'collection_diagnostics': block}
    path.write_text(json.dumps(health))
    report = health_watch.classify_health(str(path), 3710, 600)
    lines = '\n'.join(health_watch._reason_rows(report, report['state_reasons']))
    assert '時間切れ1件' in lines and '親本文未取得1件' in lines and '最長1.0時間' in lines
    assert 'SYNTHETIC-SECRET' not in json.dumps(report)
    stale = health_watch.classify_health(str(path), 5000, 600)
    assert stale['state_reasons'] is None
    assert '時間切れ1件' not in '\n'.join(health_watch._reason_rows(stale, None))


# Every producer shape found in collection_stages / job_ops / init_data / run_check.
PRODUCER_CODES = [
    ("project 7: unread_cap_stuck", "incomplete"), ("mark 7: post_ack_gap", "incomplete"),
    ("deadline_exceeded", "deadline"), ("self_probe: deadline_exceeded", "deadline"),
    ("project 7: parent_body_incomplete", "incomplete"), ("project 7: db_write_failed", "storage"),
    ("mark 7: http_error(status=503)", "server"), ("mark 7: rate_limited(status=429)", "rate_limit"),
    ("mark 7: http_error(status=429)", "rate_limit"), ("mark 7: http_error(status=401)", "login"),
    ("mark 7: http_error(status=403)", "permission"), ("mark 7: http_error", "network"),
    ("thread_read: reply_stored_for_other_patient", "incomplete"), ("thread_read: reply_failed", "incomplete"),
    ("thread_read: network_error: GET /x", "network"), ("backfill 7: deadline_exceeded", "deadline"),
    ("backfill 7: coverage_stalled_window", "incomplete"), ("probe 7: history_incomplete", "incomplete"),
    ("probe 7: pages_exceeded: messages[7]", "incomplete"), ("attach 9: fs", "storage"),
    ("attach 9: http_404", "server"), ("attach 9: download_failed", "attachment"),
    ("attach 9: url_not_allowed", "attachment"), ("attach 9: download_too_large", "attachment"),
    ("attach 9: download_empty", "attachment"), ("attach 9: no_token", "login"),
    ("cmd_invalid: unparsable", "command"), ("update_spawn_failed", "command"),
    ("consent_respawn_failed", "command"), ("thread 7/8: thread_incomplete", "incomplete"),
    ("reply: invalid_payload", "schema"), ("reply 8: session_expired", "login"),
    ("thread 7: replies_missing", "incomplete"), ("import job: invalid_payload", "schema"),
    ("import 7: forbidden", "permission"), ("import 7 replies: OperationalError", "storage"),
    ("import 7: window_stalled", "incomplete"), ("reconcile 7: bootstrap_error: cdp", "browser"),
    ("discovery: schema_error: projects: page invalid", "schema"),
    ("discovery: patient_write_failed:OSError archived:none", "storage"),
    ("health_write_failed: OSError", "storage"), ("code_changed", "runtime"),
    ("extract: ValueError", "postprocess"), ("extract_llm: TimeoutError", "postprocess"),
    ("drug_map: partial", "postprocess"), ("rollup: KeyError", "postprocess"),
    ("signals: request_aging:KeyError", "postprocess"), ("cmd:OSError", "command"),
    ("restore_reconcile:OSError", "command"), ("cmd_sweep:OSError", "command"),
    ("snapshot_publish_failed", "runtime"), ("config: semantic_not_object", "config"),
    ("semantic_init: ValueError", "postprocess"), ("config: self_posts_invalid", "config"),
    ("deadline_deferred:housekeeping", "deferred"), ("run_lock_lost", "runtime"),
    ("run_record_failed: OSError", "runtime"), ("crash_site: KeyError@run_check.py:12", "runtime"),
    ("stall_check_failed: ValueError", "postprocess"), ("history 7: auto_login=failed", "login"),
    ("history 7: session_expired", "login"), ("db 7: write_failed", "storage"),
    ("mark 7: mark_result_unknown", "incomplete"), ("mark 7: bad_snapshot_ts", "schema"),
    ("probe 7: self_profile_unavailable", "auxiliary"), ("probe 7: station_staffs_unavailable", "auxiliary"),
    ("mark 7: reaction_request_failed", "auxiliary"), ("project 7: capacity_limit", "incomplete"),
    ("project 7: scope_changed", "incomplete"), ("mark 7: disk_full", "storage"),
    ({"message_id": 7, "kind": "deadline_exceeded"}, "deadline"),
    # run_check producer prefixes without an id (CodeRabbit round 2)
    ("self_profile: OSError", "auxiliary"), ("cmd_int: OSError", "command"),
    ("stamps: RuntimeError", "command"), ("cmd_int_tail: RuntimeError", "command"),
]


@pytest.mark.parametrize("error,category", PRODUCER_CODES)
def test_every_producer_code_has_a_fixed_category(error, category):
    assert diagnostics.classify(error) == category


@pytest.mark.parametrize("error", ["brand_new_code", "", None, 7, {"kind": 7}, "project 7: ???"])
def test_unknown_codes_stay_other(error):
    assert diagnostics.classify(error) == "other"


def test_fine_categories_project_onto_the_legacy_nine_reasons(db):
    block = observe(db, [error for error, _ in PRODUCER_CODES])
    errors = block["errors"]
    assert set(errors["reasons"]) == set(diagnostics.ERROR_LABELS)
    assert sum(errors["categories"].values()) == sum(errors["reasons"].values()) == len(PRODUCER_CODES)
    assert errors["categories"]["other"] == errors["reasons"]["other"] - errors["categories"]["config"] \
        - errors["categories"]["postprocess"] - errors["categories"]["runtime"]
    fine = diagnostics.validated(block, fine=True)
    assert fine == block
    legacy = diagnostics.validated(block)
    assert "categories" not in legacy["errors"] and legacy["errors"]["reasons"] == errors["reasons"]


def test_health_written_before_categories_still_validates_and_renders(db):
    block = observe(db, ["mark 7: network_error"])
    old = copy.deepcopy(block)
    del old["errors"]["categories"]
    assert diagnostics.validated(old, fine=True) == old
    assert diagnostics.detail_lines(old, "errors") == ["収集エラー1件: 通信1件"]
    assert diagnostics.guidance(old) is None


def test_inconsistent_categories_are_rejected(db):
    block = observe(db, ["mark 7: network_error"])
    bad = copy.deepcopy(block)
    bad["errors"]["categories"]["network"] = 0
    bad["errors"]["categories"]["storage"] = 1
    assert diagnostics.validated(bad, fine=True) is None


def test_guidance_gives_a_fixed_cause_and_next_step_per_category(db):
    block = observe(db, ["deadline_deferred:housekeeping", "mark 7: network_error", "mark 8: network_error"])
    rows = diagnostics.guidance(block)
    assert [row[:2] for row in rows] == [("通信", 2), ("時間内に終わらず先送り", 1)]
    assert all(row[2] and row[3] for row in rows)
    assert "housekeeping" not in json.dumps(rows, ensure_ascii=False)
    for category, (label, legacy, transient, what, todo) in diagnostics.CATEGORIES.items():
        assert legacy in diagnostics.ERROR_LABELS and type(transient) is bool and what and todo


def test_the_user_guide_lists_every_category_with_its_action():
    from pathlib import Path
    guide = (Path(__file__).resolve().parents[2] / "docs/guides/COLLECTION_DIAGNOSTICS.md").read_text(encoding="utf-8")
    for label, _legacy, transient, what, todo in diagnostics.CATEGORIES.values():
        assert f"| {label} | {what} | {'はい' if transient else 'いいえ'} | {todo} |" in guide


@pytest.mark.parametrize("error", ["cmd_sweep:OSError", "restore_reconcile:KeyError", "cmd_invalid: unparsable"])
def test_command_errors_never_advise_an_unconditional_retry(error):
    category = diagnostics.classify(error)
    assert category == "command"
    todo = diagnostics.CATEGORIES[category][4]
    assert "反映されていないと確認できた場合だけ" in todo and "確認できない場合はやり直さず" in todo
