"""Paid-answer journal scan must not let ignored staging leftovers hide journals."""
import contextlib
import os

import pytest

import attachment_paid_results as journal

NAME = 'a' * 32 + '.json'


def _dir(tmp_path, tmp_count, *, journals=(NAME,)):
    d = tmp_path / 'attachment_paid_results'
    d.mkdir(mode=0o700)
    for i in range(tmp_count):
        (d / f'.atomic.{i}.tmp').write_text('x')
    for name in journals:
        (d / name).write_text('{}')
    return d


@pytest.fixture
def staging_first(monkeypatch):
    real = os.scandir

    @contextlib.contextmanager
    def ordered(path):
        with real(path) as it:
            items = sorted(it, key=lambda e: not e.name.startswith('.atomic.'))
        yield iter(items)
    monkeypatch.setattr(journal.os, 'scandir', ordered)


@pytest.mark.parametrize('leftovers', [0, 63, 64, 200])
def test_journal_found_behind_staging_leftovers(tmp_path, staging_first, leftovers):
    d = _dir(tmp_path, leftovers)
    assert journal.pending(tmp_path) == [d / NAME]


def test_scan_budget_exhausted_is_not_reported_as_empty(tmp_path, staging_first):
    _dir(tmp_path, journal.SCAN_LIMIT + 1)
    with pytest.raises(ValueError, match='attachment_paid_scan_incomplete'):
        journal.pending(tmp_path)


def test_batch_stays_bounded_and_unknown_name_still_refused(tmp_path):
    _dir(tmp_path, 0, journals=[f'{i:032x}.json' for i in range(70)])
    assert len(journal.pending(tmp_path)) == journal.BATCH
    (tmp_path / 'attachment_paid_results' / 'stray.txt').write_text('x')
    with pytest.raises(ValueError, match='attachment_paid_filename_invalid'):
        journal.pending(tmp_path)
