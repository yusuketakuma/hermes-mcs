"""Rollup dictionary currency is fenced, stable across bounded cursor progress."""
from copy import deepcopy
import json

import drug_map
import rollup
from test_drug_map import DOCUMENT, _dictionary, _seed, store as store
from ingest_testkit import _message


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
    build = rollup.build_rollup
    def changed_before_build(ledger, pid):
        drug_map.derive(ledger, replacement, deadline=0)
        return build(ledger, pid)
    monkeypatch.setattr(rollup, 'build_rollup', changed_before_build)
    rollup.rebuild(store, 1)
    _, meta = _cached(store)
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


def test_generation_change_during_cached_source_read_drops_only_refs(store, tmp_path, monkeypatch):
    _seed(store)
    drug_map.derive(store, _dictionary(tmp_path))
    rollup.rebuild(store, 1)
    cached, meta = _cached(store)
    document = deepcopy(DOCUMENT)
    document['dict_id'] = 'fictional-replacement'
    replacement = _dictionary(tmp_path, document)
    real = rollup.current_refs
    def racing(db, mid):
        refs = real(db, mid)
        drug_map.derive(store, replacement, deadline=0)
        return refs
    monkeypatch.setattr(rollup, 'current_refs', racing)
    rendered = rollup.current_cached_refs(store.db, 1, cached, meta)
    assert rendered['medications'][0]['name'] == 'キラナ'
    assert 'ref' not in rendered['medications'][0]
    assert 'ref' in cached['medications'][0]  # neither cache nor raw artifacts rewritten
