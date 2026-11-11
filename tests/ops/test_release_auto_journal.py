"""v3 journal variant(release_auto_journal)を合成pathで検証する。

実update_state.json・実journalには触れない。tmp_path配下の合成fileのみ。
"""
import os

import pytest

import update_journal
import release_auto_journal as rj


def _record(op='op-1', event='applied', at=1000):
    return {'kind': 'release-auto-v3', 'event': event,
            'operation_id': op, 'grant_id': 'g', 'attempt_key': 'a',
            'source_commit': 'c' * 40, 'applied_at': at}


def _path(tmp_path, name='update_state.json'):
    return str(tmp_path / 'data' / name)


def test_apply_journal_preserves_existing_state(tmp_path):
    path = _path(tmp_path)
    os.makedirs(os.path.dirname(path))
    prior = update_journal.default_state()
    prior['stages'] = [{'stage': 'v1-stage', 'at': 1}]
    update_journal.save_state(path, prior)

    rj.apply_journal(_record(), state_path=path)
    state = update_journal.load_state(path)
    assert state.get('_corrupt') is not True
    # v1側の既存記録は保持される
    assert state['stages'] == [{'stage': 'v1-stage', 'at': 1}]
    variant = state['v3']
    assert variant['schema_version'] == rj.JOURNAL_V3_SCHEMA
    assert variant['applied']['op-1']['event'] == 'applied'


def test_apply_journal_idempotent_overwrite(tmp_path):
    path = _path(tmp_path)
    rj.apply_journal(_record(at=1), state_path=path)
    rj.apply_journal(_record(at=2), state_path=path)
    state = update_journal.load_state(path)
    assert len(state['v3']['applied']) == 1
    assert state['v3']['applied']['op-1']['applied_at'] == 2


def test_apply_journal_corrupt_rejected(tmp_path):
    path = _path(tmp_path)
    os.makedirs(os.path.dirname(path))
    with open(path, 'w') as f:
        f.write('{broken')
    with pytest.raises(rj.JournalError) as e:
        rj.apply_journal(_record(), state_path=path)
    assert e.value.code == 'JOURNAL_CORRUPT'
    # corrupt fileは上書きされない
    assert open(path).read() == '{broken'


def test_apply_journal_invalid_record(tmp_path):
    path = _path(tmp_path)
    with pytest.raises(rj.JournalError):
        rj.apply_journal({'operation_id': 'x'}, state_path=path)
    with pytest.raises(rj.JournalError):
        rj.apply_journal(_record(op=''), state_path=path)
    with pytest.raises(rj.JournalError):
        rj.apply_journal(_record(at='not-int'), state_path=path)


def test_apply_journal_bounded_eviction(tmp_path, monkeypatch):
    monkeypatch.setattr(rj, 'MAX_RECORDS', 3)
    path = _path(tmp_path)
    for i in range(5):
        rj.apply_journal(_record(op=f'op-{i}'), state_path=path)
    state = update_journal.load_state(path)
    applied = state['v3']['applied']
    assert len(applied) == 3
    assert 'op-0' not in applied and 'op-1' not in applied
    assert 'op-4' in applied


def test_journal_state_path(tmp_path):
    assert rj.journal_state_path('/x/data') == '/x/data/update_state.json'
