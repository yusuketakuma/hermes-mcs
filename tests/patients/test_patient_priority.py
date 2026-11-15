"""Shared SQL and Python ordering retain active, latest, tie and NULL semantics."""
from contextlib import closing
import sqlite3

import pytest

import patient_priority


def test_python_and_sql_share_activity_order_and_archive_cohorts():
    rows = [(9, 1, 100), (3, 0, None), (2, 0, 10), (1, 0, 10), (4, 0, 0), (5, 0, -1)]
    with closing(sqlite3.connect(':memory:')) as db:
        db.execute('CREATE TABLE patients(project_id INTEGER, is_archived INTEGER)')
        db.execute('CREATE TABLE messages(project_id INTEGER, posted_at_ts REAL)')
        db.executemany('INSERT INTO patients VALUES(?,?)', [(pid, archived) for pid, archived, _ in rows])
        db.executemany('INSERT INTO messages VALUES(?,?)', [(pid, latest) for pid, _, latest in rows])
        actual = [row[0] for row in db.execute(
            'SELECT p.project_id FROM patients p ORDER BY p.is_archived, '
            + patient_priority.activity_order_sql('p.project_id'))]
    expected = [1, 2, 4, 5, 3, 9]
    assert actual == expected
    assert [row[0] for row in sorted(rows, key=lambda row:
        patient_priority.priority_key(row[0], row[2], archived=bool(row[1])))] == expected


def test_null_project_has_sql_ascending_tie_order_without_becoming_a_patient():
    assert patient_priority.priority_key(None, 10) < patient_priority.priority_key(1, 10)


@pytest.mark.parametrize('column', ['patient', 'm.project_id DESC', 'm.project_id; DROP TABLE patients'])
def test_sql_projection_rejects_unowned_column_expressions(column):
    with pytest.raises(ValueError, match='patient_priority_column_invalid'):
        patient_priority.activity_order_sql(column)
