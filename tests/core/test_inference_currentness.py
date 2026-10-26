"""Synthetic SQL and Python readers agree on inference generation boundaries."""
import json
import sqlite3

import pytest

from inference_version import VERSION_KEY, read_version, valid_meta_pred
from mcs_queries import (
    FACT_KINDS_SQL, current_extract_pred, current_fact_pred,
    current_projection_id, current_v4_id, extraction_kind_map,
    source_extract_pred,
)
from test_inference_version import CASES, _decode
from read_model import _fact_relations, _message_records
from semantic_projection import PROJECTION_VERSION

KINDS = ('extract_llm', 'canonical_projection', 'semantic_facts_v4')
MISSING = object()


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.executescript('''
        CREATE TABLE messages(message_id INTEGER PRIMARY KEY, project_id INTEGER,
          parent_id INTEGER, posted_at_ts INTEGER, body_text TEXT,
          content_hash TEXT, body_state TEXT);
        CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY, kind TEXT,
          message_id INTEGER, project_id INTEGER, content TEXT, meta TEXT);
        INSERT INTO messages VALUES(1,10,NULL,100,'synthetic source','source-hash','full');
    ''')
    try:
        yield connection
    finally:
        connection.close()


def _add(db, kind, *, version='1.0', content=None, meta=None, project_id=10):
    binding = {'hash': 'source-hash', 'engine_version': 4,
               'projection_version': PROJECTION_VERSION}
    if version is not MISSING:
        binding[VERSION_KEY] = version
    binding.update(meta or {})
    return db.execute(
        'INSERT INTO artifacts(kind,message_id,project_id,content,meta) VALUES(?,1,?,?,?)',
        (kind, project_id, json.dumps({} if content is None else content),
         json.dumps(binding))).lastrowid


def _selected(db, *, target='1.0'):
    return [r[0] for r in db.execute(
        'SELECT a.artifact_id FROM artifacts a JOIN messages m USING(message_id) '
        f'WHERE a.kind IN ({FACT_KINDS_SQL}) '
        + current_fact_pred(inference_target=target))]


def _python(db, *, target='1.0'):
    return next(_message_records(db, 10, inference_target=target))


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('version,expected', [
    (MISSING, False), ('0.1', False), ('1.0', True), ('1.1', False),
    ('1.2', False), ('2.0', False), (None, False), (1.0, False), (True, False),
])
def test_default_generation_agrees_in_sql_and_python(db, kind, version, expected):
    artifact = _add(db, kind, version=version)
    assert _selected(db) == ([artifact] if expected else [])
    rec, by_kind, _ = _python(db)
    assert (by_kind[kind]['state'] == 'current') is expected
    assert rec['state'] == ('current' if expected else
                            'unknown' if version is None or type(version) in (int, float, bool)
                            else 'stale')
    assert db.execute('SELECT count(*) FROM artifacts').fetchone()[0] == 1


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('fault', [
    'invalid_meta', 'nonobject_meta', 'invalid_content', 'wrong_project',
    'old_hash', 'deleted', 'error',
])
def test_current_version_cannot_bypass_existing_source_and_validity_fences(db, kind, fault):
    artifact = _add(db, kind, project_id=20 if fault == 'wrong_project' else 10,
                    meta={'hash': 'old'} if fault == 'old_hash' else
                         {'error': True} if fault == 'error' else {})
    if fault in ('invalid_meta', 'nonobject_meta', 'invalid_content'):
        column = 'content' if fault == 'invalid_content' else 'meta'
        raw = '[]' if fault == 'nonobject_meta' else '{broken'
        db.execute(f'UPDATE artifacts SET {column}=? WHERE artifact_id=?', (raw, artifact))
    elif fault == 'deleted':
        db.execute("UPDATE messages SET body_state='deleted'")
    assert _selected(db) == []
    rec, by_kind, _ = _python(db)
    assert by_kind[kind]['state'] != 'current'
    assert rec['state'] != 'current'


@pytest.mark.parametrize('kind', KINDS[1:])
@pytest.mark.parametrize('version', [MISSING, '0.1', '1.0', '1.1', None])
def test_maintenance_omits_only_projection_counter_not_inference_or_source(db, kind, version):
    artifact = _add(db, kind, version=version,
                    meta={'projection_version': PROJECTION_VERSION - 1})
    selector = current_v4_id if kind == 'semantic_facts_v4' else current_projection_id
    sql = f'SELECT {selector(require_version=False)} FROM messages m'
    assert db.execute(sql).fetchone()[0] == (artifact if version == '1.0' else None)
    db.execute("UPDATE artifacts SET meta=json_set(meta,'$.hash','changed')")
    assert db.execute(sql).fetchone()[0] is None


@pytest.mark.parametrize('old_kind', KINDS[1:])
@pytest.mark.parametrize('version', [MISSING, '0.1', '1.1'])
def test_old_or_future_projection_does_not_shadow_current_legacy(db, old_kind, version):
    current = _add(db, 'extract_llm', content={'summary': 'current synthetic'})
    _add(db, old_kind, version=version, content={'summary': 'obsolete synthetic'})
    assert _selected(db) == [current]
    _, by_kind, _ = _python(db)
    assert by_kind['extract_llm']['artifact_id'] == current
    assert by_kind[old_kind]['state'] == 'stale'


