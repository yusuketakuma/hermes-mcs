"""Snapshot exports reject stale cached dictionary candidates without DB writes."""
from copy import deepcopy
from pathlib import Path

import pytest

import brain_export
import drug_map
from ledger import publish_snapshot
import rollup
from test_drug_map import DOCUMENT, _dictionary, _seed, store as store


@pytest.mark.parametrize('mode', ['switch', 'disable', 'corrupt'])
def test_export_cache_refs_follow_snapshot_active_generation(store, tmp_path, monkeypatch, mode):
    _seed(store)
    drug_map.derive(store, _dictionary(tmp_path))
    rollup.rebuild(store, 1)
    original = store.db.execute("SELECT content FROM artifacts WHERE kind=?", (rollup.KIND,)).fetchone()[0]
    snapshot = Path(publish_snapshot(str(tmp_path / 'ledger.db'), str(tmp_path / 'snapshots')))
    out = tmp_path / 'export'
    assert brain_export.run(out, snapshot)['ok']
    assert '架空成分甲' in (out / 'patients' / 'p1.md').read_text()
    if mode == 'switch':
        document = deepcopy(DOCUMENT)
        document['dict_id'] = 'fictional-replacement'
        drug_map.derive(store, _dictionary(tmp_path, document), deadline=0)
    elif mode == 'disable':
        drug_map.derive(store, None, deadline=0)
    else:
        store.db.execute("UPDATE artifacts SET content='[' WHERE kind=?", (drug_map.PROGRESS_KIND,))
        store.db.commit()
    assert store.db.execute("SELECT content FROM artifacts WHERE kind=?", (rollup.KIND,)).fetchone()[0] == original
    assert store.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?", (drug_map.KIND,)).fetchone()[0] == 1
    snapshot = Path(publish_snapshot(str(tmp_path / 'ledger.db'), str(tmp_path / 'snapshots')))
    before = snapshot.read_bytes()
    monkeypatch.setattr(drug_map, 'load', lambda *a, **k: pytest.fail('dictionary file access'))
    assert brain_export.run(out, snapshot)['ok']
    patient = (out / 'patients' / 'p1.md').read_text()
    assert '架空成分甲' not in patient and 'キラナ' in patient
    assert 'med_ref_generation' not in (out / 'export.jsonl').read_text()
    assert snapshot.read_bytes() == before
