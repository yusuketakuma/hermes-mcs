"""Cached summaries retain raw medication/vitals while dictionary candidates expire."""
import extract_llm_runner
import extract_llm_source_contract
import extract_llm_store
from copy import deepcopy
import json

import pytest

import drug_map
import extract_llm
import inference_dependency_loss
from inference_version import CURRENT_VERSION, VERSION_KEY
from extract_testkit import _hash, _message
import notify_views
import rollup
from medications_testkit import DOCUMENT, _dictionary, store as store


def _summary_cache(store, tmp_path, monkeypatch):
    monkeypatch.setattr(extract_llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm_runner, '_WRITER_STATE', extract_llm._WRITER_STATE)
    monkeypatch.setattr(extract_llm_source_contract, '_WRITER_STATE', extract_llm._WRITER_STATE)
    monkeypatch.setattr(extract_llm, "_rerender_cards", lambda *a, **k: None)
    monkeypatch.setattr(extract_llm_store, '_rerender_cards', extract_llm._rerender_cards)
    with store.db:
        store.db.execute("UPDATE patients SET fetch_state='complete', history_floor=-1 WHERE project_id=1")
    med = "本人はキラナを開始し服用しています。"
    body = med + "本人の2026-09-19測定で血圧130/80でした。架空の次回予定。"
    store.save_messages([_message(mid=1, body=body, posted_at="2026-09-19T12:00:00+09:00")])
    row = dict(store.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = inference_dependency_loss.source_binding(store.db, 1, 1)
    row["_target"] = ("", extract_llm.resolved_model({}))
    quality = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
               "completeness_chunks": 1,
               "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
    doc = {"meds": [{"name": "キラナ", "action": "start", "subject": "patient",
                      "status": "current", "negated": False, "unverified": False, "evidence": med}],
           "vitals": {"sbp": 130, "dbp": 80}}
    assert extract_llm._replace_current(store, row, json.dumps(doc, ensure_ascii=False), integrity=quality)
    store.artifact_add("extract_v1", json.dumps({"next_planned": "架空の次回予定"}),
                       project_id=1, message_id=1,
                       meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(store, 1)})
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    rollup.rebuild(store, 1, cfg={})
    return dictionary


@pytest.mark.parametrize('mode', ['switch', 'disable', 'corrupt'])
def test_cached_summary_hides_candidate_before_budgeted_rebuild(store, tmp_path, monkeypatch, mode):
    _summary_cache(store, tmp_path, monkeypatch)
    before = store.db.execute("SELECT content FROM artifacts WHERE kind=?", (rollup.KIND,)).fetchone()[0]
    assert '架空成分甲' in notify_views.patient_summary_text(store.db, 1, cfg={})[1]
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
    body = notify_views.patient_summary_text(store.db, 1, cfg={})[1]
    assert '架空成分甲' not in body and 'キラナ' in body
    assert '130/80' in body and '架空の次回予定' in body
    assert store.db.execute("SELECT content FROM artifacts WHERE kind=?", (rollup.KIND,)).fetchone()[0] == before
    assert store.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?", (drug_map.KIND,)).fetchone()[0] == 1


def test_legacy_unstamped_cache_requires_current_source_only_with_marker(store, tmp_path, monkeypatch):
    _summary_cache(store, tmp_path, monkeypatch)
    store.db.execute("UPDATE artifacts SET meta='{}' WHERE kind=?", (rollup.KIND,))
    store.db.commit()
    historical = store.db.execute("SELECT content,meta FROM artifacts WHERE kind=?", (rollup.KIND,)).fetchone()
    assert '架空成分甲' in notify_views.patient_summary_text(store.db, 1, cfg={})[1]
    # An old cache cannot authorize a candidate after its raw source changes,
    # even when the dictionary-generation marker is later absent.
    store.db.execute("UPDATE messages SET content_hash=?", ('b' * 64,))
    store.db.commit()
    assert '架空成分甲' not in notify_views.patient_summary_text(store.db, 1, cfg={})[1]
    store.db.execute("DELETE FROM artifacts WHERE kind=?", (drug_map.PROGRESS_KIND,))
    store.db.commit()
    assert '架空成分甲' not in notify_views.patient_summary_text(store.db, 1, cfg={})[1]
    assert tuple(store.db.execute("SELECT content,meta FROM artifacts WHERE kind=?", (rollup.KIND,)).fetchone()) == tuple(historical)


def test_corrupt_cached_ref_cannot_hide_raw_medication(store, tmp_path, monkeypatch):
    _summary_cache(store, tmp_path, monkeypatch)
    row = store.db.execute("SELECT content FROM artifacts WHERE kind=?", (rollup.KIND,)).fetchone()
    data = json.loads(row[0])
    data['medications'][0]['ref']['cands'][0]['display'] = '破損候補'
    store.db.execute("UPDATE artifacts SET content=? WHERE kind=?", (json.dumps(data), rollup.KIND))
    store.db.commit()
    body = notify_views.patient_summary_text(store.db, 1, cfg={})[1]
    assert '破損候補' not in body and 'キラナ' in body and '130/80' in body
