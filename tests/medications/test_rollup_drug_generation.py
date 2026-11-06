"""Rollup dictionary currency is fenced, stable across bounded cursor progress."""
from copy import deepcopy
import json
import sqlite3

import pytest

import drug_map
import rollup
import rollup_builder
from medications_testkit import DOCUMENT, _dictionary, _seed, store as store
from ingest_testkit import _message
from ledger import Ledger


def _cached(store):
    row = store.db.execute("SELECT content,meta FROM artifacts WHERE kind=?", (rollup.KIND,)).fetchone()
    return json.loads(row[0]), json.loads(row[1])


def test_cursor_advance_does_not_redirty_all_patients(store, tmp_path, monkeypatch):
    for mid in range(1, 8):
        _seed(store, mid=mid)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    aid = rollup.rebuild(store, 1)
    store.ensure_patient(2)
    store.save_messages([_message(mid=99, project_id=2)])
    rollup.rebuild(store, 2)
    row = store.db.execute("SELECT meta FROM artifacts WHERE kind=? AND project_id=1", (rollup.KIND,)).fetchone()
    meta = json.loads(row[0])
    assert meta['med_ref_generation'] == drug_map.generation_signature(store.db)
    clock = iter(range(1000))
    monkeypatch.setattr(drug_map.time, 'monotonic', lambda: next(clock))
    assert drug_map.derive(store, dictionary, deadline=4)['status'] == 'partial'
    assert rollup.dirty_projects(store) == []
    assert rollup.rebuild(store, 1) == aid


def test_generation_change_before_build_stays_dirty_then_settles(store, tmp_path, monkeypatch):
    _seed(store)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    document = deepcopy(DOCUMENT)
    document['dict_id'] = 'fictional-replacement'
    replacement = _dictionary(tmp_path, document)
    before = drug_map.generation_signature(store.db)
    initial_id = rollup.rebuild(store, 1)
    initial_cache = _cached(store)
    build = rollup.build_rollup
    def changed_before_build(ledger, pid, *, cfg=None):
        drug_map.derive(store, replacement, deadline=0)
        return build(ledger, pid, cfg=cfg)
    monkeypatch.setattr(rollup, 'build_rollup', changed_before_build)
    with pytest.raises(sqlite3.OperationalError, match="^inference_rollup_source_changed$"):
        rollup.rebuild(store, 1)
    _, meta = _cached(store)
    assert _cached(store) == initial_cache
    assert store.db.execute("SELECT artifact_id FROM artifacts WHERE kind=?", (rollup.KIND,)).fetchone()[0] == initial_id
    assert meta['med_ref_generation'] == before
    assert rollup.dirty_projects(store) == [1]
    monkeypatch.setattr(rollup, 'build_rollup', build)
    drug_map.derive(store, replacement)
    aid = rollup.rebuild(store, 1)
    data, meta = _cached(store)
    assert meta['med_ref_generation'] == drug_map.generation_signature(store.db)
    assert data['medications'][0]['ref']['dict_id'] == replacement.dict_id
    assert rollup.dirty_projects(store) == []
    assert rollup.rebuild(store, 1) == aid


def test_generation_change_during_cached_source_read_keeps_snapshot_consistent(store, tmp_path, monkeypatch):
    _seed(store)
    drug_map.derive(store, _dictionary(tmp_path))
    rollup.rebuild(store, 1)
    cached, meta = _cached(store)
    document = deepcopy(DOCUMENT)
    document['dict_id'] = 'fictional-replacement'
    replacement = _dictionary(tmp_path, document)
    real = rollup_builder.current_refs
    assert cached['medications'][0]['ref']['dict_id'] == 'fictional-v1'
    unknown = rollup.current_cached_refs(store.db, 1, cached, meta)
    assert unknown['rollup_freshness'] == 'unknown' and 'medications' not in unknown
    old_generation = drug_map.generation_signature(store.db)
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    other = Ledger(path)
    calls = []
    try:
        def racing(db, mid):
            refs = real(db, mid)
            calls.append((mid, refs))
            assert drug_map.derive(other, replacement, deadline=0)['status'] == 'partial'
            return refs

        monkeypatch.setattr(rollup_builder, 'current_refs', racing)
        rendered = rollup.current_cached_refs(store.db, 1, cached, meta, cfg={})
        assert [mid for mid, _ in calls] == [1]
        assert calls[0][1][0]['dict_id'] == 'fictional-v1'
        assert rendered['medications'][0]['name'] == 'キラナ'
        assert rendered['medications'][0]['ref']['dict_id'] == 'fictional-v1'
        assert drug_map.generation_signature(store.db) != old_generation
        monkeypatch.setattr(rollup_builder, 'current_refs', real)
        refreshed = rollup.current_cached_refs(store.db, 1, cached, meta, cfg={})
        assert refreshed['medications'][0]['name'] == 'キラナ'
        assert 'ref' not in refreshed['medications'][0]
        assert _cached(store) == (cached, meta)
        assert cached['medications'][0]['ref']['dict_id'] == 'fictional-v1'
    finally:
        other.close()
