"""Synthetic dated blood pressure readings do not become extra dates."""
from datetime import date, datetime
from itertools import product
import re

import pytest

import clinical_values
import patient_context_state
import rollup
from test_rollup_measurement_time import _save, store

__all__ = ['store']


def test_bp_prefix_reverse_scan_matches_original_unicode_regex():
    original = re.compile(r"(?<![A-Za-z])(?:BP|血圧)[:：は]?\s*$", re.I)
    before = ('', 'x', 'İ', 'ı', 'ſ', 'K', '家', '９')
    labels = ('BP', 'bp', 'Bp', '血圧', 'ＢＰ', 'bP')
    separators = ('', ':', '：', 'は', '::', 'は ')
    spaces = ('', ' ', '\t', '\n', '\u00a0', '\u3000', '\u0085', '\x1c')
    for left, label, separator, whitespace in product(before, labels, separators, spaces):
        prefix = left + label + separator + whitespace
        text = prefix + '90/60'
        assert clinical_values._has_bp_date_prefix(text, len(prefix)) == bool(original.search(prefix))


def test_many_bp_date_candidates_keep_one_date_and_no_false_conflict():
    body = '2026-10-02（本日）に' + '、'.join(['BP 90/60'] * 2000)
    posted = datetime.fromisoformat('2026-10-02T10:00:00+09:00').timestamp()
    assert clinical_values.written_day(body) == ('ymd', date(2026, 10, 2))
    assert not patient_context_state.paired_date_conflict(body, posted)


@pytest.mark.parametrize(('text', 'expected'), [
    ('2026-10-02測定でBP 90/60', ('ymd', date(2026, 10, 2))),
    ('2026-10-02測定で血圧９０／６０', ('ymd', date(2026, 10, 2))),
    ('2026-10-02測定でBP 10/02', None),
    ('BP 10/02', ('md', 10, 2)),
    ('2026-10-02と2026-10-03にBP 90/60', None),
    ('2026-10-02と10/03にBP 90/60', None),
    ('2026-10-02に90/60', None),
    ('2026-10-02と90/60日', None),
    ('2026-02-30測定でBP 90/60', None),
    ('血圧2026-10-02 90/60', None),
])
def test_unambiguous_bp_does_not_hide_other_date_candidates(text, expected):
    assert clinical_values.written_day(text) == expected


@pytest.mark.parametrize(('text', 'conflict'), [
    ('2026-10-02（本日）にBP 90/60', False),
    ('2026-10-02（本日）にBP 10/02', True),
    ('2026-10-02（昨日）にBP 90/60', True),
    ('本日（金）にBP 90/60', False),
    ('本日（木）にBP 90/60', True),
    ('本日（金）に血圧90/60日', True),
    ('2026-10-02（本日）と10/03にBP 90/60', True),
])
def test_bp_does_not_hide_other_relative_or_weekday_conflicts(text, conflict):
    posted = datetime.fromisoformat('2026-10-02T10:00:00+09:00').timestamp()
    assert patient_context_state.paired_date_conflict(text, posted) is conflict


def test_dated_labelled_bp_reaches_current_rollup(store):
    body = '本人の2026-10-02測定でBP 90/60でした。'
    _save(store, 1, '2026-10-03T09:00:00+09:00', body, vitals={'sbp': 90, 'dbp': 60})
    result = rollup.build_rollup(store.db, 1, cfg={})
    assert result['measurement_coverage']['complete'] is True
    assert result['latest_vitals']['sbp'] == 90
    assert result['latest_vitals']['dbp'] == 60
    reports = result['measurement_reports']['items']
    assert any(row['kind'] == 'vitals' and row['measured_on'] == '2026-10-02' for row in reports)


@pytest.mark.parametrize(('body', 'vitals'), [
    ('本人の2026-10-02測定でBP 10/02でした。', {'sbp': 10, 'dbp': 2}),
    ('本人の2026-10-02測定でBP 90/60、2026-10-03も記録。', {'sbp': 90, 'dbp': 60}),
    ('本人の2026-02-30測定でBP 90/60でした。', {'sbp': 90, 'dbp': 60}),
])
def test_ambiguous_or_invalid_dates_do_not_become_current(store, body, vitals):
    _save(store, 1, '2026-10-03T09:00:00+09:00', body, vitals=vitals)
    result = rollup.build_rollup(store.db, 1, cfg={})
    assert result['measurement_coverage']['complete'] is True
    assert 'latest_vitals' not in result