@pytest.mark.parametrize('kind', KINDS[1:])
def test_current_empty_result_suppresses_old_nonempty_fallback(db, kind):
    _add(db, 'extract_llm', version='0.1', content={'summary': 'old nonempty'})
    _add(db, 'semantic_facts_v4', version='0.1',
         content={'canonical_facts': [{'fact_id': 'old', 'statement': 'old fact'}]})
    empty = _add(db, kind, content={})
    assert _selected(db) == [empty]
    _, by_kind, art_rows = _python(db)
    assert _fact_relations(by_kind, art_rows, 'detail') == ([], [])
    assert by_kind['extract_llm']['state'] == 'stale'


def test_same_target_is_used_in_outer_and_every_precedence_subquery(db):
    legacy_old = _add(db, 'extract_llm', version=MISSING)
    v4_current = _add(db, 'semantic_facts_v4')
    future_projection = _add(db, 'canonical_projection', version='1.2')
    assert _selected(db, target='0.1') == [legacy_old]
    assert _selected(db, target='1.0') == [v4_current]
    assert _selected(db, target='1.2') == [future_projection]
    for target, kind, wanted in [('0.1', 'extract_llm', legacy_old),
                                  ('1.0', 'semantic_facts_v4', v4_current),
                                  ('1.2', 'canonical_projection', future_projection)]:
        _, by_kind, _ = _python(db, target=target)
        assert by_kind[kind]['artifact_id'] == wanted


def test_content_and_error_exceptions_do_not_remove_inference_check(db):
    old = _add(db, 'extract_llm', version=MISSING, meta={'error': True})
    db.execute("UPDATE artifacts SET content='broken'")
    pred = current_extract_pred(content=False, error_check=False)
    query = ('SELECT a.artifact_id FROM artifacts a JOIN messages m USING(message_id) '
             'WHERE 1=1 ' + pred)
    assert db.execute(query).fetchall() == []
    db.execute("UPDATE artifacts SET meta=json_set(meta,'$.inference_engine_version','1.0')")
    assert [r[0] for r in db.execute(query)] == [old]


def test_python_kind_map_rejects_version_drift_without_losing_pending_state():
    empty = extraction_kind_map([], 'source-hash')
    assert all(item['state'] == 'pending' for item in empty.values())
    rows = [{'artifact_id': 1, 'kind': 'extract_llm', 'content': {},
             'meta': {'hash': 'source-hash', VERSION_KEY: '0.1'}}]
    assert extraction_kind_map(rows, 'source-hash')['extract_llm']['state'] == 'stale'


@pytest.mark.parametrize('raw,expected', CASES + [
    (json.dumps({VERSION_KEY:'0.0'}),'0.0'),
    (json.dumps({VERSION_KEY:'123456789012345.1234567890123456'}),'123456789012345.1234567890123456'),
    (json.dumps({VERSION_KEY:'1.01'}),None), (json.dumps({VERSION_KEY:'.1'}),None),
    (json.dumps({VERSION_KEY:'1.'}),None), (json.dumps({VERSION_KEY:'-1.0'}),None),
])
def test_valid_version_sql_agrees_with_python_without_promoting_any_version_to_current(db, raw, expected):
    aid = _add(db,'extract_llm')
    db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',(raw,aid))
    value = db.execute('SELECT '+valid_meta_pred()+' FROM artifacts a').fetchone()[0]
    assert type(value) is int and value==int(expected is not None)
    assert read_version(_decode(raw))==expected


@pytest.mark.parametrize('column', [None, '', 'a.meta); DROP TABLE artifacts;--', 'json(meta)', 'a.meta + 1', 'a.meta ', 'a.meta\n'])
def test_valid_version_sql_rejects_expressions_instead_of_accepting_them_as_columns(column):
    with pytest.raises(ValueError,match='invalid inference metadata column'):
        valid_meta_pred(column)


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('invalidated', [True,1,'true','0',{},[]])
def test_sticky_source_invalidation_survives_an_aba_hash_and_matches_python(db, kind, invalidated):
    aid = _add(db,kind,meta={'invalidated':invalidated})
    db.execute("UPDATE messages SET content_hash='second-source'")
    assert _selected(db)==[]
    db.execute("UPDATE messages SET content_hash='source-hash'")
    assert _selected(db)==[]
    _,by_kind,_ = _python(db)
    assert by_kind[kind]['state']=='stale'
    assert db.execute('SELECT a.artifact_id FROM artifacts a JOIN messages m USING(message_id) '
                      'WHERE 1=1 '+source_extract_pred()).fetchall()==[]
    assert db.execute('SELECT count(*) FROM artifacts WHERE artifact_id=?',(aid,)).fetchone()[0]==1


@pytest.mark.parametrize('invalidated', [None,False,0,0.0])
def test_missing_or_explicitly_noninvalidated_source_still_accepts_a_current_empty_result(db, invalidated):
    _add(db,'extract_llm',content={'summary':'obsolete synthetic'},meta={'invalidated':True})
    current = _add(db,'canonical_projection',content={},meta={'invalidated':invalidated})
    assert _selected(db)==[current]
    _,by_kind,rows = _python(db)
    assert by_kind['canonical_projection']['state']=='current'
    assert _fact_relations(by_kind,rows,'detail')==([],[])


@pytest.mark.parametrize('content,error_check', [(True,True),(False,True),(True,False),(False,False)])
def test_source_invalidation_is_never_disabled_by_parsed_or_error_exceptions(db,content,error_check):
    _add(db,'extract_llm',version='0.1',meta={'invalidated':True,'error':True})
    if not content:
        db.execute("UPDATE artifacts SET content='broken'")
    query = 'SELECT a.artifact_id FROM artifacts a JOIN messages m USING(message_id) WHERE 1=1 '
    assert db.execute(query+source_extract_pred(content=content,error_check=error_check)).fetchall()==[]
