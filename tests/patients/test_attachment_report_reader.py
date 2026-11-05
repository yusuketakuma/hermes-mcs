"""Synthetic report pages seek in both directions without rehydrating previous ready pages."""
import json

import pytest

import attachment_extraction
import attachment_ocr
import attachment_report_reader as reader
from notify_testkit import led, pinned_clock
from test_ocr_empty_report_navigation import _seed

__all__ = ['led', 'pinned_clock']


def _pages(result):
    return [(source['attachment_id'], source['page']) for source in result['sources']]


def _clone_attachment(ledger, artifact_id, aid=8, mid=100):
    doc = json.loads(ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?',
                                     (artifact_id,)).fetchone()[0])
    doc['attachment_id'] = aid
    with ledger.db:
        ledger.db.execute("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                          "VALUES(?,?,'downloaded',?,32,1)", (aid, mid, 'a' * 64))
    return ledger.artifact_add(attachment_ocr.KIND, json.dumps(doc), project_id=1,
                              message_id=mid, meta={'attachment_id': aid})


def test_forward_late_page_never_rechecks_the_earlier_displayed_sources(led, monkeypatch, pinned_clock):
    _, cfg, _ = _seed(led, monkeypatch, count=12)
    source = attachment_extraction.source
    calls = []

    def counted(db, pid, key, **kwargs):
        calls.append(int(key.rsplit(':', 1)[1]))
        return source(db, pid, key, **kwargs)

    monkeypatch.setattr(attachment_extraction, 'source', counted)
    before = led.db.total_changes
    led.db.execute('PRAGMA query_only=ON')
    first = reader.read_page(led.db, 1, [100], cfg=cfg)
    assert _pages(first) == [(7, page) for page in range(1, 6)]
    assert calls == list(range(1, 7))
    calls.clear()
    second = reader.read_page(led.db, 1, [100], cfg=cfg, cursor=first['next_cursor'])
    assert _pages(second) == [(7, page) for page in range(6, 11)]
    assert calls == list(range(6, 12))
    calls.clear()
    third = reader.read_page(led.db, 1, [100], cfg=cfg, cursor=second['next_cursor'])
    assert _pages(third) == [(7, 11), (7, 12)]
    assert calls == [11, 12] and third['next_cursor'] is None
    assert led.db.total_changes == before


def test_before_selects_five_then_reverses_without_losing_the_lookahead(led, monkeypatch, pinned_clock):
    _, cfg, _ = _seed(led, monkeypatch, count=12)
    tail = reader.read_page(led.db, 1, [100], cfg=cfg,
                           cursor={'direction': 'before', 'attachment_id': 7, 'page': 13})
    assert _pages(tail) == [(7, page) for page in range(8, 13)]
    middle = reader.read_page(led.db, 1, [100], cfg=cfg, cursor=tail['prev_cursor'])
    assert _pages(middle) == [(7, page) for page in range(3, 8)]
    again = reader.read_page(led.db, 1, [100], cfg=cfg, cursor=middle['next_cursor'])
    assert _pages(again) == _pages(tail)
    first = reader.read_page(led.db, 1, [100], cfg=cfg, cursor=middle['prev_cursor'])
    assert _pages(first) == [(7, 1), (7, 2)] and first['prev_cursor'] is None
    assert _pages(reader.read_page(led.db, 1, [100], cfg=cfg, cursor=first['next_cursor'])) == _pages(middle)


def test_keyset_crosses_attachment_boundaries_in_both_directions(led, monkeypatch, pinned_clock):
    _, cfg, aid = _seed(led, monkeypatch, count=3)
    _clone_attachment(led, aid)
    first = reader.read_page(led.db, 1, [100], cfg=cfg)
    assert _pages(first) == [(7, 1), (7, 2), (7, 3), (8, 1), (8, 2)]
    last = reader.read_page(led.db, 1, [100], cfg=cfg, cursor=first['next_cursor'])
    assert _pages(last) == [(8, 3)]
    restored = reader.read_page(led.db, 1, [100], cfg=cfg, cursor=last['prev_cursor'])
    assert _pages(restored) == _pages(first)


@pytest.mark.parametrize('direction', ['after', 'before'])
def test_empty_bounded_windows_keep_a_cursor_toward_later_ready_pages(led, monkeypatch, pinned_clock, direction):
    _, cfg, _ = _seed(led, monkeypatch, count=7)
    monkeypatch.setattr(reader, 'SCAN_LIMIT', 2)
    monkeypatch.setattr(reader, 'READ_SECONDS', 1000)
    current = attachment_extraction.current
    empty = {1, 2, 3, 4} if direction == 'after' else {4, 5, 6, 7}

    def pending(db, pid, key, **kwargs):
        return None if int(key.rsplit(':', 1)[1]) in empty else current(db, pid, key, **kwargs)

    monkeypatch.setattr(attachment_extraction, 'current', pending)
    cursor = None if direction == 'after' else {'direction': 'before', 'attachment_id': 7, 'page': 8}
    key = 'next_cursor' if direction == 'after' else 'prev_cursor'
    results = []
    observed = []
    for _ in range(6):
        result = reader.read_page(led.db, 1, [100], cfg=cfg, cursor=cursor)
        results.append(result)
        observed.extend(_pages(result))
        assert result['scanned'] <= 2
        cursor = result[key]
        if cursor is None:
            break
    assert not results[0]['sources'] and not results[1]['sources']
    assert all(result['limited'] and result['pending'] == 2 and not result['range_complete'] for result in results[:2])
    assert results[0][key] != results[1][key]
    assert sorted(observed) == [(7, page) for page in range(1, 8) if page not in empty]


def test_elapsed_budget_still_advances_one_physical_page(led, monkeypatch, pinned_clock):
    _, cfg, _ = _seed(led, monkeypatch, count=3)
    monkeypatch.setattr(reader, 'READ_SECONDS', 0)
    first = reader.read_page(led.db, 1, [100], cfg=cfg)
    assert first['limited'] and first['scanned'] == 1 and _pages(first) == [(7, 1)]
    second = reader.read_page(led.db, 1, [100], cfg=cfg, cursor=first['next_cursor'])
    assert second['scanned'] == 1 and _pages(second) == [(7, 2)]


@pytest.mark.parametrize('cursor', [{}, {'direction': 'other', 'attachment_id': 7, 'page': 1},
    {'direction': 'after', 'attachment_id': True, 'page': 1},
    {'direction': 'after', 'attachment_id': 2**63, 'page': 1},
    {'direction': 'after', 'attachment_id': 7, 'page': True},
    {'direction': 'after', 'attachment_id': 7, 'page': 31},
    {'direction': 'after', 'attachment_id': 7, 'page': -1},
    {'direction': 'after', 'attachment_id': 7, 'page': 1, 'extra': 0}])
def test_malformed_cursor_is_rejected(led, monkeypatch, pinned_clock, cursor):
    _, cfg, _ = _seed(led, monkeypatch, count=1)
    with pytest.raises(ValueError, match='cursor_invalid'):
        reader.read_page(led.db, 1, [100], cfg=cfg, cursor=cursor)


def test_cursor_and_message_ids_cannot_cross_the_manifest_scope(led, monkeypatch, pinned_clock):
    _, cfg, aid = _seed(led, monkeypatch, count=1)
    _clone_attachment(led, aid, mid=101)
    with pytest.raises(ValueError, match='cursor_invalid'):
        reader.read_page(led.db, 1, [100], cfg=cfg,
                         cursor={'direction': 'after', 'attachment_id': 8, 'page': 1})
    with pytest.raises(ValueError, match='scope_invalid'):
        reader.read_page(led.db, 1, [100, 999], cfg=cfg)
    with led.db:
        led.db.execute('UPDATE messages SET project_id=2 WHERE message_id=101')
    with pytest.raises(ValueError, match='scope_invalid'):
        reader.read_page(led.db, 1, [100, 101], cfg=cfg)


@pytest.mark.parametrize('direction', ['after', 'before'])
def test_skipped_inclusive_boundary_cannot_hide_ready_rows_beyond_the_sql_window(
        led, monkeypatch, pinned_clock, direction):
    _, cfg, aid = _seed(led, monkeypatch, count=1)
    # One inclusive boundary row + 64 unready rows fills LIMIT 65; the final ready row must remain reachable.
    with led.db:
        led.db.executemany("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                          "VALUES(?,100,'downloaded',?,32,1)",
                          [(number, 'a' * 64) for number in range(8, 72)])
    _clone_attachment(led, aid, aid=72)
    view = attachment_ocr.view
    monkeypatch.setattr(attachment_ocr, 'view', lambda db, number, **kwargs:
                        {'state': 'unsupported'} if 8 <= number < 72 else view(db, number, **kwargs))
    monkeypatch.setattr(reader, 'READ_SECONDS', 1000)
    boundary, ready = (7, 72) if direction == 'after' else (72, 7)
    cursor = {'direction': direction, 'attachment_id': boundary, 'page': 1}
    key = 'next_cursor' if direction == 'after' else 'prev_cursor'
    first = reader.read_page(led.db, 1, [100], cfg=cfg, cursor=cursor)
    assert first['sources'] == [] and first['scanned'] == 64
    assert first['limited'] and first['excluded'] == 63 and not first['range_complete']
    assert first[key] is not None and first[key] != cursor
    second = reader.read_page(led.db, 1, [100], cfg=cfg, cursor=first[key])
    assert _pages(second) == [(ready, 1)]


@pytest.mark.parametrize('direction', ['after', 'before'])
def test_skip_only_boundary_advances_at_a_one_item_budget(led, monkeypatch, pinned_clock, direction):
    _, cfg, aid = _seed(led, monkeypatch, count=1)
    _clone_attachment(led, aid)
    monkeypatch.setattr(reader, 'SCAN_LIMIT', 1)
    monkeypatch.setattr(reader, 'READ_SECONDS', 1000)
    boundary, ready = (7, 8) if direction == 'after' else (8, 7)
    cursor = {'direction': direction, 'attachment_id': boundary, 'page': 1}
    key = 'next_cursor' if direction == 'after' else 'prev_cursor'
    first = reader.read_page(led.db, 1, [100], cfg=cfg, cursor=cursor)
    assert first['sources'] == [] and first['scanned'] == 1 and first['limited']
    assert first[key] == {'direction': direction, 'attachment_id': boundary,
                          'page': attachment_ocr.MAX_PAGES if direction == 'after' else 0}
    assert first[key] != cursor
    second = reader.read_page(led.db, 1, [100], cfg=cfg, cursor=first[key])
    assert _pages(second) == [(ready, 1)]
