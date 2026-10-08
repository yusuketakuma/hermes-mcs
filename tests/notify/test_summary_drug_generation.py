"""Cached summaries retain raw medication/vitals while dictionary candidates expire."""
from copy import deepcopy
import json

import pytest

import drug_map
import notify_views
import rollup
from test_drug_map import DOCUMENT, _dictionary, _seed, store as store


def _summary_cache(store, tmp_path):
    _seed(store)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    rollup.rebuild(store, 1)
    row = store.db.execute("SELECT content FROM artifacts WHERE kind=?", (rollup.KIND,)).fetchone()
    content = json.loads(row[0])
    content['latest_vitals'] = {'at': '2026-09-19', 'sbp': 130, 'dbp': 80}
    content['next_planned'] = '架空の次回予定'
    store.db.execute("UPDATE artifacts SET content=? WHERE kind=?", (json.dumps(content), rollup.KIND))
    store.db.commit()
    return dictionary


@pytest.mark.parametrize('mode', ['switch', 'disable', 'corrupt'])
def test_cached_summary_hides_candidate_before_budgeted_rebuild(store, tmp_path, monkeypatch, mode):
    _summary_cache(store, tmp_path)
    before = store.db.execute("SELECT content FROM artifacts WHERE kind=?", (rollup.KIND,)).fetchone()[0]
    assert '架空成分甲' in notify_views.patient_summary_text(store.db, 1)[1]
    if mode == 'switch':
        document = deepcopy(DOCUMENT)
        document['dict_id'] = 'fictional-replacement'
        drug_map.derive(store, _dictionary(tmp_path, document), deadline=0)
    elif mode == 'disable':
        drug_map.derive(store, None, deadline=0)
    else:
        store.db.execute("UPDATE artifacts SET content='[]' WHERE kind=?", (drug_map.PROGRESS_KIND,))
        store.db.commit()
    monkeypatch.setattr(drug_map, 'load', lambda *a, **k: pytest.fail('dictionary file access'))
    store.db.execute('PRAGMA query_only=ON')
    body = notify_views.patient_summary_text(store.db, 1)[1]
    assert '架空成分甲' not in body and 'キラナ' in body
    assert '130/80' in body and '架空の次回予定' in body
    assert store.db.execute("SELECT content FROM artifacts WHERE kind=?", (rollup.KIND,)).fetchone()[0] == before
    assert store.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?", (drug_map.KIND,)).fetchone()[0] == 1


def test_legacy_unstamped_cache_requires_current_source_only_with_marker(store, tmp_path):
    _summary_cache(store, tmp_path)
    store.db.execute("UPDATE artifacts SET meta='{}' WHERE kind=?", (rollup.KIND,))
    store.db.commit()
    assert '架空成分甲' in notify_views.patient_summary_text(store.db, 1)[1]
    # Legacy cache has no generation stamp; active-source validation still
    # rejects a candidate after its source binding changes.
    store.db.execute("UPDATE messages SET content_hash=?", ('b' * 64,))
    store.db.commit()
    assert '架空成分甲' not in notify_views.patient_summary_text(store.db, 1)[1]
    store.db.execute("DELETE FROM artifacts WHERE kind=?", (drug_map.PROGRESS_KIND,))
    store.db.commit()
    assert '架空成分甲' in notify_views.patient_summary_text(store.db, 1)[1]


def test_corrupt_cached_ref_cannot_hide_raw_medication(store, tmp_path):
    _summary_cache(store, tmp_path)
    row = store.db.execute("SELECT content FROM artifacts WHERE kind=?", (rollup.KIND,)).fetchone()
    data = json.loads(row[0])
    data['medications'][0]['ref']['cands'][0]['display'] = '破損候補'
    store.db.execute("UPDATE artifacts SET content=? WHERE kind=?", (json.dumps(data), rollup.KIND))
    store.db.commit()
    body = notify_views.patient_summary_text(store.db, 1)[1]
    assert '破損候補' not in body and 'キラナ' in body and '130/80' in body
