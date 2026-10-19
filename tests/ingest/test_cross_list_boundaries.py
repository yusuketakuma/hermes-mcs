"""Synthetic cross-list ownership windows reuse the real bounded wire fixture."""
import copy

import pytest

import cross_lists
import mcs_adapter
from test_cross_lists import MID, PID, STAMP, message, page, replay as replay


@pytest.mark.parametrize('cut', ['next_row', 'page_end'])
def test_row_and_page_end_deadline_checks_discard_partial_entries(replay, monkeypatch, cut):
    clock = {'now': 100.0}
    monkeypatch.setattr(mcs_adapter.time, 'monotonic', lambda: clock['now'])
    rows = [message(), message(MID + 1)] if cut == 'next_row' else [message()]
    client, pending, calls = replay([page(rows)])
    client.set_deadline(110.0)
    original = cross_lists.normalize_entry
    normalized = []

    def normalise(row, seen_ids):
        result = original(row, seen_ids)
        normalized.append(result.message.message_id)
        clock['now'] = 103.0
        return result

    monkeypatch.setattr(cross_lists, 'normalize_entry', normalise)
    result = client.fetch_cross_list('mentioned', deadline_s=2)
    assert result.reason == 'deadline_exceeded' and not result.complete
    assert result.entries == [] and result.pages == 1 and result.timestamp == STAMP
    assert normalized == [MID] and len(calls) == 1 and not pending
    assert client._deadline == 110.0


@pytest.mark.parametrize('raw,options,reason', [
    (page([], has_next=True), {}, 'schema_error'),
    (page([message()], total_entries=0), {}, 'schema_error'),
    (page([message()], has_next=True, timestamp=None, total_entries='SYNTHETIC_BAD'),
     {}, 'snapshot_missing'),
    (page([message(), message(MID + 1)], total_entries='SYNTHETIC_BAD'),
     {'max_rows': 1}, 'row_limit'),
])
def test_failed_capture_retains_page_facts_and_validation_precedence(replay, raw, options, reason):
    client, pending, calls = replay([raw])
    result = client.fetch_cross_list('mentioned', **options)
    assert result.reason == reason and not result.complete and result.entries == []
    assert result.pages == 1 and result.timestamp == raw['paginate']['timestamp']
    assert len(calls) == 1 and not pending and client._deadline is None


def test_pure_page_and_scope_contract_does_not_mutate_borrowed_data():
    raw = page([message(is_unread=True, reactions=[])], total_entries=1)
    before = copy.deepcopy(raw)
    seen = {(PID + 1, MID + 1)}
    parsed = cross_lists.parse_page(raw, page=1, per_page=20,
                                   previous_timestamp=None, total_pages=None)
    reason, total = cross_lists.validate_page_rows(
        parsed, entry_count=0, max_rows=100, total=None)
    entry = cross_lists.normalize_entry(parsed.rows[0], seen)
    cross_lists.validate_page_total(has_next=parsed.has_next, entry_count=1, total=total)
    assert raw == before and seen == {(PID + 1, MID + 1)}
    assert reason is None and total == 1 and parsed.timestamp == STAMP
    assert entry.message.project_id == PID and entry.message.message_id == MID
    assert entry.is_unread is True and entry.message.metadata == {'reactions': []}


def test_cache_load_is_explicit_and_never_bootstraps_or_calls_worker(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('explicit cached-session operation must not contact browser or wire')

    client = mcs_adapter.MCSAdapter(token_cache='SYNTHETIC_CACHE', worker=forbidden)
    reads = []
    monkeypatch.setattr(client, '_read_cache', lambda: reads.append(True) or 'SYNTHETIC_TOKEN')
    monkeypatch.setattr(client, 'bootstrap_token', forbidden)
    assert client.fetch_cross_list('mentioned').reason == 'cached_session_required'
    assert reads == []
    assert client.load_cached_session() is True
    assert reads == [True] and client._token == 'SYNTHETIC_TOKEN'
